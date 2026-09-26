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
class GardenerReport:
    """Complete summary of a Gardener scan and classification run."""
    repo_root: str
    timestamp: str
    min_confidence: int
    frontend_findings: List[ToolFinding] = field(default_factory=list)
    backend_findings: List[ToolFinding] = field(default_factory=list)
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
) -> GardenerReport:
    """Executes full gardener analysis pipeline."""
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. Frontend Scan
    frontend_findings: List[ToolFinding] = []
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
    backend_findings: List[ToolFinding] = []
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

    # 3. Compute Summary Metrics
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
    }

    report = GardenerReport(
        repo_root=str(repo_root),
        timestamp=timestamp,
        min_confidence=min_confidence,
        frontend_findings=frontend_findings,
        backend_findings=backend_findings,
        summary=summary,
    )

    # 4. Generate Task Spec
    task_spec = generate_cleanup_task_spec(report, max_items=max_items, target_lane=target_lane)
    report.task_spec = task_spec

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

    args = parser.parse_args()

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
    )

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

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    elif args.markdown:
        print(format_summary_markdown(report))
    else:
        print(format_summary_text(report))

    return 0


if __name__ == "__main__":
    sys.exit(main())
