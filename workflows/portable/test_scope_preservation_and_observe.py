#!/usr/bin/env python3
"""
workflows/test_scope_preservation_and_observe.py

Regression test suite for scope-preservation observation tooling and AC-1..AC-4:
1. First failure durable record: actionable diagnosis, owner, and next action (observe --diagnosis --owner --next-action).
2. Repeat-failure rule: second identical failure signature refuses a third attempt and forces reassignment/next-action.
3. check --strict: flags items whose scope was silently dropped (criteria removed or state regressed without an authorization note).
4. Negative controls: verifies that prior code shapes fail where the new invariants now hold.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

from ledger import RequestLedger
from recurrence_guard import RecurrenceGuard


class TestScopePreservationAndObserve(unittest.TestCase):

    HEAD = "c0ffee112233445566778899aabbccddeeff0011"

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_scope_obs_")
        self.ledger_path = os.path.join(self.test_dir, "ledger.json")
        self.recurrence_path = os.path.join(self.test_dir, "recurrence.json")
        self.ledger = RequestLedger(self.ledger_path, state_dir=self.test_dir)
        self.guard = RecurrenceGuard(state_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _create_sample_request(self, req_id="req-test-01", state="pending"):
        return self.ledger.add_request(
            req_id=req_id,
            prompt="Test scope preservation and failure observation",
            session="sess-test",
            project="Bavariance/polysimulator",
            acceptance_criteria=[
                {"id": "AC-1", "description": "Criteria 1"},
                {"id": "AC-2", "description": "Criteria 2"},
            ],
            owner="initial-owner",
            head=self.HEAD,
            state=state,
            task_type="harness",
            next_action="Start implementation",
        )

    def test_first_failure_records_diagnosis_owner_next_action_durably(self):
        """AC-1/AC-4: First failure records actionable diagnosis, owner, and next action durably."""
        self._create_sample_request("req-fail-01")

        # Run observe via Python API with full diagnosis, owner, and next action
        res = self.ledger.observe_failure(
            req_id="req-fail-01",
            diagnosis="Virtual bunfs path not bundled into binary; fallback throws ENOENT",
            owner="diagnosing-engineer",
            next_action="Bundle native-ledger-bridge.py into binary and verify path resolution",
            error="Worker returned verdict 'fail' for stage 'qa': path unresolved",
            error_class="worker_verdict:qa:fail:req-fail-01",
            environment="harness",
            operation="worker:qa",
            head_sha=self.HEAD,
        )

        # 1. Verify ledger request was updated with owner and next_action
        req = self.ledger.get_request("req-fail-01")
        self.assertEqual(req["owner"], "diagnosing-engineer")
        self.assertEqual(
            req["next_action"],
            "Bundle native-ledger-bridge.py into binary and verify path resolution",
        )

        # 2. Verify durable evidence was appended
        evidence_types = [ev.get("type") for ev in req["evidence"]]
        self.assertIn("failure_observation", evidence_types)
        obs_ev = next(ev for ev in req["evidence"] if ev["type"] == "failure_observation")
        self.assertIn("diagnosing-engineer", obs_ev["summary"])
        details = json.loads(obs_ev["details"])
        self.assertEqual(details["owner"], "diagnosing-engineer")
        self.assertIn("Virtual bunfs path", details["diagnosis"])

        # 3. Verify recurrence store records diagnosis_complete = True
        rec_data = self.guard.load()
        sig = res["recurrence"]["signature"]
        entry = rec_data["signatures"][sig]
        self.assertEqual(entry["occurrences"], 1)
        self.assertTrue(entry["diagnosis_complete"])
        self.assertEqual(entry["owner"], "diagnosing-engineer")
        self.assertIn("Virtual bunfs path", entry["diagnosis"])

        # 4. Verify CLI invocation works seamlessly
        cli_py = os.path.join(TEST_DIR, "ledger.py")
        cmd = [
            sys.executable,
            cli_py,
            "--ledger",
            self.ledger_path,
            "--state-dir",
            self.test_dir,
            "observe",
            "req-fail-01",
            "--diagnosis",
            "Refined diagnosis from CLI",
            "--owner",
            "cli-owner",
            "--next-action",
            "Run local verification script",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"CLI observe failed: {proc.stderr}")
        self.assertIn("[OK] Recorded failure observation", proc.stdout)

        # Check updated request
        req_cli = self.ledger.get_request("req-fail-01")
        self.assertEqual(req_cli["owner"], "cli-owner")
        self.assertEqual(req_cli["next_action"], "Run local verification script")

    def test_repeat_failure_rule_refuses_third_attempt_and_forces_reassignment(self):
        """Repeat-failure rule: 2nd identical failure signature refuses 3rd attempt & forces reassignment."""
        self._create_sample_request("req-repeat-01")

        err_text = "SyntaxError: unexpected token in native-bridge"
        err_cls = "syntax_error:native_bridge"

        # 1. First failure
        intake1 = self.guard.observe(
            project="Bavariance/polysimulator",
            environment="harness",
            operation="worker:qa",
            error=err_text,
            explicit_error_class=err_cls,
            request_id="req-repeat-01",
            head_sha=self.HEAD,
            attempt="attempt-1",
            diagnosis="Syntax error in bridge",
            owner="failing-dev",
            next_action="Fix syntax error",
            ledger=self.ledger,
        )
        self.assertEqual(intake1.occurrences, 1)
        self.assertTrue(intake1.retry_allowed)

        # Check retry is permitted on first failure
        retry_dec1 = self.guard.check_retry(signature=intake1.signature)
        self.assertTrue(retry_dec1.allowed)

        # 2. Second failure with identical signature on subsequent attempt
        intake2 = self.guard.observe(
            project="Bavariance/polysimulator",
            environment="harness",
            operation="worker:qa",
            error=err_text,
            explicit_error_class=err_cls,
            request_id="req-repeat-01",
            attempt="attempt-2",
            head_sha=self.HEAD,
            ledger=self.ledger,
        )
        self.assertEqual(intake2.occurrences, 2)
        self.assertFalse(intake2.retry_allowed)
        self.assertEqual(intake2.status, "corrective_action_required")

        # 3. Verify check_retry REFUSES third attempt
        retry_dec2 = self.guard.check_retry(signature=intake2.signature)
        self.assertFalse(retry_dec2.allowed)
        self.assertIn("Unchanged retry refused", retry_dec2.reason)
        self.assertIn("2 distinct occurrences", retry_dec2.reason)

        # 4. Verify ledger request has blocker and forces reassignment
        req = self.ledger.get_request("req-repeat-01")
        self.assertIsNotNone(req.get("blocker"))
        self.assertIn("Recurring failure (2 distinct occurrences)", req["blocker"])
        self.assertIn("Reassign task from failing owner", req["next_action"])

        # 5. Reassigning owner updates request
        self.ledger.observe_failure(
            req_id="req-repeat-01",
            owner="senior-engineer",
            next_action="Implement AST-based parser rewrite",
            error=err_text,
            error_class=err_cls,
            environment="harness",
            operation="worker:qa",
        )
        req_reassigned = self.ledger.get_request("req-repeat-01")
        self.assertEqual(req_reassigned["owner"], "senior-engineer")

    def test_check_strict_flags_unauthorized_criterion_drop(self):
        """AC-3: check --strict flags items where an acceptance criterion was removed without authorization."""
        self._create_sample_request("req-crit-drop-01")

        # Remove criterion AC-2 without an authorization note
        self.ledger.update_request(
            req_id="req-crit-drop-01",
            remove_criterion="AC-2",
            actor="test-actor",
            reason="Dropped AC-2 without note",
        )

        req = self.ledger.get_request("req-crit-drop-01")
        # Assert AC-2 was removed from active criteria
        active_ids = [c["id"] for c in req["acceptance_criteria"]]
        self.assertEqual(active_ids, ["AC-1"])
        self.assertIn("dropped_criteria", req)
        self.assertFalse(req["dropped_criteria"][0]["authorized"])

        # check_request must flag as BLOCKED with an issue
        chk = self.ledger.check_request("req-crit-drop-01")
        self.assertEqual(chk["status"], "BLOCKED")
        self.assertTrue(any("Scope silently dropped" in iss and "AC-2" in iss for iss in chk["issues"]))

        # Subprocess check --strict must exit non-zero (code 1)
        cli_py = os.path.join(TEST_DIR, "ledger.py")
        cmd = [
            sys.executable,
            cli_py,
            "--ledger",
            self.ledger_path,
            "--state-dir",
            self.test_dir,
            "check",
            "req-crit-drop-01",
            "--strict",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("[ERROR]", proc.stdout)
        self.assertIn("Scope silently dropped", proc.stdout)

    def test_check_strict_passes_authorized_criterion_drop(self):
        """AC-3: check --strict passes when criterion drop includes an authorization note."""
        self._create_sample_request("req-crit-drop-auth-01")

        # Remove criterion AC-2 WITH authorization note
        self.ledger.update_request(
            req_id="req-crit-drop-auth-01",
            remove_criterion="AC-2",
            authorization_update={"notes": "Operator authorized scope refinement per issue #4629"},
            actor="test-actor",
            reason="Authorized scope refinement",
        )

        req = self.ledger.get_request("req-crit-drop-auth-01")
        self.assertTrue(req["dropped_criteria"][0]["authorized"])
        self.assertIn("Operator authorized", req["dropped_criteria"][0]["notes"])

        # check_request must be HEALTHY
        chk = self.ledger.check_request("req-crit-drop-auth-01")
        self.assertEqual(chk["status"], "HEALTHY")
        self.assertFalse(any("Scope silently dropped" in iss for iss in chk["issues"]))

        # Subprocess check --strict must exit 0
        cli_py = os.path.join(TEST_DIR, "ledger.py")
        cmd = [
            sys.executable,
            cli_py,
            "--ledger",
            self.ledger_path,
            "--state-dir",
            self.test_dir,
            "check",
            "req-crit-drop-auth-01",
            "--strict",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"check --strict should succeed: {proc.stderr}")

    def test_check_strict_flags_unauthorized_state_regression(self):
        """AC-3: check --strict flags items where state regressed without an authorization note."""
        self._create_sample_request("req-state-reg-01", state="pending")

        # Advance state: pending -> implementation -> QA
        self.ledger.update_request("req-state-reg-01", state="implementation")
        self.ledger.update_request("req-state-reg-01", state="QA")

        # Regress state: QA -> implementation without authorization note
        self.ledger.update_request(
            "req-state-reg-01",
            state="implementation",
            actor="test-actor",
            reason="Ad-hoc regression",
        )

        req = self.ledger.get_request("req-state-reg-01")
        self.assertIn("unauthorized_regressions", req)
        self.assertEqual(req["unauthorized_regressions"][0]["from_state"], "QA")
        self.assertEqual(req["unauthorized_regressions"][0]["to_state"], "implementation")

        # check_request must flag as BLOCKED
        chk = self.ledger.check_request("req-state-reg-01")
        self.assertEqual(chk["status"], "BLOCKED")
        self.assertTrue(any("Scope silently dropped" in iss and "QA -> implementation" in iss for iss in chk["issues"]))

        # CLI check --strict must exit 1
        cli_py = os.path.join(TEST_DIR, "ledger.py")
        cmd = [
            sys.executable,
            cli_py,
            "--ledger",
            self.ledger_path,
            "--state-dir",
            self.test_dir,
            "check",
            "req-state-reg-01",
            "--strict",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("[ERROR]", proc.stdout)

    def test_check_strict_passes_authorized_state_regression(self):
        """AC-3: check --strict passes when state regression has an authorization note."""
        self._create_sample_request("req-state-reg-auth-01", state="pending")

        self.ledger.update_request("req-state-reg-auth-01", state="implementation")
        self.ledger.update_request("req-state-reg-auth-01", state="QA")

        # Regress state with an authorization note
        self.ledger.update_request(
            "req-state-reg-auth-01",
            state="implementation",
            authorization_update={"notes": "Authorized regression to rework QA failure"},
            actor="operator",
            reason="Authorized rework",
        )

        chk = self.ledger.check_request("req-state-reg-auth-01")
        self.assertEqual(chk["status"], "HEALTHY")
        self.assertFalse(any("Scope silently dropped" in iss for iss in chk["issues"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
