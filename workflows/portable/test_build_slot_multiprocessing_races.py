"""
Comprehensive Multiprocessing Race Verification Suite for build_slot.py
========================================================================

Validates concurrency invariants and race-freedom for build_slot.py
under multi-process contention on Windows NTFS (PR #695).

Strict requirements:
- Executes actual, unmodified product functions from build_slot.py.
- Hooks ONLY os.rename and os.rmdir via saved real OS operations and multiprocessing.Event synchronizations.
- Zero copied product methods.
- Real child processes via multiprocessing (spawn method).
- Real filesystem operations on Windows NTFS.
- Genuine PID lifecycle management (real dead PIDs).
- All Windows child processes created with CREATE_NO_WINDOW.
- Bounded waits and joins; thorough process cleanup in tearDown.

Tests:
  1. test_mp_ordinary_mutual_exclusion_contention
     - Verifies baseline contention: two concurrent processes contending for 1 slot
       never experience overlapping critical sections.
  2. test_slot_stale_reclaim_rename_race_detaches_live_owner
     - Verifies slot stale reclaim mutual exclusion under stable file guard.
     - Invariant 1: Contender P2 MUST BLOCK while P1 reclaimer holds guard in paused critical section.
     - Invariant 2: Successor P2 unblocks and acquires slot 0 after P1 reclaim finishes.
     - Invariant 3: P2 retains its lock intact (mutual exclusion, third entrant P3 blocked, clean release).
  3. test_queue_stale_reclaim_rename_race_detaches_live_owner
     - Verifies queue lock stale reclaim mutual exclusion under stable file guard.
     - Invariant 1: Contender P2 MUST BLOCK while P1 queue reclaimer holds guard in paused critical section.
     - Invariant 2: Successor P2 unblocks and enters queue critical section after P1 reclaim finishes.
     - Invariant 3: P2 retains its lock intact (mutual exclusion, third entrant P3 blocked, clean exit).
  4. test_release_retry_loop_deletes_fresh_successor
     - Verifies slot release mutual exclusion and successor directory retention under stable file guard.
     - Invariant 1: Contender P2 MUST BLOCK while P1 holds release guard in paused critical section.
     - Invariant 2: Successor P2 unblocks and acquires slot 0 after P1 release finishes.
     - Invariant 3: Successor slot lock directory remains intact (NOT deleted by P1 release loop).
"""

import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time
import unittest

# Profile policy section 13: CREATE_NO_WINDOW must be passed to all child processes on Windows
try:
    import _winapi
    _orig_CreateProcess = _winapi.CreateProcess
    def _hidden_CreateProcess(appName, cmdLine, procSec, thrdSec, inheritHnd, crtFlags, env, curDir, startInfo):
        crtFlags |= 0x08000000  # CREATE_NO_WINDOW
        return _orig_CreateProcess(appName, cmdLine, procSec, thrdSec, inheritHnd, crtFlags, env, curDir, startInfo)
    _winapi.CreateProcess = _hidden_CreateProcess
except Exception:
    pass

# Ensure host environment paths dynamically from local module
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, "..", ".."))

os.environ["BUILD_SLOT_RAM_PERCENT"] = "50.0"
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

current_pp = os.environ.get("PYTHONPATH", "")
extra_pp = os.pathsep.join([SCRIPT_DIR, REPO_ROOT])
os.environ["PYTHONPATH"] = f"{extra_pp}{os.pathsep}{current_pp}" if current_pp else extra_pp

import build_slot
from build_slot import (
    BuildSlotManager,
    _queue_atomic_lock,
    _read_queue_lock_info,
    QUEUE_LOCK_NAME,
    INFO_FILE_NAME,
    is_pid_alive,
)


def _dummy_exit_worker():
    """Worker that exits immediately to create a guaranteed dead PID."""
    sys.exit(0)


# =========================================================================
# Worker Functions for Test 1: Ordinary Contention
# =========================================================================

def _worker_ordinary_contention(run_dir: str, name: str, hold_duration: float, q_out: mp.Queue):
    """Contends for a single build slot using unmodified BuildSlotManager."""
    manager = BuildSlotManager(run_dir=run_dir, max_slots=1, acquisition_stagger=0.0)
    pid = os.getpid()
    acquired = manager.acquire(name, timeout=10.0, poll_interval=0.02)
    if not acquired:
        q_out.put({"name": name, "pid": pid, "acquired": False})
        return

    enter_time = time.time()
    time.sleep(hold_duration)
    exit_time = time.time()

    released = manager.release(name)
    q_out.put({
        "name": name,
        "pid": pid,
        "acquired": True,
        "enter_time": enter_time,
        "exit_time": exit_time,
        "released": released,
    })


# =========================================================================
# Worker Functions for Test 2: Slot Stale Reclaim Rename Race
# =========================================================================

def _p1_slot_reclaimer_worker(
    run_dir: str,
    evt_paused_before_rename: mp.Event,
    evt_resume_rename: mp.Event,
    q_out: mp.Queue,
):
    """P1 runs unmodified check_stale_and_reclaim, hooking os.rename around slot directory."""
    manager = BuildSlotManager(run_dir=run_dir, max_slots=1, acquisition_stagger=0.0)
    orig_rename = os.rename
    rename_error = None
    first_rename_done = False
    interleaving = []

    def hooked_rename(src, dst):
        nonlocal rename_error, first_rename_done
        if "build-slot.lock" in str(src) and not first_rename_done:
            first_rename_done = True
            interleaving.append("P1: Checked dead holder; pausing before real os.rename (guard held)")
            evt_paused_before_rename.set()
            evt_resume_rename.wait(5.0)
            interleaving.append("P1: Resumed rename")

        try:
            return orig_rename(src, dst)
        except OSError as e:
            rename_error = str(e)
            interleaving.append(f"P1: os.rename raised: {e}")
            raise

    os.rename = hooked_rename
    try:
        reclaimed_count = manager.check_stale_and_reclaim()
        interleaving.append(f"P1: Reclaim finished with reclaimed_count={reclaimed_count}")
        q_out.put({
            "role": "P1",
            "reclaimed_count": reclaimed_count,
            "rename_error": rename_error,
            "interleaving": interleaving,
        })
    finally:
        os.rename = orig_rename


def _p2_slot_owner_worker(
    run_dir: str,
    evt_p2_started: mp.Event,
    evt_p2_inside: mp.Event,
    evt_p2_exit: mp.Event,
    q_out: mp.Queue,
):
    """P2 attempts to acquire slot 0. Must block while P1 holds guard, then acquire after P1 ends."""
    interleaving = []
    manager = BuildSlotManager(run_dir=run_dir, max_slots=1, acquisition_stagger=0.0)
    evt_p2_started.set()
    interleaving.append("P2: Starting acquire attempt for slot 0")

    acquired = manager.acquire("live-owner-p2", timeout=5.0, poll_interval=0.02)
    slot_info = manager._read_slot_info(0)
    interleaving.append(f"P2: Acquire returned ok={acquired}, token={slot_info.get('token') if slot_info else None}")

    if acquired:
        evt_p2_inside.set()
        interleaving.append("P2: Inside critical section, waiting for exit signal")
        evt_p2_exit.wait(5.0)

        slot_info_before_release = manager._read_slot_info(0)
        s_dir_exists = os.path.exists(manager.slot_dirs[0])
        released = manager.release("live-owner-p2")
        interleaving.append(f"P2: Release returned ok={released}")
        q_out.put({
            "role": "P2",
            "pid": os.getpid(),
            "acquired": acquired,
            "released": released,
            "s_dir_exists_before_release": s_dir_exists,
            "slot_info": slot_info,
            "slot_info_before_release": slot_info_before_release,
            "interleaving": interleaving,
        })
    else:
        q_out.put({
            "role": "P2",
            "pid": os.getpid(),
            "acquired": False,
            "released": False,
            "interleaving": interleaving,
        })


def _p3_slot_entrant_worker(
    run_dir: str,
    evt_p3_start: mp.Event,
    evt_p3_done: mp.Event,
    q_out: mp.Queue,
):
    """P3 attempts acquire while P2 holds slot 0 (must fail/timeout)."""
    interleaving = []
    evt_p3_start.wait(5.0)
    interleaving.append("P3: Attempting acquire while P2 holds slot 0 (should fail/timeout)")

    manager = BuildSlotManager(run_dir=run_dir, max_slots=1, acquisition_stagger=0.0)
    acquired = manager.acquire("third-entrant-p3", timeout=0.2, poll_interval=0.02)
    interleaving.append(f"P3: Acquire attempt returned ok={acquired}")
    q_out.put({
        "role": "P3",
        "pid": os.getpid(),
        "acquired": acquired,
        "interleaving": interleaving,
    })
    evt_p3_done.set()


# =========================================================================
# Worker Functions for Test 3: Queue Lock Stale Reclaim Rename Race
# =========================================================================

def _p1_queue_reclaimer_worker(
    run_dir: str,
    evt_paused_before_rename: mp.Event,
    evt_resume_rename: mp.Event,
    q_out: mp.Queue,
):
    """P1 runs unmodified _tombstone_stale_queue_lock, hooking os.rename around queue lock dir."""
    queue_lock_dir = os.path.join(run_dir, QUEUE_LOCK_NAME)
    info = _read_queue_lock_info(queue_lock_dir)

    orig_rename = os.rename
    rename_error = None
    first_rename_done = False
    interleaving = []

    def hooked_rename(src, dst):
        nonlocal rename_error, first_rename_done
        if QUEUE_LOCK_NAME in str(src) and not first_rename_done:
            first_rename_done = True
            interleaving.append("P1 (Queue): Checked dead token/PID; pausing before real os.rename (guard held)")
            evt_paused_before_rename.set()
            evt_resume_rename.wait(5.0)
            interleaving.append("P1 (Queue): Resumed rename")

        try:
            return orig_rename(src, dst)
        except OSError as e:
            rename_error = str(e)
            interleaving.append(f"P1 (Queue): os.rename raised: {e}")
            raise

    os.rename = hooked_rename
    try:
        reclaimed = build_slot._tombstone_stale_queue_lock(queue_lock_dir, info, stale_after=15.0)
        interleaving.append(f"P1 (Queue): Reclaim finished with reclaimed={reclaimed}")
        q_out.put({
            "role": "P1",
            "reclaimed": reclaimed,
            "rename_error": rename_error,
            "interleaving": interleaving,
        })
    finally:
        os.rename = orig_rename


def _p2_queue_owner_worker(
    run_dir: str,
    evt_p2_started: mp.Event,
    evt_p2_inside: mp.Event,
    evt_p2_exit: mp.Event,
    q_out: mp.Queue,
):
    """P2 attempts to acquire queue lock. Must block while P1 holds guard, then enter after P1 ends."""
    interleaving = []
    evt_p2_started.set()
    interleaving.append("P2 (Queue): Starting acquire attempt for _queue_atomic_lock")

    try:
        with _queue_atomic_lock(run_dir, timeout=5.0):
            queue_lock_dir = os.path.join(run_dir, QUEUE_LOCK_NAME)
            info = _read_queue_lock_info(queue_lock_dir)
            interleaving.append(f"P2 (Queue): Entered queue critical section (token={info.get('token') if info else None})")
            evt_p2_inside.set()
            evt_p2_exit.wait(5.0)

            info_before_exit = _read_queue_lock_info(queue_lock_dir)
            interleaving.append("P2 (Queue): Exiting queue critical section")
            q_out.put({
                "role": "P2",
                "entered": True,
                "pid": os.getpid(),
                "info": info,
                "info_before_exit": info_before_exit,
                "interleaving": interleaving,
            })
    except Exception as e:
        interleaving.append(f"P2 (Queue): Failed with exception: {e}")
        q_out.put({
            "role": "P2",
            "entered": False,
            "pid": os.getpid(),
            "error": str(e),
            "interleaving": interleaving,
        })


def _p3_queue_entrant_worker(
    run_dir: str,
    evt_p3_start: mp.Event,
    evt_p3_done: mp.Event,
    q_out: mp.Queue,
):
    """P3 attempts to acquire queue lock while P2 is inside (must fail/timeout)."""
    interleaving = []
    evt_p3_start.wait(5.0)
    interleaving.append("P3 (Queue): Attempting _queue_atomic_lock while P2 holds it (should fail/timeout)")

    entered = False
    try:
        with _queue_atomic_lock(run_dir, timeout=0.2):
            entered = True
            interleaving.append("P3 (Queue): ERROR - entered queue critical section while P2 inside!")
    except (TimeoutError, Exception) as e:
        interleaving.append(f"P3 (Queue): Correctly failed/timed out: {type(e).__name__}")

    q_out.put({
        "role": "P3",
        "entered": entered,
        "pid": os.getpid(),
        "interleaving": interleaving,
    })
    evt_p3_done.set()


# =========================================================================
# Worker Functions for Test 4: Slot Release Retry Loop Deletes Successor
# =========================================================================

def _p1_releasing_worker(
    run_dir: str,
    evt_paused_in_release: mp.Event,
    evt_resume_release: mp.Event,
    q_out: mp.Queue,
):
    """
    P1 acquires slot 0 and calls manager.release('owner-p1').
    Hooks os.rmdir/os.rename: pauses P1 while release guard is held.
    Contender P2 attempts acquire while P1 is paused (must block).
    P1 resumes and completes release cleanly.
    """
    manager = BuildSlotManager(run_dir=run_dir, max_slots=1, acquisition_stagger=0.0)
    acq = manager.acquire("owner-p1", timeout=5.0)
    assert acq, "P1 should acquire slot 0"

    orig_rmdir = os.rmdir
    orig_rename = os.rename
    first_op_done = False
    interleaving = []

    def hooked_rmdir(path):
        nonlocal first_op_done
        orig_rmdir(path)
        if not first_op_done and "build-slot.lock" in str(path):
            first_op_done = True
            interleaving.append("P1: First real os.rmdir succeeded; pausing in release (guard held)")
            evt_paused_in_release.set()
            evt_resume_release.wait(5.0)
            interleaving.append("P1: Resumed release critical section")

    def hooked_rename(src, dst):
        nonlocal first_op_done
        if not first_op_done and "build-slot.lock" in str(src):
            first_op_done = True
            interleaving.append("P1: In release before/at rename; pausing in release (guard held)")
            evt_paused_in_release.set()
            evt_resume_release.wait(5.0)
            interleaving.append("P1: Resumed release critical section")
        return orig_rename(src, dst)

    os.rmdir = hooked_rmdir
    os.rename = hooked_rename
    try:
        rel = manager.release("owner-p1")
        interleaving.append(f"P1: Release completed with return={rel}")
        q_out.put({"role": "P1", "released": rel, "interleaving": interleaving})
    finally:
        os.rmdir = orig_rmdir
        os.rename = orig_rename


def _p2_successor_worker(
    run_dir: str,
    evt_p2_started: mp.Event,
    evt_p2_acquired: mp.Event,
    evt_p2_exit: mp.Event,
    q_out: mp.Queue,
):
    """
    P2 attempts to acquire fresh successor slot 0.
    Must block while P1 release holds guard, then acquire after P1 ends.
    Must retain slot lock intact (not deleted by P1 retry loop).
    """
    interleaving = []
    manager = BuildSlotManager(run_dir=run_dir, max_slots=1, acquisition_stagger=0.0)
    evt_p2_started.set()
    interleaving.append("P2: Starting acquire attempt for successor slot 0")

    acq = manager.acquire("successor-p2", timeout=5.0, poll_interval=0.01)
    slot_info_after_acq = manager._read_slot_info(0)
    interleaving.append(f"P2: Acquired successor slot 0: {acq}, token={slot_info_after_acq.get('token') if slot_info_after_acq else None}")

    if acq:
        evt_p2_acquired.set()
        evt_p2_exit.wait(5.0)

        slot_info_after_sleep = manager._read_slot_info(0)
        s_dir_exists = os.path.exists(manager.slot_dirs[0])
        released = manager.release("successor-p2")
        interleaving.append(f"P2: Exit complete, s_dir_exists={s_dir_exists}, released={released}")

        q_out.put({
            "role": "P2",
            "acquired": acq,
            "s_dir_exists_after": s_dir_exists,
            "slot_info_after": slot_info_after_sleep,
            "released": released,
            "interleaving": interleaving,
        })
    else:
        q_out.put({
            "role": "P2",
            "acquired": False,
            "s_dir_exists_after": False,
            "slot_info_after": None,
            "released": False,
            "interleaving": interleaving,
        })


# =========================================================================
# Unittest Test Suite
# =========================================================================

class TestBuildSlotMultiprocessingRaces(unittest.TestCase):
    def setUp(self):
        self.test_run_dir = tempfile.mkdtemp(prefix="test-bs-mp-race-")
        self._active_processes = []

    def tearDown(self):
        for p in self._active_processes:
            if p.is_alive():
                try:
                    p.terminate()
                    p.join(timeout=1.0)
                    if p.is_alive():
                        p.kill()
                        p.join(timeout=1.0)
                except Exception:
                    pass
        shutil.rmtree(self.test_run_dir, ignore_errors=True)

    def _create_genuinely_dead_pid(self) -> int:
        p = mp.Process(target=_dummy_exit_worker)
        self._active_processes.append(p)
        p.start()
        dead_pid = p.pid
        p.join(timeout=5.0)
        while is_pid_alive(dead_pid):
            time.sleep(0.01)
        return dead_pid

    def test_mp_ordinary_mutual_exclusion_contention(self):
        """
        Baseline test: 2 real processes contend for 1 slot using unmodified product code.
        Zero critical section overlaps must occur.
        """
        q = mp.Queue()
        p1 = mp.Process(target=_worker_ordinary_contention, args=(self.test_run_dir, "procA-job", 0.15, q))
        p2 = mp.Process(target=_worker_ordinary_contention, args=(self.test_run_dir, "procB-job", 0.15, q))
        self._active_processes.extend([p1, p2])

        p1.start()
        p2.start()
        p1.join(timeout=10.0)
        p2.join(timeout=10.0)

        results = []
        while not q.empty():
            results.append(q.get())

        self.assertEqual(len(results), 2, "Both processes should return results")
        res_a = next(r for r in results if r["name"] == "procA-job")
        res_b = next(r for r in results if r["name"] == "procB-job")

        self.assertTrue(res_a["acquired"], "procA-job must acquire")
        self.assertTrue(res_b["acquired"], "procB-job must acquire")
        self.assertTrue(res_a["released"], "procA-job must release")
        self.assertTrue(res_b["released"], "procB-job must release")

        overlap = not (res_a["exit_time"] <= res_b["enter_time"] or res_b["exit_time"] <= res_a["enter_time"])
        self.assertFalse(overlap, f"Critical sections overlapped! A: {res_a}, B: {res_b}")

    def test_slot_stale_reclaim_rename_race_detaches_live_owner(self):
        """
        Verifies stale slot reclaim mutual exclusion under stable file guard:
        1. Slot 0 has dead PID info.
        2. Reclaimer P1 checks dead holder and pauses before rename to tombstone (guard held).
        3. Contender P2 attempts to acquire slot 0.
        4. Invariant 1: P2 MUST BLOCK while P1 holds guard in paused critical section.
        5. P1 is unblocked, completes reclaim, and exits.
        6. Invariant 2: Successor P2 unblocks and acquires slot 0.
        7. Invariant 3: P2 retains its lock after old actor P1 ends (mutual exclusion,
           P3 cannot acquire while P2 holds slot 0, P2 releases cleanly).
        """
        dead_pid = self._create_genuinely_dead_pid()
        slot_0_dir = os.path.join(self.test_run_dir, "build-slot.lock")
        os.makedirs(slot_0_dir, exist_ok=True)
        with open(os.path.join(slot_0_dir, INFO_FILE_NAME), "w", encoding="utf-8") as f:
            json.dump({
                "owner": "dead-previous-holder",
                "pid": dead_pid,
                "token": "dead-token-1234",
                "slot": 0,
                "acquired_at": "2026-10-08T00:00:00+00:00",
                "acquired_at_epoch": time.time() - 200,
                "heartbeat_at": "2026-10-08T00:00:00+00:00",
                "heartbeat_at_epoch": time.time() - 200,
            }, f)

        evt_paused_before_rename = mp.Event()
        evt_resume_rename = mp.Event()
        evt_p2_started = mp.Event()
        evt_p2_inside = mp.Event()
        evt_p2_exit = mp.Event()
        evt_p3_start = mp.Event()
        evt_p3_done = mp.Event()

        q = mp.Queue()

        p1 = mp.Process(target=_p1_slot_reclaimer_worker, args=(
            self.test_run_dir,
            evt_paused_before_rename,
            evt_resume_rename,
            q,
        ))
        p2 = mp.Process(target=_p2_slot_owner_worker, args=(
            self.test_run_dir,
            evt_p2_started,
            evt_p2_inside,
            evt_p2_exit,
            q,
        ))
        p3 = mp.Process(target=_p3_slot_entrant_worker, args=(
            self.test_run_dir,
            evt_p3_start,
            evt_p3_done,
            q,
        ))
        self._active_processes.extend([p1, p2, p3])

        p1.start()
        self.assertTrue(evt_paused_before_rename.wait(5.0), "P1 timed out pausing before rename")

        # P1 is now holding the guard in its paused critical section.
        # Start contender P2.
        p2.start()
        self.assertTrue(evt_p2_started.wait(5.0), "P2 timed out starting acquire")

        # Crucial invariant: Contender P2 must block and NOT acquire while P1 holds guard!
        p2_acquired_prematurely = evt_p2_inside.wait(0.4)
        self.assertFalse(
            p2_acquired_prematurely,
            "Contender P2 must block while P1 reclaimer holds guard before rename"
        )

        # Allow P1 to finish its critical section
        evt_resume_rename.set()
        p1.join(5.0)
        self.assertFalse(p1.is_alive(), "P1 timed out completing reclaim")

        # Now that P1 ended, successor P2 unblocks and acquires slot 0
        self.assertTrue(evt_p2_inside.wait(5.0), "Successor P2 timed out acquiring slot 0 after P1 ended")

        # While P2 holds slot 0, third entrant P3 attempts acquire and must fail
        p3.start()
        evt_p3_start.set()
        self.assertTrue(evt_p3_done.wait(5.0), "P3 timed out")
        p3.join(5.0)

        # Release P2 and verify clean release
        evt_p2_exit.set()
        p2.join(5.0)

        results = {}
        while not q.empty():
            item = q.get()
            results[item.get("role")] = item

        p1_res = results.get("P1", {})
        p2_res = results.get("P2", {})
        p3_res = results.get("P3", {})

        print("\n=== OBSERVED SLOT GUARD INTERLEAVING ===")
        print(f"P1: {p1_res.get('interleaving')}")
        print(f"P2: {p2_res.get('interleaving')}")
        print(f"P3: {p3_res.get('interleaving')}")
        print("========================================\n")

        self.assertIsNone(p1_res.get("rename_error"), f"P1 rename raised: {p1_res.get('rename_error')}")
        self.assertTrue(p2_res.get("acquired"), "P2 must have acquired slot 0")
        self.assertTrue(p2_res.get("s_dir_exists_before_release"), "P2 slot directory must exist before release")
        self.assertTrue(p2_res.get("released"), "P2 must release slot 0 cleanly")
        self.assertFalse(p3_res.get("acquired"), "P3 must not acquire while P2 holds slot 0 (mutual exclusion)")

    def test_queue_stale_reclaim_rename_race_detaches_live_owner(self):
        """
        Verifies stale queue lock reclaim mutual exclusion under stable file guard:
        1. Queue lock has dead token/PID info.
        2. Reclaimer P1 checks dead lock and pauses before rename to tombstone (guard held).
        3. Contender P2 attempts _queue_atomic_lock().
        4. Invariant 1: P2 MUST BLOCK while P1 holds guard in paused critical section.
        5. P1 is unblocked, completes queue reclaim, and exits.
        6. Invariant 2: Successor P2 unblocks and enters queue critical section.
        7. Invariant 3: P2 retains its lock after old actor P1 ends (mutual exclusion,
           P3 cannot enter while P2 holds queue lock, P2 exits cleanly).
        """
        dead_pid = self._create_genuinely_dead_pid()
        queue_lock_dir = os.path.join(self.test_run_dir, QUEUE_LOCK_NAME)
        os.makedirs(queue_lock_dir, exist_ok=True)
        with open(os.path.join(queue_lock_dir, INFO_FILE_NAME), "w", encoding="utf-8") as f:
            json.dump({
                "pid": dead_pid,
                "token": "dead-queue-token-1234",
                "acquired_at_epoch": time.time() - 200,
            }, f)

        evt_paused_before_rename = mp.Event()
        evt_resume_rename = mp.Event()
        evt_p2_started = mp.Event()
        evt_p2_inside = mp.Event()
        evt_p2_exit = mp.Event()
        evt_p3_start = mp.Event()
        evt_p3_done = mp.Event()

        q = mp.Queue()

        p1 = mp.Process(target=_p1_queue_reclaimer_worker, args=(
            self.test_run_dir,
            evt_paused_before_rename,
            evt_resume_rename,
            q,
        ))
        p2 = mp.Process(target=_p2_queue_owner_worker, args=(
            self.test_run_dir,
            evt_p2_started,
            evt_p2_inside,
            evt_p2_exit,
            q,
        ))
        p3 = mp.Process(target=_p3_queue_entrant_worker, args=(
            self.test_run_dir,
            evt_p3_start,
            evt_p3_done,
            q,
        ))
        self._active_processes.extend([p1, p2, p3])

        p1.start()
        self.assertTrue(evt_paused_before_rename.wait(5.0), "P1 timed out pausing before queue rename")

        # P1 is holding the guard. Start contender P2.
        p2.start()
        self.assertTrue(evt_p2_started.wait(5.0), "P2 timed out starting queue acquire")

        # Crucial invariant: Contender P2 must block and NOT enter while P1 holds guard!
        p2_entered_prematurely = evt_p2_inside.wait(0.4)
        self.assertFalse(
            p2_entered_prematurely,
            "Contender P2 must block while P1 queue reclaimer holds guard before rename"
        )

        # Allow P1 to finish its critical section
        evt_resume_rename.set()
        p1.join(5.0)
        self.assertFalse(p1.is_alive(), "P1 timed out completing queue reclaim")

        # Now that P1 ended, successor P2 unblocks and enters queue critical section
        self.assertTrue(evt_p2_inside.wait(5.0), "Successor P2 timed out entering queue critical section")

        # While P2 holds queue lock, third entrant P3 attempts acquire and must fail
        p3.start()
        evt_p3_start.set()
        self.assertTrue(evt_p3_done.wait(5.0), "P3 timed out")
        p3.join(5.0)

        # Release P2 and verify clean exit
        evt_p2_exit.set()
        p2.join(5.0)

        results = {}
        while not q.empty():
            item = q.get()
            results[item.get("role")] = item

        p1_res = results.get("P1", {})
        p2_res = results.get("P2", {})
        p3_res = results.get("P3", {})

        print("\n=== OBSERVED QUEUE GUARD INTERLEAVING ===")
        print(f"P1: {p1_res.get('interleaving')}")
        print(f"P2: {p2_res.get('interleaving')}")
        print(f"P3: {p3_res.get('interleaving')}")
        print("=========================================\n")

        self.assertIsNone(p1_res.get("rename_error"), f"P1 rename raised: {p1_res.get('rename_error')}")
        self.assertTrue(p2_res.get("entered"), "P2 must have entered queue critical section")
        self.assertFalse(p3_res.get("entered"), "P3 must not enter queue lock while P2 holds it (mutual exclusion)")

    def test_release_retry_loop_deletes_fresh_successor(self):
        """
        Verifies slot release mutual exclusion and successor retention under stable file guard:
        1. P1 acquires slot 0 and begins release('owner-p1').
        2. Hook on os.rmdir: after first real rmdir succeeds, pauses P1 while release guard is held.
        3. Contender P2 attempts to acquire fresh successor slot 0.
        4. Invariant 1: P2 MUST BLOCK while P1 release holds guard.
        5. P1 is unblocked, completes release, and exits.
        6. Invariant 2: Successor P2 unblocks and acquires slot 0.
        7. Invariant 3: P2 retains its slot lock directory (NOT deleted by P1 release retry loop).
        8. P2 releases slot 0 cleanly.
        """
        evt_paused_in_release = mp.Event()
        evt_resume_release = mp.Event()
        evt_p2_started = mp.Event()
        evt_p2_acquired = mp.Event()
        evt_p2_exit = mp.Event()
        q = mp.Queue()

        p1 = mp.Process(target=_p1_releasing_worker, args=(
            self.test_run_dir,
            evt_paused_in_release,
            evt_resume_release,
            q,
        ))
        p2 = mp.Process(target=_p2_successor_worker, args=(
            self.test_run_dir,
            evt_p2_started,
            evt_p2_acquired,
            evt_p2_exit,
            q,
        ))
        self._active_processes.extend([p1, p2])

        p1.start()
        self.assertTrue(evt_paused_in_release.wait(5.0), "P1 timed out reaching pause in release")

        # P1 is paused inside release. Start successor P2.
        p2.start()
        self.assertTrue(evt_p2_started.wait(5.0), "P2 timed out starting acquire")

        # Crucial invariant: P2 must block and NOT acquire while P1 release holds guard!
        p2_acquired_prematurely = evt_p2_acquired.wait(0.4)
        self.assertFalse(
            p2_acquired_prematurely,
            "Contender P2 must block while P1 release holds guard"
        )

        # Allow P1 to resume and finish release
        evt_resume_release.set()
        p1.join(5.0)
        self.assertFalse(p1.is_alive(), "P1 timed out completing release")

        # Now that P1 release has finished, successor P2 unblocks and acquires
        self.assertTrue(evt_p2_acquired.wait(5.0), "Successor P2 timed out acquiring slot 0 after P1 ended")

        # Allow P2 to verify slot directory retention and exit
        evt_p2_exit.set()
        p2.join(5.0)

        results = {}
        while not q.empty():
            item = q.get()
            results[item.get("role")] = item

        p1_res = results.get("P1", {})
        p2_res = results.get("P2", {})

        print("\n=== OBSERVED RELEASE RETRY LOOP INTERLEAVING ===")
        print(f"P1: {p1_res.get('interleaving')}")
        print(f"P2: {p2_res.get('interleaving')}")
        print("================================================\n")

        self.assertTrue(p1_res.get("released"), "P1 release should report success")
        self.assertTrue(p2_res.get("acquired"), "P2 should have acquired fresh successor slot")

        # Crucial invariant: P2's acquired slot directory MUST NOT be deleted by P1's release loop!
        self.assertTrue(
            p2_res.get("s_dir_exists_after"),
            "Successor slot lock directory must remain intact and not deleted by P1!"
        )
        self.assertTrue(p2_res.get("released"), "P2 release should report success")


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)
    unittest.main()
