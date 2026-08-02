"""Task 16 — the fallback auto-add is disabled, idempotent, and gated.

Pure stdlib `unittest`. No network, no `gh`.

GitHub's built-in Projects auto-add already covers `is:issue is:open`. This
Action is a redundant backup for the day that breaks, and a redundant backup
that runs unattended is a duplicate-card generator: it fires on `issues.opened`,
the built-in workflow fires on the same event, and the board ends up with two
cards for one issue that then disagree about status.

So it ships DISABLED, it checks membership by immutable content node ID before
inserting anything, and every uncertain answer resolves to "do not insert".
"Not found in a page-capped snapshot" is not the same as "not on the board".

Re-enabling it is a separate, human-reviewed decision — not something an
installer, a proof run, or an activation-mode change can do as a side effect.

Run directly:
  python -B tests/test_fallback_auto_add.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_fallback_auto_add.py' -v
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

from super_board_runtime.auth import REQUIRED_SCOPES, classify_token  # noqa: E402
from super_board_runtime.project import (  # noqa: E402
    FALLBACK_ENABLE_VALUE,
    FALLBACK_ENABLE_VARIABLE,
    FallbackDecision,
    ProjectSnapshot,
    evaluate_fallback_auto_add,
)

WORKFLOW = _REPO_ROOT / "payload" / "github" / "workflows" / "auto-add-to-project.yml"
ONBOARD = _REPO_ROOT / "skills" / "super-board" / "references" / "onboard.md"

ISSUE_NODE = "I_kwNOTAREALISSUENODE"
OTHER_NODE = "I_kwSOMEONEELSESNODE"


def _project(*content_ids: str, hit_cap: bool = False) -> ProjectSnapshot:
    return ProjectSnapshot(
        project_owner="Wladefant",
        project_number=1,
        items=tuple({"item_node_id": f"PVTI_{i}", "content_node_id": node}
                    for i, node in enumerate(content_ids)),
        fields={},
        hit_cap=hit_cap,
    )


class _Preflight:
    """Records the order in which the preflight checks were consulted."""

    def __init__(self, *, identity: bool = True, quota: bool = True) -> None:
        self.calls: list[str] = []
        self._identity = identity
        self._quota = quota

    def identity(self) -> bool:
        self.calls.append("identity")
        return self._identity

    def quota(self) -> None:
        self.calls.append("quota")
        if not self._quota:
            raise RuntimeError("the immutable GraphQL reserve was reached")


def _decide(node=ISSUE_NODE, project=None, enabled=True, preflight=None) -> FallbackDecision:
    preflight = preflight or _Preflight()
    return evaluate_fallback_auto_add(
        node,
        _project(OTHER_NODE) if project is None else project,
        enabled,
        identity_check=preflight.identity,
        quota_check=preflight.quota,
    )


# ───────────────────────────── the disable guard ─────────────────────────────


class DisabledByDefaultTests(unittest.TestCase):
    def test_the_enable_variable_contract_is_pinned(self) -> None:
        self.assertEqual(FALLBACK_ENABLE_VARIABLE, "ENABLE_ADD_TO_PROJECT")
        self.assertEqual(FALLBACK_ENABLE_VALUE, "true")

    def test_a_disabled_fallback_inserts_nothing(self) -> None:
        decision = _decide(enabled=False)
        self.assertFalse(decision.insert)
        self.assertEqual(decision.reason_code, "fallback-disabled")

    def test_the_workflow_requires_the_variable_to_be_exactly_true(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(f"vars.{FALLBACK_ENABLE_VARIABLE} == '{FALLBACK_ENABLE_VALUE}'", text)

    def test_the_workflow_says_it_ships_disabled(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8").casefold()
        self.assertIn("disabled", text)

    def test_the_workflow_carries_no_unresolved_substitution_marker(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        markers = re.findall(r"__[A-Za-z0-9_]+__", text)
        self.assertEqual(markers, [], f"unresolved substitution markers: {markers}")

    def test_nothing_in_the_tree_still_substitutes_the_old_marker(self) -> None:
        for path in sorted(_REPO_ROOT.rglob("*")):
            if not path.is_file() or ".git" in path.parts:
                continue
            if path.suffix.lower() not in {".md", ".yml", ".yaml", ".sh"}:
                continue
            if path.name == "test_fallback_auto_add.py":
                continue
            with self.subTest(path=path.relative_to(_REPO_ROOT).as_posix()):
                self.assertNotIn(
                    "__PROJECT" + "_URL__", path.read_text(encoding="utf-8", errors="replace")
                )


# ───────────────────────────── membership ─────────────────────────────


class MembershipTests(unittest.TestCase):
    def test_an_existing_card_is_never_duplicated(self) -> None:
        decision = _decide(project=_project(OTHER_NODE, ISSUE_NODE))
        self.assertFalse(decision.insert)
        self.assertEqual(decision.reason_code, "already-member")

    def test_membership_is_decided_by_the_immutable_content_node_id(self) -> None:
        # Same issue number, different node ID: a transferred or recreated
        # issue is a different card, and matching on number would merge them.
        project = ProjectSnapshot(
            project_owner="Wladefant",
            project_number=1,
            items=({"item_node_id": "PVTI_0", "content_node_id": OTHER_NODE, "number": 101},),
            fields={},
            hit_cap=False,
        )
        decision = _decide(project=project)
        self.assertTrue(decision.insert)
        self.assertEqual(decision.membership_key, "content_node_id")

    def test_a_page_capped_snapshot_is_unknown_not_absent(self) -> None:
        decision = _decide(project=_project(OTHER_NODE, hit_cap=True))
        self.assertFalse(decision.insert)
        self.assertEqual(decision.reason_code, "membership-unknown")

    def test_a_duplicated_card_is_ambiguous(self) -> None:
        decision = _decide(project=_project(ISSUE_NODE, ISSUE_NODE))
        self.assertFalse(decision.insert)
        self.assertEqual(decision.reason_code, "membership-unknown")

    def test_a_failed_membership_query_is_unknown(self) -> None:
        decision = _decide(project=None if False else _Unreadable())
        self.assertFalse(decision.insert)
        self.assertEqual(decision.reason_code, "membership-unknown")

    def test_a_missing_node_id_inserts_nothing(self) -> None:
        for node in ("", "   ", None, 101):
            with self.subTest(node=node):
                decision = _decide(node=node)
                self.assertFalse(decision.insert)
                self.assertEqual(decision.reason_code, "issue-node-id-invalid")

    def test_the_workflow_checks_membership_by_node_id_before_inserting(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("content_node_id", text)
        self.assertIn("evaluate_fallback_auto_add", text)
        membership_at = text.index("evaluate_fallback_auto_add")
        insert_at = text.index("actions/add-to-project")
        self.assertLess(membership_at, insert_at, "membership must be decided before insertion")


class _Unreadable:
    """A Project snapshot whose items cannot be read."""

    hit_cap = False

    @property
    def items(self):
        raise RuntimeError("the Project inventory could not be read")


# ───────────────────────────── preflight ─────────────────────────────


class PreflightTests(unittest.TestCase):
    def test_identity_then_quota_run_before_any_insertion(self) -> None:
        preflight = _Preflight()
        decision = _decide(preflight=preflight)
        self.assertTrue(decision.insert)
        self.assertEqual(preflight.calls, ["identity", "quota"])
        self.assertEqual(decision.preflight, ("identity", "quota"))

    def test_an_unverified_identity_inserts_nothing(self) -> None:
        preflight = _Preflight(identity=False)
        decision = _decide(preflight=preflight)
        self.assertFalse(decision.insert)
        self.assertEqual(decision.reason_code, "identity-unverified")
        self.assertEqual(preflight.calls, ["identity"], "quota must not be consulted after a failed identity check")

    def test_an_exhausted_reserve_inserts_nothing(self) -> None:
        preflight = _Preflight(quota=False)
        decision = _decide(preflight=preflight)
        self.assertFalse(decision.insert)
        self.assertEqual(decision.reason_code, "quota-unavailable")

    def test_a_missing_preflight_fails_closed(self) -> None:
        decision = evaluate_fallback_auto_add(ISSUE_NODE, _project(OTHER_NODE), True)
        self.assertFalse(decision.insert)
        self.assertEqual(decision.reason_code, "identity-unverified")

    def test_a_disabled_fallback_never_consults_the_preflight(self) -> None:
        preflight = _Preflight()
        _decide(enabled=False, preflight=preflight)
        self.assertEqual(preflight.calls, [])

    def test_an_existing_member_never_consults_the_preflight(self) -> None:
        preflight = _Preflight()
        _decide(project=_project(ISSUE_NODE), preflight=preflight)
        self.assertEqual(preflight.calls, [])


class CredentialContractTests(unittest.TestCase):
    """One credential contract for the whole pipeline, not two.

    The workflow header instructed a FINE-GRAINED PAT, which
    `super_board_runtime.auth` refuses outright as `token-class-not-classic`.
    Two contracts for one pipeline is one contract nobody can state, and the
    contradiction surfaces on the day the fallback is finally needed.
    """

    WORKFLOW = (
        _REPO_ROOT / "payload" / "github" / "workflows" / "auto-add-to-project.yml"
    )

    def setUp(self) -> None:
        self.source = self.WORKFLOW.read_text(encoding="utf-8")

    def test_the_workflow_asks_for_the_class_the_runtime_accepts(self) -> None:
        header = self.source.split("name:", 1)[0]
        self.assertIn("CLASSIC PAT", header)
        for scope in REQUIRED_SCOPES:
            with self.subTest(scope=scope):
                self.assertIn(scope, header)

    def test_the_identity_preflight_refuses_a_non_classic_token(self) -> None:
        # A comment cannot refuse a token; the guard has to.
        self.assertIn("classify_token", self.source)
        self.assertIn('token_class != "classic"', self.source)

    def test_the_runtime_still_refuses_a_fine_grained_token(self) -> None:
        # The prefix the header used to instruct.
        self.assertEqual(classify_token("github" + "_pat_" + "x" * 30), "fine-grained")
        self.assertEqual(classify_token("gh" + "p_" + "y" * 36), "classic")


class DecisionShapeTests(unittest.TestCase):
    def test_the_decision_carries_insert_and_a_reason_code(self) -> None:
        body = _decide().to_dict()
        self.assertIn("insert", body)
        self.assertIn("reason_code", body)
        self.assertIs(type(body["insert"]), bool)
        self.assertIsInstance(body["reason_code"], str)

    def test_an_authorized_insertion_says_so(self) -> None:
        decision = _decide()
        self.assertTrue(decision.insert)
        self.assertEqual(decision.reason_code, "insert-authorized")


# ───────────────────────────── the re-enable gate ─────────────────────────────


class ReEnableGateTests(unittest.TestCase):
    REQUIRED_PHRASES = (
        "configuration-only issue",
        "user-reviewed pull request",
        "built-in auto-add",
        "content node ID",
        "identity verification",
        "quota preflight",
        "rollback",
        "no-op",
    )

    def setUp(self) -> None:
        self.text = ONBOARD.read_text(encoding="utf-8")

    def test_the_gate_is_documented(self) -> None:
        for phrase in self.REQUIRED_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_the_gate_names_what_does_not_authorize_re_enabling(self) -> None:
        for phrase in ("installer", "proof run", "active-mode change"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)
        self.assertIn("Neither the installer", self.text)

    def test_the_workflow_points_at_the_gate(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("onboard.md", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
