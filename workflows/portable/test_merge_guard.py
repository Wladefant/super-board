#!/usr/bin/env python3
"""test_merge_guard.py - contract tests for the lane-brief merge guard (Wladefant/super-board#227).

The guard sits between a lane and `gh pr merge`. These tests pin what it must refuse and
what it must let through, with GitHub replaced by a recorded fake so no network is touched:

  1. Command parsing finds every real merge (plain, URL, path-qualified gh, chained,
     shell-wrapped, `gh api -X PUT .../merge`) and ignores look-alikes (`gh pr view`,
     GET on the merge endpoint, `--disable-auto`, a merge quoted inside `echo`).
  2. The feature-map bookend: required only for non-test product code, satisfied by a
     marker line in the body, a comment or a review, never by a prose mention.
  3. Decisions: enforce blocks, warn only records `would_block`, off never calls GitHub,
     unguarded repos and bases pass, an unreachable GitHub fails closed.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from merge_guard import (  # noqa: E402  (sys.path set above)
    check_command,
    evaluate_feature_map,
    main,
    parse_merge_commands,
    resolve_mode,
)

REPO = "Bavariance/polysimulator"
HEAD = "1234567890abcdef1234567890abcdef12345678"
IMAGES = "\n".join(
    f"![shot-{i}](https://github.com/user-attachments/assets/{i:08d}-1111-2222-3333-{i:012d})" for i in range(2)
)
RECEIPT = {"body": f"QA-RECEIPT: PASS {HEAD}\nBrowser QA.\n{IMAGES}", "html_url": "https://example.test/receipt"}
NOTE = "feature-map: trade panel (frontend/components/trade/*, tests, owner #5600, risk high)"


class FakeGitHub:
    """Answers the gh/git calls merge_guard makes, and records them."""

    def __init__(self, prs=None, fail=False):
        self.prs = prs or {}
        self.fail = fail
        self.calls = []

    def __call__(self, cmd, cwd=None):
        self.calls.append(cmd)
        if cmd[:2] == ["git", "remote"]:
            return 1, "", "not a PolySimulator checkout"
        if self.fail:
            return 1, "", "gh: could not reach api.github.com"
        if cmd[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps({"number": 42, "url": f"https://github.com/{REPO}/pull/42"}), ""
        path = cmd[2]
        for (repo, number), pr in self.prs.items():
            prefix = f"repos/{repo}/pulls/{number}"
            if path == prefix:
                return 0, json.dumps({
                    "state": "open", "merged": False, "body": pr.get("body", ""),
                    "head": {"sha": HEAD}, "base": {"ref": pr.get("base", "staging")},
                }), ""
            if path.startswith(prefix + "/files"):
                return 0, json.dumps([{"filename": name} for name in pr["files"]]), ""
            if path.startswith(f"repos/{repo}/issues/{number}/comments"):
                return 0, json.dumps(pr.get("comments", [])), ""
            if path.startswith(prefix + "/reviews"):
                return 0, json.dumps(pr.get("reviews", [])), ""
        return 1, "", f"unexpected call {cmd}"

    def fetched(self):
        return [cmd for cmd in self.calls if cmd[:1] == ["gh"]]


class CommandParsingTest(unittest.TestCase):
    def targets(self, command):
        return [(t["repo"], t["pr"], t["via"]) for t in parse_merge_commands(command)]

    def test_every_real_merge_form_is_found(self):
        cases = {
            f"gh pr merge 5 --merge -R {REPO}": [(REPO, 5, "gh pr merge")],
            f"gh pr merge https://github.com/{REPO}/pull/77 --merge": [(REPO, 77, "gh pr merge")],
            f"gh pr merge --repo={REPO} '#9' --merge": [(REPO, 9, "gh pr merge")],
            f"C:\\tools\\gh.exe pr merge 8 --merge -R {REPO}": [(REPO, 8, "gh pr merge")],
            f"GH_TOKEN=x gh pr merge 3 -R {REPO} --merge": [(REPO, 3, "gh pr merge")],
            f"git fetch origin && gh pr merge 11 -R {REPO} --merge; echo done": [(REPO, 11, "gh pr merge")],
            f"gh api -X PUT repos/{REPO}/pulls/12/merge -f merge_method=merge": [(REPO, 12, "gh api merge")],
            f"gh api --method=put /repos/{REPO}/pulls/13/merge": [(REPO, 13, "gh api merge")],
            f'powershell -NoProfile -Command "gh pr merge 14 -R {REPO} --merge"': [(REPO, 14, "gh pr merge")],
            "gh pr merge --merge": [(None, None, "gh pr merge")],
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(self.targets(command), expected)

    def test_a_branch_reference_is_kept_for_gh_to_resolve(self):
        [target] = parse_merge_commands(f"gh pr merge feat/x -R {REPO} --merge")
        self.assertEqual((target["pr"], target["ref"]), (None, "feat/x"))

    def test_look_alikes_are_not_merges(self):
        for command in (
            f"gh pr view 5 -R {REPO}",
            f"gh api repos/{REPO}/pulls/5/merge",
            f"gh pr merge 5 -R {REPO} --disable-auto",
            'echo "gh pr merge 5"',
            f"gh pr list --search 'merge' -R {REPO}",
            "git merge origin/staging --no-ff",
            f'gh pr comment 5 -R {REPO} --body "please gh pr merge later"',
        ):
            with self.subTest(command=command):
                self.assertEqual(parse_merge_commands(command), [])


class FeatureMapBookendTest(unittest.TestCase):
    def verdict(self, files, body="", comments=(), reviews=()):
        pr = {"files": [{"path": f} for f in files], "body": body,
              "comments": list(comments), "reviews": list(reviews)}
        return evaluate_feature_map(pr)[0]

    def test_only_non_test_product_code_needs_a_note(self):
        self.assertEqual(self.verdict(["docs/runbook.md", "scripts/qa/x.py"]), "EXEMPT")
        self.assertEqual(self.verdict(["frontend/components/__tests__/Ticket.test.tsx"]), "EXEMPT")
        self.assertEqual(self.verdict(["backend/tests/test_orders.py"]), "EXEMPT")
        self.assertEqual(self.verdict(["backend/app/trading.py"]), "REQUIRED")
        self.assertEqual(self.verdict(["docs/a.md", "frontend/app/page.tsx"]), "REQUIRED")

    def test_a_marker_line_anywhere_on_the_pr_satisfies_it(self):
        files = ["frontend/app/page.tsx"]
        self.assertEqual(self.verdict(files, body=f"## Summary\n{NOTE}"), "PASSED")
        self.assertEqual(self.verdict(files, body="- **feature-map:** no match for hub card"), "PASSED")
        self.assertEqual(self.verdict(files, comments=[{"body": f"> {NOTE}"}]), "PASSED")
        self.assertEqual(self.verdict(files, reviews=[{"body": NOTE}]), "PASSED")

    def test_a_prose_mention_or_empty_marker_does_not(self):
        files = ["frontend/app/page.tsx"]
        self.assertEqual(self.verdict(files, body="I skipped the feature-map: step here"), "REQUIRED")
        self.assertEqual(self.verdict(files, body="feature-map:   \nnext line"), "REQUIRED")
        self.assertEqual(self.verdict(files, body="- **feature-map:**\n- next item"), "REQUIRED")


class DecisionTest(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="merge-guard-test-"))

    def decide(self, command, github, mode="enforce"):
        return check_command(command, None, mode=mode, runner=github, state_dir=self.state)

    def ui_pr(self, **extra):
        return {"files": ["frontend/components/OrderTicket.tsx"], **extra}

    def test_a_ui_merge_missing_both_bookends_is_blocked_with_both_reasons(self):
        github = FakeGitHub({(REPO, 42): self.ui_pr()})
        decision = self.decide(f"gh pr merge 42 -R {REPO} --merge", github)
        self.assertTrue(decision["block"])
        [entry] = decision["merges"]
        self.assertEqual(entry["qa_receipt"]["verdict"], "REQUIRED")
        self.assertEqual(entry["feature_map"]["verdict"], "REQUIRED")
        self.assertIn("QA receipt required", decision["reason"])
        self.assertIn("feature-map note required", decision["reason"])
        self.assertIn("Bavariance/polysimulator#42", decision["reason"])

    def test_each_bookend_alone_still_blocks(self):
        receipt_only = FakeGitHub({(REPO, 42): self.ui_pr(comments=[RECEIPT])})
        note_only = FakeGitHub({(REPO, 42): self.ui_pr(body=NOTE)})
        for github, missing in ((receipt_only, "feature_map"), (note_only, "qa_receipt")):
            with self.subTest(missing=missing):
                decision = self.decide(f"gh pr merge 42 -R {REPO} --merge", github)
                self.assertTrue(decision["block"])
                self.assertEqual(decision["merges"][0][missing]["verdict"], "REQUIRED")

    def test_a_merge_carrying_both_bookends_passes(self):
        github = FakeGitHub({(REPO, 42): self.ui_pr(body=NOTE, comments=[RECEIPT])})
        decision = self.decide(f"gh pr merge 42 -R {REPO} --merge", github)
        self.assertFalse(decision["block"])
        self.assertNotIn("would_block", decision)
        self.assertEqual(decision["merges"][0]["qa_receipt"]["verdict"], "PASSED")

    def test_a_receipt_for_another_revision_does_not_count(self):
        stale = dict(RECEIPT, body=RECEIPT["body"].replace(HEAD, "f" * 40))
        github = FakeGitHub({(REPO, 42): self.ui_pr(body=NOTE, comments=[stale])})
        decision = self.decide(f"gh pr merge 42 -R {REPO} --merge", github)
        self.assertTrue(decision["block"])

    def test_warn_mode_records_but_never_blocks(self):
        github = FakeGitHub({(REPO, 42): self.ui_pr()})
        decision = self.decide(f"gh pr merge 42 -R {REPO} --merge", github, mode="warn")
        self.assertFalse(decision["block"])
        self.assertTrue(decision["would_block"])

    def test_off_mode_never_calls_github(self):
        github = FakeGitHub({(REPO, 42): self.ui_pr()})
        decision = self.decide(f"gh pr merge 42 -R {REPO} --merge", github, mode="off")
        self.assertFalse(decision["block"])
        self.assertEqual(github.fetched(), [])

    def test_unguarded_repo_and_base_pass(self):
        github = FakeGitHub({(REPO, 42): self.ui_pr(base="main")})
        self.assertFalse(self.decide(f"gh pr merge 42 -R {REPO} --merge", github)["block"])
        other = FakeGitHub()
        decision = self.decide("gh pr merge 5 -R Wladefant/super-board --merge", other)
        self.assertFalse(decision["block"])
        self.assertEqual(other.fetched(), [])

    def test_backend_docs_only_merge_needs_no_bookend(self):
        github = FakeGitHub({(REPO, 42): {"files": ["docs/internal/x.md", "AGENTS.md"]}})
        self.assertFalse(self.decide(f"gh pr merge 42 -R {REPO} --merge", github)["block"])

    def test_non_trading_backend_needs_the_note_but_no_receipt(self):
        github = FakeGitHub({(REPO, 42): {"files": ["backend/app/api_v1/keys.py"]}})
        decision = self.decide(f"gh pr merge 42 -R {REPO} --merge", github)
        self.assertTrue(decision["block"])
        self.assertEqual(decision["merges"][0]["qa_receipt"]["verdict"], "EXEMPT")
        self.assertEqual(decision["merges"][0]["feature_map"]["verdict"], "REQUIRED")

    def test_an_unreachable_github_fails_closed(self):
        decision = self.decide(f"gh pr merge 42 -R {REPO} --merge", FakeGitHub(fail=True))
        self.assertTrue(decision["block"])
        self.assertIn("fails closed", decision["reason"])

    def test_a_bare_merge_resolves_the_pr_from_the_checkout(self):
        github = FakeGitHub({(REPO, 42): self.ui_pr(body=NOTE, comments=[RECEIPT])})
        decision = self.decide("gh pr merge --merge", github)
        self.assertEqual(decision["merges"][0]["pr"], 42)
        self.assertFalse(decision["block"])

    def test_every_evaluated_decision_is_logged(self):
        github = FakeGitHub({(REPO, 42): self.ui_pr()})
        self.decide(f"gh pr merge 42 -R {REPO} --merge", github, mode="warn")
        self.decide("git status", github)
        lines = (self.state / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(json.loads(lines[0])["would_block"])


class ModeTest(unittest.TestCase):
    def test_env_then_file_then_enforce(self):
        state = Path(tempfile.mkdtemp(prefix="merge-guard-mode-"))
        self.assertEqual(resolve_mode({}, state), "enforce")
        (state / "mode").write_text("warn\n", encoding="utf-8")
        self.assertEqual(resolve_mode({}, state), "warn")
        self.assertEqual(resolve_mode({"SUPERBOARD_MERGE_GUARD_MODE": "off"}, state), "off")
        (state / "mode").write_text("disable-please", encoding="utf-8")
        self.assertEqual(resolve_mode({}, state), "enforce")

    def test_cli_prints_a_decision_for_a_non_merge(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["check-command", "--command", "git status", "--mode", "enforce"]), 0)
        self.assertFalse(json.loads(out.getvalue())["block"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
