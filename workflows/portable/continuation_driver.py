#!/usr/bin/env python3
"""
continuation_driver.py - Minimal continuation driver for the portable workflow core.

WHY THIS EXISTS
---------------
The coordinator and the Superboard execution adapter can each advance a request
by exactly one step. Nothing drove them repeatedly, so "autonomous start to
finish" meant a human re-invoking a one-shot command once per stage.

This module is that missing loop and nothing more. It is a *wrapper* around the
adapter's existing run_step, not a second scheduler: it owns no eligibility
logic, no routing, no preflight, no gate, and no state transitions. Every one of
those already lives in the coordinator and the adapter, and this driver would be
a competing implementation if it re-derived any of them.

WHAT IT GUARANTEES
------------------
Authorized identifiers only
    The driver is constructed with an explicit list of request ids and will
    never touch anything else. It does not scan the ledger for work, does not
    promote a request it noticed in passing, and does not invent a task when it
    runs out of authorized ones. An empty authorization list is an error, not an
    invitation to find something to do.

State reloaded every step
    Request state is re-read from the ledger before each dispatch, so an
    external actor editing the ledger between steps is observed rather than
    overwritten from a stale in-memory copy.

Real progress or stop
    Progress is measured, not assumed: a fingerprint of (state, head, evidence
    count, updated_at) is captured before and after every step. A step that
    reports success while changing nothing is treated as no progress and parks
    the request. The loop cannot spin.

Park, never poll
    Blocked, errored, awaiting-authorization, decision-blocked and no-progress
    requests are parked with a reason and left alone. The driver never sleeps in
    a tight loop waiting for the world to change.

Restart resumes, and never repeats a completed stage
    Every dispatch attempt is journalled with its outcome. Only a stage that
    actually completed is added to the completed-stage guard; blocked and
    failed attempts remain retryable only after their parked request is
    explicitly unparked.

One driver at a time
    A run holds an OS-level advisory lock for its state directory. A second
    driver fails immediately with the holder's pid rather than racing it. The
    lock is released by the OS if the process dies, so a crash does not wedge
    the next run.

Signals are handled
    SIGINT and SIGTERM request a stop. The current step is allowed to finish so
    a worker is never orphaned mid-write, then the journal is flushed, the lock
    released, and the original handlers restored.

WHAT IT REFUSES
---------------
The driver never merges, never pushes, never deploys, and never satisfies a
human authorization gate. `awaiting authorization` is a terminal parking state
for this driver. If an adapter result ever claims auto-merge or auto-deploy is
permitted, the run aborts immediately rather than continuing under a boundary it
does not recognise.

USAGE
-----
    from superboard_adapter import SuperboardExecutionAdapter
    from worker_backend import WorkerBackend
    from continuation_driver import ContinuationDriver

    adapter = SuperboardExecutionAdapter(
        state_dir=state_dir,
        worker_backend=WorkerBackend(state_dir=state_dir),
    )
    driver = ContinuationDriver(
        adapter=adapter,
        authorized_ids=["req-1234"],
        state_dir=state_dir,
    )
    outcome = driver.run()

The adapter is duck-typed. Anything exposing
`run_step(request_id=..., real_worker=...) -> result` with the documented result
attributes will drive, so this module imports nothing from the adapter and the
two can be loaded in either order.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import signal
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from ledger import FileLock, RequestLedger
except ImportError as e:  # pragma: no cover - packaging error, not a runtime path
    raise ImportError(f"continuation_driver requires the sibling ledger module: {e}")

JOURNAL_FILENAME = "continuation_journal.json"
LOCK_FILENAME = "continuation_driver.lock"
JOURNAL_SCHEMA_VERSION = "1.1"

#: Stages this driver dispatches. Anything else is the adapter's business.
DRIVABLE_STAGES = ("build", "qa", "review")

#: Ledger states the driver treats as finished for its purposes. `awaiting
#: authorization` is included deliberately: the human merge gate is where this
#: driver stops, by design.
TERMINAL_STATES = ("awaiting authorization", "integration", "live verification", "done")

#: Adapter statuses that end a request's participation in this run.
PARK_STATUSES = ("blocked", "error", "awaiting_authorization")

#: Park codes that mean a real, operator-actionable failure rather than an
#: expected stop. A run that ends on one of these exits non-zero.
FAILED_PARK_CODES = ("blocked", "error", "recurrence_blocked")

#: Adapter statuses whose failure is worth remembering across runs, so a second
#: identical one stops being retried blind.
OBSERVABLE_FAILURE_STATUSES = ("blocked", "error")

#: A bounded decision re-check may never become a tight poll. Even a caller
#: asking for a 1-second interval waits this long.
MIN_DECISION_SYNC_INTERVAL = 15.0

DEFAULT_MAX_STEPS = 24


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from an object or mapping. Adapter results are duck-typed."""
    if isinstance(obj, Mapping):
        value = obj.get(name, default)
    else:
        value = getattr(obj, name, default)
    return default if value is None else value


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class StepRecord:
    """One dispatched step, as journalled."""
    step_index: int
    request_id: str
    stage: str
    entry_head: Optional[str]
    entry_state: Optional[str]
    result_status: str
    result_reason: str
    result_head: Optional[str]
    exit_state: Optional[str]
    progressed: bool
    worker_ok: Optional[bool]
    worker_backend: Optional[str]
    artifacts: List[str] = field(default_factory=list)
    at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ParkRecord:
    """Why a request stopped participating in this run."""
    request_id: str
    reason_code: str
    reason: str
    stage: Optional[str] = None
    head: Optional[str] = None
    at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DriverOutcome:
    """Result of one continuation run."""
    run_id: str
    started_at: str
    finished_at: str
    authorized_ids: List[str]
    steps_executed: int
    stop_reason: str
    steps: List[Dict[str, Any]] = field(default_factory=list)
    parked: List[Dict[str, Any]] = field(default_factory=list)
    inflight: List[Dict[str, Any]] = field(default_factory=list)
    skipped_completed: List[Dict[str, Any]] = field(default_factory=list)
    resumed_from_journal: bool = False
    boundaries: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str)


class DriverLockError(RuntimeError):
    """Another driver already holds this state directory."""


class BoundaryViolation(RuntimeError):
    """An adapter result claimed a boundary this driver refuses to operate under."""


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------

class DriverJournal:
    """
    Durable record of which stages have already completed, and why requests are
    parked. Restart correctness depends only on this file and the ledger.

    Written atomically via a temp file plus os.replace, so a crash mid-write
    leaves the previous journal intact rather than a truncated one.
    """

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.data: Dict[str, Any] = {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "completed_stages": {},   # request_id -> [ {stage, entry_head, result_head, at} ]
            "stage_attempts": {},     # request_id -> [ {stage, outcome, ...} ]
            "inflight": {},           # request_id -> durable native dispatch record
            "parked": {},             # request_id -> ParkRecord dict
            "runs": [],               # run summaries, newest last
        }
        self.existed = False
        self.load()

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, ValueError):
            # A corrupt journal must not wedge the driver, but it must not be
            # silently trusted either: keep the defaults and note the loss.
            self.data["journal_recovered_from_corruption_at"] = _now()
            return
        if isinstance(loaded, dict):
            for key in ("completed_stages", "stage_attempts", "inflight", "parked", "runs"):
                if key in loaded:
                    self.data[key] = loaded[key]
            self.existed = True

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp_cdrv_", dir=directory, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, default=str)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise

    # -- completed stages -------------------------------------------------

    def stage_completed_at(self, request_id: str, stage: str, entry_head: Optional[str]) -> Optional[Dict[str, Any]]:
        """
        Return the journal record if this exact stage already completed from this
        exact commit. This is the restart guard: it does not consult the ledger,
        so it still holds when a ledger write was lost.
        """
        for rec in self.data["completed_stages"].get(request_id, []):
            if rec.get("stage") != stage:
                continue
            if rec.get("entry_head") == entry_head:
                return rec
        return None

    def record_completed(self, request_id: str, stage: str, entry_head: Optional[str],
                         result_head: Optional[str]) -> None:
        bucket = self.data["completed_stages"].setdefault(request_id, [])
        bucket.append({
            "stage": stage,
            "entry_head": entry_head,
            "result_head": result_head,
            "at": _now(),
        })

    def record_attempt(self, request_id: str, stage: str, entry_head: Optional[str],
                       result_head: Optional[str], status: str, outcome: str) -> None:
        """Record a dispatch outcome without conflating failure with completion."""
        bucket = self.data["stage_attempts"].setdefault(request_id, [])
        bucket.append({
            "stage": stage,
            "entry_head": entry_head,
            "result_head": result_head,
            "status": status,
            "outcome": outcome,
            "at": _now(),
        })

    def record_inflight(
        self, request_id: str, stage: str, entry_head: Optional[str],
        run_id: str, state: str,
    ) -> None:
        self.data["inflight"][request_id] = {
            "request_id": request_id,
            "stage": stage,
            "entry_head": entry_head,
            "run_id": run_id,
            "state": state,
            "at": _now(),
        }

    def clear_inflight(self, request_id: str) -> None:
        self.data["inflight"].pop(request_id, None)

    # -- parking ----------------------------------------------------------

    def park(self, record: ParkRecord) -> None:
        self.data["parked"][record.request_id] = record.to_dict()

    def unpark(self, request_id: str) -> None:
        self.data["parked"].pop(request_id, None)

    def parked_record(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self.data["parked"].get(request_id)

    def add_run(self, outcome: DriverOutcome) -> None:
        self.data["runs"].append({
            "run_id": outcome.run_id,
            "started_at": outcome.started_at,
            "finished_at": outcome.finished_at,
            "steps_executed": outcome.steps_executed,
            "stop_reason": outcome.stop_reason,
            "authorized_ids": outcome.authorized_ids,
        })
        # Keep the journal bounded; the interesting history is recent.
        if len(self.data["runs"]) > 200:
            self.data["runs"] = self.data["runs"][-200:]


# ---------------------------------------------------------------------------
# Run lock
# ---------------------------------------------------------------------------

#: Lock paths held by a DriverRunLock in THIS process, with the run that holds
#: each. The shared FileLock is thread-local re-entrant, so two drivers on one
#: thread would both "acquire" the same OS lock and race each other's journal.
#: This registry closes that, while the OS lock still handles other processes
#: and still releases on a crash.
_HELD_LOCKS: Dict[str, str] = {}
_HELD_LOCKS_GUARD = threading.Lock()


class DriverRunLock:
    """
    Fail-fast single-driver guard, in two layers that each cover what the other
    cannot.

    In-process: a registry of held paths, so a second driver constructed in the
    same process (or the same thread, where the underlying file lock is
    deliberately re-entrant) is refused rather than silently admitted.

    Cross-process: the OS advisory lock, which the kernel releases if this
    process dies, so a crash never wedges the next run.

    The sidecar holder file only names the holder in the error message. It is
    never consulted to decide whether the lock is held, because a stale sidecar
    must be unable to block a run or to falsely permit one.
    """

    def __init__(self, lock_path: str):
        self.lock_path = os.path.abspath(lock_path)
        self.info_path = self.lock_path + ".holder.json"
        self._lock: Optional[FileLock] = None
        self._registered = False

    def _holder_hint(self) -> str:
        try:
            with open(self.info_path, "r", encoding="utf-8") as fh:
                info = json.load(fh)
            return f"pid={info.get('pid')} run_id={info.get('run_id')} since={info.get('since')}"
        except (OSError, ValueError):
            return "holder unknown (no readable holder file)"

    def _refuse(self) -> "DriverLockError":
        return DriverLockError(
            f"Another continuation driver already holds {self.lock_path} "
            f"({self._holder_hint()}). Refusing to start a second driver over the "
            f"same state; stop the running one first."
        )

    def acquire(self, run_id: str) -> None:
        with _HELD_LOCKS_GUARD:
            if self.lock_path in _HELD_LOCKS:
                raise self._refuse()
            _HELD_LOCKS[self.lock_path] = run_id
            self._registered = True

        lock = FileLock(self.lock_path, timeout=0.0)
        try:
            lock.acquire()
        except TimeoutError:
            self._unregister()
            raise self._refuse()
        except BaseException:
            self._unregister()
            raise
        self._lock = lock
        try:
            with open(self.info_path, "w", encoding="utf-8") as fh:
                json.dump({"pid": os.getpid(), "run_id": run_id, "since": _now()}, fh)
        except OSError:
            pass

    def _unregister(self) -> None:
        if not self._registered:
            return
        with _HELD_LOCKS_GUARD:
            _HELD_LOCKS.pop(self.lock_path, None)
        self._registered = False

    def release(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        self._unregister()
        try:
            if os.path.exists(self.info_path):
                os.unlink(self.info_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------

class ContinuationDriver:
    """
    Drives an existing adapter's run_step repeatedly over an explicit list of
    authorized request ids, until real progress stops.
    """

    def __init__(
        self,
        adapter: Any,
        authorized_ids: Sequence[str],
        state_dir: Optional[str] = None,
        ledger: Any = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        real_worker: bool = True,
        journal_path: Optional[str] = None,
        lock_path: Optional[str] = None,
        decision_sync_attempts: int = 0,
        decision_sync_interval: float = 60.0,
        sleep_fn: Optional[Callable[[float], None]] = None,
        install_signal_handlers: bool = True,
    ):
        ids = [str(i).strip() for i in (authorized_ids or []) if str(i).strip()]
        if not ids:
            raise ValueError(
                "ContinuationDriver requires an explicit non-empty list of authorized "
                "request ids. It will not select work on its own."
            )
        # Preserve caller order, drop duplicates.
        seen: set = set()
        self.authorized_ids: List[str] = []
        for i in ids:
            if i not in seen:
                seen.add(i)
                self.authorized_ids.append(i)

        self.adapter = adapter
        self.state_dir = os.path.abspath(state_dir) if state_dir else self._infer_state_dir(adapter)
        self.max_steps = max(1, int(max_steps))
        self.real_worker = bool(real_worker)
        self.decision_sync_attempts = max(0, int(decision_sync_attempts))
        self.decision_sync_interval = max(MIN_DECISION_SYNC_INTERVAL, float(decision_sync_interval))
        self._sleep = sleep_fn or time.sleep
        self.install_signal_handlers = install_signal_handlers

        self.ledger = ledger or self._infer_ledger(adapter, self.state_dir)
        self.journal = DriverJournal(journal_path or os.path.join(self.state_dir, JOURNAL_FILENAME))
        self.lock = DriverRunLock(lock_path or os.path.join(self.state_dir, LOCK_FILENAME))

        self._stop_requested = False
        self._stop_signal: Optional[str] = None
        self._original_handlers: Dict[int, Any] = {}

    # -- wiring ------------------------------------------------------------

    @staticmethod
    def _infer_state_dir(adapter: Any) -> str:
        for probe in (lambda: adapter.state_dir, lambda: adapter.coordinator.state_dir):
            try:
                value = probe()
                if value:
                    return os.path.abspath(str(value))
            except Exception:
                continue
        return SCRIPT_DIR

    @staticmethod
    def _infer_ledger(adapter: Any, state_dir: str) -> Any:
        for probe in (lambda: adapter.ledger, lambda: adapter.coordinator.ledger):
            try:
                value = probe()
                if value is not None:
                    return value
            except Exception:
                continue
        return RequestLedger(state_dir=state_dir)

    def _decision_manager(self) -> Any:
        try:
            return self.adapter.coordinator.decision_mgr
        except Exception:
            return None

    # -- recurrence memory -------------------------------------------------
    #
    # Owned by recurrence_guard.py and soft-imported, so a partial export still
    # drives. What the driver asks of it is deliberately narrow: remember a
    # failure, and refuse a retry that already failed for the same reason twice.
    # It adds no eligibility logic and never overrides a gate.

    def _recurrence_guard(self) -> Any:
        try:
            from recurrence_guard import RecurrenceGuard
        except ImportError:
            return None
        return RecurrenceGuard(state_dir=self.state_dir)

    def _recurrence_refusal(self, request_id: str) -> Optional[str]:
        """
        Why this request must not be dispatched again unchanged, or None.

        An unreadable recurrence history is itself a refusal: a failure record
        that cannot be read must not be mistaken for an absence of failures.
        """
        guard = self._recurrence_guard()
        if guard is None:
            return None
        try:
            decision = guard.check_retry(request_id=request_id)
        except Exception as e:
            return (
                f"The recurrence history governing unchanged retry is unreadable "
                f"({type(e).__name__}: {e}). Repair it rather than dispatching blind."
            )
        if decision.allowed:
            return None
        return f"{decision.reason} {decision.required_action}".strip()

    def _observe_failure(
        self,
        request_id: str,
        stage: str,
        status: str,
        reason: str,
        head: Optional[str],
        run_id: str,
        native_run_id: Optional[str] = None,
        error_class: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Record one observed stage failure so a later run can refuse to repeat it.

        Whose failure this is decides the identity, and getting that wrong is how
        one real failure became several.

        When the step reports a native background attempt, the failure *is* that
        attempt's outcome, not a separate driver-level event: the worker already
        recorded it. The driver therefore ingests it under the attempt's own
        identity - the same operation, the same fault class and the same
        observation id the worker derived from the run id - so the guard
        recognises it as the event it already holds. That is what stops one native
        failure from counting twice (once as `worker:<stage>`, once as
        `driver:<stage>`, with two ledger evidence rows), and it is what stops a
        driver whose journal was lost from turning repeated re-reads of one
        completed ticket into fresh occurrences that close the gate and open a
        corrective item for failures that never happened.

        Without a native attempt, the failure is genuinely the driver's own - the
        adapter raised, or reported a status with no attempt behind it - and the
        identity has to distinguish a real second dispatch from a re-ingestion of
        the first. `run_id` alone cannot: it is a second-resolution timestamp plus
        a pid, so two runs inside one second in one process share it and the
        second failure would be swallowed as a duplicate. The journal's
        append-only attempt list gives a durable, monotonic dispatch ordinal that
        survives restart, which is exactly the identity of "this dispatch and no
        other".
        """
        guard = self._recurrence_guard()
        if guard is None:
            return None
        # The request already carries the session and the authoritative issue link;
        # both belong in an escalation so the notification owner can correlate it
        # without re-deriving either.
        req = self._load_request(request_id) or {}
        observation_id = None
        operation = f"driver:{stage}"
        attempt = None
        if native_run_id:
            try:
                from recurrence_guard import native_attempt_observation_id

                observation_id = native_attempt_observation_id(native_run_id)
                operation = f"worker:{stage}"
                attempt = native_run_id
            except Exception:
                observation_id = None
        if attempt is None:
            ordinal = len((self.journal.data.get("stage_attempts") or {}).get(request_id) or [])
            attempt = f"{run_id}|dispatch-{ordinal}"
        try:
            result = guard.observe(
                environment="harness",
                operation=operation,
                error=reason or f"adapter reported '{status}' with no reason",
                source="continuation_driver",
                request_id=request_id,
                head_sha=head,
                stage=stage,
                attempt=attempt,
                observation_id=observation_id,
                explicit_error_class=error_class,
                detail=f"adapter_status={status}",
                repo_root=getattr(self.adapter, "repo_root", None),
                canonical_link=(req.get("github") or {}).get("issue_url"),
                session=req.get("session"),
                ledger=self.ledger,
            )
        except Exception as e:
            # Advisory memory never converts a real failure into a different one.
            return {"recorded": False, "reason": f"{type(e).__name__}: {e}"}
        return result.to_dict()

    # -- state observation -------------------------------------------------

    def _load_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Re-read one request from the ledger. Fresh every step, never cached."""
        try:
            return self.ledger.get_request(request_id)
        except Exception:
            return None

    @staticmethod
    def _fingerprint(req: Optional[Mapping[str, Any]]) -> Tuple:
        """
        Observable progress signature. Two identical fingerprints across a step
        mean nothing moved, whatever the step claimed.
        """
        if not req:
            return ("missing",)
        evidence = req.get("evidence")
        evidence_count = len(evidence) if isinstance(evidence, (list, tuple)) else 0
        return (
            req.get("state"),
            req.get("head"),
            evidence_count,
            req.get("updated_at"),
            req.get("blocker") or "",
        )

    @staticmethod
    def _stage_for_state(state: Optional[str]) -> Optional[str]:
        """
        Map ledger state to the stage the adapter will run. This mirrors the
        adapter's own mapping and is used only to decide whether the driver has
        already journalled that stage; the adapter remains the authority on what
        actually runs.
        """
        if state in ("pending", "implementation"):
            return "build"
        if state == "QA":
            return "qa"
        if state == "review":
            return "review"
        return None

    # -- decision gate -----------------------------------------------------

    def _blocking_decisions(self, request_id: str) -> List[Dict[str, Any]]:
        """
        Unresolved decisions that block this request.

        A decision blocks while it is not answered by a genuine authorized human
        operator. Synthetic, agent-authored and unauthorized replies leave it
        blocking, which is the whole point of the decision workflow.
        """
        mgr = self._decision_manager()
        if mgr is None:
            return []
        try:
            decisions = mgr.list_decisions()
        except Exception:
            return []

        blocking = []
        for dec in decisions or []:
            if not isinstance(dec, Mapping):
                continue
            deps = dec.get("blocking_dependencies") or []
            if dec.get("request_id") != request_id and request_id not in deps:
                continue
            if not self._decision_is_authorized_answer(dec):
                blocking.append(dec)
        return blocking

    @staticmethod
    def _decision_is_authorized_answer(dec: Mapping[str, Any]) -> bool:
        """
        True only for a real answer from an authorized human operator.

        Requires all of: status "answered", an answer payload, human_operator
        provenance, not flagged as a test, a non-empty interpretation, and a
        responder on the decision's own authorized_responders list. Any weaker
        combination is exactly what the decision workflow is built to reject, so
        the driver must not resume on it.
        """
        if dec.get("status") != "answered":
            return False
        answer = dec.get("answer")
        if not isinstance(answer, Mapping):
            return False
        if answer.get("provenance") != "human_operator":
            return False
        if answer.get("is_test"):
            return False
        if not str(answer.get("interpretation") or "").strip():
            return False
        allowed = dec.get("authorized_responders") or []
        if allowed:
            responder = str(answer.get("responder") or "").lstrip("@").strip()
            normalized = {str(a).lstrip("@").strip() for a in allowed}
            if responder not in normalized:
                return False
        return True

    def _bounded_decision_resume(self, request_id: str,
                                 decisions: Sequence[Mapping[str, Any]]) -> Tuple[bool, str]:
        """
        Optional, bounded re-check of a decision that is blocking this request.

        Off unless the caller asks for attempts. Each attempt waits at least
        MIN_DECISION_SYNC_INTERVAL, performs one bounded sync through the
        coordinator's own one-shot sync, and resumes only when a real authorized
        answer is observed. The attempt count is finite, so this can never
        become an endless poll, and a stop signal ends it immediately.
        """
        if self.decision_sync_attempts <= 0:
            ids = ", ".join(str(d.get("decision_id")) for d in decisions) or "unknown"
            return False, (
                f"Blocked on unanswered decision(s) [{ids}]. Bounded decision re-check is "
                f"disabled, so the driver is parking rather than waiting."
            )

        ids = ", ".join(str(d.get("decision_id")) for d in decisions) or "unknown"
        for attempt in range(1, self.decision_sync_attempts + 1):
            if self._stop_requested:
                return False, f"Stop requested while waiting on decision(s) [{ids}]."
            self._sleep(self.decision_sync_interval)
            if self._stop_requested:
                return False, f"Stop requested while waiting on decision(s) [{ids}]."
            try:
                self.adapter.coordinator.sync_decisions_if_configured()
            except Exception as e:
                return False, (
                    f"Bounded decision re-check for [{ids}] could not sync on attempt "
                    f"{attempt}/{self.decision_sync_attempts}: {e}"
                )
            still_blocking = self._blocking_decisions(request_id)
            if not still_blocking:
                return True, (
                    f"Decision(s) [{ids}] answered by an authorized operator; resuming after "
                    f"{attempt} bounded re-check(s)."
                )
        return False, (
            f"Decision(s) [{ids}] still unanswered after {self.decision_sync_attempts} bounded "
            f"re-check(s) at {self.decision_sync_interval:.0f}s. Parking; a human answer is "
            f"required to continue."
        )

    # -- boundary assertion ------------------------------------------------

    @staticmethod
    def _assert_boundaries(result: Any, request_id: str) -> None:
        """
        Refuse to keep driving under a boundary this driver does not recognise.

        The driver never merges or deploys. If an adapter ever reports those as
        permitted, that is a contract change the driver must not paper over, so
        the run aborts.
        """
        boundaries = _attr(result, "boundaries", {}) or {}
        if not isinstance(boundaries, Mapping):
            return
        for key in ("auto_merge_allowed", "auto_deploy_allowed"):
            if boundaries.get(key) is True:
                raise BoundaryViolation(
                    f"Adapter reported {key}=True for request '{request_id}'. This driver never "
                    f"merges or deploys; aborting the run instead of continuing under an "
                    f"unrecognised boundary."
                )

    # -- signals -----------------------------------------------------------

    def _handle_signal(self, signum, _frame) -> None:
        self._stop_requested = True
        try:
            self._stop_signal = signal.Signals(signum).name
        except (ValueError, AttributeError):
            self._stop_signal = str(signum)

    def _install_signals(self) -> None:
        if not self.install_signal_handlers:
            return
        if threading.current_thread() is not threading.main_thread():
            # signal.signal is main-thread only. An embedded driver simply runs
            # without handlers rather than raising.
            return
        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                self._original_handlers[int(sig)] = signal.getsignal(sig)
                signal.signal(sig, self._handle_signal)
            except (ValueError, OSError, RuntimeError):
                self._original_handlers.pop(int(sig), None)

    def _restore_signals(self) -> None:
        for signum, handler in self._original_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError, RuntimeError):
                pass
        self._original_handlers.clear()

    # -- the loop ----------------------------------------------------------

    def run(self) -> DriverOutcome:
        """
        Drive the adapter until real progress stops. Always releases the lock and
        restores signal handlers, including on abort.
        """
        run_id = f"run_{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d_%H%M%S')}_{os.getpid()}"
        started = _now()
        steps: List[Dict[str, Any]] = []
        parked: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        inflight: List[Dict[str, Any]] = []
        stop_reason = "completed"
        error: Optional[str] = None

        self.lock.acquire(run_id)
        self._install_signals()

        # Requests parked by an earlier run stay parked. Resuming a parked
        # request is a human decision, not something a restart does silently.
        active: List[str] = []
        for rid in self.authorized_ids:
            prior = self.journal.parked_record(rid)
            if prior:
                parked.append(prior)
            else:
                active.append(rid)

        resumed = self.journal.existed
        step_index = 0

        try:
            while active and step_index < self.max_steps:
                if self._stop_requested:
                    stop_reason = f"stop requested via {self._stop_signal or 'signal'}"
                    break

                progressed_this_pass = False

                for rid in list(active):
                    if self._stop_requested or step_index >= self.max_steps:
                        break

                    req = self._load_request(rid)
                    if req is None:
                        rec = ParkRecord(rid, "missing",
                                         f"Request '{rid}' is not present in the ledger at "
                                         f"{getattr(self.ledger, 'ledger_path', self.state_dir)}.")
                        self.journal.park(rec)
                        parked.append(rec.to_dict())
                        active.remove(rid)
                        continue

                    state = req.get("state")
                    entry_head = req.get("head")

                    if state in TERMINAL_STATES:
                        rec = ParkRecord(
                            rid,
                            "terminal" if state != "awaiting authorization" else "awaiting_authorization",
                            f"Request '{rid}' is in state '{state}'."
                            + (" A human operator must authorize and perform the merge; this driver "
                               "never merges." if state == "awaiting authorization" else ""),
                            head=entry_head,
                        )
                        self.journal.park(rec)
                        parked.append(rec.to_dict())
                        active.remove(rid)
                        continue

                    stage = self._stage_for_state(state)
                    if stage not in DRIVABLE_STAGES:
                        rec = ParkRecord(rid, "unroutable",
                                         f"Request '{rid}' is in state '{state}', which this driver "
                                         f"does not dispatch a stage for.", head=entry_head)
                        self.journal.park(rec)
                        parked.append(rec.to_dict())
                        active.remove(rid)
                        continue

                    # Restart guard: a stage already completed from this exact
                    # commit is never dispatched twice.
                    done = self.journal.stage_completed_at(rid, stage, entry_head)
                    if done:
                        note = {
                            "request_id": rid,
                            "stage": stage,
                            "entry_head": entry_head,
                            "completed_at": done.get("at"),
                            "result_head": done.get("result_head"),
                            "reason": (
                                f"Stage '{stage}' for '{rid}' is already journalled complete from "
                                f"commit {entry_head}; refusing to run it again after restart."
                            ),
                        }
                        skipped.append(note)
                        rec = ParkRecord(rid, "already_completed", note["reason"],
                                         stage=stage, head=entry_head)
                        self.journal.park(rec)
                        parked.append(rec.to_dict())
                        active.remove(rid)
                        continue

                    # Decision gate, checked before dispatch so a worker is not
                    # spent on a request whose direction is unresolved.
                    blocking = self._blocking_decisions(rid)
                    if blocking:
                        resumable, message = self._bounded_decision_resume(rid, blocking)
                        if not resumable:
                            rec = ParkRecord(rid, "decision_blocked", message,
                                             stage=stage, head=entry_head)
                            self.journal.park(rec)
                            parked.append(rec.to_dict())
                            active.remove(rid)
                            continue

                    # Recurrence gate, checked before dispatch for the same reason as
                    # the decision gate: a step that already failed twice for one
                    # reason would spend a worker reproducing it.
                    recurrence_refusal = self._recurrence_refusal(rid)
                    if recurrence_refusal:
                        rec = ParkRecord(rid, "recurrence_blocked", recurrence_refusal,
                                         stage=stage, head=entry_head)
                        self.journal.park(rec)
                        parked.append(rec.to_dict())
                        active.remove(rid)
                        continue

                    before = self._fingerprint(req)

                    # --- the one and only dispatch: the adapter's own step ---
                    step_index += 1
                    try:
                        result = self.adapter.run_step(
                            request_id=rid,
                            real_worker=self.real_worker,
                        )
                    except Exception as e:
                        rec = ParkRecord(rid, "error",
                                         f"adapter.run_step raised for '{rid}' at stage '{stage}': "
                                         f"{type(e).__name__}: {e}", stage=stage, head=entry_head)
                        self._observe_failure(
                            request_id=rid, stage=stage, status="error",
                            reason=rec.reason, head=entry_head, run_id=run_id,
                        )
                        self.journal.park(rec)
                        parked.append(rec.to_dict())
                        active.remove(rid)
                        steps.append(StepRecord(
                            step_index=step_index, request_id=rid, stage=stage,
                            entry_head=entry_head, entry_state=state,
                            result_status="error", result_reason=rec.reason,
                            result_head=None, exit_state=state, progressed=False,
                            worker_ok=False, worker_backend=None,
                        ).to_dict())
                        self.journal.record_attempt(
                            rid, stage, entry_head, None, "error", "failed",
                        )
                        continue

                    self._assert_boundaries(result, rid)

                    status = str(_attr(result, "status", "unknown"))
                    reason = str(_attr(result, "status_reason", ""))
                    worker = _attr(result, "worker_result", None)
                    worker_ok = _attr(worker, "ok", None) if worker is not None else None
                    worker_backend = _attr(worker, "backend_name", None) if worker is not None else None
                    worker_artifacts = _attr(worker, "artifacts", []) if worker is not None else []
                    if not isinstance(worker_artifacts, list):
                        worker_artifacts = []
                    # The failing attempt's own identity, when the step ran one. The
                    # journal is not consulted for it: a lost journal must not turn a
                    # re-read of one completed attempt into a new occurrence.
                    worker_evidence = _attr(worker, "evidence", None) if worker is not None else None
                    if not isinstance(worker_evidence, Mapping):
                        worker_evidence = {}
                    native_run_id = str(
                        (_attr(worker, "native_run_id", None) if worker is not None else None)
                        or worker_evidence.get("native_run_id")
                        or ""
                    ) or None
                    native_error_class = worker_evidence.get("failure_error_class") or None

                    after_req = self._load_request(rid)
                    after = self._fingerprint(after_req)
                    progressed = after != before

                    steps.append(StepRecord(
                        step_index=step_index, request_id=rid, stage=stage,
                        entry_head=entry_head, entry_state=state,
                        result_status=status, result_reason=reason,
                        result_head=_attr(result, "head_sha", None) or (
                            after_req.get("head") if after_req else None),
                        exit_state=(after_req or {}).get("state"),
                        progressed=progressed,
                        worker_ok=worker_ok, worker_backend=worker_backend,
                        artifacts=[str(a) for a in worker_artifacts],
                    ).to_dict())

                    if status in ("prepared", "background_dispatched"):
                        pending = {
                            "request_id": rid,
                            "stage": stage,
                            "entry_head": entry_head,
                            "run_id": str(_attr(worker, "native_run_id", "") or ""),
                            "state": status,
                        }
                        self.journal.record_inflight(
                            rid, stage, entry_head, pending["run_id"], status,
                        )
                        self.journal.record_attempt(
                            rid, stage, entry_head, (after_req or {}).get("head"),
                            status, status,
                        )
                        self.journal.save()
                        inflight.append(pending)
                        active.remove(rid)
                        continue

                    result_head = (after_req or {}).get("head")
                    if status in ("blocked", "error"):
                        attempt_outcome = "blocked" if status == "blocked" else "failed"
                    elif progressed:
                        attempt_outcome = "completed"
                    else:
                        attempt_outcome = "no_progress"
                    self.journal.record_attempt(
                        rid, stage, entry_head, result_head, status, attempt_outcome,
                    )

                    if attempt_outcome == "completed":
                        self.journal.record_completed(
                            rid, stage, entry_head, result_head,
                        )
                        self.journal.clear_inflight(rid)
                        progressed_this_pass = True
                        self.journal.save()

                    if status in PARK_STATUSES:
                        code = "awaiting_authorization" if status == "awaiting_authorization" else status
                        rec = ParkRecord(rid, code, reason or f"Adapter reported '{status}'.",
                                         stage=stage, head=entry_head)
                        if status in OBSERVABLE_FAILURE_STATUSES:
                            # A native attempt owns its own identity; only a failure
                            # with no attempt behind it is identified by this run.
                            self._observe_failure(
                                request_id=rid, stage=stage, status=status,
                                reason=rec.reason, head=entry_head, run_id=run_id,
                                native_run_id=native_run_id,
                                error_class=native_error_class,
                            )
                        self.journal.park(rec)
                        parked.append(rec.to_dict())
                        active.remove(rid)
                        continue

                    if status in ("done", "completed"):
                        rec = ParkRecord(rid, "done", reason or "Adapter reported no further work.",
                                         stage=stage, head=entry_head)
                        self.journal.park(rec)
                        parked.append(rec.to_dict())
                        active.remove(rid)
                        continue

                    if not progressed:
                        # A step that reported success and changed nothing is the
                        # signature of a loop that would spin forever.
                        rec = ParkRecord(
                            rid, "no_progress",
                            f"Step reported '{status}' for stage '{stage}' but request '{rid}' is "
                            f"unchanged in the ledger (state, head, evidence and timestamp all "
                            f"identical). Parking rather than repeating the same step.",
                            stage=stage, head=entry_head,
                        )
                        self.journal.park(rec)
                        parked.append(rec.to_dict())
                        active.remove(rid)
                        continue

                if self._stop_requested:
                    stop_reason = f"stop requested via {self._stop_signal or 'signal'}"
                    break
                if step_index >= self.max_steps:
                    stop_reason = f"step ceiling reached ({self.max_steps})"
                    break
                if active and not progressed_this_pass:
                    stop_reason = "no authorized request made progress in a full pass"
                    break

            else:
                if not active:
                    stop_reason = (
                        "native background work is in flight"
                        if inflight else "every authorized request is parked or terminal"
                    )
                elif step_index >= self.max_steps:
                    stop_reason = f"step ceiling reached ({self.max_steps})"

        except BoundaryViolation as e:
            error = str(e)
            stop_reason = "aborted on boundary violation"
        finally:
            outcome = DriverOutcome(
                run_id=run_id,
                started_at=started,
                finished_at=_now(),
                authorized_ids=list(self.authorized_ids),
                steps_executed=step_index,
                stop_reason=stop_reason,
                steps=steps,
                parked=parked,
                skipped_completed=skipped,
                inflight=inflight,
                resumed_from_journal=resumed,
                boundaries={
                    "auto_merge_allowed": False,
                    "auto_deploy_allowed": False,
                    "self_spawn_loop": False,
                    "authorized_ids_only": True,
                    "real_worker": self.real_worker,
                },
                error=error,
            )
            try:
                self.journal.add_run(outcome)
                self.journal.save()
            finally:
                self._restore_signals()
                self.lock.release()

        return outcome


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Continuously drive the Superboard execution adapter's run_step over an "
                    "explicit list of authorized request ids."
    )
    p.add_argument("--request-id", action="append", default=[], dest="request_ids",
                   required=False,
                   help="Authorized request id (repeatable). Required; the driver never "
                        "selects work on its own.")
    p.add_argument("--state-dir", default=None, help="Directory holding ledger.json and driver state.")
    p.add_argument("--config", default=None, help="Superboard project config JSON.")
    p.add_argument("--repo-root", default=None,
                   help="Explicit git repository root workers execute in. Required for dispatch; "
                        "the installed workflows directory is never treated as the project.")
    p.add_argument("--worker-config", default=None, help="Worker backend config JSON.")
    p.add_argument("--model", default=None, help="Default model for the worker backend.")
    p.add_argument("--backend", default=None, help="Force a specific worker backend.")
    p.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                   help=f"Maximum dispatched steps for this run (default {DEFAULT_MAX_STEPS}).")
    p.add_argument("--decision-sync-attempts", type=int, default=0,
                   help="Bounded re-checks of a blocking decision before parking (default 0, off).")
    p.add_argument("--decision-sync-interval", type=float, default=60.0,
                   help=f"Seconds between bounded decision re-checks "
                        f"(floor {MIN_DECISION_SYNC_INTERVAL:.0f}s).")
    p.add_argument("--no-real-worker", action="store_true",
                   help="Pass real_worker=False to the adapter.")
    p.add_argument("--notify-telegram", action="store_true",
                   help="Enable Telegram notifications")
    p.add_argument("--telegram-dry-run", dest="telegram_dry_run", action="store_true", default=None,
                   help="Dry-run Telegram notification (default: True unless --telegram-send)")
    p.add_argument("--telegram-send", dest="telegram_send", action="store_true", default=False,
                   help="Send live Telegram notification")
    p.add_argument("--telegram-pool-db", dest="telegram_pool_db", default=None,
                   help="Path to the shared bot_pool.db holding the outbound message correlation "
                        "index (default: VEYYON_POOL_DB, else the installed pool when it exists)")
    p.add_argument("--show-parked", action="store_true",
                   help="Print the journal's parked requests and exit.")
    p.add_argument("--unpark", action="append", default=[], dest="unpark",
                   help="Clear a parked request so the next run may retry it (repeatable).")
    p.add_argument("--json", action="store_true", help="Emit the run outcome as JSON.")
    p.add_argument("--diagnostics", action="store_true",
                   help="Run aggregate diagnostic inspection across requests and services.")
    return p


def format_outcome(outcome: DriverOutcome) -> str:
    lines = [
        "=" * 70,
        "PORTABLE CONTINUATION DRIVER - RUN OUTCOME",
        "=" * 70,
        f"run id          : {outcome.run_id}",
        f"authorized ids  : {', '.join(outcome.authorized_ids)}",
        f"steps executed  : {outcome.steps_executed}",
        f"stop reason     : {outcome.stop_reason}",
        f"resumed journal : {outcome.resumed_from_journal}",
    ]
    if outcome.error:
        lines.append(f"ERROR           : {outcome.error}")
    if outcome.steps:
        lines.append("-" * 70)
        lines.append("STEPS")
        for s in outcome.steps:
            lines.append(
                f"  #{s['step_index']} {s['request_id']} stage={s['stage']} "
                f"{s['entry_state']} -> {s['exit_state']} status={s['result_status']} "
                f"progressed={s['progressed']} worker_ok={s['worker_ok']} "
                f"backend={s['worker_backend']}"
            )
            if s.get("artifacts"):
                lines.append(f"      artifacts: {', '.join(s['artifacts'])}")
            if s.get("result_reason"):
                lines.append(f"      {s['result_reason']}")
    if outcome.skipped_completed:
        lines.append("-" * 70)
        lines.append("SKIPPED (already completed at this commit)")
        for s in outcome.skipped_completed:
            lines.append(f"  {s['request_id']} stage={s['stage']} head={s['entry_head']}")
    if outcome.parked:
        lines.append("-" * 70)
        lines.append("PARKED")
        for p in outcome.parked:
            lines.append(f"  {p['request_id']} [{p['reason_code']}] {p['reason']}")
    if outcome.parked or outcome.error:
        lines.append("-" * 70)
        lines.append("DIAGNOSTIC HANDOFF")
        lines.append("  Inspect root causes, missing prerequisites and required operator inputs:")
        lines.append("  python diagnostics.py --summary")
    lines.append("-" * 70)
    lines.append(
        "boundaries      : auto_merge=False auto_deploy=False authorized_ids_only=True"
    )
    lines.append("=" * 70)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    state_dir = os.path.abspath(args.state_dir) if args.state_dir else SCRIPT_DIR

    if getattr(args, "diagnostics", False):
        try:
            from diagnostics import DiagnosticCollector, format_diagnostic_summary
            collector = DiagnosticCollector(state_dir=state_dir)
            rep = collector.run_diagnostics()
            if args.json:
                print(rep.to_json())
            else:
                print(format_diagnostic_summary(rep))
            return 0
        except Exception as e:
            sys.stderr.write(f"Diagnostics error: {e}\n")
            return 1

    if args.show_parked or args.unpark:
        journal = DriverJournal(os.path.join(state_dir, JOURNAL_FILENAME))
        for rid in args.unpark:
            inflight = journal.data.get("inflight", {}).get(rid) or {}
            native_run_id = inflight.get("run_id")
            if native_run_id:
                try:
                    from worker_backend import WorkerBackend
                    ticket = WorkerBackend(
                        config=args.worker_config,
                        state_dir=state_dir,
                        default_model=args.model,
                        default_backend=args.backend,
                    ).retry_native(native_run_id)
                except Exception as e:
                    print(
                        f"cannot unpark {rid}: native retry refused: {e}",
                        file=sys.stderr,
                    )
                    return 1
                journal.clear_inflight(rid)
                print(f"unparked: {rid} -> prepared native retry {ticket.run_id}")
            else:
                print(f"unparked: {rid}")
            journal.unpark(rid)
        if args.unpark:
            journal.save()
        if args.show_parked:
            parked = journal.data.get("parked", {})
            if not parked:
                print("no parked requests")
            for rid, rec in parked.items():
                print(f"{rid} [{rec.get('reason_code')}] {rec.get('reason')}")
        return 0

    if not args.request_ids:
        print("--request-id is required at least once; the driver never selects work on its own.",
              file=sys.stderr)
        return 64
    if not args.repo_root:
        print(
            "--repo-root is required for dispatch; refusing to infer a project from the installed "
            "workflow location.",
            file=sys.stderr,
        )
        return 64

    repo_root = os.path.abspath(args.repo_root)

    try:
        from superboard_adapter import SuperboardExecutionAdapter
    except ImportError as e:
        print(f"continuation_driver could not import the Superboard adapter: {e}", file=sys.stderr)
        return 70

    telegram_dry_run = (
        args.telegram_dry_run
        if args.telegram_dry_run is not None
        else (not args.telegram_send)
    )
    adapter_kwargs: Dict[str, Any] = {
        "state_dir": state_dir,
        "config_path": args.config,
        "fake_executor": False,
        "repo_root": repo_root,
        "notify_telegram": bool(args.notify_telegram),
        "telegram_dry_run": bool(telegram_dry_run),
        "telegram_send": bool(args.telegram_send and not args.telegram_dry_run),
    }
    # Passed only when named, so the driver still loads against an installed adapter
    # that predates the argument; unset means the adapter resolves the pool itself.
    if args.telegram_pool_db:
        adapter_kwargs["telegram_pool_db"] = args.telegram_pool_db

    if not args.no_real_worker:
        try:
            from worker_backend import WorkerBackend
        except ImportError as e:
            print(f"continuation_driver could not import the worker backend: {e}", file=sys.stderr)
            return 70
        adapter_kwargs["worker_backend"] = WorkerBackend(
            config=args.worker_config,
            state_dir=state_dir,
            default_model=args.model,
            default_backend=args.backend,
        )

    try:
        adapter = SuperboardExecutionAdapter(**adapter_kwargs)
    except TypeError as e:
        print(
            f"The installed Superboard adapter does not accept the expected keyword arguments "
            f"({e}). The driver refuses to guess an alternative call shape.",
            file=sys.stderr,
        )
        return 70

    driver = ContinuationDriver(
        adapter=adapter,
        authorized_ids=args.request_ids,
        state_dir=state_dir,
        max_steps=args.max_steps,
        real_worker=not args.no_real_worker,
        decision_sync_attempts=args.decision_sync_attempts,
        decision_sync_interval=args.decision_sync_interval,
    )

    try:
        outcome = driver.run()
    except DriverLockError as e:
        print(str(e), file=sys.stderr)
        return 75

    if args.json:
        print(outcome.to_json())
    else:
        print(format_outcome(outcome))

    failed_attempt = any(
        parked.get("reason_code") in FAILED_PARK_CODES
        for parked in outcome.parked
    )
    return 1 if outcome.error or failed_attempt else 0


if __name__ == "__main__":
    sys.exit(main())
