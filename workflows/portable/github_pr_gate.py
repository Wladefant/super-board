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
import hashlib
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Mapping
try:
    from merge_policy import merge_first_enabled
except ImportError:
    _SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    if _SCRIPT_DIR not in sys.path:
        sys.path.insert(0, _SCRIPT_DIR)
    from merge_policy import merge_first_enabled
try:
    from verify import validate_verify_receipt
except ImportError:
    def validate_verify_receipt(
        receipt: Dict[str, Any],
        head_sha: str,
        expected_pr: Optional[int] = None,
        expected_repo: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        if not isinstance(receipt, dict):
            return False, "Receipt payload is not a valid JSON object."
        if receipt.get("schema_version") != "verify-receipt/v1":
            return False, f"Invalid receipt schema_version '{receipt.get('schema_version')}', expected 'verify-receipt/v1'."
        receipt_head = str(receipt.get("head_sha") or "")
        if receipt_head != head_sha:
            return False, f"Receipt head {receipt_head[:8]} does not match expected head {head_sha[:8]}."
        if expected_pr is not None and receipt.get("pr_number") is not None:
            if int(receipt["pr_number"]) != expected_pr:
                return False, f"Receipt PR #{receipt['pr_number']} does not match expected PR #{expected_pr}."
        status = str(receipt.get("status") or "").upper()
        if status != "PASSED":
            summary = receipt.get("summary") or "Scenario checks failed."
            return False, f"Verification receipt status is '{status}': {summary}"
        return True, None


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
    # Issue #195: small, low-risk diffs may skip independent review. Only named
    # non-production policies opt in; the strict default never does.
    allow_review_exemption: bool = False
    require_verify_receipt: bool = False
    rationale: str = ""

    def matches(self, repo: str, base_ref: str) -> bool:
        repo_ok = self.repo in ("*", repo)
        base_ok = self.base_ref in ("*", base_ref)
        return repo_ok and base_ok

    @staticmethod
    def is_production_protected(repo: str, base_ref: str) -> bool:
        return base_ref in PRODUCTION_PROTECTED_BASES.get(repo, [])


# Explicit policy table. The default entry is strict; every relaxation is named.
DEFAULT_GATE_POLICIES: List[GateApprovalPolicy] = [
    GateApprovalPolicy(
        repo="Bavariance/polysimulator",
        base_ref="staging",
        require_github_approval=False,
        require_head_bound_review_evidence=True,
        allow_review_exemption=True,
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
        allow_review_exemption=True,
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
    GateApprovalPolicy(
        repo="Wladefant/veyyon",
        base_ref="main",
        require_github_approval=False,
        require_head_bound_review_evidence=True,
        allow_review_exemption=True,
        rationale=(
            "Single authenticated identity on veyyon main; exact-head independent automated "
            "review evidence is required instead of an unobtainable non-author GitHub approval."
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
    if base_ref in protected:
        return GateApprovalPolicy(
            repo=repo,
            base_ref=base_ref,
            require_github_approval=True,
            require_head_bound_review_evidence=True,
            advisory_checks=[],
            rationale=(
                f"Refused to waive approval or CI checks on production-protected base '{base_ref}' of "
                f"{repo}; independent human approval and all CI checks remain required."
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
MAX_REVIEW_FUTURE_SKEW_SECONDS = 60

HIGH_RISK_DOMAINS = {"money", "billing", "auth", "concurrency", "migration"}

MONEY_PATH_RE = re.compile(
    r"(^|[/_.-])(money|billing|wallets?|ledgers?|payments?|stripe)([/_.-]|$)",
    re.IGNORECASE,
)
AUTH_PATH_RE = re.compile(
    r"(^|[/_.-])(auth|tokens?|rls|permissions?)([/_.-]|$)",
    re.IGNORECASE,
)
MIGRATION_PATH_RE = re.compile(
    r"alembic/|migrations?/|(^|[/_.-])(alembic|migrations?)([/_.-]|$)",
    re.IGNORECASE,
)

LOCKFILE_NAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
    "bun.lock",
    "poetry.lock",
    "pipfile.lock",
    "cargo.lock",
    "composer.lock",
    "go.sum",
    "flake.lock",
}

# Deploy-critical checks that must NEVER be timed out. When one of these is
# pending, the gate returns PENDING/BLOCKED — it never falls through to local
# verification gates. The 5-minute CI timeout (AGENTS.md §6) applies only to
# checks NOT in this list.  Names are matched with fnmatch so wildcards work.
DEPLOY_CRITICAL_CHECKS: List[str] = [
    "build-and-boot",
    "build-and-boot *",
    "backend build*",
    "docker build*",
    "No new workaround or band-aid comments*",
    "Anti-pattern structural linting (ast-grep)*",
    "No new fabricated render values*",
    "workaround-comments*",
    "ast-grep*",
]


def is_lockfile_or_generated(path: str) -> bool:
    norm = path.replace("\\", "/").lower()
    filename = norm.rsplit("/", 1)[-1]
    if filename in LOCKFILE_NAMES:
        return True
    if filename.endswith(".lock") or filename.endswith(".lockb"):
        return True
    if (
        filename.endswith(".min.js")
        or filename.endswith(".min.css")
        or filename.endswith(".map")
    ):
        return True
    if (
        ".generated." in filename
        or filename.endswith("_pb2.py")
        or filename.endswith("_pb2_grpc.py")
    ):
        return True
    if "/generated/" in norm or norm.startswith("generated/"):
        return True
    return False

DEPENDENCY_FIELDS: Tuple[str, ...] = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
    "bundledDependencies",
    "bundleDependencies",
    "overrides",
    "resolutions",
)


def validate_local_tests_record(
    record: Dict[str, Any],
    head_sha: str,
    require_tsc_and_tests: bool = False,
) -> Tuple[bool, str]:
    """Validate a local tests record against the evaluated PR head commit."""
    if not isinstance(record, dict):
        return False, "local tests record must be a JSON object"
    rec_sha = str(record.get("head_sha") or "")
    if not rec_sha or rec_sha.lower() != head_sha.lower():
        return False, f"head_sha mismatch: record has {rec_sha[:8]}, evaluated head is {head_sha[:8]}"
    failed = record.get("failed")
    if not isinstance(failed, int) or failed != 0:
        return False, f"local tests record has failures: failed={failed}"
    passed = record.get("passed")
    if not isinstance(passed, int) or passed <= 0:
        return False, f"local tests record invalid passed count: {passed}"
    commands = record.get("commands")
    if not isinstance(commands, list) or len(commands) == 0:
        return False, "local tests record commands must be a non-empty list"
    if require_tsc_and_tests:
        has_tsc = any("tsc" in str(cmd).lower() for cmd in commands)
        if not has_tsc:
            return False, "local tests record missing tsc command"
        has_tests = any(
            any(k in str(cmd).lower() for k in ("test", "pytest", "vitest", "jest"))
            for cmd in commands
        )
        if not has_tests:
            return False, "local tests record missing targeted tests command"
    return True, "valid local tests record"


def validate_ci_absent_local_tests(
    record: Dict[str, Any],
    head_sha: str,
) -> Tuple[bool, str]:
    """Validate that when CI is absent, exact-head local tsc and targeted tests are recorded with positive counts."""
    return validate_local_tests_record(record, head_sha, require_tsc_and_tests=True)


QA_RECEIPT_CHECK_NAMES = frozenset({
    "superboard/exact-sha-qa",
    "exact-sha-qa",
    "qa-receipt",
    "browser-qa",
    "browser qa",
    "flow-qa",
    "flow qa",
    "staging qa capture",
    "staging-qa-capture",
    "control glass",
    "control-glass",
})

QA_RECEIPT_CHECK_PATTERNS = (
    "superboard/exact-sha-qa*",
    "*exact-sha-qa*",
    "*qa-receipt*",
    "*browser-qa*",
    "*browser qa*",
    "*flow-qa*",
    "*flow qa*",
    "*staging qa capture*",
    "*staging-qa-capture*",
    "*control glass*",
    "*control-glass*",
)

NON_RECEIPT_QA_KEYWORDS = (
    "security",
    "code-qa",
    "code qa",
    "lint",
    "audit",
    "sast",
    "sonar",
    "test-suite",
    "unit-test",
    "backend build",
    "docker build",
    "build-and-boot",
)


def is_qa_check_name(name: str) -> bool:
    """Return whether a check run name identifies a browser QA or flow QA receipt check.

    Tightened to explicit receipt/browser checks (e.g. superboard/exact-sha-qa, FLOW-QA,
    QA-RECEIPT, staging QA capture). Security, code quality, test, and build QA checks
    remain strictly non-receipt checks so their failures remain blocking.
    """
    n = name.lower().strip()
    if any(k in n for k in NON_RECEIPT_QA_KEYWORDS):
        return False
    if n in QA_RECEIPT_CHECK_NAMES:
        return True
    for pat in QA_RECEIPT_CHECK_PATTERNS:
        if fnmatch.fnmatch(n, pat):
            return True
    return False

def package_json_dependencies_changed(
    f: Any,
    base_commit: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> bool:
    """Check whether a package.json change modified dependency fields."""
    if isinstance(f, dict):
        if "dependency_fields_changed" in f:
            return bool(f["dependency_fields_changed"])
        if "patch" in f and isinstance(f["patch"], str):
            patch_text = f["patch"]
            dep_pattern = re.compile(r'["\']?(' + "|".join(DEPENDENCY_FIELDS) + r')["\']?\s*:')
            for line in patch_text.splitlines():
                if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                    if dep_pattern.search(line):
                        return True
            return False
    path = f.get("path") if isinstance(f, dict) else str(f)
    if base_commit and head_sha and path:
        try:
            b_out = subprocess.run(
                ["git", "show", f"{base_commit}:{path}"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            h_out = subprocess.run(
                ["git", "show", f"{head_sha}:{path}"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            b_json = json.loads(b_out)
            h_json = json.loads(h_out)
            for k in DEPENDENCY_FIELDS:
                if b_json.get(k) != h_json.get(k):
                    return True
            return False
        except Exception:
            pass
    return True


def is_build_and_boot_critical_diff(
    pr_data: Dict[str, Any],
    base_commit: Optional[str] = None,
    head_sha: Optional[str] = None,
) -> Tuple[bool, str]:
    """Check if diff touches Dockerfile*, requirements*.txt, lockfiles, or package.json deps."""
    files = pr_data.get("files") or []
    for f in files:
        path = f.get("path") if isinstance(f, dict) else str(f)
        norm = path.replace("\\", "/").lower()
        filename = norm.rsplit("/", 1)[-1]
        if fnmatch.fnmatch(filename, "dockerfile*"):
            return True, f"Dockerfile changed ({path})"
        if fnmatch.fnmatch(filename, "requirements*.txt"):
            return True, f"requirements file changed ({path})"
        if (
            fnmatch.fnmatch(filename, "*lock*.json")
            or filename in ("yarn.lock", "pnpm-lock.yaml")
            or filename.endswith(".lock")
            or filename.endswith(".lockb")
            or filename in LOCKFILE_NAMES
        ):
            return True, f"lockfile changed ({path})"
        if filename == "package.json":
            if package_json_dependencies_changed(f, base_commit=base_commit, head_sha=head_sha):
                return True, f"package.json dependencies changed ({path})"
    return False, ""


def evaluate_review_requirement(pr_data: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Evaluate whether a PR requires independent review per issue #195.

    A PR REQUIRES one independent review iff ANY of:
      (a) high-risk domain: label `risk:high`, or any label/area among money, billing,
          auth, concurrency, migration; OR changed paths match money/billing/wallet/ledger/
          payment/stripe, auth/token/rls/permission, alembic/ or migrations/
      (b) total changed lines (additions+deletions, excluding lockfiles and generated files) > 250.
    Otherwise review is EXEMPT: gate passes on green CI plus browser QA evidence.

    Returns:
      (review_required: bool, reason: str)
    """
    labels = pr_data.get("labels") or []
    for label in labels:
        name = label.get("name") if isinstance(label, dict) else str(label)
        name_lower = name.lower().strip()
        if name_lower == "risk:high":
            return True, f"high-risk label {name}"
        if name_lower in HIGH_RISK_DOMAINS:
            return True, f"high-risk domain label {name}"
        if name_lower.startswith("area:") and name_lower.split(":", 1)[1] in HIGH_RISK_DOMAINS:
            return True, f"high-risk area label {name}"

    files = pr_data.get("files")
    if files is not None:
        for f in files:
            path = f.get("path") if isinstance(f, dict) else str(f)
            norm_path = path.replace("\\", "/")
            if MONEY_PATH_RE.search(norm_path):
                return True, f"money path {path}"
            if AUTH_PATH_RE.search(norm_path):
                return True, f"auth path {path}"
            if MIGRATION_PATH_RE.search(norm_path):
                return True, f"migration path {path}"

    # `gh pr view --json files` returns at most 100 files; a capped list may hide
    # high-risk paths and undercount lines, so it cannot justify an exemption.
    if files is not None and len(files) >= 100:
        return True, f"file list truncated at {len(files)} files, review required by default"

    total_lines: Optional[int] = None
    if files is not None:
        total_lines = 0
        for f in files:
            path = f.get("path", "") if isinstance(f, dict) else str(f)
            if not is_lockfile_or_generated(path):
                if isinstance(f, dict):
                    total_lines += int(f.get("additions") or 0) + int(f.get("deletions") or 0)
    elif pr_data.get("additions") is not None or pr_data.get("deletions") is not None:
        total_lines = int(pr_data.get("additions") or 0) + int(pr_data.get("deletions") or 0)

    if total_lines is not None:
        if total_lines > 250:
            return True, f"{total_lines} lines changed > 250"
        return False, f"{total_lines} lines, no high-risk paths"

    return True, "diff/files data missing, review required by default"


# --------------------------------------------------------------- browser QA
# A unit test cannot see a pill that jumps, a strip that blanks, or an order
# that fills against the wrong side. PolySimulator staging therefore demands a
# browser-QA receipt on the PR itself before any merge whose diff reaches a
# surface a user touches: every `frontend/` file (the UI) and the backend
# order/trading paths the UI drives. Verdicts: EXEMPT (not a staging UI/trading
# PR), PASSED, REQUIRED (no receipt, or one that does not bind this diff).
# Lanes post the marker as a plain line, a bold line (`**QA-RECEIPT: PASS**`) and inside
# list items, so leading markdown decoration is tolerated; the served revision on the marker
# line is what binds, and a decorated line carrying none cannot bind on its own.
QA_RECEIPT_MARKER_RE = re.compile(
    r"^[ \t>*_`#|\-]*QA-RECEIPT:\s*(?P<state>PASS|FAIL|FAILED|RETRACTED|RETRACT)\b",
    re.IGNORECASE | re.MULTILINE,
)
# A receipt states a verdict, and a later verdict overrides an earlier one (AGENTS.md §4).
QA_RECEIPT_FAILED_STATES = frozenset({"FAIL", "FAILED", "RETRACT", "RETRACTED"})
QA_RECEIPT_MIN_IMAGES = 2
SHA_TOKEN_RE = re.compile(r"\b[0-9a-fA-F]{40}\b")
# The served revision has to follow the verdict, separated only by markdown decoration: a SHA
# buried in the prose of the same line is a quotation, not a claim about what QA ran against,
# and cannot bind (Bavariance/polysimulator#5589). The head's stripped-diff sha256 is accepted
# too, and no 40-hex word sits inside a 64-hex string, so the pattern spans both.
QA_RECEIPT_SERVED_RE = re.compile(
    r"QA-RECEIPT:\s*(?:PASS|FAIL|FAILED|RETRACTED|RETRACT)\b[ \t>*_`|:\-–—]*"
    r"(?P<served>[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\b",
    re.IGNORECASE,
)
# Evidence images that actually render on a PR: uploaded attachments and commit-pinned
# raw URLs (pinned to a full commit SHA). Release-asset URLs are excluded on purpose —
# GitHub's image proxy 404s them in a private repo
# (Bavariance/polysimulator PR #5630, 2026-09-27), and raw.githubusercontent.com,
# unpinned raw paths and relative paths never resolve either.
EVIDENCE_IMAGE_RE = re.compile(
    r"https://github\.com/user-attachments/assets/"
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/raw/[0-9a-fA-F]{40}/[^\s)\"'>]+"
)
UI_PATH_RE = re.compile(r"^frontend/", re.IGNORECASE)
ORDER_TRADING_PATH_RE = re.compile(
    r"^backend/.*[/_.-](orders?|trades?|trading|matching|settlement|positions?|fills?)([/_.-]|$)",
    re.IGNORECASE,
)
TEST_PATH_RE = re.compile(
    r"(^|/)(tests?|__tests__|__mocks__)(/|$)"
    r"|(^|/)test_[^/]*$"
    r"|_test\.(py|ts|tsx|js|jsx)$"
    r"|\.(test|spec)\.(ts|tsx|js|jsx)$",
    re.IGNORECASE,
)


def is_test_path(path: str) -> bool:
    """
    Test files ship nothing, so they cannot regress a compiled or served surface.

    They still count as a trigger whenever the change also touches product code —
    this only keeps a test-only diff from demanding browser QA it cannot use.
    """
    return bool(TEST_PATH_RE.search(path))


def evaluate_qa_receipt_requirement(
    pr_data: Dict[str, Any], repo: str, base_ref: str
) -> Tuple[bool, str]:
    """
    Whether this PR must carry a browser-QA receipt: PolySimulator `staging` only,
    and only when the changed paths reach the UI or an order/trading path.

    Like the review-exemption rule, an empty or absent `files` list cannot name a
    UI path, so it cannot trigger the requirement; a list capped at 100 may hide
    one and therefore does trigger it.

    Test files are skipped: a test-only diff changes no shipped surface, while a
    change that also touches product code still triggers on that file.
    """
    target = get_flow_qa_target(repo, base_ref)
    is_poly = repo == "Bavariance/polysimulator" and base_ref == "staging"
    is_ship = bool(target and target.require_capture)
    if not (is_poly or is_ship):
        return False, f"no QA receipt requirement for {repo}@{base_ref or 'unknown'}"
    files = pr_data.get("files")
    if files is None:
        return False, "no file list supplied, QA receipt not required"
    if len(files) >= 100:
        return True, f"file list truncated at {len(files)} files, QA receipt required by default"
    for f in files:
        path = f.get("path", "") if isinstance(f, dict) else str(f)
        norm_path = path.replace("\\", "/")
        if is_test_path(norm_path):
            continue
        if is_poly:
            if UI_PATH_RE.match(norm_path):
                return True, f"UI path {path}"
            if ORDER_TRADING_PATH_RE.match(norm_path):
                return True, f"order/trading path {path}"
        elif is_ship:
            if target.ui_pattern.search(norm_path):
                return True, f"UI path {path}"
    return False, "no UI or order/trading paths"


def _receipt_timestamp(source: Dict[str, Any]) -> str:
    """
    When a comment or review was posted, for newest-first receipt evaluation.

    GitHub's REST payloads use `created_at` for comments and `submitted_at` for
    reviews; the CLI's camelCase shape is accepted too. A source with no readable
    timestamp sorts oldest rather than being dropped.
    """
    for key in ("submitted_at", "submittedAt", "created_at", "createdAt"):
        value = source.get(key)
        if value:
            return str(value)
    return ""


def _receipt_line_tokens(body: str, start: int) -> List[str]:
    """The served revision named on the marker line, if the verdict is followed by one."""
    end = body.find("\n", start)
    line = body[start : end if end != -1 else len(body)]
    match = QA_RECEIPT_SERVED_RE.search(line)
    return [match.group("served").lower()] if match else []


# A receipt that presents before/after screenshots must carry the machine captions that
# `control_polysim.py snapshot --label` writes (`SHOT before served=... expected=...`) and
# the `SHOT-PAIR` line that `control_polysim.py pair` verified. Alt text like `![before 1440]`
# is what marks a before/after claim; `initial`/`exercised` receipt shots make no such claim.
SHOT_CLAIM_RE = re.compile(r"!\[\s*(?:before|after)\b", re.IGNORECASE)
SHOT_CAPTION_LINE_RE = re.compile(
    r"^[ \t>*_`|\-]*SHOT (?P<label>before|after)(?P<fields>(?: [a-z0-9]+=\S+)+)[ \t*_`|]*$",
    re.IGNORECASE | re.MULTILINE,
)
SHOT_PAIR_LINE_RE = re.compile(
    r"^[ \t>*_`|\-]*SHOT-PAIR viewport=(?P<viewport>\S+) phash_dist=(?P<dist>\d+) changed_ratio=(?P<ratio>[0-9.]+)[ \t*_`|]*$",
    re.MULTILINE,
)
SHOT_NEAR_IDENTICAL_BITS = 3
SHOT_MIN_CHANGED_RATIO = 0.0005


def capture_evidence_is_current(body: str, binds: Any) -> bool:
    """Ignore proven older revisions, but never exempt missing or malformed captions."""
    after = [
        dict(token.partition("=")[::2] for token in match.group("fields").split())
        for match in SHOT_CAPTION_LINE_RE.finditer(body)
        if match.group("label").lower() == "after"
    ]
    if not after:
        return True
    for fields in after:
        served = fields.get("served", "").lower()
        expected = fields.get("expected", "").lower()
        if not SHA40_RE.fullmatch(served) or served != expected or binds(served):
            return True
    return False


def capture_coverage_problems(sources: List[Dict[str, Any]], binds: Any, required: Any) -> List[str]:
    labels: Dict[str, set] = {}
    hashes: Dict[str, str] = {}
    problems = []
    for source in sources:
        body = str(source.get("body") or "")
        if not SHOT_CLAIM_RE.search(body) or not capture_evidence_is_current(body, binds):
            continue
        for match in SHOT_CAPTION_LINE_RE.finditer(body):
            fields = dict(token.partition("=")[::2] for token in match.group("fields").split())
            viewport = fields.get("viewport", "")
            labels.setdefault(viewport, set()).add(match.group("label").lower())
            digest = fields.get("sha256", "")
            if digest in hashes and hashes[digest] != viewport:
                problems.append("capture image hash reused across viewports")
            hashes[digest] = viewport
    if labels:
        for viewport in required:
            if labels.get(viewport) != {"before", "after"}:
                problems.append(f"missing capture pair for viewport {viewport}")
    return problems


def shot_provenance_problems(body: str, binds: Any, require_capture: bool = False) -> List[str]:
    """
    Why a receipt's before/after screenshots are not provenance-backed evidence.

    Empty when the body claims no before/after pair, or when the pair's machine captions
    prove: the `after` shot was served by the PR head (`binds` says whether a sha names this
    diff), the `before` shot was served by a different commit, every caption's served sha
    equals the sha it was expected to serve, and the pair is neither identical nor near-identical.
    Mislabelled pairs (a staging build captioned as the PR's "after") are the failure this stops
    (Bavariance/polysimulator PR #5630 and the audit in its screenshot-provenance issue).
    """
    if not SHOT_CLAIM_RE.search(body):
        return []
    caption_matches = list(SHOT_CAPTION_LINE_RE.finditer(body))
    viewports = {dict(token.partition("=")[::2] for token in match.group("fields").split()).get("viewport")
                 for match in caption_matches}
    if len(viewports) > 1:
        problems = []
        if require_capture:
            problems.extend(capture_coverage_problems([{"body": body}], binds, []))
        for viewport in viewports:
            lines = []
            for line in body.splitlines():
                caption = SHOT_CAPTION_LINE_RE.fullmatch(line)
                pair_line = SHOT_PAIR_LINE_RE.fullmatch(line)
                if caption and dict(token.partition("=")[::2] for token in caption.group("fields").split()).get("viewport") != viewport:
                    continue
                if pair_line and pair_line.group("viewport") != viewport:
                    continue
                lines.append(line)
            problems.extend(shot_provenance_problems("\n".join(lines), binds, require_capture))
        return problems
    if len(caption_matches) != 2:
        return ["each pair needs exactly one before and one after caption"]
    captions: Dict[str, Dict[str, str]] = {}
    for match in SHOT_CAPTION_LINE_RE.finditer(body):
        label = match.group("label").lower()
        fields = dict(token.partition("=")[::2] for token in match.group("fields").split())
        captions.setdefault(label, fields)
    problems: List[str] = []
    for label in ("before", "after"):
        if label not in captions:
            problems.append(f"the receipt shows a '{label}' screenshot but carries no machine 'SHOT {label}' caption")
    if problems:
        return problems
    for label, fields in captions.items():
        served = str(fields.get("served", "")).lower()
        expected = str(fields.get("expected", "")).lower()
        if not (SHA40_RE.fullmatch(served) and SHA40_RE.fullmatch(expected)):
            problems.append(f"'{label}' caption has no valid 40-hex served/expected sha")
        elif served != expected:
            problems.append(f"'{label}' shot served {served[:8]} but was labelled {expected[:8]}")
    if problems:
        return problems
    before_sha = captions["before"]["served"].lower()
    after_sha = captions["after"]["served"].lower()
    if not binds(after_sha):
        problems.append(f"the 'after' shot was served by {after_sha[:8]}, which is not this PR's head")
    if binds(before_sha) or before_sha == after_sha:
        problems.append(f"the 'before' shot was served by {before_sha[:8]}, the PR head itself, not origin/staging")
    pair = SHOT_PAIR_LINE_RE.search(body)
    if pair is None:
        problems.append("no 'SHOT-PAIR' line: run `control_polysim.py pair` on the before/after captures")
    elif int(pair.group("dist")) <= SHOT_NEAR_IDENTICAL_BITS and float(pair.group("ratio")) < SHOT_MIN_CHANGED_RATIO:
        problems.append(
            f"before and after are near-identical (phash distance {pair.group('dist')}, "
            f"{float(pair.group('ratio')):.4%} of pixels changed)"
        )
    if not require_capture:
        return problems
    records = {}
    for line in re.findall(r"(?m)^CAPTURE (.+)$", body):
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("capture record must be an object")
            key = (record.get("label"), record.get("viewport"))
            if key in records:
                problems.append("duplicate capture record")
            records[key] = record
        except (ValueError, TypeError):
            problems.append("malformed capture record")
    selected = []
    for label, fields in captions.items():
        viewport = fields.get("viewport")
        if pair and pair.group("viewport") != viewport:
            problems.append(f"{label}: pair viewport differs from capture")
        record = records.get((label, viewport))
        if not record:
            problems.append(f"{label}: missing measured capture record")
            continue
        selected.append(record)
        if str(record.get("served_sha", "")).lower() != fields.get("served", "").lower() or record.get("sha256") != fields.get("sha256"):
            problems.append(f"{label}: caption does not match capture record")
        if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", ""))):
            problems.append(f"{label}: invalid capture image hash")
        if not record.get("account") or record.get("source") != "application":
            problems.append(f"{label}: signed-in application source not proven")
        if not re.match(r"^https?://", str(record.get("url", ""))):
            problems.append(f"{label}: static or missing capture URL")
        scale = record.get("device_scale")
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not 0 < scale <= 8:
            problems.append(f"{label}: device scale not measured")
        expected_scale = {"390x844": 2, "390x420": 2, "1440x900": 1}.get(viewport)
        if expected_scale is None:
            problems.append(f"{label}: unsupported capture viewport")
        elif scale != expected_scale:
            problems.append(f"{label}: device scale differs from viewport")
    if len(selected) == 2:
        if selected[0].get("sha256") == selected[1].get("sha256"):
            problems.append("before and after capture image hashes are identical")
        for field in ("account", "viewport", "device_scale"):
            if selected[0].get(field) != selected[1].get(field):
                problems.append(f"capture pair has mismatched {field}")
    return problems


def _content_binder(
    head_sha: str, base_ref: str, cwd: Optional[str]
) -> Tuple[Any, List[str], Optional[str]]:
    """
    Build `binds(token)` for receipt evaluation: whether a 40-hex token names this diff.

    The head SHA and the head's own identity forms bind directly. Any other token has to be a
    commit whose content identity matches the head's, which keeps a receipt served from a
    pre-sync head valid. A token that is not a commit here never binds. The content forms need
    a real checkout of the head; a checkout that cannot supply them narrows what a receipt may
    name, it never blocks one that names the head SHA outright.
    Returns (binds, identity_forms, identity_error).
    """
    from review_content import content_identity

    base = "origin/" + base_ref
    identity_forms = [head_sha]
    head_identity = set()
    identity_error = None
    try:
        head_identity = {form.lower() for form in content_identity(head_sha, base, cwd) if form}
        identity_forms.extend(sorted(head_identity))
    except subprocess.TimeoutExpired as exc:
        return lambda token: False, [], f"blocked: git timed out after {exc.timeout} seconds"
    except (ValueError, subprocess.CalledProcessError) as exc:
        identity_error = str(exc)
    accepted = {form.lower() for form in identity_forms if form}
    resolved: Dict[str, bool] = {}

    def binds(token: str) -> bool:
        if token in accepted:
            return True
        if token not in resolved:
            match = False
            if head_identity:
                try:
                    forms = {form.lower() for form in content_identity(token, base, cwd) if form}
                    match = bool(forms & head_identity)
                except (ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    match = False
            resolved[token] = match
        return resolved[token]

    return binds, identity_forms, identity_error


def evaluate_qa_receipt(
    pr_data: Dict[str, Any],
    *,
    repo: str,
    base_ref: str,
    head_sha: str,
    cwd: Optional[str] = None,
) -> Tuple[str, str, Optional[str]]:
    """
    Verify the browser-QA receipt that a staging UI/order change must carry.

    One PR comment (or review body) has to hold all three of: a `QA-RECEIPT: PASS`
    marker line, the served revision on that line, and at least two rendered
    evidence images. The served token may be the head SHA, the head's patch-id or
    its stripped-diff sha256 (the two content forms survive a sync-only push), or
    any other commit whose content identity equals the head's — a receipt written
    against a pre-sync head still describes the same diff.

    Only the marker line names the revision. A receipt that quotes some other SHA
    in its prose, the head SHA included, cannot bind through it: a receipt produced
    against a revision that was not serving the head is missing evidence, not a
    pass, which is the failure mode behind Bavariance/polysimulator#5589.

    Receipts are evaluated newest first and the newest one that binds this diff
    decides: a later `FAIL` or `RETRACTED` overrides an earlier `PASS`, and a
    receipt with too few images stays missing evidence rather than falling back to
    an older, fuller one.

    Returns (verdict, reason, comment_url). FAILED is never returned: the gate
    either cannot find a receipt (REQUIRED) or has one it can bind (PASSED).
    """
    required, requirement_reason = evaluate_qa_receipt_requirement(pr_data, repo, base_ref)
    if not required:
        return "EXEMPT", requirement_reason, None

    binds, identity_forms, identity_error = _content_binder(head_sha, base_ref, cwd)
    target = get_flow_qa_target(repo, base_ref)
    require_capture = bool(target and target.require_capture)

    all_sources = list(pr_data.get("comments") or []) + list(pr_data.get("reviews") or [])
    evidence_comments = [
        source for source in all_sources
        if SHOT_CLAIM_RE.search(str(source.get("body") or ""))
        and (not require_capture or capture_evidence_is_current(str(source.get("body") or ""), binds))
    ]

    for source in evidence_comments:
        shot_problems = shot_provenance_problems(
            str(source.get("body") or ""),
            (lambda sha: sha.lower() == head_sha.lower()) if require_capture else binds,
            require_capture,
        )
        if shot_problems:
            url = str(source.get("html_url") or source.get("url") or "") or None
            return (
                "REQUIRED",
                f"QA receipt required ({requirement_reason}): before/after screenshot provenance "
                f"failed: {'; '.join(shot_problems)}.",
                url,
            )
    if require_capture:
        coverage_problems = capture_coverage_problems(evidence_comments, binds, target.required_viewports)
        if coverage_problems:
            return "REQUIRED", f"capture provenance failed: {'; '.join(coverage_problems)}", None

    declarations = []
    for source in all_sources:
        body = str(source.get("body") or "")
        for marker in QA_RECEIPT_MARKER_RE.finditer(body):
            declarations.append(
                {
                    "posted": _receipt_timestamp(source),
                    "state": marker.group("state").upper(),
                    "tokens": _receipt_line_tokens(body, marker.start()),
                    "images": len(EVIDENCE_IMAGE_RE.findall(body)),
                    "url": str(source.get("html_url") or source.get("url") or ""),
                    "body": body,
                }
            )
    declarations.sort(key=lambda item: item["posted"], reverse=True)

    saw_pass_marker = False
    saw_pass_served = False
    for declaration in declarations:
        tokens = set(declaration["tokens"])
        if declaration["state"] in QA_RECEIPT_FAILED_STATES:
            tokens |= {token.lower() for token in SHA_TOKEN_RE.findall(declaration["body"])}
        else:
            saw_pass_marker = True
            saw_pass_served = saw_pass_served or bool(declaration["tokens"])
        if not any(binds(token) for token in tokens):
            continue
        if declaration["state"] in QA_RECEIPT_FAILED_STATES:
            return (
                "REQUIRED",
                f"QA receipt required ({requirement_reason}): the newest receipt binding this "
                f"diff is {declaration['state']}.",
                declaration["url"] or None,
            )
        if declaration["images"] < QA_RECEIPT_MIN_IMAGES:
            return (
                "REQUIRED",
                f"QA receipt required ({requirement_reason}): the receipt carries "
                f"{declaration['images']} GitHub-hosted evidence image(s), "
                f"{QA_RECEIPT_MIN_IMAGES} required.",
                declaration["url"] or None,
            )
        return (
            "PASSED",
            f"browser QA receipt binds head {head_sha[:8]} ({requirement_reason})",
            declaration["url"] or None,
        )

    if not declarations:
        if not require_capture:
            return (
                "REQUIRED",
                f"QA receipt required ({requirement_reason}): no PR comment carries a "
                "'QA-RECEIPT: PASS' marker.",
                None,
            )
        if evidence_comments:
            first_url = str(evidence_comments[0].get("html_url") or evidence_comments[0].get("url") or "") or None
            return (
                "PASSED",
                f"browser QA receipt binds head {head_sha[:8]} ({requirement_reason})",
                first_url,
            )
        return (
            "EXEMPT",
            f"no capture evidence or QA receipt declarations for {repo}@{base_ref}",
            None,
        )
    if saw_pass_marker and not saw_pass_served:
        return (
            "REQUIRED",
            f"QA receipt required ({requirement_reason}): the marker names no served revision; "
            "a 'QA-RECEIPT: PASS <served-sha>' line is required.",
            None,
        )
    forms = ", ".join(identity_forms) or "none resolved"
    return (
        "REQUIRED",
        f"QA receipt required ({requirement_reason}): the receipt names no identity for "
        f"this head (accepted identity tokens: {forms}"
        f"{'; identity lookup failed: ' + identity_error if identity_error else ''}).",
        None,
    )


# ------------------------------------------------------------------ flow QA
# `QA-RECEIPT` proves a human-style look at screenshots. `FLOW-QA` proves a user flow ran:
# the runner (`flow_qa_runner.mjs`) tapped, typed, swiped and switched tabs against the build
# serving the head, at phone and desktop widths, and counted assertions. Staging UI PRs need
# both. The marker line names the served revision, exactly like QA-RECEIPT, so a receipt from
# a stale head cannot bind (negative control: test_github_pr_gate.py).
FLOW_QA_MARKER_RE = re.compile(
    r"^[ \t>*_`#|\-]*FLOW-QA:\s*(?P<state>PASS|FAIL|FAILED|RETRACTED|RETRACT)\b",
    re.IGNORECASE | re.MULTILINE,
)
FLOW_QA_SERVED_RE = re.compile(
    r"FLOW-QA:\s*(?:PASS|FAIL|FAILED|RETRACTED|RETRACT)\b[ \t>*_`|:\-–—]*"
    r"(?P<served>[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\b",
    re.IGNORECASE,
)
FLOW_QA_ASSERTIONS_RE = re.compile(
    r"^[ \t>*_`|\-]*FLOW-QA-ASSERTIONS pass=(?P<passed>\d+) fail=(?P<failed>\d+)[ \t*_`|]*$",
    re.IGNORECASE | re.MULTILINE,
)
FLOW_QA_VIEWPORTS_RE = re.compile(
    r"^[ \t>*_`|\-]*FLOW-QA-VIEWPORTS (?P<viewports>[0-9x,\s]+)[ \t*_`|]*$",
    re.IGNORECASE | re.MULTILINE,
)
FLOW_QA_REQUIRED_VIEWPORTS: Tuple[str, ...] = ("390x844", "1440x900")


@dataclass(frozen=True)
class FlowQATarget:
    repo: str
    base_ref: str
    ui_pattern: Any
    required_viewports: Tuple[str, ...] = FLOW_QA_REQUIRED_VIEWPORTS
    sticky_failures: bool = False
    require_capture: bool = False


FLOW_QA_TARGETS: Tuple[FlowQATarget, ...] = (
    FlowQATarget(
        repo="Bavariance/polysimulator",
        base_ref="staging",
        ui_pattern=re.compile(r"^frontend/", re.IGNORECASE),
        required_viewports=("390x844", "1440x900"),
    ),
    FlowQATarget(
        repo="Wladefant/shipnovo",
        base_ref="main",
        ui_pattern=re.compile(r"^src/(?:app|components|features)/.*\.tsx$", re.IGNORECASE),
        required_viewports=("390x420", "390x844", "1440x900"),
        sticky_failures=True,
        require_capture=True,
    ),
)


def get_flow_qa_target(repo: str, base_ref: str) -> Optional[FlowQATarget]:
    """Resolve configured FLOW-QA target by repository and base branch."""
    for target in FLOW_QA_TARGETS:
        if target.repo == repo and target.base_ref == base_ref:
            return target
    return None


def evaluate_flow_qa_requirement(
    pr_data: Dict[str, Any], repo: str, base_ref: str
) -> Tuple[bool, str]:
    """Whether this PR must carry a FLOW-QA receipt based on repository/base targets."""
    target = get_flow_qa_target(repo, base_ref)
    if target is None:
        return False, f"no Flow QA requirement for {repo}@{base_ref or 'unknown'}"
    files = pr_data.get("files")
    if files is None:
        return True, "no file list supplied, Flow QA required by default"
    if len(files) >= 100:
        return True, f"file list truncated at {len(files)} files, Flow QA required by default"
    for f in files:
        path = f.get("path", "") if isinstance(f, dict) else str(f)
        norm_path = path.replace("\\", "/")
        if not is_test_path(norm_path) and target.ui_pattern.search(norm_path):
            return True, f"UI path {path}"
    return False, "no UI paths"


def evaluate_flow_qa_receipt(
    pr_data: Dict[str, Any],
    *,
    repo: str,
    base_ref: str,
    head_sha: str,
    cwd: Optional[str] = None,
) -> Tuple[str, str, Optional[str]]:
    """
    Verify the FLOW-QA receipt a staging UI change must carry.

    One comment (or review body) must hold a `FLOW-QA: PASS <served-sha>` line whose sha names
    this diff, a `FLOW-QA-ASSERTIONS pass=N fail=0` line with N > 0, and a
    `FLOW-QA-VIEWPORTS` line covering 390x844 and 1440x900. The newest receipt that binds
    this diff decides; a later FAIL or RETRACTED overrides an earlier PASS. Returns
    (EXEMPT | PASSED | REQUIRED, reason, comment_url).
    """
    required, requirement_reason = evaluate_flow_qa_requirement(pr_data, repo, base_ref)
    if not required:
        return "EXEMPT", requirement_reason, None
    prefix = f"Flow QA receipt required ({requirement_reason})"
    binds, identity_forms, identity_error = _content_binder(head_sha, base_ref, cwd)

    declarations = []
    for source in list(pr_data.get("comments") or []) + list(pr_data.get("reviews") or []):
        body = str(source.get("body") or "")
        for marker in FLOW_QA_MARKER_RE.finditer(body):
            end = body.find("\n", marker.start())
            line = body[marker.start() : end if end != -1 else len(body)]
            served = FLOW_QA_SERVED_RE.search(line)
            declarations.append(
                {
                    "posted": _receipt_timestamp(source),
                    "state": marker.group("state").upper(),
                    "tokens": [served.group("served").lower()] if served else [],
                    "url": str(source.get("html_url") or source.get("url") or ""),
                    "body": body,
                }
            )
    declarations.sort(key=lambda item: item["posted"], reverse=True)
    target = get_flow_qa_target(repo, base_ref)
    saw_pass_marker = False
    saw_pass_served = False
    for declaration in declarations:
        tokens = set(declaration["tokens"])
        if declaration["state"] in QA_RECEIPT_FAILED_STATES:
            tokens |= {token.lower() for token in SHA_TOKEN_RE.findall(declaration["body"])}
        else:
            saw_pass_marker = True
            saw_pass_served = saw_pass_served or bool(declaration["tokens"])
        if head_sha.lower() not in tokens or not binds(head_sha.lower()):
            continue
        if declaration["state"] in QA_RECEIPT_FAILED_STATES:
            return (
                "REQUIRED",
                f"{prefix}: the newest receipt binding this diff is {declaration['state']}.",
                declaration["url"] or None,
            )
        if target and target.sticky_failures:
            for other in declarations:
                if other["state"] in ("FAIL", "FAILED"):
                    other_tokens = set(other["tokens"]) | {
                        token.lower() for token in SHA_TOKEN_RE.findall(other["body"])
                    }
                    if any(binds(token) for token in other_tokens):
                        return "REQUIRED", f"{prefix}: a failed target still binds this content.", other["url"] or None
        project = "shipnovo" if repo.lower() == "wladefant/shipnovo" else "polysimulator"
        source_lines = re.findall(r"(?m)^FLOW-QA-SOURCE runner=([0-9a-f]{64}) flow=([0-9a-f]{64}) project=([a-z0-9-]+)$", declaration["body"])
        try:
            root = os.path.dirname(os.path.abspath(__file__))
            with open(os.path.join(root, "flow_qa_runner.mjs"), "rb") as runner_file:
                runner_hash = hashlib.sha256(runner_file.read()).hexdigest()
            with open(os.path.join(root, "flows", project + ".json"), "rb") as flow_file:
                flow_hash = hashlib.sha256(flow_file.read()).hexdigest()
        except OSError as exc:
            return "REQUIRED", f"{prefix}: approved QA source unavailable: {exc}", declaration["url"] or None
        if source_lines != [(runner_hash, flow_hash, project)]:
            return "REQUIRED", f"{prefix}: missing or changed canonical runner/flow hashes.", declaration["url"] or None
        if target and target.require_capture:
            problems = shot_provenance_problems(declaration["body"], lambda sha: sha.lower() == head_sha.lower(), True)
            if problems:
                return "REQUIRED", f"{prefix}: capture provenance failed: {'; '.join(problems)}", declaration["url"] or None
            for source in list(pr_data.get("comments") or []) + list(pr_data.get("reviews") or []):
                s_body = str(source.get("body") or "")
                if s_body == declaration["body"]:
                    continue
                if SHOT_CLAIM_RE.search(s_body) and capture_evidence_is_current(s_body, binds):
                    s_problems = shot_provenance_problems(s_body, lambda sha: sha.lower() == head_sha.lower(), True)
                    if s_problems:
                        s_url = str(source.get("html_url") or source.get("url") or "") or declaration["url"] or None
                        return "REQUIRED", f"{prefix}: capture provenance failed: {'; '.join(s_problems)}", s_url
            coverage_problems = capture_coverage_problems(
                list(pr_data.get("comments") or []) + list(pr_data.get("reviews") or []),
                binds, target.required_viewports)
            if coverage_problems:
                return "REQUIRED", f"{prefix}: capture provenance failed: {'; '.join(coverage_problems)}", declaration["url"] or None
        counts = FLOW_QA_ASSERTIONS_RE.search(declaration["body"])
        if counts is None:
            return "REQUIRED", f"{prefix}: no 'FLOW-QA-ASSERTIONS pass=N fail=M' line.", declaration["url"] or None
        if int(counts.group("passed")) <= 0 or int(counts.group("failed")) != 0:
            return (
                "REQUIRED",
                f"{prefix}: assertions pass={counts.group('passed')} fail={counts.group('failed')}; "
                "a PASS needs pass>0 and fail=0.",
                declaration["url"] or None,
            )
        target = get_flow_qa_target(repo, base_ref)
        required_viewports = target.required_viewports if target else FLOW_QA_REQUIRED_VIEWPORTS
        vp = FLOW_QA_VIEWPORTS_RE.search(declaration["body"])
        covered = {v.strip() for v in vp.group("viewports").lower().split(",") if v.strip()} if vp else set()
        missing = [v for v in required_viewports if v not in covered]
        if missing:
            return (
                "REQUIRED",
                f"{prefix}: the receipt does not cover viewport(s) {', '.join(missing)}.",
                declaration["url"] or None,
            )
        return (
            "PASSED",
            f"Flow QA receipt binds head {head_sha[:8]} ({requirement_reason})",
            declaration["url"] or None,
        )

    if not declarations:
        return "REQUIRED", f"{prefix}: no PR comment carries a 'FLOW-QA: PASS' marker.", None
    if saw_pass_marker and not saw_pass_served:
        return (
            "REQUIRED",
            f"{prefix}: the marker names no served revision; a 'FLOW-QA: PASS <served-sha>' line is required.",
            None,
        )
    forms = ", ".join(identity_forms) or "none resolved"
    return (
        "REQUIRED",
        f"{prefix}: the receipt names no identity for this head (accepted identity tokens: {forms}"
        f"{'; identity lookup failed: ' + identity_error if identity_error else ''}).",
        None,
    )


def validate_review_artifact(
    record: Dict[str, Any],
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    pr_author: str,
    checked_at_utc: str,
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
    checked_time = datetime.datetime.fromisoformat(checked_at_utc.replace("Z", "+00:00"))
    if parsed_time > checked_time + datetime.timedelta(
        seconds=MAX_REVIEW_FUTURE_SKEW_SECONDS
    ):
        return None, (
            f"Review artifact submitted_at {submitted_at} is in the future relative to "
            f"gate time {checked_at_utc} beyond the "
            f"{MAX_REVIEW_FUTURE_SKEW_SECONDS}-second clock-skew allowance"
        )

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
    review_decision: str = "required"
    review_decision_reason: str = ""
    decision_line: str = ""
    verify_receipt_verdict: Optional[str] = None
    verify_receipt_reason: Optional[str] = None
    verify_receipt: Optional[Dict[str, Any]] = None
    qa_receipt_verdict: Optional[str] = None
    qa_receipt_reason: Optional[str] = None
    qa_receipt_url: Optional[str] = None
    flow_qa_receipt_verdict: Optional[str] = None
    flow_qa_receipt_reason: Optional[str] = None
    flow_qa_receipt_url: Optional[str] = None
    released_checks: List[str] = field(default_factory=list)
    local_tests_record: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_compact_markdown(self) -> str:
        failing_str = ", ".join(self.failing_checks) if self.failing_checks else "None"
        pending_str = ", ".join(self.pending_checks) if self.pending_checks else "None"
        released_str = f", Released: {', '.join(self.released_checks)} (local tests recorded)" if self.released_checks else ""
        decision_line = self.decision_line or f"review: {self.review_decision} ({self.review_decision_reason})"
        return (
            f"### Deterministic PR Gate Evaluation: PR #{self.pr_number} ({self.gate_verdict})\n"
            f"- **Head SHA:** `{self.head_sha[:8]}` (Base: `{self.base_sha[:8]}`)\n"
            f"- **State:** `{self.state}` (Draft: `{self.is_draft}`)\n"
            f"- **CI Status:** `{self.ci_verdict}` (Failing: {failing_str}, Pending: {pending_str}{released_str})\n"
            f"- **Review:** `{decision_line}`\n"
            f"- **Approval:** `{self.approval_verdict}` (By: `{self.approved_by or 'None'}`, "
            f"GitHub approval required: `{self.github_approval_required}`)\n"
            f"- **Advisory failures:** {', '.join(self.advisory_failing_checks) or 'None'}\n"
            f"- **Review Reused:** `{self.review_reused}` (Invalidated: `{self.review_invalidated}`"
            f"{f' - {self.invalidation_reason}' if self.invalidation_reason else ''})\n"
            f"- **Verification:** `{self.verify_receipt_verdict or 'EXEMPT'}`"
            f"{f' - {self.verify_receipt_reason}' if self.verify_receipt_reason else ''}\n"
            f"- **Browser QA:** `{self.qa_receipt_verdict or 'EXEMPT'}`"
            f"{f' - {self.qa_receipt_reason}' if self.qa_receipt_reason else ''}\n"
            f"- **Flow QA:** `{self.flow_qa_receipt_verdict or 'EXEMPT'}`"
            f"{f' - {self.flow_qa_receipt_reason}' if self.flow_qa_receipt_reason else ''}\n"
            f"- **Verdict:** **{self.gate_verdict}** — {self.verdict_reason}\n"
        )

def _run_gh(cmd: List[str], timeout_sec: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        shell=True if sys.platform == "win32" else False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
        "number,state,isDraft,headRefOid,baseRefName,reviews,statusCheckRollup,author,labels,files,additions,deletions",
    ]

    res = _run_gh(cmd, timeout_sec)
    if res.returncode != 0:
        raise RuntimeError(f"gh pr view failed (exit {res.returncode}): {res.stderr.strip()}")

    data = json.loads(res.stdout)
    data["baseRefOid"] = fetch_base_sha(pr_number, repo, timeout_sec)
    reviews = _run_gh(["gh", "api", f"repos/{repo}/pulls/{pr_number}/reviews?per_page=100", "--paginate"], timeout_sec)
    if reviews.returncode:
        raise RuntimeError("Unable to fetch complete PR reviews")
    comments = _run_gh(["gh", "api", f"repos/{repo}/issues/{pr_number}/comments?per_page=100", "--paginate"], timeout_sec)
    if comments.returncode:
        raise RuntimeError("Unable to fetch complete PR comments")
    from review_content import json_pages
    data["reviews"] = [review for page in json_pages(reviews.stdout) for review in page]
    data["comments"] = [comment for page in json_pages(comments.stdout) for comment in page]
    subprocess.run(["git", "fetch", "--depth=200", "origin", f"+refs/heads/{data['baseRefName']}:refs/remotes/origin/{data['baseRefName']}"],
                   check=True, stdin=subprocess.DEVNULL, timeout=10,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    from review_content import target_shas
    for sha in target_shas(data["reviews"], data["headRefOid"]):
        if subprocess.run(["git", "cat-file", "-e", sha + "^{commit}"], stderr=subprocess.DEVNULL,
                          stdin=subprocess.DEVNULL, timeout=10,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode:
            # A target that stays unreachable fails closed in evaluate(), and only
            # when it is actually needed; an unfetchable one must not abort the gate.
            subprocess.run(["git", "fetch", "--depth=200", "origin", sha], stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return data

def evaluate_pr_gate(
    pr_data: Dict[str, Any],
    repo: str = "Bavariance/polysimulator",
    review_artifact: Optional[Dict[str, Any]] = None,
    security_alerts: Optional[List[Dict[str, Any]]] = None,
    expected_head_sha: Optional[str] = None,
    policy: Optional[GateApprovalPolicy] = None,
    native_required_contexts: Optional[List[str]] = None,
    verify_receipt: Optional[Dict[str, Any]] = None,
    require_verify_receipt: Optional[bool] = None,
    local_tests_record: Optional[Dict[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> PRGateEvaluation:
    """
    Deterministically evaluates GitHub PR status gate without LLM churn.

    When `expected_head_sha` is supplied, the live head must equal it; a mismatch is a
    hard BLOCK, because every QA and review artifact is bound to one exact head SHA.

    `policy` decides whether a non-author GitHub APPROVED review is mandatory for this
    repo/base. Named automated branches still require a content-bound GitHub review.
    Legacy local review metadata is advisory cache data and cannot grant approval.

    Independently of review policy, under default merge-first workflow
    (SUPERBOARD_MERGE_FIRST=1), staging UI PRs with green CI pass risk-exempt without
    pre-merge QA or FLOW receipts; verification runs post-deploy on staging.
    When SUPERBOARD_MERGE_FIRST=0, exact legacy behavior is restored where any PR
    whose diff reaches `frontend/` or an order/trading path is BLOCKED unless a PR comment
    carries a browser QA receipt for this diff; see `evaluate_qa_receipt`.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base_ref = str(pr_data.get("baseRefName") or (pr_data.get("base") or {}).get("ref") or "")
    if policy is None:
        policy = resolve_gate_policy(repo, base_ref)

    if policy.allow_review_exemption:
        review_required, review_decision_reason = evaluate_review_requirement(pr_data)
    else:
        review_required, review_decision_reason = True, f"no review exemption for {repo}@{base_ref or 'unknown'}"
    review_decision = "required" if review_required else "exempt"
    decision_line = f"review: {review_decision} ({review_decision_reason})"

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

    # 0. Exact-head binding requires a resolvable full SHA even without a caller pin.
    invalid_live_head = not SHA40_RE.fullmatch(head_sha)
    if invalid_live_head or (expected_head_sha and expected_head_sha != head_sha):
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
                "Live PR head is not a full 40-character hexadecimal commit SHA."
                if invalid_live_head
                else f"Expected head {expected_head_sha[:8]} but PR head is {head_sha[:8]}"
            ),
            gate_verdict="BLOCKED",
            verdict_reason=(
                "Live PR head is not a full 40-character hexadecimal commit SHA; review cannot be bound."
                if invalid_live_head
                else (
                    f"Head mismatch: caller pinned {expected_head_sha[:8]}, live PR head is "
                    f"{head_sha[:8]}. All prior QA/review evidence is invalidated by the new push."
                )
            ),
            checked_at_utc=now_utc,
            review_decision=review_decision,
            review_decision_reason=review_decision_reason,
            decision_line=decision_line,
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
            review_decision=review_decision,
            review_decision_reason=review_decision_reason,
            decision_line=decision_line,
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
            review_decision=review_decision,
            review_decision_reason=review_decision_reason,
            decision_line=decision_line,
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

    # Deduplicate status_rollup by check name: preserve latest check run. GitHub
    # reports an unfinished run's completedAt as the 0001-01-01 sentinel, so that
    # value must fall back to the start time; otherwise a pending re-run would sort
    # before the older finished run it supersedes and be silently dropped.
    def _check_sort_key(chk: dict) -> str:
        comp = chk.get("completedAt")
        if comp and not str(comp).startswith("0001"):
            return str(comp)
        return str(chk.get("startedAt") or chk.get("createdAt") or "")

    deduped_status_rollup: Dict[str, dict] = {}
    for check in status_rollup:
        c_name = check.get("name") or check.get("context") or "unknown_check"
        if c_name not in deduped_status_rollup or _check_sort_key(check) > _check_sort_key(deduped_status_rollup[c_name]):
            deduped_status_rollup[c_name] = check

    for check in deduped_status_rollup.values():
        # Check either CheckRun or StatusContext
        c_name = check.get("name") or check.get("context") or "unknown_check"
        c_status = str(check.get("status") or "").upper()
        c_conclusion = str(check.get("conclusion") or check.get("state") or "").upper()
        c_completed_at = check.get("completedAt") or check.get("createdAt")
        if c_conclusion in ("FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED", "STARTUP_FAILURE"):
            if merge_first_enabled(env) and is_qa_check_name(c_name):
                advisory_failing_checks.append(c_name)
            elif is_blocking(c_name):
                failing_checks.append(c_name)
                if c_completed_at and (latest_ci_failure_time is None or c_completed_at > latest_ci_failure_time):
                    latest_ci_failure_time = c_completed_at
            else:
                advisory_failing_checks.append(c_name)
        elif c_status in ("IN_PROGRESS", "QUEUED", "PENDING", "EXPECTED"):
            if merge_first_enabled(env) and is_qa_check_name(c_name):
                pass
            elif is_blocking(c_name):
                pending_checks.append(c_name)

    # Per AGENTS.md §6: Never wait more than 5 minutes on CI — EXCEPT for
    # deploy-critical checks (build-and-boot, backend build, docker build).
    # Those must complete; the gate stays PENDING until they finish or fail.
    def _is_deploy_critical(name: str) -> bool:
        for pattern in DEPLOY_CRITICAL_CHECKS:
            if fnmatch.fnmatch(name, pattern):
                return True
        return False

    ci_timed_out_checks: List[str] = []
    if pending_checks and not failing_checks:
        now_utc_dt = datetime.datetime.now(datetime.timezone.utc)
        oldest_non_critical_sec = 0.0
        for check in deduped_status_rollup.values():
            c_name = check.get("name") or check.get("context") or "unknown_check"
            if c_name in pending_checks and not _is_deploy_critical(c_name):
                start = check.get("startedAt") or check.get("createdAt")
                if start:
                    try:
                        t = datetime.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                        age = (now_utc_dt - t).total_seconds()
                        if age > oldest_non_critical_sec:
                            oldest_non_critical_sec = age
                    except Exception:
                        pass
        if oldest_non_critical_sec >= 300:
            # Time out only the non-deploy-critical checks.
            timed = [c for c in pending_checks if not _is_deploy_critical(c)]
            ci_timed_out_checks = timed
            pending_checks = [c for c in pending_checks if _is_deploy_critical(c)]

    released_critical_checks: List[str] = []
    is_valid_ltr = False
    ltr_reason = ""
    if local_tests_record is not None:
        is_valid_ltr, ltr_reason = validate_local_tests_record(local_tests_record, head_sha)

    if is_valid_ltr and not failing_checks:
        bb_critical, bb_reason = is_build_and_boot_critical_diff(
            pr_data, base_commit=base_sha, head_sha=head_sha
        )
        new_pending_checks: List[str] = []
        for c_name in pending_checks:
            is_crit = _is_deploy_critical(c_name)
            is_bb = fnmatch.fnmatch(c_name, "build-and-boot*")
            if is_crit:
                if is_bb and bb_critical:
                    new_pending_checks.append(c_name)
                else:
                    released_critical_checks.append(c_name)
            else:
                ci_timed_out_checks.append(c_name)
        pending_checks = new_pending_checks

    if failing_checks:
        ci_verdict = "FAILURE"
    elif pending_checks:
        ci_verdict = "PENDING"
    elif not deduped_status_rollup and merge_first_enabled(env):
        if local_tests_record is None:
            ci_verdict = "FAILURE"
            failing_checks.append("local-tests-record (CI absent: local tests record required)")
        else:
            is_valid_ltr, ltr_reason = validate_ci_absent_local_tests(local_tests_record, head_sha)
            if not is_valid_ltr:
                ci_verdict = "FAILURE"
                failing_checks.append(f"local-tests-record ({ltr_reason})")
            else:
                ci_verdict = "SUCCESS"
    else:
        ci_verdict = "SUCCESS"

    # 4. GitHub approval and independent automated review artifact verification.
    reviews_list = pr_data.get("reviews") or []
    valid_github_approvers: List[str] = []
    self_approvers: List[str] = []
    changes_requesters: List[str] = []

    from review_content import evaluate as evaluate_content
    try:
        # GitHub cannot grant an author formal approval. The staging waiver
        # accepts an explicit automated COMMENT verdict, not self-APPROVED data.
        eligible_reviews = [
            review for review in reviews_list
            if not (
                (review.get("author") or review.get("user") or {}).get("login", "").lower()
                == pr_author.lower()
                and str(review.get("state") or "").upper() == "APPROVED"
            )
        ]
        staging_waiver = (
            (repo == "Bavariance/polysimulator" and base_ref == "staging")
            or (repo == "Wladefant/super-board" and base_ref == "main")
            or (repo == "Wladefant/veyyon" and base_ref == "main")
            or (not policy.require_github_approval and policy.require_head_bound_review_evidence)
        )
        content_review = evaluate_content(
            eligible_reviews, head_sha, pr_author, base="origin/" + base_ref,
            staging=staging_waiver,
        )
    except (ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        content_review = {"passed": False, "reason": str(exc)}
    if content_review["passed"] and content_review["state"] == "APPROVED" and content_review["reviewer"].lower() != pr_author.lower():
        valid_github_approvers.append(content_review["reviewer"])
    for review in reviews_list:
        review_author = (review.get("author") or review.get("user") or {}).get("login", "")
        review_state = str(review.get("state") or "").upper()
        if review_author.lower() == pr_author.lower():
            if review_state in ("APPROVED", "COMMENTED") and not content_review["passed"]:
                self_approvers.append(review_author)
            continue
        review_commit = review.get("commit_id") or (review.get("commit") or {}).get("oid") or review.get("commitRefOid")
        if review_state == "CHANGES_REQUESTED" and review_commit == head_sha and not content_review["passed"]:
            changes_requesters.append(review_author)

    artifact_evidence: Optional[Dict[str, str]] = None
    review_invalidated = False
    invalidation_reason = None
    review_reused = bool(content_review["passed"])
    # Content equality preserves the code review, not permission to ignore
    # newly failing checks or alerts. Bind freshness to GitHub review time,
    # never to an optional local artifact that could hide a later finding.
    if content_review["passed"]:
        review_time = content_review.get("reviewed_at") or ""
        if latest_ci_failure_time and (
            not review_time or latest_ci_failure_time > review_time
        ):
            review_invalidated = True
            invalidation_reason = (
                "New CI failure occurred after automated review "
                f"({latest_ci_failure_time} > {review_time or 'unknown review time'})"
            )
        elif security_alerts:
            new_alerts = [
                alert for alert in security_alerts
                if not review_time or not alert.get("created_at")
                or alert["created_at"] > review_time
            ]
            if new_alerts:
                review_invalidated = True
                invalidation_reason = (
                    f"{len(new_alerts)} new security alert(s) detected after automated review"
                )
        if review_invalidated:
            review_reused = False
    if review_artifact is not None and not content_review["passed"]:
        artifact_evidence, artifact_error = validate_review_artifact(
            review_artifact,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            base_sha=base_sha,
            pr_author=pr_author,
            checked_at_utc=now_utc,
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

    if valid_github_approvers:
        approval_verdict = "APPROVED"
        approved_by = valid_github_approvers[-1]
    elif content_review["passed"]:
        approval_verdict = "AUTOMATED_REVIEW_APPROVED"
        approved_by = content_review["reviewer"]
    elif self_approvers:
        approval_verdict = "SELF_APPROVED_ONLY"
        approved_by = None
    else:
        approval_verdict = "UNAPPROVED"
        approved_by = None

    has_head_bound_review_evidence = content_review["passed"]

    # 4B. Verification Receipt (verify-receipt/v1) Evaluation
    effective_require_verify_receipt = (
        require_verify_receipt
        if require_verify_receipt is not None
        else policy.require_verify_receipt
    )

    if effective_require_verify_receipt and verify_receipt is None:
        standard_verify_paths = [
            os.path.join(".veyyon", "verify", f"receipt-{head_sha}.json"),
            os.path.join("workflows", "portable", "receipts", f"verify-{head_sha}.json"),
            os.path.join(f"verify-receipt-{head_sha}.json"),
            os.path.join("verify-receipt.json"),
            os.path.expanduser(f"~/.veyyon/verify/receipt-{head_sha}.json"),
        ]
        for v_path in standard_verify_paths:
            if os.path.exists(v_path):
                try:
                    with open(v_path, "r", encoding="utf-8") as vf:
                        c_data = json.load(vf)
                        if isinstance(c_data, dict) and c_data.get("schema_version") == "verify-receipt/v1":
                            verify_receipt = c_data
                            break
                except Exception:
                    continue

    verify_receipt_verdict: Optional[str] = None
    verify_receipt_reason: Optional[str] = None
    verify_receipt_blocked = False

    if effective_require_verify_receipt and verify_receipt is None:
        verify_receipt_blocked = True
        verify_receipt_verdict = "MISSING"
        verify_receipt_reason = f"Verification receipt missing for commit {head_sha[:8]}: PR is not ready for promotion."
    elif verify_receipt is not None:
        is_valid_vr, vr_err = validate_verify_receipt(
            verify_receipt, head_sha=head_sha, expected_pr=pr_number, expected_repo=repo
        )
        if not is_valid_vr:
            verify_receipt_blocked = True
            verify_receipt_verdict = "FAILED"
            verify_receipt_reason = f"Verification receipt rejected: {vr_err}"
        else:
            verify_receipt_verdict = "PASSED"
            verify_receipt_reason = verify_receipt.get("summary") or "All scenario checks passed."
    else:
        verify_receipt_verdict = "EXEMPT"
        verify_receipt_reason = "No verification receipt required by policy."
    # 4C. Browser QA Receipt (staging UI / order-trading paths)
    qa_receipt_verdict, qa_receipt_reason, qa_receipt_url = evaluate_qa_receipt(
        pr_data, repo=repo, base_ref=base_ref, head_sha=head_sha
    )
    # 4D. Real user-flow QA receipt (FLOW-QA), required for staging UI changes
    flow_qa_receipt_verdict, flow_qa_receipt_reason, flow_qa_receipt_url = evaluate_flow_qa_receipt(
        pr_data, repo=repo, base_ref=base_ref, head_sha=head_sha
    )
    if merge_first_enabled(env):
        qa_receipt_blocked = False
        flow_qa_receipt_blocked = False
    else:
        qa_receipt_blocked = qa_receipt_verdict == "REQUIRED"
        flow_qa_receipt_blocked = flow_qa_receipt_verdict == "REQUIRED"
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
    elif verify_receipt_blocked:
        gate_verdict = "BLOCKED"
        verdict_reason = f"{verify_receipt_reason}"
    elif qa_receipt_blocked:
        gate_verdict = "BLOCKED"
        verdict_reason = f"{qa_receipt_reason}"
    elif flow_qa_receipt_blocked:
        gate_verdict = "BLOCKED"
        verdict_reason = f"{flow_qa_receipt_reason}"
    elif review_required:
        if approval_verdict == "SELF_APPROVED_ONLY":
            gate_verdict = "BLOCKED"
            verdict_reason = (
                f"Self-approval rejected (PR author {pr_author}); independent review required "
                f"({review_decision_reason})."
            )
        elif policy.require_github_approval and not valid_github_approvers:
            gate_verdict = "BLOCKED"
            verdict_reason = (
                f"No GitHub APPROVED review pinned to commit {head_sha[:8]} "
                f"(review required: {review_decision_reason})."
            )
        elif (
            not policy.require_github_approval
            and policy.require_head_bound_review_evidence
            and not has_head_bound_review_evidence
        ):
            gate_verdict = "BLOCKED"
            verdict_reason = (
                f"GitHub approval is not required for {repo}@{base_ref or 'unknown'}, but no "
                f"independent head-bound review evidence exists for commit {head_sha[:8]} "
                f"(review required: {review_decision_reason})."
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
    else:
        # Review is EXEMPT.
        if policy.is_production_protected(repo, base_ref) and not valid_github_approvers:
            gate_verdict = "BLOCKED"
            verdict_reason = (
                f"Production-protected base {repo}@{base_ref} strictly requires independent "
                f"human GitHub approval; review exemption does not apply to production."
            )
        else:
            gate_verdict = "PASSED"
            verdict_reason = (
                f"All required CI checks succeeded; independent review is exempt ({review_decision_reason})."
            )
            if advisory_failing_checks:
                verdict_reason += f" Advisory (non-blocking) failures: {', '.join(advisory_failing_checks)}."
    if ci_timed_out_checks:
        verdict_reason += f" CI timed out after >=5m queued with 0 failures per AGENTS.md §6 (non-deploy-critical): {', '.join(ci_timed_out_checks)}."
    verdict_reason += " Content freshness: " + json.dumps(content_review, sort_keys=True)
    if verify_receipt_verdict and verify_receipt_verdict != "EXEMPT":
        verdict_reason += f" Verification receipt: {verify_receipt_verdict}."
    if qa_receipt_verdict and qa_receipt_verdict != "EXEMPT":
        verdict_reason += f" Browser QA receipt: {qa_receipt_verdict}."
    if flow_qa_receipt_verdict and flow_qa_receipt_verdict != "EXEMPT":
        verdict_reason += f" Flow QA receipt: {flow_qa_receipt_verdict}."
    if released_critical_checks:
        verdict_reason += f" Deploy-critical checks released: local tests recorded ({', '.join(released_critical_checks)})."
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
        review_decision=review_decision,
        review_decision_reason=review_decision_reason,
        decision_line=decision_line,
        verify_receipt_verdict=verify_receipt_verdict,
        verify_receipt_reason=verify_receipt_reason,
        verify_receipt=verify_receipt,
        qa_receipt_verdict=qa_receipt_verdict,
        qa_receipt_reason=qa_receipt_reason,
        qa_receipt_url=qa_receipt_url,
        flow_qa_receipt_verdict=flow_qa_receipt_verdict,
        flow_qa_receipt_reason=flow_qa_receipt_reason,
        flow_qa_receipt_url=flow_qa_receipt_url,
        released_checks=released_critical_checks,
        local_tests_record=local_tests_record if is_valid_ltr else None,
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
    parser.add_argument(
        "--verify-receipt",
        default=None,
        help="Path to a verify-receipt/v1 JSON verification receipt",
    )
    parser.add_argument(
        "--require-verify-receipt",
        action="store_true",
        help="Require a valid verification receipt; missing or failed receipt blocks gate",
    )
    parser.add_argument(
        "--local-tests-record",
        default=None,
        help="Path to a JSON file with local test results {head_sha, commands, passed, failed}",
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
        verify_receipt_data = None
        if args.verify_receipt:
            if not os.path.exists(args.verify_receipt):
                sys.stderr.write(f"Verification receipt file not found: {args.verify_receipt}\n")
                sys.exit(2)
            with open(args.verify_receipt, "r", encoding="utf-8") as vf:
                verify_receipt_data = json.load(vf)
        local_tests_record_data = None
        if args.local_tests_record:
            if not os.path.exists(args.local_tests_record):
                sys.stderr.write(f"Local tests record file not found: {args.local_tests_record}\n")
                sys.exit(2)
            with open(args.local_tests_record, "r", encoding="utf-8") as ltr_f:
                local_tests_record_data = json.load(ltr_f)
        eval_result = evaluate_pr_gate(
            pr_data=pr_data,
            repo=repo,
            expected_head_sha=args.head_sha,
            review_artifact=review_artifact,
            policy=policy,
            native_required_contexts=fetch_required_contexts(repo, base_ref) if base_ref else None,
            verify_receipt=verify_receipt_data,
            require_verify_receipt=args.require_verify_receipt or bool(args.verify_receipt),
            local_tests_record=local_tests_record_data,
        )
    except subprocess.TimeoutExpired as e:
        sys.stderr.write(f"BLOCKED: git or GitHub process timed out after {e.timeout} seconds\n")
        sys.exit(2)
    except Exception as e:
        sys.stderr.write(f"PR Gate evaluation failed: {e}\n")
        sys.exit(1)

    if args.json:
        print(json.dumps(eval_result.to_dict(), indent=2))
    else:
        print(eval_result.decision_line)
        print(eval_result.to_compact_markdown())
    if eval_result.gate_verdict != "PASSED":
        sys.exit(2)


if __name__ == "__main__":
    main()
