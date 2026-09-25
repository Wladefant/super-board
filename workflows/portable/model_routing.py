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

# Antigravity serves Claude and GPT families on their own daily windows, separate from
# Gemini's (live-tested 2026-09-25). They reset daily, so unused headroom expires sooner
# than any Anthropic/Codex weekly window and is spent first where risk policy allows.
MODEL_AG_CLAUDE_OPUS = "google-antigravity/claude-opus-4-6"
MODEL_AG_CLAUDE_SONNET = "google-antigravity/claude-sonnet-4-6"
MODEL_AG_GPT_OSS = "google-antigravity/gpt-oss-120b"

# Cheap pay-per-token overflow worker: DeepSeek direct API (DeepSeek-V4.1-Flash,
# $0.30/$1.20 per 1M peak, half off-peak), with its OpenRouter twin as fallback.
MODEL_DEEPSEEK_FLASH = "deepseek/deepseek-flash:high"
MODEL_OR_DEEPSEEK_FLASH = "openrouter/deepseek/deepseek-v4.1-flash@deepinfra"

# Second-opinion reviewer on the OpenRouter free quota (1,000 requests/day). Advisory
# only: it never approves, blocks or replaces the required review. Nemotron 3 Ultra free
# serves reliably with tools; Qwen3.8 27B free has the best free Terminal-Bench 4.0 score
# (5.6% vs 0.5%) but its only free upstream returned 429 on every attempt 2026-09-25.
MODEL_OR_FREE_ADVISORY = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"

# Weekly subscription windows are paced, not capped (operator 2026-09-25): each must
# last the whole week AND be spent fully by its reset. Pace headroom is remaining
# fraction / remaining time fraction (1.0 = linear spend).
# - Below CODEX_PACE_MIN_HEADROOM the Codex pro window is spending ahead of pace and is
#   held back for emergencies until the clock catches up.
# - At or above SURPLUS_PACE_HEADROOM within SURPLUS_WINDOW_HOURS of reset, the unused
#   allowance would expire, so it is promoted onto work it can do.
# - Direct Anthropic is the Opus orchestrator's budget. Worker lanes may draw on it only
#   while it runs at least ANTHROPIC_WORKER_MIN_HEADROOM behind pace, so the orchestrator
#   keeps a linear share for the entire week.
CODEX_PACE_MIN_HEADROOM = 1.0
SURPLUS_PACE_HEADROOM = 1.25
SURPLUS_WINDOW_HOURS = 48.0
ANTHROPIC_WORKER_MIN_HEADROOM = 1.10
# An Antigravity family below this remaining fraction is left alone for the day.
AG_FAMILY_MIN_REMAINING = 0.10
AG_ANTHROPIC_PROVIDER = "google-antigravity:anthropic"
AG_OPENAI_PROVIDER = "google-antigravity:openai"

# Catalog-verified model context windows (models.db authoritative, no fabricated context sizes)
VERIFIED_CONTEXT_WINDOWS: Dict[str, int] = {
    MODEL_GEMINI_FLASH: 1048576,
    MODEL_GEMINI_LITE: 1048576,
    MODEL_GEMINI_PRO: 1048576,
    MODEL_CLAUDE_FABLE: 1000000,
    MODEL_CLAUDE_OPUS: 1000000,
    MODEL_CODEX_FAST: 400000,
    MODEL_CODEX_SOL: 372000,
    MODEL_CODEX_ASTRA: 272000,
    MODEL_AG_CLAUDE_OPUS: 250000,
    MODEL_AG_CLAUDE_SONNET: 250000,
    MODEL_AG_GPT_OSS: 131072,
    MODEL_DEEPSEEK_FLASH: 1048576,
}


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
    if model_id.startswith(("deepseek/", "openrouter/deepseek/")):
        return "ds-task"
    if model_id.endswith(":free"):
        return "extra-review"
    if model_id.startswith("google-antigravity/") and ("claude" in model_id or "gpt-oss" in model_id):
        # Antigravity Claude/GPT run on their own daily window; the ag-opus agent pins it.
        return "ag-opus"
    if "flash-lite" in model_id:
        return "compactor"
    if "flash" in model_id:
        return "qa-verifier" if task_type == TaskType.STRONG_REVIEW and risk_level == RiskLevel.LOW else "task"
    if "pro" in model_id:
        return "task"
    if "fable" in model_id:
        return "reviewer"
    if "opus" in model_id:
        return "thinker" if task_type == TaskType.DEEP_REASONING else "reviewer"
    if "sol" in model_id:
        return "codex-reviewer" if task_type == TaskType.STRONG_REVIEW else "codex-worker"
    if "astra" in model_id:
        return "orchestrator" if task_type not in (TaskType.STRONG_REVIEW, TaskType.ROUTINE_EXECUTION) else ("codex-reviewer" if task_type == TaskType.STRONG_REVIEW else "codex-worker")
    if "codex" in model_id:
        return "codex-reviewer" if task_type == TaskType.STRONG_REVIEW else "codex-worker"
    return "task"

def model_to_provider(model_id: str) -> str:
    """Map model ID to canonical provider name."""
    if model_id.startswith("openrouter/"):
        return "openrouter"
    if model_id.startswith("deepseek/"):
        return "deepseek"
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
    # Non-blocking second opinion; never a merge gate or approval.
    advisory_model: Optional[str] = None


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
                "cycle_headroom": 1.0,
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

        # Pace of the cycle window: remaining fraction over remaining time fraction.
        # 1.0 = spending exactly linearly; < 1.0 = ahead of pace (window would run out
        # before reset); > 1.0 = behind pace (allowance would expire unused).
        cycle_dur = getattr(norm_prov, "cycle_duration_hours", None) or window_duration_hours
        cycle_time_ratio = max(0.01, min(1.0, cycle_hrs / cycle_dur))
        cycle_headroom = cycle_rem / cycle_time_ratio

        # Pro weekly allowance for Codex (specifically tracking the 7d window for Sol/Astra promotion)
        pro_rem = cycle_rem
        pro_hrs = cycle_hrs
        if norm_prov and hasattr(norm_prov, "pro_weekly_remaining_fraction") and norm_prov.pro_weekly_remaining_fraction is not None:
            pro_rem = norm_prov.pro_weekly_remaining_fraction
            pro_hrs = norm_prov.pro_weekly_hours_to_reset if norm_prov.pro_weekly_hours_to_reset is not None else cycle_hrs
            pro_dur = norm_prov.pro_weekly_duration_hours or 168.0
            pro_time_ratio = max(0.01, min(1.0, pro_hrs / pro_dur))
            pro_headroom = pro_rem / pro_time_ratio
        else:
            pro_headroom = cycle_headroom

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
            "cycle_headroom": cycle_headroom,
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
        ag_anthropic_meta = self.evaluate_provider(AG_ANTHROPIC_PROVIDER)
        ag_openai_meta = self.evaluate_provider(AG_OPENAI_PROVIDER)

        provider_statuses = {
            "google-antigravity": google_meta["status"],
            AG_ANTHROPIC_PROVIDER: ag_anthropic_meta["status"],
            AG_OPENAI_PROVIDER: ag_openai_meta["status"],
            "anthropic": anthropic_meta["status"],
            "openai-codex": codex_meta["status"],
            "xai-oauth": "dormant",
        }

        quota_metrics = {
            "google_remaining": google_meta["remaining_fraction"],
            "google_reset_hrs": google_meta["hours_to_reset"],
            "ag_anthropic_remaining": ag_anthropic_meta["remaining_fraction"],
            "ag_anthropic_reset_hrs": ag_anthropic_meta["hours_to_reset"],
            "anthropic_remaining": anthropic_meta["remaining_fraction"],
            "anthropic_reset_hrs": anthropic_meta["hours_to_reset"],
            "codex_remaining": codex_meta["remaining_fraction"],
            "codex_reset_hrs": codex_meta["hours_to_reset"],
            "codex_burn_headroom": codex_meta["burn_headroom"],
        }

        # 2. Codex pro 7d window (Spark excluded): promote while it would expire unused,
        # hold back while it is being spent ahead of pace.
        codex_pro_hrs = codex_meta.get("pro_hours_to_reset", codex_meta["hours_to_reset"])
        codex_pro_headroom = codex_meta.get("pro_headroom", codex_meta["burn_headroom"])

        codex_near_reset_surplus = (
            allow_codex_promotion
            and codex_meta["is_available"]
            and codex_pro_hrs <= SURPLUS_WINDOW_HOURS
            and codex_pro_headroom >= SURPLUS_PACE_HEADROOM
        )
        codex_throttled = codex_meta["is_available"] and codex_pro_headroom < CODEX_PACE_MIN_HEADROOM
        codex_usable = codex_meta["is_available"] and not codex_throttled
        quota_metrics["codex_pro_headroom"] = codex_pro_headroom
        quota_metrics["codex_pro_throttled"] = codex_throttled

        # Antigravity Claude runs on a free daily window. Only a window the snapshot
        # actually reports counts; an unreported family is not assumed to exist.
        ag_claude_ok = (
            ag_anthropic_meta["status"] == "ok"
            and ag_anthropic_meta["remaining_fraction"] >= AG_FAMILY_MIN_REMAINING
        )
        ag_claude_note = (
            f"Antigravity Claude daily window: {ag_anthropic_meta['remaining_fraction']*100:.1f}% unused, "
            f"resets in {ag_anthropic_meta['hours_to_reset']:.1f}h"
        )

        # 3. Direct Anthropic 7d window is the Opus orchestrator's budget. Workers only get
        # the slack behind pace; stale or unknown data never counts as slack.
        anthropic_cycle_hrs = anthropic_meta.get("cycle_hours_to_reset", anthropic_meta["hours_to_reset"])
        anthropic_headroom = anthropic_meta.get("cycle_headroom", 1.0)
        anthropic_worker_ok = (
            anthropic_meta["status"] == "ok" and anthropic_headroom >= ANTHROPIC_WORKER_MIN_HEADROOM
        )
        # Low/medium work takes Anthropic only when its surplus is about to expire.
        anthropic_surplus = (
            anthropic_worker_ok
            and anthropic_cycle_hrs <= SURPLUS_WINDOW_HOURS
            and anthropic_headroom >= SURPLUS_PACE_HEADROOM
        )
        quota_metrics["anthropic_headroom"] = anthropic_headroom
        quota_metrics["anthropic_orchestrator_reserve"] = not anthropic_worker_ok

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

        def strong_ladder(anthropic_model: str, label: str) -> Tuple[str, str, str, bool, bool]:
            """High-risk ladder: (selected, fallback, reasoning, promotion, cooldown).

            Order spends what expires first: Codex surplus about to reset, then the free
            Antigravity Claude daily window, then Anthropic slack behind pace, then Codex
            on pace; the orchestrator's Anthropic reserve and Codex ahead of pace only as
            emergencies. Flash, DeepSeek and free models never qualify.
            """
            anthropic_alt = MODEL_CLAUDE_OPUS if anthropic_model == MODEL_CLAUDE_FABLE else MODEL_CLAUDE_FABLE
            if codex_near_reset_surplus:
                fallback = anthropic_model if anthropic_worker_ok else (
                    MODEL_AG_CLAUDE_OPUS if ag_claude_ok else MODEL_CODEX_ASTRA
                )
                return (
                    MODEL_CODEX_SOL, fallback,
                    f"{label}: Codex pro weekly window resets in {codex_pro_hrs:.1f}h at "
                    f"{codex_pro_headroom:.2f}x pace headroom. Promoted Codex Sol to spend allowance "
                    "that would otherwise expire.",
                    True, False,
                )
            if ag_claude_ok:
                if anthropic_worker_ok:
                    fallback = anthropic_model
                elif codex_usable:
                    fallback = MODEL_CODEX_SOL
                else:
                    fallback = anthropic_model if anthropic_meta["is_available"] else MODEL_GEMINI_PRO
                return (
                    MODEL_AG_CLAUDE_OPUS, fallback,
                    f"{label} on Antigravity Claude Opus 4.6: its daily window expires before any "
                    f"weekly window. {ag_claude_note}.",
                    False, False,
                )
            if anthropic_worker_ok:
                return (
                    anthropic_model, anthropic_alt,
                    f"{label} on {anthropic_model}: the Anthropic weekly window runs "
                    f"{anthropic_headroom:.2f}x behind pace, so slack beyond the orchestrator's share is spent.",
                    False, False,
                )
            if codex_usable:
                return (
                    MODEL_CODEX_SOL, MODEL_CODEX_ASTRA,
                    f"{label} on Codex Sol ({codex_pro_headroom:.2f}x pace headroom); direct Anthropic "
                    "stays reserved for the Opus orchestrator.",
                    False, False,
                )
            if anthropic_meta["is_available"]:
                return (
                    anthropic_model, anthropic_alt,
                    f"{label}: no worker allowance left; drawing on the Anthropic orchestrator reserve.",
                    False, True,
                )
            if codex_meta["is_available"]:
                return (
                    MODEL_CODEX_SOL, MODEL_GEMINI_PRO,
                    f"{label}: only Codex pro ahead of pace remains; spending its emergency reserve (Flash barred).",
                    False, True,
                )
            return (
                MODEL_GEMINI_PRO, MODEL_GEMINI_PRO,
                f"{label}: all strong models unavailable or in cooldown; emergency fallback to Gemini Pro "
                "(Flash barred from high-risk work and structural failure retry).",
                False, True,
            )

        # CASE A: DEEP CONTEXT (> 180k tokens)
        if context_tokens > 180000 or task_type == TaskType.DEEP_CONTEXT:
            # Multi-provider deep context support: Gemini 3.1 Pro (2M) or Claude Opus (200k)
            if google_meta["is_available"]:
                selected_model = MODEL_GEMINI_PRO
                fallback_model = MODEL_CLAUDE_OPUS if anthropic_meta["is_available"] else MODEL_GEMINI_FLASH
                reasoning = f"Context size {context_tokens} tokens routed to Gemini 3.1 Pro ({VERIFIED_CONTEXT_WINDOWS.get(MODEL_GEMINI_PRO, '1M')} token window)."
            elif anthropic_meta["is_available"]:
                selected_model = MODEL_CLAUDE_OPUS
                fallback_model = MODEL_CLAUDE_FABLE
                reasoning = f"Context size {context_tokens} tokens routed to Claude Opus ({VERIFIED_CONTEXT_WINDOWS.get(MODEL_CLAUDE_OPUS, '1M')} token window)."
            else:
                selected_model = MODEL_GEMINI_FLASH
                fallback_model = MODEL_GEMINI_LITE
                cooldown_fallback = True
                reasoning = "Emergency deep-context fallback."

        # CASE B: STRONG REVIEW
        elif task_type == TaskType.STRONG_REVIEW:
            if is_rework_critical:
                # High-risk review: strong models only; Flash is barred as primary or fallback gate.
                selected_model, fallback_model, reasoning, promotion_applied, cooldown_fallback = strong_ladder(
                    MODEL_CLAUDE_FABLE, "High-risk review"
                )
            elif risk_level == RiskLevel.MEDIUM:
                if codex_near_reset_surplus:
                    selected_model = MODEL_CODEX_SOL
                    fallback_model = MODEL_AG_CLAUDE_OPUS if ag_claude_ok else MODEL_GEMINI_FLASH
                    promotion_applied = True
                    reasoning = "Medium-risk review: Promoted Codex Sol near reset to consume surplus capacity."
                elif ag_claude_ok:
                    selected_model = MODEL_AG_CLAUDE_OPUS
                    fallback_model = MODEL_CODEX_SOL if codex_usable else MODEL_GEMINI_FLASH
                    reasoning = (
                        "Medium-risk review on Antigravity Claude Opus 4.6: its daily window expires unused "
                        f"long before any weekly window. {ag_claude_note}."
                    )
                elif anthropic_surplus:
                    selected_model = MODEL_CLAUDE_FABLE
                    fallback_model = MODEL_GEMINI_FLASH
                    reasoning = "Medium-risk review: Anthropic weekly surplus expires soon, routing to Claude Fable."
                elif codex_usable:
                    selected_model = MODEL_CODEX_SOL
                    fallback_model = MODEL_GEMINI_FLASH
                    reasoning = "Medium-risk review on Codex Sol (on pace); direct Anthropic reserved for the orchestrator."
                else:
                    selected_model = MODEL_GEMINI_FLASH
                    fallback_model = MODEL_DEEPSEEK_FLASH
                    reasoning = "Medium-risk review on Gemini 3.8 Flash; direct Anthropic reserved for the orchestrator."
            else:
                # Low-risk review: Flash 3.8 is safe and fast
                selected_model = MODEL_GEMINI_FLASH
                fallback_model = MODEL_CLAUDE_FABLE if anthropic_meta["is_available"] else MODEL_GEMINI_PRO
                reasoning = "Low-risk review: Gemini 3.8 Flash fast review execution."

        # CASE C: DEEP REASONING / ARCHITECTURE
        elif task_type == TaskType.DEEP_REASONING:
            if is_rework_critical:
                # High-risk reasoning: strong models only; Flash is barred as primary or fallback.
                selected_model, fallback_model, reasoning, promotion_applied, cooldown_fallback = strong_ladder(
                    MODEL_CLAUDE_OPUS, "High-risk deep reasoning"
                )
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
                elif ag_claude_ok:
                    selected_model = MODEL_AG_CLAUDE_OPUS
                    fallback_model = MODEL_GEMINI_FLASH
                    reasoning = f"Deep reasoning on Antigravity Claude Opus 4.6 before paid Anthropic. {ag_claude_note}."
                elif anthropic_surplus:
                    selected_model = MODEL_CLAUDE_OPUS
                    fallback_model = MODEL_GEMINI_FLASH
                    reasoning = "Deep reasoning on Claude Opus 5: Anthropic weekly surplus expires soon."
                elif google_meta["is_available"]:
                    # Preserve distant Anthropic, use abundant Gemini Flash for low-medium risk
                    selected_model = MODEL_GEMINI_FLASH
                    fallback_model = MODEL_CODEX_SOL if codex_usable else MODEL_GEMINI_PRO
                    reasoning = (
                        "Deep reasoning: Preserving distant-reset Anthropic capacity "
                        f"({anthropic_meta['hours_to_reset']:.1f}h to reset); using abundant Gemini 3.8 Flash."
                    )
                elif codex_usable:
                    selected_model = MODEL_CODEX_SOL
                    fallback_model = MODEL_DEEPSEEK_FLASH
                    cooldown_fallback = True
                    reasoning = "Gemini unavailable; deep reasoning on Codex Sol (on pace), Anthropic reserved for the orchestrator."
                else:
                    selected_model = MODEL_DEEPSEEK_FLASH
                    fallback_model = MODEL_OR_DEEPSEEK_FLASH
                    cooldown_fallback = True
                    reasoning = "Gemini and Codex unavailable or ahead of pace; deep reasoning overflow to DeepSeek V4.1 Flash."
        # CASE D: TINY TASK (Compaction / Commits)
        elif task_type == TaskType.TINY_TASK:
            selected_model = MODEL_GEMINI_LITE
            fallback_model = MODEL_GEMINI_FLASH
            reasoning = "Lightweight background / compaction task routed to Gemini 3.1 Flash Lite."

        # CASE E: ROUTINE EXECUTION (Implementation, Mapping, Routine QA)
        else:
            if is_rework_critical:
                # High-risk first pass (state machines, auth, money, migrations, concurrency):
                # a weak first pass risks invariant failure and expensive rework.
                selected_model, fallback_model, reasoning, promotion_applied, cooldown_fallback = strong_ladder(
                    MODEL_CLAUDE_OPUS, "High-risk implementation first pass (to prevent invariant rework)"
                )
            elif codex_near_reset_surplus and risk_level != RiskLevel.LOW:
                # Promote Codex Fast for capable implementation when Codex capacity is expiring
                selected_model = MODEL_CODEX_FAST
                fallback_model = MODEL_GEMINI_FLASH
                promotion_applied = True
                reasoning = (
                    f"Routine execution: Codex Pro allowance expiring in {codex_pro_hrs:.1f}h "
                    f"({codex_pro_headroom:.2f}x pace headroom). Promoted Codex Fast to burn surplus capacity."
                )
            elif google_meta["is_available"]:
                selected_model = MODEL_GEMINI_FLASH
                fallback_model = MODEL_DEEPSEEK_FLASH
                reasoning = (
                    "Primary abundant execution lane: Gemini 3.8 Flash (Ultra daily allowance); "
                    "DeepSeek V4.1 Flash is the overflow tier."
                )
            elif codex_usable:
                selected_model = MODEL_CODEX_FAST
                fallback_model = MODEL_DEEPSEEK_FLASH
                cooldown_fallback = True
                reasoning = "Google Antigravity in cooldown; falling back to Codex Fast (subscription headroom) for execution."
            else:
                # Subscription lanes are exhausted, in cooldown or throttled: the cheap
                # pay-per-token tier takes routine work instead of burning Anthropic.
                selected_model = MODEL_DEEPSEEK_FLASH
                fallback_model = MODEL_OR_DEEPSEEK_FLASH
                cooldown_fallback = True
                reasoning = "Gemini Flash and Codex unavailable or throttled; overflow to DeepSeek V4.1 Flash (direct API, OpenRouter fallback)."

        # Free OpenRouter second opinion for reviews: advisory only (1000 req/day free tier),
        # never a merge gate, never an approval, never a replacement for the review above.
        advisory_model = MODEL_OR_FREE_ADVISORY if task_type == TaskType.STRONG_REVIEW else None

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
            advisory_model=advisory_model,
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
