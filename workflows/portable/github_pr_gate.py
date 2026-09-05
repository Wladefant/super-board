#!/usr/bin/env python3
"""
Deterministic GitHub PR Status & Review Gate Helper
Location: ~/.veyyon/workflows/github_pr_gate.py

Provides a deterministic status gate helper to replace non-deterministic LLM final gates:
  1. Deterministic CI status rollup verification (all required checks must succeed).
  2. Independent review approval verification (pinned to PR head commit, no self-approvals).
  3. Head/base/contract-bound review reuse:
     - Reuses verified review when head commit SHA and base are unchanged.
     - Automatically expires/invalidates review if:
         * PR head commit changed (new push).
         * New CI or security check failure occurred after review approval.
         * New security finding/alert flagged.
  4. Zero LLM gate churn: performs evaluations via deterministic rule logic.
"""

import argparse
import datetime
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class CheckRunStatus:
    name: str
    status: str       # COMPLETED, IN_PROGRESS, QUEUED, PENDING
    conclusion: str   # SUCCESS, FAILURE, NEUTRAL, SKIPPED, TIMED_OUT, CANCELLED
    started_at: str = ""
    completed_at: str = ""
    details_url: str = ""


# Bases whose approval requirement can NEVER be waived by configuration. A production
# branch must always demand an independent human GitHub approval; a config file that tries
# to relax one is rejected rather than honoured.
PRODUCTION_PROTECTED_BASES: Dict[str, List[str]] = {
    "Bavariance/polysimulator": ["main", "master", "production", "prod"],
}


@dataclass
class GateApprovalPolicy:
    """
    Per-repository, per-base-branch gate policy.

    GitHub's own required-review enforcement is unavailable on these repositories
    (super-board main is unprotected; the polysimulator plan returns 403 for the branch
    protection API), so a universal 'a human must click Approve' rule is our software
    policy alone. On an automated staging branch driven by a single authenticated identity
    that rule cannot be satisfied at all, so it is configurable per base.

    Waiving the GitHub approval never waives review: head-bound independent review evidence
    is still required, self-approval still never counts, and CI still gates.
    """
    repo: str = "*"
    base_ref: str = "*"
    require_github_approval: bool = True
    require_head_bound_review_evidence: bool = True
    advisory_checks: List[str] = field(default_factory=list)
    rationale: str = ""

    def matches(self, repo: str, base_ref: str) -> bool:
        repo_ok = self.repo in ("*", repo)
        base_ok = self.base_ref in ("*", base_ref)
        return repo_ok and base_ok


# Explicit policy table. The default entry is strict; every relaxation is named.
DEFAULT_GATE_POLICIES: List[GateApprovalPolicy] = [
    GateApprovalPolicy(
        repo="Bavariance/polysimulator",
        base_ref="staging",
        require_github_approval=False,
        require_head_bound_review_evidence=True,
        rationale=(
            "Automated staging integration runs under one authenticated identity, which is "
            "also the PR author, so a non-author GitHub approval is unobtainable. Exact-head "
            "independent automated review evidence is required instead."
        ),
    ),
    GateApprovalPolicy(
        repo="Wladefant/super-board",
        base_ref="main",
        require_github_approval=False,
        require_head_bound_review_evidence=True,
        # Named individually, never a wildcard over all checks: these two jobs fail
        # identically on base main (ddb85b45, run 33019898958, same failing steps), so they
        # are inherited and a PR cannot regress them. Every other check still blocks.
        advisory_checks=[
            "claudex PowerShell fixtures",
            "claudex zero-quota integration*",
        ],
        rationale=(
            "Workflow tooling repository, operator-designated non-production. Same single "
            "authenticated identity constraint; head-bound independent review evidence required. "
            "The two claudex jobs are advisory because they fail identically on base main and "
            "are not caused by any PR."
        ),
    ),
    GateApprovalPolicy(rationale="Default: independent non-author GitHub approval required."),
]


def resolve_gate_policy(
    repo: str,
    base_ref: str,
    config_path: Optional[str] = None,
) -> GateApprovalPolicy:
    """
    Resolve the gate policy for a repo/base pair, most specific entry first.

    A config file may add or override entries, but any attempt to waive approval on a
    production-protected base is refused and forced back to strict.
    """
    policies: List[GateApprovalPolicy] = []
    if config_path:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for entry in raw.get("policies", []):
            policies.append(
                GateApprovalPolicy(
                    repo=entry.get("repo", "*"),
                    base_ref=entry.get("base_ref", "*"),
                    require_github_approval=bool(entry.get("require_github_approval", True)),
                    require_head_bound_review_evidence=bool(
                        entry.get("require_head_bound_review_evidence", True)
                    ),
                    advisory_checks=list(entry.get("advisory_checks", [])),
                    rationale=entry.get("rationale", ""),
                )
            )
    policies.extend(DEFAULT_GATE_POLICIES)

    # Most specific match wins: exact repo and base, then repo, then default.
    def specificity(p: GateApprovalPolicy) -> int:
        return (0 if p.repo == "*" else 2) + (0 if p.base_ref == "*" else 1)

    candidates = [p for p in policies if p.matches(repo, base_ref)]
    if not candidates:
        return GateApprovalPolicy()
    policy = sorted(candidates, key=specificity, reverse=True)[0]

    protected = PRODUCTION_PROTECTED_BASES.get(repo, [])
    if base_ref in protected and not policy.require_github_approval:
        return GateApprovalPolicy(
            repo=repo,
            base_ref=base_ref,
            require_github_approval=True,
            require_head_bound_review_evidence=True,
            advisory_checks=policy.advisory_checks,
            rationale=(
                f"Refused to waive approval on production-protected base '{base_ref}' of "
                f"{repo}; forced back to requiring independent human approval."
            ),
        )
    return policy


def fetch_required_contexts(repo: str, base_ref: str, timeout_sec: int = 25) -> Optional[List[str]]:
    """
    Native required status check contexts for a base branch.

    Returns None when GitHub cannot tell us: an unprotected branch (404) or a plan without
    branch protection (403). None means 'no native requirement data', which is treated as
    'every check blocks' rather than 'nothing blocks'.
    """
    res = _run_gh(
        [
            "gh", "api",
            f"repos/{repo}/branches/{base_ref}/protection/required_status_checks",
            "--jq", ".contexts // []",
        ],
        timeout_sec,
    )
    if res.returncode != 0:
        return None
    try:
        return list(json.loads(res.stdout or "[]"))
    except json.JSONDecodeError:
        return None


@dataclass
class ReviewRecord:
    author: str
    state: str        # APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED
    submitted_at: str
    commit_sha: str


@dataclass
class PRGateEvaluation:
    pr_number: int
    repo: str
    state: str                     # OPEN, MERGED, CLOSED
    is_draft: bool
    head_sha: str
    base_sha: str
    ci_verdict: str                # SUCCESS, FAILURE, PENDING
    failing_checks: List[str]
    pending_checks: List[str]
    approval_verdict: str          # APPROVED, UNAPPROVED, SELF_APPROVED_ONLY
    approved_by: Optional[str]
    review_reused: bool
    review_invalidated: bool
    invalidation_reason: Optional[str]
    gate_verdict: str              # PASSED, BLOCKED, PENDING
    verdict_reason: str
    checked_at_utc: str = ""
    base_ref: str = ""
    advisory_failing_checks: List[str] = field(default_factory=list)
    github_approval_required: bool = True
    approval_policy_rationale: str = ""
    native_required_contexts: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_compact_markdown(self) -> str:
        failing_str = ", ".join(self.failing_checks) if self.failing_checks else "None"
        pending_str = ", ".join(self.pending_checks) if self.pending_checks else "None"
        return (
            f"### Deterministic PR Gate Evaluation: PR #{self.pr_number} ({self.gate_verdict})\n"
            f"- **Head SHA:** `{self.head_sha[:8]}` (Base: `{self.base_sha[:8]}`)\n"
            f"- **State:** `{self.state}` (Draft: `{self.is_draft}`)\n"
            f"- **CI Status:** `{self.ci_verdict}` (Failing: {failing_str}, Pending: {pending_str})\n"
            f"- **Approval:** `{self.approval_verdict}` (By: `{self.approved_by or 'None'}`, "
            f"GitHub approval required: `{self.github_approval_required}`)\n"
            f"- **Advisory failures:** {', '.join(self.advisory_failing_checks) or 'None'}\n"
            f"- **Review Reused:** `{self.review_reused}` (Invalidated: `{self.review_invalidated}`"
            f"{f' - {self.invalidation_reason}' if self.invalidation_reason else ''})\n"
            f"- **Verdict:** **{self.gate_verdict}** — {self.verdict_reason}\n"
        )

def _run_gh(cmd: List[str], timeout_sec: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        shell=True if sys.platform == "win32" else False,
    )


def fetch_base_sha(pr_number: int, repo: str, timeout_sec: int = 25) -> str:
    """
    Resolve the PR base commit SHA.

    `gh pr view --json` exposes no base SHA field at all (only `baseRefName`), so the
    base OID must come from the REST endpoint. Returns "" when it cannot be resolved,
    which the evaluator reports rather than silently treating as "base unchanged".
    """
    res = _run_gh(
        ["gh", "api", f"repos/{repo}/pulls/{pr_number}", "--jq", ".base.sha"],
        timeout_sec,
    )
    if res.returncode != 0:
        return ""
    return res.stdout.strip()


def fetch_pr_json(pr_number: int, repo: str = "Bavariance/polysimulator", timeout_sec: int = 25) -> Dict[str, Any]:
    """Fetch PR details from GitHub CLI, including the base SHA the JSON view cannot supply."""
    cmd = [
        "gh",
        "pr",
        "view",
        str(pr_number),
        "--repo",
        repo,
        "--json",
        "number,state,isDraft,headRefOid,baseRefName,reviews,statusCheckRollup,author",
    ]

    res = _run_gh(cmd, timeout_sec)
    if res.returncode != 0:
        raise RuntimeError(f"gh pr view failed (exit {res.returncode}): {res.stderr.strip()}")

    data = json.loads(res.stdout)
    data["baseRefOid"] = fetch_base_sha(pr_number, repo, timeout_sec)
    return data

def evaluate_pr_gate(
    pr_data: Dict[str, Any],
    repo: str = "Bavariance/polysimulator",
    prior_review_record: Optional[Dict[str, Any]] = None,
    security_alerts: Optional[List[Dict[str, Any]]] = None,
    expected_head_sha: Optional[str] = None,
    policy: Optional[GateApprovalPolicy] = None,
    native_required_contexts: Optional[List[str]] = None,
) -> PRGateEvaluation:
    """
    Deterministically evaluates GitHub PR status gate without LLM churn.

    When `expected_head_sha` is supplied, the live head must equal it; a mismatch is a
    hard BLOCK, because every QA and review artifact is bound to one exact head SHA.

    `policy` decides whether a non-author GitHub APPROVED review is mandatory for this
    repo/base. Waiving it never waives review: head-bound independent review evidence is
    still required and self-approval still never counts.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base_ref = str(pr_data.get("baseRefName") or (pr_data.get("base") or {}).get("ref") or "")
    if policy is None:
        policy = resolve_gate_policy(repo, base_ref)

    pr_number = int(pr_data.get("number") or 0)
    state = str(pr_data.get("state") or "UNKNOWN").upper()
    is_draft = bool(pr_data.get("isDraft", False))
    head_sha = str(pr_data.get("headRefOid") or "")
    base_sha = str(
        pr_data.get("baseRefOid")
        or (pr_data.get("base") or {}).get("sha")
        or pr_data.get("base_sha")
        or ""
    )
    pr_author = (pr_data.get("author") or {}).get("login", "")

    # 0. Exact-head binding: refuse to evaluate a head other than the one asked about.
    if expected_head_sha and head_sha and expected_head_sha != head_sha:
        return PRGateEvaluation(
            pr_number=pr_number,
            repo=repo,
            state=state,
            is_draft=is_draft,
            head_sha=head_sha,
            base_sha=base_sha,
            ci_verdict="FAILURE",
            failing_checks=[],
            pending_checks=[],
            approval_verdict="UNAPPROVED",
            approved_by=None,
            review_reused=False,
            review_invalidated=True,
            invalidation_reason=(
                f"Expected head {expected_head_sha[:8]} but PR head is {head_sha[:8]}"
            ),
            gate_verdict="BLOCKED",
            verdict_reason=(
                f"Head mismatch: caller pinned {expected_head_sha[:8]}, live PR head is "
                f"{head_sha[:8]}. All prior QA/review evidence is invalidated by the new push."
            ),
            checked_at_utc=now_utc,
        )

    # 1. Draft Check
    if is_draft:
        return PRGateEvaluation(
            pr_number=pr_number,
            repo=repo,
            state=state,
            is_draft=is_draft,
            head_sha=head_sha,
            base_sha=base_sha,
            ci_verdict="PENDING",
            failing_checks=[],
            pending_checks=[],
            approval_verdict="UNAPPROVED",
            approved_by=None,
            review_reused=False,
            review_invalidated=False,
            invalidation_reason=None,
            gate_verdict="BLOCKED",
            verdict_reason="PR is marked as Draft. Ready for review must be set before promotion.",
            checked_at_utc=now_utc,
        )

    # 2. PR Open Check
    if state not in ("OPEN", "MERGED"):
        return PRGateEvaluation(
            pr_number=pr_number,
            repo=repo,
            state=state,
            is_draft=is_draft,
            head_sha=head_sha,
            base_sha=base_sha,
            ci_verdict="FAILURE",
            failing_checks=[],
            pending_checks=[],
            approval_verdict="UNAPPROVED",
            approved_by=None,
            review_reused=False,
            review_invalidated=False,
            invalidation_reason=None,
            gate_verdict="BLOCKED",
            verdict_reason=f"PR state is '{state}', expected 'OPEN'.",
            checked_at_utc=now_utc,
        )

    # 3. Status Checks Rollup Verification
    status_rollup = pr_data.get("statusCheckRollup") or []
    failing_checks: List[str] = []
    advisory_failing_checks: List[str] = []
    pending_checks: List[str] = []
    latest_ci_failure_time = None

    def is_blocking(check_name: str) -> bool:
        """
        Whether a failure of this check blocks the gate.

        Native required contexts win when GitHub supplies them. When it does not (an
        unprotected branch, or a plan without branch protection) every check blocks unless
        the policy names it advisory explicitly. Absence of native data never means
        'nothing blocks'.
        """
        for pattern in policy.advisory_checks:
            if fnmatch.fnmatch(check_name, pattern):
                return False
        if native_required_contexts is not None:
            return check_name in native_required_contexts
        return True

    for check in status_rollup:
        # Check either CheckRun or StatusContext
        c_name = check.get("name") or check.get("context") or "unknown_check"
        c_status = str(check.get("status") or "").upper()
        c_conclusion = str(check.get("conclusion") or check.get("state") or "").upper()
        c_completed_at = check.get("completedAt") or check.get("createdAt")

        if c_conclusion in ("FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED", "STARTUP_FAILURE"):
            if is_blocking(c_name):
                failing_checks.append(c_name)
                if c_completed_at and (latest_ci_failure_time is None or c_completed_at > latest_ci_failure_time):
                    latest_ci_failure_time = c_completed_at
            else:
                advisory_failing_checks.append(c_name)
        elif c_status in ("IN_PROGRESS", "QUEUED", "PENDING", "EXPECTED"):
            if is_blocking(c_name):
                pending_checks.append(c_name)

    if failing_checks:
        ci_verdict = "FAILURE"
    elif pending_checks:
        ci_verdict = "PENDING"
    else:
        ci_verdict = "SUCCESS"

    # 4. Review & Approval Verification (with Head-Binding and Reuse Invalidation)
    reviews_list = pr_data.get("reviews") or []
    valid_approvers = []
    self_approvers = []
    latest_approval_time = None
    approval_commit_sha = None

    for r in reviews_list:
        r_author = (r.get("author") or {}).get("login", "")
        r_state = str(r.get("state") or "").upper()
        r_commit = (r.get("commit") or {}).get("oid") or r.get("commitRefOid") or ""
        r_time = r.get("submittedAt")

        if r_author == pr_author:
            # PR author review: whether APPROVED, COMMENTED, or DISMISSED, it can NEVER approve the PR!
            if r_state in ("APPROVED", "COMMENTED"):
                self_approvers.append(r_author)
            continue

        if r_state == "APPROVED":
            # Strict head binding: an approval whose commit OID is unknown or points at any
            # other commit is NOT evidence for this head and must never count as approval.
            if r_commit and r_commit == head_sha:
                valid_approvers.append(r_author)
                if r_time and (latest_approval_time is None or r_time > latest_approval_time):
                    latest_approval_time = r_time
                    approval_commit_sha = r_commit
    review_reused = False
    review_invalidated = False
    invalidation_reason = None

    # Check prior review record for reuse / invalidation
    if prior_review_record:
        prior_head = prior_review_record.get("commit_sha")
        prior_base = prior_review_record.get("base_sha")
        prior_status = str(prior_review_record.get("status") or "").lower()
        prior_approved = prior_status in ("approved", "pass", "passed")
        prior_time = prior_review_record.get("submitted_at")
        prior_reviewer = prior_review_record.get("approved_by")

        if prior_approved:
            if prior_reviewer and prior_reviewer == pr_author:
                self_approvers.append(prior_reviewer)
                review_invalidated = True
                invalidation_reason = f"Prior review was a self-approval submitted by PR author ({pr_author}); independent review approval required"
            elif prior_head != head_sha:
                review_invalidated = True
                invalidation_reason = f"PR head changed from {prior_head[:8] if prior_head else 'none'} to {head_sha[:8]}"
            elif prior_base and prior_base != base_sha:
                # Base moved under the review: the merge result is no longer what was approved.
                review_invalidated = True
                invalidation_reason = (
                    f"Base changed from {prior_base[:8]} to "
                    f"{base_sha[:8] if base_sha else 'unresolved'}"
                )
            elif prior_base and not base_sha:
                review_invalidated = True
                invalidation_reason = (
                    f"Prior review was bound to base {prior_base[:8]} but the current base SHA "
                    "could not be resolved, so base-unchanged cannot be proven"
                )
            elif latest_ci_failure_time and prior_time and latest_ci_failure_time > prior_time:
                review_invalidated = True
                invalidation_reason = f"New CI failure occurred after prior review approval ({latest_ci_failure_time} > {prior_time})"
            elif security_alerts:
                # Check for active security alerts created after approval
                new_alerts = [a for a in security_alerts if a.get("created_at", "") > (prior_time or "")]
                if new_alerts:
                    review_invalidated = True
                    invalidation_reason = f"{len(new_alerts)} new security alert(s) detected after review approval"
            else:
                # Reuse verified prior review!
                review_reused = True
                if not valid_approvers and prior_reviewer and prior_reviewer != pr_author:
                    valid_approvers.append(prior_reviewer)

    if valid_approvers:
        approval_verdict = "APPROVED"
        approved_by = valid_approvers[-1]
    elif self_approvers and not valid_approvers:
        approval_verdict = "SELF_APPROVED_ONLY"
        approved_by = None
    else:
        approval_verdict = "UNAPPROVED"
        approved_by = None

    # Head-bound independent review evidence, independent of GitHub's approve button. This
    # is what still has to hold when the GitHub approval requirement is waived.
    has_head_bound_review_evidence = bool(valid_approvers) and not review_invalidated

    # 5. Final Gate Verdict
    if ci_verdict == "FAILURE":
        gate_verdict = "BLOCKED"
        verdict_reason = f"CI status check(s) failed: {', '.join(failing_checks)}"
    elif ci_verdict == "PENDING":
        gate_verdict = "PENDING"
        verdict_reason = f"CI check(s) currently pending/in-progress: {', '.join(pending_checks)}"
    elif review_invalidated:
        gate_verdict = "BLOCKED"
        verdict_reason = f"Prior review invalidated: {invalidation_reason}"
    elif approval_verdict == "SELF_APPROVED_ONLY":
        # Never policy-dependent: an author approving their own work is not review.
        gate_verdict = "BLOCKED"
        verdict_reason = f"Self-approval rejected (PR author {pr_author}); independent review required."
    elif policy.require_github_approval and approval_verdict == "UNAPPROVED":
        gate_verdict = "BLOCKED"
        verdict_reason = f"No head-bound review approval found for commit {head_sha[:8]}."
    elif (
        not policy.require_github_approval
        and policy.require_head_bound_review_evidence
        and not has_head_bound_review_evidence
    ):
        gate_verdict = "BLOCKED"
        verdict_reason = (
            f"GitHub approval is not required for {repo}@{base_ref or 'unknown'}, but no "
            f"independent head-bound review evidence exists for commit {head_sha[:8]}."
        )
    else:
        gate_verdict = "PASSED"
        if policy.require_github_approval:
            verdict_reason = (
                f"All required CI checks succeeded and independent review approval verified for head "
                f"{head_sha[:8]} (approved by {approved_by}{', review reused' if review_reused else ''})."
            )
        else:
            verdict_reason = (
                f"All required CI checks succeeded and independent head-bound review evidence verified "
                f"for head {head_sha[:8]} (reviewer {approved_by}"
                f"{', review reused' if review_reused else ''}); GitHub approval not required for "
                f"{repo}@{base_ref or 'unknown'}."
            )
        if advisory_failing_checks:
            verdict_reason += f" Advisory (non-blocking) failures: {', '.join(advisory_failing_checks)}."

    return PRGateEvaluation(
        pr_number=pr_number,
        repo=repo,
        state=state,
        is_draft=is_draft,
        head_sha=head_sha,
        base_sha=base_sha,
        ci_verdict=ci_verdict,
        failing_checks=failing_checks,
        pending_checks=pending_checks,
        approval_verdict=approval_verdict,
        approved_by=approved_by,
        review_reused=review_reused,
        review_invalidated=review_invalidated,
        invalidation_reason=invalidation_reason,
        gate_verdict=gate_verdict,
        verdict_reason=verdict_reason,
        checked_at_utc=now_utc,
        base_ref=base_ref,
        advisory_failing_checks=advisory_failing_checks,
        github_approval_required=policy.require_github_approval,
        approval_policy_rationale=policy.rationale,
        native_required_contexts=native_required_contexts,
    )


def parse_pr_ref(value: str) -> Tuple[int, Optional[str]]:
    """
    Accept either a bare PR number or a full GitHub PR URL.

    Returns (pr_number, repo_or_None). A URL also yields its owner/repo so the
    caller does not have to restate --repo for a cross-repository PR.
    """
    raw = str(value).strip()
    if raw.isdigit():
        return int(raw), None

    match = re.search(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)", raw)
    if match:
        return int(match.group(3)), f"{match.group(1)}/{match.group(2)}"

    raise argparse.ArgumentTypeError(
        f"Invalid PR reference '{value}': expected a PR number or a github.com/<owner>/<repo>/pull/<n> URL."
    )


def main():
    parser = argparse.ArgumentParser(description="Deterministic GitHub PR Status & Review Gate CLI")
    parser.add_argument(
        "pr_positional",
        nargs="?",
        default=None,
        metavar="PR",
        help="GitHub PR number or PR URL",
    )
    parser.add_argument(
        "--pr",
        dest="pr_flag",
        default=None,
        help="GitHub PR number or PR URL (equivalent to the positional form)",
    )
    parser.add_argument("--repo", default="Bavariance/polysimulator", help="GitHub repository (owner/repo)")
    parser.add_argument(
        "--head-sha",
        default=None,
        help="Pin evaluation to this exact 40-char head SHA; a live head mismatch is a hard BLOCK",
    )
    parser.add_argument(
        "--policy-config",
        default=None,
        help="Path to a JSON gate policy file whose entries override the built-in table",
    )
    parser.add_argument("--json", action="store_true", help="Output evaluation as JSON")
    args = parser.parse_args()

    pr_ref = args.pr_flag if args.pr_flag is not None else args.pr_positional
    if pr_ref is None:
        parser.error("a PR is required: pass it positionally or with --pr")

    try:
        pr_number, url_repo = parse_pr_ref(pr_ref)
    except argparse.ArgumentTypeError as e:
        parser.error(str(e))

    repo = url_repo or args.repo

    try:
        pr_data = fetch_pr_json(pr_number=pr_number, repo=repo)
        base_ref = str(pr_data.get("baseRefName") or "")
        policy = resolve_gate_policy(repo, base_ref, config_path=args.policy_config)
        eval_result = evaluate_pr_gate(
            pr_data=pr_data,
            repo=repo,
            expected_head_sha=args.head_sha,
            policy=policy,
            native_required_contexts=fetch_required_contexts(repo, base_ref) if base_ref else None,
        )
    except Exception as e:
        sys.stderr.write(f"PR Gate evaluation failed: {e}\n")
        sys.exit(1)

    if args.json:
        print(json.dumps(eval_result.to_dict(), indent=2))
    else:
        print(eval_result.to_compact_markdown())

    if eval_result.gate_verdict != "PASSED":
        sys.exit(2)


if __name__ == "__main__":
    main()
