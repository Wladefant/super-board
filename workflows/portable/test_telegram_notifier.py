#!/usr/bin/env python3
"""
workflows/test_telegram_notifier.py — Unit Tests for Portable Telegram Notification Adapter

Covers:
1. Event schema validation and event filtering.
2. Secret and path sanitization.
3. Single-sentence message formatting and canonical link attachment.
4. CoordinatorPacket ingestion and translation.
5. ProjectSlotResolver multi-repo resolution, strict project affinity, and credential retrieval.
6. DeduplicationLedger hashing, deduplication windows, per-request cooldowns, and rate limits.
7. Mocked Telegram API transport (success, HTTP errors, connection failures, invalid chat).
8. Zero credential leaks in outputs or traces.
9. Outbound reply correlation: session binding, lease-derived binding, stale-lease refusal,
   and cross-language resolution through the installed TypeScript session bridge.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from telegram_notifier import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_DEDUP_WINDOW_SECONDS,
    DEFAULT_GLOBAL_MIN_INTERVAL,
    MESSAGE_CORRELATIONS_DDL,
    VALID_EVENT_TYPES,
    DeduplicationLedger,
    DeliveryReceipt,
    NotificationEvent,
    OutboundCorrelationStore,
    ProjectSlotResolver,
    SecretSanitizer,
    TelegramNotificationAdapter,
    resolve_pool_db_path,
)


class TestSecretSanitizer(unittest.TestCase):
    def test_token_redaction(self):
        text = "Failed with token bot123456789:AAH6f2isp_ai6PgU9OCO-_3f3aRrUNQhSFI during run"
        cleaned = SecretSanitizer.sanitize(text)
        self.assertNotIn("AAH6f2isp", cleaned)
        self.assertIn("[REDACTED_BOT_TOKEN]", cleaned)

    def test_bearer_token_redaction(self):
        text = "Auth header was Bearer secret_jwt_token_payload_xyz123456"
        cleaned = SecretSanitizer.sanitize(text)
        self.assertNotIn("secret_jwt_token", cleaned)
        self.assertIn("Bearer [REDACTED]", cleaned)

    def test_supabase_token_redaction(self):
        text = "Token was sbp_oauth_secret_access_token_123456"
        cleaned = SecretSanitizer.sanitize(text)
        self.assertNotIn("secret_access_token", cleaned)
        self.assertIn("[REDACTED_SUPABASE_TOKEN]", cleaned)
    def test_windows_user_path_redaction(self):
        text = r"File saved at C:\Users\wkiri\development\polysimulator\test.py"
        cleaned = SecretSanitizer.sanitize(text)
        self.assertNotIn(r"Users\wkiri", cleaned)
        self.assertIn(r"C:\Users\<user>\development", cleaned)


class TestNotificationEvent(unittest.TestCase):
    def test_valid_event_types(self):
        for et in VALID_EVENT_TYPES:
            ev = NotificationEvent(
                event_type=et,
                project="polysimulator",
                request_id="req-123",
                summary="A concise summary statement.",
                canonical_link="https://github.com/Bavariance/polysimulator/issues/123",
            )
            ev.validate()

    def test_invalid_event_type_raises(self):
        ev = NotificationEvent(
            event_type="routine_tool_call",
            project="polysimulator",
            request_id="req-123",
            summary="Tool execution trace",
            canonical_link="https://github.com/Bavariance/polysimulator/issues/123",
        )
        with self.assertRaises(ValueError):
            ev.validate()

    def test_missing_fields_raise(self):
        ev = NotificationEvent(
            event_type="milestone",
            project="",
            request_id="req-1",
            summary="Summary",
            canonical_link="http://link",
        )
        with self.assertRaises(ValueError):
            ev.validate()


class TestMessageFormatting(unittest.TestCase):
    def test_format_message_structure(self):
        ev = NotificationEvent(
            event_type="milestone",
            project="Bavariance/polysimulator",
            request_id="req-4545",
            summary="Implementation complete, advancing to review",
            canonical_link="https://github.com/Bavariance/polysimulator/issues/4545",
        )
        msg = TelegramNotificationAdapter.format_message(ev)
        self.assertTrue(msg.startswith("[Milestone] Bavariance/polysimulator req-4545:"))
        self.assertTrue(msg.endswith("https://github.com/Bavariance/polysimulator/issues/4545"))
        # Verify single sentence (no newlines)
        self.assertNotIn("\n", msg)

    def test_format_decision_label(self):
        ev = NotificationEvent(
            event_type="decision",
            project="Bavariance/polysimulator",
            request_id="req-4543",
            summary="Paused awaiting human decision on DEC-4543-01",
            canonical_link="https://github.com/Bavariance/polysimulator/issues/4543#issuecomment-5550731410",
        )
        msg = TelegramNotificationAdapter.format_message(ev)
        self.assertTrue(msg.startswith("[Decision Needed]"))

    def test_format_status_and_question_events(self):
        status_ev = NotificationEvent(
            event_type="status",
            project="Bavariance/polysimulator",
            request_id="req-harness-continuous-orchestration",
            summary="Workflow in progress at 1a28d9d8ad1976160db7223a0d5df57df421f862, adapter unfinished",
            canonical_link="https://github.com/Wladefant/super-board/pull/74",
        )
        msg = TelegramNotificationAdapter.format_message(status_ev)
        self.assertTrue(msg.startswith("[Status Update] Bavariance/polysimulator req-harness-continuous-orchestration:"))
        self.assertIn("1a28d9d8ad1976160db7223a0d5df57df421f862", msg)
        self.assertTrue(msg.endswith("https://github.com/Wladefant/super-board/pull/74"))

        question_ev = NotificationEvent(
            event_type="question",
            project="Bavariance/polysimulator",
            request_id="DEC-4543-01",
            summary="How should background execution proceed? Options: A (Park), B (Speculate)",
            canonical_link="https://github.com/Bavariance/polysimulator/issues/4543",
        )
        q_msg = TelegramNotificationAdapter.format_message(question_ev)
        self.assertTrue(q_msg.startswith("[Question] Bavariance/polysimulator DEC-4543-01:"))

    def test_format_multiple_links_and_deduplication(self):
        ev = NotificationEvent(
            event_type="status",
            project="Bavariance/polysimulator",
            request_id="req-multi-link",
            summary="Workflow active across multiple tracking endpoints",
            canonical_link="https://github.com/Wladefant/super-board/pull/74",
            metadata={
                "links": [
                    "https://github.com/Bavariance/polysimulator/pull/4545",
                    "https://github.com/Bavariance/polysimulator/issues/4543",
                    "https://github.com/Wladefant/super-board/pull/74",  # Duplicate link
                ]
            },
        )
        msg = TelegramNotificationAdapter.format_message(ev)
        self.assertIn("https://github.com/Wladefant/super-board/pull/74", msg)
        self.assertIn("https://github.com/Bavariance/polysimulator/pull/4545", msg)
        self.assertIn("https://github.com/Bavariance/polysimulator/issues/4543", msg)
        # Verify deduplication: PR74 should appear exactly once
        self.assertEqual(msg.count("https://github.com/Wladefant/super-board/pull/74"), 1)


class TestCoordinatorPacketIngestion(unittest.TestCase):
    def test_decision_packet_translation(self):
        packet = {
            "status": "pending_decision",
            "status_reason": "Decision required",
            "request": {
                "id": "req-4543",
                "state": "implementation",
                "issue_url": "https://github.com/Bavariance/polysimulator/issues/4543",
            },
            "decision_status": {
                "blocking_this_request": True,
                "blocking_decision_ids": ["DEC-4543-01"],
            },
        }
        ev = TelegramNotificationAdapter.from_coordinator_packet(packet)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "decision")
        self.assertIn("DEC-4543-01", ev.summary)
        self.assertEqual(ev.canonical_link, "https://github.com/Bavariance/polysimulator/issues/4543")

    def test_blocker_packet_translation(self):
        packet = {
            "status": "blocked",
            "status_reason": "Preflight check failed",
            "request": {
                "id": "req-4550",
                "state": "implementation",
                "issue_url": "https://github.com/Bavariance/polysimulator/issues/4550",
            },
            "preflight": {
                "status": "blocked",
                "blockers": ["dokploy_staging compose unhealthy"],
            },
        }
        ev = TelegramNotificationAdapter.from_coordinator_packet(packet)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "blocker")
        self.assertIn("dokploy_staging compose unhealthy", ev.summary)

    def test_completion_packet_translation(self):
        packet = {
            "status": "completed",
            "status_reason": "All tasks done",
            "request": {
                "id": "req-4540",
                "state": "done",
                "issue_url": "https://github.com/Bavariance/polysimulator/pull/4541",
            },
        }
        ev = TelegramNotificationAdapter.from_coordinator_packet(packet)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "completion")

    def test_routine_chatter_filtered(self):
        # Implementation ongoing without decision or blocker should NOT notify
        packet = {
            "status": "ready",
            "status_reason": "Evaluating next step",
            "request": {
                "id": "req-4545",
                "state": "implementation",
                "issue_url": "https://github.com/Bavariance/polysimulator/issues/4545",
            },
            "decision_status": {"blocking_this_request": False},
            "preflight": {"status": "passed"},
        }
        ev = TelegramNotificationAdapter.from_coordinator_packet(packet)
        self.assertIsNone(ev)


class TestDeduplicationAndCooldown(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "notify_state.json"
        self.ledger = DeduplicationLedger(
            state_file=self.state_file,
            dedup_window=3600.0,
            cooldown_window=60.0,
            min_global_interval=10.0,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_dedup_drops_identical_event(self):
        ev = NotificationEvent(
            event_type="milestone",
            project="polysimulator",
            request_id="req-1",
            summary="Phase 1 finished.",
            canonical_link="http://link",
        )
        now = 1000.0
        eligible, status, reason = self.ledger.check_eligible(ev, now=now)
        self.assertTrue(eligible)

        self.ledger.record_dispatch(ev, now=now)

        # Immediate second attempt should be dropped as deduped
        eligible2, status2, reason2 = self.ledger.check_eligible(ev, now=now + 5.0)
        self.assertFalse(eligible2)
        self.assertEqual(status2, "deduped")

    def test_cooldown_delays_different_event_same_request(self):
        ev1 = NotificationEvent(
            event_type="milestone",
            project="polysimulator",
            request_id="req-1",
            summary="Phase 1 finished.",
            canonical_link="http://link/1",
        )
        ev2 = NotificationEvent(
            event_type="milestone",
            project="polysimulator",
            request_id="req-1",
            summary="Phase 2 started.",
            canonical_link="http://link/2",
        )
        now = 1000.0
        self.ledger.record_dispatch(ev1, now=now)

        # Within cooldown window (e.g. 20s later, cooldown is 60s)
        eligible, status, reason = self.ledger.check_eligible(ev2, now=now + 20.0)
        self.assertFalse(eligible)
        self.assertEqual(status, "cooldown")

        # After cooldown window (e.g. 70s later)
        eligible3, status3, reason3 = self.ledger.check_eligible(ev2, now=now + 70.0)
        self.assertTrue(eligible3)

    def test_blocker_bypasses_per_request_cooldown(self):
        ev1 = NotificationEvent(
            event_type="milestone",
            project="polysimulator",
            request_id="req-1",
            summary="Phase 1 finished.",
            canonical_link="http://link/1",
        )
        blocker = NotificationEvent(
            event_type="blocker",
            project="polysimulator",
            request_id="req-1",
            summary="Critical deadlock detected.",
            canonical_link="http://link/block",
        )
        now = 1000.0
        self.ledger.record_dispatch(ev1, now=now)

        # Blocker should bypass per-request cooldown (after global min interval)
        eligible, status, reason = self.ledger.check_eligible(blocker, now=now + 15.0)
        self.assertTrue(eligible)

    def test_decision_bypasses_per_request_cooldown(self):
        ev1 = NotificationEvent(
            event_type="milestone",
            project="polysimulator",
            request_id="req-4543",
            summary="Milestone passed.",
            canonical_link="http://link/1",
        )
        decision_ev = NotificationEvent(
            event_type="decision",
            project="polysimulator",
            request_id="req-4543",
            summary="Operator architectural choice required.",
            canonical_link="http://link/dec",
        )
        now = 1000.0
        self.ledger.record_dispatch(ev1, now=now)

        # Decision should bypass per-request cooldown (after global min interval)
        eligible, status, reason = self.ledger.check_eligible(decision_ev, now=now + 15.0)
        self.assertTrue(eligible)


class TestProjectSlotResolver(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manifest_file = Path(self.temp_dir.name) / "manifest.json"
        self.channels_dir = Path(self.temp_dir.name) / "channels"

        # Create mock manifest
        manifest_data = {
            "version": 1,
            "slots": [
                {
                    "slotId": "telegram-polysim",
                    "stateDir": str(self.channels_dir / "telegram-polysim"),
                    "preferredProjects": ["polysimulator", "polysim"],
                    "enabled": True,
                },
                {
                    "slotId": "telegram-soundcore",
                    "stateDir": str(self.channels_dir / "telegram-soundcore"),
                    "preferredProjects": ["soundcore"],
                    "enabled": True,
                },
                {
                    "slotId": "telegram-superboard",
                    "stateDir": str(self.channels_dir / "telegram-superboard"),
                    "preferredProjects": ["superboard", "super-board"],
                    "enabled": True,
                },
            ],
        }
        self.manifest_file.write_text(json.dumps(manifest_data), encoding="utf-8")

        # Create channel state dirs
        for slot in manifest_data["slots"]:
            sdir = Path(slot["stateDir"])
            sdir.mkdir(parents=True, exist_ok=True)
            (sdir / ".env").write_text("TELEGRAM_BOT_TOKEN=fake_token_12345\n", encoding="utf-8")
            (sdir / "access.json").write_text(json.dumps({"dmPolicy": "pairing", "allowFrom": ["1247617658"]}), encoding="utf-8")

        self.resolver = ProjectSlotResolver(
            manifest_path=self.manifest_file,
            channels_dir=self.channels_dir,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_resolve_exact_affinity(self):
        res = self.resolver.resolve_slot("polysimulator")
        self.assertEqual(res["slotId"], "telegram-polysim")

        res_sc = self.resolver.resolve_slot("soundcore")
        self.assertEqual(res_sc["slotId"], "telegram-soundcore")

        res_sb = self.resolver.resolve_slot("Wladefant/super-board")
        self.assertEqual(res_sb["slotId"], "telegram-superboard")

    def test_token_and_destination_loading(self):
        state_dir = self.channels_dir / "telegram-polysim"
        token = self.resolver.load_token(state_dir)
        self.assertEqual(token, "fake_token_12345")

        destinations = self.resolver.load_allowed_destinations(state_dir)
        self.assertEqual(destinations, ["1247617658"])


class TestTelegramNotificationAdapter(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "state.json"
        self.ledger = DeduplicationLedger(self.state_file)

        self.channels_dir = Path(self.temp_dir.name) / "channels"
        self.polysim_dir = self.channels_dir / "telegram-polysim"
        self.polysim_dir.mkdir(parents=True, exist_ok=True)
        (self.polysim_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=dummy_token_999\n", encoding="utf-8")
        (self.polysim_dir / "access.json").write_text(json.dumps({"allowFrom": ["1247617658"]}), encoding="utf-8")

        manifest_file = Path(self.temp_dir.name) / "manifest.json"
        manifest_file.write_text(
            json.dumps({
                "version": 1,
                "slots": [{
                    "slotId": "telegram-polysim",
                    "stateDir": str(self.polysim_dir),
                    "preferredProjects": ["polysimulator"],
                    "enabled": True,
                }],
            }),
            encoding="utf-8",
        )
        self.resolver = ProjectSlotResolver(manifest_path=manifest_file, channels_dir=self.channels_dir)
        # The pool database is named explicitly so this fixture can never resolve to the
        # installed pool and write correlations into a real session bridge index.
        self.pool_db = Path(self.temp_dir.name) / "bot_pool.db"
        self.adapter = TelegramNotificationAdapter(
            resolver=self.resolver,
            ledger=self.ledger,
            correlation_store=OutboundCorrelationStore(self.pool_db),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_dry_run_does_not_call_network(self):
        ev = NotificationEvent(
            event_type="milestone",
            project="polysimulator",
            request_id="req-100",
            summary="Milestone passed.",
            canonical_link="https://github.com/Bavariance/polysimulator/issues/100",
        )
        receipt = self.adapter.notify(ev, dry_run=True)
        self.assertTrue(receipt.delivered)
        self.assertEqual(receipt.status, "dry_run")
        self.assertIn("[DRY-RUN]", receipt.reason)
        self.assertEqual(receipt.chat_id, "[REDACTED_DESTINATION]")
        self.assertNotIn("1247617658", receipt.reason)

    @patch("urllib.request.urlopen")
    def test_live_send_mocked_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "result": {
                "message_id": 999123,
                "from": {"id": 8566730274, "is_bot": True},
            },
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        ev = NotificationEvent(
            event_type="decision",
            project="polysimulator",
            request_id="req-200",
            summary="Decision needed on DEC-200.",
            canonical_link="https://github.com/Bavariance/polysimulator/issues/200",
        )
        receipt = self.adapter.notify(ev, dry_run=False, force=True)
        self.assertTrue(receipt.delivered)
        self.assertEqual(receipt.status, "sent")
        self.assertEqual(receipt.message_id, 999123)
        self.assertEqual(receipt.chat_id, "[REDACTED_DESTINATION]")
        self.assertNotIn("1247617658", receipt.reason)
        self.assertEqual(receipt.bot_id, "8566730274")

    def test_unallowlisted_chat_blocked(self):
        ev = NotificationEvent(
            event_type="milestone",
            project="polysimulator",
            request_id="req-300",
            summary="Milestone summary.",
            canonical_link="https://github.com/Bavariance/polysimulator/issues/300",
        )
        # Attempt to send to an unauthorized chat ID
        receipt = self.adapter.notify(ev, explicit_chat_id="9999999999", dry_run=False)
        self.assertFalse(receipt.delivered)
        self.assertEqual(receipt.status, "blocked")
        self.assertIn("not in allowlist", receipt.reason)

    def test_from_decision_contract_translation(self):
        decision_data = {
            "decision_id": "DEC-ARCH-01",
            "request_id": "req-arch-01",
            "question": "When the agent swarm encounters a blocking human decision, how should background execution proceed?",
            "options": [
                {"id": "A", "label": "Park and Idle Wait"},
                {"id": "B", "label": "Speculative Feature Branching"},
            ],
            "recommendation": "Option A: Park and Idle Wait ensures strict invariant compliance",
            "issue_url": "https://github.com/Bavariance/polysimulator/issues/4543",
            "status": "pending",
        }
        ev = TelegramNotificationAdapter.from_decision(decision_data, project_override="Bavariance/polysimulator")
        self.assertIsNotNone(ev)
        self.assertEqual(ev.event_type, "decision")
        self.assertEqual(ev.request_id, "req-arch-01 (DEC-ARCH-01)")
        self.assertEqual(ev.canonical_link, "https://github.com/Bavariance/polysimulator/issues/4543")
        self.assertIn("Park and Idle Wait", ev.summary)
        self.assertIn("A: Park and Idle Wait", ev.summary)
        self.assertIn("B: Speculative Feature Branching", ev.summary)
        self.assertIn("Option A: Park and Idle Wait", ev.summary)

        formatted = TelegramNotificationAdapter.format_message(ev)
        self.assertTrue(formatted.startswith("[Decision Needed] Bavariance/polysimulator req-arch-01 (DEC-ARCH-01):"))
        self.assertIn("Options: A: Park and Idle Wait; B: Speculative Feature Branching", formatted)
        self.assertIn("Recommended: Option A", formatted)
        self.assertTrue(formatted.endswith("https://github.com/Bavariance/polysimulator/issues/4543"))

    def test_load_decision_from_file(self):
        temp_dec_file = Path(self.temp_dir.name) / "test_decisions.json"
        test_data = {
            "version": 2,
            "decisions": {
                "DEC-TEST-01": {
                    "decision_id": "DEC-TEST-01",
                    "request_id": "req-test-01",
                    "question": "Test question?",
                    "options": [{"id": "A", "label": "Option A"}],
                    "recommendation": "Option A",
                    "issue_url": "https://github.com/Bavariance/polysimulator/issues/999",
                }
            },
        }
        temp_dec_file.write_text(json.dumps(test_data), encoding="utf-8")
        dec = TelegramNotificationAdapter.load_decision_from_file("DEC-TEST-01", decisions_file=temp_dec_file)
        self.assertIsNotNone(dec)
        self.assertEqual(dec["decision_id"], "DEC-TEST-01")
        self.assertEqual(dec["question"], "Test question?")

    def test_retired_typed_status_refusal_with_neutral_id(self):
        """Verify decisions with neutral IDs are refused based strictly on authoritative typed status."""
        for bad_status in ("rejected", "resolved", "answered", "closed", "retired", "done"):
            dec = {
                "decision_id": "DEC-NEUTRAL-42",
                "request_id": "req-neutral-42",
                "status": bad_status,
                "question": "Neutral architectural choice?",
            }
            is_valid, reason = TelegramNotificationAdapter.is_decision_notifiable(dec)
            self.assertFalse(is_valid, f"Neutral ID with typed status '{bad_status}' should be refused")
            self.assertIn("non-actionable", reason)
            self.assertIsNone(TelegramNotificationAdapter.from_decision(dec))

    def test_decision_refusal_synthetic_and_demo(self):
        """Verify explicit synthetic test probes and synthetic provenance are strictly refused."""
        synthetic_decisions = [
            {"decision_id": "DEC-TEST-01", "request_id": "req-test-01", "status": "pending", "is_synthetic": True, "question": "Q?"},
            {"decision_id": "DEC-TEST-02", "request_id": "req-test-02", "status": "pending", "is_test": True, "question": "Q?"},
            {"decision_id": "DEC-TEST-03", "request_id": "req-test-03", "status": "pending", "provenance": "synthetic_test", "question": "Q?"},
            {
                "decision_id": "DEC-TEST-04",
                "request_id": "req-test-04",
                "status": "pending",
                "question": "Q?",
                "audit_trail": [{"provenance": "synthetic_test", "status": "rejected", "comment_id": "999"}],
            },
        ]
        for dec in synthetic_decisions:
            is_valid, reason = TelegramNotificationAdapter.is_decision_notifiable(dec)
            self.assertFalse(is_valid, f"Synthetic decision '{dec['decision_id']}' should be refused: got valid")
            self.assertIsNone(TelegramNotificationAdapter.from_decision(dec))

    def test_valid_demo_feature_and_retiring_api_decisions_accepted(self):
        """Regression: Legitimate questions discussing demo features or retiring APIs must be accepted."""
        demo_feature_dec = {
            "decision_id": "DEC-DEMO-FEATURE-01",
            "request_id": "req-feature-demo-mode",
            "prompt": "Implement interactive demo feature for product showcase",
            "question": "Should the interactive demo feature be embedded in the hero or linked to modal?",
            "options": [{"id": "A", "label": "Hero embed"}, {"id": "B", "label": "Modal link"}],
            "recommendation": "Option A",
            "status": "pending",
            "provenance": "agent_authored",  # Agent-authored questions are normal and valid
            "issue_url": "https://github.com/Bavariance/polysimulator/issues/4543",
        }
        is_valid, reason = TelegramNotificationAdapter.is_decision_notifiable(demo_feature_dec)
        self.assertTrue(is_valid, f"Legitimate demo feature question was incorrectly refused: {reason}")
        ev = TelegramNotificationAdapter.from_decision(demo_feature_dec)
        self.assertIsNotNone(ev)
        formatted = TelegramNotificationAdapter.format_message(ev)
        self.assertIn("interactive demo feature", formatted)

        retire_api_dec = {
            "decision_id": "DEC-RETIRE-API-02",
            "request_id": "req-retire-v1-endpoints",
            "prompt": "Retire legacy v1 endpoints in favor of unified v2 schema",
            "question": "Should we retire legacy v1 endpoints immediately or retain 30-day deprecation shim?",
            "options": [{"id": "A", "label": "Immediate retirement"}, {"id": "B", "label": "30-day shim"}],
            "recommendation": "Option B",
            "status": "pending",
            "provenance": "human_operator",
            "issue_url": "https://github.com/Bavariance/polysimulator/issues/4543",
        }
        is_valid2, reason2 = TelegramNotificationAdapter.is_decision_notifiable(retire_api_dec)
        self.assertTrue(is_valid2, f"Legitimate API retirement question was incorrectly refused: {reason2}")
        ev2 = TelegramNotificationAdapter.from_decision(retire_api_dec)
        self.assertIsNotNone(ev2)
        formatted2 = TelegramNotificationAdapter.format_message(ev2)
        self.assertIn("retire legacy v1 endpoints", formatted2)
    def test_decision_refusal_completed_ledger_request(self):
        """Verify decisions on completed/done requests are refused."""
        dec = {
            "decision_id": "DEC-GENUINE-01",
            "request_id": "req-completed-01",
            "status": "pending",
            "question": "Genuine question?",
        }
        completed_ledger_req = {"id": "req-completed-01", "state": "done"}
        is_valid, reason = TelegramNotificationAdapter.is_decision_notifiable(dec, ledger_request=completed_ledger_req)
        self.assertFalse(is_valid)
        self.assertIn("terminal state", reason)
        self.assertIsNone(TelegramNotificationAdapter.from_decision(dec, ledger_request=completed_ledger_req))

    @patch("urllib.request.urlopen")
    def test_destination_identifiers_strictly_redacted(self, mock_urlopen):
        """Verify destination chat IDs are completely redacted across receipts, sanitizers, and connection tests."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "result": {"id": 8566730274, "username": "testbot", "first_name": "TestBot"},
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        ev = NotificationEvent(
            event_type="milestone",
            project="polysimulator",
            request_id="req-clean",
            summary="Summary text.",
            canonical_link="http://link",
        )
        receipt = self.adapter.notify(ev, dry_run=True)
        self.assertEqual(receipt.chat_id, "[REDACTED_DESTINATION]")
        self.assertNotIn("1247617658", receipt.reason)

        # Test sanitizer
        leak_text = "Sending alert to chat_id: 1247617658 for user"
        sanitized = SecretSanitizer.sanitize(leak_text)
        self.assertNotIn("1247617658", sanitized)
        self.assertIn("[REDACTED_DESTINATION]", sanitized)

        # Test connection result masking
        conn = self.adapter.test_connection()
        for dest in conn.get("configured_destinations", []):
            self.assertNotIn("1247617658", dest)
            self.assertEqual(dest, "[CONFIGURED_DESTINATION]")


class TestStrictProjectAffinity(unittest.TestCase):
    """A configured slot pool must never deliver one project's notification on another's bot."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.channels_dir = root / "channels"
        self.manifest_file = root / "manifest.json"

        slots = []
        for slot_id, preferred in (
            ("telegram-polysim", ["polysimulator", "polysim"]),
            ("telegram-soundcore", ["soundcore"]),
        ):
            state_dir = self.channels_dir / slot_id
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=fake_token_12345\n", encoding="utf-8")
            (state_dir / "access.json").write_text(
                json.dumps({"dmPolicy": "allowlist", "allowFrom": ["1247617658"]}), encoding="utf-8"
            )
            slots.append(
                {
                    "slotId": slot_id,
                    "stateDir": str(state_dir),
                    "preferredProjects": preferred,
                    "enabled": True,
                }
            )
        self.manifest_file.write_text(json.dumps({"version": 1, "slots": slots}), encoding="utf-8")

        self.resolver = ProjectSlotResolver(manifest_path=self.manifest_file, channels_dir=self.channels_dir)
        self.adapter = TelegramNotificationAdapter(
            resolver=self.resolver,
            ledger=DeduplicationLedger(root / "state.json"),
            correlation_store=OutboundCorrelationStore(root / "unused_pool.db"),
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_unmatched_project_is_unresolved_not_defaulted(self):
        self.assertEqual(self.resolver.resolve_slot("polysimulator")["slotId"], "telegram-polysim")
        unmatched = self.resolver.resolve_slot("Bavariance/unrelated-service")
        self.assertEqual(unmatched["source"], "unresolved")
        self.assertIsNone(unmatched["slotId"])

    @patch("urllib.request.urlopen")
    def test_unmatched_project_is_refused_without_network_call(self, mock_urlopen):
        ev = NotificationEvent(
            event_type="blocker",
            project="Bavariance/unrelated-service",
            request_id="req-foreign",
            summary="Unrelated project blocked.",
            canonical_link="https://github.com/Bavariance/unrelated-service/issues/1",
        )
        receipt = self.adapter.notify(ev, dry_run=False, force=True)
        self.assertFalse(receipt.delivered)
        self.assertEqual(receipt.status, "blocked")
        self.assertIn("No Telegram bot slot declares affinity", receipt.reason)
        self.assertEqual(mock_urlopen.call_count, 0)

    @patch("urllib.request.urlopen")
    def test_connection_refuses_unmatched_project(self, mock_urlopen):
        result = self.adapter.test_connection(project="Bavariance/unrelated-service")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(mock_urlopen.call_count, 0)


class TestOutboundCorrelationStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.pool_db = self.root / "bot_pool.db"
        self.store = OutboundCorrelationStore(self.pool_db)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _seed_lease(self, slot_id: str, session_id: str, heartbeat_age: float) -> None:
        conn = sqlite3.connect(str(self.pool_db))
        try:
            with conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS bot_leases ("
                    "slot_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, project_path TEXT NOT NULL, "
                    "owner_pid INTEGER NOT NULL, owner_proc_start TEXT NOT NULL, acquired_at REAL NOT NULL, "
                    "heartbeat_at REAL NOT NULL, ttl_seconds REAL NOT NULL DEFAULT 20.0, lease_status TEXT NOT NULL)"
                )
                now = time.time()
                conn.execute(
                    "INSERT OR REPLACE INTO bot_leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')",
                    (slot_id, session_id, "C:/dev/polysimulator", 4242, "0", now, now - heartbeat_age, 20.0),
                )
        finally:
            conn.close()

    def test_pool_database_resolution_order(self):
        installed = self.root / "installed_pool.db"
        explicit = self.root / "explicit_pool.db"
        env_named = self.root / "env_pool.db"

        # An explicit path wins outright, and is honoured before the file exists: naming
        # it is the caller's statement that this is the pool.
        self.assertEqual(
            resolve_pool_db_path(explicit, default_path=installed, env={"VEYYON_POOL_DB": str(env_named)}),
            (explicit, "explicit"),
        )
        # Then the environment.
        self.assertEqual(
            resolve_pool_db_path(None, default_path=installed, env={"VEYYON_POOL_DB": str(env_named)}),
            (env_named, "env"),
        )
        # An off value is a deliberate opt-out even inside an installed pool.
        installed.write_bytes(b"")
        for off in ("", "off", "0", "none", "disabled", "false", " OFF "):
            self.assertEqual(
                resolve_pool_db_path(None, default_path=installed, env={"VEYYON_POOL_DB": off}),
                (None, "env_disabled"),
                msg=f"VEYYON_POOL_DB={off!r} must disable correlation",
            )
        # With no explicit path and no environment, an existing installed pool is used,
        # so a real caller correlates by default instead of needing an opt-in.
        self.assertEqual(resolve_pool_db_path(None, default_path=installed, env={}), (installed, "installed"))
        # But a default that does not exist is never created: a pool no bridge reads
        # would swallow correlations and make replies look routable when they are not.
        missing = self.root / "no_such_pool.db"
        self.assertEqual(resolve_pool_db_path(None, default_path=missing, env={}), (None, "absent"))

    def test_store_reports_the_rule_that_decided_its_path(self):
        installed = self.root / "installed_pool.db"
        installed.write_bytes(b"")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VEYYON_POOL_DB", None)
            enabled_by_default = OutboundCorrelationStore(default_path=installed)
            self.assertTrue(enabled_by_default.enabled)
            self.assertEqual(enabled_by_default.source, "installed")

            off = OutboundCorrelationStore(default_path=self.root / "absent_pool.db")
            self.assertFalse(off.enabled)
            self.assertEqual(off.source, "absent")

        self.assertTrue(self.store.enabled)
        self.assertEqual(self.store.source, "explicit")

    def test_record_and_lookup_round_trip(self):
        self.assertTrue(
            self.store.record(
                bot_id="8566730274",
                chat_id="1247617658",
                message_id=999123,
                slot_id="telegram-polysim",
                session_id="sess-py-1",
                request_id="req-4582-telegram-input",
                decision_id="DEC-1",
                project_path="Bavariance/polysimulator",
            )
        )
        found = self.store.lookup("8566730274", "1247617658", 999123)
        self.assertEqual(found["session_id"], "sess-py-1")
        self.assertEqual(found["request_id"], "req-4582-telegram-input")
        self.assertEqual(found["slot_id"], "telegram-polysim")
        self.assertIsNone(self.store.lookup("8566730274", "1247617658", 111))
        self.assertIsNone(self.store.lookup("9999999999", "1247617658", 999123))

    def test_record_refuses_unbound_session(self):
        self.assertFalse(
            self.store.record(
                bot_id="8566730274",
                chat_id="1247617658",
                message_id=1,
                slot_id="telegram-polysim",
                session_id="",
            )
        )

    def test_lease_holder_is_never_used_as_a_session_binding(self):
        """A live lease on the destination slot must not become a message's binding."""
        self._seed_lease("telegram-polysim", "sess-leaseholder", heartbeat_age=2.0)
        self.assertFalse(hasattr(self.store, "resolve_active_session"))

    def test_schema_matches_shared_definition(self):
        self.store.record(
            bot_id="1",
            chat_id="2",
            message_id=3,
            slot_id="telegram-polysim",
            session_id="s",
        )
        conn = sqlite3.connect(str(self.pool_db))
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(message_correlations)")]
        finally:
            conn.close()
        self.assertEqual(
            columns,
            [
                "bot_id",
                "chat_id",
                "message_id",
                "slot_id",
                "session_id",
                "request_id",
                "decision_id",
                "project_path",
                "created_at",
            ],
        )
        self.assertIn("PRIMARY KEY (bot_id, chat_id, message_id)", MESSAGE_CORRELATIONS_DDL)


class TestNotifyCorrelationBinding(unittest.TestCase):
    """A delivered notification must be bound to the session that owns the request."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.pool_db = root / "bot_pool.db"
        self.state_dir = root / "channels" / "telegram-polysim"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=dummy_token_999\n", encoding="utf-8")
        (self.state_dir / "access.json").write_text(
            json.dumps({"allowFrom": ["1247617658"]}), encoding="utf-8"
        )

        manifest_file = root / "manifest.json"
        manifest_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "slots": [
                        {
                            "slotId": "telegram-polysim",
                            "stateDir": str(self.state_dir),
                            "preferredProjects": ["polysimulator"],
                            "enabled": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.store = OutboundCorrelationStore(self.pool_db)
        self.adapter = TelegramNotificationAdapter(
            resolver=ProjectSlotResolver(manifest_path=manifest_file, channels_dir=root / "channels"),
            ledger=DeduplicationLedger(root / "state.json"),
            correlation_store=self.store,
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _mock_send(self, mock_urlopen, message_id: int = 999123) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"ok": True, "result": {"message_id": message_id, "from": {"id": 8566730274, "is_bot": True}}}
        ).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

    def _event(self, request_id: str = "req-4582-telegram-input", session_id=None) -> NotificationEvent:
        return NotificationEvent(
            event_type="blocker",
            project="polysimulator",
            request_id=request_id,
            summary="Worker blocked on missing prerequisite.",
            canonical_link="https://github.com/Bavariance/polysimulator/issues/4543",
            session_id=session_id,
        )

    @patch("urllib.request.urlopen")
    def test_explicit_session_is_recorded(self, mock_urlopen):
        self._mock_send(mock_urlopen)
        receipt = self.adapter.notify(self._event(session_id="sess-owner"), force=True)
        self.assertTrue(receipt.delivered)
        self.assertEqual(receipt.session_id, "sess-owner")
        self.assertEqual(receipt.slot_id, "telegram-polysim")
        self.assertTrue(receipt.correlation_recorded)
        self.assertEqual(receipt.correlation_status, "recorded")

        row = self.store.lookup("8566730274", "1247617658", 999123)
        self.assertEqual(row["session_id"], "sess-owner")
        self.assertEqual(row["request_id"], "req-4582-telegram-input")

    @patch("urllib.request.urlopen")
    def test_without_originating_session_message_stays_uncorrelated(self, mock_urlopen):
        self._mock_send(mock_urlopen, message_id=999124)
        receipt = self.adapter.notify(self._event(request_id="req-nobind"), force=True)
        self.assertTrue(receipt.delivered)
        self.assertIsNone(receipt.session_id)
        self.assertFalse(receipt.correlation_recorded)
        self.assertEqual(receipt.correlation_status, "unbound")
        self.assertIsNone(self.store.lookup("8566730274", "1247617658", 999124))

    @patch("urllib.request.urlopen")
    def test_live_lease_on_the_slot_never_binds_the_message(self, mock_urlopen):
        """An unrelated session holding the lease must not inherit this request's reply."""
        conn = sqlite3.connect(str(self.pool_db))
        try:
            with conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS bot_leases ("
                    "slot_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, project_path TEXT NOT NULL, "
                    "owner_pid INTEGER NOT NULL, owner_proc_start TEXT NOT NULL, acquired_at REAL NOT NULL, "
                    "heartbeat_at REAL NOT NULL, ttl_seconds REAL NOT NULL DEFAULT 20.0, lease_status TEXT NOT NULL)"
                )
                now = time.time()
                conn.execute(
                    "INSERT OR REPLACE INTO bot_leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')",
                    ("telegram-polysim", "sess-leaseholder", "C:/dev/polysimulator", 99, "0", now, now, 20.0),
                )
        finally:
            conn.close()

        self._mock_send(mock_urlopen, message_id=999125)
        receipt = self.adapter.notify(self._event(request_id="req-no-identity"), force=True)
        self.assertTrue(receipt.delivered)
        self.assertIsNone(receipt.session_id)
        self.assertFalse(receipt.correlation_recorded)
        self.assertEqual(receipt.correlation_status, "unbound")
        self.assertIsNone(self.store.lookup("8566730274", "1247617658", 999125))

    @patch("urllib.request.urlopen")
    def test_originating_session_wins_over_a_mismatched_lease_holder(self, mock_urlopen):
        conn = sqlite3.connect(str(self.pool_db))
        try:
            with conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS bot_leases ("
                    "slot_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, project_path TEXT NOT NULL, "
                    "owner_pid INTEGER NOT NULL, owner_proc_start TEXT NOT NULL, acquired_at REAL NOT NULL, "
                    "heartbeat_at REAL NOT NULL, ttl_seconds REAL NOT NULL DEFAULT 20.0, lease_status TEXT NOT NULL)"
                )
                now = time.time()
                conn.execute(
                    "INSERT OR REPLACE INTO bot_leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')",
                    ("telegram-polysim", "sess-current-lease", "C:/dev/polysimulator", 99, "0", now, now, 20.0),
                )
        finally:
            conn.close()

        self._mock_send(mock_urlopen, message_id=999126)
        receipt = self.adapter.notify(self._event(request_id="req-owned", session_id="sess-request-owner"), force=True)
        self.assertEqual(receipt.session_id, "sess-request-owner")
        self.assertTrue(receipt.correlation_recorded)
        self.assertEqual(
            self.store.lookup("8566730274", "1247617658", 999126)["session_id"], "sess-request-owner"
        )


INSTALLED_BRIDGE_PROBE = Path.home() / ".veyyon" / "telegram" / "tests" / "reply_probe.ts"


@unittest.skipUnless(
    shutil.which("bun") is not None and INSTALLED_BRIDGE_PROBE.exists(),
    "requires bun and the installed Telegram session bridge",
)
class TestCrossLanguageReplyRouting(unittest.TestCase):
    """A correlation written by this sender must resolve identically in the TS bridge."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.pool_db = Path(self.temp_dir.name) / "bot_pool.db"
        self.store = OutboundCorrelationStore(self.pool_db)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _probe(self, message_id: int, session_id: str) -> dict:
        proc = subprocess.run(
            [
                shutil.which("bun"),
                str(INSTALLED_BRIDGE_PROBE),
                str(self.pool_db),
                "8566730274",
                "1247617658",
                str(message_id),
                session_id,
            ],
            cwd=str(INSTALLED_BRIDGE_PROBE.parent.parent),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, f"probe failed: {proc.stderr}")
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_recorded_reply_routes_only_to_the_owning_session(self):
        self.store.record(
            bot_id="8566730274",
            chat_id="1247617658",
            message_id=999321,
            slot_id="telegram-polysim",
            session_id="sess-owner",
            request_id="req-4582-telegram-input",
            project_path="Bavariance/polysimulator",
        )

        own = self._probe(999321, "sess-owner")
        self.assertEqual(own["decision"], "deliver")
        self.assertEqual(own["correlatedRequest"], "req-4582-telegram-input")

        foreign = self._probe(999321, "sess-other")
        self.assertEqual(foreign["decision"], "reject_foreign_session")

        unknown = self._probe(555000, "sess-owner")
        self.assertEqual(unknown["decision"], "reject_unknown")


class TestCoordinatorHookCorrelation(unittest.TestCase):
    """The coordinator's own notification hook must correlate what it delivers.

    It constructed a bare TelegramNotificationAdapter, so its notifications were the
    one delivery path that could never be correlated regardless of configuration, and
    the bridge refused every reply to one.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state_dir = self.root / "state"
        self.state_dir.mkdir()
        self.pool_db = self.root / "configured_pool.db"

        self.channels_dir = self.root / "channels"
        slot_dir = self.channels_dir / "telegram-polysim"
        slot_dir.mkdir(parents=True)
        (slot_dir / ".env").write_text("TELEGRAM_BOT_TOKEN=1000000000:AAsynthetic\n", encoding="utf-8")
        (slot_dir / "access.json").write_text(
            json.dumps({"dmPolicy": "allowlist", "allowFrom": ["1247617658"]}), encoding="utf-8"
        )
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(
            json.dumps({
                "version": 1,
                "slots": [{
                    "slotId": "telegram-polysim",
                    "stateDir": str(slot_dir),
                    "preferredProjects": ["polysimulator"],
                    "enabled": True,
                }],
            }),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_configured_pool_reaches_the_coordinator_notification_hook(self):
        from coordinator import Coordinator

        packet = {
            "status": "blocked",
            "status_reason": "Prerequisite missing",
            "request": {
                "id": "req-coordinator-hook",
                "state": "implementation",
                "session": "sess-coordinator-owner",
                "issue_url": "https://github.com/Bavariance/polysimulator/issues/75",
            },
            "blocker": "dokploy_staging compose unhealthy",
        }

        env = {
            "TELEGRAM_NOTIFY_CHAT_ID": "1247617658",
            "TELEGRAM_BOT_TOKEN": "1000000000:AAsynthetic",
            "VEYYON_MANIFEST_PATH": str(self.manifest),
            "VEYYON_CHANNELS_DIR": str(self.channels_dir),
        }
        with patch.dict(os.environ, env), patch("urllib.request.urlopen") as transport:
            # Nothing in the environment names a pool: only the coordinator's own
            # configuration can enable correlation here.
            os.environ.pop("VEYYON_POOL_DB", None)
            transport.return_value.__enter__.return_value.read.return_value = json.dumps({
                "ok": True,
                "result": {
                    "message_id": 660066,
                    "from": {"id": 54321},
                    "chat": {"id": 1247617658},
                },
            }).encode("utf-8")

            coordinator = Coordinator(
                state_dir=str(self.state_dir),
                notify_telegram=True,
                telegram_dry_run=False,
                telegram_send=True,
                telegram_pool_db=str(self.pool_db),
            )
            receipt = coordinator.maybe_notify_telegram(_PacketStub(packet))

        self.assertEqual(receipt["status"], "sent", receipt["reason"])
        self.assertEqual(receipt["correlation_source"], "explicit")
        self.assertTrue(receipt["correlation_recorded"], receipt["reason"])

        row = OutboundCorrelationStore(self.pool_db).lookup("54321", "1247617658", 660066)
        self.assertIsNotNone(row, "the coordinator hook recorded no correlation")
        self.assertEqual(row["session_id"], "sess-coordinator-owner")

        # Deduplication state belongs to the configured state directory, not the cwd.
        self.assertTrue((self.state_dir / "telegram_notify_state.json").is_file())
        self.assertFalse((Path.cwd() / "telegram_notify_state.json").exists())


class _PacketStub:
    """Minimal stand-in for CoordinatorPacket: the hook only calls to_dict()."""

    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


if __name__ == "__main__":
    unittest.main(verbosity=2)
