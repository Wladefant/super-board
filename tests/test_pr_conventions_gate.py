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


def run_evaluate_pr(pr_dict, sub_issues=None, fetcher_error=None):
    script = """
const fs = require('fs');
const { evaluatePR } = require('./.github/scripts/pr-conventions-gate.cjs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const options = {};
if (input.fetcher_error) {
    options.subIssuesFetcher = () => { throw new Error(input.fetcher_error); };
} else {
    options.subIssuesFetcher = () => input.sub_issues;
}
const res = evaluatePR(input.pr, options);
console.log(JSON.stringify(res));
"""
    payload = json.dumps({"pr": pr_dict, "sub_issues": sub_issues or [], "fetcher_error": fetcher_error})
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

    def test_refuse_all_closing_keyword_variants_on_parent_with_open_sub_issues(self):
        """Probe strings from review finding 2: Fixed, Close, Closes:, Resolved, fix, full URL, owner/repo."""
        probe_keywords = [
            "Fixed #227",
            "Close #227",
            "Closes: #227",
            "Closes:#227",
            "Resolved #227",
            "fix #227",
            "resolve #227",
            "resolves: #227",
            "closed #227",
            "Fixed Wladefant/super-board#227",
            "Closes: https://github.com/Wladefant/super-board/issues/227",
        ]
        sub_issues = [{"number": 245, "title": "Open child task", "state": "open"}]
        for probe in probe_keywords:
            with self.subTest(probe=probe):
                pr = {
                    "number": 10,
                    "labels": ["kind:feature", "area:workflow"],
                    "body": f"## Linked Issue\n{probe}\n\n## Summary\nWorking on parent.",
                    "milestone": {"title": "Phase 2 - Tooling + quota"},
                    "additions": 10,
                    "deletions": 5,
                }
                res = run_evaluate_pr(pr, sub_issues)
                failures = [f["check"] for f in res["failures"]]
                self.assertIn(
                    "Parent Issue Closing Keyword Guard",
                    failures,
                    f"Probe '{probe}' did not trigger Parent Issue Closing Keyword Guard",
                )

    def test_refuse_closing_keyword_paired_with_valid_reference(self):
        """Probe strings paired with valid references must not bypass guard."""
        paired_probes = [
            "Refs #245\nFixed #227",
            "Part of #227\nCloses: #227",
            "Refs #245\nclose #227",
            "Part of #227\nFixes: #227",
        ]
        sub_issues = [{"number": 245, "title": "Open child task", "state": "open"}]
        for probe in paired_probes:
            with self.subTest(probe=probe):
                pr = {
                    "number": 10,
                    "labels": ["kind:feature", "area:workflow"],
                    "body": f"## Linked Issue\n{probe}\n\n## Summary\nWorking on parent.",
                    "milestone": {"title": "Phase 2 - Tooling + quota"},
                    "additions": 10,
                    "deletions": 5,
                }
                res = run_evaluate_pr(pr, sub_issues)
                failures = [f["check"] for f in res["failures"]]
                self.assertIn(
                    "Parent Issue Closing Keyword Guard",
                    failures,
                    f"Paired probe '{probe}' bypassed Parent Issue Closing Keyword Guard",
                )

    def test_guard_fails_closed_on_fetcher_error(self):
        """Finding 1 & 3: Failed sub-issues lookup must fail closed, treating error as guard failure."""
        pr = {
            "number": 10,
            "labels": ["kind:feature", "area:workflow"],
            "body": "## Linked Issue\nFixes #227\n\n## Summary\nWorking on parent.",
            "milestone": {"title": "Phase 2 - Tooling + quota"},
            "additions": 10,
            "deletions": 5,
        }
        res = run_evaluate_pr(pr, fetcher_error="gh api authentication failed (HTTP 401)")
        failures = [f["check"] for f in res["failures"]]
        self.assertIn("Parent Issue Closing Keyword Guard", failures)
        fail_obj = next(f for f in res["failures"] if f["check"] == "Parent Issue Closing Keyword Guard")
        self.assertIn("Guard fails closed", fail_obj["message"])
        self.assertIn("HTTP 401", fail_obj["message"])

    def test_allow_closing_keyword_on_leaf_issue_without_sub_issues(self):
        """Leaf issues without sub-issues pass cleanly with closing keyword."""
        pr = {
            "number": 10,
            "labels": ["kind:feature", "area:workflow"],
            "body": "## Linked Issue\nFixes #245\n\n## Summary\nClosing leaf issue.",
            "milestone": {"title": "Phase 2 - Tooling + quota"},
            "additions": 10,
            "deletions": 5,
        }
        res = run_evaluate_pr(pr, sub_issues=[])
        self.assertEqual(len(res["failures"]), 0)
        self.assertTrue(any("Fixes #245" in p for p in res["passes"]))

    def test_no_false_positive_on_non_closing_word_with_substring(self):
        """Words like 'prefixes #227' must not trigger closing keyword guard."""
        pr = {
            "number": 10,
            "labels": ["kind:feature", "area:workflow"],
            "body": "## Linked Issue\nPart of #227\n\n## Summary\nWe added prefixes #227.",
            "milestone": {"title": "Phase 2 - Tooling + quota"},
            "additions": 10,
            "deletions": 5,
        }
        sub_issues = [{"number": 245, "title": "Child task", "state": "open"}]
        res = run_evaluate_pr(pr, sub_issues)
        failures = [f["check"] for f in res["failures"]]
        self.assertNotIn("Parent Issue Closing Keyword Guard", failures)


if __name__ == "__main__":
    unittest.main(verbosity=2)
