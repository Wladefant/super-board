#!/usr/bin/env python3
"""
supabase_staging_health.py - unattended, READ-ONLY health check for STAGING Supabase.

Project: hgzyqmaanndcimnclxtv (PolySimulator-Staging). Production is never contacted: the script
refuses to run unless the staging PAT sees exactly that one project.

What one run does
  1. Calls the Supabase Management API (GET only): security + performance advisors, and ClickHouse
     logs for an explicit, hour-aligned window (default 6 h).
  2. Compares the result with the last stored snapshot (--state-dir).
  3. Posts ONE comment on Wladefant/super-board#467 only when something changed.

Bounds: at most one run per --min-interval-hours (default 6) unless --force; an identical comment
body is never posted twice; log noise is ignored (a log signature counts only when it appears,
disappears, or its count moves 5x or more).

Dry run (default) prints the comment and writes NOTHING. --live posts and updates the snapshot.

  python supabase_staging_health.py            # dry run
  python supabase_staging_health.py --live     # scheduled form
  python install_supabase_health_task.py       # register the Task Scheduler job

The PAT is read only from the documented file and never printed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STAGING_REF = "hgzyqmaanndcimnclxtv"
API = "https://api.supabase.com"
PAT_FILE = Path(os.path.expanduser("~")) / ".veyyon" / "shared-auth" / "supabase_staging_management_pat.txt"
STATE_DIR = Path(os.path.expanduser("~")) / ".veyyon" / "run" / "supabase-health"
ISSUE_REPO = "Wladefant/super-board"
ISSUE_NUMBER = 467
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
HTTP_TIMEOUT = 45
MAX_LISTED = 12  # per list in the comment

LOG_QUERIES: Dict[str, str] = {
    "sources": "select source, count() as n from logs group by source order by n desc limit 50",
    "postgres_errors": (
        "select log_attributes['parsed.error_severity'] as sev, "
        "log_attributes['parsed.sql_state_code'] as sqlstate, count() as n from logs "
        "where source = 'postgres_logs' and log_attributes['parsed.error_severity'] in ('ERROR','FATAL','PANIC') "
        "group by sev, sqlstate order by n desc limit 50"
    ),
    "edge_5xx": (
        "select toInt32OrZero(log_attributes['response.status_code']) as status, count() as n from logs "
        "where source = 'edge_logs' and toInt32OrZero(log_attributes['response.status_code']) >= 500 "
        "group by status order by n desc limit 20"
    ),
}


class HealthError(Exception):
    pass


# --------------------------------------------------------------------------- API


def load_pat(path: Path = PAT_FILE) -> str:
    try:
        pat = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise HealthError(f"cannot read staging PAT file {path.name}: {exc.__class__.__name__}") from None
    if not pat:
        raise HealthError("staging PAT file is empty")
    return pat


def api_get(pat: str, path: str, params: Optional[Dict[str, str]] = None) -> Any:
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {pat}", "User-Agent": "superboard-supabase-health/1"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise HealthError(f"GET {path} -> HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise HealthError(f"GET {path} failed: {exc.__class__.__name__}") from None


def verify_staging_only(pat: str) -> None:
    ids = sorted(p.get("id") for p in api_get(pat, "/v1/projects"))
    if ids != [STAGING_REF]:
        # Never print more than the fact; production must not be reachable with this token.
        raise HealthError(f"PAT scope check failed: sees {len(ids)} project(s), expected only staging")


def fetch_advisor(pat: str, kind: str) -> Dict[str, str]:
    """cache_key -> 'NAME|LEVEL' for one advisor kind (security|performance)."""
    data = api_get(pat, f"/v1/projects/{STAGING_REF}/advisors/{kind}")
    return {l["cache_key"]: f"{l['name']}|{l['level']}" for l in data.get("lints", [])}


def log_window(now: Optional[dt.datetime] = None, hours: int = 6) -> Tuple[str, str]:
    now = (now or dt.datetime.now(dt.timezone.utc)).replace(minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (now - dt.timedelta(hours=hours)).strftime(fmt), now.strftime(fmt)


def query_logs(pat: str, sql: str, start: str, end: str) -> List[Dict[str, Any]]:
    last = "unknown"
    for _ in range(3):  # the logs backend answers 200 + {"error": "Backend error! Retry"} transiently
        data = api_get(pat, f"/v1/projects/{STAGING_REF}/analytics/endpoints/logs",
                       {"sql": sql, "iso_timestamp_start": start, "iso_timestamp_end": end})
        if "result" in data:
            return data["result"]
        last = str(data.get("error", "no result"))[:120]
        if "retry" not in last.lower():
            break
    raise HealthError(f"logs query failed: {last}")


def collect(pat: str, hours: int = 6) -> Dict[str, Any]:
    start, end = log_window(hours=hours)
    snap: Dict[str, Any] = {
        "window": {"start": start, "end": end},
        "advisors": {k: fetch_advisor(pat, k) for k in ("security", "performance")},
        "logs": {},
        "log_error": None,
    }
    try:
        sources = {r["source"]: int(r["n"]) for r in query_logs(pat, LOG_QUERIES["sources"], start, end)}
        pg = {f"{r['sev']}/{r['sqlstate'] or 'none'}": int(r["n"]) for r in query_logs(pat, LOG_QUERIES["postgres_errors"], start, end)}
        edge = {str(r["status"]): int(r["n"]) for r in query_logs(pat, LOG_QUERIES["edge_5xx"], start, end)}
        snap["logs"] = {"sources": sources, "postgres_errors": pg, "edge_5xx": edge}
    except HealthError as exc:
        snap["log_error"] = str(exc)
    return snap


# ------------------------------------------------------------------------ compare


def _counts_by_name(advisor: Dict[str, str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for v in advisor.values():
        out[v] = out.get(v, 0) + 1
    return out


def count_moved(old: int, new: int, factor: int = 5) -> bool:
    return old > 0 and new > 0 and (new >= old * factor or new * factor <= old)


def diff(prev: Optional[Dict[str, Any]], cur: Dict[str, Any]) -> List[str]:
    """Human-readable change lines; empty list means nothing material changed."""
    if prev is None:
        return ["baseline: first snapshot, no earlier one to compare"]
    lines: List[str] = []
    for kind in ("security", "performance"):
        old, new = prev["advisors"].get(kind, {}), cur["advisors"].get(kind, {})
        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        changed = sorted(k for k in set(new) & set(old) if new[k] != old[k])
        for tag, keys, src in (("new", added, new), ("resolved", removed, old), ("level changed", changed, new)):
            if keys:
                lines.append(f"{kind} advisor {tag}: {len(keys)} finding(s)")
                lines.extend(f"  - {k} ({src[k]})" for k in keys[:MAX_LISTED])
                if len(keys) > MAX_LISTED:
                    lines.append(f"  - ... {len(keys) - MAX_LISTED} more")
    if cur.get("log_error") and not prev.get("log_error"):
        lines.append(f"log query started failing: {cur['log_error']}")
    elif prev.get("log_error") and not cur.get("log_error"):
        lines.append("log query works again")
    if not cur.get("log_error") and not prev.get("log_error"):
        for group in ("postgres_errors", "edge_5xx"):
            old, new = prev["logs"].get(group, {}), cur["logs"].get(group, {})
            for sig in sorted(set(new) - set(old)):
                lines.append(f"log {group} new signature {sig}: {new[sig]} event(s)")
            for sig in sorted(set(old) - set(new)):
                lines.append(f"log {group} signature gone {sig}: was {old[sig]} event(s)")
            for sig in sorted(set(new) & set(old)):
                if count_moved(old[sig], new[sig]):
                    lines.append(f"log {group} {sig} moved {old[sig]} -> {new[sig]} (5x or more)")
    return lines


def render_comment(cur: Dict[str, Any], changes: List[str], now_iso: str) -> str:
    w = cur["window"]
    out = [
        f"Staging Supabase health check, {now_iso} (project `{STAGING_REF}`, read-only).",
        "",
        f"Log window: {w['start']} to {w['end']} UTC.",
        "",
        "**What changed since the last snapshot**",
        *[f"- {c}" if not c.startswith("  ") else c for c in changes],
        "",
        "**Current state**",
    ]
    for kind in ("security", "performance"):
        counts = _counts_by_name(cur["advisors"][kind])
        body = ", ".join(f"{k.replace('|', ' ')} x{n}" for k, n in sorted(counts.items())) or "no findings"
        out.append(f"- {kind} advisor: {sum(counts.values())} finding(s): {body}")
    if cur.get("log_error"):
        out.append(f"- logs: query failed ({cur['log_error']})")
    else:
        lg = cur["logs"]
        out.append("- logs by source: " + (", ".join(f"{k} {v}" for k, v in lg["sources"].items()) or "none"))
        out.append("- postgres errors: " + (", ".join(f"{k} x{v}" for k, v in lg["postgres_errors"].items()) or "none"))
        out.append("- edge 5xx: " + (", ".join(f"{k} x{v}" for k, v in lg["edge_5xx"].items()) or "none"))
    out += ["", "Automated by `workflows/portable/supabase_staging_health.py`. Read-only; it changes nothing in Supabase."]
    return "\n".join(out)


# --------------------------------------------------------------------------- state


def load_state(state_dir: Path) -> Dict[str, Any]:
    try:
        return json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state_dir: Path, state: Dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, state_dir / "state.json")


def too_soon(state: Dict[str, Any], now: dt.datetime, min_hours: float) -> bool:
    last = state.get("last_run_utc")
    if not last:
        return False
    try:
        age = now - dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return False
    return age < dt.timedelta(hours=min_hours)


def body_hash(body: str) -> str:
    # Ignore the timestamp line so only real content decides "identical".
    return hashlib.sha256("\n".join(body.split("\n")[1:]).encode("utf-8")).hexdigest()


# -------------------------------------------------------------------------- GitHub


def post_comment(body: str) -> str:
    """Post via `gh api`; returns the comment URL. Fails loudly if the URL cannot be read back."""
    proc = subprocess.run(
        ["gh", "api", f"repos/{ISSUE_REPO}/issues/{ISSUE_NUMBER}/comments", "-f", f"body={body}", "--jq", ".html_url"],
        capture_output=True, text=True, timeout=60, creationflags=CREATE_NO_WINDOW,
    )
    url = (proc.stdout or "").strip()
    if proc.returncode != 0 or not url.startswith("https://github.com/"):
        raise HealthError(f"gh comment post failed rc={proc.returncode} url={url!r}")
    return url


# ----------------------------------------------------------------------------- main


def log(msg: str) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    if sys.stdout is not None:
        print(line)


def run(live: bool, force: bool, state_dir: Path, min_hours: float, hours: int, post=post_comment) -> int:
    now = dt.datetime.now(dt.timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state = load_state(state_dir)
    if live and not force and too_soon(state, now, min_hours):
        log(f"skip: last run {state.get('last_run_utc')} is under {min_hours} h ago")
        return 0
    pat = load_pat()
    verify_staging_only(pat)
    cur = collect(pat, hours)
    changes = diff(state.get("snapshot"), cur)
    if not changes:
        log("no change; nothing to post")
        if live:
            state.update(last_run_utc=now_iso, snapshot=cur)
            save_state(state_dir, state)
        return 0
    body = render_comment(cur, changes, now_iso)
    if not live:
        log("DRY RUN: would post this comment (nothing written):")
        print(body)
        return 0
    if state.get("last_body_hash") == body_hash(body):
        log("identical to the last posted comment; not posting")
        state.update(last_run_utc=now_iso, snapshot=cur)
        save_state(state_dir, state)
        return 0
    url = post(body)
    state.update(last_run_utc=now_iso, snapshot=cur, last_body_hash=body_hash(body), last_comment_url=url)
    save_state(state_dir, state)
    log(f"posted {url}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--live", action="store_true", help="post to the issue and update the snapshot (default: dry run)")
    ap.add_argument("--force", action="store_true", help="ignore the minimum run interval")
    ap.add_argument("--state-dir", default=str(STATE_DIR))
    ap.add_argument("--min-interval-hours", type=float, default=6.0)
    ap.add_argument("--window-hours", type=int, default=6)
    args = ap.parse_args(argv)
    try:
        return run(args.live, args.force, Path(args.state_dir), args.min_interval_hours, args.window_hours)
    except HealthError as exc:
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
