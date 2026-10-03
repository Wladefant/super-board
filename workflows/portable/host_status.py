#!/usr/bin/env python3
r"""Host status monitor — RAM and disk with spawn-safety states.

Reports RAM usage, disk free space, and a state classification:
  - no_spawn: RAM >= 95% or disk < 20 GB — spawn nothing
  - wait:     RAM >= 90% — wait before spawning
  - reap:     RAM >= 85% or disk < 30 GB — reap before spawning
  - ok:       within safe bounds

Usage:
    python host_status.py              # human-readable
    python host_status.py --json       # machine-readable JSON
    python host_status.py --drive D:   # check a different drive (default C:\ on Windows, / on POSIX)

No external dependencies — stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from typing import Any


# ---------- RAM ----------


def _ram_windows() -> dict[str, Any]:
    """Read physical RAM via Win32 GlobalMemoryStatusEx."""
    import ctypes
    import ctypes.wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.wintypes.DWORD),
            ("dwMemoryLoad", ctypes.wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(stat)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        raise OSError("GlobalMemoryStatusEx failed")

    total = stat.ullTotalPhys
    available = stat.ullAvailPhys
    used = total - available
    percent = round((used / total) * 100, 1) if total else 0.0
    return {
        "total_gb": round(total / (1024**3), 1),
        "used_gb": round(used / (1024**3), 1),
        "available_gb": round(available / (1024**3), 1),
        "percent": percent,
    }


def _ram_linux() -> dict[str, Any]:
    """Read physical RAM from /proc/meminfo."""
    info: dict[str, int] = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                key = parts[0].rstrip(":")
                # Values in kB
                info[key] = int(parts[1]) * 1024

    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    used = total - available
    percent = round((used / total) * 100, 1) if total else 0.0
    return {
        "total_gb": round(total / (1024**3), 1),
        "used_gb": round(used / (1024**3), 1),
        "available_gb": round(available / (1024**3), 1),
        "percent": percent,
    }


def _ram_darwin() -> dict[str, Any]:
    """Read physical RAM on macOS via sysctl and vm_stat."""
    import subprocess

    try:
        out = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
        total = int(out)
        vm = subprocess.check_output(["vm_stat"], text=True)
        page_size = 4096
        free_pages = 0
        inactive_pages = 0
        speculative_pages = 0
        for line in vm.splitlines():
            if "page size of" in line:
                parts = line.split("page size of")
                if len(parts) > 1:
                    page_size = int(parts[1].split()[0])
            elif line.startswith("Pages free:"):
                free_pages = int(line.split(":")[1].strip().rstrip("."))
            elif line.startswith("Pages inactive:"):
                inactive_pages = int(line.split(":")[1].strip().rstrip("."))
            elif line.startswith("Pages speculative:"):
                speculative_pages = int(line.split(":")[1].strip().rstrip("."))
        available = (free_pages + inactive_pages + speculative_pages) * page_size
        used = max(0, total - available)
        percent = round((used / total) * 100, 1) if total else 0.0
        return {
            "total_gb": round(total / (1024**3), 1),
            "used_gb": round(used / (1024**3), 1),
            "available_gb": round(available / (1024**3), 1),
            "percent": percent,
        }
    except Exception as exc:
        raise OSError(f"Failed to read macOS RAM stats: {exc}") from exc


def get_ram() -> dict[str, Any]:
    """Return RAM stats for the current platform."""
    sys_name = platform.system()
    if sys_name == "Windows":
        return _ram_windows()
    if sys_name == "Linux":
        return _ram_linux()
    if sys_name == "Darwin":
        return _ram_darwin()
    raise OSError(f"Unsupported operating system for RAM detection: {sys_name}")


# ---------- Disk ----------


def default_drive() -> str:
    """Return default drive/path based on operating system."""
    if platform.system() == "Windows":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


def get_disk(drive: str | None = None) -> dict[str, Any]:
    """Return disk stats for the given path/drive."""
    if drive is None:
        drive = default_drive()
    elif platform.system() == "Windows":
        if drive.endswith(":"):
            drive += "\\"
        elif not drive.endswith(("\\", "/")):
            drive += "\\"

    usage = shutil.disk_usage(drive)
    total = usage.total
    free = usage.free
    used = usage.used
    percent_used = round((used / total) * 100, 1) if total else 0.0
    return {
        "drive": drive,
        "total_gb": round(total / (1024**3), 1),
        "free_gb": round(free / (1024**3), 1),
        "percent_used": percent_used,
    }


# ---------- Classification ----------

# Thresholds:
# RAM thresholds match the profile AGENTS.md policy (95% no_spawn, 90% wait, 85% reap).
# Disk thresholds are local defaults picked from the 2026-09-26 measurement (~36 GB free on C:)
# and can be tuned as host storage changes; they are not set by standing policy.
RAM_NO_SPAWN_PCT = 95
RAM_WAIT_PCT = 90
RAM_REAP_PCT = 85
DISK_NO_SPAWN_GB = 20
DISK_REAP_GB = 30


def classify(ram_percent: float, disk_free_gb: float) -> tuple[str, list[str]]:
    """Return (state, reasons) based on RAM and disk thresholds.

    Precedence order:
      1. no_spawn: RAM >= 95% or disk < 20 GB
      2. wait:     RAM >= 90%
      3. reap:     RAM >= 85% or disk < 30 GB
      4. ok:       all values within normal bounds
    """
    reasons: list[str] = []

    # 1. no_spawn check
    if ram_percent >= RAM_NO_SPAWN_PCT:
        reasons.append(f"RAM >= {RAM_NO_SPAWN_PCT}%")
    if disk_free_gb < DISK_NO_SPAWN_GB:
        reasons.append(f"disk < {DISK_NO_SPAWN_GB} GB")

    if reasons:
        return "no_spawn", reasons

    # 2. wait check
    if ram_percent >= RAM_WAIT_PCT:
        reasons.append(f"RAM >= {RAM_WAIT_PCT}%")
        if disk_free_gb < DISK_REAP_GB:
            reasons.append(f"disk < {DISK_REAP_GB} GB")
        return "wait", reasons

    # 3. reap check
    if ram_percent >= RAM_REAP_PCT:
        reasons.append(f"RAM >= {RAM_REAP_PCT}%")
    if disk_free_gb < DISK_REAP_GB:
        reasons.append(f"disk < {DISK_REAP_GB} GB")

    if reasons:
        return "reap", reasons

    return "ok", []


# ---------- Output ----------


def format_human(ram: dict[str, Any], disk: dict[str, Any], state: str, reasons: list[str]) -> str:
    """Format a human-readable status report."""
    lines = []
    reason_tag = f"  [{state}: {'; '.join(reasons)}]" if reasons else ""
    lines.append(
        f"RAM:  {ram['used_gb']:.1f} / {ram['total_gb']:.1f} GB "
        f"({ram['percent']:.0f}%){reason_tag}"
    )
    lines.append(
        f"Disk: {disk['free_gb']:.1f} / {disk['total_gb']:.1f} GB free "
        f"({disk['percent_used']:.1f}% used)  [{disk['drive']}]"
    )
    lines.append(f"State: {state}")
    return "\n".join(lines)


def format_json(ram: dict[str, Any], disk: dict[str, Any], state: str, reasons: list[str]) -> str:
    """Format a machine-readable JSON status report."""
    return json.dumps(
        {"ram": ram, "disk": disk, "state": state, "reasons": reasons},
        indent=None,
    )


# ---------- CLI ----------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        prog="host_status.py",
        description="Host status monitor — RAM and disk with spawn-safety states.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="use_json",
        help="Emit machine-readable JSON",
    )
    parser.add_argument(
        "--drive",
        default=None,
        help="Path or drive to check free space for (default: C:\\ on Windows, / on POSIX)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    ram = get_ram()
    try:
        disk = get_disk(args.drive)
    except OSError as exc:
        print(f"host_status.py: error: {exc}", file=sys.stderr)
        sys.exit(2)
    state, reasons = classify(ram["percent"], disk["free_gb"])

    if args.use_json:
        print(format_json(ram, disk, state, reasons))
    else:
        print(format_human(ram, disk, state, reasons))


if __name__ == "__main__":
    main()
