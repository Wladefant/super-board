#!/usr/bin/env python3
"""test_close_guard.py - the close guard (super-board#440).

Fixtures `fixtures/close_guard_shipnovo_*.json` are real issues read from
Wladefant/shipnovo on 2026-10-04:
  - 210: open plan issue with an unchecked box in its body.
  - 209: issue that was closed 2 s after it was created, body fully ticked.
  - 55 : issue closed 90 s after creation.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

import close_guard
from close_guard import CloseRefused, guard_close, unchecked_boxes
from adoption_audit import GitHubClient, audit_premature_closes, format_markdown_report, run_adoption_audit
from ledger import RequestLedger
from project_adapter import SuperboardProjectUpdater, create_polysimulator_config

FIXTURES = os.path.join(TEST_DIR, "fixtures")
REPO = "Wladefant/shipnovo"
HEAD = "a" * 40
NOW = datetime.now(timezone.utc)  # ledger/adapter paths read the real clock; keep fixtures relative to it


def load(n: int) -> dict:
    with open(os.path.join(FIXTURES, f"close_guard_shipnovo_{n}.json"), encoding="utf-8") as fh:
        return json.load(fh)


def aged(issue: dict, age_minutes: float, **overrides) -> dict:
    """Copy of a real issue whose creation time is `age_minutes` before NOW."""
    out = dict(issue)
    out["created_at"] = (NOW - timedelta(minutes=age_minutes)).isoformat().replace("+00:00", "Z")
    out.update(overrides)
    return out


class TestBoxDetection(unittest.TestCase):
    def test_real_issue_with_open_box_is_detected(self):
        self.assertGreaterEqual(len(unchecked_boxes(load(210)["body"])), 1)

    def test_real_fully_ticked_issue_has_no_open_box(self):
        self.assertEqual(unchecked_boxes(load(209)["body"]), [])

    def test_box_syntax_variants_and_code_fences(self):
        body = "- [x] done\n* [ ] star\n1. [ ] numbered\n```\n- [ ] inside fence\n```\ntext - [ ] midline\n"
        self.assertEqual(len(unchecked_boxes(body)), 2)


class TestGuardClose(unittest.TestCase):
    def test_body_with_unchecked_box_is_refused_even_when_old(self):
        issue = aged(load(210), age_minutes=24 * 60)
        with self.assertRaises(CloseRefused) as ctx:
            guard_close(REPO, 210, fetcher=lambda r, n: issue, now=NOW)
        self.assertIn("unchecked box", str(ctx.exception))

    def test_all_ticked_body_older_than_ten_minutes_passes(self):
        issue = aged(load(209), age_minutes=11)
        result = guard_close(REPO, 209, fetcher=lambda r, n: issue, now=NOW)
        self.assertEqual(result, {"allowed": True, "forced": False, "overridden": []})

    def test_just_created_issue_is_refused_even_with_ticked_body(self):
        issue = aged(load(209), age_minutes=0.5)
        with self.assertRaises(CloseRefused) as ctx:
            guard_close(REPO, 209, fetcher=lambda r, n: issue, now=NOW)
        self.assertIn("old", str(ctx.exception))

    def test_real_issue_closed_90s_after_creation_would_have_been_refused(self):
        real = load(55)
        at_close = datetime.fromisoformat(real["closed_at"].replace("Z", "+00:00"))
        with self.assertRaises(CloseRefused):
            guard_close(REPO, 55, fetcher=lambda r, n: real, now=at_close)

    def test_issue_created_in_this_run_is_refused_even_when_clock_says_old(self):
        issue = aged(load(209), age_minutes=60)
        close_guard._reset_created_for_tests()
        close_guard.note_created(REPO, 209)
        try:
            with self.assertRaises(CloseRefused) as ctx:
                guard_close(REPO, 209, fetcher=lambda r, n: issue, now=NOW)
            self.assertIn("same run", str(ctx.exception))
        finally:
            close_guard._reset_created_for_tests()

    def test_lookup_failure_fails_closed(self):
        def boom(repo, num):
            raise RuntimeError("gh down")

        with self.assertRaises(CloseRefused):
            guard_close(REPO, 209, fetcher=boom, now=NOW)

    def test_missing_creation_time_fails_closed(self):
        issue = aged(load(209), 60)
        issue["created_at"] = None
        with self.assertRaises(CloseRefused):
            guard_close(REPO, 209, fetcher=lambda r, n: issue, now=NOW)

    def test_force_close_records_reason_in_comment_before_allowing(self):
        issue = aged(load(210), 60)
        comments = []
        result = guard_close(
            REPO, 210, force_reason="duplicate of #209", actor="Lane1",
            fetcher=lambda r, n: issue, commenter=lambda r, n, b: comments.append((r, n, b)), now=NOW,
        )
        self.assertTrue(result["forced"])
        self.assertEqual(len(comments), 1)
        self.assertIn("duplicate of #209", comments[0][2])
        self.assertIn("unchecked box", comments[0][2])

    def test_force_close_refused_when_comment_cannot_be_posted(self):
        issue = aged(load(210), 60)

        def fail(repo, num, body):
            raise RuntimeError("403")

        with self.assertRaises(CloseRefused):
            guard_close(REPO, 210, force_reason="x", fetcher=lambda r, n: issue, commenter=fail, now=NOW)

    def test_blank_force_reason_is_not_an_override(self):
        issue = aged(load(210), 60)
        with self.assertRaises(CloseRefused):
            guard_close(REPO, 210, force_reason="   ", fetcher=lambda r, n: issue, now=NOW)


class TestLedgerCloseGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ledger = RequestLedger(ledger_path=os.path.join(self.tmp, "ledger.json"),
                                    sub_issues_checker=lambda r, n: [])
        self.ledger.add_request(
            req_id="req-1", prompt="p", session="s", project="shipnovo",
            acceptance_criteria=["works"], owner="Lane", state="review", task_type="local_doc",
            head=HEAD, github_repo=REPO, issue_number=210,
        )
        self.ledger.update_request(
            "req-1",
            criterion_update={"id": "AC-1", "status": "verified", "evidence": "observed", "verified_head": HEAD},
            github_update={"issue_number": 210, "repo": REPO, "proof_url": f"https://github.com/{REPO}/pull/1",
                           "proof_verified": True},
            actor="Lane",
        )

    def _done(self, **kw):
        return self.ledger.update_request("req-1", state="done", actor="Lane", **kw)

    def test_unchecked_boxes_block_done(self):
        self.ledger.issue_fetcher = lambda r, n: aged(load(210), 60 * 24)
        with self.assertRaises(ValueError) as ctx:
            self._done()
        self.assertIn("unchecked box", str(ctx.exception))
        self.assertNotEqual(self.ledger.get_request("req-1")["state"], "done")

    def test_just_created_issue_blocks_done(self):
        self.ledger.issue_fetcher = lambda r, n: aged(load(209), 0.2)
        with self.assertRaises(ValueError):
            self._done()

    def test_ticked_old_issue_allows_done(self):
        self.ledger.issue_fetcher = lambda r, n: aged(load(209), 60)
        self.assertEqual(self._done()["state"], "done")

    def test_force_close_allows_done_and_records_comment_and_history(self):
        comments = []
        self.ledger.issue_fetcher = lambda r, n: aged(load(210), 60)
        self.ledger.close_commenter = lambda r, n, b: comments.append(b)
        req = self._done(force_close_reason="operator accepted: box is obsolete")
        self.assertEqual(req["state"], "done")
        self.assertEqual(len(comments), 1)
        self.assertIn("operator accepted", comments[0])
        self.assertIn("force-close", req["history"][-1]["reason"])


def _board_graphql_factory():
    status = ["Backlog"]

    def run(query, variables):
        if "updateProjectV2ItemFieldValue" in query:
            status[0] = "Done"
            return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_1"}}}}
        if "projectV2" in query:
            return {"data": {"repositoryOwner": {"projectV2": {"id": "PVT_1", "title": "T", "fields": {"nodes": [
                {"id": "F1", "name": "Status",
                 "options": [{"id": "O_DONE", "name": "Done"}, {"id": "O_B", "name": "Backlog"}]}
            ]}}}}}
        return {"data": {"repository": {"issue": {"id": "I1", "projectItems": {"nodes": [
            {"id": "ITEM_1", "project": {"id": "PVT_1", "number": 5, "owner": {"login": "Wladefant"}},
             "fieldValueByName": {"name": status[0], "optionId": "O_DONE" if status[0] == "Done" else "O_B"}}]}}}}}

    return run


class TestAdapterCloseGuard(unittest.TestCase):
    RECORD = {
        "state": "done", "head": HEAD,
        "acceptance_criteria": [{"id": "AC-1", "status": "verified", "evidence": "ok", "verified_head": HEAD}],
        "github": {"proof_verified": True, "proof_url": "https://github.com/Bavariance/polysimulator/issues/4543"},
    }

    def _go(self, issue, **kw):
        updater = SuperboardProjectUpdater(create_polysimulator_config(), graphql_runner=_board_graphql_factory(),
                                           sub_issues_checker=lambda r, n: [])
        return updater.update_lifecycle("req-4543", "Done", head_sha=HEAD, ledger_record=self.RECORD,
                                        issue_fetcher=lambda r, n: issue, **kw)

    def test_unchecked_boxes_block_done_with_zero_writes(self):
        out = self._go(aged(load(210), 60 * 24))
        self.assertFalse(out.ok)
        self.assertEqual(out.github_writes, 0)
        self.assertIn("unchecked box", out.blocked_reason)

    def test_just_created_issue_blocks_done(self):
        self.assertFalse(self._go(aged(load(209), 0.1)).ok)

    def test_ticked_old_issue_moves_to_done(self):
        out = self._go(aged(load(209), 60))
        self.assertTrue(out.ok)
        self.assertEqual(out.github_writes, 1)

    def test_force_close_moves_to_done_and_comments(self):
        comments = []
        out = self._go(aged(load(210), 60), force_close_reason="accepted by operator",
                       close_commenter=lambda r, n, b: comments.append(b))
        self.assertTrue(out.ok)
        self.assertEqual(len(comments), 1)

    def test_dry_run_force_close_posts_no_comment(self):
        comments = []
        out = self._go(aged(load(210), 60), force_close_reason="x", dry_run=True,
                       close_commenter=lambda r, n, b: comments.append(b))
        self.assertTrue(out.ok)
        self.assertEqual(comments, [])


class FakeClient(GitHubClient):
    def __init__(self, issues, sub_counts=None):
        super().__init__(token="x")
        self.issues = issues
        self.sub_counts = sub_counts or {}

    def get_closed_issues_since(self, repo, since_iso):
        return list(self.issues)

    def get_sub_issues(self, repo, issue_number):
        return [{"number": 1}] * self.sub_counts.get(issue_number, 0)


def closed(issue, closed_after_min, age_days=1.0, **kw):
    created = NOW - timedelta(days=age_days)
    out = dict(issue)
    out["state"] = "closed"
    out["created_at"] = created.isoformat().replace("+00:00", "Z")
    out["closed_at"] = (created + timedelta(minutes=closed_after_min)).isoformat().replace("+00:00", "Z")
    out.update(kw)
    return out


class TestPrematureCloseAudit(unittest.TestCase):
    def cats(self, findings):
        return sorted(f.category for f in findings)

    def test_flags_unchecked_boxes_fast_close_and_bare_plan_with_urls(self):
        plan = closed(load(210), 60, number=210, html_url="https://github.com/Wladefant/shipnovo/issues/210")
        fast = closed(load(209), 0.03, number=209, html_url="https://github.com/Wladefant/shipnovo/issues/209")
        client = FakeClient([plan, fast])
        findings, scanned = audit_premature_closes([REPO], days=7, client=client, now=NOW)
        self.assertEqual(scanned, 2)
        self.assertEqual(
            self.cats(findings),
            ["closed_with_unchecked_boxes", "closed_within_minutes_of_creation", "plan_closed_without_subissues"],
        )
        self.assertTrue(all(f.url.startswith("https://github.com/") for f in findings))

    def test_clean_closed_issue_is_not_flagged(self):
        ok = closed(load(209), 120, number=209, title="Fix the pack screen")
        findings, _ = audit_premature_closes([REPO], days=7, client=FakeClient([ok]), now=NOW)
        self.assertEqual(findings, [])

    def test_plan_with_sub_issues_is_not_flagged_as_bare(self):
        plan = closed(load(209), 120, number=209, title="Plan: pack redesign")
        findings, _ = audit_premature_closes([REPO], days=7, client=FakeClient([plan], {209: 3}), now=NOW)
        self.assertEqual(findings, [])

    def test_issue_closed_outside_window_is_ignored(self):
        old = closed(load(210), 60, age_days=30, number=210)
        findings, scanned = audit_premature_closes([REPO], days=7, client=FakeClient([old]), now=NOW)
        self.assertEqual((findings, scanned), ([], 0))

    def test_report_lists_each_hit_as_a_link(self):
        fast = closed(load(209), 0.03, number=209, html_url="https://github.com/Wladefant/shipnovo/issues/209")
        result = run_adoption_audit(repo=REPO, client=_EmptyRepoClient([fast]), premature_close_repos=[REPO],
                                    premature_close_days=3650)
        md = format_markdown_report(result)
        self.assertIn("(https://github.com/Wladefant/shipnovo/issues/209)", md)
        self.assertEqual(result.status, "fail")


class _EmptyRepoClient(FakeClient):
    def get_issues(self, repo, state="all"):
        return []


if __name__ == "__main__":
    unittest.main()
