#!/usr/bin/env python3
"""
install_hosting_audit_task.py - register the daily hosting audit (Windows Task Scheduler).

Task `SuperboardHostingAudit` runs hosting_audit.py --live once a day at 06:15.
It starts pythonw.exe directly, so no console window opens. The script edits the tracking
issue "Projects live on Dokploy" only when the audit result changed.

  python install_hosting_audit_task.py --script C:\\Users\\wkiri\\.veyyon\\workflows\\hosting_audit.py [--dry-run]
  python install_hosting_audit_task.py --remove
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import List, Optional

TASK_NAME = "SuperboardHostingAudit"
PYTHONW = r"C:\Users\wkiri\miniconda3\pythonw.exe"
START_TIME = "06:15"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def schtasks_argv(script: str, pythonw: str = PYTHONW) -> List[str]:
    if not pythonw.lower().endswith("pythonw.exe"):
        raise ValueError("the task must start pythonw.exe so that no console window opens")
    tr = f'"{pythonw}" "{script}" --live'
    if len(tr) > 250:
        raise ValueError("schtasks /tr limit is 261 characters; use a shorter path")
    return ["schtasks", "/create", "/f", "/tn", TASK_NAME, "/sc", "daily", "/st", START_TIME, "/tr", tr]


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
