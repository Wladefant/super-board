#!/usr/bin/env python3
"""
Decision Workflow Utility (~/.veyyon/workflows/decision_workflow.py)

Machine-local issue-based asynchronous human decision workflow and integration
contract for the request ledger.

Key Features:
  - Question Contract:
      * decision_id, request_id, prompt/original_request, concrete question
      * options with explicit tradeoffs, recommendation with rationale
      * decision_scope: strictly bounded to architectural preferences and design choices
        (protected actions like production deployment, main branch merges, or database drops
        CANNOT be authorized via issue decisions).
      * blocking_dependencies (ledger request IDs blocked pending answer)
      * authorized_responders (operator GitHub handles)
      * answer details (comment_id, verified comment creation time, ingest audit
        time, responder, interpretation, provenance)
  - Strict Provenance & Authored-Comment Exclusion:
      * Distinguishes human_operator, synthetic_test, and agent_authored provenance.
      * Account identity alone (e.g. Wladefant) does NOT constitute human provenance.
      * Autonomous agent-authored comments (questions, test probes, notices) are tracked
        in authored_comment_ids and strictly rejected from authorizing real work.
      * Synthetic test runs with --test record provenance: synthetic_test and NEVER
        unblock real ledger tasks.
  - CLI Ingestion & API Verification:
      * CLI ingestion fetches comments directly from GitHub API, never trusting
        caller-supplied actor, body or creation time.
      * Validates issue number, question window (created_at >= question timestamp),
        responder authorization, and checks for comment edits.
      * Detects and rejects actor, body and timestamp forgery.
  - Answer Time Provenance:
      * `answer.comment_created_at` is the comment's own creation time, copied
        verbatim from the GitHub API response and persisted only on an API-verified
        ingest (`ingest`/`sync`). A caller-supplied value is used for the
        fail-closed staleness check but is never recorded as proof, so the field is
        either API-verified or absent.
      * `answer.answered_at` is ingest audit only. Sync is bounded and runs at
        execution barriers, so it routinely postdates the operator's comment by
        minutes or days; ordering an answer against other events must use
        `comment_created_at` and fail closed when it is absent.
      * A resolved decision is terminal: only the exact same authenticated comment
        replays (idempotently, to re-synchronize a ledger write). A different or
        edited comment is refused as a rejected input, and no refusal ever rewrites
        the answer, its timestamps, or a request the answer already unblocked.
  - Plain Reply First with Typed Options:
      * Matches natural-language responses ("Option A", "A", "go with recommendation")
      * Ambiguous replies request clarification (tasks remain blocked)
      * Unauthorized replies are rejected (tasks remain blocked)
  - Autonomous Resumption & Bounded Sync:
      * GitHub issue comments do NOT trigger automatic webhooks into local environments.
      * Supported periodic polling / goal resumption via bounded one-shot sync:
        `python decision_workflow.py sync [--id DEC-ID] [--once]`
      * Coordinator-driven sync at coordination barriers or periodic polling loop.
  - Question Lifecycle vs Reply Outcome:
      * A refused reply (stale, agent-authored, unauthorized, unsafe) is an INPUT
        outcome, recorded in audit_trail and rejected_inputs. It never becomes the
        question's own status, so an unanswered question stays `pending` (or
        `clarification_requested`) and therefore stays visible to `sync`.
      * Replaying an unchanged refused comment is idempotent: full provenance
        validation still runs, but no duplicate audit row or ledger write is made.
      * `status: rejected` survives only as a legacy value written by earlier
        versions, which stamped a refused reply onto the question and dropped it out
        of the sync window. Reopen such a record explicitly and fail-closed:
        `python decision_workflow.py recover DEC-ID --actor <handle> --reason <why>`
        Recovery refuses terminal, resolved, historyless, ambiguous or out-of-scope
        records, and independently requires the ledger to show a non-terminal request
        holding an unresolved decision entry for that exact id: an all-terminal,
        mismatched or ambiguous binding is refused, so finished work is never
        re-blocked. It retains audit, question binding and blockers, writes only to
        the requests it proved are still waiting, and never manufactures an answer.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# Import RequestLedger from sibling ledger module
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from ledger import FileLock, RequestLedger, get_iso_timestamp

DEFAULT_DECISIONS_PATH = os.path.join(SCRIPT_DIR, "decisions.json")
try:
    from project_adapter import (
        get_current_project_config,
        check_text_for_forbidden_patterns,
    )
    _proj_cfg = get_current_project_config()
    DEFAULT_REPO = _proj_cfg.repo
except ImportError:
    get_current_project_config = None
    check_text_for_forbidden_patterns = None
    DEFAULT_REPO = "Bavariance/polysimulator"

# ----------------------------------------------------------------------
# Scope and Provenance Definitions
# ----------------------------------------------------------------------

class DecisionScope:
    ARCHITECTURAL_PREFERENCE = "architectural_preference"
    DESIGN_CHOICE = "design_choice"
    IMPLEMENTATION_STRATEGY = "implementation_strategy"
    TRADEOFF_SELECTION = "tradeoff_selection"


ALLOWED_DECISION_SCOPES = [
    DecisionScope.ARCHITECTURAL_PREFERENCE,
    DecisionScope.DESIGN_CHOICE,
    DecisionScope.IMPLEMENTATION_STRATEGY,
    DecisionScope.TRADEOFF_SELECTION,
]

PROTECTED_ACTION_SCOPES = [
    "production_deployment",
    "staging_promotion",
    "main_merge",
    "destructive_operation",
    "credential_access",
    "money_write",
]


class ProvenanceType:
    HUMAN_OPERATOR = "human_operator"
    SYNTHETIC_TEST = "synthetic_test"
    AGENT_AUTHORED = "agent_authored"
    FORGED_ACTOR = "forged_actor"
    UNAUTHORIZED_ACTOR = "unauthorized_actor"
    UNVERIFIED_CALLER = "unverified_caller"


class DecisionStatus:
    """Question lifecycle states. A refused *reply* is never one of these."""

    PENDING = "pending"
    CLARIFICATION_REQUESTED = "clarification_requested"
    ANSWERED = "answered"
    # Legacy only: written by versions that stamped a refused reply onto the
    # question. Never assigned by process_reply; reopened via `recover`.
    REJECTED = "rejected"


# A question in one of these states is still unresolved and still actionable,
# so `sync_decisions` must keep scanning it for a genuine answer.
OPEN_DECISION_STATUSES = (
    DecisionStatus.PENDING,
    DecisionStatus.CLARIFICATION_REQUESTED,
)

TERMINAL_DECISION_STATUSES = (DecisionStatus.ANSWERED,)

#: A dependent ledger request in one of these states is finished work. Recovery
#: never writes a blocker onto it and never counts it as work waiting on an answer.
TERMINAL_REQUEST_STATES = ("done",)


class CommentTimeProvenance:
    """
    Where a reply's *creation* timestamp came from.

    Only a value read straight out of the GitHub API response for that comment is
    proof of when the operator actually answered. Everything else — a caller
    argument, this process's own clock, a hand-edited store — is unproven, and
    `process_reply` records it as absent rather than substituting it.
    """

    #: Copied verbatim from the API response for this comment id, and parseable
    #: as an offset-aware ISO-8601 instant. The only value persisted as proof.
    API_VERIFIED = "api_verified"
    #: Handed in by a caller through the direct reply path. Never persisted as proof.
    CALLER_SUPPLIED = "caller_supplied"
    #: The API-verified path ran but the response carried no creation time.
    MISSING = "missing"
    #: The API-verified path ran and the value could not be read as an
    #: offset-aware ISO-8601 instant, so it proves nothing about ordering.
    MALFORMED = "malformed"


def verified_comment_created_at(
    comment_created_at: Optional[str],
    provenance: str,
) -> Tuple[Optional[str], str]:
    """
    Reduce a reply's creation timestamp to proof-or-nothing.

    Returns `(value, source)`. `value` is non-None only for an API-verified,
    offset-aware ISO-8601 instant; in every other case it is None and `source`
    says why, so a consumer that needs ordering proof fails closed instead of
    reading an ingest clock or a caller argument.
    """
    if provenance != CommentTimeProvenance.API_VERIFIED:
        return None, CommentTimeProvenance.CALLER_SUPPLIED
    raw = (comment_created_at or "").strip()
    if not raw:
        return None, CommentTimeProvenance.MISSING
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None, CommentTimeProvenance.MALFORMED
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None, CommentTimeProvenance.MALFORMED
    return raw, CommentTimeProvenance.API_VERIFIED


class DecisionRecoveryRefused(Exception):
    """An explicit legacy-recovery request that was refused fail-closed."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


AGENT_SIGNATURE_PATTERNS = [
    r"<!--\s*veyyon-agent-authored",
    r"<!--\s*veyyon-agent-provenance",
    r"<!--\s*synthetic-test",
    r"###\s*❓\s*Decision\s*Needed:",
    r"###\s*⚠️\s*CORRECTION:\s*Synthetic\s*Test\s*Probe",
]

# Forbidden keywords in issue text that attempt production or destructive operations
FORBIDDEN_DESTRUCTIVE_PATTERNS = [
    r"\bdeploy(?:ment)?\s+(?:to\s+)?prod(?:uction)?\b",
    r"\bpromote\s+(?:to\s+)?prod(?:uction)?\b",
    r"\bmerge\s+(?:to\s+|into\s+)?main\b",
    r"\bprod(?:uction)?\s+merge\b",
    r"\bzaraprptkegxqpvnsubu\b",  # Production Supabase project ref
    r"\bdrop\s+database\b",
    r"\bdrop\s+table\b",
    r"\btruncate\s+table\b",
    r"\brm\s+-rf\b",
    r"\bformat\s+disk\b",
    r"\bprod(?:uction)?\s+(?:money\s+)?backfill\b",
]


@dataclass
class DecisionOption:
    id: str  # e.g. "A", "B", "1", "2"
    label: str  # e.g. "Dedicated audit_events table"
    description: str  # Description of this option
    tradeoffs: str  # Explicit tradeoffs / downsides / upsides


@dataclass
class DecisionContract:
    decision_id: str
    request_id: str
    prompt: str
    question: str
    options: List[Dict[str, str]]
    recommendation: str
    blocking_dependencies: List[str]
    authorized_responders: List[str]
    decision_scope: str = DecisionScope.ARCHITECTURAL_PREFERENCE
    status: str = DecisionStatus.PENDING  # pending, clarification_requested, answered
    format_preference: str = "plain"  # plain or form
    issue_number: Optional[int] = None
    issue_url: Optional[str] = None
    question_comment_id: Optional[str] = None
    question_posted_at: Optional[str] = None
    created_at: str = field(default_factory=get_iso_timestamp)
    updated_at: str = field(default_factory=get_iso_timestamp)
    answer: Optional[Dict[str, Any]] = None
    clarification_prompt: Optional[str] = None
    rejection_reason: Optional[str] = None
    audit_trail: List[Dict[str, Any]] = field(default_factory=list)
    # Refused reply inputs, keyed by comment id. Separate from `status`: a refused
    # comment is audited evidence about an input, not the state of the question.
    rejected_inputs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    last_rejected_input: Optional[Dict[str, Any]] = None
    recovery: Optional[Dict[str, Any]] = None


def validate_decision_scope(decision: DecisionContract) -> Tuple[bool, str]:
    """Validate that the decision scope and content are strictly within safe architectural bounds."""
    scope = (decision.decision_scope or "").strip().lower()
    if scope in PROTECTED_ACTION_SCOPES:
        return (
            False,
            f"Invalid decision scope '{scope}'. Decisions can only govern architectural preferences "
            "and design choices. Protected actions (production deployments, main branch merges, "
            "destructive operations) cannot be authorized via issue decisions.",
        )
    if scope not in ALLOWED_DECISION_SCOPES:
        return (
            False,
            f"Unsupported decision scope '{scope}'. Allowed scopes: {', '.join(ALLOWED_DECISION_SCOPES)}",
        )

    # Also verify prompt/question don't attempt to delegate protected actions
    combined_text = f"{decision.prompt} {decision.question}"
    is_safe, safety_err = check_safety_guardrails(combined_text)
    if not is_safe:
        return (False, f"Scope violation: {safety_err}")

    return (True, "")


def check_safety_guardrails(text: str) -> Tuple[bool, str]:
    """Verify that issue text does not attempt to authorize production or destructive actions."""
    if check_text_for_forbidden_patterns:
        is_forbidden, reason = check_text_for_forbidden_patterns(text)
        if is_forbidden:
            return (
                False,
                f"Safety refusal: {reason}. "
                "Issue comments cannot authorize production deployment, main branch merges, "
                "or destructive data actions per AGENTS.md policy.",
            )
    for pattern in FORBIDDEN_DESTRUCTIVE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return (
                False,
                f"Safety refusal: Matched forbidden pattern '{match.group(0)}'. "
                "Issue comments cannot authorize production deployment, main branch merges, "
                "or destructive data actions per AGENTS.md policy.",
            )
    return (True, "")


def is_agent_authored(
    comment_id: Optional[str],
    body: str,
    authored_comment_ids: Set[str],
) -> Tuple[bool, str]:
    """Check if comment was authored by autonomous agent or test probe."""
    if comment_id and str(comment_id) in authored_comment_ids:
        return True, f"Comment ID {comment_id} matches known agent-authored comment registry."

    for pat in AGENT_SIGNATURE_PATTERNS:
        if re.search(pat, body, re.IGNORECASE):
            return True, f"Comment body matches agent signature pattern '{pat}'."

    return False, ""


def rejected_input_fingerprint(body: str, comment_updated_at: Optional[str]) -> str:
    """
    Identity of a refused comment *as an input*.

    Replaying the same untouched comment must not duplicate audit rows, but an
    edited body or a new edit timestamp is a different input and is revalidated
    from scratch.
    """
    digest = hashlib.sha256()
    digest.update((body or "").encode("utf-8"))
    digest.update(b"\x00")
    digest.update((comment_updated_at or "").encode("utf-8"))
    return digest.hexdigest()


def extract_task_list_options(text: str) -> Dict[str, Dict[str, Any]]:
    """
    Extract task-list checkbox options from markdown text.
    Matches lines like:
      - [ ] **Option A**: Dedicated audit_events table
      - [x] **Option A**: Dedicated audit_events table
      - [X] **A**: Dedicated audit_events table
      - [ ] [A] Dedicated table
      - [x] Choice 1 - First option
      * [x] Option 2: Second option
    """
    options: Dict[str, Dict[str, Any]] = {}
    pattern = re.compile(
        r"^\s*[-*]\s*\[([ xX])\]\s*(?:\*\*)?(?:Option\s+|Choice\s+)?\[?([a-zA-Z0-9_-]+)\]?(?:\*\*)?(?:\s*[:\-\.]\s*|\s+)?(.*)$",
        re.MULTILINE,
    )
    for match in pattern.finditer(text or ""):
        state = match.group(1).lower() == "x"
        opt_id = match.group(2).strip()
        label = (match.group(3) or "").strip()
        label = re.sub(r"^\*\*\s*", "", label)
        options[opt_id] = {
            "checked": state,
            "id": opt_id,
            "label": label,
            "raw_line": match.group(0).strip(),
        }
    return options


def extract_additional_context(text: str) -> str:
    """
    Extract free-text notes / additional context from markdown context block or section.
    Looks for:
      <!-- decision-context: DEC-ID -->
      ...
      <!-- /decision-context -->
    or under '#### Additional Context / Alternative Proposal'.
    """
    ctx_match = re.search(
        r"<!--\s*decision-context(?::\s*[\w-]+)?\s*-->\s*(.*?)\s*<!--\s*/decision-context\s*-->",
        text or "",
        re.DOTALL | re.IGNORECASE,
    )
    if ctx_match:
        content = ctx_match.group(1).strip()
        if content.startswith("_") and content.endswith("_"):
            return ""
        return content

    heading_match = re.search(
        r"#{3,4}\s*(?:Additional Context|Alternative Proposal|Notes)[^\n]*\n(.*?)(?:\n#{2,4}\s|\Z)",
        text or "",
        re.DOTALL | re.IGNORECASE,
    )
    if heading_match:
        content = heading_match.group(1).strip()
        content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL).strip()
        if content.startswith("_") and content.endswith("_"):
            return ""
        return content

    return ""


def extract_context_from_reply(
    reply_text: str,
    matched_option_id: Optional[str] = None,
    matched_label: Optional[str] = None,
) -> str:
    """Extract supplemental notes / free text from a plain or task-list reply comment."""
    lines = []
    task_re = re.compile(r"^\s*[-*]\s*\[([ xX])\]")
    for line in (reply_text or "").splitlines():
        if not task_re.match(line):
            lines.append(line)
    text_without_tasks = "\n".join(lines).strip()

    if not matched_option_id:
        return text_without_tasks

    cleaned = re.sub(
        rf"^\s*(?:i\s+choose\s+|go\s+with\s+|i\s+prefer\s+)?(?:option\s+|choice\s+)?\[?{re.escape(matched_option_id)}\]?(?:\*\*)?(?:\s*[:\-\.]\s*|\s+)",
        "",
        text_without_tasks,
        flags=re.IGNORECASE,
    ).strip()

    if matched_label and len(matched_label) > 3:
        cleaned = re.sub(
            rf"^\s*{re.escape(matched_label)}(?:\s*[:\-\.]\s*|\s+)?",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()

    cleaned = re.sub(r"^(?:note|notes|additional context|context)\s*:\s*", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned

def extract_form_fields(text: str) -> Dict[str, str]:
    """Extract key-value pairs from optional markdown form block or lines."""
    fields: Dict[str, str] = {}

    comment_block_match = re.search(
        r"<!--\s*decision-form(?::\s*[\w-]+)?\s*-->\s*(.*?)\s*<!--\s*/decision-form\s*-->",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    search_target = comment_block_match.group(1) if comment_block_match else text

    for line in search_target.splitlines():
        line = line.strip().lstrip("-* ").strip()
        if not line or line.startswith("<!--") or line.startswith("#"):
            continue
        colon_match = re.match(r"^([\w\s-]+)\s*:\s*(.+)$", line)
        if colon_match:
            k = colon_match.group(1).strip().lower().replace(" ", "_").replace("-", "_")
            v = colon_match.group(2).strip()
            fields[k] = v

    return fields


def parse_plain_reply(
    reply_text: str,
    decision: DecisionContract,
    responder: str,
    provenance: str = ProvenanceType.HUMAN_OPERATOR,
    is_test: bool = False,
) -> Dict[str, Any]:
    """
    Parse a reply to a decision contract.
    Enforces safety guardrails, provenance checks, authorization, and typed option resolution.
    """
    # 1. Safety Guardrail Check
    is_safe, safety_err = check_safety_guardrails(reply_text)
    if not is_safe:
        return {
            "status": "rejected",
            "selected_option": None,
            "interpretation": "Safety violation detected in reply text.",
            "form_fields": {},
            "rejection_reason": safety_err,
            "clarification_prompt": None,
            "provenance": provenance,
        }

    # 2. Provenance Check: Autonomous agent-authored comments cannot answer real decisions
    if provenance in [ProvenanceType.AGENT_AUTHORED] or (
        provenance == ProvenanceType.SYNTHETIC_TEST and not is_test
    ):
        return {
            "status": "rejected",
            "selected_option": None,
            "interpretation": "Agent-authored reply rejected from human decision authority.",
            "form_fields": {},
            "rejection_reason": (
                "Authored-comment / synthetic test exclusion: Comment was generated by "
                "autonomous agent or test harness, not genuine human operator. "
                "Agent-authored comments cannot authorize real work."
            ),
            "clarification_prompt": None,
            "provenance": provenance,
        }

    # 3. Authorization Check
    auth_normalized = [a.lower().lstrip("@") for a in decision.authorized_responders]
    responder_clean = responder.lower().lstrip("@")
    if auth_normalized and responder_clean not in auth_normalized:
        return {
            "status": "rejected",
            "selected_option": None,
            "interpretation": f"Responder '@{responder}' is not authorized to answer this decision.",
            "form_fields": {},
            "rejection_reason": (
                f"Unauthorized responder '@{responder}'. Authorized responders: "
                f"{', '.join('@' + a for a in decision.authorized_responders)}"
            ),
            "clarification_prompt": None,
            "provenance": ProvenanceType.UNAUTHORIZED_ACTOR,
        }

    # 4. Optional Form Extraction
    form_fields = extract_form_fields(reply_text)

    # 5. Check Task-List Checkbox Options in Reply
    task_options = extract_task_list_options(reply_text)
    checked_task_ids = [opt_id for opt_id, info in task_options.items() if info["checked"]]
    if len(checked_task_ids) > 1:
        options_summary = " or ".join(f"Option {o['id']} ({o['label']})" for o in decision.options)
        clarification = (
            f"Multiple options were selected by @{responder}: {', '.join(checked_task_ids)}. "
            f"Please choose only one option from {options_summary}."
        )
        return {
            "status": "clarification_requested",
            "selected_option": None,
            "selection_method": "task_list_checkbox",
            "additional_context": None,
            "notes": None,
            "interpretation": f"Ambiguous reply: Multiple options checked ({', '.join(checked_task_ids)}).",
            "form_fields": form_fields,
            "rejection_reason": None,
            "clarification_prompt": clarification,
            "provenance": provenance,
        }
    elif len(checked_task_ids) == 1:
        matched_task_opt = None
        for opt in decision.options:
            if opt["id"].lower() == checked_task_ids[0].lower():
                matched_task_opt = opt
                break
        if matched_task_opt:
            ctx = extract_context_from_reply(
                reply_text, matched_task_opt["id"], matched_task_opt["label"]
            ) or form_fields.get("notes", "") or extract_additional_context(reply_text)
            ctx = ctx.strip() if ctx else None
            interp = f"Explicit choice via task list: Option {matched_task_opt['id']} ({matched_task_opt['label']})"
            if ctx:
                interp += f" (notes: {ctx})"
            return {
                "status": "answered",
                "selected_option": matched_task_opt,
                "selection_method": "task_list_checkbox",
                "additional_context": ctx,
                "notes": ctx,
                "interpretation": interp,
                "form_fields": form_fields,
                "rejection_reason": None,
                "clarification_prompt": None,
                "provenance": provenance,
            }

    # 6. Typed Option Resolution from Plain / Form Text
    candidate_text = form_fields.get("choice") or form_fields.get("option") or reply_text.strip()
    candidate_lower = candidate_text.lower()

    # Check for recommendation reference
    if re.search(r"\b(?:your\s+)?recommend(?:ation|ed)?\b", candidate_lower):
        rec_opt = None
        for opt in decision.options:
            if (
                opt["id"].lower() in decision.recommendation.lower()
                or opt["label"].lower() in decision.recommendation.lower()
            ):
                rec_opt = opt
                break
        if not rec_opt and decision.options:
            rec_opt = decision.options[0]

        if rec_opt:
            ctx = extract_context_from_reply(
                reply_text, rec_opt["id"], rec_opt["label"]
            ) or form_fields.get("notes", "") or extract_additional_context(reply_text)
            ctx = ctx.strip() if ctx else None
            interp = f"Approved recommendation ({rec_opt['id']}: {rec_opt['label']})"
            if ctx:
                interp += f" (notes: {ctx})"
            return {
                "status": "answered",
                "selected_option": rec_opt,
                "selection_method": "recommendation",
                "additional_context": ctx,
                "notes": ctx,
                "interpretation": interp,
                "form_fields": form_fields,
                "rejection_reason": None,
                "clarification_prompt": None,
                "provenance": provenance,
            }

    # Check for explicit option IDs
    matched_options = []
    for opt in decision.options:
        opt_id = opt["id"].lower()
        opt_label = opt["label"].lower()

        id_pattern = rf"\b(?:option\s+|choice\s+)?\[?{re.escape(opt_id)}\]?\b"
        if re.search(id_pattern, candidate_lower):
            matched_options.append((opt, "id_match"))
        elif len(opt_label) > 3 and opt_label in candidate_lower:
            matched_options.append((opt, "label_match"))

    # Check for ambiguity
    ambiguous_keywords = [
        "maybe",
        "not sure",
        "either",
        "neither",
        "both",
        "what about",
        "can we",
        "depends",
        "unsure",
        "i don't know",
        "idk",
    ]
    has_ambiguous_phrase = any(kw in candidate_lower for kw in ambiguous_keywords)
    unique_matched_ids = {m[0]["id"] for m in matched_options}

    if len(unique_matched_ids) == 1 and not has_ambiguous_phrase:
        chosen_opt = matched_options[0][0]
        ctx = extract_context_from_reply(
            reply_text, chosen_opt["id"], chosen_opt["label"]
        ) or form_fields.get("notes", "") or extract_additional_context(reply_text)
        ctx = ctx.strip() if ctx else None
        interp = f"Explicit choice: Option {chosen_opt['id']} ({chosen_opt['label']})"
        if ctx:
            interp += f" (notes: {ctx})"
        return {
            "status": "answered",
            "selected_option": chosen_opt,
            "selection_method": "form" if form_fields.get("choice") or form_fields.get("option") else "plain_reply",
            "additional_context": ctx,
            "notes": ctx,
            "interpretation": interp,
            "form_fields": form_fields,
            "rejection_reason": None,
            "clarification_prompt": None,
            "provenance": provenance,
        }

    options_summary = " or ".join(f"Option {o['id']} ({o['label']})" for o in decision.options)
    clean_reply = reply_text.strip()

    # If ambiguous phrase or multiple options matched
    if has_ambiguous_phrase or len(unique_matched_ids) > 1:
        clarification = (
            f"Ambiguous or unrecognized reply received from @{responder}: '{reply_text}'. "
            f"Please clarify your choice by replying with either {options_summary}, "
            f"or simply 'Recommendation'."
        )
        return {
            "status": "clarification_requested",
            "selected_option": None,
            "selection_method": "ambiguous_reply",
            "additional_context": clean_reply,
            "notes": None,
            "interpretation": "Ambiguous reply; clarification requested from operator.",
            "form_fields": form_fields,
            "rejection_reason": None,
            "clarification_prompt": clarification,
            "provenance": provenance,
        }

    # Alternative proposal / custom free-text answer (not matching pre-defined options)
    # Selection is optional; custom proposals are retained for review/interpretation
    # rather than discarded or forced into a checkbox. Tasks remain blocked until resolved.
    clarification = (
        f"Alternative proposal received from @{responder}: '{clean_reply}'. "
        f"This response has been recorded for interpretation. To advance the blocked tasks automatically, "
        f"please select one of the available options ({options_summary}), or clarify approval."
    )
    return {
        "status": "clarification_requested",
        "selected_option": None,
        "selection_method": "alternative_proposal",
        "alternative_proposal": clean_reply,
        "additional_context": clean_reply,
        "notes": clean_reply,
        "interpretation": f"Alternative proposal / custom response received from @{responder}: '{clean_reply}' (retained for interpretation; tasks remain blocked until an authorized option or scope is approved).",
        "form_fields": form_fields,
        "rejection_reason": None,
        "clarification_prompt": clarification,
        "provenance": provenance,
    }



def format_decision_markdown(decision: DecisionContract) -> str:
    """Format the decision contract as a clean, actionable GitHub issue comment with interactive task-list options."""
    lines = [
        f"### ❓ Decision Needed: `{decision.decision_id}`",
        "",
        f"**Original Request / Goal:** {decision.prompt}",
        f"**Target Task ID:** `{decision.request_id}`",
        f"**Decision Scope:** `{decision.decision_scope}` (Architectural / design preference only)",
        "",
        "#### Concrete Question",
        f"> **{decision.question}**",
        "",
        "#### Available Options & Tradeoffs",
        "| Option | Proposal | Tradeoffs |",
        "| :--- | :--- | :--- |",
    ]

    for opt in decision.options:
        lines.append(f"| **{opt['id']}** | {opt['label']} - {opt['description']} | {opt['tradeoffs']} |")

    lines.extend([
        "",
        "#### Choose an Option (Click checkbox to select)",
        f"<!-- decision-options: {decision.decision_id} -->",
    ])

    selected_id = None
    if decision.answer and isinstance(decision.answer, dict):
        selected_id = str(decision.answer.get("selected_option_id") or "").strip().lower()

    for opt in decision.options:
        is_checked = "x" if selected_id and opt["id"].lower() == selected_id else " "
        lines.append(f"- [{is_checked}] **Option {opt['id']}**: {opt['label']} - {opt['description']}")

    lines.extend([
        f"<!-- /decision-options -->",
        "",
        "#### Additional Context / Alternative Proposal (Optional)",
        f"<!-- decision-context: {decision.decision_id} -->",
        "_Leave any supplemental notes, constraints, or alternative proposals below:_",
        f"<!-- /decision-context -->",
        "",
        "#### Recommendation",
        f"👉 **{decision.recommendation}**",
        "",
        "#### Blocking Dependencies",
        "The following ledger tasks are **BLOCKED** awaiting this decision:",
    ])

    for dep in decision.blocking_dependencies:
        lines.append(f"- `{dep}`")

    authorized_mentions = ", ".join(f"@{a.lstrip('@')}" for a in decision.authorized_responders)
    lines.extend([
        "",
        "#### How to Respond",
        f"Authorized responder(s): {authorized_mentions}",
        "",
        "**Option 1: Click a checkbox above (Fastest)**",
        "- Click the checkbox next to your preferred option directly in this issue.",
        "",
        "**Option 2: Plain reply comment (Preferred for mobile or quick reply)**",
        "- Reply to this issue with: `Option A`, `Option B`, or simply `Recommendation`.",
        "- You can include additional notes or instructions in your reply (e.g. `Option A: ensure we add indices`).",
        "",
        "**Option 3: Alternative proposal or custom answer**",
        "- Reply with your own alternative proposal or instructions in free text.",
        "- Selection is optional; custom proposals are retained for review and interpretation without silent automatic approval.",
        "",
        "**Option 4: Structured Form (Optional for multi-field responses)**",
        "```markdown",
        f"<!-- decision-form: {decision.decision_id} -->",
        "choice: Option A",
        "notes: <optional extra notes>",
        "<!-- /decision-form -->",
        "```",
        "",
        "> ⚠️ **Strict Safety Invariant:** Issue comments can ONLY select architectural/design options. "
        "They CANNOT authorize production deployment, staging promotion, main branch merges, "
        "or destructive database operations. Protected actions require separate explicit operator authorization.",
        "",
        f"<!-- veyyon-agent-authored: decision-question:{decision.decision_id} -->",
    ])

    return "\n".join(lines)



# ----------------------------------------------------------------------
# GitHub API Comment Fetching
# ----------------------------------------------------------------------

def fetch_github_comment_default(repo: str, comment_id: str) -> Dict[str, Any]:
    """Fetch raw comment data directly from GitHub API using gh CLI."""
    cmd = [
        "gh",
        "api",
        f"repos/{repo}/issues/comments/{comment_id}",
        "--jq",
        "{id: .id, user: .user.login, body: .body, created_at: .created_at, updated_at: .updated_at, html_url: .html_url, issue_url: .issue_url}",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    out = res.stdout.strip()
    if not out:
        raise ValueError(f"Empty response fetching comment {comment_id} from {repo}")
    return json.loads(out)


# ----------------------------------------------------------------------
# Decision Manager
# ----------------------------------------------------------------------

class DecisionManager:
    """Manages machine-local decision contracts and their integration with RequestLedger."""

    def __init__(
        self,
        decisions_path: Optional[str] = None,
        ledger_path: Optional[str] = None,
        comment_fetcher: Optional[Callable[[str, str], Dict[str, Any]]] = None,
    ):
        self.decisions_path = os.path.abspath(
            decisions_path or os.environ.get("DECISIONS_PATH") or DEFAULT_DECISIONS_PATH
        )
        self.lock_path = self.decisions_path + ".lock"
        self.ledger = RequestLedger(ledger_path)
        self.comment_fetcher = comment_fetcher or fetch_github_comment_default

    def _load_data_unlocked(self) -> Dict[str, Any]:
        if not os.path.exists(self.decisions_path):
            return {
                "version": 2,
                "created_at": get_iso_timestamp(),
                "updated_at": get_iso_timestamp(),
                "authored_comment_ids": [],
                "synthetic_test_comment_ids": [],
                "decisions": {},
            }
        try:
            with open(self.decisions_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {
                        "version": 2,
                        "authored_comment_ids": [],
                        "synthetic_test_comment_ids": [],
                        "decisions": {},
                    }
                data = json.loads(content)
                data.setdefault("authored_comment_ids", [])
                data.setdefault("synthetic_test_comment_ids", [])
                data.setdefault("decisions", {})
                return data
        except json.JSONDecodeError as e:
            raise ValueError(f"Corrupt decisions file at {self.decisions_path}: {e}")

    def _save_data_unlocked(self, data: Dict[str, Any]):
        data["updated_at"] = get_iso_timestamp()
        dir_name = os.path.dirname(self.decisions_path)
        os.makedirs(dir_name, exist_ok=True)

        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_decisions_", dir=dir_name, text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.decisions_path)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            raise

    def record_authored_comment(self, comment_id: str, is_synthetic_test: bool = False):
        """Record an agent-authored or synthetic test comment ID to ensure exclusion."""
        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            s_id = str(comment_id)
            if s_id not in data["authored_comment_ids"]:
                data["authored_comment_ids"].append(s_id)
            if is_synthetic_test and s_id not in data["synthetic_test_comment_ids"]:
                data["synthetic_test_comment_ids"].append(s_id)
            self._save_data_unlocked(data)

    def _blockable_requests(self, req_ids: List[str]) -> List[str]:
        """
        The requests from `req_ids` that a decision blocker may be written onto.

        A request in a terminal state is finished work. Writing a blocker onto it
        reopens it for every reader of the ledger — including the pending-decision
        scan — and no decision outcome is entitled to do that: not a new question,
        not a clarification, not a refused reply, not a recovery. Requests the
        ledger cannot read are dropped here rather than at each call site, which is
        what those call sites' `except KeyError` already did.
        """
        targets = []
        for req_id in req_ids:
            try:
                state = self.ledger.get_request(req_id).get("state")
            except Exception:
                continue
            if state in TERMINAL_REQUEST_STATES:
                continue
            targets.append(req_id)
        return targets

    def register_question(self, decision: DecisionContract) -> Dict[str, Any]:
        """
        Register a new decision contract and block dependent requests in the ledger.
        Validates safe architectural scope; protected action scopes are strictly rejected.
        """
        # Scope validation
        is_scope_valid, scope_err = validate_decision_scope(decision)
        if not is_scope_valid:
            raise ValueError(f"Cannot register decision '{decision.decision_id}': {scope_err}")

        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            d_id = decision.decision_id
            if d_id in data["decisions"]:
                raise ValueError(f"Decision '{d_id}' already exists.")

            dec_dict = asdict(decision)
            data["decisions"][d_id] = dec_dict
            self._save_data_unlocked(data)

        # Update dependent ledger requests with active blocker
        blocker_msg = f"Awaiting human decision [{d_id}]: {decision.question}"
        for req_id in self._blockable_requests(decision.blocking_dependencies):
            try:
                if hasattr(self.ledger, "add_decision"):
                    try:
                        self.ledger.add_decision(
                            req_id=req_id,
                            question=decision.question,
                            options=[f"{opt['id']}: {opt['label']}" for opt in decision.options],
                            decision_id=d_id,
                            blocks=True,
                            blocks_action="implementation",
                            authorized_responder=", ".join(decision.authorized_responders),
                            actor="decision-workflow",
                        )
                    except Exception:
                        pass
                self.ledger.update_request(
                    req_id=req_id,
                    blocker=blocker_msg,
                    next_action=f"Blocked until decision [{d_id}] is answered by {', '.join(decision.authorized_responders)}",
                    actor="decision-workflow",
                    reason=f"Registered blocking decision {d_id}",
                )
            except KeyError:
                pass

        return dec_dict

    @staticmethod
    def _answer_replay_mismatch(
        recorded_answer: Dict[str, Any],
        comment_id: Optional[str],
        reply_text: str,
        comment_created_at: Optional[str],
        comment_time_provenance: str,
    ) -> Optional[str]:
        """
        Why this reply is not an exact replay of the recorded answer, or None.

        A resolved decision accepts exactly one further input: the same comment,
        unedited, claiming the same creation time. That replay is idempotent and
        exists only to re-synchronize a ledger write that did not land. Anything
        else — a different comment, an edited body, a different creation time — is
        an attempt to re-answer settled work or to restate when it was answered.
        """
        if not recorded_answer:
            return (
                "the record is marked resolved but carries no answer to replay, so no "
                "input can be matched against it"
            )
        if not comment_id:
            return "the reply carries no comment id, so it cannot match the recorded answer"
        recorded_id = str(recorded_answer.get("comment_id"))
        if recorded_id != str(comment_id):
            return f"comment {comment_id} is not the answering comment {recorded_id}"
        recorded_text = (recorded_answer.get("raw_text") or "").strip()
        if recorded_text != (reply_text or "").strip():
            return (
                f"comment {comment_id} no longer carries the body that was accepted as "
                "the answer, so it is an edit rather than a replay"
            )
        # An unproven replay makes no timestamp claim, so it cannot conflict. A
        # claim that contradicts the recorded proof is a rewrite attempt.
        claimed, _ = verified_comment_created_at(comment_created_at, comment_time_provenance)
        recorded_created_at = recorded_answer.get("comment_created_at")
        if claimed and recorded_created_at and claimed != recorded_created_at:
            return (
                f"comment {comment_id} now reports creation time {claimed}, but the answer "
                f"was proved at {recorded_created_at}; a verified creation time is immutable"
            )
        if claimed and not recorded_created_at:
            return (
                f"comment {comment_id} now supplies creation time {claimed}, but the recorded "
                "answer has no verified creation time; a settled answer is never upgraded "
                "with proof gathered after the fact"
            )
        return None

    def _refuse_reanswer_unlocked(
        self,
        data: Dict[str, Any],
        dec_dict: Dict[str, Any],
        decision_id: str,
        recorded_answer: Dict[str, Any],
        mismatch: str,
        reply_text: str,
        responder: str,
        comment_id: Optional[str],
        comment_url: Optional[str],
        comment_created_at: Optional[str],
        comment_updated_at: Optional[str],
        comment_time_provenance: str,
        provenance: str,
        is_test: bool,
    ) -> Dict[str, Any]:
        """
        Refuse a reply aimed at an already-resolved decision, changing nothing that
        the answer settled.

        The refusal is recorded the same way every other refused input is — an audit
        row plus a `rejected_inputs` entry — and nothing else moves: not `status`,
        not `answer`, not `answered_at`, not `comment_created_at`, and no ledger
        write. Re-blocking a request the genuine answer already unblocked would be
        the same poisoning this module exists to prevent, one lifecycle later.
        """
        reason = (
            f"Resolved decision: '{decision_id}' was answered by comment "
            f"{recorded_answer.get('comment_id')} and is terminal — {mismatch}. "
            "A resolved decision is never re-answered and its answer timestamps are "
            "never rewritten; post a new decision question instead."
        )
        now = get_iso_timestamp()

        if comment_id:
            fingerprint = rejected_input_fingerprint(reply_text, comment_updated_at)
            prior = (dec_dict.get("rejected_inputs") or {}).get(str(comment_id))
            if prior and prior.get("fingerprint") == fingerprint:
                prior["occurrences"] = int(prior.get("occurrences", 1)) + 1
                prior["last_seen_at"] = now
                dec_dict["last_rejected_input"] = dict(prior)
                self._save_data_unlocked(data)
                return {
                    "idempotent_replay": True,
                    "status": DecisionStatus.REJECTED,
                    "decision_id": decision_id,
                    "interpretation": "Reply to a resolved decision refused.",
                    "rejection_reason": reason,
                    "clarification_prompt": None,
                    "unblocked_requests": [],
                    "provenance": provenance,
                    "question_status": dec_dict.get("status"),
                    "reanswer_refused": True,
                    "rejected_input_occurrences": prior["occurrences"],
                }

        dec_dict.setdefault("audit_trail", []).append(
            {
                "timestamp": now,
                "responder": responder,
                "comment_id": comment_id,
                "comment_url": comment_url,
                "reply_text": reply_text,
                "status": DecisionStatus.REJECTED,
                "provenance": provenance,
                "interpretation": "Reply to a resolved decision refused.",
                "rejection_reason": reason,
                "clarification_prompt": None,
                "is_test": is_test,
                "comment_created_at": comment_created_at,
                "comment_updated_at": comment_updated_at,
                "comment_time_provenance": comment_time_provenance,
                "reanswer_refused": True,
            }
        )
        rejected_record = {
            "comment_id": str(comment_id) if comment_id else None,
            "comment_url": comment_url,
            "responder": responder,
            "fingerprint": rejected_input_fingerprint(reply_text, comment_updated_at),
            "reason": reason,
            "provenance": provenance,
            "interpretation": "Reply to a resolved decision refused.",
            "first_seen_at": now,
            "last_seen_at": now,
            "occurrences": 1,
        }
        if comment_id:
            dec_dict.setdefault("rejected_inputs", {})[str(comment_id)] = rejected_record
        dec_dict["last_rejected_input"] = dict(rejected_record)
        self._save_data_unlocked(data)
        return {
            "idempotent_replay": False,
            "status": DecisionStatus.REJECTED,
            "decision_id": decision_id,
            "interpretation": "Reply to a resolved decision refused.",
            "rejection_reason": reason,
            "clarification_prompt": None,
            "unblocked_requests": [],
            "provenance": provenance,
            "question_status": dec_dict.get("status"),
            "reanswer_refused": True,
        }

    def process_reply(
        self,
        decision_id: str,
        reply_text: str,
        responder: str,
        comment_id: Optional[str] = None,
        comment_url: Optional[str] = None,
        provenance: str = ProvenanceType.HUMAN_OPERATOR,
        is_test: bool = False,
        comment_created_at: Optional[str] = None,
        comment_updated_at: Optional[str] = None,
        comment_time_provenance: str = CommentTimeProvenance.CALLER_SUPPLIED,
    ) -> Dict[str, Any]:
        """
        Process a reply to a decision.
        Enforces idempotency, authored-comment exclusion, question window, and provenance tracking.

        `comment_time_provenance` says whether `comment_created_at` came out of the
        GitHub API response for this comment id. Only an API-verified value is
        persisted as `answer.comment_created_at`; a caller-supplied one is used for
        the fail-closed staleness check but is never recorded as proof, so no caller
        can hand this store a creation time it did not read from the API.

        A decision that already carries an answer is terminal: only the exact same
        authenticated comment replays (idempotently, re-synchronizing the ledger),
        and any other comment is refused as a rejected input without touching the
        recorded answer or its timestamps.
        """
        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            if decision_id not in data["decisions"]:
                raise KeyError(f"Decision '{decision_id}' not found.")

            dec_dict = data["decisions"][decision_id]
            authored_ids = set(str(c) for c in data.get("authored_comment_ids", []))
            if dec_dict.get("question_comment_id"):
                authored_ids.add(str(dec_dict["question_comment_id"]))

            # Convert back to DecisionContract
            decision = DecisionContract(
                decision_id=dec_dict["decision_id"],
                request_id=dec_dict["request_id"],
                prompt=dec_dict["prompt"],
                question=dec_dict["question"],
                options=dec_dict["options"],
                recommendation=dec_dict["recommendation"],
                blocking_dependencies=dec_dict["blocking_dependencies"],
                authorized_responders=dec_dict["authorized_responders"],
                decision_scope=dec_dict.get("decision_scope", DecisionScope.ARCHITECTURAL_PREFERENCE),
                status=dec_dict.get("status", "pending"),
                format_preference=dec_dict.get("format_preference", "plain"),
                issue_number=dec_dict.get("issue_number"),
                issue_url=dec_dict.get("issue_url"),
                question_comment_id=dec_dict.get("question_comment_id"),
                question_posted_at=dec_dict.get("question_posted_at"),
                created_at=dec_dict.get("created_at"),
                updated_at=dec_dict.get("updated_at"),
                answer=dec_dict.get("answer"),
                clarification_prompt=dec_dict.get("clarification_prompt"),
                rejection_reason=dec_dict.get("rejection_reason"),
                audit_trail=dec_dict.get("audit_trail", []),
            )

            # A resolved decision is terminal. The recorded answer, its verified
            # comment creation time and its ingest audit time are immutable from here:
            # the exact authenticated comment replays, everything else is refused.
            recorded_answer = decision.answer or {}
            if recorded_answer or decision.status in TERMINAL_DECISION_STATUSES:
                replay_mismatch = self._answer_replay_mismatch(
                    recorded_answer=recorded_answer,
                    comment_id=comment_id,
                    reply_text=reply_text,
                    comment_created_at=comment_created_at,
                    comment_time_provenance=comment_time_provenance,
                )
                if replay_mismatch:
                    return self._refuse_reanswer_unlocked(
                        data=data,
                        dec_dict=dec_dict,
                        decision_id=decision_id,
                        recorded_answer=recorded_answer,
                        mismatch=replay_mismatch,
                        reply_text=reply_text,
                        responder=responder,
                        comment_id=comment_id,
                        comment_url=comment_url,
                        comment_created_at=comment_created_at,
                        comment_updated_at=comment_updated_at,
                        comment_time_provenance=comment_time_provenance,
                        provenance=provenance,
                        is_test=is_test,
                    )
                # Ensure ledger is synchronized (explicit pending-sync recovery)
                clean_responder = str(recorded_answer.get("responder") or responder or "").strip()
                if clean_responder.startswith("decision-workflow:@"):
                    clean_responder = clean_responder[len("decision-workflow:@"):]
                elif clean_responder.startswith("decision-workflow:"):
                    clean_responder = clean_responder[len("decision-workflow:"):]
                normalized_actor = clean_responder.lstrip("@").strip()
                prov_type = recorded_answer.get("provenance", ProvenanceType.HUMAN_OPERATOR)

                for req_id in decision.blocking_dependencies:
                    try:
                        req_data = self.ledger.get_request(req_id)
                        if decision.decision_id in req_data.get("decision_blockers", []):
                            if hasattr(self.ledger, "resolve_decision"):
                                self.ledger.resolve_decision(
                                    req_id=req_id,
                                    decision_id=decision.decision_id,
                                    answer=recorded_answer.get("interpretation") or "",
                                    comment_id=comment_id,
                                    provenance_type=prov_type,
                                    actor=normalized_actor,
                                )
                            upd_kwargs = {
                                "req_id": req_id,
                                "clear_blocker": True,
                                "actor": normalized_actor,
                                "reason": f"Idempotent replay: synchronized decision [{decision.decision_id}]",
                            }
                            if hasattr(self.ledger, "clear_decision_blocker"):
                                upd_kwargs["clear_decision_blocker"] = decision.decision_id
                            self.ledger.update_request(**upd_kwargs)
                    except Exception:
                        pass

                return {
                    "idempotent_replay": True,
                    "status": decision.status,
                    "decision_id": decision.decision_id,
                    "answer": decision.answer,
                    "interpretation": recorded_answer.get("interpretation"),
                    "unblocked_requests": decision.blocking_dependencies,
                    "provenance": recorded_answer.get("provenance", ProvenanceType.HUMAN_OPERATOR),
                }

            # Authored-comment / synthetic probe check
            is_authored, authored_reason = is_agent_authored(comment_id, reply_text, authored_ids)
            if is_authored:
                provenance = ProvenanceType.AGENT_AUTHORED

            # Question window / stale check
            is_stale = False
            stale_reason = ""
            if comment_created_at:
                try:
                    q_time_str = decision.question_posted_at or decision.created_at
                    # Parse ISO timestamps
                    c_dt = datetime.datetime.fromisoformat(comment_created_at.replace("Z", "+00:00"))
                    q_dt = datetime.datetime.fromisoformat(q_time_str.replace("Z", "+00:00"))
                    if c_dt < q_dt:
                        is_stale = True
                        stale_reason = (
                            f"Stale reply: Comment timestamp ({comment_created_at}) is earlier than "
                            f"decision question creation timestamp ({q_time_str})."
                        )
                except Exception:
                    pass

            # Parse reply
            parse_result = parse_plain_reply(
                reply_text=reply_text,
                decision=decision,
                responder=responder,
                provenance=provenance,
                is_test=is_test,
            )

            # Override with stale or authored rejection if detected
            if is_authored and not is_test:
                parse_result["status"] = "rejected"
                parse_result["selected_option"] = None
                parse_result["interpretation"] = "Self-authored comment rejected from human decision authority."
                parse_result["rejection_reason"] = (
                    f"Authored-comment exclusion: {authored_reason} Autonomous agent-authored comments "
                    "cannot authorize real work or resolve human decisions."
                )
            elif is_stale:
                parse_result["status"] = "rejected"
                parse_result["selected_option"] = None
                parse_result["interpretation"] = "Stale comment rejected."
                parse_result["rejection_reason"] = stale_reason

            # Refused-input replay. Validation above already ran in full, so this
            # never weakens provenance: it only suppresses a duplicate audit row and
            # a redundant ledger write for a comment that is byte-for-byte the one
            # already refused.
            if parse_result["status"] == "rejected" and comment_id:
                fingerprint = rejected_input_fingerprint(reply_text, comment_updated_at)
                prior = (dec_dict.get("rejected_inputs") or {}).get(str(comment_id))
                if prior and prior.get("fingerprint") == fingerprint:
                    prior["occurrences"] = int(prior.get("occurrences", 1)) + 1
                    prior["last_seen_at"] = get_iso_timestamp()
                    dec_dict["last_rejected_input"] = dict(prior)
                    self._save_data_unlocked(data)
                    return {
                        "idempotent_replay": True,
                        "status": parse_result["status"],
                        "decision_id": decision_id,
                        "interpretation": parse_result["interpretation"],
                        "rejection_reason": parse_result["rejection_reason"],
                        "clarification_prompt": parse_result["clarification_prompt"],
                        "unblocked_requests": [],
                        "provenance": parse_result.get("provenance", provenance),
                        "question_status": dec_dict.get("status", DecisionStatus.PENDING),
                        "rejected_input_occurrences": prior["occurrences"],
                    }

            now = get_iso_timestamp()

            audit_entry = {
                "timestamp": now,
                "responder": responder,
                "comment_id": comment_id,
                "comment_url": comment_url,
                "reply_text": reply_text,
                "status": parse_result["status"],
                "provenance": parse_result.get("provenance", provenance),
                "interpretation": parse_result["interpretation"],
                "rejection_reason": parse_result["rejection_reason"],
                "clarification_prompt": parse_result["clarification_prompt"],
                "is_test": is_test,
                "comment_created_at": comment_created_at,
                "comment_updated_at": comment_updated_at,
                "comment_time_provenance": comment_time_provenance,
            }
            dec_dict.setdefault("audit_trail", []).append(audit_entry)

            unblocked = []
            if parse_result["status"] == "answered":
                opt = parse_result["selected_option"]
                proof_created_at, created_at_source = verified_comment_created_at(
                    comment_created_at, comment_time_provenance
                )
                ans_data = {
                    "comment_id": comment_id,
                    "comment_url": comment_url,
                    "responder": responder,
                    # Ingestion audit only: when THIS process observed the reply.
                    # A bounded sync runs at execution barriers, so this routinely
                    # postdates the operator's comment by minutes or days. It is
                    # never evidence of when the decision was actually answered.
                    "answered_at": now,
                    # Proof provenance: the comment's own creation time exactly as
                    # the GitHub API reported it. Written only on an API-verified
                    # ingest; None means "unproven", and consumers that order this
                    # answer against other events must fail closed rather than fall
                    # back to `answered_at` or any store mtime.
                    "comment_created_at": proof_created_at,
                    "comment_created_at_source": created_at_source,
                    "raw_text": reply_text,
                    "selected_option_id": opt["id"] if opt else None,
                    "selected_option_label": opt["label"] if opt else None,
                    "interpretation": parse_result["interpretation"],
                    "form_fields": parse_result["form_fields"],
                    "selection_method": parse_result.get("selection_method", "plain_reply"),
                    "additional_context": parse_result.get("additional_context"),
                    "notes": parse_result.get("notes"),
                    "alternative_proposal": parse_result.get("alternative_proposal"),
                    "provenance": parse_result.get("provenance", provenance),
                    "is_test": is_test,
                }

                # INVARIANT: Only human_operator provenance can resolve real tasks!
                if is_test or parse_result.get("provenance") != ProvenanceType.HUMAN_OPERATOR:
                    # Synthetic test: record validation in audit trail, but leave decision pending and tasks BLOCKED
                    dec_dict["rejection_reason"] = (
                        "Synthetic test verified option parsing, but real task unblock is prohibited "
                        "for synthetic/test provenance."
                    )
                    # Do not set status = answered on real decision
                    # Do not unblock ledger requests
                else:
                    # Genuine human operator
                    # DO NOT mark answer committed until ledger succeeds!
                    # Normalize verified actor separately from provenance:
                    clean_responder = str(responder or "").strip()
                    if clean_responder.startswith("decision-workflow:@"):
                        clean_responder = clean_responder[len("decision-workflow:@"):]
                    elif clean_responder.startswith("decision-workflow:"):
                        clean_responder = clean_responder[len("decision-workflow:"):]
                    normalized_actor = clean_responder.lstrip("@").strip()
                    prov_type = parse_result.get("provenance", provenance)

                    # Unblock dependent ledger requests and record human decision evidence
                    for req_id in decision.blocking_dependencies:
                        try:
                            if hasattr(self.ledger, "resolve_decision"):
                                self.ledger.resolve_decision(
                                    req_id=req_id,
                                    decision_id=decision.decision_id,
                                    answer=parse_result["interpretation"],
                                    comment_id=comment_id,
                                    provenance_type=prov_type,
                                    actor=normalized_actor,
                                )
                            ev_payload = {
                                "type": "human_decision",
                                "summary": f"Decision [{decision.decision_id}] answered by @{normalized_actor}: {parse_result['interpretation']}",
                                "details": (
                                    f"Comment ID: {comment_id} | URL: {comment_url} | "
                                    f"Selected: {opt['id'] if opt else 'N/A'} - {opt['label'] if opt else 'chosen option'} | "
                                    f"Provenance: {prov_type} | "
                                    f"Raw reply: '{reply_text}'"
                                    + (f" | Context: '{parse_result.get('additional_context')}'" if parse_result.get("additional_context") else "")
                                ),
                                "recorded_by": f"decision-workflow:@{normalized_actor}",
                                "comment_id": comment_id,
                                "comment_url": comment_url,
                                "responder": responder,
                            }
                            upd_kwargs = {
                                "req_id": req_id,
                                "clear_blocker": True,
                                "next_action": f"Proceed with implementation following decision [{decision.decision_id}]: {opt['label'] if opt else 'chosen option'}",
                                "add_evidence": ev_payload,
                                "actor": normalized_actor,
                                "reason": f"Human decision [{decision.decision_id}] resolved with {parse_result['interpretation']}",
                            }
                            if hasattr(self.ledger, "clear_decision_blocker"):
                                upd_kwargs["clear_decision_blocker"] = decision.decision_id
                            self.ledger.update_request(**upd_kwargs)
                            unblocked.append(req_id)
                        except KeyError:
                            pass

                    # Commit to decisions store ONLY after ledger update succeeds
                    dec_dict["status"] = "answered"
                    dec_dict["answer"] = ans_data
                    dec_dict["clarification_prompt"] = None
                    dec_dict["rejection_reason"] = None

            elif parse_result["status"] == "clarification_requested":
                dec_dict["status"] = "clarification_requested"
                dec_dict["clarification_prompt"] = parse_result["clarification_prompt"]
                if parse_result.get("alternative_proposal"):
                    dec_dict["alternative_proposal"] = parse_result["alternative_proposal"]
                    dec_dict["last_alternative_proposal"] = parse_result["alternative_proposal"]
                    dec_dict["alternative_responder"] = responder
                    dec_dict["alternative_received_at"] = now
                for req_id in self._blockable_requests(decision.blocking_dependencies):
                    try:
                        blocker_msg = (
                            f"BLOCKED: Alternative proposal received on decision [{decision.decision_id}] from @{responder}: '{parse_result['alternative_proposal']}'. Awaiting interpretation/clarification."
                            if parse_result.get("alternative_proposal")
                            else f"BLOCKED: Clarification requested on decision [{decision.decision_id}] from @{responder}"
                        )
                        next_act = (
                            f"Awaiting operator choice or interpretation of alternative proposal on decision [{decision.decision_id}]"
                            if parse_result.get("alternative_proposal")
                            else f"Awaiting clarified response on decision [{decision.decision_id}]"
                        )
                        self.ledger.update_request(
                            req_id=req_id,
                            blocker=blocker_msg,
                            next_action=next_act,
                            actor=f"decision-workflow:@{responder}",
                            reason=f"Clarification or alternative proposal for decision [{decision.decision_id}]",
                        )
                    except KeyError:
                        pass
            elif parse_result["status"] == "rejected":
                # A refused reply is an INPUT outcome. The question stays unresolved:
                # writing the refusal onto `status` is exactly what poisoned
                # unanswered decisions out of the pending-only sync window.
                rejected_record = {
                    "comment_id": str(comment_id) if comment_id else None,
                    "comment_url": comment_url,
                    "responder": responder,
                    "fingerprint": rejected_input_fingerprint(reply_text, comment_updated_at),
                    "reason": parse_result["rejection_reason"],
                    "provenance": parse_result.get("provenance", provenance),
                    "interpretation": parse_result["interpretation"],
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "occurrences": 1,
                }
                if comment_id:
                    dec_dict.setdefault("rejected_inputs", {})[str(comment_id)] = rejected_record
                dec_dict["last_rejected_input"] = dict(rejected_record)
                dec_dict["rejection_reason"] = parse_result["rejection_reason"]
                for req_id in self._blockable_requests(decision.blocking_dependencies):
                    try:
                        self.ledger.update_request(
                            req_id=req_id,
                            blocker=f"BLOCKED: Decision [{decision.decision_id}] reply from @{responder} was REJECTED: {parse_result['rejection_reason']}",
                            next_action=f"Awaiting valid authorized response on decision [{decision.decision_id}]",
                            actor=f"decision-workflow:@{responder}",
                            reason=f"Rejected reply on decision [{decision.decision_id}]",
                        )
                    except KeyError:
                        pass

            self._save_data_unlocked(data)

            return {
                "idempotent_replay": False,
                "status": parse_result["status"],
                "decision_id": decision_id,
                "interpretation": parse_result["interpretation"],
                "rejection_reason": parse_result["rejection_reason"],
                "clarification_prompt": parse_result["clarification_prompt"],
                "unblocked_requests": unblocked,
                "question_status": dec_dict.get("status", DecisionStatus.PENDING),
                "provenance": parse_result.get("provenance", provenance),
            }

    def ingest_comment(
        self,
        decision_id: str,
        comment_id: str,
        repo: str = DEFAULT_REPO,
        caller_responder: Optional[str] = None,
        caller_text: Optional[str] = None,
        caller_created_at: Optional[str] = None,
        is_test: bool = False,
    ) -> Dict[str, Any]:
        """
        Ingest a reply by fetching the comment directly from GitHub API.
        Does NOT trust caller-supplied actor, body or creation time. Detects forgery
        and enforces issue bounds.

        This is the only path that produces `answer.comment_created_at`, because it is
        the only one that reads the creation time out of the API response instead of
        being handed it.
        """
        dec = self.get_decision(decision_id)
        cid = str(comment_id).strip()

        # 1. Fetch real comment from API
        try:
            comment_data = self.comment_fetcher(repo, cid)
        except Exception as e:
            raise RuntimeError(f"Failed to fetch GitHub comment {cid} from repo '{repo}': {e}")

        api_author = comment_data.get("user")
        api_body = comment_data.get("body", "")
        api_created_at = comment_data.get("created_at")
        api_updated_at = comment_data.get("updated_at")
        api_html_url = comment_data.get("html_url")
        api_issue_url = comment_data.get("issue_url", "")

        # 2. Forgery Check: If caller supplied responder/text, verify it matches API
        if caller_responder and caller_responder.lower().lstrip("@") != api_author.lower().lstrip("@"):
            raise ValueError(
                f"Actor forgery detected: Caller supplied responder '@{caller_responder}', "
                f"but GitHub API verified author is '@{api_author}'."
            )
        if caller_text and caller_text.strip() != api_body.strip():
            raise ValueError(
                "Body forgery detected: Caller supplied reply text does not match "
                "GitHub API verified comment body."
            )
        if caller_created_at and str(caller_created_at).strip() != str(api_created_at or "").strip():
            raise ValueError(
                f"Timestamp forgery detected: Caller supplied creation time "
                f"'{caller_created_at}', but GitHub API reports '{api_created_at}'."
            )

        # 3. Issue constraint check
        if dec.get("issue_number") and api_issue_url:
            # Extract issue number from issue_url (e.g. ".../issues/4543")
            match = re.search(r"/issues/(\d+)$", api_issue_url)
            if match:
                comment_issue_num = int(match.group(1))
                if comment_issue_num != dec["issue_number"]:
                    raise ValueError(
                        f"Issue mismatch: Comment {cid} belongs to issue #{comment_issue_num}, "
                        f"but decision '{decision_id}' is attached to issue #{dec['issue_number']}."
                    )

        # 4. Provenance determination
        provenance = ProvenanceType.SYNTHETIC_TEST if is_test else ProvenanceType.HUMAN_OPERATOR

        # 5. Process through reply handler
        return self.process_reply(
            decision_id=decision_id,
            reply_text=api_body,
            responder=api_author,
            comment_id=cid,
            comment_url=api_html_url,
            provenance=provenance,
            is_test=is_test,
            comment_created_at=api_created_at,
            comment_updated_at=api_updated_at,
            comment_time_provenance=CommentTimeProvenance.API_VERIFIED,
        )
    def process_issue_edit(
        self,
        decision_id: str,
        old_body: str,
        new_body: str,
        editor: str,
        event_type: str = "issue_edit",
        comment_id: Optional[str] = None,
        comment_url: Optional[str] = None,
        edit_time: Optional[str] = None,
        provenance: str = ProvenanceType.HUMAN_OPERATOR,
        is_test: bool = False,
    ) -> Dict[str, Any]:
        """
        Process an edit event on an issue body or question comment containing clickable task-list options.
        Validates transition (newly selected option vs old body), actor authorization,
        safety guardrails, decision scope, and idempotent replay/conflicting edits.
        """
        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            if decision_id not in data["decisions"]:
                raise KeyError(f"Decision '{decision_id}' not found.")

            dec_dict = data["decisions"][decision_id]
            decision = DecisionContract(
                decision_id=dec_dict["decision_id"],
                request_id=dec_dict["request_id"],
                prompt=dec_dict["prompt"],
                question=dec_dict["question"],
                options=dec_dict["options"],
                recommendation=dec_dict["recommendation"],
                blocking_dependencies=dec_dict["blocking_dependencies"],
                authorized_responders=dec_dict["authorized_responders"],
                decision_scope=dec_dict.get("decision_scope", DecisionScope.ARCHITECTURAL_PREFERENCE),
                status=dec_dict.get("status", "pending"),
                format_preference=dec_dict.get("format_preference", "plain"),
                issue_number=dec_dict.get("issue_number"),
                issue_url=dec_dict.get("issue_url"),
                question_comment_id=dec_dict.get("question_comment_id"),
                question_posted_at=dec_dict.get("question_posted_at"),
                created_at=dec_dict.get("created_at"),
                updated_at=dec_dict.get("updated_at"),
                answer=dec_dict.get("answer"),
                clarification_prompt=dec_dict.get("clarification_prompt"),
                rejection_reason=dec_dict.get("rejection_reason"),
                audit_trail=dec_dict.get("audit_trail", []),
            )

            # Check if decision is already terminal
            recorded_answer = decision.answer or {}
            if recorded_answer or decision.status in TERMINAL_DECISION_STATUSES:
                new_options = extract_task_list_options(new_body)
                new_checked = [opt_id for opt_id, info in new_options.items() if info["checked"]]
                recorded_opt_id = recorded_answer.get("selected_option_id")
                if len(new_checked) == 1 and str(new_checked[0]).lower() == str(recorded_opt_id or "").lower():
                    return {
                        "idempotent_replay": True,
                        "status": decision.status,
                        "decision_id": decision.decision_id,
                        "answer": decision.answer,
                        "interpretation": recorded_answer.get("interpretation"),
                        "unblocked_requests": decision.blocking_dependencies,
                        "provenance": recorded_answer.get("provenance", ProvenanceType.HUMAN_OPERATOR),
                    }
                else:
                    mismatch = f"Conflicting edit: decision already answered with option {recorded_opt_id}"
                    return self._refuse_reanswer_unlocked(
                        data=data,
                        dec_dict=dec_dict,
                        decision_id=decision_id,
                        recorded_answer=recorded_answer,
                        mismatch=mismatch,
                        reply_text=new_body,
                        responder=editor,
                        comment_id=comment_id,
                        comment_url=comment_url,
                        comment_created_at=edit_time,
                        comment_updated_at=edit_time,
                        comment_time_provenance=CommentTimeProvenance.API_VERIFIED if edit_time else CommentTimeProvenance.CALLER_SUPPLIED,
                        provenance=provenance,
                        is_test=is_test,
                    )

            # 1. Safety Guardrail Check
            is_safe, safety_err = check_safety_guardrails(new_body)
            if not is_safe:
                parse_result = {
                    "status": "rejected",
                    "selected_option": None,
                    "interpretation": "Safety violation detected in edited issue body.",
                    "form_fields": {},
                    "rejection_reason": safety_err,
                    "clarification_prompt": None,
                    "provenance": provenance,
                }
            # 2. Provenance Check: Autonomous agent-authored edits cannot answer real decisions
            elif provenance in [ProvenanceType.AGENT_AUTHORED] or (
                provenance == ProvenanceType.SYNTHETIC_TEST and not is_test
            ):
                parse_result = {
                    "status": "rejected",
                    "selected_option": None,
                    "interpretation": "Agent-authored edit rejected from human decision authority.",
                    "form_fields": {},
                    "rejection_reason": (
                        "Authored-comment / synthetic test exclusion: Edit was performed by "
                        "autonomous agent or test harness, not genuine human operator. "
                        "Agent-authored edits cannot authorize real work."
                    ),
                    "clarification_prompt": None,
                    "provenance": provenance,
                }
            # 3. Authorization Check
            else:
                auth_normalized = [a.lower().lstrip("@") for a in decision.authorized_responders]
                editor_clean = editor.lower().lstrip("@")
                if auth_normalized and editor_clean not in auth_normalized:
                    parse_result = {
                        "status": "rejected",
                        "selected_option": None,
                        "interpretation": f"Editor '@{editor}' is not authorized to answer this decision.",
                        "form_fields": {},
                        "rejection_reason": (
                            f"Unauthorized editor '@{editor}'. Authorized responders: "
                            f"{', '.join('@' + a for a in decision.authorized_responders)}"
                        ),
                        "clarification_prompt": None,
                        "provenance": ProvenanceType.UNAUTHORIZED_ACTOR,
                    }
                else:
                    # 4. Transition Analysis
                    old_options = extract_task_list_options(old_body)
                    new_options = extract_task_list_options(new_body)
                    currently_checked = [opt_id for opt_id, info in new_options.items() if info["checked"]]
                    additional_context = extract_additional_context(new_body)
                    options_summary = " or ".join(f"Option {o['id']} ({o['label']})" for o in decision.options)

                    if len(currently_checked) > 1:
                        parse_result = {
                            "status": "clarification_requested",
                            "selected_option": None,
                            "selection_method": "task_list_checkbox",
                            "additional_context": additional_context or None,
                            "notes": None,
                            "interpretation": f"Ambiguous edit: Multiple options checked ({', '.join(currently_checked)}).",
                            "form_fields": {},
                            "rejection_reason": None,
                            "clarification_prompt": (
                                f"Multiple options were selected in the edit by @{editor}: {', '.join(currently_checked)}. "
                                f"Please choose only one option from {options_summary}."
                            ),
                            "provenance": provenance,
                        }
                    elif len(currently_checked) == 1:
                        chosen_id = currently_checked[0]
                        matched_opt = None
                        for opt in decision.options:
                            if opt["id"].lower() == chosen_id.lower():
                                matched_opt = opt
                                break
                        if matched_opt:
                            interp = f"Explicit choice via task list: Option {matched_opt['id']} ({matched_opt['label']})"
                            if additional_context:
                                interp += f" (notes: {additional_context})"
                            parse_result = {
                                "status": "answered",
                                "selected_option": matched_opt,
                                "selection_method": "task_list_checkbox",
                                "additional_context": additional_context or None,
                                "notes": additional_context or None,
                                "interpretation": interp,
                                "form_fields": {},
                                "rejection_reason": None,
                                "clarification_prompt": None,
                                "provenance": provenance,
                            }
                        else:
                            parse_result = {
                                "status": "clarification_requested",
                                "selected_option": None,
                                "selection_method": "task_list_checkbox",
                                "additional_context": additional_context or None,
                                "notes": None,
                                "interpretation": f"Unrecognized option '{chosen_id}' checked.",
                                "form_fields": {},
                                "rejection_reason": None,
                                "clarification_prompt": (
                                    f"Option '{chosen_id}' is not one of the available options. "
                                    f"Please choose from {options_summary}."
                                ),
                                "provenance": provenance,
                            }
                    else:
                        # Zero options checked
                        if additional_context:
                            parse_result = {
                                "status": "clarification_requested",
                                "selected_option": None,
                                "selection_method": "alternative_proposal",
                                "alternative_proposal": additional_context,
                                "additional_context": additional_context,
                                "notes": additional_context,
                                "interpretation": f"Alternative proposal / custom response received from @{editor}: '{additional_context}' (retained for interpretation; tasks remain blocked until an authorized option or scope is approved).",
                                "form_fields": {},
                                "rejection_reason": None,
                                "clarification_prompt": (
                                    f"Alternative proposal received from @{editor}: '{additional_context}'. "
                                    f"Recorded for interpretation; please choose an option from {options_summary} to advance automatically."
                                ),
                                "provenance": provenance,
                            }
                        else:
                            parse_result = {
                                "status": "clarification_requested",
                                "selected_option": None,
                                "selection_method": "no_selection",
                                "additional_context": None,
                                "notes": None,
                                "interpretation": f"No option selected in edit by @{editor}.",
                                "form_fields": {},
                                "rejection_reason": None,
                                "clarification_prompt": f"No option selected. Please choose an option from {options_summary}.",
                                "provenance": provenance,
                            }

            now = get_iso_timestamp()
            audit_entry = {
                "timestamp": now,
                "responder": editor,
                "comment_id": comment_id,
                "comment_url": comment_url,
                "reply_text": new_body,
                "status": parse_result["status"],
                "provenance": parse_result.get("provenance", provenance),
                "interpretation": parse_result["interpretation"],
                "rejection_reason": parse_result["rejection_reason"],
                "clarification_prompt": parse_result["clarification_prompt"],
                "is_test": is_test,
                "comment_created_at": edit_time,
                "comment_updated_at": edit_time,
                "comment_time_provenance": CommentTimeProvenance.API_VERIFIED if edit_time else CommentTimeProvenance.CALLER_SUPPLIED,
                "event_type": event_type,
            }
            dec_dict.setdefault("audit_trail", []).append(audit_entry)

            unblocked = []
            if parse_result["status"] == "answered":
                opt = parse_result["selected_option"]
                proof_created_at, created_at_source = verified_comment_created_at(
                    edit_time,
                    CommentTimeProvenance.API_VERIFIED if edit_time else CommentTimeProvenance.CALLER_SUPPLIED,
                )
                ans_data = {
                    "comment_id": comment_id,
                    "comment_url": comment_url,
                    "responder": editor,
                    "answered_at": now,
                    "comment_created_at": proof_created_at,
                    "comment_created_at_source": created_at_source,
                    "raw_text": new_body,
                    "selected_option_id": opt["id"] if opt else None,
                    "selected_option_label": opt["label"] if opt else None,
                    "interpretation": parse_result["interpretation"],
                    "form_fields": {},
                    "selection_method": parse_result.get("selection_method", "task_list_checkbox"),
                    "additional_context": parse_result.get("additional_context"),
                    "notes": parse_result.get("notes"),
                    "alternative_proposal": parse_result.get("alternative_proposal"),
                    "provenance": parse_result.get("provenance", provenance),
                    "is_test": is_test,
                }

                if is_test or parse_result.get("provenance") != ProvenanceType.HUMAN_OPERATOR:
                    dec_dict["rejection_reason"] = (
                        "Synthetic test verified option parsing, but real task unblock is prohibited "
                        "for synthetic/test provenance."
                    )
                else:
                    clean_editor = str(editor or "").strip().lstrip("@")
                    prov_type = parse_result.get("provenance", provenance)

                    for req_id in decision.blocking_dependencies:
                        try:
                            if hasattr(self.ledger, "resolve_decision"):
                                self.ledger.resolve_decision(
                                    req_id=req_id,
                                    decision_id=decision.decision_id,
                                    answer=parse_result["interpretation"],
                                    comment_id=comment_id,
                                    provenance_type=prov_type,
                                    actor=clean_editor,
                                )
                            ev_payload = {
                                "type": "human_decision",
                                "summary": f"Decision [{decision.decision_id}] answered by @{clean_editor}: {parse_result['interpretation']}",
                                "details": (
                                    f"Event: {event_type} | URL: {comment_url} | "
                                    f"Selected: {opt['id'] if opt else 'N/A'} - {opt['label'] if opt else 'chosen option'} | "
                                    f"Provenance: {prov_type}"
                                    + (f" | Context: '{parse_result.get('additional_context')}'" if parse_result.get("additional_context") else "")
                                ),
                                "recorded_by": f"decision-workflow:@{clean_editor}",
                                "comment_id": comment_id,
                                "comment_url": comment_url,
                                "responder": editor,
                            }
                            upd_kwargs = {
                                "req_id": req_id,
                                "clear_blocker": True,
                                "next_action": f"Proceed with implementation following decision [{decision.decision_id}]: {opt['label'] if opt else 'chosen option'}",
                                "add_evidence": ev_payload,
                                "actor": clean_editor,
                                "reason": f"Human decision [{decision.decision_id}] resolved via task-list selection: {parse_result['interpretation']}",
                            }
                            if hasattr(self.ledger, "clear_decision_blocker"):
                                upd_kwargs["clear_decision_blocker"] = decision.decision_id
                            self.ledger.update_request(**upd_kwargs)
                            unblocked.append(req_id)
                        except KeyError:
                            pass

                    dec_dict["status"] = "answered"
                    dec_dict["answer"] = ans_data
                    dec_dict["clarification_prompt"] = None
                    dec_dict["rejection_reason"] = None

            elif parse_result["status"] == "clarification_requested":
                dec_dict["status"] = "clarification_requested"
                dec_dict["clarification_prompt"] = parse_result["clarification_prompt"]
                if parse_result.get("alternative_proposal"):
                    dec_dict["alternative_proposal"] = parse_result["alternative_proposal"]
                    dec_dict["last_alternative_proposal"] = parse_result["alternative_proposal"]
                    dec_dict["alternative_responder"] = editor
                    dec_dict["alternative_received_at"] = now
                for req_id in self._blockable_requests(decision.blocking_dependencies):
                    try:
                        blocker_msg = (
                            f"BLOCKED: Alternative proposal received on decision [{decision.decision_id}] from @{editor}: '{parse_result['alternative_proposal']}'. Awaiting interpretation/clarification."
                            if parse_result.get("alternative_proposal")
                            else f"BLOCKED: Clarification requested on decision [{decision.decision_id}] from @{editor}"
                        )
                        next_act = (
                            f"Awaiting operator choice or interpretation of alternative proposal on decision [{decision.decision_id}]"
                            if parse_result.get("alternative_proposal")
                            else f"Awaiting clarified response on decision [{decision.decision_id}]"
                        )
                        self.ledger.update_request(
                            req_id=req_id,
                            blocker=blocker_msg,
                            next_action=next_act,
                            actor=f"decision-workflow:@{editor}",
                            reason=f"Clarification or alternative proposal for decision [{decision.decision_id}]",
                        )
                    except KeyError:
                        pass

            elif parse_result["status"] == "rejected":
                rejected_record = {
                    "comment_id": str(comment_id) if comment_id else None,
                    "comment_url": comment_url,
                    "responder": editor,
                    "fingerprint": rejected_input_fingerprint(new_body, edit_time),
                    "reason": parse_result["rejection_reason"],
                    "provenance": parse_result.get("provenance", provenance),
                    "interpretation": parse_result["interpretation"],
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "occurrences": 1,
                    "event_type": event_type,
                }
                if comment_id:
                    dec_dict.setdefault("rejected_inputs", {})[str(comment_id)] = rejected_record
                dec_dict["last_rejected_input"] = dict(rejected_record)
                dec_dict["rejection_reason"] = parse_result["rejection_reason"]
                for req_id in self._blockable_requests(decision.blocking_dependencies):
                    try:
                        self.ledger.update_request(
                            req_id=req_id,
                            blocker=f"BLOCKED: Decision [{decision.decision_id}] edit from @{editor} was REJECTED: {parse_result['rejection_reason']}",
                            next_action=f"Awaiting valid authorized response on decision [{decision.decision_id}]",
                            actor=f"decision-workflow:@{editor}",
                            reason=f"Rejected edit on decision [{decision.decision_id}]",
                        )
                    except KeyError:
                        pass

            self._save_data_unlocked(data)

            return {
                "idempotent_replay": False,
                "status": parse_result["status"],
                "decision_id": decision_id,
                "interpretation": parse_result["interpretation"],
                "rejection_reason": parse_result["rejection_reason"],
                "clarification_prompt": parse_result["clarification_prompt"],
                "unblocked_requests": unblocked,
                "question_status": dec_dict.get("status", DecisionStatus.PENDING),
                "provenance": parse_result.get("provenance", provenance),
            }

    def ingest_github_event(
        self,
        event_payload: Dict[str, Any],
        decision_id: Optional[str] = None,
        repo: str = DEFAULT_REPO,
        is_test: bool = False,
    ) -> Dict[str, Any]:
        """
        Ingest a GitHub webhook event payload (issues.edited, issue_comment.created, issue_comment.edited).
        Validates event structure, sender authorization, decision scope, and transition.
        """
        action = event_payload.get("action", "")
        sender_data = event_payload.get("sender", {})
        sender = sender_data.get("login", "")
        issue_data = event_payload.get("issue", {})
        issue_number = issue_data.get("number")
        comment_data = event_payload.get("comment")
        changes = event_payload.get("changes", {})

        target_text = ""
        if comment_data:
            target_text = comment_data.get("body", "")
        elif issue_data:
            target_text = issue_data.get("body", "")

        resolved_decision_id = decision_id
        if not resolved_decision_id and target_text:
            m = re.search(
                r"(?:decision-(?:question|options|form|context):\s*|Decision Needed:\s*`?)([\w-]+)",
                target_text,
                re.IGNORECASE,
            )
            if m:
                resolved_decision_id = m.group(1).strip("`")

        if not resolved_decision_id and issue_number:
            open_decs = [
                d["decision_id"]
                for d in self.list_open_decisions()
                if d.get("issue_number") == issue_number
            ]
            if len(open_decs) == 1:
                resolved_decision_id = open_decs[0]

        if not resolved_decision_id:
            return {
                "status": "ignored",
                "reason": f"Could not correlate event to a known decision (issue #{issue_number}).",
                "action": action,
                "sender": sender,
            }

        if comment_data and action == "edited":
            old_body = changes.get("body", {}).get("from", "")
            new_body = comment_data.get("body", "")
            return self.process_issue_edit(
                decision_id=resolved_decision_id,
                old_body=old_body,
                new_body=new_body,
                editor=sender,
                event_type="comment_edit",
                comment_id=str(comment_data.get("id")),
                comment_url=comment_data.get("html_url"),
                edit_time=comment_data.get("updated_at"),
                provenance=ProvenanceType.SYNTHETIC_TEST if is_test else ProvenanceType.HUMAN_OPERATOR,
                is_test=is_test,
            )

        elif comment_data and action == "created":
            return self.process_reply(
                decision_id=resolved_decision_id,
                reply_text=comment_data.get("body", ""),
                responder=sender,
                comment_id=str(comment_data.get("id")),
                comment_url=comment_data.get("html_url"),
                provenance=ProvenanceType.SYNTHETIC_TEST if is_test else ProvenanceType.HUMAN_OPERATOR,
                is_test=is_test,
                comment_created_at=comment_data.get("created_at"),
                comment_updated_at=comment_data.get("updated_at"),
                comment_time_provenance=CommentTimeProvenance.API_VERIFIED,
            )

        elif issue_data and action == "edited" and not comment_data:
            old_body = changes.get("body", {}).get("from", "")
            new_body = issue_data.get("body", "")
            return self.process_issue_edit(
                decision_id=resolved_decision_id,
                old_body=old_body,
                new_body=new_body,
                editor=sender,
                event_type="issue_edit",
                comment_id=None,
                comment_url=issue_data.get("html_url"),
                edit_time=issue_data.get("updated_at"),
                provenance=ProvenanceType.SYNTHETIC_TEST if is_test else ProvenanceType.HUMAN_OPERATOR,
                is_test=is_test,
            )

        return {
            "status": "ignored",
            "reason": f"Unhandled event action '{action}' for decision '{resolved_decision_id}'.",
            "action": action,
            "sender": sender,
        }


    def sync_decisions(
        self,
        decision_id: Optional[str] = None,
        repo: str = DEFAULT_REPO,
        once: bool = True,
        max_iterations: int = 1,
        interval_seconds: float = 10.0,
    ) -> Dict[str, Any]:
        """
        Bounded one-shot synchronization or polling loop to scan GitHub issue comments
        for pending decisions.

        Note on Autonomous Resumption Limitations:
          GitHub issue comments do NOT trigger automatic webhooks into local developer
          environments. Autonomous resumption relies on coordinator-driven periodic polling
          or bounded one-shot sync calls at execution barriers.
        """
        target_ids = [decision_id] if decision_id else [
            d["decision_id"] for d in self.list_open_decisions()
        ]

        summary = {
            "iterations_run": 0,
            "decisions_checked": target_ids,
            "comments_evaluated": 0,
            "unblocked_requests": [],
            "resolved_decisions": [],
        }

        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            summary["iterations_run"] = iteration

            for d_id in target_ids:
                try:
                    dec = self.get_decision(d_id)
                    if dec.get("status", DecisionStatus.PENDING) not in OPEN_DECISION_STATUSES:
                        continue
                    issue_num = dec.get("issue_number")
                    if not issue_num:
                        continue

                    # Fetch comments on issue via gh CLI
                    cmd = [
                        "gh",
                        "api",
                        f"repos/{repo}/issues/{issue_num}/comments",
                        "--jq",
                        ".[] | {id: .id, user: .user.login, body: .body, created_at: .created_at, updated_at: .updated_at, html_url: .html_url, issue_url: .issue_url}",
                    ]
                    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
                    comments = []
                    for line in res.stdout.strip().splitlines():
                        if line.strip():
                            try:
                                comments.append(json.loads(line.strip()))
                            except json.JSONDecodeError:
                                pass

                    for c in comments:
                        c_id = str(c.get("id"))
                        c_body = c.get("body", "")
                        summary["comments_evaluated"] += 1

                        # If question comment, check for task-list checkbox selection
                        if f"decision-question:{d_id}" in c_body or "### ❓ Decision Needed:" in c_body:
                            task_opts = extract_task_list_options(c_body)
                            checked_task_ids = [opt_id for opt_id, info in task_opts.items() if info["checked"]]
                            if checked_task_ids:
                                ingest_res = self.process_issue_edit(
                                    decision_id=d_id,
                                    old_body="",
                                    new_body=c_body,
                                    editor=dec.get("authorized_responders", ["Wladefant"])[0],
                                    event_type="comment_edit",
                                    comment_id=c_id,
                                    comment_url=c.get("html_url"),
                                    edit_time=c.get("updated_at"),
                                    provenance=ProvenanceType.HUMAN_OPERATOR,
                                    is_test=False,
                                )
                                if ingest_res.get("status") == "answered":
                                    summary["resolved_decisions"].append(d_id)
                                    summary["unblocked_requests"].extend(ingest_res.get("unblocked_requests", []))
                                    break
                            continue

                        # Ingest comment through verified pipeline. The stream came
                        # straight out of the issue comments API, so its creation
                        # times are proof; the ingest clock below never is.
                        ingest_res = self.process_reply(
                            decision_id=d_id,
                            reply_text=c_body,
                            responder=c.get("user", ""),
                            comment_id=c_id,
                            comment_url=c.get("html_url"),
                            provenance=ProvenanceType.HUMAN_OPERATOR,
                            is_test=False,
                            comment_created_at=c.get("created_at"),
                            comment_updated_at=c.get("updated_at"),
                            comment_time_provenance=CommentTimeProvenance.API_VERIFIED,
                        )

                        if ingest_res.get("status") == "answered":
                            summary["resolved_decisions"].append(d_id)
                            summary["unblocked_requests"].extend(ingest_res.get("unblocked_requests", []))
                            break

                except Exception as e:
                    pass

            if once or summary["resolved_decisions"] or iteration >= max_iterations:
                break

            time.sleep(interval_seconds)

        return summary

    def get_decision(self, decision_id: str) -> Dict[str, Any]:
        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            if decision_id not in data["decisions"]:
                raise KeyError(f"Decision '{decision_id}' not found.")
            return data["decisions"][decision_id]

    def list_decisions(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            results = []
            for d in data["decisions"].values():
                if status and d.get("status") != status:
                    continue
                results.append(d)
            return results

    def list_open_decisions(self) -> List[Dict[str, Any]]:
        """Questions that are still unresolved, and therefore still worth scanning."""
        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            return [
                d
                for d in data["decisions"].values()
                if d.get("status", DecisionStatus.PENDING) in OPEN_DECISION_STATUSES
            ]

    def _evaluate_recovery_binding(
        self,
        decision_id: str,
        blocking_dependencies: List[str],
    ) -> Dict[str, Any]:
        """
        Decide, from the ledger, whether a legacy recovery has real open work to bind to.

        A decision record's own `blocking_dependencies` is a self-report: it says which
        requests the question was raised for, not which requests are still waiting.
        Trusting it is what let a recovery stamp a fresh blocker onto a request that had
        already reached `done`, whose ledger decision entry named a different decision id
        entirely, and drag the finished issue back into every sync scan.

        So the binding has to be proved on the ledger side, per request:
          * a matching decision entry (same id) that the ledger does not consider
            resolved, and
          * a request that is not in a terminal state.
        Only those requests are recoverable. Terminal requests are reported and left
        strictly alone — a mixed record still recovers, but nothing is written to the
        finished half. If nothing is recoverable, recovery refuses.
        """
        recoverable: List[str] = []
        terminal: List[Dict[str, Any]] = []
        unbound: List[Dict[str, Any]] = []
        ambiguous: List[Dict[str, Any]] = []

        for req_id in blocking_dependencies:
            try:
                req_data = self.ledger.get_request(req_id)
            except KeyError:
                unbound.append({"request_id": req_id, "why": "request is not in the ledger"})
                continue
            except Exception as e:
                raise DecisionRecoveryRefused(
                    "ledger_unreadable",
                    f"Cannot cross-check decision '{decision_id}' against request "
                    f"'{req_id}': {e}. Recovery refuses to reopen a question it cannot verify.",
                )

            entries = [
                entry
                for entry in (req_data.get("decisions") or [])
                if str(entry.get("id")) == str(decision_id)
            ]
            # A decision the ledger already resolved is terminal there too, whatever
            # state the request itself is in.
            for entry in entries:
                if entry.get("status") == "resolved" or entry.get("answer"):
                    raise DecisionRecoveryRefused(
                        "ambiguous_ledger_resolution",
                        f"Request '{req_id}' records decision '{decision_id}' as resolved "
                        f"(answer: {entry.get('answer')!r}). Recovery refuses to reopen a "
                        "question the ledger considers answered.",
                    )

            state = req_data.get("state")
            if state in TERMINAL_REQUEST_STATES:
                terminal.append(
                    {"request_id": req_id, "state": state, "has_matching_entry": bool(entries)}
                )
                continue
            if not entries:
                named = [str(e.get("id")) for e in (req_data.get("decisions") or [])]
                unbound.append(
                    {
                        "request_id": req_id,
                        "why": (
                            f"state '{state}' but no ledger decision entry names "
                            f"'{decision_id}' (entries present: {named or 'none'})"
                        ),
                    }
                )
                continue
            if len(entries) > 1:
                ambiguous.append(
                    {
                        "request_id": req_id,
                        "why": (
                            f"{len(entries)} ledger decision entries name '{decision_id}' "
                            f"with statuses {[e.get('status') for e in entries]}"
                        ),
                    }
                )
                continue
            recoverable.append(req_id)

        binding = {
            "recoverable": recoverable,
            "terminal": terminal,
            "unbound": unbound,
            "ambiguous": ambiguous,
            "refusal_code": None,
            "refusal_message": None,
        }
        if recoverable:
            return binding

        if not blocking_dependencies:
            binding["refusal_code"] = "no_blocking_work"
            binding["refusal_message"] = (
                f"Decision '{decision_id}' names no blocking request, so no work is waiting "
                "on an answer. Recovery reopens a question only for work the ledger shows "
                "is still blocked by it."
            )
        elif ambiguous:
            binding["refusal_code"] = "ambiguous_ledger_binding"
            binding["refusal_message"] = (
                f"Decision '{decision_id}' binds ambiguously in the ledger: "
                + "; ".join(f"{a['request_id']}: {a['why']}" for a in ambiguous)
                + ". Recovery refuses to guess which binding is the real one."
            )
        elif terminal and not unbound:
            binding["refusal_code"] = "all_blocking_work_terminal"
            binding["refusal_message"] = (
                f"Every request blocked by decision '{decision_id}' is finished work: "
                + "; ".join(
                    f"{t['request_id']} is '{t['state']}'"
                    + ("" if t["has_matching_entry"] else " and carries no matching decision entry")
                    for t in terminal
                )
                + ". Reopening the question would add a blocker to completed work and pull it "
                "back into the sync window, so recovery refuses."
            )
        else:
            details = [f"{u['request_id']}: {u['why']}" for u in unbound]
            details += [
                f"{t['request_id']}: state '{t['state']}' is terminal" for t in terminal
            ]
            binding["refusal_code"] = "missing_ledger_binding"
            binding["refusal_message"] = (
                f"No open request carries an unresolved ledger decision entry for "
                f"'{decision_id}': " + "; ".join(details) + ". Recovery refuses to reopen a "
                "question with no verifiable binding to blocked work."
            )
        return binding

    def recover_rejected_question(
        self,
        decision_id: str,
        actor: str = "operator",
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Reopen a legacy record whose *question* was stamped `rejected` by a refused
        reply, so an authorized operator can still answer it.

        Fail-closed on both halves of the record:

          * the decision must be an unanswered question with demonstrable
            input-rejection history and a still-safe scope; anything terminal,
            resolved, historyless or ambiguous is refused;
          * the ledger must independently show at least one non-terminal request
            holding an unresolved decision entry for this exact id. An all-terminal,
            mismatched, missing or ambiguous binding is refused, so recovery cannot
            reopen a question whose work is already finished.

        Recovery restores the question and nothing else: it never writes an answer,
        never clears a blocker, never touches authorization, and never writes to a
        terminal or unbound request.
        """
        with FileLock(self.lock_path):
            data = self._load_data_unlocked()
            if decision_id not in data["decisions"]:
                raise KeyError(f"Decision '{decision_id}' not found.")
            dec_dict = data["decisions"][decision_id]

            status = dec_dict.get("status", DecisionStatus.PENDING)
            if status != DecisionStatus.REJECTED:
                raise DecisionRecoveryRefused(
                    "status_not_legacy_rejected",
                    f"Decision '{decision_id}' has status '{status}', not the legacy "
                    f"'{DecisionStatus.REJECTED}' question status. Recovery only reopens a "
                    "question that a refused reply wrote over; it never reopens an already "
                    "open, answered or otherwise terminal decision.",
                )
            if dec_dict.get("answer"):
                raise DecisionRecoveryRefused(
                    "answer_present",
                    f"Decision '{decision_id}' already carries a recorded answer (comment "
                    f"{dec_dict['answer'].get('comment_id')}). A resolved decision is terminal "
                    "and is never reopened by recovery.",
                )

            audit = dec_dict.get("audit_trail") or []
            if not any(e.get("status") == DecisionStatus.REJECTED for e in audit):
                raise DecisionRecoveryRefused(
                    "no_input_rejection_history",
                    f"Decision '{decision_id}' has no audited reply rejection, so there is no "
                    "evidence its 'rejected' status came from a refused input. Recovery refuses "
                    "to guess why a record is rejected.",
                )
            if any(e.get("status") == DecisionStatus.ANSWERED for e in audit):
                raise DecisionRecoveryRefused(
                    "ambiguous_answered_history",
                    f"Decision '{decision_id}' has an audited 'answered' reply but no committed "
                    "answer. That record is ambiguous and needs an operator, not an automatic "
                    "reopen.",
                )
            rejected_ids = [
                str(e.get("comment_id"))
                for e in audit
                if e.get("status") == DecisionStatus.REJECTED and e.get("comment_id") is not None
            ]

            contract = DecisionContract(
                decision_id=dec_dict["decision_id"],
                request_id=dec_dict["request_id"],
                prompt=dec_dict.get("prompt", ""),
                question=dec_dict.get("question", ""),
                options=dec_dict.get("options", []),
                recommendation=dec_dict.get("recommendation", ""),
                blocking_dependencies=dec_dict.get("blocking_dependencies", []),
                authorized_responders=dec_dict.get("authorized_responders", []),
                decision_scope=dec_dict.get(
                    "decision_scope", DecisionScope.ARCHITECTURAL_PREFERENCE
                ),
            )
            is_scope_valid, scope_err = validate_decision_scope(contract)
            if not is_scope_valid:
                raise DecisionRecoveryRefused(
                    "unsafe_question_scope",
                    f"Decision '{decision_id}' no longer passes scope validation and will not be "
                    f"reopened: {scope_err}",
                )

            # The ledger decides whether any still-open request is genuinely waiting
            # on this question. Nothing has been written yet, so a refusal here leaves
            # the record exactly as it was found.
            binding = self._evaluate_recovery_binding(
                decision_id, contract.blocking_dependencies
            )
            if not binding["recoverable"]:
                raise DecisionRecoveryRefused(
                    binding["refusal_code"], binding["refusal_message"]
                )

            restored = (
                DecisionStatus.CLARIFICATION_REQUESTED
                if dec_dict.get("clarification_prompt")
                else DecisionStatus.PENDING
            )
            now = get_iso_timestamp()
            prior_reason = dec_dict.get("rejection_reason")
            recovery_reason = reason or (
                "Legacy rejected question reopened: the reply was refused, the question was "
                "never answered."
            )
            dec_dict["status"] = restored
            dec_dict["rejection_reason"] = None
            dec_dict["recovery"] = {
                "recovered_at": now,
                "actor": actor,
                "reason": recovery_reason,
                "previous_status": DecisionStatus.REJECTED,
                "restored_status": restored,
                "prior_rejection_reason": prior_reason,
                "rejected_input_comment_ids": rejected_ids,
                "authorization_granted": False,
                "bound_requests": list(binding["recoverable"]),
                "terminal_requests_untouched": [t["request_id"] for t in binding["terminal"]],
                "unbound_requests_untouched": [u["request_id"] for u in binding["unbound"]],
            }
            dec_dict["updated_at"] = now
            audit.append(
                {
                    "timestamp": now,
                    "action": "legacy_rejected_recovery",
                    "actor": actor,
                    "status": restored,
                    "previous_status": DecisionStatus.REJECTED,
                    "restored_status": restored,
                    "prior_rejection_reason": prior_reason,
                    "reason": recovery_reason,
                    "rejected_input_comment_ids": rejected_ids,
                    "authorization_granted": False,
                    "bound_requests": list(binding["recoverable"]),
                    "terminal_requests_untouched": [
                        t["request_id"] for t in binding["terminal"]
                    ],
                    "unbound_requests_untouched": [
                        u["request_id"] for u in binding["unbound"]
                    ],
                }
            )
            dec_dict["audit_trail"] = audit
            self._save_data_unlocked(data)

        # Restate the blocker truthfully, and only on the requests the ledger proved
        # are still waiting. Blockers and authorization are deliberately left standing:
        # reopening a question grants no authority to act on it. Terminal and unbound
        # requests are never written to, so a finished half of a mixed record keeps its
        # completed shape byte for byte.
        responders = ", ".join(contract.authorized_responders) or "an authorized responder"
        for req_id in binding["recoverable"]:
            try:
                self.ledger.update_request(
                    req_id=req_id,
                    blocker=f"Awaiting human decision [{decision_id}]: {contract.question}",
                    next_action=(
                        f"Awaiting valid authorized response on recovered decision "
                        f"[{decision_id}] from {responders}"
                    ),
                    actor=f"decision-workflow:@{actor}",
                    reason=f"Recovered legacy rejected decision {decision_id}",
                )
            except KeyError:
                pass

        return {
            "recovered": True,
            "decision_id": decision_id,
            "previous_status": DecisionStatus.REJECTED,
            "restored_status": restored,
            "prior_rejection_reason": prior_reason,
            "rejected_input_comment_ids": rejected_ids,
            "answer": None,
            "authorization_granted": False,
            "blocking_dependencies": contract.blocking_dependencies,
            "bound_requests": list(binding["recoverable"]),
            "terminal_requests_untouched": [t["request_id"] for t in binding["terminal"]],
            "unbound_requests_untouched": [u["request_id"] for u in binding["unbound"]],
            "recovered_at": now,
            "actor": actor,
            "reason": recovery_reason,
        }


# ----------------------------------------------------------------------
# GitHub CLI Integration (post, scan comments)
# ----------------------------------------------------------------------

def post_decision_to_github_issue(
    decision_id: str,
    issue_number: int,
    repo: str = DEFAULT_REPO,
    manager: Optional[DecisionManager] = None,
) -> Dict[str, Any]:
    """Format and post decision question to a GitHub issue via gh CLI."""
    mgr = manager or DecisionManager()
    dec_dict = mgr.get_decision(decision_id)
    decision = DecisionContract(
        decision_id=dec_dict["decision_id"],
        request_id=dec_dict["request_id"],
        prompt=dec_dict["prompt"],
        question=dec_dict["question"],
        options=dec_dict["options"],
        recommendation=dec_dict["recommendation"],
        blocking_dependencies=dec_dict["blocking_dependencies"],
        authorized_responders=dec_dict["authorized_responders"],
        decision_scope=dec_dict.get("decision_scope", DecisionScope.ARCHITECTURAL_PREFERENCE),
        status=dec_dict.get("status", "pending"),
        format_preference=dec_dict.get("format_preference", "plain"),
    )

    body = format_decision_markdown(decision)

    cmd = [
        "gh",
        "issue",
        "comment",
        str(issue_number),
        "--repo",
        repo,
        "--body",
        body,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    comment_url = res.stdout.strip()

    # Extract comment ID from URL if possible
    comment_id = None
    match = re.search(r"#issuecomment-(\d+)", comment_url)
    if match:
        comment_id = match.group(1)
        mgr.record_authored_comment(comment_id)

    # Update decision record with issue details
    with FileLock(mgr.lock_path):
        data = mgr._load_data_unlocked()
        data["decisions"][decision_id]["issue_number"] = issue_number
        data["decisions"][decision_id]["issue_url"] = comment_url
        data["decisions"][decision_id]["question_comment_id"] = comment_id
        data["decisions"][decision_id]["question_posted_at"] = get_iso_timestamp()
        mgr._save_data_unlocked(data)

    return {
        "status": "posted",
        "comment_url": comment_url,
        "comment_id": comment_id,
        "decision_id": decision_id,
    }
def ingest_github_event(
    event_payload: Dict[str, Any],
    decision_id: Optional[str] = None,
    repo: str = DEFAULT_REPO,
    is_test: bool = False,
    decisions_path: Optional[str] = None,
    ledger_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Ingest a GitHub webhook event payload via a fresh DecisionManager instance."""
    mgr = DecisionManager(decisions_path=decisions_path, ledger_path=ledger_path)
    return mgr.ingest_github_event(
        event_payload=event_payload,
        decision_id=decision_id,
        repo=repo,
        is_test=is_test,
    )



# ----------------------------------------------------------------------
# CLI Interface
# ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Issue-based asynchronous human decision workflow utility"
    )
    parser.add_argument("--decisions", default=None, help="Path to decisions JSON")
    parser.add_argument("--ledger", default=None, help="Path to ledger JSON")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # ASK
    p_ask = subparsers.add_parser("ask", help="Register a new decision question")
    p_ask.add_argument("--id", required=True, help="Decision ID (e.g. DEC-001)")
    p_ask.add_argument("--request-id", required=True, help="Target ledger request ID")
    p_ask.add_argument("--prompt", required=True, help="Prompt or task goal")
    p_ask.add_argument("--question", required=True, help="Concrete question")
    p_ask.add_argument(
        "--options",
        required=True,
        help="JSON list of options [{'id':'A','label':'...','description':'...','tradeoffs':'...'}]",
    )
    p_ask.add_argument("--recommendation", required=True, help="Recommended option")
    p_ask.add_argument("--blocks", required=True, help="Comma-separated request IDs blocked")
    p_ask.add_argument(
        "--authorized", default="Wladefant", help="Comma-separated authorized responders"
    )
    p_ask.add_argument(
        "--scope",
        default=DecisionScope.ARCHITECTURAL_PREFERENCE,
        choices=ALLOWED_DECISION_SCOPES,
        help="Decision scope (strictly architectural/design only)",
    )
    p_ask.add_argument("--issue", type=int, default=None, help="GitHub issue number to attach/post")
    p_ask.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo")
    p_ask.add_argument("--post", action="store_true", help="Post to GitHub issue immediately")

    # INGEST
    p_ing = subparsers.add_parser("ingest", help="Ingest and verify a reply comment from GitHub API")
    p_ing.add_argument("id", help="Decision ID")
    p_ing.add_argument("--comment-id", required=True, help="GitHub issue comment ID")
    p_ing.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo")
    p_ing.add_argument("--expected-responder", default=None, help="Optional responder to verify against API")
    p_ing.add_argument("--expected-text", default=None, help="Optional text to verify against API")
    p_ing.add_argument(
        "--expected-created-at",
        default=None,
        help="Optional comment creation timestamp to verify against the API value",
    )
    p_ing.add_argument("--test", action="store_true", help="Flag as synthetic test probe (cannot unblock real tasks)")

    # REPLY (Legacy / Test only)
    p_rep = subparsers.add_parser("reply", help="Process a reply (requires --comment-id or --test)")
    p_rep.add_argument("id", help="Decision ID")
    p_rep.add_argument("--text", required=True, help="Reply text (plain or form)")
    p_rep.add_argument("--responder", required=True, help="GitHub username of responder")
    p_rep.add_argument("--comment-id", default=None, help="Optional comment ID")
    p_rep.add_argument("--comment-url", default=None, help="Optional comment URL")
    p_rep.add_argument("--test", action="store_true", help="Mark as synthetic test (cannot unblock real ledger tasks)")

    # SYNC
    p_syn = subparsers.add_parser("sync", help="Synchronize pending decisions with GitHub issue comments")
    p_syn.add_argument("--id", default=None, help="Optional decision ID to sync (defaults to all pending)")
    p_syn.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo")
    p_syn.add_argument("--once", action="store_true", default=True, help="Perform bounded one-shot synchronization")
    p_syn.add_argument("--max-iterations", type=int, default=1, help="Max polling iterations")
    p_syn.add_argument("--interval", type=float, default=10.0, help="Polling interval seconds")

    # SHOW
    p_shw = subparsers.add_parser("show", help="Show decision contract details")
    p_shw.add_argument("id", help="Decision ID")
    p_shw.add_argument("--markdown", action="store_true", help="Display as GitHub issue markdown")

    # LIST
    p_lst = subparsers.add_parser("list", help="List all decisions")
    p_lst.add_argument(
        "--status",
        choices=["pending", "answered", "clarification_requested", "rejected"],
    )

    # RECOVER
    p_rcv = subparsers.add_parser(
        "recover",
        help="Reopen a legacy rejected question that was never answered (fail-closed)",
    )
    p_rcv.add_argument("id", help="Decision ID")
    p_rcv.add_argument("--actor", required=True, help="Operator requesting the recovery")
    p_rcv.add_argument(
        "--reason", default=None, help="Why this record is a legacy poisoned question"
    )
    p_rcv.add_argument("--json", action="store_true", help="Emit the recovery result as JSON")

    # INGEST-EVENT
    p_evt = subparsers.add_parser(
        "ingest-event",
        help="Ingest GitHub webhook event payload (issues.edited, issue_comment.created, issue_comment.edited)",
    )
    p_evt.add_argument("--event-path", default=None, help="Path to GitHub event payload JSON file (e.g. $GITHUB_EVENT_PATH)")
    p_evt.add_argument("--event-json", default=None, help="Inline JSON string of GitHub event payload")
    p_evt.add_argument("--id", default=None, help="Optional decision ID (overrides payload discovery)")
    p_evt.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repository name")
    p_evt.add_argument("--test", action="store_true", help="Flag as synthetic test probe (cannot unblock real tasks)")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    mgr = DecisionManager(decisions_path=args.decisions, ledger_path=args.ledger)

    try:
        if args.command == "ask":
            options = json.loads(args.options)
            blocks = [b.strip() for b in args.blocks.split(",") if b.strip()]
            authorized = [a.strip() for a in args.authorized.split(",") if a.strip()]

            contract = DecisionContract(
                decision_id=args.id,
                request_id=args.request_id,
                prompt=args.prompt,
                question=args.question,
                options=options,
                recommendation=args.recommendation,
                blocking_dependencies=blocks,
                authorized_responders=authorized,
                decision_scope=args.scope,
                issue_number=args.issue,
            )

            res = mgr.register_question(contract)
            print(f"[OK] Decision '{contract.decision_id}' registered in status '{res['status']}'.")
            print(f"     Scope: {contract.decision_scope}")
            print(f"     Blocked requests in ledger: {', '.join(contract.blocking_dependencies)}")

            if args.post and args.issue:
                post_res = post_decision_to_github_issue(
                    decision_id=contract.decision_id,
                    issue_number=args.issue,
                    repo=args.repo,
                    manager=mgr,
                )
                print(f"[OK] Posted decision to GitHub issue #{args.issue}: {post_res['comment_url']}")

        elif args.command == "ingest-event":
            if not args.event_path and not args.event_json:
                print("[ERROR] Must provide either --event-path or --event-json", file=sys.stderr)
                sys.exit(1)
            payload = {}
            if args.event_path:
                with open(args.event_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            else:
                payload = json.loads(args.event_json)

            res = mgr.ingest_github_event(
                event_payload=payload,
                decision_id=args.id,
                repo=args.repo,
                is_test=args.test,
            )
            print(f"Status: {res.get('status')}")
            if res.get("decision_id"):
                print(f"Decision ID: {res['decision_id']}")
            if res.get("interpretation"):
                print(f"Interpretation: {res['interpretation']}")
            if res.get("unblocked_requests"):
                print(f"Unblocked Requests: {', '.join(res['unblocked_requests'])}")
            if res.get("rejection_reason"):
                print(f"Rejection Reason: {res['rejection_reason']}")
            if res.get("clarification_prompt"):
                print(f"Clarification Prompt: {res['clarification_prompt']}")

        elif args.command == "ingest":
            res = mgr.ingest_comment(
                decision_id=args.id,
                comment_id=args.comment_id,
                repo=args.repo,
                caller_responder=args.expected_responder,
                caller_text=args.expected_text,
                caller_created_at=args.expected_created_at,
                is_test=args.test,
            )
            print(f"Decision:    {args.id}")
            print(f"Status:      {res['status']}")
            print(f"Provenance:  {res.get('provenance')}")
            print(f"Message:     {res['interpretation']}")
            if res.get("rejection_reason"):
                print(f"Rejection:   {res['rejection_reason']}")
            if res.get("clarification_prompt"):
                print(f"Clarification: {res['clarification_prompt']}")
            if res.get("unblocked_requests"):
                print(f"Unblocked Requests: {', '.join(res['unblocked_requests'])}")

        elif args.command == "reply":
            # Safety rule: Direct reply without comment-id must carry --test flag
            if not args.comment_id and not args.test:
                print(
                    "[ERROR] Direct reply ingestion without --comment-id is only allowed with --test flag. "
                    "Live decisions must be ingested via GitHub comment ID using 'ingest'.",
                    file=sys.stderr,
                )
                sys.exit(1)

            res = mgr.process_reply(
                decision_id=args.id,
                reply_text=args.text,
                responder=args.responder,
                comment_id=args.comment_id,
                comment_url=args.comment_url,
                provenance=ProvenanceType.SYNTHETIC_TEST if args.test else ProvenanceType.HUMAN_OPERATOR,
                is_test=args.test,
            )
            print(f"Decision:    {args.id}")
            print(f"Status:      {res['status']}")
            print(f"Provenance:  {res.get('provenance')}")
            print(f"Message:     {res['interpretation']}")
            if res.get("rejection_reason"):
                print(f"Rejection:   {res['rejection_reason']}")
            if res.get("clarification_prompt"):
                print(f"Clarification: {res['clarification_prompt']}")
            if res.get("unblocked_requests"):
                print(f"Unblocked Requests: {', '.join(res['unblocked_requests'])}")

        elif args.command == "sync":
            summary = mgr.sync_decisions(
                decision_id=args.id,
                repo=args.repo,
                once=args.once,
                max_iterations=args.max_iterations,
                interval_seconds=args.interval,
            )
            print(f"[SYNC COMPLETE] Iterations: {summary['iterations_run']}")
            print(f"Decisions checked:   {', '.join(summary['decisions_checked']) or 'None'}")
            print(f"Comments evaluated:  {summary['comments_evaluated']}")
            print(f"Resolved decisions:  {', '.join(summary['resolved_decisions']) or 'None'}")
            print(f"Unblocked requests:  {', '.join(summary['unblocked_requests']) or 'None'}")

        elif args.command == "show":
            dec = mgr.get_decision(args.id)
            if args.markdown:
                contract = DecisionContract(**{k: v for k, v in dec.items() if k in DecisionContract.__annotations__})
                print(format_decision_markdown(contract))
            else:
                print(json.dumps(dec, indent=2))

        elif args.command == "list":
            decs = mgr.list_decisions(status=args.status)
            print(f"{'ID':<18} {'STATUS':<24} {'SCOPE':<24} {'REQUEST':<18} {'AUTHORIZED'}")
            print("=" * 100)
            for d in decs:
                auth = ", ".join(d.get("authorized_responders", []))
                scope = d.get("decision_scope", "architectural_preference")
                print(f"{d['decision_id']:<18} {d['status']:<24} {scope:<24} {d['request_id']:<18} {auth}")

        elif args.command == "recover":
            res = mgr.recover_rejected_question(
                decision_id=args.id, actor=args.actor, reason=args.reason
            )
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"[RECOVERED] Decision '{args.id}' reopened as '{res['restored_status']}'.")
                print(f"     Prior rejection: {res['prior_rejection_reason']}")
                print(
                    "     Rejected inputs retained: "
                    f"{', '.join(res['rejected_input_comment_ids']) or 'none'}"
                )
                print(
                    "     Blocker restated on: "
                    f"{', '.join(res['bound_requests']) or 'none'}"
                )
                untouched = res["terminal_requests_untouched"] + res["unbound_requests_untouched"]
                if untouched:
                    print(f"     Left untouched (terminal or unbound): {', '.join(untouched)}")
                print(
                    "     No answer was manufactured; decision blockers and authorization "
                    "are unchanged."
                )

    except DecisionRecoveryRefused as e:
        print(f"[REFUSED] {e.code}: {e.message}", file=sys.stderr)
        sys.exit(3)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
