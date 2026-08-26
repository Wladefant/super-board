#!/usr/bin/env python3
"""Release identity: one number, derived by a written rule.

This repository had four sources of truth for "what version is this?" and they
disagreed. That is not a cosmetic problem: the installer pins a release, the
manifest records it, and a support question that starts "I'm on 1.6.0" cannot be
answered when the tree says 1.7.1 and the only tag says 1.2.0.

The rule, in full:

1. **Content sources vote.** `VERSION` and the newest `RELEASE-NOTES.md`
   heading are updated as part of cutting a release, so they are the two claims
   about what the code *is*. They must agree; if they do not, reconciliation
   fails rather than picking one.
2. **`skills/super-board/VERSION` never votes.** It is a mirror that was last
   updated at v1.6.0 and then forgotten. A stale mirror is not evidence of an
   older release; it is evidence of a missed bump. From this release on it is
   pinned to the root version and asserted equal.
3. **The git tag never votes.** A tag is a *publication* record, not a
   release-content record. Its absence means a release was never published, not
   that it never happened.
4. **The next number** is a major bump when the release changes a documented
   contract in a backward-incompatible way, and a minor bump otherwise.

`authorize_release_publication` exists because tagging and publishing are
outward-facing: they put a number in front of other people. It refuses unless
an explicit approval is passed in. Nothing in this module ever creates a tag.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

try:  # normal package import
    from . import EXIT_CONFIG
    from .routing import NON_DISPATCH_BRANCHES
except ImportError:  # executed as a plain file path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFIG
    from super_board_runtime.routing import NON_DISPATCH_BRANCHES

#: The four sources that disagreed, named as they are named in the audit.
VERSION_SOURCES: tuple[str, ...] = (
    "VERSION",
    "skills/super-board/VERSION",
    "RELEASE-NOTES.md",
    "git-tag",
)

#: Where the reconciliation is written down.
RECONCILIATION_DOCUMENT = "docs/version-reconciliation.md"

#: The runtime reference documents. `skills/super-board/SKILL.md` and
#: `references/run.md` route workers into these files mid-run, so they are read
#: as instructions to somebody using the runtime today — the same standard as
#: the top-level guidance below. They sat OUTSIDE this scan until now, which is
#: how a superseded quota contract (a threshold argument and a sleep to the
#: reset) and an undispatchable route example survived a release that retired
#: both.
ACTIVE_REFERENCE_FILES: tuple[str, ...] = (
    "skills/super-board/references/agent-native.md",
    "skills/super-board/references/block-template.md",
    "skills/super-board/references/config-schema.json",
    "skills/super-board/references/lint.md",
    "skills/super-board/references/onboard.md",
    "skills/super-board/references/github-ops.md",
    "skills/super-board/references/rate-limit-etiquette.md",
    "skills/super-board/references/run-workflow.md",
    "skills/super-board/references/run.md",
    "skills/super-board/references/status.md",
    "skills/super-board/references/stop.md",
)

#: Reference documents deliberately left out of the scan, listed so that the
#: omission is a decision somebody made rather than a gap nobody noticed.
#:
#: There are none. `references/status.md` was the last entry: it transcribes
#: what `scripts/super-board-status.py` prints, and the renderer still printed a
#: retired-status glyph row and a collapsed-history merge label, so the document
#: was a true record of a wrong renderer. The renderer was corrected, so the
#: transcription is correctable too, and the exemption has nothing left to
#: excuse.
UNSCANNED_REFERENCE_FILES: tuple[str, ...] = ()

#: Documents that carry ACTIVE guidance. Historical release-notes sections are
#: a record of what past releases did and are not rewritten; everything here is
#: read as an instruction to somebody using the runtime today.
ACTIVE_GUIDANCE_FILES: tuple[str, ...] = (
    "README.md",
    "MY-SYSTEM.md",
    "DOCS-SYSTEM.md",
    "docs/README.md",
    "skills/super-board/SKILL.md",
    "skills/super-build/SKILL.md",
    "skills/super-qa/SKILL.md",
    "skills/super-review/SKILL.md",
) + ACTIVE_REFERENCE_FILES

#: Branches that can never be dispatch routes, spelled for a regex. Taken from
#: `routing.NON_DISPATCH_BRANCHES` rather than restated, so retiring another
#: branch teaches the scanner in the same edit.
_NON_DISPATCH_ALTERNATION = "|".join(
    re.escape(branch) for branch in sorted(NON_DISPATCH_BRANCHES)
)

#: Claims this release retires. Each one was true of some earlier release and is
#: now actively wrong, which is the dangerous kind of documentation.
RETIRED_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("retired-status-skipped", re.compile(r"\bSkipped\b")),
    ("squash-merging", re.compile(r"squash[ _-]?merg|merge_method\D{0,6}squash", re.I)),
    (
        "runtime-merging",
        re.compile(
            r"(either merges|then merges|auto[ _-]?merge|the (?:agent|worker|reviewer|runtime|"
            r"workflow) merges)",
            re.I,
        ),
    ),
    (
        "200-point-reserve",
        re.compile(r"\b200\b[^\n]{0,48}(point|quota|reserve)|(quota|reserve)[^\n]{0,48}\b200\b", re.I),
    ),
    (
        # The guard's numeric argument is an estimated mutation cost, not a
        # level to stay above. Guidance that frames it as a threshold — or that
        # gates on the remaining balance falling under a number — is teaching
        # the contract this release replaced.
        "quota-threshold-guard",
        re.compile(
            r"(remaining|quota|graphql|balance|budget)[^\n]{0,40}(below|under|beneath|<)\s*\d{2,5}"
            r"|(quota|graphql|rate.?limit|reserve|sb_gh_guard_check)[^\n]{0,60}\bthreshold\b"
            r"|\bthreshold\b[^\n]{0,60}(quota|graphql|rate.?limit|reserve|sb_gh_guard_check)",
            re.I,
        ),
    ),
    (
        # Reaching the reserve halts with exit 75. Waiting for the window to
        # roll over holds a lane open for nothing, and retrying spends the
        # reserve the runtime exists to protect.
        "sleep-to-reset-remedy",
        re.compile(
            r"(sleep|sleeps|sleeping|wait|waits|waiting|pause|pauses|pausing|back\s?off|"
            r"backoff)[^\n]{0,40}\b(until|through|to|till|for|out)\b[^\n]{0,24}reset"
            r"|re-?try[^\n]{0,24}(until|when)[^\n]{0,24}(quota|reset|window)",
            re.I,
        ),
    ),
    (
        # A route declaration naming a non-dispatch branch fails closed as
        # `route-declaration-unknown`, so an example offering one hands the
        # reader a label that can never resolve.
        "impossible-branch-route",
        re.compile(
            rf"\"route:(?:{_NON_DISPATCH_ALTERNATION})\""
            rf"|branch[ \t_-]*route[ \t]*:[ \t]*(?:{_NON_DISPATCH_ALTERNATION})\b",
            re.I,
        ),
    ),
    (
        "workflow-default-backend",
        re.compile(
            r"(default[^\n]{0,60}\bworkflow\b[^\n]{0,20}backend|\"workflow\"[^\n]{0,30}is the "
            r"default|default backend[^\n]{0,40}workflow|worker_backend\"?\s*:\s*\"workflow\")",
            re.I,
        ),
    ),
)

#: Everything the release notes and the README must actually say. A release that
#: changes this many contracts and documents none of them is not shippable.
RELEASE_CONTRACT_TOPICS: tuple[str, ...] = (
    "seven-state",
    "claude-p",
    "activation modes",
    "1,000-point",
    "exact-SHA QA",
    "invalidation",
    "fail-closed branch routing",
    "Codex",
    "rebase",
    "intake normalizer",
    "closure normalizer",
    "fallback auto-add",
    "read-only projection",
    "install manifest",
)

#: A retired claim that is being REFUSED is not an advertisement for it. The
#: prohibition lists in the skills name every mechanism precisely so they can
#: forbid it, and the lead-in that forbids them sits a few lines above the list.
#: `no-op` is not a negation of anything — it describes what a call costs. It
#: used to satisfy this pattern and quietly excuse the whole line, which is one
#: reason superseded quota guidance read as clean.
_RETIRED_NEGATION_RE = re.compile(
    r"(never|not\b|no\b(?!-op)|cannot|can't|refus|reject|prohibit|forbidden|denied|false|removed|"
    r"retired|must not|is no longer|instead of|rather than|collapses|destroys|opt[- ]in|explicit opt|non-default)",
    re.I,
)

#: How far above a match to look for that lead-in.
_NEGATION_LOOKBACK = 6

#: Claims for which only a refusal ON THE LINE ITSELF counts. A configuration
#: example is a thing to copy, and the copied line does not carry the
#: surrounding paragraph's caveats with it: the schema's undispatchable route
#: sat two lines under prose ending "not a coin toss", and that stray negation
#: was enough to excuse it.
_LINE_SCOPED_NEGATION_CLAIMS: frozenset[str] = frozenset({"impossible-branch-route"})

_HEADING_RE = re.compile(r"(?m)^##\s+v?(\d+\.\d+\.\d+)")


class ReleaseError(ValueError):
    """The release identity does not reconcile. Maps to exit code 65."""

    exit_code = EXIT_CONFIG

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class VersionObservation:
    source: str
    value: Optional[str]
    read_at_sha: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


@dataclass(frozen=True)
class VersionIdentity:
    root: Optional[str]
    skill: Optional[str]
    notes: Optional[str]
    ok: bool
    disagreements: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        body = dict(asdict(self))
        body["disagreements"] = list(self.disagreements)
        return body


@dataclass(frozen=True)
class ReleaseAuthorization:
    version: str
    authorized: bool
    reason_code: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


# ───────────────────────────── reading ─────────────────────────────


def normalize_version(value: Optional[str]) -> Optional[str]:
    """Strip a leading `v` and surrounding whitespace. Nothing else."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    return trimmed[1:].strip() if trimmed[:1] in ("v", "V") else trimmed


def newest_release_notes_version(text: str) -> Optional[str]:
    match = _HEADING_RE.search(text or "")
    return match.group(1) if match else None


def newest_release_notes_section(text: str) -> str:
    """The active section: from the newest heading to the one before it."""
    matches = list(_HEADING_RE.finditer(text or ""))
    if not matches:
        return ""
    start = matches[0].start()
    end = matches[1].start() if len(matches) > 1 else len(text)
    return text[start:end]


def newest_contract_release_section(text: str) -> str:
    """The newest section for a release that could have changed a contract.

    A patch release cannot change one: `derive_next_release` refuses to number a
    backward-incompatible change as a patch. So the contract inventory is
    asserted against the newest major or minor section, and a defect-only
    release is not required to restate contracts it never touched. Requiring it
    to would turn the inventory into a copy-paste ritual — and an inventory
    everybody copies forward without reading stops being a check the moment one
    of its entries goes stale.

    Falls back to the newest section when no major/minor heading exists at all,
    so a repository whose whole history is patches still has something checked.
    """
    body = text or ""
    matches = list(_HEADING_RE.finditer(body))
    if not matches:
        return ""
    for index, match in enumerate(matches):
        if match.group(1).endswith(".0"):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
            return body[match.start() : end]
    return newest_release_notes_section(body)


def read_version_sources(root: Path) -> dict[str, Optional[str]]:
    """The three in-tree sources. The git tag is read separately, by the caller."""
    root = Path(root)

    def read(relative: str) -> Optional[str]:
        try:
            return (root / relative).read_text(encoding="utf-8")
        except OSError:
            return None

    notes = read("RELEASE-NOTES.md") or ""
    return {
        "VERSION": normalize_version(read("VERSION")),
        "skills/super-board/VERSION": normalize_version(read("skills/super-board/VERSION")),
        "RELEASE-NOTES.md": normalize_version(newest_release_notes_version(notes)),
    }


def verify_version_identity(root: Path) -> VersionIdentity:
    """All three in-tree sources must be byte-identical after stripping `v`."""
    sources = read_version_sources(root)
    values = {name: value for name, value in sources.items()}
    distinct = {value for value in values.values()}
    disagreements = tuple(
        f"{name}={value!r}" for name, value in sorted(values.items())
    ) if len(distinct) != 1 or None in distinct else ()
    return VersionIdentity(
        root=values["VERSION"],
        skill=values["skills/super-board/VERSION"],
        notes=values["RELEASE-NOTES.md"],
        ok=not disagreements,
        disagreements=disagreements,
    )


# ───────────────────────────── the rule ─────────────────────────────


def _parts(value: str) -> tuple[int, int, int]:
    numbers = [int(chunk) for chunk in re.findall(r"\d+", value or "")][:3]
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2]


def reconcile_current_release(
    root_version: Optional[str],
    skill_version: Optional[str],
    notes_version: Optional[str],
    tag_version: Optional[str],
) -> tuple[str, tuple[str, ...]]:
    """Decide which of four disagreeing values is the current release, and say why."""
    root = normalize_version(root_version)
    notes = normalize_version(notes_version)
    skill = normalize_version(skill_version)
    tag = normalize_version(tag_version)

    if not root or not notes:
        raise ReleaseError(
            "release-content-source-missing",
            "the current release is decided by VERSION and the newest RELEASE-NOTES "
            "heading; one of them is missing",
        )
    if root != notes:
        raise ReleaseError(
            "release-content-sources-disagree",
            f"VERSION says {root} and the newest RELEASE-NOTES heading says {notes}; "
            "reconciliation refuses to pick one",
        )

    reasoning = [
        f"VERSION and the newest RELEASE-NOTES heading both say {root}; both are "
        "updated as part of cutting a release, so together they are the claim about "
        "what the code is.",
        f"skills/super-board/VERSION says {skill}: a lagging mirror, not an independent "
        "claim. It is pinned to the root version from now on and asserted equal.",
        f"The only published git tag is {tag}: a publication record, not a "
        "release-content record. Its absence for later releases means they were "
        "never published, not that they never happened.",
    ]
    return root, tuple(reasoning)


def derive_next_release(
    current: str, *, backward_incompatible: bool, defect_fix_only: bool = False
) -> str:
    """Major for a broken contract, patch for defects only, minor otherwise.

    The patch branch exists because a release that only restores behaviour the
    previous release already promised has nothing to announce: no new contract,
    no new surface, nothing for an operator to adopt. Numbering it a minor would
    say there is something new to read, and the next genuinely new capability
    would then be indistinguishable from it.

    The two flags cannot both be true. A change that breaks a documented
    contract is not a defect fix however it was discovered, and letting the
    combination through would silently take the smaller bump.
    """
    if backward_incompatible and defect_fix_only:
        raise ReleaseError(
            "release-bump-contradictory",
            "a release cannot be both a defect-only fix and a backward-incompatible "
            "contract change; decide which it is before numbering it",
        )
    major, minor, patch = _parts(normalize_version(current) or "")
    if backward_incompatible:
        return f"{major + 1}.0.0"
    if defect_fix_only:
        return f"{major}.{minor}.{patch + 1}"
    return f"{major}.{minor + 1}.0"


# ───────────────────────────── retired claims ─────────────────────────────


def scan_retired_release_claims(
    root: Path, *, files: Optional[Sequence[str]] = None
) -> list[dict[str, Any]]:
    """Find active guidance that still advertises something this release retired."""
    root = Path(root)
    targets: Iterable[str] = ACTIVE_GUIDANCE_FILES if files is None else files
    findings: list[dict[str, Any]] = []

    def inspect(relative: str, text: str, offset: int = 0) -> None:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            number = index + 1 + offset
            context = "\n".join(lines[max(0, index - _NEGATION_LOOKBACK) : index + 1])
            negated_context = bool(_RETIRED_NEGATION_RE.search(context))
            negated_line = bool(_RETIRED_NEGATION_RE.search(line))
            for claim, pattern in RETIRED_CLAIM_PATTERNS:
                excused = negated_line if claim in _LINE_SCOPED_NEGATION_CLAIMS else negated_context
                if excused:
                    continue
                if pattern.search(line):
                    findings.append(
                        {
                            "path": relative,
                            "line": number,
                            "claim": claim,
                            "text": line.strip()[:160],
                        }
                    )

    for relative in targets:
        path = root / relative
        if not path.is_file():
            continue
        inspect(relative, path.read_text(encoding="utf-8"))

    notes_path = root / "RELEASE-NOTES.md"
    if notes_path.is_file():
        text = notes_path.read_text(encoding="utf-8")
        inspect("RELEASE-NOTES.md (newest section)", newest_release_notes_section(text))

    return findings


# ───────────────────────────── publication gate ─────────────────────────────


def authorize_release_publication(
    version: str, *, approved: bool = False, approval_text: Optional[str] = None
) -> ReleaseAuthorization:
    """Tagging and publishing are outward-facing. They need a real approval.

    An implied approval is not one: this returns unauthorized unless the caller
    passes both an explicit flag and the operator's plain-text approval.
    """
    normalized = normalize_version(version)
    if not normalized:
        return ReleaseAuthorization("", False, "release-version-invalid")
    if not approved:
        return ReleaseAuthorization(normalized, False, "release-publication-approval-required")
    if not isinstance(approval_text, str) or not approval_text.strip():
        return ReleaseAuthorization(normalized, False, "release-publication-approval-required")
    return ReleaseAuthorization(normalized, True, None)


def verify_release_tag(
    version: str, recorded_sha: str, *, tag_reader: Callable[[str], Optional[str]]
) -> bool:
    """True only when `v<version>` resolves to exactly the recorded commit."""
    normalized = normalize_version(version)
    if not normalized or not recorded_sha:
        return False
    try:
        resolved = tag_reader(f"v{normalized}")
    except Exception:
        return False
    return bool(resolved) and resolved == recorded_sha


__all__ = [
    "ACTIVE_GUIDANCE_FILES",
    "ACTIVE_REFERENCE_FILES",
    "UNSCANNED_REFERENCE_FILES",
    "RECONCILIATION_DOCUMENT",
    "RELEASE_CONTRACT_TOPICS",
    "RETIRED_CLAIM_PATTERNS",
    "VERSION_SOURCES",
    "ReleaseAuthorization",
    "ReleaseError",
    "VersionIdentity",
    "VersionObservation",
    "authorize_release_publication",
    "derive_next_release",
    "newest_contract_release_section",
    "newest_release_notes_section",
    "newest_release_notes_version",
    "normalize_version",
    "read_version_sources",
    "reconcile_current_release",
    "scan_retired_release_claims",
    "verify_release_tag",
    "verify_version_identity",
]
