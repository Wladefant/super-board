#!/usr/bin/env python3
"""
pr_demo_video.py - record a short demo of a change, upload it, embed it in the PR.

Pipeline (each stage fails closed; nothing is posted unless every stage passed):
  1. record   Playwright (isolated temp profile, never the operator's browser) -> .webm
  2. convert  ffmpeg -> .mp4 (h264, plays inline) + small .gif preview
  3. upload   `gh image` -> https://github.com/user-attachments/assets/<uuid>
  4. embed    Markdown block: bare MP4 URL on its own line (GitHub renders a player)
              plus the GIF preview as a Markdown image
  5. lint     evidence_lint.lint_text must report zero violations
  6. post     `gh pr comment` / `gh issue comment`, then evidence_lint.verify_posted

Usage:
  python pr_demo_video.py --url <page> --repo owner/repo --pr N --sha <40-hex> \
      [--steps steps.json] [--caption "..."] [--seconds 8] [--post] [--workdir DIR]

steps.json is a list of {"action": "click|fill|wait|scroll|goto", ...}:
  {"action":"click","selector":"text=Files changed"}  {"action":"fill","selector":"input","value":"x"}
  {"action":"wait","ms":1000}  {"action":"scroll","y":600}  {"action":"goto","url":"https://..."}

No paid services. Requires: playwright (python), ffmpeg, gh with the `gh image` extension.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import evidence_lint  # noqa: E402

ATTACHMENT_URL_RE = re.compile(r"https://github\.com/user-attachments/assets/[0-9a-fA-F-]{36}")
SHA40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
MARKER = "<!-- pr-demo-video -->"
MAX_SECONDS = 30
MIN_PLAYWRIGHT = (1, 50)  # 1.46 crashes its driver when a recording context closes (TargetClosedError)
PLAYWRIGHT_BROWSERS = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / "AppData" / "Local" / "ms-playwright")


class DemoError(RuntimeError):
    """A stage failed; nothing was posted."""


def apply_steps(page: Any, steps: List[Dict[str, Any]]) -> None:
    for i, step in enumerate(steps):
        action = step.get("action")
        if action == "click":
            page.click(step["selector"], timeout=10000)
        elif action == "fill":
            page.fill(step["selector"], step["value"], timeout=10000)
        elif action == "wait":
            page.wait_for_timeout(int(step.get("ms", 500)))
        elif action == "scroll":
            page.mouse.wheel(0, int(step.get("y", 400)))
            page.wait_for_timeout(int(step.get("ms", 400)))
        elif action == "goto":
            page.goto(step["url"], wait_until="domcontentloaded", timeout=30000)
        else:
            raise DemoError(f"step {i}: unknown action {action!r}")


def find_browser() -> Optional[str]:
    """PR_DEMO_BROWSER, else the newest installed Playwright chromium build, else None (Playwright default)."""
    env = os.environ.get("PR_DEMO_BROWSER")
    if env:
        return env
    builds = sorted(PLAYWRIGHT_BROWSERS.glob("chromium-*/chrome-win64/chrome.exe"),
                    key=lambda p: int(p.parts[-3].split("-")[-1]), reverse=True)
    return str(builds[0]) if builds else None


def check_playwright_version() -> None:
    from importlib.metadata import version
    parts = tuple(int(x) for x in version("playwright").split(".")[:2])
    if parts < MIN_PLAYWRIGHT:
        raise DemoError(f"playwright {version('playwright')} is too old (need >= {MIN_PLAYWRIGHT[0]}.{MIN_PLAYWRIGHT[1]}; "
                        "older drivers crash on video close). Use a venv with a newer playwright.")


def record(url: str, out_dir: Path, steps: List[Dict[str, Any]], seconds: int, width: int, height: int) -> Path:
    if seconds < 1 or seconds > MAX_SECONDS:
        raise DemoError(f"--seconds must be 1..{MAX_SECONDS}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise DemoError("python playwright is not installed") from e
    out_dir.mkdir(parents=True, exist_ok=True)
    check_playwright_version()
    exe = find_browser()
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path=exe) if exe else p.chromium.launch(headless=True)
        except Exception as e:
            raise DemoError(f"cannot launch Chromium ({exe or 'playwright default'}): {e}") from e
        try:
            ctx = browser.new_context(
                viewport={"width": width, "height": height},
                record_video_dir=str(out_dir),
                record_video_size={"width": width, "height": height},
            )
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(800)
            apply_steps(page, steps)
            page.wait_for_timeout(max(500, seconds * 1000 - 1500 if not steps else 800))
            video = page.video
            ctx.close()  # flushes the recording
            webm = Path(video.path())
        finally:
            browser.close()
    if not webm.exists() or webm.stat().st_size == 0:
        raise DemoError("recording produced no video file")
    return webm


def run(cmd: List[str], timeout: int) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as e:
        raise DemoError(f"{cmd[0]} timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise DemoError(f"{cmd[0]} not found") from e
    if proc.returncode != 0:
        raise DemoError(f"{' '.join(cmd[:3])} failed (exit {proc.returncode}): {proc.stderr.strip()[:300]}")
    return proc.stdout


def convert(webm: Path) -> Dict[str, Path]:
    if not shutil.which("ffmpeg"):
        raise DemoError("ffmpeg not found")
    mp4, gif = webm.with_suffix(".mp4"), webm.with_suffix(".gif")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm), "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(mp4)], 180)
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm),
         "-vf", "fps=6,scale=480:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse",
         str(gif)], 180)
    return {"mp4": mp4, "gif": gif}


def upload(path: Path, repo: str) -> str:
    """Upload one file as a GitHub user attachment; return its URL."""
    out = run(["gh", "image", "--repo", repo, str(path)], 180)
    m = ATTACHMENT_URL_RE.search(out)
    if not m:
        raise DemoError(f"gh image returned no user-attachments URL for {path.name}")
    return m.group(0)


def build_block(sha: str, mp4_url: str, gif_url: Optional[str], caption: str, page_url: str) -> str:
    if not SHA40_RE.match(sha):
        raise DemoError("--sha must be a full 40-hex commit SHA")
    lines = [MARKER, f"### Demo video (head `{sha}`)", "", caption or f"Recording of {page_url}", "", mp4_url, ""]
    if gif_url:
        lines += [f"![demo preview]({gif_url})", ""]
    lines += [f"_Recorded from {page_url} with Playwright; GIF is a preview, the MP4 above plays inline._"]
    return "\n".join(lines)


def post(kind: str, repo: str, number: int, block: str) -> str:
    sub = "pr" if kind == "pr" else "issue"
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf8") as f:
        f.write(block)
        path = f.name
    try:
        out = run(["gh", sub, "comment", str(number), "-R", repo, "-F", path], 60)
    finally:
        os.unlink(path)
    return out.strip().splitlines()[-1]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", type=int, help="PR number to comment on (use --issue for issues)")
    ap.add_argument("--issue", type=int)
    ap.add_argument("--sha", required=True, help="full head SHA the demo was recorded against")
    ap.add_argument("--steps", help="JSON file with interaction steps")
    ap.add_argument("--caption", default="")
    ap.add_argument("--seconds", type=int, default=8)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--post", action="store_true", help="post the comment (default: build and lint only)")
    args = ap.parse_args(argv)

    steps = json.loads(Path(args.steps).read_text(encoding="utf8")) if args.steps else []
    work = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="pr-demo-"))
    try:
        webm = record(args.url, work, steps, args.seconds, args.width, args.height)
        files = convert(webm)
        mp4_url = upload(files["mp4"], args.repo)
        gif_url = upload(files["gif"], args.repo)
        block = build_block(args.sha, mp4_url, gif_url, args.caption, args.url)
        violations = evidence_lint.lint_text(block)
        if violations:
            raise DemoError("evidence lint failed: " + "; ".join(str(v) for v in violations))
        print(block)
        if not args.post:
            print("\n[dry] not posted (pass --post)")
            return 0
        target_kind, number = ("pr", args.pr) if args.pr else ("issue", args.issue)
        if not number:
            raise DemoError("--post needs --pr or --issue")
        comment_url = post(target_kind, args.repo, number, block)
        print(f"posted: {comment_url}")
        passed, results = evidence_lint.verify_posted(comment_url)
        for r in results:
            print(f"{'OK  ' if r['ok'] else 'FAIL'} <{r['tag']}> {r['url'].split('?')[0]} -> {r['detail']}")
        if not passed:
            raise DemoError("posted comment does not render all media")
        print("verify-posted: PASS")
        return 0
    except DemoError as e:
        print(f"pr-demo-video: FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
