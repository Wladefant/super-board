#!/usr/bin/env python3
"""Merge-prohibition scanner and the human Review handoff.

**The runtime never merges.** It creates branches, pushes commits, opens and
updates pull requests, runs QA and local review, publishes sanitized evidence,
and moves a successful card to Review — and then it stops. A human rebase-merges.

That rule only holds if it is *enforced*, so `scan_merge_prohibitions` is a
release gate rather than a convention. It source-scans every executable runtime,
workflow, skill, and reviewer path for all eight ways a merge can happen, and
ANY active occurrence fails the gate. The eight are not arbitrary: each one is a
distinct path that has, at some point, merged something nobody approved.

  1. cli-merge-subcommand      `gh pr merge`
  2. rest-merge-endpoint       `/pulls/<n>/merge`, `/merges`
  3. graphql-merge-mutation    `mergePullRequest`, `enablePullRequestAutoMerge`
  4. mcp-merge-tool            `merge_pull_request`
  5. auto-merge-enablement     `auto-merge` / `auto_merge`
  6. squash-or-merge-commit    `merge_method: squash|merge`, `--squash`
  7. runtime-issue-closure     closing the issue INSTEAD of merging it
  8. runtime-done-transition   writing the literal `Done` status

Mechanisms 7 and 8 are scoped to dispatcher, reviewer, QA, and workflow paths,
because closing an issue and writing `Done` are legitimate elsewhere — for the
closure normalizer, which is the only actor allowed to produce `Done`, and only
after a confirmed external merge.

Exclusions come from an explicit allowlist FILE at the repository root, listing
paths one per line. Never a path heuristic: "skip anything under docs/" is
exactly how a real merge path hides in a file named `docs/deploy-helper.sh`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

try:  # normal package import
    from . import EXIT_CONFIG
    from .config import NormalizedConfig
except ImportError:  # executed as a plain file path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFIG
    from super_board_runtime.config import NormalizedConfig

#: The explicit exclusion list. A FILE, deliberately — see the module docstring.
ALLOWLIST_FILENAME = "merge-scan-allowlist.txt"

#: The only actor permitted to write the `Done` status, and only after a
#: confirmed external merge or closure.
DONE_WRITER = "closure-normalizer"

#: Configuration the runtime requires before it will run at all.
REQUIRED_MERGE_CONFIG: Mapping[str, Any] = {
    "human_approves_merge": True,
    "merge_method": "rebase",
}

#: Repository settings the board contract requires. Squash destroys the TDD
#: breadcrumb trail; a merge commit hides it. Rebase keeps every commit.
REQUIRED_REPOSITORY_SETTINGS: Mapping[str, bool] = {
    "allow_merge_commit": False,
    "allow_rebase_merge": True,
    "allow_squash_merge": False,
}

_MECHANISM_PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("cli-merge-subcommand", re.compile(r"gh\s+pr\s+merge\b"), False),
    ("rest-merge-endpoint", re.compile(r"/pulls/[^/\s]+/merge|/merges\b"), False),
    (
        "graphql-merge-mutation",
        re.compile(r"mergePullRequest|enablePullRequestAutoMerge"),
        False,
    ),
    ("mcp-merge-tool", re.compile(r"merge_pull_request"), False),
    ("auto-merge-enablement", re.compile(r"auto[-_]merge"), False),
    (
        "squash-or-merge-commit",
        re.compile(r"""merge_method\s*[:=]\s*["']?(squash|merge)|--squash\b"""),
        False,
    ),
    # Scoped to dispatcher / reviewer paths — see the module docstring.
    ("runtime-issue-closure", re.compile(r"""state_reason|state\s*[:=]\s*["']closed"""), True),
    ("runtime-done-transition", re.compile(r"""[:=]\s*["']Done["']|\bstatus\s+Done\b"""), True),
)

#: The eight mechanisms, in scan order.
MERGE_MECHANISMS: tuple[str, ...] = tuple(name for name, _p, _s in _MECHANISM_PATTERNS)

#: Files whose path marks them as a dispatcher, reviewer, QA, or workflow path.
_SCOPED_PATH_RE = re.compile(
    r"(^|/)(super-board-run|super-board-wave|super-review|super-qa|super-build|review|qa|workflows?)"
    r"[^/]*$|(^|/)(workflows|skills/super-review|skills/super-qa|skills/super-build)/",
    re.IGNORECASE,
)

#: Only source that can actually run — or that instructs a model to run — is
#: scanned. A skill Markdown file IS an executable path: the model obeys it.
_SCANNED_SUFFIXES: frozenset[str] = frozenset(
    {".sh", ".bash", ".py", ".js", ".mjs", ".cjs", ".ts", ".yml", ".yaml", ".md", ".ps1"}
)

_ALWAYS_SKIPPED_DIRS: frozenset[str] = frozenset({".git", "node_modules", "__pycache__", ".venv"})

_RETIRED_STATUS_RE = re.compile(r"\bSkipped\b")


class MergeContractError(ValueError):
    """The human-merge contract is not satisfied. Maps to exit code 65."""

    exit_code = EXIT_CONFIG

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class MergeOccurrence:
    path: str
    line: int
    mechanism: str

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


@dataclass(frozen=True)
class MergeScanReport:
    occurrences: tuple[MergeOccurrence, ...]
    clean: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "occurrences": [o.to_dict() for o in self.occurrences],
        }


# ───────────────────────────── allowlist ─────────────────────────────


def load_allowlist(root: Path) -> tuple[str, ...]:
    """Read the explicit exclusion list. Missing file means exclude nothing."""
    path = Path(root) / ALLOWLIST_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    entries: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return tuple(entries)


def _allowlisted(relative: str, allowlist: Sequence[str]) -> bool:
    for entry in allowlist:
        if entry.endswith("/"):
            if relative == entry.rstrip("/") or relative.startswith(entry):
                return True
        elif relative == entry:
            return True
    return False


def _scannable_files(root: Path) -> Iterable[Path]:
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        if any(part in _ALWAYS_SKIPPED_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in _SCANNED_SUFFIXES:
            yield path


# ───────────────────────────── the scan ─────────────────────────────


def scan_merge_prohibitions(
    root: Path, *, allowlist: Optional[Sequence[str]] = None
) -> MergeScanReport:
    """Source-scan a tree for every merge mechanism. Any hit fails the gate."""
    root = Path(root)
    entries = load_allowlist(root) if allowlist is None else tuple(allowlist)
    occurrences: list[MergeOccurrence] = []

    for path in _scannable_files(root):
        relative = path.relative_to(root).as_posix()
        if relative == ALLOWLIST_FILENAME or _allowlisted(relative, entries):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        scoped = bool(_SCOPED_PATH_RE.search(relative))
        for number, text in enumerate(lines, start=1):
            for mechanism, pattern, scope_limited in _MECHANISM_PATTERNS:
                if scope_limited and not scoped:
                    continue
                if pattern.search(text):
                    occurrences.append(MergeOccurrence(relative, number, mechanism))

    return MergeScanReport(tuple(occurrences), not occurrences)


def scan_retired_status(
    root: Path, *, allowlist: Optional[Sequence[str]] = None
) -> MergeScanReport:
    """`Skipped` is not a lifecycle status; it must be absent from active surfaces."""
    root = Path(root)
    entries = load_allowlist(root) if allowlist is None else tuple(allowlist)
    occurrences: list[MergeOccurrence] = []
    for path in _scannable_files(root):
        relative = path.relative_to(root).as_posix()
        if relative == ALLOWLIST_FILENAME or _allowlisted(relative, entries):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, text in enumerate(lines, start=1):
            if _RETIRED_STATUS_RE.search(text):
                occurrences.append(MergeOccurrence(relative, number, "retired-status-skipped"))
    return MergeScanReport(tuple(occurrences), not occurrences)


# ───────────────────────────── contracts ─────────────────────────────


def verify_human_merge_config(config: NormalizedConfig) -> None:
    """Refuse to run unless a human approves every merge, by rebase."""
    if config.human_approves_merge is not True:
        raise MergeContractError(
            "human-approves-merge-required",
            "human_approves_merge must be true: the runtime has no merge path and no "
            "substitute for one",
        )
    if config.merge_method != "rebase":
        raise MergeContractError(
            "merge-method-must-be-rebase",
            "merge_method must be 'rebase': squash collapses the TDD breadcrumb trail",
        )


def verify_repository_settings(settings: Mapping[str, Any]) -> None:
    """Pin the repository's merge buttons. An absent setting fails closed."""
    for key, required in REQUIRED_REPOSITORY_SETTINGS.items():
        if settings.get(key) is not required:
            raise MergeContractError(
                f"repository-setting-invalid:{key}",
                f"repository setting {key} must be {required!r}; an unreadable or "
                f"disagreeing value is refused rather than assumed",
            )


# ───────────────────────────── the handoff ─────────────────────────────


@dataclass(frozen=True)
class ReviewHandoff:
    issue_url: str
    pull_request_url: str
    tested_sha: str
    next_status: str
    merged: bool
    merge_ready: bool
    merge_method: str
    awaiting: str
    reason_code: Optional[str]
    completed_by: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


def review_handoff(
    *,
    issue_url: str,
    pull_request_url: str,
    tested_sha: str,
    merge_ready: bool,
    reason_code: Optional[str] = None,
) -> ReviewHandoff:
    """What a successful review produces: a record, not a merge.

    `next_status` is `Review` on every path. There is no branch of this function
    that returns `Done`, because producing `Done` is not the runtime's to do.
    """
    return ReviewHandoff(
        issue_url=issue_url,
        pull_request_url=pull_request_url,
        tested_sha=tested_sha,
        next_status="Review",
        merged=False,
        merge_ready=bool(merge_ready),
        merge_method="rebase",
        awaiting="human-rebase-merge",
        reason_code=reason_code,
        completed_by=None,
    )


def may_write_done(actor: str, *, merged_externally: bool) -> bool:
    """`Done` is produced by the closure normalizer, after a real merge. Only."""
    return actor == DONE_WRITER and bool(merged_externally)


__all__ = [
    "ALLOWLIST_FILENAME",
    "DONE_WRITER",
    "MERGE_MECHANISMS",
    "REQUIRED_MERGE_CONFIG",
    "REQUIRED_REPOSITORY_SETTINGS",
    "MergeContractError",
    "MergeOccurrence",
    "MergeScanReport",
    "ReviewHandoff",
    "load_allowlist",
    "may_write_done",
    "review_handoff",
    "scan_merge_prohibitions",
    "scan_retired_status",
    "verify_human_merge_config",
    "verify_repository_settings",
]
