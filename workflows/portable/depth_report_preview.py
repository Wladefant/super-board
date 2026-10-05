#!/usr/bin/env python3
"""
depth_report_preview.py - serve one freshly rendered depth-survey report, for screenshot evidence.

    python workflows/portable/depth_report_preview.py --repo-root <repo to survey> [--port 4791]

At start it runs `depth_survey.survey()` on --repo-root and renders the result through
`depth_survey.find_template()`. Then it serves, on 127.0.0.1 only:

    GET /             the rendered report
    GET /api/version  {"sha", "dirty", "surveyed_sha", "template"}

`sha` is the HEAD of the checkout that holds the template and the renderer, so a capture proves which
template code drew the page. `dirty` is true when that checkout has local changes to either file.
Every response carries `x-served-sha`. `depth_report_capture.mjs` reads the SHA from here, never from
the local branch (evidence provenance rule, https://github.com/Wladefant/super-board/issues/421).
Start-up errors exit with code 2 and one line on stderr: a busy port names the port and says to pass
--port; a repo-root that is not a git checkout serves nothing and names the directory.
Tracking: https://github.com/Wladefant/super-board/issues/517, https://github.com/Wladefant/super-board/issues/560
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import depth_survey  # noqa: E402

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(root: Path, *args: str) -> Optional[str]:
    """stdout of one git command, or None when git fails (for example: not a checkout)."""
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60,
                          creationflags=_NO_WINDOW)
    return proc.stdout.strip() if proc.returncode == 0 else None


def served_identity(template: Path, renderer: Path, surveyed_sha: str) -> dict:
    """The commit that drew the page. Fails closed: `dirty` is true unless one clean checkout holds both files."""
    checkout = _git(template.parent, "rev-parse", "--show-toplevel")
    same = checkout is not None and _git(renderer.parent, "rev-parse", "--show-toplevel") == checkout
    status = _git(Path(checkout), "status", "--porcelain", "--", str(template), str(renderer)) if same else None
    return {"sha": (_git(Path(checkout), "rev-parse", "HEAD") if checkout else None) or "",
            "dirty": status != "", "surveyed_sha": surveyed_sha, "template": str(template)}


def make_handler(page: bytes, version: dict):
    body = json.dumps(version).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            path = self.path.split("?", 1)[0]
            if path == "/api/version":
                self._send(200, "application/json", body)
            elif path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page)
            else:
                self._send(404, "text/plain; charset=utf-8", b"Not found: the preview serves / and /api/version.")

        def _send(self, status: int, kind: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("content-type", kind)
            self.send_header("cache-control", "no-store")
            self.send_header("x-served-sha", version["sha"])
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args) -> None:
            pass

    return Handler


def serve_until_stopped(server: ThreadingHTTPServer) -> None:
    """Serve until shutdown() or Ctrl+C, then close the listening socket so the port is free again."""
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Serve one rendered depth-survey report with its served SHA.")
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--since-days", type=int, default=90)
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--port", type=int, default=4791)
    a = ap.parse_args(argv)

    server = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", a.port), None, bind_and_activate=False)
        server.server_bind()
        server.server_activate()
    except OSError:
        if server is not None:
            server.server_close()
        print(f"depth_report_preview: port {a.port} is busy; pass --port to choose another", file=sys.stderr)
        return 2
    repo = Path(a.repo_root).resolve()
    if not repo.is_dir() or _git(repo, "rev-parse", "--is-inside-work-tree") != "true":
        server.server_close()
        print(f"depth_report_preview: {a.repo_root} is not a git checkout", file=sys.stderr)
        return 2

    try:
        sv = depth_survey.survey(repo, a.since_days, a.limit)
        template = depth_survey.find_template()
        version = served_identity(template, Path(depth_survey.__file__).resolve(), sv.sha)
        page = depth_survey.render_report(sv, template).encode("utf-8")
    except Exception as e:
        server.server_close()
        print(f"depth_report_preview: {e}", file=sys.stderr)
        return 1

    server.RequestHandlerClass = make_handler(page, version)
    print(f"depth report preview on http://127.0.0.1:{server.server_port} serving {template} at {version['sha']}"
          f"{' (dirty)' if version['dirty'] else ''}; {len(sv.candidates)} candidate(s) from {sv.repo_root}",
          flush=True)
    serve_until_stopped(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
