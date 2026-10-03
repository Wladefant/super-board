"""No candidate reaches GitHub before the entire judged batch passes intake."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from super_board_runtime.idea import IdeaError, decompose, file_drafts, lint


def valid():
    sections = {"Context": "Export queued cards for offline inspection.",
                "Steps": "1. Add an export command.",
                "Acceptance criteria": "Given queued cards, When export runs, Then JSON contains their titles.",
                "Test Area": "scripts/super-board-status.py", "Priority": "P2",
                "Work type": "build", "Environment constraint": "none",
                "Branch route": "staging", "Milestone": "Portable core"}
    return {"title": "Export queued cards", "body": "\n\n".join(
        f"## {key}\n{value}" for key, value in sections.items())}

class IdeaTests(unittest.TestCase):
    def test_invalid_judged_draft_rewritten_before_file(self):
        bad = {"title": "Export", "body": "TODO"}
        calls = []
        responses = iter([json.dumps([bad]), json.dumps([bad]),
                          json.dumps([valid()]), json.dumps([valid()])])
        def model(stage, prompt):
            calls.append((stage, prompt))
            return next(responses)
        result = decompose("Export queued cards as JSON.", model, context="main")
        filed = []
        file_drafts(result, lambda issue: filed.append(issue) or "https://example.test/1")
        self.assertEqual([c[0] for c in calls], ["draft", "judge", "draft", "judge"])
        self.assertIn("intake-section-missing", calls[2][1])
        self.assertEqual(filed, [valid()])
        self.assertIsNone(lint(filed[0]))
        self.assertTrue(result["history"][0]["failures"][0])

    def test_invalid_later_draft_blocks_whole_batch(self):
        filed = []
        result = {"drafts": [valid(), {"title": "Bad", "body": "TODO"}], "filed": []}
        with self.assertRaises(IdeaError):
            file_drafts(result, lambda issue: filed.append(issue))
        self.assertEqual(filed, [])

    def test_unfixable_drafts_never_file(self):
        with self.assertRaises(IdeaError):
            decompose("Export cards.", lambda *_: '[{"title":"Bad","body":"TODO"}]',
                      context="main", max_rewrites=1)

    def test_malformed_response_is_rewritten(self):
        responses = iter(["not json", "not json", json.dumps([valid()]), json.dumps([valid()])])
        result = decompose("Export cards.", lambda *_: next(responses), context="main")
        self.assertEqual(result["drafts"], [valid()])

    def test_empty_batch_is_rejected(self):
        with self.assertRaises(IdeaError):
            decompose("Export cards.", lambda *_: "[]", context="main", max_rewrites=0)

    def test_blank_or_multiline_input_does_not_call_model(self):
        for sentence in ["", "One\nTwo"]:
            with self.assertRaises(IdeaError):
                decompose(sentence, lambda *_: self.fail("Model was called"), context="main")


if __name__ == "__main__":
    unittest.main()
