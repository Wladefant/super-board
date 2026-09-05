#!/usr/bin/env python3
"""
workflows/test_telegram_notifier.py — Unit Tests for Portable Telegram Notification Adapter

Covers:
1. Event schema validation and event filtering.
2. Secret and path sanitization.
3. Single-sentence message formatting and canonical link attachment.
4. CoordinatorPacket ingestion and translation.
5. ProjectSlotResolver multi-repo resolution and credential retrieval.
6. DeduplicationLedger hashing, deduplication windows, per-request cooldowns, and rate limits.
7. Mocked Telegram API transport (success, HTTP errors, connection failures, invalid chat).
8. Zero credential leaks in outputs or traces.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from telegram_notifier import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_DEDUP_WINDOW_SECONDS,
    DEFAULT_GLOBAL_MIN_INTERVAL,
    VALID_EVENT_TYPES,
    DeduplicationLedger,
    DeliveryReceipt,
    NotificationEvent,
    ProjectSlotResolver,
    SecretSanitizer,
    TelegramNotificationAdapter,
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
        self.adapter = TelegramNotificationAdapter(resolver=self.resolver, ledger=self.ledger)

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

    def test_decision_refusal_retired_and_resolved(self):
        """Verify retired, rejected, resolved, and answered decisions are strictly refused."""
        for bad_status in ("rejected", "resolved", "answered", "closed", "retired", "done"):
            dec = {
                "decision_id": "DEC-100",
                "request_id": "req-100",
                "status": bad_status,
                "question": "Some question?",
            }
            is_valid, reason = TelegramNotificationAdapter.is_decision_notifiable(dec)
            self.assertFalse(is_valid, f"Status '{bad_status}' should be refused")
            self.assertIn("non-actionable", reason)
            self.assertIsNone(TelegramNotificationAdapter.from_decision(dec))

    def test_decision_refusal_synthetic_and_demo(self):
        """Verify synthetic test probes and demo decisions are strictly refused."""
        synthetic_decisions = [
            {"decision_id": "DEC-4543-01", "request_id": "req-synthetic-decision-demo-4543", "status": "pending", "question": "Q?"},
            {"decision_id": "DEC-SYNTHETIC-99", "request_id": "req-real-01", "status": "pending", "question": "Q?"},
            {"decision_id": "DEC-DEMO-01", "request_id": "req-real-01", "status": "pending", "question": "Q?"},
            {"decision_id": "DEC-RETIRED-01", "request_id": "req-real-01", "status": "pending", "question": "Q?"},
            {"decision_id": "DEC-REAL-01", "request_id": "req-real-01", "status": "pending", "is_synthetic": True, "question": "Q?"},
            {"decision_id": "DEC-REAL-02", "request_id": "req-real-02", "status": "pending", "provenance": "synthetic_test", "question": "Q?"},
            {"decision_id": "DEC-REAL-03", "request_id": "req-real-03", "status": "pending", "provenance": "agent_authored", "question": "Q?"},
        ]
        for dec in synthetic_decisions:
            is_valid, reason = TelegramNotificationAdapter.is_decision_notifiable(dec)
            self.assertFalse(is_valid, f"Synthetic decision '{dec['decision_id']}' should be refused")
            self.assertIsNone(TelegramNotificationAdapter.from_decision(dec))

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
