#!/usr/bin/env python3
"""Turn an e2e `.e2e/report.json` into the FLOW-QA receipt that `github_pr_gate.py` accepts.

https://github.com/Wladefant/super-board/issues/487

The receipt counts assertions in the report (profile section 13.9), never an exit code. It is a
PASS only when all of these hold; each failed condition prints a `FLOW-QA-REASON <name>` line:

  report_invalid         the file is not an e2e report-1 document
  run_not_passed         run.status is not "passed", or run.errors is not empty
  assertion_failed       at least one executed step failed
  zero_assertions        no assertion step ran and passed (a zero-test or no-assertion run)
  served_sha_unverified  the served revision is not a 40-hex sha
  served_sha_mismatch    the served revision differs from --expected-sha
  production_host        a target ran against a production host
  missing_viewports      a required viewport (default 390x844 and 1440x900) did not run and pass

Served sha: `--served-sha`, or read from `<--base-url>/api/version`. Prefer a full
40-hex commit, then legacy sha, served_sha, version, git_sha, or commitSha.
SemVer and deploymentId never identify served content. Missing commits raise an error.

Replay is the default: a model call or a cache miss gives `FLOW-QA-REASON replay_not_clean`
(`replay_not_clean` joins the list above). `--allow-model-calls` opts out, for record runs only.
`--require-replay` is accepted and does nothing.

Usage:
  e2e_receipt.py --report .e2e/report.json --expected-sha <40hex> --base-url http://127.0.0.1:3000
  e2e_receipt.py --report R --expected-sha S --served-sha S --out receipt.txt
Exit code: 0 PASS, 1 FAIL. The gate reads the text, not the exit code.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_guard  # noqa: E402

HERE = Path(__file__).resolve().parent
PINS = json.loads((HERE / "pins.json").read_text(encoding="utf-8"))
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
VIEWPORT_RE = re.compile(r"^\d+x\d+$")
# A step counts as an assertion when it is a matcher or an agent judgment.
ASSERTION_KINDS = {"assertion"}
ASSERTION_APIS = {"agent.assert", "agent.waitFor"}


def fetch_served_sha(base_url: str, timeout: float = 15.0, allowed: Optional[List[str]] = None) -> Optional[str]:
    """Read /api/version without following redirects; the host must pass the allow-list."""
    url = base_url.rstrip("/") + "/api/version"
    refusal = e2e_guard.host_allowed(url, allowed or ["localhost", "127.0.0.1"])
    if refusal:
        raise ValueError(refusal)
    with e2e_guard.open_no_redirect(url, timeout, "application/json") as res:
        data = json.loads(res.read().decode("utf-8"))
    for key in ("commit", "sha", "served_sha", "version", "git_sha", "commitSha"):
        value = data.get(key)
        if isinstance(value, str) and SHA_RE.fullmatch(value):
            return value
    raise ValueError("Served SHA requires a 40-hex commit")


def _last_attempt(result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    attempts = result.get("attempts") or []
    return attempts[-1] if attempts else None


def _is_assertion(step: Dict[str, Any]) -> bool:
    return step.get("kind") in ASSERTION_KINDS or step.get("api") in ASSERTION_APIS


def _is_production(origin: str) -> bool:
    return e2e_guard.is_forbidden_host(e2e_guard.norm_host(origin))


def evaluate(
    report: Any, expected_sha: str, served_sha: Optional[str], required_viewports: List[str],
    require_replay: bool = True,
) -> Dict[str, Any]:
    reasons: List[str] = []
    out: Dict[str, Any] = {
        "passed": 0, "failed": 0, "viewports": [], "reasons": reasons,
        "cache_replayed": 0, "cache_missed": 0, "model_calls": 0, "model": None,
    }
    run = report.get("run") if isinstance(report, dict) else None
    if not isinstance(run, dict) or report.get("schemaVersion") != "report-1":
        reasons.append("report_invalid")
        return out

    if run.get("status") != "passed" or run.get("errors"):
        reasons.append("run_not_passed")

    passing_targets = set()
    for result in run.get("results") or []:
        if not result.get("selected"):
            continue
        attempt = _last_attempt(result)
        result_ok = result.get("status") == "passed"
        if attempt is None:
            out["failed"] += 1
            continue
        for step in attempt.get("steps") or []:
            ok = step.get("status") == "passed"
            if _is_assertion(step):
                out["passed" if ok else "failed"] += 1
            elif not ok:
                out["failed"] += 1
            metrics = step.get("metrics") or {}
            out["model_calls"] += int(metrics.get("modelCalls") or 0)
            cache = step.get("cache") or {}
            if cache.get("mode") == "self-finalized":
                out["cache_replayed"] += 1
            elif cache.get("mode") == "missed":
                out["cache_missed"] += 1
        if not result_ok:
            out["failed"] += 1
        elif result.get("targetId"):
            passing_targets.add(result["targetId"])
    if out["failed"] > 0:
        reasons.append("assertion_failed")
    if out["passed"] == 0:
        reasons.append("zero_assertions")

    for target in run.get("targets") or []:
        if _is_production(str(target.get("baseOrigin") or "")):
            reasons.append("production_host")
        tid = str(target.get("id") or "")
        if VIEWPORT_RE.match(tid) and tid in passing_targets:
            out["viewports"].append(tid)
    missing = [v for v in required_viewports if v not in out["viewports"]]
    if missing:
        reasons.append("missing_viewports")
        out["missing_viewports"] = missing
    if require_replay and (out["model_calls"] > 0 or out["cache_missed"] > 0):
        reasons.append("replay_not_clean")
    out["replay_check"] = "required" if require_replay else "skipped"

    if not served_sha or not SHA_RE.match(served_sha):
        reasons.append("served_sha_unverified")
    elif not SHA_RE.match(expected_sha or "") or served_sha.lower() != expected_sha.lower():
        reasons.append("served_sha_mismatch")
    return out


def render(ev: Dict[str, Any], served_sha: Optional[str]) -> str:
    state = "FAIL" if ev["reasons"] else "PASS"
    served = served_sha.lower() if served_sha and SHA_RE.match(served_sha) else ""
    lines = [
        f"FLOW-QA: {state}{' ' + served if served else ''}",
        f"FLOW-QA-ASSERTIONS pass={ev['passed']} fail={ev['failed']}",
    ]
    if ev["viewports"]:
        lines.append(f"FLOW-QA-VIEWPORTS {','.join(ev['viewports'])}")
    for reason in dict.fromkeys(ev["reasons"]):
        lines.append(f"FLOW-QA-REASON {reason}")
    lines.append(
        f"E2E-CACHE replayed={ev['cache_replayed']} missed={ev['cache_missed']} model_calls={ev['model_calls']}"
    )
    if ev.get("replay_check") == "skipped":
        lines.append("E2E-REPLAY-CHECK skipped (--allow-model-calls)")
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", required=True)
    ap.add_argument("--expected-sha", required=True)
    ap.add_argument("--served-sha")
    ap.add_argument("--base-url")
    ap.add_argument("--allow-host", action="append", default=[], help="extra staging host for the /api/version read; repeatable")
    ap.add_argument("--allow-model-calls", action="store_true",
                    help="opt out of the default replay check: accept model calls and cache misses (record runs only)")
    ap.add_argument("--require-replay", action="store_true", help=argparse.SUPPRESS)  # deprecated: now the default
    ap.add_argument("--viewports", default=",".join(PINS["requiredReceiptViewports"]),
                    help="required viewports (comma list); the gate needs 390x844 and 1440x900")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    served = args.served_sha
    if not served and args.base_url:
        try:
            served = fetch_served_sha(args.base_url, allowed=["localhost", "127.0.0.1", *args.allow_host])
        except Exception as exc:  # network error is a FAIL, not a crash
            print(f"served sha read failed: {exc}", file=sys.stderr)
    try:
        report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        report = None
    required = [v for v in args.viewports.split(",") if v]
    ev = evaluate(report, args.expected_sha, served, required, require_replay=not args.allow_model_calls)
    text = render(ev, served)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 1 if ev["reasons"] else 0


if __name__ == "__main__":
    sys.exit(main())
