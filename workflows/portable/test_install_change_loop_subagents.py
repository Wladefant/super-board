"""Tests for install_change_loop_subagents (Wladefant/super-board#501)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import install_change_loop_subagents as icl

BLOCK = icl.block_text()
STE = "<!-- ste-writing:begin -->\nSTE line\n<!-- ste-writing:end -->\n"


class RenderTests(unittest.TestCase):
    def test_appends_once_and_is_idempotent(self):
        once = icl.render("---\nname: x\n---\nBody.\n", BLOCK)
        self.assertEqual(once.count(icl.BEGIN), 1)
        self.assertEqual(icl.render(once, BLOCK), once)

    def test_goes_before_another_marker_block_and_leaves_it_alone(self):
        out = icl.render("Body.\n" + STE, BLOCK)
        self.assertLess(out.index(icl.BEGIN), out.index("<!-- ste-writing:begin -->"))
        self.assertTrue(out.endswith(STE))

    def test_update_replaces_old_block_text(self):
        old = "Body.\n" + icl.BEGIN + "\nstale\n" + icl.END + "\n" + STE
        out = icl.render(old, BLOCK)
        self.assertNotIn("stale", out)
        self.assertEqual(out.count(icl.BEGIN), 1)
        self.assertTrue(out.endswith(STE))

    def test_block_names_both_report_lines(self):
        self.assertIn("Deleted:", BLOCK)
        self.assertIn("Not run:", BLOCK)
        self.assertIn("Project stage:", BLOCK)


class SyncTests(unittest.TestCase):
    def test_check_reports_drift_then_install_clears_it_and_writes_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "task.md").write_text("---\nname: task\n---\nBody.\n", encoding="utf-8")
            self.assertEqual(icl.sync(root, BLOCK, True, "20261005"), ["task"])
            self.assertEqual(icl.sync(root, BLOCK, False, "20261005"), ["task"])
            self.assertEqual(icl.sync(root, BLOCK, True, "20261005"), [])
            self.assertTrue((root / "task.md.bak-greenfield-20261005").exists())

    def test_missing_agent_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(icl.sync(Path(tmp), BLOCK, True, "20261005"), [])


if __name__ == "__main__":
    unittest.main()
