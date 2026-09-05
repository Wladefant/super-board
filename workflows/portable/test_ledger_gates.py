#!/usr/bin/env python3
"""
workflows/portable/test_ledger_gates.py — Ledger lifecycle gate regressions

Covers the two invariants that let an unverified request look ready to merge:
  1. 'awaiting authorization' asserts QA and review completed, so it is unreachable
     directly from 'implementation' or 'QA' for a deployable task.
  2. At any state that asserts verification is complete, an unverified, missing or stale
     acceptance criterion is a health ISSUE (status BLOCKED), not a warning. Reporting it
     as a warning is what let a request sit at the merge gate reporting HEALTHY with every
     criterion still pending.
"""

import os
import shutil
import sys
import tempfile
import unittest

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

from ledger import (
    ALLOWED_TRANSITIONS_DEPLOYABLE,
    VERIFICATION_COMPLETE_STATES,
    RequestLedger,
)


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

    def test_authorization_unreachable_without_review(self):
        """A deployable task must pass through review before the authorization gate."""
        self.assertNotIn("awaiting authorization", ALLOWED_TRANSITIONS_DEPLOYABLE["implementation"])
        self.assertNotIn("awaiting authorization", ALLOWED_TRANSITIONS_DEPLOYABLE["QA"])
        self.assertIn("awaiting authorization", ALLOWED_TRANSITIONS_DEPLOYABLE["review"])

        req_id = "req-gate-01"
        self._add(req_id)

        with self.assertRaises(Exception):
            self.ledger.update_request(req_id, state="awaiting authorization", actor="test")
        self.assertEqual(self.ledger.get_request(req_id)["state"], "implementation")

        # QA is also not a shortcut.
        self.ledger.update_request(req_id, state="QA", actor="test")
        with self.assertRaises(Exception):
            self.ledger.update_request(req_id, state="awaiting authorization", actor="test")
        self.assertEqual(self.ledger.get_request(req_id)["state"], "QA")

        # The legitimate route succeeds.
        self.ledger.update_request(req_id, state="review", actor="test")
        self.ledger.update_request(req_id, state="awaiting authorization", actor="test")
        self.assertEqual(self.ledger.get_request(req_id)["state"], "awaiting authorization")

    def test_pending_criteria_block_at_authorization_gate(self):
        """Unverified criteria at the merge gate must report BLOCKED, never HEALTHY."""
        req_id = "req-gate-02"
        self._add(req_id)

        # While still being implemented, pending criteria are an expected warning.
        early = self.ledger.check_request(req_id)
        self.assertEqual(early["status"], "HEALTHY")
        self.assertTrue(any("Pending criteria" in w for w in early["warnings"]))

        for state in ("QA", "review", "awaiting authorization"):
            self.ledger.update_request(req_id, state=state, actor="test")

        gated = self.ledger.check_request(req_id)
        self.assertEqual(gated["status"], "BLOCKED")
        self.assertTrue(
            any("criteria unverified" in i for i in gated["issues"]),
            f"expected an unverified-criteria issue, got {gated['issues']}",
        )
        self.assertTrue(
            any("criteria missing evidence" in i for i in gated["issues"]),
            f"expected a missing-evidence issue, got {gated['issues']}",
        )

    def test_verified_criteria_clear_the_authorization_gate(self):
        """With every criterion verified on the current head, the gate reports HEALTHY."""
        req_id = "req-gate-03"
        self._add(req_id)

        for state in ("QA", "review"):
            self.ledger.update_request(req_id, state=state, actor="test")

        for criterion in self.ledger.get_request(req_id)["acceptance_criteria"]:
            self.ledger.update_request(
                req_id,
                actor="test",
                criterion_update={
                    "id": criterion["id"],
                    "status": "verified",
                    "evidence": "https://github.com/Wladefant/super-board/pull/74",
                },
            )

        self.ledger.update_request(req_id, state="awaiting authorization", actor="test")
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
