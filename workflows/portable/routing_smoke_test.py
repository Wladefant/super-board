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
import itertools
import json
import os
import shutil
import sys
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
    EvidencePacket,
    HarnessDispatchPacket,
    ResetAwareModelSelector,
    RiskLevel,
    TaskType,
    MODEL_CLAUDE_FABLE,
    MODEL_CODEX_FAST,
    MODEL_CODEX_ASTRA,
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
    CODEX_PACE_MIN_HEADROOM,
    CODEX_PACE_USED_FLOOR,
    ANTHROPIC_BOTTLENECK_MAX_USED,
    CREDENTIAL_ENV_BY_PROVIDER,
    MINIMAX_PROVIDER,
    ROLE_MODEL_PINS,
    VERIFIED_CONTEXT_WINDOWS,
    ZAI_PROVIDER,
    detect_credentialed_providers,
    model_to_agent_role,
    model_to_provider,
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
        quota_patch = mock.patch("model_routing.load_quota_snapshot", return_value=QuotaSnapshot())
        quota_patch.start()
        self.addCleanup(quota_patch.stop)

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

        # A) High-Risk Review: should promote Codex Astra to prevent allowance expiration!
        rec_review = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertTrue(rec_review.promotion_applied)
        self.assertEqual(rec_review.selected_model, MODEL_CODEX_ASTRA)
        self.assertIn("promoted Codex Astra", rec_review.reasoning)
        print(f"  [PASS] High-risk review promoted Codex Astra: {rec_review.selected_model}")

        # B) Deep Reasoning: should promote Codex Astra
        rec_reasoning = selector.select_model(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.MEDIUM)
        self.assertTrue(rec_reasoning.promotion_applied)
        self.assertEqual(rec_reasoning.selected_model, MODEL_CODEX_ASTRA)
        print(f"  [PASS] Deep reasoning promoted Codex Astra: {rec_reasoning.selected_model}")

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

        # High-risk review should NOT promote Codex; should route to Claude Fable 5.1
        rec = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertFalse(rec.promotion_applied)
        self.assertEqual(rec.selected_model, MODEL_CLAUDE_FABLE)
        print(f"  [PASS] Quota exhausted: No promotion; routed to {rec.selected_model}")

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

        # High-risk review must NOT select Gemini 3.8 Flash as sole quality gate
        rec = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertNotEqual(rec.selected_model, MODEL_GEMINI_FLASH)
        self.assertEqual(rec.selected_model, MODEL_CLAUDE_FABLE)
        self.assertTrue(rec.evidence_packet_required)
        print(f"  [PASS] High-risk review enforced strong model: {rec.selected_model}")

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
        self.assertEqual(packet.recommendation["model"], MODEL_CLAUDE_FABLE)
        self.assertEqual(packet.recommendation["agent_role"], "reviewer")
        self.assertEqual(packet.recommendation["provider"], "anthropic")
        self.assertIsNotNone(packet.evidence_packet)
        self.assertEqual(packet.evidence_packet["head_sha"], "1122334455667788")

        # Verify JSON serialization
        json_str = packet.to_json()
        parsed = json.loads(json_str)
        self.assertEqual(parsed["task"]["task_type"], "strong_review")
        self.assertEqual(parsed["recommendation"]["agent_role"], "reviewer")
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

        # Routine execution with rework_count=1 escalates to the high-risk worker ladder
        # (Flash barred): GLM-5.3 (credentialed) -> DeepSeek V4 Pro -> Codex Astra medium ->
        # Antigravity Opus -> Fable last resort. Paid Anthropic is never a worker primary or
        # fallback while a cheap tier has headroom (operator 2026-09-25: the cheap tiers precede
        # the subscription Codex window in worker ladders; Codex leads only in strong review).
        rec = selector.select_model(
            task_type=TaskType.ROUTINE_EXECUTION,
            risk_level=RiskLevel.LOW,
            rework_count=1,
        )
        self.assertNotEqual(rec.selected_model, MODEL_GEMINI_FLASH)
        # Default fixture: no Z.AI or OpenCode Go credential, Codex on pace, Anthropic with slack.
        self.assertEqual(rec.selected_model, MODEL_DEEPSEEK_PRO)
        self.assertEqual(rec.fallback_model, MODEL_CODEX_ASTRA)
        self.assertNotIn("anthropic/", rec.fallback_model)
        self.assertIn("high-risk implementation", rec.reasoning.lower())
        print(f"  [PASS] Rework escalation: {rec.selected_model} (fallback: {rec.fallback_model}, cross-provider)")

        # A credentialed Z.AI GLM Coding Plan is the ladder's first rung.
        glm_selector = ResetAwareModelSelector(snapshot, credentialed_providers={ZAI_PROVIDER})
        rec_glm = glm_selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW, rework_count=1)
        self.assertEqual(rec_glm.selected_model, MODEL_ZAI_GLM)
        self.assertEqual(rec_glm.fallback_model, MODEL_DEEPSEEK_PRO)
        print(f"  [PASS] Credentialed GLM-5.3 leads the ladder: {rec_glm.selected_model} (fallback {rec_glm.fallback_model})")

    # -------------------------------------------------------------------------
    # TEST 18: Domain Tags Escalation (C9 Invariant)
    # -------------------------------------------------------------------------
    def test_domain_tags_escalation(self):
        print("\n--- TEST 18: Domain Tags Escalation (C9) ---")
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        # High-risk domain tags: auth, state_machine, money, concurrency, migrations
        # Same high-risk worker ladder as rework; never paid Anthropic while cheap tiers exist.
        rec = selector.select_model(
            task_type=TaskType.ROUTINE_EXECUTION,
            risk_level=RiskLevel.LOW,
            domain_tags=["auth", "state_machine"],
        )
        self.assertNotEqual(rec.selected_model, MODEL_GEMINI_FLASH)
        self.assertEqual(rec.selected_model, MODEL_DEEPSEEK_PRO)
        self.assertNotIn("anthropic/", rec.fallback_model)
        # Cross-provider fallback
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

        # A) High-risk review
        rec_rev = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertNotEqual(rec_rev.selected_model, MODEL_GEMINI_FLASH)
        self.assertNotEqual(rec_rev.fallback_model, MODEL_GEMINI_FLASH)
        self.assertIn(rec_rev.fallback_model, (MODEL_CLAUDE_FABLE, MODEL_CODEX_ASTRA, MODEL_GEMINI_PRO, MODEL_DEEPSEEK_PRO))

        # B) High-risk deep reasoning
        rec_reason = selector.select_model(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.HIGH)
        self.assertNotEqual(rec_reason.selected_model, MODEL_GEMINI_FLASH)
        self.assertNotEqual(rec_reason.fallback_model, MODEL_GEMINI_FLASH)
        self.assertIn(rec_reason.fallback_model, (MODEL_CODEX_ASTRA, MODEL_GEMINI_PRO, MODEL_DEEPSEEK_PRO, MODEL_AG_CLAUDE_OPUS))
        print(f"  [PASS] High-risk reasoning and review fallback strictly strong model: {rec_reason.fallback_model} (Flash barred).")

    # -------------------------------------------------------------------------
    # TEST 21: Codex Agent Roles & Structural Retry Flash Bar
    # -------------------------------------------------------------------------
    def test_codex_roles_and_structural_retry_invariants(self):
        print("\n--- TEST 21: Codex Agent Roles & Structural Failure Retry ---")
        # 1. Verify model_to_agent_role assigns actual Codex agent roles from roster
        self.assertEqual(model_to_agent_role(MODEL_CODEX_ASTRA, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "codex-reviewer")
        self.assertEqual(model_to_agent_role(MODEL_CODEX_ASTRA, TaskType.ROUTINE_EXECUTION, RiskLevel.HIGH), "codex-worker")
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
        self.assertEqual(packet_review.recommendation["model"], MODEL_CODEX_ASTRA)
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
                         MODEL_AG_CLAUDE_OPUS)

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
                         MODEL_AG_CLAUDE_OPUS)

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
        # High-risk review climbs to Antigravity Opus; the high-risk worker ladder (GLM ->
        # Astra -> DeepSeek V4 Pro -> ag-opus -> Fable) lands on DeepSeek V4 Pro.
        expected = {
            TaskType.STRONG_REVIEW: MODEL_AG_CLAUDE_OPUS,
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
        print("  [PASS] 92% used with 145h left: Codex held back; high-risk review on ag-opus, workers on DeepSeek V4 Pro.")

        # The same 92% with 6h left is behind pace: the last 8% would expire unused, so it
        # is promoted ahead of every other strong lane, including Antigravity Claude.
        usage_end = self._usage_with_ag_families(codex_used=0.92, codex_reset_hrs=6)
        rec_end = self._selector(usage_end).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertFalse(rec_end.quota_metrics["codex_pro_throttled"])
        self.assertTrue(rec_end.promotion_applied)
        self.assertEqual(rec_end.selected_model, MODEL_CODEX_ASTRA)
        print(f"  [PASS] 92% used with 6h left: remaining Codex spent before reset on {rec_end.selected_model}.")

        # Exactly on pace (50% used, 84h left) is neither throttled nor promoted.
        usage_pace = self._usage_with_ag_families(anthropic_used=0.95, codex_used=0.49, codex_reset_hrs=84)
        self._block_anthropic(usage_pace)
        rec_pace = self._selector(usage_pace).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertFalse(rec_pace.quota_metrics["codex_pro_throttled"])
        self.assertFalse(rec_pace.promotion_applied)
        self.assertEqual(rec_pace.selected_model, MODEL_CODEX_ASTRA)
        print(f"  [PASS] On-pace Codex remains a normal strong lane: {rec_pace.selected_model}")

    # -------------------------------------------------------------------------
    # TEST 26: Direct Anthropic is the Fable orchestrator's weekly budget
    # -------------------------------------------------------------------------
    def test_anthropic_orchestrator_reserve(self):
        print("\n--- TEST 26: Anthropic Orchestrator Reserve ---")
        # 64% of the week used with 72h left (57% elapsed): ahead of pace, so workers leave it
        # to the orchestrator even for high-risk review while Codex is on pace.
        usage = self._usage_with_ag_families(anthropic_used=0.95, anthropic_week_used=0.64, anthropic_week_reset_hrs=72)
        rec = self._selector(usage).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertTrue(rec.quota_metrics["anthropic_orchestrator_reserve"])
        self.assertEqual(rec.selected_model, MODEL_CODEX_ASTRA)
        self.assertNotIn("anthropic/", rec.fallback_model)
        print(f"  [PASS] Ahead-of-pace Anthropic reserved; high-risk review on {rec.selected_model}.")

        # The reserve is an emergency lane only when nothing else strong can take the work.
        usage_last = self._usage_with_ag_families(
            anthropic_used=0.95, codex_used=0.92, codex_reset_hrs=145,
            anthropic_week_used=0.64, anthropic_week_reset_hrs=72,
        )
        rec_last = self._selector(usage_last).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertEqual(rec_last.selected_model, MODEL_CLAUDE_FABLE)
        self.assertTrue(rec_last.cooldown_fallback)
        self.assertIn("orchestrator reserve", rec_last.reasoning)
        print(f"  [PASS] Reserve drawn only as last resort: {rec_last.selected_model}")

        # 30% used with 20h left: most of the week would expire unused, so the high-risk
        # REVIEW lane spends it. Worker lanes still never take paid Anthropic while a cheap
        # tier has headroom (operator 2026-09-25), surplus or not.
        usage_surplus = self._usage_with_ag_families(anthropic_used=0.95, anthropic_week_used=0.30, anthropic_week_reset_hrs=20)
        selector = self._selector(usage_surplus)
        self.assertFalse(selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
                         .quota_metrics["anthropic_orchestrator_reserve"])
        self.assertEqual(
            selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH).selected_model,
            MODEL_CLAUDE_FABLE,
        )
        for task_type, risk in ((TaskType.DEEP_REASONING, RiskLevel.MEDIUM), (TaskType.DEEP_REASONING, RiskLevel.HIGH),
                                (TaskType.ROUTINE_EXECUTION, RiskLevel.HIGH), (TaskType.STRONG_REVIEW, RiskLevel.MEDIUM)):
            rec_worker = selector.select_model(task_type=task_type, risk_level=risk)
            self.assertNotIn("anthropic/", rec_worker.selected_model, f"{task_type}/{risk}")
            self.assertNotIn("anthropic/", rec_worker.fallback_model, f"{task_type}/{risk}")
        print("  [PASS] Anthropic surplus near reset spent by high-risk review only; worker lanes stay on cheap tiers.")

        # Stale Anthropic data never counts as slack.
        stale = self._usage_with_ag_families(anthropic_used=0.95, anthropic_week_used=0.0, anthropic_week_reset_hrs=20)
        stale["reports"][1]["fetchedAt"] = self.mock_now_ms - 2 * 3600 * 1000
        rec_stale = self._selector(stale).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertTrue(rec_stale.quota_metrics["anthropic_orchestrator_reserve"])
        self.assertEqual(rec_stale.selected_model, MODEL_CODEX_ASTRA)
        print(f"  [PASS] Stale Anthropic snapshot kept in reserve; routed to {rec_stale.selected_model}.")

    # -------------------------------------------------------------------------
    # TEST 23: Antigravity Claude daily window is spent before paid Anthropic
    # -------------------------------------------------------------------------
    def test_antigravity_claude_chosen(self):
        print("\n--- TEST 23: Antigravity Claude Chosen Before Paid Anthropic ---")
        selector = self._selector(self._usage_with_ag_families(anthropic_used=0.0))
        rec_review = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.MEDIUM)
        self.assertEqual(rec_review.selected_model, MODEL_AG_CLAUDE_OPUS)
        # Operator DEEP_REASONING ladder policy (#214): LOW and MEDIUM lead with OpenCode Go
        # GLM-5.3, then DeepSeek V4 Pro, then Gemini 3.8 Flash. Opus is minimized and reserved
        # for orchestrator and high-risk reviews only, so medium-risk deep reasoning does NOT
        # spend ag-opus; only HIGH leads with Antigravity Claude Opus.
        rec_reason = selector.select_model(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.MEDIUM)
        self.assertEqual(rec_reason.selected_model, MODEL_DEEPSEEK_PRO)
        packet = selector.dispatch(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.MEDIUM)
        self.assertEqual(packet.recommendation["model"], MODEL_DEEPSEEK_PRO)
        self.assertEqual(packet.recommendation["agent_role"], "ds-pro")
        print(f"  [PASS] Medium review on {rec_review.selected_model} (ag-opus); medium reasoning on {rec_reason.selected_model} (ds-pro).")

        # An almost spent Antigravity Claude window (95% used) is left alone.
        spent = self._selector(self._usage_with_ag_families(anthropic_used=0.95))
        rec_spent = spent.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.MEDIUM)
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
        # Throttled pro Codex is not handed high-risk review while ag-opus has headroom.
        rec_review = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertEqual(rec_review.selected_model, MODEL_AG_CLAUDE_OPUS)
        print(f"  [PASS] Two-account Codex: metrics from pro window (remaining "
              f"{rec.quota_metrics['codex_remaining']:.2f}, throttled={rec.quota_metrics['codex_pro_throttled']}), "
              f"lane remaining {rec.quota_metrics['codex_lane_remaining']:.2f}; review on {rec_review.selected_model}")

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
        # AG Opus → ag-opus
        self.assertEqual(model_to_agent_role(MODEL_AG_CLAUDE_OPUS, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "ag-opus")
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
        # Every role this router emits for a pinned model resolves back to that role, so a
        # dispatched role never silently runs a different model.
        for role, model in ROLE_MODEL_PINS.items():
            task_type = TaskType.STRONG_REVIEW if role == "codex-reviewer" else TaskType.ROUTINE_EXECUTION
            self.assertEqual(model_to_agent_role(model, task_type, RiskLevel.HIGH), role, f"{role} pin {model}")
        print("  [PASS] All role and provider mappings correct (ag-sonnet, ag-gpt, ds-pro, zai-task, zai-flash, minimax-task).")

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
    # TEST 33: Paid Anthropic is never a worker primary or fallback while a cheap tier has
    # headroom — including the low-risk review, low/medium reasoning and deep-context lanes.
    # -------------------------------------------------------------------------
    def test_worker_lanes_never_spend_orchestrator_reserve(self):
        print("\n--- TEST 33: Worker Lanes Never Take Paid Anthropic While Cheap Tiers Exist ---")
        # Anthropic has maximum slack (0% used, 20h left) — the most tempting case — plus the
        # states named in reviews 5317588095 and 5317952644: reserve active with Antigravity
        # Claude spent and Codex throttled, Anthropic surplus near reset, Google down, and
        # Google + DeepSeek out with Codex on pace (the deep-context gap).
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
                                if is_review_lane and ctx <= 180000:
                                    continue  # the high-risk REVIEW lane may spend Anthropic slack
                                self.assertNotIn("anthropic/", rec.selected_model, where)
                                self.assertNotIn("anthropic/", rec.fallback_model, where)
        print(f"  [PASS] {checked} worker routes (task x risk x rework/domain x 6 context sizes x credentials x "
              f"{len(scenarios)} quota states): none selects or falls back to paid Anthropic, to a model whose window "
              "is below the context, or to MiniMax outside low-risk bulk/deep-context work; no high-risk, rework or "
              "money route falls back to a Flash tier; every fallback crosses providers.")

        # The deep-context gap (review 5317952644 A'): with both 1M-context cheap tiers out,
        # Codex on pace holds 200k/240k, and credentialed GLM holds a 10k DEEP_CONTEXT task.
        gap = ResetAwareModelSelector(parse_usage_json(scenarios["google_deepseek_out_codex_on_pace"],
                                                       current_time_ms=self.mock_now_ms))
        for ctx in (200000, 240000):
            rec = gap.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.MEDIUM, context_tokens=ctx)
            self.assertEqual(rec.selected_model, MODEL_CODEX_ASTRA, ctx)
            self.assertFalse(rec.quota_metrics["codex_pro_throttled"])
        gap_glm = ResetAwareModelSelector(parse_usage_json(scenarios["google_deepseek_out_codex_on_pace"],
                                                           current_time_ms=self.mock_now_ms),
                                          credentialed_providers={ZAI_PROVIDER})
        self.assertEqual(gap_glm.select_model(task_type=TaskType.DEEP_CONTEXT, risk_level=RiskLevel.LOW,
                                              context_tokens=10000).selected_model, MODEL_ZAI_GLM)
        # 500k exceeds every non-1M window (Codex Fast 400k): only then may Fable fire on slack.
        rec_500k = gap.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.MEDIUM, context_tokens=500000)
        self.assertEqual(rec_500k.selected_model, MODEL_CLAUDE_FABLE)
        self.assertIn("whose window holds 500000 tokens is unavailable", rec_500k.reasoning)
        print(f"  [PASS] Google + DeepSeek out, Codex on pace: 200k/240k -> {MODEL_CODEX_ASTRA}, DEEP_CONTEXT/10k -> "
              f"{MODEL_ZAI_GLM} (credentialed); Fable only at 500k, beyond every cheap window.")

        # Review 5318271884 K: with Gemini Pro and DeepSeek V4 Pro out and MiniMax credentialed,
        # high-risk implementation, review and money reasoning above 180k go to Codex Astra on
        # pace; only a low-risk deep-context read may use the MiniMax overflow.
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
            self.assertEqual(rec.selected_model, MODEL_CODEX_ASTRA, f"{task_type.value}/{ctx}/{extra}")
            self.assertNotIn("minimax-code/", rec.fallback_model)
        self.assertEqual(gap_minimax.select_model(task_type=TaskType.DEEP_CONTEXT, risk_level=RiskLevel.LOW,
                                                  context_tokens=200000).selected_model, MODEL_MINIMAX_M3)
        print(f"  [PASS] Gemini Pro + DeepSeek V4 Pro out, MiniMax credentialed: high-risk impl/review/money reasoning "
              f"above 180k -> {MODEL_CODEX_ASTRA}; MiniMax only for a low-risk deep-context read.")

        # Review 5318271884 L: GLM-5.3 holds exactly 131,072 tokens; at 131,073 every ladder
        # skips it (high-risk worker, deep-context, tiny-task GLM-5.3-Flash). The high-risk
        # worker lane then takes DeepSeek V4 Pro, which precedes Codex Astra in that ladder.
        glm_all_ok = ResetAwareModelSelector(parse_usage_json(scenarios["all_ok"], current_time_ms=self.mock_now_ms),
                                             credentialed_providers={ZAI_PROVIDER})
        at_limit = glm_all_ok.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.HIGH,
                                           context_tokens=131072)
        over_limit = glm_all_ok.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.HIGH,
                                             context_tokens=131073)
        self.assertEqual(at_limit.selected_model, MODEL_ZAI_GLM)
        self.assertEqual(over_limit.selected_model, MODEL_DEEPSEEK_PRO)
        self.assertEqual(gap_glm.select_model(task_type=TaskType.DEEP_CONTEXT, risk_level=RiskLevel.LOW,
                                              context_tokens=131072).selected_model, MODEL_ZAI_GLM)
        self.assertEqual(gap_glm.select_model(task_type=TaskType.DEEP_CONTEXT, risk_level=RiskLevel.LOW,
                                              context_tokens=131073).selected_model, MODEL_CODEX_ASTRA)
        glm_google_down = ResetAwareModelSelector(parse_usage_json(google_down, current_time_ms=self.mock_now_ms),
                                                  credentialed_providers={ZAI_PROVIDER})
        self.assertEqual(glm_google_down.select_model(task_type=TaskType.TINY_TASK, context_tokens=131072).selected_model,
                         MODEL_ZAI_GLM_FLASH)
        self.assertNotEqual(glm_google_down.select_model(task_type=TaskType.TINY_TASK, context_tokens=131073).selected_model,
                            MODEL_ZAI_GLM_FLASH)
        print(f"  [PASS] GLM window fit: 131,072 -> {MODEL_ZAI_GLM}; 131,073 -> {over_limit.selected_model} "
              "(high-risk worker, deep-context and tiny-task ladders).")

        # When every cheap tier is out, the high-risk worker ladder reaches Fable as its last
        # rung, and only on slack behind pace.
        usage_out = self._usage_with_ag_families(anthropic_used=0.95, anthropic_week_used=0.0, anthropic_week_reset_hrs=20)
        for rep in usage_out["reports"]:
            if rep["provider"] in ("google-antigravity", "openai-codex"):
                rep["metadata"]["limitReached"] = True
                rep["metadata"]["allowed"] = False
        deepseek_out(usage_out)
        rec_last = self._selector(usage_out).select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.HIGH)
        self.assertEqual(rec_last.selected_model, MODEL_CLAUDE_FABLE)
        self.assertTrue(rec_last.cooldown_fallback)
        self.assertIn("last resort", rec_last.reasoning)
        # Ahead of pace, the orchestrator reserve is never touched: pay-per-token last resort.
        usage_reserve = copy.deepcopy(usage_out)
        set_week = usage_reserve["reports"][1]["limits"][1]
        set_week["amount"].update({"remainingFraction": 0.36, "usedFraction": 0.64, "remaining": 36.0, "used": 64.0})
        set_week["window"]["resetsAt"] = self.mock_now_ms + 72 * 3600 * 1000
        rec_reserve = self._selector(usage_reserve).select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.HIGH)
        self.assertNotIn("anthropic/", rec_reserve.selected_model)
        print(f"  [PASS] Every cheap tier out: {rec_last.selected_model} on slack; reserve kept -> {rec_reserve.selected_model}.")

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
