#!/usr/bin/env python3
"""
test_decision_ux_adapter.py — Behavioral test suite for clickable task-list decision UX,
free-text alternative answers, context retention, and authenticated GitHub event ingestion.

Acceptance Criteria Exercised:
1. Clickable task-list rendering with interactive markdown checkboxes.
2. Single-choice transition validation (newly checked box advances pending decision to answered).
3. Free-text context and supplemental notes path (both checkbox + notes flow into decision handling).
4. Alternative proposals / custom answers retained for interpretation without being discarded or forced into checkboxes.
5. Negative controls:
   - Ambiguous multiple choices (- [x] A and - [x] B) cannot silently approve.
   - Unauthorized actor edits/comments rejected, tasks remain blocked.
   - Autonomous agent/bot edits rejected from human decision authority.
   - Safety guardrail violations (production promotion, destructive DDL, prod refs) rejected.
   - Conflicting edits on terminal answered decisions refused, preserving original answer.
   - Idempotent replays re-synchronize without corrupting state or duplicating records.
6. Authenticated GitHub event ingestion for issues.edited, issue_comment.created, and issue_comment.edited.
7. CLI interface for ingest-event (--event-path, --event-json).
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import decision_workflow
from decision_workflow import (
    CommentTimeProvenance,
    DecisionContract,
    DecisionManager,
    DecisionScope,
    DecisionStatus,
    ProvenanceType,
    extract_additional_context,
    extract_context_from_reply,
    extract_task_list_options,
    format_decision_markdown,
    ingest_github_event,
)
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


class BaseDecisionUXTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="decision_ux_test_")
        self.decisions_path = os.path.join(self.tmp_dir, "decisions.json")
        self.ledger_path = os.path.join(self.tmp_dir, "ledger.json")

        self.ledger = RequestLedger(self.ledger_path)
        self.ledger.add_request(
            req_id="req-test-ux-001",
            prompt="Implement event audit logging architecture",
            session="sess-test-ux",
            project="Bavariance/polysimulator",
            owner="ImplementClickableDecisions",
            acceptance_criteria=["Durable audit trail established"],
        )

        self.mgr = DecisionManager(
            decisions_path=self.decisions_path,
            ledger_path=self.ledger_path,
        )

        self.contract = DecisionContract(
            decision_id="DEC-TEST-UX-01",
            request_id="req-test-ux-001",
            prompt="Implement event audit logging architecture",
            question="Which database storage format should we use for audit events?",
            options=OPTIONS,
            recommendation="Option A: Dedicated audit_events table provides cleanest retention.",
            blocking_dependencies=["req-test-ux-001"],
            authorized_responders=["Wladefant"],
            decision_scope=DecisionScope.ARCHITECTURAL_PREFERENCE,
            issue_number=4543,
        )
        self.mgr.register_question(self.contract)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)


class TestDecisionMarkdownRendering(BaseDecisionUXTest):
    def test_markdown_renders_interactive_task_list_checkboxes(self):
        md = format_decision_markdown(self.contract)
        self.assertIn("#### Choose an Option (Click checkbox to select)", md)
        self.assertIn("<!-- decision-options: DEC-TEST-UX-01 -->", md)
        self.assertIn("- [ ] **Option A**: Dedicated audit_events table", md)
        self.assertIn("- [ ] **Option B**: Inline JSON audit column", md)
        self.assertIn("<!-- /decision-options -->", md)

    def test_markdown_renders_context_and_alternative_section(self):
        md = format_decision_markdown(self.contract)
        self.assertIn("#### Additional Context / Alternative Proposal (Optional)", md)
        self.assertIn("<!-- decision-context: DEC-TEST-UX-01 -->", md)
        self.assertIn("<!-- /decision-context -->", md)

    def test_markdown_pre_checks_answered_option(self):
        # Answer decision with Option A
        self.mgr.process_reply(
            decision_id="DEC-TEST-UX-01",
            reply_text="Option A",
            responder="Wladefant",
            provenance=ProvenanceType.HUMAN_OPERATOR,
        )
        dec = self.mgr.get_decision("DEC-TEST-UX-01")
        contract = DecisionContract(**{k: v for k, v in dec.items() if k in DecisionContract.__annotations__})
        md = format_decision_markdown(contract)
        self.assertIn("- [x] **Option A**: Dedicated audit_events table", md)
        self.assertIn("- [ ] **Option B**: Inline JSON audit column", md)


class TestTaskListExtraction(unittest.TestCase):
    def test_extract_various_task_list_formats(self):
        text = (
            "Some preamble\n"
            "- [ ] **Option A**: Dedicated table\n"
            "- [x] **Option B**: Inline JSON\n"
            "- [X] Choice C - Third option\n"
            "* [x] Option 1: First item\n"
            "- [ ] [2] Second item\n"
        )
        opts = extract_task_list_options(text)
        self.assertIn("A", opts)
        self.assertFalse(opts["A"]["checked"])
        self.assertIn("B", opts)
        self.assertTrue(opts["B"]["checked"])
        self.assertIn("C", opts)
        self.assertTrue(opts["C"]["checked"])
        self.assertIn("1", opts)
        self.assertTrue(opts["1"]["checked"])
        self.assertIn("2", opts)
        self.assertFalse(opts["2"]["checked"])

    def test_extract_additional_context_from_block(self):
        text = (
            "<!-- decision-context: DEC-001 -->\n"
            "Please ensure table is partitioned by month.\n"
            "<!-- /decision-context -->"
        )
        ctx = extract_additional_context(text)
        self.assertEqual(ctx, "Please ensure table is partitioned by month.")

    def test_extract_context_from_reply_comments(self):
        ctx1 = extract_context_from_reply("Option A - ensure we backfill timestamps", "A", "Dedicated audit_events table")
        self.assertEqual(ctx1, "ensure we backfill timestamps")

        ctx2 = extract_context_from_reply("Option A\n\nNotes: Backfill required", "A", "Dedicated audit_events table")
        self.assertEqual(ctx2, "Backfill required")

        ctx3 = extract_context_from_reply("- [x] Option A\n\nPlease add retention policy", "A", "Dedicated audit_events table")
        self.assertEqual(ctx3, "Please add retention policy")


class TestSingleChoiceTaskListTransition(BaseDecisionUXTest):
    def test_clicking_checkbox_advances_decision_and_unblocks_ledger(self):
        old_body = format_decision_markdown(self.contract)
        new_body = old_body.replace("- [ ] **Option A**", "- [x] **Option A**")

        res = self.mgr.process_issue_edit(
            decision_id="DEC-TEST-UX-01",
            old_body=old_body,
            new_body=new_body,
            editor="Wladefant",
            event_type="comment_edit",
            comment_id="5559001",
            comment_url="https://github.com/Bavariance/polysimulator/issues/4543#issuecomment-5559001",
            edit_time="2026-09-06T10:15:00Z",
            provenance=ProvenanceType.HUMAN_OPERATOR,
        )

        self.assertEqual(res["status"], "answered")
        self.assertEqual(res["decision_id"], "DEC-TEST-UX-01")
        self.assertIn("req-test-ux-001", res["unblocked_requests"])

        # Verify decision store
        dec = self.mgr.get_decision("DEC-TEST-UX-01")
        self.assertEqual(dec["status"], "answered")
        self.assertEqual(dec["answer"]["selected_option_id"], "A")
        self.assertEqual(dec["answer"]["selection_method"], "task_list_checkbox")

        # Verify ledger
        req = self.ledger.get_request("req-test-ux-001")
        self.assertFalse(req["decision_blockers"])
        self.assertIsNone(req["blocker"])
        self.assertTrue(any(e.get("type") == "human_decision" for e in req.get("evidence", [])))


class TestMultipleChoiceRejection(BaseDecisionUXTest):
    def test_checking_multiple_options_fails_closed_without_silent_approval(self):
        old_body = format_decision_markdown(self.contract)
        # Check both A and B
        new_body = old_body.replace("- [ ] **Option A**", "- [x] **Option A**").replace("- [ ] **Option B**", "- [x] **Option B**")

        res = self.mgr.process_issue_edit(
            decision_id="DEC-TEST-UX-01",
            old_body=old_body,
            new_body=new_body,
            editor="Wladefant",
            event_type="comment_edit",
            comment_id="5559002",
        )

        self.assertEqual(res["status"], "clarification_requested")
        self.assertEqual(res["unblocked_requests"], [])
        self.assertIn("Multiple options", res["interpretation"])

        # Decision stays pending / clarification_requested, ledger stays blocked
        dec = self.mgr.get_decision("DEC-TEST-UX-01")
        self.assertEqual(dec["status"], "clarification_requested")
        self.assertIsNone(dec["answer"])

        req = self.ledger.get_request("req-test-ux-001")
        self.assertIn("DEC-TEST-UX-01", req["decision_blockers"])
        self.assertIn("BLOCKED", req["blocker"])


class TestFreeTextAndContextRetention(BaseDecisionUXTest):
    def test_checkbox_selection_with_additional_context_retains_both(self):
        old_body = format_decision_markdown(self.contract)
        new_body = old_body.replace("- [ ] **Option A**", "- [x] **Option A**")
        new_body = new_body.replace(
            "_Leave any supplemental notes, constraints, or alternative proposals below:_",
            "Ensure compound index on (tenant_id, created_at) is created.",
        )

        res = self.mgr.process_issue_edit(
            decision_id="DEC-TEST-UX-01",
            old_body=old_body,
            new_body=new_body,
            editor="Wladefant",
            event_type="comment_edit",
            comment_id="5559003",
        )

        self.assertEqual(res["status"], "answered")
        dec = self.mgr.get_decision("DEC-TEST-UX-01")
        self.assertEqual(dec["answer"]["selected_option_id"], "A")
        self.assertEqual(dec["answer"]["additional_context"], "Ensure compound index on (tenant_id, created_at) is created.")
        self.assertIn("Ensure compound index on (tenant_id, created_at) is created.", dec["answer"]["interpretation"])

        # Ledger evidence includes context
        req = self.ledger.get_request("req-test-ux-001")
        ev = [e for e in req.get("evidence", []) if e.get("type") == "human_decision"][0]
        self.assertIn("Ensure compound index", ev["details"])

    def test_comment_reply_with_context_retains_both(self):
        res = self.mgr.process_reply(
            decision_id="DEC-TEST-UX-01",
            reply_text="Option B - make sure we configure JSONB in PostgreSQL",
            responder="Wladefant",
            provenance=ProvenanceType.HUMAN_OPERATOR,
        )

        self.assertEqual(res["status"], "answered")
        dec = self.mgr.get_decision("DEC-TEST-UX-01")
        self.assertEqual(dec["answer"]["selected_option_id"], "B")
        self.assertEqual(dec["answer"]["additional_context"], "make sure we configure JSONB in PostgreSQL")
        self.assertIn("make sure we configure JSONB in PostgreSQL", dec["answer"]["interpretation"])


class TestAlternativeProposalRetention(BaseDecisionUXTest):
    def test_alternative_answer_is_retained_for_interpretation_not_discarded(self):
        reply = "I propose Option C: Use SQLite with WAL mode and in-memory caching instead."
        res = self.mgr.process_reply(
            decision_id="DEC-TEST-UX-01",
            reply_text=reply,
            responder="Wladefant",
            provenance=ProvenanceType.HUMAN_OPERATOR,
        )

        # Retained for interpretation; does NOT silently approve or unblock
        self.assertEqual(res["status"], "clarification_requested")
        self.assertEqual(res["unblocked_requests"], [])
        self.assertIn("Alternative proposal / custom response", res["interpretation"])

        dec = self.mgr.get_decision("DEC-TEST-UX-01")
        self.assertEqual(dec["alternative_proposal"], reply)
        self.assertEqual(dec["alternative_responder"], "Wladefant")
        self.assertIsNone(dec["answer"])

        # Ledger records the proposal and stays blocked
        req = self.ledger.get_request("req-test-ux-001")
        self.assertIn("DEC-TEST-UX-01", req["decision_blockers"])
        self.assertIn("Alternative proposal received", req["blocker"])
        self.assertIn("SQLite with WAL mode", req["blocker"])


class TestNegativeControlsAndSecurity(BaseDecisionUXTest):
    def test_unauthorized_actor_clicking_checkbox_is_rejected(self):
        old_body = format_decision_markdown(self.contract)
        new_body = old_body.replace("- [ ] **Option A**", "- [x] **Option A**")

        res = self.mgr.process_issue_edit(
            decision_id="DEC-TEST-UX-01",
            old_body=old_body,
            new_body=new_body,
            editor="malicious_user",
        )

        self.assertEqual(res["status"], "rejected")
        self.assertEqual(res["unblocked_requests"], [])
        self.assertIn("Unauthorized editor", res["rejection_reason"])

        # Tasks remain blocked
        dec = self.mgr.get_decision("DEC-TEST-UX-01")
        self.assertEqual(dec["status"], "pending")
        self.assertIsNone(dec["answer"])

    def test_agent_authored_edit_is_rejected(self):
        old_body = format_decision_markdown(self.contract)
        new_body = old_body.replace("- [ ] **Option A**", "- [x] **Option A**")

        res = self.mgr.process_issue_edit(
            decision_id="DEC-TEST-UX-01",
            old_body=old_body,
            new_body=new_body,
            editor="Wladefant",
            provenance=ProvenanceType.AGENT_AUTHORED,
        )

        self.assertEqual(res["status"], "rejected")
        self.assertIn("Agent-authored edit rejected", res["interpretation"])

    def test_safety_guardrails_reject_destructive_commands(self):
        old_body = format_decision_markdown(self.contract)
        new_body = old_body.replace("- [ ] **Option A**", "- [x] **Option A**")
        new_body = new_body.replace(
            "_Leave any supplemental notes, constraints, or alternative proposals below:_",
            "deploy to prod and drop database",
        )

        res = self.mgr.process_issue_edit(
            decision_id="DEC-TEST-UX-01",
            old_body=old_body,
            new_body=new_body,
            editor="Wladefant",
        )

        self.assertEqual(res["status"], "rejected")
        self.assertIn("Safety refusal", res["rejection_reason"])

    def test_conflicting_edit_on_resolved_decision_is_refused(self):
        # Answer with Option A first
        self.mgr.process_reply(
            decision_id="DEC-TEST-UX-01",
            reply_text="Option A",
            responder="Wladefant",
            comment_id="5559010",
            provenance=ProvenanceType.HUMAN_OPERATOR,
        )

        # Now an edit arrives trying to switch to Option B
        old_body = format_decision_markdown(self.contract)
        new_body = old_body.replace("- [ ] **Option B**", "- [x] **Option B**")

        res = self.mgr.process_issue_edit(
            decision_id="DEC-TEST-UX-01",
            old_body=old_body,
            new_body=new_body,
            editor="Wladefant",
            comment_id="5559011",
        )

        self.assertEqual(res["status"], "rejected")
        self.assertIn("Conflicting edit", res["rejection_reason"])

        # Original answer stays Option A
        dec = self.mgr.get_decision("DEC-TEST-UX-01")
        self.assertEqual(dec["answer"]["selected_option_id"], "A")

    def test_idempotent_replay_of_same_selection(self):
        # Answer with Option A via edit
        old_body = format_decision_markdown(self.contract)
        new_body = old_body.replace("- [ ] **Option A**", "- [x] **Option A**")

        res1 = self.mgr.process_issue_edit(
            decision_id="DEC-TEST-UX-01",
            old_body=old_body,
            new_body=new_body,
            editor="Wladefant",
            comment_id="5559020",
        )
        self.assertEqual(res1["status"], "answered")
        self.assertFalse(res1.get("idempotent_replay", False))

        # Replay identical edit
        res2 = self.mgr.process_issue_edit(
            decision_id="DEC-TEST-UX-01",
            old_body=old_body,
            new_body=new_body,
            editor="Wladefant",
            comment_id="5559020",
        )
        self.assertEqual(res2["status"], "answered")
        self.assertTrue(res2.get("idempotent_replay", False))


class TestGitHubEventIngestion(BaseDecisionUXTest):
    def test_ingest_issue_comment_edited_event(self):
        old_body = format_decision_markdown(self.contract)
        new_body = old_body.replace("- [ ] **Option A**", "- [x] **Option A**")

        event = {
            "action": "edited",
            "issue": {"number": 4543},
            "comment": {
                "id": 5559050,
                "body": new_body,
                "html_url": "https://github.com/Bavariance/polysimulator/issues/4543#issuecomment-5559050",
                "user": {"login": "Wladefant"},
                "updated_at": "2026-09-06T10:20:00Z",
            },
            "changes": {"body": {"from": old_body}},
            "sender": {"login": "Wladefant"},
        }

        res = ingest_github_event(
            event_payload=event,
            decisions_path=self.decisions_path,
            ledger_path=self.ledger_path,
        )

        self.assertEqual(res["status"], "answered")
        self.assertEqual(res["decision_id"], "DEC-TEST-UX-01")
        self.assertIn("req-test-ux-001", res["unblocked_requests"])

    def test_ingest_issues_edited_event(self):
        old_body = format_decision_markdown(self.contract)
        new_body = old_body.replace("- [ ] **Option B**", "- [x] **Option B**")

        event = {
            "action": "edited",
            "issue": {
                "number": 4543,
                "body": new_body,
                "html_url": "https://github.com/Bavariance/polysimulator/issues/4543",
                "updated_at": "2026-09-06T10:25:00Z",
            },
            "changes": {"body": {"from": old_body}},
            "sender": {"login": "Wladefant"},
        }

        res = ingest_github_event(
            event_payload=event,
            decisions_path=self.decisions_path,
            ledger_path=self.ledger_path,
        )

        self.assertEqual(res["status"], "answered")
        self.assertEqual(res["decision_id"], "DEC-TEST-UX-01")

        dec = self.mgr.get_decision("DEC-TEST-UX-01")
        self.assertEqual(dec["answer"]["selected_option_id"], "B")

    def test_ingest_issue_comment_created_event(self):
        event = {
            "action": "created",
            "issue": {"number": 4543},
            "comment": {
                "id": 5559060,
                "body": "Option A - approved for staging",
                "html_url": "https://github.com/Bavariance/polysimulator/issues/4543#issuecomment-5559060",
                "user": {"login": "Wladefant"},
                "created_at": "2026-09-07T10:30:00Z",
                "updated_at": "2026-09-07T10:30:00Z",
            },
            "sender": {"login": "Wladefant"},
        }

        res = ingest_github_event(
            event_payload=event,
            decisions_path=self.decisions_path,
            ledger_path=self.ledger_path,
        )

        self.assertEqual(res["status"], "answered")
        dec = self.mgr.get_decision("DEC-TEST-UX-01")
        self.assertEqual(dec["answer"]["selected_option_id"], "A")
        self.assertEqual(dec["answer"]["additional_context"], "approved for staging")


    def test_cli_ingest_event_json(self):
        old_body = format_decision_markdown(self.contract)
        new_body = old_body.replace("- [ ] **Option B**", "- [x] **Option B**")

        event = {
            "action": "edited",
            "issue": {"number": 4543},
            "comment": {
                "id": 5559070,
                "body": new_body,
                "html_url": "https://github.com/Bavariance/polysimulator/issues/4543#issuecomment-5559070",
                "user": {"login": "Wladefant"},
                "updated_at": "2026-09-07T10:35:00Z",
            },
            "changes": {"body": {"from": old_body}},
            "sender": {"login": "Wladefant"},
        }

        import subprocess
        script_path = os.path.join(SCRIPT_DIR, "decision_workflow.py")
        cmd = [
            sys.executable,
            script_path,
            "--decisions",
            self.decisions_path,
            "--ledger",
            self.ledger_path,
            "ingest-event",
            "--event-json",
            json.dumps(event),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn("Status: answered", proc.stdout)
        self.assertIn("Decision ID: DEC-TEST-UX-01", proc.stdout)
        self.assertIn("Unblocked Requests: req-test-ux-001", proc.stdout)

    def test_cli_ingest_event_path(self):
        old_body = format_decision_markdown(self.contract)
        new_body = old_body.replace("- [ ] **Option A**", "- [x] **Option A**")

        event = {
            "action": "edited",
            "issue": {"number": 4543},
            "comment": {
                "id": 5559080,
                "body": new_body,
                "html_url": "https://github.com/Bavariance/polysimulator/issues/4543#issuecomment-5559080",
                "user": {"login": "Wladefant"},
                "updated_at": "2026-09-07T10:40:00Z",
            },
            "changes": {"body": {"from": old_body}},
            "sender": {"login": "Wladefant"},
        }

        event_file = os.path.join(self.tmp_dir, "event.json")
        with open(event_file, "w", encoding="utf-8") as f:
            json.dump(event, f)

        import subprocess
        script_path = os.path.join(SCRIPT_DIR, "decision_workflow.py")
        cmd = [
            sys.executable,
            script_path,
            "--decisions",
            self.decisions_path,
            "--ledger",
            self.ledger_path,
            "ingest-event",
            "--event-path",
            event_file,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn("Status: answered", proc.stdout)
        self.assertIn("Decision ID: DEC-TEST-UX-01", proc.stdout)
        self.assertIn("Unblocked Requests: req-test-ux-001", proc.stdout)

    def test_cli_show_markdown_includes_clickable_task_list(self):
        import subprocess
        script_path = os.path.join(SCRIPT_DIR, "decision_workflow.py")
        cmd = [
            sys.executable,
            script_path,
            "--decisions",
            self.decisions_path,
            "--ledger",
            self.ledger_path,
            "show",
            "DEC-TEST-UX-01",
            "--markdown",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        self.assertIn("#### Choose an Option (Click checkbox to select)", proc.stdout)
        self.assertIn("- [ ] **Option A**: Dedicated audit_events table", proc.stdout)
        self.assertIn("- [ ] **Option B**: Inline JSON audit column", proc.stdout)
        self.assertIn("#### Additional Context / Alternative Proposal (Optional)", proc.stdout)
if __name__ == "__main__":
    unittest.main()
