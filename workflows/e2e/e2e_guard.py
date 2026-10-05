#!/usr/bin/env python3
"""Guards for the e2e integration (https://github.com/Wladefant/super-board/issues/476).

Subcommands (each prints one line per finding as `FAIL <check-id>: <detail>`; exit 1 on any):

  staged  [--repo DIR] [--allow-cache]   scan `git diff --cached` for secrets and e2e output
  tree    [--repo DIR] [--allow-cache]   same scan over tracked files (adoption audit, CI)
  config  FILE [--package-json FILE]     check an e2e.config.ts (and package.json pins)
  host    --url URL --allow HOST[,HOST]  the host check the config performs, for tests

Check ids are stable names so tests and gates can assert on them.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
PINS = json.loads((HERE / "pins.json").read_text(encoding="utf-8"))

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Paths that must never be committed, matched against the repo-relative posix path.
FORBIDDEN_PATHS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("oauth-file", re.compile(r"(^|/)(\.e2e/)?oauth[^/]*\.json$", re.I)),
    ("oauth-dir", re.compile(r"(^|/)\.config/e2e/", re.I)),
    ("env-file", re.compile(r"(^|/)\.env(\.(?!example$|sample$|defaults)[^/]*)?$", re.I)),
    ("e2e-output", re.compile(r"(^|/)\.e2e/(?!cache/)", re.I)),
    ("ai-trace", re.compile(r"(^|/)\.ai-trace(/|$)", re.I)),
]
CACHE_PATH = re.compile(r"(^|/)\.e2e/cache/", re.I)

# Secret shapes. Matched against added lines (staged) or whole files (tree).
SECRET_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("secret-api-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("secret-bearer", re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.=]{24,}")),
    ("secret-jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("secret-refresh-token", re.compile(r"refresh_token[\"']?\s*[:=]\s*[\"'][^\"']{12,}", re.I)),
    ("secret-oauth-env", re.compile(r"E2E_OAUTH_CREDENTIALS\s*=\s*\S{8,}")),
    ("secret-model-key", re.compile(r"E2E_MODEL_API_KEY\s*=\s*[^\s\$\"'][^\s]{7,}")),
    ("secret-github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("secret-private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]
MAX_SCAN_BYTES = 2_000_000

CONFIG_FORBIDDEN = [
    ("config-hosted-engine", re.compile(r"@e2e-dev/(kernel|eas)\b")),
    ("config-mobile-engine", re.compile(r"@e2e-dev/mobile\b")),
    ("config-login", re.compile(r"\be2e\s+login\b|\bloginWith\b|subscription", re.I)),
    ("config-telemetry-on", re.compile(r"E2E_TELEMETRY_DISABLED\s*=\s*[\"']?0|telemetry\s+enable", re.I)),
    ("config-literal-key", re.compile(r"apiKey\s*:\s*[\"'][^\"']{8,}[\"']")),
]


def _git(repo: Path, *args: str) -> bytes:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def check_path(path: str, allow_cache: bool) -> List[str]:
    posix = path.replace("\\", "/")
    out = []
    for check_id, pat in FORBIDDEN_PATHS:
        if pat.search(posix):
            out.append(f"FAIL {check_id}: {posix}")
    if CACHE_PATH.search(posix) and not allow_cache:
        out.append(f"FAIL e2e-cache-not-allowed: {posix} (this repo has no cache-commit rule; see POLICY.md)")
    return out


def check_text(path: str, text: str) -> List[str]:
    out = []
    for check_id, pat in SECRET_PATTERNS:
        if pat.search(text):
            out.append(f"FAIL {check_id}: {path}")
    return out


def scan_staged(repo: Path, allow_cache: bool) -> List[str]:
    names = _git(repo, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").decode("utf-8", "replace")
    findings: List[str] = []
    for name in [n for n in names.split("\0") if n]:
        findings += check_path(name, allow_cache)
        try:
            blob = _git(repo, "show", f":{name}")
        except RuntimeError:
            continue
        if len(blob) > MAX_SCAN_BYTES or b"\0" in blob[:4096]:
            continue
        findings += check_text(name, blob.decode("utf-8", "replace"))
    return findings


def scan_tree(repo: Path, allow_cache: bool) -> List[str]:
    names = _git(repo, "ls-files", "-z").decode("utf-8", "replace")
    findings: List[str] = []
    for name in [n for n in names.split("\0") if n]:
        findings += check_path(name, allow_cache)
        target = repo / name
        try:
            if target.stat().st_size > MAX_SCAN_BYTES:
                continue
            raw = target.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:4096]:
            continue
        findings += check_text(name, raw.decode("utf-8", "replace"))
    return findings


def norm_host(url_or_host: str) -> str:
    """Lowercase hostname without userinfo, port, brackets or trailing dots."""
    text = url_or_host.strip()
    parsed = urlparse(text if "://" in text else f"http://{text}")
    return (parsed.hostname or "").lower().rstrip(".")


def is_forbidden_host(host: str) -> bool:
    """Production deny-list: exact hosts, plus tokens (project refs) that may sit inside a longer host."""
    return host in PINS["forbiddenHosts"] or any(token in host for token in PINS["forbiddenHostTokens"])


def host_allowed(url: str, allowed: Iterable[str]) -> Optional[str]:
    """Return None when allowed, else the failing assertion id. Allow-list entries match the host or any subdomain."""
    host = norm_host(url)
    if is_forbidden_host(host):
        return "E2E_HOST_NOT_ALLOWED:forbidden-production-host"
    entries = [norm_host(a) for a in allowed]
    if not host or not any(host == a or host.endswith("." + a) for a in entries):
        return "E2E_HOST_NOT_ALLOWED:not-in-allow-list"
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def open_no_redirect(url: str, timeout: float = 10.0, accept: str = "*/*"):
    """GET without following redirects; a 3xx comes back as an HTTPError carrying the Location header."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={"accept": accept})
    return opener.open(req, timeout=timeout)


def check_redirects(url: str, allowed: Iterable[str], timeout: float = 10.0, max_hops: int = 5) -> Optional[str]:
    """Check the start URL and every redirect hop against the allow-list; None when all hops pass.
    An unreachable app is not a refusal: e2e itself reports it."""
    allowed = list(allowed)
    current = url
    for _ in range(max_hops + 1):
        refusal = host_allowed(current, allowed)
        if refusal:
            return f"{refusal} (hop {norm_host(current)})"
        try:
            res = open_no_redirect(current, timeout)
            code, location = res.status, res.headers.get("Location")
        except urllib.error.HTTPError as exc:
            code, location = exc.code, exc.headers.get("Location")
        except OSError:
            return None
        if code in (301, 302, 303, 307, 308) and location:
            current = urljoin(current, location)
            continue
        return None
    return "E2E_HOST_NOT_ALLOWED:too-many-redirects"


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?<!:)//.*$", "", text, flags=re.M)


def _guard_block(raw: str) -> Optional[str]:
    m = re.search(r"// BEGIN host-guard\r?\n(.*?)// END host-guard", raw, re.S)
    return m.group(1).replace("\r\n", "\n") if m else None


def check_config(config_path: Path, package_json: Optional[Path]) -> List[str]:
    raw = config_path.read_text(encoding="utf-8")
    text = _strip_comments(raw)
    findings: List[str] = []
    block = _guard_block(raw)
    if block is None:
        findings.append("FAIL config-host-guard-missing: the host allow-list block is absent")
    elif block != _guard_block((HERE / "e2e.config.template.ts").read_text(encoding="utf-8")):
        findings.append("FAIL config-host-guard-modified: the host-guard block differs from workflows/e2e/e2e.config.template.ts")
    if "E2E_CACHE_MODE" not in text or "'read-only'" not in text:
        findings.append("FAIL config-cache-default: cache must default to 'read-only' (cached replay is the default)")
    for check_id, pat in CONFIG_FORBIDDEN:
        # Comments are stripped first, so explanatory text cannot trip a check.
        if pat.search(text):
            findings.append(f"FAIL {check_id}: {config_path.name}")
    # A production host must not appear inside the allow-list literal.
    for m in re.finditer(r"(STAGING_HOSTS|ALLOWED_HOSTS)[^=\n]*=\s*\[([^\]]*)\]", text):
        for entry in re.findall(r"['\"]([^'\"]+)['\"]", m.group(2)):
            if is_forbidden_host(norm_host(entry)):
                findings.append(f"FAIL config-production-in-allow-list: {entry}")
    if package_json is not None:
        findings += check_package_pins(package_json)
    return findings


def check_package_pins(package_json: Path) -> List[str]:
    data = json.loads(package_json.read_text(encoding="utf-8"))
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    findings: List[str] = []
    for name in ("e2e", "@e2e-dev/web"):
        want = PINS["packages"][name]
        have = deps.get(name)
        if have is None:
            findings.append(f"FAIL pin-missing: {name}")
        elif have != want:
            findings.append(f"FAIL pin-mismatch: {name} is '{have}', pinned '{want}' (exact version required)")
    for name in PINS["forbiddenPackages"]:
        if name in deps:
            findings.append(f"FAIL forbidden-package: {name}")
    return findings


def _report(findings: List[str]) -> int:
    for line in findings:
        print(line)
    if findings:
        print(f"e2e_guard: {len(findings)} finding(s)")
        return 1
    print("e2e_guard: ok")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("staged", "tree"):
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".")
        p.add_argument("--allow-cache", action="store_true", help="repo rule permits committing .e2e/cache")
    p = sub.add_parser("config")
    p.add_argument("file")
    p.add_argument("--package-json")
    p = sub.add_parser("host")
    p.add_argument("--url", required=True)
    p.add_argument("--allow", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "staged":
        return _report(scan_staged(Path(args.repo), args.allow_cache))
    if args.cmd == "tree":
        return _report(scan_tree(Path(args.repo), args.allow_cache))
    if args.cmd == "config":
        return _report(check_config(Path(args.file), Path(args.package_json) if args.package_json else None))
    if args.cmd == "host":
        verdict = host_allowed(args.url, args.allow.split(","))
        if verdict:
            print(f"FAIL {verdict}: {args.url}")
            return 1
        print("e2e_guard: host ok")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
