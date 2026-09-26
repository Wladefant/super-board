#!/usr/bin/env python3
"""
workflows/portable/test_outer_loop_intake.py — Targeted Unit Tests for Outer-Loop Webhook Intake.

Covers:
  1. Label inference: canonical kind preservation, deprecated label upgrade, duplicate kind deduplication
  2. Operator directives: operator comments overriding text heuristics for kind, milestone, status
  3. Text pattern recognition: canonical kind inference (bug, feature, research, docs, governance, incident, task)
  4. Area and risk label inference and preservation
  5. Milestone inference: preservation of open milestones, keyword matching, fallback
  6. Superboard Project 5 lifecycle card state inference: Backlog, Ready, Building, QA, Review, Blocked, Done
  7. Guaranteed 0-write dry-run mode
  8. Idempotency: guaranteed 0 writes on pre-triaged or unchanged issues, verified across sequential runs
  9. GitHub Actions event payload parsing: issues.opened, issue_comment.created, PR filtering
 10. End-to-end triage flow with mock GraphQL and CLI runners
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional

# Ensure sibling portable workflow modules are importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from outer_loop_intake import (
    CANONICAL_AREAS,
    CANONICAL_KINDS,
    CANONICAL_RISKS,
    DEFAULT_OPERATORS,
    DEFAULT_PROJECT_NUMBER,
    DEFAULT_PROJECT_OWNER,
    OuterLoopIntake,
    TriagePlan,
    TriageResult,
    parse_event_payload,
)


class MockGraphQLRunner:
    """Mock GraphQL runner simulating GitHub API responses for triage testing."""

    def __init__(self):
        self.queries_executed: List[Dict[str, Any]] = []
        self.mutations_executed: List[Dict[str, Any]] = []
        self.issue_data: Dict[str, Any] = {
            "id": "I_mock_issue_123",
            "number": 101,
            "title": "Fix crash in SSE price stream",
            "body": "## Scope\nFix unhandled exception when stream reconnects.\n\n## Acceptance Criteria\n- Reconnect handles 503 cleanly.",
            "state": "OPEN",
            "labels": {"nodes": []},
            "milestone": None,
            "assignees": {"nodes": [{"login": "Wladefant"}]},
            "projectItems": {"nodes": []},
            "comments": {"nodes": []},
        }
        self.open_milestones: List[Dict[str, Any]] = [
            {"id": "MI_1", "number": 1, "title": "Phase 1 - System hardening", "state": "OPEN"},
            {"id": "MI_2", "number": 2, "title": "Phase 2 - Tooling + quota", "state": "OPEN"},
            {"id": "MI_3", "number": 3, "title": "Phase 3 - Docs + rollout", "state": "OPEN"},
            {"id": "MI_4", "number": 4, "title": "GitHub System Integration", "state": "OPEN"},
        ]
        self.project_schema: Dict[str, Any] = {
            "id": "PVT_project_5",
            "title": "Superboard System",
            "fields": {
                "nodes": [
                    {
                        "id": "PVTSSF_status",
                        "name": "Status",
                        "options": [
                            {"id": "opt_backlog", "name": "Backlog"},
                            {"id": "opt_ready", "name": "Ready"},
                            {"id": "opt_building", "name": "Building"},
                            {"id": "opt_qa", "name": "QA"},
                            {"id": "opt_review", "name": "Review"},
                            {"id": "opt_blocked", "name": "Blocked"},
                            {"id": "opt_done", "name": "Done"},
                        ],
                    }
                ]
            },
        }

    def __call__(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        self.queries_executed.append({"query": query, "variables": variables})

        # Project Status Schema Query
        if "projectV2(number: $number)" in query or "repositoryOwner(login: $owner)" in query:
            return {
                "data": {
                    "repositoryOwner": {
                        "projectV2": self.project_schema,
                    }
                }
            }

        # Issue Intake Query
        if "issue(number: $issueNumber)" in query:
            return {
                "data": {
                    "repository": {
                        "issue": self.issue_data,
                    }
                }
            }

        # Repo Metadata Query
        if "milestones(first: 50" in query:
            return {
                "data": {
                    "repository": {
                        "milestones": {"nodes": self.open_milestones},
                    }
                }
            }

        # Add Project Item Mutation
        if "addProjectV2ItemById" in query:
            self.mutations_executed.append({"type": "addProjectV2Item", "variables": variables})
            item_id = "PVTI_mock_item_999"
            # Update local mock issue state to reflect enrollment
            self.issue_data["projectItems"]["nodes"] = [
                {
                    "id": item_id,
                    "project": {
                        "id": variables.get("projectId"),
                        "number": DEFAULT_PROJECT_NUMBER,
                        "title": "Superboard System",
                        "owner": {"login": DEFAULT_PROJECT_OWNER},
                    },
                    "fieldValueByName": {"name": None, "optionId": None},
                }
            ]
            return {
                "data": {
                    "addProjectV2ItemById": {
                        "item": {"id": item_id}
                    }
                }
            }

        # Update Project Item Status Mutation
        if "updateProjectV2ItemFieldValue" in query:
            self.mutations_executed.append({"type": "updateProjectStatus", "variables": variables})
            # Find status name corresponding to optionId
            opts = self.project_schema["fields"]["nodes"][0]["options"]
            opt_name = next((o["name"] for o in opts if o["id"] == variables.get("optionId")), "Ready")
            for item in self.issue_data["projectItems"]["nodes"]:
                if item["id"] == variables.get("itemId"):
                    item["fieldValueByName"] = {"name": opt_name, "optionId": variables.get("optionId")}
            return {
                "data": {
                    "updateProjectV2ItemFieldValue": {
                        "projectV2Item": {"id": variables.get("itemId")}
                    }
                }
            }

        return {"data": {}}


class MockCLIRunner:
    """Mock CLI runner tracking gh issue edit calls."""

    def __init__(self):
        self.commands: List[List[str]] = []

    def __call__(self, args: List[str]) -> str:
        self.commands.append(args)
        return "https://github.com/Wladefant/super-board/issues/101\n"


class TestOuterLoopIntake(unittest.TestCase):
    """Targeted test suite for outer loop intake engine."""

    def setUp(self):
        self.mock_gql = MockGraphQLRunner()
        self.mock_cli = MockCLIRunner()
        self.intake = OuterLoopIntake(
            graphql_runner=self.mock_gql,
            cli_runner=self.mock_cli,
            operator_users={"Wladefant"},
            project_number=DEFAULT_PROJECT_NUMBER,
            project_owner=DEFAULT_PROJECT_OWNER,
        )

    # -----------------------------------------------------------------------
    # 1. Label Inference Tests
    # -----------------------------------------------------------------------

    def test_01_canonical_kind_label_preservation(self):
        """Preserve existing canonical kind: label without modification."""
        kind, to_remove = self.intake.infer_kind_label(
            title="Some generic task title",
            body="Some text with fix and crash",
            existing_labels=["kind:feature", "area:workflow"],
        )
        self.assertEqual(kind, "kind:feature")
        self.assertEqual(to_remove, [])

    def test_02_deprecated_kind_label_upgrade(self):
        """Upgrade deprecated flat labels to dimension-prefixed canonical labels."""
        # 'bug' -> 'kind:bug'
        kind, to_remove = self.intake.infer_kind_label(
            title="App crashes on startup",
            body="Details",
            existing_labels=["bug", "area:ui"],
        )
        self.assertEqual(kind, "kind:bug")
        self.assertEqual(to_remove, ["bug"])

        # 'enhancement' -> 'kind:feature'
        kind, to_remove = self.intake.infer_kind_label(
            title="Add dark mode support",
            body="Details",
            existing_labels=["enhancement"],
        )
        self.assertEqual(kind, "kind:feature")
        self.assertEqual(to_remove, ["enhancement"])

        # 'docs' -> 'kind:docs'
        kind, to_remove = self.intake.infer_kind_label(
            title="Update runbook",
            body="Details",
            existing_labels=["docs"],
        )
        self.assertEqual(kind, "kind:docs")
        self.assertEqual(to_remove, ["docs"])

    def test_03_duplicate_kind_labels_deduplicated_to_single_canonical(self):
        """Ensure exactly ONE canonical kind label by pruning extras according to priority."""
        kind, to_remove = self.intake.infer_kind_label(
            title="Title",
            body="Body",
            existing_labels=["kind:task", "kind:bug", "area:workflow"],
        )
        # kind:bug outranks kind:task
        self.assertEqual(kind, "kind:bug")
        self.assertEqual(to_remove, ["kind:task"])

    def test_04_operator_directive_overrides_text_inference_for_kind(self):
        """Operator comment explicitly specifying /kind overrides body heuristics."""
        comments = [
            {"author": {"login": "random_user"}, "body": "/kind bug"},  # Ignored (non-operator)
            {"author": {"login": "Wladefant"}, "body": "Please treat this as /kind research for now"},
        ]
        kind, to_remove = self.intake.infer_kind_label(
            title="Fix broken price stream crash",
            body="Looks like a regression bug",
            existing_labels=["kind:bug"],
            comments=comments,
        )
        self.assertEqual(kind, "kind:research")
        self.assertEqual(to_remove, ["kind:bug"])

    def test_05_pattern_based_kind_inference_all_canonical_types(self):
        """Verify keyword pattern recognition for all canonical kind types."""
        cases = [
            ("Production database outage and downtime", "kind:incident"),
            ("Crash in settlement loop with traceback", "kind:bug"),
            ("Introduce new user preference endpoint", "kind:feature"),
            ("Investigate feasibility of streaming bridge", "kind:research"),
            ("Update operational runbook and deployment guide", "kind:docs"),
            ("Revise branch protection policy ruleset", "kind:governance"),
            ("Clean up orphan files in worktree", "kind:task"),
        ]
        for text, expected_kind in cases:
            with self.subTest(text=text):
                kind, to_remove = self.intake.infer_kind_label(
                    title=text,
                    body=f"Details for {text}",
                    existing_labels=[],
                )
                self.assertEqual(kind, expected_kind)
                self.assertEqual(to_remove, [])

    def test_06_area_label_preservation_and_inference(self):
        """Preserve existing area: label or infer from domain keywords."""
        # Preserves existing
        area = self.intake.infer_area_label("Telegram bot bridge", "body", ["area:bridge"])
        self.assertIsNone(area)

        # Inferred from keywords
        self.assertEqual(self.intake.infer_area_label("Fix Telegram notifications", "", []), "area:bridge")
        self.assertEqual(self.intake.infer_area_label("Veyyon agent runtime error", "", []), "area:harness")
        self.assertEqual(self.intake.infer_area_label("Upstream fork sync failed", "", []), "area:sync")
        self.assertEqual(self.intake.infer_area_label("Button styling broken on mobile UI", "", []), "area:ui")
        self.assertEqual(self.intake.infer_area_label("Docker runner build slot issue", "", []), "area:infra")
        self.assertEqual(self.intake.infer_area_label("Secret key token redacted", "", []), "area:security")
        # Default for super-board
        self.assertEqual(self.intake.infer_area_label("Generic issue", "", []), "area:workflow")

    def test_07_risk_label_preservation_and_inference(self):
        """Preserve existing risk: label or infer from blast-radius keywords."""
        # Preserves existing
        self.assertIsNone(self.intake.infer_risk_label("Money path", "body", ["risk:money-path"]))

        # Inferred from keywords
        self.assertEqual(self.intake.infer_risk_label("Wallet ledger balance mutation", "", []), "risk:money-path")
        self.assertEqual(self.intake.infer_risk_label("Alembic schema migration drift", "", []), "risk:migration")
        self.assertEqual(self.intake.infer_risk_label("Critical breaking change", "", []), "risk:high")
        self.assertEqual(self.intake.infer_risk_label("Fix defect in calculation", "", []), "risk:medium")
        self.assertEqual(self.intake.infer_risk_label("Update README documentation", "", []), "risk:low")

    # -----------------------------------------------------------------------
    # 2. Milestone Inference Tests
    # -----------------------------------------------------------------------

    def test_08_milestone_preservation_when_open(self):
        """Preserve existing open milestone without overwriting."""
        current_ms = {"number": 2, "title": "Phase 2 - Tooling + quota", "state": "OPEN"}
        target = self.intake.infer_milestone(
            title="GitHub System Integration card",
            body="Details",
            current_milestone=current_ms,
            open_milestones=self.mock_gql.open_milestones,
        )
        self.assertIsNone(target)  # No change required

    def test_09_milestone_operator_directive(self):
        """Operator comment specifying milestone overrides automated inference."""
        comments = [
            {"author": {"login": "Wladefant"}, "body": "/milestone Phase 1 - System hardening"},
        ]
        target = self.intake.infer_milestone(
            title="GitHub System Integration work",
            body="Details",
            current_milestone=None,
            open_milestones=self.mock_gql.open_milestones,
            comments=comments,
        )
        self.assertIsNotNone(target)
        self.assertEqual(target["title"], "Phase 1 - System hardening")

    def test_10_milestone_keyword_matching(self):
        """Match unassigned issue to appropriate open milestone based on text semantics."""
        # GitHub integration keywords
        target = self.intake.infer_milestone(
            title="Outer-loop webhook intake for Project 5",
            body="Sub-issues and triage hierarchy",
            current_milestone=None,
            open_milestones=self.mock_gql.open_milestones,
        )
        self.assertEqual(target["title"], "GitHub System Integration")

        # Quota and tooling keywords
        target_quota = self.intake.infer_milestone(
            title="Balance loader model routing rate limit",
            body="Allowance pacing",
            current_milestone=None,
            open_milestones=self.mock_gql.open_milestones,
        )
        self.assertEqual(target_quota["title"], "Phase 2 - Tooling + quota")

    # -----------------------------------------------------------------------
    # 3. Card State Inference Tests
    # -----------------------------------------------------------------------

    def test_11_closed_issue_maps_to_done(self):
        """Closed issue is mapped to Done regardless of labels."""
        status = self.intake.infer_card_state(
            issue_state="CLOSED",
            issue_body="## Scope\nTask",
            labels=["state:blocked"],
        )
        self.assertEqual(status, "Done")

    def test_12_operator_directives_set_card_state(self):
        """Operator directives set target card lifecycle status."""
        cases = [
            ("Approved, ready for work", "Ready"),
            ("/status ready", "Ready"),
            ("Hold off, blocked on upstream PR", "Blocked"),
            ("/state blocked", "Blocked"),
            ("Work in progress, building now", "Building"),
            ("/status qa", "QA"),
            ("/status review", "Review"),
            ("Landed and verified done", "Done"),
            ("/status backlog", "Backlog"),
        ]
        for comment_text, expected_state in cases:
            with self.subTest(comment=comment_text):
                status = self.intake.infer_card_state(
                    issue_state="OPEN",
                    issue_body="## Scope\n...",
                    labels=[],
                    comments=[{"author": {"login": "Wladefant"}, "body": comment_text}],
                )
                self.assertEqual(status, expected_state)

    def test_13_blockers_and_decisions_map_to_blocked(self):
        """Issues with state:blocked or state:needs-decision labels map to Blocked."""
        self.assertEqual(
            self.intake.infer_card_state("OPEN", "Body", ["state:blocked"]),
            "Blocked",
        )
        self.assertEqual(
            self.intake.infer_card_state("OPEN", "Body", ["state:needs-decision"]),
            "Blocked",
        )
        self.assertEqual(
            self.intake.infer_card_state("OPEN", "Body", [], blocked_by=["https://github.com/repo/issues/1"]),
            "Blocked",
        )

    def test_14_ready_vs_backlog_determination(self):
        """Issue with scope/criteria and assignees maps to Ready; incomplete to Backlog."""
        # Complete issue with owner and scope
        self.assertEqual(
            self.intake.infer_card_state(
                "OPEN",
                "## Scope\nFix X\n## Acceptance Criteria\n- X is fixed",
                labels=[],
                has_owner=True,
                has_criteria=True,
            ),
            "Ready",
        )
        # Empty/incomplete issue
        self.assertEqual(
            self.intake.infer_card_state(
                "OPEN",
                "Vague request without sections",
                labels=[],
                has_owner=False,
                has_criteria=False,
            ),
            "Backlog",
        )

    # -----------------------------------------------------------------------
    # 4. Dry-Run & Idempotency Tests
    # -----------------------------------------------------------------------

    def test_15_dry_run_mode_guaranteed_zero_writes(self):
        """Dry-run mode calculates plan but executes 0 live writes to GitHub."""
        plan = self.intake.plan_triage(
            owner="Wladefant",
            repo="super-board",
            issue_number=101,
        )
        result = self.intake.apply_triage(plan, dry_run=True)

        self.assertTrue(result.ok)
        self.assertTrue(result.dry_run)
        self.assertEqual(result.github_writes, 0)
        self.assertEqual(len(self.mock_gql.mutations_executed), 0)
        self.assertEqual(len(self.mock_cli.commands), 0)

    def test_16_idempotency_zero_writes_on_unchanged_issue(self):
        """An issue already enrolled in Project 5 with target status & labels requires 0 writes."""
        # Configure issue to already have target kind, area, risk, milestone, and Project 5 status
        self.mock_gql.issue_data["labels"] = {
            "nodes": [
                {"name": "kind:bug"},
                {"name": "area:workflow"},
                {"name": "risk:low"},
            ]
        }
        self.mock_gql.issue_data["milestone"] = {
            "id": "MI_4",
            "number": 4,
            "title": "GitHub System Integration",
            "state": "OPEN",
        }
        self.mock_gql.issue_data["projectItems"] = {
            "nodes": [
                {
                    "id": "PVTI_mock_item_999",
                    "project": {
                        "id": "PVT_project_5",
                        "number": DEFAULT_PROJECT_NUMBER,
                        "title": "Superboard System",
                        "owner": {"login": DEFAULT_PROJECT_OWNER},
                    },
                    "fieldValueByName": {"name": "Ready", "optionId": "opt_ready"},
                }
            ]
        }

        plan = self.intake.plan_triage(
            owner="Wladefant",
            repo="super-board",
            issue_number=101,
        )
        self.assertTrue(plan.is_idempotent_noop)
        self.assertEqual(plan.labels_to_add, [])
        self.assertEqual(plan.labels_to_remove, [])
        self.assertFalse(plan.needs_project_enrollment)
        self.assertFalse(plan.needs_status_update)

        result = self.intake.apply_triage(plan, dry_run=False)
        self.assertTrue(result.ok)
        self.assertTrue(result.is_idempotent_noop)
        self.assertEqual(result.github_writes, 0)
        self.assertEqual(len(self.mock_gql.mutations_executed), 0)
        self.assertEqual(len(self.mock_cli.commands), 0)

    def test_17_sequential_run_idempotency(self):
        """First run applies necessary mutations; second run on the resulting state is a no-op (0 writes)."""
        # Run 1: Untriaged issue
        plan1 = self.intake.plan_triage(owner="Wladefant", repo="super-board", issue_number=101)
        self.assertFalse(plan1.is_idempotent_noop)
        result1 = self.intake.apply_triage(plan1, dry_run=False)
        self.assertTrue(result1.ok)
        self.assertGreater(result1.github_writes, 0)

        # Update mock state with the results of Run 1
        self.mock_gql.issue_data["labels"]["nodes"].extend([{"name": l} for l in plan1.labels_to_add])
        if plan1.target_milestone:
            ms = next(m for m in self.mock_gql.open_milestones if m["title"] == plan1.target_milestone)
            self.mock_gql.issue_data["milestone"] = ms

        # Run 2: Re-triage on the updated state
        plan2 = self.intake.plan_triage(owner="Wladefant", repo="super-board", issue_number=101)
        self.assertTrue(plan2.is_idempotent_noop)
        result2 = self.intake.apply_triage(plan2, dry_run=False)
        self.assertTrue(result2.ok)
        self.assertTrue(result2.is_idempotent_noop)
        self.assertEqual(result2.github_writes, 0)

    # -----------------------------------------------------------------------
    # 5. GitHub Event Payload Parsing Tests
    # -----------------------------------------------------------------------

    def test_18_parse_event_payload_issue_opened(self):
        """Parse issues.opened GitHub Actions event payload."""
        payload = {
            "action": "opened",
            "repository": {"full_name": "Wladefant/super-board"},
            "issue": {"number": 246},
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
            json.dump(payload, tf)
            tf_path = tf.name

        try:
            repo, num, action = parse_event_payload(tf_path)
            self.assertEqual(repo, "Wladefant/super-board")
            self.assertEqual(num, 246)
            self.assertEqual(action, "opened")
        finally:
            os.remove(tf_path)

    def test_19_parse_event_payload_skip_pull_request(self):
        """Skip pull request events gracefully."""
        payload = {
            "action": "created",
            "repository": {"full_name": "Wladefant/super-board"},
            "issue": {"number": 240, "pull_request": {"url": "https://api.github.com/..."}},
            "comment": {"body": "LGTM"},
        }
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tf:
            json.dump(payload, tf)
            tf_path = tf.name

        try:
            repo, num, action = parse_event_payload(tf_path)
            self.assertIsNone(repo)
            self.assertIsNone(num)
            self.assertEqual(action, "skip_pr")
        finally:
            os.remove(tf_path)

    # -----------------------------------------------------------------------
    # 6. End-to-End Triage Execution
    # -----------------------------------------------------------------------

    def test_20_full_end_to_end_triage_flow(self):
        """Verify full triage flow: enrolls in Project 5, updates status, sets labels and milestone."""
        plan = self.intake.plan_triage(
            owner="Wladefant",
            repo="super-board",
            issue_number=101,
        )
        self.assertEqual(plan.inferred_kind, "kind:bug")
        self.assertIn("area:workflow", plan.labels_to_add)
        self.assertIn("risk:low", plan.labels_to_add)
        self.assertEqual(plan.target_project_status, "Ready")
        self.assertTrue(plan.needs_project_enrollment)
        self.assertTrue(plan.needs_status_update)

        result = self.intake.apply_triage(plan, dry_run=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.github_writes, 3)  # 1 issue edit + 1 project enroll + 1 status update
        self.assertEqual(len(self.mock_cli.commands), 1)
        self.assertEqual(len(self.mock_gql.mutations_executed), 2)


def main():
    unittest.main(verbosity=2)


if __name__ == "__main__":
    main()
