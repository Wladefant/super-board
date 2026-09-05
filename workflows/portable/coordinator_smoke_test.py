#!/usr/bin/env python3
"""
Portable Workflow Coordinator Smoke Test Suite (~/.veyyon/workflows/coordinator_smoke_test.py)

Validates:
  1. Package export procedure to an isolated temporary directory.
  2. Standalone execution with NO .veyyon path dependencies.
  3. Graceful handling of missing optional tools (gh, veyyon).
  4. Explicit boundary enforcement (no auto-merge, no auto-deploy, no self-spawn).
  5. Isolated synthetic request lifecycle across both task types:
     a) local_doc task:
        implementation -> preflight exempt -> decision blocker (wait) ->
        resolve decision -> QA (ready with evidence packet) -> review -> done.
     b) deployable task:
        implementation -> preflight gate missing staging probe (block) ->
        record preflight staging probe -> preflight passes (ready) ->
        awaiting authorization (wait, no auto-merge) ->
        authorized by operator -> integration -> live verification -> proof -> done.
  6. Protection of primary ledger: main ledger requests are NEVER marked done.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON_EXE = sys.executable

EXPORT_FILES = [
    "coordinator.py",
    "ledger.py",
    "decision_workflow.py",
    "preflight.py",
    "balance_loader.py",
    "model_routing.py",
    "github_plan_renderer.py",
    "github_plan_templates.py",
    "usage_fixture.json",
    "manifest.json",
    "PORTABLE.md",
    "project_adapter.py",
    "superboard_adapter.py",
    "github_pr_gate.py",
    "telegram_notifier.py",
    "diagnostics.py",
]


def log_test(title: str):
    print("\n" + "=" * 70)
    print(f"TEST: {title}")
    print("=" * 70)


def assert_true(condition: bool, message: str):
    if not condition:
        sys.stderr.write(f"\n[ASSERTION FAILED]: {message}\n")
        sys.exit(1)
    print(f"  [PASS] {message}")


def test_export_package(export_dir: str):
    log_test("Export portable workflow package to isolated directory")
    os.makedirs(export_dir, exist_ok=True)

    for fname in EXPORT_FILES:
        src = os.path.join(SCRIPT_DIR, fname)
        dst = os.path.join(export_dir, fname)
        assert_true(os.path.exists(src), f"Source file exists: {fname}")
        shutil.copy2(src, dst)
        assert_true(os.path.exists(dst), f"Exported file exists: {dst}")

    # Verify manifest in export
    manifest_path = os.path.join(export_dir, "manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    assert_true(manifest.get("name") == "portable-workflow-core", "Manifest name verified")
    print(f"Export completed successfully to: {export_dir}")


def test_standalone_coordinator_execution(export_dir: str):
    log_test("Standalone coordinator execution with no Veyyon requirement")
    coordinator_script = os.path.join(export_dir, "coordinator.py")
    fixture_path = os.path.join(export_dir, "usage_fixture.json")

    # Run coordinator with --summary and --json in export_dir
    cmd = [
        PYTHON_EXE,
        coordinator_script,
        "--state-dir", export_dir,
        "--usage-adapter", "file",
        "--balance-file", fixture_path,
        "--no-sync-decisions",
        "--json",
    ]
    res = subprocess.run(cmd, cwd=export_dir, capture_output=True, text=True)
    assert_true(res.returncode == 0, f"Coordinator exited code 0 (stderr: {res.stderr})")

    packet = json.loads(res.stdout)
    assert_true(packet.get("schema_version") == "1.0", "Packet schema_version is 1.0")
    assert_true("status" in packet, f"Packet has status: {packet.get('status')}")
    assert_true("boundaries" in packet, "Packet includes explicit boundaries")

    bounds = packet["boundaries"]
    assert_true(bounds["auto_merge_allowed"] is False, "Boundary: auto_merge_allowed is False")
    assert_true(bounds["auto_deploy_allowed"] is False, "Boundary: auto_deploy_allowed is False")
    assert_true(bounds["self_spawn_loop"] is False, "Boundary: self_spawn_loop is False")
    assert_true(bounds["execution_dispatched"] is False, "Boundary: execution_dispatched is False")
    print("Standalone coordinator executed cleanly with explicit safety boundaries.")


def test_missing_optional_tools(export_dir: str):
    log_test("Behavior with missing optional tools (gh, veyyon absent from PATH)")
    coordinator_script = os.path.join(export_dir, "coordinator.py")
    fixture_path = os.path.join(export_dir, "usage_fixture.json")

    # Create stripped environment without PATH to gh/veyyon
    minimal_env = os.environ.copy()
    system_root = os.environ.get("SystemRoot", "C:\\Windows")
    minimal_env["PATH"] = f"{system_root};{system_root}\\System32;{os.path.dirname(PYTHON_EXE)}"

    cmd = [
        PYTHON_EXE,
        coordinator_script,
        "--state-dir", export_dir,
        "--usage-adapter", "file",
        "--balance-file", fixture_path,
        "--json",
    ]
    res = subprocess.run(cmd, cwd=export_dir, env=minimal_env, capture_output=True, text=True)
    assert_true(res.returncode == 0, f"Coordinator executed cleanly with missing tools: {res.stderr}")

    packet = json.loads(res.stdout)
    dec_stat = packet["decision_status"]
    assert_true(
        dec_stat["sync_attempted"] is False or dec_stat["sync_success"] is False,
        f"Missing gh gracefully handled: {dec_stat['sync_message']}",
    )
    print("Graceful tool absence verified: zero unhandled exceptions or crashes.")


def test_isolated_synthetic_request_lifecycle(export_dir: str):
    log_test("Full isolated synthetic request lifecycle (local_doc & deployable tasks)")
    state_dir = tempfile.mkdtemp(prefix="synthetic_state_", dir=export_dir)
    try:
        _run_isolated_synthetic_request_lifecycle(export_dir, state_dir)
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def _run_isolated_synthetic_request_lifecycle(export_dir: str, state_dir: str):

    ledger_py = os.path.join(export_dir, "ledger.py")
    decisions_py = os.path.join(export_dir, "decision_workflow.py")
    preflight_py = os.path.join(export_dir, "preflight.py")
    coordinator_py = os.path.join(export_dir, "coordinator.py")
    fixture_path = os.path.join(export_dir, "usage_fixture.json")

    ledger_json = os.path.join(state_dir, "ledger.json")
    decisions_json = os.path.join(state_dir, "decisions.json")
    evidence_dir = os.path.join(state_dir, "preflight_evidence")
    os.makedirs(evidence_dir, exist_ok=True)

    def run_coord(req_id: str = None) -> Dict[str, Any]:
        c_cmd = [
            PYTHON_EXE, coordinator_py,
            "--state-dir", state_dir,
            "--ledger", ledger_json,
            "--decisions", decisions_json,
            "--evidence-dir", evidence_dir,
            "--usage-adapter", "file",
            "--balance-file", fixture_path,
            "--no-sync-decisions",
            "--json",
        ]
        if req_id:
            c_cmd.extend(["--request-id", req_id])
        c_res = subprocess.run(c_cmd, capture_output=True, text=True)
        assert_true(c_res.returncode == 0, f"Coordinator run success: {c_res.stderr}")
        return json.loads(c_res.stdout)

    # =========================================================================
    # Part A: local_doc Task Lifecycle (Exempt Preflight, Decision Block, QA)
    # =========================================================================
    local_req_id = "req-synthetic-localdoc-001"
    add_local_cmd = [
        PYTHON_EXE, ledger_py,
        "--ledger", ledger_json,
        "add",
        "--id", local_req_id,
        "--prompt", "Synthetic workflow coordinator test for local_doc task",
        "--session", "00000000-0000-0000-0000-000000000001",
        "--project", export_dir,
        "--task-type", "local_doc",
        "--owner", "SmokeDocWorker",
        "--criteria", "AC-1: Local doc verification,AC-2: Coordinator contract smoke",
        "--state", "implementation",
        "--labels", "area:harness,local_doc",
        "--next-action", "Draft documentation updates",
        "--issue-number", "4555",
        "--issue-url", "https://github.com/Bavariance/polysimulator/issues/4555",
    ]
    res = subprocess.run(add_local_cmd, capture_output=True, text=True)
    assert_true(res.returncode == 0, f"Added synthetic local_doc request: {res.stderr}")

    # A1: Coordinator evaluates local_doc -> READY (preflight exempt)
    p1 = run_coord(local_req_id)
    assert_true(p1["status"] == "ready", f"local_doc in implementation is 'ready' (got {p1['status']})")
    assert_true(p1["preflight"]["passed"] is True, "Preflight passed for local_doc task")
    assert_true(p1["routing"]["recommended_model"] is not None, f"Model recommended: {p1['routing']['recommended_model']}")

    # A2: Add human decision blocker -> Coordinator WAIT
    dec_id = "DEC-SMOKE-01"
    ask_cmd = [
        PYTHON_EXE, decisions_py,
        "--decisions", decisions_json,
        "--ledger", ledger_json,
        "ask",
        "--id", dec_id,
        "--request-id", local_req_id,
        "--prompt", "Synthetic workflow coordinator test for local_doc task",
        "--question", "Should documentation use single-file or multi-file layout?",
        "--options", '[{"id":"A","label":"Single file","description":"Keep all in PORTABLE.md","tradeoffs":"Compact"},{"id":"B","label":"Multi file","description":"Split into modules","tradeoffs":"Modular"}]',
        "--recommendation", "Option A",
        "--blocks", local_req_id,
        "--authorized", "Wladefant",
        "--issue", "4555",
    ]
    res = subprocess.run(ask_cmd, capture_output=True, text=True)
    assert_true(res.returncode == 0, f"Registered decision: {res.stderr}")

    p2 = run_coord(local_req_id)
    assert_true(p2["status"] == "wait", f"Coordinator emits 'wait' on decision blocker (got {p2['status']})")
    assert_true(p2["decision_status"]["blocking_this_request"] is True, "Decision blocking flag is True")

    # A3: Resolve decision: reply directly via decision_workflow (which resolves ledger directly)
    reply_cmd = [
        PYTHON_EXE, decisions_py,
        "--decisions", decisions_json,
        "--ledger", ledger_json,
        "reply", dec_id,
        "--text", "Option A",
        "--responder", "Wladefant",
        "--comment-id", "999888777",
    ]
    res = subprocess.run(reply_cmd, capture_output=True, text=True)
    assert_true(res.returncode == 0, f"Resolved decision via workflow: {res.stderr}")

    p3 = run_coord(local_req_id)
    assert_true(p3["status"] == "ready", f"Coordinator status returned to 'ready' (got {p3['status']})")
    # A4: Advance local_doc to QA -> READY with evidence packet
    qa_cmd = [
        PYTHON_EXE, ledger_py,
        "--ledger", ledger_json,
        "update", local_req_id,
        "--state", "QA",
    ]
    res = subprocess.run(qa_cmd, capture_output=True, text=True)
    assert_true(res.returncode == 0, f"Advanced local_doc to QA: {res.stderr}")

    p4 = run_coord(local_req_id)
    assert_true(p4["status"] == "ready", f"Coordinator in QA is 'ready' (got {p4['status']})")
    assert_true(p4["evidence_packet"] is not None, "Evidence packet generated for QA")

    # A5: Verify criteria and record proof -> DONE
    crit1_cmd = [
        PYTHON_EXE, ledger_py,
        "--ledger", ledger_json,
        "update", local_req_id,
        "--criterion-id", "AC-1",
        "--criterion-status", "verified",
        "--criterion-evidence", "Local doc verified in PORTABLE.md",
    ]
    subprocess.run(crit1_cmd, check=True)

    crit2_cmd = [
        PYTHON_EXE, ledger_py,
        "--ledger", ledger_json,
        "update", local_req_id,
        "--criterion-id", "AC-2",
        "--criterion-status", "verified",
        "--criterion-evidence", "Smoke test passed in coordinator_smoke_test.py",
    ]
    subprocess.run(crit2_cmd, check=True)

    # In local_doc tasks, state advances QA -> review -> done
    rev_cmd = [
        PYTHON_EXE, ledger_py,
        "--ledger", ledger_json,
        "update", local_req_id,
        "--state", "review",
    ]
    subprocess.run(rev_cmd, check=True)

    proof_cmd = [
        PYTHON_EXE, ledger_py,
        "--ledger", ledger_json,
        "update", local_req_id,
        "--github-proof", "https://github.com/Bavariance/polysimulator/pull/4555",
        "--verify-github-proof",
        "--state", "done",
    ]
    res = subprocess.run(proof_cmd, capture_output=True, text=True)
    assert_true(res.returncode == 0, f"Completed local_doc to done: {res.stderr}")

    # =========================================================================
    # Part B: deployable Task Lifecycle (Preflight Gate, Auth, Integration)
    # =========================================================================
    deploy_req_id = "req-synthetic-deployable-002"
    add_deploy_cmd = [
        PYTHON_EXE, ledger_py,
        "--ledger", ledger_json,
        "add",
        "--id", deploy_req_id,
        "--prompt", "Synthetic deployable task requiring staging preflight",
        "--session", "00000000-0000-0000-0000-000000000002",
        "--project", export_dir,
        "--task-type", "deployable",
        "--owner", "DeployWorker",
        "--criteria", "AC-1: Staging container verified,AC-2: Integration signoff",
        "--state", "implementation",
        "--labels", "runtime,ui",
        "--next-action", "Run staging smoke verification",
        "--issue-number", "4556",
        "--issue-url", "https://github.com/Bavariance/polysimulator/issues/4556",
    ]
    res = subprocess.run(add_deploy_cmd, capture_output=True, text=True)
    assert_true(res.returncode == 0, f"Added synthetic deployable request: {res.stderr}")

    # B1: Deployable task without staging evidence -> Coordinator BLOCK on preflight
    p5 = run_coord(deploy_req_id)
    assert_true(p5["status"] == "block", f"Deployable task without preflight probe is 'block' (got {p5['status']})")
    assert_true("preflight" in p5["status_reason"].lower(), "Status reason mentions preflight")
    print("  [PASS] Preflight gate successfully BLOCKED deployable task lacking staging probe.")

    # B2: Record valid staging probe evidence for dokploy_staging.
    # Timestamps are generated relative to now: a hardcoded time silently turns this suite
    # red once it drifts past ttl_seconds, which is a clock failure, not a code failure.
    probe_now = datetime.datetime.now(datetime.timezone.utc)
    evidence_file = os.path.join(evidence_dir, "dokploy_evidence_payload.json")
    evidence_payload = {
        "service": "dokploy_staging",
        "environment": "staging",
        "target_identity": {
            "compose_id": "TU7b_dY9l9_nCas6YBNwj",
            "app_name": "polysimulator-staging-iad-v09j4g",
        },
        "read_only": True,
        "access_status": "success",
        "timestamp": probe_now.isoformat(),
        "ttl_seconds": 3600,
        "issue": "4556",
        "bounded_utc_logs": {
            "count": 5,
            "first_timestamp": (probe_now - datetime.timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_timestamp": probe_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "baseline_behavior": {
            "container_running": True,
            "status": "running",
        },
        "attestation_source": "cli_record",
    }
    with open(evidence_file, "w", encoding="utf-8") as f:
        json.dump(evidence_payload, f, indent=2)

    record_probe_cmd = [
        PYTHON_EXE, preflight_py,
        "--evidence-dir", evidence_dir,
        "record-evidence",
        "--file", evidence_file,
    ]
    res = subprocess.run(record_probe_cmd, capture_output=True, text=True)
    assert_true(res.returncode == 0, f"Recorded staging preflight evidence: {res.stderr}")
    # B3: With staging evidence recorded -> Coordinator READY
    p6 = run_coord(deploy_req_id)
    assert_true(p6["status"] == "ready", f"Deployable task with preflight probe is 'ready' (got {p6['status']})")
    assert_true(p6["preflight"]["passed"] is True, "Preflight passed with recorded staging probe")

    # B4: A deployable request cannot jump to 'awaiting authorization' from 'implementation':
    # that state asserts QA and review are complete.
    def set_state(state):
        return subprocess.run(
            [
                PYTHON_EXE, ledger_py,
                "--ledger", ledger_json,
                "update", deploy_req_id,
                "--state", state,
            ],
            capture_output=True,
            text=True,
        )

    illegal = set_state("awaiting authorization")
    assert_true(
        illegal.returncode != 0,
        "Deployable task must not reach 'awaiting authorization' straight from 'implementation'",
    )
    print("  [PASS] Authorization gate refuses to skip QA and review.")

    for stage_state in ("QA", "review", "awaiting authorization"):
        res = set_state(stage_state)
        assert_true(res.returncode == 0, f"Transitioned to {stage_state}: {res.stderr}")

    p7 = run_coord(deploy_req_id)
    assert_true(p7["status"] == "wait", f"Coordinator emits 'wait' for awaiting authorization (got {p7['status']})")
    assert_true("awaiting authorization" in p7["status_reason"], "Status reason mentions awaiting authorization")
    print("  [PASS] Invariant enforced: no auto-merge without explicit human authorization.")

    # B5: Grant human authorization -> Advance to integration -> live verification -> done
    auth_cmd = [
        PYTHON_EXE, ledger_py,
        "--ledger", ledger_json,
        "update", deploy_req_id,
        "--authorize",
        "--authorized-by", "operator",
        "--auth-notes", "Authorized by operator for staging integration",
        "--state", "integration",
    ]
    res = subprocess.run(auth_cmd, capture_output=True, text=True)
    assert_true(res.returncode == 0, f"Authorized integration: {res.stderr}")

    to_live_cmd = [
        PYTHON_EXE, ledger_py,
        "--ledger", ledger_json,
        "update", deploy_req_id,
        "--state", "live verification",
    ]
    subprocess.run(to_live_cmd, check=True)

    crit_dep1_cmd = [
        PYTHON_EXE, ledger_py,
        "--ledger", ledger_json,
        "update", deploy_req_id,
        "--criterion-id", "AC-1",
        "--criterion-status", "verified",
        "--criterion-evidence", "Container verified on staging",
    ]
    subprocess.run(crit_dep1_cmd, check=True)

    crit_dep2_cmd = [
        PYTHON_EXE, ledger_py,
        "--ledger", ledger_json,
        "update", deploy_req_id,
        "--criterion-id", "AC-2",
        "--criterion-status", "verified",
        "--criterion-evidence", "Live verification signoff complete",
    ]
    subprocess.run(crit_dep2_cmd, check=True)

    proof_dep_cmd = [
        PYTHON_EXE, ledger_py,
        "--ledger", ledger_json,
        "update", deploy_req_id,
        "--github-proof", "https://github.com/Bavariance/polysimulator/pull/4556",
        "--verify-github-proof",
        "--state", "done",
    ]
    res = subprocess.run(proof_dep_cmd, capture_output=True, text=True)
    assert_true(res.returncode == 0, f"Completed deployable request to done: {res.stderr}")

    # B6: Now that all requests in synthetic ledger are done -> Coordinator emits DONE
    p8 = run_coord()
    assert_true(p8["status"] == "done", f"Coordinator emits 'done' when all requests are completed (got {p8['status']})")
    print("  [PASS] Full synthetic request lifecycle across local_doc and deployable tasks verified cleanly!")


def test_main_ledger_unmodified():
    log_test("Verify primary ledger in ~/.veyyon/workflows was NOT closed or altered")
    main_ledger_path = os.path.join(SCRIPT_DIR, "ledger.json")
    if os.path.exists(main_ledger_path):
        with open(main_ledger_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        requests = data.get("requests", {})
        main_req = requests.get("req-harness-continuous-orchestration")
        if main_req:
            assert_true(
                main_req.get("state") != "done",
                f"Primary task 'req-harness-continuous-orchestration' remains '{main_req.get('state')}' (NOT falsely closed)"
            )
            print("Primary ledger safety verified: actual main task preserved in active progress.")


def main():
    print("\n" + "#" * 70)
    print("STARTING PORTABLE WORKFLOW COORDINATOR SMOKE TEST SUITE")
    print("#" * 70)
    temp_export_dir = tempfile.mkdtemp(prefix="portable_workflow_smoke_test_")
    try:
        test_export_package(temp_export_dir)
        test_standalone_coordinator_execution(temp_export_dir)
        test_missing_optional_tools(temp_export_dir)
        test_isolated_synthetic_request_lifecycle(temp_export_dir)
        test_main_ledger_unmodified()

        print("\n" + "#" * 70)
        print("ALL 5 SMOKE TEST SUITES PASSED CLEANLY (100% SUCCESS)")
        print(f"Export Directory Verified: {temp_export_dir}")
        print("#" * 70)
    finally:
        shutil.rmtree(temp_export_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
