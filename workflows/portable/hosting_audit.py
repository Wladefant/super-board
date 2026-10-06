#!/usr/bin/env python3
"""
hosting_audit.py - track where every project runs, what it costs and what it still needs.

Reads one `hosting.json` manifest per repo (schema: docs/hosting-manifest.md), compares
it with live Dokploy and Cloudflare (read-only), prints a table plus drift findings, and
rewrites ONE marked block in the tracking issue "Projects live on Dokploy". A run that
changes nothing makes no edit.

  python hosting_audit.py            # dry run: print only, change nothing
  python hosting_audit.py --live     # also edit the tracking issue (one deduped edit)

Drift rules: host-disk-low (under 15% free), unmanifested-live-app, manifest-url-down, supabase-in-use, no-home,
stale-dokploy-id, cloudflare-unmanifested, missing-manifest, invalid-manifest.
Everything is read-only except the single issue edit under --live.
Never prints a secret. PolySimulator production is never probed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import traceback
from typing import Any, Dict, Iterable, List, Optional, Tuple


class SourceError(RuntimeError):
    """A source (Dokploy, Cloudflare) could not be read in full. The audit marks it UNCHECKED."""

SCHEMA_VERSION = 1
HOSTS = (
    "cloudflare-pages", "cloudflare-workers", "cloudflare-r2", "cloudflare-d1",
    "dokploy-app", "dokploy-db", "supabase", "other",
)
STAGES = ("greenfield", "live")
KINDS = ("web", "api", "db", "worker", "static", "storage", "other")
DNS_PROVIDERS = ("cloudflare", "hostinger", "other", "none")
LIVE_STATUSES = ("done", "running")

ISSUE_REPO = "Wladefant/super-board"
ISSUE_TITLE = "Projects live on Dokploy"
BLOCK_START = "<!-- hosting-audit:start -->"
BLOCK_END = "<!-- hosting-audit:end -->"
DEV_ROOT = Path("C:/Users/wkiri/development")
GH_OWNER = "Wladefant"
EXTRA_REPOS = ("Bavariance/polysimulator",)
# Branch that carries the manifest when it is not the default branch.
REPO_REFS = {"Bavariance/polysimulator": "staging"}
# Repos that MUST carry a hosting.json. A missing one is drift.
TRACKED_REPOS = (
    "Wladefant/super-board", "Wladefant/shipnovo", "Wladefant/komo",
    "Wladefant/veyyon", "Bavariance/polysimulator",
    "Wladefant/agent-native-platform", "Wladefant/soundcore-work-workflow",
    "Wladefant/FNSKUWarehouseScanner", "Wladefant/heylolo-app",
    "Wladefant/heylolo-website", "Wladefant/heylolo-api", "Wladefant/heylolo-hq",
    "Wladefant/heylolo-hq-api", "Wladefant/elumiai-website", "Wladefant/wladefant.de",
)
# Never probed, never audited: PolySimulator production.
PROTECTED_HOST_RE = re.compile(r"^((www|app|api)\.)?polysimulator\.com$|zaraprptkegxqpvnsubu|akamai-iad-prod", re.I)
PROTECTED_NAME_RE = re.compile(r"^\s*production(\s+iad)?\s*$", re.I)
DOKPLOY_BASE = "https://hosting.wladefant.de/api"
CF_TOKEN_FILE = Path.home() / ".veyyon" / "shared-auth" / "cloudflare_api_token.txt"
STATE_DIR = Path.home() / ".veyyon" / "run" / "hosting-audit"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
HTTP_TIMEOUT = 12

R_UNMANIFESTED = "unmanifested-live-app"
R_URL_DOWN = "manifest-url-down"
R_SUPABASE = "supabase-in-use"
R_NO_HOME = "no-home"
R_STALE_ID = "stale-dokploy-id"
R_CF_UNMANIFESTED = "cloudflare-unmanifested"
R_MISSING = "missing-manifest"
R_INVALID = "invalid-manifest"
R_DISK_LOW = "host-disk-low"

# Read-only disk source: `df -P /` on the Dokploy host over SSH (the Dokploy disk endpoints answer 404).
DOKPLOY_SSH_HOST = "hostinger-dokploy"
DISK_MIN_FREE_PCT = 15.0
SSH_TIMEOUT = 30


# ------------------------------------------------------------------ manifest schema


def validate_manifest(m: Any) -> List[str]:
    """Return a list of schema errors; empty means valid."""
    if not isinstance(m, dict):
        return ["manifest must be a JSON object"]
    errs: List[str] = []
    if m.get("schema") != SCHEMA_VERSION:
        errs.append(f"schema must be {SCHEMA_VERSION}")
    if not isinstance(m.get("project"), str) or not m["project"].strip():
        errs.append("project must be a non-empty string")
    if m.get("stage") not in STAGES:
        errs.append(f"stage must be one of {', '.join(STAGES)}")
    dns = m.get("dns")
    if not isinstance(dns, dict) or dns.get("provider") not in DNS_PROVIDERS:
        errs.append(f"dns.provider must be one of {', '.join(DNS_PROVIDERS)}")
    comps = m.get("components")
    if not isinstance(comps, list):
        errs.append("components must be a list")
        comps = []
    seen = set()
    for i, c in enumerate(comps):
        if not isinstance(c, dict):
            errs.append(f"components[{i}] must be an object")
            continue
        name = c.get("name")
        if not isinstance(name, str) or not name:
            errs.append(f"components[{i}].name must be a non-empty string")
        elif name in seen:
            errs.append(f"components[{i}].name '{name}' is duplicated")
        else:
            seen.add(name)
        if c.get("host") not in HOSTS:
            errs.append(f"components[{i}].host must be one of {', '.join(HOSTS)}")
        if c.get("kind") not in KINDS:
            errs.append(f"components[{i}].kind must be one of {', '.join(KINDS)}")
        url = c.get("url")
        if url is not None and not (isinstance(url, str) and re.match(r"^https?://", url)):
            errs.append(f"components[{i}].url must be null or an http(s) URL")
        cost = c.get("monthly_cost_usd")
        if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0):
            errs.append(f"components[{i}].monthly_cost_usd must be null or a number >= 0")
        did = c.get("dokploy_app_id")
        if did is not None and not isinstance(did, str):
            errs.append(f"components[{i}].dokploy_app_id must be null or a string")
        if str(c.get("host", "")).startswith("dokploy") and not did:
            errs.append(f"components[{i}] is on Dokploy and needs dokploy_app_id")
    sb = m.get("supabase")
    if not isinstance(sb, dict) or not isinstance(sb.get("in_use"), bool):
        errs.append("supabase.in_use must be true or false")
    elif sb["in_use"] and not isinstance(sb.get("used_for"), list):
        errs.append("supabase.used_for must be a list when in_use is true")
    step = m.get("next_migration_step")
    if step is not None and not isinstance(step, str):
        errs.append("next_migration_step must be null or a string")
    ex = m.get("exempt")
    if ex is not None and not (isinstance(ex, dict) and isinstance(ex.get("reason"), str) and ex["reason"].strip()):
        errs.append("exempt must be null or an object with a non-empty reason")
    return errs


def parse_manifest(text: str, source: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    try:
        m = json.loads(text)
    except ValueError as exc:
        return None, [f"{source}: not valid JSON ({exc.__class__.__name__})"]
    errs = validate_manifest(m)
    if errs:
        return None, [f"{source}: {e}" for e in errs]
    m["_source"] = source
    return m, []


# ------------------------------------------------------------------------ drift rules


def is_protected_host(url: Optional[str]) -> bool:
    if not url:
        return False
    host = urllib.parse.urlparse(url).hostname or ""
    return bool(PROTECTED_HOST_RE.search(host))


def _finding(rule: str, project: str, subject: str, detail: str) -> Dict[str, str]:
    return {"rule": rule, "project": project, "subject": subject, "detail": detail}


def find_drift(
    manifests: List[Dict[str, Any]],
    dokploy: Optional[List[Dict[str, Any]]],
    url_status: Dict[str, Optional[int]],
    cloudflare: Optional[List[Dict[str, str]]],
    missing_repos: Iterable[str] = (),
    invalid: Iterable[str] = (),
    disk: Optional[Tuple[int, int]] = None,
) -> List[Dict[str, str]]:
    """Pure drift evaluation. dokploy/cloudflare/disk are None when that source was unreachable.

    disk is (free_kb, total_kb) of the Dokploy host root filesystem.
    """
    out: List[Dict[str, str]] = []
    if disk is not None:
        pct = 100.0 * disk[0] / disk[1]
        if pct < DISK_MIN_FREE_PCT:
            out.append(_finding(R_DISK_LOW, "-", "dokploy-host", f"{pct:.1f}% free ({disk[0] / 1048576:.1f} GB of {disk[1] / 1048576:.1f} GB), limit {DISK_MIN_FREE_PCT:.0f}%"))
    for msg in invalid:
        out.append(_finding(R_INVALID, "-", msg.split(":", 1)[0], msg))
    for repo in missing_repos:
        out.append(_finding(R_MISSING, repo, "hosting.json", "no hosting.json in the repo"))

    manifest_ids = {}
    for m in manifests:
        for c in m["components"]:
            if c.get("dokploy_app_id"):
                manifest_ids[c["dokploy_app_id"]] = (m["project"], c["name"])

    for m in manifests:
        project = m["project"]
        comps = m["components"]
        homes = [c for c in comps if c["host"].startswith(("dokploy", "cloudflare"))]
        if not homes and not (m.get("exempt") or {}).get("reason"):
            out.append(_finding(R_NO_HOME, project, "-", "runs on neither Dokploy nor Cloudflare and gives no exempt.reason"))
        sb = m["supabase"]
        sb_comps = [c["name"] for c in comps if c["host"] == "supabase"]
        if sb["in_use"] or sb_comps:
            what = ", ".join(sb.get("used_for") or []) or "components: " + ", ".join(sb_comps)
            out.append(_finding(R_SUPABASE, project, "supabase", f"still in use for {what}"))
        for c in comps:
            url = c.get("url")
            if url and not is_protected_host(url) and url in url_status:
                st = url_status[url]
                if st is None or st >= 500:
                    out.append(_finding(R_URL_DOWN, project, c["name"], f"{url} -> {'no response' if st is None else st}"))
            if dokploy is not None and c.get("dokploy_app_id"):
                if c["dokploy_app_id"] not in {d["id"] for d in dokploy}:
                    out.append(_finding(R_STALE_ID, project, c["name"], f"Dokploy has no item {c['dokploy_app_id']}"))

    if dokploy is not None:
        for d in dokploy:
            if d["status"] not in LIVE_STATUSES or d["id"] in manifest_ids:
                continue
            if PROTECTED_NAME_RE.match(d["name"] or ""):
                continue
            hosts = (" at " + ", ".join(d.get("hosts") or [])) if d.get("hosts") else ""
            out.append(_finding(R_UNMANIFESTED, d["project"], d["name"], f"{d['kind']} {d['id']} is {d['status']} in Dokploy{hosts} and in no manifest"))

    if cloudflare is not None:
        known = {(c["host"], c["name"]) for m in manifests for c in m["components"]}
        for r in cloudflare:
            if (r["host"], r["name"]) not in known:
                out.append(_finding(R_CF_UNMANIFESTED, "-", r["name"], f"{r['host']} exists in Cloudflare and in no manifest"))
    return out


# ---------------------------------------------------------------------------- sources


def _gh(args: List[str], stdin: Optional[str] = None, timeout: int = 90) -> str:
    r = subprocess.run(
        ["gh"] + args, input=stdin, capture_output=True, text=True, timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
        stdin=None if stdin is not None else subprocess.DEVNULL,
    )
    if r.returncode != 0 or r.stdout is None:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed (rc={r.returncode})")
    return r.stdout


def local_manifests(root: Path = DEV_ROOT) -> List[Tuple[str, str]]:
    """(source, text) for each real clone (a .git directory; worktrees are skipped)."""
    found = []
    if not root.is_dir():
        return found
    for d in sorted(root.iterdir()):
        if d.name.startswith((".", "wt-")) or not (d / ".git").is_dir():
            continue
        f = d / "hosting.json"
        if f.is_file():
            found.append((f"local:{d.name}", f.read_text(encoding="utf-8")))
    return found


def github_repo_names() -> List[str]:
    rows = json.loads(_gh(["repo", "list", GH_OWNER, "--limit", "200", "--no-archived", "--json", "nameWithOwner"]))
    names = [r["nameWithOwner"] for r in rows]
    for extra in EXTRA_REPOS:
        if extra not in names:
            names.append(extra)
    return names


def github_manifests(names: List[str], batch: int = 40) -> Tuple[List[Tuple[str, str]], List[str]]:
    """One GraphQL call per `batch` repos. Returns ((source, text) found, repos that were readable)."""
    found: List[Tuple[str, str]] = []
    readable: List[str] = []
    for i in range(0, len(names), batch):
        chunk = names[i:i + batch]
        parts = []
        for j, nwo in enumerate(chunk):
            owner, name = nwo.split("/", 1)
            ref = REPO_REFS.get(nwo, "HEAD")
            parts.append(
                f'r{j}: repository(owner:{json.dumps(owner)}, name:{json.dumps(name)}) '
                f'{{ nameWithOwner object(expression:{json.dumps(ref + ":hosting.json")}) {{ ... on Blob {{ text }} }} }}'
            )
        data = json.loads(_gh(["api", "graphql", "-f", "query={" + " ".join(parts) + "}"]))
        if not isinstance(data.get("data"), dict):
            raise RuntimeError("GraphQL returned no data")
        for node in data["data"].values():
            if not node:
                continue
            readable.append(node["nameWithOwner"])
            obj = node.get("object")
            if obj and obj.get("text") is not None:
                found.append((f"github:{node['nameWithOwner']}", obj["text"]))
    return found, readable


def load_dokploy_key() -> Optional[str]:
    key = os.environ.get("DOKPLOY_API_KEY")
    if key:
        return key.strip().strip("\"'")
    for p in (Path.home() / ".veyyon" / "profiles" / "default" / "agent" / ".env", Path.home() / ".veyyon" / ".env"):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.startswith("DOKPLOY_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip("\"'")
                    if val:
                        return val
        except OSError:
            continue
    return None


def _http_json(url: str, headers: Dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "super-board-hosting-audit", **headers})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def dokploy_inventory(key: str, base: str = DOKPLOY_BASE, fetch=None) -> List[Dict[str, Any]]:
    """Applications and compose stacks from project.all, with the live host names. Read-only GETs.

    Only id, name, status and domain host names are kept: Dokploy responses also carry env secrets.
    Any failed request (401, network, bad body) raises SourceError: a partial inventory is never returned.
    """
    raw = fetch or (lambda proc, params=None: _http_json(
        f"{base}/{proc}" + ("?" + urllib.parse.urlencode(params) if params else ""), {"x-api-key": key}))

    def get(proc: str, params: Optional[Dict[str, str]] = None) -> Any:
        try:
            return raw(proc, params)
        except urllib.error.HTTPError as exc:
            raise SourceError(f"{proc} answered HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SourceError(f"{proc} failed: {exc.__class__.__name__}") from None

    projects = get("project.all")
    if not isinstance(projects, list):
        raise SourceError("project.all did not return a list")
    items: List[Dict[str, Any]] = []
    for p in projects:
        for env in p.get("environments") or []:
            for kind, field, id_key, status_key in (
                ("application", "applications", "applicationId", "applicationStatus"),
                ("compose", "compose", "composeId", "composeStatus"),
            ):
                for a in env.get(field) or []:
                    items.append({
                        "id": a.get(id_key), "name": a.get("name"), "kind": kind,
                        "status": a.get(status_key), "project": p["name"], "env": env["name"], "hosts": [],
                    })
    for it in items:
        if it["status"] not in LIVE_STATUSES or PROTECTED_NAME_RE.match(it["name"] or ""):
            continue
        proc, pkey = (("domain.byApplicationId", "applicationId") if it["kind"] == "application"
                      else ("domain.byComposeId", "composeId"))
        domains = get(proc, {pkey: it["id"]})
        if not isinstance(domains, list):
            raise SourceError(f"{proc} did not return a list")
        it["hosts"] = [d["host"] for d in domains if d.get("host")]
    return items


def _cf_pages(get, path: str) -> List[Any]:
    """Every item of a Cloudflare list endpoint. An error body is a failure, not an empty list."""
    items: List[Any] = []
    page, cursor = 1, None
    for _ in range(200):
        q = f"per_page=50&page={page}" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        body = get(f"{path}?{q}")
        if not isinstance(body, dict) or body.get("success") is not True:
            errs = body.get("errors") if isinstance(body, dict) else None
            msg = "; ".join(str(e.get("message", e)) for e in (errs or [])[:3]) or "no success flag"
            raise SourceError(f"{path} failed: {msg}")
        res = body.get("result") or []
        if isinstance(res, dict):  # r2 wraps the list in {"buckets": [...]}
            res = res.get("buckets") or []
        items += res
        info = body.get("result_info") or {}
        if info.get("cursor"):
            cursor = info["cursor"]
            continue
        if page < int(info.get("total_pages") or 1):
            page += 1
            continue
        return items
    raise SourceError(f"{path}: pagination did not end")


def cloudflare_inventory(token: str, fetch=None) -> List[Dict[str, str]]:
    """Pages, Workers, R2 and D1 names for every account the token sees. Read-only GETs."""
    api = "https://api.cloudflare.com/client/v4"
    get = fetch or (lambda path: _http_json(api + path, {"Authorization": f"Bearer {token}"}))
    out: List[Dict[str, str]] = []
    accounts = _cf_pages(get, "/accounts")
    if not accounts:
        raise SourceError("the token sees no Cloudflare account")
    for acct in accounts:
        a = acct["id"]
        for host, path, pick in (
            ("cloudflare-pages", f"/accounts/{a}/pages/projects", lambda x: x.get("name")),
            ("cloudflare-workers", f"/accounts/{a}/workers/scripts", lambda x: x.get("id")),
            ("cloudflare-r2", f"/accounts/{a}/r2/buckets", lambda x: x.get("name")),
            ("cloudflare-d1", f"/accounts/{a}/d1/database", lambda x: x.get("name")),
        ):
            out += [{"host": host, "name": pick(x)} for x in _cf_pages(get, path) if pick(x)]
    return out


def parse_df(text: str) -> Tuple[int, int]:
    """(free_kb, total_kb) from `df -P /` output. Anything else raises SourceError."""
    rows = [ln.split() for ln in text.strip().splitlines()]
    if len(rows) != 2 or len(rows[1]) < 6 or not (rows[1][1].isdigit() and rows[1][3].isdigit()):
        raise SourceError("df output not understood")
    total, free = int(rows[1][1]), int(rows[1][3])
    if total <= 0:
        raise SourceError("df reported a zero-size filesystem")
    return free, total


def host_disk(run=None) -> Tuple[int, int]:
    """Free and total KB of / on the Dokploy host. Read-only. Any failure raises SourceError."""
    run = run or (lambda argv: subprocess.run(
        argv, capture_output=True, text=True, timeout=SSH_TIMEOUT,
        creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL))
    try:
        r = run(["ssh", "-n", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", DOKPLOY_SSH_HOST, "df -P /"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError(f"ssh {exc.__class__.__name__}") from None
    if r.returncode != 0 or not r.stdout:
        raise SourceError(f"ssh df failed (rc={r.returncode})")
    return parse_df(r.stdout)


def probe_urls(urls: Iterable[str], opener=None) -> Dict[str, Optional[int]]:
    """GET each URL once. Status code, or None when nothing answered. Protected hosts are skipped."""
    todo = sorted({u for u in urls if u and not is_protected_host(u)})

    def one(u: str) -> Tuple[str, Optional[int]]:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "super-board-hosting-audit"})
            with (opener or urllib.request.urlopen)(req, timeout=HTTP_TIMEOUT) as r:
                return u, r.status
        except urllib.error.HTTPError as exc:
            return u, exc.code
        except (urllib.error.URLError, OSError, ValueError):
            return u, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        return dict(pool.map(one, todo))


# ---------------------------------------------------------------------------- render


def _money(v: Optional[float]) -> str:
    return "?" if v is None else f"{v:.2f}"


def component_rows(manifests: List[Dict[str, Any]], url_status: Dict[str, Optional[int]]) -> List[List[str]]:
    rows = []
    for m in sorted(manifests, key=lambda x: x["project"]):
        for c in m["components"]:
            url = c.get("url")
            if not url:
                st = "-"
            elif is_protected_host(url):
                st = "not probed"
            else:
                code = url_status.get(url)
                st = "down" if code is None or code >= 500 else str(code)
            rows.append([m["project"], c["name"], c["host"], url or "-", st, _money(c.get("monthly_cost_usd"))])
    return rows


def md_table(head: List[str], rows: List[List[str]]) -> str:
    esc = lambda s: str(s).replace("|", "\\|")
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def render_block(
    manifests: List[Dict[str, Any]], findings: List[Dict[str, str]], url_status: Dict[str, Optional[int]],
    notes: List[str], now_iso: str, unchecked: Optional[List[str]] = None,
) -> str:
    unchecked = unchecked or []
    total = sum(c.get("monthly_cost_usd") or 0 for m in manifests for c in m["components"])
    unknown = sum(1 for m in manifests for c in m["components"] if c.get("monthly_cost_usd") is None)
    steps = [[m["project"], m["stage"], m["dns"]["provider"], m.get("next_migration_step") or "-"]
             for m in sorted(manifests, key=lambda x: x["project"])]
    out = [
        BLOCK_START,
        f"Last audit: {now_iso}. Written by workflows/portable/hosting_audit.py. Do not edit this block by hand.",
        "",
        f"{len(manifests)} manifest(s). Known monthly cost: {total:.2f} USD ({unknown} component(s) with no cost yet).",
        "",
    ]
    if unchecked:
        out += ["### UNCHECKED", "", "The audit could not read these sources. Treat the drift list as incomplete.", ""]
        out += [f"- UNCHECKED {u}" for u in unchecked]
        out.append("")
    out += ["### Drift", ""]
    if findings:
        out.append(md_table(["Rule", "Project", "Subject", "Detail"],
                            [[f["rule"], f["project"], f["subject"], f["detail"]] for f in findings]))
    elif unchecked:
        out.append("No drift in the sources that were read. The drift check is incomplete.")
    else:
        out.append("No drift found.")
    out += ["", "### Components", "", md_table(["Project", "Component", "Host", "URL", "Status", "USD/mo"], component_rows(manifests, url_status)),
            "", "### Next migration step", "", md_table(["Project", "Stage", "DNS", "Next step"], steps)]
    if notes:
        out += ["", "### Notes", ""] + [f"- {n}" for n in notes]
    out.append(BLOCK_END)
    return "\n".join(out)


_TS_LINE = re.compile(r"^Last audit: .*$", re.M)


def block_identity(block: str) -> str:
    """Hash of a block with the timestamp line removed, so only real changes count."""
    return hashlib.sha256(_TS_LINE.sub("", block).encode("utf-8")).hexdigest()


def extract_block(body: str) -> Optional[str]:
    s, e = body.find(BLOCK_START), body.find(BLOCK_END)
    if s < 0 or e < s:
        return None
    return body[s:e + len(BLOCK_END)]


def splice_block(body: str, block: str) -> str:
    old = extract_block(body or "")
    if old is not None:
        return body.replace(old, block)
    return (body.rstrip() + "\n\n" if body.strip() else "") + block + "\n"


def plan_issue_edit(body: str, block: str) -> Optional[str]:
    """New issue body, or None when the block is already current (no edit, no noise)."""
    old = extract_block(body or "")
    if old is not None and block_identity(old) == block_identity(block):
        return None
    return splice_block(body, block)


def find_tracking_issue(number: Optional[int] = None) -> Dict[str, Any]:
    if number:
        return json.loads(_gh(["issue", "view", str(number), "-R", ISSUE_REPO, "--json", "number,title,body,state,url"]))
    rows = json.loads(_gh(["issue", "list", "-R", ISSUE_REPO, "--state", "all", "--limit", "20",
                           "--search", f'"{ISSUE_TITLE}" in:title', "--json", "number,title,body,state,url"]))
    exact = [r for r in rows if r["title"].strip() == ISSUE_TITLE]
    if not exact:
        raise RuntimeError(f'tracking issue "{ISSUE_TITLE}" not found in {ISSUE_REPO}')
    exact.sort(key=lambda r: (r["state"] != "OPEN", r["number"]))
    return exact[0]


def edit_issue_body(number: int, body: str) -> None:
    _gh(["api", "-X", "PATCH", f"repos/{ISSUE_REPO}/issues/{number}", "--input", "-"], stdin=json.dumps({"body": body}))


# ------------------------------------------------------------------------------ main


def log(msg: str) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with (STATE_DIR / "audit.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is not None:  # pythonw has no stdout
        print(line)


def collect(args: argparse.Namespace):
    """Gather everything. A source that cannot be read goes into `unchecked`, never into 'no drift'."""
    notes: List[str] = []
    unchecked: List[str] = []
    texts: Dict[str, Tuple[str, str]] = {}
    readable: List[str] = []
    try:
        names = github_repo_names()
        found, readable = github_manifests(names)
        for src, text in found:
            texts[src] = (src, text)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        unchecked.append(f"GitHub manifests: {exc}")
    for src, text in local_manifests():
        texts[src] = (src, text)

    manifests: List[Dict[str, Any]] = []
    invalid: List[str] = []
    seen: Dict[str, str] = {}
    # GitHub first: it is the default-branch truth; a local clone only fills gaps.
    for src in sorted(texts, key=lambda s: (not s.startswith("github:"), s)):
        m, errs = parse_manifest(texts[src][1], src)
        invalid += errs
        if m and m["project"] not in seen:
            seen[m["project"]] = src
            manifests.append(m)

    from_repos = {s.split(":", 1)[1] for s in texts if s.startswith("github:")}
    missing = [r for r in TRACKED_REPOS if r in readable and r not in from_repos]
    unread = [r for r in TRACKED_REPOS if r not in readable]
    if unread and readable:
        unchecked.append("tracked repos not readable with the current gh login: " + ", ".join(unread))

    dokploy = None
    key = load_dokploy_key()
    if not key:
        unchecked.append("Dokploy: DOKPLOY_API_KEY not found")
    else:
        try:
            dokploy = dokploy_inventory(key)
        except SourceError as exc:
            unchecked.append(f"Dokploy: {exc}")
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
            unchecked.append(f"Dokploy: {exc.__class__.__name__}")

    cloudflare = None
    try:
        token = CF_TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        unchecked.append("Cloudflare: token file shared-auth/cloudflare_api_token.txt is not there yet")
    else:
        try:
            cloudflare = cloudflare_inventory(token)
        except SourceError as exc:
            unchecked.append(f"Cloudflare: {exc}")
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
            unchecked.append(f"Cloudflare: {exc.__class__.__name__}")

    disk = None
    try:
        disk = host_disk()
        notes.append(f"Dokploy host disk: {100.0 * disk[0] / disk[1]:.1f}% free ({disk[0] / 1048576:.1f} GB of {disk[1] / 1048576:.1f} GB)")
    except SourceError as exc:
        unchecked.append(f"Dokploy host disk: {exc}")

    urls = [c["url"] for m in manifests for c in m["components"] if c.get("url")]
    url_status = probe_urls(urls)
    return manifests, dokploy, url_status, cloudflare, missing, invalid, notes, unchecked, disk


def run(live: bool, issue: Optional[int]) -> int:
    """Exit 0 clean, 1 tracking issue not edited, 2 some source was UNCHECKED, 3 uncaught error (see main)."""
    manifests, dokploy, url_status, cloudflare, missing, invalid, notes, unchecked, disk = collect(argparse.Namespace())
    findings = find_drift(manifests, dokploy, url_status, cloudflare, missing, invalid, disk)
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    block = render_block(manifests, findings, url_status, notes, now_iso, unchecked)
    if sys.stdout is not None:
        print(block)
    log(f"audit manifests={len(manifests)} findings={len(findings)} unchecked={len(unchecked)} live={live}")
    for u in unchecked:
        log(f"UNCHECKED: {u}")
    done = 2 if unchecked else 0
    if not live:
        log("dry run: tracking issue not edited")
        return done
    try:
        tracking = find_tracking_issue(issue)
        new_body = plan_issue_edit(tracking.get("body") or "", block)
        if new_body is None:
            log(f"tracking issue {tracking['url']} already current: no edit")
            return done
        edit_issue_body(tracking["number"], new_body)
        log(f"tracking issue {tracking['url']} edited")
        return done
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        log(f"tracking issue not edited: {exc}")
        return 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--live", action="store_true", help="edit the tracking issue (default is a dry run)")
    ap.add_argument("--issue", type=int, help="tracking issue number (default: find by title)")
    ap.add_argument("--check", nargs="+", metavar="FILE", help="validate hosting.json file(s) against the schema and exit")
    args = ap.parse_args(argv)
    try:
        if args.check:
            bad = 0
            for f in args.check:
                _, errs = parse_manifest(Path(f).read_text(encoding="utf-8"), f)
                for e in errs:
                    print(e)
                bad += bool(errs)
                if not errs:
                    print(f"{f}: ok")
            return 1 if bad else 0
        return run(args.live, args.issue)
    except Exception:  # pythonw has no console: the log file is the only place this can be seen
        log("uncaught error:\n" + traceback.format_exc())
        return 3


if __name__ == "__main__":
    sys.exit(main())
