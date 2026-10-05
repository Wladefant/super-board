"""Tests for lessons.py (Wladefant/super-board#503)."""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import lessons

BASE = "# Repo\n\nProject stage: greenfield\n\n## Lessons\n\nNewest first.\n\n{items}\n## Other\n\ntext\n"


def doc(*items, stage="greenfield"):
    body = "".join(f"- {i}\n" for i in items)
    return BASE.format(items=body).replace("greenfield", stage, 1)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = lessons.main(argv)
    return code, out.getvalue() + err.getvalue()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, text, name="AGENTS.md", newline="\n"):
        path = self.dir / name
        path.write_bytes(text.replace("\n", newline).encode("utf-8"))
        return path


class AddTests(Fixture):
    def test_newest_line_goes_first(self):
        path = self.write(doc("When A happens, do alpha work"))
        code, _ = run(["add", str(self.dir), "When B appears, do beta thing"])
        self.assertEqual(code, 0)
        _, _, got = lessons.locate(path.read_text(encoding="utf-8"))
        self.assertEqual([t for _, t in got], ["When B appears, do beta thing", "When A happens, do alpha work"])

    def test_empty_section_gets_first_line_before_next_heading(self):
        path = self.write(doc())
        self.assertEqual(run(["add", str(path), "When X, do Y now"])[0], 0)
        text = path.read_text(encoding="utf-8")
        self.assertLess(text.index("- When X"), text.index("## Other"))
        self.assertIn("\n\n## Other", text)

    def test_duplicate_is_rejected_and_old_line_shown(self):
        path = self.write(doc("When a lane leaves an alias, do delete the alias and update callers"))
        before = path.read_bytes()
        code, out = run(["add", str(path), "When a lane leaves an alias, do delete alias and update every caller"])
        self.assertEqual(code, 2)
        self.assertIn("old: When a lane leaves an alias", out)
        self.assertEqual(path.read_bytes(), before)

    def test_line_21_prints_a_proposal_and_edits_nothing(self):
        items = [f"When topic{i} number{i} breaks, do fix{i} carefully" for i in range(20)]
        path = self.write(doc(*items))
        before = path.read_bytes()
        code, out = run(["add", str(path), "When brand new thing, do something else entirely"])
        self.assertEqual(code, 4)
        self.assertIn("Merge proposal", out)
        self.assertEqual(path.read_bytes(), before)

    def test_live_repo_is_refused_without_pr_branch_and_allowed_with_it(self):
        path = self.write(doc(stage="live"))
        before = path.read_bytes()
        self.assertEqual(run(["add", str(path), "When X, do Y now"])[0], 3)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(run(["add", str(path), "When X, do Y now", "--pr-branch"])[0], 0)

    def test_missing_stage_line_counts_as_live(self):
        path = self.write("## Lessons\n\n")
        self.assertEqual(run(["add", str(path), "When X, do Y now"])[0], 3)

    def test_bad_format_rejected(self):
        path = self.write(doc())
        self.assertEqual(run(["add", str(path), "always be careful"])[0], 5)

    def test_crlf_file_stays_crlf(self):
        path = self.write(doc("When A happens, do alpha work"), newline="\r\n")
        self.assertEqual(run(["add", str(path), "When B appears, do beta thing"])[0], 0)
        raw = path.read_bytes()
        self.assertNotIn(b"\r\r", raw)
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))


class CheckTests(Fixture):
    def test_clean_file_exits_zero(self):
        path = self.write(doc("When A happens, do alpha work"))
        self.assertEqual(run(["check", str(path)])[0], 0)

    def test_empty_section_is_fine(self):
        self.assertEqual(run(["check", str(self.write(doc()))])[0], 0)

    def test_over_cap_fails(self):
        path = self.write(doc(*[f"When topic{i} number{i} breaks, do fix{i} carefully" for i in range(21)]))
        code, out = run(["check", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("21/20", out)

    def test_near_duplicate_pair_fails(self):
        path = self.write(doc("When a lane leaves an alias, do delete the alias", "When lane leaves alias, do delete alias now"))
        code, out = run(["check", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("near-duplicate", out)

    def test_missing_section_fails(self):
        self.assertEqual(run(["check", str(self.write("# no lessons\n"))])[0], 1)

    def test_falls_back_to_claude_md(self):
        self.write(doc(), name="CLAUDE.md")
        self.assertEqual(run(["check", str(self.dir)])[0], 0)


if __name__ == "__main__":
    unittest.main()
