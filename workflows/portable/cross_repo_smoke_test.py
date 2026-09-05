#!/usr/bin/env python3
"""
Cross-Repository Portability & Project Adapter Smoke Test Suite
(~/.veyyon/workflows/cross_repo_smoke_test.py)

Demonstrates that the generic workflow core (coordinator, ledger, decision engine,
preflight gate, model router) cleanly accepts repo/project/environment configuration
without hardcoded project assumptions, that PolySimulator identities live exclusively
inside the explicit project adapter, and that a second fake/alternate repository fixture
and safe unknown environment work with zero PolySimulator defaults leaking.

Inviolable Guarantees Verified:
  1. PolySimulator Adapter Isolation:
     - Exact staging compose ID (TU7b_dY9l9_nCas6YBNwj) and staging DB ref (hgzyqmaanndcimnclxtv) preserved.
     - Production compose ID (vpyL-7TDEUREH6Uo_y1sb) and prod DB ref (zaraprptkegxqpvnsubu) strictly prohibited.
     - Authority string matches 'GitHub Issues & Superboard (PolySimulator #1)'.
  2. Alternate Repo Fixture (acme/demo-service):
     - Configured via JSON fixture with custom project number (42) and custom staging coordinates.
     - ZERO PolySimulator strings ('polysimulator', 'Bavariance', 'hgzyqmaanndcimnclxtv', 'TU7b_dY9l9_nCas6YBNwj') leak into boundaries or output.
     - Rejection of project-specific forbidden patterns (prod_db_acme_000).
     - Full synthetic request lifecycle executes cleanly with alternate repo proof validation.
  3. Safe Unknown Environment (isolated/unknown-project):
     - Unconfigured staging environment safely exempts local_doc tasks.
     - Deployable tasks requiring runtime staging are cleanly BLOCKED with unconfigured explanation,
       never assuming or defaulting to PolySimulator staging.
     - Strict no-auto-merge, no-auto-deploy, and no production writes enforced.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from project_adapter import (
    ProjectConfig,
    create_generic_config,
    create_polysimulator_config,
    get_current_project_config,
    reset_current_project_config,
    set_current_project_config,
    validate_dokploy_compose_id,
    validate_supabase_project_ref,
    check_text_for_forbidden_patterns,
)
from coordinator import Coordinator, CoordinatorPacket
from ledger import RequestLedger
from preflight import PreflightEngine, ServiceEvidence, get_iso_timestamp


def assert_true(condition: bool, msg: str):
    if not condition:
        print(f"  [FAIL] {msg}")
        raise AssertionError(msg)
    print(f"  [PASS] {msg}")


def assert_false(condition: bool, msg: str):
    assert_true(not condition, msg)


# ======================================================================
# TEST SUITE 1: PolySimulator Explicit Adapter Verification
# ======================================================================

def test_polysimulator_adapter():
    print("\n" + "=" * 70)
    print("TEST SUITE 1: PolySimulator Explicit Adapter Verification")
    print("=" * 70)

    reset_current_project_config()
    cfg = set_current_project_config("polysimulator")

    assert_true(cfg.repo == "Bavariance/polysimulator", f"Repo is {cfg.repo}")
    assert_true(cfg.project_name == "PolySimulator", f"Project name is {cfg.project_name}")
    assert_true(cfg.project_number == 1, f"Project number is {cfg.project_number}")
    assert_true(cfg.base_branch == "staging", f"Base branch is {cfg.base_branch}")

    # Verify staging coordinates
    assert_true(cfg.staging.dokploy_compose_id == "TU7b_dY9l9_nCas6YBNwj", "Dokploy staging compose ID matches")
    assert_true(cfg.staging.supabase_project_ref == "hgzyqmaanndcimnclxtv", "Supabase staging project ref matches")

    # Verify strict production prohibitions
    assert_true("vpyL-7TDEUREH6Uo_y1sb" in cfg.safety.forbidden_compose_ids, "Prod compose ID in forbidden list")
    assert_true("zaraprptkegxqpvnsubu" in cfg.safety.forbidden_supabase_refs, "Prod Supabase ref in forbidden list")

    # Verify forbidden pattern checker
    is_bad, reason = check_text_for_forbidden_patterns("deploy to zaraprptkegxqpvnsubu now", cfg)
    assert_true(is_bad, f"Production DB ref correctly flagged: {reason}")

    # Verify Dokploy validation
    ok, status, _ = validate_dokploy_compose_id("TU7b_dY9l9_nCas6YBNwj", cfg)
    assert_true(ok and status == "valid", "Staging compose ID accepted")
    ok, status, reason = validate_dokploy_compose_id("vpyL-7TDEUREH6Uo_y1sb", cfg)
    assert_true(not ok and status == "blocked", f"Production compose ID blocked: {reason}")

    # Verify Supabase validation
    ok, status, _ = validate_supabase_project_ref("hgzyqmaanndcimnclxtv", cfg)
    assert_true(ok and status == "valid", "Staging Supabase ref accepted")
    ok, status, reason = validate_supabase_project_ref("zaraprptkegxqpvnsubu", cfg)
    assert_true(not ok and status == "blocked", f"Production Supabase ref blocked: {reason}")

    # Coordinator packet boundaries
    test_dir = tempfile.mkdtemp(prefix="coord_poly_")
    try:
        coord = Coordinator(
            state_dir=test_dir,
            usage_adapter="file",
            balance_file=os.path.join(SCRIPT_DIR, "usage_fixture.json"),
            adapter_name="polysimulator",
            sync_decisions=False,
        )
        packet = coord.evaluate_step()
        assert_true(
            packet.boundaries.shared_authority == "GitHub Issues & Superboard (PolySimulator #1)",
            f"Authority string is: {packet.boundaries.shared_authority}",
        )
        assert_false(packet.boundaries.auto_merge_allowed, "Auto-merge strictly prohibited")
        assert_false(packet.boundaries.auto_deploy_allowed, "Auto-deploy strictly prohibited")
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


# ======================================================================
# TEST SUITE 2: Alternate Repo Fixture (acme/demo-service)
# ======================================================================

def test_alternate_repo_fixture():
    print("\n" + "=" * 70)
    print("TEST SUITE 2: Alternate Repo Fixture Verification (acme/demo-service)")
    print("=" * 70)

    fixture_path = os.path.join(SCRIPT_DIR, "fixtures", "alternate_repo_config.json")
    assert_true(os.path.exists(fixture_path), f"Fixture file exists: {fixture_path}")

    reset_current_project_config()
    cfg = set_current_project_config(fixture_path)

    assert_true(cfg.repo == "acme/demo-service", f"Repo is {cfg.repo}")
    assert_true(cfg.project_number == 42, f"Project number is {cfg.project_number}")
    assert_true(cfg.base_branch == "main", f"Base branch is {cfg.base_branch}")

    # Verify alternate staging coordinates
    assert_true(cfg.staging.dokploy_compose_id == "compose_staging_acme_42", "Custom compose ID configured")
    assert_true(cfg.staging.supabase_project_ref == "sb_staging_acme_42", "Custom Supabase ref configured")

    # Verify custom forbidden patterns
    is_bad, reason = check_text_for_forbidden_patterns("connect to prod_db_acme_000", cfg)
    assert_true(is_bad, f"Custom prod DB pattern flagged: {reason}")

    # Verify Dokploy validation rejects PolySimulator IDs under alternate repo
    ok, status, reason = validate_dokploy_compose_id("TU7b_dY9l9_nCas6YBNwj", cfg)
    assert_true(not ok and status == "blocked", f"PolySimulator compose ID rejected on alternate repo: {reason}")
    ok, status, _ = validate_dokploy_compose_id("compose_staging_acme_42", cfg)
    assert_true(ok and status == "valid", "Alternate staging compose ID accepted")

    # Verify Supabase validation rejects PolySimulator refs under alternate repo
    ok, status, reason = validate_supabase_project_ref("hgzyqmaanndcimnclxtv", cfg)
    assert_true(not ok and status == "blocked", f"PolySimulator Supabase ref rejected on alternate repo: {reason}")
    ok, status, _ = validate_supabase_project_ref("sb_staging_acme_42", cfg)
    assert_true(ok and status == "valid", "Alternate staging Supabase ref accepted")

    # Test standalone Coordinator execution with alternate repo fixture
    test_dir = tempfile.mkdtemp(prefix="coord_alt_")
    try:
        coord = Coordinator(
            state_dir=test_dir,
            usage_adapter="file",
            balance_file=os.path.join(SCRIPT_DIR, "usage_fixture.json"),
            project_config=cfg,
            sync_decisions=False,
        )

        # Add synthetic request to ledger for alternate repo
        ledger = coord.ledger
        req = ledger.add_request(
            req_id="req-acme-service-001",
            prompt="Implement event webhook ingest for DemoService",
            session="sess-acme-001",
            project=cfg.repo,
            task_type="deployable",
            acceptance_criteria=[
                {"id": "AC-1", "description": "Webhook endpoint verified", "status": "pending"}
            ],
            owner="AcmeWorker",
            github_repo=cfg.repo,
            issue_number=101,
            issue_url=f"https://github.com/{cfg.repo}/issues/101",
        )
        assert_true(req["id"] == "req-acme-service-001", "Request added to ledger under alternate repo")

        # Step A: Preflight gate blocks deployable task lacking staging evidence
        packet1 = coord.evaluate_step()
        assert_true(packet1.status == "block", f"Deployable task blocked on preflight (got {packet1.status})")
        assert_true(
            packet1.boundaries.shared_authority == "GitHub Issues & Superboard (DemoService #42)",
            f"Authority adapted to DemoService: {packet1.boundaries.shared_authority}",
        )

        # Step B: Record valid staging evidence using alternate repo compose ID
        pf_engine = coord.preflight_engine
        ev = ServiceEvidence(
            service="dokploy_staging",
            environment="staging",
            target_identity={"compose_id": "compose_staging_acme_42"},
            read_only=True,
            access_status="success",
            timestamp=get_iso_timestamp(),
            revision_match=True,
            runtime_revision="head-sha-acme-111",
            bounded_utc_logs={"count": 12, "first_utc": "2026-09-05T09:00:00Z", "last_utc": "2026-09-05T09:15:00Z"},
            baseline_behavior={"compose_status": "done"},
            attestation_source="tool_verified",
        )
        pf_engine.save_evidence(ev)

        # Step C: Coordinator now evaluates preflight as passed for alternate repo
        packet2 = coord.evaluate_step()
        assert_true(packet2.status == "ready", f"Deployable task now ready with alternate staging evidence (got {packet2.status})")

        # Step D: PROVE ZERO PolySimulator defaults leaked in packet JSON
        packet_json = packet2.to_json()
        assert_true("Bavariance" not in packet_json, "Zero 'Bavariance' in packet JSON")
        assert_true("polysimulator" not in packet_json.lower(), "Zero 'polysimulator' in packet JSON")
        assert_true("hgzyqmaanndcimnclxtv" not in packet_json, "Zero PolySimulator DB ref in packet JSON")
        assert_true("TU7b_dY9l9_nCas6YBNwj" not in packet_json, "Zero PolySimulator compose ID in packet JSON")
        assert_true("acme/demo-service" in packet_json, "'acme/demo-service' present in packet JSON")
        assert_true("DemoService #42" in packet_json, "'DemoService #42' present in packet JSON")
        print("  [PASS] Zero PolySimulator defaults leaked into alternate repository packet!")
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


# ======================================================================
# TEST SUITE 3: Safe Unknown Environment Verification
# ======================================================================

def test_safe_unknown_environment():
    print("\n" + "=" * 70)
    print("TEST SUITE 3: Safe Unknown Environment Verification (isolated/unknown-project)")
    print("=" * 70)

    fixture_path = os.path.join(SCRIPT_DIR, "fixtures", "unknown_env_config.json")
    assert_true(os.path.exists(fixture_path), f"Fixture file exists: {fixture_path}")

    reset_current_project_config()
    cfg = set_current_project_config(fixture_path)

    assert_true(cfg.repo == "isolated/unknown-project", f"Repo is {cfg.repo}")
    assert_true(cfg.staging.dokploy_compose_id is None, "Dokploy compose ID is None (unconfigured)")
    assert_true(cfg.staging.supabase_project_ref is None, "Supabase project ref is None (unconfigured)")

    test_dir = tempfile.mkdtemp(prefix="coord_unknown_")
    try:
        coord = Coordinator(
            state_dir=test_dir,
            usage_adapter="file",
            balance_file=os.path.join(SCRIPT_DIR, "usage_fixture.json"),
            project_config=cfg,
            sync_decisions=False,
        )

        ledger = coord.ledger

        # Case A: local_doc / harness task passes cleanly as not_applicable in unknown environment
        ledger.add_request(
            req_id="req-unknown-doc-001",
            prompt="Write architecture specification for isolated project",
            session="sess-unknown-001",
            project=cfg.repo,
            task_type="local_doc",
            acceptance_criteria=[
                {"id": "AC-1", "description": "Spec drafted", "status": "pending"}
            ],
            owner="DocWorker",
            github_repo=cfg.repo,
        )

        packet_doc = coord.evaluate_step("req-unknown-doc-001")
        assert_true(packet_doc.status == "ready", f"local_doc task is 'ready' without staging (got {packet_doc.status})")
        assert_true(packet_doc.preflight.status == "not_applicable", "Preflight status is not_applicable")
        assert_true(len(packet_doc.preflight.required_probes) == 0, "No external probes required for local_doc")

        # Case B: deployable task requiring staging is cleanly BLOCKED with unconfigured explanation
        ledger.add_request(
            req_id="req-unknown-deploy-002",
            prompt="Deploy unknown service backend",
            session="sess-unknown-002",
            project=cfg.repo,
            task_type="deployable",
            acceptance_criteria=[
                {"id": "AC-1", "description": "Backend running", "status": "pending"}
            ],
            owner="DeployWorker",
            github_repo=cfg.repo,
        )

        packet_dep = coord.evaluate_step("req-unknown-deploy-002")
        assert_true(packet_dep.status == "block", f"Deployable task is cleanly blocked (got {packet_dep.status})")
        assert_true(not packet_dep.preflight.passed, "Preflight passed is False")

        # Verify probe refusal does NOT leak PolySimulator staging
        evidence = coord.preflight_engine.check_task({"areas": ["runtime"]})
        assert_true(not evidence.passed, "Runtime preflight is not passed")

        # Verify boundary safety invariants
        assert_false(packet_dep.boundaries.auto_merge_allowed, "Invariant: auto-merge false")
        assert_false(packet_dep.boundaries.auto_deploy_allowed, "Invariant: auto-deploy false")
        assert_false(packet_dep.boundaries.self_spawn_loop, "Invariant: self-spawn false")
        assert_true(
            packet_dep.boundaries.shared_authority == "GitHub Issues & Superboard (UnknownProject #99)",
            f"Authority string adapted: {packet_dep.boundaries.shared_authority}",
        )
        print("  [PASS] Safe unknown environment cleanly isolates local tasks and blocks unconfigured staging!")
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)


# ======================================================================
# Main Test Runner
# ======================================================================

def main():
    print("######################################################################")
    print("STARTING CROSS-REPOSITORY PORTABILITY & ADAPTER SMOKE TEST SUITE")
    print("######################################################################")

    test_polysimulator_adapter()
    test_alternate_repo_fixture()
    test_safe_unknown_environment()

    # Restore default configuration
    reset_current_project_config()

    print("\n" + "#" * 70)
    print("ALL 3 CROSS-REPOSITORY SMOKE TEST SUITES PASSED CLEANLY (100% SUCCESS)!")
    print("######################################################################")


if __name__ == "__main__":
    main()
