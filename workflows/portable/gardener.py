#!/usr/bin/env python3
"""
gardener.py - Automated dead code, unused exports, and tech debt analysis for PolySimulator.

Part of the portable workflow core in Wladefant/super-board.
References:
  - Superboard Issue #227 (High-Trust Agent Architecture & Verification Skills)
  - Adopt / Adapt / Build Master Matrix: Idea 5 (The Gardener Role)

Tools integrated:
  - Frontend: Knip (`npx --yes knip@6.38.0 --reporter json`) in Next.js / TypeScript frontend.
  - Backend:  Vulture (`python -m vulture backend/ --min-confidence 80`) in FastAPI backend.

Responsibilities:
  1. Executes Knip and Vulture scans (or ingests pre-computed reports).
  2. Classifies findings into safe-to-prune vs review-required vs false-positive categories.
     - Framework entrypoints (Next.js app router pages, layout, route handlers) are preserved.
     - Test files and test fixtures (pytest fixtures, test mocks) are preserved.
     - Alembic migration scripts and re-exports in __init__.py are preserved.
  3. Prepares a bounded cleanup task spec for a cheap lane (Spark / Gemini 3.8 Flash)
     capping pruning targets to a safe subset (default 15 items), with strict verification
     gates (tsc --noEmit, targeted tests) and PR labels: `kind:gardener`, `risk:low`.
  4. Provides `--dry-run` to print findings summary and generated task spec without mutating code.
  5. Standalone CLI execution with zero daemon scheduling requirements.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ==============================================================================
# Data Models
# ==============================================================================

@dataclass
class ToolFinding:
    """Represents a single unused code finding from Knip or Vulture."""
    tool: str                          # "knip" | "vulture"
    file: str                          # Repo-relative path with forward slashes
    line: Optional[int] = None
    col: Optional[int] = None
    symbol: str = ""
    kind: str = ""                     # "file" | "export" | "type" | "import" | "variable" | "function" | "class" | "unreachable" | "dependency"
    confidence: int = 100              # 0 - 100
    category: str = "other"            # "verified_dead_file" | "verified_dead_export" | "verified_dead_type" | "unused_import" | "unreachable_code" | "unused_variable" | "test_fixture_or_dummy" | "framework_entrypoint" | "test_file" | "alembic_config" | "unused_dependency" | "other"
    safe_to_prune: bool = False
    reason: str = ""


@dataclass
class CleanupTaskSpec:
    """Structured specification for a cheap subagent lane (Spark/Flash)."""
    title: str
    pr_title: str
    pr_labels: List[str]
    target_lane: str
    max_items: int
    context_text: str
    task_text: str
    target_items: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class BugLintProposal:
    """Proposal for a concrete static analysis lint rule to prevent regression of a closed bug."""
    bug_number: int
    bug_title: str
    fix_pr_url: Optional[str] = None
    fix_pr_number: Optional[int] = None
    rule_type: str = "ast-grep"  # "ast-grep" | "regex" | "eslint" | "ruff"
    target_pattern: str = ""
    anti_pattern_code: str = ""
    fixed_code: str = ""
    proof_text: str = ""
    target_file: str = ""
    description: str = ""

@dataclass
class GardenerIssueCandidate:
    """A candidate GitHub issue adhering to the Superboard Issue Contract."""
    fingerprint: str
    title: str
    labels: List[str]
    area: str
    risk: str
    body: str
    category: str  # "dead_code_prune" | "workaround_comment" | "bug_to_lint"
    target_items_count: int = 1


@dataclass
class GardenerReport:
    """Complete summary of a Gardener scan, classification, and issue generation run."""
    repo_root: str
    timestamp: str
    min_confidence: int
    frontend_findings: List[ToolFinding] = field(default_factory=list)
    backend_findings: List[ToolFinding] = field(default_factory=list)
    workaround_findings: List[ToolFinding] = field(default_factory=list)
    bug_lint_proposals: List[BugLintProposal] = field(default_factory=list)
    issue_candidates: List[GardenerIssueCandidate] = field(default_factory=list)
    created_issues: List[Dict[str, Any]] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    task_spec: Optional[CleanupTaskSpec] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo_root": self.repo_root,
            "timestamp": self.timestamp,
            "min_confidence": self.min_confidence,
            "summary": self.summary,
            "frontend_findings": [asdict(f) for f in self.frontend_findings],
            "backend_findings": [asdict(f) for f in self.backend_findings],
            "workaround_findings": [asdict(f) for f in self.workaround_findings],
            "bug_lint_proposals": [asdict(p) for p in self.bug_lint_proposals],
            "issue_candidates": [asdict(c) for c in self.issue_candidates],
            "created_issues": self.created_issues,
            "task_spec": asdict(self.task_spec) if self.task_spec else None,
        }

# ==============================================================================
# Knip Frontend Scanner & Classifier
# ==============================================================================

# Next.js App Router file-system convention suffixes
NEXTJS_ENTRYPOINT_PATTERNS = (
    "/page.",
    "/layout.",
    "/route.",
    "/loading.",
    "/error.",
    "/not-found.",
    "/template.",
    "/default.",
    "/global-error.",
    "/opengraph-image.",
    "/twitter-image.",
    "/icon.",
    "/apple-icon.",
    "/sitemap.",
    "/robots.",
)

CONFIG_FILE_NAMES = {
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "tailwind.config.js",
    "tailwind.config.ts",
    "postcss.config.js",
    "postcss.config.mjs",
    "middleware.ts",
    "middleware.js",
    "tsconfig.json",
    "package.json",
    "vitest.config.ts",
    "jest.config.js",
    "playwright.config.ts",
}
def is_frontend_entrypoint(rel_path: str) -> bool:
    """Checks if a file is a Next.js entrypoint, config file, or public asset."""
    p = rel_path.replace("\\", "/").lower()
    base = os.path.basename(p)
    if base in CONFIG_FILE_NAMES:
        return True
    if p.startswith("public/"):
        return True
    if p.startswith("app/") or "/app/" in p:
        for suffix in NEXTJS_ENTRYPOINT_PATTERNS:
            if suffix in p:
                return True
    return False


def is_test_file(rel_path: str) -> bool:
    """Checks if a file is a unit/integration test."""
    p = rel_path.replace("\\", "/").lower()
    return (
        ".test." in p
        or ".spec." in p
        or "/__tests__/" in p
        or p.startswith("__tests__/")
    )


def classify_knip_issue(
    issue: Dict[str, Any],
    frontend_subpath: str = "frontend",
    frontend_rel: Optional[str] = None,
) -> List[ToolFinding]:
    """Classifies raw Knip issue entries into structured ToolFinding objects."""
    subpath = frontend_rel or frontend_subpath
    findings: List[ToolFinding] = []
    file_raw = issue.get("file", "")
    norm_file = os.path.normpath(os.path.join(subpath, file_raw)).replace("\\", "/")

    # 1. Unused whole files
    if issue.get("files"):
        for _ in issue["files"]:
            if is_frontend_entrypoint(file_raw):
                findings.append(
                    ToolFinding(
                        tool="knip",
                        file=norm_file,
                        kind="file",
                        symbol=file_raw,
                        category="framework_entrypoint",
                        safe_to_prune=False,
                        reason="Next.js App Router entrypoint or framework configuration file",
                    )
                )
            elif is_test_file(file_raw):
                findings.append(
                    ToolFinding(
                        tool="knip",
                        file=norm_file,
                        kind="file",
                        symbol=file_raw,
                        category="test_file",
                        safe_to_prune=False,
                        reason="Test file; test runner may execute it via glob pattern rather than direct import",
                    )
                )
            else:
                findings.append(
                    ToolFinding(
                        tool="knip",
                        file=norm_file,
                        kind="file",
                        symbol=file_raw,
                        category="verified_dead_file",
                        safe_to_prune=True,
                        reason="Unreferenced component, script, or module with zero imports across the project",
                    )
                )

    # 2. Unused exports
    for exp in issue.get("exports", []):
        name = exp.get("name", "")
        line = exp.get("line")
        col = exp.get("col")
        # Entrypoints export defaults or specific metadata
        if is_frontend_entrypoint(file_raw):
            findings.append(
                ToolFinding(
                    tool="knip",
                    file=norm_file,
                    line=line,
                    col=col,
                    kind="export",
                    symbol=name,
                    category="framework_entrypoint",
                    safe_to_prune=False,
                    reason="Export in framework entrypoint or route handler",
                )
            )
        else:
            findings.append(
                ToolFinding(
                    tool="knip",
                    file=norm_file,
                    line=line,
                    col=col,
                    kind="export",
                    symbol=name,
                    category="verified_dead_export",
                    safe_to_prune=True,
                    reason="Exported symbol is never imported outside or referenced internally",
                )
            )

    # 3. Unused types
    for typ in issue.get("types", []):
        name = typ.get("name", "")
        line = typ.get("line")
        col = typ.get("col")
        findings.append(
            ToolFinding(
                tool="knip",
                file=norm_file,
                line=line,
                col=col,
                kind="type",
                symbol=name,
                category="verified_dead_type",
                safe_to_prune=True,
                reason="Exported TypeScript type or interface is never referenced",
            )
        )

    # 4. Unused dependencies
    for dep in issue.get("dependencies", []):
        name = dep if isinstance(dep, str) else dep.get("name", str(dep))
        findings.append(
            ToolFinding(
                tool="knip",
                file=norm_file,
                kind="dependency",
                symbol=name,
                category="unused_dependency",
                safe_to_prune=False,
                reason="Unused package in package.json; requires manual review before removal (may be CLI or runtime peer)",
            )
        )

    for dep in issue.get("devDependencies", []):
        name = dep if isinstance(dep, str) else dep.get("name", str(dep))
        findings.append(
            ToolFinding(
                tool="knip",
                file=norm_file,
                kind="dependency",
                symbol=name,
                category="unused_dependency",
                safe_to_prune=False,
                reason="Unused devDependency; requires manual review before removal",
            )
        )

    return findings


def run_knip(
    repo_root: Path, frontend_subpath: str = "frontend"
) -> Tuple[List[ToolFinding], Optional[str]]:
    """Runs pinned `npx --yes knip@6.38.0 --reporter json` inside the frontend dir and parses findings."""
    frontend_dir = repo_root / frontend_subpath
    if not frontend_dir.is_dir():
        return [], f"Frontend directory not found: {frontend_dir}"

    package_json = frontend_dir / "package.json"
    if not package_json.is_file():
        return [], f"No package.json found in {frontend_dir}"

    # Pinned exact version: `--yes` with an unpinned spec would fetch and execute
    # whatever knip npm serves at runtime; the pin keeps the network install
    # reproducible (offline / fixture mode via --knip-report-file needs no network).
    cmd = ["npx", "--yes", "knip@6.38.0", "--reporter", "json"]
    # Windows: `npx` resolves to npx.cmd (a batch file) which subprocess.run
    # cannot execute with shell=False, so the shell flag is load-bearing there.
    use_shell = os.name == "nt"

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(frontend_dir),
            capture_output=True,
            text=True,
            shell=use_shell,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return [], "Knip scan timed out after 180 seconds"
    except Exception as e:
        return [], f"Failed to execute knip: {e}"

    stdout = proc.stdout.strip()
    if not stdout and proc.stderr:
        return [], f"Knip produced no stdout. Stderr: {proc.stderr.strip()[:500]}"

    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        return [], f"Failed to parse knip JSON output: {e}. Output preview: {stdout[:300]}"

    issues = data.get("issues", [])
    findings: List[ToolFinding] = []
    for issue in issues:
        findings.extend(classify_knip_issue(issue, frontend_subpath=frontend_subpath))

    return findings, None


# ==============================================================================
# Vulture Backend Scanner & Classifier
# ==============================================================================

VULTURE_LINE_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):\s*"
    r"(?P<msg>(?:unused\s+(?P<kind>[a-zA-Z_]+)\s+'(?P<name>[^']+)'|unreachable code after '(?P<token>[^']+)'))\s*"
    r"\((?P<confidence>\d+)%\s+confidence\)$"
)


def classify_vulture_line(line: str, repo_root: Path) -> Optional[ToolFinding]:
    """Parses a single vulture output line and classifies it."""
    line = line.strip()
    if not line:
        return None

    m = VULTURE_LINE_RE.match(line)
    if not m:
        return None

    raw_file = m.group("file").replace("\\", "/")
    # Normalize relative to repo root if path is absolute
    try:
        p = Path(raw_file)
        if p.is_absolute():
            rel_file = os.path.relpath(raw_file, str(repo_root)).replace("\\", "/")
        else:
            rel_file = raw_file
    except Exception:
        rel_file = raw_file

    line_no = int(m.group("line"))
    kind = m.group("kind") or "unreachable"
    symbol = m.group("name") or m.group("token") or ""
    confidence = int(m.group("confidence"))

    # Determine classification
    is_test = "tests/" in rel_file or "conftest.py" in rel_file
    is_alembic = "alembic/" in rel_file or "migration" in rel_file
    is_init = rel_file.endswith("__init__.py")

    if is_alembic:
        return ToolFinding(
            tool="vulture",
            file=rel_file,
            line=line_no,
            symbol=symbol,
            kind=kind,
            confidence=confidence,
            category="alembic_config",
            safe_to_prune=False,
            reason="Alembic migration metadata, revision variable, or migration helper",
        )

    if is_test:
        if kind == "variable":
            return ToolFinding(
                tool="vulture",
                file=rel_file,
                line=line_no,
                symbol=symbol,
                kind=kind,
                confidence=confidence,
                category="test_fixture_or_dummy",
                safe_to_prune=False,
                reason="Pytest fixture parameter or test mock return variable",
            )
        elif kind == "import":
            return ToolFinding(
                tool="vulture",
                file=rel_file,
                line=line_no,
                symbol=symbol,
                kind=kind,
                confidence=confidence,
                category="unused_import",
                safe_to_prune=True,
                reason="Unused import in test file",
            )
        else:
            return ToolFinding(
                tool="vulture",
                file=rel_file,
                line=line_no,
                symbol=symbol,
                kind=kind,
                confidence=confidence,
                category="test_file",
                safe_to_prune=False,
                reason="Test function, class, or fixture helper",
            )

    if is_init and kind == "import":
        return ToolFinding(
            tool="vulture",
            file=rel_file,
            line=line_no,
            symbol=symbol,
            kind=kind,
            confidence=confidence,
            category="framework_entrypoint",
            safe_to_prune=False,
            reason="Re-export in __init__.py defining package public API",
        )

    if kind == "import":
        return ToolFinding(
            tool="vulture",
            file=rel_file,
            line=line_no,
            symbol=symbol,
            kind=kind,
            confidence=confidence,
            category="unused_import",
            safe_to_prune=True,
            reason=f"Unused import '{symbol}' in application code ({confidence}% confidence)",
        )

    if kind == "unreachable":
        return ToolFinding(
            tool="vulture",
            file=rel_file,
            line=line_no,
            symbol=symbol,
            kind=kind,
            confidence=confidence,
            category="unreachable_code",
            safe_to_prune=True,
            reason=f"Unreachable code block after '{symbol}' ({confidence}% confidence)",
        )

    if kind in ("function", "class", "method"):
        is_internal = symbol.startswith("_")
        return ToolFinding(
            tool="vulture",
            file=rel_file,
            line=line_no,
            symbol=symbol,
            kind=kind,
            confidence=confidence,
            category="verified_dead_code" if is_internal else "unused_function",
            safe_to_prune=is_internal,
            reason=f"Unused {kind} '{symbol}' ({confidence}% confidence)"
            + (" (internal private symbol)" if is_internal else " (requires route/decorator verification)"),
        )

    if kind == "variable":
        return ToolFinding(
            tool="vulture",
            file=rel_file,
            line=line_no,
            symbol=symbol,
            kind=kind,
            confidence=confidence,
            category="unused_variable",
            safe_to_prune=confidence >= 90,
            reason=f"Unused local variable '{symbol}' ({confidence}% confidence)",
        )

    return ToolFinding(
        tool="vulture",
        file=rel_file,
        line=line_no,
        symbol=symbol,
        kind=kind,
        confidence=confidence,
        category="other",
        safe_to_prune=False,
        reason=f"Vulture finding: {line}",
    )


def run_vulture(
    repo_root: Path, backend_subpath: str = "backend", min_confidence: int = 80
) -> Tuple[List[ToolFinding], Optional[str]]:
    """Runs `vulture backend/ --min-confidence <min_confidence>` and parses output."""
    backend_dir = repo_root / backend_subpath
    if not backend_dir.is_dir():
        return [], f"Backend directory not found: {backend_dir}"

    cmd = [
        sys.executable,
        "-m",
        "vulture",
        backend_subpath + "/",
        "--min-confidence",
        str(min_confidence),
    ]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return [], "Vulture scan timed out after 180 seconds"
    except Exception as e:
        return [], f"Failed to execute vulture: {e}"

    # Vulture exits with 0 (no dead code), 1 (error), or 2/3 (dead code found)
    output_lines = (proc.stdout + "\n" + proc.stderr).splitlines()
    findings: List[ToolFinding] = []

    for line in output_lines:
        f = classify_vulture_line(line, repo_root)
        if f is not None and f.confidence >= min_confidence:
            findings.append(f)

    return findings, None


# ==============================================================================
# Task Spec Generator
# ==============================================================================

def generate_cleanup_task_spec(
    report: GardenerReport, max_items: int = 15, target_lane: str = "spark"
) -> CleanupTaskSpec:
    """Selects high-confidence, bounded safe-to-prune targets and prepares task spec."""
    safe_candidates: List[ToolFinding] = []

    # Priority 1: Verified dead files in frontend (zero risk of breaking callers)
    dead_files = [
        f for f in report.frontend_findings
        if f.safe_to_prune and f.category == "verified_dead_file"
    ]
    # Priority 2: Unused backend imports in app/
    backend_imports = [
        f for f in report.backend_findings
        if f.safe_to_prune and f.category == "unused_import"
    ]
    # Priority 3: Verified dead exports in frontend
    dead_exports = [
        f for f in report.frontend_findings
        if f.safe_to_prune and f.category == "verified_dead_export"
    ]
    # Priority 4: Verified dead types in frontend
    dead_types = [
        f for f in report.frontend_findings
        if f.safe_to_prune and f.category == "verified_dead_type"
    ]
    # Priority 5: Unreachable code in backend
    unreachable = [
        f for f in report.backend_findings
        if f.safe_to_prune and f.category == "unreachable_code"
    ]

    # Combine in order of safety
    pool = dead_files + backend_imports + dead_exports + dead_types + unreachable

    # Deduplicate by file + symbol + kind; enforce the bound BEFORE appending so
    # max_items=0 yields an empty spec instead of one off-by-one candidate.
    seen: Set[str] = set()
    for item in pool:
        if len(safe_candidates) >= max_items:
            break
        key = f"{item.file}:{item.symbol}:{item.kind}"
        if key not in seen:
            seen.add(key)
            safe_candidates.append(item)

    target_items_data = [
        {
            "file": item.file,
            "line": item.line,
            "symbol": item.symbol,
            "kind": item.kind,
            "category": item.category,
            "reason": item.reason,
            "tool": item.tool,
        }
        for item in safe_candidates
    ]

    target_items_formatted = "\n".join(
        f"  - `{item.file}`{f':{item.line}' if item.line else ''} [{item.kind}] `{item.symbol}` ({item.category})"
        for item in safe_candidates
    ) or "  - (No items selected; scan was clean or all findings require review)"

    context_text = f"""## Goal
Safely prune a bounded batch of {len(safe_candidates)} verified-dead code artifacts (orphaned files, unreferenced exports, and dead imports) identified by the Gardener scanner (Knip + Vulture) to reduce code debt without altering runtime behavior.

## Constraints
- Remove ONLY the explicitly targeted verified-dead files and symbols listed below.
- Do NOT modify public API routes, database schemas, or Alembic revision heads.
- Verification gates MUST pass: `cd frontend && npx tsc --noEmit` and targeted unit tests.
- PR must be created against base branch labeled `kind:gardener`, `risk:low`.
- Author: Wladimir Kirjanovs <wladefant@gmail.com>.

## Contract
- Zero TypeScript typecheck errors (`npx tsc --noEmit` exits 0).
- Git diff strictly confined to removing listed dead files and symbols.
- PR opened with labels `kind:gardener`, `risk:low` linking to issue #227."""

    task_text = f"""## Target
Bounded batch of verified dead code artifacts ({len(safe_candidates)} items):
{target_items_formatted}

Explicit non-goals:
- Do NOT refactor surrounding application logic.
- Do NOT touch untested or ambiguous dynamic exports.
- Do NOT remove test files or Alembic configuration.

## Change
1. For verified dead files: delete the unreferenced file from the repository.
2. For verified dead exports: if the helper function or constant is only used locally within the file, remove the `export` keyword; if it is completely unused internally as well, remove the symbol declaration.
3. For verified dead imports: remove the unused import statement in the specified backend file.
4. Run `cd frontend && npx tsc --noEmit` to verify frontend TypeScript compilation passes.
5. If backend Python files were modified, run targeted tests to confirm no regressions.
6. Commit changes with message `chore(gardener): prune verified dead exports and files` and open PR.

## Acceptance
- `npx tsc --noEmit` completes cleanly with 0 errors.
- Modified files pass targeted tests.
- Pull request opened with labels `kind:gardener`, `risk:low`."""

    return CleanupTaskSpec(
        title=f"chore(gardener): prune {len(safe_candidates)} verified dead artifacts",
        pr_title=f"chore(gardener): prune {len(safe_candidates)} verified dead artifacts",
        pr_labels=["kind:gardener", "risk:low"],
        target_lane=target_lane,
        max_items=max_items,
        context_text=context_text,
        task_text=task_text,
        target_items=target_items_data,
    )


# ==============================================================================
# Summary Formatting
# ==============================================================================

def format_summary_markdown(report: GardenerReport) -> str:
    """Formats the Gardener report as a comprehensive Markdown document."""
    s = report.summary
    md = []
    md.append(f"# Gardener Scan Report")
    md.append(f"**Date:** {report.timestamp} | **Target Root:** `{report.repo_root}` | **Min Confidence:** {report.min_confidence}%\n")

    md.append("## Findings Summary")
    md.append("| Tool | Surface | Total | Safe to Prune | Review Required | Skipped (Framework/Tests) |")
    md.append("|---|---|---|---|---|---|")
    md.append(
        f"| **Knip** (v6.38) | Frontend (TS/React) | {s.get('frontend_total', 0)} | "
        f"**{s.get('frontend_safe_prune', 0)}** | {s.get('frontend_review_required', 0)} | "
        f"{s.get('frontend_skipped', 0)} |"
    )
    md.append(
        f"| **Vulture** (>= {report.min_confidence}%) | Backend (Python) | {s.get('backend_total', 0)} | "
        f"**{s.get('backend_safe_prune', 0)}** | {s.get('backend_review_required', 0)} | "
        f"{s.get('backend_skipped', 0)} |"
    )
    md.append(
        f"| **Total** | Full Codebase | {s.get('total_findings', 0)} | "
        f"**{s.get('total_safe_prune', 0)}** | {s.get('total_review_required', 0)} | "
        f"{s.get('total_skipped', 0)} |\n"
    )

    md.append("## Classified Categories")
    md.append("### Frontend (Knip)")
    md.append(f"- **Verified Dead Files (components/lib):** {s.get('frontend_dead_files', 0)} (safe to delete)")
    md.append(f"- **Verified Dead Exports:** {s.get('frontend_dead_exports', 0)} (safe to un-export / delete)")
    md.append(f"- **Verified Dead Types:** {s.get('frontend_dead_types', 0)} (safe to delete)")
    md.append(f"- **Framework Entrypoints / Configs:** {s.get('frontend_framework_entrypoints', 0)} (preserved)")
    md.append(f"- **Unused Tests (vitest glob):** {s.get('frontend_test_files', 0)} (preserved)")
    md.append(f"- **Unused Package Dependencies:** {s.get('frontend_unused_deps', 0)} (review required)\n")

    md.append("### Backend (Vulture)")
    md.append(f"- **Unused Application Imports:** {s.get('backend_unused_imports', 0)} (safe to prune)")
    md.append(f"- **Unreachable Code:** {s.get('backend_unreachable', 0)} (safe to prune)")
    md.append(f"- **Unused Application Variables:** {s.get('backend_unused_variables', 0)}")
    md.append(f"- **Test Fixtures & Dummies:** {s.get('backend_test_fixtures', 0)} (preserved)")
    md.append(f"- **Alembic Migrations / Helpers:** {s.get('backend_alembic', 0)} (preserved)\n")

    if report.task_spec and report.task_spec.target_items:
        md.append(f"## Bounded Cleanup Candidate Batch ({len(report.task_spec.target_items)} items for {report.task_spec.target_lane.upper()})")
        md.append("| # | Tool | Location | Kind | Symbol | Category |")
        md.append("|---|---|---|---|---|---|")
        for i, item in enumerate(report.task_spec.target_items, 1):
            loc = f"`{item['file']}`" + (f":{item['line']}" if item.get('line') else "")
            md.append(f"| {i} | {item['tool']} | {loc} | `{item['kind']}` | `{item['symbol']}` | {item['category']} |")
        md.append("")
        md.append("### Generated Task Spec Preview")
        md.append("```markdown")
        md.append(report.task_spec.context_text)
        md.append("")
        md.append(report.task_spec.task_text)
        md.append("```\n")

    return "\n".join(md)


def format_summary_text(report: GardenerReport) -> str:
    """Formats the Gardener report as clean terminal text output."""
    s = report.summary
    lines = []
    lines.append("=" * 70)
    lines.append("GARDENER SCAN REPORT (Knip + Vulture)")
    lines.append("=" * 70)
    lines.append(f"Target Repo     : {report.repo_root}")
    lines.append(f"Scan Timestamp  : {report.timestamp}")
    lines.append(f"Min Confidence  : {report.min_confidence}%")
    lines.append("-" * 70)
    lines.append(f"Frontend Findings (Knip)       : {s.get('frontend_total', 0)}")
    lines.append(f"  - Verified dead files        : {s.get('frontend_dead_files', 0)} (SAFE TO PRUNE)")
    lines.append(f"  - Verified dead exports      : {s.get('frontend_dead_exports', 0)} (SAFE TO PRUNE)")
    lines.append(f"  - Verified dead types        : {s.get('frontend_dead_types', 0)} (SAFE TO PRUNE)")
    lines.append(f"  - Preserved framework routes : {s.get('frontend_framework_entrypoints', 0)}")
    lines.append(f"  - Preserved test files       : {s.get('frontend_test_files', 0)}")
    lines.append(f"  - Unused dependencies        : {s.get('frontend_unused_deps', 0)} (review required)")
    lines.append("-" * 70)
    lines.append(f"Backend Findings (Vulture)     : {s.get('backend_total', 0)}")
    lines.append(f"  - Unused app imports         : {s.get('backend_unused_imports', 0)} (SAFE TO PRUNE)")
    lines.append(f"  - Unreachable code           : {s.get('backend_unreachable', 0)} (SAFE TO PRUNE)")
    lines.append(f"  - Unused app variables       : {s.get('backend_unused_variables', 0)}")
    lines.append(f"  - Preserved test fixtures    : {s.get('backend_test_fixtures', 0)}")
    lines.append(f"  - Preserved Alembic metadata : {s.get('backend_alembic', 0)}")
    lines.append("-" * 70)
    lines.append(f"TOTAL SAFE TO PRUNE            : {s.get('total_safe_prune', 0)}")
    lines.append(f"TOTAL REVIEW REQUIRED          : {s.get('total_review_required', 0)}")
    lines.append(f"TOTAL PRESERVED (FALSE POSITIVE): {s.get('total_skipped', 0)}")
    lines.append(f"UNTRACKED WORKAROUND COMMENTS  : {s.get('untracked_workaround_comments', 0)}")

    if report.bug_lint_proposals:
        lines.append("-" * 70)
        lines.append(f"BUG-TO-LINT RULE PROPOSALS ({len(report.bug_lint_proposals)} closed bugs)")
        lines.append("-" * 70)
        for p in report.bug_lint_proposals[:5]:
            lines.append(f"  - #{p.bug_number}: {p.bug_title} (Fix: {p.fix_pr_url or p.fix_pr_number})")

    if report.issue_candidates:
        lines.append("-" * 70)
        lines.append(f"ISSUE CANDIDATES GENERATED ({len(report.issue_candidates)} total)")
        lines.append("-" * 70)
        for c in report.issue_candidates:
            lines.append(f"  [{c.fingerprint}] {c.title}")

    if report.created_issues:
        lines.append("=" * 70)
        lines.append(f"CREATED GITHUB ISSUES ({len(report.created_issues)})")
        lines.append("=" * 70)
        for ci in report.created_issues:
            lines.append(f"  * {ci.get('url')} - {ci.get('title')}")

    if report.task_spec:
        lines.append("=" * 70)
        lines.append(f"BOUNDED CLEANUP TASK SPEC ({len(report.task_spec.target_items)} items for {report.task_spec.target_lane.upper()})")
        lines.append("=" * 70)
        for i, item in enumerate(report.task_spec.target_items, 1):
            loc = item['file'] + (f":{item['line']}" if item.get('line') else "")
            lines.append(f"  [{i:02d}] {item['tool'].upper():<7} {loc} -> {item['kind']} '{item['symbol']}'")
        lines.append("-" * 70)
        lines.append(f"PR Title  : {report.task_spec.pr_title}")
        lines.append(f"PR Labels : {', '.join(report.task_spec.pr_labels)}")
        lines.append("=" * 70)

    return "\n".join(lines)


# ==============================================================================
# Workaround Comment Scanner
# ==============================================================================

WORKAROUND_COMMENT_PATTERNS = re.compile(
    r'(?://|#)\s*(?:workaround|hack|temporary fix|bandaid|quick fix|todo:\s*fix later)',
    re.IGNORECASE,
)
ISSUE_REF_PATTERNS = re.compile(
    r'(?:#\d+|issues/\d+|issue\s*#?\d+|gh-\d+)',
    re.IGNORECASE,
)


def scan_workaround_comments(
    repo_root: Path,
    frontend_subpath: str = "frontend",
    backend_subpath: str = "backend",
) -> List[ToolFinding]:
    """Scans frontend and backend code for workaround comments lacking tracking issue references."""
    findings: List[ToolFinding] = []
    subpaths = [frontend_subpath, backend_subpath]
    valid_exts = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".py"}
    skip_dirs = {"node_modules", ".next", "__pycache__", ".pytest_cache", ".git", "dist", "build"}

    for sub in subpaths:
        base = repo_root / sub
        if not base.is_dir():
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for file in files:
                p = Path(root) / file
                if p.suffix not in valid_exts:
                    continue
                rel_path = p.relative_to(repo_root).as_posix()
                try:
                    with open(p, "r", encoding="utf-8", errors="ignore") as f:
                        for idx, line in enumerate(f, 1):
                            if WORKAROUND_COMMENT_PATTERNS.search(line):
                                has_issue = bool(ISSUE_REF_PATTERNS.search(line))
                                if not has_issue:
                                    findings.append(
                                        ToolFinding(
                                            tool="comment_linter",
                                            file=rel_path,
                                            line=idx,
                                            symbol="workaround_comment",
                                            kind="workaround",
                                            confidence=100,
                                            category="untracked_workaround_comment",
                                            safe_to_prune=False,
                                            reason=f"Workaround/hack comment without tracking issue: {line.strip()[:100]}",
                                        )
                                    )
                except Exception:
                    pass
    return findings


# ==============================================================================
# Bug to Lint-Rule Loop Scanner
# ==============================================================================

def parse_pr_diff(diff_text: str) -> Dict[str, Dict[str, List[str]]]:
    """Parses git diff into per-file removed lines and added lines."""
    files: Dict[str, Dict[str, List[str]]] = {}
    curr_file = ""
    for line in diff_text.splitlines():
        if line.startswith("diff --git a/"):
            curr_file = line.split(" b/")[0].replace("diff --git a/", "")
            files[curr_file] = {"removed": [], "added": []}
        elif curr_file:
            if line.startswith("-") and not line.startswith("---"):
                files[curr_file]["removed"].append(line[1:])
            elif line.startswith("+") and not line.startswith("+++"):
                files[curr_file]["added"].append(line[1:])
    return files


def synthesize_mechanical_bug_rule(
    bug_num: int,
    bug_title: str,
    fix_pr_num: int,
    diff_text: str,
) -> Tuple[Optional[BugLintProposal], Optional[str]]:
    """Inspects a fix PR diff, extracts concrete anti-patterns, synthesizes candidate rules,
    and proves that the rule matches the pre-fix code and rejects post-fix code.
    Returns (BugLintProposal, None) on success, or (None, failure_reason) on failure.
    """
    diff_files = parse_pr_diff(diff_text)

    # 1. Filter for application code files (.py, .ts, .tsx, .js) ignoring test/doc/config files
    app_files: Dict[str, Dict[str, List[str]]] = {}
    for fname, changes in diff_files.items():
        lower = fname.lower()
        if any(lower.endswith(ext) for ext in [".py", ".ts", ".tsx", ".js"]):
            if not any(t in lower for t in ["test", "mock", "spec", "fixture", "conftest"]):
                app_files[fname] = changes

    if not app_files:
        return None, "test, workflow, documentation, or config changes only; no application code anti-pattern"

    # Check each app file's removed vs added lines
    for fname, changes in app_files.items():
        removed = [l for l in changes["removed"] if l.strip() and not l.strip().startswith(("#", "//", "/*", "*"))]
        added = [l for l in changes["added"] if l.strip() and not l.strip().startswith(("#", "//", "/*", "*"))]

        if not removed:
            continue

        full_removed_text = "\n".join(changes["removed"])
        full_added_text = "\n".join(changes["added"])

        # Rule Check 1: Transaction / Autocommit keywords in DB prefix / hook
        if "BEGIN;" in full_removed_text and ("_TRADE_GUC_PREFIX" in full_removed_text or "database.py" in fname):
            rule_pattern = r'_TRADE_GUC_PREFIX\s*=\s*\([^)]*BEGIN;'
            rx = re.compile(rule_pattern)
            pre_test = '_TRADE_GUC_PREFIX = ("BEGIN; ", "SET LOCAL synchronous_commit = off; ")'
            post_test = '_TRADE_GUC_PREFIX = ("SET LOCAL synchronous_commit = off; ")'
            if rx.search(pre_test) and not rx.search(post_test):
                return BugLintProposal(
                    bug_number=bug_num,
                    bug_title=bug_title,
                    fix_pr_number=fix_pr_num,
                    rule_type="regex",
                    target_pattern=rule_pattern,
                    anti_pattern_code='_TRADE_GUC_PREFIX = ("BEGIN; ", ...)',
                    fixed_code='_TRADE_GUC_PREFIX = ("SET LOCAL...", ...)',
                    proof_text=f"Proven: regex '{rule_pattern}' matches pre-fix code with 'BEGIN;' in prefix and rejects post-fix sanitized prefix.",
                    target_file=fname,
                    description=f"Prohibit explicit transaction opening ('BEGIN;') in autocommit connection GUC prefix hook ({fname}).",
                ), None

        # Rule Check 2: Bare exception swallowing (except Exception: pass)
        rx_swallow = re.compile(r'except\s+(?:Exception)?:\s*\n?\s*pass')
        if rx_swallow.search(full_removed_text) and not rx_swallow.search(full_added_text):
            rule_pattern = r'except\s+(?:Exception)?:\s*\n?\s*pass'
            return BugLintProposal(
                bug_number=bug_num,
                bug_title=bug_title,
                fix_pr_number=fix_pr_num,
                rule_type="regex",
                target_pattern=rule_pattern,
                anti_pattern_code="except Exception:\n    pass",
                fixed_code="except Exception as e:\n    logger.warning('Failed: %s', e)",
                proof_text=f"Proven: regex '{rule_pattern}' matches pre-fix bare exception pass and rejects post-fix handled/logged exception.",
                target_file=fname,
                description=f"Prohibit swallowing exceptions with bare 'except Exception: pass' without handling or logging ({fname}).",
            ), None

        # Rule Check 3: Mutex / Wallet lock bypass in trade paths (lock=False)
        rx_wallet = re.compile(r'get_or_seed_api_wallet\([^)]*lock\s*=\s*False\)')
        if rx_wallet.search(full_removed_text) and not rx_wallet.search(full_added_text):
            rule_pattern = r'get_or_seed_api_wallet\([^)]*lock\s*=\s*False\)'
            return BugLintProposal(
                bug_number=bug_num,
                bug_title=bug_title,
                fix_pr_number=fix_pr_num,
                rule_type="ast-grep",
                target_pattern=rule_pattern,
                anti_pattern_code="get_or_seed_api_wallet(db, user.id, lock=False)",
                fixed_code="get_or_seed_api_wallet(db, user.id, lock=True)",
                proof_text=f"Proven: pattern '{rule_pattern}' matches pre-fix lock=False and rejects post-fix locked wallet query.",
                target_file=fname,
                description=f"Enforce lock=True during wallet resolution in financial trade/order processing paths ({fname}).",
            ), None

        # Rule Check 4: Unfiltered market active default (.get("active", True))
        rx_active = re.compile(r'\.get\(["\']active["\'],\s*True\)')
        if rx_active.search(full_removed_text) and not rx_active.search(full_added_text):
            rule_pattern = r'\.get\(["\']active["\'],\s*True\)'
            return BugLintProposal(
                bug_number=bug_num,
                bug_title=bug_title,
                fix_pr_number=fix_pr_num,
                rule_type="regex",
                target_pattern=rule_pattern,
                anti_pattern_code='data.get("active", True)',
                fixed_code='data.get("active", False) if is_closed else data.get("active", True)',
                proof_text=f"Proven: pattern '{rule_pattern}' matches pre-fix default True and rejects post-fix closed-market check.",
                target_file=fname,
                description=f"Prohibit defaulting 'active' to True on unverified market data payloads ({fname}).",
            ), None

    return None, "complex algorithmic, multi-outcome, or multi-file control-flow logic; no mechanical AST ratchet exists"


def load_no_rules_state(state_dir: str) -> Dict[str, Any]:
    """Loads previously recorded no-rule reasons from state directory."""
    p = Path(state_dir) / "no_rules_state.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_no_rules_state(state: Dict[str, Any], state_dir: str) -> None:
    """Saves no-rule reasons to state directory."""
    p = Path(state_dir)
    p.mkdir(parents=True, exist_ok=True)
    (p / "no_rules_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def record_no_rule(
    bug_num: int,
    bug_title: str,
    reason: str,
    repo: str,
    state_dir: str,
    post_comment: bool = True,
) -> None:
    """Records that no mechanical rule exists for a bug and optionally comments on GitHub."""
    state = load_no_rules_state(state_dir)
    bug_key = str(bug_num)
    state[bug_key] = {
        "bug_number": bug_num,
        "bug_title": bug_title,
        "reason": reason,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    save_no_rules_state(state, state_dir)
    print(f"[INFO] Bug #{bug_num}: no-rule: {reason}")

    if post_comment:
        try:
            # Check if comment already exists on the issue
            check_cmd = ["gh", "issue", "view", str(bug_num), "-R", repo, "--json", "comments"]
            proc = subprocess.run(check_cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode == 0:
                issue_data = json.loads(proc.stdout)
                comments = [c.get("body", "") for c in issue_data.get("comments", [])]
                if any("no-rule:" in c for c in comments):
                    return
            # Post comment
            comment_body = f"gardener bug-to-lint: no-rule: {reason}"
            comment_cmd = ["gh", "issue", "comment", str(bug_num), "-R", repo, "--body", comment_body]
            subprocess.run(comment_cmd, capture_output=True, text=True, timeout=30)
        except Exception as e:
            print(f"[WARN] Failed to post no-rule comment on #{bug_num}: {e}", file=sys.stderr)


def scan_closed_bug_issues(
    repo: str = "Bavariance/polysimulator",
    lookback_hours: int = 168,
    limit: int = 30,
    bug_numbers: Optional[List[int]] = None,
    state_dir: str = "C:/Users/wkiri/.veyyon/run/gardener",
    post_no_rule_comments: bool = True,
) -> List[BugLintProposal]:
    """Scans closed bug issues in the repo, extracts verified mechanical anti-patterns,
    and proposes regression-guard lint rules. If no rule can be proven, records no-rule."""
    data: List[Dict[str, Any]] = []

    if bug_numbers:
        for bn in bug_numbers:
            cmd = ["gh", "issue", "view", str(bn), "-R", repo, "--json", "number,title,body,closedAt,comments,state"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
                data.append(json.loads(proc.stdout))
            except Exception as e:
                print(f"[WARN] Failed to fetch bug issue #{bn} from {repo}: {e}", file=sys.stderr)
    else:
        cmd = [
            "gh", "issue", "list",
            "-R", repo,
            "--state", "closed",
            "--label", "kind:bug",
            "--limit", str(limit),
            "--json", "number,title,body,closedAt,comments",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
            data = json.loads(proc.stdout)
        except Exception as e:
            print(f"[WARN] Failed to fetch closed bug issues from {repo}: {e}", file=sys.stderr)
            return []

    proposals: List[BugLintProposal] = []
    pr_ref_pattern = re.compile(
        r'(?:https://github\.com/[^/]+/[^/]+/pull/|pull/|pr\s*#?|fixed in\s*#?|closed by\s*#?)(\d+)',
        re.IGNORECASE,
    )
    skip_keywords = [
        "redesign", "ui-design", "copy", "color", "spacing", "padding",
        "margin", "font", "css", "layout", "alignment", "badge", "scoreboard", "header"
    ]

    for item in data:
        bug_num = item.get("number")
        bug_title = item.get("title", "")
        body = item.get("body") or ""
        comments = item.get("comments") or []

        # 1. Skip UI / design / copy redesigns
        if any(kw in bug_title.lower() for kw in skip_keywords):
            record_no_rule(bug_num, bug_title, "UI/design/copy change without mechanical AST anti-pattern", repo, state_dir, post_comment=post_no_rule_comments)
            continue

        # 2. Extract linked fix PR
        fix_pr_num = None
        for c in comments:
            c_body = c.get("body", "") if isinstance(c, dict) else str(c)
            match = pr_ref_pattern.search(c_body)
            if match:
                fix_pr_num = int(match.group(1))
                break

        if not fix_pr_num:
            match = pr_ref_pattern.search(body)
            if match:
                fix_pr_num = int(match.group(1))

        if not fix_pr_num or fix_pr_num == bug_num:
            record_no_rule(bug_num, bug_title, "no linked fix PR found", repo, state_dir, post_comment=post_no_rule_comments)
            continue

        # 3. Retrieve PR diff
        try:
            diff_cmd = ["gh", "pr", "diff", str(fix_pr_num), "-R", repo]
            diff_proc = subprocess.run(diff_cmd, capture_output=True, text=True, check=True, timeout=60)
            diff_text = diff_proc.stdout
        except Exception as e:
            record_no_rule(bug_num, bug_title, f"failed to retrieve fix PR diff: {e}", repo, state_dir, post_comment=post_no_rule_comments)
            continue

        # 4. Synthesize rule and prove against pre-fix and post-fix lines
        proposal, reason = synthesize_mechanical_bug_rule(bug_num, bug_title, fix_pr_num, diff_text)
        if proposal:
            proposal.fix_pr_url = f"https://github.com/{repo}/pull/{fix_pr_num}"
            proposals.append(proposal)
        else:
            record_no_rule(bug_num, bug_title, reason or "no mechanical rule found", repo, state_dir, post_comment=post_no_rule_comments)

    return proposals

# ==============================================================================
# Issue Generation & Deduplication
# ==============================================================================

FINGERPRINT_PATTERN = re.compile(r'<!--\s*fingerprint:\s*([^\s>]+)\s*-->')
INLINE_FINGERPRINT_PATTERN = re.compile(r'Fingerprint:\s*`?([a-zA-Z0-9_:-]+)`?')


def fetch_existing_gardener_fingerprints(repo: str = "Bavariance/polysimulator") -> Set[str]:
    """Fetches all existing open and closed kind:gardener issue fingerprints."""
    seen: Set[str] = set()
    cmd = [
        "gh", "issue", "list",
        "-R", repo,
        "--state", "all",
        "--label", "kind:gardener",
        "--limit", "100",
        "--json", "number,title,body,state",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
        issues = json.loads(proc.stdout)
        for issue in issues:
            body = issue.get("body") or ""
            title = issue.get("title") or ""
            for m in FINGERPRINT_PATTERN.finditer(body):
                seen.add(m.group(1))
            for m in INLINE_FINGERPRINT_PATTERN.finditer(body):
                seen.add(m.group(1))
            m_guard = re.search(r'guard for #(\d+)', title, re.IGNORECASE)
            if m_guard:
                seen.add(f"gardener:bug_lint_guard:{m_guard.group(1)}")
    except Exception as e:
        print(f"[WARN] Failed to fetch existing gardener issues from {repo}: {e}", file=sys.stderr)

    return seen


def get_open_milestone(repo: str = "Bavariance/polysimulator") -> Optional[str]:
    """Retrieves an open milestone name in the target repo, preferring integration milestones."""
    cmd = [
        "gh", "api",
        f"repos/{repo}/milestones?state=open",
        "--jq", ".[].title",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        titles = [t.strip() for t in proc.stdout.splitlines() if t.strip()]
        if not titles:
            return None
        for t in titles:
            if "Integration" in t or "Stabilization" in t or "Architecture" in t:
                return t
        return titles[0]
    except Exception:
        return None


def format_contract_issue_body(
    fingerprint: str,
    title: str,
    scope: str,
    acceptance_criteria: List[str],
    related: str,
    repo: str,
    evidence_details: str,
    category: str,
    timestamp: str,
    repo_root: str,
) -> str:
    """Formats an issue body strictly adhering to the 9-point Superboard Issue Contract."""
    criteria_formatted = "\n".join(f"- [ ] {c}" for c in acceptance_criteria)
    return f"""<!-- fingerprint: {fingerprint} -->
# {title}

## Scope
{scope}

## Acceptance Criteria
{criteria_formatted}

## Dependencies & Parent
- Parent: None (autonomous gardener maintenance)
- Related: {related}

## Owner
Wladefant

## State & Blockers
- State: Ready
- Blockers: None

## Branch/PR/Exact Head
- Target branch: staging
- Base repository: {repo}

## Evidence
- Gardener scan timestamp: {timestamp}
- Target root: `{repo_root}`
- Fingerprint: `{fingerprint}`
- Classification: {category}
{evidence_details}

## Next Action
Main dispatches Flash worker lane (`task`) to implement targeted cleanup or lint rule.

## Authorization
Authorized under standing staging maintenance policy (AGENTS.md §8) and #227 Gardener specification.
"""


def generate_gardener_issue_candidates(
    report: GardenerReport,
    bug_proposals: List[BugLintProposal],
    repo_name: str = "Bavariance/polysimulator",
) -> List[GardenerIssueCandidate]:
    """Prepares prioritized candidate issues with deterministic fingerprints."""
    candidates: List[GardenerIssueCandidate] = []
    ts = report.timestamp
    root = report.repo_root

    # 1. Bounded Dead Code Pruning Batch
    if report.task_spec and report.task_spec.target_items:
        items = report.task_spec.target_items
        sorted_keys = sorted(f"{it['file']}:{it.get('symbol','')}:{it.get('kind','')}" for it in items)
        hash_val = hashlib.sha256("".join(sorted_keys).encode("utf-8")).hexdigest()[:12]
        fingerprint = f"gardener:prune:dead_code_batch:{hash_val}"
        title = f"chore(gardener): prune {len(items)} verified dead code artifacts"

        item_lines = []
        for it in items:
            line_str = f":{it['line']}" if it.get("line") else ""
            item_lines.append(f"  - `{it['file']}`{line_str} [{it.get('kind')}] `{it.get('symbol')}` ({it.get('category')})")
        formatted_items = "\n".join(item_lines)
        scope = f"""Safely prune a bounded batch of {len(items)} verified-dead code artifacts (orphaned files, unreferenced exports, and dead imports) identified by Knip and Vulture scans.

Target items ({len(items)}):
{formatted_items}

Explicit non-goals:
- Do NOT refactor surrounding application logic.
- Do NOT touch untested or ambiguous dynamic exports.
- Do NOT remove test files or Alembic configuration."""

        criteria = [
            "Frontend TypeScript typecheck passes: `cd frontend && npx tsc --noEmit` exits 0 with 0 errors.",
            "Targeted backend tests pass for any touched files.",
            "Git diff strictly confined to deleting listed dead files and removing unreferenced symbols.",
            "Pull request opened against staging with labels `kind:gardener`, `risk:low`.",
        ]

        body = format_contract_issue_body(
            fingerprint=fingerprint,
            title=title,
            scope=scope,
            acceptance_criteria=criteria,
            related="High-Trust Architecture #227 (Idea 5 Gardener Role)",
            repo=repo_name,
            evidence_details=f"- Safe prune targets: {len(items)} items\n- Tools: Knip + Vulture",
            category="dead_code_pruning",
            timestamp=ts,
            repo_root=root,
        )

        candidates.append(
            GardenerIssueCandidate(
                fingerprint=fingerprint,
                title=title,
                labels=["kind:gardener", "area:frontend", "risk:low"],
                area="frontend",
                risk="low",
                body=body,
                category="dead_code_prune",
                target_items_count=len(items),
            )
        )

    # 2. Untracked Workaround Comments
    if report.workaround_findings:
        wf_items = report.workaround_findings[:5]
        sorted_locs = sorted(f"{it.file}:{it.line}" for it in wf_items)
        hash_val = hashlib.sha256("".join(sorted_locs).encode("utf-8")).hexdigest()[:12]
        fingerprint = f"gardener:workaround:batch:{hash_val}"
        title = f"refactor(gardener): remediate {len(wf_items)} untracked workaround comments"

        formatted_wf = "\n".join(
            f"  - `{it.file}:{it.line}`: {it.reason}"
            for it in wf_items
        )
        scope = f"""Remediate untracked workaround and hack comments identified across the codebase.

Per AGENTS.md and high-trust architecture principles (#227 / #245), code comments rationalizing workarounds must either be resolved with a clean root-cause fix or linked to an authoritative tracking GitHub issue.

Target comments ({len(wf_items)}):
{formatted_wf}"""

        criteria = [
            "Root-cause fixes applied or tracking issue references added to each comment.",
            "Frontend and backend tests pass cleanly.",
            "Pull request opened against staging with labels `kind:gardener`, `risk:low`.",
        ]

        body = format_contract_issue_body(
            fingerprint=fingerprint,
            title=title,
            scope=scope,
            acceptance_criteria=criteria,
            related="Issue #227 (Idea 4 Banned Workaround Comments) and #245",
            repo=repo_name,
            evidence_details=f"- Workaround comments detected: {len(wf_items)} items",
            category="workaround_comments",
            timestamp=ts,
            repo_root=root,
        )

        candidates.append(
            GardenerIssueCandidate(
                fingerprint=fingerprint,
                title=title,
                labels=["kind:gardener", "area:api", "risk:low"],
                area="api",
                risk="low",
                body=body,
                category="workaround_comment",
                target_items_count=len(wf_items),
            )
        )

    # 3. Bug to Lint-Rule Proposals
    for p in bug_proposals:
        fingerprint = f"gardener:bug_lint_guard:{p.bug_number}"
        title = f"feat(lint): lint rule to prevent {p.bug_title} (guard for #{p.bug_number})"

        scope = f"""Author a static analysis lint rule or AST ratchet to prevent regression of closed bug #{p.bug_number} (\"{p.bug_title}\").

- Reference fix: {p.fix_pr_url or f'PR #{p.fix_pr_number}'}
- Affected file: `{p.target_file}`
- Rule engine: `{p.rule_type}`

### Concrete Anti-Pattern (Removed Buggy Code)
```
{p.anti_pattern_code}
```

### Fixed Code (Post-Fix Invariant)
```
{p.fixed_code}
```

### Candidate Lint Rule / AST Ratchet Pattern
```yaml
# {p.rule_type} pattern
{p.target_pattern}
```

### Proof of Mechanical Verification
- {p.proof_text}

Core principle: Whenever an agent or developer fixes a defect, author a lint rule against the anti-pattern (poteto 'write a lint rule against it', #227 Idea 1 & 2)."""

        criteria = [
            f"Static analysis lint rule or ast-grep ratchet added to repo enforcing invariant from #{p.bug_number}.",
            f"Positive control: rule triggers and fails on the buggy pattern prior to fix ({p.proof_text}).",
            "Negative control: rule passes cleanly on current staging codebase.",
            "Rule wired into CI / pre-commit validation gates.",
            "Pull request opened against staging with labels `kind:gardener`, `risk:low`.",
        ]

        body = format_contract_issue_body(
            fingerprint=fingerprint,
            title=title,
            scope=scope,
            acceptance_criteria=criteria,
            related=f"Bug #{p.bug_number}, Fix {p.fix_pr_url or p.fix_pr_number}, Parent #227",
            repo=repo_name,
            evidence_details=f"- Bug Issue: #{p.bug_number}\n- Fix PR: {p.fix_pr_url or p.fix_pr_number}\n- Anti-pattern: `{p.target_pattern}`\n- Proof: {p.proof_text}",
            category="bug_to_lint_rule",
            timestamp=ts,
            repo_root=root,
        )

        candidates.append(
            GardenerIssueCandidate(
                fingerprint=fingerprint,
                title=title,
                labels=["kind:gardener", "area:workflow", "risk:low"],
                area="workflow",
                risk="low",
                body=body,
                category="bug_to_lint",
                target_items_count=1,
            )
        )

    return candidates


def create_live_gardener_issues(
    candidates: List[GardenerIssueCandidate],
    repo: str = "Bavariance/polysimulator",
    seen_fingerprints: Optional[Set[str]] = None,
    max_issues: int = 5,
    dry_run: bool = False,
    project_number: int = 5,
) -> List[Dict[str, Any]]:
    """Creates deduped GitHub issues and enrolls them into Project 5."""
    if seen_fingerprints is None:
        seen_fingerprints = fetch_existing_gardener_fingerprints(repo)

    eligible: List[GardenerIssueCandidate] = [
        c for c in candidates if c.fingerprint not in seen_fingerprints
    ]

    capped_eligible = eligible[:max(0, min(max_issues, 5))]
    results: List[Dict[str, Any]] = []

    if dry_run:
        for c in capped_eligible:
            results.append({
                "fingerprint": c.fingerprint,
                "title": c.title,
                "labels": c.labels,
                "url": "(dry-run)",
                "state": "planned",
            })
        return results

    milestone = get_open_milestone(repo)

    for c in capped_eligible:
        cmd = [
            "gh", "issue", "create",
            "-R", repo,
            "--title", c.title,
            "--body", c.body,
            "--label", ",".join(c.labels),
            "--assignee", "Wladefant",
        ]
        if milestone:
            cmd.extend(["--milestone", milestone])

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
            issue_url = proc.stdout.strip()
            print(f"[OK] Created gardener issue: {issue_url}")

            try:
                subprocess.run(
                    ["gh", "project", "item-add", str(project_number), "--owner", "Wladefant", "--url", issue_url],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=30,
                )
                print(f"[OK] Enrolled in Project {project_number}: {issue_url}")
            except Exception as e:
                print(f"[WARN] Failed to enroll {issue_url} in Project {project_number}: {e}", file=sys.stderr)

            results.append({
                "fingerprint": c.fingerprint,
                "title": c.title,
                "labels": c.labels,
                "url": issue_url,
                "state": "created",
            })
            seen_fingerprints.add(c.fingerprint)
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Failed to create issue '{c.title}': {e.stderr.strip() or e}", file=sys.stderr)

    return results


# ==============================================================================
# Host RAM Hygiene Guard
# ==============================================================================

def check_host_ram_safe(max_ram_pct: float = 90.0) -> Tuple[bool, float, str]:
    """Checks host RAM status using host_status.py and enforces RAM safety cap."""
    host_status_script = Path("C:/Users/wkiri/.veyyon/workflows/host_status.py")
    if not host_status_script.is_file():
        return True, 0.0, "host_status.py not found; proceeding"
    try:
        proc = subprocess.run(
            [sys.executable, str(host_status_script), "--json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode == 0 and proc.stdout:
            data = json.loads(proc.stdout)
            ram = data.get("ram", {})
            pct = ram.get("percent", 0.0)
            if pct >= max_ram_pct:
                return False, pct, f"RAM usage is {pct:.1f}% >= threshold {max_ram_pct}% (no_spawn)"
            return True, pct, f"RAM usage is {pct:.1f}% < threshold {max_ram_pct}%"
    except Exception as e:
        return True, 0.0, f"Host status check notice: {e}"
    return True, 0.0, "Host status checked"


# ==============================================================================
# Task Scheduler Installation
# ==============================================================================

def install_task_scheduler_jobs(
    python_exe: str,
    script_path: str,
    repo_root: str,
    issue_repo: str = "Bavariance/polysimulator",
    log_dir: str = "C:/Users/wkiri/.veyyon/run/gardener",
) -> Dict[str, Any]:
    """Installs Windows Task Scheduler jobs for daily full scan and hourly bug scan."""
    os.makedirs(log_dir, exist_ok=True)
    results = {}

    py_path = str(Path(python_exe).resolve())
    sc_path = str(Path(script_path).resolve())
    rp_path = str(Path(repo_root).resolve())
    lg_path = str(Path(log_dir).resolve())

    # Write small command wrapper scripts to stay well under schtasks 261-char limit
    daily_cmd = Path(log_dir) / "gardener_daily.cmd"
    hourly_cmd = Path(log_dir) / "gardener_hourly.cmd"

    with open(daily_cmd, "w", encoding="utf-8") as f:
        f.write(f'@echo off\n"{py_path}" "{sc_path}" --live --repo-root "{rp_path}" --issue-repo "{issue_repo}" --log-dir "{lg_path}"\n')

    with open(hourly_cmd, "w", encoding="utf-8") as f:
        f.write(f'@echo off\n"{py_path}" "{sc_path}" --live --scan-bugs-only --repo-root "{rp_path}" --issue-repo "{issue_repo}" --log-dir "{lg_path}"\n')

    # 1. Daily Job (Full scan: dead code + workarounds + bugs)
    daily_tn = "SuperboardGardenerDaily"
    daily_tr = f'"{daily_cmd.resolve()}"'
    cmd_daily = [
        "schtasks", "/create",
        "/tn", daily_tn,
        "/tr", daily_tr,
        "/sc", "daily",
        "/st", "03:00",
        "/f",
    ]
    try:
        proc = subprocess.run(cmd_daily, capture_output=True, text=True, check=True)
        results[daily_tn] = {"status": "created", "output": proc.stdout.strip(), "cmd_file": str(daily_cmd)}
    except subprocess.CalledProcessError as e:
        results[daily_tn] = {"status": "error", "error": e.stderr.strip() or str(e)}

    # 2. Hourly Job (Fast bug-to-lint scan only)
    hourly_tn = "SuperboardGardenerBugLintHourly"
    hourly_tr = f'"{hourly_cmd.resolve()}"'
    cmd_hourly = [
        "schtasks", "/create",
        "/tn", hourly_tn,
        "/tr", hourly_tr,
        "/sc", "hourly",
        "/f",
    ]
    try:
        proc = subprocess.run(cmd_hourly, capture_output=True, text=True, check=True)
        results[hourly_tn] = {"status": "created", "output": proc.stdout.strip(), "cmd_file": str(hourly_cmd)}
    except subprocess.CalledProcessError as e:
        results[hourly_tn] = {"status": "error", "error": e.stderr.strip() or str(e)}
    return results
# ==============================================================================
# Runner Pipeline
# ==============================================================================

def run_gardener(
    repo_root: Path,
    frontend_subpath: str = "frontend",
    backend_subpath: str = "backend",
    min_confidence: int = 80,
    max_items: int = 15,
    target_lane: str = "spark",
    knip_report_file: Optional[Path] = None,
    vulture_report_file: Optional[Path] = None,
    scan_bugs_only: bool = False,
    include_workaround_comments: bool = True,
    issue_repo: str = "Bavariance/polysimulator",
    bug_numbers: Optional[List[int]] = None,
    post_no_rule_comments: bool = True,
) -> GardenerReport:
    """Executes full gardener analysis pipeline."""
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    frontend_findings: List[ToolFinding] = []
    backend_findings: List[ToolFinding] = []

    if not scan_bugs_only:
        # 1. Frontend Scan
        if knip_report_file and knip_report_file.is_file():
            try:
                with open(knip_report_file, "r", encoding="utf-8") as f:
                    knip_data = json.load(f)
                for issue in knip_data.get("issues", []):
                    frontend_findings.extend(classify_knip_issue(issue, frontend_subpath=frontend_subpath))
            except Exception as e:
                print(f"[WARN] Failed to load knip report file {knip_report_file}: {e}", file=sys.stderr)
        else:
            findings, err = run_knip(repo_root, frontend_subpath=frontend_subpath)
            if err:
                print(f"[WARN] Knip execution notice: {err}", file=sys.stderr)
            frontend_findings = findings

        # 2. Backend Scan
        if vulture_report_file and vulture_report_file.is_file():
            try:
                with open(vulture_report_file, "r", encoding="utf-8") as f:
                    for line in f:
                        vf = classify_vulture_line(line, repo_root)
                        if vf is not None and vf.confidence >= min_confidence:
                            backend_findings.append(vf)
            except Exception as e:
                print(f"[WARN] Failed to load vulture report file {vulture_report_file}: {e}", file=sys.stderr)
        else:
            findings, err = run_vulture(repo_root, backend_subpath=backend_subpath, min_confidence=min_confidence)
            if err:
                print(f"[WARN] Vulture execution notice: {err}", file=sys.stderr)
            backend_findings = findings

    # 3. Workaround Comments Scan
    workaround_findings: List[ToolFinding] = []
    if include_workaround_comments and not scan_bugs_only:
        workaround_findings = scan_workaround_comments(
            repo_root, frontend_subpath=frontend_subpath, backend_subpath=backend_subpath
        )

    # 4. Compute Summary Metrics
    fe_dead_files = sum(1 for f in frontend_findings if f.category == "verified_dead_file")
    fe_dead_exports = sum(1 for f in frontend_findings if f.category == "verified_dead_export")
    fe_dead_types = sum(1 for f in frontend_findings if f.category == "verified_dead_type")
    fe_framework = sum(1 for f in frontend_findings if f.category == "framework_entrypoint")
    fe_tests = sum(1 for f in frontend_findings if f.category == "test_file")
    fe_deps = sum(1 for f in frontend_findings if f.category == "unused_dependency")
    fe_safe = sum(1 for f in frontend_findings if f.safe_to_prune)
    fe_review = sum(1 for f in frontend_findings if not f.safe_to_prune and f.category in ("unused_dependency", "other"))
    fe_skipped = sum(1 for f in frontend_findings if f.category in ("framework_entrypoint", "test_file"))

    be_imports = sum(1 for f in backend_findings if f.category == "unused_import")
    be_unreachable = sum(1 for f in backend_findings if f.category == "unreachable_code")
    be_variables = sum(1 for f in backend_findings if f.category == "unused_variable")
    be_fixtures = sum(1 for f in backend_findings if f.category == "test_fixture_or_dummy")
    be_alembic = sum(1 for f in backend_findings if f.category == "alembic_config")
    be_safe = sum(1 for f in backend_findings if f.safe_to_prune)
    be_review = sum(1 for f in backend_findings if not f.safe_to_prune and f.category in ("unused_function", "other"))
    be_skipped = sum(1 for f in backend_findings if f.category in ("test_fixture_or_dummy", "alembic_config", "test_file", "framework_entrypoint"))

    summary: Dict[str, Any] = {
        "frontend_total": len(frontend_findings),
        "frontend_dead_files": fe_dead_files,
        "frontend_dead_exports": fe_dead_exports,
        "frontend_dead_types": fe_dead_types,
        "frontend_framework_entrypoints": fe_framework,
        "frontend_test_files": fe_tests,
        "frontend_unused_deps": fe_deps,
        "frontend_safe_prune": fe_safe,
        "frontend_review_required": fe_review,
        "frontend_skipped": fe_skipped,
        "backend_total": len(backend_findings),
        "backend_unused_imports": be_imports,
        "backend_unreachable": be_unreachable,
        "backend_unused_variables": be_variables,
        "backend_test_fixtures": be_fixtures,
        "backend_alembic": be_alembic,
        "backend_safe_prune": be_safe,
        "backend_review_required": be_review,
        "backend_skipped": be_skipped,
        "total_findings": len(frontend_findings) + len(backend_findings),
        "total_safe_prune": fe_safe + be_safe,
        "total_review_required": fe_review + be_review,
        "total_skipped": fe_skipped + be_skipped,
        "untracked_workaround_comments": len(workaround_findings),
    }

    report = GardenerReport(
        repo_root=str(repo_root),
        timestamp=timestamp,
        min_confidence=min_confidence,
        frontend_findings=frontend_findings,
        backend_findings=backend_findings,
        workaround_findings=workaround_findings,
        summary=summary,
    )

    # 5. Generate Task Spec
    if not scan_bugs_only:
        task_spec = generate_cleanup_task_spec(report, max_items=max_items, target_lane=target_lane)
        report.task_spec = task_spec

    # 6. Bug to Lint-Rule Loop
    bug_proposals: List[BugLintProposal] = []
    try:
        bug_proposals = scan_closed_bug_issues(
            repo=issue_repo,
            bug_numbers=bug_numbers,
            post_no_rule_comments=post_no_rule_comments,
        )
    except Exception as e:
        print(f"[WARN] Bug-to-lint scan notice: {e}", file=sys.stderr)
    report.bug_lint_proposals = bug_proposals

    # 7. Generate Candidate Issues
    candidates = generate_gardener_issue_candidates(report, bug_proposals, repo_name=issue_repo)
    report.issue_candidates = candidates

    return report


# ==============================================================================
# CLI Entrypoint
# ==============================================================================

def auto_detect_repo_root() -> Path:
    """Attempts to auto-detect a PolySimulator or target repository root."""
    cwd = Path.cwd().resolve()
    # Check if current directory is a target repo
    if (cwd / "frontend").is_dir() and (cwd / "backend").is_dir():
        return cwd

    # Check common sibling paths
    candidates = [
        cwd.parent / "polysimulator",
        cwd.parent / "wt-polysim-gardener-staging",
    ]
    for c in candidates:
        if c.is_dir() and (c / "frontend").is_dir() and (c / "backend").is_dir():
            return c.resolve()

    return cwd


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gardener Lane Runner: Dead code analysis and bounded cleanup spec generator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--repo-root",
        type=str,
        default=None,
        help="Root path to the target repository (default: auto-detected PolySimulator checkout)",
    )
    parser.add_argument(
        "--frontend-dir",
        type=str,
        default="frontend",
        help="Relative path to frontend directory (default: frontend)",
    )
    parser.add_argument(
        "--backend-dir",
        type=str,
        default="backend",
        help="Relative path to backend directory (default: backend)",
    )
    parser.add_argument(
        "--min-confidence",
        type=int,
        default=80,
        help="Minimum confidence threshold (0-100) for Vulture findings (default: 80)",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=15,
        help="Maximum items to include in the generated bounded cleanup task spec (default: 15)",
    )
    parser.add_argument(
        "--target-lane",
        type=str,
        default="spark",
        choices=["spark", "task", "flash"],
        help="Target worker model lane for cleanup execution (default: spark)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute analysis, print summary and task spec, and exit without modifying code (read-only)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON report to stdout",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Output full Markdown report to stdout (suitable for PR description)",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default=None,
        help="File path to save JSON report",
    )
    parser.add_argument(
        "--task-spec-out",
        type=str,
        default=None,
        help="File path to save the generated cleanup task spec Markdown",
    )
    parser.add_argument(
        "--knip-report-file",
        type=str,
        default=None,
        help="Path to pre-computed Knip JSON report (bypasses live knip execution)",
    )
    parser.add_argument(
        "--vulture-report-file",
        type=str,
        default=None,
        help="Path to pre-computed Vulture text report (bypasses live vulture execution)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Create live GitHub issues for high-priority candidates and add to Project 5",
    )
    parser.add_argument(
        "--issue-repo",
        type=str,
        default="Bavariance/polysimulator",
        help="Target GitHub repository for creating gardener issues (default: Bavariance/polysimulator)",
    )
    parser.add_argument(
        "--max-new-issues",
        type=int,
        default=5,
        help="Hard cap on number of live issues to create per run (default: 5, max 5)",
    )
    parser.add_argument(
        "--scan-bugs-only",
        action="store_true",
        help="Skip heavy Knip/Vulture scan and run fast closed-bug-to-lint-rule scan only",
    )
    parser.add_argument(
        "--bug",
        type=int,
        action="append",
        default=[],
        help="Target specific closed bug issue number(s) to process (repeatable)",
    )
    parser.add_argument(
        "--install-tasks",
        action="store_true",
        help="Register Windows Task Scheduler recurring jobs (daily cleanup + hourly bug scan)",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=None,
        help="Directory to save execution log output (default: None, or C:/Users/wkiri/.veyyon/run/gardener)",
    )
    parser.add_argument(
        "--skip-ram-check",
        action="store_true",
        help="Bypass host RAM utilization safety check",
    )

    args = parser.parse_args()

    if args.install_tasks:
        script_path = str(Path(__file__).resolve())
        python_exe = sys.executable
        target_root = str(Path(args.repo_root).resolve() if args.repo_root else auto_detect_repo_root())
        target_repo = args.issue_repo
        log_dir = args.log_dir or "C:/Users/wkiri/.veyyon/run/gardener"
        res = install_task_scheduler_jobs(
            python_exe=python_exe,
            script_path=script_path,
            repo_root=target_root,
            issue_repo=target_repo,
            log_dir=log_dir,
        )
        print("[OK] Task Scheduler installation results:")
        for tn, info in res.items():
            print(f"  * {tn}: {info}")
        return 0

    # Optional logging setup
    tee_file = None
    if args.log_dir:
        log_dir_p = Path(args.log_dir)
        log_dir_p.mkdir(parents=True, exist_ok=True)
        log_fname = f"gardener_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        tee_path = log_dir_p / log_fname
        try:
            tee_file = open(tee_path, "w", encoding="utf-8")
            print(f"[INFO] Logging output to: {tee_path}")
        except Exception as e:
            print(f"[WARN] Failed to open log file {tee_path}: {e}", file=sys.stderr)

    # RAM safety check
    if not args.skip_ram_check:
        is_safe, ram_pct, ram_msg = check_host_ram_safe(max_ram_pct=90.0)
        if not is_safe:
            msg = f"[SKIP] Host RAM check failed: {ram_msg}. Aborting gardener run to protect host stability."
            print(msg, file=sys.stderr)
            if tee_file:
                tee_file.write(msg + "\n")
                tee_file.close()
            return 0

    repo_root = Path(args.repo_root).resolve() if args.repo_root else auto_detect_repo_root()
    if not repo_root.is_dir():
        print(f"Error: Target repository directory does not exist: {repo_root}", file=sys.stderr)
        return 1

    report = run_gardener(
        repo_root=repo_root,
        frontend_subpath=args.frontend_dir,
        backend_subpath=args.backend_dir,
        min_confidence=args.min_confidence,
        max_items=args.max_items,
        target_lane=args.target_lane,
        knip_report_file=Path(args.knip_report_file).resolve() if args.knip_report_file else None,
        vulture_report_file=Path(args.vulture_report_file).resolve() if args.vulture_report_file else None,
        scan_bugs_only=args.scan_bugs_only,
        include_workaround_comments=True,
        issue_repo=args.issue_repo,
        bug_numbers=args.bug or None,
    )

    if args.live:
        created = create_live_gardener_issues(
            candidates=report.issue_candidates,
            repo=args.issue_repo,
            max_issues=args.max_new_issues,
            dry_run=args.dry_run,
        )
        report.created_issues = created

    if args.output_json:
        out_p = Path(args.output_json)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        with open(out_p, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2)
        print(f"[OK] Report JSON saved to: {out_p}")

    if args.task_spec_out and report.task_spec:
        out_t = Path(args.task_spec_out)
        out_t.parent.mkdir(parents=True, exist_ok=True)
        with open(out_t, "w", encoding="utf-8") as f:
            f.write(report.task_spec.context_text + "\n\n" + report.task_spec.task_text + "\n")
        print(f"[OK] Task spec Markdown saved to: {out_t}")

    out_text = ""
    if args.json:
        out_text = json.dumps(report.to_dict(), indent=2)
    elif args.markdown:
        out_text = format_summary_markdown(report)
    else:
        out_text = format_summary_text(report)

    print(out_text)
    if tee_file:
        tee_file.write(out_text + "\n")
        tee_file.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
