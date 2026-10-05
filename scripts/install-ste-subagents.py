#!/usr/bin/env python3
"""Append the one-line STE requirement to every subagent prompt in ~/.veyyon/subagents.

Idempotent: the line sits between <!-- ste-writing:begin --> and <!-- ste-writing:end -->
markers and is replaced in place on re-run. Other marker blocks are never touched.
Backup files (*.bak*) are skipped. Usage: install-ste-subagents.py [--dir PATH] [--check]
--check exits 1 when any prompt lacks the block and writes nothing.
"""

import argparse
import re
import sys
from pathlib import Path

BEGIN = "<!-- ste-writing:begin -->"
END = "<!-- ste-writing:end -->"
LINE = (
    "Operator-facing text (final answer, PR and issue text, handoffs, Telegram) follows the skill "
    "`ste-writing`: state in line 1, 25 words or fewer per sentence, active voice, plain words. "
    "Keep every fact, link and hedge. Check with "
    "`python ~/.veyyon/workflows/ste_check.py check <file>` (it warns and never blocks)."
)
BLOCK = f"{BEGIN}\n{LINE}\n{END}"
BLOCK_RE = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)


def prompts(directory: Path):
    return sorted(p for p in directory.glob("*.md") if ".bak" not in p.name)


def with_block(text: str) -> str:
    if BLOCK_RE.search(text):
        return BLOCK_RE.sub(lambda _m: BLOCK, text, count=1)
    return text.rstrip("\n") + "\n\n" + BLOCK + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", default=str(Path.home() / ".veyyon" / "subagents"))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    missing = []
    for path in prompts(Path(args.dir)):
        text = path.read_text(encoding="utf-8")
        new = with_block(text)
        if new == text:
            continue
        missing.append(path.name)
        if not args.check:
            path.write_text(new, encoding="utf-8", newline="")
    print(("missing: " if args.check else "updated: ") + (", ".join(missing) or "none"))
    return 1 if (args.check and missing) else 0


if __name__ == "__main__":
    sys.exit(main())
