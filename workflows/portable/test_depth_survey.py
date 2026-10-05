#!/usr/bin/env python3
"""
test_depth_survey.py - contracts of the report-only module-depth survey (issue #514).

Each test builds a throwaway git repo and checks an observable behaviour:
  - a pass-through module is reported with deletion test `pass-through`, Strong when hot
  - a deep module is not reported
  - the survey never edits a file
  - an ADR that records a rejected candidate suppresses it
  - filing dedupes by fingerprint and only files Strong candidates
  - the HTML report is offline (no remote reference, no script) and every card shows a deletion-test result
  - `gardener.py --survey-depth` path runs end to end, dry-run, and writes the report
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import depth_survey  # noqa: E402
from gardener import run_depth_survey_cli  # noqa: E402

REPO_ROOT = Path(SCRIPT_DIR).parents[1]
TEMPLATE = REPO_ROOT / "skills" / "improve-codebase-architecture" / "report-template.html"

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

WRAPPER = '''def fetch(order_id, db):
    return real_fetch(order_id, db)

def save(order, db):
    return real_save(order, db)

def drop(order_id, db):
    return real_drop(order_id, db)
'''

DEEP = '''def price(cart):
    total = 0
    for line in cart:
        qty = line["qty"]
        if qty > 10:
            total += qty * line["unit"] * 0.9
        else:
            total += qty * line["unit"]
    discount = 0
    if total > 1000:
        discount = total * 0.05
    tax = (total - discount) * 0.2
    rounded = round(total - discount + tax, 2)
    return rounded

def refund(cart, reason):
    amount = price(cart)
    if reason == "damaged":
        amount = amount * 1.0
    elif reason == "late":
        amount = amount * 0.5
    else:
        amount = 0
    return amount
'''


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=ENV)


def make_repo(tmp: Path) -> Path:
    git(tmp, "init", "-q")
    (tmp / "pkg").mkdir()
    (tmp / "pkg" / "orders_repo.py").write_text(WRAPPER, encoding="utf-8")
    (tmp / "pkg" / "pricing.py").write_text(DEEP, encoding="utf-8")
    for n in range(3):
        (tmp / "pkg" / f"caller{n}.py").write_text("from pkg.orders_repo import fetch\nx = fetch(1, None)\n", encoding="utf-8")
    git(tmp, "add", "-A")
    git(tmp, "commit", "-q", "-m", "init")
    for n in range(6):  # make the wrapper and the deep module hot
        for name in ("orders_repo", "pricing"):
            p = tmp / "pkg" / f"{name}.py"
            p.write_text(p.read_text(encoding="utf-8") + f"\n# edit {n}\n", encoding="utf-8")
        git(tmp, "commit", "-q", "-am", f"edit {n}")
    return tmp


def tree_state(root: Path):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and ".git" not in p.parts}


class TestDepthSurvey(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_repo(Path(self._tmp.name))
        self._real_gh = depth_survey._gh
        depth_survey._gh = lambda *a, **k: (1, "")  # no network in tests
        self.addCleanup(setattr, depth_survey, "_gh", self._real_gh)

    def test_passthrough_is_reported_strong_and_deep_module_is_not(self):
        sv = depth_survey.survey(self.root)
        by_path = {c.path: c for c in sv.candidates}
        wrapper = by_path["pkg/orders_repo.py"]
        self.assertEqual(wrapper.deletion_test, "pass-through")
        self.assertEqual(wrapper.strength, "Strong")
        self.assertGreaterEqual(wrapper.callers, 3)
        self.assertNotIn("pkg/pricing.py", by_path)

    def test_survey_edits_no_file(self):
        before = tree_state(self.root)
        depth_survey.survey(self.root)
        self.assertEqual(before, tree_state(self.root))

    def test_adr_recording_a_rejection_suppresses_the_candidate(self):
        adr = self.root / "docs" / "adr"
        adr.mkdir(parents=True)
        (adr / "0001.md").write_text("Rejected: keep pkg/orders_repo.py as the persistence seam.", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "adr")
        sv = depth_survey.survey(self.root)
        self.assertNotIn("pkg/orders_repo.py", [c.path for c in sv.candidates])
        self.assertIn("pkg/orders_repo.py", sv.skipped_adr)

    def test_filing_is_deduped_and_strong_only(self):
        sv = depth_survey.survey(self.root)
        strong = [c for c in sv.candidates if c.strength == "Strong"]
        self.assertTrue(strong)
        first = depth_survey.file_candidates(sv, "o/r", dry_run=True, seen=set())
        self.assertEqual(len(first), len(strong))
        again = depth_survey.file_candidates(sv, "o/r", dry_run=True, seen={c.fingerprint for c in strong})
        self.assertEqual(again, [])

    def test_report_is_offline_and_every_card_has_a_deletion_test(self):
        sv = depth_survey.survey(self.root)
        page = depth_survey.render_report(sv, TEMPLATE)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("<script", page)
        self.assertEqual(page.count('class="candidate"'), page.count("Deletion test:"))
        self.assertGreater(page.count("Deletion test:"), 0)

    def test_gardener_survey_path_writes_report_in_dry_run(self):
        outer = tempfile.TemporaryDirectory()
        self.addCleanup(outer.cleanup)
        out = Path(outer.name) / "out"
        before = tree_state(self.root)
        rc = run_depth_survey_cli(self.root, "o/r", live=False, out_dir=out)
        self.assertEqual(rc, 0)
        self.assertEqual(len(list(out.glob("architecture-review-*.html"))), 1)
        self.assertEqual(before, tree_state(self.root))

    def test_live_filing_refuses_when_dedupe_cannot_be_read(self):
        sv = depth_survey.survey(self.root)
        with self.assertRaises(RuntimeError):
            depth_survey.file_candidates(sv, "o/r", dry_run=False)


if __name__ == "__main__":
    unittest.main()
