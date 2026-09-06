#!/usr/bin/env python3
"""
Portable Workflow Coordinator Entrypoint (~/.veyyon/workflows/coordinator.py)

A single bounded, harness-agnostic coordinator command that:
  1. Reads durable state from the request ledger (recovery cache).
  2. Syncs GitHub issue decisions where configured and available.
  3. Evaluates staging integration preflight gates and area-bound probes.
  4. Reads normalized usage metrics via a selected adapter (file fixture, veyyon, or direct).
  5. Selects reset-aware model routing and token-saving evidence packet.
  6. Emits one compact, machine-readable next-work packet or explicit status (ready, wait, block, done).

Inviolable Architectural Invariants:
  - Canonical System of Record: GitHub Issues and Superboard (Project #1) are authoritative.
  - No Auto-Merge / No Auto-Deploy: Protected operations require explicit human authorization.
  - No Self-Spawn Loop: Single bounded evaluation step; emits recommendation without self-spawning.
  - No Native Scheduler Dependencies: Standalone execution via pure Python standard library + gh.
  - Configurable State Paths & Zero Path Dependencies: Fully portable to any export directory.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# Ensure sibling workflow modules are in sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from ledger import FileLock, RequestLedger, get_iso_timestamp
except ImportError as e:
    raise ImportError(f"Failed to import ledger module from {SCRIPT_DIR}: {e}")

try:
    from decision_workflow import DecisionManager, DecisionScope, ProvenanceType
except ImportError as e:
    raise ImportError(f"Failed to import decision_workflow module from {SCRIPT_DIR}: {e}")

try:
    from preflight import PreflightEngine, PreflightResult, check_preflight
except ImportError as e:
    raise ImportError(f"Failed to import preflight module from {SCRIPT_DIR}: {e}")

try:
    from balance_loader import (
        BalanceAdapter,
        DirectJsonAdapter,
        FileBalanceAdapter,
        NormalizedBalanceSnapshot,
        get_balance_adapter,
        load_snapshot,
    )
except ImportError as e:
    raise ImportError(f"Failed to import balance_loader module from {SCRIPT_DIR}: {e}")

try:
    from model_routing import (
        HarnessDispatchPacket,
        ResetAwareModelSelector,
        RiskLevel,
        TaskType,
        model_to_agent_role,
    )
except ImportError as e:
    raise ImportError(f"Failed to import model_routing module from {SCRIPT_DIR}: {e}")

try:
    from project_adapter import (
        ProjectConfig,
        get_current_project_config,
        set_current_project_config,
        create_polysimulator_config,
        create_generic_config,
    )
except ImportError:
    get_current_project_config = None
    set_current_project_config = None


DEFAULT_REPO = "Bavariance/polysimulator"
DEFAULT_FIXTURE_FILENAME = "usage_fixture.json"
DEFAULT_CACHE_FILENAME = "usage_snapshot_cache.json"


@dataclass
class CoordinatorBoundaries:
    """Explicit system boundaries and safety guarantees."""
    auto_merge_allowed: bool = False
    auto_deploy_allowed: bool = False
    self_spawn_loop: bool = False
    execution_dispatched: bool = False
    shared_authority: str = "GitHub Issues & Superboard (Project #1)"
    local_recovery_cache: str = "ledger.json"


@dataclass
class RequestSummary:
    id: str
    state: str
    task_type: str
    owner: str
    prompt: str
    head: Optional[str]
    issue_number: Optional[int]
    issue_url: Optional[str]
    next_action: Optional[str]
    pending_criteria: List[str] = field(default_factory=list)
    labels: List[str] = field(default_factory=list)


@dataclass
class DecisionStatus:
    sync_attempted: bool
    sync_success: bool
    sync_message: str
    pending_count: int
    blocking_this_request: bool
    blocking_decision_ids: List[str] = field(default_factory=list)
    decision_details: Optional[Dict[str, Any]] = None


@dataclass
class PreflightStatus:
    evaluated: bool
    passed: bool
    status: str
    required_probes: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    probe_details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RoutingStatus:
    evaluated: bool
    recommended_model: Optional[str] = None
    recommended_role: Optional[str] = None
    task_type: Optional[str] = None
    risk_level: Optional[str] = None
    fallback_model: Optional[str] = None
    promotion_applied: bool = False
    cooldown_fallback: bool = False
    rationale: Optional[str] = None
    quota_context: Dict[str, Any] = field(default_factory=dict)
    evidence_packet_required: bool = False


@dataclass
class CoordinatorPacket:
    """
    Compact, machine-readable coordinator packet emitted on each bounded step.
    Can be ingested by Veyyon, Codex, Claude, or custom orchestrators.
    """
    schema_version: str
    generated_at_utc: str
    status: str  # 'ready', 'wait', 'block', 'done'
    status_reason: str
    next_action: str
    request: Optional[RequestSummary]
    decision_status: DecisionStatus
    preflight: PreflightStatus
    routing: RoutingStatus
    evidence_packet: Optional[Dict[str, Any]]
    boundaries: CoordinatorBoundaries = field(default_factory=CoordinatorBoundaries)
    notification: Optional[Dict[str, Any]] = None
    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


class Coordinator:
    """
    Harness-Agnostic Single-Step Bounded Workflow Coordinator.
    """

    def __init__(
        self,
        state_dir: Optional[str] = None,
        ledger_path: Optional[str] = None,
        decisions_path: Optional[str] = None,
        evidence_dir: Optional[str] = None,
        usage_adapter: str = "auto",
        balance_file: Optional[str] = None,
        repo: str = DEFAULT_REPO,
        sync_decisions: bool = True,
        notify_telegram: bool = False,
        telegram_project: Optional[str] = None,
        telegram_dry_run: bool = True,
        telegram_send: bool = False,
        telegram_pool_db: Optional[str] = None,
        project_config: Optional[Union[Any, str, dict]] = None,
        adapter_name: Optional[str] = None,
    ):
        self.state_dir = os.path.abspath(state_dir) if state_dir else SCRIPT_DIR
        self.ledger_path = os.path.abspath(
            ledger_path
            or (os.path.join(self.state_dir, "ledger.json") if state_dir else os.path.join(SCRIPT_DIR, "ledger.json"))
        )
        self.decisions_path = os.path.abspath(
            decisions_path
            or (os.path.join(self.state_dir, "decisions.json") if state_dir else os.path.join(SCRIPT_DIR, "decisions.json"))
        )
        self.evidence_dir = os.path.abspath(
            evidence_dir
            or (os.path.join(self.state_dir, "preflight_evidence") if state_dir else os.path.join(SCRIPT_DIR, "preflight_evidence"))
        )
        self.usage_adapter_name = usage_adapter
        self.balance_file = os.path.abspath(balance_file) if balance_file else None
        self.sync_decisions_enabled = sync_decisions
        self.notify_telegram = notify_telegram
        self.telegram_project = telegram_project
        self.telegram_dry_run = telegram_dry_run
        self.telegram_send = telegram_send
        # Shared correlation index a reply to a notification is resolved through. None
        # means "resolve it the way the session bridge does" (VEYYON_POOL_DB, else the
        # installed pool when present); a path here overrides that.
        self.telegram_pool_db = telegram_pool_db

        # Initialize project configuration adapter
        if project_config is not None and set_current_project_config is not None:
            self.project_config = set_current_project_config(project_config)
        elif adapter_name and set_current_project_config is not None:
            self.project_config = set_current_project_config(adapter_name)
        elif repo and repo != DEFAULT_REPO and set_current_project_config is not None:
            self.project_config = set_current_project_config(repo)
        elif get_current_project_config is not None:
            self.project_config = get_current_project_config()
        else:
            self.project_config = None

        self.repo = self.project_config.repo if self.project_config else repo

        # Initialize ledger and decision managers
        self.ledger = RequestLedger(ledger_path=self.ledger_path)
        self.decision_mgr = DecisionManager(
            decisions_path=self.decisions_path,
            ledger_path=self.ledger_path,
        )
        self.preflight_engine = PreflightEngine(
            evidence_dir=self.evidence_dir,
            project_config=self.project_config,
        )

    def get_boundaries(self) -> CoordinatorBoundaries:
        """Return explicit system boundaries adapted to the active project."""
        if self.project_config:
            authority = f"GitHub Issues & Superboard ({self.project_config.project_name} #{self.project_config.project_number})"
        else:
            authority = f"GitHub Issues & Superboard for {self.repo}"
        return CoordinatorBoundaries(
            auto_merge_allowed=False,
            auto_deploy_allowed=False,
            self_spawn_loop=False,
            execution_dispatched=False,
            shared_authority=authority,
            local_recovery_cache=os.path.basename(self.ledger_path),
        )

    def _finalize_packet(self, packet: CoordinatorPacket) -> CoordinatorPacket:
        """Attach adapted boundaries and optional notification receipt if enabled."""
        packet.boundaries = self.get_boundaries()
        if self.notify_telegram:
            packet.notification = self.maybe_notify_telegram(packet)
        return packet

    def maybe_notify_telegram(self, packet: CoordinatorPacket) -> Optional[Dict[str, Any]]:
        """Emit deduped status notification to Telegram on meaningful transitions."""
        if not self.notify_telegram:
            return None
        try:
            from telegram_notifier import OutboundCorrelationStore, TelegramNotificationAdapter
        except ImportError:
            return {
                "enabled": True,
                "delivered": False,
                "status": "failed",
                "reason": "telegram_notifier module not available",
            }

        try:
            project_name = self.telegram_project or (self.repo.split("/")[-1] if "/" in self.repo else self.repo)
            event = TelegramNotificationAdapter.from_coordinator_packet(packet.to_dict(), project_override=project_name)
            if not event:
                return {
                    "enabled": True,
                    "delivered": False,
                    "status": "filtered",
                    "reason": "Routine transition filtered per notification policy",
                }

            adapter = TelegramNotificationAdapter(
                # Deduplication state belongs to this coordinator's state directory, not
                # to whatever directory the process happens to be running in: a run from
                # a different cwd would otherwise start with an empty dedup window and
                # re-send notifications already delivered.
                state_dir_override=Path(self.state_dir),
                correlation_store=OutboundCorrelationStore(
                    Path(self.telegram_pool_db) if self.telegram_pool_db else None
                ),
            )
            dry_run_mode = self.telegram_dry_run or (not self.telegram_send)
            receipt = adapter.notify(event, dry_run=dry_run_mode)
            return asdict(receipt)
        except Exception as e:
            return {
                "enabled": True,
                "delivered": False,
                "status": "failed",
                "reason": f"Notification error: {e}",
            }

    def resolve_balance_adapter(self) -> BalanceAdapter:
        """Resolve a suitable balance adapter based on configuration and host environment."""
        if self.balance_file and os.path.exists(self.balance_file):
            return FileBalanceAdapter(self.balance_file)

        if self.usage_adapter_name == "file":
            candidate_paths = [
                os.path.join(self.state_dir, DEFAULT_FIXTURE_FILENAME),
                os.path.join(SCRIPT_DIR, DEFAULT_FIXTURE_FILENAME),
                os.path.join(self.state_dir, DEFAULT_CACHE_FILENAME),
                os.path.join(SCRIPT_DIR, DEFAULT_CACHE_FILENAME),
            ]
            for p in candidate_paths:
                if os.path.exists(p):
                    return FileBalanceAdapter(p)
            raise FileNotFoundError(
                f"Balance file adapter requested but no fixture found in: {candidate_paths}"
            )

        if self.usage_adapter_name == "veyyon":
            if not shutil.which("veyyon"):
                raise RuntimeError("Usage adapter 'veyyon' specified but 'veyyon' executable not found on PATH.")
            return get_balance_adapter(adapter_type="veyyon", allow_live=True)

        if self.usage_adapter_name == "direct":
            return get_balance_adapter(adapter_type="direct", allow_live=True)

        # 'auto' adapter resolution
        # 1. Check for fixture files in state_dir or script_dir
        candidate_paths = [
            os.path.join(self.state_dir, DEFAULT_FIXTURE_FILENAME),
            os.path.join(SCRIPT_DIR, DEFAULT_FIXTURE_FILENAME),
            os.path.join(self.state_dir, DEFAULT_CACHE_FILENAME),
            os.path.join(SCRIPT_DIR, DEFAULT_CACHE_FILENAME),
        ]
        for p in candidate_paths:
            if os.path.exists(p):
                return FileBalanceAdapter(p)

        # 2. Check if veyyon is available on PATH
        if shutil.which("veyyon"):
            try:
                return get_balance_adapter(adapter_type="veyyon", allow_live=True)
            except Exception:
                pass  # Fall back to internal minimal snapshot

        # 3. Fallback to minimal synthetic normalized snapshot (guarantees zero crash)
        fallback_data = {
            "schema_version": "1.0",
            "generated_at_utc": get_iso_timestamp(),
            "providers": {
                "google-antigravity": {
                    "provider": "google-antigravity",
                    "status": "ok",
                    "is_active": True,
                    "effective_remaining_fraction": 0.90,
                    "cycle_seconds_to_reset": 16000.0,
                    "cycle_hours_to_reset": 4.44,
                    "bottleneck_window_id": "Usage (Google)",
                    "windows": [],
                    "metadata": {},
                },
                "anthropic": {
                    "provider": "anthropic",
                    "status": "ok",
                    "is_active": True,
                    "effective_remaining_fraction": 0.95,
                    "cycle_seconds_to_reset": 500000.0,
                    "cycle_hours_to_reset": 138.8,
                    "bottleneck_window_id": "Claude 5 Hour",
                    "windows": [],
                    "metadata": {},
                },
                "openai-codex": {
                    "provider": "openai-codex",
                    "status": "ok",
                    "is_active": True,
                    "effective_remaining_fraction": 0.95,
                    "cycle_seconds_to_reset": 500000.0,
                    "cycle_hours_to_reset": 138.8,
                    "bottleneck_window_id": "7 days",
                    "windows": [],
                    "metadata": {},
                },
                "xai-oauth": {
                    "provider": "xai-oauth",
                    "status": "dormant",
                    "is_active": False,
                    "effective_remaining_fraction": 0.0,
                    "cycle_seconds_to_reset": 0.0,
                    "cycle_hours_to_reset": 0.0,
                    "bottleneck_window_id": "dormant",
                    "windows": [],
                    "metadata": {},
                },
            },
            "active_providers": ["anthropic", "google-antigravity", "openai-codex"],
            "dormant_providers": ["xai-oauth"],
        }
        return DirectJsonAdapter(fallback_data)

    def sync_decisions_if_configured(self) -> Tuple[bool, bool, str, int]:
        """Perform a single bounded one-shot sync of pending decisions with GitHub if enabled."""
        if not self.sync_decisions_enabled:
            return False, False, "Decision synchronization disabled via configuration.", 0

        has_gh = shutil.which("gh") is not None
        if not has_gh:
            return False, False, "gh CLI tool not found on PATH; skipping remote decision sync.", 0

        try:
            summary = self.decision_mgr.sync_decisions(
                repo=self.repo,
                once=True,
                max_iterations=1,
            )
            resolved_count = len(summary.get("resolved_decisions", []))
            checked_count = len(summary.get("decisions_checked", []))
            msg = (
                f"Sync completed: checked {checked_count} decision(s), "
                f"resolved {resolved_count} decision(s)."
            )
            return True, True, msg, resolved_count
        except Exception as e:
            return True, False, f"Decision sync failed: {e}", 0

    def select_target_request(
        self, request_id: Optional[str] = None
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Select target request from ledger (specified ID, next runnable, or first active)."""
        data = self.ledger._load_data_unlocked()
        requests = data.get("requests", {})
        if not requests:
            return None, "No requests found in ledger."

        if request_id:
            req = requests.get(request_id)
            if not req:
                return None, f"Request '{request_id}' not found in ledger."
            return req, None

        # Check if any requests are runnable via ledger.next_actions()
        actions = self.ledger.next_actions()
        runnable = [a for a in actions if not a.get("is_blocked")]
        if runnable:
            req_id_cand = runnable[0]["id"]
            return requests.get(req_id_cand), None

        # Otherwise select first candidate from next_actions (e.g. blocked or waiting)
        if actions:
            req_id_cand = actions[0]["id"]
            return requests.get(req_id_cand), None

        # Otherwise find first non-done request
        active = [r for r in requests.values() if r.get("state") != "done"]
        if active:
            return active[0], None

        # All requests are done
        return None, "All requests in ledger are in terminal 'done' state."

    def evaluate_step(self, request_id: Optional[str] = None) -> CoordinatorPacket:
        """
        Execute a single bounded coordinator evaluation step and return a compact next-work packet.
        """
        now_utc = get_iso_timestamp()

        # Step 1: Sync decisions if configured
        sync_att, sync_ok, sync_msg, _ = self.sync_decisions_if_configured()

        # Step 2: Query pending decisions
        pending_decs = self.decision_mgr.list_decisions(status="pending")
        pending_count = len(pending_decs)

        # Step 3: Select target request
        target_req, err_msg = self.select_target_request(request_id)

        if not target_req:
            # Entire ledger completed or empty
            return self._finalize_packet(CoordinatorPacket(
                schema_version="1.0",
                generated_at_utc=now_utc,
                status="done",
                status_reason=err_msg or "All work in ledger is completed.",
                next_action="No outstanding actions. Maintain monitoring or await new operator requests.",
                request=None,
                decision_status=DecisionStatus(
                    sync_attempted=sync_att,
                    sync_success=sync_ok,
                    sync_message=sync_msg,
                    pending_count=pending_count,
                    blocking_this_request=False,
                    blocking_decision_ids=[],
                ),
                preflight=PreflightStatus(
                    evaluated=False,
                    passed=True,
                    status="not_applicable",
                    required_probes=[],
                    blockers=[],
                ),
                routing=RoutingStatus(
                    evaluated=False,
                    rationale="No active request to route.",
                ),
                evidence_packet=None,
            ))

        # Extract request details
        req_id = target_req.get("id", "")
        req_state = target_req.get("state", "pending")
        req_type = target_req.get("task_type", "deployable")
        req_owner = target_req.get("owner", "unassigned")
        req_prompt = target_req.get("prompt", "")
        req_head = target_req.get("head")
        gh_info = target_req.get("github", {})
        req_issue = target_req.get("issue_number") or gh_info.get("issue_number")
        req_issue_url = target_req.get("issue_url") or gh_info.get("issue_url")
        req_next_action = target_req.get("next_action") or ""
        req_labels = target_req.get("labels", [])
        if not req_labels:
            req_labels = target_req.get("superboard", {}).get("labels", [])
        if isinstance(req_labels, str):
            req_labels = [l.strip() for l in req_labels.split(",") if l.strip()]

        criteria = target_req.get("acceptance_criteria", [])
        pending_crit = [
            c.get("id", "")
            for c in criteria
            if c.get("status") != "verified"
        ]

        req_summary = RequestSummary(
            id=req_id,
            state=req_state,
            task_type=req_type,
            owner=req_owner,
            prompt=req_prompt,
            head=req_head,
            issue_number=req_issue,
            issue_url=req_issue_url,
            next_action=req_next_action,
            pending_criteria=pending_crit,
            labels=req_labels,
        )

        # Step 4: Check Decision Blockers
        blocking_dec_ids = target_req.get("decision_blockers", [])
        # Also check if any pending decision blocks this request
        for d in pending_decs:
            if req_id in d.get("blocking_dependencies", []) and d["decision_id"] not in blocking_dec_ids:
                blocking_dec_ids.append(d["decision_id"])

        if blocking_dec_ids:
            # Request is BLOCKED on human decision
            primary_dec = self.decision_mgr.get_decision(blocking_dec_ids[0])
            dec_details = {
                "decision_id": primary_dec.get("decision_id"),
                "question": primary_dec.get("question"),
                "options": primary_dec.get("options"),
                "recommendation": primary_dec.get("recommendation"),
                "authorized_responders": primary_dec.get("authorized_responders"),
                "issue_url": primary_dec.get("issue_url"),
            } if primary_dec else None

            responder_str = ", ".join(primary_dec.get("authorized_responders", ["operator"])) if primary_dec else "operator"
            issue_str = f"issue #{primary_dec.get('issue_number')}" if primary_dec and primary_dec.get("issue_number") else "GitHub"

            return self._finalize_packet(CoordinatorPacket(
                schema_version="1.0",
                generated_at_utc=now_utc,
                status="wait",
                status_reason=f"Request '{req_id}' is blocked awaiting human decision on {', '.join(blocking_dec_ids)}.",
                next_action=(
                    f"Await human decision for {blocking_dec_ids[0]} on {issue_str} from authorized responder (@{responder_str}). "
                    f"Background workers must park and refrain from speculative divergence."
                ),
                request=req_summary,
                decision_status=DecisionStatus(
                    sync_attempted=sync_att,
                    sync_success=sync_ok,
                    sync_message=sync_msg,
                    pending_count=pending_count,
                    blocking_this_request=True,
                    blocking_decision_ids=blocking_dec_ids,
                    decision_details=dec_details,
                ),
                preflight=PreflightStatus(
                    evaluated=False,
                    passed=False,
                    status="blocked_by_decision",
                    required_probes=[],
                    blockers=[f"Blocked by decision: {d_id}" for d_id in blocking_dec_ids],
                ),
                routing=RoutingStatus(
                    evaluated=False,
                    rationale="Execution paused pending human decision.",
                ),
                evidence_packet=None,
            ))

        # Step 5: Check Authorization State (No Auto-Merge)
        if req_state == "awaiting authorization":
            auth = target_req.get("authorization", {})
            if auth.get("status") != "authorized":
                return self._finalize_packet(CoordinatorPacket(
                    schema_version="1.0",
                    generated_at_utc=now_utc,
                    status="wait",
                    status_reason=f"Request '{req_id}' is in 'awaiting authorization' state. Invariant forbids automated integration.",
                    next_action=(
                        f"Await explicit human operator authorization before advancing '{req_id}' to integration. "
                        f"Run 'python ledger.py update {req_id} --authorize --authorized-by <operator>' once granted."
                    ),
                    request=req_summary,
                    decision_status=DecisionStatus(
                        sync_attempted=sync_att,
                        sync_success=sync_ok,
                        sync_message=sync_msg,
                        pending_count=pending_count,
                        blocking_this_request=False,
                        blocking_decision_ids=[],
                    ),
                    preflight=PreflightStatus(
                        evaluated=False,
                        passed=False,
                        status="awaiting_authorization",
                        required_probes=[],
                        blockers=["Awaiting human authorization before integration"],
                    ),
                    routing=RoutingStatus(
                        evaluated=False,
                        rationale="Awaiting human authorization.",
                    ),
                    evidence_packet=None,
                ))

        # Step 6: Check Unfinished Dependencies
        all_reqs = self.ledger._load_data_unlocked().get("requests", {})
        unresolved_deps = []
        for dep_id in target_req.get("dependencies", []):
            dep = all_reqs.get(dep_id)
            if not dep or dep.get("state") != "done":
                unresolved_deps.append(dep_id)

        if unresolved_deps:
            return self._finalize_packet(CoordinatorPacket(
                schema_version="1.0",
                generated_at_utc=now_utc,
                status="block",
                status_reason=f"Request '{req_id}' has unfinished dependencies: {', '.join(unresolved_deps)}.",
                next_action=f"Resolve dependency request(s) ({', '.join(unresolved_deps)}) before advancing '{req_id}'.",
                request=req_summary,
                decision_status=DecisionStatus(
                    sync_attempted=sync_att,
                    sync_success=sync_ok,
                    sync_message=sync_msg,
                    pending_count=pending_count,
                    blocking_this_request=False,
                    blocking_decision_ids=[],
                ),
                preflight=PreflightStatus(
                    evaluated=False,
                    passed=False,
                    status="blocked_by_dependency",
                    required_probes=[],
                    blockers=[f"Unfinished dependency: {dep}" for dep in unresolved_deps],
                ),
                routing=RoutingStatus(
                    evaluated=False,
                    rationale="Dependencies unfinished.",
                ),
                evidence_packet=None,
            ))

        # Step 7: Preflight Gate Evaluation
        # Determine areas for preflight check
        areas = []
        is_exempt = (
            req_type in ["local_doc", "local", "doc", "harness", "workflow", "analysis"]
            or any(l in ["local_doc", "harness", "workflow", "docs", "area:harness"] for l in req_labels)
            or any(k in req_prompt.lower() for k in ["local_doc", "documentation", "prompt recurrence", "harness policy"])
        )
        if is_exempt:
            areas.append("local_doc")
        else:
            # Check keywords in labels and prompt
            combined_text = (req_prompt + " " + " ".join(req_labels)).lower()
            if any(k in combined_text for k in ["ui", "runtime", "frontend", "api", "incident"]):
                areas.append("runtime")
            if any(k in combined_text for k in ["db", "database", "migration", "schema", "sql"]):
                areas.append("db")
            if any(k in combined_text for k in ["stripe", "billing", "payment", "subscription"]):
                areas.append("billing")
            if not areas:
                areas.append("runtime")  # Default safe staging check for deployable tasks
        preflight_res: PreflightResult = self.preflight_engine.check_task({
            "areas": areas,
            "task_type": req_type,
            "head": req_head,
            "issue": req_issue,
        })

        if not preflight_res.passed:
            # Preflight gate failed
            return self._finalize_packet(CoordinatorPacket(
                schema_version="1.0",
                generated_at_utc=now_utc,
                status="block",
                status_reason=(
                    f"Preflight check failed for request '{req_id}' in area(s) {areas}: "
                    f"missing or invalid staging evidence for {', '.join(preflight_res.required_probes)}."
                ),
                next_action=(
                    f"Execute safe read-only preflight probe: "
                    f"'python preflight.py probe --all' and record evidence before proceeding."
                ),
                request=req_summary,
                decision_status=DecisionStatus(
                    sync_attempted=sync_att,
                    sync_success=sync_ok,
                    sync_message=sync_msg,
                    pending_count=pending_count,
                    blocking_this_request=False,
                    blocking_decision_ids=[],
                ),
                preflight=PreflightStatus(
                    evaluated=True,
                    passed=False,
                    status=preflight_res.status,
                    required_probes=preflight_res.required_probes,
                    blockers=preflight_res.blockers,
                    probe_details={
                        s: preflight_res.service_statuses.get(s, "unknown")
                        for s in preflight_res.required_probes
                    },
                ),
                routing=RoutingStatus(
                    evaluated=False,
                    rationale="Preflight gate not passed.",
                ),
                evidence_packet=None,
            ))

        preflight_info = PreflightStatus(
            evaluated=True,
            passed=True,
            status=preflight_res.status,
            required_probes=preflight_res.required_probes,
            blockers=[],
            probe_details=preflight_res.service_statuses,
        )

        # Step 8: Model Routing & Quota Context
        task_type = TaskType.ROUTINE_EXECUTION
        if req_state in ["QA", "review"]:
            task_type = TaskType.STRONG_REVIEW
        elif any(k in req_prompt.lower() for k in ["invariant", "architecture", "concurrency"]):
            task_type = TaskType.DEEP_REASONING

        risk_level = RiskLevel.MEDIUM
        if req_type == "local_doc" or "docs" in req_labels:
            risk_level = RiskLevel.LOW
        elif any(k in req_prompt.lower() for k in ["security", "financial", "invariant", "critical"]):
            risk_level = RiskLevel.HIGH

        adapter = self.resolve_balance_adapter()
        selector = ResetAwareModelSelector(adapter)
        rec = selector.select_model(
            task_type=task_type,
            risk_level=risk_level,
            allow_codex_promotion=True,
        )

        # Generate evidence packet if needed
        evidence_packet = None
        if rec.evidence_packet_required or req_state in ["QA", "review"]:
            evidence_packet = {
                "request_id": req_id,
                "head": req_head or "HEAD",
                "state": req_state,
                "criteria": criteria,
                "verified_count": len([c for c in criteria if c.get("status") == "verified"]),
                "total_criteria": len(criteria),
                "required_review_depth": "deep" if risk_level == RiskLevel.HIGH else "standard",
            }

        routing_info = RoutingStatus(
            evaluated=True,
            recommended_model=rec.selected_model,
            task_type=task_type.value,
            risk_level=risk_level.value,
            recommended_role=model_to_agent_role(rec.selected_model, task_type, risk_level),
            fallback_model=rec.fallback_model,
            promotion_applied=rec.promotion_applied,
            cooldown_fallback=rec.cooldown_fallback,
            rationale=rec.reasoning,
            quota_context={
                "provider_statuses": rec.provider_statuses,
            },
            evidence_packet_required=rec.evidence_packet_required,
        )

        # Status is READY for execution
        action_desc = req_next_action or f"Execute {req_state} steps for request '{req_id}'"
        next_action_str = (
            f"Dispatch worker lane using model '{rec.selected_model}' "
            f"(role: {routing_info.recommended_role}) to perform: {action_desc}."
        )

        return self._finalize_packet(CoordinatorPacket(
            schema_version="1.0",
            generated_at_utc=now_utc,
            status="ready",
            status_reason=(
                f"Request '{req_id}' is eligible for execution in state '{req_state}'. "
                f"Preflight passed ({preflight_res.status}). Model selected: {rec.selected_model}."
            ),
            next_action=next_action_str,
            request=req_summary,
            decision_status=DecisionStatus(
                sync_attempted=sync_att,
                sync_success=sync_ok,
                sync_message=sync_msg,
                pending_count=pending_count,
                blocking_this_request=False,
                blocking_decision_ids=[],
            ),
            preflight=preflight_info,
            routing=routing_info,
            evidence_packet=evidence_packet,
        ))

    def run_diagnostics(self) -> Any:
        """Run comprehensive aggregate system, service, provider and request diagnostics."""
        try:
            from diagnostics import DiagnosticCollector
            collector = DiagnosticCollector(
                state_dir=self.state_dir,
                ledger_path=self.ledger_path,
                decisions_path=self.decisions_path,
                evidence_dir=self.evidence_dir,
                balance_file=self.balance_file,
                repo=self.repo,
            )
            return collector.run_diagnostics()
        except ImportError as e:
            raise ImportError(f"Failed to import diagnostics module: {e}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Portable Multi-Agent Workflow Coordinator Entrypoint"
    )
    parser.add_argument("--state-dir", default=None, help="Root directory for state files")
    parser.add_argument("--ledger", default=None, help="Path to ledger JSON file")
    parser.add_argument("--decisions", default=None, help="Path to decisions JSON file")
    parser.add_argument("--evidence-dir", default=None, help="Path to preflight evidence directory")
    parser.add_argument(
        "--usage-adapter",
        choices=["auto", "file", "veyyon", "direct"],
        default="auto",
        help="Usage metrics adapter type (default: auto)",
    )
    parser.add_argument("--balance-file", default=None, help="Path to balance / usage JSON fixture")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"GitHub repo (default: {DEFAULT_REPO})")
    parser.add_argument("--project-config", default=None, help="Path to project adapter JSON config or Superboard config")
    parser.add_argument("--adapter", default=None, help="Name of project adapter (polysimulator, generic, etc.)")
    parser.add_argument(
        "--no-sync-decisions",
        action="store_true",
        help="Skip remote GitHub decision synchronization",
    )
    parser.add_argument("--request-id", default=None, help="Target specific request ID to evaluate")
    parser.add_argument(
        "--notify-telegram",
        action="store_true",
        help="Enable optional Telegram notification for meaningful transitions",
    )
    parser.add_argument("--telegram-project", default=None, help="Project/repo for Telegram slot routing")
    parser.add_argument(
        "--telegram-dry-run",
        action="store_true",
        default=True,
        help="Dry-run Telegram notification (format and check dedup without live send)",
    )
    parser.add_argument(
        "--telegram-send",
        action="store_true",
        help="Execute live network delivery to Telegram (requires explicit opt-in)",
    )
    parser.add_argument(
        "--telegram-pool-db",
        default=None,
        help=(
            "Path to the shared bot_pool.db holding the outbound message correlation index. "
            "Defaults to VEYYON_POOL_DB, else the installed pool at ~/.veyyon/telegram/bot_pool.db "
            "when it exists; set VEYYON_POOL_DB=off to record nothing"
        ),
    )
    parser.add_argument(
        "--dispatch",
        action="store_true",
        help="Dispatch eligible step via SuperboardExecutionAdapter (executes worker and advances ledger)",
    )
    parser.add_argument(
        "--fake-executor",
        action="store_true",
        default=True,
        help="Use fake executor with labeled fixture output for bounded integration testing",
    )
    parser.add_argument(
        "--real-worker",
        action="store_true",
        help="Execute safe harmless real worker command (config validation or git status)",
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Run aggregate system, service, provider, request and host diagnostics",
    )
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON packet")
    parser.add_argument("--summary", action="store_true", help="Output concise terminal summary")
    return parser


def format_packet_summary(packet: CoordinatorPacket) -> str:
    """Format human-readable summary of coordinator output."""
    lines = [
        "=" * 70,
        "PORTABLE WORKFLOW COORDINATOR - BOUNDED STEP EVALUATION",
        "=" * 70,
        f"Generated UTC:   {packet.generated_at_utc}",
        f"Status:          {packet.status.upper()}",
        f"Reason:          {packet.status_reason}",
        f"Next Action:     {packet.next_action}",
        "-" * 70,
    ]

    if packet.request:
        r = packet.request
        lines.extend([
            f"Target Request:  {r.id} [{r.state}] (type: {r.task_type})",
            f"Owner:           {r.owner}",
            f"Prompt:          {r.prompt[:60]}..." if len(r.prompt) > 60 else f"Prompt:          {r.prompt}",
            f"GitHub Issue:    #{r.issue_number}" if r.issue_number else "GitHub Issue:    None",
            f"Head SHA:        {r.head or 'unbound'}",
            f"Pending Criteria: {', '.join(r.pending_criteria) or 'None'}",
            "-" * 70,
        ])

    dec = packet.decision_status
    lines.extend([
        f"Decision Sync:   {'Executed' if dec.sync_attempted else 'Skipped'} ({dec.sync_message})",
        f"Pending Decs:    {dec.pending_count} pending in registry",
        f"Decision Block:  {'YES (' + ', '.join(dec.blocking_decision_ids) + ')' if dec.blocking_this_request else 'NO'}",
        "-" * 70,
    ])

    pf = packet.preflight
    lines.extend([
        f"Preflight Gate:  {'PASSED' if pf.passed else 'BLOCKED'} (status: {pf.status})",
        f"Required Probes: {', '.join(pf.required_probes) or 'None'}",
    ])
    if pf.blockers:
        lines.append(f"PF Blockers:     {'; '.join(pf.blockers)}")
    lines.append("-" * 70)

    rt = packet.routing
    if rt.evaluated:
        lines.extend([
            f"Selected Model:  {rt.recommended_model} (role: {rt.recommended_role})",
            f"Fallback Model:  {rt.fallback_model}",
            f"Promotion:       {'YES (Codex surplus promoted)' if rt.promotion_applied else 'NO'}",
            f"Routing Reason:  {rt.rationale}",
        ])
    else:
        lines.append(f"Model Routing:   Not evaluated ({rt.rationale})")

    lines.extend([
        "=" * 70,
        "BOUNDARIES & SAFETY INVARIANTS:",
        f"  Auto-Merge Allowed:      {packet.boundaries.auto_merge_allowed} (prohibited)",
        f"  Auto-Deploy Allowed:     {packet.boundaries.auto_deploy_allowed} (prohibited)",
        f"  Self-Spawn Loop:         {packet.boundaries.self_spawn_loop} (single-step bounded)",
        f"  Execution Dispatched:    {packet.boundaries.execution_dispatched} (caller must invoke worker)",
        f"  Authoritative System:    {packet.boundaries.shared_authority}",
        "=" * 70,
    ])
    return "\n".join(lines)


def main():
    parser = build_parser()
    args = parser.parse_args()

    coordinator = Coordinator(
        state_dir=args.state_dir,
        ledger_path=args.ledger,
        decisions_path=args.decisions,
        evidence_dir=args.evidence_dir,
        usage_adapter=args.usage_adapter,
        repo=args.repo,
        sync_decisions=not args.no_sync_decisions,
        notify_telegram=args.notify_telegram,
        telegram_project=args.telegram_project,
        telegram_dry_run=args.telegram_dry_run,
        telegram_send=args.telegram_send,
        telegram_pool_db=args.telegram_pool_db,
        project_config=args.project_config,
        adapter_name=args.adapter,
    )

    if getattr(args, "diagnostics", False):
        try:
            from diagnostics import format_diagnostic_summary
            report = coordinator.run_diagnostics()
            if args.json or not args.summary:
                print(report.to_json())
            else:
                print(format_diagnostic_summary(report))
            return
        except Exception as e:
            sys.stderr.write(f"Diagnostics execution error: {e}\n")
            sys.exit(1)

    if args.dispatch:
        try:
            from superboard_adapter import SuperboardExecutionAdapter, format_adapter_summary
            adapter = SuperboardExecutionAdapter(
                coordinator=coordinator,
                state_dir=args.state_dir,
                config_path=args.project_config,
                fake_executor=args.fake_executor and not args.real_worker,
                notify_telegram=args.notify_telegram,
                telegram_project=args.telegram_project,
                telegram_dry_run=args.telegram_dry_run,
                telegram_send=args.telegram_send,
                telegram_pool_db=args.telegram_pool_db,
            )
            res = adapter.run_step(request_id=args.request_id, real_worker=args.real_worker)
            if args.json or not args.summary:
                print(res.to_json())
            else:
                print(format_adapter_summary(res))
            return
        except Exception as e:
            sys.stderr.write(f"Adapter execution error: {e}\n")
            sys.exit(1)

    try:
        packet = coordinator.evaluate_step(request_id=args.request_id)
    except Exception as e:
        sys.stderr.write(f"Coordinator execution error: {e}\n")
        sys.exit(1)
    if args.json or not args.summary:
        # Default to JSON output for programmatic consumers
        print(packet.to_json())
    else:
        print(format_packet_summary(packet))


if __name__ == "__main__":
    main()
