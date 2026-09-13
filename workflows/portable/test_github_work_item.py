"""Observable GitHub-authority, publication and installed-review contracts."""
import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from github_work_item import HEADINGS, PROJECT_URL, execution_view, fetch_work_item, publish_report
from ledger import RequestLedger
from coordinator import Coordinator
from superboard_adapter import SuperboardExecutionAdapter, WorkerExecutionResult


def api_issue():
    body = "\n\n".join(f"### {heading}\nConcrete instruction for {heading}" for heading in HEADINGS)
    def connection(nodes):
        return {"nodes": nodes, "pageInfo": {"hasNextPage": False}}
    return {"id": "ISSUE", "url": "https://github.com/Wladefant/super-board/issues/114",
            "title": "Live title", "body": body, "state": "OPEN", "updatedAt": "2026-09-13T00:00:00Z",
            "milestone": {"title": "Integration", "number": 4, "state": "OPEN", "url": "https://github.com/Wladefant/super-board/milestone/4"},
            "parent": {"id": "PARENT", "url": "https://github.com/Wladefant/super-board/issues/113"},
            "labels": connection([{"name": label} for label in ("kind:task", "area:workflow", "risk:low")]),
            "assignees": connection([{"login": "Wladefant"}]), "blockedBy": connection([]),
            "projectItems": connection([{"id": "CARD", "project": {"id": "BOARD", "url": PROJECT_URL, "number": 5}, "fieldValueByName": {"name": "Building"}}])}


class GitHubAuthority(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = RequestLedger(state_dir=self.temp.name)
        self.record = self.ledger.add_request(
            req_id="work", prompt="Stale cache prompt", session="test", project="workflow",
            acceptance_criteria=[{"id": "AC-1", "description": "Implement intake"}],
            owner="OldOwner", state="implementation", task_type="local_doc",
            github_repo="Wladefant/super-board", issue_number=114,
        )
        self.issue = api_issue()

    def read_live(self, record=None):
        return fetch_work_item(record or self.record, lambda *_: {"data": {"repository": {"issue": copy.deepcopy(self.issue)}}})

    def test_live_api_overrides_cache_and_keeps_checkpoint(self):
        view = self.ledger.refresh_from_github("work", self.read_live)
        self.assertIn("Scope & Original Request", view["prompt"])
        self.assertEqual(view["owner"], "Wladefant")
        self.assertEqual(view["milestone"]["number"], 4)
        self.assertTrue(view["parent"]["url"].endswith("/113"))
        self.assertEqual(view["superboard"]["status"], "Building")
        self.assertEqual(view["state"], "implementation")
        cached = self.ledger.get_request("work")
        self.assertEqual(cached["github_cache"]["role"], "api_read_cache")
        self.issue["assignees"]["nodes"] = [{"login": "NewOwner"}]
        self.assertEqual(self.ledger.refresh_from_github("work", self.read_live)["owner"], "NewOwner")

    def test_api_outage_never_falls_back_to_previous_snapshot(self):
        self.ledger.refresh_from_github("work", self.read_live)
        coordinator = Coordinator(state_dir=self.temp.name, repo="Wladefant/super-board", sync_decisions=False)
        with patch("github_work_item.fetch_work_item", side_effect=RuntimeError("API unavailable")):
            packet = coordinator.evaluate_step("work")
        self.assertEqual(packet.status, "blocked")
        self.assertIn("API unavailable", packet.status_reason)
        self.assertIsNone(packet.request)

    def test_completed_cache_requires_remote_closure_and_reopened_work_is_retained(self):
        coordinator = Coordinator(state_dir=self.temp.name, sync_decisions=False)
        completed = dict(self.record, state="done")
        self.issue["state"] = "CLOSED"
        self.issue["milestone"]["state"] = "CLOSED"
        self.issue["projectItems"]["nodes"][0]["fieldValueByName"]["name"] = "Done"
        with patch.object(coordinator.ledger, "list_requests", return_value=[completed]), patch.object(
            coordinator.ledger, "refresh_from_github",
            side_effect=lambda _: execution_view(completed, self.read_live()),
        ):
            selected, reason = coordinator.select_target_request()
            self.assertIsNone(selected)
            self.assertIn("GitHub confirms closure", reason)
            self.issue["state"] = "OPEN"
            self.issue["milestone"]["state"] = "OPEN"
            selected, reason = coordinator.select_target_request()
            self.assertIsNone(selected)
            self.assertTrue(reason.startswith("GitHub intake blocked:"))

    def test_native_blocker_and_closed_issue_prevent_dispatch(self):
        for field, value in [("blockedBy", {"nodes": [{"url": "https://github.com/Wladefant/super-board/issues/1", "state": "OPEN"}], "pageInfo": {"hasNextPage": False}}), ("state", "CLOSED")]:
            with self.subTest(field=field):
                self.issue = api_issue()
                self.issue[field] = value
                self.assertTrue(execution_view(self.record, self.read_live())["github_blocker"])

    def test_board_movement_is_not_overwritten_by_cache(self):
        self.issue["projectItems"]["nodes"][0]["fieldValueByName"]["name"] = "Blocked"
        self.assertIn("Blocked", self.ledger.refresh_from_github("work", self.read_live)["github_blocker"])
        self.issue["projectItems"]["nodes"][0]["fieldValueByName"]["name"] = "Review"
        self.assertIn("disagrees", self.ledger.refresh_from_github("work", self.read_live)["github_blocker"])

    def test_missing_structure_is_rejected(self):
        for key, value in [("milestone", None), ("parent", None), ("body", "## Scope\nNot enough"), ("assignees", {"nodes": [], "pageInfo": {"hasNextPage": False}}), ("labels", {"nodes": [], "pageInfo": {"hasNextPage": False}})]:
            with self.subTest(key=key):
                self.issue = api_issue()
                self.issue[key] = value
                with self.assertRaises(ValueError): self.read_live()

    def test_standalone_parent_exception_is_explicit(self):
        self.issue["parent"] = None
        self.issue["body"] = self.issue["body"].replace("Concrete instruction for Dependencies & Parent Issue", "Standalone: one independent fix, no dependencies")
        self.assertIsNone(self.read_live()["parent"])

    def test_partial_connections_and_graphql_errors_fail_closed(self):
        for field in ("labels", "assignees", "blockedBy", "projectItems"):
            self.issue = api_issue()
            self.issue[field]["pageInfo"]["hasNextPage"] = True
            with self.assertRaises(ValueError): self.read_live()
        with self.assertRaises(ValueError):
            fetch_work_item(self.record, lambda *_: {"data": {"repository": {"issue": api_issue()}}, "errors": [{"message": "denied"}]})

    def test_same_project_number_other_owner_is_not_authority(self):
        self.issue["projectItems"]["nodes"][0]["project"]["url"] = "https://github.com/users/Other/projects/5"
        with self.assertRaises(ValueError): self.read_live()

    def test_local_evidence_rejected_before_cache_write(self):
        for evidence in ("local://private.md", "https://raw.githubusercontent.com/o/r/x/log"):
            with self.assertRaises(ValueError):
                self.ledger.update_request("work", add_evidence={"details": evidence})
        self.assertEqual(self.ledger.get_request("work")["evidence"], [])

    def test_publication_readback_and_mismatch(self):
        url = self.record["github"]["issue_url"]
        calls = []
        def runner(query, variables):
            calls.append(variables)
            if "repository(" in query: return {"data": {"repository": {"issue": {"id": "ISSUE", "url": url}}}}
            if "addComment" in query: return {"data": {"addComment": {"commentEdge": {"node": {"id": "COMMENT", "url": url + "#issuecomment-1"}}}}}
            return {"data": {"node": {"body": "## Proof\nPassed", "url": url + "#issuecomment-1"}}}
        self.assertTrue(publish_report(url, "## Proof\nPassed", runner).endswith("#issuecomment-1"))
        self.assertEqual(len(calls), 3)
        with self.assertRaisesRegex(ValueError, "readback"):
            publish_report(url, "Different text", runner)
        with self.assertRaises(ValueError): publish_report(url, "local://secret.md", runner)

    def test_missing_publication_values_are_actionable_without_network(self):
        def no_network(*_):
            self.fail("Invalid publication values must never call GitHub")
        for url in (None, "", 42, {}):
            with self.assertRaisesRegex(ValueError, "canonical GitHub issue URL"):
                publish_report(url, "Evidence", no_network)
        with self.assertRaisesRegex(ValueError, "markdown text"):
            publish_report(self.record["github"]["issue_url"], None, no_network)

    def test_publication_failure_preserves_primary_worker_diagnosis(self):
        adapter = SuperboardExecutionAdapter(state_dir=self.temp.name, notify_telegram=False)
        for result, primary in (
            (WorkerExecutionResult(stage="build", exit_code=0, output="checks failed",
                                   head_sha="a" * 40, blocked_reason="unclassified failing check"),
             "Stage 'build' blocked: unclassified failing check"),
            (WorkerExecutionResult(stage="build", exit_code=2, output="compiler error",
                                   head_sha="a" * 40),
             "Worker build execution failed with exit code 2"),
        ):
            with patch("github_work_item.publish_report", side_effect=RuntimeError("write denied")):
                state, reason, gate = adapter.verify_and_advance_request(self.record, "build", result)
            self.assertEqual(state, "implementation")
            self.assertTrue(reason.startswith(primary), reason)
            self.assertIn("write denied", reason)
            self.assertIn("write denied", gate["publication_error"])
            self.assertFalse(gate["verified"])

    def test_publication_failure_cannot_advance_successful_worker(self):
        adapter = SuperboardExecutionAdapter(state_dir=self.temp.name, notify_telegram=False)
        result = WorkerExecutionResult(stage="build", exit_code=0, output="Passed", head_sha="a" * 40)
        with patch("github_work_item.publish_report", side_effect=RuntimeError("write denied")):
            state, reason, gate = adapter.verify_and_advance_request(self.record, "build", result)
        self.assertEqual(state, "implementation")
        self.assertFalse(gate["verified"])
        self.assertIn("write denied", reason)
        self.assertEqual(self.ledger.get_request("work")["state"], "implementation")

    def test_dry_run_does_not_cache_proposed_project_state(self):
        from project_adapter import SuperboardLifecycleOutcome
        self.ledger.update_request("work", superboard_update={"item_id": "CARD", "status": "Building"})
        adapter = SuperboardExecutionAdapter(state_dir=self.temp.name, dry_run=True)
        with patch.object(adapter.project_config, "update_lifecycle", return_value=SuperboardLifecycleOutcome(ok=True, new_status="QA", item_id="CARD", dry_run=True)):
            adapter._sync_project_lifecycle("work", "QA")
        self.assertEqual(self.ledger.get_request("work")["superboard"]["status"], "Building")


class PreservedReviewBehavior(unittest.TestCase):
    """Run the preserved installed gate against actual git content, not hash mocks."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git("init", "-b", "staging")
        self.git("config", "user.name", "Wladimir Kirjanovs")
        self.git("config", "user.email", "wladefant@gmail.com")
        (self.repo / "code.py").write_text("value = 1\n")
        self.git("add", ".")
        self.git("commit", "-m", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("update-ref", "refs/remotes/origin/staging", self.base)
        self.git("checkout", "-b", "feature")
        self.head = self.change("value = 2\n", "feature")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, stderr=subprocess.DEVNULL).decode().strip()

    def change(self, text, message):
        (self.repo / "code.py").write_text(text)
        self.git("add", ".")
        self.git("commit", "-m", message)
        return self.git("rev-parse", "HEAD")

    def review(self, sha, body="APPROVE", actor="reviewer", state="COMMENTED", ordinal=1):
        return {"id": ordinal, "user": {"login": actor}, "state": state, "body": body, "commit_id": sha}

    def gate(self, reviews, staging=True):
        import github_pr_gate
        with patch("os.getcwd", wraps=os.getcwd):
            previous = os.getcwd()
            try:
                os.chdir(self.repo)
                return github_pr_gate.evaluate_pr_gate({
                    "number": 1, "state": "OPEN", "headRefOid": self.git("rev-parse", "HEAD"),
                    "baseRefName": "staging", "baseRefOid": self.base,
                    "author": {"login": "author"}, "reviews": reviews, "statusCheckRollup": [],
                }, repo="Bavariance/polysimulator" if staging else "example/fixture")
            finally:
                os.chdir(previous)

    def test_staging_author_comment_waiver_not_formal_approval(self):
        result = self.gate([self.review(self.head, actor="author")])
        self.assertEqual(result.gate_verdict, "PASSED")
        self.assertEqual(result.approval_verdict, "AUTOMATED_REVIEW_APPROVED")
        self.assertNotEqual(self.gate([self.review(self.head, actor="author")], False).gate_verdict, "PASSED")

    def test_sync_preserves_both_content_identities(self):
        from review_content import content_identity
        old = content_identity(self.head, cwd=self.repo)
        self.git("checkout", "staging")
        (self.repo / "unrelated.txt").write_text("base advanced\n")
        self.git("add", "."); self.git("commit", "-m", "base advance")
        self.git("update-ref", "refs/remotes/origin/staging", self.git("rev-parse", "HEAD"))
        self.git("checkout", "feature"); self.git("merge", "--no-ff", "staging", "-m", "sync")
        current = content_identity(self.git("rev-parse", "HEAD"), cwd=self.repo)
        self.assertEqual(old, current)
        self.assertEqual(self.gate([self.review(self.head)]).gate_verdict, "PASSED")

    def test_delta_requires_approved_ancestor(self):
        newer = self.change("value = 3\n", "delta")
        review = self.review(newer, "APPROVE\ndelta-from: " + self.head, ordinal=2)
        self.assertNotEqual(self.gate([review]).gate_verdict, "PASSED")
        self.assertEqual(self.gate([self.review(self.head), review]).gate_verdict, "PASSED")

    def test_whitespace_change_requires_delta_despite_patch_id(self):
        from review_content import content_identity
        before = content_identity(self.head, cwd=self.repo)
        newer = self.change("value  = 2\n", "whitespace")
        after = content_identity(newer, cwd=self.repo)
        self.assertEqual(before[0], after[0])
        self.assertNotEqual(before[1], after[1])
        self.assertNotEqual(self.gate([self.review(self.head)]).gate_verdict, "PASSED")

    def test_later_negative_verdict_overrides_approval(self):
        self.assertNotEqual(self.gate([self.review(self.head), self.review(self.head, "REQUEST-CHANGES", ordinal=2)]).gate_verdict, "PASSED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
