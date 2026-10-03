#!/usr/bin/env python3
"""
workflows/portable/outer_loop_intake.py — Outer-Loop Webhook Intake for Superboard.

Turns new or updated GitHub issues and operator comments into triaged Superboard
Project 5 cards with canonical kind labels, milestones, and card lifecycle states
without requiring a continuously polling Main.

Key invariants:
  1. Idempotence: 0 writes if issue is already triaged to target state and enrolled.
  2. Dry-run mode: guaranteed 0 writes to GitHub.
  3. Canonical label taxonomy: exactly one kind: label, plus area: and risk: labels.
  4. Milestone semantics: preserves existing open milestones; assigns capability milestone.
  5. Superboard Project 5 lifecycle: Backlog, Ready, Building, QA, Review, Blocked, Done.
  6. Operator directive primacy: operator comments (e.g. Wladefant) override heuristics.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
import os
import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


# Ensure sibling portable workflow modules are importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from project_adapter import (
        CANONICAL_LIFECYCLE_STATUSES,
        PROJECT_STATUS_SCHEMA_QUERY,
        canonicalize_lifecycle_status,
        default_graphql_runner,
        get_current_project_config,
    )
except ImportError:
    CANONICAL_LIFECYCLE_STATUSES = (
        "Backlog",
        "Ready",
        "Building",
        "QA",
        "Review",
        "Blocked",
        "Done",
    )
    PROJECT_STATUS_SCHEMA_QUERY = """query($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) {
    ... on Organization {
      projectV2(number: $number) {
        id
        title
        fields(first: 30) {
          nodes {
            ... on ProjectV2SingleSelectField {
              id
              name
              options {
                id
                name
              }
            }
          }
        }
      }
    }
    ... on User {
      projectV2(number: $number) {
        id
        title
        fields(first: 30) {
          nodes {
            ... on ProjectV2SingleSelectField {
              id
              name
              options {
                id
                name
              }
            }
          }
        }
      }
    }
  }
}"""
    def canonicalize_lifecycle_status(value: str) -> str:
        folded = value.strip().casefold()
        mapping = {
            "backlog": "Backlog",
            "pending": "Ready",
            "ready": "Ready",
            "building": "Building",
            "build": "Building",
            "qa": "QA",
            "review": "Review",
            "blocked": "Blocked",
            "done": "Done",
            "completed": "Done",
        }
        if folded in mapping:
            return mapping[folded]
        for s in CANONICAL_LIFECYCLE_STATUSES:
            if folded == s.casefold():
                return s
        raise ValueError(f"Unknown lifecycle status '{value}'")

    def default_graphql_runner(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
        for k, v in variables.items():
            if isinstance(v, (int, float)):
                cmd.extend(["-F", f"{k}={v}"])
            elif isinstance(v, bool):
                cmd.extend(["-F", f"{k}={str(v).lower()}"])
            elif isinstance(v, (dict, list)):
                cmd.extend(["-f", f"{k}={json.dumps(v)}"])
            else:
                cmd.extend(["-f", f"{k}={v}"])
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return json.loads(res.stdout)


# ---------------------------------------------------------------------------
# Taxonomy Constants
# ---------------------------------------------------------------------------

CANONICAL_KINDS = (
    "kind:bug",
    "kind:feature",
    "kind:task",
    "kind:research",
    "kind:docs",
    "kind:governance",
    "kind:incident",
)

DEPRECATED_KIND_MAP = {
    "bug": "kind:bug",
    "enhancement": "kind:feature",
    "feature": "kind:feature",
    "docs": "kind:docs",
    "documentation": "kind:docs",
    "research": "kind:research",
    "task": "kind:task",
    "incident": "kind:incident",
    "governance": "kind:governance",
}

CANONICAL_AREAS = (
    "area:workflow",
    "area:harness",
    "area:bridge",
    "area:sync",
    "area:ui",
    "area:infra",
    "area:security",
)

CANONICAL_RISKS = (
    "risk:money-path",
    "risk:migration",
    "risk:high",
    "risk:medium",
    "risk:low",
)

DEFAULT_OPERATORS = {"Wladefant"}
DEFAULT_PROJECT_URL = "https://github.com/users/Wladefant/projects/5"
DEFAULT_PROJECT_NUMBER = 5
DEFAULT_PROJECT_OWNER = "Wladefant"


# ---------------------------------------------------------------------------
# GraphQL Queries and Mutations
# ---------------------------------------------------------------------------

ISSUE_INTAKE_QUERY = """query($owner: String!, $repo: String!, $issueNumber: Int!) {
  repository(owner: $owner, name: $repo) {
    id
    issue(number: $issueNumber) {
      id
      number
      title
      body
      state
      url
      labels(first: 50) {
        nodes {
          id
          name
        }
      }
      milestone {
        id
        number
        title
        state
      }
      assignees(first: 10) {
        nodes {
          login
        }
      }
      projectItems(first: 20) {
        nodes {
          id
          project {
            id
            number
            title
            owner {
              ... on User { login }
              ... on Organization { login }
            }
          }
          fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue {
              name
              optionId
            }
          }
        }
      }
      comments(last: 20) {
        nodes {
          id
          body
          author {
            login
          }
          createdAt
        }
      }
    }
  }
}"""

REPO_METADATA_QUERY = """query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    id
    milestones(first: 50, states: [OPEN]) {
      nodes {
        id
        number
        title
        state
      }
    }
    labels(first: 100) {
      nodes {
        id
        name
      }
    }
  }
}"""

ADD_PROJECT_ITEM_MUTATION = """mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) {
    item {
      id
    }
  }
}"""

UPDATE_PROJECT_STATUS_MUTATION = """mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(
    input: {
      projectId: $projectId
      itemId: $itemId
      fieldId: $fieldId
      value: {
        singleSelectOptionId: $optionId
      }
    }
  ) {
    projectV2Item {
      id
    }
  }
}"""


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class TriagePlan:
    """Calculated plan of triage actions for an issue."""
    issue_number: int
    issue_id: Optional[str]
    issue_title: str
    current_kind: Optional[str]
    inferred_kind: str
    labels_to_add: List[str]
    labels_to_remove: List[str]
    current_milestone: Optional[str]
    target_milestone: Optional[str]
    target_milestone_number: Optional[int]
    needs_project_enrollment: bool
    project_item_id: Optional[str]
    current_project_status: Optional[str]
    target_project_status: str
    needs_status_update: bool
    is_idempotent_noop: bool
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TriageResult:
    """Outcome of applying a triage plan."""
    ok: bool
    issue_number: int
    dry_run: bool
    is_idempotent_noop: bool
    github_writes: int
    actions_taken: List[str]
    error: Optional[str] = None
    plan: Optional[TriagePlan] = None

    def to_dict(self) -> Dict[str, Any]:
        res = asdict(self)
        if self.plan:
            res["plan"] = self.plan.to_dict()
        return res


# ---------------------------------------------------------------------------
# Default CLI Runner
# ---------------------------------------------------------------------------

def default_cli_runner(args: List[str]) -> str:
    """Run gh command line tool for issue edits."""
    cmd = ["gh"] + args
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return res.stdout


# ---------------------------------------------------------------------------
# Outer Loop Intake Engine
# ---------------------------------------------------------------------------

class OuterLoopIntake:
    """
    Triage and intake engine converting GitHub events into triaged Project 5 cards.
    """

    def __init__(
        self,
        *,
        graphql_runner: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
        cli_runner: Optional[Callable[[List[str]], str]] = None,
        operator_users: Optional[Set[str]] = None,
        project_number: int = DEFAULT_PROJECT_NUMBER,
        project_owner: str = DEFAULT_PROJECT_OWNER,
    ):
        self.graphql_runner = graphql_runner or default_graphql_runner
        self.cli_runner = cli_runner or default_cli_runner
        self.operator_users = operator_users or set(DEFAULT_OPERATORS)
        self.project_number = project_number
        self.project_owner = project_owner

    # -----------------------------------------------------------------------
    # Inference Helpers
    # -----------------------------------------------------------------------

    def infer_kind_label(
        self,
        title: str,
        body: str,
        existing_labels: List[str],
        comments: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, List[str]]:
        """
        Infer canonical kind: label, ensuring exactly ONE kind label is assigned.
        Returns (target_kind, labels_to_remove).
        """
        comments = comments or []
        combined_text = f"{title}\n{body}"

        # 1. Check for operator directive in recent comments (newest first)
        for comment in reversed(comments):
            author = ((comment.get("author") or {}).get("login") or "").strip()
            if author in self.operator_users:
                c_body = comment.get("body") or ""
                # Match /kind <name> or kind:<name> or type:<name>
                m = re.search(r"(?i)(?:^|[/\s,;])(?:kind:|type:|/kind\s+)(bug|feature|task|research|docs|documentation|governance|incident)\b", c_body)
                if m:
                    raw = m.group(1).lower()
                    target = DEPRECATED_KIND_MAP.get(raw, f"kind:{raw}")
                    if target in CANONICAL_KINDS:
                        to_remove = [l for l in existing_labels if (l.startswith("kind:") or l in DEPRECATED_KIND_MAP) and l != target]
                        return target, to_remove

        # 2. Check existing labels
        current_canonical = [l for l in existing_labels if l in CANONICAL_KINDS]
        if len(current_canonical) == 1:
            # Already has exactly one valid kind label! Preserve it.
            return current_canonical[0], []
        elif len(current_canonical) > 1:
            # Multiple canonical kinds exist; prioritize most specific and prune extras
            kind_priority = [
                "kind:incident",
                "kind:bug",
                "kind:feature",
                "kind:research",
                "kind:governance",
                "kind:docs",
                "kind:task",
            ]
            chosen = next((k for k in kind_priority if k in current_canonical), current_canonical[0])
            to_remove = [k for k in current_canonical if k != chosen]
            return chosen, to_remove

        # Check for deprecated labels to upgrade
        for l in existing_labels:
            folded = l.strip().casefold()
            if folded in DEPRECATED_KIND_MAP:
                target = DEPRECATED_KIND_MAP[folded]
                to_remove = [l]
                return target, to_remove

        # 3. Infer from title and body text patterns
        # Incident
        if re.search(r"(?i)\b(incident|outage|downtime|sev[01]|service\s+down)\b", combined_text):
            return "kind:incident", []

        # Bug
        if re.search(r"(?i)\b(bug|defect|regression|broken|fix|error|fail|crash|traceback|exception|panic)\b", combined_text):
            return "kind:bug", []

        # Feature
        if re.search(r"(?i)\b(feature|feat|add|support|implement|introduce|new\s+capability)\b", combined_text):
            return "kind:feature", []

        # Research
        if re.search(r"(?i)\b(research|investigate|spike|evaluate|survey|benchmark|feasibility)\b", combined_text):
            return "kind:research", []

        # Docs
        if re.search(r"(?i)\b(doc|docs|documentation|readme|guide|runbook|specification|spec)\b", combined_text):
            return "kind:docs", []

        # Governance
        if re.search(r"(?i)\b(governance|policy|ruleset|compliance|audit|precedence)\b", combined_text):
            return "kind:governance", []

        # Default fallback
        return "kind:task", []

    def infer_area_label(
        self,
        title: str,
        body: str,
        existing_labels: List[str],
        repo: str = "Wladefant/super-board",
    ) -> Optional[str]:
        """Infer an area: label if none is already assigned."""
        if any(l.startswith("area:") for l in existing_labels):
            return None  # Already has area label

        combined_text = f"{title}\n{body}"
        if re.search(r"(?i)\b(telegram|bridge|chatgpt|mcp|bot)\b", combined_text):
            return "area:bridge"
        if re.search(r"(?i)\b(harness|runtime|agent|prompt|session|veyyon|ipc|tool)\b", combined_text):
            return "area:harness"
        if re.search(r"(?i)\b(sync|fork|upstream|merge)\b", combined_text):
            return "area:sync"
        if re.search(r"(?i)\b(ui|frontend|tui|dashboard|screen|css|layout)\b", combined_text):
            return "area:ui"
        if re.search(r"(?i)\b(infra|ci|github\s+actions|runner|docker|dokploy|build\s+slot)\b", combined_text):
            return "area:infra"
        if re.search(r"(?i)\b(security|secret|token|key|auth|permission)\b", combined_text):
            return "area:security"

        # Default for super-board repository
        return "area:workflow"

    def infer_risk_label(
        self,
        title: str,
        body: str,
        existing_labels: List[str],
    ) -> Optional[str]:
        """Infer a risk: label if none is already assigned."""
        if any(l.startswith("risk:") for l in existing_labels):
            return None  # Already has risk label

        combined_text = f"{title}\n{body}"
        if re.search(r"(?i)\b(money|wallet|trading|balance|billing|ledger|stripe|payment)\b", combined_text):
            return "risk:money-path"
        if re.search(r"(?i)\b(migration|schema|alembic|ddl)\b", combined_text):
            return "risk:migration"
        if re.search(r"(?i)\b(breaking|critical|production|security)\b", combined_text):
            return "risk:high"
        if re.search(r"(?i)\b(bug|defect|refactor)\b", combined_text):
            return "risk:medium"

        return "risk:low"

    def infer_milestone(
        self,
        title: str,
        body: str,
        current_milestone: Optional[Dict[str, Any]],
        open_milestones: List[Dict[str, Any]],
        comments: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Infer milestone assignment.
        Returns target milestone dict if change needed, or None if already satisfied.
        """
        # 1. If currently has an OPEN milestone, preserve it
        if current_milestone and current_milestone.get("state") == "OPEN":
            return None

        comments = comments or []
        combined_text = f"{title}\n{body}"

        # 2. Check operator comment directive
        for comment in reversed(comments):
            author = ((comment.get("author") or {}).get("login") or "").strip()
            if author in self.operator_users:
                c_body = comment.get("body") or ""
                m = re.search(r"(?i)(?:^|[/\s,;])(?:milestone:|/milestone\s+)(.+?)(?:\r?\n|$)", c_body)
                if m:
                    target_name = m.group(1).strip().strip("\"'")
                    for ms in open_milestones:
                        if ms["title"].casefold() == target_name.casefold() or str(ms.get("number")) == target_name:
                            return ms

        if not open_milestones:
            return None

        # 3. Keyword matching against open milestones
        # Check GitHub System Integration
        if re.search(r"(?i)\b(github|integration|hierarchy|sub-issue|triage|intake|outer-loop|project\s*5|superboard)\b", combined_text):
            ms = next((m for m in open_milestones if "github" in m["title"].lower() or "integration" in m["title"].lower()), None)
            if ms:
                return ms

        # Check Tooling + quota
        if re.search(r"(?i)\b(tooling|quota|rate\s*limit|balance|routing|model|allowance)\b", combined_text):
            ms = next((m for m in open_milestones if "tooling" in m["title"].lower() or "quota" in m["title"].lower()), None)
            if ms:
                return ms

        # Check Hardening / safety
        if re.search(r"(?i)\b(hardening|safety|preflight|invariant|gate|verification)\b", combined_text):
            ms = next((m for m in open_milestones if "hardening" in m["title"].lower()), None)
            if ms:
                return ms

        # Check Docs
        if re.search(r"(?i)\b(docs|documentation|rollout|guide|onboarding)\b", combined_text):
            ms = next((m for m in open_milestones if "docs" in m["title"].lower() or "rollout" in m["title"].lower()), None)
            if ms:
                return ms

        # Fallback to GitHub System Integration or first open milestone
        fallback = next((m for m in open_milestones if "github" in m["title"].lower()), open_milestones[0])
        return fallback

    def infer_card_state(
        self,
        issue_state: str,
        issue_body: str,
        labels: List[str],
        comments: Optional[List[Dict[str, Any]]] = None,
        has_owner: bool = False,
        has_criteria: bool = False,
        blocked_by: Optional[List[str]] = None,
    ) -> str:
        """
        Infer canonical Superboard Project 5 lifecycle status:
        Backlog, Ready, Building, QA, Review, Blocked, Done.
        """
        # 1. Closed issue is Done
        if issue_state.upper() == "CLOSED":
            return "Done"

        comments = comments or []
        blocked_by = blocked_by or []

        # 2. Operator comment directives (newest first)
        for comment in reversed(comments):
            author = ((comment.get("author") or {}).get("login") or "").strip()
            if author in self.operator_users:
                c_body = comment.get("body") or ""
                # Check explicit status commands
                m = re.search(r"(?i)(?:^|[/\s,;])(?:status:|state:|/state\s+|/status\s+)(ready|building|qa|review|blocked|done|backlog)\b", c_body)
                if m:
                    return canonicalize_lifecycle_status(m.group(1))

                # Natural language operator keywords
                if re.search(r"(?i)\b(approved|go ahead|ready for work|ready to build|dispatch)\b", c_body):
                    return "Ready"
                if re.search(r"(?i)\b(blocked|on hold|hold off|paused)\b", c_body):
                    return "Blocked"
                if re.search(r"(?i)\b(building|in progress|wip|start building)\b", c_body):
                    return "Building"
                if re.search(r"(?i)\b(needs[ -]qa|ready for qa|\bqa\b)\b", c_body):
                    return "QA"
                if re.search(r"(?i)\b(in review|ready for review|\breview\b)\b", c_body):
                    return "Review"
                if re.search(r"(?i)\b(completed|verified done|landed)\b", c_body):
                    return "Done"
                if re.search(r"(?i)\b(backlog|deprioritize|deprioritized)\b", c_body):
                    return "Backlog"

        # 3. Blockers check
        if "state:blocked" in labels or "state:needs-decision" in labels:
            return "Blocked"
        if blocked_by:
            return "Blocked"
        if re.search(r"(?i)##\s*Current State & Blockers[\s\S]*?\bblocked\b", issue_body):
            return "Blocked"

        # 4. QA / Review check
        if "state:live-verified" in labels:
            return "Done"
        if "state:qa-verified" in labels or "state:needs-qa" in labels:
            return "QA"
        if "state:review" in labels:
            return "Review"

        # 5. Building check
        if "state:building" in labels:
            return "Building"

        # 6. Ready vs Backlog
        # If it has criteria and scope (or explicit Ready section/owner), it is Ready for dispatch
        has_scope_section = bool(re.search(r"(?i)##\s*(?:Scope|Acceptance Criteria)", issue_body))
        if has_owner and (has_criteria or has_scope_section):
            return "Ready"

        if has_scope_section:
            return "Ready"

        return "Backlog"

    # -----------------------------------------------------------------------
    # Triage Planning & Execution
    # -----------------------------------------------------------------------

    def plan_triage(
        self,
        owner: str,
        repo: str,
        issue_number: int,
    ) -> TriagePlan:
        """
        Query issue and repo metadata, evaluate inference rules, and produce
        a complete deterministic TriagePlan.
        """
        # Fetch issue data
        issue_res = self.graphql_runner(
            ISSUE_INTAKE_QUERY,
            {"owner": owner, "repo": repo, "issueNumber": issue_number},
        )
        data = (issue_res.get("data") or {}).get("repository") or {}
        issue = data.get("issue")
        if not issue:
            errors = issue_res.get("errors", [])
            err_msg = errors[0].get("message") if errors else f"Issue #{issue_number} not found in {owner}/{repo}"
            raise RuntimeError(err_msg)

        issue_id = issue.get("id")
        title = issue.get("title", "")
        body = issue.get("body", "")
        state = issue.get("state", "OPEN")
        labels = [n["name"] for n in (issue.get("labels") or {}).get("nodes", [])]
        assignees = [n["login"] for n in (issue.get("assignees") or {}).get("nodes", [])]
        comments = (issue.get("comments") or {}).get("nodes", [])
        current_milestone = issue.get("milestone")

        # Fetch repo open milestones
        repo_res = self.graphql_runner(REPO_METADATA_QUERY, {"owner": owner, "repo": repo})
        repo_data = (repo_res.get("data") or {}).get("repository") or {}
        open_milestones = (repo_data.get("milestones") or {}).get("nodes", [])

        # Fetch Project V2 Schema
        schema_res = self.graphql_runner(
            PROJECT_STATUS_SCHEMA_QUERY,
            {"owner": self.project_owner, "number": self.project_number},
        )
        schema_data = (schema_res.get("data") or {}).get("repositoryOwner") or {}
        project = schema_data.get("projectV2")
        if not project:
            errors = schema_res.get("errors", [])
            err_msg = errors[0].get("message") if errors else f"Project #{self.project_number} not found for {self.project_owner}"
            raise RuntimeError(err_msg)

        project_id = project.get("id")
        status_field_id = None
        options_map: Dict[str, str] = {}
        for f in (project.get("fields") or {}).get("nodes", []):
            if (f.get("name") or "").strip().casefold() == "status":
                status_field_id = f.get("id")
                for opt in f.get("options", []):
                    options_map[opt["name"].strip().casefold()] = opt["id"]
                break

        # Check current project enrollment and status
        project_items = (issue.get("projectItems") or {}).get("nodes", [])
        matching_item = None
        for it in project_items:
            proj = it.get("project") or {}
            if proj.get("number") == self.project_number and (
                (proj.get("owner") or {}).get("login", "") == self.project_owner
            ):
                matching_item = it
                break

        project_item_id = matching_item.get("id") if matching_item else None
        current_status = None
        if matching_item:
            current_status = (matching_item.get("fieldValueByName") or {}).get("name")

        needs_project_enrollment = (matching_item is None)

        # 1. Infer Kind Label
        inferred_kind, kinds_to_remove = self.infer_kind_label(title, body, labels, comments)
        current_kinds = [l for l in labels if l.startswith("kind:")]
        current_kind = current_kinds[0] if current_kinds else None

        labels_to_add: List[str] = []
        labels_to_remove: List[str] = list(kinds_to_remove)

        if inferred_kind not in labels:
            labels_to_add.append(inferred_kind)

        # 2. Infer Area Label
        inferred_area = self.infer_area_label(title, body, labels, repo)
        if inferred_area and inferred_area not in labels:
            labels_to_add.append(inferred_area)

        # 3. Infer Risk Label
        inferred_risk = self.infer_risk_label(title, body, labels)
        if inferred_risk and inferred_risk not in labels:
            labels_to_add.append(inferred_risk)

        # 4. Infer Milestone
        target_ms = self.infer_milestone(title, body, current_milestone, open_milestones, comments)
        target_milestone_title = target_ms.get("title") if target_ms else None
        target_milestone_num = target_ms.get("number") if target_ms else None

        # 5. Infer Project Status
        target_status = self.infer_card_state(
            issue_state=state,
            issue_body=body,
            labels=labels,
            comments=comments,
            has_owner=bool(assignees),
            has_criteria="Acceptance Criteria" in body,
        )

        needs_status_update = (current_status != target_status)

        # 6. Idempotency Check
        is_idempotent_noop = (
            not labels_to_add
            and not labels_to_remove
            and target_milestone_title is None
            and not needs_project_enrollment
            and not needs_status_update
        )

        return TriagePlan(
            issue_number=issue_number,
            issue_id=issue_id,
            issue_title=title,
            current_kind=current_kind,
            inferred_kind=inferred_kind,
            labels_to_add=labels_to_add,
            labels_to_remove=labels_to_remove,
            current_milestone=current_milestone.get("title") if current_milestone else None,
            target_milestone=target_milestone_title,
            target_milestone_number=target_milestone_num,
            needs_project_enrollment=needs_project_enrollment,
            project_item_id=project_item_id,
            current_project_status=current_status,
            target_project_status=target_status,
            needs_status_update=needs_status_update,
            is_idempotent_noop=is_idempotent_noop,
            details={
                "project_id": project_id,
                "status_field_id": status_field_id,
                "options_map": options_map,
                "target_option_id": options_map.get(target_status.casefold()),
                "owner": owner,
                "repo": repo,
            },
        )

    def apply_triage(
        self,
        plan: TriagePlan,
        dry_run: bool = False,
    ) -> TriageResult:
        """
        Apply a calculated TriagePlan against GitHub.
        Guarantees 0 writes in dry-run mode or if plan is an idempotent no-op.
        """
        if dry_run or plan.is_idempotent_noop:
            return TriageResult(
                ok=True,
                issue_number=plan.issue_number,
                dry_run=dry_run,
                is_idempotent_noop=plan.is_idempotent_noop,
                github_writes=0,
                actions_taken=[],
                plan=plan,
            )

        writes = 0
        actions: List[str] = []
        owner = plan.details.get("owner", "Wladefant")
        repo = plan.details.get("repo", "super-board")
        repo_slug = f"{owner}/{repo}"

        try:
            # 1. Update Labels and Milestone via gh issue edit
            if plan.labels_to_add or plan.labels_to_remove or plan.target_milestone:
                edit_args = ["issue", "edit", str(plan.issue_number), "--repo", repo_slug]
                if plan.labels_to_add:
                    edit_args.extend(["--add-label", ",".join(plan.labels_to_add)])
                if plan.labels_to_remove:
                    edit_args.extend(["--remove-label", ",".join(plan.labels_to_remove)])
                if plan.target_milestone:
                    edit_args.extend(["--milestone", plan.target_milestone])

                self.cli_runner(edit_args)
                writes += 1
                actions.append(f"Updated issue metadata: add={plan.labels_to_add}, remove={plan.labels_to_remove}, ms={plan.target_milestone}")

            # 2. Project Enrollment
            item_id = plan.project_item_id
            if plan.needs_project_enrollment:
                if not plan.issue_id:
                    raise RuntimeError(f"Cannot enroll issue #{plan.issue_number} without GraphQL issue ID")
                res = self.graphql_runner(
                    ADD_PROJECT_ITEM_MUTATION,
                    {
                        "projectId": plan.details["project_id"],
                        "contentId": plan.issue_id,
                    },
                )
                item_data = ((res.get("data") or {}).get("addProjectV2ItemById") or {}).get("item")
                if not item_data or not item_data.get("id"):
                    raise RuntimeError(f"Failed to add issue #{plan.issue_number} to project #{self.project_number}: {res}")
                item_id = item_data["id"]
                writes += 1
                actions.append(f"Enrolled issue in Project {self.project_number} (item {item_id})")

            # 3. Project Status Update
            if plan.needs_status_update or plan.needs_project_enrollment:
                target_option_id = plan.details.get("target_option_id")
                if not target_option_id:
                    raise RuntimeError(f"Target status '{plan.target_project_status}' not found in project options")
                if not item_id:
                    raise RuntimeError(f"No item_id available for status update on issue #{plan.issue_number}")

                self.graphql_runner(
                    UPDATE_PROJECT_STATUS_MUTATION,
                    {
                        "projectId": plan.details["project_id"],
                        "itemId": item_id,
                        "fieldId": plan.details["status_field_id"],
                        "optionId": target_option_id,
                    },
                )
                writes += 1
                actions.append(f"Updated Project {self.project_number} status to '{plan.target_project_status}'")

            return TriageResult(
                ok=True,
                issue_number=plan.issue_number,
                dry_run=False,
                is_idempotent_noop=False,
                github_writes=writes,
                actions_taken=actions,
                plan=plan,
            )

        except Exception as e:
            return TriageResult(
                ok=False,
                issue_number=plan.issue_number,
                dry_run=False,
                is_idempotent_noop=False,
                github_writes=writes,
                actions_taken=actions,
                error=str(e),
                plan=plan,
            )


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Outer-Loop Webhook Intake: triage GitHub issues & comments into Superboard Project 5 cards."
    )
    parser.add_argument("--event-path", default=os.getenv("GITHUB_EVENT_PATH"), help="Path to GitHub Actions event payload JSON.")
    parser.add_argument("--repo", default=None, help="Target repository (e.g. Wladefant/super-board).")
    parser.add_argument("--issue", type=int, default=None, help="Specific issue number to triage.")
    parser.add_argument("--dry-run", action="store_true", help="Calculate triage plan without applying mutations.")
    parser.add_argument("--project-number", type=int, default=DEFAULT_PROJECT_NUMBER, help="Project V2 number (default: 5).")
    parser.add_argument("--project-owner", default=DEFAULT_PROJECT_OWNER, help="Project V2 owner (default: Wladefant).")
    parser.add_argument("--operator", action="append", default=[], help="Authorized operator logins (default: Wladefant).")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON result.")
    return parser


def parse_event_payload(path: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Parse GitHub Actions event JSON to extract (repo_slug, issue_number, action)."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Event file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        event = json.load(f)

    # Check if pull request event (we skip PR comments/events)
    if "pull_request" in event or (event.get("issue") or {}).get("pull_request"):
        return None, None, "skip_pr"

    repo = (event.get("repository") or {}).get("full_name")
    issue = event.get("issue") or {}
    issue_num = issue.get("number")
    action = event.get("action", "")

    return repo, issue_num, action


def main():
    parser = build_parser()
    args = parser.parse_args()

    repo = args.repo
    issue_number = args.issue
    action = None

    if args.event_path:
        ev_repo, ev_issue, action = parse_event_payload(args.event_path)
        if action == "skip_pr":
            print("Outer-loop intake: Skipping pull request event.")
            sys.exit(0)
        repo = repo or ev_repo
        issue_number = issue_number or ev_issue

    if not repo:
        try:
            cfg = get_current_project_config()
            repo = cfg.repo
        except Exception:
            repo = "Wladefant/super-board"

    if not repo or not issue_number:
        parser.error("Both --repo and --issue (or valid --event-path) are required.")

    if "/" not in repo:
        parser.error(f"Invalid repo format '{repo}'; expected 'owner/name'.")

    owner, repo_name = repo.split("/", 1)
    operators = set(args.operator) if args.operator else DEFAULT_OPERATORS

    intake = OuterLoopIntake(
        project_number=args.project_number,
        project_owner=args.project_owner,
        operator_users=operators,
    )

    try:
        plan = intake.plan_triage(owner, repo_name, issue_number)
        result = intake.apply_triage(plan, dry_run=args.dry_run)

        if args.json:
            print(json.dumps(result.to_dict(), indent=2))
        else:
            print("=" * 60)
            print(f"OUTER-LOOP INTAKE: #{issue_number} in {repo}")
            print("=" * 60)
            print(f"Action/Event: {action or 'manual'}")
            print(f"Kind Label:   {plan.inferred_kind} (current: {plan.current_kind})")
            print(f"Labels to Add:    {plan.labels_to_add}")
            print(f"Labels to Remove: {plan.labels_to_remove}")
            print(f"Milestone:    {plan.target_milestone or plan.current_milestone or 'None'}")
            print(f"Card State:   {plan.target_project_status} (current: {plan.current_project_status})")
            print(f"Project 5:    {'Enroll' if plan.needs_project_enrollment else 'Enrolled'} (item: {plan.project_item_id or 'new'})")
            print(f"Idempotent:   {'YES (No-op)' if plan.is_idempotent_noop else 'NO (Mutations needed)'}")
            print(f"Dry Run:      {'YES' if args.dry_run else 'NO'}")
            print(f"Writes Done:  {result.github_writes}")
            if result.actions_taken:
                print("Actions:")
                for a in result.actions_taken:
                    print(f"  - {a}")
            if not result.ok:
                print(f"ERROR: {result.error}")
                sys.exit(1)

    except Exception as e:
        print(f"Outer-loop intake failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
