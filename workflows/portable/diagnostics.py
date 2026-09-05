#!/usr/bin/env python3
"""
diagnostics.py — Unified Portable System, Request & Service Diagnostics

Exposes exactly where problems lie, what is missing, and whether human input
or autonomous agent action is required.

Inviolable Invariants:
1. Unknown and stale evidence are NEVER green/healthy.
2. Explicitly distinguishes Access (endpoint reachable/authenticated) from
   Health (runtime service healthy, expected schema/revision running).
3. Explicitly distinguishes Stale (expired evidence, age > TTL) from Failed
   (active probe failure or error response).
4. Explicitly distinguishes Unknown Root Cause (failure without diagnosed source)
   from Proven Diagnosis (confirmed evidence-based cause).
5. Human input (human_input_needed=True) is exposed ONLY for true authorization
   (merge/deploy), architectural preferences/tradeoffs (DEC-*), or secure
   credential setup.
6. Missing code implementation, unwritten tests, and failing test suites remain
   strictly AGENT-OWNED actions, never punts to the operator.
7. Deduplicatable question identifier (question_id) for every human input item.
8. Never asks for secret values in chat/issues/diagnostics; provides secure
   configuration guidance only.
9. Prohibits production probes (zaraprptkegxqpvnsubu, vpyL-7TDEUREH6Uo_y1sb, sk_live_).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import datetime
import json
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# Sibling workflow modules
try:
    from ledger import RequestLedger, VALID_STATES
except ImportError as e:
    raise ImportError(f"diagnostics requires sibling ledger module: {e}")

try:
    from preflight import (
        ALL_KNOWN_SERVICES,
        DEFAULT_EVIDENCE_TTL_SECONDS,
        PreflightEngine,
        ServiceEvidence,
        parse_iso_timestamp,
        get_iso_timestamp,
        PRODUCTION_DOKPLOY_COMPOSE_ID,
        PRODUCTION_SUPABASE_PROJECT_REF,
        STRIPE_LIVE_PREFIXES,
    )
except ImportError as e:
    raise ImportError(f"diagnostics requires sibling preflight module: {e}")

try:
    from decision_workflow import DecisionManager, DecisionContract
except ImportError as e:
    raise ImportError(f"diagnostics requires sibling decision_workflow module: {e}")

try:
    from balance_loader import get_balance_adapter, FileBalanceAdapter
except ImportError as e:
    get_balance_adapter = None
    FileBalanceAdapter = None

try:
    from project_adapter import get_current_project_config
except ImportError:
    get_current_project_config = None


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class ServiceDiagnostic:
    """Diagnostic assessment of a configured external service or infrastructure."""
    service: str
    system_type: str  # "runtime", "database", "billing"
    configured: bool
    target_identity: Dict[str, Any]
    observed_source: str
    observed_time_utc: Optional[str]
    evidence_age_seconds: Optional[float]
    ttl_seconds: int
    is_stale: bool
    live_verified: bool
    access_status: str  # "granted", "blocked", "unconfigured", "unknown"
    health_status: str  # "healthy", "unhealthy", "unverified", "stale", "unknown"
    state: str  # "healthy", "stale", "blocked", "failed", "unverified", "missing"
    diagnosis_type: str  # "confirmed_diagnosis" vs "unknown_cause"
    confirmed_or_unknown_cause: str
    missing_prerequisites: List[str]
    action_owner: str  # "agent_action" vs "human_input"
    human_input_needed: bool
    question_id: Optional[str]
    question_text: Optional[str]
    resolution_guidance: Optional[str]
    next_action: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderDiagnostic:
    """Diagnostic assessment of a model/quota provider."""
    provider: str
    status: str  # "ok", "cooldown", "exhausted", "dormant", "stale", "unknown"
    usage_percent: Optional[float]
    window_id: Optional[str]
    reset_time_utc: Optional[str]
    cooldown_active: bool
    observed_source: str
    observed_time_utc: Optional[str]
    is_stale: bool
    state: str
    diagnosis_type: str
    confirmed_or_unknown_cause: str
    missing_prerequisites: List[str]
    action_owner: str
    human_input_needed: bool
    question_id: Optional[str]
    next_action: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DecisionDiagnostic:
    """Diagnostic assessment of an asynchronous human decision item."""
    decision_id: str
    request_id: Optional[str]
    status: str  # "pending", "resolved", "rejected"
    decision_scope: str
    question: str
    options: List[Dict[str, Any]]
    recommendation: Optional[str]
    authorized_responders: List[str]
    issue_url: Optional[str]
    rejection_reason: Optional[str]
    observed_source: str
    observed_time_utc: Optional[str]
    state: str  # "pending_human_response", "rejected_safety", "resolved"
    diagnosis_type: str
    confirmed_or_unknown_cause: str
    missing_prerequisites: List[str]
    action_owner: str
    human_input_needed: bool
    question_id: Optional[str]
    question_text: Optional[str]
    next_action: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RequestDiagnostic:
    """Diagnostic assessment of a registered request in the ledger."""
    request_id: str
    state: str
    task_type: str
    owner: str
    head: Optional[str]
    criteria_summary: Dict[str, int]
    pending_criteria: List[str]
    dependencies: List[str]
    blockers: List[str]
    decision_blockers: List[str]
    observed_source: str
    observed_time_utc: Optional[str]
    diagnosis_type: str
    confirmed_or_unknown_cause: str
    missing_prerequisites: List[str]
    action_owner: str
    human_input_needed: bool
    question_id: Optional[str]
    question_text: Optional[str]
    next_action: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class WorkerBoardDiagnostic:
    """Diagnostic assessment of worker execution backends and project board."""
    project_repo: str
    project_number: int
    worker_backend_configured: str
    available_executables: Dict[str, bool]
    state: str
    observed_source: str
    diagnosis_type: str
    confirmed_or_unknown_cause: str
    missing_prerequisites: List[str]
    action_owner: str
    human_input_needed: bool
    question_id: Optional[str]
    next_action: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HumanInputItem:
    """An exact, deduplicatable question required from the human operator."""
    question_id: str  # Unique deduplicatable key (e.g. 'credential:stripe_test:sk_test_key')
    category: str  # "authorization", "preference", "credential"
    target: str
    question: str
    resolution_guidance: str
    actionable_command: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class AgentActionItem:
    """An autonomous next step owned and executable by an agent worker."""
    target: str
    category: str  # "implementation", "qa_verification", "review", "preflight_probe", "decision_refactor"
    missing_prerequisites: List[str]
    next_action: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

@dataclass
class HostResourceDiagnostic:
    """Diagnostic assessment of host machine RAM and resources (no auto-kill)."""
    telemetry_available: bool
    platform: str
    ram_used_percent: Optional[float]
    ram_total_gb: Optional[float]
    ram_available_gb: Optional[float]
    state: str  # "ok", "elevated", "critical", "unknown"
    diagnosis_type: str
    confirmed_or_unknown_cause: str
    missing_prerequisites: List[str]
    action_owner: str
    human_input_needed: bool
    question_id: Optional[str]
    next_action: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DiagnosticReport:
    """Top-level aggregate diagnostic report."""
    schema_version: str
    generated_at_utc: str
    aggregate_status: str  # "healthy", "actionable", "blocked", "awaiting_human"
    summary_reason: str
    services: Dict[str, ServiceDiagnostic]
    providers: Dict[str, ProviderDiagnostic]
    requests: Dict[str, RequestDiagnostic]
    decisions: Dict[str, DecisionDiagnostic]
    worker_and_board: WorkerBoardDiagnostic
    host_resources: HostResourceDiagnostic
    human_inputs: List[HumanInputItem]
    agent_actions: List[AgentActionItem]
    boundaries: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# Diagnostics Collector Engine
# ---------------------------------------------------------------------------

class DiagnosticCollector:
    """
    Evaluates current real state across ledger, preflight, decisions,
    billing/quotas, and execution backends without modifying anything.
    """

    def __init__(
        self,
        state_dir: Optional[str] = None,
        ledger_path: Optional[str] = None,
        decisions_path: Optional[str] = None,
        evidence_dir: Optional[str] = None,
        balance_file: Optional[str] = None,
        repo: Optional[str] = None,
        now: Optional[datetime.datetime] = None,
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
        self.balance_file = os.path.abspath(balance_file) if balance_file else None
        self.repo = repo
        self._now_override = now

        # Engines
        self.ledger = RequestLedger(ledger_path=self.ledger_path)
        self.decision_mgr = DecisionManager(
            decisions_path=self.decisions_path,
            ledger_path=self.ledger_path,
        )
        self.project_config = get_current_project_config() if get_current_project_config else None
        self.preflight_engine = PreflightEngine(
            evidence_dir=self.evidence_dir,
            project_config=self.project_config,
        )

    def _get_now(self) -> datetime.datetime:
        if self._now_override is not None:
            return self._now_override
        return datetime.datetime.now(datetime.timezone.utc)

    # -----------------------------------------------------------------------
    # 1. Services & Integration Diagnostics
    # -----------------------------------------------------------------------

    def diagnose_services(self) -> Dict[str, ServiceDiagnostic]:
        """
        Assess configured service evidence (runtime, database, billing).
        Strictly distinguishes:
        - Access vs Health: cached credential is NOT live health.
        - Stale vs Failed: age > TTL is stale, not necessarily failed, but NEVER green.
        - Unknown cause vs Proven diagnosis.
        """
        now = self._get_now()
        services: Dict[str, ServiceDiagnostic] = {}

        # System type map
        system_types = {
            "dokploy_staging": "runtime",
            "supabase_staging": "database",
            "stripe_test": "billing",
        }

        for s_name in ALL_KNOWN_SERVICES:
            sys_type = system_types.get(s_name, "infrastructure")
            ev: Optional[ServiceEvidence] = self.preflight_engine.load_evidence(s_name)

            if ev is None:
                # Missing evidence
                services[s_name] = ServiceDiagnostic(
                    service=s_name,
                    system_type=sys_type,
                    configured=False,
                    target_identity={},
                    observed_source="preflight_evidence_directory",
                    observed_time_utc=None,
                    evidence_age_seconds=None,
                    ttl_seconds=DEFAULT_EVIDENCE_TTL_SECONDS,
                    is_stale=False,
                    live_verified=False,
                    access_status="unknown",
                    health_status="unknown",
                    state="missing",
                    diagnosis_type="confirmed_diagnosis",
                    confirmed_or_unknown_cause=f"No preflight evidence file recorded for service '{s_name}'.",
                    missing_prerequisites=[f"preflight_probe:{s_name}"],
                    action_owner="agent_action",
                    human_input_needed=False,
                    question_id=None,
                    question_text=None,
                    resolution_guidance=None,
                    next_action=f"Execute safe read-only preflight probe: python preflight.py probe --service {s_name}",
                )
                continue

            # Evidence file exists on disk
            source_path = self.preflight_engine.get_evidence_path(s_name)
            evidence_file_label = os.path.relpath(source_path, self.state_dir) if self.state_dir else source_path

            # Guard against production references in evidence
            if (
                ev.target_identity.get("compose_id") == PRODUCTION_DOKPLOY_COMPOSE_ID
                or ev.target_identity.get("project_ref") == PRODUCTION_SUPABASE_PROJECT_REF
                or ev.environment == "production"
                or any(str(ev.target_identity.get(k, "")).startswith(p) for p in STRIPE_LIVE_PREFIXES for k in ["key_prefix", "key"])
            ):
                services[s_name] = ServiceDiagnostic(
                    service=s_name,
                    system_type=sys_type,
                    configured=True,
                    target_identity=ev.target_identity,
                    observed_source=evidence_file_label,
                    observed_time_utc=ev.timestamp,
                    evidence_age_seconds=0.0,
                    ttl_seconds=ev.ttl_seconds,
                    is_stale=False,
                    live_verified=False,
                    access_status="blocked",
                    health_status="unhealthy",
                    state="blocked",
                    diagnosis_type="confirmed_diagnosis",
                    confirmed_or_unknown_cause="FATAL: Production identity detected in evidence. Production access strictly prohibited.",
                    missing_prerequisites=["staging_isolation_enforcement"],
                    action_owner="agent_action",
                    human_input_needed=False,
                    question_id=None,
                    question_text=None,
                    resolution_guidance=None,
                    next_action="Re-target probe strictly to staging resources.",
                )
                continue

            # Calculate evidence age and stale status
            ev_dt = parse_iso_timestamp(ev.timestamp)
            age_sec = (now - ev_dt).total_seconds() if ev_dt else None
            is_stale = (age_sec is not None and age_sec > ev.ttl_seconds)

            # Access status
            access_status = "granted" if ev.access_status == "success" else (
                "blocked" if ev.access_status == "blocked" else (
                    "unconfigured" if ev.access_status == "not_applicable" else "unknown"
                )
            )

            # Health status (Access != Health, and cached != live verified)
            live_verified = False  # Evidence read from cached file is not live verified at current moment
            health_status: str

            if is_stale:
                health_status = "stale"
            elif ev.access_status == "blocked":
                health_status = "unverified"
            else:
                # Access is success; check baseline and logs to determine health
                if s_name == "dokploy_staging":
                    logs = ev.bounded_utc_logs or {}
                    if logs.get("log_query_failed"):
                        health_status = "unhealthy"
                    elif ev.runtime_revision:
                        health_status = "healthy"
                    else:
                        health_status = "unverified"
                elif s_name == "supabase_staging":
                    base = ev.baseline_behavior or {}
                    if base.get("read_only_query_verified"):
                        health_status = "healthy"
                    else:
                        health_status = "unverified"
                elif s_name == "stripe_test":
                    base = ev.baseline_behavior or {}
                    if base.get("test_mode_verified") and base.get("get_endpoint_verified"):
                        health_status = "healthy"
                    else:
                        health_status = "unverified"
                else:
                    health_status = "unverified"

            # Determine composite state (UNKNOWN/STALE NEVER GREEN)
            state: str
            if is_stale:
                state = "stale"
            elif ev.access_status == "blocked":
                state = "blocked"
            elif health_status == "healthy" and access_status == "granted" and not is_stale:
                state = "healthy"
            elif health_status == "unhealthy":
                state = "failed"
            else:
                state = "unverified"

            # Diagnosis and confirmed cause
            diagnosis_type = "confirmed_diagnosis"
            confirmed_cause: str

            if ev.blocker_reason:
                confirmed_cause = ev.blocker_reason
            elif is_stale:
                confirmed_cause = (
                    f"Evidence expired: age {int(age_sec or 0)}s exceeds TTL {ev.ttl_seconds}s. "
                    "Historical cached attestation cannot guarantee current live health."
                )
            elif ev.access_status == "success":
                confirmed_cause = "Attestation recorded and verified within valid TTL window."
            else:
                diagnosis_type = "unknown_cause"
                confirmed_cause = "Probe recorded non-success status without explicit diagnostic failure code."

            # Missing prerequisites & action ownership
            missing_prereqs: List[str] = []
            human_needed = False
            q_id: Optional[str] = None
            q_text: Optional[str] = None
            guidance: Optional[str] = None
            next_act: str

            if is_stale:
                missing_prereqs.append(f"fresh_preflight_probe:{s_name}")

            # Specific service diagnostics
            if s_name == "stripe_test" and ev.access_status == "blocked":
                missing_prereqs.append("stripe_test_credentials:sk_test_")
                # True credential setup requires human operator setup
                human_needed = True
                q_id = "credential:stripe_test:sk_test_key"
                q_text = (
                    "Stripe test-mode API credentials (sk_test_...) are not configured in the workstation environment or vault. "
                    "Central secrets manager holds only production live key (sk_live_...) which is prohibited. "
                    "Please configure a test key in your environment or staging vault (do not post secret values in chat or issues)."
                )
                guidance = (
                    "Export STRIPE_SECRET_KEY=sk_test_... in your workstation environment or configure test credentials in the staging secrets vault. "
                    "Autonomous agents are strictly forbidden from generating fake live keys or accessing production keys."
                )
                next_act = (
                    "Operator configure test key (sk_test_...) in environment/vault; "
                    "agents remain parked on billing preflight until configured."
                )
                action_owner = "human_input"

            elif is_stale:
                action_owner = "agent_action"
                next_act = f"Run 'python preflight.py probe --service {s_name}' to refresh evidence."

            elif ev.access_status == "blocked":
                action_owner = "agent_action"
                missing_prereqs.append(f"unblock_service_access:{s_name}")
                next_act = f"Investigate access blocker for {s_name}: {confirmed_cause[:80]}"

            else:
                action_owner = "agent_action"
                next_act = f"Service {s_name} ready and active."

            services[s_name] = ServiceDiagnostic(
                service=s_name,
                system_type=sys_type,
                configured=True,
                target_identity=ev.target_identity,
                observed_source=evidence_file_label,
                observed_time_utc=ev.timestamp,
                evidence_age_seconds=round(age_sec, 1) if age_sec is not None else None,
                ttl_seconds=ev.ttl_seconds,
                is_stale=is_stale,
                live_verified=live_verified,
                access_status=access_status,
                health_status=health_status,
                state=state,
                diagnosis_type=diagnosis_type,
                confirmed_or_unknown_cause=confirmed_cause,
                missing_prerequisites=missing_prereqs,
                action_owner=action_owner,
                human_input_needed=human_needed,
                question_id=q_id,
                question_text=q_text,
                resolution_guidance=guidance,
                next_action=next_act,
            )

        return services

    # -----------------------------------------------------------------------
    # 2. Providers & Quotas
    # -----------------------------------------------------------------------

    def diagnose_providers(self) -> Dict[str, ProviderDiagnostic]:
        """
        Assess model providers, quota windows, and reset timers.
        """
        providers: Dict[str, ProviderDiagnostic] = {}
        now = self._get_now()

        # Known providers
        known_providers = ["google-antigravity", "anthropic", "openai-codex", "xai-oauth"]

        snapshot_data = None
        source_label = "none"
        is_stale = False
        gen_time = None

        if FileBalanceAdapter:
            candidates = []
            if self.balance_file and os.path.exists(self.balance_file):
                candidates.append(self.balance_file)
            if self.state_dir:
                candidates.append(os.path.join(self.state_dir, "usage_snapshot_cache.json"))
                candidates.append(os.path.join(self.state_dir, "usage_fixture.json"))
            candidates.append(os.path.join(SCRIPT_DIR, "usage_snapshot_cache.json"))
            candidates.append(os.path.join(SCRIPT_DIR, "usage_fixture.json"))

            for p in candidates:
                if os.path.exists(p):
                    try:
                        adapter = FileBalanceAdapter(p)
                        snapshot_data = adapter.fetch_snapshot()
                        source_label = os.path.basename(p)
                        gen_time = snapshot_data.generated_at_utc
                        s_dt = parse_iso_timestamp(gen_time)
                        if s_dt and (now - s_dt).total_seconds() > 3600.0 * 24.0:
                            is_stale = True
                        break
                    except Exception:
                        continue

        if snapshot_data:
            if isinstance(snapshot_data.providers, dict):
                prov_map = snapshot_data.providers
            else:
                prov_map = {getattr(p, "provider", str(p)): p for p in snapshot_data.providers}
            for p_name in known_providers:
                p_obj = prov_map.get(p_name)
                if not p_obj:
                    # Provider unlisted
                    providers[p_name] = ProviderDiagnostic(
                        provider=p_name,
                        status="unknown",
                        usage_percent=None,
                        window_id=None,
                        reset_time_utc=None,
                        cooldown_active=False,
                        observed_source=source_label,
                        observed_time_utc=gen_time,
                        is_stale=is_stale,
                        state="unknown",
                        diagnosis_type="unknown_cause",
                        confirmed_or_unknown_cause=f"Provider '{p_name}' not listed in snapshot.",
                        missing_prerequisites=[],
                        action_owner="agent_action",
                        human_input_needed=False,
                        question_id=None,
                        next_action="Model router uses fallback available providers.",
                    )
                    continue

                status = getattr(p_obj, "status", "ok")
                rem_frac = getattr(p_obj, "effective_remaining_fraction", None)
                usage_pct = round((1.0 - rem_frac) * 100.0, 1) if rem_frac is not None else None
                win_id = getattr(p_obj, "bottleneck_window_id", None) or getattr(p_obj, "primary_window_id", None)
                reset_hours = getattr(p_obj, "cycle_hours_to_reset", None)
                reset_t = f"{reset_hours:.1f}h" if reset_hours is not None else None
                is_cooldown = (status == "cooldown")

                diag_type = "confirmed_diagnosis"
                cause = f"Status '{status}'; usage {usage_pct}%" if usage_pct is not None else f"Status '{status}'"
                if p_name == "xai-oauth":
                    cause = "Provider dormant: No active Grok subscription currently exists; workers routed away from Grok."

                providers[p_name] = ProviderDiagnostic(
                    provider=p_name,
                    status=status,
                    usage_percent=usage_pct,
                    window_id=win_id,
                    reset_time_utc=reset_t,
                    cooldown_active=is_cooldown,
                    observed_source=source_label,
                    observed_time_utc=gen_time,
                    is_stale=is_stale,
                    state="dormant" if status == "dormant" else ("cooldown" if is_cooldown else ("ok" if status == "ok" else "exhausted")),
                    diagnosis_type=diag_type,
                    confirmed_or_unknown_cause=cause,
                    missing_prerequisites=[],
                    action_owner="agent_action",
                    human_input_needed=False,
                    question_id=None,
                    next_action="Model router handles selection automatically.",
                )
        else:
            # No snapshot data available
            for p_name in known_providers:
                providers[p_name] = ProviderDiagnostic(
                    provider=p_name,
                    status="unconfigured" if p_name != "xai-oauth" else "dormant",
                    usage_percent=None,
                    window_id=None,
                    reset_time_utc=None,
                    cooldown_active=False,
                    observed_source="none",
                    observed_time_utc=None,
                    is_stale=False,
                    state="unknown" if p_name != "xai-oauth" else "dormant",
                    diagnosis_type="unknown_cause",
                    confirmed_or_unknown_cause="No usage snapshot or fixture loaded.",
                    missing_prerequisites=["usage_snapshot"],
                    action_owner="agent_action",
                    human_input_needed=False,
                    question_id=None,
                    next_action="Router uses minimal fallback profile.",
                )

        return providers

    # -----------------------------------------------------------------------
    # 3. Decisions Diagnostics
    # -----------------------------------------------------------------------

    def diagnose_decisions(self) -> Dict[str, DecisionDiagnostic]:
        """
        Assess human decisions from decisions.json.
        Distinguishes:
        - Pending human decision: human_input_needed=True, specific question_id.
        - Rejected decision: safety refusal, agent_action to reformulate or avoid.
        - Resolved decision: resolved.
        """
        decisions: Dict[str, DecisionDiagnostic] = {}
        try:
            data = self.decision_mgr._load_data_unlocked()
            raw_decs = data.get("decisions", {})
        except Exception:
            raw_decs = {}

        for d_id, d_data in raw_decs.items():
            status = d_data.get("status", "pending")
            q_text = d_data.get("question", "")
            scope = d_data.get("decision_scope", "architectural_preference")
            opts = d_data.get("options", [])
            rec = d_data.get("recommendation")
            responders = d_data.get("authorized_responders", ["operator"])
            url = d_data.get("issue_url")
            rej_reason = d_data.get("rejection_reason")
            req_id = d_data.get("request_id")
            upd_time = d_data.get("updated_at") or d_data.get("created_at")

            if status == "pending":
                state = "pending_human_response"
                diag_type = "confirmed_diagnosis"
                cause = f"Asynchronous decision pending human operator response on {d_id}."
                missing_prereqs = [f"human_decision:{d_id}"]
                action_owner = "human_input"
                human_needed = True
                question_id = f"decision:{d_id}"
                next_act = (
                    f"Authorized responder (@{', @'.join(responders)}) reply on issue or via "
                    f"'python decision_workflow.py reply {d_id} --answer <option>'"
                )
            elif status == "rejected":
                state = "rejected_safety"
                diag_type = "confirmed_diagnosis"
                cause = rej_reason or "Decision rejected by safety guardrails."
                missing_prereqs = []
                action_owner = "agent_action"
                human_needed = False
                question_id = None
                next_act = "Agent must not repeat rejected operation; follow established policy."
            else:
                state = "resolved"
                diag_type = "confirmed_diagnosis"
                cause = f"Decision resolved with answer: {d_data.get('answer')}."
                missing_prereqs = []
                action_owner = "agent_action"
                human_needed = False
                question_id = None
                next_act = "Decision incorporated into request workflow."

            decisions[d_id] = DecisionDiagnostic(
                decision_id=d_id,
                request_id=req_id,
                status=status,
                decision_scope=scope,
                question=q_text,
                options=opts,
                recommendation=rec,
                authorized_responders=responders,
                issue_url=url,
                rejection_reason=rej_reason,
                observed_source=os.path.basename(self.decisions_path),
                observed_time_utc=upd_time,
                state=state,
                diagnosis_type=diag_type,
                confirmed_or_unknown_cause=cause,
                missing_prerequisites=missing_prereqs,
                action_owner=action_owner,
                human_input_needed=human_needed,
                question_id=question_id,
                question_text=q_text if human_needed else None,
                next_action=next_act,
            )

        return decisions

    # -----------------------------------------------------------------------
    # 4. Registered Requests in Ledger
    # -----------------------------------------------------------------------

    def diagnose_requests(
        self,
        service_diags: Dict[str, ServiceDiagnostic],
        decision_diags: Dict[str, DecisionDiagnostic],
    ) -> Dict[str, RequestDiagnostic]:
        """
        Assess all registered requests in ledger.json.
        Enforces:
        - Missing implementation, unwritten tests, and failing tests are AGENT-OWNED.
        - Only true authorization (awaiting authorization) or blocking human decisions are human_input.
        """
        requests: Dict[str, RequestDiagnostic] = {}
        try:
            data = self.ledger._load_data_unlocked()
            raw_reqs = data.get("requests", {})
        except Exception:
            raw_reqs = {}

        for req_id, r_data in raw_reqs.items():
            state = r_data.get("state", "pending")
            task_type = r_data.get("task_type", "deployable")
            owner = r_data.get("owner", "unassigned")
            head = r_data.get("head")
            upd_time = r_data.get("updated_at") or r_data.get("created_at")

            criteria = r_data.get("acceptance_criteria", [])
            total_crit = len(criteria)
            verif_crit = len([c for c in criteria if c.get("status") == "verified"])
            pend_crit = [c.get("id", "") for c in criteria if c.get("status") != "verified"]
            crit_summary = {"total": total_crit, "verified": verif_crit, "pending": len(pend_crit)}

            deps = r_data.get("dependencies", [])
            blockers = []
            if r_data.get("blocker"):
                blockers.append(str(r_data.get("blocker")))

            dec_blockers = r_data.get("decision_blockers", [])
            for d_id, d in decision_diags.items():
                if d.status == "pending" and req_id in (d_id, d.request_id) and d_id not in dec_blockers:
                    dec_blockers.append(d_id)

            missing_prereqs: List[str] = []
            action_owner = "agent_action"
            human_needed = False
            q_id: Optional[str] = None
            q_text: Optional[str] = None
            diag_type = "confirmed_diagnosis"
            cause: str
            next_act: str

            # Evaluate state-specific diagnostics
            if state == "done":
                cause = "All acceptance criteria verified with GitHub proofs recorded on authoritative commit."
                next_act = "Request complete and closed."

            elif dec_blockers:
                cause = f"Blocked awaiting human decision on: {', '.join(dec_blockers)}."
                missing_prereqs.extend([f"decision_resolution:{d_id}" for d_id in dec_blockers])
                action_owner = "human_input"
                human_needed = True
                q_id = f"decision:{dec_blockers[0]}"
                d_obj = decision_diags.get(dec_blockers[0])
                q_text = d_obj.question if d_obj else f"Human decision required for {dec_blockers[0]}"
                next_act = f"Await human decision for {dec_blockers[0]} on GitHub issue."

            elif state == "awaiting authorization":
                auth = r_data.get("authorization", {})
                if auth.get("status") != "authorized":
                    cause = (
                        f"Implementation, QA, and review complete on head '{head}'. "
                        "Policy strictly prohibits autonomous integration without explicit human operator authorization."
                    )
                    missing_prereqs.append("operator_merge_authorization")
                    action_owner = "human_input"
                    human_needed = True
                    q_id = f"authorization:{req_id}:merge"
                    q_text = f"Authorize merging request '{req_id}' (verified on commit {head[:8] if head else 'unbound'}) into integration trunk (staging)?"
                    next_act = f"Operator authorize merge: python ledger.py update {req_id} --authorize --authorized-by <operator>"
                else:
                    cause = "Authorized by operator; awaiting integration merge."
                    missing_prereqs.append("git_merge_commit")
                    action_owner = "agent_action"
                    next_act = f"Execute git merge commit (--no-ff) for '{req_id}' into staging."

            elif deps:
                unres_deps = [d for d in deps if raw_reqs.get(d, {}).get("state") != "done"]
                if unres_deps:
                    cause = f"Blocked by unfinished upstream dependencies: {', '.join(unres_deps)}."
                    missing_prereqs.extend([f"upstream_dependency:{d}" for d in unres_deps])
                    action_owner = "agent_action"
                    next_act = f"Complete upstream dependency request(s) before advancing '{req_id}'."
                else:
                    cause = "Dependencies satisfied; proceeding with execution."
                    next_act = r_data.get("next_action") or f"Execute stage for {req_id}."

            elif state == "implementation":
                cause = f"Active implementation with {len(pend_crit)} unverified acceptance criteria."
                missing_prereqs.extend([f"criteria_verification:{c_id}" for c_id in pend_crit])
                action_owner = "agent_action"
                human_needed = False  # Implementation is AGENT-OWNED, not a punt
                next_act = r_data.get("next_action") or f"Implement and verify pending criteria on head '{head or 'unbound'}'."

            elif state == "QA":
                cause = f"Active QA behavioral and visual verification required on head '{head}'."
                missing_prereqs.append(f"exact_head_qa_evidence:{head}")
                action_owner = "agent_action"
                human_needed = False
                next_act = f"Execute exact-head QA run on head '{head}'."

            elif state == "review":
                cause = f"Adversarial independent code review required on head '{head}'."
                missing_prereqs.append(f"non_author_review_approval:{head}")
                action_owner = "agent_action"
                human_needed = False
                next_act = f"Perform non-author code review on head '{head}'."

            elif state in ("integration", "live verification"):
                cause = f"Post-merge verification on staging required."
                missing_prereqs.append("staging_smoke_verification")
                action_owner = "agent_action"
                next_act = f"Perform signed-in staging verification."

            else:
                cause = f"Request in state '{state}'."
                action_owner = "agent_action"
                next_act = r_data.get("next_action") or f"Advance request '{req_id}'."

            requests[req_id] = RequestDiagnostic(
                request_id=req_id,
                state=state,
                task_type=task_type,
                owner=owner,
                head=head,
                criteria_summary=crit_summary,
                pending_criteria=pend_crit,
                dependencies=deps,
                blockers=blockers,
                decision_blockers=dec_blockers,
                observed_source=os.path.basename(self.ledger_path),
                observed_time_utc=upd_time,
                diagnosis_type=diag_type,
                confirmed_or_unknown_cause=cause,
                missing_prerequisites=missing_prereqs,
                action_owner=action_owner,
                human_input_needed=human_needed,
                question_id=q_id,
                question_text=q_text,
                next_action=next_act,
            )

        return requests

    # -----------------------------------------------------------------------
    # 5. Worker & Board Diagnostics
    # -----------------------------------------------------------------------

    def diagnose_worker_and_board(self) -> WorkerBoardDiagnostic:
        """
        Assess worker backend CLI availability and Superboard project configuration.
        """
        execs = {
            "claude": shutil.which("claude") is not None,
            "codex": shutil.which("codex") is not None,
            "veyyon": shutil.which("veyyon") is not None,
            "gh": shutil.which("gh") is not None,
            "git": shutil.which("git") is not None,
        }

        repo = self.repo or (self.project_config.repo if self.project_config else "Bavariance/polysimulator")
        proj_num = self.project_config.project_number if self.project_config else 1

        missing: List[str] = []
        if not execs["git"]:
            missing.append("git_executable_missing")

        state = "ready" if execs["git"] else "degraded"
        diag_type = "confirmed_diagnosis"
        cause = f"Worker CLI tools checked: {', '.join(k + '=' + str(v) for k, v in execs.items())}."

        return WorkerBoardDiagnostic(
            project_repo=repo,
            project_number=proj_num,
            worker_backend_configured="WorkerBackend (argv[0] execution, shell=False)",
            available_executables=execs,
            state=state,
            observed_source="environment_path_inspection",
            diagnosis_type=diag_type,
            confirmed_or_unknown_cause=cause,
            missing_prerequisites=missing,
            action_owner="agent_action",
            human_input_needed=False,
            question_id=None,
            next_action="Worker backend dispatches via available host executables.",
        )

    def diagnose_host_resources(self) -> HostResourceDiagnostic:
        """
        Assess host system RAM and resources portably using Python standard library.
        Never executes auto-kills or spawns background daemons.
        If telemetry is missing/unsupported, marks state strictly as 'unknown' (never 'healthy').
        """
        platform_name = sys.platform
        ram_used_pct: Optional[float] = None
        total_gb: Optional[float] = None
        avail_gb: Optional[float] = None
        telemetry_ok = False
        cause: str

        if platform_name == "win32":
            try:
                import ctypes
                class MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                    ]
                stat = MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                    ram_used_pct = float(stat.dwMemoryLoad)
                    total_gb = round(stat.ullTotalPhys / (1024 ** 3), 1)
                    avail_gb = round(stat.ullAvailPhys / (1024 ** 3), 1)
                    telemetry_ok = True
            except Exception as e:
                cause = f"Windows memory telemetry query failed: {e}"
        elif platform_name.startswith("linux"):
            try:
                mem_total = None
                mem_avail = None
                if os.path.exists("/proc/meminfo"):
                    with open("/proc/meminfo", "r", encoding="utf-8") as f:
                        for line in f:
                            if line.startswith("MemTotal:"):
                                mem_total = float(line.split()[1]) * 1024
                            elif line.startswith("MemAvailable:"):
                                mem_avail = float(line.split()[1]) * 1024
                    if mem_total and mem_avail:
                        ram_used_pct = round(((mem_total - mem_avail) / mem_total) * 100.0, 1)
                        total_gb = round(mem_total / (1024 ** 3), 1)
                        avail_gb = round(mem_avail / (1024 ** 3), 1)
                        telemetry_ok = True
            except Exception as e:
                cause = f"Linux /proc/meminfo read failed: {e}"

        if not telemetry_ok:
            return HostResourceDiagnostic(
                telemetry_available=False,
                platform=platform_name,
                ram_used_percent=None,
                ram_total_gb=None,
                ram_available_gb=None,
                state="unknown",
                diagnosis_type="unknown_cause",
                confirmed_or_unknown_cause="Host resource telemetry unavailable on this platform or query failed.",
                missing_prerequisites=["host_resource_telemetry"],
                action_owner="agent_action",
                human_input_needed=False,
                question_id=None,
                next_action="Continue execution; verify host RAM manually if experiencing OOM or slowdowns.",
            )

        if ram_used_pct is not None and ram_used_pct >= 95.0:
            state = "critical"
            diag_type = "confirmed_diagnosis"
            cause = f"Host RAM at {ram_used_pct}% (critical threshold >= 95%). Spawn no new workers to avoid OOM crash."
            next_act = "Operator or agent close idle processes or reap finished dev servers before adding tasks."
        elif ram_used_pct is not None and ram_used_pct >= 85.0:
            state = "elevated"
            diag_type = "confirmed_diagnosis"
            cause = f"Host RAM at {ram_used_pct}% (elevated threshold >= 85%). Bounded concurrency required."
            next_act = "Monitor memory usage; reap finished servers before starting heavy tasks."
        else:
            state = "ok"
            diag_type = "confirmed_diagnosis"
            cause = f"Host RAM at {ram_used_pct}% ({avail_gb} GB available of {total_gb} GB total)."
            next_act = "Host resources healthy; worker dispatch permitted."

        return HostResourceDiagnostic(
            telemetry_available=True,
            platform=platform_name,
            ram_used_percent=ram_used_pct,
            ram_total_gb=total_gb,
            ram_available_gb=avail_gb,
            state=state,
            diagnosis_type=diag_type,
            confirmed_or_unknown_cause=cause,
            missing_prerequisites=[],
            action_owner="agent_action",
            human_input_needed=False,
            question_id=None,
            next_action=next_act,
        )

    # -----------------------------------------------------------------------
    # 6. Aggregate Diagnostics Report
    # -----------------------------------------------------------------------

    def run_diagnostics(self) -> DiagnosticReport:
        """
        Produce complete end-to-end diagnostic report across all subsystems.
        """
        now_utc = get_iso_timestamp()

        # Gather subsystems
        services = self.diagnose_services()
        providers = self.diagnose_providers()
        decisions = self.diagnose_decisions()
        requests = self.diagnose_requests(services, decisions)
        worker_board = self.diagnose_worker_and_board()
        host_resources = self.diagnose_host_resources()
        # Aggregate human inputs and agent actions
        human_inputs: List[HumanInputItem] = []
        agent_actions: List[AgentActionItem] = []

        seen_q_ids = set()

        # Check services for human inputs (e.g. missing credentials)
        for s in services.values():
            if s.human_input_needed and s.question_id and s.question_id not in seen_q_ids:
                seen_q_ids.add(s.question_id)
                human_inputs.append(HumanInputItem(
                    question_id=s.question_id,
                    category="credential",
                    target=s.service,
                    question=s.question_text or f"Configure credentials for {s.service}",
                    resolution_guidance=s.resolution_guidance or "Set environment variable in secure shell.",
                    actionable_command=None,
                ))
            elif not s.human_input_needed and s.missing_prerequisites:
                agent_actions.append(AgentActionItem(
                    target=s.service,
                    category="preflight_probe",
                    missing_prerequisites=s.missing_prerequisites,
                    next_action=s.next_action,
                ))

        # Check decisions for human inputs
        for d in decisions.values():
            if d.human_input_needed and d.question_id and d.question_id not in seen_q_ids:
                seen_q_ids.add(d.question_id)
                cmd = f"python decision_workflow.py reply {d.decision_id} --answer <option>"
                human_inputs.append(HumanInputItem(
                    question_id=d.question_id,
                    category="preference",
                    target=d.decision_id,
                    question=d.question,
                    resolution_guidance=f"Authorized responder (@{', @'.join(d.authorized_responders)}) reply on issue or via CLI.",
                    actionable_command=cmd,
                ))

        # Check requests for human inputs / agent actions
        for r in requests.values():
            if r.human_input_needed and r.question_id and r.question_id not in seen_q_ids:
                seen_q_ids.add(r.question_id)
                cmd = f"python ledger.py update {r.request_id} --authorize --authorized-by <operator>"
                human_inputs.append(HumanInputItem(
                    question_id=r.question_id,
                    category="authorization",
                    target=r.request_id,
                    question=r.question_text or f"Authorize {r.request_id}",
                    resolution_guidance="Verify all QA/review proofs before granting merge authorization.",
                    actionable_command=cmd,
                ))
            elif r.state != "done":
                cat = "implementation" if r.state == "implementation" else (
                    "qa_verification" if r.state == "QA" else (
                        "review" if r.state == "review" else "stage_progression"
                    )
                )
                agent_actions.append(AgentActionItem(
                    target=r.request_id,
                    category=cat,
                    missing_prerequisites=r.missing_prerequisites,
                    next_action=r.next_action,
                ))

        # Determine overall aggregate status
        has_blocked_services = any(s.state == "blocked" for s in services.values())
        has_stale_services = any(s.state == "stale" for s in services.values())
        all_reqs_done = all(r.state == "done" for r in requests.values()) if requests else False

        if human_inputs:
            agg_status = "awaiting_human"
            summary_reason = f"{len(human_inputs)} item(s) require human operator input (authorization, decision, or credential setup)."
        elif has_blocked_services or any(r.blockers for r in requests.values()):
            agg_status = "blocked"
            summary_reason = "Workflow blocked by unsatisfied service or request dependencies."
        elif has_stale_services:
            agg_status = "stale"
            summary_reason = "Service evidence is stale (> TTL); fresh preflight probes required before deployable task progression."
        elif all_reqs_done:
            agg_status = "healthy"
            summary_reason = "All registered requests completed and verified."
        else:
            agg_status = "actionable"
            summary_reason = "Requests are actionable for autonomous agent execution."

        boundaries = {
            "auto_merge_allowed": False,
            "auto_deploy_allowed": False,
            "self_spawn_loop": False,
            "production_access": "strictly_prohibited",
            "unknown_or_stale_never_green": True,
            "cached_credentials_never_live_healthy": True,
            "missing_code_or_tests_agent_owned": True,
        }

        return DiagnosticReport(
            schema_version="1.0",
            generated_at_utc=now_utc,
            aggregate_status=agg_status,
            summary_reason=summary_reason,
            services=services,
            providers=providers,
            requests=requests,
            decisions=decisions,
            worker_and_board=worker_board,
            host_resources=host_resources,
            human_inputs=human_inputs,
            agent_actions=agent_actions,
            boundaries=boundaries,
        )


# ---------------------------------------------------------------------------
# Terminal Summary Formatter
# ---------------------------------------------------------------------------

def format_diagnostic_summary(report: DiagnosticReport) -> str:
    """Format human-readable comprehensive diagnostic overview."""
    lines = [
        "=" * 76,
        "PORTABLE WORKFLOW AGGREGATE SYSTEM & REQUEST DIAGNOSTICS",
        "=" * 76,
        f"Generated UTC:       {report.generated_at_utc}",
        f"Aggregate Status:    {report.aggregate_status.upper()}",
        f"Diagnosis Summary:   {report.summary_reason}",
        "-" * 76,
        "1. CONFIGURED SERVICES & PREFLIGHT EVIDENCE (Access vs Health vs Stale)",
        "-" * 76,
    ]

    for s_name, s in report.services.items():
        stale_flag = " [EXPIRED/STALE]" if s.is_stale else ""
        live_flag = " (live verified)" if s.live_verified else " (cached attestation)"
        status_line = f"  [{s.state.upper()}]{stale_flag} {s_name:<18} (type: {s.system_type})"
        lines.append(status_line)
        lines.append(f"    Access Status:    {s.access_status} | Health Status: {s.health_status}{live_flag}")
        lines.append(f"    Observed Source:  {s.observed_source} (age: {s.evidence_age_seconds or 0}s, TTL: {s.ttl_seconds}s)")
        lines.append(f"    Cause / Reason:   {s.confirmed_or_unknown_cause}")
        if s.missing_prerequisites:
            lines.append(f"    Missing Prereqs:  {', '.join(s.missing_prerequisites)}")
        lines.append(f"    Action Owner:     {s.action_owner.upper()} -> {s.next_action}")
        lines.append("")

    lines.extend([
        "-" * 76,
        "2. MODEL PROVIDERS, QUOTAS & USAGE WINDOWS",
        "-" * 76,
    ])
    for p_name, p in report.providers.items():
        usage_str = f"{p.usage_percent:.1f}%" if p.usage_percent is not None else "N/A"
        reset_str = f", reset: {p.reset_time_utc}" if p.reset_time_utc else ""
        lines.append(f"  [{p.state.upper():<9}] {p_name:<20} usage: {usage_str}{reset_str} ({p.confirmed_or_unknown_cause})")

    lines.extend([
        "-" * 76,
        "3. HOST MACHINE RESOURCES & RAM TELEMETRY",
        "-" * 76,
    ])
    hr = report.host_resources
    if hr.telemetry_available:
        lines.append(f"  [{hr.state.upper():<9}] Platform: {hr.platform} | RAM Load: {hr.ram_used_percent}% ({hr.ram_available_gb} GB avail / {hr.ram_total_gb} GB total)")
        lines.append(f"    Diagnosis:        {hr.confirmed_or_unknown_cause}")
        lines.append(f"    Next Action:      {hr.next_action}")
    else:
        lines.append(f"  [UNKNOWN  ] Platform: {hr.platform} | Telemetry: unavailable (never assumed healthy)")
        lines.append(f"    Diagnosis:        {hr.confirmed_or_unknown_cause}")
        lines.append(f"    Next Action:      {hr.next_action}")

    lines.extend([
        "-" * 76,
        "4. REGISTERED REQUESTS & CRITERIA COMPLETION",
        "-" * 76,
    ])
    if not report.requests:
        lines.append("  (No requests registered in ledger)")
    for req_id, r in report.requests.items():
        lines.append(f"  [{r.state.upper():<12}] {req_id} (type: {r.task_type}, owner: {r.owner})")
        lines.append(f"    Head Commit:      {r.head or 'unbound'}")
        lines.append(f"    Criteria:         {r.criteria_summary['verified']}/{r.criteria_summary['total']} verified (pending: {', '.join(r.pending_criteria) or 'None'})")
        lines.append(f"    Cause / State:    {r.confirmed_or_unknown_cause}")
        if r.missing_prerequisites:
            lines.append(f"    Missing Prereqs:  {', '.join(r.missing_prerequisites)}")
        lines.append(f"    Action Owner:     {r.action_owner.upper()} -> {r.next_action}")
        lines.append("")

    lines.extend([
        "-" * 76,
        "5. ASYNCHRONOUS DECISIONS & AUTHORIZATION GATES",
        "-" * 76,
    ])
    if not report.decisions:
        lines.append("  (No decisions registered)")
    for d_id, d in report.decisions.items():
        lines.append(f"  [{d.status.upper():<8}] {d_id}: {d.question}")
        lines.append(f"    Responders:       @{', @'.join(d.authorized_responders)}")
        lines.append(f"    Status Detail:    {d.confirmed_or_unknown_cause}")
        lines.append(f"    Next Action:      {d.next_action}")

    lines.extend([
        "-" * 76,
        "6. HUMAN OPERATOR INPUT REQUIRED (Zero Punts; Pure Authorization/Credentials)",
        "-" * 76,
    ])
    if not report.human_inputs:
        lines.append("  [NONE] No human operator input currently required. All pending work is agent-owned.")
    else:
        for idx, hi in enumerate(report.human_inputs, 1):
            lines.append(f"  ({idx}) Question ID:   [{hi.question_id}] (category: {hi.category})")
            lines.append(f"      Target:        {hi.target}")
            lines.append(f"      Question:      {hi.question}")
            lines.append(f"      Resolution:    {hi.resolution_guidance}")
            if hi.actionable_command:
                lines.append(f"      Command:       {hi.actionable_command}")
            lines.append("")

    lines.extend([
        "-" * 76,
        "7. AUTONOMOUS AGENT ACTIONS (Implementation, Tests, QA, Probes)",
        "-" * 76,
    ])
    if not report.agent_actions:
        lines.append("  (No autonomous agent actions queued)")
    else:
        for idx, act in enumerate(report.agent_actions, 1):
            lines.append(f"  ({idx}) Target:        {act.target} [{act.category}]")
            lines.append(f"      Missing:       {', '.join(act.missing_prerequisites) or 'none'}")
            lines.append(f"      Next Step:     {act.next_action}")

    lines.extend([
        "=" * 76,
        "INVARIANTS & POLICIES ENFORCED:",
        f"  Unknown/Stale Never Green:        {report.boundaries['unknown_or_stale_never_green']}",
        f"  Cached Credentials != Live Health: {report.boundaries['cached_credentials_never_live_healthy']}",
        f"  Missing Code/Tests Agent-Owned:   {report.boundaries['missing_code_or_tests_agent_owned']}",
        f"  Production Access Forbidden:      {report.boundaries['production_access']}",
        f"  Auto-Merge Forbidden:             True (human authorization required)",
        "=" * 76,
    ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI Interface
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Portable System, Service & Request Diagnostics"
    )
    p.add_argument("--state-dir", default=None, help="Root directory holding ledger.json, decisions.json, preflight_evidence/")
    p.add_argument("--ledger", default=None, help="Path to ledger.json")
    p.add_argument("--decisions", default=None, help="Path to decisions.json")
    p.add_argument("--evidence-dir", default=None, help="Path to preflight evidence directory")
    p.add_argument("--balance-file", default=None, help="Path to usage snapshot or fixture JSON")
    p.add_argument("--repo", default=None, help="Target repository override")
    p.add_argument("--json", action="store_true", help="Emit diagnostic report as machine-readable JSON")
    p.add_argument("--summary", action="store_true", help="Emit diagnostic report as formatted terminal summary")
    p.add_argument("--strict", action="store_true", help="Exit code 1 if status is blocked or awaiting human input")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    collector = DiagnosticCollector(
        state_dir=args.state_dir,
        ledger_path=args.ledger,
        decisions_path=args.decisions,
        evidence_dir=args.evidence_dir,
        balance_file=args.balance_file,
        repo=args.repo,
    )

    report = collector.run_diagnostics()

    if args.json or not args.summary:
        print(report.to_json())
    else:
        print(format_diagnostic_summary(report))

    if args.strict and report.aggregate_status not in ("healthy", "actionable"):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
