#!/usr/bin/env python3
"""
recurrence_guard.py - Durable failure recurrence persistence and corrective-action gates.

WHY THIS EXISTS
---------------
Every other module in the portable core is head-bound and single-step: it observes
one failure, reports it, and forgets it. Nothing remembered that *the same failure
already happened*, so the loop's honest answer to a repeated failure was to retry
it unchanged, forever, at the same cost and with the same result.

This module is that missing memory and nothing else. It is **not** a scheduler,
not a retry engine and not a second gate authority:

* It never dispatches, retries, merges, deploys, applies DDL, edits code, or runs
  a privileged operation. It records observations and answers one question:
  *may this be retried unchanged, or is a systemic corrective action owed first?*
* It never satisfies, weakens or bypasses an authorization gate, a head-bound QA
  or review approval, or an acceptance criterion. Recording a corrective action
  unblocks *retry* and nothing more.
* It owns no eligibility logic. `worker_backend.py` still decides what a valid
  worker outcome is; `continuation_driver.py` still decides what to dispatch;
  `ledger.py` remains the durable request record.

WHAT IT GUARANTEES
------------------
Stable signatures
    A failure is identified by ``project | environment | operation | error_class``.
    ``error_class`` is a normalized digest of the error text with volatile parts
    (paths, timestamps, uuids, hex digests, addresses, bare integers) replaced, so
    the same fault recurring in a different temp directory is still recognised as
    the same fault. Callers that know a better identity pass ``error_class``
    explicitly rather than relying on the heuristic.

Duplicate ingestion is not recurrence
    Every observation carries a unique observation id. Re-ingesting an already
    known observation id is idempotent: no new occurrence, no state change, no
    escalation, no ledger write. An id is either supplied by the caller (a native
    run id, a CI run id) or derived deterministically from the event's own durable
    identity, so replaying the same event can never manufacture a recurrence.

An intended failure is not a broken system
    Only observations dispositioned ``unexpected`` count. A negative control, a
    mutation probe or a baseline reproduction that is *supposed* to fail is
    ingested as ``expected_negative_control`` / ``superseded_attempt``: retained
    in history as the real executed evidence it is, and excluded from every
    threshold. This is deliberate rather than inferred, because a guard that
    treated every non-zero exit as a fresh fault would escalate off a test
    suite's own intended output. Note that ``worker_backend`` accepts non-zero
    check exits alongside a ``pass`` verdict for exactly that reason, so the
    worker intake fires only on a validated failing outcome and never on a
    passing result that retains a reproduction failure.

Recurrence changes behaviour instead of repeating it
    1st distinct occurrence  -> ``open``. Retry allowed; an actionable diagnosis,
    an owner and a next action are recorded and their absence is reported.
    2nd distinct occurrence  -> ``corrective_action_required``. Unchanged blind
    retry is refused until a systemic corrective action is *recorded*.
    3rd and beyond           -> ``escalated``, once per escalation epoch. An
    epoch closes when a corrective action or resolution is recorded, so ten
    identical restarts in ten minutes produce one notification event, not eight,
    and a recurrence *after* a correction opens a new one.

History survives, and correction can be undone by reality
    The store is a durable JSON file written atomically under the shared
    ``ledger.FileLock``, so it survives process restart, crash and compaction.
    Status and the retry gate are recomputed from that history on every load, so
    no stored flag can open a gate its own record says is closed. A corrective
    action recorded *before* a later observation is stale by construction: the
    new observation reopens the signature and blocks retry again. Nothing is ever
    deleted; the only way to take an occurrence out of the count is an audited
    ``supersede_observation`` naming an actor and a reason.

USAGE
-----
    from recurrence_guard import RecurrenceGuard

    guard = RecurrenceGuard(state_dir=state_dir)
    intake = guard.observe(
        project="Bavariance/polysimulator",
        environment="staging",
        operation="worker:qa",
        error="ledger add --criteria crashed: 'str' object has no attribute 'get'",
        source="native_worker",
        request_id="req-4582",
        head_sha=head,
        attempt=run_id,
        diagnosis="parse_criteria_arg returns raw JSON strings; add_request expects mappings",
        owner="RecurrenceGuard",
        next_action="Normalize criteria entries at the schema boundary",
    )
    if not guard.check_retry(request_id="req-4582").allowed:
        ...  # corrective action is owed; do not retry unchanged

CLI
---
    python recurrence_guard.py --state-dir DIR observe   ...
    python recurrence_guard.py --state-dir DIR check-retry --request-id req-1
    python recurrence_guard.py --state-dir DIR record-corrective-action ...
    python recurrence_guard.py --state-dir DIR list | show | escalations | resolve

Exit codes: 0 ok, 1 error, 3 gate refusal (retry blocked / corrective action refused).
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from ledger import FileLock

STORE_FILENAME = "recurrence.json"
STORE_VERSION = 1

#: Occurrence count at which an unchanged retry stops being acceptable.
CORRECTIVE_ACTION_THRESHOLD = 2
#: Occurrence count at which the failure stops being a local matter.
ESCALATION_THRESHOLD = 3

STATUS_OPEN = "open"
STATUS_CORRECTIVE_ACTION_REQUIRED = "corrective_action_required"
STATUS_CORRECTIVE_ACTION_RECORDED = "corrective_action_recorded"
STATUS_ESCALATED = "escalated"
STATUS_RESOLVED = "resolved"

VALID_STATUSES = (
    STATUS_OPEN,
    STATUS_CORRECTIVE_ACTION_REQUIRED,
    STATUS_CORRECTIVE_ACTION_RECORDED,
    STATUS_ESCALATED,
    STATUS_RESOLVED,
)

#: Where an observation came from. Deliberately a closed set: an unknown source
#: is a wiring mistake, and silently accepting it would hide untracked intake.
OBSERVATION_SOURCES = (
    "native_worker",
    "continuation_driver",
    "ci",
    "deploy",
    "tool",
    "manual",
)

#: What an observed failure *means*. Only `unexpected` counts toward the
#: recurrence gates; the others are retained in history and excluded from the
#: count. Without this distinction, a suite that deliberately exits non-zero -
#: a negative control, a mutation probe, a baseline reproduction that is supposed
#: to fail until it is fixed - would be ingested as a fresh unresolved system
#: failure and would drive an escalation loop off its own intended output.
#:
#: The two retained dispositions are why the store keeps every observation rather
#: than only the counted ones: the executed failure is real evidence and must
#: survive, it simply is not evidence that something is currently broken.
OBSERVATION_DISPOSITIONS = (
    "unexpected",
    "expected_negative_control",
    "superseded_attempt",
)
DEFAULT_DISPOSITION = "unexpected"

#: Dispositions that count toward occurrence, escalation and the retry gate.
COUNTED_DISPOSITIONS = ("unexpected",)

#: Corrective action kinds, mapped to whether recording one asserts a privileged
#: act. A privileged kind is only *recordable* with an explicit authorization
#: reference, and recording it still executes nothing.
CORRECTIVE_ACTION_KINDS: Dict[str, bool] = {
    "code_change": False,
    "config_change": False,
    "process_change": False,
    "test_added": False,
    "monitoring_added": False,
    "input_correction": False,
    "ddl": True,
    "deployment": True,
    "privileged_operation": True,
    "gate_change": True,
}

MAX_ERROR_SAMPLE = 4000
MAX_NORMALIZED_ERROR = 512

_TRACEBACK_MARKER = "Traceback (most recent call last)"

#: Volatile substitutions, applied in order. Paths run before hex and integers
#: because a path contains both. Quoted strings are deliberately NOT collapsed:
#: for an error identity the quoted symbol usually *is* the discriminating part
#: ("'str' object has no attribute 'get'"), and merging distinct faults into one
#: class would invent recurrence that never happened.
#:
#: Hex and integer runs use explicit alphanumeric guards rather than \b, because
#: a word boundary does not fire after an underscore and would leave the volatile
#: half of an identifier like ``native_a98f185c20ba1b72`` in the digest. Path
#: segments deliberately exclude spaces: allowing them let a path swallow the
#: prose after it, which merges genuinely different errors - far worse than
#: splitting a Windows path with a space into a normalized prefix and a stable
#: literal tail.
_VOLATILE_PATTERNS: Sequence[Tuple[Any, str]] = (
    (re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"), "<uuid>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"(?:[A-Za-z]:)?(?:[\\/][\w.\-]+){2,}[\\/]?"), "<path>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
    (re.compile(r"(?<![0-9A-Za-z])[0-9a-f]{7,64}(?![0-9A-Za-z])"), "<hex>"),
    (re.compile(r"(?<![0-9A-Za-z])\d+(?![0-9A-Za-z])"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _clip(text: str, limit: int) -> str:
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[clipped {len(text) - limit} chars]"


def _sha(*parts: Any) -> str:
    raw = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class RecurrenceGuardError(RuntimeError):
    """Raised for a refused or malformed guard operation, never for a failure it records."""


# ---------------------------------------------------------------------------
# Error identity
# ---------------------------------------------------------------------------

def normalize_error(error: str) -> str:
    """
    Collapse an error message to its stable identity.

    A Python traceback is reduced to its final line first: the intermediate
    frames move with every refactor while the raised error is the fault. Then
    volatile substrings are replaced and the result is lowercased, collapsed and
    clipped, so an unbounded traceback tail cannot destabilise the digest.
    """
    text = str(error or "").strip()
    if not text:
        return ""
    if _TRACEBACK_MARKER in text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            text = lines[-1]
    for pattern, replacement in _VOLATILE_PATTERNS:
        text = pattern.sub(replacement, text)
    return _clip(text.strip().lower(), MAX_NORMALIZED_ERROR)


def error_class(error: str, explicit: Optional[str] = None) -> str:
    """
    The stable error-identity component of a signature.

    An explicit class from a caller that knows the fault's real identity always
    wins; otherwise it is derived from the normalized message.
    """
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    normalized = normalize_error(error)
    if not normalized:
        raise RecurrenceGuardError(
            "An observation needs either a non-empty error text or an explicit error_class; "
            "refusing to record a failure with no identity."
        )
    return "auto:" + _sha(normalized)[:16]


def compute_signature(project: str, environment: str, operation: str, err_class: str) -> str:
    """The durable signature: project + environment + operation + error class."""
    for name, value in (
        ("project", project),
        ("environment", environment),
        ("operation", operation),
        ("error_class", err_class),
    ):
        if not value or not str(value).strip():
            raise RecurrenceGuardError(f"A failure signature requires a non-empty '{name}'.")
    return _sha(
        str(project).strip(),
        str(environment).strip(),
        str(operation).strip(),
        str(err_class).strip(),
    )


def derive_observation_id(
    signature: str,
    source: str,
    request_id: Optional[str],
    head_sha: Optional[str],
    attempt: Optional[str],
    normalized_error: str,
) -> str:
    """
    Deterministic identity for one observed event.

    Every input is durable, so re-ingesting the same event yields the same id and
    is recognised as a duplicate. A genuinely new occurrence differs in at least
    one of them - in practice ``attempt`` (a native run id, a CI run id) or
    ``head_sha``. A caller that has a native unique id for the event should pass
    it as the observation id directly instead of relying on this derivation.
    """
    return "obs-" + _sha(
        signature, source, request_id or "", head_sha or "", attempt or "", normalized_error
    )[:24]


def resolve_project_identity(explicit: Optional[str], repo_root: Optional[str]) -> str:
    """
    One project identity shared by CLI intake and worker intake.

    Explicit wins. Otherwise the configured project adapter's repository is used,
    so a CI observation and a worker observation about the same repository land on
    the same signature. The repository directory name is the last resort.
    """
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    try:
        from project_adapter import get_current_project_config

        repo = getattr(get_current_project_config(), "repo", None)
        if repo and str(repo).strip():
            return str(repo).strip()
    except Exception:
        pass
    if repo_root and str(repo_root).strip():
        return os.path.basename(os.path.abspath(str(repo_root))) or str(repo_root).strip()
    raise RecurrenceGuardError(
        "Cannot resolve a project identity for this observation; pass an explicit project."
    )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class IntakeResult:
    """What one ingestion did, and what it now demands."""

    signature: str
    observation_id: str
    duplicate: bool
    occurrences: int
    status: str
    previous_status: Optional[str]
    retry_allowed: bool
    required_action: str
    reopened: bool = False
    disposition: str = DEFAULT_DISPOSITION
    counted: bool = True
    escalation: Optional[Dict[str, Any]] = None
    ledger_update: Optional[Dict[str, Any]] = None
    diagnosis_complete: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RetryDecision:
    """Whether unchanged retry is permitted, and why not."""

    allowed: bool
    reason: str
    blocking: List[Dict[str, Any]] = field(default_factory=list)
    required_action: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class RecurrenceGuard:
    """
    Durable recurrence store with corrective-action gates.

    Persistence reuses the primitives the rest of the core already relies on:
    the shared ``ledger.FileLock`` for cross-process exclusion and an atomic
    temp-file/fsync/replace write, so a crash mid-write cannot truncate history.
    """

    def __init__(self, state_dir: Optional[str] = None, store_path: Optional[str] = None):
        if store_path:
            self.store_path = os.path.abspath(store_path)
        elif state_dir:
            self.store_path = os.path.abspath(os.path.join(state_dir, STORE_FILENAME))
        elif os.environ.get("STATE_DIR"):
            self.store_path = os.path.abspath(
                os.path.join(os.environ["STATE_DIR"], STORE_FILENAME)
            )
        else:
            self.store_path = os.path.join(SCRIPT_DIR, STORE_FILENAME)
        self.lock_path = self.store_path + ".lock"

    # -- persistence -------------------------------------------------------

    @staticmethod
    def _empty() -> Dict[str, Any]:
        now = _now()
        return {
            "version": STORE_VERSION,
            "role": "durable_failure_recurrence_store",
            "authority": "advisory local recurrence memory; never a gate authority of its own",
            "created_at": now,
            "updated_at": now,
            "seq": 0,
            "signatures": {},
            "observation_index": {},
            "request_index": {},
        }

    def _load_unlocked(self) -> Dict[str, Any]:
        if not os.path.exists(self.store_path):
            return self._empty()
        try:
            with open(self.store_path, "r", encoding="utf-8") as fh:
                content = fh.read().strip()
        except OSError as e:
            raise RecurrenceGuardError(f"Recurrence store at {self.store_path} is unreadable: {e}")
        if not content:
            return self._empty()
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            # History is the point of this file. A corrupt store is reported, never
            # silently replaced with an empty one that would erase every occurrence.
            raise RecurrenceGuardError(
                f"Corrupt recurrence store at {self.store_path}: {e}. Repair or move it; "
                "refusing to discard recurrence history."
            )
        if not isinstance(data, dict) or not isinstance(data.get("signatures"), dict):
            raise RecurrenceGuardError(
                f"Recurrence store at {self.store_path} is not a recurrence store document."
            )
        data.setdefault("observation_index", {})
        data.setdefault("request_index", {})
        data.setdefault("seq", 0)
        # Status and the retry gate are recomputed on every load, so no read path
        # can be answered from a stored flag. A hand-edited or half-written status
        # therefore cannot open a gate its own history says is closed.
        for sig, entry in data["signatures"].items():
            if not isinstance(entry, dict):
                raise RecurrenceGuardError(
                    f"Recurrence store at {self.store_path} holds a non-object entry for "
                    f"signature {sig!r}; repair it rather than reading past it."
                )
            self._derive(entry)
        return data

    def _save_unlocked(self, data: Dict[str, Any]) -> None:
        data["updated_at"] = _now()
        dir_name = os.path.dirname(self.store_path)
        os.makedirs(dir_name, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_recurrence_", dir=dir_name, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.store_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise

    # -- derivation --------------------------------------------------------

    @staticmethod
    def _last_seq(entries: Sequence[Mapping[str, Any]]) -> int:
        return max((int(e.get("seq") or 0) for e in entries), default=0)

    @classmethod
    def _escalation_epoch_start(cls, entry: Mapping[str, Any]) -> int:
        """The sequence number at which the current escalation epoch opened."""
        return max(
            cls._last_seq(entry.get("corrective_actions") or []),
            cls._last_seq(entry.get("resolutions") or []),
        )

    @classmethod
    def _needs_escalation(cls, entry: Mapping[str, Any]) -> bool:
        """
        Whether this occurrence is news, under one escalation per epoch.

        An epoch opens when a signature crosses the escalation threshold and closes
        when a corrective action or a resolution is recorded. Occurrences 4..N
        inside one epoch are the same unresolved incident, not new information: an
        observed staging daemon that restarted ten times in ten minutes on one
        identical migration signature must escalate once, not eight times. A
        recurrence *after* a recorded correction opens a new epoch, because a
        failure that came back despite a fix genuinely is new information.
        """
        epoch_start = cls._escalation_epoch_start(entry)
        return not any(
            int(esc.get("seq") or 0) > epoch_start for esc in entry.get("escalations") or []
        )

    @classmethod
    def _derive(cls, entry: Dict[str, Any]) -> Dict[str, Any]:
        """
        Recompute status and the retry gate from the entry's durable facts.

        Never trusts a stored status: everything is a function of the observation,
        corrective-action and resolution sequence numbers, so a restart, a manual
        edit or a partially written mutation cannot leave the gate disagreeing with
        the history it is supposed to enforce.

        Only observations dispositioned `unexpected` count. A retained negative
        control or a superseded attempt stays in `observations` as evidence of an
        executed failure, and stays out of every threshold, because a suite that is
        meant to fail is not a system that is currently broken.
        """
        observations = entry.get("observations") or []
        counted = [
            obs for obs in observations
            if str(obs.get("disposition") or DEFAULT_DISPOSITION) in COUNTED_DISPOSITIONS
        ]
        occurrences = len(counted)
        entry["occurrences"] = occurrences
        entry["observations_recorded"] = len(observations)
        entry["observations_retained_uncounted"] = len(observations) - occurrences
        last_obs = cls._last_seq(counted)
        last_action = cls._last_seq(entry.get("corrective_actions") or [])
        last_resolution = cls._last_seq(entry.get("resolutions") or [])

        corrected = last_action > last_obs
        resolved = last_resolution > last_obs

        entry["corrective_action_current"] = corrected
        entry["retry_blocked"] = bool(
            occurrences >= CORRECTIVE_ACTION_THRESHOLD and not (corrected or resolved)
        )

        if resolved:
            status = STATUS_RESOLVED
        elif occurrences >= ESCALATION_THRESHOLD:
            status = STATUS_CORRECTIVE_ACTION_RECORDED if corrected else STATUS_ESCALATED
        elif occurrences >= CORRECTIVE_ACTION_THRESHOLD:
            status = STATUS_CORRECTIVE_ACTION_RECORDED if corrected else STATUS_CORRECTIVE_ACTION_REQUIRED
        else:
            status = STATUS_OPEN
        entry["status"] = status

        diagnosis_complete = bool(
            str(entry.get("diagnosis") or "").strip()
            and str(entry.get("owner") or "").strip()
            and str(entry.get("next_action") or "").strip()
        )
        entry["diagnosis_complete"] = diagnosis_complete
        entry["required_action"] = cls._required_action(entry)
        return entry

    @staticmethod
    def _required_action(entry: Mapping[str, Any]) -> str:
        status = entry.get("status")
        occurrences = int(entry.get("occurrences") or 0)
        if status == STATUS_RESOLVED:
            return (
                "Resolved and verified. A further observation reopens this signature and "
                "blocks unchanged retry again."
            )
        if status == STATUS_CORRECTIVE_ACTION_RECORDED:
            return (
                "A systemic corrective action is recorded and current. Retry is permitted; "
                "the corrective action is not QA, review or authorization and verifies nothing."
            )
        if status == STATUS_ESCALATED:
            return (
                f"Occurrence {occurrences} of an unresolved failure. Escalated for operator "
                "attention. Complete the corrective work item, then record the corrective "
                "action with its change reference before any further retry."
            )
        if status == STATUS_CORRECTIVE_ACTION_REQUIRED:
            return (
                "Second distinct occurrence: unchanged retry is refused. Implement the systemic "
                "change through the corrective work item's normal build/QA/review path, then "
                "record it with record-corrective-action --change-ref. 'Retry later' does not "
                "clear this gate."
            )
        if not entry.get("diagnosis_complete"):
            return (
                "First failure: record an actionable diagnosis, an owner and a next action "
                "(observe --diagnosis --owner --next-action)."
            )
        return "First failure with diagnosis, owner and next action recorded. Retry is permitted."

    # -- reads -------------------------------------------------------------

    def load(self) -> Dict[str, Any]:
        with FileLock(self.lock_path):
            return self._load_unlocked()

    def get(self, signature: str) -> Optional[Dict[str, Any]]:
        data = self.load()
        entry = data["signatures"].get(str(signature))
        return dict(entry) if entry else None

    def list_signatures(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        if status is not None and status not in VALID_STATUSES:
            raise RecurrenceGuardError(
                f"Unknown status '{status}'. Valid statuses: {list(VALID_STATUSES)}"
            )
        data = self.load()
        out = []
        for sig, entry in data["signatures"].items():
            if status and entry.get("status") != status:
                continue
            out.append(
                {
                    "signature": sig,
                    "project": entry.get("project"),
                    "environment": entry.get("environment"),
                    "operation": entry.get("operation"),
                    "error_class": entry.get("error_class"),
                    "occurrences": entry.get("occurrences"),
                    "status": entry.get("status"),
                    "retry_blocked": entry.get("retry_blocked"),
                    "owner": entry.get("owner"),
                    "next_action": entry.get("next_action"),
                    "first_observed_at": entry.get("first_observed_at"),
                    "last_observed_at": entry.get("last_observed_at"),
                    "reopened_count": entry.get("reopened_count", 0),
                    "request_ids": entry.get("request_ids", []),
                }
            )
        out.sort(key=lambda e: (-int(e["occurrences"] or 0), str(e["signature"])))
        return out

    def pending_escalations(self) -> List[Dict[str, Any]]:
        """Escalation events recorded but not yet acknowledged by a sender."""
        data = self.load()
        pending = []
        for sig, entry in data["signatures"].items():
            for esc in entry.get("escalations") or []:
                if not esc.get("acknowledged_at"):
                    pending.append({"signature": sig, **esc})
        pending.sort(key=lambda e: int(e.get("seq") or 0))
        return pending

    # -- the retry gate ----------------------------------------------------

    def check_retry(
        self,
        signature: Optional[str] = None,
        request_id: Optional[str] = None,
        project: Optional[str] = None,
        environment: Optional[str] = None,
        operation: Optional[str] = None,
        err_class: Optional[str] = None,
    ) -> RetryDecision:
        """
        Answer whether an unchanged retry is permitted.

        Addressable three ways, because the caller's knowledge differs by seam: an
        exact signature, the four signature components, or a request id (what a
        driver or a worker retry actually holds at the moment it would retry).
        """
        data = self.load()
        signatures = data["signatures"]
        candidates: List[str] = []

        if signature:
            candidates = [str(signature)]
        elif project or environment or operation or err_class:
            candidates = [compute_signature(project, environment, operation, err_class)]
        elif request_id:
            candidates = [
                str(s) for s in (data["request_index"].get(str(request_id)) or [])
            ]
        else:
            raise RecurrenceGuardError(
                "check_retry needs a signature, the four signature components, or a request_id."
            )

        blocking = []
        for sig in candidates:
            entry = signatures.get(sig)
            if not entry or not entry.get("retry_blocked"):
                continue
            blocking.append(
                {
                    "signature": sig,
                    "status": entry.get("status"),
                    "occurrences": entry.get("occurrences"),
                    "operation": entry.get("operation"),
                    "environment": entry.get("environment"),
                    "error_class": entry.get("error_class"),
                    "diagnosis": entry.get("diagnosis"),
                    "owner": entry.get("owner"),
                    "next_action": entry.get("next_action"),
                    "required_action": entry.get("required_action"),
                }
            )

        if not blocking:
            return RetryDecision(
                allowed=True,
                reason=(
                    "No recurrence signature blocks this retry."
                    if candidates
                    else "No recurrence history for this identity."
                ),
            )

        first = blocking[0]
        return RetryDecision(
            allowed=False,
            reason=(
                f"Unchanged retry refused: failure signature {first['signature'][:16]} has "
                f"{first['occurrences']} distinct occurrences of "
                f"'{first['operation']}' in '{first['environment']}' with no current systemic "
                "corrective action. Repeating the same attempt would reproduce the same failure."
            ),
            blocking=blocking,
            required_action=str(first.get("required_action") or ""),
        )

    # -- intake ------------------------------------------------------------

    def observe(
        self,
        project: Optional[str] = None,
        environment: str = "",
        operation: str = "",
        error: str = "",
        source: str = "manual",
        request_id: Optional[str] = None,
        head_sha: Optional[str] = None,
        stage: Optional[str] = None,
        attempt: Optional[str] = None,
        observation_id: Optional[str] = None,
        explicit_error_class: Optional[str] = None,
        diagnosis: Optional[str] = None,
        owner: Optional[str] = None,
        next_action: Optional[str] = None,
        detail: Optional[str] = None,
        repo_root: Optional[str] = None,
        canonical_link: Optional[str] = None,
        session: Optional[str] = None,
        disposition: str = DEFAULT_DISPOSITION,
        ledger: Any = None,
        update_ledger: bool = True,
    ) -> IntakeResult:
        """
        Ingest one observed failure.

        Idempotent in the observation id: a duplicate returns the current state
        unchanged, writes nothing, and never escalates.

        `disposition` says whether this failure means something is broken now.
        A negative control, a mutation probe or a baseline reproduction that is
        supposed to fail is retained as evidence and excluded from the gates; only
        `unexpected` drives recurrence. Ingesting an intended failure as unexpected
        is how a guard like this turns a test suite's own output into an escalation
        loop, so the classification is explicit rather than inferred from an exit
        code.
        """
        if source not in OBSERVATION_SOURCES:
            raise RecurrenceGuardError(
                f"Unknown observation source '{source}'. Valid sources: {list(OBSERVATION_SOURCES)}"
            )
        if disposition not in OBSERVATION_DISPOSITIONS:
            raise RecurrenceGuardError(
                f"Unknown observation disposition '{disposition}'. Valid dispositions: "
                f"{list(OBSERVATION_DISPOSITIONS)}"
            )
        resolved_project = resolve_project_identity(project, repo_root)
        err_class = error_class(error, explicit_error_class)
        signature = compute_signature(resolved_project, environment, operation, err_class)
        normalized = normalize_error(error)
        obs_id = str(observation_id).strip() if observation_id and str(observation_id).strip() else (
            derive_observation_id(signature, source, request_id, head_sha, attempt, normalized)
        )

        with FileLock(self.lock_path):
            data = self._load_unlocked()
            known_signature = data["observation_index"].get(obs_id)
            if known_signature:
                entry = data["signatures"].get(known_signature)
                if entry is None:
                    raise RecurrenceGuardError(
                        f"Recurrence store at {self.store_path} indexes observation {obs_id!r} "
                        f"under signature {known_signature!r}, which is absent; repair it rather "
                        "than treating a known failure as new."
                    )
                self._derive(entry)
                stored = next(
                    (o for o in entry.get("observations") or []
                     if o.get("observation_id") == obs_id),
                    {},
                )
                # Idempotent replay: no occurrence, no escalation, no ledger write.
                # The stored disposition is reported, so a replayed negative control
                # never reads back as a counted failure.
                return IntakeResult(
                    signature=known_signature,
                    observation_id=obs_id,
                    duplicate=True,
                    occurrences=int(entry["occurrences"]),
                    status=str(entry["status"]),
                    previous_status=str(entry["status"]),
                    retry_allowed=not bool(entry["retry_blocked"]),
                    required_action=str(entry["required_action"]),
                    disposition=str(stored.get("disposition") or DEFAULT_DISPOSITION),
                    counted=bool(stored.get("counted", True)),
                    diagnosis_complete=bool(entry["diagnosis_complete"]),
                )

            now = _now()
            data["seq"] = int(data["seq"]) + 1
            seq = int(data["seq"])

            entry = data["signatures"].get(signature)
            if entry is None:
                entry = {
                    "signature": signature,
                    "project": resolved_project,
                    "environment": str(environment).strip(),
                    "operation": str(operation).strip(),
                    "error_class": err_class,
                    "error_sample": _clip(str(error or ""), MAX_ERROR_SAMPLE),
                    "normalized_error": normalized,
                    "first_observed_at": now,
                    "last_observed_at": now,
                    "diagnosis": "",
                    "owner": "",
                    "next_action": "",
                    "canonical_link": None,
                    "session": None,
                    "request_ids": [],
                    "observations": [],
                    "corrective_actions": [],
                    "resolutions": [],
                    "escalations": [],
                    "history": [],
                    "reopened_count": 0,
                }
                data["signatures"][signature] = entry
                previous_status = None
            else:
                previous_status = str(self._derive(entry)["status"])

            counts = disposition in COUNTED_DISPOSITIONS
            # A retained negative control never reopens a corrected signature: only a
            # failure that means something is broken now can undo a correction.
            reopened = counts and previous_status in (
                STATUS_CORRECTIVE_ACTION_RECORDED,
                STATUS_RESOLVED,
            )
            if reopened:
                entry["reopened_count"] = int(entry.get("reopened_count") or 0) + 1

            entry["observations"].append(
                {
                    "observation_id": obs_id,
                    "seq": seq,
                    "observed_at": now,
                    "source": source,
                    "disposition": disposition,
                    "counted": counts,
                    "request_id": request_id or None,
                    "head_sha": head_sha or None,
                    "stage": stage or None,
                    "attempt": attempt or None,
                    "error": _clip(str(error or ""), MAX_ERROR_SAMPLE),
                    "detail": _clip(str(detail or ""), MAX_ERROR_SAMPLE),
                }
            )
            entry["last_observed_at"] = now

            # First-failure triage fields are recorded once and only ever refined,
            # never overwritten with a later blank.
            for name, value in (
                ("diagnosis", diagnosis),
                ("owner", owner),
                ("next_action", next_action),
                ("canonical_link", canonical_link),
                ("session", session),
            ):
                if value and str(value).strip():
                    entry[name] = str(value).strip()

            if request_id:
                if request_id not in entry["request_ids"]:
                    entry["request_ids"].append(str(request_id))
                index = data["request_index"].setdefault(str(request_id), [])
                if signature not in index:
                    index.append(signature)

            data["observation_index"][obs_id] = signature
            self._derive(entry)

            entry["history"].append(
                {
                    "seq": seq,
                    "at": now,
                    "event": (
                        "reopened_on_recurrence" if reopened
                        else "observed" if counts
                        else f"retained_{disposition}"
                    ),
                    "observation_id": obs_id,
                    "disposition": disposition,
                    "counted": counts,
                    "occurrences": entry["occurrences"],
                    "from_status": previous_status,
                    "to_status": entry["status"],
                    "actor": owner or source,
                }
            )

            escalation = None
            if entry["status"] == STATUS_ESCALATED and self._needs_escalation(entry):
                escalation = self._build_escalation(entry, seq, now, request_id, canonical_link)
                entry["escalations"].append(escalation)

            self._save_unlocked(data)
            result = IntakeResult(
                signature=signature,
                observation_id=obs_id,
                duplicate=False,
                occurrences=int(entry["occurrences"]),
                status=str(entry["status"]),
                previous_status=previous_status,
                retry_allowed=not bool(entry["retry_blocked"]),
                required_action=str(entry["required_action"]),
                reopened=reopened,
                escalation=escalation,
                disposition=disposition,
                counted=counts,
            )
            result.diagnosis_complete = bool(entry["diagnosis_complete"])

        # The ledger is a separate durable authority with its own lock; it is
        # never written while this store's lock is held.
        if update_ledger and request_id:
            result.ledger_update = self._record_in_ledger(
                ledger=ledger, request_id=str(request_id), entry=entry, result=result
            )
        return result

    # -- escalation --------------------------------------------------------

    def _build_escalation(
        self,
        entry: Mapping[str, Any],
        seq: int,
        now: str,
        request_id: Optional[str],
        canonical_link: Optional[str],
    ) -> Dict[str, Any]:
        """
        Build one escalation as an existing-contract notification event.

        This module never sends. It produces the `NotificationEvent` payload the
        notifier already consumes plus that contract's own deduplication
        signature, so outbound delivery, correlation and rate limiting stay with
        the notification owner.
        """
        link = (
            (canonical_link or "").strip()
            or str(entry.get("canonical_link") or "").strip()
            or self._project_link(str(entry.get("project") or ""))
        )
        summary = (
            f"Recurring failure: occurrence {entry.get('occurrences')} of "
            f"'{entry.get('operation')}' in '{entry.get('environment')}' "
            f"({entry.get('error_class')}) with no current systemic corrective action. "
            f"Unchanged retry is blocked. Owner: {entry.get('owner') or 'unassigned'}. "
            f"Next action: {entry.get('next_action') or 'record a systemic corrective action'}."
        )
        event: Dict[str, Any] = {
            "event_type": "blocker",
            "project": str(entry.get("project") or ""),
            "request_id": str(request_id or (entry.get("request_ids") or [""])[0] or ""),
            "summary": summary,
            "canonical_link": link,
            "metadata": {
                "recurrence_signature": entry.get("signature"),
                "occurrences": entry.get("occurrences"),
                "operation": entry.get("operation"),
                "environment": entry.get("environment"),
                "error_class": entry.get("error_class"),
                "retry_blocked": entry.get("retry_blocked"),
                "reopened_count": entry.get("reopened_count", 0),
            },
        }

        record: Dict[str, Any] = {
            "escalation_id": "esc-" + _sha(entry.get("signature"), seq)[:16],
            "seq": seq,
            "at": now,
            "occurrences": entry.get("occurrences"),
            "notification_event": event,
            "acknowledged_at": None,
            "acknowledged_by": None,
        }

        # Reuse the notification contract's own signature so the sender's dedup
        # ledger and this record agree on identity. No local re-derivation of that
        # formula: a second implementation of it would drift.
        #
        # `session_id` is passed only when the installed contract declares it, so
        # this module works against a notifier with or without session binding and
        # never guesses at a field that installation does not have.
        try:
            import dataclasses

            from telegram_notifier import DeduplicationLedger, NotificationEvent

            kwargs = dict(event)
            session = str(entry.get("session") or "").strip()
            accepted = {f.name for f in dataclasses.fields(NotificationEvent)}
            if session and "session_id" in accepted:
                kwargs["session_id"] = session
                event["session_id"] = session
            candidate = NotificationEvent(**kwargs)
            candidate.validate()
            record["dedup_signature"] = DeduplicationLedger.compute_signature(candidate)
            record["event_valid"] = True
        except ImportError as e:
            record["dedup_signature"] = None
            record["event_valid"] = False
            record["event_invalid_reason"] = (
                f"Notification contract unavailable in this installation: {e}"
            )
        except (TypeError, ValueError) as e:
            record["dedup_signature"] = None
            record["event_valid"] = False
            record["event_invalid_reason"] = f"Escalation event does not satisfy the contract: {e}"
        return record

    @staticmethod
    def _project_link(project: str) -> str:
        """The project's canonical GitHub link, mirroring the notifier's own fallback."""
        cleaned = project.strip().strip("/")
        if re.fullmatch(r"[\w.\-]+/[\w.\-]+", cleaned):
            return f"https://github.com/{cleaned}"
        try:
            from project_adapter import get_current_project_config

            repo = getattr(get_current_project_config(), "repo", None)
            if repo and re.fullmatch(r"[\w.\-]+/[\w.\-]+", str(repo).strip()):
                return f"https://github.com/{str(repo).strip()}"
        except Exception:
            pass
        return "https://github.com"

    def acknowledge_escalation(self, escalation_id: str, acknowledged_by: str) -> Dict[str, Any]:
        """Mark one escalation handed off to the notification owner."""
        if not acknowledged_by or not str(acknowledged_by).strip():
            raise RecurrenceGuardError("Acknowledging an escalation requires an acknowledged_by.")
        with FileLock(self.lock_path):
            data = self._load_unlocked()
            for entry in data["signatures"].values():
                for esc in entry.get("escalations") or []:
                    if esc.get("escalation_id") == str(escalation_id):
                        if esc.get("acknowledged_at"):
                            return dict(esc)
                        esc["acknowledged_at"] = _now()
                        esc["acknowledged_by"] = str(acknowledged_by).strip()
                        self._save_unlocked(data)
                        return dict(esc)
        raise RecurrenceGuardError(f"No escalation '{escalation_id}' in the recurrence store.")

    # -- reclassification --------------------------------------------------

    def supersede_observation(
        self,
        observation_id: str,
        disposition: str,
        reason: str,
        actor: str,
    ) -> Dict[str, Any]:
        """
        Reclassify an already-recorded observation that turned out not to mean
        something is currently broken.

        The concrete case: a probe that exited non-zero on purpose after an
        existing test wrongly passed, where the misleading assertion was then
        removed and the real command passes. The executed failure is real evidence
        and stays in history; what changes is whether it counts toward the gates.

        Deliberately narrow. It cannot invent an observation, cannot delete one,
        cannot mark one `unexpected` that never was, requires an actor and a
        reason, and is written into the signature's history. It is the only way to
        take an occurrence out of the count, and it is auditable precisely because
        clearing a gate without evidence is the failure mode this whole module
        exists to prevent.
        """
        if disposition not in OBSERVATION_DISPOSITIONS:
            raise RecurrenceGuardError(
                f"Unknown disposition '{disposition}'. Valid dispositions: "
                f"{list(OBSERVATION_DISPOSITIONS)}"
            )
        if disposition in COUNTED_DISPOSITIONS:
            raise RecurrenceGuardError(
                f"supersede-observation only retains an observation out of the count. "
                f"Reclassifying one back to '{disposition}' would manufacture an occurrence; "
                "record a new observation instead."
            )
        if not reason or not str(reason).strip():
            raise RecurrenceGuardError(
                "Superseding an observation requires a reason: what showed this failure was "
                "intentional or obsolete."
            )
        if not actor or not str(actor).strip():
            raise RecurrenceGuardError("Superseding an observation requires an actor.")

        with FileLock(self.lock_path):
            data = self._load_unlocked()
            signature = data["observation_index"].get(str(observation_id))
            entry = data["signatures"].get(str(signature)) if signature else None
            if entry is None:
                raise RecurrenceGuardError(
                    f"No observation '{observation_id}' in the recurrence store."
                )
            observation = next(
                (o for o in entry.get("observations") or []
                 if o.get("observation_id") == str(observation_id)),
                None,
            )
            if observation is None:
                raise RecurrenceGuardError(
                    f"Observation '{observation_id}' is indexed under {signature} but absent "
                    "from its history; repair the store."
                )
            previous_disposition = str(observation.get("disposition") or DEFAULT_DISPOSITION)
            before = str(self._derive(entry)["status"])
            data["seq"] = int(data["seq"]) + 1
            seq = int(data["seq"])
            now = _now()
            observation["disposition"] = disposition
            observation["counted"] = False
            observation["superseded"] = {
                "seq": seq,
                "at": now,
                "from_disposition": previous_disposition,
                "reason": str(reason).strip(),
                "actor": str(actor).strip(),
            }
            self._derive(entry)
            entry.setdefault("history", []).append(
                {
                    "seq": seq,
                    "at": now,
                    "event": "observation_superseded",
                    "observation_id": str(observation_id),
                    "disposition": disposition,
                    "from_disposition": previous_disposition,
                    "occurrences": entry["occurrences"],
                    "from_status": before,
                    "to_status": entry["status"],
                    "actor": str(actor).strip(),
                    "reason": str(reason).strip(),
                }
            )
            self._save_unlocked(data)
            return {
                "signature": str(signature),
                "observation_id": str(observation_id),
                "disposition": disposition,
                "previous_disposition": previous_disposition,
                "occurrences": entry["occurrences"],
                "observations_recorded": entry["observations_recorded"],
                "status": entry["status"],
                "previous_status": before,
                "retry_allowed": not bool(entry["retry_blocked"]),
                "required_action": entry["required_action"],
            }

    # -- corrective action -------------------------------------------------

    def record_corrective_action(
        self,
        signature: str,
        kind: str,
        description: str,
        actor: str,
        authorization: Optional[str] = None,
        change_ref: Optional[str] = None,
        request_id: Optional[str] = None,
        head_sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Record - never execute - the systemic corrective action that unblocks retry.

        This writes a claim into durable history and clears the retry gate. It does
        not apply a change, does not touch acceptance criteria, does not authorize a
        merge or deployment, and does not substitute for head-bound QA or review. A
        privileged kind additionally requires an explicit authorization reference,
        because recording one asserts that a human already authorized it.

        `change_ref` is required for every kind. A corrective action that names no
        commit, PR, configuration or decision is indistinguishable from "retry
        later", and "retry later" is the behaviour this module exists to stop.
        """
        kind = str(kind or "").strip()
        if kind not in CORRECTIVE_ACTION_KINDS:
            raise RecurrenceGuardError(
                f"Unknown corrective action kind '{kind}'. Valid kinds: "
                f"{sorted(CORRECTIVE_ACTION_KINDS)}"
            )
        if not description or not str(description).strip():
            raise RecurrenceGuardError(
                "A corrective action requires a description of what systemically changed. "
                "An empty description would clear the retry gate while changing nothing."
            )
        if not change_ref or not str(change_ref).strip():
            raise RecurrenceGuardError(
                "A corrective action requires a change reference: the commit, PR, "
                "configuration or recorded decision that carries the systemic change. "
                "Without one this is 'retry later', which does not clear the gate."
            )
        if not actor or not str(actor).strip():
            raise RecurrenceGuardError("A corrective action requires an actor.")
        privileged = CORRECTIVE_ACTION_KINDS[kind]
        if privileged and not (authorization and str(authorization).strip()):
            raise RecurrenceGuardError(
                f"Corrective action kind '{kind}' asserts a privileged act, so it is only "
                "recordable with an explicit authorization reference naming the human who "
                "authorized it. This module never executes or authorizes one itself."
            )

        with FileLock(self.lock_path):
            data = self._load_unlocked()
            entry = data["signatures"].get(str(signature))
            if entry is None:
                raise RecurrenceGuardError(
                    f"No recurrence signature '{signature}' in the store; a corrective action "
                    "must attach to an observed failure."
                )
            before = str(self._derive(entry)["status"])
            data["seq"] = int(data["seq"]) + 1
            seq = int(data["seq"])
            now = _now()
            record = {
                "action_id": "ca-" + _sha(signature, seq)[:16],
                "seq": seq,
                "recorded_at": now,
                "kind": kind,
                "privileged": privileged,
                "description": str(description).strip(),
                "change_ref": (str(change_ref).strip() if change_ref else None),
                "authorization": (str(authorization).strip() if authorization else None),
                "actor": str(actor).strip(),
                "request_id": (str(request_id).strip() if request_id else None),
                "head_sha": (str(head_sha).strip() if head_sha else None),
                "occurrences_at_record": int(entry.get("occurrences") or 0),
                "executed": False,
                "verifies_nothing": (
                    "Recorded claim only. Clears the unchanged-retry gate; does not verify "
                    "acceptance criteria, does not satisfy head-bound QA or review, and does "
                    "not authorize a merge or deployment."
                ),
            }
            entry.setdefault("corrective_actions", []).append(record)
            self._derive(entry)
            entry.setdefault("history", []).append(
                {
                    "seq": seq,
                    "at": now,
                    "event": "corrective_action_recorded",
                    "action_id": record["action_id"],
                    "kind": kind,
                    "from_status": before,
                    "to_status": entry["status"],
                    "actor": record["actor"],
                }
            )
            self._save_unlocked(data)
            return {
                "signature": str(signature),
                "action": record,
                "status": entry["status"],
                "previous_status": before,
                "retry_allowed": not bool(entry["retry_blocked"]),
                "required_action": entry["required_action"],
            }

    def resolve(
        self,
        signature: str,
        head_sha: str,
        evidence: str,
        actor: str,
        scenario: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Close a signature against the corrective change and a re-executed scenario.

        Closure must point at both halves, because either one alone is what a
        false green looks like:

        * the corrective change — at least one recorded corrective action must be
          current, and its change references are copied into the resolution, so a
          closed signature always names what actually changed;
        * the original failure scenario — `scenario` names the exact failing
          scenario that was re-executed, and `evidence` carries what running it
          produced. A generic suite passing is not proof that this failure is gone.

        Resolution is an observation about one commit, not a claim of universal
        absence: a later observation reopens the signature and blocks retry again.
        """
        if not re.fullmatch(r"[0-9a-f]{40}", str(head_sha or "").strip().lower()):
            raise RecurrenceGuardError(
                "Resolving a recurrence requires the full 40-character commit the absence was "
                f"observed on (got '{head_sha}')."
            )
        if not evidence or not str(evidence).strip():
            raise RecurrenceGuardError(
                "Resolving a recurrence requires exercised evidence: the command, exit code or "
                "observation that showed the failure no longer occurs."
            )
        if not scenario or not str(scenario).strip():
            raise RecurrenceGuardError(
                "Resolving a recurrence requires the original failure scenario that was "
                "re-executed. Closing on a generic suite pass is what lets a defect stay open "
                "behind a green tick."
            )
        if not actor or not str(actor).strip():
            raise RecurrenceGuardError("Resolving a recurrence requires an actor.")

        with FileLock(self.lock_path):
            data = self._load_unlocked()
            entry = data["signatures"].get(str(signature))
            if entry is None:
                raise RecurrenceGuardError(f"No recurrence signature '{signature}' in the store.")
            before = str(self._derive(entry)["status"])
            # "Current" means recorded after the last counted observation: a corrective
            # action that predates the latest real failure did not hold, and closing on
            # it would claim a fix the failure already contradicted.
            last_counted = self._last_seq(
                [o for o in entry.get("observations") or [] if o.get("counted", True)]
            )
            current_actions = [
                a for a in (entry.get("corrective_actions") or [])
                if int(a.get("seq") or 0) > last_counted
            ]
            if not current_actions:
                raise RecurrenceGuardError(
                    f"Cannot resolve '{signature}': no systemic corrective action is recorded "
                    "and current for this signature. Closure must point at the corrective "
                    "change, not only at a scenario that happened to pass."
                )
            data["seq"] = int(data["seq"]) + 1
            seq = int(data["seq"])
            now = _now()
            record = {
                "resolution_id": "res-" + _sha(signature, seq)[:16],
                "seq": seq,
                "resolved_at": now,
                "head_sha": str(head_sha).strip().lower(),
                "scenario": str(scenario).strip(),
                "evidence": str(evidence).strip(),
                "actor": str(actor).strip(),
                "corrective_action_ids": [a["action_id"] for a in current_actions],
                "change_refs": [
                    a["change_ref"] for a in current_actions if a.get("change_ref")
                ],
                "scope": (
                    "Observed absent for this re-executed scenario on this commit, after the "
                    "named corrective change. No claim of universal absence; a later "
                    "observation reopens this signature."
                ),
            }
            entry.setdefault("resolutions", []).append(record)
            self._derive(entry)
            entry.setdefault("history", []).append(
                {
                    "seq": seq,
                    "at": now,
                    "event": "resolved",
                    "resolution_id": record["resolution_id"],
                    "change_refs": record["change_refs"],
                    "from_status": before,
                    "to_status": entry["status"],
                    "actor": record["actor"],
                }
            )
            self._save_unlocked(data)
            return {
                "signature": str(signature),
                "resolution": record,
                "status": entry["status"],
                "previous_status": before,
                "retry_allowed": not bool(entry["retry_blocked"]),
                "required_action": entry["required_action"],
            }

    # -- ledger intake -----------------------------------------------------

    def corrective_request_id(self, signature: str) -> str:
        """
        The stable ledger id of a signature's corrective work item.

        Derived from the signature, so creating it is idempotent: a second
        blocking occurrence finds the existing item instead of opening a duplicate.
        """
        return "req-corrective-" + str(signature)[:12]

    def _ensure_corrective_work_item(
        self, ledger: Any, entry: Mapping[str, Any], failing_request_id: Optional[str]
    ) -> Dict[str, Any]:
        """
        Open the durable corrective work item the second occurrence owes.

        A blocked flag is inert: it stops a retry and leaves nobody holding
        anything. What the second occurrence produces instead is a real ledger
        request the existing coordinator can pick up as work, with acceptance
        criteria that bind its closure to the two things closure actually needs -
        the corrective change, and the original failure scenario re-executed.

        This creates a *work item*; it does not do the work. Nothing here
        implements a fix, and the new request carries no authorization and no
        deployment applicability: an authorized lane implements it through the
        normal isolated build/QA/review path, under the same gates as any other
        request.
        """
        signature = str(entry.get("signature") or "")
        corrective_id = self.corrective_request_id(signature)
        try:
            if ledger.get_request(corrective_id):
                return {"created": False, "request_id": corrective_id, "reason": "already open"}
        except KeyError:
            pass
        except (OSError, ValueError) as e:
            return {"created": False, "reason": f"{type(e).__name__}: {e}"}

        operation = entry.get("operation")
        environment = entry.get("environment")
        diagnosis = str(entry.get("diagnosis") or "").strip()
        scenario = _clip(str(entry.get("error_sample") or ""), 600)
        prompt = (
            f"Systemic corrective work for a recurring failure: '{operation}' in "
            f"'{environment}' has failed {entry.get('occurrences')} distinct times with "
            f"error class {entry.get('error_class')}, so unchanged retry is refused.\n\n"
            f"Recurrence signature: {signature}\n"
            f"Failing request: {failing_request_id or '(none recorded)'}\n"
            f"Diagnosis: {diagnosis or '(not yet recorded)'}\n"
            f"Next action: {entry.get('next_action') or '(not yet recorded)'}\n\n"
            f"Original failure scenario to re-execute for closure:\n{scenario}"
        )
        criteria = [
            {
                "id": "AC-1",
                "description": (
                    f"A systemic change is implemented and referenced by commit, PR or "
                    f"configuration so that '{operation}' in '{environment}' no longer fails "
                    f"with {entry.get('error_class')}. Retrying the previous attempt unchanged "
                    "does not satisfy this."
                ),
            },
            {
                "id": "AC-2",
                "description": (
                    "The original failure scenario is re-executed on the corrected head and "
                    "observed absent, with the command, exit code and observation recorded. A "
                    "generic suite passing does not satisfy this."
                ),
            },
        ]
        # Whoever owns the failing request owns fixing why it keeps failing, so the
        # work item inherits that owner rather than landing unassigned.
        owner = str(entry.get("owner") or "").strip()
        if not owner and failing_request_id:
            try:
                owner = str((ledger.get_request(failing_request_id) or {}).get("owner") or "").strip()
            except (KeyError, OSError, ValueError):
                owner = ""
        try:
            ledger.add_request(
                req_id=corrective_id,
                prompt=prompt,
                session="recurrence-guard-corrective-intake",
                project=str(entry.get("project") or ""),
                acceptance_criteria=criteria,
                owner=owner or "unassigned",
                state="pending",
                # Deliberately a local work item: opening it must not assert that a
                # deployment or a DDL apply is in scope. A lane that needs those
                # states retypes the request under its own authorization.
                task_type="local",
                next_action=(
                    "Implement the systemic corrective change on an isolated branch, then "
                    "verify it through the normal build/QA/review path."
                ),
                issue_url=entry.get("canonical_link") or None,
                labels=[
                    "type:corrective-action",
                    f"recurrence:{signature[:12]}",
                    f"operation:{operation}",
                ],
            )
        except (KeyError, OSError, ValueError) as e:
            return {"created": False, "request_id": corrective_id,
                    "reason": f"{type(e).__name__}: {e}"}
        return {"created": True, "request_id": corrective_id}

    def _record_in_ledger(
        self,
        ledger: Any,
        request_id: str,
        entry: Mapping[str, Any],
        result: IntakeResult,
    ) -> Optional[Dict[str, Any]]:
        """
        Persist the recurrence into the request's durable record.

        On the failing request, writes only evidence, blocker and next_action - the
        ledger's own non-transitioning fields. It never changes state, head,
        acceptance criteria or authorization, so an authorization-aware recovery
        reading the ledger afterwards sees the same gates it saw before.

        When the gate closes, it also opens the separate corrective work item, so
        the second occurrence leaves real work in the coordinator's queue instead
        of only a flag saying not to retry.
        """
        try:
            if ledger is None:
                from ledger import RequestLedger

                ledger = RequestLedger(state_dir=os.path.dirname(self.store_path))
            existing = ledger.get_request(request_id)
        except (ImportError, KeyError, OSError, ValueError) as e:
            return {"recorded": False, "reason": f"{type(e).__name__}: {e}"}
        if not existing:
            return {
                "recorded": False,
                "reason": f"Request '{request_id}' is not in the ledger; recurrence kept locally.",
            }

        blocker = None
        next_action = None
        corrective: Optional[Dict[str, Any]] = None
        if entry.get("retry_blocked"):
            corrective = self._ensure_corrective_work_item(ledger, entry, request_id)
            corrective_id = corrective.get("request_id")
            blocker = (
                f"Recurring failure ({entry.get('occurrences')} distinct occurrences) in "
                f"'{entry.get('operation')}' on '{entry.get('environment')}': "
                f"{entry.get('error_class')}. Unchanged retry is refused pending a systemic "
                f"corrective action. Recurrence signature {entry.get('signature')}."
                + (f" Corrective work item: {corrective_id}." if corrective_id else "")
            )
            next_action = str(entry.get("required_action") or "")
            if corrective_id:
                next_action = (
                    f"Complete corrective work item {corrective_id} (systemic change plus "
                    f"original-scenario proof), then retry. {next_action}"
                ).strip()
        elif result.counted and result.occurrences == 1 and not entry.get("diagnosis_complete"):
            next_action = str(entry.get("required_action") or "")

        if result.counted:
            summary = (
                f"Failure observation {result.observation_id} recorded: occurrence "
                f"{result.occurrences} of signature {entry.get('signature')[:16]} "
                f"({entry.get('operation')} / {entry.get('environment')}); "
                f"status {result.status}"
            )
        else:
            # The executed failure is retained as evidence and explicitly does not
            # assert that anything is currently broken.
            summary = (
                f"Executed failure {result.observation_id} retained as "
                f"'{result.disposition}' and excluded from the recurrence count "
                f"({entry.get('operation')} / {entry.get('environment')}); "
                f"occurrences unchanged at {result.occurrences}"
            )

        try:
            ledger.update_request(
                request_id,
                blocker=blocker,
                next_action=next_action,
                add_evidence={
                    "type": "recurrence_observation",
                    "summary": summary,
                    "details": json.dumps(
                        {
                            "recurrence_signature": entry.get("signature"),
                            "observation_id": result.observation_id,
                            "disposition": result.disposition,
                            "counted": result.counted,
                            "observations_recorded": entry.get("observations_recorded"),
                            "occurrences": result.occurrences,
                            "status": result.status,
                            "retry_allowed": result.retry_allowed,
                            "error_class": entry.get("error_class"),
                            "diagnosis": entry.get("diagnosis"),
                            "owner": entry.get("owner"),
                            "required_action": entry.get("required_action"),
                            "corrective_work_item": (corrective or {}).get("request_id"),
                        },
                        default=str,
                    ),
                    "recorded_by": "RecurrenceGuard",
                },
                actor="RecurrenceGuard",
                reason=f"Recurrence intake for observation {result.observation_id}",
            )
        except (KeyError, OSError, ValueError) as e:
            return {"recorded": False, "reason": f"{type(e).__name__}: {e}"}
        return {
            "recorded": True,
            "request_id": request_id,
            "blocker_set": bool(blocker),
            "next_action_set": bool(next_action),
            "corrective_work_item": corrective,
        }


# ---------------------------------------------------------------------------
# Worker / driver integration helpers
# ---------------------------------------------------------------------------

def observe_worker_failure(
    state_dir: str,
    stage: str,
    request_id: str,
    repo_root: str,
    reason: str,
    run_id: Optional[str] = None,
    head_sha: Optional[str] = None,
    exit_code: Optional[int] = None,
    source: str = "native_worker",
    project: Optional[str] = None,
    environment: str = "harness",
) -> Optional[Dict[str, Any]]:
    """
    Intake seam for a real worker or driver failure.

    Best-effort by design: the recurrence store is advisory memory, so a store
    problem is reported to the caller and never converts a real, already-validated
    worker failure into a different one. ``run_id`` is the attempt identity, which
    is what makes a replayed completion a duplicate and a genuine new attempt a
    new occurrence.
    """
    try:
        guard = RecurrenceGuard(state_dir=state_dir)
        result = guard.observe(
            project=project,
            environment=environment,
            operation=f"worker:{stage}",
            error=str(reason or ""),
            source=source,
            request_id=request_id or None,
            head_sha=head_sha,
            stage=stage,
            attempt=run_id,
            detail=(f"exit_code={exit_code}" if exit_code is not None else None),
            repo_root=repo_root,
        )
        return result.to_dict()
    except Exception as e:  # advisory intake never masks the failure it records
        return {"recorded": False, "reason": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_GATE_REFUSED = 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Durable failure recurrence store and corrective-action gates. Records observed "
            "failures, refuses unchanged blind retry on recurrence, and emits escalation "
            "events through the existing deduplicated notification contract. Executes nothing."
        )
    )
    p.add_argument("--state-dir", default=None, help="Directory holding recurrence.json and ledger.json.")
    p.add_argument("--store", default=None, help="Explicit path to the recurrence store JSON.")
    p.add_argument("--summary", action="store_true", help="Human-readable output instead of JSON.")

    sub = p.add_subparsers(dest="command", required=True)

    obs = sub.add_parser("observe", help="Ingest one observed failure (idempotent per observation id).")
    obs.add_argument("--project", default=None, help="Project identity (default: configured repository).")
    obs.add_argument("--environment", required=True, help="Where it failed (staging, harness, ci, local).")
    obs.add_argument("--operation", required=True, help="What failed (worker:qa, ci:build, deploy:staging).")
    obs.add_argument("--error", default="", help="Observed error text. Required unless --error-class is given.")
    obs.add_argument("--error-class", default=None, help="Explicit stable error identity.")
    obs.add_argument("--source", default="manual", choices=list(OBSERVATION_SOURCES))
    obs.add_argument("--request-id", default=None, help="Ledger request this failure belongs to.")
    obs.add_argument("--head-sha", default=None, help="Commit the failure was observed on.")
    obs.add_argument("--stage", default=None, help="Workflow stage, when applicable.")
    obs.add_argument(
        "--attempt",
        default=None,
        help="Attempt identity (native run id, CI run id). Distinguishes a real new occurrence "
             "from a replay of the same event.",
    )
    obs.add_argument(
        "--observation-id",
        default=None,
        help="Explicit unique observation id. Preferred when the source has one; re-ingesting "
             "it is a duplicate and never a recurrence.",
    )
    obs.add_argument(
        "--disposition",
        default=DEFAULT_DISPOSITION,
        choices=list(OBSERVATION_DISPOSITIONS),
        help="Whether this failure means something is broken now. 'unexpected' (default) counts "
             "toward the recurrence gates; 'expected_negative_control' and 'superseded_attempt' "
             "are retained as evidence and excluded from the count. Never pipe every non-zero "
             "exit in as 'unexpected': a suite's intended failure is not a broken system.",
    )
    obs.add_argument("--diagnosis", default=None, help="Actionable diagnosis of the failure.")
    obs.add_argument("--owner", default=None, help="Who owns fixing it.")
    obs.add_argument("--next-action", default=None, help="The immediate next action.")
    obs.add_argument("--detail", default=None, help="Extra observed detail (command, exit code).")
    obs.add_argument("--repo-root", default=None, help="Repository root, for project identity fallback.")
    obs.add_argument("--canonical-link", default=None, help="GitHub issue/PR link for escalation.")
    obs.add_argument(
        "--session",
        default=None,
        help="Originating session id, carried into the escalation event for outbound correlation.",
    )
    obs.add_argument("--no-ledger-update", action="store_true", help="Do not write into the ledger.")

    chk = sub.add_parser(
        "check-retry",
        help="Gate one retry. Exit 0 when permitted, 3 when a corrective action is owed.",
    )
    chk.add_argument("--signature", default=None)
    chk.add_argument("--request-id", default=None)
    chk.add_argument("--project", default=None)
    chk.add_argument("--environment", default=None)
    chk.add_argument("--operation", default=None)
    chk.add_argument("--error-class", default=None)

    ca = sub.add_parser(
        "record-corrective-action",
        help="Record (never execute) the systemic corrective action that unblocks retry.",
    )
    ca.add_argument("--signature", required=True)
    ca.add_argument("--kind", required=True, choices=sorted(CORRECTIVE_ACTION_KINDS))
    ca.add_argument("--description", required=True, help="What systemically changed.")
    ca.add_argument("--actor", required=True)
    ca.add_argument("--change-ref", default=None, help="Commit, PR or config reference.")
    ca.add_argument(
        "--authorization",
        default=None,
        help="Explicit human authorization reference. Required for a privileged kind.",
    )
    ca.add_argument("--request-id", default=None)
    ca.add_argument("--head-sha", default=None)

    sup = sub.add_parser(
        "supersede-observation",
        help="Retain a recorded failure as evidence but take it out of the recurrence count.",
    )
    sup.add_argument("--observation-id", required=True)
    sup.add_argument(
        "--disposition",
        default="superseded_attempt",
        choices=[d for d in OBSERVATION_DISPOSITIONS if d not in COUNTED_DISPOSITIONS],
        help="Why it no longer counts: an intended negative control, or a superseded attempt.",
    )
    sup.add_argument(
        "--reason",
        required=True,
        help="What showed this failure was intentional or obsolete (evidence, not assertion).",
    )
    sup.add_argument("--actor", required=True)

    res = sub.add_parser("resolve", help="Close a signature against exercised evidence on one commit.")
    res.add_argument("--signature", required=True)
    res.add_argument("--head-sha", required=True, help="Full 40-character commit.")
    res.add_argument("--evidence", required=True, help="Exercised command, exit code or observation.")
    res.add_argument(
        "--scenario",
        required=True,
        help="The original failure scenario that was re-executed. A generic suite pass is not it.",
    )
    res.add_argument("--actor", required=True)

    lst = sub.add_parser("list", help="List known failure signatures.")
    lst.add_argument("--status", default=None, choices=list(VALID_STATUSES))

    show = sub.add_parser("show", help="Show one signature's full durable history.")
    show.add_argument("--signature", required=True)

    esc = sub.add_parser("escalations", help="List escalation events awaiting a sender.")
    esc.add_argument("--ack", default=None, help="Acknowledge one escalation id.")
    esc.add_argument("--acknowledged-by", default=None, help="Who took the handoff.")

    return p


def _print(payload: Any, summary_lines: Optional[List[str]], use_summary: bool) -> None:
    if use_summary and summary_lines is not None:
        print("\n".join(summary_lines))
    else:
        print(json.dumps(payload, indent=2, default=str))


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    guard = RecurrenceGuard(state_dir=args.state_dir, store_path=args.store)

    try:
        if args.command == "observe":
            if not str(args.error or "").strip() and not args.error_class:
                print(
                    "observe requires --error text or an explicit --error-class; refusing to "
                    "record a failure with no identity.",
                    file=sys.stderr,
                )
                return EXIT_ERROR
            result = guard.observe(
                project=args.project,
                environment=args.environment,
                operation=args.operation,
                error=args.error,
                source=args.source,
                request_id=args.request_id,
                head_sha=args.head_sha,
                stage=args.stage,
                attempt=args.attempt,
                observation_id=args.observation_id,
                explicit_error_class=args.error_class,
                diagnosis=args.diagnosis,
                owner=args.owner,
                next_action=args.next_action,
                detail=args.detail,
                repo_root=args.repo_root,
                canonical_link=args.canonical_link,
                session=args.session,
                disposition=args.disposition,
                update_ledger=not args.no_ledger_update,
            )
            payload = result.to_dict()
            lines = [
                f"signature      : {result.signature}",
                f"observation    : {result.observation_id}",
                f"duplicate      : {result.duplicate}",
                f"disposition    : {result.disposition} (counted={result.counted})",
                f"occurrences    : {result.occurrences}",
                f"status         : {result.previous_status or '(new)'} -> {result.status}",
                f"retry allowed  : {result.retry_allowed}",
                f"reopened       : {result.reopened}",
                f"required action: {result.required_action}",
            ]
            if result.escalation:
                lines.append(f"escalation     : {result.escalation.get('escalation_id')} "
                             f"(event_valid={result.escalation.get('event_valid')})")
            if result.ledger_update:
                lines.append(f"ledger         : {result.ledger_update}")
            _print(payload, lines, args.summary)
            return EXIT_OK

        if args.command == "check-retry":
            decision = guard.check_retry(
                signature=args.signature,
                request_id=args.request_id,
                project=args.project,
                environment=args.environment,
                operation=args.operation,
                err_class=args.error_class,
            )
            lines = [
                f"retry allowed  : {decision.allowed}",
                f"reason         : {decision.reason}",
            ]
            if decision.required_action:
                lines.append(f"required action: {decision.required_action}")
            _print(decision.to_dict(), lines, args.summary)
            return EXIT_OK if decision.allowed else EXIT_GATE_REFUSED

        if args.command == "supersede-observation":
            out = guard.supersede_observation(
                observation_id=args.observation_id,
                disposition=args.disposition,
                reason=args.reason,
                actor=args.actor,
            )
            lines = [
                f"signature      : {out['signature']}",
                f"observation    : {out['observation_id']}",
                f"disposition    : {out['previous_disposition']} -> {out['disposition']}",
                f"occurrences    : {out['occurrences']} "
                f"(of {out['observations_recorded']} recorded, all retained)",
                f"status         : {out['previous_status']} -> {out['status']}",
                f"retry allowed  : {out['retry_allowed']}",
            ]
            _print(out, lines, args.summary)
            return EXIT_OK

        if args.command == "record-corrective-action":
            out = guard.record_corrective_action(
                signature=args.signature,
                kind=args.kind,
                description=args.description,
                actor=args.actor,
                authorization=args.authorization,
                change_ref=args.change_ref,
                request_id=args.request_id,
                head_sha=args.head_sha,
            )
            lines = [
                f"signature      : {out['signature']}",
                f"action         : {out['action']['action_id']} ({out['action']['kind']})",
                f"status         : {out['previous_status']} -> {out['status']}",
                f"retry allowed  : {out['retry_allowed']}",
                f"scope          : {out['action']['verifies_nothing']}",
            ]
            _print(out, lines, args.summary)
            return EXIT_OK

        if args.command == "resolve":
            out = guard.resolve(
                signature=args.signature,
                head_sha=args.head_sha,
                evidence=args.evidence,
                scenario=args.scenario,
                actor=args.actor,
            )
            lines = [
                f"signature      : {out['signature']}",
                f"status         : {out['previous_status']} -> {out['status']}",
                f"head           : {out['resolution']['head_sha']}",
                f"change refs    : {', '.join(out['resolution']['change_refs']) or '(none)'}",
                f"scenario       : {out['resolution']['scenario']}",
                f"scope          : {out['resolution']['scope']}",
            ]
            _print(out, lines, args.summary)
            return EXIT_OK

        if args.command == "list":
            rows = guard.list_signatures(status=args.status)
            lines = [f"{len(rows)} signature(s)"]
            for row in rows:
                lines.append(
                    f"  {row['signature'][:16]} x{row['occurrences']} [{row['status']}] "
                    f"retry_blocked={row['retry_blocked']} {row['operation']} @ {row['environment']}"
                )
            _print(rows, lines, args.summary)
            return EXIT_OK

        if args.command == "show":
            entry = guard.get(args.signature)
            if entry is None:
                print(f"No recurrence signature '{args.signature}'.", file=sys.stderr)
                return EXIT_ERROR
            lines = [
                f"signature      : {entry['signature']}",
                f"project        : {entry['project']}",
                f"operation      : {entry['operation']} @ {entry['environment']}",
                f"error class    : {entry['error_class']}",
                f"occurrences    : {entry['occurrences']}  reopened: {entry.get('reopened_count', 0)}",
                f"status         : {entry['status']}  retry_blocked: {entry['retry_blocked']}",
                f"diagnosis      : {entry.get('diagnosis') or '(none)'}",
                f"owner          : {entry.get('owner') or '(none)'}",
                f"next action    : {entry.get('next_action') or '(none)'}",
                f"required action: {entry['required_action']}",
                "history        :",
            ]
            for h in entry.get("history") or []:
                lines.append(
                    f"  #{h['seq']} {h['at']} {h['event']} "
                    f"{h.get('from_status')} -> {h.get('to_status')}"
                )
            _print(entry, lines, args.summary)
            return EXIT_OK

        if args.command == "escalations":
            if args.ack:
                if not args.acknowledged_by:
                    print("--ack requires --acknowledged-by.", file=sys.stderr)
                    return EXIT_ERROR
                out = guard.acknowledge_escalation(args.ack, args.acknowledged_by)
                _print(
                    out,
                    [f"acknowledged   : {out['escalation_id']} by {out['acknowledged_by']}"],
                    args.summary,
                )
                return EXIT_OK
            pending = guard.pending_escalations()
            lines = [f"{len(pending)} pending escalation(s)"]
            for esc in pending:
                lines.append(
                    f"  {esc['escalation_id']} x{esc['occurrences']} "
                    f"dedup={str(esc.get('dedup_signature'))[:16]} "
                    f"{esc['notification_event']['summary'][:90]}"
                )
            _print(pending, lines, args.summary)
            return EXIT_OK

    except RecurrenceGuardError as e:
        print(f"[REFUSED] {e}", file=sys.stderr)
        return EXIT_GATE_REFUSED
    except (OSError, ValueError, KeyError) as e:
        print(f"[ERROR] {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR

    print(f"Unhandled command '{args.command}'.", file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
