#!/usr/bin/env python3
"""Alert (Telegram) when the newest off-box Komo backup is missing or stale.

Runs on the operator workstation, independent of the Hostinger source host.
Reads only file names/mtimes on hetzner-ashburn1:/root/komo-backups over ssh.
No secret is read or printed; Telegram delivery uses the existing notifier.
Exit 0 = fresh, 1 = stale/unreachable (alert sent unless --dry-run).
"""
import argparse, subprocess, sys, time
from pathlib import Path

NOTIFIER = Path.home() / ".veyyon" / "workflows" / "telegram_notifier.py"
ISSUE = "https://github.com/Wladefant/super-board/issues/401"


def newest_age_hours(host: str, directory: str):
    cmd = f"ls -1t {directory}/komo-*.tar.gz.gpg 2>/dev/null | head -1 | xargs -r stat -c %Y"
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, cmd],
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        return None, f"ssh failed (exit {r.returncode})"
    out = r.stdout.strip()
    if not out:
        return None, "no backup files found"
    return (time.time() - int(out)) / 3600.0, ""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="hetzner-ashburn1")
    p.add_argument("--dir", default="/root/komo-backups")
    p.add_argument("--max-age-hours", type=float, default=26.0)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    age, err = newest_age_hours(a.host, a.dir)
    if age is not None and age <= a.max_age_hours:
        print(f"ok newest backup {age:.1f}h old")
        return 0
    reason = err or f"newest backup is {age:.1f}h old (limit {a.max_age_hours:g}h)"
    print("ALERT:", reason)
    cmd = [sys.executable, str(NOTIFIER), "--project", "super-board", "--event-type", "blocker",
           "--request-id", "komo-backup", "--summary", f"Komo backup missed: {reason}",
           "--link", ISSUE, "--dry-run" if a.dry_run else "--send"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    print("notifier exit", r.returncode, (r.stdout or "")[-300:])
    return 1


if __name__ == "__main__":
    sys.exit(main())
