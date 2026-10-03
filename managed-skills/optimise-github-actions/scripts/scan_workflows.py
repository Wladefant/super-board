#!/usr/bin/env python3
"""Static scan of .github/workflows/*.yml for the waste patterns in SKILL.md.

Usage: python scan_workflows.py [REPO_ROOT] [--json]

Read-only: parses YAML files, makes no network calls, writes nothing.
Needs PyYAML (`pip install pyyaml` into a venv, never into a project).
Findings: missing timeout-minutes, missing concurrency / cancel-in-progress,
pull_request without path filters, push + pull_request on the same branches
(duplicate triggers), matrix size, hosted Windows/macOS runners, cron cadence,
setup-* actions without a cache, `fetch-depth: 0`.
"""
import itertools
import json
import sys
from pathlib import Path

import yaml

root = Path(next((a for a in sys.argv[1:] if not a.startswith("--")), "."))
as_json = "--json" in sys.argv


def triggers(doc):
    # PyYAML parses the bare key `on` as boolean True.
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on: {}}
    if isinstance(on, list):
        return {k: {} for k in on}
    return {k: (v or {}) for k, v in (on or {}).items()}


def labels(job):
    ro = job.get("runs-on")
    if isinstance(ro, dict):
        ro = ro.get("labels") or ro.get("group") or ""
    return ro if isinstance(ro, list) else [ro] if ro else []


def matrix_legs(job):
    m = (job.get("strategy") or {}).get("matrix")
    if not isinstance(m, dict):
        return 1
    axes = [v for k, v in m.items() if k not in ("include", "exclude") and isinstance(v, list)]
    n = 1
    for a in axes:
        n *= len(a)
    n = n if axes else 0
    n += len(m.get("include") or []) if isinstance(m.get("include"), list) else 0
    n -= len(m.get("exclude") or []) if isinstance(m.get("exclude"), list) else 0
    return max(n, 1)


findings = []


def add(wf, job, kind, detail):
    findings.append({"workflow": wf, "job": job, "kind": kind, "detail": detail})


files = sorted(itertools.chain((root / ".github" / "workflows").glob("*.yml"), (root / ".github" / "workflows").glob("*.yaml")))
summary = []
for f in files:
    try:
        doc = yaml.safe_load(f.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        add(f.name, None, "unparseable", str(e).splitlines()[0])
        continue
    if not isinstance(doc, dict):
        continue
    trig = triggers(doc)
    jobs = doc.get("jobs") or {}
    conc = doc.get("concurrency")
    has_pr = "pull_request" in trig or "pull_request_target" in trig
    has_push = "push" in trig
    if has_pr and not conc and not any("concurrency" in (j or {}) for j in jobs.values()):
        add(f.name, None, "no-concurrency", "runs on pull_request with no concurrency group: every push to a PR stacks runs")
    elif has_pr:
        cips = [conc] if conc else [(j or {}).get("concurrency") for j in jobs.values()]
        if not any(isinstance(c, dict) and c.get("cancel-in-progress") for c in cips if c):
            add(f.name, None, "no-cancel-in-progress", "concurrency is set but cancel-in-progress is not: superseded PR runs finish anyway")
    if "pull_request" in trig:
        pr = trig["pull_request"]
        if not (pr.get("paths") or pr.get("paths-ignore")):
            add(f.name, None, "no-path-filter", "pull_request has no paths/paths-ignore: docs-only PRs run the whole workflow")
    if has_pr and has_push:
        push = trig["push"]
        br = push.get("branches")
        if not br:
            add(f.name, None, "duplicate-trigger", "push has no branches filter alongside pull_request: a PR branch push runs twice")
        else:
            add(f.name, None, "push-and-pr", f"push on {br} and pull_request both run the same jobs: check that main is not re-running what the PR passed")
    if trig.get("schedule"):
        for s in trig["schedule"]:
            add(f.name, None, "schedule", f"cron `{s.get('cron')}`")
    for name, job in jobs.items():
        job = job or {}
        ro = labels(job)
        is_reusable = "uses" in job
        if not is_reusable and "timeout-minutes" not in job:
            add(f.name, name, "no-timeout", "no timeout-minutes: a hung job holds a runner for up to 6 hours")
        legs = matrix_legs(job)
        if legs >= 4:
            add(f.name, name, "matrix", f"{legs} matrix legs: each pays checkout and install; check that the legs are not shorter than their setup")
        for l in map(str, ro):
            if l.lower().startswith(("windows", "macos")):
                add(f.name, name, "costly-runner", f"{l} bills at a 2x/10x multiplier on hosted runners")
        for step in job.get("steps") or []:
            uses = str(step.get("uses") or "")
            w = step.get("with") or {}
            if uses.startswith(("actions/setup-node", "actions/setup-python", "actions/setup-go", "actions/setup-java")) and "cache" not in w:
                add(f.name, name, "no-setup-cache", f"{uses.split('@')[0]} without `cache:`")
            if uses.startswith("actions/checkout") and str(w.get("fetch-depth")) == "0":
                add(f.name, name, "full-history-checkout", "fetch-depth: 0 (keep only if the job reads git history)")
    summary.append({"workflow": f.name, "name": doc.get("name"), "triggers": sorted(map(str, trig)), "jobs": len(jobs), "runs_on": sorted({str(l) for j in jobs.values() for l in labels(j or {})})})

if as_json:
    print(json.dumps({"workflows": summary, "findings": findings}, indent=2))
    sys.exit(0)

print(f"# Workflow scan: {root}\n")
print("| Workflow | Triggers | Jobs | Runs on |\n|---|---|---:|---|")
for s in summary:
    print(f"| `{s['workflow']}` | {', '.join(s['triggers'])} | {s['jobs']} | {', '.join(s['runs_on'])} |")
print("\n| Workflow | Job | Finding | Detail |\n|---|---|---|---|")
for x in findings:
    print(f"| `{x['workflow']}` | {x['job'] or ''} | {x['kind']} | {x['detail']} |")
print(f"\n{len(findings)} findings in {len(summary)} workflows.")
