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

import atexit
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from typing import Dict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
from ledger import (  # noqa: E402
    EXPLICIT_SELECTION_LABEL,
    RequestLedger,
    normalize_acceptance_criteria,
    requires_explicit_selection,
)
from recurrence_guard import (  # noqa: E402
    CORRECTIVE_ACTION_THRESHOLD,
    ESCALATION_THRESHOLD,
    STATUS_CORRECTIVE_ACTION_RECORDED,
    STATUS_CORRECTIVE_ACTION_REQUIRED,
    STATUS_ESCALATED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    OfflineEscalationOutbox,
    RecurrenceGuard,
    RecurrenceGuardError,
    compute_signature,
    error_class,
    normalize_error,
    redact_diagnostic,
    redact_durable,
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

#: A 40-hex commit that exists in no fixture repository. It stands in for the
#: reference a correction *claims*, so a test can prove that an unresolvable one
#: is recorded without opening the gate and a refuted one is refused outright.
FIXTURE_CORRECTIVE_COMMIT = "0aaa09c1b2d34e56f7089a1b2c3d4e5f60718293"


def _later_than_now(seconds=60):
    """An ISO timestamp after every observation a test has recorded by now."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=seconds)
    ).isoformat()


#: Well before any observation a test records, standing in for a decision that was
#: already taken while the failure was happening.
BEFORE_ANY_FAILURE = "2025-01-01T00:00:00+00:00"

#: One real repository with the real commits a correction can be verified against,
#: built once for the module.
#:
#: The gate resolves the change reference, binds the exercised proof to it, and
#: relates the whole verification context back to the failure, so a fixture
#: correction cannot be a plausible-looking SHA: it has to be a commit a
#: verification can actually open, on a head that contains it, in a repository that
#: resolves a head the failure was observed on. Building it once keeps 80-odd tests
#: off 80-odd `git init`s.
#:
#: * ``base``   - the head the fixture failures are observed on, when a test needs
#:                the verification context to be relatable to the failure at all;
#: * ``fix``    - a real corrective commit on top of it;
#: * ``sibling``- a real commit that is *not* an ancestor of ``base`` and does not
#:                contain ``fix``, for the "ran on a real head that does not carry
#:                the change" case.
_FIXTURE_REPO: Dict[str, str] = {}


def fixture_corrective_repo():
    """The shared fixture repository and the real commits inside it."""
    if not _FIXTURE_REPO:
        root = tempfile.mkdtemp(prefix="recurrence_fixture_repo_")
        base = make_repo(root)
        _FIXTURE_REPO.update(
            root=root,
            base=base,
            fix=commit_systemic_change(root, "fixture_fix.txt"),
            sibling=commit_systemic_change(root, "fixture_unrelated.txt"),
        )

        def _drop():
            import shutil

            shutil.rmtree(root, ignore_errors=True)

        atexit.register(_drop)
    return _FIXTURE_REPO


def run_cli(args, cwd=None):
    """Run the guard CLI in its own process, the way an operator or script does."""
    proc = subprocess.run(
        [sys.executable, GUARD_CLI] + list(args),
        capture_output=True,
        text=True,
        cwd=cwd or SCRIPT_DIR,
    )
    return proc


def record_fixture_correction(guard, signature, **kwargs):
    """
    Record a corrective action that satisfies the full evidence *and* binding contract.

    Every field the gate demands has a fixture default so a test that is about
    something else does not have to restate them, and a test that is about one of
    them overrides exactly that one. The defaults are deliberately a real commit
    in a real repository, exercised at exit 0 on a head that contains it: that is
    the only shape that opens the gate, so a test that means to open it must use
    a correction that could really have been verified.
    """
    repo = fixture_corrective_repo()
    params = dict(
        signature=signature,
        kind="code_change",
        description="restored the missing revision file so the code chain contains it",
        change_ref=f"commit:{repo['fix']}",
        actor="DaemonMigrationRepair",
        scenario="restart the staging daemon on the fixture container",
        evidence="entrypoint started backend and backend-daemon; drift check exit 0",
        evidence_command=["python", "check_alembic_drift.py"],
        evidence_exit_code=0,
        head_sha=repo["fix"],
        verification_root=repo["root"],
    )
    params.update(kwargs)
    return guard.record_corrective_action(**params)


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recurrence_guard_test_")
        self.addCleanup(self._cleanup)
        self.guard = RecurrenceGuard(state_dir=self.tmp)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def observe_daemon_cycle(self, minute, lineage=None, **kwargs):
        """
        One observed restart cycle of the staging daemon fixture.

        The fixture incident happened *somewhere*, so by default it is recorded on
        the shared fixture repository's real base commit: a correction is only
        bound when the head its proof ran on descends from every head the failure
        was observed on, so a test about anything else still needs a failure that
        has a lineage to descend from. A caller that names its own `head_sha` or
        `repo_root` keeps exactly the shape it named, and `lineage=False` records
        neither, which is the honestly unanchored case - it binds nothing and is
        tested by name.
        """
        params = dict(
            project="fixture-org/fixture-repo",
            environment="staging",
            operation="daemon:startup",
            error=DAEMON_RESTART_ERROR.format(minute=minute),
            source="deploy",
            attempt=f"fixture-restart-cycle-{minute}",
            update_ledger=False,
        )
        if lineage is None:
            lineage = not ({"head_sha", "repo_root"} & set(kwargs))
        if lineage:
            repo = fixture_corrective_repo()
            params.update(head_sha=repo["base"], repo_root=repo["root"])
        params.update(kwargs)
        return self.guard.observe(**params)

    def correct(self, signature, **kwargs):
        return record_fixture_correction(self.guard, signature, **kwargs)


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
        out = self.correct(second.signature)
        self.assertEqual(out["status"], STATUS_CORRECTIVE_ACTION_RECORDED)
        self.assertTrue(self.guard.check_retry(signature=second.signature).allowed)

    def test_recurrence_after_correction_reopens_and_blocks_again(self):
        """A fix that did not hold is exactly what must not be retried blind."""
        self.observe_daemon_cycle(1)
        second = self.observe_daemon_cycle(2)
        self.correct(second.signature)
        third = self.observe_daemon_cycle(3)
        self.assertTrue(third.reopened)
        self.assertFalse(third.retry_allowed)
        self.assertEqual(self.guard.get(third.signature)["reopened_count"], 1)

    def test_corrective_action_without_a_description_or_change_is_refused(self):
        """A gate cleared by a blank claim, or by 'retry later', is worse than no gate."""
        sig = self.observe_daemon_cycle(1).signature
        self.observe_daemon_cycle(2)
        for kwargs in (
            {"description": "   "},
            {"change_ref": None},
            {"change_ref": "  "},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(RecurrenceGuardError):
                    self.correct(sig, actor="someone", **kwargs)
        self.assertFalse(self.guard.check_retry(signature=sig).allowed)

    def test_arbitrary_change_ref_text_does_not_clear_the_gate(self):
        """
        The reviewed defect, exactly: `--change-ref later` with the description
        "will retry later" moved the signature to corrective_action_recorded and
        answered check-retry allowed. 'Retry later' is the behaviour this module
        exists to stop, so it must not be expressible as a correction.
        """
        sig = self.observe_daemon_cycle(1).signature
        self.observe_daemon_cycle(2)
        self.assertFalse(self.guard.check_retry(signature=sig).allowed)
        for change_ref in ("later", "PR-1", "see slack", "0aaa09c", "commit:0aaa09c"):
            with self.subTest(change_ref=change_ref):
                with self.assertRaises(RecurrenceGuardError) as ctx:
                    self.correct(
                        sig, description="will retry later", change_ref=change_ref
                    )
                self.assertIn("verifiable reference", str(ctx.exception))
        entry = self.guard.get(sig)
        self.assertEqual(entry["corrective_actions"], [])
        self.assertEqual(entry["status"], STATUS_CORRECTIVE_ACTION_REQUIRED)
        self.assertFalse(self.guard.check_retry(signature=sig).allowed)

    def test_corrective_action_requires_the_original_scenario_re_executed(self):
        """
        The gate is cleared here, so the exercised proof has to exist here. A
        verifiable change reference with no scenario, no observation, no command or
        no exit code is still an assertion that something was fixed.
        """
        sig = self.observe_daemon_cycle(1).signature
        self.observe_daemon_cycle(2)
        for kwargs in (
            {"scenario": ""},
            {"scenario": "ran it"},
            {"evidence": ""},
            {"evidence": "green"},
            {"evidence_command": []},
            {"evidence_command": ["  "]},
            {"evidence_exit_code": "0"},
            {"evidence_exit_code": None},
            {"head_sha": ""},
            {"head_sha": "0aaa09c"},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(RecurrenceGuardError):
                    self.correct(sig, **kwargs)
        self.assertFalse(self.guard.check_retry(signature=sig).allowed)

    def test_code_change_cannot_point_at_a_commit_that_already_failed(self):
        """
        A "code change" whose commit is one of the observed failing heads changed
        nothing about the tree being retried, so it is the unchanged retry it
        claims to replace.
        """
        failing_head = fixture_corrective_repo()["base"]
        self.observe_daemon_cycle(1, lineage=True)
        second = self.observe_daemon_cycle(2, lineage=True)
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.correct(second.signature, change_ref=f"commit:{failing_head}")
        self.assertIn("carries no systemic change", str(ctx.exception))
        self.assertFalse(self.guard.check_retry(signature=second.signature).allowed)
        # A commit that is not one of them is accepted and does clear the gate.
        self.correct(second.signature)
        self.assertTrue(self.guard.check_retry(signature=second.signature).allowed)

    def test_repository_backed_kinds_must_name_a_commit(self):
        """A code change referenced only by a decision id names no changed tree."""
        sig = self.observe_daemon_cycle(1).signature
        self.observe_daemon_cycle(2)
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.correct(sig, kind="code_change", change_ref="decision:dec-1")
        self.assertIn("must name a commit", str(ctx.exception))
        # A process change legitimately has no commit. A decision reference is a
        # real reference for it, but with no decision record beside this store
        # there is nothing to resolve it against, so it is recorded unverified and
        # the gate stays exactly where the failures put it.
        out = self.correct(sig, kind="process_change", change_ref="decision:dec-1")
        self.assertEqual(out["action"]["change_ref_form"], "decision")
        self.assertFalse(out["action"]["change_ref_verification"]["checked"])
        self.assertTrue(out["recorded"])
        self.assertFalse(out["gate"]["opened"])
        self.assertFalse(self.guard.check_retry(signature=sig).allowed)

    def test_a_commit_reference_is_verified_against_the_observed_repository(self):
        """
        When the repository the failure was observed in is reachable, a change
        reference naming an object that is not in it is refused rather than
        recorded as a correction.
        """
        repo = os.path.join(self.tmp, "repo")
        real_head = make_repo(repo)
        self.observe_daemon_cycle(1, repo_root=repo)
        second = self.observe_daemon_cycle(2, repo_root=repo)
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.correct(
                second.signature,
                change_ref=f"commit:{FIXTURE_CORRECTIVE_COMMIT}",
                verification_root=None,
            )
        self.assertIn("does not exist", str(ctx.exception))
        self.assertFalse(self.guard.check_retry(signature=second.signature).allowed)

        # The correction itself is a real commit made *after* the failures, in the
        # repository they were recorded in: these observations carry no head, so
        # the head the proof ran on is bound to the failure by postdating it.
        corrective = commit_systemic_change(repo, "revision_restored.py")
        out = self.correct(
            second.signature,
            change_ref=f"commit:{corrective}",
            head_sha=corrective,
            verification_root=None,
        )
        self.assertTrue(out["action"]["change_ref_verification"]["checked"])
        self.assertTrue(out["action"]["change_ref_verification"]["exists"])
        self.assertEqual(
            out["action"]["change_ref_verification"]["repo_root"], os.path.abspath(repo)
        )
        self.assertTrue(out["gate"]["opened"])
        self.assertTrue(self.guard.check_retry(signature=second.signature).allowed)

    def test_corrective_action_needs_an_observed_failure(self):
        with self.assertRaises(RecurrenceGuardError):
            self.correct("0" * 64)


class TestSafetyBoundaries(GuardTestCase):
    """The guard records; it never executes, authorizes or verifies."""

    def test_privileged_kinds_require_explicit_authorization(self):
        sig = self.observe_daemon_cycle(1).signature
        self.observe_daemon_cycle(2)
        for kind in ("ddl", "deployment", "privileged_operation", "gate_change"):
            with self.subTest(kind=kind):
                with self.assertRaises(RecurrenceGuardError):
                    self.correct(
                        sig, kind=kind, description="apply it", actor="lane",
                        change_ref="decision:fixture-comment-5543918753",
                    )
        self.assertFalse(self.guard.check_retry(signature=sig).allowed)

    def test_authorized_privileged_action_is_recorded_not_executed(self):
        sig = self.observe_daemon_cycle(1).signature
        self.observe_daemon_cycle(2)
        out = self.correct(
            sig,
            kind="ddl",
            description="operator applied the reviewed downgrade SQL out of band",
            change_ref="decision:fixture-comment-5543918753",
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
        self.correct(result.signature)
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

        self.correct(sig, actor="lane")
        out = self.guard.resolve(
            sig,
            head_sha="a" * 40,
            scenario=scenario,
            evidence="check_alembic_drift.py exited 0 on the fixture database",
            actor="lane",
        )
        self.assertEqual(out["status"], STATUS_RESOLVED)
        self.assertEqual(
            out["resolution"]["change_refs"],
            [f"commit:{fixture_corrective_repo()['fix']}"],
        )
        self.assertEqual(
            out["resolution"]["corrective_evidence"][0]["command"],
            ["python", "check_alembic_drift.py"],
        )
        self.assertEqual(out["resolution"]["scenario"], scenario)
        self.assertIn("No claim of universal absence", out["resolution"]["scope"])

    def test_resolution_is_not_a_permanent_claim(self):
        sig = self.observe_daemon_cycle(1).signature
        self.correct(sig, actor="lane")
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
        # The observed head is transcribed verbatim inside REVIEW_FAIL_ERROR, which
        # is where it authentically appeared. `head_sha` has to be a commit that
        # really exists, because a correction only binds when its verification
        # context resolves a head the failure was observed on, so the fixture
        # repository's real base commit stands in for that exact head.
        repo = fixture_corrective_repo()
        observed_head = repo["base"]
        review = self.guard.observe(
            project="fixture-org/fixture-repo",
            environment="harness",
            operation="worker:review",
            error=REVIEW_FAIL_ERROR,
            source="native_worker",
            head_sha=observed_head,
            repo_root=repo["root"],
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
            head_sha=observed_head,
            repo_root=repo["root"],
            stage="qa",
            attempt="fixture-qa-run-1",
            request_id="req-fixture-market-qa",
            update_ledger=False,
        )
        self.assertNotEqual(review.signature, qa.signature)

        # A QA resolution on the corrected head and same request closes only the
        # QA signature.
        self.correct(
            qa.signature, actor="qa-lane", description="fixed the scoped QA path",
            scenario="the scoped QA scenario that failed on this head",
            evidence="scoped QA scenario passed on the exact head",
            evidence_command=["python", "-m", "pytest", "tests/test_scoped_qa.py"],
            evidence_exit_code=0,
        )
        self.guard.resolve(
            qa.signature,
            head_sha=repo["fix"],
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
            head_sha=observed_head,
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
        self.correct(last.signature)
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
        # The CLI records the failure where it happened, so the corrective action
        # further down has a lineage to descend from - which is the only shape
        # that opens the gate through the CLI an operator actually runs.
        fixture = fixture_corrective_repo()
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
                "--head-sha", fixture["base"],
                "--repo-root", fixture["root"],
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
            "--head-sha", fixture["base"],
            "--repo-root", fixture["root"],
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

        repo = fixture_corrective_repo()
        evidence_args = [
            "--scenario", "restart the staging daemon on the fixture container",
            "--evidence", "entrypoint started both services; drift check exit 0",
            "--evidence-command", "python", "check_alembic_drift.py",
            "--evidence-exit-code", "0",
            "--head-sha", repo["fix"],
            "--verify-root", repo["root"],
        ]

        refused = run_cli(base + [
            "record-corrective-action", "--signature", signature,
            "--kind", "deployment", "--description", "redeploy it", "--actor", "lane",
            "--change-ref", "decision:fixture-comment-5543918753",
        ] + evidence_args)
        self.assertEqual(refused.returncode, 3, refused.stdout)
        self.assertIn("authorization", refused.stderr)

        # The reviewed laundering path, through the CLI an operator actually runs.
        laundered = run_cli(base + [
            "record-corrective-action", "--signature", signature,
            "--kind", "process_change", "--description", "will retry later",
            "--actor", "lane", "--change-ref", "later",
        ] + evidence_args)
        self.assertEqual(laundered.returncode, 3, laundered.stdout)
        self.assertIn("verifiable reference", laundered.stderr)
        still_blocked = run_cli(base + ["check-retry", "--signature", signature])
        self.assertEqual(still_blocked.returncode, 3, still_blocked.stdout)

        # The same laundering wearing a reference shape: 'decision:later' with a
        # description that says the quiet part. The record lands, exit 3 says the
        # gate did not open, and the gate really is still closed.
        shaped = run_cli(base + [
            "record-corrective-action", "--signature", signature,
            "--kind", "process_change", "--description", "will retry later",
            "--actor", "lane", "--change-ref", "decision:later",
        ] + evidence_args)
        self.assertEqual(shaped.returncode, 3, shaped.stdout)
        shaped_payload = json.loads(shaped.stdout)
        self.assertTrue(shaped_payload["recorded"])
        self.assertFalse(shaped_payload["gate"]["opened"])
        self.assertEqual(
            run_cli(base + ["check-retry", "--signature", signature]).returncode, 3
        )

        # A commit reference the recorded repository *refutes* is refused outright
        # through the CLI too, so a fabricated SHA never lands as a correction.
        refuted = run_cli(base + [
            "record-corrective-action", "--signature", signature,
            "--kind", "code_change",
            "--description", "restored the missing revision file on the branch",
            "--change-ref", f"commit:{FIXTURE_CORRECTIVE_COMMIT}",
            "--actor", "DaemonMigrationRepair",
            "--scenario", "restart the staging daemon on the fixture container",
            "--evidence", "entrypoint started both services; drift check exit 0",
            "--evidence-command", "python", "check_alembic_drift.py",
            "--evidence-exit-code", "0",
            "--head-sha", FIXTURE_CORRECTIVE_COMMIT,
        ])
        self.assertEqual(refuted.returncode, 3, refuted.stdout)
        self.assertIn("does not exist", refuted.stderr)

        # A reference with nothing to resolve it against - a pull request needs a
        # remote API this module never contacts - is recorded and opens nothing.
        unverifiable = run_cli(base + [
            "record-corrective-action", "--signature", signature,
            "--kind", "process_change",
            "--description", "opened the upstream fix as a pull request",
            "--change-ref", "pr:fixture-org/fixture-repo#4574",
            "--actor", "DaemonMigrationRepair",
        ] + evidence_args)
        self.assertEqual(unverifiable.returncode, 3, unverifiable.stdout)
        self.assertFalse(json.loads(unverifiable.stdout)["gate"]["opened"])
        self.assertEqual(
            run_cli(base + ["check-retry", "--signature", signature]).returncode, 3
        )

        accepted = run_cli(base + [
            "record-corrective-action", "--signature", signature,
            "--kind", "code_change",
            "--description", "restored the missing revision file on the branch",
            "--change-ref", f"commit:{repo['fix']}",
            "--actor", "DaemonMigrationRepair",
        ] + evidence_args)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertTrue(json.loads(accepted.stdout)["gate"]["opened"])

        reopened = run_cli(base + ["check-retry", "--signature", signature])
        self.assertEqual(reopened.returncode, 0, reopened.stdout)

        escalations = run_cli(base + ["escalations"])
        self.assertEqual(escalations.returncode, 0, escalations.stderr)
        self.assertEqual(len(json.loads(escalations.stdout)), 1)

        shown = run_cli(base + ["show", "--signature", signature])
        self.assertEqual(shown.returncode, 0, shown.stderr)
        entry = json.loads(shown.stdout)
        self.assertEqual(entry["occurrences"], ESCALATION_THRESHOLD)
        events = [h["event"] for h in entry["history"]]
        self.assertEqual(events[-1], "corrective_action_recorded")
        self.assertIn("corrective_action_recorded_unbound", events)
        self.assertEqual(entry["corrective_actions_gate_bound"], 1)
        self.assertEqual(entry["corrective_actions_recorded"], 3)

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
        # Recorded on the fixture repository's real base commit: the probe ran
        # somewhere, and a correction only binds when the head its proof ran on
        # descends from the head the failure was observed on.
        repo = fixture_corrective_repo()
        params = dict(
            project="fixture-org/fixture-repo",
            environment="ci",
            operation="ci:migration-safety-mutation-probe",
            error=MUTATION_PROBE_ERROR,
            source="ci",
            attempt=f"fixture-probe-run-{run}",
            disposition="expected_negative_control",
            head_sha=repo["base"],
            repo_root=repo["root"],
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
        self.correct(
            counted.signature,
            kind="test_added",
            change_ref=f"commit:{fixture_corrective_repo()['fix']}",
            description="removed the misleading source assertion so the guard asserts the command",
            actor="lane",
            scenario="run the mutation probe against the corrected guard",
            evidence="probe exited 0: the guard now fails when --noconftest is removed",
            evidence_command=["python", "migration-guard-mutation-repro.py"],
            evidence_exit_code=0,
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


def commit_files(repo, files, message="fixture change"):
    """
    A real commit carrying `files`, without moving HEAD, the index or the working tree.

    Two constraints meet here. A corrective action is now verified against the
    *committed tree* of the head its proof ran on - a working-tree edit is not a
    change any head carries - so a test that legitimately corrects a failure needs
    a real commit holding real content. But the worker's retry is bound to the head
    it failed on, so moving HEAD would make the retry fail for an unrelated reason
    and stop testing the gate.

    So the commit is built with plumbing against a scratch index: the object is
    real, reachable by SHA and a child of HEAD, while HEAD, the index and the
    working tree are exactly as they were - which also means the content the gate
    reads can only have come out of the tree.
    """
    env = dict(os.environ, GIT_INDEX_FILE=os.path.join(repo, ".git", "corrective.index"))

    def plumbing(*args):
        return subprocess.run(
            ["git"] + list(args), cwd=repo, env=env,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    written = []
    try:
        plumbing("read-tree", "HEAD")
        for rel, content in files.items():
            path = os.path.join(repo, *rel.split("/"))
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            written.append(path)
            plumbing("add", rel)
        return plumbing("commit-tree", plumbing("write-tree"), "-p", "HEAD", "-m", message)
    finally:
        for path in written:
            if os.path.exists(path):
                os.remove(path)


def commit_systemic_change(repo, filename="fix.txt"):
    """A real commit standing in for the systemic change, without moving HEAD."""
    return commit_files(
        repo, {filename: "systemic change\n"}, "systemic corrective change"
    )


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

        # A recorded systemic corrective action is what reopens it, and only when
        # its reference is really in this repository and its proof succeeded on a
        # head that carries it.
        corrective_head = commit_systemic_change(self.repo)
        record_fixture_correction(
            guard,
            state["signature"],
            description="restored the events-list fallback path the revert removed",
            actor="RecurrenceGuardTest",
            change_ref=f"commit:{corrective_head}",
            head_sha=corrective_head,
            verification_root=self.repo,
            scenario="re-dispatch the native review stage on the failing head",
            evidence="review stage returned verdict pass with executed checks",
            evidence_command=["python", "-m", "pytest", "tests/test_events_fallback.py"],
            evidence_exit_code=0,
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

    #: The revision file the fixture's corrective config change names. The gate
    #: resolves a `config:<path>#<key>` reference out of the committed tree of the
    #: head the proof ran on, so the file and the key have to be really committed -
    #: which is also what the fix in the observed incident actually was. It is
    #: deliberately left out of the working tree, so the only place the gate can
    #: have read it from is the corrective commit's tree.
    REVISION_FILE = "alembic/versions/20260904_page_views_kind.py"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recurrence_driver_test_")
        self.addCleanup(self._cleanup)
        self.repo = os.path.join(self.tmp, "repo")
        self.head = make_repo(self.repo)
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

    def _commit_correction(self):
        """
        The corrective commit, made when the correction is recorded rather than in
        setUp.

        These failures record no head (the fixture request has none), so the head
        the proof ran on is bound to the failure by postdating it: a tree that
        already existed while the failure was happening cannot be what corrected
        it. Building the commit up front made the fixture assert the opposite.
        """
        return commit_files(
            self.repo,
            {
                self.REVISION_FILE: (
                    'revision = "20260904_page_views_kind"\ndown_revision = None\n'
                )
            },
            "restore the missing alembic revision",
        )

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

        record_fixture_correction(
                    self.guard,
                    state["signature"],
                    kind="config_change",
                    description="restored the missing revision file so the drift check passes",
                    change_ref=f"config:{self.REVISION_FILE}#revision",
                    head_sha=self._commit_correction(),
                    verification_root=self.repo,
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

        record_fixture_correction(
                    self.guard,
                    signature,
                    kind="config_change",
                    description="restored the missing revision file so the drift check passes",
                    change_ref=f"config:{self.REVISION_FILE}#revision",
                    head_sha=self._commit_correction(),
                    verification_root=self.repo,
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


class TestVerdictIdentityAtTheRealWorkerSeam(unittest.TestCase):
    """
    The reviewed efficacy defect, reproduced at the seam it was found at.

    Two attempts at the same defect, with the wording of the agent's summary
    changed the way a real agent changes it, used to produce two signatures at
    occurrence 1 each, so `retry_native` stayed allowed after two identical-fault
    failures and the gate only closed for a byte-identical replay. These tests
    pin that the fault identity is the structured verdict, not the prose.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recurrence_verdict_test_")
        self.addCleanup(self._cleanup)
        self.repo = os.path.join(self.tmp, "repo")
        self.head = make_repo(self.repo)
        self.state_dir = os.path.join(self.tmp, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.ledger = RequestLedger(state_dir=self.state_dir)
        self.ledger.add_request(
            req_id="req-fixture-seam",
            prompt="review the exact head",
            session="fixture-session",
            project="fixture-org/fixture-repo",
            acceptance_criteria=["review approves the exact head"],
            owner="ReviewLane",
            state="review",
            task_type="local",
        )
        from worker_backend import WorkerBackend

        self.backend = WorkerBackend(state_dir=self.state_dir)
        self.guard = RecurrenceGuard(state_dir=self.state_dir)
        self.request = {
            "request_id": "req-fixture-seam",
            "stage": "review",
            "repo_root": self.repo,
            "head_sha": self.head,
            "prompt": "review the exact head",
            "criteria": ["review approves the exact head"],
        }

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fail(self, run_id, summary):
        self.backend.record_native_dispatch(run_id, f"agent://{run_id}")
        return self.backend.complete_native(
            run_id,
            f"agent://{run_id}",
            {
                "stage": "review",
                "request_id": "req-fixture-seam",
                "head_sha": self.head,
                "verdict": "fail",
                "summary": summary,
                "checks": [
                    {
                        "name": "code trace",
                        "command": ["git", "show", self.head],
                        "exit_code": 0,
                        "observed": "F1 fallback grid still injects unrelated hot groups",
                    }
                ],
                "artifacts": [],
            },
        )

    def test_reworded_summaries_for_one_fault_close_the_retry_gate(self):
        from worker_backend import WorkerBackendError

        first = self.backend.prepare_native(self.request)
        self._fail(
            first.run_id,
            "REQUEST-CHANGES: F1 fallback grid injects unrelated hot groups on exact head",
        )
        signatures = self.guard.list_signatures()
        self.assertEqual(len(signatures), 1)
        self.assertEqual(signatures[0]["occurrences"], 1)

        second = self.backend.retry_native(first.run_id)
        self._fail(
            second.run_id,
            "Fail. Unrelated hot groups are still injected by the fallback grid (F1 unchanged).",
        )
        signatures = self.guard.list_signatures()
        self.assertEqual(
            len(signatures), 1,
            "a reworded summary for the same fault must not open a second signature",
        )
        self.assertEqual(signatures[0]["occurrences"], 2)
        self.assertEqual(signatures[0]["status"], STATUS_CORRECTIVE_ACTION_REQUIRED)

        with self.assertRaises(WorkerBackendError) as ctx:
            self.backend.retry_native(second.run_id)
        self.assertIn("will not be retried unchanged", str(ctx.exception))

    def test_a_different_verdict_is_a_different_fault(self):
        """
        Collapsing prose must not collapse genuinely different outcomes: a
        `blocked` verdict is not the `fail` verdict, and merging them would invent
        a recurrence that never happened.
        """
        first = self.backend.prepare_native(self.request)
        self._fail(first.run_id, "REQUEST-CHANGES: F1 is present")
        second = self.backend.retry_native(first.run_id)
        self.backend.record_native_dispatch(second.run_id, f"agent://{second.run_id}")
        self.backend.complete_native(
            second.run_id,
            f"agent://{second.run_id}",
            {
                "stage": "review",
                "request_id": "req-fixture-seam",
                "head_sha": self.head,
                "verdict": "blocked",
                "summary": "Cannot review: the PR diff endpoint is unreachable",
                "checks": [],
                "artifacts": [],
            },
        )
        classes = sorted(s["error_class"] for s in self.guard.list_signatures())
        self.assertEqual(len(classes), 2, classes)
        self.assertTrue(all(c.startswith("worker_verdict:review:") for c in classes))
        self.assertTrue(all(s["occurrences"] == 1 for s in self.guard.list_signatures()))

    def test_one_native_failure_is_one_occurrence_across_worker_and_driver(self):
        """
        The reviewed truthfulness defect: the adapter's blocked result carried no
        attempt identity, so the driver recorded the worker's failure a second time
        under `driver:review` and the request collected two recurrence evidence
        rows for one failure.
        """
        from continuation_driver import JOURNAL_FILENAME, ContinuationDriver
        from superboard_adapter import WorkerExecutionResult

        first = self.backend.prepare_native(self.request)
        self._fail(first.run_id, "REQUEST-CHANGES: F1 is present")

        backend, request = self.backend, self.request

        class TerminalTicketAdapter:
            """
            The adapter's real native path for a terminal ticket: prepare returns
            the completed attempt, its outcome is translated, and the blocked
            result carries the attempt identity the backend recorded.
            """

            def __init__(self, repo_root):
                self.repo_root = repo_root
                self.calls = 0

            def run_step(self, request_id=None, real_worker=False, target_sha=None):
                self.calls += 1
                ticket = backend.prepare_native(request)
                assert ticket.state in ("finalized", "blocked"), ticket.state
                outcome = backend.get_native_outcome(ticket.run_id)
                worker_res = WorkerExecutionResult(
                    stage="review",
                    exit_code=1,
                    output=f"[DISPATCH_BLOCKED] {outcome.blocked_reason}",
                    head_sha=outcome.head_sha,
                    native_run_id=ticket.run_id,
                    blocked_reason=outcome.blocked_reason,
                    evidence=dict(outcome.evidence or {}),
                )
                result = FakeAdapterResult("blocked", outcome.blocked_reason)
                result.head_sha = outcome.head_sha
                result.worker_result = worker_res
                return result

        journal_path = os.path.join(self.state_dir, JOURNAL_FILENAME)
        adapters = []
        for _ in range(3):
            # A lost journal is the harder case: the driver has no memory of the
            # dispatch, so nothing but the attempt's own identity can stop the
            # re-read from counting.
            if os.path.exists(journal_path):
                os.remove(journal_path)
            adapter = TerminalTicketAdapter(self.repo)
            adapters.append(adapter)
            ContinuationDriver(
                adapter=adapter,
                authorized_ids=["req-fixture-seam"],
                state_dir=self.state_dir,
                max_steps=2,
                install_signal_handlers=False,
            ).run()

        self.assertEqual([a.calls for a in adapters], [1, 1, 1])
        signatures = self.guard.list_signatures()
        self.assertEqual(
            [(s["operation"], s["occurrences"]) for s in signatures],
            [("worker:review", 1)],
            "three re-reads of one completed attempt are one occurrence",
        )
        self.assertTrue(self.guard.check_retry(request_id="req-fixture-seam").allowed)
        self.assertEqual(self.guard.pending_escalations(), [])
        req = self.ledger.get_request("req-fixture-seam")
        rows = [e for e in req["evidence"] if e["type"] == "recurrence_observation"]
        self.assertEqual(len(rows), 1, "one failure, one evidence row")
        with self.assertRaises(KeyError):
            self.ledger.get_request(
                self.guard.corrective_request_id(signatures[0]["signature"])
            )

    def test_a_failure_with_no_attempt_behind_it_is_still_the_drivers_own(self):
        """
        Deferring to the attempt identity must not silence a failure that has no
        attempt: an adapter that raises produced no attempt, so the observation is
        the driver's own and two dispatches of it are two occurrences that close
        the gate. Its identity is the journal's dispatch ordinal, which is why the
        journal is preserved here and the request is unparked between runs exactly
        as an operator resuming a parked request does.
        """
        from continuation_driver import ContinuationDriver, DriverJournal, JOURNAL_FILENAME

        class RaisingAdapter:
            def __init__(self, repo_root):
                self.repo_root = repo_root

            def run_step(self, request_id=None, real_worker=False, target_sha=None):
                raise RuntimeError("preflight probe socket refused")

        for _ in range(2):
            ContinuationDriver(
                adapter=RaisingAdapter(self.repo),
                authorized_ids=["req-fixture-seam"],
                state_dir=self.state_dir,
                max_steps=2,
                install_signal_handlers=False,
            ).run()
            journal = DriverJournal(os.path.join(self.state_dir, JOURNAL_FILENAME))
            journal.unpark("req-fixture-seam")
            journal.save()

        signatures = self.guard.list_signatures()
        self.assertEqual([s["operation"] for s in signatures], ["driver:review"])
        self.assertEqual(signatures[0]["occurrences"], 2)
        self.assertFalse(self.guard.check_retry(request_id="req-fixture-seam").allowed)


#: A sanitized transcription of the observed failure reason that carried
#: credentials. The values are fabricated for the fixture; the *shapes* are the
#: ones that were persisted verbatim in the reviewed head.
CREDENTIAL_FAILURE_ERROR = (
    "psycopg2.OperationalError: connection to "
    "postgresql://svc:supers3cretpw@db.internal:5432/app failed; "
    "token=ghp_abcdefghijklmnopqrstuvwxyz012345; "
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzdmMifQ.c2lnbmF0dXJl"
)
#: Every substring that must not survive anywhere durable.
CREDENTIAL_SECRETS = (
    "supers3cretpw",
    "ghp_abcdefghijklmnopqrstuvwxyz012345",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzdmMifQ.c2lnbmF0dXJl",
)


class TestDiagnosticsAreRedactedBeforePersistence(unittest.TestCase):
    """
    The reviewed privacy defect: a DSN password and a GitHub token in one failure
    reason were written to the store verbatim (error_sample, observations[].error,
    normalized_error) and 600 characters of that text were copied into the
    corrective work item's prompt. Redaction has to happen before the first write,
    not at each place the text is later read.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recurrence_redaction_test_")
        self.addCleanup(self._cleanup)
        self.guard = RecurrenceGuard(state_dir=self.tmp)
        self.ledger = RequestLedger(state_dir=self.tmp)
        self.ledger.add_request(
            req_id="req-fixture-secret",
            prompt="apply the migration",
            session="fixture-session",
            project="fixture-org/fixture-repo",
            acceptance_criteria=["migration applies on the exact head"],
            owner="MigrationLane",
            task_type="local",
        )

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _observe(self, attempt):
        return self.guard.observe(
            project="fixture-org/fixture-repo",
            environment="staging",
            operation="deploy:migrate",
            error=CREDENTIAL_FAILURE_ERROR,
            source="deploy",
            request_id="req-fixture-secret",
            attempt=attempt,
            # A named assignment, which is how a credential actually appears in a
            # diagnosis. Redaction is pattern-based and deliberately claims nothing
            # about a bare secret written as an unlabelled word in prose: there is
            # no way to tell that from any other token, and pretending otherwise
            # would be exactly the false guarantee this module refuses to make.
            diagnosis=f"the service DSN is wrong (password={CREDENTIAL_SECRETS[0]})",
            owner="MigrationLane",
            next_action="rotate the credential and re-run",
            ledger=self.ledger,
        )

    def assert_no_secret_in(self, blob, where):
        for secret in CREDENTIAL_SECRETS:
            self.assertNotIn(secret, blob, f"{secret!r} survived in {where}")

    def test_the_store_on_disk_holds_no_credential(self):
        self._observe("fixture-migrate-1")
        with open(self.guard.store_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.assert_no_secret_in(raw, "the recurrence store file")
        self.assertIn("<redacted-password>", raw)
        self.assertIn("token=<redacted>", raw)
        # The JWT arrives inside an Authorization header, so the bearer pattern
        # claims it first; the bare-JWT pattern covers one that arrives alone.
        self.assertIn("Bearer <redacted>", raw)
        self.assertIn("<redacted-jwt>", redact_diagnostic(CREDENTIAL_SECRETS[2]))

    def test_the_diagnosis_and_error_sample_are_redacted(self):
        result = self._observe("fixture-migrate-1")
        entry = self.guard.get(result.signature)
        self.assert_no_secret_in(entry["error_sample"], "error_sample")
        self.assert_no_secret_in(entry["normalized_error"], "normalized_error")
        self.assert_no_secret_in(entry["diagnosis"], "diagnosis")
        self.assert_no_secret_in(
            json.dumps(entry["observations"], default=str), "observations"
        )
        # The identity still separates faults: the redacted text is not empty and
        # the class is derived from it.
        self.assertIn("operationalerror", entry["normalized_error"])
        self.assertTrue(entry["error_class"].startswith("auto:"))

    def test_redaction_is_stable_so_the_signature_does_not_drift(self):
        first = self._observe("fixture-migrate-1")
        second = self._observe("fixture-migrate-2")
        self.assertEqual(first.signature, second.signature)
        self.assertEqual(second.occurrences, 2)

    def test_the_corrective_work_item_prompt_holds_no_credential(self):
        self._observe("fixture-migrate-1")
        blocking = self._observe("fixture-migrate-2")
        self.assertFalse(blocking.retry_allowed)
        corrective_id = self.guard.corrective_request_id(blocking.signature)
        item = self.ledger.get_request(corrective_id)
        self.assert_no_secret_in(item["prompt"], "the corrective work item prompt")
        self.assert_no_secret_in(
            json.dumps(item["acceptance_criteria"], default=str), "its criteria"
        )
        self.assertIn("<redacted-password>", item["prompt"])

    def test_the_ledger_evidence_and_blocker_hold_no_credential(self):
        self._observe("fixture-migrate-1")
        self._observe("fixture-migrate-2")
        req = self.ledger.get_request("req-fixture-secret")
        self.assert_no_secret_in(json.dumps(req, default=str), "the failing request record")

    def test_the_escalation_event_holds_no_credential(self):
        for n in range(1, ESCALATION_THRESHOLD + 1):
            self._observe(f"fixture-migrate-{n}")
        pending = self.guard.pending_escalations()
        self.assertEqual(len(pending), 1)
        self.assert_no_secret_in(json.dumps(pending, default=str), "the escalation event")

    def test_an_old_store_written_without_redaction_is_not_re_emitted(self):
        """
        Old durable history is preserved as written - deleting it would erase real
        occurrences - but it must not be copied into a *new* durable record. The
        prompt is the place that copying happened, so it redacts on the way out too.
        """
        self._observe("fixture-migrate-1")
        data = self.guard.load()
        signature = next(iter(data["signatures"]))
        data["signatures"][signature]["error_sample"] = CREDENTIAL_FAILURE_ERROR
        with open(self.guard.store_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, default=str)
        blocking = self._observe("fixture-migrate-2")
        item = self.ledger.get_request(self.guard.corrective_request_id(blocking.signature))
        self.assert_no_secret_in(item["prompt"], "a prompt built from an unredacted entry")
        self.assertIn(
            CREDENTIAL_SECRETS[0], self.guard.get(signature)["error_sample"],
            "the pre-existing history itself is preserved, not rewritten",
        )


class FlakyLedger:
    """
    A ledger whose writes fail, wrapping a real one for reads.

    Stands in for the real gap: the recurrence store commits under its own lock,
    then the ledger write fails on the other side of it (a disk error, a lock
    timeout, a killed process).
    """

    def __init__(self, inner):
        self.inner = inner

    def get_request(self, req_id):
        return self.inner.get_request(req_id)

    def add_request(self, **kwargs):
        raise OSError("simulated failure before the corrective work item was written")

    def update_request(self, *args, **kwargs):
        raise OSError("simulated failure before the ledger write landed")


class TestLedgerProjectionIsRecoverable(unittest.TestCase):
    """
    The reviewed durability defect: the store committed occurrence 2 and refused
    retry, the ledger write failed, and replaying the same observation wrote
    nothing because it was a duplicate - so the failing request never got its
    blocker, the corrective work item was never created, and there was no resync.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recurrence_gap_test_")
        self.addCleanup(self._cleanup)
        self.guard = RecurrenceGuard(state_dir=self.tmp)
        self.ledger = RequestLedger(state_dir=self.tmp)
        self.ledger.add_request(
            req_id="req-fixture-gap",
            prompt="restore the migration graph",
            session="fixture-session",
            project="fixture-org/fixture-repo",
            acceptance_criteria=["containers boot on the exact head"],
            owner="DaemonMigrationRepair",
            state="implementation",
            task_type="local",
        )

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _observe(self, attempt, ledger):
        return self.guard.observe(
            project="fixture-org/fixture-repo",
            environment="harness",
            operation="worker:qa",
            error="Worker returned verdict 'fail': container never became healthy",
            source="native_worker",
            request_id="req-fixture-gap",
            attempt=attempt,
            ledger=ledger,
        )

    def _open_a_gap(self):
        self._observe("native_run_1", self.ledger)
        second = self._observe("native_run_2", FlakyLedger(self.ledger))
        self.assertEqual(second.occurrences, 2)
        self.assertFalse(second.retry_allowed)
        self.assertFalse(second.ledger_update["recorded"])
        req = self.ledger.get_request("req-fixture-gap")
        self.assertIsNone(req["blocker"])
        with self.assertRaises(KeyError):
            self.ledger.get_request(self.guard.corrective_request_id(second.signature))
        return second

    def test_replaying_the_observation_recovers_the_projection(self):
        second = self._open_a_gap()
        replay = self._observe("native_run_2", self.ledger)
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.occurrences, 2, "recovery must not add an occurrence")
        self.assertTrue(replay.ledger_update["recorded"])
        self.assertTrue(replay.ledger_update["recovered"])

        req = self.ledger.get_request("req-fixture-gap")
        self.assertIn("Unchanged retry is refused", req["blocker"])
        corrective = self.ledger.get_request(
            self.guard.corrective_request_id(second.signature)
        )
        self.assertEqual(corrective["state"], "pending")
        rows = [e for e in req["evidence"] if e["type"] == "recurrence_observation"]
        self.assertEqual(len(rows), 2, "one row per observation, not one per attempt to write")

    def test_resync_recovers_the_projection_without_a_replay(self):
        second = self._open_a_gap()
        out = self.guard.resync_ledger(ledger=self.ledger)
        self.assertEqual(out["outstanding"], 1)
        self.assertEqual(out["recovered"], 1)
        self.assertEqual(out["still_outstanding"], [])
        req = self.ledger.get_request("req-fixture-gap")
        self.assertIn("Unchanged retry is refused", req["blocker"])
        self.assertTrue(
            self.ledger.get_request(self.guard.corrective_request_id(second.signature))
        )
        self.assertEqual(self.guard.get(second.signature)["occurrences"], 2)

    def test_resync_is_idempotent_and_appends_no_second_evidence_row(self):
        self._open_a_gap()
        self.guard.resync_ledger(ledger=self.ledger)
        again = self.guard.resync_ledger(ledger=self.ledger)
        self.assertEqual(again["outstanding"], 0)
        req = self.ledger.get_request("req-fixture-gap")
        rows = [e for e in req["evidence"] if e["type"] == "recurrence_observation"]
        self.assertEqual(len(rows), 2)

    def test_resync_cli_reports_what_it_repaired(self):
        self._open_a_gap()
        proc = run_cli(["--state-dir", self.tmp, "resync-ledger", "--strict"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["recovered"], 1)
        clean = run_cli(["--state-dir", self.tmp, "resync-ledger", "--strict"])
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(json.loads(clean.stdout)["outstanding"], 0)


class TestCorrectiveWorkItemNeedsExplicitSelection(unittest.TestCase):
    """
    The reviewed authority-boundary defect: the machine-authored corrective item
    was pending and unblocked, so it became the coordinator's *first* runnable
    choice with no operator scope authorization.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recurrence_selection_test_")
        self.addCleanup(self._cleanup)
        self.guard = RecurrenceGuard(state_dir=self.tmp)
        self.ledger = RequestLedger(state_dir=self.tmp)
        self.ledger.add_request(
            req_id="req-fixture-parent",
            prompt="restore the migration graph",
            session="fixture-session",
            project="fixture-org/fixture-repo",
            acceptance_criteria=["containers boot on the exact head"],
            owner="DaemonMigrationRepair",
            state="implementation",
            task_type="local",
        )
        for attempt in ("native_run_1", "native_run_2"):
            self.result = self.guard.observe(
                project="fixture-org/fixture-repo",
                environment="harness",
                operation="worker:qa",
                error="Worker returned verdict 'fail': container never became healthy",
                source="native_worker",
                request_id="req-fixture-parent",
                attempt=attempt,
                ledger=self.ledger,
            )
        self.corrective_id = self.guard.corrective_request_id(self.result.signature)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_item_exists_and_is_labelled_and_scoped(self):
        item = self.ledger.get_request(self.corrective_id)
        labels = item["superboard"]["labels"]
        self.assertIn(EXPLICIT_SELECTION_LABEL, labels)
        self.assertIn("parent-scope:req-fixture-parent", labels)
        self.assertEqual(item["state"], "pending")
        self.assertIsNone(item["blocker"], "it is real work, not a blocked placeholder")
        self.assertTrue(requires_explicit_selection(item))

    def test_the_item_carries_the_failing_requests_originating_session(self):
        """
        A machine-authored item used to carry a literal placeholder session, and
        the notifier binds a notification about a request to whatever session that
        request names - so a correlation row pointed at a session that never
        existed while the failing request's real one was right there.
        """
        item = self.ledger.get_request(self.corrective_id)
        self.assertEqual(
            item["session"], self.ledger.get_request("req-fixture-parent")["session"]
        )
        self.assertEqual(item["session"], "fixture-session")

    def test_the_session_the_failure_was_observed_in_wins_over_the_ledgers(self):
        """The guard already carries it into the escalation event; the item agrees."""
        for attempt in ("native_run_3", "native_run_4"):
            result = self.guard.observe(
                project="fixture-org/fixture-repo",
                environment="harness",
                operation="worker:review",
                error="Worker returned verdict 'fail': review found the same defect",
                source="native_worker",
                request_id="req-fixture-parent",
                session="sess-live-parent",
                attempt=attempt,
                ledger=self.ledger,
            )
        item = self.ledger.get_request(
            self.guard.corrective_request_id(result.signature)
        )
        self.assertEqual(item["session"], "sess-live-parent")
        escalating = [
            e["notification_event"]["session_id"]
            for e in self.guard.pending_escalations()
        ]
        self.assertNotIn("recurrence-guard-corrective-intake", escalating)

    def test_an_unknown_originating_session_is_left_unset_not_invented(self):
        """
        Nothing to inherit is an honestly uncorrelated notification, which the
        bridge refuses to route rather than routing somewhere wrong. An invented
        identity is the one outcome that cannot be recovered from.
        """
        self.ledger.add_request(
            req_id="req-fixture-sessionless",
            prompt="a failure whose originating session was never recorded",
            session="",
            project="fixture-org/fixture-repo",
            acceptance_criteria=["it works"],
            owner="SomeLane",
            state="implementation",
            task_type="local",
        )
        for attempt in ("native_run_5", "native_run_6"):
            result = self.guard.observe(
                project="fixture-org/fixture-repo",
                environment="harness",
                operation="worker:build",
                error="Worker returned verdict 'fail': the build never produced an artifact",
                source="native_worker",
                request_id="req-fixture-sessionless",
                attempt=attempt,
                ledger=self.ledger,
            )
        item = self.ledger.get_request(
            self.guard.corrective_request_id(result.signature)
        )
        self.assertIsNone(item["session"])

    def test_implicit_selection_never_picks_it_up(self):
        from coordinator import Coordinator

        coordinator = Coordinator(state_dir=self.tmp, sync_decisions=False)
        selected, error = coordinator.select_target_request()
        # The parent is blocked, so the corrective item is the only unblocked
        # candidate. Before the fix it was returned here.
        if selected is not None:
            self.assertNotEqual(selected["id"], self.corrective_id, selected["id"])
            self.assertEqual(selected["id"], "req-fixture-parent")
        else:
            self.assertIn(EXPLICIT_SELECTION_LABEL, error or "")

    def test_naming_it_explicitly_still_selects_it(self):
        from coordinator import Coordinator

        coordinator = Coordinator(state_dir=self.tmp, sync_decisions=False)
        selected, error = coordinator.select_target_request(request_id=self.corrective_id)
        self.assertIsNone(error)
        self.assertEqual(selected["id"], self.corrective_id)

    def test_an_unlabelled_request_is_still_implicitly_selectable(self):
        """The filter must be the label, not "anything the guard touched"."""
        from coordinator import Coordinator

        self.ledger.add_request(
            req_id="req-fixture-operator-asked",
            prompt="an operator asked for this",
            session="fixture-session",
            project="fixture-org/fixture-repo",
            acceptance_criteria=["it works"],
            owner="SomeLane",
            task_type="local",
        )
        coordinator = Coordinator(state_dir=self.tmp, sync_decisions=False)
        selected, error = coordinator.select_target_request()
        self.assertIsNone(error)
        self.assertNotEqual(selected["id"], self.corrective_id)


class RecordingSender:
    """
    A sender that reports what the notification contract reports, and sends
    nothing. It constructs no adapter, resolves no destination, opens no bot pool
    and makes no network call: it is the injected transport boundary, which is the
    only honest way to exercise consumption offline.
    """

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = []

    def notify(self, event, dry_run=False, force=False):
        self.calls.append({"event": event, "dry_run": dry_run})
        status = self.statuses.pop(0) if self.statuses else "sent"
        if status == "raise":
            raise RuntimeError("transport socket refused")
        from telegram_notifier import DeliveryReceipt

        return DeliveryReceipt(
            delivered=status == "sent",
            status=("dry_run" if dry_run else status),
            reason=f"fixture transport reported {status}",
        )


class TestEscalationsAreConsumedAndAcknowledged(unittest.TestCase):
    """
    The reviewed limitation: escalations were built as valid notification events
    and stored, and nothing consumed or acknowledged them, so a third occurrence
    was a durable record and never a notification.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recurrence_delivery_test_")
        self.addCleanup(self._cleanup)
        self.guard = RecurrenceGuard(state_dir=self.tmp)
        for minute in DAEMON_RESTART_MINUTES[:ESCALATION_THRESHOLD]:
            self.guard.observe(
                project="fixture-org/fixture-repo",
                environment="staging",
                operation="daemon:startup",
                error=DAEMON_RESTART_ERROR.format(minute=minute),
                source="deploy",
                attempt=f"fixture-restart-cycle-{minute}",
                update_ledger=False,
            )
        self.assertEqual(len(self.guard.pending_escalations()), 1)

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_taken_escalation_is_acknowledged_and_never_offered_again(self):
        sender = RecordingSender(["sent"])
        out = self.guard.deliver_escalations(sender=sender, acknowledged_by="TelegramLane")
        self.assertEqual(len(out["acknowledged"]), 1)
        self.assertEqual(out["acknowledged"][0]["acknowledged_by"], "TelegramLane")
        self.assertEqual(self.guard.pending_escalations(), [])

        again = self.guard.deliver_escalations(
            sender=RecordingSender(["sent"]), acknowledged_by="TelegramLane"
        )
        self.assertEqual(again["considered"], 0, "an acknowledged escalation is never re-sent")

    def test_the_senders_own_dedup_counts_as_a_handoff(self):
        """
        `deduped` means the notification owner already holds an identical event
        inside its own window. Retrying it would produce a duplicate, so it is
        acknowledged rather than offered forever - and no second deduplication
        formula is implemented here.
        """
        out = self.guard.deliver_escalations(
            sender=RecordingSender(["deduped"]), acknowledged_by="TelegramLane"
        )
        self.assertEqual(len(out["acknowledged"]), 1)
        self.assertEqual(out["acknowledged"][0]["status"], "deduped")
        self.assertEqual(self.guard.pending_escalations(), [])

    def test_rate_limiting_and_failure_leave_the_escalation_pending(self):
        for status in ("cooldown", "suppressed", "blocked", "failed", "raise"):
            with self.subTest(status=status):
                out = self.guard.deliver_escalations(
                    sender=RecordingSender([status]), acknowledged_by="TelegramLane"
                )
                self.assertEqual(out["acknowledged"], [])
                self.assertEqual(len(out["still_pending"]), 1)
                self.assertEqual(len(self.guard.pending_escalations()), 1)
        attempts = self.guard.pending_escalations()[0]["delivery_attempts"]
        self.assertEqual(len(attempts), 5)
        self.assertEqual(attempts[-1]["status"], "failed")

    def test_a_dry_run_acknowledges_nothing(self):
        sender = RecordingSender(["sent"])
        out = self.guard.deliver_escalations(
            sender=sender, acknowledged_by="TelegramLane", dry_run=True
        )
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["acknowledged"], [])
        self.assertTrue(sender.calls[0]["dry_run"])
        self.assertEqual(len(self.guard.pending_escalations()), 1)

    def test_delivery_requires_a_sender_and_an_acknowledger(self):
        with self.assertRaises(RecurrenceGuardError):
            self.guard.deliver_escalations(sender=None, acknowledged_by="lane")
        with self.assertRaises(RecurrenceGuardError):
            self.guard.deliver_escalations(sender=object(), acknowledged_by="lane")
        with self.assertRaises(RecurrenceGuardError):
            self.guard.deliver_escalations(
                sender=RecordingSender(["sent"]), acknowledged_by="  "
            )
        self.assertEqual(len(self.guard.pending_escalations()), 1)

    def test_the_offline_outbox_hands_off_through_the_notifier_contract(self):
        """
        The shipped offline sender: the notifier formats the message and the
        notifier's deduplication ledger decides eligibility, both against paths
        this test names explicitly, so nothing resolves to a shared or installed
        default. No transport, no credentials, no destination, no bot pool.
        """
        outbox = os.path.join(self.tmp, "outbox.jsonl")
        dedup = os.path.join(self.tmp, "outbox.dedup.json")
        sender = OfflineEscalationOutbox(outbox_path=outbox, dedup_state_file=dedup)
        out = self.guard.deliver_escalations(sender=sender, acknowledged_by="TelegramLane")
        self.assertEqual(len(out["acknowledged"]), 1)
        self.assertEqual(self.guard.pending_escalations(), [])

        with open(outbox, "r", encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertIn("[Blocker]", lines[0]["message"])
        self.assertIn("fixture-org/fixture-repo", lines[0]["message"])
        self.assertTrue(os.path.exists(dedup))

    def test_the_offline_outbox_cli_delivers_once_and_then_has_nothing_to_do(self):
        outbox = os.path.join(self.tmp, "cli-outbox.jsonl")
        args = [
            "--state-dir", self.tmp, "deliver-escalations",
            "--outbox", outbox,
            "--dedup-state", os.path.join(self.tmp, "cli.dedup.json"),
            "--acknowledged-by", "TelegramLane",
        ]
        first = run_cli(args)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(len(json.loads(first.stdout)["acknowledged"]), 1)

        second = run_cli(args)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["considered"], 0)
        with open(outbox, "r", encoding="utf-8") as fh:
            self.assertEqual(len([l for l in fh if l.strip()]), 1)


class TestCorrectiveProofIsBoundToAVerifiedChange(GuardTestCase):
    """
    The reviewed defect, one layer under the one before it: `--change-ref later`
    was refused, and then `decision:later` with the description "will retry
    later", an exit code of 1 and the failing head as the evidence head opened the
    gate anyway. The evidence fields were *collected*, not bound: nothing resolved
    the reference, nothing read the exit code, nothing compared the head.

    Every test here is one shape that must be recorded and still leave the gate
    where the failures put it, or one shape that must open it because it really
    was verified.

    The failing head is a real commit in a real repository, because the gate now
    relates the whole verification context back to it: a synthetic failing head
    means no context anywhere can be related to the failure, which is its own
    (fail-closed) case and is tested by name in
    `TestCorrectiveProofIsBoundToTheFailuresLineage`.
    """

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        # The tree the failure was observed on, and two real commits on top of it:
        # the fix, and a sibling that is a real head carrying no fix at all.
        self.failing_head = make_repo(self.repo)
        self.fix = commit_systemic_change(self.repo, "revision_restored.py")
        self.sibling = commit_systemic_change(self.repo, "unrelated_change.py")
        self.observe_daemon_cycle(1, head_sha=self.failing_head, repo_root=self.repo)
        self.blocking = self.observe_daemon_cycle(
            2, head_sha=self.failing_head, repo_root=self.repo
        )
        self.signature = self.blocking.signature
        self.assertFalse(self.blocking.retry_allowed)

    def record(self, **kwargs):
        params = dict(verification_root=self.repo, head_sha=self.fix)
        params.update(kwargs)
        return self.correct(self.signature, **params)

    def assert_recorded_but_closed(self, out):
        self.assertTrue(out["recorded"], "a proposal is still worth recording")
        self.assertFalse(out["gate"]["opened"], out["gate"]["reason"])
        self.assertFalse(out["retry_allowed"])
        self.assertEqual(out["status"], STATUS_CORRECTIVE_ACTION_REQUIRED)
        self.assertFalse(self.guard.check_retry(signature=self.signature).allowed)

    def test_retry_later_wearing_a_decision_shape_records_and_opens_nothing(self):
        """The reviewer's exact variant: decision:later, exit 1, the failing head."""
        out = self.record(
            kind="process_change",
            description="will retry later",
            change_ref="decision:later",
            scenario="will retry later again",
            evidence="will retry later again",
            evidence_command=["echo", "retry"],
            evidence_exit_code=1,
            head_sha=self.failing_head,
        )
        self.assert_recorded_but_closed(out)

    def test_a_fabricated_pull_request_reference_records_and_opens_nothing(self):
        out = self.record(kind="process_change", change_ref="pr:nobody/nowhere#999999")
        self.assert_recorded_but_closed(out)
        self.assertIn("remote API", out["action"]["change_ref_verification"]["reason"])

    def test_a_config_reference_the_repository_refutes_is_refused_outright(self):
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.record(kind="config_change", change_ref="config:does/not/exist.yml#nokey")
        self.assertIn("is not in", str(ctx.exception))
        self.assertEqual(self.guard.get(self.signature)["corrective_actions"], [])

    def test_a_config_key_the_committed_tree_does_not_carry_is_refused_outright(self):
        settings = commit_files(self.repo, {"settings.yml": "other_key: 1\n"})
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.record(
                kind="config_change",
                change_ref="config:settings.yml#revision",
                head_sha=settings,
            )
        self.assertIn("not assigned anywhere", str(ctx.exception))

    def test_a_config_key_present_only_as_an_uncommitted_edit_is_refused_outright(self):
        """
        The laundering shape: the key really is in the checkout, and no head carries
        it, so no run of any head ever ran with it.
        """
        with open(os.path.join(self.repo, "settings.yml"), "w", encoding="utf-8") as fh:
            fh.write('revision: "20260904_page_views_kind"\n')
        self.addCleanup(os.remove, os.path.join(self.repo, "settings.yml"))
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.record(kind="config_change", change_ref="config:settings.yml#revision")
        self.assertIn("not in the tree of the evidence head", str(ctx.exception))
        self.assertEqual(self.guard.get(self.signature)["corrective_actions"], [])

    def test_a_test_reference_the_module_does_not_define_is_refused_outright(self):
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.record(kind="process_change", change_ref="test:tests/none.py::test_none")
        self.assertIn("is not in", str(ctx.exception))

    def test_a_commit_with_no_reachable_repository_records_and_opens_nothing(self):
        """
        The normal case for an ephemeral worktree: the tree the failure was
        observed in is gone, so nothing here can look the commit up. That is a
        missing verification context, so the record lands unverified rather than
        clearing the gate on nothing.
        """
        self.observe_daemon_cycle(1, operation="deploy:migrate", head_sha=self.failing_head)
        orphan = self.observe_daemon_cycle(
            2, operation="deploy:migrate", head_sha=self.failing_head
        )
        self.assertFalse(orphan.retry_allowed)
        out = self.correct(
            orphan.signature,
            change_ref=f"commit:{'e' * 40}",
            head_sha="e" * 40,
            verification_root=os.path.join(self.tmp, "vanished"),
        )
        self.assertTrue(out["recorded"])
        self.assertFalse(out["gate"]["opened"])
        self.assertFalse(out["retry_allowed"])
        self.assertIn("no reachable repository", out["gate"]["reason"])
        self.assertFalse(self.guard.check_retry(signature=orphan.signature).allowed)

    def test_a_still_failing_proof_records_and_opens_nothing(self):
        out = self.record(change_ref=f"commit:{self.fix}", evidence_exit_code=1)
        self.assert_recorded_but_closed(out)
        self.assertIn("exited 1", out["gate"]["reason"])
        self.assertTrue(out["action"]["change_ref_verification"]["checked"])

    def test_a_proof_exercised_on_the_failing_head_records_and_opens_nothing(self):
        out = self.record(change_ref=f"commit:{self.fix}", head_sha=self.failing_head)
        self.assert_recorded_but_closed(out)
        self.assertIn("observed on", out["gate"]["reason"])

    def test_a_head_that_does_not_carry_the_change_records_and_opens_nothing(self):
        """The proof ran on a real commit, but not on one containing the fix."""
        out = self.record(change_ref=f"commit:{self.fix}", head_sha=self.sibling)
        self.assert_recorded_but_closed(out)
        self.assertIn("does not contain the change commit", out["gate"]["reason"])
        self.assertFalse(out["gate"]["head_carries_change"])

    def test_a_verified_commit_with_a_successful_bound_proof_opens_the_gate(self):
        out = self.record(change_ref=f"commit:{self.fix}", head_sha=self.fix)
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        self.assertTrue(out["gate"]["head_carries_change"])
        self.assertEqual(out["gate"]["lineage"], "observed_head")
        self.assertEqual(out["status"], STATUS_CORRECTIVE_ACTION_RECORDED)
        self.assertTrue(self.guard.check_retry(signature=self.signature).allowed)

    def test_a_newly_committed_config_key_opens_the_gate(self):
        """A config change has no commit of its own, and is still verifiable."""
        corrected = commit_files(
            self.repo, {"settings.yml": 'revision: "20260904_page_views_kind"\n'}
        )
        out = self.record(
            kind="config_change",
            change_ref="config:settings.yml#revision",
            head_sha=corrected,
        )
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        verification = out["action"]["change_ref_verification"]
        self.assertEqual(verification["line"], 1)
        self.assertEqual(verification["change_comparison"], "differs")
        self.assertEqual(verification["compared_with_observed"], [self.failing_head])
        self.assertTrue(self.guard.check_retry(signature=self.signature).allowed)

    def test_a_committed_test_reference_opens_the_gate(self):
        corrected = commit_files(
            self.repo,
            {
                "tests/test_drift.py": (
                    "def test_drift_check_fails_closed():\n    assert True\n"
                )
            },
        )
        out = self.record(
            kind="process_change",
            change_ref="test:tests/test_drift.py::test_drift_check_fails_closed",
            head_sha=corrected,
        )
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        self.assertEqual(
            out["action"]["change_ref_verification"]["change_comparison"], "differs"
        )

    def _write_decisions(self, payload):
        with open(os.path.join(self.tmp, "decisions.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def test_an_answered_decision_beside_the_store_opens_the_gate(self):
        self._write_decisions(
            {
                "decisions": {
                    "dec-4582": {
                        "status": "answered",
                        "selected_option": "stop retrying",
                        "answer": {
                            # The comment that answered it has to have been
                            # *posted* after the failure it claims to correct, and
                            # this is the API-verified creation time
                            # decision_workflow copies from the comment.
                            "comment_created_at": _later_than_now(),
                            # The instant the reply was ingested. Kept as audit and
                            # never read as proof: a sync running after the failure
                            # would otherwise make any older comment postdate it.
                            "answered_at": _later_than_now(120),
                        },
                    }
                }
            }
        )
        out = self.record(
            kind="process_change", change_ref="decision:dec-4582", head_sha=self.fix
        )
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        verification = out["action"]["change_ref_verification"]
        self.assertEqual(verification["decision_status"], "answered")
        self.assertTrue(verification["answered_after_failure"])

    def test_a_decision_absent_from_the_record_is_refused_outright(self):
        self._write_decisions({"decisions": {"dec-other": {"status": "answered"}}})
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.record(kind="process_change", change_ref="decision:dec-4582")
        self.assertIn("is not in the decision record", str(ctx.exception))

    def test_a_decision_that_was_never_answered_is_refused_outright(self):
        self._write_decisions({"decisions": {"dec-4582": {"status": "posted"}}})
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.record(kind="process_change", change_ref="decision:dec-4582")
        self.assertIn("not 'answered'", str(ctx.exception))

    def test_an_unbound_record_does_not_silence_the_escalation_either(self):
        """
        If recording an unresolvable reference closed the escalation epoch, the
        gate would simply be laundered around the operator instead of past the
        retry.
        """
        third = self.observe_daemon_cycle(3, head_sha=self.failing_head, repo_root=self.repo)
        self.assertEqual(third.status, STATUS_ESCALATED)
        self.record(kind="process_change", change_ref="pr:nobody/nowhere#999999")
        entry = self.guard.get(self.signature)
        self.assertEqual(entry["status"], STATUS_ESCALATED)
        self.assertTrue(entry["retry_blocked"])
        self.assertEqual(entry["corrective_actions_gate_bound"], 0)
        self.assertEqual(entry["corrective_actions_recorded"], 1)

    def test_resolve_will_not_close_on_an_unbound_correction(self):
        """Resolving is the other way the gate opens, so it inherits the binding."""
        self.record(kind="process_change", change_ref="pr:nobody/nowhere#999999")
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.guard.resolve(
                self.signature,
                head_sha=self.fix,
                scenario="restart the staging daemon on the fixture container",
                evidence="drift check exited 0",
                actor="lane",
            )
        self.assertIn("bound to a verified change", str(ctx.exception))
        self.assertFalse(self.guard.check_retry(signature=self.signature).allowed)

    def test_the_gate_is_recomputed_from_the_actions_own_facts(self):
        """
        A stored verdict must not be able to open a gate the action's own record
        denies - the same property the status already has, extended to the binding.
        """
        out = self.record(change_ref=f"commit:{self.fix}", evidence_exit_code=1)
        self.assertFalse(out["gate"]["opened"])
        with open(self.guard.store_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        action = data["signatures"][self.signature]["corrective_actions"][0]
        action["gate_binding"]["bound"] = True
        action["gate_binding"]["evidence_succeeded"] = True
        data["signatures"][self.signature]["status"] = STATUS_CORRECTIVE_ACTION_RECORDED
        data["signatures"][self.signature]["retry_blocked"] = False
        with open(self.guard.store_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        self.assertFalse(
            self.guard.check_retry(signature=self.signature).allowed,
            "the recorded exit code still says the scenario failed",
        )


class TestCorrectiveProofIsBoundToTheFailuresLineage(GuardTestCase):
    """
    The layer under the proof binding: the *context* the proof was verified in.

    Resolving a reference proved something about whatever repository the caller
    named, and nothing about the failure. So an unrelated scratch repository
    holding any commit and a descendant of it opened the gate while the repository
    the failure was recorded in still existed; a config key or a test that had been
    there, unchanged, since before the failure resolved against the checkout; and a
    decision answered months earlier counted as the change. Every test here is one
    of those, or the legitimate correction that must keep working beside them.
    """

    def setUp(self):
        super().setUp()
        # The repository the failure happened in, with a real three-commit history:
        # `before` already carries the config and the test, `failing_head` is the
        # tree the failure was observed on, `fix` is the correction on top of it.
        # A reference into `before` therefore resolves and still names nothing that
        # changed, which is the whole laundering shape under test.
        self.repo = os.path.join(self.tmp, "repo")
        make_repo(self.repo)
        self.before = self._commit_worktree(
            {
                "app.yml": "revision: 1\nfeature: on\n",
                "tests/test_x.py": "def test_old():\n    assert True\n",
            },
            "config and test as they were before the failure",
        )
        self.failing_head = self._commit_worktree(
            {"src.py": "broken = True\n"}, "the head the failure was observed on"
        )
        self.fix = commit_systemic_change(self.repo, "revision_restored.py")

        # A repository that shares no history with the failure at all.
        self.unrelated = os.path.join(self.tmp, "unrelated")
        self.unrelated_base = make_repo(self.unrelated)
        self.unrelated_fix = commit_systemic_change(self.unrelated, "other.txt")

        self.observe_daemon_cycle(1, head_sha=self.failing_head, repo_root=self.repo)
        blocking = self.observe_daemon_cycle(
            2, head_sha=self.failing_head, repo_root=self.repo
        )
        self.signature = blocking.signature
        self.assertFalse(blocking.retry_allowed)

    def _commit_worktree(self, files, message):
        """One real commit that does move HEAD, building the fixture's history."""
        for rel, content in files.items():
            path = os.path.join(self.repo, *rel.split("/"))
            if os.path.dirname(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def record(self, **kwargs):
        params = dict(verification_root=self.repo, head_sha=self.fix)
        params.update(kwargs)
        return self.correct(self.signature, **params)

    def assert_closed(self, out):
        self.assertTrue(out["recorded"], "a proposal is still worth recording")
        self.assertFalse(out["gate"]["opened"], out["gate"]["reason"])
        self.assertFalse(self.guard.check_retry(signature=self.signature).allowed)

    # -- the legitimate corrections that must keep working -----------------

    def test_a_second_worktree_of_the_same_repository_verifies_the_correction(self):
        """
        The documented normal case: the tree the failure was observed in is gone,
        and the correction is verified in another checkout of the same repository.
        It shares the object store, so it resolves the failing head and the gate
        opens - which is the whole reason lineage is checked by head and not by path.
        """
        second = os.path.join(self.tmp, "worktree-2")
        git(self.repo, "worktree", "add", "--detach", second, self.failing_head)
        out = self.record(change_ref=f"commit:{self.fix}", verification_root=second)
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        self.assertEqual(out["gate"]["lineage"], "observed_head")
        self.assertTrue(self.guard.check_retry(signature=self.signature).allowed)

    def test_a_committed_value_change_to_an_existing_key_opens_the_gate(self):
        """The key was already there; what changed is the value it is assigned."""
        corrected = commit_files(self.repo, {"app.yml": "revision: 2\nfeature: on\n"})
        out = self.record(
            kind="config_change",
            change_ref="config:app.yml#revision",
            head_sha=corrected,
        )
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        self.assertEqual(
            out["action"]["change_ref_verification"]["change_comparison"], "differs"
        )

    def test_a_committed_new_test_beside_an_old_one_opens_the_gate(self):
        corrected = commit_files(
            self.repo,
            {
                "tests/test_x.py": (
                    "def test_old():\n    assert True\n\n\n"
                    "def test_drift_check_fails_closed():\n    assert True\n"
                )
            },
        )
        out = self.record(
            kind="process_change",
            change_ref="test:tests/test_x.py::test_drift_check_fails_closed",
            head_sha=corrected,
        )
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])

    def test_a_decision_answered_after_the_last_failure_opens_the_gate(self):
        """The authentic shape: the answering comment was posted after the failure."""
        self._decisions(
            {
                "dec-now": {
                    "status": "answered",
                    "answer": {
                        "comment_created_at": _later_than_now(),
                        "answered_at": _later_than_now(120),
                        "comment_created_at_source": "api_verified",
                    },
                }
            }
        )
        out = self.record(kind="process_change", change_ref="decision:dec-now")
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        self.assertIn("posted", out["gate"]["reason"])

    # -- WIR-1: the verification context has to be the failure's ----------

    def test_an_unrelated_repository_opens_nothing_even_with_a_real_commit(self):
        """
        The reviewed shape: a real commit, a real descendant head, exit 0 - in a
        repository that shares no history with the failure, while the repository
        the failure was recorded in is still right there.
        """
        self.assertTrue(os.path.isdir(self.repo), "the recorded repository still exists")
        out = self.record(
            change_ref=f"commit:{self.unrelated_base}",
            head_sha=self.unrelated_fix,
            verification_root=self.unrelated,
        )
        self.assert_closed(out)
        self.assertIsNone(out["gate"]["lineage"])
        self.assertIn("resolves none of the", out["gate"]["reason"])
        self.assertTrue(
            out["action"]["change_ref_verification"]["checked"],
            "the reference really did resolve there; that is exactly why resolution "
            "alone cannot be the gate",
        )

    def test_a_failing_head_no_reachable_repository_resolves_opens_nothing(self):
        """
        Fail-closed on missing lineage: the failure recorded a head, and no context
        on this host can resolve it, so nothing can be related to it.
        """
        self.observe_daemon_cycle(
            1, operation="deploy:migrate", head_sha="b" * 40, repo_root=self.repo
        )
        orphan = self.observe_daemon_cycle(
            2, operation="deploy:migrate", head_sha="b" * 40, repo_root=self.repo
        )
        out = self.correct(
            orphan.signature,
            change_ref=f"commit:{self.fix}",
            head_sha=self.fix,
            verification_root=self.repo,
        )
        self.assertFalse(out["gate"]["opened"], out["gate"]["reason"])
        self.assertIsNone(out["gate"]["lineage"])
        self.assertFalse(self.guard.check_retry(signature=orphan.signature).allowed)

    def test_a_failure_that_recorded_no_lineage_binds_nothing_and_names_the_way_out(self):
        """
        NEW-3, and the reviewed regression of this very test: neither a head nor a
        reachable repository was ever recorded, so there is nothing a proof can be
        related to - and "related to nothing" was previously recorded honestly and
        then read as a pass, so any repository with any commit opened the gate.

        It stays closed. That is not a deadlock and needs no new authorization:
        the refusal names the two recovery paths this module already contracts,
        re-observing the failure with a head (and a repository) or superseding its
        counted observations as an operator, and the second one is exercised here.
        """
        self.observe_daemon_cycle(1, operation="deploy:migrate", lineage=False)
        orphan = self.observe_daemon_cycle(2, operation="deploy:migrate", lineage=False)
        out = self.correct(
            orphan.signature,
            change_ref=f"commit:{self.unrelated_base}",
            head_sha=self.unrelated_fix,
            verification_root=self.unrelated,
        )
        self.assertTrue(out["recorded"], "a proposal is still worth recording")
        self.assertFalse(out["gate"]["opened"], out["gate"]["reason"])
        self.assertEqual(out["gate"]["lineage"], "unanchored")
        self.assertIn("stands on nothing", out["gate"]["reason"])
        self.assertIn("--head-sha", out["gate"]["reason"])
        self.assertIn("supersede-observation", out["gate"]["reason"])
        self.assertFalse(self.guard.check_retry(signature=orphan.signature).allowed)
        self.assertIn(
            "recorded no head and no reachable repository",
            self.guard.get(orphan.signature)["required_action"],
        )

        # The contracted operator path out, with an actor and a reason on the
        # record: taking the counted observations out of the count unblocks retry
        # without anybody claiming a proof that stands on nothing.
        for observation in self.guard.get(orphan.signature)["observations"]:
            self.guard.supersede_observation(
                observation_id=observation["observation_id"],
                disposition="superseded_attempt",
                reason="operator reclassified the unanchored attempts after triage",
                actor="operator",
            )
        self.assertTrue(self.guard.check_retry(signature=orphan.signature).allowed)

    def test_an_unanchored_failure_reobserved_with_a_head_can_be_corrected(self):
        """
        The other contracted way out, and the one that keeps the gate meaningful:
        the failure is re-observed where it happens, so a descendant correction has
        something to descend from.
        """
        self.observe_daemon_cycle(1, operation="deploy:migrate", lineage=False)
        orphan = self.observe_daemon_cycle(2, operation="deploy:migrate", lineage=False)
        self.assertFalse(self.guard.check_retry(signature=orphan.signature).allowed)
        anchored = self.observe_daemon_cycle(
            3,
            operation="deploy:migrate",
            head_sha=self.failing_head,
            repo_root=self.repo,
        )
        self.assertEqual(anchored.signature, orphan.signature)
        out = self.correct(
            orphan.signature,
            change_ref=f"commit:{self.fix}",
            head_sha=self.fix,
            verification_root=self.repo,
        )
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        self.assertEqual(out["gate"]["lineage"], "observed_head")
        self.assertTrue(self.guard.check_retry(signature=orphan.signature).allowed)

    # -- WIR-2: the reference has to name something that changed ----------

    def test_a_config_key_unchanged_since_before_the_failure_opens_nothing(self):
        out = self.record(
            kind="config_change",
            description="will retry later",
            change_ref="config:app.yml#revision",
            head_sha=self.before,
        )
        self.assert_closed(out)
        verification = out["action"]["change_ref_verification"]
        self.assertEqual(verification["change_comparison"], "identical")
        self.assertEqual(verification["unchanged_at_observed"], [self.failing_head])

    def test_a_test_that_predates_the_failure_opens_nothing(self):
        out = self.record(
            kind="process_change",
            change_ref="test:tests/test_x.py::test_old",
            head_sha=self.before,
        )
        self.assert_closed(out)
        self.assertEqual(
            out["action"]["change_ref_verification"]["change_comparison"], "identical"
        )

    def test_an_evidence_head_the_failing_head_was_built_on_opens_nothing(self):
        """
        A commit reference whose head really contains it, and is an ancestor of the
        head the failure was observed on: that tree already existed when the
        failure happened, so nothing in it can be the change.
        """
        out = self.record(change_ref=f"commit:{self.before}", head_sha=self.before)
        self.assert_closed(out)
        self.assertEqual(
            out["gate"]["head_precedes_observed_failure"], self.failing_head
        )
        self.assertIn("is an ancestor of", out["gate"]["reason"])

    # -- WIR-3 / NEW-4: the answering comment has to postdate the failure --

    def _decisions(self, decisions):
        with open(os.path.join(self.tmp, "decisions.json"), "w", encoding="utf-8") as fh:
            json.dump({"decisions": decisions}, fh)

    def test_a_decision_answered_before_the_failure_opens_nothing(self):
        self._decisions(
            {
                "dec-old": {
                    "status": "answered",
                    "question": "an unrelated question from January",
                    "answer": {"comment_created_at": BEFORE_ANY_FAILURE},
                }
            }
        )
        out = self.record(
            kind="process_change",
            description="will retry later",
            change_ref="decision:dec-old",
        )
        self.assert_closed(out)
        verification = out["action"]["change_ref_verification"]
        self.assertFalse(verification["answered_after_failure"])
        self.assertIn("before this signature last failed", verification["reason"])

    def test_a_comment_posted_before_the_failure_but_ingested_after_it_opens_nothing(self):
        """
        The reviewed cross-lane defect (NEW-4), and the *normal* case rather than
        an edge: `sync_decisions` is a bounded one-shot invoked at barriers, so a
        comment posted before the failure is routinely ingested after it. Reading
        the ingest instant made every such answer "postdate" the failure it was
        already taken during.
        """
        self._decisions(
            {
                "dec-lag": {
                    "status": "answered",
                    "answer": {
                        "comment_created_at": BEFORE_ANY_FAILURE,
                        # Ingested a minute from now: after the failure, and not
                        # proof of anything about when the answer was posted.
                        "answered_at": _later_than_now(),
                    },
                }
            }
        )
        out = self.record(kind="process_change", change_ref="decision:dec-lag")
        self.assert_closed(out)
        verification = out["action"]["change_ref_verification"]
        self.assertFalse(verification["answered_after_failure"])
        self.assertEqual(verification["comment_created_at"], BEFORE_ANY_FAILURE)
        self.assertIn("before this signature last failed", verification["reason"])

    def test_an_ingest_timestamp_alone_is_not_proof_and_opens_nothing(self):
        """
        No fallback. An answer whose only timestamp is the ingest instant - which
        is what the decision workflow wrote before the producer carried the
        comment's creation time - cannot be shown to have been posted after the
        failure, so it resolves and binds nothing.
        """
        self._decisions(
            {"dec-ingest-only": {"status": "answered",
                                 "answer": {"answered_at": _later_than_now()}}}
        )
        out = self.record(kind="process_change", change_ref="decision:dec-ingest-only")
        self.assert_closed(out)
        verification = out["action"]["change_ref_verification"]
        self.assertIsNone(verification["comment_created_at"])
        self.assertIn("no readable answer.comment_created_at", verification["reason"])
        self.assertIn("not the instant it was posted", verification["reason"])

    def test_a_hand_edited_record_timestamp_is_not_a_fallback_either(self):
        """
        `record.updated_at` and a top-level `answered_at` are whatever wrote the
        file last said they are. Neither may stand in for the comment's creation
        time.
        """
        self._decisions(
            {
                "dec-hand-edited": {
                    "status": "answered",
                    "answered_at": _later_than_now(),
                    "updated_at": _later_than_now(120),
                    "answer": {"responder": "operator", "comment_id": "1"},
                }
            }
        )
        out = self.record(kind="process_change", change_ref="decision:dec-hand-edited")
        self.assert_closed(out)
        self.assertIn(
            "no readable answer.comment_created_at",
            out["action"]["change_ref_verification"]["reason"],
        )

    def test_an_unparseable_comment_creation_time_opens_nothing(self):
        self._decisions(
            {"dec-garbled": {"status": "answered",
                             "answer": {"comment_created_at": "last tuesday"}}}
        )
        out = self.record(kind="process_change", change_ref="decision:dec-garbled")
        self.assert_closed(out)
        self.assertIn(
            "no readable answer.comment_created_at",
            out["action"]["change_ref_verification"]["reason"],
        )

    def test_a_decision_answered_between_two_occurrences_opens_nothing(self):
        """It was already taken when the failure happened again, so it did not hold."""
        # Posted one millisecond after the second occurrence was recorded, and
        # then a third occurrence lands: the answer predates the last counted
        # failure, so it demonstrably did not hold.
        second = self.guard.get(self.signature)["observations"][1]["observed_at"]
        posted = (
            datetime.datetime.fromisoformat(second)
            + datetime.timedelta(milliseconds=1)
        ).isoformat()
        self._decisions(
            {"dec-mid": {"status": "answered",
                         "answer": {"comment_created_at": posted}}}
        )
        self.observe_daemon_cycle(
            3, head_sha=self.failing_head, repo_root=self.repo,
            attempt="fixture-restart-cycle-after-decision",
        )
        out = self.record(kind="process_change", change_ref="decision:dec-mid")
        self.assert_closed(out)
        self.assertIn(
            "before this signature last failed",
            out["action"]["change_ref_verification"]["reason"],
        )

    # -- the gate reason must not describe a relation nobody checked -------

    def test_the_recorded_reason_names_the_relation_that_actually_held(self):
        corrected = commit_files(self.repo, {"app.yml": "revision: 2\nfeature: on\n"})
        config = self.record(
            kind="config_change",
            change_ref="config:app.yml#revision",
            head_sha=corrected,
        )
        self.assertIn("committed configuration key differs", config["gate"]["reason"])
        self.assertNotIn("change commit", config["gate"]["reason"])

        commit = self.record(change_ref=f"commit:{self.fix}", head_sha=self.fix)
        self.assertIn("contains the change commit", commit["gate"]["reason"])

    # -- reality still wins over any record --------------------------------

    def test_a_recurrence_after_a_refused_correction_still_escalates(self):
        self.record(
            kind="config_change",
            description="will retry later",
            change_ref="config:app.yml#revision",
            head_sha=self.before,
        )
        third = self.observe_daemon_cycle(
            3, head_sha=self.failing_head, repo_root=self.repo
        )
        self.assertEqual(third.status, STATUS_ESCALATED)
        self.assertFalse(third.retry_allowed)
        self.assertEqual(third.occurrences, 3)


class TestCorrectiveProofRunsOnTheCorrectedFailingTree(GuardTestCase):
    """
    NEW-1 and NEW-2: the head the proof ran on, and the region the reference names.

    The layer under the lineage check. Once the verification *context* was
    anchored, the evidence *head* was still related to the failure only by what it
    was not - not an observed head, not an ancestor of one - which every tree that
    never carried the failure satisfies: an orphan root in the same repository, an
    orphan commit in any clone that merely holds the failing commit, a sibling
    branch cut from the failing commit's parent. And "the reference names something
    that changed" was whole-file byte inequality, so an untouched key or test
    counted as the change whenever anything else in the file moved.

    The fixture is one repository with a real shape: `before` carries the config
    and the test, `failing_head` is the tree the failure was observed on, and
    `fix`, `orphan` and `sibling` are the three heads a proof might claim.
    """

    def setUp(self):
        import shutil

        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        make_repo(self.repo)
        trunk = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.before = self._commit(
            {
                "app.yml": "revision: 1\nfeature: on\n",
                "tests/test_x.py": "def test_old():\n    assert True\n",
            },
            "config and test as they were before the failure",
        )
        self.failing_head = self._commit(
            {"src.py": "broken = True\n"}, "the head the failure was observed on"
        )
        self.fix = self._commit({"src.py": "broken = False\n"}, "the systemic fix")

        # A sibling branch from the failing commit's parent: a real head, in the
        # right repository, that never contained the failure.
        git(self.repo, "checkout", "-q", "-b", "sibling", self.before)
        self.sibling = self._commit({"other.py": "unrelated = 1\n"}, "sibling change")
        # An orphan root in the same repository: shares no history with anything.
        git(self.repo, "checkout", "-q", "--orphan", "orphan")
        git(self.repo, "rm", "-rfq", "--cached", ".")
        for name in ("src.py", "other.py", "app.yml"):
            path = os.path.join(self.repo, name)
            if os.path.exists(path):
                os.remove(path)
        shutil.rmtree(os.path.join(self.repo, "tests"), ignore_errors=True)
        self.orphan = self._commit({"README": "orphan\n"}, "orphan root")
        git(self.repo, "checkout", "-q", trunk)

        self.observe(1)
        blocking = self.observe(2)
        self.signature = blocking.signature
        self.assertFalse(blocking.retry_allowed)

    def _commit(self, files, message):
        for rel, content in files.items():
            path = os.path.join(self.repo, *rel.split("/"))
            if os.path.dirname(path):
                os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def observe(self, minute, head=None, **kwargs):
        return self.observe_daemon_cycle(
            minute, head_sha=head or self.failing_head, repo_root=self.repo, **kwargs
        )

    def record(self, **kwargs):
        params = dict(verification_root=self.repo, head_sha=self.fix)
        params.update(kwargs)
        return self.correct(self.signature, **params)

    def assert_closed(self, out):
        self.assertTrue(out["recorded"], "a proposal is still worth recording")
        self.assertFalse(out["gate"]["opened"], out["gate"]["reason"])
        self.assertFalse(self.guard.check_retry(signature=self.signature).allowed)

    # -- NEW-1: the head has to descend from every observed failing head ---

    def test_an_orphan_head_in_the_same_repository_opens_nothing(self):
        """
        The reviewed laundering shape one level down: the context resolves the
        failing head, and the head the proof ran on shares no history with it.
        """
        out = self.record(change_ref=f"commit:{self.orphan}", head_sha=self.orphan)
        self.assert_closed(out)
        self.assertEqual(out["gate"]["lineage"], "observed_head")
        self.assertTrue(
            out["action"]["change_ref_verification"]["checked"],
            "the reference resolves; descent is what it does not establish",
        )
        self.assertFalse(out["gate"]["descends_from_observed"])
        self.assertEqual(
            out["gate"]["observed_heads_not_contained"], [self.failing_head]
        )
        self.assertIn("does not descend from", out["gate"]["reason"])

    def test_a_sibling_head_that_never_carried_the_failure_opens_nothing(self):
        """
        A common ancestor is not descent: `sibling` branched from the failing
        commit's parent, so the failure was never in its history.
        """
        out = self.record(change_ref=f"commit:{self.sibling}", head_sha=self.sibling)
        self.assert_closed(out)
        self.assertFalse(out["gate"]["descends_from_observed"])
        self.assertIn("sharing a merge-base with it is not descent", out["gate"]["reason"])

    def test_an_orphan_head_in_a_clone_that_merely_holds_the_failure_opens_nothing(self):
        """
        Every clone of the project holds the failing commit, so resolving it there
        proves availability and nothing else.
        """
        clone = os.path.join(self.tmp, "clone")
        git(self.tmp, "clone", "-q", self.repo, clone)
        git(clone, "checkout", "-q", self.orphan)
        out = self.record(
            change_ref=f"commit:{self.orphan}",
            head_sha=self.orphan,
            verification_root=clone,
        )
        self.assert_closed(out)
        self.assertEqual(out["gate"]["lineage"], "observed_head")
        self.assertFalse(out["gate"]["descends_from_observed"])

    def test_a_head_descending_from_only_one_of_two_observed_heads_opens_nothing(self):
        """
        Both branches really failed, so a fix that only ever contained one of them
        cannot be the correction for the signature: `sibling` is a second counted
        failing head, and `fix` descends from the first only.
        """
        self.observe(3, head=self.sibling, attempt="fixture-restart-cycle-sibling")
        out = self.record(change_ref=f"commit:{self.fix}", head_sha=self.fix)
        self.assert_closed(out)
        self.assertEqual(out["gate"]["observed_heads_not_contained"], [self.sibling])
        self.assertIn("does not descend from", out["gate"]["reason"])

    def test_an_unresolvable_observed_head_fails_closed_rather_than_being_skipped(self):
        """
        A second counted head that this context cannot resolve leaves the descent
        unproven for that head. Unproven is refused, not ignored - otherwise a
        fabricated second head would *weaken* the check it is subject to.
        """
        self.observe(4, head="b" * 40, attempt="fixture-restart-cycle-unresolvable")
        out = self.record(change_ref=f"commit:{self.fix}", head_sha=self.fix)
        self.assert_closed(out)
        self.assertEqual(out["gate"]["observed_heads_unresolved"], ["b" * 40])
        self.assertFalse(out["gate"]["containment_determined"])
        self.assertIn("is not resolvable in", out["gate"]["reason"])

    def test_the_real_descendant_fix_still_opens_the_gate(self):
        out = self.record(change_ref=f"commit:{self.fix}", head_sha=self.fix)
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        self.assertTrue(out["gate"]["descends_from_observed"])
        self.assertEqual(out["gate"]["observed_heads_not_contained"], [])
        self.assertIn("descending from every head", out["gate"]["reason"])
        self.assertTrue(self.guard.check_retry(signature=self.signature).allowed)

    def test_a_descendant_fix_verified_in_a_plain_clone_still_opens_the_gate(self):
        """A clone legitimately holds both the failure and its descendant fix."""
        clone = os.path.join(self.tmp, "clone-ok")
        git(self.tmp, "clone", "-q", self.repo, clone)
        out = self.record(
            change_ref=f"commit:{self.fix}", head_sha=self.fix, verification_root=clone
        )
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        self.assertTrue(out["gate"]["descends_from_observed"])

    def test_a_replacement_worktree_of_the_same_repository_still_opens_the_gate(self):
        """The ephemeral-lane flow: the tree the failure was observed in is gone."""
        replacement = os.path.join(self.tmp, "worktree-replacement")
        git(self.repo, "worktree", "add", "-q", "--detach", replacement, self.fix)
        out = self.record(
            change_ref=f"commit:{self.fix}",
            head_sha=self.fix,
            verification_root=replacement,
        )
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        self.assertTrue(out["gate"]["descends_from_observed"])

    # -- NEW-2: the reference has to name the region that changed ---------

    def test_an_untouched_key_in_a_file_changed_elsewhere_opens_nothing(self):
        """
        `feature` has been byte-identical since before the failure; what the
        corrective commit changed is `revision` beside it. Whole-file inequality
        made that count as a change to `feature`.
        """
        corrected = commit_files(self.repo, {"app.yml": "revision: 2\nfeature: on\n"})
        out = self.record(
            kind="config_change",
            change_ref="config:app.yml#feature",
            head_sha=corrected,
        )
        self.assert_closed(out)
        verification = out["action"]["change_ref_verification"]
        self.assertEqual(verification["change_comparison"], "identical")
        self.assertEqual(verification["unchanged_at_observed"], [self.failing_head])

    def test_a_comment_only_touch_of_the_file_opens_nothing(self):
        corrected = commit_files(
            self.repo, {"app.yml": "revision: 1\nfeature: on\n# touched\n"}
        )
        out = self.record(
            kind="config_change",
            change_ref="config:app.yml#revision",
            head_sha=corrected,
        )
        self.assert_closed(out)
        self.assertEqual(
            out["action"]["change_ref_verification"]["change_comparison"], "identical"
        )

    def test_an_inline_comment_beside_the_value_is_not_a_value_change(self):
        corrected = commit_files(
            self.repo, {"app.yml": "revision: 1  # restored by hand\nfeature: on\n"}
        )
        out = self.record(
            kind="config_change",
            change_ref="config:app.yml#revision",
            head_sha=corrected,
        )
        self.assert_closed(out)
        self.assertEqual(
            out["action"]["change_ref_verification"]["change_comparison"], "identical"
        )

    def test_a_pre_existing_test_beside_a_newly_added_one_opens_nothing(self):
        """
        `test_old` is untouched; a different test was added below it. The reference
        names `test_old`, so `test_old` is what has to have changed.
        """
        corrected = commit_files(
            self.repo,
            {
                "tests/test_x.py": (
                    "def test_old():\n    assert True\n\n\n"
                    "def test_new():\n    assert True\n"
                )
            },
        )
        out = self.record(
            kind="process_change",
            change_ref="test:tests/test_x.py::test_old",
            head_sha=corrected,
        )
        self.assert_closed(out)
        verification = out["action"]["change_ref_verification"]
        self.assertEqual(verification["change_comparison"], "identical")
        self.assertEqual(verification["unchanged_at_observed"], [self.failing_head])

    def test_a_key_assigned_twice_in_the_file_is_refused_outright(self):
        """
        Ambiguous rather than wrong: the reference does not identify one
        assignment, so picking the first would compare a region nobody named.
        """
        corrected = commit_files(
            self.repo, {"app.yml": "revision: 1\nfeature: on\nrevision: 2\n"}
        )
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.record(
                kind="config_change",
                change_ref="config:app.yml#revision",
                head_sha=corrected,
            )
        self.assertIn("is assigned on 2 lines", str(ctx.exception))
        self.assertEqual(self.guard.get(self.signature)["corrective_actions"], [])

    def test_a_key_only_inside_a_minified_document_is_refused_outright(self):
        """
        A flow mapping or a minified document puts the whole file on one line, so
        the compared region would be the whole file - the defect, one level down.
        """
        corrected = commit_files(
            self.repo, {"app.json": '{"revision": 2, "feature": "on"}\n'}
        )
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.record(
                kind="config_change",
                change_ref="config:app.json#revision",
                head_sha=corrected,
            )
        self.assertIn("flow or minified line", str(ctx.exception))

    def test_a_test_defined_twice_in_the_module_is_refused_outright(self):
        corrected = commit_files(
            self.repo,
            {
                "tests/test_x.py": (
                    "def test_old():\n    assert True\n\n\n"
                    "def test_old():\n    assert False\n"
                )
            },
        )
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.record(
                kind="process_change",
                change_ref="test:tests/test_x.py::test_old",
                head_sha=corrected,
            )
        self.assertIn("is defined 2 times", str(ctx.exception))

    def test_a_dotted_key_that_resolves_to_two_candidates_is_refused_outright(self):
        corrected = commit_files(
            self.repo,
            {
                "app.yml": (
                    "revision: 1\nfeature: on\n"
                    "server:\n  port: 8080\n  port: 9090\n"
                )
            },
        )
        with self.assertRaises(RecurrenceGuardError) as ctx:
            self.record(
                kind="config_change",
                change_ref="config:app.yml#server.port",
                head_sha=corrected,
            )
        self.assertIn("resolves ambiguously", str(ctx.exception))

    # -- the scoped comparison still recognises a real change -------------

    def test_a_committed_value_change_to_the_referenced_key_opens_the_gate(self):
        corrected = commit_files(self.repo, {"app.yml": "revision: 1\nfeature: off\n"})
        out = self.record(
            kind="config_change",
            change_ref="config:app.yml#feature",
            head_sha=corrected,
        )
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        self.assertEqual(
            out["action"]["change_ref_verification"]["change_comparison"], "differs"
        )

    def test_a_nested_dotted_key_change_opens_the_gate(self):
        corrected = commit_files(
            self.repo,
            {"app.yml": "revision: 1\nfeature: on\nserver:\n  port: 9090\n"},
        )
        out = self.record(
            kind="config_change",
            change_ref="config:app.yml#server.port",
            head_sha=corrected,
        )
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        self.assertEqual(
            out["action"]["change_ref_verification"]["absent_at_observed"],
            [self.failing_head],
        )

    def test_a_changed_body_of_the_referenced_test_opens_the_gate(self):
        """The definition is what is compared, so a rewritten body is a change."""
        corrected = commit_files(
            self.repo,
            {
                "tests/test_x.py": (
                    "def test_old():\n    assert compute() == 2\n\n\n"
                    "def test_new():\n    assert True\n"
                )
            },
        )
        out = self.record(
            kind="process_change",
            change_ref="test:tests/test_x.py::test_old",
            head_sha=corrected,
        )
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        self.assertEqual(
            out["action"]["change_ref_verification"]["change_comparison"], "differs"
        )

    def test_a_new_decorator_on_the_referenced_test_opens_the_gate(self):
        """A decorator is part of the test's definition, not of its surroundings."""
        corrected = commit_files(
            self.repo,
            {
                "tests/test_x.py": (
                    "@pytest.mark.parametrize('n', [1, 2])\n"
                    "def test_old():\n    assert True\n"
                )
            },
        )
        out = self.record(
            kind="process_change",
            change_ref="test:tests/test_x.py::test_old",
            head_sha=corrected,
        )
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        self.assertEqual(
            out["action"]["change_ref_verification"]["change_comparison"], "differs"
        )

    def test_a_record_written_before_descent_was_checked_recomputes_as_unbound(self):
        """
        The gate is recomputed from the action's own recorded facts, so a record
        that predates this check carries none of them - and an absent relation is
        an unestablished one, not a passed one.
        """
        out = self.record(change_ref=f"commit:{self.fix}", head_sha=self.fix)
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        with open(self.guard.store_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        binding = data["signatures"][self.signature]["corrective_actions"][0][
            "gate_binding"
        ]
        for field in (
            "descends_from_observed",
            "containment_determined",
            "observed_heads_contained",
            "observed_heads_not_contained",
            "observed_heads_recorded",
        ):
            binding.pop(field, None)
        with open(self.guard.store_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        self.assertFalse(
            self.guard.check_retry(signature=self.signature).allowed,
            "descent was never established on this record, so it cannot open the gate",
        )

    # -- a headless failure has no descent, so the head must postdate it --

    def _commit_dated(self, message, when):
        """
        A real commit whose committer date is `when`, without moving HEAD.

        Wall-clock dating is not a fixture: git reports a commit time to the
        second, so a commit made in the same second as the observation is
        deliberately *not* ordered against it. A test about a pre-failure tree has
        to name a date, not race one.
        """
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"], cwd=self.repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        env = dict(os.environ, GIT_COMMITTER_DATE=when, GIT_AUTHOR_DATE=when)
        return subprocess.run(
            ["git", "commit-tree", tree, "-p", "HEAD", "-m", message],
            cwd=self.repo, env=env, capture_output=True, text=True, check=True,
        ).stdout.strip()

    def test_a_headless_failure_binds_only_a_head_committed_after_it(self):
        """
        A failure that recorded no head has no history to descend from, so the
        only relation left is that the corrective tree came *after* it. Without
        this, any commit already sitting in the recorded repository bound - the
        same pre-failure tree the descent check refuses when there is a head.
        """
        self.observe_daemon_cycle(1, operation="deploy:migrate", repo_root=self.repo)
        headless = self.observe_daemon_cycle(
            2, operation="deploy:migrate", repo_root=self.repo
        )
        self.assertFalse(headless.retry_allowed)
        before_the_failure = self._commit_dated(
            "a tree that already existed when the failure happened",
            "2025-01-01T00:00:00+00:00",
        )
        stale = self.correct(
            headless.signature,
            change_ref=f"commit:{before_the_failure}",
            head_sha=before_the_failure,
            verification_root=self.repo,
        )
        self.assertFalse(stale["gate"]["opened"], stale["gate"]["reason"])
        self.assertEqual(stale["gate"]["lineage"], "recorded_repository")
        self.assertFalse(stale["gate"]["head_committed_after_failure"])
        self.assertIn(
            "already existed while the failure was happening", stale["gate"]["reason"]
        )
        self.assertFalse(self.guard.check_retry(signature=headless.signature).allowed)

        after = commit_files(self.repo, {"src.py": "broken = False\n"}, "the later fix")
        opened = self.correct(
            headless.signature,
            change_ref=f"commit:{after}",
            head_sha=after,
            verification_root=self.repo,
        )
        self.assertTrue(opened["gate"]["opened"], opened["gate"]["reason"])
        self.assertTrue(opened["gate"]["head_committed_after_failure"])
        self.assertTrue(self.guard.check_retry(signature=headless.signature).allowed)


class TestEveryDurableWriteRedacts(GuardTestCase):
    """
    The reviewed privacy defect, one layer under the one before it: redaction was
    applied to a *list* of named fields, and every field added after that list was
    written verbatim - a supersede reason, a corrective action's argv, its
    authorization reference. Two credential shapes were uncovered as well:
    `PGPASSWORD=` (a word boundary never fires inside it) and `Authorization:
    Basic` (base64, which no token pattern recognises).

    Redaction is now a property of the write boundary, so these tests are about
    fields nobody redacted on purpose.
    """

    PASSWORD = "supers3cretpw"
    TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz012345"
    PGPASS = "envsecret999"
    BASIC = "dXNlcjpwYXNzd29yZA=="

    def setUp(self):
        super().setUp()
        self.repo = os.path.join(self.tmp, "repo")
        self.base = make_repo(self.repo)
        self.fix = commit_systemic_change(self.repo, "revision_restored.py")
        self.observe_daemon_cycle(1, head_sha=self.base, repo_root=self.repo)
        self.blocking = self.observe_daemon_cycle(
            2, head_sha=self.base, repo_root=self.repo
        )

    def stored(self):
        with open(self.guard.store_path, "r", encoding="utf-8") as fh:
            return fh.read()

    def assert_absent(self, *secrets):
        blob = self.stored()
        for secret in secrets:
            self.assertNotIn(secret, blob, f"{secret!r} reached the store")

    def test_a_supersede_reason_is_redacted_in_the_observation_and_in_history(self):
        observation = self.guard.get(self.blocking.signature)["observations"][1]
        self.guard.supersede_observation(
            observation_id=observation["observation_id"],
            disposition="expected_negative_control",
            reason=f"probe was intentional; ran with password={self.PASSWORD} token={self.TOKEN}",
            actor="lane",
        )
        self.assert_absent(self.PASSWORD, self.TOKEN)
        entry = self.guard.get(self.blocking.signature)
        self.assertIn(
            "password=<redacted>", entry["observations"][1]["superseded"]["reason"]
        )
        self.assertIn(
            "token=<redacted>",
            [h.get("reason") for h in entry["history"] if h.get("reason")][-1],
        )

    def test_an_evidence_command_and_an_authorization_reference_are_redacted(self):
        out = self.correct(
            self.blocking.signature,
            kind="ddl",
            change_ref=f"commit:{self.fix}",
            head_sha=self.fix,
            verification_root=self.repo,
            description=f"rotated the credential and restored the revision ({self.TOKEN})",
            evidence_command=["psql", f"postgresql://svc:{self.PASSWORD}@db/app"],
            authorization=f"operator attestation token={self.TOKEN}",
        )
        self.assert_absent(self.PASSWORD, self.TOKEN)
        action = out["action"]
        self.assertIn("<redacted-password>", action["evidence_command"][1])
        self.assertIn("<redacted>", action["authorization"])
        self.assertIn("<redacted-token>", action["description"])
        # The privileged record still says who authorized it: the key is kept and
        # only its value is gone.
        self.assertIn("operator attestation", action["authorization"])

    def test_a_credential_passed_as_a_command_line_flag_is_redacted(self):
        """
        The residual shape: a credential that labels itself with a flag rather than
        an assignment. As two argv elements neither one is secret-shaped on its own,
        so element-at-a-time redaction wrote it verbatim; space-separated inside one
        recorded command string, no ':' or '=' followed the name.
        """
        argv_secret = "adjacentHunter2Value"
        out = self.correct(
            self.blocking.signature,
            change_ref=f"commit:{self.fix}",
            head_sha=self.fix,
            verification_root=self.repo,
            evidence_command=[
                "psql",
                "--password",
                argv_secret,
                f"psql --password {argv_secret}",
                f"pg_dump --db-password={argv_secret}",
            ],
        )
        self.assert_absent(argv_secret)
        command = out["action"]["evidence_command"]
        self.assertEqual(command[:3], ["psql", "--password", "<redacted>"])
        self.assertEqual(command[3], "psql --password <redacted>")
        self.assertEqual(command[4], "pg_dump --db-password=<redacted>")

    def test_a_valueless_credential_flag_does_not_swallow_the_next_option(self):
        """
        `psql --password` prompts rather than taking a value, so the element after
        it is the next option. Redacting it would corrupt the command this record
        exists to preserve.
        """
        out = self.correct(
            self.blocking.signature,
            change_ref=f"commit:{self.fix}",
            head_sha=self.fix,
            verification_root=self.repo,
            evidence_command=["psql", "--password", "--host", "db", "--dbname", "app"],
        )
        self.assertEqual(
            out["action"]["evidence_command"],
            ["psql", "--password", "--host", "db", "--dbname", "app"],
        )

    def test_a_credential_flag_already_on_disk_is_not_rewritten(self):
        """
        Text a previous build wrote unredacted is the only record those occurrences
        happened, so it stays byte-exact - and a new write of the same value is
        still redacted at intake rather than hiding behind it.
        """
        legacy = "legacyRawFlagValue"
        with open(self.guard.store_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        entry = data["signatures"][self.blocking.signature]
        entry["diagnosis"] = f"ran with --password {legacy}"
        with open(self.guard.store_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        self.observe_daemon_cycle(3, next_action=f"rotate --password {legacy}")
        blob = self.stored()
        self.assertEqual(blob.count(legacy), 1, "no second copy was written")
        refreshed = self.guard.get(self.blocking.signature)
        self.assertEqual(refreshed["diagnosis"], f"ran with --password {legacy}")
        self.assertEqual(refreshed["next_action"], "rotate --password <redacted>")

    def test_environment_and_basic_authorization_shapes_are_covered(self):
        self.guard.observe(
            project="fixture-org/fixture-repo",
            environment="staging",
            operation="deploy:migrate",
            error=(
                f"psql failed: PGPASSWORD={self.PGPASS} "
                f"Authorization: Basic {self.BASIC}"
            ),
            source="deploy",
            attempt="pgpass-1",
            update_ledger=False,
        )
        self.assert_absent(self.PGPASS, self.BASIC)
        self.assertIn("PGPASSWORD=<redacted>", self.stored())
        self.assertIn("Authorization: Basic <redacted>", self.stored())

    def test_an_acknowledgement_actor_is_redacted_too(self):
        """Nobody would think to redact this field, which is the point."""
        self.observe_daemon_cycle(3, repo_root=self.repo)
        escalation = self.guard.pending_escalations()[0]
        self.guard.acknowledge_escalation(
            escalation["escalation_id"], acknowledged_by=f"ops token={self.TOKEN}"
        )
        self.assert_absent(self.TOKEN)

    def test_structured_identity_and_provenance_stay_usable(self):
        """
        Redaction must not anonymise a value something is later looked up by: a
        redacted repository root cannot be opened, and the gate could never verify
        a commit again.
        """
        entry = self.guard.get(self.blocking.signature)
        self.assertEqual(entry["repo_root"], os.path.abspath(self.repo))
        out = self.correct(
            self.blocking.signature,
            change_ref=f"commit:{self.fix}",
            head_sha=self.fix,
            verification_root=self.repo,
        )
        self.assertTrue(out["gate"]["opened"], out["gate"]["reason"])
        action = self.guard.get(self.blocking.signature)["corrective_actions"][0]
        self.assertEqual(action["change_ref"], f"commit:{self.fix}")
        self.assertEqual(action["head_sha"], self.fix)
        self.assertEqual(action["gate_binding"]["verification_root"], os.path.abspath(self.repo))
        # A credential shape inside a structural value is still redacted.
        self.assertIn(
            "<redacted-password>",
            redact_durable({"canonical_link": f"https://u:{self.PASSWORD}@example.invalid/i/1"})[
                "canonical_link"
            ],
        )


class TestSuppressedProjectionSurvives(unittest.TestCase):
    """
    A `--no-ledger-update` observation that named a request was written as "no
    projection applied", which is exactly how a crash between the two locks looks.
    A later `resync-ledger` therefore repaired it into the ledger and wrote the row
    the caller had refused.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="recurrence_suppressed_test_")
        self.addCleanup(self._cleanup)
        self.guard = RecurrenceGuard(state_dir=self.tmp)
        self.ledger = RequestLedger(state_dir=self.tmp)
        self.ledger.add_request(
            req_id="req-fixture-suppressed",
            prompt="restore the migration graph",
            session="fixture-session",
            project="fixture-org/fixture-repo",
            acceptance_criteria=["containers boot on the exact head"],
            owner="DaemonMigrationRepair",
            state="implementation",
            task_type="local",
        )

    def _cleanup(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _observe(self, attempt, **kwargs):
        params = dict(
            project="fixture-org/fixture-repo",
            environment="harness",
            operation="worker:qa",
            error="Worker returned verdict 'fail': container never became healthy",
            source="native_worker",
            request_id="req-fixture-suppressed",
            attempt=attempt,
            ledger=self.ledger,
        )
        params.update(kwargs)
        return self.guard.observe(**params)

    def _rows(self):
        req = self.ledger.get_request("req-fixture-suppressed")
        return [e for e in req["evidence"] or [] if e["type"] == "recurrence_observation"]

    def _projection(self, index=0):
        signature = self.guard.list_signatures()[0]["signature"]
        return self.guard.get(signature)["observations"][index]["ledger_projection"]

    def test_a_suppression_is_recorded_as_a_suppression(self):
        result = self._observe("native_run_1", update_ledger=False)
        self.assertIsNone(result.ledger_update)
        self.assertEqual(self._rows(), [])
        projection = self._projection()
        self.assertEqual(projection["state"], "suppressed")
        self.assertFalse(projection["applied"])
        self.assertIn("suppressed", projection["reason"])

    def test_resync_leaves_a_suppression_alone_and_says_so(self):
        self._observe("native_run_1", update_ledger=False)
        out = self.guard.resync_ledger(ledger=self.ledger)
        self.assertEqual(out["outstanding"], 0)
        self.assertEqual(out["recovered"], 0)
        self.assertEqual(len(out["suppressed_skipped"]), 1)
        self.assertEqual(out["suppressed_skipped"][0]["state"], "suppressed")
        self.assertEqual(self._rows(), [], "the refused row must not appear")

    def test_a_replay_honours_the_suppression(self):
        self._observe("native_run_1", update_ledger=False)
        replay = self._observe("native_run_1")
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.ledger_update["state"], "suppressed")
        self.assertFalse(replay.ledger_update["recorded"])
        self.assertEqual(self._rows(), [])

    def test_projecting_a_suppression_is_deliberate_and_recorded(self):
        self._observe("native_run_1", update_ledger=False)
        out = self.guard.resync_ledger(
            ledger=self.ledger, include_suppressed=True, unsuppressed_by="operator"
        )
        self.assertEqual(out["outstanding"], 1)
        self.assertEqual(out["recovered"], 1)
        self.assertEqual(out["projections"][0]["from_state"], "suppressed")
        self.assertEqual(len(self._rows()), 1)
        projection = self._projection()
        self.assertEqual(projection["state"], "applied")
        self.assertEqual(projection["previous_state"], "suppressed")
        self.assertEqual(projection["unsuppressed_by"], "operator")

    def test_a_crashed_projection_is_still_repaired(self):
        """The repair path itself must keep working; only suppression is exempt."""
        self._observe("native_run_1", ledger=FlakyLedger(self.ledger))
        self.assertEqual(self._projection()["state"], "failed")
        out = self.guard.resync_ledger(ledger=self.ledger)
        self.assertEqual(out["outstanding"], 1)
        self.assertEqual(out["recovered"], 1)
        self.assertEqual(len(self._rows()), 1)
        self.assertEqual(self._projection()["state"], "applied")

    def test_an_observation_with_no_request_is_not_an_outstanding_projection(self):
        self.guard.observe(
            project="fixture-org/fixture-repo",
            environment="harness",
            operation="ci:build",
            error="build failed: missing artifact",
            source="ci",
            attempt="ci-1",
            ledger=self.ledger,
        )
        signature = [
            s["signature"] for s in self.guard.list_signatures() if s["operation"] == "ci:build"
        ][0]
        projection = self.guard.get(signature)["observations"][0]["ledger_projection"]
        self.assertEqual(projection["state"], "not_applicable")
        out = self.guard.resync_ledger(ledger=self.ledger)
        self.assertEqual(out["outstanding"], 0)
        self.assertEqual(out["suppressed_skipped"], [])

    def test_the_resync_cli_reports_and_refuses_an_unattributed_unsuppress(self):
        self._observe("native_run_1", update_ledger=False)
        base = ["--state-dir", self.tmp, "resync-ledger"]
        reported = run_cli(base)
        self.assertEqual(reported.returncode, 0, reported.stderr)
        payload = json.loads(reported.stdout)
        self.assertEqual(payload["outstanding"], 0)
        self.assertEqual(len(payload["suppressed_skipped"]), 1)

        unattributed = run_cli(base + ["--include-suppressed"])
        self.assertEqual(unattributed.returncode, 1, unattributed.stdout)
        self.assertIn("--unsuppressed-by", unattributed.stderr)
        self.assertEqual(self._rows(), [])

        attributed = run_cli(base + ["--include-suppressed", "--unsuppressed-by", "operator"])
        self.assertEqual(attributed.returncode, 0, attributed.stderr)
        self.assertEqual(json.loads(attributed.stdout)["recovered"], 1)
        self.assertEqual(len(self._rows()), 1)


class TestProjectionReportsObservationTimeEvidence(TestSuppressedProjectionSurvives):
    """
    A projection recovered after later failures landed described the present as if
    it were the past: the evidence row for observation 1 read "occurrence 2 of
    signature ... status corrective_action_required".
    """

    def _details(self, row):
        return json.loads(row["details"])

    def test_a_live_projection_records_the_observation_it_is_about(self):
        self._observe("native_run_1")
        details = self._details(self._rows()[0])
        self.assertEqual(details["occurrence"], 1)
        self.assertEqual(details["status"], STATUS_OPEN)
        self.assertEqual(details["occurrence_evidence"], "observation_time")

    def test_a_late_projection_reports_the_occurrence_it_actually_was(self):
        self._observe("native_run_1", ledger=FlakyLedger(self.ledger))
        second = self._observe("native_run_2")
        self.assertEqual(second.occurrences, 2)

        out = self.guard.resync_ledger(ledger=self.ledger)
        self.assertEqual(out["recovered"], 1)
        rows = {r["id"]: r for r in self._rows()}
        late = [r for r in rows.values() if "occurrence 1" in r["summary"]]
        self.assertEqual(len(late), 1, rows)
        details = self._details(late[0])
        self.assertEqual(details["occurrence"], 1)
        self.assertEqual(details["status"], STATUS_OPEN)
        self.assertEqual(details["occurrence_evidence"], "observation_time")
        self.assertTrue(details["projected_by_recovery"])
        # The request's present state is still available, and labelled as present.
        self.assertEqual(details["occurrences_at_projection"], 2)
        self.assertEqual(details["status_at_projection"], STATUS_CORRECTIVE_ACTION_REQUIRED)

    def test_the_observation_time_snapshot_is_not_revised_by_a_later_supersede(self):
        first = self._observe("native_run_1")
        self._observe("native_run_2")
        self.guard.supersede_observation(
            observation_id=first.observation_id,
            disposition="superseded_attempt",
            reason="the first attempt was superseded by the re-dispatch",
            actor="lane",
        )
        snapshot = self.guard.get(first.signature)["observations"][0]["at_observation"]
        self.assertEqual(snapshot["occurrence"], 1)
        self.assertEqual(snapshot["status"], STATUS_OPEN)
        self.assertTrue(snapshot["counted"], "what was observed then does not change")
        self.assertEqual(self.guard.get(first.signature)["occurrences"], 1)

    def test_a_projection_with_no_snapshot_says_its_numbers_are_projection_time(self):
        """
        An observation recorded before snapshots were kept has no history to
        report, so the row carries projection-time numbers and says so rather than
        inventing a snapshot that was never taken.
        """
        self._observe("native_run_1", ledger=FlakyLedger(self.ledger))
        self._observe("native_run_2")
        with open(self.guard.store_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        signature = next(iter(data["signatures"]))
        for observation in data["signatures"][signature]["observations"]:
            observation.pop("at_observation", None)
        with open(self.guard.store_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

        out = self.guard.resync_ledger(ledger=self.ledger)
        self.assertEqual(out["recovered"], 1)
        late = [r for r in self._rows() if json.loads(r["details"])["projected_by_recovery"]]
        self.assertEqual(len(late), 1)
        details = self._details(late[0])
        self.assertEqual(details["occurrence_evidence"], "projection_time")
        self.assertIn("no observation-time snapshot", late[0]["summary"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
