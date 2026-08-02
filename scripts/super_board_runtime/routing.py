#!/usr/bin/env python3
"""Branch-route declaration and redundant-label validation.

A branch route is something an issue **says**, exactly once, in a normalized
declaration:

    Branch route: staging
    Branch route: staging-frankfurt

Nothing else routes anything. Not a Test Area, not the word "Frankfurt" in the
prose, not a label on its own, and not whatever branch happens to be checked
out. Every one of those has been mistaken for a route before, and the failure
mode is silent: work lands on the wrong branch and nobody notices until a deploy
picks it up.

Fail-closed reason codes:

  route-declaration-missing     no declaration at all
  route-declaration-unknown     `default`, `designstaging`, `main`, anything else
  route-declaration-duplicate   two declarations, even two identical ones
  route-label-conflict          the declaration and the labels disagree

The redundancy rule is deliberate and asymmetric:

  * a `staging` declaration must **not** carry `branch:staging-frankfurt`;
  * a `staging-frankfurt` declaration **must** carry it.

The Frankfurt route is the dangerous one — it is a separate deploying branch —
so it is the one that must be stated twice, in the body and on the card, before
anything is created. `designstaging` is never a dispatch route for any card,
design-labelled or not.

Every invalid case fails **before** a branch is created: `create_branch_for_route`
raises rather than calling the creator.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

try:  # normal package import
    from . import EXIT_CONFIG
    from .config import NormalizedConfig
except ImportError:  # executed as a plain file path
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFIG
    from super_board_runtime.config import NormalizedConfig

#: The only two accepted declarations.
ROUTE_DECLARATIONS: tuple[str, ...] = ("staging", "staging-frankfurt")

#: The label that must accompany — and only accompany — the Frankfurt route.
FRANKFURT_LABEL = "branch:staging-frankfurt"

#: Branches that are never dispatch routes, however they are spelled.
NON_DISPATCH_BRANCHES: tuple[str, ...] = ("designstaging", "main", "master", "default")

#: One declaration per line, key matched case-insensitively; the value is
#: normalized by trimming and case-folding before it is compared.
_DECLARATION_RE = re.compile(r"(?im)^[^\S\n]*branch[ \t_-]*route[^\S\n]*:[^\S\n]*(\S+)[^\S\n]*$")


class RoutingError(ValueError):
    """An invalid or absent branch-route declaration. Maps to exit code 65."""

    exit_code = EXIT_CONFIG

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class BranchRoute:
    declaration: Optional[str]
    base_branch: Optional[str]
    required_label: Optional[str]
    valid: bool
    reason_code: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


def _invalid(reason: str, declaration: Optional[str] = None) -> BranchRoute:
    return BranchRoute(
        declaration=declaration,
        base_branch=None,
        required_label=None,
        valid=False,
        reason_code=reason,
    )


def _route_labels(config: NormalizedConfig, labels: tuple[str, ...]) -> set[str]:
    """The configured route labels this card actually carries."""
    folded = {label.strip().casefold() for label in labels}
    return {key for key in config.branch_routes if key.strip().casefold() in folded}


def resolve_branch_route(issue: Any, config: NormalizedConfig) -> BranchRoute:
    """Resolve the declared route, or explain exactly why there isn't one."""
    body = getattr(issue, "body", None)
    matches = _DECLARATION_RE.findall(body) if isinstance(body, str) else []

    if not matches:
        return _invalid("route-declaration-missing")
    if len(matches) > 1:
        # Two declarations means two intentions. Picking one is guessing.
        return _invalid("route-declaration-duplicate")

    declaration = matches[0].strip().casefold()
    if declaration in {b.casefold() for b in NON_DISPATCH_BRANCHES}:
        return _invalid("route-declaration-unknown", declaration)
    if declaration not in ROUTE_DECLARATIONS:
        return _invalid("route-declaration-unknown", declaration)

    labels = tuple(getattr(issue, "labels", ()) or ())
    carried = _route_labels(config, labels)
    branches = {config.branch_routes[key] for key in carried}
    if len(branches) > 1:
        # Two route labels naming different branches is a routing error, not a
        # tie to be broken by the declaration.
        return _invalid("route-label-conflict", declaration)

    has_frankfurt_label = FRANKFURT_LABEL.casefold() in {label.strip().casefold() for label in labels}
    if declaration == "staging-frankfurt" and not has_frankfurt_label:
        return _invalid("route-label-conflict", declaration)
    if declaration == "staging" and has_frankfurt_label:
        return _invalid("route-label-conflict", declaration)

    return BranchRoute(
        declaration=declaration,
        base_branch=declaration,
        required_label=FRANKFURT_LABEL if declaration == "staging-frankfurt" else None,
        valid=True,
        reason_code=None,
    )


def verify_pull_request_base(route: BranchRoute, pull_request_base: Any) -> tuple[bool, Optional[str]]:
    """Confirm an existing pull request still targets its declared route.

    Run before QA and before Review: a base branch edited after the pull request
    was opened silently re-targets the whole change.
    """
    if not route.valid or route.base_branch is None:
        return False, route.reason_code or "route-declaration-missing"
    if not isinstance(pull_request_base, str) or not pull_request_base.strip():
        return False, "route-base-branch-unreadable"
    if pull_request_base.strip() != route.base_branch:
        return False, "route-base-branch-drift"
    return True, None


def create_branch_for_route(
    issue: Any,
    config: NormalizedConfig,
    *,
    creator: Callable[[str, str], Any],
    branch: Optional[str] = None,
) -> Any:
    """Create the issue branch — only after the route is proven valid.

    The route check happens BEFORE `creator` is called, so an ineligible card
    never leaves a half-created branch behind to be cleaned up later.
    """
    route = resolve_branch_route(issue, config)
    if not route.valid:
        raise RoutingError(
            route.reason_code or "route-declaration-missing",
            "the issue does not carry exactly one valid `Branch route:` declaration; "
            "refusing to create a branch",
        )
    name = branch or f"issue-{getattr(issue, 'number', 'unknown')}"
    return creator(name, route.base_branch)


__all__ = [
    "FRANKFURT_LABEL",
    "NON_DISPATCH_BRANCHES",
    "ROUTE_DECLARATIONS",
    "BranchRoute",
    "RoutingError",
    "create_branch_for_route",
    "resolve_branch_route",
    "verify_pull_request_base",
]
