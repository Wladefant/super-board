#!/usr/bin/env python3
"""Intake normalization: turn every issue and pull-request event into a plan.

Intake is not a one-shot thing that happens when somebody remembers to fill the
form. It runs on **every** issue and pull-request event and on a bounded
periodic sweep, because the ways a card goes stale are all events: a label is
removed, a milestone is cleared, a body is edited after the fact, a pull request
is re-pointed at another base branch.

Two rules carry most of the weight.

**Incomplete intake is never promoted.** A card whose canonical form is missing
a section, or whose branch route is anything other than `staging` or
`staging-frankfurt`, produces *no* mutation plan at all and a named
`blocked_reason`. It stays in Backlog or Blocked. It never becomes `Ready`,
because `Ready` is the only dispatchable status and a dispatchable card with an
unreadable route is how work lands on the wrong branch.

**Nothing is written from remembered state.** Every planned operation carries
`expected`, `current`, and `desired`, and every operation against an existing
Project item carries a `compare_project_mutation` decision. If the board moved
after the event that triggered this run, the whole plan quarantines rather than
overwriting a decision that is newer than ours.

The milestone lives in exactly one place: the repository. This module reads it
and can plan to synchronize it, but it never copies it into a Project field —
a second ledger is a second thing to disagree with the first.

Closure normalization lives in the same module (see `normalize_closure`),
because closure is the other half of the same event stream.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Optional, Sequence

try:  # normal package import
    from . import EXIT_CONFIG
    from .lifecycle import LIFECYCLE_STATUSES
    from .project import (
        CurrentState,
        ExpectedState,
        MutationDecision,
        ProjectSnapshot,
        compare_project_mutation,
    )
    from .publication import (
        UnsafePublication,
        render_payload,
        sanitize_and_validate_publication,
    )
    from .routing import resolve_branch_route
except ImportError:  # executed as a plain file path
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFIG
    from super_board_runtime.lifecycle import LIFECYCLE_STATUSES
    from super_board_runtime.project import (
        CurrentState,
        ExpectedState,
        MutationDecision,
        ProjectSnapshot,
        compare_project_mutation,
    )
    from super_board_runtime.publication import (
        UnsafePublication,
        render_payload,
        sanitize_and_validate_publication,
    )
    from super_board_runtime.routing import resolve_branch_route

# ───────────────────────────── the trigger set ─────────────────────────────

#: Every issue event that re-runs normalization.
INTAKE_ISSUE_EVENTS: tuple[str, ...] = (
    "opened",
    "edited",
    "labeled",
    "unlabeled",
    "milestoned",
    "demilestoned",
    "reopened",
    "closed",
)

#: Every pull-request event that re-runs normalization. `merged` is listed
#: separately from `closed` because a merged pull request and an abandoned one
#: are different dispositions with different evidence requirements.
INTAKE_PULL_REQUEST_EVENTS: tuple[str, ...] = (
    "opened",
    "edited",
    "synchronize",
    "ready_for_review",
    "converted_to_draft",
    "labeled",
    "unlabeled",
    "milestoned",
    "demilestoned",
    "reopened",
    "closed",
    "merged",
)

#: The bounded periodic reconciliation sweep. Events can be dropped; a sweep is
#: how a card that missed its event still gets normalized.
PERIODIC_SWEEP_EVENT = "schedule"

# ───────────────────────────── the canonical form ─────────────────────────────

#: Every section the canonical issue form asks for, in form order.
REQUIRED_INTAKE_SECTIONS: tuple[str, ...] = (
    "Context",
    "Steps",
    "Acceptance criteria",
    "Test Area",
    "Priority",
    "Work type",
    "Environment constraint",
    "Branch route",
    "Milestone",
)

#: Sections that must be filled in unconditionally. `Milestone` is conditional:
#: a card may carry a concrete Blocked reason instead.
_MANDATORY_SECTIONS: tuple[str, ...] = tuple(
    section for section in REQUIRED_INTAKE_SECTIONS if section != "Milestone"
)

#: The universal work-type taxonomy. Domain labels are per-project; these are not.
WORK_TYPES: tuple[str, ...] = ("build", "docs", "research", "proof", "decision", "risk")

#: The canonical priority ladder.
PRIORITIES: tuple[str, ...] = ("P0", "P1", "P2", "P3")

#: The one canonical environment term. The form, the label taxonomy, and the
#: dispatch filter all use this exact string.
ENVIRONMENT_CONSTRAINT_LABEL = "environment-constraint"

#: Historical spellings that still exist on real boards. They are recognized and
#: mapped onto the canonical term; they are never silently deleted.
LEGACY_ENVIRONMENT_ALIASES: tuple[str, ...] = ("laptop",)

#: Values that explicitly declare "no constraint".
_NO_ENVIRONMENT_VALUES = frozenset({"none", "no", "n/a", "-", "not applicable"})

TEST_AREA_LABEL_PREFIX = "area:"
PRIORITY_LABEL_PREFIX = "priority:"

#: Statuses an un-promotable card is allowed to sit in.
HOLDING_STATUSES: tuple[str, ...] = ("Backlog", "Blocked")

#: A Blocked reason has to say something. These do not.
_PLACEHOLDER_REASONS = frozenset(
    {"tbd", "n/a", "na", "-", "?", "??", "see above", "none", "todo", "unknown", "later"}
)
_MIN_CONCRETE_REASON_LEN = 4

#: Every field a pull-request classification record must carry.
REQUIRED_PULL_REQUEST_CLASSIFICATION_FIELDS: tuple[str, ...] = (
    "base_branch",
    "head_branch",
    "head_sha",
    "draft",
    "reviews",
    "unresolved_discussions",
    "checks",
    "mergeable",
    "linked_issues",
    "closing_state",
    "merge_evidence",
    "supersession_evidence",
    "abandonment_evidence",
    "close_evidence",
)

#: The only evidence kinds that close an issue as completed work. Each one is a
#: thing somebody can open and read; "it looked done" is not on the list.
ACCEPTED_COMPLETION_EVIDENCE_TYPES: tuple[str, ...] = (
    "merged-pull-request",
    "qa-evidence",
    "review-evidence",
    "operator-acceptance",
)

#: Every disposition a closure can resolve to. Anything not on this list leaves
#: the card out of the completion column.
CLOSURE_DISPOSITIONS: tuple[str, ...] = (
    "merged",
    "completed",
    "duplicate",
    "not-planned",
    "superseded",
    "abandoned",
    "reopened",
    "open-in-completion-column",
    "pre-activation-historical",
)

#: The completion column. Only the closure normalizer writes it — see
#: `review.DONE_WRITER`.
COMPLETION_STATUS = "Done"

_SECTION_RE = re.compile(r"(?m)^[ \t]{0,3}#{2,4}[ \t]+(.+?)[ \t]*$")

#: GitHub renders an unanswered optional form field as this exact string.
_NO_RESPONSE = "_no response_"


class NormalizationError(ValueError):
    """Invalid normalization input. Maps to exit code 65."""

    exit_code = EXIT_CONFIG

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ───────────────────────────── snapshots ─────────────────────────────


@dataclass(frozen=True)
class IssueOrPullRequestSnapshot:
    """One subject of normalization, as the triggering event saw it."""

    kind: str
    event: str
    number: Optional[int]
    url: Optional[str]
    node_id: Optional[str]
    state: Optional[str]
    title: Optional[str] = None
    body: Optional[str] = None
    labels: tuple[str, ...] = ()
    milestone: Optional[str] = None
    #: When the event fired.
    observed_at: Optional[str] = None
    #: The Project item's `updated_at` the event carried. Anything newer on the
    #: board is a decision made after ours and must not be overwritten.
    observed_project_updated_at: Optional[str] = None

    # Pull-request-only fields.
    base_branch: Optional[str] = None
    head_branch: Optional[str] = None
    head_sha: Optional[str] = None
    draft: Optional[bool] = None
    reviews: tuple[Mapping[str, Any], ...] = ()
    unresolved_discussions: Optional[int] = None
    checks: tuple[Mapping[str, Any], ...] = ()
    mergeable: Optional[str] = None
    linked_issues: tuple["IssueOrPullRequestSnapshot", ...] = ()
    closing_state: Optional[str] = None
    closed_at: Optional[str] = None
    merged_at: Optional[str] = None
    merge_commit_sha: Optional[str] = None
    supersession_evidence: Optional[Mapping[str, Any]] = None
    abandonment_evidence: Optional[Mapping[str, Any]] = None
    close_evidence: Optional[Mapping[str, Any]] = None
    completion_evidence: Optional[Mapping[str, Any]] = None
    duplicate_of: Optional[str] = None
    not_planned_reason: Optional[str] = None
    #: The SHA the last recorded QA run attested to, if any.
    qa_tested_sha: Optional[str] = None

    @property
    def is_pull_request(self) -> bool:
        return (self.kind or "").strip().casefold() in ("pull_request", "pull-request", "pr")


#: Task 13 names this type `IssueSnapshot`; Task 14 names the same type
#: `IssueOrPullRequestSnapshot`. It is one dataclass with two names.
IssueSnapshot = IssueOrPullRequestSnapshot


@dataclass(frozen=True)
class IntakeForm:
    sections: Mapping[str, str]
    missing: tuple[str, ...]
    complete: bool
    context: Optional[str] = None
    steps: Optional[str] = None
    acceptance_criteria: Optional[str] = None
    test_area: Optional[str] = None
    priority: Optional[str] = None
    work_type: Optional[str] = None
    environment_constraint: Optional[str] = None
    branch_route: Optional[str] = None
    milestone: Optional[str] = None
    blocked_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        body = dict(asdict(self))
        body["sections"] = dict(self.sections)
        body["missing"] = list(self.missing)
        return body


@dataclass(frozen=True)
class PullRequestClassification:
    base_branch: Optional[str]
    head_branch: Optional[str]
    head_sha: Optional[str]
    draft: Optional[bool]
    reviews: tuple[Mapping[str, Any], ...]
    unresolved_discussions: Optional[int]
    checks: tuple[Mapping[str, Any], ...]
    mergeable: Optional[str]
    linked_issues: tuple[str, ...]
    closing_state: Optional[str]
    merge_evidence: Optional[Mapping[str, Any]]
    supersession_evidence: Optional[Mapping[str, Any]]
    abandonment_evidence: Optional[Mapping[str, Any]]
    close_evidence: Optional[Mapping[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "abandonment_evidence": dict(self.abandonment_evidence or {}) or None,
            "base_branch": self.base_branch,
            "checks": [dict(check) for check in self.checks],
            "close_evidence": dict(self.close_evidence or {}) or None,
            "closing_state": self.closing_state,
            "draft": self.draft,
            "head_branch": self.head_branch,
            "head_sha": self.head_sha,
            "linked_issues": list(self.linked_issues),
            "merge_evidence": dict(self.merge_evidence or {}) or None,
            "mergeable": self.mergeable,
            "reviews": [dict(review) for review in self.reviews],
            "supersession_evidence": dict(self.supersession_evidence or {}) or None,
            "unresolved_discussions": self.unresolved_discussions,
        }


@dataclass(frozen=True)
class PlannedOperation:
    kind: str
    expected: ExpectedState
    current: CurrentState
    desired: Mapping[str, Any]
    decision: Optional[MutationDecision] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current.to_dict(),
            "decision": self.decision.to_dict() if self.decision else None,
            "desired": dict(self.desired),
            "expected": self.expected.to_dict(),
            "kind": self.kind,
        }


@dataclass(frozen=True)
class NormalizationPlan:
    subject_url: Optional[str]
    subject_kind: str
    event: str
    normalized: bool
    operations: tuple[PlannedOperation, ...] = ()
    quarantined: tuple[PlannedOperation, ...] = ()
    blocked_reason: Optional[str] = None
    desired_status: Optional[str] = None
    is_member: bool = False
    membership_lookups: int = 0
    membership_key: str = "content_node_id"
    branch_route: Optional[str] = None
    milestone: Optional[str] = None
    milestone_source: str = "repository"
    declared_blocked_reason: Optional[str] = None
    inherited_from: Optional[str] = None
    classification: Optional[PullRequestClassification] = None
    comment: Optional[str] = None
    reopen: bool = False
    evidence_preserved: bool = True
    pre_activation: bool = False
    disposition: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked_reason": self.blocked_reason,
            "branch_route": self.branch_route,
            "classification": self.classification.to_dict() if self.classification else None,
            "comment": self.comment,
            "declared_blocked_reason": self.declared_blocked_reason,
            "desired_status": self.desired_status,
            "disposition": self.disposition,
            "event": self.event,
            "evidence_preserved": self.evidence_preserved,
            "inherited_from": self.inherited_from,
            "is_member": self.is_member,
            "membership_key": self.membership_key,
            "membership_lookups": self.membership_lookups,
            "milestone": self.milestone,
            "milestone_source": self.milestone_source,
            "normalized": self.normalized,
            "operations": [operation.to_dict() for operation in self.operations],
            "pre_activation": self.pre_activation,
            "quarantined": [operation.to_dict() for operation in self.quarantined],
            "reopen": self.reopen,
            "subject_kind": self.subject_kind,
            "subject_url": self.subject_url,
        }


@dataclass(frozen=True)
class _RouteConfig:
    """The only part of the configuration `resolve_branch_route` reads."""

    branch_routes: Mapping[str, str] = field(default_factory=dict)


# ───────────────────────────── the form ─────────────────────────────


def _clean(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed or trimmed.casefold() == _NO_RESPONSE:
        return None
    return trimmed


def parse_intake_form(body: Optional[str]) -> IntakeForm:
    """Split a rendered issue form into its sections.

    Section headings are matched case-insensitively so `### Test area` and
    `### Test Area` are the same section — the form renders whatever the author
    typed into the field label, and a heading-case difference is not a missing
    section.
    """
    sections: dict[str, str] = {}
    text = body if isinstance(body, str) else ""
    matches = list(_SECTION_RE.finditer(text))
    for index, match in enumerate(matches):
        name = match.group(1).strip().rstrip(":").strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[name.casefold()] = text[match.end() : end].strip()

    def section(name: str) -> Optional[str]:
        return _clean(sections.get(name.casefold()))

    missing = tuple(name for name in _MANDATORY_SECTIONS if section(name) is None)
    return IntakeForm(
        sections={name: value for name, value in sections.items()},
        missing=missing,
        complete=not missing,
        context=section("Context"),
        steps=section("Steps"),
        acceptance_criteria=section("Acceptance criteria"),
        test_area=section("Test Area"),
        priority=section("Priority"),
        work_type=section("Work type"),
        environment_constraint=section("Environment constraint"),
        branch_route=section("Branch route"),
        milestone=section("Milestone"),
        blocked_reason=section("Blocked reason"),
    )


def canonical_environment_label(value: Optional[str]) -> Optional[str]:
    """Map any environment declaration onto the canonical taxonomy term."""
    cleaned = _clean(value)
    if cleaned is None:
        return None
    folded = cleaned.casefold()
    if folded in _NO_ENVIRONMENT_VALUES:
        return None
    if folded == ENVIRONMENT_CONSTRAINT_LABEL:
        return ENVIRONMENT_CONSTRAINT_LABEL
    if folded in {alias.casefold() for alias in LEGACY_ENVIRONMENT_ALIASES}:
        return ENVIRONMENT_CONSTRAINT_LABEL
    return None


def _is_concrete_reason(value: Optional[str]) -> bool:
    cleaned = _clean(value)
    if cleaned is None:
        return False
    folded = cleaned.casefold().rstrip(".")
    if folded in _PLACEHOLDER_REASONS:
        return False
    return len(folded) >= _MIN_CONCRETE_REASON_LEN


def _section_slug(name: str) -> str:
    return name.strip().casefold().replace(" ", "-")


def handles_event(kind: str, event: Optional[str]) -> bool:
    """True when this (kind, event) pair re-runs normalization."""
    if not isinstance(event, str) or not event.strip():
        return False
    name = event.strip()
    if name == PERIODIC_SWEEP_EVENT:
        return True
    folded = (kind or "").strip().casefold()
    if folded in ("pull_request", "pull-request", "pr"):
        return name in INTAKE_PULL_REQUEST_EVENTS
    return name in INTAKE_ISSUE_EVENTS


# ───────────────────────────── pull-request classification ─────────────────────────────


def classify_pull_request(pull: IssueOrPullRequestSnapshot) -> PullRequestClassification:
    """The complete record a pull-request decision is allowed to be made on."""
    merge_evidence: Optional[Mapping[str, Any]] = None
    if pull.merged_at:
        merge_evidence = {
            "merged_at": pull.merged_at,
            "merge_commit_sha": pull.merge_commit_sha,
        }
    return PullRequestClassification(
        base_branch=pull.base_branch,
        head_branch=pull.head_branch,
        head_sha=pull.head_sha,
        draft=pull.draft,
        reviews=tuple(dict(review) for review in (pull.reviews or ())),
        unresolved_discussions=pull.unresolved_discussions,
        checks=tuple(dict(check) for check in (pull.checks or ())),
        mergeable=pull.mergeable,
        linked_issues=tuple(
            linked.url for linked in (pull.linked_issues or ()) if linked.url
        ),
        closing_state=pull.closing_state,
        merge_evidence=merge_evidence,
        supersession_evidence=pull.supersession_evidence,
        abandonment_evidence=pull.abandonment_evidence,
        close_evidence=pull.close_evidence,
    )


# ───────────────────────────── project item plumbing ─────────────────────────────


def _item_content_id(item: Mapping[str, Any]) -> Optional[str]:
    for key in ("content_node_id", "contentNodeId"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    content = item.get("content")
    if isinstance(content, Mapping):
        value = content.get("id")
        if isinstance(value, str) and value:
            return value
    return None


def find_project_item(
    items: Sequence[Mapping[str, Any]], content_node_id: Optional[str]
) -> Optional[Mapping[str, Any]]:
    """Locate a card by its immutable content node ID. Never by number or title."""
    if not content_node_id:
        return None
    for item in items:
        if isinstance(item, Mapping) and _item_content_id(item) == content_node_id:
            return item
    return None


def _states(
    item: Optional[Mapping[str, Any]], subject: IssueOrPullRequestSnapshot
) -> tuple[ExpectedState, CurrentState]:
    """`expected` is the board as the event saw it; `current` is the board now."""
    record = dict(item or {})
    common = {
        "item_node_id": record.get("item_node_id") or record.get("id"),
        "content_node_id": _item_content_id(record) or (None if item else subject.node_id),
        "field_id": record.get("field_id"),
        "field_name": record.get("field_name"),
        "option_id": record.get("option_id"),
        "option_name": record.get("option_name"),
        "status": record.get("status"),
        "repository_head": record.get("repository_head"),
        "evidence_revision": record.get("evidence_revision"),
        "project_values": dict(record.get("project_values") or {}),
    }
    expected = ExpectedState(**common, updated_at=subject.observed_project_updated_at)
    current = CurrentState(**common, updated_at=record.get("updated_at"))
    return expected, current


def _operation(
    kind: str,
    expected: ExpectedState,
    current: CurrentState,
    desired: Mapping[str, Any],
    *,
    comparable: bool,
    desired_status: Optional[str] = None,
) -> PlannedOperation:
    decision = (
        compare_project_mutation(expected, current, desired_status=desired_status)
        if comparable
        else None
    )
    return PlannedOperation(
        kind=kind, expected=expected, current=current, desired=dict(desired), decision=decision
    )


def _finalize(plan: NormalizationPlan) -> NormalizationPlan:
    """Quarantine the whole plan if any single operation lost its authorization."""
    conflicted = tuple(
        operation
        for operation in plan.operations
        if operation.decision is not None and operation.decision.action != "apply"
    )
    if not conflicted:
        return plan
    return replace(
        plan,
        operations=(),
        quarantined=conflicted,
        blocked_reason="board-decision-newer",
        desired_status=None,
    )


# ───────────────────────────── intake ─────────────────────────────


def _route_body(raw_body: Optional[str], declared: Optional[str]) -> str:
    """Feed `resolve_branch_route` one canonical declaration line.

    The form renders the route as a section, not as a declaration, so the
    section value is appended as a declaration. A body that ALSO carries an
    inline declaration then produces two of them, which `routing.py` refuses as
    `route-declaration-duplicate` — exactly the right answer for a card that
    says two different things.
    """
    body = raw_body if isinstance(raw_body, str) else ""
    if declared:
        return f"{body}\nBranch route: {declared}"
    return body


def _desired_labels(
    current_labels: Sequence[str], form: IntakeForm, route: Optional[str]
) -> tuple[str, ...]:
    labels = {label for label in current_labels if isinstance(label, str) and label.strip()}
    if form.work_type and form.work_type.casefold() in WORK_TYPES:
        labels.add(form.work_type.casefold())
    if form.test_area:
        labels.add(f"{TEST_AREA_LABEL_PREFIX}{form.test_area.strip()}")
    if form.priority:
        labels.add(f"{PRIORITY_LABEL_PREFIX}{form.priority.strip()}")
    environment = canonical_environment_label(form.environment_constraint)
    if environment:
        labels.add(environment)
    if route == "staging-frankfurt":
        labels.add("branch:staging-frankfurt")
    return tuple(sorted(labels))


def _validate_form(form: IntakeForm, subject: IssueOrPullRequestSnapshot) -> Optional[str]:
    """The first thing that is not true about this intake, or None."""
    if form.missing:
        return f"intake-section-missing:{_section_slug(form.missing[0])}"
    if form.work_type and form.work_type.casefold() not in WORK_TYPES:
        return "intake-work-type-unknown"
    if form.priority and form.priority.strip().upper() not in PRIORITIES:
        return "intake-priority-unknown"

    route = resolve_branch_route(
        _RouteSubject(_route_body(subject.body, form.branch_route), subject.labels),
        _RouteConfig(),
    )
    if not route.valid:
        return route.reason_code or "route-declaration-missing"

    milestone = subject.milestone or form.milestone
    if not _clean(milestone):
        if form.blocked_reason is None:
            return "intake-milestone-or-blocked-reason-missing"
        if not _is_concrete_reason(form.blocked_reason):
            return "blocked-reason-not-concrete"
    return None


@dataclass(frozen=True)
class _RouteSubject:
    """The two attributes `resolve_branch_route` reads off an issue."""

    body: str
    labels: tuple[str, ...]


def _resolved_route(form: IntakeForm, subject: IssueOrPullRequestSnapshot) -> Optional[str]:
    route = resolve_branch_route(
        _RouteSubject(_route_body(subject.body, form.branch_route), subject.labels),
        _RouteConfig(),
    )
    return route.declaration if route.valid else None


def normalize_intake(
    issue: IssueOrPullRequestSnapshot, project: ProjectSnapshot
) -> NormalizationPlan:
    """Plan the normalization of one issue or pull-request event."""
    kind = "pull_request" if issue.is_pull_request else "issue"
    event = issue.event or ""
    base = NormalizationPlan(
        subject_url=issue.url,
        subject_kind=kind,
        event=event,
        normalized=False,
    )

    if not handles_event(kind, event):
        return replace(base, blocked_reason="event-not-normalized")
    base = replace(base, normalized=True)

    # A snapshot that hit the page cap is not "the board"; membership cannot be
    # decided from it, and an undecidable membership is not an absent card.
    if getattr(project, "hit_cap", False):
        return replace(base, blocked_reason="project-membership-unknown")

    items = tuple(project.items)
    item = find_project_item(items, issue.node_id)
    base = replace(
        base, membership_lookups=1, is_member=item is not None, membership_key="content_node_id"
    )
    expected, current = _states(item, issue)
    comparable = item is not None

    # Where the metadata comes from: an issue carries its own form; a pull
    # request inherits from exactly one unambiguous linked issue or nothing.
    inherited_from: Optional[str] = None
    if kind == "pull_request":
        linked = tuple(issue.linked_issues or ())
        if not linked:
            return _holding(base, "pull-request-linkage-missing", item, expected, current, comparable, issue)
        if len({entry.url for entry in linked}) > 1:
            return _holding(
                base, "pull-request-linkage-ambiguous", item, expected, current, comparable, issue
            )
        source = linked[0]
        form = parse_intake_form(source.body)
        failure = _validate_form(form, source)
        if failure is not None:
            return _holding(
                base,
                "pull-request-linked-intake-incomplete",
                item,
                expected,
                current,
                comparable,
                issue,
            )
        inherited_from = source.url
        route = _resolved_route(form, source)
        milestone = source.milestone or form.milestone
    else:
        source = issue
        form = parse_intake_form(issue.body)
        failure = _validate_form(form, issue)
        if failure is not None:
            return _holding(base, failure, item, expected, current, comparable, issue)
        route = _resolved_route(form, issue)
        milestone = issue.milestone or form.milestone

    base = replace(
        base,
        branch_route=route,
        milestone=_clean(milestone),
        milestone_source="repository",
        declared_blocked_reason=form.blocked_reason if _is_concrete_reason(form.blocked_reason) else None,
        inherited_from=inherited_from,
        classification=classify_pull_request(issue) if kind == "pull_request" else None,
    )

    operations: list[PlannedOperation] = []

    # Membership first: a field cannot be written on a card that is not on the
    # board, and adding it twice creates a duplicate nobody reconciles.
    if item is None:
        operations.append(
            _operation(
                "project-membership",
                expected,
                current,
                {"content_node_id": issue.node_id, "member": True},
                comparable=False,
            )
        )

    desired_labels = _desired_labels(issue.labels, form, route)
    if set(desired_labels) != {label for label in issue.labels if label}:
        operations.append(
            _operation(
                "labels",
                expected,
                current,
                {"labels": list(desired_labels)},
                comparable=comparable,
            )
        )

    declared_milestone = _clean(milestone)
    if declared_milestone and _clean(issue.milestone) != declared_milestone:
        operations.append(
            _operation(
                "repository-milestone",
                expected,
                current,
                {"milestone": declared_milestone, "target": "repository"},
                comparable=comparable,
            )
        )

    desired_status: Optional[str] = None
    if event == "reopened":
        desired_status = "Backlog"
    elif event == "opened" and not current.status:
        desired_status = "Backlog"
    if desired_status and current.status != desired_status and comparable:
        operations.append(
            _operation(
                "status",
                expected,
                current,
                {"status": desired_status},
                comparable=True,
                desired_status=desired_status,
            )
        )
    elif desired_status == current.status:
        desired_status = None

    if kind == "pull_request" and issue.qa_tested_sha and issue.head_sha != issue.qa_tested_sha:
        qa_expected = replace(expected, repository_head=issue.qa_tested_sha)
        qa_current = replace(current, repository_head=issue.qa_tested_sha)
        operations.append(
            _operation(
                "qa-invalidation",
                qa_expected,
                qa_current,
                {
                    "head_sha": issue.head_sha,
                    "invalidated": True,
                    "tested_sha": issue.qa_tested_sha,
                },
                comparable=comparable,
            )
        )

    return _finalize(
        replace(base, operations=tuple(operations), desired_status=desired_status)
    )


def _holding(
    base: NormalizationPlan,
    reason: str,
    item: Optional[Mapping[str, Any]],
    expected: ExpectedState,
    current: CurrentState,
    comparable: bool,
    issue: IssueOrPullRequestSnapshot,
) -> NormalizationPlan:
    """Refuse to promote, and demote a card that already escaped the holding columns."""
    status = current.status
    if (
        comparable
        and isinstance(status, str)
        and status in LIFECYCLE_STATUSES
        and status not in HOLDING_STATUSES
    ):
        operation = _operation(
            "status",
            expected,
            current,
            {"status": "Blocked", "reason": reason},
            comparable=True,
            desired_status="Blocked",
        )
        return _finalize(
            replace(
                base,
                operations=(operation,),
                blocked_reason=reason,
                desired_status="Blocked",
            )
        )
    return replace(base, operations=(), blocked_reason=reason, desired_status=None)


# ───────────────────────────── closure ─────────────────────────────


def _timestamp(value: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _linked(evidence: Any) -> bool:
    """Evidence somebody can open. A mapping with no link is a claim, not a link."""
    return isinstance(evidence, Mapping) and _clean(evidence.get("url")) is not None


def accepted_completion_evidence(evidence: Any) -> bool:
    """True only for typed, linked completion evidence."""
    if not _linked(evidence):
        return False
    return evidence.get("type") in ACCEPTED_COMPLETION_EVIDENCE_TYPES


def _issue_disposition(subject: IssueOrPullRequestSnapshot) -> Optional[str]:
    if accepted_completion_evidence(subject.completion_evidence):
        return "completed"
    if _clean(subject.duplicate_of):
        return "duplicate"
    if _is_concrete_reason(subject.not_planned_reason):
        return "not-planned"
    return None


def _pull_request_disposition(subject: IssueOrPullRequestSnapshot) -> Optional[str]:
    # A merge is the one disposition GitHub itself attests to — but only once
    # the readback carries the timestamp. A merge commit SHA on its own can be
    # a stale field on a pull request that was closed instead.
    if _clean(subject.merged_at):
        return "merged"
    if _linked(subject.supersession_evidence):
        return "superseded"
    if _linked(subject.abandonment_evidence):
        return "abandoned"
    return None


_ISSUE_CORRECTIVE_COMMENT = (
    "This issue was closed without an accepted disposition, so Superboard "
    "reopened it and moved it to Blocked rather than counting it as finished "
    "work.\n\n"
    "Subject: {url}\n"
    "Title: {title}\n\n"
    "Close it again with exactly one of:\n\n"
    "- accepted completion evidence — a linked, typed record "
    "({evidence_types});\n"
    "- a linked duplicate — the issue that survives;\n"
    "- a not planned decision that states, concretely, what was decided.\n\n"
    "Until one of those exists there is no way to tell this card apart from "
    "work that was actually built, tested, reviewed, and merged."
)

_PULL_REQUEST_CORRECTIVE_COMMENT = (
    "This pull request was closed without being merged and without a linked "
    "disposition, so Superboard moved its card to Blocked rather than counting "
    "it as finished work.\n\n"
    "Subject: {url}\n"
    "Title: {title}\n\n"
    "Record one of:\n\n"
    "- linked supersession evidence — the pull request that carried this work;\n"
    "- linked abandonment evidence — the issue or decision that dropped it.\n\n"
    "A closed branch with neither is unfinished work, not completed work."
)


def _corrective_comment(
    subject: IssueOrPullRequestSnapshot, environment: Mapping[str, str]
) -> Optional[str]:
    """Render the comment, then sanitize it. Never the other way round."""
    template = (
        _PULL_REQUEST_CORRECTIVE_COMMENT
        if subject.is_pull_request
        else _ISSUE_CORRECTIVE_COMMENT
    )
    rendered = render_payload(
        [
            template.format(
                url=subject.url or "(unknown subject)",
                title=subject.title or "(untitled)",
                evidence_types=", ".join(ACCEPTED_COMPLETION_EVIDENCE_TYPES),
            )
        ]
    )
    try:
        return sanitize_and_validate_publication(
            rendered, environment, surface="closure-comment"
        ).text
    except UnsafePublication:
        # Nothing is published, and nothing quotes the value that failed.
        return None


def normalize_closure(
    subject: IssueOrPullRequestSnapshot,
    project: ProjectSnapshot,
    *,
    activation_boundary: Optional[str] = None,
    environment: Optional[Mapping[str, str]] = None,
) -> NormalizationPlan:
    """Decide, from evidence, whether a closed subject belongs in the completion column."""
    kind = "pull_request" if subject.is_pull_request else "issue"
    event = subject.event or ""
    base = NormalizationPlan(
        subject_url=subject.url,
        subject_kind=kind,
        event=event,
        normalized=True,
        classification=classify_pull_request(subject) if kind == "pull_request" else None,
    )

    if getattr(project, "hit_cap", False):
        return replace(base, blocked_reason="project-membership-unknown")

    items = tuple(project.items)
    item = find_project_item(items, subject.node_id)
    base = replace(base, membership_lookups=1, is_member=item is not None)
    if item is None:
        return replace(base, blocked_reason="closure-card-not-on-board")

    expected, current = _states(item, subject)

    closed = (subject.state or "").strip().casefold() == "closed"
    boundary = _timestamp(activation_boundary)
    closed_at = _timestamp(subject.closed_at)
    pre_activation = bool(closed and boundary and closed_at and closed_at < boundary)

    disposition: Optional[str] = None
    desired_status: Optional[str] = None
    blocked_reason: Optional[str] = None
    reopen = False
    comment: Optional[str] = None

    if event == "reopened" or (not closed and event == "reopened"):
        disposition = "reopened"
        desired_status = "Backlog"
    elif not closed:
        if current.status == COMPLETION_STATUS:
            # An open card in the completion column is a board error, not a state.
            disposition = "open-in-completion-column"
            desired_status = "Backlog"
    elif pre_activation:
        # Its board status is corrected. Its evidence is not touched: writing
        # modern acceptance evidence onto a historical closure would be
        # manufacturing a record of a review that never happened.
        disposition = "pre-activation-historical"
        desired_status = COMPLETION_STATUS
    else:
        disposition = (
            _pull_request_disposition(subject)
            if kind == "pull_request"
            else _issue_disposition(subject)
        )
        if disposition is not None:
            desired_status = COMPLETION_STATUS
        else:
            desired_status = "Blocked"
            blocked_reason = (
                "pull-request-disposition-missing"
                if kind == "pull_request"
                else "closure-disposition-missing"
            )
            # A closed pull request is never reopened by the runtime: the branch
            # is the author's, and reopening it would re-enter a review nobody
            # asked for. An issue is reopened, because the work is not done.
            reopen = kind == "issue"
            comment = _corrective_comment(subject, environment or {})
            if comment is None:
                blocked_reason = "closure-comment-unsafe"

    operations: list[PlannedOperation] = []
    if reopen:
        operations.append(
            _operation("reopen", expected, current, {"state": "open"}, comparable=True)
        )
    if desired_status and current.status != desired_status:
        operations.append(
            _operation(
                "status",
                expected,
                current,
                {"status": desired_status},
                comparable=True,
                desired_status=desired_status,
            )
        )
    if comment is not None:
        operations.append(
            _operation(
                "closure-comment",
                expected,
                current,
                {"body": comment, "surface": "closure-comment"},
                comparable=True,
            )
        )

    return _finalize(
        replace(
            base,
            operations=tuple(operations),
            desired_status=desired_status,
            blocked_reason=blocked_reason,
            disposition=disposition,
            reopen=reopen,
            comment=comment,
            pre_activation=pre_activation,
            evidence_preserved=True,
        )
    )


__all__ = [
    "ACCEPTED_COMPLETION_EVIDENCE_TYPES",
    "CLOSURE_DISPOSITIONS",
    "COMPLETION_STATUS",
    "ENVIRONMENT_CONSTRAINT_LABEL",
    "HOLDING_STATUSES",
    "INTAKE_ISSUE_EVENTS",
    "INTAKE_PULL_REQUEST_EVENTS",
    "LEGACY_ENVIRONMENT_ALIASES",
    "PERIODIC_SWEEP_EVENT",
    "PRIORITIES",
    "PRIORITY_LABEL_PREFIX",
    "REQUIRED_INTAKE_SECTIONS",
    "REQUIRED_PULL_REQUEST_CLASSIFICATION_FIELDS",
    "TEST_AREA_LABEL_PREFIX",
    "WORK_TYPES",
    "IntakeForm",
    "IssueOrPullRequestSnapshot",
    "IssueSnapshot",
    "NormalizationError",
    "NormalizationPlan",
    "PlannedOperation",
    "PullRequestClassification",
    "accepted_completion_evidence",
    "canonical_environment_label",
    "classify_pull_request",
    "find_project_item",
    "handles_event",
    "normalize_closure",
    "normalize_intake",
    "parse_intake_form",
]
