#!/usr/bin/env python3
"""
Unit and Smoke Test Suite for Deterministic GitHub PR Status Gate Helper
Location: ~/.veyyon/workflows/test_github_pr_gate.py

Verifies:
  1. Valid passing PR: Open, non-draft, all CI checks successful, independent approval pinned to head.
  2. Failing CI check: Immediate BLOCKED verdict identifying specific failing checks.
  3. Pending CI check: PENDING verdict awaiting workflow completion.
  4. Draft PR: Blocked from promotion until marked ready for review.
  5. Unapproved PR: Blocked until an independent review approval is submitted.
  6. Self-approval invariant: PR author cannot self-approve.
  7. Head invalidation: Approval on old head commit is strictly invalidated when head SHA changes.
  8. Expiry on new CI failure: Review invalidated if CI fails after the approval was granted.
  9. Expiry on security finding: Review invalidated if a new security alert is flagged after approval.
  10. Review reuse: Head-bound review is safely reused when head SHA and CI are unchanged (zero LLM churn).
  11. Real gh CLI smoke: Evaluates live CLI execution against real repository.
  12. Base SHA absence: `gh pr view --json` exposes no base SHA field; REST-shaped base.sha is read.
  13. Pinned head binding: a caller-pinned head that differs from the live head is a hard block.
  14. Base invalidation: base movement (or an unresolvable base) invalidates a base-bound review.
  15. Approval pinning: an approval with no commit OID, or one pinned elsewhere, never approves.
  16. CLI reference parsing: PR number, PR URL, and invalid reference handling.
  17. Approval policy: resolved per repo/base; production-protected bases cannot be relaxed.
  18. Waived GitHub approval still requires head-bound independent review evidence.
  19. Advisory vs blocking checks; absent native required-check data never drops CI.
"""

import argparse
import copy
import datetime
import json
import os
import shutil
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from github_pr_gate import (
    DEPLOY_CRITICAL_CHECKS,
    GateApprovalPolicy,
    PRGateEvaluation,
    evaluate_pr_gate,
    evaluate_review_requirement,
    fetch_pr_json,
    is_lockfile_or_generated,
    parse_pr_ref,
    resolve_gate_policy,
)


class TestGitHubPRGate(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Every test reads the same immutable commits; mutable API payloads
        # are rebuilt in setUp. No live GitHub call belongs in this suite.
        cls.repository = tempfile.TemporaryDirectory(prefix="gate-content-fixture-")
        cls.addClassCleanup(cls.repository.cleanup)
        previous = os.getcwd()
        cls.addClassCleanup(os.chdir, previous)
        os.chdir(cls.repository.name)
        def git(*args):
            return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL).decode().strip()
        git("init", "-b", "fixture-base")
        git("config", "user.name", "Wladimir Kirjanovs")
        git("config", "user.email", "wladefant@gmail.com")
        with open("change.txt", "w", encoding="utf-8") as source:
            source.write("base\n")
        git("add", ".")
        git("commit", "-m", "base")
        cls.base_sha = git("rev-parse", "HEAD")
        for base in ("fixture-base", "main", "staging"):
            git("update-ref", f"refs/remotes/origin/{base}", cls.base_sha)
            if base != "fixture-base":
                git("branch", base, cls.base_sha)
        git("remote", "add", "origin", cls.repository.name)
        git("checkout", "-b", "feature")
        with open("change.txt", "w", encoding="utf-8") as source:
            source.write("changed\n")
        git("add", ".")
        git("commit", "-m", "feature")
        cls.head_sha = git("rev-parse", "HEAD")

    def setUp(self):
        self.mock_pr = {
            "number": 4545,
            "state": "OPEN",
            "isDraft": False,
            "headRefOid": self.head_sha,
            "baseRefOid": self.base_sha,
            "baseRefName": "fixture-base",
            "author": {"login": "feature-developer"},
            "statusCheckRollup": [
                {
                    "name": "test-suite",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "completedAt": "2026-09-05T08:00:00Z",
                },
                {
                    "name": "lint-and-typecheck",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "completedAt": "2026-09-05T08:05:00Z",
                },
                {
                    "name": "security-scan",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "completedAt": "2026-09-05T08:10:00Z",
                },
            ],
            "reviews": [
                {
                    "author": {"login": "independent-reviewer"},
                    "state": "APPROVED",
                    "submittedAt": "2026-09-05T08:15:00Z",
                    "commit": {"oid": self.head_sha},
                }
            ],
        }

    def make_review_artifact(
        self,
        *,
        head_sha=None,
        base_sha=None,
        reviewer="independent-review-agent",
        outcome="approved",
        repository="Bavariance/polysimulator",
        pull_request=4545,
        author="feature-developer",
        source_uri=None,
    ):
        source_uri = source_uri or f"agent://{reviewer}"
        return {
            "schema": "portable-review/v1",
            "artifact_type": "independent_automated_code_review",
            "repository": repository,
            "pull_request": pull_request,
            "head_sha": head_sha or self.head_sha,
            "base_sha": base_sha or self.base_sha,
            "outcome": outcome,
            "submitted_at": "2026-09-05T08:15:00Z",
            "subject": {"author_login": author},
            "reviewer": {"actor_id": reviewer, "actor_type": "automation"},
            "source": {
                "kind": "agent_transcript",
                "uri": source_uri,
                "producer_id": reviewer,
                "sha256": "a" * 64,
            },
        }

    def waived_policy(self):
        return GateApprovalPolicy(
            repo="Bavariance/polysimulator",
            base_ref="staging",
            require_github_approval=False,
            require_head_bound_review_evidence=True,
        )

    QA_RECEIPT_URL = "https://github.com/Bavariance/polysimulator/pull/4545#issuecomment-900000001"

    def staging_policy(self):
        return GateApprovalPolicy(
            repo="Bavariance/polysimulator",
            base_ref="staging",
            require_github_approval=False,
            require_head_bound_review_evidence=True,
            allow_review_exemption=True,
        )

    def qa_receipt_comment(self, *, named=None, identity=None, images=2, marker="PASS", served=None, extra=""):
        """A browser-QA receipt in the shape lanes post on a PR, for `named` (default: the head).

        `served` mirrors the printer's `QA-RECEIPT: PASS <served-sha>` form: the revision QA
        ran against, which the gate binds to on its own.
        """
        named = named or self.head_sha
        identity = named if identity is None else identity
        lines = []
        if marker:
            suffix = f" {served}" if served else ""
            lines.append(f"QA-RECEIPT: {marker}{suffix}")
        lines.append(f"Browser QA on {named} (identity {identity}).")
        lines.extend(
            f"![shot-{i}](https://github.com/user-attachments/assets/{i:08d}-1111-2222-3333-{i:012d})"
            for i in range(images)
        )
        if extra:
            lines.append(extra)
        return {"body": "\n".join(lines), "html_url": self.QA_RECEIPT_URL}

    def staging_ui_pr(self, receipt=None, *, files=None, comments=None):
        """A review-exempt staging PR whose diff reaches the order ticket UI."""
        pr = copy.deepcopy(self.mock_pr)
        pr["reviews"] = []
        pr["baseRefName"] = "staging"
        pr["labels"] = []
        pr["files"] = files or [
            {"path": "frontend/components/OrderTicket.tsx", "additions": 8, "deletions": 3}
        ]
        if comments is None:
            comments = [receipt] if receipt else []
        pr["comments"] = comments
        return pr

    def test_unresolvable_live_head_never_approves(self):
        for head in ("", "short", "g" * 40):
            for expected in (None, self.head_sha):
                with self.subTest(head=head, expected=expected):
                    pr = copy.deepcopy(self.mock_pr)
                    pr["headRefOid"] = head
                    pr["reviews"][0]["commit"] = {"oid": head}
                    result = evaluate_pr_gate(pr, expected_head_sha=expected)
                    self.assertEqual(result.gate_verdict, "BLOCKED")
                    self.assertTrue(result.review_invalidated)

    def test_protected_policy_cannot_make_failed_checks_advisory(self):
        with tempfile.TemporaryDirectory(prefix="gate_strict_ci_") as directory:
            path = os.path.join(directory, "policy.json")
            for require_approval in (False, True):
                with self.subTest(require_approval=require_approval):
                    with open(path, "w", encoding="utf-8") as config_file:
                        json.dump({"policies": [{
                            "repo": "Bavariance/polysimulator",
                            "base_ref": "main",
                            "require_github_approval": require_approval,
                            "require_head_bound_review_evidence": False,
                            "advisory_checks": ["*"],
                        }]}, config_file)
                    policy = resolve_gate_policy(
                        "Bavariance/polysimulator", "main", config_path=path
                    )
                    pr = copy.deepcopy(self.mock_pr)
                    pr["baseRefName"] = "main"
                    pr["statusCheckRollup"][0]["conclusion"] = "FAILURE"
                    result = evaluate_pr_gate(pr, policy=policy)
                    self.assertEqual(result.gate_verdict, "BLOCKED")
                    self.assertEqual(result.failing_checks, ["test-suite"])
                    self.assertEqual(policy.advisory_checks, [])
                    self.assertTrue(policy.require_head_bound_review_evidence)

    # -------------------------------------------------------------------------
    # TEST 1: Valid Passing PR
    # -------------------------------------------------------------------------
    def test_valid_passing_pr(self):
        print("\n--- TEST 1: Valid Passing PR Gate Evaluation ---")
        result = evaluate_pr_gate(self.mock_pr)
        self.assertEqual(result.gate_verdict, "PASSED")
        self.assertEqual(result.ci_verdict, "SUCCESS")
        self.assertEqual(result.approval_verdict, "APPROVED")
        self.assertEqual(result.approved_by, "independent-reviewer")
        self.assertFalse(result.review_invalidated)
        print(f"  [PASS] Gate passed: {result.verdict_reason}")

    # -------------------------------------------------------------------------
    # TEST 2: Failing CI Check
    # -------------------------------------------------------------------------
    def test_failing_ci_check(self):
        print("\n--- TEST 2: Failing CI Check Blocks Gate ---")
        pr = copy.deepcopy(self.mock_pr)
        pr["statusCheckRollup"][0]["conclusion"] = "FAILURE"

        result = evaluate_pr_gate(pr)
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertEqual(result.ci_verdict, "FAILURE")
        self.assertIn("test-suite", result.failing_checks)
        self.assertIn("failed", result.verdict_reason.lower())
        print(f"  [PASS] Failing CI correctly blocked: {result.verdict_reason}")

    # -------------------------------------------------------------------------
    # TEST 3: Pending CI Check
    # -------------------------------------------------------------------------
    def test_pending_ci_check(self):
        print("\n--- TEST 3: Pending CI Check Emits PENDING ---")
        pr = copy.deepcopy(self.mock_pr)
        pr["statusCheckRollup"][1]["status"] = "IN_PROGRESS"
        pr["statusCheckRollup"][1]["conclusion"] = ""

        result = evaluate_pr_gate(pr)
        self.assertEqual(result.gate_verdict, "PENDING")
        self.assertEqual(result.ci_verdict, "PENDING")
        self.assertIn("lint-and-typecheck", result.pending_checks)
        print(f"  [PASS] Pending CI emitted PENDING: {result.verdict_reason}")

    # -------------------------------------------------------------------------
    # TEST 4: Draft PR Blocked
    # -------------------------------------------------------------------------
    def test_draft_pr_blocked(self):
        print("\n--- TEST 4: Draft PR Blocked ---")
        pr = copy.deepcopy(self.mock_pr)
        pr["isDraft"] = True

        result = evaluate_pr_gate(pr)
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertIn("draft", result.verdict_reason.lower())
        print(f"  [PASS] Draft PR correctly blocked: {result.verdict_reason}")

    # -------------------------------------------------------------------------
    # TEST 5: Unapproved PR Blocked
    # -------------------------------------------------------------------------
    def test_unapproved_pr_blocked(self):
        print("\n--- TEST 5: Unapproved PR Blocked ---")
        pr = copy.deepcopy(self.mock_pr)
        pr["reviews"] = []

        result = evaluate_pr_gate(pr)
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertEqual(result.approval_verdict, "UNAPPROVED")
        self.assertIn("no github approved review", result.verdict_reason.lower())
        print(f"  [PASS] Unapproved PR correctly blocked: {result.verdict_reason}")

    # -------------------------------------------------------------------------
    # TEST 6: Self-Approval Invariant
    # -------------------------------------------------------------------------
    def test_self_approval_rejected(self):
        print("\n--- TEST 6: Self-Approval Invariant (No Self-Merges) ---")
        pr = copy.deepcopy(self.mock_pr)
        # Reviewer is the PR author
        pr["reviews"][0]["author"]["login"] = "feature-developer"

        result = evaluate_pr_gate(pr)
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertEqual(result.approval_verdict, "SELF_APPROVED_ONLY")
        self.assertIn("self-approval rejected", result.verdict_reason.lower())
        print(f"  [PASS] Self-approval rejected: {result.verdict_reason}")


    # -------------------------------------------------------------------------
    # TEST 6B: COMMENTED Self-Review Never Treated as Approval
    # -------------------------------------------------------------------------
    def test_commented_self_review_never_approves(self):
        print("\n--- TEST 6B: COMMENTED Self-Review Never Approves Gate ---")
        pr = copy.deepcopy(self.mock_pr)
        # Author submits a review with state "COMMENTED" (e.g. self-comments saying looks good)
        pr["reviews"][0]["author"]["login"] = "feature-developer"
        pr["reviews"][0]["state"] = "COMMENTED"

        result = evaluate_pr_gate(pr)
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertEqual(result.approval_verdict, "SELF_APPROVED_ONLY")
        self.assertIn("self-approval", result.verdict_reason.lower())
        print(f"  [PASS] COMMENTED self-review correctly blocked: {result.verdict_reason}")

        # A source-backed artifact produced by the PR author is also blocked.
        prior_self_review = self.make_review_artifact(reviewer="feature-developer")
        pr_no_reviews = copy.deepcopy(self.mock_pr)
        pr_no_reviews["reviews"] = []
        result2 = evaluate_pr_gate(pr_no_reviews, review_artifact=prior_self_review)
        self.assertEqual(result2.gate_verdict, "BLOCKED")
        self.assertTrue(result2.review_invalidated)
        self.assertIn("pr author", result2.invalidation_reason.lower())
        print(f"  [PASS] Prior self-review record correctly invalidated: {result2.invalidation_reason}")
    # -------------------------------------------------------------------------
    # TEST 7: Head Change Invalidates Prior Review
    # -------------------------------------------------------------------------
    def test_head_change_invalidates_review(self):
        print("\n--- TEST 7: Head Change Invalidates Review ---")
        # PR has a new head while the artifact remains pinned to the old head.
        pr = copy.deepcopy(self.mock_pr)
        pr["headRefOid"] = "b" * 40
        pr["reviews"] = []
        prior_review = self.make_review_artifact()

        result = evaluate_pr_gate(
            pr, review_artifact=prior_review, policy=self.waived_policy()
        )
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertTrue(result.review_invalidated)
        self.assertIn("does not match live head", result.invalidation_reason.lower())
        print(f"  [PASS] Head change correctly invalidated review: {result.invalidation_reason}")

    # -------------------------------------------------------------------------
    # TEST 8: New CI Failure Invalidates Prior Review Approval
    # -------------------------------------------------------------------------
    def test_new_ci_failure_invalidates_review(self):
        print("\n--- TEST 8: New CI Failure Invalidates Prior Approval ---")
        # Prior review approved at 08:15:00Z
        # CI failed at 08:20:00Z (after approval!)
        pr = copy.deepcopy(self.mock_pr)
        pr["statusCheckRollup"][0]["conclusion"] = "FAILURE"
        pr["statusCheckRollup"][0]["completedAt"] = "2026-09-05T08:20:00Z"

        prior_review = self.make_review_artifact()

        result = evaluate_pr_gate(
            pr, review_artifact=prior_review, policy=self.waived_policy()
        )
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertTrue(result.review_invalidated)
        self.assertIn("new ci failure occurred after automated review", result.invalidation_reason.lower())
        print(f"  [PASS] Post-review CI failure invalidated approval: {result.invalidation_reason}")

    # -------------------------------------------------------------------------
    # TEST 9: New Security Alert Invalidates Prior Review
    # -------------------------------------------------------------------------
    def test_new_security_alert_invalidates_review(self):
        print("\n--- TEST 9: New Security Alert Invalidates Review ---")
        pr = copy.deepcopy(self.mock_pr)
        prior_review = self.make_review_artifact()
        # Security alert flagged at 08:30:00Z
        alerts = [{"id": "sec-001", "created_at": "2026-09-05T08:30:00Z", "severity": "high"}]

        result = evaluate_pr_gate(
            pr,
            review_artifact=prior_review,
            security_alerts=alerts,
            policy=self.waived_policy(),
        )
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertTrue(result.review_invalidated)
        self.assertIn("security alert", result.invalidation_reason.lower())
        print(f"  [PASS] New security alert invalidated approval: {result.invalidation_reason}")

    def test_native_review_safety_does_not_depend_on_local_artifact(self):
        pr = copy.deepcopy(self.mock_pr)
        pr["statusCheckRollup"][0].update(
            conclusion="FAILURE", completedAt="2026-09-05T08:20:00Z"
        )
        failed_ci = evaluate_pr_gate(pr, policy=self.waived_policy())
        self.assertEqual(failed_ci.gate_verdict, "BLOCKED")
        self.assertTrue(failed_ci.review_invalidated)
        self.assertFalse(failed_ci.review_reused)

        pr = copy.deepcopy(self.mock_pr)
        for alerts in (
            [{"created_at": "2026-09-05T08:30:00Z", "severity": "high"}],
            [{"severity": "high"}],
        ):
            blocked = evaluate_pr_gate(pr, security_alerts=alerts, policy=self.waived_policy())
            self.assertEqual(blocked.gate_verdict, "BLOCKED")
            self.assertTrue(blocked.review_invalidated)
        known_alert = evaluate_pr_gate(
            pr, security_alerts=[{"created_at": "2026-09-05T08:00:00Z"}],
            policy=self.waived_policy(),
        )
        self.assertEqual(known_alert.gate_verdict, "PASSED")
        self.assertTrue(known_alert.review_reused)

        del pr["reviews"][0]["submittedAt"]
        unknown_time = evaluate_pr_gate(
            pr, security_alerts=[{"created_at": "2026-09-05T08:00:00Z"}],
            policy=self.waived_policy(),
        )
        self.assertEqual(unknown_time.gate_verdict, "BLOCKED")

    # -------------------------------------------------------------------------
    # TEST 10: Review Reuse on Unchanged Head & Clean Checks
    # -------------------------------------------------------------------------
    def test_review_reuse_unchanged_head(self):
        print("\n--- TEST 10: Review Reuse on Unchanged Head (Zero LLM Tokens) ---")
        # Native reviews bind to content; local artifacts are not work authority.
        pr = copy.deepcopy(self.mock_pr)

        prior_review = self.make_review_artifact()

        result = evaluate_pr_gate(
            pr, review_artifact=prior_review, policy=self.waived_policy()
        )
        self.assertEqual(result.gate_verdict, "PASSED")
        self.assertTrue(result.review_reused)
        self.assertFalse(result.review_invalidated)
        self.assertEqual(result.approved_by, "independent-reviewer")
        self.assertIn("review matches current content", result.verdict_reason.lower())
        print(f"  [PASS] Review successfully reused: {result.verdict_reason}")

    # -------------------------------------------------------------------------
    # TEST 12: `gh pr view --json` exposes no base SHA field
    # -------------------------------------------------------------------------
    def test_base_sha_absent_from_raw_pr_view_payload(self):
        print("\n--- TEST 12: Raw gh pr view Payload Carries No Base SHA ---")
        # Faithful `gh pr view --json ...` output: baseRefName only, never baseRefOid.
        pr = copy.deepcopy(self.mock_pr)
        del pr["baseRefOid"]
        pr["baseRefName"] = "main"

        result = evaluate_pr_gate(pr)
        self.assertEqual(result.base_sha, "")
        self.assertEqual(result.gate_verdict, "PASSED")
        print("  [PASS] Raw view payload yields empty base_sha (must be resolved separately)")

        # REST-shaped nested base is accepted without a separate injection step.
        pr_rest = copy.deepcopy(pr)
        pr_rest["base"] = {"sha": self.base_sha}
        self.assertEqual(evaluate_pr_gate(pr_rest).base_sha, self.base_sha)
        print(f"  [PASS] Nested base.sha resolved: {self.base_sha[:8]}")

    # -------------------------------------------------------------------------
    # TEST 13: Pinned head must match the live head
    # -------------------------------------------------------------------------
    def test_expected_head_mismatch_blocks(self):
        print("\n--- TEST 13: Pinned Head Mismatch Is A Hard Block ---")
        pr = copy.deepcopy(self.mock_pr)
        stale = "0" * 40

        result = evaluate_pr_gate(pr, expected_head_sha=stale)
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertTrue(result.review_invalidated)
        self.assertIn("head mismatch", result.verdict_reason.lower())
        print(f"  [PASS] Stale pinned head blocked: {result.verdict_reason}")

        # Pinning the true head must not disturb an otherwise passing gate.
        matched = evaluate_pr_gate(pr, expected_head_sha=self.head_sha)
        self.assertEqual(matched.gate_verdict, "PASSED")
        print("  [PASS] Correct pinned head still PASSES")

    # -------------------------------------------------------------------------
    # TEST 14: Base movement invalidates a reused review
    # -------------------------------------------------------------------------
    def test_base_change_invalidates_reused_review(self):
        print("\n--- TEST 14: Base Change Invalidates Prior Approval ---")
        pr = copy.deepcopy(self.mock_pr)
        pr["reviews"] = []
        prior_review = self.make_review_artifact(base_sha="9" * 40)

        result = evaluate_pr_gate(
            pr, review_artifact=prior_review, policy=self.waived_policy()
        )
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertTrue(result.review_invalidated)
        self.assertFalse(result.review_reused)
        self.assertIn("does not match live base", (result.invalidation_reason or "").lower())
        print(f"  [PASS] Base movement invalidated review: {result.invalidation_reason}")

        # Unresolvable base cannot prove "base unchanged" for a base-bound review.
        pr_no_base = copy.deepcopy(pr)
        del pr_no_base["baseRefOid"]
        unresolved = evaluate_pr_gate(
            pr_no_base, review_artifact=prior_review, policy=self.waived_policy()
        )
        self.assertEqual(unresolved.gate_verdict, "BLOCKED")
        self.assertTrue(unresolved.review_invalidated)
        print("  [PASS] Unresolved base blocks base-bound review reuse")

        # A structurally valid local artifact alone is still insufficient:
        # the preserved installed gate requires a content-bound GitHub review.
        prior_ok = self.make_review_artifact()
        reused = evaluate_pr_gate(pr, review_artifact=prior_ok, policy=self.waived_policy())
        self.assertEqual(reused.gate_verdict, "BLOCKED")
        self.assertFalse(reused.review_invalidated)
        pr["reviews"] = copy.deepcopy(self.mock_pr["reviews"])
        self.assertEqual(evaluate_pr_gate(pr, policy=self.waived_policy()).gate_verdict, "PASSED")

    # -------------------------------------------------------------------------
    # TEST 15: An approval with no commit OID is not head-bound evidence
    # -------------------------------------------------------------------------
    def test_approval_without_commit_oid_is_not_head_bound(self):
        print("\n--- TEST 15: Approval Lacking Commit OID Never Approves ---")
        pr = copy.deepcopy(self.mock_pr)
        pr["reviews"] = [
            {
                "author": {"login": "independent-reviewer"},
                "state": "APPROVED",
                "submittedAt": "2026-09-05T08:15:00Z",
                "commit": None,
            }
        ]

        result = evaluate_pr_gate(pr)
        self.assertEqual(result.approval_verdict, "UNAPPROVED")
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertIsNone(result.approved_by)
        print(f"  [PASS] Unpinned approval rejected: {result.verdict_reason}")

        # An approval pinned to some other commit is equally worthless for this head.
        pr_other = copy.deepcopy(pr)
        pr_other["reviews"][0]["commit"] = {"oid": "7" * 40}
        other = evaluate_pr_gate(pr_other)
        self.assertEqual(other.approval_verdict, "UNAPPROVED")
        print("  [PASS] Approval pinned to a foreign commit rejected")

    # -------------------------------------------------------------------------
    # TEST 16: PR reference parsing for the documented CLI contract
    # -------------------------------------------------------------------------
    def test_parse_pr_ref_accepts_number_and_url(self):
        print("\n--- TEST 16: CLI PR Reference Parsing ---")
        self.assertEqual(parse_pr_ref("74"), (74, None))
        self.assertEqual(
            parse_pr_ref("https://github.com/Wladefant/super-board/pull/74"),
            (74, "Wladefant/super-board"),
        )
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_pr_ref("not-a-pr")
        print("  [PASS] Number, URL, and invalid reference all handled")

    # -------------------------------------------------------------------------
    # TEST 17: Approval policy is per repo/base, and production cannot be relaxed
    # -------------------------------------------------------------------------
    def test_approval_policy_resolution_and_production_guard(self):
        print("\n--- TEST 17: Configurable Approval Policy & Production Guard ---")
        self.assertFalse(
            resolve_gate_policy("Bavariance/polysimulator", "staging").require_github_approval
        )
        self.assertFalse(resolve_gate_policy("Wladefant/super-board", "main").require_github_approval)
        self.assertFalse(resolve_gate_policy("Wladefant/veyyon", "main").require_github_approval)
        self.assertTrue(resolve_gate_policy("Wladefant/veyyon", "staging").require_github_approval)
        self.assertTrue(resolve_gate_policy("Wladefant/veyyon", "feature-branch").require_github_approval)
        # Production base and unknown repositories stay strict.
        self.assertTrue(resolve_gate_policy("Bavariance/polysimulator", "main").require_github_approval)
        self.assertTrue(resolve_gate_policy("some/other-repo", "staging").require_github_approval)
        # Waiving the button never waives review evidence.
        self.assertTrue(
            resolve_gate_policy("Bavariance/polysimulator", "staging").require_head_bound_review_evidence
        )
        print("  [PASS] Policy resolves per repo/base; defaults stay strict")

        # A config file may not relax a production-protected base.
        tmp_dir = tempfile.mkdtemp(prefix="gate_policy_")
        try:
            cfg = os.path.join(tmp_dir, "policy.json")
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "policies": [
                            {
                                "repo": "Bavariance/polysimulator",
                                "base_ref": "main",
                                "require_github_approval": False,
                            }
                        ]
                    },
                    f,
                )
            forced = resolve_gate_policy("Bavariance/polysimulator", "main", config_path=cfg)
            self.assertTrue(forced.require_github_approval)
            self.assertIn("Refused to waive approval", forced.rationale)
            print(f"  [PASS] Production relaxation refused: {forced.rationale[:60]}...")

            # A non-production base may be configured freely.
            cfg2 = os.path.join(tmp_dir, "policy2.json")
            with open(cfg2, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "policies": [
                            {
                                "repo": "some/other-repo",
                                "base_ref": "staging",
                                "require_github_approval": False,
                            }
                        ]
                    },
                    f,
                )
            self.assertFalse(
                resolve_gate_policy("some/other-repo", "staging", config_path=cfg2).require_github_approval
            )
            print("  [PASS] Non-production base configurable via policy file")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # TEST 18: Waived GitHub approval still demands head-bound review evidence
    # -------------------------------------------------------------------------
    def test_waived_approval_still_requires_head_bound_review_evidence(self):
        print("\n--- TEST 18: Waived GitHub Approval Is Not A Free Pass ---")
        waived = GateApprovalPolicy(
            repo="Bavariance/polysimulator",
            base_ref="staging",
            require_github_approval=False,
            require_head_bound_review_evidence=True,
        )

        # No review of any kind: must still BLOCK.
        bare = copy.deepcopy(self.mock_pr)
        bare["reviews"] = []
        bare["baseRefName"] = "staging"
        blocked = evaluate_pr_gate(bare, policy=waived)
        self.assertEqual(blocked.gate_verdict, "BLOCKED")
        self.assertFalse(blocked.github_approval_required)
        self.assertIn("no", blocked.verdict_reason.lower())
        self.assertIn("independent head-bound review evidence", blocked.verdict_reason)
        print(f"  [PASS] No review evidence still blocked: {blocked.verdict_reason[:70]}...")

        # Author's own review must never satisfy it.
        selfrev = copy.deepcopy(self.mock_pr)
        selfrev["baseRefName"] = "staging"
        selfrev["reviews"] = [
            {
                "author": {"login": "feature-developer"},
                "state": "APPROVED",
                "submittedAt": "2026-09-05T08:15:00Z",
                "commit": {"oid": self.head_sha},
            }
        ]
        self_only = evaluate_pr_gate(selfrev, policy=waived)
        self.assertEqual(self_only.gate_verdict, "BLOCKED")
        self.assertEqual(self_only.approval_verdict, "SELF_APPROVED_ONLY")
        print("  [PASS] Self-approval rejected even with approval waived")

        # A local artifact alone cannot clear the installed content gate.
        ok = copy.deepcopy(bare)
        artifact = self.make_review_artifact()
        without_native = evaluate_pr_gate(ok, policy=waived, review_artifact=artifact)
        self.assertEqual(without_native.gate_verdict, "BLOCKED")
        ok["reviews"] = [{"author": {"login": "independent-reviewer"}, "state": "COMMENTED",
                          "body": "APPROVE", "commit": {"oid": self.head_sha}}]
        passed = evaluate_pr_gate(ok, policy=waived)
        self.assertEqual(passed.gate_verdict, "PASSED")
        self.assertEqual(passed.approval_verdict, "AUTOMATED_REVIEW_APPROVED")
        self.assertIn("review matches current content", passed.verdict_reason)

        # The same PR with no reviews under the strict default is blocked for approval.
        strict = evaluate_pr_gate(bare, policy=GateApprovalPolicy())
        self.assertEqual(strict.gate_verdict, "BLOCKED")
        self.assertTrue(strict.github_approval_required)
        print("  [PASS] Strict default unchanged")

    def test_review_artifact_rejects_malformed_stale_author_and_changes_requested(self):
        pr = copy.deepcopy(self.mock_pr)
        pr["reviews"] = []
        pr["baseRefName"] = "staging"
        waived = self.waived_policy()

        invalid_records = [
            {"status": "approved", "approved_by": "somebody"},
            dict(self.make_review_artifact(), source=None),
            self.make_review_artifact(head_sha="b" * 40),
            self.make_review_artifact(base_sha="b" * 40),
            self.make_review_artifact(reviewer="feature-developer"),
            self.make_review_artifact(source_uri="https://example.invalid/review"),
        ]
        for record in invalid_records:
            with self.subTest(record=record):
                result = evaluate_pr_gate(pr, policy=waived, review_artifact=record)
                self.assertEqual(result.gate_verdict, "BLOCKED")
                self.assertTrue(result.review_invalidated)

        changes_requested = self.make_review_artifact(outcome="changes_requested")
        result = evaluate_pr_gate(pr, policy=waived, review_artifact=changes_requested)
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertIn("changes requested", result.verdict_reason.lower())

        github_changes = copy.deepcopy(pr)
        github_changes["reviews"] = [
            {
                "author": {"login": "independent-reviewer"},
                "state": "CHANGES_REQUESTED",
                "submittedAt": "2026-09-05T08:15:00Z",
                "commit": {"oid": self.head_sha},
            }
        ]
        result = evaluate_pr_gate(github_changes, policy=waived)
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertIn("changes requested", result.verdict_reason.lower())
        self.assertNotEqual(result.approval_verdict, "APPROVED")

    def test_review_artifact_rejects_future_timestamp_and_accepts_current_or_past(self):
        pr = copy.deepcopy(self.mock_pr)
        pr["reviews"] = []
        pr["baseRefName"] = "staging"
        now = datetime.datetime.now(datetime.timezone.utc)

        current = self.make_review_artifact()
        current["submitted_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        current_result = evaluate_pr_gate(
            pr, policy=self.waived_policy(), review_artifact=current
        )
        self.assertEqual(current_result.gate_verdict, "BLOCKED")
        self.assertFalse(current_result.review_invalidated)

        past_result = evaluate_pr_gate(
            pr,
            policy=self.waived_policy(),
            review_artifact=self.make_review_artifact(),
        )
        self.assertEqual(past_result.gate_verdict, "BLOCKED")
        self.assertFalse(past_result.review_invalidated)

        future = self.make_review_artifact()
        future["submitted_at"] = (
            now + datetime.timedelta(minutes=5)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        future_result = evaluate_pr_gate(
            pr, policy=self.waived_policy(), review_artifact=future
        )
        self.assertEqual(future_result.gate_verdict, "BLOCKED")
        self.assertTrue(future_result.review_invalidated)
        self.assertIn("is in the future", future_result.verdict_reason)

    # -------------------------------------------------------------------------
    # TEST 19: Advisory vs blocking checks; absent native data never drops CI
    # -------------------------------------------------------------------------
    def test_advisory_checks_versus_native_required_contexts(self):
        print("\n--- TEST 19: Advisory Checks Never Become A Blanket CI Drop ---")
        pr = copy.deepcopy(self.mock_pr)
        pr["baseRefName"] = "main"
        pr["statusCheckRollup"] = [
            {"name": "unit-tests", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {"name": "claudex PowerShell fixtures", "status": "COMPLETED", "conclusion": "FAILURE",
             "completedAt": "2026-09-05T08:00:00Z"},
        ]

        # No policy exemption and no native data: the failure blocks.
        strict = evaluate_pr_gate(pr, policy=GateApprovalPolicy(require_github_approval=False))
        self.assertEqual(strict.ci_verdict, "FAILURE")
        self.assertIn("claudex PowerShell fixtures", strict.failing_checks)
        self.assertEqual(strict.advisory_failing_checks, [])
        print("  [PASS] Absent native required-check data blocks, never drops")

        # Explicitly declaring it advisory makes it non-blocking but still reported.
        advisory_policy = GateApprovalPolicy(
            require_github_approval=False,
            advisory_checks=["claudex *"],
        )
        advisory = evaluate_pr_gate(pr, policy=advisory_policy)
        self.assertEqual(advisory.ci_verdict, "SUCCESS")
        self.assertEqual(advisory.failing_checks, [])
        self.assertIn("claudex PowerShell fixtures", advisory.advisory_failing_checks)
        self.assertEqual(advisory.gate_verdict, "PASSED")
        self.assertIn("Advisory (non-blocking) failures", advisory.verdict_reason)
        print("  [PASS] Declared-advisory failure reported but non-blocking")

        # Native required contexts win: a failure outside them is advisory.
        native = evaluate_pr_gate(
            pr,
            policy=GateApprovalPolicy(require_github_approval=False),
            native_required_contexts=["unit-tests"],
        )
        self.assertEqual(native.failing_checks, [])
        self.assertIn("claudex PowerShell fixtures", native.advisory_failing_checks)
        self.assertEqual(native.native_required_contexts, ["unit-tests"])
        print("  [PASS] Native required contexts govern when GitHub supplies them")

        # A failure that IS a native required context still blocks.
        required = evaluate_pr_gate(
            pr,
            policy=GateApprovalPolicy(require_github_approval=False),
            native_required_contexts=["unit-tests", "claudex PowerShell fixtures"],
        )
        self.assertEqual(required.ci_verdict, "FAILURE")
        self.assertEqual(required.gate_verdict, "BLOCKED")
        print("  [PASS] Native required failure still blocks")

    def test_executable_cli_review_record_boundary(self):
        """Exercise --review-record through a real process and a labelled gh fixture."""
        head_sha = self.head_sha
        base_sha = self.base_sha
        fixture_pr = {
            "number": 74,
            "state": "OPEN",
            "isDraft": False,
            "headRefOid": head_sha,
            "baseRefName": "main",
            "author": {"login": "fixture-change-author"},
            "statusCheckRollup": [
                {"name": "fixture-ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
            ],
            "reviews": [],
        }
        script_path = os.path.abspath(os.path.join(SCRIPT_DIR, "github_pr_gate.py"))

        with tempfile.TemporaryDirectory(prefix="gate_cli_fixture_") as tmp_dir:
            pr_path = os.path.join(tmp_dir, "pr.json")
            with open(pr_path, "w", encoding="utf-8") as fixture_file:
                json.dump(fixture_pr, fixture_file)

            gh_fixture = os.path.join(tmp_dir, "gh_fixture.py")
            with open(gh_fixture, "w", encoding="utf-8") as fixture_file:
                fixture_file.write(
                    "import json, os, sys\n"
                    "args = sys.argv[1:]\n"
                    "if args[:2] == ['pr', 'view']:\n"
                    "    print(open(os.environ['GATE_FIXTURE_PR'], encoding='utf-8').read())\n"
                    "    raise SystemExit(0)\n"
                    "if args and args[0] == 'api' and '/reviews' in args[1]:\n"
                    "    print(json.dumps(json.load(open(os.environ['GATE_FIXTURE_PR'], encoding='utf-8'))['reviews']))\n"
                    "    raise SystemExit(0)\n"
                    "if args and args[0] == 'api' and '/comments' in args[1]:\n"
                    "    print(json.dumps(json.load(open(os.environ['GATE_FIXTURE_PR'], encoding='utf-8')).get('comments', [])))\n"
                    "    raise SystemExit(0)\n"
                    "if args and args[0] == 'api' and '/pulls/74' in args[1]:\n"
                    "    print(os.environ['GATE_FIXTURE_BASE'])\n"
                    "    raise SystemExit(0)\n"
                    "raise SystemExit(1)\n"
                )

            if sys.platform == "win32":
                gh_path = os.path.join(tmp_dir, "gh.cmd")
                with open(gh_path, "w", encoding="utf-8") as gh_file:
                    gh_file.write(
                        f'@echo off\r\n"{sys.executable}" "{gh_fixture}" %*\r\n'
                    )
            else:
                gh_path = os.path.join(tmp_dir, "gh")
                with open(gh_path, "w", encoding="utf-8") as gh_file:
                    gh_file.write(f'#!/bin/sh\nexec "{sys.executable}" "{gh_fixture}" "$@"\n')
                os.chmod(gh_path, 0o755)

            env = os.environ.copy()
            env["PATH"] = tmp_dir + os.pathsep + env.get("PATH", "")
            env["GATE_FIXTURE_PR"] = pr_path
            env["GATE_FIXTURE_BASE"] = base_sha
            record_path = os.path.join(tmp_dir, "review.json")

            def run_cli(record):
                with open(record_path, "w", encoding="utf-8") as record_file:
                    json.dump(record, record_file)
                return subprocess.run(
                    [
                        sys.executable,
                        script_path,
                        "--pr",
                        "https://github.com/Wladefant/super-board/pull/74",
                        "--head-sha",
                        head_sha,
                        "--review-record",
                        record_path,
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=20,
                )

            valid = self.make_review_artifact(
                head_sha=head_sha,
                base_sha=base_sha,
                repository="Wladefant/super-board",
                pull_request=74,
                author="fixture-change-author",
                reviewer="fixture-independent-reviewer",
            )
            completed = run_cli(valid)
            self.assertEqual(completed.returncode, 2, completed.stderr or completed.stdout)
            self.assertEqual(json.loads(completed.stdout)["gate_verdict"], "BLOCKED")

            invalid_records = [
                {"status": "approved", "approved_by": "fabricated"},
                dict(valid, source=None),
                dict(valid, head_sha="3" * 40),
                dict(valid, base_sha="3" * 40),
                self.make_review_artifact(
                    head_sha=head_sha,
                    base_sha=base_sha,
                    repository="Wladefant/super-board",
                    pull_request=74,
                    author="fixture-change-author",
                    reviewer="fixture-change-author",
                ),
            ]
            for record in invalid_records:
                rejected = run_cli(record)
                self.assertEqual(rejected.returncode, 2, rejected.stderr or rejected.stdout)
                self.assertEqual(json.loads(rejected.stdout)["gate_verdict"], "BLOCKED")
            # Positive control: authenticated native review metadata and real
            # git content clear the same executable CLI, without a local artifact.
            fixture_pr["reviews"] = [{
                "user": {"login": "fixture-independent-reviewer"}, "state": "APPROVED",
                "body": "APPROVE", "commit_id": head_sha,
            }]
            with open(pr_path, "w", encoding="utf-8") as fixture_file:
                json.dump(fixture_pr, fixture_file)
            passed = run_cli(valid)
            self.assertEqual(passed.returncode, 0, passed.stderr or passed.stdout)
            self.assertEqual(json.loads(passed.stdout)["approval_verdict"], "APPROVED")

    # -------------------------------------------------------------------------
    # TEST 11: GitHub access failure is an explicit error, never approval
    # -------------------------------------------------------------------------
    def test_gh_access_failure_is_not_approval(self):
        denied = subprocess.CompletedProcess(["gh"], 1, "", "HTTP 403: access denied")
        with patch("github_pr_gate._run_gh", return_value=denied):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                fetch_pr_json(pr_number=1, repo="example/fixture")

    # -------------------------------------------------------------------------
    # TEST 20: Risk-Based Review Exemption & High-Risk Requirements (Issue #195)
    # -------------------------------------------------------------------------
    def test_exempt_small_ui_pr_passes_without_review(self):
        """Exempt small UI PR (<50 lines, no high-risk labels/paths) passes gate without review."""
        pr = copy.deepcopy(self.mock_pr)
        pr["reviews"] = []
        pr["baseRefName"] = "staging"
        pr["labels"] = [{"name": "area:ui"}]
        pr["files"] = [
            {"path": "frontend/components/Navbar.tsx", "additions": 30, "deletions": 10}
        ]
        policy = self.staging_policy()
        pr["comments"] = [self.qa_receipt_comment()]
        result = evaluate_pr_gate(pr, policy=policy)
        self.assertEqual(result.review_decision, "exempt")
        self.assertEqual(result.review_decision_reason, "40 lines, no high-risk paths")
        self.assertEqual(result.decision_line, "review: exempt (40 lines, no high-risk paths)")
        self.assertEqual(result.gate_verdict, "PASSED")
        self.assertIn("independent review is exempt", result.verdict_reason)
        print("  [PASS] Exempt small UI PR passes without review")

    def test_300_line_pr_requires_review(self):
        """300-line PR (>250 lines changed) requires review: blocked without review, passes with review."""
        pr = copy.deepcopy(self.mock_pr)
        pr["reviews"] = []
        pr["baseRefName"] = "staging"
        pr["labels"] = []
        pr["files"] = [
            {"path": "frontend/components/DataTable.tsx", "additions": 200, "deletions": 100}
        ]
        policy = self.staging_policy()
        pr["comments"] = [self.qa_receipt_comment()]
        # Without review: BLOCKED
        blocked = evaluate_pr_gate(pr, policy=policy)
        self.assertEqual(blocked.review_decision, "required")
        self.assertEqual(blocked.review_decision_reason, "300 lines changed > 250")
        self.assertEqual(blocked.decision_line, "review: required (300 lines changed > 250)")
        self.assertEqual(blocked.gate_verdict, "BLOCKED")
        self.assertIn("review required: 300 lines changed > 250", blocked.verdict_reason)

        # With independent review: PASSED
        pr["reviews"] = [
            {
                "author": {"login": "independent-reviewer"},
                "state": "APPROVED",
                "submittedAt": "2026-09-05T08:15:00Z",
                "commit": {"oid": self.head_sha},
            }
        ]
        passed = evaluate_pr_gate(pr, policy=policy)
        self.assertEqual(passed.gate_verdict, "PASSED")
        self.assertEqual(passed.review_decision, "required")
        print("  [PASS] 300-line PR requires review (blocked without review, passes with review)")

    def test_10_line_alembic_migration_requires_review(self):
        """10-line alembic migration requires review despite small line count."""
        pr = copy.deepcopy(self.mock_pr)
        pr["reviews"] = []
        pr["baseRefName"] = "staging"
        pr["labels"] = []
        pr["files"] = [
            {"path": "alembic/versions/20260923_001_add_index.py", "additions": 8, "deletions": 2}
        ]
        policy = GateApprovalPolicy(
            repo="Bavariance/polysimulator",
            base_ref="staging",
            require_github_approval=False,
            require_head_bound_review_evidence=True,
            allow_review_exemption=True,
        )
        result = evaluate_pr_gate(pr, policy=policy)
        self.assertEqual(result.review_decision, "required")
        self.assertEqual(
            result.review_decision_reason,
            "migration path alembic/versions/20260923_001_add_index.py",
        )
        self.assertEqual(
            result.decision_line,
            "review: required (migration path alembic/versions/20260923_001_add_index.py)",
        )
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertIn("migration path alembic/versions/20260923_001_add_index.py", result.verdict_reason)
        print("  [PASS] 10-line alembic migration requires review")

    def test_money_path_requires_review(self):
        """Money path (billing, wallet, ledger, payment, stripe) requires review."""
        for path in (
            "backend/app/billing/charge.py",
            "backend/app/models/wallet.py",
            "backend/app/ledger/balance.py",
            "backend/app/payment/stripe_webhook.py",
        ):
            with self.subTest(path=path):
                pr = copy.deepcopy(self.mock_pr)
                pr["reviews"] = []
                pr["baseRefName"] = "staging"
                pr["labels"] = []
                pr["files"] = [{"path": path, "additions": 5, "deletions": 2}]
                policy = GateApprovalPolicy(
                    repo="Bavariance/polysimulator",
                    base_ref="staging",
                    require_github_approval=False,
                    require_head_bound_review_evidence=True,
                    allow_review_exemption=True,
                )
                result = evaluate_pr_gate(pr, policy=policy)
                self.assertEqual(result.review_decision, "required")
                self.assertEqual(result.review_decision_reason, f"money path {path}")
                self.assertEqual(result.decision_line, f"review: required (money path {path})")
                self.assertEqual(result.gate_verdict, "BLOCKED")
        print("  [PASS] Money path requires review (billing, wallet, ledger, payment/stripe)")

    def test_lockfile_exclusion_and_high_risk_labels(self):
        """Lockfiles are excluded from changed lines count; risk:high and area labels require review."""
        # Lockfile exclusion: 1000 lines lockfile + 20 lines UI is exempt (< 250 non-lockfile lines)
        pr = copy.deepcopy(self.mock_pr)
        pr["reviews"] = []
        pr["baseRefName"] = "staging"
        pr["labels"] = []
        pr["files"] = [
            {"path": "package-lock.json", "additions": 800, "deletions": 200},
            {"path": "frontend/components/Button.tsx", "additions": 15, "deletions": 5},
        ]
        policy = self.staging_policy()
        pr["comments"] = [self.qa_receipt_comment()]
        result = evaluate_pr_gate(pr, policy=policy)
        self.assertEqual(result.review_decision, "exempt")
        self.assertEqual(result.review_decision_reason, "20 lines, no high-risk paths")
        self.assertEqual(result.gate_verdict, "PASSED")

        # label risk:high requires review even on 10 lines
        pr_risk = copy.deepcopy(self.mock_pr)
        pr_risk["reviews"] = []
        pr_risk["baseRefName"] = "staging"
        pr_risk["labels"] = [{"name": "risk:high"}]
        pr_risk["files"] = [{"path": "frontend/components/Button.tsx", "additions": 5, "deletions": 2}]
        pr_risk["comments"] = [self.qa_receipt_comment()]
        res_risk = evaluate_pr_gate(pr_risk, policy=policy)
        self.assertEqual(res_risk.review_decision, "required")
        self.assertEqual(res_risk.review_decision_reason, "high-risk label risk:high")
        self.assertEqual(res_risk.gate_verdict, "BLOCKED")
        self.assertEqual(res_risk.qa_receipt_verdict, "PASSED")

        # area:auth requires review even on 10 lines
        pr_auth = copy.deepcopy(self.mock_pr)
        pr_auth["reviews"] = []
        pr_auth["baseRefName"] = "staging"
        pr_auth["labels"] = [{"name": "area:auth"}]
        pr_auth["files"] = [{"path": "frontend/components/Button.tsx", "additions": 5, "deletions": 2}]
        pr_auth["comments"] = [self.qa_receipt_comment()]
        res_auth = evaluate_pr_gate(pr_auth, policy=policy)
        self.assertEqual(res_auth.review_decision, "required")
        self.assertEqual(res_auth.review_decision_reason, "high-risk area label area:auth")
        self.assertEqual(res_auth.gate_verdict, "BLOCKED")
        self.assertEqual(res_auth.qa_receipt_verdict, "PASSED")
        print("  [PASS] Lockfile exclusion and high-risk label tests pass")

    def test_review_exemption_is_scoped_and_fails_closed(self):
        """Strict default policy never exempts; a capped 100-file list never exempts."""
        pr = copy.deepcopy(self.mock_pr)
        pr["reviews"] = []
        pr["baseRefName"] = "main"
        pr["labels"] = []
        pr["files"] = [{"path": "src/ui/Button.tsx", "additions": 3, "deletions": 1}]
        strict = GateApprovalPolicy(rationale="strict default")
        res = evaluate_pr_gate(pr, policy=strict)
        self.assertEqual(res.review_decision, "required")
        self.assertEqual(res.gate_verdict, "BLOCKED")

        pr_many = copy.deepcopy(self.mock_pr)
        pr_many["reviews"] = []
        pr_many["baseRefName"] = "staging"
        pr_many["labels"] = []
        pr_many["files"] = [{"path": f"frontend/c{i}.tsx", "additions": 1, "deletions": 0} for i in range(100)]
        relaxed = GateApprovalPolicy(
            repo="Bavariance/polysimulator",
            base_ref="staging",
            require_github_approval=False,
            require_head_bound_review_evidence=True,
            allow_review_exemption=True,
        )
        res_many = evaluate_pr_gate(pr_many, policy=relaxed)
        self.assertEqual(res_many.review_decision, "required")
        self.assertIn("truncated", res_many.review_decision_reason)
        self.assertEqual(res_many.gate_verdict, "BLOCKED")
        print("  [PASS] Review exemption scoped to named policies and fails closed on truncation")
    # ── Deploy-critical check tests (incident #5535 prevention) ──────────
    #
    # Every fixture below is review-exempt under the REAL PolySimulator staging policy
    # (small low-risk diff, resolve_gate_policy), so the only thing that can hold the
    # gate is CI. The bare waived_policy() fixture does not opt into review exemption,
    # which makes a no-review PR BLOCKED for missing review evidence regardless of CI
    # and would let a PENDING/PASSED assertion pass or fail for the wrong reason.

    def _exempt_staging_pr(self, checks):
        """Return a review-exempt staging PR with the given status check rollup."""
        pr = copy.deepcopy(self.mock_pr)
        pr["statusCheckRollup"] = checks
        pr["baseRefName"] = "staging"
        pr["files"] = [{"path": "frontend/foo.tsx", "additions": 10, "deletions": 5}]
        pr["labels"] = []
        pr["reviews"] = []
        # The QA receipt is a given here: these fixtures isolate CI behaviour.
        pr["comments"] = [self.qa_receipt_comment()]
        return pr

    def _evaluate_staging(self, pr):
        policy = resolve_gate_policy("Bavariance/polysimulator", "staging")
        self.assertTrue(policy.allow_review_exemption)
        res = evaluate_pr_gate(pr, repo="Bavariance/polysimulator", policy=policy)
        self.assertEqual(res.review_decision, "exempt", res.review_decision_reason)
        return res

    def _six_min_ago(self):
        """Return an ISO timestamp 6 minutes in the past."""
        return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_deploy_critical_names_match(self):
        self.assertIn("build-and-boot", DEPLOY_CRITICAL_CHECKS)

    def test_deploy_critical_pending_never_passes(self):
        """build-and-boot pending for 6 minutes keeps the gate PENDING; it is never timed out."""
        six_min = self._six_min_ago()
        for status in ("QUEUED", "IN_PROGRESS", "PENDING"):
            with self.subTest(status=status):
                res = self._evaluate_staging(self._exempt_staging_pr([
                    {"name": "build-and-boot", "status": status, "conclusion": "", "startedAt": six_min, "createdAt": six_min},
                    {"name": "lint-and-typecheck", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": six_min},
                ]))
                self.assertEqual(res.ci_verdict, "PENDING")
                self.assertEqual(res.gate_verdict, "PENDING")
                self.assertEqual(res.pending_checks, ["build-and-boot"])
                self.assertNotIn("timed out", res.verdict_reason.lower())

    def test_deploy_critical_failure_blocks(self):
        """build-and-boot failure is a hard BLOCKED even on a review-exempt PR."""
        res = self._evaluate_staging(self._exempt_staging_pr([
            {"name": "build-and-boot", "status": "COMPLETED", "conclusion": "FAILURE", "completedAt": "2026-09-25T19:00:00Z"},
            {"name": "lint-and-typecheck", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-09-25T19:00:00Z"},
        ]))
        self.assertEqual(res.ci_verdict, "FAILURE")
        self.assertEqual(res.gate_verdict, "BLOCKED")
        self.assertIn("build-and-boot", res.failing_checks)

    def test_deploy_critical_success_passes(self):
        """build-and-boot success lets a review-exempt PR pass."""
        res = self._evaluate_staging(self._exempt_staging_pr([
            {"name": "build-and-boot", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-09-25T19:00:00Z"},
            {"name": "lint-and-typecheck", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": "2026-09-25T19:00:00Z"},
        ]))
        self.assertEqual(res.ci_verdict, "SUCCESS")
        self.assertEqual(res.gate_verdict, "PASSED")

    def test_non_critical_pending_times_out_but_critical_stays(self):
        """A lint pending 6 minutes times out, but a co-pending build-and-boot keeps the gate PENDING."""
        six_min = self._six_min_ago()
        res = self._evaluate_staging(self._exempt_staging_pr([
            {"name": "build-and-boot", "status": "IN_PROGRESS", "conclusion": "", "startedAt": six_min, "createdAt": six_min},
            {"name": "lint-and-typecheck", "status": "IN_PROGRESS", "conclusion": "", "startedAt": six_min, "createdAt": six_min},
        ]))
        self.assertEqual(res.ci_verdict, "PENDING")
        self.assertEqual(res.gate_verdict, "PENDING")
        self.assertEqual(res.pending_checks, ["build-and-boot"])
        self.assertIn("timed out", res.verdict_reason.lower())

    def test_only_non_critical_pending_times_out_and_passes(self):
        """An unrelated check pending 6 minutes times out and the gate passes on local gates."""
        six_min = self._six_min_ago()
        res = self._evaluate_staging(self._exempt_staging_pr([
            {"name": "build-and-boot", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": six_min},
            {"name": "lint-and-typecheck", "status": "IN_PROGRESS", "conclusion": "", "startedAt": six_min, "createdAt": six_min},
        ]))
        self.assertEqual(res.ci_verdict, "SUCCESS")
        self.assertEqual(res.gate_verdict, "PASSED")
        self.assertIn("lint-and-typecheck", res.verdict_reason)
        self.assertIn("timed out", res.verdict_reason.lower())

    def test_non_critical_pending_under_timeout_stays_pending(self):
        """A non-critical check pending under 5 minutes is still waited on."""
        recent = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        res = self._evaluate_staging(self._exempt_staging_pr([
            {"name": "build-and-boot", "status": "COMPLETED", "conclusion": "SUCCESS", "completedAt": recent},
            {"name": "lint-and-typecheck", "status": "IN_PROGRESS", "conclusion": "", "startedAt": recent, "createdAt": recent},
        ]))
        self.assertEqual(res.gate_verdict, "PENDING")
        self.assertEqual(res.pending_checks, ["lint-and-typecheck"])

    def test_pending_rerun_of_deploy_critical_supersedes_older_success(self):
        """A re-run build-and-boot (completedAt = GitHub's 0001 sentinel) outranks its older success."""
        six_min = self._six_min_ago()
        res = self._evaluate_staging(self._exempt_staging_pr([
            {"name": "build-and-boot", "status": "COMPLETED", "conclusion": "SUCCESS",
             "startedAt": "2026-09-25T18:00:00Z", "completedAt": "2026-09-25T18:10:00Z"},
            {"name": "build-and-boot", "status": "IN_PROGRESS", "conclusion": "",
             "startedAt": six_min, "completedAt": "0001-01-01T00:00:00Z"},
        ]))
        self.assertEqual(res.gate_verdict, "PENDING")
        self.assertEqual(res.pending_checks, ["build-and-boot"])

    def test_veyyon_main_waiver_author_comment_review(self):
        print("\n--- TEST 20: Veyyon Main Waiver & Negative Controls ---")
        pr_author = "feature-developer"
        base_pr = {
            "number": 130,
            "state": "OPEN",
            "isDraft": False,
            "headRefOid": self.head_sha,
            "baseRefOid": self.base_sha,
            "baseRefName": "main",
            "author": {"login": pr_author},
            "statusCheckRollup": [
                {
                    "name": "test-suite",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "completedAt": "2026-09-05T08:00:00Z",
                }
            ],
            # Review required by default (no file data or labels)
            "reviews": [],
        }

        # 1. POSITIVE TEST: author COMMENT review with APPROVE + matching content on veyyon@main passes
        passing_pr = copy.deepcopy(base_pr)
        passing_pr["reviews"] = [
            {
                "author": {"login": pr_author},
                "state": "COMMENTED",
                "body": f"APPROVE {self.head_sha}\n\nAutomated review against clean head.",
                "submittedAt": "2026-09-05T08:15:00Z",
            }
        ]
        result = evaluate_pr_gate(passing_pr, repo="Wladefant/veyyon")
        self.assertEqual(result.gate_verdict, "PASSED")
        self.assertEqual(result.approval_verdict, "AUTOMATED_REVIEW_APPROVED")
        self.assertEqual(result.approved_by, pr_author)
        self.assertFalse(result.github_approval_required)
        print("  [PASS] Author COMMENT with APPROVE + matching content on veyyon@main passes")

        # 2. NEGATIVE CONTROL: without a verdict does not pass (author COMMENT without verdict doesn't count)
        no_verdict_pr = copy.deepcopy(base_pr)
        no_verdict_pr["reviews"] = [
            {
                "author": {"login": pr_author},
                "state": "COMMENTED",
                "body": f"Reviewing commit {self.head_sha} - notes and comments without verdict.",
                "submittedAt": "2026-09-05T08:15:00Z",
            }
        ]
        res_no_verdict = evaluate_pr_gate(no_verdict_pr, repo="Wladefant/veyyon")
        self.assertEqual(res_no_verdict.gate_verdict, "BLOCKED")
        self.assertEqual(res_no_verdict.approval_verdict, "SELF_APPROVED_ONLY")
        print("  [PASS] Negative control: author COMMENT without verdict blocked")

        # 3. NEGATIVE CONTROL: other content (mismatched SHA / diff) does not pass
        mismatched_content_pr = copy.deepcopy(base_pr)
        mismatched_content_pr["reviews"] = [
            {
                "author": {"login": pr_author},
                "state": "COMMENTED",
                "body": f"APPROVE {self.base_sha}\n\nReviewed base commit, not head.",
                "submittedAt": "2026-09-05T08:15:00Z",
            }
        ]
        res_mismatched = evaluate_pr_gate(mismatched_content_pr, repo="Wladefant/veyyon")
        self.assertEqual(res_mismatched.gate_verdict, "BLOCKED")
        self.assertNotEqual(res_mismatched.gate_verdict, "PASSED")
        print("  [PASS] Negative control: author review for other content blocked")

        # 4. NEGATIVE CONTROL: another veyyon branch (not main) does not pass
        other_branch_pr = copy.deepcopy(base_pr)
        other_branch_pr["baseRefName"] = "feature-branch"
        other_branch_pr["reviews"] = [
            {
                "author": {"login": pr_author},
                "state": "COMMENTED",
                "body": f"APPROVE {self.head_sha}\n\nAutomated review.",
                "submittedAt": "2026-09-05T08:15:00Z",
            }
        ]
        res_other_branch = evaluate_pr_gate(other_branch_pr, repo="Wladefant/veyyon")
        self.assertEqual(res_other_branch.gate_verdict, "BLOCKED")
        self.assertTrue(res_other_branch.github_approval_required)
        print("  [PASS] Negative control: author review on non-main veyyon branch blocked")

        # 5. NEGATIVE CONTROL: another repo does not pass
        other_repo_pr = copy.deepcopy(base_pr)
        other_repo_pr["reviews"] = [
            {
                "author": {"login": pr_author},
                "state": "COMMENTED",
                "body": f"APPROVE {self.head_sha}\n\nAutomated review.",
                "submittedAt": "2026-09-05T08:15:00Z",
            }
        ]
        res_other_repo = evaluate_pr_gate(other_repo_pr, repo="other-org/other-repo")
        self.assertEqual(res_other_repo.gate_verdict, "BLOCKED")
        self.assertTrue(res_other_repo.github_approval_required)
        print("  [PASS] Negative control: author review on another repo blocked")

    # -------------------------------------------------------------------------
    # TEST 21: Browser QA receipt on staging UI / order-trading diffs
    # -------------------------------------------------------------------------
    def test_staging_ui_pr_without_qa_receipt_is_blocked(self):
        """Negative control: green CI and an exempt review still never pass a UI diff with no QA receipt."""
        result = evaluate_pr_gate(self.staging_ui_pr(comments=[]), policy=self.staging_policy())
        self.assertEqual(result.ci_verdict, "SUCCESS")
        self.assertEqual(result.review_decision, "exempt")
        self.assertEqual(result.qa_receipt_verdict, "REQUIRED")
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertIsNone(result.qa_receipt_url)
        self.assertIn("QA receipt required (UI path frontend/components/OrderTicket.tsx)", result.verdict_reason)
        self.assertIn("no PR comment carries a 'QA-RECEIPT: PASS' marker", result.verdict_reason)
        print("  [PASS] UI diff without a QA receipt is BLOCKED despite green CI and exempt review")

    def test_staging_ui_pr_with_qa_receipt_passes(self):
        """Positive control: the same diff passes once a receipt binds it and shows two images."""
        receipt = self.qa_receipt_comment()
        result = evaluate_pr_gate(self.staging_ui_pr(receipt), policy=self.staging_policy())
        self.assertEqual(result.qa_receipt_verdict, "PASSED")
        self.assertEqual(result.qa_receipt_url, self.QA_RECEIPT_URL)
        self.assertEqual(result.gate_verdict, "PASSED")
        self.assertIn("Browser QA receipt: PASSED", result.verdict_reason)
        self.assertEqual(result.to_dict()["qa_receipt_verdict"], "PASSED")
        self.assertIn("Browser QA", result.to_compact_markdown())
        print("  [PASS] UI diff with a head-bound QA receipt passes and reports the receipt URL")

    def test_qa_receipt_marker_and_image_count_are_required(self):
        """Negative controls: no marker, and fewer than two rendered images, each stay BLOCKED."""
        cases = [
            (self.qa_receipt_comment(marker=None), "no PR comment carries a 'QA-RECEIPT: PASS' marker"),
            (self.qa_receipt_comment(images=0), "0 github.com/user-attachments image(s), 2 required"),
            (self.qa_receipt_comment(images=1), "1 github.com/user-attachments image(s), 2 required"),
        ]
        for comment, expected in cases:
            with self.subTest(expected=expected):
                result = evaluate_pr_gate(self.staging_ui_pr(comments=[comment]), policy=self.staging_policy())
                self.assertEqual(result.qa_receipt_verdict, "REQUIRED")
                self.assertEqual(result.gate_verdict, "BLOCKED")
                self.assertIn(expected, result.verdict_reason)
        # An image on a prohibited host is not evidence, however many are pasted.
        raw = "![a](https://raw.githubusercontent.com/o/r/deadbeef/shot.png)"
        result = evaluate_pr_gate(
            self.staging_ui_pr(comments=[self.qa_receipt_comment(images=0, extra=raw)]),
            policy=self.staging_policy(),
        )
        self.assertEqual(result.qa_receipt_verdict, "REQUIRED")
        self.assertIn("0 github.com/user-attachments image(s)", result.verdict_reason)
        print("  [PASS] Negative controls: marker, image count and image host all enforced")

    def test_qa_receipt_for_another_revision_is_blocked(self):
        """Negative control: a receipt written before a further edit never binds the new head."""
        stale = self.qa_receipt_comment(named="a" * 40, identity="b" * 40)
        result = evaluate_pr_gate(self.staging_ui_pr(comments=[stale]), policy=self.staging_policy())
        self.assertEqual(result.qa_receipt_verdict, "REQUIRED")
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertIn("names no identity for this head", result.verdict_reason)
        # The same receipt becomes valid once it names the live head.
        self.assertEqual(
            evaluate_pr_gate(
                self.staging_ui_pr(comments=[self.qa_receipt_comment()]), policy=self.staging_policy()
            ).qa_receipt_verdict,
            "PASSED",
        )
        print("  [PASS] Negative control: receipt bound to another revision is BLOCKED")

    def test_qa_receipt_served_from_another_revision_is_blocked(self):
        """Negative control: a PASS receipt for the served revision is missing evidence, not a pass."""
        from review_content import content_identity

        patch_id, _ = content_identity(self.head_sha, "origin/staging")
        # The marker line decides. QA ran against `served`, so the head SHA printed in the same
        # comment (the printer's "Commit SHA" field) is not evidence for the diff under review:
        # a deploy that lags the head was never the thing QA exercised.
        stale = self.qa_receipt_comment(served="c" * 40)
        result = evaluate_pr_gate(self.staging_ui_pr(comments=[stale]), policy=self.staging_policy())
        self.assertEqual(result.qa_receipt_verdict, "REQUIRED")
        self.assertEqual(result.gate_verdict, "BLOCKED")
        self.assertIn("names no identity for this head", result.verdict_reason)
        # Served from the head itself, or from the same content at an older head, it passes.
        for served in (self.head_sha, patch_id):
            with self.subTest(served=served[:12]):
                case = self.staging_ui_pr(comments=[self.qa_receipt_comment(served=served)])
                self.assertEqual(
                    evaluate_pr_gate(case, policy=self.staging_policy()).qa_receipt_verdict, "PASSED"
                )
        # A FAIL marker beside the head still requires QA.
        failed = self.staging_ui_pr(comments=[self.qa_receipt_comment(marker="FAIL", served=self.head_sha)])
        self.assertEqual(
            evaluate_pr_gate(failed, policy=self.staging_policy()).qa_receipt_verdict, "REQUIRED"
        )
        print("  [PASS] Negative control: receipt served from another revision is BLOCKED")

    def test_qa_receipt_accepts_content_identity_and_review_bodies(self):
        """A receipt may name the patch-id (survives a sync merge) and may live in a review body."""
        from review_content import content_identity

        patch_id, digest = content_identity(self.head_sha, "origin/staging")
        self.assertTrue(patch_id)
        for identity in (patch_id, digest):
            with self.subTest(identity=identity[:12]):
                receipt = self.qa_receipt_comment(identity=identity)
                pr = self.staging_ui_pr(comments=[receipt])
                self.assertEqual(
                    evaluate_pr_gate(pr, policy=self.staging_policy()).qa_receipt_verdict, "PASSED"
                )
        as_review = self.staging_ui_pr(comments=[])
        as_review["reviews"] = [{
            "author": {"login": "qa-lane"},
            "state": "COMMENTED",
            "body": self.qa_receipt_comment()["body"],
        }]
        result = evaluate_pr_gate(as_review, policy=self.staging_policy())
        self.assertEqual(result.qa_receipt_verdict, "PASSED")
        print("  [PASS] Receipt accepted via patch-id, diff sha256 and review body")

    def test_order_trading_paths_require_a_qa_receipt(self):
        """Every order/trading backend path demands browser QA; unrelated paths do not."""
        for path in (
            "backend/app/api_v1/orders.py",
            "backend/app/matching_engine.py",
            "backend/app/order_pipeline/pipeline.py",
            "backend/app/api_v1/settlement.py",
        ):
            with self.subTest(path=path):
                result = evaluate_pr_gate(
                    self.staging_ui_pr(files=[{"path": path, "additions": 3, "deletions": 1}]),
                    policy=self.staging_policy(),
                )
                self.assertEqual(result.qa_receipt_verdict, "REQUIRED")
                self.assertEqual(result.gate_verdict, "BLOCKED")
                self.assertIn(f"order/trading path {path}", result.qa_receipt_reason)
        for path in (
            "backend/app/api_v1/markets.py",
            "backend/app/models/user.py",
            "docs/runbook.md",
            # Tests ship nothing, so a test-only diff must not demand browser QA,
            # however loudly its filename names orders or settlement.
            "backend/tests/test_orders.py",
            "backend/tests/test_market_detail_stale_settlement.py",
            "frontend/components/__tests__/OrderTicket.test.tsx",
            "frontend/components/OrderTicket.spec.tsx",
        ):
            with self.subTest(exempt=path):
                result = evaluate_pr_gate(
                    self.staging_ui_pr(files=[{"path": path, "additions": 3, "deletions": 1}]),
                    policy=self.staging_policy(),
                )
                self.assertEqual(result.qa_receipt_verdict, "EXEMPT", result.qa_receipt_reason)
                self.assertEqual(result.gate_verdict, "PASSED")
        # A diff that ships code as well as tests still triggers on the code.
        mixed = evaluate_pr_gate(
            self.staging_ui_pr(files=[
                {"path": "backend/tests/test_orders.py", "additions": 4, "deletions": 1},
                {"path": "backend/app/api_v1/orders.py", "additions": 2, "deletions": 0},
            ]),
            policy=self.staging_policy(),
        )
        self.assertEqual(mixed.qa_receipt_verdict, "REQUIRED")
        self.assertIn("order/trading path backend/app/api_v1/orders.py", mixed.qa_receipt_reason)
        print("  [PASS] Order/trading paths require QA; test-only and unrelated paths stay exempt")

    def test_qa_receipt_scope_is_staging_only_and_fails_closed_when_truncated(self):
        """A capped 100-file list cannot prove a diff is UI-free; another repo is out of scope."""
        many = [
            {"path": f"backend/app/api_v1/module_{i}.py", "additions": 1, "deletions": 0}
            for i in range(100)
        ]
        result = evaluate_pr_gate(
            self.staging_ui_pr(files=many, comments=[]), policy=self.staging_policy()
        )
        self.assertEqual(result.qa_receipt_verdict, "REQUIRED")
        self.assertIn("truncated at 100 files", result.qa_receipt_reason)
        self.assertEqual(result.gate_verdict, "BLOCKED")

        pr = self.staging_ui_pr(comments=[])
        pr["baseRefName"] = "main"
        main_result = evaluate_pr_gate(
            pr,
            policy=GateApprovalPolicy(
                repo="Bavariance/polysimulator",
                base_ref="main",
                require_github_approval=False,
                require_head_bound_review_evidence=True,
                allow_review_exemption=True,
            ),
        )
        self.assertEqual(main_result.qa_receipt_verdict, "EXEMPT")
        self.assertIn("no QA receipt requirement for Bavariance/polysimulator@main", main_result.qa_receipt_reason)
        print("  [PASS] QA receipt requirement is staging-scoped and fails closed on a truncated file list")

def main():
    print("=" * 70)
    print("RUNNING DETERMINISTIC GITHUB PR GATE TEST SUITE")
    print("=" * 70)
    suite = unittest.TestLoader().loadTestsFromTestCase(TestGitHubPRGate)
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("\n" + "=" * 70)
        print(f"ALL {result.testsRun} PR GATE TESTS PASSED")
        print("=" * 70)
        sys.exit(0)
    else:
        print("\n" + "=" * 70)
        print(f"TESTS FAILED: {len(result.failures)} failed, {len(result.errors)} errored")
        print("=" * 70)
        sys.exit(1)


if __name__ == "__main__":
    main()
