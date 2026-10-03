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
      comments(first:100) {
        nodes { id url body author { login } createdAt updatedAt }
        pageInfo { hasNextPage endCursor }
      }
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


def read_comments(issue: dict, runner: Callable) -> list[dict]:
    """Read the complete discussion; publication and recovery use this same path."""
    connection = issue.get("comments") or {}
    comments, ids, cursors = [], set(), set()
    while True:
        page = connection.get("pageInfo") or {}
        if not isinstance(connection.get("nodes"), list) or not isinstance(page.get("hasNextPage"), bool):
            raise ValueError("Incomplete GitHub comments connection")
        for comment in connection["nodes"]:
            if (not isinstance(comment, dict) or not comment.get("id")
                or not isinstance(comment.get("body"), str)
                or not isinstance(comment.get("url"), str)
                or not re.fullmatch(re.escape(issue["url"]) + r"#issuecomment-[1-9][0-9]*", comment["url"])
                or comment["id"] in ids):
                raise ValueError("Incomplete, duplicate or foreign GitHub comment")
            ids.add(comment["id"])
            comments.append(comment)
        if not page["hasNextPage"]:
            return comments
        cursor = page.get("endCursor")
        if not cursor or cursor in cursors:
            raise ValueError("GitHub comments pagination did not advance")
        cursors.add(cursor)
        response = runner(
            """query($id:ID!,$cursor:String!){node(id:$id){... on Issue {
            id url comments(first:100,after:$cursor){
              nodes{id url body author{login} createdAt updatedAt}
              pageInfo{hasNextPage endCursor}
            }}}}""", {"id": issue["id"], "cursor": cursor})
        node = (response.get("data") or {}).get("node") or {}
        if response.get("errors") or node.get("id") != issue["id"] or node.get("url") != issue["url"]:
            raise ValueError("GitHub comments pagination lost issue identity")
        connection = node.get("comments") or {}


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
    issue["comments"] = read_comments(issue, runner)
    return issue


def execution_view(record: dict, issue: dict) -> dict:
    """Overlay API-owned work state without weakening local QA/authorization gates."""
    from project_adapter import canonicalize_lifecycle_status
    result = dict(record)
    result["github_snapshot"] = issue
    result["prompt"] = issue["body"]
    for comment in issue["comments"]:
        author = (comment.get("author") or {}).get("login") or "deleted account"
        result["prompt"] += (
            f"\n\n## GitHub discussion: {comment['url']}\n"
            f"Author: {author}; updated: {comment.get('updatedAt') or 'unknown'}\n\n{comment['body']}"
        )
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


def publish_report(issue_url: str, body: str, runner: Callable | None = None,
                   *, comment_id: str | None = None) -> str:
    """Publish/update only when the installed recovery reader can retrieve the result."""
    from project_adapter import default_graphql_runner
    runner = runner or default_graphql_runner
    if not isinstance(issue_url, str) or not ISSUE_URL.fullmatch(issue_url):
        raise ValueError("Report requires a canonical GitHub issue URL")
    if not isinstance(body, str):
        raise ValueError("Report body must be markdown text")
    reject_local_reports(body)
    if not body.strip():
        raise ValueError("Report body is empty")
    record = {"github": {"issue_url": issue_url}}
    issue = fetch_work_item(record, runner)
    if comment_id is None:
        response = runner(
            "mutation($id:ID!,$body:String!){addComment(input:{subjectId:$id,body:$body}){commentEdge{node{id url}}}}",
            {"id": issue["id"], "body": body})
        comment = (((response.get("data") or {}).get("addComment") or {}).get("commentEdge") or {}).get("node") or {}
    else:
        matches = [comment for comment in issue["comments"]
                   if comment["url"] == f"{issue_url}#issuecomment-{comment_id}"]
        if len(matches) != 1:
            raise ValueError("Comment does not belong to the canonical issue")
        response = runner(
            "mutation($id:ID!,$body:String!){updateIssueComment(input:{id:$id,body:$body}){issueComment{id url}}}",
            {"id": matches[0]["id"], "body": body})
        comment = ((response.get("data") or {}).get("updateIssueComment") or {}).get("issueComment") or {}
    if response.get("errors") or not comment.get("id") or not comment.get("url"):
        raise ValueError("GitHub report publication returned no verified comment identity")
    try:
        recovered = fetch_work_item(record, runner)
    except Exception as exc:
        raise ValueError(f"Recovery readback failed for {comment['url']}: {exc}") from exc
    if not any(item["id"] == comment["id"] and item["url"] == comment["url"] and item["body"] == body
               for item in recovered["comments"]):
        raise ValueError(f"Published report readback differs at {comment['url']}; do not claim delivered evidence")
    return comment["url"]
