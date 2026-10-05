"""Install the change-loop block into the installed subagent prompts (Wladefant/super-board#501).

The block lives in policies/default/subagents/change-loop.md. It is written between
`<!-- change-loop:begin -->` and `<!-- change-loop:end -->` markers, so a re-run replaces it
instead of duplicating it. When another marker block already ends the file (for
example the ste-writing one), the new block goes before it and that block is never
edited. Each changed file gets a `.bak-greenfield-<date>` backup first.

  python install_change_loop_subagents.py            install or update
  python install_change_loop_subagents.py --check    exit 1 when an installed copy differs
"""
from __future__ import annotations

import argparse
import datetime
from pathlib import Path
import re
import sys

BEGIN = "<!-- change-loop:begin -->"
END = "<!-- change-loop:end -->"
AGENTS = (
    "sonnet", "task", "qa-verifier", "opus", "reviewer", "codex-worker",
    "codex-reviewer", "agc-sonnet", "agc-opus", "astra-ux", "ds-task",
)
SOURCE = Path(__file__).resolve().parents[2] / "policies/default/subagents/change-loop.md"
SUBAGENTS = Path.home() / ".veyyon/subagents"
_OTHER_BEGIN = re.compile(r"^<!-- (?!change-loop:)[\w-]+:begin -->\s*$", re.MULTILINE)


def block_text(source: Path = SOURCE) -> str:
    text = source.read_text(encoding="utf-8").replace("\r\n", "\n").strip("\n")
    if not (text.startswith(BEGIN) and text.endswith(END)):
        raise ValueError(f"{source} must start with {BEGIN} and end with {END}")
    return text + "\n"


def render(current: str, block: str) -> str:
    """Return `current` with the block installed exactly once."""
    text = current.replace("\r\n", "\n")
    start = text.find(BEGIN)
    if start != -1:
        end = text.index(END, start) + len(END)
        return text[:start] + block.rstrip("\n") + text[end:]
    other = _OTHER_BEGIN.search(text)
    if other:
        return text[: other.start()] + block + text[other.start():]
    return text.rstrip("\n") + "\n" + block


def sync(subagents: Path, block: str, check: bool, today: str) -> list[str]:
    changed = []
    for name in AGENTS:
        path = subagents / f"{name}.md"
        if not path.exists():
            continue
        current = path.read_bytes().decode("utf-8")
        wanted = render(current, block)
        if "\r\n" in current:
            wanted = wanted.replace("\n", "\r\n")
        if wanted == current:
            continue
        changed.append(name)
        if not check:
            backup = path.with_name(f"{path.name}.bak-greenfield-{today}")
            if not backup.exists():
                backup.write_bytes(current.encode("utf-8"))
            path.write_bytes(wanted.encode("utf-8"))
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--subagents", type=Path, default=SUBAGENTS)
    args = parser.parse_args(argv)
    block = block_text()
    changed = sync(args.subagents, block, args.check, datetime.date.today().strftime("%Y%m%d"))
    verb = "differs" if args.check else "updated"
    print(f"{verb}: {', '.join(changed) if changed else 'none'}")
    return 1 if (args.check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
