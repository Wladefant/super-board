#!/usr/bin/env python3
"""
GitHub-Native Plan & PR Recap Templates (~/.veyyon/workflows/github_plan_templates.py)

Provides structured, token-efficient, GitHub-native issue and PR review templates
adapted from Agent-Native Plan/Recap concepts without requiring external services
or running daemons.

Core Capabilities:
  - Structured canonical plan per issue with managed section markers
  - Dedicated Connected-Service Preflight Gate
  - Architecture & workflow diagrams using GitHub-native Mermaid syntax
  - Asynchronous human decision contracts (aligns with decisions.json & ledger.py)
  - Execution checklists with stable criteria IDs
  - Risk cards with severity tiers and mitigation strategies
  - Changed files & annotated structure walkthrough
  - Before vs After state comparison
  - QA evidence gates (head-bound, 1920px/320px viewports, sandbox transactions)
  - Sticky PR recap templates with before/after and verification proof
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Section Marker Delimiters for Managed Sections
SECTION_BEGIN_FMT = "<!-- github-plan:begin:{section_id} -->"
SECTION_END_FMT = "<!-- github-plan:end:{section_id} -->"


@dataclass
class PreflightGate:
    """Preflight check and connected-service evidence gate."""
    environment: str = "staging"  # staging, local; strictly never production
    target_branch: str = "staging"
    supabase_project_ref: str = "hgzyqmaanndcimnclxtv"  # Staging ref
    auth_db_status: str = "healthy"
    github_cli_status: str = "authenticated"
    decision_blockers: List[str] = field(default_factory=list)
    production_guard_status: str = "enforced"  # zaraprptkegxqpvnsubu blocked
    notes: Optional[str] = None

    def render(self) -> str:
        status_icon = "🟢 PASS" if not self.decision_blockers else "🟡 BLOCKED (Awaiting Decision)"
        blockers_str = ", ".join(self.decision_blockers) if self.decision_blockers else "None (clear to proceed)"
        
        md = [
            f"### 🛡️ Preflight & Connected-Service Gate [{status_icon}]",
            "",
            "| Gate / Service | Target / Value | Status | Invariant / Policy |",
            "| :--- | :--- | :--- | :--- |",
            f"| **Target Environment** | `{self.environment}` | ✅ Enforced | Production access strictly prohibited |",
            f"| **Branch Route** | `{self.target_branch}` | ✅ Validated | PR target must be staging trunk |",
            f"| **Supabase Project** | `{self.supabase_project_ref}` (staging) | ✅ Isolated | Prod ref `zaraprptkegxqpvnsubu` blocked |",
            f"| **GitHub CLI / API** | `gh` CLI token | ✅ Authenticated | Project #1 & issue workflow access |",
            f"| **Decision Gate** | `{blockers_str}` | {'⚠️ Blocked' if self.decision_blockers else '✅ Clear'} | Operator decision required before forward action |",
            f"| **Safety Guardrail** | No destructive DDL / No main merge | ✅ Enforced | Automated bypass prohibited |",
        ]
        if self.notes:
            md.extend(["", f"> **Preflight Note:** {self.notes}"])
        return "\n".join(md)


@dataclass
class ArchitectureDiagram:
    """GitHub-native Mermaid workflow/architecture diagram."""
    title: str
    diagram_type: str = "workflow"  # workflow, sequence, class
    mermaid_code: str = ""
    archify_asset_path: Optional[str] = None
    description: Optional[str] = None

    def render(self) -> str:
        md = [
            f"### 🏛️ Architecture & Workflow: {self.title}",
            "",
        ]
        if self.description:
            md.append(f"{self.description}\n")
        
        if self.mermaid_code:
            md.extend([
                "```mermaid",
                self.mermaid_code.strip(),
                "```",
            ])
        
        if self.archify_asset_path:
            md.extend([
                "",
                f"*Canonical Archify Asset Specification:* `{self.archify_asset_path}`",
                "*(Rendered via `scripts/archify.mjs deliver` with schema_version 2 showcase standards)*",
            ])
        return "\n".join(md)


@dataclass
class PlanDecision:
    """Asynchronous human decision contract entry."""
    decision_id: str
    question: str
    options: List[Dict[str, str]]  # id, label, tradeoffs
    recommendation: str
    authorized_responders: List[str]
    status: str = "pending"  # pending, answered, rejected, clarification_requested
    answer: Optional[str] = None
    provenance_notice: Optional[str] = None
    blocking_tasks: List[str] = field(default_factory=list)

    def render(self) -> str:
        status_badge = "🟡 PENDING OPERATOR CHOICE" if self.status == "pending" else f"🟢 RESOLVED ({self.status})"
        responders = ", ".join([f"@{r}" if not r.startswith("@") else r for r in self.authorized_responders])
        
        md = [
            f"#### ❓ Decision Contract: `{self.decision_id}` [{status_badge}]",
            "",
            f"**Question:** {self.question}",
            "",
            "| Option | Proposal | Tradeoffs |",
            "| :--- | :--- | :--- |",
        ]
        for opt in self.options:
            opt_id = opt.get("id", "")
            label = opt.get("label", "")
            tradeoffs = opt.get("tradeoffs", "")
            md.append(f"| **Option {opt_id}** | {label} | {tradeoffs} |")
        
        if self.status == "retired_synthetic":
            status_badge = "⚪ RETIRED SYNTHETIC DEMO"
        elif self.status == "pending":
            status_badge = "🟡 PENDING OPERATOR CHOICE"
        else:
            status_badge = f"🟢 RESOLVED ({self.status})"

        md.extend([
            "",
            f"👉 **Recommendation:** {self.recommendation}",
            f"🔒 **Authorized Responder(s):** {responders}",
        ])
        
        if self.blocking_tasks:
            md.append(f"⛔ **Blocking Tasks:** {', '.join(self.blocking_tasks)}")
        
        if self.provenance_notice:
            md.extend([
                "",
                f"> ⚠️ **Provenance & Integrity Guard:** {self.provenance_notice}",
            ])
        
        if self.status == "pending":
            md.extend([
                "",
                "**How to Respond:**",
                f"- Plain reply: Post comment with `Option A`, `Option B`, or `Recommendation`.",
                f"- Form reply:",
                "```markdown",
                f"<!-- decision-form: {self.decision_id} -->",
                "choice: Option A",
                "notes: <rationale>",
                "<!-- /decision-form -->",
                "```",
                "> *Note: Autonomous agents are prohibited from auto-answering human decisions.*",
            ])
        else:
            md.extend([
                "",
                f"**Resolved State:** `{self.answer}`",
            ])
        return "\n".join(md)


@dataclass
class ChecklistItem:
    """Execution checklist item with stable ID."""
    item_id: str
    label: str
    completed: bool = False
    evidence: Optional[str] = None
    criterion_ref: Optional[str] = None  # e.g. AC-1

    def render(self) -> str:
        mark = "x" if self.completed else " "
        crit = f" `[{self.criterion_ref}]`" if self.criterion_ref else ""
        item_text = f"- [{mark}] **{self.item_id}**{crit}: {self.label}"
        if self.evidence:
            item_text += f" *(Evidence: {self.evidence})*"
        return item_text


@dataclass
class RiskCard:
    """Risk card adapted from Agent-Native RiskCard block."""
    risk_id: str
    severity: str  # low, medium, high, critical
    description: str
    mitigation: str

    def render(self) -> str:
        icons = {
            "low": "🔵 LOW",
            "medium": "🟡 MEDIUM",
            "high": "🟠 HIGH",
            "critical": "🔴 CRITICAL",
        }
        sev_label = icons.get(self.severity.lower(), self.severity.upper())
        return (
            f"| `{self.risk_id}` | **{sev_label}** | {self.description} | {self.mitigation} |"
        )


@dataclass
class ChangedFile:
    """Changed or planned file entry with rationale."""
    path: str
    change_type: str  # create, modify, delete
    rationale: str
    annotation: Optional[str] = None

    def render(self) -> str:
        badge = {"create": "✨ ADD", "modify": "✏️ EDIT", "delete": "🗑️ REMOVE"}.get(self.change_type, self.change_type)
        line = f"| `{self.path}` | **{badge}** | {self.rationale} |"
        if self.annotation:
            line += f" *({self.annotation})* |"
        return line


@dataclass
class BeforeAfterState:
    """Before vs After comparison block."""
    aspect: str
    before_state: str
    after_state: str
    impact: str

    def render(self) -> str:
        return f"| **{self.aspect}** | {self.before_state} | {self.after_state} | {self.impact} |"


@dataclass
class QaEvidenceGate:
    """QA evidence gate specification."""
    head_sha: str
    desktop_viewport_verified: bool = False
    mobile_viewport_verified: bool = False
    sandbox_money_path_verified: bool = False
    desktop_asset_url: Optional[str] = None
    mobile_asset_url: Optional[str] = None
    transaction_proof: Optional[str] = None
    automated_test_summary: Optional[str] = None

    def render(self) -> str:
        d_mark = "✅ VERIFIED" if self.desktop_viewport_verified else "⏳ PENDING LOCAL BROWSER RUN"
        m_mark = "✅ VERIFIED" if self.mobile_viewport_verified else "⏳ PENDING LOCAL BROWSER RUN"
        s_mark = "✅ VERIFIED" if self.sandbox_money_path_verified else "⏳ PENDING SANDBOX ORDER TEST"

        md = [
            "### 🧪 QA Evidence & Head-Bound Verification Gate",
            "",
            f"**Bound Git HEAD SHA:** `{self.head_sha}` *(Head change invalidates all head-bound evidence)*",
            "",
            "| Verification Surface | Required Gate | Status | Evidence Reference |",
            "| :--- | :--- | :--- | :--- |",
            f"| **Desktop Viewport (1920px)** | Authenticated Browser Run | {d_mark} | {self.desktop_asset_url or 'Pending run'} |",
            f"| **Mobile Viewport (320px)** | Authenticated Touch Run | {m_mark} | {self.mobile_asset_url or 'Pending run'} |",
            f"| **SANDBOX Trading Path** | Order Place/Fill/Refund | {s_mark} | {self.transaction_proof or 'Pending execution'} |",
        ]
        if self.automated_test_summary:
            md.extend([
                "",
                f"**Automated Suite Status:** {self.automated_test_summary}",
            ])
        return "\n".join(md)


@dataclass
class GitHubIssuePlan:
    """Complete GitHub-Native Issue Plan."""
    issue_number: int
    title: str
    brief: str
    is_synthetic_example: bool = False
    preflight: Optional[PreflightGate] = None
    architecture: Optional[ArchitectureDiagram] = None
    decisions: List[PlanDecision] = field(default_factory=list)
    checklist: List[ChecklistItem] = field(default_factory=list)
    risks: List[RiskCard] = field(default_factory=list)
    changed_files: List[ChangedFile] = field(default_factory=list)
    before_after: List[BeforeAfterState] = field(default_factory=list)
    qa_evidence: Optional[QaEvidenceGate] = None
    superboard_project_url: str = "https://github.com/orgs/Bavariance/projects/1"
    labels: List[str] = field(default_factory=list)

    def render_section(self, section_id: str) -> str:
        """Render a single managed section wrapped in markers."""
        begin_marker = SECTION_BEGIN_FMT.format(section_id=section_id)
        end_marker = SECTION_END_FMT.format(section_id=section_id)
        
        content = ""
        if section_id == "header":
            synthetic_badge = (
                "\n> [!NOTE]\n> **REFERENCE IMPLEMENTATION / SYNTHETIC DEMONSTRATION**\n"
                "> This plan demonstrates the GitHub-Native token-efficient structured workflow.\n"
                "> In-flight acceptance criteria remain unchecked; no false closures.\n"
                if self.is_synthetic_example else ""
            )
            labels_str = " ".join([f"`{l}`" for l in self.labels]) if self.labels else "`type:harness`"
            content = (
                f"## 📋 Structured Plan: {self.title}\n"
                f"{synthetic_badge}\n"
                f"**Target Issue:** #{self.issue_number} | **Superboard:** [Project #1]({self.superboard_project_url})\n"
                f"**Labels:** {labels_str}\n\n"
                f"### 🎯 High-Altitude Brief\n{self.brief}\n"
            )
        elif section_id == "preflight" and self.preflight:
            content = self.preflight.render() + "\n"
        elif section_id == "architecture" and self.architecture:
            content = self.architecture.render() + "\n"
        elif section_id == "decisions":
            md = ["### ⚖️ Asynchronous Human Decision Contracts\n"]
            if self.decisions:
                for dec in self.decisions:
                    md.append(dec.render())
                    md.append("")
            else:
                md.append("*(No blocking human decisions pending)*\n")
            content = "\n".join(md)
        elif section_id == "checklist":
            md = [
                "### 📝 Execution Checklist & Acceptance Criteria",
                "",
            ]
            for item in self.checklist:
                md.append(item.render())
            md.append("")
            content = "\n".join(md)
        elif section_id == "risks":
            md = [
                "### ⚠️ Risk Matrix & Mitigations",
                "",
                "| Risk ID | Severity | Description | Mitigation Strategy |",
                "| :--- | :--- | :--- | :--- |",
            ]
            for r in self.risks:
                md.append(r.render())
            md.append("")
            content = "\n".join(md)
        elif section_id == "changed_files":
            md = [
                "### 📂 Planned File Modifications & Components",
                "",
                "| File Path | Action | Rationale |",
                "| :--- | :--- | :--- |",
            ]
            for f in self.changed_files:
                md.append(f.render())
            md.append("")
            content = "\n".join(md)
        elif section_id == "before_after":
            md = [
                "### 🔄 Before vs. After State Comparison",
                "",
                "| Architectural Aspect | Current State (Before) | Proposed State (After) | Impact |",
                "| :--- | :--- | :--- | :--- |",
            ]
            for ba in self.before_after:
                md.append(ba.render())
            md.append("")
            content = "\n".join(md)
        elif section_id == "qa_evidence" and self.qa_evidence:
            content = self.qa_evidence.render() + "\n"
        elif section_id == "limitations":
            content = (
                "### 🔍 Source Comparison & GitHub Markdown Limitations\n\n"
                "| Feature Dimension | Hosted Agent-Native Canvas | GitHub-Native Plan (This Workflow) |\n"
                "| :--- | :--- | :--- |\n"
                "| **Service & Infrastructure** | External Node/Dokploy service, port bindings, OAuth | Lightweight local Python CLI; no background daemons *(wrappers under active verification)* |\n"
                "| **Source of Truth** | External DB (`plan.agent-native.com` / SQLite) | GitHub Issue & PR Timeline directly |\n"
                "| **Visual Diagrams** | Interactive flex cards, canvas drag-and-drop | Native Mermaid diagrams (` ```mermaid `) *(limited canvas support; declarative only)* |\n"
                "| **Idempotent Updates** | Proprietary REST API | Managed section markers (`<!-- github-plan:... -->`) |\n"
                "| **Decision Governance** | Markdown comment pins | Typed `DEC-*` contracts integrated with request ledger |\n"
                "| **Realistic Tradeoffs** | Spatial drag/drop canvas, live multi-cursor editing | High token efficiency, version auditability, timeline integration; no dynamic canvas |\n"
            )

        return f"{begin_marker}\n{content.strip()}\n{end_marker}"

    def render_full_plan(self) -> str:
        """Render the complete GitHub Issue Plan with all managed sections."""
        section_order = [
            "header",
            "preflight",
            "architecture",
            "decisions",
            "checklist",
            "risks",
            "changed_files",
            "before_after",
            "qa_evidence",
            "limitations",
        ]
        sections = [self.render_section(s) for s in section_order]
        return "\n\n".join(sections)


@dataclass
class GitHubPrRecap:
    """Sticky Pull Request Recap template."""
    pr_number: int
    head_sha: str
    base_branch: str
    title: str
    summary: str
    architecture_delta_mermaid: Optional[str] = None
    changed_files: List[ChangedFile] = field(default_factory=list)
    before_after: List[BeforeAfterState] = field(default_factory=list)
    qa_evidence: Optional[QaEvidenceGate] = None
    is_synthetic_example: bool = False

    def render(self) -> str:
        marker_begin = SECTION_BEGIN_FMT.format(section_id="pr_recap")
        marker_end = SECTION_END_FMT.format(section_id="pr_recap")
        
        synthetic_badge = (
            "\n> [!NOTE]\n> **SYNTHETIC REFERENCE RECAP**\n"
            "> Demonstrates high-altitude sticky PR review structure without external services.\n"
            if self.is_synthetic_example else ""
        )

        md = [
            marker_begin,
            f"## 🔍 PR Visual Recap: {self.title}",
            synthetic_badge,
            f"**PR:** #{self.pr_number} | **Head:** `{self.head_sha}` | **Base:** `{self.base_branch}` | **Status:** ℹ️ Review Aid (Non-Blocking)",
            "",
            "### 🎯 Summary of Changes",
            self.summary,
            "",
        ]

        if self.architecture_delta_mermaid:
            md.extend([
                "### 🏛️ Architecture Delta",
                "```mermaid",
                self.architecture_delta_mermaid.strip(),
                "```",
                "",
            ])

        if self.changed_files:
            md.extend([
                "### 📂 Modified & Created Files",
                "",
                "| Path | Type | Rationale |",
                "| :--- | :--- | :--- |",
            ])
            for f in self.changed_files:
                md.append(f.render())
            md.append("")

        if self.before_after:
            md.extend([
                "### 🔄 Behavior & Contract Delta (Before vs After)",
                "",
                "| Aspect | Before | After | Impact |",
                "| :--- | :--- | :--- | :--- |",
            ])
            for ba in self.before_after:
                md.append(ba.render())
            md.append("")

        if self.qa_evidence:
            md.append(self.qa_evidence.render())
            md.append("")

        md.extend([
            "---",
            "*(This is a sticky PR recap comment. On subsequent pushes to this PR branch, this comment is updated in place.)*",
            marker_end,
        ])
        return "\n".join(md)
