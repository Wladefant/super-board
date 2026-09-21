"""A board whose built-in workflow closes its own cards is refused.

Pure stdlib `unittest`. No network, no `gh`. The board read is a fixture
recorded from the live Shipnovo board with
`super-board-project.py query --workflows`.

WHY: the Shipnovo Superboard ran with the built-in **Auto-close issue** workflow
enabled, so every card dragged into `Building` closed its issue — five issues
were closed while their work was still in flight
(https://github.com/Wladefant/super-board/issues/48). GitHub's GraphQL API
exposes no mutation for the setting (`enabled` is read-only on
`ProjectV2Workflow`, and `deleteProjectV2Workflow` removes a workflow with
nothing to recreate it), so the repair is an operator's click in the Projects UI
and the only thing code can do is refuse to run against such a board.

The class this closes is wider than that one workflow: any built-in Project
workflow that destroys work without a human decision, including one GitHub
renames or ships new. So the guard carries a deny list AND a closed allow list,
and an enabled workflow on neither is a finding.

What it does NOT catch: GitHub omits built-in workflows that were never saved,
so a board that simply does not list "Auto-close issue" reads clean here. That
is unconfigured, not proven off — `test_a_board_that_omits_the_workflow_is_not
_proof_of_disabled` pins the limitation, and the read-back in the setup runbook
remains the only proof of a deliberate off. The guard also cannot see a
workflow's *trigger*: the API returns no trigger or action detail, so
"Auto-close issue" is refused whatever status it is wired to.

Run directly:
  python -B tests/test_autoclose_workflow_guard.py
Or through pytest:
  python -m pytest tests/test_autoclose_workflow_guard.py -v
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from io import StringIO
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_PROJECT_CLI = _SCRIPTS / "super-board-project.py"
_project_spec = importlib.util.spec_from_file_location("super_board_project_cli", _PROJECT_CLI)
assert _project_spec is not None and _project_spec.loader is not None
project_cli = importlib.util.module_from_spec(_project_spec)
_project_spec.loader.exec_module(project_cli)

from super_board_runtime import EXIT_CONFIG, EXIT_OK  # noqa: E402
from super_board_runtime.project import (  # noqa: E402
    BENIGN_PROJECT_WORKFLOWS,
    DESTRUCTIVE_PROJECT_WORKFLOWS,
    PROJECT_WORKFLOWS_QUERY,
    MutationConflict,
    audit_project_workflows,
    project_workflows_from_graphql,
    workflow_key,
)

FIXTURE = _REPO_ROOT / "tests" / "fixtures" / "project-workflows-shipnovo.json"

OWNER = "Wladefant"
NUMBER = 11


def _recorded() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _nodes(payload: dict) -> list[dict]:
    return payload["data"]["repositoryOwner"]["projectV2"]["workflows"]["nodes"]


def _with(name: str, **overrides: object) -> dict:
    """The recorded board with the named workflow's fields overridden.

    `name_after` renames it, which is how a rename of a destructive built-in is
    exercised.
    """
    payload = copy.deepcopy(_recorded())
    for node in _nodes(payload):
        if node["name"] != name:
            continue
        for key, value in overrides.items():
            node["name" if key == "name_after" else key] = value
    return payload


def _audit(payload: dict):
    return audit_project_workflows(payload, project_owner=OWNER, project_number=NUMBER)


def _reported(audit) -> set[tuple[int | None, str, str]]:
    return {(f.workflow_number, f.workflow_name, f.reason_code) for f in audit.findings}


class RecordedBoardTests(unittest.TestCase):
    def test_the_recorded_board_is_refused_for_its_auto_close_workflow(self) -> None:
        audit = _audit(_recorded())
        self.assertFalse(audit.ok)
        self.assertEqual(
            _reported(audit),
            {(3, "Auto-close issue", "destructive-workflow-enabled")},
            "the recorded board's only finding is the workflow that closed the five issues",
        )
        finding = audit.findings[0]
        self.assertEqual(finding.workflow_node_id, "PWF_lAHOBL7E1c4BeofvzgZukU8")
        self.assertIn("closes the issue", finding.effect)

    def test_the_audit_carries_the_whole_board_it_judged(self) -> None:
        audit = _audit(_recorded())
        self.assertEqual(audit.project_owner, OWNER)
        self.assertEqual(audit.project_number, NUMBER)
        self.assertEqual(audit.project_title, "Shipnovo")
        self.assertEqual(
            {workflow["name"] for workflow in audit.workflows},
            {node["name"] for node in _nodes(_recorded())},
        )
        self.assertIs(audit.to_dict()["ok"], False)

    def test_turning_the_workflow_off_clears_the_board(self) -> None:
        audit = _audit(_with("Auto-close issue", enabled=False))
        self.assertTrue(audit.ok, f"unexpected findings: {_reported(audit)}")
        self.assertEqual(audit.findings, ())
        self.assertIs(audit.to_dict()["ok"], True)


class DestructiveClassTests(unittest.TestCase):
    def test_every_enabled_destructive_workflow_is_named_not_just_the_first(self) -> None:
        payload = copy.deepcopy(_recorded())
        _nodes(payload).append(
            {"enabled": True, "id": "PWF_ARCHIVE", "name": "Auto-archive items", "number": 7}
        )
        self.assertEqual(
            _reported(_audit(payload)),
            {
                (3, "Auto-close issue", "destructive-workflow-enabled"),
                (7, "Auto-archive items", "destructive-workflow-enabled"),
            },
        )

    def test_a_renamed_destructive_workflow_still_trips_the_guard(self) -> None:
        # A deny list by name goes blind the day GitHub renames the workflow, so
        # an enabled built-in on neither list is a finding of its own.
        payload = _with("Auto-close issue", name_after="Close issue automatically")
        self.assertEqual(
            _reported(_audit(payload)),
            {(3, "Close issue automatically", "unreviewed-workflow-enabled")},
        )

    def test_an_unreviewed_workflow_that_is_off_is_not_a_finding(self) -> None:
        payload = copy.deepcopy(_recorded())
        _nodes(payload).append(
            {"enabled": False, "id": "PWF_NEW", "name": "Some new GitHub built-in", "number": 8}
        )
        self.assertEqual(
            _reported(_audit(payload)),
            {(3, "Auto-close issue", "destructive-workflow-enabled")},
        )

    def test_a_name_that_differs_only_in_case_or_spacing_is_the_same_workflow(self) -> None:
        payload = _with("Auto-close issue", name_after="  AUTO-CLOSE   ISSUE  ")
        self.assertEqual(
            {f.reason_code for f in _audit(payload).findings},
            {"destructive-workflow-enabled"},
        )

    def test_an_unreadable_enabled_flag_is_never_read_as_disabled(self) -> None:
        payload = _with("Item closed", enabled=None)
        self.assertEqual(
            _reported(_audit(payload)),
            {
                (3, "Auto-close issue", "destructive-workflow-enabled"),
                (1, "Item closed", "workflow-state-unreadable"),
            },
        )

    def test_a_board_that_omits_the_workflow_is_not_proof_of_disabled(self) -> None:
        # The documented gap: GitHub omits a built-in that was never saved, and
        # this guard reports what the API returns. Absent reads clean.
        payload = copy.deepcopy(_recorded())
        nodes = _nodes(payload)
        payload["data"]["repositoryOwner"]["projectV2"]["workflows"]["nodes"] = [
            node for node in nodes if node["name"] != "Auto-close issue"
        ]
        self.assertTrue(_audit(payload).ok)


class ReviewedListTests(unittest.TestCase):
    """A new built-in workflow must not arrive silently approved."""

    def test_the_destructive_list_is_pinned(self) -> None:
        self.assertEqual(
            set(DESTRUCTIVE_PROJECT_WORKFLOWS),
            {"auto-close issue", "auto-archive items"},
        )

    def test_the_benign_list_is_pinned(self) -> None:
        self.assertEqual(
            set(BENIGN_PROJECT_WORKFLOWS),
            {
                "auto-add sub-issues to project",
                "auto-add to project",
                "item added to project",
                "item closed",
                "item reopened",
                "pull request linked to issue",
                "pull request merged",
            },
        )

    def test_every_reviewed_name_is_stored_normalized(self) -> None:
        # A table entry that is not already normalized can never match a live
        # workflow name, which would be a silently empty deny list.
        for name in (*DESTRUCTIVE_PROJECT_WORKFLOWS, *BENIGN_PROJECT_WORKFLOWS):
            self.assertEqual(workflow_key(name), name)

    def test_the_two_lists_never_overlap(self) -> None:
        self.assertEqual(
            set(DESTRUCTIVE_PROJECT_WORKFLOWS) & set(BENIGN_PROJECT_WORKFLOWS), set()
        )


class UnreadableBoardTests(unittest.TestCase):
    """An unreadable board is never a clean one."""

    def _reason(self, payload: object) -> str:
        with self.assertRaises(MutationConflict) as caught:
            project_workflows_from_graphql(payload)
        return caught.exception.reason

    def test_a_graphql_errors_array_refuses(self) -> None:
        payload = _recorded()
        payload["errors"] = [{"message": "Could not resolve to a User"}]
        self.assertEqual(self._reason(payload), "project-workflows-unreadable")

    def test_an_unresolved_owner_refuses(self) -> None:
        self.assertEqual(
            self._reason({"data": {"repositoryOwner": None}}), "project-owner-unresolved"
        )

    def test_an_absent_project_refuses(self) -> None:
        self.assertEqual(
            self._reason({"data": {"repositoryOwner": {"__typename": "User", "projectV2": None}}}),
            "project-not-found",
        )

    def test_a_missing_workflow_connection_refuses(self) -> None:
        payload = _recorded()
        payload["data"]["repositoryOwner"]["projectV2"].pop("workflows")
        self.assertEqual(self._reason(payload), "project-workflows-unreadable")

    def test_a_response_that_is_not_an_object_refuses(self) -> None:
        self.assertEqual(self._reason(["not", "a", "response"]), "project-workflows-unreadable")


class CommandLineTests(unittest.TestCase):
    """The CLI exits non-zero and names the operator's click path."""

    def _run(self, payload: dict, tmp: Path) -> tuple[int, str, str]:
        raw = tmp / "workflows.json"
        raw.write_text(json.dumps(payload), encoding="utf-8")
        out, err = StringIO(), StringIO()
        stdout, stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = project_cli.main(
                ["workflows", "--owner", OWNER, "--number", str(NUMBER), "--raw", str(raw)]
            )
        finally:
            sys.stdout, sys.stderr = stdout, stderr
        return code, out.getvalue(), err.getvalue()

    def test_the_recorded_board_exits_non_zero_with_the_click_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._run(_recorded(), Path(tmp))
        self.assertEqual(code, EXIT_CONFIG)
        self.assertEqual(out, "", "a refused board must leave stdout empty")
        self.assertIn("Auto-close issue", err)
        self.assertIn("Workflows", err)
        self.assertIn("no mutation for this setting", err)
        self.assertIs(json.loads(err.splitlines()[0])["ok"], False)

    def test_a_clean_board_exits_zero_with_the_audit_on_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._run(_with("Auto-close issue", enabled=False), Path(tmp))
        self.assertEqual(code, EXIT_OK, err)
        body = json.loads(out)
        self.assertIs(body["ok"], True)
        self.assertEqual(body["findings"], [])

    def test_an_unreadable_read_exits_non_zero_rather_than_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self._run({"data": {"repositoryOwner": None}}, Path(tmp))
        self.assertEqual(code, EXIT_CONFIG)
        self.assertEqual(out, "")
        self.assertIn("project-owner-unresolved", err)

    def test_the_workflow_query_has_one_authority(self) -> None:
        # Same reason `query` exists for the board read: no caller keeps a
        # second copy, and either owner type resolves without an owner-type
        # input.
        out = StringIO()
        stdout = sys.stdout
        sys.stdout = out
        try:
            code = project_cli.main(["query", "--workflows"])
        finally:
            sys.stdout = stdout
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(out.getvalue().strip(), PROJECT_WORKFLOWS_QUERY.strip())
        self.assertIn("... on Organization", out.getvalue())
        self.assertIn("workflows(first: 50)", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
