"""Task 8 — the read-only merge handoff validator.

`validate_merge_handoff` runs immediately before a human merges. It rereads the
pull request head, compares it with the last successful `tested_sha`, and
verifies the SHA-bound required check concluded success. Any mismatch refuses to
report merge-ready.

It performs **no** writes — not a status, not a comment, not a label. This file
proves that by failing the test if any subprocess is spawned while it runs.

Run directly:
  python -B tests/test_merge_handoff.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_merge_handoff.py' -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime import qa as qa_module  # noqa: E402
from super_board_runtime.qa import (  # noqa: E402
    PullRequestHead,
    QaResult,
    record_qa_result,
    validate_merge_handoff,
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


def _head(sha: str = TESTED_SHA) -> PullRequestHead:
    return PullRequestHead(
        pull_request_url=PR_URL,
        pull_request_node_id="PR_kwNOTAREALNODEID",
        head_ref="feat/exact-sha",
        head_sha=sha,
        base_ref="staging",
        is_draft=False,
        mergeable="MERGEABLE",
    )


class MergeHandoffTests(unittest.TestCase):
    def test_matching_head_and_successful_check_is_merge_ready(self) -> None:
        decision = validate_merge_handoff(_entry(), _head(), "success")
        self.assertTrue(decision.merge_ready)
        self.assertIsNone(decision.reason_code)

    def test_a_moved_head_is_never_merge_ready(self) -> None:
        decision = validate_merge_handoff(_entry(), _head(MOVED_SHA), "success")
        self.assertFalse(decision.merge_ready)
        self.assertEqual(decision.reason_code, "head-moved")

    def test_a_missing_check_is_never_merge_ready(self) -> None:
        for conclusion in (None, "", "   "):
            with self.subTest(conclusion=conclusion):
                decision = validate_merge_handoff(_entry(), _head(), conclusion)
                self.assertFalse(decision.merge_ready)
                self.assertEqual(decision.reason_code, "check-missing")

    def test_a_non_success_check_is_never_merge_ready(self) -> None:
        for conclusion in ("failure", "pending", "neutral", "cancelled", "timed_out"):
            with self.subTest(conclusion=conclusion):
                decision = validate_merge_handoff(_entry(), _head(), conclusion)
                self.assertFalse(decision.merge_ready)
                self.assertEqual(decision.reason_code, "check-not-success")

    def test_an_invalidated_entry_is_never_merge_ready(self) -> None:
        entry = _entry(current_head_sha=MOVED_SHA)
        decision = validate_merge_handoff(entry, _head(MOVED_SHA), "success")
        self.assertFalse(decision.merge_ready)
        self.assertEqual(decision.reason_code, "qa-evidence-invalidated")

    def test_a_failed_qa_entry_is_never_merge_ready(self) -> None:
        entry = _entry(result="failure", failure_kind="repairable")
        decision = validate_merge_handoff(entry, _head(), "success")
        self.assertFalse(decision.merge_ready)
        self.assertEqual(decision.reason_code, "qa-evidence-not-success")

    def test_the_decision_carries_both_shas_for_the_operator(self) -> None:
        decision = validate_merge_handoff(_entry(), _head(MOVED_SHA), "success")
        body = decision.to_dict()
        self.assertEqual(body["tested_sha"], TESTED_SHA)
        self.assertEqual(body["current_head_sha"], MOVED_SHA)
        self.assertFalse(body["merge_ready"])


class ReadOnlyTests(unittest.TestCase):
    """The validator must not write. Any spawned process would be a write path."""

    def setUp(self) -> None:
        self.spawned: list[object] = []
        self._real_run = qa_module.subprocess.run

        def refuse(*args, **kwargs):
            self.spawned.append(args)
            raise AssertionError("validate_merge_handoff spawned a process")

        qa_module.subprocess.run = refuse

    def tearDown(self) -> None:
        qa_module.subprocess.run = self._real_run

    def test_zero_write_calls_on_every_path(self) -> None:
        cases = (
            (_entry(), _head(), "success"),
            (_entry(), _head(MOVED_SHA), "success"),
            (_entry(), _head(), None),
            (_entry(), _head(), "failure"),
            (_entry(current_head_sha=MOVED_SHA), _head(MOVED_SHA), "success"),
        )
        for entry, head, conclusion in cases:
            validate_merge_handoff(entry, head, conclusion)
        self.assertEqual(self.spawned, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
