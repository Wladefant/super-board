#!/usr/bin/env python3
"""
test_continuation_driver.py - Contract tests for the continuation driver.

The driver's whole value is that it stops for the right reasons. Every test here
defends one of those reasons, because the failure modes it prevents are the ones
that look like success: a loop that spins on an unchanged request forever, a
restart that redoes a completed stage, a second driver racing the first, or a run
that keeps going past the human merge gate.

The driver is deliberately adapter-agnostic, so these tests drive it with a
scripted adapter whose run_step performs real ledger transitions. That is not a
stand-in for the real adapter: it is the documented duck-typed integration
surface, and the real adapter is exercised separately end to end.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import subprocess
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from continuation_driver import (  # noqa: E402
    JOURNAL_FILENAME,
    MIN_DECISION_SYNC_INTERVAL,
    BoundaryViolation,
    ContinuationDriver,
    DriverJournal,
    DriverLockError,
    DriverRunLock,
    build_parser,
)
from continuation_driver import ParkRecord, main as continuation_main  # noqa: E402
from worker_backend import WorkerBackend  # noqa: E402
from ledger import RequestLedger  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

SAFE_BOUNDARIES = {"auto_merge_allowed": False, "auto_deploy_allowed": False}


class FakeResult:
    """Shaped like AdapterExecutionResult, read only through its public names."""

    def __init__(self, status, reason="", head_sha=None, worker=None, boundaries=None):
        self.status = status
        self.status_reason = reason
        self.head_sha = head_sha
        self.worker_result = worker
        self.boundaries = dict(boundaries if boundaries is not None else SAFE_BOUNDARIES)


class FakeWorker:
    def __init__(self, ok=True, backend_name="scripted", artifacts=None):
        self.ok = ok
        self.backend_name = backend_name
        self.artifacts = list(artifacts or [])


class ScriptedAdapter:
    """
    Duck-typed adapter that performs real ledger transitions.

    `script` maps a ledger state to (next_state, status). A step with no entry
    for the current state returns 'advanced' while changing nothing, which is
    exactly the no-progress case the driver must catch.
    """

    def __init__(self, ledger, state_dir, script=None, decision_mgr=None,
                 raise_on=None, boundaries=None):
        self.ledger = ledger
        self.state_dir = state_dir
        # `script or {...}` would be wrong: an intentionally EMPTY script means
        # "never transition", and a falsy check would silently swap in the
        # default and hide the no-progress path this harness exists to exercise.
        self.script = script if script is not None else {
            "pending": ("QA", "advanced"),
            "implementation": ("QA", "advanced"),
            "QA": ("review", "advanced"),
            "review": ("awaiting authorization", "awaiting_authorization"),
        }
        self.calls = []
        self.raise_on = raise_on or set()
        self.boundaries = boundaries
        self.coordinator = type("C", (), {
            "decision_mgr": decision_mgr,
            "state_dir": state_dir,
            "ledger": ledger,
            "sync_decisions_if_configured": staticmethod(
                lambda: (True, True, "synced", 0)),
        })()

    def run_step(self, request_id=None, target_sha=None, real_worker=False):
        self.calls.append((request_id, real_worker))
        if request_id in self.raise_on:
            raise RuntimeError("scripted adapter failure")
        req = self.ledger.get_request(request_id)
        state = req.get("state")
        entry = self.script.get(state)
        if entry is None:
            return FakeResult("advanced", f"nothing scripted for state {state}",
                              boundaries=self.boundaries)
        next_state, status = entry
        if next_state in ("review", "awaiting authorization"):
            stage_name = "qa" if next_state == "review" else "review"
            current = self.ledger.get_request(request_id)
            for criterion in current.get("acceptance_criteria", []):
                self.ledger.update_request(
                    request_id,
                    criterion_update={
                        "id": criterion["id"],
                        "status": "verified",
                        "evidence": f"{stage_name} verified scripted driver contract",
                    },
                    actor="ScriptedAdapter",
                )
            self.ledger.update_request(
                request_id,
                add_evidence={
                    "type": f"{stage_name}_verification",
                    "summary": f"{stage_name} stage passed",
                    "details": f"scripted adapter verified {stage_name} on {current['head']}",
                    "head": current["head"],
                },
                actor="ScriptedAdapter",
            )
        self.ledger.update_request(
            request_id,
            state=next_state,
            actor="ScriptedAdapter",
            add_evidence={"summary": f"scripted {state} -> {next_state}"},
        )
        return FakeResult(status, f"{state} -> {next_state}",
                          worker=FakeWorker(artifacts=["out.txt"]),
                          boundaries=self.boundaries)


class _Fixture(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cdrv_test_")
        self.ledger = RequestLedger(ledger_path=os.path.join(self.tmp, "ledger.json"))
        # Correlation defaults to the installed ~/.veyyon/telegram/bot_pool.db when that
        # file exists, which is the live index the session bridge resolves replies
        # through. Any test here that reaches the sender must write into its own pool,
        # never that one.
        self._pool_db_env = patch.dict(
            os.environ, {"VEYYON_POOL_DB": os.path.join(self.tmp, "bot_pool.db")}
        )
        self._pool_db_env.start()

    def tearDown(self):
        self._pool_db_env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, req_id, state="implementation", head="a" * 40, task_type="local_doc"):
        return self.ledger.add_request(
            req_id=req_id,
            prompt=f"prompt for {req_id}",
            session="test",
            project="test-project",
            acceptance_criteria=[{"criterion": "works", "status": "pending", "evidence": ""}],
            owner="Tester",
            state=state,
            head=head,
            task_type=task_type,
        )

    def _driver(self, adapter, ids, **over):
        kwargs = dict(
            adapter=adapter,
            authorized_ids=ids,
            state_dir=self.tmp,
            ledger=self.ledger,
            install_signal_handlers=False,
        )
        kwargs.update(over)
        return ContinuationDriver(**kwargs)

    def _adapter(self, **over):
        return ScriptedAdapter(self.ledger, self.tmp, **over)


# ---------------------------------------------------------------------------
# Authorization scope
# ---------------------------------------------------------------------------

class TestAuthorizationScope(_Fixture):

    def test_empty_authorization_is_rejected(self):
        """The driver must never be handed an open mandate to find work."""
        with self.assertRaises(ValueError):
            self._driver(self._adapter(), [])
        with self.assertRaises(ValueError):
            self._driver(self._adapter(), ["  "])

    def test_only_authorized_ids_are_dispatched(self):
        self._add("req-authorized")
        self._add("req-not-authorized")
        adapter = self._adapter()
        outcome = self._driver(adapter, ["req-authorized"]).run()
        touched = {c[0] for c in adapter.calls}
        self.assertEqual(touched, {"req-authorized"})
        self.assertEqual(outcome.authorized_ids, ["req-authorized"])

    def test_unauthorized_request_is_untouched_in_the_ledger(self):
        self._add("req-a")
        self._add("req-b")
        self._driver(self._adapter(), ["req-a"]).run()
        self.assertEqual(self.ledger.get_request("req-b")["state"], "implementation")

    def test_duplicate_ids_collapse_but_order_is_kept(self):
        self._add("req-1")
        self._add("req-2")
        driver = self._driver(self._adapter(), ["req-2", "req-1", "req-2"])
        self.assertEqual(driver.authorized_ids, ["req-2", "req-1"])

    def test_missing_request_parks_rather_than_raising(self):
        outcome = self._driver(self._adapter(), ["req-ghost"]).run()
        self.assertEqual(outcome.steps_executed, 0)
        self.assertEqual(outcome.parked[0]["reason_code"], "missing")


# ---------------------------------------------------------------------------
# Continuation across stages
# ---------------------------------------------------------------------------

class TestStageProgression(_Fixture):

    def test_repeated_stages_progress_in_one_run(self):
        """
        The point of the driver: one invocation carries a request through every
        stage it can, rather than one step per human invocation.
        """
        self._add("req-flow", state="implementation")
        adapter = self._adapter()
        outcome = self._driver(adapter, ["req-flow"]).run()

        stages = [s["stage"] for s in outcome.steps]
        self.assertEqual(stages, ["build", "qa", "review"])
        transitions = [(s["entry_state"], s["exit_state"]) for s in outcome.steps]
        self.assertEqual(transitions, [
            ("implementation", "QA"),
            ("QA", "review"),
            ("review", "awaiting authorization"),
        ])
        self.assertTrue(all(s["progressed"] for s in outcome.steps))
        self.assertEqual(self.ledger.get_request("req-flow")["state"], "awaiting authorization")

    def test_real_worker_flag_is_passed_through(self):
        self._add("req-flow")
        adapter = self._adapter()
        self._driver(adapter, ["req-flow"], real_worker=True).run()
        self.assertTrue(all(call[1] is True for call in adapter.calls))

    def test_step_ceiling_stops_the_run(self):
        self._add("req-flow")
        adapter = self._adapter()
        outcome = self._driver(adapter, ["req-flow"], max_steps=2).run()
        self.assertEqual(outcome.steps_executed, 2)
        self.assertIn("step ceiling", outcome.stop_reason)
        self.assertEqual(self.ledger.get_request("req-flow")["state"], "review")

    def test_multiple_authorized_requests_are_driven(self):
        self._add("req-one")
        self._add("req-two")
        outcome = self._driver(self._adapter(), ["req-one", "req-two"]).run()
        driven = {s["request_id"] for s in outcome.steps}
        self.assertEqual(driven, {"req-one", "req-two"})
        for rid in ("req-one", "req-two"):
            self.assertEqual(self.ledger.get_request(rid)["state"], "awaiting authorization")

    def test_worker_artifacts_are_journalled_on_the_step(self):
        self._add("req-flow")
        outcome = self._driver(self._adapter(), ["req-flow"], max_steps=1).run()
        self.assertEqual(outcome.steps[0]["artifacts"], ["out.txt"])
        self.assertEqual(outcome.steps[0]["worker_backend"], "scripted")


# ---------------------------------------------------------------------------
# Stopping conditions
# ---------------------------------------------------------------------------

class TestStopsForTheRightReasons(_Fixture):

    def test_no_progress_parks_instead_of_spinning(self):
        """
        A step that claims success and changes nothing is the signature of an
        infinite loop. It must park after exactly one attempt.
        """
        self._add("req-stuck", state="implementation")
        adapter = self._adapter(script={})   # nothing scripted: never transitions
        outcome = self._driver(adapter, ["req-stuck"], max_steps=10).run()
        self.assertEqual(outcome.steps_executed, 1, "the driver retried a no-op step")
        self.assertEqual(outcome.parked[0]["reason_code"], "no_progress")
        self.assertFalse(outcome.steps[0]["progressed"])

    def test_awaiting_authorization_is_where_the_driver_stops(self):
        self._add("req-gate", state="review")
        outcome = self._driver(self._adapter(), ["req-gate"]).run()
        codes = {p["reason_code"] for p in outcome.parked}
        self.assertIn("awaiting_authorization", codes)
        self.assertEqual(self.ledger.get_request("req-gate")["state"], "awaiting authorization")

    def test_request_already_at_the_merge_gate_is_never_dispatched(self):
        """A driver must not spend a worker on something waiting for a human."""
        self._add("req-waiting", state="review")
        setup_adapter = self._adapter()
        setup_adapter.run_step(request_id="req-waiting")
        adapter = self._adapter()
        outcome = self._driver(adapter, ["req-waiting"]).run()
        self.assertEqual(adapter.calls, [])
        self.assertEqual(outcome.parked[0]["reason_code"], "awaiting_authorization")
        self.assertIn("never merges", outcome.parked[0]["reason"])

    def test_done_request_is_not_dispatched(self):
        self._add("req-done", state="implementation")
        setup_adapter = self._adapter()
        for _ in range(3):
            setup_adapter.run_step(request_id="req-done")
        adapter = self._adapter()
        outcome = self._driver(adapter, ["req-done"]).run()
        self.assertEqual(adapter.calls, [])
        self.assertEqual(outcome.parked[0]["reason_code"], "awaiting_authorization")

    def test_adapter_exception_parks_the_request(self):
        self._add("req-boom")
        adapter = self._adapter(raise_on={"req-boom"})
        outcome = self._driver(adapter, ["req-boom"]).run()
        self.assertEqual(outcome.parked[0]["reason_code"], "error")
        self.assertIn("RuntimeError", outcome.parked[0]["reason"])
        self.assertEqual(outcome.steps[0]["result_status"], "error")

    def test_blocked_status_parks_the_request(self):
        self._add("req-blocked")
        adapter = self._adapter(script={"implementation": ("implementation", "blocked")})

        def run_step(request_id=None, target_sha=None, real_worker=False):
            adapter.calls.append((request_id, real_worker))
            return FakeResult("blocked", "preflight refused: staging unreachable")

        adapter.run_step = run_step
        outcome = self._driver(adapter, ["req-blocked"]).run()
        self.assertEqual(outcome.parked[0]["reason_code"], "blocked")
        self.assertIn("staging unreachable", outcome.parked[0]["reason"])
        with open(os.path.join(self.tmp, JOURNAL_FILENAME), encoding="utf-8") as fh:
            journal = json.load(fh)
        self.assertNotIn(
            "req-blocked",
            journal["completed_stages"],
            "a blocker write must not masquerade as completed stage work",
        )
        attempts = journal["stage_attempts"]["req-blocked"]
        self.assertEqual(attempts[-1]["outcome"], "blocked")

        # Restart remains idempotent while parked; the failed attempt is not
        # dispatched again unless an operator explicitly unparks it.
        restarted_adapter = self._adapter()
        restarted = self._driver(restarted_adapter, ["req-blocked"]).run()
        self.assertEqual(restarted.steps_executed, 0)
        self.assertEqual(restarted_adapter.calls, [])

    def test_one_request_parking_does_not_stop_the_other(self):
        self._add("req-ok")
        self._add("req-bad")
        adapter = self._adapter(raise_on={"req-bad"})
        outcome = self._driver(adapter, ["req-bad", "req-ok"]).run()
        self.assertEqual(self.ledger.get_request("req-ok")["state"], "awaiting authorization")
        codes = {p["request_id"]: p["reason_code"] for p in outcome.parked}
        self.assertEqual(codes["req-bad"], "error")


# ---------------------------------------------------------------------------
# Restart behaviour
# ---------------------------------------------------------------------------

class TestRestartDoesNotDuplicateWork(_Fixture):

    def test_restart_resumes_from_persisted_state_without_repeating(self):
        """
        A second run must continue from where the ledger stands, never re-run a
        stage the first run completed.
        """
        self._add("req-resume", state="implementation")
        adapter1 = self._adapter()
        first = self._driver(adapter1, ["req-resume"], max_steps=1).run()
        self.assertEqual([s["stage"] for s in first.steps], ["build"])
        self.assertEqual(self.ledger.get_request("req-resume")["state"], "QA")

        adapter2 = self._adapter()
        second = self._driver(adapter2, ["req-resume"], max_steps=5).run()
        self.assertTrue(second.resumed_from_journal)
        self.assertNotIn("build", [s["stage"] for s in second.steps],
                         "restart re-ran a completed stage")
        self.assertEqual([s["stage"] for s in second.steps], ["qa", "review"])

    def test_completed_stage_at_the_same_commit_is_refused_after_rollback(self):
        """
        The journal guard is independent of the ledger. If a ledger transition is
        lost or reverted, the driver still refuses to redo the stage it recorded
        complete at that exact commit.
        """
        head = "a" * 40
        self._add("req-rollback", state="implementation", head=head)
        adapter1 = self._adapter(script={"implementation": ("QA", "advanced")})
        self._driver(adapter1, ["req-rollback"], max_steps=1).run()

        # Something external reverts the state, leaving the same commit.
        self.ledger.update_request("req-rollback", state="implementation", actor="external")

        adapter2 = self._adapter()
        outcome = self._driver(adapter2, ["req-rollback"], max_steps=5).run()
        self.assertEqual(adapter2.calls, [], "the completed stage was dispatched again")
        self.assertTrue(outcome.skipped_completed)
        self.assertEqual(outcome.skipped_completed[0]["stage"], "build")
        self.assertEqual(outcome.parked[0]["reason_code"], "already_completed")

    def test_a_new_commit_makes_the_stage_runnable_again(self):
        """
        The guard is per commit, not per stage forever: new work at a new head is
        legitimately a new stage run.
        """
        journal = DriverJournal(os.path.join(self.tmp, JOURNAL_FILENAME))
        journal.record_completed("req-x", "qa", "a" * 40, "a" * 40)
        journal.save()
        self.assertIsNotNone(journal.stage_completed_at("req-x", "qa", "a" * 40))
        self.assertIsNone(journal.stage_completed_at("req-x", "qa", "b" * 40))

    def test_parked_request_stays_parked_across_restart(self):
        """Resuming a parked request is a human decision, not a restart side effect."""
        self._add("req-parked")
        adapter1 = self._adapter(raise_on={"req-parked"})
        self._driver(adapter1, ["req-parked"]).run()

        adapter2 = self._adapter()
        outcome = self._driver(adapter2, ["req-parked"]).run()
        self.assertEqual(adapter2.calls, [])
        self.assertEqual(outcome.parked[0]["reason_code"], "error")

    def test_unpark_allows_a_retry(self):
        self._add("req-retry")
        adapter1 = self._adapter(raise_on={"req-retry"})
        self._driver(adapter1, ["req-retry"]).run()

        journal = DriverJournal(os.path.join(self.tmp, JOURNAL_FILENAME))
        journal.unpark("req-retry")
        journal.save()

        adapter2 = self._adapter()
        self._driver(adapter2, ["req-retry"]).run()
        self.assertTrue(adapter2.calls)
        self.assertEqual(self.ledger.get_request("req-retry")["state"], "awaiting authorization")

    def test_corrupt_journal_does_not_wedge_the_driver(self):
        path = os.path.join(self.tmp, JOURNAL_FILENAME)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        self._add("req-after-corrupt")
        outcome = self._driver(self._adapter(), ["req-after-corrupt"], max_steps=1).run()
        self.assertEqual(outcome.steps_executed, 1)

    def test_journal_write_is_atomic_and_readable(self):
        self._add("req-journal")
        self._driver(self._adapter(), ["req-journal"], max_steps=1).run()
        with open(os.path.join(self.tmp, JOURNAL_FILENAME), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("completed_stages", data)
        self.assertIn("req-journal", data["completed_stages"])
        self.assertEqual(len(data["runs"]), 1)


# ---------------------------------------------------------------------------
# Single-driver guarantee
# ---------------------------------------------------------------------------

class TestSingleDriver(_Fixture):

    def test_second_driver_fails_fast_with_holder_details(self):
        lock_path = os.path.join(self.tmp, "d.lock")
        first = DriverRunLock(lock_path)
        first.acquire("run-a")
        try:
            second = DriverRunLock(lock_path)
            with self.assertRaises(DriverLockError) as ctx:
                second.acquire("run-b")
            message = str(ctx.exception)
            self.assertIn("run-a", message)
            self.assertIn(str(os.getpid()), message)
            self.assertIn("Refusing to start a second driver", message)
        finally:
            first.release()

    def test_lock_is_reusable_after_release(self):
        lock_path = os.path.join(self.tmp, "d.lock")
        a = DriverRunLock(lock_path)
        a.acquire("run-a")
        a.release()
        b = DriverRunLock(lock_path)
        b.acquire("run-b")
        b.release()

    def test_lock_is_released_even_when_a_run_aborts(self):
        self._add("req-abort")
        adapter = self._adapter(boundaries={"auto_merge_allowed": True})
        outcome = self._driver(adapter, ["req-abort"]).run()
        self.assertIsNotNone(outcome.error)
        follow_on = DriverRunLock(os.path.join(self.tmp, "continuation_driver.lock"))
        follow_on.acquire("run-next")
        follow_on.release()


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------

class TestBoundariesAreRefused(_Fixture):

    def test_auto_merge_claim_aborts_the_run(self):
        self._add("req-merge")
        adapter = self._adapter(boundaries={"auto_merge_allowed": True,
                                            "auto_deploy_allowed": False})
        outcome = self._driver(adapter, ["req-merge"]).run()
        self.assertIsNotNone(outcome.error)
        self.assertIn("auto_merge_allowed", outcome.error)
        self.assertEqual(outcome.stop_reason, "aborted on boundary violation")

    def test_auto_deploy_claim_aborts_the_run(self):
        self._add("req-deploy")
        adapter = self._adapter(boundaries={"auto_merge_allowed": False,
                                            "auto_deploy_allowed": True})
        outcome = self._driver(adapter, ["req-deploy"]).run()
        self.assertIsNotNone(outcome.error)
        self.assertIn("auto_deploy_allowed", outcome.error)

    def test_outcome_always_reports_merge_and_deploy_denied(self):
        self._add("req-bounds")
        outcome = self._driver(self._adapter(), ["req-bounds"], max_steps=1).run()
        self.assertIs(outcome.boundaries["auto_merge_allowed"], False)
        self.assertIs(outcome.boundaries["auto_deploy_allowed"], False)
        self.assertIs(outcome.boundaries["authorized_ids_only"], True)

    def test_boundary_check_reads_duck_typed_results(self):
        with self.assertRaises(BoundaryViolation):
            ContinuationDriver._assert_boundaries(
                {"boundaries": {"auto_merge_allowed": True}}, "req-x")
        ContinuationDriver._assert_boundaries({"boundaries": {}}, "req-x")
        ContinuationDriver._assert_boundaries({}, "req-x")


# ---------------------------------------------------------------------------
# Decision gating
# ---------------------------------------------------------------------------

class FakeDecisionManager:
    def __init__(self, decisions):
        self.decisions = decisions
        self.list_calls = 0

    def list_decisions(self, status=None):
        self.list_calls += 1
        if status:
            return [d for d in self.decisions if d.get("status") == status]
        return list(self.decisions)


def _pending_decision(req_id="req-dec", decision_id="DEC-1"):
    return {
        "decision_id": decision_id,
        "status": "pending",
        "request_id": req_id,
        "answer": None,
        "authorized_responders": ["Wladefant"],
        "blocking_dependencies": [req_id],
    }


def _answered_decision(req_id="req-dec", decision_id="DEC-1", responder="Wladefant",
                       provenance="human_operator", is_test=False,
                       interpretation="Option A"):
    return {
        "decision_id": decision_id,
        "status": "answered",
        "request_id": req_id,
        "answer": {
            "responder": responder,
            "provenance": provenance,
            "is_test": is_test,
            "interpretation": interpretation,
            "selected_option_id": "A",
        },
        "authorized_responders": ["Wladefant"],
        "blocking_dependencies": [req_id],
    }


class TestDecisionGating(_Fixture):

    def test_pending_decision_parks_before_any_dispatch(self):
        """A worker must not be spent while the direction is unresolved."""
        self._add("req-dec")
        mgr = FakeDecisionManager([_pending_decision()])
        adapter = self._adapter(decision_mgr=mgr)
        outcome = self._driver(adapter, ["req-dec"]).run()
        self.assertEqual(adapter.calls, [])
        self.assertEqual(outcome.parked[0]["reason_code"], "decision_blocked")
        self.assertIn("DEC-1", outcome.parked[0]["reason"])

    def test_bounded_recheck_is_off_by_default(self):
        self._add("req-dec")
        mgr = FakeDecisionManager([_pending_decision()])
        outcome = self._driver(self._adapter(decision_mgr=mgr), ["req-dec"]).run()
        self.assertIn("disabled", outcome.parked[0]["reason"])

    def test_authorized_human_answer_unblocks(self):
        self._add("req-dec")
        mgr = FakeDecisionManager([_answered_decision()])
        adapter = self._adapter(decision_mgr=mgr)
        self._driver(adapter, ["req-dec"]).run()
        self.assertTrue(adapter.calls, "an answered decision did not unblock the request")
        self.assertEqual(self.ledger.get_request("req-dec")["state"], "awaiting authorization")

    def test_only_a_genuine_authorized_answer_counts(self):
        """
        Every weaker shape the decision workflow exists to reject must keep the
        request blocked.
        """
        cases = {
            "synthetic test": _answered_decision(is_test=True),
            "agent authored": _answered_decision(provenance="agent_authored"),
            "unauthorized responder": _answered_decision(responder="RandomPerson"),
            "empty interpretation": _answered_decision(interpretation="   "),
            "still pending": _pending_decision(),
        }
        for label, decision in cases.items():
            with self.subTest(label):
                self.assertFalse(
                    ContinuationDriver._decision_is_authorized_answer(decision),
                    f"{label} was accepted as an authorized answer",
                )
        self.assertTrue(
            ContinuationDriver._decision_is_authorized_answer(_answered_decision()))

    def test_bounded_recheck_resumes_only_on_a_real_answer(self):
        """The re-check is finite and resumes on a genuine answer, without polling."""
        self._add("req-dec")
        mgr = FakeDecisionManager([_pending_decision()])
        adapter = self._adapter(decision_mgr=mgr)
        sleeps = []

        def sync():
            # The operator answers between the first and second re-check.
            mgr.decisions = [_answered_decision()]
            return (True, True, "synced", 1)

        adapter.coordinator.sync_decisions_if_configured = sync
        driver = self._driver(adapter, ["req-dec"],
                              decision_sync_attempts=3,
                              decision_sync_interval=1.0,
                              sleep_fn=sleeps.append)
        driver.run()
        self.assertEqual(len(sleeps), 1, "resumed later than the first real answer")
        self.assertTrue(adapter.calls)

    def test_bounded_recheck_gives_up_after_its_attempts(self):
        self._add("req-dec")
        mgr = FakeDecisionManager([_pending_decision()])
        adapter = self._adapter(decision_mgr=mgr)
        sleeps = []
        driver = self._driver(adapter, ["req-dec"],
                              decision_sync_attempts=3,
                              decision_sync_interval=1.0,
                              sleep_fn=sleeps.append)
        outcome = driver.run()
        self.assertEqual(len(sleeps), 3, "the re-check was not bounded by its attempts")
        self.assertEqual(adapter.calls, [])
        self.assertIn("still unanswered after 3", outcome.parked[0]["reason"])

    def test_recheck_interval_has_a_floor(self):
        """No caller can turn the bounded re-check into a tight poll."""
        self._add("req-dec")
        driver = self._driver(self._adapter(), ["req-dec"],
                              decision_sync_attempts=1,
                              decision_sync_interval=0.001)
        self.assertEqual(driver.decision_sync_interval, MIN_DECISION_SYNC_INTERVAL)

    def test_missing_decision_manager_does_not_block(self):
        """A harness without the decision workflow still drives."""
        self._add("req-nodec")
        adapter = self._adapter(decision_mgr=None)
        self._driver(adapter, ["req-nodec"]).run()
        self.assertTrue(adapter.calls)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

class TestSignalHandling(_Fixture):

    def test_stop_request_ends_the_run_after_the_current_step(self):
        """
        A signal must not orphan a worker mid-write: the in-flight step finishes,
        then the loop exits.
        """
        self._add("req-signal", state="implementation")
        adapter = self._adapter()
        driver = self._driver(adapter, ["req-signal"], max_steps=10)
        original = adapter.run_step

        def run_step(request_id=None, target_sha=None, real_worker=False):
            result = original(request_id=request_id, target_sha=target_sha,
                              real_worker=real_worker)
            driver._stop_requested = True
            driver._stop_signal = "SIGINT"
            return result

        adapter.run_step = run_step
        outcome = driver.run()
        self.assertEqual(outcome.steps_executed, 1)
        self.assertIn("SIGINT", outcome.stop_reason)
        # The step that was in flight still completed its transition.
        self.assertEqual(self.ledger.get_request("req-signal")["state"], "QA")

    def test_stop_request_ends_a_bounded_decision_wait(self):
        self._add("req-dec")
        mgr = FakeDecisionManager([_pending_decision()])
        adapter = self._adapter(decision_mgr=mgr)
        driver = self._driver(adapter, ["req-dec"],
                              decision_sync_attempts=5,
                              decision_sync_interval=1.0,
                              sleep_fn=lambda _s: setattr(driver, "_stop_requested", True))
        outcome = driver.run()
        self.assertEqual(adapter.calls, [])
        self.assertIn("Stop requested", outcome.parked[0]["reason"])

    def test_handlers_are_restored_after_a_run(self):
        import signal as signal_mod
        self._add("req-sig")
        before = signal_mod.getsignal(signal_mod.SIGINT)
        self._driver(self._adapter(), ["req-sig"],
                    install_signal_handlers=True, max_steps=1).run()
        self.assertIs(signal_mod.getsignal(signal_mod.SIGINT), before)


class TestInstalledDriverCliRepoRoot(_Fixture):
    """Exercise the documented CLI boundary, including a real child worker."""

    def _make_repo(self) -> tuple[str, str]:
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@localhost"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        with open(os.path.join(repo, "seed.txt"), "w", encoding="utf-8") as fh:
            fh.write("seed\n")
        subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        return repo, head

    def _worker_config(self, sentinel: str) -> str:
        worker = os.path.join(self.tmp, "cli_worker.py")
        with open(worker, "w", encoding="utf-8") as fh:
            fh.write(
                "import json, pathlib, subprocess, sys\n"
                "request_id, stage, repo_root, sentinel = sys.argv[1:5]\n"
                "pathlib.Path(sentinel).write_text('executed', encoding='utf-8')\n"
                "head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo_root, "
                "check=True, capture_output=True, text=True).stdout.strip()\n"
                "result = {'stage': stage, 'request_id': request_id, 'head_sha': head, "
                "'verdict': 'pass', 'summary': 'CLI worker verified exact HEAD', "
                "'checks': [{'command': ['git', 'rev-parse', 'HEAD'], 'exit_code': 0, "
                "'observed': head}], 'artifacts': []}\n"
                "print(json.dumps({'structured_output': result}))\n"
            )
        config = os.path.join(self.tmp, "worker_config.json")
        with open(config, "w", encoding="utf-8") as fh:
            json.dump({
                "default_backend": "scripted-cli",
                "backends": {
                    "scripted-cli": {
                        "argv": [
                            sys.executable, worker, "{request_id}", "{stage}",
                            "{repo_root}", sentinel,
                        ],
                        "result_source": "stdout_json",
                        "stdout_result_keys": ["structured_output"],
                        "schema_mode": "none",
                        "strict_model": False,
                    },
                },
            }, fh)
        return config

    def _run_cli(self, request_id: str, repo_root: str, config: str):
        return subprocess.run(
            [
                sys.executable, os.path.join(SCRIPT_DIR, "continuation_driver.py"),
                "--request-id", request_id,
                "--state-dir", self.tmp,
                "--repo-root", repo_root,
                "--worker-config", config,
                "--backend", "scripted-cli",
                "--max-steps", "1",
                "--json",
            ],
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            timeout=60,
            shell=False,
        )

    def test_cli_runs_worker_in_explicit_real_repository(self):
        repo, head = self._make_repo()
        sentinel = os.path.join(self.tmp, "legitimate-worker-ran")
        config = self._worker_config(sentinel)
        self._add("req-cli-valid", state="QA", head=head)

        proc = self._run_cli("req-cli-valid", repo, config)
        self.assertEqual(proc.returncode, 0, proc.stderr or proc.stdout)
        result = json.loads(proc.stdout)
        self.assertEqual(result["steps_executed"], 1)
        self.assertTrue(os.path.exists(sentinel))
        self.assertEqual(self.ledger.get_request("req-cli-valid")["state"], "review")

    def test_cli_requires_explicit_repo_root(self):
        proc = subprocess.run(
            [
                sys.executable, os.path.join(SCRIPT_DIR, "continuation_driver.py"),
                "--request-id", "req-no-root",
                "--state-dir", self.tmp,
            ],
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        self.assertEqual(proc.returncode, 64)
        self.assertIn("--repo-root is required", proc.stderr)

    def test_cli_invalid_directory_never_executes_sentinel_worker(self):
        repo, head = self._make_repo()
        invalid_repo = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(invalid_repo)
        sentinel = os.path.join(self.tmp, "invalid-worker-ran")
        config = self._worker_config(sentinel)
        self._add("req-cli-invalid", state="QA", head=head)

        proc = self._run_cli("req-cli-invalid", invalid_repo, config)
        self.assertEqual(proc.returncode, 1, proc.stderr or proc.stdout)
        result = json.loads(proc.stdout)
        self.assertEqual(result["steps_executed"], 1)
        self.assertFalse(os.path.exists(sentinel), "invalid repo dispatched the worker")
        self.assertEqual(result["parked"][0]["reason_code"], "error")
        with open(os.path.join(self.tmp, JOURNAL_FILENAME), encoding="utf-8") as fh:
            journal = json.load(fh)
        self.assertNotIn("req-cli-invalid", journal["completed_stages"])
        self.assertEqual(
            journal["stage_attempts"]["req-cli-invalid"][-1]["outcome"], "failed",
        )



class TestNativeBackgroundPending(_Fixture):

    def test_prepared_native_work_stops_cleanly_without_advancing_or_parking(self):
        self._add("req-native")
        adapter = self._adapter(script={})

        def pending_step(request_id=None, target_sha=None, real_worker=False):
            worker = type("PendingWorker", (), {
                "ok": False,
                "backend_name": "native",
                "artifacts": [],
                "native_run_id": "native_" + "a" * 24,
            })()
            return FakeResult(
                "prepared",
                "native task prepared; no lifecycle state advanced",
                worker=worker,
            )

        adapter.run_step = pending_step
        outcome = self._driver(adapter, ["req-native"]).run()
        self.assertEqual(outcome.steps_executed, 1)
        self.assertEqual(outcome.parked, [])
        self.assertEqual(outcome.inflight[0]["state"], "prepared")
        self.assertEqual(outcome.stop_reason, "native background work is in flight")
        self.assertEqual(self.ledger.get_request("req-native")["state"], "implementation")
        with open(os.path.join(self.tmp, JOURNAL_FILENAME), encoding="utf-8") as fh:
            journal = json.load(fh)
        self.assertEqual(journal["inflight"]["req-native"]["state"], "prepared")
        self.assertNotIn("req-native", journal["completed_stages"])

    def test_cli_unpark_creates_fresh_attempt_only_for_terminal_blocked_native(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@localhost"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        with open(os.path.join(repo, "seed.txt"), "w", encoding="utf-8") as fh:
            fh.write("seed\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        backend = WorkerBackend(state_dir=self.tmp)
        request = {
            "request_id": "req-retry",
            "stage": "qa",
            "repo_root": repo,
            "head_sha": head,
        }
        first = backend.prepare_native(request)
        first = backend.record_native_dispatch(first.run_id, "agent://first-child")
        invalid = {
            "stage": "qa",
            "request_id": "req-retry",
            "head_sha": head,
            "verdict": "pass",
            "summary": "missing evidence",
            "checks": [],
            "artifacts": [],
        }
        self.assertFalse(
            backend.complete_native(first.run_id, first.task_handle, invalid).ok
        )
        journal = DriverJournal(os.path.join(self.tmp, JOURNAL_FILENAME))
        journal.record_inflight(
            "req-retry", "qa", head, first.run_id, "background_dispatched",
        )
        journal.park(ParkRecord("req-retry", "blocked", "bad result", "qa", head))
        journal.save()

        exit_code = continuation_main([
            "--unpark", "req-retry",
            "--state-dir", self.tmp,
        ])

        self.assertEqual(exit_code, 0)
        retry = backend.prepare_native(request)
        self.assertNotEqual(retry.run_id, first.run_id)
        self.assertEqual(retry.state, "prepared")
        retry = backend.record_native_dispatch(retry.run_id, "agent://retry-child")
        check = subprocess.run(
            ["git", "show", "HEAD:seed.txt"], cwd=repo,
            capture_output=True, text=True,
        )
        valid = {
            "stage": "qa",
            "request_id": "req-retry",
            "head_sha": head,
            "verdict": "pass",
            "summary": "retry child verified repository",
            "checks": [{
                "name": "read seed",
                "command": ["git", "show", "HEAD:seed.txt"],
                "exit_code": check.returncode,
                "observed": check.stdout.strip(),
            }],
            "artifacts": [],
        }
        succeeded = backend.complete_native(
            retry.run_id, retry.task_handle, valid,
        )
        self.assertTrue(succeeded.ok, succeeded.blocked_reason)
        self.assertFalse(backend.get_native_outcome(first.run_id).ok)
        reloaded = DriverJournal(os.path.join(self.tmp, JOURNAL_FILENAME))
        self.assertIsNone(reloaded.parked_record("req-retry"))
        self.assertNotIn("req-retry", reloaded.data["inflight"])

class TestNativeCheckExpectationReconcile(_Fixture):
    """
    The real prepare-record-complete-reconcile lifecycle across the check
    expectation contract, driven through the actual SuperboardExecutionAdapter
    rather than the scripted one.

    This is the seam the defect lived in and the only place it is observable end
    to end: worker_backend finalized a completion whose baseline check exited 1,
    the adapter refused the same result on the next driver pass, run_step still
    reported 'advanced', and the driver parked the request as 'no_progress' with
    its own generic message while the ledger had never moved.
    """

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self._git("init", "-q", "-b", "main")
        self._git("config", "user.email", "t@localhost")
        self._git("config", "user.name", "T")
        self._git("config", "commit.gpgsign", "false")
        self.head = self._commit("seed.txt", "seed\n", "seed")

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True,
            check=False, shell=False,
        )

    def _commit(self, name, body, message):
        with open(os.path.join(self.repo, name), "w", encoding="utf-8") as fh:
            fh.write(body)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD").stdout.strip()

    def _real_adapter(self, backend):
        from superboard_adapter import SuperboardExecutionAdapter

        return SuperboardExecutionAdapter(
            state_dir=self.tmp,
            worker_backend=backend,
            notify_telegram=False,
            repo_root=self.repo,
        )

    def _dispatch(self, req_id, backend, adapter):
        """Drive the request to a bound background task, as the host would."""
        outcome = self._driver(adapter, [req_id]).run()
        self.assertEqual(outcome.parked, [], outcome.parked)
        self.assertEqual(len(outcome.inflight), 1, outcome.to_dict())
        run_id = outcome.inflight[0]["run_id"]
        self.assertTrue(run_id, "the driver recorded no native run id")
        handle = f"agent://child-{req_id}"
        self.assertEqual(
            backend.record_native_dispatch(run_id, handle).state,
            "background_dispatched",
        )
        return run_id, handle

    @staticmethod
    def _result(req_id, head, checks):
        return {
            "stage": "build",
            "request_id": req_id,
            "head_sha": head,
            "verdict": "pass",
            "summary": "implemented and verified",
            "checks": checks,
            "artifacts": [{"path": "fix.txt", "role": "implementation"}],
        }

    @staticmethod
    def _evidence_types(req):
        return [entry.get("type") for entry in req.get("evidence", [])]

    @staticmethod
    def _lifecycle_view(req):
        """
        The lifecycle-bearing projection of a request.

        A refused completion is a real failure, and the recurrence guard durably
        records that it happened: an observation, a same-state history row and
        the diagnosis it now wants. None of that is progress. What must not move
        is the lifecycle itself, so this projects exactly the fields that carry
        it - and any real advance changes at least one of them - instead of
        pretending the failure went unobserved.
        """
        return {
            "state": req.get("state"),
            "head": req.get("head"),
            "acceptance_criteria": req.get("acceptance_criteria"),
            "authorization": req.get("authorization"),
            "verification_evidence": [
                entry for entry in req.get("evidence", [])
                if entry.get("type") != "recurrence_observation"
            ],
            "state_path": [
                row.get("to_state") for row in req.get("history", [])
                if row.get("from_state") != row.get("to_state")
            ],
        }

    def test_declared_controls_reconcile_and_advance_exactly_once(self):
        req_id = "req-native-controls"
        self._add(req_id, head=self.head)
        backend = WorkerBackend(state_dir=self.tmp)
        adapter = self._real_adapter(backend)
        run_id, handle = self._dispatch(req_id, backend, adapter)

        # The background worker really worked: it committed, reproduced the
        # defect it was fixing, proved a guard still refuses, and ran a suite.
        built = self._commit("fix.txt", "fix\n", "fix")
        result = self._result(req_id, built, [
            {"name": "baseline defect reproduction", "command": ["python", "repro.py"],
             "exit_code": 1, "observed": "defect present before the fix",
             "purpose": "baseline", "expected_exit_code": 1},
            {"name": "guard refuses laundered evidence", "command": ["python", "guard.py"],
             "exit_code": 3, "observed": "refused", "purpose": "negative_control",
             "expected_exit_code": 3},
            {"name": "focused suite", "command": ["python", "-m", "unittest", "-q"],
             "exit_code": 0, "observed": "68 tests OK", "purpose": "verification"},
        ])

        completed = backend.complete_native(run_id, handle, result)
        self.assertTrue(completed.ok, completed.blocked_reason)
        # The observed exits survive finalization untouched.
        self.assertEqual(
            [c["exit_code"] for c in completed.evidence["checks"]], [1, 3, 0],
        )
        self.assertEqual(
            completed.evidence["check_expectations"]["verifications_passed"], 1,
        )
        self.assertEqual(
            completed.evidence["check_expectations"]["controls_retained"], 2,
        )

        reconciled = self._driver(adapter, [req_id]).run()
        step = reconciled.steps[0]
        self.assertEqual(step["result_status"], "advanced")
        self.assertTrue(step["progressed"])
        advanced = self.ledger.get_request(req_id)
        self.assertEqual(advanced["state"], "QA")
        self.assertEqual(advanced["head"], built)
        self.assertEqual(
            self._evidence_types(advanced).count("implementation_verification"), 1,
        )

        # Restart with a lost journal, and replay the completion: neither may
        # credit the build stage a second time.
        replay = backend.complete_native(run_id, handle, result)
        self.assertEqual(replay.to_dict(), completed.to_dict())
        os.remove(os.path.join(self.tmp, JOURNAL_FILENAME))
        restarted = self._driver(self._real_adapter(backend), [req_id]).run()
        self.assertEqual(restarted.steps[0]["stage"], "qa")
        after = self.ledger.get_request(req_id)
        self.assertEqual(after["state"], "QA")
        self.assertEqual(
            self._evidence_types(after).count("implementation_verification"), 1,
        )

    def test_unclassified_failing_check_parks_with_the_real_reason(self):
        """The reproduced case: refused at the shared source, reported truthfully."""
        req_id = "req-native-unclassified"
        self._add(req_id, head=self.head)
        backend = WorkerBackend(state_dir=self.tmp)
        adapter = self._real_adapter(backend)
        run_id, handle = self._dispatch(req_id, backend, adapter)
        before = self.ledger.get_request(req_id)

        built = self._commit("fix.txt", "fix\n", "fix")
        completed = backend.complete_native(run_id, handle, self._result(req_id, built, [
            {"name": "baseline defect reproduction", "command": ["python", "repro.py"],
             "exit_code": 1, "observed": "defect present"},
            {"name": "focused suite", "command": ["python", "-m", "unittest", "-q"],
             "exit_code": 0, "observed": "68 tests OK"},
        ]))
        self.assertFalse(completed.ok)
        self.assertIn("unclassified failing check", completed.blocked_reason)

        reconciled = self._driver(adapter, [req_id]).run()
        step = reconciled.steps[0]
        self.assertNotEqual(step["result_status"], "advanced")
        self.assertFalse(step["progressed"])
        park = reconciled.parked[0]
        self.assertNotEqual(park["reason_code"], "no_progress")
        self.assertIn("unclassified failing check", park["reason"])
        self.assertEqual(
            self._lifecycle_view(self.ledger.get_request(req_id)),
            self._lifecycle_view(before),
        )

    def test_controls_only_completion_is_refused_and_never_advances(self):
        req_id = "req-native-controls-only"
        self._add(req_id, head=self.head)
        backend = WorkerBackend(state_dir=self.tmp)
        adapter = self._real_adapter(backend)
        run_id, handle = self._dispatch(req_id, backend, adapter)
        before = self.ledger.get_request(req_id)

        built = self._commit("fix.txt", "fix\n", "fix")
        completed = backend.complete_native(run_id, handle, self._result(req_id, built, [
            {"name": "baseline defect reproduction", "command": ["python", "repro.py"],
             "exit_code": 1, "observed": "defect present",
             "purpose": "baseline", "expected_exit_code": 1},
            {"name": "guard refuses", "command": ["python", "guard.py"],
             "exit_code": 3, "observed": "refused",
             "purpose": "negative_control", "expected_exit_code": 3},
        ]))
        self.assertFalse(completed.ok)
        self.assertIn("none is a successful 'verification' check", completed.blocked_reason)

        reconciled = self._driver(adapter, [req_id]).run()
        self.assertNotEqual(reconciled.steps[0]["result_status"], "advanced")
        self.assertEqual(
            self._lifecycle_view(self.ledger.get_request(req_id)),
            self._lifecycle_view(before),
        )


class TestContinuationDriverTelegramNotifications(_Fixture):
    """Behavioral regression exercising parser/driver -> adapter notification path.

    Defends:
      1. Same CLI switches and safe default semantics as superboard_adapter:
         --notify-telegram, --telegram-send, --telegram-dry-run.
      2. Forwarding booleans via adapter_kwargs to SuperboardExecutionAdapter.
      3. No-default-send: default CLI execution or --notify-telegram alone does NOT dispatch to network.
      4. Explicit send enables existing sender: --notify-telegram --telegram-send dispatches via transport.
      5. Dry-run overrides send: --telegram-dry-run suppresses network dispatch even if --telegram-send is present.
      6. Safe defaults: enabling live requires explicit notification + send; send alone does not notify.
    """

    def setUp(self):
        super().setUp()
        # Zero-quota fixture credentials for dry-run Telegram notification testing
        self._orig_chat_id = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID")
        self._orig_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not self._orig_chat_id:
            os.environ["TELEGRAM_NOTIFY_CHAT_ID"] = "1247617658"
        if not self._orig_token:
            os.environ["TELEGRAM_BOT_TOKEN"] = "dummy_token_for_dry_run"

    def tearDown(self):
        if self._orig_chat_id is None:
            os.environ.pop("TELEGRAM_NOTIFY_CHAT_ID", None)
        else:
            os.environ["TELEGRAM_NOTIFY_CHAT_ID"] = self._orig_chat_id

        if self._orig_token is None:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        else:
            os.environ["TELEGRAM_BOT_TOKEN"] = self._orig_token
        super().tearDown()

    def _setup_git_repo(self) -> str:
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@localhost"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
        with open(os.path.join(repo, "seed.txt"), "w", encoding="utf-8") as f:
            f.write("seed\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
        return repo

    def _add_request(self, req_id: str, state: str = "implementation", issue_number: int = 100):
        return self.ledger.add_request(
            req_id=req_id,
            prompt=f"prompt for {req_id}",
            session="test",
            project="polysimulator",
            acceptance_criteria=[{"criterion": "works", "status": "pending", "evidence": ""}],
            owner="Tester",
            state=state,
            head="a" * 40,
            task_type="deployable",
            issue_number=issue_number,
        )

    def test_cli_telegram_flags_parsing(self):
        """Verify build_parser preserves safe dry-run by default unless explicit --telegram-send."""
        parser = build_parser()

        # 1. Default (no telegram flags)
        args_default = parser.parse_args([])
        self.assertFalse(args_default.notify_telegram)
        self.assertIsNone(args_default.telegram_dry_run)
        self.assertFalse(args_default.telegram_send)

        # 2. Notify enabled (dry-run default preserved)
        args_notify = parser.parse_args(["--notify-telegram"])
        self.assertTrue(args_notify.notify_telegram)
        self.assertIsNone(args_notify.telegram_dry_run)
        self.assertFalse(args_notify.telegram_send)

        # 3. Explicit send
        args_send = parser.parse_args(["--notify-telegram", "--telegram-send"])
        self.assertTrue(args_send.notify_telegram)
        self.assertIsNone(args_send.telegram_dry_run)
        self.assertTrue(args_send.telegram_send)

        # 4. Explicit dry run
        args_dry = parser.parse_args(["--notify-telegram", "--telegram-dry-run"])
        self.assertTrue(args_dry.notify_telegram)
        self.assertTrue(args_dry.telegram_dry_run)
        self.assertFalse(args_dry.telegram_send)

        # 5. Both flags present
        args_both = parser.parse_args(["--notify-telegram", "--telegram-dry-run", "--telegram-send"])
        self.assertTrue(args_both.notify_telegram)
        self.assertTrue(args_both.telegram_dry_run)
        self.assertTrue(args_both.telegram_send)

    def test_adapter_kwargs_forwarding_booleans(self):
        """Verify continuation_main resolves and forwards booleans via adapter_kwargs."""
        repo = self._setup_git_repo()

        scenarios = [
            (
                ["--request-id", "req-kwargs-test", "--state-dir", self.tmp, "--repo-root", repo, "--no-real-worker", "--max-steps", "1"],
                {"notify_telegram": False, "telegram_dry_run": True, "telegram_send": False},
            ),
            (
                ["--request-id", "req-kwargs-test", "--state-dir", self.tmp, "--repo-root", repo, "--no-real-worker", "--max-steps", "1", "--notify-telegram"],
                {"notify_telegram": True, "telegram_dry_run": True, "telegram_send": False},
            ),
            (
                ["--request-id", "req-kwargs-test", "--state-dir", self.tmp, "--repo-root", repo, "--no-real-worker", "--max-steps", "1", "--notify-telegram", "--telegram-send"],
                {"notify_telegram": True, "telegram_dry_run": False, "telegram_send": True},
            ),
            (
                ["--request-id", "req-kwargs-test", "--state-dir", self.tmp, "--repo-root", repo, "--no-real-worker", "--max-steps", "1", "--notify-telegram", "--telegram-dry-run"],
                {"notify_telegram": True, "telegram_dry_run": True, "telegram_send": False},
            ),
            (
                ["--request-id", "req-kwargs-test", "--state-dir", self.tmp, "--repo-root", repo, "--no-real-worker", "--max-steps", "1", "--notify-telegram", "--telegram-dry-run", "--telegram-send"],
                {"notify_telegram": True, "telegram_dry_run": True, "telegram_send": False},
            ),
            (
                ["--request-id", "req-kwargs-test", "--state-dir", self.tmp, "--repo-root", repo, "--no-real-worker", "--max-steps", "1", "--telegram-send"],
                {"notify_telegram": False, "telegram_dry_run": False, "telegram_send": True},
            ),
        ]

        for i, (argv_template, expected) in enumerate(scenarios):
            req_id = f"req-kwargs-{i}"
            self._add_request(req_id, state="implementation")
            argv = [req_id if arg == "req-kwargs-test" else arg for arg in argv_template]

            with patch("superboard_adapter.SuperboardExecutionAdapter") as mock_adapter_cls:
                mock_inst = MagicMock()
                mock_inst.state_dir = self.tmp
                mock_inst.ledger = self.ledger
                mock_inst.coordinator.ledger = self.ledger
                mock_inst.run_step.return_value = FakeResult(status="done")
                mock_adapter_cls.return_value = mock_inst

                continuation_main(argv)

                kwargs = mock_adapter_cls.call_args.kwargs
                self.assertIsInstance(kwargs.get("notify_telegram"), bool)
                self.assertIsInstance(kwargs.get("telegram_dry_run"), bool)
                self.assertIsInstance(kwargs.get("telegram_send"), bool)
                self.assertEqual(kwargs.get("notify_telegram"), expected["notify_telegram"])
                self.assertEqual(kwargs.get("telegram_dry_run"), expected["telegram_dry_run"])
                self.assertEqual(kwargs.get("telegram_send"), expected["telegram_send"])

    def test_no_default_send_leaves_transport_untouched(self):
        """Safe default: Running without --notify-telegram or with safe defaults never calls network."""
        repo = self._setup_git_repo()
        self._add_request("req-no-send", state="implementation", issue_number=101)

        with patch("urllib.request.urlopen") as mock_urlopen:
            exit_code = continuation_main([
                "--request-id", "req-no-send",
                "--state-dir", self.tmp,
                "--repo-root", repo,
                "--no-real-worker",
                "--max-steps", "1",
            ])
            self.assertEqual(mock_urlopen.call_count, 0, "Default run must never contact Telegram API")

    def test_notify_dry_run_prevents_live_network(self):
        """Dry-run prevents live network dispatch while activating adapter notifications."""
        repo = self._setup_git_repo()
        self._add_request("req-dry-run", state="implementation", issue_number=102)

        with patch("urllib.request.urlopen") as mock_urlopen:
            exit_code = continuation_main([
                "--request-id", "req-dry-run",
                "--state-dir", self.tmp,
                "--repo-root", repo,
                "--no-real-worker",
                "--max-steps", "1",
                "--notify-telegram",
                "--telegram-dry-run",
            ])
            self.assertEqual(mock_urlopen.call_count, 0, "Dry-run must strictly prevent live network calls")

    def test_dry_run_overrides_send_flag(self):
        """When both --telegram-dry-run and --telegram-send are passed, dry-run overrides send."""
        repo = self._setup_git_repo()
        self._add_request("req-forced-dry", state="implementation", issue_number=103)

        with patch("urllib.request.urlopen") as mock_urlopen:
            exit_code = continuation_main([
                "--request-id", "req-forced-dry",
                "--state-dir", self.tmp,
                "--repo-root", repo,
                "--no-real-worker",
                "--max-steps", "1",
                "--notify-telegram",
                "--telegram-dry-run",
                "--telegram-send",
            ])
            self.assertEqual(mock_urlopen.call_count, 0, "Explicit dry-run must override send and prevent network")

    def test_explicit_send_enables_existing_sender_via_transport(self):
        """Explicit --notify-telegram and --telegram-send enables the existing sender to dispatch to network."""
        repo = self._setup_git_repo()
        self._add_request("req-explicit-send", state="implementation", issue_number=104)

        with patch("telegram_notifier.DeduplicationLedger.check_eligible", return_value=(True, "eligible", "ok")), \
             patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({
                "ok": True,
                "result": {"message_id": 99991, "from": {"id": 8566730274, "is_bot": True}},
            }).encode("utf-8")
            mock_urlopen.return_value.__enter__.return_value = mock_resp

            exit_code = continuation_main([
                "--request-id", "req-explicit-send",
                "--state-dir", self.tmp,
                "--repo-root", repo,
                "--no-real-worker",
                "--max-steps", "1",
                "--notify-telegram",
                "--telegram-send",
            ])

            self.assertGreaterEqual(mock_urlopen.call_count, 1, "Live send must dispatch via urllib.request.urlopen")
            req = mock_urlopen.call_args_list[0][0][0]
            self.assertEqual(req.get_method(), "POST")
            self.assertIn("api.telegram.org", req.full_url)
            self.assertIn("/sendMessage", req.full_url)

            payload = json.loads(req.data.decode("utf-8"))
            self.assertIn("chat_id", payload)
            self.assertIn("text", payload)
            self.assertIn("req-explicit-send", payload["text"])
            self.assertIn("https://github.com/", payload["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
