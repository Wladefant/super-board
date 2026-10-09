#!/usr/bin/env python3
"""
test_build_slot_nested.py - Unit and regression tests for nested build slot reservations.

Validates the nested reservation contract in Wladefant/super-board build_slot.py:
- Marker BUILD_SLOT_HELD is JSON with owner, pid, token, job_class, mem_gib.
- Saturated budget nested acquire starts immediately with same reservation without enqueue,
  with exact reserved total unchanged.
- Real build_slot.py run propagates BUILD_SLOT_HELD to child command environment.
- Nested run executes with same marker, avoids double reservation, and does not release holder.
- Child release returns true without releasing any live holder.
- Class and memory escalation fail; nested run escalation is refused and command never executes.
- Inherited lock in another run-dir cannot bypass admission.
- Forged memory and class markers cannot bypass admission.
- Nested run timeout returns 124 and holder slot is retained.
- Operator freeze under valid marker refuses with exit code 75.
- Windows nested child job path executes cleanly with nested Job Objects.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

# Ensure workflows/portable is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import build_slot
from build_slot import BuildSlotManager


class TestBuildSlotNested(unittest.TestCase):
    """Test suite for nested build slot acquisition, marker propagation, and release contracts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="build-slot-nested-")
        self.run_dir = self.tmp.name

        self.orig_held = os.environ.get("BUILD_SLOT_HELD")
        os.environ.pop("BUILD_SLOT_HELD", None)

        self.orig_ram = os.environ.get("BUILD_SLOT_RAM_PERCENT")
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "50.0"

        self.orig_avail_gib = os.environ.get("BUILD_SLOT_AVAILABLE_GIB")
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "64.0"

        self.orig_stagger = os.environ.get("BUILD_SLOT_STAGGER_SECONDS")
        os.environ["BUILD_SLOT_STAGGER_SECONDS"] = "0"

        self.orig_allow_force = os.environ.get("BUILD_SLOT_ALLOW_FORCE")

    def tearDown(self):
        if self.orig_held is not None:
            os.environ["BUILD_SLOT_HELD"] = self.orig_held
        else:
            os.environ.pop("BUILD_SLOT_HELD", None)

        if self.orig_ram is not None:
            os.environ["BUILD_SLOT_RAM_PERCENT"] = self.orig_ram
        else:
            os.environ.pop("BUILD_SLOT_RAM_PERCENT", None)

        if self.orig_avail_gib is not None:
            os.environ["BUILD_SLOT_AVAILABLE_GIB"] = self.orig_avail_gib
        else:
            os.environ.pop("BUILD_SLOT_AVAILABLE_GIB", None)

        if self.orig_stagger is not None:
            os.environ["BUILD_SLOT_STAGGER_SECONDS"] = self.orig_stagger
        else:
            os.environ.pop("BUILD_SLOT_STAGGER_SECONDS", None)

        if self.orig_allow_force is not None:
            os.environ["BUILD_SLOT_ALLOW_FORCE"] = self.orig_allow_force
        else:
            os.environ.pop("BUILD_SLOT_ALLOW_FORCE", None)

        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def test_saturated_budget_nested_acquire_starts_immediately_with_same_reservation(self):
        """A nested acquire with a valid live marker starts immediately under saturated budget without enqueue."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(
            manager.acquire("parent-holder", job_class="medium", mem_gib=1.5, timeout=2.0),
            "Parent slot must acquire successfully",
        )
        info = manager._read_slot_info(0)
        self.assertIsNotNone(info)

        # Saturate budget: 0 available RAM, 99% RAM usage
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "0.0"
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "99.0"

        # Baseline: ordinary acquire fails / times out under saturated budget
        self.assertFalse(
            manager.acquire("ordinary-blocked", timeout=0.1),
            "Ordinary acquire must fail under saturated budget",
        )

        # Set valid BUILD_SLOT_HELD marker matching the live slot
        marker = {
            "owner": "parent-holder",
            "pid": info["pid"],
            "token": info["token"],
            "job_class": "medium",
            "mem_gib": 1.5,
        }
        os.environ["BUILD_SLOT_HELD"] = json.dumps(marker)

        # Check memory budget before nested acquire
        budget_before = manager._memory_budget()
        reserved_before = budget_before["reserved_gib"]
        self.assertEqual(reserved_before, 1.5, "Initial holder reservation must be exactly 1.5 GiB")

        # Nested acquire under saturated budget must start immediately
        t0 = time.monotonic()
        acquired = manager.acquire("child-lane", job_class="medium", mem_gib=1.5, timeout=1.0)
        elapsed = time.monotonic() - t0
        self.assertTrue(
            acquired,
            "Nested acquire under saturated budget must start immediately with same reservation",
        )
        self.assertLess(elapsed, 0.5, "Nested acquire must start immediately without queue waiting")

        # Contract: Nested acquire returns true without enqueue
        queue = manager._read_queue()
        self.assertEqual(queue, [], "Nested acquire must not enqueue into queue.json")

        # Contract: Reuses same reservation, no second slot directory allocated
        held_dirs = [d for d in manager.slot_dirs if os.path.isdir(d)]
        self.assertEqual(
            len(held_dirs), 1,
            "Nested acquire must reuse existing reservation without creating a second slot",
        )

        # Contract: Exact reserved total remains unchanged, not only slot count
        budget_after = manager._memory_budget()
        self.assertEqual(
            budget_after["reserved_gib"], reserved_before,
            f"Exact reserved memory total must remain unchanged ({reserved_before} GiB), got {budget_after['reserved_gib']} GiB",
        )
        self.assertEqual(
            budget_after["ramp_reservations_gib"], budget_before["ramp_reservations_gib"],
            "Ramp reservations total must remain unchanged after nested acquire",
        )
    def test_real_run_propagates_marker(self):
        """build_slot.py run propagates BUILD_SLOT_HELD JSON marker to child command environment."""
        marker_file = os.path.join(self.run_dir, "observed_marker.json")
        probe_code = (
            "import os, json, sys; "
            "val = os.environ.get('BUILD_SLOT_HELD'); "
            "open(sys.argv[1], 'w').write(val or '')"
        )
        cmd = [
            sys.executable,
            os.path.join(SCRIPT_DIR, "build_slot.py"),
            "--run-dir", self.run_dir,
            "run", "probe-holder",
            "--class", "medium",
            "--mem-gib", "1.5",
            "--",
            sys.executable, "-c", probe_code, marker_file,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.returncode, 0, f"build_slot.py run failed: {proc.stderr}")
        self.assertTrue(os.path.exists(marker_file))
        with open(marker_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
        self.assertTrue(bool(content), "Real run must propagate BUILD_SLOT_HELD marker to child process")

        marker = json.loads(content)
        self.assertEqual(marker.get("owner"), "probe-holder")
        self.assertEqual(marker.get("job_class"), "medium")
        self.assertEqual(marker.get("mem_gib"), 1.5)
        self.assertIsInstance(marker.get("pid"), int)
        self.assertTrue(bool(marker.get("token")), "Marker must include valid token")

    def test_nested_run_no_double_reservation(self):
        """Nested run executes under existing marker, avoids double reservation, and preserves holder."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(
            manager.acquire("holder", job_class="medium", mem_gib=1.5, timeout=2.0),
            "Holder slot must acquire successfully",
        )
        info = manager._read_slot_info(0)
        self.assertIsNotNone(info)

        marker = {
            "owner": "holder",
            "pid": info["pid"],
            "token": info["token"],
            "job_class": "medium",
            "mem_gib": 1.5,
        }
        os.environ["BUILD_SLOT_HELD"] = json.dumps(marker)

        snapshot_file = os.path.join(self.run_dir, "slots_during_nested_run.json")
        nested_script = (
            "import os, json, sys; "
            "run_dir = sys.argv[1]; "
            "slots = [n for n in os.listdir(run_dir) if n.startswith('build-slot') and n.endswith('.lock')]; "
            "open(sys.argv[2], 'w').write(json.dumps(slots))"
        )
        exit_code = manager.run_command(
            name="nested-worker",
            cmd=[sys.executable, "-c", nested_script, self.run_dir, snapshot_file],
            job_class="medium",
            mem_gib=1.5,
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(os.path.exists(snapshot_file))
        with open(snapshot_file, "r", encoding="utf-8") as f:
            slots_during_run = json.load(f)

        self.assertEqual(
            len(slots_during_run), 1,
            f"Nested run must not create a double reservation (found slots: {slots_during_run})",
        )

        # Holder must remain held after nested run completes
        self.assertTrue(
            manager.is_held_by("holder", token=info["token"]),
            "Holder slot must remain held after nested run completion",
        )
        self.assertTrue(os.path.isdir(manager.slot_dirs[0]))

    def test_child_release_cannot_remove_holder(self):
        """Nested release returns true without releasing any live holder."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(
            manager.acquire("holder", job_class="heavy", mem_gib=5.0, timeout=2.0),
            "Holder slot must acquire successfully",
        )
        info = manager._read_slot_info(0)
        self.assertIsNotNone(info)

        marker = {
            "owner": "holder",
            "pid": info["pid"],
            "token": info["token"],
            "job_class": "heavy",
            "mem_gib": 5.0,
        }
        os.environ["BUILD_SLOT_HELD"] = json.dumps(marker)

        # Case A: Child name release
        child_release_result = manager.release("child-lane")
        self.assertTrue(child_release_result, "Nested release for child must return True")
        self.assertTrue(
            manager.is_held_by("holder", token=info["token"]),
            "Holder slot must not be released by child release",
        )
        self.assertTrue(os.path.isdir(manager.slot_dirs[0]))

        # Case B: Nested context release with holder's name from child PID
        with mock.patch("os.getpid", return_value=info["pid"] + 9999):
            nested_holder_release = manager.release("holder")
            self.assertTrue(nested_holder_release, "Nested release must return True")
            self.assertTrue(
                manager.is_held_by("holder", token=info["token"]),
                "Nested release must not remove live holder slot",
            )

    def test_class_escalation_fails(self):
        """Requesting a class larger than holder (order: light < browser < medium < heavy) fails."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        # Holder has "browser" (rank: light=0, browser=1, medium=2, heavy=3)
        self.assertTrue(
            manager.acquire("holder-browser", job_class="browser", mem_gib=1.1, timeout=2.0),
            "Browser slot must acquire successfully",
        )
        info = manager._read_slot_info(0)
        self.assertIsNotNone(info)

        marker = {
            "owner": "holder-browser",
            "pid": info["pid"],
            "token": info["token"],
            "job_class": "browser",
            "mem_gib": 1.1,
        }
        os.environ["BUILD_SLOT_HELD"] = json.dumps(marker)

        # Escalation to "medium" (larger class) must fail
        escalated_med = manager.acquire("child-med", job_class="medium", mem_gib=1.5, timeout=0.1)
        self.assertFalse(escalated_med, "Class escalation from browser to medium must fail")

        # Escalation to "heavy" (larger class) must fail
        escalated_heavy = manager.acquire("child-heavy", job_class="heavy", mem_gib=5.0, timeout=0.1)
        self.assertFalse(escalated_heavy, "Class escalation from browser to heavy must fail")

        # Same class ("browser") or smaller class ("light") must succeed
        self.assertTrue(
            manager.acquire("child-browser", job_class="browser", mem_gib=1.1, timeout=0.5),
            "Same class nested acquire must succeed",
        )
        self.assertTrue(
            manager.acquire("child-light", job_class="light", mem_gib=0.5, timeout=0.5),
            "Smaller class nested acquire must succeed",
        )

    def test_memory_escalation_fails(self):
        """Requesting higher memory than holder fails."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(
            manager.acquire("holder-mem", job_class="medium", mem_gib=1.5, timeout=2.0),
            "Medium slot must acquire successfully",
        )
        info = manager._read_slot_info(0)
        self.assertIsNotNone(info)

        marker = {
            "owner": "holder-mem",
            "pid": info["pid"],
            "token": info["token"],
            "job_class": "medium",
            "mem_gib": 1.5,
        }
        os.environ["BUILD_SLOT_HELD"] = json.dumps(marker)

        # Memory escalation to 2.5 GiB (higher memory) must fail
        escalated_mem = manager.acquire("child-mem-high", job_class="medium", mem_gib=2.5, timeout=0.1)
        self.assertFalse(escalated_mem, "Memory escalation from 1.5 GiB to 2.5 GiB must fail")

        # Same memory (1.5 GiB) or lower memory (1.0 GiB) must succeed
        self.assertTrue(
            manager.acquire("child-mem-same", job_class="medium", mem_gib=1.5, timeout=0.5),
            "Same memory nested acquire must succeed",
        )
        self.assertTrue(
            manager.acquire("child-mem-low", job_class="medium", mem_gib=1.0, timeout=0.5),
            "Lower memory nested acquire must succeed",
        )

    def test_invalid_dead_wrong_token_markers_cannot_bypass_admission(self):
        """Invalid, dead PID, and wrong-token markers cannot bypass admission; invalid markers use ordinary admission."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(
            manager.acquire("holder", job_class="medium", mem_gib=1.5, timeout=2.0),
            "Holder slot must acquire successfully",
        )
        info = manager._read_slot_info(0)
        self.assertIsNotNone(info)

        # Saturated budget: ordinary admission cannot admit
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "0.0"
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "99.0"

        # Contrast assertion: under saturated budget, a VALID marker DOES bypass admission
        valid_marker = {
            "owner": "holder",
            "pid": info["pid"],
            "token": info["token"],
            "job_class": "medium",
            "mem_gib": 1.5,
        }
        os.environ["BUILD_SLOT_HELD"] = json.dumps(valid_marker)
        valid_acquired = manager.acquire("valid-bypass", timeout=0.5)
        self.assertTrue(valid_acquired, "Valid marker must bypass saturated budget admission")

        # 1. Malformed JSON
        os.environ["BUILD_SLOT_HELD"] = "not-json-{{"
        self.assertFalse(
            manager.acquire("test-malformed", timeout=0.1),
            "Malformed JSON marker must not bypass admission under saturated budget",
        )

        # 2. Missing required fields (e.g. missing token)
        os.environ["BUILD_SLOT_HELD"] = json.dumps({"owner": "holder", "pid": info["pid"]})
        self.assertFalse(
            manager.acquire("test-missing-fields", timeout=0.1),
            "Marker with missing fields must not bypass admission under saturated budget",
        )

        # 3. Wrong token (not matching any live slot)
        os.environ["BUILD_SLOT_HELD"] = json.dumps({
            "owner": "holder",
            "pid": info["pid"],
            "token": "wrong-token-not-matching-slot",
            "job_class": "medium",
            "mem_gib": 1.5,
        })
        self.assertFalse(
            manager.acquire("test-wrong-token", timeout=0.1),
            "Wrong-token marker must not bypass admission under saturated budget",
        )

        # 4. Dead PID
        with mock.patch.object(manager, "is_pid_alive", return_value=False):
            os.environ["BUILD_SLOT_HELD"] = json.dumps({
                "owner": "holder",
                "pid": info["pid"],
                "token": info["token"],
                "job_class": "medium",
                "mem_gib": 1.5,
            })
            self.assertFalse(
                manager.acquire("test-dead-pid", timeout=0.1),
                "Dead PID marker must not bypass admission under saturated budget",
            )

        # 5. Non-existent slot directory on manager run_dir
        other_run_dir = os.path.join(self.run_dir, "other_sub_dir")
        other_manager = BuildSlotManager(run_dir=other_run_dir)
        os.environ["BUILD_SLOT_HELD"] = json.dumps({
            "owner": "holder",
            "pid": info["pid"],
            "token": info["token"],
            "job_class": "medium",
            "mem_gib": 1.5,
        })
        self.assertFalse(
            other_manager.acquire("test-other-dir", timeout=0.1),
            "Marker not matching live slot on manager run_dir must not bypass admission",
        )

        # 6. Contract: Invalid markers use ordinary admission when resources are available
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "64.0"
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "50.0"
        os.environ["BUILD_SLOT_HELD"] = "not-json"
        ordinary_ok = manager.acquire("ordinary-admit", timeout=1.0)
        self.assertTrue(ordinary_ok, "Invalid marker must fall back to ordinary admission when budget allows")
        self.assertTrue(manager.is_held_by("ordinary-admit"))

    def test_real_subprocess_nested_run_propagation_and_no_double_reservation(self):
        """Real subprocess execution proves nested build_slot.py run reuses marker without double reservation."""
        output_file = os.path.join(self.run_dir, "nested_proc_output.json")
        inner_probe = (
            "import os, json, sys; "
            "run_dir = sys.argv[1]; "
            "marker = os.environ.get('BUILD_SLOT_HELD'); "
            "slots = [n for n in os.listdir(run_dir) if n.startswith('build-slot') and n.endswith('.lock')]; "
            "res = {'marker': marker, 'slots': slots}; "
            "open(sys.argv[2], 'w').write(json.dumps(res))"
        )
        outer_script = (
            "import subprocess, sys, os; "
            "build_slot_script = sys.argv[1]; "
            "run_dir = sys.argv[2]; "
            "output_file = sys.argv[3]; "
            "inner_code = sys.argv[4]; "
            "cmd = [sys.executable, build_slot_script, '--run-dir', run_dir, 'run', 'child-runner', "
            "'--class', 'light', '--', sys.executable, '-c', inner_code, run_dir, output_file]; "
            "proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, "
            "creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)); "
            "sys.exit(proc.returncode)"
        )
        cmd = [
            sys.executable,
            os.path.join(SCRIPT_DIR, "build_slot.py"),
            "--run-dir", self.run_dir,
            "run", "parent-runner",
            "--class", "medium",
            "--mem-gib", "1.5",
            "--",
            sys.executable, "-c", outer_script,
            os.path.join(SCRIPT_DIR, "build_slot.py"),
            self.run_dir,
            output_file,
            inner_probe,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.returncode, 0, f"Outer run failed: {proc.stderr}\nSTDOUT: {proc.stdout}")
        self.assertTrue(os.path.exists(output_file))
        with open(output_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIsNotNone(data.get("marker"), "Nested child must receive BUILD_SLOT_HELD")
        marker_data = json.loads(data["marker"])
        self.assertEqual(marker_data.get("owner"), "parent-runner")
        self.assertEqual(
            len(data.get("slots", [])), 1,
            f"Nested run must not allocate second slot (found: {data.get('slots')})",
        )


    def test_nested_run_escalation_refuses_and_command_never_executes(self):
        """Adversarial check: Nested run escalation is refused and command never executes."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(
            manager.acquire("parent-holder", job_class="light", mem_gib=0.5, timeout=2.0),
            "Parent slot must acquire successfully",
        )
        info = manager._read_slot_info(0)
        self.assertIsNotNone(info)

        marker = {
            "owner": "parent-holder",
            "pid": info["pid"],
            "token": info["token"],
            "job_class": "light",
            "mem_gib": 0.5,
        }
        os.environ["BUILD_SLOT_HELD"] = json.dumps(marker)

        # 1. API: Class escalation in run_command (light -> heavy)
        canary_class = os.path.join(self.run_dir, "canary_class_escalation.txt")
        cmd_class = [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write('escalated')", canary_class]
        rc_class = manager.run_command(
            name="escalated-class-child",
            cmd=cmd_class,
            job_class="heavy",
            mem_gib=5.0,
        )
        self.assertEqual(rc_class, 1, "Class escalation must return exit code 1")
        self.assertFalse(
            os.path.exists(canary_class),
            "Command must never execute upon class escalation",
        )
        self.assertTrue(
            manager.is_held_by("parent-holder", token=info["token"]),
            "Parent slot must remain held after refused escalation",
        )

        # 2. API: Memory escalation in run_command (0.5 GiB -> 2.5 GiB in light class)
        canary_mem = os.path.join(self.run_dir, "canary_mem_escalation.txt")
        cmd_mem = [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write('escalated')", canary_mem]
        rc_mem = manager.run_command(
            name="escalated-mem-child",
            cmd=cmd_mem,
            job_class="light",
            mem_gib=2.5,
        )
        self.assertEqual(rc_mem, 1, "Memory escalation must return exit code 1")
        self.assertFalse(
            os.path.exists(canary_mem),
            "Command must never execute upon memory escalation",
        )
        self.assertTrue(
            manager.is_held_by("parent-holder", token=info["token"]),
            "Parent slot must remain held after refused memory escalation",
        )

        # 3. CLI Subprocess: CLI run command refusal
        canary_cli = os.path.join(self.run_dir, "canary_cli_escalation.txt")
        cmd_cli = [
            sys.executable,
            os.path.join(SCRIPT_DIR, "build_slot.py"),
            "--run-dir", self.run_dir,
            "run", "cli-escalated-child",
            "--class", "heavy",
            "--mem-gib", "5.0",
            "--",
            sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write('escalated')", canary_cli,
        ]
        proc = subprocess.run(
            cmd_cli,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.returncode, 1, f"CLI escalation run must exit with 1: {proc.stderr}")
        self.assertFalse(
            os.path.exists(canary_cli),
            "CLI command must never execute upon escalation",
        )
        self.assertIn("[NESTED] Refused", proc.stderr, "CLI stderr must explain nested refusal")
        self.assertTrue(
            manager.is_held_by("parent-holder", token=info["token"]),
            "Parent slot must remain held after CLI refused escalation",
        )

    def test_inherited_lock_in_another_run_dir_cannot_bypass_admission(self):
        """Adversarial check: Inherited lock in another run-dir cannot bypass admission."""
        manager_1 = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(
            manager_1.acquire("holder-dir1", job_class="medium", mem_gib=1.5, timeout=2.0),
            "Slot in run_dir_1 must acquire successfully",
        )
        info_1 = manager_1._read_slot_info(0)
        self.assertIsNotNone(info_1)

        marker_1 = {
            "owner": "holder-dir1",
            "pid": info_1["pid"],
            "token": info_1["token"],
            "job_class": "medium",
            "mem_gib": 1.5,
        }
        os.environ["BUILD_SLOT_HELD"] = json.dumps(marker_1)

        # Separate run-dir
        other_run_dir = os.path.join(self.tmp.name, "separate_run_dir")
        manager_2 = BuildSlotManager(run_dir=other_run_dir)

        # In manager_2, _inherited_holder must return None because slot is in run_dir_1
        self.assertIsNone(
            manager_2._inherited_holder(),
            "Manager in another run_dir must not inherit holder from a different run_dir",
        )

        # Saturated budget in manager_2: ordinary admission must fail / cannot bypass
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "0.0"
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "99.0"

        acquired_sat = manager_2.acquire("child-in-dir2", timeout=0.1)
        self.assertFalse(
            acquired_sat,
            "Marker from another run_dir must not bypass admission in separate run_dir",
        )

        # Contrast: Normal budget in manager_2 allocates fresh slot, does not reuse dir1 slot
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "64.0"
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "50.0"
        acquired_norm = manager_2.acquire("child-in-dir2", job_class="medium", mem_gib=1.5, timeout=1.0)
        self.assertTrue(acquired_norm, "Normal admission in separate run_dir must succeed")
        self.assertTrue(
            os.path.isdir(manager_2.slot_dirs[0]),
            "Separate run_dir must allocate its own fresh slot directory",
        )
        self.assertTrue(
            manager_1.is_held_by("holder-dir1", token=info_1["token"]),
            "Original holder in run_dir_1 must remain unaffected",
        )
        # Release manager_2 does not affect run_dir_1
        manager_2.release("child-in-dir2")
        self.assertTrue(
            manager_1.is_held_by("holder-dir1", token=info_1["token"]),
            "Original holder in run_dir_1 must remain held after separate manager release",
        )

    def test_forged_memory_and_class_markers_cannot_bypass_admission(self):
        """Adversarial check: Forged memory or class markers cannot bypass admission."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(
            manager.acquire("legit-holder", job_class="light", mem_gib=0.5, timeout=2.0),
            "Legitimate slot must acquire successfully",
        )
        info = manager._read_slot_info(0)
        self.assertIsNotNone(info)

        # Saturated budget: only valid matching markers can bypass
        os.environ["BUILD_SLOT_AVAILABLE_GIB"] = "0.0"
        os.environ["BUILD_SLOT_RAM_PERCENT"] = "99.0"

        forged_markers = [
            ("forged_escalated_class", {
                "owner": "legit-holder", "pid": info["pid"], "token": info["token"],
                "job_class": "heavy", "mem_gib": 5.0,
            }),
            ("forged_higher_memory", {
                "owner": "legit-holder", "pid": info["pid"], "token": info["token"],
                "job_class": "light", "mem_gib": 2.5,
            }),
            ("forged_tampered_lower_memory", {
                "owner": "legit-holder", "pid": info["pid"], "token": info["token"],
                "job_class": "light", "mem_gib": 0.1,
            }),
            ("forged_unknown_class", {
                "owner": "legit-holder", "pid": info["pid"], "token": info["token"],
                "job_class": "superheavy", "mem_gib": 0.5,
            }),
            ("forged_negative_memory", {
                "owner": "legit-holder", "pid": info["pid"], "token": info["token"],
                "job_class": "light", "mem_gib": -1.0,
            }),
            ("forged_owner_name", {
                "owner": "attacker-owner", "pid": info["pid"], "token": info["token"],
                "job_class": "light", "mem_gib": 0.5,
            }),
            ("forged_token", {
                "owner": "legit-holder", "pid": info["pid"], "token": "forged-token-abc",
                "job_class": "light", "mem_gib": 0.5,
            }),
        ]

        for label, forged in forged_markers:
            os.environ["BUILD_SLOT_HELD"] = json.dumps(forged)
            self.assertIsNone(
                manager._inherited_holder(),
                f"Marker {label} must not be accepted by _inherited_holder",
            )
            acquired = manager.acquire(f"child-{label}", timeout=0.05)
            self.assertFalse(
                acquired,
                f"Forged marker {label} must not bypass admission under saturated budget",
            )

        # Contrast verification: Exact matching marker DOES bypass admission immediately
        valid_marker = {
            "owner": "legit-holder",
            "pid": info["pid"],
            "token": info["token"],
            "job_class": "light",
            "mem_gib": 0.5,
        }
        os.environ["BUILD_SLOT_HELD"] = json.dumps(valid_marker)
        self.assertIsNotNone(
            manager._inherited_holder(),
            "Exact matching marker must be accepted by _inherited_holder",
        )
        self.assertTrue(
            manager.acquire("valid-child", job_class="light", mem_gib=0.5, timeout=0.5),
            "Exact matching marker must bypass saturated budget admission immediately",
        )

    def test_nested_run_timeout_returns_124_and_holder_retained(self):
        """Adversarial check: Nested run timeout returns 124 and holder slot is retained."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(
            manager.acquire("holder", job_class="medium", mem_gib=1.5, timeout=2.0),
            "Holder slot must acquire successfully",
        )
        info = manager._read_slot_info(0)
        self.assertIsNotNone(info)

        marker = {
            "owner": "holder",
            "pid": info["pid"],
            "token": info["token"],
            "job_class": "medium",
            "mem_gib": 1.5,
        }
        os.environ["BUILD_SLOT_HELD"] = json.dumps(marker)

        # 1. API: manager.run_command with timeout
        exit_code = manager.run_command(
            name="sleeper-child",
            cmd=[sys.executable, "-c", "import time; time.sleep(10)"],
            run_timeout=0.5,
            job_class="medium",
            mem_gib=1.5,
        )
        self.assertEqual(exit_code, 124, "Nested run command timeout must return exit code 124")
        self.assertTrue(
            manager.is_held_by("holder", token=info["token"]),
            "Holder slot must remain held after nested run timeout",
        )
        self.assertTrue(os.path.isdir(manager.slot_dirs[0]))
        slot_info = manager._read_slot_info(0)
        self.assertEqual(slot_info.get("owner"), "holder")
        self.assertEqual(slot_info.get("token"), info["token"])

        # 2. CLI Subprocess: nested build_slot.py run under parent build_slot.py run in dedicated run_dir
        cli_run_dir = os.path.join(self.run_dir, "cli_timeout_subtest")
        os.makedirs(cli_run_dir, exist_ok=True)
        evidence_file = os.path.join(cli_run_dir, "timeout_subprocess_evidence.json")
        script_file = os.path.join(cli_run_dir, "nested_timeout_runner.py")
        with open(script_file, "w", encoding="utf-8") as f:
            f.write(
                "import sys, os, subprocess, json\n"
                "bs_script, run_dir, out_file = sys.argv[1], sys.argv[2], sys.argv[3]\n"
                "nested_cmd = [\n"
                "    sys.executable, bs_script, '--run-dir', run_dir,\n"
                "    'run', 'nested-sleeper', '--class', 'medium', '--mem-gib', '1.5',\n"
                "    '--run-timeout', '0.5', '--',\n"
                "    sys.executable, '-c', 'import time; time.sleep(10)'\n"
                "]\n"
                "proc = subprocess.run(nested_cmd, capture_output=True, text=True, timeout=10,\n"
                "                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))\n"
                "info_path = os.path.join(run_dir, 'build-slot.lock', 'info.json')\n"
                "held = os.path.exists(info_path)\n"
                "with open(info_path, 'r', encoding='utf-8') as f:\n"
                "    lock_data = json.load(f)\n"
                "res = {'rc': proc.returncode, 'held': held, 'owner': lock_data.get('owner')}\n"
                "with open(out_file, 'w', encoding='utf-8') as f:\n"
                "    json.dump(res, f)\n"
            )

        cmd = [
            sys.executable,
            os.path.join(SCRIPT_DIR, "build_slot.py"),
            "--run-dir", cli_run_dir,
            "run", "parent-holder-cli",
            "--class", "medium",
            "--mem-gib", "1.5",
            "--",
            sys.executable, script_file,
            os.path.join(SCRIPT_DIR, "build_slot.py"),
            cli_run_dir,
            evidence_file,
        ]

        cli_env = os.environ.copy()
        cli_env.pop("BUILD_SLOT_HELD", None)

        p = subprocess.run(
            cmd,
            env=cli_env,
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(p.returncode, 0, f"Parent run failed: {p.stderr}\nSTDOUT: {p.stdout}")
        self.assertTrue(os.path.exists(evidence_file))
        with open(evidence_file, "r", encoding="utf-8") as f:
            evidence = json.load(f)
        self.assertEqual(evidence["rc"], 124, "Subprocess nested run timeout must return exit code 124")
        self.assertTrue(evidence["held"], "Parent lock must remain held when nested child times out")
        self.assertEqual(evidence["owner"], "parent-holder-cli", "Lock owner must remain parent")
    def test_freeze_under_valid_marker_exits_75(self):
        """Adversarial check: Operator freeze under valid marker refuses with exit code 75."""
        manager = BuildSlotManager(run_dir=self.run_dir)
        self.assertTrue(
            manager.acquire("holder", job_class="medium", mem_gib=1.5, timeout=2.0),
            "Holder slot must acquire successfully",
        )
        info = manager._read_slot_info(0)
        self.assertIsNotNone(info)

        marker = {
            "owner": "holder",
            "pid": info["pid"],
            "token": info["token"],
            "job_class": "medium",
            "mem_gib": 1.5,
        }
        os.environ["BUILD_SLOT_HELD"] = json.dumps(marker)

        # Place build-freeze in run_dir
        freeze_file = os.path.join(self.run_dir, "build-freeze")
        with open(freeze_file, "w", encoding="utf-8") as f:
            f.write("emergency maintenance active")

        # 1. API acquire raises SystemExit(75)
        with self.assertRaises(SystemExit) as cm_acq:
            manager.acquire("nested-child", job_class="medium", mem_gib=1.5, timeout=0.5)
        self.assertEqual(cm_acq.exception.code, 75, "Acquire under freeze must exit with 75")

        # 2. API run_command raises SystemExit(75)
        with self.assertRaises(SystemExit) as cm_run:
            manager.run_command(
                name="nested-child",
                cmd=[sys.executable, "-c", "import sys; sys.exit(0)"],
                job_class="medium",
                mem_gib=1.5,
            )
        self.assertEqual(cm_run.exception.code, 75, "Run command under freeze must exit with 75")

        # 3. CLI subprocess exits with returncode 75
        cmd_cli = [
            sys.executable,
            os.path.join(SCRIPT_DIR, "build_slot.py"),
            "--run-dir", self.run_dir,
            "run", "cli-nested-child",
            "--class", "medium",
            "--mem-gib", "1.5",
            "--",
            sys.executable, "-c", "import sys; sys.exit(0)",
        ]
        proc = subprocess.run(
            cmd_cli,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.returncode, 75, f"CLI under freeze must exit with 75: {proc.stderr}")
        self.assertIn("build freeze active", proc.stderr, "CLI stderr must explain build freeze")

        # Holder slot in run_dir must remain held and unaffected
        self.assertTrue(
            manager.is_held_by("holder", token=info["token"]),
            "Holder slot must remain intact despite freeze refusals",
        )

    def test_windows_nested_child_job_subprocess_path(self):
        """Inspect and verify Windows nested child job path through actual subprocess execution."""
        evidence_file = os.path.join(self.run_dir, "win_job_evidence.json")
        script_file = os.path.join(self.run_dir, "win_nested_job_runner.py")
        with open(script_file, "w", encoding="utf-8") as f:
            f.write(
                "import sys, os, subprocess, json\n"
                "bs_script, run_dir, out_file = sys.argv[1], sys.argv[2], sys.argv[3]\n"
                "nested_cmd = [\n"
                "    sys.executable, bs_script, '--run-dir', run_dir,\n"
                "    'run', 'nested-job-worker', '--class', 'light', '--mem-gib', '0.5',\n"
                "    '--', sys.executable, '-c',\n"
                "    'import os, sys; print(\"nested child running, pid=\", os.getpid())'\n"
                "]\n"
                "proc = subprocess.run(nested_cmd, capture_output=True, text=True, timeout=15,\n"
                "                      creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))\n"
                "res = {\n"
                "    'nested_rc': proc.returncode,\n"
                "    'nested_stdout': proc.stdout,\n"
                "    'nested_stderr': proc.stderr,\n"
                "    'has_nested_tag': '[NESTED] running under held slot' in proc.stderr,\n"
                "}\n"
                "with open(out_file, 'w', encoding='utf-8') as f:\n"
                "    json.dump(res, f)\n"
            )

        cmd = [
            sys.executable,
            os.path.join(SCRIPT_DIR, "build_slot.py"),
            "--run-dir", self.run_dir,
            "run", "win-parent-job",
            "--class", "medium",
            "--mem-gib", "1.5",
            "--",
            sys.executable, script_file,
            os.path.join(SCRIPT_DIR, "build_slot.py"),
            self.run_dir,
            evidence_file,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(proc.returncode, 0, f"Parent run failed: {proc.stderr}\nSTDOUT: {proc.stdout}")
        self.assertTrue(os.path.exists(evidence_file), "Evidence file must be written")
        with open(evidence_file, "r", encoding="utf-8") as f:
            evidence = json.load(f)

        self.assertEqual(evidence["nested_rc"], 0, f"Nested run under Windows job must succeed: {evidence.get('nested_stderr')}")
        self.assertTrue(evidence["has_nested_tag"], "Nested run must log running under held slot")
        self.assertIn("nested child running", evidence["nested_stdout"])

if __name__ == "__main__":
    unittest.main()
