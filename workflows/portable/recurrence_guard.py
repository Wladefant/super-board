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

One attempt is one occurrence, at every seam that sees it
    A native background attempt has exactly one durable identity, derived from
    its run id. The worker that finalises the attempt and the driver that later
    re-reads its terminal ticket ingest the *same* observation id, so one real
    failure is one occurrence with one ledger evidence row however many seams or
    restarts observe it. Identity never comes from an agent's free-text summary:
    a worker's non-pass verdict is identified by the structured verdict on that
    request at that commit, because prose gets reworded on every attempt and a
    reworded identical fault must still close the retry gate.

Diagnostics are redacted before they are persisted
    Every free-text field an observation carries is passed through
    ``redact_diagnostic`` *before* it reaches the store, so a credential in a
    failure reason is not written to disk, not embedded in a corrective work
    item's prompt, not copied into ledger evidence and not carried into an
    escalation. Redaction happens once, at intake, rather than at each of those
    egress points, because the store itself is the thing that outlives the
    incident.

The ledger projection is recoverable, not best-effort
    The store and the request ledger are two durable authorities with two locks,
    so a crash can land between them. Each observation therefore records whether
    its ledger projection was applied. A replay of that observation adds no
    occurrence and re-applies the missing projection, and ``resync-ledger``
    re-applies every outstanding one, so a failing request cannot end up blocked
    in the store yet unmarked in the ledger with no repair path.

Correction must be a verifiable, exercised change
    Clearing the retry gate requires a structured change reference, the original
    failure scenario, and the command and exit code that exercised it on a named
    commit. An arbitrary string is refused, so "retry later" cannot be spelled as
    a change reference. The corrective action still verifies nothing: head-bound
    QA, review, acceptance criteria and authorization are untouched.

Machine-authored work is not silently dispatchable
    The corrective work item the second occurrence opens is labelled for explicit
    selection only and names the parent request whose failure authorised it, so
    the coordinator's implicit "first runnable" selection never picks up work no
    operator scoped. Naming it explicitly still runs it.

Escalations are consumed and acknowledged, once
    ``deliver_escalations`` hands each pending escalation to the existing
    notification contract and acknowledges only what the sender actually took,
    so a rate-limited or blocked escalation stays pending instead of being lost,
    and an already-delivered one is never re-sent.

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
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from ledger import EXPLICIT_SELECTION_LABEL, FileLock

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

#: Kinds whose systemic change lives in the repository's own history, so their
#: change reference must name a commit rather than a decision or a config key.
COMMIT_BACKED_KINDS = ("code_change", "test_added")

#: The change-reference forms a corrective action may name, as (form, pattern).
#:
#: This closed set is the whole point. The previous contract accepted any
#: non-empty string, so ``--change-ref later`` cleared the retry gate and "retry
#: later" - the single behaviour this module exists to stop - was expressible as
#: a correction. A reference now has to identify something that can be looked up
#: by someone who did not write it: a commit, a pull request, a configuration key,
#: a recorded decision, or a named test.
CHANGE_REF_FORMS: Sequence[Tuple[str, Any]] = (
    ("commit", re.compile(r"^commit:(?P<value>[0-9a-fA-F]{40})$")),
    ("pr", re.compile(r"^pr:(?P<value>[\w.\-]+/[\w.\-]+#\d+)$")),
    ("pr", re.compile(r"^(?P<value>https://github\.com/[\w.\-]+/[\w.\-]+/pull/\d+)$")),
    ("config", re.compile(r"^config:(?P<value>[^\s#]+#[^\s#]+)$")),
    ("decision", re.compile(r"^decision:(?P<value>[\w.\-]+)$")),
    ("test", re.compile(r"^test:(?P<value>[^\s:]+::[^\s:]+)$")),
)

#: The shortest scenario or evidence text that can still say what was exercised.
#: Not a quality bar - a length bar, so a single filler word cannot occupy a
#: field whose whole job is to describe a real executed run.
MIN_EVIDENCE_CHARS = 12

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

#: Credential shapes that must never reach the durable store, applied in order.
#:
#: This module persists failure text for the lifetime of an incident and copies it
#: into a work item's prompt and into ledger evidence, so an unredacted DSN or
#: token in one failure reason outlives the process that produced it. Redaction
#: therefore happens at intake, before the first write, rather than at each place
#: the text is later read.
#:
#: The patterns below are the ones this module owns because the notifier's
#: ``SecretSanitizer`` does not cover them: URL userinfo (a Postgres DSN password)
#: and unquoted ``key=value`` assignments. ``redact_diagnostic`` then delegates to
#: ``SecretSanitizer`` for everything it already knows, rather than growing a
#: second copy of that pattern set here that would drift from it.
#:
#: Redaction is deterministic, so it happens before normalization and the error
#: class stays stable: the same failure text always yields the same redaction and
#: therefore the same signature.
_SECRET_PATTERNS: Sequence[Tuple[Any, str]] = (
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "<redacted-private-key>",
    ),
    # URL userinfo: the user half identifies the connection, the secret half does not.
    (
        re.compile(r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.\-]*://)(?P<user>[^\s:/@]+):[^\s/@]+@"),
        r"\g<scheme>\g<user>:<redacted-password>@",
    ),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/\-]{8,}={0,2}", re.IGNORECASE), "Bearer <redacted>"),
    # Named assignments, quoted or bare. The key is kept: which credential was
    # wrong is often the diagnosis, and only its value is the secret.
    (
        re.compile(
            r"(?i)\b(pass|passwd|password|secret|token|api[_-]?key|access[_-]?key|"
            r"secret[_-]?key|private[_-]?key|auth[_-]?token|service[_-]?role[_-]?key|"
            r"client[_-]?secret|connection[_-]?string)\b(\s*[:=]\s*)"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&)\]}]+)"
        ),
        r"\1\2<redacted>",
    ),
    (re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{8,}"), "<redacted-token>"),
    (re.compile(r"\bsbp_[A-Za-z0-9_\-]{8,}"), "<redacted-token>"),
    (re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{8,}"), "<redacted-token>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted-token>"),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}"), "<redacted-token>"),
    (re.compile(r"\bbot\d{6,}:[A-Za-z0-9_\-]{20,}"), "<redacted-token>"),
    (
        re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}"),
        "<redacted-jwt>",
    ),
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
# Redaction
# ---------------------------------------------------------------------------

def redact_diagnostic(text: Any) -> str:
    """
    Strip credentials out of one free-text diagnostic field.

    Called on every text field at intake, so nothing downstream has to remember
    to do it: the store, the corrective work item's prompt, ledger evidence and
    the escalation summary all read text that was already redacted on the way in.

    Composition rather than reimplementation: the module-owned patterns handle
    URL userinfo and bare ``key=value`` assignments, then the notifier's
    ``SecretSanitizer`` is applied for the shapes it already owns. A missing
    notifier only loses that second pass; the module-owned pass always runs, so a
    partial export still redacts.
    """
    if text is None:
        return ""
    out = str(text)
    if not out:
        return ""
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    try:
        from telegram_notifier import SecretSanitizer

        out = SecretSanitizer.sanitize(out)
    except ImportError:
        pass
    return out


def parse_change_ref(change_ref: Optional[str]) -> Tuple[str, str]:
    """
    Resolve a corrective action's change reference to (form, value).

    Refuses anything outside ``CHANGE_REF_FORMS``. This is the gate that stopped
    ``--change-ref later`` from clearing an unchanged-retry block: a reference has
    to name something a third party can look up, not assert that a change exists.
    """
    raw = str(change_ref or "").strip()
    if not raw:
        raise RecurrenceGuardError(
            "A corrective action requires a change reference: the commit, pull request, "
            "configuration key, recorded decision or test that carries the systemic change. "
            "Without one this is 'retry later', which does not clear the gate."
        )
    for form, pattern in CHANGE_REF_FORMS:
        match = pattern.match(raw)
        if match:
            return form, match.group("value")
    raise RecurrenceGuardError(
        f"Change reference {raw!r} is not a verifiable reference. Accepted forms: "
        "commit:<40-hex>, pr:<owner>/<repo>#<number>, "
        "https://github.com/<owner>/<repo>/pull/<number>, config:<path>#<key>, "
        "decision:<id>, test:<path>::<name>. An arbitrary string asserts a change "
        "instead of naming one, which is exactly how 'retry later' cleared this gate."
    )


def native_attempt_observation_id(run_id: str) -> str:
    """
    The one observation identity of a native background attempt.

    Every seam that observes the same attempt derives the same id from its run id:
    the worker that finalises it, and the driver that later re-reads its terminal
    ticket. That is what makes one real failure one occurrence with one ledger
    evidence row, instead of one per seam and one more per restart that re-read
    the completed ticket.
    """
    cleaned = str(run_id or "").strip()
    if not cleaned:
        raise RecurrenceGuardError(
            "A native attempt observation id requires the attempt's run id."
        )
    return "native-attempt-" + _sha(cleaned)[:24]


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
                "record it with record-corrective-action, naming a verifiable change reference "
                "and the original scenario re-executed against it (--change-ref --scenario "
                "--evidence --evidence-command --evidence-exit-code --head-sha). An arbitrary "
                "reference string is refused, so 'retry later' cannot clear this gate."
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
        # Redaction happens here, before the identity is derived and before the
        # first write, so no persisted field, prompt, evidence row or escalation
        # downstream can carry a credential that arrived in a failure reason.
        error = redact_diagnostic(error)
        detail = redact_diagnostic(detail) if detail else detail
        diagnosis = redact_diagnostic(diagnosis) if diagnosis else diagnosis
        next_action = redact_diagnostic(next_action) if next_action else next_action
        resolved_project = resolve_project_identity(project, repo_root)
        err_class = error_class(error, explicit_error_class)
        signature = compute_signature(resolved_project, environment, operation, err_class)
        normalized = normalize_error(error)
        obs_id = str(observation_id).strip() if observation_id and str(observation_id).strip() else (
            derive_observation_id(signature, source, request_id, head_sha, attempt, normalized)
        )

        duplicate_result: Optional[IntakeResult] = None
        duplicate_entry: Dict[str, Any] = {}
        duplicate_request_id: Optional[str] = None

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
                # Idempotent replay: no occurrence, no escalation, no state change.
                # The stored disposition is reported, so a replayed negative control
                # never reads back as a counted failure.
                duplicate_result = IntakeResult(
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
                # A replay is also the recovery path for a projection that never
                # landed. The store and the ledger are two authorities with two
                # locks, so a crash between them left the store blocking retry
                # while the failing request carried no blocker and no corrective
                # work item, and re-ingesting the event wrote nothing because it
                # was a duplicate. The replay now re-applies the outstanding
                # projection, which adds no occurrence because that is already
                # decided above.
                if not (stored.get("ledger_projection") or {}).get("applied"):
                    duplicate_request_id = str(stored.get("request_id") or "") or None
                duplicate_entry = dict(entry)

        if duplicate_result is not None:
            if update_ledger and duplicate_request_id:
                duplicate_result.ledger_update = self._project_to_ledger(
                    ledger=ledger,
                    request_id=duplicate_request_id,
                    entry=duplicate_entry,
                    result=duplicate_result,
                    recovery=True,
                )
            return duplicate_result

        with FileLock(self.lock_path):
            data = self._load_unlocked()

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
                    # Kept so a corrective action naming a commit can be verified
                    # against the repository the failure was observed in.
                    "repo_root": os.path.abspath(str(repo_root)) if repo_root else None,
                }
                data["signatures"][signature] = entry
                previous_status = None
            else:
                previous_status = str(self._derive(entry)["status"])
                if repo_root and not entry.get("repo_root"):
                    entry["repo_root"] = os.path.abspath(str(repo_root))

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
                    # Whether this observation's ledger projection landed. Written
                    # after the ledger write returns, so a crash in between leaves
                    # `applied` false and the replay path knows there is work left.
                    "ledger_projection": {
                        "applied": False,
                        "at": None,
                        "reason": "not attempted",
                        "attempts": 0,
                    },
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
        # never written while this store's lock is held. Whether the projection
        # landed is recorded back onto the observation, so a crash here is
        # recoverable by replay or by resync rather than silently lost.
        if update_ledger and request_id:
            result.ledger_update = self._project_to_ledger(
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

    def record_delivery_attempt(
        self, escalation_id: str, status: str, reason: str, delivered: bool
    ) -> Dict[str, Any]:
        """
        Append what one hand-off attempt returned, without acknowledging it.

        A rate-limited or refused escalation has to stay pending, or the incident
        is silently dropped; but the attempt is still a durable fact, so the next
        operator can see that delivery was tried and what the sender said.
        """
        with FileLock(self.lock_path):
            data = self._load_unlocked()
            for entry in data["signatures"].values():
                for esc in entry.get("escalations") or []:
                    if esc.get("escalation_id") != str(escalation_id):
                        continue
                    esc.setdefault("delivery_attempts", []).append(
                        {
                            "at": _now(),
                            "status": str(status),
                            "reason": redact_diagnostic(reason),
                            "delivered": bool(delivered),
                        }
                    )
                    self._save_unlocked(data)
                    return dict(esc)
        raise RecurrenceGuardError(f"No escalation '{escalation_id}' in the recurrence store.")

    def deliver_escalations(
        self,
        sender: Any,
        acknowledged_by: str,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        Hand every pending escalation to the notification owner, once.

        Recording an escalation and delivering it are different jobs, and until
        now this module only did the first: escalations accumulated as durable
        records that nothing consumed, so a third occurrence produced no
        notification at all. This is the consumer.

        `sender` is required and is never constructed here. It must expose the
        notification contract's own call, ``notify(event, dry_run=...)``,
        returning a receipt with ``delivered``, ``status`` and ``reason``. The
        transport, its credentials, its destination and its bot pool belong to the
        notification owner; this module must not decide any of them, and a default
        constructed here would silently reach whatever installation is on the box.

        What the receipt means for the escalation:

        * ``sent`` - the owner took it. Acknowledged, never offered again.
        * ``deduped`` - the owner already has an identical event inside its own
          window. That is a hand-off too, so it is acknowledged rather than
          retried into a duplicate.
        * ``cooldown`` / ``suppressed`` - rate limiting, not refusal. The attempt
          is recorded and the escalation stays pending for the next run.
        * ``dry_run`` - nothing was handed off, so nothing is acknowledged.
        * ``blocked`` / ``failed`` / a raised exception - recorded, still pending.

        Deduplication is the notifier's, not a second copy of it here: the store
        already emits one escalation per epoch, the acknowledgement marker stops
        re-consumption, and identical-event suppression stays with the sender that
        owns the outbound window.
        """
        if sender is None or not hasattr(sender, "notify"):
            raise RecurrenceGuardError(
                "Delivering escalations requires a sender exposing notify(event, dry_run=...). "
                "This module never constructs one: transport, credentials and destination "
                "belong to the notification owner."
            )
        if not dry_run and not str(acknowledged_by or "").strip():
            raise RecurrenceGuardError(
                "Delivering escalations requires an acknowledged_by naming who took the handoff."
            )

        try:
            from telegram_notifier import NotificationEvent
        except ImportError as e:
            raise RecurrenceGuardError(
                f"The notification contract is unavailable in this installation: {e}. "
                "Escalations stay pending rather than being delivered through a local "
                "re-implementation of it."
            )

        acknowledged: List[Dict[str, Any]] = []
        deferred: List[Dict[str, Any]] = []
        for pending in self.pending_escalations():
            escalation_id = str(pending.get("escalation_id"))
            payload = dict(pending.get("notification_event") or {})
            if not pending.get("event_valid", True) or not payload:
                deferred.append(
                    {
                        "escalation_id": escalation_id,
                        "status": "invalid_event",
                        "reason": str(
                            pending.get("event_invalid_reason")
                            or "the recorded escalation carries no valid notification event"
                        ),
                    }
                )
                continue
            try:
                event = NotificationEvent(**payload)
                event.validate()
            except (TypeError, ValueError) as e:
                deferred.append(
                    {
                        "escalation_id": escalation_id,
                        "status": "invalid_event",
                        "reason": f"{type(e).__name__}: {e}",
                    }
                )
                continue

            try:
                receipt = sender.notify(event, dry_run=dry_run)
                status = str(getattr(receipt, "status", "") or "unknown")
                reason = str(getattr(receipt, "reason", "") or "")
                delivered = bool(getattr(receipt, "delivered", False))
            except Exception as e:  # a transport fault must not lose the escalation
                status, reason, delivered = "failed", f"{type(e).__name__}: {e}", False

            handed_off = not dry_run and (delivered or status in ("sent", "deduped"))
            if handed_off:
                record = self.acknowledge_escalation(escalation_id, acknowledged_by)
                acknowledged.append(
                    {
                        "escalation_id": escalation_id,
                        "status": status,
                        "reason": reason,
                        "acknowledged_by": record.get("acknowledged_by"),
                        "acknowledged_at": record.get("acknowledged_at"),
                    }
                )
            else:
                self.record_delivery_attempt(escalation_id, status, reason, delivered)
                deferred.append(
                    {"escalation_id": escalation_id, "status": status, "reason": reason}
                )

        return {
            "considered": len(acknowledged) + len(deferred),
            "acknowledged": acknowledged,
            "still_pending": deferred,
            "dry_run": bool(dry_run),
        }

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

    @staticmethod
    def _observed_heads(entry: Mapping[str, Any]) -> List[str]:
        """Every commit a counted failure of this signature was observed on."""
        return [
            str(obs.get("head_sha") or "").strip().lower()
            for obs in entry.get("observations") or []
            if obs.get("counted", True) and obs.get("head_sha")
        ]

    @classmethod
    def _verify_change_ref(
        cls,
        entry: Mapping[str, Any],
        kind: str,
        ref_form: str,
        ref_value: str,
    ) -> Dict[str, Any]:
        """
        Check the change reference against the failure it claims to correct.

        Two checks, in order of how much they prove:

        * scope - a kind whose systemic change lives in the repository must name a
          commit, and that commit must not be one of the heads the failures were
          observed on. A "code change" that points at the failing commit itself is
          the unchanged retry it is supposed to replace, and no amount of prose
          around it makes the retried tree different.
        * existence - when the repository the failure was observed in is reachable,
          the commit is looked up with ``git cat-file``. A reference to an object
          that is not there is refused rather than recorded.

        A reference that cannot be checked (no recorded repository, or git absent)
        is recorded with that stated truthfully instead of being reported as
        verified. It still had to pass the form and scope checks to get here.
        """
        if kind in COMMIT_BACKED_KINDS and ref_form != "commit":
            raise RecurrenceGuardError(
                f"Corrective action kind '{kind}' changes the repository, so its change "
                f"reference must name a commit (commit:<40-hex>), not a '{ref_form}' "
                "reference. The commit is what makes the next retry a different attempt."
            )
        if ref_form != "commit":
            return {"checked": False, "reason": f"'{ref_form}' references are not git objects"}

        sha = ref_value.lower()
        observed = cls._observed_heads(entry)
        if sha in observed:
            raise RecurrenceGuardError(
                f"Change reference commit {sha} is one of the commits this failure was "
                "observed on, so it carries no systemic change: retrying against it is the "
                "same unchanged attempt that already failed."
            )

        repo_root = str(entry.get("repo_root") or "").strip()
        if not repo_root or not os.path.isdir(repo_root):
            return {
                "checked": False,
                "reason": (
                    "no reachable repository was recorded for this signature, so the commit's "
                    "existence could not be looked up"
                ),
                "commit": sha,
            }
        try:
            proc = subprocess.run(
                ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as e:
            return {
                "checked": False,
                "reason": f"git could not be run in {repo_root}: {type(e).__name__}: {e}",
                "commit": sha,
            }
        if proc.returncode != 0:
            raise RecurrenceGuardError(
                f"Change reference commit {sha} does not exist in {repo_root}, so it cannot "
                "be the systemic change that unblocks this retry."
            )
        return {"checked": True, "exists": True, "commit": sha, "repo_root": repo_root}

    def record_corrective_action(
        self,
        signature: str,
        kind: str,
        description: str,
        actor: str,
        change_ref: str,
        scenario: str,
        evidence: str,
        evidence_command: Sequence[str],
        evidence_exit_code: int,
        head_sha: str,
        authorization: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Record - never execute - the systemic corrective action that unblocks retry.

        What this demands, and why each part is load-bearing:

        * ``change_ref`` must be one of ``CHANGE_REF_FORMS``. The previous contract
          took any non-empty string, so ``--change-ref later`` with the description
          "will retry later" moved the signature to ``corrective_action_recorded``
          and reopened retry. "Retry later" is the exact behaviour this module
          exists to stop, so it must not be spellable as a correction.
        * for a kind whose change lives in the repository, the reference must name
          a commit that is *not* one of the heads the failures were observed on,
          and that commit is verified to exist when the observed repository is
          reachable. A correction that points at the failing commit itself did not
          change the thing being retried.
        * ``scenario``, ``evidence``, ``evidence_command`` and
          ``evidence_exit_code`` are the original failing scenario re-executed
          against the change. Requiring them here rather than only at ``resolve``
          is the point: the gate is cleared *here*, so this is where exercised
          proof has to exist.
        * ``head_sha`` is the full commit the evidence was exercised on.

        It still records a claim and executes nothing: no change is applied, no
        acceptance criterion is touched, no merge or deployment is authorized, and
        head-bound QA and review are unaffected. A privileged kind additionally
        requires an explicit authorization reference, because recording one asserts
        that a human already authorized it.
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
        if not actor or not str(actor).strip():
            raise RecurrenceGuardError("A corrective action requires an actor.")
        ref_form, ref_value = parse_change_ref(change_ref)
        scenario = redact_diagnostic(scenario)
        evidence = redact_diagnostic(evidence)
        description = redact_diagnostic(description)
        if len(str(scenario).strip()) < MIN_EVIDENCE_CHARS:
            raise RecurrenceGuardError(
                "A corrective action requires the original failure scenario it was exercised "
                "against. Clearing the retry gate without naming the scenario is how a "
                "recurrence stays open behind a recorded correction."
            )
        if len(str(evidence).strip()) < MIN_EVIDENCE_CHARS:
            raise RecurrenceGuardError(
                "A corrective action requires the observation the re-execution produced: "
                "what the scenario did after the change."
            )
        command = [str(c).strip() for c in (evidence_command or []) if str(c).strip()]
        if not command:
            raise RecurrenceGuardError(
                "A corrective action requires the command that exercised the scenario. A "
                "described change with no executed command is an assertion, not evidence."
            )
        if not isinstance(evidence_exit_code, int) or isinstance(evidence_exit_code, bool):
            raise RecurrenceGuardError(
                "A corrective action requires the integer exit code the exercising command "
                "returned."
            )
        head = str(head_sha or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{40}", head):
            raise RecurrenceGuardError(
                "A corrective action requires the full 40-character commit its evidence was "
                f"exercised on (got '{head_sha}'). Recording a correction against no commit "
                "leaves nothing for a later exact-head gate to disagree with."
            )
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
            ref_verification = self._verify_change_ref(entry, kind, ref_form, ref_value)
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
                "change_ref": str(change_ref).strip(),
                "change_ref_form": ref_form,
                "change_ref_value": ref_value,
                "change_ref_verification": ref_verification,
                "scenario": str(scenario).strip(),
                "evidence": str(evidence).strip(),
                "evidence_command": command,
                "evidence_exit_code": int(evidence_exit_code),
                "authorization": (str(authorization).strip() if authorization else None),
                "actor": str(actor).strip(),
                "request_id": (str(request_id).strip() if request_id else None),
                "head_sha": head,
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
        scenario = redact_diagnostic(scenario)
        evidence = redact_diagnostic(evidence)

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
                # The exercised proof each current corrective action carried. A
                # resolution therefore names both the change and the run that
                # showed the change worked, without re-deriving either.
                "corrective_evidence": [
                    {
                        "action_id": a["action_id"],
                        "change_ref": a.get("change_ref"),
                        "scenario": a.get("scenario"),
                        "command": a.get("evidence_command"),
                        "exit_code": a.get("evidence_exit_code"),
                        "head_sha": a.get("head_sha"),
                    }
                    for a in current_actions
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
        # Redacted on the way out as well as on the way in: an entry written by an
        # older build of this module holds raw text, and this prompt is where that
        # text would otherwise be copied into a second durable record.
        scenario = _clip(redact_diagnostic(entry.get("error_sample")), 600)
        prompt = (
            f"Systemic corrective work for a recurring failure: '{operation}' in "
            f"'{environment}' has failed {entry.get('occurrences')} distinct times with "
            f"error class {entry.get('error_class')}, so unchanged retry is refused.\n\n"
            f"Recurrence signature: {signature}\n"
            f"Failing request: {failing_request_id or '(none recorded)'}\n"
            f"Diagnosis: {diagnosis or '(not yet recorded)'}\n"
            f"Next action: {entry.get('next_action') or '(not yet recorded)'}\n\n"
            f"This work item was opened by the recurrence guard, not by an operator. It is "
            f"labelled '{EXPLICIT_SELECTION_LABEL}' and scoped to parent request "
            f"{failing_request_id or '(none recorded)'}: an implicit 'next runnable' "
            f"selection will skip it, and it runs when a caller names it explicitly under "
            f"that authorized scope.\n\n"
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
                    # Machine-authored work is real work, but no operator scoped it.
                    # The label keeps it out of every implicit selector while leaving
                    # it runnable the moment someone names it.
                    EXPLICIT_SELECTION_LABEL,
                    f"parent-scope:{failing_request_id or 'none'}",
                ],
            )
        except (KeyError, OSError, ValueError) as e:
            return {"created": False, "request_id": corrective_id,
                    "reason": f"{type(e).__name__}: {e}"}
        return {"created": True, "request_id": corrective_id}

    @staticmethod
    def _projection_evidence_id(observation_id: str) -> str:
        """
        The stable ledger evidence id for one observation's projection.

        A stable id is what makes the projection idempotent: re-applying it after
        a crash finds its own row instead of appending a second one, so recovery
        cannot inflate a request's evidence into a false history of repeats.
        """
        return f"ev-recurrence-{observation_id}"

    def _mark_ledger_projection(
        self, observation_id: str, applied: bool, reason: str
    ) -> None:
        """
        Record on the observation whether its ledger projection landed.

        Written under the store's lock after the ledger call returned, so the
        window between the two authorities is visible in the store rather than
        assumed away. Nothing here touches occurrence, status or the gate: the
        marker is bookkeeping about a projection, not evidence about a failure.
        """
        try:
            with FileLock(self.lock_path):
                data = self._load_unlocked()
                signature = data["observation_index"].get(str(observation_id))
                entry = data["signatures"].get(str(signature)) if signature else None
                if entry is None:
                    return
                for obs in entry.get("observations") or []:
                    if obs.get("observation_id") != str(observation_id):
                        continue
                    previous = obs.get("ledger_projection") or {}
                    obs["ledger_projection"] = {
                        "applied": bool(applied),
                        "at": _now(),
                        "reason": reason,
                        "attempts": int(previous.get("attempts") or 0) + 1,
                    }
                    self._save_unlocked(data)
                    return
        except (RecurrenceGuardError, OSError, ValueError):
            # The projection itself already happened or already failed; failing to
            # write the marker must not change what was reported about it.
            return

    def _project_to_ledger(
        self,
        ledger: Any,
        request_id: str,
        entry: Mapping[str, Any],
        result: IntakeResult,
        recovery: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Persist the recurrence into the request's durable record, recoverably.

        On the failing request, writes only evidence, blocker and next_action - the
        ledger's own non-transitioning fields. It never changes state, head,
        acceptance criteria or authorization, so an authorization-aware recovery
        reading the ledger afterwards sees the same gates it saw before.

        When the gate closes, it also opens the separate corrective work item, so
        the second occurrence leaves real work in the queue instead of only a flag
        saying not to retry.

        Two properties make it safe to call twice, which is what recovery needs:
        the evidence row carries a stable id derived from the observation, so a
        second call updates nothing and appends nothing; and the corrective work
        item's id is derived from the signature, so a second call finds it. The
        outcome is written back onto the observation either way, so an outstanding
        projection stays discoverable by ``resync_ledger`` instead of being lost
        in the gap between the two locks.
        """
        outcome = self._apply_ledger_projection(ledger, request_id, entry, result, recovery)
        self._mark_ledger_projection(
            result.observation_id,
            bool(outcome.get("recorded")),
            str(outcome.get("reason") or ("applied" if outcome.get("recorded") else "failed")),
        )
        return outcome

    def _apply_ledger_projection(
        self,
        ledger: Any,
        request_id: str,
        entry: Mapping[str, Any],
        result: IntakeResult,
        recovery: bool,
    ) -> Dict[str, Any]:
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

        evidence_id = self._projection_evidence_id(result.observation_id)
        already_recorded = any(
            isinstance(row, Mapping) and row.get("id") == evidence_id
            for row in existing.get("evidence") or []
        )
        add_evidence = None
        if not already_recorded:
            add_evidence = {
                "id": evidence_id,
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
                        "projected_by_recovery": bool(recovery),
                    },
                    default=str,
                ),
                "recorded_by": "RecurrenceGuard",
            }

        try:
            ledger.update_request(
                request_id,
                blocker=blocker,
                next_action=next_action,
                add_evidence=add_evidence,
                actor="RecurrenceGuard",
                reason=(
                    f"Recurrence projection recovery for observation {result.observation_id}"
                    if recovery
                    else f"Recurrence intake for observation {result.observation_id}"
                ),
            )
        except (KeyError, OSError, ValueError) as e:
            return {"recorded": False, "reason": f"{type(e).__name__}: {e}"}
        return {
            "recorded": True,
            "request_id": request_id,
            "blocker_set": bool(blocker),
            "next_action_set": bool(next_action),
            "evidence_appended": bool(add_evidence),
            "recovered": bool(recovery),
            "corrective_work_item": corrective,
            "reason": "applied by projection recovery" if recovery else "applied",
        }

    def resync_ledger(self, ledger: Any = None) -> Dict[str, Any]:
        """
        Re-apply every ledger projection that never landed.

        The repair path for a crash between the store write and the ledger write.
        Reads the store, finds each observation whose projection is still
        unapplied and belongs to a request, and projects it again. Occurrences,
        status and the retry gate are untouched: this replays a projection, it
        does not re-observe a failure.
        """
        data = self.load()
        outstanding: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
        for entry in data["signatures"].values():
            for obs in entry.get("observations") or []:
                if (obs.get("ledger_projection") or {}).get("applied"):
                    continue
                if not obs.get("request_id"):
                    continue
                outstanding.append((entry, obs))

        repaired: List[Dict[str, Any]] = []
        for entry, obs in outstanding:
            result = IntakeResult(
                signature=str(entry.get("signature") or ""),
                observation_id=str(obs.get("observation_id")),
                duplicate=True,
                occurrences=int(entry.get("occurrences") or 0),
                status=str(entry.get("status") or STATUS_OPEN),
                previous_status=str(entry.get("status") or STATUS_OPEN),
                retry_allowed=not bool(entry.get("retry_blocked")),
                required_action=str(entry.get("required_action") or ""),
                disposition=str(obs.get("disposition") or DEFAULT_DISPOSITION),
                counted=bool(obs.get("counted", True)),
                diagnosis_complete=bool(entry.get("diagnosis_complete")),
            )
            outcome = self._project_to_ledger(
                ledger=ledger,
                request_id=str(obs["request_id"]),
                entry=entry,
                result=result,
                recovery=True,
            )
            repaired.append(
                {
                    "observation_id": result.observation_id,
                    "request_id": str(obs["request_id"]),
                    "signature": result.signature,
                    "recorded": bool((outcome or {}).get("recorded")),
                    "reason": (outcome or {}).get("reason"),
                    "blocker_set": bool((outcome or {}).get("blocker_set")),
                    "corrective_work_item": (outcome or {}).get("corrective_work_item"),
                }
            )
        return {
            "outstanding": len(outstanding),
            "recovered": sum(1 for r in repaired if r["recorded"]),
            "still_outstanding": [r for r in repaired if not r["recorded"]],
            "projections": repaired,
        }


# ---------------------------------------------------------------------------
# Escalation hand-off targets
# ---------------------------------------------------------------------------

class OfflineEscalationOutbox:
    """
    A sender that hands escalations to a durable local outbox instead of a network.

    Not a stub of a transport: the hand-off is real and the file *is* the queue a
    notification owner reads. It exists because delivery configuration -
    credentials, destinations, the bot pool - belongs to the notification owner,
    and a consumer that had to construct a live transport to be usable would reach
    whatever installation happened to be on the box. Everything except the socket
    comes from the existing contract: the notifier formats the message and the
    notifier's deduplication ledger decides eligibility, against an explicitly
    named state file so nothing resolves to a shared or installed default.

    Its receipt is shaped like the contract's own: ``delivered``, ``status``,
    ``reason``. ``sent`` means the event was appended to the outbox, and that is
    what it claims - never that a message reached a person.
    """

    def __init__(self, outbox_path: str, dedup_state_file: Optional[str] = None):
        self.outbox_path = os.path.abspath(outbox_path)
        self.dedup_state_file = (
            os.path.abspath(dedup_state_file)
            if dedup_state_file
            else self.outbox_path + ".dedup.json"
        )

    @dataclass
    class Receipt:
        delivered: bool
        status: str
        reason: str

    def _dedup_ledger(self) -> Any:
        from pathlib import Path

        from telegram_notifier import DeduplicationLedger

        return DeduplicationLedger(state_file=Path(self.dedup_state_file))

    def notify(self, event: Any, dry_run: bool = False, force: bool = False) -> "OfflineEscalationOutbox.Receipt":
        from telegram_notifier import TelegramNotificationAdapter

        # `format_message` is a classmethod, so formatting costs no adapter
        # instance and therefore resolves no destination, credential or pool.
        message = TelegramNotificationAdapter.format_message(event)
        ledger = self._dedup_ledger()
        if not force:
            eligible, status, reason = ledger.check_eligible(event)
            if not eligible:
                return self.Receipt(delivered=False, status=status, reason=reason)
        if dry_run:
            return self.Receipt(
                delivered=False,
                status="dry_run",
                reason="Formatted and eligible; nothing written and nothing handed off.",
            )
        os.makedirs(os.path.dirname(self.outbox_path) or ".", exist_ok=True)
        with open(self.outbox_path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "at": _now(),
                        "message": message,
                        "event": asdict(event) if hasattr(event, "__dataclass_fields__") else dict(event),
                    },
                    ensure_ascii=False,
                    default=str,
                )
                + "\n"
            )
            fh.flush()
            os.fsync(fh.fileno())
        signature = ledger.record_dispatch(event)
        return self.Receipt(
            delivered=True,
            status="sent",
            reason=f"Appended to offline outbox {self.outbox_path} (signature {signature[:16]}).",
        )


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
    explicit_error_class: Optional[str] = None,
    observation_id: Optional[str] = None,
    operation: Optional[str] = None,
    ledger: Any = None,
) -> Optional[Dict[str, Any]]:
    """
    Intake seam for a real worker or driver failure.

    Best-effort by design: the recurrence store is advisory memory, so a store
    problem is reported to the caller and never converts a real, already-validated
    worker failure into a different one.

    Identity comes from the caller, because the caller is the only party that
    knows it. ``run_id`` is the attempt; ``observation_id`` is the one identity of
    that attempt, so the worker that finalises it and the driver that later
    re-reads its terminal ticket produce one occurrence instead of two;
    ``explicit_error_class`` is the structured fault identity, so a reworded agent
    summary is not a different failure.
    """
    try:
        guard = RecurrenceGuard(state_dir=state_dir)
        result = guard.observe(
            project=project,
            environment=environment,
            operation=operation or f"worker:{stage}",
            error=str(reason or ""),
            source=source,
            request_id=request_id or None,
            head_sha=head_sha,
            stage=stage,
            attempt=run_id,
            observation_id=observation_id,
            explicit_error_class=explicit_error_class,
            detail=(f"exit_code={exit_code}" if exit_code is not None else None),
            repo_root=repo_root,
            ledger=ledger,
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
    ca.add_argument(
        "--change-ref",
        required=True,
        help="Verifiable reference to the systemic change: commit:<40-hex>, "
             "pr:<owner>/<repo>#<n>, https://github.com/<owner>/<repo>/pull/<n>, "
             "config:<path>#<key>, decision:<id> or test:<path>::<name>. Arbitrary text is "
             "refused, because that is how 'retry later' cleared this gate.",
    )
    ca.add_argument(
        "--scenario",
        required=True,
        help="The original failure scenario this change was exercised against.",
    )
    ca.add_argument(
        "--evidence",
        required=True,
        help="What re-executing that scenario produced after the change.",
    )
    ca.add_argument(
        "--evidence-command",
        required=True,
        nargs="+",
        help="The command that exercised the scenario.",
    )
    ca.add_argument(
        "--evidence-exit-code",
        required=True,
        type=int,
        help="The exit code that command returned.",
    )
    ca.add_argument(
        "--head-sha",
        required=True,
        help="Full 40-character commit the evidence was exercised on.",
    )
    ca.add_argument(
        "--authorization",
        default=None,
        help="Explicit human authorization reference. Required for a privileged kind.",
    )
    ca.add_argument("--request-id", default=None)

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

    rsy = sub.add_parser(
        "resync-ledger",
        help="Re-apply every ledger projection that never landed (repair after a crash "
             "between the store write and the ledger write). Adds no occurrence.",
    )
    rsy.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 when a projection is still outstanding after the attempt.",
    )

    dlv = sub.add_parser(
        "deliver-escalations",
        help="Hand pending escalations to a sender and acknowledge only what it took.",
    )
    dlv.add_argument(
        "--outbox",
        required=True,
        help="Path to the durable offline outbox the escalations are handed to. Delivery "
             "configuration for a live transport belongs to the notification owner, so this "
             "command never constructs one.",
    )
    dlv.add_argument(
        "--dedup-state",
        default=None,
        help="Explicit deduplication state file (default: <outbox>.dedup.json). Always "
             "explicit: nothing here may resolve to a shared or installed default.",
    )
    dlv.add_argument(
        "--acknowledged-by",
        default=None,
        help="Who took the handoff. Required unless --dry-run.",
    )
    dlv.add_argument(
        "--dry-run",
        action="store_true",
        help="Format and check eligibility, write nothing, acknowledge nothing.",
    )

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
                change_ref=args.change_ref,
                scenario=args.scenario,
                evidence=args.evidence,
                evidence_command=args.evidence_command,
                evidence_exit_code=args.evidence_exit_code,
                head_sha=args.head_sha,
                authorization=args.authorization,
                request_id=args.request_id,
            )
            action = out["action"]
            lines = [
                f"signature      : {out['signature']}",
                f"action         : {action['action_id']} ({action['kind']})",
                f"change ref     : {action['change_ref']} "
                f"[{action['change_ref_form']}] verification={action['change_ref_verification']}",
                f"scenario       : {action['scenario']}",
                f"evidence       : {' '.join(action['evidence_command'])} "
                f"-> exit {action['evidence_exit_code']} on {action['head_sha']}",
                f"status         : {out['previous_status']} -> {out['status']}",
                f"retry allowed  : {out['retry_allowed']}",
                f"scope          : {action['verifies_nothing']}",
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

        if args.command == "resync-ledger":
            out = guard.resync_ledger()
            lines = [
                f"outstanding    : {out['outstanding']}",
                f"recovered      : {out['recovered']}",
            ]
            for row in out["projections"]:
                lines.append(
                    f"  {row['observation_id']} -> {row['request_id']} "
                    f"recorded={row['recorded']} blocker_set={row['blocker_set']} "
                    f"({row['reason']})"
                )
            _print(out, lines, args.summary)
            if args.strict and out["still_outstanding"]:
                return EXIT_ERROR
            return EXIT_OK

        if args.command == "deliver-escalations":
            sender = OfflineEscalationOutbox(
                outbox_path=args.outbox, dedup_state_file=args.dedup_state
            )
            out = guard.deliver_escalations(
                sender=sender,
                acknowledged_by=args.acknowledged_by or "",
                dry_run=args.dry_run,
            )
            lines = [
                f"considered     : {out['considered']}",
                f"acknowledged   : {len(out['acknowledged'])}",
                f"still pending  : {len(out['still_pending'])}",
                f"dry run        : {out['dry_run']}",
            ]
            for row in out["acknowledged"]:
                lines.append(
                    f"  handed off {row['escalation_id']} [{row['status']}] "
                    f"to {row['acknowledged_by']}"
                )
            for row in out["still_pending"]:
                lines.append(
                    f"  still pending {row['escalation_id']} [{row['status']}] {row['reason']}"
                )
            _print(out, lines, args.summary)
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
