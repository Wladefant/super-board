#!/usr/bin/env python3
"""
pr_label_bot.py - deterministic labels on open PRs, computed from the diff.

The agent no longer has to remember these (issue #208, T3 Code model):
  size:XS|S|M|L|XL|XXL   from effective changed lines (additions + deletions,
                         lockfiles and generated files excluded, same rule as
                         github_pr_gate.evaluate_review_requirement)
  risk:money-path        a changed path matches the money/billing/ledger regex
  risk:migration         a changed path matches alembic/ or migrations/
  risk:high              a changed path matches the auth/token/rls regex

Ownership: the bot owns `size:*` only (it swaps a stale size label for the right
one; `size:exempt` is a human opt-out and is never touched or replaced). Risk
labels are ADD-only: the bot never removes a label a human or lane set.

Dry-run is the default and writes nothing. `--live` applies at most
`--max-writes` PR edits per run, then reads every edited PR back and fails if a
label did not land. A second run on the same state makes 0 writes.

  python pr_label_bot.py --repo owner/repo [--live] [--max-writes 20] [--pr N]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from github_pr_gate import (  # noqa: E402
    AUTH_PATH_RE,
    MIGRATION_PATH_RE,
    MONEY_PATH_RE,
    is_lockfile_or_generated,
)

SIZE_BUCKETS: List[Tuple[int, str]] = [
    (10, "size:XS"),
    (30, "size:S"),
    (100, "size:M"),
    (250, "size:L"),
    (500, "size:XL"),
]
SIZE_TOP = "size:XXL"
SIZE_LABELS = [name for _, name in SIZE_BUCKETS] + [SIZE_TOP]
SIZE_COLORS = {"size:XS": "c2e0c6", "size:S": "bfdadc", "size:M": "fef2c0", "size:L": "f9d0c4", "size:XL": "e99695", "size:XXL": "d93f0b"}
SIZE_EXEMPT = "size:exempt"
FILE_LIST_CAP = 100  # `gh pr list --json files` is capped; a capped list undercounts

Runner = Callable[[List[str], int], str]


def default_runner(cmd: List[str], timeout: int) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])} failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}")
    return proc.stdout


def effective_lines(pr: Dict[str, Any]) -> int:
    files = pr.get("files") or []
    if len(files) >= FILE_LIST_CAP or not files:
        # The file list cannot be trusted; fall back to the PR totals (over-counts lockfiles, never under-counts).
        return int(pr.get("additions") or 0) + int(pr.get("deletions") or 0)
    return sum(
        int(f.get("additions") or 0) + int(f.get("deletions") or 0)
        for f in files
        if not is_lockfile_or_generated(f.get("path", ""))
    )


def size_label(lines: int) -> str:
    for limit, name in SIZE_BUCKETS:
        if lines < limit:
            return name
    return SIZE_TOP


def risk_labels(pr: Dict[str, Any]) -> List[str]:
    found = set()
    for f in pr.get("files") or []:
        path = (f.get("path") or "").replace("\\", "/")
        if MONEY_PATH_RE.search(path):
            found.add("risk:money-path")
        if MIGRATION_PATH_RE.search(path):
            found.add("risk:migration")
        if AUTH_PATH_RE.search(path):
            found.add("risk:high")
    return sorted(found)


def plan_pr(pr: Dict[str, Any]) -> Dict[str, Any]:
    have = {(l.get("name") if isinstance(l, dict) else str(l)) for l in pr.get("labels") or []}
    add: List[str] = []
    remove: List[str] = []
    want_size = None
    if SIZE_EXEMPT not in have:
        want_size = size_label(effective_lines(pr))
        remove = sorted(l for l in have if l in SIZE_LABELS and l != want_size)
        if want_size not in have:
            add.append(want_size)
    add += [r for r in risk_labels(pr) if r not in have]
    return {"number": pr["number"], "size": want_size, "add": add, "remove": remove}


def ensure_labels(repo: str, needed: List[str], existing: set, live: bool, runner: Runner) -> List[str]:
    created = []
    for name in needed:
        if name in existing or name not in SIZE_COLORS:
            continue
        created.append(name)
        if live:
            runner(["gh", "label", "create", name, "-R", repo, "--color", SIZE_COLORS[name],
                    "--description", "Computed by pr_label_bot from effective changed lines"], 30)
    return created


def run_sweep(repo: str, live: bool, max_writes: int, only_pr: Optional[int] = None, runner: Runner = default_runner) -> Dict[str, Any]:
    raw = runner(["gh", "pr", "list", "-R", repo, "--state", "open", "-L", "200",
                  "--json", "number,labels,files,additions,deletions"], 120)
    prs = json.loads(raw)
    if only_pr is not None:
        prs = [p for p in prs if p["number"] == only_pr]
    plans = [p for p in (plan_pr(pr) for pr in prs) if p["add"] or p["remove"]]
    existing = {l["name"] for l in json.loads(runner(["gh", "label", "list", "-R", repo, "-L", "300", "--json", "name"], 60))}
    needed = sorted({a for p in plans for a in p["add"]})
    created = ensure_labels(repo, needed, existing, live, runner)

    applied, failed = [], []
    skipped = max(0, len(plans) - max_writes)
    if live:
        for plan in plans[:max_writes]:
            cmd = ["gh", "pr", "edit", str(plan["number"]), "-R", repo]
            for a in plan["add"]:
                cmd += ["--add-label", a]
            for r in plan["remove"]:
                cmd += ["--remove-label", r]
            runner(cmd, 60)
            back = json.loads(runner(["gh", "pr", "view", str(plan["number"]), "-R", repo, "--json", "labels"], 60))
            names = {l["name"] for l in back["labels"]}
            ok = all(a in names for a in plan["add"]) and not any(r in names for r in plan["remove"])
            (applied if ok else failed).append(plan["number"])
    return {"repo": repo, "mode": "live" if live else "dry-run", "open_prs": len(prs), "planned": plans,
            "labels_created": created, "applied": applied, "readback_failed": failed, "deferred_over_cap": skipped}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--live", action="store_true", help="apply changes (default: dry-run)")
    ap.add_argument("--max-writes", type=int, default=20)
    ap.add_argument("--pr", type=int, help="limit to one PR")
    args = ap.parse_args(argv)
    report = run_sweep(args.repo, args.live, args.max_writes, args.pr)
    print(json.dumps(report, indent=2))
    return 1 if report["readback_failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
