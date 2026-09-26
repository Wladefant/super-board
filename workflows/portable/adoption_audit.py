#!/usr/bin/env python3
"""adoption_audit.py - Enforce adopt-or-reject rule and parent/sub-issue integrity.

The operator's standing demand:
  1. Every written recommendation or requirement must be either adopted
     (shipped AND wired into default use, citing `adopted-at: <file:line or skill>`)
     or explicitly rejected with a recorded rule (`rejected: <rule link>`).
  2. A parent issue never closes while native sub-issues remain open.

Audit targets in Wladefant/super-board (or specified repository):
  - Issues labeled `kind:research` or `kind:governance`.
  - Any issue with native sub-issues.

Violations flagged:
  (a) A closed parent with open sub-issues (`closed_parent_with_open_subissues`).
  (b) A closed sub-issue without an `adopted-at: <file:line or skill>` or
      `rejected: <rule link>` line in its closing comment or body
      (`closed_subissue_without_adoption_or_rejection`).
  (c) A research recommendation with no sub-issue
      (`research_recommendation_without_subissue`).

Options:
  --repo <owner/repo>   Target repository (default: Wladefant/super-board or GITHUB_REPOSITORY).
  --issue <number>      Audit a single issue instead of full repository.
  --auto-reopen         Automatically reopen closed parent issues that have open sub-issues.
  --dry-run             Do not perform mutations (default unless --auto-reopen is active).
  --json                Emit machine-readable JSON output to stdout.
  --markdown            Emit Markdown report to stdout.

Exit code:
  0: No findings (clean audit).
  1: Findings detected (violations present).
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

DEFAULT_REPO = "Wladefant/super-board"

# Regex for adoption & rejection annotations
ADOPTED_AT_RE = re.compile(r"(?i)\badopted-at:\s*(\S+)")
REJECTED_RE = re.compile(r"(?i)\brejected:\s*(\S+)")

# Header regex identifying recommendations / proposed tasks / roadmap sections
RECOMMENDATION_SECTION_RE = re.compile(
    r"(?im)^(#{1,4})\s+(?:(?:\d+[\.\)]?)?\s*)?(?:.*recommendation.*|.*recommended.*|.*proposed.*|.*next steps?.*|.*action items?.*|.*action plan.*|.*roadmap.*|.*buy recommendation.*)\s*$"
)

# Common domain keywords that signal high-confidence token matching
DOMAIN_KEYWORDS: Set[str] = {
    "feature", "map", "lint", "workaround", "comment", "comments",
    "gardener", "verification", "smoke_test", "smoke", "gate",
    "outer", "loop", "webhook", "intake", "dokploy", "sentry",
    "effort", "caching", "prefix", "handoff", "calibration",
    "routing", "deepseek", "minimax", "glm", "subscription",
    "superboard", "orchestration", "subagent"
}

STOP_WORDS: Set[str] = {
    "the", "a", "an", "for", "in", "and", "or", "of", "to", "at", "by", "from",
    "with", "on", "p0", "p1", "p2", "task", "phase", "follow", "up", "scripts",
    "json", "py", "md", "issue", "subissue", "pr", "prs", "via", "using", "into",
    "item", "step", "steps"
}


@dataclass
class Finding:
    category: str  # closed_parent_with_open_subissues | closed_subissue_without_adoption_or_rejection | research_recommendation_without_subissue
    issue_number: int
    title: str
    url: str
    details: Dict[str, Any]
    action_taken: str = "none"


@dataclass
class AuditSummary:
    total_issues_scanned: int = 0
    total_findings: int = 0
    closed_parents_with_open_subissues: int = 0
    closed_subissues_without_adoption_or_rejection: int = 0
    research_recommendations_without_subissues: int = 0


@dataclass
class AuditResult:
    repo: str
    timestamp: str
    status: str  # "pass" | "fail"
    summary: AuditSummary
    findings: List[Finding] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo": self.repo,
            "timestamp": self.timestamp,
            "status": self.status,
            "summary": asdict(self.summary),
            "findings": [asdict(f) for f in self.findings],
        }


def tokenize(text: str) -> Set[str]:
    """Extract significant lowercase word tokens from text."""
    clean = re.sub(r"[^\w\s]", " ", text.lower())
    return {w for w in clean.split() if len(w) > 2 and w not in STOP_WORDS}


def extract_research_recommendations(body: str) -> List[str]:
    """Parse recommendation sections in an issue body and extract individual recommendations."""
    if not body:
        return []
    recs: List[str] = []
    seen: Set[str] = set()

    for match in RECOMMENDATION_SECTION_RE.finditer(body):
        header_level = len(match.group(1))
        start_pos = match.end()
        # Find next header of equal or higher level (<= header_level)
        next_header_re = re.compile(r"(?im)^#{1," + str(header_level) + r"}\s+")
        next_match = next_header_re.search(body[start_pos:])
        section_text = body[start_pos : start_pos + next_match.start()] if next_match else body[start_pos:]

        section_recs: List[str] = []

        # 1. Subheadings (e.g. ### Task 1: ...)
        subheading_re = re.compile(r"(?im)^#{3,5}\s+(.+)$")
        for m in subheading_re.finditer(section_text):
            item = m.group(1).strip()
            if item and item.lower() not in ("overview", "summary", "notes", "background") and item not in seen:
                seen.add(item)
                section_recs.append(item)

        if not section_recs:
            # 2. Numbered list items: 1. **Title**: ... or 1. Title
            numbered_re = re.compile(r"(?im)^\s*\d+[\.\)]\s+(?:\*\*([^*]+)\*\*|([^\n]+))")
            for m in numbered_re.finditer(section_text):
                item = (m.group(1) or m.group(2)).strip()
                if item and len(item) > 3 and item not in seen:
                    seen.add(item)
                    section_recs.append(item)

        if not section_recs:
            # 3. Bullet items with bold title: - **Title**
            bullet_re = re.compile(r"(?im)^\s*[-*]\s+\*\*([^*]+)\*\*")
            for m in bullet_re.finditer(section_text):
                item = m.group(1).strip()
                if item and len(item) > 3 and item not in seen:
                    seen.add(item)
                    section_recs.append(item)

        if not section_recs:
            # 4. Table rows with bold items
            table_re = re.compile(r"(?im)^\|\s*(?:\d+|[-—])\s*\|\s*(?:\*\*)?([^|*]+)(?:\*\*)?\s*\|")
            for m in table_re.finditer(section_text):
                t = m.group(1).strip()
                if t and t.lower() not in ("purchase", "task", "item", "priority", "why now") and t not in seen:
                    seen.add(t)
                    section_recs.append(t)

        recs.extend(section_recs)

    return recs


def match_recommendation_to_subissues(
    recommendation: str,
    sub_issues: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Match a recommendation string to an existing sub-issue."""
    if not sub_issues:
        return None

    r_tokens = tokenize(recommendation)

    for sub in sub_issues:
        sub_title = sub.get("title", "")
        sub_number = sub.get("number")

        # 1. Direct issue number mention in recommendation
        if sub_number and (f"#{sub_number}" in recommendation or f"/issues/{sub_number}" in recommendation):
            return sub

        # 2. Token overlap with sub-issue title
        s_tokens = tokenize(sub_title)
        overlap = r_tokens & s_tokens

        if len(overlap) >= 2:
            return sub
        if len(overlap) == 1 and bool(overlap & DOMAIN_KEYWORDS):
            return sub

        # 3. Explicit task/phase numbering alignment
        m_rec_task = re.search(r"(?i)\b(?:task|phase)\s*(\d+)", recommendation)
        m_sub_task = re.search(r"(?i)\b(?:task|phase)\s*(\d+)", sub_title)
        if m_rec_task and m_sub_task and m_rec_task.group(1) == m_sub_task.group(1):
            return sub

    return None


def check_adopted_or_rejected(text: str) -> Tuple[bool, Optional[str]]:
    """Check whether a text blob contains `adopted-at:` or `rejected:`."""
    if not text:
        return False, None
    m_adopt = ADOPTED_AT_RE.search(text)
    if m_adopt:
        return True, m_adopt.group(0)
    m_reject = REJECTED_RE.search(text)
    if m_reject:
        return True, m_reject.group(0)
    return False, None


class GitHubClient:
    """Interface to GitHub CLI / REST API with runner injection for unit tests."""

    def __init__(self, token: Optional[str] = None, timeout_sec: int = 30):
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.timeout_sec = timeout_sec

    def _run_gh(self, args: List[str]) -> Tuple[int, str, str]:
        cmd = ["gh"] + args
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                shell=False,
            )
            return res.returncode, res.stdout, res.stderr
        except Exception as e:
            return 1, "", str(e)

    def get_issues(self, repo: str, state: str = "all") -> List[Dict[str, Any]]:
        """Fetch all non-PR issues in the repository."""
        issues: List[Dict[str, Any]] = []
        page = 1
        while True:
            rc, stdout, stderr = self._run_gh([
                "api",
                f"repos/{repo}/issues?state={state}&per_page=100&page={page}"
            ])
            if rc != 0 or not stdout.strip():
                break
            try:
                batch = json.loads(stdout)
            except json.JSONDecodeError:
                break
            if not isinstance(batch, list) or not batch:
                break
            # Filter out pull requests
            issues.extend([i for i in batch if not i.get("pull_request")])
            if len(batch) < 100:
                break
            page += 1
        return issues

    def get_single_issue(self, repo: str, issue_number: int) -> Optional[Dict[str, Any]]:
        """Fetch a single issue by number."""
        rc, stdout, _ = self._run_gh(["api", f"repos/{repo}/issues/{issue_number}"])
        if rc != 0 or not stdout.strip():
            return None
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return None

    def get_sub_issues(self, repo: str, issue_number: int) -> List[Dict[str, Any]]:
        """Fetch native sub-issues for an issue."""
        rc, stdout, _ = self._run_gh(["api", f"repos/{repo}/issues/{issue_number}/sub_issues"])
        if rc != 0 or not stdout.strip():
            return []
        try:
            data = json.loads(stdout)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    def get_issue_comments(self, repo: str, issue_number: int) -> List[Dict[str, Any]]:
        """Fetch comments for an issue."""
        rc, stdout, _ = self._run_gh(["api", f"repos/{repo}/issues/{issue_number}/comments?per_page=100"])
        if rc != 0 or not stdout.strip():
            return []
        try:
            data = json.loads(stdout)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    def reopen_issue(self, repo: str, issue_number: int) -> bool:
        """Reopen an issue."""
        rc, _, _ = self._run_gh(["issue", "reopen", str(issue_number), "-R", repo])
        return rc == 0

    def add_comment(self, repo: str, issue_number: int, body: str) -> bool:
        """Add a comment to an issue."""
        rc, _, _ = self._run_gh(["issue", "comment", str(issue_number), "-R", repo, "--body", body])
        return rc == 0


def reopen_parent_with_comment(
    client: GitHubClient,
    repo: str,
    parent: Dict[str, Any],
    open_subs: List[Dict[str, Any]],
    dry_run: bool = False,
) -> str:
    """Reopen a prematurely closed parent issue and post an explanatory comment."""
    num = parent.get("number")
    title = parent.get("title", "")
    open_sub_lines = []
    for s in open_subs:
        s_num = s.get("number")
        s_title = s.get("title", "")
        s_url = s.get("html_url", f"https://github.com/{repo}/issues/{s_num}")
        open_sub_lines.append(f"- #{s_num}: {s_title} ({s_url})")

    comment_body = (
        f"### ⚠️ Reopened by Adoption Audit (`adoption_audit.py`)\n\n"
        f"Parent issue #{num} has been automatically reopened because it has {len(open_subs)} open sub-issue(s):\n"
        + "\n".join(open_sub_lines)
        + "\n\n**Rule (AGENTS.md §5):** A parent issue cannot be closed while sub-issues remain open.\n"
        "Every sub-issue must be closed with evidence (`adopted-at: <file:line or skill>` or `rejected: <rule link>`) "
        "before the parent issue may be closed.\n"
    )

    if dry_run:
        return "dry_run_reopen_skipped"

    reopened = client.reopen_issue(repo, num)
    if reopened:
        client.add_comment(repo, num, comment_body)
        return "reopened"
    return "reopen_failed"


def run_adoption_audit(
    repo: str = DEFAULT_REPO,
    issue_number: Optional[int] = None,
    auto_reopen: bool = False,
    dry_run: bool = False,
    client: Optional[GitHubClient] = None,
) -> AuditResult:
    """Execute adoption audit against target repository or single issue."""
    if client is None:
        client = GitHubClient()

    now_iso = datetime.now(timezone.utc).isoformat()
    findings: List[Finding] = []
    scanned_count = 0

    closed_parent_count = 0
    closed_subissue_count = 0
    research_rec_count = 0

    if issue_number is not None:
        single = client.get_single_issue(repo, issue_number)
        issues = [single] if single else []
    else:
        issues = client.get_issues(repo, state="all")

    scanned_count = len(issues)

    # Cache sub-issues per parent: parent_number -> List[sub_issues]
    parent_subissues_cache: Dict[int, List[Dict[str, Any]]] = {}

    for issue in issues:
        num = issue.get("number")
        title = issue.get("title", "")
        url = issue.get("html_url", f"https://github.com/{repo}/issues/{num}")
        state = issue.get("state", "open")
        body = issue.get("body") or ""
        labels = [l.get("name", "") if isinstance(l, dict) else str(l) for l in issue.get("labels", [])]

        is_research = "kind:research" in labels
        is_governance = "kind:governance" in labels

        sub_summary = issue.get("sub_issues_summary") or {}
        has_subissues = (sub_summary.get("total", 0) > 0)

        # Retrieve sub-issues if this issue has sub-issues
        sub_issues: List[Dict[str, Any]] = []
        if has_subissues:
            sub_issues = client.get_sub_issues(repo, num)
            parent_subissues_cache[num] = sub_issues

        # -------------------------------------------------------------
        # Check (a): Closed parent with open sub-issues
        # -------------------------------------------------------------
        if has_subissues and state == "closed":
            open_subs = [s for s in sub_issues if s.get("state") == "open"]
            if open_subs:
                action_taken = "none"
                if auto_reopen:
                    action_taken = reopen_parent_with_comment(
                        client=client,
                        repo=repo,
                        parent=issue,
                        open_subs=open_subs,
                        dry_run=dry_run,
                    )
                finding = Finding(
                    category="closed_parent_with_open_subissues",
                    issue_number=num,
                    title=title,
                    url=url,
                    details={
                        "open_subissues_count": len(open_subs),
                        "total_subissues_count": len(sub_issues),
                        "open_subissues": [
                            {
                                "number": s.get("number"),
                                "title": s.get("title"),
                                "url": s.get("html_url", f"https://github.com/{repo}/issues/{s.get('number')}"),
                                "state": s.get("state"),
                            }
                            for s in open_subs
                        ],
                    },
                    action_taken=action_taken,
                )
                findings.append(finding)
                closed_parent_count += 1

        # -------------------------------------------------------------
        # Check (c): Research recommendation with no sub-issue
        # -------------------------------------------------------------
        if is_research:
            recommendations = extract_research_recommendations(body)
            for rec in recommendations:
                matched_sub = match_recommendation_to_subissues(rec, sub_issues)
                if matched_sub is None:
                    finding = Finding(
                        category="research_recommendation_without_subissue",
                        issue_number=num,
                        title=title,
                        url=url,
                        details={
                            "recommendation": rec,
                            "existing_subissues_count": len(sub_issues),
                        },
                    )
                    findings.append(finding)
                    research_rec_count += 1

    # -----------------------------------------------------------------
    # Check (b): Closed sub-issue without adopted-at: or rejected:
    # -----------------------------------------------------------------
    # Sweep all sub-issues found under parent issues
    all_sub_issues: List[Tuple[int, Dict[str, Any]]] = []
    for p_num, s_list in parent_subissues_cache.items():
        for s in s_list:
            all_sub_issues.append((p_num, s))

    # Deduplicate sub-issues by sub-issue number
    seen_subs: Set[int] = set()
    for p_num, s in all_sub_issues:
        s_num = s.get("number")
        if not s_num or s_num in seen_subs:
            continue
        seen_subs.add(s_num)

        if s.get("state") == "closed":
            s_body = s.get("body") or ""
            has_marker, marker = check_adopted_or_rejected(s_body)

            if not has_marker:
                # Check comments
                comments = client.get_issue_comments(repo, s_num)
                for c in comments:
                    has_marker, marker = check_adopted_or_rejected(c.get("body") or "")
                    if has_marker:
                        break

            if not has_marker:
                s_title = s.get("title", "")
                s_url = s.get("html_url", f"https://github.com/{repo}/issues/{s_num}")
                finding = Finding(
                    category="closed_subissue_without_adoption_or_rejection",
                    issue_number=s_num,
                    title=s_title,
                    url=s_url,
                    details={
                        "parent_issue_number": p_num,
                        "reason": "Missing required 'adopted-at: <file:line or skill>' or 'rejected: <rule link>' marker in body or comments",
                    },
                )
                findings.append(finding)
                closed_subissue_count += 1

    status = "fail" if findings else "pass"
    summary = AuditSummary(
        total_issues_scanned=scanned_count,
        total_findings=len(findings),
        closed_parents_with_open_subissues=closed_parent_count,
        closed_subissues_without_adoption_or_rejection=closed_subissue_count,
        research_recommendations_without_subissues=research_rec_count,
    )

    return AuditResult(
        repo=repo,
        timestamp=now_iso,
        status=status,
        summary=summary,
        findings=findings,
    )


def format_markdown_report(result: AuditResult) -> str:
    """Format AuditResult as human-readable Markdown."""
    lines: List[str] = [
        "# 📋 Adoption & Sub-Issue Integrity Audit Report",
        "",
        f"- **Repository:** `{result.repo}`",
        f"- **Timestamp:** `{result.timestamp}`",
        f"- **Audit Status:** `{'PASSED (0 findings)' if result.status == 'pass' else 'FAILED (' + str(result.summary.total_findings) + ' findings)'}`",
        "",
        "## Summary",
        "",
        f"- **Total Issues Scanned:** {result.summary.total_issues_scanned}",
        f"- **(a) Closed parents with open sub-issues:** {result.summary.closed_parents_with_open_subissues}",
        f"- **(b) Closed sub-issues without adoption or rejection proof:** {result.summary.closed_subissues_without_adoption_or_rejection}",
        f"- **(c) Research recommendations with no sub-issue:** {result.summary.research_recommendations_without_subissues}",
        "",
    ]

    if not result.findings:
        lines.append("✅ **All audited issues satisfy the adopt-or-reject policy and sub-issue integrity.**")
        return "\n".join(lines)

    lines.append("## Findings Detail")
    lines.append("")

    # Group findings by category
    a_findings = [f for f in result.findings if f.category == "closed_parent_with_open_subissues"]
    b_findings = [f for f in result.findings if f.category == "closed_subissue_without_adoption_or_rejection"]
    c_findings = [f for f in result.findings if f.category == "research_recommendation_without_subissue"]

    if a_findings:
        lines.append("### (a) Closed Parents with Open Sub-Issues")
        lines.append("")
        for f in a_findings:
            action_suffix = f" *(Action: {f.action_taken})*" if f.action_taken != "none" else ""
            lines.append(f"- **Issue #{f.issue_number}:** [{f.title}]({f.url}){action_suffix}")
            open_subs = f.details.get("open_subissues", [])
            for s in open_subs:
                lines.append(f"  - ⚠️ Open sub-issue #{s.get('number')}: [{s.get('title')}]({s.get('url')})")
        lines.append("")

    if b_findings:
        lines.append("### (b) Closed Sub-Issues Missing Adoption or Rejection Proof")
        lines.append("")
        for f in b_findings:
            p_num = f.details.get("parent_issue_number")
            parent_ref = f" (parent #{p_num})" if p_num else ""
            lines.append(f"- **Sub-Issue #{f.issue_number}:** [{f.title}]({f.url}){parent_ref}")
            lines.append(f"  - 🚩 Missing `adopted-at: <file:line or skill>` or `rejected: <rule link>`")
        lines.append("")

    if c_findings:
        lines.append("### (c) Research Recommendations Missing Sub-Issues")
        lines.append("")
        for f in c_findings:
            rec = f.details.get("recommendation", "")
            lines.append(f"- **Parent Issue #{f.issue_number}:** [{f.title}]({f.url})")
            lines.append(f"  - 🚩 Unlinked recommendation: `{rec}`")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enforce adopt-or-reject rule and parent/sub-issue integrity."
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO),
        help=f"Target repository (default: {DEFAULT_REPO} or GITHUB_REPOSITORY env)",
    )
    parser.add_argument(
        "--issue",
        type=int,
        default=None,
        help="Audit a single issue number instead of the full repository",
    )
    parser.add_argument(
        "--auto-reopen",
        action="store_true",
        help="Automatically reopen closed parent issues that have open sub-issues",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not mutate GitHub state (reopen issues or add comments)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON to stdout",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Emit Markdown report to stdout",
    )

    args = parser.parse_args()

    result = run_adoption_audit(
        repo=args.repo,
        issue_number=args.issue,
        auto_reopen=args.auto_reopen,
        dry_run=args.dry_run,
    )

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_markdown_report(result))

    # Exit non-zero on findings per requirement
    sys.exit(1 if result.findings else 0)


if __name__ == "__main__":
    main()
