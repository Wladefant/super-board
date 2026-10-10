#!/usr/bin/env python3
"""Targeted unit tests for Post-Deploy QA Orchestrator (workflows/portable/test_post_deploy_qa.py).

Covers:
  - Revert hint: on failure, prints exact git revert -m 1 MERGE, git push origin BASE, and deploy cmd;
    never runs revert automatically; marks project card Blocked.
  - Production host refusal: refuses PolySimulator production hosts directly and via redirect targets.
  - Served mismatch: detects served revision differing from expected merge SHA.
  - Dry run non-mutation: contacts version endpoint, reports plan without browser, posts, or mutation.
  - Explicit identifiers: requires expected SHA, deploy command, base branch, PR, and issue.
"""

from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
import subprocess
import unittest
from unittest.mock import MagicMock, call, patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import post_deploy_qa

class TestPostDeployQARevertHint(unittest.TestCase):
    """Test revert instructions, deploy command inclusion, and Project card blocking on failure."""

    def test_revert_hint_output_format(self):
        merge_sha = "1234567890abcdef1234567890abcdef12345678"
        base_branch = "main"
        deploy_cmd = "dokploy deploy pinthread-staging"

        instructions = post_deploy_qa.format_revert_instructions(
            merge_sha=merge_sha,
            base_branch=base_branch,
            deploy_cmd=deploy_cmd,
            reason="FLOW-QA assertion failure",
        )

        expected_revert = f"git revert -m 1 {merge_sha}"
        expected_push = f"git push origin {base_branch}"

        self.assertIn(expected_revert, instructions)
        self.assertIn(expected_push, instructions)
        self.assertIn(deploy_cmd, instructions)
        self.assertIn("FLOW-QA assertion failure", instructions)

    @patch("post_deploy_qa.update_project_card_status")
    @patch("post_deploy_qa.post_github_comment")
    @patch("post_deploy_qa.run_flow_qa")
    @patch("post_deploy_qa.fetch_served_sha")
    def test_failure_marks_card_blocked_and_prints_revert(
        self, mock_fetch_sha, mock_run_flow_qa, mock_post_comment, mock_update_card
    ):
        merge_sha = "1234567890abcdef1234567890abcdef12345678"
        base_branch = "staging"
        deploy_cmd = "deploy-staging.sh"
        mock_fetch_sha.return_value = merge_sha
        mock_run_flow_qa.return_value = {
            "passed": False,
            "receipt": "FLOW-QA: FAIL 1234567890abcdef1234567890abcdef12345678\nFLOW-QA-ASSERTIONS pass=2 fail=1",
            "report": {"passed": False},
        }
        mock_update_card.return_value = MagicMock(ok=True)

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            outcome = post_deploy_qa.run_post_deploy_qa(
                base_url="https://test.dev",
                expected_sha=merge_sha,
                base_branch=base_branch,
                pr=101,
                issue=202,
                deploy_cmd=deploy_cmd,
                repo="Wladefant/super-board",
                dry_run=False,
            )

        self.assertFalse(outcome["ok"])
        output = stdout_buf.getvalue()
        self.assertIn(f"git revert -m 1 {merge_sha}", output)
        self.assertIn(f"git push origin {base_branch}", output)
        self.assertIn(deploy_cmd, output)

        # Verify Project card marked as Blocked
        mock_update_card.assert_called_once_with(
            issue=202,
            repo="Wladefant/super-board",
            state="Blocked",
            head_sha=merge_sha,
            dry_run=False,
        )

        # Verify revert was NOT run automatically
        # (mocked environment does not invoke git revert subprocess)
        self.assertNotIn("Reverting automatically", output)


class TestPostDeployQAProductionHostRefusal(unittest.TestCase):
    """Test refusal of PolySimulator production hosts directly and via redirects."""

    def test_refuse_direct_production_domains(self):
        forbidden_urls = [
            "https://polysimulator.com",
            "https://www.polysimulator.com",
            "https://app.polysimulator.com",
            "https://prod.polysimulator.com",
            "https://zaraprptkegxqpvnsubu.supabase.co",
            "https://akamai-iad-prod.polysimulator.com",
        ]
        for url in forbidden_urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as ctx:
                    post_deploy_qa.check_forbidden_host(url)
                self.assertIn("Refused production host", str(ctx.exception))

    @patch("post_deploy_qa.probe_redirect_chain")
    def test_refuse_redirect_to_production_domain(self, mock_probe):
        mock_probe.return_value = "https://polysimulator.com/api/version"
        with self.assertRaises(ValueError) as ctx:
            post_deploy_qa.validate_target_url("https://innocent-proxy.dev")
        self.assertIn("Refused production host", str(ctx.exception))


class TestPostDeployQAServedMismatch(unittest.TestCase):
    """Test served SHA mismatch detection against expected merge SHA."""

    @patch("post_deploy_qa.update_project_card_status")
    @patch("post_deploy_qa.post_github_comment")
    @patch("post_deploy_qa.run_flow_qa")
    @patch("post_deploy_qa.fetch_served_sha")
    def test_served_mismatch_fails_and_blocks_card(
        self, mock_fetch_sha, mock_run_flow_qa, mock_post_comment, mock_update_card
    ):
        expected_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        observed_sha = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        mock_fetch_sha.return_value = observed_sha
        mock_update_card.return_value = MagicMock(ok=True)

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            outcome = post_deploy_qa.run_post_deploy_qa(
                base_url="https://test.dev",
                expected_sha=expected_sha,
                base_branch="main",
                pr=55,
                issue=66,
                deploy_cmd="dokploy redeploy",
                repo="Wladefant/test-repo",
                dry_run=False,
            )

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "served_sha_mismatch")
        self.assertEqual(outcome["served_sha"], observed_sha)
        self.assertEqual(outcome["expected_sha"], expected_sha)

        # FLOW-QA browser runner must not run on mismatch
        mock_run_flow_qa.assert_not_called()

        # Output must contain recovery commands
        output = stdout_buf.getvalue()
        self.assertIn(f"git revert -m 1 {expected_sha}", output)
        self.assertIn("git push origin main", output)
        self.assertIn("dokploy redeploy", output)

        # Card must be marked Blocked
        mock_update_card.assert_called_once_with(
            issue=66,
            repo="Wladefant/test-repo",
            state="Blocked",
            head_sha=expected_sha,
            dry_run=False,
        )

        # Failure receipt must be posted
        self.assertTrue(mock_post_comment.called)
        posted_body = mock_post_comment.call_args_list[0][0][2]
        self.assertIn("FLOW-QA: FAIL", posted_body)
        self.assertIn(observed_sha, posted_body)


class TestPostDeployQADryRunNonMutation(unittest.TestCase):
    """Test dry run contacts version endpoint and reports plan without mutations."""

    @patch("post_deploy_qa.update_project_card_status")
    @patch("post_deploy_qa.post_github_comment")
    @patch("post_deploy_qa.run_flow_qa")
    @patch("post_deploy_qa.fetch_served_sha")
    def test_dry_run_reports_plan_without_mutating(
        self, mock_fetch_sha, mock_run_flow_qa, mock_post_comment, mock_update_card
    ):
        sha = "254b94b91e1ada39bcafe66b58e92bc235e14f09"
        mock_fetch_sha.return_value = sha

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            outcome = post_deploy_qa.run_post_deploy_qa(
                base_url="https://pinthread.dev",
                expected_sha=sha,
                base_branch="main",
                pr=42,
                issue=84,
                deploy_cmd="dokploy deploy pinthread",
                repo="Wladefant/pinthread",
                dry_run=True,
            )

        self.assertTrue(outcome["ok"])
        self.assertTrue(outcome["dry_run"])
        self.assertEqual(outcome["served_sha"], sha)

        # Version endpoint was contacted
        mock_fetch_sha.assert_called_once()

        # Zero browser runs
        mock_run_flow_qa.assert_not_called()

        # Zero GitHub comments posted
        mock_post_comment.assert_not_called()

        # Zero Project card mutations (or only with dry_run=True if called)
        for c in mock_update_card.mock_calls:
            self.assertTrue(c.kwargs.get("dry_run"))

        output = stdout_buf.getvalue()
        self.assertIn("[DRY-RUN] Post-Deploy QA Plan", output)
        self.assertIn("Served SHA matches expected merge SHA", output)


class TestPostDeployQAExplicitIdentifiers(unittest.TestCase):
    """Test requirement of explicit identifiers and deploy command."""

    def test_missing_required_identifiers_fails(self):
        valid_kwargs = {
            "base_url": "https://test.dev",
            "expected_sha": "1234567890abcdef1234567890abcdef12345678",
            "base_branch": "main",
            "pr": 1,
            "issue": 2,
            "deploy_cmd": "deploy.sh",
        }

        for missing in ["expected_sha", "base_branch", "pr", "issue", "deploy_cmd", "base_url"]:
            with self.subTest(missing=missing):
                kwargs = dict(valid_kwargs)
                kwargs[missing] = None
                with self.assertRaises(ValueError) as ctx:
                    post_deploy_qa.validate_required_identifiers(**kwargs)
                self.assertIn(missing, str(ctx.exception))



class TestPostDeployQAFlowQAVerification(unittest.TestCase):
    """Test verification contracts for run_flow_qa exact SHA and assertion counts."""

    @patch("post_deploy_qa.subprocess.run")
    def test_flow_qa_fails_on_pass_marker_without_sha(self, mock_subproc):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_subproc.return_value = MagicMock(
            returncode=0,
            stdout="FLOW-QA: PASS\nFLOW-QA-ASSERTIONS pass=3 fail=0\n",
            stderr="",
        )
        res = post_deploy_qa.run_flow_qa(
            base_url="https://test.dev",
            expected_sha=expected_sha,
            project="test",
        )
        self.assertFalse(res["passed"])

    @patch("post_deploy_qa.subprocess.run")
    def test_flow_qa_fails_on_pass_marker_with_wrong_sha(self, mock_subproc):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        wrong_sha = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        mock_subproc.return_value = MagicMock(
            returncode=0,
            stdout=f"FLOW-QA: PASS {wrong_sha}\nFLOW-QA-ASSERTIONS pass=3 fail=0\n",
            stderr="",
        )
        res = post_deploy_qa.run_flow_qa(
            base_url="https://test.dev",
            expected_sha=expected_sha,
            project="test",
        )
        self.assertFalse(res["passed"])

    @patch("post_deploy_qa.subprocess.run")
    def test_flow_qa_fails_on_zero_positive_assertions(self, mock_subproc):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_subproc.return_value = MagicMock(
            returncode=0,
            stdout=f"FLOW-QA: PASS {expected_sha}\nFLOW-QA-ASSERTIONS pass=0 fail=0\n",
            stderr="",
        )
        res = post_deploy_qa.run_flow_qa(
            base_url="https://test.dev",
            expected_sha=expected_sha,
            project="test",
        )
        self.assertFalse(res["passed"])

    @patch("post_deploy_qa.subprocess.run")
    def test_flow_qa_fails_on_non_zero_failed_assertions(self, mock_subproc):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_subproc.return_value = MagicMock(
            returncode=0,
            stdout=f"FLOW-QA: PASS {expected_sha}\nFLOW-QA-ASSERTIONS pass=3 fail=1\n",
            stderr="",
        )
        res = post_deploy_qa.run_flow_qa(
            base_url="https://test.dev",
            expected_sha=expected_sha,
            project="test",
        )
        self.assertFalse(res["passed"])

    @patch("post_deploy_qa.subprocess.run")
    def test_flow_qa_fails_when_assertions_missing(self, mock_subproc):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_subproc.return_value = MagicMock(
            returncode=0,
            stdout=f"FLOW-QA: PASS {expected_sha}\n",
            stderr="",
        )
        res = post_deploy_qa.run_flow_qa(
            base_url="https://test.dev",
            expected_sha=expected_sha,
            project="test",
        )
        self.assertFalse(res["passed"])

    @patch("post_deploy_qa.subprocess.run")
    def test_flow_qa_passes_on_exact_sha_and_positive_assertions_zero_fail(self, mock_subproc):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_subproc.return_value = MagicMock(
            returncode=0,
            stdout=f"FLOW-QA: PASS {expected_sha}\nFLOW-QA-ASSERTIONS pass=5 fail=0\n",
            stderr="",
        )
        res = post_deploy_qa.run_flow_qa(
            base_url="https://test.dev",
            expected_sha=expected_sha,
            project="test",
        )
        self.assertTrue(res["passed"])

    @patch("post_deploy_qa.subprocess.run")
    def test_flow_qa_subprocess_timeout_returns_failure_dict(self, mock_subproc):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_subproc.side_effect = subprocess.TimeoutExpired(cmd=["test"], timeout=30)
        res = post_deploy_qa.run_flow_qa(
            base_url="https://test.dev",
            expected_sha=expected_sha,
            project="test",
        )
        self.assertFalse(res["passed"])
        self.assertTrue(res.get("timed_out"))
        self.assertEqual(res["returncode"], 124)
        self.assertIn(f"FLOW-QA: FAIL {expected_sha}", res["receipt"])
        self.assertIn("FLOW-QA-REASON timeout", res["receipt"])


class TestPostDeployQATimeoutAndExceptionHandling(unittest.TestCase):
    """Test timeout and runner exception handling produces SHA-bound FAIL, hints, card Blocked."""

    @patch("post_deploy_qa.update_project_card_status")
    @patch("post_deploy_qa.post_github_comment")
    @patch("post_deploy_qa.run_flow_qa")
    @patch("post_deploy_qa.fetch_served_sha")
    def test_flow_qa_timeout_produces_sha_bound_fail_hints_and_blocks_card(
        self, mock_fetch_sha, mock_run_flow_qa, mock_post_comment, mock_update_card
    ):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_fetch_sha.return_value = expected_sha
        mock_run_flow_qa.side_effect = subprocess.TimeoutExpired(cmd=["node", "runner.mjs"], timeout=300)
        mock_update_card.return_value = MagicMock(ok=True)
        mock_post_comment.return_value = (0, "ok", "")

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            outcome = post_deploy_qa.run_post_deploy_qa(
                base_url="https://test.dev",
                expected_sha=expected_sha,
                base_branch="main",
                pr=10,
                issue=20,
                deploy_cmd="dokploy deploy test",
                repo="Wladefant/test",
            )

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "timeout")
        self.assertIn(f"FLOW-QA: FAIL {expected_sha}", outcome["receipt"])

        # Revert hint in stdout
        output = stdout_buf.getvalue()
        self.assertIn(f"git revert -m 1 {expected_sha}", output)
        self.assertIn("dokploy deploy test", output)

        # Card marked Blocked
        mock_update_card.assert_called_once_with(
            issue=20,
            repo="Wladefant/test",
            state="Blocked",
            head_sha=expected_sha,
            dry_run=False,
        )

        # Failure receipt posted
        self.assertTrue(mock_post_comment.called)
        posted_body = mock_post_comment.call_args_list[0][0][2]
        self.assertIn(f"FLOW-QA: FAIL {expected_sha}", posted_body)

    @patch("post_deploy_qa.update_project_card_status")
    @patch("post_deploy_qa.post_github_comment")
    @patch("post_deploy_qa.run_flow_qa")
    @patch("post_deploy_qa.fetch_served_sha")
    def test_runner_exception_produces_sha_bound_fail_hints_and_blocks_card(
        self, mock_fetch_sha, mock_run_flow_qa, mock_post_comment, mock_update_card
    ):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_fetch_sha.return_value = expected_sha
        mock_run_flow_qa.side_effect = RuntimeError("Runner crashed unexpectedly")
        mock_update_card.return_value = MagicMock(ok=True)
        mock_post_comment.return_value = (0, "ok", "")

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            outcome = post_deploy_qa.run_post_deploy_qa(
                base_url="https://test.dev",
                expected_sha=expected_sha,
                base_branch="main",
                pr=10,
                issue=20,
                deploy_cmd="dokploy deploy test",
                repo="Wladefant/test",
            )

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "runner_exception")
        self.assertIn(f"FLOW-QA: FAIL {expected_sha}", outcome["receipt"])

        output = stdout_buf.getvalue()
        self.assertIn(f"git revert -m 1 {expected_sha}", output)

        mock_update_card.assert_called_once_with(
            issue=20,
            repo="Wladefant/test",
            state="Blocked",
            head_sha=expected_sha,
            dry_run=False,
        )


class TestPostDeployQAPublicationAndCardFailures(unittest.TestCase):
    """Test publication and card result failures rather than ignoring them."""

    @patch("post_deploy_qa.update_project_card_status")
    @patch("post_deploy_qa.post_github_comment")
    @patch("post_deploy_qa.run_flow_qa")
    @patch("post_deploy_qa.fetch_served_sha")
    def test_pr_comment_failure_fails_post_deploy_qa(
        self, mock_fetch_sha, mock_run_flow_qa, mock_post_comment, mock_update_card
    ):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_fetch_sha.return_value = expected_sha
        mock_run_flow_qa.return_value = {
            "passed": True,
            "receipt": f"FLOW-QA: PASS {expected_sha}\nFLOW-QA-ASSERTIONS pass=3 fail=0\n",
            "report": {"passed": True},
        }
        # PR comment fails (rc=1), Issue comment succeeds (rc=0)
        mock_post_comment.side_effect = [(1, "", "gh: PR not found"), (0, "ok", "")]

        outcome = post_deploy_qa.run_post_deploy_qa(
            base_url="https://test.dev",
            expected_sha=expected_sha,
            base_branch="main",
            pr=10,
            issue=20,
            deploy_cmd="dokploy deploy test",
            repo="Wladefant/test",
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "publication_failed")
        self.assertFalse(outcome.get("publication_ok", True))

    @patch("post_deploy_qa.update_project_card_status")
    @patch("post_deploy_qa.post_github_comment")
    @patch("post_deploy_qa.run_flow_qa")
    @patch("post_deploy_qa.fetch_served_sha")
    def test_issue_comment_failure_fails_post_deploy_qa(
        self, mock_fetch_sha, mock_run_flow_qa, mock_post_comment, mock_update_card
    ):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_fetch_sha.return_value = expected_sha
        mock_run_flow_qa.return_value = {
            "passed": True,
            "receipt": f"FLOW-QA: PASS {expected_sha}\nFLOW-QA-ASSERTIONS pass=3 fail=0\n",
            "report": {"passed": True},
        }
        # PR comment succeeds (rc=0), Issue comment fails (rc=1)
        mock_post_comment.side_effect = [(0, "ok", ""), (1, "", "gh: API rate limit")]

        outcome = post_deploy_qa.run_post_deploy_qa(
            base_url="https://test.dev",
            expected_sha=expected_sha,
            base_branch="main",
            pr=10,
            issue=20,
            deploy_cmd="dokploy deploy test",
            repo="Wladefant/test",
        )

        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "publication_failed")

    @patch("post_deploy_qa.update_project_card_status")
    @patch("post_deploy_qa.post_github_comment")
    @patch("post_deploy_qa.run_flow_qa")
    @patch("post_deploy_qa.fetch_served_sha")
    def test_card_failure_recorded_on_flow_failure(
        self, mock_fetch_sha, mock_run_flow_qa, mock_post_comment, mock_update_card
    ):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_fetch_sha.return_value = expected_sha
        mock_run_flow_qa.return_value = {
            "passed": False,
            "receipt": f"FLOW-QA: FAIL {expected_sha}\nFLOW-QA-ASSERTIONS pass=2 fail=1\n",
            "report": {"passed": False},
        }
        mock_post_comment.return_value = (0, "ok", "")
        mock_update_card.return_value = MagicMock(ok=False)

        outcome = post_deploy_qa.run_post_deploy_qa(
            base_url="https://test.dev",
            expected_sha=expected_sha,
            base_branch="main",
            pr=10,
            issue=20,
            deploy_cmd="dokploy deploy test",
            repo="Wladefant/test",
        )

        self.assertFalse(outcome["ok"])
        self.assertFalse(outcome.get("card_ok", True))


class TestPostDeployQADryRunPlanOutput(unittest.TestCase):
    """Test dry-run does not claim PASS will occur, only receipt follows flow result."""

    @patch("post_deploy_qa.update_project_card_status")
    @patch("post_deploy_qa.fetch_served_sha")
    def test_dry_run_does_not_claim_pass_receipt(self, mock_fetch_sha, mock_update_card):
        sha = "1234567890abcdef1234567890abcdef12345678"
        mock_fetch_sha.return_value = sha
        mock_update_card.return_value = MagicMock(ok=True)

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            outcome = post_deploy_qa.run_post_deploy_qa(
                base_url="https://pinthread.dev",
                expected_sha=sha,
                base_branch="main",
                pr=1,
                issue=2,
                deploy_cmd="deploy",
                dry_run=True,
            )

        self.assertTrue(outcome["ok"])
        output = stdout_buf.getvalue()
        self.assertNotIn("Would post FLOW-QA PASS receipt", output)
        self.assertIn("receipt following flow result", output)


class TestPostDeployQAStorageStatePassthrough(unittest.TestCase):
    """Test CLI storage-state passthrough when runner supports it."""

    @patch("post_deploy_qa.subprocess.run")
    def test_storage_state_passed_when_supported(self, mock_subproc):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_subproc.return_value = MagicMock(
            returncode=0,
            stdout=f"FLOW-QA: PASS {expected_sha}\nFLOW-QA-ASSERTIONS pass=3 fail=0\n",
            stderr="",
        )

        # Mock runner_supports_storage_state to return True
        with patch("post_deploy_qa.runner_supports_storage_state", return_value=True):
            post_deploy_qa.run_flow_qa(
                base_url="https://test.dev",
                expected_sha=expected_sha,
                storage_state="/path/to/storage.json",
            )

        called_cmd = mock_subproc.call_args[0][0]
        self.assertIn("--storage-state", called_cmd)
        self.assertIn("/path/to/storage.json", called_cmd)

    @patch("post_deploy_qa.subprocess.run")
    def test_storage_state_omitted_when_not_supported(self, mock_subproc):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_subproc.return_value = MagicMock(
            returncode=0,
            stdout=f"FLOW-QA: PASS {expected_sha}\nFLOW-QA-ASSERTIONS pass=3 fail=0\n",
            stderr="",
        )

        with patch("post_deploy_qa.runner_supports_storage_state", return_value=False):
            post_deploy_qa.run_flow_qa(
                base_url="https://test.dev",
                expected_sha=expected_sha,
                storage_state="/path/to/storage.json",
            )

        called_cmd = mock_subproc.call_args[0][0]
        self.assertNotIn("--storage-state", called_cmd)

    @patch("post_deploy_qa.update_project_card_status")
    @patch("post_deploy_qa.post_github_comment")
    @patch("post_deploy_qa.run_flow_qa")
    @patch("post_deploy_qa.fetch_served_sha")
    def test_storage_state_passed_through_run_post_deploy_qa(
        self, mock_fetch_sha, mock_run_flow_qa, mock_post_comment, mock_update_card
    ):
        expected_sha = "1234567890abcdef1234567890abcdef12345678"
        mock_fetch_sha.return_value = expected_sha
        mock_run_flow_qa.return_value = {
            "passed": True,
            "receipt": f"FLOW-QA: PASS {expected_sha}\nFLOW-QA-ASSERTIONS pass=3 fail=0\n",
            "report": {"passed": True},
        }
        mock_post_comment.return_value = (0, "ok", "")
        mock_update_card.return_value = MagicMock(ok=True)

        outcome = post_deploy_qa.run_post_deploy_qa(
            base_url="https://test.dev",
            expected_sha=expected_sha,
            base_branch="main",
            pr=10,
            issue=20,
            deploy_cmd="deploy",
            storage_state="/tmp/session.json",
        )

        self.assertTrue(outcome["ok"])
        mock_run_flow_qa.assert_called_once()
        self.assertEqual(mock_run_flow_qa.call_args.kwargs.get("storage_state"), "/tmp/session.json")
if __name__ == "__main__":
    unittest.main()
