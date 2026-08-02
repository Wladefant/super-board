"""Task 13 — continuous intake normalization and the canonical issue form.

Pure stdlib `unittest`. No network, no `gh`.

The rule this file pins: intake is normalized on **every** issue and pull-request
event, and an intake that does not carry the complete canonical form is never
promoted. Incomplete work stays in a holding column with a named reason instead
of quietly becoming dispatchable.

Run directly:
  python -B tests/test_intake_normalizer.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_intake_normalizer.py' -v
"""

from __future__ import annotations

import re
import sys
import unittest
from dataclasses import replace
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.normalize import (  # noqa: E402
    ENVIRONMENT_CONSTRAINT_LABEL,
    INTAKE_ISSUE_EVENTS,
    INTAKE_PULL_REQUEST_EVENTS,
    LEGACY_ENVIRONMENT_ALIASES,
    PERIODIC_SWEEP_EVENT,
    PRIORITIES,
    REQUIRED_INTAKE_SECTIONS,
    REQUIRED_PULL_REQUEST_CLASSIFICATION_FIELDS,
    WORK_TYPES,
    IssueSnapshot,
    canonical_environment_label,
    classify_pull_request,
    handles_event,
    normalize_intake,
    parse_intake_form,
)
from super_board_runtime.project import ProjectSnapshot  # noqa: E402

ISSUE_NODE = "I_kwNOTAREALISSUENODE"
PR_NODE = "PR_kwNOTAREALPULLNODE"
ITEM_NODE = "PVTI_kwNOTAREALITEMNODE"
FIELD_NODE = "PVTSSF_kwNOTAREALFIELDNODE"
FORM_YAML = _REPO_ROOT / "payload" / "github" / "ISSUE_TEMPLATE" / "superboard-issue.yml"
WORKFLOW_YAML = _REPO_ROOT / "payload" / "github" / "workflows" / "super-board-normalize.yml"
NORMALIZE_CLI = _REPO_ROOT / "scripts" / "super-board-normalize.py"


def _form_body(**overrides: object) -> str:
    """A rendered GitHub issue form, section by section."""
    sections = {
        "Context": "The board cannot classify unstructured cards.",
        "Steps": "1. Write the normalizer.\n2. Wire the workflow.",
        "Acceptance criteria": "- [ ] Given a form, When normalized, Then it is classified.",
        "Test Area": "runtime",
        "Priority": "P1",
        "Work type": "build",
        "Environment constraint": "none",
        "Branch route": "staging",
        "Milestone": "Phase 0 - Install",
    }
    for key, value in overrides.items():
        name = key.replace("_", " ")
        if value is None:
            sections.pop(name, None)
        else:
            sections[name] = str(value)
    return "\n\n".join(f"### {name}\n\n{value}" for name, value in sections.items()) + "\n"


def _issue(**overrides: object) -> IssueSnapshot:
    fields: dict[str, object] = {
        "kind": "issue",
        "event": "opened",
        "number": 101,
        "url": "https://github.com/Bavariance/polysimulator/issues/101",
        "node_id": ISSUE_NODE,
        "state": "open",
        "title": "Normalize intake",
        "body": _form_body(),
        "labels": (),
        "milestone": "Phase 0 - Install",
        "observed_at": "2026-08-02T10:00:00Z",
        "observed_project_updated_at": "2026-08-02T09:00:00Z",
    }
    fields.update(overrides)
    return IssueSnapshot(**fields)  # type: ignore[arg-type]


def _pull_request(**overrides: object) -> IssueSnapshot:
    fields: dict[str, object] = {
        "kind": "pull_request",
        "event": "opened",
        "number": 202,
        "url": "https://github.com/Bavariance/polysimulator/pull/202",
        "node_id": PR_NODE,
        "state": "open",
        "title": "feat: normalize intake",
        "body": "Closes #101",
        "labels": (),
        "milestone": None,
        "observed_at": "2026-08-02T10:00:00Z",
        "observed_project_updated_at": "2026-08-02T09:00:00Z",
        "base_branch": "staging",
        "head_branch": "feat/normalize-intake",
        "head_sha": "b" * 40,
        "draft": False,
        "reviews": ({"state": "APPROVED", "author": "reviewer"},),
        "unresolved_discussions": 0,
        "checks": ({"name": "ci", "conclusion": "success"},),
        "mergeable": "MERGEABLE",
        "linked_issues": (_issue(),),
        "closing_state": None,
    }
    fields.update(overrides)
    return IssueSnapshot(**fields)  # type: ignore[arg-type]


def _item(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "item_node_id": ITEM_NODE,
        "content_node_id": ISSUE_NODE,
        "status": "Backlog",
        "field_id": FIELD_NODE,
        "field_name": "Status",
        "option_id": "backlog-option",
        "option_name": "Backlog",
        "updated_at": "2026-08-02T09:00:00Z",
        "project_values": {"Status": "Backlog"},
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


class _CountingProject:
    """A ProjectSnapshot whose item collection counts how often it is read."""

    def __init__(self, snapshot: ProjectSnapshot) -> None:
        self._snapshot = snapshot
        self.reads = 0

    @property
    def items(self):
        self.reads += 1
        return self._snapshot.items

    def __getattr__(self, name):
        return getattr(self._snapshot, name)


def _kinds(plan) -> list[str]:
    return [operation.kind for operation in plan.operations]


def _operation(plan, kind: str):
    for operation in plan.operations:
        if operation.kind == kind:
            return operation
    raise AssertionError(f"no {kind!r} operation in {_kinds(plan)}")


# ───────────────────────────── the canonical form ─────────────────────────────


class CanonicalFormTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = FORM_YAML.read_text(encoding="utf-8")
        self.labels = re.findall(r'^\s*label:\s*"([^"]+)"', self.text, re.MULTILINE)

    def test_the_form_asks_for_every_required_section(self) -> None:
        for section in REQUIRED_INTAKE_SECTIONS:
            with self.subTest(section=section):
                self.assertIn(section, self.labels)

    def test_the_work_type_field_is_named_work_type_not_type(self) -> None:
        self.assertIn("Work type", self.labels)
        self.assertNotIn("Type", self.labels)

    def test_the_branch_route_field_offers_exactly_two_values(self) -> None:
        block = self.text.split('label: "Branch route"', 1)[1].split("- type:", 1)[0]
        options = re.findall(r"^\s*-\s*(\S+)\s*$", block, re.MULTILINE)
        self.assertEqual(options, ["staging", "staging-frankfurt"])

    def test_the_environment_section_emits_the_canonical_taxonomy(self) -> None:
        block = self.text.split('label: "Environment constraint"', 1)[1].split("- type:", 1)[0]
        self.assertIn(ENVIRONMENT_CONSTRAINT_LABEL, block)
        for alias in LEGACY_ENVIRONMENT_ALIASES:
            self.assertIn(alias, block)

    def test_the_priority_field_offers_the_canonical_priorities(self) -> None:
        block = self.text.split('label: "Priority"', 1)[1].split("- type:", 1)[0]
        for priority in PRIORITIES:
            self.assertIn(priority, block)

    def test_the_work_type_field_offers_the_canonical_work_types(self) -> None:
        block = self.text.split('label: "Work type"', 1)[1].split("- type:", 1)[0]
        for work_type in WORK_TYPES:
            self.assertIn(work_type, block)

    def test_the_form_offers_milestone_and_a_blocked_reason(self) -> None:
        self.assertIn("Milestone", self.labels)
        self.assertIn("Blocked reason", self.labels)


class EnvironmentTaxonomyTests(unittest.TestCase):
    def test_the_canonical_term_maps_to_itself(self) -> None:
        self.assertEqual(
            canonical_environment_label(ENVIRONMENT_CONSTRAINT_LABEL),
            ENVIRONMENT_CONSTRAINT_LABEL,
        )

    def test_the_legacy_alias_is_preserved_and_mapped(self) -> None:
        for alias in LEGACY_ENVIRONMENT_ALIASES:
            with self.subTest(alias=alias):
                self.assertEqual(
                    canonical_environment_label(alias), ENVIRONMENT_CONSTRAINT_LABEL
                )
        self.assertIn("laptop", LEGACY_ENVIRONMENT_ALIASES)

    def test_none_and_unknown_values_map_to_nothing(self) -> None:
        for value in ("none", "", "  ", "workstation", None):
            with self.subTest(value=value):
                self.assertIsNone(canonical_environment_label(value))


# ───────────────────────────── the trigger set ─────────────────────────────


class TriggerSetTests(unittest.TestCase):
    ISSUE_EVENTS = (
        "opened",
        "edited",
        "labeled",
        "unlabeled",
        "milestoned",
        "demilestoned",
        "reopened",
        "closed",
    )
    PULL_REQUEST_EVENTS = (
        "opened",
        "edited",
        "synchronize",
        "ready_for_review",
        "converted_to_draft",
        "labeled",
        "unlabeled",
        "milestoned",
        "demilestoned",
        "reopened",
        "closed",
        "merged",
    )

    def test_every_issue_event_is_declared_and_handled(self) -> None:
        self.assertEqual(set(INTAKE_ISSUE_EVENTS), set(self.ISSUE_EVENTS))
        for event in self.ISSUE_EVENTS:
            with self.subTest(event=event):
                self.assertTrue(handles_event("issue", event))

    def test_every_pull_request_event_is_declared_and_handled(self) -> None:
        self.assertEqual(set(INTAKE_PULL_REQUEST_EVENTS), set(self.PULL_REQUEST_EVENTS))
        for event in self.PULL_REQUEST_EVENTS:
            with self.subTest(event=event):
                self.assertTrue(handles_event("pull_request", event))

    def test_the_periodic_sweep_is_part_of_the_trigger_set(self) -> None:
        self.assertTrue(handles_event("issue", PERIODIC_SWEEP_EVENT))
        self.assertTrue(handles_event("pull_request", PERIODIC_SWEEP_EVENT))

    def test_an_unlisted_event_is_not_handled(self) -> None:
        for event in ("assigned", "pinned", "transferred", ""):
            with self.subTest(event=event):
                self.assertFalse(handles_event("issue", event))

    def test_every_listed_event_actually_invokes_the_normalizer(self) -> None:
        for event in self.ISSUE_EVENTS + (PERIODIC_SWEEP_EVENT,):
            with self.subTest(event=event):
                plan = normalize_intake(_issue(event=event), _project())
                self.assertEqual(plan.event, event)
                self.assertTrue(plan.normalized)

    def test_the_workflow_declares_the_whole_trigger_set(self) -> None:
        text = WORKFLOW_YAML.read_text(encoding="utf-8")
        issue_block = text.split("issues:", 1)[1].split("pull_request", 1)[0]
        for event in self.ISSUE_EVENTS:
            with self.subTest(scope="issues", event=event):
                self.assertIn(event, issue_block)
        pull_block = text.split("pull_request", 1)[1].split("schedule:", 1)[0]
        for event in self.PULL_REQUEST_EVENTS:
            with self.subTest(scope="pull_request", event=event):
                self.assertIn(event, pull_block)
        self.assertIn("schedule:", text)

    def test_the_workflow_calls_the_normalizer_cli(self) -> None:
        text = WORKFLOW_YAML.read_text(encoding="utf-8")
        self.assertIn("super-board-normalize.py", text)
        self.assertNotIn("__", text.replace("__PROJECT_URL__", ""))

    def test_the_normalizer_cli_exists_and_declares_both_subcommands(self) -> None:
        text = NORMALIZE_CLI.read_text(encoding="utf-8")
        self.assertIn("intake", text)
        self.assertIn("--issue", text)


# ───────────────────────────── form completeness ─────────────────────────────


class FormCompletenessTests(unittest.TestCase):
    def test_a_complete_form_parses_every_section(self) -> None:
        form = parse_intake_form(_form_body())
        self.assertTrue(form.complete)
        self.assertEqual(form.missing, ())
        self.assertEqual(form.work_type, "build")
        self.assertEqual(form.test_area, "runtime")
        self.assertEqual(form.priority, "P1")
        self.assertEqual(form.branch_route, "staging")

    def test_each_missing_section_blocks_and_plans_nothing(self) -> None:
        for section in ("Context", "Steps", "Acceptance criteria", "Test Area", "Priority",
                        "Work type", "Environment constraint", "Branch route"):
            with self.subTest(section=section):
                body = _form_body(**{section.replace(" ", "_"): None})
                plan = normalize_intake(_issue(body=body), _project())
                self.assertEqual(plan.operations, ())
                self.assertIsNotNone(plan.blocked_reason)
                self.assertIn(section.casefold().replace(" ", "-"), plan.blocked_reason)
                self.assertNotEqual(plan.desired_status, "Ready")

    def test_an_empty_section_counts_as_missing(self) -> None:
        plan = normalize_intake(_issue(body=_form_body(Steps="   ")), _project())
        self.assertEqual(plan.operations, ())
        self.assertIn("steps", plan.blocked_reason)

    def test_an_incomplete_card_is_never_promoted_to_ready(self) -> None:
        plan = normalize_intake(_issue(body="free-form prose, no sections"), _project())
        self.assertEqual(plan.operations, ())
        self.assertNotEqual(plan.desired_status, "Ready")
        self.assertFalse(any(op.desired.get("status") == "Ready" for op in plan.operations))

    def test_an_incomplete_card_outside_a_holding_column_is_demoted_to_blocked(self) -> None:
        project = _project(_item(status="Ready", option_name="Ready"))
        plan = normalize_intake(_issue(body="free-form prose"), project)
        self.assertEqual(_kinds(plan), ["status"])
        self.assertEqual(plan.desired_status, "Blocked")
        self.assertNotEqual(plan.desired_status, "Ready")


class BranchRouteTests(unittest.TestCase):
    def test_both_canonical_routes_are_accepted(self) -> None:
        for route, labels in (
            ("staging", ()),
            ("staging-frankfurt", ("branch:staging-frankfurt",)),
        ):
            with self.subTest(route=route):
                plan = normalize_intake(
                    _issue(body=_form_body(Branch_route=route), labels=labels), _project()
                )
                self.assertIsNone(plan.blocked_reason)
                self.assertEqual(plan.branch_route, route)

    def test_everything_else_is_refused(self) -> None:
        for route in ("default", "main", "designstaging", "staging2", "Staging Frankfurt"):
            with self.subTest(route=route):
                plan = normalize_intake(_issue(body=_form_body(Branch_route=route)), _project())
                self.assertEqual(plan.operations, ())
                self.assertIsNotNone(plan.blocked_reason)
                self.assertNotEqual(plan.desired_status, "Ready")

    def test_an_empty_route_is_refused(self) -> None:
        plan = normalize_intake(_issue(body=_form_body(Branch_route="   ")), _project())
        self.assertEqual(plan.operations, ())
        self.assertIsNotNone(plan.blocked_reason)

    def test_a_contradicting_inline_declaration_is_refused(self) -> None:
        body = _form_body(Branch_route="staging") + "\nBranch route: staging-frankfurt\n"
        plan = normalize_intake(_issue(body=body), _project())
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.blocked_reason, "route-declaration-duplicate")

    def test_the_frankfurt_route_must_carry_its_label(self) -> None:
        plan = normalize_intake(
            _issue(body=_form_body(Branch_route="staging-frankfurt")), _project()
        )
        self.assertEqual(plan.blocked_reason, "route-label-conflict")
        self.assertEqual(plan.operations, ())


class MilestoneTests(unittest.TestCase):
    def test_the_milestone_is_read_from_the_repository(self) -> None:
        plan = normalize_intake(_issue(), _project())
        self.assertEqual(plan.milestone, "Phase 0 - Install")
        self.assertEqual(plan.milestone_source, "repository")

    def test_the_milestone_is_never_duplicated_into_a_project_field(self) -> None:
        plan = normalize_intake(_issue(milestone=None), _project())
        for operation in plan.operations:
            with self.subTest(kind=operation.kind):
                self.assertNotIn("Milestone", dict(operation.desired.get("project_values", {})))
                self.assertNotEqual(operation.desired.get("field_name"), "Milestone")
        milestone_ops = [op for op in plan.operations if op.kind == "repository-milestone"]
        for operation in milestone_ops:
            self.assertEqual(operation.desired["target"], "repository")

    def test_a_declared_milestone_missing_from_the_repository_is_synchronized(self) -> None:
        plan = normalize_intake(_issue(milestone=None), _project())
        operation = _operation(plan, "repository-milestone")
        self.assertEqual(operation.desired["milestone"], "Phase 0 - Install")
        self.assertIsNone(operation.current.project_values.get("milestone"))

    def test_no_milestone_and_no_blocked_reason_is_refused(self) -> None:
        body = _form_body(Milestone=None)
        plan = normalize_intake(_issue(body=body, milestone=None), _project())
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.blocked_reason, "intake-milestone-or-blocked-reason-missing")

    def test_a_concrete_blocked_reason_substitutes_for_a_milestone(self) -> None:
        body = _form_body(Milestone=None) + "\n### Blocked reason\n\nWaiting on the DPA review.\n"
        plan = normalize_intake(_issue(body=body, milestone=None), _project())
        self.assertIsNone(plan.blocked_reason)
        self.assertEqual(plan.declared_blocked_reason, "Waiting on the DPA review.")


class BlockedReasonTests(unittest.TestCase):
    def test_blocked_without_a_concrete_reason_is_refused(self) -> None:
        project = _project(_item(status="Blocked", option_name="Blocked"))
        body = _form_body(Milestone=None)
        plan = normalize_intake(_issue(body=body, milestone=None), project)
        self.assertEqual(plan.operations, ())
        self.assertIsNotNone(plan.blocked_reason)

    def test_a_placeholder_blocked_reason_is_refused(self) -> None:
        for placeholder in ("TBD", "tbd", "n/a", "-", "?", "see above"):
            with self.subTest(placeholder=placeholder):
                body = (
                    _form_body(Milestone=None)
                    + f"\n### Blocked reason\n\n{placeholder}\n"
                )
                plan = normalize_intake(_issue(body=body, milestone=None), _project())
                self.assertEqual(plan.operations, ())
                self.assertEqual(plan.blocked_reason, "blocked-reason-not-concrete")


class ReopenTests(unittest.TestCase):
    def test_a_reopened_issue_normalizes_to_backlog(self) -> None:
        project = _project(_item(status="Done", option_name="Done"))
        plan = normalize_intake(_issue(event="reopened", state="open"), project)
        self.assertEqual(plan.desired_status, "Backlog")
        operation = _operation(plan, "status")
        self.assertEqual(operation.desired["status"], "Backlog")
        self.assertEqual(operation.current.status, "Done")

    def test_a_reopened_issue_with_an_incomplete_form_still_lands_in_a_holding_column(self) -> None:
        project = _project(_item(status="Done", option_name="Done"))
        plan = normalize_intake(
            _issue(event="reopened", body="free-form prose"), project
        )
        self.assertIn(plan.desired_status, ("Backlog", "Blocked"))
        self.assertNotEqual(plan.desired_status, "Ready")


# ───────────────────────────── membership ─────────────────────────────


class MembershipTests(unittest.TestCase):
    def test_membership_is_checked_exactly_once(self) -> None:
        counting = _CountingProject(_project())
        plan = normalize_intake(_issue(), counting)
        self.assertEqual(counting.reads, 1)
        self.assertEqual(plan.membership_lookups, 1)

    def test_membership_is_keyed_by_the_immutable_content_node_id(self) -> None:
        plan = normalize_intake(_issue(), _project())
        self.assertEqual(plan.membership_key, "content_node_id")
        self.assertTrue(plan.is_member)

    def test_a_missing_card_is_added_before_any_field_update(self) -> None:
        project = _project(_item(content_node_id="I_kwSOMEONEELSE"))
        plan = normalize_intake(_issue(labels=("stale",)), project)
        self.assertEqual(plan.operations[0].kind, "project-membership")
        self.assertFalse(plan.is_member)
        self.assertEqual(plan.operations[0].desired["content_node_id"], ISSUE_NODE)
        self.assertIsNone(plan.operations[0].decision)

    def test_a_partial_project_snapshot_fails_closed(self) -> None:
        plan = normalize_intake(_issue(), _project(hit_cap=True))
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.blocked_reason, "project-membership-unknown")


# ───────────────────────────── compare before mutate ─────────────────────────────


class CompareBeforeMutateTests(unittest.TestCase):
    def test_every_operation_carries_expected_current_and_desired(self) -> None:
        project = _project(_item(content_node_id="I_kwSOMEONEELSE"))
        plan = normalize_intake(_issue(event="reopened"), project)
        self.assertTrue(plan.operations)
        for operation in plan.operations:
            with self.subTest(kind=operation.kind):
                self.assertIsNotNone(operation.expected)
                self.assertIsNotNone(operation.current)
                self.assertIsNotNone(operation.desired)
                body = operation.to_dict()
                self.assertIn("expected", body)
                self.assertIn("current", body)
                self.assertIn("desired", body)

    def test_a_newer_board_decision_quarantines_instead_of_being_overwritten(self) -> None:
        project = _project(_item(updated_at="2026-08-02T11:30:00Z", status="Building"))
        plan = normalize_intake(
            _issue(event="reopened", observed_project_updated_at="2026-08-02T09:00:00Z"),
            project,
        )
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.blocked_reason, "board-decision-newer")
        self.assertTrue(plan.quarantined)
        self.assertEqual(
            plan.quarantined[0].decision.reason_code, "record-changed-since-manifest"
        )

    def test_an_authorized_status_operation_carries_an_apply_decision(self) -> None:
        project = _project(_item(status="Done", option_name="Done"))
        plan = normalize_intake(_issue(event="reopened"), project)
        operation = _operation(plan, "status")
        self.assertEqual(operation.decision.action, "apply")
        self.assertEqual(operation.decision.desired_status, "Backlog")


# ───────────────────────────── labels ─────────────────────────────


class LabelTests(unittest.TestCase):
    def test_work_type_test_area_and_priority_become_labels(self) -> None:
        plan = normalize_intake(_issue(), _project())
        desired = set(_operation(plan, "labels").desired["labels"])
        self.assertIn("build", desired)
        self.assertIn("area:runtime", desired)
        self.assertIn("priority:P1", desired)

    def test_the_frankfurt_route_label_is_planned(self) -> None:
        plan = normalize_intake(
            _issue(
                body=_form_body(Branch_route="staging-frankfurt"),
                labels=("branch:staging-frankfurt",),
            ),
            _project(),
        )
        desired = set(_operation(plan, "labels").desired["labels"])
        self.assertIn("branch:staging-frankfurt", desired)

    def test_the_legacy_environment_alias_is_mapped_and_preserved(self) -> None:
        plan = normalize_intake(
            _issue(body=_form_body(Environment_constraint="laptop"), labels=("laptop",)),
            _project(),
        )
        desired = set(_operation(plan, "labels").desired["labels"])
        self.assertIn(ENVIRONMENT_CONSTRAINT_LABEL, desired)
        self.assertIn("laptop", desired)

    def test_existing_unrelated_labels_are_kept(self) -> None:
        plan = normalize_intake(_issue(labels=("history", "security")), _project())
        desired = set(_operation(plan, "labels").desired["labels"])
        self.assertIn("history", desired)
        self.assertIn("security", desired)


# ───────────────────────────── pull requests ─────────────────────────────


class PullRequestClassificationTests(unittest.TestCase):
    def test_the_classification_record_carries_every_required_field(self) -> None:
        record = classify_pull_request(_pull_request())
        body = record.to_dict()
        for name in REQUIRED_PULL_REQUEST_CLASSIFICATION_FIELDS:
            with self.subTest(field=name):
                self.assertIn(name, body)

    def test_the_required_field_set_is_the_documented_one(self) -> None:
        self.assertEqual(
            set(REQUIRED_PULL_REQUEST_CLASSIFICATION_FIELDS),
            {
                "base_branch",
                "head_branch",
                "head_sha",
                "draft",
                "reviews",
                "unresolved_discussions",
                "checks",
                "mergeable",
                "linked_issues",
                "closing_state",
                "merge_evidence",
                "supersession_evidence",
                "abandonment_evidence",
                "close_evidence",
            },
        )

    def test_the_classification_reflects_the_snapshot(self) -> None:
        record = classify_pull_request(_pull_request(draft=True, unresolved_discussions=2))
        self.assertEqual(record.base_branch, "staging")
        self.assertEqual(record.head_sha, "b" * 40)
        self.assertTrue(record.draft)
        self.assertEqual(record.unresolved_discussions, 2)
        self.assertEqual(record.linked_issues, (_issue().url,))

    def test_a_normalized_pull_request_carries_its_classification(self) -> None:
        plan = normalize_intake(_pull_request(), _project(_item(content_node_id=PR_NODE)))
        self.assertIsNotNone(plan.classification)
        self.assertEqual(plan.classification.head_branch, "feat/normalize-intake")


class PullRequestInheritanceTests(unittest.TestCase):
    def _project_for_pr(self, **item_overrides):
        return _project(_item(content_node_id=PR_NODE, **item_overrides))

    def test_one_unambiguous_linked_issue_is_inherited(self) -> None:
        plan = normalize_intake(_pull_request(), self._project_for_pr())
        self.assertEqual(plan.inherited_from, _issue().url)
        self.assertEqual(plan.branch_route, "staging")
        self.assertEqual(plan.milestone, "Phase 0 - Install")
        desired = set(_operation(plan, "labels").desired["labels"])
        self.assertIn("build", desired)
        self.assertIn("area:runtime", desired)
        self.assertIn("priority:P1", desired)

    def test_no_linked_issue_stays_in_a_holding_column(self) -> None:
        plan = normalize_intake(_pull_request(linked_issues=()), self._project_for_pr())
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.blocked_reason, "pull-request-linkage-missing")
        self.assertIn(plan.desired_status, (None, "Backlog", "Blocked"))
        self.assertNotEqual(plan.desired_status, "Ready")

    def test_two_linked_issues_are_ambiguous_and_inherit_nothing(self) -> None:
        second = _issue(number=102, url="https://github.com/Bavariance/polysimulator/issues/102")
        plan = normalize_intake(
            _pull_request(linked_issues=(_issue(), second)), self._project_for_pr()
        )
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.blocked_reason, "pull-request-linkage-ambiguous")
        self.assertIsNone(plan.inherited_from)

    def test_an_incomplete_linked_issue_is_not_inherited(self) -> None:
        broken = _issue(body="free-form prose")
        plan = normalize_intake(
            _pull_request(linked_issues=(broken,)), self._project_for_pr()
        )
        self.assertEqual(plan.operations, ())
        self.assertEqual(plan.blocked_reason, "pull-request-linked-intake-incomplete")

    def test_a_pull_request_is_never_promoted_to_ready(self) -> None:
        for event in TriggerSetTests.PULL_REQUEST_EVENTS:
            with self.subTest(event=event):
                plan = normalize_intake(
                    _pull_request(event=event), self._project_for_pr()
                )
                self.assertNotEqual(plan.desired_status, "Ready")


class PullRequestQaInvalidationTests(unittest.TestCase):
    def test_a_head_change_invalidates_stale_qa(self) -> None:
        pull = _pull_request(event="synchronize", head_sha="c" * 40, qa_tested_sha="b" * 40)
        plan = normalize_intake(pull, _project(_item(content_node_id=PR_NODE)))
        operation = _operation(plan, "qa-invalidation")
        self.assertEqual(operation.current.repository_head, "b" * 40)
        self.assertEqual(operation.desired["head_sha"], "c" * 40)
        self.assertTrue(operation.desired["invalidated"])

    def test_an_unchanged_head_leaves_qa_alone(self) -> None:
        pull = _pull_request(event="synchronize", head_sha="b" * 40, qa_tested_sha="b" * 40)
        plan = normalize_intake(pull, _project(_item(content_node_id=PR_NODE)))
        self.assertNotIn("qa-invalidation", _kinds(plan))

    def test_a_pull_request_without_qa_evidence_needs_no_invalidation(self) -> None:
        pull = _pull_request(event="synchronize", head_sha="c" * 40)
        plan = normalize_intake(pull, _project(_item(content_node_id=PR_NODE)))
        self.assertNotIn("qa-invalidation", _kinds(plan))


# ───────────────────────────── idempotency ─────────────────────────────


def _settled(subject: IssueSnapshot, project: ProjectSnapshot, plan):
    """Apply a plan to the fixtures so the second run sees a settled board."""
    item = dict(project.items[0])
    labels = subject.labels
    milestone = subject.milestone
    for operation in plan.operations:
        if operation.kind == "labels":
            labels = tuple(operation.desired["labels"])
        elif operation.kind == "status":
            item["status"] = operation.desired["status"]
            item["option_name"] = operation.desired["status"]
            item["project_values"] = {"Status": operation.desired["status"]}
        elif operation.kind == "repository-milestone":
            milestone = operation.desired["milestone"]
        elif operation.kind == "project-membership":
            item["content_node_id"] = operation.desired["content_node_id"]
    return (
        replace(subject, labels=labels, milestone=milestone, event="edited"),
        replace(project, items=(item,)),
    )


class IdempotencyTests(unittest.TestCase):
    def test_a_second_run_on_unchanged_input_plans_nothing(self) -> None:
        subject, project = _issue(), _project()
        first = normalize_intake(subject, project)
        self.assertTrue(first.operations)
        subject, project = _settled(subject, project, first)
        second = normalize_intake(subject, project)
        self.assertEqual(second.operations, ())
        self.assertIsNone(second.blocked_reason)

    def test_a_third_run_still_plans_nothing(self) -> None:
        subject, project = _issue(), _project()
        subject, project = _settled(subject, project, normalize_intake(subject, project))
        second = normalize_intake(subject, project)
        subject, project = _settled(subject, project, second)
        self.assertEqual(normalize_intake(subject, project).operations, ())

    def test_a_missing_card_settles_after_one_pass(self) -> None:
        subject = _issue(labels=("history",))
        project = _project(_item(content_node_id="I_kwSOMEONEELSE"))
        first = normalize_intake(subject, project)
        self.assertEqual(first.operations[0].kind, "project-membership")
        subject, project = _settled(subject, project, first)
        self.assertEqual(normalize_intake(subject, project).operations, ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
