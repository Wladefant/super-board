#!/usr/bin/env python3
"""
Build & Browser Slot Arbiter (workflows/build_slot.py)

Arbitrates the exclusive build/browser slot ('next build', 'next start', dev Chromium)
across parallel lanes using an atomic directory lock and a FIFO queue file.

Replaces orchestrator IRC messages ('BUILD SLOT TAKEN/FREE') with a local,
Windows-safe (no fcntl), crash-resilient lock file.

Commands:
    acquire <name> [--timeout SEC] [--stale-after SEC] [--poll-interval SEC] [--force]
    release <name>
    status [--json]

Invariants:
    - Atomic acquisition via os.mkdir('~/.veyyon/run/build-slot.lock').
    - Lock metadata contains owner, pid, and acquired_at timestamp.
    - Waiting lanes are tracked in a FIFO queue file ('~/.veyyon/run/build-slot.queue.json').
    - Release by non-owner is strictly refused.
    - Stale locks (owner PID dead, or older than --stale-after [default 30m]) are reclaimed
      with a logged notice.
    - RAM guard: acquire refuses when host system RAM >= 85% unless --force is passed.
    - Pure standard library + Windows-safe ctypes (zero fcntl imports).
"""

import argparse
import ctypes
import datetime
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple
import uuid

logger = logging.getLogger("build_slot")

DEFAULT_RUN_DIR = os.path.expanduser("~/.veyyon/run")
LOCK_DIR_NAME = "build-slot.lock"
QUEUE_FILE_NAME = "build-slot.queue.json"
QUEUE_LOCK_NAME = "build-slot-queue.lock"
INFO_FILE_NAME = "info.json"

DEFAULT_STALE_AFTER_SECONDS = 30 * 60  # 30 minutes
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0  # update queue entry heartbeat every <=15s
DEFAULT_QUEUE_STALE_HEARTBEAT_SECONDS = 60.0  # reclaim if heartbeat older than 60s
DEFAULT_QUEUE_STALE_FALLBACK_SECONDS = 30 * 60  # 30 minutes fallback for legacy entries without heartbeat
RAM_GUARD_THRESHOLD_PERCENT = 85.0

def get_system_ram_percent() -> Optional[float]:
    """
    Returns the host system RAM usage percentage (0.0 to 100.0).
    Windows: uses GlobalMemoryStatusEx via ctypes.
    Linux: uses /proc/meminfo.
    Returns None if telemetry cannot be determined.
    """
    env_override = os.environ.get("BUILD_SLOT_RAM_PERCENT")
    if env_override is not None:
        try:
            return float(env_override)
        except ValueError:
            pass
    if sys.platform == "win32":
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return float(stat.dwMemoryLoad)
        except Exception as e:
            logger.warning("Failed to query Windows GlobalMemoryStatusEx: %s", e)
    elif sys.platform.startswith("linux"):
        try:
            mem_total = None
            mem_avail = None
            if os.path.exists("/proc/meminfo"):
                with open("/proc/meminfo", "r", encoding="utf-8") as f:
                    for line in f:
                        if line.startswith("MemTotal:"):
                            mem_total = float(line.split()[1])
                        elif line.startswith("MemAvailable:"):
                            mem_avail = float(line.split()[1])
                if mem_total and mem_avail:
                    return round(((mem_total - mem_avail) / mem_total) * 100.0, 1)
        except Exception as e:
            logger.warning("Failed to read Linux /proc/meminfo: %s", e)

    # Fallback to psutil if installed
    try:
        import psutil  # type: ignore
        return float(psutil.virtual_memory().percent)
    except Exception:
        pass

    return None


def is_pid_alive(pid: int) -> bool:
    """
    Checks whether a process with the given PID is alive.
    Windows: uses OpenProcess + GetExitCodeProcess.
    POSIX: uses os.kill(pid, 0).
    """
    if pid <= 0:
        return False

    if sys.platform == "win32":
        try:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
            if not handle:
                err = k32.GetLastError()
                # ERROR_ACCESS_DENIED (5) means the process exists but is restricted
                if err == 5:
                    return True
                return False

            STILL_ACTIVE = 259
            exit_code = ctypes.c_ulong()
            success = k32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            k32.CloseHandle(handle)
            if success and exit_code.value == STILL_ACTIVE:
                return True
            return False
        except Exception as e:
            logger.warning("Windows is_pid_alive check failed for PID %d: %s", pid, e)
            return True  # err on the side of caution
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False


def _parse_timestamp(val: Any) -> Optional[float]:
    """Parses numeric epoch or ISO timestamp string into epoch seconds."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val)
        except ValueError:
            pass
        try:
            return datetime.datetime.fromisoformat(val).timestamp()
        except Exception:
            pass
    return None


@contextmanager
def _queue_atomic_lock(run_dir: str, timeout: float = 10.0, retry_interval: float = 0.05):
    """
    Short-lived atomic directory lock protecting reads/writes to build-slot.queue.json.
    Uses atomic os.mkdir on Windows and Linux (no fcntl).
    """
    queue_lock_dir = os.path.join(run_dir, QUEUE_LOCK_NAME)
    start_time = time.time()
    acquired = False

    while True:
        try:
            os.mkdir(queue_lock_dir)
            acquired = True
            break
        except FileExistsError:
            # Check if queue lock is stale (older than 15s indicates abandoned lock)
            try:
                mtime = os.path.getmtime(queue_lock_dir)
                if time.time() - mtime > 15.0:
                    shutil.rmtree(queue_lock_dir, ignore_errors=True)
                    continue
            except Exception:
                pass

            if time.time() - start_time >= timeout:
                raise TimeoutError(f"Timed out waiting for queue file lock: {queue_lock_dir}")
            time.sleep(retry_interval)

    try:
        yield
    finally:
        if acquired:
            try:
                os.rmdir(queue_lock_dir)
            except Exception:
                pass


class BuildSlotManager:
    """
    Manages the exclusive build slot with atomic directory locking,
    FIFO queuing, stale lock reclamation, and RAM safety.
    """

    def __init__(
        self,
        run_dir: Optional[str] = None,
        is_pid_alive_fn=None,
        queue_stale_heartbeat_after: float = DEFAULT_QUEUE_STALE_HEARTBEAT_SECONDS,
        queue_stale_fallback_after: float = DEFAULT_QUEUE_STALE_FALLBACK_SECONDS,
    ):
        self.run_dir = os.path.abspath(run_dir or DEFAULT_RUN_DIR)
        self.lock_dir = os.path.join(self.run_dir, LOCK_DIR_NAME)
        self.info_file = os.path.join(self.lock_dir, INFO_FILE_NAME)
        self.queue_file = os.path.join(self.run_dir, QUEUE_FILE_NAME)
        self.is_pid_alive = is_pid_alive_fn or is_pid_alive
        self.queue_stale_heartbeat_after = queue_stale_heartbeat_after
        self.queue_stale_fallback_after = queue_stale_fallback_after
        os.makedirs(self.run_dir, exist_ok=True)

    def _read_lock_info(self) -> Optional[Dict[str, Any]]:
        """Reads lock info metadata if lock dir exists."""
        if not os.path.isdir(self.lock_dir):
            return None

        if not os.path.isfile(self.info_file):
            # Directory exists but info.json is missing; might be mid-creation or orphan
            try:
                mtime = os.path.getmtime(self.lock_dir)
            except Exception:
                mtime = time.time()
            return {
                "owner": "unknown",
                "pid": 0,
                "acquired_at": datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc).isoformat(),
                "acquired_at_epoch": mtime,
                "corrupt": True,
            }

        try:
            with open(self.info_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data
        except Exception as e:
            logger.warning("Failed to read lock info: %s", e)
            try:
                mtime = os.path.getmtime(self.lock_dir)
            except Exception:
                mtime = time.time()
            return {
                "owner": "unknown",
                "pid": 0,
                "acquired_at": datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc).isoformat(),
                "acquired_at_epoch": mtime,
                "corrupt": True,
            }

    def _write_lock_info(self, owner: str, pid: int) -> None:
        """Writes info.json inside the newly created lock directory."""
        now = time.time()
        now_iso = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()
        info = {
            "owner": owner,
            "pid": pid,
            "acquired_at": now_iso,
            "acquired_at_epoch": now,
        }
        with open(self.info_file, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)

    def _read_queue(self) -> List[Dict[str, Any]]:
        """Reads queue list safely without lock."""
        if not os.path.isfile(self.queue_file):
            return []
        try:
            with open(self.queue_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            pass
        return []

    def _write_queue(self, queue: List[Dict[str, Any]]) -> None:
        """Writes queue list atomically using a temp file and os.replace."""
        temp_dir = self.run_dir
        fd, temp_path = tempfile.mkstemp(dir=temp_dir, prefix="queue-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(queue, f, indent=2)
            os.replace(temp_path, self.queue_file)
        except Exception:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
            raise

    def _is_entry_stale(
        self,
        item: Dict[str, Any],
        now: float,
        stale_heartbeat_after: float,
        stale_fallback_after: float,
    ) -> Tuple[bool, str]:
        """
        Evaluates whether a queue entry is stale.
        Rule: removed when its PID is dead OR heartbeat_at is older than 60s
        (entries without heartbeat_at from older versions: fall back to enqueued_at age > 30 min).
        """
        pid = item.get("pid", 0)
        if pid > 0 and not self.is_pid_alive(pid):
            return True, f"PID {pid} is dead"

        hb_val = item.get("heartbeat_at")
        if hb_val is not None:
            hb_epoch = _parse_timestamp(hb_val)
            if hb_epoch is not None:
                hb_age = now - hb_epoch
                if hb_age > stale_heartbeat_after:
                    return True, f"heartbeat expired ({hb_age:.1f}s > {stale_heartbeat_after:.1f}s)"
            else:
                return True, "corrupt heartbeat_at timestamp"
        else:
            enq_val = item.get("enqueued_at")
            if enq_val is not None:
                enq_epoch = _parse_timestamp(enq_val)
                if enq_epoch is not None:
                    enq_age = now - enq_epoch
                    if enq_age > stale_fallback_after:
                        return True, f"legacy entry enqueued_at expired ({enq_age:.1f}s > {stale_fallback_after:.1f}s)"
                else:
                    return True, "corrupt enqueued_at timestamp"
            else:
                return True, "missing heartbeat_at and enqueued_at"

        return False, ""

    def _clean_queue_locked(
        self,
        queue: List[Dict[str, Any]],
        now: float,
        stale_heartbeat_after: float,
        stale_fallback_after: float,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Filters out stale entries while holding queue atomic lock."""
        new_queue = []
        changed = False
        for item in queue:
            stale, reason = self._is_entry_stale(item, now, stale_heartbeat_after, stale_fallback_after)
            if stale:
                changed = True
                logger.info(
                    "Pruned queue entry '%s' (token=%s, PID=%s): %s",
                    item.get("name"),
                    item.get("token"),
                    item.get("pid"),
                    reason,
                )
                continue
            new_queue.append(item)
        return new_queue, changed

    def clean_queue(
        self,
        stale_heartbeat_after: Optional[float] = None,
        stale_fallback_after: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """
        Removes dead PIDs and stale entries (heartbeat expired or legacy age exceeded)
        from the queue to maintain FIFO integrity.
        Returns the updated queue.
        """
        hb_limit = stale_heartbeat_after if stale_heartbeat_after is not None else self.queue_stale_heartbeat_after
        fb_limit = stale_fallback_after if stale_fallback_after is not None else self.queue_stale_fallback_after

        with _queue_atomic_lock(self.run_dir):
            queue = self._read_queue()
            new_queue, changed = self._clean_queue_locked(queue, time.time(), hb_limit, fb_limit)
            if changed:
                self._write_queue(new_queue)
            return new_queue

    def enqueue(self, name: str, pid: int, token: Optional[str] = None) -> int:
        """
        Adds (name, pid, token) to the queue if not already present.
        Returns the 0-indexed position in queue.
        """
        with _queue_atomic_lock(self.run_dir):
            queue = self._read_queue()
            now = time.time()
            valid_queue, changed = self._clean_queue_locked(
                queue, now, self.queue_stale_heartbeat_after, self.queue_stale_fallback_after
            )
            if token is not None:
                existing_idx = next(
                    (i for i, item in enumerate(valid_queue) if item.get("token") == token),
                    None,
                )
                if existing_idx is None:
                    # Check if there is an untokenized entry for (name, pid) to bind to
                    legacy_idx = next(
                        (
                            i
                            for i, item in enumerate(valid_queue)
                            if item.get("name") == name and item.get("pid") == pid and not item.get("token")
                        ),
                        None,
                    )
                    if legacy_idx is not None:
                        valid_queue[legacy_idx]["token"] = token
                        valid_queue[legacy_idx]["heartbeat_at"] = now
                        valid_queue[legacy_idx]["heartbeat_at_iso"] = datetime.datetime.fromtimestamp(
                            now, datetime.timezone.utc
                        ).isoformat()
                        self._write_queue(valid_queue)
                        return legacy_idx
            else:
                existing_idx = next(
                    (i for i, item in enumerate(valid_queue) if item.get("name") == name and item.get("pid") == pid),
                    None,
                )

            if existing_idx is not None:
                if changed or len(valid_queue) != len(queue):
                    self._write_queue(valid_queue)
                return existing_idx

            now_iso = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()
            entry = {
                "name": name,
                "pid": pid,
                "token": token,
                "enqueued_at": now,
                "enqueued_at_iso": now_iso,
                "heartbeat_at": now,
                "heartbeat_at_iso": now_iso,
            }
            valid_queue.append(entry)
            self._write_queue(valid_queue)
            return len(valid_queue) - 1

    def dequeue(
        self,
        name: Optional[str] = None,
        pid: Optional[int] = None,
        token: Optional[str] = None,
    ) -> None:
        """Removes entry matching token, or (name, pid) if token is not provided."""
        with _queue_atomic_lock(self.run_dir):
            queue = self._read_queue()
            new_queue = []
            for item in queue:
                if token is not None:
                    if item.get("token") == token:
                        continue
                    if not item.get("token") and name is not None and item.get("name") == name:
                        if pid is None or item.get("pid") == pid:
                            continue
                else:
                    if name is not None and item.get("name") == name:
                        if pid is None or item.get("pid") == pid:
                            continue
                new_queue.append(item)
            if len(new_queue) != len(queue):
                self._write_queue(new_queue)

    def heartbeat(
        self,
        token: Optional[str] = None,
        name: Optional[str] = None,
        pid: Optional[int] = None,
    ) -> bool:
        """
        Updates the heartbeat_at timestamp for the queue entry identified by token
        (or name + pid if token is not provided/matched).
        Returns True if an entry was found and updated, False otherwise.
        """
        if token is None and name is None:
            return False

        with _queue_atomic_lock(self.run_dir):
            queue = self._read_queue()
            now = time.time()
            now_iso = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()
            updated = False
            for item in queue:
                matched = False
                if token is not None and item.get("token") == token:
                    matched = True
                elif token is not None and not item.get("token") and name is not None and item.get("name") == name:
                    if pid is None or item.get("pid") == pid:
                        matched = True
                        item["token"] = token
                elif token is None and name is not None and item.get("name") == name:
                    if pid is None or item.get("pid") == pid:
                        matched = True

                if matched:
                    item["heartbeat_at"] = now
                    item["heartbeat_at_iso"] = now_iso
                    updated = True
                    break

            if updated:
                self._write_queue(queue)
            return updated

    def check_stale_and_reclaim(self, stale_after: float = DEFAULT_STALE_AFTER_SECONDS) -> bool:
        """
        Checks if the currently held lock is stale.
        Reclaims it if:
          1. Owner PID is dead.
          2. Lock age exceeds stale_after seconds.
          3. Corrupt lock directory older than 10s grace period.
        Returns True if a stale lock was reclaimed, False otherwise.
        """
        if not os.path.isdir(self.lock_dir):
            return False

        info = self._read_lock_info()
        now = time.time()
        is_stale = False
        reason = ""

        if info is None or info.get("corrupt"):
            mtime = os.path.getmtime(self.lock_dir) if os.path.exists(self.lock_dir) else now
            age = now - mtime
            if age > 10.0:  # grace period for mid-creation
                is_stale = True
                reason = f"corrupt or incomplete lock directory (age={age:.1f}s)"
        else:
            pid = info.get("pid", 0)
            owner = info.get("owner", "unknown")
            acquired_epoch = info.get("acquired_at_epoch")
            if acquired_epoch is None:
                try:
                    iso_str = info.get("acquired_at", "")
                    acquired_epoch = datetime.datetime.fromisoformat(iso_str).timestamp()
                except Exception:
                    acquired_epoch = now

            age = max(0.0, now - acquired_epoch)

            if pid > 0 and not self.is_pid_alive(pid):
                is_stale = True
                reason = f"owner PID {pid} is dead (owner='{owner}', age={age:.1f}s)"
            elif age >= stale_after:
                is_stale = True
                reason = f"exceeded stale-after threshold ({age:.1f}s >= {stale_after:.1f}s, owner='{owner}', PID={pid})"

        if is_stale:
            notice = f"[NOTICE] Reclaiming stale build slot lock: {reason}"
            print(notice, file=sys.stderr)
            try:
                if os.path.isfile(self.info_file):
                    os.unlink(self.info_file)
            except Exception:
                pass
            try:
                shutil.rmtree(self.lock_dir, ignore_errors=True)
            except Exception:
                pass
            return True

        return False

    def acquire(
        self,
        name: str,
        timeout: Optional[float] = None,
        stale_after: float = DEFAULT_STALE_AFTER_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        force: bool = False,
        pid: Optional[int] = None,
        token: Optional[str] = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    ) -> bool:
        """
        Acquires the build slot lock for 'name'.
        Blocks with poll_interval until acquired, or until timeout.
        Returns True on success, raises or returns False on failure.
        """
        if pid is None:
            pid = os.getpid()
        if token is None:
            token = str(uuid.uuid4())

        # 1. RAM Guard Check
        ram_pct = get_system_ram_percent()
        if ram_pct is not None and ram_pct >= RAM_GUARD_THRESHOLD_PERCENT:
            if not force:
                msg = (
                    f"RAM guard: acquisition refused for '{name}' because system RAM is at "
                    f"{ram_pct:.1f}% (>= {RAM_GUARD_THRESHOLD_PERCENT:.1f}% limit). "
                    f"Use --force to override."
                )
                print(msg, file=sys.stderr)
                logger.error(msg)
                return False
            else:
                notice = (
                    f"[NOTICE] RAM guard overridden with --force: system RAM is at {ram_pct:.1f}% "
                    f"(>= {RAM_GUARD_THRESHOLD_PERCENT:.1f}% limit)."
                )
                print(notice, file=sys.stderr)
                logger.warning(notice)

        # 2. Register in FIFO Queue
        self.enqueue(name, pid, token=token)
        start_time = time.time()
        last_heartbeat = start_time
        acquired = False

        try:
            while True:
                # Check and reclaim any stale lock
                self.check_stale_and_reclaim(stale_after=stale_after)

                # Clean dead PIDs / stale heartbeats from queue
                queue = self.clean_queue()

                # Write heartbeat if needed (every <= 15s)
                now = time.time()
                if now - last_heartbeat >= heartbeat_interval:
                    self.heartbeat(token=token, name=name, pid=pid)
                    last_heartbeat = now

                # Check if current caller is at the head of the FIFO queue
                is_head_of_queue = False
                if queue:
                    head = queue[0]
                    if head.get("token"):
                        is_head_of_queue = (head.get("token") == token)
                    elif head.get("name") == name and head.get("pid") == pid:
                        is_head_of_queue = True

                # If lock does not exist and we are head of queue, attempt atomic os.mkdir
                if not os.path.isdir(self.lock_dir):
                    if is_head_of_queue or not queue:
                        try:
                            os.mkdir(self.lock_dir)
                            # Atomic creation succeeded! We own the lock.
                            self._write_lock_info(owner=name, pid=pid)
                            acquired = True
                            self.dequeue(name, pid, token=token)
                            msg = f"Acquired build slot lock for '{name}' (PID {pid})"
                            print(msg)
                            logger.info(msg)
                            return True
                        except FileExistsError:
                            # Lost race to another lane
                            pass
                        except OSError as e:
                            logger.warning("os.mkdir failed: %s", e)
                else:
                    # Lock exists; check if we already own it (re-entrant / idempotent)
                    info = self._read_lock_info()
                    if info and info.get("owner") == name and info.get("pid") == pid:
                        acquired = True
                        self.dequeue(name, pid, token=token)
                        msg = f"Build slot lock already held by '{name}' (PID {pid})"
                        print(msg)
                        return True

                # Check timeout
                if timeout is not None:
                    elapsed = time.time() - start_time
                    if elapsed >= timeout:
                        msg = f"Timed out after {timeout:.1f}s waiting for build slot lock (lane '{name}', PID {pid})"
                        print(msg, file=sys.stderr)
                        logger.error(msg)
                        return False

                time.sleep(poll_interval)
        finally:
            # If we exited without holding the lock, remove self from queue
            if not acquired:
                try:
                    self.dequeue(name, pid, token=token)
                except Exception as e:
                    logger.warning("Failed to dequeue on cleanup: %s", e)

    def is_held_by(self, name: str, pid: Optional[int] = None) -> bool:
        """Returns True if the lock is held by 'name' (and optionally pid)."""
        info = self._read_lock_info()
        if not info:
            return False
        if info.get("owner") != name:
            return False
        if pid is not None and info.get("pid") != pid:
            return False
        return True

    def release(self, name: str) -> bool:
        """
        Releases the build slot lock.
        Refuses if the lock is held by a different owner.
        Returns True if released or already free; returns False if non-owner refused.
        """
        if not os.path.isdir(self.lock_dir):
            # Already free; remove name from queue if lingering
            self.dequeue(name)
            msg = f"Build slot lock is already free (release called for '{name}')"
            print(msg)
            return True

        info = self._read_lock_info()
        current_owner = info.get("owner", "unknown") if info else "unknown"
        current_pid = info.get("pid", 0) if info else 0

        if current_owner != name:
            msg = (
                f"ERROR: Refusing to release build slot lock: currently held by "
                f"'{current_owner}' (PID {current_pid}), not '{name}'."
            )
            print(msg, file=sys.stderr)
            logger.error(msg)
            return False

        # Owner matches: remove info file and rmdir
        try:
            if os.path.isfile(self.info_file):
                os.unlink(self.info_file)
        except Exception:
            pass

        try:
            os.rmdir(self.lock_dir)
        except Exception as e:
            # Fallback in case non-empty
            shutil.rmtree(self.lock_dir, ignore_errors=True)

        self.dequeue(name)
        msg = f"Released build slot lock for '{name}'"
        print(msg)
        logger.info(msg)
        return True

    def status(self, stale_after: float = DEFAULT_STALE_AFTER_SECONDS) -> Dict[str, Any]:
        """
        Returns full status dictionary and prints summary.
        Reclaims stale locks with a logged notice.
        """
        # 1. Reclaim stale lock if present
        reclaimed = self.check_stale_and_reclaim(stale_after=stale_after)

        # 2. Read lock info
        info = self._read_lock_info()
        now = time.time()

        lock_status: Dict[str, Any] = {
            "locked": False,
            "owner": None,
            "pid": None,
            "acquired_at": None,
            "age_seconds": None,
            "pid_alive": None,
        }

        if info and os.path.isdir(self.lock_dir):
            lock_status["locked"] = True
            lock_status["owner"] = info.get("owner")
            lock_status["pid"] = info.get("pid")
            lock_status["acquired_at"] = info.get("acquired_at")

            acquired_epoch = info.get("acquired_at_epoch")
            if acquired_epoch is None:
                try:
                    iso_str = info.get("acquired_at", "")
                    acquired_epoch = datetime.datetime.fromisoformat(iso_str).timestamp()
                except Exception:
                    acquired_epoch = now
            age = max(0.0, now - acquired_epoch)
            lock_status["age_seconds"] = round(age, 1)

            pid = info.get("pid", 0)
            lock_status["pid_alive"] = self.is_pid_alive(pid) if pid > 0 else False

        # 3. Clean queue and read
        queue = self.clean_queue()
        queue_status = []
        for item in queue:
            enqueued_epoch = item.get("enqueued_at", now)
            wait_time = max(0.0, now - enqueued_epoch)
            hb_val = item.get("heartbeat_at")
            hb_epoch = _parse_timestamp(hb_val) if hb_val is not None else None
            hb_age = round(max(0.0, now - hb_epoch), 1) if hb_epoch is not None else None
            queue_status.append({
                "name": item.get("name"),
                "pid": item.get("pid"),
                "token": item.get("token"),
                "enqueued_at": item.get("enqueued_at_iso"),
                "wait_seconds": round(wait_time, 1),
                "heartbeat_at": item.get("heartbeat_at_iso"),
                "heartbeat_age_seconds": hb_age,
            })

        # 4. System RAM
        ram_pct = get_system_ram_percent()

        result = {
            "lock": lock_status,
            "queue": queue_status,
            "queue_depth": len(queue_status),
            "ram_percent": ram_pct,
            "ram_guard_threshold": RAM_GUARD_THRESHOLD_PERCENT,
            "stale_reclaimed_in_status": reclaimed,
            "run_dir": self.run_dir,
            "lock_dir": self.lock_dir,
        }
        return result


def format_status_human(stat: Dict[str, Any]) -> str:
    """Formats status dictionary for terminal display."""
    lines = []
    lines.append("=== Build Slot Arbiter Status ===")
    lock = stat.get("lock", {})
    if lock.get("locked"):
        age = lock.get("age_seconds", 0)
        alive_str = "alive" if lock.get("pid_alive") else "DEAD"
        lines.append(f"Status:      LOCKED")
        lines.append(f"Owner:       {lock.get('owner')}")
        lines.append(f"PID:         {lock.get('pid')} ({alive_str})")
        lines.append(f"Acquired:    {lock.get('acquired_at')} (age: {age:.1f}s)")
    else:
        lines.append("Status:      FREE (unlocked)")

    ram = stat.get("ram_percent")
    ram_str = f"{ram:.1f}%" if ram is not None else "unavailable"
    ram_status = " (ELEVATED >= 85%)" if (ram is not None and ram >= stat.get("ram_guard_threshold", 85.0)) else " (OK)"
    lines.append(f"System RAM:  {ram_str}{ram_status}")

    queue = stat.get("queue", [])
    lines.append(f"FIFO Queue:  {len(queue)} waiting")
    for i, q in enumerate(queue):
        hb_age = q.get("heartbeat_age_seconds")
        hb_str = f", hb: {hb_age:.1f}s ago" if hb_age is not None else ""
        lines.append(f"  [{i + 1}] {q.get('name')} (PID {q.get('pid')}, waiting {q.get('wait_seconds', 0):.1f}s{hb_str})")
    return "\n".join(lines)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_slot.py",
        description="Exclusive build/browser slot arbiter using atomic directory locks and FIFO queue.",
    )
    parser.add_argument(
        "--run-dir",
        default=DEFAULT_RUN_DIR,
        help=f"Base run directory for lock and queue files (default: {DEFAULT_RUN_DIR})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # acquire <name> [--timeout SEC] [--stale-after SEC] [--poll-interval SEC] [--force]
    p_acq = subparsers.add_parser("acquire", help="Acquire build slot lock (blocks until available)")
    p_acq.add_argument("name", help="Lane or worker identifier requesting the slot")
    p_acq.add_argument("--pid", type=int, default=None, help="Explicit PID to associate with the lock (default: parent process PID)")
    p_acq.add_argument("--timeout", type=float, default=None, help="Maximum seconds to wait (default: block indefinitely)")
    p_acq.add_argument(
        "--stale-after",
        type=float,
        default=DEFAULT_STALE_AFTER_SECONDS,
        help=f"Seconds after which an inactive/dead lock is reclaimed (default: {DEFAULT_STALE_AFTER_SECONDS}s / 30m)",
    )
    p_acq.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"Seconds to sleep between retry polls (default: {DEFAULT_POLL_INTERVAL_SECONDS}s)",
    )
    p_acq.add_argument(
        "--force",
        action="store_true",
        help="Bypass the RAM guard (proceed even if system RAM >= 85%%)",
    )

    # release <name>
    p_rel = subparsers.add_parser("release", help="Release build slot lock (refused if not owner)")
    p_rel.add_argument("name", help="Lane or worker identifier releasing the slot")

    # status [--json]
    p_stat = subparsers.add_parser("status", help="Print current lock owner, age, and FIFO queue")
    p_stat.add_argument(
        "--stale-after",
        type=float,
        default=DEFAULT_STALE_AFTER_SECONDS,
        help=f"Seconds after which an inactive/dead lock is reclaimed during status check (default: {DEFAULT_STALE_AFTER_SECONDS}s / 30m)",
    )
    p_stat.add_argument("--json", action="store_true", help="Output status as structured JSON")

    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    manager = BuildSlotManager(run_dir=args.run_dir)

    if args.command == "acquire":
        caller_pid = args.pid
        if caller_pid is None:
            try:
                ppid = os.getppid()
                caller_pid = ppid if ppid > 0 else os.getpid()
            except Exception:
                caller_pid = os.getpid()
        success = manager.acquire(
            name=args.name,
            timeout=args.timeout,
            stale_after=args.stale_after,
            poll_interval=args.poll_interval,
            force=args.force,
            pid=caller_pid,
        )
        return 0 if success else 1

    elif args.command == "release":
        success = manager.release(name=args.name)
        return 0 if success else 1

    elif args.command == "status":
        stat = manager.status(stale_after=args.stale_after)
        if args.json:
            print(json.dumps(stat, indent=2))
        else:
            print(format_status_human(stat))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
