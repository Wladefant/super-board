#!/usr/bin/env python3
"""Superboard authentication preflight CLI.

  super-board-auth.py preflight --config PATH --mode {interactive,unattended} [--json]

Machine-readable JSON on stdout, human diagnostics on stderr. Exit codes:
0 verified, 64 invalid invocation, 65 invalid configuration, 69 authentication,
identity, or permission failure.

No token value, environment value, authorization header, or command line is ever
printed. Environment variables are named, never quoted.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:
    from super_board_runtime import EXIT_AUTH, EXIT_CONFIG, EXIT_OK, EXIT_USAGE
    from super_board_runtime.auth import (
        AUTH_MODES,
        LOGIN_ENV_VAR,
        TOKEN_ENV_VAR,
        AuthReport,
        token_class_explanation,
        verify_github_identity,
    )
    from super_board_runtime.config import ConfigError, load_and_validate_config
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from super_board_runtime import EXIT_AUTH, EXIT_CONFIG, EXIT_OK, EXIT_USAGE
    from super_board_runtime.auth import (
        AUTH_MODES,
        LOGIN_ENV_VAR,
        TOKEN_ENV_VAR,
        AuthReport,
        token_class_explanation,
        verify_github_identity,
    )
    from super_board_runtime.config import ConfigError, load_and_validate_config


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(f"super-board-auth: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def explain(report: AuthReport) -> str:
    """One human line for a failed report. Quotes no credential material."""
    reason = report.reason_code or "unknown"
    if reason == "token-env-missing":
        return (
            f"unattended mutation requires a machine-account classic PAT in the environment "
            f"variable {TOKEN_ENV_VAR}; it is unset or empty"
        )
    if reason == "expected-login-missing":
        return (
            f"set the expected machine-account login in {LOGIN_ENV_VAR} (or "
            f"github_auth.expected_login) so the identity can be pinned"
        )
    if reason == "token-class-not-classic":
        return token_class_explanation(report.token_class)
    if reason == "scope-ambiguous":
        return (
            "GitHub returned no usable OAuth scope header, so the token's grants cannot be "
            "enumerated; capability probing does not rescue an unenumerable token"
        )
    if reason == "insufficient-scope":
        return "the token is missing at least one of the required scopes"
    if reason == "identity-mismatch":
        return (
            f"the token authenticates as {report.login!r}, which is not the expected machine "
            f"account named in {LOGIN_ENV_VAR}"
        )
    if reason == "identity-unavailable":
        return "GitHub did not return a usable identity; nothing fails open, so this is a stop"
    if reason.startswith("capability-missing:"):
        return (
            f"the identity cannot reach {reason.split(':', 1)[1]!r}; grant access or fix the "
            "configured owner/number before running"
        )
    if reason == "auth-mode-invalid":
        return f"mode must be one of {', '.join(AUTH_MODES)}"
    return f"authentication failed: {reason}"


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="super-board-auth.py", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight", help="verify the GitHub identity and its capabilities")
    preflight.add_argument("--config", required=True, help="path to the config JSON")
    preflight.add_argument("--mode", required=True, choices=AUTH_MODES)
    preflight.add_argument("--json", action="store_true", help="emit JSON on stdout (the default)")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    probe: Any = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_and_validate_config(Path(args.config))
    except ConfigError as exc:
        print(f"super-board-auth: invalid config: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True), file=sys.stderr)
        return EXIT_CONFIG

    report = verify_github_identity(config, args.mode, env=env, probe=probe)
    if not report.ok:
        print(f"super-board-auth: {explain(report)}", file=sys.stderr)
        print(json.dumps(report.to_dict(), sort_keys=True), file=sys.stderr)
        return EXIT_AUTH
    print(json.dumps(report.to_dict(), sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
