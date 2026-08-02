"""Task 14 — closure is a disposition, not a click.

Pure stdlib `unittest`. No network, no `gh`.

The failure this file pins shut: an issue closed with no explanation reads,
downstream, exactly like finished work. The board shows it in the completion
column, the burn-down counts it, and nobody can tell it apart from something
that was actually built, tested, reviewed, and merged.

So the completion column is evidence-gated. A merged pull request qualifies
because GitHub itself attests to the merge. A closed issue qualifies with
accepted completion evidence, a linked duplicate, or a not-planned decision
that says what was decided. Everything else is reopened, moved to Blocked, and
told — in a sanitized comment — which disposition is missing.

Closures that predate activation are a separate case. Their board status is
corrected; their evidence is left exactly as it was. Back-filling modern
acceptance evidence onto a 2025 closure would be manufacturing a record of a
review that never happened.

Run directly:
  python -B tests/test_closure_normalizer.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_closure_normalizer.py' -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.normalize import (  # noqa: E402
    ACCEPTED_COMPLETION_EVIDENCE_TYPES,
    CLOSURE_DISPOSITIONS,
    IssueOrPullRequestSnapshot,
    normalize_closure,
)
from super_board_runtime.project import ProjectSnapshot  # noqa: E402
from super_board_runtime.publication import (  # noqa: E402
    sanitize_and_validate_publication,
)

FIXTURES = json.loads(
    (_REPO_ROOT / "tests" / "fixtures" / "closure-cases.json").read_text(encoding="utf-8")
)
BOUNDARY = FIXTURES["activation_boundary"]
CASES = {case["name"]: case for case in FIXTURES["cases"]}

ISSUE_NODE = "I_kwNOTAREALISSUENODE"
ITEM_NODE = "PVTI_kwNOTAREALITEMNODE"
FIELD_NODE = "PVTSSF_kwNOTAREALFIELDNODE"


def _subject(**overrides: object) -> IssueOrPullRequestSnapshot:
    fields: dict[str, object] = {
        "kind": "issue",
        "event": "closed",
        "number": 101,
        "url": "https://github.com/Bavariance/polysimulator/issues/101",
        "node_id": ISSUE_NODE,
        "state": "closed",
        "title": "Normalize closure",
        "observed_at": "2026-08-02T12:00:00Z",
        "observed_project_updated_at": "2026-08-02T09:00:00Z",
    }
    fields.update(overrides)
    return IssueOrPullRequestSnapshot(**fields)  # type: ignore[arg-type]


def _item(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "item_node_id": ITEM_NODE,
        "content_node_id": ISSUE_NODE,
        "status": "Review",
        "field_id": FIELD_NODE,
        "field_name": "Status",
        "option_id": "review-option",
        "option_name": "Review",
        "updated_at": "2026-08-02T09:00:00Z",
        "project_values": {"Status": "Review"},
    }
    record.update(overrides)
    return record


def _project(*items: dict[str, object], hit_cap: bool = False) -> ProjectSnapshot:
    return ProjectSnapshot(
        project_owner="Wladefant",
        project_number=1,
        items=tuple(items) or (_item(),),
        fields={"Status": {"id": FIELD_NODE}},
        hit_cap=hit_cap,
    )


def _case(name: str):
    """Build the (subject, project) pair for one fixture case."""
    case = CASES[name]
    raw = dict(case["subject"])
    node = raw.get("node_id", ISSUE_NODE)
    subject = _subject(**raw)
    item = _item(content_node_id=node, **case["item"])
    item["option_name"] = item["status"]
    item["project_values"] = {"Status": item["status"]}
    return case, subject, _project(item)


def _kinds(plan) -> list[str]:
    return [operation.kind for operation in plan.operations]


# ───────────────────────────── the fixture matrix ─────────────────────────────


class FixtureMatrixTests(unittest.TestCase):
    def test_every_documented_case_has_a_fixture(self) -> None:
        self.assertEqual(
            set(CASES),
            {
                "merged-pull-request",
                "closed-issue-with-completion-evidence",
                "closed-issue-with-linked-duplicate",
                "closed-issue-not-planned",
                "closed-issue-without-any-disposition",
                "closed-unmerged-pull-request-with-supersession",
                "closed-unmerged-pull-request-without-evidence",
                "reopened-issue",
                "open-issue-currently-in-done",
                "closed-issue-currently-outside-done",
            },
        )

    def test_each_fixture_produces_exactly_its_expected_outcome(self) -> None:
        for name in CASES:
            with self.subTest(case=name):
                case, subject, project = _case(name)
                plan = normalize_closure(subject, project, activation_boundary=BOUNDARY)
                expect = case["expect"]
                self.assertEqual(plan.desired_status, expect["desired_status"])
                self.assertEqual(plan.disposition, expect["disposition"])
                self.assertEqual(plan.reopen, expect["reopen"])
                self.assertEqual(plan.blocked_reason, expect["blocked_reason"])
                self.assertEqual(plan.comment is not None, expect["comment"])

    def test_the_declared_dispositions_are_the_documented_ones(self) -> None:
        self.assertEqual(
            set(CLOSURE_DISPOSITIONS),
            {
                "merged",
                "completed",
                "duplicate",
                "not-planned",
                "superseded",
                "abandoned",
                "reopened",
                "open-in-completion-column",
                "pre-activation-historical",
            },
        )


# ───────────────────────────── the completion gate ─────────────────────────────


class CompletionGateTests(unittest.TestCase):
    def test_a_closed_issue_without_a_disposition_is_reopened_and_blocked(self) -> None:
        _case_data, subject, project = _case("closed-issue-without-any-disposition")
        plan = normalize_closure(subject, project, activation_boundary=BOUNDARY)
        self.assertTrue(plan.reopen)
        self.assertEqual(plan.desired_status, "Blocked")
        self.assertIn("reopen", _kinds(plan))
        self.assertIn("status", _kinds(plan))
        self.assertIn("closure-comment", _kinds(plan))
        self.assertEqual(_kinds(plan)[0], "reopen")

    def test_the_corrective_comment_names_the_missing_disposition(self) -> None:
        _case_data, subject, project = _case("closed-issue-without-any-disposition")
        plan = normalize_closure(subject, project, activation_boundary=BOUNDARY)
        for expected in ("completion evidence", "duplicate", "not planned"):
            with self.subTest(phrase=expected):
                self.assertIn(expected, plan.comment)
        self.assertIn(subject.url, plan.comment)

    def test_empty_completion_evidence_is_not_evidence(self) -> None:
        for evidence in ({}, {"type": "merged-pull-request"}, {"url": "https://x/1"},
                         {"type": "vibes", "url": "https://x/1"}):
            with self.subTest(evidence=evidence):
                subject = _subject(closing_state="completed", completion_evidence=evidence,
                                   closed_at="2026-08-02T12:00:00Z")
                plan = normalize_closure(subject, _project(), activation_boundary=BOUNDARY)
                self.assertNotEqual(plan.desired_status, "Done")
                self.assertTrue(plan.reopen)

    def test_the_accepted_evidence_types_are_pinned(self) -> None:
        self.assertEqual(
            set(ACCEPTED_COMPLETION_EVIDENCE_TYPES),
            {"merged-pull-request", "qa-evidence", "review-evidence", "operator-acceptance"},
        )

    def test_a_duplicate_without_a_link_is_not_a_disposition(self) -> None:
        subject = _subject(closing_state="not_planned", duplicate_of="   ",
                           closed_at="2026-08-02T12:00:00Z")
        plan = normalize_closure(subject, _project(), activation_boundary=BOUNDARY)
        self.assertNotEqual(plan.desired_status, "Done")
        self.assertEqual(plan.blocked_reason, "closure-disposition-missing")

    def test_a_not_planned_reason_must_be_concrete(self) -> None:
        for reason in ("TBD", "n/a", "-", "?"):
            with self.subTest(reason=reason):
                subject = _subject(closing_state="not_planned", not_planned_reason=reason,
                                   closed_at="2026-08-02T12:00:00Z")
                plan = normalize_closure(subject, _project(), activation_boundary=BOUNDARY)
                self.assertNotEqual(plan.desired_status, "Done")
                self.assertTrue(plan.reopen)

    def test_an_already_correct_closure_plans_nothing(self) -> None:
        _case_data, subject, _project_snapshot = _case("closed-issue-with-completion-evidence")
        project = _project(_item(status="Done", option_name="Done",
                                 project_values={"Status": "Done"}))
        plan = normalize_closure(subject, project, activation_boundary=BOUNDARY)
        self.assertEqual(plan.operations, ())
        self.assertIsNone(plan.blocked_reason)
        self.assertEqual(plan.disposition, "completed")


# ───────────────────────────── pull requests ─────────────────────────────


class PullRequestClosureTests(unittest.TestCase):
    def test_a_merged_pull_request_reaches_the_completion_column(self) -> None:
        _case_data, subject, project = _case("merged-pull-request")
        plan = normalize_closure(subject, project, activation_boundary=BOUNDARY)
        self.assertEqual(plan.desired_status, "Done")
        self.assertEqual(plan.disposition, "merged")
        self.assertFalse(plan.reopen)

    def test_a_merge_without_a_confirmed_timestamp_is_refused(self) -> None:
        subject = _subject(
            kind="pull_request",
            url="https://github.com/Bavariance/polysimulator/pull/202",
            state="closed",
            closed_at="2026-08-02T12:00:00Z",
            merge_commit_sha="d" * 40,
        )
        plan = normalize_closure(subject, _project(), activation_boundary=BOUNDARY)
        self.assertNotEqual(plan.desired_status, "Done")
        self.assertEqual(plan.blocked_reason, "pull-request-disposition-missing")

    def test_no_unmerged_pull_request_ever_reaches_done_without_linked_evidence(self) -> None:
        variants = (
            {},
            {"supersession_evidence": {}},
            {"abandonment_evidence": {}},
            {"supersession_evidence": {"type": "superseded-by"}},
            {"abandonment_evidence": {"url": "   "}},
        )
        for extra in variants:
            with self.subTest(extra=sorted(extra)):
                subject = _subject(
                    kind="pull_request",
                    url="https://github.com/Bavariance/polysimulator/pull/205",
                    state="closed",
                    closed_at="2026-08-02T12:00:00Z",
                    **extra,
                )
                plan = normalize_closure(subject, _project(), activation_boundary=BOUNDARY)
                self.assertNotEqual(plan.desired_status, "Done")
                self.assertEqual(plan.desired_status, "Blocked")

    def test_linked_abandonment_evidence_is_a_disposition(self) -> None:
        subject = _subject(
            kind="pull_request",
            url="https://github.com/Bavariance/polysimulator/pull/206",
            state="closed",
            closed_at="2026-08-02T12:00:00Z",
            abandonment_evidence={
                "type": "abandoned",
                "url": "https://github.com/Bavariance/polysimulator/issues/61",
            },
        )
        plan = normalize_closure(subject, _project(), activation_boundary=BOUNDARY)
        self.assertEqual(plan.desired_status, "Done")
        self.assertEqual(plan.disposition, "abandoned")

    def test_a_closed_pull_request_is_never_reopened(self) -> None:
        _case_data, subject, project = _case("closed-unmerged-pull-request-without-evidence")
        plan = normalize_closure(subject, project, activation_boundary=BOUNDARY)
        self.assertFalse(plan.reopen)
        self.assertNotIn("reopen", _kinds(plan))


# ───────────────────────────── compare before mutate ─────────────────────────────


class CompareBeforeMutateTests(unittest.TestCase):
    def test_every_correction_carries_a_compare_decision(self) -> None:
        for name in CASES:
            with self.subTest(case=name):
                _case_data, subject, project = _case(name)
                plan = normalize_closure(subject, project, activation_boundary=BOUNDARY)
                for operation in plan.operations:
                    self.assertIsNotNone(
                        operation.decision, f"{name}/{operation.kind} has no compare decision"
                    )
                    self.assertEqual(operation.decision.action, "apply")
                    self.assertIsNotNone(operation.expected)
                    self.assertIsNotNone(operation.current)
                    self.assertIsNotNone(operation.desired)

    def test_a_board_decision_newer_than_the_event_quarantines(self) -> None:
        item = _item(status="Building", updated_at="2026-08-02T18:00:00Z")
        _case_data, subject, _unused = _case("closed-issue-with-completion-evidence")
        plan = normalize_closure(subject, _project(item), activation_boundary=BOUNDARY)
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.blocked_reason, "board-decision-newer")
        self.assertTrue(plan.quarantined)

    def test_a_card_that_is_not_on_the_board_plans_no_status_change(self) -> None:
        _case_data, subject, _unused = _case("closed-issue-with-completion-evidence")
        plan = normalize_closure(
            subject, _project(_item(content_node_id="I_kwSOMEONEELSE")),
            activation_boundary=BOUNDARY,
        )
        self.assertFalse(plan.is_member)
        self.assertNotIn("status", _kinds(plan))
        self.assertEqual(plan.blocked_reason, "closure-card-not-on-board")

    def test_a_partial_project_snapshot_fails_closed(self) -> None:
        _case_data, subject, _unused = _case("closed-issue-with-completion-evidence")
        plan = normalize_closure(
            subject, _project(hit_cap=True), activation_boundary=BOUNDARY
        )
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.blocked_reason, "project-membership-unknown")


# ───────────────────────────── publication boundary ─────────────────────────────


class SanitizedCommentTests(unittest.TestCase):
    def test_the_comment_is_already_sanitized(self) -> None:
        _case_data, subject, project = _case("closed-issue-without-any-disposition")
        plan = normalize_closure(subject, project, activation_boundary=BOUNDARY)
        again = sanitize_and_validate_publication(
            plan.comment, {}, surface="closure-comment"
        )
        self.assertEqual(again.text, plan.comment)
        self.assertTrue(again.safe)

    def test_a_credential_shaped_title_never_reaches_the_comment(self) -> None:
        # Built by concatenation so this file carries no token-shaped literal.
        sentinel = "gh" + "p_" + ("A1b2C3d4E5f6G7h8" * 2)
        _case_data, subject, project = _case("closed-issue-without-any-disposition")
        plan = normalize_closure(
            type(subject)(**{**subject.__dict__, "title": f"fix {sentinel}"}),
            project,
            activation_boundary=BOUNDARY,
        )
        self.assertNotIn(sentinel, plan.comment)
        self.assertIn("[redacted:github-token]", plan.comment)

    def test_a_credential_named_environment_value_never_reaches_the_comment(self) -> None:
        secret = "s3cr3t-" + "environment-value-that-is-long-enough"
        _case_data, subject, project = _case("closed-issue-without-any-disposition")
        plan = normalize_closure(
            type(subject)(**{**subject.__dict__, "title": f"fix {secret}"}),
            project,
            activation_boundary=BOUNDARY,
            environment={"SUPERBOARD_API_KEY": secret},
        )
        self.assertNotIn(secret, plan.comment)
        self.assertIn("[redacted:env-value]", plan.comment)

    def test_a_comment_is_published_only_on_the_closure_surface(self) -> None:
        _case_data, subject, project = _case("closed-issue-without-any-disposition")
        plan = normalize_closure(subject, project, activation_boundary=BOUNDARY)
        operation = next(op for op in plan.operations if op.kind == "closure-comment")
        self.assertEqual(operation.desired["surface"], "closure-comment")
        self.assertEqual(operation.desired["body"], plan.comment)


# ───────────────────────────── the pre-activation boundary ─────────────────────────────


class PreActivationTests(unittest.TestCase):
    HISTORIC = {
        "closed_at": "2025-11-03T08:00:00Z",
        "closing_state": "completed",
        "state": "closed",
    }

    def test_a_pre_activation_closure_is_recorded_as_historical(self) -> None:
        plan = normalize_closure(
            _subject(**self.HISTORIC), _project(), activation_boundary=BOUNDARY
        )
        self.assertTrue(plan.pre_activation)
        self.assertEqual(plan.disposition, "pre-activation-historical")

    def test_its_board_status_is_still_corrected(self) -> None:
        plan = normalize_closure(
            _subject(**self.HISTORIC), _project(), activation_boundary=BOUNDARY
        )
        self.assertEqual(plan.desired_status, "Done")
        self.assertEqual(_kinds(plan), ["status"])

    def test_no_modern_acceptance_evidence_is_manufactured(self) -> None:
        plan = normalize_closure(
            _subject(**self.HISTORIC), _project(), activation_boundary=BOUNDARY
        )
        self.assertTrue(plan.evidence_preserved)
        self.assertNotEqual(plan.disposition, "completed")
        for operation in plan.operations:
            with self.subTest(kind=operation.kind):
                self.assertNotIn("evidence", operation.kind)
                self.assertNotIn("evidence", dict(operation.desired))
                self.assertNotIn("completion_evidence", dict(operation.desired))

    def test_it_is_never_reopened_and_never_commented_on(self) -> None:
        plan = normalize_closure(
            _subject(**self.HISTORIC), _project(), activation_boundary=BOUNDARY
        )
        self.assertFalse(plan.reopen)
        self.assertIsNone(plan.comment)

    def test_existing_historical_evidence_is_left_untouched(self) -> None:
        evidence = {"type": "operator-acceptance", "url": "https://example.invalid/legacy"}
        subject = _subject(**self.HISTORIC, completion_evidence=evidence)
        plan = normalize_closure(subject, _project(), activation_boundary=BOUNDARY)
        self.assertTrue(plan.evidence_preserved)
        self.assertEqual(subject.completion_evidence, evidence)
        for operation in plan.operations:
            self.assertNotIn("completion_evidence", dict(operation.desired))

    def test_a_closure_after_the_boundary_is_held_to_the_modern_contract(self) -> None:
        subject = _subject(closed_at="2026-08-02T12:00:00Z", closing_state="completed")
        plan = normalize_closure(subject, _project(), activation_boundary=BOUNDARY)
        self.assertFalse(plan.pre_activation)
        self.assertTrue(plan.reopen)

    def test_without_a_declared_boundary_nothing_is_treated_as_historical(self) -> None:
        plan = normalize_closure(_subject(**self.HISTORIC), _project())
        self.assertFalse(plan.pre_activation)
        self.assertTrue(plan.reopen)


class ReopenedAndStrayCardTests(unittest.TestCase):
    def test_a_reopened_issue_returns_to_backlog(self) -> None:
        _case_data, subject, project = _case("reopened-issue")
        plan = normalize_closure(subject, project, activation_boundary=BOUNDARY)
        self.assertEqual(plan.desired_status, "Backlog")
        self.assertEqual(plan.disposition, "reopened")
        self.assertNotEqual(plan.desired_status, "Ready")

    def test_an_open_card_is_pulled_out_of_the_completion_column(self) -> None:
        _case_data, subject, project = _case("open-issue-currently-in-done")
        plan = normalize_closure(subject, project, activation_boundary=BOUNDARY)
        self.assertEqual(plan.desired_status, "Backlog")
        self.assertEqual(plan.disposition, "open-in-completion-column")

    def test_an_open_card_outside_the_completion_column_is_left_alone(self) -> None:
        subject = _subject(state="open", event="schedule")
        plan = normalize_closure(
            subject, _project(_item(status="Building", option_name="Building",
                                    project_values={"Status": "Building"})),
            activation_boundary=BOUNDARY,
        )
        self.assertEqual(plan.operations, ())
        self.assertIsNone(plan.desired_status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
