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
import io
import json
import os
import math
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import List, Optional, Tuple, Union

SHOT_MIN_CHANGED_RATIO = 0.0005
GITHUB_TOKEN_HOSTS = {"github.com", "api.github.com"}


class TablePair(tuple):
    before: str
    after: str
    label: str
    reason: Optional[str]

    def __new__(cls, before: str, after: str, label: str, reason: Optional[str] = None):
        obj = super().__new__(cls, (before, after, label))
        obj.before = before
        obj.after = after
        obj.label = label
        obj.reason = reason
        return obj

    @property
    def ok(self) -> bool:
        return self.reason is None

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


class _TableCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: List[List[List[dict]]] = []
        self._cur_table: Optional[List[List[dict]]] = None
        self._cur_row: Optional[List[dict]] = None
        self._cur_cell: Optional[dict] = None
        self._cell_text: List[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attrs_d = dict(attrs)
        if tag == "table":
            self._cur_table = []
        elif tag == "tr" and self._cur_table is not None:
            self._cur_row = []
        elif tag in ("th", "td") and self._cur_row is not None:
            self._cur_cell = {"tag": tag, "imgs": [], "text": ""}
            self._cell_text = []
        elif tag in ("img", "video", "source") and self._cur_cell is not None:
            src = attrs_d.get("src")
            if src:
                self._cur_cell["imgs"].append((src, attrs_d.get("alt", "")))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("th", "td") and self._cur_cell is not None:
            self._cur_cell["text"] = "".join(self._cell_text).strip()
            if self._cur_row is not None:
                self._cur_row.append(self._cur_cell)
            self._cur_cell = None
        elif tag == "tr" and self._cur_row is not None:
            if self._cur_table is not None:
                self._cur_table.append(self._cur_row)
            self._cur_row = None
        elif tag == "table" and self._cur_table is not None:
            self.tables.append(self._cur_table)
            self._cur_table = None

    def handle_data(self, data: str) -> None:
        if self._cur_cell is not None:
            self._cell_text.append(data)


def extract_table_pairs(html: str) -> List[TablePair]:
    """
    Extract explicit before/after screenshot pairs from HTML tables.
    Requires distinct 'before' and 'after' column headers.
    Pairs images in document order within matching rows, ignoring ambiguous alt text.
    Does not mistake side-by-side galleries (e.g. mobile vs desktop) for before/after.
    Tables with unequal image counts per row fail closed with a validation reason.
    Returns list of TablePair(before_url, after_url, label).
    """
    parser = _TableCollector()
    parser.feed(html)
    pairs: List[TablePair] = []
    for table_idx, rows in enumerate(parser.tables):
        if not rows:
            continue
        header_row_idx = None
        before_col = None
        after_col = None
        label_col = None

        for r_idx, row in enumerate(rows):
            headers = [c["text"] for c in row]
            b_c = None
            a_c = None
            for c_idx, h in enumerate(headers):
                has_b = bool(re.search(r"\bbefore\b", h, re.I))
                has_a = bool(re.search(r"\bafter\b", h, re.I))
                if has_b and not has_a:
                    b_c = c_idx
                elif has_a and not has_b:
                    a_c = c_idx
            if b_c is not None and a_c is not None:
                header_row_idx = r_idx
                before_col = b_c
                after_col = a_c
                for c_idx in range(len(headers)):
                    if c_idx != b_c and c_idx != a_c:
                        label_col = c_idx
                        break
                break

        if before_col is None or after_col is None or header_row_idx is None:
            continue

        for r_idx in range(header_row_idx + 1, len(rows)):
            row = rows[r_idx]
            if len(row) <= max(before_col, after_col):
                continue
            b_cell = row[before_col]
            a_cell = row[after_col]
            row_label = (
                row[label_col]["text"]
                if label_col is not None and len(row) > label_col and row[label_col]["text"]
                else f"Table {table_idx + 1} Row {r_idx}"
            )
            b_imgs = [src for src, _ in b_cell["imgs"]]
            a_imgs = [src for src, _ in a_cell["imgs"]]
            if len(b_imgs) == len(a_imgs):
                for b_src, a_src in zip(b_imgs, a_imgs):
                    pairs.append(TablePair(b_src, a_src, row_label))
            else:
                b_src = b_imgs[0] if b_imgs else ""
                a_src = a_imgs[0] if a_imgs else ""
                reason = (
                    f"unequal-count: malformed before/after row with {len(b_imgs)} before image(s) "
                    f"and {len(a_imgs)} after image(s)"
                )
                pairs.append(TablePair(b_src, a_src, row_label, reason=reason))
    return pairs


class _AuthRedirectHandler(urllib.request.HTTPRedirectHandler):
    """HTTP redirect handler that strips Authorization headers when redirected (e.g. S3)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req:
            new_req.headers.pop("Authorization", None)
            new_req.headers.pop("authorization", None)
            if hasattr(new_req, "unredirected_hdrs"):
                new_req.unredirected_hdrs.pop("Authorization", None)
                new_req.unredirected_hdrs.pop("authorization", None)
        return new_req


def fetch_media_bytes(source: str, timeout: int = 30) -> bytes:
    """Fetch raw bytes from a local path, file:// URL, or HTTP(S) URL."""
    if source.startswith("http://") or source.startswith("https://"):
        headers = {"User-Agent": "super-board-evidence-lint"}
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        parsed = urllib.parse.urlparse(source)
        hostname = (parsed.hostname or "").lower()
        if token and hostname in GITHUB_TOKEN_HOSTS:
            headers["Authorization"] = f"token {token}"
        opener = urllib.request.build_opener(_AuthRedirectHandler())
        req = urllib.request.Request(source, headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            return resp.read()
    if source.startswith("file://"):
        parsed = urllib.parse.urlparse(source)
        path = urllib.parse.unquote(parsed.path)
        if sys.platform == "win32" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        with open(path, "rb") as f:
            return f.read()
    with open(source, "rb") as f:
        return f.read()


def compare_pair(
    before_source: Union[str, bytes],
    after_source: Union[str, bytes],
    threshold: float = SHOT_MIN_CHANGED_RATIO,
    timeout: int = 30,
) -> Tuple[bool, str, Optional[float]]:
    """
    Compare a before/after screenshot pair.
    Refuses byte-identical images, re-encoded images with identical RGB pixels,
    pairs with changed-pixel ratio < threshold, mismatched dimensions,
    and invalid thresholds (NaN, infinity, negative, or < 0.0005).
    Returns (passed, reason, changed_ratio).
    """
    if (
        threshold is None
        or not isinstance(threshold, (int, float))
        or math.isnan(threshold)
        or math.isinf(threshold)
        or threshold < SHOT_MIN_CHANGED_RATIO
    ):
        return (
            False,
            f"invalid-threshold: threshold must be a finite number >= {SHOT_MIN_CHANGED_RATIO:.6f} (got {threshold})",
            None,
        )

    if not before_source or not after_source:
        return False, "missing-source: before or after image source is empty", None
    try:
        b_bytes = before_source if isinstance(before_source, bytes) else fetch_media_bytes(before_source, timeout=timeout)
        a_bytes = after_source if isinstance(after_source, bytes) else fetch_media_bytes(after_source, timeout=timeout)
    except Exception as exc:
        return False, f"fetch-error: {exc}", None

    if b_bytes == a_bytes:
        return False, "byte-identical: before and after images are byte-for-byte identical (same sha256 / file bytes)", 0.0

    try:
        from PIL import Image, ImageChops
    except ImportError:
        return False, "dependency-missing: Pillow is required for pixel decoding and dimension comparison", None

    try:
        b_im = Image.open(io.BytesIO(b_bytes))
        a_im = Image.open(io.BytesIO(a_bytes))
    except Exception as exc:
        return False, f"decode-error: cannot decode image: {exc}", None

    if b_im.size != a_im.size:
        return (
            False,
            f"dimension-mismatch: before ({b_im.width}x{b_im.height}) and after ({a_im.width}x{a_im.height}) dimensions do not match",
            None,
        )

    b_rgb = b_im.convert("RGB")
    a_rgb = a_im.convert("RGB")
    diff = ImageChops.difference(b_rgb, a_rgb)
    r, g, b = diff.split()
    any_diff = ImageChops.lighter(ImageChops.lighter(r, g), b)
    hist = any_diff.histogram()
    total = b_im.width * b_im.height
    changed = total - hist[0]
    ratio = changed / float(total)

    if changed == 0:
        return False, "reencoded-identical: before and after decoded RGB pixels are identical (changed_ratio=0.0)", 0.0

    if ratio < threshold:
        return (
            False,
            f"near-identical: changed-pixel ratio {ratio:.6f} < {threshold:.6f} ({changed}/{total} pixels changed)",
            ratio,
        )

    return (
        True,
        f"ok: changed-pixel ratio {ratio:.6f} >= {threshold:.6f} ({changed}/{total} pixels changed)",
        ratio,
    )


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
        stdin=subprocess.DEVNULL,
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

    table_pairs = extract_table_pairs(html)
    for pair in table_pairs:
        b_src, a_src, label = pair[0], pair[1], pair[2]
        if getattr(pair, "reason", None):
            results.append({
                "tag": "pair",
                "label": label,
                "before": b_src,
                "after": a_src,
                "ok": False,
                "detail": pair.reason,
                "ratio": None,
            })
        else:
            ok, detail, ratio = compare_pair(b_src, a_src)
            results.append({
                "tag": "pair",
                "label": label,
                "before": b_src,
                "after": a_src,
                "ok": ok,
                "detail": detail,
                "ratio": ratio,
            })

    media_ok = bool(media) and all(r["ok"] for r in results if r.get("tag") != "pair")
    pairs_ok = all(r["ok"] for r in results if r.get("tag") == "pair")
    passed = media_ok and pairs_ok
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
    cp = sub.add_parser("pair", help="Compare a before/after screenshot pair")
    cp.add_argument("before", help="path or URL to before image")
    cp.add_argument("after", help="path or URL to after image")
    cp.add_argument("--threshold", type=float, default=SHOT_MIN_CHANGED_RATIO, help="minimum changed-pixel ratio")
    cp.add_argument("--timeout", type=int, default=30, help="network timeout in seconds")
    args = ap.parse_args(argv)

    if args.cmd == "lint":
        if args.file == "-":
            text = sys.stdin.read()
        else:
            with open(args.file, encoding="utf8") as f:
                text = f.read()
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
            if args.file == "-":
                text = sys.stdin.read()
            else:
                with open(args.file, encoding="utf8") as f:
                    text = f.read()
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

    if args.cmd == "pair":
        ok, reason, ratio = compare_pair(args.before, args.after, threshold=args.threshold, timeout=args.timeout)
        label = "PASS" if ok else "FAIL"
        print(f"{label} pair: {reason}")
        return 0 if ok else 1

    passed, results = verify_posted(args.url, retries=args.retries)
    for r in results:
        if r.get("tag") == "pair":
            print(f"{'OK  ' if r['ok'] else 'FAIL'} [pair] {r['label']} -> {r['detail']}")
        else:
            print(f"{'OK  ' if r['ok'] else 'FAIL'} <{r['tag']}> {r['url'].split('?')[0]} -> {r['detail']}")
    if not results:
        print("verify-posted: no media found in the rendered HTML")
    pairs_count = sum(1 for r in results if r.get("tag") == "pair")
    media_count = sum(1 for r in results if r.get("tag") != "pair")
    print(f"verify-posted: {'PASS' if passed else 'FAIL'} ({media_count} media, {pairs_count} pair(s))")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
