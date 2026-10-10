#!/usr/bin/env python3
"""Copy managed skills from this repo to the Veyyon profile. The repo is the source of truth.

Direction: <repo>/managed-skills/<name>/ -> ~/.veyyon/profiles/<profile>/agent/managed-skills/<name>/.
Edit a skill in the repo, merge it, then run this script. A profile file that differs from the
repo is copied to --backup-dir first, so a local edit is never lost; port it into the repo.
Line endings do not count as a difference. Files that exist only in the profile stay.

Local values: the repo text is scrubbed, so it holds placeholders such as `<prod-supabase-ref>`.
The values file (default ~/.veyyon/profiles/<profile>/agent/managed-skills.local.json, never in
the repo) maps them back: {"*": {"<placeholder>": "value"}, "<skill>": {"<placeholder>": "value"}}.
"*" applies to every skill; a skill entry applies to that skill only and wins over "*". The
installer writes the resolved text, and --check compares against the resolved text, so a
resolved value is not drift. Files that are not UTF-8 text are copied unchanged.

Usage: install-managed-skills.py [NAME ...] [--profile NAME] [--dest PATH] [--values PATH] [--check]
No NAME installs every skill folder that has a SKILL.md. --check writes nothing and exits 1
when any profile file is missing or differs.
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

REPO_SKILLS = Path(__file__).resolve().parent.parent / "managed-skills"


def load_values(path: Path) -> dict[str, dict[str, str]]:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def resolved(file: Path, values: dict[str, str]) -> bytes:
    data = file.read_bytes()
    if not values:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    for placeholder, value in values.items():
        text = text.replace(placeholder, value)
    return text.encode("utf-8")


def same(data: bytes, target: Path) -> bool:
    return target.is_file() and data.replace(b"\r\n", b"\n") == target.read_bytes().replace(b"\r\n", b"\n")


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
    ap.add_argument("--values")
    ap.add_argument("--backup-dir", default=str(Path.home() / ".veyyon" / "tmp" / "managed-skills-backup"))
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    src = Path(args.src)
    agent = Path.home() / ".veyyon" / "profiles" / args.profile / "agent"
    dest = Path(args.dest) if args.dest else agent / "managed-skills"
    values = load_values(Path(args.values) if args.values else agent / "managed-skills.local.json")
    backup = Path(args.backup_dir) / time.strftime("%Y%m%dT%H%M%S")
    drift, backed_up = [], []
    for name in skill_names(src, args.names):
        skill_values = {**values.get("*", {}), **values.get(name, {})}
        for file in sorted(p for p in (src / name).rglob("*") if p.is_file()):
            rel = file.relative_to(src)
            target = dest / rel
            data = resolved(file, skill_values)
            if same(data, target):
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
            target.write_bytes(data)
    print(("differs: " if args.check else "installed: ") + (", ".join(drift) or "none"))
    if backed_up:
        print("backed up local versions: " + ", ".join(backed_up))
    return 1 if (args.check and drift) else 0


if __name__ == "__main__":
    sys.exit(main())
