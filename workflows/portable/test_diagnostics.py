#!/usr/bin/env python3
"""
test_diagnostics.py — Focused Unit Tests for Portable Diagnostics

Validates:
1. Real installed state diagnostics:
   - Stale/expired preflight evidence (Dokploy, Supabase, Stripe) recognized as stale.
   - Stripe test blocker reported honestly with confirmed diagnosis (sk_test_ missing).
   - Human input required ONLY for Stripe test credential (with deduplicatable question_id).
   - No secret keys asked for in chat or issues.
   - Missing implementation / pending acceptance criteria remain strictly agent-owned.
   - DEC-4543-01 safety rejection correctly identified with confirmed cause.
   - Host resource telemetry reports valid metrics, or strictly 'unknown' if unavailable.
2. Rejection of fabricated healthy / stale evidence:
   - Expired evidence is NEVER reported as green or healthy.
   - Access success without verified health does not report healthy.
   - Cached credentials are never marked live_verified=True.
   - Production compose ID, Supabase ref, or live keys are strictly blocked.
3. Distinction between confirmed diagnosis vs unknown root cause.
4. CLI commands:
   - python diagnostics.py --summary / --json
   - python coordinator.py --diagnostics --summary / --json
   - python continuation_driver.py --diagnostics
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from diagnostics import (
    DiagnosticCollector,
    DiagnosticReport,
    ServiceDiagnostic,
    ProviderDiagnostic,
    RequestDiagnostic,
    DecisionDiagnostic,
    HostResourceDiagnostic,
    HumanInputItem,
    AgentActionItem,
    format_diagnostic_summary,
)


class TestRealInstalledDiagnostics(unittest.TestCase):
    """Test diagnostics against the actual installed ~/.veyyon/workflows state."""

    def setUp(self):
        self.installed_state_dir = "C:/Users/wkiri/.veyyon/workflows"
        if not os.path.exists(self.installed_state_dir):
            self.skipTest(f"Installed state dir not found: {self.installed_state_dir}")
        self.collector = DiagnosticCollector(state_dir=self.installed_state_dir)

    def test_real_state_diagnostics_report(self):
        report: DiagnosticReport = self.collector.run_diagnostics()

        self.assertEqual(report.schema_version, "1.0")
        self.assertIn(report.aggregate_status, ("awaiting_human", "blocked", "stale"))

        # 1. Verify Services
        services = report.services
        self.assertIn("dokploy_staging", services)
        self.assertIn("supabase_staging", services)
        self.assertIn("stripe_test", services)

        # Dokploy staging must be reported as stale (age > TTL)
        dokploy = services["dokploy_staging"]
        self.assertTrue(dokploy.is_stale, "Dokploy evidence must be marked stale (> TTL)")
        self.assertEqual(dokploy.state, "stale", "Stale evidence state must be 'stale' (never 'healthy')")
        self.assertEqual(dokploy.health_status, "stale")
        self.assertFalse(dokploy.live_verified, "Cached evidence must not claim live_verified")
        self.assertEqual(dokploy.action_owner, "agent_action", "Probing stale runtime is agent action")
        self.assertFalse(dokploy.human_input_needed)

        # Supabase staging must be reported as stale
        supabase = services["supabase_staging"]
        self.assertTrue(supabase.is_stale)
        self.assertEqual(supabase.state, "stale")
        self.assertEqual(supabase.health_status, "stale")
        self.assertFalse(supabase.live_verified)
        self.assertEqual(supabase.action_owner, "agent_action")
        self.assertFalse(supabase.human_input_needed)

        # Stripe test must be reported as blocked with confirmed cause
        stripe = services["stripe_test"]
        self.assertEqual(stripe.access_status, "blocked")
        self.assertEqual(stripe.diagnosis_type, "confirmed_diagnosis")
        self.assertIn("STRIPE_API_KEY_SECRET", stripe.confirmed_or_unknown_cause)
        self.assertIn("sk_live_", stripe.confirmed_or_unknown_cause)
        self.assertTrue(stripe.human_input_needed, "Missing secret key requires operator configuration")
        self.assertEqual(stripe.question_id, "credential:stripe_test:sk_test_key")
        self.assertIsNotNone(stripe.question_text)
        self.assertIsNotNone(stripe.resolution_guidance)
        # Verify secret keys are NOT asked for in chat or issues
        self.assertNotIn("paste key here", stripe.question_text.lower())
        self.assertIn("do not post secret values", stripe.question_text.lower())

        # 2. Verify Human Inputs list
        self.assertTrue(len(report.human_inputs) >= 1)
        stripe_input = next((h for h in report.human_inputs if h.question_id == "credential:stripe_test:sk_test_key"), None)
        self.assertIsNotNone(stripe_input)
        self.assertEqual(stripe_input.category, "credential")
        self.assertEqual(stripe_input.target, "stripe_test")

        # 3. Verify Registered Requests
        requests = report.requests
        self.assertIn("req-harness-continuous-orchestration", requests)
        harness_req = requests["req-harness-continuous-orchestration"]
        self.assertEqual(harness_req.state, "implementation")
        self.assertEqual(harness_req.action_owner, "agent_action")
        self.assertFalse(harness_req.human_input_needed, "Implementation is agent-owned, not a punt")
        self.assertTrue(len(harness_req.pending_criteria) > 0)

        # Verify completed request
        self.assertIn("req-synthetic-decision-demo-4543", requests)
        done_req = requests["req-synthetic-decision-demo-4543"]
        self.assertEqual(done_req.state, "done")
        self.assertEqual(done_req.action_owner, "agent_action")

        # 4. Verify Decisions
        decisions = report.decisions
        self.assertIn("DEC-4543-01", decisions)
        dec = decisions["DEC-4543-01"]
        self.assertEqual(dec.status, "rejected")
        self.assertEqual(dec.state, "rejected_safety")
        self.assertIn("zaraprptkegxqpvnsubu", dec.confirmed_or_unknown_cause)
        self.assertFalse(dec.human_input_needed, "Rejected decision is agent-owned to not repeat")

        # 5. Verify Host Resources
        hr = report.host_resources
        if hr.telemetry_available:
            self.assertIn(hr.state, ("ok", "elevated", "critical"))
            self.assertIsNotNone(hr.ram_used_percent)
            self.assertIsNotNone(hr.ram_total_gb)
        else:
            self.assertEqual(hr.state, "unknown", "Missing telemetry must strictly be 'unknown', never 'healthy'")

        # 6. Verify Invariants in Boundaries
        self.assertTrue(report.boundaries["unknown_or_stale_never_green"])
        self.assertTrue(report.boundaries["cached_credentials_never_live_healthy"])
        self.assertTrue(report.boundaries["missing_code_or_tests_agent_owned"])
        self.assertEqual(report.boundaries["production_access"], "strictly_prohibited")


class TestFabricatedEvidenceRejection(unittest.TestCase):
    """Test that fabricated healthy or stale evidence is properly detected and rejected."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_diag_synth_")
        self.evidence_dir = os.path.join(self.tmp_dir, "preflight_evidence")
        os.makedirs(self.evidence_dir, exist_ok=True)
        self.ledger_path = os.path.join(self.tmp_dir, "ledger.json")
        self.decisions_path = os.path.join(self.tmp_dir, "decisions.json")

        # Minimal ledger
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "requests": {}}, f)
        # Minimal decisions
        with open(self.decisions_path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "decisions": {}}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_stale_evidence_never_reported_healthy(self):
        """Fabricated evidence marked success but expired MUST report stale, never healthy."""
        expired_ts = "2020-01-01T00:00:00Z"
        ev_data = {
            "service": "dokploy_staging",
            "environment": "staging",
            "target_identity": {"compose_id": "TU7b_dY9l9_nCas6YBNwj"},
            "read_only": True,
            "access_status": "success",
            "timestamp": expired_ts,
            "ttl_seconds": 3600,
            "runtime_revision": "18f6e27dc26ddbdb429347ebae6bc142bb12e96d",
            "bounded_utc_logs": {"count": 10, "query_status": "success"},
        }
        ev_file = os.path.join(self.evidence_dir, "dokploy_staging.json")
        with open(ev_file, "w", encoding="utf-8") as f:
            json.dump(ev_data, f)

        collector = DiagnosticCollector(
            state_dir=self.tmp_dir,
            ledger_path=self.ledger_path,
            decisions_path=self.decisions_path,
            evidence_dir=self.evidence_dir,
        )
        report = collector.run_diagnostics()
        dokploy = report.services["dokploy_staging"]

        self.assertTrue(dokploy.is_stale)
        self.assertNotEqual(dokploy.state, "healthy", "Expired evidence MUST NOT be healthy")
        self.assertEqual(dokploy.state, "stale")
        self.assertEqual(dokploy.health_status, "stale")
        self.assertFalse(dokploy.live_verified)

    def test_access_granted_without_health_is_not_healthy(self):
        """Evidence with access success but failed container logs must be unhealthy/failed."""
        fresh_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        ev_data = {
            "service": "dokploy_staging",
            "environment": "staging",
            "target_identity": {"compose_id": "TU7b_dY9l9_nCas6YBNwj"},
            "read_only": True,
            "access_status": "success",
            "timestamp": fresh_ts,
            "ttl_seconds": 3600,
            "runtime_revision": None,
            "bounded_utc_logs": {
                "count": 0,
                "query_status": "container_not_found",
                "log_query_failed": True,
                "log_query_error": "No such container: backend/frontend",
            },
        }
        ev_file = os.path.join(self.evidence_dir, "dokploy_staging.json")
        with open(ev_file, "w", encoding="utf-8") as f:
            json.dump(ev_data, f)

        collector = DiagnosticCollector(
            state_dir=self.tmp_dir,
            ledger_path=self.ledger_path,
            decisions_path=self.decisions_path,
            evidence_dir=self.evidence_dir,
        )
        report = collector.run_diagnostics()
        dokploy = report.services["dokploy_staging"]

        self.assertEqual(dokploy.access_status, "granted")
        self.assertEqual(dokploy.health_status, "unhealthy")
        self.assertEqual(dokploy.state, "failed")
        self.assertNotEqual(dokploy.state, "healthy")

    def test_production_probe_identity_rejected(self):
        """Probing production compose ID or Supabase ref must be strictly rejected."""
        fresh_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        ev_data = {
            "service": "dokploy_staging",
            "environment": "production",
            "target_identity": {"compose_id": "vpyL-7TDEUREH6Uo_y1sb"},
            "read_only": True,
            "access_status": "success",
            "timestamp": fresh_ts,
            "ttl_seconds": 3600,
        }
        ev_file = os.path.join(self.evidence_dir, "dokploy_staging.json")
        with open(ev_file, "w", encoding="utf-8") as f:
            json.dump(ev_data, f)

        collector = DiagnosticCollector(
            state_dir=self.tmp_dir,
            ledger_path=self.ledger_path,
            decisions_path=self.decisions_path,
            evidence_dir=self.evidence_dir,
        )
        report = collector.run_diagnostics()
        dokploy = report.services["dokploy_staging"]

        self.assertEqual(dokploy.state, "blocked")
        self.assertIn("FATAL: Production", dokploy.confirmed_or_unknown_cause)

    def test_distinguish_confirmed_diagnosis_vs_unknown_cause(self):
        """Failure with no error text must be unknown_cause; failure with blocker must be confirmed."""
        fresh_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Unknown cause
        ev_unknown = {
            "service": "supabase_staging",
            "environment": "staging",
            "target_identity": {"project_ref": "hgzyqmaanndcimnclxtv"},
            "read_only": True,
            "access_status": "failed",
            "timestamp": fresh_ts,
            "ttl_seconds": 3600,
            "blocker_reason": None,
        }
        with open(os.path.join(self.evidence_dir, "supabase_staging.json"), "w", encoding="utf-8") as f:
            json.dump(ev_unknown, f)

        collector = DiagnosticCollector(
            state_dir=self.tmp_dir,
            ledger_path=self.ledger_path,
            decisions_path=self.decisions_path,
            evidence_dir=self.evidence_dir,
        )
        report = collector.run_diagnostics()
        sup = report.services["supabase_staging"]
        self.assertEqual(sup.diagnosis_type, "unknown_cause")

        # Confirmed cause
        ev_confirmed = {
            "service": "supabase_staging",
            "environment": "staging",
            "target_identity": {"project_ref": "hgzyqmaanndcimnclxtv"},
            "read_only": True,
            "access_status": "blocked",
            "timestamp": fresh_ts,
            "ttl_seconds": 3600,
            "blocker_reason": "Database connection pool exhausted: timeout after 10000ms",
        }
        with open(os.path.join(self.evidence_dir, "supabase_staging.json"), "w", encoding="utf-8") as f:
            json.dump(ev_confirmed, f)

        report2 = collector.run_diagnostics()
        sup2 = report2.services["supabase_staging"]
        self.assertEqual(sup2.diagnosis_type, "confirmed_diagnosis")
        self.assertIn("connection pool exhausted", sup2.confirmed_or_unknown_cause)


class TestHumanInputVsAgentAction(unittest.TestCase):
    """Test that human input is ONLY requested for true authorizations/credentials."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_diag_human_")
        self.ledger_path = os.path.join(self.tmp_dir, "ledger.json")
        self.decisions_path = os.path.join(self.tmp_dir, "decisions.json")
        self.evidence_dir = os.path.join(self.tmp_dir, "preflight_evidence")
        os.makedirs(self.evidence_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_awaiting_authorization_triggers_human_input(self):
        """A request in awaiting authorization must produce a deduplicatable question_id."""
        ledger_data = {
            "version": 2,
            "requests": {
                "req-deploy-001": {
                    "id": "req-deploy-001",
                    "state": "awaiting authorization",
                    "task_type": "deployable",
                    "owner": "Tester",
                    "head": "a" * 40,
                    "authorization": {"status": "pending"},
                    "acceptance_criteria": [
                        {"id": "AC-1", "description": "Done", "status": "verified"}
                    ],
                }
            },
        }
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump(ledger_data, f)
        with open(self.decisions_path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "decisions": {}}, f)

        collector = DiagnosticCollector(
            state_dir=self.tmp_dir,
            ledger_path=self.ledger_path,
            decisions_path=self.decisions_path,
            evidence_dir=self.evidence_dir,
        )
        report = collector.run_diagnostics()

        req = report.requests["req-deploy-001"]
        self.assertTrue(req.human_input_needed)
        self.assertEqual(req.action_owner, "human_input")
        self.assertEqual(req.question_id, "authorization:req-deploy-001:merge")
        self.assertIn("Authorize", req.question_text)

        # Check in human_inputs list
        hi = next((h for h in report.human_inputs if h.question_id == "authorization:req-deploy-001:merge"), None)
        self.assertIsNotNone(hi)
        self.assertEqual(hi.category, "authorization")

    def test_failing_or_pending_implementation_is_agent_action(self):
        """A request in implementation with missing criteria is AGENT-OWNED, not a punt."""
        ledger_data = {
            "version": 2,
            "requests": {
                "req-impl-001": {
                    "id": "req-impl-001",
                    "state": "implementation",
                    "task_type": "deployable",
                    "owner": "Builder",
                    "head": "b" * 40,
                    "acceptance_criteria": [
                        {"id": "AC-1", "description": "Write code", "status": "pending"},
                        {"id": "AC-2", "description": "Pass tests", "status": "pending"},
                    ],
                }
            },
        }
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump(ledger_data, f)
        with open(self.decisions_path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "decisions": {}}, f)

        collector = DiagnosticCollector(
            state_dir=self.tmp_dir,
            ledger_path=self.ledger_path,
            decisions_path=self.decisions_path,
            evidence_dir=self.evidence_dir,
        )
        report = collector.run_diagnostics()

        req = report.requests["req-impl-001"]
        self.assertFalse(req.human_input_needed)
        self.assertEqual(req.action_owner, "agent_action")
        self.assertIsNone(req.question_id)

        # Must appear in agent_actions, NOT in human_inputs
        hi = next((h for h in report.human_inputs if "req-impl-001" in h.target), None)
        self.assertIsNone(hi, "Implementation must never be placed in human_inputs")

        act = next((a for a in report.agent_actions if a.target == "req-impl-001"), None)
        self.assertIsNotNone(act)
        self.assertEqual(act.category, "implementation")


class TestCLIExecution(unittest.TestCase):
    """Test CLI commands work as documented."""

    def test_diagnostics_cli_summary_and_json(self):
        state_dir = "C:/Users/wkiri/.veyyon/workflows"
        if not os.path.exists(state_dir):
            self.skipTest("Installed state dir missing")

        diag_script = os.path.join(SCRIPT_DIR, "diagnostics.py")

        # 1. Summary mode
        res_sum = subprocess.run(
            [sys.executable, diag_script, "--state-dir", state_dir, "--summary"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_sum.returncode, 0)
        self.assertIn("PORTABLE WORKFLOW AGGREGATE SYSTEM & REQUEST DIAGNOSTICS", res_sum.stdout)
        self.assertIn("dokploy_staging", res_sum.stdout)
        self.assertIn("stripe_test", res_sum.stdout)

        # 2. JSON mode
        res_json = subprocess.run(
            [sys.executable, diag_script, "--state-dir", state_dir, "--json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_json.returncode, 0)
        data = json.loads(res_json.stdout)
        self.assertEqual(data["schema_version"], "1.0")
        self.assertIn("services", data)
        self.assertIn("requests", data)
        self.assertIn("human_inputs", data)
        self.assertIn("agent_actions", data)

    def test_coordinator_diagnostics_flag(self):
        state_dir = "C:/Users/wkiri/.veyyon/workflows"
        if not os.path.exists(state_dir):
            self.skipTest("Installed state dir missing")

        coord_script = os.path.join(SCRIPT_DIR, "coordinator.py")
        res = subprocess.run(
            [sys.executable, coord_script, "--state-dir", state_dir, "--diagnostics", "--summary"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("PORTABLE WORKFLOW AGGREGATE SYSTEM & REQUEST DIAGNOSTICS", res.stdout)

    def test_continuation_driver_diagnostics_flag(self):
        state_dir = "C:/Users/wkiri/.veyyon/workflows"
        if not os.path.exists(state_dir):
            self.skipTest("Installed state dir missing")

        driver_script = os.path.join(SCRIPT_DIR, "continuation_driver.py")
        res = subprocess.run(
            [sys.executable, driver_script, "--state-dir", state_dir, "--diagnostics"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("PORTABLE WORKFLOW AGGREGATE SYSTEM & REQUEST DIAGNOSTICS", res.stdout)


if __name__ == "__main__":
    unittest.main()
