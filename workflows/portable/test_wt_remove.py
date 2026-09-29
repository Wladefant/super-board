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

    # --- issue #326, item 1: the caller's ancestor chain is not "busy" ---------------
    def test_busy_ignores_caller_ancestor_chain(self) -> None:
        wt = str(self.wt)
        wrapper_cmd = f"cmd /c python wt_remove.py {wt} --dry-run"
        procs = [
            (os.getpid(), 11, "python.exe", "", wrapper_cmd),
            (11, 0, "cmd.exe", "", wrapper_cmd),  # parent of this process: skipped
            (12, 11, "cmd.exe", "", wrapper_cmd),  # sibling of this process: still busy
            (13, 0, "bash.exe", "", f"grep {wt} log.txt"),  # unrelated process: busy
        ]
        hits = wt_remove.busy_processes(wt, procs)
        self.assertEqual([pid for pid, _name, _cwd in hits], [12, 13])

    def test_busy_cwd_of_active_process_blocks_removal(self) -> None:
        if sys.platform != "win32":
            self.skipTest("process cwd scan is Windows-only")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=str(self.wt))
        self.addCleanup(_kill, proc)
        self.assertEqual(self.remove(), 2)
        self.assert_untouched()

    # --- issue #326, item 2: command-line match needs a boundary ----------------------
    def test_busy_command_line_requires_boundary(self) -> None:
        base = wt_remove.norm(self.wt)
        procs = [
            (21, 0, "cmd.exe", "", f"cmd /c python wt_remove.py {base}-2 --dry-run"),  # sibling prefix
            (22, 0, "cmd.exe", "", f'cmd /c python wt_remove.py "{base}" --dry-run'),
            (23, 0, "python.exe", "", f"python wt_remove.py {base}\\scripts"),
            (24, 0, "cmd.exe", "", f"cmd /c python wt_remove.py {base} --dry-run"),
        ]
        hits = wt_remove.busy_processes(self.wt, procs)
        self.assertEqual([pid for pid, _name, _cwd in hits], [22, 23, 24])

    @unittest.skipUnless(sys.platform == "win32", "cmd wrapper ancestry is Windows-specific")
    def test_cmd_wrapper_dry_run_on_idle_worktree_exits_zero(self) -> None:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wt_remove.py")
        r = subprocess.run(
            f'cmd /c "{sys.executable}" "{script}" "{self.wt}" --dry-run',
            capture_output=True, text=True, timeout=120, cwd=str(self.tmp),
        )
    def test_verbatim_unc_target_keeps_share(self) -> None:
        self.assertEqual(wt_remove._verbatim_to_abs("\\\\?\\UNC\\server\\share\\dir"), "\\\\server\\share\\dir")
        self.assertEqual(wt_remove._verbatim_to_abs("\\\\?\\C:\\dir"), "C:\\dir")
        self.assertEqual(wt_remove._verbatim_to_abs("relative\\dir"), "relative\\dir")

    # --- issue #326, items 4/5: scan errors and partial process scans are reported ---
    def test_iter_links_reports_unscannable_directory(self) -> None:
        import unittest.mock

        (self.wt / "private").mkdir()
        real_scandir = os.scandir

        def fake_scandir(path):
            if wt_remove.norm(path) == wt_remove.norm(self.wt / "private"):
                raise PermissionError(13, "Permission denied", path)
            return real_scandir(path)

        errors: list[str] = []
        with unittest.mock.patch("os.scandir", side_effect=fake_scandir):
            links = list(wt_remove.iter_links(self.wt, errors))
            report = wt_remove.remove_worktree(str(self.wt), False, True, False, (self.tmp,))
        self.assertEqual(links, [str(self.link)])
        self.assertTrue(errors and "private" in errors[0])
        self.assertTrue(report["refused"])
        self.assertEqual(report["scan_errors"], errors)
        self.assert_untouched()

    def test_report_records_process_scan_coverage(self) -> None:
        report = wt_remove.remove_worktree(str(self.wt), False, True, False, (self.tmp,))
        scan = report["process_scan"]
        self.assertEqual(scan["platform"], sys.platform)
        if sys.platform != "win32":
            self.assertTrue(scan["partial"])
        else:
            self.assertGreaterEqual(scan["read"], 1)
            self.assertEqual(scan["partial"], scan["read"] < scan["total"])

    # --- issue #326, items 6/7: removal runs against the common dir and validated top --
    # (exercised by every removal test above; the old dirname(common) main-root guess and
    # the resolve() of the argument would break them, so no separate test is added.)

    # --- issue #326, item 8: an unlink failure keeps the progress report -------------
    def test_unlink_failure_preserves_progress_and_skips_removal(self) -> None:
        import unittest.mock

        (self.wt / "frontend").mkdir()
        make_link(self.wt / "frontend" / "node_modules", self.shared)
        calls: list[str] = []
        real_unlink = wt_remove.unlink_link

        def flaky(link: str) -> None:
            calls.append(link)
            if len(calls) == 2:
                raise OSError("simulated unlink failure")
            real_unlink(link)

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with unittest.mock.patch.object(wt_remove, "unlink_link", side_effect=flaky):
                report = wt_remove.remove_worktree(str(self.wt), False, False, False, (self.tmp,))
        self.assertIn("simulated unlink failure", report["error"])
        self.assertEqual(len(report["unlinked"]), 2)
        self.assertFalse(report["removed"])
        self.assertTrue(self.wt.is_dir())
        self.assertFalse(wt_remove.is_link(self.link), "first link should have been unlinked")
        self.assertTrue(wt_remove.is_link(self.wt / "frontend" / "node_modules"))
        self.assertTrue(self.sentinel.is_file())

    # --- issue #326, item 9: an untracked directory holding only links is not dirty ---
    def test_untracked_dir_holding_only_links_is_not_dirty(self) -> None:
        (self.wt / "stuff").mkdir()
        make_link(self.wt / "stuff" / "node_modules", self.shared)
        report = wt_remove.remove_worktree(str(self.wt), False, True, False, (self.tmp,))
        self.assertFalse(report.get("refused"), report)
        self.assertEqual(report.get("dirty", []), [])
        self.assertEqual(self.remove(), 0)
        self.assertFalse(self.wt.exists())
        self.assertTrue(self.sentinel.is_file())

    def test_untracked_dir_with_extra_file_is_still_dirty(self) -> None:
        (self.wt / "stuff").mkdir()
        make_link(self.wt / "stuff" / "node_modules", self.shared)
        (self.wt / "stuff" / "keep.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.remove(), 2)
        self.assert_untouched()

    # --- issue #326, item 10: ls-files uses literal pathspecs ------------------------
    def test_glob_in_link_name_matches_only_itself(self) -> None:
        (self.wt / "a1").write_text("tracked", encoding="utf-8")
        run_git("add", "a1", cwd=self.wt)
        run_git("commit", "-q", "-m", "a1", cwd=self.wt)
        make_link(self.wt / "a[1]", self.shared)
        self.assertEqual(self.remove(), 0)
        self.assertFalse(self.wt.exists())
        self.assertTrue(self.sentinel.is_file())


def _kill(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait()


if __name__ == "__main__":
    unittest.main()
