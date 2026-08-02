#!/usr/bin/env python3
"""Activation-mode and proof-allowlist policy.

Three modes, and nothing at runtime can bypass them:

  off         nothing is ever dispatched, including a perfectly formed `Ready`
              issue. This is the default, and it stays the default until every
              installation and repository gate passes.
  proof-only  exactly one allowlisted issue may be selected. Every other card is
              refused with `activation-not-allowlisted`.
  active      normal dispatch; the decision defers entirely to
              `eligibility.evaluate_dispatch`.

The mode is re-read from disk immediately before a claim and immediately before
a launch, so an operator who flips a board off mid-run aborts the very next
claim rather than the run after it.

This module deliberately does not import `eligibility` — it takes any object
carrying `url` and `number` — so eligibility can call activation without a
circular import.

CLI:

    python -m super_board_runtime.activation --config <cfg> --issue-url <url> \\
        [--planned-mode active] [--stage claim|launch]

Machine-readable JSON on stdout, diagnostics on stderr. Exit 0 when the decision
was reached (read `permitted`), 64 invalid invocation, 65 invalid configuration.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

try:  # normal package import
    from . import EXIT_CONFIG, EXIT_OK, EXIT_USAGE
    from .config import ACTIVATION_MODES, ConfigError, NormalizedConfig, load_and_validate_config
except ImportError:  # executed as a plain file path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFIG, EXIT_OK, EXIT_USAGE
    from super_board_runtime.config import (
        ACTIVATION_MODES,
        ConfigError,
        NormalizedConfig,
        load_and_validate_config,
    )

STAGES: tuple[str, ...] = ("plan", "claim", "launch")


@dataclass(frozen=True)
class ActivationDecision:
    permitted: bool
    activation_mode: str
    reason_code: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "permitted": self.permitted,
            "activation_mode": self.activation_mode,
            "reason_code": self.reason_code,
        }


def normalize_issue_url(url: Optional[str]) -> Optional[str]:
    """Fold a GitHub issue URL for comparison.

    Owner and repository names are case-insensitive on GitHub, so the whole URL
    folds. A missing or non-string URL folds to None and can never match.
    """
    if not isinstance(url, str):
        return None
    text = url.strip().rstrip("/")
    if not text:
        return None
    return text.casefold()


def evaluate_activation(issue: Any, config: NormalizedConfig) -> ActivationDecision:
    """Decide whether activation permits dispatching ``issue``. Fails closed."""
    mode = config.activation_mode

    if mode == "off":
        return ActivationDecision(False, mode, "activation-off")

    if mode == "proof-only":
        allowed = normalize_issue_url(config.proof_issue_url)
        candidate = normalize_issue_url(getattr(issue, "url", None))
        if allowed is not None and candidate is not None and candidate == allowed:
            return ActivationDecision(True, mode, None)
        return ActivationDecision(False, mode, "activation-not-allowlisted")

    if mode == "active":
        return ActivationDecision(True, mode, None)

    # Unreachable through validated config; still never permissive.
    return ActivationDecision(False, mode, "activation-mode-invalid")


def guard_stage(
    issue: Any,
    config_path: Path,
    *,
    planned_mode: Optional[str] = None,
    stage: str = "claim",
    loader: Callable[[Path], NormalizedConfig] = load_and_validate_config,
) -> ActivationDecision:
    """Re-evaluate activation from disk at a mutation boundary.

    Called immediately before the assignee claim and again immediately before
    the worker launch. A mode that changed since planning aborts the stage with
    `activation-mode-changed`; an unreadable or invalid config aborts it with
    `activation-config-invalid`.
    """
    if stage not in STAGES:
        return ActivationDecision(False, "unknown", "activation-stage-invalid")
    try:
        config = loader(Path(config_path))
    except ConfigError:
        return ActivationDecision(False, "unknown", "activation-config-invalid")
    except OSError:
        return ActivationDecision(False, "unknown", "activation-config-invalid")

    if planned_mode is not None and planned_mode != config.activation_mode:
        return ActivationDecision(False, config.activation_mode, "activation-mode-changed")
    return evaluate_activation(issue, config)


# ───────────────────────────── CLI ─────────────────────────────


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(f"super-board-activation: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


class _UrlOnlyIssue:
    """Minimal carrier so the CLI needs no board payload."""

    def __init__(self, url: Optional[str], number: Optional[int]) -> None:
        self.url = url
        self.number = number


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="super_board_runtime.activation", description=__doc__)
    parser.add_argument("--config", required=True, help="path to the config JSON")
    parser.add_argument("--issue-url", default=None, help="the card's issue URL")
    parser.add_argument("--issue-number", type=int, default=None, help="the card's issue number")
    parser.add_argument(
        "--planned-mode",
        default=None,
        choices=ACTIVATION_MODES,
        help="the mode observed at plan time; a change aborts the stage",
    )
    parser.add_argument("--stage", default="claim", choices=STAGES, help="the mutation boundary")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        load_and_validate_config(Path(args.config))
    except ConfigError as exc:
        print(f"super-board-activation: invalid config: {exc}", file=sys.stderr)
        print(json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True), file=sys.stderr)
        return EXIT_CONFIG

    decision = guard_stage(
        _UrlOnlyIssue(args.issue_url, args.issue_number),
        Path(args.config),
        planned_mode=args.planned_mode,
        stage=args.stage,
    )
    payload = decision.to_dict()
    payload["stage"] = args.stage
    payload["issue_url"] = args.issue_url
    payload["issue_number"] = args.issue_number
    print(json.dumps(payload, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
