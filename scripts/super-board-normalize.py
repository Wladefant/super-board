#!/usr/bin/env python3
"""super-board-normalize.py — continuous intake and closure normalization.

    intake    plan the normalization of one issue or pull-request event
    closure   decide, from evidence, where a closed subject belongs

The plan is machine-readable and is never applied here. Project card adds and
Project status changes are top-level orchestration: an operator session holds
the Projects credential and performs them against this plan. That is also why
the CLI refuses to guess at Project state — a plan built without a complete,
paginated Project snapshot would decide membership from nothing, and an
undecidable membership is not an absent card.

Usage:
    super-board-normalize.py intake  --issue URL   [--event EVENT]
                                     --payload FILE [--json]
    super-board-normalize.py closure --subject URL [--event EVENT]
                                     [--activation-boundary RFC3339]
                                     --payload FILE [--json]

`closure` never manufactures evidence. A subject closed before the declared
activation boundary keeps its original evidence untouched; only its board
status is corrected.

`--payload` carries a pre-fetched `{"subject": {...}, "project": {...}}`
document: the subject as the event delivered it, and a complete Project
snapshot as produced by `super-board-project.py snapshot`. Without it the
command fails closed rather than normalizing against a partial board.

Exit: 0 the plan was produced (a blocked intake is a successful classification,
      not an error) · 3 compare-before-mutate conflict; nothing was planned ·
      64 invalid invocation · 65 invalid input or a missing Project snapshot.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime import EXIT_CONFIG, EXIT_CONFLICT, EXIT_OK, EXIT_USAGE  # noqa: E402
from super_board_runtime.normalize import (  # noqa: E402
    IssueOrPullRequestSnapshot,
    NormalizationError,
    normalize_closure,
    normalize_intake,
)
from super_board_runtime.project import ProjectSnapshot  # noqa: E402

_SNAPSHOT_FIELDS = frozenset(IssueOrPullRequestSnapshot.__dataclass_fields__)
_TUPLE_FIELDS = ("labels", "reviews", "checks")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(f"super-board-normalize: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="super-board-normalize.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    intake = sub.add_parser("intake", help="plan the normalization of one event")
    intake.add_argument("--issue", required=True, help="the issue or pull-request URL")

    closure = sub.add_parser("closure", help="decide where a closed subject belongs")
    closure.add_argument("--subject", required=True, help="the issue or pull-request URL")
    closure.add_argument(
        "--activation-boundary",
        default=None,
        help="RFC3339 instant before which closures are historical and evidence is preserved",
    )

    for command in (intake, closure):
        command.add_argument("--event", default=None, help="the delivered event action")
        command.add_argument(
            "--payload", default=None, help="pre-fetched subject and Project snapshot"
        )
        command.add_argument(
            "--json", action="store_true", help="accepted for symmetry; output is always JSON"
        )
    return parser


def _subject(raw: Any) -> IssueOrPullRequestSnapshot:
    if not isinstance(raw, dict):
        raise NormalizationError("normalize-subject-invalid", "the subject must be a JSON object")
    fields = {key: value for key, value in raw.items() if key in _SNAPSHOT_FIELDS}
    for name in _TUPLE_FIELDS:
        if isinstance(fields.get(name), list):
            fields[name] = tuple(fields[name])
    linked = fields.get("linked_issues")
    if isinstance(linked, (list, tuple)):
        fields["linked_issues"] = tuple(_subject(entry) for entry in linked)
    fields.setdefault("kind", "issue")
    fields.setdefault("event", "opened")
    fields.setdefault("number", None)
    fields.setdefault("url", None)
    fields.setdefault("node_id", None)
    fields.setdefault("state", None)
    return IssueOrPullRequestSnapshot(**fields)


def _project(raw: Any) -> ProjectSnapshot:
    if not isinstance(raw, dict):
        raise NormalizationError(
            "normalize-project-snapshot-invalid", "the Project snapshot must be a JSON object"
        )
    items = raw.get("items")
    if not isinstance(items, list):
        raise NormalizationError(
            "normalize-project-snapshot-invalid",
            "the Project snapshot must carry an `items` array",
        )
    return ProjectSnapshot(
        project_owner=str(raw.get("project_owner") or ""),
        project_number=int(raw.get("project_number") or 0),
        items=tuple(item for item in items if isinstance(item, dict)),
        fields=raw.get("fields") if isinstance(raw.get("fields"), dict) else {},
        hit_cap=bool(raw.get("hit_cap")),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    requested = args.issue if args.command == "intake" else args.subject

    try:
        if args.payload is None:
            raise NormalizationError(
                "normalize-project-snapshot-required",
                "a complete Project snapshot is required; pass --payload with the "
                "subject and the snapshot from `super-board-project.py snapshot`",
            )
        document = json.loads(Path(args.payload).read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise NormalizationError(
                "normalize-payload-invalid", "--payload must hold a JSON object"
            )
        subject = _subject(document.get("subject"))
        if args.event:
            subject = type(subject)(**{**subject.__dict__, "event": args.event})
        if subject.url and requested and subject.url != requested:
            raise NormalizationError(
                "normalize-subject-mismatch",
                "the named subject is not the one the payload carries",
            )
        project = _project(document.get("project"))
        if args.command == "intake":
            plan = normalize_intake(subject, project)
        else:
            plan = normalize_closure(
                subject,
                project,
                activation_boundary=args.activation_boundary,
                environment=os.environ,
            )
    except NormalizationError as exc:
        print(f"super-board-normalize: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True), file=sys.stderr)
        return EXIT_CONFIG
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"super-board-normalize: invalid input: {exc}", file=sys.stderr)
        print(
            json.dumps({"ok": False, "reason": "normalize-input-invalid"}, sort_keys=True),
            file=sys.stderr,
        )
        return EXIT_CONFIG

    body = {**plan.to_dict(), "ok": True}
    if plan.quarantined:
        print(json.dumps({**body, "ok": False}, sort_keys=True), file=sys.stderr)
        print(
            "🛑 super-board-normalize: the board moved after the event; nothing was planned.",
            file=sys.stderr,
        )
        return EXIT_CONFLICT
    print(json.dumps(body, sort_keys=True))
    if plan.blocked_reason:
        print(
            f"super-board-normalize: intake held ({plan.blocked_reason}); not promoted.",
            file=sys.stderr,
        )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
