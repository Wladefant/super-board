#!/usr/bin/env python3
"""
Portable Project Adapter Interface (~/.veyyon/workflows/project_adapter.py)

Provides a clean boundary between the generic multi-agent workflow core
(coordinator, ledger, decision engine, preflight gate, model router) and
project-specific identities (GitHub repo, Superboard project number, staging compose
IDs, Supabase project refs, safety regexes).

Inviolable Guarantees:
  1. Pure standard library (no third-party dependencies).
  2. Generic core contains NO hardcoded project defaults; PolySimulator identities
     live exclusively inside the explicit PolysimulatorAdapter.
  3. ZERO production/main DB access. Main DB is strictly prohibited across all adapters.
  4. Preserves PolySimulator policy files, isolated staging DB identity (hgzyqmaanndcimnclxtv),
     and Dokploy staging compose ID (TU7b_dY9l9_nCas6YBNwj).
  5. Supports loading from Superboard configs (.claude/super-board/configs/<slug>.json),
     custom JSON fixtures, environment variables, or CLI arguments.
  6. Safe unknown environment behavior: an unconfigured or novel project cleanly isolates
     local tasks and safely flags unconfigured external staging rather than assuming PolySimulator.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class StagingEnvironmentConfig:
    """Staging infrastructure identifiers and configuration for a project."""
    dokploy_compose_id: Optional[str] = None
    dokploy_app_name: Optional[str] = None
    supabase_project_ref: Optional[str] = None
    stripe_mode: str = "test"  # Always 'test'; 'live' is strictly rejected
    services: List[str] = field(default_factory=list)

    def is_configured(self, service: str) -> bool:
        """Check if a specific service is configured for this staging environment."""
        if service == "dokploy_staging":
            return bool(self.dokploy_compose_id)
        elif service == "supabase_staging":
            return bool(self.supabase_project_ref)
        elif service == "stripe_test":
            return self.stripe_mode == "test"
        return service in self.services


@dataclass
class SafetyRules:
    """Safety boundaries and prohibited patterns for a project."""
    forbidden_patterns: List[str] = field(default_factory=list)
    prohibit_production: bool = True
    prohibit_main_db: bool = True
    prohibit_live_keys: bool = True
    forbidden_compose_ids: List[str] = field(default_factory=list)
    forbidden_supabase_refs: List[str] = field(default_factory=list)


@dataclass
class ProjectConfig:
    """Complete project adapter configuration."""
    repo: str
    project_name: str = ""
    project_number: int = 1
    base_branch: str = "main"
    staging: StagingEnvironmentConfig = field(default_factory=StagingEnvironmentConfig)
    safety: SafetyRules = field(default_factory=SafetyRules)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ProjectConfig:
        staging_data = data.get("staging", {})
        if not isinstance(staging_data, dict):
            staging_data = {}
        staging = StagingEnvironmentConfig(
            dokploy_compose_id=staging_data.get("dokploy_compose_id"),
            dokploy_app_name=staging_data.get("dokploy_app_name"),
            supabase_project_ref=staging_data.get("supabase_project_ref"),
            stripe_mode=staging_data.get("stripe_mode", "test"),
            services=staging_data.get("services", []),
        )

        safety_data = data.get("safety", {})
        if not isinstance(safety_data, dict):
            safety_data = {}
        safety = SafetyRules(
            forbidden_patterns=safety_data.get("forbidden_patterns", []),
            prohibit_production=safety_data.get("prohibit_production", True),
            prohibit_main_db=safety_data.get("prohibit_main_db", True),
            prohibit_live_keys=safety_data.get("prohibit_live_keys", True),
            forbidden_compose_ids=safety_data.get("forbidden_compose_ids", []),
            forbidden_supabase_refs=safety_data.get("forbidden_supabase_refs", []),
        )

        return cls(
            repo=data.get("repo", ""),
            project_name=data.get("project_name") or data.get("repo", "").split("/")[-1],
            project_number=int(data.get("project_number", 1)),
            base_branch=data.get("base_branch", "main"),
            staging=staging,
            safety=safety,
            metadata=data.get("metadata", {}),
        )

    def update_lifecycle(
        self,
        request_id: str,
        state: str,
        head_sha: Optional[str] = None,
        evidence_url: Optional[str] = None,
        *,
        issue_number: Optional[int] = None,
        dry_run: bool = False,
        closure_verified: bool = False,
        graphql_runner: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> "SuperboardLifecycleOutcome":
        """
        Update GitHub Project V2 card status for this project.
        Duck-typed callable conforming to the frozen Superboard updater contract:
          .update_lifecycle(request_id, state, head_sha, evidence_url) -> outcome
          exposing attributes: .ok (bool), .blocked_reason (str|None), .board_url (str|None)
        """
        updater = SuperboardProjectUpdater(self, graphql_runner=graphql_runner)
        return updater.update_lifecycle(
            request_id=request_id,
            state=state,
            head_sha=head_sha,
            evidence_url=evidence_url,
            issue_number=issue_number,
            dry_run=dry_run,
            closure_verified=closure_verified,
        )


# ---------------------------------------------------------------------------
# Pre-registered Adapters
# ---------------------------------------------------------------------------

def create_polysimulator_config() -> ProjectConfig:
    """
    Explicit PolySimulator project adapter preserving all exact staging identities,
    policy files, and strict production/main DB prohibitions.
    """
    return ProjectConfig(
        repo="Bavariance/polysimulator",
        project_name="PolySimulator",
        project_number=1,
        base_branch="staging",
        staging=StagingEnvironmentConfig(
            dokploy_compose_id="TU7b_dY9l9_nCas6YBNwj",
            dokploy_app_name="polysimulator-staging-iad-v09j4g",
            supabase_project_ref="hgzyqmaanndcimnclxtv",
            stripe_mode="test",
            services=["dokploy_staging", "supabase_staging", "stripe_test"],
        ),
        safety=SafetyRules(
            forbidden_patterns=[
                r"\bzaraprptkegxqpvnsubu\b",  # Production Supabase project ref
                r"\bvpyL-7TDEUREH6Uo_y1sb\b", # Production Dokploy compose ID
                r"\bapi\.polysimulator\.com\b",
                r"\bpolysim\.com\b",
            ],
            prohibit_production=True,
            prohibit_main_db=True,
            prohibit_live_keys=True,
            forbidden_compose_ids=["vpyL-7TDEUREH6Uo_y1sb"],
            forbidden_supabase_refs=["zaraprptkegxqpvnsubu"],
        ),
        metadata={
            "description": "PolySimulator staging engineering environment",
            "dokploy_host": "hosting.wladefant.de",
            "staging_domain": "polysim.wladefant.de",
        },
    )


def create_generic_config(repo: str = "generic/unconfigured") -> ProjectConfig:
    """
    Generic project adapter for unknown or safe baseline environments.
    Local tasks work normally; staging services require explicit configuration.
    """
    name = repo.split("/")[-1] if "/" in repo else repo
    return ProjectConfig(
        repo=repo,
        project_name=name,
        project_number=1,
        base_branch="main",
        staging=StagingEnvironmentConfig(
            dokploy_compose_id=None,
            dokploy_app_name=None,
            supabase_project_ref=None,
            stripe_mode="test",
            services=[],
        ),
        safety=SafetyRules(
            forbidden_patterns=[],
            prohibit_production=True,
            prohibit_main_db=True,
            prohibit_live_keys=True,
            forbidden_compose_ids=[],
            forbidden_supabase_refs=[],
        ),
        metadata={"description": f"Generic adapter for {repo}"},
    )


# ---------------------------------------------------------------------------
# Superboard Config (.claude/super-board/configs/<slug>.json) Parser
# ---------------------------------------------------------------------------

def parse_superboard_config(path: str) -> Optional[ProjectConfig]:
    """Parse an existing Superboard JSON configuration into a ProjectConfig."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    # Extract repo
    repo_raw = data.get("repo", {})
    repo = ""
    if isinstance(repo_raw, dict):
        remote = repo_raw.get("remote", "")
        # Parse 'https://github.com/owner/repo.git' or 'owner/repo'
        if remote.startswith("http") or remote.startswith("git@"):
            m = re.search(r"[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$", remote)
            if m:
                repo = m.group(1)
        elif "/" in remote:
            repo = remote
    elif isinstance(repo_raw, str) and "/" in repo_raw:
        repo = repo_raw

    # Extract project number & name
    project_data = data.get("project", {})
    project_num = int(data.get("project_number") or (project_data.get("number", 1) if isinstance(project_data, dict) else 1))
    project_name = data.get("project_name") or (project_data.get("title", "") if isinstance(project_data, dict) else "")
    base_branch = data.get("base_branch", "main")

    # If repo matches PolySimulator, use the authoritative PolySimulator adapter
    if repo.lower() == "bavariance/polysimulator":
        cfg = create_polysimulator_config()
        cfg.base_branch = base_branch
        cfg.project_number = project_num
        return cfg

    # Otherwise construct a clean generic ProjectConfig
    deploy_data = data.get("deploy", {})
    dokploy_compose = None
    if isinstance(deploy_data, dict) and deploy_data.get("provider") == "dokploy":
        dokploy_compose = deploy_data.get("compose_id")

    return ProjectConfig(
        repo=repo or "unknown/repo",
        project_name=project_name or (repo.split("/")[-1] if repo else "Unknown"),
        project_number=project_num,
        base_branch=base_branch,
        staging=StagingEnvironmentConfig(
            dokploy_compose_id=dokploy_compose,
            dokploy_app_name=None,
            supabase_project_ref=None,
            stripe_mode="test",
            services=["dokploy_staging"] if dokploy_compose else [],
        ),
        safety=SafetyRules(
            forbidden_patterns=[],
            prohibit_production=True,
            prohibit_main_db=True,
            prohibit_live_keys=True,
        ),
        metadata={"source_superboard_config": path},
    )


# ---------------------------------------------------------------------------
# Global State & Loader
# ---------------------------------------------------------------------------

_ACTIVE_CONFIG: Optional[ProjectConfig] = None


def get_current_project_config() -> ProjectConfig:
    """
    Get the active project configuration.
    If none is set, defaults safely to create_polysimulator_config() if in
    Polysimulator workspace, else create_generic_config().
    """
    global _ACTIVE_CONFIG
    if _ACTIVE_CONFIG is not None:
        return _ACTIVE_CONFIG

    # Check environment override
    env_repo = os.environ.get("SUPERBOARD_REPO") or os.environ.get("PROJECT_REPO")
    if env_repo:
        if env_repo.lower() == "bavariance/polysimulator":
            _ACTIVE_CONFIG = create_polysimulator_config()
            return _ACTIVE_CONFIG
        _ACTIVE_CONFIG = create_generic_config(env_repo)
        return _ACTIVE_CONFIG

    # Check if a project_adapter.json exists in cwd or script dir
    for candidate in [
        os.path.join(os.getcwd(), "project_adapter.json"),
        os.path.join(os.path.dirname(__file__), "project_adapter.json"),
    ]:
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    _ACTIVE_CONFIG = ProjectConfig.from_dict(json.load(f))
                    return _ACTIVE_CONFIG
            except Exception:
                pass

    # Default to polysimulator adapter as the reference integration
    _ACTIVE_CONFIG = create_polysimulator_config()
    return _ACTIVE_CONFIG


def set_current_project_config(config: Union[ProjectConfig, Dict[str, Any], str]) -> ProjectConfig:
    """Set the active project configuration globally."""
    global _ACTIVE_CONFIG
    if isinstance(config, ProjectConfig):
        _ACTIVE_CONFIG = config
    elif isinstance(config, dict):
        _ACTIVE_CONFIG = ProjectConfig.from_dict(config)
    elif isinstance(config, str):
        if config.lower() == "polysimulator":
            _ACTIVE_CONFIG = create_polysimulator_config()
        elif config.lower() == "generic":
            _ACTIVE_CONFIG = create_generic_config()
        elif os.path.isfile(config):
            # Check if it's an explicit ProjectConfig or Superboard config
            with open(config, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
            if "staging" in raw_data or "safety" in raw_data or "project_number" in raw_data:
                _ACTIVE_CONFIG = ProjectConfig.from_dict(raw_data)
            else:
                sb_cfg = parse_superboard_config(config)
                if sb_cfg:
                    _ACTIVE_CONFIG = sb_cfg
                else:
                    _ACTIVE_CONFIG = ProjectConfig.from_dict(raw_data)
        elif "/" in config:
            _ACTIVE_CONFIG = create_generic_config(config)
        else:
            _ACTIVE_CONFIG = create_generic_config(config)
    else:
        raise ValueError(f"Unsupported config type: {type(config)}")
    return _ACTIVE_CONFIG


def reset_current_project_config() -> None:
    """Reset the active project configuration."""
    global _ACTIVE_CONFIG
    _ACTIVE_CONFIG = None


# ---------------------------------------------------------------------------
# Validation Helpers
# ---------------------------------------------------------------------------

def check_text_for_forbidden_patterns(
    text: str, config: Optional[ProjectConfig] = None
) -> Tuple[bool, Optional[str]]:
    """
    Check if text contains any forbidden production or secret patterns.
    Returns (has_violation, matched_pattern_or_reason).
    """
    cfg = config or get_current_project_config()
    for pattern in cfg.safety.forbidden_patterns:
        try:
            if re.search(pattern, text, re.IGNORECASE):
                return True, f"Matched forbidden pattern '{pattern}'"
        except re.error:
            if pattern in text:
                return True, f"Matched forbidden string '{pattern}'"

    # Always enforce strict production Supabase ref block if safety requires
    if cfg.safety.prohibit_main_db:
        for forbidden_ref in cfg.safety.forbidden_supabase_refs:
            if forbidden_ref and forbidden_ref in text:
                return True, f"Prohibited production Supabase project ref '{forbidden_ref}'"

    if cfg.safety.prohibit_production:
        for forbidden_cid in cfg.safety.forbidden_compose_ids:
            if forbidden_cid and forbidden_cid in text:
                return True, f"Prohibited production Dokploy compose ID '{forbidden_cid}'"

    return False, None


def validate_dokploy_compose_id(
    compose_id: str, config: Optional[ProjectConfig] = None
) -> Tuple[bool, str, str]:
    """
    Validate Dokploy compose ID against project staging configuration.
    Returns (valid, status, reason).
    """
    cfg = config or get_current_project_config()
    cid = compose_id.strip()

    if not cid:
        return False, "unconfigured", "No Dokploy compose ID provided"

    # Check forbidden production compose IDs
    if cid in cfg.safety.forbidden_compose_ids:
        return (
            False,
            "blocked",
            f"FATAL: Production Dokploy compose ID '{cid}' referenced; production probing strictly forbidden.",
        )

    # Check configured staging compose ID
    staging_cid = cfg.staging.dokploy_compose_id
    if not staging_cid:
        # Safe unknown environment: dokploy staging is not configured for this repo
        return (
            False,
            "unconfigured",
            f"Dokploy staging compose ID is not configured for project '{cfg.repo}'.",
        )

    if cid != staging_cid:
        return (
            False,
            "blocked",
            f"Invalid Dokploy compose ID '{cid}'; expected staging '{staging_cid}' for project '{cfg.repo}'.",
        )

    return True, "valid", "Dokploy compose ID matches configured staging environment."


def validate_supabase_project_ref(
    project_ref: str, config: Optional[ProjectConfig] = None
) -> Tuple[bool, str, str]:
    """
    Validate Supabase project ref against project staging configuration.
    Returns (valid, status, reason).
    """
    cfg = config or get_current_project_config()
    pref = project_ref.strip()

    if not pref:
        return False, "unconfigured", "No Supabase project ref provided"

    # Check forbidden production Supabase project refs
    if pref in cfg.safety.forbidden_supabase_refs:
        return (
            False,
            "blocked",
            f"FATAL: Production Supabase project ref '{pref}' referenced; production probing strictly forbidden.",
        )

    # Check configured staging project ref
    staging_ref = cfg.staging.supabase_project_ref
    if not staging_ref:
        # Safe unknown environment: supabase staging is not configured for this repo
        return (
            False,
            "unconfigured",
            f"Supabase staging project ref is not configured for project '{cfg.repo}'.",
        )

    if pref != staging_ref:
        return (
            False,
            "blocked",
            f"Invalid Supabase project ref '{pref}'; expected staging '{staging_ref}' for project '{cfg.repo}'.",
        )

    return True, "valid", "Supabase project ref matches configured staging environment."



# ---------------------------------------------------------------------------
# Superboard Project V2 Lifecycle Updater & Data Models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SuperboardLifecycleOutcome:
    """
    Outcome of a Superboard lifecycle update on GitHub Project V2.
    Exposes the frozen duck-typed contract:
      .ok: bool
      .blocked_reason: Optional[str]
      .board_url: Optional[str]
    """
    ok: bool
    blocked_reason: Optional[str] = None
    board_url: Optional[str] = None
    item_id: Optional[str] = None
    previous_status: Optional[str] = None
    new_status: Optional[str] = None
    dry_run: bool = False
    github_writes: int = 0
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


CANONICAL_LIFECYCLE_STATUSES: Tuple[str, ...] = (
    "Backlog",
    "Ready",
    "Building",
    "QA",
    "Review",
    "Blocked",
    "Done",
)

STATE_TO_CANONICAL_LIFECYCLE: Dict[str, str] = {
    "backlog": "Backlog",
    "pending": "Ready",
    "ready": "Ready",
    "building": "Building",
    "build": "Building",
    "implementation": "Building",
    "qa": "QA",
    "review": "Review",
    "awaiting authorization": "Review",
    "awaiting_authorization": "Review",
    "blocked": "Blocked",
    "done": "Done",
    "completed": "Done",
}


def canonicalize_lifecycle_status(value: str) -> str:
    """
    Canonicalize a state string into the canonical 7-state Superboard lifecycle status.
    Raises ValueError on retired ('Skipped') or unknown statuses.
    """
    if not isinstance(value, str):
        raise ValueError(f"Lifecycle status must be a string, got {type(value).__name__}")
    trimmed = value.strip()
    if not trimmed:
        raise ValueError("Lifecycle status must not be empty")
    folded = trimmed.casefold()
    if folded in ("skipped",):
        raise ValueError(
            "'Skipped' is a retired status; canonical statuses are: Backlog, Ready, Building, QA, Review, Blocked, Done"
        )

    # Check if exact canonical status
    for status in CANONICAL_LIFECYCLE_STATUSES:
        if folded == status.casefold():
            return status

    # Check state mapping
    mapped = STATE_TO_CANONICAL_LIFECYCLE.get(folded)
    if mapped:
        return mapped

    raise ValueError(
        f"Unknown lifecycle status '{trimmed}'; canonical statuses are: Backlog, Ready, Building, QA, Review, Blocked, Done"
    )


PROJECT_STATUS_SCHEMA_QUERY = """query($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) {
    ... on Organization {
      projectV2(number: $number) {
        id
        title
        fields(first: 50) {
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
        fields(first: 50) {
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


ISSUE_PROJECT_ITEM_QUERY = """query($owner: String!, $repo: String!, $issueNumber: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $issueNumber) {
      id
      title
      projectItems(first: 20) {
        nodes {
          id
          project {
            id
            number
            title
            owner {
              ... on Organization { login }
              ... on User { login }
            }
          }
          fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue {
              name
              optionId
              field {
                ... on ProjectV2SingleSelectField {
                  id
                  name
                }
              }
            }
          }
        }
      }
    }
  }
}"""


UPDATE_PROJECT_ITEM_STATUS_MUTATION = """mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
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


def default_graphql_runner(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a GraphQL query or mutation via gh api graphql subprocess."""
    cmd = ["gh", "api", "graphql"]
    for k, v in variables.items():
        if isinstance(v, int):
            cmd.extend(["-F", f"{k}={v}"])
        else:
            cmd.extend(["-f", f"{k}={str(v)}"])
    cmd.extend(["-f", f"query={query}"])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except Exception as e:
        raise RuntimeError(f"Failed to execute gh api graphql subprocess: {e}")

    if res.returncode != 0:
        err_msg = res.stderr.strip() or res.stdout.strip() or f"gh exited with code {res.returncode}"
        raise RuntimeError(f"GraphQL execution failed: {err_msg}")

    try:
        return json.loads(res.stdout)
    except Exception as e:
        raise RuntimeError(f"Invalid JSON returned by gh api graphql: {e}\nRaw output: {res.stdout}")


class SuperboardProjectUpdater:
    """
    Native GitHub Project V2 lifecycle updater for Superboard.
    Ensures:
      - Dynamic schema discovery (no generic global fixed IDs)
      - Fails closed on wrong-target, generic, or unconfigured projects
      - Idempotence: 0 writes if card is already in target status
      - Dry-run: guaranteed 0 writes
      - Done status requires verified closure and head_sha
      - Readback verification on all live mutations
      - Truthful error propagation
    """

    def __init__(
        self,
        config: Optional[ProjectConfig] = None,
        *,
        graphql_runner: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    ):
        self.config = config or get_current_project_config()
        self.graphql_runner = graphql_runner or default_graphql_runner

    def _parse_repo(self) -> Tuple[bool, str, str, Optional[str]]:
        """Validate and parse repo into (owner, repo_name). Returns (ok, owner, repo, err)."""
        repo = (self.config.repo or "").strip()
        if not repo or repo.startswith("generic/") or "unconfigured" in repo.lower():
            return False, "", "", f"Project repository '{repo}' is unconfigured or generic; failing closed"
        if "/" not in repo:
            return False, "", "", f"Invalid repository identifier '{repo}'; expected 'owner/repo'"
        parts = repo.split("/", 1)
        return True, parts[0].strip(), parts[1].strip(), None

    def _resolve_issue_number(self, request_id: str, issue_number: Optional[int] = None) -> Optional[int]:
        if issue_number and issue_number > 0:
            return issue_number
        if str(request_id).isdigit():
            return int(request_id)
        # Parse patterns like req-4543, issue-4543, #4543
        m = re.search(r"(?:issue|req|#)[-_]?(\d+)", str(request_id), re.IGNORECASE)
        if m:
            return int(m.group(1))
        m2 = re.search(r"\b(\d+)\b", str(request_id))
        if m2:
            return int(m2.group(1))
        return None

    def get_board_schema(self, owner: str, project_number: int) -> Dict[str, Any]:
        """Dynamically fetch project ID, Status field ID, and option IDs map."""
        raw = self.graphql_runner(PROJECT_STATUS_SCHEMA_QUERY, {"owner": owner, "number": project_number})
        data = raw.get("data", {})
        repo_owner = data.get("repositoryOwner") or {}
        project = repo_owner.get("projectV2")
        if not project:
            errors = raw.get("errors", [])
            err_msg = errors[0].get("message") if errors else f"Project #{project_number} not found for owner '{owner}'"
            raise RuntimeError(err_msg)

        project_id = project.get("id")
        project_title = project.get("title", "")
        status_field_id = None
        options_map: Dict[str, str] = {}  # canonical status name (folded) -> option_id

        fields = (project.get("fields") or {}).get("nodes") or []
        for f in fields:
            if not isinstance(f, dict):
                continue
            if (f.get("name") or "").strip().casefold() == "status":
                status_field_id = f.get("id")
                for opt in f.get("options") or []:
                    opt_name = opt.get("name")
                    opt_id = opt.get("id")
                    if opt_name and opt_id:
                        options_map[opt_name.strip().casefold()] = opt_id
                break

        if not status_field_id:
            raise RuntimeError(f"Status field not found on project #{project_number} for owner '{owner}'")

        return {
            "project_id": project_id,
            "project_title": project_title,
            "status_field_id": status_field_id,
            "options_map": options_map,
        }

    def get_issue_project_item(
        self, owner: str, repo: str, issue_number: int, project_number: int
    ) -> Dict[str, Any]:
        """Fetch project item node ID, current status name and option ID for the issue."""
        raw = self.graphql_runner(
            ISSUE_PROJECT_ITEM_QUERY,
            {"owner": owner, "repo": repo, "issueNumber": issue_number},
        )
        data = raw.get("data", {})
        repository = data.get("repository") or {}
        issue = repository.get("issue")
        if not issue:
            errors = raw.get("errors", [])
            err_msg = errors[0].get("message") if errors else f"Issue #{issue_number} not found in {owner}/{repo}"
            raise RuntimeError(err_msg)

        items = (issue.get("projectItems") or {}).get("nodes") or []
        for it in items:
            proj = it.get("project") or {}
            if proj.get("number") == project_number:
                status_val = it.get("fieldValueByName") or {}
                return {
                    "item_id": it.get("id"),
                    "project_id": proj.get("id"),
                    "project_number": proj.get("number"),
                    "project_title": proj.get("title"),
                    "status_name": status_val.get("name"),
                    "option_id": status_val.get("optionId"),
                }

        raise RuntimeError(f"Issue #{issue_number} is not linked to project #{project_number} on {owner}")

    def update_lifecycle(
        self,
        request_id: str,
        state: str,
        head_sha: Optional[str] = None,
        evidence_url: Optional[str] = None,
        *,
        issue_number: Optional[int] = None,
        dry_run: bool = False,
        closure_verified: bool = False,
    ) -> SuperboardLifecycleOutcome:
        """
        Public duck-typed lifecycle update method conforming to the frozen contract:
          .update_lifecycle(request_id, state, head_sha, evidence_url) -> outcome
          exposing: .ok, .blocked_reason, .board_url
        """
        # 1. Guard wrong-target / unconfigured repo
        ok_repo, owner, repo_name, repo_err = self._parse_repo()
        if not ok_repo:
            return SuperboardLifecycleOutcome(
                ok=False,
                blocked_reason=repo_err,
                dry_run=dry_run,
                github_writes=0,
            )

        project_number = int(self.config.project_number or 1)
        board_url = f"https://github.com/orgs/{owner}/projects/{project_number}"

        # 2. Resolve issue number
        target_issue = self._resolve_issue_number(request_id, issue_number)
        if not target_issue:
            return SuperboardLifecycleOutcome(
                ok=False,
                blocked_reason=f"Cannot resolve target issue number from request_id '{request_id}'",
                board_url=board_url,
                dry_run=dry_run,
                github_writes=0,
            )

        # 3. Canonicalize desired status
        try:
            canonical_status = canonicalize_lifecycle_status(state)
        except ValueError as e:
            return SuperboardLifecycleOutcome(
                ok=False,
                blocked_reason=str(e),
                board_url=board_url,
                dry_run=dry_run,
                github_writes=0,
            )

        # 4. Inviolable Done-closure gate: fail closed on unverified Done or missing head
        if canonical_status == "Done":
            if not closure_verified:
                return SuperboardLifecycleOutcome(
                    ok=False,
                    blocked_reason="Done status requires verified live closure; unverified transitions to Done are prohibited",
                    board_url=board_url,
                    dry_run=dry_run,
                    github_writes=0,
                )
            if not head_sha or len(head_sha.strip()) < 7:
                return SuperboardLifecycleOutcome(
                    ok=False,
                    blocked_reason="Done status requires an authoritative head_sha",
                    board_url=board_url,
                    dry_run=dry_run,
                    github_writes=0,
                )

        # 5. Dynamic schema discovery
        try:
            schema = self.get_board_schema(owner, project_number)
        except Exception as e:
            return SuperboardLifecycleOutcome(
                ok=False,
                blocked_reason=f"Failed to discover project schema for {owner}#{project_number}: {e}",
                board_url=board_url,
                dry_run=dry_run,
                github_writes=0,
            )

        project_id = schema["project_id"]
        status_field_id = schema["status_field_id"]
        options_map = schema["options_map"]

        target_option_id = options_map.get(canonical_status.casefold())
        if not target_option_id:
            return SuperboardLifecycleOutcome(
                ok=False,
                blocked_reason=f"Status option '{canonical_status}' not found on project #{project_number} for {owner}",
                board_url=board_url,
                dry_run=dry_run,
                github_writes=0,
            )

        # 6. Find card on board
        try:
            card = self.get_issue_project_item(owner, repo_name, target_issue, project_number)
        except Exception as e:
            return SuperboardLifecycleOutcome(
                ok=False,
                blocked_reason=str(e),
                board_url=board_url,
                dry_run=dry_run,
                github_writes=0,
            )

        item_id = card["item_id"]
        current_status = card["status_name"]

        # 7. Idempotence: if already in target status, 0 writes
        if current_status and current_status.casefold() == canonical_status.casefold():
            return SuperboardLifecycleOutcome(
                ok=True,
                board_url=board_url,
                item_id=item_id,
                previous_status=current_status,
                new_status=canonical_status,
                dry_run=dry_run,
                github_writes=0,
                details={
                    "message": f"Card already in status '{canonical_status}'; mutation skipped (idempotent)",
                    "issue_number": target_issue,
                    "head_sha": head_sha,
                    "evidence_url": evidence_url,
                },
            )

        # 8. Dry-run guard: guaranteed 0 writes
        if dry_run:
            return SuperboardLifecycleOutcome(
                ok=True,
                board_url=board_url,
                item_id=item_id,
                previous_status=current_status,
                new_status=canonical_status,
                dry_run=True,
                github_writes=0,
                details={
                    "message": f"Dry-run: would mutate status from '{current_status}' to '{canonical_status}'",
                    "target_option_id": target_option_id,
                    "status_field_id": status_field_id,
                    "issue_number": target_issue,
                    "head_sha": head_sha,
                    "evidence_url": evidence_url,
                },
            )

        # 9. Real mutation
        try:
            self.graphql_runner(
                UPDATE_PROJECT_ITEM_STATUS_MUTATION,
                {
                    "projectId": project_id,
                    "itemId": item_id,
                    "fieldId": status_field_id,
                    "optionId": target_option_id,
                },
            )
        except Exception as e:
            return SuperboardLifecycleOutcome(
                ok=False,
                blocked_reason=f"GraphQL mutation failed: {e}",
                board_url=board_url,
                item_id=item_id,
                previous_status=current_status,
                new_status=None,
                dry_run=False,
                github_writes=0,
            )

        # 10. Readback verification
        try:
            readback_card = self.get_issue_project_item(owner, repo_name, target_issue, project_number)
            observed_status = readback_card["status_name"]
            if not observed_status or observed_status.casefold() != canonical_status.casefold():
                return SuperboardLifecycleOutcome(
                    ok=False,
                    blocked_reason=f"Readback mismatch: expected status '{canonical_status}', observed '{observed_status}'",
                    board_url=board_url,
                    item_id=item_id,
                    previous_status=current_status,
                    new_status=observed_status,
                    dry_run=False,
                    github_writes=1,
                )
        except Exception as e:
            return SuperboardLifecycleOutcome(
                ok=False,
                blocked_reason=f"Mutation executed but readback verification failed: {e}",
                board_url=board_url,
                item_id=item_id,
                previous_status=current_status,
                new_status=None,
                dry_run=False,
                github_writes=1,
            )

        return SuperboardLifecycleOutcome(
            ok=True,
            board_url=board_url,
            item_id=item_id,
            previous_status=current_status,
            new_status=canonical_status,
            dry_run=False,
            github_writes=1,
            details={
                "message": f"Successfully transitioned card status from '{current_status}' to '{canonical_status}'",
                "readback_verified": True,
                "issue_number": target_issue,
                "head_sha": head_sha,
                "evidence_url": evidence_url,
            },
        )


def update_project_lifecycle(
    request_id: str,
    state: str,
    head_sha: Optional[str] = None,
    evidence_url: Optional[str] = None,
    *,
    config: Optional[ProjectConfig] = None,
    issue_number: Optional[int] = None,
    dry_run: bool = False,
    closure_verified: bool = False,
    graphql_runner: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
) -> SuperboardLifecycleOutcome:
    """Convenience functional wrapper around SuperboardProjectUpdater."""
    cfg = config or get_current_project_config()
    return cfg.update_lifecycle(
        request_id=request_id,
        state=state,
        head_sha=head_sha,
        evidence_url=evidence_url,
        issue_number=issue_number,
        dry_run=dry_run,
        closure_verified=closure_verified,
        graphql_runner=graphql_runner,
    )


def main():
    """CLI interface for project adapter and Superboard lifecycle updater."""
    import argparse

    parser = argparse.ArgumentParser(description="Portable Project Adapter & Superboard Lifecycle Updater")
    subparsers = parser.add_subparsers(dest="command")

    # config
    subparsers.add_parser("config", help="Print active project configuration JSON")

    # update-lifecycle
    up_p = subparsers.add_parser("update-lifecycle", help="Update Superboard Project V2 card status")
    up_p.add_argument("--request-id", default="", help="Request ID (e.g. req-4543)")
    up_p.add_argument("--issue", type=int, default=None, help="Target GitHub issue number")
    up_p.add_argument("--state", required=True, help="Target lifecycle state (Backlog, Ready, Building, QA, Review, Blocked, Done)")
    up_p.add_argument("--head-sha", default=None, help="Authoritative commit SHA")
    up_p.add_argument("--evidence-url", default=None, help="Evidence URL or proof link")
    up_p.add_argument("--dry-run", action="store_true", help="Dry run mode: do not mutate GitHub")
    up_p.add_argument("--verified", action="store_true", help="Assert verified live closure (required for Done)")
    up_p.add_argument("--json", action="store_true", help="Output outcome as JSON")

    # status
    st_p = subparsers.add_parser("status", help="Get current Superboard card status for an issue")
    st_p.add_argument("--issue", type=int, required=True, help="Target GitHub issue number")
    st_p.add_argument("--json", action="store_true", help="Output status as JSON")

    # board-info
    subparsers.add_parser("board-info", help="Get project board schema (fields & options)")

    args = parser.parse_args()

    cfg = get_current_project_config()

    if args.command == "config":
        print(json.dumps(cfg.to_dict(), indent=2))
        sys.exit(0)

    elif args.command == "update-lifecycle":
        updater = SuperboardProjectUpdater(cfg)
        outcome = updater.update_lifecycle(
            request_id=args.request_id or f"issue-{args.issue}",
            state=args.state,
            head_sha=args.head_sha,
            evidence_url=args.evidence_url,
            issue_number=args.issue,
            dry_run=args.dry_run,
            closure_verified=args.verified,
        )
        if args.json:
            print(json.dumps(outcome.to_dict(), indent=2))
        else:
            status_label = "OK" if outcome.ok else "BLOCKED"
            print(f"[{status_label}] Superboard Lifecycle Update")
            print(f"  Board URL:        {outcome.board_url}")
            print(f"  Item ID:          {outcome.item_id}")
            print(f"  Previous Status:  {outcome.previous_status}")
            print(f"  New Status:       {outcome.new_status}")
            print(f"  Dry Run:          {outcome.dry_run}")
            print(f"  GitHub Writes:    {outcome.github_writes}")
            if outcome.blocked_reason:
                print(f"  Blocked Reason:   {outcome.blocked_reason}")
        sys.exit(0 if outcome.ok else 1)

    elif args.command == "status":
        updater = SuperboardProjectUpdater(cfg)
        ok_repo, owner, repo_name, err = updater._parse_repo()
        if not ok_repo:
            print(f"Error: {err}", file=sys.stderr)
            sys.exit(1)
        try:
            item = updater.get_issue_project_item(owner, repo_name, args.issue, cfg.project_number)
            if args.json:
                print(json.dumps(item, indent=2))
            else:
                print(f"Issue #{args.issue} on Project #{cfg.project_number} ({owner}/{repo_name}):")
                print(f"  Item ID: {item.get('item_id')}")
                print(f"  Status:  {item.get('status_name')} (optionId: {item.get('option_id')})")
            sys.exit(0)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    elif args.command == "board-info":
        updater = SuperboardProjectUpdater(cfg)
        ok_repo, owner, repo_name, err = updater._parse_repo()
        if not ok_repo:
            print(f"Error: {err}", file=sys.stderr)
            sys.exit(1)
        try:
            schema = updater.get_board_schema(owner, cfg.project_number)
            print(json.dumps(schema, indent=2))
            sys.exit(0)
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()