"""GitHub is work truth; disk records are execution/resumption caches only.

Authenticated, read-only intake is mandatory before scheduling. No stale-cache
fallback on API failure, incomplete connections or missing structure. Reporting
writes only an issue comment and verifies it through the authenticated API.
"""
from __future__ import annotations

import re
from typing import Any, Callable

PROJECT_URL = "https://github.com/users/Wladefant/projects/5"
ISSUE_URL = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/issues/([1-9][0-9]*)\Z")
HEADINGS = (
    "Scope & Original Request", "Acceptance Criteria", "Dependencies & Parent Issue",
    "Owner & Assignment", "Current State & Blockers", "Branch, PR & Exact Head",
    "Verification Evidence", "Next Action", "Authorization & Constraints",
)
ISSUE_QUERY = """query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    issue(number:$number) {
      id url title body state updatedAt
      milestone { number title state url }
      parent { id url title }
      labels(first:100) { nodes { name } pageInfo { hasNextPage } }
      assignees(first:100) { nodes { login } pageInfo { hasNextPage } }
      blockedBy(first:100) { nodes { url state } pageInfo { hasNextPage } }
      projectItems(first:100) {
        nodes { id project { id url number }
          fieldValueByName(name:"Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        }
        pageInfo { hasNextPage }
      }
    }
  }
}"""


def reject_local_reports(value: Any) -> None:
    """Never accept unreadable local reports as issue content or evidence."""
    if isinstance(value, dict):
        for item in value.values():
            reject_local_reports(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_local_reports(item)
    elif isinstance(value, str) and re.search(r"local://|raw\.githubusercontent\.com", value, re.I):
        raise ValueError("Reports and evidence must be readable GitHub markdown, not local:// or raw.githubusercontent.com")


def issue_sections(body: str) -> dict[str, str]:
    # Forms emit level-three headings; handwritten issues may use level two.
    clean = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    parts = re.split(r"(?m)^#{2,3}\s+([^\n]+)\n", clean)
    return {parts[i].strip().casefold(): parts[i + 1].strip() for i in range(1, len(parts), 2)}


def fetch_work_item(record: dict, runner: Callable | None = None) -> dict:
    from project_adapter import default_graphql_runner
    runner = runner or default_graphql_runner
    info = record.get("github") or {}
    url = info.get("issue_url") or record.get("issue_url")
    match = ISSUE_URL.fullmatch(url or "")
    if not match:
        raise ValueError("Publish a dedicated GitHub issue before execution")
    owner, repo, number = match.groups()
    if info.get("repo") and info["repo"] != f"{owner}/{repo}":
        raise ValueError("Issue URL disagrees with cached repository identity")
    response = runner(ISSUE_QUERY, {"owner": owner, "repo": repo, "number": int(number)})
    if response.get("errors"):
        raise ValueError("GitHub returned incomplete work-item data")
    issue = ((response.get("data") or {}).get("repository") or {}).get("issue")
    if not issue or issue.get("url") != url:
        raise ValueError("GitHub issue missing or identity changed; reconcile its canonical URL")
    for field in ("labels", "assignees", "blockedBy", "projectItems"):
        connection = issue.get(field) or {}
        if "nodes" not in connection or connection.get("pageInfo", {}).get("hasNextPage", True):
            raise ValueError(f"Incomplete GitHub {field} connection; narrow/reconcile intake before execution")
    issue["labels"] = [n["name"] for n in issue["labels"]["nodes"]]
    issue["assignees"] = [n["login"] for n in issue["assignees"]["nodes"]]
    issue["blocked_by"] = [n["url"] for n in issue.pop("blockedBy")["nodes"] if n["state"] == "OPEN"]
    projects = [n for n in issue.pop("projectItems")["nodes"] if n["project"]["url"] == PROJECT_URL]
    if len(projects) != 1:
        raise ValueError(f"Issue must be enrolled exactly once on {PROJECT_URL}")
    issue["project"] = projects[0]
    issue["project_status"] = (projects[0].get("fieldValueByName") or {}).get("name")
    from project_adapter import CANONICAL_LIFECYCLE_STATUSES
    if issue["project_status"] not in CANONICAL_LIFECYCLE_STATUSES:
        raise ValueError("GitHub Project 5 Status is missing or unknown")
    if not issue.get("milestone") or (issue["state"] == "OPEN" and issue["milestone"]["state"] != "OPEN"):
        raise ValueError("Assign an open capability/integration milestone on GitHub")
    if not issue["assignees"]:
        raise ValueError("Assign an accountable GitHub owner")
    if sum(label.startswith("kind:") for label in issue["labels"]) != 1:
        raise ValueError("Assign exactly one canonical kind: label")
    for prefix in ("area:", "risk:"):
        if not any(label.startswith(prefix) for label in issue["labels"]):
            raise ValueError(f"Assign a canonical {prefix} label")
    reject_local_reports(issue["body"])
    sections = issue_sections(issue["body"])
    for heading in HEADINGS:
        if not sections.get(heading.casefold()) or sections[heading.casefold()] == "_No response_":
            raise ValueError(f"Memoryless-agent contract missing section: {heading}")
    dependency_text = sections["dependencies & parent issue"]
    if not issue.get("parent") and not re.search(r"(?i)\bstandalone\b|\bno parent\b", dependency_text):
        raise ValueError("Link the native parent, or explicitly state standalone/no parent with rationale")
    issue["sections"] = sections
    return issue


def execution_view(record: dict, issue: dict) -> dict:
    """Overlay API-owned work state without weakening local QA/authorization gates."""
    from project_adapter import canonicalize_lifecycle_status
    result = dict(record)
    result["github_snapshot"] = issue
    result["prompt"] = issue["body"]
    result["labels"] = issue["labels"]
    result["owner"] = ", ".join(issue["assignees"])
    result["next_action"] = issue["sections"]["next action"]
    result["milestone"] = issue["milestone"]
    result["parent"] = issue["parent"]
    result["superboard"] = {
        "role": "api_read_cache", "project_number": 5,
        "project_id": issue["project"]["project"]["id"],
        "item_id": issue["project"]["id"], "status": issue["project_status"],
        "labels": issue["labels"],
    }
    status = issue["project_status"]
    blockers = []
    if issue["state"] != "OPEN":
        blockers.append("GitHub issue is closed; cached execution cannot reopen or complete it")
    if status in ("Backlog", "Blocked", "Done"):
        blockers.append(f"GitHub Project 5 status is {status}; not eligible for execution")
    if issue["blocked_by"]:
        blockers.append("Open native dependencies: " + ", ".join(issue["blocked_by"]))
    # State is a local execution checkpoint, not the board. A disagreement must
    # be reconciled, not silently reset to bypass acceptance/review/authorization.
    expected = canonicalize_lifecycle_status(record.get("state", "pending"))
    if expected != status and not (expected == "Backlog" and status == "Ready"):
        blockers.append(f"Execution cache ({expected}) disagrees with GitHub ({status}); reconcile checkpoint")
    result["github_blocker"] = "; ".join(blockers)
    return result


def publish_report(issue_url: str, body: str, runner: Callable | None = None) -> str:
    """Publish readable markdown and confirm exact body via authenticated readback."""
    from project_adapter import default_graphql_runner
    runner = runner or default_graphql_runner
    if not isinstance(issue_url, str) or not (match := ISSUE_URL.fullmatch(issue_url)):
        raise ValueError("Report requires a canonical GitHub issue URL")
    if not isinstance(body, str):
        raise ValueError("Report body must be markdown text")
    reject_local_reports(body)
    if not body.strip():
        raise ValueError("Report body is empty")
    owner, repo, number = match.groups()
    response = runner("query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){issue(number:$number){id url}}}", {"owner": owner, "repo": repo, "number": int(number)})
    issue = ((response.get("data") or {}).get("repository") or {}).get("issue")
    if response.get("errors") or not issue or issue["url"] != issue_url:
        raise ValueError("Report issue identity could not be verified")
    response = runner("mutation($id:ID!,$body:String!){addComment(input:{subjectId:$id,body:$body}){commentEdge{node{id url}}}}", {"id": issue["id"], "body": body})
    if response.get("errors"):
        raise ValueError("GitHub report publication failed")
    comment = response["data"]["addComment"]["commentEdge"]["node"]
    proof = runner("query($id:ID!){node(id:$id){... on IssueComment {url body}}}", {"id": comment["id"]})
    observed = (proof.get("data") or {}).get("node") or {}
    if proof.get("errors") or observed.get("body") != body or observed.get("url") != comment["url"]:
        raise ValueError("Published report readback differs; do not claim delivered evidence")
    return comment["url"]
