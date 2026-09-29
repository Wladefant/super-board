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

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test-build-slot-")
        self.run_dir = self.tmp.name
        self.orig_ram = os.environ.get("BUILD_SLOT_RAM_PERCENT")
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "80.0"
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

    def test_live_acquire_owner_never_reclaimed_on_age(self):
        """
        #315: an `acquire` lock whose owner is alive keeps the slot however old it is. The old
        30-minute age rule freed slots under builds that were still running.
        """
        manager = BuildSlotManager(run_dir=self.run_dir)
        os.makedirs(manager.lock_dir, exist_ok=True)
        past_epoch = time.time() - 3600.0
        info = {
            "owner": "long-build-lane",
            "pid": os.getpid(),
            "acquired_at": datetime.datetime.fromtimestamp(past_epoch, datetime.timezone.utc).isoformat(),
            "acquired_at_epoch": past_epoch,
            "heartbeat_at_epoch": past_epoch,
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

    def test_ram_guard_force_bypasses_at_96_percent(self):
        """At 96% RAM (>= 95%), --force bypasses the RAM guard and acquires."""
        manager = BuildSlotManager(run_dir=self.run_dir)
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

    def test_live_run_wrapper_with_stale_heartbeat_reclaimed(self):
        """A live wrapper whose heartbeat is older than 5 minutes has hung and is reclaimed."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        wrapper = self._live_process()
        self._write_run_lock(manager, wrapper.pid, wrapper.pid, age=600.0, hb_age=301.0)

        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertTrue(manager.check_stale_and_reclaim())
        self.assertFalse(os.path.isdir(manager.lock_dir))
        self.assertIn(f"run wrapper PID {wrapper.pid} is alive but its heartbeat is stale", stderr.getvalue())

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

        def read_then_lose_it(lock_dir, slot_idx):
            info = real_read(lock_dir, slot_idx)
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
        """Acquisition stagger delays second slot acquisition until stagger interval elapses."""
        manager = BuildSlotManager(run_dir=self.run_dir, acquisition_stagger=0.2)
        # Lane 1 acquires at t=0
        self.assertTrue(manager.acquire("stagger-lane-1", timeout=1.0, poll_interval=0.02))
        self.assertTrue(manager.is_held_by("stagger-lane-1"))

        # Lane 2 tries to acquire immediately with short timeout -> fails because stagger has not elapsed
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            res_2 = manager.acquire("stagger-lane-2", timeout=0.08, poll_interval=0.02)
        self.assertFalse(res_2)
        self.assertIn("stagger delay active", stderr_buf.getvalue())

        # After waiting for stagger to elapse, Lane 2 succeeds
        time.sleep(0.15)
        self.assertTrue(manager.acquire("stagger-lane-2", timeout=1.0, poll_interval=0.02))
        self.assertTrue(manager.is_held_by("stagger-lane-2"))

        # Status reflects stagger state
        st = manager.status()
        self.assertIn("stagger-lane-1", st["holders"])
        self.assertIn("stagger-lane-2", st["holders"])

        manager.release("stagger-lane-1")
        manager.release("stagger-lane-2")

    def test_acquisition_stagger_bypassed_with_force(self):
        """--force bypasses acquisition stagger delay."""
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

if __name__ == "__main__":
    unittest.main()
