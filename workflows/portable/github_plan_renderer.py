#!/usr/bin/env python3
"""
GitHub-Native Plan & Recap Renderer (~/.veyyon/workflows/github_plan_renderer.py)

CLI tool and Python engine for parsing, rendering, and idempotently updating
structured GitHub issue plans and sticky PR visual recaps using managed section markers.

Key Invariants & Guarantees:
  1. Idempotency: Updating a section leaves other sections and user notes untouched.
  2. Non-Destructive: Preserves user-written text outside managed markers.
  3. Decision Invariant: Never auto-answers human decisions without authorized input.
  4. Preflight Enforcement: Validates connected-service gates (staging, tokens, safety).
  5. Compact Updates: Generates token-efficient deltas when full re-renders are unnecessary.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from github_plan_templates import (
    SECTION_BEGIN_FMT,
    SECTION_END_FMT,
    ArchitectureDiagram,
    BeforeAfterState,
    ChangedFile,
    ChecklistItem,
    GitHubIssuePlan,
    GitHubPrRecap,
    PlanDecision,
    PreflightGate,
    QaEvidenceGate,
    RiskCard,
)

# Regex to match managed sections
# Matches <!-- github-plan:begin:(?P<id>[a-zA-Z0-9_-]+) --> ... <!-- github-plan:end:(?P=id) -->
SECTION_REGEX = re.compile(
    r"<!--\s*github-plan:begin:(?P<id>[a-zA-Z0-9_-]+)\s*-->"
    r"(?P<content>.*?)"
    r"<!--\s*github-plan:end:(?P=id)\s*-->",
    re.DOTALL,
)


def parse_managed_sections(text: str) -> Dict[str, str]:
    """Extract all managed sections from text by section ID."""
    sections = {}
    for match in SECTION_REGEX.finditer(text):
        sec_id = match.group("id")
        content = match.group("content").strip()
        sections[sec_id] = content
    return sections


def replace_managed_section(existing_text: str, section_id: str, new_content: str) -> Tuple[str, bool]:
    """
    Idempotently replaces a managed section inside existing_text.
    Returns (updated_text, was_replaced).
    If section marker was not present, appends the new section cleanly.
    """
    pattern = re.compile(
        rf"(<!--\s*github-plan:begin:{re.escape(section_id)}\s*-->)"
        rf"(.*?)"
        rf"(<!--\s*github-plan:end:{re.escape(section_id)}\s*-->)",
        re.DOTALL,
    )
    
    clean_content = new_content.strip()
    replacement = rf"\1\n{clean_content}\n\3"
    
    if pattern.search(existing_text):
        updated_text = pattern.sub(replacement, existing_text, count=1)
        return updated_text, True
    else:
        # Append section at end
        begin_marker = SECTION_BEGIN_FMT.format(section_id=section_id)
        end_marker = SECTION_END_FMT.format(section_id=section_id)
        addition = f"\n\n{begin_marker}\n{clean_content}\n{end_marker}\n"
        return existing_text.rstrip() + addition, False


def update_managed_sections(existing_text: str, updates: Dict[str, str]) -> str:
    """Apply multiple section updates idempotently."""
    text = existing_text
    for sec_id, content in updates.items():
        text, _ = replace_managed_section(text, sec_id, content)
    return text


def load_plan_from_dict(data: Dict[str, Any]) -> GitHubIssuePlan:
    """Construct a GitHubIssuePlan from a dictionary/spec."""
    preflight = None
    if "preflight" in data and data["preflight"]:
        preflight = PreflightGate(**data["preflight"])
    
    architecture = None
    if "architecture" in data and data["architecture"]:
        architecture = ArchitectureDiagram(**data["architecture"])
    
    decisions = []
    for d in data.get("decisions", []):
        decisions.append(PlanDecision(**d))
        
    checklist = []
    for c in data.get("checklist", []):
        checklist.append(ChecklistItem(**c))
        
    risks = []
    for r in data.get("risks", []):
        risks.append(RiskCard(**r))
        
    changed_files = []
    for f in data.get("changed_files", []):
        changed_files.append(ChangedFile(**f))
        
    before_after = []
    for ba in data.get("before_after", []):
        before_after.append(BeforeAfterState(**ba))
        
    qa_evidence = None
    if "qa_evidence" in data and data["qa_evidence"]:
        qa_evidence = QaEvidenceGate(**data["qa_evidence"])
        
    return GitHubIssuePlan(
        issue_number=data.get("issue_number", 0),
        title=data.get("title", ""),
        brief=data.get("brief", ""),
        is_synthetic_example=data.get("is_synthetic_example", False),
        preflight=preflight,
        architecture=architecture,
        decisions=decisions,
        checklist=checklist,
        risks=risks,
        changed_files=changed_files,
        before_after=before_after,
        qa_evidence=qa_evidence,
        superboard_project_url=data.get("superboard_project_url", "https://github.com/orgs/Bavariance/projects/1"),
        labels=data.get("labels", []),
    )


def load_recap_from_dict(data: Dict[str, Any]) -> GitHubPrRecap:
    """Construct a GitHubPrRecap from a dictionary/spec."""
    changed_files = [ChangedFile(**f) for f in data.get("changed_files", [])]
    before_after = [BeforeAfterState(**ba) for ba in data.get("before_after", [])]
    qa_evidence = QaEvidenceGate(**data["qa_evidence"]) if data.get("qa_evidence") else None
    
    return GitHubPrRecap(
        pr_number=data.get("pr_number", 0),
        head_sha=data.get("head_sha", ""),
        base_branch=data.get("base_branch", "staging"),
        title=data.get("title", ""),
        summary=data.get("summary", ""),
        architecture_delta_mermaid=data.get("architecture_delta_mermaid"),
        changed_files=changed_files,
        before_after=before_after,
        qa_evidence=qa_evidence,
        is_synthetic_example=data.get("is_synthetic_example", False),
    )


def gh_cli_run(args: List[str]) -> Tuple[int, str, str]:
    """Execute gh command safely and return (exit_code, stdout, stderr)."""
    cmd = ["gh"] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def main():
    parser = argparse.ArgumentParser(description="GitHub-Native Plan & Recap Renderer")
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommands")

    # Render plan from JSON spec
    p_render = subparsers.add_parser("render-plan", help="Render full GitHub issue plan from JSON spec")
    p_render.add_argument("--spec", required=True, help="Path to plan JSON spec")
    p_render.add_argument("--out", help="Output file path (default stdout)")

    # Render PR recap from JSON spec
    p_recap = subparsers.add_parser("render-recap", help="Render PR visual recap from JSON spec")
    p_recap.add_argument("--spec", required=True, help="Path to recap JSON spec")
    p_recap.add_argument("--out", help="Output file path (default stdout)")

    # Parse sections from markdown
    p_parse = subparsers.add_parser("parse-sections", help="Extract managed sections from markdown file")
    p_parse.add_argument("--file", required=True, help="Path to markdown file")

    # Idempotent update section
    p_update = subparsers.add_parser("update-section", help="Idempotently update a managed section in a file")
    p_update.add_argument("--file", required=True, help="Target markdown file to update in place")
    p_update.add_argument("--section", required=True, help="Section ID to update")
    p_update.add_argument("--content-file", required=True, help="File containing new section content")

    # Publish or comment on GitHub issue
    p_post = subparsers.add_parser("post-issue-comment", help="Post or update comment on GitHub issue")
    p_post.add_argument("--issue", type=int, required=True, help="GitHub issue number")
    p_post.add_argument("--repo", default="Bavariance/polysimulator", help="GitHub repo (owner/repo)")
    p_post.add_argument("--file", required=True, help="Path to markdown content file")
    p_post.add_argument("--comment-id", help="Existing comment ID to update in place")

    args = parser.parse_args()
    if not args.subcommand:
        parser.print_help()
        sys.exit(0)

    if args.subcommand == "render-plan":
        with open(args.spec, "r", encoding="utf-8") as f:
            data = json.load(f)
        plan = load_plan_from_dict(data)
        rendered = plan.render_full_plan()
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(rendered)
            print(f"Rendered plan written to {args.out}")
        else:
            print(rendered)

    elif args.subcommand == "render-recap":
        with open(args.spec, "r", encoding="utf-8") as f:
            data = json.load(f)
        recap = load_recap_from_dict(data)
        rendered = recap.render()
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(rendered)
            print(f"Rendered recap written to {args.out}")
        else:
            print(rendered)

    elif args.subcommand == "parse-sections":
        with open(args.file, "r", encoding="utf-8") as f:
            content = f.read()
        sections = parse_managed_sections(content)
        print(json.dumps({"section_count": len(sections), "sections": list(sections.keys())}, indent=2))

    elif args.subcommand == "update-section":
        with open(args.file, "r", encoding="utf-8") as f:
            existing = f.read()
        with open(args.content_file, "r", encoding="utf-8") as f:
            new_content = f.read()
        
        updated, was_replaced = replace_managed_section(existing, args.section, new_content)
        with open(args.file, "w", encoding="utf-8") as f:
            f.write(updated)
        print(f"Section '{args.section}' {'replaced' if was_replaced else 'appended'} in {args.file}")

    elif args.subcommand == "post-issue-comment":
        with open(args.file, "r", encoding="utf-8") as f:
            body = f.read()
        
        if args.comment_id:
            # Update existing comment
            cmd = ["api", f"repos/{args.repo}/issues/comments/{args.comment_id}", "-X", "PATCH", "-f", f"body={body}"]
            code, out, err = gh_cli_run(cmd)
            if code == 0:
                print(f"Successfully updated comment {args.comment_id}")
            else:
                print(f"Error updating comment: {err}", file=sys.stderr)
                sys.exit(code)
        else:
            # Post new comment
            cmd = ["issue", "comment", str(args.issue), "-R", args.repo, "--body", body]
            code, out, err = gh_cli_run(cmd)
            if code == 0:
                print(f"Successfully posted comment on #{args.issue}: {out}")
            else:
                print(f"Error posting comment: {err}", file=sys.stderr)
                sys.exit(code)


if __name__ == "__main__":
    main()
