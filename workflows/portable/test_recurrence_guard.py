#!/usr/bin/env python3
"""
test_recurrence_guard.py - Contract tests for durable failure recurrence.

The guard's whole value is that a repeated failure changes what the system does.
Every test here defends one of the properties that makes that true, because each
failure mode looks like normal operation from the inside:

  * a replayed event counted as a new occurrence, escalating an incident that
    happened once;
  * a corrective action that clears the retry gate and then silently survives the
    same failure happening again;
  * history that does not survive the process that wrote it;
  * a corrective action that quietly stands in for QA, review or authorization;
  * one incident escalating once per restart instead of once per incident.

Two fixtures are real, observed incidents rather than invented strings:

  * a staging backend/daemon that restarted ten times in ten minutes on one
    identical Alembic missing-revision signature (`check_alembic_drift.py` taking
    the "DB has revision not in code chain" branch and exiting 2, so
    `docker-entrypoint.sh` refused to start). Identifiers and timestamps are
    sanitized fixture values; nothing here contacts a live deployment.
  * a native review stage that returned verdict `fail` (REQUEST-CHANGES, F1/F2)
    on an exact head whose scoped QA had passed. It is here to pin the property
    that a QA pass is not recovery for an independent review gate failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from ledger import RequestLedger, normalize_acceptance_criteria  # noqa: E402
from recurrence_guard import (  # noqa: E402
    CORRECTIVE_ACTION_THRESHOLD,
    ESCALATION_THRESHOLD,
    STATUS_CORRECTIVE_ACTION_RECORDED,
    STATUS_CORRECTIVE_ACTION_REQUIRED,
    STATUS_ESCALATED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    RecurrenceGuard,
    RecurrenceGuardError,
    compute_signature,
    error_class,
    normalize_error,
)

GUARD_CLI = os.path.join(SCRIPT_DIR, "recurrence_guard.py")
LEDGER_CLI = os.path.join(SCRIPT_DIR, "ledger.py")

#: Sanitized transcription of the observed staging daemon restart loop: one
#: identical signature, ten cycles one minute apart. Each cycle differs only in
#: its wall-clock timestamp, which is exactly the volatile detail a signature
#: must see through, and the attempt id is what makes each cycle a real distinct
#: occurrence rather than a replay.
DAEMON_RESTART_ERROR = (
    "check_alembic_drift.py: DB has revision not in code chain "
    "(alembic_version=20260904_page_views_kind); exit=2; "
    "docker-entrypoint.sh refused to start backend and backend-daemon "
    "[container fixture-container-a, 2026-09-05T23:{minute:02d}:00Z]"
)
#: 23:21:00Z through 23:30:00Z, as observed.
DAEMON_RESTART_MINUTES = tuple(range(21, 31))
DAEMON_RESTART_CYCLES = len(DAEMON_RESTART_MINUTES)

#: Sanitized transcription of the observed review-stage failure.
REVIEW_FAIL_ERROR = (
    "Worker returned verdict 'fail' for stage 'review': REQUEST-CHANGES on exact head "
    "a16b070bd78abd9a3838938f68d8656b53959f48 (PR #4576, base staging). "
    "[F1, regression, HIGH] Fallback grid injects unrelated hot groups. "
    "[F2, defect, MEDIUM] Pagination completeness is not preserved."
)
REVIEW_HEAD = "a16b070bd78abd9a3838938f68d8656b53959f48"

#: Sanitized transcription of an observed mutation probe. It exits 1 *on purpose*
#: when the guard under test is too weak, so its non-zero exit is the probe
#: working, not a system failing. Ingesting it as an unexpected failure is how a
#: recurrence guard escalates off a test suite's own intended output.
MUTATION_PROBE_ERROR = (
    "migration-guard-mutation-repro.py exit=1: REPRODUCED: existing guard passed after "
    "--noconftest was removed from the executed command; its comment still satisfies the "
    "source-text assertion"
)


def run_cli(args, cwd=None):
    """Run the guard CLI in its own process, the way an operator or script does."""
    proc = subprocess.run(
        [sys.executable, GUARD_CLI] + list(args),
        capture_output=True,
        text=True,
        cwd=cwd or SCRIPT_DIR,
    )
    return proc


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recurrence_guard_test_")
        self.addCleanup(self._cleanup)
        self.guard = RecurrenceGuard(state_dir=self.tmp)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def observe_daemon_cycle(self, minute, **kwargs):
        """One observed restart cycle of the staging daemon fixture."""
        params = dict(
            project="fixture-org/fixture-repo",
            environment="staging",
            operation="daemon:startup",
            error=DAEMON_RESTART_ERROR.format(minute=minute),
            source="deploy",
            attempt=f"fixture-restart-cycle-{minute}",
            update_ledger=False,
        )
        params.update(kwargs)
        return self.guard.observe(**params)


class TestErrorIdentity(GuardTestCase):
    """A signature must survive volatile text and still separate distinct faults."""

    def test_volatile_parts_do_not_split_one_fault(self):
        """The same fault at a different path, run id and line number is one class."""
        a = (
            "Native dispatch native_a98f185c20ba1b72 blocked at "
            r"C:\Users\op\AppData\Local\Temp\tmp8f2a\repo (ledger.py:452): "
            "'str' object has no attribute 'get'"
        )
        b = (
            "Native dispatch native_0011223344556677 blocked at "
            "/tmp/pytest-991/repo (ledger.py:987): "
            "'str' object has no attribute 'get'"
        )
        self.assertEqual(error_class(a), error_class(b))

    def test_distinct_faults_stay_distinct(self):
        """A different failing type is a different fault, not the same recurrence."""
        a = "/tmp/x/y (ledger.py:1): 'str' object has no attribute 'get'"
        b = "/tmp/x/y (ledger.py:1): 'dict' object has no attribute 'get'"
        self.assertNotEqual(error_class(a), error_class(b))

    def test_traceback_reduces_to_the_raised_error(self):
        """Intermediate frames move with every refactor; the raised error is the fault."""
        tb = (
            "Traceback (most recent call last):\n"
            '  File "/a/b.py", line 3, in outer\n'
            "    inner()\n"
            "ValueError: bad thing 42"
        )
        self.assertEqual(normalize_error(tb), "valueerror: bad thing <n>")

    def test_explicit_class_overrides_the_heuristic(self):
        """A caller that knows the real identity is not second-guessed."""
        self.assertEqual(
            error_class("anything at all", explicit="alembic:missing-revision"),
            "alembic:missing-revision",
        )

    def test_identityless_observation_is_refused(self):
        with self.assertRaises(RecurrenceGuardError):
            error_class("   ")

    def test_signature_requires_all_four_components(self):
        for missing in range(4):
            parts = ["proj", "staging", "daemon:startup", "cls"]
            parts[missing] = ""
            with self.assertRaises(RecurrenceGuardError):
                compute_signature(*parts)


class TestDuplicateIngestion(GuardTestCase):
    """Re-ingesting a known event must never manufacture a recurrence."""

    def test_replayed_event_is_not_a_second_occurrence(self):
        first = self.observe_daemon_cycle(1)
        replay = self.observe_daemon_cycle(1)
        self.assertFalse(first.duplicate)
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.occurrences, 1)
        self.assertEqual(replay.status, STATUS_OPEN)
        self.assertTrue(replay.retry_allowed)

    def test_replay_after_escalation_neither_escalates_nor_counts(self):
        """Re-reading the same incident artifact must not add occurrences."""
        for minute in range(1, ESCALATION_THRESHOLD + 1):
            self.observe_daemon_cycle(minute)
        before = self.guard.get(self.observe_daemon_cycle(1).signature)
        replay = self.observe_daemon_cycle(2)
        after = self.guard.get(replay.signature)
        self.assertTrue(replay.duplicate)
        self.assertIsNone(replay.escalation)
        self.assertEqual(before["occurrences"], after["occurrences"])
        self.assertEqual(len(before["escalations"]), len(after["escalations"]))

    def test_explicit_observation_id_is_the_dedup_key(self):
        """A source with its own unique id controls duplicate detection directly."""
        a = self.observe_daemon_cycle(1, observation_id="ci-run-771", attempt="whatever")
        b = self.observe_daemon_cycle(9, observation_id="ci-run-771", attempt="different")
        self.assertFalse(a.duplicate)
        self.assertTrue(b.duplicate)
        self.assertEqual(b.occurrences, 1)


class TestCorrectiveActionGates(GuardTestCase):
    """Occurrence count must change behaviour, not just a counter."""

    def test_first_occurrence_permits_retry_and_demands_triage(self):
        result = self.observe_daemon_cycle(1)
        self.assertEqual(result.occurrences, 1)
        self.assertEqual(result.status, STATUS_OPEN)
        self.assertTrue(result.retry_allowed)
        self.assertFalse(result.diagnosis_complete)
        self.assertIn("diagnosis", result.required_action.lower())

    def test_first_occurrence_retains_diagnosis_owner_and_next_action(self):
        result = self.observe_daemon_cycle(
            1,
            diagnosis="staging alembic_version is stamped at a revision absent from the code chain",
            owner="DaemonMigrationRepair",
            next_action="restore the missing revision file on the branch",
        )
        entry = self.guard.get(result.signature)
        self.assertTrue(entry["diagnosis_complete"])
        self.assertEqual(entry["owner"], "DaemonMigrationRepair")
        self.assertIn("code chain", entry["diagnosis"])
        self.assertIn("restore the missing revision", entry["next_action"])

    def test_second_distinct_occurrence_blocks_unchanged_retry(self):
        self.observe_daemon_cycle(1)
        second = self.observe_daemon_cycle(2)
        self.assertEqual(second.occurrences, CORRECTIVE_ACTION_THRESHOLD)
        self.assertEqual(second.status, STATUS_CORRECTIVE_ACTION_REQUIRED)
        self.assertFalse(second.retry_allowed)
        decision = self.guard.check_retry(signature=second.signature)
        self.assertFalse(decision.allowed)
        self.assertIn("Unchanged retry refused", decision.reason)

    def test_recorded_corrective_action_unblocks_retry(self):
        self.observe_daemon_cycle(1)
        second = self.observe_daemon_cycle(2)
        out = self.guard.record_corrective_action(
            signature=second.signature,
            kind="code_change",
            description="restored the missing revision file so the code chain contains it",
            change_ref="fixture-commit-0aaa09c",
            actor="DaemonMigrationRepair",
        )
        self.assertEqual(out["status"], STATUS_CORRECTIVE_ACTION_RECORDED)
        self.assertTrue(self.guard.check_retry(signature=second.signature).allowed)

    def test_recurrence_after_correction_reopens_and_blocks_again(self):
        """A fix that did not hold is exactly what must not be retried blind."""
        self.observe_daemon_cycle(1)
        second = self.observe_daemon_cycle(2)
        self.guard.record_corrective_action(
            signature=second.signature,
            kind="code_change",
            description="restored the missing revision file",
            change_ref="fixture-commit-0aaa09c",
            actor="DaemonMigrationRepair",
        )
        third = self.observe_daemon_cycle(3)
        self.assertTrue(third.reopened)
        self.assertFalse(third.retry_allowed)
        self.assertEqual(self.guard.get(third.signature)["reopened_count"], 1)

    def test_corrective_action_without_a_description_or_change_is_refused(self):
        """A gate cleared by a blank claim, or by 'retry later', is worse than no gate."""
        sig = self.observe_daemon_cycle(1).signature
        self.observe_daemon_cycle(2)
        for kwargs in (
            {"description": "   ", "change_ref": "fixture-commit-0aaa09c"},
            {"description": "restored the revision file", "change_ref": None},
            {"description": "restored the revision file", "change_ref": "  "},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(RecurrenceGuardError):
                    self.guard.record_corrective_action(
                        signature=sig, kind="code_change", actor="someone", **kwargs
                    )
        self.assertFalse(self.guard.check_retry(signature=sig).allowed)

    def test_corrective_action_needs_an_observed_failure(self):
        with self.assertRaises(RecurrenceGuardError):
            self.guard.record_corrective_action(
                signature="0" * 64, kind="code_change", description="x", actor="y",
                change_ref="fixture-commit-0aaa09c",
            )


class TestSafetyBoundaries(GuardTestCase):
    """The guard records; it never executes, authorizes or verifies."""

    def test_privileged_kinds_require_explicit_authorization(self):
        sig = self.observe_daemon_cycle(1).signature
        self.observe_daemon_cycle(2)
        for kind in ("ddl", "deployment", "privileged_operation", "gate_change"):
            with self.subTest(kind=kind):
                with self.assertRaises(RecurrenceGuardError):
                    self.guard.record_corrective_action(
                        signature=sig, kind=kind, description="apply it", actor="lane",
                        change_ref="fixture-commit-0aaa09c",
                    )
        self.assertFalse(self.guard.check_retry(signature=sig).allowed)

    def test_authorized_privileged_action_is_recorded_not_executed(self):
        sig = self.observe_daemon_cycle(1).signature
        self.observe_daemon_cycle(2)
        out = self.guard.record_corrective_action(
            signature=sig,
            kind="ddl",
            description="operator applied the reviewed downgrade SQL out of band",
            change_ref="fixture-comment-5543918753",
            actor="lane",
            authorization="operator attestation fixture-comment-5543918753",
        )
        self.assertTrue(out["action"]["privileged"])
        self.assertFalse(out["action"]["executed"])
        self.assertEqual(
            out["action"]["authorization"], "operator attestation fixture-comment-5543918753"
        )

    def test_corrective_action_does_not_verify_criteria_or_authorization(self):
        """Clearing the retry gate must not touch the request's real gates."""
        ledger = RequestLedger(state_dir=self.tmp)
        ledger.add_request(
            req_id="req-fixture-daemon",
            prompt="restore the migration graph",
            session="fixture-session",
            project="fixture-org/fixture-repo",
            acceptance_criteria=["containers boot on the exact head"],
            owner="DaemonMigrationRepair",
            task_type="local",
        )
        result = self.observe_daemon_cycle(1, request_id="req-fixture-daemon",
                                           update_ledger=True, ledger=ledger)
        self.observe_daemon_cycle(2, request_id="req-fixture-daemon",
                                  update_ledger=True, ledger=ledger)
        self.guard.record_corrective_action(
            signature=result.signature,
            kind="code_change",
            description="restored the missing revision file",
            change_ref="fixture-commit-0aaa09c",
            actor="DaemonMigrationRepair",
        )
        req = ledger.get_request("req-fixture-daemon")
        self.assertEqual(req["state"], "pending")
        self.assertEqual(req["authorization"]["status"], "pending")
        self.assertTrue(all(c["status"] == "pending" for c in req["acceptance_criteria"]))

    def test_resolution_requires_a_commit_evidence_scenario_and_a_change(self):
        """Closure points at the corrective change AND the re-executed scenario."""
        sig = self.observe_daemon_cycle(1).signature
        scenario = "run docker-entrypoint.sh with staging stamped at the missing revision"
        for kwargs in (
            {"head_sha": "0aaa09c", "evidence": "ok", "scenario": scenario},
            {"head_sha": "a" * 40, "evidence": "  ", "scenario": scenario},
            {"head_sha": "a" * 40, "evidence": "ok", "scenario": "  "},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(RecurrenceGuardError):
                    self.guard.resolve(sig, actor="lane", **kwargs)

        # No corrective change recorded yet: a passing scenario alone cannot close it.
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.guard.resolve(
                sig, head_sha="a" * 40, scenario=scenario,
                evidence="check_alembic_drift.py exited 0", actor="lane",
            )
        self.assertIn("no systemic corrective action", str(ctx.exception))

        self.guard.record_corrective_action(
            signature=sig, kind="code_change", actor="lane",
            description="restored the revision file", change_ref="fixture-commit-0aaa09c",
        )
        out = self.guard.resolve(
            sig,
            head_sha="a" * 40,
            scenario=scenario,
            evidence="check_alembic_drift.py exited 0 on the fixture database",
            actor="lane",
        )
        self.assertEqual(out["status"], STATUS_RESOLVED)
        self.assertEqual(out["resolution"]["change_refs"], ["fixture-commit-0aaa09c"])
        self.assertEqual(out["resolution"]["scenario"], scenario)
        self.assertIn("No claim of universal absence", out["resolution"]["scope"])

    def test_resolution_is_not_a_permanent_claim(self):
        sig = self.observe_daemon_cycle(1).signature
        self.guard.record_corrective_action(
            signature=sig, kind="code_change", actor="lane",
            description="restored the revision file", change_ref="fixture-commit-0aaa09c",
        )
        self.guard.resolve(
            sig, head_sha="a" * 40, evidence="exit 0", actor="lane",
            scenario="run the entrypoint with the missing revision stamped",
        )
        again = self.observe_daemon_cycle(2)
        self.assertTrue(again.reopened)
        self.assertFalse(again.retry_allowed)

    def test_qa_pass_is_not_recovery_for_an_independent_review_failure(self):
        """
        Observed case: scoped QA passed on the exact head and review still returned
        REQUEST-CHANGES for F1/F2. The review failure is its own signature and only
        its own evidence closes it.
        """
        review = self.guard.observe(
            project="fixture-org/fixture-repo",
            environment="harness",
            operation="worker:review",
            error=REVIEW_FAIL_ERROR,
            source="native_worker",
            head_sha=REVIEW_HEAD,
            stage="review",
            attempt="fixture-review-run-1",
            request_id="req-fixture-market-qa",
            update_ledger=False,
        )
        qa = self.guard.observe(
            project="fixture-org/fixture-repo",
            environment="harness",
            operation="worker:qa",
            error="scoped QA scenario failed once before passing",
            source="native_worker",
            head_sha=REVIEW_HEAD,
            stage="qa",
            attempt="fixture-qa-run-1",
            request_id="req-fixture-market-qa",
            update_ledger=False,
        )
        self.assertNotEqual(review.signature, qa.signature)

        # A QA resolution on the same head and request closes only the QA signature.
        self.guard.record_corrective_action(
            signature=qa.signature, kind="code_change", actor="qa-lane",
            description="fixed the scoped QA path", change_ref="fixture-commit-a16b070",
        )
        self.guard.resolve(
            qa.signature,
            head_sha=REVIEW_HEAD,
            scenario="the scoped QA scenario that failed on this head",
            evidence="scoped QA scenario passed on the exact head",
            actor="qa-lane",
        )
        self.assertEqual(self.guard.get(qa.signature)["status"], STATUS_RESOLVED)
        self.assertEqual(self.guard.get(review.signature)["status"], STATUS_OPEN)

        # And a second review failure still blocks unchanged retry of the request.
        self.guard.observe(
            project="fixture-org/fixture-repo",
            environment="harness",
            operation="worker:review",
            error=REVIEW_FAIL_ERROR,
            source="native_worker",
            head_sha=REVIEW_HEAD,
            stage="review",
            attempt="fixture-review-run-2",
            request_id="req-fixture-market-qa",
            update_ledger=False,
        )
        decision = self.guard.check_retry(request_id="req-fixture-market-qa")
        self.assertFalse(decision.allowed)
        self.assertEqual(
            [b["operation"] for b in decision.blocking], ["worker:review"]
        )

    def test_unknown_observation_source_is_refused(self):
        with self.assertRaises(RecurrenceGuardError):
            self.observe_daemon_cycle(1, source="telemetry")

    def test_corrupt_store_is_reported_not_silently_emptied(self):
        """History is the point of the file; discarding it would erase every occurrence."""
        self.observe_daemon_cycle(1)
        with open(self.guard.store_path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        with self.assertRaises(RecurrenceGuardError):
            self.guard.list_signatures()


class TestEscalation(GuardTestCase):
    """Escalation is one event per incident, through the existing contract."""

    def test_third_occurrence_escalates_through_the_notification_contract(self):
        self.observe_daemon_cycle(1)
        self.observe_daemon_cycle(2)
        third = self.observe_daemon_cycle(
            3, canonical_link="https://github.com/fixture-org/fixture-repo/issues/4574"
        )
        self.assertEqual(third.status, STATUS_ESCALATED)
        self.assertIsNotNone(third.escalation)
        self.assertTrue(third.escalation["event_valid"], third.escalation)
        self.assertTrue(third.escalation["dedup_signature"])
        event = third.escalation["notification_event"]
        self.assertEqual(event["event_type"], "blocker")
        self.assertEqual(
            event["canonical_link"], "https://github.com/fixture-org/fixture-repo/issues/4574"
        )
        self.assertEqual(event["metadata"]["recurrence_signature"], third.signature)

    def test_ten_observed_restarts_escalate_once(self):
        """
        The observed incident: ten identical restarts inside ten minutes. Every
        cycle after the third is the same unresolved incident, not new news.
        """
        results = [self.observe_daemon_cycle(m) for m in DAEMON_RESTART_MINUTES]
        signatures = {r.signature for r in results}
        self.assertEqual(len(signatures), 1, "ten cycles of one fault are one signature")
        entry = self.guard.get(results[-1].signature)
        self.assertEqual(entry["occurrences"], DAEMON_RESTART_CYCLES)
        self.assertEqual(len(entry["escalations"]), 1)
        self.assertEqual(len([r for r in results if r.escalation]), 1)
        self.assertEqual(results[ESCALATION_THRESHOLD - 1].occurrences, ESCALATION_THRESHOLD)
        self.assertTrue(entry["retry_blocked"])
        self.assertEqual(entry["status"], STATUS_ESCALATED)

    def test_recurrence_after_a_correction_opens_a_new_escalation(self):
        """A failure that returns despite a recorded fix genuinely is new information."""
        for minute in range(1, ESCALATION_THRESHOLD + 1):
            last = self.observe_daemon_cycle(minute)
        self.guard.record_corrective_action(
            signature=last.signature,
            kind="code_change",
            description="restored the missing revision file",
            change_ref="fixture-commit-0aaa09c",
            actor="DaemonMigrationRepair",
        )
        after = self.observe_daemon_cycle(4)
        self.assertTrue(after.reopened)
        self.assertIsNotNone(after.escalation)
        self.assertEqual(len(self.guard.get(after.signature)["escalations"]), 2)

    def test_pending_escalations_are_handed_off_once(self):
        for minute in range(1, ESCALATION_THRESHOLD + 1):
            self.observe_daemon_cycle(minute)
        pending = self.guard.pending_escalations()
        self.assertEqual(len(pending), 1)
        self.guard.acknowledge_escalation(pending[0]["escalation_id"], "TelegramRouting")
        self.assertEqual(self.guard.pending_escalations(), [])

    def test_session_is_carried_into_the_escalation_when_supported(self):
        """
        Outbound correlation belongs to the notifier, so the session is only
        attached when the installed contract declares the field.
        """
        import dataclasses

        from telegram_notifier import NotificationEvent

        for minute in range(1, ESCALATION_THRESHOLD + 1):
            last = self.observe_daemon_cycle(minute, session="fixture-session-uuid")
        event = self.guard.get(last.signature)["escalations"][0]["notification_event"]
        supported = "session_id" in {f.name for f in dataclasses.fields(NotificationEvent)}
        self.assertEqual("session_id" in event, supported)
        if supported:
            self.assertEqual(event["session_id"], "fixture-session-uuid")


class TestLedgerIntake(GuardTestCase):
    """A failure must land in the request's durable record without moving its gates."""

    def setUp(self):
        super().setUp()
        self.ledger = RequestLedger(state_dir=self.tmp)
        self.ledger.add_request(
            req_id="req-fixture-daemon",
            prompt="restore the migration graph",
            session="fixture-session",
            project="fixture-org/fixture-repo",
            acceptance_criteria=["containers boot on the exact head"],
            owner="DaemonMigrationRepair",
            task_type="local",
            head="b" * 40,
        )

    def test_first_failure_records_evidence_and_keeps_the_state(self):
        self.observe_daemon_cycle(
            1, request_id="req-fixture-daemon", update_ledger=True, ledger=self.ledger,
            diagnosis="revision absent from the code chain", owner="DaemonMigrationRepair",
            next_action="restore the revision file",
        )
        req = self.ledger.get_request("req-fixture-daemon")
        self.assertEqual(req["state"], "pending")
        self.assertEqual(req["head"], "b" * 40)
        self.assertIsNone(req["blocker"])
        recurrence_evidence = [
            e for e in req["evidence"] if e["type"] == "recurrence_observation"
        ]
        self.assertEqual(len(recurrence_evidence), 1)
        self.assertEqual(recurrence_evidence[0]["recorded_by"], "RecurrenceGuard")

    def test_blocking_recurrence_opens_a_durable_corrective_work_item(self):
        """
        A blocked flag is inert. The second occurrence must leave real work in the
        ledger that the existing coordinator can pick up, with criteria that bind
        closure to the corrective change and the original scenario.
        """
        self.observe_daemon_cycle(1, request_id="req-fixture-daemon",
                                  update_ledger=True, ledger=self.ledger)
        second = self.observe_daemon_cycle(2, request_id="req-fixture-daemon",
                                           update_ledger=True, ledger=self.ledger)
        corrective_id = self.guard.corrective_request_id(second.signature)
        self.assertEqual(
            second.ledger_update["corrective_work_item"],
            {"created": True, "request_id": corrective_id},
        )

        req = self.ledger.get_request("req-fixture-daemon")
        self.assertIn("Unchanged retry is refused", req["blocker"])
        self.assertIn(corrective_id, req["blocker"])
        self.assertIn(corrective_id, req["next_action"])

        work = self.ledger.get_request(corrective_id)
        self.assertEqual(work["state"], "pending")
        self.assertEqual(work["task_type"], "local")
        self.assertFalse(work["deployment_applicable"])
        self.assertEqual(work["authorization"]["status"], "pending")
        self.assertEqual(work["owner"], "DaemonMigrationRepair")
        self.assertIn("type:corrective-action", work["superboard"]["labels"])
        self.assertIn("req-fixture-daemon", work["prompt"])
        self.assertIn("check_alembic_drift.py", work["prompt"])
        descriptions = " ".join(c["description"] for c in work["acceptance_criteria"])
        self.assertIn("systemic change is implemented and referenced", descriptions)
        self.assertIn("original failure scenario is re-executed", descriptions)

        # It is the coordinator's next eligible work, not a note.
        self.assertIn(corrective_id, [r["id"] for r in self.ledger.list_requests()])

        # Idempotent: a third blocking observation does not open a duplicate.
        third = self.observe_daemon_cycle(3, request_id="req-fixture-daemon",
                                          update_ledger=True, ledger=self.ledger)
        self.assertEqual(
            third.ledger_update["corrective_work_item"],
            {"created": False, "request_id": corrective_id, "reason": "already open"},
        )

    def test_duplicate_ingestion_writes_no_further_evidence(self):
        self.observe_daemon_cycle(1, request_id="req-fixture-daemon",
                                  update_ledger=True, ledger=self.ledger)
        self.observe_daemon_cycle(1, request_id="req-fixture-daemon",
                                  update_ledger=True, ledger=self.ledger)
        req = self.ledger.get_request("req-fixture-daemon")
        self.assertEqual(
            len([e for e in req["evidence"] if e["type"] == "recurrence_observation"]), 1
        )

    def test_request_absent_from_the_ledger_keeps_the_recurrence_locally(self):
        result = self.observe_daemon_cycle(
            1, request_id="req-not-in-ledger", update_ledger=True, ledger=self.ledger
        )
        self.assertFalse(result.ledger_update["recorded"])
        self.assertEqual(result.occurrences, 1)

    def test_check_retry_is_addressable_by_request_id(self):
        self.observe_daemon_cycle(1, request_id="req-fixture-daemon",
                                  update_ledger=True, ledger=self.ledger)
        self.observe_daemon_cycle(2, request_id="req-fixture-daemon",
                                  update_ledger=True, ledger=self.ledger)
        self.assertFalse(self.guard.check_retry(request_id="req-fixture-daemon").allowed)
        self.assertTrue(self.guard.check_retry(request_id="req-unrelated").allowed)


class TestPersistenceAcrossProcesses(GuardTestCase):
    """History that does not survive the process that wrote it is not history."""

    def test_cli_escalation_and_gate_survive_separate_processes(self):
        base = ["--state-dir", self.tmp]
        signature = None
        for minute in range(1, ESCALATION_THRESHOLD + 1):
            proc = run_cli(base + [
                "observe",
                "--project", "fixture-org/fixture-repo",
                "--environment", "staging",
                "--operation", "daemon:startup",
                "--error", DAEMON_RESTART_ERROR.format(minute=minute),
                "--source", "deploy",
                "--attempt", f"fixture-restart-cycle-{minute}",
                "--canonical-link", "https://github.com/fixture-org/fixture-repo/issues/4574",
                "--no-ledger-update",
            ])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            signature = payload["signature"]
            self.assertFalse(payload["duplicate"])
            self.assertEqual(payload["occurrences"], minute)

        replay = run_cli(base + [
            "observe",
            "--project", "fixture-org/fixture-repo",
            "--environment", "staging",
            "--operation", "daemon:startup",
            "--error", DAEMON_RESTART_ERROR.format(minute=2),
            "--source", "deploy",
            "--attempt", "fixture-restart-cycle-2",
            "--no-ledger-update",
        ])
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replay_payload = json.loads(replay.stdout)
        self.assertTrue(replay_payload["duplicate"])
        self.assertEqual(replay_payload["occurrences"], ESCALATION_THRESHOLD)
        self.assertIsNone(replay_payload["escalation"])

        gate = run_cli(base + ["check-retry", "--signature", signature])
        self.assertEqual(gate.returncode, 3, gate.stdout)
        self.assertFalse(json.loads(gate.stdout)["allowed"])

        refused = run_cli(base + [
            "record-corrective-action", "--signature", signature,
            "--kind", "deployment", "--description", "redeploy it", "--actor", "lane",
            "--change-ref", "fixture-commit-0aaa09c",
        ])
        self.assertEqual(refused.returncode, 3, refused.stdout)
        self.assertIn("authorization", refused.stderr)

        accepted = run_cli(base + [
            "record-corrective-action", "--signature", signature,
            "--kind", "code_change",
            "--description", "restored the missing revision file on the branch",
            "--change-ref", "fixture-commit-0aaa09c",
            "--actor", "DaemonMigrationRepair",
        ])
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        reopened = run_cli(base + ["check-retry", "--signature", signature])
        self.assertEqual(reopened.returncode, 0, reopened.stdout)

        escalations = run_cli(base + ["escalations"])
        self.assertEqual(escalations.returncode, 0, escalations.stderr)
        self.assertEqual(len(json.loads(escalations.stdout)), 1)

        shown = run_cli(base + ["show", "--signature", signature])
        self.assertEqual(shown.returncode, 0, shown.stderr)
        entry = json.loads(shown.stdout)
        self.assertEqual(entry["occurrences"], ESCALATION_THRESHOLD)
        self.assertEqual(
            [h["event"] for h in entry["history"]][-1], "corrective_action_recorded"
        )

    def test_status_is_rederived_from_history_not_trusted_from_disk(self):
        """A tampered status must not be able to open the gate it is supposed to close."""
        self.observe_daemon_cycle(1)
        second = self.observe_daemon_cycle(2)
        with open(self.guard.store_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data["signatures"][second.signature]["status"] = STATUS_RESOLVED
        data["signatures"][second.signature]["retry_blocked"] = False
        with open(self.guard.store_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        self.assertFalse(self.guard.check_retry(signature=second.signature).allowed)


class TestLedgerCriteriaIntake(unittest.TestCase):
    """
    The observed intake bug: `--criteria '["a","b"]'` crashed with
    "'str' object has no attribute 'get'" on input the CLI's own help documents.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recurrence_criteria_test_")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, criteria, req_id="req-crit"):
        return subprocess.run(
            [
                sys.executable, LEDGER_CLI, "--state-dir", self.tmp, "add",
                "--id", req_id, "--prompt", "p", "--session", "s",
                "--project", "fixture-org/fixture-repo", "--criteria", criteria,
                "--owner", "lane", "--task-type", "local",
            ],
            capture_output=True, text=True, cwd=SCRIPT_DIR,
        )

    def test_json_array_of_description_strings_is_accepted(self):
        proc = self._add('["first criterion","second criterion"]')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        req = RequestLedger(state_dir=self.tmp).get_request("req-crit")
        self.assertEqual(
            [(c["id"], c["description"]) for c in req["acceptance_criteria"]],
            [("AC-1", "first criterion"), ("AC-2", "second criterion")],
        )

    def test_criterion_alias_keeps_its_description(self):
        """The durable bug intake writes 'criterion'; it must not store a blank."""
        normalized = normalize_acceptance_criteria(
            [{"criterion": "Original reproduction scenario proven absent"}]
        )
        self.assertEqual(
            normalized[0]["description"], "Original reproduction scenario proven absent"
        )

    def test_unusable_entry_names_its_position_and_type(self):
        proc = self._add("[123]")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Acceptance criterion 1", proc.stderr)
        self.assertIn("int", proc.stderr)
        self.assertNotIn("has no attribute", proc.stderr)

    def test_malformed_json_reports_that_it_was_read_as_json(self):
        proc = self._add("[oops")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not valid JSON", proc.stderr)

    def test_rejected_criteria_leave_no_partial_request(self):
        self.assertEqual(self._add("[123]", req_id="req-bad").returncode, 1)
        ledger = RequestLedger(state_dir=self.tmp)
        with self.assertRaises(KeyError):
            ledger.get_request("req-bad")

    def test_verified_criterion_binds_to_the_head(self):
        normalized = normalize_acceptance_criteria(
            [{"description": "d", "status": "verified"}], head="c" * 40
        )
        self.assertEqual(normalized[0]["verified_head"], "c" * 40)
        pending = normalize_acceptance_criteria([{"description": "d"}], head="c" * 40)
        self.assertIsNone(pending[0]["verified_head"])


class TestIntendedFailuresAreNotRecurrence(GuardTestCase):
    """
    An executed failure that was supposed to fail is evidence, not a fault.

    Observed case: a mutation probe exited 1 on purpose because an existing test
    wrongly passed. The misleading source assertion was removed and the real CI
    command then reported 50 passed. Had the probe's intended exit 1 been ingested
    as an unexpected failure, re-running it would have driven a retry and
    escalation loop off the suite's own output.
    """

    def _probe(self, run, **kwargs):
        params = dict(
            project="fixture-org/fixture-repo",
            environment="ci",
            operation="ci:migration-safety-mutation-probe",
            error=MUTATION_PROBE_ERROR,
            source="ci",
            attempt=f"fixture-probe-run-{run}",
            disposition="expected_negative_control",
            update_ledger=False,
        )
        params.update(kwargs)
        return self.guard.observe(**params)

    def test_repeated_negative_control_never_blocks_or_escalates(self):
        results = [self._probe(run) for run in range(1, 6)]
        entry = self.guard.get(results[-1].signature)
        self.assertEqual(entry["occurrences"], 0)
        self.assertEqual(entry["observations_recorded"], 5)
        self.assertEqual(entry["observations_retained_uncounted"], 5)
        self.assertEqual(entry["status"], STATUS_OPEN)
        self.assertFalse(entry["retry_blocked"])
        self.assertEqual(entry["escalations"], [])
        self.assertTrue(all(r.retry_allowed for r in results))
        self.assertTrue(all(not r.counted for r in results))

    def test_retained_failures_stay_in_history_verbatim(self):
        """Excluded from the count is not excluded from the record."""
        result = self._probe(1)
        entry = self.guard.get(result.signature)
        observation = entry["observations"][0]
        self.assertEqual(observation["disposition"], "expected_negative_control")
        self.assertFalse(observation["counted"])
        self.assertIn("REPRODUCED", observation["error"])
        self.assertEqual(entry["history"][0]["event"], "retained_expected_negative_control")

    def test_a_retained_failure_does_not_reopen_a_corrected_signature(self):
        counted = self._probe(1, disposition="unexpected")
        self._probe(2, disposition="unexpected")
        self.guard.record_corrective_action(
            signature=counted.signature,
            kind="test_added",
            description="removed the misleading source assertion so the guard asserts the command",
            change_ref="fixture-commit-b290a48",
            actor="lane",
        )
        retained = self._probe(3)
        self.assertFalse(retained.reopened)
        self.assertTrue(retained.retry_allowed)
        self.assertEqual(self.guard.get(counted.signature)["reopened_count"], 0)

    def test_unknown_disposition_is_refused(self):
        with self.assertRaises(RecurrenceGuardError):
            self._probe(1, disposition="probably_fine")

    def test_superseding_retains_the_failure_and_reopens_the_gate(self):
        first = self._probe(1, disposition="unexpected")
        second = self._probe(2, disposition="unexpected")
        self.assertFalse(second.retry_allowed)

        out = self.guard.supersede_observation(
            observation_id=second.observation_id,
            disposition="superseded_attempt",
            reason=(
                "the probe's exit 1 was the intended negative control; the real CI command "
                "reports 50 passed after the misleading assertion was removed"
            ),
            actor="RecurrenceGuardTest",
        )
        self.assertEqual(out["occurrences"], 1)
        self.assertEqual(out["observations_recorded"], 2)
        self.assertTrue(out["retry_allowed"])

        entry = self.guard.get(first.signature)
        retained = next(
            o for o in entry["observations"] if o["observation_id"] == second.observation_id
        )
        self.assertIn("REPRODUCED", retained["error"])
        self.assertEqual(retained["superseded"]["from_disposition"], "unexpected")
        self.assertEqual(retained["superseded"]["actor"], "RecurrenceGuardTest")
        self.assertEqual(entry["history"][-1]["event"], "observation_superseded")

    def test_superseding_cannot_manufacture_an_occurrence(self):
        first = self._probe(1)
        with self.assertRaises(RecurrenceGuardError):
            self.guard.supersede_observation(
                observation_id=first.observation_id,
                disposition="unexpected",
                reason="it really was broken",
                actor="lane",
            )
        self.assertEqual(self.guard.get(first.signature)["occurrences"], 0)

    def test_superseding_requires_a_reason_an_actor_and_a_real_observation(self):
        first = self._probe(1, disposition="unexpected")
        for kwargs in (
            {"reason": "   ", "actor": "lane"},
            {"reason": "ok", "actor": ""},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(RecurrenceGuardError):
                    self.guard.supersede_observation(
                        observation_id=first.observation_id,
                        disposition="superseded_attempt",
                        **kwargs,
                    )
        with self.assertRaises(RecurrenceGuardError):
            self.guard.supersede_observation(
                observation_id="obs-does-not-exist",
                disposition="superseded_attempt",
                reason="ok",
                actor="lane",
            )

    def test_cli_retains_intended_failures_without_closing_the_gate(self):
        observation_ids = []
        signature = None
        for run in range(1, 4):
            proc = run_cli([
                "--state-dir", self.tmp, "observe",
                "--project", "fixture-org/fixture-repo",
                "--environment", "ci",
                "--operation", "ci:migration-safety-mutation-probe",
                "--error", MUTATION_PROBE_ERROR,
                "--source", "ci",
                "--attempt", f"fixture-probe-run-{run}",
                "--disposition", "expected_negative_control",
                "--no-ledger-update",
            ])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["occurrences"], 0)
            self.assertFalse(payload["counted"])
            self.assertIsNone(payload["escalation"])
            observation_ids.append(payload["observation_id"])
            signature = payload["signature"]
        self.assertEqual(len(set(observation_ids)), 3, "three real runs, three observations")

        gate = run_cli(["--state-dir", self.tmp, "check-retry", "--signature", signature])
        self.assertEqual(gate.returncode, 0, gate.stdout)

        already_retained = run_cli([
            "--state-dir", self.tmp, "supersede-observation",
            "--observation-id", observation_ids[0],
            "--reason", "already retained as an intended negative control",
            "--actor", "lane",
            "--disposition", "expected_negative_control",
        ])
        self.assertEqual(already_retained.returncode, 0, already_retained.stderr)
        self.assertEqual(json.loads(already_retained.stdout)["occurrences"], 0)


def git(repo, *args):
    subprocess.run(["git"] + list(args), cwd=repo, check=True, capture_output=True, text=True)


def make_repo(path):
    """A real single-commit git repository, because head binding is enforced for real."""
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q")
    git(path, "config", "user.email", "fixture@example.invalid")
    git(path, "config", "user.name", "fixture")
    with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("fixture\n")
    git(path, "add", "README.md")
    git(path, "commit", "-q", "-m", "fixture")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    )
    return head.stdout.strip()


class TestNativeWorkerIntake(unittest.TestCase):
    """
    The real native worker seam: an authentic failed completion is remembered, and
    the second one stops the backend from preparing another identical attempt.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recurrence_worker_test_")
        self.addCleanup(self._cleanup)
        self.repo = os.path.join(self.tmp, "repo")
        self.head = make_repo(self.repo)
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.state_dir, exist_ok=True)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _backend(self):
        from worker_backend import WorkerBackend

        return WorkerBackend(state_dir=self.state_dir)

    def _fail_once(self, backend, request):
        """Prepare, bind and complete one attempt with an honest failing verdict."""
        ticket = backend.prepare_native(request)
        self.assertIsNone(ticket.blocked_reason, ticket.blocked_reason)
        backend.record_native_dispatch(ticket.run_id, f"agent://{ticket.run_id}")
        outcome = backend.complete_native(
            ticket.run_id,
            f"agent://{ticket.run_id}",
            {
                "stage": "review",
                "request_id": request["request_id"],
                "head_sha": self.head,
                "verdict": "fail",
                "summary": "REQUEST-CHANGES for F1/F2 on the exact head",
                "checks": [
                    {
                        "name": "code trace",
                        "command": ["rg", "MarketsGrid"],
                        "exit_code": 0,
                        "observed": "fallback grid injects unrelated hot groups",
                    }
                ],
                "artifacts": [],
            },
        )
        self.assertFalse(outcome.ok)
        return ticket.run_id, outcome

    def test_failed_completion_is_observed_and_second_one_refuses_retry(self):
        request = {
            "request_id": "req-fixture-market-qa",
            "stage": "review",
            "repo_root": self.repo,
            "head_sha": self.head,
            "prompt": "review the exact head",
            "criteria": ["no newly reachable defects"],
        }
        backend = self._backend()
        first_run, _ = self._fail_once(backend, request)

        guard = RecurrenceGuard(state_dir=self.state_dir)
        signatures = guard.list_signatures()
        self.assertEqual(len(signatures), 1, signatures)
        self.assertEqual(signatures[0]["occurrences"], 1)
        self.assertEqual(signatures[0]["operation"], "worker:review")

        # A replayed completion of the same run is the same event, not a recurrence.
        replay = backend.complete_native(
            first_run, f"agent://{first_run}", {"stage": "review", "verdict": "fail"}
        )
        self.assertFalse(replay.ok)
        self.assertEqual(guard.list_signatures()[0]["occurrences"], 1)

        # One occurrence permits a fresh attempt.
        retry = backend.retry_native(first_run)
        self.assertEqual(retry.state, "prepared")
        backend.record_native_dispatch(retry.run_id, f"agent://{retry.run_id}")
        second = backend.complete_native(
            retry.run_id,
            f"agent://{retry.run_id}",
            {
                "stage": "review",
                "request_id": "req-fixture-market-qa",
                "head_sha": self.head,
                "verdict": "fail",
                "summary": "REQUEST-CHANGES for F1/F2 on the exact head",
                "checks": [
                    {
                        "name": "code trace",
                        "command": ["rg", "MarketsGrid"],
                        "exit_code": 0,
                        "observed": "fallback grid injects unrelated hot groups",
                    }
                ],
                "artifacts": [],
            },
        )
        self.assertFalse(second.ok)
        state = guard.list_signatures()[0]
        self.assertEqual(state["occurrences"], 2)
        self.assertEqual(state["status"], STATUS_CORRECTIVE_ACTION_REQUIRED)

        # The second identical failure is where blind retry stops.
        from worker_backend import WorkerBackendError

        with self.assertRaises(WorkerBackendError) as ctx:
            backend.retry_native(retry.run_id)
        self.assertIn("will not be retried unchanged", str(ctx.exception))

        # A recorded systemic corrective action is what reopens it.
        guard.record_corrective_action(
            signature=state["signature"],
            kind="code_change",
            description="restored the events-list fallback path the revert removed",
            change_ref="fixture-commit-b30670b",
            actor="RecurrenceGuardTest",
        )
        reopened = backend.retry_native(retry.run_id)
        self.assertEqual(reopened.state, "prepared")

    def test_successful_completion_records_no_recurrence(self):
        request = {
            "request_id": "req-fixture-ok",
            "stage": "review",
            "repo_root": self.repo,
            "head_sha": self.head,
            "prompt": "review the exact head",
            "criteria": ["looks right"],
        }
        backend = self._backend()
        ticket = backend.prepare_native(request)
        backend.record_native_dispatch(ticket.run_id, f"agent://{ticket.run_id}")
        outcome = backend.complete_native(
            ticket.run_id,
            f"agent://{ticket.run_id}",
            {
                "stage": "review",
                "request_id": "req-fixture-ok",
                "head_sha": self.head,
                "verdict": "pass",
                "summary": "approved on the exact head",
                "checks": [
                    {
                        "name": "diff read",
                        "command": ["git", "show", "--stat", "HEAD"],
                        "exit_code": 0,
                        "observed": "one file, additive",
                    }
                ],
                "artifacts": [],
            },
        )
        self.assertTrue(outcome.ok, outcome.blocked_reason)
        self.assertEqual(RecurrenceGuard(state_dir=self.state_dir).list_signatures(), [])


class BlockedAdapter:
    """
    Duck-typed adapter on the driver's documented integration surface.

    It always reports the same blocker and writes the same real ledger evidence,
    which is exactly the shape of a step that would otherwise be retried forever.
    """

    def __init__(self, ledger, state_dir, repo_root):
        self.ledger = ledger
        self.state_dir = state_dir
        self.repo_root = repo_root
        self.calls = 0

    def run_step(self, request_id=None, real_worker=False, target_sha=None):
        self.calls += 1
        self.ledger.update_request(
            request_id,
            add_evidence={
                "type": "automated",
                "summary": f"dispatch attempt {self.calls} blocked",
            },
            actor="BlockedAdapter",
            reason="attempt",
        )
        return FakeAdapterResult(
            status="blocked",
            reason=(
                "Preflight gate blocked request: staging alembic drift check exited 2 "
                "(DB has revision not in code chain)"
            ),
        )


class FakeAdapterResult:
    def __init__(self, status, reason):
        self.status = status
        self.status_reason = reason
        self.head_sha = None
        self.worker_result = None
        self.boundaries = {"auto_merge_allowed": False, "auto_deploy_allowed": False}


class TestDriverRecurrenceGate(unittest.TestCase):
    """
    The driver must stop repeating a step that already failed twice for one
    reason, and must stop it *before* spending a dispatch.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recurrence_driver_test_")
        self.addCleanup(self._cleanup)
        self.repo = os.path.join(self.tmp, "repo")
        make_repo(self.repo)
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.ledger = RequestLedger(state_dir=self.state_dir)
        self.ledger.add_request(
            req_id="req-fixture-driver",
            prompt="restore the migration graph",
            session="fixture-session",
            project="fixture-org/fixture-repo",
            acceptance_criteria=["containers boot on the exact head"],
            owner="DaemonMigrationRepair",
            state="implementation",
            task_type="local",
        )
        self.guard = RecurrenceGuard(state_dir=self.state_dir)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        """One driver run, with its own adapter and its own journal read from disk."""
        from continuation_driver import ContinuationDriver, DriverJournal, JOURNAL_FILENAME

        adapter = BlockedAdapter(self.ledger, self.state_dir, self.repo)
        driver = ContinuationDriver(
            adapter=adapter,
            authorized_ids=["req-fixture-driver"],
            state_dir=self.state_dir,
            max_steps=4,
            install_signal_handlers=False,
        )
        outcome = driver.run()
        journal = DriverJournal(os.path.join(self.state_dir, JOURNAL_FILENAME))
        journal.unpark("req-fixture-driver")
        journal.save()
        return outcome, adapter

    def test_third_run_refuses_to_dispatch_and_says_why(self):
        first, adapter_one = self._run()
        self.assertEqual(adapter_one.calls, 1)
        self.assertEqual([p["reason_code"] for p in first.parked], ["blocked"])
        self.assertEqual(self.guard.list_signatures()[0]["occurrences"], 1)

        second, adapter_two = self._run()
        self.assertEqual(adapter_two.calls, 1)
        self.assertEqual([p["reason_code"] for p in second.parked], ["blocked"])
        state = self.guard.list_signatures()[0]
        self.assertEqual(state["occurrences"], 2)
        self.assertEqual(state["status"], STATUS_CORRECTIVE_ACTION_REQUIRED)

        third, adapter_three = self._run()
        self.assertEqual(adapter_three.calls, 0, "no worker may be spent on a known recurrence")
        self.assertEqual(third.steps_executed, 0)
        self.assertEqual([p["reason_code"] for p in third.parked], ["recurrence_blocked"])
        self.assertIn("Unchanged retry refused", third.parked[0]["reason"])
        self.assertEqual(self.guard.list_signatures()[0]["occurrences"], 2)

        self.guard.record_corrective_action(
            signature=state["signature"],
            kind="config_change",
            description="restored the missing revision file so the drift check passes",
            change_ref="fixture-commit-0aaa09c",
            actor="DaemonMigrationRepair",
        )
        fourth, adapter_four = self._run()
        self.assertEqual(adapter_four.calls, 1, "a recorded corrective action reopens dispatch")
        self.assertEqual(self.guard.list_signatures()[0]["occurrences"], 3)

    def test_blocker_and_evidence_reach_the_request_record(self):
        self._run()
        self._run()
        req = self.ledger.get_request("req-fixture-driver")
        self.assertIn("Unchanged retry is refused", req["blocker"])
        recurrence = [e for e in req["evidence"] if e["type"] == "recurrence_observation"]
        self.assertEqual(len(recurrence), 2)

    def test_escalation_needs_a_correction_that_did_not_hold(self):
        """
        The driver cannot escalate on its own: after the second failure the
        pre-dispatch gate refuses to spend a third dispatch. Escalation is
        therefore reached only when a recorded corrective action failed to hold,
        which is the only case where a third occurrence is real news.
        """
        self.ledger.update_request(
            "req-fixture-driver",
            github_update={"issue_url": "https://github.com/Bavariance/polysimulator/issues/4574"},
            actor="test",
        )
        self._run()
        self._run()
        signature = self.guard.list_signatures()[0]["signature"]

        blocked_run, blocked_adapter = self._run()
        self.assertEqual(blocked_adapter.calls, 0)
        self.assertEqual([p["reason_code"] for p in blocked_run.parked], ["recurrence_blocked"])
        self.assertEqual(self.guard.pending_escalations(), [])

        self.guard.record_corrective_action(
            signature=signature,
            kind="config_change",
            description="restored the missing revision file so the drift check passes",
            change_ref="fixture-commit-0aaa09c",
            actor="DaemonMigrationRepair",
        )
        _, adapter = self._run()
        self.assertEqual(adapter.calls, 1)

        pending = self.guard.pending_escalations()
        self.assertEqual(len(pending), 1)
        event = pending[0]["notification_event"]
        self.assertEqual(
            event["canonical_link"], "https://github.com/Bavariance/polysimulator/issues/4574"
        )
        self.assertEqual(event["request_id"], "req-fixture-driver")
        self.assertEqual(event["metadata"]["occurrences"], ESCALATION_THRESHOLD)
        self.assertEqual(event["metadata"]["reopened_count"], 1)
        self.assertTrue(pending[0]["event_valid"], pending[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
