#!/usr/bin/env python3
"""
test_decision_recovery.py — Focused behavioral tests for the decision lifecycle.

Reproduces and defends the fix for req-4574-decision-reply-recovery:

1. Unanswered-decision poisoning (the original defect):
   A stale or agent-authored comment was a *rejected input*, but `process_reply`
   wrote that outcome onto the *question* (`status = "rejected"`). `sync_decisions`
   only scanned pending decisions, so every later genuine operator answer was
   silently skipped and the blocked request deadlocked forever.
2. Rejected reply outcomes and audit are kept separate from question lifecycle
   state; a question stays open (`pending` / `clarification_requested`) until it
   is genuinely answered.
3. A clarification-requested question can still receive a later authenticated
   answer, including through `sync_decisions`.
4. Replaying an unchanged rejected comment is idempotent: no duplicate audit
   rows, no repeated ledger writes — and provenance validation still runs, so an
   edited body is re-evaluated as a fresh input.
5. Explicit fail-closed recovery for legacy `rejected` records that carry no
   answer and a demonstrable input-rejection history. Recovery restores an
   unanswered actionable question only: it never manufactures an answer, never
   clears authorization or decision blockers, and refuses terminal/resolved or
   ambiguous records.

Everything here is offline: the GitHub comment fetcher and the `gh` subprocess
used by `sync_decisions` are replaced by faithful in-process fakes.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import decision_workflow
from decision_workflow import (
    DecisionContract,
    DecisionManager,
    DecisionRecoveryRefused,
    DecisionScope,
    DecisionStatus,
    OPEN_DECISION_STATUSES,
    ProvenanceType,
)
from ledger import RequestLedger

PYTHON_EXE = sys.executable

QUESTION_POSTED_AT = "2026-09-01T10:00:00+00:00"
BEFORE_QUESTION = "2026-08-30T09:00:00+00:00"
AFTER_QUESTION = "2026-09-02T11:00:00+00:00"

OPTIONS = [
    {
        "id": "A",
        "label": "Dedicated audit_events table",
        "description": "Normalized append-only audit table.",
        "tradeoffs": "More joins, cleaner retention policy.",
    },
    {
        "id": "B",
        "label": "Inline JSON audit column",
        "description": "Audit payload stored on the row.",
        "tradeoffs": "Fewer joins, weaker query ergonomics.",
    },
]


class _FakeCompleted:
    """Minimal stand-in for subprocess.CompletedProcess as sync_decisions uses it."""

    def __init__(self, stdout: str):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


class DecisionLifecycleTestBase(unittest.TestCase):
    REQ_ID = "req-4574-decision-reply-recovery"
    DEC_ID = "DEC-4574-01"
    ISSUE_NUMBER = 4574
    REPO = "Bavariance/polysimulator"

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dec_recovery_")
        self.decisions_path = os.path.join(self.tmpdir, "decisions.json")
        self.ledger_path = os.path.join(self.tmpdir, "ledger.json")

        self.ledger = RequestLedger(self.ledger_path)
        self.ledger.add_request(
            req_id=self.REQ_ID,
            prompt="Choose the audit storage shape for the decision workflow.",
            session="offline-test",
            project="portable-workflow-core",
            acceptance_criteria=["Audit storage shape selected by the operator"],
            owner="BuildWorker",
            task_type="harness",
        )

        self.mgr = DecisionManager(
            decisions_path=self.decisions_path,
            ledger_path=self.ledger_path,
            comment_fetcher=self._fake_fetch,
        )
        self.comments = {}
        self.mgr.register_question(self._contract())
        self._set_question_posted(QUESTION_POSTED_AT)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- fixtures ------------------------------------------------------------

    def _contract(self, **overrides) -> DecisionContract:
        kwargs = dict(
            decision_id=self.DEC_ID,
            request_id=self.REQ_ID,
            prompt="Harden the decision workflow audit trail.",
            question="Should audit rows live in a dedicated table or an inline column?",
            options=OPTIONS,
            recommendation="Option A: Dedicated audit_events table",
            blocking_dependencies=[self.REQ_ID],
            authorized_responders=["Wladefant"],
            decision_scope=DecisionScope.DESIGN_CHOICE,
            issue_number=self.ISSUE_NUMBER,
        )
        kwargs.update(overrides)
        return DecisionContract(**kwargs)

    def _set_question_posted(self, timestamp: str, comment_id: str = "9000"):
        raw = self._raw_store()
        raw["decisions"][self.DEC_ID]["question_posted_at"] = timestamp
        raw["decisions"][self.DEC_ID]["question_comment_id"] = comment_id
        self._write_store(raw)

    def _raw_store(self):
        with open(self.decisions_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_store(self, data):
        with open(self.decisions_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _fake_fetch(self, repo: str, comment_id: str):
        key = str(comment_id)
        if key not in self.comments:
            raise RuntimeError(f"fake fetcher has no comment {key}")
        c = dict(self.comments[key])
        c.setdefault("issue_url", f"https://api.github.com/repos/{repo}/issues/{self.ISSUE_NUMBER}")
        return c

    def _add_comment(
        self,
        comment_id: str,
        body: str,
        user: str = "Wladefant",
        created_at: str = AFTER_QUESTION,
        updated_at=None,
    ):
        self.comments[str(comment_id)] = {
            "id": comment_id,
            "user": user,
            "body": body,
            "created_at": created_at,
            "updated_at": updated_at or created_at,
            "html_url": (
                f"https://github.com/{self.REPO}/issues/{self.ISSUE_NUMBER}"
                f"#issuecomment-{comment_id}"
            ),
        }
        return self.comments[str(comment_id)]

    def _sync_with(self, comment_ids, decision_id=None):
        """Run the real sync_decisions against a fake `gh api` comment stream."""
        payload = "\n".join(json.dumps(self.comments[str(c)]) for c in comment_ids)

        def fake_run(cmd, *args, **kwargs):
            self.assertEqual(cmd[0], "gh", "sync must go through the gh comment API")
            return _FakeCompleted(payload)

        with mock.patch.object(decision_workflow.subprocess, "run", side_effect=fake_run):
            return self.mgr.sync_decisions(decision_id=decision_id, repo=self.REPO, once=True)

    def _decision(self):
        return self.mgr.get_decision(self.DEC_ID)

    def _request(self):
        return self.ledger.get_request(self.REQ_ID)

    def _audit_len(self):
        return len(self._decision().get("audit_trail", []))


class TestRejectedInputDoesNotPoisonQuestion(DecisionLifecycleTestBase):
    """A rejected reply is an input outcome, never a question lifecycle outcome."""

    def test_stale_comment_leaves_question_open_and_audited(self):
        self._add_comment("1001", "Option A", created_at=BEFORE_QUESTION)
        res = self.mgr.ingest_comment(
            decision_id=self.DEC_ID,
            comment_id="1001",
            repo=self.REPO,
        )

        self.assertEqual(res["status"], "rejected", "Stale input must be rejected")
        self.assertIn("Stale reply", res["rejection_reason"])

        dec = self._decision()
        self.assertEqual(
            dec["status"],
            DecisionStatus.PENDING,
            "A stale reply must NOT move the unanswered question out of pending",
        )
        self.assertIsNone(dec["answer"], "A rejected input never records an answer")
        self.assertIn(dec["status"], OPEN_DECISION_STATUSES)

        audit = dec["audit_trail"]
        self.assertEqual(len(audit), 1, "The rejected input must be audited exactly once")
        self.assertEqual(audit[0]["status"], "rejected")
        self.assertEqual(audit[0]["comment_id"], "1001")

        rejected = dec["rejected_inputs"]["1001"]
        self.assertEqual(rejected["occurrences"], 1)
        self.assertIn("Stale reply", rejected["reason"])
        self.assertEqual(dec["last_rejected_input"]["comment_id"], "1001")

        req = self._request()
        self.assertIn(self.DEC_ID, req["decision_blockers"], "Blocker must be preserved")
        self.assertIn("REJECTED", req["blocker"])
        self.assertEqual(req["authorization"]["status"], "pending")

    def test_agent_authored_comment_leaves_question_open(self):
        self._add_comment("1002", "Option B", user="Wladefant")
        self.mgr.record_authored_comment("1002")

        res = self.mgr.ingest_comment(
            decision_id=self.DEC_ID, comment_id="1002", repo=self.REPO
        )
        self.assertEqual(res["status"], "rejected")
        self.assertIn("Authored-comment exclusion", res["rejection_reason"])

        dec = self._decision()
        self.assertEqual(dec["status"], DecisionStatus.PENDING)
        self.assertIsNone(dec["answer"])
        self.assertEqual(dec["rejected_inputs"]["1002"]["provenance"], ProvenanceType.AGENT_AUTHORED)

    def test_question_comment_itself_never_answers_and_never_poisons(self):
        self._add_comment("9000", "Option A")
        res = self.mgr.ingest_comment(
            decision_id=self.DEC_ID, comment_id="9000", repo=self.REPO
        )
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(self._decision()["status"], DecisionStatus.PENDING)

    def test_unauthorized_responder_leaves_question_open(self):
        self._add_comment("1003", "Option A", user="drive-by-contributor")
        res = self.mgr.ingest_comment(
            decision_id=self.DEC_ID, comment_id="1003", repo=self.REPO
        )
        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["provenance"], ProvenanceType.UNAUTHORIZED_ACTOR)

        dec = self._decision()
        self.assertEqual(dec["status"], DecisionStatus.PENDING)
        self.assertIsNone(dec["answer"])
        self.assertEqual(self._request()["authorization"]["status"], "pending")

    def test_forged_actor_is_refused_before_any_state_change(self):
        self._add_comment("1004", "Option A", user="Wladefant")
        with self.assertRaises(ValueError):
            self.mgr.ingest_comment(
                decision_id=self.DEC_ID,
                comment_id="1004",
                repo=self.REPO,
                caller_responder="someone-else",
            )
        dec = self._decision()
        self.assertEqual(dec["status"], DecisionStatus.PENDING)
        self.assertEqual(len(dec["audit_trail"]), 0, "A forged claim must not be audited as a reply")

    def test_later_genuine_answer_is_accepted_after_a_rejected_input(self):
        self._add_comment("1001", "Option A", created_at=BEFORE_QUESTION)
        self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="1001", repo=self.REPO)

        self._add_comment("1010", "Option B", created_at=AFTER_QUESTION)
        res = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="1010", repo=self.REPO)

        self.assertEqual(res["status"], "answered")
        self.assertEqual(res["unblocked_requests"], [self.REQ_ID])

        dec = self._decision()
        self.assertEqual(dec["status"], DecisionStatus.ANSWERED)
        self.assertEqual(dec["answer"]["selected_option_id"], "B")
        self.assertEqual(dec["answer"]["comment_id"], "1010")
        self.assertIsNotNone(dec["answer"]["answered_at"])
        self.assertEqual(len(dec["audit_trail"]), 2, "Both the rejected and the accepted input are audited")

        req = self._request()
        self.assertNotIn(self.DEC_ID, req["decision_blockers"])
        self.assertIsNone(req["blocker"])


class TestOriginalPoisoningReproduction(DecisionLifecycleTestBase):
    """The exact original failure scenario, driven through sync_decisions."""

    def test_sync_after_stale_rejection_still_accepts_the_real_answer(self):
        stale = self._add_comment("2001", "Option A", created_at=BEFORE_QUESTION)

        first = self._sync_with(["2001"])
        self.assertEqual(first["comments_evaluated"], 1)
        self.assertEqual(first["resolved_decisions"], [])
        self.assertEqual(
            self._decision()["status"],
            DecisionStatus.PENDING,
            "POISONING: the stale comment must not close the unanswered question",
        )

        # The operator answers later. Under the defect, sync_decisions no longer
        # selected this decision at all, because it was no longer 'pending'.
        self._add_comment("2002", "Go with Option B", created_at=AFTER_QUESTION)
        second = self._sync_with(["2001", "2002"])

        self.assertEqual(
            second["decisions_checked"],
            [self.DEC_ID],
            "An unanswered question must remain visible to sync after a rejected input",
        )
        self.assertEqual(second["resolved_decisions"], [self.DEC_ID])
        self.assertEqual(second["unblocked_requests"], [self.REQ_ID])

        dec = self._decision()
        self.assertEqual(dec["status"], DecisionStatus.ANSWERED)
        self.assertEqual(dec["answer"]["selected_option_id"], "B")
        self.assertEqual(dec["answer"]["comment_id"], "2002")
        self.assertEqual(stale["id"], "2001")

        req = self._request()
        self.assertNotIn(self.DEC_ID, req["decision_blockers"])


class TestClarificationCanStillBeAnswered(DecisionLifecycleTestBase):
    def test_clarification_requested_question_accepts_a_later_answer(self):
        self._add_comment("3001", "maybe either one, not sure")
        res = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="3001", repo=self.REPO)
        self.assertEqual(res["status"], "clarification_requested")

        dec = self._decision()
        self.assertEqual(dec["status"], DecisionStatus.CLARIFICATION_REQUESTED)
        self.assertIsNotNone(dec["clarification_prompt"])
        self.assertIsNone(dec["answer"])

        self._add_comment("3002", "Option A")
        second = self._sync_with(["3001", "3002"])
        self.assertEqual(
            second["decisions_checked"],
            [self.DEC_ID],
            "A clarification-requested question is still actionable for sync",
        )
        self.assertEqual(second["resolved_decisions"], [self.DEC_ID])

        dec = self._decision()
        self.assertEqual(dec["status"], DecisionStatus.ANSWERED)
        self.assertEqual(dec["answer"]["selected_option_id"], "A")
        self.assertIsNone(dec["clarification_prompt"])


class TestRejectedReplayIdempotency(DecisionLifecycleTestBase):
    def test_unchanged_rejected_comment_replay_adds_no_duplicate_audit(self):
        self._add_comment("4001", "Option A", created_at=BEFORE_QUESTION)

        first = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="4001", repo=self.REPO)
        self.assertFalse(first["idempotent_replay"])
        self.assertEqual(self._audit_len(), 1)
        blocker_after_first = self._request()["blocker"]

        for _ in range(3):
            replay = self.mgr.ingest_comment(
                decision_id=self.DEC_ID, comment_id="4001", repo=self.REPO
            )
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(replay["status"], "rejected")

        dec = self._decision()
        self.assertEqual(len(dec["audit_trail"]), 1, "Replays must not duplicate audit rows")
        self.assertEqual(dec["rejected_inputs"]["4001"]["occurrences"], 4)
        self.assertEqual(dec["status"], DecisionStatus.PENDING)
        self.assertEqual(self._request()["blocker"], blocker_after_first)

    def test_edited_rejected_comment_is_revalidated_as_a_new_input(self):
        self._add_comment("4002", "Option A", user="drive-by-contributor")
        self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="4002", repo=self.REPO)
        self.assertEqual(self._audit_len(), 1)

        # Same comment id, edited body: provenance validation must run again.
        self._add_comment(
            "4002",
            "Option B",
            user="drive-by-contributor",
            created_at=AFTER_QUESTION,
            updated_at="2026-09-03T12:00:00+00:00",
        )
        second = self.mgr.ingest_comment(
            decision_id=self.DEC_ID, comment_id="4002", repo=self.REPO
        )
        self.assertFalse(second["idempotent_replay"], "An edited comment is a fresh input")
        self.assertEqual(second["status"], "rejected")
        self.assertEqual(self._audit_len(), 2)
        self.assertEqual(self._decision()["rejected_inputs"]["4002"]["occurrences"], 1)

    def test_replay_does_not_downgrade_provenance_checks(self):
        """An authored comment replayed under a human-provenance call is still rejected."""
        self._add_comment("4003", "Option A")
        self.mgr.record_authored_comment("4003")
        self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="4003", repo=self.REPO)

        replay = self.mgr.process_reply(
            decision_id=self.DEC_ID,
            reply_text="Option A",
            responder="Wladefant",
            comment_id="4003",
            provenance=ProvenanceType.HUMAN_OPERATOR,
            comment_created_at=AFTER_QUESTION,
            comment_updated_at=AFTER_QUESTION,
        )
        self.assertEqual(replay["status"], "rejected")
        self.assertIsNone(self._decision()["answer"])
        self.assertEqual(self._request()["authorization"]["status"], "pending")

    def test_answered_replay_remains_idempotent(self):
        self._add_comment("4010", "Option A")
        self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="4010", repo=self.REPO)
        audit_len = self._audit_len()

        replay = self.mgr.ingest_comment(
            decision_id=self.DEC_ID, comment_id="4010", repo=self.REPO
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["status"], DecisionStatus.ANSWERED)
        self.assertEqual(self._audit_len(), audit_len)


class TestLegacyRejectedRecovery(DecisionLifecycleTestBase):
    """Fail-closed recovery of legacy poisoned records."""

    def _poison_legacy_record(self, rejection_status="rejected", answer=None, extra_audit=None):
        """Write the record shape the old code produced: status rejected, no answer."""
        raw = self._raw_store()
        dec = raw["decisions"][self.DEC_ID]
        dec["status"] = "rejected"
        dec["rejection_reason"] = (
            "Stale reply: Comment timestamp (2026-08-30T09:00:00+00:00) is earlier than "
            "decision question creation timestamp (2026-09-01T10:00:00+00:00)."
        )
        dec["answer"] = answer
        dec["audit_trail"] = [
            {
                "timestamp": "2026-08-31T09:05:00+00:00",
                "responder": "Wladefant",
                "comment_id": "5001",
                "comment_url": None,
                "reply_text": "Option A",
                "status": rejection_status,
                "provenance": ProvenanceType.HUMAN_OPERATOR,
                "interpretation": "Stale comment rejected.",
                "rejection_reason": dec["rejection_reason"],
                "clarification_prompt": None,
                "is_test": False,
                "comment_created_at": BEFORE_QUESTION,
                "comment_updated_at": BEFORE_QUESTION,
            }
        ]
        if extra_audit:
            dec["audit_trail"].extend(extra_audit)
        self._write_store(raw)
        self.ledger.update_request(
            req_id=self.REQ_ID,
            blocker=f"BLOCKED: Decision [{self.DEC_ID}] reply from @Wladefant was REJECTED",
            actor="decision-workflow",
            reason="legacy poisoned state",
        )
        return dec

    def test_recovery_restores_only_an_unanswered_actionable_question(self):
        self._poison_legacy_record()
        before = self._request()

        res = self.mgr.recover_rejected_question(
            decision_id=self.DEC_ID,
            actor="Wladefant",
            reason="Legacy stale-comment poisoning; question was never answered.",
        )

        self.assertTrue(res["recovered"])
        self.assertEqual(res["previous_status"], "rejected")
        self.assertEqual(res["restored_status"], DecisionStatus.PENDING)
        self.assertEqual(res["rejected_input_comment_ids"], ["5001"])

        dec = self._decision()
        self.assertEqual(dec["status"], DecisionStatus.PENDING)
        self.assertIn(dec["status"], OPEN_DECISION_STATUSES)
        self.assertIsNone(dec["answer"], "Recovery must NEVER manufacture an answer")
        self.assertIsNone(dec["rejection_reason"])

        # Audit and question binding are retained, plus one recovery entry.
        self.assertEqual(len(dec["audit_trail"]), 2)
        self.assertEqual(dec["audit_trail"][0]["comment_id"], "5001")
        recovery_entry = dec["audit_trail"][-1]
        self.assertEqual(recovery_entry["action"], "legacy_rejected_recovery")
        self.assertEqual(recovery_entry["previous_status"], "rejected")
        self.assertEqual(recovery_entry["actor"], "Wladefant")
        self.assertIn("Stale reply", recovery_entry["prior_rejection_reason"])
        self.assertEqual(dec["question_comment_id"], "9000")
        self.assertEqual(dec["request_id"], self.REQ_ID)
        self.assertEqual(dec["blocking_dependencies"], [self.REQ_ID])
        self.assertEqual(dec["recovery"]["actor"], "Wladefant")

        # No authority granted: blockers and authorization are untouched.
        req = self._request()
        self.assertIn(self.DEC_ID, req["decision_blockers"], "Recovery must not clear blockers")
        self.assertIsNotNone(req["blocker"])
        self.assertIn(self.DEC_ID, req["blocker"])
        self.assertEqual(req["authorization"]["status"], before["authorization"]["status"])
        self.assertIsNone(req["authorization"]["authorized_by"])
        self.assertEqual(req["state"], before["state"])
        self.assertEqual(req["evidence"], before["evidence"], "Recovery adds no decision evidence")

    def test_recovered_question_can_then_be_answered_normally(self):
        self._poison_legacy_record()
        self.mgr.recover_rejected_question(
            decision_id=self.DEC_ID, actor="Wladefant", reason="legacy poisoning"
        )

        self._add_comment("5010", "Option A")
        res = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="5010", repo=self.REPO)
        self.assertEqual(res["status"], "answered")
        self.assertEqual(self._decision()["answer"]["comment_id"], "5010")

    def test_recovery_restores_clarification_when_that_was_the_open_state(self):
        raw = self._raw_store()
        dec = raw["decisions"][self.DEC_ID]
        dec["status"] = "rejected"
        dec["clarification_prompt"] = "Please clarify: Option A or Option B?"
        dec["rejection_reason"] = "Authored-comment exclusion: agent signature."
        dec["audit_trail"] = [
            {
                "timestamp": "2026-09-01T11:00:00+00:00",
                "comment_id": "5100",
                "status": "clarification_requested",
                "responder": "Wladefant",
                "reply_text": "not sure",
                "interpretation": "Ambiguous reply.",
                "rejection_reason": None,
                "clarification_prompt": "Please clarify: Option A or Option B?",
            },
            {
                "timestamp": "2026-09-01T12:00:00+00:00",
                "comment_id": "5101",
                "status": "rejected",
                "responder": "Wladefant",
                "reply_text": "Option A",
                "interpretation": "Self-authored comment rejected.",
                "rejection_reason": "Authored-comment exclusion: agent signature.",
                "clarification_prompt": None,
            },
        ]
        self._write_store(raw)

        res = self.mgr.recover_rejected_question(
            decision_id=self.DEC_ID, actor="Wladefant", reason="authored-comment poisoning"
        )
        self.assertEqual(res["restored_status"], DecisionStatus.CLARIFICATION_REQUESTED)
        recovered = self._decision()
        self.assertEqual(recovered["status"], DecisionStatus.CLARIFICATION_REQUESTED)
        self.assertEqual(recovered["clarification_prompt"], "Please clarify: Option A or Option B?")
        self.assertIsNone(recovered["answer"])

    def test_recovery_refuses_answered_terminal_record(self):
        self._add_comment("5200", "Option A")
        self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="5200", repo=self.REPO)
        self.assertEqual(self._decision()["status"], DecisionStatus.ANSWERED)

        with self.assertRaises(DecisionRecoveryRefused) as ctx:
            self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(ctx.exception.code, "status_not_legacy_rejected")
        self.assertEqual(self._decision()["status"], DecisionStatus.ANSWERED)

    def test_recovery_refuses_open_pending_record(self):
        with self.assertRaises(DecisionRecoveryRefused) as ctx:
            self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(ctx.exception.code, "status_not_legacy_rejected")

    def test_recovery_refuses_rejected_record_that_carries_an_answer(self):
        self._poison_legacy_record(
            answer={
                "comment_id": "5300",
                "responder": "Wladefant",
                "answered_at": AFTER_QUESTION,
                "selected_option_id": "A",
                "interpretation": "Explicit choice: Option A",
                "provenance": ProvenanceType.HUMAN_OPERATOR,
            }
        )
        with self.assertRaises(DecisionRecoveryRefused) as ctx:
            self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(ctx.exception.code, "answer_present")
        dec = self._decision()
        self.assertEqual(dec["status"], "rejected")
        self.assertEqual(dec["answer"]["comment_id"], "5300")

    def test_recovery_refuses_without_demonstrable_input_rejection_history(self):
        raw = self._raw_store()
        raw["decisions"][self.DEC_ID]["status"] = "rejected"
        raw["decisions"][self.DEC_ID]["rejection_reason"] = "Opaque legacy rejection"
        raw["decisions"][self.DEC_ID]["audit_trail"] = []
        self._write_store(raw)

        with self.assertRaises(DecisionRecoveryRefused) as ctx:
            self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(ctx.exception.code, "no_input_rejection_history")
        self.assertEqual(self._decision()["status"], "rejected")

    def test_recovery_refuses_ambiguous_record_with_answered_history(self):
        self._poison_legacy_record(
            extra_audit=[
                {
                    "timestamp": "2026-09-02T09:00:00+00:00",
                    "comment_id": "5400",
                    "status": "answered",
                    "responder": "Wladefant",
                    "reply_text": "Option A",
                    "interpretation": "Explicit choice: Option A",
                    "rejection_reason": None,
                    "clarification_prompt": None,
                }
            ]
        )
        with self.assertRaises(DecisionRecoveryRefused) as ctx:
            self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(ctx.exception.code, "ambiguous_answered_history")
        self.assertEqual(self._decision()["status"], "rejected")

    def test_recovery_refuses_when_the_ledger_already_resolved_the_decision(self):
        self._poison_legacy_record()
        self.ledger.resolve_decision(
            req_id=self.REQ_ID,
            decision_id=self.DEC_ID,
            answer="Explicit choice: Option A",
            comment_id="5500",
            actor="Wladefant",
        )
        with self.assertRaises(DecisionRecoveryRefused) as ctx:
            self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(ctx.exception.code, "ambiguous_ledger_resolution")
        self.assertEqual(self._decision()["status"], "rejected")

    def test_recovery_refuses_a_question_that_no_longer_passes_scope_validation(self):
        self._poison_legacy_record()
        raw = self._raw_store()
        raw["decisions"][self.DEC_ID]["question"] = (
            "Should we deploy to production to unblock this?"
        )
        self._write_store(raw)

        with self.assertRaises(DecisionRecoveryRefused) as ctx:
            self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(ctx.exception.code, "unsafe_question_scope")
        self.assertEqual(self._decision()["status"], "rejected")

    def test_recovery_refuses_unknown_decision(self):
        with self.assertRaises(KeyError):
            self.mgr.recover_rejected_question(decision_id="DEC-does-not-exist", actor="Wladefant")

    def test_recovery_is_not_repeatable_once_the_question_is_open(self):
        self._poison_legacy_record()
        self.mgr.recover_rejected_question(
            decision_id=self.DEC_ID, actor="Wladefant", reason="legacy poisoning"
        )
        audit_len = self._audit_len()
        with self.assertRaises(DecisionRecoveryRefused):
            self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(self._audit_len(), audit_len)


class TestRecoveryCLI(DecisionLifecycleTestBase):
    def _run_cli(self, *cli_args):
        cmd = [
            PYTHON_EXE,
            os.path.join(SCRIPT_DIR, "decision_workflow.py"),
            "--decisions",
            self.decisions_path,
            "--ledger",
            self.ledger_path,
            *cli_args,
        ]
        return subprocess.run(cmd, capture_output=True, text=True)

    def _poison(self):
        raw = self._raw_store()
        dec = raw["decisions"][self.DEC_ID]
        dec["status"] = "rejected"
        dec["rejection_reason"] = "Stale reply: comment predates the question."
        dec["audit_trail"] = [
            {
                "timestamp": "2026-08-31T09:05:00+00:00",
                "comment_id": "6001",
                "status": "rejected",
                "responder": "Wladefant",
                "reply_text": "Option A",
                "interpretation": "Stale comment rejected.",
                "rejection_reason": "Stale reply: comment predates the question.",
                "clarification_prompt": None,
            }
        ]
        self._write_store(raw)

    def test_cli_recover_succeeds_on_a_legacy_poisoned_record(self):
        self._poison()
        res = self._run_cli(
            "recover", self.DEC_ID, "--actor", "Wladefant", "--reason", "legacy poisoning", "--json"
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        payload = json.loads(res.stdout)
        self.assertTrue(payload["recovered"])
        self.assertEqual(payload["restored_status"], DecisionStatus.PENDING)
        self.assertIsNone(self._decision()["answer"])
        self.assertIn(self.DEC_ID, self._request()["decision_blockers"])

    def test_cli_recover_refuses_fail_closed_with_distinct_exit_code(self):
        res = self._run_cli("recover", self.DEC_ID, "--actor", "Wladefant")
        self.assertEqual(res.returncode, 3, f"stdout={res.stdout} stderr={res.stderr}")
        self.assertIn("REFUSED", res.stderr)
        self.assertIn("status_not_legacy_rejected", res.stderr)
        self.assertEqual(self._decision()["status"], DecisionStatus.PENDING)

    def test_cli_list_reports_open_statuses(self):
        res = self._run_cli("list")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(self.DEC_ID, res.stdout)
        self.assertIn("pending", res.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
