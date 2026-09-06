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
6. Answer time provenance (combined-review finding NEW-4):
   `answer.comment_created_at` is the comment's own API-reported creation time and
   is written only on an API-verified ingest, so a caller cannot substitute one;
   `answer.answered_at` is ingest audit only and routinely lags the comment. A
   resolved decision is terminal: only the exact authenticated comment replays,
   and no later or edited comment re-answers it or rewrites its timestamps.
7. Recovery ledger binding (combined-review finding NEW-5): recovery requires the
   ledger to show a non-terminal request holding an unresolved decision entry for
   that exact id. All-terminal, mismatched, missing and ambiguous bindings are
   refused, and a mixed record leaves its terminal half byte-identical.

The NEW-4/NEW-5 reproducers are faithful transcriptions of the record shapes the
combined review preserved under `local://WorkflowCombinedGateReview-evidence/`.
They are inert local fixtures: no installed store is read and no live or
production system is contacted. Fixture strings that name a production resource
exist only to prove the refusal that names them stays a refusal.

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
    CommentTimeProvenance,
    DecisionContract,
    DecisionManager,
    DecisionRecoveryRefused,
    DecisionScope,
    DecisionStatus,
    OPEN_DECISION_STATUSES,
    ProvenanceType,
    verified_comment_created_at,
)
from ledger import RequestLedger

PYTHON_EXE = sys.executable

QUESTION_POSTED_AT = "2026-09-01T10:00:00+00:00"
BEFORE_QUESTION = "2026-08-30T09:00:00+00:00"
AFTER_QUESTION = "2026-09-02T11:00:00+00:00"
LATE_ANSWER = "2026-09-04T08:00:00+00:00"


def iso(value: str) -> datetime.datetime:
    """Parse an offset-aware ISO-8601 instant the way the workflow does."""
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


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

    def _raw_ledger(self):
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_ledger(self, data):
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _ledger_decision_entries(self, req_id=None):
        return self.ledger.get_request(req_id or self.REQ_ID).get("decisions") or []

    def _poison_question(self, rejection_reason="Stale reply: comment predates the question."):
        """Write the legacy record shape: question stamped `rejected`, no answer."""
        raw = self._raw_store()
        dec = raw["decisions"][self.DEC_ID]
        dec["status"] = DecisionStatus.REJECTED
        dec["rejection_reason"] = rejection_reason
        dec["answer"] = None
        dec["audit_trail"] = [
            {
                "timestamp": "2026-08-31T09:05:00+00:00",
                "responder": "Wladefant",
                "comment_id": "5001",
                "reply_text": "Option A",
                "status": DecisionStatus.REJECTED,
                "provenance": ProvenanceType.HUMAN_OPERATOR,
                "interpretation": "Stale comment rejected.",
                "rejection_reason": rejection_reason,
                "clarification_prompt": None,
                "is_test": False,
                "comment_created_at": BEFORE_QUESTION,
                "comment_updated_at": BEFORE_QUESTION,
            }
        ]
        self._write_store(raw)
        return dec

    def _set_blocking_dependencies(self, req_ids):
        raw = self._raw_store()
        raw["decisions"][self.DEC_ID]["blocking_dependencies"] = list(req_ids)
        self._write_store(raw)


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


class TestVerifiedAnswerTimeProvenance(DecisionLifecycleTestBase):
    """
    NEW-4 producer half: what an answer records about *when* it was answered.

    The combined review's ADV8 reproducer answered a question on 2026-09-02 and
    ingested it after a failure at 06:14:48.538, and the record said the decision
    was answered at 06:14:48.572 — the ingest clock. Bounded sync runs at execution
    barriers, so that lag is the normal case, and a consumer ordering the answer
    against other events must read the comment's own creation time or nothing.
    """

    def test_ingest_persists_the_api_comment_creation_time_beside_the_ingest_clock(self):
        self._add_comment("7001", "Option A", created_at=AFTER_QUESTION)
        res = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="7001", repo=self.REPO)
        self.assertEqual(res["status"], "answered")

        ans = self._decision()["answer"]
        self.assertEqual(ans["comment_created_at"], AFTER_QUESTION)
        self.assertEqual(ans["comment_created_at_source"], CommentTimeProvenance.API_VERIFIED)
        # The ADV8 lag itself: the audit clock is strictly later than the comment,
        # and the two are recorded as different facts rather than one.
        self.assertNotEqual(ans["answered_at"], ans["comment_created_at"])
        self.assertGreater(iso(ans["answered_at"]), iso(ans["comment_created_at"]))

    def test_sync_ingest_also_records_the_comment_time_not_the_barrier_time(self):
        self._add_comment("7002", "Option B", created_at=AFTER_QUESTION)
        summary = self._sync_with(["7002"])
        self.assertEqual(summary["resolved_decisions"], [self.DEC_ID])

        ans = self._decision()["answer"]
        self.assertEqual(ans["comment_created_at"], AFTER_QUESTION)
        self.assertEqual(ans["comment_created_at_source"], CommentTimeProvenance.API_VERIFIED)
        self.assertGreater(iso(ans["answered_at"]), iso(ans["comment_created_at"]))

    def test_caller_supplied_creation_time_is_never_persisted_as_proof(self):
        forged = "2026-09-30T23:59:59+00:00"
        res = self.mgr.process_reply(
            decision_id=self.DEC_ID,
            reply_text="Option A",
            responder="Wladefant",
            comment_id="7003",
            comment_created_at=forged,
            comment_updated_at=forged,
            provenance=ProvenanceType.HUMAN_OPERATOR,
        )
        self.assertEqual(res["status"], "answered")

        ans = self._decision()["answer"]
        self.assertIsNone(ans["comment_created_at"], "an unverified time is not proof")
        self.assertEqual(ans["comment_created_at_source"], CommentTimeProvenance.CALLER_SUPPLIED)
        # The claim is still visible as an unverified claim in the audit row.
        audit = self._decision()["audit_trail"][-1]
        self.assertEqual(audit["comment_created_at"], forged)
        self.assertEqual(audit["comment_time_provenance"], CommentTimeProvenance.CALLER_SUPPLIED)

    def test_caller_supplied_stale_time_still_refuses_the_reply(self):
        """Unverified timestamps may close the gate on a reply, never open one."""
        res = self.mgr.process_reply(
            decision_id=self.DEC_ID,
            reply_text="Option A",
            responder="Wladefant",
            comment_id="7004",
            comment_created_at=BEFORE_QUESTION,
            comment_updated_at=BEFORE_QUESTION,
            provenance=ProvenanceType.HUMAN_OPERATOR,
        )
        self.assertEqual(res["status"], "rejected")
        self.assertIn("Stale reply", res["rejection_reason"])
        self.assertIsNone(self._decision()["answer"])

    def test_an_api_response_without_a_creation_time_records_no_proof(self):
        self._add_comment("7005", "Option A", created_at=None)
        res = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="7005", repo=self.REPO)
        self.assertEqual(res["status"], "answered")

        ans = self._decision()["answer"]
        self.assertIsNone(ans["comment_created_at"])
        self.assertEqual(ans["comment_created_at_source"], CommentTimeProvenance.MISSING)

    def test_a_timezone_naive_api_creation_time_records_no_proof(self):
        self._add_comment("7006", "Option A", created_at="2026-09-02T11:00:00")
        res = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="7006", repo=self.REPO)
        self.assertEqual(res["status"], "answered")

        ans = self._decision()["answer"]
        self.assertIsNone(ans["comment_created_at"], "an unorderable instant proves nothing")
        self.assertEqual(ans["comment_created_at_source"], CommentTimeProvenance.MALFORMED)

    def test_ingest_refuses_a_caller_creation_time_the_api_contradicts(self):
        self._add_comment("7007", "Option A", created_at=AFTER_QUESTION)
        with self.assertRaises(ValueError) as ctx:
            self.mgr.ingest_comment(
                decision_id=self.DEC_ID,
                comment_id="7007",
                repo=self.REPO,
                caller_created_at="2026-09-30T00:00:00+00:00",
            )
        self.assertIn("Timestamp forgery", str(ctx.exception))
        self.assertEqual(self._decision()["status"], DecisionStatus.PENDING)
        self.assertIsNone(self._decision()["answer"])

    def test_verified_comment_created_at_reduces_every_input_to_proof_or_nothing(self):
        self.assertEqual(
            verified_comment_created_at(AFTER_QUESTION, CommentTimeProvenance.API_VERIFIED),
            (AFTER_QUESTION, CommentTimeProvenance.API_VERIFIED),
        )
        self.assertEqual(
            verified_comment_created_at("2026-09-02T11:00:00Z", CommentTimeProvenance.API_VERIFIED),
            ("2026-09-02T11:00:00Z", CommentTimeProvenance.API_VERIFIED),
        )
        self.assertEqual(
            verified_comment_created_at(AFTER_QUESTION, CommentTimeProvenance.CALLER_SUPPLIED),
            (None, CommentTimeProvenance.CALLER_SUPPLIED),
        )
        self.assertEqual(
            verified_comment_created_at("  ", CommentTimeProvenance.API_VERIFIED),
            (None, CommentTimeProvenance.MISSING),
        )
        self.assertEqual(
            verified_comment_created_at("yesterday", CommentTimeProvenance.API_VERIFIED),
            (None, CommentTimeProvenance.MALFORMED),
        )


class TestResolvedDecisionIsTerminal(DecisionLifecycleTestBase):
    """
    NEW-4 re-answer half: the REANSWER reproducer posted a second authorized
    comment on an answered decision and replaced the answer, bumping `answered_at`
    so the old answer appeared to postdate any later failure.
    """

    def _answer(self, comment_id="8000", body="Option A", created_at=AFTER_QUESTION):
        self._add_comment(comment_id, body, created_at=created_at)
        res = self.mgr.ingest_comment(
            decision_id=self.DEC_ID, comment_id=comment_id, repo=self.REPO
        )
        self.assertEqual(res["status"], "answered", res)
        return dict(self._decision()["answer"])

    def test_a_later_authorized_comment_cannot_re_answer_a_resolved_decision(self):
        first = self._answer()
        req_before = self._request()

        self._add_comment("8001", "Option B", created_at=LATE_ANSWER)
        res = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="8001", repo=self.REPO)

        self.assertEqual(res["status"], DecisionStatus.REJECTED)
        self.assertTrue(res["reanswer_refused"])
        self.assertEqual(res["unblocked_requests"], [])
        self.assertIn("terminal", res["rejection_reason"])

        dec = self._decision()
        self.assertEqual(dec["status"], DecisionStatus.ANSWERED)
        self.assertEqual(dec["answer"], first, "the recorded answer is immutable")
        self.assertEqual(dec["answer"]["selected_option_id"], "A")
        self.assertIn("8001", dec["rejected_inputs"])

        # The request the genuine answer unblocked is not re-blocked by the refusal.
        req = self._request()
        self.assertIsNone(req["blocker"])
        self.assertEqual(req["decision_blockers"], [])
        self.assertEqual(req["state"], req_before["state"])

    def test_editing_the_answering_comment_cannot_rewrite_the_answer(self):
        first = self._answer()
        self.comments["8000"]["body"] = "Option B"
        self.comments["8000"]["updated_at"] = "2026-09-05T00:00:00+00:00"

        res = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="8000", repo=self.REPO)
        self.assertEqual(res["status"], DecisionStatus.REJECTED)
        self.assertTrue(res["reanswer_refused"])
        self.assertIn("edit rather than a replay", res["rejection_reason"])
        self.assertEqual(self._decision()["answer"], first)

    def test_a_replay_claiming_a_new_creation_time_is_refused(self):
        first = self._answer()
        self.comments["8000"]["created_at"] = "2026-09-30T00:00:00+00:00"

        res = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="8000", repo=self.REPO)
        self.assertEqual(res["status"], DecisionStatus.REJECTED)
        self.assertTrue(res["reanswer_refused"])
        self.assertIn("immutable", res["rejection_reason"])
        self.assertEqual(self._decision()["answer"]["comment_created_at"], AFTER_QUESTION)
        self.assertEqual(self._decision()["answer"], first)

    def test_a_settled_answer_without_proof_is_not_upgraded_after_the_fact(self):
        res = self.mgr.process_reply(
            decision_id=self.DEC_ID,
            reply_text="Option A",
            responder="Wladefant",
            comment_id="8100",
            provenance=ProvenanceType.HUMAN_OPERATOR,
        )
        self.assertEqual(res["status"], "answered")
        first = dict(self._decision()["answer"])
        self.assertIsNone(first["comment_created_at"])

        self._add_comment("8100", "Option A", created_at=AFTER_QUESTION)
        replay = self.mgr.ingest_comment(
            decision_id=self.DEC_ID, comment_id="8100", repo=self.REPO
        )
        self.assertEqual(replay["status"], DecisionStatus.REJECTED)
        self.assertTrue(replay["reanswer_refused"])
        self.assertEqual(self._decision()["answer"], first)
        self.assertIsNone(self._decision()["answer"]["comment_created_at"])

    def test_exact_authenticated_replay_stays_idempotent(self):
        first = self._answer()
        audit_len = self._audit_len()

        res = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="8000", repo=self.REPO)
        self.assertTrue(res["idempotent_replay"])
        self.assertEqual(res["status"], DecisionStatus.ANSWERED)
        self.assertEqual(self._decision()["answer"], first)
        self.assertEqual(self._audit_len(), audit_len, "a replay adds no audit row")

    def test_replaying_a_refused_reanswer_is_deduplicated(self):
        self._answer()
        self._add_comment("8200", "Option B", created_at=LATE_ANSWER)
        first = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="8200", repo=self.REPO)
        self.assertFalse(first["idempotent_replay"])
        audit_len = self._audit_len()

        again = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="8200", repo=self.REPO)
        self.assertTrue(again["idempotent_replay"])
        self.assertEqual(again["rejected_input_occurrences"], 2)
        self.assertEqual(self._audit_len(), audit_len)

    def test_a_reply_that_tries_to_authorize_production_stays_a_refusal(self):
        """
        A production-naming reply is refused as an unsafe input and authorizes
        nothing. The forbidden ref appears here only as inert fixture text.
        """
        self._add_comment(
            "8300",
            "Option A, and deploy to production on zaraprptkegxqpvnsubu once merged",
        )
        res = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="8300", repo=self.REPO)

        self.assertEqual(res["status"], DecisionStatus.REJECTED)
        dec = self._decision()
        self.assertIsNone(dec["answer"])
        self.assertIn(dec["status"], OPEN_DECISION_STATUSES)
        req = self._request()
        self.assertEqual(req["authorization"]["status"], "pending")
        self.assertIsNone(req["authorization"]["authorized_by"])
        self.assertIn(self.DEC_ID, req["decision_blockers"])


class TestRecoveryRequiresAnOpenLedgerBinding(DecisionLifecycleTestBase):
    """
    NEW-5: `recover` inspected only `decisions[].status` keyed by decision id and
    never the dependent request's state, so a legacy record whose ledger entry
    named a different id and whose only blocking request was already `done` still
    reopened — writing a fresh blocker onto finished work and dragging it back into
    every sync scan.
    """

    DONE_REQ = "req-4574-decision-demo-closed"

    def _add_done_request(self, req_id=None, decision_entry_id=None):
        req_id = req_id or self.DONE_REQ
        self.ledger.add_request(
            req_id=req_id,
            prompt="Closed companion work for the decision demo.",
            session="offline-test",
            project="portable-workflow-core",
            acceptance_criteria=["Companion work completed"],
            owner="BuildWorker",
            task_type="harness",
            state="done",
        )
        if decision_entry_id:
            self.ledger.add_decision(
                req_id=req_id,
                question=self._contract().question,
                options=["A: Alpha", "B: Beta"],
                decision_id=decision_entry_id,
                blocks=False,
                actor="decision-workflow",
            )
        return self.ledger.get_request(req_id)

    def test_recovery_refuses_when_every_blocking_request_is_terminal(self):
        self._add_done_request(decision_entry_id=self.DEC_ID)
        self._set_blocking_dependencies([self.DONE_REQ])
        self._poison_question()
        done_before = self.ledger.get_request(self.DONE_REQ)

        with self.assertRaises(DecisionRecoveryRefused) as ctx:
            self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(ctx.exception.code, "all_blocking_work_terminal")
        self.assertIn(self.DONE_REQ, ctx.exception.message)

        self.assertEqual(self._decision()["status"], DecisionStatus.REJECTED)
        self.assertEqual(self.ledger.get_request(self.DONE_REQ), done_before)
        self.assertIsNone(self.ledger.get_request(self.DONE_REQ)["blocker"])

    def test_recovery_refuses_when_the_ledger_entry_names_a_different_decision(self):
        raw = self._raw_ledger()
        entries = raw["requests"][self.REQ_ID]["decisions"]
        self.assertEqual(len(entries), 1)
        entries[0]["id"] = f"{self.DEC_ID}-DEMO"
        self._write_ledger(raw)
        self._poison_question()
        before = self._request()

        with self.assertRaises(DecisionRecoveryRefused) as ctx:
            self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(ctx.exception.code, "missing_ledger_binding")
        self.assertIn(f"{self.DEC_ID}-DEMO", ctx.exception.message)

        self.assertEqual(self._decision()["status"], DecisionStatus.REJECTED)
        self.assertEqual(self._request(), before)

    def test_recovery_refuses_when_the_blocking_request_is_not_in_the_ledger(self):
        self._set_blocking_dependencies(["req-not-in-any-ledger"])
        self._poison_question()

        with self.assertRaises(DecisionRecoveryRefused) as ctx:
            self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(ctx.exception.code, "missing_ledger_binding")
        self.assertIn("not in the ledger", ctx.exception.message)
        self.assertEqual(self._decision()["status"], DecisionStatus.REJECTED)

    def test_recovery_refuses_when_the_record_names_no_blocking_work(self):
        self._set_blocking_dependencies([])
        self._poison_question()

        with self.assertRaises(DecisionRecoveryRefused) as ctx:
            self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(ctx.exception.code, "no_blocking_work")
        self.assertEqual(self._decision()["status"], DecisionStatus.REJECTED)

    def test_recovery_refuses_a_duplicated_ambiguous_binding(self):
        self.ledger.add_decision(
            req_id=self.REQ_ID,
            question=self._contract().question,
            options=["A: Alpha", "B: Beta"],
            decision_id=self.DEC_ID,
            blocks=False,
            actor="decision-workflow",
        )
        self.assertEqual(len(self._ledger_decision_entries()), 2)
        self._poison_question()

        with self.assertRaises(DecisionRecoveryRefused) as ctx:
            self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(ctx.exception.code, "ambiguous_ledger_binding")
        self.assertEqual(self._decision()["status"], DecisionStatus.REJECTED)

    def test_mixed_dependencies_recover_the_open_half_and_leave_the_done_half_intact(self):
        done_before = self._add_done_request(decision_entry_id=self.DEC_ID)
        self._set_blocking_dependencies([self.REQ_ID, self.DONE_REQ])
        self._poison_question()

        res = self.mgr.recover_rejected_question(
            decision_id=self.DEC_ID, actor="Wladefant", reason="legacy stale-comment poisoning"
        )
        self.assertTrue(res["recovered"])
        self.assertEqual(res["bound_requests"], [self.REQ_ID])
        self.assertEqual(res["terminal_requests_untouched"], [self.DONE_REQ])

        # The finished record is byte-identical: no blocker, no next_action, no history row.
        self.assertEqual(self.ledger.get_request(self.DONE_REQ), done_before)

        # The open half is reopened with its blocker restated and nothing authorized.
        self.assertEqual(self._decision()["status"], DecisionStatus.PENDING)
        self.assertIsNone(self._decision()["answer"])
        open_req = self._request()
        self.assertIn(self.DEC_ID, open_req["blocker"])
        self.assertIn(self.DEC_ID, open_req["decision_blockers"])
        self.assertEqual(open_req["authorization"]["status"], "pending")

    def test_recovery_reports_the_binding_it_proved(self):
        self._poison_question()
        res = self.mgr.recover_rejected_question(decision_id=self.DEC_ID, actor="Wladefant")
        self.assertEqual(res["bound_requests"], [self.REQ_ID])
        self.assertEqual(res["terminal_requests_untouched"], [])
        self.assertEqual(res["unbound_requests_untouched"], [])
        recovery = self._decision()["recovery"]
        self.assertEqual(recovery["bound_requests"], [self.REQ_ID])
        self.assertFalse(recovery["authorization_granted"])


class TestPreservedReviewerRecoveryReproducers(unittest.TestCase):
    """
    The two legacy records the combined review drove `recover` against, transcribed
    from its preserved evidence into isolated temp state.

    `DEC-4543-01` is the bypass: last audited refusal is a production-ref safety
    refusal, its sole blocking request is `done`, and its ledger decision entry
    names `DEC-4543-01-DEMO`. `staging-ci-access-403` is the legitimate control:
    stale-reply poisoning with the request still in `implementation` and holding the
    blocker. Nothing here reads an installed store or touches a live system; the
    production ref is inert fixture text proving the refusal that names it survives.
    """

    DEMO_DEC = "DEC-4543-01"
    DEMO_REQ = "req-synthetic-decision-demo-4543"
    DEMO_LEDGER_ENTRY = "DEC-4543-01-DEMO"
    DEMO_SAFETY_REFUSAL = (
        "Safety refusal: Matched forbidden pattern 'zaraprptkegxqpvnsubu'. Issue comments "
        "cannot authorize production deployment, main branch merges, or destructive data "
        "actions per AGENTS.md policy."
    )
    CI_DEC = "staging-ci-access-403"
    CI_REQ = "req-4574-ci-staging-boundary"
    CI_BLOCKER = (
        "GitHub-runner Dokploy 403 access strategy remains pending operator decision "
        "staging-ci-access-403"
    )
    CI_STALE_REFUSAL = (
        "Stale reply: Comment timestamp (2026-09-06T02:30:05Z) is earlier than decision "
        "question creation timestamp (2026-09-06T02:42:01.314512+00:00)."
    )

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="dec_reviewer_")
        self.decisions_path = os.path.join(self.tmpdir, "decisions.json")
        self.ledger_path = os.path.join(self.tmpdir, "ledger.json")
        self.ledger = RequestLedger(self.ledger_path)

        self.ledger.add_request(
            req_id=self.DEMO_REQ,
            prompt="Synthetic decision workflow demonstration.",
            session="offline-test",
            project="portable-workflow-core",
            acceptance_criteria=["Decision workflow demonstrated"],
            owner="BuildWorker",
            task_type="harness",
            state="done",
        )
        self.ledger.add_decision(
            req_id=self.DEMO_REQ,
            question="How should autonomous background execution proceed?",
            options=["A: Park", "B: Proceed"],
            decision_id=self.DEMO_LEDGER_ENTRY,
            blocks=False,
            actor="decision-workflow",
        )
        self.ledger.add_request(
            req_id=self.CI_REQ,
            prompt="Replace the blocked GitHub-hosted runner path.",
            session="offline-test",
            project="portable-workflow-core",
            acceptance_criteria=["Access strategy selected by the operator"],
            owner="BuildWorker",
            task_type="harness",
            state="implementation",
        )
        self.ledger.add_decision(
            req_id=self.CI_REQ,
            question="Which approved access strategy should replace the blocked runner path?",
            options=["A: Self-hosted runner", "B: Reverse tunnel"],
            decision_id=self.CI_DEC,
            blocks=True,
            actor="decision-workflow",
        )
        self.ledger.update_request(
            req_id=self.CI_REQ,
            blocker=self.CI_BLOCKER,
            actor="decision-workflow",
            reason="legacy poisoned state",
        )

        self._write_store(
            {
                "version": 2,
                "authored_comment_ids": [],
                "synthetic_test_comment_ids": [],
                "decisions": {
                    self.DEMO_DEC: self._legacy_record(
                        decision_id=self.DEMO_DEC,
                        request_id=self.DEMO_REQ,
                        question=(
                            "When the agent swarm encounters a blocking human decision "
                            "question on a GitHub issue, how should autonomous background "
                            "execution proceed?"
                        ),
                        rejection_reason=self.DEMO_SAFETY_REFUSAL,
                        rejected_comment_ids=["5550734838", "5550751241", "5550773923"],
                        scope=DecisionScope.ARCHITECTURAL_PREFERENCE,
                        issue_number=4543,
                    ),
                    self.CI_DEC: self._legacy_record(
                        decision_id=self.CI_DEC,
                        request_id=self.CI_REQ,
                        question=(
                            "Which approved access strategy should replace the blocked "
                            "GitHub-hosted runner path?"
                        ),
                        rejection_reason=self.CI_STALE_REFUSAL,
                        rejected_comment_ids=[
                            "5555741072",
                            "5555967887",
                            "5556144132",
                            "5556360605",
                        ],
                        scope=DecisionScope.IMPLEMENTATION_STRATEGY,
                        issue_number=4574,
                    ),
                },
            }
        )
        self.mgr = DecisionManager(
            decisions_path=self.decisions_path, ledger_path=self.ledger_path
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _legacy_record(
        self,
        decision_id,
        request_id,
        question,
        rejection_reason,
        rejected_comment_ids,
        scope,
        issue_number,
    ):
        return {
            "decision_id": decision_id,
            "request_id": request_id,
            "prompt": "Decision workflow legacy record.",
            "question": question,
            "options": OPTIONS,
            "recommendation": "Option A: Dedicated audit_events table",
            "blocking_dependencies": [request_id],
            "authorized_responders": ["Wladefant"],
            "decision_scope": scope,
            "status": DecisionStatus.REJECTED,
            "format_preference": "plain",
            "issue_number": issue_number,
            "issue_url": None,
            "question_comment_id": "9001",
            "question_posted_at": QUESTION_POSTED_AT,
            "created_at": QUESTION_POSTED_AT,
            "updated_at": QUESTION_POSTED_AT,
            "answer": None,
            "clarification_prompt": None,
            "rejection_reason": rejection_reason,
            "audit_trail": [
                {
                    "timestamp": "2026-09-06T02:45:00+00:00",
                    "responder": "Wladefant",
                    "comment_id": cid,
                    "reply_text": "Option A",
                    "status": DecisionStatus.REJECTED,
                    "provenance": ProvenanceType.HUMAN_OPERATOR,
                    "interpretation": "Reply refused.",
                    "rejection_reason": rejection_reason,
                    "clarification_prompt": None,
                    "is_test": False,
                    "comment_created_at": BEFORE_QUESTION,
                    "comment_updated_at": BEFORE_QUESTION,
                }
                for cid in rejected_comment_ids
            ],
            "recovery": None,
        }

    def _write_store(self, data):
        with open(self.decisions_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

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

    def test_the_terminal_demo_record_is_refused_and_finished_work_is_untouched(self):
        demo_before = self.ledger.get_request(self.DEMO_REQ)
        self.assertEqual(demo_before["state"], "done")
        self.assertIsNone(demo_before["blocker"])
        self.assertEqual(
            [e["id"] for e in demo_before["decisions"]], [self.DEMO_LEDGER_ENTRY]
        )

        res = self._run_cli("recover", self.DEMO_DEC, "--actor", "probe", "--reason", "review probe")
        self.assertEqual(res.returncode, 3, f"stdout={res.stdout} stderr={res.stderr}")
        self.assertIn("REFUSED", res.stderr)
        self.assertIn("all_blocking_work_terminal", res.stderr)

        demo_after = self.ledger.get_request(self.DEMO_REQ)
        self.assertEqual(demo_after, demo_before, "recovery must not write to done work")
        self.assertIsNone(demo_after["blocker"])

        dec = self.mgr.get_decision(self.DEMO_DEC)
        self.assertEqual(dec["status"], DecisionStatus.REJECTED)
        self.assertIsNone(dec["answer"])
        self.assertIsNone(dec["recovery"])
        self.assertEqual(
            dec["rejection_reason"],
            self.DEMO_SAFETY_REFUSAL,
            "a production-ref safety refusal stays a refusal",
        )

    def test_the_refused_demo_record_stays_out_of_the_sync_window(self):
        with self.assertRaises(DecisionRecoveryRefused):
            self.mgr.recover_rejected_question(decision_id=self.DEMO_DEC, actor="probe")
        open_ids = [d["decision_id"] for d in self.mgr.list_open_decisions()]
        self.assertNotIn(self.DEMO_DEC, open_ids)

    def test_the_genuine_unanswered_ci_record_recovers_with_binding_and_blockers(self):
        before = self.ledger.get_request(self.CI_REQ)

        res = self.mgr.recover_rejected_question(
            decision_id=self.CI_DEC,
            actor="Wladefant",
            reason="Legacy stale-reply poisoning; the question was never answered.",
        )
        self.assertTrue(res["recovered"])
        self.assertEqual(res["restored_status"], DecisionStatus.PENDING)
        self.assertEqual(res["bound_requests"], [self.CI_REQ])
        self.assertEqual(res["terminal_requests_untouched"], [])
        self.assertIsNone(res["answer"])
        self.assertFalse(res["authorization_granted"])
        self.assertEqual(res["prior_rejection_reason"], self.CI_STALE_REFUSAL)
        self.assertEqual(
            res["rejected_input_comment_ids"],
            ["5555741072", "5555967887", "5556144132", "5556360605"],
        )

        dec = self.mgr.get_decision(self.CI_DEC)
        self.assertEqual(dec["status"], DecisionStatus.PENDING)
        self.assertIn(dec["status"], OPEN_DECISION_STATUSES)
        self.assertIsNone(dec["answer"])
        self.assertIsNone(dec["rejection_reason"])
        self.assertEqual(len(dec["audit_trail"]), 5)

        req = self.ledger.get_request(self.CI_REQ)
        self.assertEqual(req["state"], before["state"])
        self.assertIn(self.CI_DEC, req["decision_blockers"])
        self.assertIn(self.CI_DEC, req["blocker"])
        self.assertEqual(req["authorization"], before["authorization"])
        self.assertEqual(req["evidence"], before["evidence"])

    def test_recovering_the_ci_record_leaves_the_demo_record_alone(self):
        demo_before = self.mgr.get_decision(self.DEMO_DEC)
        demo_req_before = self.ledger.get_request(self.DEMO_REQ)

        self.mgr.recover_rejected_question(decision_id=self.CI_DEC, actor="Wladefant")

        self.assertEqual(self.mgr.get_decision(self.DEMO_DEC), demo_before)
        self.assertEqual(self.ledger.get_request(self.DEMO_REQ), demo_req_before)


class TestTerminalRequestsAreNeverBlockered(DecisionLifecycleTestBase):
    """
    The same invariant recovery enforces, one lifecycle earlier: no decision
    outcome writes a blocker onto finished work. A mixed record's terminal half
    stays byte-identical through a refused reply and a clarification request too,
    not only through `recover`.
    """

    DONE_REQ = "req-4574-decision-demo-closed"

    def setUp(self):
        super().setUp()
        self.ledger.add_request(
            req_id=self.DONE_REQ,
            prompt="Closed companion work for the decision demo.",
            session="offline-test",
            project="portable-workflow-core",
            acceptance_criteria=["Companion work completed"],
            owner="BuildWorker",
            task_type="harness",
            state="done",
        )
        self._set_blocking_dependencies([self.REQ_ID, self.DONE_REQ])
        self.done_before = self.ledger.get_request(self.DONE_REQ)

    def test_a_refused_reply_does_not_blocker_the_done_dependency(self):
        self._add_comment("9300", "Option A", created_at=BEFORE_QUESTION)
        res = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="9300", repo=self.REPO)
        self.assertEqual(res["status"], DecisionStatus.REJECTED)

        self.assertEqual(self.ledger.get_request(self.DONE_REQ), self.done_before)
        self.assertIsNone(self.ledger.get_request(self.DONE_REQ)["blocker"])
        # The open dependency is still blocked, as a refused reply should leave it.
        self.assertIn("REJECTED", self._request()["blocker"])

    def test_a_clarification_request_does_not_blocker_the_done_dependency(self):
        self._add_comment("9301", "not sure yet, maybe either one")
        res = self.mgr.ingest_comment(decision_id=self.DEC_ID, comment_id="9301", repo=self.REPO)
        self.assertEqual(res["status"], "clarification_requested")

        self.assertEqual(self.ledger.get_request(self.DONE_REQ), self.done_before)
        self.assertIsNone(self.ledger.get_request(self.DONE_REQ)["blocker"])
        self.assertIn("Clarification requested", self._request()["blocker"])

    def test_registering_a_question_does_not_blocker_a_done_dependency(self):
        self.mgr.register_question(
            self._contract(
                decision_id="DEC-4574-02",
                question="Should the audit index be partial or full?",
                blocking_dependencies=[self.REQ_ID, self.DONE_REQ],
            )
        )
        self.assertEqual(self.ledger.get_request(self.DONE_REQ), self.done_before)
        self.assertEqual(self.ledger.get_request(self.DONE_REQ)["decision_blockers"], [])
        self.assertIn("DEC-4574-02", self._request()["decision_blockers"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
