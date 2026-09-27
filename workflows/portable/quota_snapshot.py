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

import email.utils
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import fcntl
except ImportError:
    fcntl = None

_THREAD_LOCK = threading.RLock()

# Ensure sibling modules in workflows/portable are importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from balance_loader import parse_usage_json, sanitize_string
except ImportError:
    from .balance_loader import parse_usage_json, sanitize_string

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


class QuotaSnapshotLoadError(Exception):
    """Base error for snapshot load failures."""
    pass


class QuotaSnapshotCorruptError(QuotaSnapshotLoadError):
    """Snapshot file on disk is corrupt or invalid JSON."""
    pass


class QuotaSnapshotLockError(QuotaSnapshotLoadError):
    """Snapshot lock could not be acquired or file is locked."""
    pass


@contextmanager
def snapshot_file_lock(path: Union[Path, str] = SNAPSHOT_PATH, timeout: float = 15.0):
    """Cross-process and intra-process lock around snapshot read-modify-write."""
    resolved = _resolve_snapshot_path(path)
    lock_path = Path(str(resolved) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with _THREAD_LOCK:
        lock_file = None
        start = time.time()
        acquired = False
        while not acquired:
            try:
                lock_file = open(lock_path, "a+b")
                if msvcrt is not None:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                elif fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                else:
                    acquired = True
            except (OSError, PermissionError):
                if lock_file is not None:
                    try:
                        lock_file.close()
                    except Exception:
                        pass
                    lock_file = None
                if time.time() - start >= timeout:
                    raise QuotaSnapshotLockError(
                        f"Could not acquire cross-process lock on {lock_path} within {timeout}s"
                    )
                time.sleep(0.02)
        try:
            yield
        finally:
            if lock_file is not None:
                try:
                    if msvcrt is not None:
                        lock_file.seek(0)
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    elif fcntl is not None:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    lock_file.close()
                except Exception:
                    pass

def _parse_iso_utc(ts_str: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp (including offsets like +02:00) or HTTP-date."""
    if not ts_str:
        return None
    s = ts_str.strip()
    if not s:
        return None
    # Check for HTTP-date format (e.g. 'Wed, 21 Oct 2026 07:28:00 GMT')
    if "," in s:
        try:
            dt = email.utils.parsedate_to_datetime(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    clean_s = s
    if clean_s.endswith("Z") or clean_s.endswith("z"):
        clean_s = clean_s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(clean_s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        try:
            dt = email.utils.parsedate_to_datetime(s)
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
    """Parse relative duration values, including compound durations (e.g. '1h30m', '250ms', '2m', '1h', bare numbers)."""
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
        matches = list(re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*(d|days?|h|hours?|hrs?|ms|millis?|milliseconds?|m|mins?|minutes?|s|secs?|seconds?)", s))
        if matches:
            total = 0.0
            for m in matches:
                amt = float(m.group(1))
                unit = m.group(2)
                if unit.startswith("d"):
                    total += amt * 86400.0
                elif unit.startswith("h"):
                    total += amt * 3600.0
                elif unit.startswith("ms"):
                    total += amt / 1000.0
                elif unit.startswith("m"):
                    total += amt * 60.0
                elif unit.startswith("s"):
                    total += amt
            return total
        try:
            return float(s)
        except ValueError:
            return None
    return None


@dataclass
class QuotaWindowEntry:
    provider: str
    window_id: str
    account: str = "default"
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
    entries: Dict[str, QuotaWindowEntry] = field(default_factory=dict)  # key f"{provider}|{account}|{window_id}"
    load_error: Optional[str] = None
    path: Optional[Path] = field(default=None, repr=False)

    def entry(
        self,
        provider: str,
        window_id: Optional[str] = None,
        account: Optional[str] = None,
    ) -> Optional[QuotaWindowEntry]:
        """Look up an entry by provider and optional window_id and account."""
        if account is not None and window_id is not None:
            key = f"{provider}|{account}|{window_id}"
            if key in self.entries:
                return self.entries[key]
            short_win = window_id.split(":")[-1] if ":" in window_id else window_id
            short_key = f"{provider}|{account}|{short_win}"
            if short_key in self.entries:
                return self.entries[short_key]

        matching = [e for e in self.entries.values() if e.provider == provider]
        if account is not None:
            matching = [e for e in matching if e.account == account]
        if window_id is not None:
            short_win = window_id.split(":")[-1] if ":" in window_id else window_id
            matching = [e for e in matching if e.window_id == window_id or e.window_id == short_win]

        if not matching:
            return None

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

        Order-independent multi-account logic (F3):
        A provider is eligible if ANY account for that provider is eligible.
        An account is eligible if none of its windows are currently exhausted.
        When window_id is given, only that window is consulted for each account.
        If no entries exist for the provider, returns True.
        """
        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        matching = [e for e in self.entries.values() if e.provider == provider]
        if not matching:
            return True

        by_account: Dict[str, List[QuotaWindowEntry]] = {}
        for e in matching:
            by_account.setdefault(e.account, []).append(e)

        for acc, entries in by_account.items():
            if window_id is not None:
                short_wid = window_id.split(":")[-1] if ":" in window_id else window_id
                acc_win_entries = [e for e in entries if e.window_id == window_id or e.window_id == short_wid]
                if not acc_win_entries:
                    return True
                if not any(e.is_exhausted(now_dt) for e in acc_win_entries):
                    return True
            else:
                if not any(e.is_exhausted(now_dt) for e in entries):
                    return True

        return False

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
                    account = str(v.get("account", "default"))
                    if k.count("|") >= 2:
                        parts = k.split("|", 2)
                        provider, account, window_id = parts[0], parts[1], parts[2]
                    used_fraction = float(v.get("used_fraction", 0.0))
                    exhausted_until = v.get("exhausted_until")
                    if exhausted_until is not None:
                        exhausted_until = str(exhausted_until)
                    fetched_at = str(v.get("fetched_at", ""))
                    source = str(v.get("source", "usage"))
                    entries[k] = QuotaWindowEntry(
                        provider=provider,
                        window_id=window_id,
                        account=account,
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


def load_snapshot(
    path: Union[Path, str] = SNAPSHOT_PATH,
    *,
    raise_on_error: bool = False,
) -> QuotaSnapshot:
    """Load snapshot from disk.

    Distinguishes a missing file (returns clean empty snapshot) from a corrupt
    or locked file. If raise_on_error is True, raises QuotaSnapshotCorruptError or
    QuotaSnapshotLockError on failures. If False, returns QuotaSnapshot with
    load_error populated so callers can detect read failures and avoid saving over.
    """
    resolved = _resolve_snapshot_path(path)
    if not resolved.is_file():
        return QuotaSnapshot(path=resolved)

    content: Optional[str] = None
    read_err: Optional[Exception] = None
    for attempt in range(10):
        try:
            with open(resolved, "r", encoding="utf-8") as f:
                content = f.read()
            read_err = None
            break
        except (PermissionError, OSError) as e:
            read_err = e
            time.sleep(0.02)

    if read_err is not None:
        if raise_on_error:
            raise QuotaSnapshotLockError(f"Failed to read snapshot file {resolved}: {read_err}") from read_err
        return QuotaSnapshot(load_error=f"locked: {read_err}", path=resolved)

    if not content or not content.strip():
        return QuotaSnapshot(path=resolved)

    try:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError(f"Snapshot payload is not a JSON object: {type(payload)}")
        snap = QuotaSnapshot.from_dict(payload)
        snap.path = resolved
        return snap
    except Exception as e:
        if raise_on_error:
            raise QuotaSnapshotCorruptError(f"Snapshot file {resolved} is corrupt: {e}") from e
        return QuotaSnapshot(load_error=f"corrupt: {e}", path=resolved)


def save_snapshot(snapshot: QuotaSnapshot, path: Union[Path, str] = SNAPSHOT_PATH) -> Path:
    """Atomically save snapshot to disk via temp file + os.replace, retrying on PermissionError."""
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

        max_retries = 25
        backoff = 0.02
        for attempt in range(max_retries):
            try:
                os.replace(tmp_path, resolved)
                tmp_path = None
                return resolved
            except PermissionError:
                if attempt == max_retries - 1:
                    raise
                time.sleep(backoff)
                backoff = min(0.2, backoff * 1.5)
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
    account: str = "default",
    used_fraction: float = 1.0,
    source: str = "429",
    path: Union[Path, str] = SNAPSHOT_PATH,
    now: Optional[datetime] = None,
) -> QuotaSnapshot:
    """Record a provider window as exhausted until the given ISO-8601 UTC timestamp."""
    with snapshot_file_lock(path):
        snapshot = load_snapshot(path, raise_on_error=True)
        if snapshot.load_error:
            raise QuotaSnapshotLoadError(f"Cannot mark exhausted: snapshot read failed: {snapshot.load_error}")

        now_dt = now or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        now_iso = _format_iso_utc(now_dt)

        parsed_until = _parse_iso_utc(exhausted_until)
        normalized_until = _format_iso_utc(parsed_until) if parsed_until else exhausted_until

        key = f"{provider}|{account}|{window_id}" if account and account != "default" else f"{provider}|{window_id}"
        entry = QuotaWindowEntry(
            provider=provider,
            window_id=window_id,
            account=account,
            used_fraction=float(used_fraction),
            exhausted_until=normalized_until,
            fetched_at=now_iso,
            source=source,
        )

        matching_keys = [
            k for k, e in snapshot.entries.items()
            if e.provider == provider and (e.window_id == window_id or e.window_id == window_id.split(":")[-1])
        ]
        if account != "default" or not matching_keys:
            snapshot.entries[key] = entry
        else:
            for k in matching_keys:
                snapshot.entries[k].exhausted_until = normalized_until
                snapshot.entries[k].source = source
                snapshot.entries[k].used_fraction = float(used_fraction)
                snapshot.entries[k].fetched_at = now_iso

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
      1. Absolute reset timestamp from explicitly reset-named keys or phrases:
         keys quotaresettimestamp, quota_reset_time_stamp, resets_at, resetsat, reset_at,
         reset_time, exhausted_until, or retry_after (if HTTP-date). Past timestamps rejected.
      2. Relative delay: keys quotaresetdelay, retrydelay, retry_delay, retry_after,
         retryafter, retry_delay_seconds (accepts compound durations like '1h30m', '250ms',
         '2m', '1h', bare numbers, and Google RetryInfo in error.details[]).
      3. Returns None when no usable reset info is present.
    """
    if not body or not isinstance(body, str):
        return None

    now_dt = now or datetime.now(timezone.utc)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

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
        "reset_time",
        "exhausted_until",
    }
    rel_keys = {
        "quotaresetdelay",
        "quota_reset_delay",
        "retrydelay",
        "retry_delay",
        "retry_after",
        "retryafter",
        "retry_delay_seconds",
    }
    rel_ms_keys = {
        "retry_after_ms",
        "retry-after-ms",
        "retryafterms",
        "quotaresetdelayms",
        "quota_reset_delay_ms",
    }

    # 1. Absolute reset timestamp from json
    def _extract_abs_ts_from_json(obj: Any) -> Optional[datetime]:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() in abs_keys and isinstance(v, (str, int, float)):
                    if isinstance(v, (int, float)):
                        if v > 1e11:  # ms
                            return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)
                        elif v > 1e9:  # s
                            return datetime.fromtimestamp(v, tz=timezone.utc)
                    return _parse_iso_utc(str(v))
                if k.lower() in ("retry_after", "retryafter") and isinstance(v, str) and "," in v:
                    return _parse_iso_utc(v)
            for v in obj.values():
                res = _extract_abs_ts_from_json(v)
                if res is not None:
                    return res
        elif isinstance(obj, list):
            for item in obj:
                res = _extract_abs_ts_from_json(item)
                if res is not None:
                    return res
        return None

    abs_dt = _extract_abs_ts_from_json(parsed_json) if parsed_json is not None else None
    if abs_dt is None:
        m_abs = re.search(
            r"(?:quota[_-]?reset[_-]?(?:time[_-]?stamp)?|resets?(?:[_-]at|\s+at)|exhausted[_-]until)[\"':\s=]+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2}))",
            body,
            re.IGNORECASE,
        )
        if m_abs:
            abs_dt = _parse_iso_utc(m_abs.group(1))

    if abs_dt is None:
        m_http = re.search(
            r"(?:retry[_-]?after|resets?\s+at)[\"':\s=]+([A-Za-z]{3},\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{2}:\d{2}:\d{2}\s+GMT)",
            body,
            re.IGNORECASE,
        )
        if m_http:
            abs_dt = _parse_iso_utc(m_http.group(1))

    if abs_dt is not None and abs_dt > now_dt:
        delay_sec = max(0.0, (abs_dt - now_dt).total_seconds())
        return QuotaReset(
            exhausted_until=_format_iso_utc(abs_dt),
            retry_after_seconds=delay_sec,
            raw=body,
        )

    # 2. Relative delay from json or regex
    def _extract_rel_delay_from_json(obj: Any) -> Optional[float]:
        if isinstance(obj, dict):
            type_val = str(obj.get("@type", ""))
            if "RetryInfo" in type_val or "retry_info" in type_val.lower():
                dur = _parse_duration_seconds(obj.get("retryDelay"))
                if dur is not None:
                    return dur
            for k, v in obj.items():
                if k.lower() in rel_ms_keys and isinstance(v, (int, float, str)):
                    try:
                        return float(v) / 1000.0
                    except (ValueError, TypeError):
                        pass
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

    delay_sec = _extract_rel_delay_from_json(parsed_json) if parsed_json is not None else None
    if delay_sec is None:
        m_ms = re.search(
            r"retry[_-]?after[_-]?ms[\"':\s=]+([0-9]+(?:\.[0-9]+)?)",
            body,
            re.IGNORECASE,
        )
        if m_ms:
            try:
                delay_sec = float(m_ms.group(1)) / 1000.0
            except (ValueError, TypeError):
                pass
    if delay_sec is None:
        m_rel = re.search(
            r"(?:quota[_-]?reset[_-]?delay|retry[_-]?delay(?:[_-]seconds)?|retry[_-]?after)[\"':\s=]+([0-9]+(?:\.[0-9]+)?(?:\s*(?:d|h|m|s|ms|millis|hours?|mins?|secs?))?(?:\s*[0-9]+(?:\.[0-9]+)?\s*(?:d|h|m|s|ms|millis|hours?|mins?|secs?))*)",
            body,
            re.IGNORECASE,
        )
        if m_rel:
            delay_sec = _parse_duration_seconds(m_rel.group(1))

    if delay_sec is not None and delay_sec >= 0.0:
        exhausted_dt = now_dt + timedelta(seconds=delay_sec)
        return QuotaReset(
            exhausted_until=_format_iso_utc(exhausted_dt),
            retry_after_seconds=delay_sec,
            raw=body,
        )

    return None


def apply_quota_error(
    provider: str,
    window_id: str,
    body: str,
    *,
    account: str = "default",
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
        account=account,
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

    current_time_ms = int(now_dt.timestamp() * 1000)
    if hasattr(payload, "subscriptions"):
        sanitized = payload
        raw_dict = {}
    else:
        try:
            sanitized = parse_usage_json(payload, current_time_ms=current_time_ms)
        except Exception:
            return load_snapshot(path)
        raw_dict = json.loads(payload) if isinstance(payload, str) else (payload if isinstance(payload, dict) else {})

    with snapshot_file_lock(path):
        snapshot = load_snapshot(path, raise_on_error=True)
        if snapshot.load_error:
            raise QuotaSnapshotLoadError(f"Cannot update from usage: snapshot read failed: {snapshot.load_error}")

        raw_window_map: Dict[Tuple[str, str], str] = {}
        raw_amount_map: Dict[Tuple[str, str], dict] = {}
        if isinstance(raw_dict, dict):
            for rep in raw_dict.get("reports", []):
                if isinstance(rep, dict):
                    rep_meta = rep.get("metadata", {}) or {}
                    rep_acc = (
                        sanitize_string(rep_meta.get("accountId"))
                        or sanitize_string(rep_meta.get("email"))
                        or "default"
                    )
                    for lim in rep.get("limits", []):
                        if isinstance(lim, dict):
                            lid = lim.get("id")
                            wid = lim.get("window", {}).get("id") if isinstance(lim.get("window"), dict) else None
                            if lid:
                                if wid:
                                    raw_window_map[(rep_acc, lid)] = wid
                                    raw_window_map[("default", lid)] = wid
                                if isinstance(lim.get("amount"), dict):
                                    raw_amount_map[(rep_acc, lid)] = lim["amount"]
                                    raw_amount_map[("default", lid)] = lim["amount"]

        for sub in sanitized.subscriptions:
            provider = sub.provider
            account = getattr(sub, "account_id_redacted", None) or getattr(sub, "email_redacted", None) or "default"
            fetched_at = sub.fetched_at_utc or now_iso

            for lim in sub.limits:
                lid = lim.id
                wid = raw_window_map.get((account, lid)) or raw_window_map.get(("default", lid))
                if not wid:
                    wid = lid.split(":")[-1] if ":" in lid else lid
                if not wid:
                    wid = "default"
                if provider == "opencode-go":
                    from balance_loader import identify_opencode_go_window
                    wid, _ = identify_opencode_go_window(lid, getattr(lim, "label", ""), getattr(lim, "duration_ms", 0), wid)

                # Determine used_fraction
                used_frac: Optional[float] = None
                raw_amt = raw_amount_map.get((account, lid)) or raw_amount_map.get(("default", lid), {})
                if hasattr(lim, "amount") and lim.amount is not None:
                    if getattr(lim.amount, "used_fraction", None) is not None and lim.amount.used_fraction > 0.0:
                        used_frac = float(lim.amount.used_fraction)
                    elif getattr(lim.amount, "remaining_fraction", None) is not None:
                        used_frac = float(1.0 - lim.amount.remaining_fraction)
                    elif getattr(lim.amount, "used_fraction", None) is not None:
                        used_frac = float(lim.amount.used_fraction)
                if used_frac is None:
                    if "usedFraction" in raw_amt:
                        used_frac = float(raw_amt["usedFraction"])
                    elif "remainingFraction" in raw_amt:
                        used_frac = float(1.0 - float(raw_amt["remainingFraction"]))
                if used_frac is None:
                    used_frac = 0.0
                used_frac = max(0.0, min(1.0, used_frac))
                if account and account != "default":
                    key = f"{provider}|{account}|{wid}"
                else:
                    key = f"{provider}|{wid}"
                existing = snapshot.entries.get(key)
                if not existing and key != f"{provider}|{wid}":
                    existing = snapshot.entries.get(f"{provider}|{wid}")

                if existing and existing.source == "429" and existing.is_exhausted(now_dt):
                    exhausted_until = existing.exhausted_until
                    source = existing.source
                else:
                    lim_status = getattr(lim, "status", "ok")
                    is_limited = lim_status in ("limit_reached", "rate_limited", "cooldown", "not_allowed") or used_frac >= 1.0
                    resets_utc = getattr(lim, "resets_at_utc", "")
                    resets_ms = getattr(lim, "resets_at_ms", 0)
                    if is_limited:
                        if resets_utc and resets_ms > 0:
                            exhausted_until = resets_utc
                            source = "usage"
                        else:
                            # F6: limit_reached without reset time -> conservative default window
                            dur_ms = getattr(lim, "duration_ms", 0)
                            window_delta = timedelta(milliseconds=dur_ms) if dur_ms and dur_ms > 0 else timedelta(hours=5)
                            exhausted_until = _format_iso_utc(now_dt + window_delta)
                            source = "usage"
                    else:
                        exhausted_until = None
                        source = "usage"

                entry = QuotaWindowEntry(
                    provider=provider,
                    window_id=wid,
                    account=account,
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
