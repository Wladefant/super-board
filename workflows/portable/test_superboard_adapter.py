#!/usr/bin/env python3
"""
workflows/portable/test_superboard_adapter.py — Smoke & Integration Tests for SuperboardExecutionAdapter

Verifies the integration between the portable workflow core and the Superboard
execution tooling:
  1. Full pipeline lifecycle with fake executor producing labeled fixture results.
  2. Bounded gates: preflight gating, reset-aware role routing, exact-SHA QA, review handoff.
  3. Inviolable human authorization gate: halts at 'awaiting authorization'; zero auto-merge.
  4. Real harmless worker task dispatch: exercises actual subprocess execution and exit-code capture.
  5. Concise Telegram status notification event emission with deduplication and links.
  6. Sequential single-step invariant: no parallel scheduler or daemon sprawl.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure portable directory is on sys.path
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

from superboard_adapter import (
    AdapterExecutionResult,
    SuperboardExecutionAdapter,
    WorkerExecutionResult,
)
from coordinator import Coordinator
from ledger import RequestLedger
from model_routing import HarnessDispatchPacket, TaskType, RiskLevel


class TestSuperboardExecutionAdapter(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_sb_adapter_")
        self.state_dir = os.path.join(self.test_dir, "state")
        self.evidence_dir = os.path.join(self.test_dir, "evidence")
        os.makedirs(self.state_dir, exist_ok=True)
        os.makedirs(self.evidence_dir, exist_ok=True)

        self.ledger_path = os.path.join(self.state_dir, "ledger.json")
        self.decisions_path = os.path.join(self.state_dir, "decisions.json")

        # Initialize clean test ledger
        self.ledger = RequestLedger(self.ledger_path)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_full_pipeline_with_fake_executor_and_labeled_fixture(self):
        """Test complete workflow with fake executor producing labeled fixture output."""
        # 1. Add eligible request in 'implementation'
        req_id = "req-feature-auth-token-01"
        self.ledger.add_request(
            req_id=req_id,
            prompt="Implement secure auth token refreshing for staging API",
            session="test-session-01",
            project="SuperboardCore",
            acceptance_criteria=[
                {"criterion": "Token rotation works", "status": "pending", "evidence": ""},
                {"criterion": "Revocation endpoint returns 200", "status": "pending", "evidence": ""},
            ],
            owner="BuilderLane",
            state="implementation",
            task_type="local_doc",
            issue_number=75,
            head="d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3",
        )

        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            fake_executor=True,
            notify_telegram=True,
            telegram_dry_run=True,
        )

        # Step 1: Execute Build stage
        res1 = adapter.run_step(request_id=req_id)
        self.assertEqual(res1.stage, "build")
        self.assertEqual(res1.status, "advanced")
        self.assertTrue(res1.preflight_passed)
        self.assertIsNotNone(res1.worker_result)
        self.assertTrue(res1.worker_result.is_fixture)
        self.assertIn("[FIXTURE_EXECUTION_RESULT]", res1.worker_result.fixture_label)
        self.assertEqual(res1.boundaries["execution_dispatched"], True)
        self.assertEqual(res1.boundaries["auto_merge_allowed"], False)
        self.assertEqual(res1.boundaries["auto_deploy_allowed"], False)

        # Verify ledger transitioned to QA
        req_after_1 = self.ledger.get_request(req_id)
        self.assertEqual(req_after_1["state"], "QA")

        # Step 2: Execute QA stage
        res2 = adapter.run_step(request_id=req_id)
        self.assertEqual(res2.stage, "qa")
        self.assertEqual(res2.status, "advanced")
        self.assertTrue(res2.gate_result["verified"])
        self.assertEqual(res2.gate_result["new_state"], "review")

        # Verify ledger transitioned to Review
        req_after_2 = self.ledger.get_request(req_id)
        self.assertEqual(req_after_2["state"], "review")

        # Step 3: Execute Review stage -> must halt at 'awaiting authorization'
        res3 = adapter.run_step(request_id=req_id)
        self.assertEqual(res3.stage, "review")
        self.assertEqual(res3.status, "awaiting_authorization")
        self.assertTrue(res3.gate_result["human_authorization_required"])

        # Inviolable Invariant Check: State is strictly 'awaiting authorization', NOT merged or done!
        req_after_3 = self.ledger.get_request(req_id)
        self.assertEqual(req_after_3["state"], "awaiting authorization")

        # Step 4: Next step must respect human authorization gate
        res4 = adapter.run_step(request_id=req_id)
        self.assertEqual(res4.status, "awaiting_authorization")
        self.assertIn("awaiting explicit human operator authorization", res4.status_reason)

    def test_02_preflight_blocker_halts_dispatch(self):
        """Test that a failing preflight probe halts worker dispatch and reports blocker."""
        req_id = "req-deployable-broken-probe-02"
        self.ledger.add_request(
            req_id=req_id,
            prompt="Deploy staging database schema migration",
            session="test-session-02",
            project="SuperboardCore",
            acceptance_criteria=[
                {"criterion": "Migration applies cleanly", "status": "pending", "evidence": ""},
            ],
            owner="DeployLane",
            state="implementation",
            task_type="deployable",
            issue_number=75,
        )

        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            fake_executor=True,
            notify_telegram=True,
            telegram_dry_run=True,
        )

        res = adapter.run_step(request_id=req_id)
        self.assertEqual(res.status, "blocked")
        self.assertFalse(res.preflight_passed)
        self.assertIn("Preflight gate blocked", res.status_reason)
        # Worker must NOT have been dispatched
        self.assertIsNone(res.worker_result)
        self.assertEqual(res.boundaries.get("execution_dispatched", False), False)

    def test_03_real_harmless_worker_execution(self):
        """Test real worker command execution on a harmless, non-mutating command."""
        req_id = "req-real-probe-03"
        self.ledger.add_request(
            req_id=req_id,
            prompt="Audit local repository worktree hygiene",
            session="test-session-03",
            project="SuperboardCore",
            acceptance_criteria=[
                {"criterion": "Working tree state verified", "status": "pending", "evidence": ""},
            ],
            owner="ProbeLane",
            state="implementation",
            task_type="local_doc",
            issue_number=75,
        )

        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            fake_executor=False,
            notify_telegram=False,
        )

        # Run with real_worker=True (exercises git status or config validate subprocess)
        res = adapter.run_step(request_id=req_id, real_worker=True)
        self.assertEqual(res.status, "advanced")
        self.assertIsNotNone(res.worker_result)
        self.assertFalse(res.worker_result.is_fixture)
        self.assertEqual(res.worker_result.exit_code, 0)
        self.assertIn("[REAL_WORKER_EXECUTION]", res.worker_result.output)
        self.assertEqual(res.boundaries["execution_dispatched"], True)

    def test_04_telegram_notification_event_formatting(self):
        """Test concise, single-sentence Telegram notification formatting with canonical link."""
        req_id = "req-notify-04"
        self.ledger.add_request(
            req_id=req_id,
            prompt="Fix UI focus leak on drawer close",
            session="test-session-04",
            project="SuperboardCore",
            acceptance_criteria=[
                {"criterion": "Inert attribute set on closed drawer", "status": "pending", "evidence": ""},
            ],
            owner="FrontendLane",
            state="implementation",
            task_type="local_doc",
            issue_number=75,
        )

        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            fake_executor=True,
            notify_telegram=True,
            telegram_dry_run=True,
        )

        res = adapter.run_step(request_id=req_id)
        self.assertIsNotNone(res.notification_receipt)
        self.assertTrue(res.notification_receipt.get("dry_run", True))
        payload = res.notification_receipt.get("payload", {})
        reason = res.notification_receipt.get("reason", "")
        # Invariant: Must contain single-sentence summary and canonical link in dry-run reason
        self.assertIn("https://github.com/", reason)
        self.assertIn("/issues/75", reason)
        self.assertIn(req_id, reason)
    def test_05_no_parallel_scheduler_invariant(self):
        """Test sequential, single-step execution contract without daemon loops."""
        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            fake_executor=True,
        )
        # Verify adapter is bounded: evaluate empty ledger
        res = adapter.run_step()
        self.assertIn(res.status, ("done", "completed"))
        self.assertEqual(res.request_id, None)
        self.assertFalse(res.boundaries.get("execution_dispatched", False))
        self.assertFalse(res.boundaries.get("self_spawn_loop", False))

    def test_06_unresolved_bug_retention_and_reproduction_absence_invariants(self):
        """
        USER CRITICAL INVARIANT TEST:
        1. Every reported important bug must persist until QA specifically proves original reproduction gone.
        2. Intake must durable-upsert authoritative issue/card BEFORE dispatch, preserving original prompt/repro/severity.
        3. New prompts/compaction/restart cannot replace/delete unresolved bug.
        4. Closure binds exact original scenario + regression evidence + reviewed head.
        5. Cannot close via generic suite alone, related fix assumption, or 'no-repro' without explicit user disposition.
        """
        bug_id = "bug-auth-refresh-500"
        original_prompt = "POST /api/v1/auth/refresh crashes with 500 UnhandledKeyError on expired tokens"
        repro_scenario = "curl -X POST http://localhost:8000/api/v1/auth/refresh -H 'Authorization: Bearer expired_token_xyz'"

        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            fake_executor=True,
            notify_telegram=True,
            telegram_dry_run=True,
        )

        # 1. Durable Upsert BEFORE dispatch
        bug_rec = adapter.durable_upsert_bug(
            bug_id=bug_id,
            prompt=original_prompt,
            reproduction_scenario=repro_scenario,
            severity="high",
            issue_number=75,
        )
        self.assertEqual(bug_rec["id"], bug_id)
        self.assertEqual(bug_rec["state"], "implementation")
        self.assertEqual(bug_rec["task_type"], "local_doc")
        labels = bug_rec.get("labels") or bug_rec.get("superboard", {}).get("labels", [])
        self.assertIn("type:bug", labels)

        # 2. Retention Invariant: New intake/prompt cannot overwrite or delete unresolved bug
        compaction_attempt = adapter.durable_upsert_bug(
            bug_id=bug_id,
            prompt="Unrelated cosmetic prompt trying to overwrite bug",
            reproduction_scenario="different scenario",
            severity="low",
        )
        persisted_bug = self.ledger.get_request(bug_id)
        self.assertEqual(persisted_bug["prompt"], original_prompt)
        self.assertEqual(persisted_bug["state"], "implementation")

        # 3. Advance to QA
        res_build = adapter.run_step(request_id=bug_id)
        self.assertEqual(res_build.stage, "build")
        self.assertEqual(res_build.status, "advanced")
        bug_in_qa = self.ledger.get_request(bug_id)
        self.assertEqual(bug_in_qa["state"], "QA")

        # 4. QA execution where original reproduction failed to be disproved -> MUST REOPEN to implementation
        failing_qa_worker_fn = lambda req, stage, dispatch: WorkerExecutionResult(
            stage=stage,
            exit_code=0,
            output="[REPRODUCTION_FAILED_TO_DISPROVE] Reproduction curl command still returned 500 on commit c1c2c3c4",
            head_sha="c1c2c3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0",
        )
        adapter_reopen = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            fake_executor_fn=failing_qa_worker_fn,
            notify_telegram=True,
            telegram_dry_run=True,
        )
        res_reopen = adapter_reopen.run_step(request_id=bug_id)
        self.assertEqual(res_reopen.stage, "qa")
        # Must be reopened to implementation!
        self.assertEqual(res_reopen.gate_result.get("new_state"), None)
        self.assertTrue(res_reopen.gate_result.get("reopened", False))
        self.assertFalse(res_reopen.gate_result.get("verified", True))
        reopened_bug = self.ledger.get_request(bug_id)
        self.assertEqual(reopened_bug["state"], "implementation")
        self.assertIn("reopened to implementation", reopened_bug["evidence"][-1]["summary"])

        # 5. Strict Bug Closure Gate Verification
        # 5a. Generic suite alone is rejected
        ok, reason = adapter.verify_bug_closure(
            req_id=bug_id,
            tested_sha="c1c2c3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0",
            reproduction_verified_absent=True,
            regression_evidence="Unit tests passed",
            generic_suite_only=True,
        )
        self.assertFalse(ok)
        self.assertIn("Generic test suite pass is not proof of specific reproduction absence", reason)

        # 5b. 'No-repro' claim without user disposition is rejected
        ok, reason = adapter.verify_bug_closure(
            req_id=bug_id,
            tested_sha="c1c2c3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0",
            reproduction_verified_absent=False,
            regression_evidence="Could not reproduce",
            no_repro_claimed=True,
            user_explicit_disposition=None,
        )
        self.assertFalse(ok)
        self.assertIn("requires explicit human user disposition", reason)

        # 5c. Valid verification with exact reproduction absence and regression evidence passes
        ok, evidence_str = adapter.verify_bug_closure(
            req_id=bug_id,
            tested_sha="c1c2c3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0",
            reproduction_verified_absent=True,
            regression_evidence="curl returned HTTP 401 with JSON error {'detail': 'Token expired'}; 500 no longer triggers",
        )
        self.assertTrue(ok)
        self.assertIn("Reproduction scenario specifically proven absent", evidence_str)
        self.assertIn("no mathematical proof of global absence claimed", evidence_str)

    def test_07_ui_bug_screenshot_gates_and_behavioral_proof_invariant(self):
        """
        USER CRITICAL INVARIANT TEST:
        1. UI bug closure strictly requires before/after assets tied to original reproduction,
           across desktop (1440px) and mobile (320px/390px) viewports on exact head commit.
        2. Raw.githubusercontent.com URLs strictly prohibited (fails on private repos).
        3. Render verification mandatory (must be confirmed visibly rendered).
        4. Functional bug also requires behavioral proof; screenshot alone is insufficient.
        5. If 'before' asset unavailable, must be documented as explicit limitation, never fabricated.
        """
        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            fake_executor=True,
        )
        head_sha = "e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0"
        ui_bug_id = "bug-ui-chart-slider-overlap"

        # 1. Missing behavioral proof rejected (screenshots alone insufficient)
        ok, reason = adapter.verify_bug_closure(
            req_id=ui_bug_id,
            tested_sha=head_sha,
            reproduction_verified_absent=True,
            regression_evidence="",  # Empty behavioral proof
            bug_type="ui",
            desktop_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_1440.png",
            mobile_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_390.png",
            desktop_before_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_before_1440.png",
            render_verified=True,
        )
        self.assertFalse(ok)
        self.assertIn("Specific behavioral reproduction regression proof", reason)

        # 2. UI bug with missing desktop/mobile after screenshots rejected
        ok, reason = adapter.verify_bug_closure(
            req_id=ui_bug_id,
            tested_sha=head_sha,
            reproduction_verified_absent=True,
            regression_evidence="Component hydrated cleanly without overlap",
            bug_type="ui",
            desktop_after_url="",  # Missing desktop
            mobile_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_390.png",
            render_verified=True,
        )
        self.assertFalse(ok)
        self.assertIn("Desktop after-fix visual asset URL (1440px) is mandatory", reason)

        # 3. Prohibited raw.githubusercontent.com URL rejected
        ok, reason = adapter.verify_bug_closure(
            req_id=ui_bug_id,
            tested_sha=head_sha,
            reproduction_verified_absent=True,
            regression_evidence="Component hydrated cleanly without overlap",
            bug_type="ui",
            desktop_after_url="https://raw.githubusercontent.com/Bavariance/polysimulator/main/evidence/slider_1440.png",
            mobile_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_390.png",
            desktop_before_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_before.png",
            render_verified=True,
        )
        self.assertFalse(ok)
        self.assertIn("Prohibited raw.githubusercontent.com URL", reason)

        # 4. Missing before asset without documented limitation rejected
        ok, reason = adapter.verify_bug_closure(
            req_id=ui_bug_id,
            tested_sha=head_sha,
            reproduction_verified_absent=True,
            regression_evidence="Component hydrated cleanly without overlap",
            bug_type="ui",
            desktop_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_1440.png",
            mobile_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_390.png",
            desktop_before_url=None,
            before_unavailable_reason=None,
            render_verified=True,
        )
        self.assertFalse(ok)
        self.assertIn("Before visual asset required, or explicit documented limitation", reason)

        # 5. Unverified render rejected
        ok, reason = adapter.verify_bug_closure(
            req_id=ui_bug_id,
            tested_sha=head_sha,
            reproduction_verified_absent=True,
            regression_evidence="Component hydrated cleanly without overlap",
            bug_type="ui",
            desktop_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_1440.png",
            mobile_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_390.png",
            desktop_before_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_before_1440.png",
            render_verified=False,  # Unverified render
        )
        self.assertFalse(ok)
        self.assertIn("Visual assets must be confirmed visibly rendered", reason)

        # 6. Valid UI bug closure with explicit before limitation (never fabricated)
        ok, evidence_limitation = adapter.verify_bug_closure(
            req_id=ui_bug_id,
            tested_sha=head_sha,
            reproduction_verified_absent=True,
            regression_evidence="DOM layout inspection confirms 0px overlap between chart and slider; z-index 10 cleanly stacked",
            bug_type="ui",
            desktop_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_1440.png",
            mobile_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_390.png",
            before_unavailable_reason="Before screenshot unavailable due to fatal JavaScript crash on unpatched commit",
            render_verified=True,
            environment="staging",
        )
        self.assertTrue(ok)
        self.assertIn("Before Asset Limitation: Before screenshot unavailable due to fatal JavaScript crash", evidence_limitation)
        self.assertIn("Desktop After (1440px)", evidence_limitation)
        self.assertIn("Mobile After (320px/390px)", evidence_limitation)
        self.assertIn("Render Verified: YES", evidence_limitation)

        # 7. Valid full UI bug closure with all assets
        ok, evidence_full = adapter.verify_bug_closure(
            req_id=ui_bug_id,
            tested_sha=head_sha,
            reproduction_verified_absent=True,
            regression_evidence="Chromium automated evaluation: slider bounds [x=24, y=100, w=342, h=40], chart bounds [x=24, y=160, w=342, h=220]; no intersection",
            bug_type="ui",
            desktop_before_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_before_1440.png",
            desktop_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_after_1440.png",
            mobile_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_after_390.png",
            render_verified=True,
            environment="staging",
        )
        self.assertTrue(ok)
        self.assertIn("Desktop Before:", evidence_full)
        self.assertIn("Desktop After (1440px):", evidence_full)
        self.assertIn("Mobile After (320px/390px):", evidence_full)
        self.assertIn("Behavioral Evidence: Chromium automated evaluation", evidence_full)
        self.assertIn("no mathematical proof of global absence claimed", evidence_full)
def run_tests():
    print("=" * 70)
    print("RUNNING SUPERBOARD EXECUTION ADAPTER SMOKE & INTEGRATION TEST SUITE")
    print("=" * 70)
    suite = unittest.TestLoader().loadTestsFromTestCase(TestSuperboardExecutionAdapter)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
    print("=" * 70)
    print("ALL SUPERBOARD ADAPTER TESTS PASSED CLEANLY (100% SUCCESS)")
    print("=" * 70)


if __name__ == "__main__":
    run_tests()
