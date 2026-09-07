#!/usr/bin/env python3
"""Focused production-path proofs for GitHub decision input safety."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from decision_workflow import (
    DecisionContract,
    DecisionManager,
    DecisionScope,
    ProvenanceType,
    extract_additional_context,
    extract_scoped_task_list_options,
    extract_task_list_options,
    format_decision_markdown,
    fetch_github_comment_history_default,
)
from continuation_driver import ContinuationDriver
from ledger import RequestLedger

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


class DecisionUXProof(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="synthetic_decision_ux_")
        self.decisions_path = os.path.join(self.tmp, "decisions.json")
        self.ledger_path = os.path.join(self.tmp, "ledger.json")
        self.ledger = RequestLedger(self.ledger_path)
        self.ledger.add_request(
            req_id="REQ-1",
            prompt="Choose audit storage",
            session="synthetic-proof",
            project="Wladefant/super-board",
            owner="DecisionUXProof",
            acceptance_criteria=["A durable decision reaches the existing consumer"],
        )
        self.comments = {}
        self.mgr = DecisionManager(
            decisions_path=self.decisions_path,
            ledger_path=self.ledger_path,
            comment_fetcher=self._fetch_comment,
        )
        self.contract = DecisionContract(
            decision_id="DEC-1",
            request_id="REQ-1",
            prompt="Choose audit storage",
            question="Which safe architecture should be used?",
            options=OPTIONS,
            recommendation="Option A: Dedicated audit_events table",
            blocking_dependencies=["REQ-1"],
            authorized_responders=["Operator"],
            decision_scope=DecisionScope.ARCHITECTURAL_PREFERENCE,
            issue_number=77,
        )
        self.mgr.register_question(self.contract)
        self.initial_body = format_decision_markdown(self.contract)
        self.question = {
            "id": "100",
            "user": "Automation",
            "user_type": "User",
            "body": self.initial_body,
            "created_at": "2026-09-06T11:00:00Z",
            "updated_at": "2026-09-06T11:00:00Z",
            "html_url": "https://github.com/Wladefant/super-board/issues/77#issuecomment-100",
            "issue_url": "https://api.github.com/repos/Wladefant/super-board/issues/77",
            "performed_via_github_app": False,
        }
        self.comments["100"] = self.question
        self._set_question_state()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fetch_comment(self, repo, comment_id):
        return dict(self.comments[str(comment_id)])

    def _set_question_state(self):
        with open(self.decisions_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        decision = data["decisions"]["DEC-1"]
        decision.update(
            {
                "issue_number": 77,
                "question_comment_id": "100",
                "question_posted_at": self.question["created_at"],
                "question_body_snapshot": self.initial_body,
                "question_snapshot_updated_at": self.question["updated_at"],
                "question_author": "Automation",
            }
        )
        data.setdefault("authored_comment_ids", []).append("100")
        with open(self.decisions_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def _checked(self, option="A", context=None):
        body = self.initial_body.replace(
            f"- [ ] **Option {option}**", f"- [x] **Option {option}**"
        )
        if context is not None:
            body = body.replace(
                "_Leave any supplemental notes, constraints, or alternative proposals below:_",
                context,
            )
        return body

    def _edit_event(self, old_body, new_body, sender="Operator", sender_type="User"):
        self.comments["100"] = {
            **self.question,
            "body": new_body,
            "updated_at": "2026-09-06T12:00:00Z",
        }
        return {
            "action": "edited",
            "sender": {"login": sender, "type": sender_type},
            "issue": {"number": 77},
            "comment": {
                "id": 100,
                "user": {"login": "Automation", "type": "User"},
                "body": new_body,
                "updated_at": "2026-09-06T12:00:00Z",
                "html_url": self.question["html_url"],
            },
            "changes": {"body": {"from": old_body}},
        }

    def _created_comment(self, comment_id, body, user="Operator", user_type="User", app=False):
        comment = {
            "id": str(comment_id),
            "user": user,
            "user_type": user_type,
            "body": body,
            "created_at": "2026-09-06T12:00:00Z",
            "updated_at": "2026-09-06T12:00:00Z",
            "html_url": f"https://github.com/Wladefant/super-board/issues/77#issuecomment-{comment_id}",
            "issue_url": "https://api.github.com/repos/Wladefant/super-board/issues/77",
            "performed_via_github_app": app,
        }
        self.comments[str(comment_id)] = comment
        return comment
    def _sync(self):
        output = "".join(json.dumps(comment) + "\n" for comment in self.comments.values())
        completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
        with patch("decision_workflow.subprocess.run", return_value=completed):
            return self.mgr.sync_decisions(repo="Wladefant/super-board")


    def _assert_blocked(self):
        request = self.ledger.get_request("REQ-1")
        self.assertIn("DEC-1", request.get("decision_blockers", []))
        self.assertNotEqual(self.mgr.get_decision("DEC-1").get("status"), "answered")

    def test_renderer_and_exact_scoped_extraction(self):
        self.assertIn("<!-- decision-options: DEC-1 -->", self.initial_body)
        scoped, error = extract_scoped_task_list_options(self.initial_body, "DEC-1")
        self.assertIsNone(error)
        self.assertEqual(set(scoped), {"A", "B"})
        hostile = (
            "- [x] **Option A**: unrelated\n"
            "```\n- [x] **Option B**: code\n```\n"
            "<!--\n- [x] **Option B**: hidden\n-->"
        )
        combined = self.initial_body + "\n" + hostile
        combined_scoped, combined_error = extract_scoped_task_list_options(combined, "DEC-1")
        self.assertIsNone(combined_error)
        self.assertEqual(set(combined_scoped), {"A", "B"})

    def test_B1_existing_check_plus_unrelated_edit_does_not_approve(self):
        checked = self._checked("A")
        result = self.mgr.process_issue_edit(
            "DEC-1", checked, checked + "\nUnrelated prose changed.", "Operator",
            event_type="comment_edit", comment_id="100", edit_time="2026-09-06T12:00:00Z",
            provenance=ProvenanceType.GITHUB_VERIFIED_USER,
        )
        self.assertEqual(result["status"], "clarification_requested")
        self.assertIn("No valid unchecked-to-checked", result["interpretation"])
        self._assert_blocked()

    def test_B2_unrelated_checklists_and_nonrendered_markdown_do_not_select(self):
        body = self.initial_body + "\n- [x] A. Migration applied\n    - [x] **Option B**: nested\n```\n- [x] **Option B**: code\n```"
        result = self.mgr.process_issue_edit(
            "DEC-1", self.initial_body, body, "Operator", event_type="comment_edit",
            comment_id="100", edit_time="2026-09-06T12:00:00Z",
            provenance=ProvenanceType.GITHUB_VERIFIED_USER,
        )
        self.assertEqual(result["status"], "clarification_requested")
        self._assert_blocked()

        canonical_b = next(
            line for line in self.initial_body.splitlines()
            if line.startswith("- [ ] **Option B**:")
        )
        fenced_inside = self.initial_body.replace(
            "<!-- /decision-options -->",
            f"```markdown\n{canonical_b.replace('[ ]', '[x]')}\n```\n<!-- /decision-options -->",
        )
        hidden = self.mgr.process_issue_edit(
            "DEC-1", self.initial_body, fenced_inside, "Operator",
            event_type="comment_edit", comment_id="100",
            edit_time="2026-09-06T12:00:00Z",
            provenance=ProvenanceType.GITHUB_VERIFIED_USER,
        )
        self.assertEqual(hidden["status"], "rejected")
        self.assertIn("Non-canonical", hidden["rejection_reason"])
        self._assert_blocked()

    def test_B3_poller_never_invents_editor_and_persists_revision_across_restart(self):
        changed = self._checked("A", "Keep every free-form constraint, including article a.")
        question = {**self.question, "body": changed, "updated_at": "2026-09-06T12:00:00Z"}
        self.comments["100"] = question
        output = json.dumps(question) + "\n"
        completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
        with patch("decision_workflow.subprocess.run", return_value=completed):
            first = self.mgr.sync_decisions(repo="Wladefant/super-board")
        self.assertEqual(first["errors"], [])
        decision = self.mgr.get_decision("DEC-1")
        self.assertEqual(decision["question_body_snapshot"], changed)
        self.assertIn("free-form constraint", decision["last_alternative_proposal"])
        self._assert_blocked()
        audit_count = len(decision["audit_trail"])
        restarted = DecisionManager(self.decisions_path, self.ledger_path, self._fetch_comment)
        with patch("decision_workflow.subprocess.run", return_value=completed):
            replay = restarted.sync_decisions(repo="Wladefant/super-board")
        self.assertEqual(replay["errors"], [])
        self.assertEqual(len(restarted.get_decision("DEC-1")["audit_trail"]), audit_count)

    def test_B4_shared_account_and_bot_edits_fail_closed(self):
        with open(self.decisions_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        state["decisions"]["DEC-1"]["authorized_responders"] = ["Automation"]
        with open(self.decisions_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        shared_event = self._edit_event(self.initial_body, self._checked("A"), sender="Automation")
        result = self.mgr.ingest_github_event(
            shared_event, repo="Wladefant/super-board", trusted_transport=True
        )
        self.assertEqual(result["status"], "clarification_requested")
        self.assertEqual(result["provenance"], ProvenanceType.SHARED_ACCOUNT_AMBIGUOUS)
        self._assert_blocked()

        other_tmp = DecisionUXProof(methodName="runTest")
        other_tmp.setUp()
        try:
            bot_event = other_tmp._edit_event(other_tmp.initial_body, other_tmp._checked("A"), sender="agent[bot]", sender_type="Bot")
            rejected = other_tmp.mgr.ingest_github_event(
                bot_event, repo="Wladefant/super-board", trusted_transport=True
            )
            self.assertEqual(rejected["status"], "rejected")
            other_tmp._assert_blocked()
        finally:
            other_tmp.tearDown()

    def test_article_a_negation_and_arbitrary_text_are_retained_without_approval(self):
        prose = "Do not proceed; I need a migration plan and a DBA review before deciding."
        comment = self._created_comment("201", prose)
        result = self.mgr.ingest_comment("DEC-1", "201", repo="Wladefant/super-board")
        self.assertEqual(result["status"], "clarification_requested")
        self.assertEqual(self.mgr.get_decision("DEC-1")["last_alternative_proposal"], prose)
        self._assert_blocked()

    def test_tampered_option_text_stale_event_and_question_publication_fail(self):
        tampered = self._checked("A").replace("Dedicated audit_events table", "Delete audit history")
        bad = self.mgr.process_issue_edit(
            "DEC-1", self.initial_body, tampered, "Operator", event_type="comment_edit",
            comment_id="100", edit_time="2026-09-06T12:00:00Z",
            provenance=ProvenanceType.GITHUB_VERIFIED_USER,
        )
        self.assertEqual(bad["status"], "rejected")
        self.assertIn("option text changed", bad["rejection_reason"])

        stale = self.mgr.process_issue_edit(
            "DEC-1", self.initial_body, self._checked("A"), "Operator", event_type="comment_edit",
            comment_id="100", edit_time="2020-01-01T00:00:00Z",
            provenance=ProvenanceType.GITHUB_VERIFIED_USER,
        )
        self.assertEqual(stale["status"], "rejected")
        publication = self.mgr.ingest_github_event(
            {"action": "created", "sender": {"login": "Automation"}, "issue": {"number": 77}, "comment": {"id": 100, "body": self.initial_body}},
            repo="Wladefant/super-board",
        )
        self.assertEqual(publication["status"], "ignored")
        self._assert_blocked()

    def test_provenance_less_reply_cannot_approve(self):
        result = self.mgr.process_reply(
            "DEC-1",
            "Decision DEC-1: Option A",
            responder="Operator",
        )
        self.assertEqual(result["status"], "clarification_requested")
        self.assertEqual(result["provenance"], ProvenanceType.UNVERIFIED_CALLER)
        self.assertIsNone(self.mgr.get_decision("DEC-1")["answer"])
        self._assert_blocked()

    def test_ambiguous_and_missing_prior_revision_fail_closed(self):
        both = self._checked("A").replace(
            "- [ ] **Option B**", "- [x] **Option B**"
        )
        ambiguous = self.mgr.process_issue_edit(
            "DEC-1", self.initial_body, both, "Operator", event_type="comment_edit",
            comment_id="100", edit_time="2026-09-06T12:00:00Z",
            provenance=ProvenanceType.GITHUB_VERIFIED_USER,
        )
        self.assertEqual(ambiguous["status"], "clarification_requested")
        missing_old = self._edit_event(self.initial_body, self._checked("A"))
        del missing_old["changes"]["body"]["from"]
        rejected = self.mgr.ingest_github_event(missing_old, repo="Wladefant/super-board")
        self.assertEqual(rejected["status"], "rejected")
        untrusted_event = self._edit_event(self.initial_body, self._checked("A"))
        untrusted = self.mgr.ingest_github_event(
            untrusted_event, repo="Wladefant/super-board"
        )
        self.assertEqual(untrusted["status"], "clarification_requested")
        self.assertEqual(untrusted["provenance"], ProvenanceType.UNVERIFIED_CALLER)
        self._assert_blocked()

    def test_checkbox_replay_repairs_ledger_before_reporting_unblocked(self):
        body = self._checked("A")
        event = self._edit_event(self.initial_body, body)
        with patch.object(self.mgr.ledger, "resolve_decision", side_effect=KeyError("synthetic missing write")):
            first = self.mgr.ingest_github_event(
                event, repo="Wladefant/super-board", trusted_transport=True
            )
        self.assertEqual(first["status"], "answered")
        self.assertEqual(first["unblocked_requests"], [])
        self.assertIn("DEC-1", self.ledger.get_request("REQ-1")["decision_blockers"])
        replay = self.mgr.ingest_github_event(
            event, repo="Wladefant/super-board", trusted_transport=True
        )
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["unblocked_requests"], ["REQ-1"])
        self.assertNotIn("DEC-1", self.ledger.get_request("REQ-1")["decision_blockers"])

    def test_distinct_verified_click_reaches_decision_manager_ledger_and_replays(self):
        body = self._checked("B", "Preserve this unrestricted context: a, b, and not Option A.")
        outside_context = "IMPORTANT: wait for DBA sign-off and keep raw events for 90 days."
        body += "\n" + outside_context
        event = self._edit_event(self.initial_body, body)
        result = self.mgr.ingest_github_event(
            event, repo="Wladefant/super-board", trusted_transport=True
        )
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["unblocked_requests"], ["REQ-1"])
        answer = self.mgr.get_decision("DEC-1")["answer"]
        self.assertEqual(answer["selected_option_id"], "B")
        self.assertEqual(answer["responder"], "Operator")
        self.assertEqual(answer["provenance"], ProvenanceType.GITHUB_VERIFIED_USER)
        self.assertIn("unrestricted context", answer["additional_context"])
        self.assertIn(outside_context, answer["additional_context"])
        self.assertTrue(ContinuationDriver._decision_is_authorized_answer(self.mgr.get_decision("DEC-1")))
        request = self.ledger.get_request("REQ-1")
        self.assertNotIn("DEC-1", request.get("decision_blockers", []))
        durable = [item for item in request["evidence"] if item.get("type") == "github_decision"][-1]
        self.assertIn(outside_context, durable["details"])
        restarted = DecisionManager(self.decisions_path, self.ledger_path, self._fetch_comment)
        replay = restarted.ingest_github_event(
            event, repo="Wladefant/super-board", trusted_transport=True
        )
        self.assertTrue(replay["idempotent_replay"])
        conflicting_event = self._edit_event(body, self._checked("A"))
        conflicting = restarted.ingest_github_event(
            conflicting_event,
            repo="Wladefant/super-board",
            trusted_transport=True,
        )
        self.assertEqual(conflicting["status"], "rejected")
        self.assertIn("Conflicting edit", conflicting["rejection_reason"])
        self.assertEqual(restarted.get_decision("DEC-1")["answer"]["selected_option_id"], "B")

    def test_free_text_then_explicit_choice_reaches_consumer_with_prior_context(self):
        proposal = "Alternative proposal: keep all raw events and use partitioned storage."
        self._created_comment("301", proposal)
        first = self._sync()
        self.assertEqual(first["errors"], [])
        self.assertEqual(self.mgr.get_decision("DEC-1")["status"], "clarification_requested")
        self._created_comment("302", "Decision DEC-1: Option A: add a covering index")
        second = self._sync()
        self.assertEqual(second["resolved_decisions"], ["DEC-1"])
        answer = self.mgr.get_decision("DEC-1")["answer"]
        self.assertEqual(answer["additional_context"], "add a covering index")
        self.assertEqual(answer["prior_alternative_proposal"], proposal)
        evidence = self.ledger.get_request("REQ-1")["evidence"]
        durable = [item for item in evidence if item.get("type") == "github_decision"][-1]
        self.assertIn(proposal, durable["details"])

    def test_placeholder_and_bare_option_do_not_pollute_context(self):
        self.assertEqual(extract_additional_context(self.initial_body, "DEC-1"), "")
        self._created_comment("401", "Option B")
        result = self.mgr.ingest_comment("DEC-1", "401", repo="Wladefant/super-board")
        self.assertEqual(result["status"], "answered")
        self.assertIsNone(self.mgr.get_decision("DEC-1")["answer"]["additional_context"])


class GraphQLDecisionHistoryProof(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="graphql_decision_ux_")
        self.decisions_path = os.path.join(self.tmp, "decisions.json")
        self.ledger_path = os.path.join(self.tmp, "ledger.json")
        self.ledger = RequestLedger(self.ledger_path)
        self.ledger.add_request(
            req_id="REQ-1",
            prompt="Choose audit storage",
            session="synthetic-proof",
            project="Wladefant/super-board",
            owner="GraphQLDecisionHistoryProof",
            acceptance_criteria=["A durable decision reaches the existing consumer"],
        )
        self.contract = DecisionContract(
            decision_id="DEC-1",
            request_id="REQ-1",
            prompt="Choose audit storage",
            question="Which safe architecture should be used?",
            options=OPTIONS,
            recommendation="Option A: Dedicated audit_events table",
            blocking_dependencies=["REQ-1"],
            authorized_responders=["Operator"],
            decision_scope=DecisionScope.ARCHITECTURAL_PREFERENCE,
            issue_number=77,
        )
        self.initial_body = format_decision_markdown(self.contract)
        self.comments = {
            "100": {
                "id": "100",
                "node_id": "IC_100",
                "user": "Automation",
                "user_type": "User",
                "body": self.initial_body,
                "created_at": "2026-09-06T11:00:00Z",
                "updated_at": "2026-09-06T11:00:00Z",
                "html_url": "https://github.com/Wladefant/super-board/issues/77#issuecomment-100",
                "issue_url": "https://api.github.com/repos/Wladefant/super-board/issues/77",
                "performed_via_github_app": False,
            }
        }
        self.mgr = DecisionManager(
            decisions_path=self.decisions_path,
            ledger_path=self.ledger_path,
            comment_fetcher=lambda repo, cid: dict(self.comments[str(cid)]),
        )
        self.mgr.register_question(self.contract)
        with open(self.decisions_path, "r", encoding="utf-8") as f:
            dstate = json.load(f)
        dstate["decisions"]["DEC-1"].update({
            "issue_number": 77,
            "question_comment_id": "100",
            "question_posted_at": "2026-09-06T11:00:00Z",
            "question_body_snapshot": self.initial_body,
            "question_snapshot_updated_at": "2026-09-06T11:00:00Z",
            "question_author": "Automation",
        })
        with open(self.decisions_path, "w", encoding="utf-8") as f:
            json.dump(dstate, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _checked(self, option="A", context=None):
        body = self.initial_body.replace(
            f"- [ ] **Option {option}**", f"- [x] **Option {option}**"
        )
        if context is not None:
            body = body.replace(
                "_Leave any supplemental notes, constraints, or alternative proposals below:_",
                context,
            )
        return body

    def test_graphql_distinct_verified_user_approves(self):
        checked = self._checked("A", "Authorized choice by Operator")
        payload = {
            "data": {
                "node": {
                    "id": "IC_100",
                    "body": checked,
                    "lastEditedAt": "2026-09-06T12:00:00Z",
                    "editor": {"login": "Operator", "__typename": "User"},
                    "userContentEdits": {
                        "pageInfo": {"hasNextPage": False},
                        "totalCount": 2,
                        "nodes": [
                            {
                                "id": "UCE_2",
                                "editedAt": "2026-09-06T12:00:00Z",
                                "editor": {"login": "Operator", "__typename": "User"},
                                "diff": checked,
                            },
                            {
                                "id": "UCE_1",
                                "editedAt": "2026-09-06T11:00:00Z",
                                "editor": {"login": "Automation", "__typename": "User"},
                                "diff": self.initial_body,
                            },
                        ],
                    },
                }
            }
        }
        res = self.mgr.ingest_comment_edit_history("DEC-1", "100", history_data=payload)
        self.assertEqual(res["status"], "answered")
        self.assertEqual(res["provenance"], ProvenanceType.GITHUB_VERIFIED_USER)
        self.assertEqual(res["unblocked_requests"], ["REQ-1"])
        self.assertIn("Authorized choice by Operator", res["interpretation"])

        # Verify existing ContinuationDriver consumer accepts the record
        dec = self.mgr.get_decision("DEC-1")
        self.assertTrue(ContinuationDriver._decision_is_authorized_answer(dec))

        # Verify ledger blocker is cleared and evidence recorded
        req = self.ledger.get_request("REQ-1")
        self.assertEqual(req.get("decision_blockers", []), [])
        ev = [item for item in req["evidence"] if item.get("type") == "github_decision"][-1]
        self.assertIn("Provenance: github_verified_user", ev["details"])

    def test_graphql_shared_account_fails_closed(self):
        with open(self.decisions_path, "r", encoding="utf-8") as f:
            dstate = json.load(f)
        dstate["decisions"]["DEC-1"]["question_author"] = "Operator"
        with open(self.decisions_path, "w", encoding="utf-8") as f:
            json.dump(dstate, f)
        self.comments["100"]["user"] = "Operator"

        checked = self._checked("A")
        payload = {
            "data": {
                "node": {
                    "id": "IC_100",
                    "body": checked,
                    "lastEditedAt": "2026-09-06T12:00:00Z",
                    "editor": {"login": "Operator", "__typename": "User"},
                    "userContentEdits": {
                        "pageInfo": {"hasNextPage": False},
                        "totalCount": 2,
                        "nodes": [
                            {"id": "UCE_2", "editedAt": "2026-09-06T12:00:00Z", "editor": {"login": "Operator", "__typename": "User"}, "diff": checked},
                            {"id": "UCE_1", "editedAt": "2026-09-06T11:00:00Z", "editor": {"login": "Operator", "__typename": "User"}, "diff": self.initial_body},
                        ],
                    },
                }
            }
        }
        res = self.mgr.ingest_comment_edit_history("DEC-1", "100", history_data=payload)
        self.assertEqual(res["status"], "clarification_requested")
        self.assertEqual(res["provenance"], ProvenanceType.SHARED_ACCOUNT_AMBIGUOUS)

        dec = self.mgr.get_decision("DEC-1")
        self.assertFalse(ContinuationDriver._decision_is_authorized_answer(dec))
        req = self.ledger.get_request("REQ-1")
        self.assertIn("DEC-1", req.get("decision_blockers", []))

    def test_graphql_race_condition_fails_closed(self):
        checked = self._checked("A")
        payload = {
            "data": {
                "node": {
                    "id": "IC_100",
                    "body": checked + "\nConcurrent in-flight change",
                    "lastEditedAt": "2026-09-06T12:00:00Z",
                    "editor": {"login": "Operator", "__typename": "User"},
                    "userContentEdits": {
                        "pageInfo": {"hasNextPage": False},
                        "totalCount": 2,
                        "nodes": [
                            {"id": "UCE_2", "editedAt": "2026-09-06T12:00:00Z", "editor": {"login": "Operator", "__typename": "User"}, "diff": checked},
                            {"id": "UCE_1", "editedAt": "2026-09-06T11:00:00Z", "editor": {"login": "Automation", "__typename": "User"}, "diff": self.initial_body},
                        ],
                    },
                }
            }
        }
        res = self.mgr.ingest_comment_edit_history("DEC-1", "100", history_data=payload)
        self.assertEqual(res["status"], "rejected")
        self.assertIn("Race condition", res["reason"])
        req = self.ledger.get_request("REQ-1")
        self.assertIn("DEC-1", req.get("decision_blockers", []))

    def test_graphql_gaps_and_deleted_edits_fail_closed(self):
        checked = self._checked("A")
        payload = {
            "data": {
                "node": {
                    "id": "IC_100",
                    "body": checked,
                    "lastEditedAt": "2026-09-06T12:00:00Z",
                    "editor": {"login": "Operator", "__typename": "User"},
                    "userContentEdits": {
                        "pageInfo": {"hasNextPage": False},
                        "totalCount": 1,
                        "nodes": [
                            {"id": "UCE_2", "editedAt": "2026-09-06T12:00:00Z", "editor": {"login": "Operator", "__typename": "User"}, "diff": checked},
                        ],
                    },
                }
            }
        }
        res = self.mgr.ingest_comment_edit_history("DEC-1", "100", history_data=payload)
        self.assertEqual(res["status"], "rejected")
        self.assertIn("fewer than two revisions", res["reason"])

    def test_graphql_unpaginated_history_fails_closed(self):
        checked = self._checked("A")
        payload = {
            "data": {
                "node": {
                    "id": "IC_100",
                    "body": checked,
                    "lastEditedAt": "2026-09-06T12:00:00Z",
                    "editor": {"login": "Operator", "__typename": "User"},
                    "userContentEdits": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "cursor_999"},
                        "totalCount": 10,
                        "nodes": [
                            {"id": "UCE_2", "editedAt": "2026-09-06T12:00:00Z", "editor": {"login": "Operator", "__typename": "User"}, "diff": checked},
                            {"id": "UCE_1", "editedAt": "2026-09-06T11:00:00Z", "editor": {"login": "Automation", "__typename": "User"}, "diff": self.initial_body},
                        ],
                    },
                }
            }
        }
        res = self.mgr.ingest_comment_edit_history("DEC-1", "100", history_data=payload)
        self.assertEqual(res["status"], "rejected")
        self.assertIn("unpaginated pages", res["reason"])

    def test_graphql_non_monotonic_timestamps_fail_closed(self):
        checked = self._checked("A")
        payload = {
            "data": {
                "node": {
                    "id": "IC_100",
                    "body": checked,
                    "lastEditedAt": "2026-09-06T10:00:00Z",
                    "editor": {"login": "Operator", "__typename": "User"},
                    "userContentEdits": {
                        "pageInfo": {"hasNextPage": False},
                        "totalCount": 2,
                        "nodes": [
                            {"id": "UCE_2", "editedAt": "2026-09-06T10:00:00Z", "editor": {"login": "Operator", "__typename": "User"}, "diff": checked},
                            {"id": "UCE_1", "editedAt": "2026-09-06T11:00:00Z", "editor": {"login": "Automation", "__typename": "User"}, "diff": self.initial_body},
                        ],
                    },
                }
            }
        }
        res = self.mgr.ingest_comment_edit_history("DEC-1", "100", history_data=payload)
        self.assertEqual(res["status"], "rejected")
        self.assertIn("Non-monotonic", res["reason"])

    def test_graphql_bot_editor_fails_closed(self):
        checked = self._checked("A")
        payload = {
            "data": {
                "node": {
                    "id": "IC_100",
                    "body": checked,
                    "lastEditedAt": "2026-09-06T12:00:00Z",
                    "editor": {"login": "agent[bot]", "__typename": "Bot"},
                    "userContentEdits": {
                        "pageInfo": {"hasNextPage": False},
                        "totalCount": 2,
                        "nodes": [
                            {"id": "UCE_2", "editedAt": "2026-09-06T12:00:00Z", "editor": {"login": "agent[bot]", "__typename": "Bot"}, "diff": checked},
                            {"id": "UCE_1", "editedAt": "2026-09-06T11:00:00Z", "editor": {"login": "Automation", "__typename": "User"}, "diff": self.initial_body},
                        ],
                    },
                }
            }
        }
        res = self.mgr.ingest_comment_edit_history("DEC-1", "100", history_data=payload)
        self.assertEqual(res["status"], "rejected")

    def test_graphql_missing_editor_metadata_fails_closed(self):
        checked = self._checked("A")
        payload = {
            "data": {
                "node": {
                    "id": "IC_100",
                    "body": checked,
                    "lastEditedAt": "2026-09-06T12:00:00Z",
                    "editor": {"login": "Operator", "__typename": "User"},
                    "userContentEdits": {
                        "pageInfo": {"hasNextPage": False},
                        "totalCount": 2,
                        "nodes": [
                            {"id": "UCE_2", "editedAt": "2026-09-06T12:00:00Z", "editor": None, "diff": checked},
                            {"id": "UCE_1", "editedAt": "2026-09-06T11:00:00Z", "editor": {"login": "Automation", "__typename": "User"}, "diff": self.initial_body},
                        ],
                    },
                }
            }
        }
        res = self.mgr.ingest_comment_edit_history("DEC-1", "100", history_data=payload)
        self.assertEqual(res["status"], "rejected")
        self.assertIn("missing required editor metadata", res["reason"])

    def test_sync_decisions_with_graphql_edit_history(self):
        checked = self._checked("A")
        payload = {
            "data": {
                "node": {
                    "id": "IC_100",
                    "body": checked,
                    "lastEditedAt": "2026-09-06T12:00:00Z",
                    "editor": {"login": "Operator", "__typename": "User"},
                    "userContentEdits": {
                        "pageInfo": {"hasNextPage": False},
                        "totalCount": 2,
                        "nodes": [
                            {"id": "UCE_2", "editedAt": "2026-09-06T12:00:00Z", "editor": {"login": "Operator", "__typename": "User"}, "diff": checked},
                            {"id": "UCE_1", "editedAt": "2026-09-06T11:00:00Z", "editor": {"login": "Automation", "__typename": "User"}, "diff": self.initial_body},
                        ],
                    },
                }
            }
        }
        self.mgr.comment_history_fetcher = lambda repo, cid, nid=None: payload

        self.comments["100"] = {
            **self.comments["100"],
            "body": checked,
            "updated_at": "2026-09-06T12:00:00Z",
        }
        output = "".join(json.dumps(comment) + "\n" for comment in self.comments.values())
        completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
        with patch("decision_workflow.subprocess.run", return_value=completed):
            sync_res = self.mgr.sync_decisions(repo="Wladefant/super-board")

        self.assertEqual(sync_res["resolved_decisions"], ["DEC-1"])
        self.assertEqual(sync_res["unblocked_requests"], ["REQ-1"])
        self.assertEqual(self.mgr.get_decision("DEC-1")["status"], "answered")
        self.assertEqual(self.ledger.get_request("REQ-1").get("decision_blockers", []), [])

    def test_sync_decisions_fallback_to_unverified_rest_fails_closed(self):
        checked = self._checked("A")
        def fail_history(*args, **kwargs):
            raise RuntimeError("GraphQL unavailable")
        self.mgr.comment_history_fetcher = fail_history

        self.comments["100"] = {
            **self.comments["100"],
            "body": checked,
            "updated_at": "2026-09-06T12:00:00Z",
        }
        output = "".join(json.dumps(comment) + "\n" for comment in self.comments.values())
        completed = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
        with patch("decision_workflow.subprocess.run", return_value=completed):
            sync_res = self.mgr.sync_decisions(repo="Wladefant/super-board")

        self.assertEqual(sync_res["resolved_decisions"], [])
        self.assertEqual(self.mgr.get_decision("DEC-1")["status"], "clarification_requested")
        self.assertIn("DEC-1", self.ledger.get_request("REQ-1").get("decision_blockers", []))

    def test_cli_ingest_history_command(self):
        checked = self._checked("A")
        payload = {
            "data": {
                "node": {
                    "id": "IC_100",
                    "body": checked,
                    "lastEditedAt": "2026-09-06T12:00:00Z",
                    "editor": {"login": "Operator", "__typename": "User"},
                    "userContentEdits": {
                        "pageInfo": {"hasNextPage": False},
                        "totalCount": 2,
                        "nodes": [
                            {"id": "UCE_2", "editedAt": "2026-09-06T12:00:00Z", "editor": {"login": "Operator", "__typename": "User"}, "diff": checked},
                            {"id": "UCE_1", "editedAt": "2026-09-06T11:00:00Z", "editor": {"login": "Automation", "__typename": "User"}, "diff": self.initial_body},
                        ],
                    },
                }
            }
        }
        hist_path = os.path.join(self.tmp, "history.json")
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        from decision_workflow import main
        test_args = [
            "decision_workflow.py",
            "--decisions", self.decisions_path,
            "--ledger", self.ledger_path,
            "ingest-history",
            "DEC-1",
            "--comment-id", "100",
            "--history-path", hist_path,
        ]
        with patch("sys.argv", test_args):
            with patch("sys.stdout"):
                main()

        dec = self.mgr.get_decision("DEC-1")
        self.assertEqual(dec["status"], "answered")
        self.assertEqual(dec["answer"]["selected_option_id"], "A")

class TestTelegramCallbackResolutionAndSessionProvenance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="telegram_callback_test_")
        self.decisions_path = os.path.join(self.tmp, "decisions.json")
        self.ledger_path = os.path.join(self.tmp, "ledger.json")
        self.ledger = RequestLedger(self.ledger_path)
        self.ledger.add_request(
            req_id="req-test-cb",
            prompt="Test request for callback resolution.",
            session="session-alpha-100",
            project="super-board",
            acceptance_criteria=["Callback resolves properly"],
            owner="TestWorker",
            task_type="harness",
            state="implementation",
        )
        self.mgr = DecisionManager(decisions_path=self.decisions_path, ledger_path=self.ledger_path)
        self.decision = DecisionContract(
            decision_id="DEC-CB-1",
            request_id="req-test-cb",
            prompt="Architectural choice prompt",
            question="Choose architecture A or B",
            options=OPTIONS,
            recommendation="Option A",
            blocking_dependencies=["req-test-cb"],
            authorized_responders=["Operator"],
            decision_scope=DecisionScope.ARCHITECTURAL_PREFERENCE,
            status="pending",
        )
        self.mgr.register_question(self.decision)
        # Bind session to the decision record
        data = self.mgr._load_data_unlocked()
        data["decisions"]["DEC-CB-1"]["session"] = "session-alpha-100"
        data["decisions"]["DEC-CB-1"]["session_id"] = "session-alpha-100"
        self.mgr._save_data_unlocked(data)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_valid_session_bound_callback_resolves_and_records_context(self):
        res = self.mgr.resolve_telegram_callback(
            decision_id="DEC-CB-1",
            choice_id="A",
            callback_token="tok_abc_123",
            responder="Operator",
            session_id="session-alpha-100",
            context="Approved with audit retention requirement",
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["status"], "answered")
        self.assertEqual(res["choice_id"], "A")
        self.assertEqual(res["session_id"], "session-alpha-100")
        self.assertEqual(res["context"], "Approved with audit retention requirement")
        self.assertEqual(res["provenance"], ProvenanceType.TELEGRAM_VERIFIED_CALLBACK)
        self.assertIn("req-test-cb", res["unblocked_requests"])

        # Verify decision store
        dec = self.mgr.get_decision("DEC-CB-1")
        self.assertEqual(dec["status"], "answered")
        ans = dec["answer"]
        self.assertEqual(ans["selected_option_id"], "A")
        self.assertEqual(ans["session_id"], "session-alpha-100")
        self.assertEqual(ans["context"], "Approved with audit retention requirement")
        self.assertEqual(ans["provenance"], ProvenanceType.TELEGRAM_VERIFIED_CALLBACK)

        # Verify ledger request unblocked and evidence recorded
        req = self.ledger.get_request("req-test-cb")
        self.assertNotIn("DEC-CB-1", req.get("decision_blockers", []))

    def test_callback_mismatched_session_is_refused(self):
        res = self.mgr.resolve_telegram_callback(
            decision_id="DEC-CB-1",
            choice_id="A",
            callback_token="tok_mismatch_456",
            responder="Operator",
            session_id="session-wrong-999",
            context="Mismatched session attempt",
        )
        self.assertFalse(res["ok"])
        self.assertIn("Session mismatch", res["error"])
        dec = self.mgr.get_decision("DEC-CB-1")
        self.assertEqual(dec["status"], "pending")
        self.assertIsNone(dec.get("answer"))
        req = self.ledger.get_request("req-test-cb")
        self.assertIn("DEC-CB-1", req.get("decision_blockers", []))

    def test_callback_missing_session_when_decision_session_bound_is_refused(self):
        res = self.mgr.resolve_telegram_callback(
            decision_id="DEC-CB-1",
            choice_id="A",
            callback_token="tok_missing_session",
            responder="Operator",
            session_id=None,
        )
        self.assertFalse(res["ok"])
        self.assertIn("Session mismatch", res["error"])
        dec = self.mgr.get_decision("DEC-CB-1")
        self.assertEqual(dec["status"], "pending")
        self.assertIsNone(dec.get("answer"))

    def test_callback_on_already_answered_decision_is_refused(self):
        # First resolve
        self.mgr.resolve_telegram_callback(
            decision_id="DEC-CB-1",
            choice_id="A",
            callback_token="tok_first",
            responder="Operator",
            session_id="session-alpha-100",
        )
        # Second attempt on terminal/answered decision
        res2 = self.mgr.resolve_telegram_callback(
            decision_id="DEC-CB-1",
            choice_id="B",
            callback_token="tok_second",
            responder="Operator",
            session_id="session-alpha-100",
        )
        self.assertFalse(res2["ok"])
        self.assertEqual(res2["error"], "decision_already_answered")
        dec = self.mgr.get_decision("DEC-CB-1")
        self.assertEqual(dec["answer"]["selected_option_id"], "A")

    def test_callback_invalid_choice_is_refused(self):
        res = self.mgr.resolve_telegram_callback(
            decision_id="DEC-CB-1",
            choice_id="Z",
            callback_token="tok_bad_choice",
            responder="Operator",
            session_id="session-alpha-100",
        )
        self.assertFalse(res["ok"])
        self.assertIn("Invalid choice", res["error"])
        dec = self.mgr.get_decision("DEC-CB-1")
        self.assertEqual(dec["status"], "pending")
        self.assertIsNone(dec.get("answer"))

    def test_callback_unauthorized_responder_is_refused(self):
        res = self.mgr.resolve_telegram_callback(
            decision_id="DEC-CB-1",
            choice_id="A",
            callback_token="tok_unauth",
            responder="Intruder",
            session_id="session-alpha-100",
        )
        self.assertFalse(res["ok"])
        self.assertIn("Unauthorized responder", res["error"])
        dec = self.mgr.get_decision("DEC-CB-1")
        self.assertEqual(dec["status"], "pending")
        self.assertIsNone(dec.get("answer"))

    def test_no_authorization_by_silence_empty_reply_rejected(self):
        res = self.mgr.process_reply(
            decision_id="DEC-CB-1",
            reply_text="   \n\t  ",
            responder="Operator",
            provenance=ProvenanceType.GITHUB_VERIFIED_USER,
        )
        self.assertEqual(res["status"], "rejected")
        self.assertIn("Empty reply received", res["rejection_reason"])
        dec = self.mgr.get_decision("DEC-CB-1")
        self.assertEqual(dec["status"], "pending")
        self.assertIsNone(dec.get("answer"))
        req = self.ledger.get_request("req-test-cb")
        self.assertIn("DEC-CB-1", req.get("decision_blockers", []))

    def test_process_reply_session_mismatch_refused(self):
        res = self.mgr.process_reply(
            decision_id="DEC-CB-1",
            reply_text="Option A",
            responder="Operator",
            provenance=ProvenanceType.GITHUB_VERIFIED_USER,
            comment_created_at="2026-09-08T12:00:00+00:00",
            comment_time_provenance="api_verified",
            session_id="session-wrong-999",
        )
        self.assertEqual(res["status"], "rejected")
        self.assertIn("Session mismatch", res["rejection_reason"])
        dec = self.mgr.get_decision("DEC-CB-1")
        self.assertEqual(dec["status"], "pending")
        self.assertIsNone(dec.get("answer"))

    def test_unselected_free_text_does_not_unblock_or_authorize(self):
        res = self.mgr.process_reply(
            decision_id="DEC-CB-1",
            reply_text="I think we should consider a third option C using Redis streams instead.",
            responder="Operator",
            provenance=ProvenanceType.GITHUB_VERIFIED_USER,
            comment_created_at="2026-09-08T12:00:00+00:00",
            comment_time_provenance="api_verified",
        )
        self.assertEqual(res["status"], "clarification_requested")
        self.assertEqual(res["unblocked_requests"], [])
        self.assertIn("Alternative proposal", res["interpretation"])
        dec = self.mgr.get_decision("DEC-CB-1")
        self.assertEqual(dec["status"], "clarification_requested")
        self.assertIsNone(dec.get("answer"))
        req = self.ledger.get_request("req-test-cb")
        self.assertIn("DEC-CB-1", req.get("decision_blockers", []))

if __name__ == "__main__":
    unittest.main(verbosity=2)
