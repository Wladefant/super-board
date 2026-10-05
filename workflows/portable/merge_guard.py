#!/usr/bin/env python3
"""merge_guard.py - refuse a PolySimulator staging merge that skipped the lane-brief bookends.

Lane briefs carry two bookends (Wladefant/super-board#227): a Feature Map query at the
start and a Control Glass `QA-RECEIPT` at the end. Both were optional in practice: of the
frontend PRs merged to staging after the bookends became mandatory (2026-09-26 ~22:45Z)
a third carried no receipt and none carried a `feature-map:` note, because nothing stood
between a lane and `gh pr merge`.

This module is that check. The Veyyon extension `superboard-merge-guard.ts` calls
`check-command` on every bash command and on the GitHub MCP merge tool before it runs;
a merge of a `Bavariance/polysimulator` PR into `staging` is refused unless

  1. the PR's browser-QA receipt binds its current diff, whenever the diff reaches the UI
     or an order/trading path (the same rule `github_pr_gate.evaluate_qa_receipt` applies), and
  2. the PR body or one of its comments carries a `feature-map:` line, whenever the diff
     changes non-test product code under `frontend/` or `backend/`.

Every decision is appended to ~/.veyyon/run/merge-guard/decisions.jsonl.

Mode (first match wins): env SUPERBOARD_MERGE_GUARD_MODE, then the one-word file
~/.veyyon/run/merge-guard/mode, then `enforce`.
  enforce  block a merge that fails either bookend
  warn     log the decision, never block (the dry-run mode)
  off      skip the check entirely (emergency off-switch)
A guard that cannot evaluate a merge (GitHub unreachable, unparseable PR) blocks it in
enforce mode and says how to switch the guard off, rather than letting it through blind.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from github_pr_gate import evaluate_qa_receipt, is_test_path  # noqa: E402  (sys.path set above)

GUARDED_REPO = "Bavariance/polysimulator"
GUARDED_BASE = "staging"
STATE_DIR = Path.home() / ".veyyon" / "run" / "merge-guard"
MODES = ("enforce", "warn", "off")
GH_TIMEOUT_SEC = 40
BASE_TIMEOUT_SEC = 20
BASE_CACHE_TTL_SEC = 300  # 5 minutes
# One decision, every gh call included, ends inside this many seconds, so it always beats the
# 120 s the superboard-merge-guard.ts hook gives the script. A timed-out gh call is retried once
# only while more than RETRY_MIN_REMAINING_SEC of that budget is left.
DECISION_DEADLINE_SEC = 100
RETRY_MIN_REMAINING_SEC = 5
TIMEOUT_MARK = "timed out after"  # inside str(subprocess.TimeoutExpired)
_deadline: Optional[float] = None

# A `feature-map:` line, with the markdown decoration lanes put in front of markers. The
# note has to sit on the marker line: an empty marker followed by any next line is no note.
FEATURE_MAP_NOTE_RE = re.compile(r"^[ \t>*_`#|\-]*feature-map:[ \t*_`]*[^\s*_`]", re.IGNORECASE | re.MULTILINE)
PRODUCT_PATH_RE = re.compile(r"^(frontend|backend)/", re.IGNORECASE)
PR_URL_RE = re.compile(r"^https?://github\.com/([^/\s]+/[^/\s]+)/pull/(\d+)")
API_MERGE_PATH_RE = re.compile(r"^/?repos/([^/\s]+/[^/\s]+)/pulls/(\d+)/merge/?$")
# Splits a shell line into the simple commands it runs. Quoted separators are rare in a
# merge invocation; a split inside a quoted title only loses that title, never the merge.
COMMAND_SEPARATOR_RE = re.compile(r"&&|\|\||[;|\n]")
# Cheap prefilter: nothing without both words can be a merge.
MERGE_HINT_RE = re.compile(r"\bgh\b[\s\S]*\bmerge\b")
WRAPPING_SHELLS = {"powershell", "pwsh", "bash", "sh", "cmd", "wsl"}
Runner = Callable[[List[str], Optional[str]], Tuple[int, str, str]]

# gh flags that take a value, so the value is not mistaken for the PR reference or API
# path: global flags, `gh pr merge` flags and `gh api` flags.
GH_VALUE_FLAGS = {
    "-R", "--repo", "--hostname",
    "-t", "--subject", "-b", "--body", "-F", "--body-file", "--match-head-commit", "-A", "--author-email",
    "-H", "--header", "-f", "--field", "--raw-field", "--input", "-q", "--jq", "--template",
    "--cache", "-p", "--preview",
}


def _start_deadline() -> None:
    global _deadline
    _deadline = time.monotonic() + DECISION_DEADLINE_SEC


def _clear_deadline() -> None:
    global _deadline
    _deadline = None


def _remaining() -> Optional[float]:
    return None if _deadline is None else _deadline - time.monotonic()


def _run(cmd: List[str], cwd: Optional[str] = None, timeout: Optional[float] = None) -> Tuple[int, str, str]:
    t = timeout if timeout is not None else GH_TIMEOUT_SEC
    left = _remaining()
    if left is not None:
        t = max(1.0, min(t, left))
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=t, cwd=cwd, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return res.returncode, res.stdout, res.stderr


def _call_runner(
    runner: Runner, cmd: List[str], cwd: Optional[str] = None, timeout: Optional[float] = None
) -> Tuple[int, str, str]:
    try:
        return runner(cmd, cwd, timeout=timeout)
    except TypeError:
        return runner(cmd, cwd)


def _load_base_cache(state_dir: Path) -> Dict[str, Any]:
    cache_path = Path(state_dir) / "base_cache.json"
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_base_cache(cache: Dict[str, Any], state_dir: Path) -> None:
    try:
        p = Path(state_dir)
        p.mkdir(parents=True, exist_ok=True)
        (p / "base_cache.json").write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


_IN_MEMORY_BASE_CACHE: Dict[Tuple[str, str, int], Tuple[str, float]] = {}


def clear_base_cache() -> None:
    _IN_MEMORY_BASE_CACHE.clear()


def get_cached_base(
    repo: str, pr: int, state_dir: Path = STATE_DIR, ttl_sec: float = BASE_CACHE_TTL_SEC
) -> Optional[str]:
    mem_key = (str(Path(state_dir).resolve()), repo, pr)
    now = time.time()
    if mem_key in _IN_MEMORY_BASE_CACHE:
        base, ts = _IN_MEMORY_BASE_CACHE[mem_key]
        if (now - ts) < ttl_sec:
            return base

    cache = _load_base_cache(state_dir)
    key = f"{repo}#{pr}"
    entry = cache.get(key)
    if isinstance(entry, dict):
        ts = entry.get("ts", 0)
        base = entry.get("base")
        if isinstance(base, str) and (now - ts) < ttl_sec:
            _IN_MEMORY_BASE_CACHE[mem_key] = (base, ts)
            return base
    return None


def set_cached_base(repo: str, pr: int, base: str, state_dir: Path = STATE_DIR) -> None:
    mem_key = (str(Path(state_dir).resolve()), repo, pr)
    now = time.time()
    _IN_MEMORY_BASE_CACHE[mem_key] = (base, now)
    cache = _load_base_cache(state_dir)
    key = f"{repo}#{pr}"
    cache[key] = {"base": base, "ts": now}
    _save_base_cache(cache, state_dir)

def fetch_pr_base(
    repo: str,
    pr: int,
    runner: Runner = _run,
    *,
    timeout: float = BASE_TIMEOUT_SEC,
    budget: Optional[float] = None,
    max_retries: int = 1,
    state_dir: Path = STATE_DIR,
    ttl_sec: float = BASE_CACHE_TTL_SEC,
) -> str:
    cached = get_cached_base(repo, pr, state_dir=state_dir, ttl_sec=ttl_sec)
    if cached is not None:
        return cached

    cmd = ["gh", "api", f"repos/{repo}/pulls/{pr}", "--jq", ".base.ref"]
    total_budget = budget if budget is not None else (timeout * (max_retries + 1))
    start_time = time.monotonic()
    last_err = ""

    for attempt in range(max_retries + 1):
        elapsed = time.monotonic() - start_time
        remaining = total_budget - elapsed
        if attempt > 0 and remaining <= 0:
            break
        call_timeout = min(timeout, remaining) if remaining > 0 else timeout

        rc, out, err = _call_runner(runner, cmd, timeout=call_timeout)
        if rc == 0 and out.strip():
            base = out.strip().splitlines()[0].strip()
            set_cached_base(repo, pr, base, state_dir=state_dir)
            return base

        last_err = err.strip() or out.strip() or f"exit code {rc}"

    raise RuntimeError(f"failed to look up base branch for {repo}#{pr}: {last_err or 'timeout'}")


def resolve_mode(env: Optional[Dict[str, str]] = None, state_dir: Path = STATE_DIR) -> str:
    env = os.environ if env is None else env
    raw = (env.get("SUPERBOARD_MERGE_GUARD_MODE") or "").strip().lower()
    if not raw:
        mode_file = state_dir / "mode"
        if mode_file.exists():
            raw = mode_file.read_text(encoding="utf-8").strip().lower()
    return raw if raw in MODES else "enforce"


# ------------------------------------------------------------------ command parsing
def _split_words(segment: str) -> List[str]:
    # POSIX shlex eats backslashes, which would turn `C:\...\gh.exe` into one unrecognisable word.
    segment = segment.replace("\\", "/")
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        # An unbalanced quote: the separator split landed inside a quoted wrapper command.
        return [word.strip("\"'") for word in segment.split()]


def _merge_target(words: List[str]) -> Optional[Dict[str, Any]]:
    """The PR a single `gh ...` command merges, or None when it merges nothing."""
    # Skip env assignments and a path-qualified gh (`C:/.../gh.exe`, `/usr/bin/gh`).
    idx = 0
    while idx < len(words) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[idx]):
        idx += 1
    if idx >= len(words) or re.split(r"[\\/]", words[idx])[-1].lower() not in ("gh", "gh.exe"):
        return None
    rest = words[idx + 1:]
    repo: Optional[str] = None
    positional: List[str] = []
    flags: List[str] = []
    method: Optional[str] = None
    i = 0
    while i < len(rest):
        word = rest[i]
        if word in GH_VALUE_FLAGS or word in ("-X", "--method"):
            value = rest[i + 1] if i + 1 < len(rest) else ""
            if word in ("-R", "--repo"):
                repo = value
            elif word in ("-X", "--method"):
                method = value.upper()
            i += 2
            continue
        if word.startswith("--repo="):
            repo = word.split("=", 1)[1]
        elif word.startswith("--method="):
            method = word.split("=", 1)[1].upper()
        elif word.startswith("-X") and len(word) > 2:
            method = word[2:].upper()
        elif word.startswith("-"):
            flags.append(word)
        else:
            positional.append(word)
        i += 1

    if positional[:2] == ["pr", "merge"]:
        if "--disable-auto" in flags:
            return None
        ref = positional[2] if len(positional) > 2 else None
        pr: Optional[int] = None
        if ref:
            url = PR_URL_RE.match(ref)
            if url:
                repo, pr = url.group(1), int(url.group(2))
            elif ref.lstrip("#").isdigit():
                pr = int(ref.lstrip("#"))
            else:
                # A branch name: gh resolves it, and so does the guard.
                return {"repo": repo, "pr": None, "ref": ref, "via": "gh pr merge"}
        return {"repo": repo, "pr": pr, "ref": ref, "via": "gh pr merge"}

    if positional[:1] == ["api"] and (method or "GET") == "PUT":
        for word in positional[1:]:
            path = API_MERGE_PATH_RE.match(word)
            if path:
                return {"repo": path.group(1), "pr": int(path.group(2)), "ref": None, "via": "gh api merge"}
    return None


def parse_merge_commands(command: str) -> List[Dict[str, Any]]:
    """Every PR merge a shell command line would run, in order."""
    if not MERGE_HINT_RE.search(command or ""):
        return []
    targets = []
    for segment in COMMAND_SEPARATOR_RE.split(command):
        words = _split_words(segment.strip())
        target = _merge_target(words)
        if target:
            targets.append(target)
        elif words and re.split(r"[\\/]", words[0])[-1].lower().removesuffix(".exe") in WRAPPING_SHELLS:
            # `powershell -Command "gh pr merge ..."`: the merge sits inside one quoted word.
            for word in words[1:]:
                if " " in word:
                    targets.extend(parse_merge_commands(word))
    return targets


# ------------------------------------------------------------------ PR data
def _gh_call(cmd: List[str], runner: Runner, cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """Run one gh command; retry once, and only when it timed out and budget remains."""
    rc, out, err = runner(cmd, cwd)
    left = _remaining()
    if rc != 0 and TIMEOUT_MARK in err and (left is None or left > RETRY_MIN_REMAINING_SEC):
        rc, out, err = runner(cmd, cwd)
    return rc, out, err


def _gh_json(args: List[str], runner: Runner, cwd: Optional[str] = None) -> Any:
    rc, out, err = _gh_call(["gh", *args], runner, cwd)
    if rc != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {err.strip() or out.strip()}")
    return json.loads(out)


def _gh_pages(path: str, runner: Runner) -> List[Dict[str, Any]]:
    rc, out, err = _gh_call(["gh", "api", path, "--paginate"], runner)
    if rc != 0:
        raise RuntimeError(f"gh api {path} failed: {err.strip()}")
    from review_content import json_pages

    return [item for page in json_pages(out) for item in page]


def resolve_target(target: Dict[str, Any], cwd: Optional[str], runner: Runner) -> Tuple[str, int]:
    """Fill in the repo and PR number gh itself would infer from the checkout."""
    repo, pr = target.get("repo"), target.get("pr")
    if repo and pr:
        return repo, int(pr)
    args = ["pr", "view"]
    if target.get("ref"):
        args.append(str(target["ref"]))
    if repo:
        args += ["--repo", repo]
    data = _gh_json(args + ["--json", "number,url"], runner, cwd)
    url = PR_URL_RE.match(str(data.get("url") or ""))
    if not url:
        raise RuntimeError(f"could not resolve the PR gh would merge from {cwd or 'the current directory'}")
    return url.group(1), int(url.group(2))


def fetch_pr(repo: str, pr: int, runner: Runner) -> Dict[str, Any]:
    """The PR in the shape `github_pr_gate.evaluate_qa_receipt` reads, plus body and base."""
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_pull = pool.submit(_gh_json, ["api", f"repos/{repo}/pulls/{pr}"], runner)
        f_files = pool.submit(_gh_pages, f"repos/{repo}/pulls/{pr}/files?per_page=100", runner)
        f_comments = pool.submit(_gh_pages, f"repos/{repo}/issues/{pr}/comments?per_page=100", runner)
        f_reviews = pool.submit(_gh_pages, f"repos/{repo}/pulls/{pr}/reviews?per_page=100", runner)
        pull = f_pull.result()
        files = f_files.result()
        comments = f_comments.result()
        reviews = f_reviews.result()
    return {
        "number": pr,
        "state": str(pull.get("state") or "").upper(),
        "merged": bool(pull.get("merged")),
        "headRefOid": (pull.get("head") or {}).get("sha") or "",
        "baseRefName": (pull.get("base") or {}).get("ref") or "",
        "body": pull.get("body") or "",
        "files": [{"path": f.get("filename", "")} for f in files],
        "comments": comments,
        "reviews": reviews,
    }


# ------------------------------------------------------------------ bookends
def feature_map_requirement(pr_data: Dict[str, Any]) -> Tuple[bool, str]:
    for f in pr_data.get("files") or []:
        path = (f.get("path", "") if isinstance(f, dict) else str(f)).replace("\\", "/")
        if PRODUCT_PATH_RE.match(path) and not is_test_path(path):
            return True, f"product path {path}"
    return False, "no non-test frontend/ or backend/ paths"


def evaluate_feature_map(pr_data: Dict[str, Any]) -> Tuple[str, str]:
    """EXEMPT, PASSED, or REQUIRED: a product-code PR has to record its Feature Map lookup."""
    required, reason = feature_map_requirement(pr_data)
    if not required:
        return "EXEMPT", reason
    sources = [pr_data.get("body") or ""] + [
        str(item.get("body") or "") for item in list(pr_data.get("comments") or []) + list(pr_data.get("reviews") or [])
    ]
    if any(FEATURE_MAP_NOTE_RE.search(text) for text in sources):
        return "PASSED", f"feature-map note present ({reason})"
    return (
        "REQUIRED",
        f"feature-map note required ({reason}): add a line 'feature-map: <matched feature, entry files, "
        "tests, owning issue>' (or 'feature-map: no match for <term>') to the PR body, from "
        "`python ~/.veyyon/workflows/feature_map.py query <term>`.",
    )


def evaluate_bookends(pr_data: Dict[str, Any], repo: str, cwd: Optional[str] = None) -> Dict[str, Any]:
    base = str(pr_data.get("baseRefName") or "")
    if repo != GUARDED_REPO or base != GUARDED_BASE:
        if repo == GUARDED_REPO and base == "main":
            return {
                "guarded": True,
                "block": True,
                "reason": f"merges into {repo}@{base} are blocked: direct merges to main are forbidden (staging-only policy; AGENTS.md §4)",
            }
        return {"guarded": False, "block": False, "reason": f"{repo}@{base or 'unknown'} is not guarded"}
    qa_verdict, qa_reason, qa_url = evaluate_qa_receipt(
        pr_data, repo=repo, base_ref=base, head_sha=str(pr_data.get("headRefOid") or ""), cwd=cwd
    )
    fm_verdict, fm_reason = evaluate_feature_map(pr_data)
    failures = [reason for verdict, reason in ((qa_verdict, qa_reason), (fm_verdict, fm_reason)) if verdict == "REQUIRED"]
    return {
        "guarded": True,
        "block": bool(failures),
        "qa_receipt": {"verdict": qa_verdict, "reason": qa_reason, "url": qa_url},
        "feature_map": {"verdict": fm_verdict, "reason": fm_reason},
        "reason": " ".join(failures) if failures else "lane-brief bookends present",
    }


def _polysim_checkout(cwd: Optional[str], runner: Runner) -> Optional[str]:
    """`cwd` when it is a PolySimulator checkout, so receipts naming a pre-sync head can bind."""
    if not cwd:
        return None
    rc, out, _ = runner(["git", "remote", "get-url", "origin"], cwd)
    return cwd if rc == 0 and GUARDED_REPO.lower() in out.lower() else None


def check_command(
    command: str,
    cwd: Optional[str] = None,
    *,
    mode: Optional[str] = None,
    runner: Runner = _run,
    state_dir: Path = STATE_DIR,
    targets: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Decide whether a command may run. `block` is only ever true in enforce mode."""
    mode = mode or resolve_mode(state_dir=state_dir)
    targets = parse_merge_commands(command) if targets is None else targets
    decision: Dict[str, Any] = {"mode": mode, "block": False, "merges": [], "reason": "no PR merge in command"}
    if not targets or mode == "off":
        if targets:
            decision["reason"] = "merge guard is off"
        return decision

    _start_deadline()
    blocked_reasons = []
    checkout = _polysim_checkout(cwd, runner)
    for target in targets:
        entry: Dict[str, Any] = {"via": target.get("via")}
        try:
            repo, pr = resolve_target(target, cwd, runner)
            entry.update(repo=repo, pr=pr)
            if repo != GUARDED_REPO:
                entry.update(guarded=False, block=False, reason=f"{repo} is not guarded")
            else:
                base = fetch_pr_base(repo, pr, runner, state_dir=state_dir)
                entry.update(base=base)
                if base == "main":
                    entry.update(
                        guarded=True,
                        block=True,
                        reason=f"merges into {repo}@{base} are blocked: direct merges to main are forbidden (staging-only policy; AGENTS.md §4)",
                    )
                elif base != GUARDED_BASE:
                    entry.update(
                        guarded=False,
                        block=False,
                        reason=f"{repo}@{base} is not guarded",
                    )
                else:
                    pr_data = fetch_pr(repo, pr, runner)
                    entry.update(head=pr_data["headRefOid"])
                    entry.update(evaluate_bookends(pr_data, repo, checkout))
        except (RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            entry.update(
                guarded=True, block=True,
                reason=f"merge guard could not evaluate this merge ({exc}); it fails closed",
            )
        decision["merges"].append(entry)
        if entry.get("block"):
            label = f"{entry.get('repo', '?')}#{entry.get('pr', '?')}"
            blocked_reasons.append(f"{label}: {entry['reason']}")

    if blocked_reasons:
        decision["would_block"] = True
        decision["block"] = mode == "enforce"
        decision["reason"] = (
            "Merge refused by the super-board merge guard (Wladefant/super-board#227). "
            + " | ".join(blocked_reasons)
            + " Post the missing bookend on the PR, then merge again. Emergency off-switch: "
            "write 'off' to ~/.veyyon/run/merge-guard/mode (operator only)."
        )
    else:
        decision["reason"] = "lane-brief bookends present on every merged PR"
    _clear_deadline()
    _log(decision, command, cwd, state_dir)
    return decision


def _log(decision: Dict[str, Any], command: str, cwd: Optional[str], state_dir: Path) -> None:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cwd": cwd,
            "command": command[:500],
            **decision,
        }
        with (state_dir / "decisions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")
    except OSError as exc:
        print(f"merge_guard: decision log not written: {exc}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="action", required=True)
    cmd = sub.add_parser("check-command", help="decide whether a shell command may run")
    cmd.add_argument("--command", required=True)
    cmd.add_argument("--cwd")
    pr = sub.add_parser("check-pr", help="decide whether one PR may merge")
    pr.add_argument("--repo", required=True)
    pr.add_argument("--pr", required=True, type=int)
    pr.add_argument("--cwd")
    for p in (cmd, pr):
        p.add_argument("--mode", choices=MODES, help="override the configured mode")
    args = parser.parse_args(argv)

    if args.action == "check-command":
        decision = check_command(args.command, args.cwd, mode=args.mode)
    else:
        target = {"repo": args.repo, "pr": args.pr, "ref": None, "via": "check-pr"}
        decision = check_command(
            f"check-pr {args.repo}#{args.pr}", args.cwd, mode=args.mode, targets=[target]
        )
    print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
