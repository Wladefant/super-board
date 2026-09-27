#!/usr/bin/env python3
"""
Build & Browser Slot Arbiter (workflows/build_slot.py)

Arbitrates the exclusive build/browser slot ('next build', 'next start', dev Chromium)
across parallel lanes using an atomic directory lock and a FIFO queue file.

Replaces orchestrator IRC messages ('BUILD SLOT TAKEN/FREE') with a local,
Windows-safe (no fcntl), crash-resilient lock file.

Commands:
    acquire <name> [--timeout SEC] [--heartbeat-stale-after SEC] [--poll-interval SEC] [--force]
    run <name> [--timeout SEC] [--priority] [--force] [--cwd DIR] [--heartbeat-stale-after SEC] -- <cmd...>
    release <name>
    status [--json]

Invariants:
    - Atomic acquisition via os.mkdir('~/.veyyon/run/build-slot.lock').
    - Lock metadata contains owner, pid, token, and acquired_at timestamp.
    - Waiting lanes are tracked in a FIFO queue file ('~/.veyyon/run/build-slot.queue.json')
      with unique tokens to differentiate in-process waiters sharing a parent PID.
    - Queue entries emit periodic heartbeats (heartbeat_at); entries with dead PIDs or
      heartbeats older than 60s are automatically reclaimed.
    - Legacy queue entries without heartbeats fall back to enqueued_at age > 30m.
    - Acquire wait loops write heartbeats before queue cleaning, re-enqueue if pruned,
      and clean up queue entries via try/finally on timeout, exit, or exception.
    - Release by non-owner is strictly refused.
    - Stale locks are reclaimed with a logged notice. A live holder is never reclaimed on
      age alone. A `run` lock lives exactly as long as its wrapper process (the
      `build_slot.py run` PID, which heartbeats every 5s, waits for the command and releases
      in `finally`): a dead wrapper is reclaimed after the 60s grace period, and a live one
      only once its heartbeat is older than --heartbeat-stale-after [default 5m] (hung).
      Other locks (`acquire` mode has no process left to heartbeat): only when the owner PID
      is dead past the grace period.
    - A reclaim renames the lock dir to a unique tombstone and deletes it only if the
      tombstone still holds the lock that was judged stale; otherwise it is put back. A
      reclaim never deletes a lock other than the one it judged stale.
    - The acquire-mode owner PID is the nearest veyyon session host (not its
      `__veyyon_worker*` helpers); the ancestor climb stops at a parent created after its
      child, since Windows reuses a dead parent's PID.
    - RAM guard: when host system RAM >= 85%, acquire stays in the FIFO queue and waits until
      RAM drops below the limit (or --timeout expires); --force bypasses the wait.
    - Pure standard library + Windows-safe ctypes (zero fcntl imports).
"""

import argparse
import ctypes
import datetime
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple
import uuid

logger = logging.getLogger("build_slot")

DEFAULT_RUN_DIR = os.path.expanduser("~/.veyyon/run")
LOCK_DIR_NAME = "build-slot.lock"
SLOT_LOCK_DIR_NAMES = ["build-slot.lock", "build-slot-1.lock"]
QUEUE_FILE_NAME = "build-slot.queue.json"
QUEUE_LOCK_NAME = "build-slot-queue.lock"
INFO_FILE_NAME = "info.json"

DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS = 5 * 60  # a live `run` holder silent this long is hung
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0  # update queue entry heartbeat every <=15s
DEFAULT_QUEUE_STALE_HEARTBEAT_SECONDS = 60.0  # reclaim if heartbeat older than 60s
DEFAULT_QUEUE_STALE_FALLBACK_SECONDS = 30 * 60  # 30 minutes fallback for legacy entries without heartbeat
RAM_TWO_SLOT_THRESHOLD_PERCENT = 75.0
DEFAULT_RAM_GUARD_THRESHOLD_PERCENT = 85.0
DEFAULT_RAM_TWO_SLOT_THRESHOLD_PERCENT = 75.0
DEFAULT_PID_DEAD_GRACE_PERIOD_SECONDS = 60.0  # never reclaim a dead-PID lock younger than 60s

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


# Bounded backoff for os.replace, which Windows refuses transiently while another process
# has the target open (WinError 5: Access is denied, WinError 32: sharing violation).
_REPLACE_RETRY_DELAYS = (0.05, 0.1, 0.15, 0.2, 0.25, 0.25, 0.25, 0.25, 0.25, 0.25)
# Lock info reads retried before a slot counts as corrupt (~0.1s in total).
_INFO_READ_ATTEMPTS = 5
_INFO_READ_RETRY_DELAY = 0.025


def _replace_with_retry(src: str, dst: str) -> None:
    """os.replace(src, dst), retried over ~2s on transient Windows sharing errors."""
    for attempt, delay in enumerate(_REPLACE_RETRY_DELAYS):
        try:
            os.replace(src, dst)
            return
        except (PermissionError, OSError) as e:
            transient = isinstance(e, PermissionError) or getattr(e, "winerror", None) in (5, 32)
            if not transient or attempt == len(_REPLACE_RETRY_DELAYS) - 1:
                raise
            time.sleep(delay)


def _write_json_atomic(path: str, data: Any, prefix: str = ".info-") -> None:
    """
    Writes JSON so readers see the old or the new document, never a truncated one.
    Lock info is read by every waiter on every poll; an in-place rewrite let a reader
    hit the empty file, call the live lock corrupt and reclaim it (#315).
    """
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _replace_with_retry(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


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


# veyyon.exe re-runs itself as helper processes (`__veyyon_worker_daemon_broker` behind
# `launch`, `__veyyon_worker_js_eval_process` behind JS eval). They live and die on their
# own schedule, so recording one as the owner freed acquire-mode slots mid-build (#315).
_VEYYON_HELPER_MARK = "__veyyon_worker"
# A veyyon entrypoint on a JS runtime's command line (`bun .../veyyon/.../cli.ts`); a path
# under the `.veyyon` config dir (MCP wrappers, the sidecar) is not one.
_VEYYON_ENTRY_RE = re.compile(r"(?<![.\w])veyyon(?![\w-])")
_JS_RUNTIME_EXES = ("node.exe", "bun.exe", "deno.exe", "node", "bun", "deno")


def _is_veyyon_host(pid: int, names: Dict[int, str], cmdlines: Dict[int, Optional[str]]) -> bool:
    """True when pid is a veyyon session host, not a helper worker or an unrelated tool."""
    exe = names.get(pid, "")
    cmdline = (cmdlines.get(pid) or "").lower()
    if _VEYYON_HELPER_MARK in cmdline:
        return False
    if "veyyon" in exe:
        return True
    return exe in _JS_RUNTIME_EXES and bool(_VEYYON_ENTRY_RE.search(cmdline))


def _owner_pid_from_process_table(
    cur_pid: int,
    parents: Dict[int, int],
    names: Dict[int, str],
    cmdlines: Optional[Dict[int, Optional[str]]] = None,
    created: Optional[Dict[int, int]] = None,
) -> int:
    """
    Climbs cur_pid's ancestors in a (pid -> parent pid, pid -> lowercase exe name) table.
    The nearest veyyon host (see _is_veyyon_host) ends the climb and is the owner; without
    one, the farthest ancestor reached stands in.
    Windows keeps a dead process's PID in its children's parent field and hands that PID to
    new processes, so a parent created after its child is an impostor: the climb stops there
    instead of wandering into an unrelated, often short-lived process (#315).
    """
    cmdlines = cmdlines or {}
    created = created or {}
    cur = cur_pid
    candidate = cur_pid
    visited = set()
    while cur in parents and cur not in visited and cur != 0:
        visited.add(cur)
        candidate = cur
        if _is_veyyon_host(cur, names, cmdlines):
            break
        parent = parents[cur]
        parent_created, child_created = created.get(parent), created.get(cur)
        if parent_created is not None and child_created is not None and parent_created > child_created:
            break
        cur = parent
    return candidate


def _win_process_details(pids: List[int]) -> Tuple[Dict[int, Optional[str]], Dict[int, int]]:
    """Command line and creation time (FILETIME ticks) for each pid that can be opened."""
    from ctypes import wintypes

    class UNICODE_STRING(ctypes.Structure):
        _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    ntdll.NtQueryInformationProcess.argtypes = (
        wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
    )
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_COMMAND_LINE_INFORMATION = 60

    cmdlines: Dict[int, Optional[str]] = {}
    created: Dict[int, int] = {}
    for pid in pids:
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                created[pid] = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            size = wintypes.ULONG(0)
            ntdll.NtQueryInformationProcess(handle, PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(size))
            if size.value:
                buf = ctypes.create_string_buffer(size.value)
                status = ntdll.NtQueryInformationProcess(
                    handle, PROCESS_COMMAND_LINE_INFORMATION, buf, size, ctypes.byref(size)
                )
                if status >= 0:
                    us = UNICODE_STRING.from_buffer(buf)
                    cmdlines[pid] = ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ""
        finally:
            k32.CloseHandle(handle)
    return cmdlines, created


_MAX_ANCESTOR_DEPTH = 64


def find_long_lived_owner_pid() -> int:
    """
    Finds the owner PID for `acquire`-mode lock attribution.
    The acquire CLI exits as soon as it holds the slot, so its own PID must not be
    recorded: that PID is dead within seconds and the dead-PID rule would free the slot
    60s into the build. Under veyyon the lane lives inside the veyyon host process, so
    the host PID is recorded; outside veyyon the farthest trustworthy ancestor stands in.
    """
    cur_pid = os.getpid()
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class PROCESSENTRY32(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260),
                ]

            k32 = ctypes.windll.kernel32
            TH32CS_SNAPPROCESS = 0x00000002
            h = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if h and h != -1:
                pe = PROCESSENTRY32()
                pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
                parents = {}
                names = {}
                if k32.Process32First(h, ctypes.byref(pe)):
                    while True:
                        pid = pe.th32ProcessID
                        ppid = pe.th32ParentProcessID
                        exe = pe.szExeFile.decode("latin-1", "ignore").lower()
                        parents[pid] = ppid
                        names[pid] = exe
                        if not k32.Process32Next(h, ctypes.byref(pe)):
                            break
                k32.CloseHandle(h)

                chain, cur = [], cur_pid
                while cur in parents and cur not in chain and len(chain) < _MAX_ANCESTOR_DEPTH:
                    chain.append(cur)
                    cur = parents[cur]
                if cur in parents:
                    chain.append(cur)
                cmdlines, created = _win_process_details(chain)
                return _owner_pid_from_process_table(cur_pid, parents, names, cmdlines, created)
        except Exception as e:
            logger.debug("Failed in find_long_lived_owner_pid: %s", e)

    try:
        ppid = os.getppid()
        if ppid > 0:
            return ppid
    except Exception:
        pass
    return cur_pid


def _corrupt_lock_info(lock_dir: str, slot_idx: int) -> Dict[str, Any]:
    try:
        mtime = os.path.getmtime(lock_dir)
    except Exception:
        mtime = time.time()
    return {
        "owner": "unknown",
        "pid": 0,
        "slot": slot_idx,
        "acquired_at": datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc).isoformat(),
        "acquired_at_epoch": mtime,
        "corrupt": True,
    }


def _read_lock_dir_info(lock_dir: str, slot_idx: int) -> Optional[Dict[str, Any]]:
    """Reads the info.json of a slot lock dir (or its tombstone); None if the dir is gone."""
    if not os.path.isdir(lock_dir):
        return None
    info_path = os.path.join(lock_dir, INFO_FILE_NAME)
    if not os.path.isfile(info_path):
        return _corrupt_lock_info(lock_dir, slot_idx)

    # Retry briefly: a holder still running an older copy rewrites info.json in place,
    # and Windows can refuse a read mid-replace. Only a persistent failure is corrupt.
    last_err: Optional[Exception] = None
    for attempt in range(_INFO_READ_ATTEMPTS):
        try:
            with open(info_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("slot", slot_idx)
            return data
        except Exception as e:
            last_err = e
            if attempt < _INFO_READ_ATTEMPTS - 1:
                time.sleep(_INFO_READ_RETRY_DELAY)
    logger.warning("Failed to read lock info for slot %d: %s", slot_idx, last_err)
    return _corrupt_lock_info(lock_dir, slot_idx)


def _lock_identity(info: Optional[Dict[str, Any]]) -> Optional[Tuple[Any, ...]]:
    """
    What tells one holding of a slot from the next: the per-acquisition token (acquire
    always sets one), else owner, PID and acquisition time for locks written without one.
    A corrupt lock is known only by its dir's mtime, which a new holder's dir never shares.
    """
    if not info:
        return None
    if info.get("corrupt"):
        return ("corrupt", info.get("acquired_at_epoch"))
    if info.get("token"):
        return ("token", info["token"])
    return ("legacy", info.get("owner"), info.get("pid"), info.get("acquired_at_epoch") or info.get("acquired_at"))


def _read_queue_lock_info(queue_lock_dir: str) -> Optional[Dict[str, Any]]:
    """Reads info.json from queue lock directory if present."""
    info_file = os.path.join(queue_lock_dir, INFO_FILE_NAME)
    if not os.path.isfile(info_file):
        return None
    try:
        with open(info_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


@contextmanager
def _queue_atomic_lock(
    run_dir: str,
    timeout: float = 10.0,
    retry_interval: float = 0.05,
    stale_after: float = 15.0,
    is_pid_alive_fn=None,
):
    """
    Short-lived atomic directory lock protecting reads/writes to build-slot.queue.json.
    Uses atomic os.mkdir on Windows and Linux (no fcntl).
    Reclaims stale queue locks if the holding PID is dead or age exceeds stale_after.
    """
    queue_lock_dir = os.path.join(run_dir, QUEUE_LOCK_NAME)
    start_time = time.time()
    acquired = False
    pid_checker = is_pid_alive_fn or is_pid_alive

    while True:
        try:
            os.mkdir(queue_lock_dir)
            # Write queue lock metadata (PID + timestamp) for stale reclamation
            try:
                info_path = os.path.join(queue_lock_dir, INFO_FILE_NAME)
                tmp_path = info_path + f".{os.getpid()}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "pid": os.getpid(),
                        "acquired_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "acquired_at_epoch": time.time(),
                    }, f)
                os.replace(tmp_path, info_path)
            except Exception:
                pass
            acquired = True
            break
        except PermissionError:
            # On Windows, a directory another process is removing sits in
            # delete-pending state, and os.mkdir raises PermissionError (WinError 5).
            # Treat like contention: honour timeout, sleep retry_interval, and retry.
            # Do NOT run stale-rmtree branch (the dir is already being deleted).
            if time.time() - start_time >= timeout:
                raise TimeoutError(f"Timed out waiting for queue file lock: {queue_lock_dir}")
            time.sleep(retry_interval)
        except FileExistsError:
            # Check if queue lock is stale:
            # 1. Owner PID is dead (immediate reclaim)
            # 2. Or lock age exceeds stale_after seconds
            # 3. Or corrupt lock directory older than grace period
            try:
                is_stale = False
                now = time.time()
                info = _read_queue_lock_info(queue_lock_dir)
                if info and info.get("pid"):
                    lock_pid = int(info["pid"])
                    if lock_pid > 0 and not pid_checker(lock_pid):
                        is_stale = True
                    else:
                        acq_time = info.get("acquired_at_epoch")
                        if acq_time and (now - float(acq_time)) >= stale_after:
                            is_stale = True
                else:
                    # No info file or mid-creation: fallback to directory mtime
                    mtime = os.path.getmtime(queue_lock_dir)
                    if (now - mtime) >= stale_after:
                        is_stale = True

                if is_stale:
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
                info_path = os.path.join(queue_lock_dir, INFO_FILE_NAME)
                if os.path.isfile(info_path):
                    os.unlink(info_path)
            except Exception:
                pass
            try:
                os.rmdir(queue_lock_dir)
            except Exception:
                try:
                    # Narrow fallback: call shutil.rmtree on the queue lock dir only
                    # if the dir is still ours (it was just created by us and is empty
                    # apart from our own files); otherwise leave it.
                    if os.path.isdir(queue_lock_dir):
                        entries = [e for e in os.listdir(queue_lock_dir) if e != INFO_FILE_NAME]
                        if not entries:
                            shutil.rmtree(queue_lock_dir, ignore_errors=True)
                except Exception:
                    pass


def _create_dir_link(target: str, link_path: str) -> None:
    """
    Creates a directory link (junction on Windows, symlink on Unix).
    On Windows, mklink /J creates an NTFS junction which does NOT require
    administrator privileges or developer mode.
    """
    target = os.path.abspath(target)
    link_path = os.path.abspath(link_path)
    if sys.platform == "win32":
        try:
            cmd = ["cmd.exe", "/c", "mklink", "/J", link_path, target]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return
        except Exception as e:
            logger.debug("cmd.exe mklink /J failed: %s; trying os.symlink", e)
        os.symlink(target, link_path, target_is_directory=True)
    else:
        os.symlink(target, link_path, target_is_directory=True)


def _remove_dir_link(link_path: str) -> bool:
    """
    Safely removes a directory junction or symlink without deleting target contents.
    On Windows, uses os.rmdir() or cmd /c rmdir without /s.
    """
    if not os.path.exists(link_path) and not os.path.islink(link_path):
        return False
    if sys.platform == "win32":
        try:
            os.rmdir(link_path)
            return True
        except Exception:
            try:
                cmd = ["cmd.exe", "/c", "rmdir", os.path.abspath(link_path)]
                res = subprocess.run(cmd, capture_output=True, text=True, check=True)
                return True
            except Exception as e:
                logger.warning("Failed to remove junction '%s': %s", link_path, e)
                return False
    else:
        try:
            if os.path.islink(link_path):
                os.unlink(link_path)
                return True
            else:
                os.rmdir(link_path)
                return True
        except Exception as e:
            logger.warning("Failed to remove link '%s': %s", link_path, e)
            return False


def _queue_order_key(item: Dict[str, Any]) -> Tuple[int, float]:
    """FIFO queue order: priority entries first, each group by original enqueue time."""
    return (0 if item.get("priority") else 1, _parse_timestamp(item.get("enqueued_at")) or 0.0)


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
        pid_dead_grace_period: Optional[float] = None,
        max_slots: Optional[int] = None,
        ram_two_slot_threshold: float = DEFAULT_RAM_TWO_SLOT_THRESHOLD_PERCENT,
        ram_guard_threshold: float = DEFAULT_RAM_GUARD_THRESHOLD_PERCENT,
    ):
        self.run_dir = os.path.abspath(run_dir or DEFAULT_RUN_DIR)
        self.slot_dirs = [os.path.join(self.run_dir, name) for name in SLOT_LOCK_DIR_NAMES]
        self.lock_dir = self.slot_dirs[0]
        self.info_file = os.path.join(self.lock_dir, INFO_FILE_NAME)
        self.queue_file = os.path.join(self.run_dir, QUEUE_FILE_NAME)
        self.is_pid_alive = is_pid_alive_fn or is_pid_alive
        self.queue_stale_heartbeat_after = queue_stale_heartbeat_after
        self.queue_stale_fallback_after = queue_stale_fallback_after
        self.pid_dead_grace_period = (
            float(pid_dead_grace_period)
            if pid_dead_grace_period is not None
            else DEFAULT_PID_DEAD_GRACE_PERIOD_SECONDS
        )
        self.max_slots_override = max_slots
        env_slots = os.environ.get("BUILD_SLOT_MAX_SLOTS")
        if env_slots is not None and self.max_slots_override is None:
            try:
                self.max_slots_override = int(env_slots)
            except ValueError:
                pass
        self.ram_two_slot_threshold = float(ram_two_slot_threshold)
        self.ram_guard_threshold = float(ram_guard_threshold)
        os.makedirs(self.run_dir, exist_ok=True)

    def get_max_slots(self, ram_pct: Optional[float] = None) -> int:
        """
        Returns the maximum number of concurrent build slots allowed:
        2 slots when system RAM is under 75% at acquisition, 1 slot otherwise.
        """
        if self.max_slots_override is not None:
            return self.max_slots_override
        if ram_pct is None:
            ram_pct = get_system_ram_percent()
        if ram_pct is not None and ram_pct >= self.ram_two_slot_threshold:
            return 1
        return 2

    def _read_slot_info(self, slot_idx: int = 0) -> Optional[Dict[str, Any]]:
        """Reads lock info metadata for slot_idx if its lock dir exists."""
        if slot_idx >= len(self.slot_dirs):
            return None
        return _read_lock_dir_info(self.slot_dirs[slot_idx], slot_idx)

    def _tombstone_stale_slot(self, slot_idx: int, judged: Dict[str, Any], reason: str) -> bool:
        """
        Removes the stale lock judged from `judged`, and only that lock (#315). Two waiters
        can judge the same lock stale: the first removes it, a lane acquires the free slot,
        and a plain rmtree by the second would delete that lane's live lock. So the lock is
        re-read, renamed to a unique tombstone and deleted only if the tombstone still holds
        the judged lock. The rename does not make the reclaimer the lock's only owner: on
        Windows two reclaimers' renames of the same dir can both report success, the later
        one carrying the dir out of the earlier one's tombstone. What holds is that nothing
        but the judged lock is ever deleted. Returns True if this call deleted it.
        """
        slot_dir = self.slot_dirs[slot_idx]
        want = _lock_identity(judged)
        if want is None or _lock_identity(self._read_slot_info(slot_idx)) != want:
            return False
        tombstone = f"{slot_dir}.tombstone-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            os.rename(slot_dir, tombstone)
        except OSError:
            # Another reclaimer or the holder moved it first, or Windows refused the rename
            # while a reader has info.json open; the next poll judges the slot again.
            return False
        # A rename keeps the dir's mtime, so even a corrupt lock's identity survives the move.
        moved = _read_lock_dir_info(tombstone, slot_idx)
        if moved is None:
            # Another reclaimer's rename carried the dir out of this tombstone; that reclaimer
            # checks it against its own judgment. Nothing was moved aside here to put back.
            return False
        if _lock_identity(moved) != want:
            # The lock changed hands between the re-read and the rename: put it back.
            try:
                os.rename(tombstone, slot_dir)
            except OSError as e:
                if not os.path.isdir(tombstone):
                    # Carried off by another reclaimer after the read above, as in the
                    # `moved is None` case: nothing is left here to put back.
                    return False
                msg = f"[ERROR] Moved a live build slot lock aside and could not restore it (slot {slot_idx}, {tombstone}): {e}"
                print(msg, file=sys.stderr)
                logger.error(msg)
            return False
        print(f"[NOTICE] Reclaiming stale build slot lock: {reason}", file=sys.stderr)
        shutil.rmtree(tombstone, ignore_errors=True)
        return True

    def _read_lock_info(self) -> Optional[Dict[str, Any]]:
        """Reads lock info metadata if primary lock dir exists (backwards compatibility)."""
        return self._read_slot_info(0)

    def _write_slot_info(
        self,
        slot_idx: int,
        owner: str,
        pid: int,
        token: Optional[str] = None,
        child_pid: Optional[int] = None,
    ) -> None:
        """Writes info.json inside the newly created lock directory for slot_idx."""
        if slot_idx >= len(self.slot_dirs):
            return
        slot_dir = self.slot_dirs[slot_idx]
        info_path = os.path.join(slot_dir, INFO_FILE_NAME)
        now = time.time()
        now_iso = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()
        info = {
            "owner": owner,
            "pid": pid,
            "token": token,
            "slot": slot_idx,
            "acquired_at": now_iso,
            "acquired_at_epoch": now,
            "heartbeat_at": now_iso,
            "heartbeat_at_epoch": now,
        }
        if child_pid is not None:
            info["child_pid"] = child_pid
        _write_json_atomic(info_path, info)

    def _write_lock_info(self, owner: str, pid: int, token: Optional[str] = None) -> None:
        """Writes info.json inside the primary lock directory (backwards compatibility)."""
        self._write_slot_info(0, owner, pid, token)

    def _read_queue(self) -> List[Dict[str, Any]]:
        """Reads queue list safely with retries for transient Windows locks."""
        if not os.path.isfile(self.queue_file):
            return []
        delays = [0.02, 0.05, 0.1, 0.15, 0.2]
        for attempt in range(len(delays)):
            try:
                with open(self.queue_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
                return []
            except (PermissionError, OSError) as e:
                winerror = getattr(e, "winerror", None)
                if isinstance(e, PermissionError) or winerror in (5, 32):
                    if attempt < len(delays) - 1:
                        time.sleep(delays[attempt])
                        continue
                logger.warning("Failed to read queue file '%s': %s", self.queue_file, e)
                return []
            except json.JSONDecodeError:
                if attempt < len(delays) - 1:
                    time.sleep(delays[attempt])
                    continue
                logger.warning("Corrupt JSON in queue file '%s'", self.queue_file)
                return []
            except Exception as e:
                logger.warning("Unexpected error reading queue file '%s': %s", self.queue_file, e)
                return []
        return []

    def _write_queue(self, queue: List[Dict[str, Any]]) -> None:
        """Writes queue list atomically using a temp file and os.replace."""
        _write_json_atomic(self.queue_file, queue, prefix="queue-")

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
        hb_val = item.get("heartbeat_at")
        hb_epoch = _parse_timestamp(hb_val) if hb_val is not None else None
        is_hb_fresh = (hb_epoch is not None and (now - hb_epoch) < stale_heartbeat_after)

        pid = item.get("pid", 0)
        if pid > 0 and not self.is_pid_alive(pid):
            if not is_hb_fresh:
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
        """Filters out stale entries and deduplicates by (name, pid) while holding queue atomic lock."""
        new_queue = []
        changed = False
        seen_keys = set()
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
            key = (item.get("name"), item.get("pid"))
            if key in seen_keys:
                changed = True
                logger.info(
                    "Deduplicated duplicate queue entry '%s' (token=%s, PID=%s)",
                    item.get("name"),
                    item.get("token"),
                    item.get("pid"),
                )
                continue
            seen_keys.add(key)
            new_queue.append(item)
        # FIFO order: priority entries first, then by original enqueue time (stable).
        ordered = sorted(new_queue, key=_queue_order_key)
        if ordered != new_queue:
            changed = True
        return ordered, changed

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

        try:
            with _queue_atomic_lock(self.run_dir, is_pid_alive_fn=self.is_pid_alive):
                queue = self._read_queue()
                new_queue, changed = self._clean_queue_locked(queue, time.time(), hb_limit, fb_limit)
                if changed:
                    try:
                        self._write_queue(new_queue)
                    except Exception as e:
                        logger.warning("Failed to write queue in clean_queue: %s (will retry next tick)", e)
                return new_queue
        except (TimeoutError, PermissionError, OSError) as e:
            logger.warning("Queue lock acquisition failed in clean_queue: %s; falling back to read-only queue", e)
            return self._read_queue()

    def enqueue(
        self,
        name: str,
        pid: int,
        token: Optional[str] = None,
        stale_heartbeat_after: Optional[float] = None,
        priority: bool = False,
    ) -> int:
        """
        Adds (name, pid, token) to the queue if not already present.
        The queue is kept in _queue_order_key order: priority entries first,
        each group first-come first-served by original enqueue time. A new
        entry lands behind every earlier entry of its group. An entry that is
        already queued only has its heartbeat refreshed; it moves only when
        re-enqueued with priority while still normal, and then joins the
        priority group at its own enqueue time.
        Returns the 0-indexed position in queue.
        """
        hb_limit = stale_heartbeat_after if stale_heartbeat_after is not None else self.queue_stale_heartbeat_after
        with _queue_atomic_lock(self.run_dir, is_pid_alive_fn=self.is_pid_alive):
            queue = self._read_queue()
            now = time.time()
            valid_queue, changed = self._clean_queue_locked(
                queue, now, hb_limit, self.queue_stale_fallback_after
            )
            existing_idx = None
            if token is not None:
                existing_idx = next(
                    (i for i, item in enumerate(valid_queue) if item.get("token") == token),
                    None,
                )
            if existing_idx is None:
                # Deduplicate by (name, pid)
                existing_idx = next(
                    (i for i, item in enumerate(valid_queue) if item.get("name") == name and item.get("pid") == pid),
                    None,
                )

            if existing_idx is not None:
                item = valid_queue[existing_idx]
                if token is not None:
                    item["token"] = token
                item["heartbeat_at"] = now
                item["heartbeat_at_iso"] = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()
                if priority and not item.get("priority"):
                    # Upgrade: joins the priority group at its own enqueue time.
                    item["priority"] = True
                    valid_queue.sort(key=_queue_order_key)
                self._write_queue(valid_queue)
                return valid_queue.index(item)

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
            if priority:
                entry["priority"] = True
            valid_queue.append(entry)
            # Newest enqueue time: lands behind every earlier entry of its group.
            valid_queue.sort(key=_queue_order_key)
            self._write_queue(valid_queue)
            return valid_queue.index(entry)

    def bump(self, name: str, token: Optional[str] = None) -> bool:
        """
        Marks the entry for 'name' (and optional token) as priority under the
        atomic queue lock. It joins the priority group at its own enqueue time,
        so it passes normal waiters but never an earlier priority waiter.
        Returns True if found, False otherwise.
        """
        with _queue_atomic_lock(self.run_dir, is_pid_alive_fn=self.is_pid_alive):
            queue = self._read_queue()
            target_idx = None
            for i, item in enumerate(queue):
                if token is not None and item.get("token") == token:
                    target_idx = i
                    break
                elif token is None and item.get("name") == name:
                    target_idx = i
                    break

            if target_idx is None:
                return False

            item = queue[target_idx]
            item["priority"] = True
            now = time.time()
            item["heartbeat_at"] = now
            item["heartbeat_at_iso"] = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()
            # Upgrade to priority but keep FIFO among priority entries (no queue jumping).
            queue.sort(key=_queue_order_key)
            self._write_queue(queue)
            return True

    def dequeue(
        self,
        name: Optional[str] = None,
        pid: Optional[int] = None,
        token: Optional[str] = None,
    ) -> None:
        """Removes entry matching token, or (name, pid) if token is not provided."""
        with _queue_atomic_lock(self.run_dir, is_pid_alive_fn=self.is_pid_alive):
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
        Never raises out on queue lock timeout or write error.
        """
        if token is None and name is None:
            return False

        try:
            with _queue_atomic_lock(self.run_dir, is_pid_alive_fn=self.is_pid_alive):
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
                    try:
                        self._write_queue(queue)
                    except Exception as e:
                        logger.warning(
                            "Failed to write queue during heartbeat for '%s' (token=%s, PID=%s): %s (will retry next tick)",
                            name, token, pid, e,
                        )
                        return False
                return updated
        except TimeoutError as e:
            logger.warning("Queue lock timeout during heartbeat for '%s': %s (will retry next tick)", name, e)
            return False
        except Exception as e:
            logger.warning("Queue heartbeat failed for '%s': %s (will retry next tick)", name, e)
            return False

    def _record_run_child(self, name: str, wrapper_pid: int, child_pid: int, token: Optional[str] = None) -> bool:
        """
        Records the `run` wrapper PID (the lock's liveness source) and the wrapped
        command's PID (diagnostics only) on the slot held by 'name', and refreshes the
        heartbeat. 'pid' stays the wrapper: the command may be a cmd.exe or launcher
        shim whose own lifetime says nothing about the build.
        """
        for slot_idx, slot_dir in enumerate(self.slot_dirs):
            if not os.path.isdir(slot_dir):
                continue
            info = self._read_slot_info(slot_idx)
            if not info or info.get("owner") != name:
                continue
            if token and info.get("token") and info.get("token") != token:
                continue
            now = time.time()
            now_iso = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()
            info["pid"] = wrapper_pid
            info["wrapper_pid"] = wrapper_pid
            info["child_pid"] = child_pid
            if token:
                info["token"] = token
                info["run_token"] = token
            info["heartbeat_at"] = now_iso
            info["heartbeat_at_epoch"] = now
            return self._write_heartbeat(name, slot_idx, slot_dir, info)
        return False

    def heartbeat_lock(self, name: str, token: Optional[str] = None) -> bool:
        """
        Updates the heartbeat timestamp in info.json of whichever slot is held by 'name'.
        """
        for slot_idx, slot_dir in enumerate(self.slot_dirs):
            if not os.path.isdir(slot_dir):
                continue
            info = self._read_slot_info(slot_idx)
            if not info or info.get("owner") != name:
                continue
            if token and info.get("token") and info.get("token") != token:
                continue
            now = time.time()
            now_iso = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()
            info["heartbeat_at"] = now_iso
            info["heartbeat_at_epoch"] = now
            return self._write_heartbeat(name, slot_idx, slot_dir, info)
        return False

    def _write_heartbeat(self, name: str, slot_idx: int, slot_dir: str, info: Dict[str, Any]) -> bool:
        """
        Writes heartbeat-bearing lock info. A failure gets its own line: a holder whose
        heartbeat writes keep failing is reclaimed as hung after heartbeat_stale_after, and
        this line is how that reclaim gets traced back to its cause (#315).
        """
        try:
            _write_json_atomic(os.path.join(slot_dir, INFO_FILE_NAME), info)
            return True
        except Exception as e:
            msg = f"[HEARTBEAT] Failed to write heartbeat for '{name}' (slot {slot_idx}): {e}"
            print(msg, file=sys.stderr)
            logger.warning(msg)
            return False

    def check_stale_and_reclaim(
        self,
        heartbeat_stale_after: float = DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS,
        pid_dead_grace_period: Optional[float] = None,
    ) -> bool:
        """
        Checks if the currently held lock is stale. A live holder is never reclaimed on age
        alone: a build that runs past any fixed age must keep its slot.
        A heartbeat counts as fresh when younger than the grace period (default 60s).
        Reclaims it if:
          1. `run` lock (has wrapper_pid): the wrapper is dead, the lock is older than the
             grace period and the heartbeat is not fresh; or the wrapper is alive but its
             heartbeat (written every 5s) is older than heartbeat_stale_after, so it hung.
             A dead wrapped command (a shim or launcher) never frees the slot: the wrapper
             waits for its command and releases in `finally`.
          2. Other locks (`acquire` mode, which has no process left to heartbeat): owner
             PID is dead AND lock age exceeds grace period (and heartbeat not fresh).
          3. Corrupt lock directory older than 10s grace period.
        Returns True if a stale lock was reclaimed, False otherwise.
        """
        effective_grace = (
            pid_dead_grace_period
            if pid_dead_grace_period is not None
            else self.pid_dead_grace_period
        )
        reclaimed_any = False

        for slot_idx, slot_dir in enumerate(self.slot_dirs):
            try:
                if not os.path.isdir(slot_dir):
                    continue

                info = self._read_slot_info(slot_idx)
                if info is None:
                    continue

                now = time.time()
                is_stale = False
                reason = ""

                if info.get("corrupt"):
                    # The read's dir mtime, not a second stat: the verdict must describe the
                    # same lock _tombstone_stale_slot checks its tombstone against.
                    age = now - info.get("acquired_at_epoch", now)
                    if age > 10.0:  # grace period for mid-creation
                        is_stale = True
                        reason = f"corrupt or incomplete lock directory (age={age:.1f}s, slot {slot_idx})"
                else:
                    pid = info.get("pid", 0)
                    wrapper_pid = info.get("wrapper_pid")
                    owner = info.get("owner", "unknown")
                    acquired_epoch = info.get("acquired_at_epoch")
                    if acquired_epoch is None:
                        try:
                            iso_str = info.get("acquired_at", "")
                            acquired_epoch = datetime.datetime.fromisoformat(iso_str).timestamp()
                        except Exception:
                            acquired_epoch = now

                    age = max(0.0, now - acquired_epoch)

                    # Check heartbeat recency if available
                    hb_epoch = info.get("heartbeat_at_epoch")
                    if hb_epoch is None and info.get("heartbeat_at"):
                        try:
                            hb_epoch = datetime.datetime.fromisoformat(info["heartbeat_at"]).timestamp()
                        except Exception:
                            hb_epoch = None
                    hb_age = (now - hb_epoch) if hb_epoch is not None else None
                    is_hb_fresh = (hb_age is not None and hb_age < effective_grace)
                    if wrapper_pid:
                        if self.is_pid_alive(wrapper_pid):
                            silence = hb_age if hb_age is not None else age
                            if silence >= heartbeat_stale_after:
                                is_stale = True
                                reason = (
                                    f"run wrapper PID {wrapper_pid} is alive but its heartbeat is stale "
                                    f"({silence:.1f}s >= {heartbeat_stale_after:.1f}s, "
                                    f"owner='{owner}', slot {slot_idx})"
                                )
                        elif age >= effective_grace and not is_hb_fresh:
                            is_stale = True
                            reason = (
                                f"run wrapper PID {wrapper_pid} is dead and lock age exceeds grace period "
                                f"({age:.1f}s >= {effective_grace:.1f}s, owner='{owner}', slot {slot_idx})"
                            )
                    elif pid > 0 and not self.is_pid_alive(pid):
                        # Never reclaim a lock younger than the grace period (e.g. 60s),
                        # or whose heartbeat is fresh (< 60s).
                        if age < effective_grace or is_hb_fresh:
                            logger.debug(
                                "Owner PID %d is dead for owner '%s' (slot %d) but lock is protected by grace period "
                                "(age=%.1fs < %.1fs, hb_fresh=%s); not reclaiming",
                                pid, owner, slot_idx, age, effective_grace, is_hb_fresh,
                            )
                        else:
                            is_stale = True
                            reason = (
                                f"owner PID {pid} is dead and lock age exceeds grace period "
                                f"({age:.1f}s >= {effective_grace:.1f}s, owner='{owner}', slot {slot_idx})"
                            )

                if is_stale and self._tombstone_stale_slot(slot_idx, info, reason):
                    reclaimed_any = True
            except (FileNotFoundError, OSError):
                pass
            except Exception as e:
                logger.warning("Error checking stale lock on slot %d: %s", slot_idx, e)

        return reclaimed_any

    def acquire(
        self,
        name: str,
        timeout: Optional[float] = None,
        heartbeat_stale_after: float = DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        force: bool = False,
        pid: Optional[int] = None,
        token: Optional[str] = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        queue_stale_heartbeat_after: Optional[float] = None,
        priority: bool = False,
    ) -> bool:
        """
        Acquires the build slot lock for 'name'.
        Blocks with poll_interval until acquired, or until timeout.
        Returns True on success, raises or returns False on failure.
        """
        if pid is None:
            pid = os.getpid()
        explicit_token = token is not None
        if token is None:
            token = str(uuid.uuid4())
        effective_heartbeat_threshold = (
            queue_stale_heartbeat_after
            if queue_stale_heartbeat_after is not None
            else self.queue_stale_heartbeat_after
        )
        if poll_interval >= effective_heartbeat_threshold:
            raise ValueError(
                f"poll_interval ({poll_interval}s) must be less than "
                f"queue_stale_heartbeat_after ({effective_heartbeat_threshold}s)"
            )
        # 1. RAM Guard notice. High RAM never refuses here: the caller is enqueued below and
        #    step 3 of the queue loop waits until RAM drops (or the timeout expires).
        ram_pct = get_system_ram_percent()
        if ram_pct is not None and ram_pct >= self.ram_guard_threshold:
            if force:
                notice = (
                    f"[NOTICE] RAM guard overridden with --force: system RAM is at {ram_pct:.1f}% "
                    f"(>= {self.ram_guard_threshold:.1f}% limit)."
                )
                print(notice, file=sys.stderr)
                logger.warning(notice)
            else:
                msg = (
                    f"RAM guard: RAM at {ram_pct:.1f}% (>= {self.ram_guard_threshold:.1f}%), "
                    f"'{name}' stays queued and waits"
                )
                print(msg, file=sys.stderr)
                logger.warning(msg)

        start_time = time.time()
        last_heartbeat = start_time
        acquired = False

        try:
            # 2. Register in FIFO Queue inside try so finally always cleans up
            self.enqueue(name, pid, token=token, stale_heartbeat_after=effective_heartbeat_threshold, priority=priority)

            while True:
                # Update heartbeat first if due (every <= 15s)
                now = time.time()
                if now - last_heartbeat >= heartbeat_interval:
                    try:
                        hb_ok = self.heartbeat(token=token, name=name, pid=pid)
                        if hb_ok:
                            last_heartbeat = now
                        else:
                            # Verify if the entry is ACTUALLY missing from the queue before logging and re-enqueuing
                            queue_snapshot = self._read_queue()
                            is_in_queue = any(
                                (token and item.get("token") == token)
                                or (not token and item.get("name") == name and (pid is None or item.get("pid") == pid))
                                for item in queue_snapshot
                            )
                            if not is_in_queue:
                                notice = (
                                    f"[NOTICE] Queue entry for '{name}' (PID {pid}, token {token}) "
                                    f"was missing during heartbeat; re-enqueuing."
                                )
                                print(notice, file=sys.stderr)
                                logger.warning(notice)
                                self.enqueue(
                                    name, pid, token=token, stale_heartbeat_after=effective_heartbeat_threshold, priority=priority
                                )
                                last_heartbeat = now
                            else:
                                logger.warning(
                                    "Heartbeat write not persisted for '%s', but entry is still present; will retry next tick",
                                    name,
                                )
                    except Exception as e:
                        logger.warning("Queue heartbeat error for '%s': %s (will retry next tick)", name, e)

                # Check and reclaim any stale lock
                try:
                    self.check_stale_and_reclaim(heartbeat_stale_after=heartbeat_stale_after)
                except Exception as e:
                    logger.debug("Transient error checking stale lock for '%s': %s", name, e)

                # Clean dead PIDs / stale heartbeats from queue
                try:
                    queue = self.clean_queue(stale_heartbeat_after=effective_heartbeat_threshold)
                except (TimeoutError, PermissionError, OSError) as e:
                    logger.warning("Queue error during clean_queue for '%s': %s (will retry next tick)", name, e)
                    if timeout is not None:
                        elapsed = time.time() - start_time
                        if elapsed >= timeout:
                            msg = f"Timed out after {timeout:.1f}s waiting for build slot lock (lane '{name}', PID {pid})"
                            print(msg, file=sys.stderr)
                            logger.error(msg)
                            return False
                    time.sleep(min(poll_interval, heartbeat_interval))
                    continue
                except Exception as e:
                    logger.warning("Unexpected error during clean_queue for '%s': %s (will retry next tick)", name, e)
                    time.sleep(min(poll_interval, heartbeat_interval))
                    continue

                # 3. Dynamic RAM evaluation at acquisition
                curr_ram = get_system_ram_percent()
                if curr_ram is not None and curr_ram >= self.ram_guard_threshold and not force:
                    # System RAM is >= 85%, refuse acquisition until it drops
                    if timeout is not None:
                        elapsed = time.time() - start_time
                        if elapsed >= timeout:
                            msg = (
                                f"Timed out after {timeout:.1f}s waiting for build slot lock: "
                                f"system RAM remains at {curr_ram:.1f}% (>= {self.ram_guard_threshold:.1f}%)"
                            )
                            print(msg, file=sys.stderr)
                            logger.error(msg)
                            return False
                    time.sleep(min(poll_interval, heartbeat_interval))
                    continue

                max_slots = self.get_max_slots(curr_ram)
                # Check currently held slots
                held_slot_indices = [
                    idx for idx, s_dir in enumerate(self.slot_dirs)
                    if os.path.isdir(s_dir)
                ]

                # Check if caller already holds one of the slots (re-entrant / idempotent)
                lane_already_held = False
                for idx in held_slot_indices:
                    info = self._read_slot_info(idx)
                    if info and info.get("owner") == name:
                        lane_already_held = True
                        if info.get("pid") == pid:
                            lock_token = info.get("token")
                            if not (explicit_token and lock_token and lock_token != token):
                                acquired = True
                                self.dequeue(name, pid, token=token)
                                msg = f"Build slot lock already held by '{name}' (PID {pid})"
                                print(msg)
                                return True
                        break

                if lane_already_held:
                    # Lane already holds a slot or token mismatch; cannot acquire another slot
                    if timeout is not None:
                        elapsed = time.time() - start_time
                        if elapsed >= timeout:
                            msg = f"Timed out after {timeout:.1f}s waiting for build slot lock (lane '{name}', PID {pid})"
                            print(msg, file=sys.stderr)
                            logger.error(msg)
                            return False
                    time.sleep(min(poll_interval, heartbeat_interval))
                    continue
                # Check capacity: how many slots can be acquired?
                currently_held_count = len(held_slot_indices)
                available_slots_count = max(0, max_slots - currently_held_count)

                if available_slots_count > 0:
                    # Find caller's position in FIFO queue
                    caller_idx = None
                    for i, item in enumerate(queue):
                        if token is not None and item.get("token") == token:
                            caller_idx = i
                            break
                        elif item.get("name") == name and item.get("pid") == pid:
                            caller_idx = i
                            break

                    is_eligible = (caller_idx is not None and caller_idx < available_slots_count) or (not queue)

                    if is_eligible:
                        # Attempt to acquire the first free slot within allowed max_slots
                        for slot_idx in range(min(max_slots, len(self.slot_dirs))):
                            slot_dir = self.slot_dirs[slot_idx]
                            if not os.path.isdir(slot_dir):
                                try:
                                    os.mkdir(slot_dir)
                                    # Atomic creation succeeded! We own slot_idx.
                                    self._write_slot_info(slot_idx, owner=name, pid=pid, token=token)
                                    acquired = True
                                    try:
                                        self.dequeue(name, pid, token=token)
                                    except TimeoutError as e:
                                        logger.warning("Queue lock timeout during dequeue after acquisition for '%s': %s", name, e)
                                    except Exception as e:
                                        logger.warning("Queue error during dequeue: %s", e)
                                    msg = f"Acquired build slot lock for '{name}' (PID {pid})"
                                    print(msg)
                                    logger.info(msg)
                                    return True
                                except (FileExistsError, PermissionError):
                                    # Lost race to another lane on this slot, try next free slot if available
                                    continue
                                except OSError as e:
                                    logger.warning("os.mkdir failed for slot %d: %s", slot_idx, e)
                                    break

                # Check timeout
                if timeout is not None:
                    elapsed = time.time() - start_time
                    if elapsed >= timeout:
                        msg = f"Timed out after {timeout:.1f}s waiting for build slot lock (lane '{name}', PID {pid})"
                        print(msg, file=sys.stderr)
                        logger.error(msg)
                        return False

                time.sleep(min(poll_interval, heartbeat_interval))
        finally:
            # If we exited without holding the lock, remove self from queue
            if not acquired:
                try:
                    self.dequeue(name, pid, token=token)
                except Exception as e:
                    logger.warning("Failed to dequeue on cleanup: %s", e)
    def is_held_by(self, name: str, pid: Optional[int] = None, token: Optional[str] = None) -> bool:
        """Returns True if any slot lock is held by 'name' (and optionally pid / token)."""
        for slot_idx, slot_dir in enumerate(self.slot_dirs):
            if not os.path.isdir(slot_dir):
                continue
            info = self._read_slot_info(slot_idx)
            if not info:
                continue
            if info.get("owner") != name:
                continue
            if pid is not None and info.get("pid") != pid:
                continue
            if token is not None and info.get("token") and info.get("token") != token:
                continue
            return True
        return False

    def release(self, name: str, token: Optional[str] = None) -> bool:
        """
        Releases the build slot lock held by 'name'.
        Checks all slots; releases matching slot(s).
        Refuses if all slots are held by other owners.
        Returns True if released or already free; returns False if non-owner refused.
        """
        held_slots = []
        for slot_idx, slot_dir in enumerate(self.slot_dirs):
            if os.path.isdir(slot_dir):
                info = self._read_slot_info(slot_idx)
                owner = info.get("owner", "unknown") if info else "unknown"
                pid = info.get("pid", 0) if info else 0
                tok = info.get("token") if info else None
                held_slots.append((slot_idx, slot_dir, owner, pid, tok))

        # Check if any slot is held by this owner (matching token if provided)
        matching_slots = [
            (idx, s_dir) for idx, s_dir, owner, pid, tok in held_slots
            if owner == name and (token is None or not tok or tok == token)
        ]

        if matching_slots:
            for idx, s_dir in matching_slots:
                info_path = os.path.join(s_dir, INFO_FILE_NAME)
                try:
                    if os.path.isfile(info_path):
                        os.unlink(info_path)
                except Exception:
                    pass
                try:
                    os.rmdir(s_dir)
                except Exception:
                    shutil.rmtree(s_dir, ignore_errors=True)
                msg = f"Released build slot lock for '{name}'"
                print(msg)
                logger.info(msg)
            self.dequeue(name, token=token)
            return True

        if not held_slots:
            # Already free; remove name from queue if lingering
            self.dequeue(name, token=token)
            msg = f"Build slot lock is already free (release called for '{name}')"
            print(msg)
            return True

        # Held, but token mismatch or non-owner
        for idx, s_dir, owner, pid, tok in held_slots:
            if owner == name and token is not None and tok and tok != token:
                msg = (
                    f"ERROR: Refusing to release build slot lock: token mismatch for "
                    f"'{name}' (held token '{tok}', release requested for '{token}')."
                )
                print(msg, file=sys.stderr)
                logger.error(msg)
                return False

        other_owners = ", ".join(f"'{o}' (PID {p})" for _, _, o, p, _ in held_slots)
        msg = f"ERROR: Refusing to release build slot lock: currently held by {other_owners}, not '{name}'."
        print(msg, file=sys.stderr)
        logger.error(msg)
        return False
    def status(self, heartbeat_stale_after: float = DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS) -> Dict[str, Any]:
        """
        Returns full status dictionary and prints summary.
        Reclaims stale locks with a logged notice.
        Shows all slot holders (up to 2 slots).
        """
        # 1. Reclaim stale lock if present across all slots
        reclaimed = self.check_stale_and_reclaim(heartbeat_stale_after=heartbeat_stale_after)

        # 2. Read lock info for all slots
        now = time.time()
        slots_status = []
        primary_lock_status: Optional[Dict[str, Any]] = None

        for slot_idx, slot_dir in enumerate(self.slot_dirs):
            info = self._read_slot_info(slot_idx)
            s_stat: Dict[str, Any] = {
                "slot": slot_idx,
                "slot_dir": slot_dir,
                "locked": False,
                "owner": None,
                "pid": None,
                "token": None,
                "acquired_at": None,
                "age_seconds": None,
                "pid_alive": None,
                "child_pid": None,
                "heartbeat_at": None,
            }
            if info and os.path.isdir(slot_dir):
                s_stat["locked"] = True
                s_stat["owner"] = info.get("owner")
                s_stat["pid"] = info.get("pid")
                s_stat["token"] = info.get("token")
                s_stat["acquired_at"] = info.get("acquired_at")
                s_stat["child_pid"] = info.get("child_pid")
                s_stat["heartbeat_at"] = info.get("heartbeat_at")

                acquired_epoch = info.get("acquired_at_epoch")
                if acquired_epoch is None:
                    try:
                        iso_str = info.get("acquired_at", "")
                        acquired_epoch = datetime.datetime.fromisoformat(iso_str).timestamp()
                    except Exception:
                        acquired_epoch = now
                age = max(0.0, now - acquired_epoch)
                s_stat["age_seconds"] = round(age, 1)

                pid = info.get("pid", 0)
                s_stat["pid_alive"] = self.is_pid_alive(pid) if pid > 0 else False

                if primary_lock_status is None:
                    primary_lock_status = s_stat

            slots_status.append(s_stat)

        if primary_lock_status is None:
            primary_lock_status = slots_status[0]

        # 3. Clean queue and read
        try:
            queue = self.clean_queue()
        except Exception as e:
            logger.warning("clean_queue failed in status: %s; reading queue directly", e)
            queue = self._read_queue()
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
                "priority": bool(item.get("priority", False)),
                "enqueued_at": item.get("enqueued_at_iso"),
                "wait_seconds": round(wait_time, 1),
                "heartbeat_at": item.get("heartbeat_at_iso"),
                "heartbeat_age_seconds": hb_age,
            })

        # 4. System RAM and dynamic capacity
        ram_pct = get_system_ram_percent()
        max_slots = self.get_max_slots(ram_pct)
        active_slots = sum(1 for s in slots_status if s["locked"])
        holders = [s["owner"] for s in slots_status if s["locked"] and s.get("owner")]

        result = {
            "lock": primary_lock_status,
            "slots": slots_status,
            "max_slots": max_slots,
            "active_slots": active_slots,
            "holders": holders,
            "queue": queue_status,
            "queue_depth": len(queue_status),
            "ram_percent": ram_pct,
            "ram_two_slot_threshold": self.ram_two_slot_threshold,
            "ram_guard_threshold": self.ram_guard_threshold,
            "stale_reclaimed_in_status": reclaimed,
            "run_dir": self.run_dir,
            "lock_dir": self.lock_dir,
        }
        return result
    def prep_cache(self, worktree: str, cache_dir: Optional[str] = None) -> str:
        """
        Links <worktree>/frontend/.next/cache to shared next-cache directory.
        Returns the resolved link path.
        """
        worktree_abs = os.path.abspath(worktree)
        shared_cache = os.path.abspath(cache_dir or os.path.join(self.run_dir, "next-cache"))
        os.makedirs(shared_cache, exist_ok=True)

        frontend_next = os.path.join(worktree_abs, "frontend", ".next")
        os.makedirs(frontend_next, exist_ok=True)

        link_path = os.path.join(frontend_next, "cache")
        if os.path.exists(link_path) or os.path.islink(link_path):
            try:
                if os.path.samefile(link_path, shared_cache):
                    logger.debug("Cache link already exists and points to shared cache: %s", link_path)
                    return link_path
            except Exception:
                pass
            _remove_dir_link(link_path)
            if os.path.isdir(link_path) and not os.path.islink(link_path):
                try:
                    for item in os.listdir(link_path):
                        s = os.path.join(link_path, item)
                        d = os.path.join(shared_cache, item)
                        if os.path.isdir(s):
                            shutil.copytree(s, d, dirs_exist_ok=True)
                        else:
                            shutil.copy2(s, d)
                except Exception as e:
                    logger.debug("Error merging existing cache into shared cache: %s", e)
                shutil.rmtree(link_path, ignore_errors=True)

        _create_dir_link(shared_cache, link_path)
        logger.info("Linked %s -> %s", link_path, shared_cache)
        return link_path

    def unprep_cache(self, worktree: str) -> bool:
        """
        Removes the frontend/.next/cache junction/link safely without deleting
        the shared cache contents.
        """
        worktree_abs = os.path.abspath(worktree)
        link_path = os.path.join(worktree_abs, "frontend", ".next", "cache")
        return _remove_dir_link(link_path)

    def check_ram(self, threshold: float = 90.0) -> Tuple[bool, Optional[float]]:
        """
        Checks if system RAM is below the threshold percentage.
        Returns (is_under_threshold, current_ram_percent).
        """
        ram_pct = get_system_ram_percent()
        if ram_pct is None:
            return True, None
        return (ram_pct < threshold), ram_pct

    def run_command(
        self,
        name: str,
        cmd: List[str],
        timeout: Optional[float] = None,
        priority: bool = False,
        force: bool = False,
        cwd: Optional[str] = None,
        heartbeat_stale_after: float = DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> int:
        """
        Executes a command under the exclusive build slot lock.
        Holds the lock ONLY for the duration of the command, and guarantees
        release upon command completion or failure.
        Records the wrapper PID (this process, the lock's liveness source), the wrapped
        child's PID, a per-run token, and maintains a heartbeat. The lock is reclaimed
        by others only once this wrapper is dead (after the grace period), or when it
        stops heartbeating for longer than heartbeat_stale_after.
        Returns the command exit code, or 1 if lock could not be acquired.
        """
        run_token = str(uuid.uuid4())
        runner_pid = os.getpid()

        try:
            acquired = self.acquire(
                name=name,
                timeout=timeout,
                heartbeat_stale_after=heartbeat_stale_after,
                poll_interval=poll_interval,
                force=force,
                pid=runner_pid,
                token=run_token,
                priority=priority,
            )
        except Exception as e:
            print(f"[RUN] Failed to acquire build slot lock for '{name}': {e}", file=sys.stderr)
            logger.error("Failed to acquire build slot lock for '%s': %s", name, e)
            return 1

        if not acquired:
            print(f"[RUN] Failed to acquire build slot lock for '{name}'.", file=sys.stderr)
            return 1

        cmd_display = " ".join(cmd)
        print(f"[RUN] Acquired build slot lock for '{name}'. Executing command: {cmd_display}", file=sys.stderr)

        stop_heartbeat = threading.Event()
        proc = None
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, shell=(sys.platform == "win32"))
            child_pid = proc.pid

            # Record wrapper and child PIDs and the initial heartbeat in lock info
            self._record_run_child(name=name, wrapper_pid=runner_pid, child_pid=child_pid, token=run_token)

            # Start background heartbeat while child runs
            def _heartbeat_worker():
                while not stop_heartbeat.wait(5.0):
                    try:
                        self.heartbeat_lock(name=name, token=run_token)
                    except Exception:
                        pass

            hb_thread = threading.Thread(target=_heartbeat_worker, daemon=True)
            hb_thread.start()

            ret = proc.wait()
            return ret
        except Exception as e:
            print(f"[RUN] Error running command for '{name}': {e}", file=sys.stderr)
            logger.error("Error executing command in run_command: %s", e)
            return 1
        finally:
            stop_heartbeat.set()
            print(f"[RUN] Releasing build slot lock for '{name}'...", file=sys.stderr)
            try:
                self.release(name=name, token=run_token)
            except Exception as e:
                logger.warning("Error releasing lock for '%s': %s", name, e)


def format_status_human(stat: Dict[str, Any]) -> str:
    """Formats status dictionary for terminal display showing all slot holders."""
    lines = []
    lines.append("=== Build Slot Arbiter Status ===")
    slots = stat.get("slots", [])
    if not slots and stat.get("lock"):
        slots = [stat["lock"]]

    max_slots = stat.get("max_slots", 1)
    two_slot_thresh = stat.get("ram_two_slot_threshold", 75.0)
    lines.append(f"Capacity:    {max_slots} slot(s) allowed (2 if RAM < {two_slot_thresh:.0f}%, 1 otherwise)")

    holders = [s for s in slots if s.get("locked")]
    if holders:
        lines.append(f"Status:      LOCKED ({len(holders)}/{len(slots)} in use)")
        lock = stat.get("lock", {})
        if lock.get("owner"):
            lines.append(f"Owner:       {lock.get('owner')}")
            alive_str = "alive" if lock.get("pid_alive") else "DEAD"
            lines.append(f"PID:         {lock.get('pid')} ({alive_str})")
        for s in slots:
            idx = s.get("slot", 0)
            if s.get("locked"):
                age = s.get("age_seconds", 0)
                alive_str = "alive" if s.get("pid_alive") else "DEAD"
                lines.append(
                    f"  Slot {idx}:   LOCKED by '{s.get('owner')}' (PID {s.get('pid')}, {alive_str}, age: {age:.1f}s)"
                )
            else:
                lines.append(f"  Slot {idx}:   FREE (unlocked)")
    else:
        lines.append("Status:      FREE (unlocked)")
        for s in slots:
            idx = s.get("slot", 0)
            lines.append(f"  Slot {idx}:   FREE (unlocked)")

    ram = stat.get("ram_percent")
    ram_str = f"{ram:.1f}%" if ram is not None else "unavailable"
    guard_thresh = stat.get("ram_guard_threshold", 85.0)
    ram_status = f" (ELEVATED >= {guard_thresh:.0f}%)" if (ram is not None and ram >= guard_thresh) else " (OK)"
    lines.append(f"System RAM:  {ram_str}{ram_status}")

    queue = stat.get("queue", [])
    lines.append(f"FIFO Queue:  {len(queue)} waiting")
    for i, q in enumerate(queue):
        hb_age = q.get("heartbeat_age_seconds")
        hb_str = f", hb: {hb_age:.1f}s ago" if hb_age is not None else ""
        prio_str = " [PRIORITY]" if q.get("priority") else ""
        lines.append(f"  [{i + 1}] {q.get('name')} (PID {q.get('pid')}, waiting {q.get('wait_seconds', 0):.1f}s{hb_str}){prio_str}")
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

    # acquire <name> [--timeout SEC] [--heartbeat-stale-after SEC] [--poll-interval SEC] [--force]
    p_acq = subparsers.add_parser("acquire", help="Acquire build slot lock (blocks until available)")
    p_acq.add_argument("name", help="Lane or worker identifier requesting the slot")
    p_acq.add_argument("--pid", type=int, default=None, help="Explicit PID to associate with the lock (default: parent process PID)")
    p_acq.add_argument("--timeout", type=float, default=None, help="Maximum seconds to wait (default: block indefinitely)")
    p_acq.add_argument(
        "--heartbeat-stale-after",
        type=float,
        default=DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS,
        help=(
            "Seconds without a heartbeat after which a live `run` holder counts as hung and is "
            f"reclaimed (default: {DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS}s / 5m); live holders are never reclaimed on age"
        ),
    )
    p_acq.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=f"Seconds to sleep between retry polls (default: {DEFAULT_POLL_INTERVAL_SECONDS}s)",
    )
    p_acq.add_argument(
        "--token",
        default=None,
        help="Explicit token for queue/lock disambiguation (default: auto-generated uuid4)",
    )
    p_acq.add_argument(
        "--force",
        action="store_true",
        help="Bypass the RAM guard (proceed even if system RAM >= 85%%)",
    )
    p_acq.add_argument(
        "--priority",
        action="store_true",
        help="Queue ahead of non-priority waiters, after earlier priority waiters (FIFO among priority)",
    )

    # bump <name> [--token TOKEN]
    p_bump = subparsers.add_parser(
        "bump", help="Mark a queued lane as priority (FIFO among priority waiters, ahead of normal ones)"
    )
    p_bump.add_argument("name", help="Lane or worker identifier to mark as priority")
    p_bump.add_argument("--token", default=None, help="Optional token matching the queued entry")

    # prep-cache <worktree> [--cache-dir DIR]
    p_cache = subparsers.add_parser("prep-cache", help="Link worktree frontend/.next/cache to shared next-cache directory")
    p_cache.add_argument("worktree", help="Path to worktree root directory")
    p_cache.add_argument("--cache-dir", default=None, help="Path to shared cache directory (default: <run-dir>/next-cache)")

    # unprep-cache <worktree>
    p_uncache = subparsers.add_parser("unprep-cache", help="Remove frontend/.next/cache junction without deleting shared cache contents")
    p_uncache.add_argument("worktree", help="Path to worktree root directory")

    # check-ram [--threshold PERCENT]
    p_ram = subparsers.add_parser("check-ram", help="Check system RAM percentage against threshold (default: 90%%)")
    p_ram.add_argument("--threshold", type=float, default=90.0, help="RAM percentage threshold (default: 90.0)")
    p_ram.add_argument("--json", action="store_true", help="Output RAM status as JSON")

    # run <name> [--timeout SEC] [--priority] [--force] [--cwd DIR] [--heartbeat-stale-after SEC] -- <cmd...>
    p_run = subparsers.add_parser(
        "run",
        help="Run a build command under the build slot lock, automatically releasing on exit",
    )
    p_run.add_argument("name", help="Lane or worker identifier requesting the slot")
    _add_run_options(p_run)
    p_run.add_argument("cmd", nargs=argparse.REMAINDER, help="Command and arguments to execute under the lock (use -- before command)")

    # release <name>
    p_rel = subparsers.add_parser("release", help="Release build slot lock (refused if not owner)")
    p_rel.add_argument("name", help="Lane or worker identifier releasing the slot")

    # status [--json]
    p_stat = subparsers.add_parser("status", help="Print current lock owner, age, and FIFO queue")
    p_stat.add_argument(
        "--heartbeat-stale-after",
        type=float,
        default=DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS,
        help=(
            "Seconds without a heartbeat after which a live `run` holder is reclaimed during "
            f"the status check (default: {DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS}s / 5m)"
        ),
    )
    p_stat.add_argument("--json", action="store_true", help="Output status as structured JSON")

    args = parser.parse_args(argv)
    if args.command == "acquire" and args.poll_interval >= DEFAULT_QUEUE_STALE_HEARTBEAT_SECONDS:
        parser.error(
            f"--poll-interval ({args.poll_interval}s) must be less than "
            f"queue_stale_heartbeat_after ({DEFAULT_QUEUE_STALE_HEARTBEAT_SECONDS}s)"
        )
    if args.command == "run" and args.cmd and args.cmd[0].startswith("-") and args.cmd[0] != "--" and "--" in args.cmd:
        # REMAINDER swallows run options written after <name> (`run <name> --timeout 60 -- <cmd>`);
        # parse the tokens before the `--` separator as run options.
        sep = args.cmd.index("--")
        tail_parser = argparse.ArgumentParser(prog="build_slot.py run <name>")
        _add_run_options(tail_parser)
        tail_parser.parse_args(args.cmd[:sep], namespace=args)
        args.cmd = args.cmd[sep:]
    return args


def _add_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=float, default=None, help="Maximum seconds to wait to acquire slot")
    parser.add_argument(
        "--priority",
        action="store_true",
        help="Queue ahead of non-priority waiters, after earlier priority waiters (FIFO among priority)",
    )
    parser.add_argument("--force", action="store_true", help="Bypass RAM guard during acquisition")
    parser.add_argument("--cwd", default=None, help="Working directory to execute command in (default: current directory)")
    parser.add_argument(
        "--heartbeat-stale-after",
        type=float,
        default=DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS,
        help=(
            "Seconds without a heartbeat after which a live `run` holder counts as hung and is "
            f"reclaimed while this run waits (default: {DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS}s / 5m)"
        ),
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    manager = BuildSlotManager(run_dir=args.run_dir)

    if args.command == "acquire":
        caller_pid = args.pid
        if caller_pid is None:
            caller_pid = find_long_lived_owner_pid()
        success = manager.acquire(
            name=args.name,
            timeout=args.timeout,
            heartbeat_stale_after=args.heartbeat_stale_after,
            poll_interval=args.poll_interval,
            force=args.force,
            pid=caller_pid,
            token=args.token,
            priority=args.priority,
        )
        return 0 if success else 1

    elif args.command == "bump":
        success = manager.bump(name=args.name, token=args.token)
        if success:
            print(f"[BUMP] Marked '{args.name}' as priority in the build slot queue.", file=sys.stderr)
            return 0
        else:
            print(f"[BUMP] Entry '{args.name}' not found in build slot queue.", file=sys.stderr)
            return 1

    elif args.command == "prep-cache":
        try:
            link = manager.prep_cache(worktree=args.worktree, cache_dir=args.cache_dir)
            print(f"[PREP-CACHE] Linked {link} -> shared next-cache")
            return 0
        except Exception as e:
            print(f"[ERROR] Failed to prep-cache for '{args.worktree}': {e}", file=sys.stderr)
            return 1

    elif args.command == "unprep-cache":
        try:
            ok = manager.unprep_cache(worktree=args.worktree)
            if ok:
                print(f"[UNPREP-CACHE] Removed cache junction for '{args.worktree}'")
            else:
                print(f"[UNPREP-CACHE] No cache junction found for '{args.worktree}'")
            return 0
        except Exception as e:
            print(f"[ERROR] Failed to unprep-cache for '{args.worktree}': {e}", file=sys.stderr)
            return 1

    elif args.command == "check-ram":
        ok, ram_pct = manager.check_ram(threshold=args.threshold)
        ram_str = f"{ram_pct:.1f}%" if ram_pct is not None else "unavailable"
        if args.json:
            print(json.dumps({"ok": ok, "ram_percent": ram_pct, "threshold": args.threshold}, indent=2))
        else:
            if ok:
                print(f"[OK] System RAM is {ram_str} (under {args.threshold:.1f}% limit). Safe to proceed.")
            else:
                print(f"[WARNING] System RAM is {ram_str} (>= {args.threshold:.1f}% limit). Hold parallel startup.", file=sys.stderr)
        return 0 if ok else 1

    elif args.command == "run":
        cmd = args.cmd
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print("[ERROR] No command specified to run under build slot lock. Usage: build_slot.py run <name> -- <cmd...>", file=sys.stderr)
            return 1
        return manager.run_command(
            name=args.name,
            cmd=cmd,
            timeout=args.timeout,
            priority=args.priority,
            force=args.force,
            cwd=args.cwd,
            heartbeat_stale_after=args.heartbeat_stale_after,
        )

    elif args.command == "release":
        success = manager.release(name=args.name)
        return 0 if success else 1

    elif args.command == "status":
        stat = manager.status(heartbeat_stale_after=args.heartbeat_stale_after)
        if args.json:
            print(json.dumps(stat, indent=2))
        else:
            print(format_status_human(stat))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
