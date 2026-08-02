"""The normalization WORKFLOW must actually produce a plan.

`super-board-normalize.py` requires `--payload` — a complete `{subject,
project}` document — and fails closed with `normalize-project-snapshot-required`
without it. The shipped workflow invoked both intake and closure with no
payload at all and then only *logged* the non-zero status, so every delivered
event exited before producing anything while the job reported success. A
pipeline that cannot fail is a pipeline nobody notices has stopped.

These tests cover both halves: the payload assembly the workflow now calls, and
the workflow's own contract with it.

Run directly:
  python -B tests/test_normalize_workflow.py
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_CLI = _SCRIPTS / "super-board-normalize.py"
_spec = importlib.util.spec_from_file_location("super_board_normalize_cli", _CLI)
assert _spec is not None and _spec.loader is not None
normalize_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(normalize_cli)

WORKFLOW = _REPO_ROOT / "payload" / "github" / "workflows" / "super-board-normalize.yml"

ISSUE_EVENT = {
    "action": "labeled",
    "issue": {
        "number": 12,
        "html_url": "https://github.com/test-owner/test-repo/issues/12",
        "node_id": "I_kwNOTAREALNODEID",
        "state": "open",
        "title": "a card",
        "body": "## Acceptance Criteria\n- [ ] it works\n",
        "labels": [{"name": "bug"}, {"name": "env:staging"}],
        "milestone": {"title": "M1"},
    },
}

PR_EVENT = {
    "action": "closed",
    "pull_request": {
        "number": 34,
        "html_url": "https://github.com/test-owner/test-repo/pull/34",
        "node_id": "PR_kwNOTAREALNODEID",
        "state": "closed",
        "title": "implement the card",
        "body": "Closes #12",
        "labels": [],
        "milestone": None,
        "draft": False,
        "merged_at": "2026-08-02T10:00:00Z",
        "merge_commit_sha": "c" * 40,
        "mergeable_state": "clean",
        "base": {"ref": "staging"},
        "head": {"ref": "issue-12-a-card", "sha": "a" * 40},
    },
}

ONE_PAGE = [
    {
        "items": [{"item_node_id": "PVTI_1", "content_node_id": "I_kwNOTAREALNODEID"}],
        "fields": {"Status": {"id": "F_1"}},
        "pageInfo": {"hasNextPage": False, "endCursor": None},
    }
]

TRUNCATED_PAGES = [
    {
        "items": [{"item_node_id": "PVTI_1", "content_node_id": "I_1"}],
        "fields": {},
        "pageInfo": {"hasNextPage": True, "endCursor": "CUR1"},
    }
]


def _write(directory: Path, name: str, document: object) -> str:
    path = directory / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


class SubjectFromEventTests(unittest.TestCase):
    def test_an_issue_event_maps_onto_the_subject(self) -> None:
        subject = normalize_cli.subject_from_event(ISSUE_EVENT)
        self.assertEqual(subject["kind"], "issue")
        self.assertEqual(subject["event"], "labeled")
        self.assertEqual(subject["number"], 12)
        self.assertEqual(subject["url"], ISSUE_EVENT["issue"]["html_url"])
        self.assertEqual(subject["labels"], ["bug", "env:staging"])
        self.assertEqual(subject["milestone"], "M1")

    def test_a_pull_request_event_carries_its_branches_and_merge_evidence(self) -> None:
        subject = normalize_cli.subject_from_event(PR_EVENT)
        self.assertEqual(subject["kind"], "pull_request")
        self.assertEqual(subject["base_branch"], "staging")
        self.assertEqual(subject["head_sha"], "a" * 40)
        self.assertEqual(subject["merge_commit_sha"], "c" * 40)

    def test_an_event_with_neither_subject_is_refused(self) -> None:
        with self.assertRaises(normalize_cli.NormalizationError) as ctx:
            normalize_cli.subject_from_event({"action": "opened"})
        self.assertEqual(ctx.exception.reason, "normalize-event-subject-missing")


class PayloadAssemblyTests(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = normalize_cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_the_assembled_payload_is_accepted_by_intake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            code, out, err = self._run(
                [
                    "payload",
                    "--event-payload",
                    _write(directory, "event.json", ISSUE_EVENT),
                    "--owner",
                    "test-owner",
                    "--number",
                    "99",
                    "--project-pages",
                    _write(directory, "pages.json", ONE_PAGE),
                ]
            )
            self.assertEqual(code, 0, err)
            document = json.loads(out)
            self.assertIn("subject", document)
            self.assertIn("project", document)

            payload_path = _write(directory, "payload.json", document)
            code, plan_out, plan_err = self._run(
                [
                    "intake",
                    "--issue",
                    ISSUE_EVENT["issue"]["html_url"],
                    "--event",
                    "labeled",
                    "--payload",
                    payload_path,
                    "--json",
                ]
            )
        # The point of the test: a plan came out, and NOT the refusal the
        # workflow was producing on every event.
        self.assertNotIn("normalize-project-snapshot-required", plan_err)
        self.assertIn(code, (0, 3), plan_err)
        if code == 0:
            self.assertTrue(json.loads(plan_out)["ok"])

    def test_a_truncated_project_inventory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            code, _, err = self._run(
                [
                    "payload",
                    "--event-payload",
                    _write(directory, "event.json", ISSUE_EVENT),
                    "--owner",
                    "test-owner",
                    "--number",
                    "99",
                    "--project-pages",
                    _write(directory, "pages.json", TRUNCATED_PAGES),
                ]
            )
        self.assertEqual(code, 65)
        self.assertIn("project-snapshot-incomplete", err)

    def test_intake_without_a_payload_still_refuses(self) -> None:
        # The guarantee the workflow was quietly relying on, kept explicit.
        code, _, err = self._run(
            ["intake", "--issue", ISSUE_EVENT["issue"]["html_url"], "--json"]
        )
        self.assertEqual(code, 65)
        self.assertIn("normalize-project-snapshot-required", err)


class WorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = WORKFLOW.read_text(encoding="utf-8")

    def test_both_normalization_steps_pass_a_payload(self) -> None:
        for command in ("normalize.py intake", "normalize.py closure"):
            with self.subTest(command=command):
                tail = self.source.split(command, 1)
                self.assertEqual(len(tail), 2, f"{command} is no longer invoked")
                invocation = tail[1].split("\n\n", 1)[0]
                self.assertIn(
                    "--payload",
                    invocation,
                    f"{command} runs without --payload and can only ever refuse",
                )

    def test_the_workflow_builds_the_payload_it_passes(self) -> None:
        self.assertTrue(
            "normalize.py payload" in self.source,
            "the workflow never builds the payload it must pass",
        )
        self.assertTrue(
            "--project-pages" in self.source,
            "the payload step reads no Project inventory",
        )

    def test_a_failed_normalization_fails_the_job(self) -> None:
        # `|| status=$?` followed by a bare echo is how the job stayed green
        # while producing nothing.
        self.assertFalse(
            "|| status=$?" in self.source,
            "a swallowed exit status keeps the job green while it produces nothing",
        )
        for step in self.source.split("run: |")[1:]:
            with self.subTest(step=step.strip().splitlines()[0] if step.strip() else ""):
                self.assertIn(
                    "set -euo pipefail",
                    step.split("\n      - name:")[0],
                    "a run step that does not stop on error cannot report a refusal",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
