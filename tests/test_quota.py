"""Task 6 — the immutable GraphQL reserve.

Pure stdlib `unittest`. No network, no `gh`.

Run directly:
  python -B tests/test_quota.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_quota.py' -v
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.quota import (  # noqa: E402
    IMMUTABLE_GRAPHQL_FLOOR,
    MAX_MUTATION_BATCH,
    MAX_PAGINATION_PAGES,
    QuotaCycle,
    QuotaError,
    QuotaExhausted,
    QuotaSnapshot,
    bounded_batches,
    effective_floor,
    QUOTA_SUMMARY_PREFIX,
    quota_log_line,
    quota_summary_line,
    read_graphql_quota,
    require_graphql_budget,
)


def _payload(remaining: int, limit: int = 5000, reset: int = 1_785_000_000) -> dict:
    return {"resources": {"graphql": {"remaining": remaining, "limit": limit, "reset": reset}}}


def _snapshot(remaining: int) -> QuotaSnapshot:
    return read_graphql_quota(fetch=lambda: _payload(remaining))


class FloorTests(unittest.TestCase):
    def test_the_floor_is_one_thousand_points(self) -> None:
        self.assertEqual(IMMUTABLE_GRAPHQL_FLOOR, 1000)

    def test_a_configured_floor_below_the_immutable_floor_is_raised(self) -> None:
        for configured in (0, 1, 200, 999, -5000, None):
            with self.subTest(configured=configured):
                self.assertEqual(effective_floor(configured), 1000)

    def test_a_configured_floor_above_the_immutable_floor_is_honoured(self) -> None:
        self.assertEqual(effective_floor(1001), 1001)
        self.assertEqual(effective_floor(2500), 2500)


class BudgetTests(unittest.TestCase):
    def test_remaining_minus_cost_must_stay_at_or_above_the_floor(self) -> None:
        snapshot = _snapshot(1103)
        require_graphql_budget(snapshot, 103, 1000)  # exactly at the floor is allowed
        with self.assertRaises(QuotaExhausted) as ctx:
            require_graphql_budget(snapshot, 104, 1000)
        self.assertEqual(ctx.exception.reason, "graphql-reserve-reached")
        self.assertEqual(ctx.exception.exit_code, 75)

    def test_a_raised_floor_is_enforced_not_just_recorded(self) -> None:
        snapshot = _snapshot(2000)
        require_graphql_budget(snapshot, 500, 1000)
        with self.assertRaises(QuotaExhausted):
            require_graphql_budget(snapshot, 500, 2000)

    def test_a_floor_below_one_thousand_cannot_buy_extra_headroom(self) -> None:
        snapshot = _snapshot(900)
        with self.assertRaises(QuotaExhausted):
            require_graphql_budget(snapshot, 1, 200)

    def test_an_unavailable_quota_response_is_never_permissive(self) -> None:
        def failing_fetch():
            raise RuntimeError("gh exited 1")

        snapshot = read_graphql_quota(fetch=failing_fetch)
        self.assertFalse(snapshot.available)
        self.assertIsNone(snapshot.remaining)
        with self.assertRaises(QuotaExhausted) as ctx:
            require_graphql_budget(snapshot, 1, 1000)
        self.assertEqual(ctx.exception.reason, "quota-unavailable")

    def test_a_malformed_quota_response_is_never_permissive(self) -> None:
        for payload in ({}, {"resources": {}}, {"resources": {"graphql": {}}},
                        {"resources": {"graphql": {"remaining": "lots"}}}, None, "nonsense"):
            with self.subTest(payload=payload):
                snapshot = read_graphql_quota(fetch=lambda p=payload: p)
                self.assertFalse(snapshot.available)
                with self.assertRaises(QuotaExhausted):
                    require_graphql_budget(snapshot, 1, 1000)

    def test_a_nonsense_estimated_cost_is_an_input_error_not_a_free_pass(self) -> None:
        snapshot = _snapshot(5000)
        for cost in (0, -1, "103", 1.5, None):
            with self.subTest(cost=cost):
                with self.assertRaises(QuotaError) as ctx:
                    require_graphql_budget(snapshot, cost, 1000)
                self.assertEqual(ctx.exception.reason, "quota-cost-invalid")


class NoFallbackCapacityTests(unittest.TestCase):
    """The old 5,000-point fallback and 200-point threshold must be gone."""

    GUARDED = (
        "scripts/super-board-gh-guard.sh",
        "scripts/super-board-run.sh",
        "scripts/super-board-wave-plan.sh",
        "scripts/super_board_runtime/quota.py",
    )

    def test_no_fabricated_fallback_capacity_remains(self) -> None:
        for relative in self.GUARDED:
            with self.subTest(path=relative):
                text = (_REPO_ROOT / relative).read_text(encoding="utf-8")
                self.assertNotIn(
                    "5000", text, f"{relative} still invents fallback capacity"
                )
                self.assertNotRegex(
                    text, r"-lt\s+200\b", f"{relative} still uses the 200-point threshold"
                )
                self.assertNotRegex(
                    text, r"remaining\s*<\s*200\b", f"{relative} still uses the 200-point threshold"
                )

    def test_reaching_the_reserve_stops_cleanly_with_no_sleep_and_no_retry(self) -> None:
        quota_source = (_REPO_ROOT / "scripts/super_board_runtime/quota.py").read_text(encoding="utf-8")
        self.assertNotIn("time.sleep", quota_source)
        self.assertNotIn("sleep(", quota_source)
        guard = (_REPO_ROOT / "scripts/super-board-gh-guard.sh").read_text(encoding="utf-8")
        self.assertNotRegex(
            guard, r"^\s*sleep\s", "the guard must not sleep through a reset"
        )
        self.assertNotIn("until reset", guard)

    def test_the_exhausted_exception_carries_the_quota_exit_code(self) -> None:
        self.assertEqual(QuotaExhausted("graphql-reserve-reached", "x").exit_code, 75)


class LogLineTests(unittest.TestCase):
    def test_the_log_line_carries_only_the_four_safe_fields(self) -> None:
        snapshot = _snapshot(4213)
        line = quota_log_line(snapshot, 103, 1000)
        fields = dict(re.findall(r"(\w+)=(\S+)", line))
        self.assertEqual(
            sorted(fields), ["effective_floor", "estimated_cost", "remaining", "reset_at"]
        )
        self.assertEqual(fields["remaining"], "4213")
        self.assertEqual(fields["estimated_cost"], "103")
        self.assertEqual(fields["effective_floor"], "1000")

    def test_the_log_line_never_carries_a_token_header_or_raw_payload(self) -> None:
        snapshot = _snapshot(4213)
        line = quota_log_line(snapshot, 103, 1000)
        for forbidden in ("token", "Authorization", "authorization", "Bearer", "cookie", "resources"):
            self.assertNotIn(forbidden, line)
        # `limit` is deliberately absent: it is not needed to act and only adds noise.
        self.assertNotIn("limit", line)

    def test_an_unavailable_snapshot_still_produces_a_safe_line(self) -> None:
        line = quota_log_line(read_graphql_quota(fetch=lambda: None), 103, 1000)
        self.assertIn("remaining=unknown", line)
        self.assertIn("effective_floor=1000", line)


class ExitSummaryTests(unittest.TestCase):
    """The `gh-quota-on-exit:` line every worker's handoff comment must carry.

    `sb_gh_guard_summary` used to run the quota check with both streams sent to
    /dev/null and then return 0, so the line the references promise could never
    be produced. Its shape is pinned here and its wiring in
    `tests/test-gh-guard-summary.sh`.
    """

    def test_the_summary_carries_only_the_three_safe_fields(self) -> None:
        line = quota_summary_line(_snapshot(4213), 1000)
        self.assertTrue(line.startswith(QUOTA_SUMMARY_PREFIX), line)
        fields = dict(re.findall(r"(\w+)=(\S+)", line))
        self.assertEqual(sorted(fields), ["floor", "graphql", "reset"])
        self.assertEqual(fields["graphql"], "4213")
        self.assertEqual(fields["floor"], "1000")

    def test_the_summary_never_carries_a_token_header_or_raw_payload(self) -> None:
        line = quota_summary_line(_snapshot(4213), 1000)
        for forbidden in (
            "token", "Authorization", "authorization", "Bearer", "cookie", "resources", "limit"
        ):
            self.assertNotIn(forbidden, line)

    def test_the_summary_reports_the_raised_floor_not_the_minimum(self) -> None:
        self.assertIn("floor=4000", quota_summary_line(_snapshot(4213), 4000))

    def test_an_unreadable_quota_is_a_marker_not_a_fabricated_balance(self) -> None:
        line = quota_summary_line(read_graphql_quota(fetch=lambda: None), 1000)
        self.assertTrue(line.startswith(QUOTA_SUMMARY_PREFIX), line)
        self.assertIn("unavailable", line)
        self.assertNotIn("graphql=", line)

    def test_the_summary_cli_always_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "rate-limit.json"
            payload.write_text(json.dumps(_payload(4213)), encoding="utf-8")
            broken = Path(tmp) / "broken.json"
            broken.write_text('{"resources": {}}', encoding="utf-8")
            missing = Path(tmp) / "absent.json"
            for path, expected in (
                (payload, "graphql=4213"),
                (broken, "unavailable"),
                (missing, "unavailable"),
            ):
                with self.subTest(payload=path.name):
                    result = subprocess.run(
                        [
                            sys.executable, "-B", "-m", "super_board_runtime.quota",
                            "summary", "--payload", str(path),
                        ],
                        capture_output=True,
                        text=True,
                        cwd=str(_SCRIPTS),
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(QUOTA_SUMMARY_PREFIX, result.stdout)
                    self.assertIn(expected, result.stdout)

    def test_an_unreadable_config_cannot_fail_the_summary(self) -> None:
        # A worker's last act must not be able to change the status it exits
        # with — and a floor it cannot read is a floor it must not claim.
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text("{not json", encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable, "-B", "-m", "super_board_runtime.quota",
                    "summary", "--config", str(config),
                ],
                capture_output=True,
                text=True,
                cwd=str(_SCRIPTS),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unavailable", result.stdout)


class BoundedWorkTests(unittest.TestCase):
    def test_mutation_batches_never_exceed_twenty_five_records(self) -> None:
        self.assertEqual(MAX_MUTATION_BATCH, 25)
        batches = bounded_batches(list(range(60)))
        self.assertEqual([len(batch) for batch in batches], [25, 25, 10])
        self.assertEqual([item for batch in batches for item in batch], list(range(60)))

    def test_a_caller_cannot_ask_for_a_bigger_batch(self) -> None:
        with self.assertRaises(QuotaError) as ctx:
            bounded_batches(list(range(30)), size=26)
        self.assertEqual(ctx.exception.reason, "batch-size-too-large")
        self.assertEqual(len(bounded_batches(list(range(30)), size=10)), 3)

    def test_pagination_is_bounded(self) -> None:
        self.assertEqual(MAX_PAGINATION_PAGES, 20)
        self.assertIsInstance(MAX_PAGINATION_PAGES, int)


class CycleTests(unittest.TestCase):
    def test_one_cached_inventory_per_runtime_cycle(self) -> None:
        calls = []

        def fetch():
            calls.append(1)
            return _payload(5000 - 100 * len(calls))

        cycle = QuotaCycle(fetch=fetch)
        first = cycle.snapshot()
        second = cycle.snapshot()
        cycle.require(10, 1000)
        cycle.require(10, 1000)
        self.assertEqual(len(calls), 1, "the inventory is read once per cycle")
        self.assertEqual(first, second)

        cycle.begin_cycle()
        cycle.snapshot()
        self.assertEqual(len(calls), 2, "a new cycle reads a fresh inventory")

    def test_a_cycle_that_hits_the_reserve_does_not_retry(self) -> None:
        calls = []

        def fetch():
            calls.append(1)
            return _payload(1050)

        cycle = QuotaCycle(fetch=fetch)
        with self.assertRaises(QuotaExhausted):
            cycle.require(100, 1000)
        with self.assertRaises(QuotaExhausted):
            cycle.require(100, 1000)
        self.assertEqual(len(calls), 1, "no re-fetch, no retry spin")


class QuotaCliTests(unittest.TestCase):
    def _run(self, *args: str, payload: dict | None = None) -> subprocess.CompletedProcess:
        env_args = list(args)
        if payload is not None:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
                json.dump(payload, handle)
                env_args += ["--payload", handle.name]
        return subprocess.run(
            [sys.executable, "-B", "-m", "super_board_runtime.quota", *env_args],
            capture_output=True,
            text=True,
            cwd=str(_SCRIPTS),
        )

    def test_a_healthy_quota_exits_zero_with_json(self) -> None:
        result = self._run("check", "--estimated-cost", "103", payload=_payload(4000))
        self.assertEqual(result.returncode, 0, result.stderr)
        body = json.loads(result.stdout)
        self.assertTrue(body["ok"])
        self.assertEqual(body["effective_floor"], 1000)
        self.assertEqual(list(body), sorted(body))

    def test_reaching_the_reserve_exits_75(self) -> None:
        result = self._run("check", "--estimated-cost", "103", payload=_payload(1050))
        self.assertEqual(result.returncode, 75)
        self.assertIn("graphql-reserve-reached", result.stderr)

    def test_an_unreadable_quota_exits_75(self) -> None:
        result = self._run("check", "--estimated-cost", "103", payload={"resources": {}})
        self.assertEqual(result.returncode, 75)
        self.assertIn("quota-unavailable", result.stderr)

    def test_a_floor_below_the_immutable_floor_in_config_exits_65(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "project": {"owner": "Bavariance", "number": 1},
                        "minimum_graphql_reserve": 200,
                    }
                ),
                encoding="utf-8",
            )
            result = self._run("check", "--estimated-cost", "1", "--config", str(path), payload=_payload(4000))
        self.assertEqual(result.returncode, 65)
        self.assertIn("graphql-reserve-below-floor", result.stderr)

    def test_a_bad_invocation_exits_64(self) -> None:
        result = self._run("frobnicate")
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
