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

#: The ONE query every caller uses to read a board's items.
#:
#: A Superboard is owned by a user OR by an organization — the Master board at
#: https://github.com/users/Wladefant/projects/5 is user-owned and the product
#: board at https://github.com/orgs/Bavariance/projects/1 is organization-owned
#: — and `user(login:)` resolves to null for the second one. A null owner reads
#: as a board with no items, which is indistinguishable from a board that lost
#: every card, so continuous intake would plan nothing against exactly the board
#: it exists to run on and report success doing it.
#:
#: `repositoryOwner` resolves either kind, and `projectV2` is selected through an
#: inline fragment per concrete type: `projectV2` lives on `User` and on
#: `Organization`, not on the `RepositoryOwner` interface. No owner-type input
#: is asked for, because an input is a value an operator can get wrong and this
#: needs no value at all.
PROJECT_ITEMS_QUERY = """query($owner: String!, $number: Int!, $endCursor: String) {
  repositoryOwner(login: $owner) {
    __typename
    ... on User { ...superboardItems }
    ... on Organization { ...superboardItems }
  }
}
fragment superboardItems on ProjectV2Owner {
  projectV2(number: $number) {
    id
    items(first: 100, after: $endCursor) {
      nodes {
        id
        updatedAt
        content { ... on Issue { id url } ... on PullRequest { id url } }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

#: The repository variable that arms the fallback auto-add workflow, and the
#: exact value it must hold. Anything else leaves the workflow inert.
FALLBACK_ENABLE_VARIABLE = "ENABLE_ADD_TO_PROJECT"
FALLBACK_ENABLE_VALUE = "true"


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


def project_pages_from_graphql(raw: Any) -> list[dict[str, Any]]:
    """Turn `gh api graphql --paginate --slurp` output into walkable pages.

    Every refusal here is the same refusal: we could not read the board, and an
    unreadable board is never an empty one. A null `repositoryOwner`, a null
    `projectV2`, or a GraphQL `errors` array all halt instead of yielding a
    smaller snapshot — the failure this exists to prevent is a board that reads
    as drained because the query asked the wrong owner type.
    """
    responses = raw if isinstance(raw, list) else [raw]
    pages: list[dict[str, Any]] = []
    for response in responses:
        if not isinstance(response, Mapping):
            raise MutationConflict(
                "project-snapshot-incomplete", "the Project inventory response was unreadable"
            )
        if response.get("errors"):
            raise MutationConflict(
                "project-snapshot-incomplete",
                "the Project inventory query returned errors; refusing to act on a partial "
                "snapshot",
            )
        owner = (response.get("data") or {}).get("repositoryOwner")
        if not isinstance(owner, Mapping):
            raise MutationConflict(
                "project-owner-unresolved",
                "the board owner did not resolve — check the owner login, and note that a "
                "board may be owned by a user OR by an organization",
            )
        project = owner.get("projectV2")
        if not isinstance(project, Mapping):
            raise MutationConflict(
                "project-not-found",
                f"owner {owner.get('__typename') or 'unknown'} resolved but carries no such "
                "Project; an absent board is not an empty one",
            )
        items = project.get("items") or {}
        nodes = items.get("nodes") if isinstance(items, Mapping) else None
        pages.append(
            {
                "items": [
                    {
                        "item_node_id": node.get("id"),
                        "content_node_id": (node.get("content") or {}).get("id"),
                        "content_url": (node.get("content") or {}).get("url"),
                        "updated_at": node.get("updatedAt"),
                    }
                    for node in (nodes or [])
                    if isinstance(node, Mapping)
                ],
                "fields": {},
                "pageInfo": (
                    items.get("pageInfo")
                    if isinstance(items, Mapping) and isinstance(items.get("pageInfo"), Mapping)
                    else {"hasNextPage": False}
                ),
            }
        )
    if not pages:
        raise MutationConflict(
            "project-snapshot-incomplete", "the Project inventory response carried no pages"
        )
    return pages


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


# ───────────────────────────── fallback auto-add guard ─────────────────────────────


@dataclass(frozen=True)
class FallbackDecision:
    """Whether the redundant auto-add workflow may insert a card, and why not."""

    insert: bool
    reason_code: str
    membership_key: str = "content_node_id"
    preflight: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "insert": self.insert,
            "membership_key": self.membership_key,
            "preflight": list(self.preflight),
            "reason_code": self.reason_code,
        }


def _membership(project: Any, issue_node_id: str) -> Optional[bool]:
    """True member, False absent, None undecidable. Never guesses."""
    if project is None or getattr(project, "hit_cap", False):
        # A page-capped snapshot is not the board. "Not found in the pages we
        # managed to read" is not the same as "not on the board".
        return None
    try:
        items = tuple(project.items)
    except Exception:
        return None
    matches = 0
    for item in items:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content_node_id") or item.get("contentNodeId")
        if not content and isinstance(item.get("content"), Mapping):
            content = item["content"].get("id")
        if content == issue_node_id:
            matches += 1
    if matches > 1:
        # Two cards for one issue is the failure this guard exists to prevent.
        # Adding a third is not the fix.
        return None
    return matches == 1


def evaluate_fallback_auto_add(
    issue_node_id: Any,
    project: Any,
    enabled: bool,
    *,
    identity_check: Optional[Callable[[], Any]] = None,
    quota_check: Optional[Callable[[], Any]] = None,
) -> FallbackDecision:
    """Decide whether the fallback may insert one card.

    Every uncertain answer resolves to "do not insert". The workflow is a
    redundant backup for a built-in feature; the cost of not inserting is one
    card an operator adds by hand, and the cost of inserting wrongly is a
    duplicate card that two workflows then fight over.
    """
    if not enabled:
        return FallbackDecision(False, "fallback-disabled")
    if not isinstance(issue_node_id, str) or not issue_node_id.strip():
        return FallbackDecision(False, "issue-node-id-invalid")

    member = _membership(project, issue_node_id)
    if member is None:
        return FallbackDecision(False, "membership-unknown")
    if member:
        return FallbackDecision(False, "already-member")

    # Only now, with an insertion actually in prospect, does the preflight run.
    consulted: list[str] = []
    consulted.append("identity")
    try:
        verified = bool(identity_check()) if identity_check is not None else False
    except Exception:
        verified = False
    if not verified:
        return FallbackDecision(False, "identity-unverified", preflight=tuple(consulted))

    consulted.append("quota")
    if quota_check is None:
        return FallbackDecision(False, "quota-unavailable", preflight=tuple(consulted))
    try:
        quota_check()
    except Exception:
        return FallbackDecision(False, "quota-unavailable", preflight=tuple(consulted))

    return FallbackDecision(True, "insert-authorized", preflight=tuple(consulted))


__all__ = [
    "FALLBACK_ENABLE_VALUE",
    "FALLBACK_ENABLE_VARIABLE",
    "MAX_PROJECT_PAGES",
    "PROJECT_ITEMS_QUERY",
    "CurrentState",
    "ExpectedState",
    "FallbackDecision",
    "MutationConflict",
    "MutationDecision",
    "ProjectSnapshot",
    "apply_project_mutation",
    "compare_project_mutation",
    "evaluate_fallback_auto_add",
    "project_pages_from_graphql",
    "snapshot_project",
]
