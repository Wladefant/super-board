"""Interactive and unattended GitHub identity verification.

Two modes:

  interactive   the signed-in session identity. No environment credential is
                read, supplied, or required.
  unattended    a machine-account **classic** PAT, read only from the
                environment variable named `SUPERBOARD_GITHUB_TOKEN`, whose
                login must equal `SUPERBOARD_GITHUB_LOGIN`, carrying scopes
                `repo`, `project`, `read:org`.

Everything fails closed with exit 69: a missing variable, the wrong login, a
fine-grained token, a GitHub App installation token, a token whose OAuth scope
header is absent or unparseable, a missing scope, an inaccessible repository or
Project, or any capability that cannot be confirmed.

**GitHub Apps cannot access personal Projects v2 at all.** That is why an app
installation token is refused outright rather than probed: no amount of
capability checking can rescue it, and the pipeline's Projects v2 mutations
would fail at the worst possible moment.

Token values never leave this module. The token class is decided from the
prefix, capability probes receive the token but nothing echoes it, and
`AuthReport` carries no token material at all. Environment variables are
referenced by NAME.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

try:  # normal package import
    from .config import LOGIN_ENV_VAR, REQUIRED_SCOPES, TOKEN_ENV_VAR, NormalizedConfig
except ImportError:  # executed as a plain file path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime.config import (
        LOGIN_ENV_VAR,
        REQUIRED_SCOPES,
        TOKEN_ENV_VAR,
        NormalizedConfig,
    )

AUTH_MODES: tuple[str, ...] = ("interactive", "unattended")

_LEGACY_CLASSIC_RE = re.compile(r"^[A-Fa-f0-9]{40}$")


@dataclass(frozen=True)
class AuthReport:
    ok: bool
    login: Optional[str]
    token_class: str
    scopes: tuple[str, ...]
    capabilities: Mapping[str, bool]
    reason_code: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "login": self.login,
            "token_class": self.token_class,
            "scopes": list(self.scopes),
            "capabilities": dict(self.capabilities),
            "reason_code": self.reason_code,
        }


def classify_token(token: Optional[str]) -> str:
    """Classify a token from its prefix alone. The value is never logged."""
    if not isinstance(token, str) or not token.strip():
        return "absent"
    value = token.strip()
    if value.startswith("github_pat_"):
        return "fine-grained"
    if value.startswith("ghp_"):
        return "classic"
    if value.startswith(("ghs_", "ghu_")):
        return "github-app"
    if value.startswith("gho_"):
        return "oauth-app"
    if _LEGACY_CLASSIC_RE.match(value):
        return "classic"
    return "unknown"


def token_class_explanation(token_class: str) -> str:
    """Human diagnostic for a refused token class. Never quotes the token."""
    if token_class == "fine-grained":
        return (
            "a fine-grained personal access token was supplied; Superboard requires a "
            "machine-account CLASSIC PAT with scopes " + ", ".join(REQUIRED_SCOPES)
        )
    if token_class == "github-app":
        return (
            "a GitHub App token was supplied; GitHub Apps cannot access personal "
            "Projects v2 at all, so no capability probe can rescue it. Use a "
            "machine-account CLASSIC PAT with scopes " + ", ".join(REQUIRED_SCOPES)
        )
    if token_class == "oauth-app":
        return (
            "an OAuth app token was supplied; Superboard requires a machine-account "
            "CLASSIC PAT with scopes " + ", ".join(REQUIRED_SCOPES)
        )
    return (
        f"token class {token_class!r} is not a machine-account CLASSIC PAT with scopes "
        + ", ".join(REQUIRED_SCOPES)
    )


class GhProbe:
    """Default probe. Talks to GitHub through `gh` and returns plain data.

    The token is passed to `gh` through the child process environment only. It
    is never placed on a command line, never logged, and never returned.
    """

    def identity(self, token: Optional[str]) -> Optional[Mapping[str, Any]]:
        env = dict(os.environ)
        if token:
            env["GH_TOKEN"] = token
        try:
            result = subprocess.run(
                ["gh", "api", "--include", "user"],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        headers, _, body = result.stdout.partition("\n\n")
        scopes: Optional[tuple[str, ...]] = None
        scope_header_present = False
        for line in headers.splitlines():
            name, sep, value = line.partition(":")
            if sep and name.strip().casefold() == "x-oauth-scopes":
                scope_header_present = True
                scopes = tuple(s.strip() for s in value.split(",") if s.strip())
        try:
            login = json.loads(body).get("login")
        except (json.JSONDecodeError, AttributeError):
            login = None
        return {"login": login, "scopes": scopes, "scope_header_present": scope_header_present}

    def capability(self, name: str, target: Mapping[str, Any], token: Optional[str]) -> bool:
        env = dict(os.environ)
        if token:
            env["GH_TOKEN"] = token
        if target.get("kind") == "repository":
            command = ["gh", "repo", "view", str(target.get("repo")), "--json", "name"]
        else:
            command = [
                "gh",
                "project",
                "view",
                str(target.get("number")),
                "--owner",
                str(target.get("owner")),
                "--format",
                "json",
            ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0


def _capability_targets(config: NormalizedConfig) -> list[tuple[str, dict[str, Any]]]:
    targets: list[tuple[str, dict[str, Any]]] = []
    if config.repo_remote:
        targets.append(("repository", {"kind": "repository", "repo": config.repo_remote}))
    targets.append(
        (
            "project",
            {
                "kind": "project",
                "owner": config.project_owner,
                "number": config.project_number,
            },
        )
    )
    for entry in config.github_auth.get("required_projects", ()):  # type: ignore[union-attr]
        targets.append(
            (
                str(entry["name"]),
                {"kind": "project", "owner": entry["owner"], "number": entry["number"]},
            )
        )
    return targets


def verify_github_identity(
    config: NormalizedConfig,
    mode: str,
    *,
    env: Optional[Mapping[str, str]] = None,
    probe: Any = None,
) -> AuthReport:
    """Verify the identity Superboard will act as. Never fails open."""
    env = os.environ if env is None else env
    probe = GhProbe() if probe is None else probe

    if mode not in AUTH_MODES:
        return AuthReport(False, None, "unknown", (), {}, "auth-mode-invalid")

    token: Optional[str] = None
    expected_login: Optional[str] = None
    token_class = "session"

    if mode == "unattended":
        token = env.get(TOKEN_ENV_VAR) or None
        if not token:
            return AuthReport(False, None, "absent", (), {}, "token-env-missing")
        expected_login = env.get(LOGIN_ENV_VAR) or config.github_auth.get("expected_login")
        if not expected_login:
            return AuthReport(False, None, classify_token(token), (), {}, "expected-login-missing")
        # Class is decided BEFORE anything is scanned, probed, or mutated.
        token_class = classify_token(token)
        if token_class != "classic":
            return AuthReport(False, None, token_class, (), {}, "token-class-not-classic")

    identity = probe.identity(token)
    if not isinstance(identity, Mapping):
        return AuthReport(False, None, token_class, (), {}, "identity-unavailable")
    login = identity.get("login")
    if not isinstance(login, str) or not login.strip():
        return AuthReport(False, None, token_class, (), {}, "identity-unavailable")
    login = login.strip()

    scopes: tuple[str, ...] = ()
    if mode == "unattended":
        raw_scopes = identity.get("scopes")
        if raw_scopes is None:
            # An absent or unparseable OAuth scope header is ambiguous, and
            # capability probing does not rescue it: a token whose grants we
            # cannot enumerate is a token we will not act with.
            return AuthReport(False, login, token_class, (), {}, "scope-ambiguous")
        scopes = tuple(str(scope).strip() for scope in raw_scopes if str(scope).strip())
        if login != expected_login:
            return AuthReport(False, login, token_class, scopes, {}, "identity-mismatch")
        required = tuple(config.github_auth.get("required_scopes", REQUIRED_SCOPES))
        if any(scope not in scopes for scope in required):
            return AuthReport(False, login, token_class, scopes, {}, "insufficient-scope")

    capabilities: dict[str, bool] = {}
    missing: Optional[str] = None
    for name, target in _capability_targets(config):
        try:
            granted = bool(probe.capability(name, target, token))
        except Exception:  # an unconfirmable capability is a missing capability
            granted = False
        capabilities[name] = granted
        if not granted and missing is None:
            missing = name
    if missing is not None:
        return AuthReport(False, login, token_class, scopes, capabilities, f"capability-missing:{missing}")

    return AuthReport(True, login, token_class, scopes, capabilities, None)


__all__ = [
    "AUTH_MODES",
    "LOGIN_ENV_VAR",
    "REQUIRED_SCOPES",
    "TOKEN_ENV_VAR",
    "AuthReport",
    "GhProbe",
    "classify_token",
    "token_class_explanation",
    "verify_github_identity",
]
