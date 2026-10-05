#!/usr/bin/env python3
"""lessons.py - the per-repo `## Lessons` section with its 20-line cap (Wladefant/super-board#503).

Operator corrections become one `When X, do Y` line, newest first, in the repo's
AGENTS.md (or CLAUDE.md when there is no AGENTS.md). Rules (profile policy section 14):
  - the same mistake twice means rewrite the old line, not add a new one;
  - at most 20 lines; past that the script prints a merge proposal and edits nothing;
  - a repo whose `Project stage:` is `live` is never written without a PR: pass
    --pr-branch to say the checkout is a PR branch.

  lessons.py add <repo> "When X, do Y" [--pr-branch]
  lessons.py check <repo>...

<repo> is a checkout directory or an AGENTS.md / CLAUDE.md file. Exit codes: 0 ok,
1 check found a problem, 2 near-duplicate (rewrite), 3 live repo without --pr-branch,
4 over the cap (proposal printed), 5 bad input or no Lessons section.
Standard library only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

CAP = 20
DUP_OVERLAP = 0.6
LINE_RE = re.compile(r"^When .+, do .+$")
HEADING_RE = re.compile(r"^## +Lessons\s*$", re.MULTILINE)
STAGE_RE = re.compile(r"^Project stage:\s*(\w+)", re.MULTILINE)
_STOP = frozenset("a an the to of in on for and or is it do when be with at by as that this".split())


def find_file(target: str) -> Path:
    path = Path(target)
    if path.is_file():
        return path
    for name in ("AGENTS.md", "CLAUDE.md"):
        if (path / name).is_file() and HEADING_RE.search((path / name).read_text(encoding="utf-8")):
            return path / name
    raise FileNotFoundError(f"no AGENTS.md or CLAUDE.md with a '## Lessons' section in {target}")


def stage_of(text: str) -> str:
    m = STAGE_RE.search(text)
    return m.group(1).lower() if m else "live"


def locate(text: str) -> Tuple[int, int, List[Tuple[int, str]]]:
    """(first line index after the heading, end index, [(line index, lesson text)])."""
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if HEADING_RE.match(l.rstrip("\r"))), None)
    if start is None:
        raise ValueError("no '## Lessons' section")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    lessons = [(i, lines[i].rstrip("\r")[2:].strip()) for i in range(start + 1, end) if lines[i].startswith("- ")]
    return start + 1, end, lessons


def _tokens(line: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", line.lower()) if w not in _STOP and len(w) > 2}


def overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / min(len(ta), len(tb)) if ta and tb else 0.0


def near_duplicate(line: str, existing: List[str]) -> Optional[str]:
    return next((old for old in existing if overlap(line, old) >= DUP_OVERLAP), None)


def merge_proposal(lines: List[str]) -> str:
    pairs = [(a, b) for i, a in enumerate(lines) for b in lines[i + 1:] if overlap(a, b) >= DUP_OVERLAP]
    out = [f"{len(lines)} lessons, cap is {CAP}. Merge proposal for the operator (nothing was edited):"]
    out += [f"  merge: {a}\n    with: {b}" for a, b in pairs] or ["  no near-duplicates found; drop the outdated ones:"]
    out += [f"  oldest: {l}" for l in lines[-3:]]
    return "\n".join(out)


def add_line(text: str, line: str) -> str:
    """Insert `line` as the newest lesson in the `## Lessons` section."""
    crlf = "\r\n" in text
    start, end, lessons = locate(text)
    lines = text.split("\n")
    eol = "\r" if crlf else ""
    if lessons:
        at = lessons[0][0]
    else:
        at = end
        while at > start and lines[at - 1].strip() == "":
            at -= 1
        lines.insert(at, eol)
        at += 1
    lines.insert(at, "- " + line + eol)
    if not lessons and at + 1 < len(lines) and lines[at + 1].startswith("## "):
        lines.insert(at + 1, eol)
    return "\n".join(lines)


def run_add(target: str, line: str, pr_branch: bool) -> int:
    line = " ".join(line.split())
    if not LINE_RE.match(line):
        print('lessons: a lesson must read "When X, do Y"', file=sys.stderr)
        return 5
    try:
        path = find_file(target)
        text = path.read_bytes().decode("utf-8")
        _, _, lessons = locate(text)
    except (FileNotFoundError, ValueError) as exc:
        print(f"lessons: {exc}", file=sys.stderr)
        return 5
    if stage_of(text) == "live" and not pr_branch:
        print(f"lessons: {path} is a live repo; edit it on a PR branch and pass --pr-branch", file=sys.stderr)
        return 3
    texts = [t for _, t in lessons]
    old = near_duplicate(line, texts)
    if old:
        print(f"lessons: same mistake twice, rewrite the old line instead of adding one:\n  old: {old}\n  new: {line}")
        return 2
    if len(texts) >= CAP:
        print(merge_proposal(texts + [line]))
        return 4
    path.write_bytes(add_line(text, line).encode("utf-8"))
    print(f"lessons: added to {path} ({len(texts) + 1}/{CAP})")
    return 0


def run_check(targets: List[str]) -> int:
    status = 0
    for target in targets:
        try:
            path = find_file(target)
            text = path.read_bytes().decode("utf-8")
            _, _, lessons = locate(text)
        except (FileNotFoundError, ValueError) as exc:
            print(f"FAIL {target}: {exc}")
            status = 1
            continue
        texts = [t for _, t in lessons]
        problems = []
        if len(texts) > CAP:
            problems.append(merge_proposal(texts))
        problems += [f"near-duplicate, rewrite one: {a} | {b}" for i, a in enumerate(texts)
                     for b in texts[i + 1:] if overlap(a, b) >= DUP_OVERLAP]
        problems += [f"not 'When X, do Y': {t}" for t in texts if not LINE_RE.match(t)]
        print(f"{'FAIL' if problems else 'ok  '} {path}: {len(texts)}/{CAP} lessons")
        for p in problems:
            print("  " + p)
        status = 1 if problems else status
    return status


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("repo")
    a.add_argument("lesson")
    a.add_argument("--pr-branch", action="store_true")
    c = sub.add_parser("check")
    c.add_argument("repo", nargs="+")
    args = ap.parse_args(argv)
    return run_add(args.repo, args.lesson, args.pr_branch) if args.cmd == "add" else run_check(args.repo)


if __name__ == "__main__":
    sys.exit(main())
