#!/usr/bin/env python3
"""admin_digest.py - daily admin digest sent through @pinthreadbot.

One Telegram message per run: Dokploy host RAM/disk/CPU (from the Dokploy metrics agent),
containers that are restarting or unhealthy (docker ps over SSH, read-only), and which public
health URLs answer (packages/admin-monitor/targets.json). Down/recovery alerts and the
threshold alerts are not sent here: the admin-monitor Worker and Dokploy do that.

  python scripts/admin_digest.py --dry-run   # print the message, send nothing
  python scripts/admin_digest.py             # send it

Secrets are read from ~/.veyyon/shared-auth and are never printed.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

AUTH = Path.home() / ".veyyon" / "shared-auth"
BOT_TOKEN_FILE = AUTH / "telegram_admin_bot_token.txt"
METRICS_TOKEN_FILE = AUTH / "dokploy_hostinger_metrics_token.txt"

SSH_HOST = "hostinger-dokploy"
CHAT_ID = "1247617658"
TARGETS = Path(__file__).resolve().parent.parent / "packages" / "admin-monitor" / "targets.json"
DOKPLOY_ADMIN = "https://hosting.wladefant.de"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
HTTP_TIMEOUT = 10
SSH_TIMEOUT = 30


def read_secret(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def host_line(m: Dict[str, Any]) -> str:
    disk_used, disk_total = float(m["diskUsed"]), float(m["totalDisk"])
    free_pct = 100.0 * (disk_total - disk_used) / disk_total
    return (f"RAM {float(m['memUsed']):.0f}% ({m['memUsedGB']} of {m['memTotal']} GB), "
            f"disk {free_pct:.0f}% free ({disk_total - disk_used:.0f} of {disk_total:.0f} GB), "
            f"CPU {float(m['cpu']):.0f}%, up {int(m['uptime']) // 86400} d")


def bad_containers(docker_ps: str) -> List[str]:
    """Lines look like 'name|Up 3 hours (unhealthy)'. Return restarting or unhealthy ones."""
    out = []
    for line in docker_ps.splitlines():
        name, _, status = line.partition("|")
        s = status.lower()
        if name and ("restarting" in s or "unhealthy" in s):
            out.append(f"{name}: {status.strip()}")
    return out


def probe(url: str) -> int:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "admin-digest"})
        opener = urllib.request.build_opener(_NoRedirect)
        return opener.open(req, timeout=HTTP_TIMEOUT).status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a 3xx counts as answering
        return None


def is_up(status: int) -> bool:
    return 0 < status < 500


def build_message(host: Optional[str], bad: Optional[List[str]], down: List[Tuple[str, int]], total: int) -> str:
    lines = ["<b>Daily admin digest</b>"]
    lines.append(f"Host: {html.escape(host)}" if host else "Host: metrics unavailable")
    if bad is None:
        lines.append("Containers: could not read docker ps")
    elif bad:
        lines.append("Containers restarting or unhealthy:")
        lines += [f"  {html.escape(b)}" for b in bad]
    else:
        lines.append("Containers: none restarting or unhealthy")
    if down:
        lines.append(f"Health URLs: {total - len(down)} of {total} up. Down:")
        lines += [f"  {html.escape(n)} (status {s})" for n, s in down]
    else:
        lines.append(f"Health URLs: all {total} up")
    lines.append(f'<a href="{DOKPLOY_ADMIN}">Open Dokploy</a>')
    return "\n".join(lines)


def get_metrics() -> Optional[Dict[str, Any]]:
    """Read the metrics agent through SSH (loopback on the host), so the token never crosses plain http."""
    try:
        cfg = f'header = "Authorization: Bearer {read_secret(METRICS_TOKEN_FILE)}"\n'
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", SSH_HOST,
                            "curl -s -m 10 -K - http://127.0.0.1:4500/metrics?limit=1"],
                           input=cfg, capture_output=True, text=True, timeout=SSH_TIMEOUT,
                           creationflags=CREATE_NO_WINDOW)
        return json.loads(p.stdout)[0] if p.returncode == 0 else None
    except Exception:
        return None


def get_host() -> Optional[str]:
    m = get_metrics()
    return host_line(m) if m else None


def get_bad_containers() -> Optional[List[str]]:
    try:
        p = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", SSH_HOST,
                            "docker ps -a --format '{{.Names}}|{{.Status}}'"],
                           capture_output=True, text=True, timeout=SSH_TIMEOUT, creationflags=CREATE_NO_WINDOW)
        return bad_containers(p.stdout or "") if p.returncode == 0 else None
    except Exception:
        return None


def send(text: str) -> None:
    body = json.dumps({"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{read_secret(BOT_TOKEN_FILE)}/sendMessage",
                                 data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=HTTP_TIMEOUT).read()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    targets = json.loads(TARGETS.read_text(encoding="utf-8"))
    down = [(t["name"], s) for t in targets for s in [probe(t["url"])] if not is_up(s)]
    msg = build_message(get_host(), get_bad_containers(), down, len(targets))
    if args.dry_run:
        print(msg)
        return 0
    send(msg)
    print("digest sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
