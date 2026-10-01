#!/usr/bin/env python3
"""
test_lane_inventory.py - Unit and regression tests for mechanically verified lane inventory.

Part of portable workflow core in Wladefant/super-board.
References:
  - Superboard Issue #227 (High-Trust Agent Architecture & Verification Skills)
  - Profile AGENTS.md §2 (Never Lose Started Lanes & Bounded Concurrency)

Tests:
  1. Path normalization for POSIX and Windows paths.
  2. Worktree verification: missing directory, non-git directory, valid git repo.
  3. PR verification: open matching PR, branch mismatch, merged PR, closed PR.
  4. Telemetry extraction from session JSONL: tool calls, prompt hints, last action.
  5. Audit integration: tagging UNVERIFIED on missing worktree, merged PR, branch mismatch.
  6. Markdown table formatting.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lane_inventory import (
    audit_lane,
    extract_lane_telemetry,
    format_markdown_table,
    normalize_path,
    verify_pr,
    verify_worktree,
)


class TestLaneInventory(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_lane_inv_")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_normalize_path(self):
        self.assertEqual(normalize_path("C:\\Users\\test\\dir"), "C:/Users/test/dir")
        self.assertEqual(normalize_path("foo/bar", base_dir="C:/Users/test"), "C:/Users/test/foo/bar")
        self.assertEqual(normalize_path(""), "")

    def test_verify_worktree_missing_and_non_git(self):
        # Missing directory
        res_missing = verify_worktree(os.path.join(self.temp_dir, "does_not_exist"))
        self.assertFalse(res_missing["verified"])
        self.assertIn("does not exist", res_missing["reason"])

        # Existing directory, but not a git repo
        non_git = Path(self.temp_dir) / "plain_dir"
        non_git.mkdir()
        res_non_git = verify_worktree(str(non_git))
        self.assertFalse(res_non_git["verified"])
        self.assertIn("Not a git repository", res_non_git["reason"])

    def test_verify_worktree_valid_git(self):
        git_dir = Path(self.temp_dir) / "test_repo"
        git_dir.mkdir()
        subprocess.run(["git", "-C", str(git_dir), "init", "-b", "feat/my-feature"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(git_dir), "config", "user.name", "Test"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(git_dir), "config", "user.email", "test@test.com"], capture_output=True, check=True)
        
        # Commit a file
        (git_dir / "test.txt").write_text("hello", encoding="utf-8")
        subprocess.run(["git", "-C", str(git_dir), "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(git_dir), "commit", "-m", "init"], capture_output=True, check=True)

        res = verify_worktree(str(git_dir))
        self.assertTrue(res["verified"])
        self.assertEqual(res["branch"], "feat/my-feature")
        self.assertIsNotNone(res["sha"])
        self.assertEqual(len(res["sha"]), 40)
        self.assertEqual(res["short_sha"], res["sha"][:8])
        self.assertFalse(res["dirty"])

    @patch("subprocess.run")
    def test_verify_pr_states(self, mock_run):
        # 1. Open matching PR
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "number": 100,
                "state": "OPEN",
                "title": "fix: my fix",
                "headRefName": "feat/my-branch",
                "headRefOid": "1234567890abcdef",
                "url": "https://github.com/owner/repo/pull/100"
            })
        )
        res_open = verify_pr("owner/repo", 100, expected_branch="feat/my-branch")
        self.assertTrue(res_open["verified"])
        self.assertEqual(res_open["state"], "OPEN")

        # 2. Branch mismatch
        res_mismatch = verify_pr("owner/repo", 100, expected_branch="feat/other-branch")
        self.assertFalse(res_mismatch["verified"])
        self.assertIn("branch mismatch", res_mismatch["reason"])

        # 3. Merged PR
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "number": 101,
                "state": "MERGED",
                "title": "docs: update docs",
                "headRefName": "feat/my-branch",
                "headRefOid": "1234567890abcdef",
                "url": "https://github.com/owner/repo/pull/101"
            })
        )
        res_merged = verify_pr("owner/repo", 101, expected_branch="feat/my-branch")
        self.assertFalse(res_merged["verified"])
        self.assertIn("is MERGED", res_merged["reason"])

        # 4. Closed PR
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "number": 102,
                "state": "CLOSED",
                "title": "abandoned work",
                "headRefName": "feat/my-branch",
                "headRefOid": "1234567890abcdef",
                "url": "https://github.com/owner/repo/pull/102"
            })
        )
        res_closed = verify_pr("owner/repo", 102)
        self.assertFalse(res_closed["verified"])
        self.assertIn("is CLOSED", res_closed["reason"])

        # 5. Non-existent PR (gh error)
        mock_run.return_value = MagicMock(returncode=1, stderr="not found")
        res_missing = verify_pr("owner/repo", 999)
        self.assertFalse(res_missing["verified"])
        self.assertIn("not found", res_missing["reason"])

    def test_extract_lane_telemetry(self):
        session_file = Path(self.temp_dir) / "TestLane-1.jsonl"
        lines = [
            json.dumps({"type": "session_init"}),
            json.dumps({"type": "model_change", "model": "openai-codex/gpt-5.6-sol"}),
            json.dumps({
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Worktree C:/Users/wkiri/development/.wt-test, branch feat/test, PR https://github.com/Bavariance/polysimulator/pull/5000"}]
                }
            }),
            json.dumps({
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "toolCall", "name": "set_cwd", "arguments": {"path": "C:/Users/wkiri/development/.wt-actual"}},
                        {"type": "toolCall", "name": "bash", "arguments": {"command": "git -C C:/Users/wkiri/development/.wt-tool diff", "cwd": "C:/Users/wkiri/development/.wt-actual"}}
                    ]
                }
            }),
            json.dumps({"timestamp": "2026-09-29T21:42:00Z"})
        ]
        session_file.write_text("\n".join(lines), encoding="utf-8")

        telemetry = extract_lane_telemetry(session_file)
        self.assertEqual(telemetry["name"], "TestLane-1")
        self.assertEqual(telemetry["model"], "openai-codex/gpt-5.6-sol")
        self.assertEqual(telemetry["status"], "STOPPED")
        self.assertEqual(telemetry["last_ts"], "2026-09-29T21:42:00Z")
        self.assertIn("C:/Users/wkiri/development/.wt-actual", telemetry["tool_cwds"])
        self.assertIn("C:/Users/wkiri/development/.wt-test", telemetry["prompt_wts"])
        self.assertIn(("Bavariance/polysimulator", 5000), telemetry["candidate_prs"])

    @patch("lane_inventory.verify_pr")
    def test_audit_lane_unverified_marking(self, mock_verify_pr):
        session_file = Path(self.temp_dir) / "BogusLane-1.jsonl"
        lines = [
            json.dumps({"type": "model_change", "model": "gemini-3.8-flash"}),
            json.dumps({
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Worktree C:/Users/wkiri/development/.wt-does-not-exist, branch fix/phantom, PR https://github.com/Bavariance/polysimulator/pull/1234"}]
                }
            }),
            json.dumps({"timestamp": "2026-09-29T21:40:00Z"})
        ]
        session_file.write_text("\n".join(lines), encoding="utf-8")

        mock_verify_pr.return_value = {
            "verified": False,
            "reason": "PR #1234 is MERGED (docs: old release notes)",
            "state": "MERGED"
        }

        audited = audit_lane(session_file, dev_root=self.temp_dir)
        self.assertIn("UNVERIFIED", audited["worktree"])
        self.assertEqual(audited["branch"], "UNVERIFIED")
        self.assertIn("UNVERIFIED", audited["pr"])
        self.assertIn("is MERGED", audited["pr"])

    @patch("lane_inventory.verify_pr")
    def test_audit_lane_default_cwd_marked_unverified_and_falls_back_to_git_branch(self, mock_verify_pr):
        """A lane running in session root cwd on staging/main must be tagged UNVERIFIED (default cwd) and fall back to git push/commit branch."""
        shared_root = Path(self.temp_dir) / ".wt-merge-recovery-20260922-main2"
        shared_root.mkdir()
        (shared_root / ".git").mkdir()

        session_file = Path(self.temp_dir) / "RealtimeLane-1.jsonl"
        lines = [
            json.dumps({"type": "model_change", "model": "gpt-5.6-sol"}),
            json.dumps({
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Worktree .wt-merge-recovery-20260922-main2, PR #5828"}]
                }
            }),
            json.dumps({
                "message": {
                    "role": "assistant",
                    "content": [{
                        "type": "toolCall",
                        "name": "bash",
                        "arguments": {"command": "git push origin feat/2769-user-state-events"}
                    }]
                }
            }),
            json.dumps({"timestamp": "2026-09-29T21:42:33Z"})
        ]
        session_file.write_text("\n".join(lines), encoding="utf-8")

        mock_verify_pr.return_value = {
            "verified": False,
            "reason": "PR #5828 is MERGED",
            "state": "MERGED"
        }

        audited = audit_lane(session_file, dev_root=self.temp_dir)
        self.assertEqual(audited["worktree"], "UNVERIFIED (default cwd)")
        self.assertEqual(audited["branch"], "feat/2769-user-state-events")
        self.assertIn("UNVERIFIED", audited["pr"])

    def test_format_markdown_table(self):
        items = [{
            "name": "Lane-1",
            "model": "google-antigravity/gemini-3.8-flash",
            "status": "STOPPED",
            "last_ts": "2026-09-29T21:42:49Z",
            "worktree": "C:/Users/wkiri/development/.wt-test",
            "branch": "feat/my-branch",
            "pr": "[123](https://github.com/Bavariance/polysimulator/pull/123)",
            "head_sha": "abcdef12",
            "last_action": "bash: git status",
            "issues": ["5800"]
        }]
        md = format_markdown_table(items)
        self.assertIn("| **Lane-1** |", md)
        self.assertIn("`gemini-3.8-flash`", md)
        self.assertIn("**STOPPED**", md)
        self.assertIn("[123](https://github.com/Bavariance/polysimulator/pull/123)", md)


if __name__ == "__main__":
    unittest.main()
