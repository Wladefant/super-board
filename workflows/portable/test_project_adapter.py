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
                                            "project": {"id": "PVT_1", "number": 1},
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
                                            "project": {"id": "PVT_1", "number": 1},
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

    def test_11_done_closure_gate_enforces_live_verified_closure(self):
        """USER INVIOLABLE SAFETY GATE: Transitions to 'Done' strictly require verified closure and valid head SHA."""
        cfg = create_polysimulator_config()
        updater = SuperboardProjectUpdater(cfg)

        # 1. Unverified closure rejected
        outcome_unverified = updater.update_lifecycle("req-4543", "Done", closure_verified=False)
        self.assertFalse(outcome_unverified.ok)
        self.assertIn("verified live closure", outcome_unverified.blocked_reason or "")
        self.assertEqual(outcome_unverified.github_writes, 0)

        # 2. Verified closure but missing head SHA rejected
        outcome_no_head = updater.update_lifecycle("req-4543", "Done", head_sha=None, closure_verified=True)
        self.assertFalse(outcome_no_head.ok)
        self.assertIn("authoritative head_sha", outcome_no_head.blocked_reason or "")
        self.assertEqual(outcome_no_head.github_writes, 0)

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
                                            "project": {"id": "PVT_1", "number": 1},
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

        # Test call on config object
        outcome = cfg.update_lifecycle("req-4543", "QA", dry_run=True)
        self.assertIsInstance(outcome, SuperboardLifecycleOutcome)
        self.assertTrue(hasattr(outcome, "ok"))
        self.assertTrue(hasattr(outcome, "blocked_reason"))
        self.assertTrue(hasattr(outcome, "board_url"))
        self.assertTrue(outcome.ok)
        self.assertTrue(outcome.dry_run)

    def test_15_baseline_adapter_validations_unaffected(self):
        """Ensure all baseline project adapter safety functions remain intact."""
        cfg = create_polysimulator_config()
        self.assertEqual(cfg.repo, "Bavariance/polysimulator")
        self.assertEqual(cfg.project_number, 1)

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


def main():
    print("=" * 70)
    print("RUNNING PORTABLE PROJECT ADAPTER & SUPERBOARD UPDATER TEST SUITE")
    print("=" * 70)
    unittest.main(verbosity=2)


if __name__ == "__main__":
    main()
