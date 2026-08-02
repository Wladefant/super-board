"""Task 7 — QA evidence is bound to the exact pull request SHA.

Pure stdlib `unittest`. No network, no `gh`, no `git`: every GitHub read and
every git command is injected, and the tests assert on what was *asked for*,
never on credential material.

Run directly:
  python -B tests/test_exact_sha_qa.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_exact_sha_qa.py' -v
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

from super_board_runtime.config import load_and_validate_config  # noqa: E402
from super_board_runtime.publication import UnsafePublication  # noqa: E402
from super_board_runtime.qa import (  # noqa: E402
    QA_CHECK_CONTEXT,
    QA_FAILURE_KINDS,
    QaError,
    QaResult,
    disposition_qa_failure,
    file_qa_failure,
    inherited_check_state,
    locked_qa_worktree,
    publish_qa_status,
    record_qa_result,
    resolve_linked_pull_request,
    resolve_pull_request_head,
)

TESTED_SHA = "a" * 40
MOVED_SHA = "b" * 40
PR_URL = "https://github.com/Bavariance/polysimulator/pull/456"
ISSUE_URL = "https://github.com/Bavariance/polysimulator/issues/123"


def _pr_payload(**overrides: object) -> dict:
    payload = {
        "url": PR_URL,
        "id": "PR_kwNOTAREALNODEID",
        "headRefName": "feat/exact-sha",
        "headRefOid": TESTED_SHA,
        "baseRefName": "staging",
        "isDraft": False,
        "mergeable": "MERGEABLE",
    }
    payload.update(overrides)
    return payload


def _config():
    payload = {
        "version": 1,
        "project": {"owner": "Bavariance", "number": 1},
        "repo": {"remote": "Bavariance/polysimulator"},
        "base_branch": "staging",
        "activation_mode": "off",
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_and_validate_config(path)


def _result(**overrides: object) -> QaResult:
    fields = {
        "issue_url": ISSUE_URL,
        "issue_node_id": "I_kwNOTAREALNODEID",
        "pull_request_url": PR_URL,
        "pull_request_node_id": "PR_kwNOTAREALNODEID",
        "tested_sha": TESTED_SHA,
        "current_head_sha": TESTED_SHA,
        "selected_base_branch": "staging",
        "branch_declaration": "staging",
        "result": "success",
        "failure_kind": None,
        "started_at": "2026-08-02T10:00:00Z",
        "completed_at": "2026-08-02T10:12:00Z",
        "check_url": "https://github.com/Bavariance/polysimulator/commit/" + TESTED_SHA,
        "sanitized_evidence_url": ISSUE_URL + "#issuecomment-1",
    }
    fields.update(overrides)
    return QaResult(**fields)  # type: ignore[arg-type]


class RecordingGit:
    """Injected git. Records argv lists; never runs anything."""

    def __init__(self, timeline: list | None = None) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.timeline = timeline

    def __call__(self, argv):
        self.calls.append(tuple(argv))
        if self.timeline is not None:
            self.timeline.append(("git", tuple(argv)))


class RecordingWriter:
    """Injected GitHub write boundary. Counts calls; returns a fake URL."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, payload):
        self.calls.append(dict(payload))
        return {"url": "https://github.com/Bavariance/polysimulator/commit/x"}


# ───────────────────────────── head resolution ─────────────────────────────


class ResolveHeadTests(unittest.TestCase):
    def test_head_is_read_and_recorded_before_any_command_runs(self) -> None:
        timeline: list = []

        def fetch(url):
            timeline.append(("read-head", url))
            return _pr_payload()

        head = resolve_pull_request_head(PR_URL, fetch=fetch)
        git = RecordingGit(timeline)
        with locked_qa_worktree(
            root=self.tmp, item_key="123", tested_sha=head.head_sha, git=git
        ):
            timeline.append(("run-tests", head.head_sha))

        self.assertEqual(head.head_sha, TESTED_SHA)
        self.assertEqual(head.base_ref, "staging")
        self.assertEqual(head.pull_request_node_id, "PR_kwNOTAREALNODEID")
        self.assertFalse(head.is_draft)
        # The recorded head is the FIRST event; nothing ran before it.
        self.assertEqual(timeline[0], ("read-head", PR_URL))

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_head_refuses(self) -> None:
        with self.assertRaises(QaError) as ctx:
            resolve_pull_request_head(PR_URL, fetch=lambda url: _pr_payload(headRefOid=None))
        self.assertEqual(ctx.exception.reason, "qa-head-missing")

    def test_non_sha_head_refuses(self) -> None:
        with self.assertRaises(QaError) as ctx:
            resolve_pull_request_head(PR_URL, fetch=lambda url: _pr_payload(headRefOid="HEAD"))
        self.assertEqual(ctx.exception.reason, "qa-head-invalid")

    def test_unresolvable_pull_request_refuses(self) -> None:
        def boom(url):
            raise RuntimeError("network down")

        with self.assertRaises(QaError) as ctx:
            resolve_pull_request_head(PR_URL, fetch=boom)
        self.assertEqual(ctx.exception.reason, "qa-pull-request-unresolved")

        with self.assertRaises(QaError) as ctx:
            resolve_pull_request_head(PR_URL, fetch=lambda url: None)
        self.assertEqual(ctx.exception.reason, "qa-pull-request-unresolved")

    def test_stale_head_refuses_until_linkage_is_reconciled(self) -> None:
        head = resolve_pull_request_head(PR_URL, fetch=lambda url: _pr_payload())
        with self.assertRaises(QaError) as ctx:
            resolve_pull_request_head(
                PR_URL, fetch=lambda url: _pr_payload(headRefOid=MOVED_SHA), expected_sha=head.head_sha
            )
        self.assertEqual(ctx.exception.reason, "qa-head-changed")

    def test_linkage_missing_and_ambiguous_refuse(self) -> None:
        with self.assertRaises(QaError) as ctx:
            resolve_linked_pull_request(ISSUE_URL, [])
        self.assertEqual(ctx.exception.reason, "qa-linkage-missing")

        with self.assertRaises(QaError) as ctx:
            resolve_linked_pull_request(ISSUE_URL, [PR_URL, PR_URL + "7"])
        self.assertEqual(ctx.exception.reason, "qa-linkage-ambiguous")

        self.assertEqual(resolve_linked_pull_request(ISSUE_URL, [PR_URL]), PR_URL)


# ───────────────────────────── worktree + lock ─────────────────────────────


class LockedWorktreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _leftovers(self) -> list[str]:
        return sorted(p.name for p in self.tmp.rglob("*") if p.is_file())

    def test_worktree_is_detached_at_the_tested_sha(self) -> None:
        git = RecordingGit()
        with locked_qa_worktree(root=self.tmp, item_key="123", tested_sha=TESTED_SHA, git=git) as wt:
            self.assertTrue(wt.detached)
            self.assertEqual(wt.tested_sha, TESTED_SHA)
        joined = [" ".join(call) for call in git.calls]
        self.assertTrue(any(call.startswith("fetch") for call in joined), joined)
        add = [call for call in joined if call.startswith("worktree add")]
        self.assertEqual(len(add), 1, joined)
        self.assertIn("--detach", add[0])
        self.assertIn(TESTED_SHA, add[0])

    def test_mutable_branch_checkout_is_refused(self) -> None:
        git = RecordingGit()
        with self.assertRaises(QaError) as ctx:
            with locked_qa_worktree(
                root=self.tmp, item_key="123", tested_sha=TESTED_SHA, checkout="branch", git=git
            ):
                pass
        self.assertEqual(ctx.exception.reason, "qa-mutable-checkout-refused")
        self.assertEqual(git.calls, [])
        self.assertEqual(self._leftovers(), [])

    def test_lock_is_exclusive_per_item(self) -> None:
        git = RecordingGit()
        with locked_qa_worktree(root=self.tmp, item_key="123", tested_sha=TESTED_SHA, git=git):
            with self.assertRaises(QaError) as ctx:
                with locked_qa_worktree(
                    root=self.tmp, item_key="123", tested_sha=TESTED_SHA, git=RecordingGit()
                ):
                    pass
            self.assertEqual(ctx.exception.reason, "qa-worktree-locked")
        self.assertEqual(self._leftovers(), [])

    def test_released_on_success_failure_interruption_and_stale_head(self) -> None:
        for label, error in (
            ("success", None),
            ("failure", RuntimeError("tests failed")),
            ("interruption", KeyboardInterrupt()),
            ("stale-head", QaError("qa-head-changed", "head moved mid-run")),
        ):
            with self.subTest(path=label):
                git = RecordingGit()
                try:
                    with locked_qa_worktree(
                        root=self.tmp, item_key="123", tested_sha=TESTED_SHA, git=git
                    ):
                        if error is not None:
                            raise error
                except BaseException:  # noqa: BLE001 - the point is that cleanup happened
                    pass
                joined = [" ".join(call) for call in git.calls]
                self.assertTrue(
                    any(call.startswith("worktree remove") for call in joined),
                    f"{label}: worktree was not removed — {joined}",
                )
                self.assertEqual(self._leftovers(), [], f"{label}: lock left behind")


# ───────────────────────────── ledger + publication ─────────────────────────────


class LedgerTests(unittest.TestCase):
    def test_successful_entry_matches_the_contract(self) -> None:
        entry = record_qa_result(_result())
        body = entry.to_dict()
        self.assertEqual(body["schema_version"], 1)
        self.assertEqual(body["tested_sha"], TESTED_SHA)
        self.assertEqual(body["current_head_sha"], TESTED_SHA)
        self.assertEqual(body["check_context"], QA_CHECK_CONTEXT)
        self.assertEqual(body["result"], "success")
        self.assertFalse(body["invalidated"])
        self.assertEqual(body["selected_base_branch"], "staging")
        # Deterministic, key-sorted JSON.
        self.assertEqual(
            json.dumps(body, sort_keys=True), json.dumps(json.loads(entry.to_json()), sort_keys=True)
        )

    def test_sha_change_mid_run_discards_the_result(self) -> None:
        entry = record_qa_result(_result(current_head_sha=MOVED_SHA))
        self.assertEqual(entry.result, "discarded")
        self.assertTrue(entry.invalidated)

    def test_success_publishes_only_for_an_unchanged_sha(self) -> None:
        writer = RecordingWriter()
        entry = record_qa_result(_result())
        published = publish_qa_status(entry, writer=writer)
        self.assertTrue(published["published"])
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(writer.calls[0]["context"], QA_CHECK_CONTEXT)
        self.assertEqual(writer.calls[0]["sha"], TESTED_SHA)
        self.assertEqual(writer.calls[0]["state"], "success")

    def test_discarded_entry_never_publishes(self) -> None:
        writer = RecordingWriter()
        entry = record_qa_result(_result(current_head_sha=MOVED_SHA))
        with self.assertRaises(QaError) as ctx:
            publish_qa_status(entry, writer=writer)
        self.assertEqual(ctx.exception.reason, "qa-result-discarded")
        self.assertEqual(writer.calls, [])

    def test_dry_run_issues_zero_github_writes(self) -> None:
        writer = RecordingWriter()
        published = publish_qa_status(record_qa_result(_result()), writer=writer, dry_run=True)
        self.assertFalse(published["published"])
        self.assertTrue(published["dry_run"])
        self.assertEqual(writer.calls, [])

    def test_a_credential_bearing_target_url_never_reaches_github(self) -> None:
        """The commit status is a GitHub write like any other.

        `target_url` is copied from the evidence URL; an evidence URL that
        carries `user:password@` would be written to GitHub verbatim unless the
        complete status payload goes through the one sanitizer first.
        """
        writer = RecordingWriter()
        password = "N" * 24
        entry = record_qa_result(
            _result(
                sanitized_evidence_url=(
                    "https://svc:" + password + "@evidence.internal.example/qa/1"
                )
            )
        )
        published = publish_qa_status(entry, writer=writer)
        self.assertTrue(published["published"])
        self.assertEqual(len(writer.calls), 1)
        written = json.dumps(writer.calls[0], sort_keys=True)
        self.assertNotIn(password, written)
        self.assertIn("credentialed-url", written)
        # The SHA binding survives sanitization — that is the whole evidence.
        self.assertEqual(writer.calls[0]["sha"], TESTED_SHA)
        self.assertEqual(writer.calls[0]["context"], QA_CHECK_CONTEXT)

    def test_a_status_payload_that_fails_the_gate_writes_nothing(self) -> None:
        """Fail closed, with no partial write — a status is a write like any other."""
        writer = RecordingWriter()
        short = "N" * 6  # too short to substring-redact safely; detected, never published
        entry = record_qa_result(
            _result(sanitized_evidence_url="https://evidence.example/qa/" + short)
        )
        with self.assertRaises(UnsafePublication):
            publish_qa_status(entry, writer=writer, environment={"DEPLOY_SECRET": short})
        self.assertEqual(writer.calls, [])

    def test_a_later_head_inherits_no_passing_result(self) -> None:
        entry = record_qa_result(_result())
        self.assertEqual(inherited_check_state(entry, TESTED_SHA), "success")
        self.assertIsNone(inherited_check_state(entry, MOVED_SHA))


# ───────────────────────────── failure disposition ─────────────────────────────


class FailureDispositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _config()

    def test_repairable_failure_returns_to_building(self) -> None:
        d = disposition_qa_failure(_result(result="failure", failure_kind="repairable"), self.config)
        self.assertEqual(d.next_status, "Building")
        self.assertFalse(d.follow_up_issue_required)

    def test_external_input_blocks_without_a_follow_up(self) -> None:
        d = disposition_qa_failure(
            _result(result="failure", failure_kind="external-input"), self.config
        )
        self.assertEqual(d.next_status, "Blocked")
        self.assertFalse(d.follow_up_issue_required)

    def test_failure_outside_acceptance_criteria_files_exactly_one_follow_up(self) -> None:
        result = _result(result="failure", failure_kind="outside-acceptance")
        d = disposition_qa_failure(result, self.config)
        self.assertEqual(d.next_status, "Blocked")
        self.assertTrue(d.follow_up_issue_required)

        writer = RecordingWriter()
        filed = file_qa_failure(result, self.config, issue_writer=writer)
        self.assertEqual(len(writer.calls), 1)
        self.assertIn("title", writer.calls[0])
        self.assertIn("body", writer.calls[0])
        self.assertEqual(filed.next_status, "Blocked")

    def test_the_follow_up_issue_is_sanitized_before_it_is_filed(self) -> None:
        """`file_qa_failure` writes to GitHub, so it is a publication.

        The follow-up carries a pull request URL and a base branch straight from
        the run. Handing that to `issue_writer` directly is a GitHub write that
        no sanitizer ever saw.
        """
        writer = RecordingWriter()
        token = "gh" + "p_" + ("N" * 36)
        result = _result(
            result="failure",
            failure_kind="outside-acceptance",
            pull_request_url=PR_URL + "?token=" + token,
        )
        file_qa_failure(result, self.config, issue_writer=writer)
        self.assertEqual(len(writer.calls), 1)
        written = json.dumps(writer.calls[0], sort_keys=True)
        self.assertNotIn(token, written)
        self.assertIn(TESTED_SHA, written)

    def test_no_follow_up_is_filed_for_the_other_two_dispositions(self) -> None:
        for kind in ("repairable", "external-input"):
            with self.subTest(kind=kind):
                writer = RecordingWriter()
                file_qa_failure(
                    _result(result="failure", failure_kind=kind), self.config, issue_writer=writer
                )
                self.assertEqual(writer.calls, [])

    def test_unknown_failure_kind_fails_closed_to_blocked(self) -> None:
        d = disposition_qa_failure(_result(result="failure", failure_kind=None), self.config)
        self.assertEqual(d.next_status, "Blocked")
        self.assertFalse(d.follow_up_issue_required)
        self.assertEqual(d.reason_code, "qa-failure-kind-unknown")

    def test_dry_run_files_nothing(self) -> None:
        writer = RecordingWriter()
        file_qa_failure(
            _result(result="failure", failure_kind="outside-acceptance"),
            self.config,
            issue_writer=writer,
            dry_run=True,
        )
        self.assertEqual(writer.calls, [])

    def test_failure_never_merges_and_never_moves_to_done(self) -> None:
        for kind in QA_FAILURE_KINDS + (None,):
            with self.subTest(kind=kind):
                d = disposition_qa_failure(_result(result="failure", failure_kind=kind), self.config)
                self.assertIn(d.next_status, ("Building", "Blocked"))
                self.assertNotEqual(d.next_status, "Done")

    def test_a_successful_result_has_no_failure_disposition(self) -> None:
        with self.assertRaises(QaError) as ctx:
            disposition_qa_failure(_result(), self.config)
        self.assertEqual(ctx.exception.reason, "qa-result-not-a-failure")


if __name__ == "__main__":
    unittest.main(verbosity=2)
