"""
Unit and behavioral tests for Issue #216 Item 3:
Dispatch-visible note when a routing recommendation's fallback tier is out / unavailable.

Validates that:
1. When a fallback model's provider is exhausted or in cooldown/limit_reached,
   a clear human-readable note is emitted at dispatch time by:
   - model_routing.ResetAwareModelSelector.dispatch() (HarnessDispatchPacket)
   - coordinator.format_packet_summary() (Coordinator human-readable summary)
   - superboard_adapter.format_adapter_summary() (Adapter execution summary)
   - superboard_adapter.dispatch_via_backend() (WorkerRequest payload)
2. When the fallback provider is healthy and available, no warning note is emitted (fallback_note is None).
3. Model selection and fallback model choice are strictly preserved (no degradation of choice).
4. Out-tier status from QuotaSnapshot (with exhausted_until ISO timestamp) or balance usage
   status (e.g. limit_reached) is accurately reflected in the note.
"""

import copy
import datetime
from datetime import timezone
import unittest
from typing import Any, Dict, List, Optional

import os
import sys
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
if TEST_DIR not in sys.path:
    sys.path.insert(0, TEST_DIR)

from balance_loader import parse_usage_json
from coordinator import (
    CoordinatorPacket,
    DecisionStatus,
    PreflightStatus,
    RoutingStatus,
    format_packet_summary,
)
from model_routing import (
    MODEL_DEEPSEEK_FLASH,
    MODEL_DEEPSEEK_PRO,
    MODEL_GEMINI_FLASH,
    MODEL_GEMINI_PRO,
    MODEL_GO_BUNNY,
    MODEL_OR_DEEPSEEK_FLASH,
    HarnessDispatchPacket,
    ResetAwareModelSelector,
    RiskLevel,
    RoutingRecommendation,
    TaskType,
)
from quota_snapshot import QuotaSnapshot, QuotaWindowEntry
from superboard_adapter import (
    AdapterExecutionResult,
    SuperboardExecutionAdapter,
    format_adapter_summary,
)


class TestOutTierFallbackNote(unittest.TestCase):
    """Test dispatch-visible out-tier fallback note behavior across router and consumers."""

    def setUp(self):
        self.mock_now_ms = 1727000000000  # Fixed epoch for determinism
        self.exhausted_usage_dict: Dict[str, Any] = {
            "reports": [
                {
                    "provider": "google-antigravity",
                    "account": "default",
                    "metadata": {"limitReached": True, "allowed": False},
                    "limits": [
                        {
                            "name": "daily",
                            "status": "limit_reached",
                            "window_seconds": 86400,
                            "remaining_fraction": 0.0,
                            "resets_in_seconds": 7200,
                        }
                    ],
                },
                {
                    "provider": "deepseek",
                    "account": "default",
                    "metadata": {"limitReached": True, "allowed": False},
                    "limits": [
                        {
                            "name": "daily",
                            "status": "limit_reached",
                            "window_seconds": 86400,
                            "remaining_fraction": 0.0,
                            "resets_in_seconds": 7200,
                        }
                    ],
                },
                {
                    "provider": "opencode-go",
                    "account": "default",
                    "metadata": {"allowed": True},
                    "limits": [
                        {
                            "name": "weekly",
                            "status": "ok",
                            "window_seconds": 604800,
                            "remaining_fraction": 0.85,
                            "resets_in_seconds": 120000,
                        }
                    ],
                },
            ]
        }

        self.healthy_usage_dict: Dict[str, Any] = {
            "reports": [
                {
                    "provider": "google-antigravity",
                    "account": "default",
                    "metadata": {"limitReached": False, "allowed": True},
                    "limits": [
                        {
                            "name": "daily",
                            "status": "ok",
                            "window_seconds": 86400,
                            "remaining_fraction": 0.90,
                            "resets_in_seconds": 72000,
                        }
                    ],
                },
                {
                    "provider": "deepseek",
                    "account": "default",
                    "metadata": {"limitReached": False, "allowed": True},
                    "limits": [
                        {
                            "name": "daily",
                            "status": "ok",
                            "window_seconds": 86400,
                            "remaining_fraction": 0.95,
                            "resets_in_seconds": 72000,
                        }
                    ],
                },
                {
                    "provider": "opencode-go",
                    "account": "default",
                    "metadata": {"allowed": True},
                    "limits": [
                        {
                            "name": "weekly",
                            "status": "ok",
                            "window_seconds": 604800,
                            "remaining_fraction": 0.85,
                            "resets_in_seconds": 120000,
                        }
                    ],
                },
            ]
        }

    def test_exhausted_fallback_carries_visible_dispatch_note(self):
        """When the chosen fallback model is on an exhausted tier, a visible note is emitted."""
        snapshot = parse_usage_json(self.exhausted_usage_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot, quota_snapshot=QuotaSnapshot())

        rec = selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertIsNotNone(rec.fallback_note)
        self.assertIn(rec.fallback_model, rec.fallback_note)
        self.assertIn("limit_reached", rec.fallback_note)

        # 1. HarnessDispatchPacket recommendation carries fallback_note
        packet = selector.dispatch(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertIsInstance(packet, HarnessDispatchPacket)
        rec_data = packet.recommendation
        self.assertIn("fallback_note", rec_data)
        self.assertEqual(rec_data["fallback_note"], rec.fallback_note)
        self.assertIn("out", rec_data["fallback_note"])

        # 2. Coordinator summary visibly emits fallback note
        rt = RoutingStatus(
            evaluated=True,
            recommended_model=rec.selected_model,
            recommended_role=rec_data.get("agent_role"),
            fallback_model=rec.fallback_model,
            fallback_note=rec.fallback_note,
            rationale=rec.reasoning,
        )
        coord_packet = CoordinatorPacket(
            schema_version="1.0",
            generated_at_utc="2026-10-03T12:00:00Z",
            status="ready",
            status_reason="ok",
            next_action="execute",
            request=None,
            decision_status=DecisionStatus(True, True, "ok", 0, False),
            preflight=PreflightStatus(evaluated=True, passed=True, status="ok"),
            routing=rt,
            evidence_packet=None,
        )
        coord_summary = format_packet_summary(coord_packet)
        self.assertIn("Fallback Model:  " + rec.fallback_model, coord_summary)
        self.assertIn("NOTE:", coord_summary)
        self.assertIn(rec.fallback_note, coord_summary)

        # 3. Superboard adapter summary visibly emits fallback model and note
        adapter_res = AdapterExecutionResult(
            step_id="step-test-01",
            request_id="req-test-01",
            stage="build",
            status="advanced",
            status_reason="ok",
            next_action="continue",
            preflight_passed=True,
            dispatch_packet=packet.to_dict(),
        )
        adapter_summary = format_adapter_summary(adapter_res)
        self.assertIn("Fallback Model:  " + rec.fallback_model, adapter_summary)
        self.assertIn("NOTE:", adapter_summary)
        self.assertIn(rec.fallback_note, adapter_summary)
        self.assertIn("Assigned Model:  " + rec.selected_model, adapter_summary)

    def test_healthy_fallback_has_no_warning_note(self):
        """When all providers are healthy, fallback_note is None and no warning note is emitted."""
        snapshot = parse_usage_json(self.healthy_usage_dict, current_time_ms=self.mock_now_ms)
        selector = ResetAwareModelSelector(snapshot, quota_snapshot=QuotaSnapshot())

        rec = selector.select_model(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertIsNone(rec.fallback_note)

        packet = selector.dispatch(task_type=TaskType.ROUTINE_EXECUTION, risk_level=RiskLevel.LOW)
        self.assertIsNone(packet.recommendation.get("fallback_note"))

        rt = RoutingStatus(
            evaluated=True,
            recommended_model=rec.selected_model,
            recommended_role="task",
            fallback_model=rec.fallback_model,
            fallback_note=None,
            rationale="testing healthy",
        )
        coord_packet = CoordinatorPacket(
            schema_version="1.0",
            generated_at_utc="2026-10-03T12:00:00Z",
            status="ready",
            status_reason="ok",
            next_action="execute",
            request=None,
            decision_status=DecisionStatus(True, True, "ok", 0, False),
            preflight=PreflightStatus(evaluated=True, passed=True, status="ok"),
            routing=rt,
            evidence_packet=None,
        )
        coord_summary = format_packet_summary(coord_packet)
        self.assertIn("Fallback Model:  " + rec.fallback_model, coord_summary)
        self.assertNotIn("NOTE:", coord_summary)

        adapter_res = AdapterExecutionResult(
            step_id="step-test-healthy",
            request_id="req-test-healthy",
            stage="build",
            status="advanced",
            status_reason="ok",
            next_action="continue",
            preflight_passed=True,
            dispatch_packet=packet.to_dict(),
        )
        adapter_summary = format_adapter_summary(adapter_res)
        self.assertIn("Fallback Model:  " + rec.fallback_model, adapter_summary)
        self.assertNotIn("NOTE:", adapter_summary)

    def test_quota_snapshot_exhaustion_timestamp_in_note(self):
        """When QuotaSnapshot records future exhaustion, the note includes the timestamp."""
        qs = QuotaSnapshot()
        until_dt = datetime.datetime.now(timezone.utc) + datetime.timedelta(hours=3)
        until_iso = until_dt.isoformat()
        qs.entries["google-antigravity|default|daily"] = QuotaWindowEntry(
            provider="google-antigravity",
            window_id="daily",
            account="default",
            used_fraction=1.0,
            exhausted_until=until_iso,
            fetched_at=datetime.datetime.now(timezone.utc).isoformat(),
            source="test",
        )

        selector = ResetAwareModelSelector(quota_snapshot=qs)

        note = selector.resolve_fallback_note("google-antigravity/gemini-3.1-pro")
        self.assertIsNotNone(note)
        self.assertIn("google-antigravity", note)
        self.assertIn("exhausted until", note)
        self.assertIn("gemini-3.1-pro", note)

    def test_selection_preserved_identically(self):
        """Verify that fallback_note does not change model or fallback selection."""
        snapshot_exhausted = parse_usage_json(self.exhausted_usage_dict, current_time_ms=self.mock_now_ms)
        selector_exhausted = ResetAwareModelSelector(snapshot_exhausted, quota_snapshot=QuotaSnapshot())

        for task_type in TaskType:
            for risk_level in RiskLevel:
                if task_type == TaskType.TINY_TASK and risk_level == RiskLevel.HIGH:
                    with self.assertRaisesRegex(ValueError, "TINY_TASK must not carry"):
                        selector_exhausted.select_model(task_type=task_type, risk_level=risk_level)
                    with self.assertRaisesRegex(ValueError, "TINY_TASK must not carry"):
                        selector_exhausted.dispatch(task_type=task_type, risk_level=risk_level)
                    continue
                rec = selector_exhausted.select_model(task_type=task_type, risk_level=risk_level)
                packet = selector_exhausted.dispatch(task_type=task_type, risk_level=risk_level)
                self.assertEqual(rec.selected_model, packet.recommendation["model"])
                self.assertEqual(rec.fallback_model, packet.recommendation["fallback_model"])
                self.assertEqual(rec.fallback_note, packet.recommendation.get("fallback_note"))
    def test_backend_receives_fallback_note_on_dispatch(self):
        """WorkerBackend receives fallback_model and fallback_note in worker_request."""
        class MockBackend:
            def __init__(self):
                self.default_backend = "native"
                self.calls = []

            def prepare_native(self, request_payload):
                self.calls.append(request_payload)
                class MockTicket:
                    run_id = "test-run-001"
                    state = "prepared"
                    head_sha = "abcd1234efgh"
                    blocked_reason = None
                    def to_dict(self):
                        return {"run_id": self.run_id}
                return MockTicket()

        mock_backend = MockBackend()
        adapter = SuperboardExecutionAdapter(
            worker_backend=mock_backend,
            notify_telegram=False,
        )

        # Synthesize a dispatch packet with an exhausted fallback note
        dp = HarnessDispatchPacket(
            schema_version="1.0",
            generated_at_utc="2026-10-03T12:00:00Z",
            task={"task_type": "routine_execution", "risk_level": "low"},
            recommendation={
                "model": "openrouter/deepseek/deepseek-v4.1-flash",
                "agent_role": "ds-task",
                "fallback_model": "deepseek/deepseek-flash:high",
                "fallback_agent_role": "ds-task",
                "fallback_note": "fallback model 'deepseek/deepseek-flash:high' tier is currently out (deepseek: limit_reached)",
            },
            quota_context={},
        )

        res = adapter.dispatch_via_backend(
            req={"id": "req-fb-01", "state": "implementation", "prompt": "test prompt", "head": "abcd1234efgh"},
            stage="build",
            dispatch=dp,
            target_sha="abcd1234efgh",
        )
        self.assertEqual(len(mock_backend.calls), 1)
        dispatched_req = mock_backend.calls[0]
        self.assertEqual(dispatched_req["fallback_model"], "deepseek/deepseek-flash:high")
        self.assertEqual(
            dispatched_req["fallback_note"],
            "fallback model 'deepseek/deepseek-flash:high' tier is currently out (deepseek: limit_reached)",
        )


if __name__ == "__main__":
    unittest.main()
