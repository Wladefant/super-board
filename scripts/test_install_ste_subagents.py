#!/usr/bin/env python3
"""test_install_ste_subagents.py - the installer adds the block once and leaves other blocks alone."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("inst", Path(__file__).with_name("install-ste-subagents.py"))
inst = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inst)

OTHER = "<!-- change-loop:begin -->\nkeep me\n<!-- change-loop:end -->"


class Installer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "task.md").write_text("---\nname: task\n---\nBody.\n" + OTHER + "\n", encoding="utf-8")
        (self.dir / "task.md.bak-1").write_text("old\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_adds_block_once_and_keeps_other_blocks(self):
        self.assertEqual(inst.main(["--dir", str(self.dir)]), 0)
        first = (self.dir / "task.md").read_text(encoding="utf-8")
        self.assertEqual(first.count(inst.BEGIN), 1)
        self.assertIn(OTHER, first)
        inst.main(["--dir", str(self.dir)])
        self.assertEqual((self.dir / "task.md").read_text(encoding="utf-8"), first)

    def test_backups_are_skipped(self):
        inst.main(["--dir", str(self.dir)])
        self.assertEqual((self.dir / "task.md.bak-1").read_text(encoding="utf-8"), "old\n")

    def test_check_mode_reports_and_writes_nothing(self):
        before = (self.dir / "task.md").read_text(encoding="utf-8")
        self.assertEqual(inst.main(["--dir", str(self.dir), "--check"]), 1)
        self.assertEqual((self.dir / "task.md").read_text(encoding="utf-8"), before)
        inst.main(["--dir", str(self.dir)])
        self.assertEqual(inst.main(["--dir", str(self.dir), "--check"]), 0)

    def test_stale_line_is_replaced_in_place(self):
        (self.dir / "task.md").write_text("Body.\n" + inst.BEGIN + "\nold line\n" + inst.END + "\n", encoding="utf-8")
        inst.main(["--dir", str(self.dir)])
        text = (self.dir / "task.md").read_text(encoding="utf-8")
        self.assertNotIn("old line", text)
        self.assertIn("ste_check.py", text)


if __name__ == "__main__":
    unittest.main()
