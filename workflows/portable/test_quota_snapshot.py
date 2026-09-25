#!/usr/bin/env python3
"""
test_quota_snapshot.py - Hermetic tests for local quota-snapshot cache.

Covers:
  1. Entry marked exhausted is NOT eligible; another provider is still eligible.
  2. Entry whose exhausted_until is in the past IS eligible again; boundary flip.
  3. Parsing 429 error bodies (absolute timestamp, Google RetryInfo, Anthropic message,
     retry_after seconds) updates snapshot and marks provider ineligible.
  4. refresh_from_usage skips fresh snapshot, refreshes stale/forced, and survives runner failure.
  5. Missing, empty, or malformed snapshot JSON loads as empty QuotaSnapshot without raising.
  6. to_dict / from_dict round-trip and atomic write leaving no temp files behind.
  7. update_from_usage_json preserves existing future exhausted_until and extracts used fractions.
  8. parse_quota_error duration variations and non-quota error handling.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure workflows/portable is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from quota_snapshot import (
    QuotaReset,
    QuotaSnapshot,
    QuotaWindowEntry,
    apply_quota_error,
    load_snapshot,
    mark_exhausted,
    parse_quota_error,
    refresh_from_usage,
    save_snapshot,
    update_from_usage_json,
)


# ---------------------------------------------------------------------------
# TEST 1: Exhausted entry is ineligible, other provider remains eligible
# ---------------------------------------------------------------------------
def test_exhausted_entry_not_eligible_other_eligible(tmp_path: Path):
    """An entry marked exhausted is NOT eligible, while another provider is still eligible."""
    snap_path = tmp_path / "quota-snapshot.json"
    now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
    future_reset = "2026-09-25T14:00:00Z"

    # Mark google-antigravity:anthropic daily window exhausted
    snapshot = mark_exhausted(
        provider="google-antigravity:anthropic",
        window_id="daily",
        exhausted_until=future_reset,
        path=snap_path,
        now=now,
    )

    # Provider should not be eligible
    assert snapshot.is_eligible("google-antigravity:anthropic", now=now) is False
    assert snapshot.is_eligible("google-antigravity:anthropic", "daily", now=now) is False

    # Another provider without exhausted entries should be eligible
    assert snapshot.is_eligible("anthropic", now=now) is True
    assert snapshot.is_eligible("openai-codex", now=now) is True

    # An unexhausted window of the same provider is eligible when queried specifically
    assert snapshot.is_eligible("google-antigravity:anthropic", "weekly", now=now) is True

    # Persisted snapshot read from disk behaves identically
    reloaded = load_snapshot(snap_path)
    assert reloaded.is_eligible("google-antigravity:anthropic", now=now) is False
    assert reloaded.is_eligible("anthropic", now=now) is True


# ---------------------------------------------------------------------------
# TEST 2: Reset passed in past is eligible again; exact boundary flip
# ---------------------------------------------------------------------------
def test_exhausted_until_in_past_and_boundary_flip(tmp_path: Path):
    """An entry whose exhausted_until is in past is eligible again; flips exactly at boundary."""
    snap_path = tmp_path / "quota-snapshot.json"
    reset_time = datetime(2026, 9, 25, 14, 0, 0, tzinfo=timezone.utc)
    reset_str = "2026-09-25T14:00:00Z"

    snapshot = mark_exhausted(
        provider="anthropic",
        window_id="5h",
        exhausted_until=reset_str,
        path=snap_path,
        now=datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc),
    )

    entry = snapshot.entry("anthropic", "5h")
    assert entry is not None

    # 1 second before reset: NOT eligible
    now_before = reset_time - timedelta(seconds=1)
    assert entry.is_exhausted(now_before) is True
    assert snapshot.is_eligible("anthropic", "5h", now=now_before) is False
    assert snapshot.is_eligible("anthropic", now=now_before) is False

    # Exactly at boundary: eligible again!
    assert entry.is_exhausted(reset_time) is False
    assert snapshot.is_eligible("anthropic", "5h", now=reset_time) is True
    assert snapshot.is_eligible("anthropic", now=reset_time) is True

    # 1 second after boundary: eligible
    now_after = reset_time + timedelta(seconds=1)
    assert entry.is_exhausted(now_after) is False
    assert snapshot.is_eligible("anthropic", "5h", now=now_after) is True
    assert snapshot.is_eligible("anthropic", now=now_after) is True

    # provider_exhausted_until returns the latest datetime regardless of past/future
    exhausted_dt = snapshot.provider_exhausted_until("anthropic")
    assert exhausted_dt == reset_time


# ---------------------------------------------------------------------------
# TEST 3: Parsing 429 bodies updates snapshot across all format variants
# ---------------------------------------------------------------------------
def test_429_body_updates_snapshot_all_formats(tmp_path: Path):
    """apply_quota_error parses 429 bodies and writes entry, reporting provider ineligible."""
    snap_path = tmp_path / "quota-snapshot.json"
    now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Absolute quotaResetTimeStamp body
    body_abs = '{"quotaResetTimeStamp": "2026-09-25T16:00:00Z"}'
    reset_abs = apply_quota_error(
        "google-antigravity",
        "daily",
        body_abs,
        path=snap_path,
        now=now,
    )
    assert reset_abs is not None
    assert reset_abs.exhausted_until == "2026-09-25T16:00:00Z"
    assert reset_abs.retry_after_seconds == 14400.0  # 4 hours
    snap1 = load_snapshot(snap_path)
    assert snap1.is_eligible("google-antigravity", now=now) is False
    assert snap1.is_eligible("google-antigravity", "daily", now=now) is False

    # 2. Google RetryInfo body with retryDelay: "3s"
    body_google = json.dumps(
        {
            "error": {
                "code": 429,
                "message": "Resource exhausted",
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "3s",
                    }
                ],
            }
        }
    )
    reset_google = apply_quota_error(
        "google-gemini",
        "daily",
        body_google,
        path=snap_path,
        now=now,
    )
    assert reset_google is not None
    assert reset_google.retry_after_seconds == 3.0
    assert reset_google.exhausted_until == "2026-09-25T12:00:03Z"
    snap2 = load_snapshot(snap_path)
    assert snap2.is_eligible("google-gemini", now=now) is False
    # After 4s it becomes eligible
    assert snap2.is_eligible("google-gemini", now=now + timedelta(seconds=4)) is True

    # 3. Anthropic error.message "resets at <iso>Z" body
    body_anthropic = json.dumps(
        {
            "error": {
                "type": "rate_limit_error",
                "message": "Your organization's quota has exceeded its limit. resets at 2026-09-25T18:30:00Z",
            }
        }
    )
    reset_anthropic = apply_quota_error(
        "anthropic",
        "5h",
        body_anthropic,
        path=snap_path,
        now=now,
    )
    assert reset_anthropic is not None
    assert reset_anthropic.exhausted_until == "2026-09-25T18:30:00Z"
    snap3 = load_snapshot(snap_path)
    assert snap3.is_eligible("anthropic", now=now) is False

    # Also verify Anthropic message with trailing "+00:00" converts to "Z"
    body_anthropic_tz = json.dumps(
        {
            "error": {
                "type": "rate_limit_error",
                "message": "Rate limit exceeded; resets at 2026-09-25T19:00:00+00:00",
            }
        }
    )
    reset_tz = parse_quota_error(body_anthropic_tz, now=now)
    assert reset_tz is not None
    assert reset_tz.exhausted_until == "2026-09-25T19:00:00Z"

    # 4. retry_after seconds body
    body_retry_after = '{"retry_after": 60}'
    reset_retry = apply_quota_error(
        "openai-codex",
        "7d",
        body_retry_after,
        path=snap_path,
        now=now,
    )
    assert reset_retry is not None
    assert reset_retry.retry_after_seconds == 60.0
    assert reset_retry.exhausted_until == "2026-09-25T12:01:00Z"
    snap4 = load_snapshot(snap_path)
    assert snap4.is_eligible("openai-codex", now=now) is False
    assert snap4.is_eligible("openai-codex", now=now + timedelta(seconds=61)) is True


# ---------------------------------------------------------------------------
# TEST 4: refresh_from_usage behavior (fresh, stale, forced, and runner error)
# ---------------------------------------------------------------------------
def test_refresh_from_usage_fresh_stale_force_and_runner_failure(tmp_path: Path):
    """refresh_from_usage obeys max_age_hours, respects force, and survives runner exceptions."""
    snap_path = tmp_path / "quota-snapshot.json"
    t0 = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)

    # Create initial snapshot with updated_at = 10:00:00Z
    init_snap = QuotaSnapshot(
        schema_version=1,
        updated_at="2026-09-25T10:00:00Z",
        entries={
            "anthropic|5h": QuotaWindowEntry(
                provider="anthropic",
                window_id="5h",
                used_fraction=0.2,
                fetched_at="2026-09-25T10:00:00Z",
            )
        },
    )
    save_snapshot(init_snap, snap_path)

    runner = MagicMock()
    runner.return_value = json.dumps(
        {
            "generatedAt": 1788598659263,
            "reports": [
                {
                    "provider": "anthropic",
                    "fetchedAt": 1788598659263,
                    "limits": [
                        {
                            "id": "anthropic:5h",
                            "window": {"id": "5h", "durationMs": 18000000},
                            "amount": {"usedFraction": 0.5, "remainingFraction": 0.5},
                            "status": "ok",
                        }
                    ],
                }
            ],
        }
    )

    # 1. Fresh snapshot (1 hour old, max_age_hours=3.0) -> Runner NOT called
    t_fresh = t0 + timedelta(hours=1)
    res_fresh = refresh_from_usage(
        max_age_hours=3.0,
        force=False,
        path=snap_path,
        now=t_fresh,
        runner=runner,
    )
    assert runner.call_count == 0
    assert res_fresh.entries["anthropic|5h"].used_fraction == 0.2

    # 2. Stale snapshot (4 hours old, max_age_hours=3.0) -> Runner IS called
    t_stale = t0 + timedelta(hours=4)
    res_stale = refresh_from_usage(
        max_age_hours=3.0,
        force=False,
        path=snap_path,
        now=t_stale,
        runner=runner,
    )
    assert runner.call_count == 1
    assert res_stale.entries["anthropic|5h"].used_fraction == 0.5
    assert res_stale.updated_at == "2026-09-25T14:00:00Z"

    # 3. Fresh snapshot but force=True -> Runner IS called
    runner.reset_mock()
    t_force = t_stale + timedelta(minutes=5)
    refresh_from_usage(
        max_age_hours=3.0,
        force=True,
        path=snap_path,
        now=t_force,
        runner=runner,
    )
    assert runner.call_count == 1

    # 4. Runner raises exception -> survives cleanly, returns existing snapshot intact
    failing_runner = MagicMock(side_effect=RuntimeError("Subprocess failed with exit code 1"))
    res_fail = refresh_from_usage(
        force=True,
        path=snap_path,
        now=t_force,
        runner=failing_runner,
    )
    assert failing_runner.call_count == 1
    assert isinstance(res_fail, QuotaSnapshot)
    assert "anthropic|5h" in res_fail.entries


# ---------------------------------------------------------------------------
# TEST 5: Malformed JSON and missing file load as empty snapshot
# ---------------------------------------------------------------------------
def test_malformed_and_missing_file_load_empty(tmp_path: Path):
    """Missing file, empty file, non-dict, and corrupt JSON all load as empty QuotaSnapshot."""
    # 1. Missing file
    missing_path = tmp_path / "missing-snapshot.json"
    snap_missing = load_snapshot(missing_path)
    assert isinstance(snap_missing, QuotaSnapshot)
    assert len(snap_missing.entries) == 0

    # 2. Empty file
    empty_path = tmp_path / "empty.json"
    empty_path.write_text("", encoding="utf-8")
    snap_empty = load_snapshot(empty_path)
    assert isinstance(snap_empty, QuotaSnapshot)
    assert len(snap_empty.entries) == 0

    # 3. Corrupted / malformed JSON
    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("{ broken json: [1, 2,", encoding="utf-8")
    snap_corrupt = load_snapshot(corrupt_path)
    assert isinstance(snap_corrupt, QuotaSnapshot)
    assert len(snap_corrupt.entries) == 0

    # 4. Non-dict JSON
    list_path = tmp_path / "list.json"
    list_path.write_text("[1, 2, 3]", encoding="utf-8")
    snap_list = load_snapshot(list_path)
    assert isinstance(snap_list, QuotaSnapshot)
    assert len(snap_list.entries) == 0


# ---------------------------------------------------------------------------
# TEST 6: to_dict / from_dict round-trip and atomic write leaving no temp file
# ---------------------------------------------------------------------------
def test_round_trip_and_atomic_write_no_temp_files(tmp_path: Path):
    """to_dict/from_dict round-trips correctly and atomic save leaves no temp file behind."""
    entry = QuotaWindowEntry(
        provider="google-antigravity:anthropic",
        window_id="daily",
        used_fraction=1.0,
        exhausted_until="2026-09-25T20:38:56Z",
        fetched_at="2026-09-25T18:40:00Z",
        source="429",
    )
    snapshot = QuotaSnapshot(
        schema_version=1,
        updated_at="2026-09-25T18:40:00Z",
        entries={"google-antigravity:anthropic|daily": entry},
    )

    data = snapshot.to_dict()

    # Verify exact schema matching specification
    assert data["schema_version"] == 1
    assert data["updated_at"] == "2026-09-25T18:40:00Z"
    assert "google-antigravity:anthropic|daily" in data["entries"]
    entry_dict = data["entries"]["google-antigravity:anthropic|daily"]
    assert entry_dict["provider"] == "google-antigravity:anthropic"
    assert entry_dict["window_id"] == "daily"
    assert entry_dict["used_fraction"] == 1.0
    assert entry_dict["exhausted_until"] == "2026-09-25T20:38:56Z"
    assert entry_dict["fetched_at"] == "2026-09-25T18:40:00Z"
    assert entry_dict["source"] == "429"

    # Add unknown keys to payload and ensure from_dict ignores them
    data["unknown_top_level"] = "foo"
    entry_dict["unknown_nested_key"] = "bar"

    restored = QuotaSnapshot.from_dict(data)
    assert restored.schema_version == 1
    assert restored.updated_at == "2026-09-25T18:40:00Z"
    assert len(restored.entries) == 1
    assert "google-antigravity:anthropic|daily" in restored.entries
    r_entry = restored.entries["google-antigravity:anthropic|daily"]
    assert r_entry.provider == "google-antigravity:anthropic"
    assert r_entry.window_id == "daily"
    assert r_entry.used_fraction == 1.0
    assert r_entry.exhausted_until == "2026-09-25T20:38:56Z"

    # Atomic write test in a subfolder
    target_path = tmp_path / "deep" / "nested" / "quota-snapshot.json"
    written_path = save_snapshot(snapshot, target_path)
    assert written_path == target_path
    assert target_path.is_file()

    # Ensure no temporary file (.quota_tmp_*) is left in directory
    files_in_dir = list(target_path.parent.iterdir())
    assert len(files_in_dir) == 1
    assert files_in_dir[0].name == "quota-snapshot.json"


# ---------------------------------------------------------------------------
# TEST 7: update_from_usage_json preserves future reset & extracts used_fraction
# ---------------------------------------------------------------------------
def test_update_from_usage_json_behavior(tmp_path: Path):
    """update_from_usage_json records usage and preserves future 429 reset timestamps."""
    snap_path = tmp_path / "quota-snapshot.json"
    now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
    future_reset = "2026-09-25T15:00:00Z"

    # Pre-seed snapshot with a 429 future exhausted_until
    mark_exhausted(
        "google-antigravity:anthropic",
        "daily",
        future_reset,
        path=snap_path,
        now=now,
    )

    usage_payload = {
        "generatedAt": 1788598659263,
        "reports": [
            {
                "provider": "google-antigravity",
                "fetchedAt": 1788598659263,
                "limits": [
                    {
                        "id": "google-antigravity:anthropic:pro:daily",
                        "label": "Daily",
                        "window": {
                            "id": "daily",
                            "durationMs": 86400000,
                            "resetsAt": 1788609459263,  # timestamp in payload
                        },
                        "amount": {
                            "usedFraction": 0.85,
                            "remainingFraction": 0.15,
                        },
                        "status": "ok",
                    }
                ],
            },
            {
                "provider": "anthropic",
                "fetchedAt": 1788598659263,
                "limits": [
                    {
                        "id": "anthropic:5h",
                        "label": "5 Hour",
                        "window": {"id": "5h", "durationMs": 18000000, "resetsAt": 1788616659263},
                        "amount": {"remainingFraction": 0.3},  # usedFraction missing, tests fallback
                        "status": "ok",
                    }
                ],
            },
        ],
    }

    updated = update_from_usage_json(usage_payload, path=snap_path, now=now)

    # Antigravity daily window: should preserve future reset from 429
    ag_entry = updated.entry("google-antigravity:anthropic", "daily")
    assert ag_entry is not None
    assert ag_entry.used_fraction == 0.85
    assert ag_entry.exhausted_until == future_reset
    assert ag_entry.source == "429"

    # Anthropic 5h window: used_fraction should be 1 - 0.3 = 0.70
    anth_entry = updated.entry("anthropic", "5h")
    assert anth_entry is not None
    assert pytest.approx(anth_entry.used_fraction, 0.01) == 0.70
    # Status was ok, so exhausted_until is None
    assert anth_entry.exhausted_until is None


# ---------------------------------------------------------------------------
# TEST 8: parse_quota_error duration formats & non-quota bodies
# ---------------------------------------------------------------------------
def test_parse_quota_error_durations_and_non_quota():
    """parse_quota_error correctly parses varied relative units and rejects non-quota errors."""
    now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)

    # Varied units
    r_ms = parse_quota_error('{"retryDelay": "250ms"}', now=now)
    assert r_ms is not None
    assert pytest.approx(r_ms.retry_after_seconds, 0.001) == 0.25

    r_m = parse_quota_error('{"quotaResetDelay": "2m"}', now=now)
    assert r_m is not None
    assert r_m.retry_after_seconds == 120.0

    r_h = parse_quota_error('{"retry_after": "1.5h"}', now=now)
    assert r_h is not None
    assert r_h.retry_after_seconds == 5400.0

    # Plain text format
    r_text = parse_quota_error("Rate limit reached. retry_after: 30", now=now)
    assert r_text is not None
    assert r_text.retry_after_seconds == 30.0

    # Non-quota error body without reset info should return None
    assert parse_quota_error("Internal Server Error 500", now=now) is None
    assert parse_quota_error('{"error": {"code": 404, "message": "Not Found"}}', now=now) is None
    assert parse_quota_error('{"error": "invalid_request"}', now=now) is None
    assert parse_quota_error("", now=now) is None
    assert parse_quota_error(None, now=now) is None  # type: ignore
