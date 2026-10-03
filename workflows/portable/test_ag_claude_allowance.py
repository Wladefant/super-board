#!/usr/bin/env python3
"""Antigravity Claude is a small, separately tracked allowance (operator 2026-10-03).

The usage payloads below have the exact shape `veyyon usage --json` returns today: one shared
`claude-gpt` pool per Google account with a 5h and a weekly window.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from balance_loader import antigravity_family_provider
from model_routing import (
    AG_ANTHROPIC_PROVIDER,
    AG_CLAUDE_MAX_USED_5H,
    AG_CLAUDE_MAX_USED_WEEKLY,
    MODEL_AG_CLAUDE_OPUS,
    MODEL_AG_CLAUDE_SONNET,
    ROLE_MODEL_PINS,
    ag_claude_allowance,
    is_agent_role_available,
    resolve_role_model,
)
from quota_snapshot import QuotaSnapshot, update_from_usage_json

NOW = datetime(2026, 10, 3, 22, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
H = 3600 * 1000


def _limit(window: str, used: float) -> dict:
    duration = 5 * H if window == "5h" else 168 * H
    return {
        "id": f"google-antigravity:claude-gpt:default:{window}",
        "label": "Claude and GPT models",
        "scope": {"provider": "google-antigravity", "windowId": window, "shared": True},
        "window": {"id": window, "label": window, "durationMs": duration, "resetsAt": NOW_MS + duration // 2},
        "amount": {"unit": "percent", "remainingFraction": 1 - used, "usedFraction": used,
                   "remaining": (1 - used) * 100, "used": used * 100, "limit": 100},
        "status": "limit_reached" if used >= 1.0 else "ok",
    }


def _payload(accounts: dict) -> dict:
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


def _snapshot(tmp_path: Path, accounts: dict) -> QuotaSnapshot:
    return update_from_usage_json(_payload(accounts), path=tmp_path / "snap.json", now=NOW)


def test_claude_gpt_pool_is_the_anthropic_family():
    assert antigravity_family_provider("google-antigravity:claude-gpt:default:5h") == AG_ANTHROPIC_PROVIDER
    assert antigravity_family_provider("google-antigravity:claude-gpt:default:weekly") == AG_ANTHROPIC_PROVIDER
    assert antigravity_family_provider("google-antigravity:google:default:5h") == "google-antigravity"


def test_live_payload_reaches_the_router_under_the_claude_provider(tmp_path):
    snap = _snapshot(tmp_path, {"a@example.com": (0.0128, 0.0064), "b@example.com": (0.0, 0.0)})
    windows = {(e.account, e.window_id) for e in snap.entries.values() if e.provider == AG_ANTHROPIC_PROVIDER}
    assert len({acc for acc, _ in windows}) == 2
    assert {w for _, w in windows} == {"5h", "weekly"}
    assert ag_claude_allowance(snap)[0] is True


def test_fresh_pool_serves_both_agc_lanes_on_the_5_5_models(tmp_path):
    snap = _snapshot(tmp_path, {"a@example.com": (0.01, 0.0)})
    assert resolve_role_model("agc-opus", snap) == MODEL_AG_CLAUDE_OPUS == "google-antigravity/claude-opus-5-5-medium"
    assert resolve_role_model("agc-sonnet", snap) == MODEL_AG_CLAUDE_SONNET == "google-antigravity/claude-sonnet-5-5-medium"
    assert ROLE_MODEL_PINS["agc-opus"] == MODEL_AG_CLAUDE_OPUS
    assert is_agent_role_available("agc-opus") in (True, False)  # reads the on-disk snapshot; must not raise


def test_cap_pauses_opus_and_moves_sonnet_off_antigravity_never_to_anthropic(tmp_path, monkeypatch):
    monkeypatch.setenv("VEYYON_CODEX_ENABLED", "1")
    over_5h = AG_CLAUDE_MAX_USED_5H + 0.01
    snap = _snapshot(tmp_path, {"a@example.com": (over_5h, 0.0), "b@example.com": (over_5h, 0.0)})
    ok, reason = ag_claude_allowance(snap)
    assert ok is False and "5h" in reason
    assert resolve_role_model("agc-opus", snap) is None  # UI/design pauses, no other rung
    sonnet = resolve_role_model("agc-sonnet", snap)
    assert sonnet is not None and sonnet.startswith("openai-codex/")
    monkeypatch.setenv("VEYYON_CODEX_ENABLED", "0")
    sonnet_no_codex = resolve_role_model("agc-sonnet", snap)
    assert sonnet_no_codex is not None and not sonnet_no_codex.startswith(("anthropic/", "google-antigravity/"))


def test_weekly_cap_blocks_even_when_the_5h_window_is_fresh(tmp_path):
    snap = _snapshot(tmp_path, {"a@example.com": (0.0, AG_CLAUDE_MAX_USED_WEEKLY)})
    ok, reason = ag_claude_allowance(snap)
    assert ok is False and "weekly" in reason


def test_one_account_under_cap_is_enough(tmp_path):
    snap = _snapshot(tmp_path, {"a@example.com": (0.9, 0.5), "b@example.com": (0.02, 0.01)})
    assert ag_claude_allowance(snap)[0] is True
    assert resolve_role_model("agc-opus", snap) == MODEL_AG_CLAUDE_OPUS


def test_exhausted_pool_blocks_and_unreported_pool_is_never_assumed(tmp_path):
    exhausted = _snapshot(tmp_path, {"a@example.com": (1.0, 0.1)})
    assert ag_claude_allowance(exhausted)[0] is False
    assert ag_claude_allowance(None)[0] is False
    assert ag_claude_allowance(QuotaSnapshot())[0] is False
    assert resolve_role_model("agc-opus", QuotaSnapshot()) is None
