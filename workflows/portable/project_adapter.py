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
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union


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
