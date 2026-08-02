"""Task 10 — branch routes are declared explicitly or the card is ineligible.

Pure stdlib `unittest`. No network, no `gh`, no `git`.

The rule this file pins: a branch route is something an issue **says**, in one
normalized declaration. It is never inferred from a Test Area, from geography
in the prose, from a label alone, or from whatever happens to be checked out.
Every invalid case fails BEFORE a branch is created — the recorder below proves
zero branch-creation calls happen.

Run directly:
  python -B tests/test_routing.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_routing.py' -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.config import ConfigError, load_and_validate_config  # noqa: E402
from super_board_runtime.eligibility import IssueSnapshot  # noqa: E402
from super_board_runtime.routing import (  # noqa: E402
    FRANKFURT_LABEL,
    NON_DISPATCH_BRANCHES,
    ROUTE_DECLARATIONS,
    RoutingError,
    create_branch_for_route,
    resolve_branch_route,
    verify_pull_request_base,
)


def _config(**overrides: object):
    payload = {
        "version": 1,
        "project": {"owner": "Bavariance", "number": 1},
        "repo": {"remote": "Bavariance/polysimulator"},
        "base_branch": "staging",
        "activation_mode": "off",
        "branch_routes": {FRANKFURT_LABEL: "staging-frankfurt"},
    }
    payload.update(overrides)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_and_validate_config(path)


def _issue(body: str, labels: tuple[str, ...] = ()) -> IssueSnapshot:
    return IssueSnapshot(
        url="https://github.com/Bavariance/polysimulator/issues/123",
        node_id="I_kwNOTAREALNODEID",
        number=123,
        content_type="Issue",
        state="OPEN",
        title="Route me",
        body=body,
        labels=labels,
        assignees=(),
        status="Ready",
        milestone=None,
    )


class RecordingBranchCreator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, branch, base):
        self.calls.append((branch, base))
        return {"ref": branch}


class ValidRouteTests(unittest.TestCase):
    def test_declared_routes_are_exactly_two(self) -> None:
        self.assertEqual(ROUTE_DECLARATIONS, ("staging", "staging-frankfurt"))

    def test_explicit_staging_declaration(self) -> None:
        route = resolve_branch_route(_issue("Context.\n\nBranch route: staging\n"), _config())
        self.assertTrue(route.valid)
        self.assertEqual(route.declaration, "staging")
        self.assertEqual(route.base_branch, "staging")
        self.assertIsNone(route.required_label)
        self.assertIsNone(route.reason_code)

    def test_explicit_frankfurt_declaration_with_its_redundant_label(self) -> None:
        route = resolve_branch_route(
            _issue("Branch route: staging-frankfurt\n", (FRANKFURT_LABEL,)), _config()
        )
        self.assertTrue(route.valid)
        self.assertEqual(route.declaration, "staging-frankfurt")
        self.assertEqual(route.base_branch, "staging-frankfurt")
        self.assertEqual(route.required_label, FRANKFURT_LABEL)

    def test_the_declaration_is_normalized_for_case_and_spacing(self) -> None:
        for body in (
            "branch route:staging\n",
            "Branch Route:   staging  \n",
            "Some text\n\n  Branch route: staging\n\nmore text\n",
        ):
            with self.subTest(body=body):
                route = resolve_branch_route(_issue(body), _config())
                self.assertTrue(route.valid, route.reason_code)
                self.assertEqual(route.base_branch, "staging")


class InvalidRouteTests(unittest.TestCase):
    """Every one of these must fail BEFORE a branch is created."""

    def _assert_invalid(self, issue, reason) -> None:
        route = resolve_branch_route(issue, _config())
        self.assertFalse(route.valid, f"expected invalid, got {route}")
        self.assertEqual(route.reason_code, reason)
        self.assertIsNone(route.base_branch)

        creator = RecordingBranchCreator()
        with self.assertRaises(RoutingError) as ctx:
            create_branch_for_route(issue, _config(), creator=creator)
        self.assertEqual(ctx.exception.reason, reason)
        self.assertEqual(creator.calls, [], "a branch was created for an invalid route")

    def test_frankfurt_declaration_without_its_label(self) -> None:
        self._assert_invalid(_issue("Branch route: staging-frankfurt\n"), "route-label-conflict")

    def test_staging_declaration_carrying_the_frankfurt_label(self) -> None:
        self._assert_invalid(
            _issue("Branch route: staging\n", (FRANKFURT_LABEL,)), "route-label-conflict"
        )

    def test_missing_declaration(self) -> None:
        self._assert_invalid(_issue("No route here at all.\n"), "route-declaration-missing")

    def test_empty_body(self) -> None:
        self._assert_invalid(_issue(""), "route-declaration-missing")
        self._assert_invalid(_issue(None), "route-declaration-missing")  # type: ignore[arg-type]

    def test_the_literal_string_default(self) -> None:
        self._assert_invalid(_issue("Branch route: default\n"), "route-declaration-unknown")

    def test_an_unknown_value(self) -> None:
        self._assert_invalid(_issue("Branch route: production\n"), "route-declaration-unknown")
        self._assert_invalid(_issue("Branch route: main\n"), "route-declaration-unknown")

    def test_designstaging_is_never_a_dispatch_route(self) -> None:
        self.assertIn("designstaging", NON_DISPATCH_BRANCHES)
        self._assert_invalid(_issue("Branch route: designstaging\n"), "route-declaration-unknown")
        # Even a design-labelled card cannot be routed there.
        self._assert_invalid(
            _issue("Branch route: designstaging\n", ("design",)), "route-declaration-unknown"
        )

    def test_two_declarations_in_one_body(self) -> None:
        self._assert_invalid(
            _issue("Branch route: staging\nBranch route: staging-frankfurt\n"),
            "route-declaration-duplicate",
        )

    def test_two_identical_declarations_are_still_duplicates(self) -> None:
        self._assert_invalid(
            _issue("Branch route: staging\n\nBranch route: staging\n"),
            "route-declaration-duplicate",
        )

    def test_a_route_label_naming_a_non_dispatch_branch(self) -> None:
        # A config may map a label onto any branch name, so `route:main` loads
        # fine. It still never dispatches, and the refusal has to come from this
        # module or a caller reading labels reaches a different verdict from a
        # caller reading declarations.
        config = _config(branch_routes={"route:main": "main"})
        for body in ("Branch route: staging\n", "nothing declared\n"):
            with self.subTest(body=body):
                route = resolve_branch_route(_issue(body, ("route:main",)), config)
                self.assertFalse(route.valid)
                self.assertEqual(route.reason_code, "route-declaration-unknown")
                self.assertIsNone(route.base_branch)

    def test_the_configured_branch_value_is_never_the_base(self) -> None:
        """`branch_routes` VALIDATES labels; it does not choose the branch.

        The schema used to say a card carrying a route label "is branched from
        (and targets) that branch instead of `base_branch`", which is a table
        that decides routes. `resolve_branch_route` does not read it that way and
        never has: the declaration in the issue body is the route, and the only
        two declarations that resolve are `staging` and `staging-frankfurt`. A
        table that silently retargeted work from a config value would be a second
        routing authority, and the more permissive of two authorities is the one
        that acts.
        """
        config = _config(branch_routes={FRANKFURT_LABEL: "some-other-branch"})
        route = resolve_branch_route(
            _issue("Branch route: staging-frankfurt\n", (FRANKFURT_LABEL,)), config
        )
        self.assertTrue(route.valid)
        self.assertEqual(route.declaration, "staging-frankfurt")
        self.assertEqual(
            route.base_branch,
            "staging-frankfurt",
            "the declaration is the base branch; the config value is not consulted",
        )

    def test_the_schema_does_not_promise_a_routing_table(self) -> None:
        schema = (
            _REPO_ROOT / "skills" / "super-board" / "references" / "config-schema.json"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            "instead of `base_branch`",
            schema,
            "the schema promises a behaviour `resolve_branch_route` does not implement",
        )
        self.assertIn("validation table", schema.lower())

    def test_two_conflicting_route_labels(self) -> None:
        config = _config(
            branch_routes={FRANKFURT_LABEL: "staging-frankfurt", "branch:staging": "staging"}
        )
        issue = _issue("Branch route: staging\n", (FRANKFURT_LABEL, "branch:staging"))
        route = resolve_branch_route(issue, config)
        self.assertFalse(route.valid)
        self.assertEqual(route.reason_code, "route-label-conflict")


class NeverInferredTests(unittest.TestCase):
    def test_test_area_never_implies_a_route(self) -> None:
        route = resolve_branch_route(
            _issue("Test Area: staging-frankfurt\nSteps: none\n"), _config()
        )
        self.assertFalse(route.valid)
        self.assertEqual(route.reason_code, "route-declaration-missing")

    def test_geography_in_prose_never_implies_a_route(self) -> None:
        route = resolve_branch_route(
            _issue("The Frankfurt compose 502s and staging-frankfurt looks stale.\n"), _config()
        )
        self.assertFalse(route.valid)
        self.assertEqual(route.reason_code, "route-declaration-missing")

    def test_a_label_alone_never_implies_a_route(self) -> None:
        route = resolve_branch_route(_issue("No declaration.\n", (FRANKFURT_LABEL,)), _config())
        self.assertFalse(route.valid)
        self.assertEqual(route.reason_code, "route-declaration-missing")

    def test_the_configured_base_branch_never_substitutes_for_a_declaration(self) -> None:
        route = resolve_branch_route(_issue("Nothing declared.\n"), _config(base_branch="staging"))
        self.assertIsNone(route.base_branch)


class PullRequestBaseTests(unittest.TestCase):
    def test_a_matching_base_passes(self) -> None:
        route = resolve_branch_route(_issue("Branch route: staging\n"), _config())
        ok, reason = verify_pull_request_base(route, "staging")
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_a_drifted_base_is_refused_before_qa_or_review(self) -> None:
        route = resolve_branch_route(_issue("Branch route: staging\n"), _config())
        ok, reason = verify_pull_request_base(route, "main")
        self.assertFalse(ok)
        self.assertEqual(reason, "route-base-branch-drift")

    def test_an_invalid_route_can_never_validate_a_base(self) -> None:
        route = resolve_branch_route(_issue("no declaration"), _config())
        ok, reason = verify_pull_request_base(route, "staging")
        self.assertFalse(ok)
        self.assertEqual(reason, "route-declaration-missing")


class EligibilityIntegrationTests(unittest.TestCase):
    """A board that requires declarations refuses undeclared cards outright."""

    def test_a_declared_card_is_eligible_and_carries_its_route(self) -> None:
        from super_board_runtime.eligibility import evaluate_dispatch

        config = _config(activation_mode="active", require_branch_route_declaration=True)
        decision = evaluate_dispatch(_issue("Branch route: staging\n"), config)
        self.assertTrue(decision.eligible, decision.reason_codes)
        self.assertEqual(decision.selected_base_branch, "staging")
        self.assertEqual(decision.branch_declaration, "staging")

    def test_an_undeclared_card_is_ineligible_with_the_routing_reason_code(self) -> None:
        from super_board_runtime.eligibility import evaluate_dispatch

        config = _config(activation_mode="active", require_branch_route_declaration=True)
        for body, reason in (
            ("nothing declared", "route-declaration-missing"),
            ("Branch route: default\n", "route-declaration-unknown"),
            ("Branch route: staging\nBranch route: staging\n", "route-declaration-duplicate"),
            ("Branch route: staging-frankfurt\n", "route-label-conflict"),
        ):
            with self.subTest(reason=reason):
                decision = evaluate_dispatch(_issue(body), config)
                self.assertFalse(decision.eligible)
                self.assertEqual(decision.reason_codes, (reason,))
                self.assertIsNone(decision.selected_base_branch)

    def test_no_board_can_opt_out_of_declared_routing(self) -> None:
        # PREVIOUSLY LOCKED THE OPPOSITE. This assertion used to require that a
        # board which had not set `require_branch_route_declaration` kept a
        # permissive label-routing path, in which an undeclared card was
        # ELIGIBLE on `config.base_branch`. That is the same defect as a
        # `route:main` card being eligible: a fallback base branch is a branch
        # nobody chose. The flag can now only state the requirement.
        with self.assertRaises(ConfigError) as ctx:
            _config(require_branch_route_declaration=False)
        self.assertEqual(ctx.exception.reason, "branch-route-declaration-required")

    def test_an_undeclared_card_is_ineligible_on_every_board(self) -> None:
        from super_board_runtime.eligibility import evaluate_dispatch

        config = _config(activation_mode="active")
        decision = evaluate_dispatch(_issue("no declaration anywhere"), config)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_codes, ("route-declaration-missing",))
        self.assertIsNone(decision.selected_base_branch)


class BranchCreationTests(unittest.TestCase):
    def test_a_valid_route_creates_the_branch_from_its_declared_base(self) -> None:
        creator = RecordingBranchCreator()
        create_branch_for_route(
            _issue("Branch route: staging\n"), _config(), creator=creator, branch="fix/123-route"
        )
        self.assertEqual(creator.calls, [("fix/123-route", "staging")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
