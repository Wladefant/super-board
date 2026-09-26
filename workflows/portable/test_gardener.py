#!/usr/bin/env python3
"""
test_gardener.py - Unit and regression tests for the Gardener lane runner.

Tests:
  1. Data model initialization and serialization.
  2. Next.js App Router entrypoint and test file classification heuristics.
  3. Knip finding classification (dead files, dead exports, dead types, entrypoints, tests, deps).
  4. Vulture finding classification (app imports, test fixtures, alembic configs, unreachable code).
  5. Bounded cleanup task spec generation (priority ordering, max_items capping, contract format).
  6. Markdown and text report summary generation.
  7. End-to-end gardener pipeline using fixture reports.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Ensure workflows/portable is on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from gardener import (
    CleanupTaskSpec,
    GardenerReport,
    ToolFinding,
    classify_knip_issue,
    classify_vulture_line,
    format_summary_markdown,
    format_summary_text,
    generate_cleanup_task_spec,
    is_frontend_entrypoint,
    is_test_file,
    run_gardener,
)


class TestGardenerClassification(unittest.TestCase):
    """Test classification rules for Knip and Vulture findings."""

    def test_is_frontend_entrypoint(self):
        """Next.js App Router conventions and configuration files must be recognized as entrypoints."""
        self.assertTrue(is_frontend_entrypoint("app/page.tsx"))
        self.assertTrue(is_frontend_entrypoint("app/markets/page.tsx"))
        self.assertTrue(is_frontend_entrypoint("app/layout.tsx"))
        self.assertTrue(is_frontend_entrypoint("app/api/route.ts"))
        self.assertTrue(is_frontend_entrypoint("app/not-found.tsx"))
        self.assertTrue(is_frontend_entrypoint("app/loading.tsx"))
        self.assertTrue(is_frontend_entrypoint("app/error.tsx"))
        self.assertTrue(is_frontend_entrypoint("next.config.mjs"))
        self.assertTrue(is_frontend_entrypoint("tailwind.config.js"))
        self.assertTrue(is_frontend_entrypoint("middleware.ts"))
        self.assertTrue(is_frontend_entrypoint("public/sw.js"))

        # Internal components and utilities are NOT entrypoints
        self.assertFalse(is_frontend_entrypoint("components/AccessSignup.tsx"))
        self.assertFalse(is_frontend_entrypoint("components/Hero.tsx"))
        self.assertFalse(is_frontend_entrypoint("lib/marketLifecycle.ts"))
        self.assertFalse(is_frontend_entrypoint("hooks/useTrading.ts"))

    def test_is_test_file(self):
        """Test files must be accurately identified to prevent automated deletion."""
        self.assertTrue(is_test_file("lib/gamma.test.ts"))
        self.assertTrue(is_test_file("components/__tests__/hero.test.tsx"))
        self.assertTrue(is_test_file("app/__tests__/meta.spec.ts"))
        self.assertFalse(is_test_file("components/Hero.tsx"))
        self.assertFalse(is_test_file("lib/gamma.ts"))

    def test_classify_knip_dead_component(self):
        """Knip unused component should be classified as verified_dead_file and safe to prune."""
        issue = {
            "file": "components/AccessSignup.tsx",
            "files": [{"name": "components/AccessSignup.tsx"}],
            "exports": [],
            "types": [],
        }
        findings = classify_knip_issue(issue, frontend_rel="frontend")
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.file, "frontend/components/AccessSignup.tsx")
        self.assertEqual(f.category, "verified_dead_file")
        self.assertTrue(f.safe_to_prune)
        self.assertEqual(f.tool, "knip")

    def test_classify_knip_framework_entrypoint(self):
        """Knip flagging an App Router page must be classified as framework_entrypoint and NOT safe to prune."""
        issue = {
            "file": "app/markets/page.tsx",
            "files": [{"name": "app/markets/page.tsx"}],
            "exports": [{"name": "default", "line": 10}],
            "types": [],
        }
        findings = classify_knip_issue(issue, frontend_rel="frontend")
        self.assertTrue(all(not f.safe_to_prune for f in findings))
        self.assertTrue(any(f.category == "framework_entrypoint" for f in findings))

    def test_classify_knip_test_file(self):
        """Knip flagging a test file must be classified as test_file and preserved."""
        issue = {
            "file": "lib/marketLifecycle.test.ts",
            "files": [{"name": "lib/marketLifecycle.test.ts"}],
            "exports": [],
            "types": [],
        }
        findings = classify_knip_issue(issue, frontend_rel="frontend")
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.category, "test_file")
        self.assertFalse(f.safe_to_prune)

    def test_classify_knip_dead_exports_and_types(self):
        """Knip dead exports and types must be classified as safe to prune."""
        issue = {
            "file": "components/SEO/schema.ts",
            "exports": [
                {"name": "ORGANIZATION_ID", "line": 72, "col": 14},
                {"name": "WEBSITE_ID", "line": 73, "col": 14},
            ],
            "types": [
                {"name": "MarketLifecycle", "line": 325, "col": 13}
            ],
        }
        findings = classify_knip_issue(issue, frontend_rel="frontend")
        self.assertEqual(len(findings), 3)

        exports = [f for f in findings if f.kind == "export"]
        self.assertEqual(len(exports), 2)
        self.assertTrue(all(f.safe_to_prune for f in exports))
        self.assertTrue(all(f.category == "verified_dead_export" for f in exports))

        types = [f for f in findings if f.kind == "type"]
        self.assertEqual(len(types), 1)
        self.assertTrue(types[0].safe_to_prune)
        self.assertEqual(types[0].category, "verified_dead_type")
        self.assertEqual(types[0].symbol, "MarketLifecycle")

    def test_classify_knip_dependencies(self):
        """Unused package dependencies must be flagged for manual review, not auto-pruned."""
        issue = {
            "file": "package.json",
            "dependencies": ["lodash"],
            "devDependencies": ["prettier"],
        }
        findings = classify_knip_issue(issue, frontend_rel="frontend")
        self.assertEqual(len(findings), 2)
        self.assertTrue(all(not f.safe_to_prune for f in findings))
        self.assertTrue(all(f.category == "unused_dependency" for f in findings))

    def test_classify_vulture_app_unused_import(self):
        """Vulture unused import in app/ must be classified as unused_import and safe to prune."""
        repo_root = Path("/fake/repo")
        line = "backend/app/api_v1/keys.py:33: unused import 'get_jwt_user' (90% confidence)"
        f = classify_vulture_line(line, repo_root)
        self.assertIsNotNone(f)
        self.assertEqual(f.tool, "vulture")
        self.assertEqual(f.kind, "import")
        self.assertEqual(f.symbol, "get_jwt_user")
        self.assertEqual(f.confidence, 90)
        self.assertEqual(f.category, "unused_import")
        self.assertTrue(f.safe_to_prune)

    def test_classify_vulture_test_fixtures(self):
        """Vulture unused variable in test files must be classified as test_fixture_or_dummy and NOT pruned."""
        repo_root = Path("/fake/repo")
        line = "backend/tests/test_accounts.py:318: unused variable 'seeded_wallets' (100% confidence)"
        f = classify_vulture_line(line, repo_root)
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, "variable")
        self.assertEqual(f.category, "test_fixture_or_dummy")
        self.assertFalse(f.safe_to_prune)

    def test_classify_vulture_alembic_config(self):
        """Vulture finding in alembic must be preserved."""
        repo_root = Path("/fake/repo")
        line = "backend/alembic/env.py:1: unused import 'fileConfig' (90% confidence)"
        f = classify_vulture_line(line, repo_root)
        self.assertIsNotNone(f)
        self.assertEqual(f.category, "alembic_config")
        self.assertFalse(f.safe_to_prune)

    def test_classify_vulture_unreachable_code(self):
        """Vulture unreachable code in application code should be safe to prune."""
        repo_root = Path("/fake/repo")
        line = "backend/app/polymarket_ws.py:12653: unreachable code after 'return' (100% confidence)"
        f = classify_vulture_line(line, repo_root)
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, "unreachable")
        self.assertEqual(f.symbol, "return")
        self.assertEqual(f.category, "unreachable_code")
        self.assertTrue(f.safe_to_prune)

    def test_classify_vulture_package_init_reexport(self):
        """Re-exports in __init__.py must be preserved."""
        repo_root = Path("/fake/repo")
        line = "backend/app/__init__.py:10: unused import 'celery_app' (90% confidence)"
        f = classify_vulture_line(line, repo_root)
        self.assertIsNotNone(f)
        self.assertEqual(f.category, "framework_entrypoint")
        self.assertFalse(f.safe_to_prune)


class TestGardenerTaskSpecAndReporting(unittest.TestCase):
    """Test task spec generation and summary report formatting."""

    def setUp(self):
        self.fe_findings = [
            ToolFinding(
                tool="knip",
                file="frontend/components/AccessSignup.tsx",
                kind="file",
                symbol="components/AccessSignup.tsx",
                category="verified_dead_file",
                safe_to_prune=True,
                reason="Unreferenced component",
            ),
            ToolFinding(
                tool="knip",
                file="frontend/components/Hero.tsx",
                kind="file",
                symbol="components/Hero.tsx",
                category="verified_dead_file",
                safe_to_prune=True,
                reason="Unreferenced component",
            ),
            ToolFinding(
                tool="knip",
                file="frontend/components/SEO/schema.ts",
                line=72,
                kind="export",
                symbol="ORGANIZATION_ID",
                category="verified_dead_export",
                safe_to_prune=True,
                reason="Unused export",
            ),
            ToolFinding(
                tool="knip",
                file="frontend/app/page.tsx",
                kind="file",
                symbol="app/page.tsx",
                category="framework_entrypoint",
                safe_to_prune=False,
                reason="App router entrypoint",
            ),
        ]
        self.be_findings = [
            ToolFinding(
                tool="vulture",
                file="backend/app/api_v1/keys.py",
                line=33,
                kind="import",
                symbol="get_jwt_user",
                confidence=90,
                category="unused_import",
                safe_to_prune=True,
                reason="Unused import in app code",
            ),
            ToolFinding(
                tool="vulture",
                file="backend/tests/test_foo.py",
                line=50,
                kind="variable",
                symbol="dummy_fixture",
                confidence=100,
                category="test_fixture_or_dummy",
                safe_to_prune=False,
                reason="Test fixture",
            ),
        ]
        self.report = GardenerReport(
            repo_root="/test/repo",
            timestamp="2026-09-25T22:00:00Z",
            min_confidence=80,
            frontend_findings=self.fe_findings,
            backend_findings=self.be_findings,
            summary={
                "frontend_total": 4,
                "frontend_dead_files": 2,
                "frontend_dead_exports": 1,
                "frontend_dead_types": 0,
                "frontend_framework_entrypoints": 1,
                "frontend_test_files": 0,
                "frontend_unused_deps": 0,
                "frontend_safe_prune": 3,
                "frontend_review_required": 0,
                "frontend_skipped": 1,
                "backend_total": 2,
                "backend_unused_imports": 1,
                "backend_unreachable": 0,
                "backend_unused_variables": 0,
                "backend_test_fixtures": 1,
                "backend_alembic": 0,
                "backend_safe_prune": 1,
                "backend_review_required": 0,
                "backend_skipped": 1,
                "total_findings": 6,
                "total_safe_prune": 4,
                "total_review_required": 0,
                "total_skipped": 2,
            },
        )

    def test_generate_cleanup_task_spec(self):
        """Task spec must cap items to max_items and include structured Goal/Constraints/Contract and Target/Change/Acceptance."""
        spec = generate_cleanup_task_spec(self.report, max_items=3, target_lane="spark")
        self.assertEqual(spec.target_lane, "spark")
        self.assertEqual(len(spec.target_items), 3)
        self.assertIn("kind:gardener", spec.pr_labels)
        self.assertIn("risk:low", spec.pr_labels)

        # Context structure check
        self.assertIn("## Goal", spec.context_text)
        self.assertIn("## Constraints", spec.context_text)
        self.assertIn("## Contract", spec.context_text)
        self.assertIn("tsc --noEmit", spec.context_text)

        # Task structure check
        self.assertIn("## Target", spec.task_text)
        self.assertIn("## Change", spec.task_text)
        self.assertIn("## Acceptance", spec.task_text)
        self.assertIn("frontend/components/AccessSignup.tsx", spec.task_text)

    def test_format_summary_markdown(self):
        """Markdown output must include overview table, category breakdowns, and task spec preview."""
        self.report.task_spec = generate_cleanup_task_spec(self.report, max_items=5)
        md = format_summary_markdown(self.report)
        self.assertIn("# Gardener Scan Report", md)
        self.assertIn("| **Knip**", md)
        self.assertIn("| **Vulture**", md)
        self.assertIn("Verified Dead Files", md)
        self.assertIn("Unused Application Imports", md)
        self.assertIn("Bounded Cleanup Candidate Batch", md)

    def test_format_summary_text(self):
        """Text summary output must include scannable metrics and task spec breakdown."""
        self.report.task_spec = generate_cleanup_task_spec(self.report, max_items=5)
        text = format_summary_text(self.report)
        self.assertIn("GARDENER SCAN REPORT", text)
        self.assertIn("Verified dead files", text)
        self.assertIn("TOTAL SAFE TO PRUNE", text)
        self.assertIn("BOUNDED CLEANUP TASK SPEC", text)

    def test_pipeline_with_fixture_files(self):
        """Gardener pipeline must run end-to-end with pre-computed fixture report files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            knip_file = Path(tmpdir) / "knip.json"
            vulture_file = Path(tmpdir) / "vulture.txt"

            knip_data = {
                "issues": [
                    {
                        "file": "components/OldCard.tsx",
                        "files": [{"name": "components/OldCard.tsx"}],
                        "exports": [],
                        "types": [],
                    }
                ]
            }
            with open(knip_file, "w", encoding="utf-8") as f:
                json.dump(knip_data, f)

            with open(vulture_file, "w", encoding="utf-8") as f:
                f.write("backend/app/main.py:42: unused import 'old_lib' (90% confidence)\n")
                f.write("backend/tests/test_x.py:10: unused variable 'mock_db' (100% confidence)\n")

            report = run_gardener(
                repo_root=Path(tmpdir),
                knip_report_file=knip_file,
                vulture_report_file=vulture_file,
                max_items=5,
            )

            self.assertEqual(report.summary["total_findings"], 3)
            self.assertEqual(report.summary["total_safe_prune"], 2)
            self.assertEqual(report.summary["backend_test_fixtures"], 1)
            self.assertIsNotNone(report.task_spec)
            self.assertEqual(len(report.task_spec.target_items), 2)


    def test_run_knip_subprocess_contract(self):
        """run_knip must invoke the pinned knip via subprocess without a NameError,
        pass shell=True only on Windows, and parse the returned JSON findings."""
        import subprocess as _subprocess
        import unittest.mock as _mock

        from gardener import run_knip

        with tempfile.TemporaryDirectory() as tmpdir:
            frontend = Path(tmpdir) / "frontend"
            frontend.mkdir()
            (frontend / "package.json").write_text("{}", encoding="utf-8")

            knip_payload = json.dumps({"issues": []})
            captured = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                captured["kwargs"] = kwargs
                return _subprocess.CompletedProcess(cmd, 0, stdout=knip_payload, stderr="")

            with _mock.patch("gardener.subprocess.run", side_effect=fake_run):
                findings, error = run_knip(Path(tmpdir))

            self.assertIsNone(error)
            self.assertEqual(captured["cmd"][:4], ["npx", "--yes", "knip@6.38.0", "--reporter"])
            self.assertEqual(captured["kwargs"]["shell"], os.name == "nt")
            self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
