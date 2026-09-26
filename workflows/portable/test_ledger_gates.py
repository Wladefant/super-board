#!/usr/bin/env python3
"""
workflows/portable/test_ledger_gates.py — Ledger lifecycle gate regressions & AC10 smoke suite

Comprehensive coverage for ledger lifecycle gates and AC10 observable contracts:
  1. Multi-threaded & multi-process concurrency with atomic writes and zero history corruption.
  2. Source-bound subprocess execution asserting current checkout ledger.py module provenance.
  3. OS kernel-level crash safety and thread-local re-entrant advisory lock recovery.
  4. Strict 8-state deployable lifecycle and local-doc lifecycle (rejecting fake deployments).
  5. Stale-head invalidation of criteria/evidence/proofs and clean re-verification on new HEAD.
  6. Dependency existence, circular dependency detection, and prerequisite state gating.
  7. Decision blockers, unauthorized actor rejection, option validation, and synthetic probe isolation.
  8. Authorization provenance requirement and owner self-authorization rejection.
  9. Restart recovery reconstructing queues, active blockers, and unmet dependencies purely from disk.
  10. Verification-complete state guards and pre-merge authorization gate reachability.
"""

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

from ledger import (
    ALLOWED_TRANSITIONS_DEPLOYABLE,
    ALLOWED_TRANSITIONS_LOCAL_DOC,
    VERIFICATION_COMPLETE_STATES,
    FileLock,
    RequestLedger,
    check_parent_sub_issues_guard,
    fetch_github_sub_issues,
    validate_github_url,
)


def normalize_mod_path(p: str) -> str:
    p = os.path.normcase(os.path.realpath(p))
    base = os.path.basename(p)
    parent = os.path.dirname(p)
    if os.path.basename(parent) == "__pycache__":
        parent = os.path.dirname(parent)
        base = base.split(".")[0] + ".py"
    if base.endswith(".pyc"):
        base = base[:-1]
    return os.path.join(parent, base)


class TestLedgerLifecycleGates(unittest.TestCase):

    HEAD = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_ledger_gates_")
        self.ledger = RequestLedger(os.path.join(self.test_dir, "ledger.json"))

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _add(self, req_id, state="implementation", task_type="deployable"):
        self.ledger.add_request(
            req_id=req_id,
            prompt="Ship the authorization gate",
            session="test-session",
            project="SuperboardCore",
            acceptance_criteria=[
                {"criterion": "Gate refuses unverified merge", "status": "pending", "evidence": ""},
                {"criterion": "Regression covers the refusal", "status": "pending", "evidence": ""},
            ],
            owner="GateLane",
            state=state,
            task_type=task_type,
            head=self.HEAD,
        )
    def _verify_criteria(self, req_id):
        for criterion in self.ledger.get_request(req_id)["acceptance_criteria"]:
            self.ledger.update_request(
                req_id,
                actor="real-stage-runner",
                criterion_update={
                    "id": criterion["id"],
                    "status": "verified",
                    "evidence": f"Observed criterion {criterion['id']} pass on {self.HEAD}",
                },
            )

    def _advance_after_stage(self, req_id, stage, target):
        self.ledger.update_request(
            req_id,
            state=target,
            actor=f"real-{stage}-runner",
            add_evidence={
                "type": f"{stage.lower()}_verification",
                "summary": f"{stage} passed on exact head",
                "details": f"command exited 0 and observed expected behavior on {self.HEAD}",
                "head": self.HEAD,
            },
        )


    def test_authorization_unreachable_without_review(self):
        """A deployable task must pass through review before the authorization gate."""
        self.assertNotIn("awaiting authorization", ALLOWED_TRANSITIONS_DEPLOYABLE["implementation"])
        self.assertNotIn("awaiting authorization", ALLOWED_TRANSITIONS_DEPLOYABLE["QA"])
        self.assertIn("awaiting authorization", ALLOWED_TRANSITIONS_DEPLOYABLE["review"])

        req_id = "req-gate-01"
        self._add(req_id)

        with self.assertRaisesRegex(ValueError, "Illegal state transition"):
            self.ledger.update_request(req_id, state="review", actor="test")
        with self.assertRaisesRegex(ValueError, "Illegal state transition"):
            self.ledger.update_request(req_id, state="awaiting authorization", actor="test")
        self.assertEqual(self.ledger.get_request(req_id)["state"], "implementation")

        self.ledger.update_request(req_id, state="QA", actor="test")
        with self.assertRaisesRegex(ValueError, "Illegal state transition"):
            self.ledger.update_request(req_id, state="awaiting authorization", actor="test")
        with self.assertRaisesRegex(ValueError, "acceptance criterion"):
            self._advance_after_stage(req_id, "QA", "review")
        self.assertEqual(self.ledger.get_request(req_id)["state"], "QA")

        self._verify_criteria(req_id)
        self._advance_after_stage(req_id, "QA", "review")
        self._advance_after_stage(req_id, "review", "awaiting authorization")
        self.assertEqual(self.ledger.get_request(req_id)["state"], "awaiting authorization")

        local_id = "req-local-no-review-skip"
        self._add(local_id, task_type="local_doc")
        self.ledger.update_request(local_id, state="QA", actor="test")
        with self.assertRaisesRegex(ValueError, "Illegal state transition"):
            self.ledger.update_request(local_id, state="done", actor="test")

    def test_pending_criteria_rejected_at_update_boundary(self):
        """Pending criteria cannot enter review; a later health warning is not the gate."""
        req_id = "req-gate-02"
        self._add(req_id)
        self.ledger.update_request(req_id, state="QA", actor="test")

        with self.assertRaisesRegex(ValueError, "acceptance criterion"):
            self._advance_after_stage(req_id, "QA", "review")

        current = self.ledger.get_request(req_id)
        self.assertEqual(current["state"], "QA")
        self.assertTrue(all(c["status"] == "pending" for c in current["acceptance_criteria"]))

    def test_verified_criteria_clear_the_authorization_gate(self):
        """With every criterion verified on the current head, the gate reports HEALTHY."""
        req_id = "req-gate-03"
        self._add(req_id)

        self.ledger.update_request(req_id, state="QA", actor="test")
        self._verify_criteria(req_id)
        self._advance_after_stage(req_id, "QA", "review")
        self._advance_after_stage(req_id, "review", "awaiting authorization")
        result = self.ledger.check_request(req_id)
        self.assertEqual(result["status"], "HEALTHY", f"unexpected issues: {result['issues']}")
        self.assertTrue(
            any("Awaiting operator authorization" in w for w in result["warnings"]),
            f"expected the authorization warning, got {result['warnings']}",
        )

    def test_verification_complete_states_cover_every_post_review_state(self):
        """Every state after review asserts verification, so none may downgrade to warnings."""
        for state in ("awaiting authorization", "integration", "live verification", "done"):
            self.assertIn(state, VERIFICATION_COMPLETE_STATES)
        self.assertNotIn("implementation", VERIFICATION_COMPLETE_STATES)
        self.assertNotIn("QA", VERIFICATION_COMPLETE_STATES)
        self.assertNotIn("review", VERIFICATION_COMPLETE_STATES)

    def test_reentrant_file_lock_nested_acquisition(self):
        """ADV-8: Acquiring re-entrant FileLock multiple times on same thread must not deadlock."""
        with FileLock(self.ledger.lock_path):
            with FileLock(self.ledger.lock_path):
                with FileLock(self.ledger.lock_path):
                    pass

    def test_crash_safety_lock_recovery_subprocess(self):
        """ADV-7/8: Kernel-level crash safety releases lock on process exit, allowing immediate re-acquisition."""
        source_ledger_path = os.path.join(TEST_DIR, "ledger.py")
        crash_script = os.path.join(self.test_dir, "crash_worker.py")
        with open(crash_script, "w", encoding="utf-8") as f:
            f.write(f"""import os
import sys

SOURCE_DIR = r"{TEST_DIR}"
if SOURCE_DIR not in sys.path:
    sys.path.insert(0, SOURCE_DIR)

import ledger
from ledger import FileLock

def normalize_mod_path(p):
    p = os.path.normcase(os.path.realpath(p))
    base = os.path.basename(p)
    parent = os.path.dirname(p)
    if os.path.basename(parent) == "__pycache__":
        parent = os.path.dirname(parent)
        base = base.split(".")[0] + ".py"
    if base.endswith(".pyc"):
        base = base[:-1]
    return os.path.join(parent, base)

expected = normalize_mod_path(r"{source_ledger_path}")
actual = normalize_mod_path(ledger.__file__)
if expected != actual:
    sys.stderr.write(f"PROVENANCE ERROR: child imported {{actual}}, expected {{expected}}\\n")
    sys.exit(99)

lock = FileLock(r"{self.ledger.lock_path}")
lock.acquire()
os._exit(42)
""")

        proc = subprocess.run(
            [sys.executable, "-B", crash_script],
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(
            proc.returncode,
            42,
            f"Worker failed with return code {proc.returncode}: {proc.stderr.decode('utf-8', errors='replace')}",
        )

        t0 = time.time()
        with FileLock(self.ledger.lock_path, timeout=5.0):
            pass
        dt = time.time() - t0
        self.assertLess(dt, 2.0, f"Lock re-acquisition took too long after crash: {dt:.4f}s")

    def test_concurrent_thread_updates_history_integrity(self):
        """Multi-threaded atomic writes ensure zero history corruption and exact sequence counts."""
        req_id = "req-concur-threads"
        self._add(req_id)

        num_threads = 5
        updates_per_thread = 10

        def thread_task(worker_id):
            for i in range(updates_per_thread):
                self.ledger.update_request(
                    req_id=req_id,
                    next_action=f"Action by thread {worker_id} iter {i}",
                    actor=f"thread-{worker_id}",
                    reason=f"Concurrent thread update {i}",
                )
                time.sleep(0.002)

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(thread_task, w) for w in range(num_threads)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        req = self.ledger.get_request(req_id)
        expected_hist = 1 + (num_threads * updates_per_thread)
        self.assertEqual(len(req["history"]), expected_hist)

        with open(self.ledger.ledger_path, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        self.assertIn(req_id, disk_data["requests"])
        self.assertEqual(len(disk_data["requests"][req_id]["history"]), expected_hist)

    def test_concurrent_process_updates_source_bound_subprocesses(self):
        """ADV-7: Multi-process concurrency asserting checkout source ledger provenance in children."""
        req_id = "req-concur-procs"
        self._add(req_id)

        source_ledger_path = os.path.join(TEST_DIR, "ledger.py")
        proc_script = os.path.join(self.test_dir, "proc_worker.py")
        with open(proc_script, "w", encoding="utf-8") as f:
            f.write(f"""import os
import sys
import time

SOURCE_DIR = r"{TEST_DIR}"
if SOURCE_DIR not in sys.path:
    sys.path.insert(0, SOURCE_DIR)

import ledger
from ledger import RequestLedger

def normalize_mod_path(p):
    p = os.path.normcase(os.path.realpath(p))
    base = os.path.basename(p)
    parent = os.path.dirname(p)
    if os.path.basename(parent) == "__pycache__":
        parent = os.path.dirname(parent)
        base = base.split(".")[0] + ".py"
    if base.endswith(".pyc"):
        base = base[:-1]
    return os.path.join(parent, base)

expected = normalize_mod_path(r"{source_ledger_path}")
actual = normalize_mod_path(ledger.__file__)
if expected != actual:
    sys.stderr.write(f"PROVENANCE ERROR: child imported {{actual}}, expected {{expected}}\\n")
    sys.exit(99)

proc_id = sys.argv[1]
count = int(sys.argv[2])
ledger_inst = RequestLedger(r"{self.ledger.ledger_path}")
for i in range(count):
    ledger_inst.update_request(
        req_id="{req_id}",
        next_action=f"Action by proc {{proc_id}} iter {{i}}",
        actor=f"process-{{proc_id}}",
        reason=f"Concurrent proc update {{i}}",
    )
    time.sleep(0.005)
sys.exit(0)
""")

        num_procs = 3
        updates_per_proc = 5
        procs = []
        for p_idx in range(num_procs):
            p = subprocess.Popen([sys.executable, "-B", proc_script, str(p_idx), str(updates_per_proc)])
            procs.append(p)

        for p in procs:
            ret = p.wait(timeout=15)
            self.assertEqual(ret, 0, f"Child process worker failed with return code {ret}")

        req = self.ledger.get_request(req_id)
        expected_hist = 1 + (num_procs * updates_per_proc)
        self.assertEqual(len(req["history"]), expected_hist)

        with open(self.ledger.ledger_path, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        self.assertIn(req_id, disk_data["requests"])
        self.assertEqual(len(disk_data["requests"][req_id]["history"]), expected_hist)

    def test_deployable_full_lifecycle_to_done_with_evidence(self):
        """ADV-6: Deployable tasks require full 8 states and fresh evidence at every transition."""
        req_id = "req-full-lifecycle"
        self._add(req_id, state="pending")

        self.ledger.update_request(req_id, state="implementation", actor="lead", reason="Begin implementation")
        self.ledger.update_request(req_id, state="QA", actor="qa-lead", reason="Begin QA verification")
        self._verify_criteria(req_id)
        self._advance_after_stage(req_id, "QA", "review")
        self._advance_after_stage(req_id, "review", "awaiting authorization")
        self.ledger.update_request(
            req_id,
            authorization_update={
                "status": "authorized",
                "authorized_by": "operator",
                "provenance": "PR-74",
                "notes": "Verified operator authorization",
            },
            actor="operator",
        )
        self.ledger.update_request(req_id, state="integration", actor="integrator", reason="Merged to staging")
        self.ledger.update_request(req_id, state="live verification", actor="smoke-agent", reason="Deployed staging")
        self.ledger.update_request(
            req_id,
            github_update={
                "proof_url": "https://github.com/Bavariance/polysimulator/pull/4545",
                "proof_verified": True,
            },
            state="done",
            actor="operator",
            reason="Completed and verified live",
        )

        final_req = self.ledger.get_request(req_id)
        self.assertEqual(final_req["state"], "done")
        health = self.ledger.check_request(req_id)
        self.assertEqual(health["status"], "HEALTHY", f"Issues: {health['issues']}")

    def test_local_doc_lifecycle_rejects_fake_deployments(self):
        """Local-doc / harness tasks reject integration/live verification and complete directly from review."""
        req_id = "req-local-doc-lifecycle"
        self._add(req_id, state="pending", task_type="local_doc")

        self.ledger.update_request(req_id, state="implementation", actor="doc-writer")
        self.ledger.update_request(req_id, state="QA", actor="doc-reviewer")
        self._verify_criteria(req_id)
        self._advance_after_stage(req_id, "QA", "review")

        for dep_state in ("integration", "live verification"):
            with self.assertRaisesRegex(ValueError, "not applicable for local_doc tasks"):
                self.ledger.update_request(req_id, state=dep_state, actor="doc-reviewer")

        self.ledger.update_request(
            req_id,
            github_update={
                "proof_url": "https://github.com/Bavariance/polysimulator/pull/4546",
                "proof_verified": True,
            },
            state="done",
            actor="doc-reviewer",
            reason="Documentation review verified",
        )
        self.assertEqual(self.ledger.get_request(req_id)["state"], "done")

    def test_stale_head_invalidation_and_reverification(self):
        """ADV-3: Changing head resets active verification stages to implementation and marks evidence stale."""
        head_a = "1111111111111111111111111111111111111111"
        head_b = "2222222222222222222222222222222222222222"
        req_id = "req-head-invalidation"

        self.ledger.add_request(
            req_id=req_id,
            prompt="Test git head invalidation",
            session="sess-head",
            project="SuperboardCore",
            acceptance_criteria=[
                {"id": "AC-1", "criterion": "Logic test on commit", "status": "pending", "evidence": ""},
            ],
            owner="lane-core",
            head=head_a,
            state="implementation",
        )
        self.ledger.update_request(req_id, state="QA", actor="tester")
        self.ledger.update_request(
            req_id,
            criterion_update={"id": "AC-1", "status": "verified", "evidence": f"Tested on {head_a}"},
            actor="qa-agent",
        )
        self.ledger.update_request(
            req_id,
            add_evidence={
                "type": "qa_verification",
                "summary": "QA passed",
                "details": f"Tests green on {head_a}",
                "head": head_a,
            },
            actor="qa-agent",
        )
        self.ledger.update_request(
            req_id,
            github_update={
                "proof_url": f"https://github.com/Bavariance/polysimulator/commit/{head_a}",
                "proof_verified": True,
            },
        )
        self.assertTrue(self.ledger.get_request(req_id)["github"]["proof_verified"])

        # Advance head to head_b
        self.ledger.update_request(
            req_id,
            head=head_b,
            actor="git-watcher",
            reason=f"Pushed commit {head_b}",
        )

        req_inv = self.ledger.get_request(req_id)
        self.assertEqual(req_inv["state"], "implementation")
        self.assertEqual(req_inv["acceptance_criteria"][0]["status"], "pending")
        self.assertIn("[STALE", req_inv["acceptance_criteria"][0]["evidence"])
        self.assertTrue(req_inv["evidence"][0]["stale"])
        self.assertFalse(req_inv["github"]["proof_verified"])
        self.assertIsNotNone(req_inv["github"].get("proof_invalidated_at"))

        # Attempting stage advance without fresh evidence on head_b fails
        self.ledger.update_request(req_id, state="QA", actor="tester")
        with self.assertRaisesRegex(ValueError, "lacks fresh verified evidence"):
            self.ledger.update_request(
                req_id,
                state="review",
                actor="qa-agent",
                add_evidence={
                    "type": "qa_verification",
                    "summary": "QA passed",
                    "details": f"Tests on {head_b}",
                    "head": head_b,
                },
            )

        # Re-verify criterion on head_b and re-advance
        self.ledger.update_request(
            req_id,
            criterion_update={"id": "AC-1", "status": "verified", "evidence": f"Fresh test executed on {head_b}"},
            actor="qa-agent",
        )
        self.ledger.update_request(
            req_id,
            state="review",
            actor="qa-agent",
            add_evidence={
                "type": "qa_verification",
                "summary": "QA passed on head B",
                "details": f"Command observed expected behavior on {head_b}",
                "head": head_b,
            },
        )
        self.ledger.update_request(
            req_id,
            state="awaiting authorization",
            actor="review-agent",
            add_evidence={
                "type": "review_verification",
                "summary": "Review passed on head B",
                "details": f"Adversarial review clean on {head_b}",
                "head": head_b,
            },
        )
        self.ledger.update_request(
            req_id,
            authorization_update={
                "status": "authorized",
                "authorized_by": "operator",
                "provenance": "PR-74",
            },
            actor="operator",
        )
        self.ledger.update_request(req_id, state="integration", actor="merger")
        self.ledger.update_request(req_id, state="live verification", actor="smoke-agent")
        self.ledger.update_request(
            req_id,
            github_update={
                "proof_url": "https://github.com/Bavariance/polysimulator/pull/4545",
                "proof_verified": True,
            },
            state="done",
            actor="operator",
            reason="Completed after fresh re-verification on head B",
        )
        self.assertEqual(self.ledger.get_request(req_id)["state"], "done")

    def test_dependency_existence_cycle_detection_and_prerequisite_gating(self):
        """ADV-4: Dependency existence, cycle detection, and prerequisite state gating."""
        with self.assertRaisesRegex(ValueError, "Dependency 'missing-dep-999' does not exist"):
            self.ledger.add_request(
                req_id="orphan-test",
                prompt="Orphan",
                session="sess-orphan",
                project="SuperboardCore",
                acceptance_criteria=[],
                owner="lane-main",
                dependencies=["missing-dep-999"],
            )

        self.ledger.add_request(
            req_id="dep-base",
            prompt="Base task",
            session="sess-dep",
            project="SuperboardCore",
            acceptance_criteria=[{"id": "AC-1", "criterion": "Base work", "status": "pending", "evidence": ""}],
            owner="lane-base",
            task_type="local_doc",
            state="pending",
            head=self.HEAD,
        )
        self.ledger.add_request(
            req_id="dep-child",
            prompt="Child task",
            session="sess-dep",
            project="SuperboardCore",
            acceptance_criteria=[],
            owner="lane-child",
            dependencies=["dep-base"],
            state="pending",
            head=self.HEAD,
        )

        with self.assertRaisesRegex(ValueError, "Circular dependency detected"):
            self.ledger.update_request("dep-base", dependencies=["dep-child"])

        # Attempting to add task in implementation while dependency is not done fails
        with self.assertRaisesRegex(ValueError, "Dependency 'dep-base' is not 'done'"):
            self.ledger.add_request(
                req_id="dep-consumer-eager",
                prompt="Consumer task eager",
                session="sess-dep",
                project="SuperboardCore",
                acceptance_criteria=[],
                owner="lane-consumer",
                dependencies=["dep-base"],
                state="implementation",
            )

        # Attempting to advance dep-child to implementation fails while dep-base is not done
        with self.assertRaisesRegex(ValueError, "Dependency 'dep-base' is not 'done'"):
            self.ledger.update_request("dep-child", state="implementation")

        # Complete dep-base
        self.ledger.update_request("dep-base", state="implementation", actor="worker")
        self.ledger.update_request("dep-base", state="QA", actor="worker")
        self._verify_criteria("dep-base")
        self._advance_after_stage("dep-base", "QA", "review")
        self.ledger.update_request(
            "dep-base",
            github_update={
                "proof_url": "https://github.com/Bavariance/polysimulator/pull/4546",
                "proof_verified": True,
            },
            state="done",
            actor="worker",
            reason="Base work complete",
        )
        self.assertEqual(self.ledger.get_request("dep-base")["state"], "done")

        # Now dep-child unblocks
        self.ledger.update_request("dep-child", state="implementation", actor="child-worker")
        self.assertEqual(self.ledger.get_request("dep-child")["state"], "implementation")

    def test_decision_blocker_guards_and_provenance_resolution(self):
        """ADV-5: Decision blockers prevent completion and require authorized responder & valid option."""
        req_id = "req-decision-blocker"
        self._add(req_id, task_type="local_doc")
        self.ledger.update_request(req_id, state="QA", actor="tester")
        self._verify_criteria(req_id)
        self._advance_after_stage(req_id, "QA", "review")

        self.ledger.add_decision(
            req_id=req_id,
            question="Select stream or batch architecture",
            options=["A: batch", "B: stream"],
            blocks=True,
            authorized_responder="Wladefant",
        )
        req_d = self.ledger.get_request(req_id)
        self.assertIn("DEC-1", req_d["decision_blockers"])

        with self.assertRaisesRegex(ValueError, "Unresolved decision blocker"):
            self.ledger.update_request(req_id, state="done")
        with self.assertRaisesRegex(ValueError, "is not authorized"):
            self.ledger.resolve_decision(
                req_id=req_id,
                decision_id="DEC-1",
                answer="B: stream",
                actor="unauthorized_rogue_agent",
            )

        with self.assertRaisesRegex(ValueError, "is not among valid options"):
            self.ledger.resolve_decision(
                req_id=req_id,
                decision_id="DEC-1",
                answer="invalid choice",
                actor="Wladefant",
            )

        with self.assertRaisesRegex(ValueError, "are unresolved"):
            self.ledger.clear_decision_blocker(req_id, "all")

        # Synthetic test probe records resolution but preserves blocker
        self.ledger.resolve_decision(
            req_id=req_id,
            decision_id="DEC-1",
            answer="B: stream",
            comment_id=5550734838,
            provenance_type="synthetic_test",
            actor="Wladefant",
        )
        req_probe = self.ledger.get_request(req_id)
        self.assertIn("DEC-1", req_probe["decision_blockers"])
        self.assertEqual(req_probe["decisions"][0]["status"], "synthetic_test_recorded")

        # Genuine human operator resolution clears blocker
        self.ledger.resolve_decision(
            req_id=req_id,
            decision_id="DEC-1",
            answer="B: stream",
            comment_id=6000000001,
            provenance_type="human_operator",
            actor="Wladefant",
        )
        req_resolved = self.ledger.get_request(req_id)
        self.assertNotIn("DEC-1", req_resolved["decision_blockers"])
        self.assertEqual(req_resolved["decisions"][0]["status"], "resolved")

        # Now that decision blocker is cleared, transition to done succeeds with proof
        self.ledger.update_request(
            req_id,
            github_update={
                "proof_url": "https://github.com/Bavariance/polysimulator/pull/4546",
                "proof_verified": True,
            },
            state="done",
            actor="tester",
            reason="Completed after decision resolved",
        )
        self.assertEqual(self.ledger.get_request(req_id)["state"], "done")

    def test_authorization_provenance_and_self_authorization_guards(self):
        """ADV-9: Owner cannot self-authorize and operator authorization requires verified provenance."""
        req_id = "req-auth-provenance"
        self._add(req_id)

        with self.assertRaisesRegex(ValueError, "Owner 'GateLane' cannot self-authorize"):
            self.ledger.update_request(
                req_id,
                authorization_update={"status": "authorized", "authorized_by": "GateLane"},
            )

        with self.assertRaisesRegex(ValueError, "Cannot mint operator authorization without provenance"):
            self.ledger.update_request(
                req_id,
                authorization_update={"status": "authorized", "authorized_by": "operator"},
            )

        self.ledger.update_request(
            req_id,
            authorization_update={
                "status": "authorized",
                "authorized_by": "operator",
                "provenance": "https://github.com/Bavariance/polysimulator/issues/4543#issuecomment-999",
                "notes": "Verified operator comment",
            },
        )
        self.assertEqual(self.ledger.get_request(req_id)["authorization"]["status"], "authorized")

    def test_restart_recovery_from_disk_ledger(self):
        """ADV-10: Reconstruct work queues, active blockers, and unmet dependencies purely from disk ledger."""
        # Req 1: done
        self.ledger.add_request(
            req_id="req-rec-done",
            prompt="Done task",
            session="sess-rec",
            project="SuperboardCore",
            acceptance_criteria=[{"id": "AC-1", "criterion": "Done crit", "status": "pending", "evidence": ""}],
            owner="lane-rec",
            task_type="local_doc",
            state="pending",
            head=self.HEAD,
        )
        self.ledger.update_request("req-rec-done", state="implementation", actor="tester")
        self.ledger.update_request("req-rec-done", state="QA", actor="tester")
        self._verify_criteria("req-rec-done")
        self._advance_after_stage("req-rec-done", "QA", "review")
        self.ledger.update_request(
            "req-rec-done",
            github_update={
                "proof_url": "https://github.com/Bavariance/polysimulator/pull/4546",
                "proof_verified": True,
            },
            state="done",
            actor="tester",
            reason="Completed",
        )

        # Req 2: active in implementation
        self._add("req-rec-active")

        # Req 3: blocked by dependency
        self.ledger.add_request(
            req_id="req-rec-blocked-dep",
            prompt="Blocked by active dependency",
            session="sess-rec",
            project="SuperboardCore",
            acceptance_criteria=[],
            owner="lane-rec",
            dependencies=["req-rec-active"],
            state="pending",
            head=self.HEAD,
        )

        # Req 4: blocked by decision
        self._add("req-rec-blocked-dec")
        self.ledger.add_decision(
            req_id="req-rec-blocked-dec",
            decision_id="DEC-REC-01",
            question="Architecture decision",
            options=["Option 1", "Option 2"],
            blocks=True,
            authorized_responder="Wladefant",
        )

        # Fresh ledger instance simulating restart
        recovered = RequestLedger(self.ledger.ledger_path)
        rec = recovered.recover()

        self.assertEqual(rec["role"], "local_recovery_cache")
        self.assertEqual(rec["authority"], "github_issues_and_superboard")
        self.assertEqual(rec["summary"]["total_requests"], 4)
        self.assertEqual(rec["summary"]["done_count"], 1)
        self.assertEqual(rec["summary"]["active_count"], 3)
        self.assertGreaterEqual(rec["summary"]["blocked_count"], 2)
        self.assertEqual(rec["summary"]["decision_blocked_count"], 1)

        active = rec["active_requests"]
        self.assertIn("req-rec-active", active)
        self.assertEqual(active["req-rec-active"]["state"], "implementation")
        self.assertIn("req-rec-blocked-dep", active)
        self.assertEqual(active["req-rec-blocked-dep"]["unmet_dependencies"], ["req-rec-active"])
        self.assertTrue(active["req-rec-blocked-dep"]["blocked"])
        self.assertIn("req-rec-blocked-dec", active)
        self.assertIn("DEC-REC-01", active["req-rec-blocked-dec"]["decision_blockers"])
        self.assertTrue(active["req-rec-blocked-dec"]["blocked"])

    def test_parent_close_guard_refuses_when_sub_issues_or_sub_requests_open(self):
        """Parent requests refuse to close to 'done' when sub-requests or native sub-issues are open."""
        # 1. Setup parent and child requests
        self._add("req-parent", state="review", task_type="local_doc")
        self._verify_criteria("req-parent")
        self.ledger.update_request(
            req_id="req-parent",
            sub_requests=["req-child-1"],
            github_update={"proof_url": "https://github.com/Bavariance/polysimulator/pull/1", "proof_verified": True},
        )

        self._add("req-child-1", state="implementation", task_type="local_doc")
        self.ledger.update_request(req_id="req-child-1", parent_req_id="req-parent")

        # Parent close refused because req-child-1 is in 'implementation'
        with self.assertRaises(ValueError) as ctx:
            self.ledger.update_request(req_id="req-parent", state="done")
        self.assertIn("open sub-request(s)", str(ctx.exception))
        self.assertIn("req-child-1", str(ctx.exception))

        # Advance and close child request
        self.ledger.update_request(req_id="req-child-1", state="QA")
        self._verify_criteria("req-child-1")
        self._advance_after_stage("req-child-1", "QA", "review")
        self.ledger.update_request(
            req_id="req-child-1",
            github_update={"proof_url": "https://github.com/Bavariance/polysimulator/pull/1", "proof_verified": True},
            state="done",
        )
        self.assertEqual(self.ledger.get_request("req-child-1")["state"], "done")

        # 2. GitHub native sub-issues guard refusal
        mock_open_sub_issues = [{"number": 101, "title": "Open child issue", "state": "open"}]
        checker = lambda repo, issue_num: mock_open_sub_issues

        self.ledger.update_request(
            req_id="req-parent",
            github_update={"issue_number": 50, "proof_url": "https://github.com/Bavariance/polysimulator/pull/1", "proof_verified": True},
        )

        with self.assertRaises(ValueError) as ctx:
            self.ledger.update_request(req_id="req-parent", state="done", sub_issues_checker=checker)
        self.assertIn("open sub-issue(s)", str(ctx.exception))
        self.assertIn("#101", str(ctx.exception))

        # 3. Allowed close when all sub-issues are closed
        mock_closed_sub_issues = [{"number": 101, "title": "Open child issue", "state": "closed"}]
        checker_closed = lambda repo, issue_num: mock_closed_sub_issues

        res = self.ledger.update_request(req_id="req-parent", state="done", sub_issues_checker=checker_closed)
        self.assertEqual(res["state"], "done")

    def test_fetch_github_sub_issues_contracts_and_fail_closed(self):
        """fetch_github_sub_issues must fail closed (raise RuntimeError/ValueError) on query failures."""
        # 1. Invalid input raises ValueError
        with self.assertRaises(ValueError):
            fetch_github_sub_issues("", 50)
        with self.assertRaises(ValueError):
            fetch_github_sub_issues("owner/repo", 0)
        with self.assertRaises(ValueError):
            fetch_github_sub_issues("owner/repo", -1)

        # 2. Non-zero exit code fails closed (RuntimeError)
        mock_fail_runner = lambda args: (1, "", "gh: authentication required (401)")
        with self.assertRaises(RuntimeError) as ctx:
            fetch_github_sub_issues("owner/repo", 50, runner=mock_fail_runner)
        self.assertIn("401", str(ctx.exception))

        # 3. Malformed JSON response fails closed (RuntimeError)
        mock_bad_json_runner = lambda args: (0, "{not valid json", "")
        with self.assertRaises(RuntimeError) as ctx:
            fetch_github_sub_issues("owner/repo", 50, runner=mock_bad_json_runner)
        self.assertIn("Failed to parse", str(ctx.exception))

        # 4. Empty response returns [] (zero sub-issues)
        mock_empty_runner = lambda args: (0, "", "")
        subs = fetch_github_sub_issues("owner/repo", 50, runner=mock_empty_runner)
        self.assertEqual(subs, [])

        # 5. Paginated NDJSON response parses multiple sub-issues correctly
        ndjson_output = '{"number": 101, "title": "Sub 1", "state": "open"}\n{"number": 102, "title": "Sub 2", "state": "closed"}'
        mock_ndjson_runner = lambda args: (0, ndjson_output, "")
        subs = fetch_github_sub_issues("owner/repo", 50, runner=mock_ndjson_runner)
        self.assertEqual(len(subs), 2)
        self.assertEqual(subs[0]["number"], 101)
        self.assertEqual(subs[0]["state"], "open")
        self.assertEqual(subs[1]["number"], 102)
        self.assertEqual(subs[1]["state"], "closed")

    def test_parent_close_guard_fails_closed_on_fetch_error(self):
        """Parent request close refuses when sub-issues query fails (fail-closed)."""
        self._add("req-fail-closed-parent", state="review", task_type="local_doc")
        self._verify_criteria("req-fail-closed-parent")
        self.ledger.update_request(
            req_id="req-fail-closed-parent",
            github_update={"issue_number": 99, "proof_url": "https://github.com/Bavariance/polysimulator/pull/1", "proof_verified": True},
        )

        def failing_checker(repo, issue_num):
            raise RuntimeError("GitHub API network timeout")

        with self.assertRaises(ValueError) as ctx:
            self.ledger.update_request(req_id="req-fail-closed-parent", state="done", sub_issues_checker=failing_checker)
        self.assertIn("Guard fails closed", str(ctx.exception))
        self.assertIn("Failed to verify parent sub-issues", str(ctx.exception))

    def test_parent_close_guard_does_not_trust_stale_cached_snapshot(self):
        """Parent request close does not trust stale cached snapshot when live lookup detects open sub-issues."""
        self._add("req-stale-cache-parent", state="review", task_type="local_doc")
        self._verify_criteria("req-stale-cache-parent")
        # Stale cache claiming sub-issues are all closed
        stale_cache = {
            "snapshot": {
                "sub_issues": [{"number": 101, "title": "Closed in snapshot", "state": "closed"}],
            }
        }
        self.ledger.update_request(
            req_id="req-stale-cache-parent",
            github_update={"issue_number": 88, "proof_url": "https://github.com/Bavariance/polysimulator/pull/1", "proof_verified": True},
        )
        # Inject github_cache into ledger record
        with FileLock(self.ledger.lock_path):
            data = self.ledger._load_data_unlocked()
            data["requests"]["req-stale-cache-parent"]["github_cache"] = stale_cache
            self.ledger._save_data_unlocked(data)
        # Live checker reveals a newly opened sub-issue 102
        live_checker = lambda repo, issue_num: [{"number": 102, "title": "New live open sub-issue", "state": "open"}]

        with self.assertRaises(ValueError) as ctx:
            self.ledger.update_request(req_id="req-stale-cache-parent", state="done", sub_issues_checker=live_checker)
        self.assertIn("open sub-issue(s)", str(ctx.exception))
        self.assertIn("#102", str(ctx.exception))

    def test_dangling_parent_req_id_does_not_bypass_sub_issue_guard(self):
        """A dangling parent_req_id is not an exemption: the live sub-issue guard still runs and fires."""
        self._add("req-dangling-parent-ref", state="review", task_type="local_doc")
        self._verify_criteria("req-dangling-parent-ref")
        self.ledger.update_request(
            req_id="req-dangling-parent-ref",
            parent_req_id="req-parent-that-does-not-exist",
            github_update={"issue_number": 777, "proof_url": "https://github.com/Bavariance/polysimulator/pull/1", "proof_verified": True},
        )
        # The referenced parent is genuinely dangling: no such request exists in the ledger
        with self.assertRaises(KeyError):
            self.ledger.get_request("req-parent-that-does-not-exist")

        calls = []

        def open_sub_issue_checker(repo, issue_num):
            calls.append((repo, issue_num))
            return [{"number": 555, "title": "Open child issue", "state": "open"}]

        with self.assertRaises(ValueError) as ctx:
            self.ledger.update_request(
                req_id="req-dangling-parent-ref", state="done", sub_issues_checker=open_sub_issue_checker
            )
        self.assertIn("open sub-issue(s)", str(ctx.exception))
        self.assertIn("#555", str(ctx.exception))
        # The live lookup ran for this request: parent_req_id granted no bypass
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 777)
        self.assertEqual(self.ledger.get_request("req-dangling-parent-ref")["state"], "review")

        # Positive control: the same dangling parent closes once no sub-issue is open
        res = self.ledger.update_request(
            req_id="req-dangling-parent-ref", state="done", sub_issues_checker=lambda repo, issue_num: []
        )
        self.assertEqual(res["state"], "done")

def run_tests():
    print("=" * 70)
    print("RUNNING LEDGER LIFECYCLE GATE REGRESSIONS")
    print("=" * 70)
    suite = unittest.TestLoader().loadTestsFromTestCase(TestLedgerLifecycleGates)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print("=" * 70)
    print(f"ALL {result.testsRun} LEDGER GATE TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    run_tests()
