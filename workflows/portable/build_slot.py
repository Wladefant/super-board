#!/usr/bin/env python3
"""
Build & Browser Slot Arbiter (workflows/build_slot.py)

Arbitrates the exclusive build/browser slot ('next build', 'next start', dev Chromium)
across parallel lanes using an atomic directory lock and a FIFO queue file.

Replaces orchestrator IRC messages ('BUILD SLOT TAKEN/FREE') with a local,
Windows-safe (no fcntl), crash-resilient lock file.

Commands:
    acquire <name> [--timeout SEC] [--heartbeat-stale-after SEC] [--poll-interval SEC] [--force] [--next-dir DIR]
    run <name> [--timeout SEC] [--priority] [--force] [--cwd DIR] [--next-dir DIR] [--heartbeat-stale-after SEC] -- <cmd...>
    heartbeat <name>
    release <name>
    status [--json]

Invariants:
    - Atomic acquisition via os.mkdir('~/.veyyon/run/build-slot.lock').
    - Lock metadata contains owner, pid, token, and acquired_at timestamp.
    - Waiting lanes are tracked in a FIFO queue file ('~/.veyyon/run/build-slot.queue.json')
      with unique tokens to differentiate in-process waiters sharing a parent PID.
    - Queue token heartbeats expire after 60s even with a live shared PID.
      Dead PIDs expire regardless of heartbeat freshness. Only entries without
      any heartbeat use the 1800s enqueue-age fallback.
    - Acquire wait loops write heartbeats before queue cleaning, re-enqueue preserving original
      enqueued_at if missing during heartbeat validation, and clean up queue entries via try/finally.
    - Queue operations are protected by the short-lived directory lock ('build-slot-queue.lock');
      stale queue locks are reclaimed if the owner PID is dead or if a live holder keeps it longer
      than 120s (default max hold), using token-verified tombstone reclamation. Releasers verify
      unique ownership tokens so an expired owner's late release cannot remove a successor's lock.
    - Queue critical sections maintain strict execution hygiene: no RAM probes, process waits
      (is_pid_alive), or sleep calls occur while holding the queue lock.
    - Queue reads retry on transient OS/JSON sharing errors and raise on failure; missing file
      returns empty list only on initial queue creation.
    - Release by non-owner is strictly refused.
    - Stale locks are reclaimed with a logged notice. A live `run` holder is never reclaimed for a
      stale heartbeat, however old: its wrapper process (`build_slot.py run`) waits for the command
      and releases in `finally`, so a dead wrapper is reclaimed immediately, a recycled wrapper PID
      is reclaimed by process creation identity, and a genuine live one keeps the slot.
      Heartbeats survive a foreign handle on info.json: if the replace fails, the heartbeat is
      written to heartbeat.json in the lock dir and readers fold it in; our own readers open
      files with FILE_SHARE_DELETE and close them before parsing (#690).
      Other locks (`acquire` mode has no process left to heartbeat): when the owner PID is
      dead, or when its token heartbeat is older than 30 min (#620). Without a heartbeat,
      the acquisition age supplies the 30-min limit. This bounded lease permits recovery
      of abandoned lanes but may reclaim a live process silent beyond its lease limit.
    - A reclaim renames the lock dir to a unique tombstone and deletes it only if the
      tombstone still holds the lock that was judged stale; otherwise it is put back. A
      reclaim never deletes a lock other than the one it judged stale.
    - The acquire-mode owner PID is the nearest veyyon session host (not its
      `__veyyon_worker*` helpers); the ancestor climb stops at a parent created after its
      child, since Windows reuses a dead parent's PID.
    - RAM admission charges held reservations during ramp-up (heavy 300s, medium
      120s, light/browser 60s), then uses available RAM alone, keeping a 3 GiB floor.
      Idle wait never bypasses guards. Force requires BUILD_SLOT_ALLOW_FORCE=1.
    - Admission ignores queue entries belonging to occupied lane names,
      preserving queued entries and FIFO among eligible waiters.
    - Enqueue rejects reservations above physical total RAM minus the free floor.
      Temporary RAM shortages stay queued. Priority heads still allow fitting backfill.
    - Operator build freeze: when 'build-freeze' exists in run_dir, acquire and run
      commands are refused immediately with exit 75 and 'build freeze active (<reason>)'
      printed to stderr (fallback 'reason unavailable' on read error). No queue entry
      is created and no command is launched; status and release remain unaffected.
    - Transition guards record PID, token and acquisition time in a .owner.json sidecar.
      The OS releases byte locks when a process exits. The next holder replaces stale
      metadata only after obtaining that same OS lock, never by deleting a live lock.
      Stale checks, detached cleanup, metadata retry waits and queue cleanup run outside
      the slot transition guard.
      Reclaim aborts if lock metadata changes while stale checks run.
    - Run children inherit BUILD_SLOT_HELD with the holder identity and reservation.
      Nested acquire/run reuse a live matching lock without another reservation.
      Requests above the holder's class or memory fail instead of upgrading it.
      Nested release leaves the holder's slot intact. Invalid markers do not bypass admission.
    - Standard library only. Windows uses msvcrt byte locks; POSIX uses flock.
"""

import argparse
import ctypes
import datetime
import json
import logging
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple
import uuid
from functools import wraps
import math
import signal
import atexit


_guard_state = threading.local()


@contextmanager
def _transition_guard(path: str, timeout: float = 5.0, cancel=None):
    """Serialize lock-directory transitions on a permanent OS-locked file."""
    path = os.path.abspath(path)
    pid = os.getpid()
    held = getattr(_guard_state, "held", None)
    if held is None or getattr(_guard_state, "pid", None) != pid:
        _guard_state.pid = pid
        held = _guard_state.held = set()
    if path in held:
        yield
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a+b") as guard:
        if os.fstat(guard.fileno()).st_size == 0:
            guard.write(b"\0")
            guard.flush()
        deadline = time.monotonic() + timeout
        while True:
            if cancel is not None:
                cancel()
            try:
                guard.seek(0)
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    try:
                        with open(path + ".owner.json", encoding="utf-8") as stream:
                            owner = json.load(stream)
                        age = max(0.0, time.time() - owner["acquired_at_epoch"])
                        detail = f" (holder PID {owner['pid']}, age {age:.1f}s, token {owner['token']})"
                    except (OSError, ValueError, KeyError, TypeError):
                        detail = " (holder metadata unavailable)"
                    raise TimeoutError(f"Timed out waiting for transition guard: {path}{detail}")
                time.sleep(min(_jittered(0.1), max(0.0, deadline - time.monotonic())))
        owner_path = path + ".owner.json"
        owner = {"pid": pid, "token": uuid.uuid4().hex, "acquired_at_epoch": time.time()}
        held.add(path)
        try:
            # Replace a dead holder's metadata only after obtaining its OS lock.
            with open(owner_path, "w", encoding="utf-8") as stream:
                json.dump(owner, stream)
            yield
        finally:
            try:
                os.unlink(owner_path)
            except OSError:
                pass
            held.remove(path)
            guard.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(guard.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(guard.fileno(), fcntl.LOCK_UN)


def _guard_queue_transition(fn):
    @wraps(fn)
    def guarded(queue_lock_dir, *args, **kwargs):
        with _transition_guard(queue_lock_dir + ".guard"):
            return fn(queue_lock_dir, *args, **kwargs)
    return guarded


def _guard_slot_transition(fn):
    @wraps(fn)
    def guarded(self, *args, **kwargs):
        try:
            with _transition_guard(os.path.join(self.run_dir, "build-slot.guard")):
                return fn(self, *args, **kwargs)
        finally:
            if not getattr(_guard_state, "held", set()):
                notices = getattr(_guard_state, "notices", [])
                _guard_state.notices = []
                for msg, level in notices:
                    _transition_notice(msg, level)
    return guarded


def _transition_notice(msg, level=None):
    """Never let a full output pipe stall a state transition."""
    if getattr(_guard_state, "held", set()):
        if not hasattr(_guard_state, "notices"):
            _guard_state.notices = []
        _guard_state.notices.append((msg, level))
        return
    print(msg, file=sys.stderr)
    if level:
        getattr(logger, level)(msg)


logger = logging.getLogger("build_slot")
logger.addHandler(logging.NullHandler())

DEFAULT_RUN_DIR = os.path.expanduser("~/.veyyon/run")
LOCK_DIR_NAME = "build-slot.lock"
DEFAULT_MAX_SLOTS = 8
SLOT_LOCK_DIR_NAMES = [
    "build-slot.lock",
    "build-slot-1.lock",
    "build-slot-2.lock",
    "build-slot-3.lock",
    "build-slot-4.lock",
    "build-slot-5.lock",
    "build-slot-6.lock",
    "build-slot-7.lock",
]
QUEUE_FILE_NAME = "build-slot.queue.json"
QUEUE_LOCK_NAME = "build-slot-queue.lock"
INFO_FILE_NAME = "info.json"
HEARTBEAT_FILE_NAME = "heartbeat.json"  # fallback heartbeat, written when info.json cannot be replaced
DEFAULT_ACQUIRE_HOLDER_STALE_SECONDS = 30 * 60  # a live-PID `acquire` holder silent this long was abandoned (#620)

DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS = 5 * 60  # legacy threshold; live wrapper is preserved by process identity
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 10.0  # update queue entry heartbeat every <=15s
DEFAULT_QUEUE_STALE_HEARTBEAT_SECONDS = 60.0  # reclaim if heartbeat older than 60s
DEFAULT_QUEUE_STALE_FALLBACK_SECONDS = 30 * 60  # 30 minutes fallback for legacy entries without heartbeat
DEFAULT_RAM_GUARD_THRESHOLD_PERCENT = 95.0
DEFAULT_ACQUISITION_STAGGER_SECONDS = 45.0
DEFAULT_RAM_GUARD_IDLE_ADMIT_SECONDS = 120.0  # retained constructor compatibility; no idle bypass
DEFAULT_RELEASE_TIMEOUT_SECONDS = 30.0
DEFAULT_QUEUE_LOCK_MAX_HOLD_SECONDS = 120.0  # a queue lock is held for milliseconds; older than 120s was leaked, even if its owner lives (#690)
DEFAULT_QUEUE_GRANT_CLEANUP_GRACE_SECONDS = 1.0  # a lane that already holds its slot spends at most this long on queue cleanup; clean_queue sweeps the rest
DEFAULT_QUEUE_CLEANUP_GRACE_SECONDS = 8.0  # queue cleanup after a slot is settled retries a busy queue lock this long; shorter than the release deadline
LAST_ACQUIRED_FILE_NAME = "last-acquired-at.json"
DEFAULT_PID_DEAD_GRACE_PERIOD_SECONDS = 60.0  # never reclaim a dead-PID lock younger than 60s

HEAVY_RESERVATION_GIB = 4.5
HEAVY_CONCURRENCY_RAM_THRESHOLD_PERCENT = 85.0
MEMORY_RESERVATIONS = {"heavy": HEAVY_RESERVATION_GIB, "medium": 1.5, "light": 0.5, "browser": 1.1}
MEMORY_RAMP_SECONDS = {"heavy": 300.0, "medium": 120.0, "light": 60.0, "browser": 60.0}
MEMORY_FLOOR_GIB = 3.0


def _reservation_gib(job_class: str, mem_gib: Optional[float] = None) -> float:
    heavy_minimum = HEAVY_RESERVATION_GIB if job_class == "heavy" else 5.0
    if job_class not in MEMORY_RESERVATIONS:
        return max(5.0, _reservation_gib("heavy", mem_gib)) if mem_gib is not None else 5.0
    value = MEMORY_RESERVATIONS[job_class] if mem_gib is None else mem_gib
    if not math.isfinite(value) or value <= 0:
        raise ValueError("mem_gib must be positive and finite")
    return max(value, heavy_minimum) if job_class == "heavy" else value


def second_heavy_fits(available_gib, new_reservation_gib, running_heavy_ramp_gib):
    """Preserve the free floor using current RAM and young heavy reservations."""
    values = (available_gib, new_reservation_gib, running_heavy_ramp_gib)
    return all(value is not None and math.isfinite(value) and value >= 0 for value in values) and (
        available_gib - new_reservation_gib - running_heavy_ramp_gib >= MEMORY_FLOOR_GIB
    )


def _heavy_limit(ram_percent):
    return 2 if (ram_percent is not None and math.isfinite(ram_percent)
                 and ram_percent < HEAVY_CONCURRENCY_RAM_THRESHOLD_PERCENT) else 1


def get_available_ram_gib() -> Optional[float]:
    """Available physical RAM, not swap. Unknown telemetry blocks admission."""
    override = os.environ.get("BUILD_SLOT_AVAILABLE_GIB")
    if override is not None:
        value = float(override)
        return value if math.isfinite(value) and value >= 0 else None
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in
                ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")
            ]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.available / (1024 ** 3)
    elif sys.platform.startswith("linux"):
        with open("/proc/meminfo", encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / (1024 ** 2)
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 ** 3)
    except Exception:
        return None


def get_total_ram_gib() -> Optional[float]:
    """Physical host capacity, independent of current available RAM or overrides."""
    try:
        if sys.platform == "win32":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in
                    ("total", "available", "page_total", "page_available",
                     "virtual_total", "virtual_available", "extended")
                ]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return status.total / (1024 ** 3)
        elif sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="utf-8") as stream:
                for line in stream:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / (1024 ** 2)
        import psutil
        return psutil.virtual_memory().total / (1024 ** 3)
    except (OSError, ValueError, AttributeError, ImportError):
        return None


def classify_command(cmd: List[str]) -> str:
    command = (cmd if isinstance(cmd, str) else " ".join(cmd)).lower().replace("\\", "/")
    if re.search(r"\bnext(?:\.cmd)?\s+(?:build|start)\b|\b(?:npm|pnpm|bun|yarn)\s+(?:run\s+)?(?:build|start)\b", command):
        return "heavy"
    if re.search(r"chrom(?:e|ium)|playwright|puppeteer", command):
        return "browser"
    if re.search(r"\b(?:tsc|vitest|wrangler|workerd)(?:\.cmd|\.exe)?\b", command):
        return "medium"
    return "light"


def _check_freeze(run_dir: str) -> None:
    path = os.path.join(run_dir, "build-freeze")
    if os.path.exists(path):
        try:
            reason = _read_file_bytes(path).decode("utf-8").strip()
        except OSError:
            reason = "reason unavailable"
        print(f"build freeze active ({reason})", file=sys.stderr)
        raise SystemExit(75)


class _WindowsChildJob:
    """A non-inherited handle kills descendants when the wrapper closes or dies."""
    def __init__(self, token):
        from ctypes import wintypes
        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                ("active_limit", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD),
            ]
        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_ulonglong * 6),
                       ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                       ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.kernel.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.handle = self.kernel.CreateJobObjectW(None, "Local\\build-slot-" + token)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error


    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


class _WindowsJobProcess:
    """Create the process inside its Job atomically, before any child can escape."""
    def __init__(self, cmd, cwd, env, job, stdin=subprocess.DEVNULL):
        from ctypes import wintypes
        class Startup(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
                ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
                ("x", wintypes.DWORD), ("y", wintypes.DWORD),
                ("xsize", wintypes.DWORD), ("ysize", wintypes.DWORD),
                ("xchars", wintypes.DWORD), ("ychars", wintypes.DWORD),
                ("fill", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("show", wintypes.WORD), ("reserved_size", wintypes.WORD),
                ("reserved_bytes", ctypes.c_void_p),
                ("stdin", wintypes.HANDLE), ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE),
            ]
        class StartupEx(ctypes.Structure):
            _fields_ = [("startup", Startup), ("attributes", ctypes.c_void_p)]
        class ProcessInfo(ctypes.Structure):
            _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                        ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]
        self.kernel = job.kernel
        kernel = self.kernel
        kernel.InitializeProcThreadAttributeList.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t))
        kernel.UpdateProcThreadAttribute.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
        kernel.DeleteProcThreadAttributeList.argtypes = (ctypes.c_void_p,)
        kernel.CreateProcessW.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                                         wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                                         ctypes.c_void_p, ctypes.POINTER(ProcessInfo))
        kernel.GetStdHandle.argtypes = (wintypes.DWORD,)
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        size = ctypes.c_size_t()
        kernel.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        attributes = ctypes.create_string_buffer(size.value)
        if not kernel.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        null_input = None
        try:
            handles = (wintypes.HANDLE * 1)(job.handle)
            if not kernel.UpdateProcThreadAttribute(attributes, 0, 0x2000D, handles, ctypes.sizeof(handles), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            startup = StartupEx()
            startup.startup.cb = ctypes.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            if stdin == subprocess.DEVNULL:
                import msvcrt
                null_input = open(os.devnull, "rb")
                input_handle = msvcrt.get_osfhandle(null_input.fileno())
                os.set_handle_inheritable(input_handle, True)
            else:
                input_handle = kernel.GetStdHandle(-10 & 0xFFFFFFFF)
            startup.startup.stdin = input_handle
            startup.startup.stdout = kernel.GetStdHandle(-11 & 0xFFFFFFFF)
            startup.startup.stderr = kernel.GetStdHandle(-12 & 0xFFFFFFFF)
            startup.attributes = ctypes.addressof(attributes)
            shell = os.environ.get("COMSPEC") or os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe")
            command = ctypes.create_unicode_buffer(f'{shell} /c "{subprocess.list2cmdline(cmd)}"')
            environment = ctypes.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(env.items())) + "\0\0")
            info = ProcessInfo()
            # EXTENDED_STARTUPINFO_PRESENT, CREATE_UNICODE_ENVIRONMENT, CREATE_NO_WINDOW
            if not kernel.CreateProcessW(shell, command, None, None, True, 0x80000 | 0x400 | 0x8000000,
                                         environment, cwd, ctypes.byref(startup), ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = info.process
            self.pid = info.pid
            self.returncode = None
            kernel.CloseHandle(info.thread)
        finally:
            kernel.DeleteProcThreadAttributeList(attributes)
            if null_input is not None:
                null_input.close()

    def poll(self):
        if self.returncode is None and self.kernel.WaitForSingleObject(self._handle, 0) == 0:
            code = ctypes.c_ulong()
            if not self.kernel.GetExitCodeProcess(self._handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.returncode = code.value
        return self.returncode

    def wait(self, timeout):
        result = self.kernel.WaitForSingleObject(self._handle, max(0, min(int(timeout * 1000), 0xFFFFFFFE)))
        if result == 258:
            raise subprocess.TimeoutExpired(str(self.pid), timeout)
        if result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        return self.poll()

    def kill(self):
        if not self.kernel.TerminateProcess(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        self.kernel.CloseHandle(self._handle)


def _windows_job_alive(token) -> bool:
    """Query the named job before releasing a crashed wrapper's reservation."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.OpenJobObjectW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenJobObjectW(4, False, "Local\\build-slot-" + token)
    if not handle:
        return ctypes.get_last_error() != 2
    class Accounting(ctypes.Structure):
        _fields_ = [("times", ctypes.c_longlong * 4), ("faults", wintypes.DWORD),
                    ("total", wintypes.DWORD), ("active", wintypes.DWORD), ("terminated", wintypes.DWORD)]
    info = Accounting()
    try:
        if not kernel.QueryInformationJobObject(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            return True
        return info.active > 0
    finally:
        kernel.CloseHandle(handle)


def _kill_child_tree(proc) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=30)

def _arm_deadline(seconds: float, what: str) -> threading.Timer:
    """
    Ends the process with exit code 124 if it is still running after `seconds`. A command
    stalled by a thrashing host must fail its caller, not hang it (#620). The caller
    cancels the returned timer when it finishes in time.
    """

    def _expire() -> None:
        print(f"ERROR: {what} timed out after {seconds:g}s", file=sys.stderr, flush=True)
        os._exit(124)

    timer = threading.Timer(seconds, _expire)
    timer.daemon = True
    timer.start()
    return timer


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


def _write_json_atomic(path: str, data: Any, prefix: str = ".info-", retry: bool = True) -> None:
    """
    Writes JSON so readers see the old or the new document, never a truncated one.
    Lock info is read by every waiter on every poll; an in-place rewrite let a reader
    hit the empty file, call the live lock corrupt and reclaim it (#315).
    The temp file is removed whether or not the replace succeeded.
    """
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        if retry:
            _replace_with_retry(tmp_path, path)
        else:
            os.replace(tmp_path, path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _read_file_bytes(path: str) -> bytes:
    """
    Reads a whole file and closes it before returning. On Windows the file is opened with
    FILE_SHARE_DELETE, which Python's open() never sets: a plain open() makes every
    os.replace of the file fail with WinError 5 for as long as the reader holds it, and
    waiters poll lock info all day (#690).
    A failed ReadFile raises its WinError immediately and closes the handle; it never
    returns partial data as EOF and never falls back to an unshared open() retry.
    """
    if sys.platform == "win32":
        try:
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateFileW.restype = wintypes.HANDLE
            k32.CreateFileW.argtypes = (
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
            )
            k32.ReadFile.argtypes = (
                wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
            )
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            GENERIC_READ, SHARE_ALL, OPEN_EXISTING = 0x80000000, 0x7, 3
            handle = k32.CreateFileW(path, GENERIC_READ, SHARE_ALL, None, OPEN_EXISTING, 0x80, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                err = ctypes.get_last_error()
                if err in (2, 3):
                    raise FileNotFoundError(2, "No such file", path)
                raise ctypes.WinError(err)
            chunks = []
            try:
                buf = ctypes.create_string_buffer(65536)
                got = wintypes.DWORD(0)
                while True:
                    ok = k32.ReadFile(handle, buf, 65536, ctypes.byref(got), None)
                    if not ok:
                        err = ctypes.get_last_error()
                        if err == 38:  # ERROR_HANDLE_EOF
                            break
                        raise ctypes.WinError(err)
                    if got.value == 0:
                        break
                    chunks.append(buf.raw[: got.value])
            finally:
                k32.CloseHandle(handle)
            return b"".join(chunks)
        except (FileNotFoundError, OSError):
            raise
    with open(path, "rb") as f:
        return f.read()


def _read_json_file(path: str) -> Any:
    """Reads the bytes, closes the file, then parses: no reader keeps a handle while parsing."""
    return json.loads(_read_file_bytes(path).decode("utf-8"))


def _merge_side_heartbeat(lock_dir: str, info: Dict[str, Any]) -> None:
    """
    Folds the fallback heartbeat file into info when it belongs to the same holding and is
    newer. A holder whose info.json replace keeps failing (a foreign handle on the file)
    still proves it is alive through this file and preserves its run mode metadata.
    """
    try:
        side = _read_json_file(os.path.join(lock_dir, HEARTBEAT_FILE_NAME))
    except Exception:
        return
    if not isinstance(side, dict) or side.get("token") != info.get("token"):
        return
    side_epoch = _parse_timestamp(side.get("heartbeat_at_epoch"))
    own_epoch = _parse_timestamp(info.get("heartbeat_at_epoch"))
    if side_epoch is not None and (own_epoch is None or side_epoch > own_epoch):
        info["heartbeat_at_epoch"] = side_epoch
        if side.get("heartbeat_at"):
            info["heartbeat_at"] = side["heartbeat_at"]
    for key in ("wrapper_pid", "child_pid", "wrapper_created_ticks", "wrapper_created_epoch"):
        if side.get(key) is not None and info.get(key) is None:
            info[key] = side[key]


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



def _get_process_create_ticks(pid: int) -> Optional[int]:
    """Returns creation time (FILETIME ticks) for a process, or None if unavailable."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        try:
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return None
            try:
                times = [wintypes.FILETIME() for _ in range(4)]
                if k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                    return (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return None
    return None


def _get_process_create_epoch(pid: int) -> Optional[float]:
    """
    Returns process creation time as epoch seconds, or None if unavailable.
    Does not use /proc/<pid> directory timestamps because filesystem mtime is
    not a dependable process creation identity and would cause genuine live owners
    to be mistaken for recycled processes. Reads native Windows ticks, psutil,
    or Linux /proc/<pid>/stat starttime with /proc/stat btime.
    """
    if pid <= 0:
        return None
    ticks = _get_process_create_ticks(pid)
    if ticks is not None:
        return (ticks - 116444736000000000) / 10000000.0
    try:
        import psutil  # type: ignore

        return float(psutil.Process(pid).create_time())
    except Exception:
        pass
    try:
        stat_path = f"/proc/{pid}/stat"
        if os.path.isfile(stat_path):
            with open(stat_path, "r", encoding="utf-8") as f:
                content = f.read()
            rparen = content.rfind(")")
            if rparen != -1:
                fields = content[rparen + 1:].split()
                if len(fields) > 19:
                    starttime_ticks = float(fields[19])
                    btime = None
                    if os.path.isfile("/proc/stat"):
                        with open("/proc/stat", "r", encoding="utf-8") as f:
                            for line in f:
                                if line.startswith("btime "):
                                    btime = float(line.split()[1])
                                    break
                    if btime is not None:
                        clk_tck = 100.0
                        if hasattr(os, "sysconf") and hasattr(os, "sysconf_names"):
                            try:
                                clk_tck = float(os.sysconf("SC_CLK_TCK"))
                            except (KeyError, ValueError, OSError):
                                clk_tck = 100.0
                        return btime + (starttime_ticks / clk_tck)
    except Exception:
        pass
    return None

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


def _read_lock_dir_info(lock_dir: str, slot_idx: int, retry: bool = True) -> Optional[Dict[str, Any]]:
    """Reads the info.json of a slot lock dir (or its tombstone); None if the dir is gone."""
    if not os.path.isdir(lock_dir):
        return None
    info_path = os.path.join(lock_dir, INFO_FILE_NAME)
    if not os.path.isfile(info_path):
        return _corrupt_lock_info(lock_dir, slot_idx)

    # Retry briefly: a holder still running an older copy rewrites info.json in place,
    # and Windows can refuse a read mid-replace. Only a persistent failure is corrupt.
    last_err: Optional[Exception] = None
    attempts = _INFO_READ_ATTEMPTS if retry else 1
    for attempt in range(attempts):
        try:
            data = _read_json_file(info_path)
            if isinstance(data, dict):
                data.setdefault("slot", slot_idx)
            if isinstance(data, dict) and not data.get("corrupt"):
                _merge_side_heartbeat(lock_dir, data)
            return data
        except Exception as e:
            last_err = e
            if retry and attempt < attempts - 1:
                time.sleep(_INFO_READ_RETRY_DELAY)
    _transition_notice(f"Failed to read lock info for slot {slot_idx}: {last_err}", "warning")
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
        return _read_json_file(info_file)
    except Exception:
        return None


def _jittered(delay: float) -> float:
    """Spreads waiters out so processes polling one lock do not retry in step."""
    return delay * (0.5 + random.random())


def _clean_detached_lock_dir(target_dir: str, patience: float = 5.0) -> None:
    """
    Deletes a detached tombstone directory. Because the directory is already
    detached from the canonical lock path, pollers cannot block new lock holders
    at the canonical path. Windows may keep handles open temporarily, so we retry
    until open handles pass.
    """
    info_path = os.path.join(target_dir, INFO_FILE_NAME)
    deadline = time.time() + patience
    delay = 0.01
    while True:
        try:
            if os.path.isfile(info_path):
                os.unlink(info_path)
        except (FileNotFoundError, OSError):
            pass
        try:
            os.rmdir(target_dir)
            return
        except FileNotFoundError:
            return
        except OSError:
            if not os.path.isdir(target_dir):
                return
            if time.time() >= deadline:
                shutil.rmtree(target_dir, ignore_errors=True)
                return
        time.sleep(_jittered(delay))
        delay = min(delay * 2, 0.1)


@_guard_queue_transition
def _remove_owned_queue_lock(
    queue_lock_dir: str,
    patience: float = 5.0,
    expected_token: Optional[str] = None,
    expected_identity: Optional[Tuple[Any, ...]] = None,
) -> bool:
    """
    Deletes the queue lock directory its owner holds.
    To prevent check-then-unlink races where a successor lock could have its metadata
    or directory deleted between check and unlink, we detach the directory with an
    atomic rename to a unique tombstone FIRST, then verify the full _lock_identity
    (token else PID+acquisition) before deleting only the tombstone.
    Atomic directory transitions share a stable OS file guard with acquisition.
    Rename the owned directory, verify its identity, then delete only the tombstone.
    No contender can create a successor during the guarded rename.
    """
    if expected_token is not None and expected_identity is None:
        expected_identity = ("token", expected_token)

    is_already_detached = ".tombstone-" in queue_lock_dir or ".releasing-" in queue_lock_dir
    if is_already_detached:
        if expected_identity is not None:
            info = _read_queue_lock_info(queue_lock_dir)
            if _lock_identity(info) != expected_identity:
                return False
        _clean_detached_lock_dir(queue_lock_dir, patience=patience)
        return True

    if not os.path.exists(queue_lock_dir):
        return False

    # Verify ownership before detaching canonical lock name (Codex P1)
    if expected_identity is not None:
        info = _read_queue_lock_info(queue_lock_dir)
        for _ in range(_INFO_READ_ATTEMPTS - 1):
            if info is not None:
                break
            time.sleep(_INFO_READ_RETRY_DELAY)
            info = _read_queue_lock_info(queue_lock_dir)
        if _lock_identity(info) != expected_identity:
            return False

    tombstone = f"{queue_lock_dir}.releasing-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        os.rename(queue_lock_dir, tombstone)
    except OSError:
        return False

    moved_info = _read_queue_lock_info(tombstone)
    for _ in range(_INFO_READ_ATTEMPTS - 1):
        if moved_info is not None:
            break
        time.sleep(_INFO_READ_RETRY_DELAY)
        moved_info = _read_queue_lock_info(tombstone)
    moved_identity = _lock_identity(moved_info)

    if expected_identity is not None and moved_identity != expected_identity:
        try:
            os.rename(tombstone, queue_lock_dir)
        except OSError as exc:
            logger.warning(
                "Could not restore mismatched queue lock tombstone %s to %s: %s; preserving successor evidence",
                tombstone,
                queue_lock_dir,
                exc,
            )
        return False

    _clean_detached_lock_dir(tombstone, patience=patience)
    return True


@_guard_queue_transition
def _tombstone_stale_queue_lock(
    queue_lock_dir: str,
    judged: Optional[Dict[str, Any]],
    stale_after: float = 15.0,
) -> bool:
    """
    Safely reclaims a stale queue lock using atomic directory rename to a unique tombstone.
    Verifies that the tombstone still contains the lock judged stale (full _lock_identity)
    before removing it. If the lock changed hands or belongs to a fresh successor, restores it.
    If restoration is impossible, preserves successor evidence.
    Returns True if this call removed the stale lock.
    """
    judged_identity = _lock_identity(judged)

    current_info = _read_queue_lock_info(queue_lock_dir)
    current_identity = _lock_identity(current_info)
    if judged_identity is not None and current_identity != judged_identity:
        return False
    if judged_identity is None:
        if current_identity is not None:
            return False
        try:
            mtime = os.path.getmtime(queue_lock_dir)
            if (time.time() - mtime) < stale_after:
                return False
        except OSError:
            return False

    tombstone = f"{queue_lock_dir}.tombstone-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        os.rename(queue_lock_dir, tombstone)
    except OSError:
        return False

    moved_info = _read_queue_lock_info(tombstone)
    moved_identity = _lock_identity(moved_info)

    is_mismatch = False
    if judged_identity is not None:
        if moved_identity != judged_identity:
            is_mismatch = True
    else:
        if moved_identity is not None:
            is_mismatch = True
        else:
            try:
                mtime = os.path.getmtime(tombstone)
                if (time.time() - mtime) < stale_after:
                    is_mismatch = True
            except OSError:
                is_mismatch = True

    if is_mismatch:
        try:
            os.rename(tombstone, queue_lock_dir)
        except OSError as exc:
            logger.warning(
                "Could not restore successor queue lock tombstone %s to %s: %s; preserving successor evidence",
                tombstone,
                queue_lock_dir,
                exc,
            )
        return False

    _clean_detached_lock_dir(tombstone, patience=2.0)
    return True


@contextmanager
def _queue_atomic_lock(
    run_dir: str,
    timeout: float = 10.0,
    retry_interval: float = 0.05,
    stale_after: float = 15.0,
    is_pid_alive_fn=None,
    max_hold: float = DEFAULT_QUEUE_LOCK_MAX_HOLD_SECONDS,
    cancel=None,
):
    """
    Short-lived atomic directory lock protecting reads/writes to build-slot.queue.json.
    Uses atomic os.mkdir on Windows and Linux (no fcntl).
    Reclaims a queue lock whose holding PID is dead, or whose live holder kept it longer than
    max_hold (default 120s). Stale reclamation renames to a unique tombstone and verifies
    token identity before deletion to prevent race conditions with concurrent acquisitions.
    Verifies unique ownership token before release so successor locks are never deleted.
    """
    queue_lock_dir = os.path.join(run_dir, QUEUE_LOCK_NAME)
    start_time = time.time()
    acquired = False
    lock_token = str(uuid.uuid4())
    pid_checker = is_pid_alive_fn or is_pid_alive
    delay = retry_interval
    max_delay = max(retry_interval, min(0.25, retry_interval * 10))

    while True:
        if cancel is not None:
            cancel()
        try:
            with _transition_guard(queue_lock_dir + ".guard", timeout=max(0.0, timeout - (time.time() - start_time)), cancel=cancel):
                os.mkdir(queue_lock_dir)
                try:
                    info_path = os.path.join(queue_lock_dir, INFO_FILE_NAME)
                    tmp_path = info_path + f".{os.getpid()}.{lock_token}.tmp"
                    with open(tmp_path, "w", encoding="utf-8") as f:
                        json.dump({
                            "pid": os.getpid(),
                            "token": lock_token,
                            "acquired_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "acquired_at_epoch": time.time(),
                        }, f)
                    _replace_with_retry(tmp_path, info_path)
                except Exception:
                    shutil.rmtree(queue_lock_dir, ignore_errors=True)
                    raise
                acquired = True
                break
        except PermissionError:
            # On Windows, a directory another process is removing sits in
            # delete-pending state, and os.mkdir raises PermissionError (WinError 5).
            # Treat like contention: honour timeout, sleep retry_interval, and retry.
            # Do NOT run stale-rmtree branch (the dir is already being deleted).
            if time.time() - start_time >= timeout:
                raise TimeoutError(f"Timed out waiting for queue file lock: {queue_lock_dir}")
            time.sleep(_jittered(delay))
            delay = min(delay * 1.5, max_delay)
        except FileExistsError:
            # Check if queue lock is stale:
            # Reclaim a dead holder, an orphan directory, or a live holder past max_hold.
            try:
                is_stale = False
                now = time.time()
                info = _read_queue_lock_info(queue_lock_dir)
                if info and info.get("pid"):
                    lock_pid = int(info["pid"])
                    if lock_pid > 0 and not pid_checker(lock_pid):
                        is_stale = True
                    else:
                        held_since = info.get("acquired_at_epoch")
                        if not isinstance(held_since, (int, float)):
                            held_since = os.path.getmtime(queue_lock_dir)
                        if now - held_since >= max_hold:
                            is_stale = True
                else:
                    # No info file or mid-creation: fallback to directory mtime
                    mtime = os.path.getmtime(queue_lock_dir)
                    if (now - mtime) >= stale_after:
                        info2 = _read_queue_lock_info(queue_lock_dir)
                        if not info2 or not info2.get("pid") or not pid_checker(int(info2["pid"])):
                            is_stale = True

                if is_stale:
                    if _tombstone_stale_queue_lock(queue_lock_dir, info, stale_after=stale_after):
                        continue
            except Exception:
                pass

            if time.time() - start_time >= timeout:
                raise TimeoutError(f"Timed out waiting for queue file lock: {queue_lock_dir}")
            time.sleep(_jittered(delay))
            delay = min(delay * 1.5, max_delay)

    try:
        yield
    finally:
        if acquired:
            try:
                _remove_owned_queue_lock(queue_lock_dir, expected_token=lock_token)
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
            res = subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return
        except Exception as e:
            logger.debug("cmd.exe mklink /J failed: %s; trying os.symlink", e)
        os.symlink(target, link_path, target_is_directory=True)
    else:
        os.symlink(target, link_path, target_is_directory=True)


def _is_link_or_junction(path: str) -> bool:
    """
    Returns True if path exists and is a symlink or directory junction (Windows reparse point).
    """
    try:
        if os.path.islink(path):
            return True
        st = os.stat(path, follow_symlinks=False)
        reparse_attr = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        file_attrs = getattr(st, "st_file_attributes", 0)
        if file_attrs & reparse_attr:
            return True
    except (OSError, ValueError):
        pass
    return False


def _remove_dir_link(link_path: str) -> bool:
    """
    Safely removes a directory junction or symlink without deleting target contents.
    On Windows, uses os.rmdir(), os.unlink(), or cmd /c rmdir without /s.
    Never uses shutil.rmtree or deletes target contents.
    """
    if not _is_link_or_junction(link_path) and not os.path.exists(link_path) and not os.path.islink(link_path):
        return False
    if sys.platform == "win32":
        try:
            if os.path.islink(link_path):
                os.unlink(link_path)
                return True
            os.rmdir(link_path)
            return True
        except Exception:
            try:
                os.unlink(link_path)
                return True
            except Exception:
                pass
            try:
                cmd = ["cmd.exe", "/c", "rmdir", os.path.abspath(link_path)]
                subprocess.run(cmd, capture_output=True, text=True, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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


def clean_stale_next_junction(
    next_dir: Optional[str] = None,
    cwd: Optional[str] = None,
) -> List[str]:
    """
    Safely removes stale junction or symlink at frontend/.next/standalone/node_modules
    before building, preventing Next.js cleanDistDir from hanging forever on Windows.

    Root cause: Next's cleanDistDir follows the junction into the protected deps store,
    gets EPERM, and its unlinkPath retry never increments, so it loops forever.

    Auto-detects:
      - <cwd>/frontend/.next/standalone/node_modules
      - <cwd>/.next/standalone/node_modules
    Plus optional next_dir:
      - <next_dir>/.next/standalone/node_modules
      - <next_dir>/frontend/.next/standalone/node_modules
      - <next_dir>/standalone/node_modules
      - <next_dir> (if named node_modules)

    Only removes if the path is a junction or symlink. Never recursively deletes contents
    or calls shutil.rmtree. Logs and returns all removed paths.
    """
    base_cwd = os.path.abspath(cwd or os.getcwd())
    raw_candidates: List[str] = []

    if next_dir:
        nd = os.path.abspath(os.path.join(base_cwd, next_dir)) if not os.path.isabs(next_dir) else os.path.abspath(next_dir)
        raw_candidates.append(os.path.join(nd, ".next", "standalone", "node_modules"))
        raw_candidates.append(os.path.join(nd, "frontend", ".next", "standalone", "node_modules"))
        raw_candidates.append(os.path.join(nd, "standalone", "node_modules"))
        if os.path.basename(nd).lower() == "node_modules":
            raw_candidates.append(nd)

    raw_candidates.append(os.path.join(base_cwd, "frontend", ".next", "standalone", "node_modules"))
    raw_candidates.append(os.path.join(base_cwd, ".next", "standalone", "node_modules"))

    seen = set()
    cleaned: List[str] = []
    for cand in raw_candidates:
        norm = os.path.abspath(cand)
        if norm in seen:
            continue
        seen.add(norm)

        if _is_link_or_junction(norm):
            if _remove_dir_link(norm):
                msg = f"[CLEANUP] Removed stale next standalone node_modules junction: {norm}"
                print(msg, file=sys.stderr)
                logger.info("Removed stale next standalone node_modules junction: %s", norm)
                cleaned.append(norm)
            else:
                logger.warning("Failed to remove stale next standalone node_modules junction: %s", norm)

    return cleaned

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
        ram_guard_threshold: float = DEFAULT_RAM_GUARD_THRESHOLD_PERCENT,
        ram_guard_idle_admit_after: Optional[float] = None,
        acquisition_stagger: Optional[float] = None,
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
        self.ram_guard_threshold = float(ram_guard_threshold)
        self.ram_guard_idle_admit_after = float(
            DEFAULT_RAM_GUARD_IDLE_ADMIT_SECONDS if ram_guard_idle_admit_after is None else ram_guard_idle_admit_after
        )
        if acquisition_stagger is not None:
            self.acquisition_stagger = float(acquisition_stagger)
        else:
            self.acquisition_stagger = float(DEFAULT_ACQUISITION_STAGGER_SECONDS)
            env_stagger = os.environ.get("BUILD_SLOT_STAGGER_SECONDS")
            if env_stagger is not None:
                try:
                    self.acquisition_stagger = float(env_stagger)
                except ValueError:
                    pass
        self.last_acquired_file = os.path.join(self.run_dir, LAST_ACQUIRED_FILE_NAME)
        self.queue_cleanup_grace = DEFAULT_QUEUE_CLEANUP_GRACE_SECONDS
        self.queue_grant_cleanup_grace = DEFAULT_QUEUE_GRANT_CLEANUP_GRACE_SECONDS
        self._queue_op_state = threading.local()
        os.makedirs(self.run_dir, exist_ok=True)

    def get_max_slots(self, ram_pct: Optional[float] = None) -> int:
        """
        Returns the maximum number of concurrent build slots allowed (default: 8).
        Can be overridden via max_slots parameter or BUILD_SLOT_MAX_SLOTS env var.
        """
        if self.max_slots_override is not None:
            return self.max_slots_override
        env_slots = os.environ.get("BUILD_SLOT_MAX_SLOTS")
        if env_slots is not None:
            try:
                return int(env_slots)
            except ValueError:
                pass
        return len(self.slot_dirs)
    def _read_last_acquired_at(self) -> Optional[float]:
        """Reads epoch timestamp of most recent slot acquisition from run_dir, if present."""
        if not os.path.exists(self.last_acquired_file):
            return None
        try:
            with open(self.last_acquired_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return float(data.get("acquired_at_epoch", 0.0))
        except Exception:
            return None

    def _record_last_acquired_at(self, name: str, pid: int, slot_idx: int) -> None:
        """Atomically records the timestamp of a new slot acquisition."""
        tmp_path = f"{self.last_acquired_file}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
        now = time.time()
        payload = {
            "acquired_at_epoch": now,
            "acquired_at_iso": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
            "owner": name,
            "pid": pid,
            "slot": slot_idx,
        }
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp_path, self.last_acquired_file)
        except Exception as e:
            logger.warning("Failed to record last-acquired-at: %s", e)
            try:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass

    def _read_slot_info(self, slot_idx: int = 0, retry: Optional[bool] = None) -> Optional[Dict[str, Any]]:
        """Reads lock info metadata for slot_idx if its lock dir exists."""
        if slot_idx >= len(self.slot_dirs):
            return None
        if retry is None:
            retry = not getattr(self._queue_op_state, "under_queue_lock", False)
        if retry:
            return _read_lock_dir_info(self.slot_dirs[slot_idx], slot_idx)
        return _read_lock_dir_info(self.slot_dirs[slot_idx], slot_idx, retry=False)

    def _tombstone_stale_slot(self, slot_idx: int, judged: Dict[str, Any], reason: str) -> bool:
        """Detach a matching stale generation before cleaning it outside the guard."""
        tombstone = self._detach_stale_slot(slot_idx, judged, reason)
        if tombstone is None:
            return False
        shutil.rmtree(tombstone, ignore_errors=True)
        return True

    @_guard_slot_transition
    def _detach_stale_slot(self, slot_idx: int, judged: Dict[str, Any], reason: str) -> Optional[str]:
        slot_dir = self.slot_dirs[slot_idx]
        want = _lock_identity(judged)
        current = _read_lock_dir_info(slot_dir, slot_idx, retry=False)
        if want is None or current != judged:
            return None
        tombstone = f"{slot_dir}.tombstone-{os.getpid()}-{uuid.uuid4().hex}"
        try:
            os.rename(slot_dir, tombstone)
        except OSError:
            return None
        moved = _read_lock_dir_info(tombstone, slot_idx, retry=False)
        if moved is None:
            return None
        if moved != judged:
            try:
                os.rename(tombstone, slot_dir)
            except OSError as e:
                if not os.path.isdir(tombstone):
                    return None
                msg = f"[ERROR] Moved a live build slot lock aside and could not restore it (slot {slot_idx}, {tombstone}): {e}"
                _transition_notice(msg, "error")
            return None
        _transition_notice(f"[NOTICE] Reclaiming stale build slot lock: {reason}")
        return tombstone

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
        wrapper_pid: Optional[int] = None,
        job_class: str = "light",
        mem_gib: float = 0.5,
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
            "job_class": job_class,
            "mem_gib": mem_gib,
            "acquired_at": now_iso,
            "acquired_at_epoch": now,
            "heartbeat_at": now_iso,
            "heartbeat_at_epoch": now,
        }
        if child_pid is not None:
            info["child_pid"] = child_pid
        if wrapper_pid is not None:
            info["wrapper_pid"] = wrapper_pid
            created_ticks = _get_process_create_ticks(wrapper_pid)
            if created_ticks is not None:
                info["wrapper_created_ticks"] = created_ticks
            created_epoch = _get_process_create_epoch(wrapper_pid)
            if created_epoch is not None:
                info["wrapper_created_epoch"] = created_epoch
        _write_json_atomic(info_path, info)

    def _write_lock_info(self, owner: str, pid: int, token: Optional[str] = None) -> None:
        """Writes info.json inside the primary lock directory (backwards compatibility)."""
        self._write_slot_info(0, owner, pid, token)

    def _read_queue(self) -> List[Dict[str, Any]]:
        """Reads queue list safely with retries for transient Windows locks."""
        if not os.path.isfile(self.queue_file):
            return []
        delays = [0.02, 0.05, 0.1, 0.15, 0.2]
        last_error: Optional[Exception] = None
        for attempt in range(len(delays)):
            try:
                with open(self.queue_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
                raise ValueError(
                    f"Queue file '{self.queue_file}' does not contain a list (got {type(data).__name__})"
                )
            except (PermissionError, OSError) as e:
                last_error = e
                winerror = getattr(e, "winerror", None)
                if isinstance(e, PermissionError) or winerror in (5, 32):
                    if attempt < len(delays) - 1:
                        time.sleep(delays[attempt])
                        continue
                logger.warning("Failed to read queue file '%s': %s", self.queue_file, e)
                raise
            except json.JSONDecodeError as e:
                last_error = e
                if attempt < len(delays) - 1:
                    time.sleep(delays[attempt])
                    continue
                logger.warning("Corrupt JSON in queue file '%s': %s", self.queue_file, e)
                raise
            except Exception as e:
                last_error = e
                logger.warning("Unexpected error reading queue file '%s': %s", self.queue_file, e)
                raise
        if last_error is not None:
            raise last_error
        raise OSError(f"Failed to read queue file '{self.queue_file}' after {len(delays)} attempts")

    def _write_queue(self, queue: List[Dict[str, Any]]) -> None:
        """Writes queue list atomically using a temp file and os.replace."""
        _write_json_atomic(self.queue_file, queue, prefix="queue-", retry=True)

    def _is_entry_stale(
        self,
        item: Dict[str, Any],
        now: float,
        stale_heartbeat_after: float,
        stale_fallback_after: float,
        alive_pids: Optional[Dict[int, bool]] = None,
    ) -> Tuple[bool, str]:
        """Dead PIDs expire immediately; heartbeat leases expire even with shared PIDs."""
        pid = item.get("pid", 0)
        if pid > 0:
            if alive_pids is not None:
                is_alive = alive_pids.get(pid)
                if is_alive is None:
                    is_alive = True
            else:
                is_alive = self.is_pid_alive(pid)
            if not is_alive:
                return True, f"PID {pid} is dead"
        heartbeat = _parse_timestamp(item.get("heartbeat_at"))
        if item.get("heartbeat_at") is not None and heartbeat is None:
            return True, "corrupt heartbeat timestamp"
        stamp = heartbeat if heartbeat is not None else _parse_timestamp(item.get("enqueued_at"))
        if stamp is None:
            return True, "missing or corrupt lease timestamp"
        limit = stale_heartbeat_after if heartbeat is not None else stale_fallback_after
        silence = now - stamp
        return (silence > limit, f"token lease expired ({silence:.1f}s > {limit:.1f}s)")

    def _clean_queue_locked(
        self,
        queue: List[Dict[str, Any]],
        now: float,
        stale_heartbeat_after: float,
        stale_fallback_after: float,
        alive_pids: Optional[Dict[int, bool]] = None,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Filters out stale entries and deduplicates by (name, pid) while holding queue atomic lock."""
        new_queue = []
        changed = False
        seen_keys = set()
        for item in queue:
            stale, reason = self._is_entry_stale(
                item, now, stale_heartbeat_after, stale_fallback_after, alive_pids=alive_pids
            )
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

    def _held_slot_tokens(self) -> set:
        """Tokens of the leases that currently hold a slot."""
        tokens = set()
        for slot_idx, slot_dir in enumerate(self.slot_dirs):
            if os.path.isdir(slot_dir):
                info = self._read_slot_info(slot_idx)
                if info and info.get("token"):
                    tokens.add(info["token"])
        return tokens

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

        held_tokens = self._held_slot_tokens()

        def prune(
            queue: List[Dict[str, Any]],
            alive_pids: Optional[Dict[int, bool]] = None,
        ) -> Tuple[List[Dict[str, Any]], bool]:
            # A lease that holds a slot has left the queue; an entry still naming it is residue
            # from a cleanup that could not take the queue lock.
            kept = [item for item in queue if not (item.get("token") and item["token"] in held_tokens)]
            cleaned, changed = self._clean_queue_locked(
                kept, time.time(), hb_limit, fb_limit, alive_pids=alive_pids
            )
            return cleaned, changed or len(kept) != len(queue)

        try:
            # The queue file is replaced atomically, so a lock-free snapshot is consistent. Most
            # polls change nothing; taking the queue lock for those only starves the writers.
            snapshot = self._read_queue()
            alive_pids = {
                item["pid"]: self.is_pid_alive(item["pid"])
                for item in snapshot
                if item.get("pid") and item.get("pid") > 0
            }
            try:
                res_snapshot, would_change = prune(snapshot, alive_pids=alive_pids)
                if not would_change:
                    return res_snapshot
            except Exception:
                pass
            with self._queue_lock():
                queue = self._read_queue()
                new_queue, changed = prune(queue, alive_pids=alive_pids)
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
        enqueued_at: Optional[float] = None,
        job_class: str = "light",
        mem_gib: Optional[float] = None,
    ) -> int:
        """
        Adds (name, pid, token) to the queue if not already present.
        The queue is kept in _queue_order_key order: priority entries first,
        each group first-come first-served by original enqueue time. A new
        entry lands behind every earlier entry of its group. An entry that is
        already queued only has its heartbeat refreshed; it moves only when
        re-enqueued with priority while still normal, and then joins the
        priority group at its own enqueue time.
        When recovering a dropped entry, pass enqueued_at to preserve original position.
        Returns the 0-indexed position in queue.
        """
        reservation = _reservation_gib(job_class, mem_gib)
        total = get_total_ram_gib()
        if total is not None and math.isfinite(total) and total > 0:
            maximum = max(0.0, total - MEMORY_FLOOR_GIB)
            if reservation > maximum:
                raise ValueError(
                    f"request cannot fit on this host: needs {reservation:.2f}, "
                    f"max possible {maximum:.2f} GiB (physical RAM minus free floor)"
                )
        hb_limit = stale_heartbeat_after if stale_heartbeat_after is not None else self.queue_stale_heartbeat_after
        try:
            pre_queue = self._read_queue()
            alive_pids = {
                item["pid"]: self.is_pid_alive(item["pid"])
                for item in pre_queue
                if item.get("pid") and item.get("pid") > 0
            }
        except Exception:
            alive_pids = {}
        alive_pids[pid] = True

        with self._queue_lock():
            queue = self._read_queue()
            now = time.time()
            valid_queue, changed = self._clean_queue_locked(
                queue, now, hb_limit, self.queue_stale_fallback_after, alive_pids=alive_pids
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
                item["job_class"] = job_class
                item["mem_gib"] = _reservation_gib(job_class, mem_gib)
                if priority and not item.get("priority"):
                    # Upgrade: joins the priority group at its own enqueue time.
                    item["priority"] = True
                    valid_queue.sort(key=_queue_order_key)
                self._write_queue(valid_queue)
                return valid_queue.index(item)

            now_iso = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()
            effective_enqueued_at = enqueued_at if enqueued_at is not None else now
            effective_enqueued_iso = (
                datetime.datetime.fromtimestamp(effective_enqueued_at, datetime.timezone.utc).isoformat()
                if enqueued_at is not None
                else now_iso
            )
            entry = {
                "name": name,
                "pid": pid,
                "token": token,
                "enqueued_at": effective_enqueued_at,
                "enqueued_at_iso": effective_enqueued_iso,
                "heartbeat_at": now,
                "heartbeat_at_iso": now_iso,
                "job_class": job_class,
                "mem_gib": _reservation_gib(job_class, mem_gib),
            }
            if priority:
                entry["priority"] = True
            valid_queue.append(entry)
            # Lands in queue ordered by (priority, original enqueue time)
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
        with self._queue_lock():
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

    def _any_slot_held(self) -> bool:
        return any(os.path.isdir(s_dir) for s_dir in self.slot_dirs)

    @contextmanager
    def _queue_lock(self):
        """
        The queue file lock, with each attempt capped at the time left to the deadline of the
        running _retry_queue_op, so no attempt overshoots the caller's own --timeout.
        Sets under_queue_lock on self._queue_op_state while held so queue writes and
        slot-info reads do not sleep or retry in the critical section.
        """
        deadline = getattr(self._queue_op_state, "deadline", None)
        if deadline is None:
            lock_cm = _queue_atomic_lock(self.run_dir, is_pid_alive_fn=self.is_pid_alive,
                                         cancel=getattr(self._queue_op_state, "cancel", None))
        else:
            remaining = max(0.05, min(10.0, deadline - time.time()))
            lock_cm = _queue_atomic_lock(self.run_dir, timeout=remaining, is_pid_alive_fn=self.is_pid_alive,
                                         cancel=getattr(self._queue_op_state, "cancel", None))
        with lock_cm:
            prev = getattr(self._queue_op_state, "under_queue_lock", False)
            self._queue_op_state.under_queue_lock = True
            try:
                yield
            finally:
                self._queue_op_state.under_queue_lock = prev

    def _within_deadline(self, deadline: Optional[float], op):
        """Runs op once with every queue-lock attempt capped at 'deadline' (None: uncapped)."""
        previous = getattr(self._queue_op_state, "deadline", None)
        self._queue_op_state.deadline = deadline
        try:
            return op()
        finally:
            self._queue_op_state.deadline = previous

    def _retry_queue_op(self, op, deadline: Optional[float] = None):
        """
        Runs a queue-file operation and retries lock timeouts and transient OS errors with
        jittered backoff until 'deadline' (epoch seconds; None retries until it succeeds).
        A busy queue lock must never abort a waiter or drop its place (#691).
        """
        delay = 0.05
        while True:
            cancel = getattr(self._queue_op_state, "cancel", None)
            if cancel is not None:
                cancel()
            previous = getattr(self._queue_op_state, "deadline", None)
            self._queue_op_state.deadline = deadline
            try:
                return op()
            except OSError as exc:
                now = time.time()
                if deadline is not None and now >= deadline:
                    raise
                logger.debug("Queue operation retrying after: %s", exc)
                pause = _jittered(delay)
                if deadline is not None:
                    pause = min(pause, max(0.0, deadline - now))
                time.sleep(pause)
                delay = min(delay * 2, 1.0)
            finally:
                self._queue_op_state.deadline = previous

    def _dequeue_own_entry(
        self, name: str, pid: Optional[int], token: Optional[str], grace: Optional[float] = None
    ) -> None:
        """
        Removes the caller's queue entry once its slot or wait is settled. Retries a busy
        queue lock for the release grace period; if it still fails, clean_queue sweeps the
        entry (its PID dies, or its token holds a slot).
        """
        try:
            self._retry_queue_op(
                lambda: self.dequeue(name, pid, token=token),
                time.time() + (self.queue_cleanup_grace if grace is None else grace),
            )
        except Exception as exc:
            logger.warning("Queue cleanup for '%s' deferred to clean_queue: %s", name, exc)

    def _dequeue_best_effort(self, name: str, token: Optional[str]) -> None:
        """
        Queue cleanup after the slot is already freed. A busy queue lock is retried for the
        release grace period; a completed release is never turned into a failure or a hang (#620).
        """
        try:
            self._retry_queue_op(
                lambda: self.dequeue(name, token=token),
                time.time() + self.queue_cleanup_grace,
            )
        except (TimeoutError, PermissionError, OSError) as exc:
            print(f"WARNING: '{name}' released its slot but queue cleanup was skipped: {exc}", file=sys.stderr)

    def dequeue(
        self,
        name: Optional[str] = None,
        pid: Optional[int] = None,
        token: Optional[str] = None,
    ) -> None:
        """Removes entry matching token, or (name, pid) if token is not provided."""
        with self._queue_lock():
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
            with self._queue_lock():
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

    @_guard_slot_transition
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
            if info.get("wrapper_created_ticks") is None:
                ticks = _get_process_create_ticks(wrapper_pid)
                if ticks is not None:
                    info["wrapper_created_ticks"] = ticks
            if info.get("wrapper_created_epoch") is None:
                ep = _get_process_create_epoch(wrapper_pid)
                if ep is not None:
                    info["wrapper_created_epoch"] = ep
            info["heartbeat_at"] = now_iso
            info["heartbeat_at_epoch"] = now
            return self._write_heartbeat(name, slot_idx, slot_dir, info)
        return False

    @_guard_slot_transition
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
        Writes heartbeat-bearing lock info. When info.json cannot be replaced (Windows
        refuses while a foreign process keeps it open, WinError 5) the heartbeat goes to
        a second file in the lock dir that no other process holds, and readers fold it in
        (_merge_side_heartbeat). Only when both writes fail does the caller see False.
        The failure is printed: a heartbeat that cannot be written is how a slot looks dead (#315, #690).
        """
        info_path = os.path.join(slot_dir, INFO_FILE_NAME)
        side_path = os.path.join(slot_dir, HEARTBEAT_FILE_NAME)
        try:
            _write_json_atomic(info_path, info)
        except Exception as e:
            msg = f"[HEARTBEAT] Failed to replace info.json for '{name}' (slot {slot_idx}): {e}"
            _transition_notice(msg, "warning")
            try:
                side_data: Dict[str, Any] = {
                    "token": info.get("token"),
                    "heartbeat_at": info.get("heartbeat_at"),
                    "heartbeat_at_epoch": info.get("heartbeat_at_epoch"),
                }
                for key in ("wrapper_pid", "child_pid", "wrapper_created_ticks", "wrapper_created_epoch"):
                    if info.get(key) is not None:
                        side_data[key] = info[key]
                _write_json_atomic(side_path, side_data, prefix=".hb-")
                return True
            except Exception as e2:
                msg = f"[HEARTBEAT] Failed to write heartbeat for '{name}' (slot {slot_idx}): {e2}"
                _transition_notice(msg, "warning")
                return False
        try:
            os.unlink(side_path)  # info.json carries the newest heartbeat again
        except OSError:
            pass
        return True

    def check_stale_and_reclaim(
        self,
        heartbeat_stale_after: float = DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS,
        pid_dead_grace_period: Optional[float] = None,
        acquire_holder_stale_after: float = DEFAULT_ACQUIRE_HOLDER_STALE_SECONDS,
    ) -> bool:
        """
        Checks if the currently held lock is stale. A live `run` holder is never reclaimed on
        age or stale heartbeat alone: a build that runs past any fixed age must keep its slot.
        Reclaims it if:
          1. `run` lock (has wrapper_pid): the wrapper PID is dead; or the wrapper PID is alive
             but proven recycled (mismatched creation identity, or created after lock was acquired).
             A genuine live wrapper or one with unknown identity is never reclaimed on age or
             heartbeat staleness. A dead wrapped command (a shim or launcher) never frees the slot:
             the wrapper waits for its command and releases in `finally`.
          2. Other locks (`acquire` mode, which has no process left to heartbeat): owner
             PID is dead AND lock age exceeds grace period (and heartbeat not fresh); or the
             owner PID is alive (the lane's host) but the heartbeat, or the lock age when
             there is none, is older than acquire_holder_stale_after (30 min): the lane
             ended without releasing (#620).
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
                wrapper_pid = info.get("wrapper_pid")

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
                    holder_pid = wrapper_pid or pid
                    silence = hb_age if hb_age is not None else age
                    limit = heartbeat_stale_after if wrapper_pid else acquire_holder_stale_after
                    if holder_pid > 0 and not self.is_pid_alive(holder_pid):
                        is_stale = True
                        reason = f"owner PID {holder_pid} is dead (owner='{owner}', slot {slot_idx})"
                    elif holder_pid > 0 and wrapper_pid:
                        # A `run` wrapper. Check if the live PID was recycled by comparing creation identity.
                        recorded_ticks = info.get("wrapper_created_ticks")
                        recorded_epoch = info.get("wrapper_created_epoch")
                        current_ticks = _get_process_create_ticks(holder_pid)
                        current_epoch = _get_process_create_epoch(holder_pid)
                        if recorded_ticks is not None and current_ticks is not None:
                            if current_ticks != recorded_ticks:
                                is_stale = True
                                reason = (
                                    f"wrapper PID {holder_pid} was recycled (creation ticks mismatch: "
                                    f"recorded={recorded_ticks}, current={current_ticks}, slot {slot_idx})"
                                )
                        elif recorded_epoch is not None and current_epoch is not None:
                            if abs(current_epoch - recorded_epoch) > 1.0:
                                is_stale = True
                                reason = (
                                    f"wrapper PID {holder_pid} was recycled (creation epoch mismatch: "
                                    f"recorded={recorded_epoch:.1f}, current={current_epoch:.1f}, slot {slot_idx})"
                                )
                        elif acquired_epoch is not None:
                            # Legacy lock: check if live process was proven created after the lock was acquired
                            if current_ticks is not None:
                                acquired_ticks = int(acquired_epoch * 10000000) + 116444736000000000
                                if current_ticks > acquired_ticks + 10000000:
                                    is_stale = True
                                    reason = f"wrapper PID {holder_pid} was recycled (created after lock acquired, slot {slot_idx})"
                            elif current_epoch is not None and current_epoch > acquired_epoch + 1.0:
                                is_stale = True
                                reason = f"wrapper PID {holder_pid} was recycled (created after lock acquired, slot {slot_idx})"
                    elif holder_pid > 0 and silence >= limit:
                        is_stale = True
                        reason = (
                            f"token heartbeat lease expired ({silence:.1f}s >= {limit:.1f}s, "
                            f"owner='{owner}', slot {slot_idx})"
                        )

                if is_stale and wrapper_pid:
                    child = info.get("child_pid", 0)
                    if (child and self.is_pid_alive(child)) or (
                        sys.platform == "win32" and info.get("token") and _windows_job_alive(info["token"])
                    ):
                        is_stale = False
                if is_stale and self._tombstone_stale_slot(slot_idx, info, reason):
                    reclaimed_any = True
            except (FileNotFoundError, OSError):
                pass
            except Exception as e:
                logger.warning("Error checking stale lock on slot %d: %s", slot_idx, e)

        return reclaimed_any

    def _memory_budget(self) -> Dict[str, Any]:
        available = get_available_ram_gib()
        held = [self._read_slot_info(i) for i, path in enumerate(self.slot_dirs) if os.path.isdir(path)]
        reserved = 0.0
        ramp_reservations = 0.0
        heavy_ramp_reservations = 0.0
        ram_percent = get_system_ram_percent()
        heavy_jobs = 0
        now = time.time()
        for info in held:
            info = info or {}
            job_class = info.get("job_class")
            reservation = _reservation_gib(job_class, info.get("mem_gib"))
            if job_class not in MEMORY_RESERVATIONS:
                job_class = "heavy"
            heavy_jobs += job_class == "heavy"
            reserved += reservation
            acquired = _parse_timestamp(info.get("acquired_at_epoch", info.get("acquired_at")))
            window = MEMORY_RAMP_SECONDS.get(job_class, MEMORY_RAMP_SECONDS["heavy"])
            # Available RAM already excludes a settled job's memory. Reserve only
            # during its peak ramp; unknown or future timestamps stay conservative.
            if acquired is None or not math.isfinite(acquired) or now - acquired < window:
                ramp_reservations += reservation
                if job_class == "heavy":
                    heavy_ramp_reservations += HEAVY_RESERVATION_GIB
        return {
            "available_gib": available, "reserved_gib": reserved,
            "ramp_reservations_gib": ramp_reservations,
            "floor_gib": MEMORY_FLOOR_GIB,
            "free_budget_gib": None if available is None else available - ramp_reservations - MEMORY_FLOOR_GIB,
            "occupied_names": [info["owner"] for info in held if info and info.get("owner")],
            "heavy_jobs": heavy_jobs,
            "heavy_ramp_reservations_gib": heavy_ramp_reservations,
            "heavy_default_gib": HEAVY_RESERVATION_GIB,
            "ram_percent": ram_percent,
            "heavy_limit": _heavy_limit(ram_percent),
        }

    def _resource_refusal(self, job_class, mem_gib, budget, force=False):
        if job_class not in MEMORY_RESERVATIONS:
            job_class = "heavy"
        reasons = []
        ram_percent = budget["ram_percent"] if "ram_percent" in budget else get_system_ram_percent()
        heavy_limit = _heavy_limit(ram_percent)
        if job_class == "heavy" and budget["heavy_jobs"] >= heavy_limit:
            reasons.append(f"heavy cap: {budget['heavy_jobs']} heavy job(s) held, limit {heavy_limit}")
        if job_class == "heavy" and budget["heavy_jobs"] and not second_heavy_fits(
            budget["available_gib"], mem_gib,
            budget.get("heavy_ramp_reservations_gib", HEAVY_RESERVATION_GIB * budget["heavy_jobs"]),
        ):
            reasons.append(f"current RAM: second heavy needs {mem_gib:.2f} GiB plus young heavy reservations and {MEMORY_FLOOR_GIB:.2f} GiB floor")
        free = budget["free_budget_gib"]
        if not force:
            if free is None:
                reasons.append("budget unavailable: RAM telemetry missing")
            elif free < mem_gib:
                reasons.append(
                    f"budget: available {budget['available_gib']:.2f} - reserved "
                    f"{budget.get('ramp_reservations_gib', budget['reserved_gib']):.2f} - floor {budget['floor_gib']:.2f} "
                    f"= {free:.2f} GiB, needs {mem_gib:.2f} GiB"
                )
        return "; ".join(reasons) or None

    def _queue_admission(self, queue, token, budget, now, force=False, last_acquired_at=None):
        caller = next((item for item in queue if item.get("token") == token), None)
        if caller is None:
            return False, "waiter missing from queue"
        occupied_names = budget.get("occupied_names", ())
        if caller.get("name") in occupied_names:
            return False, f"lane '{caller.get('name')}' already holds a slot"
        # acquire cannot grant a second slot to an occupied lane, even under a
        # different token. Grant residues may also appear after clean_queue's
        # snapshot. Neither is a runnable FIFO predecessor.
        queue = [item for item in queue if item.get("name") not in occupied_names]
        head = queue[0]
        head_class = head.get("job_class")
        head_mem = _reservation_gib(head_class, head.get("mem_gib"))
        if head_class not in MEMORY_RESERVATIONS:
            head_class = "heavy"
        head_reason = self._resource_refusal(head_class, head_mem, budget, force)
        stagger_reason = None
        if not force and last_acquired_at is not None:
            elapsed = now - last_acquired_at
            if elapsed < self.acquisition_stagger:
                stagger_reason = f"stagger delay active ({elapsed:.1f}s < {self.acquisition_stagger:.1f}s since last acquisition)"
                head_reason = f"{head_reason}; {stagger_reason}" if head_reason else stagger_reason
        available = budget["available_gib"]
        projected = None if available is None else available + budget["reserved_gib"] - budget["floor_gib"]
        impossible = projected is not None and projected < head_mem
        notice = (
            f"head cannot fit on this host: needs {head_mem:.2f}, max possible {projected:.2f} GiB"
            if impossible else None
        )
        if notice:
            _transition_notice(notice, "warning")
        if caller is head:
            return head_reason is None, notice or head_reason
        if head_reason is None:
            return False, f"FIFO head '{head.get('name')}' can run"
        age = now - (_parse_timestamp(head.get("enqueued_at")) or now)
        if (head_class == "heavy" and age > 1200 and not impossible
                and projected is not None and projected >= head_mem and budget["heavy_jobs"] == 0
                and (age >= 2400 or (age - 1200) % 180 < 60)):
            return False, f"backfill paused: aging heavy head '{head.get('name')}' waited {age:.1f}s"
        caller_mem = _reservation_gib(caller.get("job_class"), caller.get("mem_gib"))
        if caller_mem >= head_mem:
            return False, notice or "backfill requires a smaller reservation than the blocked head"
        # Keep FIFO among jobs that can currently run. Blocked entries keep their place.
        for item in queue[1:]:
            item_class = item.get("job_class")
            item_mem = _reservation_gib(item_class, item.get("mem_gib"))
            if item_class not in MEMORY_RESERVATIONS:
                item_class = "heavy"
            reason = self._resource_refusal(item_class, item_mem, budget, force)
            if stagger_reason and item_class == "heavy":
                reason = f"{reason}; {stagger_reason}" if reason else stagger_reason
            if item is caller:
                return reason is None, notice or reason
            if item_mem < head_mem and reason is None:
                return False, notice or f"earlier backfill waiter '{item.get('name')}' can run"
        return False, notice or "waiting for FIFO admission"

    def _inherited_holder(self):
        """Trust a marker only while its exact holder still owns a live slot."""
        try:
            marker = json.loads(os.environ.get("BUILD_SLOT_HELD", ""))
            if not isinstance(marker, dict):
                return None
            pid = marker.get("pid")
            if not isinstance(pid, int) or pid <= 0 or not marker.get("token"):
                return None
            if not self.is_pid_alive(pid):
                return None
            for idx, path in enumerate(self.slot_dirs):
                info = _read_lock_dir_info(path, idx)
                if not info or info.get("corrupt"):
                    continue
                if any(info.get(key) != marker.get(key) for key in ("owner", "pid", "token", "job_class", "mem_gib")):
                    continue
                recorded = info.get("wrapper_created_ticks")
                current = _get_process_create_ticks(pid) if recorded is not None else None
                if recorded is not None and current != recorded:
                    return None
                if info.get("job_class") not in MEMORY_RESERVATIONS:
                    return None
                _reservation_gib(info["job_class"], info.get("mem_gib"))
                return marker
        except (OSError, ValueError, TypeError):
            pass
        return None

    def _nested_allowed(self, holder, job_class, mem_gib):
        ranks = {"light": 0, "browser": 1, "medium": 2, "heavy": 3}
        if ranks[job_class] > ranks[holder["job_class"]] or mem_gib > holder["mem_gib"]:
            print(
                f"[NESTED] Refused {job_class} {mem_gib:g} GiB: held slot "
                f"{holder['owner']} allows {holder['job_class']} {holder['mem_gib']:g} GiB. "
                "Nested calls cannot upgrade the holder's reservation.",
                file=sys.stderr,
            )
            return False
        print(f"[NESTED] running under held slot {holder['owner']}", file=sys.stderr)
        return True

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
        next_dir: Optional[str] = None,
        cwd: Optional[str] = None,
        wrapper_pid: Optional[int] = None,
        job_class: Optional[str] = None,
        mem_gib: Optional[float] = None,
    ) -> bool:
        """
        Acquires the build slot lock for 'name'.
        Blocks with poll_interval until acquired, or until timeout.
        Returns True on success, raises or returns False on failure.
        """
        _check_freeze(self.run_dir)
        job_class = job_class or "light"
        if job_class not in MEMORY_RESERVATIONS:
            raise ValueError("Unknown job class")
        mem_gib = _reservation_gib(job_class, mem_gib)
        holder = self._inherited_holder()
        if holder is not None:
            return self._nested_allowed(holder, job_class, mem_gib)
        if force:
            if os.environ.get("BUILD_SLOT_ALLOW_FORCE") != "1":
                print("--force requires BUILD_SLOT_ALLOW_FORCE=1", file=sys.stderr)
                return False
            print("[NOTICE] BUILD_SLOT_ALLOW_FORCE=1: --force overrides RAM admission", file=sys.stderr)
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

        ram_pct = get_system_ram_percent()
        start_time = time.time()
        last_heartbeat = start_time
        acquired = False
        original_enqueued_at = start_time
        last_refusal = None

        previous_cancel = getattr(self._queue_op_state, "cancel", None)
        self._queue_op_state.cancel = lambda: _check_freeze(self.run_dir)
        try:
            # 2. Register in FIFO Queue inside try so finally always cleans up
            queue_deadline = start_time + timeout if timeout is not None else None
            try:
                self._retry_queue_op(
                    lambda: self.enqueue(
                        name, pid, token=token, stale_heartbeat_after=effective_heartbeat_threshold,
                        priority=priority, enqueued_at=original_enqueued_at,
                        job_class=job_class, mem_gib=mem_gib,
                    ),
                    queue_deadline,
                )
            except ValueError as e:
                print(str(e), file=sys.stderr)
                return False
            except OSError as e:
                msg = f"Timed out after {timeout:.1f}s waiting for build slot lock: queue file lock stayed busy ({e})"
                print(msg, file=sys.stderr)
                logger.error(msg)
                return False
            # Announce waiting only after physical-capacity validation and enqueue.
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

            while True:
                _check_freeze(self.run_dir)
                # Update heartbeat first if due (every <= 15s)
                now = time.time()
                if now - last_heartbeat >= heartbeat_interval:
                    try:
                        hb_ok = self._within_deadline(
                            queue_deadline, lambda: self.heartbeat(token=token, name=name, pid=pid)
                        )
                        if hb_ok:
                            last_heartbeat = now
                        else:
                            # Verify if the entry is ACTUALLY missing from the queue before logging and re-enqueuing
                            try:
                                queue_snapshot = self._read_queue()
                                is_in_queue = any(
                                    (token and item.get("token") == token)
                                    or (not token and item.get("name") == name and (pid is None or item.get("pid") == pid))
                                    for item in queue_snapshot
                                )
                            except Exception as e:
                                logger.warning(
                                    "Failed to read queue snapshot during heartbeat check for '%s': %s; will retry next tick",
                                    name, e,
                                )
                                is_in_queue = True

                            if not is_in_queue:
                                notice = (
                                    f"[NOTICE] Queue entry for '{name}' (PID {pid}, token {token}) "
                                    f"was missing during heartbeat; re-enqueuing."
                                )
                                print(notice, file=sys.stderr)
                                logger.warning(notice)
                                self.enqueue(
                                    name,
                                    pid,
                                    token=token,
                                    stale_heartbeat_after=effective_heartbeat_threshold,
                                    priority=priority,
                                    enqueued_at=original_enqueued_at,
                                    job_class=job_class, mem_gib=mem_gib,
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
                    queue = self._within_deadline(
                        queue_deadline,
                        lambda: self.clean_queue(stale_heartbeat_after=effective_heartbeat_threshold),
                    )
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
                    if timeout is not None and time.time() - start_time >= timeout:
                        msg = f"Timed out after {timeout:.1f}s waiting for build slot lock: queue read failed ({e})"
                        print(msg, file=sys.stderr)
                        logger.error(msg)
                        return False
                    time.sleep(min(poll_interval, heartbeat_interval))
                    continue

                # 3. Dynamic RAM evaluation at acquisition
                curr_ram = get_system_ram_percent()
                ram_blocked = curr_ram is not None and curr_ram >= self.ram_guard_threshold and not force
                if ram_blocked:
                    # System RAM is >= 95%, refuse acquisition until it drops
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
                                self._dequeue_own_entry(name, pid, token, self.queue_grant_cleanup_grace)
                                msg = f"Build slot lock already held by '{name}' (PID {pid})"
                                print(msg)
                                clean_stale_next_junction(next_dir=next_dir, cwd=cwd)
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

                    # Resource eligibility is decided again under the slot guard.
                    is_eligible = caller_idx is not None

                    if is_eligible:
                        # Attempt to acquire the first free slot within allowed max_slots
                        for slot_idx in range(min(max_slots, len(self.slot_dirs))):
                            slot_dir = self.slot_dirs[slot_idx]
                            if not os.path.isdir(slot_dir):
                                try:
                                    with _transition_guard(os.path.join(self.run_dir, "build-slot.guard")):
                                        _check_freeze(self.run_dir)
                                        budget = self._memory_budget()
                                        current_queue = self._read_queue()
                                        last_acq = self._read_last_acquired_at()
                                        eligible, reason = self._queue_admission(
                                            current_queue, token, budget, time.time(), force, last_acq
                                        )
                                        if reason and reason != last_refusal:
                                            _transition_notice(f"[WAIT] '{name}': {reason}", "warning")
                                        last_refusal = reason
                                        if not eligible:
                                            break
                                        os.mkdir(slot_dir)
                                        self._write_slot_info(slot_idx, owner=name, pid=pid, token=token, wrapper_pid=wrapper_pid,
                                                              job_class=job_class, mem_gib=mem_gib)
                                        if job_class == "heavy":
                                            self._record_last_acquired_at(name, pid, slot_idx)
                                        acquired = True
                                    self._dequeue_own_entry(name, pid, token, self.queue_grant_cleanup_grace)
                                    msg = f"Acquired build slot lock for '{name}' (PID {pid})"
                                    print(msg)
                                    logger.info(msg)
                                    clean_stale_next_junction(next_dir=next_dir, cwd=cwd)
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
                        msg = f"Timed out after {timeout:.1f}s waiting for build slot lock (lane '{name}', PID {pid}): {last_refusal or 'slot capacity exhausted'}"
                        print(msg, file=sys.stderr)
                        logger.error(msg)
                        return False

                time.sleep(min(poll_interval, heartbeat_interval))
        finally:
            self._queue_op_state.cancel = previous_cancel
            # If we exited without holding the lock, remove self from queue
            if not acquired:
                # Gave up (own deadline or error): spend little time past the caller's --timeout.
                # A residue entry is swept by clean_queue once this PID is gone.
                self._dequeue_own_entry(name, pid, token, self.queue_grant_cleanup_grace)
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
        """Free the slot before output, detached cleanup, or queue waits."""
        holder = self._inherited_holder()
        if holder is not None:
            print(f"[NESTED] keeping held slot {holder['owner']}", file=sys.stderr)
            return True
        for attempt in range(_INFO_READ_ATTEMPTS):
            released, msg, detached = self._release_slot(name, token)
            for path in detached:
                _clean_detached_lock_dir(path, patience=1.0)
            if released or msg:
                break
            if attempt < _INFO_READ_ATTEMPTS - 1:
                time.sleep(_INFO_READ_RETRY_DELAY)
        if not released and not msg:
            msg = f"[ERROR] Could not read or transition lock metadata for '{name}'"
        if released:
            self._dequeue_best_effort(name, token)
        if msg:
            print(msg, file=sys.stdout if released else sys.stderr)
            (logger.info if released else logger.error)(msg)
        return released

    @_guard_slot_transition
    def _release_slot(self, name: str, token: Optional[str] = None):
        detached = []
        held_slots = []
        read_failed = False
        for slot_idx, slot_dir in enumerate(self.slot_dirs):
            if os.path.isdir(slot_dir):
                info = _read_lock_dir_info(slot_dir, slot_idx, retry=False)
                if info is None or info.get("corrupt"):
                    read_failed = True
                    continue
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
                judged = _read_lock_dir_info(s_dir, idx, retry=False)
                if judged is None or judged.get("corrupt"):
                    return False, "", detached
                expected = _lock_identity(judged)
                tombstone = f"{s_dir}.releasing-{os.getpid()}-{uuid.uuid4().hex}"
                try:
                    os.rename(s_dir, tombstone)
                except OSError:
                    return False, "", detached
                moved = _read_lock_dir_info(tombstone, idx, retry=False)
                if expected is None or _lock_identity(moved) != expected:
                    os.rename(tombstone, s_dir)
                    return False, "", detached
                detached.append(tombstone)
            return True, f"Released build slot lock for '{name}'", detached

        if read_failed:
            return False, "", detached

        if not held_slots:
            return True, f"Build slot lock is already free (release called for '{name}')", detached

        # Held, but token mismatch or non-owner
        for idx, s_dir, owner, pid, tok in held_slots:
            if owner == name and token is not None and tok and tok != token:
                msg = (
                    f"ERROR: Refusing to release build slot lock: token mismatch for "
                    f"'{name}' (held token '{tok}', release requested for '{token}')."
                )
                return False, msg, detached

        other_owners = ", ".join(f"'{o}' (PID {p})" for _, _, o, p, _ in held_slots)
        msg = f"ERROR: Refusing to release build slot lock: currently held by {other_owners}, not '{name}'."
        return False, msg, detached
    def status(self, heartbeat_stale_after: float = DEFAULT_LOCK_HEARTBEAT_STALE_SECONDS) -> Dict[str, Any]:
        """
        Returns full status dictionary and prints summary.
        Reclaims stale locks with a logged notice.
        Shows all slot holders (up to 8 slots).
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
                s_stat["job_class"] = info.get("job_class", "heavy")
                s_stat["mem_gib"] = info.get("mem_gib", 3.0)

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
        budget = self._memory_budget()
        last_acq = self._read_last_acquired_at()
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
                "job_class": item.get("job_class", "heavy"),
                "mem_gib": _reservation_gib(item.get("job_class"), item.get("mem_gib")),
                "refusal_reason": self._queue_admission(queue, item.get("token"), budget, now, last_acquired_at=last_acq)[1],
            })

        # 4. System RAM and dynamic capacity
        ram_pct = get_system_ram_percent()
        last_acq_age = round(now - last_acq, 1) if last_acq is not None else None
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
            "memory_budget": budget,
            "ram_guard_threshold": self.ram_guard_threshold,
            "acquisition_stagger": self.acquisition_stagger,
            "last_acquired_at": last_acq,
            "last_acquired_age_seconds": last_acq_age,
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
        next_dir: Optional[str] = None,
        job_class: Optional[str] = None,
        mem_gib: Optional[float] = None,
        run_timeout: float = 1800.0,
        inherit_stdin: bool = False,
    ) -> int:
        """
        Executes a command under the exclusive build slot lock.
        Holds the lock ONLY for the duration of the command, and guarantees
        release upon command completion or failure.
        Initial acquire atomically records the wrapper PID (this process, the lock's liveness source),
        creation identity, a per-run token, and initial heartbeat before launching the command.
        A failed subsequent metadata refresh (e.g. when updating child_pid or heartbeat) is safe
        and never aborts the command: the wrapper waits for command completion before releasing,
        and the lock is protected from double-grants while the command is active.
        Returns the command exit code, or 1 if lock could not be acquired.
        """
        if not math.isfinite(run_timeout) or run_timeout <= 0:
            raise ValueError("run_timeout must be positive and finite")
        job_class = job_class or classify_command(cmd)
        run_token = str(uuid.uuid4())
        runner_pid = os.getpid()

        try:
            if job_class == "browser" and classify_command(cmd) == "heavy":
                raise ValueError("browser class cannot build or serve Next")
            _check_freeze(self.run_dir)
            mem_gib = _reservation_gib(job_class, mem_gib)
            holder = self._inherited_holder()
            if holder is not None and not self._nested_allowed(holder, job_class, mem_gib):
                return 1
            acquired = holder is not None or self.acquire(
                name=name,
                timeout=timeout,
                heartbeat_stale_after=heartbeat_stale_after,
                poll_interval=poll_interval,
                force=force,
                pid=runner_pid,
                token=run_token,
                priority=priority,
                next_dir=next_dir,
                cwd=cwd,
                wrapper_pid=runner_pid,
                job_class=job_class,
                mem_gib=mem_gib,
            )
        except Exception as e:
            print(f"[RUN] Failed to acquire build slot lock for '{name}': {e}", file=sys.stderr)
            logger.error("Failed to acquire build slot lock for '%s': %s", name, e)
            return 1

        if not acquired:
            print(f"[RUN] Failed to acquire build slot lock for '{name}'.", file=sys.stderr)
            return 1

        cmd_display = " ".join(cmd)
        if holder is None:
            print(f"[RUN] Acquired build slot lock for '{name}'. Executing command: {cmd_display}", file=sys.stderr)

        # Before running the command, clean any stale Next.js standalone node_modules junction
        clean_stale_next_junction(next_dir=next_dir, cwd=cwd)

        stop_heartbeat = threading.Event()
        proc = None
        old_signals = {}
        cleanup_on_exit = None
        child_job = None
        try:
            child_env = os.environ.copy()
            child_env["BUILD_SLOT_HELD"] = json.dumps(holder or {
                "owner": name, "pid": runner_pid, "token": run_token,
                "job_class": job_class, "mem_gib": mem_gib,
            })
            node_options = child_env.get("NODE_OPTIONS", "")
            if job_class in ("heavy", "medium") and not re.search(r"--max[-_]old[-_]space[-_]size\b", node_options):
                child_env["NODE_OPTIONS"] = (node_options + f" --max-old-space-size={3072 if job_class == 'heavy' else 1536}").strip()
            if re.search(r"\bvitest\b", " ".join(cmd), re.I):
                child_env.setdefault("VITEST_MAX_WORKERS", "2")
            if sys.platform == "win32":
                child_job = _WindowsChildJob(run_token)
                proc = _WindowsJobProcess(cmd, cwd, child_env, child_job,
                                          stdin=None if inherit_stdin else subprocess.DEVNULL)
            else:
                proc = subprocess.Popen(cmd, cwd=cwd, env=child_env, start_new_session=True,
                                        stdin=None if inherit_stdin else subprocess.DEVNULL)
            child_pid = proc.pid
            def cleanup_on_exit():
                if proc.poll() is None:
                    _kill_child_tree(proc)
                if holder is None:
                    self.release(name=name, token=run_token)
            atexit.register(cleanup_on_exit)
            old_signals = {}
            if threading.current_thread() is threading.main_thread():
                def interrupted(signum, frame):
                    raise SystemExit(128 + signum)
                for sig in (signal.SIGTERM, signal.SIGINT):
                    old_signals[sig] = signal.signal(sig, interrupted)

            # Record wrapper and child PIDs and the initial heartbeat in lock info
            if holder is None:
                self._record_run_child(name=name, wrapper_pid=runner_pid, child_pid=child_pid, token=run_token)

            # Start background heartbeat while child runs
            def _heartbeat_worker():
                while not stop_heartbeat.wait(5.0):
                    try:
                        self.heartbeat_lock(name=name, token=run_token)
                    except Exception:
                        pass

            if holder is None:
                hb_thread = threading.Thread(target=_heartbeat_worker, daemon=True)
                hb_thread.start()

            try:
                return proc.wait(timeout=run_timeout)
            except subprocess.TimeoutExpired:
                print(f"[RUN] Execution timed out after {run_timeout:g}s. Killing child tree.", file=sys.stderr)
                _kill_child_tree(proc)
                return 124
        except Exception as e:
            print(f"[RUN] Error running command for '{name}': {e}", file=sys.stderr)
            logger.error("Error executing command in run_command: %s", e)
            return 1
        finally:
            stop_heartbeat.set()
            try:
                if proc is not None and proc.poll() is None:
                    _kill_child_tree(proc)
            except Exception as error:
                logger.error("Child tree cleanup failed: %s", error)
            finally:
                if child_job is not None:
                    child_job.close()
                    if proc is not None:
                        proc.close()
                if cleanup_on_exit is not None:
                    atexit.unregister(cleanup_on_exit)
                for sig, handler in old_signals.items():
                    signal.signal(sig, handler)
            if holder is None:
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

    max_slots = stat.get("max_slots", len(SLOT_LOCK_DIR_NAMES))
    guard_thresh = stat.get("ram_guard_threshold", DEFAULT_RAM_GUARD_THRESHOLD_PERCENT)
    lines.append(f"Capacity:    {max_slots} slot(s) allowed (RAM guard: {guard_thresh:.0f}%)")
    budget = stat.get("memory_budget", {})
    lines.append(
        f"Heavy jobs:  limit {budget.get('heavy_limit', _heavy_limit(stat.get('ram_percent')))}, "
        f"default/minimum {HEAVY_RESERVATION_GIB:.2f} GiB "
        f"(2 below {HEAVY_CONCURRENCY_RAM_THRESHOLD_PERCENT:.0f}% RAM, otherwise 1)"
    )

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
    stagger = stat.get("acquisition_stagger", DEFAULT_ACQUISITION_STAGGER_SECONDS)
    last_acq_age = stat.get("last_acquired_age_seconds")
    if stagger and stagger > 0:
        if last_acq_age is not None and last_acq_age < stagger:
            lines.append(f"Stagger:     WAIT ({last_acq_age:.1f}s since last acquisition, spacing: {stagger:.0f}s)")
        elif last_acq_age is not None:
            lines.append(f"Stagger:     READY ({last_acq_age:.1f}s since last acquisition, spacing: {stagger:.0f}s)")
        else:
            lines.append(f"Stagger:     READY (no prior acquisition, spacing: {stagger:.0f}s)")

    ram_str = f"{ram:.1f}%" if ram is not None else "unavailable"
    guard_thresh = stat.get("ram_guard_threshold", DEFAULT_RAM_GUARD_THRESHOLD_PERCENT)
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
    p_acq.add_argument("--class", dest="job_class", choices=MEMORY_RESERVATIONS, default=None)
    p_acq.add_argument("--mem-gib", type=float, default=None)
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
        help="Bypass the RAM guard (proceed even if system RAM >= 95%%)",
    )
    p_acq.add_argument(
        "--priority",
        action="store_true",
        help="Queue ahead of non-priority waiters, after earlier priority waiters (FIFO among priority)",
    )
    p_acq.add_argument(
        "--next-dir",
        default=None,
        help="Optional path to frontend directory for cleaning stale next standalone junctions",
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
    p_rel.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_RELEASE_TIMEOUT_SECONDS,
        help=(
            "Seconds before a stalled release gives up with exit code 124 "
            f"(default: {DEFAULT_RELEASE_TIMEOUT_SECONDS:g})"
        ),
    )

    # heartbeat <name>
    p_hb = subparsers.add_parser(
        "heartbeat",
        help="Refresh the heartbeat of the slot held by <name> (a long `acquire` build calls this to keep its slot)",
    )
    p_hb.add_argument("name", help="Lane or worker identifier holding the slot")

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
    if args.command == "run":
        raw_argv = sys.argv[1:] if argv is None else list(argv)
        sub_idx = _find_subcommand_index(raw_argv)
        sub_argv = raw_argv[sub_idx + 1:] if sub_idx >= 0 else []
        i = 0
        lane_idx = -1
        while i < len(sub_argv):
            tok = sub_argv[i]
            if tok == "--":
                break
            opt_name = tok.split("=")[0]
            if opt_name in {"--priority", "--force", "--stdin"}:
                i += 1
            elif opt_name in {"--timeout", "--cwd", "--next-dir", "--heartbeat-stale-after", "--class", "--mem-gib", "--run-timeout"}:
                if "=" in tok:
                    i += 1
                else:
                    i += 2
            elif tok.startswith("-"):
                i += 1
            else:
                lane_idx = i
                break
        if lane_idx >= 0:
            rest = sub_argv[lane_idx + 1:]
            if rest and rest[0].startswith("-") and rest[0] != "--":
                if "--" in rest:
                    sep = rest.index("--")
                    tail_parser = argparse.ArgumentParser(
                        prog="build_slot.py run <name>",
                        argument_default=argparse.SUPPRESS,
                    )
                    _add_run_options(tail_parser)
                    tail_parser.parse_args(rest[:sep], namespace=args)
                    args.cmd = rest[sep:]
                else:
                    parser.error(
                        "options must precede the lane name: build_slot.py run [--cwd DIR] [--timeout S] [--priority] <name> -- <cmd>"
                    )
    return args


def _find_subcommand_index(argv: List[str]) -> int:
    """Finds the index of the subcommand in argv, skipping global options and their values."""
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--run-dir":
            i += 2
        elif tok.startswith("--run-dir="):
            i += 1
        elif tok.startswith("-"):
            i += 1
        else:
            return i
    return -1


def _add_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--timeout", type=float, default=None, help="Maximum seconds to wait to acquire slot")
    parser.add_argument("--class", dest="job_class", choices=MEMORY_RESERVATIONS, default=None)
    parser.add_argument("--mem-gib", type=float, default=None)
    parser.add_argument("--run-timeout", type=float, default=1800.0, help="Execution deadline after acquisition")
    parser.add_argument("--stdin", action="store_true", help="Inherit caller stdin instead of null stdin")
    parser.add_argument(
        "--priority",
        action="store_true",
        help="Queue ahead of non-priority waiters, after earlier priority waiters (FIFO among priority)",
    )
    parser.add_argument("--force", action="store_true", help="Bypass RAM guard during acquisition")
    parser.add_argument("--cwd", default=None, help="Working directory to execute command in (default: current directory)")
    parser.add_argument(
        "--next-dir",
        default=None,
        help="Optional path to frontend directory for cleaning stale next standalone junctions",
    )
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
    if args.command in ("acquire", "run"):
        freeze_path = os.path.join(manager.run_dir, "build-freeze")
        if os.path.exists(freeze_path):
            try:
                with open(freeze_path, encoding="utf-8") as freeze_file:
                    reason = freeze_file.read().strip()
            except OSError:
                reason = "reason unavailable"
            print(f"build freeze active ({reason})", file=sys.stderr)
            return 75

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
            next_dir=getattr(args, "next_dir", None),
            job_class=args.job_class,
            mem_gib=args.mem_gib,
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
            next_dir=getattr(args, "next_dir", None),
            job_class=args.job_class,
            mem_gib=args.mem_gib,
            run_timeout=args.run_timeout,
            inherit_stdin=args.stdin,
        )

    elif args.command == "heartbeat":
        if manager.heartbeat_lock(name=args.name):
            print(f"Refreshed build slot heartbeat for '{args.name}'")
            return 0
        print(f"ERROR: No build slot is held by '{args.name}'.", file=sys.stderr)
        return 1

    elif args.command == "release":
        deadline_timer = _arm_deadline(args.timeout, "release")
        try:
            success = manager.release(name=args.name)
        finally:
            deadline_timer.cancel()
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
