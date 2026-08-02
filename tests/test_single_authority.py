"""One authority per concept — proven against the shipped sources.

Two concepts in this runtime were implemented twice: branch routing (once in
`super_board_runtime.routing`, once as a hardcoded mirror in the Node workflow)
and the merge handoff (once in `super_board_runtime.qa`, once as a smaller
reimplementation in the same workflow, with a narrower reason-code vocabulary
and camelCase output).

A second implementation is not a safety net. It is a place where the same card
gets two different answers, and the more permissive answer is the one that acts.
These tests fail if either duplicate comes back.

Run directly:
  python -B tests/test_single_authority.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.qa import QA_CHECK_CONTEXT  # noqa: E402
from super_board_runtime.routing import NON_DISPATCH_BRANCHES  # noqa: E402

WAVE = _REPO_ROOT / "workflows" / "super-board-wave.js"

#: Reason codes owned by `super_board_runtime.qa.validate_merge_handoff`. A copy
#: of any of them in another language is a second decision procedure.
MERGE_HANDOFF_REASON_CODES = (
    "qa-evidence-missing",
    "qa-evidence-invalidated",
    "qa-evidence-not-success",
    "head-moved",
    "check-missing",
    "check-not-success",
)


class WaveWorkflowSingleAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = WAVE.read_text(encoding="utf-8")

    def test_the_workflow_does_not_mirror_the_routing_constants(self) -> None:
        for branch in NON_DISPATCH_BRANCHES:
            with self.subTest(branch=branch):
                self.assertFalse(
                    f"'{branch}'" in self.source,
                    f"{branch!r} is routing's to decide; the workflow consumes the "
                    "planner's resolved route and never re-derives it",
                )

    def test_the_workflow_does_not_reimplement_the_merge_handoff(self) -> None:
        for code in MERGE_HANDOFF_REASON_CODES:
            with self.subTest(reason_code=code):
                self.assertFalse(
                    f"'{code}'" in self.source,
                    f"{code!r} belongs to super_board_runtime.qa; the workflow must "
                    "consume that decision, not produce its own",
                )

    def test_the_workflow_does_not_compare_shas_itself(self) -> None:
        # A merge-readiness decision made from two strings the stage result
        # carried is a decision about a head nobody reread.
        self.assertIsNone(
            re.search(r"current\w*Sha\s*!==\s*tested", self.source),
            "the workflow compares SHAs itself instead of asking the authority",
        )

    def test_the_workflow_names_the_authority_it_delegates_to(self) -> None:
        self.assertTrue(
            "merge-handoff" in self.source,
            "the workflow must call the `qa merge-handoff` authority for the gate",
        )

    def test_the_qa_check_context_is_not_a_second_literal(self) -> None:
        # The context may be NAMED in a prompt or a log line, but it must be the
        # same string the runtime publishes.
        for match in re.findall(r"superboard/[a-z-]+", self.source):
            self.assertEqual(match, QA_CHECK_CONTEXT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
