#!/usr/bin/env python3
"""
quota_snapshot.py - Local quota snapshot cache for model routing.

Maintains a local quota snapshot (~/.veyyon/run/quota-snapshot.json) holding
per-provider and per-window usage fractions and exhausted_until timestamps (UTC).
The snapshot is refreshed from `veyyon usage --json` at most every few hours
(or when stale / forced), and updated immediately from 429 or quota error bodies.
The model router consults ONLY the snapshot, avoiding expensive live CLI calls
before every dispatch.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

# Ensure sibling modules in workflows/portable are importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from balance_loader import parse_usage_json
except ImportError:
    from .balance_loader import parse_usage_json

DEFAULT_SNAPSHOT_PATH = Path(
    os.environ.get(
        "VEYYON_QUOTA_SNAPSHOT",
        os.path.expanduser("~/.veyyon/run/quota-snapshot.json"),
    )
)
SNAPSHOT_PATH = DEFAULT_SNAPSHOT_PATH


def _resolve_snapshot_path(path: Optional[Union[Path, str]] = None) -> Path:
    """Resolve the effective snapshot path, checking VEYYON_QUOTA_SNAPSHOT env var."""
    if path is not None and path != SNAPSHOT_PATH:
        return Path(path)
    env_override = os.environ.get("VEYYON_QUOTA_SNAPSHOT")
    if env_override:
        return Path(env_override)
    return Path(path) if path is not None else SNAPSHOT_PATH


def _parse_iso_utc(ts_str: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp into a timezone-aware datetime."""
    if not ts_str:
        return None
    s = ts_str.strip()
    if not s:
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _format_iso_utc(dt: datetime) -> str:
    """Format a datetime as an ISO-8601 UTC string ending in 'Z'."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_duration_seconds(val: Any) -> Optional[float]:
    """Parse relative duration values (e.g. '3s', '1.5s', '250ms', '2m', '1h', bare numbers)."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        secs = float(val.get("seconds", 0))
        nanos = float(val.get("nanos", 0))
        return secs + (nanos / 1e9)
    if isinstance(val, str):
        s = val.strip().lower()
        if not s:
            return None
        if s.endswith("ms"):
            try:
                return float(s[:-2]) / 1000.0
            except ValueError:
                return None
        if s.endswith("s"):
            try:
                return float(s[:-1])
            except ValueError:
                return None
        if s.endswith("m"):
            try:
                return float(s[:-1]) * 60.0
            except ValueError:
                return None
        if s.endswith("h"):
            try:
                return float(s[:-1]) * 3600.0
            except ValueError:
                return None
        if s.endswith("d"):
            try:
                return float(s[:-1]) * 86400.0
            except ValueError:
                return None
        try:
            return float(s)
        except ValueError:
            return None
    return None


@dataclass
class QuotaWindowEntry:
    provider: str
    window_id: str
    used_fraction: float = 0.0
    exhausted_until: Optional[str] = None  # ISO-8601 UTC, e.g. "2026-09-25T20:38:56Z"
    fetched_at: str = ""
    source: str = "usage"  # "usage" | "429"

    def is_exhausted(self, now: Optional[datetime] = None) -> bool:
        """Check if this entry has an exhausted_until timestamp strictly in the future."""
        if not self.exhausted_until:
            return False
        parsed_dt = _parse_iso_utc(self.exhausted_until)
        if parsed_dt is None:
            return False
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        return parsed_dt > now_dt


@dataclass
class QuotaSnapshot:
    schema_version: int = 1
    updated_at: str = ""
    entries: Dict[str, QuotaWindowEntry] = field(default_factory=dict)  # key f"{provider}|{window_id}"

    def entry(self, provider: str, window_id: Optional[str] = None) -> Optional[QuotaWindowEntry]:
        """Look up an entry by provider and optional window_id."""
        if window_id is not None:
            key = f"{provider}|{window_id}"
            if key in self.entries:
                return self.entries[key]
            # Handle potential window_id prefix/suffix variance (e.g. daily vs provider:pro:daily)
            if ":" in window_id:
                short_win = window_id.split(":")[-1]
                short_key = f"{provider}|{short_win}"
                if short_key in self.entries:
                    return self.entries[short_key]
            for e in self.entries.values():
                if e.provider == provider and (e.window_id == window_id or e.window_id == window_id.split(":")[-1]):
                    return e
            return None

        # When window_id is omitted, find all entries for provider
        matching = [e for e in self.entries.values() if e.provider == provider]
        if not matching:
            return None
        # If any window is exhausted, prefer the exhausted entry with latest reset
        exhausted = [e for e in matching if e.is_exhausted()]
        if exhausted:
            return max(
                exhausted,
                key=lambda e: _parse_iso_utc(e.exhausted_until) or datetime.min.replace(tzinfo=timezone.utc),
            )
        return matching[0]

    def provider_exhausted_until(self, provider: str, now: Optional[datetime] = None) -> Optional[datetime]:
        """Return the latest future-or-past exhausted_until datetime for provider, or None."""
        matching = [e for e in self.entries.values() if e.provider == provider]
        datetimes = []
        for e in matching:
            if e.exhausted_until:
                dt = _parse_iso_utc(e.exhausted_until)
                if dt is not None:
                    datetimes.append(dt)
        return max(datetimes) if datetimes else None

    def is_eligible(
        self,
        provider: str,
        window_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """Check whether provider is eligible to serve requests.

        Returns False when ANY entry for that provider has exhausted_until strictly in the future.
        When window_id is given, only that window is consulted.
        """
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        if window_id is not None:
            ent = self.entry(provider, window_id)
            if ent is None:
                return True
            return not ent.is_exhausted(now_dt)

        matching = [e for e in self.entries.values() if e.provider == provider]
        for e in matching:
            if e.is_exhausted(now_dt):
                return False
        return True

    def to_dict(self) -> dict:
        """Serialize snapshot to dictionary."""
        return {
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
            "entries": {
                k: asdict(v) if is_dataclass(v) else dict(v)
                for k, v in self.entries.items()
            },
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "QuotaSnapshot":
        """Deserialize snapshot from dictionary, ignoring unknown keys."""
        if not isinstance(payload, dict):
            return cls()
        schema_version = int(payload.get("schema_version", 1))
        updated_at = str(payload.get("updated_at", ""))
        raw_entries = payload.get("entries", {})
        entries: Dict[str, QuotaWindowEntry] = {}
        if isinstance(raw_entries, dict):
            for k, v in raw_entries.items():
                if isinstance(v, dict):
                    provider = str(v.get("provider", ""))
                    window_id = str(v.get("window_id", ""))
                    used_fraction = float(v.get("used_fraction", 0.0))
                    exhausted_until = v.get("exhausted_until")
                    if exhausted_until is not None:
                        exhausted_until = str(exhausted_until)
                    fetched_at = str(v.get("fetched_at", ""))
                    source = str(v.get("source", "usage"))
                    entries[k] = QuotaWindowEntry(
                        provider=provider,
                        window_id=window_id,
                        used_fraction=used_fraction,
                        exhausted_until=exhausted_until,
                        fetched_at=fetched_at,
                        source=source,
                    )
        return cls(
            schema_version=schema_version,
            updated_at=updated_at,
            entries=entries,
        )


def load_snapshot(path: Union[Path, str] = SNAPSHOT_PATH) -> QuotaSnapshot:
    """Load snapshot from disk. Missing or corrupt file returns empty snapshot without raising."""
    resolved = _resolve_snapshot_path(path)
    if not resolved.is_file():
        return QuotaSnapshot()
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            return QuotaSnapshot()
        payload = json.loads(content)
        if not isinstance(payload, dict):
            return QuotaSnapshot()
        return QuotaSnapshot.from_dict(payload)
    except Exception:
        return QuotaSnapshot()


def save_snapshot(snapshot: QuotaSnapshot, path: Union[Path, str] = SNAPSHOT_PATH) -> Path:
    """Atomically save snapshot to disk via temp file + os.replace."""
    resolved = _resolve_snapshot_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)

    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=resolved.parent,
            delete=False,
            mode="w",
            encoding="utf-8",
            prefix=".quota_tmp_",
        ) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(snapshot.to_dict(), tmp, indent=2)
        os.replace(tmp_path, resolved)
        tmp_path = None
        return resolved
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def mark_exhausted(
    provider: str,
    window_id: str,
    exhausted_until: str,
    *,
    used_fraction: float = 1.0,
    source: str = "429",
    path: Union[Path, str] = SNAPSHOT_PATH,
    now: Optional[datetime] = None,
) -> QuotaSnapshot:
    """Record a provider window as exhausted until the given ISO-8601 UTC timestamp."""
    snapshot = load_snapshot(path)
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_iso = _format_iso_utc(now_dt)

    parsed_until = _parse_iso_utc(exhausted_until)
    normalized_until = _format_iso_utc(parsed_until) if parsed_until else exhausted_until

    key = f"{provider}|{window_id}"
    entry = QuotaWindowEntry(
        provider=provider,
        window_id=window_id,
        used_fraction=float(used_fraction),
        exhausted_until=normalized_until,
        fetched_at=now_iso,
        source=source,
    )
    snapshot.entries[key] = entry
    snapshot.updated_at = now_iso
    save_snapshot(snapshot, path)
    return snapshot


@dataclass
class QuotaReset:
    exhausted_until: str
    retry_after_seconds: Optional[float]
    raw: str


def parse_quota_error(body: str, now: Optional[datetime] = None) -> Optional[QuotaReset]:
    """Parse 429 / quota error body to extract reset timestamp or relative retry delay.

    Prefers in order:
      1. Absolute reset timestamp: keys quotaResetTimeStamp, quota_reset_time_stamp,
         quotaResetTimestamp, resets_at, resetsAt, reset_at, exhausted_until,
         or any ISO-8601 UTC instant in error.message (e.g. "resets at 2026-09-25T20:38:56Z").
      2. Relative delay: keys quotaResetDelay, retryDelay, retry_after, retryAfter,
         retry_delay_seconds (accepts "3s", "1.5s", "250ms", "2m", "1h", bare numbers,
         and Google RetryInfo in error.details[]).
      3. Returns None when no usable reset info is present.
    """
    if not body or not isinstance(body, str):
        return None

    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    # 1. Absolute reset timestamp
    parsed_json: Optional[Any] = None
    try:
        parsed_json = json.loads(body)
    except Exception:
        parsed_json = None

    abs_keys = {
        "quotaresettimestamp",
        "quota_reset_time_stamp",
        "resets_at",
        "resetsat",
        "reset_at",
        "exhausted_until",
    }

    def _extract_abs_ts_from_json(obj: Any) -> Optional[str]:
        if isinstance(obj, dict):
            # Direct key check
            for k, v in obj.items():
                if k.lower() in abs_keys and isinstance(v, (str, int, float)):
                    if isinstance(v, (int, float)):
                        if v > 1e11:  # ms
                            return _format_iso_utc(datetime.fromtimestamp(v / 1000.0, tz=timezone.utc))
                        elif v > 1e9:  # s
                            return _format_iso_utc(datetime.fromtimestamp(v, tz=timezone.utc))
                    return str(v)
            # Message / error fields for ISO instant
            for k, v in obj.items():
                if ("message" in k.lower() or "error" in k.lower() or "detail" in k.lower()) and isinstance(v, str):
                    m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00))", v, re.IGNORECASE)
                    if m:
                        return m.group(1)
            # Recurse
            for v in obj.values():
                res = _extract_abs_ts_from_json(v)
                if res:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = _extract_abs_ts_from_json(item)
                if res:
                    return res
        return None

    abs_ts_str: Optional[str] = None
    if parsed_json is not None:
        abs_ts_str = _extract_abs_ts_from_json(parsed_json)

    if not abs_ts_str:
        # Search plain string for reset patterns
        patterns = [
            r"(?:quota[_-]?reset[_-]?(?:time[_-]?stamp)?|resets?(?:[_-]at|\s+at)?|exhausted[_-]until)[\"':\s=]+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00))",
            r"resets?\s+at\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00))",
            r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00))\b",
        ]
        is_quota_related = bool(re.search(r"quota|rate[_-]?limit|exceeded|exhausted|resets?|429", body, re.IGNORECASE))
        active_patterns = patterns if is_quota_related else patterns[:2]
        for p in active_patterns:
            m = re.search(p, body, re.IGNORECASE)
            if m:
                abs_ts_str = m.group(1)
                break

    if abs_ts_str:
        dt = _parse_iso_utc(abs_ts_str)
        if dt is not None:
            delay_sec = max(0.0, (dt - now_dt).total_seconds())
            iso_utc = _format_iso_utc(dt)
            return QuotaReset(
                exhausted_until=iso_utc,
                retry_after_seconds=delay_sec,
                raw=body,
            )

    # 2. Relative delay
    rel_keys = {
        "quotaresetdelay",
        "quota_reset_delay",
        "retrydelay",
        "retry_delay",
        "retry_after",
        "retryafter",
        "retry_delay_seconds",
    }

    def _extract_rel_delay_from_json(obj: Any) -> Optional[float]:
        if isinstance(obj, dict):
            type_val = str(obj.get("@type", ""))
            if "RetryInfo" in type_val or "retry_info" in type_val.lower():
                dur = _parse_duration_seconds(obj.get("retryDelay"))
                if dur is not None:
                    return dur
            for k, v in obj.items():
                if k.lower() in rel_keys:
                    dur = _parse_duration_seconds(v)
                    if dur is not None:
                        return dur
            for v in obj.values():
                res = _extract_rel_delay_from_json(v)
                if res is not None:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = _extract_rel_delay_from_json(item)
                if res is not None:
                    return res
        return None

    delay_sec: Optional[float] = None
    if parsed_json is not None:
        delay_sec = _extract_rel_delay_from_json(parsed_json)

    if delay_sec is None:
        m = re.search(
            r"(?:quota[_-]?reset[_-]?delay|retry[_-]?delay(?:[_-]seconds)?|retry[_-]?after)[\"':\s=]+([0-9]+(?:\.[0-9]+)?(?:ms|s|m|h)?)",
            body,
            re.IGNORECASE,
        )
        if m:
            delay_sec = _parse_duration_seconds(m.group(1))

    if delay_sec is not None:
        delay_sec = max(0.0, float(delay_sec))
        exhausted_dt = now_dt + timedelta(seconds=delay_sec)
        iso_utc = _format_iso_utc(exhausted_dt)
        return QuotaReset(
            exhausted_until=iso_utc,
            retry_after_seconds=delay_sec,
            raw=body,
        )

    # 3. No usable reset info
    return None


def apply_quota_error(
    provider: str,
    window_id: str,
    body: str,
    *,
    path: Union[Path, str] = SNAPSHOT_PATH,
    now: Optional[datetime] = None,
) -> Optional[QuotaReset]:
    """Parse 429 / quota error body, mark snapshot window exhausted, and return QuotaReset."""
    reset = parse_quota_error(body, now=now)
    if reset is None:
        return None
    mark_exhausted(
        provider=provider,
        window_id=window_id,
        exhausted_until=reset.exhausted_until,
        used_fraction=1.0,
        source="429",
        path=path,
        now=now,
    )
    return reset


def update_from_usage_json(
    payload: Union[dict, str],
    *,
    path: Union[Path, str] = SNAPSHOT_PATH,
    now: Optional[datetime] = None,
) -> QuotaSnapshot:
    """Update snapshot from `veyyon usage --json` payload (dict or JSON string)."""
    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)
    now_iso = _format_iso_utc(now_dt)

    snapshot = load_snapshot(path)

    current_time_ms = int(now_dt.timestamp() * 1000)
    if hasattr(payload, "subscriptions"):
        sanitized = payload
        raw_dict = {}
    else:
        try:
            sanitized = parse_usage_json(payload, current_time_ms=current_time_ms)
        except Exception:
            return snapshot
        raw_dict = json.loads(payload) if isinstance(payload, str) else (payload if isinstance(payload, dict) else {})
    raw_window_map: Dict[str, str] = {}
    raw_amount_map: Dict[str, dict] = {}
    if isinstance(raw_dict, dict):
        for rep in raw_dict.get("reports", []):
            if isinstance(rep, dict):
                for lim in rep.get("limits", []):
                    if isinstance(lim, dict):
                        lid = lim.get("id")
                        wid = lim.get("window", {}).get("id") if isinstance(lim.get("window"), dict) else None
                        if lid and wid:
                            raw_window_map[lid] = wid
                        if lid and isinstance(lim.get("amount"), dict):
                            raw_amount_map[lid] = lim["amount"]
    for sub in sanitized.subscriptions:
        provider = sub.provider
        fetched_at = sub.fetched_at_utc or now_iso

        for lim in sub.limits:
            lid = lim.id
            wid = raw_window_map.get(lid)
            if not wid:
                wid = lid.split(":")[-1] if ":" in lid else lid
            if not wid:
                wid = "default"
            if provider == "opencode-go":
                from balance_loader import identify_opencode_go_window
                wid, _ = identify_opencode_go_window(lid, getattr(lim, "label", ""), getattr(lim, "duration_ms", 0), wid)
            # Determine used_fraction: prefer usedFraction, fallback to 1 - remainingFraction
            used_frac: Optional[float] = None
            raw_amt = raw_amount_map.get(lid, {})
            if provider == "opencode-go" and hasattr(lim, "amount") and lim.amount is not None:
                used_frac = float(lim.amount.used_fraction)
            elif "usedFraction" in raw_amt:
                used_frac = float(raw_amt["usedFraction"])
            elif "remainingFraction" in raw_amt:
                used_frac = float(1.0 - float(raw_amt["remainingFraction"]))
            elif hasattr(lim, "amount") and lim.amount is not None:
                if getattr(lim.amount, "used_fraction", None) is not None and lim.amount.used_fraction > 0.0:
                    used_frac = float(lim.amount.used_fraction)
                elif getattr(lim.amount, "remaining_fraction", None) is not None:
                    used_frac = float(1.0 - lim.amount.remaining_fraction)
                elif getattr(lim.amount, "used_fraction", None) is not None:
                    used_frac = float(lim.amount.used_fraction)
            if used_frac is None:
                used_frac = 0.0
            used_frac = max(0.0, min(1.0, used_frac))
            key = f"{provider}|{wid}"
            existing = snapshot.entries.get(key)
            if existing and existing.source == "429" and existing.is_exhausted(now_dt):
                exhausted_until = existing.exhausted_until
                source = existing.source
            else:
                lim_status = getattr(lim, "status", "ok")
                is_limited = lim_status in ("limit_reached", "rate_limited", "cooldown", "not_allowed") or used_frac >= 1.0
                resets_utc = getattr(lim, "resets_at_utc", "")
                resets_ms = getattr(lim, "resets_at_ms", 0)
                if is_limited and resets_utc and resets_ms > 0:
                    exhausted_until = resets_utc
                    source = "usage"
                else:
                    exhausted_until = None
                    source = "usage"

            entry = QuotaWindowEntry(
                provider=provider,
                window_id=wid,
                used_fraction=used_frac,
                exhausted_until=exhausted_until,
                fetched_at=fetched_at,
                source=source,
            )
            snapshot.entries[key] = entry

    snapshot.updated_at = now_iso
    save_snapshot(snapshot, path)
    return snapshot


def refresh_from_usage(
    *,
    max_age_hours: float = 3.0,
    force: bool = False,
    path: Union[Path, str] = SNAPSHOT_PATH,
    now: Optional[datetime] = None,
    runner: Optional[Callable[[], str]] = None,
) -> QuotaSnapshot:
    """Refresh snapshot from `veyyon usage --json` when stale or forced.

    Calls runner or CLI only when force=True, file is missing, or updated_at
    is older than max_age_hours. Never raises on CLI or runner failures.
    """
    resolved = _resolve_snapshot_path(path)
    file_exists = resolved.is_file()
    snapshot = load_snapshot(resolved)

    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

    should_refresh = False
    if force or not file_exists:
        should_refresh = True
    else:
        updated_dt = _parse_iso_utc(snapshot.updated_at)
        if updated_dt is None:
            should_refresh = True
        else:
            age_sec = (now_dt - updated_dt).total_seconds()
            if age_sec > max_age_hours * 3600.0:
                should_refresh = True

    if not should_refresh:
        return snapshot

    # Perform refresh
    try:
        if runner is not None:
            output = runner()
        else:
            proc = subprocess.run(
                ["veyyon", "usage", "--json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return snapshot
            output = proc.stdout

        if not output or not output.strip():
            return snapshot
        return update_from_usage_json(output, path=resolved, now=now_dt)
    except Exception:
        # A failing or empty CLI read leaves existing snapshot intact and does not raise
        return snapshot
