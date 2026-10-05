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
  (d) An open issue with more than MAX_TOPIC_COMMENTS long comments
      (>= LONG_COMMENT_CHARS characters each) and no native sub-issues
      (`topic_comment_dump_without_subissues`). Per-topic results belong in
      one native sub-issue each, never in comments on the parent
      (https://github.com/Wladefant/super-board/issues/419).

  (f) A tracked repo's default-branch AGENTS.md (or CLAUDE.md) with no `Project stage:` line,
      more than one, or a value other than greenfield/live (`stage_line_invalid`).
  (g) The same file with no `## Lessons` section or more than POLICY_LESSON_CAP lessons
      (`lessons_section_invalid`).
  (h) A repo marked `live` whose text permits skipping migrations or compatibility
      (`live_repo_permits_skipping_compat`).
  (f)-(h) run with --policy-docs; the repos are listed in --policy-repos
  (https://github.com/Wladefant/super-board/issues/504).

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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from close_guard import MIN_AGE_SECONDS, is_plan_issue, issue_labels, parse_github_time, unchecked_boxes

DEFAULT_REPO = "Wladefant/super-board"

# Check (d): more long comments than this on an issue with no sub-issues is a dump.
MAX_TOPIC_COMMENTS = 5
LONG_COMMENT_CHARS = 1500

# Check (e): look at issues closed within this many days.
PREMATURE_CLOSE_DAYS = 7

# Check (e) categories.
PREMATURE_CLOSE_CATEGORIES = (
    "closed_with_unchecked_boxes",
    "closed_within_minutes_of_creation",
    "plan_closed_without_subissues",
)

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
    topic_comment_dumps_without_subissues: int = 0
    premature_closes: int = 0
    policy_doc_violations: int = 0


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
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return res.returncode, res.stdout, res.stderr
        except Exception as e:
            return 1, "", str(e)

    def get_file(self, repo: str, path: str, ref: str) -> Optional[str]:
        """Return a repository file's text at `ref`, or None when it does not exist."""
        rc, stdout, _ = self._run_gh([
            "api", f"repos/{repo}/contents/{path}?ref={ref}",
            "-H", "Accept: application/vnd.github.raw+json",
        ])
        return stdout if rc == 0 else None

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

    def get_closed_issues_since(self, repo: str, since_iso: str) -> List[Dict[str, Any]]:
        """Fetch non-PR issues updated since `since_iso` that are closed (read-only)."""
        issues: List[Dict[str, Any]] = []
        page = 1
        while True:
            rc, stdout, _ = self._run_gh([
                "api",
                f"repos/{repo}/issues?state=closed&since={since_iso.replace('+', '%2B')}&per_page=100&page={page}",
            ])
            if rc != 0 or not stdout.strip():
                break
            try:
                batch = json.loads(stdout)
            except json.JSONDecodeError:
                break
            if not isinstance(batch, list) or not batch:
                break
            issues.extend([i for i in batch if not i.get("pull_request")])
            if len(batch) < 100:
                break
            page += 1
        return issues

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
    premature_close_repos: Optional[List[str]] = None,
    premature_close_days: int = PREMATURE_CLOSE_DAYS,
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
    topic_dump_count = 0

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
        # Check (d): Topic dump - many long comments, no native sub-issues
        # -------------------------------------------------------------
        comment_total = issue.get("comments") or 0
        if state == "open" and not has_subissues and comment_total > MAX_TOPIC_COMMENTS:
            long_comments = [
                c for c in client.get_issue_comments(repo, num)
                if len(c.get("body") or "") >= LONG_COMMENT_CHARS
            ]
            if len(long_comments) > MAX_TOPIC_COMMENTS:
                findings.append(
                    Finding(
                        category="topic_comment_dump_without_subissues",
                        issue_number=num,
                        title=title,
                        url=url,
                        details={
                            "long_comments_count": len(long_comments),
                            "threshold": MAX_TOPIC_COMMENTS,
                            "min_chars": LONG_COMMENT_CHARS,
                        },
                    )
                )
                topic_dump_count += 1

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

    if premature_close_repos:
        pc_findings, pc_scanned = audit_premature_closes(premature_close_repos, days=premature_close_days, client=client)
        findings.extend(pc_findings)
        scanned_count += pc_scanned

    status = "fail" if findings else "pass"
    summary = AuditSummary(
        total_issues_scanned=scanned_count,
        total_findings=len(findings),
        closed_parents_with_open_subissues=closed_parent_count,
        closed_subissues_without_adoption_or_rejection=closed_subissue_count,
        research_recommendations_without_subissues=research_rec_count,
        topic_comment_dumps_without_subissues=topic_dump_count,
        premature_closes=sum(1 for f in findings if f.category in PREMATURE_CLOSE_CATEGORIES),
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
        f"- **(d) Topic dumps in comments without sub-issues:** {result.summary.topic_comment_dumps_without_subissues}",
        f"- **(e) Closed without proof:** {result.summary.premature_closes}",
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
    d_findings = [f for f in result.findings if f.category == "topic_comment_dump_without_subissues"]
    e_findings = [f for f in result.findings if f.category in PREMATURE_CLOSE_CATEGORIES]

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
    if d_findings:
        lines.append("### (d) Topic Dumps in Comments (create one native sub-issue per topic)")
        lines.append("")
        for f in d_findings:
            n = f.details.get("long_comments_count")
            lines.append(f"- **Issue #{f.issue_number}:** [{f.title}]({f.url}) has {n} long comments and no sub-issues")
        lines.append("")
    if e_findings:
        lines.append("### (e) Closed Without Proof (unchecked boxes, closed within 10 min, or plan with no sub-issue)")
        lines.append("")
        for f in e_findings:
            repo = f.details.get("repo", "")
            if f.category == "closed_with_unchecked_boxes":
                why = f"{f.details.get('unchecked_boxes')} unchecked box(es), e.g. `{(f.details.get('first_boxes') or [''])[0]}`"
            elif f.category == "closed_within_minutes_of_creation":
                why = f"closed {f.details.get('seconds_open')}s after creation"
            else:
                why = "plan/research issue closed with no sub-issue"
            lines.append(f"- **{repo}#{f.issue_number}:** [{f.title}]({f.url}) - {why}")
        lines.append("")

    return "\n".join(lines)


def audit_premature_closes(
    repos: List[str],
    days: int = PREMATURE_CLOSE_DAYS,
    client: Optional[GitHubClient] = None,
    now: Optional[datetime] = None,
) -> Tuple[List[Finding], int]:
    """Check (e): issues closed in the last `days` days that were not proven done.

    Flags a closed issue when it (a) still has a `- [ ]` box in the body, (b) was closed
    less than MIN_AGE_SECONDS after creation, or (c) is a plan/research issue that was
    closed with no sub-issue at all (nothing carries the work forward).
    Read-only. Returns (findings, closed_issues_scanned). Every finding has a URL.
    """
    client = client or GitHubClient()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    findings: List[Finding] = []
    scanned = 0
    for repo in repos:
        for issue in client.get_closed_issues_since(repo, cutoff.isoformat()):
            closed_at = parse_github_time(issue.get("closed_at"))
            if issue.get("state") != "closed" or closed_at is None or closed_at < cutoff:
                continue
            scanned += 1
            num = issue.get("number")
            title = issue.get("title", "")
            url = issue.get("html_url", f"https://github.com/{repo}/issues/{num}")
            boxes = unchecked_boxes(issue.get("body"))
            created = parse_github_time(issue.get("created_at"))
            lifetime = (closed_at - created).total_seconds() if created else None
            if boxes:
                findings.append(Finding(
                    category="closed_with_unchecked_boxes",
                    issue_number=num, title=title, url=url,
                    details={"repo": repo, "unchecked_boxes": len(boxes), "first_boxes": boxes[:3],
                             "closed_at": issue.get("closed_at")},
                ))
            if lifetime is not None and lifetime < MIN_AGE_SECONDS:
                findings.append(Finding(
                    category="closed_within_minutes_of_creation",
                    issue_number=num, title=title, url=url,
                    details={"repo": repo, "seconds_open": int(lifetime), "min_seconds": MIN_AGE_SECONDS,
                             "created_at": issue.get("created_at"), "closed_at": issue.get("closed_at")},
                ))
            if is_plan_issue(title, issue_labels(issue)):
                summary = issue.get("sub_issues_summary") or {}
                total = summary.get("total")
                if total is None:
                    total = len(client.get_sub_issues(repo, num))
                if total == 0:
                    findings.append(Finding(
                        category="plan_closed_without_subissues",
                        issue_number=num, title=title, url=url,
                        details={"repo": repo, "closed_at": issue.get("closed_at")},
                    ))
    return findings, scanned


# Checks (f)-(h): the project stage, Lessons cap and live-repo compatibility text.
POLICY_FILES = ("AGENTS.md", "CLAUDE.md")
POLICY_LESSON_CAP = 20
POLICY_REPOS_DEFAULT = (
    "Wladefant/super-board@main",
    "Wladefant/veyyon@main",
    "Wladefant/komo@main",
    "Wladefant/shipnovo@main",
    "Bavariance/polysimulator@staging",
)
STAGE_LINE_RE = re.compile(r"(?m)^Project stage:[ \t]*(\S*)")
LESSONS_HEADING_RE = re.compile(r"(?m)^## +Lessons[ \t]*$")
# A line that lets work skip migrations or compatibility.
SKIP_COMPAT_RE = re.compile(
    r"(?i)\b(?:skip(?:ping)?|ignore|ignoring|no need for|don'?t need|do not need|not need)\s+(?:a\s+|any\s+|the\s+)?"
    r"(?:migrations?|backwards?[- ]compat\w*|compatibility|deprecation)\b"
    r"|\bbreaking changes? (?:are|is) (?:fine|ok|allowed)\b"
)
# A line that forbids or describes the other stage is not a permission.
SKIP_COMPAT_EXEMPT_RE = re.compile(r"(?i)\b(?:never|must not|do not skip|don'?t skip|not allowed|forbidden|greenfield|unless|rather than)\b")


def lessons_lines(text: str) -> Optional[List[str]]:
    """The `- ` lines inside `## Lessons`, or None when the section is missing."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if LESSONS_HEADING_RE.match(l)), None)
    if start is None:
        return None
    out: List[str] = []
    for l in lines[start + 1:]:
        if l.startswith("## "):
            break
        if l.startswith("- "):
            out.append(l)
    return out


def audit_policy_text(repo: str, ref: str, path: str, text: str) -> List[Finding]:
    """Checks (f)-(h) on one policy file. Pure: no network."""
    url = f"https://github.com/{repo}/blob/{ref}/{path}"
    where = {"repo": repo, "ref": ref, "file": path}

    def finding(category: str, **details: Any) -> Finding:
        return Finding(category=category, issue_number=0, title=f"{repo}@{ref} {path}", url=url,
                       details={**where, **details})

    out: List[Finding] = []
    stages = STAGE_LINE_RE.findall(text)
    if len(stages) != 1 or stages[0].lower() not in ("greenfield", "live"):
        out.append(finding("stage_line_invalid", stage_lines=stages))
    lessons = lessons_lines(text)
    if lessons is None:
        out.append(finding("lessons_section_invalid", problem="no '## Lessons' section"))
    elif len(lessons) > POLICY_LESSON_CAP:
        out.append(finding("lessons_section_invalid", problem=f"{len(lessons)} lessons, cap {POLICY_LESSON_CAP}"))
    if len(stages) == 1 and stages[0].lower() == "live":
        for line in text.splitlines():
            if SKIP_COMPAT_RE.search(line) and not SKIP_COMPAT_EXEMPT_RE.search(line):
                out.append(finding("live_repo_permits_skipping_compat", line=line.strip()[:200]))
    return out


def audit_policy_docs(
    repos: List[str], client: Optional[GitHubClient] = None
) -> Tuple[List[Finding], int]:
    """Checks (f)-(h) over `owner/repo@branch` entries. Read-only. Returns (findings, repos scanned)."""
    client = client or GitHubClient()
    findings: List[Finding] = []
    for entry in repos:
        repo, _, ref = entry.partition("@")
        ref = ref or "main"
        for path in POLICY_FILES:
            text = client.get_file(repo, path, ref)
            if text is not None and (path == POLICY_FILES[0] or LESSONS_HEADING_RE.search(text) or STAGE_LINE_RE.search(text)):
                findings.extend(audit_policy_text(repo, ref, path, text))
                break
        else:
            findings.append(Finding(
                category="stage_line_invalid", issue_number=0, title=f"{repo}@{ref}",
                url=f"https://github.com/{repo}/tree/{ref}",
                details={"repo": repo, "ref": ref, "problem": "no AGENTS.md or CLAUDE.md"},
            ))
    return findings, len(repos)


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
    parser.add_argument(
        "--premature-closes",
        action="store_true",
        help="Also run check (e): issues closed in the last --close-days days with unchecked boxes, "
        "closed under 10 min after creation, or plan issues closed with no sub-issue",
    )
    parser.add_argument(
        "--only-premature-closes",
        action="store_true",
        help="Run only check (e) (read-only); skips checks (a)-(d)",
    )
    parser.add_argument(
        "--close-repos",
        default=None,
        help="Comma-separated owner/repo list for check (e) (default: --repo)",
    )
    parser.add_argument(
        "--close-days",
        type=int,
        default=PREMATURE_CLOSE_DAYS,
        help=f"Look-back window in days for check (e) (default: {PREMATURE_CLOSE_DAYS})",
    )

    parser.add_argument(
        "--policy-docs",
        action="store_true",
        help="Also run checks (f)-(h): project stage line, Lessons cap, live-repo compatibility text",
    )
    parser.add_argument(
        "--only-policy-docs",
        action="store_true",
        help="Run only checks (f)-(h) (read-only)",
    )
    parser.add_argument(
        "--policy-repos",
        default=",".join(POLICY_REPOS_DEFAULT),
        help="Comma-separated owner/repo@branch list for checks (f)-(h)",
    )

    args = parser.parse_args()
    policy_repos = [r.strip() for r in args.policy_repos.split(",") if r.strip()]

    close_repos = [r.strip() for r in args.close_repos.split(",") if r.strip()] if args.close_repos else [args.repo]
    if args.only_policy_docs:
        pd_findings, pd_scanned = audit_policy_docs(policy_repos)
        result = AuditResult(
            repo=",".join(policy_repos),
            timestamp=datetime.now(timezone.utc).isoformat(),
            status="fail" if pd_findings else "pass",
            summary=AuditSummary(
                total_issues_scanned=pd_scanned,
                total_findings=len(pd_findings),
                policy_doc_violations=len(pd_findings),
            ),
            findings=pd_findings,
        )
    elif args.only_premature_closes:
        pc_findings, pc_scanned = audit_premature_closes(close_repos, days=args.close_days)
        result = AuditResult(
            repo=",".join(close_repos),
            timestamp=datetime.now(timezone.utc).isoformat(),
            status="fail" if pc_findings else "pass",
            summary=AuditSummary(
                total_issues_scanned=pc_scanned,
                total_findings=len(pc_findings),
                premature_closes=len(pc_findings),
            ),
            findings=pc_findings,
        )
    else:
        result = run_adoption_audit(
            repo=args.repo,
            issue_number=args.issue,
            auto_reopen=args.auto_reopen,
            dry_run=args.dry_run,
            premature_close_repos=close_repos if args.premature_closes else None,
            premature_close_days=args.close_days,
        )

    if args.policy_docs and not args.only_policy_docs:
        pd_findings, _ = audit_policy_docs(policy_repos)
        result.findings.extend(pd_findings)
        result.summary.policy_doc_violations = len(pd_findings)
        result.summary.total_findings += len(pd_findings)
        result.status = "fail" if result.findings else "pass"

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(format_markdown_report(result))

    # Exit non-zero on findings per requirement
    sys.exit(1 if result.findings else 0)


if __name__ == "__main__":
    main()
