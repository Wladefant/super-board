#!/usr/bin/env python3
"""Immutable GraphQL reserve accounting.

The reserve is a floor of 1000 points that the pipeline will not spend. A
configuration may RAISE it; lowering it is a configuration error (exit 65,
enforced in `config.py`) and is clamped back up here as a second line of defence.

The rules:

- One cached inventory per runtime cycle. `QuotaCycle` reads the quota once and
  every check inside that cycle reuses it — the guard must not become the thing
  that drains the bucket.
- Estimate the cost of a mutation before executing it. `remaining -
  estimated_cost >= effective_floor` is required.
- Batches are bounded: never more than 25 records, and pagination is capped.
- Reaching the reserve stops cleanly with exit 75. There is no sleep through the
  reset, no retry loop, and no fabricated fallback capacity — the old code
  assumed 5,000 points whenever `gh` failed, which is exactly how an empty
  bucket looked like a full one.
- An unreadable or malformed quota response is `available == false` and raises.
  Nothing fails open.
- Only four fields are ever logged: remaining points, estimated cost, effective
  floor, and reset time. No token, header, cookie, or raw payload.

CLI:

    python -m super_board_runtime.quota check --estimated-cost 103 [--config <cfg>]
    python -m super_board_runtime.quota summary [--config <cfg>]

`check` exits 0 within budget, 64 invalid invocation, 65 invalid configuration,
75 quota unavailable or reserve reached. `summary` only reports — it prints the
worker exit line documented in `references/rate-limit-etiquette.md` and always
exits 0, so a worker's last act cannot change the status it exits with.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

try:  # normal package import
    from . import EXIT_CONFIG, EXIT_OK, EXIT_QUOTA, EXIT_USAGE, gh_binary
    from .config import MINIMUM_GRAPHQL_RESERVE, ConfigError, load_and_validate_config
except ImportError:  # executed as a plain file path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFIG, EXIT_OK, EXIT_QUOTA, EXIT_USAGE, gh_binary
    from super_board_runtime.config import (
        MINIMUM_GRAPHQL_RESERVE,
        ConfigError,
        load_and_validate_config,
    )

#: The reserve floor, in GraphQL points. Configuration may raise it, never lower it.
IMMUTABLE_GRAPHQL_FLOOR: int = MINIMUM_GRAPHQL_RESERVE

#: No mutation batch may carry more records than this.
MAX_MUTATION_BATCH: int = 25

#: No paginated read may request more pages than this.
MAX_PAGINATION_PAGES: int = 20


class QuotaError(Exception):
    """Invalid quota input. Maps to exit code 65."""

    exit_code = EXIT_CONFIG

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class QuotaExhausted(QuotaError):
    """The reserve was reached, or the quota could not be read. Exit code 75."""

    exit_code = EXIT_QUOTA


@dataclass(frozen=True)
class QuotaSnapshot:
    remaining: Optional[int]
    limit: Optional[int]
    reset_at: Optional[str]
    available: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "limit": self.limit,
            "remaining": self.remaining,
            "reset_at": self.reset_at,
        }


UNAVAILABLE = QuotaSnapshot(remaining=None, limit=None, reset_at=None, available=False)


def effective_floor(configured_floor: Optional[int]) -> int:
    """The floor actually enforced: the immutable minimum, or higher."""
    if isinstance(configured_floor, bool) or not isinstance(configured_floor, int):
        return IMMUTABLE_GRAPHQL_FLOOR
    return max(IMMUTABLE_GRAPHQL_FLOOR, configured_floor)


def _reset_to_rfc3339(reset: Any) -> Optional[str]:
    if isinstance(reset, bool) or not isinstance(reset, int) or reset <= 0:
        return None
    return (
        datetime.fromtimestamp(reset, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def snapshot_from_payload(payload: Any) -> QuotaSnapshot:
    """Parse a `gh api rate_limit` payload. Anything unexpected is unavailable."""
    if not isinstance(payload, dict):
        return UNAVAILABLE
    resources = payload.get("resources")
    if not isinstance(resources, dict):
        return UNAVAILABLE
    graphql = resources.get("graphql")
    if not isinstance(graphql, dict):
        return UNAVAILABLE
    remaining = graphql.get("remaining")
    if isinstance(remaining, bool) or not isinstance(remaining, int):
        return UNAVAILABLE
    limit = graphql.get("limit")
    if isinstance(limit, bool) or not isinstance(limit, int):
        limit = None
    return QuotaSnapshot(
        remaining=remaining,
        limit=limit,
        reset_at=_reset_to_rfc3339(graphql.get("reset")),
        available=True,
    )


def _gh_rate_limit() -> Any:
    # `gh_binary()`, not a literal — the same override every other GitHub call in
    # this runtime honours, so the quota read is exercisable without an account.
    result = subprocess.run(
        [gh_binary(), "api", "rate_limit"], capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return None
    return json.loads(result.stdout)


def read_graphql_quota(fetch: Optional[Callable[[], Any]] = None) -> QuotaSnapshot:
    """Read the GraphQL quota once. Any failure yields an unavailable snapshot."""
    fetch = _gh_rate_limit if fetch is None else fetch
    try:
        payload = fetch()
    except Exception:
        return UNAVAILABLE
    return snapshot_from_payload(payload)


def require_graphql_budget(
    snapshot: QuotaSnapshot, estimated_cost: int, configured_floor: Optional[int]
) -> None:
    """Raise unless the operation can run without breaking the reserve.

    Never returns a permissive value: it either returns None or raises.
    """
    if isinstance(estimated_cost, bool) or not isinstance(estimated_cost, int) or estimated_cost < 1:
        raise QuotaError(
            "quota-cost-invalid",
            "estimated_cost must be a positive integer number of GraphQL points; "
            "estimate the mutation before executing it",
        )
    floor = effective_floor(configured_floor)
    if not snapshot.available or snapshot.remaining is None:
        raise QuotaExhausted(
            "quota-unavailable",
            "the GraphQL quota could not be read; refusing to spend against an unknown budget",
        )
    if snapshot.remaining - estimated_cost < floor:
        raise QuotaExhausted(
            "graphql-reserve-reached",
            quota_log_line(snapshot, estimated_cost, configured_floor)
            + " — stopping cleanly rather than spending the reserve",
        )


#: The prefix every worker's exit summary carries, so the run manifest can find
#: the line without parsing the rest of a handoff comment.
QUOTA_SUMMARY_PREFIX = "gh-quota-on-exit:"

#: What a worker reports when the quota could not be read. It is a marker, not a
#: number: an exit line that quietly omitted the balance would read like a run
#: that never touched the API.
QUOTA_SUMMARY_UNAVAILABLE = f"{QUOTA_SUMMARY_PREFIX} unavailable (quota could not be read)"


def quota_summary_line(snapshot: QuotaSnapshot, configured_floor: Optional[int]) -> str:
    """The worker exit line documented in `references/rate-limit-etiquette.md`.

    Three safe fields and nothing else — remaining points, the floor actually
    enforced, and the reset time. No token, no header, no cookie, no raw
    payload, and no estimated cost, because an exit summary is not spending
    anything. An unreadable quota renders the unavailable marker rather than a
    fabricated balance.
    """
    if not snapshot.available or snapshot.remaining is None:
        return QUOTA_SUMMARY_UNAVAILABLE
    return (
        f"{QUOTA_SUMMARY_PREFIX} graphql={snapshot.remaining} "
        f"floor={effective_floor(configured_floor)} reset={snapshot.reset_at or 'unknown'}"
    )


def quota_log_line(
    snapshot: QuotaSnapshot, estimated_cost: int, configured_floor: Optional[int]
) -> str:
    """The only quota line the runtime is allowed to emit.

    Four safe fields, nothing else: no token, no header, no cookie, no raw
    payload, not even the account's rate limit.
    """
    remaining = "unknown" if snapshot.remaining is None else str(snapshot.remaining)
    reset_at = snapshot.reset_at or "unknown"
    return (
        f"[quota] remaining={remaining} estimated_cost={estimated_cost} "
        f"effective_floor={effective_floor(configured_floor)} reset_at={reset_at}"
    )


def bounded_batches(records: Iterable[Any], size: int = MAX_MUTATION_BATCH) -> list[list[Any]]:
    """Split records into batches no larger than `MAX_MUTATION_BATCH`."""
    if isinstance(size, bool) or not isinstance(size, int) or size < 1:
        raise QuotaError("batch-size-invalid", "batch size must be a positive integer")
    if size > MAX_MUTATION_BATCH:
        raise QuotaError(
            "batch-size-too-large",
            f"mutation batches may not exceed {MAX_MUTATION_BATCH} records; got {size}",
        )
    items = list(records)
    return [items[start : start + size] for start in range(0, len(items), size)]


class QuotaCycle:
    """One cached quota inventory per runtime cycle.

    Call `begin_cycle()` at the top of a tick; every `require()` inside that tick
    reuses the single reading. Reaching the reserve raises and does not re-fetch:
    there is no retry spin.
    """

    def __init__(self, fetch: Optional[Callable[[], Any]] = None) -> None:
        self._fetch = fetch
        self._snapshot: Optional[QuotaSnapshot] = None

    def begin_cycle(self) -> None:
        self._snapshot = None

    def snapshot(self) -> QuotaSnapshot:
        if self._snapshot is None:
            self._snapshot = read_graphql_quota(self._fetch)
        return self._snapshot

    def require(self, estimated_cost: int, configured_floor: Optional[int]) -> None:
        require_graphql_budget(self.snapshot(), estimated_cost, configured_floor)


# ───────────────────────────── CLI ─────────────────────────────


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(f"super-board-quota: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="super_board_runtime.quota", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="refuse an operation that would break the reserve")
    check.add_argument("--estimated-cost", type=int, required=True, help="GraphQL points")
    check.add_argument("--config", default=None, help="config supplying a raised floor")
    check.add_argument(
        "--payload", default=None, help="read a rate_limit payload from this file instead of gh"
    )
    summary = sub.add_parser(
        "summary", help="print the worker exit line; never fails, never spends"
    )
    summary.add_argument("--config", default=None, help="config supplying a raised floor")
    summary.add_argument(
        "--payload", default=None, help="read a rate_limit payload from this file instead of gh"
    )
    return parser


def _payload_fetch(payload: Optional[str]) -> Optional[Callable[[], Any]]:
    if not payload:
        return None
    payload_path = Path(payload)

    def fetch() -> Any:
        return json.loads(payload_path.read_text(encoding="utf-8"))

    return fetch


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "summary":
        # A worker calls this on its way out. It reports; it never decides, and
        # it never changes the caller's exit status — a config it cannot read
        # means it cannot state the enforced floor honestly, so it says so.
        floor: Optional[int] = None
        readable = True
        if args.config:
            try:
                floor = load_and_validate_config(Path(args.config)).minimum_graphql_reserve
            except ConfigError:
                readable = False
        snapshot = read_graphql_quota(_payload_fetch(args.payload)) if readable else UNAVAILABLE
        print(quota_summary_line(snapshot, floor))
        return EXIT_OK

    floor = None
    if args.config:
        try:
            floor = load_and_validate_config(Path(args.config)).minimum_graphql_reserve
        except ConfigError as exc:
            print(f"super-board-quota: invalid config: {exc}", file=sys.stderr)
            print(json.dumps({"ok": False, "reason": exc.reason}, sort_keys=True), file=sys.stderr)
            return EXIT_CONFIG

    snapshot = read_graphql_quota(_payload_fetch(args.payload))
    body = {
        "effective_floor": effective_floor(floor),
        "estimated_cost": args.estimated_cost,
        "ok": False,
        "quota_available": snapshot.available,
        "remaining": snapshot.remaining,
        "reset_at": snapshot.reset_at,
    }
    try:
        require_graphql_budget(snapshot, args.estimated_cost, floor)
    except QuotaError as exc:
        body["reason"] = exc.reason
        print(quota_log_line(snapshot, args.estimated_cost, floor), file=sys.stderr)
        print(json.dumps(body, sort_keys=True), file=sys.stderr)
        return exc.exit_code
    body["ok"] = True
    print(json.dumps(body, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
