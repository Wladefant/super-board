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

import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch

try:
    from session_crash_monitor import (
        CrashMonitorStateLedger,
        HerdrPaneError,
        HerdrPanes,
        PlannedStopEvaluator,
        ProcessProbe,
        SessionCrashMonitor,
        SessionExitLogReader,
        SessionProcessInfo,
        SessionRegistry,
        build_interrupted_lane_manifest,
        build_resume_command,
        default_resume_argv,
        format_crash_alert_text,
        utc_now_iso,
    )
    from telegram_notifier import DeliveryReceipt, NotificationEvent
except ImportError:
    from workflows.portable.session_crash_monitor import (
        CrashMonitorStateLedger,
        HerdrPaneError,
        HerdrPanes,
        PlannedStopEvaluator,
        ProcessProbe,
        SessionCrashMonitor,
        SessionExitLogReader,
        SessionProcessInfo,
        SessionRegistry,
        build_interrupted_lane_manifest,
        build_resume_command,
        default_resume_argv,
        format_crash_alert_text,
        utc_now_iso,
    )
    from workflows.portable.telegram_notifier import DeliveryReceipt, NotificationEvent


class MockProcessProbe(ProcessProbe):
    """Controllable process probe for unit testing."""

    def __init__(self):
        self.processes: Dict[int, SessionProcessInfo] = {}
        self.command_lines: Dict[int, str] = {}
        self.image_names: Dict[int, str] = {}
    def set_process(
        self,
        pid: int,
        session_id: str,
        creation_time_utc: str,
        is_alive: bool,
        exit_code: Optional[int] = None,
        command_line: Optional[str] = None,
        image_name: Optional[str] = None,
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
        if image_name:
            self.image_names[pid] = image_name
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
    def get_image_name(self, pid: int) -> Optional[str]:
        return self.image_names.get(pid, "veyyon.exe")


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
        self.assertEqual(event.canonical_link, "https://github.com/orgs/Bavariance/projects/1")
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

    def test_nested_session_marker_survives_source_refresh(self):
        home = Path(self.temp_dir.name)
        marker = home / ".veyyon" / "profiles" / "default" / "agent" / "sessions" / "project" / self.session_id / "local" / "planned-restart-marker.json"
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({
            "session_id": self.session_id,
            "target_pid": 12345,
            "owner": "operator",
            "status": "planned",
            "created_utc": utc_now_iso(),
        }), encoding="utf-8")
        with patch.object(Path, "home", return_value=home):
            result = PlannedStopEvaluator.check_planned_stop(self.session_id, 12345)
        self.assertIsNotNone(result)
        self.assertEqual(result["matched_marker_path"], str(marker))

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
        # Write planned restart marker with verified owner and unexpired timestamp
        marker_data = {
            "schema": "veyyon/planned-restart-marker/v1",
            "session_id": self.session_id,
            "target_pid": 12345,
            "owner": "RuntimeCrashCutover",
            "status": "planned",
            "reason": "planned_binary_cutover_by_RuntimeCrashCutover",
            "created_utc": utc_now_iso(),
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
        # Write planned restart marker for proc2 with verified owner and timestamp
        marker_data = {
            "schema": "veyyon/planned-restart-marker/v1",
            "session_id": self.session_id,
            "target_pid": pid2,
            "owner": "test_operator",
            "status": "planned",
            "reason": "clean_disposable_test_shutdown",
            "created_utc": utc_now_iso(),
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


    def test_old_complete_or_expired_marker_does_not_suppress_crash(self):
        """Old 'complete' marker, expired timestamp, or missing owner does NOT suppress a future crash."""
        probe = MockProcessProbe()
        notifier = MockTelegramAdapter()

        # 1. Test 'complete' marker does NOT suppress crash
        probe.set_process(
            pid=22222,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=True,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )
        complete_marker = {
            "schema": "veyyon/planned-restart-marker/v1",
            "session_id": self.session_id,
            "target_pid": 22222,
            "owner": "RuntimeCrashCutover",
            "status": "complete",  # Completed prior restart; must not suppress new crash!
            "created_utc": utc_now_iso(),
        }
        self.marker_file.write_text(json.dumps(complete_marker), encoding="utf-8")

        monitor = SessionCrashMonitor(
            session_id=self.session_id,
            pid=22222,
            state_file=self.state_file,
            marker_paths=[self.marker_file],
            notifier_adapter=notifier,
            probe=probe,
        )
        monitor.bind_target()

        # Process crashes
        probe.set_process(
            pid=22222,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=False,
            exit_code=1,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )
        res = monitor.step()
        self.assertEqual(res["status"], "terminated")
        self.assertEqual(res["classification"], "UNEXPECTED_TERMINATION")
        self.assertEqual(notifier.delivery_count, 1)

        # 2. Test expired marker (older than 900s) does NOT suppress crash
        probe.set_process(
            pid=33333,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=True,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )
        expired_marker = {
            "schema": "veyyon/planned-restart-marker/v1",
            "session_id": self.session_id,
            "target_pid": 33333,
            "owner": "RuntimeCrashCutover",
            "status": "planned",
            "created_utc": "2026-09-01T00:00:00Z",  # 7 days old!
        }
        self.marker_file.write_text(json.dumps(expired_marker), encoding="utf-8")

        monitor2 = SessionCrashMonitor(
            session_id=self.session_id,
            pid=33333,
            state_file=self.state_file,
            marker_paths=[self.marker_file],
            notifier_adapter=notifier,
            probe=probe,
        )
        monitor2.bind_target()

        probe.set_process(
            pid=33333,
            session_id=self.session_id,
            creation_time_utc="2026-09-08T00:00:00Z",
            is_alive=False,
            exit_code=1,
            command_line=f"veyyon.exe --resume {self.session_id}",
        )
        res2 = monitor2.step()
        self.assertEqual(res2["status"], "terminated")
        self.assertEqual(res2["classification"], "UNEXPECTED_TERMINATION")
        self.assertEqual(notifier.delivery_count, 2)



class FakeHerdr:
    """Stand-in for the herdr CLI that records what would be typed into a pane."""

    def __init__(self, pane_id: Optional[str] = "w9:pZ", fails: bool = False):
        self.pane_id = pane_id
        self.fails = fails
        self.pane_queries: List[int] = []
        self.runs: List[Tuple[str, str]] = []

    def pane_for_pid(self, pid: int, probe) -> Optional[str]:
        self.pane_queries.append(pid)
        return self.pane_id

    def run_in_pane(self, pane_id: str, command: str) -> Tuple[bool, str]:
        self.runs.append((pane_id, command))
        if self.fails:
            return False, "herdr pane run exited 1: pane not found"
        return True, f"herdr pane run {pane_id} {command}"


class RegistryAwareProbe(MockProcessProbe):
    """Mock probe that also answers what the registry live-owner check asks the OS."""

    def __init__(self, veyyon_pids: Optional[set] = None):
        super().__init__()
        self.veyyon_pids = set(veyyon_pids or ())

    def is_veyyon_process(self, pid: int) -> bool:
        if self.veyyon_pids:
            return pid in self.veyyon_pids
        return super().is_veyyon_process(pid)

    def get_process_ancestors(self, pid: int, limit: int = 16) -> List[int]:
        return [pid]


class TestSessionCrashAutoResume(unittest.TestCase):
    """Auto-resume: relaunch the dead session exactly once, in the pane it died in."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state_file = self.root / "state.json"
        self.registry_dir = self.root / "terminals"
        self.registry_dir.mkdir()
        self.log_dir = self.root / "logs"
        self.log_dir.mkdir()
        self.manifest_dir = self.root / "manifests"
        self.session_id = "01a0496f-64f6-733e-a9a6-89f15fc2a437"
        self.pid = 28624
        self.creation_time = "2026-09-26T11:30:00+00:00"
        self.command_line = f"veyyon.exe --resume {self.session_id}"

    def tearDown(self):
        self.temp_dir.cleanup()

    def set_target(self, probe, is_alive: bool, exit_code: Optional[int] = None, pid=None):
        probe.set_process(
            pid=pid or self.pid,
            session_id=self.session_id,
            creation_time_utc=self.creation_time,
            is_alive=is_alive,
            exit_code=exit_code,
            command_line=self.command_line,
        )

    def build_monitor(self, probe, herdr, notifier, **overrides):
        kwargs: Dict[str, Any] = {
            "session_id": self.session_id,
            "pid": self.pid,
            "state_file": self.state_file,
            "poll_interval": 0.1,
            "notifier_adapter": notifier,
            "probe": probe,
            "auto_resume": True,
            "resume_argv": ["veyyon.exe", "--resume", self.session_id],
            "herdr": herdr,
            "registry": SessionRegistry(self.registry_dir),
            "log_dir": self.log_dir,
            "inflight_dir": self.log_dir / "inflight",
            "manifest_dir": self.manifest_dir,
        }
        kwargs.update(overrides)
        return SessionCrashMonitor(**kwargs)

    def test_relaunches_once_in_the_pane_the_session_died_in(self):
        probe = RegistryAwareProbe()
        self.set_target(probe, is_alive=True)
        herdr = FakeHerdr(pane_id="w9:pZ")
        notifier = MockTelegramAdapter()

        monitor = self.build_monitor(probe, herdr, notifier)
        ok, message = monitor.bind_target()
        self.assertTrue(ok, message)
        self.assertEqual(monitor.target_pane_id, "w9:pZ")
        # The pane has to be pinned while the target is still alive to be located.
        self.assertEqual(herdr.pane_queries, [self.pid])

        self.set_target(probe, is_alive=False, exit_code=1)
        result = monitor.step()

        self.assertEqual(result["status"], "terminated")
        self.assertEqual(result["classification"], "UNEXPECTED_TERMINATION")
        resume = result["auto_resume"]
        self.assertTrue(resume["launched"], resume["reason"])
        self.assertEqual(resume["pane_id"], "w9:pZ")
        self.assertIsNotNone(resume["launched_at_utc"])

        self.assertEqual(len(herdr.runs), 1)
        pane_id, command = herdr.runs[0]
        self.assertEqual(pane_id, "w9:pZ")
        self.assertIn(f"--resume {self.session_id}", command)
        self.assertEqual(resume["command"], command)

        # The alert carries the outcome, so the operator learns it from the alert alone.
        self.assertEqual(notifier.delivery_count, 1)
        event = notifier.sent_events[0]
        self.assertIn("Auto-resume: relaunched in herdr pane", event.metadata["details"])
        self.assertTrue(event.metadata["auto_resume"]["launched"])

        manifest = json.loads(Path(resume["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "veyyon/interrupted-lane-manifest/v1")
        self.assertEqual(manifest["dead_pid"], self.pid)
        self.assertTrue(manifest["resume"]["launched"])

        # A restarted supervisor replaying the same death must not resume it twice.
        restarted = self.build_monitor(probe, herdr, notifier)
        restarted.target_pid = self.pid
        restarted.bound_target = monitor.bound_target
        self.assertEqual(restarted.step()["status"], "already_processed")
        self.assertEqual(len(herdr.runs), 1)

    def test_refuses_to_relaunch_while_the_registry_names_a_live_owner(self):
        live_pid = 987654
        (self.registry_dir / f"{live_pid}-aaaa.json").write_text(
            json.dumps(
                {
                    "sessionId": self.session_id,
                    "pid": live_pid,
                    "cwd": "/workspace/veyyon",
                    "sessionFile": None,
                }
            ),
            encoding="utf-8",
        )

        probe = RegistryAwareProbe(veyyon_pids={live_pid})
        self.set_target(probe, is_alive=True)
        # Started before its own registry entry was written, as a real owner is.
        probe.set_process(
            pid=live_pid,
            session_id=self.session_id,
            creation_time_utc="2026-09-01T00:00:00+00:00",
            is_alive=True,
            command_line="veyyon.exe",
        )
        herdr = FakeHerdr(pane_id="w9:pZ")
        notifier = MockTelegramAdapter()
        monitor = self.build_monitor(probe, herdr, notifier)
        ok, message = monitor.bind_target()
        self.assertTrue(ok, message)

        self.set_target(probe, is_alive=False, exit_code=1)
        result = monitor.step()

        resume = result["auto_resume"]
        self.assertFalse(resume["launched"])
        self.assertEqual(resume["live_owner_pid"], live_pid)
        self.assertIn(str(live_pid), resume["reason"])
        self.assertEqual(herdr.runs, [])
        # Only the relaunch is withheld; the death is still reported.
        self.assertEqual(result["classification"], "UNEXPECTED_TERMINATION")
        self.assertEqual(notifier.delivery_count, 1)

    def test_refuses_to_relaunch_while_the_registry_names_a_renamed_live_owner(self):
        live_pid = 23768
        (self.registry_dir / f"{live_pid}-bbbb.json").write_text(
            json.dumps(
                {
                    "sessionId": self.session_id,
                    "pid": live_pid,
                    "cwd": "/workspace/veyyon",
                    "sessionFile": None,
                }
            ),
            encoding="utf-8",
        )

        probe = RegistryAwareProbe()
        self.set_target(probe, is_alive=True)
        probe.set_process(
            pid=live_pid,
            session_id=self.session_id,
            creation_time_utc="2026-09-01T00:00:00+00:00",
            is_alive=True,
            command_line="veyyon.exe.bak-2026-09-26-6158132",
            image_name="veyyon.exe.bak-2026-09-26-6158132",
        )
        self.assertTrue(probe.is_veyyon_process(live_pid))

        herdr = FakeHerdr(pane_id="w9:pZ")
        notifier = MockTelegramAdapter()
        monitor = self.build_monitor(probe, herdr, notifier)
        ok, message = monitor.bind_target()
        self.assertTrue(ok, message)

        self.set_target(probe, is_alive=False, exit_code=1)
        result = monitor.step()

        resume = result["auto_resume"]
        self.assertFalse(resume["launched"])
        self.assertEqual(resume["live_owner_pid"], live_pid)
        self.assertIn(str(live_pid), resume["reason"])
        self.assertEqual(herdr.runs, [])
    def test_declines_when_there_is_no_pane_to_resume_in(self):
        probe = RegistryAwareProbe()
        self.set_target(probe, is_alive=True)
        herdr = FakeHerdr(pane_id=None)
        notifier = MockTelegramAdapter()
        monitor = self.build_monitor(probe, herdr, notifier)
        ok, message = monitor.bind_target()
        self.assertTrue(ok, message)
        self.assertIsNone(monitor.target_pane_id)

        self.set_target(probe, is_alive=False, exit_code=1)
        resume = monitor.step()["auto_resume"]
        self.assertFalse(resume["launched"])
        self.assertIn("never resolved", resume["reason"])
        self.assertEqual(herdr.runs, [])

    def test_reports_a_failed_pane_relaunch_instead_of_claiming_success(self):
        probe = RegistryAwareProbe()
        self.set_target(probe, is_alive=True)
        herdr = FakeHerdr(pane_id="w9:pZ", fails=True)
        monitor = self.build_monitor(probe, herdr, MockTelegramAdapter())
        monitor.bind_target()

        self.set_target(probe, is_alive=False, exit_code=1)
        resume = monitor.step()["auto_resume"]
        self.assertFalse(resume["launched"])
        self.assertIn("pane relaunch failed", resume["reason"])
        self.assertIsNone(resume["launched_at_utc"])

    def test_does_not_launch_when_auto_resume_is_off(self):
        probe = RegistryAwareProbe()
        self.set_target(probe, is_alive=True)
        herdr = FakeHerdr(pane_id="w9:pZ")
        notifier = MockTelegramAdapter()
        monitor = self.build_monitor(probe, herdr, notifier, auto_resume=False)
        ok, message = monitor.bind_target()
        self.assertTrue(ok, message)

        self.set_target(probe, is_alive=False, exit_code=1)
        result = monitor.step()
        self.assertIsNone(result["auto_resume"])
        self.assertEqual(herdr.runs, [])
        self.assertEqual(notifier.delivery_count, 1)

        # Interrupted-lane manifest is written even when auto-resume is off
        self.assertIsNotNone(result.get("manifest_path"))
        manifest_file = Path(result["manifest_path"])
        self.assertTrue(manifest_file.exists())
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(manifest["dead_pid"], self.pid)
        self.assertIsNone(manifest["resume"])

    def test_resume_command_targets_the_session_and_quotes_only_what_needs_it(self):
        argv = default_resume_argv(self.session_id)
        self.assertEqual(argv[1:], ["--resume", self.session_id])
        self.assertIn(f"--resume {self.session_id}", build_resume_command(argv))
        # A path with spaces has to survive being typed into a pane's shell.
        self.assertEqual(
            build_resume_command(
                ["C:/Program Files/veyyon.exe", "--resume", self.session_id]
            ),
            f'"C:/Program Files/veyyon.exe" --resume {self.session_id}',
        )

    def test_manifest_names_the_lanes_that_died_with_the_session(self):
        lane_a = "11111111-1111-1111-1111-111111111111"
        lane_b = "22222222-2222-2222-2222-222222222222"
        lines = [
            {
                "timestamp": "2026-09-26T11:36:03.700Z",
                "pid": self.pid,
                "message": "Unhandled rejection",
                "err": {
                    "name": "Error",
                    "message": "ENOSPC: no space left on device, write",
                    "code": "ENOSPC",
                },
            },
            {
                "timestamp": "2026-09-26T11:36:03.800Z",
                "pid": self.pid,
                "message": "Session exit recorded",
                "sessionId": self.session_id,
                "sessionFile": "C:/sessions/Main.md",
                "reason": "unhandled_rejection",
                "kind": "fatal",
                "pendingToolCalls": 0,
            },
            {
                "timestamp": "2026-09-26T11:36:03.810Z",
                "pid": self.pid,
                "message": "Session exit recorded",
                "sessionId": lane_a,
                "sessionFile": "C:/sessions/FixBugsA.md",
                "reason": "parent_process_exit",
                "kind": "fatal",
                "pendingToolCalls": 2,
            },
            {
                "timestamp": "2026-09-26T11:36:03.820Z",
                "pid": self.pid,
                "message": "Session exit recorded",
                "sessionId": lane_b,
                "sessionFile": "C:/sessions/FixBugsB.md",
                "reason": "parent_process_exit",
                "kind": "fatal",
                "pendingToolCalls": 1,
            },
            # Noise that must not be counted as an interrupted lane.
            {
                "timestamp": "2026-09-26T11:36:03.830Z",
                "pid": self.pid,
                "message": "Session exit recorded",
                "sessionId": "33333333-3333-3333-3333-333333333333",
                "sessionFile": "C:/sessions/Planned.md",
                "reason": "normal",
                "kind": "planned",
                "pendingToolCalls": 0,
            },
            {
                "timestamp": "2026-09-26T11:36:03.840Z",
                "pid": 999999,
                "message": "Session exit recorded",
                "sessionId": "44444444-4444-4444-4444-444444444444",
                "sessionFile": "C:/sessions/Other.md",
                "reason": "unhandled_rejection",
                "kind": "fatal",
                "pendingToolCalls": 0,
            },
        ]
        (self.log_dir / "veyyon.2026-09-26.log").write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
        )

        reader = SessionExitLogReader(self.log_dir)
        since = datetime.datetime(2026, 9, 26, 11, 35, tzinfo=datetime.timezone.utc)
        until = datetime.datetime(2026, 9, 26, 11, 37, tzinfo=datetime.timezone.utc)

        exits = reader.read_fatal_exits(self.pid, since, until)
        self.assertEqual(
            [record.session_id for record in exits], [self.session_id, lane_a, lane_b]
        )
        rejections = reader.read_unhandled_rejections(self.pid, since, until)
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0]["code"], "ENOSPC")

        manifest = build_interrupted_lane_manifest(
            session_id=self.session_id,
            dead_pid=self.pid,
            creation_time_utc=self.creation_time,
            death_observed_utc=utc_now_iso(),
            exit_code=None,
            observed_reason="Process terminated unexpectedly (process absent without planned stop marker)",
            exit_records=exits,
            rejection_records=rejections,
            in_flight_markers=[],
        )
        self.assertEqual(manifest["interrupted_lane_count"], 2)
        lanes = {lane["session_id"]: lane for lane in manifest["interrupted_lanes"]}
        self.assertEqual(set(lanes), {lane_a, lane_b})
        self.assertEqual(lanes[lane_a]["lane"], "FixBugsA")
        self.assertEqual(lanes[lane_a]["pending_tool_calls"], 2)
        self.assertEqual(manifest["main_exit_record"]["session_id"], self.session_id)
        self.assertEqual(manifest["unhandled_rejections"][0]["code"], "ENOSPC")
        self.assertIsNone(manifest["resume"])

    def test_reads_the_real_1136z_enospc_crash_from_the_live_log(self):
        """The reader must reproduce the actual 11:36:03Z ENOSPC death of pid 28624."""
        log_dir = Path.home() / ".veyyon" / "profiles" / "default" / "logs"
        target_dir = log_dir
        temp_dir = None
        if not list(log_dir.glob("veyyon.2026-09-26.log*")):
            temp_dir = tempfile.TemporaryDirectory()
            target_dir = Path(temp_dir.name)
            fixture_lines = [
                {
                    "timestamp": "2026-09-26T11:36:03.700Z",
                    "pid": 28624,
                    "message": "Unhandled rejection",
                    "err": {
                        "name": "Error",
                        "message": "ENOSPC: no space left on device, write",
                        "code": "ENOSPC",
                    },
                },
                {
                    "timestamp": "2026-09-26T11:36:03.800Z",
                    "pid": 28624,
                    "message": "Session exit recorded",
                    "sessionId": self.session_id,
                    "sessionFile": "C:/sessions/Main.md",
                    "reason": "unhandled_rejection",
                    "kind": "fatal",
                    "pendingToolCalls": 0,
                },
                {
                    "timestamp": "2026-09-26T11:36:03.850Z",
                    "pid": 28624,
                    "message": "Session exit recorded",
                    "sessionId": "11111111-1111-1111-1111-111111111111",
                    "lane": "FixBugsA",
                    "reason": "unhandled_rejection",
                    "kind": "fatal",
                    "pendingToolCalls": 2,
                },
            ]
            (target_dir / "veyyon.2026-09-26.log").write_text(
                "\n".join(json.dumps(ln) for ln in fixture_lines) + "\n",
                encoding="utf-8",
            )

        try:
            reader = SessionExitLogReader(target_dir)
            since = datetime.datetime(2026, 9, 26, 11, 35, tzinfo=datetime.timezone.utc)
            until = datetime.datetime(2026, 9, 26, 11, 37, tzinfo=datetime.timezone.utc)

            rejections = reader.read_unhandled_rejections(28624, since, until)
            self.assertTrue(rejections, "no unhandled rejection found in the real crash window")
            self.assertEqual(rejections[0]["code"], "ENOSPC")

            exits = reader.read_fatal_exits(28624, since, until)
            self.assertGreater(len(exits), 1, "the death must name the lanes it took with it")
            self.assertIn("unhandled_rejection", {record.reason for record in exits})
        finally:
            if temp_dir is not None:
                temp_dir.cleanup()


class TestHerdrPanesRunner(unittest.TestCase):
    """HerdrPanes error handling with an injected runner."""

    def test_run_in_pane_success(self):
        def runner(argv, timeout):
            return 0, "", ""

        herdr = HerdrPanes(runner=runner)
        ok, msg = herdr.run_in_pane("p1", "ls")
        self.assertTrue(ok)
        self.assertEqual(msg, "herdr pane run p1 ls")

    def test_run_in_pane_nonzero_exit(self):
        def runner(argv, timeout):
            return 2, "", "pane not found"

        herdr = HerdrPanes(runner=runner)
        ok, msg = herdr.run_in_pane("p1", "ls")
        self.assertFalse(ok)
        self.assertIn("exited 2", msg)
        self.assertIn("pane not found", msg)

    def test_run_in_pane_exception(self):
        def runner(argv, timeout):
            raise FileNotFoundError("herdr not on PATH")

        herdr = HerdrPanes(runner=runner)
        ok, msg = herdr.run_in_pane("p1", "ls")
        self.assertFalse(ok)
        self.assertIn("FileNotFoundError", msg)

    def test_list_panes_nonzero_raises_error(self):
        def runner(argv, timeout):
            return 1, "", "daemon unreachable"

        herdr = HerdrPanes(runner=runner)
        with self.assertRaises(HerdrPaneError):
            herdr.list_panes()

    def test_pane_for_pid_returns_none_when_herdr_fails(self):
        def runner(argv, timeout):
            raise RuntimeError("herdr broken")

        herdr = HerdrPanes(runner=runner)
        probe = MockProcessProbe()
        self.assertIsNone(herdr.pane_for_pid(1234, probe))


class TestCrashMonitorStateLedger(unittest.TestCase):
    """CrashMonitorStateLedger concurrent claims and corrupt state handling."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state_file = self.root / "state.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_two_ledgers_racing_claim_termination(self):
        ledger1 = CrashMonitorStateLedger(self.state_file)
        ledger2 = CrashMonitorStateLedger(self.state_file)
        claim1 = {"supervisor_pid": 111}
        claim2 = {"supervisor_pid": 222}

        # First claim wins
        self.assertTrue(
            ledger1.claim_termination("s1", 1000, "2026-09-01T00:00:00Z", claim1)
        )
        # Second claim fails
        self.assertFalse(
            ledger2.claim_termination("s1", 1000, "2026-09-01T00:00:00Z", claim2)
        )

    def test_corrupt_state_file_fails_closed(self):
        self.state_file.write_text("NOT_VALID_JSON{{{", encoding="utf-8")
        ledger = CrashMonitorStateLedger(self.state_file)
        self.assertTrue(ledger.is_corrupt())
        # Must return False rather than overwriting the corrupt file or crashing
        self.assertFalse(
            ledger.claim_termination("s1", 1000, "2026-09-01T00:00:00Z", {"pid": 111})
        )
        # Content remains untouched
        self.assertEqual(self.state_file.read_text(encoding="utf-8"), "NOT_VALID_JSON{{{")

    def test_valid_state_file_is_not_corrupt(self):
        self.state_file.write_text("{}", encoding="utf-8")
        ledger = CrashMonitorStateLedger(self.state_file)
        self.assertFalse(ledger.is_corrupt())
if __name__ == "__main__":
    unittest.main()
