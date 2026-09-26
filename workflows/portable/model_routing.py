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
import re
import socket
import sqlite3
import sys
from dataclasses import asdict, dataclass, field, replace
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
from quota_snapshot import load_snapshot as load_quota_snapshot


class TaskType(str, Enum):
    ROUTINE_EXECUTION = "routine_execution"  # Mapping, file edits, routine test runs, fast verification
    DEEP_REASONING = "deep_reasoning"        # Architecture, complex invariants, concurrency, algorithmic debugging
    STRONG_REVIEW = "strong_review"          # High-stakes code review, invariant audits, QA signoff
    DEEP_CONTEXT = "deep_context"            # Context spans > 180k tokens, large diff analysis
    TINY_TASK = "tiny_task"                  # Commit summaries, compaction, formatting, bulk triage/classification


class RiskLevel(str, Enum):
    LOW = "low"          # Cosmetic, isolated unit tests, docs
    MEDIUM = "medium"    # Internal workflows, multiple file refactors
    HIGH = "high"        # Shared contracts, security, invariants, financial/data safety


# Verified Model Identifiers (strictly verified catalog IDs, NO fictitious names)
MODEL_GEMINI_FLASH = "google-antigravity/gemini-3.8-flash:high"
MODEL_GEMINI_LITE = "google-antigravity/gemini-3.1-flash-lite"
MODEL_GEMINI_PRO = "google-antigravity/gemini-3.1-pro"

# Paid direct Anthropic: the Main orchestrator's budget and the high-risk REVIEW lane.
# Never a worker primary or worker fallback while any cheap tier has headroom (operator
# 2026-09-25). In worker ladders Fable is the very last rung, and only on slack behind
# pace (ANTHROPIC_WORKER_MIN_HEADROOM), never on the orchestrator's reserve.
MODEL_CLAUDE_FABLE = "anthropic/claude-fable-5-1"
# Paid direct Anthropic Opus 5.5: reserved strictly for super-hard work (money, billing,
# ledger, migration first-pass reviews, cross-cutting architectural changes, or when ag-opus
# quota is exhausted; operator ruling 2026-09-26 ~13:25Z: "I mean only hard, super hard work, right?
# Don't move everything in there"). Never used for routine implementation, sync, merge, triage or small reviews.
MODEL_CLAUDE_OPUS_55 = "anthropic/claude-opus-5-5:high"

MODEL_CODEX_FAST = "openai-codex/gpt-5.3-codex"
# The operator's Codex worker/review tier is Astra medium (profile `codex-worker` and
# `codex-reviewer` pins). Sol is costlier than Astra and bound to no role, so it is not routed.
MODEL_CODEX_ASTRA = "openai-codex/gpt-6-astra:medium"
# The free Spark window is separate from the Codex pro allowance and has its own enabled roster
# entry (`spark`, medium effort), so it is pinned separately from the Astral worker roles.
MODEL_CODEX_SPARK = "openai-codex/gpt-5.3-codex-spark:medium"

MODEL_GROK_DORMANT = "xai-oauth/grok-4.6:high"

# Antigravity serves Claude and GPT families on their own daily windows, separate from
# Gemini's (live-tested 2026-09-25). They reset daily, so unused headroom expires sooner
# than any Anthropic/Codex weekly window. They are a permitted cheap worker tier (operator
# ruling): `ag-opus` may take worker work while its window is above AG_FAMILY_MIN_REMAINING.
# Opus worker and review roles default to medium effort (Issue #228, operator 2026-09-26).
MODEL_AG_CLAUDE_OPUS = "google-antigravity/claude-opus-4-6"
MODEL_AG_CLAUDE_SONNET = "google-antigravity/claude-sonnet-4-6"
MODEL_AG_GPT_OSS = "google-antigravity/gpt-oss-120b"

# Cheap pay-per-token overflow worker: DeepSeek direct API (DeepSeek-V4.1-Flash,
# $0.30/$1.20 per 1M peak, half off-peak), with its OpenRouter twin as fallback.
MODEL_DEEPSEEK_FLASH = "deepseek/deepseek-flash:high"
MODEL_OR_DEEPSEEK_FLASH = "openrouter/deepseek/deepseek-v4.1-flash"
# DeepSeek V4 Pro (catalog id `deepseek/deepseek-v4-pro`, 1M context, pay-per-token):
# the strong pay-per-token worker rung. It must run through the `ds-pro` role, never
# `ds-task` (pinned to DeepSeek Flash), or a high-risk task silently runs on Flash.
MODEL_DEEPSEEK_PRO = "deepseek/deepseek-v4-pro"

# Second-opinion reviewer on the OpenRouter free quota (1,000 requests/day). Advisory
# only: it never approves, blocks or replaces the required review. Nemotron 3 Ultra free
# serves reliably with tools; Qwen3.8 27B free has the best free Terminal-Bench 4.0 score
# (5.6% vs 0.5%) but its only free upstream returned 429 on every attempt 2026-09-25.
MODEL_OR_FREE_ADVISORY = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"

# OpenCode Go provider (operator 2026-09-25).  Specs verified from the vendor page
# https://opencode.ai/docs/go/ and cross-checked against the local catalog row
# `opencode-go:models-v1:iqgqa0x44ieo` in ~/.veyyon/profiles/default/agent/models.db
# (rev 11, 35 models) on 2026-09-25.  Go is a $10/month SUBSCRIPTION, not a prepaid
# balance and not five separate plans.  Two independent limits apply:
#
#   1. A per-model monthly DOLLAR cap, enforced through three nested rolling windows:
#      5h = 20% of that cap, week = 50%, month = 100%.  A lane costing more than the 5h
#      slice cannot run at all (glm-5.3's $3 5h slice is smaller than one lane).
#   2. The provider-level spend veyyon can observe, reported in USD for those same three
#      windows (`veyyon usage --json`, 2026-09-25: rolling-5h limit $12.00, weekly
#      $30.00, monthly $60.00, unit "usd", metadata {"planType": "OpenCode Go",
#      "source": "veyyon-observed-request-costs"}).  Those totals are the Tier-1 shape,
#      i.e. the largest monthly cap on the menu, so per-model budgets are computed from
#      that aggregate and are a deliberately conservative lower bound.
#
# Measured lane cost = one task lane at the median lane shape from 3,535 local session
# traces (2026-09-25: 775,488 input / 9,244,043 cache-read / 44,114 output tokens)
# priced at the documented per-1M rates.  Whole lanes per window follow.
#
#   model                monthly cap   $/lane   lanes/month   lanes/week   lanes/5h
#   space-bunny-free     unlimited     0        unlimited     unlimited    unlimited
#   glm-5.3-flash        $60           0.4157   ~144          ~72          ~28
#   qwen3.8-flash        $30           0.2850   ~105          ~52          ~21
#   gpt-6-luna           $15           0.1920   ~78           ~39          ~15
#   mimo-v2.6-pro        $15           0.4092   ~36           ~18          ~7
#   glm-5.3              $15           3.6832   ~4            ~2           ~0.8
#   qwen3.8-max          $15           4.1267   ~3            ~1           ~0.7
#
# The Go WORKHORSE is therefore glm-5.3-flash ($60 cap), NOT glm-5.3: the flagship-tier
# model carries the SMALLEST monthly cap ($15, ~4 lanes a month) and is reserved for the
# highest-value high-risk review.  space-bunny-free is free and unlimited for a limited
# time and goes first wherever it can serve.
#
# Tool-call smoke test (2026-09-25): space-bunny-free ok 4s, glm-5.3 ok 8s,
# glm-5.3-flash ok 52s, qwen3.8-flash ok 9s, qwen3.8-max ok 11s, gpt-6-luna ok 4s,
# mimo-v2.6-pro ok 9s.  FAILED, so NOT routed: deepseek-v4.1-flash
# (`requiresReasoningContentForToolCalls`, but this provider does not replay reasoning;
# the direct `deepseek` provider works) and muse-spark-1.3-contributor (timeout).
MODEL_GO_BUNNY = "opencode-go/space-bunny-free"       # free + unlimited, 1M ctx, multimodal
MODEL_GO_GLM53_FLASH = "opencode-go/glm-5.3-flash"    # $60 cap, .15/.50/.03  <- workhorse
MODEL_GO_QWEN38_FLASH = "opencode-go/qwen3.8-flash"   # $30 cap, .15/.47/.016
MODEL_GO_GPT6_LUNA = "opencode-go/gpt-6-luna"         # $15 cap, .10/.50/.01
MODEL_GO_MIMO26_PRO = "opencode-go/mimo-v2.6-pro"     # $15 cap, .435/.87/.003625
MODEL_GO_GLM53 = "opencode-go/glm-5.3"                # $15 cap, 1.40/4.40/.26  <- rare
MODEL_GO_QWEN38_MAX = "opencode-go/qwen3.8-max"       # $15 cap, 2.00/6.00/.25   <- rare
# Catalog context windows: glm-5.3, glm-5.3-flash, qwen3.8-flash and qwen3.8-max are
# 1,000,000; mimo-v2.6-pro and space-bunny-free are 1,048,576; gpt-6-luna is 1,050,000.

OPENCODE_GO_PROVIDER = "opencode-go"

# ChatGPT Web through the local codex-chatgpt-web bridge (operator 2026-09-25). The bridge is
# a loopback daemon serving a ChatGPT subscription and is cross-family to BOTH writer
# families Option A allows (Gemini Flash and GLM-5.3), so it is the primary gating reviewer
# for critical diffs while it is up, a hard implementation/reasoning tier ahead of the paid
# and scarce lanes, and the standard-review overflow when the OpenCode Go allowance is spent.
# The provider has no entry in `veyyon usage`, so eligibility is a bridge-health precondition:
# the loopback port is probed once per selector and a closed port closes the rung, so the
# ladder falls through instead of dispatching a lane that would die on connect.
MODEL_CHATGPT_WEB = "chatgpt-web/medium"          # roles web-task / web-thinker
CHATGPT_WEB_PROVIDER = "chatgpt-web"
CHATGPT_WEB_BRIDGE_HOST = "127.0.0.1"
CHATGPT_WEB_BRIDGE_PORT = 17841
CHATGPT_WEB_BRIDGE_TIMEOUT = 0.5


def chatgpt_web_bridge_available(host: str = CHATGPT_WEB_BRIDGE_HOST,
                                 port: int = CHATGPT_WEB_BRIDGE_PORT,
                                 timeout: float = CHATGPT_WEB_BRIDGE_TIMEOUT) -> bool:
    """True while the chatgpt-web bridge daemon accepts connections on its loopback port.

    The bridge is a local daemon, not a metered provider, so there is no allowance to read:
    a listening port is the only honest readiness signal, and a closed one must close the
    rung rather than dispatch a lane that cannot run.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Per-model OpenCode Go allowance: monthly dollar cap, the fraction of it each rolling
# window may spend, and the measured cost of one task lane (see the table above).
GO_MONTHLY_CAP_USD: Dict[str, float] = {
    MODEL_GO_GLM53_FLASH: 60.0,
    MODEL_GO_QWEN38_FLASH: 30.0,
    MODEL_GO_GPT6_LUNA: 15.0,
    MODEL_GO_MIMO26_PRO: 15.0,
    MODEL_GO_GLM53: 15.0,
    MODEL_GO_QWEN38_MAX: 15.0,
}
GO_WINDOW_FRACTION: Dict[str, float] = {"rolling-5h": 0.20, "weekly": 0.50, "monthly": 1.00}
GO_LANE_COST_USD: Dict[str, float] = {
    MODEL_GO_GLM53_FLASH: 0.4157,
    MODEL_GO_QWEN38_FLASH: 0.2850,
    MODEL_GO_GPT6_LUNA: 0.1920,
    MODEL_GO_MIMO26_PRO: 0.4092,
    MODEL_GO_GLM53: 3.6832,
    MODEL_GO_QWEN38_MAX: 4.1267,
}
# The pacing window that must both last and be spent: the week is what a subscription
# schedules against, with the 5h and month windows as its inner and outer bounds.
GO_PACE_WINDOW = "weekly"
# Rungs in one pace group are interchangeable for the task, so pacing orders them: the free
# uncapped Go model never competes for allowance and stands alone, and every capped Go model
# shares one group so they are ranked by pace rather than by declared order.
PACE_GROUP_GO_FREE = "go-free"
PACE_GROUP_GO_PAID = "go-paid"
# Strong rungs (review and high-risk worker) and execution rungs are pacing groups too, so a
# Codex window about to expire is spent ahead of an Antigravity daily window that resets
# tonight anyway, while an ordinary day still prefers the cheap abundant tiers.
PACE_GROUP_STRONG = "strong"
PACE_GROUP_EXEC = "exec"

# First-class Chinese-model worker slots (#214, operator 2026-09-25): credential-gated,
# activated automatically once veyyon holds a credential for the provider (env var or a
# stored `/login` credential in the auth store). No code change turns them on.
# - Z.AI GLM Coding Plan (provider `zai`, env ZAI_API_KEY): GLM-5.3 (TB4 41.9%) is the
#   first rung of the high-risk worker ladder and the routine overflow; GLM-5.3-Flash
#   (TB4 32.8%) takes bulk/triage (TINY_TASK).
# - MiniMax Token Plan (provider `minimax-code`, env MINIMAX_CODE_API_KEY; `minimax`
#   reads MINIMAX_API_KEY and is a different provider): MiniMax-M3 (TB4 2.0%) takes
#   bulk/triage and 1M-context overflow only, never implementation or review.
MODEL_ZAI_GLM = "zai/glm-5.3:high"
MODEL_ZAI_GLM_FLASH = "zai/glm-5.3-flash:high"
MODEL_MINIMAX_M3 = "minimax-code/minimax-m3"

ZAI_PROVIDER = "zai"
MINIMAX_PROVIDER = "minimax-code"
CREDENTIAL_ENV_BY_PROVIDER: Dict[str, str] = {
    ZAI_PROVIDER: "ZAI_API_KEY",
    MINIMAX_PROVIDER: "MINIMAX_CODE_API_KEY",
}
# Providers whose credential lives ONLY in the veyyon auth store: OpenCode Go is bound to
# an API key saved by `/login`, so there is no environment variable to look for.  Naming
# an env var for such a provider (as an empty string) made it credentialed on every
# machine, which is how the first version of this table leaked Go into hermetic runs.
STORE_CREDENTIAL_PROVIDERS: Tuple[str, ...] = (OPENCODE_GO_PROVIDER,)


def _auth_store_paths() -> List[str]:
    """veyyon credential stores: the machine-wide shared store and the active profile's own
    store (used when `profileSharing: false`). `VEYYON_CONFIG_DIR` relocates the root."""
    root = os.environ.get("VEYYON_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".veyyon")
    profile = os.environ.get("VEYYON_PROFILE") or "default"
    return [
        os.path.join(root, "shared-auth", "agent.db"),
        os.path.join(root, "profiles", profile, "agent", "agent.db"),
    ]


def _stored_credential_providers(paths: List[str]) -> Set[str]:
    """Provider ids with an enabled stored credential. Reads only the `provider` column,
    read-only; credential payloads are never selected."""
    found: Set[str] = set()
    for path in paths:
        if not os.path.isfile(path):
            continue
        try:
            # timeout=0: a store held under a write lock is skipped at once instead of
            # blocking selector construction for SQLite's default 5 s busy timeout.
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0)
            try:
                rows = conn.execute(
                    "SELECT DISTINCT provider FROM auth_credentials WHERE disabled_cause IS NULL"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        found.update(row[0] for row in rows)
    return found


def detect_credentialed_providers(auth_store_paths: Optional[List[str]] = None) -> Set[str]:
    """Credential-gated providers (Z.AI, MiniMax Code, OpenCode Go) veyyon can authenticate right now."""
    stored = _stored_credential_providers(_auth_store_paths() if auth_store_paths is None else auth_store_paths)
    return {
        provider
        for provider, env_var in CREDENTIAL_ENV_BY_PROVIDER.items()
        if (env_var and os.environ.get(env_var, "").strip()) or provider in stored
    }


# Model each router-emitted role must pin as its FIRST model, for roles this router
# introduced. The router recommends a role alongside the model; a role pinned to another
# model would silently run that model instead. Operator applies these to the profile
# (see policies/default/AGENTS.md "Worker role pins"); the router never edits config.
ROLE_MODEL_PINS: Dict[str, str] = {
    "ds-pro": MODEL_DEEPSEEK_PRO,
    "zai-task": MODEL_ZAI_GLM,
    "zai-flash": MODEL_ZAI_GLM_FLASH,
    "minimax-task": MODEL_MINIMAX_M3,
    "gemini-pro": MODEL_GEMINI_PRO,
    "codex-worker": MODEL_CODEX_ASTRA,
    "codex-reviewer": MODEL_CODEX_ASTRA,
    "ag-opus": MODEL_AG_CLAUDE_OPUS,
    # The free Spark allowance has its own enabled roster entry and its own model, so a lane
    # routed onto Spark must be dispatched as `spark`, never as a Codex Astral role.
    "spark": MODEL_CODEX_SPARK,
    # One role per routed Go model, so a dispatched role can never silently run another
    # model. opencode-go/qwen3.8-flash, /qwen3.8-max and /mimo-v2.6-pro stay catalog-verified
    # and unrouted until they have a matching `modelRoles` entry of their own.
    "go-task": MODEL_GO_BUNNY,
    "go-deep": MODEL_GO_GLM53_FLASH,
    "go-review": MODEL_GO_GLM53,
    "go-bulk": MODEL_GO_GPT6_LUNA,
    # ChatGPT Web serves from a local bridge rather than a metered provider, so both of its
    # roles pin the same tier; model_to_agent_role splits them by task type.
    "web-task": MODEL_CHATGPT_WEB,
    "web-thinker": MODEL_CHATGPT_WEB,
}

# Weekly subscription windows are paced, not capped (operator 2026-09-25): each must
# last the whole week AND be spent fully by its reset. Pace headroom is remaining
# fraction / remaining time fraction (1.0 = linear spend).
# - Codex pro throttle: Codex is held back for emergencies only when BOTH
#   headroom < CODEX_PACE_MIN_HEADROOM (0.90, i.e. a 10% tolerance below linear pace)
#   AND at least CODEX_PACE_USED_FLOOR (50%) of the window is consumed.
#   The used floor is deliberate: below 50% used Codex is never throttled, whatever
#   the burn rate (e.g. 49% used with 150h left is 0.57x pace and still usable), so a
#   few early-week tasks cannot lock Codex out; the throttle engages from the midpoint.
# - At or above SURPLUS_PACE_HEADROOM within SURPLUS_WINDOW_HOURS of reset, the unused
#   allowance would expire, so it is promoted onto work it can do.
# - Direct Anthropic is the orchestrator's budget. Worker ladders may reach Fable only as
#   their last rung and only while it runs at least ANTHROPIC_WORKER_MIN_HEADROOM behind
#   pace, so the orchestrator keeps a linear share for the entire week.
# - ANTHROPIC_BOTTLENECK_MAX_USED guards the tightest Anthropic window (normally the 5h
#   one): no worker or review slack is taken once it is past this fraction used.
CODEX_PACE_MIN_HEADROOM = 0.90
CODEX_PACE_USED_FLOOR = 0.50
SURPLUS_PACE_HEADROOM = 1.25
SURPLUS_WINDOW_HOURS = 48.0
ANTHROPIC_WORKER_MIN_HEADROOM = 1.10
ANTHROPIC_BOTTLENECK_MAX_USED = 0.80
# An Antigravity family below this remaining fraction is left alone for the day.
AG_FAMILY_MIN_REMAINING = 0.10
AG_ANTHROPIC_PROVIDER = "google-antigravity:anthropic"
AG_OPENAI_PROVIDER = "google-antigravity:openai"
ANTIGRAVITY_PROVIDER = "google-antigravity"
CODEX_PROVIDER = "openai-codex"
# Every subscription window is paced, not capped (operator 2026-09-25): it must last its
# whole duration AND be spent by its reset.  `pace_ratio` is remaining allowance over
# remaining time (1.0 = linear spend), computed per window by `window_pace`:
#   pace_ratio > 1  -> behind pace: allowance would expire unused, so prefer this provider;
#   pace_ratio < 1  -> ahead of pace: it would exhaust before its reset, so throttle it
#                      once the window is at least PACE_THROTTLE_USED_FLOOR consumed.
# Within a capability tier `_apply_pace_rules` orders rungs by how far behind pace they
# run, so the provider with the most expiring allowance goes first and the one closest to
# its cap goes last; a throttled provider is dropped from the tier entirely.
PACE_THROTTLE_RATIO = 0.90
PACE_THROTTLE_USED_FLOOR = 0.50
PACE_PREFER_DELTA = 0.05
# Floor for the remaining-time fraction: a window about to reset still has a finite pace
# ratio (remaining allowance over this floor), which is what marks it as expiring.
PACE_MIN_TIME_FRACTION = 0.01

# Catalog-verified model context windows (models.db authoritative, no fabricated context sizes)
VERIFIED_CONTEXT_WINDOWS: Dict[str, int] = {
    MODEL_GEMINI_FLASH: 1048576,
    MODEL_GEMINI_LITE: 1048576,
    MODEL_GEMINI_PRO: 1048576,
    MODEL_CLAUDE_FABLE: 1000000,
    MODEL_CODEX_FAST: 400000,
    MODEL_CODEX_ASTRA: 272000,
    MODEL_AG_CLAUDE_OPUS: 250000,
    MODEL_AG_CLAUDE_SONNET: 250000,
    MODEL_AG_GPT_OSS: 131072,
    MODEL_DEEPSEEK_FLASH: 1048576,
    MODEL_DEEPSEEK_PRO: 1000000,
    MODEL_CLAUDE_OPUS_55: 1000000,
    MODEL_ZAI_GLM: 131072,
    MODEL_ZAI_GLM_FLASH: 131072,
    MODEL_MINIMAX_M3: 1000000,
    # OpenCode Go models (catalog-verified 2026-09-25)
    MODEL_GO_BUNNY: 1048576,
    MODEL_GO_GLM53: 1000000,
    MODEL_GO_GLM53_FLASH: 1000000,
    MODEL_GO_QWEN38_FLASH: 1000000,
    MODEL_GO_QWEN38_MAX: 1000000,
    MODEL_GO_GPT6_LUNA: 1050000,
    MODEL_GO_MIMO26_PRO: 1048576,
    # ChatGPT Web (catalog-verified 2026-09-26 from the profile model cache; all five bridge
    # tiers report 111,193 except chatgpt-web/pro at 112,193)
    MODEL_CHATGPT_WEB: 111193,
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
    """Map model and task type to the canonical agent role that runs that model."""
    # OpenCode Go models
    if model_id.startswith("opencode-go/"):
        if model_id == MODEL_GO_BUNNY:
            return "go-task"
        if model_id == MODEL_GO_GLM53_FLASH:
            return "go-deep"
        if model_id == MODEL_GO_GLM53:
            return "go-review"
        if model_id == MODEL_GO_GPT6_LUNA:
            return "go-bulk"
        if model_id == MODEL_GO_QWEN38_MAX:
            # Chain-only member of go-review (Qwen takes the GLM-authored diffs, where GLM-5.3
            # would be same-family); never routed as a model of its own.
            return "go-review"
        return "go-task"
    if model_id.startswith("chatgpt-web/"):
        return "web-thinker" if task_type == TaskType.STRONG_REVIEW else "web-task"
    if model_id.startswith("openai-codex/"):
        if "codex-spark" in model_id:
            # The free Spark allowance has its own enabled roster entry (`spark`); the
            # codex-worker/codex-reviewer pair is pinned for the Astral tiers and disabled here.
            return "spark"
        return "codex-reviewer" if task_type == TaskType.STRONG_REVIEW else "codex-worker"
    if model_id.endswith(":free"):
        return "extra-review"
    if model_id == MODEL_DEEPSEEK_PRO:
        return "ds-pro"
    if model_id.startswith(("deepseek/", "openrouter/deepseek/")):
        return "ds-task"
    if model_id == MODEL_ZAI_GLM_FLASH:
        return "zai-flash"
    if model_id.startswith("zai/"):
        return "zai-task"
    if model_id.startswith("minimax-code/"):
        return "minimax-task"
    if model_id == MODEL_GEMINI_PRO:
        return "gemini-pro"
    if model_id.startswith("google-antigravity/"):
        if "claude-opus" in model_id:
            return "ag-opus"
        if "claude-sonnet" in model_id:
            return "ag-sonnet"
        if "gpt-oss" in model_id:
            return "ag-gpt"
    if "flash-lite" in model_id:
        return "compactor"
    if "flash" in model_id:
        return "qa-verifier" if task_type == TaskType.STRONG_REVIEW and risk_level == RiskLevel.LOW else "task"
    if model_id.startswith("anthropic/"):
        return "reviewer"
    return "task"

def model_to_provider(model_id: str) -> str:
    """Map model ID to canonical provider name.

    All google-antigravity families share one credential (the local masking
    sidecar), so they are a single provider for fallback-diversity purposes.
    """
    if model_id.startswith("openrouter/"):
        return "openrouter"
    if model_id.startswith("deepseek/"):
        return "deepseek"
    if model_id.startswith("zai/"):
        return "zai"
    if model_id.startswith("minimax-code/"):
        return "minimax"
    if model_id.startswith("chatgpt-web/"):
        return CHATGPT_WEB_PROVIDER
    if model_id.startswith("google-antigravity/"):
        return "google-antigravity"
    if "anthropic" in model_id:
        return "anthropic"
    if "openai" in model_id or "codex" in model_id:
        return "openai"
    if model_id.startswith("opencode-go/"):
        return "opencode-go"
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


@dataclass(frozen=True)
class _Rung:
    """One step of a routing ladder."""
    model: str
    available: bool
    reason: str
    promotion: bool = False
    cooldown: bool = False
    # False keeps a rung primary-only: it is taken when everything above it is out, but it
    # is never offered as the fallback of a cheaper rung (e.g. paid Anthropic in worker ladders).
    as_fallback: bool = True
    # Rungs that are interchangeable for the task share a `pace_group`; `_apply_pace_rules`
    # then orders them by the pace of the provider that would serve each one (and drops the
    # ones that are ahead of pace). None keeps the declared order, so capability remains the
    # primary axis and pacing only ever reorders equals.
    pace_group: Optional[str] = None


@dataclass(frozen=True)
class WindowPace:
    """How one subscription window is spending relative to its own clock."""
    provider: str
    window_id: str
    duration_hours: float
    hours_to_reset: float
    remaining_fraction: float
    used_fraction: float
    time_remaining_fraction: float
    pace_ratio: float
    behind_pace: bool
    ahead_of_pace: bool
    throttled: bool
    expiring: bool


def window_pace(provider: str, window_id: str, duration_hours: float, hours_to_reset: float,
                remaining_fraction: float, used_fraction: Optional[float] = None) -> WindowPace:
    """Pace one window: `pace_ratio` = remaining allowance / remaining time (1.0 = linear).

    < 1.0 is ahead of pace (it would run out before its reset), so once the window is at
    least PACE_THROTTLE_USED_FLOOR consumed it is `throttled` and its rungs are dropped.
    > 1.0 is behind pace: the allowance would expire unused, so `prefer_furthest_behind_pace`
    spends it first.  At or above SURPLUS_PACE_HEADROOM within SURPLUS_WINDOW_HOURS of the
    reset it is `expiring`, which outranks plain pace so nothing is left on the table.
    `used_fraction` is derived from `remaining_fraction` unless the report carries its own.
    """
    duration = duration_hours if duration_hours and duration_hours > 0 else 24.0
    time_remaining = min(1.0, max(PACE_MIN_TIME_FRACTION, hours_to_reset / duration))
    remaining = min(1.0, max(0.0, remaining_fraction))
    used = min(1.0, max(0.0, 1.0 - remaining if used_fraction is None else used_fraction))
    ratio = remaining / time_remaining
    return WindowPace(
        provider=provider,
        window_id=window_id,
        duration_hours=duration,
        hours_to_reset=max(0.0, hours_to_reset),
        remaining_fraction=remaining,
        used_fraction=used,
        time_remaining_fraction=time_remaining,
        pace_ratio=ratio,
        behind_pace=ratio >= 1.0 + PACE_PREFER_DELTA,
        ahead_of_pace=ratio <= 1.0 - PACE_PREFER_DELTA,
        throttled=ratio < PACE_THROTTLE_RATIO and used >= PACE_THROTTLE_USED_FLOOR,
        expiring=(ratio >= SURPLUS_PACE_HEADROOM
                  and 0.0 < hours_to_reset <= SURPLUS_WINDOW_HOURS
                  and remaining > 0.0),
    )


def prefer_furthest_behind_pace(paces: List[WindowPace]) -> Optional[WindowPace]:
    """The window to spend next: an expiring one first, then the furthest behind pace.

    Callers pass only the windows of models that are already capable of the task, so this is
    "the provider furthest behind pace among capable models" and never a capability decision.
    """
    if not paces:
        return None
    return max(paces, key=lambda pace: (pace.expiring, pace.pace_ratio, -pace.hours_to_reset))


def balance_provider_for(model: str) -> str:
    """Snapshot provider that holds `model`'s allowance. Antigravity splits by model family.

    Codex models map to 'openai-codex' (the snapshot provider) so quota checks
    and pacing hit the right provider entry.
    """
    if re.search(r"(?:^|[/._-])codex(?:$|[/._-])", model, re.IGNORECASE) or model.startswith("openai-codex/"):
        return CODEX_PROVIDER
    provider = model_to_provider(model)
    if provider == ANTIGRAVITY_PROVIDER:
        family = model.split("/", 1)[1] if "/" in model else ""
        if family.startswith("claude"):
            return AG_ANTHROPIC_PROVIDER
        if family.startswith("gpt"):
            return AG_OPENAI_PROVIDER
    return provider

def _pace_gate_rung(rung: _Rung, pace: Optional[WindowPace],
                    blocked_reason: Optional[str] = None) -> _Rung:
    """Close a rung whose provider is spent, exhausted by cache, or would be spent too early.

    Three hard exclusions apply to every rung, whether or not it shares a pace group:
    the exhaustion cache says the provider's reset is still ahead (operator 2026-09-25: three
    lanes were dispatched onto an exhausted Antigravity Opus window and died on a 429 within
    3 s), the window reports itself fully used, or it is ahead of pace once it is at least
    PACE_THROTTLE_USED_FLOOR consumed and would be exhausted before its reset. The ahead-of-pace
    hold-back exempts the terminal reserve rungs (`cooldown=True`): `_climb` reaches them only
    after every on-pace tier above is closed, so the choice there is between drawing the last
    of a window or failing the task outright.
    """
    if not rung.available:
        return rung
    if blocked_reason:
        return replace(rung, available=False, reason=f"{rung.reason} Blocked: {blocked_reason}.")
    if pace is None:
        return rung
    if pace.remaining_fraction <= 0.0 or pace.used_fraction >= 1.0:
        return replace(rung, available=False, reason=(
            f"{rung.reason} Blocked: {pace.provider} {pace.window_id} is exhausted "
            f"({pace.used_fraction * 100:.0f}% used)."))
    if pace.throttled and not rung.cooldown:
        return replace(rung, available=False, reason=(
            f"{rung.reason} Throttled: {pace.provider} {pace.window_id} is ahead of pace "
            f"({pace.pace_ratio:.2f}x, {pace.used_fraction * 100:.0f}% used), so spending it now "
            "would exhaust it before its reset."))
    return rung


def _apply_pace_rules(rungs: List[_Rung], pace_of_model, blocked_for_model=None) -> List[_Rung]:
    """Gate every rung on its provider's exhaustion and pace, then order each pace group.

    The gate runs first and applies to all rungs: a rung whose provider is recorded as
    exhausted, whose window is fully used, or that is ahead of pace is closed, so the ladder
    climbs past it instead of dispatching work that would fail. Then each `pace_group` — a run
    of consecutive rungs that are interchangeable for the task — is ordered so an expiring
    window comes first (spend it or lose it) and the furthest behind pace follows. Rungs
    outside a group keep their declared order, so capability remains the primary axis and
    pacing never promotes a weaker model over a stronger one.
    """
    blocked_for_model = blocked_for_model or (lambda _model: None)
    ordered: List[_Rung] = []
    index = 0
    while index < len(rungs):
        group = rungs[index].pace_group
        if group is None:
            rung = rungs[index]
            ordered.append(_pace_gate_rung(rung, pace_of_model(rung.model), blocked_for_model(rung.model)))
            index += 1
            continue
        end = index
        while end < len(rungs) and rungs[end].pace_group == group:
            end += 1
        ranked = []
        for position, rung in enumerate(rungs[index:end]):
            pace = pace_of_model(rung.model)
            ranked.append((_pace_gate_rung(rung, pace, blocked_for_model(rung.model)), pace, position))
        ranked.sort(key=lambda entry: (
            # A promoted rung outranks everything else in its group: the account-level window
            # would otherwise expire unused, which no cheaper tier can compensate for.
            0 if entry[0].promotion else 1,
            0 if (entry[1] is not None and entry[1].expiring) else 1,
            -(entry[1].pace_ratio if entry[1] is not None else 1.0),
            entry[2],
        ))
        ordered.extend(rung for rung, _, _ in ranked)
        index = end
    return ordered


def _climb(
    rungs: List[_Rung],
    last_resort: _Rung,
    final_fallbacks: List[str],
    is_eligible: Optional[Any] = None,
) -> Tuple[_Rung, str]:
    """Pick the first available rung, and as its fallback the next available rung below it
    that may serve as a fallback and sits on another provider/credential. With no such rung,
    the ladder's last resort, then the first cross-provider entry of `final_fallbacks`, is used.
    All fallback candidates (including last_resort and final_fallbacks) must pass is_eligible."""
    eligible_check = is_eligible or (lambda _m: True)

    index = next((i for i, rung in enumerate(rungs) if rung.available), None)
    if index is None:
        if last_resort.available and eligible_check(last_resort.model):
            chosen = last_resort
        else:
            chosen_candidate = None
            for m in final_fallbacks:
                if eligible_check(m):
                    chosen_candidate = _Rung(m, True, "final fallback")
                    break
            if chosen_candidate is None:
                raise ValueError("no eligible model or fallback available in ladder")
            chosen = chosen_candidate
    else:
        chosen = rungs[index]

    provider = model_to_provider(chosen.model)
    below = [] if index is None else rungs[index + 1:]
    for rung in below:
        if rung.available and rung.as_fallback and model_to_provider(rung.model) != provider:
            if eligible_check(rung.model):
                return chosen, rung.model

    for model in (last_resort.model, *final_fallbacks):
        if model_to_provider(model) != provider and eligible_check(model):
            return chosen, model

    raise ValueError(f"no eligible cross-provider fallback for {chosen.model}")


class ResetAwareModelSelector:
    """
    Deterministic selector implementing:
      - Capability-first filtering
      - Reset-aware Codex promotion near expiration with surplus allowance
      - Anthropic preservation when reset is distant
      - Gemini 3.8 Flash as default executor but not sole review gate
      - Safe 429/cooldown/unknown handling
    """

    def __init__(self, snapshot: Optional[Any] = None, credentialed_providers: Optional[Set[str]] = None,
                 quota_snapshot: Optional[Any] = None, chatgpt_web_bridge: Optional[bool] = None):
        if snapshot is not None and isinstance(snapshot, BalanceAdapter):
            self.snapshot = snapshot.fetch_snapshot()
        elif snapshot is not None and hasattr(snapshot, "to_normalized"):
            self.snapshot = snapshot.to_normalized()
        else:
            self.snapshot = snapshot
        # Credential-gated providers (Z.AI, MiniMax Code); detected from env and the veyyon
        # auth stores unless the caller pins the set (tests, dry runs).
        self.credentialed_providers: Set[str] = (
            detect_credentialed_providers() if credentialed_providers is None else set(credentialed_providers)
        )
        # Exhaustion cache. `veyyon usage` is far too slow to read before every dispatch
        # (operator 2026-09-25), so eligibility is read from a local snapshot that a 429 or a
        # periodic usage read refreshes. Tests inject their own snapshot here.
        self._quota_snapshot = quota_snapshot
        # chatgpt-web readiness is a port probe, cached for the selector's lifetime; callers
        # (tests, dry runs) may pin it either way.
        self._chatgpt_web_bridge = chatgpt_web_bridge

    def quota_snapshot(self):
        """The exhaustion cache, loaded once per selector. A missing file yields an empty one."""
        if self._quota_snapshot is None:
            self._quota_snapshot = load_quota_snapshot()
        return self._quota_snapshot

    def chatgpt_web_bridge_up(self) -> bool:
        """Bridge-health precondition for every chatgpt-web rung, probed at most once."""
        if self._chatgpt_web_bridge is None:
            self._chatgpt_web_bridge = chatgpt_web_bridge_available()
        return self._chatgpt_web_bridge

    def provider_exhaustion_reason(self, model: str) -> Optional[str]:
        """Why `model`'s provider is ineligible, or None when it may be used.

        A provider whose recorded reset time is still in the future is skipped entirely:
        three lanes were dispatched onto an exhausted Antigravity Opus window and died on a
        429 within 3 s. Once that reset time passes the same provider is eligible again with
        no further bookkeeping.
        """
        snapshot = self.quota_snapshot()
        if snapshot is None:
            return None
        provider = balance_provider_for(model)
        if snapshot.is_eligible(provider):
            return None
        until = snapshot.provider_exhausted_until(provider)
        return (f"{provider} is exhausted until {until.isoformat()}" if until is not None
                else f"{provider} is exhausted")

    def set_snapshot(self, snapshot: Any):
        if snapshot is not None and isinstance(snapshot, BalanceAdapter):
            self.snapshot = snapshot.fetch_snapshot()
        elif snapshot is not None and hasattr(snapshot, "to_normalized"):
            self.snapshot = snapshot.to_normalized()
        else:
            self.snapshot = snapshot
    def pace_by_provider(self) -> Dict[str, WindowPace]:
        """Governing pace per provider: its most constrained window (lowest pace ratio)."""
        paces: Dict[str, WindowPace] = {}
        for name, provider in (getattr(self.snapshot, "providers", {}) or {}).items():
            windows = [
                window_pace(name, window.id, window.duration_seconds / 3600.0,
                            window.seconds_to_reset / 3600.0, window.remaining_fraction,
                            window.used_fraction)
                for window in provider.windows
                if window.duration_seconds and window.duration_seconds > 0
                and not window.is_cooldown and window.status != "exhausted"
            ]
            if not windows:
                windows = [
                    window_pace(name, window.id, window.duration_seconds / 3600.0,
                                window.seconds_to_reset / 3600.0, window.remaining_fraction,
                                window.used_fraction)
                    for window in provider.windows
                    if window.duration_seconds and window.duration_seconds > 0
                ]
            if windows:
                paces[name] = min(windows, key=lambda pace: (pace.pace_ratio, pace.hours_to_reset))
        return paces

    def pace_of_provider(self, provider: str) -> Optional[WindowPace]:
        return self.pace_by_provider().get(provider)

    def pace_of_model(self, model: str) -> Optional[WindowPace]:
        """Pace of the window that actually holds `model`'s allowance."""
        return self.pace_by_provider().get(balance_provider_for(model))

    def go_window_lanes(self, model: str) -> Dict[str, float]:
        """Whole Go lanes of `model` left in each rolling window, given observed spend."""
        return {window: self._go_lanes_in(model, window) for window in GO_WINDOW_FRACTION}

    def go_lanes_remaining(self, model: str, window_id: str = GO_PACE_WINDOW) -> float:
        """Whole lanes of `model` that fit its pacing window right now.

        The cap is the model's own monthly cap scaled to the window (5h 20%, week 50%,
        month 100%); the spend is the provider-level USD veyyon observed, charged in full to
        this model, which makes the result a lower bound. space-bunny-free is uncapped.
        """
        return self._go_lanes_in(model, window_id)

    def _go_lanes_in(self, model: str, window_id: str) -> float:
        cap = GO_MONTHLY_CAP_USD.get(model)
        cost = GO_LANE_COST_USD.get(model)
        if cap is None or not cost:
            return float("inf")
        provider = (getattr(self.snapshot, "providers", {}) or {}).get(OPENCODE_GO_PROVIDER)
        window = next((w for w in getattr(provider, "windows", []) if w.id == window_id), None)
        spent = 0.0
        if window is not None and window.total_limit is not None and window.remaining_units is not None:
            spent = max(0.0, window.total_limit - window.remaining_units)
        return max(0.0, (cap * GO_WINDOW_FRACTION[window_id] - spent) / cost)

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

        # Pro weekly allowance for Codex (the 7d window that paces and promotes Codex Astra)
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
        diff_lines: Optional[int] = None,
    ) -> RoutingRecommendation:
        """
        Determines the optimal model based on capability, context tokens, risk, and quota metrics.

        Every lane is an ordered ladder of rungs. The first available rung is the primary;
        the fallback is the next available rung that is allowed as a fallback and sits on a
        different provider/credential, so one outage never takes out both.
        """
        # 1. Inspect provider metrics
        google_meta = self.evaluate_provider("google-antigravity")
        anthropic_meta = self.evaluate_provider("anthropic")
        codex_meta = self.evaluate_provider("openai-codex")
        ag_anthropic_meta = self.evaluate_provider(AG_ANTHROPIC_PROVIDER)
        ag_openai_meta = self.evaluate_provider(AG_OPENAI_PROVIDER)
        deepseek_meta = self.evaluate_provider("deepseek")
        go_meta = self.evaluate_provider("opencode-go")
        chatgpt_web_up = self.chatgpt_web_bridge_up()

        provider_statuses = {
            "google-antigravity": google_meta["status"],
            AG_ANTHROPIC_PROVIDER: ag_anthropic_meta["status"],
            AG_OPENAI_PROVIDER: ag_openai_meta["status"],
            "anthropic": anthropic_meta["status"],
            "openai-codex": codex_meta["status"],
            "deepseek": deepseek_meta["status"],
            "opencode-go": go_meta["status"],
            "xai-oauth": "dormant",
            CHATGPT_WEB_PROVIDER: "ok" if chatgpt_web_up else "down",
        }

        # 2. Codex pro 7d window (Spark excluded): promote while it would expire unused,
        # hold back while it is being spent ahead of pace. Reported Codex metrics come from
        # this pro window, not from whichever account is the default lane (a free account
        # at 0% used would otherwise mask a nearly spent pro window).
        codex_pro_remaining = codex_meta.get("pro_remaining", codex_meta["remaining_fraction"])
        codex_pro_hrs = codex_meta.get("pro_hours_to_reset", codex_meta["hours_to_reset"])
        codex_pro_headroom = codex_meta.get("pro_headroom", codex_meta["burn_headroom"])
        codex_pro_used = 1.0 - codex_pro_remaining

        codex_near_reset_surplus = (
            allow_codex_promotion
            and codex_meta["is_available"]
            and codex_pro_hrs <= SURPLUS_WINDOW_HOURS
            and codex_pro_headroom >= SURPLUS_PACE_HEADROOM
        )
        codex_throttled = (
            codex_meta["is_available"]
            and codex_pro_headroom < CODEX_PACE_MIN_HEADROOM
            and codex_pro_used >= CODEX_PACE_USED_FLOOR
        )
        codex_usable = codex_meta["is_available"] and not codex_throttled

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

        # 3. Direct Anthropic 7d window is the orchestrator's budget. Slack exists only while
        # it runs behind pace AND its tightest window (normally 5h) is below the used cap;
        # stale or unknown data never counts as slack.
        anthropic_headroom = anthropic_meta.get("cycle_headroom", 1.0)
        anthropic_bottleneck_used = 1.0 - anthropic_meta["remaining_fraction"]
        anthropic_worker_ok = (
            anthropic_meta["status"] == "ok"
            and anthropic_headroom >= ANTHROPIC_WORKER_MIN_HEADROOM
            and anthropic_bottleneck_used < ANTHROPIC_BOTTLENECK_MAX_USED
        )

        # 4. Credential-gated and pay-per-token tiers. A credentialed provider still yields
        # to a cooldown or rate limit the snapshot reports for it.
        credentialed = self.credentialed_providers
        zai_ok = ZAI_PROVIDER in credentialed and self.evaluate_provider(ZAI_PROVIDER)["is_available"]
        minimax_ok = MINIMAX_PROVIDER in credentialed and self.evaluate_provider(MINIMAX_PROVIDER)["is_available"]
        deepseek_ok = deepseek_meta["is_available"]
        google_ok = google_meta["is_available"]

        # OpenCode Go is a $10/month subscription whose per-model monthly caps are enforced
        # through 5h/week/month windows (GO_MONTHLY_CAP_USD / GO_WINDOW_FRACTION).  A paid Go
        # model is offered only while a whole lane of ITS measured cost still fits the pacing
        # window, so glm-5.3 keeps its $15 cap for high-value review instead of leading every
        # ladder.  space-bunny-free is free and uncapped, so it only needs the credential.
        go_credentialed = OPENCODE_GO_PROVIDER in credentialed
        go_available = go_credentialed and go_meta["is_available"]
        go_bunny_ok = go_credentialed
        go_lanes = {model: self.go_lanes_remaining(model) for model in GO_MONTHLY_CAP_USD}
        go_pace = self.pace_of_provider(OPENCODE_GO_PROVIDER)
        go_headroom = go_pace.pace_ratio if go_pace else 1.0
        go_behind_pace = bool(go_pace and go_pace.behind_pace)

        def go_ok(model: str) -> bool:
            """Credentialed, provider healthy, and a whole lane of it left this week."""
            return go_available and go_lanes.get(model, 0.0) >= 1.0


        quota_metrics = {
            "google_remaining": google_meta["remaining_fraction"],
            "google_reset_hrs": google_meta["hours_to_reset"],
            "ag_anthropic_remaining": ag_anthropic_meta["remaining_fraction"],
            "ag_anthropic_reset_hrs": ag_anthropic_meta["hours_to_reset"],
            "anthropic_remaining": anthropic_meta["remaining_fraction"],
            "anthropic_reset_hrs": anthropic_meta["hours_to_reset"],
            "anthropic_headroom": anthropic_headroom,
            "anthropic_bottleneck_used": anthropic_bottleneck_used,
            "anthropic_orchestrator_reserve": not anthropic_worker_ok,
            "codex_remaining": codex_pro_remaining,
            "codex_reset_hrs": codex_pro_hrs,
            "codex_burn_headroom": codex_pro_headroom,
            "codex_pro_headroom": codex_pro_headroom,
            "codex_pro_throttled": codex_throttled,
            "codex_lane_remaining": codex_meta["remaining_fraction"],
            "codex_lane_reset_hrs": codex_meta["hours_to_reset"],
            "zai_available": zai_ok,
            "minimax_available": minimax_ok,
            "go_available": go_available,
            "go_bunny_ok": go_bunny_ok,
            "go_headroom": go_headroom,
            "go_behind_pace": go_behind_pace,
            "go_pace_window": GO_PACE_WINDOW,
            "go_lanes_remaining": go_lanes,
            "go_window_lanes": {model: self.go_window_lanes(model) for model in GO_MONTHLY_CAP_USD},
            "chatgpt_web_bridge_up": chatgpt_web_up,
        }

        # 5. Rework-aware routing: force a strong first pass for critical domains, large diffs (>250 lines) or after rework.
        HIGH_RISK_DOMAINS = {"state_machine", "auth", "money", "concurrency", "migration", "schema", "invariants"}
        is_rework_critical = (
            risk_level == RiskLevel.HIGH
            or rework_count >= 1
            or (domain_tags is not None and any(t in HIGH_RISK_DOMAINS for t in domain_tags))
            or (diff_lines is not None and diff_lines > 250)
        )
        is_first_pass = (rework_count <= 0)
        has_arch_tag = bool(domain_tags and any(t.lower() in {"architecture", "architectural", "cross-cutting"} for t in domain_tags))
        has_money_or_migration = bool(domain_tags and any(
            t.lower() in {"money", "billing", "wallet", "ledger", "payment", "stripe", "migration", "migrations", "alembic"}
            for t in domain_tags
        ))
        is_super_hard_review = (
            task_type == TaskType.STRONG_REVIEW
            and (
                has_arch_tag
                or (has_money_or_migration and is_first_pass)
            )
        )
        is_ag_opus_exhausted = (
            self.provider_exhaustion_reason(MODEL_AG_CLAUDE_OPUS) is not None
            or (ag_anthropic_meta["status"] == "ok" and not ag_claude_ok)
        )
        evidence_packet_required = risk_level in (RiskLevel.MEDIUM, RiskLevel.HIGH) or task_type == TaskType.STRONG_REVIEW

        def codex_promoted(model: str, label: str, group: str) -> _Rung:
            """Promote an expiring Codex window so its allowance is spent instead of lost."""
            return _Rung(
                model, codex_near_reset_surplus,
                f"Codex pro weekly window resets in {codex_pro_hrs:.1f}h at {codex_pro_headroom:.2f}x pace "
                f"headroom; promoted {label} to spend allowance that would otherwise expire.",
                promotion=True, pace_group=group,
            )

        # A promoted rung leads its pace group, so an expiring Codex window is spent ahead of the
        # interchangeable cheap tiers that share that group, and every other day loses to them.
        codex_promo = codex_promoted(MODEL_CODEX_ASTRA, "Codex Astra", PACE_GROUP_STRONG)
        astra_on_pace = _Rung(
            MODEL_CODEX_ASTRA, codex_usable,
            f"Codex Astra medium ({codex_pro_headroom:.2f}x pace headroom).",
            pace_group=PACE_GROUP_STRONG,
        )
        astra_emergency = _Rung(
            MODEL_CODEX_ASTRA, codex_meta["is_available"],
            "only Codex ahead of pace remains; spending its emergency reserve.",
            cooldown=True, pace_group=PACE_GROUP_STRONG,
        )
        codex_fast_promo = codex_promoted(MODEL_CODEX_FAST, "Codex Fast", PACE_GROUP_EXEC)
        ag_opus = _Rung(
            MODEL_AG_CLAUDE_OPUS, ag_claude_ok,
            f"Antigravity Claude Opus 4.6 (free daily window, expires before any weekly window). {ag_claude_note}.",
            pace_group=PACE_GROUP_STRONG,
        )
        opus_55_available = anthropic_meta["is_available"] and (self.provider_exhaustion_reason(MODEL_CLAUDE_OPUS_55) is None)
        opus_55 = _Rung(
            MODEL_CLAUDE_OPUS_55, opus_55_available,
            "Claude Opus 5.5 (anthropic/claude-opus-5-5:high): reserved strictly for super-hard work "
            "(money/billing/ledger/migration first-pass reviews, cross-cutting architectural changes, or ag-opus exhaustion; "
            'operator ruling 2026-09-26 ~13:25Z: "I mean only hard, super hard work, right? Don\'t move everything in there").',
        )
        glm = _Rung(MODEL_ZAI_GLM, zai_ok, "Z.AI GLM-5.3 (credentialed Coding Plan).")
        deepseek_pro = _Rung(MODEL_DEEPSEEK_PRO, deepseek_ok, "DeepSeek V4 Pro (pay-per-token, 1M context).")
        # Worker ladders reach paid Anthropic only when every cheap tier above is out, only
        # on slack behind pace, and never as a fallback.
        fable_last_resort = _Rung(
            MODEL_CLAUDE_FABLE, anthropic_worker_ok,
            f"every cheap tier is unavailable; last resort on Anthropic slack ({anthropic_headroom:.2f}x behind pace, "
            "orchestrator reserve untouched).",
            cooldown=True, as_fallback=False,
        )

        # OpenCode Go rungs, allowance-gated and pace-ordered.  glm-5.3-flash is the Go
        # workhorse ($60 cap, ~144 lanes/month); glm-5.3 is the rare precision reviewer ($15
        # cap, ~4 lanes/month, and one median lane already consumes most of a 5h window, so it
        # can serve at most one lane per 5h); GPT-6 Luna is the cheap bulk filler ($15 cap).
        # space-bunny-free is free and unlimited and leads wherever it is good enough.
        go_bunny = _Rung(
            MODEL_GO_BUNNY, go_bunny_ok,
            "OpenCode Go space-bunny-free (free, uncapped, 1M ctx, multimodal).",
            pace_group=PACE_GROUP_GO_FREE,
        )
        go_glm53_flash = _Rung(
            MODEL_GO_GLM53_FLASH, go_ok(MODEL_GO_GLM53_FLASH),
            f"OpenCode Go GLM-5.3-Flash (Go workhorse: $60 cap, ~144 lanes/month, "
            f"{go_lanes[MODEL_GO_GLM53_FLASH]:.0f} lanes left this week).",
            pace_group=PACE_GROUP_GO_PAID,
        )
        go_gpt6_luna = _Rung(
            MODEL_GO_GPT6_LUNA, go_ok(MODEL_GO_GPT6_LUNA),
            f"OpenCode Go GPT-6 Luna (cheap bulk filler: $15 cap, ~78 lanes/month, "
            f"{go_lanes[MODEL_GO_GPT6_LUNA]:.0f} lanes left this week).",
            pace_group=PACE_GROUP_GO_PAID,
        )
        go_glm53 = _Rung(
            MODEL_GO_GLM53, go_ok(MODEL_GO_GLM53),
            f"OpenCode Go GLM-5.3 (rare precision reviewer: $15 cap, ~4 lanes/month, "
            f"{go_lanes[MODEL_GO_GLM53]:.0f} lanes left this week).",
            pace_group=PACE_GROUP_GO_PAID,
        )

        # ChatGPT Web via the bridge: gated on the port probe above instead of an allowance,
        # because there is no usage row to read. Cross-family to both writer families, so it
        # gates a critical review without the intra-family self-preference the Chinese
        # reviewers are ordered around.
        chatgpt_web = _Rung(
            MODEL_CHATGPT_WEB, chatgpt_web_up,
            "ChatGPT web via the loopback bridge ("
            + ("listening" if chatgpt_web_up else "not listening")
            + f" on {CHATGPT_WEB_BRIDGE_HOST}:{CHATGPT_WEB_BRIDGE_PORT}): high-headroom "
            "subscription tier, cross-family to Gemini and GLM writers.",
        )

        if context_tokens > 180000 or task_type == TaskType.DEEP_CONTEXT:
            # CASE A: DEEP CONTEXT (> 180k tokens, or a DEEP_CONTEXT task). Every 1M-context
            # tier in cost order: free first, then the Ultra daily window, then the cheap Go
            # models, then pay-per-token DeepSeek V4 Pro, then MiniMax-M3 for bulk and
            # low-risk reads only. Codex and Opus follow those; Fable is reached only when all
            # of them are out. Implementation, review, reasoning and any rework or high-risk
            # deep-context read skip MiniMax and climb on.
            label = f"Deep context ({context_tokens} tokens)"
            rungs = [
                go_bunny,
                chatgpt_web,
                _Rung(MODEL_GEMINI_PRO, google_ok and task_type != TaskType.STRONG_REVIEW,
                      "Gemini 3.1 Pro (1M-token window; never a reviewer, since Gemini must not "
                      "review a Gemini-authored diff)."),
                go_glm53_flash,
                go_gpt6_luna,
                _Rung(MODEL_DEEPSEEK_PRO, deepseek_ok, "DeepSeek V4 Pro (pay-per-token, 1M context).", cooldown=True),
                _Rung(MODEL_MINIMAX_M3,
                      minimax_ok and (task_type == TaskType.TINY_TASK
                                      or (task_type == TaskType.DEEP_CONTEXT and not is_rework_critical)),
                      "MiniMax-M3 (1M-token window, credentialed; low-risk deep-context/bulk overflow).", cooldown=True),
                glm,
                ag_opus,
                codex_promo,
                astra_on_pace,
                _Rung(MODEL_CODEX_FAST, codex_usable, "1M-context tiers unavailable; Codex Fast (400k window, on pace).",
                      cooldown=True),
                astra_emergency,
                _Rung(MODEL_CODEX_FAST, codex_meta["is_available"],
                      "only Codex ahead of pace holds this context; spending its emergency reserve.", cooldown=True),
                _Rung(MODEL_CLAUDE_FABLE, anthropic_worker_ok,
                      f"every cheap tier whose window holds {context_tokens} tokens is unavailable; last resort on "
                      f"Anthropic slack ({anthropic_headroom:.2f}x behind pace, orchestrator reserve untouched).",
                      cooldown=True, as_fallback=False),
            ]
            last_resort = _Rung(MODEL_DEEPSEEK_PRO, True, "every deep-context tier unavailable; pay-per-token DeepSeek V4 Pro.", cooldown=True)
            # High-risk, rework and money work never falls back to a Flash tier (as in B2).
            final_fallbacks = ([MODEL_DEEPSEEK_PRO, MODEL_CODEX_FAST] if is_rework_critical
                               else [MODEL_OR_DEEPSEEK_FLASH])

        elif task_type == TaskType.STRONG_REVIEW and is_rework_critical:
            # CASE B1: HIGH-RISK REVIEW.
            # Operator ruling (2026-09-26 ~13:25Z): "I mean only hard, super hard work, right?
            # Don't move everything in there". Opus 5.5 (the `reviewer` lane, anthropic/claude-opus-5-5:high)
            # is reserved strictly for super-hard work: money, billing, ledger, migration first-pass reviews,
            # cross-cutting architectural changes, or when ag-opus quota is exhausted.
            # Routine gating reviews default to ag-opus (Opus 4.6 on free Antigravity daily window).
            if is_super_hard_review:
                label = "Super-hard review (Opus 5.5)"
                rungs = [
                    opus_55,
                    ag_opus,
                    chatgpt_web,
                    codex_promo,
                    go_glm53,
                    _Rung(MODEL_CLAUDE_FABLE, anthropic_worker_ok,
                          f"Claude Fable: the Anthropic weekly window runs {anthropic_headroom:.2f}x behind pace, "
                          "so slack beyond the orchestrator's share is spent on review."),
                    astra_on_pace,
                    _Rung(MODEL_CLAUDE_FABLE, anthropic_meta["is_available"],
                          "no review allowance left elsewhere; drawing on the Anthropic orchestrator reserve.",
                          cooldown=True, as_fallback=False),
                    astra_emergency,
                    _Rung(MODEL_DEEPSEEK_PRO, deepseek_ok, "all strong reviewers unavailable; emergency DeepSeek V4 Pro.", cooldown=True),
                ]
                last_resort = _Rung(MODEL_DEEPSEEK_PRO, True, "all strong models unavailable or in cooldown; pay-per-token DeepSeek V4 Pro.", cooldown=True)
                final_fallbacks = [MODEL_CLAUDE_OPUS_55, MODEL_AG_CLAUDE_OPUS, MODEL_CODEX_ASTRA, MODEL_DEEPSEEK_PRO]
            elif is_ag_opus_exhausted:
                # ag-opus exhaustion fallback for routine review: escalates to reviewer (Opus 5.5) on slack,
                # then astra_on_pace, then drawing on the orchestrator reserve (never Flash).
                label = "High-risk review (ag-opus exhausted fallback)"
                rungs = [
                    chatgpt_web,
                    codex_promo,
                    _Rung(MODEL_CLAUDE_OPUS_55, opus_55_available and anthropic_worker_ok,
                          "Claude Opus 5.5: ag-opus exhausted; drawing on Anthropic slack for high-risk review."),
                    go_glm53,
                    astra_on_pace,
                    _Rung(MODEL_CLAUDE_FABLE, anthropic_meta["is_available"],
                          "no review allowance left elsewhere; drawing on the Anthropic orchestrator reserve.",
                          cooldown=True, as_fallback=False),
                    astra_emergency,
                    _Rung(MODEL_DEEPSEEK_PRO, deepseek_ok, "all strong reviewers unavailable; emergency DeepSeek V4 Pro.", cooldown=True),
                ]
                last_resort = _Rung(MODEL_DEEPSEEK_PRO, True, "all strong models unavailable or in cooldown; pay-per-token DeepSeek V4 Pro.", cooldown=True)
                final_fallbacks = [MODEL_CLAUDE_OPUS_55, MODEL_CODEX_ASTRA, MODEL_DEEPSEEK_PRO]
            else:
                label = "High-risk review"
                rungs = [
                    chatgpt_web,
                    codex_promo,
                    ag_opus,
                    go_glm53,
                    _Rung(MODEL_CLAUDE_FABLE, anthropic_worker_ok,
                          f"Claude Fable: the Anthropic weekly window runs {anthropic_headroom:.2f}x behind pace, "
                          "so slack beyond the orchestrator's share is spent on review."),
                    astra_on_pace,
                    _Rung(MODEL_CLAUDE_FABLE, anthropic_meta["is_available"],
                          "no review allowance left elsewhere; drawing on the Anthropic orchestrator reserve.",
                          cooldown=True, as_fallback=False),
                    astra_emergency,
                    _Rung(MODEL_DEEPSEEK_PRO, deepseek_ok, "all strong reviewers unavailable; emergency DeepSeek V4 Pro.", cooldown=True),
                ]
                last_resort = _Rung(MODEL_DEEPSEEK_PRO, True, "all strong models unavailable or in cooldown; pay-per-token DeepSeek V4 Pro.", cooldown=True)
                final_fallbacks = [MODEL_CODEX_ASTRA, MODEL_DEEPSEEK_PRO]
        elif is_rework_critical and task_type in (TaskType.ROUTINE_EXECUTION, TaskType.DEEP_REASONING):
            # CASE B2: HIGH-RISK WORKER (implementation first pass, deep reasoning): the Go
            # workhorse first, then ChatGPT web while its bridge is up — a hard
            # implementation/reasoning tier ahead of the scarce and paid lanes — then Z.AI
            # GLM-5.3, the rare Go precision reviewer, and pay-per-token DeepSeek V4 Pro.
            # Codex follows them and its expiring surplus is promoted once the cheaper tiers
            # are out; Fable stays the last resort. Writers here are GLM-5.3 or DeepSeek V4
            # Pro, never Opus, and Gemini Flash is not a high-risk writer either.
            label = ("High-risk implementation first pass" if task_type == TaskType.ROUTINE_EXECUTION
                     else "High-risk deep reasoning")
            rungs = [
                go_glm53_flash,
                chatgpt_web,
                glm,
                go_glm53,
                deepseek_pro,
                codex_promo,
                astra_on_pace,
                astra_emergency,
                fable_last_resort,
            ]
            last_resort = _Rung(MODEL_DEEPSEEK_PRO, True, "all worker tiers unavailable; pay-per-token DeepSeek V4 Pro.", cooldown=True)
            final_fallbacks = [MODEL_CODEX_ASTRA, MODEL_DEEPSEEK_PRO]

        elif task_type == TaskType.STRONG_REVIEW and risk_level == RiskLevel.MEDIUM:
            # CASE C: MEDIUM-RISK REVIEW — standard diffs are reviewed by the cross-family
            # Chinese models (the go-review chain: GLM-5.3, else Qwen3.8 Max, else
            # GLM-5.3-Flash), with ChatGPT web as the overflow while Go is limited. Never
            # paid Anthropic or Antigravity Opus, and never Gemini, which would review a
            # Gemini-authored diff. Codex, Z.AI and DeepSeek follow.
            label = "Medium-risk review"
            rungs = [
                go_glm53,
                chatgpt_web,
                codex_promo,
                astra_on_pace,
                glm,
                deepseek_pro,
                _Rung(MODEL_DEEPSEEK_FLASH, deepseek_ok, "overflow to DeepSeek V4.1 Flash.", cooldown=True),
            ]
            last_resort = _Rung(MODEL_OR_DEEPSEEK_FLASH, True, "all review tiers unavailable; OpenRouter DeepSeek Flash.", cooldown=True)
            final_fallbacks = [MODEL_DEEPSEEK_FLASH]

        elif task_type == TaskType.STRONG_REVIEW:
            # CASE D: LOW-RISK REVIEW — the free Go model and the cheap Go workhorse are
            # enough; ChatGPT web and DeepSeek are the overflow. Never Gemini.
            label = "Low-risk review"
            rungs = [
                go_bunny,
                go_glm53_flash,
                chatgpt_web,
                _Rung(MODEL_DEEPSEEK_FLASH, deepseek_ok, "Go and the bridge unavailable; DeepSeek V4.1 Flash.", cooldown=True),
            ]
            last_resort = _Rung(MODEL_OR_DEEPSEEK_FLASH, True, "OpenRouter DeepSeek Flash.", cooldown=True)
            final_fallbacks = [MODEL_DEEPSEEK_FLASH]

        elif task_type == TaskType.DEEP_REASONING:
            # CASE E: DEEP REASONING (LOW and MEDIUM risk; HIGH risk routes via CASE B2 high-risk worker ladder).
            # Leads with ChatGPT web while its bridge is up (hard reasoning ahead of the scarce
            # Go precision model), then opencode-go GLM-5.3, then DeepSeek V4 Pro, or promoted
            # Codex when its window is expiring.
            label = "Deep reasoning"
            band_group = PACE_GROUP_EXEC if risk_level == RiskLevel.LOW else PACE_GROUP_STRONG
            flash_rung = _Rung(
                MODEL_GEMINI_FLASH, google_ok,
                "abundant Gemini 3.8 Flash; direct Anthropic reserved for the orchestrator.",
                pace_group=band_group,
            )
            codex_rung = codex_promoted(MODEL_CODEX_ASTRA, "Codex Astra", band_group)
            if codex_rung.promotion:
                band = [codex_rung, deepseek_pro, flash_rung]
            else:
                band = [deepseek_pro, flash_rung]
            rungs = [
                chatgpt_web,
                go_glm53,
                *band,
                go_glm53_flash,
                glm,
                astra_on_pace,
                _Rung(MODEL_DEEPSEEK_FLASH, deepseek_ok, "Gemini and Codex unavailable; DeepSeek V4.1 Flash overflow.", cooldown=True),
            ]
            last_resort = _Rung(MODEL_OR_DEEPSEEK_FLASH, True, "all reasoning tiers unavailable; OpenRouter DeepSeek Flash.", cooldown=True)
            final_fallbacks = [MODEL_DEEPSEEK_FLASH]

        elif task_type == TaskType.TINY_TASK:
            # CASE F: TINY TASK / BULK TRIAGE (compaction, commits, classification).
            label = "Lightweight / bulk triage task"
            rungs = [
                go_bunny,
                go_gpt6_luna,
                go_glm53_flash,
                _Rung(MODEL_GEMINI_LITE, google_ok, "Gemini 3.1 Flash Lite."),
                _Rung(MODEL_ZAI_GLM_FLASH, zai_ok, "Z.AI GLM-5.3-Flash (credentialed).", cooldown=True),
                _Rung(MODEL_MINIMAX_M3, minimax_ok, "MiniMax-M3 (credentialed, bulk/triage only).", cooldown=True),
                _Rung(MODEL_DEEPSEEK_FLASH, deepseek_ok, "DeepSeek V4.1 Flash.", cooldown=True),
            ]
            last_resort = _Rung(MODEL_OR_DEEPSEEK_FLASH, True, "OpenRouter DeepSeek Flash.", cooldown=True)
            final_fallbacks = [MODEL_DEEPSEEK_FLASH]

        else:
            # CASE G: ROUTINE EXECUTION (low/medium implementation, mapping, routine QA). The
            # free Go model first, then the primary abundant Antigravity lane, then the paid Go
            # workers and Z.AI overflow. Codex follows them: on pace it is the last subscription
            # lane, and with its window about to expire the promoted rung leads the band instead
            # of letting the allowance go to waste.
            label = "Routine execution"
            rungs = [
                go_bunny,
                _Rung(MODEL_GEMINI_FLASH, google_ok,
                      "primary abundant execution lane: Gemini 3.8 Flash (Ultra daily allowance).",
                      as_fallback=False, pace_group=PACE_GROUP_EXEC),
                codex_fast_promo,
                go_glm53_flash,
                go_gpt6_luna,
                _Rung(MODEL_ZAI_GLM, zai_ok, "overflow to Z.AI GLM-5.3 (credentialed).", cooldown=True),
                _Rung(MODEL_CODEX_FAST, codex_usable,
                      "Google Antigravity in cooldown; Codex Fast (subscription headroom).", cooldown=True, as_fallback=False),
                _Rung(MODEL_DEEPSEEK_FLASH, deepseek_ok, "overflow to DeepSeek V4.1 Flash.", cooldown=True),
            ]
            last_resort = _Rung(MODEL_OR_DEEPSEEK_FLASH, True, "all execution tiers unavailable; OpenRouter DeepSeek Flash.", cooldown=True)
            final_fallbacks = [MODEL_DEEPSEEK_FLASH]


        # Pacing first: inside each capability group it promotes the provider that is furthest
        # behind pace (or about to expire) and closes the ones that are ahead of pace, so no
        # window is exhausted before its reset and none expires unspent. Then every ladder
        # drops a rung whose verified window cannot hold the context, so GLM-5.3 (131,072
        # tokens) is never picked or offered as a fallback above its window.
        rungs = _apply_pace_rules(rungs, self.pace_of_model, self.provider_exhaustion_reason)
        rungs = [rung for rung in rungs if context_tokens <= VERIFIED_CONTEXT_WINDOWS[rung.model]]
        is_eligible_fn = lambda m: self.provider_exhaustion_reason(m) is None
        chosen, fallback_model = _climb(rungs, last_resort, final_fallbacks, is_eligible=is_eligible_fn)

        # Free OpenRouter second opinion for reviews: advisory only (1000 req/day free tier),
        # never a merge gate, never an approval, never a replacement for the review above.
        advisory_model = MODEL_OR_FREE_ADVISORY if task_type == TaskType.STRONG_REVIEW else None

        return RoutingRecommendation(
            task_type=task_type.value,
            risk_level=risk_level.value,
            context_tokens=context_tokens,
            selected_model=chosen.model,
            fallback_model=fallback_model,
            reasoning=f"{label}: {chosen.reason}",
            burn_headroom=codex_pro_headroom,
            promotion_applied=chosen.promotion,
            cooldown_fallback=chosen.cooldown,
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
        precomputed: Optional[RoutingRecommendation] = None,
    ) -> HarnessDispatchPacket:
        """
        Produce a complete, harness-agnostic dispatch packet ready for execution.
        Does not mutate any harness configuration or global state.

        `precomputed` carries a selection an upstream authority already committed
        (the coordinator commits one per evaluate_step). The coordinator is the single
        routing authority, so its exact selection is reused instead of running every
        provider ladder a second time; a plain dispatch() call keeps selecting on its own.
        """
        rec = (
            precomputed
            if isinstance(precomputed, RoutingRecommendation)
            else self.select_model(
                task_type=task_type,
                risk_level=risk_level,
                context_tokens=context_tokens,
                allow_codex_promotion=allow_codex_promotion,
                rework_count=rework_count,
                domain_tags=domain_tags,
            )
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
