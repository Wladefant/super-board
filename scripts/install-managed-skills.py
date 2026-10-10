#!/usr/bin/env python3
"""Copy managed skills from this repo to the Veyyon profile. The repo is the source of truth.

Direction: <repo>/managed-skills/<name>/ -> ~/.veyyon/profiles/<profile>/agent/managed-skills/<name>/.
Edit a skill in the repo, merge it, then run this script. A profile file that differs from the
repo is copied to --backup-dir first, so a local edit is never lost; port it into the repo.
Line endings do not count as a difference. Files that exist only in the profile stay.

Usage: install-managed-skills.py [NAME ...] [--profile NAME] [--dest PATH] [--check]
No NAME installs every skill folder that has a SKILL.md. --check writes nothing and exits 1
when any profile file is missing or differs.
"""

import argparse
import shutil
import sys
import time
from pathlib import Path

REPO_SKILLS = Path(__file__).resolve().parent.parent / "managed-skills"


def same(a: Path, b: Path) -> bool:
    return b.is_file() and a.read_bytes().replace(b"\r\n", b"\n") == b.read_bytes().replace(b"\r\n", b"\n")


def skill_names(src: Path, wanted: list[str]) -> list[str]:
    have = sorted(p.name for p in src.iterdir() if (p / "SKILL.md").is_file())
    unknown = [n for n in wanted if n not in have]
    if unknown:
        raise SystemExit(f"not in {src}: {', '.join(unknown)}")
    return wanted or have


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*")
    ap.add_argument("--src", default=str(REPO_SKILLS))
    ap.add_argument("--profile", default="default")
    ap.add_argument("--dest")
    ap.add_argument("--backup-dir", default=str(Path.home() / ".veyyon" / "tmp" / "managed-skills-backup"))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    src = Path(args.src)
    dest = Path(args.dest) if args.dest else Path.home() / ".veyyon" / "profiles" / args.profile / "agent" / "managed-skills"
    backup = Path(args.backup_dir) / time.strftime("%Y%m%dT%H%M%S")
    drift, backed_up = [], []
    for name in skill_names(src, args.names):
        for file in sorted(p for p in (src / name).rglob("*") if p.is_file()):
            rel = file.relative_to(src)
            target = dest / rel
            if same(file, target):
                continue
            drift.append(str(rel).replace("\\", "/"))
            if args.check:
                continue
            if target.is_file():
                saved = backup / rel
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
                backed_up.append(str(saved))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
    print(("differs: " if args.check else "installed: ") + (", ".join(drift) or "none"))
    if backed_up:
        print("backed up local versions: " + ", ".join(backed_up))
    return 1 if (args.check and drift) else 0


if __name__ == "__main__":
    sys.exit(main())
