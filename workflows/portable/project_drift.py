#!/usr/bin/env python3
"""
project_drift.py - detect and repair drift between repo issues/PRs and Project 5.

The agent must not be the thing that remembers the board (issue #208). This sweep
makes the mechanical parts deterministic.

Drift classes (code -> repair):
  missing-from-project   open issue/PR not on the board            -> add + set Status
  status-unset           on the board, Status empty                -> set Status from state
  closed-not-done        closed ISSUE whose Status is not Done     -> set Done
  open-but-done          open item whose Status is Done (reopened) -> set Backlog (issue) / Review (PR)
  field-unset            Kind/Area/Risk empty but the label exists -> set the field from the label
Report-only (a human or lane must decide; never auto-repaired):
  merged-not-done        merged/closed PR not Done (policy: merges never auto-Done)
  field-mismatch         Kind/Area/Risk set but disagrees with the label
  no-kind-label          open issue with no kind:* label
  no-milestone           open issue/PR with no milestone
  closed-not-on-board    closed item absent from the board (history, not repaired)

Safety: dry-run default (0 writes). `--live` performs at most `--max-writes` board
mutations, reads each one back, and refuses to start when GraphQL quota would drop
below the immutable reserve of 1000 points (exit 75). Repairs are restricted to
`--repair-repo` repos; other `--repo` entries are report-only. Idempotent: a second
live run on a repaired board makes 0 writes.

  python project_drift.py --repo Wladefant/super-board --repo Bavariance/polysimulator \
      --repair-repo Wladefant/super-board [--owner Wladefant --project 5] [--live] [--max-writes 60]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from project_adapter import default_graphql_runner  # noqa: E402

GQL_RESERVE = 1000
EXIT_QUOTA = 75

KIND_MAP = {"kind:task": "Task", "kind:bug": "Bug", "kind:feature": "Feature", "kind:research": "Research",
            "kind:governance": "Governance", "kind:docs": "Docs", "kind:incident": "Incident"}
AREA_MAP = {"area:workflow": "Workflow", "area:harness": "Harness", "area:bridge": "Bridge", "area:sync": "Sync",
            "area:ui": "UI", "area:infra": "Infra", "area:security": "Security", "area:markets": "Markets",
            "area:wallets": "Wallets"}
# Highest severity first: the board shows the worst risk the labels declare.
RISK_ORDER = [("risk:money-path", "Money Path"), ("risk:migration", "Migration"), ("risk:high", "High"),
              ("risk:medium", "Medium"), ("risk:low", "Low")]
SELECT_FIELDS = ("Status", "Kind", "Area", "Risk")

SCHEMA_QUERY = """
query($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner {
      projectV2(number: $number) {
        id
        fields(first: 50) { nodes { ... on ProjectV2SingleSelectField { id name options { id name } } } }
      }
    }
  }
}"""

ITEMS_QUERY = """
query($owner: String!, $number: Int!, $after: String) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner {
      projectV2(number: $number) {
        items(first: 100, after: $after) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            content {
              __typename
              ... on Issue { number state repository { nameWithOwner } }
              ... on PullRequest { number state merged repository { nameWithOwner } }
            }
            status: fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
            kind: fieldValueByName(name: "Kind") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
            area: fieldValueByName(name: "Area") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
            risk: fieldValueByName(name: "Risk") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
          }
        }
      }
    }
  }
}"""

ADD_MUTATION = """
mutation($project: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } }
}"""

SET_MUTATION = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field,
    value: {singleSelectOptionId: $option}}) { projectV2Item { id } }
}"""

READBACK_QUERY = """
query($item: ID!) {
  node(id: $item) {
    ... on ProjectV2Item {
      status: fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
      kind: fieldValueByName(name: "Kind") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
      area: fieldValueByName(name: "Area") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
      risk: fieldValueByName(name: "Risk") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
    }
  }
}"""

GQL = Callable[[str, Dict[str, Any]], Dict[str, Any]]
RestLister = Callable[[str], List[Dict[str, Any]]]


class QuotaError(RuntimeError):
    pass


def graphql_remaining() -> int:
    out = subprocess.run(["gh", "api", "rate_limit", "-q", ".resources.graphql.remaining"],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise QuotaError(f"cannot read GraphQL quota: {out.stderr.strip()[:200]}")
    return int(out.stdout.strip())


def list_repo_items(repo: str) -> List[Dict[str, Any]]:
    """All issues and PRs of a repo via REST (one list, PRs carry a pull_request key)."""
    jq = ('.[] | {number, node_id, state, state_reason, is_pr: (.pull_request != null), '
          'labels: [.labels[].name], milestone: (.milestone.title // null)}')
    proc = subprocess.run(["gh", "api", "--paginate", f"repos/{repo}/issues?state=all&per_page=100", "-q", jq],
                          capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(f"listing {repo} failed: {proc.stderr.strip()[:300]}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def fetch_schema(gql: GQL, owner: str, number: int) -> Dict[str, Any]:
    data = gql(SCHEMA_QUERY, {"owner": owner, "number": number})
    proj = ((data.get("data") or {}).get("repositoryOwner") or {}).get("projectV2")
    if not proj:
        raise RuntimeError(f"project {number} not found for {owner}: {data.get('errors')}")
    fields: Dict[str, Dict[str, Any]] = {}
    for f in proj["fields"]["nodes"]:
        if f and f.get("name") in SELECT_FIELDS:
            fields[f["name"]] = {"id": f["id"], "options": {o["name"]: o["id"] for o in f["options"]}}
    missing = [n for n in SELECT_FIELDS if n not in fields]
    if missing:
        raise RuntimeError(f"project is missing fields {missing}")
    return {"project_id": proj["id"], "fields": fields}


def fetch_board(gql: GQL, owner: str, number: int) -> Dict[Tuple[str, int], Dict[str, Any]]:
    board: Dict[Tuple[str, int], Dict[str, Any]] = {}
    after: Optional[str] = None
    while True:
        variables: Dict[str, Any] = {"owner": owner, "number": number}
        if after:
            variables["after"] = after
        data = gql(ITEMS_QUERY, variables)
        items = (((data.get("data") or {}).get("repositoryOwner") or {}).get("projectV2") or {}).get("items")
        if items is None:
            raise RuntimeError(f"board query failed: {data.get('errors')}")
        for node in items["nodes"]:
            c = node.get("content") or {}
            if not c.get("number"):
                continue  # draft item or inaccessible content
            key = (c["repository"]["nameWithOwner"], c["number"])
            board[key] = {"item_id": node["id"], "type": c["__typename"], "state": c["state"],
                          **{f: ((node.get(f.lower()) or {}).get("name")) for f in SELECT_FIELDS}}
        if not items["pageInfo"]["hasNextPage"]:
            return board
        after = items["pageInfo"]["endCursor"]


def expected_status(item: Dict[str, Any]) -> str:
    if item["state"] == "closed" and not item["is_pr"]:
        return "Done"
    return "Review" if item["is_pr"] else "Backlog"


def label_fields(labels: List[str]) -> Dict[str, List[str]]:
    """Field -> acceptable values, preferred first. Several area:/kind: labels are all acceptable."""
    out: Dict[str, List[str]] = {}
    for l in labels:
        if l in KIND_MAP:
            out.setdefault("Kind", []).append(KIND_MAP[l])
        if l in AREA_MAP:
            out.setdefault("Area", []).append(AREA_MAP[l])
    for lab, val in RISK_ORDER:
        if lab in labels:
            out.setdefault("Risk", []).append(val)
    return out


def detect(repo: str, items: List[Dict[str, Any]], board: Dict[Tuple[str, int], Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return findings: {code, repo, number, repair: None | [(field, value)] | 'add'}."""
    findings: List[Dict[str, Any]] = []

    def add(code: str, it: Dict[str, Any], repair: Any = None, detail: str = "") -> None:
        findings.append({"code": code, "repo": repo, "number": it["number"], "repair": repair, "detail": detail})

    for it in items:
        is_open = it["state"] == "open"
        entry = board.get((repo, it["number"]))
        want = label_fields(it["labels"])
        if entry is None:
            if is_open:
                sets = [("Status", expected_status(it))] + [(k, v[0]) for k, v in sorted(want.items())]
                add("missing-from-project", it, ("add", sets))
            else:
                add("closed-not-on-board", it)
        else:
            if not entry["Status"]:
                add("status-unset", it, [("Status", expected_status(it))])
            elif not is_open and not it["is_pr"] and entry["Status"] != "Done":
                if it.get("state_reason") in (None, "completed"):
                    add("closed-not-done", it, [("Status", "Done")], f"was {entry['Status']}")
                else:  # not_planned / duplicate: a person decides what the board should say
                    add("closed-unplanned-not-done", it, None, f"{it['state_reason']}, was {entry['Status']}")
            elif not is_open and it["is_pr"] and entry["Status"] != "Done":
                add("pr-closed-not-done", it, None, f"was {entry['Status']}")  # policy: merges never auto-Done
            elif is_open and entry["Status"] == "Done":
                add("open-but-done", it, [("Status", expected_status(it))])
            for fld, vals in sorted(want.items()):
                cur = entry.get(fld)
                if not cur:
                    add("field-unset", it, [(fld, vals[0])])
                elif cur not in vals:
                    add("field-mismatch", it, None, f"{fld}: board={cur} labels={'/'.join(vals)}")
        if is_open:
            if not it["is_pr"] and not any(l.startswith("kind:") for l in it["labels"]):
                add("no-kind-label", it)
            if not it["milestone"]:
                add("no-milestone", it)
    return findings


def count_by_code(findings: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for f in findings:
        out[f["code"]] = out.get(f["code"], 0) + 1
    return dict(sorted(out.items()))


def current_state(repo: str, number: int) -> str:
    proc = subprocess.run(["gh", "api", f"repos/{repo}/issues/{number}", "-q", ".state"],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"state recheck failed for {repo}#{number}: {proc.stderr.strip()[:200]}")
    return proc.stdout.strip()


def apply_repairs(findings: List[Dict[str, Any]], items_by_key: Dict[Tuple[str, int], Dict[str, Any]],
                  board: Dict[Tuple[str, int], Dict[str, Any]], schema: Dict[str, Any], gql: GQL,
                  max_writes: int, recheck: Optional[Callable[[str, int], str]] = None) -> Dict[str, Any]:
    """Group repairs per item (an add and its field sets are one unit). Count board mutations against the cap."""
    writes = 0
    done: List[str] = []
    failed: List[str] = []
    skipped_changed: List[str] = []
    deferred = 0
    grouped: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for f in findings:
        if not f["repair"]:
            continue
        g = grouped.setdefault((f["repo"], f["number"]), {"add": False, "sets": {}})
        if isinstance(f["repair"], tuple):
            g["add"] = True
            g["sets"].update(dict(f["repair"][1]))
        else:
            g["sets"].update(dict(f["repair"]))
    for key, g in grouped.items():
        cost = (1 if g["add"] else 0) + len(g["sets"])
        if writes + cost > max_writes:
            deferred += 1
            continue
        label = f"{key[0]}#{key[1]}"
        try:
            if recheck is not None and "Status" in g["sets"]:
                if recheck(key[0], key[1]) != items_by_key[key]["state"]:
                    skipped_changed.append(label)
                    continue
            # Count each attempt BEFORE the call: a mutation that reaches the server and then raises still spent budget.
            if g["add"]:
                writes += 1
                node_id = items_by_key[key]["node_id"]
                res = gql(ADD_MUTATION, {"project": schema["project_id"], "content": node_id})
                item_id = res["data"]["addProjectV2ItemById"]["item"]["id"]
            else:
                item_id = board[key]["item_id"]
            for fld, val in g["sets"].items():
                opt = schema["fields"][fld]["options"].get(val)
                if not opt:
                    raise RuntimeError(f"no option {val!r} on field {fld}")
                writes += 1
                gql(SET_MUTATION, {"project": schema["project_id"], "item": item_id, "field": schema["fields"][fld]["id"], "option": opt})
            back = (gql(READBACK_QUERY, {"item": item_id}).get("data") or {}).get("node") or {}
            ok = all(((back.get(f.lower()) or {}).get("name")) == v for f, v in g["sets"].items())
            (done if ok else failed).append(label)
        except Exception as e:
            failed.append(f"{label}: {e}")
    return {"writes": writes, "repaired": done, "failed": failed, "skipped_state_changed": skipped_changed,
            "deferred_items": deferred}


def run_sweep(repos: List[str], repair_repos: List[str], owner: str, project: int, live: bool, max_writes: int,
              gql: GQL = default_graphql_runner, lister: RestLister = list_repo_items,
              quota: Callable[[], int] = graphql_remaining,
              recheck: Optional[Callable[[str, int], str]] = current_state) -> Dict[str, Any]:
    remaining = quota()
    max_writes = max(0, max_writes)
    # Reading the board and schema costs about 100 points of headroom; every write may be followed by a readback (x2).
    affordable = (remaining - GQL_RESERVE - 100) // 2
    if affordable < 1:
        raise QuotaError(f"GraphQL remaining {remaining} would breach the {GQL_RESERVE}-point reserve")
    max_writes = min(max_writes, affordable)
    schema = fetch_schema(gql, owner, project)
    board = fetch_board(gql, owner, project)
    all_findings: List[Dict[str, Any]] = []
    items_by_key: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for repo in repos:
        items = lister(repo)
        for it in items:
            items_by_key[(repo, it["number"])] = it
        all_findings += detect(repo, items, board)
    repairable = [f for f in all_findings if f["repo"] in repair_repos and f["repair"]]
    history = [f for f in all_findings if f["code"] == "closed-not-on-board"]
    all_findings = [f for f in all_findings if f["code"] != "closed-not-on-board"]
    repairable = [f for f in repairable if f["code"] != "closed-not-on-board"]
    report: Dict[str, Any] = {
        "mode": "live" if live else "dry-run", "project": f"{owner}/{project}", "board_items": len(board),
        "repos": repos, "repair_repos": repair_repos, "findings": count_by_code(all_findings),
        "by_repo": {r: count_by_code([f for f in all_findings if f["repo"] == r]) for r in repos},
        "history_not_on_board": len(history),
        "repairable": len(repairable), "report_only": len(all_findings) - len(repairable),
        "graphql_remaining_at_start": remaining,
    }
    if live:
        report["applied"] = apply_repairs(repairable, items_by_key, board, schema, gql, max_writes, recheck=recheck)
        report["effective_max_writes"] = max_writes
    else:
        report["applied"] = {"writes": 0}
    report["sample"] = [f"{f['repo']}#{f['number']} {f['code']} {f['detail']}".strip() for f in all_findings if f["repair"] is None][:25]
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", action="append", required=True)
    ap.add_argument("--repair-repo", action="append", default=None, help="default: no repo (report only)")
    ap.add_argument("--owner", default="Wladefant")
    ap.add_argument("--project", type=int, default=5)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--max-writes", type=int, default=60)
    args = ap.parse_args(argv)
    try:
        report = run_sweep(args.repo, args.repair_repo or [], args.owner, args.project, args.live, args.max_writes)
    except QuotaError as e:
        print(f"project-drift: HALT quota: {e}", file=sys.stderr)
        return EXIT_QUOTA
    print(json.dumps(report, indent=2))
    return 1 if report["applied"].get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
