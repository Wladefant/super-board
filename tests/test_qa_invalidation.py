"""Task 8 — a changed head invalidates every earlier QA result.

Pure stdlib `unittest`. No network, no `gh`, no `git`.

The property under test: passing QA is a claim about ONE commit. A later commit
inherits nothing, an item sitting in QA or Review is rechecked against the live
head, and the historical entry for the old SHA is preserved rather than erased —
"what did we test, and when" must stay answerable after the head moves.

Run directly:
  python -B tests/test_qa_invalidation.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_qa_invalidation.py' -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.qa import (  # noqa: E402
    QA_CHECK_CONTEXT,
    QA_FRESHNESS_STATUSES,
    QaResult,
    invalidate_qa_entry,
    pending_check_for_head,
    record_qa_result,
    requires_freshness_check,
    validate_qa_freshness,
)

TESTED_SHA = "a" * 40
MOVED_SHA = "b" * 40
PR_URL = "https://github.com/Bavariance/polysimulator/pull/456"
ISSUE_URL = "https://github.com/Bavariance/polysimulator/issues/123"


def _entry(**overrides: object):
    fields = {
        "issue_url": ISSUE_URL,
        "issue_node_id": "I_kwNOTAREALNODEID",
        "pull_request_url": PR_URL,
        "pull_request_node_id": "PR_kwNOTAREALNODEID",
        "tested_sha": TESTED_SHA,
        "current_head_sha": TESTED_SHA,
        "selected_base_branch": "staging",
        "branch_declaration": "staging",
        "result": "success",
        "failure_kind": None,
        "started_at": "2026-08-02T10:00:00Z",
        "completed_at": "2026-08-02T10:12:00Z",
        "check_url": "https://github.com/Bavariance/polysimulator/commit/" + TESTED_SHA,
        "sanitized_evidence_url": ISSUE_URL + "#issuecomment-1",
    }
    fields.update(overrides)
    return record_qa_result(QaResult(**fields))  # type: ignore[arg-type]


class FreshnessScopeTests(unittest.TestCase):
    def test_qa_and_review_items_are_rechecked(self) -> None:
        self.assertEqual(QA_FRESHNESS_STATUSES, ("QA", "Review"))
        for status in ("QA", "Review"):
            self.assertTrue(requires_freshness_check(status), status)

    def test_other_statuses_are_not_rechecked(self) -> None:
        for status in ("Backlog", "Ready", "Building", "Blocked", "Done", None, ""):
            self.assertFalse(requires_freshness_check(status), status)


class FreshnessTests(unittest.TestCase):
    def test_an_unchanged_head_stays_fresh(self) -> None:
        freshness = validate_qa_freshness(_entry(), TESTED_SHA)
        self.assertTrue(freshness.fresh)
        self.assertFalse(freshness.invalidated)
        self.assertIsNone(freshness.next_status)
        self.assertIsNone(freshness.reason_code)

    def test_a_changed_head_invalidates_and_returns_the_card_to_qa(self) -> None:
        freshness = validate_qa_freshness(_entry(), MOVED_SHA)
        self.assertFalse(freshness.fresh)
        self.assertTrue(freshness.invalidated)
        self.assertEqual(freshness.next_status, "QA")
        self.assertEqual(freshness.reason_code, "qa-head-moved")
        self.assertEqual(freshness.tested_sha, TESTED_SHA)
        self.assertEqual(freshness.current_head_sha, MOVED_SHA)

    def test_the_new_head_gets_a_pending_status_requiring_exact_sha_qa(self) -> None:
        freshness = validate_qa_freshness(_entry(), MOVED_SHA)
        self.assertEqual(freshness.pending_status_sha, MOVED_SHA)
        pending = pending_check_for_head(MOVED_SHA)
        self.assertEqual(pending["sha"], MOVED_SHA)
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(pending["context"], QA_CHECK_CONTEXT)

    def test_an_unreadable_head_fails_closed_rather_than_staying_fresh(self) -> None:
        freshness = validate_qa_freshness(_entry(), None)
        self.assertFalse(freshness.fresh)
        self.assertEqual(freshness.next_status, "QA")
        self.assertEqual(freshness.reason_code, "qa-head-unreadable")

    def test_a_later_commit_inherits_no_passing_result(self) -> None:
        # Even a descendant of the tested commit is a different commit.
        self.assertFalse(validate_qa_freshness(_entry(), MOVED_SHA).fresh)


class HistoryPreservationTests(unittest.TestCase):
    def test_invalidation_produces_a_new_entry_and_leaves_the_old_one_intact(self) -> None:
        original = _entry()
        invalidated = invalidate_qa_entry(original, MOVED_SHA)

        self.assertTrue(invalidated.invalidated)
        self.assertEqual(invalidated.result, "invalidated")
        self.assertEqual(invalidated.tested_sha, TESTED_SHA)
        self.assertEqual(invalidated.current_head_sha, MOVED_SHA)

        # The historical record for the old SHA is untouched, not erased.
        self.assertFalse(original.invalidated)
        self.assertEqual(original.result, "success")
        self.assertEqual(original.current_head_sha, TESTED_SHA)
        self.assertEqual(original.check_url, invalidated.check_url)

    def test_the_ledger_keeps_both_entries(self) -> None:
        original = _entry()
        ledger = [original, invalidate_qa_entry(original, MOVED_SHA)]
        self.assertEqual([e.result for e in ledger], ["success", "invalidated"])
        self.assertEqual({e.tested_sha for e in ledger}, {TESTED_SHA})

    def test_invalidating_an_already_invalid_entry_is_idempotent(self) -> None:
        once = invalidate_qa_entry(_entry(), MOVED_SHA)
        twice = invalidate_qa_entry(once, MOVED_SHA)
        self.assertEqual(once.to_dict(), twice.to_dict())


if __name__ == "__main__":
    unittest.main(verbosity=2)
