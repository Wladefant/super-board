#!/usr/bin/env python3
"""test_install_managed_skills.py - repo skills reach the profile; local edits are backed up, never lost."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("inst", Path(__file__).with_name("install-managed-skills.py"))
inst = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inst)


class Installer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src, self.dest, self.bak = root / "repo", root / "profile", root / "bak"
        (self.src / "alpha" / "refs").mkdir(parents=True)
        (self.src / "alpha" / "SKILL.md").write_text("alpha v2\n", encoding="utf-8")
        (self.src / "alpha" / "refs" / "notes.md").write_text("notes\n", encoding="utf-8")
        (self.src / "beta").mkdir()
        (self.src / "beta" / "SKILL.md").write_text("beta\n", encoding="utf-8")
        (self.src / "README.md").write_text("catalog\n", encoding="utf-8")
        (self.dest / "alpha").mkdir(parents=True)
        (self.dest / "alpha" / "SKILL.md").write_text("alpha local edit\n", encoding="utf-8")
        (self.dest / "alpha" / "local-only.md").write_text("keep\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_inst(self, *extra):
        return inst.main([*extra, "--src", str(self.src), "--dest", str(self.dest), "--backup-dir", str(self.bak)])

    def test_installs_every_skill_and_backs_up_the_local_edit(self):
        self.assertEqual(self.run_inst(), 0)
        self.assertEqual((self.dest / "alpha" / "SKILL.md").read_text(encoding="utf-8"), "alpha v2\n")
        self.assertEqual((self.dest / "alpha" / "refs" / "notes.md").read_text(encoding="utf-8"), "notes\n")
        self.assertEqual((self.dest / "beta" / "SKILL.md").read_text(encoding="utf-8"), "beta\n")
        saved = list(self.bak.rglob("SKILL.md"))
        self.assertEqual([p.read_text(encoding="utf-8") for p in saved], ["alpha local edit\n"])
        self.assertEqual((self.dest / "alpha" / "local-only.md").read_text(encoding="utf-8"), "keep\n")
        self.assertFalse((self.dest / "README.md").exists())

    def test_named_skill_only(self):
        self.run_inst("beta")
        self.assertTrue((self.dest / "beta" / "SKILL.md").is_file())
        self.assertEqual((self.dest / "alpha" / "SKILL.md").read_text(encoding="utf-8"), "alpha local edit\n")

    def test_check_reports_drift_and_writes_nothing(self):
        self.assertEqual(self.run_inst("--check"), 1)
        self.assertEqual((self.dest / "alpha" / "SKILL.md").read_text(encoding="utf-8"), "alpha local edit\n")
        self.assertFalse((self.dest / "beta").exists())
        self.run_inst()
        self.assertEqual(self.run_inst("--check"), 0)

    def test_line_endings_are_not_drift(self):
        self.run_inst()
        (self.dest / "beta" / "SKILL.md").write_bytes(b"beta\r\n")
        self.assertEqual(self.run_inst("--check"), 0)

    def test_unknown_name_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_inst("gamma")


if __name__ == "__main__":
    unittest.main()
