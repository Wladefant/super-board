#!/usr/bin/env python3
"""Post-Deploy QA Orchestrator (workflows/portable/post_deploy_qa.py).

Verifies deployed web application health and revision alignment following merge-first deployment.
Orchestrates served revision confirmation, FLOW-QA execution via build_slot, PR/issue receipt posting,
and automatic project card blocking with explicit revert instructions on failure.

Workflow:
  1. Production Guard:
     Strictly refuses PolySimulator production hosts (zaraprptkegxqpvnsubu, akamai-iad-prod,
     polysimulator.com, www.polysimulator.com, app.polysimulator.com, prod.polysimulator.com)
     including any redirect targets encountered during probing.

  2. Served Revision Check:
     Fetches the served revision from the version endpoint (<base-url>/api/version).
     Validates that the response contains a 40-character hexadecimal commit SHA.
     Compares the served SHA against the expected merge commit SHA.
     A mismatch halts execution and triggers the failure workflow.

  3. Dry-Run Mode (--dry-run):
     Contacts the version endpoint and reports the execution plan without:
       - executing browser flows,
       - posting GitHub PR or issue comments,
       - mutating Superboard Project cards.

  4. FLOW-QA Execution:
     Invokes flow_qa_runner.mjs under build_slot.py with `--class browser`.
     Executes touch, keyboard, and viewport assertions across mobile (390x844),
     keyboard-open (390x420), and desktop (1440x900) viewports.
     Captures the resulting FLOW-QA receipt containing the served SHA, assertion counts,
     and viewport coverage.

  5. Receipt Posting:
     Posts the FLOW-QA receipt as a comment to both the pull request (--pr) and the
     tracking issue (--issue) using `gh pr comment` and `gh issue comment`.

  6. Failure Handling & Revert Instructions:
     If FLOW-QA fails after the served SHA and PR merge identity match:
       - Outputs the exact manual recovery commands:
           git revert -m 1 <MERGE_SHA>
           git push origin <BASE_BRANCH>
           <DEPLOY_CMD>
       - Marks the Superboard Project card as 'Blocked'.
       - The merging lane executes recovery and confirms the previous behavior.
     Unknown or mismatched identity prints "served SHA does not match: do not revert, ask Main".
     The helper does not run rollback, own an app-wide lock, or start a deadline daemon.
     Lanes coordinate deployment ownership over IRC.

Invariants:
  - Subprocesses use `creationflags=subprocess.CREATE_NO_WINDOW` and mandatory timeouts.
  - Explicit identifiers (--expected-sha, --pr, --issue, --base, --deploy-cmd, --base-url) are required.
  - Production hosts are strictly forbidden under all circumstances.

Usage:
  python post_deploy_qa.py \\
    --base-url https://pinthread.dev \\
    --expected-sha 254b94b91e1ada39bcafe66b58e92bc235e14f09 \\
    --base main \\
    --pr 123 \\
    --issue 456 \\
    --deploy-cmd "dokploy deploy pinthread" \\
    --repo Wladefant/pinthread

Dry-run:
  python post_deploy_qa.py \\
    --base-url https://pinthread.dev \\
    --expected-sha 254b94b91e1ada39bcafe66b58e92bc235e14f09 \\
    --base main \\
    --pr 123 \\
    --issue 456 \\
    --deploy-cmd "dokploy deploy pinthread" \\
    --repo Wladefant/pinthread \\
    --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.I)

FORBIDDEN_PRODUCTION_DOMAINS = [
    "polysimulator.com",
    "www.polysimulator.com",
    "app.polysimulator.com",
    "prod.polysimulator.com",
]

FORBIDDEN_HOST_TOKENS = [
    "zaraprptkegxqpvnsubu",  # PolySimulator Supabase production
    "akamai-iad-prod",       # PolySimulator Akamai production
]

DEFAULT_BUILD_SLOT_PY = Path("C:/Users/wkiri/.veyyon/workflows/build_slot.py")
DEFAULT_FLOW_QA_RUNNER = HERE / "flow_qa_runner.mjs"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Prevent automatic redirect following to inspect intermediate hops."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def norm_host(url_or_host: str) -> str:
    """Normalize URL or hostname to lowercase hostname without port or userinfo."""
    text = url_or_host.strip()
    if "://" not in text:
        text = f"http://{text}"
    parsed = urllib.parse.urlparse(text)
    return (parsed.hostname or "").lower().rstrip(".")


def check_forbidden_host(url_or_host: str) -> None:
    """Raise ValueError if the host is a PolySimulator production host or token."""
    host = norm_host(url_or_host)
    if not host:
        return

    if host in FORBIDDEN_PRODUCTION_DOMAINS or any(
        host == d or host.endswith(f".{d}") for d in FORBIDDEN_PRODUCTION_DOMAINS
    ):
        raise ValueError(f"Refused production host: {host}")

    if any(token in host for token in FORBIDDEN_HOST_TOKENS):
        raise ValueError(f"Refused production host (token match): {host}")


def probe_redirect_chain(url: str, timeout: float = 10.0, max_hops: int = 5) -> str:
    """Follow redirect hops iteratively, checking every hop against forbidden hosts."""
    current = url
    opener = urllib.request.build_opener(_NoRedirect)

    for _ in range(max_hops + 1):
        check_forbidden_host(current)
        req = urllib.request.Request(
            current,
            headers={
                "User-Agent": "post-deploy-qa/1.0",
                "Accept": "application/json, text/html, */*",
            },
        )
        try:
            with opener.open(req, timeout=timeout) as res:
                code = getattr(res, "status", 200)
                location = res.headers.get("Location")
        except urllib.error.HTTPError as exc:
            code = exc.code
            location = exc.headers.get("Location")
        except OSError:
            # Non-redirect network or resolution issue; leave to caller
            return current

        if code in (301, 302, 303, 307, 308) and location:
            current = urllib.parse.urljoin(current, location)
            check_forbidden_host(current)
            continue
        return current

    raise ValueError(f"Too many redirects encountered while probing {url}")


def validate_target_url(url: str, timeout: float = 10.0) -> str:
    """Ensure start URL and all redirect targets do not reach forbidden production hosts."""
    check_forbidden_host(url)
    final_url = probe_redirect_chain(url, timeout=timeout)
    check_forbidden_host(final_url)
    return final_url


def fetch_served_sha(url_or_base: str, timeout: float = 15.0) -> str:
    """Read the actual version response with guarded redirects, not a separate probe."""
    text = url_or_base.strip()
    version_url = text if text.endswith(("/api/version", "/version")) else f"{text.rstrip('/')}/api/version"
    opener = urllib.request.build_opener(_NoRedirect())
    for _ in range(6):
        check_forbidden_host(version_url)
        req = urllib.request.Request(version_url, headers={"User-Agent": "post-deploy-qa/1.0", "Accept": "application/json"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                check_forbidden_host(resp.geturl())
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308) or not exc.headers.get("Location"):
                raise ValueError(f"Failed to fetch version from {version_url}: {exc}") from exc
            version_url = urllib.parse.urljoin(version_url, exc.headers["Location"])
        except Exception as exc:
            raise ValueError(f"Failed to fetch version from {version_url}: {exc}") from exc
    else:
        raise ValueError("Too many version redirects")
    if not isinstance(data, dict):
        raise ValueError("Version endpoint returned a non-object payload")
    for key in ("commit", "sha", "served_sha", "version", "git_sha", "commitSha"):
        value = data.get(key)
        if isinstance(value, str) and SHA_RE.fullmatch(value.strip()):
            return value.strip().lower()
    raise ValueError(f"No valid 40-hex commit SHA found in response from {version_url}")


def validate_required_identifiers(
    base_url: Optional[str] = None,
    expected_sha: Optional[str] = None,
    base_branch: Optional[str] = None,
    pr: Optional[Any] = None,
    issue: Optional[Any] = None,
    deploy_cmd: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """Ensure all required identifiers are explicitly provided."""
    required = {
        "base_url": base_url,
        "expected_sha": expected_sha,
        "base_branch": base_branch,
        "pr": pr,
        "issue": issue,
        "deploy_cmd": deploy_cmd,
    }
    for name, val in required.items():
        if val is None or (isinstance(val, str) and not val.strip()):
            raise ValueError(f"Missing required identifier: {name}")

    sha_str = str(expected_sha).strip()
    if not SHA_RE.fullmatch(sha_str):
        raise ValueError(f"expected_sha must be a full 40-hex commit SHA, got '{expected_sha}'")


def format_revert_instructions(
    merge_sha: str,
    base_branch: str,
    deploy_cmd: str,
    reason: str = "",
) -> str:
    """Format exact recovery instructions for manual operator execution."""
    if "mismatch" in reason.lower() or "fetch failed" in reason.lower():
        return "served SHA does not match: do not revert, ask Main\n"
    reason_line = f"Reason: {reason}\n" if reason else ""
    return (
        f"================================================================================\n"
        f"POST-DEPLOY QA FAILURE - MANUAL RECOVERY REQUIRED\n"
        f"================================================================================\n"
        f"{reason_line}"
        f"Execute the following commands to revert the failed merge and restore the base branch:\n\n"
        f"  git revert -m 1 {merge_sha}\n"
        f"  git push origin {base_branch}\n"
        f"  {deploy_cmd}\n\n"
        f"NOTE: Revert commands are NOT executed automatically.\n"
        f"================================================================================\n"
    )


def verify_merge_identity(repo: str, pr: int, merge_sha: str, base_branch: str) -> bool:
    """Confirm the lane supplied this merged PR's merge commit, not its head."""
    proc = subprocess.run(["gh", "pr", "view", str(pr), "--repo", repo, "--json", "state,mergeCommit,baseRefName"], capture_output=True, text=True, timeout=25, check=False, creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        return False
    try:
        data = json.loads(proc.stdout)
        return data.get("state") == "MERGED" and data.get("baseRefName") == base_branch and str((data.get("mergeCommit") or {}).get("oid", "")).lower() == merge_sha.lower()
    except (ValueError, TypeError):
        return False


def runner_supports_storage_state(runner_path: Optional[Path]) -> bool:
    """Check if flow QA runner script supports --storage-state flag."""
    target = runner_path or DEFAULT_FLOW_QA_RUNNER
    try:
        p = Path(target)
        if p.is_file():
            text = p.read_text(encoding="utf-8", errors="ignore")
            return "--storage-state" in text
    except Exception:
        pass
    return False


def is_post_outcome_ok(res: Any) -> bool:
    """Check if post_github_comment result indicates success."""
    if isinstance(res, (tuple, list)) and len(res) >= 1:
        return res[0] == 0
    if isinstance(res, int):
        return res == 0
    if hasattr(res, "returncode"):
        rc = getattr(res, "returncode")
        if isinstance(rc, int):
            return rc == 0
    if hasattr(res, "ok"):
        ok_val = getattr(res, "ok")
        if isinstance(ok_val, bool):
            return ok_val
    return True


def is_card_outcome_ok(outcome: Any) -> bool:
    """Check if update_project_card_status result indicates success."""
    if outcome is None:
        return False
    if isinstance(outcome, bool):
        return outcome
    if hasattr(outcome, "ok"):
        return bool(outcome.ok)
    if hasattr(outcome, "returncode"):
        return outcome.returncode == 0
    return True


def verify_flow_qa_pass(
    receipt_text: str,
    expected_sha: str,
    report_data: Optional[Dict[str, Any]] = None,
    returncode: int = 0,
) -> Tuple[bool, str]:
    """Verify that FLOW-QA output meets exact SHA and positive assertion criteria."""
    if returncode != 0:
        return False, f"Runner exited with non-zero return code {returncode}"

    clean_expected = expected_sha.strip().lower()

    # Exact SHA in FLOW-QA: PASS marker
    marker_match = re.search(r"^\s*FLOW-QA:\s+PASS\s+([0-9a-fA-F]{40})\b", receipt_text, re.MULTILINE)
    if not marker_match:
        return False, "Missing exact FLOW-QA: PASS <40-hex-sha> marker in receipt"

    marker_sha = marker_match.group(1).lower()
    if marker_sha != clean_expected:
        return False, f"Receipt SHA mismatch: marker has {marker_sha}, expected {clean_expected}"

    # Parse assertion counts from receipt_text
    assert_match = re.search(r"FLOW-QA-ASSERTIONS\s+pass=(\d+)\s+fail=(\d+)", receipt_text)
    receipt_pass = None
    receipt_fail = None
    if assert_match:
        receipt_pass = int(assert_match.group(1))
        receipt_fail = int(assert_match.group(2))

    # Also inspect report_data if available
    report_pass = None
    report_fail = None
    if isinstance(report_data, dict):
        if report_data.get("passed") is False:
            return False, "report.json indicates passed=false"
        if isinstance(report_data.get("assertions"), dict):
            assertions = report_data["assertions"]
            if "passed" in assertions and isinstance(assertions["passed"], (int, float)):
                report_pass = int(assertions["passed"])
            if "failed" in assertions and isinstance(assertions["failed"], (int, float)):
                report_fail = int(assertions["failed"])

    final_pass = receipt_pass if receipt_pass is not None else report_pass
    final_fail = receipt_fail if receipt_fail is not None else report_fail

    if final_pass is None or final_fail is None:
        return False, "No parsed assertion counts found in receipt or report"

    if final_pass <= 0:
        return False, f"Parsed pass assertion count is not positive: pass={final_pass}"

    if final_fail != 0:
        return False, f"Parsed fail assertion count is non-zero: fail={final_fail}"

    if receipt_fail is not None and receipt_fail > 0:
        return False, f"Receipt reported failed assertions: fail={receipt_fail}"
    if report_fail is not None and report_fail > 0:
        return False, f"Report reported failed assertions: fail={report_fail}"

    return True, "ok"


def update_project_card_status(
    issue: int,
    repo: str,
    state: str,
    head_sha: Optional[str] = None,
    dry_run: bool = False,
) -> Any:
    """Update Superboard Project card status using existing project adapter mechanisms."""
    try:
        from project_adapter import update_project_lifecycle

        outcome = update_project_lifecycle(
            request_id=f"issue-{issue}",
            state=state,
            head_sha=head_sha,
            issue_number=int(issue),
            dry_run=dry_run,
        )
        return outcome
    except Exception:
        # Fallback to invoking project_adapter.py directly via subprocess
        adapter_path = HERE / "project_adapter.py"
        cmd = [
            sys.executable,
            str(adapter_path),
            "update-lifecycle",
            "--issue",
            str(issue),
            "--state",
            state,
        ]
        if head_sha:
            cmd.extend(["--head-sha", head_sha])
        if dry_run:
            cmd.append("--dry-run")
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                creationflags=CREATE_NO_WINDOW,
            )
            return type("Outcome", (), {"ok": proc.returncode == 0, "blocked_reason": proc.stderr})()
        except Exception as exc:
            return type("Outcome", (), {"ok": False, "blocked_reason": str(exc)})()


def post_github_comment(
    target_type: str,
    number: int,
    body: str,
    repo: Optional[str] = None,
    timeout: int = 20,
) -> Tuple[int, str, str]:
    """Post comment to PR or Issue via `gh` CLI."""
    cmd = ["gh", target_type, "comment", str(number)]
    if repo:
        cmd.extend(["-R", repo])
    cmd.extend(["--body-file", "-"])

    try:
        proc = subprocess.run(
            cmd,
            input=body,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as e:
        return 1, "", str(e)


def run_flow_qa(
    base_url: str,
    expected_sha: str,
    project: str = "pinthread",
    flows: Optional[List[str]] = None,
    flow: Optional[str] = None,
    output_dir: Optional[str] = None,
    build_slot_py: Optional[Path] = None,
    runner_path: Optional[Path] = None,
    timeout: int = 300,
    storage_state: Optional[str] = None,
) -> Dict[str, Any]:
    """Run flow_qa_runner.mjs using build_slot.py with --class browser."""
    slot_script = build_slot_py or DEFAULT_BUILD_SLOT_PY
    qa_runner = runner_path or DEFAULT_FLOW_QA_RUNNER
    out_dir = Path(output_dir or tempfile.mkdtemp(prefix="flow_qa_run_"))
    out_dir.mkdir(parents=True, exist_ok=True)

    slot_name = f"flow-qa-{project}-{os.getpid()}"
    cmd = [
        sys.executable,
        str(slot_script),
        "run",
        slot_name,
        "--class",
        "browser",
        "--",
        "node",
        str(qa_runner),
        "--project",
        project,
        "--base-url",
        base_url,
        "--expected-sha",
        expected_sha,
        "--output",
        str(out_dir),
    ]
    if flow:
        cmd.extend(["--flow", flow])
    if flows:
        cmd.extend(["--flows", ",".join(flows)])
    if storage_state and runner_supports_storage_state(qa_runner):
        cmd.extend(["--storage-state", str(storage_state)])

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        receipt_text = (
            f"FLOW-QA: FAIL {expected_sha}\n"
            f"FLOW-QA-REASON timeout\n"
            f"Error: FLOW-QA runner timed out after {timeout}s\n"
        )
        return {
            "passed": False,
            "receipt": receipt_text,
            "report": {"passed": False, "error": f"Timed out after {timeout}s"},
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"Timed out after {timeout}s",
            "timed_out": True,
        }

    receipt_file = out_dir / "receipt.txt"
    report_file = out_dir / "report.json"

    receipt_text = ""
    if receipt_file.is_file():
        receipt_text = receipt_file.read_text(encoding="utf-8")
    elif "FLOW-QA:" in proc.stdout:
        receipt_text = proc.stdout

    report_data = {}
    if report_file.is_file():
        try:
            report_data = json.loads(report_file.read_text(encoding="utf-8"))
        except Exception:
            pass

    passed, verify_reason = verify_flow_qa_pass(
        receipt_text=receipt_text,
        expected_sha=expected_sha,
        report_data=report_data,
        returncode=proc.returncode,
    )
    if not receipt_text:
        receipt_text = (
            f"FLOW-QA: FAIL {expected_sha}\n"
            f"Error: Runner exited with rc={proc.returncode}: {proc.stderr.strip() or verify_reason or 'Unknown error'}\n"
        )

    return {
        "passed": passed,
        "receipt": receipt_text,
        "report": report_data,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "verify_reason": verify_reason,
    }


def run_post_deploy_qa(
    base_url: str,
    expected_sha: str,
    base_branch: str,
    pr: int,
    issue: int,
    deploy_cmd: str,
    repo: Optional[str] = None,
    dry_run: bool = False,
    flow: Optional[str] = None,
    flows: Optional[List[str]] = None,
    project: Optional[str] = None,
    build_slot_py: Optional[Path] = None,
    flow_qa_runner: Optional[Path] = None,
    timeout: int = 300,
    storage_state: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute complete post-deployment QA flow or dry run."""
    validate_required_identifiers(
        base_url=base_url,
        expected_sha=expected_sha,
        base_branch=base_branch,
        pr=pr,
        issue=issue,
        deploy_cmd=deploy_cmd,
    )

    if str(repo or "").lower() in ("bavariance/polysimulator", "wladefant/polysimulator") and base_branch != "staging":
        raise ValueError("PolySimulator permits only the staging branch")
    clean_expected_sha = str(expected_sha).strip().lower()
    validate_target_url(base_url)

    detected_project = project or (repo.split("/")[-1] if repo and "/" in repo else "pinthread")

    # 1. Fetch served SHA
    try:
        served_sha = fetch_served_sha(base_url)
    except Exception as e:
        reason = f"Served SHA fetch failed: {e}"
        print(f"[ERROR] {reason}")
        revert_hint = format_revert_instructions(
            merge_sha=clean_expected_sha,
            base_branch=base_branch,
            deploy_cmd=deploy_cmd,
            reason=reason,
        )
        print(revert_hint)

        if not dry_run:
            card_outcome = update_project_card_status(
                issue=issue,
                repo=repo or "",
                state="Blocked",
                head_sha=clean_expected_sha,
                dry_run=False,
            )
            card_ok = is_card_outcome_ok(card_outcome)
            if not card_ok:
                print(f"[ERROR] Failed to update project card to Blocked for issue #{issue}")

            failure_receipt = f"FLOW-QA: FAIL {clean_expected_sha}\nFLOW-QA-REASON served_sha_fetch_failed\nError: {e}\n"
            pr_res = post_github_comment("pr", pr, failure_receipt, repo=repo)
            issue_res = post_github_comment("issue", issue, failure_receipt, repo=repo)
            pr_ok = is_post_outcome_ok(pr_res)
            issue_ok = is_post_outcome_ok(issue_res)
            publication_ok = pr_ok and issue_ok
            if not publication_ok:
                print(f"[ERROR] Failed to post failure receipt: PR ok={pr_ok}, Issue ok={issue_ok}")
        else:
            card_ok = True
            publication_ok = True

        return {
            "ok": False,
            "dry_run": dry_run,
            "reason": "fetch_failed",
            "error": str(e),
            "revert_hint": revert_hint,
            "card_ok": card_ok,
            "publication_ok": publication_ok,
        }

    # 2. Check for SHA mismatch
    is_match = served_sha == clean_expected_sha
    if is_match and not dry_run:
        try:
            identity_ok = verify_merge_identity(repo or "", pr, clean_expected_sha, base_branch)
        except Exception:
            identity_ok = False
        if not identity_ok:
            message = "served SHA does not match: do not revert, ask Main"
            print(message)
            receipt = f"FLOW-QA: FAIL {served_sha}\nMerge identity could not be confirmed. {message}\n"
            card = update_project_card_status(issue=issue, repo=repo or "", state="Blocked", head_sha=clean_expected_sha, dry_run=False)
            pr_post = post_github_comment("pr", pr, receipt, repo=repo)
            issue_post = post_github_comment("issue", issue, receipt, repo=repo)
            return {"ok": False, "reason": "merge_identity_unconfirmed", "served_sha": served_sha, "receipt": receipt, "revert_hint": message, "card_ok": is_card_outcome_ok(card), "publication_ok": is_post_outcome_ok(pr_post) and is_post_outcome_ok(issue_post)}

    # 3. Handle Dry-Run Mode
    if dry_run:
        print("================================================================================")
        print("[DRY-RUN] Post-Deploy QA Plan")
        print("================================================================================")
        print(f"Target Base URL:       {base_url}")
        print(f"Served SHA Observed:   {served_sha}")
        print(f"Expected Merge SHA:    {clean_expected_sha}")
        status_str = "MATCH" if is_match else "MISMATCH"
        print(f"Served Revision State: {status_str}")
        if is_match:
            print("Served SHA matches expected merge SHA.")
            print(f"Plan: Would execute FLOW-QA for project '{detected_project}' via build_slot.py --class browser.")
            print(f"Plan: Would post FLOW-QA receipt following flow result to PR #{pr} and Issue #{issue}.")
        else:
            print("[DRY-RUN] Served revision does not match expected merge SHA.")
            print(f"Plan: Would post FLOW-QA FAIL receipt to PR #{pr} and Issue #{issue}.")
            print(f"Plan: Would mark Superboard Project card for Issue #{issue} as 'Blocked'.")
            revert_hint = format_revert_instructions(
                merge_sha=clean_expected_sha,
                base_branch=base_branch,
                deploy_cmd=deploy_cmd,
                reason=f"Served SHA mismatch: observed {served_sha}, expected {clean_expected_sha}",
            )
            print(revert_hint)

        update_project_card_status(
            issue=issue,
            repo=repo or "",
            state="Blocked" if not is_match else "QA",
            head_sha=clean_expected_sha,
            dry_run=True,
        )

        return {
            "ok": is_match,
            "dry_run": True,
            "served_sha": served_sha,
            "expected_sha": clean_expected_sha,
        }

    # 4. Handle Served SHA Mismatch in Real Mode
    if not is_match:
        reason = f"Served revision mismatch: observed {served_sha}, expected {clean_expected_sha}"
        print(f"[FAIL] {reason}")
        revert_hint = format_revert_instructions(
            merge_sha=clean_expected_sha,
            base_branch=base_branch,
            deploy_cmd=deploy_cmd,
            reason=reason,
        )
        print(revert_hint)

        card_outcome = update_project_card_status(
            issue=issue,
            repo=repo or "",
            state="Blocked",
            head_sha=clean_expected_sha,
            dry_run=False,
        )
        card_ok = is_card_outcome_ok(card_outcome)
        if not card_ok:
            print(f"[ERROR] Failed to update project card to Blocked for issue #{issue}")

        receipt = (
            f"FLOW-QA: FAIL {clean_expected_sha}\n"
            f"FLOW-QA-REASON served_sha_mismatch\n"
            f"Expected: {clean_expected_sha}\n"
            f"Observed: {served_sha}\n"
        )
        pr_res = post_github_comment("pr", pr, receipt, repo=repo)
        issue_res = post_github_comment("issue", issue, receipt, repo=repo)
        pr_ok = is_post_outcome_ok(pr_res)
        issue_ok = is_post_outcome_ok(issue_res)
        publication_ok = pr_ok and issue_ok
        if not publication_ok:
            print(f"[ERROR] Failed to post failure receipt: PR ok={pr_ok}, Issue ok={issue_ok}")

        return {
            "ok": False,
            "dry_run": False,
            "reason": "served_sha_mismatch",
            "served_sha": served_sha,
            "expected_sha": clean_expected_sha,
            "receipt": receipt,
            "revert_hint": revert_hint,
            "card_ok": card_ok,
            "publication_ok": publication_ok,
        }

    # 5. Run FLOW-QA via build_slot.py --class browser
    print(f"[RUN] Served revision matches ({served_sha}). Running FLOW-QA via build_slot.py --class browser...")
    try:
        flow_outcome = run_flow_qa(
            base_url=base_url,
            expected_sha=clean_expected_sha,
            project=detected_project,
            flows=flows,
            flow=flow,
            build_slot_py=build_slot_py,
            runner_path=flow_qa_runner,
            timeout=timeout,
            storage_state=storage_state,
        )
    except subprocess.TimeoutExpired as exc:
        reason = f"FLOW-QA execution timed out after {timeout}s: {exc}"
        print(f"[FAIL] {reason}")
        receipt = (
            f"FLOW-QA: FAIL {clean_expected_sha}\n"
            f"FLOW-QA-REASON timeout\n"
            f"Error: {reason}\n"
        )
        revert_hint = format_revert_instructions(
            merge_sha=clean_expected_sha,
            base_branch=base_branch,
            deploy_cmd=deploy_cmd,
            reason=reason,
        )
        print(revert_hint)

        card_outcome = update_project_card_status(
            issue=issue,
            repo=repo or "",
            state="Blocked",
            head_sha=clean_expected_sha,
            dry_run=False,
        )
        card_ok = is_card_outcome_ok(card_outcome)
        if not card_ok:
            print(f"[ERROR] Failed to update project card to Blocked for issue #{issue}")

        pr_res = post_github_comment("pr", pr, receipt, repo=repo)
        issue_res = post_github_comment("issue", issue, receipt, repo=repo)
        pr_ok = is_post_outcome_ok(pr_res)
        issue_ok = is_post_outcome_ok(issue_res)
        publication_ok = pr_ok and issue_ok
        if not publication_ok:
            print(f"[ERROR] Failed to post failure receipt: PR ok={pr_ok}, Issue ok={issue_ok}")

        return {
            "ok": False,
            "dry_run": False,
            "reason": "timeout",
            "served_sha": served_sha,
            "expected_sha": clean_expected_sha,
            "receipt": receipt,
            "revert_hint": revert_hint,
            "error": str(exc),
            "card_ok": card_ok,
            "publication_ok": publication_ok,
        }
    except Exception as exc:
        reason = f"FLOW-QA runner exception: {exc}"
        print(f"[FAIL] {reason}")
        receipt = (
            f"FLOW-QA: FAIL {clean_expected_sha}\n"
            f"FLOW-QA-REASON runner_exception\n"
            f"Error: {reason}\n"
        )
        revert_hint = format_revert_instructions(
            merge_sha=clean_expected_sha,
            base_branch=base_branch,
            deploy_cmd=deploy_cmd,
            reason=reason,
        )
        print(revert_hint)

        card_outcome = update_project_card_status(
            issue=issue,
            repo=repo or "",
            state="Blocked",
            head_sha=clean_expected_sha,
            dry_run=False,
        )
        card_ok = is_card_outcome_ok(card_outcome)
        if not card_ok:
            print(f"[ERROR] Failed to update project card to Blocked for issue #{issue}")

        pr_res = post_github_comment("pr", pr, receipt, repo=repo)
        issue_res = post_github_comment("issue", issue, receipt, repo=repo)
        pr_ok = is_post_outcome_ok(pr_res)
        issue_ok = is_post_outcome_ok(issue_res)
        publication_ok = pr_ok and issue_ok
        if not publication_ok:
            print(f"[ERROR] Failed to post failure receipt: PR ok={pr_ok}, Issue ok={issue_ok}")

        return {
            "ok": False,
            "dry_run": False,
            "reason": "runner_exception",
            "served_sha": served_sha,
            "expected_sha": clean_expected_sha,
            "receipt": receipt,
            "revert_hint": revert_hint,
            "error": str(exc),
            "card_ok": card_ok,
            "publication_ok": publication_ok,
        }

    receipt = flow_outcome.get("receipt", "")
    passed = flow_outcome.get("passed", False)

    # 6. Post receipts to PR and Issue
    print(f"[POST] Posting FLOW-QA receipt to PR #{pr} and Issue #{issue}...")
    pr_res = post_github_comment("pr", pr, receipt, repo=repo)
    issue_res = post_github_comment("issue", issue, receipt, repo=repo)
    pr_ok = is_post_outcome_ok(pr_res)
    issue_ok = is_post_outcome_ok(issue_res)
    publication_ok = pr_ok and issue_ok

    if not publication_ok:
        print(f"[ERROR] Publication failed: PR comment ok={pr_ok}, Issue comment ok={issue_ok}")

    if not passed:
        reason = "FLOW-QA execution failed or reported assertions failed"
        print(f"[FAIL] {reason}")
        revert_hint = format_revert_instructions(
            merge_sha=clean_expected_sha,
            base_branch=base_branch,
            deploy_cmd=deploy_cmd,
            reason=reason,
        )
        print(revert_hint)

        card_outcome = update_project_card_status(
            issue=issue,
            repo=repo or "",
            state="Blocked",
            head_sha=clean_expected_sha,
            dry_run=False,
        )
        card_ok = is_card_outcome_ok(card_outcome)
        if not card_ok:
            print(f"[ERROR] Failed to update project card to Blocked for issue #{issue}")

        return {
            "ok": False,
            "dry_run": False,
            "reason": "flow_qa_failed",
            "served_sha": served_sha,
            "expected_sha": clean_expected_sha,
            "receipt": receipt,
            "revert_hint": revert_hint,
            "card_ok": card_ok,
            "publication_ok": publication_ok,
        }

    if not publication_ok:
        reason = "Receipt publication to PR or Issue failed"
        print(f"[FAIL] {reason}")
        return {
            "ok": False,
            "dry_run": False,
            "reason": "publication_failed",
            "served_sha": served_sha,
            "expected_sha": clean_expected_sha,
            "receipt": receipt,
            "card_ok": True,
            "publication_ok": False,
            "post_pr_ok": pr_ok,
            "post_issue_ok": issue_ok,
        }

    print(f"[PASS] Post-deploy QA passed. Served SHA verified: {served_sha}")
    return {
        "ok": True,
        "dry_run": False,
        "served_sha": served_sha,
        "expected_sha": clean_expected_sha,
        "receipt": receipt,
        "card_ok": True,
        "publication_ok": True,
    }


def main() -> None:
    """CLI entrypoint for Post-Deploy QA Orchestrator."""
    parser = argparse.ArgumentParser(
        description="Post-Deploy QA Orchestrator: confirms served SHA, runs FLOW-QA, posts receipts, handles reverts."
    )
    parser.add_argument("--base-url", required=True, help="Base URL of deployed service (e.g. https://pinthread.dev)")
    parser.add_argument("--version-url", default=None, help="Explicit version URL (defaults to <base-url>/api/version)")
    parser.add_argument("--expected-sha", "--merge-sha", dest="expected_sha", required=True, help="Expected 40-hex commit SHA")
    parser.add_argument("--base", "--base-branch", dest="base_branch", required=True, help="Target base branch (e.g. main, staging)")
    parser.add_argument("--pr", type=int, required=True, help="Target pull request number")
    parser.add_argument("--issue", type=int, required=True, help="Target tracking issue number")
    parser.add_argument("--deploy-cmd", required=True, help="Explicit command required to re-deploy following a revert")
    parser.add_argument("--repo", default=None, help="GitHub repository (owner/repo)")
    parser.add_argument("--dry-run", action="store_true", help="Probe version endpoint and report plan without mutations")
    parser.add_argument("--flow", default=None, help="Specific flow ID to execute")
    parser.add_argument("--flows", default=None, help="Comma-separated flow IDs to execute")
    parser.add_argument("--project", default=None, help="Project name for FLOW-QA runner")
    parser.add_argument("--build-slot-py", default=None, help="Path to build_slot.py")
    parser.add_argument("--flow-qa-runner", default=None, help="Path to flow_qa_runner.mjs")
    parser.add_argument("--timeout", type=int, default=300, help="Subprocess timeout in seconds")
    parser.add_argument(
        "--storage-state",
        default=None,
        help="Path to browser storage state JSON (cookies/localStorage) for authenticated flows",
    )

    args = parser.parse_args()

    flows_list = [f.strip() for f in args.flows.split(",") if f.strip()] if args.flows else None

    try:
        outcome = run_post_deploy_qa(
            base_url=args.version_url or args.base_url,
            expected_sha=args.expected_sha,
            base_branch=args.base_branch,
            pr=args.pr,
            issue=args.issue,
            deploy_cmd=args.deploy_cmd,
            repo=args.repo,
            dry_run=args.dry_run,
            flow=args.flow,
            flows=flows_list,
            project=args.project,
            build_slot_py=Path(args.build_slot_py) if args.build_slot_py else None,
            flow_qa_runner=Path(args.flow_qa_runner) if args.flow_qa_runner else None,
            timeout=args.timeout,
            storage_state=args.storage_state,
        )
    except ValueError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(2)

    sys.exit(0 if outcome.get("ok") else 1)


if __name__ == "__main__":
    main()
