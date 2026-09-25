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
import json
import os
import sys
import unittest

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
from model_routing import (
    EvidencePacket,
    HarnessDispatchPacket,
    ResetAwareModelSelector,
    RiskLevel,
    TaskType,
    MODEL_CLAUDE_FABLE,
    MODEL_CLAUDE_OPUS,
    MODEL_CODEX_FAST,
    MODEL_CODEX_SOL,
    MODEL_CODEX_ASTRA,
    MODEL_GEMINI_FLASH,
    MODEL_GEMINI_LITE,
    MODEL_GEMINI_PRO,
    MODEL_AG_CLAUDE_OPUS,
    MODEL_DEEPSEEK_FLASH,
    MODEL_OR_DEEPSEEK_FLASH,
    MODEL_OR_FREE_ADVISORY,
    model_to_agent_role,
    model_to_provider,
)


class TestBalanceLoaderAndRouting(unittest.TestCase):

    def setUp(self):
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

        # A) High-Risk Review: should promote Codex Sol to prevent allowance expiration!
        rec_review = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertTrue(rec_review.promotion_applied)
        self.assertEqual(rec_review.selected_model, MODEL_CODEX_SOL)
        self.assertIn("Promoted Codex Sol", rec_review.reasoning)
        print(f"  [PASS] High-risk review promoted Codex Sol: {rec_review.selected_model}")

        # B) Deep Reasoning: should promote Codex Sol
        rec_reasoning = selector.select_model(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.MEDIUM)
        self.assertTrue(rec_reasoning.promotion_applied)
        self.assertEqual(rec_reasoning.selected_model, MODEL_CODEX_SOL)
        print(f"  [PASS] Deep reasoning promoted Codex Sol: {rec_reasoning.selected_model}")

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

        # Deep reasoning with distant Anthropic reset preserves Anthropic and routes to Gemini 3.8 Flash
        rec_reason = selector.select_model(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.LOW)
        self.assertEqual(rec_reason.selected_model, MODEL_GEMINI_FLASH)
        self.assertIn("Preserving distant-reset Anthropic", rec_reason.reasoning)
        print(f"  [PASS] Deep reasoning preserved Anthropic: {rec_reason.selected_model}")

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

        # Routine execution with rework_count=1 must escalate to strong model Sol/Opus (Flash barred)
        rec = selector.select_model(
            task_type=TaskType.ROUTINE_EXECUTION,
            risk_level=RiskLevel.LOW,
            rework_count=1,
        )
        self.assertNotEqual(rec.selected_model, MODEL_GEMINI_FLASH)
        self.assertIn(rec.selected_model, (MODEL_CODEX_SOL, MODEL_CLAUDE_OPUS))
        self.assertNotEqual(rec.fallback_model, MODEL_GEMINI_FLASH)
        self.assertIn("prevent invariant rework", rec.reasoning.lower())
        print(f"  [PASS] Rework escalation forced strong model: {rec.selected_model} (fallback: {rec.fallback_model})")

    # -------------------------------------------------------------------------
    # TEST 18: Domain Tags Escalation (C9 Invariant)
    # -------------------------------------------------------------------------
    def test_domain_tags_escalation(self):
        print("\n--- TEST 18: Domain Tags Escalation (C9) ---")
        snapshot = parse_usage_json(self.mock_usage_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot)

        # High-risk domain tags: auth, state_machine, money, concurrency, migrations
        rec = selector.select_model(
            task_type=TaskType.ROUTINE_EXECUTION,
            risk_level=RiskLevel.LOW,
            domain_tags=["auth", "state_machine"],
        )
        self.assertNotEqual(rec.selected_model, MODEL_GEMINI_FLASH)
        self.assertIn(rec.selected_model, (MODEL_CODEX_SOL, MODEL_CLAUDE_OPUS))
        print(f"  [PASS] Domain tags escalated to strong model: {rec.selected_model}")

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
        self.assertIn(rec_rev.fallback_model, (MODEL_CLAUDE_OPUS, MODEL_CLAUDE_FABLE, MODEL_CODEX_SOL, MODEL_CODEX_ASTRA, MODEL_GEMINI_PRO))

        # B) High-risk deep reasoning
        rec_reason = selector.select_model(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.HIGH)
        self.assertNotEqual(rec_reason.selected_model, MODEL_GEMINI_FLASH)
        self.assertNotEqual(rec_reason.fallback_model, MODEL_GEMINI_FLASH)
        self.assertIn(rec_reason.fallback_model, (MODEL_CLAUDE_OPUS, MODEL_CLAUDE_FABLE, MODEL_CODEX_SOL, MODEL_CODEX_ASTRA, MODEL_GEMINI_PRO))
        print(f"  [PASS] High-risk reasoning and review fallback strictly strong model: {rec_reason.fallback_model} (Flash barred).")

    # -------------------------------------------------------------------------
    # TEST 21: Codex Agent Roles & Structural Retry Flash Bar
    # -------------------------------------------------------------------------
    def test_codex_roles_and_structural_retry_invariants(self):
        print("\n--- TEST 21: Codex Agent Roles & Structural Failure Retry ---")
        # 1. Verify model_to_agent_role assigns actual Codex agent roles from roster
        self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "codex-reviewer")
        self.assertEqual(model_to_agent_role(MODEL_CODEX_SOL, TaskType.ROUTINE_EXECUTION, RiskLevel.HIGH), "codex-worker")
        self.assertEqual(model_to_agent_role(MODEL_CODEX_FAST, TaskType.ROUTINE_EXECUTION, RiskLevel.LOW), "codex-worker")
        self.assertEqual(model_to_agent_role(MODEL_CODEX_ASTRA, TaskType.STRONG_REVIEW, RiskLevel.HIGH), "codex-reviewer")

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

        # 3. Flash NOT structural-failure retry:
        # When all strong models are in cooldown, routine execution with rework_count=1 MUST route to Gemini Pro, NEVER Flash!
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
        self.assertNotEqual(rec_structural.selected_model, MODEL_GEMINI_FLASH)
        self.assertEqual(rec_structural.selected_model, MODEL_GEMINI_PRO)
        self.assertNotEqual(rec_structural.fallback_model, MODEL_GEMINI_FLASH)
        self.assertEqual(rec_structural.fallback_model, MODEL_GEMINI_PRO)
        self.assertTrue(rec_structural.cooldown_fallback)
        print(f"  [PASS] Structural failure retry strictly barred Flash under cooldown: {rec_structural.selected_model}")

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
        for task_type in (TaskType.STRONG_REVIEW, TaskType.DEEP_REASONING, TaskType.ROUTINE_EXECUTION):
            rec = selector.select_model(task_type=task_type, risk_level=RiskLevel.HIGH)
            self.assertEqual(rec.selected_model, MODEL_AG_CLAUDE_OPUS, f"{task_type}: ahead-of-pace Codex chosen")
            self.assertNotEqual(rec.fallback_model, MODEL_GEMINI_FLASH)
        self.assertTrue(rec.quota_metrics["codex_pro_throttled"])

        # Routine work with Gemini in cooldown must not drain ahead-of-pace Codex either.
        for lim in usage["reports"][0]["limits"]:
            if lim["id"].startswith("google-antigravity:google"):
                lim["status"] = "rate_limited"
        rec_routine = self._selector(usage).select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertNotIn("openai-codex/", rec_routine.selected_model)
        print(f"  [PASS] 92% used with 145h left: Codex held back; high-risk work on {MODEL_AG_CLAUDE_OPUS}.")

        # The same 92% with 6h left is behind pace: the last 8% would expire unused, so it
        # is promoted ahead of every other strong lane, including Antigravity Claude.
        usage_end = self._usage_with_ag_families(codex_used=0.92, codex_reset_hrs=6)
        rec_end = self._selector(usage_end).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertFalse(rec_end.quota_metrics["codex_pro_throttled"])
        self.assertTrue(rec_end.promotion_applied)
        self.assertEqual(rec_end.selected_model, MODEL_CODEX_SOL)
        print(f"  [PASS] 92% used with 6h left: remaining Codex spent before reset on {rec_end.selected_model}.")

        # Exactly on pace (50% used, 84h left) is neither throttled nor promoted.
        usage_pace = self._usage_with_ag_families(anthropic_used=0.95, codex_used=0.49, codex_reset_hrs=84)
        self._block_anthropic(usage_pace)
        rec_pace = self._selector(usage_pace).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertFalse(rec_pace.quota_metrics["codex_pro_throttled"])
        self.assertFalse(rec_pace.promotion_applied)
        self.assertEqual(rec_pace.selected_model, MODEL_CODEX_SOL)
        print(f"  [PASS] On-pace Codex remains a normal strong lane: {rec_pace.selected_model}")

    # -------------------------------------------------------------------------
    # TEST 26: Direct Anthropic is the Opus orchestrator's weekly budget
    # -------------------------------------------------------------------------
    def test_anthropic_orchestrator_reserve(self):
        print("\n--- TEST 26: Anthropic Orchestrator Reserve ---")
        # 64% of the week used with 72h left (57% elapsed): ahead of pace, so workers leave it
        # to the orchestrator even for high-risk review while Codex is on pace.
        usage = self._usage_with_ag_families(anthropic_used=0.95, anthropic_week_used=0.64, anthropic_week_reset_hrs=72)
        rec = self._selector(usage).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertTrue(rec.quota_metrics["anthropic_orchestrator_reserve"])
        self.assertEqual(rec.selected_model, MODEL_CODEX_SOL)
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

        # 30% used with 20h left: most of the week would expire unused, so workers take it,
        # medium-risk work included.
        usage_surplus = self._usage_with_ag_families(anthropic_used=0.95, anthropic_week_used=0.30, anthropic_week_reset_hrs=20)
        selector = self._selector(usage_surplus)
        self.assertFalse(selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
                         .quota_metrics["anthropic_orchestrator_reserve"])
        self.assertEqual(
            selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH).selected_model,
            MODEL_CLAUDE_FABLE,
        )
        self.assertEqual(
            selector.select_model(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.MEDIUM).selected_model,
            MODEL_CLAUDE_OPUS,
        )
        print("  [PASS] Anthropic surplus near reset spent by workers (Fable review, Opus reasoning).")

        # Stale Anthropic data never counts as slack.
        stale = self._usage_with_ag_families(anthropic_used=0.95, anthropic_week_used=0.0, anthropic_week_reset_hrs=20)
        stale["reports"][1]["fetchedAt"] = self.mock_now_ms - 2 * 3600 * 1000
        rec_stale = self._selector(stale).select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.HIGH)
        self.assertTrue(rec_stale.quota_metrics["anthropic_orchestrator_reserve"])
        self.assertEqual(rec_stale.selected_model, MODEL_CODEX_SOL)
        print(f"  [PASS] Stale Anthropic snapshot kept in reserve; routed to {rec_stale.selected_model}.")

    # -------------------------------------------------------------------------
    # TEST 23: Antigravity Claude daily window is spent before paid Anthropic
    # -------------------------------------------------------------------------
    def test_antigravity_claude_chosen(self):
        print("\n--- TEST 23: Antigravity Claude Chosen Before Paid Anthropic ---")
        selector = self._selector(self._usage_with_ag_families(anthropic_used=0.0))
        rec_review = selector.select_model(task_type=TaskType.STRONG_REVIEW, risk_level=RiskLevel.MEDIUM)
        self.assertEqual(rec_review.selected_model, MODEL_AG_CLAUDE_OPUS)
        rec_reason = selector.select_model(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.MEDIUM)
        self.assertEqual(rec_reason.selected_model, MODEL_AG_CLAUDE_OPUS)
        self.assertEqual(model_to_agent_role(MODEL_AG_CLAUDE_OPUS, TaskType.STRONG_REVIEW, RiskLevel.MEDIUM), "ag-opus")
        packet = selector.dispatch(task_type=TaskType.DEEP_REASONING, risk_level=RiskLevel.MEDIUM)
        self.assertEqual(packet.recommendation["agent_role"], "ag-opus")
        print(f"  [PASS] Medium review/reasoning on {rec_review.selected_model} (agent ag-opus).")

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
