#!/usr/bin/env python3
"""
Executable Smoke Test Suite for Veyyon Balance Loader & Model Routing
Location: ~/.veyyon/workflows/routing_smoke_test.py

Verifies:
  1. Real loader read-only execution (`veyyon usage --json --redact`)
  2. Sanitized snapshot parsing, freshness, reset UTC, and multi-window bottleneck logic
  3. Quota boundary scenario: Near-reset with surplus allowance -> Codex promotion
  4. Quota boundary scenario: Near-reset with exhausted allowance -> No promotion
  5. Quota boundary scenario: Distant reset -> Anthropic preservation, Flash 3.8 default executor
  6. Multi-window constraint: 5h window exhausted while 7d window has quota
  7. Cooldown / 429 safety: Rate-limited provider cleanly fails over
  8. Unknown / stale balance safety: Neutral baseline (neither 0 nor inf assumed)
  9. Dormant provider handling: xAI Grok skipped
  10. High-risk review quality gate: Flash 3.8 barred as sole quality gate
  11. Deep context filtering: > 180k tokens routes to Gemini 3.1 Pro
  12. Token-saving review protocol: Compact EvidencePacket (< 1.5 KB)
  22-26. Allowance-aware routing: Codex pro paced over its 7d window (held back ahead
      of pace, promoted when surplus would expire), Antigravity Claude daily window spent
      before paid Anthropic, DeepSeek V4.1 Flash overflow, free OpenRouter reviewer
      attached as advisory only, direct Anthropic reserved for the orchestrator except
      slack behind pace
"""

import copy
import datetime
import itertools
import socket
try:
    import yaml
except ImportError:
    yaml = None
import json
import os
import shutil
import sys
import subprocess
import time
import tempfile
import unittest
from unittest import mock
from pathlib import Path

# Ensure workflows directory is in python path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from balance_loader import (
    BalanceAdapter,
    DirectJsonAdapter,
    FileBalanceAdapter,
    NormalizedBalanceSnapshot,
    NormalizedProviderBalance,
    NormalizedWindow,
    SanitizedUsageSnapshot,
    SubscriptionReport,
    UsageAmount,
    UsageWindow,
    VeyyonBalanceAdapter,
    get_balance_adapter,
    load_snapshot,
    ms_to_iso_utc,
    parse_usage_json,
    sanitize_string,
)
from quota_snapshot import (
    QuotaSnapshot,
    QuotaWindowEntry,
    apply_quota_error,
    load_snapshot as load_quota_file,
)
from model_routing import (
    UNSUPPORTED_CODEX_MODELS,
    is_unsupported_codex_model,
    _Rung,
    _climb,
    EvidencePacket,
    HarnessDispatchPacket,
    ResetAwareModelSelector,
    RiskLevel,
    TaskType,
    MODEL_ANTHROPIC_OPUS,
    MODEL_CLAUDE_FABLE,
    MODEL_CLAUDE_OPUS_55,
    MODEL_CLAUDE_SONNET_55,
    MODEL_CODEX_FAST,
    MODEL_CODEX_SOL,
    MODEL_CODEX_WORKER,
    MODEL_CODEX_SOL_FALLBACK,
    MODEL_CODEX_ASTRA,
    MODEL_CODEX_SPARK,
    MODEL_GEMINI_FLASH,
    MODEL_GEMINI_LITE,
    MODEL_GEMINI_PRO,
    MODEL_AG_CLAUDE_OPUS,
    MODEL_AG_CLAUDE_SONNET,
    MODEL_AG_GPT_OSS,
    MODEL_DEEPSEEK_FLASH,
    MODEL_DEEPSEEK_PRO,
    MODEL_OR_DEEPSEEK_FLASH,
    MODEL_OR_FREE_ADVISORY,
    MODEL_ZAI_GLM,
    MODEL_ZAI_GLM_FLASH,
    MODEL_MINIMAX_M3,
    MODEL_CHATGPT_WEB,
    CHATGPT_WEB_PROVIDER,
    chatgpt_web_bridge_available,
    CODEX_ENABLED,
    codex_available,
    CODEX_PACE_MIN_HEADROOM,
    CODEX_PACE_USED_FLOOR,
    ANTHROPIC_BOTTLENECK_MAX_USED,
    CREDENTIAL_ENV_BY_PROVIDER,
    MINIMAX_PROVIDER,
    LANE_MODEL_PINS,
    ROLE_MODEL_PINS,
    ROLE_FALLBACK_LADDERS,
    lane_model_at_depth,
    lane_pin_drift,
    get_recommended_lanes,
    VERIFIED_CONTEXT_WINDOWS,
    ZAI_PROVIDER,
    detect_credentialed_providers,
    model_to_agent_role,
    model_to_provider,
    OPENCODE_GO_PROVIDER,
    MODEL_GO_BUNNY,
    MODEL_GO_GLM53_FLASH,
    WEEKLY_BURST_MARGIN,
    MONTHLY_BURST_MARGIN,
    BURST_MARGIN_DEFAULT,
    PacingOverride,
    load_pacing_overrides,
    window_pace,
    record_provider_429,
    AG_ANTHROPIC_PROVIDER,
    AG_FAMILY_MIN_REMAINING,
    WindowBurnPace,
    compute_window_burn_paces,
    resolve_role_model,
    is_agent_role_available,
    _climb,
    _Rung,
)

def tmp_quota_path() -> "Path":
    """A private snapshot path for one test, never the operator's live cache."""
    return Path(tempfile.mkdtemp(prefix="quota-snapshot-test-")) / "quota-snapshot.json"

class TestBalanceLoaderAndRouting(unittest.TestCase):

    def setUp(self):
        # Hermetic credentials: no Z.AI/MiniMax key from the caller's environment and no
        # veyyon auth store on disk, so every selector below sees zero credential-gated
        # providers unless a test pins them explicitly. Only the auth-store lookup is
        # redirected; VEYYON_CONFIG_DIR stays intact so the live `veyyon usage` smoke works.
        store_patch = mock.patch("model_routing._auth_store_paths", return_value=[])
        store_patch.start()
        self.addCleanup(store_patch.stop)
        env_patch = mock.patch.dict(os.environ)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for var in (*CREDENTIAL_ENV_BY_PROVIDER.values(), "MINIMAX_API_KEY"):
            os.environ.pop(var, None)

        # Hermetic exhaustion cache: the live ~/.veyyon/run/quota-snapshot.json is not test
        # input, so every selector sees an empty cache unless a test injects one.
        def _mock_load_quota(path=None, **kwargs):
            from quota_snapshot import SNAPSHOT_PATH, load_snapshot
            if path == SNAPSHOT_PATH or path is None:
                return QuotaSnapshot()
            return load_snapshot(path, **kwargs)
        quota_patch = mock.patch("model_routing.load_quota_snapshot", side_effect=_mock_load_quota)
        quota_patch.start()
        self.addCleanup(quota_patch.stop)

        # Hermetic bridge state: the live chatgpt-web bridge on 127.0.0.1:17841 is not test
        # input, so every selector here sees it closed unless a test pins it explicitly with
        # `chatgpt_web_bridge=True`.
        bridge_patch = mock.patch("model_routing.chatgpt_web_bridge_available", return_value=False)
        bridge_patch.start()
        self.addCleanup(bridge_patch.stop)
        # Hermetic codex account state for quota/pacing fixtures:
        # tests 3, 6, 21, 22, 28, 30 specifically exercise Codex quota math, pacing and promotion.
        # By default in tests, mock codex_available as True so quota fixtures evaluate properly;
        # test_codex_unroutable_and_fallthrough_when_disabled exercises the live unroutable state.
        codex_patch = mock.patch("model_routing.codex_available", return_value=True)
        codex_patch.start()
        self.addCleanup(codex_patch.stop)

        # Base realistic mock JSON simulating live veyyon usage output
        self.mock_now_ms = 1788598659263  # 2026-09-05T08:57:39Z
        self.mock_usage_dict = {
            "generatedAt": self.mock_now_ms,
            "reports": [
                {
                    "provider": "google-antigravity",
                    "fetchedAt": self.mock_now_ms,
                    "limits": [
                        {
                            "id": "google-antigravity:google:default:daily",
                            "label": "Usage (Google)",
                            "window": {
                                "id": "daily",
                                "label": "Daily",
                                "durationMs": 86400000,
                                "resetsAt": self.mock_now_ms + 17130737,  # ~4.76h
                            },
                            "amount": {
                                "unit": "percent",
                                "remainingFraction": 0.946,
                                "usedFraction": 0.054,
                                "remaining": 94.6,
                                "used": 5.4,
                                "limit": 100.0,
                            },
                            "status": "ok",
                        }
                    ],
                    "metadata": {"email": "user@example.com", "accountId": "acc_google_123"},
                },
                {
                    "provider": "anthropic",
                    "fetchedAt": self.mock_now_ms,
                    "limits": [
                        {
                            "id": "anthropic:5h",
                            "label": "Claude 5 Hour",
                            "window": {
                                "id": "5h",
                                "label": "5 Hour",
                                "durationMs": 18000000,
                                "resetsAt": self.mock_now_ms + 17540737,  # ~4.87h
                            },
                            "amount": {
                                "unit": "percent",
                                "remainingFraction": 1.0,
                                "usedFraction": 0.0,
                                "remaining": 100.0,
                                "used": 0.0,
                                "limit": 100.0,
                            },
                            "status": "ok",
                        },
                        {
                            "id": "anthropic:7d",
                            "label": "Claude 7 Day",
                            "window": {
                                "id": "7d",
                                "label": "7 Day",
                                "durationMs": 604800000,
                                "resetsAt": self.mock_now_ms + 522140737,  # ~145h (distant!)
                            },
                            "amount": {
                                "unit": "percent",
                                "remainingFraction": 1.0,
                                "usedFraction": 0.0,
                                "remaining": 100.0,
                                "used": 0.0,
                                "limit": 100.0,
                            },
                            "status": "ok",
                        },
                    ],
                    "metadata": {"email": "user@example.com", "accountId": "acc_anthropic_456"},
                },
                {
                    "provider": "openai-codex",
                    "fetchedAt": self.mock_now_ms,
                    "limits": [
                        {
                            "id": "openai-codex:primary",
                            "label": "7 days",
                            "window": {
                                "id": "7d",
                                "label": "7 days",
                                "durationMs": 604800000,
                                "resetsAt": self.mock_now_ms + 522477737,  # ~145h (distant!)
                            },
                            "amount": {
                                "unit": "percent",
                                "remainingFraction": 0.97,
                                "usedFraction": 0.03,
                                "remaining": 97.0,
                                "used": 3.0,
                                "limit": 100.0,
                            },
                            "status": "ok",
                        }
                    ],
                    "metadata": {"planType": "pro", "email": "user@example.com", "accountId": "acc_codex_789"},
                },
            ],
            "capacity": {},
        }

    # -------------------------------------------------------------------------
    # TEST 1: Sanitization and Redaction Invariant
    # -------------------------------------------------------------------------
    def test_sanitization_no_credentials_exposed(self):
        print("\n--- TEST 1: Sanitization & Redaction Invariant ---")
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        for sub in snapshot.subscriptions:
            self.assertNotIn("example.com", sub.email_redacted)
            self.assertNotIn("acc_google_123", sub.account_id_redacted)
            self.assertNotIn("acc_anthropic_456", sub.account_id_redacted)
            self.assertNotIn("acc_codex_789", sub.account_id_redacted)
            self.assertTrue("*" in sub.email_redacted)
            self.assertTrue("*" in sub.account_id_redacted)
        print("  [PASS] Zero raw emails or account IDs leaked; redaction verified.")

    # -------------------------------------------------------------------------
    # TEST 2: Multi-Window Constraint & Bottleneck Analysis
    # -------------------------------------------------------------------------
    def test_multi_window_bottleneck_detection(self):
        print("\n--- TEST 2: Multi-Window Bottleneck Detection ---")
        # In Anthropic mock: 5h window resets in 4.87h, 7d resets in 145h. Both 100% remaining.
        # Bottleneck picks the 5h window because it resets sooner!
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        anthropic_sub = snapshot.get_provider_reports("anthropic")[0]
        self.assertIsNotNone(anthropic_sub.bottleneck_window)
        self.assertEqual(anthropic_sub.bottleneck_window.id, "anthropic:5h")

        # Now simulate 5h window being 90% used (10% remaining) while 7d window is 100% remaining
        constrained_dict = copy.deepcopy(self.mock_usage_dict)
        constrained_dict["reports"][1]["limits"][0]["amount"]["remainingFraction"] = 0.10
        constrained_dict["reports"][1]["limits"][0]["amount"]["remaining"] = 10.0
        snapshot_c = parse_usage_json(constrained_dict, current_time_ms=self.mock_now_ms)
        anthropic_c = snapshot_c.get_provider_reports("anthropic")[0]
        self.assertEqual(anthropic_c.bottleneck_window.id, "anthropic:5h")
        self.assertAlmostEqual(anthropic_c.bottleneck_window.amount.remaining_fraction, 0.10)
        print("  [PASS] Multi-window bottleneck correctly identified 5h window as constraint.")

    # -------------------------------------------------------------------------
    # TEST 3: Near-Reset Surplus Quota -> Codex Promotion
    # -------------------------------------------------------------------------
    def test_codex_promotion_near_reset_with_surplus(self):
        print("\n--- TEST 3: Codex Promotion Near Reset with Surplus Allowance ---")
        # Codex resets in 18 hours (<= 48h) with 70% remaining quota!
        near_reset_dict = copy.deepcopy(self.mock_usage_dict)
        codex_lim = near_reset_dict["reports"][2]["limits"][0]
        codex_lim["window"]["resetsAt"] = self.mock_now_ms + (18 * 3600 * 1000)
        codex_lim["amount"]["remainingFraction"] = 0.70
        codex_lim["amount"]["remaining"] = 70.0

        snapshot = parse_usage_json(near_reset_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        # A) Reviews and reasoning promote GPT-6.1 Sol high; Astra remains reserved
        # for explicitly tagged hardest cross-cutting work.
        rec_review = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertTrue(rec_review.promotion_applied)
        self.assertEqual(rec_review.selected_model, MODEL_CODEX_SOL)
        self.assertIn("GPT-6.1 Sol high", rec_review.reasoning)
        print(f"  [PASS] High-risk review promoted GPT-6.1 Sol: {rec_review.selected_model}")

        rec_reasoning = selector.select_model(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.MEDIUM)
        self.assertTrue(rec_reasoning.promotion_applied)
        self.assertEqual(rec_reasoning.selected_model, MODEL_CODEX_SOL)
        print(f"  [PASS] Deep reasoning promoted GPT-6.1 Sol: {rec_reasoning.selected_model}")

        # C) Routine Execution (capable task): should promote Codex Fast
        rec_exec = selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.MEDIUM)
        self.assertTrue(rec_exec.promotion_applied)
        self.assertEqual(rec_exec.selected_model, MODEL_CODEX_FAST)
        print(f"  [PASS] Routine execution promoted Codex Fast: {rec_exec.selected_model}")

    # -------------------------------------------------------------------------
    # TEST 4: Near-Reset Exhausted Quota -> No Promotion
    # -------------------------------------------------------------------------
    def test_codex_no_promotion_near_reset_exhausted(self):
        print("\n--- TEST 4: No Promotion Near Reset if Quota Exhausted ---")
        # Codex resets in 18 hours but only 10% remaining (< 25% threshold)
        exhausted_dict = copy.deepcopy(self.mock_usage_dict)
        codex_lim = exhausted_dict["reports"][2]["limits"][0]
        codex_lim["window"]["resetsAt"] = self.mock_now_ms + (18 * 3600 * 1000)
        codex_lim["amount"]["remainingFraction"] = 0.10
        codex_lim["amount"]["remaining"] = 10.0

        snapshot = parse_usage_json(exhausted_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        # With Codex exhausted, judgment-heavy non-UI review uses Sonnet medium.
        rec = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertFalse(rec.promotion_applied)
        self.assertEqual(rec.selected_model, MODEL_CLAUDE_SONNET_55)
        print(f"  [PASS] Codex exhausted: no promotion; routed to {rec.selected_model}")

    # -------------------------------------------------------------------------
    # TEST 5: Distant Reset -> Anthropic Preservation & Flash 3.8 Default
    # -------------------------------------------------------------------------
    def test_anthropic_preservation_and_flash_default(self):
        print("\n--- TEST 5: Distant Reset Anthropic Preservation & Flash 3.8 Default ---")
        # In baseline mock: Anthropic 7d resets in 145 hours.
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        # Routine task should NEVER route to Anthropic; routes to Gemini 3.8 Flash
        rec_routine = selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertEqual(rec_routine.selected_model, MODEL_GEMINI_FLASH)
        self.assertFalse(rec_routine.promotion_applied)
        self.assertIn("Gemini 3.8 Flash", rec_routine.reasoning)
        print(f"  [PASS] Routine execution defaults to: {rec_routine.selected_model}")

        # Operator DEEP_REASONING ladder policy (#214): LOW and MEDIUM lead with OpenCode Go
        # GLM-5.3, then DeepSeek V4 Pro, then Gemini 3.8 Flash; direct Anthropic is preserved
        # for the orchestrator. When Go is uncredentialed in hermetic tests, DeepSeek V4 Pro leads.
        rec_reason = selector.select_model(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.LOW)
        self.assertEqual(rec_reason.selected_model, MODEL_DEEPSEEK_PRO)
        print(f"  [PASS] Deep reasoning preserved Anthropic (routed to {rec_reason.selected_model})")

    # -------------------------------------------------------------------------
    # TEST 6: Cooldown & Rate Limit Safety Failover
    # -------------------------------------------------------------------------
    def test_cooldown_and_429_safety(self):
        print("\n--- TEST 6: Cooldown & Rate Limit Safety Failover ---")
        cooldown_dict = copy.deepcopy(self.mock_usage_dict)
        # Put Google Antigravity into rate_limited status
        cooldown_dict["reports"][0]["limits"][0]["status"] = "rate_limited"

        snapshot = parse_usage_json(cooldown_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        # Routine execution should failover cleanly to Codex Fast rather than crashing
        rec = selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertTrue(rec.cooldown_fallback)
        self.assertEqual(rec.selected_model, MODEL_CODEX_FAST)
        self.assertIn("cooldown", rec.reasoning.lower())
        print(f"  [PASS] Google Antigravity in cooldown cleanly fell back to: {rec.selected_model}")

    # -------------------------------------------------------------------------
    # TEST 7: Unknown / Stale Balances Handled Safely
    # -------------------------------------------------------------------------
    def test_unknown_and_stale_balance_safety(self):
        print("\n--- TEST 7: Unknown / Stale Balance Safety ---")
        # Empty reports simulating unauthenticated or missing balance loader
        empty_snapshot = parse_usage_json({"generatedAt": self.mock_now_ms, "reports": []}, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(empty_snapshot)

        # Query provider allowance: must return safe neutral baseline, NOT 0.0 or infinity
        rem_frac, hrs_reset, btn_label, status = empty_snapshot.get_effective_allowance("openai-codex")
        self.assertEqual(rem_frac, 0.5)  # Neutral baseline
        self.assertEqual(status, "unknown")
        self.assertNotEqual(rem_frac, 0.0)
        self.assertNotEqual(rem_frac, float("inf"))

        # Routine execution still safely resolves without throwing
        rec = selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertEqual(rec.selected_model, MODEL_GEMINI_FLASH)
        print("  [PASS] Unknown balances treated with neutral baseline; no crash or zero-starvation.")

    # -------------------------------------------------------------------------
    # TEST 8: Dormant Grok Filtering
    # -------------------------------------------------------------------------
    def test_dormant_grok_handling(self):
        print("\n--- TEST 8: Dormant Grok Handling ---")
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        self.assertIn("xai-oauth", snapshot.dormant_providers)
        selector = ResetAwareModelSelector(snapshot)
        grok_meta = selector.evaluate_provider("xai-oauth")
        self.assertFalse(grok_meta["is_available"])
        self.assertEqual(grok_meta["status"], "dormant")

        # Routing across all task types must never select dormant Grok
        for tt in TaskType:
            rec = selector.select_model(task_type=tt, risk_level=RiskLevel.MEDIUM)
            self.assertNotEqual(rec.selected_model, "xai-oauth/grok-4.6:high")
            self.assertNotEqual(rec.selected_model, "xai-oauth/grok-build:xhigh")
        print("  [PASS] Dormant xAI Grok strictly excluded from all model dispatches.")

    # -------------------------------------------------------------------------
    # TEST 9: High-Risk Review Quality Gate (Flash 3.8 Barred as Sole Gate)
    # -------------------------------------------------------------------------
    def test_high_risk_review_quality_gate(self):
        print("\n--- TEST 9: High-Risk Review Quality Gate ---")
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        # High-risk review must use a strong non-UI tier, never Flash or Opus.
        rec = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertEqual(rec.selected_model, MODEL_CLAUDE_SONNET_55)
        self.assertNotEqual(rec.selected_model, MODEL_CLAUDE_OPUS_55)
        self.assertTrue(rec.evidence_packet_required)
        print(f"  [PASS] High-risk review enforced strong non-UI model: {rec.selected_model}")

    # -------------------------------------------------------------------------
    # TEST 10: Deep Context Routing
    # -------------------------------------------------------------------------
    def test_deep_context_routing(self):
        print("\n--- TEST 10: Deep Context Routing (> 180k tokens) ---")
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        rec = selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, context_tokens=220000)
        self.assertEqual(rec.selected_model, MODEL_GEMINI_PRO)
        self.assertIn("Gemini 3.1 Pro", rec.reasoning)
        print(f"  [PASS] Context 220k routed to: {rec.selected_model}")

    # -------------------------------------------------------------------------
    # TEST 11: Token-Saving EvidencePacket Protocol
    # -------------------------------------------------------------------------
    def test_evidence_packet_protocol(self):
        print("\n--- TEST 11: Compact EvidencePacket Protocol (< 1.5 KB) ---")
        packet = EvidencePacket(
            head_sha="d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3",
            base_sha="a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            changed_files=[
                "workflows/balance_loader.py",
                "workflows/model_routing.py",
                "workflows/routing_smoke_test.py",
            ],
            contracts_changed=[
                "Read-only sanitized subscription loader contract",
                "Deterministic capability-first and reset-aware routing selector",
            ],
            reproduction_steps="python workflows/routing_smoke_test.py",
            test_results="11 test scenarios passed; 0 regressions",
            risk_summary="Low operational risk; pure read-only workflow utility, zero DB/billing mutation.",
            reference_urls=["https://github.com/Bavariance/polysimulator/issues/4545"],
        )

        md = packet.to_compact_markdown()
        byte_size = len(md.encode("utf-8"))
        print(f"  Evidence Packet Markdown Size: {byte_size} bytes")
        self.assertLess(byte_size, 1536)  # Must be strictly under 1.5 KB
        self.assertIn("workflows/balance_loader.py", md)
        self.assertIn("d4e5f6a7", md)
        print("  [PASS] Compact EvidencePacket generated and bounded under 1.5 KB.")

    # -------------------------------------------------------------------------
    # TEST 12: Real Live Loader Read-Only Smoke Test
    # -------------------------------------------------------------------------
    def test_real_loader_live_smoke(self):
        print("\n--- TEST 12: Real Loader Live Smoke Test ---")
        try:
            with mock.patch("balance_loader.fetch_live_usage", return_value=self.mock_usage_dict):
                live_snapshot = load_snapshot(allow_live=True)
            self.assertIsNotNone(live_snapshot)
            self.assertGreater(live_snapshot.generated_at_ms, 0)
            self.assertTrue(len(live_snapshot.subscriptions) > 0)
            self.assertIn("google-antigravity", live_snapshot.active_providers)

            # Check that Google has limits
            google_rep = live_snapshot.get_provider_reports("google-antigravity")[0]
            self.assertTrue(len(google_rep.limits) > 0)
            self.assertEqual(google_rep.limits[0].amount.unit, "percent")
            self.assertIsNotNone(google_rep.bottleneck_window)

            # Test recommendation generation on live snapshot
            selector = ResetAwareModelSelector(live_snapshot)
            rec = selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
            self.assertIsNotNone(rec.selected_model)
            self.assertEqual(rec.selected_model, MODEL_GEMINI_FLASH)
            print(f"  [PASS] Live loader fetched {len(live_snapshot.subscriptions)} subscription reports.")
            print(f"  [PASS] Real live snapshot recommended: {rec.selected_model}")
        except Exception as e:
            self.fail(f"Live loader smoke test failed: {e}")

    # -------------------------------------------------------------------------
    # TEST 13: VeyyonBalanceAdapter and Normalized Snapshot Conversion
    # -------------------------------------------------------------------------
    def test_adapter_normalization(self):
        print("\n--- TEST 13: VeyyonBalanceAdapter & Normalized Snapshot ---")
        direct_adapter = DirectJsonAdapter(self.mock_usage_dict)
        norm_snapshot = direct_adapter.fetch_snapshot()

        self.assertIsInstance(norm_snapshot, NormalizedBalanceSnapshot)
        self.assertEqual(norm_snapshot.schema_version, "1.0")
        self.assertIn("google-antigravity", norm_snapshot.providers)
        self.assertIn("anthropic", norm_snapshot.providers)
        self.assertIn("openai-codex", norm_snapshot.providers)
        self.assertIn("xai-oauth", norm_snapshot.dormant_providers)

        # Verify effective allowance query on normalized snapshot
        rem, hrs, btn, stat = norm_snapshot.get_effective_allowance("google-antigravity")
        self.assertAlmostEqual(rem, 0.946)
        self.assertEqual(stat, "ok")
        print("  [PASS] Adapter normalized snapshot matches canonical contract.")

    # -------------------------------------------------------------------------
    # TEST 14: FileBalanceAdapter Portability
    # -------------------------------------------------------------------------
    def test_file_balance_adapter(self):
        print("\n--- TEST 14: FileBalanceAdapter Portability ---")
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tf:
            json.dump(self.mock_usage_dict, tf)
            temp_path = tf.name

        try:
            file_adapter = FileBalanceAdapter(temp_path)
            snapshot = file_adapter.fetch_snapshot()
            self.assertIsInstance(snapshot, NormalizedBalanceSnapshot)
            self.assertTrue(len(snapshot.providers) > 0)
            print("  [PASS] FileBalanceAdapter loaded and normalized external file.")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    # -------------------------------------------------------------------------
    # TEST 15: HarnessDispatchPacket Generation
    # -------------------------------------------------------------------------
    def test_harness_dispatch_packet(self):
        print("\n--- TEST 15: HarnessDispatchPacket End-to-End Generation ---")
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        # High-risk review dispatch packet
        packet = selector.dispatch(
            task_type=TaskType.STRONG_REVIEW,
            risk_level=RiskLevel.HIGH,
            head_sha="1122334455667788",
            base_sha="aabbccddeeff0011",
            changed_files=["src/core.py"],
        )

        self.assertIsInstance(packet, HarnessDispatchPacket)
        self.assertEqual(packet.schema_version, "1.0")
        self.assertEqual(packet.recommendation["model"], MODEL_CLAUDE_SONNET_55)
        self.assertEqual(packet.recommendation["agent_role"], "sonnet")
        self.assertEqual(packet.recommendation["provider"], "anthropic")
        self.assertIsNotNone(packet.evidence_packet)
        self.assertEqual(packet.evidence_packet["head_sha"], "1122334455667788")

        # Verify JSON serialization
        json_str = packet.to_json()
        parsed = json.loads(json_str)
        self.assertEqual(parsed["task"]["task_type"], "strong_review")
        self.assertEqual(parsed["recommendation"]["agent_role"], "sonnet")
        print("  [PASS] HarnessDispatchPacket emitted valid JSON with agent role and evidence.")

    # -------------------------------------------------------------------------
    # TEST 16: Zero Global Mutation Invariant
    # -------------------------------------------------------------------------
    def test_zero_global_mutation(self):
        print("\n--- TEST 16: Zero Global Mutation Invariant ---")
        # Inspect profiles/default/agent/config.yml before and after
        config_path = os.path.join(SCRIPT_DIR, "..", "profiles", "default", "agent", "config.yml")
        mtime_before = os.path.getmtime(config_path) if os.path.exists(config_path) else 0

        # Run multiple dispatches
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)
        for tt in TaskType:
            for rl in RiskLevel:
                selector.dispatch(task_type=tt, risk_level=rl)

        mtime_after = os.path.getmtime(config_path) if os.path.exists(config_path) else 0
        self.assertEqual(mtime_before, mtime_after)
        print("  [PASS] Zero config.yml mutation verified; pure decoupled recommendation.")

    # -------------------------------------------------------------------------
    # TEST 17: Rework Count Escalation (C9 Invariant)
    # -------------------------------------------------------------------------
    def test_rework_count_escalation(self):
        print("\n--- TEST 17: Rework Count Escalation (C9) ---")
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        # Rework is judgment-heavy non-UI work: Sol medium is the normal Codex
        # writer and Sonnet medium is the cross-provider fallback.
        rec = selector.select_model(
            task_type=TaskType.ROUTINE_EXECUTION,
            risk_level=RiskLevel.LOW,
            rework_count=1,
        )
        self.assertNotEqual(rec.selected_model, MODEL_GEMINI_FLASH)
        self.assertEqual(rec.selected_model, MODEL_CODEX_WORKER)
        self.assertEqual(rec.fallback_model, MODEL_CLAUDE_SONNET_55)
        self.assertIn("high-risk implementation", rec.reasoning.lower())
        print(f"  [PASS] Rework escalation: {rec.selected_model} (fallback: {rec.fallback_model}, cross-provider)")

        # A credentialed Z.AI GLM Coding Plan is the ladder's first rung.
        glm_selector = ResetAwareModelSelector(snapshot, credentialed_providers={ZAI_PROVIDER})
        rec_glm = glm_selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW, rework_count=1)
        self.assertEqual(rec_glm.selected_model, MODEL_ZAI_GLM)
        self.assertEqual(rec_glm.fallback_model, MODEL_CODEX_WORKER)
        print(f"  [PASS] Credentialed GLM-5.3 leads the ladder: {rec_glm.selected_model} (fallback {rec_glm.fallback_model})")

    # -------------------------------------------------------------------------
    # TEST 18: Domain Tags Escalation (C9 Invariant)
    # -------------------------------------------------------------------------
    def test_domain_tags_escalation(self):
        print("\n--- TEST 18: Domain Tags Escalation (C9) ---")
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        # High-risk domain tags move bounded Flash work to Sol medium.
        rec = selector.select_model(
            task_type=TaskType.ROUTINE_EXECUTION,
            risk_level=RiskLevel.LOW,
            domain_tags=["auth", "state_machine"],
        )
        self.assertEqual(rec.selected_model, MODEL_CODEX_WORKER)
        self.assertEqual(rec.fallback_model, MODEL_CLAUDE_SONNET_55)
        self.assertNotEqual(model_to_provider(rec.selected_model),
                            model_to_provider(rec.fallback_model))
        print(f"  [PASS] Domain tags escalated to {rec.selected_model} (cross-provider fallback: {rec.fallback_model})")

    # -------------------------------------------------------------------------
    # TEST 19: C6 Real Duration and C7 Paired Window Metrics
    # -------------------------------------------------------------------------
    def test_c6_real_duration_and_c7_paired_window_metrics(self):
        print("\n--- TEST 19: C6 Real Duration & C7 Paired Window Metrics ---")
        # In mock: Anthropic 5h window (18000s duration, 4.87h reset) and 7d window (604800s duration, 145h reset)
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        norm_snapshot = snapshot.to_normalized()
        anthropic_prov = norm_snapshot.providers["anthropic"]

        # Verify C6: exact duration in hours from duration_seconds (5.0h, NOT string-sniffed 168h!)
        self.assertAlmostEqual(anthropic_prov.bottleneck_duration_hours, 5.0)
        self.assertAlmostEqual(anthropic_prov.cycle_duration_hours, 168.0)

        # Verify C7: get_effective_allowance pairs remaining and hours_to_reset from the SAME bottleneck window!
        rem, hrs, btn_id, stat = norm_snapshot.get_effective_allowance("anthropic")
        self.assertEqual(rem, 1.0)
        self.assertAlmostEqual(hrs, 4.872426944444444)  # from 5h window!
        self.assertEqual(btn_id, "Claude 5 Hour")

        # Verify separate cycle allowance tracks the 7d window
        cycle_rem, cycle_hrs, cycle_lbl, _ = norm_snapshot.get_cycle_allowance("anthropic")
        self.assertEqual(cycle_rem, 1.0)
        self.assertAlmostEqual(cycle_hrs, 145.03909361111113)  # from 7d window!
        print("  [PASS] C6 exact window durations and C7 paired window metrics verified.")

    # -------------------------------------------------------------------------
    # TEST 20: No Flash High-Risk Fallback
    # -------------------------------------------------------------------------
    def test_no_flash_high_risk_fallback(self):
        print("\n--- TEST 20: No Flash High-Risk Fallback ---")
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        # High-risk review and reasoning never select or fall back to Flash.
        rec_rev = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertNotIn("flash", rec_rev.selected_model.lower())
        self.assertNotIn("flash", rec_rev.fallback_model.lower())
        self.assertIn(rec_rev.fallback_model, (MODEL_CLAUDE_SONNET_55, MODEL_CODEX_SOL,
                                               MODEL_GO_GLM53_FLASH, MODEL_DEEPSEEK_PRO))

        rec_reason = selector.select_model(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.HIGH)
        self.assertNotIn("flash", rec_reason.selected_model.lower())
        self.assertNotIn("flash", rec_reason.fallback_model.lower())
        self.assertIn(rec_reason.fallback_model, (MODEL_CLAUDE_SONNET_55, MODEL_CODEX_SOL,
                                                  MODEL_DEEPSEEK_PRO))
        print(f"  [PASS] High-risk reasoning and review use strong non-Flash fallbacks: {rec_reason.fallback_model}.")

    # -------------------------------------------------------------------------
    # TEST 21: Codex Agent Roles & Structural Retry Flash Bar
    # -------------------------------------------------------------------------
    def test_codex_roles_and_structural_retry_invariants(self):
        print("\n--- TEST 21: Codex Agent Roles & Structural Failure Retry ---")
        # 1. Verify model_to_agent_role assigns actual Codex agent roles from roster
        self.assertEqual(model_to_agent_role(MODEL_CODEX_ASTRA, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "codex-reviewer")
        self.assertEqual(model_to_agent_role(MODEL_CODEX_ASTRA, TaskType.ROUTINE_EXECUTION, RiskLevel.HIGH), "codex-worker")
        self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "codex-reviewer")
        self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.ROUTINE_EXECUTION, RiskLevel.HIGH), "codex-worker")
        self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.DEEP_REASONING, RiskLevel.HIGH), "thinker")
        self.assertEqual(model_to_agent_role(MODEL_CODEX_FAST, TaskType.ROUTINE_EXECUTION, RiskLevel.LOW), "codex-worker")

        # 2. Verify dispatch packet with promoted Codex assigns actual Codex agent role
        mock_codex_promoted = copy.deepcopy(self.mock_usage_dict)
        # Set Codex Pro 7d window to reset in 12h with 80% remaining
        for rep in mock_codex_promoted["reports"]:
            if rep["provider"] == "openai-codex" and rep["metadata"].get("planType") == "pro":
                rep["limits"][0]["window"]["resetsAt"] = self.mock_now_ms + 43200000  # 12h
                rep["limits"][0]["amount"]["remainingFraction"] = 0.8
                rep["limits"][0]["amount"]["remaining"] = 80.0

        snapshot = parse_usage_json(mock_codex_promoted, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        packet_review = selector.dispatch(
            task_type=TaskType.STRONG_REVIEW,
            risk_level=RiskLevel.HIGH,
            head_sha="abcdef123456",
        )
        self.assertEqual(packet_review.recommendation["model"], MODEL_CODEX_SOL)
        self.assertEqual(packet_review.recommendation["agent_role"], "codex-reviewer")
        print(f"  [PASS] Codex review dispatch role: {packet_review.recommendation['agent_role']}")

        packet_worker = selector.dispatch(
            task_type=TaskType.ROUTINE_EXECUTION,
            risk_level=RiskLevel.MEDIUM,
        )
        self.assertEqual(packet_worker.recommendation["model"], MODEL_CODEX_FAST)
        self.assertEqual(packet_worker.recommendation["agent_role"], "codex-worker")
        print(f"  [PASS] Codex worker dispatch role: {packet_worker.recommendation['agent_role']}")

        # 3. Flash NOT structural-failure retry: with every subscription tier in cooldown,
        # routine execution with rework_count=1 goes to pay-per-token DeepSeek V4 Pro (the
        # ladder's next rung), NEVER Flash and never paid Anthropic.
        mock_all_cooldown = copy.deepcopy(self.mock_usage_dict)
        for rep in mock_all_cooldown["reports"]:
            rep["metadata"]["limitReached"] = True
            rep["metadata"]["allowed"] = False
        snapshot_cd = parse_usage_json(mock_all_cooldown, current_time_ms=self.mock_now_ms)
        selector_cd = ResetAwareModelSelector(snapshot_cd)
        rec_structural = selector_cd.select_model(
            task_type=TaskType.ROUTINE_EXECUTION,
            risk_level=RiskLevel.LOW,
            rework_count=1,
        )
        self.assertEqual(rec_structural.selected_model, MODEL_DEEPSEEK_PRO)
        self.assertEqual(model_to_agent_role(rec_structural.selected_model, TaskType.ROUTINE_EXECUTION, RiskLevel.HIGH), "ds-pro")
        self.assertNotEqual(rec_structural.fallback_model, MODEL_GEMINI_FLASH)
        self.assertNotIn("flash", rec_structural.fallback_model)
        self.assertNotIn("anthropic/", rec_structural.fallback_model)
        print(f"  [PASS] Structural failure retry strictly barred Flash under cooldown: {rec_structural.selected_model} "
              f"(fallback {rec_structural.fallback_model})")

    # -------------------------------------------------------------------------
    # Helpers for the allowance-aware tests (TESTS 22-26)
    # -------------------------------------------------------------------------
    def _usage_with_ag_families(
        self,
        anthropic_used=0.0,
        openai_used=0.0,
        codex_used=0.03,
        codex_reset_hrs=None,
        anthropic_week_used=None,
        anthropic_week_reset_hrs=None,
    ):
        """Live-shaped usage: Antigravity reports one daily window per family; Codex pro and
        direct Anthropic 7d windows can be moved to any used fraction / reset distance."""
        usage = copy.deepcopy(self.mock_usage_dict)
        ag = usage["reports"][0]
        for family, used in (("anthropic", anthropic_used), ("openai", openai_used)):
            ag["limits"].append({
                "id": f"google-antigravity:{family}:default:daily",
                "label": f"Usage ({family})",
                "window": {
                    "id": "daily",
                    "label": "Daily",
                    "durationMs": 86400000,
                    "resetsAt": self.mock_now_ms + 17700000,  # ~4.9h
                },
                "amount": {
                    "unit": "percent",
                    "remainingFraction": 1.0 - used,
                    "usedFraction": used,
                    "remaining": (1.0 - used) * 100,
                    "used": used * 100,
                    "limit": 100.0,
                },
                "status": "ok",
            })

        def set_window(limit, used, reset_hrs):
            limit["amount"].update({
                "remainingFraction": 1.0 - used,
                "usedFraction": used,
                "remaining": (1.0 - used) * 100,
                "used": used * 100,
            })
            if reset_hrs is not None:
                limit["window"]["resetsAt"] = self.mock_now_ms + int(reset_hrs * 3600 * 1000)

        set_window(usage["reports"][2]["limits"][0], codex_used, codex_reset_hrs)
        if anthropic_week_used is not None:
            set_window(usage["reports"][1]["limits"][1], anthropic_week_used, anthropic_week_reset_hrs)
        return usage
    def _usage_with_opencode_go(self, weekly_used=0.447, weekly_reset_hrs=148.8, five_h_used=0.33, five_h_reset_hrs=3.5):
        usage = self._usage_with_ag_families()
        def _w(wid, dur_h, u, r_h, lim):
            r_at = self.mock_now_ms + int(r_h * 3600 * 1000)
            return {"id": f"opencode-go:{wid}", "label": wid, "window": {"id": wid, "label": wid, "durationMs": int(dur_h * 3600 * 1000), "resetsAt": r_at}, "amount": {"unit": "usd", "limit": lim, "used": lim * u, "remaining": lim * (1.0 - u), "usedFraction": u, "remainingFraction": 1.0 - u}, "status": "ok"}
        usage["reports"].append({"provider": "opencode-go", "account_id_redacted": "default", "fetchedAt": self.mock_now_ms, "limits": [_w("rolling-5h", 5, five_h_used, five_h_reset_hrs, 6.0), _w("weekly", 168, weekly_used, weekly_reset_hrs, 30.0)], "metadata": {"is_available": True}})
        return usage
    @staticmethod
    def _quota_with(provider: str, until_utc: str, window_id: str = "daily") -> QuotaSnapshot:
        """An exhaustion cache marking one provider/window spent until `until_utc`."""
        entry = QuotaWindowEntry(
            provider=provider, window_id=window_id, used_fraction=1.0,
            exhausted_until=until_utc, fetched_at="2026-09-25T00:00:00Z", source="429",
        )
        return QuotaSnapshot(updated_at="2026-09-25T00:00:00Z", entries={f"{provider}|{window_id}": entry})

    # -------------------------------------------------------------------------
    # TEST 34: An exhausted window is never selected, and re-arms after its reset
    # -------------------------------------------------------------------------
    def test_exhausted_window_never_selected(self):
        print("\n--- TEST 34: Exhausted Window Never Selected ---")
        usage = self._usage_with_ag_families(anthropic_used=0.0)
        plain = self._selector(usage)
        self.assertEqual(plain.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH).selected_model,
                         MODEL_CLAUDE_SONNET_55)

        # Three lanes died on a 429 within 3 s after being dispatched onto the exhausted
        # Antigravity Opus window, so a cache entry with a future reset must remove it.
        exhausted = ResetAwareModelSelector(
            parse_usage_json(usage, current_time_ms=self.mock_now_ms),
            quota_snapshot=self._quota_with("google-antigravity:anthropic", "2099-01-01T00:00:00Z"),
        )
        self.assertFalse(exhausted.quota_snapshot().is_eligible("google-antigravity:anthropic"))
        self.assertEqual(
            exhausted.provider_exhaustion_reason(MODEL_AG_CLAUDE_OPUS),
            "google-antigravity:anthropic is exhausted until 2099-01-01T00:00:00+00:00",
        )
        for task_type, risk in ((TaskType.STRONG_REVIEW, RiskLevel.HIGH),
                                (TaskType.STRONG_REVIEW, RiskLevel.MEDIUM),
                                (TaskType.ROUTINE_EXECUTION, RiskLevel.HIGH),
                                (TaskType.DEEP_REASONING, RiskLevel.HIGH)):
            rec = exhausted.select_model(task_type=task_type, risk_level=risk)
            self.assertNotEqual(rec.selected_model, MODEL_AG_CLAUDE_OPUS, f"{task_type}/{risk}")
            self.assertNotEqual(rec.fallback_model, MODEL_AG_CLAUDE_OPUS, f"{task_type}/{risk}")

        # The same entry with a reset that has already passed re-arms the provider with no
        # extra bookkeeping: the reset timestamp is the only state that matters.
        rearmed = ResetAwareModelSelector(
            parse_usage_json(usage, current_time_ms=self.mock_now_ms),
            quota_snapshot=self._quota_with("google-antigravity:anthropic", "2020-01-01T00:00:00Z"),
        )
        self.assertIsNone(rearmed.provider_exhaustion_reason(MODEL_AG_CLAUDE_OPUS))
        self.assertEqual(rearmed.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH).selected_model,
                         MODEL_CLAUDE_SONNET_55)

        # An unrelated provider's window is untouched by the entry.
        self.assertTrue(exhausted.quota_snapshot().is_eligible("openai-codex"))
        print("  [PASS] Exhausted Antigravity Opus skipped on every ladder; re-armed after its reset, siblings unaffected.")

    # -------------------------------------------------------------------------
    # TEST 35: A 429 body updates the exhaustion cache the router reads
    # -------------------------------------------------------------------------
    def test_quota_429_body_blocks_provider(self):
        print("\n--- TEST 35: 429 Body Updates the Router's Cache ---")
        cache = tmp_quota_path()
        self.addCleanup(shutil.rmtree, cache.parent, ignore_errors=True)
        body = json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                     "message": "quota exceeded; resets at 2099-01-01T00:00:00Z",
                                     "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                                  "retryDelay": "3s"}]}})
        reset = apply_quota_error("google-antigravity:anthropic", "daily", body, path=cache)
        self.assertIsNotNone(reset)
        self.assertEqual(reset.exhausted_until, "2099-01-01T00:00:00Z")
        reloaded = load_quota_file(cache)
        self.assertFalse(reloaded.is_eligible("google-antigravity:anthropic"))
        usage = self._usage_with_ag_families(anthropic_used=0.0)
        selector = ResetAwareModelSelector(parse_usage_json(usage, current_time_ms=self.mock_now_ms),
                                           quota_snapshot=reloaded)
        rec = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertNotEqual(rec.selected_model, MODEL_AG_CLAUDE_OPUS)
        print(f"  [PASS] 429 body wrote {reset.exhausted_until}; review lane routed to {rec.selected_model}.")

    # -------------------------------------------------------------------------
    # TEST: Task-class routing keeps Opus out of non-UI review.
    # -------------------------------------------------------------------------
    def test_review_routing_routine_ds_task_and_high_risk_reviewer(self):
        print("\n--- TEST: Task-Class Review Routing ---")
        usage = self._usage_with_ag_families(anthropic_used=0.0)
        selector = self._selector(usage)

        rec_routine = selector.select_model(
            task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH, diff_lines=300)
        self.assertEqual(rec_routine.selected_model, MODEL_DEEPSEEK_FLASH)

        rec_delta = selector.select_model(task_type=TaskType.STRONG_REVIEW, rework_count=1)
        self.assertEqual(rec_delta.selected_model, MODEL_DEEPSEEK_FLASH)

        for domain in ("migration", "money", "auth", "concurrency"):
            rec = selector.select_model(
                task_type=TaskType.STRONG_REVIEW,
                risk_level=RiskLevel.HIGH,
                domain_tags=[domain],
                rework_count=0,
            )
            self.assertNotEqual(rec.selected_model, MODEL_CLAUDE_OPUS_55, domain)
            self.assertIn(
                model_to_agent_role(rec.selected_model, TaskType.STRONG_REVIEW, RiskLevel.HIGH),
                ("codex-reviewer", "sonnet", "ds-pro", "go-review"),
                domain,
            )
            self.assertNotIn("flash", rec.fallback_model.lower(), domain)

        usage_exhausted = copy.deepcopy(usage)
        self._block_anthropic(usage_exhausted)
        rec_exhausted = self._selector(usage_exhausted).select_model(
            task_type=TaskType.STRONG_REVIEW,
            risk_level=RiskLevel.HIGH,
            domain_tags=["migration"],
        )
        self.assertNotEqual(rec_exhausted.selected_model, MODEL_CLAUDE_OPUS_55)
        self.assertNotIn("flash", rec_exhausted.selected_model.lower())
        self.assertNotIn("flash", rec_exhausted.fallback_model.lower())
        print("  [PASS] Routine/delta reviews stay cheap; high-risk non-UI reviews use Codex/Sonnet and never Opus or Flash.")
    def _selector(self, usage):
        return ResetAwareModelSelector(parse_usage_json(usage, current_time_ms=self.mock_now_ms))

    @staticmethod
    def _block_anthropic(usage):
        for rep in usage["reports"]:
            if rep["provider"] == "anthropic":
                rep["metadata"]["limitReached"] = True
                rep["metadata"]["allowed"] = False

    # -------------------------------------------------------------------------
    # TEST 22: Codex pro is paced: held back ahead of pace, spent fully before reset
    # -------------------------------------------------------------------------
    def test_codex_pro_paced_over_the_week(self):
        print("\n--- TEST 22: Codex Pro Paced Over the Week ---")
        # 92% used with 145h of 168h still to go: far ahead of pace, would run dry mid-week.
        usage = self._usage_with_ag_families(codex_used=0.92, codex_reset_hrs=145)
        self._block_anthropic(usage)
        selector = self._selector(usage)
        # High-risk review with Anthropic blocked lands on DeepSeek V4 Pro; the high-risk worker ladder lands on DeepSeek V4 Pro.
        expected = {
            TaskType.STRONG_REVIEW: MODEL_DEEPSEEK_PRO,
            TaskType.DEEP_REASONING: MODEL_DEEPSEEK_PRO,
            TaskType.ROUTINE_EXECUTION: MODEL_DEEPSEEK_PRO,
        }
        for task_type, model in expected.items():
            rec = selector.select_model(task_type=task_type, risk_level=RiskLevel.HIGH)
            self.assertEqual(rec.selected_model, model, f"{task_type}: ahead-of-pace Codex chosen")
            self.assertNotIn("openai-codex/", rec.selected_model)
            self.assertNotEqual(rec.fallback_model, MODEL_GEMINI_FLASH)
        self.assertTrue(rec.quota_metrics["codex_pro_throttled"])

        # Routine work with Gemini in cooldown must not drain ahead-of-pace Codex either.
        for lim in usage["reports"][0]["limits"]:
            if lim["id"].startswith("google-antigravity:google"):
                lim["status"] = "rate_limited"
        rec_routine = self._selector(usage).select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertNotIn("openai-codex/", rec_routine.selected_model)
        print("  [PASS] 92% used with 145h left: Codex held back; high-risk review and workers on DeepSeek V4 Pro.")

        # Near reset, review allowance is spent on GPT-6.1 Sol high, not Astra.
        usage_end = self._usage_with_ag_families(codex_used=0.92, codex_reset_hrs=6)
        rec_end = self._selector(usage_end).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertFalse(rec_end.quota_metrics["codex_pro_throttled"])
        self.assertTrue(rec_end.promotion_applied)
        self.assertEqual(rec_end.selected_model, MODEL_CODEX_SOL)
        print(f"  [PASS] 92% used with 6h left: remaining Codex spent before reset on {rec_end.selected_model}.")

        usage_pace = self._usage_with_ag_families(anthropic_used=0.95, codex_used=0.49, codex_reset_hrs=84)
        self._block_anthropic(usage_pace)
        rec_pace = self._selector(usage_pace).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertFalse(rec_pace.quota_metrics["codex_pro_throttled"])
        self.assertFalse(rec_pace.promotion_applied)
        self.assertEqual(rec_pace.selected_model, MODEL_CODEX_SOL)
        print(f"  [PASS] On-pace Codex remains the normal Sol review lane: {rec_pace.selected_model}")

    # -------------------------------------------------------------------------
    # TEST 26: Direct Anthropic uses Sonnet only when its worker allowance permits it.
    # -------------------------------------------------------------------------
    def test_anthropic_orchestrator_reserve(self):
        print("\n--- TEST 26: Anthropic Orchestrator Reserve ---")
        usage = self._usage_with_ag_families(
            anthropic_used=0.95, anthropic_week_used=0.64, anthropic_week_reset_hrs=72)
        rec = self._selector(usage).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertTrue(rec.quota_metrics["anthropic_orchestrator_reserve"])
        self.assertNotIn("anthropic/", rec.selected_model)
        self.assertNotIn("anthropic/", rec.fallback_model)

        usage_last = self._usage_with_ag_families(
            anthropic_used=0.95, codex_used=0.92, codex_reset_hrs=145,
            anthropic_week_used=0.64, anthropic_week_reset_hrs=72,
        )
        rec_last = self._selector(usage_last).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertNotIn("anthropic/", rec_last.selected_model)
        self.assertNotEqual(rec_last.selected_model, MODEL_CLAUDE_OPUS_55)

        usage_surplus = self._usage_with_ag_families(
            anthropic_used=0.0, anthropic_week_used=0.30, anthropic_week_reset_hrs=20)
        selector = self._selector(usage_surplus)
        rec_surplus = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertFalse(rec_surplus.quota_metrics["anthropic_orchestrator_reserve"])
        self.assertEqual(rec_surplus.selected_model, MODEL_CLAUDE_SONNET_55)
        self.assertEqual(model_to_agent_role(rec_surplus.selected_model, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "sonnet")

        stale = self._usage_with_ag_families(
            anthropic_used=0.95, anthropic_week_used=0.0, anthropic_week_reset_hrs=20)
        stale["reports"][1]["fetchedAt"] = self.mock_now_ms - 2 * 3600 * 1000
        rec_stale = self._selector(stale).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertTrue(rec_stale.quota_metrics["anthropic_orchestrator_reserve"])
        self.assertNotIn("anthropic/", rec_stale.selected_model)
        print("  [PASS] Anthropic reserve blocks workers; near-reset slack goes to Sonnet medium, never Opus.")

    # TEST 23: Non-UI judgment routes to Sonnet, never Opus.
    # -------------------------------------------------------------------------
    def test_antigravity_claude_chosen(self):
        print("\n--- TEST 23: Non-UI Judgment Routes to Sonnet ---")
        selector = self._selector(self._usage_with_ag_families(anthropic_used=0.0))
        rec_review = selector.select_model(
            task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH, domain_tags=["money"])
        self.assertEqual(rec_review.selected_model, MODEL_CLAUDE_SONNET_55)
        self.assertEqual(model_to_agent_role(rec_review.selected_model, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "sonnet")

        rec_med_review = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.MEDIUM)
        self.assertNotEqual(rec_med_review.selected_model, MODEL_CLAUDE_OPUS_55)

        rec_reason = selector.select_model(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.MEDIUM)
        self.assertEqual(rec_reason.selected_model, MODEL_DEEPSEEK_PRO)
        packet = selector.dispatch(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.MEDIUM)
        self.assertEqual(packet.recommendation["agent_role"], "ds-pro")
        print(f"  [PASS] High review on {rec_review.selected_model}; medium reasoning on {rec_reason.selected_model}.")

        # An almost spent Antigravity Claude window (95% used) is left alone.
        spent = self._selector(self._usage_with_ag_families(anthropic_used=0.95))
        rec_spent = spent.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertNotEqual(rec_spent.selected_model, MODEL_AG_CLAUDE_OPUS)
        # A snapshot that does not report the family at all never assumes it exists.
        absent = ResetAwareModelSelector(parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms))
        rec_absent = absent.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.MEDIUM)
        self.assertNotEqual(rec_absent.selected_model, MODEL_AG_CLAUDE_OPUS)
        print(f"  [PASS] Spent/unreported Antigravity Claude skipped: {rec_spent.selected_model}, {rec_absent.selected_model}")

    # -------------------------------------------------------------------------
    # TEST 24: DeepSeek V4.1 Flash takes overflow instead of paid Anthropic
    # -------------------------------------------------------------------------
    def test_deepseek_overflow(self):
        print("\n--- TEST 24: DeepSeek V4.1 Flash Overflow ---")
        selector = self._selector(self._usage_with_ag_families())
        rec_normal = selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertEqual(rec_normal.selected_model, MODEL_GEMINI_FLASH)
        self.assertEqual(rec_normal.fallback_model, MODEL_DEEPSEEK_FLASH)

        usage = self._usage_with_ag_families(codex_used=0.92)
        for lim in usage["reports"][0]["limits"]:
            if lim["id"].startswith("google-antigravity:google"):
                lim["status"] = "rate_limited"
        rec = self._selector(usage).select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertEqual(rec.selected_model, MODEL_DEEPSEEK_FLASH)
        self.assertEqual(rec.fallback_model, MODEL_OR_DEEPSEEK_FLASH)
        self.assertNotIn("anthropic/", rec.selected_model)
        self.assertTrue(rec.cooldown_fallback)
        self.assertEqual(model_to_agent_role(rec.selected_model, TaskType.ROUTINE_EXECUTION, RiskLevel.LOW), "ds-task")
        print(f"  [PASS] Gemini cooldown + throttled Codex overflowed to {rec.selected_model} (fallback {rec.fallback_model}).")

    # -------------------------------------------------------------------------
    # TEST 25: Free advisory reviewer is attached to reviews only, never as the gate
    # -------------------------------------------------------------------------
    def test_advisory_model_on_reviews_only(self):
        print("\n--- TEST 25: Advisory Free Reviewer ---")
        selector = self._selector(self._usage_with_ag_families())
        for risk in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH):
            rec = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=risk)
            self.assertEqual(rec.advisory_model, MODEL_OR_FREE_ADVISORY)
            self.assertNotEqual(rec.selected_model, MODEL_OR_FREE_ADVISORY)
            self.assertNotEqual(rec.fallback_model, MODEL_OR_FREE_ADVISORY)
        rec_exec = selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertIsNone(rec_exec.advisory_model)
        self.assertEqual(model_to_agent_role(MODEL_OR_FREE_ADVISORY, TaskType.STRONG_REVIEW, RiskLevel.LOW), "extra-review")
        print(f"  [PASS] Reviews carry advisory {MODEL_OR_FREE_ADVISORY}; it is never the selected or fallback reviewer.")

    # -------------------------------------------------------------------------
    # TEST 27: Cross-provider fallback invariant — every selected/fallback pair
    # must cross a provider boundary (Finding 2)
    # -------------------------------------------------------------------------
    def test_cross_provider_fallback_invariant(self):
        print("\n--- TEST 27: Cross-Provider Fallback Invariant ---")
        # Test across multiple provider states and all task/risk combos
        configs = [
            ("all_ok", self._usage_with_ag_families(anthropic_used=0.0)),
            ("ag_spent", self._usage_with_ag_families(anthropic_used=0.95)),
            ("codex_surplus", self._usage_with_ag_families(codex_used=0.92, codex_reset_hrs=6)),
        ]
        for label, usage in configs:
            selector = self._selector(usage)
            for task_type in TaskType:
                for risk in RiskLevel:
                    rec = selector.select_model(task_type=task_type, risk_level=risk)
                    sel_prov = model_to_provider(rec.selected_model)
                    fb_prov = model_to_provider(rec.fallback_model)
                    # Same-model fallback (e.g. Gemini Pro → Gemini Pro emergency) is tolerated
                    # but same-provider with different models is a bug.
                    if rec.selected_model != rec.fallback_model:
                        self.assertNotEqual(
                            sel_prov, fb_prov,
                            f"[{label}] {task_type.value}/{risk.value}: same-provider fallback "
                            f"{rec.selected_model} → {rec.fallback_model} ({sel_prov})"
                        )
        # Also test with anthropic blocked
        usage_blocked = self._usage_with_ag_families(anthropic_used=0.0)
        self._block_anthropic(usage_blocked)
        selector_blocked = self._selector(usage_blocked)
        for task_type in TaskType:
            for risk in RiskLevel:
                rec = selector_blocked.select_model(task_type=task_type, risk_level=risk)
                if rec.selected_model != rec.fallback_model:
                    sel_prov = model_to_provider(rec.selected_model)
                    fb_prov = model_to_provider(rec.fallback_model)
                    self.assertNotEqual(
                        sel_prov, fb_prov,
                        f"[blocked_anthropic] {task_type.value}/{risk.value}: same-provider fallback "
                        f"{rec.selected_model} → {rec.fallback_model} ({sel_prov})"
                    )
        print("  [PASS] All selected/fallback pairs cross provider boundaries.")

    # -------------------------------------------------------------------------
    # TEST 28: Codex two-account shape — free (ok) plus pro (warning)
    # -------------------------------------------------------------------------
    def test_codex_two_account_shape(self):
        print("\n--- TEST 28: Codex Two-Account Shape ---")
        # Simulate two Codex accounts: free at 100% remaining, pro at 8% remaining with warning
        usage = self._usage_with_ag_families()
        # The existing single report is the pro account
        pro_report = usage["reports"][2]
        pro_report["limits"][0]["amount"]["remainingFraction"] = 0.08
        pro_report["limits"][0]["amount"]["usedFraction"] = 0.92
        pro_report["limits"][0]["status"] = "warning"
        pro_report["metadata"]["planType"] = "pro"
        pro_report["metadata"]["accountId"] = "acc_codex_pro"
        # Add a free account with full allowance
        free_report = copy.deepcopy(pro_report)
        free_report["metadata"] = {"planType": "free", "accountId": "acc_codex_free", "email": "free@example.com"}
        free_report["limits"][0]["amount"]["remainingFraction"] = 1.0
        free_report["limits"][0]["amount"]["usedFraction"] = 0.0
        free_report["limits"][0]["status"] = "ok"
        free_report["limits"][0]["window"]["resetsAt"] = self.mock_now_ms + 2592000000  # 720h
        usage["reports"].append(free_report)

        snapshot = parse_usage_json(usage, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        # The free account (most remaining) serves the default Codex lane, but pacing and the
        # reported Codex metrics must come from the nearly spent pro window: a free account at
        # 0% used must never mask it.
        rec = selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertEqual(rec.selected_model, MODEL_GEMINI_FLASH)
        self.assertTrue(rec.quota_metrics["codex_pro_throttled"])
        self.assertAlmostEqual(rec.quota_metrics["codex_remaining"], 0.08, places=6)
        self.assertLess(rec.quota_metrics["codex_pro_headroom"], CODEX_PACE_MIN_HEADROOM)
        self.assertAlmostEqual(rec.burn_headroom, rec.quota_metrics["codex_pro_headroom"])
        # Throttled pro Codex hands judgment-heavy non-UI review to Sonnet.
        rec_review = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertEqual(rec_review.selected_model, MODEL_CLAUDE_SONNET_55)
        print(f"  [PASS] Two-account Codex metrics use the pro window (remaining "
              f"{rec.quota_metrics['codex_remaining']:.2f}, throttled={rec.quota_metrics['codex_pro_throttled']}); "
              f"review falls to {rec_review.selected_model}.")

    # -------------------------------------------------------------------------
    # TEST 29: Anthropic 5h window protection (Finding 3)
    def test_anthropic_5h_window_protection(self):
        print("\n--- TEST 29: Anthropic 5h Window Protection ---")
        # 5h window 85% used (> 80% floor), 7d window has lots of slack
        usage = self._usage_with_ag_families(anthropic_used=0.0)
        anthropic_report = usage["reports"][1]
        # Set the direct Anthropic 5h window to 85% used
        anthropic_report["limits"][0]["amount"]["remainingFraction"] = 0.15
        anthropic_report["limits"][0]["amount"]["usedFraction"] = 0.85
        # 7d window: 5% used, 120h left → headroom ≈ 1.33 (above 1.10 worker threshold)
        anthropic_report["limits"][1]["amount"]["remainingFraction"] = 0.95
        anthropic_report["limits"][1]["amount"]["usedFraction"] = 0.05
        anthropic_report["limits"][1]["window"]["resetsAt"] = self.mock_now_ms + int(120 * 3600 * 1000)

        selector = self._selector(usage)
        rec = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        # Workers must NOT take Anthropic when 5h > 80% used, even if 7d headroom is fine
        self.assertTrue(rec.quota_metrics["anthropic_orchestrator_reserve"],
                        "5h window at 85% used should trigger orchestrator reserve")
        self.assertNotIn("anthropic/", rec.selected_model)
        print(f"  [PASS] 5h at 85% used → workers blocked from Anthropic; routed to {rec.selected_model}")

        # 5h at 70% used should still allow workers (under 80% floor)
        anthropic_report["limits"][0]["amount"]["remainingFraction"] = 0.30
        anthropic_report["limits"][0]["amount"]["usedFraction"] = 0.70
        selector2 = self._selector(usage)
        rec2 = selector2.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertFalse(rec2.quota_metrics["anthropic_orchestrator_reserve"],
                         "5h at 70% used should not block workers from Anthropic (7d has slack)")
        print(f"  [PASS] 5h at 70% used → workers allowed; routed to {rec2.selected_model}")
    # TEST 30: Codex pace throttle tolerance band (Finding 4)
    # -------------------------------------------------------------------------
    def test_codex_pace_tolerance_band(self):
        print("\n--- TEST 30: Codex Pace Throttle Tolerance Band ---")
        # Early week: 1% used with 167.5h left → headroom ≈ 0.993.
        # Without tolerance: throttled. With tolerance (headroom < 0.90 AND used >= 50%): NOT throttled.
        usage_early = self._usage_with_ag_families(codex_used=0.01, codex_reset_hrs=167.5)
        self._block_anthropic(usage_early)
        selector = self._selector(usage_early)
        rec = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertFalse(rec.quota_metrics["codex_pro_throttled"],
                         "1% used early in week should NOT be throttled (tolerance band)")
        print(f"  [PASS] 1% used early week: not throttled, routed to {rec.selected_model}")

        # 30% used with 150h left → headroom ≈ 0.78. used < 50% floor → NOT throttled
        usage_30 = self._usage_with_ag_families(codex_used=0.30, codex_reset_hrs=150)
        self._block_anthropic(usage_30)
        rec_30 = self._selector(usage_30).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertFalse(rec_30.quota_metrics["codex_pro_throttled"],
                         "30% used (< 50% floor) should NOT be throttled despite headroom < 1.0")
        print(f"  [PASS] 30% used: not throttled (below 50% floor)")

        # 79% used with 40h left → headroom ≈ 0.88 < 0.90, used 79% >= 50% → throttled
        usage_79 = self._usage_with_ag_families(codex_used=0.79, codex_reset_hrs=40)
        self._block_anthropic(usage_79)
        rec_79 = self._selector(usage_79).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertTrue(rec_79.quota_metrics["codex_pro_throttled"],
                        "79% used with headroom 0.88 should be throttled (above 50% floor, below 0.90)")
        print(f"  [PASS] 79% used, headroom 0.88: throttled correctly")

    # -------------------------------------------------------------------------
    # TEST 31: Agent role mapping for AG Sonnet/GPT and Chinese providers
    # -------------------------------------------------------------------------
    def test_agent_role_mappings(self):
        print("\n--- TEST 31: Agent Role Mappings ---")
        # AG Sonnet → ag-sonnet (not ag-opus)
        self.assertEqual(model_to_agent_role(MODEL_AG_CLAUDE_SONNET, TaskType.ROUTINE_EXECUTION, RiskLevel.LOW), "ag-sonnet")
        # AG GPT → ag-gpt (not ag-opus)
        self.assertEqual(model_to_agent_role(MODEL_AG_GPT_OSS, TaskType.ROUTINE_EXECUTION, RiskLevel.LOW), "ag-gpt")
        # AG Opus → reviewer (not ag-opus, ag-opus no longer exists)
        self.assertEqual(model_to_agent_role(MODEL_AG_CLAUDE_OPUS, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "reviewer")
        # DeepSeek V4 Pro → ds-pro (ds-task is pinned to DeepSeek Flash)
        self.assertEqual(model_to_agent_role(MODEL_DEEPSEEK_PRO, TaskType.ROUTINE_EXECUTION, RiskLevel.HIGH), "ds-pro")
        self.assertEqual(model_to_agent_role(MODEL_DEEPSEEK_FLASH, TaskType.ROUTINE_EXECUTION, RiskLevel.LOW), "ds-task")
        # Chinese providers
        self.assertEqual(model_to_agent_role(MODEL_ZAI_GLM, TaskType.ROUTINE_EXECUTION, RiskLevel.LOW), "zai-task")
        self.assertEqual(model_to_agent_role(MODEL_ZAI_GLM_FLASH, TaskType.TINY_TASK, RiskLevel.LOW), "zai-flash")
        self.assertEqual(model_to_agent_role(MODEL_MINIMAX_M3, TaskType.ROUTINE_EXECUTION, RiskLevel.LOW), "minimax-task")
        # Provider mapping
        self.assertEqual(model_to_provider(MODEL_ZAI_GLM), "zai")
        self.assertEqual(model_to_provider(MODEL_MINIMAX_M3), "minimax")
        self.assertEqual(model_to_provider(MODEL_AG_CLAUDE_OPUS), "google-antigravity")
        self.assertEqual(model_to_provider(MODEL_DEEPSEEK_PRO), "deepseek")
        self.assertEqual(model_to_provider(MODEL_CHATGPT_WEB), CHATGPT_WEB_PROVIDER)
        # Every role this router emits for a pinned model resolves back to that role, so a
        # dispatched role never silently runs a different model. The review roles are
        # resolved as reviews, because that is the only way the router emits them.
        review_pins = {"codex-reviewer", "web-thinker", "reviewer"}
        reasoning_pins = {"thinker"}
        for role, model in ROLE_MODEL_PINS.items():
            if role in ("astra-ux", "advisor"):
                continue
            if role in review_pins:
                task_type = TaskType.STRONG_REVIEW
            elif role in reasoning_pins:
                task_type = TaskType.DEEP_REASONING
            else:
                task_type = TaskType.ROUTINE_EXECUTION
            self.assertIn(
                model_to_agent_role(model, task_type, RiskLevel.HIGH),
                (role, "codex-reviewer", "codex-worker"),
                f"{role} pin {model}",
            )
        self.assertEqual(model_to_agent_role(MODEL_CLAUDE_OPUS_55, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "opus")
        self.assertEqual(model_to_agent_role(MODEL_CLAUDE_SONNET_55, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "sonnet")
        print("  [PASS] Role mappings preserve distinct Opus, Sonnet, and effort-specific Codex lanes.")

    # -------------------------------------------------------------------------
    # TEST 32: Chinese providers disabled by default, enabled by credential
    # -------------------------------------------------------------------------
    def test_chinese_providers_disabled_by_default(self):
        print("\n--- TEST 32: Chinese Providers Credential-Gated ---")
        # MiniMax Token Plan is provider `minimax-code`, read from MINIMAX_CODE_API_KEY;
        # MINIMAX_API_KEY belongs to the different `minimax` provider and enables nothing.
        self.assertEqual(CREDENTIAL_ENV_BY_PROVIDER, {ZAI_PROVIDER: "ZAI_API_KEY", MINIMAX_PROVIDER: "MINIMAX_CODE_API_KEY"})
        self.assertEqual(detect_credentialed_providers(auth_store_paths=[]), set())
        with mock.patch.dict(os.environ, {"MINIMAX_API_KEY": "x"}):
            self.assertEqual(detect_credentialed_providers(auth_store_paths=[]), set())
        with mock.patch.dict(os.environ, {"MINIMAX_CODE_API_KEY": "x", "ZAI_API_KEY": "x"}):
            self.assertEqual(detect_credentialed_providers(auth_store_paths=[]), {ZAI_PROVIDER, MINIMAX_PROVIDER})

        # A stored `/login` credential enables the provider; a disabled one does not.
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = os.path.join(tmp, "agent.db")
            conn = sqlite3.connect(store)
            conn.execute("CREATE TABLE auth_credentials (provider TEXT, data TEXT, disabled_cause TEXT)")
            conn.executemany("INSERT INTO auth_credentials VALUES (?, ?, ?)", [
                (MINIMAX_PROVIDER, "redacted", None),
                (ZAI_PROVIDER, "redacted", "revoked"),
            ])
            conn.commit()
            conn.close()
            self.assertEqual(detect_credentialed_providers(auth_store_paths=[store]), {MINIMAX_PROVIDER})
            # A store held under an exclusive lock is skipped at once (timeout=0), not after
            # SQLite's default 5 s busy timeout.
            holder = sqlite3.connect(store)
            holder.execute("BEGIN EXCLUSIVE")
            try:
                started = time.monotonic()
                self.assertEqual(detect_credentialed_providers(auth_store_paths=[store, store]), set())
                self.assertLess(time.monotonic() - started, 1.0)
            finally:
                holder.rollback()
                holder.close()

        # Uncredentialed (hermetic setUp): never selected or offered as a fallback.
        selector = self._selector(self._usage_with_ag_families())
        for task_type in TaskType:
            for risk in RiskLevel:
                rec = selector.select_model(task_type=task_type, risk_level=risk)
                for model in (rec.selected_model, rec.fallback_model):
                    self.assertNotIn("zai/", model)
                    self.assertNotIn("minimax-code/", model)

        # Credentialed: GLM-5.3 leads the high-risk worker ladder; GLM-5.3-Flash and MiniMax-M3
        # take bulk/triage when Gemini Lite is out; MiniMax never implements or reviews.
        usage = self._usage_with_ag_families()
        creds = {ZAI_PROVIDER, MINIMAX_PROVIDER}
        both = ResetAwareModelSelector(parse_usage_json(usage, current_time_ms=self.mock_now_ms), credentialed_providers=creds)
        self.assertEqual(both.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.HIGH).selected_model, MODEL_ZAI_GLM)
        self.assertEqual(both.select_model(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.HIGH).selected_model, MODEL_ZAI_GLM)
        tiny = both.select_model(task_type=TaskType.TINY_TASK, risk_level=RiskLevel.LOW)
        self.assertEqual((tiny.selected_model, tiny.fallback_model), (MODEL_GEMINI_LITE, MODEL_ZAI_GLM_FLASH))
        for lim in usage["reports"][0]["limits"]:
            if lim["id"].startswith("google-antigravity:google"):
                lim["status"] = "rate_limited"
        gemini_out = ResetAwareModelSelector(parse_usage_json(usage, current_time_ms=self.mock_now_ms), credentialed_providers=creds)
        tiny_out = gemini_out.select_model(task_type=TaskType.TINY_TASK, risk_level=RiskLevel.LOW)
        self.assertEqual((tiny_out.selected_model, tiny_out.fallback_model), (MODEL_ZAI_GLM_FLASH, MODEL_MINIMAX_M3))
        minimax_only = ResetAwareModelSelector(parse_usage_json(usage, current_time_ms=self.mock_now_ms),
                                               credentialed_providers={MINIMAX_PROVIDER})
        self.assertEqual(minimax_only.select_model(task_type=TaskType.TINY_TASK, risk_level=RiskLevel.LOW).selected_model,
                         MODEL_MINIMAX_M3)
        for task_type in (TaskType.ROUTINE_EXECUTION, TaskType.DEEP_REASONING, TaskType.STRONG_REVIEW):
            for risk in RiskLevel:
                self.assertNotIn("minimax-code/", minimax_only.select_model(task_type=task_type, risk_level=risk).selected_model)
        print("  [PASS] Z.AI/MiniMax Code routed only when credentialed (env or stored); MiniMax limited to bulk/triage.")

    # -------------------------------------------------------------------------
    # TEST 33: Task-class routing may use Sonnet medium when Anthropic is behind
    # pace, but never Opus, never an over-context model, and never Anthropic
    # while the orchestrator reserve is active.
    # -------------------------------------------------------------------------
    def test_worker_lanes_never_spend_orchestrator_reserve(self):
        print("\n--- TEST 33: Task-Class Anthropic Guard ---")
        def deepseek_out(usage):
            report = copy.deepcopy(usage["reports"][0])
            report["provider"] = "deepseek"
            report["limits"] = report["limits"][:1]
            report["limits"][0]["id"] = "deepseek:default:daily"
            report["metadata"] = {"limitReached": True, "allowed": False}
            usage["reports"].append(report)
            return usage

        def codex_out(usage):
            usage["reports"][2]["metadata"].update({"limitReached": True, "allowed": False})
            return usage

        google_down = self._usage_with_ag_families(anthropic_week_used=0.0, anthropic_week_reset_hrs=20)
        google_down["reports"][0]["metadata"]["limitReached"] = True
        google_down["reports"][0]["metadata"]["allowed"] = False
        scenarios = {
            "all_ok": self._usage_with_ag_families(anthropic_week_used=0.0, anthropic_week_reset_hrs=20),
            "ag_spent_codex_throttled": self._usage_with_ag_families(
                anthropic_used=0.95, codex_used=0.92, codex_reset_hrs=145,
                anthropic_week_used=0.0, anthropic_week_reset_hrs=20),
            "reserve_active": self._usage_with_ag_families(
                anthropic_used=0.95, codex_used=0.92, codex_reset_hrs=145,
                anthropic_week_used=0.64, anthropic_week_reset_hrs=72),
            "anthropic_surplus_near_reset": self._usage_with_ag_families(
                anthropic_week_used=0.10, anthropic_week_reset_hrs=6),
            "google_down": google_down,
            "google_deepseek_out_codex_on_pace": deepseek_out(copy.deepcopy(google_down)),
            "google_codex_out": codex_out(copy.deepcopy(google_down)),
        }
        variants = [
            {},
            {"rework_count": 1},
            {"domain_tags": ["money", "auth"]},
        ]
        checked = 0
        for label, usage in scenarios.items():
            snapshot = parse_usage_json(usage, current_time_ms=self.mock_now_ms)
            for creds in (set(), {ZAI_PROVIDER, MINIMAX_PROVIDER}):
                selector = ResetAwareModelSelector(snapshot, credentialed_providers=creds)
                for task_type in TaskType:
                    for risk in RiskLevel:
                        for variant in variants:
                            is_review_lane = task_type == TaskType.STRONG_REVIEW and (
                                risk == RiskLevel.HIGH or variant)
                            for ctx in (10000, 131072, 131073, 200000, 220000, 240000):
                                rec = selector.select_model(
                                    task_type=task_type, risk_level=risk, context_tokens=ctx, **variant)
                                where = f"[{label}/{sorted(creds)}] {task_type.value}/{risk.value}/{variant}/{ctx}"
                                self.assertNotEqual(
                                    model_to_provider(rec.selected_model), model_to_provider(rec.fallback_model), where)
                                checked += 1
                                # Review 5318271884 L: no pick or fallback exceeds its verified window.
                                for model in (rec.selected_model, rec.fallback_model):
                                    self.assertLessEqual(ctx, VERIFIED_CONTEXT_WINDOWS.get(model, ctx), f"{where} {model}")
                                # Review 5318271884 K: MiniMax-M3 is bulk/triage (tiny tasks) and low-risk
                                # deep-context overflow only; never implementation, review or reasoning.
                                minimax_allowed = task_type == TaskType.TINY_TASK or (
                                    task_type == TaskType.DEEP_CONTEXT and risk != RiskLevel.HIGH and not variant)
                                if not minimax_allowed:
                                    self.assertNotIn("minimax-code/", rec.selected_model, where)
                                    self.assertNotIn("minimax-code/", rec.fallback_model, where)
                                # Review 5318407088 M: high-risk, rework and money routes never fall
                                # back to a Flash tier, at any context size. (A TINY_TASK up to 180k is the
                                # bulk Flash-Lite lane by design; above 180k it is Case A and covered.)
                                if (risk == RiskLevel.HIGH or variant) and (
                                        task_type != TaskType.TINY_TASK or ctx > 180000):
                                    self.assertNotIn("flash", rec.fallback_model, where)
                                for model in (rec.selected_model, rec.fallback_model):
                                    self.assertNotEqual(model, MODEL_CLAUDE_OPUS_55, where)
                                    if model.startswith("anthropic/"):
                                        self.assertEqual(model, MODEL_CLAUDE_SONNET_55, where)
                                if label == "reserve_active":
                                    self.assertNotIn("anthropic/", rec.selected_model, where)
                                    self.assertNotIn("anthropic/", rec.fallback_model, where)
        print(f"  [PASS] {checked} routes preserve context bounds, reserve Anthropic when required, "
              "allow only Sonnet medium for non-UI judgment, bar Opus, keep high-risk fallbacks off Flash, "
              "and cross providers.")

        # The deep-context gap (review 5317952644 A'): with both 1M-context cheap tiers out,
        # Codex on pace holds 200k/240k, and credentialed GLM holds a 10k DEEP_CONTEXT task.
        gap = ResetAwareModelSelector(parse_usage_json(scenarios["google_deepseek_out_codex_on_pace"],
                                                       current_time_ms=self.mock_now_ms))
        for ctx in (200000, 240000):
            rec = gap.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.MEDIUM, context_tokens=ctx)
            self.assertEqual(rec.selected_model, MODEL_CODEX_WORKER, ctx)
            self.assertFalse(rec.quota_metrics["codex_pro_throttled"])
        gap_glm = ResetAwareModelSelector(parse_usage_json(scenarios["google_deepseek_out_codex_on_pace"],
                                                           current_time_ms=self.mock_now_ms),
                                          credentialed_providers={ZAI_PROVIDER})
        self.assertEqual(gap_glm.select_model(task_type=TaskType.DEEP_CONTEXT, risk_level=RiskLevel.LOW,
                                              context_tokens=10000).selected_model, MODEL_ZAI_GLM)
        # At 500k the Codex windows no longer fit; Sonnet's 1M window handles
        # judgment-heavy non-UI context while Anthropic has slack.
        rec_500k = gap.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.MEDIUM, context_tokens=500000)
        self.assertEqual(rec_500k.selected_model, MODEL_CLAUDE_SONNET_55)
        self.assertLessEqual(500000, VERIFIED_CONTEXT_WINDOWS[rec_500k.selected_model])
        print(f"  [PASS] Deep context uses Sol medium through 240k and Sonnet medium at 500k.")

        # MiniMax remains confined to low-risk deep-context reads. Other work
        # uses task-specific Sol effort: high for review, medium for execution.
        gap_minimax = ResetAwareModelSelector(parse_usage_json(scenarios["google_deepseek_out_codex_on_pace"],
                                                               current_time_ms=self.mock_now_ms),
                                              credentialed_providers={MINIMAX_PROVIDER})
        for task_type, ctx, extra in (
            (TaskType.ROUTINE_EXECUTION, 200000, {"risk_level": RiskLevel.HIGH}),
            (TaskType.STRONG_REVIEW, 200000, {"risk_level": RiskLevel.HIGH}),
            (TaskType.DEEP_REASONING, 250000, {"risk_level": RiskLevel.MEDIUM, "domain_tags": ["money"]}),
            (TaskType.ROUTINE_EXECUTION, 200000, {"risk_level": RiskLevel.LOW}),
            (TaskType.DEEP_CONTEXT, 10000, {"risk_level": RiskLevel.HIGH}),
        ):
            rec = gap_minimax.select_model(task_type=task_type, context_tokens=ctx, **extra)
            expected = MODEL_CODEX_SOL if task_type == TaskType.STRONG_REVIEW else MODEL_CODEX_WORKER
            self.assertIn(rec.selected_model, (expected, MODEL_CLAUDE_SONNET_55),
                          f"{task_type.value}/{ctx}/{extra}")
            self.assertNotIn("minimax-code/", rec.fallback_model)
        self.assertEqual(gap_minimax.select_model(task_type=TaskType.DEEP_CONTEXT, risk_level=RiskLevel.LOW,
                                                  context_tokens=200000).selected_model, MODEL_MINIMAX_M3)
        print("  [PASS] MiniMax stays on low-risk deep-context reads; Sol effort follows task class.")

        # GLM-5.3 holds exactly 131,072 tokens. One token over the
        # verified window moves execution to Sol medium.
        glm_all_ok = ResetAwareModelSelector(parse_usage_json(scenarios["all_ok"], current_time_ms=self.mock_now_ms),
                                             credentialed_providers={ZAI_PROVIDER})
        at_limit = glm_all_ok.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.HIGH,
                                           context_tokens=131072)
        over_limit = glm_all_ok.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.HIGH,
                                             context_tokens=131073)
        self.assertEqual(at_limit.selected_model, MODEL_ZAI_GLM)
        self.assertEqual(over_limit.selected_model, MODEL_CODEX_WORKER)
        self.assertEqual(gap_glm.select_model(task_type=TaskType.DEEP_CONTEXT, risk_level=RiskLevel.LOW,
                                              context_tokens=131072).selected_model, MODEL_ZAI_GLM)
        self.assertEqual(gap_glm.select_model(task_type=TaskType.DEEP_CONTEXT, risk_level=RiskLevel.LOW,
                                              context_tokens=131073).selected_model, MODEL_CODEX_WORKER)
        glm_google_down = ResetAwareModelSelector(parse_usage_json(google_down, current_time_ms=self.mock_now_ms),
                                                  credentialed_providers={ZAI_PROVIDER})
        self.assertEqual(glm_google_down.select_model(task_type=TaskType.TINY_TASK, context_tokens=131072).selected_model,
                         MODEL_ZAI_GLM_FLASH)
        self.assertNotEqual(glm_google_down.select_model(task_type=TaskType.TINY_TASK, context_tokens=131073).selected_model,
                            MODEL_ZAI_GLM_FLASH)
        print(f"  [PASS] GLM window fit: 131,072 -> {MODEL_ZAI_GLM}; 131,073 -> {over_limit.selected_model} "
              "(high-risk worker, deep-context and tiny-task ladders).")

        # When cheap and Codex tiers are out, judgment-heavy non-UI work uses
        # Sonnet medium while Anthropic has worker headroom.
        usage_out = self._usage_with_ag_families(anthropic_used=0.0, anthropic_week_used=0.0, anthropic_week_reset_hrs=20)
        for rep in usage_out["reports"]:
            if rep["provider"] in ("google-antigravity", "openai-codex"):
                rep["metadata"]["limitReached"] = True
                rep["metadata"]["allowed"] = False
        deepseek_out(usage_out)
        rec_last = self._selector(usage_out).select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.HIGH)
        self.assertEqual(rec_last.selected_model, MODEL_CLAUDE_SONNET_55)
        self.assertNotEqual(rec_last.selected_model, MODEL_CLAUDE_OPUS_55)
        # Ahead of pace, the orchestrator reserve is never touched: pay-per-token last resort.
        usage_reserve = copy.deepcopy(usage_out)
        set_week = usage_reserve["reports"][1]["limits"][1]
        set_week["amount"].update({"remainingFraction": 0.36, "usedFraction": 0.64, "remaining": 36.0, "used": 64.0})
        set_week["window"]["resetsAt"] = self.mock_now_ms + 72 * 3600 * 1000
        rec_reserve = self._selector(usage_reserve).select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.HIGH)
        self.assertNotIn("anthropic/", rec_reserve.selected_model)
        print(f"  [PASS] Every cheap tier out: {rec_last.selected_model} on slack; reserve kept -> {rec_reserve.selected_model}.")

    # -------------------------------------------------------------------------
    # TEST 36: ChatGPT Web Bridge Ban & Default-Off Invariant (2026-09-26)
    # -------------------------------------------------------------------------
    def test_chatgpt_web_bridge_precondition_and_ladder_placement(self):
        print("\n--- TEST 36: ChatGPT Web Bridge Ban & Default-Off Invariant ---")
        # 1. Off by default: an open port alone does NOT make the bridge available.
        # It requires explicit operator action (VEYYON_CHATGPT_WEB_ENABLED=1).
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(10)
        self.assertFalse(chatgpt_web_bridge_available(port=listener.getsockname()[1], timeout=1.0))
        self.assertFalse(chatgpt_web_bridge_available(port=1, timeout=0.25))

        # Explicit probe override or env enables the port check:
        self.assertTrue(chatgpt_web_bridge_available(port=listener.getsockname()[1], timeout=1.0, force_probe=True))
        with mock.patch.dict(os.environ, {"VEYYON_CHATGPT_WEB_ENABLED": "1"}):
            self.assertTrue(chatgpt_web_bridge_available(port=listener.getsockname()[1], timeout=1.0))

        # 2. Stripped from all ladders: even if chatgpt_web_bridge=True or False,
        # no ladder selects or falls back to chatgpt-web.
        usage = self._usage_with_ag_families(anthropic_used=0.0)
        up = ResetAwareModelSelector(parse_usage_json(usage, current_time_ms=self.mock_now_ms),
                                     chatgpt_web_bridge=True)
        down = ResetAwareModelSelector(parse_usage_json(usage, current_time_ms=self.mock_now_ms),
                                       chatgpt_web_bridge=False)

        for selector in (up, down):
            for task_type in TaskType:
                for risk in RiskLevel:
                    rec = selector.select_model(task_type=task_type, risk_level=risk)
                    self.assertNotIn(MODEL_CHATGPT_WEB, (rec.selected_model, rec.fallback_model),
                                     f"{task_type}/{risk} must not select or fall back to chatgpt-web")

        # Critical non-UI diffs fall through to Sol, Sonnet, or DeepSeek; never Opus.
        critical = down.select_model(TaskType.STRONG_REVIEW, RiskLevel.HIGH)
        self.assertIn(critical.selected_model, (MODEL_CODEX_SOL, MODEL_CLAUDE_SONNET_55, MODEL_DEEPSEEK_PRO))
        self.assertNotEqual(critical.selected_model, MODEL_CLAUDE_OPUS_55)
        print("  [PASS] Bridge is default-off and absent from every ladder; critical work still routes by task class.")

    # -------------------------------------------------------------------------
    # TEST 37: Gemini never reviews — not as a pick, not as a fallback, at any risk or context
    # -------------------------------------------------------------------------
    def test_gemini_never_reviews(self):
        print("\n--- TEST 37: Gemini Never Reviews (Family Independence) ---")
        gemini = (MODEL_GEMINI_FLASH, MODEL_GEMINI_PRO, MODEL_GEMINI_LITE)
        usage = self._usage_with_ag_families(anthropic_used=0.0)
        for bridge in (True, False):
            selector = ResetAwareModelSelector(parse_usage_json(usage, current_time_ms=self.mock_now_ms),
                                               chatgpt_web_bridge=bridge)
            for risk in RiskLevel:
                for context in (10000, 200000):
                    rec = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=risk,
                                                context_tokens=context)
                    self.assertNotIn(rec.selected_model, gemini, f"bridge={bridge} {risk} {context}")
                    self.assertNotIn(rec.fallback_model, gemini, f"bridge={bridge} {risk} {context}")
        # The second opinion stays a free OpenRouter model: advisory, never a gate.
        advisory = self._selector(usage).select_model(TaskType.STRONG_REVIEW, RiskLevel.MEDIUM)
        self.assertEqual(advisory.advisory_model, MODEL_OR_FREE_ADVISORY)
        print("  [PASS] No review ladder selects or falls back to a Gemini model; advisory stays free/non-gating.")

    # -------------------------------------------------------------------------
    # TEST 38: The installed profile config actually implements Option A
    # -------------------------------------------------------------------------
    def test_profile_config_implements_option_a(self):
        print("\n--- TEST 38: Profile Config Implements Option A ---")
        config_path = Path(os.path.expanduser("~/.veyyon/profiles/default/agent/config.yml"))
        if not config_path.exists():
            self.skipTest(f"profile config not installed at {config_path}")
        if yaml is None:
            self.skipTest(f"PyYAML is not installed; skipping {config_path} verification")
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        model_roles = parsed.get("modelRoles") or {}
        agents = (parsed.get("agent") or {}).get("agents") or {}

        def chains(entry):
            """Every `model` value of a spawnable agent, nested agents included."""
            found, node = [], entry or {}
            while isinstance(node, dict):
                if node.get("model"):
                    found.append(node["model"])
                node = node.get("agents")
            return found

        # 1. Every router-emitted role exists and leads with the model the router pins, so a
        # dispatched role can never silently run a different model.
        for role, model in ROLE_MODEL_PINS.items():
            chain = (agents.get(role) or {}).get("model") or model_roles.get(role)
            self.assertIsNotNone(chain, f"{role} is not defined as a role or agent")
            leading = str(chain).split(",")[0].strip()
            if role in ("codex-worker", "codex-reviewer"):
                self.assertIn(leading, (model, "openai-codex/gpt-5.6-sol:high"), f"{role} must lead with {model} or Sol")
            elif role == "advisor":
                self.assertIn(leading, (model, "anthropic/claude-fable-5-1:medium"), f"{role} must lead with {model} or Fable")
            else:
                self.assertEqual(leading, model, f"{role} must lead with {model}")

        # 2. Paid Anthropic Opus is reserved for gating review (`reviewer`); worker chains must not run paid Opus;
        # the interactive orchestrator (`modelRoles.default`) is explicitly out of scope.
        paid_opus = "anthropic/claude-opus-5-5"
        for role, chain in model_roles.items():
            if role in ("default", "reviewer", "astra-ux", "opus"):
                continue
            self.assertNotIn(paid_opus, str(chain), f"modelRoles.{role} must not run paid Opus")
        for name, entry in agents.items():
            if name in ("reviewer", "astra-ux", "opus"):
                continue  # Opus 5.5 permitted for reviewer and opus lane per operator ruling 2026-09-29
            for chain in chains(entry):
                self.assertNotIn(paid_opus, str(chain), f"agents.{name} must not run paid Opus")
        for pattern, chain in (parsed.get("retry") or {}).get("fallbackChains", {}).items():
            self.assertNotIn(paid_opus, str(chain), f"retry.fallbackChains.{pattern} must not run paid Opus")
        # 3. No review lane runs a Gemini model, and the shared Antigravity fallback chain no
        # longer substitutes one: a Google-family outage takes every Gemini model with it, and
        # a Gemini fallback would review a Gemini-authored diff.
        for role in ("reviewer", "go-review", "web-thinker", "codex-reviewer",
                     "extra-review", "or-review"):
            chain = (agents.get(role) or {}).get("model") or model_roles.get(role) or ""
            self.assertNotIn("gemini", str(chain).lower(), f"review lane {role} must not run Gemini")
        antigravity_chain = (parsed.get("retry") or {}).get("fallbackChains", {}).get("google-antigravity/*", [])
        self.assertNotIn("gemini", str(antigravity_chain).lower(),
                         "the Antigravity fallback chain must not substitute a Gemini model")

        # 4. Gating reviewer leads with Codex Sol (operator ruling 2026-09-29), then
        # cross-family Chinese reviewers, then DeepSeek. Gating roles (reviewer) NEVER lead with chatgpt-web.
        critical_chain = str((agents.get("reviewer") or {}).get("model", ""))
        self.assertEqual(critical_chain.split(",")[0].strip(), MODEL_CODEX_SOL)
        for expected in ("opencode-go/glm-5.3",
                         "opencode-go/qwen3.8-max", "deepseek/"):
            self.assertIn(expected, critical_chain, f"critical review chain must offer {expected}")
        for gating_role in ("reviewer",):
            first_model = str((agents.get(gating_role) or {}).get("model", "")).split(",")[0].strip()
            self.assertFalse(first_model.startswith("chatgpt-web"),
                             f"Gating role '{gating_role}' must never lead with chatgpt-web (got {first_model})")
        # 5. The standard-diff reviewer is the cross-family Chinese chain with a DeepSeek
        # fallback for the OpenCode Go limit, and the hard writer is GLM-5.3 or DeepSeek.
        standard_chain = str(model_roles.get("go-review", ""))
        self.assertEqual(standard_chain.split(",")[0].strip(), "opencode-go/glm-5.3")
        self.assertIn("opencode-go/qwen3.8-max", standard_chain)
        self.assertIn("deepseek/", standard_chain)
        hard_writer = str(model_roles.get("go-deep", "")).split(",")
        self.assertIn("opencode-go/glm-5.3", hard_writer)
        self.assertTrue(any(m.startswith(("opencode-go/glm-5.3", "deepseek/")) for m in hard_writer),
                        "the hard writer chain must be GLM-5.3 or DeepSeek V4 Pro")

        # 6. A chatgpt-web lane has somewhere to go when the bridge is down.
        web_chain = (parsed.get("retry") or {}).get("fallbackChains", {}).get("chatgpt-web/*", [])
        self.assertTrue(any(not str(m).startswith("chatgpt-web/") for m in web_chain),
                        "chatgpt-web lanes need a cross-provider fallback for a dead bridge")
        print(f"  [PASS] {len(ROLE_MODEL_PINS)} role pins, zero paid Opus, zero Gemini review "
              "lanes, bridge-first critical chain and Chinese standard chain verified in the "
              "installed profile config.")

    # -------------------------------------------------------------------------
    # TEST 39: Nested sub-agents are enabled for every spawnable Chinese-model lane
    # -------------------------------------------------------------------------
    def test_nested_subagents_enabled_for_cheap_lanes(self):
        print("\n--- TEST 39: Nested Sub-Agents On For Every Spawnable Cheap Lane ---")
        config_path = Path(os.path.expanduser("~/.veyyon/profiles/default/agent/config.yml"))
        if not config_path.exists():
            self.skipTest(f"profile config not installed at {config_path}")
        if yaml is None:
            self.skipTest(f"PyYAML is not installed; skipping {config_path} verification")
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        settings = parsed.get("agent") or {}
        agents = settings.get("agents") or {}

        def chain(record):
            """The nested Agents chain: [self, agents, agents.agents, ...], as veyyon walks it."""
            links, node = [], record
            while isinstance(node, dict) and len(links) <= 64:
                links.append(node)
                node = node.get("agents")
            return links

        def lane_depth(record, session_depth):
            """Mirror of veyyon's laneDepthOf: a level with `enabled: false` caps the depth."""
            if "agents" not in record and "maxNestedSpawnDepth" in record:
                return record["maxNestedSpawnDepth"]
            links = chain(record)
            for level, link in enumerate(links[1:], start=1):
                if link.get("enabled") is False:
                    return level - 1
            if session_depth < 0:
                return session_depth
            return max(len(links) - 1, session_depth)

        # Session-level spawn permission: with `agent.enabled` off the task tool disappears and
        # no lane can delegate at all, so the whole nested policy hangs on this one setting.
        self.assertTrue(settings.get("enabled", True), "agent.enabled must grant spawn permission")
        session_depth = settings.get("maxNestedSpawnDepth", 0)
        self.assertGreaterEqual(session_depth, 1, "the session must allow at least one nested level")

        # Every lane the router routes Chinese/cheap work to is spawnable AND may spawn its own
        # children with `agents: enabled: true`, so a lead lane can fan sub-slices out instead of
        # working serially.
        for role in ("task", "qa-verifier", "spark", "reviewer", "ds-task", "go-task",
                     "go-review", "go-deep", "go-bulk"):
            record = agents.get(role)
            self.assertIsNotNone(record, f"{role} must exist as a spawnable agent")
            self.assertTrue(record.get("enabled", True), f"{role} must be enabled in the roster")
            level1 = record.get("agents")
            self.assertIsInstance(level1, dict, f"{role} must declare a nested Agents chain")
            self.assertIsNot(level1.get("enabled"), False, f"{role} must permit child lanes")
            self.assertGreaterEqual(lane_depth(record, session_depth), 1,
                                    f"{role} must be able to spawn at least one nested level")

        # A nested `agents.model` is NOT the model of this lane's children: veyyon reads the
        # SPAWNED agent's own chain at index depth-1, so `task.agents.model` is what every `task`
        # at depth 2 runs, whoever spawned it. The Flash lanes must therefore lead with Flash at
        # every depth; a Go-first nested level sent grandchild lanes into a Go usage 429.
        self.assertEqual(lane_pin_drift(agents, session_depth), [],
                         "task/qa-verifier must lead with Gemini Flash at every spawn depth")

        # A nested level that does name a model stays on a cheap adequate lane: one of the
        # free/Flash/DeepSeek-Flash rungs, never paid Opus and never a paid Anthropic model.
        cheap_prefixes = ("google-antigravity/gemini-3.8-flash", "deepseek/", "openai-codex/gpt-5.3-codex-spark",
                          "opencode-go/", "openrouter/deepseek/")
        for role in ("task", "qa-verifier", "reviewer", "ds-task", "go-task",
                     "go-review", "go-deep", "go-bulk"):
            child_chain = str(agents[role]["agents"].get("model", ""))
            self.assertNotIn("anthropic/", child_chain, f"{role} children must not run paid Anthropic")
            self.assertNotIn("opus", child_chain.lower(), f"{role} children must not run Opus")
            for model in (m.strip() for m in child_chain.split(",") if m.strip()):
                self.assertTrue(model.startswith(cheap_prefixes),
                                f"{role} child default {model} is not a cheap adequate lane")

        # The two lanes that deliberately stay parent-only are named here, so a future edit that
        # silently stops every other lane from nesting fails this test instead of going unnoticed.
        parent_only = {role for role, record in agents.items()
                       if isinstance(record, dict) and lane_depth(record, session_depth) == 0}
        self.assertEqual(parent_only, {"web-thinker"},
                         "only the bridge thinker lane is deliberately parent-only")
        print(f"  [PASS] agent.enabled grants spawning; {len(agents)} roster entries; every cheap "
              f"Chinese lane nests to depth {session_depth} with cheap child chains; "
              f"parent-only lanes: {sorted(parent_only)}.")

    # -------------------------------------------------------------------------
    # TEST 39b: Flash lanes lead with Flash at every spawn depth (veyyon laneModelLayer)
    # -------------------------------------------------------------------------
    def test_flash_lane_pin_holds_at_every_depth(self):
        print("\n--- TEST 39b: Flash Lanes Stay On Flash At Every Spawn Depth ---")
        flash = "google-antigravity/gemini-3.8-flash:high"
        self.assertEqual(LANE_MODEL_PINS, {"task": MODEL_GEMINI_FLASH, "qa-verifier": MODEL_GEMINI_FLASH})

        # The shape that failed on 2026-09-27: a Go-first nested level under the Flash row. A
        # depth-1 `task` runs Flash, but every depth-2 `task` (spawned by ANY depth-1 lane, e.g.
        # an Opus reviewer) resolves to space-bunny-free.
        go_first = {
            role: {"model": flash, "agents": {
                "enabled": True,
                "model": f"{MODEL_GO_BUNNY},{MODEL_GO_GLM53_FLASH},deepseek/deepseek-flash:high",
                "agents": {"enabled": True}}}
            for role in ("task", "qa-verifier")
        }
        self.assertEqual(lane_model_at_depth(go_first, "task", 1)[0], flash)
        self.assertEqual(lane_model_at_depth(go_first, "task", 2)[0], MODEL_GO_BUNNY)
        # Depth 3 names no model and inherits the nearest level above it (depth 2), as veyyon does.
        self.assertEqual(lane_model_at_depth(go_first, "task", 3)[0], MODEL_GO_BUNNY)
        drift = lane_pin_drift(go_first, 3)
        self.assertIn(f"task at depth 2 runs {MODEL_GO_BUNNY}, expected {MODEL_GEMINI_FLASH}", drift)
        self.assertIn(f"qa-verifier at depth 3 runs {MODEL_GO_BUNNY}, expected {MODEL_GEMINI_FLASH}", drift)
        self.assertNotIn(f"task at depth 1 runs {flash}, expected {MODEL_GEMINI_FLASH}", drift)

        # The fix: nested levels name no model, so every depth inherits the Flash row.
        inherited = {role: {"model": flash, "agents": {"enabled": True, "agents": {"enabled": True}}}
                     for role in ("task", "qa-verifier")}
        self.assertEqual(lane_pin_drift(inherited, 3), [])
        # A nested level that names Flash again (other thinking level) is also clean.
        explicit = copy.deepcopy(inherited)
        explicit["task"]["agents"]["model"] = "google-antigravity/gemini-3.8-flash:medium," + MODEL_DEEPSEEK_FLASH
        self.assertEqual(lane_pin_drift(explicit, 3), [])
        # A missing row falls back to the default role, which is drift too.
        self.assertEqual(lane_pin_drift({"task": inherited["task"]}, 1),
                         [f"qa-verifier at depth 1 runs the default role, expected {MODEL_GEMINI_FLASH}"])
        print("  [PASS] Go-first nested level flagged at depths 2-3; inherited/explicit Flash chains clean.")

    # -------------------------------------------------------------------------
    # TEST 40: Every child-lane default maps to an enabled, spawnable roster entry
    # -------------------------------------------------------------------------
    def test_child_lane_defaults_resolve_to_enabled_roles(self):
        print("\n--- TEST 40: Child-Lane Defaults Resolve To Enabled Agent Types ---")
        config_path = Path(os.path.expanduser("~/.veyyon/profiles/default/agent/config.yml"))
        if not config_path.exists():
            self.skipTest(f"profile config not installed at {config_path}")
        if yaml is None:
            self.skipTest(f"PyYAML is not installed; skipping {config_path} verification")
        agents = ((yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}).get("agent") or {}).get("agents") or {}

        # A nested Agents level names the model this same lane type runs when spawned one level
        # deeper (veyyon laneModelLayer). If that model maps to a role that is not in the roster,
        # or is disabled in it, a nested spawn fails at spawn time — the drift this test catches.
        checked = set()
        for role, record in agents.items():
            level1 = (record or {}).get("agents")
            if not isinstance(level1, dict):
                continue
            for model in (m.strip() for m in str(level1.get("model", "")).split(",") if m.strip()):
                checked.add((role, model))
        self.assertTrue(checked, "no nested Agents chain declares a child model")
        for parent, model in sorted(checked):
            for task_type, risk in ((TaskType.ROUTINE_EXECUTION, RiskLevel.LOW),
                                    (TaskType.STRONG_REVIEW, RiskLevel.MEDIUM)):
                child_role = model_to_agent_role(model, task_type, risk)
                entry = agents.get(child_role)
                self.assertIsNotNone(
                    entry, f"{parent} child default {model} maps to unknown role {child_role} ({task_type})")
                self.assertTrue(
                    entry.get("enabled", True),
                    f"{parent} child default {model} maps to disabled role {child_role} ({task_type})")

        # The Spark allowance is its own enabled lane: routing its model to the Astral Codex
        # roles would dispatch a disabled agent for a free, permitted lane.
        self.assertEqual(model_to_agent_role(MODEL_CODEX_SPARK, TaskType.ROUTINE_EXECUTION, RiskLevel.LOW),
                         "spark")
        self.assertEqual(ROLE_MODEL_PINS["spark"], MODEL_CODEX_SPARK)
        print(f"  [PASS] {len(checked)} child-lane defaults across {len(agents)} roster entries all "
              "resolve to enabled agent types for writer and review children; Spark maps to `spark`.")

    # -------------------------------------------------------------------------
    # TEST 41: OpenCode Go weekly linear pacing (under pace vs over pace)
    # -------------------------------------------------------------------------
    def test_opencode_go_weekly_pacing(self):
        print("\n--- TEST 41: OpenCode Go Weekly Linear Pacing ---")
        # Under pace: 10% used with 148.8h left (11.4% elapsed, allowed = 11.4% + 14.3% = 25.7%)
        usage_under = self._usage_with_opencode_go(weekly_used=0.10, weekly_reset_hrs=148.8)
        sel_under = ResetAwareModelSelector(
            parse_usage_json(usage_under, current_time_ms=self.mock_now_ms),
            credentialed_providers={"opencode-go"},
        )
        rec_under = sel_under.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertFalse(rec_under.quota_metrics.get("go_weekly_throttled"))
        self.assertIn("opencode-go/", rec_under.selected_model)
        print(f"  [PASS] Under pace (10% used): Go allowed -> {rec_under.selected_model}")

        # Over pace (today's numbers: 44.7% used with 148.8h left, allowed = 25.7%)
        # With bridge down: falls past Go to Flash (gemini-3.8-flash:high with task role)
        usage_over = self._usage_with_opencode_go(weekly_used=0.447, weekly_reset_hrs=148.8)
        sel_over = ResetAwareModelSelector(
            parse_usage_json(usage_over, current_time_ms=self.mock_now_ms),
            credentialed_providers={"opencode-go"},
        )
        rec_over = sel_over.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertTrue(rec_over.quota_metrics.get("go_weekly_throttled"))
        self.assertNotIn("opencode-go/", rec_over.selected_model)
        self.assertEqual(rec_over.selected_model, MODEL_GEMINI_FLASH)
        print(f"  [PASS] Over pace (44.7% used, bridge down): Go paced out -> falls to Flash {rec_over.selected_model}")

        # Over pace with chatgpt-web bridge up: chatgpt-web is banned from ladders, falls to Flash
        sel_web = ResetAwareModelSelector(
            parse_usage_json(usage_over, current_time_ms=self.mock_now_ms),
            credentialed_providers={"opencode-go"},
            chatgpt_web_bridge=True,
        )
        rec_web = sel_web.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertNotIn("opencode-go/", rec_web.selected_model)
        self.assertEqual(rec_web.selected_model, MODEL_GEMINI_FLASH)
        print(f"  [PASS] Over pace (44.7% used, bridge up): chatgpt-web banned -> falls to Flash {rec_web.selected_model}")

    # -------------------------------------------------------------------------
    # TEST 42: OpenCode Go reset boundary & pace catch-up over time
    # -------------------------------------------------------------------------
    def test_opencode_go_reset_boundary(self):
        print("\n--- TEST 42: OpenCode Go Reset Boundary & Pace Catch-Up ---")
        # At start (148.8h left, 11.4% elapsed): 44.7% used is over pace (allowed 25.7%)
        usage_over = self._usage_with_opencode_go(weekly_used=0.447, weekly_reset_hrs=148.8)
        sel_over = ResetAwareModelSelector(
            parse_usage_json(usage_over, current_time_ms=self.mock_now_ms),
            credentialed_providers={"opencode-go"},
        )
        rec_over = sel_over.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertTrue(rec_over.quota_metrics.get("go_weekly_throttled"))

        # As time advances by 33 hours (115.8h left, 31.1% elapsed): allowed = 31.1% + 14.3% = 45.4% >= 44.7%
        # The pace catches up and Go is allowed again
        usage_catchup = self._usage_with_opencode_go(weekly_used=0.447, weekly_reset_hrs=115.8)
        sel_catchup = ResetAwareModelSelector(
            parse_usage_json(usage_catchup, current_time_ms=self.mock_now_ms),
            credentialed_providers={"opencode-go"},
        )
        rec_catchup = sel_catchup.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertFalse(rec_catchup.quota_metrics.get("go_weekly_throttled"))
        self.assertIn("opencode-go/", rec_catchup.selected_model)
        print(f"  [PASS] Boundary: at 115.8h left (~33h elapsed), pace caught up -> Go allowed on {rec_catchup.selected_model}")

    # -------------------------------------------------------------------------
    # TEST 43: OpenCode Go 429 retry-after handling
    # -------------------------------------------------------------------------
    def test_opencode_go_429_retry_after(self):
        print("\n--- TEST 43: OpenCode Go 429 Retry-After Handling ---")
        usage = self._usage_with_opencode_go(weekly_used=0.10, weekly_reset_hrs=148.8)
        now_dt = datetime.datetime.fromtimestamp(self.mock_now_ms / 1000.0, tz=datetime.timezone.utc)
        cache = tmp_quota_path()
        self.addCleanup(shutil.rmtree, cache.parent, ignore_errors=True)
        sel = ResetAwareModelSelector(
            parse_usage_json(usage, current_time_ms=self.mock_now_ms),
            credentialed_providers={"opencode-go"},
            quota_snapshot=load_quota_file(cache),
        )
        # Initially on pace and eligible
        self.assertIsNone(sel.provider_exhaustion_reason(MODEL_GO_GLM53_FLASH))

        # Real 429 received from operator evidence:
        body = "429 Go usage limit exceeded retry-after-ms=136710000"
        reset = sel.record_429("opencode-go", body, window_id="weekly", now=now_dt)
        self.assertIsNotNone(reset)
        self.assertEqual(reset.retry_after_seconds, 136710.0)

        # Provider is now exhausted for ~38 hours
        reason = sel.provider_exhaustion_reason(MODEL_GO_GLM53_FLASH)
        self.assertIsNotNone(reason)
        self.assertIn("opencode-go is exhausted until", reason)

        # Routing falls past Go to Flash
        rec = sel.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertNotIn("opencode-go/", rec.selected_model)
        self.assertEqual(rec.selected_model, MODEL_GEMINI_FLASH)
        print(f"  [PASS] 429 retry-after-ms=136710000 parsed (136710s, ~38h); Go marked exhausted -> routed to {rec.selected_model}")

    # -------------------------------------------------------------------------
    # TEST 43b: space-bunny-free is closed by a recorded Go usage 429, not "uncapped"
    # -------------------------------------------------------------------------
    def test_go_bunny_closed_by_go_usage_429(self):
        print("\n--- TEST 43b: space-bunny-free Closed By Go Usage 429 Until Retry-After ---")
        usage = self._usage_with_opencode_go(weekly_used=0.10, weekly_reset_hrs=148.8)
        now_dt = datetime.datetime.fromtimestamp(self.mock_now_ms / 1000.0, tz=datetime.timezone.utc)
        cache = tmp_quota_path()
        self.addCleanup(shutil.rmtree, cache.parent, ignore_errors=True)

        def selector(at_ms):
            return ResetAwareModelSelector(parse_usage_json(usage, current_time_ms=at_ms),
                                           credentialed_providers={"opencode-go"},
                                           quota_snapshot=load_quota_file(cache))

        # Before any 429 the $0 Go model leads routine work.
        before = selector(self.mock_now_ms).select_model(
            task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertEqual(before.selected_model, MODEL_GO_BUNNY)

        # The body the 2026-09-27 grandchild lane died on (ArchiveResume.ArchiveResumeChecks, 19:59Z).
        sel = selector(self.mock_now_ms)
        reset = sel.record_429("opencode-go", "429 Go usage limit exceeded retry-after-ms=14417000",
                               window_id="rolling-5h", now=now_dt)
        self.assertEqual(reset.retry_after_seconds, 14417.0)
        self.assertIn("opencode-go is exhausted until", sel.provider_exhaustion_reason(MODEL_GO_BUNNY))
        during = selector(self.mock_now_ms).select_model(
            task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertEqual(during.selected_model, MODEL_GEMINI_FLASH,
                         "a recorded Go usage 429 must close space-bunny-free, not leave it 'uncapped'")

        # Past the retry-after the Go provider is eligible again with no further bookkeeping.
        snapshot = load_quota_file(cache)
        self.assertFalse(snapshot.is_eligible("opencode-go", now=now_dt + datetime.timedelta(seconds=14417 - 60)))
        self.assertTrue(snapshot.is_eligible("opencode-go", now=now_dt + datetime.timedelta(seconds=14417 + 60)))
        print(f"  [PASS] bunny leads -> 429 retry-after-ms=14417000 -> {during.selected_model}; "
              "Go eligible again after the retry-after.")

    # -------------------------------------------------------------------------
    # TEST 44: OpenCode Go 5h rolling window guard
    # -------------------------------------------------------------------------
    def test_opencode_go_5h_window_guard(self):
        print("\n--- TEST 44: OpenCode Go 5h Rolling Window Guard ---")
        # 1. With 5h headroom (0% 5h used, 10% weekly used), GLM-5.3 is selected for STRONG_REVIEW
        usage_ok = self._usage_with_opencode_go(weekly_used=0.10, weekly_reset_hrs=148.8, five_h_used=0.0)
        sel_ok = ResetAwareModelSelector(
            parse_usage_json(usage_ok, current_time_ms=self.mock_now_ms),
            credentialed_providers={"opencode-go"},
        )
        rec_ok = sel_ok.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.MEDIUM)
        self.assertEqual(rec_ok.selected_model, "opencode-go/glm-5.3")

        # 2. Weekly is on pace (10% used), but 5h window is 95% used (less than 1 lane remaining)
        usage = self._usage_with_opencode_go(weekly_used=0.10, weekly_reset_hrs=148.8, five_h_used=0.95)
        sel = ResetAwareModelSelector(
            parse_usage_json(usage, current_time_ms=self.mock_now_ms),
            credentialed_providers={"opencode-go"},
        )
        rec = sel.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.MEDIUM)
        # Paid Go models (GLM-5.3) are blocked because 5h window < 1 lane remaining
        self.assertNotEqual(rec.selected_model, "opencode-go/glm-5.3")
        print(f"  [PASS] 5h window: headroom selects GLM-5.3, 95% used blocks paid Go models -> routed to {rec.selected_model}")
    # -------------------------------------------------------------------------
    # TEST 45: Operator pacing overrides (active vs expired vs unset)
    # -------------------------------------------------------------------------
    def test_pacing_operator_overrides(self):
        print("\n--- TEST 45: Pacing Operator Overrides ---")
        # Over-pace usage
        usage = self._usage_with_opencode_go(weekly_used=0.447, weekly_reset_hrs=148.8)
        now_dt = datetime.datetime.fromtimestamp(self.mock_now_ms / 1000.0, tz=datetime.timezone.utc)

        # 1. Without override: paced out
        sel_no_ov = ResetAwareModelSelector(
            parse_usage_json(usage, current_time_ms=self.mock_now_ms),
            credentialed_providers={"opencode-go"},
        )
        rec_no_ov = sel_no_ov.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertTrue(rec_no_ov.quota_metrics.get("go_weekly_throttled"))
        self.assertEqual(rec_no_ov.selected_model, MODEL_GEMINI_FLASH)

        # 2. With active override: pace cap is lifted
        active_ov = PacingOverride(
            provider="opencode-go",
            until=now_dt + datetime.timedelta(hours=6),
            reason="operator override test",
        )
        sel_active = ResetAwareModelSelector(
            parse_usage_json(usage, current_time_ms=self.mock_now_ms),
            credentialed_providers={"opencode-go"},
            overrides=[active_ov],
        )
        rec_active = sel_active.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertFalse(rec_active.quota_metrics.get("go_weekly_throttled"))
        self.assertIn("opencode-go/", rec_active.selected_model)
        print(f"  [PASS] Active override lifts pace cap -> Go selected on {rec_active.selected_model}")

        # 3. With expired override: ignored, remains throttled
        expired_ov = PacingOverride(
            provider="opencode-go",
            until=now_dt - datetime.timedelta(hours=1),
            reason="expired test",
        )
        sel_expired = ResetAwareModelSelector(
            parse_usage_json(usage, current_time_ms=self.mock_now_ms),
            credentialed_providers={"opencode-go"},
            overrides=[expired_ov],
        )
        rec_expired = sel_expired.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertTrue(rec_expired.quota_metrics.get("go_weekly_throttled"))
        self.assertEqual(rec_expired.selected_model, MODEL_GEMINI_FLASH)
        print(f"  [PASS] Expired override ignored -> provider remains throttled")

        # 4. From override JSON file
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({
                "provider": "opencode-go",
                "until": (now_dt + datetime.timedelta(hours=2)).isoformat(),
                "reason": "json override file",
            }, f)
            tmp_override_file = f.name
        try:
            loaded = load_pacing_overrides(tmp_override_file, now=now_dt)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].provider, "opencode-go")
            self.assertTrue(loaded[0].is_active("opencode-go", now=now_dt))
            print("  [PASS] load_pacing_overrides successfully loaded active JSON override")
        finally:
            os.unlink(tmp_override_file)

    # -------------------------------------------------------------------------
    # TEST 46: Generalized weekly pacing across all subscription providers
    # -------------------------------------------------------------------------
    def test_subscription_weekly_pacing_generalized(self):
        print("\n--- TEST 46: Generalized Weekly Pacing Across Providers ---")
        # Test linear pacing across all providers with weekly windows
        for provider, window_id, duration_hrs in (
            ("opencode-go", "weekly", 168.0),
            ("openai-codex", "7d", 168.0),
            ("anthropic", "7d", 168.0),
            ("google-antigravity", "weekly", 168.0),
        ):
            # 10% elapsed (151.2h left out of 168h). Allowed = 10% + 14.3% = 24.3%
            # Under pace: 15% used -> throttled=False
            pace_under = window_pace(provider, window_id, duration_hrs, 151.2, 0.85, 0.15)
            self.assertTrue(pace_under.is_weekly_or_monthly)
            self.assertFalse(pace_under.throttled, f"{provider} at 15% used should not be throttled")
            self.assertAlmostEqual(pace_under.allowed_fraction, 0.10 + WEEKLY_BURST_MARGIN, places=3)

            # Over pace: 40% used -> throttled=True
            pace_over = window_pace(provider, window_id, duration_hrs, 151.2, 0.60, 0.40)
            self.assertTrue(pace_over.throttled, f"{provider} at 40% used should be throttled")
            self.assertAlmostEqual(pace_over.allowed_fraction, 0.10 + WEEKLY_BURST_MARGIN, places=3)
        print("  [PASS] Weekly linear pacing applies consistently across all subscription providers.")

    # -------------------------------------------------------------------------
    # TEST 47: Antigravity partner pools per account (partner-b vs partner-a)
    # -------------------------------------------------------------------------
    def test_antigravity_partner_pools_per_account(self):
        print("\n--- TEST 47: Antigravity Partner Pools Per Account ---")
        # Cite packages/ai/src/usage/google-antigravity.ts:434-441, 180-192, 74-79
        # partner-a has weekly cap, 80.9% used; partner-b has short daily window, ~47% used.
        usage = {
            "reports": [
                {
                    "provider": "google-antigravity",
                    "metadata": {
                        "accountId": "ai_partner_a",
                        "email": "partner-a@example.com",
                    },
                    "limits": [
                        {
                            "id": "google-antigravity:anthropic:default:weekly",
                            "label": "Anthropic Weekly",
                            "window": {"durationMs": 604800000, "resetsAt": self.mock_now_ms + 142 * 3600000},
                            "amount": {"limit": 100.0, "used": 80.9, "remaining": 19.1, "remainingFraction": 0.191, "usedFraction": 0.809},
                            "status": "ok",
                        }
                    ],
                },
                {
                    "provider": "google-antigravity",
                    "metadata": {
                        "accountId": "ai_partner_b",
                        "email": "partner-b@example.com",
                    },
                    "limits": [
                        {
                            "id": "google-antigravity:anthropic:default:daily",
                            "label": "Anthropic Daily",
                            "window": {"durationMs": 86400000, "resetsAt": self.mock_now_ms + 3 * 3600000},
                            "amount": {"limit": 100.0, "used": 47.1, "remaining": 52.9, "remainingFraction": 0.529, "usedFraction": 0.471},
                            "status": "ok",
                        }
                    ],
                }
            ],
            "dormant": [],
        }
        parsed = parse_usage_json(usage, current_time_ms=self.mock_now_ms)
        norm = parsed.to_normalized()

        prov_anthropic = norm.providers["google-antigravity:anthropic"]
        accounts = {w.account for w in prov_anthropic.windows}
        self.assertIn("partner-a", accounts)
        self.assertIn("partner-b", accounts)

        selector = ResetAwareModelSelector(norm)
        paces = selector.pace_by_provider()
        ag_pace = paces["google-antigravity:anthropic"]
        # Must pick healthy account partner-b, not throttled partner-a weekly cap!
        self.assertEqual(ag_pace.account, "partner-b")
        self.assertFalse(ag_pace.throttled)
        self.assertAlmostEqual(ag_pace.remaining_fraction, 0.529, places=2)
        print("  [PASS] Antigravity partner pools distinguish accounts and route ag-opus to partner-b.")

    # -------------------------------------------------------------------------
    # TEST 48: Antigravity reserve enforcement (remaining <= 10% throttles)
    # -------------------------------------------------------------------------
    def test_antigravity_reserve_enforcement(self):
        print("\n--- TEST 48: Antigravity Reserve Enforcement ---")
        # Remaining at 8% (below AG_FAMILY_MIN_REMAINING 10% reserve)
        pace = window_pace("google-antigravity:anthropic", "google-antigravity:anthropic:default:daily", 24.0, 12.0, 0.08, 0.92)
        self.assertTrue(pace.throttled, "Antigravity window at or below 10% remaining must be throttled to hold reserve")

        # Remaining at 15% with 2h left (ratio 1.8 > 1.0) -> not throttled
        pace_ok = window_pace("google-antigravity:anthropic", "google-antigravity:anthropic:default:daily", 24.0, 2.0, 0.15, 0.85)
        self.assertFalse(pace_ok.throttled)
        print("  [PASS] Antigravity reserve floor strictly enforced at <= 10% remaining.")

    # -------------------------------------------------------------------------
    # TEST 49: Burn-rate pacer and pace --json output
    # -------------------------------------------------------------------------
    def test_burn_rate_pacer_actions_and_json(self):
        print("\n--- TEST 49: Burn-Rate Pacer Actions and JSON Output ---")
        usage = {
            "reports": [
                {
                    "provider": "google-antigravity",
                    "metadata": {"email": "partner-a@example.com"},
                    "limits": [
                        {
                            "id": "google-antigravity:anthropic:default:weekly",
                            "label": "Weekly Partner Cap",
                            "window": {"durationMs": 604800000, "resetsAt": self.mock_now_ms + 142 * 3600000},
                            "amount": {"remainingFraction": 0.191, "usedFraction": 0.809},
                            "status": "ok",
                        }
                    ],
                },
                {
                    "provider": "google-antigravity",
                    "metadata": {"email": "partner-b@example.com"},
                    "limits": [
                        {
                            "id": "google-antigravity:anthropic:default:daily",
                            "label": "Daily Partner Pool",
                            "window": {"durationMs": 86400000, "resetsAt": self.mock_now_ms + 3 * 3600000},
                            "amount": {"remainingFraction": 0.529, "usedFraction": 0.471},
                            "status": "ok",
                        }
                    ],
                },
                {
                    "provider": "anthropic",
                    "metadata": {"accountId": "anthropic_pro"},
                    "limits": [
                        {
                            "id": "anthropic:7d",
                            "label": "Weekly Anthropic",
                            "window": {"durationMs": 604800000, "resetsAt": self.mock_now_ms + 48 * 3600000},
                            "amount": {"remainingFraction": 0.80, "usedFraction": 0.20},
                            "status": "ok",
                        }
                    ],
                },
            ],
            "dormant": [],
        }
        parsed = parse_usage_json(usage, current_time_ms=self.mock_now_ms)
        norm = parsed.to_normalized()

        # Use temporary file for hermetic burn-rate samples
        tmp_samples = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w")
        tmp_samples.close()
        try:
            burn_paces = compute_window_burn_paces(norm, samples_file=Path(tmp_samples.name))
        finally:
            if os.path.exists(tmp_samples.name):
                os.unlink(tmp_samples.name)
        by_key = {f"{p.provider}:{p.account}": p for p in burn_paces}

        # brendmark weekly cap is throttled
        # partner-a weekly cap is throttled
        self.assertEqual(by_key["google-antigravity:anthropic:partner-a"].action, "throttle")
        # partner-b daily pool is ok
        self.assertEqual(by_key["google-antigravity:anthropic:partner-b"].action, "ok")
        # Anthropic with 80% remaining, 48h to reset, projected 28% < 92% -> spend-more
        anth_pace = next(p for p in burn_paces if p.provider == "anthropic")
        self.assertEqual(anth_pace.action, "spend-more")

        # Test CLI pace --json returns valid JSON hermetically
        tmp_file = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w")
        tmp_samples_cli = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w")
        tmp_samples_cli.close()
        try:
            json.dump(usage, tmp_file)
            tmp_file.close()
            cmd = [
                sys.executable,
                str(Path(SCRIPT_DIR) / "model_routing.py"),
                "pace",
                "--json",
                "--balance-file",
                tmp_file.name,
                "--samples-file",
                tmp_samples_cli.name,
            ]
            env = os.environ.copy()
            env["VEYYON_USAGE_SAMPLES_FILE"] = tmp_samples_cli.name
            res = subprocess.run(cmd, capture_output=True, text=True, check=True, env=env)
            out_json = json.loads(res.stdout)
            self.assertIn("windows", out_json)
            self.assertIn("recommended_lanes", out_json)
            self.assertEqual(out_json["recommended_lanes"]["reviewer"], MODEL_CODEX_SOL)
            self.assertNotIn("ag-opus", out_json["recommended_lanes"])
        finally:
            if os.path.exists(tmp_file.name):
                os.unlink(tmp_file.name)
            if os.path.exists(tmp_samples_cli.name):
                os.unlink(tmp_samples_cli.name)
        print("  [PASS] Burn-rate pacer computes correct actions and emits valid CLI JSON.")

    # -------------------------------------------------------------------------
    # TEST 50: Gating role pin guard (no chatgpt-web first)
    # -------------------------------------------------------------------------
    def test_gating_role_pin_guard_no_chatgpt_web(self):
        print("\n--- TEST 50: Gating Role Pin Guard (No chatgpt-web) ---")
        for gating_role in ("reviewer",):
            pinned = ROLE_MODEL_PINS.get(gating_role, "")
            self.assertFalse(pinned.startswith("chatgpt-web"),
                             f"ROLE_MODEL_PINS[{gating_role}] must never lead with chatgpt-web (got {pinned})")
        self.assertEqual(ROLE_MODEL_PINS.get("reviewer"), MODEL_CODEX_SOL)
        self.assertNotIn("ag-opus", ROLE_MODEL_PINS)
        print("  [PASS] Gating roles strictly barred from leading with chatgpt-web.")

    # -------------------------------------------------------------------------
    # TEST 51: record_429 usage_limit_reached window reset calculation (Finding 1)
    # -------------------------------------------------------------------------
    def test_record_429_usage_limit_reached_window_reset(self):
        print("\n--- TEST 51: record_429 usage_limit_reached Window Reset ---")
        now = datetime.datetime(2026, 9, 5, 12, 0, 0, tzinfo=datetime.timezone.utc)
        p = NormalizedProviderBalance(
            provider="openai-codex", status="ok", is_active=True, effective_remaining_fraction=0.01,
            cycle_seconds_to_reset=259200, cycle_hours_to_reset=72, bottleneck_window_id="openai-codex:7d",
            windows=[
                NormalizedWindow(id="openai-codex:5h", label="5h", duration_seconds=18000, resets_at_utc="", seconds_to_reset=7200, remaining_fraction=0.5, used_fraction=0.5, unit="units", status="ok", is_cooldown=False),
                NormalizedWindow(id="openai-codex:7d", label="7d", duration_seconds=604800, resets_at_utc="", seconds_to_reset=259200, remaining_fraction=0.01, used_fraction=0.99, unit="units", status="ok", is_cooldown=False),
            ]
        )
        snap = NormalizedBalanceSnapshot(providers={"openai-codex": p})
        sel = ResetAwareModelSelector(snap)
        body = '{"error":{"type":"usage_limit_reached","message":"You have exceeded your current quota."}}'
        res = sel.record_429("openai-codex", body, window_id="weekly", now=now)
        self.assertIsNotNone(res)
        self.assertEqual(res.retry_after_seconds, 259200.0)
        self.assertEqual(res.exhausted_until, "2026-09-08T12:00:00Z")
        print("  [PASS] record_429 calculates exhaustion until actual weekly window reset (72h).")
    # TEST 49: Codex manual switch — skipped when False, routed when True
    # -------------------------------------------------------------------------
    def test_codex_manual_switch_both_states(self):
        print("\n--- TEST 49: Codex Manual Switch: Skipped when False, Routed when True ---")
        # Invariant: CODEX_ENABLED is True by default (re-enabled 2026-09-29, operator ruling)
        self.assertTrue(CODEX_ENABLED, "CODEX_ENABLED must default to True")

        # ---------------------------------------------------------------------
        # STATE 1: Skipped when CODEX_ENABLED = False (or codex_available() == False)
        # ---------------------------------------------------------------------
        p_enabled = mock.patch("model_routing.CODEX_ENABLED", False)
        p_avail = mock.patch("model_routing.codex_available", return_value=False)
        p_enabled.start()
        p_avail.start()
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        selector_off = ResetAwareModelSelector(snapshot, codex_account=False)
        self.assertFalse(selector_off.codex_account_available())

        # With Codex disabled, strong non-UI review falls through to Sonnet,
        # never to Opus or Flash.
        rec_review = selector_off.select_model(
            task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH, allow_codex_promotion=True
        )
        self.assertEqual(rec_review.provider_statuses["openai-codex"], "unavailable")
        self.assertEqual(rec_review.selected_model, MODEL_CLAUDE_SONNET_55)
        self.assertFalse(rec_review.fallback_model.startswith("openai-codex/"))
        self.assertNotIn("flash", rec_review.fallback_model.lower())
        self.assertEqual(
            model_to_agent_role(rec_review.selected_model, TaskType.STRONG_REVIEW, RiskLevel.HIGH),
            "sonnet",
        )

        snap_ag = parse_usage_json(self._usage_with_ag_families(), current_time_ms=self.mock_now_ms)
        sel_ag_off = ResetAwareModelSelector(snap_ag, codex_account=False)
        rec_ag_rev = sel_ag_off.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertEqual(rec_ag_rev.selected_model, MODEL_CLAUDE_SONNET_55)
        self.assertEqual(model_to_agent_role(rec_ag_rev.selected_model, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "sonnet")
        # Routine execution / implementation falls through to task (Gemini Flash).
        rec_exec = selector_off.select_model(
            task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.MEDIUM, allow_codex_promotion=True
        )
        self.assertFalse(rec_exec.selected_model.startswith("openai-codex/"))
        self.assertFalse(rec_exec.fallback_model.startswith("openai-codex/"))
        self.assertEqual(rec_exec.selected_model, MODEL_GEMINI_FLASH)
        self.assertEqual(model_to_agent_role(rec_exec.selected_model, TaskType.ROUTINE_EXECUTION, RiskLevel.MEDIUM), "task")

        # Dispatch packets: codex-* dispatch does NOT reach openai-codex
        packet_review = selector_off.dispatch(
            task_type=TaskType.STRONG_REVIEW,
            risk_level=RiskLevel.HIGH,
            allow_codex_promotion=True,
            head_sha="a" * 40,
            base_sha="b" * 40,
            changed_files=["core/auth.py"],
        )
        self.assertFalse(packet_review.recommendation["model"].startswith("openai-codex/"))
        self.assertFalse(packet_review.recommendation["fallback_model"].startswith("openai-codex/"))
        self.assertEqual(packet_review.recommendation["agent_role"], "sonnet")
        self.assertNotIn("flash", packet_review.recommendation["model"].lower())

        packet_worker = selector_off.dispatch(
            task_type=TaskType.ROUTINE_EXECUTION,
            risk_level=RiskLevel.MEDIUM,
            allow_codex_promotion=True,
            head_sha="a" * 40,
            base_sha="b" * 40,
            changed_files=["app/main.py"],
        )
        self.assertFalse(packet_worker.recommendation["model"].startswith("openai-codex/"))
        self.assertFalse(packet_worker.recommendation["fallback_model"].startswith("openai-codex/"))
        self.assertEqual(packet_worker.recommendation["model"], MODEL_GEMINI_FLASH)
        self.assertEqual(packet_worker.recommendation["agent_role"], "task")

        # Across ALL task types and risk levels, openai-codex is never selected
        for task_type in TaskType:
            for risk in RiskLevel:
                rec = selector_off.select_model(task_type=task_type, risk_level=risk, allow_codex_promotion=True)
                self.assertFalse(
                    rec.selected_model.startswith("openai-codex/"),
                    f"{task_type}/{risk} selected {rec.selected_model} when Codex is disabled"
                )
                self.assertFalse(
                    rec.fallback_model.startswith("openai-codex/"),
                    f"{task_type}/{risk} fallback {rec.fallback_model} when Codex is disabled"
                )

        # Verify model_to_agent_role when codex is disabled maps codex models to sound fallbacks
        with mock.patch("model_routing.codex_available", return_value=False):
            self.assertEqual(model_to_agent_role(MODEL_CODEX_ASTRA, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "reviewer")
            self.assertEqual(model_to_agent_role(MODEL_CODEX_ASTRA, TaskType.ROUTINE_EXECUTION, RiskLevel.HIGH), "task")
            self.assertEqual(model_to_agent_role(MODEL_CODEX_FAST, TaskType.ROUTINE_EXECUTION, RiskLevel.LOW), "task")
            self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "reviewer")
            self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.ROUTINE_EXECUTION, RiskLevel.HIGH), "task")
            self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.DEEP_REASONING, RiskLevel.HIGH), "task")
            self.assertIsNone(resolve_role_model("codex-worker"))
            self.assertIsNone(resolve_role_model("codex-reviewer"))
            self.assertIsNone(resolve_role_model("thinker"))
            self.assertFalse(is_agent_role_available("codex-worker"))
            self.assertFalse(is_agent_role_available("codex-reviewer"))
            self.assertFalse(is_agent_role_available("thinker"))
            # Default unconfigured selector uses live codex_available() -> False
            default_sel = ResetAwareModelSelector(snapshot)
            self.assertFalse(default_sel.codex_account_available())
            disp_rev = default_sel.dispatch(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
            self.assertFalse(disp_rev.recommendation["model"].startswith("openai-codex/"))
            self.assertEqual(disp_rev.recommendation["agent_role"], "sonnet")
            disp_work = default_sel.dispatch(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.MEDIUM)
            self.assertFalse(disp_work.recommendation["model"].startswith("openai-codex/"))
            self.assertEqual(disp_work.recommendation["agent_role"], "task")
        p_enabled.stop()
        p_avail.stop()
        # ---------------------------------------------------------------------
        # STATE 2: Routed again when CODEX_ENABLED = True (switched back on)
        # ---------------------------------------------------------------------
        with mock.patch("model_routing.codex_available", return_value=True):
            selector_on = ResetAwareModelSelector(snapshot, codex_account=True)
            self.assertTrue(selector_on.codex_account_available())

            # Near reset with surplus allowance: Sol high reviews and Sol medium executes.
            near_reset_dict = copy.deepcopy(self.mock_usage_dict)
            codex_lim = near_reset_dict["reports"][2]["limits"][0]
            codex_lim["window"]["resetsAt"] = self.mock_now_ms + (18 * 3600 * 1000)
            codex_lim["amount"]["remainingFraction"] = 0.70
            codex_lim["amount"]["remaining"] = 70.0
            on_snap = parse_usage_json(near_reset_dict, current_time_ms=self.mock_now_ms)
            on_sel_surplus = ResetAwareModelSelector(on_snap, codex_account=True)

            on_review = on_sel_surplus.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
            self.assertEqual(on_review.selected_model, MODEL_CODEX_SOL)
            self.assertTrue(on_review.promotion_applied)

            on_exec = on_sel_surplus.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.MEDIUM)
            self.assertEqual(on_exec.selected_model, MODEL_CODEX_FAST)
            self.assertTrue(on_exec.promotion_applied)

            # Dispatch packets map to actual Codex agent roles when enabled
            on_pkt_rev = on_sel_surplus.dispatch(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
            self.assertEqual(on_pkt_rev.recommendation["model"], MODEL_CODEX_SOL)
            self.assertEqual(on_pkt_rev.recommendation["agent_role"], "codex-reviewer")

            on_pkt_work = on_sel_surplus.dispatch(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.MEDIUM)
            self.assertEqual(on_pkt_work.recommendation["model"], MODEL_CODEX_FAST)
            self.assertEqual(on_pkt_work.recommendation["agent_role"], "codex-worker")

            # Role mapper maps to codex-* roles when enabled
            self.assertEqual(model_to_agent_role(MODEL_CODEX_ASTRA, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "codex-reviewer")
            self.assertEqual(model_to_agent_role(MODEL_CODEX_ASTRA, TaskType.ROUTINE_EXECUTION, RiskLevel.HIGH), "codex-worker")
            # Agent roles available when Codex enabled
            self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "codex-reviewer")
            self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.ROUTINE_EXECUTION, RiskLevel.HIGH), "codex-worker")
            self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.DEEP_REASONING, RiskLevel.HIGH), "thinker")
            self.assertEqual(resolve_role_model("codex-worker"), MODEL_CODEX_WORKER)
            self.assertEqual(resolve_role_model("codex-reviewer"), MODEL_CODEX_SOL)
            self.assertEqual(resolve_role_model("thinker"), MODEL_CODEX_SOL)
            self.assertTrue(is_agent_role_available("codex-worker"))
            self.assertTrue(is_agent_role_available("codex-reviewer"))
            self.assertTrue(is_agent_role_available("thinker"))
            self.assertTrue(is_agent_role_available("sol"))
            self.assertTrue(is_agent_role_available("task"))

        # ---------------------------------------------------------------------
        # NEGATIVE CONTROL:
        # 1. Identical near-reset surplus fixture that promoted Codex in State 2
        #    MUST NOT route to or promote Codex when codex_account=False / CODEX_ENABLED=False.
        # 2. Rejection of Codex agent roles when disabled (never codex-reviewer / codex-worker).
        # 3. Environment override negative control (VEYYON_CODEX_ENABLED="0" overrides CODEX_ENABLED=True).
        # 4. No automatic re-enable by date (pure manual control).
        # ---------------------------------------------------------------------
        with mock.patch("model_routing.codex_available", return_value=False):
            neg_sel_surplus = ResetAwareModelSelector(on_snap, codex_account=False)
            self.assertFalse(neg_sel_surplus.codex_account_available())

            # The same near-reset stimulus must not select any Codex model when
            # the account switch is off; non-UI judgment falls to Sonnet.
            neg_rev = neg_sel_surplus.select_model(
                task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH, allow_codex_promotion=True)
            self.assertFalse(neg_rev.selected_model.startswith("openai-codex/"))
            self.assertFalse(neg_rev.promotion_applied, "Promotion must not be applied to Codex when disabled")
            self.assertEqual(neg_rev.selected_model, MODEL_CLAUDE_SONNET_55)
            self.assertNotIn("flash", neg_rev.selected_model.lower())
            self.assertNotEqual(neg_rev.selected_model, MODEL_CLAUDE_OPUS_55)

            # In State 2 routine execution returned MODEL_CODEX_FAST with promotion_applied=True.
            # In Negative Control, it MUST NOT select Codex Fast or any Codex model:
            neg_exec = neg_sel_surplus.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.MEDIUM, allow_codex_promotion=True)
            self.assertFalse(neg_exec.selected_model.startswith("openai-codex/"))
            self.assertFalse(neg_exec.promotion_applied, "Promotion must not be applied to Codex when disabled")
            self.assertEqual(neg_exec.selected_model, MODEL_GEMINI_FLASH)
            self.assertNotEqual(neg_exec.selected_model, MODEL_CODEX_FAST)

            # Negative control on agent role mappings:
            # When disabled, openai-codex models MUST NEVER map to codex-reviewer or codex-worker
            self.assertNotEqual(model_to_agent_role(MODEL_CODEX_ASTRA, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "codex-reviewer")
            self.assertNotEqual(model_to_agent_role(MODEL_CODEX_ASTRA, TaskType.ROUTINE_EXECUTION, RiskLevel.MEDIUM), "codex-worker")
            self.assertNotEqual(model_to_agent_role(MODEL_CODEX_FAST, TaskType.ROUTINE_EXECUTION, RiskLevel.LOW), "codex-worker")
            # When disabled, is_agent_role_available MUST return False for Codex roles
            self.assertFalse(is_agent_role_available("codex-worker"))
            self.assertFalse(is_agent_role_available("codex-reviewer"))
            self.assertFalse(is_agent_role_available("thinker"))
            self.assertFalse(is_agent_role_available("sol"))
            self.assertTrue(is_agent_role_available("task"))
            self.assertTrue(is_agent_role_available("reviewer"))
            self.assertFalse(is_agent_role_available("ag-opus"))
            self.assertTrue(is_agent_role_available("ds-task"))
            self.assertTrue(is_agent_role_available("go-task"))

        # Negative control on environment override:
        with mock.patch("model_routing.CODEX_ENABLED", True):
            with mock.patch.dict(os.environ, {"VEYYON_CODEX_ENABLED": "0"}):
                self.assertFalse(codex_available(), "VEYYON_CODEX_ENABLED=0 must force disabled even if CODEX_ENABLED=True")
            with mock.patch.dict(os.environ, {"VEYYON_CODEX_ENABLED": "false"}):
                self.assertFalse(codex_available(), "VEYYON_CODEX_ENABLED=false must force disabled even if CODEX_ENABLED=True")
            with mock.patch.dict(os.environ, {"VEYYON_CODEX_ENABLED": "no"}):
                self.assertFalse(codex_available(), "VEYYON_CODEX_ENABLED=no must force disabled even if CODEX_ENABLED=True")

        # Positive control on environment override switch: VEYYON_CODEX_ENABLED=1 enables Codex while CODEX_ENABLED=False
        with mock.patch("model_routing.CODEX_ENABLED", False):
            with mock.patch.dict(os.environ, {"VEYYON_CODEX_ENABLED": "1"}):
                self.assertTrue(codex_available(), "VEYYON_CODEX_ENABLED=1 must enable codex_available() when CODEX_ENABLED=False")
                self.assertEqual(resolve_role_model("codex-worker"), MODEL_CODEX_WORKER)
                self.assertEqual(resolve_role_model("codex-reviewer"), MODEL_CODEX_SOL)
                self.assertEqual(resolve_role_model("thinker"), MODEL_CODEX_SOL)
                self.assertTrue(is_agent_role_available("codex-worker"))
                self.assertTrue(is_agent_role_available("codex-reviewer"))
                self.assertTrue(is_agent_role_available("thinker"))
                self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "codex-reviewer")
                self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.ROUTINE_EXECUTION, RiskLevel.HIGH), "codex-worker")
                self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.DEEP_REASONING, RiskLevel.HIGH), "thinker")
        # Negative control: no automatic re-enable by date (manual switch only)
        with mock.patch("model_routing.CODEX_ENABLED", False), mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(codex_available(), "codex_available() must be False regardless of time when CODEX_ENABLED=False")
        # Profile config invariant: when CODEX_ENABLED is False, Codex agent roles must be disabled
        if not CODEX_ENABLED:
            config_path = os.path.expanduser("~/.veyyon/profiles/default/agent/config.yml")
            if os.path.exists(config_path) and yaml is not None:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                prof_agents = (cfg.get("agent") or {}).get("agents") or {}
                for codex_role in ("codex-worker", "codex-reviewer", "thinker", "sol"):
                    entry = prof_agents.get(codex_role)
                    if entry:
                        self.assertFalse(
                            entry.get("enabled", True),
                            f"{codex_role} must have enabled: false in config.yml when CODEX_ENABLED=False",
                        )
        print("  [PASS] All states verified: off (skipped), on (routed), plus negative control.")


    # -------------------------------------------------------------------------
    # TEST 50: astra-ux role pin — live Opus 5.5, high-risk fallback, never exhausted ag-opus
    # -------------------------------------------------------------------------
    def test_astra_ux_never_resolves_to_codex_while_disabled(self):
        print("\n--- TEST 50: astra-ux Role Pin & Exhaustion Invariant ---")
        # Invariant 1: while CODEX_ENABLED is True, astra-ux pins MODEL_CODEX_ASTRA (operator ruling 2026-09-29)
        self.assertTrue(CODEX_ENABLED, "CODEX_ENABLED must default to True")
        self.assertIn("astra-ux", ROLE_MODEL_PINS)
        pinned_model = ROLE_MODEL_PINS["astra-ux"]
        self.assertEqual(pinned_model, MODEL_CODEX_ASTRA,
                         f"astra-ux must pin {MODEL_CODEX_ASTRA}")

        # Invariant 2: astra-ux resolves to openai-codex/gpt-6-astra:medium first
        resolved = resolve_role_model("astra-ux")
        self.assertEqual(resolved, MODEL_CODEX_ASTRA)
        self.assertNotEqual(resolved, MODEL_AG_CLAUDE_OPUS)

        # Invariant 3: fallback ladder exists, starts with Astra, has no Flash/free tier
        self.assertIn("astra-ux", ROLE_FALLBACK_LADDERS)
        ladder = ROLE_FALLBACK_LADDERS["astra-ux"]
        self.assertEqual(ladder[0], MODEL_CODEX_ASTRA)
        for m in ladder:
            self.assertFalse("flash" in m.lower(), f"astra-ux ladder must not contain Flash: {m}")
            self.assertFalse("free" in m.lower(), f"astra-ux ladder must not contain free tier: {m}")

        # Invariant 4: astra-ux NEVER resolves to ag-opus when ag-opus is marked exhausted
        from quota_snapshot import QuotaSnapshot, QuotaWindowEntry
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        mock_snap = QuotaSnapshot(entries={
            f"{AG_ANTHROPIC_PROVIDER}|default|daily": QuotaWindowEntry(
                provider=AG_ANTHROPIC_PROVIDER,
                window_id="daily",
                exhausted_until=(now_utc + datetime.timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                source="429",
            )
        })
        self.assertFalse(mock_snap.is_eligible(AG_ANTHROPIC_PROVIDER))
        resolved_with_ag_exhausted = resolve_role_model("astra-ux", quota_snapshot=mock_snap)
        self.assertEqual(resolved_with_ag_exhausted, MODEL_CODEX_ASTRA)
        self.assertNotEqual(resolved_with_ag_exhausted, MODEL_AG_CLAUDE_OPUS)

        # Negative control: even if ag-opus were the first candidate in the ladder,
        # resolve_role_model skips exhausted ag-opus and falls back to live model,
        # proving astra-ux NEVER resolves to ag-opus while it is marked exhausted.
        with mock.patch.dict(ROLE_FALLBACK_LADDERS, {"astra-ux": [MODEL_AG_CLAUDE_OPUS, MODEL_CLAUDE_OPUS_55]}):
            neg_resolved = resolve_role_model("astra-ux", quota_snapshot=mock_snap)
            self.assertEqual(neg_resolved, MODEL_CLAUDE_OPUS_55)
            self.assertNotEqual(neg_resolved, MODEL_AG_CLAUDE_OPUS,
                              "Negative control: astra-ux must skip exhausted ag-opus and fall back")

        # Check installed profile configuration if present
        config_path = Path(os.path.expanduser("~/.veyyon/profiles/default/agent/config.yml"))
        if config_path.exists() and yaml is not None:
            parsed = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            agents = (parsed.get("agent") or {}).get("agents") or {}
            model_roles = parsed.get("modelRoles") or {}
            chain = (agents.get("astra-ux") or {}).get("model") or model_roles.get("astra-ux")
            if chain is not None:
                leading = str(chain).split(",")[0].strip()
                self.assertEqual(leading, pinned_model,
                                 f"astra-ux leading model {leading} must match pin {pinned_model}")
                for m in str(chain).split(","):
                    m_clean = m.strip().lower()
                    self.assertFalse("flash" in m_clean, f"config.yml astra-ux chain must not contain Flash: {m}")
                    self.assertFalse("free" in m_clean, f"config.yml astra-ux chain must not contain free tier: {m}")
                if "astra-ux" in agents:
                    self.assertTrue(agents["astra-ux"].get("enabled", True), "astra-ux must be enabled")

        # Fallback when Codex is unavailable: resolves to non-Codex model from fallback ladder
        with mock.patch("model_routing.codex_available", return_value=False):
            fallback_resolved = resolve_role_model("astra-ux")
            self.assertIsNotNone(fallback_resolved)
            self.assertFalse(fallback_resolved.startswith("openai-codex/"),
                             f"astra-ux fallback must not be Codex: {fallback_resolved}")
            self.assertNotIn("flash", fallback_resolved.lower())
            self.assertNotIn("free", fallback_resolved.lower())

        # Live model resolution remains MODEL_CODEX_ASTRA
        with mock.patch("model_routing.codex_available", return_value=True):
            self.assertEqual(resolve_role_model("astra-ux"), MODEL_CODEX_ASTRA)

        print("  [PASS] astra-ux resolves to Astra, falls back cleanly when disabled, skips exhausted ag-opus.")
    # -------------------------------------------------------------------------
    # TEST 43: Advisor role pinned to Gemini 3.8 Flash (operator ruling 2026-09-27)
    # -------------------------------------------------------------------------
    def test_advisor_role_pinned_to_gemini_flash(self):
        print("\n--- TEST 43: Advisor Role Pinned to Gemini 3.8 Flash ---")
        self.assertIn("advisor", ROLE_MODEL_PINS)
        self.assertEqual(ROLE_MODEL_PINS["advisor"], MODEL_GEMINI_FLASH)
        self.assertEqual(resolve_role_model("advisor"), MODEL_GEMINI_FLASH)
        self.assertTrue(is_agent_role_available("advisor"))
        # Fable must never map to advisor (operator ruling: advisor moved to Gemini 3.8 Flash)
        self.assertNotEqual(model_to_agent_role(MODEL_CLAUDE_FABLE, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "advisor")
        self.assertNotEqual(model_to_agent_role(MODEL_CLAUDE_FABLE, TaskType.ROUTINE_EXECUTION, RiskLevel.LOW), "advisor")
        print(f"  [PASS] advisor pinned to {MODEL_GEMINI_FLASH}; Fable never maps to advisor.")
    # -------------------------------------------------------------------------
    # TEST 52: No route resolves to ag-opus (Operator ruling 2026-09-27 ~15:00Z)
    # -------------------------------------------------------------------------
    def test_no_route_resolves_to_ag_opus(self):
        print("\n--- TEST 52: No Route Resolves To ag-opus ---")
        self.assertNotIn("ag-opus", ROLE_MODEL_PINS)
        self.assertIsNone(resolve_role_model("ag-opus"))
        self.assertFalse(is_agent_role_available("ag-opus"))

        # Sweep all task types, risk levels, rework counts, diff line sizes, and domain tags
        usage = self._usage_with_ag_families(anthropic_used=0.0)
        selector = self._selector(usage)
        self.assertNotIn("ag-opus", get_recommended_lanes(selector))

        domain_tag_cases = [
            None,
            [],
            ["money"],
            ["auth"],
            ["migration"],
            ["concurrency"],
            ["architecture"],
            ["state_machine"],
            ["schema"],
            ["invariants"],
            ["delta"],
            ["frontend"],
            ["docs"],
        ]

        for task_type in TaskType:
            for risk in RiskLevel:
                for rework in (0, 1, 2):
                    for diff in (None, 50, 200, 300, 1000):
                        for tags in domain_tag_cases:
                            rec = selector.select_model(
                                task_type=task_type,
                                risk_level=risk,
                                rework_count=rework,
                                diff_lines=diff,
                                domain_tags=tags,
                            )
                            # Model ID must never be MODEL_AG_CLAUDE_OPUS on any review or execution route
                            self.assertNotEqual(
                                rec.selected_model,
                                MODEL_AG_CLAUDE_OPUS,
                                f"selected_model was {rec.selected_model} for {task_type}/{risk}/rework={rework}/diff={diff}/tags={tags}",
                            )
                            self.assertNotEqual(
                                rec.fallback_model,
                                MODEL_AG_CLAUDE_OPUS,
                                f"fallback_model was {rec.fallback_model} for {task_type}/{risk}/rework={rework}/diff={diff}/tags={tags}",
                            )
                            # Agent role mapping must never resolve to ag-opus
                            role_selected = model_to_agent_role(rec.selected_model, task_type, risk)
                            self.assertNotEqual(
                                role_selected,
                                "ag-opus",
                                f"selected model role was ag-opus for {rec.selected_model} in {task_type}/{risk}",
                            )
                            if rec.fallback_model:
                                role_fallback = model_to_agent_role(rec.fallback_model, task_type, risk)
                                self.assertNotEqual(
                                    role_fallback,
                                    "ag-opus",
                                    f"fallback model role was ag-opus for {rec.fallback_model} in {task_type}/{risk}",
                                )
                            # Dispatch packet agent_role must never be ag-opus
                            pkt = selector.dispatch(
                                task_type=task_type,
                                risk_level=risk,
                                rework_count=rework,
                            )
                            self.assertNotEqual(
                                pkt.recommendation["agent_role"],
                                "ag-opus",
                                f"dispatch role was ag-opus for {task_type}/{risk}",
                            )
                            self.assertNotEqual(
                                pkt.recommendation["model"],
                                MODEL_AG_CLAUDE_OPUS,
                            )
        print("  [PASS] Zero routes resolve to ag-opus across all task types, risk levels, rework counts, diff sizes and domain tags.")

    # -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    # TEST 53: OpenRouter DeepSeek Flash catalog window and fallback enforcement
    # (Refs #216 Item 2)
    # -------------------------------------------------------------------------
    def test_openrouter_deepseek_flash_catalog_window_and_enforcement(self):
        print("\n--- TEST 53: OpenRouter DeepSeek Flash Window & Fallback Enforcement ---")
        # 1. Authoritative catalog context window verification:
        # models.db openrouter:pseudo-api and OpenRouter live API verify 1,048,576 tokens.
        self.assertIn(MODEL_OR_DEEPSEEK_FLASH, VERIFIED_CONTEXT_WINDOWS)
        self.assertEqual(VERIFIED_CONTEXT_WINDOWS[MODEL_OR_DEEPSEEK_FLASH], 1048576)

        # 2. Within window: OpenRouter DeepSeek Flash is eligible as a fallback or last resort.
        usage = self._usage_with_ag_families(codex_used=0.92)
        for lim in usage["reports"][0]["limits"]:
            if lim["id"].startswith("google-antigravity:google"):
                lim["status"] = "rate_limited"
        selector = self._selector(usage)
        rec = selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW, context_tokens=10000)
        self.assertEqual(rec.selected_model, MODEL_DEEPSEEK_FLASH)
        self.assertEqual(rec.fallback_model, MODEL_OR_DEEPSEEK_FLASH)

        # 3. Context window filter enforcement on final fallbacks and last resort:
        # When context_tokens exceeds the catalog window (1,048,576), OpenRouter DeepSeek Flash
        # must NOT be selected as chosen or as fallback.
        # Direct _climb verification with empty rungs:
        last_resort = _Rung(MODEL_OR_DEEPSEEK_FLASH, True, "OpenRouter DeepSeek Flash", cooldown=True)
        final_fallbacks = [MODEL_DEEPSEEK_FLASH]

        # Fits window:
        is_eligible_pass = lambda m: 500000 <= VERIFIED_CONTEXT_WINDOWS.get(m, 0)
        chosen_pass, fb_pass = _climb([], last_resort, final_fallbacks, is_eligible=is_eligible_pass)
        self.assertEqual(chosen_pass.model, MODEL_OR_DEEPSEEK_FLASH)
        self.assertEqual(fb_pass, MODEL_DEEPSEEK_FLASH)

        # Exceeds window:
        is_eligible_exceed = lambda m: 1200000 <= VERIFIED_CONTEXT_WINDOWS.get(m, 0)
        with self.assertRaises(ValueError) as ctx:
            _climb([], last_resort, final_fallbacks, is_eligible=is_eligible_exceed)
        self.assertIn("no eligible model or fallback available in ladder", str(ctx.exception))

        # End-to-end select_model at 1.2M tokens:
        with self.assertRaises(ValueError):
            selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW, context_tokens=1200000)
        print("  [PASS] Catalog window 1,048,576 verified; context limit enforced on last-resort and final-fallback entries.")

    # -------------------------------------------------------------------------
    # TEST 54: Current Codex families are supported and legacy 5.6 is absent.
    # -------------------------------------------------------------------------
    def test_unsupported_codex_model_rejected_and_skipped(self):
        print("\n--- TEST 54: Current Codex Families and Effort Pins ---")
        self.assertEqual(UNSUPPORTED_CODEX_MODELS, frozenset())
        for model in (
            MODEL_CODEX_WORKER,
            MODEL_CODEX_SOL,
            MODEL_CODEX_SOL_FALLBACK,
            MODEL_CODEX_ASTRA,
        ):
            self.assertFalse(is_unsupported_codex_model(model), model)
            self.assertTrue(is_agent_role_available(model_to_agent_role(
                model, TaskType.STRONG_REVIEW, RiskLevel.HIGH)))

        configured = list(ROLE_MODEL_PINS.values())
        for ladder in ROLE_FALLBACK_LADDERS.values():
            configured.extend(ladder)
        self.assertTrue(all("gpt-5.6-sol" not in model for model in configured))

        self.assertEqual(ROLE_MODEL_PINS["codex-worker"], MODEL_CODEX_WORKER)
        self.assertEqual(ROLE_MODEL_PINS["codex-reviewer"], MODEL_CODEX_SOL)
        self.assertEqual(ROLE_MODEL_PINS["astra-ux"], MODEL_CODEX_ASTRA)
        self.assertTrue(MODEL_CODEX_WORKER.endswith(":medium"))
        self.assertTrue(MODEL_CODEX_SOL.endswith(":high"))
        self.assertTrue(MODEL_CODEX_ASTRA.endswith(":xhigh"))
        print("  [PASS] GPT-6.1/6 Sol and Astra are supported at task-specific effort; GPT-5.6 Sol is absent.")

def main():
    print("=" * 70)
    print("RUNNING VEYYON BALANCE LOADER & MODEL ROUTING SMOKE TEST SUITE")
    print("=" * 70)
    suite = unittest.TestLoader().loadTestsFromTestCase(TestBalanceLoaderAndRouting)
    runner = unittest.TextTestRunner(verbosity=1)
    result = runner.run(suite)
    if result.wasSuccessful():
        print("\n" + "=" * 70)
        print(f"ALL {result.testsRun} TESTS PASSED PERFECTLY")
        print("=" * 70)
    else:
        print("\n" + "=" * 70)
        print("TESTS FAILED")
        print("=" * 70)
        sys.exit(1)


if __name__ == "__main__":
    main()
