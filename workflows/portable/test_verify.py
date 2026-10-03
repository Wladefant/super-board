#!/usr/bin/env python3
"""
Test Suite for Verification CLI, Smoke Test Gate, and PR Gate Consumption
Location: workflows/portable/test_verify.py

Verifies:
  1. Surface Classification:
     - Pure docs changes (.md, docs/, runbooks/, .gitignore, LICENSE)
     - Pure backend changes (backend/, alembic/, pyproject.toml, requirements.txt)
     - Pure frontend changes (frontend/, .tsx, package.json, tailwind.config)
     - Pure workflow changes (.github/, workflows/, scripts/, skills/)
     - Mixed surfaces (frontend + backend, docs + workflow, all 4 surfaces)
     - Deploy-critical checks matching (DEPLOY_CRITICAL_CHECKS conventions)
     - Lockfile handling (is_lockfile_or_generated)
     - Path normalization (backslashes, leading ./)
  2. Scenario Checks:
     - Docs lint: valid markdown, unclosed code fences (``` and ~~~), invalid UTF-8
     - Frontend QA check: missing receipt, failing verdict, head mismatch, valid dual-viewport receipt
     - Backend unit tests: syntax compilation, test receipt, test command execution
     - Workflow unit tests: syntax compilation, test command execution
  3. Receipt Generation & Validation:
     - verify-receipt/v1 schema compliance
     - Serialization to JSON and compact markdown
     - validate_verify_receipt helper behavior
  4. Gate Consumption in github_pr_gate.py:
     - Gate passes when verify receipt is valid and PASSED
     - Gate BLOCKED when verify receipt has status FAILED
     - Gate BLOCKED when verify receipt head SHA does not match PR head
     - Gate BLOCKED when policy requires verify receipt but none is provided
     - Gate passes without verify receipt when policy does not require it (backwards compatible)
     - CLI options (--verify-receipt, --require-verify-receipt)
  5. CLI execution:
     - Exit 0 on PASSED
     - Exit 2 on FAILED
     - Diff parsing and --receipt-out file output
"""
from __future__ import annotations

import copy
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from typing import Any, Dict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from verify import (
    SURFACE_BACKEND,
    SURFACE_DOCS,
    SURFACE_FRONTEND,
    SURFACE_WORKFLOW,
    ScenarioCheckResult,
    VerificationReceipt,
    classify_surfaces,
    extract_files_from_diff,
    normalize_file_path,
    run_backend_unit_tests,
    run_docs_lint,
    run_frontend_qa_check,
    run_workflow_unit_tests,
    validate_verify_receipt,
    verify_changes,
)
from github_pr_gate import (
    GateApprovalPolicy,
    PRGateEvaluation,
    evaluate_pr_gate,
)


class TestSurfaceClassification(unittest.TestCase):
    """Test classification of changed files into docs, backend, frontend, workflow surfaces."""

    def test_pure_docs_classification(self):
        files = [
            "docs/runbooks/GARDENER.md",
            "README.md",
            "docs/specs/architecture.mdx",
            ".gitignore",
            "LICENSE",
        ]
        surfaces, files_by_surface, deploy_crit = classify_surfaces(files)
        self.assertEqual(surfaces, [SURFACE_DOCS])
        self.assertEqual(len(files_by_surface[SURFACE_DOCS]), 5)
        self.assertFalse(deploy_crit)

    def test_pure_backend_classification(self):
        files = [
            "backend/app/main.py",
            "backend/app/api/v1/orders.py",
            "alembic/versions/20260926_orders.py",
            "alembic.ini",
            "pyproject.toml",
            "requirements.txt",
        ]
        surfaces, files_by_surface, deploy_crit = classify_surfaces(files)
        self.assertEqual(surfaces, [SURFACE_BACKEND])
        self.assertEqual(len(files_by_surface[SURFACE_BACKEND]), 6)
        self.assertTrue(deploy_crit)
        self.assertIn("backend/app/main.py", deploy_crit)

    def test_pure_frontend_classification(self):
        files = [
            "frontend/src/components/OrderBook.tsx",
            "frontend/src/styles/globals.css",
            "frontend/package.json",
            "tailwind.config.js",
            "app/routes/markets.tsx",
        ]
        surfaces, files_by_surface, deploy_crit = classify_surfaces(files)
        self.assertEqual(surfaces, [SURFACE_FRONTEND])
        self.assertEqual(len(files_by_surface[SURFACE_FRONTEND]), 5)
        self.assertFalse(deploy_crit)

    def test_pure_workflow_classification(self):
        files = [
            ".github/workflows/cross-platform.yml",
            "workflows/portable/verify.py",
            "workflows/portable/test_verify.py",
            "scripts/super-board-run.sh",
            "skills/super-qa/SKILL.md",
            "policies/default/AGENTS.md",
        ]
        surfaces, files_by_surface, deploy_crit = classify_surfaces(files)
        self.assertEqual(surfaces, [SURFACE_WORKFLOW])
        self.assertEqual(len(files_by_surface[SURFACE_WORKFLOW]), 6)

    def test_mixed_surfaces_classification(self):
        files = [
            "frontend/src/components/WalletButton.tsx",
            "backend/app/wallets.py",
            "docs/internal/wallet-migration.md",
            "workflows/portable/github_pr_gate.py",
        ]
        surfaces, files_by_surface, deploy_crit = classify_surfaces(files)
        self.assertIn(SURFACE_FRONTEND, surfaces)
        self.assertIn(SURFACE_BACKEND, surfaces)
        self.assertIn(SURFACE_DOCS, surfaces)
        self.assertIn(SURFACE_WORKFLOW, surfaces)
        self.assertEqual(len(surfaces), 4)

    def test_lockfile_and_generated_classification(self):
        files = [
            "frontend/package-lock.json",
            "poetry.lock",
        ]
        surfaces, files_by_surface, _ = classify_surfaces(files)
        self.assertIn(SURFACE_FRONTEND, surfaces)
        self.assertIn(SURFACE_BACKEND, surfaces)

    def test_path_normalization(self):
        files = [
            r"docs\internal\notes.md",
            "./frontend/src/App.tsx",
            r"backend\app\api.py",
        ]
        surfaces, files_by_surface, _ = classify_surfaces(files)
        self.assertIn("docs/internal/notes.md", files_by_surface[SURFACE_DOCS])
        self.assertIn("frontend/src/App.tsx", files_by_surface[SURFACE_FRONTEND])
        self.assertIn("backend/app/api.py", files_by_surface[SURFACE_BACKEND])

    def test_diff_extraction(self):
        diff_text = (
            "diff --git a/docs/runbooks/GARDENER.md b/docs/runbooks/GARDENER.md\n"
            "index 1234567..89abcdef 100644\n"
            "--- a/docs/runbooks/GARDENER.md\n"
            "+++ b/docs/runbooks/GARDENER.md\n"
            + "@" + "@ -1,3 +1,4 @" + "@\n"
            "+# Title\n"
            "diff --git a/workflows/portable/verify.py b/workflows/portable/verify.py\n"
            "new file mode 100644\n"
            "index 0000000..1234567\n"
            "--- /dev/null\n"
            "+++ b/workflows/portable/verify.py\n"
        )
        extracted = extract_files_from_diff(diff_text)
        self.assertEqual(extracted, ["docs/runbooks/GARDENER.md", "workflows/portable/verify.py"])


class TestScenarioChecks(unittest.TestCase):
    """Test scenario checks: docs_lint, browser_qa_receipt, backend_unit_tests, workflow_unit_tests."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="test_verify_scenario_")
        self.repo_root = self.temp_dir.name

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_docs_lint_valid_markdown(self):
        doc_path = os.path.join(self.repo_root, "README.md")
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write("# Sample Document\n\n```python\nprint('hello')\n```\n\nText here.\n")

        res = run_docs_lint(["README.md"], repo_root=self.repo_root)
        self.assertEqual(res.status, "PASSED")
        self.assertIn("syntax checks", res.details)

    def test_docs_lint_unclosed_backtick_fence(self):
        doc_path = os.path.join(self.repo_root, "broken.md")
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write("# Broken Document\n\n```python\nprint('unclosed')\n")

        res = run_docs_lint(["broken.md"], repo_root=self.repo_root)
        self.assertEqual(res.status, "FAILED")
        self.assertIn("Unclosed markdown code fence", res.details)

    def test_docs_lint_unclosed_tilde_fence(self):
        doc_path = os.path.join(self.repo_root, "tilde.md")
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write("# Broken Tilde\n\n~~~json\n{'unclosed': true}\n")

        res = run_docs_lint(["tilde.md"], repo_root=self.repo_root)
        self.assertEqual(res.status, "FAILED")
        self.assertIn("Unclosed markdown code fence", res.details)

    def test_docs_lint_invalid_utf8(self):
        doc_path = os.path.join(self.repo_root, "bad_encoding.txt")
        with open(doc_path, "wb") as f:
            f.write(b"\xff\xfe\x00\x00Invalid UTF8 byte sequence")

        res = run_docs_lint(["bad_encoding.txt"], repo_root=self.repo_root)
        self.assertEqual(res.status, "FAILED")
        self.assertIn("Invalid UTF-8 encoding", res.details)

    def test_frontend_qa_missing_receipt(self):
        head_sha = "a" * 40
        res = run_frontend_qa_check(["frontend/src/Button.tsx"], head_sha=head_sha, repo_root=self.repo_root)
        self.assertEqual(res.status, "FAILED")
        self.assertIn("Browser QA receipt missing", res.details)

    def test_frontend_qa_head_mismatch(self):
        head_sha = "a" * 40
        wrong_head = "b" * 40
        receipt = {
            "schema_version": "qa-receipt/v1",
            "head_sha": wrong_head,
            "verdict": "PASSED",
            "viewports": ["1440x900 desktop", "390x844 mobile"],
        }
        res = run_frontend_qa_check(
            ["frontend/src/Button.tsx"],
            head_sha=head_sha,
            qa_receipt_input=receipt,
            repo_root=self.repo_root,
        )
        self.assertEqual(res.status, "FAILED")
        self.assertIn("head mismatch", res.details)

    def test_frontend_qa_failing_verdict(self):
        head_sha = "a" * 40
        receipt = {
            "schema_version": "qa-receipt/v1",
            "head_sha": head_sha,
            "verdict": "FAILED",
            "viewports": ["1440x900 desktop", "390x844 mobile"],
        }
        res = run_frontend_qa_check(
            ["frontend/src/Button.tsx"],
            head_sha=head_sha,
            qa_receipt_input=receipt,
            repo_root=self.repo_root,
        )
        self.assertEqual(res.status, "FAILED")
        self.assertIn("verdict is 'FAILED'", res.details)

    def test_frontend_qa_valid_receipt(self):
        head_sha = "a" * 40
        receipt = {
            "schema_version": "qa-receipt/v1",
            "head_sha": head_sha,
            "verdict": "PASSED",
            "viewports": ["1440x900 desktop", "390x844 mobile"],
        }
        res = run_frontend_qa_check(
            ["frontend/src/Button.tsx"],
            head_sha=head_sha,
            qa_receipt_input=receipt,
            repo_root=self.repo_root,
        )
        self.assertEqual(res.status, "PASSED")
        self.assertTrue(res.evidence["has_desktop"])
        self.assertTrue(res.evidence["has_mobile"])

    def test_frontend_qa_from_standard_disk_location(self):
        head_sha = "c" * 40
        qa_dir = os.path.join(self.repo_root, ".veyyon", "qa")
        os.makedirs(qa_dir, exist_ok=True)
        receipt_file = os.path.join(qa_dir, f"receipt-{head_sha}.json")
        with open(receipt_file, "w", encoding="utf-8") as f:
            json.dump({
                "schema_version": "qa-receipt/v1",
                "head_sha": head_sha,
                "verdict": "PASSED",
                "viewports": ["1440px desktop", "320px mobile"],
            }, f)

        res = run_frontend_qa_check(
            ["frontend/src/Widget.tsx"],
            head_sha=head_sha,
            repo_root=self.repo_root,
        )
        self.assertEqual(res.status, "PASSED")

    def test_frontend_qa_from_pr_comments(self):
        head_sha = "d" * 40
        pr_data = {
            "comments": [
                {
                    "author": "qa-agent",
                    "body": f"Automated QA report:\n```json\n{{\"schema_version\": \"qa-receipt/v1\", \"head_sha\": \"{head_sha}\", \"verdict\": \"PASSED\", \"viewports\": [\"1440x900\", \"390x844\"]}}\n```",
                }
            ]
        }
        res = run_frontend_qa_check(
            ["frontend/src/Header.tsx"],
            head_sha=head_sha,
            pr_data=pr_data,
            repo_root=self.repo_root,
        )
        self.assertEqual(res.status, "PASSED")

    def test_backend_unit_tests_valid_syntax(self):
        py_path = os.path.join(self.repo_root, "backend_module.py")
        with open(py_path, "w", encoding="utf-8") as f:
            f.write("def add(a: int, b: int) -> int:\n    return a + b\n")

        res = run_backend_unit_tests(["backend_module.py"], repo_root=self.repo_root)
        self.assertEqual(res.status, "PASSED")

    def test_backend_unit_tests_syntax_error(self):
        py_path = os.path.join(self.repo_root, "broken_module.py")
        with open(py_path, "w", encoding="utf-8") as f:
            f.write("def broken(\n")

        res = run_backend_unit_tests(["broken_module.py"], repo_root=self.repo_root)
        self.assertEqual(res.status, "FAILED")
        self.assertIn("Python compilation failed", res.details)

    def test_backend_receipt_consumption(self):
        receipt = {"status": "PASSED", "tests_run": 42}
        res = run_backend_unit_tests(["backend/app.py"], backend_receipt=receipt)
        self.assertEqual(res.status, "PASSED")
        self.assertIn("verified via test receipt", res.details)

    def test_workflow_unit_tests_valid_syntax(self):
        py_path = os.path.join(self.repo_root, "workflow_script.py")
        with open(py_path, "w", encoding="utf-8") as f:
            f.write("print('workflow ready')\n")

        res = run_workflow_unit_tests(["workflow_script.py"], repo_root=self.repo_root)
        self.assertEqual(res.status, "PASSED")

    def test_workflow_unit_tests_syntax_error(self):
        py_path = os.path.join(self.repo_root, "broken_wf.py")
        with open(py_path, "w", encoding="utf-8") as f:
            f.write("def wf_bad(:\n")

        res = run_workflow_unit_tests(["broken_wf.py"], repo_root=self.repo_root)
        self.assertEqual(res.status, "FAILED")
        self.assertIn("Workflow Python syntax error", res.details)


class TestReceiptGenerationAndValidation(unittest.TestCase):
    """Test VerificationReceipt generation, serialization, and validation."""

    def test_full_passing_receipt_generation(self):
        head_sha = "1" * 40
        receipt = verify_changes(
            files=["docs/README.md"],
            head_sha=head_sha,
            base_sha="0" * 40,
            base_ref="main",
            pr_number=247,
            repo="Wladefant/super-board",
            run_checks=False,
        )
        self.assertEqual(receipt.status, "PASSED")
        self.assertEqual(receipt.head_sha, head_sha)
        self.assertEqual(receipt.schema_version, "verify-receipt/v1")
        self.assertEqual(receipt.pr_number, 247)
        self.assertIn(SURFACE_DOCS, receipt.surfaces)

        # Check serialization round-trip
        as_dict = receipt.to_dict()
        as_json = receipt.to_json()
        loaded = json.loads(as_json)
        self.assertEqual(loaded["schema_version"], "verify-receipt/v1")
        self.assertEqual(loaded["status"], "PASSED")

        # Check markdown rendering
        md = receipt.to_compact_markdown()
        self.assertIn("Verification Receipt: PASSED", md)
        self.assertIn("`docs`", md)

    def test_failing_receipt_generation(self):
        head_sha = "2" * 40
        # Frontend change with no QA receipt -> FAILED
        receipt = verify_changes(
            files=["frontend/src/App.tsx"],
            head_sha=head_sha,
            pr_number=101,
            repo="Bavariance/polysimulator",
        )
        self.assertEqual(receipt.status, "FAILED")
        self.assertIn("frontend: Browser QA receipt missing", receipt.summary)
        self.assertEqual(receipt.checks[SURFACE_FRONTEND].status, "FAILED")

    def test_validate_verify_receipt_valid(self):
        head_sha = "3" * 40
        receipt = {
            "schema_version": "verify-receipt/v1",
            "receipt_id": "vr-12345",
            "head_sha": head_sha,
            "pr_number": 247,
            "status": "PASSED",
            "summary": "All scenario checks succeeded.",
        }
        valid, err = validate_verify_receipt(receipt, head_sha=head_sha, expected_pr=247)
        self.assertTrue(valid)
        self.assertIsNone(err)

    def test_validate_verify_receipt_head_mismatch(self):
        receipt = {
            "schema_version": "verify-receipt/v1",
            "head_sha": "4" * 40,
            "status": "PASSED",
        }
        valid, err = validate_verify_receipt(receipt, head_sha="5" * 40)
        self.assertFalse(valid)
        self.assertIn("does not match expected head", err)

    def test_validate_verify_receipt_status_failed(self):
        head_sha = "6" * 40
        receipt = {
            "schema_version": "verify-receipt/v1",
            "head_sha": head_sha,
            "status": "FAILED",
            "summary": "Frontend QA missing.",
        }
        valid, err = validate_verify_receipt(receipt, head_sha=head_sha)
        self.assertFalse(valid)
        self.assertIn("status is 'FAILED'", err)

    def test_validate_verify_receipt_wrong_schema(self):
        receipt = {
            "schema_version": "wrong-version",
            "head_sha": "7" * 40,
            "status": "PASSED",
        }
        valid, err = validate_verify_receipt(receipt, head_sha="7" * 40)
        self.assertFalse(valid)
        self.assertIn("Invalid receipt schema_version", err)


class TestGateConsumption(unittest.TestCase):
    """Test integration of verify receipt into github_pr_gate.py."""

    @classmethod
    def setUpClass(cls):
        cls.repository = tempfile.TemporaryDirectory(prefix="test_gate_consumption_")
        cls.addClassCleanup(cls.repository.cleanup)
        previous = os.getcwd()
        cls.addClassCleanup(os.chdir, previous)
        os.chdir(cls.repository.name)

        def git(*args):
            return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL).decode().strip()

        git("init", "-b", "fixture-base")
        git("config", "user.name", "Wladimir Kirjanovs")
        git("config", "user.email", "wladefant@gmail.com")
        with open("file.txt", "w", encoding="utf-8") as f:
            f.write("base\n")
        git("add", "file.txt")
        git("commit", "-m", "base commit")
        cls.base_sha = git("rev-parse", "HEAD")
        for base in ("fixture-base", "main", "staging"):
            git("update-ref", f"refs/remotes/origin/{base}", cls.base_sha)
            if base != "fixture-base":
                git("branch", base, cls.base_sha)
        git("remote", "add", "origin", cls.repository.name)
        git("checkout", "-b", "feature")
        with open("file.txt", "w", encoding="utf-8") as f:
            f.write("updated\n")
        git("add", "file.txt")
        git("commit", "-m", "head commit")
        cls.head_sha = git("rev-parse", "HEAD")

    def setUp(self):
        self.mock_pr = {
            "number": 247,
            "state": "OPEN",
            "isDraft": False,
            "headRefOid": self.head_sha,
            "baseRefOid": self.base_sha,
            "baseRefName": "fixture-base",
            "author": {"login": "feature-developer"},
            "statusCheckRollup": [
                {
                    "name": "test-suite",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "completedAt": "2026-09-05T08:00:00Z",
                },
                {
                    "name": "lint-and-typecheck",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "completedAt": "2026-09-05T08:05:00Z",
                },
                {
                    "name": "security-scan",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "completedAt": "2026-09-05T08:10:00Z",
                },
            ],
            "reviews": [
                {
                    "author": {"login": "independent-reviewer"},
                    "state": "APPROVED",
                    "commit": {"oid": self.head_sha},
                    "submittedAt": "2026-09-05T08:15:00Z",
                }
            ],
            "labels": [{"name": "kind:feature"}],
            "files": [{"path": "workflows/portable/verify.py", "additions": 10, "deletions": 2}],
        }

    def test_gate_with_passing_verify_receipt(self):
        receipt = {
            "schema_version": "verify-receipt/v1",
            "receipt_id": "vr-test-1",
            "head_sha": self.head_sha,
            "pr_number": 247,
            "status": "PASSED",
            "summary": "All scenario checks succeeded.",
        }
        res = evaluate_pr_gate(
            self.mock_pr,
            expected_head_sha=self.head_sha,
            verify_receipt=receipt,
            require_verify_receipt=True,
        )
        self.assertEqual(res.gate_verdict, "PASSED")
        self.assertEqual(res.verify_receipt_verdict, "PASSED")
        self.assertIn("Verification receipt: PASSED", res.verdict_reason)

    def test_gate_with_failing_verify_receipt(self):
        receipt = {
            "schema_version": "verify-receipt/v1",
            "receipt_id": "vr-test-2",
            "head_sha": self.head_sha,
            "pr_number": 247,
            "status": "FAILED",
            "summary": "Browser QA receipt missing for head.",
        }
        res = evaluate_pr_gate(
            self.mock_pr,
            expected_head_sha=self.head_sha,
            verify_receipt=receipt,
            require_verify_receipt=True,
        )
        self.assertEqual(res.gate_verdict, "BLOCKED")
        self.assertEqual(res.verify_receipt_verdict, "FAILED")
        self.assertIn("Verification receipt rejected", res.verdict_reason)
        self.assertIn("Browser QA receipt missing", res.verdict_reason)

    def test_gate_with_head_mismatch_verify_receipt(self):
        stale_sha = "0" * 40
        receipt = {
            "schema_version": "verify-receipt/v1",
            "receipt_id": "vr-test-3",
            "head_sha": stale_sha,
            "pr_number": 247,
            "status": "PASSED",
        }
        res = evaluate_pr_gate(
            self.mock_pr,
            expected_head_sha=self.head_sha,
            verify_receipt=receipt,
            require_verify_receipt=True,
        )
        self.assertEqual(res.gate_verdict, "BLOCKED")
        self.assertEqual(res.verify_receipt_verdict, "FAILED")
        self.assertIn("does not match expected head", res.verdict_reason)

    def test_gate_missing_receipt_when_required(self):
        res = evaluate_pr_gate(
            self.mock_pr,
            expected_head_sha=self.head_sha,
            verify_receipt=None,
            require_verify_receipt=True,
        )
        self.assertEqual(res.gate_verdict, "BLOCKED")
        self.assertEqual(res.verify_receipt_verdict, "MISSING")
        self.assertIn("Verification receipt missing for commit", res.verdict_reason)
        self.assertIn("PR is not ready for promotion", res.verdict_reason)

    def test_gate_missing_receipt_when_not_required_preserves_pass(self):
        # When require_verify_receipt is False, no receipt is required -> passes backwards-compatibly
        res = evaluate_pr_gate(
            self.mock_pr,
            expected_head_sha=self.head_sha,
            verify_receipt=None,
            require_verify_receipt=False,
        )
        self.assertEqual(res.gate_verdict, "PASSED")
        self.assertEqual(res.verify_receipt_verdict, "EXEMPT")

    def test_gate_policy_require_verify_receipt(self):
        policy = GateApprovalPolicy(
            repo="Wladefant/super-board",
            base_ref="main",
            require_github_approval=False,
            require_head_bound_review_evidence=True,
            require_verify_receipt=True,
        )
        res_blocked = evaluate_pr_gate(
            self.mock_pr,
            expected_head_sha=self.head_sha,
            policy=policy,
            verify_receipt=None,
        )
        self.assertEqual(res_blocked.gate_verdict, "BLOCKED")
        self.assertEqual(res_blocked.verify_receipt_verdict, "MISSING")

        receipt = {
            "schema_version": "verify-receipt/v1",
            "receipt_id": "vr-policy-1",
            "head_sha": self.head_sha,
            "pr_number": 247,
            "status": "PASSED",
        }
        res_passed = evaluate_pr_gate(
            self.mock_pr,
            expected_head_sha=self.head_sha,
            policy=policy,
            verify_receipt=receipt,
        )
        self.assertEqual(res_passed.gate_verdict, "PASSED")
        self.assertEqual(res_passed.verify_receipt_verdict, "PASSED")


class TestVerifyCLI(unittest.TestCase):
    """Test CLI execution of workflows/portable/verify.py."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="test_verify_cli_")
        self.repo_root = self.temp_dir.name
        self.head_sha = "8" * 40

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_cli_files_docs_pass(self):
        doc = os.path.join(self.repo_root, "GUIDE.md")
        with open(doc, "w", encoding="utf-8") as f:
            f.write("# Guide\n\n```sh\necho test\n```\n")

        receipt_out = os.path.join(self.repo_root, "receipt.json")
        cmd = [
            sys.executable,
            "-B",
            os.path.join(SCRIPT_DIR, "verify.py"),
            "--files", "GUIDE.md",
            "--head-sha", self.head_sha,
            "--repo-root", self.repo_root,
            "--receipt-out", receipt_out,
            "--json",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"CLI stderr: {res.stderr}")
        self.assertTrue(os.path.exists(receipt_out))
        with open(receipt_out, "r", encoding="utf-8") as rf:
            data = json.load(rf)
            self.assertEqual(data["status"], "PASSED")
            self.assertEqual(data["head_sha"], self.head_sha)

    def test_cli_files_frontend_fail_without_receipt(self):
        cmd = [
            sys.executable,
            "-B",
            os.path.join(SCRIPT_DIR, "verify.py"),
            "--files", "frontend/src/Card.tsx",
            "--head-sha", self.head_sha,
            "--repo-root", self.repo_root,
            "--json",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(res.returncode, 2)
        data = json.loads(res.stdout)
        self.assertEqual(data["status"], "FAILED")
        self.assertIn("frontend", data["surfaces"])


def main():
    print("=" * 70)
    print("RUNNING VERIFICATION CLI & SMOKE TEST GATE SUITE")
    print("=" * 70)
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    if not result.wasSuccessful():
        sys.exit(1)


if __name__ == "__main__":
    main()
