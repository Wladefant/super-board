#!/usr/bin/env python3
"""Versioned install manifest, tree snapshot, and drift comparison.

An installer that "mostly works" is worse than one that fails outright. Half a
payload produces a board running a new skill against an old dispatcher, and the
symptom surfaces three steps later as a policy that should have been impossible
to reach. So installation here is **manifest-driven and fails closed**:

  * the payload is enumerated up front, and a missing source asset is
    `install-payload-incomplete` before a single byte is copied;
  * the source tree's HEAD must be exactly the pinned `--source-sha`;
  * every installed file is checksummed into `.claude/super-board/install-manifest.json`
    together with the release version, the source repository and SHA, the
    configuration schema version, the install timestamp, and the design-skill
    source, SHA, and checksum as three separate fields;
  * stale removal only ever touches paths the **prior manifest owned**, so an
    operator's own files in `.claude/` survive an upgrade;
  * a downgrade is refused unless `allow_downgrade` is passed explicitly.

**The idempotency proof.** Install, `snapshot_install_tree`, install the same
release again, `snapshot_install_tree` again, `compare_install_snapshots`. Zero
added, removed, changed, or ownership-shifted entries. This is deliberately not
a diff of the working tree against the pre-install checkout: that diff is
dominated by the first install's own output, so it can read clean while the
second install rewrites half the payload — and it cannot represent an ownership
shift at all.

The manifest is excluded from the snapshot on purpose. It carries the install
timestamp, so it legitimately differs between two installs; it is the ledger of
what was installed, not one of the installed files.

Two paths are retired and must never be produced again: `.claude/super-board/config.json`
(configs are per-slug under `configs/`) and `.claude/super-board/scripts/` (every
entry point lives in `.claude/bin/`).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

try:  # normal package import
    from . import EXIT_CONFIG
except ImportError:  # executed as a plain file path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFIG

#: Where the release came from. Recorded in every manifest.
SOURCE_REPOSITORY = "https://github.com/Wladefant/super-board"

#: The configuration contract this release speaks — see `config.py`.
CONFIG_SCHEMA_VERSION = 1

#: The ledger. Excluded from its own snapshot; see the module docstring.
MANIFEST_RELATIVE_PATH = ".claude/super-board/install-manifest.json"

#: The four pipeline skills.
INSTALLED_SKILLS: tuple[str, ...] = (
    "super-board",
    "super-build",
    "super-qa",
    "super-review",
)

#: Every executable entry point, all in ONE directory. The historical names are
#: preserved so an existing runbook keeps working.
INSTALLED_BIN_SCRIPTS: tuple[str, ...] = (
    "super-board-run.sh",
    "super-board-stop.sh",
    "super-board-gh-guard.sh",
    "super-board-status.py",
    "super-board-wave-plan.sh",
    "super-board-sweep-comments.mjs",
    "super-board-python.sh",
    "super-board-config.py",
    "super-board-auth.py",
    "super-board-project.py",
    "super-board-publish.py",
    "super-board-normalize.py",
    "super-board-codex-review.py",
    "super-board-install-verify.py",
    "super-qa-dispatch.sh",
    "super-qa-file-bug.sh",
)

#: The shared runtime package, installed beside the entry points.
RUNTIME_PACKAGE = "super_board_runtime"

INSTALLED_WORKFLOWS: tuple[str, ...] = ("super-board-wave.js",)

#: Repository-side payload, relative to `.github/`.
INSTALLED_GITHUB_PAYLOAD: tuple[str, ...] = (
    "ISSUE_TEMPLATE/superboard-issue.yml",
    "ISSUE_TEMPLATE/config.yml",
    "workflows/auto-add-to-project.yml",
    "workflows/super-board-normalize.yml",
)

#: Directories the snapshot walks. Anything found here that no manifest owns is
#: recorded as `unowned` — visible in a drift report, never deleted.
INSTALL_ROOTS: tuple[str, ...] = (
    ".claude/skills",
    ".claude/bin",
    ".claude/workflows",
    ".claude/super-board",
    ".github/ISSUE_TEMPLATE",
    ".github/workflows",
)

#: Layout mistakes from earlier releases. Producing either one is a bug.
FORBIDDEN_INSTALL_PATHS: tuple[str, ...] = (
    ".claude/super-board/config.json",
    ".claude/super-board/scripts/",
)

_SKIPPED_DIRECTORIES = frozenset({"__pycache__", ".git", "node_modules", ".venv"})
_SKIPPED_SUFFIXES = frozenset({".pyc", ".pyo"})

_CONFIG_TEMPLATE: Mapping[str, Any] = {
    "activation_mode": "off",
    "base_branch": "staging",
    "exclude_labels": ["design", "history"],
    "human_approves_merge": True,
    "max_workers": 2,
    "project": {"number": 0, "owner": "REPLACE-WITH-PROJECT-OWNER"},
    "repo": {"remote": "REPLACE-WITH/REPOSITORY"},
    "version": CONFIG_SCHEMA_VERSION,
}


class InstallError(ValueError):
    """The installation contract was not satisfied. Maps to exit code 65."""

    exit_code = EXIT_CONFIG

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ───────────────────────────── records ─────────────────────────────


@dataclass(frozen=True)
class PayloadItem:
    source: str
    target: str
    owner: str
    executable: bool

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


@dataclass(frozen=True)
class InstalledFile:
    path: str
    sha256: str
    owner: str
    executable: bool

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


@dataclass(frozen=True)
class InstallManifest:
    release_version: str
    source_sha: str
    source_repository: str
    config_schema_version: int
    installed_at: str
    design_skill_source: Optional[str]
    design_skill_sha: Optional[str]
    design_skill_checksum: Optional[str]
    user_home: Optional[str]
    files: tuple[InstalledFile, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_schema_version": self.config_schema_version,
            "design_skill_checksum": self.design_skill_checksum,
            "design_skill_sha": self.design_skill_sha,
            "design_skill_source": self.design_skill_source,
            "files": [entry.to_dict() for entry in self.files],
            "installed_at": self.installed_at,
            "release_version": self.release_version,
            "source_repository": self.source_repository,
            "source_sha": self.source_sha,
            "user_home": self.user_home,
        }

    @classmethod
    def from_dict(cls, body: Mapping[str, Any]) -> "InstallManifest":
        if not isinstance(body, Mapping):
            raise InstallError("install-manifest-unreadable", "the manifest must be an object")
        raw_files = body.get("files")
        if not isinstance(raw_files, list):
            raise InstallError(
                "install-manifest-unreadable", "the manifest must carry a `files` array"
            )
        files = tuple(
            InstalledFile(
                path=str(entry.get("path")),
                sha256=str(entry.get("sha256")),
                owner=str(entry.get("owner")),
                executable=bool(entry.get("executable")),
            )
            for entry in raw_files
            if isinstance(entry, Mapping)
        )
        return cls(
            release_version=str(body.get("release_version") or ""),
            source_sha=str(body.get("source_sha") or ""),
            source_repository=str(body.get("source_repository") or SOURCE_REPOSITORY),
            config_schema_version=int(body.get("config_schema_version") or CONFIG_SCHEMA_VERSION),
            installed_at=str(body.get("installed_at") or ""),
            design_skill_source=body.get("design_skill_source"),
            design_skill_sha=body.get("design_skill_sha"),
            design_skill_checksum=body.get("design_skill_checksum"),
            user_home=body.get("user_home"),
            files=files,
        )


@dataclass(frozen=True)
class ManifestVerification:
    ok: bool
    changed: tuple[str, ...]
    missing: tuple[str, ...]
    extra: tuple[str, ...]
    reason_code: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed": list(self.changed),
            "extra": list(self.extra),
            "missing": list(self.missing),
            "ok": self.ok,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class InstallTreeSnapshot:
    entries: Mapping[str, Mapping[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": {
                path: dict(record) for path, record in sorted(self.entries.items())
            }
        }


@dataclass(frozen=True)
class InstallDriftReport:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]
    ownership_shifted: tuple[str, ...]
    clean: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "added": list(self.added),
            "changed": list(self.changed),
            "clean": self.clean,
            "ownership_shifted": list(self.ownership_shifted),
            "removed": list(self.removed),
        }


# ───────────────────────────── helpers ─────────────────────────────


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIPPED_DIRECTORIES for part in path.parts):
            continue
        if path.suffix.lower() in _SKIPPED_SUFFIXES:
            continue
        yield path


def _require(path: Path, what: str) -> Path:
    if not path.exists():
        raise InstallError(
            "install-payload-incomplete",
            f"the source tree is missing {what} ({path}); refusing to install a partial payload",
        )
    return path


def plan_install_payload(source_root: Path) -> tuple[PayloadItem, ...]:
    """Enumerate every file the installer copies. Missing asset → fail closed."""
    root = Path(source_root)
    items: list[PayloadItem] = []

    for skill in INSTALLED_SKILLS:
        directory = _require(root / "skills" / skill, f"the {skill} skill")
        files = list(_walk(directory))
        if not files:
            raise InstallError(
                "install-payload-incomplete", f"the {skill} skill directory is empty"
            )
        for path in files:
            relative = path.relative_to(directory).as_posix()
            items.append(
                PayloadItem(
                    source=path.relative_to(root).as_posix(),
                    target=f".claude/skills/{skill}/{relative}",
                    owner=f"skill:{skill}",
                    executable=False,
                )
            )

    for script in INSTALLED_BIN_SCRIPTS:
        path = _require(root / "scripts" / script, f"the {script} entry point")
        items.append(
            PayloadItem(
                source=path.relative_to(root).as_posix(),
                target=f".claude/bin/{script}",
                owner="bin",
                executable=True,
            )
        )

    package = _require(root / "scripts" / RUNTIME_PACKAGE, "the shared runtime package")
    _require(package / "__init__.py", f"{RUNTIME_PACKAGE}/__init__.py")
    for path in _walk(package):
        if path.suffix != ".py":
            continue
        relative = path.relative_to(package).as_posix()
        items.append(
            PayloadItem(
                source=path.relative_to(root).as_posix(),
                target=f".claude/bin/{RUNTIME_PACKAGE}/{relative}",
                owner="runtime",
                executable=False,
            )
        )

    for workflow in INSTALLED_WORKFLOWS:
        path = _require(root / "workflows" / workflow, f"the {workflow} workflow")
        items.append(
            PayloadItem(
                source=path.relative_to(root).as_posix(),
                target=f".claude/workflows/{workflow}",
                owner="workflow",
                executable=False,
            )
        )

    for relative in INSTALLED_GITHUB_PAYLOAD:
        path = _require(root / "payload" / "github" / relative, f"the {relative} payload")
        items.append(
            PayloadItem(
                source=path.relative_to(root).as_posix(),
                target=f".github/{relative}",
                owner="github",
                executable=False,
            )
        )

    for item in items:
        for forbidden in FORBIDDEN_INSTALL_PATHS:
            if item.target.startswith(forbidden.rstrip("/")):
                raise InstallError(
                    "install-layout-invalid",
                    f"{item.target} is a retired install path and must never be produced",
                )
    return tuple(items)


def build_install_manifest(
    source_sha: str,
    release_version: str,
    installed: Sequence[InstalledFile],
    *,
    design_skill_source: Optional[str] = None,
    design_skill_sha: Optional[str] = None,
    design_skill_checksum: Optional[str] = None,
    user_home: Optional[str] = None,
    installed_at: Optional[str] = None,
) -> InstallManifest:
    """Assemble the ledger for one installation."""
    if not source_sha or not release_version:
        raise InstallError(
            "install-manifest-invalid",
            "a manifest needs both a pinned source SHA and a release version",
        )
    stamp = installed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return InstallManifest(
        release_version=release_version,
        source_sha=source_sha,
        source_repository=SOURCE_REPOSITORY,
        config_schema_version=CONFIG_SCHEMA_VERSION,
        installed_at=stamp,
        design_skill_source=design_skill_source,
        design_skill_sha=design_skill_sha,
        design_skill_checksum=design_skill_checksum,
        user_home=user_home,
        files=tuple(sorted(installed, key=lambda entry: entry.path)),
    )


def _git_head(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout.strip()


def verify_source_sha(
    source_root: Path,
    source_sha: str,
    *,
    head_reader: Optional[Callable[[Path], str]] = None,
) -> str:
    """Refuse to install from a tree that is not the pinned commit."""
    reader = head_reader or _git_head
    try:
        head = reader(Path(source_root))
    except Exception as exc:  # unreadable HEAD is not "probably fine"
        raise InstallError(
            "install-source-sha-unreadable",
            f"the source tree's HEAD could not be read: {exc}",
        ) from exc
    if not source_sha or head != source_sha:
        raise InstallError(
            "install-source-sha-mismatch",
            "the source tree is not at the pinned commit; refusing to install a release "
            "whose provenance cannot be stated",
        )
    return head


def _version_parts(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(value or "").strip().lstrip("vV").split("."):
        digits = "".join(character for character in chunk if character.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_downgrade(previous: str, new: str) -> bool:
    """True when `new` is an older release than `previous`."""
    return _version_parts(new) < _version_parts(previous)


def read_manifest(path: Path) -> InstallManifest:
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(
            "install-manifest-unreadable", f"the install manifest could not be read: {exc}"
        ) from exc
    return InstallManifest.from_dict(body)


def stale_files(previous: InstallManifest, current: InstallManifest) -> tuple[str, ...]:
    """Paths the PRIOR manifest owned and this one does not. Nothing else."""
    now = {entry.path for entry in current.files}
    return tuple(sorted(entry.path for entry in previous.files if entry.path not in now))


# ───────────────────────────── install ─────────────────────────────


def _write_if_absent(path: Path, text: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def install_payload(
    source_root: Path,
    repo_root: Path,
    *,
    source_sha: str,
    release_version: str,
    design_skill_source: str,
    design_skill_sha: str,
    design_skill_checksum: str,
    user_home: str,
    slug: Optional[str] = None,
    allow_downgrade: bool = False,
    installed_at: Optional[str] = None,
) -> InstallManifest:
    """Copy the complete payload, prune what this installer previously owned, write the ledger."""
    source_root = Path(source_root)
    repo_root = Path(repo_root)
    items = plan_install_payload(source_root)

    manifest_path = repo_root / MANIFEST_RELATIVE_PATH
    previous: Optional[InstallManifest] = None
    if manifest_path.exists():
        previous = read_manifest(manifest_path)
        if is_downgrade(previous.release_version, release_version) and not allow_downgrade:
            raise InstallError(
                "install-downgrade-refused",
                f"{release_version} is older than the installed {previous.release_version}; "
                "pass the documented allow_downgrade override to install it anyway",
            )

    slug = (slug or repo_root.name or "superboard").strip()
    installed: list[InstalledFile] = []

    for item in items:
        destination = repo_root / item.target
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / item.source, destination)
        try:
            os.chmod(destination, 0o755 if item.executable else 0o644)
        except OSError:  # filesystems without POSIX modes (Windows) — declarative only
            pass
        installed.append(
            InstalledFile(
                path=item.target,
                sha256=sha256_file(destination),
                owner=item.owner,
                executable=item.executable,
            )
        )

    # Per-slug configuration and the per-machine active pointer. Written once
    # and never overwritten: after the first install these belong to the
    # operator, and clobbering a live configuration on upgrade is how a board
    # silently loses its activation state.
    config_target = f".claude/super-board/configs/{slug}.json"
    _write_if_absent(
        repo_root / config_target,
        json.dumps(_CONFIG_TEMPLATE, indent=2, sort_keys=True) + "\n",
    )
    _write_if_absent(repo_root / ".claude/super-board/active", f"{slug}\n")
    for target, owner in ((config_target, "config"), (".claude/super-board/active", "state")):
        installed.append(
            InstalledFile(
                path=target,
                sha256=sha256_file(repo_root / target),
                owner=owner,
                executable=False,
            )
        )

    manifest = build_install_manifest(
        source_sha,
        release_version,
        installed,
        design_skill_source=design_skill_source,
        design_skill_sha=design_skill_sha,
        design_skill_checksum=design_skill_checksum,
        user_home=user_home,
        installed_at=installed_at,
    )

    if previous is not None:
        for relative in stale_files(previous, manifest):
            candidate = repo_root / relative
            if candidate.is_file():
                candidate.unlink()

    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


# ───────────────────────────── verify ─────────────────────────────


def _is_extra(path: str) -> bool:
    for forbidden in FORBIDDEN_INSTALL_PATHS:
        if path.startswith(forbidden.rstrip("/")):
            return True
    return not path.startswith((".claude/", ".github/"))


def verify_install_manifest(manifest_path: Path, repo_root: Path) -> ManifestVerification:
    """Compare the ledger against the tree. Any disagreement fails."""
    manifest = read_manifest(manifest_path)
    repo_root = Path(repo_root)
    changed: list[str] = []
    missing: list[str] = []
    extra: list[str] = []

    for entry in manifest.files:
        if _is_extra(entry.path):
            extra.append(entry.path)
            continue
        path = repo_root / entry.path
        if not path.is_file():
            missing.append(entry.path)
            continue
        if sha256_file(path) != entry.sha256:
            changed.append(entry.path)

    ok = not (changed or missing or extra)
    return ManifestVerification(
        ok=ok,
        changed=tuple(sorted(changed)),
        missing=tuple(sorted(missing)),
        extra=tuple(sorted(extra)),
        reason_code=None if ok else "install-verification-failed",
    )


# ───────────────────────────── snapshot and drift ─────────────────────────────


def snapshot_install_tree(repo_root: Path, manifest: InstallManifest) -> InstallTreeSnapshot:
    """Path → (checksum, owner) for the installed tree, deterministically ordered.

    Files the manifest owns carry their owner. Anything else found under the
    install roots is recorded as `unowned` — so a stray file is visible as drift
    instead of invisible. The manifest itself is excluded; it carries the
    install timestamp and is the ledger, not an installed file.
    """
    repo_root = Path(repo_root)
    owners = {entry.path: entry.owner for entry in manifest.files}
    entries: dict[str, dict[str, str]] = {}

    for path, owner in owners.items():
        if path == MANIFEST_RELATIVE_PATH:
            continue
        absolute = repo_root / path
        if not absolute.is_file():
            continue
        entries[path] = {"owner": owner, "sha256": sha256_file(absolute)}

    for root in INSTALL_ROOTS:
        directory = repo_root / root
        if not directory.is_dir():
            continue
        for absolute in _walk(directory):
            relative = absolute.relative_to(repo_root).as_posix()
            if relative == MANIFEST_RELATIVE_PATH or relative in entries:
                continue
            entries[relative] = {"owner": "unowned", "sha256": sha256_file(absolute)}

    return InstallTreeSnapshot(entries=dict(sorted(entries.items())))


def compare_install_snapshots(
    first: InstallTreeSnapshot, second: InstallTreeSnapshot
) -> InstallDriftReport:
    """The accepted idempotency proof: snapshot 2 against snapshot 1, nothing else."""
    left = dict(first.entries)
    right = dict(second.entries)
    added = tuple(sorted(set(right) - set(left)))
    removed = tuple(sorted(set(left) - set(right)))
    common = sorted(set(left) & set(right))
    changed = tuple(path for path in common if left[path]["sha256"] != right[path]["sha256"])
    shifted = tuple(path for path in common if left[path]["owner"] != right[path]["owner"])
    return InstallDriftReport(
        added=added,
        removed=removed,
        changed=changed,
        ownership_shifted=shifted,
        clean=not (added or removed or changed or shifted),
    )


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "FORBIDDEN_INSTALL_PATHS",
    "INSTALLED_BIN_SCRIPTS",
    "INSTALLED_GITHUB_PAYLOAD",
    "INSTALLED_SKILLS",
    "INSTALLED_WORKFLOWS",
    "INSTALL_ROOTS",
    "MANIFEST_RELATIVE_PATH",
    "RUNTIME_PACKAGE",
    "SOURCE_REPOSITORY",
    "InstallDriftReport",
    "InstallError",
    "InstallManifest",
    "InstallTreeSnapshot",
    "InstalledFile",
    "ManifestVerification",
    "PayloadItem",
    "build_install_manifest",
    "compare_install_snapshots",
    "install_payload",
    "is_downgrade",
    "plan_install_payload",
    "read_manifest",
    "sha256_file",
    "snapshot_install_tree",
    "stale_files",
    "verify_install_manifest",
    "verify_source_sha",
]
