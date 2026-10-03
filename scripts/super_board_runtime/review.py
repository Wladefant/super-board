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

**The gate has to be runnable where the runtime runs.** It is not a repository
lint; it is the proof that an *installed* tree has no merge path. Two things
follow, and both used to be wrong:

  * *Self-exclusion is intrinsic.* The scanner is built out of the literals it
    hunts, so scanning itself always produced twelve hits. That used to be
    handled by a line in `merge-scan-allowlist.txt` — a repository-root file
    that is not part of the payload and therefore does not exist on an
    installed tree, where the gate could consequently never report clean. The
    scanner now recognises its own module by the package-relative path it was
    imported from (`super_board_runtime/review.py`) plus its own definition
    line, so the exclusion travels with the code instead of beside it. The
    scanner's own source is policed by `tests/test_human_merge_contract.py`,
    which is the right place for it: a scanner cannot audit itself.
  * *A prohibition statement is not an active mechanism.* "the runtime never
    enables auto-merge" and "enable auto-merge once CI is green" carry the same
    literal and mean opposite things. Every document that stated the rule
    therefore had to be named in the allowlist, which is how five dead
    exclusions accumulated — and a dead exclusion still excludes, so the day a
    real merge path lands in one of those files the gate stays green.
    `_is_prohibition_statement` makes the distinction directly: a match is
    prose only when it sits on a prose line (any non-fenced line of a Markdown
    document, or a comment line anywhere else) AND its own statement scope —
    the paragraph or list it belongs to, plus that list's introduction —
    carries a negation. A match inside a fenced code block is NEVER prose: a
    command cannot be excused by the paragraph above it.

`scan_retired_status` is the sibling gate, and it had the identical defect for
the identical reason: eleven allowlist entries existed only to stop it flagging
the module that refuses the retired status and every document that records the
retirement, and none of them exists on an installed tree. Its hits are values
rather than prose, so neither rule above transplants. The distinction it needs
is definition versus use:

  * *The registry is excluded intrinsically*, by the same package-relative-path
    plus own-declaration test — `super_board_runtime/lifecycle.py` carrying
    `RETIRED_STATUSES`. It is a sibling in this package, so the installer
    carries it to `.claude/bin/` beside the scanner. A file that merely occupies
    that path is still scanned. The scanner reads its pattern from that module
    rather than restating the literal, so there is one authority for what is
    retired.
  * *A status assignment is a use and nothing excuses it.* `status = "Skipped"`,
    `{"status": "Skipped"}`, `--status skipped` — the value bound to a status
    field. This is the counterpart of "a fenced code block is never prose": the
    resurrection itself cannot be talked out of. The one exception is a binding
    whose NAME says retired, because `RETIRED_STATUSES = ("Skipped",)` is a
    declaration list, which is the definition and not a use.
  * *Everything else is a mention unless the passage is about the status field
    and does not call the value retired.* The bare word is ordinary English —
    "Skipped after an operator typed", a QA cell marked Skipped, a sync report
    reading `else 'Skipped'` — and those ship inside the payload, where no
    allowlist could ever have reached them.

`merge-scan-allowlist.txt` survives for the repository-only surfaces that
neither rule reaches — the seeded fixtures, the contract tests that must name
the patterns, the release notes. It is a supplement, not the mechanism. Never a
path heuristic: "skip anything under docs/" is exactly how a real merge path
hides in a file named `docs/deploy-helper.sh`.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

try:  # normal package import
    from . import EXIT_CONFIG
    from .config import NormalizedConfig
    from .lifecycle import LIFECYCLE_STATUSES, RETIRED_STATUSES
except ImportError:  # executed as a plain file path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFIG
    from super_board_runtime.config import NormalizedConfig
    from super_board_runtime.lifecycle import LIFECYCLE_STATUSES, RETIRED_STATUSES

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
        # The value has to be the LITERAL `squash` or `merge`. `merge_method=
        # merge_method,` passes a validated, rebase-only value into a dataclass
        # and merges nothing — but `merge` is a prefix of the identifier
        # `merge_method`, so without the trailing word boundary every such
        # assignment read as a merge invocation. The optional quotes around the
        # key catch the dict/JSON form `{"merge_method": "squash"}`, which the
        # bare-identifier pattern missed entirely.
        "squash-or-merge-commit",
        re.compile(r"""["']?merge_method["']?\s*[:=]\s*["']?(squash|merge)\b|--squash\b"""),
        False,
    ),
    # Scoped to dispatcher / reviewer paths — see the module docstring.
    ("runtime-issue-closure", re.compile(r"""state_reason|state\s*[:=]\s*["']closed"""), True),
    (
        "runtime-done-transition",
        re.compile(
            r"""["']?(?:status|state|column)\w*["']?\s*\]?\s*(?:=>|=(?![=~]))\s*["'`]*Done\b"""
            r"""|(?<![!=<>])["']?(?:status|state|column)\w*["']?\s*:\s*["']?Done\b"""
            r"""|\bstatus\s+Done\b"""
            r"""|--status[=\s]+["']?Done\b"""
        ),
        True,
    ),
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

#: The retired statuses, read from the module that DECLARES them rather than
#: restated here. That declaration is the definition; every other appearance is
#: a mention or a use, which is the whole distinction below.
_RETIRED_ALTERNATION = "|".join(re.escape(status) for status in RETIRED_STATUSES)

_RETIRED_STATUS_RE = re.compile(rf"\b(?:{_RETIRED_ALTERNATION})\b")

#: The retired value BOUND to a status: `status = "Skipped"`,
#: `{"status": "Skipped"}`, `--status skipped`. This is the resurrection the
#: gate exists to catch, so — exactly as a match inside a fenced code block is
#: never prose for `scan_merge_prohibitions` — nothing around it can excuse it.
#: Case-insensitive because `canonicalize_status` folds case: `"skipped"` is
#: the same resurrection wearing a lowercase hat.
#:
#: Known gap: a write through a call whose name is not status-ish — such as
#: `move_card(id, "Skipped")` — is not caught in code without AST analysis or
#: whole-program dataflow. Prose/skill instructions ("moving a card to Blocked
#: or Skipped") are caught by `_CANONICAL_STATUS_RE` context, but an un-annotated
#: helper call in source code is not.
_STATUS_ASSIGNMENT_RE = re.compile(
    rf"""(?:status|state|column)\w*["'`]?\s*(?:=>|[:=]=?)\s*["'`]*\s*"""
    rf"""(?:{_RETIRED_ALTERNATION})\b"""
    rf"""|--status[=\s]+["'`]?(?:{_RETIRED_ALTERNATION})\b""",
    re.IGNORECASE,
)

#: Words that make a passage about the board's Status field. Without one of
#: these the bare token is ordinary English — "Skipped after an operator typed",
#: a QA cell marked Skipped, a sync report reading `else 'Skipped'` — and
#: flagging those is what no allowlist can fix on an installed tree, because
#: those files ship inside the payload.
_STATUS_CONTEXT_RE = re.compile(r"status(?:es)?\b|state(?:s)?\b|column(?:s)?\b|lifecycle", re.I)

#: A sibling of the retired value in the canonical list is status context too:
#: "moving a card to Blocked or Skipped" names no field and is still a status
#: instruction. Case-sensitive — `done`, `ready` and `review` are common words.
_CANONICAL_STATUS_RE = re.compile(rf"\b(?:{'|'.join(LIFECYCLE_STATUSES)})\b")

#: A binding whose NAME says the value is retired. `RETIRED_STATUSES = (...)` is
#: a declaration list, not a use — the distinction the negation classifier
#: cannot make, because a declaration is code and carries no prose to negate.
_RETIRED_BINDING_RE = re.compile(r"(?:RETIRED|LEGACY|DEPRECATED)[A-Za-z_]*\s*(?::[^=\n]*)?=")

#: Retirement vocabulary. `_PROHIBITION_RE` covers "is not a status" and "is
#: refused"; this covers the other half of how a retirement is written.
_RETIREMENT_RE = re.compile(
    r"retire\w*|deprecat\w*|\blegacy\b|\bremoved?\b|\bno longer\b|\bused to\b", re.I
)

#: Suffixes whose whole body is prose unless it is fenced.
_PROSE_DOCUMENT_SUFFIXES: frozenset[str] = frozenset({".md"})

#: A fenced block opener or closer in Markdown. Up to three leading spaces are
#: still a fence; four make it an indented code block, which is also not prose.
_FENCE_RE = re.compile(r"^\s{0,3}(?:`{3,}|~{3,})")

#: A comment. In a file that is not a prose document, this is the only kind of
#: line that can carry a statement rather than an instruction.
_COMMENT_RE = re.compile(r"^\s*(?:#|//|--|\*|<!--)")

_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s)")

#: Negation. A statement that carries one is asserting the rule, not performing
#: the thing it names. Deliberately generous — `no` and `not` are how the rule
#: is actually written — and deliberately unavailable inside a code fence,
#: which is what keeps it from becoming a way to excuse a real command.
_PROHIBITION_RE = re.compile(
    r"\bn[o']t\b|n't\b|"
    r"\b(?:never|no|nor|neither|none|nothing|without|cannot|zero|"
    r"forbid\w*|prohibit\w*|disallow\w*|refus\w*|reject\w*|disabled?|instead\s+of|abort\w*)\b",
    re.IGNORECASE,
)

#: How far back a statement scope may reach. A paragraph or list longer than
#: this is not one statement.
_SCOPE_LINE_LIMIT = 24

#: This module, identified the way it will still be identifiable after the
#: installer copies it to `.claude/bin/super_board_runtime/review.py`: by the
#: package-relative path it was imported from, not by an absolute location.
_SELF_MODULE_PATH: tuple[str, ...] = tuple(Path(__file__).resolve().parts[-2:])

#: Occupying the scanner's path is not being the scanner. The definition line
#: has to be there too.
_SELF_DEFINITION = "def scan_merge_prohibitions("

#: The module that DECLARES the retired statuses, identified the same way and
#: for the same reason. It is the authority `scan_retired_status` reads its own
#: pattern from, it has to name every retired status in order to refuse one,
#: and it is a sibling in this package — so the installer carries it to
#: `.claude/bin/super_board_runtime/lifecycle.py` alongside the scanner and the
#: exclusion travels with the code instead of beside it.
_REGISTRY_MODULE_PATH: tuple[str, ...] = (_SELF_MODULE_PATH[0], "lifecycle.py")
_REGISTRY_DEFINITION = "RETIRED_STATUSES"


def _is_module_source(
    path: Path, text: str, *, module_path: Sequence[str], definition: str
) -> bool:
    """True when this file IS that module — wherever it has been installed."""
    return tuple(Path(path).parts[-2:]) == tuple(module_path) and definition in text


def _is_scanner_source(path: Path, text: str) -> bool:
    """True when this file IS this module — wherever it has been installed."""
    return _is_module_source(
        path, text, module_path=_SELF_MODULE_PATH, definition=_SELF_DEFINITION
    )


def _is_retired_status_registry(path: Path, text: str) -> bool:
    """True when this file IS the module that declares the retired statuses."""
    return _is_module_source(
        path, text, module_path=_REGISTRY_MODULE_PATH, definition=_REGISTRY_DEFINITION
    )


def _fenced_flags(lines: Sequence[str], *, prose_document: bool) -> tuple[bool, ...]:
    """Mark every line that is a fence delimiter or sits inside a fenced block.

    Only Markdown has fences. In every other file type the answer is "all of
    it": source is code, and only its comments are read as prose.
    """
    if not prose_document:
        return tuple(not _COMMENT_RE.match(line) for line in lines)
    flags: list[bool] = []
    inside = False
    for line in lines:
        if _FENCE_RE.match(line):
            flags.append(True)  # the delimiter itself is a boundary, not prose
            inside = not inside
            continue
        flags.append(inside)
    return tuple(flags)


def _statement_scope(lines: Sequence[str], index: int, fenced: Sequence[bool]) -> str:
    """The text a match's own statement spans.

    Its paragraph (or list block), walked back to a blank line or a fence
    boundary — plus, when the block is a list, the introduction the list hangs
    off. `Reviewer may not, on any path:` is where the negation lives for every
    bullet under it, and a bullet read on its own says the opposite.
    """
    start = index
    while start > 0 and index - start < _SCOPE_LINE_LIMIT:
        previous = lines[start - 1]
        if not previous.strip() or fenced[start - 1]:
            break
        start -= 1
    block = list(lines[start : index + 1])

    if any(_LIST_ITEM_RE.match(line) for line in block):
        cursor = start - 1
        while cursor >= 0 and not lines[cursor].strip():
            cursor -= 1
        if cursor >= 0 and not fenced[cursor] and lines[cursor].rstrip().endswith(":"):
            intro_start = cursor
            while (
                intro_start > 0
                and lines[intro_start - 1].strip()
                and not fenced[intro_start - 1]
                and cursor - intro_start < _SCOPE_LINE_LIMIT
            ):
                intro_start -= 1
            block = list(lines[intro_start : cursor + 1]) + block

    return "\n".join(block)


def _is_prohibition_statement(
    lines: Sequence[str], index: int, fenced: Sequence[bool]
) -> bool:
    """True when this line states the rule rather than performing it."""
    if fenced[index]:
        return False
    return bool(_PROHIBITION_RE.search(_statement_scope(lines, index, fenced)))


def _is_status_context(scope: str) -> bool:
    """True when this passage is about the board's Status field at all."""
    return bool(_STATUS_CONTEXT_RE.search(scope) or _CANONICAL_STATUS_RE.search(scope))


def _is_retirement_mention(lines: Sequence[str], index: int, fenced: Sequence[bool]) -> bool:
    """True when this line NAMES the value as retired rather than using it.

    Two shapes, because a retirement is written two ways. In code it is a
    declaration — a binding whose name says the values in it are retired, of
    which `RETIRED_STATUSES = ("Skipped",)` is the one that matters. In prose
    it is an assertion, and that is `_PROHIBITION_RE` ("is not a status", "is
    refused") widened by the retirement vocabulary the merge gate never needed.

    Unlike `_is_prohibition_statement` this reads a fenced or uncommented line
    too: the retired status is a VALUE, so it appears in docstrings, error
    messages and assertions, none of which are prose lines. What keeps that
    from becoming an excuse for a real resurrection is that the caller settles
    `_STATUS_ASSIGNMENT_RE` first and never reaches here for one.
    """
    scope = _statement_scope(lines, index, fenced)
    return bool(_RETIRED_BINDING_RE.search(scope) or _RETIREMENT_RE.search(scope)) or bool(
        _PROHIBITION_RE.search(scope)
    )


#: Negative boundary declarations for auto-merge.
_AUTO_MERGE_NEGATIVE_BOUNDARY_RE = re.compile(
    r"""["']?auto[-_]merge\w*["']?\s*(?::\s*\w+\s*)?[:=]\s*["']?false\b"""
    r"""|boundaries\[["']auto[-_]merge\w*["']\]\s*=\s*False\b"""
    r"""|\.get\(["']auto[-_]merge\w*["']\s*,\s*False\)"""
    r"""|auto[-_]merge=false\b""",
    re.IGNORECASE,
)

#: Assertions and checks asserting that auto-merge is prohibited / False.
_AUTO_MERGE_ASSERTION_RE = re.compile(
    r"""assert(?:_true|_false|Equal|Is|In)?\s*\(.*auto[-_]merge.*(?:False|is\s+False|\.error)\b"""
    r"""|assert_false\s*\(.*auto[-_]merge"""
    r"""|for\s+\w+\s+in\s+[^:\n]*auto[-_]merge""",
    re.IGNORECASE,
)

#: Read-only mock snapshots in tests.
_SNAPSHOT_MOCK_RE = re.compile(r"""["']?(?:github_)?snapshot\w*["']?\s*\]?\s*[:=]""")


def _is_auto_merge_boundary_or_prohibition(
    lines: Sequence[str], index: int, fenced: Sequence[bool]
) -> bool:
    """True when this line or statement declares auto-merge disallowed rather than enabling it.

    In code this is a negative boundary: setting `auto_merge_allowed = False`
    or asserting `assert_false(boundaries.auto_merge_allowed)`. In prose,
    docstrings, comments, or error messages, it is an assertion of prohibition
    ("no auto-merge", "zero auto-merge", "NEVER auto-merges").
    """
    line = lines[index]
    if _AUTO_MERGE_NEGATIVE_BOUNDARY_RE.search(line) or _AUTO_MERGE_ASSERTION_RE.search(line):
        return True
    unfenced = [False] * len(lines)
    scope = _statement_scope(lines, index, unfenced)
    return bool(_PROHIBITION_RE.search(scope) and re.search(r"auto[-_]merge", scope, re.IGNORECASE))


def _is_snapshot_read(text: str) -> bool:
    """True when this line is constructing or reading a mock snapshot of external GitHub state."""
    return bool(_SNAPSHOT_MOCK_RE.search(text))

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
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _is_scanner_source(path, body):
            continue
        lines = body.splitlines()
        fenced = _fenced_flags(
            lines, prose_document=path.suffix.lower() in _PROSE_DOCUMENT_SUFFIXES
        )
        scoped = bool(_SCOPED_PATH_RE.search(relative))
        for number, text in enumerate(lines, start=1):
            matched = [
                mechanism
                for mechanism, pattern, scope_limited in _MECHANISM_PATTERNS
                if (scoped or not scope_limited) and pattern.search(text)
            ]
            if not matched:
                continue
            if _is_prohibition_statement(lines, number - 1, fenced):
                continue
            filtered = []
            for mechanism in matched:
                if mechanism == "auto-merge-enablement" and _is_auto_merge_boundary_or_prohibition(
                    lines, number - 1, fenced
                ):
                    continue
                if mechanism == "runtime-done-transition" and _is_snapshot_read(text):
                    continue
                filtered.append(mechanism)
            occurrences.extend(
                MergeOccurrence(relative, number, mechanism) for mechanism in filtered
            )

    return MergeScanReport(tuple(occurrences), not occurrences)


def scan_retired_status(
    root: Path, *, allowlist: Optional[Sequence[str]] = None
) -> MergeScanReport:
    """A retired status must be absent from every active surface — as a USE.

    Naming a retired value is not resurrecting it. The registry has to declare
    it, the module that refuses it has to name it in the refusal, the document
    that records the retirement has to print it, and a test has to feed it in
    to prove the refusal bites. What the gate is looking for is the opposite:
    the value being written to a status, offered in a status list, or handed to
    a worker as somewhere to move a card.

    The distinction is made three ways, in this order:

      1. The registry module is excluded whole, by package-relative path plus
         its own declaration, the same intrinsic test that excludes the scanner
         from `scan_merge_prohibitions`. It travels into `.claude/bin/` with the
         code; a file that merely occupies that path is still scanned.
      2. A status ASSIGNMENT is an occurrence unconditionally. Nothing excuses
         it, exactly as no paragraph excuses a command inside a code fence.
      3. Anything else is an occurrence only where the passage is about the
         status field AND does not name the value as retired.

    Known gap: a write through a helper function whose name is not status-ish
    (for example, `move_card(id, "Skipped")`) is not caught in source code
    because the regex scanner operates on statement syntax rather than call-graph
    dataflow.
    """
    root = Path(root)
    entries = load_allowlist(root) if allowlist is None else tuple(allowlist)
    occurrences: list[MergeOccurrence] = []
    for path in _scannable_files(root):
        relative = path.relative_to(root).as_posix()
        if relative == ALLOWLIST_FILENAME or _allowlisted(relative, entries):
            continue
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _is_retired_status_registry(path, body) or _is_scanner_source(path, body):
            continue
        lines = body.splitlines()
        fenced = _fenced_flags(
            lines, prose_document=path.suffix.lower() in _PROSE_DOCUMENT_SUFFIXES
        )
        for number, text in enumerate(lines, start=1):
            # A binding whose NAME says retired is the declaration list, which
            # is the definition and not a use — the one thing that can look
            # like an assignment and be exempt from it.
            if _STATUS_ASSIGNMENT_RE.search(text) and not _RETIRED_BINDING_RE.search(text):
                occurrences.append(MergeOccurrence(relative, number, "retired-status-skipped"))
                continue
            if not _RETIRED_STATUS_RE.search(text):
                continue
            index = number - 1
            if not _is_status_context(_statement_scope(lines, index, fenced)):
                continue
            if _is_retirement_mention(lines, index, fenced):
                continue
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


# ───────────────────────────── the local Codex gate ─────────────────────────────
#
# Exactly ONE parallel maximum-level local Codex fleet per code pull request.
# Four lenses, run concurrently, every one on the newest model at maximum
# reasoning effort. Two models working together: Claude writes the code, Codex
# reviews it from four angles, Claude fixes every finding.
#
# CodeRabbit, Copilot, Greptile, and the GitHub `@codex` connector are NOT gates.
# The connector in particular has its own easily-exhausted review rate limit, and
# treating it as the gate produced a false "usage limit" while the task budget
# was at 99%. The binding gate is the local fleet below.

#: The newest Codex model. Not the `~/.codex/config.toml` default — that lags.
CODEX_MODEL = "gpt-5.5"

#: Maximum reasoning effort. Anything less is not the gate this contract names.
CODEX_REASONING_EFFORT = "high"

#: The four lenses, in launch order.
CODEX_LENSES: tuple[str, ...] = (
    "structured-diff",
    "correctness",
    "security",
    "performance-design-consistency",
)

#: Prompts for the three plain-`codex exec` lenses. `structured-diff` has none,
#: and must never be given one — see `build_lens_command`.
CODEX_LENS_PROMPTS: Mapping[str, str] = {
    "correctness": (
        "correctness lens: read the changed files under this worktree and hunt for "
        "logic errors, unhandled failure paths, contract drift between callers and "
        "callees, and anything that fails open where it must fail closed. "
        "Output one line per finding: file:line — severity (P1/P2/nit) — fix."
    ),
    "security": (
        "security lens: read the changed files under this worktree and hunt for "
        "credential exposure, unsanitized output crossing a publication boundary, "
        "injection, path traversal, unsafe deserialization, and missing "
        "authorization checks. "
        "Output one line per finding: file:line — severity (P1/P2/nit) — fix."
    ),
    "performance-design-consistency": (
        "performance-design-consistency lens: read the changed files under this "
        "worktree and hunt for avoidable work in hot paths, unbounded growth, and "
        "divergence from the conventions the surrounding code already establishes. "
        "Output one line per finding: file:line — severity (P1/P2/nit) — fix."
    ),
}

#: Suffixes whose complete diff makes a pull request documentation-only.
_DOCUMENTATION_SUFFIXES: frozenset[str] = frozenset({".md", ".mdx", ".rst", ".txt"})

#: `file:line — severity — fix`, with an em dash or a hyphen as the separator.
_FINDING_RE = re.compile(
    r"^\s*(?P<location>[^\s:]+:\d+)\s*[—-]\s*(?P<severity>P1|P2|P3|nit)\s*[—-]\s*(?P<summary>.+?)\s*$",
    re.IGNORECASE,
)


class CodexGateError(ValueError):
    """The Codex gate contract was violated. Maps to exit code 65."""

    exit_code = EXIT_CONFIG

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Finding:
    lens: str
    severity: str
    location: str
    summary: str
    resolved: bool = False
    evidence: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


@dataclass(frozen=True)
class LensResult:
    name: str
    command: tuple[str, ...]
    model: str
    reasoning_effort: str
    exit_code: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "model": self.model,
            "name": self.name,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True)
class CodexFleetReport:
    lenses: tuple[LensResult, ...]
    findings: tuple[Finding, ...]
    passed: bool
    reason_code: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "lenses": [lens.to_dict() for lens in self.lenses],
            "passed": self.passed,
            "reason_code": self.reason_code,
        }

    def published_summary(self) -> str:
        """The only thing that may be published: counts, locations, severities.

        Raw lens output stays on local disk, outside the Git tree. It is
        unbounded text produced by a model reading the whole worktree, which is
        exactly the shape of payload that carries a secret by accident.
        """
        if not self.lenses:
            return f"Codex gate: {self.reason_code or 'no lenses run'}."
        lines = [
            f"Codex fleet: {len(self.lenses)} lenses, model {CODEX_MODEL}, "
            f"reasoning effort {CODEX_REASONING_EFFORT}.",
            f"Result: {'passed' if self.passed else 'blocked'}"
            + (f" ({self.reason_code})" if self.reason_code else "")
            + ".",
        ]
        for finding in self.findings:
            state = "resolved" if finding.resolved else "unresolved"
            lines.append(f"- {finding.location} — {finding.severity} — {state}")
        return "\n".join(lines)


def is_documentation_only(changed_files: Sequence[str]) -> bool:
    """True when EVERY changed file is documentation. An empty diff is not."""
    paths = [p for p in (changed_files or ()) if isinstance(p, str) and p.strip()]
    if not paths:
        return False
    return all(Path(p).suffix.lower() in _DOCUMENTATION_SUFFIXES for p in paths)


def raw_output_dir() -> Path:
    """Where raw lens output goes: local disk, deliberately outside the tree."""
    return Path(tempfile.gettempdir()) / "super-board-codex"


#: A resolved commit object name. Abbreviated names are accepted; anything that
#: is not hexadecimal is not a commit and is refused.
_COMMIT_SHA_RE = re.compile(r"\A[0-9a-fA-F]{7,64}\Z")


def resolve_merge_base(
    base_ref: str, worktree: Path, *, runner: Optional[Any] = None
) -> str:
    """Resolve `git merge-base origin/<base_ref> HEAD` to a real commit SHA.

    The fleet spawns argv directly with no shell, so a `$(...)` substitution in
    an argument is passed through to `codex` verbatim and reviews a ref that
    cannot exist. The expansion has to happen here, in Python, or not at all.

    Fails closed: a non-zero `git`, an unusable worktree, or anything on stdout
    that is not a commit object name raises rather than returning a value the
    caller would hand to the gate.
    """
    command = ("git", "merge-base", f"origin/{base_ref}", "HEAD")
    run = _default_runner if runner is None else runner
    try:
        result = run(command, Path(worktree))
    except Exception as exc:  # a merge base we could not compute is not a base
        raise CodexGateError(
            "codex-merge-base-unresolved",
            f"could not run `git merge-base origin/{base_ref} HEAD`: {exc}",
        ) from exc
    if int(result.get("exit_code", 1)) != 0:
        raise CodexGateError(
            "codex-merge-base-unresolved",
            f"`git merge-base origin/{base_ref} HEAD` failed; the structured lens has no "
            "diff to review and the gate must not report a pass it never performed",
        )
    sha = str(result.get("stdout") or "").strip().splitlines()
    candidate = sha[0].strip() if sha else ""
    if not _COMMIT_SHA_RE.match(candidate):
        raise CodexGateError(
            "codex-merge-base-unresolved",
            f"`git merge-base origin/{base_ref} HEAD` did not name a commit",
        )
    return candidate


def build_lens_command(
    lens: str,
    base_ref: str,
    *,
    prompt: Optional[str] = None,
    merge_base: Optional[str] = None,
) -> tuple[str, ...]:
    """The exact command one lens issues.

    `codex exec review` and a custom prompt are mutually exclusive — the CLI
    rejects the combination — so passing one here would silently lose the entire
    structured review. That is refused rather than dropped.

    `merge_base` must already be a resolved commit SHA. This command is spawned
    as argv without a shell, so a `$(...)` string here is not a substitution, it
    is a literal ref name that no repository has.
    """
    if lens not in CODEX_LENSES:
        raise CodexGateError("codex-lens-unknown", f"{lens!r} is not one of the four lenses")
    common = ("-m", CODEX_MODEL, "-c", f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"')
    if lens == "structured-diff":
        if prompt:
            raise CodexGateError(
                "codex-review-prompt-conflict",
                "`codex exec review` never receives a custom prompt: the CLI rejects the "
                "combination and the structured review would be lost",
            )
        candidate = (merge_base or "").strip()
        if not _COMMIT_SHA_RE.match(candidate):
            raise CodexGateError(
                "codex-merge-base-unresolved",
                "`codex exec review --base` needs a resolved commit SHA; "
                f"{merge_base!r} is not one",
            )
        return ("codex", "exec", "review", "--base", candidate, *common)
    return ("codex", "exec", *common, "-s", "read-only", prompt or CODEX_LENS_PROMPTS[lens])


def parse_findings(lens: str, output: str) -> tuple[Finding, ...]:
    """Parse `file:line — severity — fix` lines. Unresolved on arrival."""
    findings: list[Finding] = []
    for line in (output or "").splitlines():
        match = _FINDING_RE.match(line)
        if match is None:
            continue
        findings.append(
            Finding(
                lens=lens,
                severity=match.group("severity"),
                location=match.group("location"),
                summary=match.group("summary"),
            )
        )
    return tuple(findings)


def resolve_findings(
    findings: Sequence[Finding], resolutions: Mapping[str, str]
) -> tuple[Finding, ...]:
    """Mark findings resolved — only where committed evidence says so.

    Every severity counts, nits included. "It's only a nit" is how a review
    becomes advisory, and an advisory review is not a gate.
    """
    resolved: list[Finding] = []
    for finding in findings:
        evidence = (resolutions or {}).get(finding.location)
        has_evidence = isinstance(evidence, str) and bool(evidence.strip())
        resolved.append(
            Finding(
                lens=finding.lens,
                severity=finding.severity,
                location=finding.location,
                summary=finding.summary,
                resolved=has_evidence,
                evidence=evidence if has_evidence else None,
            )
        )
    return tuple(resolved)


def _default_runner(command: Sequence[str], cwd: Path) -> Mapping[str, Any]:
    """Spawn one lens.

    `stdin=DEVNULL` is load-bearing, not hygiene. `codex exec "<prompt>"` reads
    stdin when no terminal is attached — backgrounded, in CI, or inside a
    subagent — and BLOCKS FOREVER. It emits exactly one line first,
    `Reading additional input from stdin...`, then nothing: no error, no
    timeout, no exit. A fleet spawned that way reports four lenses running and
    delivers one, because `codex exec review` takes no prompt argument and is
    unaffected — so the structured lens produces a normal review while the three
    prompted lenses sit silent at ~39 bytes. This happened on this release's own
    review gate. Closing stdin turns the hang into an immediate EOF.
    """
    result = subprocess.run(
        list(command),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=3600,
        stdin=subprocess.DEVNULL,
    )
    return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def _read_ledger(path: Optional[Path]) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def run_codex_fleet(
    base_ref: str,
    worktree: Path,
    documentation_only: bool,
    *,
    runner: Optional[Any] = None,
    ledger: Optional[Path] = None,
    pull_request_url: Optional[str] = None,
    force_rerun: bool = False,
    prompts: Optional[Mapping[str, str]] = None,
    resolutions: Optional[Mapping[str, str]] = None,
    model: str = CODEX_MODEL,
    reasoning_effort: str = CODEX_REASONING_EFFORT,
    plan_only: bool = False,
    merge_base_resolver: Optional[Any] = None,
) -> CodexFleetReport:
    """Run the four lenses in parallel and decide whether the gate passes."""
    if model != CODEX_MODEL:
        raise CodexGateError(
            "codex-model-invalid",
            f"every lens must run on {CODEX_MODEL}; {model!r} is not the gate this contract names",
        )
    if reasoning_effort != CODEX_REASONING_EFFORT:
        raise CodexGateError(
            "codex-reasoning-effort-invalid",
            f"every lens must run at model_reasoning_effort={CODEX_REASONING_EFFORT!r}; "
            f"{reasoning_effort!r} is a cheaper review pretending to be the gate",
        )

    prompts = dict(prompts or {})
    if prompts.get("structured-diff"):
        raise CodexGateError(
            "codex-review-prompt-conflict",
            "`codex exec review` never receives a custom prompt",
        )

    if documentation_only:
        # A diff that is entirely documentation has no runtime behaviour to
        # review; running four maximum-effort lenses over it burns usage for
        # nothing.
        return CodexFleetReport((), (), True, "documentation-only-exempt")

    ledger_data = _read_ledger(ledger)
    key = pull_request_url or "unknown-pull-request"
    if ledger is not None and key in ledger_data and not force_rerun:
        raise CodexGateError(
            "codex-fleet-already-run",
            "one fleet per pull request. A second automatic run costs the same usage and "
            "reviews the same code; re-review only on an explicit request",
        )

    # Resolved before any lens is spawned: an unresolvable merge base means the
    # structured lens would review nothing, and a fleet that reviews nothing
    # must not be able to report a pass.
    resolve = resolve_merge_base if merge_base_resolver is None else merge_base_resolver
    merge_base = resolve(base_ref, Path(worktree))

    commands = {
        lens: build_lens_command(
            lens, base_ref, prompt=prompts.get(lens), merge_base=merge_base
        )
        for lens in CODEX_LENSES
    }
    if plan_only:
        return CodexFleetReport(
            tuple(
                LensResult(lens, commands[lens], model, reasoning_effort, 0)
                for lens in CODEX_LENSES
            ),
            (),
            True,
            "plan-only",
        )

    run = _default_runner if runner is None else runner
    worktree = Path(worktree)
    raw_dir = raw_output_dir()
    raw_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, Mapping[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(CODEX_LENSES)) as pool:
        futures = {
            pool.submit(run, commands[lens], worktree): lens for lens in CODEX_LENSES
        }
        for future in as_completed(futures):
            lens = futures[future]
            try:
                results[lens] = future.result()
            except Exception as exc:  # a lens that could not run did not pass
                results[lens] = {"exit_code": 1, "stdout": "", "stderr": str(exc)}

    lenses: list[LensResult] = []
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for lens in CODEX_LENSES:
        result = results.get(lens, {"exit_code": 1, "stdout": ""})
        lenses.append(
            LensResult(lens, commands[lens], model, reasoning_effort, int(result.get("exit_code", 1)))
        )
        stdout = str(result.get("stdout") or "")
        # Raw output stays on local disk, outside the Git tree.
        try:
            (raw_dir / f"{lens}.log").write_text(stdout, encoding="utf-8")
        except OSError:
            pass
        for finding in parse_findings(lens, stdout):
            # The same finding surfaced by four lenses is one finding to fix.
            fingerprint = (finding.location, finding.severity)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            findings.append(finding)

    resolved = resolve_findings(findings, resolutions or {})
    failed_lens = any(lens.exit_code != 0 for lens in lenses)
    unresolved = [f for f in resolved if not f.resolved]

    reason: Optional[str] = None
    if failed_lens:
        reason = "codex-lens-failed"
    elif unresolved:
        reason = "codex-findings-unresolved"

    if ledger is not None:
        ledger_data[key] = {"base_ref": base_ref, "lenses": list(CODEX_LENSES)}
        try:
            Path(ledger).parent.mkdir(parents=True, exist_ok=True)
            Path(ledger).write_text(
                json.dumps(ledger_data, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            pass

    return CodexFleetReport(tuple(lenses), resolved, reason is None, reason)


__all__ = [
    "ALLOWLIST_FILENAME",
    "CODEX_LENSES",
    "CODEX_LENS_PROMPTS",
    "CODEX_MODEL",
    "CODEX_REASONING_EFFORT",
    "DONE_WRITER",
    "MERGE_MECHANISMS",
    "REQUIRED_MERGE_CONFIG",
    "REQUIRED_REPOSITORY_SETTINGS",
    "CodexFleetReport",
    "CodexGateError",
    "Finding",
    "LensResult",
    "MergeContractError",
    "MergeOccurrence",
    "MergeScanReport",
    "ReviewHandoff",
    "build_lens_command",
    "is_documentation_only",
    "load_allowlist",
    "may_write_done",
    "parse_findings",
    "raw_output_dir",
    "resolve_findings",
    "resolve_merge_base",
    "review_handoff",
    "run_codex_fleet",
    "scan_merge_prohibitions",
    "scan_retired_status",
    "verify_human_merge_config",
    "verify_repository_settings",
]
