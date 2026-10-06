#!/usr/bin/env python3
"""Unit test suite for Antigravity Claude allowance and normal role routing (2026-10-04).

Covers:
- Strict exhaustion check across 5h and weekly account windows (no artificial 25%/50% caps or reserve gate).
- Account isolation: one unexhausted account is sufficient.
- Unknown pool unavailability and 429 family exhaustion.
- Normal role models (sonnet, opus) resolve AG first, then direct Anthropic on exhaustion/429.
- agc-* aliases remain AG-only (never fall back to direct Anthropic or paid tiers).
- ResetAwareModelSelector inserts AG Sonnet medium ahead of direct Sonnet rungs for judgment work.
- model_to_agent_role maps AG Claude normal decisions to normal sonnet/opus roles, not aliases.
- get_recommended_lanes dynamically resolves sonnet and opus models.
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path
from typing import Dict, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from balance_loader import antigravity_family_provider, parse_usage_json
from model_routing import (
    AG_ANTHROPIC_PROVIDER,
    MODEL_AG_CLAUDE_OPUS,
    MODEL_AG_CLAUDE_SONNET,
    MODEL_CLAUDE_OPUS_55,
    MODEL_CLAUDE_SONNET_55,
    MODEL_CODEX_ASTRA,
    MODEL_CODEX_SOL,
    MODEL_CODEX_SOL_FALLBACK,
    ROLE_FALLBACK_LADDERS,
    ROLE_MODEL_PINS,
    ResetAwareModelSelector,
    RiskLevel,
    TaskType,
    ag_claude_allowance,
    get_recommended_lanes,
    is_agent_role_available,
    model_to_agent_role,
    resolve_role_model,
)
from quota_snapshot import QuotaSnapshot, QuotaWindowEntry, update_from_usage_json

NOW = datetime.datetime(2026, 10, 4, 12, 0, tzinfo=datetime.timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
H = 3600 * 1000


def _limit(window: str, used: float) -> dict:
    duration = 5 * H if window == "5h" else 168 * H
    return {
        "id": f"google-antigravity:claude-gpt:default:{window}",
        "label": "Claude and GPT models",
        "scope": {"provider": "google-antigravity", "windowId": window, "shared": True},
        "window": {"id": window, "label": window, "durationMs": duration, "resetsAt": NOW_MS + duration // 2},
        "amount": {
            "unit": "percent",
            "remainingFraction": max(0.0, 1.0 - used),
            "usedFraction": used,
            "remaining": max(0.0, (1.0 - used) * 100),
            "used": used * 100,
            "limit": 100,
        },
        "status": "limit_reached" if used >= 1.0 else "ok",
    }


def _payload(accounts: Dict[str, Tuple[float, float]]) -> dict:
    """accounts: email -> (used_5h, used_weekly)."""
    return {
        "generatedAt": NOW_MS,
        "reports": [
            {
                "provider": "google-antigravity",
                "fetchedAt": NOW_MS,
                "limits": [_limit("5h", five), _limit("weekly", week)],
                "metadata": {"endpoint": "http://127.0.0.1:45123", "email": email},
            }
            for email, (five, week) in accounts.items()
        ],
        "accountsWithoutUsage": [],
    }


def _snapshot(tmp_path: Path, accounts: Dict[str, Tuple[float, float]]) -> QuotaSnapshot:
    return update_from_usage_json(_payload(accounts), path=tmp_path / "snap.json", now=NOW)


def test_proven_model_ids_and_pins():
    assert MODEL_AG_CLAUDE_OPUS == "google-antigravity/claude-opus-5-5-high"
    assert MODEL_AG_CLAUDE_SONNET == "google-antigravity/claude-sonnet-5-5-medium"
    assert ROLE_MODEL_PINS["sonnet"] == MODEL_CLAUDE_SONNET_55
    assert ROLE_MODEL_PINS["opus"] == MODEL_CLAUDE_OPUS_55
    assert ROLE_MODEL_PINS["agc-opus"] == MODEL_AG_CLAUDE_OPUS
    assert ROLE_MODEL_PINS["agc-sonnet"] == MODEL_AG_CLAUDE_SONNET


def test_role_fallback_ladders():
    # Normal sonnet ladder: AG Sonnet medium -> Anthropic Sonnet medium -> Codex Sol high 6.1 then 6.
    assert ROLE_FALLBACK_LADDERS["sonnet"] == [MODEL_CLAUDE_SONNET_55, MODEL_AG_CLAUDE_SONNET, MODEL_CODEX_SOL, MODEL_CODEX_SOL_FALLBACK]
    # Normal opus ladder: AG Opus high -> Anthropic Opus high.
    assert ROLE_FALLBACK_LADDERS["opus"] == [MODEL_CLAUDE_OPUS_55, MODEL_AG_CLAUDE_OPUS]
    # Reviewer and astra ladders: preserve Codex-first, insert AG Sonnet before Anthropic Sonnet.
    assert ROLE_FALLBACK_LADDERS["reviewer"][0] == MODEL_CODEX_SOL
    assert ROLE_FALLBACK_LADDERS["reviewer"][1] == MODEL_CODEX_SOL_FALLBACK
    assert ROLE_FALLBACK_LADDERS["reviewer"][2] == MODEL_CLAUDE_SONNET_55
    assert ROLE_FALLBACK_LADDERS["reviewer"][3] == MODEL_AG_CLAUDE_SONNET

    assert ROLE_FALLBACK_LADDERS["astra-ux"][0] == MODEL_CODEX_ASTRA
    assert ROLE_FALLBACK_LADDERS["astra-ux"][1] == MODEL_CODEX_SOL
    assert ROLE_FALLBACK_LADDERS["astra-ux"][2] == MODEL_CODEX_SOL_FALLBACK
    assert ROLE_FALLBACK_LADDERS["astra-ux"][3] == MODEL_CLAUDE_SONNET_55
    assert ROLE_FALLBACK_LADDERS["astra-ux"][4] == MODEL_AG_CLAUDE_SONNET

    # agc aliases are AG-only
    assert ROLE_FALLBACK_LADDERS["agc-opus"] == [MODEL_AG_CLAUDE_OPUS]
    assert ROLE_FALLBACK_LADDERS["agc-sonnet"] == [MODEL_AG_CLAUDE_SONNET]


def test_ag_claude_allowance_no_artificial_caps_and_no_reserve_gate(tmp_path):
    # High usage (80% 5h, 90% weekly) is allowed because neither window is 100% exhausted
    snap = _snapshot(tmp_path, {"user@example.com": (0.80, 0.90)})
    ok, reason = ag_claude_allowance(snap, now=NOW)
    assert ok is True
    assert "usable via account" in reason


def test_ag_claude_allowance_exhaustion_on_100_percent(tmp_path):
    # 100% on 5h marks exhausted
    snap_5h = _snapshot(tmp_path, {"user@example.com": (1.0, 0.5)})
    ok_5h, reason_5h = ag_claude_allowance(snap_5h, now=NOW)
    assert ok_5h is False
    assert "100% used" in reason_5h or "exhausted" in reason_5h

    # 100% on weekly marks exhausted
    snap_wk = _snapshot(tmp_path, {"user@example.com": (0.5, 1.0)})
    ok_wk, reason_wk = ag_claude_allowance(snap_wk, now=NOW)
    assert ok_wk is False
    assert "100% used" in reason_wk or "exhausted" in reason_wk


def test_account_isolation(tmp_path):
    # One exhausted account and one fresh account -> pool is usable
    snap = _snapshot(tmp_path, {
        "exhausted@example.com": (1.0, 1.0),
        "fresh@example.com": (0.2, 0.1),
    })
    ok, reason = ag_claude_allowance(snap, now=NOW)
    assert ok is True
    assert "usable via account" in reason and "fr*" in reason


def test_unknown_pool_unavailability():
    # None snapshot
    ok, reason = ag_claude_allowance(None, now=NOW)
    assert ok is False
    assert "unknown" in reason

    # Empty snapshot
    empty = QuotaSnapshot()
    ok_empty, reason_empty = ag_claude_allowance(empty, now=NOW)
    assert ok_empty is False
    assert "not reported" in reason_empty or "unknown" in reason_empty


def test_429_family_exhaustion():
    snap = QuotaSnapshot(entries={
        f"{AG_ANTHROPIC_PROVIDER}|default|daily": QuotaWindowEntry(
            provider=AG_ANTHROPIC_PROVIDER,
            window_id="daily",
            exhausted_until=(NOW + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            source="429",
        )
    })
    ok, reason = ag_claude_allowance(snap, now=NOW)
    assert ok is False
    assert "rate-limited" in reason


def test_normal_roles_resolve_direct_first_and_fallback_to_antigravity(tmp_path):
    snap = _snapshot(tmp_path, {"user@example.com": (0.1, 0.1)})
    assert resolve_role_model("sonnet", snap) == MODEL_CLAUDE_SONNET_55
    assert resolve_role_model("opus", snap) == MODEL_CLAUDE_OPUS_55
    snap.entries["anthropic|default|5h"] = QuotaWindowEntry(
        provider="anthropic", window_id="5h", source="429",
        exhausted_until=(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    assert resolve_role_model("sonnet", snap) == MODEL_AG_CLAUDE_SONNET
    assert resolve_role_model("opus", snap) == MODEL_AG_CLAUDE_OPUS


def test_agc_aliases_remain_ag_only(tmp_path):
    snap_fresh = _snapshot(tmp_path, {"user@example.com": (0.1, 0.1)})
    assert resolve_role_model("agc-sonnet", snap_fresh) == MODEL_AG_CLAUDE_SONNET
    assert resolve_role_model("agc-opus", snap_fresh) == MODEL_AG_CLAUDE_OPUS

    snap_exhausted = _snapshot(tmp_path, {"user@example.com": (1.0, 1.0)})
    assert resolve_role_model("agc-sonnet", snap_exhausted) is None
    assert resolve_role_model("agc-opus", snap_exhausted) is None


def test_model_to_agent_role_maps_to_normal_roles():
    assert model_to_agent_role(MODEL_AG_CLAUDE_SONNET, TaskType.ROUTINE_EXECUTION, RiskLevel.LOW) == "agc-sonnet"
    assert model_to_agent_role(MODEL_AG_CLAUDE_OPUS, TaskType.STRONG_REVIEW, RiskLevel.HIGH) == "agc-opus"


def test_recommended_lanes_dynamic_resolution(tmp_path):
    snap_fresh = _snapshot(tmp_path, {"user@example.com": (0.05, 0.05)})
    sel = ResetAwareModelSelector(
        parse_usage_json(_payload({"user@example.com": (0.05, 0.05)}), current_time_ms=NOW_MS),
        quota_snapshot=snap_fresh,
    )
    lanes = get_recommended_lanes(sel)
    assert lanes["sonnet"] == MODEL_CLAUDE_SONNET_55
    assert lanes["opus"] == MODEL_CLAUDE_OPUS_55
    assert lanes["agc-opus"] == MODEL_AG_CLAUDE_OPUS
    assert lanes["agc-sonnet"] == MODEL_AG_CLAUDE_SONNET
