#!/usr/bin/env python3
"""
Regression and contract tests for build slot resource-aware scheduler:
- Resource refusal: explicit heavy cap and memory budget numbers
- Queue admission: smaller backfill with preserved head timestamp
- Aging pause: pauses backfill only when heavy head aged >1200s, projected max >= head memory, heavy_jobs == 0
- Impossible head: projected max < head memory keeps queue moving with needs/max log
- Stale stagger snapshot race: stagger rechecked within slot guard
- Staleness contract: _is_entry_stale expires heartbeats after 60s regardless of live PID; fallback 1800s for missing heartbeat
"""

import datetime
import io
from contextlib import redirect_stderr
import json
import logging
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock
import sys
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
import build_slot
from build_slot import (
    BuildSlotManager,
    MEMORY_FLOOR_GIB,
    MEMORY_RESERVATIONS,
    MEMORY_RAMP_SECONDS,
    _reservation_gib,
    _transition_guard,
)


class TestBuildSlotAdmissionAndResourceRefusal(unittest.TestCase):
    """Test manager._resource_refusal and manager._queue_admission contracts."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="build-slot-admit-")
        self.run_dir = os.path.join(self.test_dir, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        self.manager = BuildSlotManager(run_dir=self.run_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Resource refusal: heavy cap and budget numbers
    # -------------------------------------------------------------------------

    def test_resource_refusal_heavy_job_cap_refuses_when_heavy_held(self):
        """When at or above 85% RAM (or two heavy jobs held below 85%), heavy is refused under heavy cap."""
        budget = {
            "available_gib": 16.0,
            "reserved_gib": 3.0,
            "floor_gib": 3.0,
            "free_budget_gib": 10.0,
            "heavy_jobs": 1,
        }
        with mock.patch("build_slot.get_system_ram_percent", return_value=88.0):
            refusal = self.manager._resource_refusal("heavy", 3.0, budget)
        self.assertIsNotNone(refusal, "Expected heavy job to be refused when heavy_jobs >= 1 at high RAM")
        self.assertIn("heavy", refusal.lower())

        # Low-RAM two-holder fixture:
        budget_two = dict(budget, heavy_jobs=2)
        with mock.patch("build_slot.get_system_ram_percent", return_value=50.0):
            refusal_two = self.manager._resource_refusal("heavy", 3.0, budget_two)
        self.assertIsNotNone(refusal_two, "Expected heavy job to be refused when 2 heavy jobs held below 85% RAM")
        self.assertIn("heavy", refusal_two.lower())
    def test_resource_refusal_heavy_job_accepted_when_no_heavy_held_and_budget_sufficient(self):
        """When heavy_jobs == 0 and free_budget_gib >= mem_gib, job_class='heavy' is admitted."""
        budget = {
            "available_gib": 10.0,
            "reserved_gib": 1.5,
            "floor_gib": 3.0,
            "free_budget_gib": 5.5,
            "heavy_jobs": 0,
        }
        refusal = self.manager._resource_refusal("heavy", 3.0, budget)
        self.assertIsNone(refusal, "Expected heavy job to be accepted with sufficient budget and no heavy held")

    def test_resource_refusal_budget_free_memory_breach(self):
        """When free_budget_gib < mem_gib, refusal is returned for any job class."""
        budget = {
            "available_gib": 4.0,
            "reserved_gib": 0.5,
            "floor_gib": 3.0,
            "free_budget_gib": 0.5,
            "heavy_jobs": 0,
        }
        # Light fits: 0.5 <= 0.5
        self.assertIsNone(self.manager._resource_refusal("light", 0.5, budget))

        # Medium breaches: 1.5 > 0.5
        refusal_med = self.manager._resource_refusal("medium", 1.5, budget)
        self.assertIsNotNone(refusal_med)
        self.assertTrue("budget" in refusal_med.lower() or "memory" in refusal_med.lower() or "floor" in refusal_med.lower())

        # Heavy breaches: 3.0 > 0.5
        refusal_heavy = self.manager._resource_refusal("heavy", 3.0, budget)
        self.assertIsNotNone(refusal_heavy)

    def test_resource_refusal_telemetry_missing_blocks_admission(self):
        """Unknown or missing RAM telemetry (None) blocks admission."""
        budget = {
            "available_gib": None,
            "reserved_gib": 0.0,
            "floor_gib": 3.0,
            "free_budget_gib": None,
            "heavy_jobs": 0,
        }
        self.assertIsNotNone(self.manager._resource_refusal("light", 0.5, budget))
        self.assertIsNotNone(self.manager._resource_refusal("medium", 1.5, budget))
        self.assertIsNotNone(self.manager._resource_refusal("heavy", 3.0, budget))

    def test_resource_refusal_non_heavy_ignores_heavy_job_cap(self):
        """Medium and light jobs are not blocked by heavy cap as long as free_budget_gib >= mem_gib."""
        budget = {
            "available_gib": 10.0,
            "reserved_gib": 3.0,
            "floor_gib": 3.0,
            "free_budget_gib": 4.0,
            "heavy_jobs": 1,
        }
        # Heavy is refused under high RAM (or when cap reached)
        with mock.patch("build_slot.get_system_ram_percent", return_value=88.0):
            self.assertIsNotNone(self.manager._resource_refusal("heavy", 3.0, budget))
        # Medium and light are admitted
        self.assertIsNone(self.manager._resource_refusal("medium", 1.5, budget))
        self.assertIsNone(self.manager._resource_refusal("light", 0.5, budget))
    # -------------------------------------------------------------------------
    # 2. Queue admission: backfill and head preservation
    # -------------------------------------------------------------------------

    def test_queue_admission_smaller_backfill_with_preserved_head(self):
        """Smaller jobs backfill a resource-blocked head while preserving head queue entry and timestamp."""
        now = time.time()
        head_enqueued_at = now - 50.0
        queue = [
            {
                "name": "heavy-head",
                "token": "tok-head",
                "pid": 1001,
                "job_class": "heavy",
                "mem_gib": 3.0,
                "enqueued_at": head_enqueued_at,
            },
            {
                "name": "light-waiter",
                "token": "tok-light",
                "pid": 1002,
                "job_class": "light",
                "mem_gib": 0.5,
                "enqueued_at": now - 30.0,
            },
        ]
        budget = {
            "available_gib": 5.0,
            "reserved_gib": 1.0,
            "floor_gib": 3.0,
            "free_budget_gib": 1.0,  # 1.0 GiB free: insufficient for 3.0 heavy head, sufficient for 0.5 light
            "heavy_jobs": 0,
        }

        # Head is NOT eligible
        head_eligible, head_reason = self.manager._queue_admission(queue, "tok-head", budget, now)
        self.assertFalse(head_eligible)

        # Light waiter IS eligible (backfilling)
        light_eligible, light_reason = self.manager._queue_admission(queue, "tok-light", budget, now)
        self.assertTrue(light_eligible, f"Expected light waiter to backfill; reason={light_reason}")

        # Queue head remains untouched: order and timestamp preserved
        self.assertEqual(queue[0]["token"], "tok-head")
        self.assertEqual(queue[0]["enqueued_at"], head_enqueued_at)

    # -------------------------------------------------------------------------
    # 3. Aging pauses backfill only when possible and no heavy held
    # -------------------------------------------------------------------------

    def test_queue_admission_aging_pauses_backfill_when_possible_and_no_heavy_held(self):
        """
        Aging pauses backfill only when:
        1. heavy head aged > 1200s
        2. three poll samples have projected max >= head memory + 0.5 GiB
        3. heavy_jobs == 0
        """
        now = time.time()
        queue = [
            {
                "name": "aged-heavy-head",
                "token": "tok-aged-head",
                "pid": 2001,
                "job_class": "heavy",
                "mem_gib": 3.0,
                "enqueued_at": now - 1205.0,  # aged > 1200s
            },
            {
                "name": "light-waiter",
                "token": "tok-light",
                "pid": 2002,
                "job_class": "light",
                "mem_gib": 0.5,
                "enqueued_at": now - 100.0,
            },
        ]
        # projected_max = 4.0 + 4.0 - 3.0 = 5.0 GiB: the effective heavy minimum.
        # heavy_jobs == 0
        budget = {
            "available_gib": 4.0,
            "reserved_gib": 4.0,
            "floor_gib": 3.0,
            "free_budget_gib": 0.5,  # light would physically fit, BUT aging pause must block backfill
            "heavy_jobs": 0,
        }

        for sample in range(3):
            eligible, reason = self.manager._queue_admission(queue, "tok-light", budget, now + sample * 5)
        self.assertFalse(eligible, "Stable fit confidence must pause aging backfill when no heavy job is held")
        self.assertIsNotNone(reason)
        self.assertTrue("aging" in reason.lower() or "paused" in reason.lower() or "starv" in reason.lower())

    def test_heavy_head_eventually_runs_with_181_second_medium_backfills(self):
        head = {"name": "heavy-head", "token": "head", "job_class": "heavy",
                "mem_gib": 5.0, "enqueued_at": 1000.0}
        expiry = [1261]
        admitted = []
        head_age = None
        for age in range(1201, 86401):
            expiry = [end for end in expiry if end > age]
            reserved = 1.5 * len(expiry)
            budget = {"available_gib": 9.0, "reserved_gib": reserved,
                      "floor_gib": 3.0, "free_budget_gib": 6.0 - reserved,
                      "heavy_jobs": 0}
            medium = {"name": "medium", "token": "medium", "job_class": "medium",
                      "mem_gib": 1.5, "enqueued_at": 1000.0 + age}
            queue = [head, medium]
            if self.manager._queue_admission(queue, "head", budget, 1000.0 + age)[0]:
                head_age = age
                break
            if age >= 1260 and (age - 1260) % 180 == 0:
                if self.manager._queue_admission(queue, "medium", budget, 1000.0 + age)[0]:
                    self.assertGreaterEqual(budget["free_budget_gib"], 1.5)
                    expiry.append(age + 181)
                    admitted.append(age)
        self.assertIsNotNone(head_age, "5 GiB head starved through age 86400s")
        self.assertLessEqual(head_age, 2400 + 181)
        self.assertTrue(admitted, "The replay must exercise backfill before the age cap")

    def test_aged_head_drain_windows_are_bounded(self):
        queue = [
            {"name": "head", "token": "head", "job_class": "heavy",
             "mem_gib": 3.0, "enqueued_at": 1000.0},
            {"name": "light", "token": "light", "job_class": "light",
             "mem_gib": 0.5, "enqueued_at": 1100.0},
        ]
        budget = {"available_gib": 6.5, "reserved_gib": 1.5,
                  "floor_gib": 3.0, "free_budget_gib": 2.0, "heavy_jobs": 0}
        for age in (1205, 1210):
            self.assertTrue(self.manager._queue_admission(queue, "light", budget, 1000.0 + age)[0])
        for age, expected in [(1215, False), (1259, False), (1260, True),
                              (1379, True), (1380, False), (1440, True),
                              (2399, True), (2400, False), (3060, False)]:
            with self.subTest(age=age):
                allowed, reason = self.manager._queue_admission(
                    queue, "light", budget, 1000.0 + age)
                self.assertEqual(allowed, expected, reason)
        self.assertEqual(queue[0]["enqueued_at"], 1000.0)
        budget.update(available_gib=8.0, reserved_gib=0.0, free_budget_gib=5.0)
        self.assertTrue(self.manager._queue_admission(queue, "head", budget, 2300.0)[0])
        self.assertFalse(self.manager._queue_admission(queue, "light", budget, 2300.0)[0])

    def test_queue_admission_aging_does_not_pause_backfill_when_heavy_job_already_held(self):
        """When heavy_jobs >= 1, aging does not pause backfill for smaller jobs because heavy head cannot run anyway."""
        now = time.time()
        queue = [
            {
                "name": "aged-heavy-head",
                "token": "tok-aged-head",
                "pid": 2001,
                "job_class": "heavy",
                "mem_gib": 3.0,
                "enqueued_at": now - 1250.0,  # aged > 1200s
            },
            {
                "name": "light-waiter",
                "token": "tok-light",
                "pid": 2002,
                "job_class": "light",
                "mem_gib": 0.5,
                "enqueued_at": now - 100.0,
            },
        ]
        # heavy_jobs == 1: already running a heavy job
        budget = {
            "available_gib": 7.0,
            "reserved_gib": 3.0,
            "floor_gib": 3.0,
            "free_budget_gib": 1.0,
            "heavy_jobs": 1,
        }

        eligible, reason = self.manager._queue_admission(queue, "tok-light", budget, now)
        self.assertTrue(eligible, "Aging must NOT pause backfill when heavy_jobs >= 1")

    def test_queue_admission_aging_does_not_pause_backfill_when_not_yet_aged(self):
        """When heavy head has aged <= 1200s, aging does not pause backfill."""
        now = time.time()
        queue = [
            {
                "name": "young-heavy-head",
                "token": "tok-head",
                "pid": 2001,
                "job_class": "heavy",
                "mem_gib": 3.0,
                "enqueued_at": now - 600.0,  # 600s <= 1200s
            },
            {
                "name": "light-waiter",
                "token": "tok-light",
                "pid": 2002,
                "job_class": "light",
                "mem_gib": 0.5,
                "enqueued_at": now - 100.0,
            },
        ]
        budget = {
            "available_gib": 4.0,
            "reserved_gib": 2.0,
            "floor_gib": 3.0,
            "free_budget_gib": 0.5,
            "heavy_jobs": 0,
        }

        eligible, reason = self.manager._queue_admission(queue, "tok-light", budget, now)
        self.assertTrue(eligible, "Aging must NOT pause backfill when head has not exceeded 1200s aging threshold")

    # -------------------------------------------------------------------------
    # 4. Impossible head keeps queue moving with needs/max log
    # -------------------------------------------------------------------------

    def test_queue_admission_impossible_head_keeps_queue_moving_with_needs_max_log(self):
        """
        When projected max < head memory (impossible head), aging pause does NOT trigger;
        smaller jobs are admitted and the impossible head state logs needs vs max.
        """
        now = time.time()
        queue = [
            {
                "name": "impossible-giant-head",
                "token": "tok-giant",
                "pid": 3001,
                "job_class": "heavy",
                "mem_gib": 8.0,  # Head demands 8.0 GiB
                "enqueued_at": now - 1300.0,  # Aged > 1200s
            },
            {
                "name": "light-waiter",
                "token": "tok-light",
                "pid": 3002,
                "job_class": "light",
                "mem_gib": 0.5,
                "enqueued_at": now - 50.0,
            },
        ]
        # projected_max = available (3.5) + reserved (1.5) - floor (3.0) = 2.0 GiB
        # projected_max (2.0) < head memory (8.0): IMPOSSIBLE HEAD!
        budget = {
            "available_gib": 3.5,
            "reserved_gib": 1.5,
            "floor_gib": 3.0,
            "free_budget_gib": 0.8,
            "heavy_jobs": 0,
        }

        with self.assertLogs("build_slot", level="INFO") as log_capture:
            eligible, reason = self.manager._queue_admission(queue, "tok-light", budget, now)

        self.assertTrue(eligible, "Queue must keep moving for smaller jobs when head memory > projected max")
        # Verify needs vs max logged
        all_logs = " ".join(log_capture.output).lower()
        self.assertTrue(
            ("8" in all_logs and "2" in all_logs) or "projected" in all_logs or "impossible" in all_logs or "max" in all_logs,
            f"Expected needs/max in logs, got: {log_capture.output}",
        )
@mock.patch("build_slot.get_system_ram_percent", return_value=50.0)
@mock.patch("build_slot.get_available_ram_gib", return_value=16.0)


class TestBuildSlotStaleStaggerSnapshotRace(unittest.TestCase):
    """Test race condition where stagger snapshot was stale outside slot guard."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="build-slot-stagger-")
        self.run_dir = os.path.join(self.test_dir, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        self.manager = BuildSlotManager(run_dir=self.run_dir, acquisition_stagger=45.0, max_slots=2)

        self.clock = [time.time()]
        self.clock_patch = mock.patch("build_slot.time.time", side_effect=lambda: self.clock[0])
        self.sleep_patch = mock.patch("build_slot.time.sleep", side_effect=lambda delay: self.clock.__setitem__(0, self.clock[0] + delay))
        self.clock_patch.start()
        self.sleep_patch.start()
        self.addCleanup(self.clock_patch.stop)
        self.addCleanup(self.sleep_patch.stop)
    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_stagger_rechecked_within_slot_guard_prevents_race(self, *_probes):
        """
        If last_acquired_at changes between the pre-check and entering the slot guard,
        rechecking stagger inside _transition_guard must abort acquisition on active stagger.
        """
        now = time.time()
        # Outside guard: last acquisition was 100s ago (stagger elapsed)
        self.manager._record_last_acquired_at("earlier-lane", 1111, 0)
        stagger_file = os.path.join(self.run_dir, "last-acquired-at.json")
        with open(stagger_file, "w", encoding="utf-8") as f:
            json.dump({"acquired_at_epoch": now - 100.0, "owner": "earlier-lane", "pid": 1111, "slot": 0}, f)

        # Hook: right when entering the slot guard, simulate another worker having just acquired slot 0
        original_transition_guard = build_slot._transition_guard

        def race_simulating_guard(path, timeout=5.0, cancel=None):
            if "build-slot.guard" in path:
                # Concurrent worker wins race and updates last_acquired_at to 2s ago (< 45s stagger)
                with open(stagger_file, "w", encoding="utf-8") as f:
                    json.dump({"acquired_at_epoch": time.time() - 2.0, "owner": "racing-lane", "pid": 2222, "slot": 0}, f)
            return original_transition_guard(path, timeout=timeout, cancel=cancel)

        with mock.patch("build_slot._transition_guard", side_effect=race_simulating_guard):
            # Attempt acquire with timeout=0.1
            acquired = self.manager.acquire("delayed-lane", timeout=0.1, poll_interval=0.02, job_class="heavy")

        self.assertFalse(acquired, "Acquisition must be refused when stagger is rechecked inside guard and active")
        self.assertFalse(self.manager.is_held_by("delayed-lane"))

    def test_light_and_medium_backfill_stagger_without_delaying_heavy(self, *_probes):
        self.manager._record_last_acquired_at("heavy-before", 1111, 0)
        last = self.manager._read_last_acquired_at()
        self.manager.enqueue("head", os.getpid(), token="head", job_class="heavy", mem_gib=5.0)
        for job_class in ("light", "medium"):
            with self.subTest(job_class=job_class):
                self.assertTrue(self.manager.acquire(
                    job_class, timeout=.1, poll_interval=.02, job_class=job_class))
                self.assertEqual(self.manager._read_last_acquired_at(), last)
                self.manager.release(job_class)


class TestBuildSlotSharedPidHeartbeatExpiry(unittest.TestCase):
    """
    Test issue #711:
    _is_entry_stale expires heartbeats after 60s regardless of live PID;
    missing heartbeat fallback 1800s.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="build-slot-shared-pid-")
        self.run_dir = os.path.join(self.test_dir, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        self.manager = BuildSlotManager(run_dir=self.run_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_is_entry_stale_expires_heartbeat_at_60s_regardless_of_live_pid(self):
        """A live shared PID does NOT protect a queue entry whose heartbeat is older than 60s (#711)."""
        now = time.time()
        item = {
            "name": "dead-in-process-lane",
            "token": "tok-dead-lane",
            "pid": 12536,  # Live shared Main PID
            "enqueued_at": now - 300.0,
            "heartbeat_at": now - 65.0,  # 65s > 60s
        }
        # Shared PID 12536 is alive
        alive_pids = {12536: True}

        stale, reason = self.manager._is_entry_stale(
            item, now, stale_heartbeat_after=60.0, stale_fallback_after=1800.0, alive_pids=alive_pids
        )
        self.assertTrue(stale, "Entry with 65s heartbeat must be stale despite living PID 12536")
        self.assertIn("expired", reason.lower())

    def test_is_entry_stale_preserves_fresh_heartbeat_for_live_pid(self):
        """A live shared PID with a fresh heartbeat (<= 60s) is preserved."""
        now = time.time()
        item = {
            "name": "active-in-process-lane",
            "token": "tok-active-lane",
            "pid": 12536,
            "enqueued_at": now - 100.0,
            "heartbeat_at": now - 15.0,  # 15s <= 60s
        }
        alive_pids = {12536: True}

        stale, reason = self.manager._is_entry_stale(
            item, now, stale_heartbeat_after=60.0, stale_fallback_after=1800.0, alive_pids=alive_pids
        )
        self.assertFalse(stale, "Entry with fresh heartbeat must NOT be stale")

    def test_is_entry_stale_missing_heartbeat_fallback_1800s(self):
        """
        Legacy entries without heartbeat_at:
        - preserved if enqueued_at <= 1800s under live PID
        - expired if enqueued_at > 1800s
        """
        now = time.time()
        alive_pids = {12536: True}

        # Legacy fresh (300s <= 1800s)
        legacy_fresh = {
            "name": "legacy-fresh",
            "token": "tok-leg-fresh",
            "pid": 12536,
            "enqueued_at": now - 300.0,
        }
        stale, _ = self.manager._is_entry_stale(
            legacy_fresh, now, stale_heartbeat_after=60.0, stale_fallback_after=1800.0, alive_pids=alive_pids
        )
        self.assertFalse(stale, "Legacy entry without heartbeat <= 1800s under live PID must be preserved")

        # Legacy stale (1850s > 1800s)
        legacy_stale = {
            "name": "legacy-stale",
            "token": "tok-leg-stale",
            "pid": 12536,
            "enqueued_at": now - 1850.0,
        }
        stale, reason = self.manager._is_entry_stale(
            legacy_stale, now, stale_heartbeat_after=60.0, stale_fallback_after=1800.0, alive_pids=alive_pids
        )
        self.assertTrue(stale, "Legacy entry without heartbeat > 1800s must expire")

    def test_is_entry_stale_dead_pid_expires_immediately(self):
        """Dead PID expires immediately regardless of heartbeat timestamp."""
        now = time.time()
        alive_pids = {99999: False}
        item = {
            "name": "dead-pid-lane",
            "token": "tok-dead",
            "pid": 99999,
            "enqueued_at": now - 10.0,
            "heartbeat_at": now - 2.0,  # fresh heartbeat, but dead PID
        }
        stale, reason = self.manager._is_entry_stale(
            item, now, stale_heartbeat_after=60.0, stale_fallback_after=1800.0, alive_pids=alive_pids
        )
        self.assertTrue(stale, "Dead PID must expire immediately")
        self.assertIn("dead", reason.lower())

    def test_shared_pid_dead_waiter_pruned_unblocking_next_live_waiter(self):
        """
        Exact scenario of issue #711:
        Main process PID 12536 stays alive.
        Dead lane PickList667 has heartbeat 1323s old at queue head.
        Live waiter NextLane has fresh heartbeat 5s old.
        clean_queue must prune PickList667, allowing NextLane to become queue head.
        """
        shared_pid = 12536
        manager = BuildSlotManager(run_dir=self.run_dir, is_pid_alive_fn=lambda p: p == shared_pid)
        now = time.time()
        seeded_queue = [
            {
                "name": "PickList667",
                "pid": shared_pid,
                "token": "80b52489-8cc8-4fa7-93a7-fc8f90f04ab9",
                "enqueued_at": now - 1400.0,
                "heartbeat_at": now - 1323.0,
            },
            {
                "name": "NextLiveLane",
                "pid": shared_pid,
                "token": "next-live-token",
                "enqueued_at": now - 100.0,
                "heartbeat_at": now - 5.0,
            },
        ]
        manager._write_queue(seeded_queue)

        cleaned = manager.clean_queue(stale_heartbeat_after=60.0)
        survivor_names = [x["name"] for x in cleaned]

        self.assertNotIn("PickList667", survivor_names, "Dead shared-PID waiter PickList667 must be pruned")
        self.assertEqual(survivor_names, ["NextLiveLane"], "Next live waiter must become queue head")


class TestBuildSlotAdmissionLoop(unittest.TestCase):
    def setUp(self):
        self.clock = [time.time()]
        clock_patch = mock.patch("build_slot.time.time", side_effect=lambda: self.clock[0])
        sleep_patch = mock.patch("build_slot.time.sleep", side_effect=lambda delay: self.clock.__setitem__(0, self.clock[0] + delay))
        clock_patch.start()
        sleep_patch.start()
        self.addCleanup(clock_patch.stop)
        self.addCleanup(sleep_patch.stop)

    def test_real_backfill_preserves_head(self):
        with tempfile.TemporaryDirectory() as run_dir, \
                mock.patch("build_slot.get_available_ram_gib", return_value=4.0), \
                mock.patch("build_slot.get_system_ram_percent", return_value=50.0):
            manager = BuildSlotManager(run_dir=run_dir, acquisition_stagger=0)
            manager.enqueue("head", os.getpid(), token="head", job_class="heavy", mem_gib=3.0)
            head = manager._read_queue()[0].copy()
            self.assertTrue(manager.acquire("small", token="small", timeout=.2, poll_interval=.01, job_class="light"))
            self.assertEqual(manager._read_queue()[0], head)
            self.assertTrue(manager.release("small", token="small"))

    def test_real_timeout_reports_heavy_cap_and_negative_budget(self):
        with tempfile.TemporaryDirectory() as run_dir, \
                mock.patch("build_slot.get_available_ram_gib", return_value=11.0), \
                mock.patch("build_slot.get_system_ram_percent", return_value=88.0):
            manager = BuildSlotManager(run_dir=run_dir, acquisition_stagger=0)
            self.assertTrue(manager.acquire("held", token="held", timeout=.2, job_class="heavy", mem_gib=5.0))
            self.assertTrue(manager.acquire("light-held", token="light-held", timeout=.2, mem_gib=.5))
            output = io.StringIO()
            with redirect_stderr(output), mock.patch("build_slot.get_available_ram_gib", return_value=6.68):
                self.assertFalse(manager.acquire("blocked", token="blocked", timeout=.05, poll_interval=.01, job_class="heavy"))
            self.assertIn("heavy cap", output.getvalue())
            self.assertIn("usable now -1.82 GiB", output.getvalue())
            self.assertIn("available 6.68 minus ramp reservations 5.50 minus floor 3.00", output.getvalue())
            self.assertNotIn("stagger", output.getvalue())
            manager.release("held", token="held")
            manager.release("light-held", token="light-held")
    def test_corrupt_heartbeat_does_not_use_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as run_dir:
            manager = BuildSlotManager(run_dir=run_dir)
            stale, reason = manager._is_entry_stale(
                {"pid": os.getpid(), "heartbeat_at": "bad", "enqueued_at": time.time()},
                time.time(), 60, 1800, {os.getpid(): True})
            self.assertTrue(stale)
            self.assertIn("corrupt heartbeat", reason)

class TestBuildSlotUnknownClassFallback(unittest.TestCase):
    """
    Test ADR 0004 unknown queue/holder class fallback contracts:
    - Unknown queue/holder class falls back to heavy minimum 5GiB, heavy cap,
      ramp 300s, stagger and 40min aging rules.
    - Known browser 1.1GiB preserved.
    - Explicit unknown mem 1 becomes 5, larger 7 remains 7.
    - Simulated prior-table reader without browser uses heavy fallback, not KeyError.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="build-slot-unknown-")
        self.run_dir = os.path.join(self.test_dir, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        self.manager = BuildSlotManager(run_dir=self.run_dir, acquisition_stagger=45.0)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Unknown-class head without mem and with explicit 1/7 memory
    # -------------------------------------------------------------------------

    def test_unknown_class_head_without_mem_falls_back_to_heavy_5gib(self):
        """Unknown class without explicit mem falls back to heavy minimum 5.0 GiB."""
        now = time.time()
        # Direct reservation: falls back to heavy minimum 5.0 GiB instead of KeyError
        self.assertEqual(_reservation_gib("future_worker", None), 5.0)

        # Queue admission: unknown-class head needs 5.0 GiB
        queue_no_mem = [
            {"name": "u-head", "token": "tok-u", "pid": 1001, "job_class": "future_worker", "enqueued_at": now - 10.0}
        ]
        budget_refuse = {
            "available_gib": 7.0, "reserved_gib": 0.0, "floor_gib": 3.0,
            "free_budget_gib": 4.0, "heavy_jobs": 0,
        }
        eligible, reason = self.manager._queue_admission(queue_no_mem, "tok-u", budget_refuse, now)
        self.assertFalse(eligible, "Unknown head needing 5.0 GiB must be refused when free budget is 4.0 GiB")
        self.assertIn("needs 5.00", reason)

        budget_fit = {
            "available_gib": 16.0, "reserved_gib": 0.0, "floor_gib": 3.0,
            "free_budget_gib": 10.0, "heavy_jobs": 0,
        }
        eligible_fit, reason_fit = self.manager._queue_admission(queue_no_mem, "tok-u", budget_fit, now)
        self.assertTrue(eligible_fit)
        self.assertIsNone(reason_fit)

    def test_unknown_class_head_with_explicit_1gib_and_7gib_memory(self):
        """Explicit unknown mem 1 becomes 5, larger 7 remains 7."""
        now = time.time()
        # Explicit 1.0 becomes heavy minimum 5.0 GiB
        self.assertEqual(_reservation_gib("future_worker", 1.0), 5.0)

        queue_1gib = [
            {"name": "u-head-1", "token": "tok-u1", "pid": 1001, "job_class": "future_worker", "mem_gib": 1.0, "enqueued_at": now - 10.0}
        ]
        budget_4gib = {
            "available_gib": 7.0, "reserved_gib": 0.0, "floor_gib": 3.0,
            "free_budget_gib": 4.0, "heavy_jobs": 0,
        }
        eligible_1, reason_1 = self.manager._queue_admission(queue_1gib, "tok-u1", budget_4gib, now)
        self.assertFalse(eligible_1, "Explicit 1.0 GiB on unknown class must fall back to 5.0 GiB and be refused when free < 5.0")
        self.assertIn("needs 5.00", reason_1)

        # Larger explicit 7.0 remains 7.0
        self.assertEqual(_reservation_gib("future_worker", 7.0), 7.0)
        queue_7gib = [
            {"name": "u-head-7", "token": "tok-u7", "pid": 1001, "job_class": "future_worker", "mem_gib": 7.0, "enqueued_at": now - 10.0}
        ]
        eligible_7, reason_7 = self.manager._queue_admission(
            queue_7gib, "tok-u7",
            {"available_gib": 9.0, "reserved_gib": 0.0, "floor_gib": 3.0, "free_budget_gib": 6.0, "heavy_jobs": 0},
            now,
        )
        self.assertFalse(eligible_7)
        self.assertIn("needs 7.00", reason_7)

    def test_known_browser_preserved_at_1_1gib(self):
        """Known browser 1.1GiB reservation is preserved and does not fall back to 5.0 GiB."""
        now = time.time()
        self.assertEqual(_reservation_gib("browser", None), 1.1)
        self.assertEqual(_reservation_gib("browser", 1.1), 1.1)
        queue_b = [
            {"name": "browser-head", "token": "tok-b", "pid": 1001, "job_class": "browser", "enqueued_at": now - 10.0}
        ]
        budget_2gib = {
            "available_gib": 5.0, "reserved_gib": 0.0, "floor_gib": 3.0,
            "free_budget_gib": 2.0, "heavy_jobs": 0,
        }
        eligible_b, reason_b = self.manager._queue_admission(queue_b, "tok-b", budget_2gib, now)
        self.assertTrue(eligible_b, f"Known browser must be admitted when free >= 1.1 GiB; reason={reason_b}")
        self.assertIsNone(reason_b)

    # -------------------------------------------------------------------------
    # 2. Unknown later candidate under heavy cap/stagger
    # -------------------------------------------------------------------------

    def test_unknown_later_candidate_refused_under_heavy_cap(self):
        """Unknown later candidate is refused under heavy cap when heavy job held."""
        now = time.time()
        budget_heavy = {
            "available_gib": 16.0, "reserved_gib": 5.0, "floor_gib": 3.0,
            "free_budget_gib": 8.0, "heavy_jobs": 1,
        }
        with mock.patch("build_slot.get_system_ram_percent", return_value=88.0):
            refusal = self.manager._resource_refusal("future_worker", 5.0, budget_heavy)
            self.assertIsNotNone(refusal, "Unknown candidate must be refused under heavy cap")
            self.assertIn("heavy cap", refusal)

            queue = [
                {"name": "head-blocked", "token": "tok-head", "job_class": "heavy", "mem_gib": 10.0, "enqueued_at": now - 50.0},
                {"name": "u-cand", "token": "tok-u", "pid": 1002, "job_class": "future_worker", "mem_gib": 5.0, "enqueued_at": now - 20.0},
            ]
            eligible, reason = self.manager._queue_admission(queue, "tok-u", budget_heavy, now)
            self.assertFalse(eligible, "Unknown candidate must not be admitted when heavy job held")
            self.assertIn("heavy cap", reason)
    def test_unknown_later_candidate_delayed_under_stagger(self):
        """Unknown later candidate falls back to heavy stagger rules."""
        now = time.time()
        budget_no_heavy = {
            "available_gib": 16.0, "reserved_gib": 0.0, "floor_gib": 3.0,
            "free_budget_gib": 13.0, "heavy_jobs": 0,
        }
        last_acq = now - 10.0  # 10s < 45s stagger
        queue = [
            {"name": "head-blocked", "token": "tok-head", "job_class": "heavy", "mem_gib": 10.0, "enqueued_at": now - 50.0},
            {"name": "u-cand", "token": "tok-u", "pid": 1002, "job_class": "future_worker", "mem_gib": 5.0, "enqueued_at": now - 20.0},
        ]
        eligible_stagger, reason_stagger = self.manager._queue_admission(
            queue, "tok-u", budget_no_heavy, now, last_acquired_at=last_acq
        )
        self.assertFalse(eligible_stagger, "Unknown candidate must be delayed under active stagger")
        self.assertIn("stagger delay active", reason_stagger)

    # -------------------------------------------------------------------------
    # 3. Aged unknown head blocking backfill after 40min
    # -------------------------------------------------------------------------

    def test_aged_unknown_head_blocks_backfill_after_40min(self):
        """Aged unknown head (> 2400s / 40 min) pauses backfill under heavy aging rules."""
        now = time.time()
        queue = [
            {
                "name": "aged-u-head", "token": "tok-uaged", "pid": 2001,
                "job_class": "future_worker", "mem_gib": 5.0, "enqueued_at": now - 2405.0,
            },
            {
                "name": "light-waiter", "token": "tok-light", "pid": 2002,
                "job_class": "light", "mem_gib": 0.5, "enqueued_at": now - 100.0,
            },
        ]
        budget = {
            "available_gib": 4.5, "reserved_gib": 4.0, "floor_gib": 3.0,
            "free_budget_gib": 0.5, "heavy_jobs": 0,
        }
        for sample in range(3):
            eligible, reason = self.manager._queue_admission(queue, "tok-light", budget, now + sample * 5)
        self.assertFalse(eligible, "Aged unknown head (> 40min) must pause backfill")
        self.assertIsNotNone(reason)
        self.assertTrue("aging" in reason.lower() or "paused" in reason.lower() or "waited" in reason.lower())

    # -------------------------------------------------------------------------
    # 4. Unknown held slot counts heavy and ramp conservatively
    # -------------------------------------------------------------------------

    def test_unknown_held_slot_counts_heavy_and_ramps_conservatively(self):
        """Unknown held slot counts as heavy and reserves heavy minimum with 300s ramp."""
        now = time.time()
        os.makedirs(self.manager.slot_dirs[0], exist_ok=True)
        self.manager._write_slot_info(0, owner="held-u", pid=os.getpid(), token="tok-held", job_class="future_worker", mem_gib=1.0)
        info_path = os.path.join(self.manager.slot_dirs[0], "info.json")
        with open(info_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["acquired_at_epoch"] = now - 150.0  # within 300s heavy ramp
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        with mock.patch("build_slot.get_available_ram_gib", return_value=16.0), \
             mock.patch("build_slot.time.time", return_value=now):
            b = self.manager._memory_budget()

        self.assertEqual(b["heavy_jobs"], 1, "Unknown held slot must count toward heavy_jobs")
        self.assertGreaterEqual(b["reserved_gib"], 5.0, "Unknown held slot must reserve at least 5.0 GiB")
        self.assertGreaterEqual(b["ramp_reservations_gib"], 5.0, "Ramp reservation must be at least 5.0 GiB during 300s ramp")

        # After 300s ramp window (350s ago), ramp reservation settles to 0
        data["acquired_at_epoch"] = now - 350.0
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        with mock.patch("build_slot.get_available_ram_gib", return_value=16.0), \
             mock.patch("build_slot.time.time", return_value=now):
            b_settled = self.manager._memory_budget()
        self.assertEqual(b_settled["ramp_reservations_gib"], 0.0)

        # Missing/corrupt acquired timestamp ramps conservatively
        data["acquired_at_epoch"] = None
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(data, f)

        with mock.patch("build_slot.get_available_ram_gib", return_value=16.0), \
             mock.patch("build_slot.time.time", return_value=now):
            b_conservative = self.manager._memory_budget()
        self.assertGreaterEqual(b_conservative["ramp_reservations_gib"], 5.0)

    # -------------------------------------------------------------------------
    # 5. Simulated prior-table reader by patching MEMORY_RESERVATIONS to omit browser
    # -------------------------------------------------------------------------

    def test_simulated_prior_table_reader_omits_browser(self):
        """Simulated prior-table reader omitting browser uses heavy fallback, not KeyError."""
        now = time.time()
        prior_table = {k: v for k, v in MEMORY_RESERVATIONS.items() if k != "browser"}
        with mock.patch.dict(build_slot.MEMORY_RESERVATIONS, prior_table, clear=True):
            # 1. Direct reservation fallback: must not raise KeyError, must return 5.0 GiB
            self.assertEqual(_reservation_gib("browser", None), 5.0)
            self.assertEqual(_reservation_gib("browser", 1.0), 5.0)

            # 2. Queue admission fallback: must not raise KeyError
            queue = [
                {"name": "browser-lane", "token": "tok-b", "pid": 1001, "job_class": "browser", "enqueued_at": now}
            ]
            budget_refuse = {
                "available_gib": 7.0, "reserved_gib": 0.0, "floor_gib": 3.0,
                "free_budget_gib": 4.0, "heavy_jobs": 0,
            }
            eligible, reason = self.manager._queue_admission(queue, "tok-b", budget_refuse, now)
            self.assertFalse(eligible)
            self.assertIn("needs 5.00", reason)

            # 3. Memory budget fallback: must not raise KeyError, counts as heavy
            os.makedirs(self.manager.slot_dirs[0], exist_ok=True)
            self.manager._write_slot_info(0, owner="held-b", pid=os.getpid(), token="tok-b", job_class="browser", mem_gib=1.1)
            with mock.patch("build_slot.get_available_ram_gib", return_value=16.0), \
                 mock.patch("build_slot.time.time", return_value=now):
                b = self.manager._memory_budget()
            self.assertEqual(b["heavy_jobs"], 1)
            self.assertGreaterEqual(b["reserved_gib"], 5.0)

    # -------------------------------------------------------------------------
    # 6. Missing job_class metadata fallback (legacy metadata retains 5 GiB)
    # -------------------------------------------------------------------------

    def test_missing_class_held_slot_reserved_and_ramp_stay_5(self):
        """Held slot with missing job_class and explicit mem_gib 1 and 3 stays at 5 GiB."""
        now = time.time()
        for mem in (1.0, 3.0):
            os.makedirs(self.manager.slot_dirs[0], exist_ok=True)
            info = {
                "owner": f"held-legacy-{int(mem)}",
                "pid": os.getpid(),
                "token": f"tok-held-{int(mem)}",
                "slot": 0,
                "mem_gib": mem,
                "acquired_at_epoch": now - 50.0,
                "heartbeat_at_epoch": now,
            }
            info_path = os.path.join(self.manager.slot_dirs[0], "info.json")
            with open(info_path, "w", encoding="utf-8") as f:
                json.dump(info, f)

            with mock.patch("build_slot.get_available_ram_gib", return_value=16.0), \
                 mock.patch("build_slot.time.time", return_value=now):
                b = self.manager._memory_budget()

            self.assertEqual(
                b["reserved_gib"], 5.0,
                f"Held slot with missing job_class and mem_gib={mem} must retain conservative 5 GiB reservation",
            )
            self.assertEqual(
                b["ramp_reservations_gib"], 5.0,
                f"Held slot with missing job_class and mem_gib={mem} must retain conservative 5 GiB ramp reservation",
            )

    def test_missing_class_queue_admission_refuses_legacy_head(self):
        """Queue admission refuses legacy head with missing job_class and available 6.5 GiB, reporting needs 5.00."""
        now = time.time()
        budget = {
            "available_gib": 6.5,
            "reserved_gib": 0.0,
            "floor_gib": 3.0,
            "free_budget_gib": 3.5,
            "heavy_jobs": 0,
        }
        for mem in (1.0, 3.0):
            queue = [
                {
                    "name": f"legacy-head-{int(mem)}",
                    "token": f"tok-leg-{int(mem)}",
                    "pid": 1001,
                    "mem_gib": mem,
                    "enqueued_at": now - 10.0,
                }
            ]
            eligible, reason = self.manager._queue_admission(queue, f"tok-leg-{int(mem)}", budget, now)
            self.assertFalse(
                eligible,
                f"Legacy head with missing job_class and mem_gib={mem} must be refused when free budget is 3.5 GiB",
            )
            self.assertIsNotNone(reason)
            self.assertIn(
                "needs 5.00", reason,
                f"Refusal reason for missing job_class and mem_gib={mem} must report needs 5.00",
            )

    def test_missing_class_status_queue_mem_gib_stays_5(self):
        """Status queue mem_gib stays 5 for queued items with missing job_class and explicit mem 1 and 3."""
        now = time.time()
        queue = [
            {
                "name": "legacy-mem-1",
                "token": "tok-leg-1",
                "pid": os.getpid(),
                "mem_gib": 1.0,
                "enqueued_at": now,
                "heartbeat_at": now,
            },
            {
                "name": "legacy-mem-3",
                "token": "tok-leg-3",
                "pid": os.getpid(),
                "mem_gib": 3.0,
                "enqueued_at": now,
                "heartbeat_at": now,
            },
        ]
        self.manager._write_queue(queue)
        with mock.patch("build_slot.get_available_ram_gib", return_value=16.0), \
             mock.patch("build_slot.get_system_ram_percent", return_value=50.0), \
             mock.patch("build_slot.time.time", return_value=now):
            stat = self.manager.status()

        q_items = {item["name"]: item for item in stat["queue"]}
        self.assertIn("legacy-mem-1", q_items)
        self.assertIn("legacy-mem-3", q_items)
        self.assertEqual(
            q_items["legacy-mem-1"]["mem_gib"], 5.0,
            "Queue item with missing job_class and mem_gib=1.0 must report mem_gib=5.0 in status",
        )
        self.assertEqual(
            q_items["legacy-mem-3"]["mem_gib"], 5.0,
            "Queue item with missing job_class and mem_gib=3.0 must report mem_gib=5.0 in status",
        )


class TestBuildSlotOperatorDecisionBuildQ(unittest.TestCase):
    """
    Focused behavioral tests for operator BuildQ decision (Telegram 2026-10-10):
    - Known heavy default AND minimum reservation is 4.5 GiB ('4.5 GB each')
    - Explicit heavy reservation below 4.5 GiB clamps to 4.5 GiB
    - Heavy cap: 2 heavy jobs concurrently below 85% RAM when budget fits ('2 at once below 85% RAM')
    - Boundary test 84.999% RAM admits second heavy job
    - Boundary test 85.0% RAM caps heavy at 1
    - Third heavy job refused under heavy cap below 85% RAM
    - Peak remainder safety invariant: second heavy must preserve:
        available - new - max(4.46 - running_private_resident_usage, 0) >= 3.0 floor
      even after ramp expires, evaluated via budget field heavy_peak_remainder_gib
      and helper get_process_tree_ram_gib(child_pid)
    - Missing RAM telemetry conservatively caps heavy at 1
    - Missing/unknown class metadata reservation preserved at 5 GiB
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="build-slot-buildq-")
        self.run_dir = os.path.join(self.test_dir, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        self.manager = BuildSlotManager(run_dir=self.run_dir, acquisition_stagger=0)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_buildq_default_heavy_reservation_is_four_point_five_gib(self):
        """Known heavy reservation defaults to 4.5 GiB instead of 5 GiB."""
        self.assertEqual(build_slot.MEMORY_RESERVATIONS.get("heavy"), 4.5)
        self.assertEqual(build_slot._reservation_gib("heavy", None), 4.5)

        with mock.patch("build_slot.get_available_ram_gib", return_value=16.0), \
             mock.patch("build_slot.get_system_ram_percent", return_value=50.0):
            self.assertTrue(self.manager.acquire("heavy-default", job_class="heavy", timeout=1.0))
            info = self.manager._read_slot_info(0)
            self.assertEqual(info.get("job_class"), "heavy")
            self.assertEqual(info.get("mem_gib"), 4.5)
            self.manager.release("heavy-default")

    def test_buildq_explicit_heavy_clamp_below_four_point_five_gib(self):
        """Explicit heavy reservations below 4.5 GiB clamp to 4.5 GiB; larger remain unchanged."""
        self.assertEqual(build_slot._reservation_gib("heavy", 1.0), 4.5)
        self.assertEqual(build_slot._reservation_gib("heavy", 3.0), 4.5)
        self.assertEqual(build_slot._reservation_gib("heavy", 4.0), 4.5)
        self.assertEqual(build_slot._reservation_gib("heavy", 4.5), 4.5)
        self.assertEqual(build_slot._reservation_gib("heavy", 6.0), 6.0)

    def test_buildq_two_heavy_admissions_below_85_ram_when_budget_fits(self):
        """Two heavy jobs are admitted concurrently when RAM < 85% and budget fits."""
        with mock.patch("build_slot.get_system_ram_percent", return_value=50.0), \
             mock.patch("build_slot.get_available_ram_gib", return_value=32.0):
            # Resource refusal with 1 heavy job held and free budget available must be None (cap is 2)
            budget_one = {
                "available_gib": 32.0,
                "reserved_gib": 4.5,
                "ramp_reservations_gib": 4.5,
                "floor_gib": 3.0,
                "free_budget_gib": 24.5,
                "heavy_jobs": 1,
            }
            refusal = self.manager._resource_refusal("heavy", 4.5, budget_one)
            self.assertIsNone(refusal, "Expected second heavy job to be admitted below 85% RAM when budget fits")

            # Real acquisition allows 2 concurrent heavy jobs
            self.assertTrue(self.manager.acquire("heavy-1", job_class="heavy", timeout=1.0))
            self.assertTrue(self.manager.acquire("heavy-2", job_class="heavy", timeout=1.0))
            self.manager.release("heavy-1")
            self.manager.release("heavy-2")

    def test_buildq_third_heavy_refused_below_85_ram(self):
        """When 2 heavy jobs are held below 85% RAM, a third heavy job is refused under heavy cap."""
        with mock.patch("build_slot.get_system_ram_percent", return_value=50.0), \
             mock.patch("build_slot.get_available_ram_gib", return_value=64.0):
            self.assertTrue(self.manager.acquire("heavy-1", job_class="heavy", timeout=1.0))
            self.assertTrue(self.manager.acquire("heavy-2", job_class="heavy", timeout=1.0))
            output = io.StringIO()
            with redirect_stderr(output):
                third_acquired = self.manager.acquire("heavy-3", job_class="heavy", timeout=0.05, poll_interval=0.01)
            self.assertFalse(third_acquired)
            self.assertIn("heavy cap", output.getvalue())
            self.manager.release("heavy-1")
            self.manager.release("heavy-2")

    def test_buildq_ram_boundary_84_999_admits_second_heavy(self):
        """At boundary 84.999% RAM (< 85%), heavy cap is 2 and second heavy is admitted when budget fits."""
        with mock.patch("build_slot.get_system_ram_percent", return_value=84.999):
            budget_one = {
                "available_gib": 32.0,
                "reserved_gib": 4.5,
                "ramp_reservations_gib": 4.5,
                "floor_gib": 3.0,
                "free_budget_gib": 24.5,
                "heavy_jobs": 1,
            }
            refusal = self.manager._resource_refusal("heavy", 4.5, budget_one)
            self.assertIsNone(refusal, "Expected second heavy job to be admitted at 84.999% RAM")

        # Accepts synthetic budget ram_percent directly
        synth_budget = dict(budget_one, ram_percent=84.999)
        self.assertIsNone(self.manager._resource_refusal("heavy", 4.5, synth_budget))

    def test_buildq_ram_boundary_85_caps_heavy_at_one(self):
        """At boundary 85.0% RAM (and above), heavy concurrency cap is conservatively 1."""
        for pct in (85.0, 85.001, 88.0):
            with self.subTest(ram_pct=pct):
                with mock.patch("build_slot.get_system_ram_percent", return_value=pct):
                    budget_one = {
                        "available_gib": 32.0,
                        "reserved_gib": 4.5,
                        "ramp_reservations_gib": 4.5,
                        "floor_gib": 3.0,
                        "free_budget_gib": 24.5,
                        "heavy_jobs": 1,
                    }
                    refusal = self.manager._resource_refusal("heavy", 4.5, budget_one)
                    self.assertIsNotNone(refusal)
                    self.assertIn("heavy cap", refusal)

                # Accepts synthetic budget ram_percent directly
                synth_budget = dict(budget_one, ram_percent=pct)
                refusal_synth = self.manager._resource_refusal("heavy", 4.5, synth_budget)
                self.assertIsNotNone(refusal_synth)
                self.assertIn("heavy cap", refusal_synth)

    def test_buildq_second_heavy_fits_pure_helper_boundary(self):
        """
        Pure helper second_heavy_fits(available_gib, new_reservation_gib, running_heavy_ramp_gib) -> bool
        requires available - new - running_heavy_ramp_gib >= 3.0 floor.
        Conservative: returns False on None or non-finite inputs.
        """
        # With running heavy ramp 4.5: boundary is 12.0 GiB available (12.0 - 4.5 - 4.5 = 3.0)
        self.assertTrue(build_slot.second_heavy_fits(12.0, 4.5, 4.5))
        self.assertFalse(build_slot.second_heavy_fits(11.99, 4.5, 4.5))

        # With running heavy ramp expired (0.0): boundary is 7.5 GiB available (7.5 - 4.5 - 0.0 = 3.0)
        self.assertTrue(build_slot.second_heavy_fits(7.5, 4.5, 0.0))
        self.assertFalse(build_slot.second_heavy_fits(7.49, 4.5, 0.0))

        # Intermediate ramp or custom reservation
        self.assertTrue(build_slot.second_heavy_fits(10.0, 4.5, 2.5))
        self.assertFalse(build_slot.second_heavy_fits(9.99, 4.5, 2.5))

        # Conservative handling of None and non-finite inputs
        self.assertFalse(build_slot.second_heavy_fits(None, 4.5, 0.0))
        self.assertFalse(build_slot.second_heavy_fits(12.0, None, 4.5))
        self.assertFalse(build_slot.second_heavy_fits(12.0, 4.5, None))
        self.assertFalse(build_slot.second_heavy_fits(float("nan"), 4.5, 0.0))
        self.assertFalse(build_slot.second_heavy_fits(12.0, float("inf"), 0.0))
        self.assertFalse(build_slot.second_heavy_fits(12.0, 4.5, float("nan")))

    def test_buildq_heavy_ramp_reservations_gib_in_memory_budget(self):
        """_memory_budget() includes heavy_limit, heavy_default_gib, and heavy_ramp_reservations_gib."""
        now = time.time()
        os.makedirs(self.manager.slot_dirs[0], exist_ok=True)
        # Active heavy holder within ramp window (age 50s < 300s)
        active_info = {
            "owner": "held-active-heavy",
            "pid": os.getpid(),
            "token": "tok-active-heavy",
            "job_class": "heavy",
            "mem_gib": 4.5,
            "acquired_at_epoch": now - 50.0,
            "acquired_at": "invalid",
        }
        build_slot._write_json_atomic(
            os.path.join(self.manager.slot_dirs[0], build_slot.INFO_FILE_NAME),
            active_info,
        )
        with mock.patch("build_slot.get_available_ram_gib", return_value=16.0), \
             mock.patch("build_slot.get_system_ram_percent", return_value=50.0):
            budget_active = self.manager._memory_budget()
            self.assertIn("heavy_ramp_reservations_gib", budget_active)
            self.assertEqual(budget_active.get("heavy_ramp_reservations_gib"), 4.5)
            self.assertIn("heavy_limit", budget_active)
            self.assertEqual(budget_active.get("heavy_limit"), 2)
            self.assertIn("heavy_default_gib", budget_active)
            self.assertEqual(budget_active.get("heavy_default_gib"), 4.5)

        # Expired heavy holder (age 350s >= 300s)
        active_info["acquired_at_epoch"] = now - 350.0
        build_slot._write_json_atomic(
            os.path.join(self.manager.slot_dirs[0], build_slot.INFO_FILE_NAME),
            active_info,
        )
        with mock.patch("build_slot.get_available_ram_gib", return_value=16.0), \
             mock.patch("build_slot.get_system_ram_percent", return_value=88.0):
            budget_expired = self.manager._memory_budget()
            self.assertIn("heavy_ramp_reservations_gib", budget_expired)
            self.assertEqual(budget_expired.get("heavy_ramp_reservations_gib"), 0.0)
            self.assertIn("heavy_limit", budget_expired)
            self.assertEqual(budget_expired.get("heavy_limit"), 1)
        self.manager.release("held-active-heavy", token="tok-active-heavy")
    def test_buildq_missing_ram_telemetry_conservatively_caps_heavy_at_one(self):
        """When host RAM telemetry is missing (None), heavy concurrency cap is conservatively 1."""
        with mock.patch("build_slot.get_system_ram_percent", return_value=None):
            budget_one = {
                "available_gib": 32.0,
                "reserved_gib": 4.5,
                "ramp_reservations_gib": 4.5,
                "floor_gib": 3.0,
                "free_budget_gib": 24.5,
                "heavy_jobs": 1,
            }
            refusal = self.manager._resource_refusal("heavy", 4.5, budget_one)
            self.assertIsNotNone(refusal)
            self.assertIn("heavy cap", refusal)

    def test_buildq_missing_unknown_class_reservation_still_five_gib(self):
        """Missing or unknown job class metadata preserves conservative 5 GiB reservation."""
        self.assertEqual(build_slot._reservation_gib("unknown_future_worker", None), 5.0)
        self.assertEqual(build_slot._reservation_gib("unknown_future_worker", 1.0), 5.0)
        self.assertEqual(build_slot._reservation_gib("unknown_future_worker", 7.0), 7.0)
class TestBuildSlotOccupiedLaneAdmission(unittest.TestCase):
    """
    Admission invariant tests:
    - Admission ignores queue entries belonging to occupied lane names,
      preserving queued entries and FIFO among eligible waiters.
    - Regression: duplicate owner holding slot does not block later backfill waiter.
    - Regression: same-token grant residue surviving under transition guard does not block.
    - Head priority preserved once head fits.
    - FIFO preserved among runnable backfill waiters.
    - Heavy cap only under RAM policy where limit is one.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="build-slot-occupied-")
        self.run_dir = os.path.join(self.test_dir, "run")
        os.makedirs(self.run_dir, exist_ok=True)
        self.manager = BuildSlotManager(run_dir=self.run_dir, acquisition_stagger=0)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_duplicate_owner_distinct_token_queued_does_not_block_later_backfill(self):
        """
        Regression: seed unfit medium head 1.5 GiB with budget available 3.69,
        held light slot belonging to earlier-light, queued earlier-light request
        with distinct token, then later-light 0.5 GiB. Later-light must acquire
        despite earlier duplicate owner.
        """
        with mock.patch("build_slot.get_available_ram_gib", return_value=16.0), \
             mock.patch("build_slot.get_system_ram_percent", return_value=50.0):
            ok = self.manager.acquire("earlier-light", token="tok-earlier-held", job_class="light", mem_gib=0.5, timeout=1.0)
            self.assertTrue(ok)

        # Settle the held slot so ramp reservation is 0.0
        info_path = os.path.join(self.manager.slot_dirs[0], build_slot.INFO_FILE_NAME)
        info = self.manager._read_slot_info(0)
        info["acquired_at_epoch"] = time.time() - 100.0
        build_slot._write_json_atomic(info_path, info)

        # Enqueue unfit medium head 1.5 GiB
        self.manager.enqueue("medium-head", os.getpid(), token="tok-medium-head", job_class="medium", mem_gib=1.5)

        # Queued earlier-light request with distinct token
        self.manager.enqueue("earlier-light", os.getpid(), token="tok-earlier-distinct", job_class="light", mem_gib=0.5)

        # Later-light 0.5 GiB must acquire with available 3.69 GiB (floor 3.0 -> free 0.69 GiB)
        with mock.patch("build_slot.get_available_ram_gib", return_value=3.69), \
             mock.patch("build_slot.get_system_ram_percent", return_value=50.0):
            acquired = self.manager.acquire(
                "later-light", token="tok-later-light", timeout=0.2, poll_interval=0.01, job_class="light", mem_gib=0.5
            )
            self.assertTrue(acquired, "later-light must acquire despite earlier duplicate owner holding a slot")
            self.assertTrue(self.manager.is_held_by("later-light", token="tok-later-light"))

    def test_same_token_grant_residue_under_transition_guard_does_not_block_later_backfill(self):
        """
        Regression: recently acquired same-token queue residue surviving in queue
        before/under transition guard must not block later backfill waiter.
        """
        with mock.patch("build_slot.get_available_ram_gib", return_value=16.0), \
             mock.patch("build_slot.get_system_ram_percent", return_value=50.0):
            ok = self.manager.acquire("earlier-light", token="tok-earlier-grant", job_class="light", mem_gib=0.5, timeout=1.0)
            self.assertTrue(ok)

        info_path = os.path.join(self.manager.slot_dirs[0], build_slot.INFO_FILE_NAME)
        info = self.manager._read_slot_info(0)
        info["acquired_at_epoch"] = time.time() - 100.0
        build_slot._write_json_atomic(info_path, info)

        self.manager.enqueue("medium-head", os.getpid(), token="tok-medium-head", job_class="medium", mem_gib=1.5)

        # Simulate same-token grant residue surviving in queue
        now = time.time()
        q_residue = [
            {"name": "medium-head", "pid": os.getpid(), "token": "tok-medium-head", "job_class": "medium", "mem_gib": 1.5, "enqueued_at": now - 50.0},
            {"name": "earlier-light", "pid": os.getpid(), "token": "tok-earlier-grant", "job_class": "light", "mem_gib": 0.5, "enqueued_at": now - 40.0},
        ]
        self.manager._write_queue(q_residue)

        def clean_queue_fallback(*args, **kwargs):
            return self.manager._read_queue()

        with mock.patch.object(self.manager, "clean_queue", side_effect=clean_queue_fallback), \
             mock.patch("build_slot.get_available_ram_gib", return_value=4.19), \
             mock.patch("build_slot.get_system_ram_percent", return_value=50.0):
            acquired = self.manager.acquire(
                "later-light", token="tok-later-light", timeout=0.2, poll_interval=0.01, job_class="light", mem_gib=0.5
            )
            self.assertTrue(acquired, "later-light must acquire despite same-token grant residue for occupied lane in queue")
            self.assertTrue(self.manager.is_held_by("later-light", token="tok-later-light"))

    def test_head_priority_preserved_once_head_fits(self):
        """FIFO head has priority once its required memory fits in available budget."""
        now = time.time()
        self.manager.enqueue("head-medium", os.getpid(), token="tok-head", job_class="medium", mem_gib=1.5)
        self.manager.enqueue("later-light", os.getpid(), token="tok-later", job_class="light", mem_gib=0.5)

        # When head fits (available 16.0, free budget > 1.5 GiB)
        with mock.patch("build_slot.get_available_ram_gib", return_value=16.0), \
             mock.patch("build_slot.get_system_ram_percent", return_value=50.0):
            # later-light cannot jump ahead of runnable head
            queue = self.manager._read_queue()
            budget = self.manager._memory_budget()
            admit_later, reason_later = self.manager._queue_admission(queue, "tok-later", budget, now)
            self.assertFalse(admit_later)
            self.assertIn("FIFO head 'head-medium' can run", reason_later)

            # Head acquires first
            ok_head = self.manager.acquire("head-medium", token="tok-head", timeout=0.2, poll_interval=0.01, job_class="medium")
            self.assertTrue(ok_head)
            self.assertTrue(self.manager.is_held_by("head-medium", token="tok-head"))
            self.manager.release("head-medium", token="tok-head")

    def test_fifo_preserved_among_runnable_waiters(self):
        """When head is blocked, earlier eligible backfill waiter has FIFO priority over later waiter."""
        now = time.time()
        self.manager.enqueue("blocked-heavy", os.getpid(), token="tok-head", job_class="heavy", mem_gib=4.5)
        self.manager.enqueue("first-light", os.getpid(), token="tok-first", job_class="light", mem_gib=0.5)
        self.manager.enqueue("second-light", os.getpid(), token="tok-second", job_class="light", mem_gib=0.5)

        # Budget where heavy cannot run (available 7.0, reserved 1.0, floor 3.0 -> free 3.0 < 4.5; projected 5.0 >= 4.5 not impossible)
        # light 0.5 fits
        with mock.patch("build_slot.get_available_ram_gib", return_value=7.0), \
             mock.patch("build_slot.get_system_ram_percent", return_value=50.0):
            os.makedirs(self.manager.slot_dirs[0], exist_ok=True)
            dummy_info = {
                "owner": "dummy-holder", "pid": os.getpid(), "token": "tok-dummy",
                "job_class": "medium", "mem_gib": 1.0, "acquired_at_epoch": now,
            }
            build_slot._write_json_atomic(os.path.join(self.manager.slot_dirs[0], build_slot.INFO_FILE_NAME), dummy_info)

            queue = self.manager._read_queue()
            budget = self.manager._memory_budget()

            # Second light cannot jump ahead of first light
            admit_second, reason_second = self.manager._queue_admission(queue, "tok-second", budget, now)
            self.assertFalse(admit_second)
            self.assertEqual(reason_second, "earlier backfill waiter 'first-light' can run")

            # First light acquires (into slot 1, since slot 0 is held by dummy)
            ok_first = self.manager.acquire("first-light", token="tok-first", timeout=0.2, poll_interval=0.01, job_class="light")
            self.assertTrue(ok_first)
            self.assertTrue(self.manager.is_held_by("first-light", token="tok-first"))
            self.manager.release("first-light", token="tok-first")
            self.manager.release("dummy-holder", token="tok-dummy")

    def test_heavy_cap_only_under_ram_policy_where_limit_is_one(self):
        """
        Heavy cap limits concurrency to 1 only when RAM >= 85% (or missing telemetry);
        below 85% RAM, heavy limit is 2.
        """
        with mock.patch("build_slot.get_system_ram_percent", return_value=88.0):
            budget_at_88 = {
                "available_gib": 32.0, "reserved_gib": 4.5, "floor_gib": 3.0,
                "free_budget_gib": 24.5, "heavy_jobs": 1,
            }
            refusal_at_88 = self.manager._resource_refusal("heavy", 4.5, budget_at_88)
            self.assertIsNotNone(refusal_at_88)
            self.assertIn("heavy cap", refusal_at_88)
            self.assertIn("limit 1", refusal_at_88)

            # Light job is not blocked by heavy cap at 88%
            refusal_light_at_88 = self.manager._resource_refusal("light", 0.5, budget_at_88)
            self.assertIsNone(refusal_light_at_88)

        # Below 85% RAM (e.g. 50%), heavy limit is 2, so second heavy job is admitted
        with mock.patch("build_slot.get_system_ram_percent", return_value=50.0):
            budget_at_50 = {
                "available_gib": 32.0, "reserved_gib": 4.5, "floor_gib": 3.0,
                "free_budget_gib": 24.5, "heavy_jobs": 1,
            }
            refusal_at_50 = self.manager._resource_refusal("heavy", 4.5, budget_at_50)
            self.assertIsNone(refusal_at_50)

class TestPhysicalCapacityAdmission(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = BuildSlotManager(run_dir=self.directory.name, acquisition_stagger=0)

    def test_priority_unfit_head_allows_light_backfill(self):
        self.manager.enqueue("build523", os.getpid(), token="head", priority=True,
                             job_class="heavy", mem_gib=4.5)
        for index in range(19):
            self.manager.enqueue(f"medium-{index}", os.getpid(), token=f"medium-{index}",
                                 job_class="medium", mem_gib=4.0)
        with mock.patch("build_slot.get_available_ram_gib", return_value=6.41), \
             mock.patch("build_slot.get_system_ram_percent", return_value=80.0):
            self.assertTrue(self.manager.acquire("light", token="light", job_class="light",
                                                timeout=0.5, poll_interval=0.01))
        queue = self.manager._read_queue()
        self.assertEqual(len(queue), 20)
        self.assertEqual(queue[0]["token"], "head")
        self.assertTrue(queue[0]["priority"])

    def test_physical_limit_rejects_enqueue_without_queue_mutation(self):
        with mock.patch("build_slot.get_total_ram_gib", return_value=8.0, create=True):
            with self.assertRaisesRegex(ValueError, r"needs 6\.00, max possible 5\.00 GiB"):
                self.manager.enqueue("impossible", os.getpid(), token="bad",
                                     job_class="medium", mem_gib=6.0)
        self.assertEqual(self.manager._read_queue(), [])

    def test_cli_physical_rejection_returns_one_without_waiting_or_queue(self):
        output = io.StringIO()
        with mock.patch("build_slot.get_total_ram_gib", return_value=8.0, create=True), \
             mock.patch("build_slot.get_available_ram_gib", return_value=3.5), \
             mock.patch("build_slot.get_system_ram_percent", return_value=50.0), \
             mock.patch.object(BuildSlotManager, "clean_queue",
                               side_effect=AssertionError("must reject before wait loop")), \
             redirect_stderr(output):
            code = build_slot.main(["--run-dir", self.directory.name, "acquire", "impossible",
                                    "--pid", str(os.getpid()), "--class", "medium",
                                    "--mem-gib", "6", "--timeout", "0.1"])
        self.assertEqual(code, 1)
        self.assertIn("needs 6.00, max possible 5.00 GiB", output.getvalue())
        self.assertEqual(self.manager._read_queue(), [])

    def test_physical_boundary_and_temporary_shortage_stay_queueable(self):
        with mock.patch("build_slot.get_total_ram_gib", return_value=8.0, create=True), \
             mock.patch("build_slot.get_available_ram_gib", return_value=3.1):
            self.manager.enqueue("boundary", os.getpid(), token="boundary",
                                 job_class="medium", mem_gib=5.0)
        self.assertEqual(self.manager._read_queue()[0]["token"], "boundary")

    def test_unknown_total_does_not_reject_enqueue(self):
        with mock.patch("build_slot.get_total_ram_gib", return_value=None, create=True):
            self.manager.enqueue("unknown", os.getpid(), token="unknown",
                                 job_class="medium", mem_gib=64.0)
        self.assertEqual(self.manager._read_queue()[0]["token"], "unknown")

class TestCurrentBudgetText(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = BuildSlotManager(run_dir=self.directory.name)
        self.budget = {"available_gib": 3.33, "reserved_gib": 1.5,
                       "ramp_reservations_gib": 0.0, "floor_gib": 3.0,
                       "free_budget_gib": 0.33, "heavy_jobs": 0, "ram_percent": 88.0}

    def assert_current_budget(self, text):
        self.assertIn("needs 1.50 GiB, usable now 0.33 GiB", text)
        self.assertIn("available 3.33", text)
        self.assertIn("floor 3.00", text)
        self.assertIn("max after current holders finish 1.83 GiB", text)

    def test_resource_refusal_distinguishes_current_and_projected_ram(self):
        self.assert_current_budget(self.manager._resource_refusal("medium", 1.5, self.budget))

    def test_status_reports_waiter_need_instead_of_unfit_head_need(self):
        self.manager.enqueue("head", os.getpid(), token="head", job_class="medium", mem_gib=3.0)
        self.manager.enqueue("waiter", os.getpid(), token="waiter", job_class="medium", mem_gib=1.5)
        with mock.patch.object(self.manager, "_memory_budget", return_value=self.budget):
            status = self.manager.status()
        self.assert_current_budget(status["queue"][1]["refusal_reason"])

    def test_current_budget_includes_young_reservations(self):
        budget = dict(self.budget, available_gib=5.33, reserved_gib=2.0,
                      ramp_reservations_gib=2.0)
        text = self.manager._resource_refusal("medium", 1.5, budget)
        self.assertIn("usable now 0.33 GiB", text)
        self.assertIn("ramp reservations 2.00", text)
        self.assertIn("max after current holders finish 4.33 GiB", text)

    def test_heavy_override_is_recorded_and_explained(self):
        self.manager.enqueue("heavy", os.getpid(), token="heavy", job_class="heavy", mem_gib=3.0)
        entry = self.manager._read_queue()[0]
        self.assertEqual(entry["mem_gib"], 4.5)
        self.assertEqual(entry.get("requested_mem_gib"), 3.0)
        with mock.patch.object(self.manager, "_memory_budget", return_value=self.budget):
            text = self.manager.status()["queue"][0]["refusal_reason"]
        self.assertIn("requested 3.00 GiB", text)
        self.assertIn("heavy minimum 4.50 GiB", text)
        self.assertIn("source: HEAVY_RESERVATION_GIB", text)

    def test_acquire_preserves_original_heavy_request_for_refusal(self):
        output = io.StringIO()
        with mock.patch("build_slot.get_available_ram_gib", return_value=3.33), \
             mock.patch("build_slot.get_system_ram_percent", return_value=88.0), \
             redirect_stderr(output):
            self.assertFalse(self.manager.acquire("heavy", job_class="heavy", mem_gib=3.0,
                                                  timeout=0.05, poll_interval=0.01))
        self.assertIn("requested 3.00 GiB", output.getvalue())
        self.assertIn("heavy minimum 4.50 GiB", output.getvalue())

    def test_run_preserves_original_heavy_request_for_refusal(self):
        output = io.StringIO()
        with mock.patch("build_slot.get_available_ram_gib", return_value=3.33), \
             mock.patch("build_slot.get_system_ram_percent", return_value=88.0), \
             redirect_stderr(output):
            code = self.manager.run_command("heavy", [sys.executable, "-c", "print('unexpected')"],
                                            job_class="heavy", mem_gib=3.0,
                                            timeout=0.05, poll_interval=0.01)
        self.assertEqual(code, 1)
        self.assertIn("requested 3.00 GiB", output.getvalue())
        self.assertIn("heavy minimum 4.50 GiB", output.getvalue())

class TestAgingFitHysteresis(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.manager = BuildSlotManager(run_dir=self.directory.name)
        self.queue = [
            {"name": "aged", "token": "head", "job_class": "heavy", "mem_gib": 4.5, "enqueued_at": 1000.0},
            {"name": "medium", "token": "medium", "job_class": "medium", "mem_gib": 1.5, "enqueued_at": 1100.0},
        ]

    def poll(self, projected, index, manager=None):
        budget = {"available_gib": 4.6, "reserved_gib": projected - 1.6,
                  "floor_gib": 3.0, "free_budget_gib": 1.6, "heavy_jobs": 0, "ram_percent": 80.0}
        return (manager or self.manager)._queue_admission(
            self.queue, "medium", budget, 4000.0 + index * 5.0)[0]

    def test_fluctuating_capacity_never_starts_pause(self):
        for index, projected in enumerate([4.41, 4.49, 4.52, 4.47, 4.54, 4.45]):
            self.assertTrue(self.poll(projected, index), f"single spike {projected} must not pause")

    def test_three_stable_samples_start_pause(self):
        self.assertEqual([self.poll(5.1, index) for index in range(3)], [True, True, False])

    def test_drop_below_need_ends_pause_and_requires_new_stability(self):
        for index in range(3):
            self.poll(5.1, index)
        self.assertFalse(self.poll(5.1, 3))
        self.assertTrue(self.poll(4.49, 4))
        self.assertTrue(self.poll(5.1, 5))

    def test_workers_share_stability_samples(self):
        results = [self.poll(5.1, index, BuildSlotManager(run_dir=self.directory.name))
                   for index in range(3)]
        self.assertEqual(results, [True, True, False])

    def test_same_poll_does_not_count_each_waiter_as_a_sample(self):
        self.assertEqual([self.poll(5.1, 0) for _ in range(3)], [True, True, True])

    def test_capacity_without_half_gib_headroom_never_starts_pause(self):
        self.assertEqual([self.poll(4.8, index) for index in range(3)], [True, True, True])

if __name__ == "__main__":
    unittest.main()
