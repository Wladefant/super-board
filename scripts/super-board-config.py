#!/usr/bin/env python3
"""Superboard configuration CLI.

  super-board-config.py validate --config PATH [--json]

Machine-readable JSON on stdout, human diagnostics on stderr. Exit codes:
0 success, 64 invalid invocation, 65 invalid configuration.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:  # package import when scripts/ is on sys.path
    from super_board_runtime import EXIT_CONFIG, EXIT_OK, EXIT_USAGE
    from super_board_runtime.config import ConfigError, load_and_validate_config, normalized_config_to_json
except ModuleNotFoundError:  # direct invocation by absolute path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from super_board_runtime import EXIT_CONFIG, EXIT_OK, EXIT_USAGE
    from super_board_runtime.config import ConfigError, load_and_validate_config, normalized_config_to_json


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse defaults to exit 2; the contract says 64
        self.print_usage(sys.stderr)
        print(f"super-board-config: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="super-board-config.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate a config and print its normalized form")
    validate.add_argument("--config", required=True, help="path to the config JSON")
    validate.add_argument(
        "--json", action="store_true", help="emit JSON on stdout (the default and only format)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command != "validate":  # unreachable while `validate` is the only subcommand
        print(f"unknown command: {args.command}", file=sys.stderr)
        return EXIT_USAGE
    try:
        config = load_and_validate_config(Path(args.config))
    except ConfigError as exc:
        print(f"super-board-config: invalid config: {exc}", file=sys.stderr)
        print(
            json.dumps({"ok": False, "reason": exc.reason, "config": args.config}, sort_keys=True),
            file=sys.stderr,
        )
        return EXIT_CONFIG
    print(normalized_config_to_json(config))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
