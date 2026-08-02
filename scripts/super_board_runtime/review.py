#!/usr/bin/env python3
"""Merge-prohibition scanner and the human Review handoff.

**The runtime never merges.** It creates branches, pushes commits, opens and
updates pull requests, runs QA and local review, publishes sanitized evidence,
and moves a successful card to Review — and then it stops. A human rebase-merges.

That rule only holds if it is *enforced*, so `scan_merge_prohibitions` is a
release gate rather than a convention. It source-scans every executable runtime,
workflow, skill, and reviewer path for all eight ways a merge can happen, and
ANY active occurrence fails the gate. The eight are not arbitrary: each one is a
distinct path that has, at some point, merged something nobody approved.

  1. cli-merge-subcommand      `gh pr merge`
  2. rest-merge-endpoint       `/pulls/<n>/merge`, `/merges`
  3. graphql-merge-mutation    `mergePullRequest`, `enablePullRequestAutoMerge`
  4. mcp-merge-tool            `merge_pull_request`
  5. auto-merge-enablement     `auto-merge` / `auto_merge`
  6. squash-or-merge-commit    `merge_method: squash|merge`, `--squash`
  7. runtime-issue-closure     closing the issue INSTEAD of merging it
  8. runtime-done-transition   writing the literal `Done` status

Mechanisms 7 and 8 are scoped to dispatcher, reviewer, QA, and workflow paths,
because closing an issue and writing `Done` are legitimate elsewhere — for the
closure normalizer, which is the only actor allowed to produce `Done`, and only
after a confirmed external merge.

Exclusions come from an explicit allowlist FILE at the repository root, listing
paths one per line. Never a path heuristic: "skip anything under docs/" is
exactly how a real merge path hides in a file named `docs/deploy-helper.sh`.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

try:  # normal package import
    from . import EXIT_CONFIG
    from .config import NormalizedConfig
except ImportError:  # executed as a plain file path
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from super_board_runtime import EXIT_CONFIG
    from super_board_runtime.config import NormalizedConfig

#: The explicit exclusion list. A FILE, deliberately — see the module docstring.
ALLOWLIST_FILENAME = "merge-scan-allowlist.txt"

#: The only actor permitted to write the `Done` status, and only after a
#: confirmed external merge or closure.
DONE_WRITER = "closure-normalizer"

#: Configuration the runtime requires before it will run at all.
REQUIRED_MERGE_CONFIG: Mapping[str, Any] = {
    "human_approves_merge": True,
    "merge_method": "rebase",
}

#: Repository settings the board contract requires. Squash destroys the TDD
#: breadcrumb trail; a merge commit hides it. Rebase keeps every commit.
REQUIRED_REPOSITORY_SETTINGS: Mapping[str, bool] = {
    "allow_merge_commit": False,
    "allow_rebase_merge": True,
    "allow_squash_merge": False,
}

_MECHANISM_PATTERNS: tuple[tuple[str, re.Pattern[str], bool], ...] = (
    ("cli-merge-subcommand", re.compile(r"gh\s+pr\s+merge\b"), False),
    ("rest-merge-endpoint", re.compile(r"/pulls/[^/\s]+/merge|/merges\b"), False),
    (
        "graphql-merge-mutation",
        re.compile(r"mergePullRequest|enablePullRequestAutoMerge"),
        False,
    ),
    ("mcp-merge-tool", re.compile(r"merge_pull_request"), False),
    ("auto-merge-enablement", re.compile(r"auto[-_]merge"), False),
    (
        "squash-or-merge-commit",
        re.compile(r"""merge_method\s*[:=]\s*["']?(squash|merge)|--squash\b"""),
        False,
    ),
    # Scoped to dispatcher / reviewer paths — see the module docstring.
    ("runtime-issue-closure", re.compile(r"""state_reason|state\s*[:=]\s*["']closed"""), True),
    ("runtime-done-transition", re.compile(r"""[:=]\s*["']Done["']|\bstatus\s+Done\b"""), True),
)

#: The eight mechanisms, in scan order.
MERGE_MECHANISMS: tuple[str, ...] = tuple(name for name, _p, _s in _MECHANISM_PATTERNS)

#: Files whose path marks them as a dispatcher, reviewer, QA, or workflow path.
_SCOPED_PATH_RE = re.compile(
    r"(^|/)(super-board-run|super-board-wave|super-review|super-qa|super-build|review|qa|workflows?)"
    r"[^/]*$|(^|/)(workflows|skills/super-review|skills/super-qa|skills/super-build)/",
    re.IGNORECASE,
)

#: Only source that can actually run — or that instructs a model to run — is
#: scanned. A skill Markdown file IS an executable path: the model obeys it.
_SCANNED_SUFFIXES: frozenset[str] = frozenset(
    {".sh", ".bash", ".py", ".js", ".mjs", ".cjs", ".ts", ".yml", ".yaml", ".md", ".ps1"}
)

_ALWAYS_SKIPPED_DIRS: frozenset[str] = frozenset({".git", "node_modules", "__pycache__", ".venv"})

_RETIRED_STATUS_RE = re.compile(r"\bSkipped\b")


class MergeContractError(ValueError):
    """The human-merge contract is not satisfied. Maps to exit code 65."""

    exit_code = EXIT_CONFIG

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class MergeOccurrence:
    path: str
    line: int
    mechanism: str

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


@dataclass(frozen=True)
class MergeScanReport:
    occurrences: tuple[MergeOccurrence, ...]
    clean: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "occurrences": [o.to_dict() for o in self.occurrences],
        }


# ───────────────────────────── allowlist ─────────────────────────────


def load_allowlist(root: Path) -> tuple[str, ...]:
    """Read the explicit exclusion list. Missing file means exclude nothing."""
    path = Path(root) / ALLOWLIST_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ()
    entries: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return tuple(entries)


def _allowlisted(relative: str, allowlist: Sequence[str]) -> bool:
    for entry in allowlist:
        if entry.endswith("/"):
            if relative == entry.rstrip("/") or relative.startswith(entry):
                return True
        elif relative == entry:
            return True
    return False


def _scannable_files(root: Path) -> Iterable[Path]:
    for path in sorted(Path(root).rglob("*")):
        if not path.is_file():
            continue
        if any(part in _ALWAYS_SKIPPED_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in _SCANNED_SUFFIXES:
            yield path


# ───────────────────────────── the scan ─────────────────────────────


def scan_merge_prohibitions(
    root: Path, *, allowlist: Optional[Sequence[str]] = None
) -> MergeScanReport:
    """Source-scan a tree for every merge mechanism. Any hit fails the gate."""
    root = Path(root)
    entries = load_allowlist(root) if allowlist is None else tuple(allowlist)
    occurrences: list[MergeOccurrence] = []

    for path in _scannable_files(root):
        relative = path.relative_to(root).as_posix()
        if relative == ALLOWLIST_FILENAME or _allowlisted(relative, entries):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        scoped = bool(_SCOPED_PATH_RE.search(relative))
        for number, text in enumerate(lines, start=1):
            for mechanism, pattern, scope_limited in _MECHANISM_PATTERNS:
                if scope_limited and not scoped:
                    continue
                if pattern.search(text):
                    occurrences.append(MergeOccurrence(relative, number, mechanism))

    return MergeScanReport(tuple(occurrences), not occurrences)


def scan_retired_status(
    root: Path, *, allowlist: Optional[Sequence[str]] = None
) -> MergeScanReport:
    """`Skipped` is not a lifecycle status; it must be absent from active surfaces."""
    root = Path(root)
    entries = load_allowlist(root) if allowlist is None else tuple(allowlist)
    occurrences: list[MergeOccurrence] = []
    for path in _scannable_files(root):
        relative = path.relative_to(root).as_posix()
        if relative == ALLOWLIST_FILENAME or _allowlisted(relative, entries):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, text in enumerate(lines, start=1):
            if _RETIRED_STATUS_RE.search(text):
                occurrences.append(MergeOccurrence(relative, number, "retired-status-skipped"))
    return MergeScanReport(tuple(occurrences), not occurrences)


# ───────────────────────────── contracts ─────────────────────────────


def verify_human_merge_config(config: NormalizedConfig) -> None:
    """Refuse to run unless a human approves every merge, by rebase."""
    if config.human_approves_merge is not True:
        raise MergeContractError(
            "human-approves-merge-required",
            "human_approves_merge must be true: the runtime has no merge path and no "
            "substitute for one",
        )
    if config.merge_method != "rebase":
        raise MergeContractError(
            "merge-method-must-be-rebase",
            "merge_method must be 'rebase': squash collapses the TDD breadcrumb trail",
        )


def verify_repository_settings(settings: Mapping[str, Any]) -> None:
    """Pin the repository's merge buttons. An absent setting fails closed."""
    for key, required in REQUIRED_REPOSITORY_SETTINGS.items():
        if settings.get(key) is not required:
            raise MergeContractError(
                f"repository-setting-invalid:{key}",
                f"repository setting {key} must be {required!r}; an unreadable or "
                f"disagreeing value is refused rather than assumed",
            )


# ───────────────────────────── the handoff ─────────────────────────────


@dataclass(frozen=True)
class ReviewHandoff:
    issue_url: str
    pull_request_url: str
    tested_sha: str
    next_status: str
    merged: bool
    merge_ready: bool
    merge_method: str
    awaiting: str
    reason_code: Optional[str]
    completed_by: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


def review_handoff(
    *,
    issue_url: str,
    pull_request_url: str,
    tested_sha: str,
    merge_ready: bool,
    reason_code: Optional[str] = None,
) -> ReviewHandoff:
    """What a successful review produces: a record, not a merge.

    `next_status` is `Review` on every path. There is no branch of this function
    that returns `Done`, because producing `Done` is not the runtime's to do.
    """
    return ReviewHandoff(
        issue_url=issue_url,
        pull_request_url=pull_request_url,
        tested_sha=tested_sha,
        next_status="Review",
        merged=False,
        merge_ready=bool(merge_ready),
        merge_method="rebase",
        awaiting="human-rebase-merge",
        reason_code=reason_code,
        completed_by=None,
    )


def may_write_done(actor: str, *, merged_externally: bool) -> bool:
    """`Done` is produced by the closure normalizer, after a real merge. Only."""
    return actor == DONE_WRITER and bool(merged_externally)


# ───────────────────────────── the local Codex gate ─────────────────────────────
#
# Exactly ONE parallel maximum-level local Codex fleet per code pull request.
# Four lenses, run concurrently, every one on the newest model at maximum
# reasoning effort. Two models working together: Claude writes the code, Codex
# reviews it from four angles, Claude fixes every finding.
#
# CodeRabbit, Copilot, Greptile, and the GitHub `@codex` connector are NOT gates.
# The connector in particular has its own easily-exhausted review rate limit, and
# treating it as the gate produced a false "usage limit" while the task budget
# was at 99%. The binding gate is the local fleet below.

#: The newest Codex model. Not the `~/.codex/config.toml` default — that lags.
CODEX_MODEL = "gpt-5.5"

#: Maximum reasoning effort. Anything less is not the gate this contract names.
CODEX_REASONING_EFFORT = "high"

#: The four lenses, in launch order.
CODEX_LENSES: tuple[str, ...] = (
    "structured-diff",
    "correctness",
    "security",
    "performance-design-consistency",
)

#: Prompts for the three plain-`codex exec` lenses. `structured-diff` has none,
#: and must never be given one — see `build_lens_command`.
CODEX_LENS_PROMPTS: Mapping[str, str] = {
    "correctness": (
        "correctness lens: read the changed files under this worktree and hunt for "
        "logic errors, unhandled failure paths, contract drift between callers and "
        "callees, and anything that fails open where it must fail closed. "
        "Output one line per finding: file:line — severity (P1/P2/nit) — fix."
    ),
    "security": (
        "security lens: read the changed files under this worktree and hunt for "
        "credential exposure, unsanitized output crossing a publication boundary, "
        "injection, path traversal, unsafe deserialization, and missing "
        "authorization checks. "
        "Output one line per finding: file:line — severity (P1/P2/nit) — fix."
    ),
    "performance-design-consistency": (
        "performance-design-consistency lens: read the changed files under this "
        "worktree and hunt for avoidable work in hot paths, unbounded growth, and "
        "divergence from the conventions the surrounding code already establishes. "
        "Output one line per finding: file:line — severity (P1/P2/nit) — fix."
    ),
}

#: Suffixes whose complete diff makes a pull request documentation-only.
_DOCUMENTATION_SUFFIXES: frozenset[str] = frozenset({".md", ".mdx", ".rst", ".txt"})

#: `file:line — severity — fix`, with an em dash or a hyphen as the separator.
_FINDING_RE = re.compile(
    r"^\s*(?P<location>[^\s:]+:\d+)\s*[—-]\s*(?P<severity>P1|P2|P3|nit)\s*[—-]\s*(?P<summary>.+?)\s*$",
    re.IGNORECASE,
)


class CodexGateError(ValueError):
    """The Codex gate contract was violated. Maps to exit code 65."""

    exit_code = EXIT_CONFIG

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Finding:
    lens: str
    severity: str
    location: str
    summary: str
    resolved: bool = False
    evidence: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dict(asdict(self))


@dataclass(frozen=True)
class LensResult:
    name: str
    command: tuple[str, ...]
    model: str
    reasoning_effort: str
    exit_code: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "exit_code": self.exit_code,
            "model": self.model,
            "name": self.name,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True)
class CodexFleetReport:
    lenses: tuple[LensResult, ...]
    findings: tuple[Finding, ...]
    passed: bool
    reason_code: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "lenses": [lens.to_dict() for lens in self.lenses],
            "passed": self.passed,
            "reason_code": self.reason_code,
        }

    def published_summary(self) -> str:
        """The only thing that may be published: counts, locations, severities.

        Raw lens output stays on local disk, outside the Git tree. It is
        unbounded text produced by a model reading the whole worktree, which is
        exactly the shape of payload that carries a secret by accident.
        """
        if not self.lenses:
            return f"Codex gate: {self.reason_code or 'no lenses run'}."
        lines = [
            f"Codex fleet: {len(self.lenses)} lenses, model {CODEX_MODEL}, "
            f"reasoning effort {CODEX_REASONING_EFFORT}.",
            f"Result: {'passed' if self.passed else 'blocked'}"
            + (f" ({self.reason_code})" if self.reason_code else "")
            + ".",
        ]
        for finding in self.findings:
            state = "resolved" if finding.resolved else "unresolved"
            lines.append(f"- {finding.location} — {finding.severity} — {state}")
        return "\n".join(lines)


def is_documentation_only(changed_files: Sequence[str]) -> bool:
    """True when EVERY changed file is documentation. An empty diff is not."""
    paths = [p for p in (changed_files or ()) if isinstance(p, str) and p.strip()]
    if not paths:
        return False
    return all(Path(p).suffix.lower() in _DOCUMENTATION_SUFFIXES for p in paths)


def raw_output_dir() -> Path:
    """Where raw lens output goes: local disk, deliberately outside the tree."""
    return Path(tempfile.gettempdir()) / "super-board-codex"


def build_lens_command(
    lens: str, base_ref: str, *, prompt: Optional[str] = None
) -> tuple[str, ...]:
    """The exact command one lens issues.

    `codex exec review` and a custom prompt are mutually exclusive — the CLI
    rejects the combination — so passing one here would silently lose the entire
    structured review. That is refused rather than dropped.
    """
    if lens not in CODEX_LENSES:
        raise CodexGateError("codex-lens-unknown", f"{lens!r} is not one of the four lenses")
    common = ("-m", CODEX_MODEL, "-c", f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"')
    if lens == "structured-diff":
        if prompt:
            raise CodexGateError(
                "codex-review-prompt-conflict",
                "`codex exec review` never receives a custom prompt: the CLI rejects the "
                "combination and the structured review would be lost",
            )
        return (
            "codex",
            "exec",
            "review",
            "--base",
            f'"$(git merge-base origin/{base_ref} HEAD)"',
            *common,
        )
    return ("codex", "exec", *common, "-s", "read-only", prompt or CODEX_LENS_PROMPTS[lens])


def parse_findings(lens: str, output: str) -> tuple[Finding, ...]:
    """Parse `file:line — severity — fix` lines. Unresolved on arrival."""
    findings: list[Finding] = []
    for line in (output or "").splitlines():
        match = _FINDING_RE.match(line)
        if match is None:
            continue
        findings.append(
            Finding(
                lens=lens,
                severity=match.group("severity"),
                location=match.group("location"),
                summary=match.group("summary"),
            )
        )
    return tuple(findings)


def resolve_findings(
    findings: Sequence[Finding], resolutions: Mapping[str, str]
) -> tuple[Finding, ...]:
    """Mark findings resolved — only where committed evidence says so.

    Every severity counts, nits included. "It's only a nit" is how a review
    becomes advisory, and an advisory review is not a gate.
    """
    resolved: list[Finding] = []
    for finding in findings:
        evidence = (resolutions or {}).get(finding.location)
        has_evidence = isinstance(evidence, str) and bool(evidence.strip())
        resolved.append(
            Finding(
                lens=finding.lens,
                severity=finding.severity,
                location=finding.location,
                summary=finding.summary,
                resolved=has_evidence,
                evidence=evidence if has_evidence else None,
            )
        )
    return tuple(resolved)


def _default_runner(command: Sequence[str], cwd: Path) -> Mapping[str, Any]:
    result = subprocess.run(
        list(command), cwd=str(cwd), capture_output=True, text=True, timeout=3600
    )
    return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}


def _read_ledger(path: Optional[Path]) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def run_codex_fleet(
    base_ref: str,
    worktree: Path,
    documentation_only: bool,
    *,
    runner: Optional[Any] = None,
    ledger: Optional[Path] = None,
    pull_request_url: Optional[str] = None,
    force_rerun: bool = False,
    prompts: Optional[Mapping[str, str]] = None,
    resolutions: Optional[Mapping[str, str]] = None,
    model: str = CODEX_MODEL,
    reasoning_effort: str = CODEX_REASONING_EFFORT,
    plan_only: bool = False,
) -> CodexFleetReport:
    """Run the four lenses in parallel and decide whether the gate passes."""
    if model != CODEX_MODEL:
        raise CodexGateError(
            "codex-model-invalid",
            f"every lens must run on {CODEX_MODEL}; {model!r} is not the gate this contract names",
        )
    if reasoning_effort != CODEX_REASONING_EFFORT:
        raise CodexGateError(
            "codex-reasoning-effort-invalid",
            f"every lens must run at model_reasoning_effort={CODEX_REASONING_EFFORT!r}; "
            f"{reasoning_effort!r} is a cheaper review pretending to be the gate",
        )

    prompts = dict(prompts or {})
    if prompts.get("structured-diff"):
        raise CodexGateError(
            "codex-review-prompt-conflict",
            "`codex exec review` never receives a custom prompt",
        )

    if documentation_only:
        # A diff that is entirely documentation has no runtime behaviour to
        # review; running four maximum-effort lenses over it burns usage for
        # nothing.
        return CodexFleetReport((), (), True, "documentation-only-exempt")

    ledger_data = _read_ledger(ledger)
    key = pull_request_url or "unknown-pull-request"
    if ledger is not None and key in ledger_data and not force_rerun:
        raise CodexGateError(
            "codex-fleet-already-run",
            "one fleet per pull request. A second automatic run costs the same usage and "
            "reviews the same code; re-review only on an explicit request",
        )

    commands = {
        lens: build_lens_command(lens, base_ref, prompt=prompts.get(lens))
        for lens in CODEX_LENSES
    }
    if plan_only:
        return CodexFleetReport(
            tuple(
                LensResult(lens, commands[lens], model, reasoning_effort, 0)
                for lens in CODEX_LENSES
            ),
            (),
            True,
            "plan-only",
        )

    run = _default_runner if runner is None else runner
    worktree = Path(worktree)
    raw_dir = raw_output_dir()
    raw_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, Mapping[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(CODEX_LENSES)) as pool:
        futures = {
            pool.submit(run, commands[lens], worktree): lens for lens in CODEX_LENSES
        }
        for future in as_completed(futures):
            lens = futures[future]
            try:
                results[lens] = future.result()
            except Exception as exc:  # a lens that could not run did not pass
                results[lens] = {"exit_code": 1, "stdout": "", "stderr": str(exc)}

    lenses: list[LensResult] = []
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for lens in CODEX_LENSES:
        result = results.get(lens, {"exit_code": 1, "stdout": ""})
        lenses.append(
            LensResult(lens, commands[lens], model, reasoning_effort, int(result.get("exit_code", 1)))
        )
        stdout = str(result.get("stdout") or "")
        # Raw output stays on local disk, outside the Git tree.
        try:
            (raw_dir / f"{lens}.log").write_text(stdout, encoding="utf-8")
        except OSError:
            pass
        for finding in parse_findings(lens, stdout):
            # The same finding surfaced by four lenses is one finding to fix.
            fingerprint = (finding.location, finding.severity)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            findings.append(finding)

    resolved = resolve_findings(findings, resolutions or {})
    failed_lens = any(lens.exit_code != 0 for lens in lenses)
    unresolved = [f for f in resolved if not f.resolved]

    reason: Optional[str] = None
    if failed_lens:
        reason = "codex-lens-failed"
    elif unresolved:
        reason = "codex-findings-unresolved"

    if ledger is not None:
        ledger_data[key] = {"base_ref": base_ref, "lenses": list(CODEX_LENSES)}
        try:
            Path(ledger).parent.mkdir(parents=True, exist_ok=True)
            Path(ledger).write_text(
                json.dumps(ledger_data, sort_keys=True), encoding="utf-8"
            )
        except OSError:
            pass

    return CodexFleetReport(tuple(lenses), resolved, reason is None, reason)


__all__ = [
    "ALLOWLIST_FILENAME",
    "CODEX_LENSES",
    "CODEX_LENS_PROMPTS",
    "CODEX_MODEL",
    "CODEX_REASONING_EFFORT",
    "DONE_WRITER",
    "MERGE_MECHANISMS",
    "REQUIRED_MERGE_CONFIG",
    "REQUIRED_REPOSITORY_SETTINGS",
    "CodexFleetReport",
    "CodexGateError",
    "Finding",
    "LensResult",
    "MergeContractError",
    "MergeOccurrence",
    "MergeScanReport",
    "ReviewHandoff",
    "build_lens_command",
    "is_documentation_only",
    "load_allowlist",
    "may_write_done",
    "parse_findings",
    "raw_output_dir",
    "resolve_findings",
    "review_handoff",
    "run_codex_fleet",
    "scan_merge_prohibitions",
    "scan_retired_status",
    "verify_human_merge_config",
    "verify_repository_settings",
]
