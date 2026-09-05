#!/usr/bin/env python3
"""
workflows/portable/test_superboard_adapter.py — Smoke & Integration Tests for SuperboardExecutionAdapter

Verifies the integration between the portable workflow core and the Superboard
execution tooling:
  1. Fixture results never advance real request state; only a real backend can.
  1b. Full lifecycle advancement driven by a backend implementing the published contract.
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
from coordinator import Coordinator, RoutingStatus
from ledger import FileLock, RequestLedger
from model_routing import TaskType, RiskLevel
from project_adapter import SuperboardLifecycleOutcome
import project_adapter
from decision_workflow import DecisionContract
from telegram_notifier import TelegramNotificationAdapter


class StubWorkerBackend:
    """
    Test double implementing the published worker-backend contract.

    Unlike a fixture, this stands in for a real backend at the documented extension point
    and returns structured, head-bound evidence. `reproduction` is only populated when a
    test explicitly supplies it, so bug-closure gates are exercised honestly.
    """

    def __init__(
        self,
        head_sha,
        ok=True,
        reproduction=None,
        blocked_reason=None,
        evidence=None,
        artifacts=("stub-run.log",),
    ):
        self.head_sha = head_sha
        self.ok = ok
        self.reproduction = reproduction
        self.blocked_reason = blocked_reason
        self.extra_evidence = evidence or {}
        self.artifacts = list(artifacts)
        self.calls = []

    def execute(self, request):
        stage = request["stage"]
        self.calls.append(dict(request))
        evidence = {
            "backend": "stub",
            "stage": stage,
            "head_sha": self.head_sha,
            "structured_result": {"passed": self.ok},
            "verdict": "pass" if self.ok else "fail",
            "checks": [{
                "name": f"{stage} contract",
                "command": ["stub-backend", f"--stage={stage}"],
                "exit_code": 0 if self.ok else 1,
                "observed": f"{stage} {'passed' if self.ok else 'failed'} at {self.head_sha}",
            }],
        }
        evidence.update(self.extra_evidence)
        if self.reproduction is not None:
            evidence["reproduction"] = self.reproduction
        return {
            "ok": self.ok,
            "stage": stage,
            "exit_code": 0 if self.ok else 1,
            "command": ["stub-backend", f"--stage={stage}"],
            "head_sha": self.head_sha,
            "evidence": evidence,
            "artifacts": self.artifacts,
            "blocked_reason": self.blocked_reason,
            "backend_name": "stub",
        }


class TestSuperboardExecutionAdapter(unittest.TestCase):

    HEAD_SHA = "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3"

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

    def _add_pipeline_request(self, req_id):
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
            head=self.HEAD_SHA,
        )

    def test_01_fixture_execution_never_advances_request_state(self):
        """A labelled fixture proves dispatch plumbing and must never advance real state."""
        req_id = "req-feature-auth-token-01"
        self._add_pipeline_request(req_id)

        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            fake_executor=True,
            notify_telegram=True,
            telegram_dry_run=True,
        )

        res = adapter.run_step(request_id=req_id)
        self.assertEqual(res.stage, "build")
        self.assertTrue(res.worker_result.is_fixture)
        self.assertIn("[FIXTURE_EXECUTION_RESULT]", res.worker_result.fixture_label)
        self.assertFalse(res.worker_result.is_verifiable_evidence)

        # The refusal must be explicit, not a silent no-op.
        self.assertFalse(res.gate_result["verified"])
        self.assertIn("fixture", res.gate_result["advance_refused"])
        self.assertIn("did not advance", res.status_reason)

        # Ledger state is untouched, however many fixture steps run.
        self.assertEqual(self.ledger.get_request(req_id)["state"], "implementation")
        adapter.run_step(request_id=req_id)
        self.assertEqual(self.ledger.get_request(req_id)["state"], "implementation")

    def test_01b_real_backend_advances_pipeline_to_awaiting_authorization(self):
        """A backend returning structured head-bound evidence drives the full lifecycle."""
        req_id = "req-feature-auth-token-01b"
        self._add_pipeline_request(req_id)

        backend = StubWorkerBackend(head_sha=self.HEAD_SHA)
        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            worker_backend=backend,
            notify_telegram=True,
            telegram_dry_run=True,
        )

        # Build: implementation -> QA
        res1 = adapter.run_step(request_id=req_id)
        self.assertEqual(res1.stage, "build")
        self.assertEqual(res1.status, "advanced")
        self.assertTrue(res1.preflight_passed)
        self.assertFalse(res1.worker_result.is_fixture)
        self.assertTrue(res1.worker_result.is_verifiable_evidence)
        self.assertEqual(res1.worker_result.backend_name, "stub")
        self.assertEqual(res1.boundaries["execution_dispatched"], True)
        self.assertEqual(res1.boundaries["auto_merge_allowed"], False)
        self.assertEqual(res1.boundaries["auto_deploy_allowed"], False)
        self.assertEqual(self.ledger.get_request(req_id)["state"], "QA")

        # QA: QA -> review
        res2 = adapter.run_step(request_id=req_id)
        self.assertEqual(res2.stage, "qa")
        self.assertEqual(res2.status, "advanced")
        self.assertTrue(res2.gate_result["verified"])
        self.assertEqual(res2.gate_result["new_state"], "review")
        self.assertEqual(self.ledger.get_request(req_id)["state"], "review")

        # Review: halts at awaiting authorization, never merges.
        res3 = adapter.run_step(request_id=req_id)
        self.assertEqual(res3.stage, "review")
        self.assertEqual(res3.status, "awaiting_authorization")
        self.assertTrue(res3.gate_result["human_authorization_required"])
        self.assertEqual(self.ledger.get_request(req_id)["state"], "awaiting authorization")

        # The authorization gate holds on subsequent steps.
        res4 = adapter.run_step(request_id=req_id)
        self.assertEqual(res4.status, "awaiting_authorization")
        self.assertIn("awaiting explicit human operator authorization", res4.status_reason)

        # Every dispatch carried the request identity and stage the backend contract requires.
        self.assertEqual([c["stage"] for c in backend.calls], ["build", "qa", "review"])
        self.assertTrue(all(c["request_id"] == req_id for c in backend.calls))
        self.assertTrue(all(c["head_sha"] == self.HEAD_SHA for c in backend.calls))

    def test_01c_missing_backend_blocks_instead_of_substituting_fixture(self):
        """With no backend and no explicit fixture flag, dispatch must block."""
        req_id = "req-feature-auth-token-01c"
        self._add_pipeline_request(req_id)

        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            notify_telegram=True,
            telegram_dry_run=True,
        )

        res = adapter.run_step(request_id=req_id)
        self.assertFalse(res.worker_result.is_fixture)
        self.assertIsNotNone(res.worker_result.blocked_reason)
        self.assertIn("No worker backend configured", res.worker_result.blocked_reason)
        self.assertEqual(self.ledger.get_request(req_id)["state"], "implementation")

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

    def test_04b_telegram_safe_dry_run_defaults_and_explicit_send(self):
        """Verify safe dry-run by default unless configured opt-in; explicit telegram_send actually sends."""
        # 1. Default: safe dry-run
        adapter_default = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            notify_telegram=True,
        )
        self.assertTrue(adapter_default.telegram_dry_run)
        self.assertFalse(adapter_default.telegram_send)

        # 2. Explicit send: dry_run is False
        adapter_send = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            notify_telegram=True,
            telegram_send=True,
        )
        self.assertFalse(adapter_send.telegram_dry_run)
        self.assertTrue(adapter_send.telegram_send)

        # 3. Explicit dry-run overrides send if both set
        adapter_forced_dry = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            notify_telegram=True,
            telegram_dry_run=True,
            telegram_send=True,
        )
        self.assertTrue(adapter_forced_dry.telegram_dry_run)
        self.assertTrue(adapter_forced_dry.telegram_send)

    def test_04c_telegram_decision_event_hook(self):
        """Verify emit_telegram_decision_event emits a real decision notification."""
        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            notify_telegram=True,
            telegram_dry_run=True,
        )
        decision_data = {
            "decision_id": "DEC-ARCH-01",
            "request_id": "req-arch-01",
            "question": "How should background execution proceed on blocking decisions?",
            "options": [
                {"id": "A", "label": "Park and Idle Wait"},
                {"id": "B", "label": "Speculative Feature Branching"},
            ],
            "recommendation": "Option A: Park and Idle Wait",
            "issue_url": "https://github.com/Bavariance/polysimulator/issues/4543",
            "status": "pending",
        }
        receipt = adapter.emit_telegram_decision_event(decision_data)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.get("status"), "dry_run")
        self.assertIn("Park and Idle Wait", receipt.get("reason", ""))
        self.assertIn("DEC-ARCH-01", receipt.get("reason", ""))

    def test_04e_telegram_decision_refusal_retired_and_synthetic(self):
        """Regression: Verify retired/synthetic/completed decisions are strictly refused by adapter hook."""
        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            notify_telegram=True,
            telegram_dry_run=True,
        )
        # 1. Retired synthetic demo DEC-4543-01
        retired_demo = {
            "decision_id": "DEC-4543-01",
            "request_id": "req-synthetic-decision-demo-4543",
            "question": "When agent swarm encounters blocking decision, how to proceed?",
            "status": "rejected",
            "provenance": "synthetic_test",
        }
        receipt_retired = adapter.emit_telegram_decision_event(retired_demo)
        self.assertIsNotNone(receipt_retired)
        self.assertEqual(receipt_retired.get("status"), "refused")
        self.assertIn("refused", receipt_retired.get("reason", "").lower())

        # 2. Decision on completed ledger request
        self.ledger.add_request(
            req_id="req-done-item",
            prompt="Done prompt",
            session="test",
            project="SuperboardCore",
            acceptance_criteria=[],
            owner="TestLane",
            state="done",
        )
        completed_dec = {
            "decision_id": "DEC-NEW-99",
            "request_id": "req-done-item",
            "question": "Obsolete question on done request?",
            "status": "pending",
        }
        receipt_completed = adapter.emit_telegram_decision_event(completed_dec)
        self.assertIsNotNone(receipt_completed)
        self.assertEqual(receipt_completed.get("status"), "refused")
        self.assertIn("terminal state", receipt_completed.get("reason", ""))

    def test_04f_run_step_uses_typed_decision_guard(self):
        """Real Coordinator DecisionStatus routes registry decisions through one typed guard."""
        retired_req = "req-synthetic-decision-demo-4543"
        self._add_pipeline_request(retired_req)
        retired_adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            notify_telegram=True,
            telegram_dry_run=True,
        )
        retired_manager = retired_adapter.coordinator.decision_mgr
        retired_manager.register_question(
            DecisionContract(
                decision_id="DEC-4543-01",
                request_id=retired_req,
                prompt="p",
                question="How to proceed?",
                options=[{"id": "A", "label": "Park"}, {"id": "B", "label": "Branch"}],
                recommendation="A",
                blocking_dependencies=[retired_req],
                authorized_responders=["wladefant"],
            )
        )
        packet = retired_adapter.coordinator.evaluate_step(request_id=retired_req)
        self.assertEqual(type(packet.decision_status).__name__, "DecisionStatus")
        self.assertTrue(packet.decision_status.blocking_this_request)

        with FileLock(retired_manager.lock_path):
            data = retired_manager._load_data_unlocked()
            data["decisions"]["DEC-4543-01"]["status"] = "rejected"
            data["decisions"]["DEC-4543-01"]["provenance"] = "synthetic_test"
            retired_manager._save_data_unlocked(data)

        retired = retired_adapter.run_step(request_id=retired_req)
        self.assertEqual(retired.stage, "decision")
        self.assertEqual(retired.status, "blocked")
        self.assertEqual(retired.notification_receipt["status"], "refused")
        self.assertNotIn("[Blocker]", retired.notification_receipt["reason"])

        pending_req = "req-real-pending-decision"
        self._add_pipeline_request(pending_req)
        pending_adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            notify_telegram=True,
            telegram_dry_run=True,
        )
        pending_adapter.coordinator.decision_mgr.register_question(
            DecisionContract(
                decision_id="DEC-PENDING-01",
                request_id=pending_req,
                prompt="p",
                question="Choose the supported execution path?",
                options=[{"id": "A", "label": "First"}, {"id": "B", "label": "Second"}],
                recommendation="A",
                blocking_dependencies=[pending_req],
                authorized_responders=["wladefant"],
            )
        )
        pending = pending_adapter.run_step(request_id=pending_req)
        self.assertEqual(pending.stage, "decision")
        self.assertEqual(pending.status, "blocked")
        self.assertEqual(pending.notification_receipt["status"], "dry_run")
        self.assertIn("[Decision Needed]", pending.notification_receipt["reason"])
        self.assertNotIn("[Blocker]", pending.notification_receipt["reason"])

        # Even if a malformed coordinator packet loses its DecisionStatus, the
        # blocked_by_decision preflight fallback must remain silent rather than leak a
        # generic blocker notification.
        missing_status_packet = pending_adapter.coordinator.evaluate_step(
            request_id=pending_req
        )
        missing_status_packet.decision_status = None
        pending_adapter.coordinator.evaluate_step = (
            lambda request_id=None: missing_status_packet
        )
        missing_status = pending_adapter.run_step(request_id=pending_req)
        self.assertEqual(missing_status.stage, "preflight")
        self.assertEqual(missing_status.status, "blocked")
        self.assertEqual(missing_status.preflight_passed, False)
        self.assertIsNone(missing_status.notification_receipt)

        eligible, reason = TelegramNotificationAdapter.is_decision_notifiable(
            {
                "decision_id": "DEC-MISSING-STATUS",
                "request_id": pending_req,
                "question": "Missing typed state",
            },
            ledger_request=self.ledger.get_request(pending_req),
        )
        self.assertFalse(eligible)
        self.assertIn("missing required typed status", reason)
    def test_04d_cli_telegram_flags_parsing(self):
        """Verify build_parser preserves safe dry-run by default unless explicit --telegram-send."""
        from superboard_adapter import build_parser
        parser = build_parser()

        # Default CLI args
        args_default = parser.parse_args([])
        self.assertFalse(args_default.notify_telegram)
        self.assertIsNone(args_default.telegram_dry_run)
        self.assertFalse(args_default.telegram_send)

        # Explicit send
        args_send = parser.parse_args(["--notify-telegram", "--telegram-send"])
        self.assertTrue(args_send.notify_telegram)
        self.assertIsNone(args_send.telegram_dry_run)
        self.assertTrue(args_send.telegram_send)

        # Explicit dry run
        args_dry = parser.parse_args(["--notify-telegram", "--telegram-dry-run"])
        self.assertTrue(args_dry.notify_telegram)
        self.assertTrue(args_dry.telegram_dry_run)
        self.assertFalse(args_dry.telegram_send)
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

        # 3. Advance to QA through a real backend; a fixture is not allowed to advance state.
        bug_head = self.ledger.get_request(bug_id).get("head") or self.HEAD_SHA
        self.ledger.update_request(bug_id, head=bug_head, actor="test-setup")
        build_adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            worker_backend=StubWorkerBackend(head_sha=bug_head),
            notify_telegram=True,
            telegram_dry_run=True,
        )
        res_build = build_adapter.run_step(request_id=bug_id)
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

        # 5. Strict Bug Closure Gate Verification.
        # The fix commit becomes the authoritative head that closure evidence must be bound to.
        fix_sha = "c1c2c3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0"
        self.ledger.update_request(bug_id, head=fix_sha, actor="test-setup")

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

        # 5d. Closure bound to a commit other than the ledger head is rejected
        ok, reason = adapter.verify_bug_closure(
            req_id=bug_id,
            tested_sha="b" * 40,
            reproduction_verified_absent=True,
            regression_evidence="curl returned HTTP 401; 500 no longer triggers",
        )
        self.assertFalse(ok)
        self.assertIn("authoritative head", reason)

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
            deployed_signed_in_qa=True,
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
            deployed_signed_in_qa=True,
        )
        self.assertTrue(ok)
        self.assertIn("Desktop Before:", evidence_full)
        self.assertIn("Desktop After (1440px):", evidence_full)
        self.assertIn("Mobile After (320px/390px):", evidence_full)
        self.assertIn("Behavioral Evidence: Chromium automated evaluation", evidence_full)
        self.assertIn("no mathematical proof of global absence claimed", evidence_full)
        self.assertIn("Signed-in QA: verified", evidence_full)

        # 8. Staging closure without persistent signed-in QA must be refused
        ok, reason = adapter.verify_bug_closure(
            req_id=ui_bug_id,
            tested_sha=head_sha,
            reproduction_verified_absent=True,
            regression_evidence="Chromium automated evaluation: no intersection",
            bug_type="ui",
            desktop_before_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_before_1440.png",
            desktop_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_after_1440.png",
            mobile_after_url="https://github.com/Bavariance/polysimulator/releases/download/v0.8.0/slider_after_390.png",
            render_verified=True,
            environment="staging",
            deployed_signed_in_qa=False,
        )
        self.assertFalse(ok)
        self.assertIn("Persistent signed-in QA", reason)

        # 9. A truncated SHA is not an authoritative head
        ok, reason = adapter.verify_bug_closure(
            req_id=ui_bug_id,
            tested_sha=head_sha[:8],
            reproduction_verified_absent=True,
            regression_evidence="Chromium automated evaluation: no intersection",
            bug_type="functional",
            environment="local",
        )
        self.assertFalse(ok)
        self.assertIn("full 40-character", reason)

    def test_10_build_producing_new_commit_advances_and_rebinds_head(self):
        """
        A build worker commits, so its observed head is necessarily a new SHA. That must
        advance the request and become the authoritative head; only QA and review require
        the head to stay put.
        """
        req_id = "req-build-newhead-10"
        self._add_pipeline_request(req_id)

        built_sha = "3a1cde039a75a9d35093d58cff9a437f442b17a5"
        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            worker_backend=StubWorkerBackend(head_sha=built_sha),
            notify_telegram=True,
            telegram_dry_run=True,
        )

        res = adapter.run_step(request_id=req_id)
        self.assertIsNone(res.worker_result.blocked_reason)
        self.assertEqual(res.status, "advanced")
        self.assertEqual(res.stage, "build")

        # The new commit is now the request's authoritative head.
        advanced = self.ledger.get_request(req_id)
        self.assertEqual(advanced["state"], "QA")
        self.assertEqual(advanced["head"], built_sha)

        # QA against a head other than the one recorded must not advance.
        qa_moved = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            worker_backend=StubWorkerBackend(head_sha="e" * 40),
            notify_telegram=True,
            telegram_dry_run=True,
        ).run_step(request_id=req_id)
        self.assertIsNotNone(qa_moved.worker_result.blocked_reason)
        self.assertIn("not head-bound", qa_moved.worker_result.blocked_reason)
        self.assertEqual(self.ledger.get_request(req_id)["state"], "QA")

        # QA on the recorded head proceeds.
        qa_ok = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            worker_backend=StubWorkerBackend(head_sha=built_sha),
            notify_telegram=True,
            telegram_dry_run=True,
        ).run_step(request_id=req_id)
        self.assertEqual(qa_ok.stage, "qa")
        self.assertEqual(qa_ok.status, "advanced")

    def test_11_build_without_commit_or_artifact_is_blocked(self):
        """A build that moved nothing and produced nothing has proved no work."""
        req_id = "req-build-noop-11"
        self._add_pipeline_request(req_id)

        res = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            worker_backend=StubWorkerBackend(head_sha=self.HEAD_SHA, artifacts=()),
            notify_telegram=True,
            telegram_dry_run=True,
        ).run_step(request_id=req_id)

        self.assertIsNotNone(res.worker_result.blocked_reason)
        self.assertIn("no artifacts", res.worker_result.blocked_reason)
        self.assertEqual(self.ledger.get_request(req_id)["state"], "implementation")

    def test_12_worker_request_carries_task_type(self):
        """The backend must not have to infer bug-ness from the request id."""
        req_id = "req-typed-dispatch-12"
        self.ledger.add_request(
            req_id=req_id,
            prompt="Refresh endpoint returns 500 on expired tokens",
            session="test-session-12",
            project="SuperboardCore",
            acceptance_criteria=[{"criterion": "500 no longer raised", "status": "pending", "evidence": ""}],
            owner="BugLane",
            state="implementation",
            task_type="local_doc",
            head=self.HEAD_SHA,
        )

        backend = StubWorkerBackend(head_sha="a" * 40)
        SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            worker_backend=backend,
            notify_telegram=True,
            telegram_dry_run=True,
        ).run_step(request_id=req_id)

        self.assertTrue(backend.calls)
        self.assertIn("task_type", backend.calls[0])
        self.assertEqual(backend.calls[0]["task_type"], "local_doc")

    def test_08_coordinator_risk_classification_reaches_backend(self):
        """The actual Coordinator DTO carries its real HIGH/DEEP classification to dispatch."""
        req_id = "req-routing-high-risk-08"
        self.ledger.add_request(
            req_id=req_id,
            prompt="Repair critical security architecture invariant under concurrency",
            session="test-session-routing",
            project="SuperboardCore",
            acceptance_criteria=[
                {"description": "Invariant holds under concurrent execution", "status": "pending", "evidence": ""}
            ],
            owner="RiskLane",
            state="implementation",
            task_type="local",
            head=self.HEAD_SHA,
        )
        backend = StubWorkerBackend(head_sha=self.HEAD_SHA)
        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            worker_backend=backend,
            notify_telegram=False,
        )

        coordinator_packet = adapter.coordinator.evaluate_step(request_id=req_id)
        self.assertIsInstance(coordinator_packet.routing, RoutingStatus)
        self.assertEqual(coordinator_packet.routing.task_type, TaskType.DEEP_REASONING.value)
        self.assertEqual(coordinator_packet.routing.risk_level, RiskLevel.HIGH.value)

        result = adapter.run_step(request_id=req_id)
        self.assertEqual(result.status, "advanced")
        self.assertEqual(backend.calls[0]["routing_task_type"], TaskType.DEEP_REASONING.value)
        self.assertEqual(backend.calls[0]["risk_level"], RiskLevel.HIGH.value)
        self.assertEqual(
            backend.calls[0]["criteria"],
            ["AC-1: Invariant holds under concurrent execution"],
        )
        self.assertEqual(
            backend.calls[0]["model"],
            (result.dispatch_packet or {})["recommendation"]["model"],
        )

    def test_09_coordinator_model_choice_overrides_adapter_reselection(self):
        """The worker must never receive a weaker model than the coordinator authorised."""
        req_id = "req-routing-authority-09"
        self._add_pipeline_request(req_id)

        backend = StubWorkerBackend(head_sha=self.HEAD_SHA)
        adapter = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            worker_backend=backend,
            notify_telegram=True,
            telegram_dry_run=True,
        )

        res = adapter.run_step(request_id=req_id)
        recommendation = (res.dispatch_packet or {}).get("recommendation") or {}
        coordinator_model = adapter.coordinator.evaluate_step(request_id=req_id).routing.recommended_model

        # Whatever the selector re-derives, the canonical dispatch key carries the
        # coordinator's exact model to the backend.
        if coordinator_model:
            self.assertEqual(recommendation.get("model"), coordinator_model)
            self.assertEqual(backend.calls[0]["model"], coordinator_model)
            self.assertNotIn("selected_model", recommendation)


    def test_13_configured_card_uses_native_project_v2_transition_hook(self):
        """A real ledger transition invokes the native updater for its configured card."""
        req_id = "req-native-board-13"
        self._add_pipeline_request(req_id)
        self.ledger.update_request(
            req_id,
            superboard_update={"item_id": "ITEM_1", "status": "Building"},
            actor="test-setup",
        )
        observed_status = {"name": "Building"}
        mutations = []

        def graphql_runner(query, variables):
            if "updateProjectV2ItemFieldValue" in query:
                mutations.append(dict(variables))
                observed_status["name"] = "QA"
                return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_1"}}}}
            if "projectV2" in query:
                return {
                    "data": {"repositoryOwner": {"projectV2": {
                        "id": "PVT_1",
                        "title": "Superboard",
                        "fields": {"nodes": [{
                            "id": "STATUS_FIELD",
                            "name": "Status",
                            "options": [{"id": "OPT_QA", "name": "QA"}],
                        }]},
                    }}}
                }
            if "projectItems" in query:
                return {
                    "data": {"repository": {"issue": {"projectItems": {"nodes": [{
                        "id": "ITEM_1",
                        "project": {"id": "PVT_1", "number": 1, "title": "Superboard"},
                        "fieldValueByName": {"name": observed_status["name"]},
                    }]}}}}
                }
            return {}

        original_runner = project_adapter.default_graphql_runner
        project_adapter.default_graphql_runner = graphql_runner
        try:
            result = SuperboardExecutionAdapter(
                state_dir=self.state_dir,
                worker_backend=StubWorkerBackend(head_sha=self.HEAD_SHA),
                notify_telegram=False,
            ).run_step(request_id=req_id)
        finally:
            project_adapter.default_graphql_runner = original_runner

        self.assertEqual(result.status, "advanced")
        self.assertEqual(result.gate_result["board_update"]["status"], "updated")
        self.assertEqual(len(mutations), 1)
        self.assertEqual(self.ledger.get_request(req_id)["superboard"]["status"], "QA")

    def test_14_ui_bug_qa_execution_path_enforces_closure_assets(self):
        """UI bug QA cannot advance without signed-in visual/original-scenario closure proof."""
        req_id = "bug-ui-slider-14"
        self.ledger.add_request(
            req_id=req_id,
            prompt="Slider overlaps the chart for signed-in users",
            session="test-ui-bug",
            project="SuperboardCore",
            acceptance_criteria=[
                {"criterion": "Original overlap is absent", "status": "pending", "evidence": ""}
            ],
            owner="UILane",
            state="QA",
            task_type="local_doc",
            head=self.HEAD_SHA,
            labels=["type:bug", "area:ui"],
        )
        backend = StubWorkerBackend(
            head_sha=self.HEAD_SHA,
            reproduction={
                "verdict": "absent",
                "scenario": "Open the signed-in dashboard at 390px and inspect slider overlap",
            },
            evidence={
                "checks": [{
                    "name": "original scenario",
                    "command": ["browser", "open-dashboard"],
                    "exit_code": 0,
                    "observed": "no overlap",
                }]
            },
        )

        result = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            worker_backend=backend,
            notify_telegram=False,
        ).run_step(request_id=req_id)

        self.assertEqual(result.status, "advanced")
        self.assertTrue(result.gate_result["reopened"])
        self.assertIn("Desktop after-fix visual asset", result.gate_result["repro_refused"])
        self.assertEqual(self.ledger.get_request(req_id)["state"], "implementation")

    def test_15_native_preparation_is_pending_and_never_advances_state(self):
        req_id = "req-native-background"
        self._add_pipeline_request(req_id)

        class Ticket:
            state = "prepared"
            run_id = "native_" + "a" * 24
            head_sha = self.HEAD_SHA
            task_handle = None
            blocked_reason = None

            def to_dict(self):
                return {
                    "state": self.state,
                    "run_id": self.run_id,
                    "head_sha": self.head_sha,
                    "task_handle": self.task_handle,
                }

        class NativeBackend:
            default_backend = "native"

            def prepare_native(self, request):
                return Ticket()

        result = SuperboardExecutionAdapter(
            state_dir=self.state_dir,
            worker_backend=NativeBackend(),
            notify_telegram=False,
        ).run_step(request_id=req_id)

        self.assertEqual(result.status, "prepared")
        self.assertTrue(result.worker_result.is_pending)
        self.assertFalse(result.worker_result.is_verifiable_evidence)
        self.assertEqual(result.worker_result.native_run_id, Ticket.run_id)
        self.assertFalse(result.gate_result["verified"])
        self.assertFalse(result.boundaries["execution_dispatched"])
        self.assertEqual(self.ledger.get_request(req_id)["state"], "implementation")

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
