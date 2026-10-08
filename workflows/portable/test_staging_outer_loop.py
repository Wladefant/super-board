#!/usr/bin/env python3
"""
workflows/portable/test_staging_outer_loop.py — Unit tests for Staging Outer-Loop Poller.

Tests all required acceptance criteria:
  1. Allowlist refusal (production compose id exits 2 and makes no HTTP call)
  2. Dedupe (same key twice creates one issue, then comments on second run)
  3. Spike threshold boundaries (buckets and ERROR line counts)
  4. Error-deployment detection with shuffled createdAt order
  5. Secret and token redaction in deployment logs
  6. Container down detection (unhealthy, restarting, non-running)
  7. Spike comment rate-limiting (at most 1 comment per hour)
"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from staging_outer_loop import (
    ALLOWED_COMPOSE_IDS,
    ALLOWED_SERVER_IDS,
    STAGING_COMPOSE_ID,
    STAGING_SERVER_ID,
    DokployReadOnlyClient,
    Incident,
    check_allowlist,
    check_container_down,
    check_deploy_failures,
    check_runtime_error_spikes,
    extract_service_name,
    is_error_log_line,
    parse_api_stats,
    parse_datetime,
    parse_log_timestamp,
    redact_log_content,
    run_outer_loop,
)


class TestStagingOuterLoopAllowlist(unittest.TestCase):
    """Allowlist refusal: production compose or server IDs exit 2 with no network calls."""

    def test_allowlist_permits_exact_staging(self):
        # Should not raise
        check_allowlist(STAGING_COMPOSE_ID, STAGING_SERVER_ID)

    def test_allowlist_refuses_production_compose_id(self):
        production_compose_id = "vpyL-7TDEUREH6Uo_y1sb"
        with self.assertRaises(SystemExit) as ctx:
            check_allowlist(production_compose_id, STAGING_SERVER_ID)
        self.assertEqual(ctx.exception.code, 2)

    def test_allowlist_refuses_wrong_server_id(self):
        production_server_id = "akamai-iad-prod-12345"
        with self.assertRaises(SystemExit) as ctx:
            check_allowlist(STAGING_COMPOSE_ID, production_server_id)
        self.assertEqual(ctx.exception.code, 2)

    def test_client_refuses_production_compose_id_without_network_calls(self):
        production_compose_id = "vpyL-7TDEUREH6Uo_y1sb"
        with patch("urllib.request.urlopen") as mock_urlopen:
            with self.assertRaises(SystemExit) as ctx:
                DokployReadOnlyClient(
                    base_url="https://hosting.wladefant.de/api",
                    api_key="fake-key",
                    compose_id=production_compose_id,
                    server_id=STAGING_SERVER_ID,
                )
            self.assertEqual(ctx.exception.code, 2)
            mock_urlopen.assert_not_called()


class TestStagingOuterLoopDedupe(unittest.TestCase):
    """Deduplication and throttling: deploy failures alert once and never re-alert/comment, container down throttles hourly."""

    @patch("staging_outer_loop.send_telegram_alert")
    @patch("staging_outer_loop.add_github_comment")
    @patch("staging_outer_loop.create_github_issue")
    @patch("staging_outer_loop.find_open_issue_by_key")
    def test_deploy_failure_alerts_once_and_never_recreates_or_comments(
        self,
        mock_find_issue,
        mock_create_issue,
        mock_add_comment,
        mock_send_tg,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            now = datetime(2026, 9, 26, 22, 30, 0, tzinfo=timezone.utc)
            fixture_dep = {
                "deployments": [
                    {
                        "deploymentId": "dep-fixture-001",
                        "status": "error",
                        "title": "Merge PR #5555",
                        "description": "Commit: 1234567890abcdef",
                        "errorMessage": "Build failed on test step",
                        "createdAt": "2026-09-26T22:25:00.000Z",
                        "finishedAt": "2026-09-26T22:26:00.000Z",
                        "logs": "Error: build step failed",
                    }
                ],
                "containers": [],
            }
            fixture_json = json.dumps(fixture_dep)

            # --- RUN 1: Issue does not exist yet ---
            mock_find_issue.return_value = None
            mock_create_issue.return_value = "https://github.com/Bavariance/polysimulator/issues/9001"
            mock_send_tg.return_value = True

            rc1 = run_outer_loop(
                state_path=state_file,
                inject_fixture=fixture_json,
                now_utc=now,
            )
            self.assertEqual(rc1, 0)
            mock_create_issue.assert_called_once()
            mock_send_tg.assert_called_once_with(
                "Staging deploy failed: Merge PR #5555 (ID: dep-fixture-001)",
                "https://github.com/Bavariance/polysimulator/issues/9001",
                dry_run=False,
            )
            mock_add_comment.assert_not_called()

            # Verify state was persisted with seen deployment ID
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIn("dep-fixture-001", state_data["last_seen_deployment_ids"])

            # Reset mocks for RUN 2
            mock_create_issue.reset_mock()
            mock_send_tg.reset_mock()
            mock_add_comment.reset_mock()

            # --- RUN 2: Issue still open ---
            mock_find_issue.return_value = {
                "number": 9001,
                "url": "https://github.com/Bavariance/polysimulator/issues/9001",
                "body": "outer-loop-key: deploy:dep-fixture-001\n\nIncident details...",
            }

            rc2 = run_outer_loop(
                state_path=state_file,
                inject_fixture=fixture_json,
                now_utc=now + timedelta(minutes=5),
            )
            self.assertEqual(rc2, 0)
            # Must NOT create a second issue
            mock_create_issue.assert_not_called()
            # Must NOT send Telegram
            mock_send_tg.assert_not_called()
            # Must NOT comment on deploy failures (deployments are static)
            mock_add_comment.assert_not_called()

            # Reset mocks for RUN 3
            mock_create_issue.reset_mock()
            mock_send_tg.reset_mock()
            mock_add_comment.reset_mock()

            # --- RUN 3: Issue has been closed (find_open_issue_by_key returns None) ---
            mock_find_issue.return_value = None

            rc3 = run_outer_loop(
                state_path=state_file,
                inject_fixture=fixture_json,
                now_utc=now + timedelta(minutes=10),
            )
            self.assertEqual(rc3, 0)
            # Must NOT recreate issue or alert because dep-fixture-001 is already in last_seen_deployment_ids
            mock_create_issue.assert_not_called()
            mock_send_tg.assert_not_called()
            mock_add_comment.assert_not_called()

    @patch("staging_outer_loop.send_telegram_alert")
    @patch("staging_outer_loop.add_github_comment")
    @patch("staging_outer_loop.create_github_issue")
    @patch("staging_outer_loop.find_open_issue_by_key")
    def test_first_run_seeds_deployment_ids_without_alerting(
        self,
        mock_find_issue,
        mock_create_issue,
        mock_add_comment,
        mock_send_tg,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            now = datetime(2026, 9, 26, 22, 0, 0, tzinfo=timezone.utc)
            fixture_dep = {
                "deployments": [
                    {
                        "deploymentId": "dep-historical-001",
                        "status": "error",
                        "title": "Historical deploy error",
                        "description": "Commit: 1111",
                        "errorMessage": "Old build failure",
                        "createdAt": "2026-09-26T21:00:00.000Z",
                    },
                    {
                        "deploymentId": "dep-historical-002",
                        "status": "done",
                        "title": "Historical deploy success",
                        "description": "Commit: 2222",
                        "createdAt": "2026-09-26T21:30:00.000Z",
                    },
                ],
                "containers": [],
            }
            mock_find_issue.return_value = None

            # Run 1: with seed_first_run=True, must seed without alerting
            rc1 = run_outer_loop(
                state_path=state_file,
                inject_fixture=json.dumps(fixture_dep),
                now_utc=now,
                seed_first_run=True,
            )
            self.assertEqual(rc1, 0)
            mock_create_issue.assert_not_called()
            mock_send_tg.assert_not_called()

            # Verify state was seeded
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertTrue(state_data.get("initialized"))
            self.assertIn("dep-historical-001", state_data["last_seen_deployment_ids"])
            self.assertIn("dep-historical-002", state_data["last_seen_deployment_ids"])

            # Run 2: a NEW deployment failure arrives
            fixture_dep["deployments"].append({
                "deploymentId": "dep-new-003",
                "status": "error",
                "title": "New deploy error",
                "description": "Commit: 3333",
                "errorMessage": "Fresh build failure",
                "createdAt": "2026-09-26T22:05:00.000Z",
            })
            mock_create_issue.return_value = "https://github.com/Bavariance/polysimulator/issues/9002"
            mock_send_tg.return_value = True

            rc2 = run_outer_loop(
                state_path=state_file,
                inject_fixture=json.dumps(fixture_dep),
                now_utc=now + timedelta(minutes=5),
                seed_first_run=True,
            )
            self.assertEqual(rc2, 0)
            mock_create_issue.assert_called_once()
            mock_send_tg.assert_called_once()
            state_data2 = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIn("dep-new-003", state_data2["last_seen_deployment_ids"])

    @patch("staging_outer_loop.send_telegram_alert")
    @patch("staging_outer_loop.add_github_comment")
    @patch("staging_outer_loop.create_github_issue")
    @patch("staging_outer_loop.find_open_issue_by_key")
    def test_deployment_running_on_poll_1_then_error_on_poll_2_creates_exactly_one_issue(
        self,
        mock_find_issue,
        mock_create_issue,
        mock_add_comment,
        mock_send_tg,
    ):
        """A deployment seen as 'running' on poll 1 must not be marked alerted, and must alert when 'error' on poll 2."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            now = datetime(2026, 9, 26, 22, 0, 0, tzinfo=timezone.utc)
            mock_find_issue.return_value = None

            # Poll 1: deployment is running
            fixture_poll1 = {
                "deployments": [
                    {
                        "deploymentId": "dep-flight-001",
                        "status": "running",
                        "title": "In-flight build",
                        "description": "Commit: aaaa",
                        "createdAt": "2026-09-26T22:00:00.000Z",
                    }
                ],
                "containers": [],
            }
            rc1 = run_outer_loop(
                state_path=state_file,
                inject_fixture=json.dumps(fixture_poll1),
                now_utc=now,
                seed_first_run=True,
            )
            self.assertEqual(rc1, 0)
            mock_create_issue.assert_not_called()
            mock_send_tg.assert_not_called()

            # Verify dep-flight-001 is NOT in alerted_deploy_ids
            state_data1 = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertNotIn("dep-flight-001", state_data1.get("alerted_deploy_ids", []))

            # Poll 2: 5 minutes later, deployment turned to error
            fixture_poll2 = {
                "deployments": [
                    {
                        "deploymentId": "dep-flight-001",
                        "status": "error",
                        "title": "In-flight build failed",
                        "description": "Commit: aaaa",
                        "errorMessage": "Command failed with exit code 1",
                        "createdAt": "2026-09-26T22:00:00.000Z",
                        "finishedAt": "2026-09-26T22:04:00.000Z",
                    }
                ],
                "containers": [],
            }
            mock_create_issue.return_value = "https://github.com/Bavariance/polysimulator/issues/9005"
            mock_send_tg.return_value = True

            rc2 = run_outer_loop(
                state_path=state_file,
                inject_fixture=json.dumps(fixture_poll2),
                now_utc=now + timedelta(minutes=5),
                seed_first_run=True,
            )
            self.assertEqual(rc2, 0)
            mock_create_issue.assert_called_once()
            mock_send_tg.assert_called_once()

            # Verify dep-flight-001 is now in alerted_deploy_ids
            state_data2 = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIn("dep-flight-001", state_data2.get("alerted_deploy_ids", []))

            # Poll 3: 5 minutes later, deployment still in Dokploy list as error
            mock_create_issue.reset_mock()
            mock_send_tg.reset_mock()
            rc3 = run_outer_loop(
                state_path=state_file,
                inject_fixture=json.dumps(fixture_poll2),
                now_utc=now + timedelta(minutes=10),
                seed_first_run=True,
            )
            self.assertEqual(rc3, 0)
            # Must NOT create a second issue
            mock_create_issue.assert_not_called()
            mock_send_tg.assert_not_called()

    @patch("staging_outer_loop.send_telegram_alert")
    @patch("staging_outer_loop.add_github_comment")
    @patch("staging_outer_loop.create_github_issue")
    @patch("staging_outer_loop.find_open_issue_by_key")
    def test_issue_creation_failure_retries_on_next_poll_until_success(
        self,
        mock_find_issue,
        mock_create_issue,
        mock_add_comment,
        mock_send_tg,
    ):
        """When issue creation fails, the ID must not be marked alerted, allowing retry on the next poll."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            now = datetime(2026, 9, 26, 22, 0, 0, tzinfo=timezone.utc)
            fixture_dep = {
                "deployments": [
                    {
                        "deploymentId": "dep-retry-001",
                        "status": "error",
                        "title": "Build failure with gh outage",
                        "description": "Commit: bbbb",
                        "errorMessage": "Exit 1",
                        "createdAt": "2026-09-26T22:00:00.000Z",
                        "finishedAt": "2026-09-26T22:01:00.000Z",
                    }
                ],
                "containers": [],
            }
            mock_find_issue.return_value = None

            # Poll 1: create_github_issue fails (returns None due to GitHub outage/rate limit)
            mock_create_issue.return_value = None

            rc1 = run_outer_loop(
                state_path=state_file,
                inject_fixture=json.dumps(fixture_dep),
                now_utc=now,
            )
            self.assertEqual(rc1, 0)
            mock_create_issue.assert_called_once()
            mock_send_tg.assert_not_called()

            # Verify dep-retry-001 is NOT in alerted_deploy_ids because creation failed
            state_data1 = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertNotIn("dep-retry-001", state_data1.get("alerted_deploy_ids", []))

            # Poll 2: 5 minutes later, GitHub is back up and create succeeds
            mock_create_issue.reset_mock()
            mock_create_issue.return_value = "https://github.com/Bavariance/polysimulator/issues/9006"
            mock_send_tg.return_value = True

            rc2 = run_outer_loop(
                state_path=state_file,
                inject_fixture=json.dumps(fixture_dep),
                now_utc=now + timedelta(minutes=5),
            )
            self.assertEqual(rc2, 0)
            mock_create_issue.assert_called_once()
            mock_send_tg.assert_called_once()

            # Verify dep-retry-001 IS now in alerted_deploy_ids
            state_data2 = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertIn("dep-retry-001", state_data2.get("alerted_deploy_ids", []))

            # Poll 3: next poll skips because already alerted
            mock_create_issue.reset_mock()
            mock_send_tg.reset_mock()
            rc3 = run_outer_loop(
                state_path=state_file,
                inject_fixture=json.dumps(fixture_dep),
                now_utc=now + timedelta(minutes=10),
            )
            self.assertEqual(rc3, 0)
            mock_create_issue.assert_not_called()
            mock_send_tg.assert_not_called()

    @patch("staging_outer_loop.add_github_comment")
    @patch("staging_outer_loop.create_github_issue")
    @patch("staging_outer_loop.find_open_issue_by_key")
    def test_container_down_comments_at_most_once_per_hour(
        self,
        mock_find_issue,
        mock_create_issue,
        mock_add_comment,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            now = datetime(2026, 9, 26, 22, 0, 0, tzinfo=timezone.utc)
            fixture_down = json.dumps({
                "deployments": [],
                "containers": [
                    {
                        "name": "polysimulator-staging-iad-v09j4g-backend-1",
                        "state": "exited",
                        "status": "Exited (1) 5 minutes ago",
                    }
                ],
            })
            mock_find_issue.return_value = {
                "number": 7777,
                "url": "https://github.com/Bavariance/polysimulator/issues/7777",
                "body": "outer-loop-key: container:polysimulator-staging-iad-v09j4g-backend-1\n\nExisting container down...",
            }
            mock_add_comment.return_value = True

            # Run 1 at 22:05 -> should comment
            t1 = now + timedelta(minutes=5)
            run_outer_loop(state_path=state_file, inject_fixture=fixture_down, now_utc=t1)
            mock_add_comment.assert_called_once()

            # Run 2 at 22:20 (15 min later) -> should skip comment (within 1 hour)
            mock_add_comment.reset_mock()
            t2 = now + timedelta(minutes=20)
            run_outer_loop(state_path=state_file, inject_fixture=fixture_down, now_utc=t2)
            mock_add_comment.assert_not_called()

            # Run 3 at 23:10 (65 min later) -> should comment again
            t3 = now + timedelta(minutes=70)
            run_outer_loop(state_path=state_file, inject_fixture=fixture_down, now_utc=t3)
            mock_add_comment.assert_called_once()


class TestStagingOuterLoopSpikeThresholds(unittest.TestCase):
    """Spike threshold boundaries: 1-minute bucket conditions and ERROR line counts."""

    def setUp(self):
        self.now = datetime(2026, 9, 26, 22, 30, 0, tzinfo=timezone.utc)

    def test_bucket_spike_boundary_2_of_5_does_not_trigger(self):
        # 2 buckets with >=5 errors and >=20% rate -> NO spike
        containers = [{"name": "polysimulator-staging-iad-v09j4g-backend-1", "containerId": "c1"}]
        # Create log lines for 2 spiking buckets and 3 clean buckets
        logs = []
        # Minute 0: 10 reqs, 3 errs (30% rate, 3 errs < 5 -> not spiking)
        # Minute 1: 20 reqs, 5 errs (25% rate, 5 errs -> spiking bucket 1)
        # Minute 2: 20 reqs, 6 errs (30% rate, 6 errs -> spiking bucket 2)
        # Minute 3: 20 reqs, 2 errs (10% rate < 20% -> not spiking)
        # Minute 4: 20 reqs, 1 errs (5% rate -> not spiking)
        times = [
            (self.now - timedelta(seconds=30), 10, 3),
            (self.now - timedelta(seconds=90), 20, 5),
            (self.now - timedelta(seconds=150), 20, 6),
            (self.now - timedelta(seconds=210), 20, 2),
            (self.now - timedelta(seconds=270), 20, 1),
        ]
        for dt, reqs, errs in times:
            iso = dt.isoformat()
            logs.append(f"{iso} " + json.dumps({
                "timestamp": iso,
                "logger": "api.stats",
                "data": {"request_count": reqs, "error_count": errs},
            }))
        containers[0]["logs"] = "\n".join(logs)

        incidents = check_runtime_error_spikes(containers, None, now_utc=self.now)
        self.assertEqual(len(incidents), 0)

    def test_bucket_spike_boundary_3_of_5_triggers_spike(self):
        # 3 buckets with >=5 errors and >=20% rate -> SPIKE
        containers = [{"name": "polysimulator-staging-iad-v09j4g-backend-1", "containerId": "c1"}]
        times = [
            (self.now - timedelta(seconds=30), 20, 5),   # 25% -> bucket 1
            (self.now - timedelta(seconds=90), 20, 5),   # 25% -> bucket 2
            (self.now - timedelta(seconds=150), 20, 6),  # 30% -> bucket 3
            (self.now - timedelta(seconds=210), 20, 1),  # not spiking
            (self.now - timedelta(seconds=270), 20, 1),  # not spiking
        ]
        logs = []
        for dt, reqs, errs in times:
            iso = dt.isoformat()
            logs.append(f"{iso} " + json.dumps({
                "timestamp": iso,
                "logger": "api.stats",
                "data": {"request_count": reqs, "error_count": errs},
            }))
        containers[0]["logs"] = "\n".join(logs)

        incidents = check_runtime_error_spikes(containers, None, now_utc=self.now)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].signal, "error_spike")
        self.assertEqual(incidents[0].service, "backend")
        self.assertEqual(incidents[0].key, "spike:backend:2026-09-26T22")


    def test_parse_api_stats_server_error_count_precedence_over_4xx(self):
        # Only 5xx count as server errors. error_count also counts 4xx (401/404 from clients).
        # When server_error_count is present, it must take precedence over error_count.
        line_with_server_errors = (
            '2026-10-05T06:00:00Z {"logger": "api.stats", "data": '
            '{"request_count": 100, "error_count": 25, "server_error_count": 2}}'
        )
        stats = parse_api_stats(line_with_server_errors)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["requests"], 100)
        self.assertEqual(stats["errors"], 2)

        # Older containers without server_error_count fall back to error_count
        line_fallback = (
            '2026-10-05T06:00:00Z {"logger": "api.stats", "data": '
            '{"request_count": 100, "error_count": 5}}'
        )
        stats_fb = parse_api_stats(line_fallback)
        self.assertIsNotNone(stats_fb)
        self.assertEqual(stats_fb["requests"], 100)
        self.assertEqual(stats_fb["errors"], 5)

    def test_error_line_count_boundary_49_does_not_trigger(self):
        # 49 ERROR lines in 10 minutes -> NO spike
        containers = [{"name": "polysimulator-staging-iad-v09j4g-backend-daemon-1", "containerId": "c2"}]
        logs = []
        for i in range(49):
            dt = self.now - timedelta(minutes=1, seconds=i * 5)
            logs.append(f"{dt.isoformat()} " + json.dumps({
                "timestamp": dt.isoformat(),
                "level": "ERROR",
                "message": f"Error event #{i}",
            }))
        containers[0]["logs"] = "\n".join(logs)

        incidents = check_runtime_error_spikes(containers, None, now_utc=self.now)
        self.assertEqual(len(incidents), 0)

    def test_error_line_count_boundary_50_triggers_spike(self):
        # 50 ERROR lines in 10 minutes -> SPIKE
        containers = [{"name": "polysimulator-staging-iad-v09j4g-backend-daemon-1", "containerId": "c2"}]
        logs = []
        for i in range(50):
            dt = self.now - timedelta(minutes=1, seconds=i * 5)
            logs.append(f"{dt.isoformat()} " + json.dumps({
                "timestamp": dt.isoformat(),
                "level": "ERROR",
                "message": f"Error event #{i}",
            }))
        containers[0]["logs"] = "\n".join(logs)

        incidents = check_runtime_error_spikes(containers, None, now_utc=self.now)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].signal, "error_spike")
        self.assertEqual(incidents[0].service, "backend-daemon")
        self.assertEqual(incidents[0].key, "spike:backend-daemon:2026-09-26T22")


class TestStagingOuterLoopDeploySorting(unittest.TestCase):
    """Error-deployment detection with shuffled createdAt order."""

    def test_detects_error_deployment_regardless_of_array_order(self):
        # Shuffled deployments: error is at index 2, with older and newer done deployments
        deps = [
            {
                "deploymentId": "dep-newest-done",
                "status": "done",
                "createdAt": "2026-09-26T22:20:00.000Z",
            },
            {
                "deploymentId": "dep-older-done",
                "status": "done",
                "createdAt": "2026-09-26T21:00:00.000Z",
            },
            {
                "deploymentId": "dep-middle-error",
                "status": "error",
                "title": "PR #5610 deploy",
                "description": "Commit: fedcba0987654321",
                "errorMessage": "Database connection timeout during migration",
                "createdAt": "2026-09-26T21:45:00.000Z",
                "finishedAt": "2026-09-26T21:46:00.000Z",
                "logs": "Alembic error: connection timeout",
            },
        ]
        incidents = check_deploy_failures(deps, None)
        self.assertEqual(len(incidents), 1)
        inc = incidents[0]
        self.assertEqual(inc.key, "deploy:dep-middle-error")
        self.assertEqual(inc.signal, "deploy_failed")
        self.assertIn("dep-middle-error", inc.body)
        self.assertIn("Database connection timeout", inc.body)


class TestStagingOuterLoopRedaction(unittest.TestCase):
    """Secret and token redaction in deployment logs and incident descriptions."""

    def test_redacts_tokens_and_credentials_in_logs(self):
        raw_log = (
            "Connecting with token bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz1234\n"
            "Auth header: Bearer my_secret_token_1234567890abcdef\n"
            "Connecting to db password='super_secret_db_password'\n"
            "File path: C:\\Users\\wkiri\\private\\key.pem\n"
        )
        redacted = redact_log_content(raw_log)
        self.assertNotIn("bot123456789", redacted)
        self.assertNotIn("my_secret_token", redacted)
        self.assertNotIn("super_secret_db_password", redacted)
        self.assertNotIn("wkiri", redacted)

    def test_deploy_incident_body_applies_redaction(self):
        dep = {
            "deploymentId": "dep-secret-test",
            "status": "error",
            "title": "Deploy with token leak",
            "description": "Commit: 12345",
            "errorMessage": "Failed at step with ghp_123456789012345678901234567890",
            "createdAt": "2026-09-26T22:00:00.000Z",
            "logs": "Secret found in env: Bearer sensitive_bearer_token_xyz_12345",
        }
        incidents = check_deploy_failures([dep], None)
        self.assertEqual(len(incidents), 1)
        body = incidents[0].body
        self.assertNotIn("sensitive_bearer_token", body)
        self.assertTrue("<redacted" in body or "[REDACTED" in body)


class TestStagingOuterLoopContainerDown(unittest.TestCase):
    """Container down detection: unhealthy, restarting, or non-running."""

    def setUp(self):
        self.now = datetime(2026, 9, 26, 22, 30, 0, tzinfo=timezone.utc)

    def test_created_replacement_with_running_sibling_is_ignored(self):
        prefix = "polysimulator-staging-iad-v09j4g-"
        for service in ("frontend", "backend", "backend-daemon"):
            for renamed in (False, True):
                with self.subTest(service=service, renamed=renamed):
                    old_name = prefix + service + "-1"
                    if renamed:
                        old_name = "4396f8b432ac_" + old_name
                    containers = [
                        {"name": old_name, "state": "created", "status": "created"},
                        {"name": prefix + service + "-2", "state": "running", "status": "Up 1 minute"},
                    ]
                    self.assertEqual(check_container_down(containers, self.now), [])
                    self.assertEqual(check_container_down(list(reversed(containers)), self.now), [])

    def test_created_without_same_service_running_sibling_still_alerts(self):
        prefix = "polysimulator-staging-iad-v09j4g-"
        containers = [
            {"name": prefix + "backend-1", "state": "created"},
            {"name": prefix + "backend-daemon-1", "state": "running"},
        ]
        incidents = check_container_down(containers, self.now)
        self.assertEqual([i.service for i in incidents], ["backend"])

    def test_unhealthy_running_sibling_still_alerts(self):
        prefix = "polysimulator-staging-iad-v09j4g-"
        containers = [
            {"name": prefix + "frontend-1", "state": "created"},
            {"name": prefix + "frontend-2", "state": "running", "status": "Up (unhealthy)"},
        ]
        incidents = check_container_down(containers, self.now)
        self.assertEqual(len(incidents), 1)
        self.assertIn("unhealthy", incidents[0].body)

    def test_exited_sibling_is_not_suppressed(self):
        prefix = "polysimulator-staging-iad-v09j4g-"
        containers = [
            {"name": prefix + "backend-1", "state": "exited"},
            {"name": prefix + "backend-2", "state": "running"},
        ]
        self.assertEqual(len(check_container_down(containers, self.now)), 1)

    def test_detects_unhealthy_container(self):
        containers = [
            {
                "name": "polysimulator-staging-iad-v09j4g-frontend-4",
                "state": "running",
                "status": "Up 20 minutes (unhealthy)",
            }
        ]
        incidents = check_container_down(containers, now_utc=self.now)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].key, "down:frontend:2026-09-26T22")
        self.assertEqual(incidents[0].signal, "container_down")
        self.assertEqual(incidents[0].service, "frontend")

    def test_detects_restarting_container(self):
        containers = [
            {
                "name": "polysimulator-staging-iad-v09j4g-backend-6",
                "state": "running",
                "status": "Restarting (1) 5 seconds ago",
            }
        ]
        incidents = check_container_down(containers, now_utc=self.now)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].key, "down:backend:2026-09-26T22")

    def test_detects_exited_container(self):
        containers = [
            {
                "name": "polysimulator-staging-iad-v09j4g-redis-1",
                "state": "exited",
                "status": "Exited (137) 1 minute ago",
            }
        ]
        incidents = check_container_down(containers, now_utc=self.now)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].key, "down:redis:2026-09-26T22")

    def test_healthy_running_container_produces_no_incidents(self):
        containers = [
            {
                "name": "polysimulator-staging-iad-v09j4g-frontend-4",
                "state": "running",
                "status": "Up 2 hours (healthy)",
            },
            {
                "name": "polysimulator-staging-iad-v09j4g-backend-6",
                "state": "running",
                "status": "Up 2 hours (healthy)",
            },
        ]
        incidents = check_container_down(containers, now_utc=self.now)
        self.assertEqual(len(incidents), 0)


class TestStagingOuterLoopSpikeRateLimiting(unittest.TestCase):
    """Spike comment rate-limiting: at most one comment per hour on existing issue."""

    @patch("staging_outer_loop.add_github_comment")
    @patch("staging_outer_loop.create_github_issue")
    @patch("staging_outer_loop.find_open_issue_by_key")
    def test_spike_comments_at_most_once_per_hour(
        self,
        mock_find_issue,
        mock_create_issue,
        mock_add_comment,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            now = datetime(2026, 9, 26, 22, 0, 0, tzinfo=timezone.utc)

            def make_fixture(check_time):
                err_logs = "\n".join([
                    f"{check_time.isoformat()} " + json.dumps({"timestamp": check_time.isoformat(), "level": "ERROR"})
                    for _ in range(50)
                ])
                return json.dumps({
                    "deployments": [],
                    "containers": [
                        {
                            "name": "polysimulator-staging-iad-v09j4g-backend-1",
                            "state": "running",
                            "status": "Up 1 hour (healthy)",
                            "logs": err_logs,
                        }
                    ],
                })

            mock_find_issue.return_value = {
                "number": 8888,
                "url": "https://github.com/Bavariance/polysimulator/issues/8888",
                "body": "outer-loop-key: spike:backend:2026-09-26T22\n\nExisting spike issue...",
            }
            mock_add_comment.return_value = True

            # Check 1 at 22:05 -> should comment
            t1 = now + timedelta(minutes=5)
            run_outer_loop(state_path=state_file, inject_fixture=make_fixture(t1), now_utc=t1)
            mock_add_comment.assert_called_once()

            # Reset mock
            mock_add_comment.reset_mock()

            # Check 2 at 22:20 (15 min later, same hour) -> should SKIP comment
            t2 = now + timedelta(minutes=20)
            run_outer_loop(state_path=state_file, inject_fixture=make_fixture(t2), now_utc=t2)
            mock_add_comment.assert_not_called()

            # Check 3: simulate state having last comment 65 minutes ago for this key -> should comment again
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            state_data["commented_keys"]["spike:backend:2026-09-26T22"] = (t2 - timedelta(minutes=65)).isoformat()
            state_file.write_text(json.dumps(state_data), encoding="utf-8")

            t3 = now + timedelta(minutes=30)
            run_outer_loop(state_path=state_file, inject_fixture=make_fixture(t3), now_utc=t3)
            mock_add_comment.assert_called_once()

class TestStagingOuterLoopErrorLineClassification(unittest.TestCase):
    """Case-sensitivity and structured level checks in is_error_log_line."""

    def test_is_error_log_line_ignores_lowercase_and_stats(self):
        from staging_outer_loop import is_error_log_line
        # Lowercase error in info or stats should NOT trigger
        self.assertFalse(is_error_log_line('{"timestamp": "2026-09-26T22:00:00Z", "level": "INFO", "message": "error_rate: 0.0"}'))
        self.assertFalse(is_error_log_line("2026-09-26 INFO app: handling error callback with 0 failures"))
        self.assertFalse(is_error_log_line('{"logger": "api.stats", "request_count": 100, "error_count": 0}'))
        # Uppercase ERROR should trigger
        self.assertTrue(is_error_log_line("2026-09-26 ERROR [app.database] connection refused"))
        self.assertTrue(is_error_log_line('{"level": "ERROR", "message": "Failed to connect to database"}'))
        self.assertTrue(is_error_log_line('{"logger": "app", "level": "error", "message": "Failed"}'))


if __name__ == "__main__":
    unittest.main()
