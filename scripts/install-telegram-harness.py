#!/usr/bin/env python3
"""install-telegram-harness.py — idempotent installer for the Telegram agent harness.

Synchronizes TypeScript harness files from packages/telegram-agent-harness to ~/.veyyon/telegram,
preserving runtime state, creating pre-overwrite backups, and recording an install manifest.

Usage:
    install-telegram-harness.py [--source-root PATH] [--target PATH]
                                [--check] [--dry-run] [--allow-dirty]

Exit codes:
    0: Success (or --check passed cleanly)
    1: --check detected drift (missing or modified files)
    64: Invalid command-line usage
    65: Refused install (e.g. dirty working tree without --allow-dirty, missing source)
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_USAGE = 64
EXIT_CONFIG = 65

# Protected runtime files and patterns that MUST NEVER be deleted or overwritten
PROTECTED_PATTERNS = (
    "manifest.json",
    "bot_pool.db*",
    "*.ps1",
    "veyyon_telegram_bridge.py",
    "veyyon_telegram_guard.js",
    "telegram_notify_state.json",
    ".backups",
    ".backups/*",
    "__pycache__",
    "__pycache__/*",
    ".pytest_cache",
    ".pytest_cache/*",
)


class SyncItem(NamedTuple):
    source_path: Path
    rel_target: str  # POSIX relative path from target root, e.g. "guard.ts", "harness/index.ts"


def is_protected_rel_path(rel_path: str) -> bool:
    """Check whether a relative path within target matches any protected pattern."""
    normalized = rel_path.replace("\\", "/")
    parts = normalized.split("/")

    for pattern in PROTECTED_PATTERNS:
        # Check against the full relative path
        if fnmatch.fnmatch(normalized, pattern):
            return True
        # Check against base filename
        if fnmatch.fnmatch(parts[-1], pattern):
            return True
        # Check against top-level directory name
        if parts and fnmatch.fnmatch(parts[0], pattern.rstrip("/*")):
            return True
    return False


def is_protected_target_file(target_file: Path, target_root: Path) -> bool:
    """Check whether target_file is protected or inside a protected folder."""
    try:
        rel = target_file.relative_to(target_root).as_posix()
    except ValueError:
        rel = target_file.name
    return is_protected_rel_path(rel)


def sha256_file(path: Path) -> str:
    """Calculate the sha256 hex digest of a file."""
    hasher = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def find_harness_root(source_root: Path) -> Path:
    """Find the root directory of the telegram-agent-harness package."""
    candidates = [
        source_root / "packages" / "telegram-agent-harness",
        source_root,
    ]
    for c in candidates:
        if (c / "extension").is_dir() and (c / "src").is_dir():
            return c
    raise FileNotFoundError(
        f"Could not locate telegram-agent-harness package under {source_root}. "
        "Expected packages/telegram-agent-harness/ with 'extension' and 'src' subdirectories."
    )


def plan_sync_items(harness_root: Path, target: Path) -> List[SyncItem]:
    """Plan all files to be synchronized from harness_root to target.

    Mapping rules:
    - packages/telegram-agent-harness/extension/*.ts (excluding tests/) -> target/
    - packages/telegram-agent-harness/src/*.ts -> target/harness/
    - extension/tests/*.ts -> target/tests/ (ONLY if target/tests already exists)
    - tests/*.ts -> target/harness/tests/ (ONLY if target/harness/tests already exists)
    """
    items: List[SyncItem] = []

    # 1. extension/*.ts (top-level only)
    ext_dir = harness_root / "extension"
    if ext_dir.is_dir():
        for p in sorted(ext_dir.glob("*.ts")):
            if p.is_file():
                items.append(SyncItem(source_path=p, rel_target=p.name))

    # 2. src/*.ts -> target/harness/
    src_dir = harness_root / "src"
    if src_dir.is_dir():
        for p in sorted(src_dir.glob("*.ts")):
            if p.is_file():
                items.append(SyncItem(source_path=p, rel_target=f"harness/{p.name}"))

    # 3. extension/tests -> target/tests if target/tests exists
    target_tests_dir = target / "tests"
    ext_tests_dir = harness_root / "extension" / "tests"
    if target_tests_dir.is_dir() and ext_tests_dir.is_dir():
        for p in sorted(ext_tests_dir.glob("*.ts")):
            if p.is_file():
                items.append(SyncItem(source_path=p, rel_target=f"tests/{p.name}"))

    # 4. tests/ -> target/harness/tests if target/harness/tests exists
    target_harness_tests_dir = target / "harness" / "tests"
    tests_dir = harness_root / "tests"
    if target_harness_tests_dir.is_dir() and tests_dir.is_dir():
        for p in sorted(tests_dir.glob("*.ts")):
            if p.is_file():
                items.append(SyncItem(source_path=p, rel_target=f"harness/tests/{p.name}"))

    # Verify no planned item violates protected patterns
    for item in items:
        if is_protected_rel_path(item.rel_target):
            raise ValueError(
                f"Planned sync item {item.rel_target} conflicts with protected runtime pattern."
            )

    return items


def is_git_dirty(source_root: Path) -> Tuple[bool, str]:
    """Check if the git repository at source_root has uncommitted changes."""
    try:
        res = subprocess.run(
            ["git", "-C", str(source_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            return False, ""
        output = res.stdout.strip()
        return bool(output), output
    except Exception:
        return False, ""


def get_git_info(source_root: Path) -> Tuple[str, str]:
    """Return (source_repo, source_sha) for the source root."""
    repo = "https://github.com/Wladefant/super-board"
    sha = "unknown"
    try:
        res = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            sha = res.stdout.strip()
    except Exception:
        pass

    try:
        res = subprocess.run(
            ["git", "-C", str(source_root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            repo = res.stdout.strip()
    except Exception:
        pass

    return repo, sha


def get_installed_sha(target: Path) -> str:
    """Read the previous source SHA from install-manifest.json if present."""
    manifest_file = target / "install-manifest.json"
    if manifest_file.is_file():
        try:
            data = json.loads(manifest_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("source_sha"):
                return str(data["source_sha"])
        except Exception:
            pass
    return "unversioned"


def create_backup(target: Path, old_sha: str) -> Optional[Path]:
    """Copy the previous tree to target/.backups/<UTC-timestamp>-<old-sha>/, excluding runtime state."""
    if not target.is_dir():
        return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = target / ".backups" / f"{stamp}-{old_sha}"

    # Find files to back up
    files_to_backup: List[Tuple[Path, str]] = []
    for root, dirs, files in os.walk(target):
        # Prune backup and cache dirs from walk
        dirs[:] = [
            d for d in dirs
            if d not in (".backups", "__pycache__", ".pytest_cache")
        ]
        root_path = Path(root)
        for f in files:
            # Skip runtime state, database files, and caches
            if fnmatch.fnmatch(f, "bot_pool.db*"):
                continue
            file_path = root_path / f
            try:
                rel = file_path.relative_to(target).as_posix()
            except ValueError:
                continue
            if is_protected_rel_path(rel):
                # Don't back up sensitive databases, caches, or backups
                if fnmatch.fnmatch(rel, "bot_pool.db*") or rel.startswith(".backups"):
                    continue
            files_to_backup.append((file_path, rel))
    if not files_to_backup:
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)
    for src_file, rel in files_to_backup:
        dest = backup_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest)

    return backup_dir


def run_check(items: List[SyncItem], target: Path) -> int:
    """Perform read-only check of target vs source. Return 0 if matching, 1 if drifted."""
    drift_list: List[str] = []

    for item in items:
        target_file = target / item.rel_target
        if not target_file.exists():
            drift_list.append(f"{item.rel_target} (missing)")
        else:
            s_hash = sha256_file(item.source_path)
            t_hash = sha256_file(target_file)
            if s_hash != t_hash:
                drift_list.append(f"{item.rel_target} (modified)")

    if drift_list:
        print(f"DRIFT: {len(drift_list)} file(s) differ from source:")
        for entry in sorted(drift_list):
            print(f"  - {entry}")
        return EXIT_DRIFT

    print(f"OK: all {len(items)} files match source.")
    return EXIT_OK


def run_install(
    items: List[SyncItem],
    target: Path,
    source_root: Path,
    dry_run: bool = False,
) -> int:
    """Execute installation or dry-run."""
    source_repo, source_sha = get_git_info(source_root)
    old_sha = get_installed_sha(target)

    # Check if any target files exist to be overwritten
    has_existing_files = any((target / item.rel_target).exists() for item in items)

    if dry_run:
        print(f"[dry-run] Target directory: {target}")
        print(f"[dry-run] Source repository: {source_repo} ({source_sha})")
        if has_existing_files:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            print(f"[dry-run] Would create backup at: {target}/.backups/{stamp}-{old_sha}/")
        for item in items:
            dest = target / item.rel_target
            action = "overwrite" if dest.exists() else "create"
            print(f"[dry-run] Would {action}: {item.rel_target}")
        print(f"[dry-run] Would write manifest: {target}/install-manifest.json")
        return EXIT_OK

    # Real install:
    # 1. Create backup before overwriting any files
    if has_existing_files and target.is_dir():
        backup_path = create_backup(target, old_sha)
        if backup_path:
            print(f"Created backup at: {backup_path}")

    # 2. Synchronize files
    installed_files_record = []
    target.mkdir(parents=True, exist_ok=True)

    for item in items:
        dest = target / item.rel_target
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item.source_path, dest)
        file_hash = sha256_file(dest)
        installed_files_record.append({
            "path": item.rel_target,
            "sha256": file_hash,
        })

    # 3. Write install-manifest.json
    installed_files_record.sort(key=lambda x: x["path"])
    manifest = {
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "source_repository": source_repo,
        "source_sha": source_sha,
        "files": installed_files_record,
    }
    manifest_path = target / "install-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote manifest: {manifest_path}")
    print(f"Installed {len(items)} harness files from {source_sha} to {target}.")
    return EXIT_OK


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(f"install-telegram-harness.py: error: {message}", file=sys.stderr)
        sys.exit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="install-telegram-harness.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    default_source = Path(__file__).resolve().parent.parent
    default_target = Path.home() / ".veyyon" / "telegram"

    parser.add_argument(
        "--source-root",
        type=Path,
        default=default_source,
        help=f"Path to super-board repository root (default: {default_source})",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=default_target,
        help=f"Target directory for telegram harness (default: {default_target})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check whether installed files match source (read-only; exit 0 if clean, 1 if drifted)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate installation without writing any files or backups",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow installation from a dirty git working tree",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    source_root = args.source_root.expanduser().resolve()
    target = args.target.expanduser().resolve()

    try:
        harness_root = find_harness_root(source_root)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    try:
        items = plan_sync_items(harness_root, target)
    except Exception as exc:
        print(f"Error planning sync items: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    if args.check:
        return run_check(items, target)

    # For install (real or dry-run), check dirty tree
    dirty, dirty_output = is_git_dirty(source_root)
    if dirty and not args.allow_dirty:
        print("Refusing to install from a dirty working tree (exit 65).", file=sys.stderr)
        print("Pass --allow-dirty to override.", file=sys.stderr)
        first_few = "\n".join(dirty_output.splitlines()[:5])
        print(f"Uncommitted changes:\n{first_few}", file=sys.stderr)
        return EXIT_CONFIG

    return run_install(items, target, source_root, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
