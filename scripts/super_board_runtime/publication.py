#!/usr/bin/env python3
"""Central sanitization, secret detection, and the publication boundary.

There is exactly ONE sanitizer, and it sits immediately before the GitHub write.
Not one per surface, not one per script — one. A second sanitizer is a second
place to forget a category, and the categories that get forgotten are the ones
that leak.

The order is strict and not negotiable:

  1. **Render** the complete payload. Templates are assembled first, because a
     secret can be split across two fragments (`"gh"` + `"p_…"`) and neither
     fragment matches anything on its own.
  2. **Redact** known values (the caller's environment) and recognized secret
     patterns from that complete rendered text.
  3. **Scan the complete redacted payload again.** Redaction is best-effort;
     detection is the gate.
  4. **Fail closed** — raise `UnsafePublication` (exit 78) if anything sensitive
     survived. No partial write happens: the writer is never called.
  5. Only then **write**.

Why step 3 can genuinely find something: a credential-named environment value
shorter than `MIN_REDACTABLE_ENV_VALUE_LEN` is not substring-redacted, because
replacing a six-character string everywhere it appears mangles unrelated prose
and produces evidence nobody can read. Such a value is still a secret, so it is
detected and the publication is refused rather than published or silently
corrupted.

Every GitHub-bound payload goes through here — see `PUBLICATION_SURFACES`. A
surface that is not in that tuple cannot be published at all.

Failure reports name the **category and the offset**, never the matched value.
A leak report that quotes the leak is a second leak.

CLI: `scripts/super-board-publish.py` (the only supported publication entry
point). Exit 0 success, 64 invalid invocation, 65 invalid input or unknown
surface, 78 unsafe evidence rejected.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

try:  # normal package import
    from . import EXIT_CONFIG, EXIT_UNSAFE
except ImportError:  # executed as a plain file path
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFIG, EXIT_UNSAFE

#: Every GitHub-bound payload the pipeline can produce. Publishing to anything
#: not named here is refused — an unlisted surface is an unaudited surface.
PUBLICATION_SURFACES: tuple[str, ...] = (
    "issue-create",
    "issue-edit",
    "pull-request-body",
    "pull-request-comment",
    "review-summary",
    "qa-comment",
    "check-output",
    "commit-status",
    "closure-comment",
    "bug-report",
    "release-text",
    "project-text-field",
    "dispatch-manifest",
    "reconciliation-manifest",
)

#: An artifact must declare what it is. An unclassified blob is rejected: we
#: cannot scan bytes we cannot interpret, so we do not publish them.
ARTIFACT_CLASSIFICATIONS: tuple[str, ...] = (
    "text/markdown",
    "text/plain",
    "application/json",
    "image/png",
    "image/jpeg",
    "image/gif",
)

#: Environment values shorter than this are not substring-redacted — replacing a
#: short string everywhere would mangle unrelated prose. They are still
#: detected, and their presence fails the publication closed.
MIN_REDACTABLE_ENV_VALUE_LEN = 8

#: Environment variable NAMES whose values are treated as credential material.
_CREDENTIAL_NAME_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|API[_-]?KEY|APIKEY|PRIVATE[_-]?KEY|CREDENTIAL|COOKIE|SESSION|DSN|DATABASE_URL)",
    re.IGNORECASE,
)


def _placeholder(category: str) -> str:
    return f"[redacted:{category}]"


# Ordered: the widest structural matches (fenced blocks, key blocks) run first so
# a secret inside them is removed with its container rather than piecemeal.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Raw command logs and structured tool output must be classified before
    # publication; a fenced block that declares itself as one is removed whole.
    ("raw-log", re.compile(r"```(?:log|logs|raw-log|console|shell-session|env)\b.*?```", re.S)),
    ("tool-output", re.compile(r"```(?:tool-output|tool_result|mcp|structured-output)\b.*?```", re.S)),
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
        ),
    ),
    # Headers reach this boundary far more often as JSON or YAML than as a bare
    # header line — captured requests, `gh api` output, and structured tool
    # results are all mappings — so every shape of the same header is matched,
    # not just the one a terminal happens to print.
    ("authorization-header", re.compile(r"(?im)^[^\S\n]*authorization[^\S\n]*:[^\n]*$")),
    (
        "authorization-header",
        re.compile(r"""(?i)["']?authorization["']?[^\S\n]*[:=][^\S\n]*["']?[^\s,}\]"']+"""),
    ),
    ("cookie", re.compile(r"(?im)^[^\S\n]*(?:set-)?cookie[^\S\n]*:[^\n]*$")),
    (
        "cookie",
        re.compile(r"""(?i)["']?(?:set-)?cookie["']?[^\S\n]*[:=][^\S\n]*["']?[^\s,}\]"']+"""),
    ),
    # `curl -b '<jar>'` / `--cookie <jar>`: a cookie with no `Cookie` word near
    # it. The `=` is required so an unrelated `-b` flag is not mangled.
    (
        "cookie",
        re.compile(r"""(?i)(?:^|[\s'"])(?:--cookie|-b)[= \t]+(?:"[^"]*=[^"]*"|'[^']*=[^']*'|\S*=\S*)"""),
    ),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("github-token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{16,}\b")),
    ("dokploy-key", re.compile(r"\bpolysim_mcp[A-Za-z0-9_-]{8,}\b")),
    ("dokploy-key", re.compile(r"\bdokploy_[A-Za-z0-9_-]{16,}\b")),
    ("cloud-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("cloud-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("cloud-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("cloud-key", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    # Any auth scheme carrying a credential, wherever it appears — `Basic` and
    # `token` are as common as `Bearer` and were not matched at all. Runs after
    # the provider patterns so a recognizable token keeps its own category.
    (
        "authorization-header",
        re.compile(
            r"(?i)(?<![-\w])(?:Bearer|Basic|Digest|OAuth|Token)[ \t]+[A-Za-z0-9._~+/=\-]{16,}"
        ),
    ),
    # Credentials embedded in a URL: scheme://user:password@host
    ("credentialed-url", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/@:]+:[^\s/@]+@\S+")),
    # Credential-bearing command arguments, both `--flag value` and `--flag=value`.
    (
        "credential-argument",
        re.compile(
            r"(?i)--(?:token|password|passwd|api[-_]?key|secret|private[-_]?key|credential)"
            r"(?:[= \t]+)\S+"
        ),
    ),
)

#: Any control character other than tab, newline, and carriage return means the
#: payload carries bytes we cannot interpret as text.
_BINARY_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class PublicationError(ValueError):
    """Invalid publication input or an unknown surface. Maps to exit code 65."""

    exit_code = EXIT_CONFIG

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class SecretFinding:
    """A detection. Carries the category and the offset — never the value."""

    category: str
    surface: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


@dataclass(frozen=True)
class RedactionRecord:
    category: str
    surface: str
    start: int
    end: int

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


@dataclass(frozen=True)
class SanitizedPayload:
    text: str
    surface: str
    redactions: tuple[RedactionRecord, ...]
    safe: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "redactions": [r.to_dict() for r in self.redactions],
            "safe": self.safe,
            "surface": self.surface,
            "text": self.text,
        }


class UnsafePublication(Exception):
    """Sensitive material survived redaction. Exit code 78; nothing was written."""

    exit_code = EXIT_UNSAFE

    def __init__(
        self, reason: str, message: str, findings: Sequence[SecretFinding] = ()
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.findings = tuple(findings)


def render_payload(fragments: Iterable[str]) -> str:
    """Assemble the complete payload BEFORE any scanning happens.

    Scanning fragments instead of the render is how split secrets get through:
    `"gh"` and `"p_…"` are both innocuous, their concatenation is not.
    """
    return "".join("" if fragment is None else str(fragment) for fragment in fragments)


def _credential_env_values(environment: Mapping[str, str]) -> list[tuple[str, str]]:
    """(name, value) pairs whose values are treated as credential material."""
    pairs: list[tuple[str, str]] = []
    for name, value in (environment or {}).items():
        if not isinstance(name, str) or not isinstance(value, str) or not value.strip():
            continue
        if _CREDENTIAL_NAME_RE.search(name):
            pairs.append((name, value))
    # Longest first, so a value that contains another is redacted whole.
    pairs.sort(key=lambda pair: len(pair[1]), reverse=True)
    return pairs


def _redact(text: str, environment: Mapping[str, str], surface: str) -> tuple[str, list[RedactionRecord]]:
    records: list[RedactionRecord] = []
    result = text

    # Known values first: an exact value we were handed beats any heuristic.
    for _name, value in _credential_env_values(environment):
        if len(value) < MIN_REDACTABLE_ENV_VALUE_LEN:
            continue
        start = result.find(value)
        while start != -1:
            records.append(RedactionRecord("env-value", surface, start, start + len(value)))
            result = result[:start] + _placeholder("env-value") + result[start + len(value) :]
            start = result.find(value)

    for category, pattern in _PATTERNS:
        while True:
            match = pattern.search(result)
            if match is None or match.start() == match.end():
                break
            records.append(RedactionRecord(category, surface, match.start(), match.end()))
            result = result[: match.start()] + _placeholder(category) + result[match.end() :]

    return result, records


def scan_for_secrets(
    text: str, environment: Mapping[str, str], surface: str
) -> list[SecretFinding]:
    """Detect sensitive material. Broader than the redactor, by design.

    Detection includes credential-named environment values the redactor
    deliberately refuses to substring-replace, so "too short to redact safely"
    resolves to *refuse*, never to *publish anyway*.
    """
    findings: list[SecretFinding] = []
    for _name, value in _credential_env_values(environment):
        start = text.find(value)
        while start != -1:
            findings.append(SecretFinding("env-value", surface, start, start + len(value)))
            start = text.find(value, start + 1)
    for category, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            if match.start() == match.end():
                continue
            findings.append(SecretFinding(category, surface, match.start(), match.end()))
    return findings


def _require_text_only(text: str, surface: str) -> None:
    match = _BINARY_RE.search(text)
    if match is not None:
        raise UnsafePublication(
            "binary-artifact-unclassified",
            f"the {surface} payload carries uninterpretable bytes at offset {match.start()}; "
            "classify the artifact before publishing it",
            (SecretFinding("binary-content", surface, match.start(), match.end()),),
        )


def _require_classified_artifacts(
    artifacts: Sequence[Mapping[str, Any]], surface: str
) -> None:
    for index, artifact in enumerate(artifacts or ()):
        classification = (artifact or {}).get("classification")
        if classification not in ARTIFACT_CLASSIFICATIONS:
            raise UnsafePublication(
                "binary-artifact-unclassified",
                f"artifact #{index} on the {surface} payload declares no recognized "
                f"classification; recognized: {', '.join(ARTIFACT_CLASSIFICATIONS)}",
                (SecretFinding("binary-content", surface, index, index),),
            )


def sanitize_and_validate_publication(
    rendered: str,
    environment: Mapping[str, str],
    *,
    surface: str,
    artifacts: Sequence[Mapping[str, Any]] = (),
) -> SanitizedPayload:
    """Redact, rescan, and fail closed. The only sanitizer in the runtime.

    ``rendered`` must already be the COMPLETE payload — see `render_payload`.
    ``surface`` is keyword-only and required because every record this produces
    is attributed to a surface.
    """
    if surface not in PUBLICATION_SURFACES:
        raise PublicationError(
            "publication-surface-unknown",
            f"{surface!r} is not a publication surface; recognized: "
            + ", ".join(PUBLICATION_SURFACES),
        )
    if not isinstance(rendered, str):
        raise PublicationError("publication-payload-invalid", "the rendered payload must be text")

    _require_classified_artifacts(artifacts, surface)
    _require_text_only(rendered, surface)

    text, records = _redact(rendered, environment or {}, surface)

    # The gate: scan the COMPLETE redacted payload again.
    survivors = scan_for_secrets(text, environment or {}, surface)
    if survivors:
        categories = sorted({finding.category for finding in survivors})
        raise UnsafePublication(
            "secret-survived-redaction",
            f"the {surface} payload still matches "
            f"{len(survivors)} sensitive pattern(s) after redaction "
            f"(categories: {', '.join(categories)}); refusing to publish. "
            "No value is quoted here on purpose.",
            survivors,
        )
    _require_text_only(text, surface)

    return SanitizedPayload(text=text, surface=surface, redactions=tuple(records), safe=True)


def publish(
    surface: str,
    rendered: str,
    environment: Mapping[str, str],
    *,
    writer: Callable[[str, str], Any],
    artifacts: Sequence[Mapping[str, Any]] = (),
    dry_run: bool = False,
) -> dict[str, Any]:
    """Sanitize, then write. The writer is reached only by a safe payload."""
    sanitized = sanitize_and_validate_publication(
        rendered, environment, surface=surface, artifacts=artifacts
    )
    if dry_run:
        return {
            "dry_run": True,
            "github_writes": 0,
            "published": False,
            "redactions": [r.to_dict() for r in sanitized.redactions],
            "safe": True,
            "surface": surface,
            "text": sanitized.text,
        }
    response = writer(surface, sanitized.text)
    url = response.get("url") if isinstance(response, Mapping) else None
    return {
        "dry_run": False,
        "github_writes": 1,
        "published": True,
        "redactions": [r.to_dict() for r in sanitized.redactions],
        "safe": True,
        "surface": surface,
        "text": sanitized.text,
        "url": url,
    }


__all__ = [
    "ARTIFACT_CLASSIFICATIONS",
    "MIN_REDACTABLE_ENV_VALUE_LEN",
    "PUBLICATION_SURFACES",
    "PublicationError",
    "RedactionRecord",
    "SanitizedPayload",
    "SecretFinding",
    "UnsafePublication",
    "publish",
    "render_payload",
    "sanitize_and_validate_publication",
    "scan_for_secrets",
]
