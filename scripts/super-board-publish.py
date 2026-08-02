#!/usr/bin/env python3
"""super-board-publish.py — the ONLY supported GitHub evidence-publication CLI.

Every GitHub-bound payload the pipeline produces goes through this one command:
issue creation and edits, pull-request bodies and comments, review summaries, QA
comments, checks, commit statuses, closure comments, bug reports, release text,
Project text fields, and dispatch/reconciliation manifests.

It renders the complete payload, redacts, rescans the complete redacted result,
and fails closed with exit 78 before any write happens. A second publication
path would be a second place to forget a secret category — there isn't one.

Input is a JSON document:

    {
      "surface": "qa-comment",
      "text": "…",                       // or "template_fragments": ["…", "…"]
      "environment": {"NAME": "value"},   // optional EXTRA names; the calling
                                          // process environment is always in
                                          // scope and never has to be declared
      "artifacts": [{"name": "…", "classification": "image/png"}]
    }

`template_fragments` exists because a secret can be split across two fragments
and neither matches anything alone. They are joined BEFORE scanning.

Usage:
    super-board-publish.py publish --input PATH [--surface NAME] [--json]
                                   [--execute --target <gh-target>]

Without `--execute` nothing is written: the sanitized payload is printed and
`github_writes` is 0. That is the default on purpose — a dry sanitize is always
safe to run.

Exit: 0 ok · 64 invalid invocation · 65 invalid input/unknown surface ·
      78 unsafe evidence rejected (nothing written).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime import EXIT_CONFIG, EXIT_OK, EXIT_USAGE, gh_binary  # noqa: E402
from super_board_runtime.publication import (  # noqa: E402
    MIN_REDACTABLE_ENV_VALUE_LEN,
    PUBLICATION_SURFACES,
    PublicationError,
    UnsafePublication,
    publish,
    render_payload,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(f"super-board-publish: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="super-board-publish.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pub = sub.add_parser("publish", help="sanitize a payload and optionally write it")
    pub.add_argument("--input", required=True, help="path to the payload JSON")
    pub.add_argument(
        "--surface",
        default=None,
        choices=PUBLICATION_SURFACES,
        help="override the payload's surface",
    )
    pub.add_argument("--json", action="store_true", help="machine-readable output (default)")
    pub.add_argument(
        "--execute",
        action="store_true",
        help="actually write; without it nothing is published",
    )
    pub.add_argument("--target", default=None, help="the gh target for --execute")
    return parser


#: Surfaces that CREATE an issue. GitHub needs a title as well as a body, and
#: the only title we may send is the one that came out of the sanitizer — so it
#: is taken from the first line of the sanitized text, never passed alongside it
#: as a second, unscanned channel.
_ISSUE_CREATING_SURFACES = ("issue-create", "bug-report")


def _issue_body(surface: str, text: str) -> dict[str, str]:
    if surface not in _ISSUE_CREATING_SURFACES:
        return {"body": text}
    head, _, rest = text.partition("\n")
    return {"title": head.lstrip("#").strip(), "body": rest.lstrip("\n")}


def _gh_writer(target: Optional[str]):
    def write(surface: str, text: str) -> dict[str, Any]:
        if not target:
            raise PublicationError(
                "publication-target-missing", "--execute requires --target"
            )
        # The sanitized text is handed over on stdin, never on a command line:
        # a command line is visible in the process table.
        result = subprocess.run(
            [gh_binary(), "api", "--method", "POST", target, "--input", "-"],
            input=json.dumps(_issue_body(surface, text)),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise PublicationError("publication-write-failed", "the GitHub write failed")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {}

    return write


def _environment_in_scope(declared: Any) -> dict[str, str]:
    """Every value the sanitizer should treat as known credential material.

    The calling process environment is always in scope. Shell callers publish
    `{surface, text}` and nothing else, and the credential most likely to be
    echoed into their payload is the one already exported in the process they
    are running in — a value that matches no provider pattern is only ever
    caught by being a *known* value.

    Ambient values are admitted only when they are long enough to be redacted.
    A credential-NAMED variable can hold something that is plainly not a
    credential (`SESSIONNAME=Console`, `..._SESSION=1`), and admitting those
    would refuse every payload containing the character `1` — which is a
    boundary nobody can publish through, not a boundary that fails closed.
    A value the caller DECLARES carries no such floor: the declaration itself
    says it is credential material, so a short one still fails the publication.
    """
    environment = {
        name: value
        for name, value in os.environ.items()
        if isinstance(value, str) and len(value) >= MIN_REDACTABLE_ENV_VALUE_LEN
    }
    if isinstance(declared, dict):
        environment.update(
            {
                name: value
                for name, value in declared.items()
                if isinstance(name, str) and isinstance(value, str)
            }
        )
    return environment


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"super-board-publish: unreadable payload: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": "payload-unreadable"}, sort_keys=True), file=sys.stderr)
        return EXIT_CONFIG
    if not isinstance(raw, dict):
        print("super-board-publish: the payload must be a JSON object", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": "payload-invalid"}, sort_keys=True), file=sys.stderr)
        return EXIT_CONFIG

    surface = args.surface or raw.get("surface")
    fragments = raw.get("template_fragments")
    if isinstance(fragments, list):
        text = render_payload(fragments)
    else:
        text = raw.get("text")
    if not isinstance(text, str):
        print("super-board-publish: payload needs 'text' or 'template_fragments'", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": "payload-invalid"}, sort_keys=True), file=sys.stderr)
        return EXIT_CONFIG

    environment = _environment_in_scope(raw.get("environment"))
    artifacts = raw.get("artifacts") if isinstance(raw.get("artifacts"), list) else []

    try:
        result = publish(
            surface if isinstance(surface, str) else "",
            text,
            environment,
            writer=_gh_writer(args.target),
            artifacts=artifacts,
            dry_run=not args.execute,
        )
    except UnsafePublication as exc:
        # Category and offset only. Quoting the leak would be a second leak.
        print(f"🛑 super-board-publish: {exc}", file=sys.stderr)
        print(
            json.dumps(
                {
                    "findings": [f.to_dict() for f in exc.findings],
                    "github_writes": 0,
                    "ok": False,
                    "reason": exc.reason,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return exc.exit_code
    except PublicationError as exc:
        print(f"super-board-publish: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True), file=sys.stderr)
        return exc.exit_code

    print(json.dumps({**result, "ok": True}, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
