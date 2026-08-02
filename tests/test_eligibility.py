"""Task 3 — dispatch eligibility and excluded-label enforcement.

Pure stdlib `unittest`. No network, no `gh`.

Run directly:
  python -B tests/test_eligibility.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_eligibility.py' -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.config import load_and_validate_config  # noqa: E402
from super_board_runtime.eligibility import (  # noqa: E402
    IssueSnapshot,
    evaluate_dispatch,
    plan_dispatch,
    snapshot_from_project_item,
)
from super_board_runtime.routing import (  # noqa: E402
    FRANKFURT_LABEL,
    NON_DISPATCH_BRANCHES,
    resolve_branch_route,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NON_READY_STATUSES = ("Backlog", "Building", "QA", "Review", "Blocked", "Done")


def _config(**overrides: object):
    payload = {
        "version": 1,
        "variant": "full",
        "project": {"owner": "Bavariance", "number": 1},
        "repo": {"remote": "Bavariance/polysimulator"},
        "base_branch": "staging",
        "activation_mode": "active",
    }
    payload.update(overrides)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_and_validate_config(path)


def _issue(**overrides: object) -> IssueSnapshot:
    base = {
        "url": "https://github.com/Bavariance/polysimulator/issues/12",
        "node_id": "I_kwDOexample12",
        "number": 12,
        "content_type": "Issue",
        "state": "OPEN",
        "title": "a perfectly formed card",
        # A perfectly formed card declares its branch route. Routing is
        # fail-closed, so a card that says nothing is not dispatchable.
        "body": "## Acceptance Criteria\n- [ ] it works\n\nBranch route: staging\n",
        "labels": (),
        "assignees": (),
        "status": "Ready",
        "milestone": None,
    }
    base.update(overrides)
    return IssueSnapshot(**base)  # type: ignore[arg-type]


def _item(
    number: int,
    status: str,
    *,
    labels=(),
    assignees=(),
    content_type="Issue",
    title=None,
    state="OPEN",
    body="Branch route: staging\n",
):
    """One `gh project item-list --format json` item.

    `state=None` models the real payload, which does not carry issue state — it
    forces the runtime to reach for the injected state lookup. `body` carries
    the branch-route declaration the intake normalizer writes; a card without
    one is not dispatchable.
    """
    return {
        "status": status,
        "labels": list(labels),
        "content": {
            "type": content_type,
            "number": number,
            "title": title or f"card {number}",
            "url": f"https://github.com/Bavariance/polysimulator/issues/{number}",
            "assignees": list(assignees),
            "state": state,
            "body": body,
        },
    }


class RecordingGitHubClient:
    """Mock GitHub client that records every read and every write."""

    def __init__(self, states: dict[int, str]) -> None:
        self.states = states
        self.reads: list[int] = []
        self.writes: list[tuple[str, int]] = []

    # read path — the only method the runtime is allowed to reach for
    def issue_state(self, issue: IssueSnapshot) -> str | None:
        self.reads.append(issue.number)
        return self.states.get(issue.number)

    # write path — must never fire during planning
    def claim(self, number: int) -> None:
        self.writes.append(("claim", number))

    def move(self, number: int) -> None:
        self.writes.append(("move", number))

    def branch(self, number: int) -> None:
        self.writes.append(("branch", number))

    def comment(self, number: int) -> None:
        self.writes.append(("comment", number))


class StatusGateTests(unittest.TestCase):
    def test_ready_is_the_only_dispatchable_status(self) -> None:
        decision = evaluate_dispatch(_issue(status="Ready"), _config())
        self.assertTrue(decision.eligible, decision.reason_codes)
        self.assertEqual(decision.reason_codes, ())
        self.assertEqual(decision.issue_number, 12)
        self.assertEqual(decision.selected_base_branch, "staging")

    def test_every_non_ready_status_is_rejected(self) -> None:
        for status in NON_READY_STATUSES:
            for backend in ("claude-p", "workflow"):
                with self.subTest(status=status, worker_backend=backend):
                    decision = evaluate_dispatch(
                        _issue(status=status), _config(worker_backend=backend)
                    )
                    self.assertFalse(decision.eligible)
                    self.assertEqual(decision.reason_codes, ("status-not-ready",))

    def test_unrecognised_and_retired_statuses_are_rejected(self) -> None:
        for status in ("Skipped", "In Progress", "", None):
            with self.subTest(status=status):
                decision = evaluate_dispatch(_issue(status=status), _config())
                self.assertFalse(decision.eligible)
                self.assertEqual(decision.reason_codes, ("status-not-ready",))

    def test_only_the_exact_spelling_dispatches(self) -> None:
        """`Ready` means `Ready`. Not `ready`, not ` Ready `, not `READY`.

        Canonicalization belongs to schema/alias validation. Applying it to the
        dispatch gate turns the exactly-`Ready` invariant into a
        case-and-whitespace-insensitive match, so a board whose column was
        renamed or whose payload carries stray whitespace dispatches anyway.
        """
        for status in ("ready", " Ready ", "READY", "\tReady\n", "rEaDy"):
            with self.subTest(status=status):
                decision = evaluate_dispatch(_issue(status=status), _config())
                self.assertFalse(
                    decision.eligible,
                    f"{status!r} is not the exact dispatchable status",
                )
                self.assertEqual(decision.reason_codes, ("status-not-ready",))

    def test_plan_dispatch_refuses_near_miss_spellings(self) -> None:
        items = [_item(40, "ready"), _item(41, " Ready "), _item(42, "Ready")]
        plan = plan_dispatch(items, _config())
        self.assertEqual([card["number"] for card in plan.cards], [42])


class LabelExclusionTests(unittest.TestCase):
    def test_design_card_is_skipped_and_the_next_card_is_chosen(self) -> None:
        items = [_item(30, "Ready", labels=["design"]), _item(31, "Ready")]
        plan = plan_dispatch(items, _config())
        self.assertEqual([card["number"] for card in plan.cards], [31])

    def test_history_card_is_skipped_and_the_next_card_is_chosen(self) -> None:
        items = [_item(30, "Ready", labels=["history"]), _item(31, "Ready")]
        plan = plan_dispatch(items, _config())
        self.assertEqual([card["number"] for card in plan.cards], [31])

    def test_both_exclusions_active_simultaneously(self) -> None:
        items = json.loads((FIXTURES / "wave-items-excluded.json").read_text(encoding="utf-8"))["items"]
        plan = plan_dispatch(items, _config())
        self.assertEqual([card["number"] for card in plan.cards], [42])
        excluded = {
            decision.issue_number: decision
            for decision in plan.decisions
            if "excluded-label" in decision.reason_codes
        }
        self.assertEqual(sorted(excluded), [40, 41, 43])

    def test_design_and_history_are_excluded_even_with_empty_exclude_labels(self) -> None:
        # `design` cards are human-designer-owned and `history` cards are an
        # archive: they are non-dispatchable whether a config lists them or not.
        for exclude in ([], None):
            with self.subTest(exclude_labels=exclude):
                config = _config() if exclude is None else _config(exclude_labels=exclude)
                items = [_item(30, "Ready", labels=["design"]), _item(31, "Ready")]
                self.assertEqual([c["number"] for c in plan_dispatch(items, config).cards], [31])

    def test_empty_and_absent_exclude_labels_preserve_prior_selection(self) -> None:
        items = [_item(30, "Ready", labels=["bug"]), _item(31, "Ready")]
        for exclude in ([], None):
            with self.subTest(exclude_labels=exclude):
                config = _config() if exclude is None else _config(exclude_labels=exclude)
                self.assertEqual([c["number"] for c in plan_dispatch(items, config).cards], [30, 31])

    def test_configured_labels_are_honoured_and_added_to_the_permanent_set(self) -> None:
        items = [_item(30, "Ready", labels=["bug"]), _item(31, "Ready")]
        config = _config(exclude_labels=["bug"])
        self.assertEqual([c["number"] for c in plan_dispatch(items, config).cards], [31])

    def test_label_comparison_is_case_insensitive_after_trimming(self) -> None:
        for label in ("  DeSiGn ", "HISTORY", "\tdesign\n"):
            with self.subTest(label=label):
                decision = evaluate_dispatch(_issue(labels=(label,)), _config())
                self.assertFalse(decision.eligible)
                self.assertEqual(decision.reason_codes, ("excluded-label",))

    def test_label_objects_are_accepted_as_well_as_strings(self) -> None:
        item = _item(30, "Ready")
        item["labels"] = [{"name": "Design"}]
        decision = evaluate_dispatch(snapshot_from_project_item(item), _config())
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_codes, ("excluded-label",))


class NoWriteForExcludedCardsTests(unittest.TestCase):
    def test_excluded_cards_are_never_claimed_moved_branched_or_commented_on(self) -> None:
        items = [
            _item(40, "Ready", labels=["design"], state=None),
            _item(41, "Ready", labels=["history"], state=None),
            _item(42, "Backlog", state=None),
            _item(43, "Ready", state=None),
        ]
        client = RecordingGitHubClient({40: "OPEN", 41: "OPEN", 42: "OPEN", 43: "OPEN"})
        plan = plan_dispatch(items, _config(), state_lookup=client.issue_state)
        self.assertEqual([card["number"] for card in plan.cards], [43])
        self.assertEqual(client.writes, [], "planning must never write to GitHub")
        # Excluded and non-Ready cards are cheap-rejected before any API read.
        self.assertEqual(client.reads, [43])


class ContentTypeAndLookupTests(unittest.TestCase):
    def test_pull_request_cards_can_never_dispatch(self) -> None:
        decision = evaluate_dispatch(
            _issue(content_type="PullRequest", status="Ready"), _config()
        )
        self.assertFalse(decision.eligible)
        self.assertIn("content-type-not-issue", decision.reason_codes)

    def test_draft_issue_cards_do_not_crash_planning(self) -> None:
        items = [
            {"status": "Ready", "content": {"type": "DraftIssue", "title": "draft, not an issue"}},
            _item(31, "Ready"),
        ]
        plan = plan_dispatch(items, _config())
        self.assertEqual([card["number"] for card in plan.cards], [31])
        self.assertIn("content-type-not-issue", plan.decisions[0].reason_codes)

    def test_label_less_issues_do_not_crash_planning(self) -> None:
        item = _item(31, "Ready")
        item.pop("labels")
        snapshot = snapshot_from_project_item(item)
        self.assertEqual(snapshot.labels, ())
        self.assertTrue(evaluate_dispatch(snapshot, _config()).eligible)

    def test_failed_issue_state_lookup_is_not_a_permissive_fallback(self) -> None:
        def failing_lookup(_issue: IssueSnapshot) -> str | None:
            return None

        decision = evaluate_dispatch(
            _issue(state=None), _config(), state_lookup=failing_lookup
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_codes, ("issue-state-unavailable",))

    def test_raising_issue_state_lookup_is_not_a_permissive_fallback(self) -> None:
        def exploding_lookup(_issue: IssueSnapshot) -> str | None:
            raise RuntimeError("gh exited 1")

        decision = evaluate_dispatch(
            _issue(state=None), _config(), state_lookup=exploding_lookup
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_codes, ("issue-state-unavailable",))

    def test_missing_state_without_any_lookup_is_rejected(self) -> None:
        decision = evaluate_dispatch(_issue(state=None), _config())
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_codes, ("issue-state-unavailable",))

    def test_closed_issues_are_rejected(self) -> None:
        decision = evaluate_dispatch(_issue(state="CLOSED"), _config())
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_codes, ("issue-not-open",))

    def test_claimed_cards_are_skipped(self) -> None:
        decision = evaluate_dispatch(_issue(assignees=("someone",)), _config())
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_codes, ("already-claimed",))


class BranchRouteTests(unittest.TestCase):
    """Eligibility routes through ONE authority: `routing.resolve_branch_route`.

    It used to carry a second, more permissive copy of "which base branch does
    this card get" — one that trusted any configured route label and fell back
    to `config.base_branch` when a card declared nothing. The two copies
    disagreed about the same card, and the permissive one is the one that would
    have handed a worker its base branch.
    """

    def test_a_route_label_naming_a_non_dispatch_branch_is_ineligible(self) -> None:
        # PREVIOUSLY LOCKED THE OPPOSITE. This assertion used to require a
        # `route:main` card to be ELIGIBLE with `selected_base_branch == "main"`
        # while `resolve_branch_route` refused the very same route, because
        # `main` is a repository default branch listed in NON_DISPATCH_BRANCHES.
        # Routing is the correct layer; the eligibility assertion was wrong.
        config = _config(branch_routes={"route:main": "main", "route:staging": "staging"})
        decision = evaluate_dispatch(_issue(labels=("route:main",)), config)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_codes, ("route-declaration-unknown",))
        self.assertIsNone(decision.selected_base_branch)
        self.assertIsNone(decision.branch_declaration)

    def test_a_declared_route_selects_its_branch(self) -> None:
        decision = evaluate_dispatch(_issue(body="Branch route: staging\n"), _config())
        self.assertTrue(decision.eligible, decision.reason_codes)
        self.assertEqual(decision.branch_declaration, "staging")
        self.assertEqual(decision.selected_base_branch, "staging")

    def test_no_declaration_is_ineligible_and_never_the_base_branch(self) -> None:
        # PREVIOUSLY LOCKED THE OPPOSITE: an undeclared card used to inherit
        # `config.base_branch`. A fallback base branch is a branch nobody chose.
        decision = evaluate_dispatch(_issue(body="No declaration anywhere.\n"), _config())
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_codes, ("route-declaration-missing",))
        self.assertIsNone(decision.selected_base_branch)

    def test_conflicting_route_labels_fail_closed(self) -> None:
        config = _config(
            branch_routes={"branch:staging": "staging", FRANKFURT_LABEL: "staging-frankfurt"}
        )
        decision = evaluate_dispatch(
            _issue(labels=("branch:staging", FRANKFURT_LABEL)), config
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_codes, ("route-label-conflict",))
        self.assertIsNone(decision.selected_base_branch)

    def test_eligibility_and_routing_agree_on_every_non_dispatch_branch(self) -> None:
        # The contradiction this class exists to prevent: the same card, judged
        # by the two layers, must reach the same verdict and the same code.
        config = _config()
        for branch in NON_DISPATCH_BRANCHES:
            with self.subTest(branch=branch):
                issue = _issue(body=f"Branch route: {branch}\n")
                route = resolve_branch_route(issue, config)
                decision = evaluate_dispatch(issue, config)
                self.assertFalse(route.valid)
                self.assertFalse(decision.eligible)
                self.assertEqual(decision.reason_codes, (route.reason_code,))
                self.assertIsNone(decision.selected_base_branch)


class PlanShapeAndCliTests(unittest.TestCase):
    def test_plan_respects_max_workers(self) -> None:
        items = [_item(n, "Ready") for n in range(50, 60)]
        plan = plan_dispatch(items, _config(max_workers=2))
        self.assertEqual([card["number"] for card in plan.cards], [50, 51])

    def test_decision_serializes_to_the_documented_shape(self) -> None:
        decision = evaluate_dispatch(_issue(labels=("design",)), _config())
        payload = decision.to_dict()
        self.assertEqual(
            sorted(payload),
            [
                "activation_mode",
                "branch_declaration",
                "eligible",
                "issue_node_id",
                "issue_number",
                "issue_url",
                "reason_codes",
                "selected_base_branch",
            ],
        )
        self.assertEqual(payload["reason_codes"], ["excluded-label"])
        self.assertEqual(payload["activation_mode"], "active")

    def test_cli_reads_items_from_stdin_and_emits_sorted_json(self) -> None:
        items = (FIXTURES / "wave-items-excluded.json").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "variant": "full",
                        "project": {"owner": "Bavariance", "number": 1},
                        "repo": {"remote": "Bavariance/polysimulator"},
                        "base_branch": "staging",
                        "activation_mode": "active",
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "super_board_runtime.eligibility",
                    "--items",
                    "-",
                    "--config",
                    str(config_path),
                ],
                input=items,
                capture_output=True,
                text=True,
                cwd=str(_SCRIPTS),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual([card["number"] for card in payload["cards"]], [42])
        self.assertEqual(payload["activation_mode"], "active")
        self.assertEqual(payload["exclude_labels"], ["design", "history"])
        self.assertEqual(list(payload), sorted(payload))

    def test_cli_rejects_a_bad_config_with_65_and_bad_items_with_65(self) -> None:
        def run(config_payload: str, items: str) -> subprocess.CompletedProcess:
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "config.json"
                config_path.write_text(config_payload, encoding="utf-8")
                return subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "super_board_runtime.eligibility",
                        "--items",
                        "-",
                        "--config",
                        str(config_path),
                    ],
                    input=items,
                    capture_output=True,
                    text=True,
                    cwd=str(_SCRIPTS),
                )

        good_config = json.dumps(
            {
                "version": 1,
                "project": {"owner": "Bavariance", "number": 1},
                "activation_mode": "active",
            }
        )
        bad_config = json.dumps({"version": 1, "project": {"owner": "x", "number": 1}, "columns": []})
        self.assertEqual(run(bad_config, '{"items":[]}').returncode, 65)
        broken_items = run(good_config, "{not json")
        self.assertEqual(broken_items.returncode, 65)
        self.assertEqual(broken_items.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
