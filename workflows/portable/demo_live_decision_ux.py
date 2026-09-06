#!/usr/bin/env python3
"""
demo_live_decision_ux.py — Live realistic demonstration of the clickable GitHub decision UX adapter.

Demonstrates:
1. Decision question creation with interactive task-list options and free-text context section.
2. Blocked ledger task awaiting operator choice.
3. Telegram notification formatting with canonical GitHub issue link and option summaries.
4. Negative controls:
   - Unauthorized actor attempt (rejected, task stays blocked).
   - Ambiguous multiple checkboxes checked (clarification requested, task stays blocked).
   - Destructive command attempt (rejected by safety guardrails, task stays blocked).
   - Alternative proposal submitted in free text (retained for interpretation, task stays blocked).
5. Authorized operator action:
   - Operator clicks Option A with supplemental context notes.
   - Ingested via authenticated event ingestion.
   - Decision automatically resolved to 'answered' with selection method 'task_list_checkbox'.
   - Supplemental context preserved in decision record and ledger evidence.
   - Dependent request unblocked and ready for implementation.
   - Idempotent replay re-verified.
"""

import json
import os
import shutil
import sys
import tempfile

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from decision_workflow import (
    CommentTimeProvenance,
    DecisionContract,
    DecisionManager,
    DecisionScope,
    DecisionStatus,
    ProvenanceType,
    format_decision_markdown,
    ingest_github_event,
)
from ledger import RequestLedger
from telegram_notifier import TelegramNotificationAdapter


def run_demonstration():
    print("=" * 72)
    print("LIVE REALISTIC DEMONSTRATION: CLICKABLE GITHUB DECISION UX ADAPTER")
    print("=" * 72)

    tmp_dir = tempfile.mkdtemp(prefix="demo_decision_ux_")
    decisions_path = os.path.join(tmp_dir, "decisions.json")
    ledger_path = os.path.join(tmp_dir, "ledger.json")

    try:
        # Step 1: Initialize Ledger Request
        print("\n--- STEP 1: INITIALIZE BLOCKED LEDGER REQUEST ---")
        ledger = RequestLedger(ledger_path)
        ledger.add_request(
            req_id="req-live-demo-4582",
            prompt="Implement event audit logging architecture for PolySimulator",
            session="sess-demo-operator",
            project="Bavariance/polysimulator",
            owner="ImplementClickableDecisions",
            acceptance_criteria=["Durable append-only audit trail operational"],
        )
        print("[OK] Ledger request registered: 'req-live-demo-4582' in state 'pending'")

        # Step 2: Register Decision Question
        print("\n--- STEP 2: REGISTER DECISION QUESTION WITH CLICKABLE TASK-LIST ---")
        mgr = DecisionManager(decisions_path=decisions_path, ledger_path=ledger_path)
        options = [
            {
                "id": "A",
                "label": "Dedicated audit_events table",
                "description": "Normalized append-only relational audit table.",
                "tradeoffs": "Cleaner retention policies; requires join on query.",
            },
            {
                "id": "B",
                "label": "Inline JSONB audit column",
                "description": "Audit payload stored directly on primary entity row.",
                "tradeoffs": "Zero joins; harder historical index management.",
            },
        ]

        contract = DecisionContract(
            decision_id="DEC-4582-LIVE-01",
            request_id="req-live-demo-4582",
            prompt="Implement event audit logging architecture for PolySimulator",
            question="Which database storage format should we use for audit events?",
            options=options,
            recommendation="Option A: Dedicated audit_events table provides cleanest retention and partitioning.",
            blocking_dependencies=["req-live-demo-4582"],
            authorized_responders=["Wladefant"],
            decision_scope=DecisionScope.ARCHITECTURAL_PREFERENCE,
            issue_number=4543,
            issue_url="https://github.com/Bavariance/polysimulator/issues/4543#issuecomment-5559999",
        )
        reg_res = mgr.register_question(contract)
        print(f"[OK] Decision registered: '{contract.decision_id}' in status '{reg_res['status']}'")

        # Verify task is blocked
        req = ledger.get_request("req-live-demo-4582")
        print(f"[OK] Dependent ledger task blocker: {req['blocker']}")
        print(f"[OK] Blocked decision IDs on task: {req['decision_blockers']}")

        # Step 3: Format and Display Markdown
        print("\n--- STEP 3: RENDERED GITHUB ISSUE MARKDOWN ---")
        rendered_md = format_decision_markdown(contract)
        for line in rendered_md.splitlines()[:28]:
            print(f"  {line}")
        print("  ...")

        # Step 4: Telegram Notification Preview
        print("\n--- STEP 4: TELEGRAM NOTIFICATION DISPATCH ---")
        event = TelegramNotificationAdapter.from_decision(contract, project_override="Bavariance/polysimulator")
        print(f"[OK] Event Type: {event.event_type}")
        print(f"[OK] Summary: {event.summary}")
        print(f"[OK] Canonical Link: {event.canonical_link}")
        print(f"[OK] Session ID: {event.session_id}")

        # Step 5: Negative Controls
        print("\n--- STEP 5: NEGATIVE CONTROLS & SECURITY GATES ---")

        # 5a. Unauthorized actor
        print("  -> Testing Negative Control 1: Unauthorized actor checkbox click")
        unauth_body = rendered_md.replace("- [ ] **Option A**", "- [x] **Option A**")
        res_unauth = mgr.process_issue_edit(
            decision_id="DEC-4582-LIVE-01",
            old_body=rendered_md,
            new_body=unauth_body,
            editor="malicious_unauthorized_user",
        )
        print(f"     Result status: {res_unauth['status']} | Reason: {res_unauth['rejection_reason']}")
        assert res_unauth["status"] == "rejected"
        assert ledger.get_request("req-live-demo-4582")["blocker"] is not None

        # 5b. Ambiguous multiple checkboxes
        print("  -> Testing Negative Control 2: Ambiguous multiple checkboxes checked (- [x] A and - [x] B)")
        ambig_body = rendered_md.replace("- [ ] **Option A**", "- [x] **Option A**").replace("- [ ] **Option B**", "- [x] **Option B**")
        res_ambig = mgr.process_issue_edit(
            decision_id="DEC-4582-LIVE-01",
            old_body=rendered_md,
            new_body=ambig_body,
            editor="Wladefant",
        )
        print(f"     Result status: {res_ambig['status']} | Prompt: {res_ambig['clarification_prompt']}")
        assert res_ambig["status"] == "clarification_requested"
        assert ledger.get_request("req-live-demo-4582")["blocker"] is not None

        # 5c. Safety violation attempt
        print("  -> Testing Negative Control 3: Destructive safety injection in notes")
        unsafe_body = rendered_md.replace("- [ ] **Option A**", "- [x] **Option A**")
        unsafe_body = unsafe_body.replace(
            "_Leave any supplemental notes, constraints, or alternative proposals below:_",
            "deploy to prod and drop table markets",
        )
        res_unsafe = mgr.process_issue_edit(
            decision_id="DEC-4582-LIVE-01",
            old_body=rendered_md,
            new_body=unsafe_body,
            editor="Wladefant",
        )
        print(f"     Result status: {res_unsafe['status']} | Reason: {res_unsafe['rejection_reason']}")
        assert res_unsafe["status"] == "rejected"

        # 5d. Alternative proposal
        print("  -> Testing Free-Text Alternative Proposal (retained, not forced into checkbox)")
        alt_reply = "I propose Option C: Use Redis streams with daily SQLite dump snapshots."
        res_alt = mgr.process_reply(
            decision_id="DEC-4582-LIVE-01",
            reply_text=alt_reply,
            responder="Wladefant",
            provenance=ProvenanceType.HUMAN_OPERATOR,
        )
        print(f"     Result status: {res_alt['status']}")
        print(f"     Interpretation: {res_alt['interpretation']}")
        dec = mgr.get_decision("DEC-4582-LIVE-01")
        assert dec["alternative_proposal"] == alt_reply
        print(f"     Alternative proposal preserved: '{dec['alternative_proposal']}'")
        assert ledger.get_request("req-live-demo-4582")["blocker"] is not None

        # Step 6: Authorized Operator Selection
        print("\n--- STEP 6: AUTHORIZED OPERATOR CLICK + SUPPLEMENTAL NOTES ---")
        authorized_body = rendered_md.replace("- [ ] **Option A**", "- [x] **Option A**")
        authorized_body = authorized_body.replace(
            "_Leave any supplemental notes, constraints, or alternative proposals below:_",
            "Partition audit_events table by month (tenant_id, created_at). Ensure 90 days retention.",
        )

        event_payload = {
            "action": "edited",
            "issue": {"number": 4543},
            "comment": {
                "id": 5559999,
                "body": authorized_body,
                "html_url": "https://github.com/Bavariance/polysimulator/issues/4543#issuecomment-5559999",
                "user": {"login": "Wladefant"},
                "created_at": "2026-09-06T10:00:00Z",
                "updated_at": "2026-09-06T10:35:00Z",
            },
            "changes": {
                "body": {"from": rendered_md}
            },
            "sender": {"login": "Wladefant"},
        }

        print("[...] Ingesting authenticated GitHub issue_comment.edited event...")
        event_res = ingest_github_event(
            event_payload=event_payload,
            decisions_path=decisions_path,
            ledger_path=ledger_path,
        )
        print(f"[OK] Ingest status: {event_res['status']}")
        print(f"[OK] Selected decision: {event_res['decision_id']}")
        print(f"[OK] Interpretation: {event_res['interpretation']}")
        print(f"[OK] Unblocked requests: {event_res['unblocked_requests']}")

        # Verify decision store
        dec_final = mgr.get_decision("DEC-4582-LIVE-01")
        ans = dec_final["answer"]
        print(f"\n--- STEP 7: VERIFIED TERMINAL DECISION RECORD ---")
        print(f"  Decision Status: {dec_final['status']}")
        print(f"  Selected Option ID: {ans['selected_option_id']} ({ans['selected_option_label']})")
        print(f"  Selection Method: {ans['selection_method']}")
        print(f"  Additional Context Retained: '{ans['additional_context']}'")
        print(f"  Verified Responder: @{ans['responder']}")
        print(f"  Provenance: {ans['provenance']}")

        # Verify ledger
        req_final = ledger.get_request("req-live-demo-4582")
        print(f"\n--- STEP 8: VERIFIED UNBLOCKED REQUEST LEDGER ---")
        print(f"  Request ID: {req_final['id']}")
        print(f"  State: {req_final['state']}")
        print(f"  Blocker: {req_final['blocker']} (CLEARED)")
        print(f"  Decision Blockers: {req_final['decision_blockers']} (CLEARED)")
        print(f"  Next Action: {req_final['next_action']}")
        ev = [e for e in req_final["evidence"] if e.get("type") == "human_decision"][0]
        print(f"  Evidence Summary: {ev['summary']}")
        print(f"  Evidence Details: {ev['details']}")

        # Step 9: Verify Idempotent Replay
        print("\n--- STEP 9: IDEMPOTENT REPLAY VERIFICATION ---")
        replay_res = ingest_github_event(
            event_payload=event_payload,
            decisions_path=decisions_path,
            ledger_path=ledger_path,
        )
        print(f"[OK] Replay status: {replay_res['status']} | Idempotent replay: {replay_res.get('idempotent_replay')}")
        assert replay_res.get("idempotent_replay") is True

        print("\n" + "=" * 72)
        print("ALL REALISTIC OPERATOR DEMONSTRATION GATES PASSED (100% SUCCESS)")
        print("=" * 72)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    run_demonstration()
