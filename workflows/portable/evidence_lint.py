#!/usr/bin/env python3
"""
evidence_lint.py - one supported path for PR/issue evidence media.

Supported media link forms (profile AGENTS.md section 8):
  1. GitHub user attachments: https://github.com/user-attachments/assets/<uuid>
  2. Commit-pinned raw: https://github.com/<owner>/<repo>/raw/<40-hex>/<path>

Everything else is rejected before posting: raw.githubusercontent.com, relative
or local paths, file:// URLs, third-party hosts, branch-named raw/blob refs,
release-asset URLs (404 through GitHub's image proxy on private repos), and
HTML <img>/<video> tags (broken in issue views, sanitized in comments).

The done report (profile AGENTS.md section 14, Wladefant/super-board#502): a PR body or
lane handoff must carry a `Deleted:` line and a `Not run:` line, each a list or `none`.
`Deleted: none` is flagged when the diff deletes files.

Subcommands:
  lint <file|->                     lint Markdown text, exit 1 on any violation
  done-report <file|-> [--deleted PATH ...] [--warn-only]
                                    check the two done-report lines in a body
  done-report-pr --repo R --number N [--warn-only]
                                    same check for a PR body, deletions read from the PR files
  done-report-sweep --repo R [--limit 30]
                                    dry run over recent merged PRs, always exit 0
  verify-posted <github-url> [--retries N]
                                    fetch the rendered HTML of a posted PR/issue
                                    body or comment and require every media URL
                                    to load (HTTP 200, image/* or video/*)

Pure standard library; shells out to `gh` only for verify-posted.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import List, Optional, Tuple

UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
ATTACHMENT_RE = re.compile(rf"^https://github\.com/user-attachments/(assets/{UUID}|files/\d+/[^\s]+)$")
PINNED_RAW_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+/raw/[0-9a-fA-F]{40}/[^\s]+$")

FENCE_RE = re.compile(r"^\s*(```|~~~).*?^\s*\1\s*$", re.MULTILINE | re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
HTML_MEDIA_TAG_RE = re.compile(r"<\s*(img|video|source)\b", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>)\]\"']+")
MEDIA_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|mp4|webm|mov)(\?|$)", re.IGNORECASE)


@dataclass(frozen=True)
class Violation:
    form: str
    target: str
    message: str

    def __str__(self) -> str:
        return f"[{self.form}] {self.message}: {self.target}"


def _strip_code(text: str) -> str:
    return INLINE_CODE_RE.sub("", FENCE_RE.sub("", text))


BANNED_HOSTS = frozenset({"raw.githubusercontent.com", "gist.githubusercontent.com", "i.ibb.co", "ibb.co", "imgur.com",
                          "i.imgur.com", "0x0.st", "files.catbox.moe", "postimg.cc", "i.postimg.cc"})


def _host(url: str) -> str:
    m = re.match(r"^https?://([^/:?#]+)", url.strip(), re.IGNORECASE)
    return m.group(1).lower() if m else ""


def classify_target(target: str) -> Optional[Violation]:
    """Return a Violation if `target` is not an approved media link, else None."""
    t = target.strip()
    low = t.lower()
    if _host(t) == "raw.githubusercontent.com":
        return Violation("raw-githubusercontent", t, "raw.githubusercontent.com 404s on private repos")
    if low.startswith("file:") or re.match(r"^[a-zA-Z]:[\\/]", t) or t.startswith(("\\\\", "/", "~")):
        return Violation("local-path", t, "local filesystem path")
    if not re.match(r"^https?://", low):
        return Violation("relative-path", t, "relative path does not render")
    if ATTACHMENT_RE.match(t) or PINNED_RAW_RE.match(t):
        return None
    if re.search(r"^https://github\.com/[\w.-]+/[\w.-]+/(raw|blob)/", t):
        return Violation("unpinned-ref", t, "raw/blob ref must be a full 40-hex commit SHA")
    if "/releases/download/" in low:
        return Violation("release-asset", t, "release-asset URLs 404 through the image proxy on private repos")
    return Violation("foreign-host", t, "not a GitHub user-attachment or commit-pinned raw URL")


def lint_text(text: str) -> List[Violation]:
    body = _strip_code(text)
    found: List[Violation] = []
    seen = set()

    def add(v: Optional[Violation]) -> None:
        if v and (v.form, v.target) not in seen:
            seen.add((v.form, v.target))
            found.append(v)

    for m in HTML_MEDIA_TAG_RE.finditer(body):
        add(Violation("html-media-tag", f"<{m.group(1)}>", "use Markdown image syntax or a bare user-attachments URL"))
    md_targets = set()
    for m in MD_IMAGE_RE.finditer(body):
        md_targets.add(m.group(1))
        add(classify_target(m.group(1)))
    for m in URL_RE.finditer(body):
        url = m.group(0).rstrip(".,;")
        if url in md_targets:
            continue
        if _host(url) in BANNED_HOSTS or MEDIA_EXT_RE.search(url) or "/user-attachments/" in url:
            add(classify_target(url))
    return found


# ------------------------------------------------------------ done report
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _report_line(body: str, label: str) -> Optional[str]:
    """Value of the `label:` line outside code and HTML comments; None when absent or empty."""
    text = _HTML_COMMENT_RE.sub("", _strip_code(body))
    m = re.search(rf"^[ \t]*(?:[-*][ \t]+)?(?:\*\*)?{label}:(?:\*\*)?[ \t]*(\S[^\n]*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def lint_done_report(body: str, deleted_files: Optional[List[str]] = None) -> List[Violation]:
    """Violations of the done-report contract; `deleted_files` are paths the diff removes."""
    found: List[Violation] = []
    deleted = _report_line(body, "Deleted")
    not_run = _report_line(body, "Not run")
    if deleted is None:
        found.append(Violation("missing-deleted", "Deleted:", "add a `Deleted:` line (a list, or `none`)"))
    elif deleted_files and deleted.lower().startswith("none"):
        found.append(Violation("deleted-mismatch", deleted_files[0],
                               f"says `Deleted: none` but the diff deletes {len(deleted_files)} file(s)"))
    if not_run is None:
        found.append(Violation("missing-not-run", "Not run:", "add a `Not run:` line (checks you skipped, or `none`)"))
    return found


def _gh_json(args: List[str], timeout: int = 60):
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout,
                          creationflags=_NO_WINDOW)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout)


def _deleted_paths(files: List[dict]) -> List[str]:
    return [f["path"] for f in files if f.get("changeType") == "DELETED"]


def _is_bot(pr: dict) -> bool:
    return str((pr.get("author") or {}).get("login", "")).endswith("[bot]") or (pr.get("author") or {}).get("is_bot", False)


def sweep(repo: str, limit: int) -> List[dict]:
    prs = _gh_json(["pr", "list", "--repo", repo, "--state", "merged", "--limit", str(limit),
                    "--json", "number,body,files,author,url"], timeout=120)
    rows = []
    for pr in prs:
        violations = [] if _is_bot(pr) else lint_done_report(pr.get("body") or "", _deleted_paths(pr.get("files") or []))
        rows.append({"number": pr["number"], "url": pr["url"], "bot": _is_bot(pr),
                     "violations": [v.form for v in violations]})
    return rows


# ------------------------------------------------------------ verify-posted
class _MediaCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: List[Tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("img", "video", "source"):
            d = dict(attrs)
            src = d.get("src")
            if src and src.startswith("http"):
                self.urls.append((tag, src))


def extract_media_urls(html: str) -> List[Tuple[str, str]]:
    p = _MediaCollector()
    p.feed(html)
    return p.urls


COMMENT_RE = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/(?:issues|pull)/(\d+)(?:#issuecomment-(\d+))?")


def fetch_rendered_html(url: str, timeout: int = 60) -> str:
    m = COMMENT_RE.match(url.strip())
    if not m:
        raise ValueError(f"not a GitHub issue/PR/comment URL: {url}")
    owner, repo, num, cid = m.groups()
    path = f"repos/{owner}/{repo}/issues/comments/{cid}" if cid else f"repos/{owner}/{repo}/issues/{num}"
    proc = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github.html+json", path],
        capture_output=True, text=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh api failed: {proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout).get("body_html") or ""


def check_media_url(url: str, timeout: int = 30) -> Tuple[bool, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "super-board-evidence-lint", "Range": "bytes=0-1023"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            ok = resp.status in (200, 206) and (ctype.startswith("image/") or ctype.startswith("video/"))
            return ok, f"HTTP {resp.status} {ctype}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # network failure is a verification failure, never a pass
        return False, f"error {type(e).__name__}: {e}"


def verify_posted(url: str, retries: int = 3, delay: float = 3.0) -> Tuple[bool, List[dict]]:
    html = ""
    for attempt in range(retries):
        html = fetch_rendered_html(url)
        if extract_media_urls(html):
            break
        time.sleep(delay)
    media = extract_media_urls(html)
    results = []
    for tag, src in media:
        ok, detail = check_media_url(src)
        results.append({"tag": tag, "url": src, "ok": ok, "detail": detail})
    passed = bool(results) and all(r["ok"] for r in results)
    return passed, results


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("lint")
    lp.add_argument("file", help="Markdown file, or - for stdin")
    dp = sub.add_parser("done-report")
    dp.add_argument("file", help="Markdown file, or - for stdin")
    dp.add_argument("--deleted", nargs="*", default=[], help="paths the diff deletes")
    dp.add_argument("--warn-only", action="store_true")
    pp = sub.add_parser("done-report-pr")
    pp.add_argument("--repo", required=True)
    pp.add_argument("--number", required=True)
    pp.add_argument("--warn-only", action="store_true")
    sp = sub.add_parser("done-report-sweep")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--limit", type=int, default=30)
    vp = sub.add_parser("verify-posted")
    vp.add_argument("url")
    vp.add_argument("--retries", type=int, default=3)
    args = ap.parse_args(argv)

    if args.cmd == "lint":
        text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf8").read()
        violations = lint_text(text)
        for v in violations:
            print(v)
        print(f"evidence-lint: {len(violations)} violation(s)")
        return 1 if violations else 0

    if args.cmd == "done-report-sweep":
        rows = sweep(args.repo, args.limit)
        bad = [r for r in rows if r["violations"]]
        for r in rows:
            state = "bot-exempt" if r["bot"] else ("WOULD FAIL " + ",".join(r["violations"]) if r["violations"] else "ok")
            print(f"{r['url']} {state}")
        print(f"done-report-sweep: {len(rows)} PRs, {len(rows) - len(bad)} ok, {len(bad)} would fail (dry run, exit 0)")
        return 0

    if args.cmd in ("done-report", "done-report-pr"):
        if args.cmd == "done-report":
            text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf8").read()
            deleted_files = list(args.deleted)
        else:
            pr = _gh_json(["pr", "view", args.number, "--repo", args.repo, "--json", "body,files,author"])
            if _is_bot(pr):
                print("done-report: bot author, exempt")
                return 0
            text, deleted_files = pr.get("body") or "", _deleted_paths(pr.get("files") or [])
        violations = lint_done_report(text, deleted_files)
        label = "WARN" if args.warn_only else "FAIL"
        for v in violations:
            print(f"{label} {v}")
        print(f"done-report: {len(violations)} violation(s){' (warn-only)' if args.warn_only and violations else ''}")
        return 1 if violations and not args.warn_only else 0

    passed, results = verify_posted(args.url, retries=args.retries)
    for r in results:
        print(f"{'OK  ' if r['ok'] else 'FAIL'} <{r['tag']}> {r['url'].split('?')[0]} -> {r['detail']}")
    if not results:
        print("verify-posted: no media found in the rendered HTML")
    print(f"verify-posted: {'PASS' if passed else 'FAIL'} ({len(results)} media)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
