#!/usr/bin/env python3
"""test_adoption_audit.py - Unit tests for adoption_audit.py."""

from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List, Optional

from adoption_audit import (
    AuditResult,
    AuditSummary,
    Finding,
    GitHubClient,
    check_adopted_or_rejected,
    extract_research_recommendations,
    format_markdown_report,
    match_recommendation_to_subissues,
    run_adoption_audit,
    tokenize,
)


class MockGitHubClient(GitHubClient):
    """In-memory mock for GitHubClient to test adoption audit logic without live API calls."""

    def __init__(
        self,
        issues: Optional[List[Dict[str, Any]]] = None,
        sub_issues_map: Optional[Dict[int, List[Dict[str, Any]]]] = None,
        comments_map: Optional[Dict[int, List[Dict[str, Any]]]] = None,
    ):
        super().__init__(token="mock-token")
        self.issues = issues or []
        self.sub_issues_map = sub_issues_map or {}
        self.comments_map = comments_map or {}
        self.reopened_issues: List[int] = []
        self.added_comments: List[Tuple[int, str]] = []

    def get_issues(self, repo: str, state: str = "all") -> List[Dict[str, Any]]:
        return list(self.issues)

    def get_single_issue(self, repo: str, issue_number: int) -> Optional[Dict[str, Any]]:
        for i in self.issues:
            if i.get("number") == issue_number:
                return i
        return None

    def get_sub_issues(self, repo: str, issue_number: int) -> List[Dict[str, Any]]:
        return list(self.sub_issues_map.get(issue_number, []))

    def get_issue_comments(self, repo: str, issue_number: int) -> List[Dict[str, Any]]:
        return list(self.comments_map.get(issue_number, []))

    def reopen_issue(self, repo: str, issue_number: int) -> bool:
        self.reopened_issues.append(issue_number)
        return True

    def add_comment(self, repo: str, issue_number: int, body: str) -> bool:
        self.added_comments.append((issue_number, body))
        return True


class TestAdoptionAuditExtractionAndMatching(unittest.TestCase):
    """Test recommendation extraction, tokenization, matching, and marker detection."""

    def test_extract_research_recommendations_subheadings(self):
        body = """
# Research: Agent Architecture

## 1. Context
Some background text here.

## 4. Recommended Next Steps (Separate Proposed Tasks)
### Task 1: PolySimulator Feature Map (`frontend/FEATURE_MAP.json` & Skill) [P0]
Scope details for task 1.

### Task 2: Standardized Verification CLI (`scripts/qa/smoke_test.py`) [P0]
Scope details for task 2.

### Task 3: Ban Workaround Comments via ESLint / Ruff Pre-Commit Guard [P1]
Scope details for task 3.

## 5. Full Video Talk Transcript (38:01)
Transcript contents...
"""
        recs = extract_research_recommendations(body)
        self.assertEqual(len(recs), 3)
        self.assertTrue(recs[0].startswith("Task 1: PolySimulator Feature Map"))
        self.assertTrue(recs[1].startswith("Task 2: Standardized Verification CLI"))
        self.assertTrue(recs[2].startswith("Task 3: Ban Workaround Comments"))

    def test_extract_research_recommendations_numbered_list(self):
        body = """
# Research

## Recommendations
1. **Calibrate Effort Levels for Orchestrator and Reviewers**: Lower token spend.
2. **Restrict Thinking Budget to 32k Ceiling**: Ceiling for reasoning.
3. **Strip Prompt Padding & Adopt Negative Constraints**: Improve first token speed.

## Other section
Text here.
"""
        recs = extract_research_recommendations(body)
        self.assertEqual(len(recs), 3)
        self.assertIn("Calibrate Effort Levels for Orchestrator and Reviewers", recs)
        self.assertIn("Restrict Thinking Budget to 32k Ceiling", recs)
        self.assertIn("Strip Prompt Padding & Adopt Negative Constraints", recs)

    def test_extract_research_recommendations_bullet_list(self):
        body = """
## Proposed Actions
- **Enable prompt prefix caching**
- **Refactor managed skills into directory trees**
"""
        recs = extract_research_recommendations(body)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0], "Enable prompt prefix caching")
        self.assertEqual(recs[1], "Refactor managed skills into directory trees")

    def test_extract_research_recommendations_table(self):
        body = """
## Buy recommendation

| Priority | Purchase | Monthly | Why now |
|---|---|---|---|
| 1 | **MiniMax Token Plan — Plus** | **$22** | Only plan whose published terms fit. |
| 2 | **Z.AI GLM Coding Plan — Pro** | **$80** | Best capability per dollar. |
"""
        recs = extract_research_recommendations(body)
        self.assertEqual(len(recs), 2)
        self.assertIn("MiniMax Token Plan — Plus", recs)
        self.assertIn("Z.AI GLM Coding Plan — Pro", recs)

    def test_extract_research_recommendations_empty_or_no_section(self):
        self.assertEqual(extract_research_recommendations(""), [])
        self.assertEqual(extract_research_recommendations("No recommendation section here."), [])

    def test_tokenize(self):
        tokens = tokenize("Task 1: PolySimulator Feature Map (`frontend/FEATURE_MAP.json` & Skill) [P0]")
        self.assertIn("polysimulator", tokens)
        self.assertIn("feature", tokens)
        self.assertIn("map", tokens)
        self.assertIn("skill", tokens)
        self.assertNotIn("task", tokens)
        self.assertNotIn("p0", tokens)

    def test_match_recommendation_to_subissues(self):
        sub_issues = [
            {"number": 244, "title": "Feature map for agent navigation (#227 follow-up)", "state": "open"},
            {"number": 245, "title": "Lint for workaround comments (#227 follow-up)", "state": "open"},
            {"number": 247, "title": "Verification CLI / smoke_test gate (#227 follow-up)", "state": "open"},
        ]

        # Match by keywords and domain keywords
        rec1 = "Task 1: PolySimulator Feature Map (`frontend/FEATURE_MAP.json` & Skill) [P0]"
        m1 = match_recommendation_to_subissues(rec1, sub_issues)
        self.assertIsNotNone(m1)
        self.assertEqual(m1["number"], 244)

        rec2 = "Task 2: Standardized Verification CLI (`scripts/qa/smoke_test.py`) [P0]"
        m2 = match_recommendation_to_subissues(rec2, sub_issues)
        self.assertIsNotNone(m2)
        self.assertEqual(m2["number"], 247)

        rec3 = "Task 3: Ban Workaround Comments via ESLint / Ruff Pre-Commit Guard [P1]"
        m3 = match_recommendation_to_subissues(rec3, sub_issues)
        self.assertIsNotNone(m3)
        self.assertEqual(m3["number"], 245)

        # Unmatched recommendation
        rec4 = "Task 4: Scheduled \"Gardener\" Lane in Superboard Coordinator [P1]"
        m4 = match_recommendation_to_subissues(rec4, sub_issues)
        self.assertIsNone(m4)

    def test_check_adopted_or_rejected(self):
        # Adopted in various formats
        has_it, marker = check_adopted_or_rejected("Done. adopted-at: workflows/portable/gardener.py:12\nProof attached.")
        self.assertTrue(has_it)
        self.assertEqual(marker, "adopted-at: workflows/portable/gardener.py:12")

        has_it, marker = check_adopted_or_rejected("Adopted-at: skill://control-glass")
        self.assertTrue(has_it)
        self.assertEqual(marker, "Adopted-at: skill://control-glass")

        # Rejected in various formats
        has_it, marker = check_adopted_or_rejected("rejected: https://github.com/Wladefant/super-board/issues/195")
        self.assertTrue(has_it)
        self.assertEqual(marker, "rejected: https://github.com/Wladefant/super-board/issues/195")

        has_it, marker = check_adopted_or_rejected("Rejected: rule://production-exclusion")
        self.assertTrue(has_it)
        self.assertEqual(marker, "Rejected: rule://production-exclusion")

    def test_check_adopted_or_rejected_negative(self):
        # Bare mention of rejected or adopt in text should not trigger
        has_it, _ = check_adopted_or_rejected("Formal self-approval remains rejected by policy.")
        self.assertFalse(has_it)

        has_it, _ = check_adopted_or_rejected("We decided not to adopt this approach.")
        self.assertFalse(has_it)

        has_it, _ = check_adopted_or_rejected("")
        self.assertFalse(has_it)


class TestAdoptionAuditScenarios(unittest.TestCase):
    """Test full audit scenarios for conditions (a), (b), and (c)."""

    def test_audit_closed_parent_with_open_subissues_flags_and_reopens(self):
        parent_issue = {
            "number": 227,
            "title": "Research: High-Trust Agent Architecture",
            "state": "closed",  # Prematurely closed!
            "body": "Some body",
            "labels": [{"name": "kind:research"}],
            "sub_issues_summary": {"total": 2, "completed": 1, "percent_completed": 50},
        }
        sub_issues = [
            {"number": 244, "title": "Sub task 1", "state": "open"},
            {"number": 245, "title": "Sub task 2", "state": "closed", "body": "adopted-at: workflows/foo.py:1"},
        ]

        client = MockGitHubClient(
            issues=[parent_issue],
            sub_issues_map={227: sub_issues},
        )

        # Run audit with auto_reopen=True
        result = run_adoption_audit(
            repo="Wladefant/super-board",
            auto_reopen=True,
            dry_run=False,
            client=client,
        )

        self.assertEqual(result.status, "fail")
        self.assertEqual(result.summary.closed_parents_with_open_subissues, 1)
        a_findings = [f for f in result.findings if f.category == "closed_parent_with_open_subissues"]
        self.assertEqual(len(a_findings), 1)
        self.assertEqual(a_findings[0].issue_number, 227)
        self.assertEqual(a_findings[0].action_taken, "reopened")

        # Verify client calls
        self.assertIn(227, client.reopened_issues)
        self.assertEqual(len(client.added_comments), 1)
        self.assertIn("automatically reopened", client.added_comments[0][1])
        self.assertIn("#244", client.added_comments[0][1])

    def test_audit_closed_parent_auto_reopen_dry_run(self):
        parent_issue = {
            "number": 49,
            "title": "Parent Issue",
            "state": "closed",
            "labels": [{"name": "kind:governance"}],
            "sub_issues_summary": {"total": 1, "completed": 0, "percent_completed": 0},
        }
        sub_issues = [{"number": 70, "title": "Open sub", "state": "open"}]

        client = MockGitHubClient(
            issues=[parent_issue],
            sub_issues_map={49: sub_issues},
        )

        result = run_adoption_audit(
            repo="Wladefant/super-board",
            auto_reopen=True,
            dry_run=True,  # Dry run!
            client=client,
        )

        self.assertEqual(result.status, "fail")
        self.assertEqual(len(client.reopened_issues), 0)
        self.assertEqual(len(client.added_comments), 0)
        a_findings = [f for f in result.findings if f.category == "closed_parent_with_open_subissues"]
        self.assertEqual(a_findings[0].action_taken, "dry_run_reopen_skipped")

    def test_audit_open_parent_with_open_subissues_not_flagged(self):
        parent_issue = {
            "number": 227,
            "title": "Open Parent",
            "state": "open",  # Correctly open!
            "body": "Normal body",
            "labels": [{"name": "kind:governance"}],
            "sub_issues_summary": {"total": 1, "completed": 0, "percent_completed": 0},
        }
        sub_issues = [{"number": 244, "title": "Open sub", "state": "open"}]

        client = MockGitHubClient(
            issues=[parent_issue],
            sub_issues_map={227: sub_issues},
        )

        result = run_adoption_audit(repo="Wladefant/super-board", client=client)
        self.assertEqual(result.summary.closed_parents_with_open_subissues, 0)

    def test_audit_closed_subissue_missing_marker_flagged(self):
        parent_issue = {
            "number": 113,
            "title": "Specification Parent",
            "state": "closed",
            "labels": [{"name": "kind:governance"}],
            "sub_issues_summary": {"total": 1, "completed": 1, "percent_completed": 100},
        }
        # Sub-issue closed without adopted-at or rejected
        sub_issue = {
            "number": 114,
            "title": "Closed sub issue",
            "state": "closed",
            "body": "Completed work. No marker.",
        }

        client = MockGitHubClient(
            issues=[parent_issue],
            sub_issues_map={113: [sub_issue]},
            comments_map={114: [{"body": "A normal comment without marker."}]},
        )

        result = run_adoption_audit(repo="Wladefant/super-board", client=client)
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.summary.closed_subissues_without_adoption_or_rejection, 1)
        b_findings = [f for f in result.findings if f.category == "closed_subissue_without_adoption_or_rejection"]
        self.assertEqual(len(b_findings), 1)
        self.assertEqual(b_findings[0].issue_number, 114)

    def test_audit_closed_subissue_with_marker_in_body_passes(self):
        parent_issue = {
            "number": 113,
            "title": "Specification Parent",
            "state": "closed",
            "labels": [{"name": "kind:governance"}],
            "sub_issues_summary": {"total": 1, "completed": 1, "percent_completed": 100},
        }
        sub_issue = {
            "number": 114,
            "title": "Closed sub issue",
            "state": "closed",
            "body": "All work landed.\nadopted-at: policies/default/AGENTS.md:46",
        }

        client = MockGitHubClient(
            issues=[parent_issue],
            sub_issues_map={113: [sub_issue]},
        )

        result = run_adoption_audit(repo="Wladefant/super-board", client=client)
        self.assertEqual(result.summary.closed_subissues_without_adoption_or_rejection, 0)
        self.assertEqual(result.status, "pass")

    def test_audit_closed_subissue_with_marker_in_comments_passes(self):
        parent_issue = {
            "number": 113,
            "title": "Specification Parent",
            "state": "closed",
            "labels": [{"name": "kind:governance"}],
            "sub_issues_summary": {"total": 1, "completed": 1, "percent_completed": 100},
        }
        sub_issue = {
            "number": 114,
            "title": "Closed sub issue",
            "state": "closed",
            "body": "Initial issue body.",
        }

        client = MockGitHubClient(
            issues=[parent_issue],
            sub_issues_map={113: [sub_issue]},
            comments_map={114: [
                {"body": "First comment."},
                {"body": "Closing comment.\nrejected: https://github.com/Wladefant/super-board/issues/100"},
            ]},
        )

        result = run_adoption_audit(repo="Wladefant/super-board", client=client)
        self.assertEqual(result.summary.closed_subissues_without_adoption_or_rejection, 0)
        self.assertEqual(result.status, "pass")

    def test_audit_research_recommendation_without_subissue(self):
        research_issue = {
            "number": 227,
            "title": "Research: High-Trust Agent Architecture",
            "state": "open",
            "labels": [{"name": "kind:research"}],
            "sub_issues_summary": {"total": 2, "completed": 0, "percent_completed": 0},
            "body": """
## 4. Recommended Next Steps (Separate Proposed Tasks)
### Task 1: PolySimulator Feature Map (`frontend/FEATURE_MAP.json` & Skill) [P0]
Details.
### Task 2: Scheduled "Gardener" Lane in Superboard Coordinator [P1]
Details.
""",
        }
        # Only Task 1 has a sub-issue, Task 2 does not!
        sub_issues = [
            {"number": 244, "title": "Feature map for agent navigation (#227 follow-up)", "state": "open"},
        ]

        client = MockGitHubClient(
            issues=[research_issue],
            sub_issues_map={227: sub_issues},
        )

        result = run_adoption_audit(repo="Wladefant/super-board", client=client)
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.summary.research_recommendations_without_subissues, 1)
        c_findings = [f for f in result.findings if f.category == "research_recommendation_without_subissue"]
        self.assertEqual(len(c_findings), 1)
        self.assertIn("Gardener", c_findings[0].details["recommendation"])

    def test_audit_single_issue_filter(self):
        issue1 = {
            "number": 100,
            "title": "Issue 100",
            "state": "closed",
            "labels": [{"name": "kind:governance"}],
            "sub_issues_summary": {"total": 1, "completed": 0, "percent_completed": 0},
        }
        issue2 = {
            "number": 200,
            "title": "Issue 200",
            "state": "closed",
            "labels": [{"name": "kind:governance"}],
            "sub_issues_summary": {"total": 1, "completed": 0, "percent_completed": 0},
        }

        client = MockGitHubClient(
            issues=[issue1, issue2],
            sub_issues_map={
                100: [{"number": 101, "title": "Sub 101", "state": "open"}],
                200: [{"number": 201, "title": "Sub 201", "state": "open"}],
            },
        )

        # Audit only issue 100
        result = run_adoption_audit(repo="Wladefant/super-board", issue_number=100, client=client)
        self.assertEqual(result.summary.total_issues_scanned, 1)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].issue_number, 100)

    def test_json_and_markdown_formatting(self):
        finding = Finding(
            category="closed_parent_with_open_subissues",
            issue_number=49,
            title="Parent Issue",
            url="https://github.com/Wladefant/super-board/issues/49",
            details={
                "open_subissues_count": 1,
                "open_subissues": [{"number": 70, "title": "Sub issue", "url": "https://github.com/70", "state": "open"}],
            },
            action_taken="reopened",
        )
        result = AuditResult(
            repo="Wladefant/super-board",
            timestamp="2026-09-26T12:00:00Z",
            status="fail",
            summary=AuditSummary(
                total_issues_scanned=10,
                total_findings=1,
                closed_parents_with_open_subissues=1,
            ),
            findings=[finding],
        )

        # Test JSON serialization
        d = result.to_dict()
        json_str = json.dumps(d)
        parsed = json.loads(json_str)
        self.assertEqual(parsed["status"], "fail")
        self.assertEqual(parsed["summary"]["closed_parents_with_open_subissues"], 1)

        # Test Markdown report
        md = format_markdown_report(result)
        self.assertIn("# 📋 Adoption & Sub-Issue Integrity Audit Report", md)
        self.assertIn("Issue #49", md)
        self.assertIn("⚠️ Open sub-issue #70", md)

    def test_audit_research_all_recommendations_matched_passes(self):
        research_issue = {
            "number": 227,
            "title": "Research: Agent Architecture",
            "state": "open",
            "labels": [{"name": "kind:research"}],
            "sub_issues_summary": {"total": 2, "completed": 0, "percent_completed": 0},
            "body": """
## Recommended Next Steps
### Task 1: Feature map for agent navigation
Details.
### Task 2: Standardized verification CLI
Details.
""",
        }
        sub_issues = [
            {"number": 244, "title": "Feature map for agent navigation (#227 follow-up)", "state": "open"},
            {"number": 247, "title": "Standardized verification CLI (#227 follow-up)", "state": "open"},
        ]
        client = MockGitHubClient(
            issues=[research_issue],
            sub_issues_map={227: sub_issues},
        )
        result = run_adoption_audit(repo="Wladefant/super-board", client=client)
        self.assertEqual(result.summary.research_recommendations_without_subissues, 0)
        self.assertEqual(result.status, "pass")

    def test_audit_closed_issue_without_subissues_passes(self):
        issue = {
            "number": 150,
            "title": "Ordinary closed task",
            "state": "closed",
            "labels": [{"name": "kind:feature"}],
            "sub_issues_summary": {"total": 0, "completed": 0, "percent_completed": 0},
            "body": "Normal completed feature",
        }
        client = MockGitHubClient(issues=[issue])
        result = run_adoption_audit(repo="Wladefant/super-board", client=client)
        self.assertEqual(result.status, "pass")
        self.assertEqual(len(result.findings), 0)

    def test_format_markdown_report_clean(self):
        result = AuditResult(
            repo="Wladefant/super-board",
            timestamp="2026-09-26T12:00:00Z",
            status="pass",
            summary=AuditSummary(total_issues_scanned=5),
            findings=[],
        )
        md = format_markdown_report(result)
        self.assertIn("PASSED (0 findings)", md)
        self.assertIn("All audited issues satisfy the adopt-or-reject policy", md)


if __name__ == "__main__":
    unittest.main()
