"""Executable schema validation and normalized defaults.

`skills/super-board/references/config-schema.json` documents the contract for
humans; this module *is* the contract. Every rejection carries a stable
machine-readable ``reason`` so shell callers and evidence records can key off it
instead of parsing prose.

Nothing here touches the network. The one check that needs GitHub — proving a
`proof-only` allowlist URL points at an OPEN issue — is injected by the caller
as ``issue_state_lookup`` and fails closed when it is unavailable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional

from .lifecycle import LifecycleError, canonicalize_status

WORKER_BACKENDS: tuple[str, ...] = ("claude-p", "workflow")
ACTIVATION_MODES: tuple[str, ...] = ("off", "proof-only", "active")
MERGE_METHODS: tuple[str, ...] = ("rebase", "squash", "merge")
GITHUB_AUTH_MODES: tuple[str, ...] = ("interactive", "unattended")

#: Labels that are non-dispatchable no matter what a config says. `design` cards
#: are human-designer-owned; `history` cards are an archive, not work.
PERMANENT_EXCLUDED_LABELS: tuple[str, ...] = ("design", "history")

#: How many REBUILDS a card may take beyond its first attempt. The number of
#: attempts a card gets is therefore `rebuild_cap + 1`, which is what the status
#: renderer shows as the denominator. One authority for the default, so a
#: renderer cannot quietly disagree with the validator about it.
DEFAULT_REBUILD_CAP = 2

#: The GraphQL reserve floor. A config may raise it; lowering it is an error.
MINIMUM_GRAPHQL_RESERVE = 1000

#: Unattended mutation credentials are read from these environment variable
#: NAMES only. No value of either ever enters a config file, log, or evidence
#: record.
TOKEN_ENV_VAR = "SUPERBOARD_GITHUB_TOKEN"
LOGIN_ENV_VAR = "SUPERBOARD_GITHUB_LOGIN"
REQUIRED_SCOPES: tuple[str, ...] = ("repo", "project", "read:org")

_ISSUE_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)/issues/(?P<number>\d+)$"
)
_REPO_REMOTE_RE = re.compile(r"^(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?$")

# Anything that looks like credential material must never live in a config file.
_CREDENTIAL_VALUE_RE = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bBearer\s+[A-Za-z0-9._\-]{16,}|[Aa]uthorization\s*:\s*\S+)"
)
_CREDENTIAL_KEYS = frozenset(
    {"token", "api_key", "apikey", "password", "secret", "private_key", "authorization", "cookie"}
)


class ConfigError(ValueError):
    """Invalid configuration. Maps to exit code 65."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class NormalizedConfig:
    """A validated config with every default resolved."""

    version: int
    project_owner: str
    project_number: int
    repo_remote: Optional[str]
    base_branch: str
    branch_routes: Mapping[str, str]
    require_branch_route_declaration: bool
    worker_backend: str
    human_approves_merge: bool
    merge_method: str
    activation_mode: str
    proof_issue_url: Optional[str]
    max_workers: int
    rebuild_cap: int
    exclude_labels: tuple[str, ...]
    minimum_graphql_reserve: int
    status_aliases: Mapping[str, str]
    github_auth: Mapping[str, Any]
    design_skill: Mapping[str, Any]
    agent_native: Mapping[str, Any]
    deploy: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Plain JSON-ready dict; tuples become lists, proxies become dicts."""
        out: dict[str, Any] = {}
        for field in fields(self):
            out[field.name] = _plain(getattr(self, field.name))
        return out


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(val) for key, val in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


# ───────────────────────────── field helpers ─────────────────────────────


def _require_mapping(value: Any, reason: str, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(reason, f"{label} must be an object")
    return value


def _string(raw: Mapping[str, Any], key: str, default: Optional[str], reason: str) -> Optional[str]:
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(reason, f"{key} must be a non-empty string")
    return value.strip()


def _bool(raw: Mapping[str, Any], key: str, default: bool, reason: str) -> bool:
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        raise ConfigError(reason, f"{key} must be a boolean")
    return value


def _int(raw: Mapping[str, Any], key: str, default: int, reason: str, minimum: int) -> int:
    if key not in raw or raw[key] is None:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(reason, f"{key} must be an integer")
    if value < minimum:
        raise ConfigError(reason, f"{key} must be >= {minimum}, got {value}")
    return value


def _enum(value: str, allowed: tuple[str, ...], reason: str, label: str) -> str:
    if value not in allowed:
        raise ConfigError(reason, f"{label} must be one of {', '.join(allowed)}; got {value!r}")
    return value


def _scan_for_credentials(node: Any, path: str = "$") -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(key, str) and key.strip().casefold() in _CREDENTIAL_KEYS and value is not None:
                raise ConfigError(
                    "config-inline-credential",
                    f"{path}.{key} looks like inline credential material; reference the "
                    f"environment variable NAME ({TOKEN_ENV_VAR}) instead",
                )
            _scan_for_credentials(value, f"{path}.{key}")
        return
    if isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _scan_for_credentials(value, f"{path}[{index}]")
        return
    if isinstance(node, str) and _CREDENTIAL_VALUE_RE.search(node):
        raise ConfigError(
            "config-inline-credential",
            f"{path} contains what looks like credential material; reference the "
            f"environment variable NAME ({TOKEN_ENV_VAR}) instead",
        )


def _normalize_repo_remote(raw: Mapping[str, Any]) -> Optional[str]:
    value: Any = None
    repo = raw.get("repo")
    if isinstance(repo, Mapping):
        value = repo.get("remote")
    elif isinstance(repo, str):
        value = repo
    if value is None:
        value = raw.get("repo_remote")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("repo-remote-invalid", "repo.remote must be a non-empty string or null")
    text = value.strip()
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.strip("/")
    match = _REPO_REMOTE_RE.match(text)
    if not match:
        raise ConfigError(
            "repo-remote-invalid",
            f"repo.remote must be 'owner/name' or a github.com URL; got {value!r}",
        )
    return f"{match.group('owner')}/{match.group('repo')}"


def _normalize_branch_routes(raw: Mapping[str, Any]) -> Mapping[str, str]:
    value = raw.get("branch_routes")
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ConfigError("branch-routes-invalid", "branch_routes must be an object")
    routes: dict[str, str] = {}
    for key, branch in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigError("branch-routes-invalid", "branch_routes keys must be non-empty strings")
        if not isinstance(branch, str) or not branch.strip():
            raise ConfigError(
                "branch-routes-invalid",
                f"branch_routes[{key!r}] must name a non-empty branch",
            )
        routes[key.strip()] = branch.strip()
    return MappingProxyType(routes)


def _normalize_exclude_labels(raw: Mapping[str, Any]) -> tuple[str, ...]:
    value = raw.get("exclude_labels")
    labels = set(PERMANENT_EXCLUDED_LABELS)
    if value is not None:
        if not isinstance(value, list):
            raise ConfigError("exclude-labels-invalid", "exclude_labels must be an array of strings")
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ConfigError(
                    "exclude-labels-invalid", "exclude_labels entries must be non-empty strings"
                )
            labels.add(item.strip().casefold())
    return tuple(sorted(labels))


def _normalize_status_aliases(raw: Mapping[str, Any]) -> Mapping[str, str]:
    value = raw.get("status_aliases")
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ConfigError("status-aliases-invalid", "status_aliases must be an object")
    aliases: dict[str, str] = {}
    for alias, target in value.items():
        if not isinstance(alias, str) or not alias.strip():
            raise ConfigError("status-aliases-invalid", "status_aliases keys must be non-empty strings")
        if not isinstance(target, str):
            raise ConfigError(
                "status-aliases-invalid", f"status_aliases[{alias!r}] must be a lifecycle status name"
            )
        try:
            aliases[alias.strip()] = canonicalize_status(target)
        except LifecycleError as exc:
            raise ConfigError(exc.reason, str(exc)) from exc
    return MappingProxyType(aliases)


def _normalize_github_auth(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    value = raw.get("github_auth")
    if value is None:
        value = {}
    value = _require_mapping(value, "github-auth-invalid", "github_auth")

    mode = _enum(
        _string(value, "mode", "interactive", "github-auth-invalid") or "interactive",
        GITHUB_AUTH_MODES,
        "github-auth-mode-invalid",
        "github_auth.mode",
    )
    token_env_var = _string(value, "token_env_var", TOKEN_ENV_VAR, "github-auth-token-env-var-invalid")
    if token_env_var != TOKEN_ENV_VAR:
        raise ConfigError(
            "github-auth-token-env-var-invalid",
            f"github_auth.token_env_var must be {TOKEN_ENV_VAR}",
        )
    login_env_var = _string(value, "login_env_var", LOGIN_ENV_VAR, "github-auth-login-env-var-invalid")
    if login_env_var != LOGIN_ENV_VAR:
        raise ConfigError(
            "github-auth-login-env-var-invalid",
            f"github_auth.login_env_var must be {LOGIN_ENV_VAR}",
        )
    expected_login = _string(value, "expected_login", None, "github-auth-invalid")

    scopes_raw = value.get("required_scopes")
    if scopes_raw is None:
        scopes: tuple[str, ...] = REQUIRED_SCOPES
    else:
        if not isinstance(scopes_raw, list) or not all(isinstance(s, str) for s in scopes_raw):
            raise ConfigError("github-auth-invalid", "github_auth.required_scopes must be an array of strings")
        scopes = tuple(dict.fromkeys(s.strip() for s in scopes_raw if s.strip()))
        missing = [scope for scope in REQUIRED_SCOPES if scope not in scopes]
        if missing:
            raise ConfigError(
                "github-auth-scope-incomplete",
                "github_auth.required_scopes must include " + ", ".join(REQUIRED_SCOPES)
                + f"; missing {', '.join(missing)}",
            )

    projects_raw = value.get("required_projects")
    projects: list[dict[str, Any]] = []
    if projects_raw is not None:
        if not isinstance(projects_raw, list):
            raise ConfigError("github-auth-invalid", "github_auth.required_projects must be an array")
        for entry in projects_raw:
            entry = _require_mapping(
                entry, "github-auth-invalid", "github_auth.required_projects[]"
            )
            name = _string(entry, "name", None, "github-auth-invalid")
            owner = _string(entry, "owner", None, "github-auth-invalid")
            number = _int(entry, "number", 0, "github-auth-invalid", 1)
            if not name or not owner or not number:
                raise ConfigError(
                    "github-auth-invalid",
                    "github_auth.required_projects entries need name, owner, and number",
                )
            projects.append({"name": name, "owner": owner, "number": number})

    return MappingProxyType(
        {
            "mode": mode,
            "token_env_var": token_env_var,
            "login_env_var": login_env_var,
            "expected_login": expected_login,
            "required_scopes": tuple(scopes),
            "required_projects": tuple(MappingProxyType(dict(p)) for p in projects),
        }
    )


def _normalize_design_skill(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _require_mapping(raw.get("design_skill") or {}, "design-skill-invalid", "design_skill")
    return MappingProxyType(
        {
            "enabled": _bool(value, "enabled", True, "design-skill-invalid"),
            "label": _string(value, "label", "design", "design-skill-invalid"),
        }
    )


def _normalize_agent_native(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _require_mapping(raw.get("agent_native") or {}, "agent-native-invalid", "agent_native")
    projection_only = _bool(value, "projection_only", True, "agent-native-invalid")
    if not projection_only:
        raise ConfigError(
            "agent-native-must-be-projection-only",
            "Agent Native is projection only: it owns no work state, holds no mutation "
            "credential, and executes no repository code",
        )
    return MappingProxyType(
        {
            "enabled": _bool(value, "enabled", False, "agent-native-invalid"),
            "projection_only": True,
        }
    )


def _normalize_deploy(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    value = _require_mapping(raw.get("deploy") or {}, "deploy-invalid", "deploy")
    return MappingProxyType(
        {
            "provider": _string(value, "provider", None, "deploy-invalid"),
            "auto_deploy": _bool(value, "auto_deploy", False, "deploy-invalid"),
        }
    )


def _normalize_proof_issue_url(
    raw: Mapping[str, Any],
    activation_mode: str,
    repo_remote: Optional[str],
    issue_state_lookup: Optional[Callable[[str], Optional[str]]],
) -> Optional[str]:
    value = raw.get("proof_issue_url")
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ConfigError("proof-url-invalid", "proof_issue_url must be an issue URL or null")
    url = value.strip().rstrip("/") if isinstance(value, str) else None

    if activation_mode in ("off", "active"):
        if url:
            raise ConfigError(
                "proof-url-must-be-null",
                f"proof_issue_url must be null when activation_mode is {activation_mode!r}",
            )
        return None

    # proof-only
    if not url:
        raise ConfigError(
            "proof-url-required",
            "activation_mode 'proof-only' requires proof_issue_url to name the single allowlisted issue",
        )
    match = _ISSUE_URL_RE.match(url)
    if not match:
        raise ConfigError(
            "proof-url-invalid",
            "proof_issue_url must be an exact issue URL "
            "(https://github.com/<owner>/<repo>/issues/<number>)",
        )
    if repo_remote is None:
        raise ConfigError(
            "proof-url-repository-unknown",
            "proof-only activation needs repo.remote so the allowlisted issue can be proven "
            "to live inside the configured repository",
        )
    if f"{match.group('owner')}/{match.group('repo')}".casefold() != repo_remote.casefold():
        raise ConfigError(
            "proof-url-wrong-repository",
            f"proof_issue_url must point inside {repo_remote}",
        )
    if issue_state_lookup is not None:
        try:
            state = issue_state_lookup(url)
        except Exception as exc:  # fail closed: a failed lookup is never permissive
            raise ConfigError(
                "proof-url-state-unavailable",
                f"could not resolve the state of the proof issue: {exc}",
            ) from exc
        if not isinstance(state, str) or not state.strip():
            raise ConfigError(
                "proof-url-state-unavailable",
                "could not resolve the state of the proof issue",
            )
        if state.strip().upper() != "OPEN":
            raise ConfigError(
                "proof-url-issue-not-open",
                f"the proof issue must be OPEN; it is {state.strip().upper()}",
            )
    return url


# ───────────────────────────── entry points ─────────────────────────────


def load_and_validate_config(
    path: Path,
    *,
    issue_state_lookup: Optional[Callable[[str], Optional[str]]] = None,
) -> NormalizedConfig:
    """Read ``path``, validate it, and return a fully-defaulted config.

    ``issue_state_lookup`` is optional and only consulted for ``proof-only``
    activation. When supplied it must return the GitHub state string for an
    issue URL; anything other than ``OPEN`` — including a failed lookup — is a
    configuration error.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError("config-not-found", f"config not found: {path}") from exc
    except OSError as exc:
        raise ConfigError("config-unreadable", f"config could not be read: {path}") from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError("config-not-json", f"config is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config-not-object", "config must be a JSON object")

    if "columns" in raw:
        raise ConfigError(
            "columns-removed-lifecycle-is-fixed",
            "the lifecycle is fixed and not configurable: remove 'columns'",
        )
    _scan_for_credentials(raw)

    version = _int(raw, "version", 1, "version-invalid", 1)
    if version != 1:
        raise ConfigError("version-unsupported", f"unsupported config version: {version}")

    project = _require_mapping(raw.get("project"), "project-invalid", "project")
    project_owner = _string(project, "owner", None, "project-owner-invalid")
    if not project_owner:
        raise ConfigError("project-owner-invalid", "project.owner is required")
    project_number = _int(project, "number", 0, "project-number-invalid", 1)
    if not project_number:
        raise ConfigError("project-number-invalid", "project.number is required and must be >= 1")

    repo_remote = _normalize_repo_remote(raw)
    base_branch = _string(raw, "base_branch", "main", "base-branch-invalid") or "main"
    branch_routes = _normalize_branch_routes(raw)
    # Branch routing is fail-closed — see `routing.py`. Every issue says which
    # branch it targets, in exactly one normalized declaration, or it is not
    # dispatchable. The key survives so an existing config that states the
    # requirement still loads, but it can only ever state it: a board that turns
    # it off is asking for undeclared cards to dispatch on a `base_branch`
    # fallback, and a fallback base branch is a branch nobody chose.
    require_branch_route_declaration = _bool(
        raw, "require_branch_route_declaration", True, "branch-routes-invalid"
    )
    if not require_branch_route_declaration:
        raise ConfigError(
            "branch-route-declaration-required",
            "require_branch_route_declaration may not be false: routing is fail-closed, so a "
            "card declares its branch route or it is not dispatched",
        )

    worker_backend = _enum(
        _string(raw, "worker_backend", "claude-p", "worker-backend-invalid") or "claude-p",
        WORKER_BACKENDS,
        "worker-backend-invalid",
        "worker_backend",
    )
    human_approves_merge = _bool(raw, "human_approves_merge", True, "human-approves-merge-invalid")
    merge_method = _enum(
        _string(raw, "merge_method", "rebase", "merge-method-invalid") or "rebase",
        MERGE_METHODS,
        "merge-method-invalid",
        "merge_method",
    )
    if merge_method != "rebase":
        # Squash collapses the TDD breadcrumb trail; the runtime never merges at
        # all, and the human who does merges by rebase.
        raise ConfigError(
            "merge-method-must-be-rebase",
            "merge_method must be 'rebase': humans approve every merge and history is never squashed",
        )

    activation_mode = _enum(
        _string(raw, "activation_mode", "off", "activation-mode-invalid") or "off",
        ACTIVATION_MODES,
        "activation-mode-invalid",
        "activation_mode",
    )
    proof_issue_url = _normalize_proof_issue_url(
        raw, activation_mode, repo_remote, issue_state_lookup
    )

    max_workers = _int(raw, "max_workers", 3, "max-workers-invalid", 1)
    rebuild_cap = _int(raw, "rebuild_cap", DEFAULT_REBUILD_CAP, "rebuild-cap-invalid", 0)
    exclude_labels = _normalize_exclude_labels(raw)

    reserve = _int(
        raw, "minimum_graphql_reserve", MINIMUM_GRAPHQL_RESERVE, "graphql-reserve-invalid", 0
    )
    if reserve < MINIMUM_GRAPHQL_RESERVE:
        raise ConfigError(
            "graphql-reserve-below-floor",
            f"minimum_graphql_reserve may be raised above {MINIMUM_GRAPHQL_RESERVE} but never "
            f"lowered; got {reserve}",
        )

    return NormalizedConfig(
        version=version,
        project_owner=project_owner,
        project_number=project_number,
        repo_remote=repo_remote,
        base_branch=base_branch,
        branch_routes=branch_routes,
        require_branch_route_declaration=require_branch_route_declaration,
        worker_backend=worker_backend,
        human_approves_merge=human_approves_merge,
        merge_method=merge_method,
        activation_mode=activation_mode,
        proof_issue_url=proof_issue_url,
        max_workers=max_workers,
        rebuild_cap=rebuild_cap,
        exclude_labels=exclude_labels,
        minimum_graphql_reserve=reserve,
        status_aliases=_normalize_status_aliases(raw),
        github_auth=_normalize_github_auth(raw),
        design_skill=_normalize_design_skill(raw),
        agent_native=_normalize_agent_native(raw),
        deploy=_normalize_deploy(raw),
    )


def normalized_config_to_json(config: NormalizedConfig) -> str:
    """Deterministic, key-sorted, UTF-8 JSON rendering of a normalized config."""
    return json.dumps(config.to_dict(), sort_keys=True, indent=2, ensure_ascii=False)


__all__ = [
    "ACTIVATION_MODES",
    "DEFAULT_REBUILD_CAP",
    "GITHUB_AUTH_MODES",
    "LOGIN_ENV_VAR",
    "MERGE_METHODS",
    "MINIMUM_GRAPHQL_RESERVE",
    "PERMANENT_EXCLUDED_LABELS",
    "REQUIRED_SCOPES",
    "TOKEN_ENV_VAR",
    "WORKER_BACKENDS",
    "ConfigError",
    "NormalizedConfig",
    "load_and_validate_config",
    "normalized_config_to_json",
]
