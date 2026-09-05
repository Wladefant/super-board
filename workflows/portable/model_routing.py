#!/usr/bin/env python3
"""
Veyyon Reset-Aware Model Selector & Workflow Routing Utility
Location: ~/.veyyon/workflows/model_routing.py

Implements deterministic, quota-aware and reset-aware model selection
based on the read-only subscription snapshot from balance_loader:
  1. Capability-first matching: ensures the selected model satisfies task complexity,
     context size, and review depth (Flash 3.8 is NOT the sole quality gate for strong review).
  2. Proportional budget & reset-aware promotion:
     - Near reset with surplus allowance: promotes eligible Codex models to utilize capacity
       that would otherwise expire and be wasted.
     - Distant reset: preserves scarce Anthropic capacity, preferring abundant Gemini Flash/Pro
       or promoted Codex.
  3. Cooldown / 429 / Stale safety:
     - Providers in cooldown or rate-limit fail over cleanly to capable alternatives.
     - Unknown balances are never treated as 0 (starvation) or infinite (flood).
  4. Token-saving review protocol:
     - Generates compact EvidencePacket (< 1.5 KB) referencing exact head/diff, contracts,
       reproduction commands, and test outputs so strong reviewers expand only needed files.
"""

import argparse
import datetime
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure balance_loader is importable from sibling module
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from balance_loader import (
    BalanceAdapter,
    NormalizedBalanceSnapshot,
    SanitizedUsageSnapshot,
    get_balance_adapter,
    load_snapshot,
    parse_usage_json,
)


class TaskType(str, Enum):
    ROUTINE_EXECUTION = "routine_execution"  # Mapping, file edits, routine test runs, fast verification
    DEEP_REASONING = "deep_reasoning"        # Architecture, complex invariants, concurrency, algorithmic debugging
    STRONG_REVIEW = "strong_review"          # High-stakes code review, invariant audits, QA signoff
    DEEP_CONTEXT = "deep_context"            # Context spans > 180k tokens, large diff analysis
    TINY_TASK = "tiny_task"                  # Commit summaries, compaction, lightweight formatting


class RiskLevel(str, Enum):
    LOW = "low"          # Cosmetic, isolated unit tests, docs
    MEDIUM = "medium"    # Internal workflows, multiple file refactors
    HIGH = "high"        # Shared contracts, security, invariants, financial/data safety


# Verified Model Identifiers (strictly verified catalog IDs, NO fictitious names)
MODEL_GEMINI_FLASH = "google-antigravity/gemini-3.8-flash:high"
MODEL_GEMINI_LITE = "google-antigravity/gemini-3.1-flash-lite"
MODEL_GEMINI_PRO = "google-antigravity/gemini-3.1-pro"

MODEL_CLAUDE_FABLE = "anthropic/claude-fable-5-1"
MODEL_CLAUDE_OPUS = "anthropic/claude-opus-5:high"

MODEL_CODEX_FAST = "openai-codex/gpt-5.3-codex"
MODEL_CODEX_SOL = "openai-codex/gpt-5.6-sol:high"
MODEL_CODEX_ASTRA = "openai-codex/gpt-6-astra:high"

MODEL_GROK_DORMANT = "xai-oauth/grok-4.6:high"


@dataclass
class HarnessDispatchPacket:
    """
    Harness-agnostic end-to-end dispatch recommendation packet.
    Usable by any orchestrator or harness without mutating global state or rewriting configs.
    """
    schema_version: str
    generated_at_utc: str
    task: Dict[str, Any]
    recommendation: Dict[str, Any]
    quota_context: Dict[str, Any]
    evidence_packet: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


def model_to_agent_role(model_id: str, task_type: TaskType, risk_level: RiskLevel) -> str:
    """Map model and task type to standard canonical agent role."""
    if "flash-lite" in model_id:
        return "compactor"
    if "flash" in model_id:
        return "task"
    if "pro" in model_id:
        return "task"
    if "fable" in model_id:
        return "reviewer"
    if "opus" in model_id:
        return "thinker" if task_type == TaskType.DEEP_REASONING else "reviewer"
    if "sol" in model_id:
        return "reviewer" if task_type == TaskType.STRONG_REVIEW else "thinker"
    if "astra" in model_id:
        return "orchestrator"
    if "codex" in model_id:
        return "task"
    return "task"


def model_to_provider(model_id: str) -> str:
    """Map model ID to canonical provider name."""
    if "google" in model_id:
        return "google"
    if "anthropic" in model_id:
        return "anthropic"
    if "openai" in model_id or "codex" in model_id:
        return "openai"
    if "xai" in model_id or "grok" in model_id:
        return "xai"
    return "unknown"


@dataclass
class RoutingRecommendation:
    task_type: str
    risk_level: str
    context_tokens: int
    selected_model: str
    fallback_model: str
    reasoning: str
    burn_headroom: float
    promotion_applied: bool
    cooldown_fallback: bool
    provider_statuses: Dict[str, str]
    quota_metrics: Dict[str, Any]
    evidence_packet_required: bool


@dataclass
class EvidencePacket:
    """Compact structured evidence packet for token-saving strong reviews."""
    head_sha: str
    base_sha: str
    changed_files: List[str]
    contracts_changed: List[str]
    reproduction_steps: str
    test_results: str
    risk_summary: str
    reference_urls: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_compact_markdown(self) -> str:
        """Render compact markdown (< 1.5 KB) for review prompts without token bloat."""
        files_str = ", ".join(self.changed_files[:8])
        if len(self.changed_files) > 8:
            files_str += f" (+{len(self.changed_files) - 8} more)"

        contracts_str = "; ".join(self.contracts_changed) if self.contracts_changed else "None"
        urls_str = ", ".join(self.reference_urls) if self.reference_urls else "N/A"

        return (
            f"### Evidence Packet (Review Scope: {self.base_sha[:8]}..{self.head_sha[:8]})\n"
            f"- **Target Files:** {files_str}\n"
            f"- **Contracts/Invariants:** {contracts_str}\n"
            f"- **Reproduction:** `{self.reproduction_steps}`\n"
            f"- **Verification Output:** {self.test_results}\n"
            f"- **Risk & Blast Radius:** {self.risk_summary}\n"
            f"- **References:** {urls_str}\n"
            f"> *Reviewer Note: Expand individual diffs via tool read on demand; full files are not inlined.*"
        )


class ResetAwareModelSelector:
    """
    Deterministic selector implementing:
      - Capability-first filtering
      - Reset-aware Codex promotion near expiration with surplus allowance
      - Anthropic preservation when reset is distant
      - Gemini 3.8 Flash as default executor but not sole review gate
      - Safe 429/cooldown/unknown handling
    """

    def __init__(self, snapshot: Optional[Any] = None):
        if snapshot is not None and isinstance(snapshot, BalanceAdapter):
            self.snapshot = snapshot.fetch_snapshot()
        elif snapshot is not None and hasattr(snapshot, "to_normalized"):
            self.snapshot = snapshot.to_normalized()
        else:
            self.snapshot = snapshot

    def set_snapshot(self, snapshot: Any):
        if snapshot is not None and isinstance(snapshot, BalanceAdapter):
            self.snapshot = snapshot.fetch_snapshot()
        elif snapshot is not None and hasattr(snapshot, "to_normalized"):
            self.snapshot = snapshot.to_normalized()
        else:
            self.snapshot = snapshot
    def evaluate_provider(self, provider: str) -> Dict[str, Any]:
        """Extract quota metrics and health for a given provider."""
        if not self.snapshot:
            return {
                "remaining_fraction": 0.5,
                "hours_to_reset": 999.0,
                "bottleneck_label": "unknown_snapshot",
                "status": "unknown",
                "burn_headroom": 1.0,
                "is_available": True,
            }

        # Get bottleneck window allowance (both remaining_fraction and hours_to_reset from the SAME window!)
        rem_frac, hrs_reset, btn_label, status = self.snapshot.get_effective_allowance(provider)
        is_dormant = provider in getattr(self.snapshot, "dormant_providers", []) or status == "dormant"
        is_cooldown = status in ("cooldown", "rate_limited", "limit_reached", "not_allowed")
        is_available = not is_dormant and not is_cooldown

        # Real window duration in hours directly from normalized balance (no string sniffing!)
        norm_prov = getattr(self.snapshot, "providers", {}).get(provider) if hasattr(self.snapshot, "providers") else None
        if norm_prov and hasattr(norm_prov, "bottleneck_duration_hours") and norm_prov.bottleneck_duration_hours:
            window_duration_hours = norm_prov.bottleneck_duration_hours
        else:
            window_duration_hours = 24.0

        time_ratio = max(0.01, min(1.0, hrs_reset / window_duration_hours))
        burn_headroom = rem_frac / time_ratio if time_ratio > 0 else 1.0

        # Cycle allowance (e.g. 7-day or 30-day subscription reset)
        if hasattr(self.snapshot, "get_cycle_allowance"):
            cycle_rem, cycle_hrs, cycle_lbl, _ = self.snapshot.get_cycle_allowance(provider)
        elif norm_prov and hasattr(norm_prov, "cycle_hours_to_reset"):
            cycle_rem = norm_prov.pro_weekly_remaining_fraction if norm_prov.pro_weekly_remaining_fraction is not None else rem_frac
            cycle_hrs = norm_prov.cycle_hours_to_reset
            cycle_lbl = getattr(norm_prov, "primary_window_id", btn_label)
        else:
            cycle_rem = rem_frac
            cycle_hrs = hrs_reset
            cycle_lbl = btn_label

        # Pro weekly allowance for Codex (specifically tracking the 7d window for Sol/Astra promotion)
        pro_rem = cycle_rem
        pro_hrs = cycle_hrs
        pro_headroom = burn_headroom
        if norm_prov and hasattr(norm_prov, "pro_weekly_remaining_fraction") and norm_prov.pro_weekly_remaining_fraction is not None:
            pro_rem = norm_prov.pro_weekly_remaining_fraction
            pro_hrs = norm_prov.pro_weekly_hours_to_reset if norm_prov.pro_weekly_hours_to_reset is not None else cycle_hrs
            pro_dur = norm_prov.pro_weekly_duration_hours or 168.0
            pro_time_ratio = max(0.01, min(1.0, pro_hrs / pro_dur))
            pro_headroom = pro_rem / pro_time_ratio if pro_time_ratio > 0 else 1.0

        return {
            "remaining_fraction": rem_frac,
            "hours_to_reset": hrs_reset,
            "bottleneck_label": btn_label,
            "status": status,
            "burn_headroom": burn_headroom,
            "is_available": is_available,
            "cycle_remaining": cycle_rem,
            "cycle_hours_to_reset": cycle_hrs,
            "cycle_label": cycle_lbl,
            "pro_remaining": pro_rem,
            "pro_hours_to_reset": pro_hrs,
            "pro_headroom": pro_headroom,
        }

    def select_model(
        self,
        task_type: TaskType = TaskType.ROUTINE_EXECUTION,
        risk_level: RiskLevel = RiskLevel.LOW,
        context_tokens: int = 10000,
        allow_codex_promotion: bool = True,
        rework_count: int = 0,
        domain_tags: Optional[List[str]] = None,
    ) -> RoutingRecommendation:
        """
        Determines the optimal model based on capability, context tokens, risk, and quota metrics.
        """
        # 1. Inspect provider metrics
        google_meta = self.evaluate_provider("google-antigravity")
        anthropic_meta = self.evaluate_provider("anthropic")
        codex_meta = self.evaluate_provider("openai-codex")

        provider_statuses = {
            "google-antigravity": google_meta["status"],
            "anthropic": anthropic_meta["status"],
            "openai-codex": codex_meta["status"],
            "xai-oauth": "dormant",
        }

        quota_metrics = {
            "google_remaining": google_meta["remaining_fraction"],
            "google_reset_hrs": google_meta["hours_to_reset"],
            "anthropic_remaining": anthropic_meta["remaining_fraction"],
            "anthropic_reset_hrs": anthropic_meta["hours_to_reset"],
            "codex_remaining": codex_meta["remaining_fraction"],
            "codex_reset_hrs": codex_meta["hours_to_reset"],
            "codex_burn_headroom": codex_meta["burn_headroom"],
        }

        # 2. Check Codex Promotion Eligibility using Pro 7d weekly window (specifically avoiding Spark distortion)
        codex_pro_hrs = codex_meta.get("pro_hours_to_reset", codex_meta["hours_to_reset"])
        codex_pro_rem = codex_meta.get("pro_remaining", codex_meta["remaining_fraction"])
        codex_pro_headroom = codex_meta.get("pro_headroom", codex_meta["burn_headroom"])

        codex_near_reset_surplus = (
            allow_codex_promotion
            and codex_meta["is_available"]
            and codex_pro_hrs <= 48.0
            and codex_pro_rem >= 0.25
            and codex_pro_headroom >= 1.25
        )

        # 3. Check Anthropic Preservation Need using 7d cycle window (not 5h rolling window!)
        anthropic_cycle_hrs = anthropic_meta.get("cycle_hours_to_reset", anthropic_meta["hours_to_reset"])
        anthropic_distant_reset = (
            anthropic_meta["is_available"] and anthropic_cycle_hrs > 48.0
        )

        # 4. Rework-Aware Routing: Force strong first-pass for critical domains or after invariant rework
        HIGH_RISK_DOMAINS = {"state_machine", "auth", "money", "concurrency", "migration", "schema", "invariants"}
        is_rework_critical = (
            risk_level == RiskLevel.HIGH
            or rework_count >= 1
            or (domain_tags is not None and any(t in HIGH_RISK_DOMAINS for t in domain_tags))
        )
        selected_model: str
        fallback_model: str
        reasoning: str
        promotion_applied = False
        cooldown_fallback = False
        evidence_packet_required = risk_level in (RiskLevel.MEDIUM, RiskLevel.HIGH) or task_type == TaskType.STRONG_REVIEW

        # CASE A: DEEP CONTEXT (> 180k tokens)
        if context_tokens > 180000 or task_type == TaskType.DEEP_CONTEXT:
            # Multi-provider deep context support: Gemini 3.1 Pro (2M) or Claude Opus (200k)
            if google_meta["is_available"]:
                selected_model = MODEL_GEMINI_PRO
                fallback_model = MODEL_CLAUDE_OPUS if anthropic_meta["is_available"] else MODEL_GEMINI_FLASH
                reasoning = f"Context size {context_tokens} tokens routed to Gemini 3.1 Pro 2M window."
            elif anthropic_meta["is_available"]:
                selected_model = MODEL_CLAUDE_OPUS
                fallback_model = MODEL_CLAUDE_FABLE
                reasoning = f"Context size {context_tokens} tokens routed to Claude Opus 200k window."
            else:
                selected_model = MODEL_GEMINI_FLASH
                fallback_model = MODEL_GEMINI_LITE
                cooldown_fallback = True
                reasoning = "Emergency deep-context fallback."

        # CASE B: STRONG REVIEW
        elif task_type == TaskType.STRONG_REVIEW:
            if is_rework_critical:
                # High-risk review requires strong models: Fable 5.1, Opus 5, or promoted Codex Sol/Astra.
                # Flash 3.8 is STRICTLY BARRED as primary or fallback quality gate!
                if codex_near_reset_surplus:
                    selected_model = MODEL_CODEX_SOL
                    fallback_model = MODEL_CLAUDE_FABLE if anthropic_meta["is_available"] else MODEL_CLAUDE_OPUS
                    promotion_applied = True
                    reasoning = (
                        f"High-risk review: Codex Pro weekly window resets in {codex_meta['hours_to_reset']:.1f}h "
                        f"with {codex_meta['remaining_fraction']*100:.1f}% allowance remaining. "
                        f"Promoted Codex Sol to utilize expiring allowance while preserving distant-reset Anthropic."
                    )
                elif anthropic_meta["is_available"]:
                    selected_model = MODEL_CLAUDE_FABLE
                    fallback_model = MODEL_CLAUDE_OPUS if codex_meta["is_available"] else MODEL_CODEX_SOL
                    reasoning = "High-risk review assigned to Claude Fable 5.1 (Flash 3.8 barred as sole quality gate; fallback Opus)."
                elif codex_meta["is_available"]:
                    selected_model = MODEL_CODEX_SOL
                    fallback_model = MODEL_CODEX_ASTRA
                    reasoning = "Anthropic unavailable; routing high-risk review to Codex Sol (fallback Astra)."
                else:
                    selected_model = MODEL_GEMINI_PRO
                    fallback_model = MODEL_GEMINI_PRO
                    cooldown_fallback = True
                    reasoning = "All strong review models in cooldown; emergency deep-context fallback to Gemini Pro (Flash barred)."
            elif risk_level == RiskLevel.MEDIUM:
                if codex_near_reset_surplus:
                    selected_model = MODEL_CODEX_SOL
                    fallback_model = MODEL_CLAUDE_FABLE if anthropic_meta["is_available"] else MODEL_GEMINI_FLASH
                    promotion_applied = True
                    reasoning = "Medium-risk review: Promoted Codex Sol near reset to consume surplus capacity."
                elif anthropic_meta["is_available"] and not anthropic_distant_reset:
                    selected_model = MODEL_CLAUDE_FABLE
                    fallback_model = MODEL_GEMINI_FLASH
                    reasoning = "Medium-risk review: Anthropic near reset, routing to Claude Fable."
                else:
                    selected_model = MODEL_CLAUDE_FABLE if anthropic_meta["is_available"] else MODEL_GEMINI_FLASH
                    fallback_model = MODEL_GEMINI_FLASH
                    reasoning = "Medium-risk review: Claude Fable primary with Flash 3.8 fallback."
            else:
                # Low-risk review: Flash 3.8 is safe and fast
                selected_model = MODEL_GEMINI_FLASH
                fallback_model = MODEL_CLAUDE_FABLE if anthropic_meta["is_available"] else MODEL_GEMINI_PRO
                reasoning = "Low-risk review: Gemini 3.8 Flash fast review execution."

        # CASE C: DEEP REASONING / ARCHITECTURE
        elif task_type == TaskType.DEEP_REASONING:
            if is_rework_critical:
                # High-risk reasoning: Flash 3.8 is STRICTLY FORBIDDEN as primary or fallback!
                # Strong first-pass reasoning: Sol, Astra, or Opus.
                if codex_near_reset_surplus or codex_meta["is_available"]:
                    selected_model = MODEL_CODEX_SOL
                    fallback_model = MODEL_CLAUDE_OPUS if anthropic_meta["is_available"] else MODEL_CODEX_ASTRA
                    if codex_near_reset_surplus:
                        promotion_applied = True
                    reasoning = (
                        "High-risk deep reasoning: assigned to strong model Codex Sol "
                        f"(fallback {fallback_model}; Flash barred from high-risk reasoning)."
                    )
                elif anthropic_meta["is_available"]:
                    selected_model = MODEL_CLAUDE_OPUS
                    fallback_model = MODEL_CLAUDE_FABLE
                    reasoning = "High-risk deep reasoning: assigned to Claude Opus 5 (Flash barred from high-risk reasoning)."
                else:
                    selected_model = MODEL_GEMINI_PRO
                    fallback_model = MODEL_GEMINI_PRO
                    cooldown_fallback = True
                    reasoning = "Emergency high-risk fallback to Gemini Pro (Flash barred)."
            else:
                # Medium or Low risk reasoning:
                if codex_near_reset_surplus:
                    selected_model = MODEL_CODEX_SOL
                    fallback_model = MODEL_CLAUDE_OPUS if anthropic_meta["is_available"] else MODEL_GEMINI_FLASH
                    promotion_applied = True
                    reasoning = (
                        f"Deep reasoning: Codex weekly window resets in {codex_meta['hours_to_reset']:.1f}h "
                        f"with {codex_meta['remaining_fraction']*100:.1f}% allowance. Promoted Codex Sol to prevent waste."
                    )
                elif anthropic_meta["is_available"] and not anthropic_distant_reset:
                    selected_model = MODEL_CLAUDE_OPUS
                    fallback_model = MODEL_GEMINI_FLASH
                    reasoning = "Deep reasoning assigned to Claude Opus 5 (Anthropic capacity available)."
                elif google_meta["is_available"]:
                    # Preserve distant Anthropic, use abundant Gemini Flash for low-medium risk
                    selected_model = MODEL_GEMINI_FLASH
                    fallback_model = MODEL_CLAUDE_OPUS if anthropic_meta["is_available"] else MODEL_GEMINI_PRO
                    reasoning = (
                        "Deep reasoning: Preserving distant-reset Anthropic capacity "
                        f"({anthropic_meta['hours_to_reset']:.1f}h to reset); using abundant Gemini 3.8 Flash."
                    )
                elif anthropic_meta["is_available"]:
                    selected_model = MODEL_CLAUDE_OPUS
                    fallback_model = MODEL_CODEX_SOL if codex_meta["is_available"] else MODEL_GEMINI_FLASH
                    cooldown_fallback = True
                    reasoning = "Gemini unavailable; escalated to Claude Opus for deep reasoning."
                else:
                    selected_model = MODEL_CODEX_SOL if codex_meta["is_available"] else MODEL_GEMINI_FLASH
                    fallback_model = MODEL_GEMINI_FLASH
                    reasoning = "Fallback deep reasoning lane."
        # CASE D: TINY TASK (Compaction / Commits)
        elif task_type == TaskType.TINY_TASK:
            selected_model = MODEL_GEMINI_LITE
            fallback_model = MODEL_GEMINI_FLASH
            reasoning = "Lightweight background / compaction task routed to Gemini 3.1 Flash Lite."

        # CASE E: ROUTINE EXECUTION (Implementation, Mapping, Routine QA)
        else:
            if is_rework_critical:
                # Rework-aware high-risk first-pass implementation (state machines, auth, money, migrations, concurrency)
                # First pass with a weak model risks invariant failure & expensive rework. Use strong first-pass!
                if codex_meta["is_available"]:
                    selected_model = MODEL_CODEX_SOL
                    fallback_model = MODEL_CLAUDE_OPUS if anthropic_meta["is_available"] else MODEL_CODEX_ASTRA
                    if codex_near_reset_surplus:
                        promotion_applied = True
                    reasoning = (
                        "High-risk implementation first pass (state machines/auth/money/concurrency/migrations): "
                        f"routed to strong model Codex Sol to prevent invariant rework (fallback {fallback_model})."
                    )
                elif anthropic_meta["is_available"]:
                    selected_model = MODEL_CLAUDE_OPUS
                    fallback_model = MODEL_CLAUDE_FABLE
                    reasoning = "High-risk implementation first pass: routed to Claude Opus 5 to prevent rework."
                else:
                    selected_model = MODEL_GEMINI_FLASH
                    fallback_model = MODEL_GEMINI_PRO
                    reasoning = "High-risk implementation: strong models unavailable, fallback to Gemini Flash."
            elif codex_near_reset_surplus and risk_level != RiskLevel.LOW:
                # Promote Codex Fast for capable implementation when Codex capacity is expiring
                selected_model = MODEL_CODEX_FAST
                fallback_model = MODEL_GEMINI_FLASH
                promotion_applied = True
                reasoning = (
                    f"Routine execution: Codex Pro allowance expiring in {codex_meta['hours_to_reset']:.1f}h "
                    f"({codex_meta['remaining_fraction']*100:.1f}% left). Promoted Codex Fast to burn surplus capacity."
                )
            elif google_meta["is_available"]:
                selected_model = MODEL_GEMINI_FLASH
                fallback_model = MODEL_CODEX_FAST if codex_meta["is_available"] else MODEL_GEMINI_LITE
                reasoning = "Primary abundant execution lane: Gemini 3.8 Flash (Ultra daily allowance)."
            elif codex_meta["is_available"]:
                selected_model = MODEL_CODEX_FAST
                fallback_model = MODEL_CLAUDE_FABLE if anthropic_meta["is_available"] else MODEL_GEMINI_LITE
                cooldown_fallback = True
                reasoning = "Google Antigravity in cooldown; falling back to Codex Fast for execution."
            elif anthropic_meta["is_available"]:
                selected_model = MODEL_CLAUDE_FABLE
                fallback_model = MODEL_GEMINI_FLASH
                cooldown_fallback = True
                reasoning = "Emergency fallback to Anthropic for execution."
            else:
                selected_model = MODEL_GEMINI_FLASH
                fallback_model = MODEL_GEMINI_LITE
                reasoning = "Default execution lane."

        return RoutingRecommendation(
            task_type=task_type.value,
            risk_level=risk_level.value,
            context_tokens=context_tokens,
            selected_model=selected_model,
            fallback_model=fallback_model,
            reasoning=reasoning,
            burn_headroom=codex_meta["burn_headroom"],
            promotion_applied=promotion_applied,
            cooldown_fallback=cooldown_fallback,
            provider_statuses=provider_statuses,
            quota_metrics=quota_metrics,
            evidence_packet_required=evidence_packet_required,
        )

    def dispatch(
        self,
        task_type: TaskType = TaskType.ROUTINE_EXECUTION,
        risk_level: RiskLevel = RiskLevel.LOW,
        context_tokens: int = 10000,
        allow_codex_promotion: bool = True,
        rework_count: int = 0,
        domain_tags: Optional[List[str]] = None,
        head_sha: Optional[str] = None,
        base_sha: Optional[str] = None,
        changed_files: Optional[List[str]] = None,
        contracts_changed: Optional[List[str]] = None,
        reproduction_steps: Optional[str] = None,
        test_results: Optional[str] = None,
        risk_summary: Optional[str] = None,
        reference_urls: Optional[List[str]] = None,
    ) -> HarnessDispatchPacket:
        """
        Produce a complete, harness-agnostic dispatch packet ready for execution.
        Does not mutate any harness configuration or global state.
        """
        rec = self.select_model(
            task_type=task_type,
            risk_level=risk_level,
            context_tokens=context_tokens,
            allow_codex_promotion=allow_codex_promotion,
            rework_count=rework_count,
            domain_tags=domain_tags,
        )

        agent_role = model_to_agent_role(rec.selected_model, task_type, risk_level)
        fallback_role = model_to_agent_role(rec.fallback_model, task_type, risk_level)
        provider = model_to_provider(rec.selected_model)
        fallback_provider = model_to_provider(rec.fallback_model)

        evidence_dict = None
        if rec.evidence_packet_required:
            packet = EvidencePacket(
                head_sha=head_sha or "HEAD",
                base_sha=base_sha or "HEAD~1",
                changed_files=changed_files or [],
                contracts_changed=contracts_changed or [],
                reproduction_steps=reproduction_steps or "N/A",
                test_results=test_results or "Pending verification",
                risk_summary=risk_summary or f"Risk level: {risk_level.value}",
                reference_urls=reference_urls or [],
            )
            evidence_dict = packet.to_dict()

        now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        return HarnessDispatchPacket(
            schema_version="1.0",
            generated_at_utc=now_utc,
            task={
                "task_type": rec.task_type,
                "risk_level": rec.risk_level,
                "context_tokens": rec.context_tokens,
                "rework_count": rework_count,
                "domain_tags": domain_tags or [],
            },
            recommendation={
                "model": rec.selected_model,
                "provider": provider,
                "agent_role": agent_role,
                "execution_command": f"veyyon -p --model {rec.selected_model} \"<task_prompt>\"",
                "fallback_model": rec.fallback_model,
                "fallback_provider": fallback_provider,
                "fallback_agent_role": fallback_role,
                "promotion_applied": rec.promotion_applied,
                "cooldown_fallback": rec.cooldown_fallback,
                "rationale": rec.reasoning,
            },
            quota_context={
                "burn_headroom": rec.burn_headroom,
                "hours_to_reset": rec.quota_metrics.get("codex_reset_hrs", 0.0),
                "remaining_fraction": rec.quota_metrics.get("google_remaining", 1.0),
                "provider_statuses": rec.provider_statuses,
            },
            evidence_packet=evidence_dict,
        )

def main():
    parser = argparse.ArgumentParser(description="Veyyon Reset-Aware Model Selector CLI")
    parser.add_argument("--task-type", choices=[t.value for t in TaskType], default=TaskType.ROUTINE_EXECUTION.value)
    parser.add_argument("--risk-level", choices=[r.value for r in RiskLevel], default=RiskLevel.LOW.value)
    parser.add_argument("--context-tokens", type=int, default=10000)
    parser.add_argument("--disallow-promotion", action="store_true", help="Disallow near-reset Codex promotion")
    parser.add_argument("--json", action="store_true", help="Output recommendation as JSON")
    parser.add_argument("--emit-dispatch", action="store_true", help="Output complete HarnessDispatchPacket JSON")
    parser.add_argument("--adapter", choices=["veyyon", "file", "direct"], default="veyyon", help="Adapter type")
    parser.add_argument("--balance-file", default=None, help="Input balance JSON file (or '-' for stdin)")
    parser.add_argument("--balance-cmd", default=None, help="Custom balance CLI command")
    parser.add_argument("--rework-count", type=int, default=0, help="Number of prior failed attempts/invariant reworks")
    parser.add_argument("--domain-tags", default="", help="Comma-separated domain tags (e.g. auth,state_machine,money)")
    args = parser.parse_args()

    domain_tags = [t.strip() for t in args.domain_tags.split(",") if t.strip()] if args.domain_tags else None

    adapter = get_balance_adapter(
        adapter_type="file" if args.balance_file else args.adapter,
        file_path=args.balance_file,
        cmd=args.balance_cmd,
    )
    selector = ResetAwareModelSelector(adapter)

    if args.emit_dispatch:
        packet = selector.dispatch(
            task_type=TaskType(args.task_type),
            risk_level=RiskLevel(args.risk_level),
            context_tokens=args.context_tokens,
            allow_codex_promotion=not args.disallow_promotion,
            rework_count=args.rework_count,
            domain_tags=domain_tags,
        )
        print(packet.to_json())
        return

    rec = selector.select_model(
        task_type=TaskType(args.task_type),
        risk_level=RiskLevel(args.risk_level),
        context_tokens=args.context_tokens,
        allow_codex_promotion=not args.disallow_promotion,
        rework_count=args.rework_count,
        domain_tags=domain_tags,
    )
    if args.json:
        print(json.dumps(asdict(rec), indent=2))
    else:
        print("=" * 65)
        print("VEYYON RESET-AWARE MODEL ROUTING RECOMMENDATION")
        print("=" * 65)
        print(f"Task Type:       {rec.task_type}")
        print(f"Risk Level:      {rec.risk_level}")
        print(f"Context Tokens:  {rec.context_tokens}")
        print(f"Selected Model:  {rec.selected_model}")
        print(f"Fallback Model:  {rec.fallback_model}")
        print(f"Promotion:       {'YES (Codex surplus promoted)' if rec.promotion_applied else 'NO'}")
        print(f"Cooldown Fallbk: {'YES' if rec.cooldown_fallback else 'NO'}")
        print(f"Evidence Packet: {'REQUIRED' if rec.evidence_packet_required else 'OPTIONAL'}")
        print(f"Reasoning:       {rec.reasoning}")
        print("-" * 65)
        print("Provider Statuses:")
        for prov, stat in rec.provider_statuses.items():
            print(f"  {prov:<20}: {stat}")
        print("=" * 65)


if __name__ == "__main__":
    main()
