#!/usr/bin/env python3
"""Agent Native is a projection. It is never a control surface.

The cockpit renders the board and the design work. It is a window. The moment a
window can also move a card, two things own the lifecycle and neither can be
trusted about it: a status set from the cockpit has no compare-before-mutate
record, no QA linkage, and no merge evidence behind it — but on the board it
looks exactly like one that does. The same argument rules out a second
completion ledger: two ledgers disagree eventually, and then "is this done?" has
two answers.

`evaluate_agent_native_payload` checks a cockpit payload for every way a
projection turns into a control surface: repository command execution,
credential fields, branch changes, pull-request creation, Project mutation
verbs, and a second lifecycle or completion ledger.

**Static checks are necessary and not sufficient.** A payload can declare
anything. `probe_deployed_cockpit` checks the deployed thing — and it does so
with **synthetic non-resolving targets**: a Project item ID that does not exist
and a repository command that does not exist. Handing it a real item ID or a
real command raises, because proving "mutation is unavailable" must never be
done by attempting a mutation that could succeed. A probe that *fails* is the
positive evidence; a probe that is accepted is the violation.

`scan_stale_projects_guidance` keeps one specific piece of documentation rot
from coming back: **GitHub Apps cannot access personal (user-owned) Projects v2
at all.** Any text that tells an operator to install an App or mint a
fine-grained PAT for a personal board sends them down a path that cannot work,
and the failure looks like a permissions problem rather than an impossibility.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

try:  # normal package import
    from . import EXIT_CONFIG
    from .publication import render_payload, sanitize_and_validate_publication
except ImportError:  # executed as a plain file path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFIG
    from super_board_runtime.publication import (
        render_payload,
        sanitize_and_validate_publication,
    )

#: The only mode a Superboard cockpit payload may declare.
AGENT_NATIVE_MODE = "read-only-projection"

#: The deployment setting that keeps production code execution unavailable.
CODE_EXECUTION_SETTING = "AGENT_PROD_CODE_EXECUTION"

#: Capabilities that must be false for PolySimulator.
DISABLED_CAPABILITIES: tuple[str, ...] = ("plan", "analytics", "clips")

#: The seven things the deployed cockpit must NOT have. Stated as negatives on
#: purpose: "it only projects" is a claim, "it holds no write token" is checkable.
NEGATIVE_CAPABILITIES: tuple[str, ...] = (
    "no-project-write-credential",
    "no-github-write-token",
    "no-docker-socket",
    "no-runner-filesystem-mount",
    "no-repository-checkout",
    "no-trusted-shell",
    "no-second-completion-ledger",
)

#: Where the deployed evidence is recorded.
DEPLOYED_EVIDENCE_DOCUMENT = "docs/architecture/AGENT-NATIVE-DEPLOYED-EVIDENCE.md"

#: Targets that cannot resolve anywhere, ever. The probe accepts nothing else.
SYNTHETIC_PROJECT_ITEM_ID = "PVTI_SYNTHETIC_TARGET_THAT_DOES_NOT_RESOLVE"
SYNTHETIC_REPOSITORY_COMMAND = "superboard-synthetic-command-that-does-not-exist"

#: Mutation verbs, matched case-insensitively against every key and string value
#: in the payload. The patterns are written as character classes rather than
#: literals so this module does not itself contain a string the tree-wide
#: merge-prohibition scanner would have to allowlist.
_MUTATION_VERB_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"addProjectV2ItemById|updateProjectV2ItemFieldValue|deleteProjectV2Item", re.I),
    re.compile(r"projectv2[a-z0-9_]*mutation", re.I),
    re.compile(r"create[ _-]?branch|delete[ _-]?branch|force[ _-]?push", re.I),
    re.compile(r"create[ _-]?pull[ _-]?request|open[ _-]?pull[ _-]?request", re.I),
    re.compile(r"merge[ _-]?pull[ _-]?request|enable[ _-]?pull[ _-]?request[ _-]?auto", re.I),
    re.compile(r"auto[ _-]?merge", re.I),
    re.compile(r"\bclose[ _-]?issue|\breopen[ _-]?issue|set[ _-]?status", re.I),
)

#: Keys whose presence means the payload carries credential material, whatever
#: the value is. An empty credential field today is a filled one tomorrow.
_CREDENTIAL_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|apikey|private[_-]?key|credential|cookie|session)",
    re.I,
)

#: Keys that would make the cockpit an execution surface.
_EXECUTION_KEY_RE = re.compile(
    r"(execution|exec|shell|command|docker|socket|mount|checkout|worktree|runner_filesystem)",
    re.I,
)

#: Keys that would make the cockpit a second ledger.
_LEDGER_KEY_RE = re.compile(r"(ledger|completion_store|lifecycle_store)", re.I)

#: Subtrees checked above by meaning; walking them again by key shape would flag
#: the settings that prove the payload safe.
_CHECKED_SUBTREES = frozenset(
    {"capabilities", "environment", "output", "negative_capabilities"}
)

#: The two empty declaration slots. Empty is the point; content is the violation.
_DECLARATION_SLOTS = frozenset({"credentials", "ledgers"})

#: The only two files allowed to contain the wrong Projects v2 claim, because
#: one detects it and the other seeds it in a fixture. Named explicitly — a path
#: heuristic like "skip anything under tests/" is how a real stale runbook hides.
STALE_GUIDANCE_ALLOWLIST: tuple[str, ...] = (
    "scripts/super_board_runtime/agent_native.py",
    "tests/test_agent_native_safety.py",
)

# Documentation rot: an App or fine-grained PAT presented as a way to reach a
# personal Projects v2 board.
_APP_CLAIM_RE = re.compile(r"(github app|app installation token|fine[- ]grained pat)", re.I)
_PERSONAL_PROJECT_RE = re.compile(r"(projects? v2|personal project|user-owned project)", re.I)
_NEGATION_RE = re.compile(
    r"(cannot|can not|can't|never|not an option|refus|reject|unsupported|does not|doesn't|"
    r"is not|are not|no github app|impossible)",
    re.I,
)
_SCANNED_DOC_SUFFIXES = frozenset(
    {".md", ".py", ".sh", ".yml", ".yaml", ".json", ".js", ".mjs", ".ts"}
)
_SKIPPED_DIRECTORIES = frozenset({".git", "node_modules", "__pycache__", ".venv"})


class AgentNativeError(ValueError):
    """A cockpit probe or payload was used in a way that is not permitted."""

    exit_code = EXIT_CONFIG

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class AgentNativeSafetyReport:
    safe: bool
    violations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


# ───────────────────────────── payload evaluation ─────────────────────────────


def _walk(node: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    """Every (key-path, value) pair in a nested payload."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            yield here, value
            yield from _walk(value, here)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            here = f"{path}[{index}]"
            yield here, value
            yield from _walk(value, here)


def _has_content(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (str, bytes)):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def evaluate_agent_native_payload(payload: Mapping[str, object]) -> AgentNativeSafetyReport:
    """Check a cockpit payload for every way a projection becomes a control surface."""
    if not isinstance(payload, Mapping):
        return AgentNativeSafetyReport(False, ("payload-unreadable",))

    violations: list[str] = []

    if payload.get("mode") != AGENT_NATIVE_MODE:
        violations.append("mode-not-read-only")

    capabilities = payload.get("capabilities")
    capabilities = capabilities if isinstance(capabilities, Mapping) else {}
    for name in DISABLED_CAPABILITIES:
        if capabilities.get(name):
            violations.append(f"capability-not-read-only:{name}")

    environment = payload.get("environment")
    environment = environment if isinstance(environment, Mapping) else {}
    if str(environment.get(CODE_EXECUTION_SETTING, "")).strip().casefold() != "off":
        violations.append("code-execution-not-off")

    output = payload.get("output")
    output = output if isinstance(output, Mapping) else {}
    if output.get("source") != "read-only-snapshot":
        violations.append("output-not-read-only")
    if output.get("sanitizer") != "sanitize_and_validate_publication":
        violations.append("output-not-sanitized")

    declared_negatives = payload.get("negative_capabilities")
    declared_negatives = set(declared_negatives) if isinstance(declared_negatives, (list, tuple)) else set()
    for capability in NEGATIVE_CAPABILITIES:
        if capability not in declared_negatives:
            violations.append(f"negative-capability-undeclared:{capability}")

    for key_path, value in _walk(payload):
        root = key_path.split(".")[0].split("[")[0]
        if root in _CHECKED_SUBTREES:
            # Already checked above, by meaning rather than by key shape. Walking
            # them again would flag `AGENT_PROD_CODE_EXECUTION` as an execution
            # capability and `no-second-completion-ledger` as a second ledger.
            continue
        leaf = key_path.split(".")[-1].split("[")[0]
        if _LEDGER_KEY_RE.search(leaf):
            if _has_content(value):
                violations.append("second-ledger-declared")
            continue
        if _CREDENTIAL_KEY_RE.search(leaf):
            # `credentials: []` is the empty declaration slot and is the point.
            # A credential-shaped key ANYWHERE else is a violation even when it
            # is empty: an empty credential field today is a filled one tomorrow.
            if key_path not in _DECLARATION_SLOTS or _has_content(value):
                violations.append("credential-declared")
            continue
        if _EXECUTION_KEY_RE.search(leaf) and _has_content(value):
            violations.append("repository-execution-declared")
            continue
        if isinstance(value, str):
            for pattern in _MUTATION_VERB_PATTERNS:
                if pattern.search(value):
                    violations.append("mutation-verb-declared")
                    break

    ordered = tuple(dict.fromkeys(violations))
    return AgentNativeSafetyReport(not ordered, ordered)


# ───────────────────────────── cockpit output ─────────────────────────────


def render_cockpit_projection(
    records: Sequence[Mapping[str, Any]], environment: Mapping[str, str]
) -> str:
    """Render read-only snapshot records for the cockpit, through the one sanitizer.

    The cockpit displays board text. Board text is written by humans and by
    tooling, and either can paste a credential into it. So the projection goes
    through exactly the same publication boundary as anything bound for GitHub.
    """
    lines: list[str] = []
    for record in records or ():
        fields = ", ".join(f"{key}: {value}" for key, value in sorted(dict(record).items()))
        lines.append(fields)
    rendered = render_payload(["\n".join(lines)])
    return sanitize_and_validate_publication(
        rendered, environment or {}, surface="project-text-field"
    ).text


# ───────────────────────────── deployed evidence ─────────────────────────────


def probe_deployed_cockpit(
    project_item_id: str,
    repository_command: str,
    *,
    mutate_probe: Callable[[str], Any],
    execute_probe: Callable[[str], Any],
) -> AgentNativeSafetyReport:
    """Prove, against the deployed cockpit, that two capabilities are unavailable.

    Only the synthetic targets are accepted. A real Project item ID or a real
    repository command is refused before either probe is called — the point is
    to demonstrate an absent capability, not to find out the hard way that it is
    present.

    A probe that raises is the evidence. A probe that returns anything at all
    means the cockpit accepted the request, which is the violation.
    """
    if project_item_id != SYNTHETIC_PROJECT_ITEM_ID:
        raise AgentNativeError(
            "probe-target-not-synthetic",
            "the Project mutation probe accepts only the synthetic non-resolving item ID; "
            "a real item ID would risk mutating a real card to prove it cannot be mutated",
        )
    if repository_command != SYNTHETIC_REPOSITORY_COMMAND:
        raise AgentNativeError(
            "probe-target-not-synthetic",
            "the execution probe accepts only the synthetic non-existent command; a real "
            "command would risk executing it to prove it cannot be executed",
        )

    violations: list[str] = []
    try:
        mutate_probe(project_item_id)
    except Exception:
        pass  # refused, which is the evidence we wanted
    else:
        violations.append("project-mutation-available")

    try:
        execute_probe(repository_command)
    except Exception:
        pass
    else:
        violations.append("repository-execution-available")

    return AgentNativeSafetyReport(not violations, tuple(violations))


# ───────────────────────────── stale guidance ─────────────────────────────


def scan_stale_projects_guidance(
    root: Path, *, allowlist: Optional[Sequence[str]] = None
) -> list[dict[str, Any]]:
    """Find text that presents an App or a fine-grained PAT as a way to reach a
    personal (user-owned) Projects v2 board. It is not one, and never was."""
    root = Path(root)
    excluded = STALE_GUIDANCE_ALLOWLIST if allowlist is None else tuple(allowlist)
    findings: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIPPED_DIRECTORIES for part in path.parts):
            continue
        if path.suffix.lower() not in _SCANNED_DOC_SUFFIXES:
            continue
        if path.relative_to(root).as_posix() in excluded:
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, text in enumerate(lines, start=1):
            if not _APP_CLAIM_RE.search(text) or not _PERSONAL_PROJECT_RE.search(text):
                continue
            if _NEGATION_RE.search(text):
                continue
            findings.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "line": number,
                    "text": text.strip()[:160],
                }
            )
    return findings


__all__ = [
    "AGENT_NATIVE_MODE",
    "CODE_EXECUTION_SETTING",
    "DEPLOYED_EVIDENCE_DOCUMENT",
    "DISABLED_CAPABILITIES",
    "NEGATIVE_CAPABILITIES",
    "SYNTHETIC_PROJECT_ITEM_ID",
    "STALE_GUIDANCE_ALLOWLIST",
    "SYNTHETIC_REPOSITORY_COMMAND",
    "AgentNativeError",
    "AgentNativeSafetyReport",
    "evaluate_agent_native_payload",
    "probe_deployed_cockpit",
    "render_cockpit_projection",
    "scan_stale_projects_guidance",
]
