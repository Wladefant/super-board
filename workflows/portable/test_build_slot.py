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
  5. RAM guard refusal (>=85%) and force override.
  6. FIFO queue ordering and clean queue logic.
  7. Concurrent subprocess FIFO serialization.
  8. CLI subprocess status, JSON, acquire, and release.
"""

from __future__ import annotations

import datetime
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from contextlib import redirect_stderr, redirect_stdout

# Ensure workflows/portable is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import build_slot
from build_slot import BuildSlotManager, is_pid_alive


class TestBuildSlot(unittest.TestCase):
    """Unit tests for the BuildSlotManager lock arbiter."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test-build-slot-")
        self.run_dir = self.tmp.name
        self.orig_ram = os.environ.get("BUILD_SLOT_RAM_PERCENT")
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "70.0"

    def tearDown(self):
        if self.orig_ram is not None:
            os.environ["BUILD_SLOT_RAM_PERCENT"] = self.orig_ram
        else:
            os.environ.pop("BUILD_SLOT_RAM_PERCENT", None)
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
            stale_after=60.0,
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
        now = time.time()
        info = {
            "owner": "dead-lane",
            "pid": dead_pid,
            "acquired_at": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
            "acquired_at_epoch": now,
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

    def test_stale_reclaim_age_exceeded(self):
        manager = BuildSlotManager(run_dir=self.run_dir)

        # Create lock with current PID but timestamp 3600 seconds in the past
        os.makedirs(manager.lock_dir, exist_ok=True)
        past_epoch = time.time() - 3600.0
        info = {
            "owner": "ancient-lane",
            "pid": os.getpid(),
            "acquired_at": datetime.datetime.fromtimestamp(past_epoch, datetime.timezone.utc).isoformat(),
            "acquired_at_epoch": past_epoch,
        }
        with open(manager.info_file, "w", encoding="utf-8") as f:
            json.dump(info, f)

        # Check reclaim with stale_after = 60s
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            reclaimed = manager.check_stale_and_reclaim(stale_after=60.0)
        self.assertTrue(reclaimed)
        self.assertFalse(os.path.exists(manager.lock_dir))

        captured = stderr_buf.getvalue()
        self.assertIn("[NOTICE] Reclaiming stale build slot lock", captured)
        self.assertIn("exceeded stale-after threshold", captured)

    def test_ram_guard_refusal_and_force(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "90.0"

        # Attempt acquire without force should fail
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            acquired = manager.acquire("high-ram-lane", timeout=0.5, poll_interval=0.05)

        self.assertFalse(acquired)
        self.assertIn("RAM guard: acquisition refused", stderr_buf.getvalue())
        self.assertIn("90.0%", stderr_buf.getvalue())

        # Attempt acquire with force=True should succeed
        with redirect_stderr(stderr_buf):
            acquired_force = manager.acquire(
                "force-lane", timeout=0.5, poll_interval=0.05, force=True
            )
        self.assertTrue(acquired_force)
        self.assertEqual(manager.status()["lock"]["owner"], "force-lane")
        self.assertTrue(manager.release("force-lane"))

    def test_fifo_queue_order(self):
        active_pids = {1001: True, 1002: True, 1003: True, 1004: True}
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: active_pids.get(p, False))

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
        env["BUILD_SLOT_RAM_PERCENT"] = "70.0"

        # 1. Status: FREE
        p_stat = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "status"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(p_stat.returncode, 0)
        self.assertIn("Status:      FREE", p_stat.stdout)

        # 2. Status with --json
        p_json = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "status", "--json"],
            capture_output=True,
            text=True,
            env=env,
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
        )
        self.assertEqual(p_acq.returncode, 0)

        # 4. Status should show locked
        p_stat2 = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "status"],
            capture_output=True,
            text=True,
            env=env,
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
        )
        self.assertEqual(p_rel.returncode, 0)

        # 6. Status back to FREE
        p_stat3 = subprocess.run(
            [sys.executable, script, "--run-dir", self.run_dir, "status"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(p_stat3.returncode, 0)
        self.assertIn("Status:      FREE", p_stat3.stdout)

    def test_concurrent_subprocesses_fifo(self):
        script = os.path.abspath(build_slot.__file__)
        record_file = os.path.join(self.run_dir, "acquisition_order.txt")
        env = dict(os.environ)
        env["BUILD_SLOT_RAM_PERCENT"] = "70.0"

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
        procs = []
        for i in range(1, 4):
            w_name = f"subproc-{i}"
            p = subprocess.Popen(
                [sys.executable, "-c", worker_code, w_name, script, self.run_dir, record_file],
                env=env,
            )
            procs.append(p)
            time.sleep(0.04)

        for p in procs:
            ret = p.wait()
            self.assertEqual(ret, 0, "A concurrent worker subprocess failed")

        with open(record_file, "r", encoding="utf-8") as f:
            lines = [line.strip().split(":")[0] for line in f if line.strip()]

        self.assertEqual(lines, ["subproc-1", "subproc-2", "subproc-3"])

    def test_queue_reclaim_heartbeat_and_negative_control(self):
        # Living PIDs: is_pid_alive returns True
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: True)
        now = time.time()

        # Seed queue with:
        # 1. Stale heartbeat entry under living PID (heartbeat 65s ago) -> must be reclaimed
        # 2. Fresh heartbeat entry under living PID (heartbeat 5s ago) -> must NOT be reclaimed (negative control)
        # 3. Legacy entry without heartbeat_at older than 30m (enqueued 1850s ago) -> must be reclaimed
        # 4. Legacy entry without heartbeat_at newer than 30m (enqueued 300s ago) -> must NOT be reclaimed
        seeded_queue = [
            {
                "name": "stale-hb-lane",
                "pid": 2001,
                "token": "tok-stale",
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
        ]
        manager._write_queue(seeded_queue)

        # Run clean_queue
        cleaned = manager.clean_queue()
        names = [x["name"] for x in cleaned]

        # Stale heartbeat reclaimed
        self.assertNotIn("stale-hb-lane", names)
        # Fresh heartbeat preserved (negative control!)
        self.assertIn("fresh-hb-lane", names)
        # Legacy stale (> 30m) reclaimed
        self.assertNotIn("legacy-stale-lane", names)
        # Legacy fresh (<= 30m) preserved
        self.assertIn("legacy-fresh-lane", names)

        self.assertEqual(names, ["fresh-hb-lane", "legacy-fresh-lane"])

    def test_acquire_timeout_leaves_no_entry_behind(self):
        manager = BuildSlotManager(run_dir=self.run_dir)

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
        manager = BuildSlotManager(run_dir=self.run_dir)
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
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: True)

        now = time.time()
        # Simulate lane-1 and lane-2 enqueued by Veyyon Main (same PID 35296)
        lane1_token = "tok-lane-1"
        lane2_token = "tok-lane-2"
        seeded_queue = [
            {
                "name": "lane-1",
                "pid": main_pid,
                "token": lane1_token,
                "enqueued_at": now - 80.0,
                "enqueued_at_iso": datetime.datetime.fromtimestamp(now - 80.0, datetime.timezone.utc).isoformat(),
                "heartbeat_at": now - 70.0,  # Dead waiter: heartbeat stopped 70s ago
                "heartbeat_at_iso": datetime.datetime.fromtimestamp(now - 70.0, datetime.timezone.utc).isoformat(),
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

        # Before reclaim: lane-1 is at head
        q_before = manager._read_queue()
        self.assertEqual([x["name"] for x in q_before], ["lane-1", "lane-2"])

        # Reclaim runs: lane-1 is pruned despite living PID 35296; lane-2 is kept
        q_after = manager.clean_queue()
        self.assertEqual([x["name"] for x in q_after], ["lane-2"])

        # lane-2 can now acquire the lock
        self.assertTrue(manager.acquire("lane-2", pid=main_pid, token=lane2_token, timeout=1.0, poll_interval=0.02))
        self.assertTrue(manager.is_held_by("lane-2", pid=main_pid))
        self.assertTrue(manager.release("lane-2"))
        self.assertEqual(manager.clean_queue(), [])

    def test_acquire_loop_writes_heartbeat(self):
        manager = BuildSlotManager(run_dir=self.run_dir)
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

    def test_poll_interval_validation(self):
        manager = BuildSlotManager(run_dir=self.run_dir, queue_stale_heartbeat_after=10.0)
        # poll_interval >= queue_stale_heartbeat_after must raise ValueError
        with self.assertRaises(ValueError) as ctx:
            manager.acquire("test-lane", poll_interval=10.0)
        self.assertIn("must be less than queue_stale_heartbeat_after", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx2:
            manager.acquire("test-lane", poll_interval=15.0)
        self.assertIn("must be less than queue_stale_heartbeat_after", str(ctx2.exception))

    def test_scaled_heartbeat_lapse_re_enqueue_and_fifo_preserved(self):
        scaled_threshold = 1.0  # 1.0s stands in for 60s
        manager = BuildSlotManager(
            run_dir=self.run_dir,
            is_pid_alive_fn=lambda p: True,
            queue_stale_heartbeat_after=scaled_threshold,
        )

        # Blocker holds the lock
        self.assertTrue(manager.acquire("blocker", timeout=1.0, poll_interval=0.05))

        token_a = "tok-waiter-a"
        token_b = "tok-waiter-b"
        now = time.time()

        # Seed Waiter A in queue with a heartbeat that is already older than the scaled threshold (1.2s ago)
        # This simulates a waiter that stalled or experienced a long sleep
        seeded_queue = [
            {
                "name": "waiter-a",
                "pid": 2001,
                "token": token_a,
                "enqueued_at": now - 2.0,
                "enqueued_at_iso": datetime.datetime.fromtimestamp(now - 2.0, datetime.timezone.utc).isoformat(),
                "heartbeat_at": now - 1.2,
                "heartbeat_at_iso": datetime.datetime.fromtimestamp(now - 1.2, datetime.timezone.utc).isoformat(),
            }
        ]
        manager._write_queue(seeded_queue)

        # Because heartbeat runs BEFORE clean_queue in acquire(), polling waiter-a updates its
        # heartbeat first, preventing waiter-a from being pruned by its own loop!
        # Test this by running a short acquire for waiter-a
        res_a = manager.acquire("waiter-a", pid=2001, token=token_a, timeout=0.08, poll_interval=0.02, heartbeat_interval=0.01)
        self.assertFalse(res_a)  # blocker still holds lock

        # Confirm waiter-a's entry was NOT pruned: it was kept and its heartbeat updated!
        q = manager._read_queue()
        # waiter-a is dequeued on exit of acquire by try/finally
        # Now test the case where waiter-a was pruned by an external process while sleeping:
        manager.enqueue("waiter-a", pid=2001, token=token_a)
        # External process prunes the queue:
        manager._write_queue([])
        self.assertEqual(manager._read_queue(), [])

        # Waiter A wakes up: heartbeat() returns False because entry is missing.
        # acquire() detects False and immediately re-enqueues waiter-a with token_a!
        # Enqueue waiter-b after waiter-a to verify FIFO ordering is preserved
        import threading
        events = []

        def waiter_a_thread():
            # Waiter A tries to acquire with timeout=1.5s
            # Its entry was missing, so acquire will re-enqueue it with token_a
            ok = manager.acquire("waiter-a", pid=2001, token=token_a, timeout=1.5, poll_interval=0.02, heartbeat_interval=0.02)
            if ok:
                events.append("waiter-a")
                manager.release("waiter-a")

        def waiter_b_thread():
            # Waiter B arrives shortly after Waiter A has re-enqueued
            time.sleep(0.06)
            ok = manager.acquire("waiter-b", pid=2002, token=token_b, timeout=1.5, poll_interval=0.02, heartbeat_interval=0.02)
            if ok:
                events.append("waiter-b")
                manager.release("waiter-b")

        t_a = threading.Thread(target=waiter_a_thread)
        t_b = threading.Thread(target=waiter_b_thread)

        t_a.start()
        t_b.start()

        # Let waiter A re-enqueue and waiter B enqueue behind it
        time.sleep(0.12)
        q_order = [x["name"] for x in manager.clean_queue()]
        self.assertEqual(q_order, ["waiter-a", "waiter-b"], "Waiter A must be ahead of later arrival Waiter B")

        # Now blocker releases lock
        self.assertTrue(manager.release("blocker"))

        t_a.join()
        t_b.join()

        # Both acquired and released; Waiter A acquired FIRST!
        self.assertEqual(events, ["waiter-a", "waiter-b"], "Waiter A must acquire before later arrival Waiter B")
if __name__ == "__main__":
    unittest.main()
