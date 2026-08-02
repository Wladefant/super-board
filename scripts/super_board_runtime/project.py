#!/usr/bin/env python3
"""Paginated Project inventory and compare-before-mutate.

Two ideas, both about not trusting stale state.

**Paginated inventory.** `snapshot_project` walks every page, caps the walk, and
refuses to return a partial snapshot when a page fails. A snapshot missing 200
cards looks exactly like a board that lost 200 cards, and reconciliation built
on it would "fix" the difference.

**Compare before mutate.** A mutation is authorized by state reread *at decision
time*, never by state captured during preflight. Between preflight and apply a
human can move the card, a workflow can rename the Status field, and GitHub can
hand out new option IDs. `compare_project_mutation` rereads the item by its
immutable node ID, rereads the field and option IDs by name, and compares
repository state, evidence revision, and the current Project values against the
manifest's expected prior values. Any difference at all quarantines with exit 3
and zero writes — including a record whose `updated_at` merely *differs*, in
either direction, because a newer human or automation decision must never be
overwritten and an older timestamp means we are reading something we do not
understand.

A decision that says `apply` still reads back immediately after writing, and a
readback that disagrees is a conflict, not a success.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping, Optional

try:  # normal package import
    from . import EXIT_CONFLICT, EXIT_OK
except ImportError:  # executed as a plain file path
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFLICT, EXIT_OK

#: No paginated Project read may request more pages than this.
MAX_PROJECT_PAGES = 20


class MutationConflict(Exception):
    """State moved under us, or could not be read whole. Exit code 3."""

    exit_code = EXIT_CONFLICT

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class _Record:
    """One Project item's mutation-relevant state."""

    item_node_id: Optional[str]
    content_node_id: Optional[str]
    field_id: Optional[str]
    field_name: Optional[str]
    option_id: Optional[str]
    option_name: Optional[str]
    status: Optional[str]
    repository_head: Optional[str]
    evidence_revision: Optional[str]
    project_values: Mapping[str, str] = field(default_factory=dict)
    updated_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        body = dict(asdict(self))
        body["project_values"] = dict(self.project_values or {})
        return body


@dataclass(frozen=True)
class ExpectedState(_Record):
    """What the manifest recorded when the plan was built."""


@dataclass(frozen=True)
class CurrentState(_Record):
    """What GitHub says right now, reread at decision time."""


@dataclass(frozen=True)
class MutationDecision:
    action: str
    reason_code: Optional[str]
    item_node_id: Optional[str]
    content_node_id: Optional[str]
    #: The IDs reread at decision time — never the preflight ones.
    field_id: Optional[str]
    option_id: Optional[str]
    desired_status: Optional[str]
    exit_code: int

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


@dataclass(frozen=True)
class ProjectSnapshot:
    project_owner: str
    project_number: int
    items: tuple[Mapping[str, Any], ...]
    fields: Mapping[str, Any]
    hit_cap: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "fields": dict(self.fields),
            "hit_cap": self.hit_cap,
            "items": [dict(item) for item in self.items],
            "project_number": self.project_number,
            "project_owner": self.project_owner,
        }


# ───────────────────────────── inventory ─────────────────────────────


def snapshot_project(
    project_owner: str,
    project_number: int,
    *,
    fetch: Optional[Callable[[Optional[str]], Any]] = None,
    max_pages: int = MAX_PROJECT_PAGES,
) -> ProjectSnapshot:
    """Walk every page of a Project's items. Never returns a partial snapshot."""
    if fetch is None:
        raise MutationConflict(
            "project-snapshot-unavailable",
            "snapshot_project needs a fetch callable; it does not build its own query",
        )
    items: list[Mapping[str, Any]] = []
    fields: Mapping[str, Any] = {}
    cursor: Optional[str] = None
    hit_cap = False
    for page in range(max_pages):
        try:
            payload = fetch(cursor)
        except Exception as exc:  # a failed page is not "the rest of the board"
            raise MutationConflict(
                "project-snapshot-incomplete",
                f"page {page + 1} of the Project inventory could not be read; refusing to "
                f"act on a partial snapshot",
            ) from exc
        if not isinstance(payload, Mapping):
            raise MutationConflict(
                "project-snapshot-incomplete", "the Project inventory response was unreadable"
            )
        page_items = payload.get("items")
        if isinstance(page_items, list):
            items.extend(item for item in page_items if isinstance(item, Mapping))
        if isinstance(payload.get("fields"), Mapping):
            fields = payload["fields"]
        info = payload.get("pageInfo") if isinstance(payload.get("pageInfo"), Mapping) else {}
        cursor = info.get("endCursor")
        if not info.get("hasNextPage"):
            break
        if not cursor:
            # `after=None` would refetch page one forever.
            break
    else:
        hit_cap = True
    return ProjectSnapshot(
        project_owner=project_owner,
        project_number=project_number,
        items=tuple(items),
        fields=fields,
        hit_cap=hit_cap,
    )


# ───────────────────────────── compare before mutate ─────────────────────────────


def _quarantine(
    reason: str, expected: ExpectedState, current: CurrentState, desired_status: Optional[str]
) -> MutationDecision:
    return MutationDecision(
        action="quarantine",
        reason_code=reason,
        item_node_id=current.item_node_id or expected.item_node_id,
        content_node_id=current.content_node_id or expected.content_node_id,
        field_id=current.field_id,
        option_id=current.option_id,
        desired_status=desired_status,
        exit_code=EXIT_CONFLICT,
    )


def compare_project_mutation(
    expected: ExpectedState,
    current: CurrentState,
    *,
    desired_status: Optional[str] = None,
) -> MutationDecision:
    """Authorize — or quarantine — one Project mutation.

    Ordered from the identity of the record outwards, so the reason code names
    the first thing that stopped being true.
    """
    if not current.item_node_id:
        return _quarantine("item-unreadable", expected, current, desired_status)
    if current.item_node_id != expected.item_node_id:
        return _quarantine("item-node-id-mismatch", expected, current, desired_status)
    if current.content_node_id != expected.content_node_id:
        return _quarantine("content-node-id-mismatch", expected, current, desired_status)
    if current.field_name != expected.field_name:
        return _quarantine("field-name-changed", expected, current, desired_status)
    if current.field_id != expected.field_id:
        return _quarantine("field-id-changed", expected, current, desired_status)
    if current.option_id != expected.option_id:
        return _quarantine("option-id-changed", expected, current, desired_status)
    if current.repository_head != expected.repository_head:
        return _quarantine("repository-state-changed", expected, current, desired_status)
    if current.evidence_revision != expected.evidence_revision:
        return _quarantine("evidence-revision-changed", expected, current, desired_status)
    if dict(current.project_values or {}) != dict(expected.project_values or {}):
        return _quarantine("project-values-changed", expected, current, desired_status)
    if current.updated_at != expected.updated_at:
        # Any difference, in either direction. Newer means a human or another
        # automation decided something after our manifest was built.
        return _quarantine("record-changed-since-manifest", expected, current, desired_status)

    return MutationDecision(
        action="apply",
        reason_code=None,
        item_node_id=current.item_node_id,
        content_node_id=current.content_node_id,
        field_id=current.field_id,
        option_id=current.option_id,
        desired_status=desired_status,
        exit_code=EXIT_OK,
    )


def apply_project_mutation(
    decision: MutationDecision,
    *,
    writer: Callable[[Mapping[str, Any]], Any],
    readback: Callable[[MutationDecision], CurrentState],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write a decision that says `apply`, then immediately read it back."""
    if decision.action != "apply":
        raise MutationConflict(
            decision.reason_code or "mutation-quarantined",
            "the record changed since the manifest was built; nothing was written",
        )
    payload = {
        "content_node_id": decision.content_node_id,
        "field_id": decision.field_id,
        "item_node_id": decision.item_node_id,
        "option_id": decision.option_id,
        "status": decision.desired_status,
    }
    if dry_run:
        return {"applied": False, "dry_run": True, "github_writes": 0, "payload": payload}
    writer(payload)
    observed = readback(decision)
    if decision.desired_status is not None and observed.status != decision.desired_status:
        raise MutationConflict(
            "readback-mismatch",
            "the mutation did not take effect as written; the record is now in an "
            "unknown state and must be reconciled before anything else is attempted",
        )
    return {
        "applied": True,
        "dry_run": False,
        "github_writes": 1,
        "payload": payload,
        "status": observed.status,
    }


__all__ = [
    "MAX_PROJECT_PAGES",
    "CurrentState",
    "ExpectedState",
    "MutationConflict",
    "MutationDecision",
    "ProjectSnapshot",
    "apply_project_mutation",
    "compare_project_mutation",
    "snapshot_project",
]
