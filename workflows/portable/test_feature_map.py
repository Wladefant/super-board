#!/usr/bin/env python3
"""
test_feature_map.py - Unit and regression tests for Feature Map Generator & Validator.

Part of portable workflow core in Wladefant/super-board.
References:
  - Superboard Issue #227 (High-Trust Agent Architecture & Verification Skills)
  - Superboard Issue #244 (Feature map for agent navigation)

Tests:
  1. Loading and parsing valid JSON feature map.
  2. Live repository validation: all 5 core workflows and 23 mapped files exist.
  3. Failure detection when mapped entry_file does not exist.
  4. Failure detection when mapped test file does not exist.
  5. Failure detection when mapped doc file does not exist.
  6. Failure detection on empty features dictionary.
  7. Failure detection on empty entry_files list.
  8. Failure detection on empty tests list.
  9. Failure detection on invalid risk level.
  10. Failure detection on malformed feature identifier.
  11. Failure detection on missing required string attributes.
  12. Feature querying by feature ID.
  13. Feature querying by referenced file path.
  14. Feature querying by descriptive keyword.
  15. Feature querying by component symbol.
  16. Exact reverse file lookup via find_by_file().
  17. CLI validate command success exit code 0.
  18. CLI validate command failure exit code 1 when path missing.
  19. CLI query and show subcommands output formatting.
  20. CLI list with --json serialization.
  21. Entry generator helper validation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure workflows/portable is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import feature_map
from feature_map import (
    FeatureEntry,
    FeatureMap,
    ValidationResult,
    generate_feature_entry,
    load_feature_map,
    validate_feature_map,
)


class TestFeatureMap(unittest.TestCase):
    """Unit tests for feature map models, loading, validation, and querying."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(SCRIPT_DIR).parent.parent.resolve()
        cls.live_map_path = cls.repo_root / "FEATURE_MAP.json"

    def test_01_load_valid_feature_map(self):
        """Feature map loads cleanly and parses all 5 seeded workflow features."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        self.assertIsInstance(fmap, FeatureMap)
        self.assertEqual(fmap.version, "1.0.0")
        self.assertEqual(fmap.repository, "Wladefant/super-board")
        self.assertEqual(len(fmap.features), 5)
        self.assertIn("routing", fmap.features)
        self.assertIn("gate", fmap.features)
        self.assertIn("ledger", fmap.features)
        self.assertIn("gardener", fmap.features)
        self.assertIn("build_slot", fmap.features)

    def test_02_validate_live_map_success(self):
        """The live repository FEATURE_MAP.json passes strict validation with 0 errors."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        result = validate_feature_map(fmap, self.repo_root)
        self.assertTrue(result.valid, f"Validation errors: {result.errors}")
        self.assertEqual(len(result.errors), 0)
        self.assertEqual(result.feature_count, 5)
        self.assertEqual(result.file_count, 23)

    def test_03_validate_fails_when_entry_file_missing(self):
        """Validator MUST fail when a mapped entry_file does not exist on disk."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        # Introduce a ghost entry file
        fmap.features["routing"].entry_files.append("workflows/portable/non_existent_entry.py")

        result = validate_feature_map(fmap, self.repo_root)
        self.assertFalse(result.valid)
        self.assertTrue(any("non_existent_entry.py" in err for err in result.errors))
        self.assertTrue(any("entry_file" in err for err in result.errors))

    def test_04_validate_fails_when_test_file_missing(self):
        """Validator MUST fail when a mapped test file does not exist on disk."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        # Introduce a ghost test file
        fmap.features["gate"].tests.append("workflows/portable/test_ghost_gate.py")

        result = validate_feature_map(fmap, self.repo_root)
        self.assertFalse(result.valid)
        self.assertTrue(any("test_ghost_gate.py" in err for err in result.errors))
        self.assertTrue(any("test file" in err for err in result.errors))

    def test_05_validate_fails_when_doc_file_missing(self):
        """Validator MUST fail when a mapped documentation file does not exist on disk."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        fmap.features["routing"].docs.append("docs/ghost_doc.md")

        result = validate_feature_map(fmap, self.repo_root)
        self.assertFalse(result.valid)
        self.assertTrue(any("ghost_doc.md" in err for err in result.errors))
        self.assertTrue(any("doc file" in err for err in result.errors))

    def test_06_validate_fails_on_empty_features(self):
        """Validator fails when features dictionary is empty."""
        fmap = FeatureMap(version="1.0.0", features={})
        result = validate_feature_map(fmap, self.repo_root)
        self.assertFalse(result.valid)
        self.assertTrue(any("at least one feature" in err for err in result.errors))

    def test_07_validate_fails_on_empty_entry_files(self):
        """Validator fails when entry_files list is empty."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        fmap.features["gardener"].entry_files = []

        result = validate_feature_map(fmap, self.repo_root)
        self.assertFalse(result.valid)
        self.assertTrue(any("entry_files' must contain at least one file" in err for err in result.errors))

    def test_08_validate_fails_on_empty_tests(self):
        """Validator fails when tests list is empty."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        fmap.features["gardener"].tests = []

        result = validate_feature_map(fmap, self.repo_root)
        self.assertFalse(result.valid)
        self.assertTrue(any("tests' must contain at least one test file" in err for err in result.errors))

    def test_09_validate_fails_on_invalid_risk(self):
        """Validator fails when risk level is not one of low, medium, high."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        fmap.features["gardener"].risk = "critical_extreme"

        result = validate_feature_map(fmap, self.repo_root)
        self.assertFalse(result.valid)
        self.assertTrue(any("invalid risk level" in err for err in result.errors))

    def test_10_validate_fails_on_invalid_feature_id(self):
        """Validator fails when feature ID contains spaces or invalid characters."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        fmap.features["invalid feature ID with spaces!"] = fmap.features.pop("gardener")

        result = validate_feature_map(fmap, self.repo_root)
        self.assertFalse(result.valid)
        self.assertTrue(any("identifier must contain only alphanumeric" in err for err in result.errors))

    def test_11_validate_fails_on_missing_required_string_fields(self):
        """Validator fails when name, description, or owning_issue are empty."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        fmap.features["gardener"].name = "   "
        fmap.features["gardener"].description = ""
        fmap.features["gardener"].owning_issue = ""

        result = validate_feature_map(fmap, self.repo_root)
        self.assertFalse(result.valid)
        self.assertTrue(any("'name' is required" in err for err in result.errors))
        self.assertTrue(any("'description' is required" in err for err in result.errors))
        self.assertTrue(any("'owning_issue' is required" in err for err in result.errors))

    def test_12_query_by_feature_id(self):
        """Querying by feature key returns the targeted feature."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        matches = fmap.query("gate")
        self.assertGreaterEqual(len(matches), 1)
        match_ids = [fid for fid, _ in matches]
        self.assertIn("gate", match_ids)

    def test_13_query_by_file_path(self):
        """Querying by partial file path resolves the owning feature."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        matches = fmap.query("build_slot.py")
        match_ids = [fid for fid, _ in matches]
        self.assertEqual(match_ids, ["build_slot"])

    def test_14_query_by_keyword(self):
        """Querying by domain keyword resolves matching features."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        matches = fmap.query("pacing")
        match_ids = [fid for fid, _ in matches]
        self.assertIn("routing", match_ids)

    def test_15_query_by_component(self):
        """Querying by component class name resolves the owning feature."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        matches = fmap.query("RamGuard")
        match_ids = [fid for fid, _ in matches]
        self.assertEqual(match_ids, ["build_slot"])

    def test_16_find_by_file(self):
        """find_by_file performs exact reverse lookup."""
        fmap = load_feature_map(path=self.live_map_path, repo_root=self.repo_root)
        hits = fmap.find_by_file("workflows/portable/ledger.py")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], "ledger")

        # Windows-style backslashes also match
        hits_win = fmap.find_by_file("workflows\\portable\\ledger.py")
        self.assertEqual(len(hits_win), 1)
        self.assertEqual(hits_win[0][0], "ledger")

    def test_17_cli_validate_success(self):
        """CLI validate command exits 0 on valid repository map."""
        cmd = [sys.executable, str(Path(SCRIPT_DIR) / "feature_map.py"), "validate", "--repo-root", str(self.repo_root)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"CLI validate failed: {proc.stderr}")
        self.assertIn("[PASS] Feature map valid", proc.stdout)

    def test_18_cli_validate_failure_on_missing_path(self):
        """CLI validate command exits 1 and reports missing file when path does not exist."""
        with tempfile.TemporaryDirectory(prefix="test-fmap-cli-") as tmp:
            tmp_root = Path(tmp)
            bad_map = {
                "version": "1.0.0",
                "features": {
                    "broken": {
                        "name": "Broken Feature",
                        "description": "Points to missing file",
                        "entry_files": ["missing_file_xyz.py"],
                        "tests": ["test_missing.py"],
                        "owning_issue": "https://github.com/Wladefant/super-board/issues/244",
                        "risk": "low"
                    }
                }
            }
            map_file = tmp_root / "FEATURE_MAP.json"
            map_file.write_text(json.dumps(bad_map), encoding="utf-8")

            cmd = [
                sys.executable,
                str(Path(SCRIPT_DIR) / "feature_map.py"),
                "validate",
                "--map", str(map_file),
                "--repo-root", str(tmp_root),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("[FAIL] Feature map validation failed", proc.stderr)
            self.assertIn("missing_file_xyz.py", proc.stderr)

    def test_19_cli_query_and_show(self):
        """CLI query and show commands format output correctly."""
        script = str(Path(SCRIPT_DIR) / "feature_map.py")
        # Query
        p_query = subprocess.run(
            [sys.executable, script, "query", "routing", "--repo-root", str(self.repo_root)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p_query.returncode, 0)
        self.assertIn("routing", p_query.stdout)
        self.assertIn("Model Routing", p_query.stdout)

        # Show
        p_show = subprocess.run(
            [sys.executable, script, "show", "build_slot", "--repo-root", str(self.repo_root)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(p_show.returncode, 0)
        self.assertIn("Feature ID    : build_slot", p_show.stdout)
        self.assertIn("BuildSlotManager", p_show.stdout)

    def test_20_cli_list_json(self):
        """CLI list --json produces valid JSON dictionary with all 5 features."""
        script = str(Path(SCRIPT_DIR) / "feature_map.py")
        proc = subprocess.run(
            [sys.executable, script, "list", "--json", "--repo-root", str(self.repo_root)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(len(data), 5)
        self.assertIn("routing", data)
        self.assertIn("build_slot", data)

    def test_21_generate_feature_entry(self):
        """generate_feature_entry constructs valid, serialized FeatureEntry."""
        entry = generate_feature_entry(
            feature_id="custom_tool",
            name="Custom Tool",
            description="A test tool",
            entry_files=["workflows/portable/custom.py"],
            tests=["workflows/portable/test_custom.py"],
            owning_issue="#123",
            risk="low",
            components=["CustomClass"],
        )
        self.assertEqual(entry.name, "Custom Tool")
        self.assertEqual(entry.risk, "low")
        d = entry.to_dict()
        self.assertEqual(d["name"], "Custom Tool")
        self.assertEqual(d["components"], ["CustomClass"])

        # Invalid risk raises ValueError
        with self.assertRaises(ValueError):
            generate_feature_entry(
                feature_id="bad",
                name="Bad",
                description="Bad",
                entry_files=["x.py"],
                tests=["t.py"],
                owning_issue="#1",
                risk="invalid_risk",
            )


if __name__ == "__main__":
    unittest.main()
