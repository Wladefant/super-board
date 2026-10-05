"""Tests for checks (f)-(h) of adoption_audit.py (Wladefant/super-board#504)."""
from __future__ import annotations

import unittest
from typing import Dict, Optional

import adoption_audit as aa

GOOD = "# Repo\n\nProject stage: {stage}\n\n## Lessons\n\n{lessons}\n## Other\n\n{extra}\n"


def doc(stage="greenfield", lessons=(), extra=""):
    return GOOD.format(stage=stage, lessons="".join(f"- {l}\n" for l in lessons), extra=extra)


def cats(text):
    return sorted(f.category for f in aa.audit_policy_text("o/r", "main", "AGENTS.md", text))


class StageLine(unittest.TestCase):
    def test_greenfield_and_live_pass(self):
        self.assertEqual(cats(doc("greenfield")), [])
        self.assertEqual(cats(doc("live")), [])

    def test_missing_stage_line_fails(self):
        self.assertEqual(cats(doc().replace("Project stage: greenfield\n", "")), ["stage_line_invalid"])

    def test_two_stage_lines_fail(self):
        self.assertEqual(cats(doc() + "\nProject stage: live\n"), ["stage_line_invalid"])

    def test_unknown_value_fails(self):
        self.assertEqual(cats(doc("staging")), ["stage_line_invalid"])


class LessonsCap(unittest.TestCase):
    def test_twenty_pass_twenty_one_fail(self):
        self.assertEqual(cats(doc(lessons=[f"When {i}, do {i}" for i in range(20)])), [])
        self.assertEqual(cats(doc(lessons=[f"When {i}, do {i}" for i in range(21)])), ["lessons_section_invalid"])

    def test_missing_section_fails(self):
        self.assertEqual(cats("Project stage: live\n"), ["lessons_section_invalid"])


class LiveCompat(unittest.TestCase):
    def test_live_repo_that_permits_skipping_migrations_fails(self):
        self.assertEqual(cats(doc("live", extra="Small changes may skip migrations.")), ["live_repo_permits_skipping_compat"])
        self.assertEqual(cats(doc("live", extra="Breaking changes are fine here.")), ["live_repo_permits_skipping_compat"])

    def test_live_repo_that_forbids_it_passes(self):
        self.assertEqual(cats(doc("live", extra="Never skip migrations. A deploy of code without the migration file fails.")), [])

    def test_greenfield_repo_may_say_it(self):
        self.assertEqual(cats(doc("greenfield", extra="You may skip migrations here.")), [])

    def test_live_repo_that_quotes_the_greenfield_rule_passes(self):
        self.assertEqual(cats(doc("live", extra="A greenfield repo may skip migrations; this one is not.")), [])


class FakeClient:
    def __init__(self, files: Dict[str, str]):
        self.files = files

    def get_file(self, repo: str, path: str, ref: str) -> Optional[str]:
        return self.files.get(f"{repo}@{ref}:{path}")


class AuditRepos(unittest.TestCase):
    def test_falls_back_to_claude_md(self):
        client = FakeClient({"o/a@main:CLAUDE.md": doc()})
        findings, scanned = aa.audit_policy_docs(["o/a@main"], client)
        self.assertEqual((findings, scanned), ([], 1))

    def test_removing_the_stage_line_fails_the_audit(self):
        good = FakeClient({"o/a@main:AGENTS.md": doc()})
        scratch = FakeClient({"o/a@main:AGENTS.md": doc().replace("Project stage: greenfield\n", "")})
        self.assertEqual(aa.audit_policy_docs(["o/a@main"], good)[0], [])
        self.assertEqual([f.category for f in aa.audit_policy_docs(["o/a@main"], scratch)[0]], ["stage_line_invalid"])

    def test_missing_files_are_a_finding_with_a_url(self):
        findings, _ = aa.audit_policy_docs(["o/a@main"], FakeClient({}))
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].url.startswith("https://github.com/o/a"))


if __name__ == "__main__":
    unittest.main()
