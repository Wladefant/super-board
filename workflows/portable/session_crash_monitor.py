#!/usr/bin/env python3
"""Portable independent Telegram session crash monitor and external supervisor.

Supervises a target Veyyon session instance outside Main lifetime, binding
sessionUUID + verified PID + process creation time to detect unexpected termination
while handling PID reuse, distinguishing planned stops/restarts, and avoiding false
positives from quiet/slow model turns or paused goals.

Alerts are deduplicated per owner termination and delivered through the verified
Telegram route without raw tool spam, link dumps, or secret leakage.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
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
DEFAULT_CANONICAL_LINK = "https://github.com/Bavariance/polysimulator/issues/4543"


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

        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not h:
            # Fallback with just QUERY_LIMITED_INFORMATION
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)

        if not h:
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
        """Verifies process exists, gets creation time, and checks command line for session ID."""
        info = self.get_process_info(pid)
        if not info or not info.is_alive:
            return False, "Process not alive or does not exist", None

        command_line = self.get_command_line(pid)
        if command_line and session_id not in command_line:
            return (
                False,
                f"Process PID {pid} command line does not reference session {session_id}",
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

    def discover_session_pid(self, session_id: str) -> Optional[int]:
        """Discovers running veyyon process PID holding --resume <session_id>."""
        if sys.platform == "win32":
            try:
                cmd = [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    'Get-CimInstance Win32_Process -Filter "Name=\'veyyon.exe\'" | '
                    f'Where-Object {{ $_.CommandLine -like "*{session_id}*" }} | '
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
                    return data
        except Exception:
            pass
        return {
            "schema": "veyyon/crash-monitor-state/v1",
            "updated_utc": utc_now_iso(),
            "recorded_terminations": {},
            "unsent_events": [],
            "active_supervisors": {},
        }

    def _save_locked(self, data: Dict[str, Any]) -> None:
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
        self.probe = probe or ProcessProbe()
        self.bound_target: Optional[SessionProcessInfo] = None

    def bind_target(self) -> Tuple[bool, str]:
        """Resolves target PID if not given, verifies binding, and establishes baseline creation time."""
        if self.target_pid is None:
            discovered = self.probe.discover_session_pid(self.session_id)
            if not discovered:
                return False, f"Could not discover running process for session {self.session_id}"
            self.target_pid = discovered

        ok, err, bound = self.probe.verify_session_binding(self.session_id, self.target_pid)
        if not ok or not bound:
            return False, f"Target PID {self.target_pid} verification failed: {err}"

        self.bound_target = bound
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
            },
        )
        return True, f"Bound target PID {self.target_pid} created at {bound.creation_time_utc}"

    def deliver_alert(
        self,
        pid: int,
        creation_time_utc: str,
        observed_reason: str,
        exit_code: Optional[int],
        is_test: bool = False,
        max_retries: int = 3,
    ) -> DeliveryReceipt:
        """Delivers alert to Telegram with bounded retries and records unsent status if transport is down."""
        utc_ts = utc_now_iso()
        recovery_cmd = (
            f"veyyon.exe --extension C:/Users/wkiri/.veyyon/telegram/index.ts --resume {self.session_id}"
        )
        msg_text = format_crash_alert_text(
            session_id=self.session_id,
            pid=pid,
            observed_reason=observed_reason,
            utc_timestamp=utc_ts,
            recovery_pointer=recovery_cmd,
            is_test=is_test,
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
            },
            session_id=self.session_id,
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

        receipt = self.deliver_alert(
            pid=target.pid,
            creation_time_utc=target.creation_time_utc,
            observed_reason=observed_reason,
            exit_code=exit_code,
            is_test=False,
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
        }
        self.state_ledger.record_termination(
            self.session_id, target.pid, target.creation_time_utc, record
        )

        return {
            "status": "terminated",
            "classification": "UNEXPECTED_TERMINATION",
            "record": record,
            "receipt": asdict(receipt),
        }

    def run(self, max_cycles: Optional[int] = None) -> int:
        """Runs the monitoring loop until termination is observed or max_cycles reached."""
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

    args = parser.parse_args()

    monitor = SessionCrashMonitor(
        session_id=args.session_id,
        pid=args.pid,
        state_file=args.state_file,
        marker_paths=args.marker_paths,
        poll_interval=args.poll_interval,
        dry_run=args.dry_run,
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

    return monitor.run(max_cycles=args.max_cycles)


if __name__ == "__main__":
    sys.exit(main())
