#!/usr/bin/env python3
"""super-board-codex-review.py — the local Codex review gate.

Exactly ONE parallel maximum-level Codex fleet per code pull request:

    codex exec review --base "$(git merge-base origin/<base> HEAD)" \
        -m gpt-5.5 -c model_reasoning_effort="high"
    codex exec -m gpt-5.5 -c model_reasoning_effort="high" -s read-only "<correctness lens>"
    codex exec -m gpt-5.5 -c model_reasoning_effort="high" -s read-only "<security lens>"
    codex exec -m gpt-5.5 -c model_reasoning_effort="high" -s read-only "<performance and design-consistency lens>"

`codex exec review` never receives a custom prompt — the CLI rejects the
combination, so a prompt there loses the entire structured review. The three
lens passes always use plain `codex exec` with `-s read-only`.

Every finding across every lens must be resolved, **including nits**. There is
no confidence threshold and no "skip the low ones": an advisory review is not a
gate. A second automatic fleet is refused; re-review only on an explicit request.

Pull requests whose complete diff is documentation are exempt.

CodeRabbit, Copilot, Greptile, and the GitHub `@codex` connector are NOT gates.
The connector has its own easily-exhausted review rate limit and treating it as
the gate produces false "usage limit" stops while the task budget is untouched.

Usage:
    super-board-codex-review.py run --base REF --worktree PATH [--json]
                                   [--pull-request URL] [--changed-file PATH ...]
                                   [--resolution LOCATION=EVIDENCE ...]
                                   [--plan-only] [--force-rerun]

`--plan-only` prints the four commands without invoking Codex.

Exit: 0 gate passed · 1 gate blocked · 64 invalid invocation · 65 contract violation.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime import EXIT_OK, EXIT_USAGE  # noqa: E402
from super_board_runtime.publication import (  # noqa: E402
    UnsafePublication,
    sanitize_and_validate_publication,
)
from super_board_runtime.review import (  # noqa: E402
    CodexGateError,
    is_documentation_only,
    raw_output_dir,
    run_codex_fleet,
)

GATE_BLOCKED = 1


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(f"super-board-codex-review: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="super-board-codex-review.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the four-lens fleet once")
    run.add_argument("--base", required=True, help="the pull request's base branch")
    run.add_argument("--worktree", required=True, help="the pull request's worktree")
    run.add_argument("--pull-request", default=None, help="used to enforce one fleet per PR")
    run.add_argument(
        "--changed-file",
        action="append",
        default=[],
        help="a file in the diff; repeat. All-documentation diffs are exempt.",
    )
    run.add_argument(
        "--resolution",
        action="append",
        default=[],
        metavar="LOCATION=EVIDENCE",
        help="committed evidence resolving one finding; repeat",
    )
    run.add_argument("--ledger", default=None, help="path to the one-fleet-per-PR ledger")
    run.add_argument("--plan-only", action="store_true", help="print the commands, run nothing")
    run.add_argument(
        "--force-rerun",
        action="store_true",
        help="explicit user re-review; the only way past codex-fleet-already-run",
    )
    run.add_argument("--json", action="store_true", help="machine-readable output (default)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    resolutions: dict[str, str] = {}
    for entry in args.resolution:
        location, _, evidence = str(entry).partition("=")
        if location.strip():
            resolutions[location.strip()] = evidence.strip()

    ledger = Path(args.ledger) if args.ledger else raw_output_dir() / "fleet-ledger.json"

    try:
        report = run_codex_fleet(
            args.base,
            Path(args.worktree),
            is_documentation_only(args.changed_file),
            ledger=ledger,
            pull_request_url=args.pull_request,
            force_rerun=args.force_rerun,
            resolutions=resolutions,
            plan_only=args.plan_only,
        )
    except CodexGateError as exc:
        print(f"🛑 super-board-codex-review: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True), file=sys.stderr)
        return exc.exit_code

    body = {**report.to_dict(), "ok": report.passed}
    # Only the sanitized summary is ever publishable; raw lens output stays on
    # local disk outside the Git tree.
    try:
        body["summary"] = sanitize_and_validate_publication(
            report.published_summary(), {}, surface="review-summary"
        ).text
    except UnsafePublication as exc:
        print(f"🛑 super-board-codex-review: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True), file=sys.stderr)
        return exc.exit_code

    print(json.dumps(body, sort_keys=True))
    if not report.passed:
        print(
            "🛑 the Codex gate is blocked — resolve EVERY finding, nits included, "
            "then record the committed evidence with --resolution.",
            file=sys.stderr,
        )
        return GATE_BLOCKED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
