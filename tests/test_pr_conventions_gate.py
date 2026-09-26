#!/usr/bin/env python3
"""
Unit tests for PR conventions gate parent-close guards (.github/scripts/pr-conventions-gate.cjs).
Verifies:
  1. Refused close keyword (Fixes/Closes/Resolves #N) when parent issue has open sub-issues.
  2. Allowed close keyword when all sub-issues are closed.
  3. Allowed non-closing reference (Refs #N, Part of #N) when parent issue has open sub-issues.
"""

import json
import subprocess
import unittest


def run_evaluate_pr(pr_dict, sub_issues=None):
    script = """
const fs = require('fs');
const { evaluatePR } = require('./.github/scripts/pr-conventions-gate.cjs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const res = evaluatePR(input.pr, { subIssuesFetcher: () => input.sub_issues });
console.log(JSON.stringify(res));
"""
    payload = json.dumps({"pr": pr_dict, "sub_issues": sub_issues or []})
    proc = subprocess.run(["node", "-e", script], input=payload, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


class TestPRConventionsGate(unittest.TestCase):
    def test_refuse_closing_keyword_on_parent_with_open_sub_issues(self):
        pr = {
            "number": 10,
            "labels": ["kind:feature", "area:workflow"],
            "body": "## Linked Issue\nFixes #227\n\n## Summary\nWorking on parent.",
            "milestone": {"title": "M1"},
            "additions": 10,
            "deletions": 5,
        }
        sub_issues = [{"number": 262, "title": "Child task", "state": "open"}]
        res = run_evaluate_pr(pr, sub_issues)
        failures = [f["check"] for f in res["failures"]]
        self.assertIn("Parent Issue Closing Keyword Guard", failures)
        fail_obj = next(f for f in res["failures"] if f["check"] == "Parent Issue Closing Keyword Guard")
        self.assertIn("Parent issues cannot be closed while sub-issues remain open", fail_obj["remedy"])
        self.assertIn("#227", fail_obj["message"])
        self.assertIn("#262", fail_obj["message"])

    def test_allow_closing_keyword_when_all_sub_issues_closed(self):
        pr = {
            "number": 10,
            "labels": ["kind:feature", "area:workflow"],
            "body": "## Linked Issue\nFixes #227\n\n## Summary\nClosing parent.",
            "milestone": {"title": "M1"},
            "additions": 10,
            "deletions": 5,
        }
        sub_issues = [{"number": 262, "title": "Child task", "state": "closed"}]
        res = run_evaluate_pr(pr, sub_issues)
        failures = [f["check"] for f in res["failures"]]
        self.assertNotIn("Parent Issue Closing Keyword Guard", failures)
        self.assertEqual(len(res["failures"]), 0)

    def test_allow_non_closing_reference_on_parent_with_open_sub_issues(self):
        pr = {
            "number": 10,
            "labels": ["kind:feature", "area:workflow"],
            "body": "## Linked Issue\nRefs #227\n\n## Summary\nChild task referencing parent.",
            "milestone": {"title": "M1"},
            "additions": 10,
            "deletions": 5,
        }
        sub_issues = [{"number": 262, "title": "Child task", "state": "open"}]
        res = run_evaluate_pr(pr, sub_issues)
        self.assertEqual(len(res["failures"]), 0)
        self.assertTrue(any("Refs #227" in p for p in res["passes"]))

    def test_allow_part_of_reference_on_parent_with_open_sub_issues(self):
        pr = {
            "number": 10,
            "labels": ["kind:feature", "area:workflow"],
            "body": "## Linked Issue\nPart of #227\n\n## Summary\nChild task referencing parent.",
            "milestone": {"title": "M1"},
            "additions": 10,
            "deletions": 5,
        }
        sub_issues = [{"number": 262, "title": "Child task", "state": "open"}]
        res = run_evaluate_pr(pr, sub_issues)
        self.assertEqual(len(res["failures"]), 0)
        self.assertTrue(any("Part of #227" in p for p in res["passes"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
