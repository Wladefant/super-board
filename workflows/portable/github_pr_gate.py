#!/usr/bin/env python3
"""
Deterministic GitHub PR Status & Review Gate Helper
Location: ~/.veyyon/workflows/github_pr_gate.py

Provides a deterministic status gate helper to replace non-deterministic LLM final gates:
  1. Deterministic CI status rollup verification (all required checks must succeed).
  2. Independent GitHub approval or independently-produced automated review artifact
     verification, pinned to the PR head and base with no self-approvals.
  3. Head/base/contract-bound review reuse:
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




REVIEW_ARTIFACT_SCHEMA = "portable-review/v1"
REVIEW_ARTIFACT_TYPE = "independent_automated_code_review"
SHA40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SOURCE_URI_RE = re.compile(r"^(agent|history)://([A-Za-z0-9][A-Za-z0-9_.:-]*)$")


def validate_review_artifact(
    record: Dict[str, Any],
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    pr_author: str,
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """
    Validate a trusted-workflow automated review artifact.

    This establishes an explicit, source-backed contract and exact subject bindings. It is
    advisory local workflow evidence, not a cryptographic identity proof: the workflow
    supplying the JSON remains responsible for authenticating and retaining the source.
    """
    if not isinstance(record, dict):
        return None, "Review artifact must be a JSON object"
    if record.get("schema") != REVIEW_ARTIFACT_SCHEMA:
        return None, f"Review artifact schema must be '{REVIEW_ARTIFACT_SCHEMA}'"
    if record.get("artifact_type") != REVIEW_ARTIFACT_TYPE:
        return None, f"Review artifact type must be '{REVIEW_ARTIFACT_TYPE}'"
    if record.get("repository") != repo or record.get("pull_request") != pr_number:
        return None, "Review artifact repository or pull request does not match the live PR"

    artifact_head = record.get("head_sha")
    artifact_base = record.get("base_sha")
    if not isinstance(artifact_head, str) or not SHA40_RE.fullmatch(artifact_head):
        return None, "Review artifact head_sha must be a full 40-character hexadecimal SHA"
    if not isinstance(artifact_base, str) or not SHA40_RE.fullmatch(artifact_base):
        return None, "Review artifact base_sha must be a full 40-character hexadecimal SHA"
    if artifact_head.lower() != head_sha.lower():
        return None, f"Review artifact head {artifact_head[:8]} does not match live head {head_sha[:8]}"
    if not base_sha or artifact_base.lower() != base_sha.lower():
        return None, (
            f"Review artifact base {artifact_base[:8]} does not match live base "
            f"{base_sha[:8] if base_sha else 'unresolved'}"
        )

    subject = record.get("subject")
    reviewer = record.get("reviewer")
    source = record.get("source")
    if not isinstance(subject, dict) or subject.get("author_login") != pr_author:
        return None, "Review artifact subject.author_login must match the live PR author"
    if not isinstance(reviewer, dict):
        return None, "Review artifact reviewer must be an object"
    actor_id = reviewer.get("actor_id")
    if (
        not isinstance(actor_id, str)
        or not actor_id.strip()
        or reviewer.get("actor_type") != "automation"
    ):
        return None, "Review artifact requires a non-empty automation reviewer.actor_id"
    if actor_id.casefold() == pr_author.casefold():
        return None, "Review artifact reviewer is the PR author; independent review is required"

    if not isinstance(source, dict) or source.get("kind") != "agent_transcript":
        return None, "Review artifact source.kind must be 'agent_transcript'"
    source_uri = source.get("uri")
    source_match = SOURCE_URI_RE.fullmatch(source_uri) if isinstance(source_uri, str) else None
    if not source_match:
        return None, "Review artifact source.uri must be an agent:// or history:// transcript URI"
    producer_id = source.get("producer_id")
    if producer_id != actor_id or source_match.group(2) != actor_id:
        return None, "Review artifact source producer and transcript actor must match reviewer.actor_id"
    source_digest = source.get("sha256")
    if not isinstance(source_digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", source_digest):
        return None, "Review artifact source.sha256 must be a 64-character hexadecimal digest"

    submitted_at = record.get("submitted_at")
    if not isinstance(submitted_at, str):
        return None, "Review artifact submitted_at must be an RFC3339 timestamp"
    try:
        parsed_time = datetime.datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
    except ValueError:
        return None, "Review artifact submitted_at must be an RFC3339 timestamp"
    if parsed_time.tzinfo is None:
        return None, "Review artifact submitted_at must include a timezone"

    outcome = str(record.get("outcome") or "").lower()
    if outcome not in ("approved", "changes_requested"):
        return None, "Review artifact outcome must be 'approved' or 'changes_requested'"
    return {
        "actor_id": actor_id,
        "outcome": outcome,
        "submitted_at": submitted_at,
        "source_uri": source_uri,
    }, None


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
    approval_verdict: str          # APPROVED, AUTOMATED_REVIEW_APPROVED, UNAPPROVED
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
    review_artifact: Optional[Dict[str, Any]] = None,
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
    repo/base. On a named automated branch, a valid `portable-review/v1` artifact may
    satisfy independent review without being represented as GitHub approval. The artifact
    is advisory trusted-workflow evidence, not a cryptographic identity proof.
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

    # 4. GitHub approval and independent automated review artifact verification.
    reviews_list = pr_data.get("reviews") or []
    valid_github_approvers: List[str] = []
    self_approvers: List[str] = []
    changes_requesters: List[str] = []

    for review in reviews_list:
        review_author = (review.get("author") or {}).get("login", "")
        review_state = str(review.get("state") or "").upper()
        review_commit = (
            (review.get("commit") or {}).get("oid") or review.get("commitRefOid") or ""
        )

        if review_author == pr_author:
            if review_state in ("APPROVED", "COMMENTED"):
                self_approvers.append(review_author)
            continue
        if review_commit != head_sha:
            continue
        if review_state == "APPROVED":
            valid_github_approvers.append(review_author)
        elif review_state == "CHANGES_REQUESTED":
            changes_requesters.append(review_author)

    artifact_evidence: Optional[Dict[str, str]] = None
    review_invalidated = False
    invalidation_reason = None
    review_reused = False
    if review_artifact is not None:
        artifact_evidence, artifact_error = validate_review_artifact(
            review_artifact,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            base_sha=base_sha,
            pr_author=pr_author,
        )
        if artifact_error:
            review_invalidated = True
            invalidation_reason = artifact_error
        elif artifact_evidence:
            artifact_time = artifact_evidence["submitted_at"]
            if latest_ci_failure_time and latest_ci_failure_time > artifact_time:
                review_invalidated = True
                invalidation_reason = (
                    "New CI failure occurred after automated review "
                    f"({latest_ci_failure_time} > {artifact_time})"
                )
            elif security_alerts:
                new_alerts = [
                    alert
                    for alert in security_alerts
                    if alert.get("created_at", "") > artifact_time
                ]
                if new_alerts:
                    review_invalidated = True
                    invalidation_reason = (
                        f"{len(new_alerts)} new security alert(s) detected after automated review"
                    )
            if not review_invalidated:
                review_reused = True
                if artifact_evidence["outcome"] == "changes_requested":
                    changes_requesters.append(artifact_evidence["actor_id"])

    artifact_approved = bool(
        artifact_evidence
        and artifact_evidence["outcome"] == "approved"
        and not review_invalidated
    )
    if valid_github_approvers:
        approval_verdict = "APPROVED"
        approved_by = valid_github_approvers[-1]
    elif artifact_approved:
        approval_verdict = "AUTOMATED_REVIEW_APPROVED"
        approved_by = artifact_evidence["actor_id"] if artifact_evidence else None
    elif self_approvers:
        approval_verdict = "SELF_APPROVED_ONLY"
        approved_by = None
    else:
        approval_verdict = "UNAPPROVED"
        approved_by = None

    has_head_bound_review_evidence = bool(valid_github_approvers) or artifact_approved

    # 5. Final Gate Verdict
    if ci_verdict == "FAILURE":
        gate_verdict = "BLOCKED"
        verdict_reason = f"CI status check(s) failed: {', '.join(failing_checks)}"
    elif ci_verdict == "PENDING":
        gate_verdict = "PENDING"
        verdict_reason = f"CI check(s) currently pending/in-progress: {', '.join(pending_checks)}"
    elif changes_requesters:
        gate_verdict = "BLOCKED"
        verdict_reason = (
            "Changes requested for the current head by independent reviewer(s): "
            f"{', '.join(changes_requesters)}."
        )
    elif review_invalidated:
        gate_verdict = "BLOCKED"
        verdict_reason = f"Automated review artifact rejected: {invalidation_reason}"
    elif approval_verdict == "SELF_APPROVED_ONLY":
        gate_verdict = "BLOCKED"
        verdict_reason = f"Self-approval rejected (PR author {pr_author}); independent review required."
    elif policy.require_github_approval and not valid_github_approvers:
        gate_verdict = "BLOCKED"
        verdict_reason = f"No GitHub APPROVED review pinned to commit {head_sha[:8]}."
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
                f"All required CI checks succeeded and independent GitHub approval verified for head "
                f"{head_sha[:8]} (approved by {approved_by})."
            )
        else:
            evidence_kind = (
                "GitHub approval" if valid_github_approvers else "automated review artifact"
            )
            verdict_reason = (
                f"All required CI checks succeeded and independent head/base-bound {evidence_kind} "
                f"verified for head {head_sha[:8]} (reviewer {approved_by}); GitHub approval not "
                f"required for {repo}@{base_ref or 'unknown'}."
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
    parser.add_argument(
        "--review-record",
        default=None,
        help=(
            "Path to a portable-review/v1 independent automated review artifact. "
            "Trusted workflow evidence only; not a cryptographic identity assertion."
        ),
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
        review_artifact = None
        if args.review_record:
            with open(args.review_record, "r", encoding="utf-8") as review_file:
                review_artifact = json.load(review_file)
        eval_result = evaluate_pr_gate(
            pr_data=pr_data,
            repo=repo,
            expected_head_sha=args.head_sha,
            review_artifact=review_artifact,
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
