"""Komo event intake: one human thread becomes one issue, nothing else does."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import komo_event_intake as intake  # noqa: E402

THREAD = "40ee2bdd-0df1-4327-a9f1-0ad8ecec6d05"
CFG = {"repo": "Wladefant/shipnovo", "token": "unused"}


def _event(kind="thread.created", by_agent=False, thread=THREAD, body="The card overlaps the footer"):
    return {"id": "7", "kind": kind, "project": "shipnovo", "threadId": thread, "page": "/returns",
            "author": "Wladimir", "byAgent": by_agent, "body": body}


def _run(event, threads=None, dry_run=False, found=None):
    state = {"cursor": "latest", "threads": dict(threads or {})}
    calls = {"issue": [], "comment": [], "reply": [], "intake": []}
    with mock.patch.object(intake, "existing_issue", return_value=found), \
            mock.patch.object(intake, "create_issue",
                              side_effect=lambda repo, e: calls["issue"].append(e) or (12, "https://x/12")), \
            mock.patch.object(intake, "run_intake",
                              side_effect=lambda *a: calls["intake"].append(a) or (0, "ok")), \
            mock.patch.object(intake, "reply_to_thread",
                              side_effect=lambda *a: calls["reply"].append(a)), \
            mock.patch.object(intake, "gh", side_effect=lambda *a: calls["comment"].append(a) or ""):
        intake.handle_event("shipnovo", CFG, "tok", state, event, dry_run, lambda line: None)
    return state, calls


class HandleEventTests(unittest.TestCase):
    def test_a_human_thread_opens_one_issue_runs_intake_and_replies(self) -> None:
        state, calls = _run(_event())
        self.assertEqual(len(calls["issue"]), 1)
        self.assertEqual(calls["intake"], [("Wladefant/shipnovo", 12, False)])
        self.assertEqual(calls["reply"], [("tok", THREAD, "Tracked in https://x/12")])
        self.assertEqual(state["threads"], {THREAD: 12})

    def test_an_agent_event_is_ignored(self) -> None:
        state, calls = _run(_event(by_agent=True))
        self.assertEqual(calls["issue"], [])
        self.assertEqual(state["threads"], {})

    def test_a_known_thread_never_opens_a_second_issue(self) -> None:
        _, calls = _run(_event(), threads={THREAD: 9})
        self.assertEqual(calls["issue"], [])

    def test_an_issue_found_by_marker_is_adopted_not_duplicated(self) -> None:
        state, calls = _run(_event(), found=31)
        self.assertEqual(calls["issue"], [])
        self.assertEqual(state["threads"], {THREAD: 31})

    def test_a_human_reply_on_a_mapped_thread_comments_on_its_issue(self) -> None:
        _, calls = _run(_event(kind="thread.reply", body="still broken"), threads={THREAD: 9})
        self.assertEqual(len(calls["comment"]), 1)
        self.assertIn("9", calls["comment"][0])
        self.assertIn("> still broken", calls["comment"][0][-1])

    def test_a_reply_on_an_unmapped_thread_is_skipped(self) -> None:
        _, calls = _run(_event(kind="thread.reply"))
        self.assertEqual(calls["comment"], [])

    def test_dry_run_changes_nothing(self) -> None:
        state, calls = _run(_event(), dry_run=True)
        self.assertEqual(calls["issue"], [])
        self.assertEqual(calls["reply"], [])
        self.assertEqual(state["threads"], {})


class FormatTests(unittest.TestCase):
    def test_comment_text_is_quoted_and_capped(self) -> None:
        self.assertEqual(intake.quote("a\nb"), "> a\n> b")
        self.assertEqual(len(intake.quote("x" * 9000)), 3002)

    def test_the_title_is_one_short_line(self) -> None:
        title = intake.issue_title("first line\nsecond " + "y" * 200)
        self.assertNotIn("\n", title)
        self.assertLessEqual(len(title), len("Design feedback: ") + 80)

    def test_the_marker_round_trips(self) -> None:
        self.assertEqual(intake.MARK_RE.search(intake.MARK.format(THREAD)).group(1), THREAD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
