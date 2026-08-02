#!/usr/bin/env python3
"""super-board-project.py — Project snapshot, compare, apply, and reconcile.

Six subcommands, one rule: **compare before mutate**. A mutation is authorized
by state reread at decision time, never by state captured during preflight.

    query      print the ONE board-items GraphQL query (both owner types)
    pages      convert a raw `gh api graphql` read into walkable pages
    snapshot   walk every page of the Project and write a complete inventory
    compare    decide `apply` or `quarantine` for one planned mutation
    apply      write a decision that says `apply`, then read it back
    reconcile  compare a whole manifest and report what quarantined

`query` and `pages` exist so no caller keeps its own copy of the board read. A
board is owned by a user OR by an organization, and a `user(login:)` query
returns null for an organization-owned board — which then reads as a board with
no items. `pages` refuses that: an owner or Project that did not resolve halts
with exit 65 and writes no snapshot at all.

Between preflight and apply a human can move a card, a workflow can rename the
Status field, and GitHub can hand out new option IDs. Writing anyway silently
overwrites whoever was right — so anything that moved quarantines with exit 3
and zero writes.

Usage:
    super-board-project.py query
    super-board-project.py pages     --raw FILE
    super-board-project.py snapshot  --owner OWNER --number N --payload FILE [--json]
    super-board-project.py compare   --expected FILE --current FILE [--desired-status S]
    super-board-project.py apply     --expected FILE --current FILE --desired-status S
                                     [--execute]
    super-board-project.py reconcile --manifest FILE

`--payload` reads a pre-fetched Project response instead of calling GitHub, so
the whole pipeline is testable without touching a live board. `apply` writes
nothing without `--execute`.

Exit: 0 ok · 3 compare-before-mutate conflict (nothing changed) ·
      64 invalid invocation · 65 invalid input.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime import EXIT_CONFIG, EXIT_CONFLICT, EXIT_OK, EXIT_USAGE  # noqa: E402
from super_board_runtime.project import (  # noqa: E402
    PROJECT_ITEMS_QUERY,
    CurrentState,
    ExpectedState,
    MutationConflict,
    apply_project_mutation,
    compare_project_mutation,
    project_pages_from_graphql,
    snapshot_project,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(f"super-board-project: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="super-board-project.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("query", help="print the board-items query (user- and org-owned)")

    pages = sub.add_parser("pages", help="convert a raw graphql read into walkable pages")
    pages.add_argument(
        "--raw", required=True, help="`gh api graphql --paginate --slurp` output (JSON)"
    )

    snap = sub.add_parser("snapshot", help="paginated, complete Project inventory")
    snap.add_argument("--owner", required=True)
    snap.add_argument("--number", type=int, required=True)
    snap.add_argument("--payload", required=True, help="pre-fetched pages, as a JSON array")

    for name, helptext in (
        ("compare", "decide apply or quarantine for one mutation"),
        ("apply", "write an authorized mutation, then read it back"),
    ):
        cmd = sub.add_parser(name, help=helptext)
        cmd.add_argument("--expected", required=True, help="the manifest's prior values")
        cmd.add_argument("--current", required=True, help="the values reread at decision time")
        cmd.add_argument("--desired-status", default=None)
        if name == "apply":
            cmd.add_argument("--execute", action="store_true", help="actually write")

    rec = sub.add_parser("reconcile", help="compare every record in a manifest")
    rec.add_argument("--manifest", required=True)
    return parser


def _read(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _state(cls, raw: Any):
    if not isinstance(raw, dict):
        raise ValueError("a state record must be a JSON object")
    known = {f for f in cls.__dataclass_fields__}
    return cls(**{key: value for key, value in raw.items() if key in known})


def _emit_decision(decision) -> int:
    body = {**decision.to_dict(), "ok": decision.action == "apply"}
    if decision.action == "apply":
        print(json.dumps(body, sort_keys=True))
        return EXIT_OK
    print(json.dumps(body, sort_keys=True), file=sys.stderr)
    print(
        f"🛑 super-board-project: quarantined ({decision.reason_code}); nothing was changed.",
        file=sys.stderr,
    )
    return EXIT_CONFLICT


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command == "query":
            print(PROJECT_ITEMS_QUERY.rstrip("\n"))
            return EXIT_OK

        if args.command == "pages":
            # Nothing is printed before the conversion succeeds: a refusal must
            # leave stdout empty so a caller redirecting it cannot end up with
            # half a board on disk. An unreadable board is an input-contract
            # failure (65), not a compare-before-mutate conflict (3) — nothing
            # was being mutated.
            try:
                pages = project_pages_from_graphql(_read(args.raw))
            except MutationConflict as exc:
                print(f"super-board-project: {exc}", file=sys.stderr)
                print(
                    json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True),
                    file=sys.stderr,
                )
                return EXIT_CONFIG
            print(json.dumps(pages, sort_keys=True))
            return EXIT_OK

        if args.command == "snapshot":
            pages = _read(args.payload)
            if not isinstance(pages, list):
                raise ValueError("--payload must hold a JSON array of page responses")
            cursor = {"index": 0}

            def fetch(_after):
                if cursor["index"] >= len(pages):
                    raise RuntimeError("the payload ran out of pages before the walk finished")
                page = pages[cursor["index"]]
                cursor["index"] += 1
                return page

            snapshot = snapshot_project(args.owner, args.number, fetch=fetch)
            print(json.dumps({**snapshot.to_dict(), "ok": True}, sort_keys=True))
            return EXIT_OK

        if args.command in ("compare", "apply"):
            expected = _state(ExpectedState, _read(args.expected))
            current = _state(CurrentState, _read(args.current))
            decision = compare_project_mutation(
                expected, current, desired_status=args.desired_status
            )
            if args.command == "compare":
                return _emit_decision(decision)
            if decision.action != "apply":
                return _emit_decision(decision)

            def writer(_payload):
                raise MutationConflict(
                    "mutation-writer-unconfigured",
                    "this CLI does not carry a GitHub writer; the orchestrator performs "
                    "Project mutations directly",
                )

            result = apply_project_mutation(
                decision,
                writer=writer,
                readback=lambda _decision: current,
                dry_run=not args.execute,
            )
            print(json.dumps({**result, "ok": True}, sort_keys=True))
            return EXIT_OK

        # reconcile
        manifest = _read(args.manifest)
        records = manifest.get("records") if isinstance(manifest, dict) else manifest
        if not isinstance(records, list):
            raise ValueError("the manifest must carry a `records` array")
        decisions = []
        quarantined = 0
        for record in records:
            decision = compare_project_mutation(
                _state(ExpectedState, record.get("expected")),
                _state(CurrentState, record.get("current")),
                desired_status=record.get("desired_status"),
            )
            decisions.append(decision.to_dict())
            quarantined += decision.action == "quarantine"
        body = {
            "decisions": decisions,
            "ok": quarantined == 0,
            "quarantined": quarantined,
            "total": len(decisions),
        }
        if quarantined:
            print(json.dumps(body, sort_keys=True), file=sys.stderr)
            return EXIT_CONFLICT
        print(json.dumps(body, sort_keys=True))
        return EXIT_OK

    except MutationConflict as exc:
        print(f"super-board-project: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True), file=sys.stderr)
        return exc.exit_code
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"super-board-project: invalid input: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": "project-input-invalid"}, sort_keys=True), file=sys.stderr)
        return EXIT_CONFIG


if __name__ == "__main__":
    raise SystemExit(main())
