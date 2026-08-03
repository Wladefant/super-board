#!/usr/bin/env python3
"""Dispatch eligibility and excluded-label enforcement.

One implementation serves every dispatcher path — the read-only planner
(`super-board-wave-plan.sh`), the headless dispatcher (`super-board-run.sh`),
and the dynamic workflow (`workflows/super-board-wave.js`) — so the same card
cannot be eligible in one and ineligible in another.

The rules, in the order they are applied:

1. Only issue cards dispatch. Pull-request and draft cards never do.
2. `design` and `history` are permanently non-dispatchable, plus whatever the
   config lists in `exclude_labels`. Comparison is case-insensitive after
   trimming.
3. The card's lifecycle status must be **exactly `Ready`**. There is no
   "eligible for the requested lane" concept.
4. A card carrying an assignee is already claimed.
5. The issue must be OPEN. A failed or missing state lookup is
   `issue-state-unavailable` — never a permissive fallback.
6. Branch routes must be unambiguous.
7. Only then is activation consulted (see `activation.py`).

Nothing in this module writes to GitHub. Rejections short-circuit before the
state lookup, so an excluded or non-`Ready` card costs no API call at all.

CLI:

    python -m super_board_runtime.eligibility --items - --config <config.json>

Machine-readable JSON on stdout, diagnostics on stderr.
Exit 0 success, 64 invalid invocation, 65 invalid configuration or input.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

try:  # normal package import
    from . import EXIT_CONFIG, EXIT_OK, EXIT_USAGE
    from .activation import evaluate_activation
    from .config import ConfigError, NormalizedConfig, load_and_validate_config
    from .lifecycle import DISPATCHABLE_STATUS
    from .publication import redact_for_display
    from .routing import resolve_branch_route
except ImportError:  # executed as a plain file path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFIG, EXIT_OK, EXIT_USAGE
    from super_board_runtime.activation import evaluate_activation
    from super_board_runtime.config import ConfigError, NormalizedConfig, load_and_validate_config
    from super_board_runtime.lifecycle import DISPATCHABLE_STATUS
    from super_board_runtime.publication import redact_for_display
    from super_board_runtime.routing import resolve_branch_route

StateLookup = Callable[["IssueSnapshot"], Optional[str]]

#: How much of an issue title a dispatch plan carries. Enough to classify a
#: card by; not enough for a pasted wall of text to become the plan.
PLAN_TITLE_LIMIT = 160


@dataclass(frozen=True)
class IssueSnapshot:
    """Everything eligibility needs to know about one board card."""

    url: Optional[str]
    node_id: Optional[str]
    number: Optional[int]
    content_type: Optional[str]
    state: Optional[str]
    title: Optional[str]
    body: Optional[str]
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    status: Optional[str]
    milestone: Optional[str]


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reason_codes: tuple[str, ...]
    issue_number: Optional[int]
    issue_node_id: Optional[str]
    issue_url: Optional[str]
    selected_base_branch: Optional[str]
    branch_declaration: Optional[str]
    activation_mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "eligible": self.eligible,
            "reason_codes": list(self.reason_codes),
            "issue_number": self.issue_number,
            "issue_node_id": self.issue_node_id,
            "issue_url": self.issue_url,
            "selected_base_branch": self.selected_base_branch,
            "branch_declaration": self.branch_declaration,
            "activation_mode": self.activation_mode,
        }


@dataclass(frozen=True)
class DispatchPlan:
    cards: tuple[dict[str, Any], ...]
    decisions: tuple[EligibilityDecision, ...]


@dataclass(frozen=True)
class BoardCoverage:
    """How much of the board this plan actually looked at.

    A plan is a statement about a board, and a statement about a board nobody
    finished reading is worth exactly as much as the part that was read. The
    live planner used to fetch `gh project item-list --limit 500` and say
    nothing: on a 591-card board it reported 500 decisions, and the 91 cards it
    never evaluated were indistinguishable from 91 cards it evaluated and
    rejected. Erring conservative is not the same as erring silently — a board
    that grows past the cap stops dispatching its tail and no output says so.

    `items_total` is `None` when the board's declared size could not be read. In
    that case a page that came back exactly full is the only evidence available
    and is reported as truncated, because assuming otherwise is the failure this
    record exists to prevent.
    """

    items_seen: int
    items_total: Optional[int]
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "items_seen": self.items_seen,
            "items_total": self.items_total,
            "truncated": self.truncated,
        }


def board_coverage(
    items_seen: int,
    *,
    items_total: Optional[int] = None,
    items_limit: Optional[int] = None,
) -> BoardCoverage:
    """Decide whether the scan was complete, and never guess that it was."""
    seen = max(int(items_seen), 0)
    if items_total is not None:
        total = max(int(items_total), 0)
        return BoardCoverage(seen, total, seen < total)
    if items_limit is not None:
        return BoardCoverage(seen, None, seen >= max(int(items_limit), 0))
    # No cap was applied and no total was declared: the caller handed over the
    # whole payload it had, which is all this layer can know.
    return BoardCoverage(seen, seen, False)


# ───────────────────────────── snapshot parsing ─────────────────────────────


def _names(raw: Any) -> tuple[str, ...]:
    """Normalize a GitHub label/assignee collection to a tuple of names."""
    if not isinstance(raw, (list, tuple)):
        return ()
    names: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            text = entry
        elif isinstance(entry, dict):
            text = entry.get("name") or entry.get("login") or ""
        else:
            text = ""
        if isinstance(text, str) and text.strip():
            names.append(text.strip())
    return tuple(names)


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def snapshot_from_project_item(item: Any) -> IssueSnapshot:
    """Build a snapshot from one `gh project item-list --format json` item.

    Labels and assignees live at the item level in some `gh` versions and under
    `content` in others; both are accepted. Missing keys are never fatal.
    """
    if not isinstance(item, dict):
        item = {}
    content = item.get("content")
    if not isinstance(content, dict):
        content = {}
    number = _first(content.get("number"), item.get("number"))
    if isinstance(number, str) and number.isdigit():
        number = int(number)
    if not isinstance(number, int):
        number = None
    milestone = _first(content.get("milestone"), item.get("milestone"))
    if isinstance(milestone, dict):
        milestone = milestone.get("title")
    return IssueSnapshot(
        url=_first(content.get("url"), item.get("url")),
        node_id=_first(content.get("id"), content.get("node_id"), item.get("id")),
        number=number,
        content_type=_first(content.get("type"), item.get("type")),
        state=_first(content.get("state"), item.get("state")),
        title=_first(content.get("title"), item.get("title")),
        body=_first(content.get("body"), item.get("body")),
        labels=_names(_first(item.get("labels"), content.get("labels"), [])),
        assignees=_names(_first(content.get("assignees"), item.get("assignees"), [])),
        status=_first(item.get("status"), content.get("status")),
        milestone=milestone if isinstance(milestone, str) else None,
    )


# ───────────────────────────── the decision ─────────────────────────────


def _resolve_branch(
    issue: IssueSnapshot, config: NormalizedConfig
) -> tuple[Optional[str], Optional[str]]:
    """Return (branch, reason_code). A reason code means the card is ineligible.

    Routing is delegated in full to `routing.resolve_branch_route`, which is the
    only authority on which base branch a card gets and whether it is allowed at
    all. The issue must carry exactly one explicit, normalized `Branch route:`
    declaration whose redundant label agrees with it; missing, `default`,
    unknown, duplicated, and conflicting declarations are ineligible and fail
    here — before any branch is created.

    This module used to carry a SECOND, more permissive implementation: a single
    route label selected its branch whatever it named, and a card with no label
    at all inherited `config.base_branch`. The two implementations disagreed
    about the same card — a `route:main` card was eligible here while the
    routing layer refused it as `route-declaration-unknown` — and the permissive
    one is the one that would have handed a worker its base branch. There is now
    one implementation, so the layers cannot drift apart again.
    """
    route = resolve_branch_route(issue, config)
    if not route.valid:
        return None, route.reason_code or "route-declaration-missing"
    return route.base_branch, None


def _decision(
    issue: IssueSnapshot,
    config: NormalizedConfig,
    reason_codes: tuple[str, ...],
    branch: Optional[str],
) -> EligibilityDecision:
    return EligibilityDecision(
        eligible=not reason_codes,
        reason_codes=reason_codes,
        issue_number=issue.number,
        issue_node_id=issue.node_id,
        issue_url=issue.url,
        selected_base_branch=branch,
        branch_declaration=branch,
        activation_mode=config.activation_mode,
    )


def evaluate_dispatch(
    issue: IssueSnapshot,
    config: NormalizedConfig,
    *,
    state_lookup: Optional[StateLookup] = None,
) -> EligibilityDecision:
    """Decide whether one card may be dispatched. Fails closed, always."""
    branch, route_reason = _resolve_branch(issue, config)

    # Activation is consulted first so its mode is on every decision record and
    # every run-evidence row. Its reason code is only *reported* once the card
    # has cleared the card-intrinsic gates below, so a Backlog card reads
    # ("status-not-ready",) rather than blaming activation for a card that was
    # never dispatchable in the first place.
    activation = evaluate_activation(issue, config)

    content_type = (issue.content_type or "").strip().casefold()
    if content_type != "issue":
        return _decision(issue, config, ("content-type-not-issue",), branch)

    excluded = set(config.exclude_labels)
    if any(label.strip().casefold() in excluded for label in issue.labels):
        return _decision(issue, config, ("excluded-label",), branch)

    # Exactly `Ready`, compared byte for byte. `canonicalize_status` exists for
    # schema and alias validation — where a human-authored config or a board
    # option is being checked against the lifecycle — and folding it into this
    # gate silently widened the invariant: `ready`, ` Ready `, and `READY` all
    # dispatched. The dispatch gate is the one place that must not be lenient.
    if issue.status != DISPATCHABLE_STATUS:
        return _decision(issue, config, ("status-not-ready",), branch)

    if issue.assignees:
        return _decision(issue, config, ("already-claimed",), branch)

    state = issue.state
    if not (isinstance(state, str) and state.strip()) and state_lookup is not None:
        try:
            state = state_lookup(issue)
        except Exception:  # a failed lookup is ineligible, never permissive
            state = None
    if not (isinstance(state, str) and state.strip()):
        return _decision(issue, config, ("issue-state-unavailable",), branch)
    if state.strip().upper() != "OPEN":
        return _decision(issue, config, ("issue-not-open",), branch)

    if route_reason is not None:
        return _decision(issue, config, (route_reason,), branch)

    if not activation.permitted:
        return _decision(
            issue, config, (activation.reason_code or "activation-refused",), branch
        )

    return _decision(issue, config, (), branch)


def plan_dispatch(
    items: Iterable[Any],
    config: NormalizedConfig,
    *,
    state_lookup: Optional[StateLookup] = None,
    limit: Optional[int] = None,
) -> DispatchPlan:
    """Evaluate every board item and return the cards that may be dispatched.

    Board order is preserved. The cap defaults to `config.max_workers`.
    """
    cap = config.max_workers if limit is None else limit
    decisions: list[EligibilityDecision] = []
    cards: list[dict[str, Any]] = []
    for item in items:
        snapshot = (
            item if isinstance(item, IssueSnapshot) else snapshot_from_project_item(item)
        )
        decision = evaluate_dispatch(snapshot, config, state_lookup=state_lookup)
        decisions.append(decision)
        if decision.eligible and (cap is None or len(cards) < cap):
            cards.append(
                {
                    "number": snapshot.number,
                    "status": DISPATCHABLE_STATUS,
                    # The title is GitHub-controlled text, and this plan is
                    # serialized into run manifests, dispatcher logs, and the
                    # workflow's agent prompts. It is carried — a classifier
                    # reads it — but never raw.
                    "title": redact_for_display(snapshot.title, limit=PLAN_TITLE_LIMIT),
                    "issue_url": decision.issue_url,
                    "issue_node_id": decision.issue_node_id,
                    "selected_base_branch": decision.selected_base_branch,
                    "branch_declaration": decision.branch_declaration,
                    "activation_mode": decision.activation_mode,
                }
            )
    return DispatchPlan(cards=tuple(cards), decisions=tuple(decisions))


# ───────────────────────────── gh state lookup ─────────────────────────────


def gh_issue_state_lookup(config: NormalizedConfig) -> StateLookup:
    """Resolve an issue's state with `gh issue view`. Returns None on any failure."""

    def lookup(issue: IssueSnapshot) -> Optional[str]:
        if issue.number is None:
            return None
        command = ["gh", "issue", "view", str(issue.number), "--json", "state", "-q", ".state"]
        if config.repo_remote:
            command += ["--repo", config.repo_remote]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    return lookup


# ───────────────────────────── CLI ─────────────────────────────


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(f"super-board-eligibility: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="super_board_runtime.eligibility", description=__doc__)
    parser.add_argument("--config", required=True, help="path to the config JSON")
    parser.add_argument(
        "--items",
        required=True,
        help="path to a `gh project item-list --format json` payload, or - for stdin",
    )
    parser.add_argument("--limit", type=int, default=None, help="override config.max_workers")
    parser.add_argument(
        "--items-total",
        type=int,
        default=None,
        help="the board's declared item count, when the caller could read it",
    )
    parser.add_argument(
        "--items-limit",
        type=int,
        default=None,
        help="the cap the items payload was fetched with, when one was applied",
    )
    parser.add_argument(
        "--state-lookup",
        choices=("gh", "none"),
        default="gh",
        help="how to resolve an issue state the board payload does not carry",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_and_validate_config(Path(args.config))
    except ConfigError as exc:
        print(f"super-board-eligibility: invalid config: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True), file=sys.stderr)
        return EXIT_CONFIG

    raw = sys.stdin.read() if args.items == "-" else Path(args.items).read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"super-board-eligibility: items payload is not valid JSON: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": "items-not-json"}, sort_keys=True), file=sys.stderr)
        return EXIT_CONFIG
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        print("super-board-eligibility: items payload must be a list or {items:[...]}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": "items-invalid"}, sort_keys=True), file=sys.stderr)
        return EXIT_CONFIG

    coverage = board_coverage(
        len(items), items_total=args.items_total, items_limit=args.items_limit
    )
    if coverage.truncated:
        # Loud, on stderr, in the same run that produced the plan. A bounded
        # scan that is not announced is a plan nobody can size.
        missing = (
            f"{coverage.items_total - coverage.items_seen} card(s) were never evaluated"
            if coverage.items_total is not None
            else "an unknown number of cards were never evaluated"
        )
        total = coverage.items_total if coverage.items_total is not None else "unknown"
        print(
            f"super-board-eligibility: BOARD SCAN TRUNCATED — {coverage.items_seen} of "
            f"{total} items considered; {missing}.",
            file=sys.stderr,
        )

    lookup = gh_issue_state_lookup(config) if args.state_lookup == "gh" else None
    plan = plan_dispatch(items, config, state_lookup=lookup, limit=args.limit)
    print(
        json.dumps(
            {
                "activation_mode": config.activation_mode,
                "base_branch": config.base_branch,
                "cards": list(plan.cards),
                "coverage": coverage.to_dict(),
                "decisions": [decision.to_dict() for decision in plan.decisions],
                "exclude_labels": list(config.exclude_labels),
                "max_workers": config.max_workers,
            },
            sort_keys=True,
        )
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
