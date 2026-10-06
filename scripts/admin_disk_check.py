#!/usr/bin/env python3
"""admin_disk_check.py - disk alert for the Dokploy host, with hysteresis.

Alert once when free disk drops below 15%. Send one recovery message when it rises above 20%.
Between 15% and 20% nothing is sent. State lives in ~/.veyyon/run/admin-disk-state.json.
Run hourly (task SuperboardAdminDiskCheck). Use --dry-run to print instead of send.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import admin_digest as d

ALERT_BELOW = 15.0
RECOVER_ABOVE = 20.0
STATE = Path.home() / ".veyyon" / "run" / "admin-disk-state.json"


def free_pct(m: Dict[str, Any]) -> float:
    total = float(m["totalDisk"])
    return 100.0 * (total - float(m["diskUsed"])) / total


def decide(alerting: bool, free: float) -> Tuple[bool, Optional[str]]:
    """Return (new alerting state, message kind or None)."""
    if not alerting and free < ALERT_BELOW:
        return True, "alert"
    if alerting and free > RECOVER_ABOVE:
        return False, "recovered"
    return alerting, None


def message(kind: str, free: float) -> str:
    head = "<b>DISK LOW</b>" if kind == "alert" else "<b>DISK OK</b>"
    return f'{head} Dokploy host: {free:.0f}% free.\n<a href="{d.DOKPLOY_ADMIN}">Open Dokploy</a>'


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    m = d.get_metrics()
    if not m:
        print("metrics unavailable")
        return 1
    free = free_pct(m)
    try:
        alerting = bool(json.loads(STATE.read_text(encoding="utf-8")).get("alerting"))
    except Exception:
        alerting = False
    new, kind = decide(alerting, free)
    if kind:
        text = message(kind, free)
        if args.dry_run:
            print(text)
            return 0
        d.send(text)  # state is written only after a successful send, so a failed send retries
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if not args.dry_run:
        STATE.write_text(json.dumps({"alerting": new}), encoding="utf-8")
    print(f"free={free:.0f}% alerting={new} sent={kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
