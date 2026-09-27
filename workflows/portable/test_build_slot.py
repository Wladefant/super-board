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

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test-build-slot-")
        self.run_dir = self.tmp.name
        self.orig_ram = os.environ.get("BUILD_SLOT_RAM_PERCENT")
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "80.0"
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
        env["BUILD_SLOT_RAM_PERCENT"] = "80.0"
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
        )
        self.assertEqual(p_reacq.returncode, 0)
        self.assertIn("already held", p_reacq.stdout)

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
        )
        self.assertEqual(p.returncode, 2)
        self.assertIn("must be less than queue_stale_heartbeat_after", p.stderr)

    def test_scaled_heartbeat_lapse_re_enqueue_and_fifo_preserved(self):
        scaled_threshold = 1.0  # 1.0s stands in for 60s
        manager = BuildSlotManager(
            run_dir=self.run_dir,
            is_pid_alive_fn=lambda p: True,
            queue_stale_heartbeat_after=scaled_threshold,
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
        manager = BuildSlotManager(run_dir=self.run_dir)
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
        If build-slot-queue.lock is older than stale_after, _queue_atomic_lock
        must reclaim it even if the PID might appear alive.
        """
        queue_lock_dir = os.path.join(self.run_dir, build_slot.QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir, exist_ok=True)
        info_path = os.path.join(queue_lock_dir, build_slot.INFO_FILE_NAME)
        old_epoch = time.time() - 3600  # 1 hour ago
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump({
                "pid": os.getpid(),
                "acquired_at": "2026-09-27T00:00:00Z",
                "acquired_at_epoch": old_epoch,
            }, f)

        acquired = False
        with _queue_atomic_lock(self.run_dir, timeout=2.0, retry_interval=0.01, stale_after=5.0):
            acquired = True

        self.assertTrue(acquired)
        self.assertFalse(os.path.exists(queue_lock_dir))

    def test_acquire_survives_heartbeat_timeout(self):
        """
        In acquire(), a TimeoutError from heartbeat() due to lock contention
        must log a warning and retry on the next tick, not abort acquisition.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        manager.acquire("blocker-lane")  # Hold slot so waiter must wait and send heartbeats
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

    def test_stale_lock_dead_pid_within_grace_period_not_reclaimed(self):
        """
        When a lock was acquired recently (e.g. 0.5s ago) and the recorded PID dies
        (e.g. short-lived CLI wrapper or subshell), check_stale_and_reclaim must NOT
        reclaim the lock while it is within the 60s dead-PID grace period.
        """
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

        # check_stale_and_reclaim must NOT reclaim this lock
        reclaimed = manager.check_stale_and_reclaim()
        self.assertFalse(reclaimed)
        self.assertTrue(os.path.isdir(manager.lock_dir))

    def test_stale_lock_fresh_heartbeat_not_reclaimed(self):
        """
        If the owner PID is dead and lock age exceeds grace period, but the lock has
        a fresh heartbeat (<60s), check_stale_and_reclaim must NOT reclaim it.
        """
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
        self.assertFalse(reclaimed)
        self.assertTrue(os.path.isdir(manager.lock_dir))

        # But once heartbeat is older than 60s, it IS reclaimed
        info["heartbeat_at_epoch"] = now - 120.0
        with open(manager.info_file, "w", encoding="utf-8") as f:
            json.dump(info, f)

        reclaimed = manager.check_stale_and_reclaim()
        self.assertTrue(reclaimed)
        self.assertFalse(os.path.exists(manager.lock_dir))

    def test_find_long_lived_owner_pid_returns_valid_pid(self):
        """
        find_long_lived_owner_pid must return a valid positive PID
        (either veyyon process or parent process).
        """
        pid = build_slot.find_long_lived_owner_pid()
        self.assertIsInstance(pid, int)
        self.assertGreater(pid, 0)
        self.assertTrue(is_pid_alive(pid))

    def test_enqueue_priority_inserts_at_front(self):
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: True)
        manager.enqueue("waiter-1", 1001, token="tok-1")
        manager.enqueue("waiter-2", 1002, token="tok-2")
        manager.enqueue("waiter-3", 1003, token="tok-3")

        # waiter-4 is enqueued with priority=True -> must land at index 0
        idx = manager.enqueue("waiter-4", 1004, token="tok-4", priority=True)
        self.assertEqual(idx, 0)
        queue = manager._read_queue()
        self.assertEqual([x["name"] for x in queue], ["waiter-4", "waiter-1", "waiter-2", "waiter-3"])
        self.assertTrue(queue[0].get("priority"))

        # waiter-2 is re-enqueued with priority=True -> promoted to index 0
        idx2 = manager.enqueue("waiter-2", 1002, token="tok-2", priority=True)
        self.assertEqual(idx2, 0)
        queue2 = manager._read_queue()
        self.assertEqual([x["name"] for x in queue2], ["waiter-2", "waiter-4", "waiter-1", "waiter-3"])

    def test_bump_moves_to_front(self):
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: True)
        manager.enqueue("lane-A", 2001, token="tok-A")
        manager.enqueue("lane-B", 2002, token="tok-B")
        manager.enqueue("lane-C", 2003, token="tok-C")

        # bump lane-C to front
        ok = manager.bump("lane-C")
        self.assertTrue(ok)
        queue = manager._read_queue()
        self.assertEqual([x["name"] for x in queue], ["lane-C", "lane-A", "lane-B"])
        self.assertTrue(queue[0].get("priority"))

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
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(is_pid_alive(p.pid))
        return p.pid

    def _live_process(self) -> subprocess.Popen:
        """A real process that stays alive for the test; killed on cleanup."""
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.assertTrue(is_pid_alive(p.pid))
        return p

    def _write_run_lock(self, manager, wrapper_pid, child_pid, age, hb_age):
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
        self._write_run_lock(manager, wrapper.pid, self._exited_pid(), age=600.0, hb_age=300.0)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            reclaimed = manager.check_stale_and_reclaim()
        self.assertFalse(reclaimed, stderr.getvalue())
        self.assertTrue(os.path.isdir(manager.lock_dir))

    def test_live_run_wrapper_without_heartbeat_past_stale_after_reclaimed(self):
        """A live but hung wrapper (no heartbeat for longer than stale_after) is still reclaimed."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        wrapper = self._live_process()
        self._write_run_lock(manager, wrapper.pid, wrapper.pid, age=2000.0, hb_age=1900.0)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertTrue(manager.check_stale_and_reclaim(stale_after=1800.0))
        self.assertIn(f"run wrapper PID {wrapper.pid} is alive", stderr.getvalue())

    def test_dead_run_wrapper_reclaimed_only_after_grace_period(self):
        """
        A dead wrapper frees the slot once the lock is older than the 60s grace period and
        its heartbeat has lapsed, even if the wrapped command it left behind is still alive.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        dead_wrapper = self._exited_pid()
        orphan_child = self._live_process()

        self._write_run_lock(manager, dead_wrapper, orphan_child.pid, age=30.0, hb_age=30.0)
        self.assertFalse(manager.check_stale_and_reclaim())
        self.assertTrue(os.path.isdir(manager.lock_dir))
        shutil.rmtree(manager.lock_dir)

        self._write_run_lock(manager, dead_wrapper, orphan_child.pid, age=120.0, hb_age=90.0)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertTrue(manager.check_stale_and_reclaim())
        self.assertFalse(os.path.isdir(manager.lock_dir))
        self.assertIn(f"run wrapper PID {dead_wrapper} is dead", stderr.getvalue())

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
            capture_output=True, text=True, check=True,
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

    def test_heartbeat_rewrite_never_reads_as_corrupt(self):
        """
        #315: waiters check the lock while its holder heartbeats. An in-place rewrite of
        info.json let a waiter read the empty file, call the live lock corrupt and reclaim
        it (QA5748 lost its slot this way while its driver was running).
        """
        manager = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
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

    def test_bounded_concurrency_two_slots_when_ram_under_75_percent(self):
        """When host RAM is < 75%, 2 concurrent slots can be acquired by distinct lanes."""
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "70.0"
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertEqual(manager.get_max_slots(70.0), 2)

        # 1. Lane A acquires
        self.assertTrue(manager.acquire("lane-a", timeout=1.0))
        self.assertTrue(manager.is_held_by("lane-a"))

        # 2. Lane B acquires concurrently
        self.assertTrue(manager.acquire("lane-b", timeout=1.0))
        self.assertTrue(manager.is_held_by("lane-b"))

        # 3. Third lane C is blocked because both slots are held
        res_c = manager.acquire("lane-c", timeout=0.05, poll_interval=0.02)
        self.assertFalse(res_c)

        # Verify status exposes both holders
        st = manager.status()
        self.assertEqual(st["max_slots"], 2)
        self.assertEqual(st["active_slots"], 2)
        self.assertIn("lane-a", st["holders"])
        self.assertIn("lane-b", st["holders"])

        # 4. Release lane A, then lane C can acquire
        self.assertTrue(manager.release("lane-a"))
        self.assertFalse(manager.is_held_by("lane-a"))
        self.assertTrue(manager.is_held_by("lane-b"))

        self.assertTrue(manager.acquire("lane-c", timeout=1.0))
        self.assertTrue(manager.is_held_by("lane-c"))

        self.assertTrue(manager.release("lane-b"))
        self.assertTrue(manager.release("lane-c"))
        st_end = manager.status()
        self.assertEqual(st_end["active_slots"], 0)

    def test_bounded_concurrency_one_slot_when_ram_at_or_above_75_percent(self):
        """When host RAM is >= 75%, concurrency is bounded to 1 slot."""
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "80.0"
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertEqual(manager.get_max_slots(80.0), 1)

        # 1. Lane 1 acquires
        self.assertTrue(manager.acquire("lane-1", timeout=1.0))
        self.assertTrue(manager.is_held_by("lane-1"))

        # 2. Lane 2 is blocked because capacity is 1
        res_2 = manager.acquire("lane-2", timeout=0.05, poll_interval=0.02)
        self.assertFalse(res_2)

        # Release lane 1, then lane 2 can acquire
        self.assertTrue(manager.release("lane-1"))
        self.assertTrue(manager.acquire("lane-2", timeout=1.0))
        self.assertTrue(manager.release("lane-2"))

    def test_status_shows_both_holders_and_capacity(self):
        """status() and format_status_human() format and report all slots."""
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "65.0"
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(manager.acquire("slot-holder-0", timeout=1.0))
        self.assertTrue(manager.acquire("slot-holder-1", timeout=1.0))

        st = manager.status()
        self.assertEqual(st["max_slots"], 2)
        self.assertEqual(st["active_slots"], 2)
        self.assertEqual(set(st["holders"]), {"slot-holder-0", "slot-holder-1"})

        human = build_slot.format_status_human(st)
        self.assertIn("Capacity:    2 slot(s) allowed", human)
        self.assertIn("LOCKED (2/2 in use)", human)
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

    def test_get_max_slots_thresholds_and_override(self):
        """get_max_slots correctly resolves tiers and respects overrides."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertEqual(manager.get_max_slots(50.0), 2)
        self.assertEqual(manager.get_max_slots(74.9), 2)
        self.assertEqual(manager.get_max_slots(75.0), 1)
        self.assertEqual(manager.get_max_slots(84.0), 1)

        override_mgr = BuildSlotManager(run_dir=self.run_dir, max_slots=1)
        self.assertEqual(override_mgr.get_max_slots(50.0), 1)

        override_mgr_2 = BuildSlotManager(run_dir=self.run_dir, max_slots=2)
        self.assertEqual(override_mgr_2.get_max_slots(80.0), 2)

if __name__ == "__main__":
    unittest.main()
