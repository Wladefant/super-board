#!/usr/bin/env python3
"""Tests for wt_remove.py: removal never reaches through a link, and a refusal changes nothing.

Each test builds a throwaway repository with a linked worktree whose ``node_modules`` is a
junction (Windows) or symlink (elsewhere) to a shared directory holding a sentinel file.
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wt_remove  # noqa: E402  (sibling module; path set above)


def make_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def run_git(*args: str, cwd: Path) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    )
    return r.stdout


class WtRemoveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wtremove-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.shared = self.tmp / "shared"
        self.shared.mkdir()
        self.sentinel = self.shared / "sentinel.txt"
        self.sentinel.write_text("shared", encoding="utf-8")
        self.main = self.tmp / "main"
        self.main.mkdir()
        run_git("init", "-q", "-b", "main", cwd=self.main)
        (self.main / "tracked.txt").write_text("v1", encoding="utf-8")
        run_git("add", "tracked.txt", cwd=self.main)
        run_git("commit", "-q", "-m", "init", cwd=self.main)
        self.wt = self.tmp / "lane"
        run_git("worktree", "add", "-q", "-b", "lane", str(self.wt), cwd=self.main)
        (self.wt / "feature.txt").write_text("feature-v1", encoding="utf-8")
        run_git("add", "feature.txt", cwd=self.wt)
        run_git("commit", "-q", "-m", "feature", cwd=self.wt)
        run_git("merge", "-q", "lane", cwd=self.main)
        self.link = self.wt / "node_modules"  # deliberately not gitignored: git sees it as untracked
        make_link(self.link, self.shared)
    def remove(self, *extra: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return wt_remove.main([str(self.wt), "--scan-root", str(self.tmp), *extra])

    def assert_untouched(self) -> None:
        self.assertTrue(self.wt.is_dir())
        self.assertTrue(wt_remove.is_link(self.link), "the refused worktree lost its link")
        self.assertTrue(self.sentinel.is_file())

    def test_clean_worktree_is_removed_and_shared_tree_survives(self) -> None:
        self.assertEqual(self.remove(), 0)
        self.assertFalse(self.wt.exists())
        self.assertTrue(self.sentinel.is_file())

    def test_untracked_file_without_force_is_refused_untouched(self) -> None:
        (self.wt / "scratch.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.remove(), 2)
        self.assert_untouched()

    def test_modified_tracked_file_without_force_is_refused_untouched(self) -> None:
        (self.wt / "tracked.txt").write_text("v2", encoding="utf-8")
        self.assertEqual(self.remove(), 2)
        self.assert_untouched()

    def test_dirty_worktree_with_force_is_removed_and_shared_tree_survives(self) -> None:
        (self.wt / "scratch.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.remove("--force"), 0)
        self.assertFalse(self.wt.exists())
        self.assertTrue(self.sentinel.is_file())

    def test_locked_worktree_is_refused_even_with_force(self) -> None:
        run_git("worktree", "lock", str(self.wt), cwd=self.main)
        self.assertEqual(self.remove("--force"), 2)
        self.assert_untouched()

    def test_dry_run_reports_refusal_for_dirty_tree(self) -> None:
        (self.wt / "scratch.txt").write_text("x", encoding="utf-8")
        report = wt_remove.remove_worktree(str(self.wt), False, True, False, (self.tmp,))
        self.assertTrue(report["refused"])
        self.assertEqual(report["dirty"], ["scratch.txt"])
        self.assert_untouched()

    def test_main_worktree_and_subdirectory_are_refused(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(wt_remove.main([str(self.main), "--scan-root", str(self.tmp)]), 2)
            sub = self.wt / "sub"
            sub.mkdir()
            self.assertEqual(wt_remove.main([str(sub), "--scan-root", str(self.tmp)]), 2)
        self.assertTrue((self.main / "tracked.txt").is_file())
        self.assert_untouched()

    def test_worktree_another_worktree_links_into_is_refused(self) -> None:
        other = self.tmp / "other"
        run_git("worktree", "add", "-q", "-b", "other", str(other), cwd=self.main)
        (self.wt / "frontend").mkdir()
        (self.wt / "frontend" / "node_modules").mkdir()
        (other / "frontend").mkdir()
        make_link(other / "frontend" / "node_modules", self.wt / "frontend" / "node_modules")
        self.assertEqual(self.remove("--force"), 2)
        self.assert_untouched()

    def test_clean_unmerged_worktree_is_refused_untouched(self) -> None:
        (self.wt / "unmerged.txt").write_text("unmerged", encoding="utf-8")
        run_git("add", "unmerged.txt", cwd=self.wt)
        run_git("commit", "-q", "-m", "unmerged feature", cwd=self.wt)
        self.assertEqual(self.remove(), 2)
        self.assert_untouched()
        report = wt_remove.remove_worktree(str(self.wt), False, False, False, (self.tmp,))
        self.assertTrue(report["refused"])
        self.assertEqual(report["branch"], "lane")
        self.assertEqual(report["merged_into"], [])
        self.assertIn("not reachable", report["error"])
        self.assert_untouched()

    def test_dirty_unmerged_worktree_with_force_is_refused_untouched(self) -> None:
        (self.wt / "unmerged.txt").write_text("unmerged", encoding="utf-8")
        run_git("add", "unmerged.txt", cwd=self.wt)
        run_git("commit", "-q", "-m", "unmerged feature", cwd=self.wt)
        (self.wt / "scratch.txt").write_text("dirty content", encoding="utf-8")
        self.assertEqual(self.remove("--force"), 2)
        self.assert_untouched()
        report = wt_remove.remove_worktree(str(self.wt), True, False, False, (self.tmp,))
        self.assertTrue(report["refused"])
        self.assertEqual(report["branch"], "lane")
        self.assertEqual(report["merged_into"], [])
        self.assertIn("not reachable", report["error"])
        self.assert_untouched()

    def test_detached_head_reachable_is_removed_and_unmerged_refused(self) -> None:
        run_git("checkout", "-q", "--detach", cwd=self.wt)
        report = wt_remove.remove_worktree(str(self.wt), False, True, False, (self.tmp,))
        self.assertEqual(report["branch"], "(detached)")
        self.assertIn("main", report["merged_into"])
        self.assertFalse(report.get("refused", False))

        (self.wt / "detached_unmerged.txt").write_text("unmerged", encoding="utf-8")
        run_git("add", "detached_unmerged.txt", cwd=self.wt)
        run_git("commit", "-q", "-m", "detached unmerged", cwd=self.wt)
        self.assertEqual(self.remove(), 2)
        self.assert_untouched()
        self.assertEqual(self.remove("--force"), 2)
        self.assert_untouched()

    def test_report_includes_branch_full_head_and_merged_into(self) -> None:
        report = wt_remove.remove_worktree(str(self.wt), False, True, False, (self.tmp,))
        self.assertEqual(report["branch"], "lane")
        self.assertEqual(len(report["head"]), 40)
        self.assertIn("main", report["merged_into"])


if __name__ == "__main__":
    unittest.main()
