#!/usr/bin/env python3
r"""process_reaper.py - kill agent processes whose owner is gone (Windows).

Why: lanes and sessions leave processes behind. On 2026-10-05 110 veyyon-python-runner
kernels and ~40 console windows pushed host RAM to 99%
(https://github.com/Wladefant/super-board/issues/616). This script is the safety net.
The root-cause fix in veyyon is a separate change.

It reaps three groups and nothing else:
  (a) python runners (`veyyon-python-runner\runner-*.py`): the parent is gone (or the PID was
      reused), or the runner was idle (CPU time unchanged) for more than --idle-min minutes;
  (b) node dev servers (`next dev`, `next start`, `start-server`), `tsc`, and tool-started
      `cmd /c|/k` shells whose ancestor chain ends in a missing process and holds no live
      veyyon.exe. conhost.exe is never killed on its own: it exits when its last client exits,
      and a windowless conhost can host a live pseudo-terminal, so an orphan test is unsafe;
  (c) never a process outside these patterns, never one that used CPU between two samples
      taken --sample-s seconds apart, never one younger than --min-age-s seconds, never
      this script or its ancestors.

Usage:
  python process_reaper.py                 # dry run (default): report, kill nothing
  python process_reaper.py --live          # kill
  python process_reaper.py --json          # machine-readable result on stdout
Options: --max-kills N (cap per run, default 25), --idle-min M (default 20),
         --min-age-s S (default 120), --sample-s S (default 3).

Idle tracking needs history, so every run (dry or live) records each runner's CPU time in
~/.veyyon/run/process-reaper-state.json. Each run appends one line to
~/.veyyon/run/process-reaper.jsonl.

Stdlib only. Every subprocess passes CREATE_NO_WINDOW and a timeout.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
RUN_DIR = Path(os.path.expanduser("~")) / ".veyyon" / "run"
LOG_PATH = RUN_DIR / "process-reaper.jsonl"
STATE_PATH = RUN_DIR / "process-reaper-state.json"
LOG_MAX_BYTES = 2_000_000

RUNNER_RE = re.compile(r"veyyon-python-runner[\\/]+runner-[\w.-]+\.py", re.I)
DEV_RE = re.compile(
    r"(\bnext(\.cmd|\.js)?[\"']?\s+(dev|start)\b|[\\/]next[\\/]dist[\\/]|start-server|"
    r"[\\/]typescript[\\/]lib[\\/]tsc|[\\/]\.bin[\\/]tsc\b|\btsc(\.js|\.cmd)?\s)",
    re.I,
)
PYTHON_NAMES = {"python.exe", "pythonw.exe"}
NODE_NAMES = {"node.exe", "bun.exe"}
TOOL_SHELL_RE = re.compile(r"\s/[ck]\b", re.I)  # cmd /c or /k: started by a tool, not typed by a person
VEYYON_NAME = "veyyon.exe"


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    name: str  # lower-case image name
    cmd: str
    created: float  # epoch seconds
    cpu: float  # kernel + user CPU seconds
    rss: int  # working set bytes


@dataclass
class Decision:
    proc: Proc
    group: str  # runner | devserver | console
    action: str  # reap | keep
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.proc.pid,
            "name": self.proc.name,
            "group": self.group,
            "action": self.action,
            "reason": self.reason,
            "rss_mb": round(self.proc.rss / 1048576, 1),
            "created": self.proc.created,
        }


def state_key(p: Proc) -> str:
    return f"{p.pid}:{int(p.created)}"


# ---------- classification (pure; tests call this with a fake table) ----------


def _owner_chain(p: Proc, by_pid: Dict[int, Proc]) -> Tuple[List[Proc], bool]:
    """Ancestors of p, nearest first, and whether the chain ended in a missing parent."""
    chain: List[Proc] = []
    seen = {p.pid}
    cur = p
    while True:
        parent = by_pid.get(cur.ppid)
        if parent is None or parent.created > cur.created:  # missing, or PID reused
            return chain, True
        if parent.pid in seen:
            return chain, False
        seen.add(parent.pid)
        chain.append(parent)
        cur = parent
        if cur.ppid == 0:
            return chain, False


def group_of(p: Proc) -> Optional[str]:
    if p.name in PYTHON_NAMES and RUNNER_RE.search(p.cmd):
        return "runner"
    if p.name in NODE_NAMES and DEV_RE.search(p.cmd):
        return "devserver"
    if p.name == "tsc.exe":
        return "devserver"
    if p.name == "cmd.exe" and TOOL_SHELL_RE.search(p.cmd):
        return "console"
    return None


def classify(
    procs: List[Proc],
    cpu_resample: Dict[int, float],
    state: Dict[str, Dict[str, float]],
    now: float,
    protected: Optional[set] = None,
    idle_min: float = 20.0,
    min_age_s: float = 120.0,
) -> Tuple[List[Decision], Dict[str, Dict[str, float]]]:
    """Return (decisions for in-pattern processes, new idle state).

    cpu_resample maps pid -> CPU seconds taken a few seconds after `procs`; a changed value
    means the process is busy. state maps "pid:created" -> {"cpu": last cpu, "since": epoch
    when the cpu value last changed}.
    """
    protected = protected or set()
    by_pid = {p.pid: p for p in procs}
    children: Dict[int, List[Proc]] = {}
    for p in procs:
        children.setdefault(p.ppid, []).append(p)
    decisions: List[Decision] = []
    new_state: Dict[str, Dict[str, float]] = {}

    for p in procs:
        group = group_of(p)
        if group is None:
            continue
        if p.pid in protected:
            decisions.append(Decision(p, group, "keep", "this reaper or its ancestor"))
            continue
        busy = cpu_resample.get(p.pid, p.cpu) > p.cpu + 1e-6
        age = now - p.created
        chain, broken = _owner_chain(p, by_pid)
        live_veyyon = any(a.name == VEYYON_NAME for a in chain)

        if group == "runner":
            key = state_key(p)
            prev = state.get(key)
            cpu_now = max(p.cpu, cpu_resample.get(p.pid, p.cpu))
            if prev is None or busy or abs(prev["cpu"] - cpu_now) > 1e-6:
                since = now
            else:
                since = prev["since"]
            new_state[key] = {"cpu": cpu_now, "since": since}
            idle_for = (now - since) / 60.0
            if busy:
                decisions.append(Decision(p, group, "keep", "busy (CPU time changed between samples)"))
            elif age < min_age_s:
                decisions.append(Decision(p, group, "keep", f"younger than {int(min_age_s)}s"))
            elif broken and not live_veyyon:
                decisions.append(Decision(p, group, "reap", "orphan: parent process is gone"))
            elif idle_for > idle_min:
                decisions.append(Decision(p, group, "reap", f"idle {idle_for:.0f} min (limit {idle_min:.0f})"))
            else:
                decisions.append(Decision(p, group, "keep", f"live owner, idle {idle_for:.0f} min"))
            continue

        # devserver / console: only when no veyyon.exe ancestor is alive and the chain is broken
        if busy:
            decisions.append(Decision(p, group, "keep", "busy (CPU time changed between samples)"))
        elif age < min_age_s:
            decisions.append(Decision(p, group, "keep", f"younger than {int(min_age_s)}s"))
        elif live_veyyon:
            decisions.append(Decision(p, group, "keep", "ancestor veyyon.exe is alive"))
        elif not broken:
            decisions.append(Decision(p, group, "keep", "owner chain reaches a live non-veyyon process"))
        else:
            decisions.append(Decision(p, group, "reap", "orphan: ancestor chain ends at a missing process, no live veyyon"))

    # A shell is reaped only when every child it still has is reaped too.
    verdict = {d.proc.pid: d.action for d in decisions}
    for d in decisions:
        if d.group == "console" and d.action == "reap":
            if any(verdict.get(c.pid) != "reap" for c in children.get(d.proc.pid, [])):
                d.action, d.reason = "keep", "shell still has a live child that is not being reaped"
    return decisions, new_state


# ---------- process table ----------

_PS = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ErrorActionPreference='Stop';"
    "Get-CimInstance Win32_Process | ForEach-Object {"
    "[pscustomobject]@{p=[int]$_.ProcessId;pp=[int]$_.ParentProcessId;n=[string]$_.Name;"
    "c=[string]$_.CommandLine;"
    "t=$(if($_.CreationDate){[double]([DateTimeOffset]$_.CreationDate).ToUnixTimeSeconds()}else{0});"
    "k=[double]($_.KernelModeTime+$_.UserModeTime)/10000000;w=[double]$_.WorkingSetSize}"
    "} | ConvertTo-Json -Compress"
)


def list_processes(timeout: int = 60) -> List[Proc]:
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )
    if r.returncode != 0:
        raise RuntimeError(f"process listing failed ({r.returncode}): {r.stderr.strip()[:300]}")
    data = json.loads(r.stdout, strict=False)  # command lines may hold raw control characters
    if isinstance(data, dict):
        data = [data]
    return [
        Proc(int(d["p"]), int(d["pp"]), (d["n"] or "").lower(), d["c"] or "",
             float(d["t"]), float(d["k"]), int(d["w"]))
        for d in data
    ]


def kill_pid(pid: int, timeout: int = 30) -> bool:
    r = subprocess.run(
        ["taskkill", "/F", "/PID", str(pid)],
        capture_output=True, text=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )
    return r.returncode == 0


def self_and_ancestors(procs: List[Proc]) -> set:
    by_pid = {p.pid: p for p in procs}
    out, pid = set(), os.getpid()
    while pid and pid not in out:
        out.add(pid)
        p = by_pid.get(pid)
        pid = p.ppid if p else 0
    return out


# ---------- run ----------


def load_state(path: Path) -> Dict[str, Dict[str, float]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: Dict[str, Dict[str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def append_log(path: Path, entry: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        path.replace(path.with_suffix(".jsonl.1"))
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def run(
    live: bool,
    max_kills: int = 25,
    idle_min: float = 20.0,
    min_age_s: float = 120.0,
    sample_s: float = 3.0,
    log_path: Path = LOG_PATH,
    state_path: Path = STATE_PATH,
    lister: Callable[[], List[Proc]] = list_processes,
    killer: Callable[[int], bool] = kill_pid,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> Dict[str, Any]:
    first = lister()
    sleeper(sample_s)
    second = lister()
    first_ids = {(q.pid, int(q.created)) for q in first}
    resample = {p.pid: p.cpu for p in second if (p.pid, int(p.created)) in first_ids}
    now = clock()
    state = load_state(state_path)
    decisions, new_state = classify(
        first, resample, state, now, protected=self_and_ancestors(first),
        idle_min=idle_min, min_age_s=min_age_s,
    )
    current = {(p.pid, int(p.created)): p for p in second}
    reaped: List[Dict[str, Any]] = []
    errors: List[str] = []
    freed = 0
    candidates = [d for d in decisions if d.action == "reap"]
    for d in candidates:
        entry = d.as_dict()
        if len(reaped) >= max_kills:
            entry["action"] = "skipped-cap"
            reaped.append(entry)
            continue
        entry["killed"] = False
        if live:
            # PID-reuse guard: the same pid with the same start time must still exist.
            if (d.proc.pid, int(d.proc.created)) not in current:
                entry["action"] = "gone-before-kill"
            else:
                try:
                    entry["killed"] = killer(d.proc.pid)
                except (subprocess.SubprocessError, OSError) as exc:
                    errors.append(f"{d.proc.pid}: {exc}")
                if entry["killed"]:
                    freed += d.proc.rss
        reaped.append(entry)
    if live:
        # forget state of killed runners
        for e in reaped:
            if e.get("killed"):
                new_state.pop(f"{e['pid']}:{int(e['created'])}", None)
    save_state(state_path, new_state)

    kept = [d for d in decisions if d.action == "keep"]
    counts_by_group: Dict[str, int] = {}
    for e in reaped:
        if e["action"] == "reap":
            counts_by_group[e["group"]] = counts_by_group.get(e["group"], 0) + 1
    result = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "mode": "live" if live else "dry-run",
        "scanned": len(first),
        "candidates": len(decisions),
        "reap_count": sum(1 for e in reaped if e.get("killed")) if live else len(candidates),
        "kept": len(kept),
        "capped": sum(1 for e in reaped if e["action"] == "skipped-cap"),
        "by_group": counts_by_group,
        "freed_mb": round(freed / 1048576, 1),
        "would_free_mb": round(sum(d.proc.rss for d in candidates) / 1048576, 1),
        "errors": errors,
        "reaped": reaped,
    }
    append_log(log_path, {k: v for k, v in result.items() if k != "reaped"} | {
        "targets": [{"pid": e["pid"], "name": e["name"], "group": e["group"], "reason": e["reason"],
                     "killed": e.get("killed", False)} for e in reaped]
    })
    return result


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (default)")
    mode.add_argument("--live", action="store_true", help="kill the reaped processes")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-kills", type=int, default=25)
    ap.add_argument("--idle-min", type=float, default=20.0)
    ap.add_argument("--min-age-s", type=float, default=120.0)
    ap.add_argument("--sample-s", type=float, default=3.0)
    args = ap.parse_args(argv)
    try:
        result = run(args.live, args.max_kills, args.idle_min, args.min_age_s, args.sample_s)
    except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as exc:
        print(f"process_reaper: {exc}", file=sys.stderr)
        return 2
    if sys.stdout is None:  # pythonw.exe (scheduled task) has no stdout; the log line is the record
        return 0
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{result['mode']}: scanned {result['scanned']}, in-pattern {result['candidates']}, "
              f"reap {result['reap_count']}, keep {result['kept']}, freed {result['freed_mb']} MB "
              f"(would free {result['would_free_mb']} MB)")
        for e in result["reaped"]:
            print(f"  {e['action']:>10} pid {e['pid']} {e['name']} [{e['group']}] {e['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
