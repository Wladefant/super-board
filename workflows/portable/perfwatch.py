#!/usr/bin/env python3
"""
perfwatch.py - scheduled performance watch for PolySimulator STAGING, Shipnovo, Pinthread and mail-hub.

Tracking issue: https://github.com/Wladefant/super-board/issues/672

One run
  1. Probes each configured target (timed GETs, Dokploy container CPU/RAM, staging Supabase logs).
  2. Compares every signal with its stored baseline (median of the last clean samples).
  3. A signal is a regression only after N=3 consecutive runs above its threshold.
  4. A confirmed regression opens ONE deduped issue in the project repo and a dispatch entry for Main.
  5. Writes digest.md (for Main) and comments on the tracking issue only when the status changed.

The script never starts a lane and never load-tests: at most 5 requests per URL, 1 s apart,
80 requests per run. Production is refused in code (see check_url). Redirects are not followed.

  python perfwatch.py run                 # dry run: prints the digest, writes NOTHING
  python perfwatch.py run --live          # scheduled form
  python perfwatch.py replay --target polysimulator/web-home --base URL --head URL
  python perfwatch.py mark-dispatched KEY --lane NAME
  python perfwatch.py reset-baseline KEY  # accept a new normal for one target (live)
  python install_perfwatch_task.py        # register the Task Scheduler job
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import gzip
import hashlib
import http.client
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import hosting_audit as ha  # noqa: E402  Dokploy key, HTTP helper, protected-host rules
import supabase_staging_health as sbh  # noqa: E402  staging PAT, scope check, logs, advisors

STATE_DIR = Path(os.path.expanduser("~")) / ".veyyon" / "run" / "perfwatch"
CONFIG_PATH = HERE / "perfwatch_config.json"
HOST_STATUS = Path("C:/Users/wkiri/.veyyon/workflows/host_status.py")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
LABEL = "perfwatch"
MARKER = "<!-- perfwatch:key={key} -->"
MARKER_RE = re.compile(r"<!-- perfwatch:key=(\S+) -->")
LOOPBACK = ("localhost", "127.0.0.1", "::1")
EXTRA_PROTECTED_RE = re.compile(r"(^|[.\-])prod(uction)?([.\-]|$)", re.I)
SCRIPT_RE = re.compile(r"""<script\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)
READ_CAP = 3_000_000
HTTP_TIMEOUT = 15
GH_TIMEOUT = 60
SSH_TIMEOUT = 60
THRESHOLD_KEYS = (
    "ttfb_p50_ms", "ttfb_p95_ms", "bytes", "bundle_bytes", "fail_count", "cpu_pct", "mem_mib",
    "slow_stmt_count", "slow_p95_ms", "edge_5xx_count", "perf_advisor_warn",
)
SLOW_SQL = (
    "select count() as n, quantile(0.95)(toFloat64OrZero(extract(event_message,'duration: ([0-9.]+) ms'))) as p95 "
    "from logs where source='postgres_logs' and event_message like '%duration:%' "
    "and toFloat64OrZero(extract(event_message,'duration: ([0-9.]+) ms')) >= 1000"
)
EDGE_5XX_SQL = (
    "select count() as n from logs where source = 'edge_logs' "
    "and toInt32OrZero(log_attributes['response.status_code']) >= 500"
)


class PerfError(Exception):
    pass


class Refused(PerfError):
    """A target, host or id that PerfWatch must never touch."""


# ------------------------------------------------------------------------ safety


def check_url(url: str, allowed_hosts: List[str], allow_loopback: bool = False) -> str:
    """Return `url` if PerfWatch may request it. Production is refused first, always."""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if not host:
        raise Refused(f"no host in {url!r}")
    if ha.PROTECTED_HOST_RE.search(host) or EXTRA_PROTECTED_RE.search(host):
        raise Refused(f"protected host refused: {host}")
    if allow_loopback and host in LOOPBACK and p.scheme in ("http", "https"):
        return url
    if p.scheme != "https":
        raise Refused(f"only https is allowed: {url!r}")
    if host not in [h.lower() for h in allowed_hosts]:
        raise Refused(f"host not in the PerfWatch config: {host}")
    return url


def validate_config(cfg: Dict[str, Any]) -> None:
    """Raise Refused or PerfError when the config could touch production or is incomplete."""
    if int(cfg.get("n_confirm", 0)) < 3:
        raise PerfError("n_confirm must be 3 or more")
    for key in THRESHOLD_KEYS:
        t = (cfg.get("thresholds") or {}).get(key)
        if not t or "rel" not in t or "abs" not in t:
            raise PerfError(f"threshold missing for {key}")
    seen = set()
    for proj in cfg.get("projects", []):
        for host in proj.get("hosts", []):
            if ha.PROTECTED_HOST_RE.search(host.lower()) or EXTRA_PROTECTED_RE.search(host.lower()):
                raise Refused(f"protected host in project {proj['name']}: {host}")
        for t in proj.get("targets", []):
            key = f"{proj['name']}/{t['name']}"
            if key in seen:
                raise PerfError(f"duplicate target {key}")
            seen.add(key)
            kind = t.get("kind")
            if kind == "http":
                check_url(t["url"], proj["hosts"])
            elif kind == "supabase":
                if t.get("ref") != sbh.STAGING_REF:
                    raise Refused(f"Supabase ref refused: {t.get('ref')!r}")
            elif kind == "containers":
                for it in t.get("items", []):
                    if not it.get("dokploy_id") or it.get("dokploy_kind") not in DOKPLOY_ONE:
                        raise PerfError(f"{key}: container item needs dokploy_id and a known dokploy_kind")
            else:
                raise PerfError(f"{key}: unknown kind {kind!r}")


# -------------------------------------------------------------------- decision logic


def percentile(values: List[float], p: float) -> float:
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


def metric_of(signal: str) -> str:
    return signal.split(":", 1)[0]


def is_breach(metric: str, value: float, baseline: float, thresholds: Dict[str, Dict[str, float]]) -> bool:
    """Above baseline by BOTH the relative and the absolute margin."""
    t = thresholds[metric]
    return value > baseline * (1 + t["rel"]) and (value - baseline) >= t["abs"]


def new_target() -> Dict[str, Any]:
    return {"status": "learning", "signals": {}, "confirmed": {}, "clean_streak": 0, "issue": None}


def new_state() -> Dict[str, Any]:
    return {"version": 1, "runs": 0, "last_run_utc": None, "targets": {}, "pending": [], "dispatch": [], "digest_hash": None}


def baseline_of(sig: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[float]:
    base = sig["base"]
    return statistics.median(base) if len(base) >= cfg["min_baseline"] else None


def _push(lst: List[Any], item: Any, cap: int) -> None:
    lst.append(item)
    del lst[:-cap]


def is_suspect_run(state: Dict[str, Any], obs: Dict[str, Dict[str, Optional[float]]], cfg: Dict[str, Any]) -> bool:
    """Circuit breaker: too many targets breach at once, so the probing host is the likely cause."""
    evaluated = breached = 0
    for key, signals in obs.items():
        t = state["targets"].get(key)
        if not t:
            continue
        has_base, hit = False, False
        for name, val in signals.items():
            sig = t["signals"].get(name)
            if val is None or not sig:
                continue
            b = baseline_of(sig, cfg)
            if b is None:
                continue
            has_base = True
            hit = hit or is_breach(metric_of(name), val, b, cfg["thresholds"])
        evaluated += has_base
        breached += has_base and hit
    return evaluated >= cfg["breaker_min_targets"] and breached / evaluated >= cfg["breaker_fraction"]


def evaluate(state: Dict[str, Any], obs: Dict[str, Dict[str, Optional[float]]], cfg: Dict[str, Any], now: str) -> Tuple[List[Dict[str, Any]], bool]:
    """Fold one run into `state`. Returns (events, suspect). Events: confirmed, signals_added, recovered."""
    n_confirm = cfg["n_confirm"]
    state["runs"] += 1
    suspect = is_suspect_run(state, obs, cfg)
    events: List[Dict[str, Any]] = []
    for key, signals in obs.items():
        t = state["targets"].setdefault(key, new_target())
        for name, val in signals.items():
            sig = t["signals"].setdefault(name, {"samples": [], "base": [], "streak": 0, "state": "learning"})
            if val is None:
                sig["state"] = "unavailable"  # neither a breach nor a recovery: the streak holds
                continue
            _push(sig["samples"], [now, val], cfg["sample_history"])
            if suspect:
                continue
            b = baseline_of(sig, cfg)
            if b is None:
                _push(sig["base"], val, cfg["baseline_window"])
                sig["state"] = "learning"
            elif is_breach(metric_of(name), val, b, cfg["thresholds"]):
                sig["streak"] += 1
                sig["state"] = "breach"
            else:
                sig["streak"] = 0
                sig["state"] = "ok"
                _push(sig["base"], val, cfg["baseline_window"])  # only clean samples shape the baseline
        if suspect:
            continue
        was_confirmed = t["status"] == "confirmed"
        newly = sorted(n for n, s in t["signals"].items() if s["streak"] >= n_confirm and n not in t["confirmed"])
        for n, s in t["signals"].items():
            if s["streak"] >= n_confirm:
                t["confirmed"][n] = {
                    "since": t["confirmed"].get(n, {}).get("since", now),
                    "baseline": statistics.median(s["base"]) if s["base"] else None,
                    "values": [v for _, v in s["samples"][-n_confirm:]],
                    "times": [ts for ts, _ in s["samples"][-n_confirm:]],
                }
        if newly:
            t["status"], t["clean_streak"] = "confirmed", 0
            events.append({"type": "signals_added" if was_confirmed else "confirmed", "key": key, "signals": newly, "at": now})
        elif was_confirmed:
            t["clean_streak"] = 0 if any(s["streak"] > 0 for s in t["signals"].values()) else t["clean_streak"] + 1
            if t["clean_streak"] >= n_confirm:
                events.append({"type": "recovered", "key": key, "signals": sorted(t["confirmed"]), "at": now})
                t["status"], t["confirmed"], t["clean_streak"] = "ok", {}, 0
        else:
            states = [s["state"] for s in t["signals"].values() if s["state"] != "unavailable"]
            if any(s["streak"] > 0 for s in t["signals"].values()):
                t["status"] = "watching"
            elif states and all(s == "learning" for s in states):
                t["status"] = "learning"
            else:
                t["status"] = "ok"
    return events, suspect


# ------------------------------------------------------------------------- probes


class Budget:
    def __init__(self, total: int) -> None:
        self.left = total

    def take(self, n: int = 1) -> bool:
        if self.left < n:
            return False
        self.left -= n
        return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a: Any, **k: Any) -> None:  # a 3xx is a result, never a hop to another host
        return None


def make_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


def http_request(url: str, opener: urllib.request.OpenerDirector, timeout: float = HTTP_TIMEOUT) -> Dict[str, Any]:
    """One GET. ttfb_ms = time until the response headers arrived. raw_len = transfer size (not decoded)."""
    req = urllib.request.Request(url, headers={"User-Agent": "super-board-perfwatch/1", "Accept-Encoding": "gzip"})
    start = time.perf_counter()
    out: Dict[str, Any] = {"status": None, "ttfb_ms": None, "raw_len": 0, "body": b"", "headers": {}, "error": None}
    try:
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            resp = exc
        out["ttfb_ms"] = (time.perf_counter() - start) * 1000
        out["status"] = resp.status if hasattr(resp, "status") else resp.code
        raw = resp.read(READ_CAP)
        out["raw_len"], out["body"] = len(raw), raw
        hdr = resp.headers
        out["headers"] = {k: hdr.get(k) for k in ("content-encoding", "cf-cache-status", "age", "content-type") if hdr.get(k)}
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        out["error"] = exc.__class__.__name__
    return out


def response_ok(r: Dict[str, Any], access_gated: bool) -> bool:
    s = r["status"]
    return s is not None and (200 <= s < 300 or (access_gated and s < 500))


def _html(r: Dict[str, Any]) -> str:
    body = r["body"]
    if r["headers"].get("content-encoding") == "gzip":
        try:
            body = gzip.decompress(body)
        except (OSError, EOFError):
            return ""
    return body.decode("utf-8", errors="replace")


def bundle_bytes(page_url: str, html: str, hosts: List[str], cfg: Dict[str, Any], budget: Budget,
                 opener: urllib.request.OpenerDirector, notes: List[str]) -> Optional[float]:
    """Transfer size of the page's same-origin scripts. None when any asset could not be read."""
    assets: List[str] = []
    for src in SCRIPT_RE.findall(html):
        full = urllib.parse.urljoin(page_url, src)
        try:
            check_url(full, hosts)
        except Refused:
            continue  # another origin: not this project's bundle
        if full not in assets:
            assets.append(full)
    assets = assets[: cfg["max_bundle_assets"]]
    if not assets:
        return None
    total = 0
    for a in assets:
        if not budget.take():
            notes.append("request budget exhausted before the bundle was read")
            return None
        r = http_request(a, opener)
        if r["status"] != 200:
            notes.append(f"bundle asset failed: {urllib.parse.urlparse(a).path[-60:]} -> {r['status'] or r['error']}")
            return None
        total += r["raw_len"]
    return float(total)


def probe_http(proj: Dict[str, Any], t: Dict[str, Any], cfg: Dict[str, Any], budget: Budget,
               opener: Optional[urllib.request.OpenerDirector] = None, sleep: Callable[[float], None] = time.sleep
               ) -> Tuple[Dict[str, Optional[float]], List[str]]:
    url = check_url(t["url"], proj["hosts"])
    opener = opener or make_opener()
    gated = bool(t.get("access_gated"))
    notes: List[str] = []
    results: List[Dict[str, Any]] = []
    for i in range(cfg["probes_per_url"]):
        if not budget.take():
            notes.append("request budget exhausted")
            break
        if i:
            sleep(cfg["probe_gap_s"])
        results.append(http_request(url, opener))
    sig: Dict[str, Optional[float]] = {"fail_count": None, "ttfb_p50_ms": None, "ttfb_p95_ms": None, "bytes": None}
    if not results:
        return sig, notes
    good = [r for r in results if response_ok(r, gated)]
    sig["fail_count"] = float(len(results) - len(good))
    if good:
        ttfb = [r["ttfb_ms"] for r in good]
        sig["ttfb_p50_ms"], sig["ttfb_p95_ms"] = statistics.median(ttfb), percentile(ttfb, 95)
        sig["bytes"] = float(statistics.median([r["raw_len"] for r in good]))
    if t.get("bundle"):
        page = next((r for r in good if r["status"] == 200), None)
        sig["bundle_bytes"] = bundle_bytes(url, _html(page), proj["hosts"], cfg, budget, opener, notes) if page else None
    return sig, notes


DOKPLOY_ONE = {"application": "applicationId", "postgres": "postgresId"}


def resolve_app_names(items: List[Dict[str, Any]], key: str, fetch: Optional[Callable[[str, Dict[str, str]], Any]] = None) -> Dict[str, str]:
    """item name -> Dokploy appName. Only the name is kept: Dokploy answers also carry env secrets."""
    get = fetch or (lambda proc, params: ha._http_json(f"{ha.DOKPLOY_BASE}/{proc}?{urllib.parse.urlencode(params)}", {"x-api-key": key}))
    out: Dict[str, str] = {}
    for it in items:
        kind = it["dokploy_kind"]
        one = get(f"{kind}.one", {DOKPLOY_ONE[kind]: it["dokploy_id"]})
        if not isinstance(one, dict) or not one.get("appName"):
            raise PerfError(f"Dokploy {kind}.one gave no appName for {it['name']}")
        if ha.PROTECTED_NAME_RE.match(one.get("name") or ""):
            raise Refused(f"Dokploy item {it['name']} is a Production-named item")
        out[it["name"]] = one["appName"]
    return out


def to_mib(text: str) -> float:
    m = re.match(r"\s*([0-9.]+)\s*([KMG]i?B|B)\s*", text)
    if not m:
        raise PerfError(f"cannot read memory value {text!r}")
    return float(m.group(1)) * {"B": 1 / 1048576, "KB": 1 / 1024, "KiB": 1 / 1024, "MB": 1, "MiB": 1, "GB": 1024, "GiB": 1024}[m.group(2)]


def parse_docker_stats(text: str) -> List[Dict[str, Any]]:
    rows = []
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3:
            continue
        try:
            rows.append({"name": parts[0], "cpu_pct": float(parts[1].rstrip("%")), "mem_mib": to_mib(parts[2].split("/")[0])})
        except (ValueError, PerfError):
            continue
    return rows


def docker_stats(run: Optional[Callable[..., Any]] = None) -> List[Dict[str, Any]]:
    runner = run or subprocess.run
    r = runner(
        ["ssh", "-n", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout=10", ha.DOKPLOY_SSH_HOST,
         "docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}'"],
        capture_output=True, text=True, timeout=SSH_TIMEOUT, creationflags=CREATE_NO_WINDOW,
    )
    if r.returncode != 0:
        raise PerfError(f"docker stats over ssh failed rc={r.returncode}")
    return parse_docker_stats(r.stdout)


def container_signals(names: Dict[str, str], stats: List[Dict[str, Any]]) -> Tuple[Dict[str, Optional[float]], List[str]]:
    sig: Dict[str, Optional[float]] = {}
    notes: List[str] = []
    for item, app in names.items():
        rows = [s for s in stats if s["name"] == app or s["name"].startswith(app + ".") or s["name"].startswith(app + "-")]
        if not rows:
            sig[f"cpu_pct:{item}"] = sig[f"mem_mib:{item}"] = None
            notes.append(f"{item}: no running container on the Dokploy host")
            continue
        sig[f"cpu_pct:{item}"] = sum(r["cpu_pct"] for r in rows)
        sig[f"mem_mib:{item}"] = sum(r["mem_mib"] for r in rows)
    return sig, notes


def probe_supabase(t: Dict[str, Any]) -> Tuple[Dict[str, Optional[float]], List[str]]:
    if t.get("ref") != sbh.STAGING_REF:
        raise Refused(f"Supabase ref refused: {t.get('ref')!r}")
    pat = sbh.load_pat()
    sbh.verify_staging_only(pat)
    start, end = sbh.log_window(hours=int(t.get("window_hours", 3)))
    slow = sbh.query_logs(pat, SLOW_SQL, start, end)
    edge = sbh.query_logs(pat, EDGE_5XX_SQL, start, end)
    lints = sbh.api_get(pat, f"/v1/projects/{sbh.STAGING_REF}/advisors/performance").get("lints", [])
    row = slow[0] if slow else {}
    return {
        "slow_stmt_count": float(row.get("n", 0)),
        "slow_p95_ms": float(row.get("p95", 0) or 0),
        "edge_5xx_count": float((edge[0] if edge else {}).get("n", 0)),
        "perf_advisor_warn": float(sum(1 for l in lints if l.get("level") in ("WARN", "ERROR"))),
    }, [f"log window {start} .. {end}"]


def collect_all(cfg: Dict[str, Any], budget: Budget) -> Tuple[Dict[str, Dict[str, Optional[float]]], Dict[str, List[str]]]:
    """Every source is read on its own. A failed source becomes `unavailable`, never a zero."""
    obs: Dict[str, Dict[str, Optional[float]]] = {}
    notes: Dict[str, List[str]] = {}
    stats_cache: Dict[str, Any] = {}
    for proj in cfg["projects"]:
        for t in proj["targets"]:
            key = f"{proj['name']}/{t['name']}"
            try:
                if t["kind"] == "http":
                    obs[key], notes[key] = probe_http(proj, t, cfg, budget)
                elif t["kind"] == "supabase":
                    obs[key], notes[key] = probe_supabase(t)
                else:
                    dkey = ha.load_dokploy_key()
                    if not dkey:
                        raise PerfError("DOKPLOY_API_KEY not found")
                    names = resolve_app_names(t["items"], dkey)
                    if "stats" not in stats_cache:
                        stats_cache["stats"] = docker_stats()
                    obs[key], notes[key] = container_signals(names, stats_cache["stats"])
            except (PerfError, sbh.HealthError, ha.SourceError, urllib.error.URLError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
                if isinstance(exc, Refused):
                    raise
                obs[key] = {}
                notes[key] = [f"source unavailable: {exc.__class__.__name__}: {str(exc)[:120]}"]
    return obs, notes


def host_state(run: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """host_status.py verdict. `unknown` when it cannot be read; dispatch then stays deferred."""
    py = Path(sys.executable)
    if py.name.lower().startswith("pythonw"):  # pythonw has no stdout pipe for the child's JSON
        py = py.with_name("python.exe") if py.with_name("python.exe").exists() else py
    try:
        r = (run or subprocess.run)([str(py), str(HOST_STATUS), "--json"], capture_output=True, text=True, timeout=20,
                                    creationflags=CREATE_NO_WINDOW)
        data = json.loads(r.stdout)
        return {"state": data.get("state", "unknown"), "ram_pct": (data.get("ram") or {}).get("percent")}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {"state": "unknown", "ram_pct": None}


# --------------------------------------------------------------------------- GitHub


def default_gh(args: List[str], inp: Optional[str] = None) -> str:
    r = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=GH_TIMEOUT, input=inp,
                       stdin=None if inp is not None else subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        raise PerfError(f"gh {' '.join(args[:2])} failed rc={r.returncode}: {(r.stderr or '')[:160]}")
    return r.stdout


def key_parts(key: str) -> Tuple[str, str]:
    proj, _, target = key.partition("/")
    return proj, target


def project_repo(cfg: Dict[str, Any], key: str) -> str:
    proj = key_parts(key)[0]
    return next(p["repo"] for p in cfg["projects"] if p["name"] == proj)


def fmt_val(metric: str, v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    if metric.endswith("_ms"):
        return f"{v:.0f} ms"
    if metric in ("bytes", "bundle_bytes"):
        return f"{v / 1024:.1f} KB"
    if metric == "mem_mib":
        return f"{v:.0f} MiB"
    if metric == "cpu_pct":
        return f"{v:.1f} %"
    return f"{v:.0f}"


def render_issue(key: str, target: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[str, str]:
    proj, name = key_parts(key)
    sigs = sorted(target["confirmed"])
    rows = []
    for s in sigs:
        c = target["confirmed"][s]
        m = metric_of(s)
        rows.append(f"| `{s}` | {fmt_val(m, c['baseline'])} | {' / '.join(fmt_val(m, v) for v in c['values'])} | "
                    f"{', '.join(c['times'])} |")
    n = cfg["n_confirm"]
    tgt = next(t for p in cfg["projects"] if p["name"] == proj for t in p["targets"] if t["name"] == name)
    replay_path = ""
    if tgt["kind"] == "http":
        replay_path = (f"\nReplay (same requests, base and head alternating, same cache state):\n\n```\n"
                       f"python C:/Users/wkiri/.veyyon/workflows/perfwatch.py replay --target {key} --base <base-url> --head <head-url>\n```\n")
    title = f"PerfWatch: {key} regressed ({', '.join(sigs)})"
    body = f"""{MARKER.format(key=key)}
{key} is slower than its baseline in {n} runs in a row. PerfWatch opened this issue. It will not open another one for the same key.

| Signal | Baseline | Last {n} values | Run times (UTC) |
|---|---|---|---|
{chr(10).join(rows)}

Source: https://github.com/Wladefant/super-board/issues/672
{replay_path}
## Lane brief

Investigate the cause. Fix it on a branch. Open a PR whose body has exactly these four sections:

```
## Problem
<signal, target, baseline, the {n} confirmed values and run times>
## Solution
<what changed and why it moves the signal>
## Before
<replay table, base>
## After
<replay table, head>
```

Rules: staging and local builds only. Never production. Measure Before and After with the replay command above so both sides see the same requests and cache state. One noisy sample is not proof: report the table. Close this issue only when the fix is merged and a later PerfWatch run shows {n} clean runs.
"""
    return title, body


class Publisher:
    """Turns pending events into GitHub writes. Every write counts against the per-run cap."""

    def __init__(self, cfg: Dict[str, Any], gh: Callable[..., str], dry: bool = False) -> None:
        self.cfg, self.gh, self.dry = cfg, gh, dry
        self.writes = 0
        self.actions: List[str] = []

    def _can_write(self) -> bool:
        return self.writes < self.cfg["caps"]["issue_writes"]

    def _api(self, args: List[str], payload: Optional[Dict[str, Any]] = None) -> Any:
        out = self.gh(["api"] + args + (["--input", "-"] if payload is not None else []), json.dumps(payload) if payload is not None else None)
        return json.loads(out) if out.strip() else None

    def find_issue(self, repo: str, key: str) -> Optional[Dict[str, Any]]:
        rows = self._api([f"repos/{repo}/issues?state=all&labels={LABEL}&per_page=50"]) or []
        marker = MARKER.format(key=key)
        hits = [r for r in rows if "pull_request" not in r and marker in (r.get("body") or "")]
        hits.sort(key=lambda r: (r["state"] != "open", -r["number"]))
        return hits[0] if hits else None

    def publish(self, state: Dict[str, Any]) -> None:
        keep: List[Dict[str, Any]] = []
        for ev in state["pending"]:
            if not self._can_write():
                keep.append(ev)
                continue
            try:
                done = self._one(state, ev)
            except PerfError as exc:
                self.actions.append(f"ERROR {ev['type']} {ev['key']}: {exc}")
                done = False
            if not done:
                keep.append(ev)
        state["pending"] = keep

    def _one(self, state: Dict[str, Any], ev: Dict[str, Any]) -> bool:
        key, t = ev["key"], state["targets"][ev["key"]]
        repo = project_repo(self.cfg, key)
        if ev["type"] == "recovered":
            if t["issue"]:
                self.actions.append(f"comment on {t['issue']['url']}: recovered")
                if not self.dry:
                    self._api([f"repos/{repo}/issues/{t['issue']['number']}/comments"], {"body": (
                        f"PerfWatch: {key} was clean in {self.cfg['n_confirm']} runs in a row ({ev['at']}). "
                        f"Signals back in range: {', '.join(ev['signals'])}. Close this issue if the fix is merged.")})
                self.writes += 1
            return True
        open_now = sum(1 for x in state["targets"].values() if x["status"] == "confirmed" and x["issue"])
        title, body = render_issue(key, t, self.cfg)
        issue = None
        existing = None if self.dry else self.find_issue(repo, key)
        if existing and existing["state"] == "open":
            issue = existing
        if issue:
            self.actions.append(f"update issue {issue['html_url']} ({ev['type']})")
            if not self.dry and (issue.get("body") or "") != body:
                self._api([f"repos/{repo}/issues/{issue['number']}", "-X", "PATCH"], {"title": title, "body": body})
                self.writes += 1
            t["issue"] = {"repo": repo, "number": issue["number"], "url": issue["html_url"]}
            return True
        if open_now > self.cfg["caps"]["open_issues"]:
            self.actions.append(f"HOLD {key}: {open_now} PerfWatch issues already open (cap {self.cfg['caps']['open_issues']})")
            return False
        if existing:
            body += f"\nEarlier closed issue for this key: {existing['html_url']}\n"
        self.actions.append(f"create issue in {repo}: {title}")
        if not self.dry:
            created = self._api([f"repos/{repo}/issues"], {"title": title, "body": body, "labels": [LABEL], "assignees": ["Wladefant"]})
            t["issue"] = {"repo": repo, "number": created["number"], "url": created["html_url"]}
        self.writes += 1
        return True


def refresh_dispatch(state: Dict[str, Any], cfg: Dict[str, Any], host: Dict[str, Any], now: str) -> List[str]:
    """Dispatch entries for confirmed keys. Main reads them; the script never starts a lane."""
    notes: List[str] = []
    by_key = {d["key"]: d for d in state["dispatch"]}
    created = 0
    for key, t in state["targets"].items():
        e = by_key.get(key)
        if t["status"] != "confirmed" or not t["issue"]:
            if e and e["status"] in ("pending", "deferred-host"):
                state["dispatch"].remove(e)
                notes.append(f"dispatch entry for {key} dropped (recovered)")
            continue
        if e is None:
            if created >= cfg["caps"]["dispatch"]:
                notes.append(f"dispatch cap reached; {key} waits for the next run")
                continue
            e = {"key": key, "issue_url": t["issue"]["url"], "status": "pending", "since": now, "lane": None}
            state["dispatch"].append(e)
            created += 1
            notes.append(f"dispatch entry for {key}")
        if e["status"] in ("pending", "deferred-host"):
            e["status"] = "pending" if host.get("state") == "ok" else "deferred-host"
            e["host_state"] = host.get("state")
    return notes


# ---------------------------------------------------------------------------- digest


def _fmt_sig(name: str, sig: Dict[str, Any], cfg: Dict[str, Any]) -> str:
    m = metric_of(name)
    last = sig["samples"][-1][1] if sig["samples"] else None
    b = baseline_of(sig, cfg)
    return f"| `{name}` | {fmt_val(m, b)} | {fmt_val(m, last)} | {sig['streak']} | {sig['state']} |"


def status_fingerprint(state: Dict[str, Any], notes: Dict[str, List[str]]) -> str:
    lines = []
    for key in sorted(state["targets"]):
        t = state["targets"][key]
        unavailable = sorted(n for n, s in t["signals"].items() if s["state"] == "unavailable")
        lines.append(f"{key}|{t['status']}|{','.join(sorted(t['confirmed']))}|{','.join(unavailable)}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def render_digest(state: Dict[str, Any], cfg: Dict[str, Any], notes: Dict[str, List[str]], host: Dict[str, Any],
                  now: str, suspect: bool, actions: List[str]) -> str:
    n_conf = sum(1 for t in state["targets"].values() if t["status"] == "confirmed")
    out = [f"PerfWatch digest {now}",
           f"Confirmed regressions: {n_conf}. Run {state['runs']}. Host: {host.get('state')} (RAM {host.get('ram_pct')}%)."]
    if suspect:
        out.append("Suspect run: too many targets breached at once. No streak or baseline changed.")
    q = [d for d in state["dispatch"] if d["status"] in ("pending", "deferred-host")]
    if q:
        out += ["", "Main: dispatch these confirmed regressions (one lane each, repo stage and 4-section PR from the issue):"]
        out += [f"- {d['key']} -> {d['issue_url']} [{d['status']}]" for d in q]
    if actions:
        out += ["", "Actions this run:"] + [f"- {a}" for a in actions]
    out += ["", "| Target | Status | Confirmed signals |", "|---|---|---|"]
    for key in sorted(state["targets"]):
        t = state["targets"][key]
        out.append(f"| {key} | {t['status']} | {', '.join(sorted(t['confirmed'])) or '-'} |")
    for key in sorted(state["targets"]):
        t = state["targets"][key]
        out += ["", f"### {key}", "| Signal | Baseline | Last | Streak | State |", "|---|---|---|---|---|"]
        out += [_fmt_sig(n, s, cfg) for n, s in sorted(t["signals"].items())]
        out += [f"- note: {x}" for x in notes.get(key, [])]
    return "\n".join(out) + "\n"


def digest_hash(text: str) -> str:
    return hashlib.sha256("\n".join(text.split("\n")[1:]).encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- state


LOG_DIR = STATE_DIR


def log(msg: str, to_file: bool = True) -> None:
    line = f"{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} {msg}"
    if sys.stdout is not None:
        print(line)
    if not to_file:
        return
    try:  # pythonw has no stdout; the log file is the record of an unattended run
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_DIR / "perfwatch.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_state(state_dir: Path) -> Dict[str, Any]:
    try:
        return json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return new_state()


def save_state(state_dir: Path, state: Dict[str, Any]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, state_dir / "state.json")


def load_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    validate_config(cfg)
    return cfg


def too_soon(state: Dict[str, Any], now: dt.datetime, min_minutes: float) -> bool:
    last = state.get("last_run_utc")
    if not last:
        return False
    try:
        then = dt.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return False
    return now - then < dt.timedelta(minutes=min_minutes)


# ------------------------------------------------------------------------------ run


def run(live: bool, force: bool, state_dir: Path, cfg: Dict[str, Any],
        collect: Callable[[Dict[str, Any], Budget], Tuple[Any, Any]] = collect_all,
        gh: Callable[..., str] = default_gh, host: Callable[[], Dict[str, Any]] = host_state,
        now: Optional[dt.datetime] = None, out: Callable[[str], None] = print) -> int:
    global LOG_DIR
    LOG_DIR = state_dir
    now = now or dt.datetime.now(dt.timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state = load_state(state_dir)
    if live and not force and too_soon(state, now, cfg["min_interval_min"]):
        log(f"skip: last run {state.get('last_run_utc')} is under {cfg['min_interval_min']} min ago")
        return 0
    hs = host()
    obs, notes = collect(cfg, Budget(cfg["max_requests_per_run"]))
    work = state if live else copy.deepcopy(state)
    events, suspect = evaluate(work, obs, cfg, now_iso)
    work["pending"].extend(events)
    if live:
        save_state(state_dir, work)  # the evaluated streaks are durable before any GitHub write
    pub = Publisher(cfg, gh, dry=not live)
    pub.publish(work)
    actions = pub.actions + refresh_dispatch(work, cfg, hs, now_iso)
    digest = render_digest(work, cfg, notes, hs, now_iso, suspect, actions)
    if not live:
        log("DRY RUN: nothing is written. Digest follows.", to_file=False)
        out(digest)
        return 0
    work["last_run_utc"] = now_iso
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "digest.md").write_text(digest, encoding="utf-8")
    (state_dir / "dispatch.json").write_text(json.dumps(work["dispatch"], indent=1), encoding="utf-8")
    fp = status_fingerprint(work, notes) + ("|suspect" if suspect else "")
    if work.get("status_fp") != fp:
        ti = cfg["tracking_issue"]
        gh(["api", f"repos/{ti['repo']}/issues/{ti['number']}/comments", "-f", f"body={digest}", "--jq", ".html_url"])
        work["status_fp"] = fp
        log("digest comment posted on the tracking issue")
    save_state(state_dir, work)
    log(f"run {work['runs']} done: {len(events)} event(s), suspect={suspect}, {pub.writes} issue write(s)")
    for a in actions:
        log(f"  {a}")
    return 0


# ---------------------------------------------------------------------------- replay


def replay(cfg: Dict[str, Any], key: str, base: str, head: str, runs: int,
           opener: Optional[urllib.request.OpenerDirector] = None, sleep: Callable[[float], None] = time.sleep) -> str:
    """Same request to base and head, alternating, after one warm-up each. Returns a Markdown table."""
    proj_name, tname = key_parts(key)
    proj = next((p for p in cfg["projects"] if p["name"] == proj_name), None)
    tgt = next((t for t in (proj or {}).get("targets", []) if t["name"] == tname and t["kind"] == "http"), None)
    if not tgt:
        raise PerfError(f"no http target {key!r}")
    parsed = urllib.parse.urlparse(tgt["url"])
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    origins = {}
    for label, origin in (("base", base), ("head", head)):
        o = urllib.parse.urlparse(origin)
        origins[label] = check_url(f"{o.scheme}://{o.netloc}{path}", proj["hosts"], allow_loopback=True)
    runs = max(1, min(runs, 10))
    opener = opener or make_opener()
    gated = bool(tgt.get("access_gated"))
    res: Dict[str, List[Dict[str, Any]]] = {"base": [], "head": []}
    for label in ("base", "head"):  # warm-up: not counted
        http_request(origins[label], opener)
        sleep(cfg["probe_gap_s"])
    for i in range(runs):
        for label in (("base", "head") if i % 2 == 0 else ("head", "base")):
            res[label].append(http_request(origins[label], opener))
            sleep(cfg["probe_gap_s"])
    rows = {}
    for label, rs in res.items():
        good = [r for r in rs if response_ok(r, gated)]
        ttfb = [r["ttfb_ms"] for r in good]
        rows[label] = {
            "ttfb_p50_ms": statistics.median(ttfb) if ttfb else None,
            "ttfb_p95_ms": percentile(ttfb, 95) if ttfb else None,
            "bytes": statistics.median([r["raw_len"] for r in good]) if good else None,
            "fail_count": float(len(rs) - len(good)),
            "cache": (rs[-1]["headers"].get("cf-cache-status") or rs[-1]["headers"].get("age") or "none") if rs else "none",
        }
    lines = [f"Replay of `{key}` path `{path}`: {runs} requests per side, alternating, 1 warm-up each, {cfg['probe_gap_s']} s apart.",
             "", "| Metric | Base | Head | Change |", "|---|---|---|---|"]
    for m in ("ttfb_p50_ms", "ttfb_p95_ms", "bytes", "fail_count"):
        b, h = rows["base"][m], rows["head"][m]
        delta = "n/a" if b is None or h is None else f"{h - b:+.0f} ({(h - b) / b * 100:+.1f}%)" if b else f"{h - b:+.0f}"
        lines.append(f"| {m} | {fmt_val(m, b)} | {fmt_val(m, h)} | {delta} |")
    lines.append(f"| cache state (last response) | {rows['base']['cache']} | {rows['head']['cache']} | |")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- CLI


def cmd_mark_dispatched(state_dir: Path, key: str, lane: str) -> int:
    state = load_state(state_dir)
    e = next((d for d in state["dispatch"] if d["key"] == key), None)
    if not e:
        print(f"no dispatch entry for {key}", file=sys.stderr)
        return 1
    e["status"], e["lane"] = "dispatched", lane
    save_state(state_dir, state)
    (state_dir / "dispatch.json").write_text(json.dumps(state["dispatch"], indent=1), encoding="utf-8")
    print(f"{key} marked dispatched to {lane}")
    return 0


def cmd_reset_baseline(state_dir: Path, key: str) -> int:
    state = load_state(state_dir)
    if key not in state["targets"]:
        print(f"unknown target {key}", file=sys.stderr)
        return 1
    old = state["targets"][key]
    state["targets"][key] = {**new_target(), "issue": old.get("issue")}
    save_state(state_dir, state)
    print(f"{key}: baseline, streaks and confirmation cleared")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--live", action="store_true", help="write state, issues and the digest (default: dry run)")
    r.add_argument("--force", action="store_true", help="ignore the minimum run interval")
    r.add_argument("--state-dir", default=str(STATE_DIR))
    r.add_argument("--config", default=str(CONFIG_PATH))
    p = sub.add_parser("replay")
    p.add_argument("--target", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--config", default=str(CONFIG_PATH))
    m = sub.add_parser("mark-dispatched")
    m.add_argument("key")
    m.add_argument("--lane", required=True)
    m.add_argument("--state-dir", default=str(STATE_DIR))
    b = sub.add_parser("reset-baseline")
    b.add_argument("key")
    b.add_argument("--state-dir", default=str(STATE_DIR))
    args = ap.parse_args(argv)
    try:
        if args.cmd == "run":
            return run(args.live, args.force, Path(args.state_dir), load_config(Path(args.config)))
        if args.cmd == "replay":
            print(replay(load_config(Path(args.config)), args.target, args.base, args.head, args.runs))
            return 0
        if args.cmd == "mark-dispatched":
            return cmd_mark_dispatched(Path(args.state_dir), args.key, args.lane)
        return cmd_reset_baseline(Path(args.state_dir), args.key)
    except PerfError as exc:
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
