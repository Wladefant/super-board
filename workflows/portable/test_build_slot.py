#!/usr/bin/env python3
"""
test_build_slot.py - Unit tests for Build & Browser Slot Arbiter.

Part of portable workflow core in Wladefant/super-board.
References:
  - Superboard Issue #227 (High-Trust Agent Architecture)
  - Profile AGENTS.md §2 (Exclusive Build & Browser Slot)

Tests:
  1. Basic acquire and release lifecycle.
  2. Non-owner release refusal.
  3. Stale lock reclamation for dead PIDs.
  4. Stale lock reclamation for age threshold expiration.
  5. RAM guard queued wait (>=95%), timeout, and force override.
  6. FIFO queue ordering and clean queue logic.
  7. Concurrent subprocess FIFO serialization.
  8. CLI subprocess status, JSON, acquire, and release.
"""

from __future__ import annotations

import datetime
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

# Ensure workflows/portable is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import build_slot
from build_slot import BuildSlotManager, is_pid_alive, _queue_atomic_lock


class TestBuildSlot(unittest.TestCase):
    """Unit tests for the BuildSlotManager lock arbiter."""

    def test_transition_guard_recovers_dead_owner(self):
        path = os.path.join(self.run_dir, "build-slot.guard")
        code = (
            "import os,sys; sys.path.insert(0,sys.argv[1]); import build_slot; "
            "g=build_slot._transition_guard(sys.argv[2]); g.__enter__(); os._exit(0)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code, SCRIPT_DIR, path],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        sidecar = path + ".owner.json"
        with open(sidecar, encoding="utf-8") as stream:
            dead_owner = json.load(stream)
        self.assertFalse(is_pid_alive(dead_owner["pid"]))
        started = time.monotonic()
        with build_slot._transition_guard(path, timeout=0.5):
            with open(sidecar, encoding="utf-8") as stream:
                owner = json.load(stream)
            self.assertEqual(owner["pid"], os.getpid())
            self.assertNotEqual(owner["token"], dead_owner["token"])
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(os.path.exists(sidecar))

    def test_stale_checks_do_not_hold_transition_guard(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(manager.acquire("probe", timeout=2))
        observed = []
        def probe(pid):
            held = getattr(build_slot._guard_state, "held", set())
            observed.append(os.path.abspath(os.path.join(self.run_dir, "build-slot.guard")) in held)
            return True
        with mock.patch.object(manager, "is_pid_alive", side_effect=probe):
            manager.check_stale_and_reclaim()
        self.assertEqual(observed, [False])

    def test_reclaim_preserves_heartbeat_refreshed_after_stale_judgment(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(manager.acquire("refreshing", timeout=2))
        info = manager._read_lock_info()
        token = info["token"]
        info["heartbeat_at_epoch"] = time.time() - 60
        build_slot._write_json_atomic(os.path.join(manager.slot_dirs[0], build_slot.INFO_FILE_NAME), info)
        original_reclaim = manager._tombstone_stale_slot
        refreshed = []
        def refresh_then_reclaim(slot_idx, judged, reason):
            refreshed.append(manager.heartbeat_lock("refreshing", token))
            return original_reclaim(slot_idx, judged, reason)
        with mock.patch.object(manager, "is_pid_alive", return_value=True), \
                mock.patch.object(manager, "_tombstone_stale_slot", side_effect=refresh_then_reclaim):
            reclaimed = manager.check_stale_and_reclaim(acquire_holder_stale_after=1)
        self.assertEqual(refreshed, [True])
        self.assertFalse(reclaimed, "A refreshed live lease must not be reclaimed")
        self.assertEqual(manager._read_lock_info()["token"], token)

    def test_release_metadata_retry_waits_outside_transition_guard(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(manager.acquire("read-retry", timeout=2))
        original_read = build_slot._read_json_file
        failed = []
        waits = []
        def read_once(path):
            if not failed:
                failed.append(True)
                raise PermissionError("transient metadata read")
            return original_read(path)
        def wait_outside_guard(delay):
            waits.append(bool(getattr(build_slot._guard_state, "held", ())))
        with mock.patch("build_slot._read_json_file", side_effect=read_once), \
                mock.patch("build_slot.time.sleep", side_effect=wait_outside_guard):
            self.assertTrue(manager.release("read-retry"))
        self.assertEqual(waits, [False], "Metadata retry must leave the transition guard free")

    def test_stale_detached_cleanup_leaves_transition_guard_free(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(manager.acquire("dead-cleanup", timeout=2))
        observed = []
        original_cleanup = shutil.rmtree
        def cleanup(path, *args, **kwargs):
            observed.append(bool(getattr(build_slot._guard_state, "held", ())))
            return original_cleanup(path, *args, **kwargs)
        with mock.patch.object(manager, "is_pid_alive", return_value=False), \
                mock.patch("build_slot.shutil.rmtree", side_effect=cleanup):
            self.assertTrue(manager.check_stale_and_reclaim())
        self.assertEqual(observed, [False], "Detached cleanup must not block state transitions")

    def test_release_queue_cleanup_does_not_hold_transition_guard(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(manager.acquire("probe", timeout=2))
        def cleanup(*args):
            held = getattr(build_slot._guard_state, "held", set())
            self.assertNotIn(os.path.abspath(os.path.join(self.run_dir, "build-slot.guard")), held)
        with mock.patch.object(manager, "_dequeue_best_effort", side_effect=cleanup):
            self.assertTrue(manager.release("probe"))

    def test_release_output_does_not_block_other_guard_users(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(manager.acquire("probe", timeout=2))
        def output(*args, **kwargs):
            results = []
            def contender():
                with build_slot._transition_guard(os.path.join(self.run_dir, "build-slot.guard"), timeout=0.2):
                    results.append(True)
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(timeout=1)
            self.assertEqual(results, [True])
        with mock.patch("builtins.print", side_effect=output):
            self.assertTrue(manager.release("probe"))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test-build-slot-")
        self.run_dir = self.tmp.name
        self.orig_ram = os.environ.get("BUILD_SLOT_RAM_PERCENT")
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "80.0"
        self.orig_avail_gib = os.environ.get("BUILD_SLOT_AVAILABLE_GIB")
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "64"
        self.orig_allow_force = os.environ.get("BUILD_SLOT_ALLOW_FORCE")
        self.orig_stagger = os.environ.get("BUILD_SLOT_STAGGER_SECONDS")
        os.environ["BUILD_SLOT_STAGGER_SECONDS"] = "0"

    def tearDown(self):
        if self.orig_stagger is not None:
            os.environ["BUILD_SLOT_STAGGER_SECONDS"] = self.orig_stagger
        else:
            os.environ.pop("BUILD_SLOT_STAGGER_SECONDS", None)
        if self.orig_ram is not None:
            os.environ["BUILD_SLOT_RAM_PERCENT"] = self.orig_ram
        else:
            os.environ.pop("BUILD_SLOT_RAM_PERCENT", None)
        if self.orig_avail_gib is not None:
            os.environ["BUILD_SLOT_AVAILABLE_GIB"] = self.orig_avail_gib
        else:
            os.environ.pop("BUILD_SLOT_AVAILABLE_GIB", None)
        if self.orig_allow_force is not None:
            os.environ["BUILD_SLOT_ALLOW_FORCE"] = self.orig_allow_force
        else:
            os.environ.pop("BUILD_SLOT_ALLOW_FORCE", None)
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def test_basic_acquire_and_release(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        stat = manager.status()
        self.assertFalse(stat["lock"]["locked"])
        self.assertIsNone(stat["lock"]["owner"])

        # Acquire lock
        acquired = manager.acquire(
            name="test-lane-1",
            timeout=2.0,
            poll_interval=0.05,
        )
        self.assertTrue(acquired)

        # Check status shows locked
        stat_locked = manager.status()
        self.assertTrue(stat_locked["lock"]["locked"])
        self.assertEqual(stat_locked["lock"]["owner"], "test-lane-1")
        self.assertEqual(stat_locked["lock"]["pid"], os.getpid())

        # Release lock
        released = manager.release("test-lane-1")
        self.assertTrue(released)

        # Check status after release
        stat_after = manager.status()
        self.assertFalse(stat_after["lock"]["locked"])
        self.assertIsNone(stat_after["lock"]["owner"])

    def test_release_by_non_owner_refused(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        acquired = manager.acquire("lane-owner", timeout=5.0, poll_interval=0.05)
        self.assertTrue(acquired)

        # Attempt release by another lane
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            refused = manager.release("lane-impostor")
        self.assertFalse(refused)
        self.assertTrue(os.path.isdir(manager.lock_dir))

        captured = stderr_buf.getvalue()
        self.assertIn("Refusing to release build slot lock", captured)
        self.assertIn("lane-owner", captured)

        # Lock is still held by lane-owner
        self.assertTrue(manager.is_held_by("lane-owner"))

        # Correct owner releases
        self.assertTrue(manager.release("lane-owner"))
        self.assertFalse(os.path.exists(manager.lock_dir))

    def test_stale_reclaim_dead_pid(self):
        manager = BuildSlotManager(run_dir=self.run_dir)

        # Simulate a lock acquired by a dead process (PID 999999)
        os.makedirs(manager.lock_dir, exist_ok=True)
        dead_pid = 999999
        past_epoch = time.time() - 120.0
        info = {
            "owner": "dead-lane",
            "pid": dead_pid,
            "acquired_at": datetime.datetime.fromtimestamp(past_epoch, datetime.timezone.utc).isoformat(),
            "acquired_at_epoch": past_epoch,
        }
        with open(manager.info_file, "w", encoding="utf-8") as f:
            json.dump(info, f)

        self.assertFalse(is_pid_alive(dead_pid))
        self.assertTrue(os.path.isdir(manager.lock_dir))

        # Status call should detect dead PID and reclaim the lock
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            stat = manager.status()
        self.assertTrue(stat["stale_reclaimed_in_status"])
        self.assertFalse(stat["lock"]["locked"])
        self.assertFalse(os.path.exists(manager.lock_dir))

        captured = stderr_buf.getvalue()
        self.assertIn("[NOTICE] Reclaiming stale build slot lock", captured)
        self.assertIn("is dead", captured)

        # A new lane can acquire immediately
        self.assertTrue(manager.acquire("new-lane", timeout=2.0, poll_interval=0.05))
        self.assertTrue(manager.release("new-lane"))

    def test_live_acquire_owner_with_fresh_heartbeat_never_reclaimed_on_age(self):
        """
        #315, narrowed by #620: an `acquire` lock whose owner is alive and that keeps
        heartbeating (`build_slot.py heartbeat <name>`) keeps the slot however old it is.
        Silence past 30 minutes is what frees an abandoned slot.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        os.makedirs(manager.lock_dir, exist_ok=True)
        past_epoch = time.time() - 3600.0
        info = {
            "owner": "long-build-lane",
            "pid": os.getpid(),
            "acquired_at": datetime.datetime.fromtimestamp(past_epoch, datetime.timezone.utc).isoformat(),
            "acquired_at_epoch": past_epoch,
            "heartbeat_at_epoch": time.time() - 5.0,
        }
        with open(manager.info_file, "w", encoding="utf-8") as f:
            json.dump(info, f)

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            reclaimed = manager.check_stale_and_reclaim()
        self.assertFalse(reclaimed, stderr_buf.getvalue())
        self.assertTrue(os.path.isdir(manager.lock_dir))
        self.assertEqual(manager.status()["lock"]["owner"], "long-build-lane")

    def test_acquisition_proceeds_at_90_percent_ram_with_two_plus_slots_held(self):
        """At 90% RAM (under 95% guard), multiple slots (2+) can be acquired concurrently."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        with mock.patch.object(build_slot, "get_system_ram_percent", return_value=90.0):
            self.assertTrue(manager.acquire("lane-1", timeout=1.0, poll_interval=0.02))
            self.assertTrue(manager.acquire("lane-2", timeout=1.0, poll_interval=0.02))
            self.assertTrue(manager.acquire("lane-3", timeout=1.0, poll_interval=0.02))
            st = manager.status()
            self.assertEqual(st["active_slots"], 3)
            self.assertEqual(set(st["holders"]), {"lane-1", "lane-2", "lane-3"})
            self.assertTrue(manager.release("lane-1"))
            self.assertTrue(manager.release("lane-2"))
            self.assertTrue(manager.release("lane-3"))

    def test_ram_guard_waits_at_95_percent(self):
        """At >= 95% RAM, acquire waits in queue until timeout."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        stderr_buf = io.StringIO()
        with mock.patch.object(build_slot, "get_system_ram_percent", return_value=95.0):
            with redirect_stderr(stderr_buf):
                acquired = manager.acquire("high-ram-lane", timeout=0.4, poll_interval=0.02)
        self.assertFalse(acquired)
        self.assertIn("'high-ram-lane' stays queued and waits", stderr_buf.getvalue())
        self.assertIn("95.0%", stderr_buf.getvalue())
        self.assertIn("system RAM remains at 95.0%", stderr_buf.getvalue())
        self.assertEqual(manager.clean_queue(), [])

    def test_ram_guard_does_not_admit_when_idle_and_ram_stays_high(self):
        """
        #620 adaptation: under memory safety contract, idle bypass is obsolete.
        At >= 95% RAM or exhausted memory budget, the queue head is not admitted even when idle.
        """
        manager = BuildSlotManager(run_dir=self.run_dir, ram_guard_idle_admit_after=0.1, acquisition_stagger=0)
        with mock.patch.object(build_slot, "get_system_ram_percent", return_value=98.0):
            with redirect_stderr(io.StringIO()):
                acquired = manager.acquire("idle-lane", timeout=0.3, poll_interval=0.02)
                self.assertFalse(acquired)
        self.assertFalse(manager.is_held_by("idle-lane"))

    def test_ram_guard_still_waits_when_a_slot_is_held(self):
        """Negative control: with a build running, high RAM keeps the guard on."""
        manager = BuildSlotManager(run_dir=self.run_dir, acquisition_stagger=0)
        with mock.patch.object(build_slot, "get_system_ram_percent", return_value=50.0):
            self.assertTrue(manager.acquire("running-lane", timeout=2.0, poll_interval=0.02))
        with mock.patch.object(build_slot, "get_system_ram_percent", return_value=98.0):
            with redirect_stderr(io.StringIO()):
                self.assertFalse(manager.acquire("waiting-lane", timeout=0.4, poll_interval=0.02))
        self.assertTrue(manager.release("running-lane"))

    def test_ram_guard_idle_admit_default_is_bounded(self):
        val = getattr(build_slot, "DEFAULT_RAM_GUARD_IDLE_ADMIT_SECONDS", None)
        if val is not None:
            self.assertTrue(0 < val <= 600)

    def test_release_frees_slot_even_when_queue_lock_times_out(self):
        """#620: release must not fail after the slot is gone because the queue file is busy."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(manager.acquire("busy-queue-lane", timeout=2.0, poll_interval=0.02))
        manager.queue_cleanup_grace = 0.2
        with mock.patch.object(manager, "dequeue", side_effect=TimeoutError("queue lock busy")):
            with redirect_stderr(io.StringIO()):
                self.assertTrue(manager.release("busy-queue-lane"))
        self.assertFalse(os.path.isdir(manager.lock_dir))

    def test_release_deadline_ends_stalled_process_with_exit_124(self):
        """#620: a release stalled by a thrashing host exits 124 instead of hanging its caller."""
        script_dir = os.path.dirname(os.path.abspath(build_slot.__file__))
        code = (
            "import sys, time; sys.path.insert(0, sys.argv[1]); import build_slot; "
            "build_slot._arm_deadline(0.5, 'release'); time.sleep(60)"
        )
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-c", code, script_dir],
            capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.returncode, 124, proc.stderr)
        self.assertLess(time.monotonic() - started, 20)
        self.assertIn("release timed out after 0.5s", proc.stderr)

    def test_cli_release_accepts_timeout_and_completes(self):
        """#620: the new --timeout option does not disturb a normal release."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(manager.acquire("cli-rel-lane", timeout=2.0, poll_interval=0.02))
        proc = subprocess.run(
            [sys.executable, os.path.abspath(build_slot.__file__), "--run-dir", self.run_dir,
             "release", "cli-rel-lane", "--timeout", "30"],
            capture_output=True, text=True, timeout=45, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.isdir(manager.lock_dir))

    def test_ram_guard_force_bypasses_at_96_percent(self):
        """At 96% RAM (>= 95%), --force bypasses the RAM guard only when BUILD_SLOT_ALLOW_FORCE=1."""
        manager = BuildSlotManager(run_dir=self.run_dir)

        # Without BUILD_SLOT_ALLOW_FORCE=1, force does not bypass the RAM guard
        os.environ.pop("BUILD_SLOT_ALLOW_FORCE", None)
        with mock.patch.object(build_slot, "get_system_ram_percent", return_value=96.0):
            with redirect_stderr(io.StringIO()):
                acquired = manager.acquire("force-lane-noenv", timeout=0.2, poll_interval=0.02, force=True)
            self.assertFalse(acquired)

        # With BUILD_SLOT_ALLOW_FORCE=1, force bypasses the RAM guard
        os.environ["BUILD_SLOT_ALLOW_FORCE"] = "1"
        stderr_buf = io.StringIO()
        with mock.patch.object(build_slot, "get_system_ram_percent", return_value=96.0):
            with redirect_stderr(stderr_buf):
                acquired = manager.acquire("force-lane", timeout=0.5, poll_interval=0.02, force=True)
        self.assertTrue(acquired)
        self.assertIn("[NOTICE] RAM guard overridden with --force", stderr_buf.getvalue())
        self.assertIn("96.0%", stderr_buf.getvalue())
        self.assertTrue(manager.is_held_by("force-lane"))
        self.assertTrue(manager.release("force-lane"))
    def test_ram_guard_waits_in_queue_until_ram_drops(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        ram_values = iter([95.0, 95.0, 95.0])
        queued_names_while_waiting = []

        def fake_ram():
            value = next(ram_values, 50.0)
            if value >= 95.0:
                queued_names_while_waiting.append(
                    [item["name"] for item in manager._read_queue()]
                )
            return value

        stderr_buf = io.StringIO()
        with mock.patch.object(build_slot, "get_system_ram_percent", side_effect=fake_ram):
            with redirect_stderr(stderr_buf), redirect_stdout(io.StringIO()):
                acquired = manager.acquire("waiting-lane", timeout=5.0, poll_interval=0.02)

        self.assertTrue(acquired)
        self.assertIn("'waiting-lane' stays queued and waits", stderr_buf.getvalue())
        # The initial guard check happens before enqueue; every later high-RAM poll
        # inside the queue loop must see the lane holding its queue place.
        self.assertEqual(queued_names_while_waiting[0], [])
        self.assertEqual(queued_names_while_waiting[1:], [["waiting-lane"], ["waiting-lane"]])
        self.assertEqual(manager.status()["lock"]["owner"], "waiting-lane")
        self.assertEqual(manager.clean_queue(), [])
        self.assertTrue(manager.release("waiting-lane"))

    def test_ram_guard_times_out_after_waiting_and_cleans_queue(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        timeout = 0.4
        stderr_buf = io.StringIO()
        with mock.patch.object(build_slot, "get_system_ram_percent", return_value=95.0):
            with redirect_stderr(stderr_buf):
                started = time.monotonic()
                acquired = manager.acquire("stuck-lane", timeout=timeout, poll_interval=0.02)
                elapsed = time.monotonic() - started

        self.assertFalse(acquired)
        self.assertGreaterEqual(elapsed, timeout)
        self.assertIn("system RAM remains at 95.0%", stderr_buf.getvalue())
        self.assertEqual(manager.clean_queue(), [])
        self.assertFalse(manager.status()["lock"]["locked"])
    def test_fifo_queue_order(self):
        active_pids = {1001: True, 1002: True, 1003: True, 1004: True}
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: active_pids.get(p, False), max_slots=1)

        # Lane 1 holds the lock
        self.assertTrue(manager.acquire("lane-1", pid=1001, timeout=1.0, poll_interval=0.02))

        # While Lane 1 holds it, Lane 2, Lane 3, Lane 4 enqueue in order
        idx2 = manager.enqueue("lane-2", pid=1002)
        idx3 = manager.enqueue("lane-3", pid=1003)
        idx4 = manager.enqueue("lane-4", pid=1004)

        self.assertEqual(idx2, 0)
        # Lane 3 tries to acquire while Lane 2 is ahead -> cannot acquire because not at head
        res_lane3 = manager.acquire("lane-3", pid=1003, timeout=0.05, poll_interval=0.02)
        self.assertFalse(res_lane3)
        # Because lane-3 timed out, it was dequeued; re-enqueue lane-3 and lane-4 to verify order
        manager.enqueue("lane-3", pid=1003)
        q = manager.clean_queue()
        self.assertEqual([x["name"] for x in q], ["lane-2", "lane-4", "lane-3"])

        # Lane 1 releases
        self.assertTrue(manager.release("lane-1"))

        # Lane 2 is head of queue and acquires
        res_lane2 = manager.acquire("lane-2", pid=1002, timeout=1.0, poll_interval=0.02)
        self.assertTrue(res_lane2)
        self.assertTrue(manager.is_held_by("lane-2", pid=1002))
        self.assertTrue(manager.release("lane-2"))

        # Next in line is Lane 4 (which arrived before Lane 3 re-enqueued)
        q_after_2 = manager.clean_queue()
        self.assertEqual([x["name"] for x in q_after_2], ["lane-4", "lane-3"])

        # Lane 4 acquires next
        res_lane4 = manager.acquire("lane-4", pid=1004, timeout=1.0, poll_interval=0.02)
        self.assertTrue(res_lane4)
        self.assertTrue(manager.release("lane-4"))

        # Lane 3 acquires last
        res_lane3_retry = manager.acquire("lane-3", pid=1003, timeout=1.0, poll_interval=0.02)
        self.assertTrue(res_lane3_retry)
        self.assertTrue(manager.release("lane-3"))

        self.assertEqual(manager.clean_queue(), [])

    def test_cli_subprocesses(self):
        script = os.path.abspath(build_slot.__file__)
        env = dict(os.environ)
        env["BUILD_SLOT_RAM_PERCENT"] = "80.0"
        # 1. Status: FREE
        p_stat = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "status"],
            capture_output=True,
            text=True,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(p_stat.returncode, 0)
        self.assertIn("Status:      FREE", p_stat.stdout)

        # 2. Status with --json
        p_json = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "status", "--json"],
            capture_output=True,
            text=True,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(p_json.returncode, 0)
        data = json.loads(p_json.stdout)
        self.assertFalse(data["lock"]["locked"])
        self.assertEqual(data["queue_depth"], 0)

        # 3. Acquire via CLI
        my_pid = str(os.getpid())
        p_acq = subprocess.run(
            [
                sys.executable,
                script,
                "--run-dir",
                self.run_dir,
                "acquire",
                "cli-lane",
                "--pid",
                my_pid,
                "--timeout",
                "5",
                "--poll-interval",
                "0.05",
            ],
            capture_output=True,
            text=True,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(p_acq.returncode, 0)

        # 3b. Re-acquire via CLI by same lane name and PID should print "already held" and return 0 (idempotent)
        p_reacq = subprocess.run(
            [
                sys.executable,
                script,
                "--run-dir",
                self.run_dir,
                "acquire",
                "cli-lane",
                "--pid",
                my_pid,
                "--timeout",
                "5",
            ],
            capture_output=True,
            text=True,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(p_reacq.returncode, 0)
        self.assertIn("already held", p_reacq.stdout)

        # 4. Status should show locked
        p_stat2 = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "status"],
            capture_output=True,
            text=True,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(p_stat2.returncode, 0)
        self.assertIn("Status:      LOCKED", p_stat2.stdout)
        self.assertIn("Owner:       cli-lane", p_stat2.stdout)

        # 5. Release via CLI
        p_rel = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "release", "cli-lane"],
            capture_output=True,
            text=True,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(p_rel.returncode, 0)

        # 6. Status back to FREE
        p_stat3 = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "status"],
            capture_output=True,
            text=True,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(p_stat3.returncode, 0)
        self.assertIn("Status:      FREE", p_stat3.stdout)

    def test_concurrent_subprocesses_fifo(self):
        script = os.path.abspath(build_slot.__file__)
        record_file = os.path.join(self.run_dir, "acquisition_order.txt")
        env = dict(os.environ)
        env["BUILD_SLOT_RAM_PERCENT"] = "80.0"
        worker_code = (
            "import os, sys, time, subprocess\n"
            "name = sys.argv[1]\n"
            "script = sys.argv[2]\n"
            "run_dir = sys.argv[3]\n"
            "record_file = sys.argv[4]\n"
            "p_acq = subprocess.run([sys.executable, script, '--run-dir', run_dir, 'acquire', name, '--timeout', '15', '--poll-interval', '0.02'])\n"
            "if p_acq.returncode != 0:\n"
            "    sys.exit(1)\n"
            "with open(record_file, 'a', encoding='utf-8') as f:\n"
            "    f.write(name + ':' + str(time.time()) + '\\n')\n"
            "time.sleep(0.06)\n"
            "p_rel = subprocess.run([sys.executable, script, '--run-dir', run_dir, 'release', name])\n"
            "sys.exit(p_rel.returncode)\n"
        )
        manager = BuildSlotManager(run_dir=self.run_dir)
        procs = []
        for i in range(1, 4):
            w_name = f"subproc-{i}"
            p = subprocess.Popen(
                [sys.executable, "-c", worker_code, w_name, script, self.run_dir, record_file],
                env=env,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            procs.append(p)
            # Wait until w_name has enqueued or acquired before launching next to guarantee arrival ordering
            for _ in range(50):
                q = manager._read_queue()
                info = manager._read_lock_info()
                if (info and info.get("owner") == w_name) or any(x.get("name") == w_name for x in q):
                    break
                time.sleep(0.02)

        for p in procs:
            ret = p.wait()
            self.assertEqual(ret, 0, "A concurrent worker subprocess failed")

        with open(record_file, "r", encoding="utf-8") as f:
            lines = [line.strip().split(":")[0] for line in f if line.strip()]

        self.assertEqual(lines, ["subproc-1", "subproc-2", "subproc-3"])

    def test_queue_reclaim_heartbeat_and_negative_control(self):
        # Living PIDs: is_pid_alive returns True for 2002, 2004, 2005; False for dead 2001, 2003
        living_pids = {2002, 2004, 2005}
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: p in living_pids)
        now = time.time()

        # Seed queue with:
        # 1. Stale heartbeat entry under dead PID (heartbeat 65s ago) -> must be reclaimed
        # 2. Fresh heartbeat entry under living PID (heartbeat 5s ago) -> must NOT be reclaimed (negative control)
        # 3. Legacy entry without heartbeat_at older than 30m under dead PID -> must be reclaimed
        # 4. Legacy entry without heartbeat_at newer than 30m under living PID -> must NOT be reclaimed
        # 5. Stale heartbeat entry under living PID (heartbeat 65s ago) -> must be reclaimed under 60s contract (#711)
        seeded_queue = [
            {
                "name": "dead-stale-hb-lane",
                "pid": 2001,
                "token": "tok-dead-stale",
                "enqueued_at": now - 100.0,
                "enqueued_at_iso": datetime.datetime.fromtimestamp(now - 100.0, datetime.timezone.utc).isoformat(),
                "heartbeat_at": now - 65.0,
                "heartbeat_at_iso": datetime.datetime.fromtimestamp(now - 65.0, datetime.timezone.utc).isoformat(),
            },
            {
                "name": "fresh-hb-lane",
                "pid": 2002,
                "token": "tok-fresh",
                "enqueued_at": now - 20.0,
                "enqueued_at_iso": datetime.datetime.fromtimestamp(now - 20.0, datetime.timezone.utc).isoformat(),
                "heartbeat_at": now - 5.0,
                "heartbeat_at_iso": datetime.datetime.fromtimestamp(now - 5.0, datetime.timezone.utc).isoformat(),
            },
            {
                "name": "legacy-stale-lane",
                "pid": 2003,
                "token": None,
                "enqueued_at": now - 1850.0,
                "enqueued_at_iso": datetime.datetime.fromtimestamp(now - 1850.0, datetime.timezone.utc).isoformat(),
            },
            {
                "name": "legacy-fresh-lane",
                "pid": 2004,
                "token": None,
                "enqueued_at": now - 300.0,
                "enqueued_at_iso": datetime.datetime.fromtimestamp(now - 300.0, datetime.timezone.utc).isoformat(),
            },
            {
                "name": "stale-live-hb-lane",
                "pid": 2005,
                "token": "tok-stale-live",
                "enqueued_at": now - 150.0,
                "enqueued_at_iso": datetime.datetime.fromtimestamp(now - 150.0, datetime.timezone.utc).isoformat(),
                "heartbeat_at": now - 65.0,
                "heartbeat_at_iso": datetime.datetime.fromtimestamp(now - 65.0, datetime.timezone.utc).isoformat(),
            },
        ]
        manager._write_queue(seeded_queue)

        # Run clean_queue
        cleaned = manager.clean_queue()
        names = [x["name"] for x in cleaned]

        # Dead PID with stale heartbeat reclaimed
        self.assertNotIn("dead-stale-hb-lane", names)
        # Fresh heartbeat preserved (negative control!)
        self.assertIn("fresh-hb-lane", names)
        # Legacy stale (> 30m) under dead PID reclaimed
        self.assertNotIn("legacy-stale-lane", names)
        # Legacy fresh (<= 30m) under living PID preserved
        self.assertIn("legacy-fresh-lane", names)
        # Stale heartbeat under living PID reclaimed under 60s contract (#711)
        self.assertNotIn("stale-live-hb-lane", names)

        # Survivors in FIFO order by original enqueue time
        self.assertEqual(names, ["legacy-fresh-lane", "fresh-hb-lane"])
    def test_acquire_timeout_leaves_no_entry_behind(self):
        manager = BuildSlotManager(run_dir=self.run_dir, max_slots=1)

        # Lock is held by owner-lane
        self.assertTrue(manager.acquire("owner-lane", timeout=1.0))
        self.assertTrue(manager.is_held_by("owner-lane"))

        # Waiter lane tries to acquire with short timeout -> fails on timeout
        res = manager.acquire("waiter-lane", timeout=0.05, poll_interval=0.02)
        self.assertFalse(res)

        # Confirm queue has NO entry left behind for waiter-lane
        queue = manager._read_queue()
        waiter_entries = [q for q in queue if q.get("name") == "waiter-lane"]
        self.assertEqual(waiter_entries, [])

        # Confirm release cleans up owner
        self.assertTrue(manager.release("owner-lane"))
        self.assertEqual(manager._read_queue(), [])

    def test_acquire_exception_leaves_no_entry_behind(self):
        manager = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        self.assertTrue(manager.acquire("blocker-lane", timeout=1.0))

        # Force an exception / KeyboardInterrupt during acquire loop while waiting
        with mock.patch.object(time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                manager.acquire("interrupted-lane", timeout=10.0, poll_interval=0.02)

        # Confirm interrupted-lane was dequeued by token in finally
        queue = manager._read_queue()
        self.assertEqual([q for q in queue if q.get("name") == "interrupted-lane"], [])
        self.assertTrue(manager.release("blocker-lane"))

    def test_in_process_lanes_same_pid_fifo_and_stale_reclaim(self):
        main_pid = 35296
        dead_pid = 999999
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: p == main_pid)

        now = time.time()
        # Simulate dead-lane, lane-1, and lane-2
        lane1_token = "tok-lane-1"
        lane2_token = "tok-lane-2"
        seeded_queue = [
            {
                "name": "dead-lane",
                "pid": dead_pid,
                "token": "tok-dead",
                "enqueued_at": now - 120.0,
                "enqueued_at_iso": datetime.datetime.fromtimestamp(now - 120.0, datetime.timezone.utc).isoformat(),
                "heartbeat_at": now - 70.0,
                "heartbeat_at_iso": datetime.datetime.fromtimestamp(now - 70.0, datetime.timezone.utc).isoformat(),
            },
            {
                "name": "lane-1",
                "pid": main_pid,
                "token": lane1_token,
                "enqueued_at": now - 80.0,
                "enqueued_at_iso": datetime.datetime.fromtimestamp(now - 80.0, datetime.timezone.utc).isoformat(),
                "heartbeat_at": now - 15.0,  # Active waiter: heartbeat fresh
                "heartbeat_at_iso": datetime.datetime.fromtimestamp(now - 15.0, datetime.timezone.utc).isoformat(),
            },
            {
                "name": "lane-2",
                "pid": main_pid,
                "token": lane2_token,
                "enqueued_at": now - 10.0,
                "enqueued_at_iso": datetime.datetime.fromtimestamp(now - 10.0, datetime.timezone.utc).isoformat(),
                "heartbeat_at": now - 2.0,  # Active waiter: heartbeat fresh
                "heartbeat_at_iso": datetime.datetime.fromtimestamp(now - 2.0, datetime.timezone.utc).isoformat(),
            },
        ]
        manager._write_queue(seeded_queue)

        # Before reclaim: all 3 in queue
        q_before = manager._read_queue()
        self.assertEqual([x["name"] for x in q_before], ["dead-lane", "lane-1", "lane-2"])

        # Reclaim runs: dead-lane is pruned; active lane-1 is preserved in FIFO order
        q_after = manager.clean_queue()
        self.assertEqual([x["name"] for x in q_after], ["lane-1", "lane-2"])

        # Once lane-1 is dequeued (or finishes), lane-2 can acquire the lock
        manager.dequeue("lane-1", pid=main_pid, token=lane1_token)
        self.assertTrue(manager.acquire("lane-2", pid=main_pid, token=lane2_token, timeout=1.0, poll_interval=0.02))
        self.assertTrue(manager.is_held_by("lane-2", pid=main_pid))
        self.assertTrue(manager.release("lane-2"))
        self.assertEqual(manager.clean_queue(), [])
    def test_acquire_loop_writes_heartbeat(self):
        manager = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        self.assertTrue(manager.acquire("blocker-lane", timeout=1.0))

        heartbeat_calls = []
        orig_heartbeat = manager.heartbeat

        def tracked_heartbeat(*args, **kwargs):
            heartbeat_calls.append((args, kwargs))
            return orig_heartbeat(*args, **kwargs)

        manager.heartbeat = tracked_heartbeat
        res = manager.acquire("waiter-hb-lane", timeout=0.15, poll_interval=0.02, heartbeat_interval=0.04)
        self.assertFalse(res)
        self.assertGreater(len(heartbeat_calls), 0, "Acquire waiting loop must write heartbeats")
        self.assertTrue(manager.release("blocker-lane"))

    def test_token_stored_in_info_json_and_disambiguation(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        token_1 = "tok-lane-owner-1"
        token_2 = "tok-lane-owner-2"
        same_name = "build-job"
        same_pid = os.getpid()

        # 1. Acquire with token_1
        self.assertTrue(manager.acquire(same_name, pid=same_pid, token=token_1, timeout=1.0))

        # Verify token is persisted in info.json
        info = manager._read_lock_info()
        self.assertIsNotNone(info)
        self.assertEqual(info.get("token"), token_1)
        self.assertEqual(info.get("owner"), same_name)
        self.assertEqual(info.get("pid"), same_pid)

        # Verify is_held_by checks token
        self.assertTrue(manager.is_held_by(same_name, pid=same_pid, token=token_1))
        self.assertFalse(manager.is_held_by(same_name, pid=same_pid, token=token_2))

        # Verify status exposes token
        st = manager.status()
        self.assertEqual(st["lock"]["token"], token_1)

        # 2. A distinct in-process lane with the same name and pid but different token
        # MUST NOT claim re-entrant ownership of the lock
        res_other = manager.acquire(same_name, pid=same_pid, token=token_2, timeout=0.05, poll_interval=0.02)
        self.assertFalse(res_other, "Different token under same name/PID must not claim lock ownership")

        # 3. Same token CAN re-enter cleanly
        res_reentrant = manager.acquire(same_name, pid=same_pid, token=token_1, timeout=0.1)
        self.assertTrue(res_reentrant, "Same token under same name/PID is re-entrant")

        self.assertTrue(manager.release(same_name))
        self.assertFalse(manager.is_held_by(same_name))

    def test_tokenless_reentrant_acquire(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        my_pid = os.getpid()

        # 1. Token-less acquire (like CLI)
        self.assertTrue(manager.acquire("my-lane", pid=my_pid, timeout=1.0))

        # 2. Second token-less acquire with same name & PID must succeed immediately ("already held")
        self.assertTrue(manager.acquire("my-lane", pid=my_pid, timeout=1.0))

        # 3. Explicit DIFFERENT token under same name & PID must NOT succeed as re-entrant
        res_diff_token = manager.acquire("my-lane", pid=my_pid, token="different-token", timeout=0.05, poll_interval=0.02)
        self.assertFalse(res_diff_token)

        self.assertTrue(manager.release("my-lane"))

    def test_poll_interval_validation(self):
        manager = BuildSlotManager(run_dir=self.run_dir, queue_stale_heartbeat_after=10.0)
        # Library: poll_interval >= queue_stale_heartbeat_after must raise ValueError
        with self.assertRaises(ValueError) as ctx:
            manager.acquire("test-lane", poll_interval=10.0)
        self.assertIn("must be less than queue_stale_heartbeat_after", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx2:
            manager.acquire("test-lane", poll_interval=15.0)
        self.assertIn("must be less than queue_stale_heartbeat_after", str(ctx2.exception))

        # CLI: --poll-interval >= DEFAULT_QUEUE_STALE_HEARTBEAT_SECONDS should exit with 2 (parser.error)
        script = os.path.abspath(build_slot.__file__)
        p = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "acquire", "test-lane", "--poll-interval", "65.0"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(p.returncode, 2)
        self.assertIn("must be less than queue_stale_heartbeat_after", p.stderr)

    def test_scaled_heartbeat_lapse_re_enqueue_and_fifo_preserved(self):
        scaled_threshold = 1.0  # 1.0s stands in for 60s
        manager = BuildSlotManager(
            run_dir=self.run_dir,
            is_pid_alive_fn=lambda p: True,
            queue_stale_heartbeat_after=scaled_threshold,
            max_slots=1,
        )

        # 1. Blocker holds the lock
        self.assertTrue(manager.acquire("blocker", timeout=1.0, poll_interval=0.05))

        token_a = "tok-waiter-a"
        token_b = "tok-waiter-b"

        events = []
        import threading

        def waiter_a_thread():
            # Waiter A starts acquire loop while blocker holds lock
            # heartbeat_interval is small (0.04s) so heartbeat fires quickly
            ok = manager.acquire(
                "waiter-a",
                pid=2001,
                token=token_a,
                timeout=3.0,
                poll_interval=0.02,
                heartbeat_interval=0.04,
                queue_stale_heartbeat_after=scaled_threshold,
            )
            if ok:
                events.append("waiter-a")
                manager.release("waiter-a")

        def waiter_b_thread():
            ok = manager.acquire(
                "waiter-b",
                pid=2002,
                token=token_b,
                timeout=3.0,
                poll_interval=0.02,
                heartbeat_interval=0.04,
                queue_stale_heartbeat_after=scaled_threshold,
            )
            if ok:
                events.append("waiter-b")
                manager.release("waiter-b")

        t_a = threading.Thread(target=waiter_a_thread)
        t_a.start()

        # Wait until Waiter A has enqueued and is actively looping in acquire()
        for _ in range(50):
            q = manager._read_queue()
            if any(x.get("token") == token_a for x in q):
                break
            time.sleep(0.01)
        self.assertTrue(any(x.get("token") == token_a for x in manager._read_queue()), "Waiter A must have enqueued")

        # Now simulate an external prune or lapse while Waiter A's acquire() is ALREADY LOOPING:
        # Wipe the queue directly while waiter-a is waiting
        manager._write_queue([])
        self.assertEqual(manager._read_queue(), [])

        # Waiter A is still looping in acquire(). On its next heartbeat iteration,
        # self.heartbeat(token=token_a) will return False because the entry was wiped.
        # acquire() MUST catch False and call self.enqueue(name, pid, token=token_a)!
        # Wait up to 1.0s for Waiter A's token to REAPPEAR in the queue:
        reappeared = False
        for _ in range(50):
            time.sleep(0.02)
            q = manager._read_queue()
            if any(x.get("token") == token_a for x in q):
                reappeared = True
                break

        # CRITICAL ASSERTION (defends against mutation that deletes self.enqueue on False heartbeat):
        self.assertTrue(reappeared, "Waiter A must re-enqueue its token when heartbeat() returns False while looping")

        # Now start Waiter B in second thread
        t_b = threading.Thread(target=waiter_b_thread)
        t_b.start()

        # Wait until Waiter B is in queue
        for _ in range(50):
            q = manager._read_queue()
            if any(x.get("token") == token_b for x in q):
                break
            time.sleep(0.01)

        # Confirm queue ordering: Waiter A is ahead of Waiter B
        tokens_in_queue = [x.get("token") for x in manager.clean_queue(stale_heartbeat_after=scaled_threshold)]
        self.assertEqual(tokens_in_queue, [token_a, token_b], "Waiter A must remain ahead of later arrival Waiter B")

        # Release blocker lock
        self.assertTrue(manager.release("blocker"))

        t_a.join()
        t_b.join()

        # Both acquired and released; Waiter A acquired FIRST!
        self.assertEqual(events, ["waiter-a", "waiter-b"], "Waiter A must acquire before later arrival Waiter B")

    def test_queue_atomic_lock_retries_on_permission_error(self):
        """
        On Windows, os.mkdir(queue_lock_dir) can transiently raise PermissionError [WinError 5]
        when another process is deleting the lock dir (delete-pending state).
        Verify that _queue_atomic_lock treats PermissionError as contention, retries with backoff,
        and acquires successfully instead of crashing.
        """
        real_mkdir = os.mkdir
        attempts = 0

        def fake_mkdir(path, *args, **kwargs):
            nonlocal attempts
            if os.path.basename(path) == build_slot.QUEUE_LOCK_NAME and attempts < 2:
                attempts += 1
                raise PermissionError(13, "Permission denied (simulated Windows delete-pending race)")
            return real_mkdir(path, *args, **kwargs)

        with mock.patch("os.mkdir", side_effect=fake_mkdir):
            acquired = False
            with build_slot._queue_atomic_lock(self.run_dir, timeout=2.0, retry_interval=0.01):
                acquired = True
            self.assertTrue(acquired)
            self.assertGreaterEqual(attempts, 2, "fake_mkdir should have raised PermissionError at least twice")

    def test_acquire_retries_on_queue_lock_permission_error(self):
        """
        Simulate PermissionError during BuildSlotManager.acquire() when accessing the queue lock.
        Verify that acquire() does not crash with a traceback and successfully acquires the build slot.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        real_mkdir = os.mkdir
        attempts = 0

        def fake_mkdir(path, *args, **kwargs):
            nonlocal attempts
            if os.path.basename(path) == build_slot.QUEUE_LOCK_NAME and attempts < 2:
                attempts += 1
                raise PermissionError(13, "Permission denied (simulated Windows delete-pending race)")
            return real_mkdir(path, *args, **kwargs)

        with mock.patch("os.mkdir", side_effect=fake_mkdir):
            acquired = manager.acquire(
                name="test-lane-perm",
                timeout=5.0,
                poll_interval=0.05,
            )
            self.assertTrue(acquired)
            self.assertGreaterEqual(attempts, 2)
        self.assertTrue(manager.release("test-lane-perm"))

    def test_queue_atomic_lock_permission_error_retry_success(self):
        """
        On Windows, a directory in delete-pending state raises PermissionError [WinError 5]
        on os.mkdir. The retry loop must treat PermissionError like contention,
        sleep retry_interval, retry, and successfully acquire the lock.
        """
        real_mkdir = os.mkdir
        call_count = 0

        def fake_mkdir(path, *args, **kwargs):
            nonlocal call_count
            if os.path.basename(path) == build_slot.QUEUE_LOCK_NAME:
                call_count += 1
                if call_count <= 2:
                    raise PermissionError(13, "Access is denied (delete-pending simulation)")
            return real_mkdir(path, *args, **kwargs)

        with mock.patch("os.mkdir", side_effect=fake_mkdir):
            acquired = False
            with _queue_atomic_lock(self.run_dir, timeout=2.0, retry_interval=0.01):
                acquired = True
                queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
                self.assertTrue(os.path.isdir(queue_lock_dir))

            self.assertTrue(acquired)
            self.assertGreaterEqual(call_count, 3)

    def test_queue_atomic_lock_permission_error_timeout_not_permission_error(self):
        """
        When os.mkdir always raises PermissionError, _queue_atomic_lock must
        honour the timeout and raise TimeoutError, NOT PermissionError.
        """
        real_mkdir = os.mkdir

        def fake_mkdir(path, *args, **kwargs):
            if os.path.basename(path) == build_slot.QUEUE_LOCK_NAME:
                raise PermissionError(13, "Access is denied (delete-pending simulation)")
            return real_mkdir(path, *args, **kwargs)

        with mock.patch("os.mkdir", side_effect=fake_mkdir):
            with self.assertRaises(TimeoutError) as ctx:
                with _queue_atomic_lock(self.run_dir, timeout=0.1, retry_interval=0.01):
                    pass
            self.assertIn("Timed out waiting for queue file lock", str(ctx.exception))

    def test_build_slot_acquire_permission_error_retry_success(self):
        """
        Main build-slot.lock mkdir retry loop: PermissionError on os.mkdir of
        build-slot.lock must be treated like contention and retried until acquired.
        """
        manager = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        real_mkdir = os.mkdir
        call_count = 0

        def fake_mkdir(path, *args, **kwargs):
            nonlocal call_count
            if os.path.basename(path) == build_slot.LOCK_DIR_NAME:
                call_count += 1
                if call_count <= 2:
                    raise PermissionError(13, "Access is denied (delete-pending simulation)")
            return real_mkdir(path, *args, **kwargs)

        with mock.patch("os.mkdir", side_effect=fake_mkdir):
            ok = manager.acquire("perm-lane", timeout=2.0, poll_interval=0.01)
            self.assertTrue(ok)
            self.assertTrue(manager.is_held_by("perm-lane"))
            self.assertGreaterEqual(call_count, 3)
            manager.release("perm-lane")

    def test_queue_atomic_lock_stale_dead_pid_reclaimed(self):
        """
        If build-slot-queue.lock exists with an info.json pointing to a dead PID,
        _queue_atomic_lock must reclaim the stale directory immediately and acquire.
        """
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir, exist_ok=True)
        dead_pid = 99999999
        self.assertFalse(is_pid_alive(dead_pid))
        info_path = os.path.join(queue_lock_dir, build_slot.INFO_FILE_NAME)
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump({
                "pid": dead_pid,
                "acquired_at": "2026-09-27T00:00:00Z",
                "acquired_at_epoch": time.time(),
            }, f)

        acquired = False
        with _queue_atomic_lock(self.run_dir, timeout=2.0, retry_interval=0.01):
            acquired = True
            info = build_slot._read_queue_lock_info(queue_lock_dir)
            self.assertIsNotNone(info)
            self.assertEqual(info["pid"], os.getpid())

        self.assertTrue(acquired)
        self.assertFalse(os.path.exists(queue_lock_dir))

    def test_queue_atomic_lock_stale_age_reclaimed(self):
        """
        If build-slot-queue.lock has a dead PID (or no info file) and is older than stale_after,
        _queue_atomic_lock reclaims it. A live PID lock is NEVER reclaimed on age.
        """
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir, exist_ok=True)
        info_path = os.path.join(queue_lock_dir, build_slot.INFO_FILE_NAME)
        old_epoch = time.time() - 3600  # 1 hour ago
        dead_pid = 99999999
        self.assertFalse(is_pid_alive(dead_pid))
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump({
                "pid": dead_pid,
                "acquired_at": "2026-09-27T00:00:00Z",
                "acquired_at_epoch": old_epoch,
            }, f)

        acquired = False
        with _queue_atomic_lock(self.run_dir, timeout=2.0, retry_interval=0.01, stale_after=5.0):
            acquired = True

        self.assertTrue(acquired)
        self.assertFalse(os.path.exists(queue_lock_dir))

        # A live PID lock younger than max_hold is not reclaimed on age
        os.makedirs(queue_lock_dir, exist_ok=True)
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump({
                "pid": os.getpid(),
                "acquired_at": "2026-09-27T00:00:00Z",
                "acquired_at_epoch": old_epoch,
            }, f)

        with self.assertRaises(TimeoutError):
            with _queue_atomic_lock(self.run_dir, timeout=0.1, retry_interval=0.01, stale_after=5.0, max_hold=7200.0):
                pass
        self.assertTrue(os.path.isdir(queue_lock_dir))
    def test_acquire_survives_heartbeat_timeout(self):
        """
        In acquire(), a TimeoutError from heartbeat() due to lock contention
        must log a warning and retry on the next tick, not abort acquisition.
        """
        manager = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        self.assertTrue(manager.acquire("blocker-lane", timeout=1.0))
        real_heartbeat = manager.heartbeat
        hb_calls = 0

        def flaky_heartbeat(*args, **kwargs):
            nonlocal hb_calls
            hb_calls += 1
            if hb_calls <= 2:
                raise TimeoutError("Queue lock contention during heartbeat")
            # After 2 flaky attempts, release the blocker so waiter can acquire
            manager.release("blocker-lane")
            return real_heartbeat(*args, **kwargs)

        with mock.patch.object(manager, "heartbeat", side_effect=flaky_heartbeat):
            ok = manager.acquire("hb-flaky-lane", timeout=3.0, poll_interval=0.01, heartbeat_interval=0.01)
            self.assertTrue(ok)
            self.assertTrue(manager.is_held_by("hb-flaky-lane"))
            self.assertGreaterEqual(hb_calls, 2)
            manager.release("hb-flaky-lane")

    def test_acquire_survives_clean_queue_timeout(self):
        """
        In acquire(), a TimeoutError from clean_queue() due to lock contention
        must log a warning, wait, and retry on the next tick, not abort acquisition.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        real_clean_queue = manager.clean_queue
        clean_calls = 0

        def flaky_clean_queue(*args, **kwargs):
            nonlocal clean_calls
            clean_calls += 1
            if clean_calls <= 2:
                raise TimeoutError("Queue lock contention during clean_queue")
            return real_clean_queue(*args, **kwargs)

        with mock.patch.object(manager, "clean_queue", side_effect=flaky_clean_queue):
            ok = manager.acquire("clean-flaky-lane", timeout=2.0, poll_interval=0.01)
            self.assertTrue(ok)
            self.assertTrue(manager.is_held_by("clean-flaky-lane"))
            self.assertGreaterEqual(clean_calls, 3)
            manager.release("clean-flaky-lane")

    def test_contending_lockers_concurrent_threads(self):
        """
        Simulate multiple threads contending for _queue_atomic_lock concurrently.
        All threads must acquire without data corruption, deadlocks, or unhandled errors.
        """
        counter = 0
        errors = []

        def worker(worker_id):
            nonlocal counter
            for _ in range(3):
                try:
                    with _queue_atomic_lock(self.run_dir, timeout=10.0, retry_interval=0.01):
                        current = counter
                        time.sleep(0.001)
                        counter = current + 1
                except Exception as e:
                    errors.append((worker_id, e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)

        self.assertEqual(errors, [])
        self.assertEqual(counter, 4 * 3)

    def test_dead_pid_slot_reclaimed_even_when_recent(self):
        """A dead PID expires immediately, including a newly acquired slot."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        os.makedirs(manager.lock_dir, exist_ok=True)
        dead_pid = 999999
        self.assertFalse(is_pid_alive(dead_pid))
        recent_epoch = time.time() - 0.5
        info = {
            "owner": "short-lived-cli-lane",
            "pid": dead_pid,
            "acquired_at": datetime.datetime.fromtimestamp(recent_epoch, datetime.timezone.utc).isoformat(),
            "acquired_at_epoch": recent_epoch,
        }
        with open(manager.info_file, "w", encoding="utf-8") as f:
            json.dump(info, f)

        reclaimed = manager.check_stale_and_reclaim()
        self.assertTrue(reclaimed)
        self.assertFalse(os.path.isdir(manager.lock_dir))

    def test_dead_pid_slot_reclaimed_even_with_fresh_heartbeat(self):
        """A fresh heartbeat does not preserve a dead owner's slot."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        os.makedirs(manager.lock_dir, exist_ok=True)
        dead_pid = 999999
        self.assertFalse(is_pid_alive(dead_pid))
        old_epoch = time.time() - 300.0
        now = time.time()
        info = {
            "owner": "heartbeating-lane",
            "pid": dead_pid,
            "acquired_at": datetime.datetime.fromtimestamp(old_epoch, datetime.timezone.utc).isoformat(),
            "acquired_at_epoch": old_epoch,
            "heartbeat_at": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
            "heartbeat_at_epoch": now,
        }
        with open(manager.info_file, "w", encoding="utf-8") as f:
            json.dump(info, f)

        reclaimed = manager.check_stale_and_reclaim()
        self.assertTrue(reclaimed)
        self.assertFalse(os.path.isdir(manager.lock_dir))


    def test_find_long_lived_owner_pid_returns_valid_pid(self):
        """
        find_long_lived_owner_pid must return a valid positive PID
        (either veyyon process or parent process).
        """
        pid = build_slot.find_long_lived_owner_pid()
        self.assertIsInstance(pid, int)
        self.assertGreater(pid, 0)
        self.assertTrue(is_pid_alive(pid))

    def test_enqueue_priority_is_first_come_first_served(self):
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: True)
        manager.enqueue("normal-1", 1001, token="tok-n1")

        # A priority entry passes normal waiters but queues behind earlier
        # priority entries: every lane passes --priority, so newest-first
        # would starve the oldest waiter.
        self.assertEqual(manager.enqueue("prio-1", 1011, token="tok-p1", priority=True), 0)
        self.assertEqual(manager.enqueue("prio-2", 1012, token="tok-p2", priority=True), 1)
        self.assertEqual(manager.enqueue("prio-3", 1013, token="tok-p3", priority=True), 2)
        self.assertEqual(manager.enqueue("normal-2", 1002, token="tok-n2"), 4)
        order = ["prio-1", "prio-2", "prio-3", "normal-1", "normal-2"]
        self.assertEqual([x["name"] for x in manager._read_queue()], order)

        # Re-enqueueing an entry that is already queued (the wait loop does
        # this when a heartbeat misses) refreshes it in place.
        self.assertEqual(manager.enqueue("prio-3", 1013, token="tok-p3", priority=True), 2)
        self.assertEqual(manager.enqueue("normal-2", 1002, token="tok-n2"), 4)
        self.assertEqual([x["name"] for x in manager._read_queue()], order)

        # Upgrading a normal entry joins the priority group at its own
        # enqueue time: behind every priority entry that queued earlier.
        self.assertEqual(manager.enqueue("normal-2", 1002, token="tok-n2", priority=True), 3)
        queue = manager._read_queue()
        self.assertEqual([x["name"] for x in queue], ["prio-1", "prio-2", "prio-3", "normal-2", "normal-1"])
        self.assertTrue(queue[3].get("priority"))

    def test_enqueue_priority_three_fifo(self):
        """Three priority enqueues A, B, C one after another -> queue order A, B, C (FIFO)."""
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: True)
        idx_a = manager.enqueue("A", 1001, token="tok-a", priority=True)
        idx_b = manager.enqueue("B", 1002, token="tok-b", priority=True)
        idx_c = manager.enqueue("C", 1003, token="tok-c", priority=True)
        self.assertEqual(idx_a, 0)
        self.assertEqual(idx_b, 1)
        self.assertEqual(idx_c, 2)
        queue = manager._read_queue()
        self.assertEqual([x["name"] for x in queue], ["A", "B", "C"])

    def test_enqueue_normal_then_priority_fifo(self):
        """Normal N, then priority P1, P2 -> order P1, P2, N."""
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: True)
        idx_n = manager.enqueue("N", 1000, token="tok-n", priority=False)
        self.assertEqual(idx_n, 0)
        idx_p1 = manager.enqueue("P1", 1001, token="tok-p1", priority=True)
        self.assertEqual(idx_p1, 0)
        idx_p2 = manager.enqueue("P2", 1002, token="tok-p2", priority=True)
        self.assertEqual(idx_p2, 1)
        queue = manager._read_queue()
        self.assertEqual([x["name"] for x in queue], ["P1", "P2", "N"])

    def test_enqueue_already_priority_re_enqueue_keeps_position(self):
        """Re-enqueue of the already-priority entry A (same name/pid) keeps its position."""
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: True)
        manager.enqueue("P1", 1001, token="tok-p1", priority=True)
        manager.enqueue("P2", 1002, token="tok-p2", priority=True)
        manager.enqueue("N", 1003, token="tok-n", priority=False)
        queue = manager._read_queue()
        self.assertEqual([x["name"] for x in queue], ["P1", "P2", "N"])

        # Re-enqueue P1 with priority=True -> keeps index 0
        idx_p1 = manager.enqueue("P1", 1001, token="tok-p1-new", priority=True)
        self.assertEqual(idx_p1, 0)
        # Re-enqueue P2 with priority=True -> keeps index 1
        idx_p2 = manager.enqueue("P2", 1002, token="tok-p2-new", priority=True)
        self.assertEqual(idx_p2, 1)
        queue2 = manager._read_queue()
        self.assertEqual([x["name"] for x in queue2], ["P1", "P2", "N"])

    def test_enqueue_normal_promoted_to_priority_behind_existing_priority(self):
        """Normal entry N1 re-enqueued with priority while P1 exists -> order P1, N1, ..."""
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: True)
        manager.enqueue("P1", 1001, token="tok-p1", priority=True)
        manager.enqueue("N1", 1002, token="tok-n1", priority=False)
        manager.enqueue("N2", 1003, token="tok-n2", priority=False)
        queue = manager._read_queue()
        self.assertEqual([x["name"] for x in queue], ["P1", "N1", "N2"])

        # N1 re-enqueued with priority=True -> moves behind P1, ahead of N2
        idx_n1 = manager.enqueue("N1", 1002, token="tok-n1", priority=True)
        self.assertEqual(idx_n1, 1)
        queue2 = manager._read_queue()
        self.assertEqual([x["name"] for x in queue2], ["P1", "N1", "N2"])

    def _write_timed_queue(self, manager, entries):
        now = time.time()
        manager._write_queue(
            [
                {"name": name, "pid": 3000 + i, "token": f"tok-{name}", "enqueued_at": enqueued_at,
                 "heartbeat_at": now, "priority": prio}
                for i, (name, enqueued_at, prio) in enumerate(entries)
            ]
        )

    def test_clean_queue_restores_fifo_order(self):
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: True)
        # A file written newest-first by an older build_slot.py; E's enqueue
        # time is unreadable, which must not break the sort for everyone.
        self._write_timed_queue(
            manager,
            [("D", 4.0, True), ("C", 3.0, False), ("E", "not-a-time", False), ("B", 2.0, True), ("A", 1.0, True)],
        )
        manager.clean_queue()
        self.assertEqual([x["name"] for x in manager._read_queue()], ["A", "B", "D", "E", "C"])

    def test_bump_keeps_fifo_among_priority(self):
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: True)
        self._write_timed_queue(
            manager, [("A", 1.0, True), ("B", 2.0, True), ("D", 4.0, True), ("C", 3.0, False)]
        )
        self.assertEqual(manager.enqueue("D", 3003, token="tok-D", priority=True), 2)

        # bump marks C priority; it passes no priority entry that queued before it
        self.assertTrue(manager.bump("C"))
        queue = manager._read_queue()
        self.assertEqual([x["name"] for x in queue], ["A", "B", "C", "D"])
        self.assertTrue(queue[2].get("priority"))

        # bump non-existent lane returns False
        self.assertFalse(manager.bump("lane-non-existent"))

    def test_prep_cache_and_unprep_cache(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        fake_worktree = os.path.join(self.run_dir, "fake-worktree")
        os.makedirs(os.path.join(fake_worktree, "frontend", ".next"), exist_ok=True)

        link_path = manager.prep_cache(fake_worktree)
        self.assertTrue(os.path.exists(link_path))

        # Write a dummy test file through the link
        test_file = os.path.join(link_path, "test_cache_entry.txt")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("cached compilation artifact")

        # Verify file exists in shared cache
        shared_cache = os.path.join(self.run_dir, "next-cache")
        shared_file = os.path.join(shared_cache, "test_cache_entry.txt")
        self.assertTrue(os.path.isfile(shared_file))

        # Unprep cache: removes junction without deleting shared cache contents
        removed = manager.unprep_cache(fake_worktree)
        self.assertTrue(removed)
        self.assertFalse(os.path.exists(link_path))
        # The file in shared cache MUST still exist!
        self.assertTrue(os.path.isfile(shared_file))

    def test_run_command_releases_lock(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        ret = manager.run_command(
            "run-lane",
            [sys.executable, "-c", "import sys; sys.exit(0)"],
        )
        self.assertEqual(ret, 0)
        # Lock must be released after command exits
        stat = manager.status()
        self.assertFalse(stat["lock"]["locked"])

    def test_run_options_after_name_are_honored(self):
        """Documented post-name options with `--` are parsed and honored (discussion_r4136963306)."""
        args = build_slot.parse_args(
            ["run", "lane", "--timeout", "1.5", "--cwd", "D:/wt", "--", "npx", "next", "build"]
        )
        self.assertEqual(args.timeout, 1.5)
        self.assertEqual(args.cwd, "D:/wt")
        self.assertFalse(args.priority)
        self.assertFalse(args.force)
        self.assertEqual(args.cmd, ["--", "npx", "next", "build"])

        args_force = build_slot.parse_args(["run", "lane", "--force", "--", "echo", "1"])
        self.assertTrue(args_force.force)
        self.assertEqual(args_force.cmd, ["--", "echo", "1"])

        args_hb = build_slot.parse_args(["run", "lane", "--heartbeat-stale-after", "42", "--", "echo", "1"])
        self.assertEqual(args_hb.heartbeat_stale_after, 42.0)

        args_eq = build_slot.parse_args(["run", "lane", "--timeout=10", "--", "echo", "1"])
        self.assertEqual(args_eq.timeout, 10.0)

        # Options before and after name both honored
        args_mixed = build_slot.parse_args(
            ["run", "--priority", "lane", "--timeout", "1.5", "--cwd", "D:/wt", "--", "npx", "next", "build"]
        )
        self.assertEqual(args_mixed.timeout, 1.5)
        self.assertEqual(args_mixed.cwd, "D:/wt")
        self.assertTrue(args_mixed.priority)
        self.assertFalse(args_mixed.force)
        self.assertEqual(args_mixed.cmd, ["--", "npx", "next", "build"])

        # Main cleanly runs with documented post-name options
        ret = build_slot.main(
            ["--run-dir", self.run_dir, "run", "lane", "--timeout", "10", "--", sys.executable, "-c", "import sys; sys.exit(0)"]
        )
        self.assertEqual(ret, 0)

    def test_run_options_after_name_without_separator_rejected_without_mutation(self):
        """Misplaced CLI options after lane name without `--` fail with exit code 2 and no state mutation."""
        misordered_cases = [
            ["run", "lane", "--timeout", "1.5", "--cwd", "D:/wt", "npx", "next", "build"],
            ["run", "lane", "--priority", "npx", "next", "build"],
            ["run", "lane", "--force", "echo", "1"],
            ["run", "lane", "--cwd", "D:/wt", "echo", "1"],
            ["run", "lane", "--heartbeat-stale-after", "42", "echo", "1"],
            ["run", "lane", "--timeout=10", "echo", "1"],
            ["run", "lane", "--run-dir", "/tmp", "echo", "1"],
        ]
        for cmd_args in misordered_cases:
            with self.subTest(cmd_args=cmd_args):
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                    build_slot.parse_args(cmd_args)
                self.assertEqual(cm.exception.code, 2)
                self.assertIn(
                    "options must precede the lane name: build_slot.py run [--cwd DIR] [--timeout S] [--priority] <name> -- <cmd>",
                    err.getvalue(),
                )

        # Confirm main() exits 2 and creates no queue entry or lock directory
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            build_slot.main(["--run-dir", self.run_dir, "run", "lane", "--priority", "echo", "1"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("options must precede the lane name", err.getvalue())
        # Assert no lock dir or queue file was created
        if os.path.exists(self.run_dir):
            entries = [e for e in os.listdir(self.run_dir) if e not in {"next-cache"}]
            self.assertEqual(entries, [])

    def test_misordered_global_options_rejected_without_mutation(self):
        """Misordered global options like `--run-dir` fail with exit code 2 and no state mutation."""
        misordered_global = [
            ["--run-dir", self.run_dir, "run", "--run-dir", "/tmp", "lane", "--", "echo", "1"],
            ["--run-dir", self.run_dir, "run", "lane", "--run-dir", "/tmp", "--", "echo", "1"],
            ["--run-dir", self.run_dir, "run", "lane", "--run-dir", "/tmp", "echo", "1"],
            ["--run-dir", self.run_dir, "acquire", "lane", "--run-dir", "/tmp"],
        ]
        for cmd_args in misordered_global:
            with self.subTest(cmd_args=cmd_args):
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                    build_slot.main(cmd_args)
                self.assertEqual(cm.exception.code, 2)
                if os.path.exists(self.run_dir):
                    entries = [e for e in os.listdir(self.run_dir) if e not in {"next-cache"}]
                    self.assertEqual(entries, [])

    def test_run_dir_named_run_not_mistaken_for_subcommand(self):
        """Option value equal to 'run' is not mistaken for subcommand (discussion_r4136963314)."""
        args_acq = build_slot.parse_args(["--run-dir", "run", "acquire", "--timeout", "1", "lane"])
        self.assertEqual(args_acq.run_dir, "run")
        self.assertEqual(args_acq.command, "acquire")
        self.assertEqual(args_acq.timeout, 1.0)
        self.assertEqual(args_acq.name, "lane")

        args_run_pre = build_slot.parse_args(
            ["--run-dir", "run", "run", "--timeout", "1", "lane", "--", "echo", "x"]
        )
        self.assertEqual(args_run_pre.run_dir, "run")
        self.assertEqual(args_run_pre.command, "run")
        self.assertEqual(args_run_pre.timeout, 1.0)
        self.assertEqual(args_run_pre.name, "lane")
        self.assertEqual(args_run_pre.cmd, ["echo", "x"])

        args_run_post = build_slot.parse_args(
            ["--run-dir", "run", "run", "lane", "--timeout", "1", "--", "echo", "x"]
        )
        self.assertEqual(args_run_post.run_dir, "run")
        self.assertEqual(args_run_post.command, "run")
        self.assertEqual(args_run_post.timeout, 1.0)
        self.assertEqual(args_run_post.name, "lane")
        self.assertEqual(args_run_post.cmd, ["--", "echo", "x"])

        args_run_eq = build_slot.parse_args(
            ["--run-dir=run", "run", "lane", "--timeout", "1", "--", "echo", "x"]
        )
        self.assertEqual(args_run_eq.run_dir, "run")
        self.assertEqual(args_run_eq.command, "run")
        self.assertEqual(args_run_eq.timeout, 1.0)
    def test_run_options_preceding_lane_name_are_honored(self):
        """Correctly ordered options before lane name parse cleanly."""
        args = build_slot.parse_args(
            ["run", "--priority", "--timeout", "1.5", "--cwd", "D:/wt", "lane", "--", "npx", "next", "build"]
        )
        self.assertEqual(args.timeout, 1.5)
        self.assertEqual(args.cwd, "D:/wt")
        self.assertTrue(args.priority)
        self.assertFalse(args.force)
        self.assertEqual(args.cmd, ["npx", "next", "build"])

        # A command without leading options keeps its own `--` arguments untouched
        args_plain = build_slot.parse_args(["run", "lane", "npm", "run", "build", "--", "--prod"])
        self.assertIsNone(args_plain.timeout)
        self.assertEqual(args_plain.cmd, ["npm", "run", "build", "--", "--prod"])

        # A command where `--` precedes flags passes them as the inner command
        args_flag = build_slot.parse_args(["run", "lane", "--", "--timeout", "5"])
        self.assertEqual(args_flag.cmd, ["--timeout", "5"])
    def test_cli_run_waits_under_high_ram_until_timeout(self):
        script = os.path.abspath(build_slot.__file__)
        env = dict(os.environ)
        env["BUILD_SLOT_RAM_PERCENT"] = "96.0"
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "run", "cli-ram-lane", "--timeout", "0.5",
             "--", sys.executable, "-c", "pass"],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 1)
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertIn("'cli-ram-lane' stays queued and waits", proc.stderr)
        self.assertIn("Timed out after 0.5s", proc.stderr)
        self.assertEqual(BuildSlotManager(run_dir=self.run_dir).clean_queue(), [])

    def test_check_ram(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        ok_high, _ = manager.check_ram(threshold=100.0)
        self.assertTrue(ok_high)
        ok_low, _ = manager.check_ram(threshold=0.0)
        self.assertFalse(ok_low)

    def test_help_exits_zero(self):
        """Negative control for argparse format strings like '90%' causing ValueError."""
        script = os.path.join(SCRIPT_DIR, "build_slot.py")
        proc = subprocess.run(
            [sys.executable, script, "--help"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.returncode, 0, f"--help failed with stderr: {proc.stderr}")
        self.assertIn("Check system RAM percentage against threshold", proc.stdout)


    def test_write_queue_retries_on_permission_error_and_succeeds(self):
        """
        _write_queue must retry when os.replace raises PermissionError (WinError 5 or 32)
        and succeed when the file handle is released.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        real_replace = os.replace
        replace_attempts = 0

        def flaky_replace(src, dst):
            nonlocal replace_attempts
            replace_attempts += 1
            if replace_attempts <= 2:
                err = PermissionError(13, "Access is denied")
                err.winerror = 5
                raise err
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=flaky_replace):
            manager._write_queue([{"name": "lane-1", "pid": 1234}])

        self.assertGreaterEqual(replace_attempts, 3)
        read_back = manager._read_queue()
        self.assertEqual(len(read_back), 1)
        self.assertEqual(read_back[0]["name"], "lane-1")

    def test_write_queue_cleans_up_temp_file_on_complete_failure(self):
        """
        _write_queue must clean up the temporary file when all retries fail.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)

        def failing_replace(src, dst):
            err = PermissionError(13, "Sharing violation")
            err.winerror = 32
            raise err

        with mock.patch("time.sleep", return_value=None):
            with mock.patch("os.replace", side_effect=failing_replace):
                with self.assertRaises(PermissionError):
                    manager._write_queue([{"name": "lane-fail", "pid": 1234}])

        # Verify no temporary files remain in run_dir
        tmp_files = [f for f in os.listdir(self.run_dir) if f.startswith("queue-") and f.endswith(".tmp")]
        self.assertEqual(tmp_files, [])

    def test_heartbeat_survives_queue_write_error(self):
        """
        A failed write during heartbeat must log a warning and return False,
        never raising an unhandled exception.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        manager.enqueue("lane-hb-write", 5555)

        with mock.patch.object(manager, "_write_queue", side_effect=PermissionError(13, "Access is denied")):
            ok = manager.heartbeat(name="lane-hb-write", pid=5555)
            self.assertFalse(ok)

    def test_clean_queue_survives_queue_write_error(self):
        """
        clean_queue must log a warning and return the cleaned queue if _write_queue fails,
        without raising out.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        manager.enqueue("lane-stale", 999999, stale_heartbeat_after=0.01)
        time.sleep(0.02)

        with mock.patch.object(manager, "_write_queue", side_effect=PermissionError(13, "Access is denied")):
            cleaned = manager.clean_queue(stale_heartbeat_after=0.01)
            self.assertIsInstance(cleaned, list)

    def _exited_pid(self) -> int:
        """PID of a real process that has already exited (faithful 'dead' for is_pid_alive)."""
        p = subprocess.Popen([sys.executable, "-c", "pass"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        p.wait()
        self.assertFalse(is_pid_alive(p.pid))
        return p.pid

    def _live_process(self) -> subprocess.Popen:
        """A real process that stays alive for the test; killed on cleanup."""
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.assertTrue(is_pid_alive(p.pid))
        return p

    def _write_run_lock(self, manager, wrapper_pid, child_pid, age, hb_age, wrapper_created_ticks=None):
        now = time.time()
        os.mkdir(manager.lock_dir)
        info = {
            "owner": "qa-lane-run",
            "pid": wrapper_pid,
            "wrapper_pid": wrapper_pid,
            "child_pid": child_pid,
            "token": "run-tok",
            "acquired_at_epoch": now - age,
            "heartbeat_at_epoch": now - hb_age,
        }
        if wrapper_created_ticks is not False:
            if wrapper_created_ticks is None and is_pid_alive(wrapper_pid):
                wrapper_created_ticks = build_slot._get_process_create_ticks(wrapper_pid)
            if wrapper_created_ticks is not None:
                info["wrapper_created_ticks"] = wrapper_created_ticks
        with open(manager.info_file, "w", encoding="utf-8") as f:
            json.dump(info, f)

    def test_live_run_wrapper_with_dead_child_and_stalled_heartbeat_not_reclaimed(self):
        """
        #315: the `run` wrapper waits for its command and releases in `finally`, so while
        it is alive the build is alive. A dead wrapped command (cmd.exe or launcher shim)
        and a heartbeat stalled past 90s by host memory pressure must not free the slot.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        wrapper = self._live_process()
        self._write_run_lock(manager, wrapper.pid, self._exited_pid(), age=600.0, hb_age=240.0)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            reclaimed = manager.check_stale_and_reclaim()
        self.assertFalse(reclaimed, stderr.getvalue())
        self.assertTrue(os.path.isdir(manager.lock_dir))

    def test_live_run_wrapper_past_30_min_with_fresh_heartbeat_not_reclaimed(self):
        """
        #315: QA5748 ran 30m30s under `run`, and by release time its slot belonged to another
        lane. A live wrapper that still heartbeats keeps the slot however long the build takes.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        wrapper = self._live_process()
        self._write_run_lock(manager, wrapper.pid, wrapper.pid, age=31 * 60.0, hb_age=5.0)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertFalse(manager.check_stale_and_reclaim(), stderr.getvalue())
            self.assertFalse(manager.check_stale_and_reclaim(pid_dead_grace_period=1.0), stderr.getvalue())
        self.assertTrue(os.path.isdir(manager.lock_dir))

    def _write_acquire_lock(self, manager, owner_pid, age, hb_age):
        """An `acquire`-mode lock: no wrapper_pid, owner is the lane's long-lived host PID."""
        now = time.time()
        os.mkdir(manager.lock_dir)
        info = {
            "owner": "abandoned-lane",
            "pid": owner_pid,
            "token": "acq-tok",
            "acquired_at_epoch": now - age,
            "heartbeat_at_epoch": now - hb_age,
        }
        with open(manager.info_file, "w", encoding="utf-8") as f:
            json.dump(info, f)

    def test_live_acquire_holder_expired_lease_reclaimed(self):
        """An acquire holder silent for over 30 minutes loses its lease."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        host = self._live_process()
        self._write_acquire_lock(manager, host.pid, age=134 * 60.0, hb_age=134 * 60.0)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertTrue(manager.check_stale_and_reclaim(), stderr.getvalue())
        self.assertFalse(os.path.isdir(manager.lock_dir))

    def test_acquire_holder_with_live_pid_silent_under_30_min_not_reclaimed(self):
        """#620 negative control: a live `acquire` holder below the 30 min limit keeps its slot."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        host = self._live_process()
        self._write_acquire_lock(manager, host.pid, age=29 * 60.0, hb_age=29 * 60.0)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertFalse(manager.check_stale_and_reclaim(), stderr.getvalue())
        self.assertTrue(os.path.isdir(manager.lock_dir))

    def test_acquire_holder_with_live_pid_and_fresh_heartbeat_not_reclaimed(self):
        """#620 negative control: a holder that keeps heartbeating keeps its slot at any age."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        host = self._live_process()
        self._write_acquire_lock(manager, host.pid, age=3 * 3600.0, hb_age=5.0)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertFalse(manager.check_stale_and_reclaim(), stderr.getvalue())
        self.assertTrue(os.path.isdir(manager.lock_dir))

    def test_acquire_holder_with_dead_pid_reclaimed_after_grace(self):
        """#620 repro as filed: a dead owner PID past the grace period is reclaimed on the next check."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self._write_acquire_lock(manager, self._exited_pid(), age=120.0, hb_age=120.0)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertTrue(manager.check_stale_and_reclaim(), stderr.getvalue())
        self.assertFalse(os.path.isdir(manager.lock_dir))

    def test_cli_heartbeat_keeps_long_acquire_holder_alive(self):
        """#620: `heartbeat <name>` refreshes the slot, so a build past 30 min is not reclaimed."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        host = self._live_process()
        self._write_acquire_lock(manager, host.pid, age=3 * 3600.0, hb_age=3 * 3600.0)
        with open(manager.info_file, encoding="utf-8") as f:
            owner = json.load(f)["owner"]

        script = os.path.abspath(build_slot.__file__)
        proc = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "heartbeat", owner],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with redirect_stderr(io.StringIO()):
            self.assertFalse(manager.check_stale_and_reclaim())
        self.assertTrue(os.path.isdir(manager.lock_dir))

        missing = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "heartbeat", "nobody"],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(missing.returncode, 1)

    def test_live_run_wrapper_with_stale_heartbeat_is_never_reclaimed(self):
        """#690: a live wrapper keeps its slot however old its heartbeat; a failed heartbeat must not grant it twice."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        wrapper = self._live_process()
        self._write_run_lock(manager, wrapper.pid, wrapper.pid, age=7200.0, hb_age=7000.0)

        with redirect_stderr(io.StringIO()):
            self.assertFalse(manager.check_stale_and_reclaim())
            self.assertFalse(manager.check_stale_and_reclaim(heartbeat_stale_after=1.0))
        self.assertTrue(os.path.isdir(manager.lock_dir))

    def test_live_owner_with_failing_heartbeat_is_not_granted_twice(self):
        """#690: every heartbeat write fails for the whole run; a second lane must not get the slot."""
        owner_mgr = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        wrapper = self._live_process()
        self._write_run_lock(owner_mgr, wrapper.pid, wrapper.pid, age=3600.0, hb_age=3000.0)
        with open(owner_mgr.info_file, encoding="utf-8") as f:
            owner = json.load(f)["owner"]

        def always_denied(src, dst):
            err = PermissionError(13, "Access is denied")
            err.winerror = 5
            raise err

        with mock.patch("time.sleep", return_value=None), mock.patch("os.replace", side_effect=always_denied):
            with redirect_stderr(io.StringIO()):
                self.assertFalse(owner_mgr.heartbeat_lock(owner))
        self.assertEqual([f for f in os.listdir(owner_mgr.lock_dir) if f.endswith(".tmp")], [])

        waiter = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        with redirect_stderr(io.StringIO()):
            self.assertFalse(waiter.acquire("second-lane", timeout=0.3, poll_interval=0.05, force=True))
        with open(owner_mgr.info_file, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["owner"], owner)

    def test_heartbeat_survives_foreign_handle_on_info_json(self):
        """#690: a process holding info.json open blocks os.replace on Windows; the heartbeat still lands."""
        manager = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        wrapper = self._live_process()
        self._write_run_lock(manager, wrapper.pid, wrapper.pid, age=600.0, hb_age=500.0)
        with open(manager.info_file, encoding="utf-8") as f:
            owner = json.load(f)["owner"]

        real_replace = os.replace

        def replace_denied_for_info(src, dst):
            if os.path.basename(dst) == build_slot.INFO_FILE_NAME:
                err = PermissionError(13, "Access is denied")
                err.winerror = 5
                raise err
            return real_replace(src, dst)

        holder = open(manager.info_file, "rb")  # the foreign reader that never closes
        try:
            with mock.patch("time.sleep", return_value=None), mock.patch("os.replace", side_effect=replace_denied_for_info):
                with redirect_stderr(io.StringIO()):
                    self.assertTrue(manager.heartbeat_lock(owner))
            info = build_slot._read_lock_dir_info(manager.lock_dir, 0)
            self.assertLess(time.time() - info["heartbeat_at_epoch"], 5.0)
        finally:
            holder.close()

    def test_info_reader_does_not_block_replace(self):
        """#690: our own reader closes at once and never blocks a replace of the file."""
        path = os.path.join(self.run_dir, "probe.json")
        build_slot._write_json_atomic(path, {"n": 1})
        self.assertEqual(build_slot._read_json_file(path), {"n": 1})
        build_slot._write_json_atomic(path, {"n": 2})
        self.assertEqual(build_slot._read_json_file(path), {"n": 2})

    def test_dead_run_wrapper_retains_reservation_until_child_dies(self):
        """Never free a crashed wrapper's reservation while its recorded child lives."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        dead_wrapper = self._exited_pid()
        orphan_child = self._live_process()

        self._write_run_lock(manager, dead_wrapper, orphan_child.pid, age=30.0, hb_age=30.0)
        self.assertFalse(manager.check_stale_and_reclaim())
        self.assertTrue(os.path.isdir(manager.lock_dir))
        orphan_child.kill()
        orphan_child.wait(timeout=5)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertTrue(manager.check_stale_and_reclaim())
        self.assertIn(f"owner PID {dead_wrapper} is dead", stderr.getvalue())

    def test_run_command_records_wrapper_as_lock_pid(self):
        """
        While the command runs, the lock names the wrapper (this process) as pid and
        wrapper_pid, keeps the command's PID only as child_pid, and is released after.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        snapshot = os.path.join(self.run_dir, "info-snapshot.json")
        # A script file, not `-c`: run_command uses shell=True on Windows, where cmd.exe
        # cuts a multi-line argument at the first newline.
        reader = os.path.join(self.run_dir, "snapshot_lock_info.py")
        with open(reader, "w", encoding="utf-8") as f:
            f.write(
                "import json, sys, time\n"
                "src, dst = sys.argv[1], sys.argv[2]\n"
                "for _ in range(100):\n"
                "    try:\n"
                "        info = json.load(open(src, encoding='utf-8'))\n"
                "    except Exception:\n"
                "        info = {}\n"
                "    if info.get('child_pid'):\n"
                "        break\n"
                "    time.sleep(0.05)\n"
                "json.dump(info, open(dst, 'w', encoding='utf-8'))\n"
            )
        ret = manager.run_command(
            "child-test-lane",
            [sys.executable, reader, manager.info_file, snapshot],
        )
        self.assertEqual(ret, 0)
        with open(snapshot, encoding="utf-8") as f:
            info = json.load(f)
        self.assertEqual(info["pid"], os.getpid())
        self.assertEqual(info["wrapper_pid"], os.getpid())
        self.assertNotEqual(info["child_pid"], os.getpid())
        self.assertEqual(info["token"], info["run_token"])
        self.assertFalse(manager.status()["lock"]["locked"])

    def test_acquire_owner_pid_outlives_the_acquire_process(self):
        """
        #315: `acquire` exits once it holds the slot. The owner PID it records must be a
        process that outlives it, or the dead-PID rule frees the slot 60s into the build.
        """
        probe = subprocess.run(
            [sys.executable, "-c",
             "import os, sys; sys.path.insert(0, sys.argv[1]); import build_slot; "
             "print(os.getpid(), build_slot.find_long_lived_owner_pid())",
             SCRIPT_DIR],
            stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        probe_pid, owner_pid = (int(x) for x in probe.stdout.split())
        self.assertNotEqual(owner_pid, probe_pid)
        self.assertTrue(is_pid_alive(owner_pid))

    def test_acquire_owner_is_veyyon_host_not_per_command_shell(self):
        """
        #315: under veyyon each bash-tool call runs in a shell that exits with the command.
        Recording that shell (the child just below the host) let the dead-PID rule free an
        acquire-mode slot 60s into the build; the host itself is the owner.
        """
        parents = {100: 4, 4: 0, 200: 100, 300: 200, 400: 300}
        names = {100: "veyyon.exe", 4: "explorer.exe", 200: "bash.exe", 300: "sh.exe", 400: "python.exe"}
        self.assertEqual(build_slot._owner_pid_from_process_table(400, parents, names), 100)

    def test_acquire_owner_without_veyyon_is_farthest_ancestor(self):
        """Outside veyyon the farthest ancestor in the table owns it; a parent-PID cycle ends the climb."""
        parents = {10: 999, 20: 10, 30: 20}
        names = {10: "code.exe", 20: "pwsh.exe", 30: "python.exe"}
        self.assertEqual(build_slot._owner_pid_from_process_table(30, parents, names), 10)
        self.assertEqual(build_slot._owner_pid_from_process_table(5, {5: 6, 6: 5}, {}), 6)

    def test_acquire_owner_skips_veyyon_helper_workers(self):
        """
        #315: `launch` runs commands under `veyyon.exe __veyyon_worker_daemon_broker` and JS
        eval under `__veyyon_worker_js_eval_process`. Those helpers are veyyon.exe too, but
        their lifetime is not the lane's; the session host above them owns the slot.
        """
        parents = {100: 4, 200: 100, 300: 200}
        names = {100: "veyyon.exe", 4: "powershell.exe", 200: "veyyon.exe", 300: "python.exe"}
        exe = r"C:\Users\u\AppData\Local\veyyon\veyyon.exe"
        for helper in ("__veyyon_worker_daemon_broker", "__veyyon_worker_js_eval_process"):
            cmdlines = {100: f'"{exe}"', 200: f"{exe} {helper}", 300: "python build_slot.py acquire x"}
            with self.subTest(helper=helper):
                self.assertEqual(build_slot._owner_pid_from_process_table(300, parents, names, cmdlines), 100)

    def test_acquire_owner_is_veyyon_host_on_node_runtime(self):
        """
        #315: a veyyon host running on node.exe/bun.exe has no "veyyon" in its exe name; its
        entrypoint on the command line identifies it. A node MCP wrapper under the `.veyyon`
        config dir is not a host.
        """
        parents = {4: 1, 10: 4, 20: 10, 30: 20, 40: 30}
        names = {4: "explorer.exe", 10: "pwsh.exe", 20: "node.exe", 30: "bash.exe", 40: "python.exe"}
        host = {20: r'"C:\Program Files\nodejs\node.exe" C:\src\veyyon\packages\coding-agent\dist\cli.js'}
        self.assertEqual(build_slot._owner_pid_from_process_table(40, parents, names, host), 20)
        wrapper = {20: r'node.exe C:/Users/u/.veyyon/profiles/default/agent/github-mcp-wrapper.js'}
        self.assertEqual(build_slot._owner_pid_from_process_table(40, parents, names, wrapper), 4)

    def test_acquire_owner_climb_stops_at_reused_parent_pid(self):
        """
        #315: the lane's parent died and Windows gave its PID to a newer, unrelated process.
        Climbing into that impostor recorded a PID that died seconds later, and the dead-PID
        rule reclaimed the live build. A parent created after its child ends the climb.
        """
        # 400 python <- 300 sh <- 200 (dead bash's PID, now reused by a later cmd.exe) <- 7 conhost
        parents = {400: 300, 300: 200, 200: 7, 7: 4, 4: 1}
        names = {400: "python.exe", 300: "sh.exe", 200: "cmd.exe", 7: "conhost.exe", 4: "explorer.exe"}
        created = {4: 10, 7: 20, 300: 50, 400: 60, 200: 90}
        self.assertEqual(build_slot._owner_pid_from_process_table(400, parents, names, {}, created), 300)
        # Without the reuse (parent older than child) the climb goes on as before.
        created[200] = 40
        self.assertEqual(build_slot._owner_pid_from_process_table(400, parents, names, {}, created), 4)

    def _seed_stale_lock(self, manager, token="stale-token"):
        os.makedirs(manager.lock_dir, exist_ok=True)
        past_epoch = time.time() - 300.0
        with open(manager.info_file, "w", encoding="utf-8") as f:
            json.dump({
                "owner": "dead-lane",
                "pid": 999999,
                "token": token,
                "acquired_at": datetime.datetime.fromtimestamp(past_epoch, datetime.timezone.utc).isoformat(),
                "acquired_at_epoch": past_epoch,
            }, f)
        self.assertFalse(is_pid_alive(999999))

    def test_second_reclaimer_never_removes_the_next_holders_lock(self):
        """
        #315: waiters A and B both judge the same dead lock stale. A reclaims it and lane C
        acquires the free slot before B acts. B must not delete C's live lock on the strength
        of its old read; C's build would run with the slot open to a second build.
        """
        reclaimer_a = BuildSlotManager(run_dir=self.run_dir)
        reclaimer_b = BuildSlotManager(run_dir=self.run_dir)
        lane_c = BuildSlotManager(run_dir=self.run_dir)
        self._seed_stale_lock(reclaimer_a)
        stale_snapshot = reclaimer_b._read_slot_info(0)
        real_read = reclaimer_b._read_slot_info
        reads = []

        def read_then_lose_the_race(slot_idx=0):
            reads.append(slot_idx)
            if len(reads) == 1:
                self.assertTrue(reclaimer_a.check_stale_and_reclaim())
                self.assertTrue(lane_c.acquire("lane-c", timeout=2.0, poll_interval=0.05))
                return stale_snapshot
            return real_read(slot_idx)

        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()), \
                mock.patch.object(reclaimer_b, "_read_slot_info", side_effect=read_then_lose_the_race):
            self.assertFalse(reclaimer_b.check_stale_and_reclaim())

        held = lane_c._read_slot_info(0)
        self.assertIsNotNone(held, "B deleted lane C's live lock")
        self.assertEqual(held["owner"], "lane-c")
        self.assertEqual(err.getvalue().count("[NOTICE] Reclaiming stale build slot lock"), 1)
        self.assertEqual([n for n in os.listdir(self.run_dir) if ".tombstone-" in n], [])
        self.assertTrue(lane_c.release("lane-c"))

    def test_reclaimer_restores_a_lock_that_changed_hands_before_its_rename(self):
        """
        #315: the handover can land after B's last re-read and before its rename, so B
        renames lane C's lock. B must see the new lock in its tombstone and put it back, both
        for a token lock and for a corrupt one whose successor is still being created.
        """
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt):
                reclaimer_a = BuildSlotManager(run_dir=self.run_dir)
                reclaimer_b = BuildSlotManager(run_dir=self.run_dir)
                self._seed_stale_lock(reclaimer_a)
                if corrupt:
                    os.remove(reclaimer_a.info_file)
                    old = time.time() - 300
                    os.utime(reclaimer_a.lock_dir, (old, old))
                stale_snapshot = reclaimer_b._read_slot_info(0)
                handed_over = []

                def stale_view(slot_idx=0):
                    if not handed_over:
                        handed_over.append(True)
                        self.assertTrue(reclaimer_a.check_stale_and_reclaim())
                        if corrupt:
                            os.makedirs(reclaimer_a.lock_dir)  # C mid-creation: no info.json yet
                        else:
                            self.assertTrue(reclaimer_a.acquire("lane-c", timeout=2.0, poll_interval=0.05))
                    return stale_snapshot

                with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()), \
                        mock.patch.object(reclaimer_b, "_read_slot_info", side_effect=stale_view):
                    self.assertFalse(reclaimer_b.check_stale_and_reclaim())

                self.assertTrue(os.path.isdir(reclaimer_a.lock_dir), "B deleted lane C's lock")
                if not corrupt:
                    self.assertEqual(reclaimer_a._read_slot_info(0)["owner"], "lane-c")
                self.assertEqual([n for n in os.listdir(self.run_dir) if ".tombstone-" in n], [])
                shutil.rmtree(reclaimer_a.lock_dir)

    def test_reclaim_whose_tombstone_was_carried_off_stays_quiet(self):
        """
        #315 review B2: on Windows two reclaimers' renames of one lock dir can both succeed,
        the later carrying the dir out of the earlier's tombstone. The earlier one then finds
        its tombstone gone. That is not a live lock lost; it must not log [ERROR] or delete.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        self._seed_stale_lock(manager)
        other_tombstone = manager.lock_dir + ".tombstone-other"
        real_rename = os.rename

        def rename_then_lose_it(src, dst):
            real_rename(src, dst)
            if src == manager.lock_dir:
                real_rename(dst, other_tombstone)

        err = io.StringIO()
        with redirect_stderr(err), mock.patch.object(build_slot.os, "rename", side_effect=rename_then_lose_it):
            self.assertFalse(manager.check_stale_and_reclaim())
        self.assertNotIn("[ERROR]", err.getvalue())
        self.assertNotIn("[NOTICE] Reclaiming", err.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(other_tombstone, "info.json")))
        shutil.rmtree(other_tombstone)

    def test_reclaim_restore_after_tombstone_carried_off_stays_quiet(self):
        """
        #315 review B2 (delta): the carry-off can also land after the tombstone read shows a
        different lock and before the restore rename. The restore then finds no tombstone;
        that is not a stranded live lock and must not log [ERROR].
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        self._seed_stale_lock(manager)
        other_tombstone = manager.lock_dir + ".tombstone-other"
        real_read = build_slot._read_lock_dir_info

        def read_then_lose_it(lock_dir, slot_idx, *args, **kwargs):
            info = real_read(lock_dir, slot_idx, *args, **kwargs)
            if ".tombstone-" in lock_dir and lock_dir != other_tombstone:
                os.rename(lock_dir, other_tombstone)
                return dict(info, token="next-holder")
            return info

        err = io.StringIO()
        with redirect_stderr(err), mock.patch.object(build_slot, "_read_lock_dir_info", side_effect=read_then_lose_it):
            self.assertFalse(manager.check_stale_and_reclaim())
        self.assertNotIn("[ERROR]", err.getvalue())
        self.assertTrue(os.path.isfile(os.path.join(other_tombstone, "info.json")))
        shutil.rmtree(other_tombstone)

    def test_stale_lock_reclaimed_once_without_tombstone_leftovers(self):
        """#315: the tombstone reclaim still frees a dead lock, and only one reclaimer reports it."""
        first = BuildSlotManager(run_dir=self.run_dir)
        second = BuildSlotManager(run_dir=self.run_dir)
        self._seed_stale_lock(first)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertTrue(first.check_stale_and_reclaim())
            self.assertFalse(second.check_stale_and_reclaim())
        self.assertFalse(os.path.exists(first.lock_dir))
        self.assertEqual([n for n in os.listdir(self.run_dir) if ".tombstone-" in n], [])
        self.assertEqual(err.getvalue().count("[NOTICE] Reclaiming stale build slot lock"), 1)

    def test_heartbeat_write_failure_logs_distinct_line(self):
        """
        #315: a failed heartbeat write was swallowed; after heartbeat_stale_after the holder is
        reclaimed as hung with nothing saying why. The failure gets its own line.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        with redirect_stdout(io.StringIO()):
            self.assertTrue(manager.acquire("hb-lane", timeout=2.0, poll_interval=0.05, token="hb-token"))
        err = io.StringIO()
        with redirect_stderr(err), self.assertLogs("build_slot", "WARNING") as logs, \
                mock.patch.object(build_slot, "_write_json_atomic", side_effect=OSError("disk full")):
            self.assertFalse(manager.heartbeat_lock("hb-lane", token="hb-token"))
            self.assertFalse(manager._record_run_child("hb-lane", os.getpid(), os.getpid(), token="hb-token"))
        line = "[HEARTBEAT] Failed to write heartbeat for 'hb-lane' (slot 0): disk full"
        self.assertEqual(err.getvalue().count(line), 2)
        self.assertEqual(sum(line in m for m in logs.output), 2)
        with redirect_stdout(io.StringIO()):
            self.assertTrue(manager.release("hb-lane", token="hb-token"))

    def test_run_cli_accepts_heartbeat_stale_after(self):
        """#315: `run` waits for the slot like `acquire`, so it takes the same hung-holder threshold."""
        with mock.patch.object(BuildSlotManager, "run_command", return_value=0) as run_command:
            ret = build_slot.main(
                ["--run-dir", self.run_dir, "run", "cli-lane", "--heartbeat-stale-after", "42", "--", "echo", "x"]
            )
        self.assertEqual(ret, 0)
        self.assertEqual(run_command.call_args.kwargs["heartbeat_stale_after"], 42.0)
        self.assertEqual(run_command.call_args.kwargs["cmd"], ["echo", "x"])
    def test_heartbeat_rewrite_never_reads_as_corrupt(self):
        """
        #315: waiters check the lock while its holder heartbeats. An in-place rewrite of
        info.json let a waiter read the empty file, call the live lock corrupt and reclaim
        it (QA5748 lost its slot this way while its driver was running).
        """
        manager = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        os.environ["BUILD_SLOT_ALLOW_FORCE"] = "1"
        self.assertTrue(manager.acquire("live-holder", timeout=1.0, force=True, token="tok"))
        # Past the 10s mid-creation grace, as any build that heartbeats is.
        old = time.time() - 120
        os.utime(manager.slot_dirs[0], (old, old))
        stop = threading.Event()

        def heartbeat():
            while not stop.is_set():
                manager.heartbeat_lock("live-holder", token="tok")

        holder = threading.Thread(target=heartbeat)
        holder.start()
        try:
            deadline = time.time() + 3.0
            with redirect_stderr(io.StringIO()):
                while time.time() < deadline:
                    self.assertFalse(manager.check_stale_and_reclaim(), "live lock reclaimed as corrupt")
        finally:
            stop.set()
            holder.join()
        self.assertEqual(manager.status()["lock"]["owner"], "live-holder")
        self.assertEqual(
            [n for n in os.listdir(manager.slot_dirs[0]) if n.endswith(".tmp")], [],
        )

    def test_up_to_eight_concurrent_holders(self):
        """Up to 8 concurrent build slots can be held; the 9th is blocked until a slot frees."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        lanes = [f"lane-{i}" for i in range(8)]
        for lane in lanes:
            self.assertTrue(manager.acquire(lane, timeout=1.0, poll_interval=0.02))
            self.assertTrue(manager.is_held_by(lane))

        st = manager.status()
        self.assertEqual(st["max_slots"], 8)
        self.assertEqual(st["active_slots"], 8)
        self.assertEqual(set(st["holders"]), set(lanes))

        # 9th lane is blocked
        res_9 = manager.acquire("lane-8", timeout=0.05, poll_interval=0.02)
        self.assertFalse(res_9)

        # Release first lane, then lane-8 can acquire
        self.assertTrue(manager.release("lane-0"))
        self.assertTrue(manager.acquire("lane-8", timeout=1.0, poll_interval=0.02))

        # Release remaining
        for lane in lanes[1:]:
            self.assertTrue(manager.release(lane))
        self.assertTrue(manager.release("lane-8"))
        self.assertEqual(manager.status()["active_slots"], 0)

    def test_status_shows_holders_and_capacity(self):
        """status() and format_status_human() format and report 8 slots and 95% guard."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(manager.acquire("slot-holder-0", timeout=1.0))
        self.assertTrue(manager.acquire("slot-holder-1", timeout=1.0))

        st = manager.status()
        self.assertEqual(st["max_slots"], 8)
        self.assertEqual(st["active_slots"], 2)
        self.assertEqual(set(st["holders"]), {"slot-holder-0", "slot-holder-1"})

        human = build_slot.format_status_human(st)
        self.assertIn("Capacity:    8 slot(s) allowed (RAM guard: 95%)", human)
        self.assertIn("LOCKED (2/8 in use)", human)
        self.assertIn("slot-holder-0", human)
        self.assertIn("slot-holder-1", human)

        manager.release("slot-holder-0")
        manager.release("slot-holder-1")
    def test_queue_deduplication_by_name_and_pid(self):
        """Duplicate queue entries with the same (name, pid) are deduplicated."""
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: True)
        test_pid = 44556

        # Enqueue same lane and pid twice with different tokens
        manager.enqueue("ProfileLowerFlash", pid=test_pid, token="tok-1")
        manager.enqueue("ProfileLowerFlash", pid=test_pid, token="tok-2")
        # Enqueue another lane with same pid
        manager.enqueue("OtherLane", pid=test_pid, token="tok-3")
        # Enqueue same lane with different pid
        manager.enqueue("ProfileLowerFlash", pid=99999, token="tok-4")

        q = manager.clean_queue()
        plf_entries = [item for item in q if item.get("name") == "ProfileLowerFlash" and item.get("pid") == test_pid]
        self.assertEqual(len(plf_entries), 1, "Duplicate (name, pid) must be deduplicated to 1 entry")
        self.assertEqual(plf_entries[0]["token"], "tok-2", "Latest enqueue updates/binds token")

        other_entries = [item for item in q if item.get("name") == "OtherLane"]
        self.assertEqual(len(other_entries), 1)
        diff_pid_entries = [item for item in q if item.get("name") == "ProfileLowerFlash" and item.get("pid") == 99999]
        self.assertEqual(len(diff_pid_entries), 1)

    def test_acquisition_stagger_delays_second_acquisition(self):
        """Heavy acquisition stagger delays the next heavy start after release."""
        manager = BuildSlotManager(run_dir=self.run_dir, acquisition_stagger=0.2)
        # Lane 1 acquires at t=0
        self.assertTrue(manager.acquire("stagger-lane-1", timeout=1.0, poll_interval=0.02, job_class="heavy"))
        self.assertTrue(manager.is_held_by("stagger-lane-1"))
        manager.release("stagger-lane-1")

        # Lane 2 tries to acquire immediately with short timeout -> fails because stagger has not elapsed
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            res_2 = manager.acquire("stagger-lane-2", timeout=0.08, poll_interval=0.02, job_class="heavy")
        self.assertFalse(res_2)
        self.assertIn("stagger delay active", stderr_buf.getvalue())

        # After waiting for stagger to elapse, Lane 2 succeeds
        time.sleep(0.15)
        self.assertTrue(manager.acquire("stagger-lane-2", timeout=1.0, poll_interval=0.02, job_class="heavy"))
        self.assertTrue(manager.is_held_by("stagger-lane-2"))

        # Status reflects stagger state
        st = manager.status()
        self.assertNotIn("stagger-lane-1", st["holders"])
        self.assertIn("stagger-lane-2", st["holders"])

        manager.release("stagger-lane-2")

    def test_acquisition_stagger_bypassed_with_force(self):
        """--force bypasses acquisition stagger delay when BUILD_SLOT_ALLOW_FORCE=1."""
        os.environ["BUILD_SLOT_ALLOW_FORCE"] = "1"
        manager = BuildSlotManager(run_dir=self.run_dir, acquisition_stagger=10.0)
        self.assertTrue(manager.acquire("lane-first", timeout=1.0))
        # With 10s stagger, normal acquire would fail with 0.1s timeout, but force=True acquires immediately
        self.assertTrue(manager.acquire("lane-forced", timeout=0.2, poll_interval=0.02, force=True))
        self.assertTrue(manager.is_held_by("lane-forced"))
        manager.release("lane-first")
        manager.release("lane-forced")

    def test_max_slots_override_and_env_override(self):
        """get_max_slots respects constructor override and BUILD_SLOT_MAX_SLOTS env var."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertEqual(manager.get_max_slots(50.0), 8)
        self.assertEqual(manager.get_max_slots(90.0), 8)
        self.assertEqual(manager.get_max_slots(94.9), 8)

        override_mgr = BuildSlotManager(run_dir=self.run_dir, max_slots=3)
        self.assertEqual(override_mgr.get_max_slots(), 3)
        self.assertEqual(override_mgr.get_max_slots(50.0), 3)

        orig_env = os.environ.get("BUILD_SLOT_MAX_SLOTS")
        try:
            os.environ["BUILD_SLOT_MAX_SLOTS"] = "5"
            env_mgr = BuildSlotManager(run_dir=self.run_dir)
            self.assertEqual(env_mgr.get_max_slots(), 5)
        finally:
            if orig_env is not None:
                os.environ["BUILD_SLOT_MAX_SLOTS"] = orig_env
            else:
                os.environ.pop("BUILD_SLOT_MAX_SLOTS", None)
    def test_cli_diagnostics_emitted_once_on_stderr(self):
        """#325: CLI diagnostics appear on stderr exactly once (no duplicate lastResort output)."""
        script = os.path.join(SCRIPT_DIR, "build_slot.py")

        # 1. Timeout notice on acquire
        mgr = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        self.assertTrue(mgr.acquire("holder-lane", timeout=1.0))
        env = dict(os.environ, BUILD_SLOT_MAX_SLOTS="1")

        proc = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "acquire", "waiter-lane", "--timeout", "0.1", "--poll-interval", "0.02"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.returncode, 1)
        timeout_msg = "Timed out after 0.1s waiting for build slot lock (lane 'waiter-lane'"
        self.assertEqual(proc.stderr.count(timeout_msg), 1)
        self.assertNotIn("ERROR:build_slot:", proc.stderr)

        # 2. Release refusal notice
        proc_rel = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "release", "non-owner-lane"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc_rel.returncode, 1)
        release_msg = "ERROR: Refusing to release build slot lock:"
        self.assertEqual(proc_rel.stderr.count(release_msg), 1)
        self.assertNotIn("ERROR:build_slot:", proc_rel.stderr)
        mgr.release("holder-lane")

    def test_configured_logging_receives_records(self):
        """#325: Library callers that configure logging still receive records."""
        records = []

        class TestHandler(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = TestHandler()
        target_logger = logging.getLogger("build_slot")
        target_logger.addHandler(handler)
        old_level = target_logger.level
        target_logger.setLevel(logging.DEBUG)

        try:
            mgr = BuildSlotManager(run_dir=self.run_dir)
            self.assertTrue(mgr.acquire("log-holder", timeout=1.0))
            # Trigger release refusal to log an error
            err_buf = io.StringIO()
            with redirect_stderr(err_buf):
                self.assertFalse(mgr.release("wrong-owner"))

            error_records = [r for r in records if r.levelno == logging.ERROR]
            self.assertTrue(any("Refusing to release build slot lock" in r.getMessage() for r in error_records))
            mgr.release("log-holder")
        finally:
            target_logger.removeHandler(handler)
            target_logger.setLevel(old_level)

    def test_in_process_unconfigured_stderr_emitted_once(self):
        """#325: In-process calls without root handler do not duplicate stderr lines."""
        mgr = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        self.assertTrue(mgr.acquire("holder-lane", timeout=1.0))

        err = io.StringIO()
        with redirect_stderr(err):
            ok = mgr.acquire("waiter-lane", timeout=0.1, poll_interval=0.02)
        self.assertFalse(ok)
        timeout_msg = "Timed out after 0.1s waiting for build slot lock (lane 'waiter-lane'"
        self.assertEqual(err.getvalue().count(timeout_msg), 1)
        self.assertNotIn("ERROR:build_slot:", err.getvalue())
        mgr.release("holder-lane")

    def test_clean_stale_next_junction_removes_link_preserves_target_sentinel(self):
        """
        Removes stale Next.js standalone node_modules junction without deleting target contents.
        Root cause: Next's cleanDistDir follows the junction into the protected deps store,
        gets EPERM, and its unlinkPath retry never increments, so it loops forever on Windows.
        """
        target_dir = os.path.join(self.run_dir, "protected-node-modules")
        os.makedirs(target_dir, exist_ok=True)
        sentinel_path = os.path.join(target_dir, "sentinel.txt")
        with open(sentinel_path, "w", encoding="utf-8") as f:
            f.write("sentinel-content-must-survive")

        fake_worktree = os.path.join(self.run_dir, "fake-wt")
        standalone_dir = os.path.join(fake_worktree, "frontend", ".next", "standalone")
        os.makedirs(standalone_dir, exist_ok=True)
        link_path = os.path.join(standalone_dir, "node_modules")

        build_slot._create_dir_link(target_dir, link_path)
        self.assertTrue(os.path.exists(link_path) or build_slot._is_link_or_junction(link_path))
        self.assertTrue(build_slot._is_link_or_junction(link_path))
        self.assertTrue(os.path.isfile(os.path.join(link_path, "sentinel.txt")))

        # Call helper with cwd pointing to worktree
        cleaned = build_slot.clean_stale_next_junction(cwd=fake_worktree)
        self.assertIn(os.path.abspath(link_path), [os.path.abspath(p) for p in cleaned])
        self.assertFalse(build_slot._is_link_or_junction(link_path))
        self.assertFalse(os.path.exists(link_path))

        # The target directory and sentinel file MUST still exist untouched!
        self.assertTrue(os.path.isdir(target_dir))
        self.assertTrue(os.path.isfile(sentinel_path))
        with open(sentinel_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "sentinel-content-must-survive")

    def test_clean_stale_next_junction_auto_detect_frontend_cwd_and_next_dir(self):
        """Auto-detects <cwd>/.next/standalone/node_modules and handles optional --next-dir."""
        target_dir = os.path.join(self.run_dir, "shared-deps")
        os.makedirs(target_dir, exist_ok=True)
        sentinel = os.path.join(target_dir, "keepme.txt")
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write("keepme")

        # Case 1: Running from frontend directory directly (<cwd>/.next/standalone/node_modules)
        fake_frontend = os.path.join(self.run_dir, "fake-frontend")
        standalone_1 = os.path.join(fake_frontend, ".next", "standalone")
        os.makedirs(standalone_1, exist_ok=True)
        link_1 = os.path.join(standalone_1, "node_modules")
        build_slot._create_dir_link(target_dir, link_1)
        self.assertTrue(build_slot._is_link_or_junction(link_1))

        cleaned_1 = build_slot.clean_stale_next_junction(cwd=fake_frontend)
        self.assertIn(os.path.abspath(link_1), [os.path.abspath(p) for p in cleaned_1])
        self.assertFalse(build_slot._is_link_or_junction(link_1))
        self.assertTrue(os.path.isfile(sentinel))

        # Case 2: Using explicit next_dir
        custom_root = os.path.join(self.run_dir, "custom-project")
        custom_frontend = os.path.join(custom_root, "web")
        standalone_2 = os.path.join(custom_frontend, ".next", "standalone")
        os.makedirs(standalone_2, exist_ok=True)
        link_2 = os.path.join(standalone_2, "node_modules")
        build_slot._create_dir_link(target_dir, link_2)
        self.assertTrue(build_slot._is_link_or_junction(link_2))

        cleaned_2 = build_slot.clean_stale_next_junction(next_dir=custom_frontend, cwd=self.run_dir)
        self.assertIn(os.path.abspath(link_2), [os.path.abspath(p) for p in cleaned_2])
        self.assertFalse(build_slot._is_link_or_junction(link_2))
        self.assertTrue(os.path.isfile(sentinel))

        # Case 3: Regular directory (not a junction or symlink) is never removed
        normal_dir = os.path.join(fake_frontend, ".next", "standalone", "node_modules")
        os.makedirs(normal_dir, exist_ok=True)
        normal_file = os.path.join(normal_dir, "real_package.js")
        with open(normal_file, "w", encoding="utf-8") as f:
            f.write("console.log(1)")
        self.assertFalse(build_slot._is_link_or_junction(normal_dir))

        cleaned_3 = build_slot.clean_stale_next_junction(cwd=fake_frontend)
        self.assertEqual(cleaned_3, [])
        self.assertTrue(os.path.isdir(normal_dir))
        self.assertTrue(os.path.isfile(normal_file))

    def test_acquire_cleans_stale_next_junction(self):
        """build_slot.py acquire removes stale junction so subsequent next build does not hang."""
        target_dir = os.path.join(self.run_dir, "target-deps-acq")
        os.makedirs(target_dir, exist_ok=True)
        sentinel = os.path.join(target_dir, "sentinel.txt")
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write("sentinel-acq")

        fake_wt = os.path.join(self.run_dir, "wt-acq")
        standalone = os.path.join(fake_wt, "frontend", ".next", "standalone")
        os.makedirs(standalone, exist_ok=True)
        link_path = os.path.join(standalone, "node_modules")
        build_slot._create_dir_link(target_dir, link_path)
        self.assertTrue(build_slot._is_link_or_junction(link_path))

        manager = BuildSlotManager(run_dir=self.run_dir)
        ok = manager.acquire("lane-acq", cwd=fake_wt, timeout=2.0)
        self.assertTrue(ok)
        manager.release("lane-acq")

        # Junction must be removed, target sentinel must survive
        self.assertFalse(build_slot._is_link_or_junction(link_path))
        self.assertFalse(os.path.exists(link_path))
        self.assertTrue(os.path.isfile(sentinel))

    def test_run_command_cleans_stale_next_junction(self):
        """build_slot.py run removes stale junction before executing the command."""
        target_dir = os.path.join(self.run_dir, "target-deps-run")
        os.makedirs(target_dir, exist_ok=True)
        sentinel = os.path.join(target_dir, "sentinel.txt")
        with open(sentinel, "w", encoding="utf-8") as f:
            f.write("sentinel-run")

        fake_wt = os.path.join(self.run_dir, "wt-run")
        standalone = os.path.join(fake_wt, "frontend", ".next", "standalone")
        os.makedirs(standalone, exist_ok=True)
        link_path = os.path.join(standalone, "node_modules")
        build_slot._create_dir_link(target_dir, link_path)
        self.assertTrue(build_slot._is_link_or_junction(link_path))

        manager = BuildSlotManager(run_dir=self.run_dir)
        ret = manager.run_command(
            "lane-run-cleanup",
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            cwd=fake_wt,
        )
        self.assertEqual(ret, 0)

        # Junction must be removed, target sentinel must survive
        self.assertFalse(build_slot._is_link_or_junction(link_path))
        self.assertFalse(os.path.exists(link_path))
        self.assertTrue(os.path.isfile(sentinel))

    def test_cli_next_dir_option_parsed(self):
        """CLI options for --next-dir are parsed in acquire and run."""
        args_acq = build_slot.parse_args(["acquire", "lane", "--next-dir", "frontend"])
        self.assertEqual(args_acq.next_dir, "frontend")

        args_run_pre = build_slot.parse_args(
            ["run", "lane", "--next-dir", "frontend", "--", "next", "build"]
        )
        self.assertEqual(args_run_pre.next_dir, "frontend")
        self.assertEqual(args_run_pre.cmd, ["--", "next", "build"])

        args_run_with_cwd = build_slot.parse_args(
            ["run", "lane", "--cwd", "D:/wt", "--next-dir", "frontend", "--", "next", "build"]
        )
        self.assertEqual(args_run_with_cwd.cwd, "D:/wt")
        self.assertEqual(args_run_with_cwd.next_dir, "frontend")

    def test_regression_read_queue_raises_on_read_error(self):
        """
        Regression 1: _read_queue must raise (or retry and raise) on read/parse errors
        when the queue file exists, rather than returning [] which wipes the queue.
        A missing queue file returns [] only on legitimate initial creation.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        # 1. Non-existent file returns [] for legitimate initial creation
        self.assertFalse(os.path.exists(manager.queue_file))
        self.assertEqual(manager._read_queue(), [])

        # 2. Corrupt JSON in an existing file must RAISE, not return []
        with open(manager.queue_file, "w", encoding="utf-8") as f:
            f.write("{corrupt json[")

        with self.assertRaises(Exception):
            manager._read_queue()

        # 3. Non-list JSON in existing file must RAISE, not return []
        with open(manager.queue_file, "w", encoding="utf-8") as f:
            json.dump({"not": "a list"}, f)

        with self.assertRaises(Exception):
            manager._read_queue()

    def test_regression_queue_lock_never_age_reclaimed_for_live_pid_and_releaser_does_not_delete_successor(self):
        """
        Regression 2: a live queue lock younger than max_hold is never reclaimed (leaked ones are, see #690).
        Releaser must verify unique ownership token before deleting queue lock directory,
        so it never deletes a successor's lock directory.
        """
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir, exist_ok=True)
        info_path = os.path.join(queue_lock_dir, build_slot.INFO_FILE_NAME)

        # Part A: An old lock owned by a LIVE PID (os.getpid()) must NOT be reclaimed on age
        old_epoch = time.time() - 3600  # 1 hour old
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump({
                "pid": os.getpid(),
                "token": "original-live-token",
                "acquired_at": "2026-09-27T00:00:00Z",
                "acquired_at_epoch": old_epoch,
            }, f)

        # A contender attempting to acquire with stale_after=1.0s must NOT reclaim this live lock
        with self.assertRaises(TimeoutError):
            with _queue_atomic_lock(self.run_dir, timeout=0.1, retry_interval=0.02, stale_after=1.0, max_hold=7200.0):
                pass
        self.assertTrue(os.path.isdir(queue_lock_dir), "Live PID queue lock younger than max_hold must not be reclaimed")

        # Part B: Releaser must not delete successor lock with different token
        shutil.rmtree(queue_lock_dir, ignore_errors=True)
        with _queue_atomic_lock(self.run_dir, timeout=1.0) as _:
            # While holding, simulate that ownership transitioned to a successor
            with open(info_path, "w", encoding="utf-8") as f:
                json.dump({
                    "pid": os.getpid(),
                    "token": "successor-different-token",
                    "acquired_at": "2026-09-27T00:00:00Z",
                    "acquired_at_epoch": time.time(),
                }, f)

        # After exiting context manager, successor's lock directory and info must STILL exist
        self.assertTrue(os.path.isdir(queue_lock_dir), "Releaser must not delete successor's lock directory")
        self.assertTrue(os.path.isfile(info_path), "Releaser must not delete successor's info.json")
        info = build_slot._read_queue_lock_info(queue_lock_dir)
        self.assertEqual(info.get("token"), "successor-different-token")

    def test_regression_live_pid_queue_entry_expired_on_late_heartbeat(self):
        """
        #711: Dead in-process waiters with live shared PID must expire when heartbeat
        is older than stale_heartbeat_after (60s), regardless of PID liveness.
        """
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: (p == 2001))
        now = time.time()
        seeded_queue = [
            {
                "name": "live-pid-late-hb",
                "pid": 2001,
                "token": "tok-live",
                "enqueued_at": now - 300.0,
                "enqueued_at_iso": datetime.datetime.fromtimestamp(now - 300.0, datetime.timezone.utc).isoformat(),
                "heartbeat_at": now - 120.0,
                "heartbeat_at_iso": datetime.datetime.fromtimestamp(now - 120.0, datetime.timezone.utc).isoformat(),
            },
            {
                "name": "dead-pid-late-hb",
                "pid": 9999,
                "token": "tok-dead",
                "enqueued_at": now - 300.0,
                "enqueued_at_iso": datetime.datetime.fromtimestamp(now - 300.0, datetime.timezone.utc).isoformat(),
                "heartbeat_at": now - 120.0,
                "heartbeat_at_iso": datetime.datetime.fromtimestamp(now - 120.0, datetime.timezone.utc).isoformat(),
            },
        ]
        manager._write_queue(seeded_queue)

        cleaned = manager.clean_queue(stale_heartbeat_after=60.0)
        names = [x["name"] for x in cleaned]

        # Live PID must be PRUNED when heartbeat is expired (>60s) under #711
        self.assertNotIn("live-pid-late-hb", names, "Queue entry for living PID must be pruned when heartbeat expires")
        # Dead PID must be PRUNED
        self.assertNotIn("dead-pid-late-hb", names, "Queue entry for dead PID must be pruned")
        self.assertEqual(names, [])

    def test_regression_acquire_re_enqueue_preserves_original_enqueued_at(self):
        """Recovery in the acquire loop preserves the original FIFO timestamp."""
        manager = BuildSlotManager(run_dir=self.run_dir, max_slots=1, acquisition_stagger=0,
                                   is_pid_alive_fn=lambda p: True)
        manager.acquire("holder", timeout=1)
        enqueue = manager.enqueue
        timestamps = []

        def record_enqueue(*args, **kwargs):
            result = enqueue(*args, **kwargs)
            timestamps.append(manager._read_queue()[0]["enqueued_at"])
            return result

        def lose_entry(**kwargs):
            manager.dequeue(token=kwargs["token"])
            return False

        with mock.patch.object(manager, "enqueue", side_effect=record_enqueue), \
             mock.patch.object(manager, "heartbeat", side_effect=lose_entry):
            self.assertFalse(manager.acquire("waiter", timeout=0.08, poll_interval=0.01,
                                             heartbeat_interval=0.02, token="waiter-token"))
        self.assertGreater(len(timestamps), 1)
        self.assertEqual(timestamps, [timestamps[0]] * len(timestamps))
        manager.release("holder")

    def test_corrupt_queue_retry_respects_acquire_timeout(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        clock = [100.0]
        calls = []
        def corrupt_then_readable(**kwargs):
            calls.append(True)
            clock[0] = 101.0
            if len(calls) == 1:
                raise ValueError("corrupt queue")
            return []
        with mock.patch.object(manager, "enqueue"), \
             mock.patch.object(manager, "clean_queue", side_effect=corrupt_then_readable), \
             mock.patch("build_slot.time.time", side_effect=lambda: clock[0]), \
             mock.patch("build_slot.time.sleep"), \
             mock.patch.object(manager, "dequeue"):
            self.assertFalse(manager.acquire("corrupt", timeout=0.5, force=True))

    def test_live_token_leases_expire_but_fresh_heartbeats_survive(self):
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: p == 2001)
        now = time.time()
        manager._write_queue([
            {"name": "live-late", "pid": 2001, "token": "late", "enqueued_at": now - 2000,
             "heartbeat_at": now - 1801},
            {"name": "live-fresh", "pid": 2001, "token": "fresh", "enqueued_at": now - 2000,
             "heartbeat_at": now},
            {"name": "dead-fresh", "pid": 2002, "token": "dead", "enqueued_at": now - 2000,
             "heartbeat_at": now},
        ])
        self.assertEqual([x["name"] for x in manager.clean_queue()], ["live-fresh"])
        for wrapper in (False, True):
            os.makedirs(manager.lock_dir)
            manager._write_slot_info(0, "lease-holder", 2001, token="lease")
            info = manager._read_slot_info(0)
            info.update(acquired_at_epoch=now - 4000, heartbeat_at_epoch=now)
            if wrapper:
                info["wrapper_pid"] = 2001
            with open(manager.info_file, "w", encoding="utf-8") as f:
                json.dump(info, f)
            self.assertFalse(manager.check_stale_and_reclaim())
            info["heartbeat_at_epoch"] = now - 1801
            with open(manager.info_file, "w", encoding="utf-8") as f:
                json.dump(info, f)
            # an acquire lease expires; a live `run` wrapper keeps its slot (#690)
            self.assertEqual(manager.check_stale_and_reclaim(), not wrapper)

    def test_queue_release_retries_transient_ownership_read(self):
        original = build_slot._read_queue_lock_info
        calls = []
        def flaky(path):
            calls.append(path)
            return None if len(calls) == 1 else original(path)
        with mock.patch("build_slot._read_queue_lock_info", side_effect=flaky):
            with _queue_atomic_lock(self.run_dir):
                pass
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)))

    def _flaky_queue_lock(self, failures):
        """Wraps _queue_atomic_lock so its first 'failures' calls time out, like a contended lock."""
        real = build_slot._queue_atomic_lock
        state = {"left": failures, "mutex": threading.Lock()}

        def flaky(run_dir, *args, **kwargs):
            with state["mutex"]:
                fail = state["left"] > 0
                if fail:
                    state["left"] -= 1
            if fail:
                raise TimeoutError(f"Timed out waiting for queue file lock: {run_dir}")
            return real(run_dir, *args, **kwargs)

        return flaky

    def test_acquire_retries_transient_queue_lock_timeouts_until_own_deadline(self):
        """A queue-lock timeout while enqueueing must not abort the waiter or end the run."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        with mock.patch("build_slot._queue_atomic_lock", self._flaky_queue_lock(3)):
            ok = manager.acquire("contended-lane", timeout=10.0, poll_interval=0.01)
        self.assertTrue(ok)
        self.assertTrue(manager.is_held_by("contended-lane"))
        self.assertEqual(manager._read_queue(), [])
        manager.release("contended-lane")

    def test_acquire_returns_false_at_own_deadline_when_queue_lock_never_frees(self):
        """Only the caller's own --timeout ends the wait, and it ends as a clean False."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        with mock.patch("build_slot._queue_atomic_lock", self._flaky_queue_lock(10 ** 9)):
            start = time.time()
            ok = manager.acquire("starved-lane", timeout=0.5, poll_interval=0.01)
        self.assertFalse(ok)
        self.assertGreaterEqual(time.time() - start, 0.5)
        self.assertFalse(manager.is_held_by("starved-lane"))

    def test_release_retries_queue_cleanup_and_leaves_no_residue(self):
        """A queue-lock timeout during release cleanup is retried, not skipped."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(manager.acquire("release-lane", timeout=5.0, poll_interval=0.01, token="rel-token"))
        manager.enqueue("release-lane", os.getpid(), token="rel-token")
        self.assertEqual(len(manager._read_queue()), 1)
        err = io.StringIO()
        with mock.patch("build_slot._queue_atomic_lock", self._flaky_queue_lock(2)):
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                self.assertTrue(manager.release("release-lane", token="rel-token"))
        self.assertNotIn("skipped", err.getvalue())
        self.assertEqual(manager._read_queue(), [])
        self.assertFalse(manager.is_held_by("release-lane"))

    def test_many_waiters_survive_short_queue_lock_timeouts(self):
        """
        Many waiters polling one queue in an isolated run dir: a queue-lock timeout must
        never abort a waiter, drop its place, or leave residue after release.
        """
        real = build_slot._queue_atomic_lock

        def short_timeout(run_dir, *args, **kwargs):
            kwargs["timeout"] = 0.05
            return real(run_dir, *args, **kwargs)

        results = {}
        errors = []

        def waiter(idx):
            name = f"waiter-{idx}"
            try:
                mgr = BuildSlotManager(run_dir=self.run_dir, max_slots=3)
                ok = mgr.acquire(name, timeout=120.0, poll_interval=0.02)
                results[name] = ok
                if ok:
                    time.sleep(0.005)
                    with redirect_stdout(io.StringIO()):
                        mgr.release(name)
            except Exception as exc:  # noqa: BLE001 - any escape is the defect under test
                errors.append((name, repr(exc)))

        original_write = BuildSlotManager._write_queue

        def slow_write(manager_self, queue):
            time.sleep(0.01)
            return original_write(manager_self, queue)

        with mock.patch("build_slot._queue_atomic_lock", short_timeout), \
                mock.patch.object(BuildSlotManager, "_write_queue", slow_write), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            threads = [threading.Thread(target=waiter, args=(i,)) for i in range(24)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=180.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 24)
        self.assertTrue(all(results.values()), results)
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertEqual(manager._read_queue(), [])
        self.assertFalse(any(os.path.isdir(d) for d in manager.slot_dirs))

    def test_clean_queue_sweeps_entry_of_a_lease_that_holds_a_slot(self):
        """Cleanup that could not take the queue lock leaves an entry; the next sweep removes it."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(manager.acquire("holder-lane", timeout=5.0, poll_interval=0.01, token="held-token"))
        manager.enqueue("holder-lane", os.getpid(), token="held-token")
        manager.enqueue("other-lane", os.getpid(), token="other-token")
        remaining = manager.clean_queue()
        self.assertEqual([item["name"] for item in remaining], ["other-lane"])
        self.assertEqual([item["name"] for item in manager._read_queue()], ["other-lane"])
        manager.release("holder-lane", token="held-token")

    def test_queue_lock_release_survives_busy_info_file(self):
        """
        Waiters open info.json to judge the lock; Windows refuses to delete an open file.
        The owner must retry, or the lock dir outlives its release for as long as the owner lives.
        """
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        real_unlink = os.unlink
        refusals = {"left": 3}

        def busy_unlink(path, *args, **kwargs):
            if str(path).endswith(build_slot.INFO_FILE_NAME) and refusals["left"] > 0:
                refusals["left"] -= 1
                raise PermissionError(5, "Access is denied")
            return real_unlink(path, *args, **kwargs)

        with mock.patch("build_slot.os.unlink", busy_unlink):
            with _queue_atomic_lock(self.run_dir):
                pass
        self.assertEqual(refusals["left"], 0)
        self.assertFalse(os.path.exists(queue_lock_dir))

    def test_queue_lock_leaked_by_live_owner_is_reclaimed(self):
        """A live process that leaked the queue lock (release lost to open info.json) must not block waiters."""
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir)
        with open(os.path.join(queue_lock_dir, build_slot.INFO_FILE_NAME), "w") as f:
            json.dump({"pid": os.getpid(), "token": "leaked", "acquired_at_epoch": time.time() - 3600}, f)
        with _queue_atomic_lock(self.run_dir, timeout=2.0):
            pass
        self.assertFalse(os.path.exists(queue_lock_dir))

    def test_queue_lock_held_briefly_by_live_owner_is_not_reclaimed(self):
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir)
        with open(os.path.join(queue_lock_dir, build_slot.INFO_FILE_NAME), "w") as f:
            json.dump({"pid": os.getpid(), "token": "fresh", "acquired_at_epoch": time.time()}, f)
        with self.assertRaises(TimeoutError):
            with _queue_atomic_lock(self.run_dir, timeout=0.3):
                pass
        self.assertTrue(os.path.exists(queue_lock_dir))

    def test_acquire_does_not_overshoot_timeout_while_queue_lock_is_held(self):
        """Each queue-lock attempt is capped at the time left, so --timeout is not exceeded by a full 10 s attempt."""
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir)
        with open(os.path.join(queue_lock_dir, build_slot.INFO_FILE_NAME), "w") as f:
            json.dump({"pid": os.getpid(), "token": "busy", "acquired_at_epoch": time.time()}, f)
        manager = BuildSlotManager(run_dir=self.run_dir)
        start = time.time()
        ok = manager.acquire("capped-lane", timeout=1.0, poll_interval=0.01)
        elapsed = time.time() - start
        self.assertFalse(ok)
        self.assertLess(elapsed, 3.0)

    def test_queue_lock_is_not_held_while_the_wrapped_command_runs(self):
        """A `run` wrapper holds its slot for the child, never the queue lock (#690)."""
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        manager = BuildSlotManager(run_dir=self.run_dir)
        probe = f"import os,sys; sys.exit(7 if os.path.exists({queue_lock_dir!r}) else 0)"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = manager.run_command("child-lane", [sys.executable, "-c", probe], timeout=20.0, poll_interval=0.01)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(queue_lock_dir))

    def test_queue_atomic_lock_live_pid_holder_older_than_120s_is_reclaimed(self):
        """
        A live PID holding the queue lock that was acquired older than the default 120s
        hold threshold is considered leaked and reclaimed.
        """
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir)
        info_path = os.path.join(queue_lock_dir, build_slot.INFO_FILE_NAME)
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump({
                "pid": os.getpid(),
                "token": "leaked-token-130s",
                "acquired_at_epoch": time.time() - 130.0,
            }, f)
        acquired = False
        with _queue_atomic_lock(self.run_dir, timeout=2.0, retry_interval=0.01):
            acquired = True
            info = build_slot._read_queue_lock_info(queue_lock_dir)
            self.assertIsNotNone(info)
            self.assertNotEqual(info.get("token"), "leaked-token-130s")
        self.assertTrue(acquired)
        self.assertFalse(os.path.exists(queue_lock_dir))

    def test_queue_atomic_lock_fresh_live_pid_holder_under_120s_not_reclaimed_negative_control(self):
        """
        Negative control: A live PID holding the queue lock younger than 120s (e.g. 75s)
        must NOT be reclaimed by contenders using the default threshold, timing out instead.
        """
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir)
        info_path = os.path.join(queue_lock_dir, build_slot.INFO_FILE_NAME)
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump({
                "pid": os.getpid(),
                "token": "live-token-75s",
                "acquired_at_epoch": time.time() - 75.0,
            }, f)
        with self.assertRaises(TimeoutError):
            with _queue_atomic_lock(self.run_dir, timeout=0.1, retry_interval=0.01):
                pass
        self.assertTrue(os.path.isdir(queue_lock_dir))
        info = build_slot._read_queue_lock_info(queue_lock_dir)
        self.assertIsNotNone(info)
        self.assertEqual(info.get("token"), "live-token-75s")

    def test_queue_atomic_lock_late_release_does_not_remove_successor_lock(self):
        """
        When an expired owner releases its lock late after a successor has already
        acquired, the late release must verify ownership token and NOT delete the
        successor's lock directory or info file.
        """
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        info_path = os.path.join(queue_lock_dir, build_slot.INFO_FILE_NAME)

        with _queue_atomic_lock(self.run_dir, timeout=1.0) as _:
            # While holding, simulate that a successor acquired with a new token
            successor_token = "successor-token-" + str(time.time())
            with open(info_path, "w", encoding="utf-8") as f:
                json.dump({
                    "pid": os.getpid(),
                    "token": successor_token,
                    "acquired_at_epoch": time.time(),
                }, f)

        # After the late releaser exits, successor's lock directory and info must remain intact
        self.assertTrue(os.path.isdir(queue_lock_dir))
        self.assertTrue(os.path.isfile(info_path))
        info = build_slot._read_queue_lock_info(queue_lock_dir)
        self.assertIsNotNone(info)
        self.assertEqual(info.get("token"), successor_token)

    def test_queue_atomic_lock_stale_reclaim_race_preserves_successor_tombstone(self):
        """
        When stale reclamation judges token T1 stale, but before deletion a successor
        acquires with token T2, tombstone-safe reclaim must NOT remove T2.
        """
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir, exist_ok=True)
        info_path = os.path.join(queue_lock_dir, build_slot.INFO_FILE_NAME)

        judged_stale = {
            "pid": 99999999,
            "token": "stale-judged-token",
            "acquired_at_epoch": time.time() - 500.0,
        }
        # Successor acquired right before reclaim
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump({
                "pid": os.getpid(),
                "token": "fresh-successor-token",
                "acquired_at_epoch": time.time(),
            }, f)

        # Calling _tombstone_stale_queue_lock with judged_stale must refuse to delete the fresh successor
        reclaimed = build_slot._tombstone_stale_queue_lock(queue_lock_dir, judged_stale)
        self.assertFalse(reclaimed)
        self.assertTrue(os.path.isdir(queue_lock_dir))
        info = build_slot._read_queue_lock_info(queue_lock_dir)
        self.assertIsNotNone(info)
        self.assertEqual(info.get("token"), "fresh-successor-token")

    def test_queue_critical_section_hygiene_no_ram_probes_process_waits_or_sleep(self):
        """
        While holding the queue lock, queue operations (clean_queue, enqueue, bump, dequeue, heartbeat)
        must perform NO RAM probes (get_system_ram_percent), NO process waits (is_pid_alive),
        and NO sleep calls.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)

        violations = []

        real_is_pid_alive = manager.is_pid_alive
        def monitored_is_pid_alive(p):
            if os.path.isdir(queue_lock_dir):
                # Check if current thread actually holds the queue lock
                info = build_slot._read_queue_lock_info(queue_lock_dir)
                if info and info.get("pid") == os.getpid():
                    violations.append(f"is_pid_alive({p}) called while holding queue lock")
            return real_is_pid_alive(p)

        def monitored_ram_percent():
            if os.path.isdir(queue_lock_dir):
                violations.append("get_system_ram_percent() called while holding queue lock")
            return 50.0

        def monitored_sleep(seconds):
            if os.path.isdir(queue_lock_dir):
                info = build_slot._read_queue_lock_info(queue_lock_dir)
                if info and info.get("pid") == os.getpid():
                    violations.append(f"time.sleep({seconds}) called while holding queue lock")
            time.sleep(min(seconds, 0.001))

        manager.is_pid_alive = monitored_is_pid_alive

        # Pre-populate queue with an entry for another PID to exercise queue inspection loops
        manager._write_queue([{
            "name": "prior-lane",
            "pid": 999999,
            "token": "prior-tok",
            "enqueued_at": time.time(),
            "heartbeat_at": time.time(),
        }])

        with mock.patch("build_slot.get_system_ram_percent", monitored_ram_percent), \
             mock.patch("build_slot.time.sleep", monitored_sleep):
            # 1. Enqueue entry
            manager.enqueue("lane-1", pid=os.getpid(), token="tok-1")
            # 2. Bump entry
            manager.bump("lane-1", token="tok-1")
            # 3. Heartbeat
            manager.heartbeat(token="tok-1", name="lane-1", pid=os.getpid())
            # 4. Clean queue
            manager.clean_queue()
            # 5. Dequeue
            manager.dequeue("lane-1", pid=os.getpid(), token="tok-1")
        self.assertEqual(violations, [], "Critical section hygiene violations detected: " + str(violations))

    def test_release_toctou_preserves_successor_lock(self):
        """
        Verify release detaches and verifies lock identity before unlinking, so a successor
        lock's info.json and directory are never deleted.
        """
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir)
        info_path = os.path.join(queue_lock_dir, build_slot.INFO_FILE_NAME)
        with open(info_path, "w", encoding="utf-8") as f:
            f.write('{"pid": 1234, "token": "releaser-token"}')

        # Releaser removes its own lock
        ok = build_slot._remove_owned_queue_lock(queue_lock_dir, expected_token="releaser-token")
        self.assertTrue(ok)
        self.assertFalse(os.path.exists(queue_lock_dir))

        # When a successor acquired the lock with a different token
        os.makedirs(queue_lock_dir)
        with open(info_path, "w", encoding="utf-8") as f:
            f.write('{"pid": 5678, "token": "successor-token"}')
        ok2 = build_slot._remove_owned_queue_lock(queue_lock_dir, expected_token="releaser-token")
        self.assertFalse(ok2)
        self.assertTrue(os.path.exists(queue_lock_dir))
        self.assertTrue(os.path.exists(info_path))
        info = build_slot._read_queue_lock_info(queue_lock_dir)
        self.assertEqual(info.get("token"), "successor-token")

    def test_failed_stale_rename_honors_timeout_without_infinite_loop(self):
        """
        When os.rename fails during stale lock reclamation, the loop must honor timeout
        and back off rather than continuing infinitely.
        """
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir)
        info_path = os.path.join(queue_lock_dir, build_slot.INFO_FILE_NAME)
        with open(info_path, "w", encoding="utf-8") as f:
            f.write('{"pid": 999999, "token": "dead-token"}')

        with mock.patch("os.rename", side_effect=OSError("Access denied")):
            start = time.time()
            with self.assertRaises(TimeoutError):
                with build_slot._queue_atomic_lock(
                    self.run_dir,
                    timeout=0.2,
                    retry_interval=0.02,
                    is_pid_alive_fn=lambda p: False,
                ):
                    pass
            elapsed = time.time() - start
            self.assertLess(elapsed, 1.0)

    def test_metadata_free_fresh_successor_race_is_not_reclaimed(self):
        """
        A fresh directory without metadata must not be reclaimed as stale when judged was None.
        """
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir)
        reclaimed = build_slot._tombstone_stale_queue_lock(queue_lock_dir, judged=None, stale_after=15.0)
        self.assertFalse(reclaimed)
        self.assertTrue(os.path.exists(queue_lock_dir))
    def test_read_file_bytes_windows_readfile_failure_raises_winerror(self):
        """WinError from ReadFile raises out of _read_file_bytes and closes the handle; does not return EOF."""
        test_file = os.path.join(self.run_dir, "probe_readfile.txt")
        with open(test_file, "wb") as f:
            f.write(b"data that should not be returned as eof")

        closed_handles = []
        if sys.platform == "win32":
            import ctypes
            real_WinDLL = ctypes.WinDLL
            class FakeK32:
                def __init__(self, *args, **kwargs):
                    self._real = real_WinDLL(*args, **kwargs)
                def __getattr__(self, name):
                    if name == "ReadFile":
                        def fake_readfile(handle, buf, size, got_ptr, overlapped):
                            ctypes.set_last_error(23)  # ERROR_CRC
                            return 0
                        return fake_readfile
                    if name == "CloseHandle":
                        def fake_close(h):
                            closed_handles.append(h)
                            return self._real.CloseHandle(h)
                        return fake_close
                    return getattr(self._real, name)
            with mock.patch("ctypes.WinDLL", side_effect=FakeK32):
                with self.assertRaises(OSError) as ctx:
                    build_slot._read_file_bytes(test_file)
                self.assertEqual(getattr(ctx.exception, "winerror", None), 23)
            self.assertTrue(len(closed_handles) > 0)

    def test_read_file_bytes_windows_close_before_parse(self):
        """Handle is closed before json.loads parses the file."""
        test_file = os.path.join(self.run_dir, "probe_close.json")
        with open(test_file, "w", encoding="utf-8") as f:
            json.dump({"valid": True}, f)

        events = []
        if sys.platform == "win32":
            import ctypes
            real_WinDLL = ctypes.WinDLL
            class TrackingK32:
                def __init__(self, *args, **kwargs):
                    self._real = real_WinDLL(*args, **kwargs)
                def __getattr__(self, name):
                    if name == "CloseHandle":
                        def tracking_close(h):
                            events.append("close")
                            return self._real.CloseHandle(h)
                        return tracking_close
                    return getattr(self._real, name)
            orig_loads = json.loads
            def tracking_loads(*args, **kwargs):
                events.append("parse")
                return orig_loads(*args, **kwargs)

            with mock.patch("ctypes.WinDLL", side_effect=TrackingK32), mock.patch("json.loads", side_effect=tracking_loads):
                data = build_slot._read_json_file(test_file)
                self.assertEqual(data, {"valid": True})
            self.assertEqual(events, ["close", "parse"])

    def test_recycled_wrapper_pid_reclaimed_promptly_on_mismatched_creation_identity(self):
        """A live process holding a slot whose recorded creation ticks do not match is a recycled PID and reclaimed."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        live_proc = self._live_process()
        # Recorded ticks 999999 mismatches actual ticks of live_proc
        self._write_run_lock(manager, live_proc.pid, self._exited_pid(), age=7200.0, hb_age=7000.0, wrapper_created_ticks=999999)
        with redirect_stderr(io.StringIO()):
            self.assertTrue(manager.check_stale_and_reclaim())
        self.assertFalse(os.path.isdir(manager.lock_dir))

    def test_recycled_wrapper_pid_legacy_lock_created_after_acquire_reclaimed(self):
        """A legacy lock with no recorded ticks whose live process was created after acquire is a recycled PID and reclaimed."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        live_proc = self._live_process()
        # Legacy lock: wrapper_created_ticks=False, acquired_at_epoch set to 2 hours ago
        # The live_proc was created just now, which is > acquired_at_epoch
        self._write_run_lock(manager, live_proc.pid, self._exited_pid(), age=7200.0, hb_age=7000.0, wrapper_created_ticks=False)
        with redirect_stderr(io.StringIO()):
            self.assertTrue(manager.check_stale_and_reclaim())
        self.assertFalse(os.path.isdir(manager.lock_dir))

    def test_live_wrapper_unknown_identity_never_reclaimed_on_stale_heartbeat(self):
        """When creation identity cannot be queried, a live wrapper is kept safe and never reclaimed on stale heartbeat."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        live_proc = self._live_process()
        self._write_run_lock(manager, live_proc.pid, live_proc.pid, age=7200.0, hb_age=7000.0, wrapper_created_ticks=False)
        with mock.patch("build_slot._get_process_create_ticks", return_value=None), \
             mock.patch("build_slot._get_process_create_epoch", return_value=None):
            with redirect_stderr(io.StringIO()):
                self.assertFalse(manager.check_stale_and_reclaim())
        self.assertTrue(os.path.isdir(manager.lock_dir))

    def test_run_mode_wrapper_metadata_recorded_at_initial_grant(self):
        """run_command records wrapper_pid and creation identity in info.json at initial grant."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        my_pid = os.getpid()
        ok = manager.acquire("grant-test", timeout=5.0, pid=my_pid, wrapper_pid=my_pid)
        self.assertTrue(ok)
        info = build_slot._read_lock_dir_info(manager.lock_dir, 0)
        self.assertEqual(info.get("wrapper_pid"), my_pid)
        if sys.platform == "win32":
            self.assertIsNotNone(info.get("wrapper_created_ticks"))
        manager.release("grant-test")

    def test_run_mode_preserved_when_info_replace_fails_with_winerror5_and_older_than_30min(self):
        """Simulated WinError 5 on info.json replace writes heartbeat.json with wrapper metadata; >30min run stays run mode."""
        import uuid
        manager = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        live_proc = self._live_process()
        token = str(uuid.uuid4())
        os.mkdir(manager.lock_dir)
        now = time.time()
        # 40 minutes old (past 30-min acquire-mode limit)
        acquired_epoch = now - 2400.0
        info = {
            "owner": "long-runner",
            "pid": live_proc.pid,
            "slot": 0,
            "token": token,
            "acquired_at_epoch": acquired_epoch,
            "heartbeat_at_epoch": now - 10.0,
        }
        with open(manager.info_file, "w", encoding="utf-8") as f:
            json.dump(info, f)

        # Fail replace of info.json with WinError 5
        real_replace = os.replace
        def replace_denied_for_info(src, dst):
            if os.path.basename(dst) == build_slot.INFO_FILE_NAME:
                err = PermissionError(13, "Access is denied")
                err.winerror = 5
                raise err
            return real_replace(src, dst)

        with mock.patch("time.sleep", return_value=None), mock.patch("os.replace", side_effect=replace_denied_for_info):
            with redirect_stderr(io.StringIO()):
                ok = manager._record_run_child("long-runner", wrapper_pid=live_proc.pid, child_pid=live_proc.pid, token=token)
                self.assertTrue(ok)

        # heartbeat.json must carry wrapper_pid
        hb_path = os.path.join(manager.lock_dir, build_slot.HEARTBEAT_FILE_NAME)
        self.assertTrue(os.path.isfile(hb_path))
        with open(hb_path, encoding="utf-8") as f:
            hb_data = json.load(f)
        self.assertEqual(hb_data.get("wrapper_pid"), live_proc.pid)

        # _read_lock_dir_info merges wrapper_pid
        slot_info = build_slot._read_lock_dir_info(manager.lock_dir, 0)
        self.assertEqual(slot_info.get("wrapper_pid"), live_proc.pid)

        # check_stale_and_reclaim does NOT reclaim despite being 40 minutes old
        with redirect_stderr(io.StringIO()):
            self.assertFalse(manager.check_stale_and_reclaim())
        self.assertTrue(os.path.isdir(manager.lock_dir))

        # A second lane gets NO grant
        waiter = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        with redirect_stderr(io.StringIO()):
            self.assertFalse(waiter.acquire("second-lane", timeout=0.2, poll_interval=0.05, force=True))

    def test_side_heartbeat_mismatched_token_does_not_update_owner_or_wrapper_metadata(self):
        """heartbeat.json with a mismatched token never updates info.json's heartbeat or wrapper metadata."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        os.mkdir(manager.lock_dir)
        info = {
            "owner": "real-owner",
            "token": "tok-A",
            "heartbeat_at_epoch": 100.0,
        }
        with open(manager.info_file, "w", encoding="utf-8") as f:
            json.dump(info, f)

        side = {
            "token": "tok-B",
            "heartbeat_at_epoch": 200.0,
            "wrapper_pid": 99999,
        }
        with open(os.path.join(manager.lock_dir, build_slot.HEARTBEAT_FILE_NAME), "w", encoding="utf-8") as f:
            json.dump(side, f)

        read_info = build_slot._read_lock_dir_info(manager.lock_dir, 0)
        self.assertEqual(read_info["heartbeat_at_epoch"], 100.0)
        self.assertIsNone(read_info.get("wrapper_pid"))

    def test_record_run_child_fails_safely_when_neither_write_works(self):
        """When neither info.json nor heartbeat.json can be written, _record_run_child returns False."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        os.mkdir(manager.lock_dir)
        info = {
            "owner": "lane-fail",
            "token": "tok-1",
            "slot": 0,
        }
        with open(manager.info_file, "w", encoding="utf-8") as f:
            json.dump(info, f)

        def all_writes_fail(src, dst):
            raise OSError("disk full")

        with mock.patch("time.sleep", return_value=None), mock.patch("os.replace", side_effect=all_writes_fail):
            with redirect_stderr(io.StringIO()):
                ok = manager._record_run_child("lane-fail", wrapper_pid=os.getpid(), child_pid=os.getpid(), token="tok-1")
                self.assertFalse(ok)

    def test_run_command_survives_failed_heartbeat_writes_without_abort_or_double_grant(self):
        """When heartbeat writes fail after initial grant, command runs to normal completion without abort or double grant."""
        manager = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        orig_write = build_slot._write_json_atomic
        grant_written = False

        def deny_heartbeat_writes(path, data, prefix=".info-", *args, **kwargs):
            nonlocal grant_written
            if os.path.dirname(path) in manager.slot_dirs:
                if not grant_written:
                    grant_written = True
                    return orig_write(path, data, prefix=prefix, *args, **kwargs)
                # Deny both subsequent heartbeat writes (info.json and heartbeat.json)
                raise OSError("simulated heartbeat write failure: disk full")
            return orig_write(path, data, prefix=prefix, *args, **kwargs)

        cmd = [sys.executable, "-c", "import time; time.sleep(0.3)"]
        run_ret = []

        def runner():
            with redirect_stderr(io.StringIO()):
                ret = manager.run_command("run-lane", cmd)
                run_ret.append(ret)

        with mock.patch("build_slot._write_json_atomic", side_effect=deny_heartbeat_writes):
            t = threading.Thread(target=runner)
            t.start()
            try:
                # Wait until slot is acquired and runner thread is active in the command
                for _ in range(50):
                    time.sleep(0.02)
                    if grant_written:
                        break
                self.assertTrue(grant_written)
                # Confirm slot is locked and cannot be double-granted while active
                waiter = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
                with redirect_stderr(io.StringIO()):
                    second_grant = waiter.acquire("second-lane", timeout=0.1, poll_interval=0.02, force=True)
                self.assertFalse(second_grant)
                with redirect_stderr(io.StringIO()):
                    self.assertFalse(waiter.check_stale_and_reclaim())
            finally:
                t.join(timeout=10.0)

        self.assertEqual(run_ret, [0])
        # After normal completion, lock is released and another lane can acquire
        waiter = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        self.assertTrue(waiter.acquire("after-release", timeout=1.0))
        waiter.release("after-release")

    def test_get_process_create_epoch_never_returns_proc_dir_mtime(self):
        """_get_process_create_epoch returns None instead of /proc/<pid> directory st_mtime when identity unavailable."""
        fake_pid = 99999
        fake_stat = mock.Mock()
        fake_stat.st_mtime = 1700000000.0

        with mock.patch("build_slot._get_process_create_ticks", return_value=None), \
             mock.patch.dict("sys.modules", {"psutil": None}), \
             mock.patch("os.path.isdir", side_effect=lambda p: p == f"/proc/{fake_pid}"), \
             mock.patch("os.path.isfile", return_value=False), \
             mock.patch("os.stat", return_value=fake_stat):
            epoch = build_slot._get_process_create_epoch(fake_pid)
            self.assertIsNone(epoch)

    def test_proc_dir_mtime_change_never_causes_live_owner_to_be_recycled(self):
        """A live owner is never reclaimed because /proc/<pid> directory mtime shifted."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        live_proc = self._live_process()
        # Initial lock recorded with epoch 1700000000.0
        self._write_run_lock(manager, live_proc.pid, live_proc.pid, age=7200.0, hb_age=7000.0, wrapper_created_ticks=False)
        info = build_slot._read_lock_dir_info(manager.lock_dir, 0)
        info["wrapper_created_epoch"] = 1700000000.0
        with open(manager.info_file, "w", encoding="utf-8") as f:
            json.dump(info, f)

        # Simulate /proc/<pid> directory mtime jumping by 500 seconds
        jumped_stat = mock.Mock()
        jumped_stat.st_mtime = 1700000500.0

        orig_isdir = os.path.isdir
        orig_stat = os.stat
        def fake_isdir(p):
            if str(p) == f"/proc/{live_proc.pid}":
                return True
            return orig_isdir(p)
        def fake_stat(p, *args, **kwargs):
            if str(p) == f"/proc/{live_proc.pid}":
                return jumped_stat
            return orig_stat(p, *args, **kwargs)

        with mock.patch("build_slot._get_process_create_ticks", return_value=None), \
             mock.patch.dict("sys.modules", {"psutil": None}), \
             mock.patch("os.path.isdir", side_effect=fake_isdir), \
             mock.patch("os.stat", side_effect=fake_stat):
            with redirect_stderr(io.StringIO()):
                reclaimed = manager.check_stale_and_reclaim()
            self.assertFalse(reclaimed)
        self.assertTrue(os.path.isdir(manager.lock_dir))

    def test_get_process_create_epoch_reads_linux_proc_stat_starttime_and_btime(self):
        """_get_process_create_epoch correctly parses /proc/<pid>/stat starttime and /proc/stat btime."""
        fake_pid = 4242
        fake_stat_content = "4242 (test (process) name) S 1 4242 4242 0 -1 4194304 100 0 0 0 10 5 0 0 20 0 1 0 500 123456 100 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
        fake_proc_stat = "cpu 123 456\nbtime 1700000000\nprocesses 789\n"

        def fake_isfile(path):
            return path in (f"/proc/{fake_pid}/stat", "/proc/stat")

        def fake_open(path, *args, **kwargs):
            if path == f"/proc/{fake_pid}/stat":
                return io.StringIO(fake_stat_content)
            if path == "/proc/stat":
                return io.StringIO(fake_proc_stat)
            raise FileNotFoundError(path)

        with mock.patch("build_slot._get_process_create_ticks", return_value=None), \
             mock.patch.dict("sys.modules", {"psutil": None}), \
             mock.patch("os.path.isfile", side_effect=fake_isfile), \
             mock.patch("builtins.open", side_effect=fake_open):
            epoch = build_slot._get_process_create_epoch(fake_pid)
            self.assertIsNotNone(epoch)
            self.assertAlmostEqual(epoch, 1700000005.0, places=2)

    def test_build_freeze_refuses_acquire(self):
        freeze_file = os.path.join(self.run_dir, "build-freeze")
        with open(freeze_file, "w", encoding="utf-8") as f:
            f.write("operator freeze 2026-10-09\n")

        err = io.StringIO()
        with redirect_stderr(err):
            ret = build_slot.main(["--run-dir", self.run_dir, "acquire", "lane-frozen"])

        self.assertEqual(ret, 75)
        self.assertIn("build freeze active (operator freeze 2026-10-09)", err.getvalue())

        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertFalse(manager.status()["lock"]["locked"])
        queue_path = os.path.join(self.run_dir, "build-slot.queue.json")
        self.assertFalse(os.path.exists(queue_path))

    def test_build_freeze_refuses_run_without_launching_command(self):
        freeze_file = os.path.join(self.run_dir, "build-freeze")
        with open(freeze_file, "w", encoding="utf-8") as f:
            f.write("operator freeze 2026-10-09\n")

        marker = os.path.join(self.run_dir, "dummy-ran.marker")
        dummy_cmd = [sys.executable, "-c", "import sys, pathlib; pathlib.Path(sys.argv[1]).touch()", marker]

        err = io.StringIO()
        with redirect_stderr(err):
            ret = build_slot.main(["--run-dir", self.run_dir, "run", "lane-frozen", "--"] + dummy_cmd)

        self.assertEqual(ret, 75)
        self.assertIn("build freeze active (operator freeze 2026-10-09)", err.getvalue())
        self.assertFalse(os.path.exists(marker))

        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertFalse(manager.status()["lock"]["locked"])
        queue_path = os.path.join(self.run_dir, "build-slot.queue.json")
        self.assertFalse(os.path.exists(queue_path))

    def test_build_freeze_unreadable_falls_back_to_reason_unavailable(self):
        freeze_file = os.path.join(self.run_dir, "build-freeze")
        with open(freeze_file, "w", encoding="utf-8") as f:
            f.write("some reason\n")

        orig_open = open
        def fail_freeze_open(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)) and os.path.abspath(file) == os.path.abspath(freeze_file):
                raise OSError("simulated disk read failure")
            return orig_open(file, *args, **kwargs)

        err = io.StringIO()
        with mock.patch("builtins.open", side_effect=fail_freeze_open):
            with redirect_stderr(err):
                ret = build_slot.main(["--run-dir", self.run_dir, "acquire", "lane-frozen"])

        self.assertEqual(ret, 75)
        self.assertIn("build freeze active (reason unavailable)", err.getvalue())

    def test_build_freeze_does_not_affect_release_or_status(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        acquired = manager.acquire("lane-owner", timeout=1.0)
        self.assertTrue(acquired)
        self.assertTrue(manager.status()["lock"]["locked"])

        freeze_file = os.path.join(self.run_dir, "build-freeze")
        with open(freeze_file, "w", encoding="utf-8") as f:
            f.write("operator freeze 2026-10-09\n")

        out_human = io.StringIO()
        with redirect_stdout(out_human):
            ret_status = build_slot.main(["--run-dir", self.run_dir, "status"])
        self.assertEqual(ret_status, 0)
        self.assertIn("Build Slot Arbiter Status", out_human.getvalue())

        out_json = io.StringIO()
        with redirect_stdout(out_json):
            ret_json = build_slot.main(["--run-dir", self.run_dir, "status", "--json"])
        self.assertEqual(ret_json, 0)
        data = json.loads(out_json.getvalue())
        self.assertTrue(data["lock"]["locked"])
        self.assertEqual(data["lock"]["owner"], "lane-owner")

        ret_release = build_slot.main(["--run-dir", self.run_dir, "release", "lane-owner"])
        self.assertEqual(ret_release, 0)
        self.assertFalse(manager.status()["lock"]["locked"])

    # -------------------------------------------------------------------------
    # Build-slot memory safety, one-heavy cap, force-env, run-timeout, and freeze
    # -------------------------------------------------------------------------

    def test_get_available_ram_gib_reads_env_and_system(self):
        """get_available_ram_gib returns float or None, respecting BUILD_SLOT_AVAILABLE_GIB."""
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "16.5"
        val = build_slot.get_available_ram_gib()
        self.assertIsInstance(val, float)
        self.assertEqual(val, 16.5)

        os.environ.pop("BUILD_SLOT_AVAILABLE_GIB", None)
        sys_val = build_slot.get_available_ram_gib()
        if sys_val is not None:
            self.assertIsInstance(sys_val, float)
            self.assertGreater(sys_val, 0.0)

    def test_admission_arithmetic_floor_three_gib(self):
        """
        Memory admission: available_ram - sum(reservations) - new >= 3.0 GiB.
        Refuses admission when floor is violated, admits when satisfied.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "10.0"

        # Slot 1 reserves 4.0 GiB (10.0 - 0 - 4.0 = 6.0 >= 3.0 floor) -> admitted
        self.assertTrue(manager.acquire("slot-1", mem_gib=4.0, timeout=1.0))

        # Slot 2 requests 4.0 GiB (10.0 - 4.0 - 4.0 = 2.0 < 3.0 floor) -> refused
        with redirect_stderr(io.StringIO()):
            self.assertFalse(manager.acquire("slot-2-fail", mem_gib=4.0, timeout=0.2, poll_interval=0.02))

        # Slot 2 requests 3.0 GiB (10.0 - 4.0 - 3.0 = 3.0 >= 3.0 floor) -> admitted
        self.assertTrue(manager.acquire("slot-2-ok", mem_gib=3.0, timeout=1.0))

        manager.release("slot-1")
        manager.release("slot-2-ok")

    def test_legacy_slots_without_metadata_reserve_five_gib(self):
        """Legacy slots without reservation metadata keep the 5 GiB heavy minimum."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "10.0"

        # Create simulated legacy slot directory and info.json with no mem_gib or job_class
        os.makedirs(manager.slot_dirs[0], exist_ok=True)
        now_epoch = time.time()
        legacy_info = {
            "owner": "legacy-lane",
            "pid": os.getpid(),
            "token": "tok-legacy-1",
            "acquired_at": datetime.datetime.fromtimestamp(now_epoch, datetime.timezone.utc).isoformat(),
            "acquired_at_epoch": now_epoch,
        }
        with open(os.path.join(manager.slot_dirs[0], build_slot.INFO_FILE_NAME), "w", encoding="utf-8") as f:
            json.dump(legacy_info, f)

        # 10.0 available - 5.0 legacy - 3.0 new = 2.0 < 3.0 floor -> refused
        with redirect_stderr(io.StringIO()):
            self.assertFalse(manager.acquire("slot-new-fail", mem_gib=3.0, timeout=0.2, poll_interval=0.02))

        # 10.0 available - 5.0 legacy - 2.0 new = 3.0 >= 3.0 floor -> admitted
        self.assertTrue(manager.acquire("slot-new-ok", mem_gib=2.0, timeout=1.0))
        manager.release("slot-new-ok")
        manager.release("legacy-lane")

    def test_default_acquire_sets_job_class_light(self):
        """Default acquire without explicit job_class defaults to 'light'."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(manager.acquire("default-lane", timeout=1.0))
        slot_info = manager._read_slot_info(0)
        self.assertIsNotNone(slot_info)
        self.assertEqual(slot_info.get("job_class"), "light")
        manager.release("default-lane")

    def test_one_heavy_cap_blocks_concurrent_heavy_jobs(self):
        """At most one 'heavy' job may run concurrently across all slots."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "64.0"

        # Slot 1 acquires as heavy
        self.assertTrue(manager.acquire("heavy-lane-1", job_class="heavy", timeout=1.0))

        # Slot 2 attempts heavy acquire while heavy-lane-1 is active -> refused
        with redirect_stderr(io.StringIO()):
            self.assertFalse(manager.acquire("heavy-lane-2", job_class="heavy", timeout=0.2, poll_interval=0.02))

        # Meanwhile, a medium job can acquire concurrently
        self.assertTrue(manager.acquire("medium-lane", job_class="medium", timeout=1.0))

        # Once heavy-lane-1 releases, a heavy job can acquire
        manager.release("heavy-lane-1")
        self.assertTrue(manager.acquire("heavy-lane-2", job_class="heavy", timeout=1.0))

        manager.release("medium-lane")
        manager.release("heavy-lane-2")

    def test_force_cannot_bypass_heavy_cap(self):
        """Even with BUILD_SLOT_ALLOW_FORCE=1, --force cannot bypass the one-heavy concurrency cap."""
        os.environ["BUILD_SLOT_ALLOW_FORCE"] = "1"
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "64.0"
        manager = BuildSlotManager(run_dir=self.run_dir)

        self.assertTrue(manager.acquire("heavy-1", job_class="heavy", timeout=1.0))

        # Second heavy with force=True must still be refused
        with redirect_stderr(io.StringIO()):
            self.assertFalse(manager.acquire("heavy-2-force", job_class="heavy", timeout=0.2, poll_interval=0.02, force=True))

        manager.release("heavy-1")

    def test_force_requires_build_slot_allow_force_env(self):
        """--force is ignored unless BUILD_SLOT_ALLOW_FORCE=1 is set in environment."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "4.0"

        # Without env var, force=True does not bypass floor (4 - 0 - 2 = 2 < 3)
        os.environ.pop("BUILD_SLOT_ALLOW_FORCE", None)
        with redirect_stderr(io.StringIO()):
            self.assertFalse(manager.acquire("force-noenv", mem_gib=2.0, timeout=0.2, poll_interval=0.02, force=True))

        # With BUILD_SLOT_ALLOW_FORCE=1, force=True bypasses admission
        os.environ["BUILD_SLOT_ALLOW_FORCE"] = "1"
        self.assertTrue(manager.acquire("force-with-env", mem_gib=2.0, timeout=1.0, force=True))
        manager.release("force-with-env")

    def test_run_timeout_kills_child_and_grandchild_process_tree_with_exit_124(self):
        """run --run-timeout kills the entire process tree and exits 124 when timeout elapses."""
        gc_pid_file = os.path.join(self.run_dir, "grandchild.pid")
        child_py = os.path.join(self.run_dir, "child_spawner.py")
        child_code = (
            "import sys, subprocess, time\n"
            "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
            "creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))\n"
            f"with open(r'{gc_pid_file}', 'w') as f: f.write(str(gc.pid))\n"
            "time.sleep(60)\n"
        )
        with open(child_py, "w", encoding="utf-8") as f:
            f.write(child_code)

        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, os.path.abspath(build_slot.__file__), "--run-dir", self.run_dir,
             "run", "lane-timeout", "--run-timeout", "1", "--", sys.executable, child_py],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 124, f"stderr: {proc.stderr}")
        self.assertLess(elapsed, 15)

        self.assertTrue(os.path.exists(gc_pid_file))
        with open(gc_pid_file, encoding="utf-8") as f:
            grandchild_pid = int(f.read().strip())

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and is_pid_alive(grandchild_pid):
            time.sleep(0.1)
        self.assertFalse(is_pid_alive(grandchild_pid))

    def test_run_queue_timeout_does_not_kill_running_command(self):
        """Existing --timeout option governs FIFO queue wait time only, not command execution."""
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, os.path.abspath(build_slot.__file__), "--run-dir", self.run_dir,
             "run", "lane-normal", "--timeout", "0.2", "--", sys.executable, "-c", "import time; time.sleep(0.5)"],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
        self.assertGreaterEqual(time.monotonic() - started, 0.4)

    def test_freeze_during_queue_wait_raises_system_exit_75(self):
        """A waiter blocked in the FIFO queue raises SystemExit(75) when build-freeze appears."""
        manager_blocker = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        self.assertTrue(manager_blocker.acquire("blocker-lane", timeout=1.0))

        freeze_file = os.path.join(self.run_dir, "build-freeze")
        stop_event = threading.Event()

        def write_freeze():
            time.sleep(0.1)
            if not stop_event.is_set() and os.path.isdir(self.run_dir):
                try:
                    with open(freeze_file, "w", encoding="utf-8") as f:
                        f.write("freeze active while queued\n")
                except OSError:
                    pass

        t = threading.Thread(target=write_freeze)
        t.start()

        waiter_mgr = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        err = io.StringIO()
        try:
            with redirect_stderr(err):
                with self.assertRaises(SystemExit) as ctx:
                    waiter_mgr.acquire("waiter-frozen", timeout=0.6, poll_interval=0.02)
            self.assertEqual(ctx.exception.code, 75)
            self.assertIn("build freeze active (freeze active while queued)", err.getvalue())
        finally:
            stop_event.set()
            t.join()
            manager_blocker.release("blocker-lane")

    def test_freeze_while_queue_mutex_contended(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        freeze_path = os.path.join(self.run_dir, "build-freeze")
        def freeze():
            time.sleep(0.25)
            with open(freeze_path, "w", encoding="utf-8") as stream:
                stream.write("contended freeze")
        with _queue_atomic_lock(self.run_dir):
            writer = threading.Thread(target=freeze)
            writer.start()
            try:
                with self.assertRaises(SystemExit) as raised:
                    manager.acquire("mutex-waiter", timeout=0.6, poll_interval=0.02)
                self.assertEqual(raised.exception.code, 75)
            finally:
                writer.join(timeout=2)

    @unittest.skipUnless(sys.platform == "win32", "Windows Job Object")
    def test_forced_wrapper_exit_kills_tree_and_releases_reservation(self):
        child_script = os.path.join(self.run_dir, "crash_child.py")
        pids_file = os.path.join(self.run_dir, "crash_pids.json")
        with open(child_script, "w", encoding="utf-8") as stream:
            stream.write(
                "import json,os,subprocess,sys,time\n"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
                "creationflags=subprocess.CREATE_NO_WINDOW)\n"
                "with open(sys.argv[1],'w') as f: json.dump([os.getpid(),child.pid],f)\n"
                "time.sleep(60)\n"
            )
        wrapper = subprocess.Popen(
            [sys.executable, build_slot.__file__, "--run-dir", self.run_dir,
             "run", "crash-wrapper", "--run-timeout", "3", "--",
             sys.executable, child_script, pids_file],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        pids = []
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not os.path.exists(pids_file):
                time.sleep(0.02)
            self.assertTrue(os.path.exists(pids_file), "real child must start")
            with open(pids_file, encoding="utf-8") as stream:
                pids = json.load(stream)
            wrapper.kill()
            wrapper.wait(timeout=5)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and any(is_pid_alive(pid) for pid in pids):
                time.sleep(0.02)
            self.assertFalse(any(is_pid_alive(pid) for pid in pids), "wrapper death must kill its tree within 2s")
            self.assertEqual(BuildSlotManager(run_dir=self.run_dir).status()["active_slots"], 0)
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
                wrapper.wait(timeout=5)
            for pid in pids:
                if is_pid_alive(pid):
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @unittest.skipUnless(sys.platform == "win32", "Windows creation-time job ownership")
    def test_wrapper_death_before_child_constructor_returns(self):
        probe = os.path.join(self.run_dir, "creation_probe.py")
        marker = os.path.join(self.run_dir, "created_pid")
        with open(probe, "w", encoding="utf-8") as stream:
            stream.write(
                "import sys,time,subprocess\n"
                "sys.path.insert(0,sys.argv[1]);import build_slot\n"
                "native=hasattr(build_slot,'_WindowsJobProcess')\n"
                "original=build_slot._WindowsJobProcess if native else subprocess.Popen\n"
                "def paused(*args,**kwargs):\n"
                " proc=original(*args,**kwargs)\n"
                " with open(sys.argv[3],'w') as f:f.write(str(proc.pid))\n"
                " time.sleep(60)\n"
                " return proc\n"
                "if native:build_slot._WindowsJobProcess=paused\n"
                "else:subprocess.Popen=paused\n"
                "build_slot.BuildSlotManager(run_dir=sys.argv[2]).run_command("
                "'creation-probe',[sys.executable,'-c','import time;time.sleep(60)'])\n"
            )
        wrapper = subprocess.Popen(
            [sys.executable, probe, SCRIPT_DIR, self.run_dir, marker],
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        child_pid = None
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not os.path.exists(marker):
                time.sleep(0.02)
            self.assertTrue(os.path.exists(marker), "child must exist before constructor returns")
            with open(marker) as stream:
                child_pid = int(stream.read())
            wrapper.kill()
            wrapper.wait(timeout=5)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and is_pid_alive(child_pid):
                time.sleep(0.02)
            self.assertFalse(is_pid_alive(child_pid), "creation-time ownership must survive wrapper death")
            self.assertEqual(BuildSlotManager(run_dir=self.run_dir).status()["active_slots"], 0)
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
                wrapper.wait(timeout=5)
            if child_pid and is_pid_alive(child_pid):
                subprocess.run(["taskkill", "/PID", str(child_pid), "/T", "/F"],
                               timeout=10, creationflags=subprocess.CREATE_NO_WINDOW,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_classify_command_job_classes(self):
        """Commands classify as heavy, medium, light, or browser."""
        self.assertEqual(build_slot.classify_command(["npx", "next", "build"]), "heavy")
        self.assertEqual(build_slot.classify_command(["npm", "run", "build"]), "heavy")
        self.assertEqual(build_slot.classify_command(["bun", "run", "build"]), "heavy")
        self.assertEqual(build_slot.classify_command(["next", "build"]), "heavy")
        self.assertEqual(build_slot.classify_command("npx next build"), "heavy")

        self.assertEqual(build_slot.classify_command(["npx", "vitest", "run"]), "medium")
        self.assertEqual(build_slot.classify_command(["vitest"]), "medium")
        self.assertEqual(build_slot.classify_command(["npx", "playwright", "test"]), "browser")
        self.assertEqual(build_slot.classify_command(["pytest"]), "light")

        self.assertEqual(build_slot.classify_command(["echo", "hello"]), "light")
        self.assertEqual(build_slot.classify_command(["node", "scripts/clean.js"]), "light")

    def test_node_options_tuning_and_vitest_workers_injection(self):
        """run injects NODE_OPTIONS (heavy 3072, medium 1536) and VITEST_MAX_WORKERS=2 preserving user env."""
        dump_script = (
            "import json\n"
            "import os\n"
            "import sys\n"
            "out_file = sys.argv[1]\n"
            "payload = {\n"
            "    'NODE_OPTIONS': os.environ.get('NODE_OPTIONS', ''),\n"
            "    'VITEST_MAX_WORKERS': os.environ.get('VITEST_MAX_WORKERS', ''),\n"
            "}\n"
            "with open(out_file, 'w', encoding='utf-8') as f:\n"
            "    json.dump(payload, f)\n"
        )
        script_path = os.path.join(self.run_dir, "dump_env.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(dump_script)

        # 1. Heavy job defaults
        out_heavy = os.path.join(self.run_dir, "env_heavy.json")
        proc_heavy = subprocess.run(
            [sys.executable, os.path.abspath(build_slot.__file__), "--run-dir", self.run_dir,
             "run", "lane-heavy", "--class", "heavy", "--", sys.executable, script_path, out_heavy, "vitest"],
            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc_heavy.returncode, 0, proc_heavy.stderr)
        self.assertTrue(os.path.isfile(out_heavy), "Real child must write env output file")
        with open(out_heavy, "r", encoding="utf-8") as f:
            env_heavy = json.load(f)
        self.assertIn("--max-old-space-size=3072", env_heavy.get("NODE_OPTIONS", ""))
        self.assertEqual(env_heavy.get("VITEST_MAX_WORKERS"), "2")

        # 2. Medium job defaults
        out_med = os.path.join(self.run_dir, "env_med.json")
        proc_med = subprocess.run(
            [sys.executable, os.path.abspath(build_slot.__file__), "--run-dir", self.run_dir,
             "run", "lane-med", "--class", "medium", "--", sys.executable, script_path, out_med],
            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc_med.returncode, 0, proc_med.stderr)
        self.assertTrue(os.path.isfile(out_med), "Real child must write env output file")
        with open(out_med, "r", encoding="utf-8") as f:
            env_med = json.load(f)
        self.assertIn("--max-old-space-size=1536", env_med.get("NODE_OPTIONS", ""))

        # 3. Preserves existing values
        out_custom = os.path.join(self.run_dir, "env_custom.json")
        custom_env = os.environ.copy()
        custom_env["NODE_OPTIONS"] = "--max-old-space-size=4096"
        custom_env["VITEST_MAX_WORKERS"] = "8"
        proc_custom = subprocess.run(
            [sys.executable, os.path.abspath(build_slot.__file__), "--run-dir", self.run_dir,
             "run", "lane-custom", "--class", "heavy", "--", sys.executable, script_path, out_custom, "vitest"],
            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL, env=custom_env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc_custom.returncode, 0, proc_custom.stderr)
        self.assertTrue(os.path.isfile(out_custom), "Real child must write env output file")
        with open(out_custom, "r", encoding="utf-8") as f:
            env_custom = json.load(f)
        self.assertIn("--max-old-space-size=4096", env_custom.get("NODE_OPTIONS", ""))
        self.assertNotIn("3072", env_custom.get("NODE_OPTIONS", ""))
        self.assertEqual(env_custom.get("VITEST_MAX_WORKERS"), "8")
    def test_parse_args_supports_class_mem_gib_and_run_timeout(self):
        """parse_args accepts --class, --mem-gib, and --run-timeout (default 1800)."""
        args = build_slot.parse_args(
            ["run", "lane", "--class", "heavy", "--mem-gib", "4.0", "--run-timeout", "600", "--", "echo", "1"]
        )
        parsed_class = getattr(args, "job_class", getattr(args, "class_", getattr(args, "class", None)))
        self.assertEqual(parsed_class, "heavy")
        self.assertEqual(args.mem_gib, 4.0)
        self.assertEqual(args.run_timeout, 600.0)

        # Default run_timeout is 1800
        args_def = build_slot.parse_args(["run", "lane", "--", "echo", "1"])
        self.assertEqual(args_def.run_timeout, 1800.0)

    def test_status_reports_memory_budget_and_slot_fields(self):
        """status reports memory_budget breakdown and slot job_class and mem_gib fields."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "32.0"

        stat = manager.status()
        self.assertIn("memory_budget", stat)
        mb = stat["memory_budget"]
        self.assertIn("available_gib", mb)
        self.assertIn("reserved_gib", mb)
        self.assertIn("floor_gib", mb)
        self.assertIn("free_budget_gib", mb)
        self.assertEqual(mb["floor_gib"], 3.0)
        self.assertEqual(mb["available_gib"], 32.0)
        self.assertEqual(mb["reserved_gib"], 0.0)
        self.assertEqual(mb["free_budget_gib"], 29.0)

        # Acquire a slot with class and memory
        self.assertTrue(manager.acquire("lane-status-test", job_class="heavy", mem_gib=3.0, timeout=1.0))
        stat_held = manager.status()
        held_slot = next(s for s in stat_held["slots"] if s["owner"] == "lane-status-test")
        self.assertEqual(held_slot.get("job_class"), "heavy")
        self.assertEqual(held_slot.get("mem_gib"), 5.0)

        mb_held = stat_held["memory_budget"]
        self.assertEqual(mb_held["reserved_gib"], 5.0)
        self.assertEqual(mb_held["free_budget_gib"], 24.0)

        manager.release("lane-status-test")
if __name__ == "__main__":
    unittest.main()
