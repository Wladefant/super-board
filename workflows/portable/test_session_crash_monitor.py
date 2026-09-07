#!/usr/bin/env python3
"""Comprehensive test suite for the independent Telegram session crash monitor.

Tests:
1. Normal / expected exit with planned stop marker -> SUPPRESSED (no alert)
2. Unexpected termination (crash) -> ALERT SENT / RECORDED
3. Duplicate observation -> DEDUPLICATED (single alert per owner termination)
4. PID reuse scenario -> detects creation time divergence, triggers original termination
5. Quiet / slow model / paused goal -> process alive, returns running, ZERO alert
6. Transport down / network failure -> records unsent_transport_down durably, zero crash
7. Real disposable process integration test with actual OS subprocess
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple

try:
    from session_crash_monitor import (
        CrashMonitorStateLedger,
        PlannedStopEvaluator,
        ProcessProbe,
        SessionCrashMonitor,
        SessionProcessInfo,
        format_crash_alert_text,
    )
    from telegram_notifier import DeliveryReceipt, NotificationEvent
except ImportError:
    from workflows.portable.session_crash_monitor import (
        CrashMonitorStateLedger,
        PlannedStopEvaluator,
        ProcessProbe,
        SessionCrashMonitor,
        SessionProcessInfo,
        format_crash_alert_text,
    )
    from workflows.portable.telegram_notifier import DeliveryReceipt, NotificationEvent


class MockProcessProbe(ProcessProbe):
    """Controllable process probe for unit testing."""

    def __init__(self):
        self.processes: Dict[int, SessionProcessInfo] = {}
        self.command_lines: Dict[int, str] = {}

    def set_process(
        self,
        pid: int,
        session_id: str,
        creation_time_utc: str,
        is_alive: bool,
        exit_code: Optional[int] = None,
        command_line: Optional[str] = None,
    ):
        self.processes[pid] = SessionProcessInfo(
            session_id=session_id,
            pid=pid,
            creation_time_utc=creation_time_utc,
            is_alive=is_alive,
            exit_code=exit_code,
            command_line=command_line,
        )
        if command_line:
            self.command_lines[pid] = command_line

    def get_process_info(
        self, pid: int, expected_creation_time: Optional[str] = None
    ) -> Optional[SessionProcessInfo]:
        info = self.processes.get(pid)
        if not info:
            return None
        if expected_creation_time and info.creation_time_utc != expected_creation_time:
            # PID reuse detected: different creation time means original process is dead
            return SessionProcessInfo(
                session_id=info.session_id,
                pid=pid,
                creation_time_utc=info.creation_time_utc,
                is_alive=False,
                exit_code=None,
            )
        return info

    def get_command_line(self, pid: int) -> Optional[str]:
        return self.command_lines.get(pid)


class MockTelegramAdapter:
    """Mock Telegram adapter that records outbound events."""

    def __init__(self, should_fail: bool = False):
        self.should_fail = should_fail
        self.sent_events: List[NotificationEvent] = []
        self.delivery_count = 0

    def notify(
        self,
        event: NotificationEvent,
        dry_run: bool = False,
        force: bool = False,
    ) -> DeliveryReceipt:
        if self.should_fail:
            raise ConnectionError("Simulated Telegram Bot API network failure")

        self.sent_events.append(event)
        self.delivery_count += 1
        return DeliveryReceipt(
            delivered=True,
            status="delivered",
            reason="Mock delivery successful",
            event_signature="mock_sig_" + str(self.delivery_count),
            message_id=99000 + self.delivery_count,
            slot_id="telegram-polysim",
            session_id=event.session_id,
        )


class TestSessionCrashMonitor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp_dir.name) / "test_state.json"
        self.marker_file = Path(self.temp_dir.name) / "planned-restart-marker.json"
        self.session_id = "01a0496f-64f6-733e-a9a6-89f15fc2a437"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_quiet_slow_model_alive_not_a_crash(self):
        """Invariant: a slow turn or paused goal where process is alive is NEVER a crash."""
        probe = MockProcessProbe()
        probe.set_process(
            pid=12345,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=True,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )
        notifier = MockTelegramAdapter()

        monitor = SessionCrashMonitor(
            session_id=self.session_id,
            pid=12345,
            state_file=self.state_file,
            marker_paths=[self.marker_file],
            notifier_adapter=notifier,
            probe=probe,
        )

        # Run 3 consecutive cycles while process remains alive
        for _ in range(3):
            result = monitor.step()
            self.assertEqual(result["status"], "running")
            self.assertEqual(result["pid"], 12345)

        self.assertEqual(notifier.delivery_count, 0)
        self.assertEqual(len(notifier.sent_events), 0)

    def test_unexpected_crash_triggers_alert_and_records_state(self):
        """Unexpected termination sends Telegram alert and records durable state."""
        probe = MockProcessProbe()
        probe.set_process(
            pid=12345,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=True,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )
        notifier = MockTelegramAdapter()

        monitor = SessionCrashMonitor(
            session_id=self.session_id,
            pid=12345,
            state_file=self.state_file,
            marker_paths=[self.marker_file],
            notifier_adapter=notifier,
            probe=probe,
        )

        # Initial bind
        res1 = monitor.step()
        self.assertEqual(res1["status"], "running")
        self.assertEqual(notifier.delivery_count, 0)

        # Now simulate unexpected crash with exit code 1
        probe.set_process(
            pid=12345,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=False,
            exit_code=1,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )

        res2 = monitor.step()
        self.assertEqual(res2["status"], "terminated")
        self.assertEqual(res2["classification"], "UNEXPECTED_TERMINATION")
        self.assertEqual(notifier.delivery_count, 1)

        # Verify alert content
        event = notifier.sent_events[0]
        self.assertEqual(event.session_id, self.session_id)
        details_text = event.metadata.get("details", "")
        self.assertIn("Observed PID: 12345", details_text)
        self.assertIn("code 1", details_text)
        self.assertIn("--resume", details_text)

        # Verify state ledger on disk
        ledger = CrashMonitorStateLedger(self.state_file)
        self.assertTrue(ledger.has_processed(self.session_id, 12345, "2026-09-08T00:00:00Z"))
        record = ledger.get_record(self.session_id, 12345, "2026-09-08T00:00:00Z")
        self.assertEqual(record["classification"], "UNEXPECTED_TERMINATION")
        self.assertTrue(record["alert_sent"])

    def test_planned_stop_suppression(self):
        """Planned stop marker suppresses Telegram crash alert."""
        probe = MockProcessProbe()
        probe.set_process(
            pid=12345,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=True,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )
        notifier = MockTelegramAdapter()

        # Write planned restart marker
        marker_data = {
            "schema": "veyyon/planned-restart-marker/v1",
            "session_id": self.session_id,
            "target_pid": 12345,
            "status": "planned",
            "reason": "planned_binary_cutover_by_RuntimeCrashCutover",
            "created_utc": "2026-09-08T00:05:00Z",
        }
        self.marker_file.write_text(json.dumps(marker_data), encoding="utf-8")

        monitor = SessionCrashMonitor(
            session_id=self.session_id,
            pid=12345,
            state_file=self.state_file,
            marker_paths=[self.marker_file],
            notifier_adapter=notifier,
            probe=probe,
        )

        monitor.bind_target()

        # Simulate process termination
        probe.set_process(
            pid=12345,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=False,
            exit_code=0,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )

        res = monitor.step()
        self.assertEqual(res["status"], "terminated")
        self.assertEqual(res["classification"], "PLANNED_STOP_SUPPRESSED")

        # Zero Telegram alerts sent!
        self.assertEqual(notifier.delivery_count, 0)
        self.assertEqual(len(notifier.sent_events), 0)

        # State records suppression
        ledger = CrashMonitorStateLedger(self.state_file)
        record = ledger.get_record(self.session_id, 12345, "2026-09-08T00:00:00Z")
        self.assertIsNotNone(record)
        self.assertEqual(record["classification"], "PLANNED_STOP_SUPPRESSED")
        self.assertFalse(record["alert_sent"])

    def test_deduplication_single_alert_per_owner_termination(self):
        """Subsequent checks on already-processed termination do NOT send repeated alerts."""
        probe = MockProcessProbe()
        probe.set_process(
            pid=12345,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=True,
            exit_code=None,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )
        notifier = MockTelegramAdapter()

        monitor = SessionCrashMonitor(
            session_id=self.session_id,
            pid=12345,
            state_file=self.state_file,
            marker_paths=[self.marker_file],
            notifier_adapter=notifier,
            probe=probe,
        )
        monitor.bind_target()

        # Now process terminates
        probe.set_process(
            pid=12345,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=False,
            exit_code=1,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )

        # First step triggers alert
        res1 = monitor.step()
        self.assertEqual(res1["status"], "terminated")
        self.assertEqual(notifier.delivery_count, 1)

        # Second step is deduplicated
        res2 = monitor.step()
        self.assertEqual(res2["status"], "already_processed")
        self.assertEqual(notifier.delivery_count, 1)  # Still exactly 1!

    def test_pid_reuse_detection(self):
        """PID reuse with different creation time is detected as termination of target instance."""
        probe = MockProcessProbe()
        # Original target process
        probe.set_process(
            pid=12345,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=True,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )
        notifier = MockTelegramAdapter()

        monitor = SessionCrashMonitor(
            session_id=self.session_id,
            pid=12345,
            state_file=self.state_file,
            marker_paths=[self.marker_file],
            notifier_adapter=notifier,
            probe=probe,
        )
        monitor.bind_target()

        # Now simulate OS PID reuse: PID 12345 now exists, is alive, but has NEW creation time
        probe.set_process(
            pid=12345,
            session_id="unrelated_process",
            creation_time_utc="2026-09-08T01:00:00Z",
            is_alive=True,
            command_line="notepad.exe",
        )

        res = monitor.step()
        self.assertEqual(res["status"], "terminated")
        self.assertEqual(res["classification"], "UNEXPECTED_TERMINATION")
        self.assertEqual(notifier.delivery_count, 1)

        # Target recorded is the original creation time
        ledger = CrashMonitorStateLedger(self.state_file)
        self.assertTrue(ledger.has_processed(self.session_id, 12345, "2026-09-08T00:00:00Z"))

    def test_transport_failure_records_unsent_status_safely(self):
        """Transport failure records unsent_transport_down without crashing."""
        probe = MockProcessProbe()
        probe.set_process(
            pid=12345,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=True,
            exit_code=None,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )
        # Notifier that simulates network failure
        notifier = MockTelegramAdapter(should_fail=True)

        monitor = SessionCrashMonitor(
            session_id=self.session_id,
            pid=12345,
            state_file=self.state_file,
            marker_paths=[self.marker_file],
            notifier_adapter=notifier,
            probe=probe,
        )
        monitor.bind_target()

        # Now process terminates
        probe.set_process(
            pid=12345,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=False,
            exit_code=1,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )

        res = monitor.step()
        self.assertEqual(res["status"], "terminated")
        self.assertEqual(res["classification"], "UNEXPECTED_TERMINATION")
        receipt = res["receipt"]
        self.assertFalse(receipt["delivered"])
        self.assertEqual(receipt["status"], "unsent_transport_down")

        # Unsent queue contains the record
        state_data = monitor.state_ledger._load_locked()
        self.assertEqual(len(state_data["unsent_events"]), 1)
        unsent = state_data["unsent_events"][0]
        self.assertEqual(unsent["pid"], 12345)
        self.assertEqual(unsent["session_id"], self.session_id)

    def test_real_disposable_subprocess_normal_and_planned_exit(self):
        """Integration test with real OS subprocess simulating planned and unexpected exits."""
        real_probe = ProcessProbe()

        # 1. Spawn a short-lived disposable subprocess that exits with code 42
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys, time; time.sleep(0.3); sys.exit(42)"]
        )
        pid = proc.pid

        notifier = MockTelegramAdapter()
        monitor = SessionCrashMonitor(
            session_id=self.session_id,
            pid=pid,
            state_file=self.state_file,
            marker_paths=[self.marker_file],
            notifier_adapter=notifier,
            probe=real_probe,
        )

        ok, msg = monitor.bind_target()
        self.assertTrue(ok, f"Binding failed: {msg}")
        self.assertIsNotNone(monitor.bound_target)

        # Wait for the process to exit
        proc.wait(timeout=5)

        # Next step detects exit with code 42
        res = monitor.step()
        self.assertEqual(res["status"], "terminated")
        self.assertEqual(res["classification"], "UNEXPECTED_TERMINATION")
        self.assertEqual(notifier.delivery_count, 1)
        self.assertEqual(res["record"]["exit_code"], 42)

        # 2. Test planned stop on another real disposable subprocess
        proc2 = subprocess.Popen(
            [sys.executable, "-c", "import sys, time; time.sleep(0.3); sys.exit(0)"]
        )
        pid2 = proc2.pid

        # Write planned restart marker for proc2
        marker_data = {
            "schema": "veyyon/planned-restart-marker/v1",
            "session_id": self.session_id,
            "target_pid": pid2,
            "status": "planned",
            "reason": "clean_disposable_test_shutdown",
        }
        self.marker_file.write_text(json.dumps(marker_data), encoding="utf-8")

        monitor2 = SessionCrashMonitor(
            session_id=self.session_id,
            pid=pid2,
            state_file=self.state_file,
            marker_paths=[self.marker_file],
            notifier_adapter=notifier,
            probe=real_probe,
        )
        ok2, msg2 = monitor2.bind_target()
        self.assertTrue(ok2, f"Binding failed for proc2: {msg2}")

        proc2.wait(timeout=5)
        res2 = monitor2.step()
        self.assertEqual(res2["status"], "terminated")
        self.assertEqual(res2["classification"], "PLANNED_STOP_SUPPRESSED")
        # Delivery count should still be 1 (no new alert)
        self.assertEqual(notifier.delivery_count, 1)


if __name__ == "__main__":
    unittest.main()
