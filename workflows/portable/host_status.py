#!/usr/bin/env python3
"""Host status monitor — RAM and disk with spawn-safety states.

Reports RAM usage, disk free space, and a state classification:
  - no_spawn: RAM >= 95% or disk < 10 GB — spawn nothing
  - reap:     RAM >= 85% or disk < 25 GB — reap before spawning
  - ok:       within safe bounds

Usage:
    python host_status.py              # human-readable
    python host_status.py --json       # machine-readable JSON
    python host_status.py --drive D:   # check a different drive (default C:\\)

No external dependencies — stdlib only (ctypes on Windows, /proc on Linux).
"""
from __future__ import annotations

import json
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


def get_ram() -> dict[str, Any]:
    """Return RAM stats for the current platform."""
    if platform.system() == "Windows":
        return _ram_windows()
    return _ram_linux()


# ---------- Disk ----------


def get_disk(drive: str = "C:\\") -> dict[str, Any]:
    """Return disk stats for the given path/drive."""
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

# Thresholds — these match the profile AGENTS.md policy.
RAM_NO_SPAWN_PCT = 95
RAM_REAP_PCT = 85
DISK_NO_SPAWN_GB = 10
DISK_REAP_GB = 25


def classify(ram_percent: float, disk_free_gb: float) -> tuple[str, list[str]]:
    """Return (state, reasons) based on RAM and disk thresholds."""
    reasons: list[str] = []

    if ram_percent >= RAM_NO_SPAWN_PCT:
        reasons.append(f"RAM >= {RAM_NO_SPAWN_PCT}%")
    if disk_free_gb < DISK_NO_SPAWN_GB:
        reasons.append(f"disk < {DISK_NO_SPAWN_GB} GB")

    if reasons:
        return "no_spawn", reasons

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


def main(argv: list[str] | None = None) -> None:
    args = argv if argv is not None else sys.argv[1:]

    use_json = "--json" in args
    drive = "C:\\"

    for i, arg in enumerate(args):
        if arg == "--drive" and i + 1 < len(args):
            drive = args[i + 1]
            if not drive.endswith(("\\", "/")):
                drive += "\\"

    ram = get_ram()
    disk = get_disk(drive)
    state, reasons = classify(ram["percent"], disk["free_gb"])

    if use_json:
        print(format_json(ram, disk, state, reasons))
    else:
        print(format_human(ram, disk, state, reasons))


if __name__ == "__main__":
    main()
