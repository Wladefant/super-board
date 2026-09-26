#!/usr/bin/env python3
"""Portable independent Telegram session crash monitor and external supervisor.

Supervises a target Veyyon session instance outside Main lifetime, binding
sessionUUID + verified PID + process creation time to detect unexpected termination
while handling PID reuse, distinguishing planned stops/restarts, and avoiding false
positives from quiet/slow model turns or paused goals.

Alerts are deduplicated per owner termination and delivered through the verified
Telegram route without raw tool spam, link dumps, or secret leakage.

With `--auto-resume` the monitor also relaunches the dead session exactly once, as
`veyyon --resume <session>` typed back into the herdr pane it died in, but only after
the terminal registry proves nothing else still owns that session. Every death writes
an interrupted-lane manifest naming the lanes the dead process took with it, built from
the `Session exit recorded` facts it left in the log and from any in-flight tool call
markers it left on disk.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import datetime
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Try local imports first, then package-level imports
try:
    from telegram_notifier import (
        DEFAULT_POOL_DB_PATH,
        DeliveryReceipt,
        NotificationEvent,
        SecretSanitizer,
        TelegramNotificationAdapter,
    )
    from ledger import FileLock
except ImportError:
    try:
        from workflows.portable.telegram_notifier import (
            DEFAULT_POOL_DB_PATH,
            DeliveryReceipt,
            NotificationEvent,
            SecretSanitizer,
            TelegramNotificationAdapter,
        )
        from workflows.portable.ledger import FileLock
    except ImportError:
        from workflows.telegram_notifier import (
            DEFAULT_POOL_DB_PATH,
            DeliveryReceipt,
            NotificationEvent,
            SecretSanitizer,
            TelegramNotificationAdapter,
        )
        from workflows.ledger import FileLock


DEFAULT_SESSION_ID = "01a0496f-64f6-733e-a9a6-89f15fc2a437"
DEFAULT_STATE_FILE = Path.home() / ".veyyon" / "workflows" / "crash_monitor_state.json"
DEFAULT_PROJECT = "polysimulator"
DEFAULT_CANONICAL_LINK = "https://github.com/orgs/Bavariance/projects/1"

DEFAULT_PROFILE_RUN_DIR = Path.home() / ".veyyon" / "profiles" / "default" / "run"
DEFAULT_TERMINAL_REGISTRY_DIR = DEFAULT_PROFILE_RUN_DIR / "terminals"
DEFAULT_LOG_DIR = Path.home() / ".veyyon" / "profiles" / "default" / "logs"
DEFAULT_INFLIGHT_DIR = DEFAULT_LOG_DIR / "inflight"
DEFAULT_MANIFEST_DIR = Path.home() / ".veyyon" / "workflows"
# A registry file is written as its process starts, so a live process the OS
# reports as started later than its own registry file is a reused pid, not the owner.
REGISTRY_INCARNATION_SLACK_SECONDS = 2.0
# How many times to look for the target's herdr pane while it is still alive.
PANE_LOOKUP_ATTEMPTS = 5


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass(frozen=True)
class SessionProcessInfo:
    """Snapshot of target operating system process state."""

    session_id: str
    pid: int
    creation_time_utc: str
    is_alive: bool
    exit_code: Optional[int] = None
    command_line: Optional[str] = None
    executable: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ProcessProbe:
    """Operating system probe for process liveness and creation time binding."""

    def __init__(self, session_registry: Optional["SessionRegistry"] = None):
        # The terminal registry is the only ownership signal on a platform whose
        # command line does not name the resumed session.
        self.session_registry = session_registry

    def get_process_info(
        self, pid: int, expected_creation_time: Optional[str] = None
    ) -> Optional[SessionProcessInfo]:
        """Probes process by PID. Returns None if PID does not exist."""
        if sys.platform == "win32":
            return self._get_windows_process_info(pid, expected_creation_time)
        return self._get_posix_process_info(pid, expected_creation_time)

    def _get_windows_process_info(
        self, pid: int, expected_creation_time: Optional[str] = None
    ) -> Optional[SessionProcessInfo]:
        import ctypes
        import ctypes.wintypes as w

        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000

        k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        k32.OpenProcess.restype = w.HANDLE
        k32.GetProcessTimes.argtypes = [
            w.HANDLE,
            ctypes.POINTER(w.FILETIME),
            ctypes.POINTER(w.FILETIME),
            ctypes.POINTER(w.FILETIME),
            ctypes.POINTER(w.FILETIME),
        ]
        k32.GetProcessTimes.restype = w.BOOL
        k32.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        k32.GetExitCodeProcess.restype = w.BOOL
        k32.CloseHandle.argtypes = [w.HANDLE]
        k32.CloseHandle.restype = w.BOOL

        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not h:
            # Fallback with just QUERY_LIMITED_INFORMATION
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            return None

        try:
            ct = w.FILETIME()
            et = w.FILETIME()
            kt = w.FILETIME()
            ut = w.FILETIME()
            ok = k32.GetProcessTimes(
                h,
                ctypes.byref(ct),
                ctypes.byref(et),
                ctypes.byref(kt),
                ctypes.byref(ut),
            )
            if not ok:
                return None

            ft = ct.dwLowDateTime + (ct.dwHighDateTime << 32)
            # Windows FILETIME represents 100-nanosecond intervals since Jan 1, 1601 UTC
            us = (ft - 116444736000000000) // 10
            creation_dt = datetime.datetime(
                1970, 1, 1, tzinfo=datetime.timezone.utc
            ) + datetime.timedelta(microseconds=us)
            creation_str = creation_dt.isoformat()

            # Check PID reuse: if an expected creation time was supplied and differs,
            # this PID now belongs to an unrelated new process.
            if expected_creation_time and creation_str != expected_creation_time:
                return SessionProcessInfo(
                    session_id="",
                    pid=pid,
                    creation_time_utc=creation_str,
                    is_alive=False,  # Original process is dead
                    exit_code=None,
                )

            # Query exit code
            code = w.DWORD()
            k32.GetExitCodeProcess(h, ctypes.byref(code))
            STILL_ACTIVE = 259
            is_alive = code.value == STILL_ACTIVE
            exit_code = None if is_alive else code.value

            return SessionProcessInfo(
                session_id="",
                pid=pid,
                creation_time_utc=creation_str,
                is_alive=is_alive,
                exit_code=exit_code,
            )
        finally:
            k32.CloseHandle(h)

    def _get_posix_process_info(
        self, pid: int, expected_creation_time: Optional[str] = None
    ) -> Optional[SessionProcessInfo]:
        if pid <= 0:
            return None
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, OSError):
            return None

        creation_str = ""
        stat_path = Path(f"/proc/{pid}/stat")
        if stat_path.exists():
            try:
                parts = stat_path.read_text().split()
                if len(parts) > 21:
                    creation_str = f"start_{parts[21]}"
            except Exception:
                pass
        if not creation_str:
            creation_str = "posix_process"

        if expected_creation_time and creation_str != expected_creation_time:
            return SessionProcessInfo(
                session_id="",
                pid=pid,
                creation_time_utc=creation_str,
                is_alive=False,
                exit_code=None,
            )

        return SessionProcessInfo(
            session_id="",
            pid=pid,
            creation_time_utc=creation_str,
            is_alive=True,
            exit_code=None,
        )

    def verify_session_binding(
        self, session_id: str, pid: int
    ) -> Tuple[bool, Optional[str], Optional[SessionProcessInfo]]:
        """Verifies process exists, gets its creation time, and binds it to the session.

        The command line is checked first because it is the strongest local proof
        when it is present. A windowed Veyyon launched without `--resume` does not
        name its session there, so a live terminal-registry entry naming both this
        pid and this session is accepted as the equivalent proof.
        """
        info = self.get_process_info(pid)
        if not info or not info.is_alive:
            return False, "Process not alive or does not exist", None

        command_line = self.get_command_line(pid)
        if command_line and session_id not in command_line:
            if not self.registry_claims_pid(session_id, pid):
                return (
                    False,
                    f"Process PID {pid} neither references session {session_id} "
                    "on its command line nor owns it in the terminal registry",
                    info,
                )

        bound_info = SessionProcessInfo(
            session_id=session_id,
            pid=pid,
            creation_time_utc=info.creation_time_utc,
            is_alive=info.is_alive,
            exit_code=info.exit_code,
            command_line=command_line,
        )
        return True, None, bound_info

    def registry_claims_pid(self, session_id: str, pid: int) -> bool:
        """Whether a live-session registry entry names this pid for this session."""
        registry = getattr(self, "session_registry", None)
        if registry is None:
            return False
        return any(record.pid == pid for record in registry.records_for(session_id))

    def get_command_line(self, pid: int) -> Optional[str]:
        if sys.platform == "win32":
            try:
                cmd = [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f'(Get-CimInstance Win32_Process -Filter "ProcessId={pid}").CommandLine',
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if res.returncode == 0:
                    return res.stdout.strip()
            except Exception:
                pass
        else:
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            if cmdline_path.exists():
                try:
                    return cmdline_path.read_text().replace("\0", " ").strip()
                except Exception:
                    pass
        return None

    def get_image_name(self, pid: int) -> Optional[str]:
        """Executable image name for a pid, or None when it cannot be read."""
        if sys.platform == "win32":
            import ctypes
            import ctypes.wintypes as w

            k32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
            k32.OpenProcess.restype = w.HANDLE
            k32.QueryFullProcessImageNameW.argtypes = [
                w.HANDLE,
                w.DWORD,
                w.LPWSTR,
                ctypes.POINTER(w.DWORD),
            ]
            k32.QueryFullProcessImageNameW.restype = w.BOOL
            k32.CloseHandle.argtypes = [w.HANDLE]
            k32.CloseHandle.restype = w.BOOL
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return None
            try:
                size = w.DWORD(32768)
                buf = ctypes.create_unicode_buffer(size.value)
                if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return None
                return Path(buf.value).name
            finally:
                k32.CloseHandle(h)
        try:
            return Path(os.readlink(f"/proc/{pid}/exe")).name
        except OSError:
            return None

    def get_process_ancestors(self, pid: int, limit: int = 16) -> List[int]:
        """The pid itself, then its ancestors nearest-first, at most `limit` of them."""
        if sys.platform == "win32":
            script = (
                f"$cur = {int(pid)}; $out = @(); "
                f"for ($i = 0; $i -lt {int(limit)} -and $cur -gt 0; $i++) {{ "
                "$out += $cur; "
                '$p = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" '
                "-ErrorAction SilentlyContinue; "
                "if (-not $p) { break }; $cur = [int]$p.ParentProcessId }; "
                "$out -join ','"
            )
            try:
                res = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", script],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except Exception:
                return [int(pid)]
            if res.returncode != 0:
                return [int(pid)]
            chain = [
                int(part)
                for part in res.stdout.strip().split(",")
                if part.strip().isdigit()
            ]
            return chain or [int(pid)]

        chain = [int(pid)]
        seen = {int(pid)}
        current = int(pid)
        for _ in range(max(0, limit - 1)):
            current = self._posix_parent_pid(current)
            if current <= 0 or current in seen:
                break
            chain.append(current)
            seen.add(current)
        return chain

    @staticmethod
    def _posix_parent_pid(pid: int) -> int:
        try:
            raw = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return 0
        # The comm field may contain spaces and parentheses, so ppid is read
        # from after the LAST ')' in the record.
        tail = raw.rpartition(")")[2].split()
        return int(tail[1]) if len(tail) > 1 else 0

    def is_veyyon_process(self, pid: int) -> bool:
        """Whether the live process at `pid` is a Veyyon executable image."""
        img = (self.get_image_name(pid) or "").lower()
        return img == "veyyon" or img.startswith("veyyon.exe")

    def discover_session_pid(self, session_id: str) -> Optional[int]:
        """Discovers a running process holding this session: registry first.

        The registry is authoritative because a windowed Veyyon launched without
        `--resume` never carries the session id on its command line.
        """
        registry = getattr(self, "session_registry", None)
        if registry is not None:
            owner = registry.live_owner(session_id, self)
            if owner is not None:
                return owner.pid
        if sys.platform == "win32":
            try:
                ps_session_id = session_id.replace("'", "''")
                cmd = [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process | "
                    f"Where-Object {{ ($_.Name -like 'veyyon.exe*' -or $_.Name -eq 'veyyon') -and $_.CommandLine -like '*{ps_session_id}*' }} | "
                    "Select-Object -ExpandProperty ProcessId",
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if res.returncode == 0 and res.stdout.strip():
                    lines = [ln.strip() for ln in res.stdout.strip().splitlines() if ln.strip()]
                    if lines:
                        return int(lines[0])
            except Exception:
                pass
        return None


def parse_iso_utc(ts: str) -> Optional[datetime.datetime]:
    """Parses ISO8601 string to timezone-aware UTC datetime."""
    if not ts or not isinstance(ts, str):
        return None
    try:
        clean = ts.strip()
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None


class PlannedStopEvaluator:
    """Evaluates planned stop / restart markers to suppress crash alerts on intentional shutdowns.

    Invariants:
    1. Marker must explicitly declare an owner (e.g. RuntimeCrashCutover, operator).
    2. Marker must match target session_id.
    3. Status 'complete' is an old consumed marker and NEVER suppresses future crashes.
    4. Marker must declare valid unexpired timestamp (expires_utc or created_utc within TTL).
    """

    DEFAULT_MARKER_MAX_AGE_SECONDS = 900.0  # 15 minutes default TTL

    @classmethod
    def get_default_marker_paths(cls, session_id: str) -> List[Path]:
        paths: List[Path] = []
        # Primary session local marker
        session_root = Path.home() / ".veyyon" / "profiles" / "default" / "agent" / "sessions"
        if session_root.exists():
            for p in session_root.glob(f"*{session_id}*"):
                if p.is_dir():
                    paths.append(p / "local" / "planned-restart-marker.json")
            for p in session_root.glob(f"*/*{session_id}*"):
                if p.is_dir():
                    paths.append(p / "local" / "planned-restart-marker.json")

        # Fallback workflow marker
        workflows_dir = Path.home() / ".veyyon" / "workflows"
        paths.append(workflows_dir / f"planned_restart_{session_id}.json")
        return paths

    @classmethod
    def check_planned_stop(
        cls,
        session_id: str,
        pid: int,
        marker_paths: Optional[Sequence[Path]] = None,
        max_age_seconds: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Checks if an active, unexpired planned stop marker exists and matches target session and PID."""
        candidates = list(marker_paths or [])
        candidates.extend(cls.get_default_marker_paths(session_id))

        # Strictly active intent statuses only; 'complete' is excluded so old markers never suppress future crashes
        valid_statuses = {"planned", "suppress", "expected", "restarting"}
        ttl = max_age_seconds if max_age_seconds is not None else cls.DEFAULT_MARKER_MAX_AGE_SECONDS
        now = datetime.datetime.now(datetime.timezone.utc)

        for p in candidates:
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    continue

                # 1. Verify owner binding (must not be anonymous/empty)
                owner = str(data.get("owner", "")).strip()
                if not owner:
                    continue

                # 2. Verify session binding
                marker_session = data.get("session_id")
                if not marker_session or marker_session != session_id:
                    continue

                # 3. Verify PID binding if specified
                marker_pid = data.get("target_pid")
                if marker_pid is not None and int(marker_pid) != pid:
                    continue

                # 4. Verify status (old 'complete' markers are strictly rejected)
                status = str(data.get("status", "")).lower().strip()
                if status not in valid_statuses:
                    continue

                # 5. Verify expiry / TTL binding
                if "expires_utc" in data:
                    exp_dt = parse_iso_utc(str(data["expires_utc"]))
                    if not exp_dt or now > exp_dt:
                        continue  # Expired marker
                elif "created_utc" in data:
                    crt_dt = parse_iso_utc(str(data["created_utc"]))
                    if not crt_dt or (now - crt_dt).total_seconds() > ttl:
                        continue  # Expired marker
                else:
                    # Neither expires_utc nor created_utc provided: reject (must bind expiry/time)
                    continue

                return {
                    "matched_marker_path": str(p),
                    "reason": data.get("reason", "planned_restart"),
                    "owner": owner,
                    "status": status,
                    "created_utc": data.get("created_utc", utc_now_iso()),
                }
            except Exception:
                continue

        return None

class CrashMonitorStateLedger:
    """Durable disk-backed state ledger for crash monitor observations and deduplication."""

    def __init__(self, state_file: Path):
        self.state_file = Path(state_file).resolve()
        self.lock_path = self.state_file.with_suffix(".json.lock")

    def _load_locked(self) -> Dict[str, Any]:
        if not self.state_file.exists():
            return {
                "schema": "veyyon/crash-monitor-state/v1",
                "updated_utc": utc_now_iso(),
                "recorded_terminations": {},
                "unsent_events": [],
                "active_supervisors": {},
            }
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("recorded_terminations", {})
                    data.setdefault("unsent_events", [])
                    data.setdefault("active_supervisors", {})
                    data.setdefault("resume_claims", {})
                    return data
        except Exception:
            return {
                "schema": "veyyon/crash-monitor-state/v1",
                "updated_utc": utc_now_iso(),
                "recorded_terminations": {},
                "unsent_events": [],
                "active_supervisors": {},
                "resume_claims": {},
                "corrupt": True,
            }
        return {
            "schema": "veyyon/crash-monitor-state/v1",
            "updated_utc": utc_now_iso(),
            "recorded_terminations": {},
            "unsent_events": [],
            "active_supervisors": {},
            "resume_claims": {},
            "corrupt": True,
        }

    def _save_locked(self, data: Dict[str, Any]) -> None:
        if data.get("corrupt"):
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data["updated_utc"] = utc_now_iso()
        temp_file = self.state_file.with_suffix(".tmp")
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(temp_file, self.state_file)

    def make_target_key(self, session_id: str, pid: int, creation_time_utc: str) -> str:
        return f"{session_id}:{pid}:{creation_time_utc}"

    def has_processed(self, session_id: str, pid: int, creation_time_utc: str) -> bool:
        key = self.make_target_key(session_id, pid, creation_time_utc)
        lock = FileLock(str(self.lock_path))
        with lock:
            data = self._load_locked()
            return key in data.get("recorded_terminations", {})

    def get_record(
        self, session_id: str, pid: int, creation_time_utc: str
    ) -> Optional[Dict[str, Any]]:
        key = self.make_target_key(session_id, pid, creation_time_utc)
        lock = FileLock(str(self.lock_path))
        with lock:
            data = self._load_locked()
            return data.get("recorded_terminations", {}).get(key)

    def record_termination(
        self,
        session_id: str,
        pid: int,
        creation_time_utc: str,
        record: Dict[str, Any],
    ) -> None:
        key = self.make_target_key(session_id, pid, creation_time_utc)
        lock = FileLock(str(self.lock_path))
        with lock:
            data = self._load_locked()
            data["recorded_terminations"][key] = record
            self._save_locked(data)

    def add_unsent_event(self, event_record: Dict[str, Any]) -> None:
        lock = FileLock(str(self.lock_path))
        with lock:
            data = self._load_locked()
            data["unsent_events"].append(event_record)
            self._save_locked(data)

    def register_supervisor(
        self,
        session_id: str,
        pid: int,
        creation_time_utc: str,
        supervisor_info: Dict[str, Any],
    ) -> None:
        key = self.make_target_key(session_id, pid, creation_time_utc)
        lock = FileLock(str(self.lock_path))
        with lock:
            data = self._load_locked()
            data["active_supervisors"][key] = supervisor_info
            self._save_locked(data)

    def claim_termination(
        self,
        session_id: str,
        pid: int,
        creation_time_utc: str,
        claim: Dict[str, Any],
    ) -> bool:
        """Become the one handler of this death. False when it is already handled.

        The check and the write share the ledger lock, so two monitors watching the
        same process cannot both decide to relaunch it.
        """
        key = self.make_target_key(session_id, pid, creation_time_utc)
        lock = FileLock(str(self.lock_path))
        with lock:
            data = self._load_locked()
            if data.get("corrupt"):
                return False
            claims = data.setdefault("resume_claims", {})
            if key in claims:
                return False
            claims[key] = claim
            self._save_locked(data)
            return True

    def is_corrupt(self) -> bool:
        """Check if the durable state file exists and is corrupt/unparseable."""
        if not self.state_file.exists():
            return False
        lock = FileLock(str(self.lock_path))
        with lock:
            data = self._load_locked()
            return bool(data.get("corrupt"))


def format_crash_alert_text(
    session_id: str,
    pid: int,
    observed_reason: str,
    utc_timestamp: str,
    recovery_pointer: str,
    is_test: bool = False,
    extra_details: Optional[str] = None,
) -> str:
    """Formats a concise plain-language crash alert without raw tool spam or leaked secrets."""
    if is_test or "TEST" in session_id:
        header = "🧪 TEST / PROOF: Disposable Process Crash Detection Verification"
        note = "Controlled verification: disposable process unexpected termination detected through monitor. Real Main session is untouched. Please ignore."
    else:
        header = "⚠️ PolySimulator Session Crash Alert"
        note = "Automated session monitor detected unexpected termination."

    lines = [
        header,
        note,
        f"Session ID: {session_id}",
        f"Observed PID: {pid}",
        f"Timestamp (UTC): {utc_timestamp}",
        f"Reason: {observed_reason}",
        f"Recovery: {recovery_pointer}",
    ]
    if extra_details:
        lines.append(f"Details: {extra_details}")

    raw_text = "\n".join(lines)
    # Sanitize any unexpected paths or tokens
    return SecretSanitizer.sanitize(raw_text)


@dataclass(frozen=True)
class SessionOwnerRecord:
    """A Veyyon process that claimed the target session when it started.

    Written by the process itself into the profile's `run/terminals` directory. The
    file outlives its process, so liveness is always re-proved against the operating
    system before this record is believed.
    """

    session_id: str
    pid: int
    cwd: Optional[str]
    session_file: Optional[str]
    registry_file: str
    registry_mtime_utc: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SessionRegistry:
    """Reader for the per-profile live-session registry."""

    def __init__(self, registry_dir: Optional[Path] = None):
        self.registry_dir = Path(registry_dir or DEFAULT_TERMINAL_REGISTRY_DIR)

    def records_for(self, session_id: str) -> List[SessionOwnerRecord]:
        records: List[SessionOwnerRecord] = []
        try:
            files = sorted(self.registry_dir.glob("*.json"))
        except OSError:
            return records
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict) or data.get("sessionId") != session_id:
                continue
            try:
                pid = int(data.get("pid"))
            except (TypeError, ValueError):
                continue
            try:
                mtime = datetime.datetime.fromtimestamp(
                    path.stat().st_mtime, tz=datetime.timezone.utc
                ).isoformat()
            except OSError:
                mtime = None
            records.append(
                SessionOwnerRecord(
                    session_id=session_id,
                    pid=pid,
                    cwd=data.get("cwd"),
                    session_file=data.get("sessionFile"),
                    registry_file=str(path),
                    registry_mtime_utc=mtime,
                )
            )
        return records

    def live_owner(
        self,
        session_id: str,
        probe: ProcessProbe,
        exclude_pid: Optional[int] = None,
    ) -> Optional[SessionOwnerRecord]:
        """The registry record that still owns `session_id`, if one exists.

        Fails closed. A record counts as an owner unless the operating system proves
        the pid is gone, the image is not Veyyon, or the process started after the
        record was written (a pid the OS has since handed out again). An incarnation
        that cannot be disproved is treated as live, because relaunching on top of a
        running Main is worse than declining to relaunch.
        """
        for record in self.records_for(session_id):
            if exclude_pid is not None and record.pid == exclude_pid:
                continue
            info = probe.get_process_info(record.pid)
            if not info or not info.is_alive:
                continue
            if not probe.is_veyyon_process(record.pid):
                continue
            if not self._incarnation_holds(record, info):
                continue
            return record
        return None

    @staticmethod
    def _incarnation_holds(record: SessionOwnerRecord, info: SessionProcessInfo) -> bool:
        started = parse_iso_utc(info.creation_time_utc or "")
        recorded = parse_iso_utc(record.registry_mtime_utc or "")
        if not started or not recorded:
            return True
        return started <= recorded + datetime.timedelta(
            seconds=REGISTRY_INCARNATION_SLACK_SECONDS
        )


class HerdrPaneError(RuntimeError):
    """The herdr CLI could not answer a pane request."""


class HerdrPanes:
    """herdr CLI client: find the pane a process runs in, then run a command there.

    `herdr pane run <pane> <command>` types the command into the pane's own shell and
    presses Enter, so a relaunched Main lands where the dead one was launched, with
    that pane's environment and working directory intact.
    """

    def __init__(self, herdr_bin: str = "herdr", timeout: float = 15.0, runner=None):
        self.herdr_bin = herdr_bin
        self.timeout = timeout
        self._runner = runner or self._subprocess_runner

    @staticmethod
    def _subprocess_runner(argv: Sequence[str], timeout: float):
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout, res.stderr

    def _result(self, argv: Sequence[str]) -> Dict[str, Any]:
        label = " ".join(argv[:3])
        try:
            code, out, err = self._runner(list(argv), self.timeout)
        except Exception as exc:
            raise HerdrPaneError(f"{label} failed: {type(exc).__name__}: {exc}") from exc
        if code != 0:
            raise HerdrPaneError(f"{label} exited {code}: {(err or out).strip()[:300]}")
        try:
            payload = json.loads(out)
        except json.JSONDecodeError as exc:
            raise HerdrPaneError(f"{label} returned non-JSON: {out.strip()[:200]}") from exc
        result = payload.get("result")
        return result if isinstance(result, dict) else {}

    def list_panes(self) -> List[Dict[str, Any]]:
        panes = self._result([self.herdr_bin, "pane", "list"]).get("panes") or []
        return [
            pane for pane in panes if isinstance(pane, dict) and pane.get("pane_id")
        ]

    def list_pane_ids(self) -> List[str]:
        return [str(pane["pane_id"]) for pane in self.list_panes()]

    def scratch_host_pane(self) -> Optional[str]:
        """A pane with no agent attached, preferred over one hosting a live session.

        A pane carrying an `agent` key is running a real Veyyon session, so splitting
        one of those would disturb live work. An agentless shell is used when present,
        and the focused pane only as a fallback.
        """
        panes = self.list_panes()
        for pane in panes:
            if not pane.get("agent"):
                return str(pane["pane_id"])
        for pane in panes:
            if pane.get("focused"):
                return str(pane["pane_id"])
        return None

    def pane_shell_pid(self, pane_id: str) -> Optional[int]:
        """The pane's own shell, which is the process group a command runs under."""
        info = self._result(
            [self.herdr_bin, "pane", "process-info", "--pane", pane_id]
        ).get("process_info") or {}
        try:
            return int(info.get("foreground_process_group_id"))
        except (TypeError, ValueError):
            return None

    def pane_for_pid(self, pid: int, probe: ProcessProbe) -> Optional[str]:
        """The pane whose shell owns `pid`, resolved through the process ancestry.

        A pane's foreground process group is the shell Veyyon was typed into, so the
        pane hosting a Veyyon process is the one whose shell is in that process's
        ancestor chain. This has to run while the target is alive: a dead pid has no
        ancestry left to walk.
        """
        try:
            pane_ids = self.list_pane_ids()
        except HerdrPaneError:
            return None
        shell_to_pane: Dict[int, str] = {}
        for pane_id in pane_ids:
            try:
                shell = self.pane_shell_pid(pane_id)
            except HerdrPaneError:
                continue
            if shell is not None:
                shell_to_pane.setdefault(shell, pane_id)
        for ancestor in probe.get_process_ancestors(pid):
            pane_id = shell_to_pane.get(ancestor)
            if pane_id:
                return pane_id
        return None

    def run_in_pane(self, pane_id: str, command: str) -> Tuple[bool, str]:
        """Type a command into a pane's own shell and press Enter.

        `herdr pane run` answers with an exit status and no payload — measured
        2026-09-26: exit 0, empty stdout and stderr, and the typed command really did
        run — so success is the exit code and stdout is deliberately not parsed.
        """
        code, out, err = self.run_raw([self.herdr_bin, "pane", "run", pane_id, command])
        if code == 0:
            return True, f"{self.herdr_bin} pane run {pane_id} {command}"
        return (
            False,
            f"{self.herdr_bin} pane run {pane_id} exited {code}: "
            f"{(err or out).strip()[:300]}",
        )


    def run_raw(self, argv: Sequence[str]) -> Tuple[int, str, str]:
        """Run a herdr command whose stdout is not a JSON contract we depend on."""
        try:
            return self._runner(list(argv), self.timeout)
        except Exception as exc:
            return 1, "", f"{type(exc).__name__}: {exc}"

    def split_pane(
        self,
        from_pane: Optional[str] = None,
        cwd: Optional[str] = None,
        direction: str = "down",
    ) -> Optional[str]:
        """Create a scratch pane and return its id.

        The split response shape is not a stable contract, so the new pane is
        identified by difference against the pane set already present.
        """
        before = set(self.list_pane_ids())
        argv = [
            self.herdr_bin,
            "pane",
            "split",
            "--direction",
            direction,
            "--no-focus",
        ]
        if from_pane:
            argv += ["--pane", from_pane]
        if cwd:
            argv += ["--cwd", cwd]
        code, out, err = self.run_raw(argv)
        if code != 0:
            raise HerdrPaneError(f"pane split exited {code}: {(err or out).strip()[:300]}")
        for _ in range(25):
            new = sorted(set(self.list_pane_ids()) - before)
            if new:
                return new[0]
            time.sleep(0.2)
        return None

    def close_pane(self, pane_id: str) -> Tuple[bool, str]:
        code, out, err = self.run_raw([self.herdr_bin, "pane", "close", pane_id])
        if code == 0:
            return True, f"closed {pane_id}"
        return False, f"pane close exited {code}: {(err or out).strip()[:200]}"


def _quote_for_shell(token: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._:@/\\=+-]+", token):
        return token
    if '"' in token:
        raise ValueError(f"cannot quote a command token containing a double quote: {token!r}")
    return f'"{token}"'


def build_resume_command(resume_argv: Sequence[str]) -> str:
    """One shell-ready command line, quoting only the tokens that need it."""
    return " ".join(_quote_for_shell(str(part)) for part in resume_argv)


def default_resume_argv(session_id: str) -> List[str]:
    """`veyyon --resume <session>`, resolved against the executable actually present."""
    executable = shutil.which("veyyon")
    if not executable:
        executable = "veyyon.exe" if sys.platform == "win32" else "veyyon"
    return [executable, "--resume", session_id]


@dataclass(frozen=True)
class SessionExitRecord:
    """One `Session exit recorded` fact a dying process wrote about a session."""

    session_id: str
    session_file: str
    reason: str
    kind: str
    pending_tool_calls: int
    timestamp_utc: str
    log_file: str

    @property
    def lane_name(self) -> str:
        return Path(self.session_file).stem if self.session_file else self.session_id

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["lane"] = self.lane_name
        return data


class SessionExitLogReader:
    """Reads the exit and rejection facts a dying process left in the day's log.

    A process that dies through JavaScript records one `Session exit recorded` line per
    session — its own and every in-process lane — immediately before it stops. Those
    lines are the interrupted-lane manifest; a process killed below JavaScript leaves
    none, and its in-flight tool call markers are the only evidence left.
    """

    def __init__(self, log_dir: Optional[Path] = None):
        self.log_dir = Path(log_dir or DEFAULT_LOG_DIR)

    def candidate_files(
        self, since: datetime.datetime, until: datetime.datetime
    ) -> List[Path]:
        """Rotated logs covering the window, padded by a day on each side."""
        if not self.log_dir.is_dir():
            return []
        files: List[Path] = []
        day = (since - datetime.timedelta(days=1)).date()
        last = (until + datetime.timedelta(days=1)).date()
        while day <= last:
            files.extend(sorted(self.log_dir.glob(f"veyyon.{day.isoformat()}.log*")))
            day += datetime.timedelta(days=1)
        return files

    @staticmethod
    def _open(path: Path):
        if path.name.endswith(".gz"):
            return gzip.open(path, "rt", encoding="utf-8", errors="replace")
        return open(path, "r", encoding="utf-8", errors="replace")

    def _scan(
        self,
        pid: int,
        since: datetime.datetime,
        until: datetime.datetime,
        parser,
    ) -> List[Any]:
        found: List[Any] = []
        for path in self.candidate_files(since, until):
            try:
                handle = self._open(path)
            except OSError:
                continue
            try:
                with handle:
                    for line in handle:
                        entry = parser(line, pid, since, until)
                        if entry is not None:
                            found.append((str(path), entry))
            except OSError:
                continue
        return found

    def read_fatal_exits(
        self,
        pid: int,
        since: datetime.datetime,
        until: datetime.datetime,
        session_id: Optional[str] = None,
    ) -> List[SessionExitRecord]:
        """Every fatal exit record this pid wrote inside the window, oldest first."""

        def parse(line, pid, since, until):
            return self._parse_exit_line(line, pid, since, until)

        records = [
            replace(record, log_file=log_file)
            for log_file, record in self._scan(pid, since, until, parse)
        ]
        if session_id is not None:
            records = [record for record in records if record.session_id == session_id]
        records.sort(key=lambda record: record.timestamp_utc)
        return records

    @classmethod
    def _parse_exit_line(
        cls, line: str, pid: int, since: datetime.datetime, until: datetime.datetime
    ) -> Optional[SessionExitRecord]:
        # Cheap prefilter: the log is megabytes and only these lines matter.
        if "Session exit recorded" not in line:
            return None
        try:
            entry = json.loads(line)
        except Exception:
            return None
        if not isinstance(entry, dict) or entry.get("message") != "Session exit recorded":
            return None
        if entry.get("pid") != pid:
            return None
        if str(entry.get("kind", "")).lower() != "fatal":
            return None
        timestamp = parse_iso_utc(str(entry.get("timestamp", "")))
        if not timestamp or timestamp < since or timestamp > until:
            return None
        session = str(entry.get("sessionId") or "")
        if not session:
            return None
        try:
            pending = int(entry.get("pendingToolCalls") or 0)
        except (TypeError, ValueError):
            pending = 0
        return SessionExitRecord(
            session_id=session,
            session_file=str(entry.get("sessionFile") or ""),
            reason=str(entry.get("reason") or ""),
            kind="fatal",
            pending_tool_calls=pending,
            timestamp_utc=timestamp.isoformat(),
            log_file="",
        )

    def read_unhandled_rejections(
        self, pid: int, since: datetime.datetime, until: datetime.datetime
    ) -> List[Dict[str, Any]]:
        """The unhandled rejections that ended this pid, in order."""

        def parse(line, pid, since, until):
            if "Unhandled rejection" not in line:
                return None
            try:
                entry = json.loads(line)
            except Exception:
                return None
            if not isinstance(entry, dict) or entry.get("message") != "Unhandled rejection":
                return None
            if entry.get("pid") != pid:
                return None
            timestamp = parse_iso_utc(str(entry.get("timestamp", "")))
            if not timestamp or timestamp < since or timestamp > until:
                return None
            error = entry.get("err") if isinstance(entry.get("err"), dict) else {}
            return {
                "timestamp_utc": timestamp.isoformat(),
                "name": str(error.get("name") or ""),
                "message": str(error.get("message") or ""),
                "code": str(error.get("code") or ""),
            }

        return [entry for _path, entry in self._scan(pid, since, until, parse)]


@dataclass(frozen=True)
class InFlightMarker:
    """A tool call marker a process leaves on disk while it is inside that call."""

    pid: int
    session_id: str
    tool_call_id: str
    tool_name: str
    started_at: str
    marker_file: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class InFlightMarkerStore:
    """Reader for Veyyon's `logs/inflight` tool call markers."""

    def __init__(self, inflight_dir: Optional[Path] = None):
        self.inflight_dir = Path(inflight_dir or DEFAULT_INFLIGHT_DIR)

    def markers_for_pid(self, pid: int) -> List[InFlightMarker]:
        """Markers this pid left behind. A cleared call removes its own marker."""
        markers: List[InFlightMarker] = []
        try:
            files = sorted(self.inflight_dir.glob("*.json"))
        except OSError:
            return markers
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict) or data.get("pid") != pid:
                continue
            markers.append(
                InFlightMarker(
                    pid=pid,
                    session_id=str(data.get("sessionId") or ""),
                    tool_call_id=str(data.get("toolCallId") or ""),
                    tool_name=str(data.get("toolName") or ""),
                    started_at=str(data.get("startedAt") or ""),
                    marker_file=str(path),
                )
            )
        return markers


def build_interrupted_lane_manifest(
    *,
    session_id: str,
    dead_pid: int,
    creation_time_utc: str,
    death_observed_utc: str,
    exit_code: Optional[int],
    observed_reason: str,
    exit_records: Sequence[SessionExitRecord],
    rejection_records: Sequence[Dict[str, Any]],
    in_flight_markers: Sequence[InFlightMarker],
    resume: Optional[Dict[str, Any]] = None,
    resume_command: Optional[str] = None,
    pane_id: Optional[str] = None,
) -> Dict[str, Any]:
    """The death certificate a resumed Main reads: what died and what died with it."""
    lanes: Dict[str, Dict[str, Any]] = {}
    main_exit: Optional[Dict[str, Any]] = None
    for record in exit_records:
        entry = record.to_dict()
        if record.session_id == session_id:
            main_exit = entry
            continue
        key = record.session_id or record.lane_name
        previous = lanes.get(key)
        if previous is None or entry["pending_tool_calls"] > previous["pending_tool_calls"]:
            lanes[key] = entry

    calls_by_session: Dict[str, List[Dict[str, Any]]] = {}
    for marker in in_flight_markers:
        calls_by_session.setdefault(marker.session_id, []).append(marker.to_dict())

    return {
        "schema": "veyyon/interrupted-lane-manifest/v1",
        "session_id": session_id,
        "dead_pid": dead_pid,
        "creation_time_utc": creation_time_utc,
        "death_observed_utc": death_observed_utc,
        "exit_code": exit_code,
        "observed_reason": observed_reason,
        "main_exit_record": main_exit,
        "unhandled_rejections": list(rejection_records),
        "in_flight_tool_calls": [marker.to_dict() for marker in in_flight_markers],
        "interrupted_lane_count": len(lanes),
        "interrupted_lanes": [lanes[key] for key in sorted(lanes)],
        "lanes_with_in_flight_tool_calls": {
            key: calls_by_session[key] for key in sorted(calls_by_session)
        },
        # The resume decision as it stood when this manifest was written; the path of
        # the manifest itself is derived from session_id, so it is not repeated here.
        "resume": dict(resume) if resume else None,
        "resume_command": resume_command,
        "herdr_pane_id": pane_id,
    }


def write_interrupted_lane_manifest(
    manifest: Dict[str, Any], manifest_dir: Optional[Path] = None
) -> Path:
    """Publish the manifest for this session, replacing any earlier death's."""
    directory = Path(manifest_dir or DEFAULT_MANIFEST_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"interrupted_lanes_{manifest['session_id']}.json"
    temp_file = path.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    os.replace(temp_file, path)
    return path


class SessionCrashMonitor:
    """External supervisor monitoring a target session process for unexpected termination."""

    def __init__(
        self,
        session_id: str = DEFAULT_SESSION_ID,
        pid: Optional[int] = None,
        state_file: Optional[Path] = None,
        marker_paths: Optional[Sequence[Path]] = None,
        project: str = DEFAULT_PROJECT,
        canonical_link: str = DEFAULT_CANONICAL_LINK,
        poll_interval: float = 2.0,
        dry_run: bool = False,
        notifier_adapter: Optional[TelegramNotificationAdapter] = None,
        probe: Optional[ProcessProbe] = None,
        auto_resume: bool = False,
        resume_argv: Optional[Sequence[str]] = None,
        herdr: Optional[HerdrPanes] = None,
        herdr_pane: Optional[str] = None,
        registry: Optional[SessionRegistry] = None,
        log_dir: Optional[Path] = None,
        inflight_dir: Optional[Path] = None,
        manifest_dir: Optional[Path] = None,
        alert_test_label: bool = False,
    ):
        self.session_id = session_id
        self.target_pid = pid
        self.state_file = Path(state_file or DEFAULT_STATE_FILE)
        self.state_ledger = CrashMonitorStateLedger(self.state_file)
        self.marker_paths = list(marker_paths or [])
        self.project = project
        self.canonical_link = canonical_link
        self.poll_interval = poll_interval
        self.dry_run = dry_run
        self.notifier = notifier_adapter or TelegramNotificationAdapter()
        self.registry = registry
        # A probe built here inherits the registry, because a windowed Veyyon names
        # its session only in the registry and not on its command line.
        self.probe = probe or ProcessProbe(session_registry=registry)
        self.bound_target: Optional[SessionProcessInfo] = None
        self.auto_resume = auto_resume
        self.resume_argv = list(resume_argv or default_resume_argv(session_id))
        self.herdr = herdr
        self.target_pane_id = herdr_pane
        self.log_reader = SessionExitLogReader(log_dir) if log_dir is not None else None
        self.inflight = (
            InFlightMarkerStore(inflight_dir) if inflight_dir is not None else None
        )
        self.manifest_dir = Path(manifest_dir) if manifest_dir is not None else None
        self.alert_test_label = alert_test_label
        self._pane_attempts = 0

    def bind_target(self) -> Tuple[bool, str]:
        """Resolves the target PID, verifies the session binding, and pins its pane."""
        if self.target_pid is None:
            discovered = self.probe.discover_session_pid(self.session_id)
            if not discovered:
                return False, f"Could not discover running process for session {self.session_id}"
            self.target_pid = discovered

        ok, err, bound = self.probe.verify_session_binding(self.session_id, self.target_pid)
        if not ok or not bound:
            return False, f"Target PID {self.target_pid} verification failed: {err}"

        self.bound_target = bound
        self.ensure_target_pane()
        # Register supervisor state
        self.state_ledger.register_supervisor(
            self.session_id,
            self.target_pid,
            bound.creation_time_utc,
            {
                "supervisor_pid": os.getpid(),
                "bound_at_utc": utc_now_iso(),
                "creation_time_utc": bound.creation_time_utc,
                "poll_interval": self.poll_interval,
                "herdr_pane_id": self.target_pane_id,
                "auto_resume": self.auto_resume,
            },
        )
        pane_note = f", herdr pane {self.target_pane_id}" if self.target_pane_id else ""
        return (
            True,
            f"Bound target PID {self.target_pid} created at "
            f"{bound.creation_time_utc}{pane_note}",
        )

    def ensure_target_pane(self) -> Optional[str]:
        """Pin the pane hosting the target, while it is still alive to be located.

        A dead pid leaves no ancestry to walk, so this has to succeed before the
        death. Bounded retries cover a pane that herdr has not finished describing.
        """
        if self.target_pane_id or self.herdr is None or self.target_pid is None:
            return self.target_pane_id
        if self._pane_attempts >= PANE_LOOKUP_ATTEMPTS:
            return None
        self._pane_attempts += 1
        try:
            self.target_pane_id = self.herdr.pane_for_pid(self.target_pid, self.probe)
        except Exception:
            self.target_pane_id = None
        return self.target_pane_id

    def build_manifest(
        self,
        target: SessionProcessInfo,
        exit_code: Optional[int],
        observed_reason: str,
        resume: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Collect what died and what died with it, from the log and the markers."""
        now = datetime.datetime.now(datetime.timezone.utc)
        since = parse_iso_utc(target.creation_time_utc) or (now - datetime.timedelta(days=1))
        until = now + datetime.timedelta(seconds=5)

        exit_records: List[SessionExitRecord] = []
        rejections: List[Dict[str, Any]] = []
        if self.log_reader is not None:
            exit_records = self.log_reader.read_fatal_exits(target.pid, since, until)
            rejections = self.log_reader.read_unhandled_rejections(target.pid, since, until)

        markers: List[InFlightMarker] = []
        if self.inflight is not None:
            markers = self.inflight.markers_for_pid(target.pid)

        return build_interrupted_lane_manifest(
            session_id=self.session_id,
            dead_pid=target.pid,
            creation_time_utc=target.creation_time_utc,
            death_observed_utc=utc_now_iso(),
            exit_code=exit_code,
            observed_reason=observed_reason,
            exit_records=exit_records,
            rejection_records=rejections,
            in_flight_markers=markers,
            resume=resume,
            resume_command=(
                build_resume_command(self.resume_argv) if self.auto_resume else None
            ),
            pane_id=self.target_pane_id,
        )

    def auto_resume_death(
        self,
        target: SessionProcessInfo,
        exit_code: Optional[int],
        observed_reason: str,
    ) -> Dict[str, Any]:
        """Relaunch the dead session once, in its own pane, when nothing owns it.

        Every refusal is explicit and reported, never silent: a session that still has
        a live owner, a missing registry, a missing herdr client, or an unresolved pane
        all leave `launched` false with the reason attached, because relaunching blindly
        risks two Mains on one session.
        """
        result: Dict[str, Any] = {
            "enabled": bool(self.auto_resume),
            "attempted": False,
            "launched": False,
            "reason": "auto_resume disabled",
            "pane_id": self.target_pane_id,
            "command": None,
            "live_owner_pid": None,
            "launched_at_utc": None,
        }

        if self.auto_resume:
            result["command"] = build_resume_command(self.resume_argv)

        if self.auto_resume:
            if self.registry is None:
                result["attempted"] = True
                result["reason"] = (
                    "no session registry configured, so a live owner cannot be ruled out"
                )
            else:
                owner = self.registry.live_owner(
                    self.session_id, self.probe, exclude_pid=target.pid
                )
                if owner is not None:
                    result["attempted"] = True
                    result["live_owner_pid"] = owner.pid
                    result["reason"] = (
                        f"session {self.session_id} is still owned by PID {owner.pid} "
                        "according to the terminal registry; not resuming"
                    )
                elif self.herdr is None:
                    result["attempted"] = True
                    result["reason"] = (
                        "no herdr client configured, so there is no pane to resume in"
                    )
                elif not self.target_pane_id:
                    result["attempted"] = True
                    result["reason"] = (
                        "the herdr pane holding the dead session was never resolved, "
                        "so there is no pane to resume it in"
                    )
                else:
                    result["attempted"] = True
                    if self.dry_run:
                        result["reason"] = "dry_run: relaunch not executed"
                    else:
                        launched, detail = self.herdr.run_in_pane(
                            self.target_pane_id, result["command"]
                        )
                        result["launched"] = launched
                        result["launched_at_utc"] = utc_now_iso() if launched else None
                        result["reason"] = (
                            f"relaunched in herdr pane {self.target_pane_id}"
                            if launched
                            else f"pane relaunch failed: {detail}"
                        )

        manifest = self.build_manifest(
            target, exit_code, observed_reason, result if self.auto_resume else None
        )
        result["interrupted_lane_count"] = manifest["interrupted_lane_count"]
        result["manifest_path"] = None
        if self.manifest_dir is not None:
            try:
                result["manifest_path"] = str(
                    write_interrupted_lane_manifest(manifest, self.manifest_dir)
                )
            except OSError as exc:
                result["manifest_error"] = f"{type(exc).__name__}: {exc}"

        return result


    def deliver_alert(
        self,
        pid: int,
        creation_time_utc: str,
        observed_reason: str,
        exit_code: Optional[int],
        is_test: bool = False,
        max_retries: int = 3,
        resume: Optional[Dict[str, Any]] = None,
    ) -> DeliveryReceipt:
        """Delivers alert to Telegram with bounded retries and records unsent status if transport is down.

        `resume` is the auto-resume outcome, folded into the alert so the operator
        learns what was relaunched, or why nothing was, from the alert alone.
        """
        utc_ts = utc_now_iso()
        recovery_executable = "veyyon.exe" if sys.platform == "win32" else "veyyon"
        recovery_extension = Path.home() / ".veyyon" / "telegram" / "index.ts"
        recovery_cmd = (
            f"{recovery_executable} --extension {recovery_extension} --resume {self.session_id}"
        )
        resume_details: List[str] = []
        if resume is not None:
            if resume.get("launched"):
                resume_details.append(
                    "Auto-resume: relaunched in herdr pane "
                    f"{resume.get('pane_id')} with `{resume.get('command')}`"
                )
            elif resume.get("enabled"):
                resume_details.append(f"Auto-resume: not relaunched, {resume.get('reason')}")
            else:
                resume_details.append("Auto-resume: disabled for this monitor run")
            resume_details.append(
                f"Interrupted lanes: {resume.get('interrupted_lane_count', 0)}"
            )
            if resume.get("manifest_path"):
                resume_details.append(
                    f"Interrupted-lane manifest: {resume['manifest_path']}"
                )
        msg_text = format_crash_alert_text(
            session_id=self.session_id,
            pid=pid,
            observed_reason=observed_reason,
            utc_timestamp=utc_ts,
            recovery_pointer=recovery_cmd,
            is_test=is_test,
            extra_details="\n".join(resume_details) if resume_details else None,
        )

        is_test_mode = is_test or ("TEST" in self.session_id)
        event = NotificationEvent(
            event_type="blocker" if not is_test_mode else "status",
            project=self.project,
            request_id=f"crash-{self.session_id[:8]}",
            summary=(
                f"Session {self.session_id} PID {pid} unexpected termination ({observed_reason}). Recovery: {recovery_cmd}"
                if not is_test_mode
                else f"TEST PROOF: Disposable crash monitor verification for PID {pid} ({observed_reason}). Main session untouched"
            ),
            canonical_link=self.canonical_link,
            metadata={
                "details": msg_text,
                "pid": pid,
                "exit_code": exit_code,
                "creation_time_utc": creation_time_utc,
                "observed_reason": observed_reason,
                "auto_resume": resume,
            },
            session_id=None if is_test_mode else self.session_id,
        )

        receipt: Optional[DeliveryReceipt] = None
        attempt = 0
        backoff = 1.0

        while attempt < max_retries:
            attempt += 1
            try:
                receipt = self.notifier.notify(
                    event=event,
                    dry_run=self.dry_run,
                    force=True,  # Bypass cooldown for critical crash alert
                )
                if receipt.delivered or self.dry_run:
                    break
            except Exception as e:
                if attempt >= max_retries:
                    receipt = DeliveryReceipt(
                        delivered=False,
                        status="unsent_transport_down",
                        reason=f"Transport exception after {attempt} attempts: {type(e).__name__}: {e}",
                    )
                    break
            time.sleep(backoff)
            backoff *= 2.0

        if not receipt:
            receipt = DeliveryReceipt(
                delivered=False,
                status="unsent_transport_down",
                reason=f"Failed delivery after {max_retries} attempts",
            )

        # If delivery failed or transport down, append to unsent queue
        if not receipt.delivered and not self.dry_run:
            self.state_ledger.add_unsent_event(
                {
                    "session_id": self.session_id,
                    "pid": pid,
                    "creation_time_utc": creation_time_utc,
                    "observed_at_utc": utc_ts,
                    "observed_reason": observed_reason,
                    "exit_code": exit_code,
                    "message_text": msg_text,
                    "delivery_status": receipt.status,
                    "delivery_reason": receipt.reason,
                }
            )

        return receipt

    def step(self) -> Dict[str, Any]:
        """Performs one monitoring evaluation cycle."""
        if not self.bound_target:
            ok, msg = self.bind_target()
            if not ok:
                return {"status": "unbound", "error": msg}

        target = self.bound_target
        info = self.probe.get_process_info(target.pid, expected_creation_time=target.creation_time_utc)

        # 1. If process is still alive and matches creation time:
        # Crucial invariant: do NOT equate quiet model, slow turn, or paused goal to crash.
        if info and info.is_alive:
            return {
                "status": "running",
                "session_id": self.session_id,
                "pid": target.pid,
                "creation_time_utc": target.creation_time_utc,
            }

        # 2. Process has terminated (either dead or PID reused by a new process)
        exit_code = info.exit_code if info else None

        # Check deduplication: if this exact owner termination has already been handled, skip.
        if self.state_ledger.has_processed(self.session_id, target.pid, target.creation_time_utc):
            return {
                "status": "already_processed",
                "session_id": self.session_id,
                "pid": target.pid,
                "creation_time_utc": target.creation_time_utc,
            }

        # 3. Distinguish planned stop / restart using planned stop marker
        planned_marker = PlannedStopEvaluator.check_planned_stop(
            self.session_id, target.pid, self.marker_paths
        )

        if planned_marker:
            record = {
                "session_id": self.session_id,
                "pid": target.pid,
                "creation_time_utc": target.creation_time_utc,
                "exit_code": exit_code,
                "classification": "PLANNED_STOP_SUPPRESSED",
                "observed_at_utc": utc_now_iso(),
                "observed_reason": f"Planned stop/restart confirmed by marker ({planned_marker['reason']})",
                "marker_info": planned_marker,
                "alert_sent": False,
                "telegram_delivery": {
                    "delivered": False,
                    "status": "suppressed_planned_stop",
                    "reason": "Alert suppressed because planned restart marker was present and matched.",
                },
            }
            self.state_ledger.record_termination(
                self.session_id, target.pid, target.creation_time_utc, record
            )
            return {
                "status": "terminated",
                "classification": "PLANNED_STOP_SUPPRESSED",
                "record": record,
            }

        # 4. Unexpected termination / crash
        observed_reason = (
            f"Process exited unexpectedly with code {exit_code}"
            if exit_code is not None
            else "Process terminated unexpectedly (process absent without planned stop marker)"
        )

        # Claim this death before relaunching. Two monitors watching one session both
        # observe the same exit, and only one of them may resume it. The claim lives in
        # its own ledger key space, so a supervisor that dies before it can alert never
        # suppresses the alert on the next run.
        claimed = self.state_ledger.claim_termination(
            self.session_id,
            target.pid,
            target.creation_time_utc,
            {"claimed_at_utc": utc_now_iso(), "supervisor_pid": os.getpid()},
        )

        resume: Optional[Dict[str, Any]] = None
        if claimed:
            resume = self.auto_resume_death(target, exit_code, observed_reason)
        else:
            is_corrupt = self.state_ledger.is_corrupt()
            claim_reason = (
                "crash monitor state file is corrupt; refusing to claim death or overwrite state"
                if is_corrupt
                else (
                    "another supervisor already claimed this death; "
                    "resuming exactly once means not resuming again"
                )
            )
            resume = {
                "enabled": bool(self.auto_resume),
                "attempted": False,
                "launched": False,
                "reason": claim_reason,
                "pane_id": self.target_pane_id,
                "command": build_resume_command(self.resume_argv),
                "live_owner_pid": None,
                "launched_at_utc": None,
                "manifest_path": None,
            }
            manifest = self.build_manifest(
                target, exit_code, observed_reason, resume if self.auto_resume else None
            )
            resume["interrupted_lane_count"] = manifest["interrupted_lane_count"]
            if self.manifest_dir is not None:
                try:
                    resume["manifest_path"] = str(
                        write_interrupted_lane_manifest(manifest, self.manifest_dir)
                    )
                except OSError as exc:
                    resume["manifest_error"] = f"{type(exc).__name__}: {exc}"
        receipt = self.deliver_alert(
            pid=target.pid,
            creation_time_utc=target.creation_time_utc,
            observed_reason=observed_reason,
            exit_code=exit_code,
            is_test=False,
            resume=resume,
        )

        record = {
            "session_id": self.session_id,
            "pid": target.pid,
            "creation_time_utc": target.creation_time_utc,
            "exit_code": exit_code,
            "classification": "UNEXPECTED_TERMINATION",
            "observed_at_utc": utc_now_iso(),
            "observed_reason": observed_reason,
            "alert_sent": receipt.delivered,
            "telegram_delivery": {
                "delivered": receipt.delivered,
                "status": receipt.status,
                "reason": receipt.reason,
                "message_id": getattr(receipt, "message_id", None),
            },
            "auto_resume": resume if self.auto_resume else None,
            "manifest_path": resume.get("manifest_path") if resume else None,
            "interrupted_lane_count": (
                resume.get("interrupted_lane_count", 0) if resume else 0
            ),
        }
        self.state_ledger.record_termination(
            self.session_id, target.pid, target.creation_time_utc, record
        )

        return {
            "status": "terminated",
            "classification": "UNEXPECTED_TERMINATION",
            "record": record,
            "receipt": asdict(receipt),
            "auto_resume": resume if self.auto_resume else None,
            "manifest_path": resume.get("manifest_path") if resume else None,
        }

    def run(self, max_cycles: Optional[int] = None) -> int:
        """Runs the monitoring loop until termination is observed or max_cycles reached."""
        if self.poll_interval <= 0:
            print("[session-crash-monitor] poll_interval must be positive", file=sys.stderr)
            return 1
        ok, msg = self.bind_target()
        if not ok:
            print(f"[session-crash-monitor] Bind failed: {msg}", file=sys.stderr)
            return 1

        print(
            f"[session-crash-monitor] Monitoring session {self.session_id} on PID {self.target_pid} "
            f"(poll_interval={self.poll_interval}s, dry_run={self.dry_run})"
        )

        cycles = 0
        while True:
            cycles += 1
            result = self.step()
            status = result.get("status")

            if status == "running":
                # Process is healthy; wait for next cycle
                pass
            elif status == "terminated":
                classification = result.get("classification")
                print(
                    f"[session-crash-monitor] Target process termination observed: {classification}"
                )
                if classification == "PLANNED_STOP_SUPPRESSED":
                    print("[session-crash-monitor] Planned stop marker matched. Alert suppressed.")
                else:
                    delivered = result.get("receipt", {}).get("delivered", False)
                    print(
                        f"[session-crash-monitor] Crash alert processed. Telegram delivered={delivered}"
                    )
                    resume = result.get("auto_resume")
                    if resume:
                        print(
                            "[session-crash-monitor] Auto-resume: "
                            f"launched={resume.get('launched')} "
                            f"pane={resume.get('pane_id')} reason={resume.get('reason')}"
                        )
                return 0
            elif status == "already_processed":
                print("[session-crash-monitor] Target process termination already handled.")
                return 0
            else:
                print(f"[session-crash-monitor] Cycle returned unexpected status: {result}")

            if max_cycles is not None and cycles >= max_cycles:
                print(f"[session-crash-monitor] Completed {cycles} cycles without termination.")
                return 0

            time.sleep(self.poll_interval)

def run_disposable_crash_test(
    session_id: str = DEFAULT_SESSION_ID,
    state_file: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Runs an actual disposable process unexpected exit through the monitor to verify real Telegram delivery."""
    # 1. Spawn a disposable python subprocess that terminates unexpectedly with exit code 88
    test_session_id = f"{session_id}-DISPOSABLE-TEST"
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; time.sleep(0.5); sys.exit(88)", f"--resume={test_session_id}"]
    )
    pid = proc.pid
    monitor = SessionCrashMonitor(
        session_id=test_session_id,
        pid=pid,
        state_file=state_file or DEFAULT_STATE_FILE,
        poll_interval=0.5,
        dry_run=dry_run,
    )

    ok, msg = monitor.bind_target()
    if not ok:
        return {"ok": False, "error": f"Failed to bind disposable process PID {pid}: {msg}"}

    # 2. Wait for process to terminate
    proc.wait(timeout=5)

    # 3. Monitor step detects termination, evaluates marker (none), delivers alert to Telegram
    step_res = monitor.step()

    return {
        "ok": True,
        "disposable_pid": pid,
        "test_session_id": test_session_id,
        "exit_code": proc.returncode,
        "step_result": step_res,
    }


class _RecordingNotifier:
    """Captures alerts instead of dispatching them.

    Used by verifications that must exercise the real relaunch path without sending a
    real Telegram alert; the alert surface itself is proven by `--test-disposable-crash`.
    """

    def __init__(self):
        self.events: List[Dict[str, Any]] = []

    def notify(self, event, dry_run: bool = False, force: bool = False):
        self.events.append({"event": event, "dry_run": dry_run, "force": force})
        return DeliveryReceipt(
            delivered=True,
            status="captured_by_verification",
            reason="alert captured by the resume-relaunch verification",
        )


def run_resume_relaunch_test(
    herdr_bin: str = "herdr",
    state_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Exercises the relaunch path end to end on a throwaway herdr pane.

    A scratch pane is split off an agentless pane, a disposable process is started
    inside it, and the real auto-resume path runs against that process: pane resolution
    by process ancestry, the live-owner refusal, the single-claim dedup, the herdr
    delivery, and the interrupted-lane manifest write.

    The resume command is a sentinel writer rather than a real `veyyon --resume`, so no
    Veyyon session is created, contacted, or resumed. What is proven is that the monitor
    resolves the pane that actually hosts the target, delivers the command it built into
    that pane exactly once, and writes a manifest.
    """
    workdir = Path(tempfile.mkdtemp(prefix="veyyon-resume-relaunch-"))
    sentinel = workdir / "pane-ran-resume.txt"
    pid_file = workdir / "target.pid"
    go_file = workdir / "let-the-target-die"
    registry_dir = workdir / "terminals"
    log_dir = workdir / "logs"
    registry_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Everything handed to a shell below is one token per argument, with no nested
    # quoting, because it has to survive being typed into a pane.
    target_script = workdir / "target.py"
    target_script.write_text(
        "import os, pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "go = pathlib.Path(sys.argv[2])\n"
        "deadline = time.time() + 180\n"
        "while not go.exists() and time.time() < deadline:\n"
        "    time.sleep(0.2)\n"
        "time.sleep(0.5)\n"
        "sys.exit(88)\n",
        encoding="utf-8",
    )
    sentinel_script = workdir / "resume_sentinel.py"
    sentinel_script.write_text(
        "import pathlib\n"
        f"pathlib.Path(r'{sentinel}').write_text('resumed-in-herdr-pane')\n",
        encoding="utf-8",
    )

    test_session_id = f"{DEFAULT_SESSION_ID}-RESUME-RELAUNCH-TEST"
    report: Dict[str, Any] = {
        "ok": False,
        "workdir": str(workdir),
        "test_session_id": test_session_id,
        "herdr_bin": herdr_bin,
        "steps": {},
    }
    herdr = HerdrPanes(herdr_bin=herdr_bin)
    pane_id: Optional[str] = None
    notifier = _RecordingNotifier()
    try:
        host_pane = herdr.scratch_host_pane()
        report["steps"]["scratch_host_pane"] = host_pane
        pane_id = herdr.split_pane(from_pane=host_pane, cwd=str(workdir))
        report["steps"]["scratch_pane_id"] = pane_id
        if not pane_id:
            report["error"] = "herdr did not report a new scratch pane"
            return report

        # A freshly split pane needs its shell before it can accept a command.
        ready_deadline = time.time() + 20
        while time.time() < ready_deadline and herdr.pane_shell_pid(pane_id) is None:
            time.sleep(0.2)
        report["steps"]["scratch_pane_shell_pid"] = herdr.pane_shell_pid(pane_id)

        target_command = " ".join(
            [
                _quote_for_shell(sys.executable),
                _quote_for_shell(str(target_script)),
                _quote_for_shell(str(pid_file)),
                _quote_for_shell(str(go_file)),
                f"--resume={test_session_id}",
            ]
        )
        launched, detail = herdr.run_in_pane(pane_id, target_command)
        report["steps"]["target_launch"] = detail
        if not launched:
            report["error"] = f"could not start the disposable target in {pane_id}"
            return report

        pid_deadline = time.time() + 30
        while time.time() < pid_deadline and not pid_file.exists():
            time.sleep(0.2)
        if not pid_file.exists():
            report["error"] = "the disposable target never reported its pid"
            return report
        target_pid = int(pid_file.read_text(encoding="utf-8").strip())
        report["steps"]["target_pid"] = target_pid

        def build_monitor(**overrides) -> SessionCrashMonitor:
            kwargs: Dict[str, Any] = {
                "session_id": test_session_id,
                "pid": target_pid,
                "state_file": state_file or (workdir / "state.json"),
                "poll_interval": 0.2,
                # Real relaunch, captured alert: the delivery surface is proven
                # separately by --test-disposable-crash.
                "dry_run": False,
                "notifier_adapter": notifier,
                "auto_resume": True,
                "resume_argv": [sys.executable, str(sentinel_script)],
                "herdr": herdr,
                "registry": SessionRegistry(registry_dir),
                "log_dir": log_dir,
                "inflight_dir": log_dir / "inflight",
                "manifest_dir": workdir / "manifests",
            }
            kwargs.update(overrides)
            return SessionCrashMonitor(**kwargs)

        monitor = build_monitor()
        ok, message = monitor.bind_target()
        report["steps"]["bind"] = {"ok": ok, "message": message}
        report["steps"]["resolved_pane_id"] = monitor.target_pane_id
        if not ok:
            report["error"] = f"bind failed: {message}"
            return report
        if monitor.target_pane_id != pane_id:
            report["error"] = (
                f"pane resolution picked {monitor.target_pane_id}, "
                f"not the pane hosting the target ({pane_id})"
            )
            return report

        # Let the disposable target die, then run exactly one monitoring cycle.
        go_file.write_text("go", encoding="utf-8")
        exit_deadline = time.time() + 30
        while time.time() < exit_deadline:
            info = monitor.probe.get_process_info(target_pid)
            if not info or not info.is_alive:
                break
            time.sleep(0.2)
        info_after = monitor.probe.get_process_info(target_pid)
        report["steps"]["target_alive_after_wait"] = bool(info_after and info_after.is_alive)

        step_result = monitor.step()
        report["steps"]["step_status"] = step_result.get("status")
        report["steps"]["step_classification"] = step_result.get("classification")
        resume = step_result.get("auto_resume") or {}
        report["steps"]["auto_resume"] = resume
        report["steps"]["alerts_captured"] = len(notifier.events)

        sentinel_deadline = time.time() + 15
        while time.time() < sentinel_deadline and not sentinel.exists():
            time.sleep(0.2)
        pane_ran = sentinel.exists()
        report["steps"]["pane_ran_resume_command"] = pane_ran
        if pane_ran:
            report["steps"]["sentinel_text"] = sentinel.read_text(encoding="utf-8")

        # One death, one relaunch: the same termination must not be claimable twice.
        second_claim = monitor.state_ledger.claim_termination(
            test_session_id,
            target_pid,
            monitor.bound_target.creation_time_utc,
            {"claimed_at_utc": utc_now_iso(), "supervisor_pid": -1},
        )
        report["steps"]["second_claim_refused"] = not second_claim

        manifest_path = resume.get("manifest_path")
        report["steps"]["manifest_path"] = manifest_path
        if manifest_path and Path(manifest_path).exists():
            report["steps"]["manifest"] = json.loads(
                Path(manifest_path).read_text(encoding="utf-8")
            )

        # A restarted supervisor replaying the same binding must stop at the ledger
        # rather than relaunching a second time.
        restarted = build_monitor()
        restarted.target_pid = target_pid
        restarted.bound_target = monitor.bound_target
        report["steps"]["restarted_status"] = restarted.step().get("status")

        report["ok"] = bool(
            resume.get("launched")
            and pane_ran
            and report["steps"]["second_claim_refused"]
            and report["steps"]["restarted_status"] == "already_processed"
        )
        return report
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        return report
    finally:
        if pane_id:
            _closed, close_detail = herdr.close_pane(pane_id)
            report["steps"]["scratch_pane_closed"] = close_detail
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Independent Telegram Session Crash Monitor")
    parser.add_argument(
        "--session-id",
        default=DEFAULT_SESSION_ID,
        help="Target session UUID to monitor",
    )
    parser.add_argument(
        "--pid",
        type=int,
        default=None,
        help="Target PID to bind (auto-discovered if omitted)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=DEFAULT_STATE_FILE,
        help="Path to durable crash monitor state JSON",
    )
    parser.add_argument(
        "--marker-path",
        type=Path,
        action="append",
        dest="marker_paths",
        help="Additional planned stop marker path(s) to check",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Polling interval in seconds",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Maximum polling cycles before exiting (default: loop indefinitely)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate Telegram delivery without network dispatch",
    )
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="Send a real clearly-labelled test verification message to Telegram and exit",
    )
    parser.add_argument(
        "--test-disposable-crash",
        action="store_true",
        help="Run an actual disposable process unexpected exit through monitor to verify real Telegram delivery",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Inspect and print monitor state for the target session",
    )
    parser.add_argument(
        "--auto-resume",
        action="store_true",
        help=(
            "Relaunch the dead session once, as `veyyon --resume <session>` typed into "
            "the herdr pane it died in, after proving nothing else still owns it"
        ),
    )
    parser.add_argument(
        "--herdr-bin",
        default="herdr",
        help="herdr executable used to locate and drive the target's pane",
    )
    parser.add_argument(
        "--no-herdr",
        action="store_true",
        help="Do not consult herdr; auto-resume then declines for want of a pane",
    )
    parser.add_argument(
        "--registry-dir",
        type=Path,
        default=DEFAULT_TERMINAL_REGISTRY_DIR,
        help="Terminal registry directory naming the live owners of a session",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=DEFAULT_LOG_DIR,
        help="Veyyon log directory scanned for the dead process's exit facts",
    )
    parser.add_argument(
        "--manifest-dir",
        type=Path,
        default=DEFAULT_MANIFEST_DIR,
        help="Directory the interrupted-lane manifest is published to",
    )
    parser.add_argument(
        "--test-resume-relaunch",
        action="store_true",
        help=(
            "Prove the herdr pane relaunch path on a throwaway pane and exit; "
            "creates no Veyyon session and touches no live one"
        ),
    )

    args = parser.parse_args()

    registry = SessionRegistry(args.registry_dir)
    herdr = None if args.no_herdr else HerdrPanes(herdr_bin=args.herdr_bin)

    monitor = SessionCrashMonitor(
        session_id=args.session_id,
        pid=args.pid,
        state_file=args.state_file,
        marker_paths=args.marker_paths,
        poll_interval=args.poll_interval,
        dry_run=args.dry_run,
        auto_resume=args.auto_resume,
        herdr=herdr,
        registry=registry,
        log_dir=args.log_dir,
        inflight_dir=args.log_dir / "inflight",
        manifest_dir=args.manifest_dir,
    )

    if args.status:
        state_ledger = monitor.state_ledger
        lock = FileLock(str(state_ledger.lock_path))
        with lock:
            state = state_ledger._load_locked()
        print(json.dumps(state, indent=2))
        return 0

    if args.test_telegram:
        print("[session-crash-monitor] Sending verified Telegram test proof...")
        receipt = monitor.deliver_alert(
            pid=args.pid or os.getpid(),
            creation_time_utc=utc_now_iso(),
            observed_reason="Controlled disposable verification proof for crash monitor activation",
            exit_code=0,
            is_test=True,
        )
        print(
            json.dumps(
                {
                    "delivered": receipt.delivered,
                    "status": receipt.status,
                    "reason": receipt.reason,
                    "message_id": getattr(receipt, "message_id", None),
                    "slot": getattr(receipt, "slot_id", None),
                },
                indent=2,
            )
        )
        return 0 if receipt.delivered or args.dry_run else 1

    if args.test_disposable_crash:
        print("[session-crash-monitor] Running disposable process unexpected exit verification through monitor...")
        res = run_disposable_crash_test(
            session_id=args.session_id,
            state_file=args.state_file,
            dry_run=args.dry_run,
        )
        print(json.dumps(res, indent=2, default=str))
        delivered = res.get("step_result", {}).get("receipt", {}).get("delivered", False)
        return 0 if delivered or args.dry_run else 1

    if args.test_resume_relaunch:
        print("[session-crash-monitor] Running herdr pane relaunch verification...")
        res = run_resume_relaunch_test(
            herdr_bin=args.herdr_bin,
            state_file=args.state_file,
        )
        print(json.dumps(res, indent=2, default=str))
        return 0 if res.get("ok") else 1

    return monitor.run(max_cycles=args.max_cycles)


if __name__ == "__main__":
    sys.exit(main())
