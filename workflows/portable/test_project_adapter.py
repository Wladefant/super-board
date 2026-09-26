#!/usr/bin/env python3
"""
workflows/portable/test_project_adapter.py — Targeted Unit Tests for Portable Project Adapter
and Native Superboard Project V2 Lifecycle Updater.

Covers:
  1. Canonical 7-state Superboard lifecycle mapping & retired 'Skipped' status rejection
  2. Fail-closed wrong-target and unconfigured environment guards
  3. Issue number resolution from diverse request_id formats
  4. Dynamic schema discovery without generic global fixed IDs
  5. Guaranteed 0-write dry-run mode
  6. Idempotence (no redundant mutations when already at desired status)
  7. Inviolable Done-closure gate (fail closed on unverified Done or missing head SHA)
  8. Live mutation and readback verification (detecting readback mismatches)
  9. Truthful GraphQL error propagation
 10. Duck-typed frozen interface contract on ProjectConfig (.ok, .blocked_reason, .board_url)
 11. Preserved baseline adapter validation contracts
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Dict

# Ensure sibling portable workflow modules are importable
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from project_adapter import (
    CANONICAL_LIFECYCLE_STATUSES,
    ProjectConfig,
    SuperboardLifecycleOutcome,
    SuperboardProjectUpdater,
    canonicalize_lifecycle_status,
    check_text_for_forbidden_patterns,
    create_generic_config,
    create_polysimulator_config,
    fetch_github_sub_issues,
    get_current_project_config,
    update_project_lifecycle,
    validate_dokploy_compose_id,
    validate_supabase_project_ref,
)


class TestProjectAdapterLifecycle(unittest.TestCase):
    """Test suite for Superboard Project V2 lifecycle updater and project adapter."""

    def test_01_canonical_status_spelling_and_case_folding(self):
        """Verify canonical 7-state Superboard lifecycle names."""
        for expected in CANONICAL_LIFECYCLE_STATUSES:
            self.assertEqual(canonicalize_lifecycle_status(expected), expected)
            self.assertEqual(canonicalize_lifecycle_status(expected.lower()), expected)
            self.assertEqual(canonicalize_lifecycle_status(expected.upper()), expected)
            self.assertEqual(canonicalize_lifecycle_status(f"  {expected}  "), expected)

    def test_02_status_alias_mapping(self):
        """Verify common ledger/coordinator states map cleanly to canonical statuses."""
        self.assertEqual(canonicalize_lifecycle_status("pending"), "Ready")
        self.assertEqual(canonicalize_lifecycle_status("ready"), "Ready")
        self.assertEqual(canonicalize_lifecycle_status("building"), "Building")
        self.assertEqual(canonicalize_lifecycle_status("build"), "Building")
        self.assertEqual(canonicalize_lifecycle_status("implementation"), "Building")
        self.assertEqual(canonicalize_lifecycle_status("qa"), "QA")
        self.assertEqual(canonicalize_lifecycle_status("review"), "Review")
        self.assertEqual(canonicalize_lifecycle_status("awaiting authorization"), "Review")
        self.assertEqual(canonicalize_lifecycle_status("awaiting_authorization"), "Review")
        self.assertEqual(canonicalize_lifecycle_status("blocked"), "Blocked")
        self.assertEqual(canonicalize_lifecycle_status("done"), "Done")
        self.assertEqual(canonicalize_lifecycle_status("completed"), "Done")

    def test_03_retired_skipped_status_strictly_rejected(self):
        """Verify 'Skipped' is rejected outright as a retired status."""
        with self.assertRaises(ValueError) as ctx:
            canonicalize_lifecycle_status("Skipped")
        self.assertIn("retired status", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            canonicalize_lifecycle_status("skipped")
        self.assertIn("retired status", str(ctx.exception))

    def test_04_unknown_status_strictly_rejected(self):
        """Verify unmapped random strings raise ValueError."""
        with self.assertRaises(ValueError) as ctx:
            canonicalize_lifecycle_status("RandomInvalidStatus")
        self.assertIn("Unknown lifecycle status", str(ctx.exception))

        with self.assertRaises(ValueError):
            canonicalize_lifecycle_status("")

    def test_05_wrong_target_guard_generic_fails_closed(self):
        """Verify generic or unconfigured repository fails closed without mutations."""
        cfg = create_generic_config("generic/unconfigured")
        updater = SuperboardProjectUpdater(cfg)
        outcome = updater.update_lifecycle("req-4543", "QA")
        self.assertFalse(outcome.ok)
        self.assertIn("unconfigured or generic", outcome.blocked_reason or "")
        self.assertEqual(outcome.github_writes, 0)

    def test_06_wrong_target_guard_malformed_repo_fails_closed(self):
        """Verify repository without owner/repo slash fails closed."""
        cfg = ProjectConfig(repo="invalid-repo-without-slash", project_number=1)
        updater = SuperboardProjectUpdater(cfg)
        outcome = updater.update_lifecycle("req-4543", "QA")
        self.assertFalse(outcome.ok)
        self.assertIn("Invalid repository identifier", outcome.blocked_reason or "")
        self.assertEqual(outcome.github_writes, 0)

    def test_07_issue_number_resolution(self):
        """Verify flexible and robust issue number parsing from request_id."""
        cfg = create_polysimulator_config()
        updater = SuperboardProjectUpdater(cfg)

        self.assertEqual(updater._resolve_issue_number("req-4543"), 4543)
        self.assertEqual(updater._resolve_issue_number("issue-1234"), 1234)
        self.assertEqual(updater._resolve_issue_number("#999"), 999)
        self.assertEqual(updater._resolve_issue_number("4543"), 4543)
        self.assertEqual(updater._resolve_issue_number("req_789"), 789)
        self.assertEqual(updater._resolve_issue_number("anything", issue_number=4543), 4543)
        self.assertIsNone(updater._resolve_issue_number("no-number-here"))

    def test_08_dynamic_discovery_no_fixed_ids(self):
        """Verify updater discovers custom option IDs dynamically rather than assuming fixed IDs."""
        # Simulated board with non-standard, custom option IDs (like Dubai Holding board #9)
        custom_schema_data = {
            "data": {
                "repositoryOwner": {
                    "projectV2": {
                        "id": "PVT_custom_999",
                        "title": "Custom Board",
                        "fields": {
                            "nodes": [
                                {
                                    "id": "FIELD_STATUS_CUSTOM",
                                    "name": "Status",
                                    "options": [
                                        {"id": "opt_backlog_custom", "name": "Backlog"},
                                        {"id": "opt_ready_custom", "name": "Ready"},
                                        {"id": "opt_building_custom", "name": "Building"},
                                        {"id": "opt_qa_custom_xyz", "name": "QA"},
                                        {"id": "opt_review_custom", "name": "Review"},
                                        {"id": "opt_done_custom", "name": "Done"},
                                        {"id": "opt_blocked_custom", "name": "Blocked"},
                                    ],
                                }
                            ]
                        },
                    }
                }
            }
        }

        captured_mutations = []

        def mock_runner(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
            if "projectV2" in query:
                return custom_schema_data
            elif "projectItems" in query:
                # Return issue currently at Building
                return {
                    "data": {
                        "repository": {
                            "issue": {
                                "id": "ISSUE_123",
                                "projectItems": {
                                    "nodes": [
                                        {
                                            "id": "ITEM_CARD_123",
                                            "project": {"id": "PVT_custom_999", "number": 1, "title": "Custom Board"},
                                            "fieldValueByName": {"name": "Building", "optionId": "opt_building_custom"},
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            elif "updateProjectV2ItemFieldValue" in query:
                captured_mutations.append(variables)
                # Next readback returns QA
                return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables["itemId"]}}}}
            return {}

        cfg = create_polysimulator_config()
        updater = SuperboardProjectUpdater(cfg, graphql_runner=mock_runner)
        schema = updater.get_board_schema("Bavariance", 1)

        self.assertEqual(schema["status_field_id"], "FIELD_STATUS_CUSTOM")
        # Ensure it resolved the dynamic custom QA option ID
        self.assertEqual(schema["options_map"]["qa"], "opt_qa_custom_xyz")

    def test_09_dry_run_mode_guaranteed_zero_writes(self):
        """Verify dry-run mode resolves schema and card but never writes to GitHub."""
        mutations = []

        def mock_runner(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
            if "updateProjectV2ItemFieldValue" in query:
                mutations.append(variables)
                return {}
            if "projectV2" in query:
                return {
                    "data": {
                        "repositoryOwner": {
                            "projectV2": {
                                "id": "PVT_1",
                                "title": "Test",
                                "fields": {
                                    "nodes": [
                                        {
                                            "id": "STATUS_FIELD_1",
                                            "name": "Status",
                                            "options": [{"id": "OPT_QA_1", "name": "QA"}],
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            if "projectItems" in query:
                return {
                    "data": {
                        "repository": {
                            "issue": {
                                "id": "ISS_1",
                                "projectItems": {
                                    "nodes": [
                                        {
                                            "id": "ITEM_1",
                                            "project": {"id": "PVT_1", "number": 5, "owner": {"login": "Wladefant"}},
                                            "fieldValueByName": {"name": "Building", "optionId": "OPT_BUILD_1"},
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            return {}

        cfg = create_polysimulator_config()
        updater = SuperboardProjectUpdater(cfg, graphql_runner=mock_runner)
        outcome = updater.update_lifecycle("req-4543", "QA", dry_run=True)

        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.dry_run)
        self.assertEqual(outcome.github_writes, 0)
        self.assertEqual(len(mutations), 0)
        self.assertEqual(outcome.previous_status, "Building")
        self.assertEqual(outcome.new_status, "QA")

    def test_10_idempotence_skips_write_when_status_matches(self):
        """Verify idempotence: if card is already in target status, 0 writes performed."""
        mutations = []

        def mock_runner(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
            if "updateProjectV2ItemFieldValue" in query:
                mutations.append(variables)
                return {}
            if "projectV2" in query:
                return {
                    "data": {
                        "repositoryOwner": {
                            "projectV2": {
                                "id": "PVT_1",
                                "title": "Test",
                                "fields": {
                                    "nodes": [
                                        {
                                            "id": "STATUS_FIELD_1",
                                            "name": "Status",
                                            "options": [{"id": "OPT_QA_1", "name": "QA"}],
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            if "projectItems" in query:
                return {
                    "data": {
                        "repository": {
                            "issue": {
                                "id": "ISS_1",
                                "projectItems": {
                                    "nodes": [
                                        {
                                            "id": "ITEM_1",
                                            "project": {"id": "PVT_1", "number": 5, "owner": {"login": "Wladefant"}},
                                            "fieldValueByName": {"name": "QA", "optionId": "OPT_QA_1"},
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            return {}

        cfg = create_polysimulator_config()
        updater = SuperboardProjectUpdater(cfg, graphql_runner=mock_runner)
        outcome = updater.update_lifecycle("req-4543", "QA", dry_run=False)

        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.dry_run)
        self.assertEqual(outcome.github_writes, 0)
        self.assertEqual(len(mutations), 0)
        self.assertIn("already in status", outcome.details.get("message", ""))

    def test_11_done_closure_gate_consumes_verified_ledger_record(self):
        """Done requires the ledger's verified closure, not a caller-provided boolean."""
        cfg = create_polysimulator_config()
        updater = SuperboardProjectUpdater(cfg)
        head = "a" * 40

        outcome_missing = updater.update_lifecycle("req-4543", "Done", head_sha=head)
        self.assertFalse(outcome_missing.ok)
        self.assertIn("actual ledger-verified closure record", outcome_missing.blocked_reason or "")
        self.assertEqual(outcome_missing.github_writes, 0)

        asserted_but_not_done = {
            "state": "review",
            "head": head,
            "acceptance_criteria": [{
                "id": "AC-1",
                "status": "verified",
                "evidence": "observed pass",
                "verified_head": head,
            }],
            "github": {
                "proof_verified": True,
                "proof_url": "https://github.com/Bavariance/polysimulator/issues/4543",
            },
        }
        outcome_not_closed = updater.update_lifecycle(
            "req-4543",
            "Done",
            head_sha=head,
            ledger_record=asserted_but_not_done,
        )
        self.assertFalse(outcome_not_closed.ok)
        self.assertIn("state=done", outcome_not_closed.blocked_reason or "")

    def test_12_readback_verification_detects_mismatch(self):
        """Verify readback verification: if GitHub readback status does not match desired status, fails closed."""
        def mock_runner(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
            if "projectV2" in query:
                return {
                    "data": {
                        "repositoryOwner": {
                            "projectV2": {
                                "id": "PVT_1",
                                "title": "Test",
                                "fields": {
                                    "nodes": [
                                        {
                                            "id": "STATUS_FIELD_1",
                                            "name": "Status",
                                            "options": [{"id": "OPT_QA_1", "name": "QA"}],
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            if "projectItems" in query:
                # Always returns Building, simulating mutation failing silently or racing
                return {
                    "data": {
                        "repository": {
                            "issue": {
                                "id": "ISS_1",
                                "projectItems": {
                                    "nodes": [
                                        {
                                            "id": "ITEM_1",
                                            "project": {"id": "PVT_1", "number": 5, "owner": {"login": "Wladefant"}},
                                            "fieldValueByName": {"name": "Building", "optionId": "OPT_BUILD_1"},
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            if "updateProjectV2ItemFieldValue" in query:
                return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables["itemId"]}}}}
            return {}

        cfg = create_polysimulator_config()
        updater = SuperboardProjectUpdater(cfg, graphql_runner=mock_runner)
        outcome = updater.update_lifecycle("req-4543", "QA", dry_run=False)

        self.assertFalse(outcome.ok)
        self.assertIn("Readback mismatch", outcome.blocked_reason or "")

    def test_13_truthful_graphql_error_propagation(self):
        """Verify GraphQL and subprocess errors are propagated truthfully in blocked_reason."""
        def failing_runner(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
            raise RuntimeError("Could not resolve to a ProjectV2 with number 99")

        cfg = ProjectConfig(repo="Bavariance/polysimulator", project_number=99)
        updater = SuperboardProjectUpdater(cfg, graphql_runner=failing_runner)
        outcome = updater.update_lifecycle("req-4543", "QA")

        self.assertFalse(outcome.ok)
        self.assertIn("Could not resolve to a ProjectV2 with number 99", outcome.blocked_reason or "")

    def test_14_duck_typed_interface_on_project_config(self):
        """Verify ProjectConfig provides duck-typed update_lifecycle matching frozen contract."""
        cfg = create_polysimulator_config()
        self.assertTrue(hasattr(cfg, "update_lifecycle"))

        def mock_runner(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
            if "projectV2" in query:
                return {
                    "data": {
                        "repositoryOwner": {
                            "projectV2": {
                                "id": "PVT_1",
                                "title": "Test",
                                "fields": {
                                    "nodes": [
                                        {
                                            "id": "STATUS_FIELD_1",
                                            "name": "Status",
                                            "options": [{"id": "OPT_QA_1", "name": "QA"}],
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            if "projectItems" in query:
                return {
                    "data": {
                        "repository": {
                            "issue": {
                                "id": "ISS_1",
                                "projectItems": {
                                    "nodes": [
                                        {
                                            "id": "ITEM_1",
                                            "project": {"id": "PVT_1", "number": 5, "owner": {"login": "Wladefant"}},
                                            "fieldValueByName": {"name": "Building", "optionId": "OPT_BUILD_1"},
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            return {}

        # Test call on config object
        outcome = cfg.update_lifecycle("req-4543", "QA", dry_run=True, graphql_runner=mock_runner)
        self.assertIsInstance(outcome, SuperboardLifecycleOutcome)
        self.assertTrue(hasattr(outcome, "ok"))
        self.assertTrue(hasattr(outcome, "blocked_reason"))
        self.assertTrue(hasattr(outcome, "board_url"))
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.dry_run)
        self.assertEqual(outcome.github_writes, 0)
    def test_15_baseline_adapter_validations_unaffected(self):
        """Ensure all baseline project adapter safety functions remain intact."""
        cfg = create_polysimulator_config()
        self.assertEqual(cfg.repo, "Bavariance/polysimulator")
        self.assertEqual(cfg.project_number, 5)
        self.assertEqual(cfg.metadata["project_owner"], "Wladefant")

        # Safety pattern checks
        is_bad, match = check_text_for_forbidden_patterns("zaraprptkegxqpvnsubu", cfg)
        self.assertTrue(is_bad)
        self.assertIsNotNone(match)

        # Dokploy compose ID validation
        valid_comp, status_comp, _ = validate_dokploy_compose_id("TU7b_dY9l9_nCas6YBNwj", cfg)
        self.assertTrue(valid_comp)
        self.assertEqual(status_comp, "valid")

        # Supabase ref validation
        valid_sb, status_sb, _ = validate_supabase_project_ref("hgzyqmaanndcimnclxtv", cfg)
        self.assertTrue(valid_sb)
        self.assertEqual(status_sb, "valid")

    def test_16_parent_close_guard_refuses_when_sub_issues_open(self):
        """Verify that transitioning parent issue to Done refuses if open sub-issues exist."""
        cfg = create_polysimulator_config()
        head = "a" * 40
        verified_closure = {
            "state": "done",
            "head": head,
            "acceptance_criteria": [{
                "id": "AC-1",
                "status": "verified",
                "evidence": "observed pass",
                "verified_head": head,
            }],
            "github": {
                "proof_verified": True,
                "proof_url": "https://github.com/Bavariance/polysimulator/issues/4543",
            },
        }

        # 1. Refusal when sub-issues are open
        open_subs = [{"number": 4544, "title": "Child issue", "state": "open"}]
        mock_checker = lambda repo, issue_num: open_subs

        updater = SuperboardProjectUpdater(cfg, sub_issues_checker=mock_checker)
        outcome_refused = updater.update_lifecycle(
            "req-4543",
            "Done",
            head_sha=head,
            ledger_record=verified_closure,
        )
        self.assertFalse(outcome_refused.ok)
        self.assertIn("open sub-issue(s)", outcome_refused.blocked_reason or "")
        self.assertIn("#4544", outcome_refused.blocked_reason or "")
        self.assertEqual(outcome_refused.github_writes, 0)

        # 2. Allowed when all sub-issues are closed
        closed_subs = [{"number": 4544, "title": "Child issue", "state": "closed"}]
        mock_closed_checker = lambda repo, issue_num: closed_subs

        current_status = ["Backlog"]
        def mock_graphql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
            if "updateProjectV2ItemFieldValue" in query:
                current_status[0] = "Done"
                return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "ITEM_1"}}}}
            if "projectV2" in query:
                return {
                    "data": {
                        "repositoryOwner": {
                            "projectV2": {
                                "id": "PVT_1",
                                "title": "Test",
                                "fields": {
                                    "nodes": [
                                        {
                                            "id": "STATUS_FIELD_1",
                                            "name": "Status",
                                            "options": [
                                                {"id": "OPT_DONE_1", "name": "Done"},
                                                {"id": "OPT_TODO_1", "name": "Backlog"},
                                            ],
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            if "projectItems" in query or "issue" in query:
                return {
                    "data": {
                        "repository": {
                            "issue": {
                                "id": "ISSUE_1",
                                "projectItems": {
                                    "nodes": [
                                        {
                                            "id": "ITEM_1",
                                            "project": {"id": "PVT_1", "number": 5, "owner": {"login": "Wladefant"}},
                                            "fieldValueByName": {
                                                "name": current_status[0],
                                                "optionId": "OPT_DONE_1" if current_status[0] == "Done" else "OPT_TODO_1",
                                            },
                                        }
                                    ]
                                },
                            }
                        }
                    }
                }
            return {}

        updater_ok = SuperboardProjectUpdater(cfg, graphql_runner=mock_graphql, sub_issues_checker=mock_closed_checker)
        outcome_ok = updater_ok.update_lifecycle(
            "req-4543",
            "Done",
            head_sha=head,
            ledger_record=verified_closure,
        )
        self.assertTrue(outcome_ok.ok)
        self.assertEqual(outcome_ok.github_writes, 1)

    def test_17_fetch_github_sub_issues_contracts_and_timeout(self):
        """Verify fetch_github_sub_issues handles timeout, errors, and input validation."""
        from unittest.mock import patch, MagicMock
        import subprocess

        # 1. Invalid input raises ValueError
        with self.assertRaises(ValueError):
            fetch_github_sub_issues("", 4543)
        with self.assertRaises(ValueError):
            fetch_github_sub_issues("owner/repo", 0)
        with self.assertRaises(ValueError):
            fetch_github_sub_issues("owner/repo", -5)

        # 2. Timeout raises RuntimeError and passes timeout parameter
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="gh", timeout=5)):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_github_sub_issues("owner/repo", 4543, timeout_sec=5)
            self.assertIn("timed out after 5s", str(ctx.exception))

        # 3. Non-zero exit code raises RuntimeError
        mock_proc_fail = MagicMock()
        mock_proc_fail.returncode = 1
        mock_proc_fail.stderr = "gh: command failed (HTTP 403)"
        mock_proc_fail.stdout = ""
        with patch("subprocess.run", return_value=mock_proc_fail):
            with self.assertRaises(RuntimeError) as ctx:
                fetch_github_sub_issues("owner/repo", 4543)
            self.assertIn("failed with exit 1", str(ctx.exception))
            self.assertIn("403", str(ctx.exception))

        # 4. Success parsing NDJSON
        mock_proc_ok = MagicMock()
        mock_proc_ok.returncode = 0
        mock_proc_ok.stdout = '{"number": 10, "title": "Sub 10", "state": "open"}\n{"number": 20, "title": "Sub 20", "state": "closed"}'
        with patch("subprocess.run", return_value=mock_proc_ok):
            subs = fetch_github_sub_issues("owner/repo", 4543)
            self.assertEqual(len(subs), 2)
            self.assertEqual(subs[0]["number"], 10)
            self.assertEqual(subs[0]["state"], "open")
            self.assertEqual(subs[1]["number"], 20)
            self.assertEqual(subs[1]["state"], "closed")

    def test_18_parent_close_guard_fails_closed_on_lookup_error(self):
        """Verify parent close guard fails closed when sub-issues lookup fails."""
        cfg = create_polysimulator_config()
        head = "a" * 40
        verified_closure = {
            "state": "done",
            "head": head,
            "acceptance_criteria": [{
                "id": "AC-1",
                "status": "verified",
                "evidence": "observed pass",
                "verified_head": head,
            }],
            "github": {
                "proof_verified": True,
                "proof_url": "https://github.com/Bavariance/polysimulator/issues/4543",
            },
        }

        def failing_checker(repo, issue_num):
            raise RuntimeError("API rate limit exceeded")

        updater = SuperboardProjectUpdater(cfg, sub_issues_checker=failing_checker)
        outcome = updater.update_lifecycle(
            "req-4543",
            "Done",
            head_sha=head,
            ledger_record=verified_closure,
        )
        self.assertFalse(outcome.ok)
        self.assertIn("Inviolable parent-close guard fails closed", outcome.blocked_reason or "")
        self.assertIn("Failed to verify sub-issues", outcome.blocked_reason or "")
        self.assertEqual(outcome.github_writes, 0)
def main():
    print("=" * 70)
    print("RUNNING PORTABLE PROJECT ADAPTER & SUPERBOARD UPDATER TEST SUITE")
    print("=" * 70)
    unittest.main(verbosity=2)


if __name__ == "__main__":
    main()
