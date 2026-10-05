#!/usr/bin/env python3
"""
install_upkeep_task.py - register the bounded hourly upkeep sweep (Windows Task Scheduler).

Task `SuperboardUpkeepHourly` runs `upkeep_hourly.cmd`, which
  1. fast-forwards a dedicated detached worktree of origin/main (so the task runs merged code only),
  2. runs pr_label_bot.py --live   (max 20 PR edits, super-board only),
  3. runs project_drift.py --live  (max 60 board writes, repairs super-board only,
     PolySimulator is report-only),
and appends the JSON reports to a log. Overlapping runs are ignored (IgnoreNew).

  python install_upkeep_task.py --worktree C:\\Users\\wkiri\\development\\wt-sb-upkeep [--dry-run]
  python install_upkeep_task.py --remove
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from hidden_window_audit import ensure_launcher, hidden_tr

TASK_NAME = "SuperboardUpkeepHourly"
RUN_DIR = Path(os.path.expanduser("~")) / ".veyyon" / "run" / "upkeep"
PYTHON = r"C:\Users\wkiri\miniconda3\python.exe"
SB_REPO = "Wladefant/super-board"
POLY_REPO = "Bavariance/polysimulator"


def build_cmd(worktree: str) -> str:
    log = str(RUN_DIR / "upkeep.log")
    portable = rf"{worktree}\workflows\portable"
    return "\r\n".join([
        "@echo off",
        f'git -C "{worktree}" fetch --quiet origin main || exit /b 1',
        f'git -C "{worktree}" checkout --quiet --detach origin/main || exit /b 1',
        f'echo === %DATE% %TIME% pr_label_bot >> "{log}"',
        "set RC=0",
        f'"{PYTHON}" "{portable}\\pr_label_bot.py" --repo {SB_REPO} --live --max-writes 20 >> "{log}" 2>&1 || set RC=1',
        f'echo === %DATE% %TIME% project_drift >> "{log}"',
        f'"{PYTHON}" "{portable}\\project_drift.py" --repo {SB_REPO} --repo {POLY_REPO} --repair-repo {SB_REPO} --live --max-writes 60 >> "{log}" 2>&1 || set RC=1',
        "exit /b %RC%",
        "",
    ])


def schtasks_argv(cmd_path: str) -> List[str]:
    return ["schtasks", "/create", "/f", "/tn", TASK_NAME, "/sc", "hourly", "/mo", "1", "/tr", hidden_tr(f'"{cmd_path}"')]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--worktree", help="detached worktree of origin/main the task runs from")
    ap.add_argument("--dry-run", action="store_true", help="print what would be installed, change nothing")
    ap.add_argument("--remove", action="store_true")
    args = ap.parse_args(argv)

    if args.remove:
        return subprocess.run(["schtasks", "/delete", "/f", "/tn", TASK_NAME], timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode
    if not args.worktree:
        ap.error("--worktree is required")
    cmd_path = str(RUN_DIR / "upkeep_hourly.cmd")
    body = build_cmd(args.worktree)
    if args.dry_run:
        print(f"# {cmd_path}\n{body}\n# {' '.join(schtasks_argv(cmd_path))}")
        return 0
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    ensure_launcher()
    Path(cmd_path).write_text(body, encoding="ascii", newline="")
    rc = subprocess.run(schtasks_argv(cmd_path), timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode
    print(f"registered {TASK_NAME}" if rc == 0 else f"schtasks failed ({rc})")
    return rc


if __name__ == "__main__":
    sys.exit(main())
