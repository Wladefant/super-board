#!/usr/bin/env python3
"""
install_process_reaper_task.py - register the orphan process reaper (Windows Task Scheduler).

Task `SuperboardProcessReaper` runs process_reaper.py --live every 10 minutes.
It starts pythonw.exe directly, so no console window opens.

  python install_process_reaper_task.py --script C:\\Users\\wkiri\\.veyyon\\workflows\\process_reaper.py [--dry-run]
  python install_process_reaper_task.py --remove
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import List, Optional

TASK_NAME = "SuperboardProcessReaper"
PYTHONW = r"C:\Users\wkiri\miniconda3\pythonw.exe"
EVERY_MINUTES = "10"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def schtasks_argv(script: str, pythonw: str = PYTHONW) -> List[str]:
    if not pythonw.lower().endswith("pythonw.exe"):
        raise ValueError("the task must start pythonw.exe so that no console window opens")
    tr = f'"{pythonw}" "{script}" --live'
    if len(tr) > 250:
        raise ValueError("schtasks /tr limit is 261 characters; use a shorter path")
    return ["schtasks", "/create", "/f", "/tn", TASK_NAME, "/sc", "minute", "/mo", EVERY_MINUTES, "/tr", tr]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--script")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args(argv)
    if args.remove:
        return subprocess.run(["schtasks", "/delete", "/f", "/tn", TASK_NAME], timeout=30,
                              creationflags=CREATE_NO_WINDOW).returncode
    if not args.script:
        ap.error("--script is required")
    argv_ = schtasks_argv(args.script)
    if args.dry_run:
        print(" ".join(argv_))
        return 0
    rc = subprocess.run(argv_, timeout=30, creationflags=CREATE_NO_WINDOW).returncode
    print(f"registered {TASK_NAME}" if rc == 0 else f"schtasks failed ({rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
