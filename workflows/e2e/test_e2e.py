"""Tests for the e2e integration helpers (https://github.com/Wladefant/super-board/issues/473).

Run: python -m pytest workflows/e2e/test_e2e.py -q
Every test names the assertion id it defends (FAIL <check-id> / FLOW-QA-REASON <name>).
"""
from __future__ import annotations

import http.server
import io
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import e2e_guard  # noqa: E402
import e2e_receipt  # noqa: E402
import e2e_run  # noqa: E402

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HEAD = "a" * 40
OTHER = "b" * 40
TEMPLATE = HERE / "e2e.config.template.ts"


def make_report(
    *, status="passed", targets=("390x844", "1440x900"), assertions=2, failing=0,
    origin="http://127.0.0.1:3000", errors=None, model_calls=0, cache_mode="self-finalized",
):
    """A report-1 document shaped like the one e2e 0.17.0 wrote in the Windows spike."""
    results = []
    for tid in targets:
        steps = [{"kind": "app", "api": "app.open", "status": "passed"}]
        for i in range(assertions):
            steps.append({"kind": "assertion", "api": "expect.toBeVisible", "status": "failed" if i < failing else "passed"})
        steps.append({
            "kind": "agent", "api": "agent.act", "status": "passed",
            "metrics": {"modelCalls": model_calls}, "cache": {"mode": cache_mode},
        })
        ok = failing == 0
        results.append({
            "selected": True, "targetId": tid, "status": "passed" if ok else "failed",
            "attempts": [{"status": "passed" if ok else "failed", "steps": steps}],
        })
    return {
        "schemaVersion": "report-1",
        "run": {
            "status": status, "errors": errors or [],
            "targets": [{"id": t, "baseOrigin": origin} for t in targets],
            "results": results,
        },
    }


def receipt(report, expected=HEAD, served=HEAD, viewports=("390x844", "1440x900")):
    ev = e2e_receipt.evaluate(report, expected, served, list(viewports))
    return ev, e2e_receipt.render(ev, served)


class ReceiptTests(unittest.TestCase):
    def test_pass_when_assertions_ran_and_sha_matches(self):
        ev, text = receipt(make_report())
        self.assertEqual(ev["reasons"], [])
        self.assertIn(f"FLOW-QA: PASS {HEAD}", text)
        self.assertIn("FLOW-QA-ASSERTIONS pass=4 fail=0", text)
        self.assertIn("FLOW-QA-VIEWPORTS 390x844,1440x900", text)
        self.assertIn("E2E-CACHE replayed=2 missed=0 model_calls=0", text)

    def test_failing_assertion_is_assertion_failed(self):
        ev, text = receipt(make_report(failing=1, status="failed"))
        self.assertIn("assertion_failed", ev["reasons"])
        self.assertIn("FLOW-QA: FAIL", text)
        self.assertIn("FLOW-QA-REASON assertion_failed", text)

    def test_zero_test_run_is_zero_assertions(self):
        report = make_report()
        report["run"]["results"] = []
        ev, text = receipt(report)
        self.assertIn("zero_assertions", ev["reasons"])
        self.assertIn("FLOW-QA: FAIL", text)
        self.assertIn("FLOW-QA-ASSERTIONS pass=0 fail=0", text)

    def test_run_with_only_non_assertion_steps_is_zero_assertions(self):
        ev, _ = receipt(make_report(assertions=0))
        self.assertIn("zero_assertions", ev["reasons"])

    def test_served_sha_mismatch_is_fail_and_names_the_served_sha(self):
        ev, text = receipt(make_report(), served=OTHER)
        self.assertIn("served_sha_mismatch", ev["reasons"])
        self.assertIn(f"FLOW-QA: FAIL {OTHER}", text)

    def test_unverified_served_sha_prints_no_sha(self):
        ev, text = receipt(make_report(), served=None)
        self.assertIn("served_sha_unverified", ev["reasons"])
        self.assertTrue(text.startswith("FLOW-QA: FAIL\n"))

    def test_short_served_sha_is_unverified(self):
        ev, _ = receipt(make_report(), served="abc123")
        self.assertIn("served_sha_unverified", ev["reasons"])

    def test_production_origin_is_production_host(self):
        ev, _ = receipt(make_report(origin="https://polysimulator.com"))
        self.assertIn("production_host", ev["reasons"])

    def test_missing_required_viewport(self):
        ev, _ = receipt(make_report(targets=("390x844",)))
        self.assertIn("missing_viewports", ev["reasons"])
        self.assertEqual(ev["missing_viewports"], ["1440x900"])

    def test_failed_viewport_is_not_listed_as_covered(self):
        report = make_report()
        report["run"]["results"][1]["status"] = "failed"
        ev, text = receipt(report)
        self.assertNotIn("1440x900", ev["viewports"])
        self.assertIn("missing_viewports", ev["reasons"])

    def test_run_errors_make_the_run_not_passed(self):
        ev, _ = receipt(make_report(errors=[{"code": "INVALID_CONFIG"}]))
        self.assertIn("run_not_passed", ev["reasons"])

    def test_garbage_report_is_report_invalid(self):
        for bad in (None, {}, {"schemaVersion": "report-2", "run": {}}, []):
            ev, text = receipt(bad)
            self.assertIn("report_invalid", ev["reasons"])
            self.assertIn("FLOW-QA: FAIL", text)

    def test_unselected_results_are_not_counted(self):
        report = make_report()
        report["run"]["results"].append({
            "selected": False, "targetId": "390x844", "status": "skipped", "attempts": [],
        })
        ev, _ = receipt(report)
        self.assertEqual(ev["reasons"], [])

    def test_cli_exit_code_follows_the_receipt_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text(json.dumps(make_report()), encoding="utf-8")
            base = ["--report", str(path), "--expected-sha", HEAD]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(e2e_receipt.main(base + ["--served-sha", HEAD]), 0)
                self.assertEqual(e2e_receipt.main(base + ["--served-sha", OTHER]), 1)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="e2eguard-"))
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.test")
        self.git("config", "user.name", "t")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.tmp, check=True, capture_output=True, timeout=60, creationflags=NO_WINDOW)

    def stage(self, rel, content="x\n"):
        path = self.tmp / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.git("add", "-f", rel)

    def findings(self, allow_cache=False):
        return e2e_guard.scan_staged(self.tmp, allow_cache)

    def test_clean_stage_passes(self):
        self.stage("tests/a.e2e.ts", "import { test } from 'e2e';\n")
        self.stage(".env.example", "FOO=\n")
        self.assertEqual(self.findings(), [])

    def test_oauth_file_is_refused(self):
        self.stage(".e2e/oauth.json", "{}")
        self.assertTrue(any(f.startswith("FAIL oauth-file") for f in self.findings()))

    def test_config_dir_oauth_is_refused(self):
        self.stage("home/.config/e2e/oauth.json", "{}")
        ids = " ".join(self.findings())
        self.assertIn("FAIL oauth-file", ids)

    def test_env_file_is_refused(self):
        self.stage(".env", "A=1\n")
        self.stage("app/.env.local", "A=1\n")
        found = [f for f in self.findings() if f.startswith("FAIL env-file")]
        self.assertEqual(len(found), 2)

    def test_e2e_output_is_refused(self):
        for rel in (".e2e/report.json", ".e2e/artifacts/a.png", ".e2e/logs/app.log", ".ai-trace/t.json"):
            self.stage(rel)
        ids = {f.split(":")[0] for f in self.findings()}
        self.assertEqual(ids, {"FAIL e2e-output", "FAIL ai-trace"})

    def test_cache_needs_the_repo_rule(self):
        self.stage(".e2e/cache/abc.json", "{}")
        self.assertTrue(any(f.startswith("FAIL e2e-cache-not-allowed") for f in self.findings()))
        self.assertEqual(self.findings(allow_cache=True), [])

    def test_secret_shapes_in_content_are_refused(self):
        fake_key = "sk-" + "A1b2C3d4" * 4
        self.stage("tests/leak.e2e.ts", f"const k = '{fake_key}';\n")
        self.stage("notes.txt", "E2E_OAUTH_CREDENTIALS=abcdef123456\n")
        ids = {f.split(":")[0] for f in self.findings()}
        self.assertIn("FAIL secret-api-key", ids)
        self.assertIn("FAIL secret-oauth-env", ids)

    def test_a_cache_file_with_a_token_is_refused_even_when_allowed(self):
        fake = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20
        self.stage(".e2e/cache/abc.json", json.dumps({"typed": fake}))
        self.assertTrue(any(f.startswith("FAIL secret-jwt") for f in self.findings(allow_cache=True)))

    def test_env_reference_is_not_a_secret(self):
        self.stage("run.ps1", "$env:E2E_MODEL_API_KEY = $key\nE2E_MODEL_API_KEY=$KEY\n")
        self.assertEqual(self.findings(), [])

    def test_cli_exit_codes(self):
        self.stage(".e2e/oauth.json", "{}")
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(e2e_guard.main(["staged", "--repo", str(self.tmp)]), 1)
        self.assertIn("FAIL oauth-file", out.getvalue())


class ConfigTests(unittest.TestCase):
    def check(self, text, package=None):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "e2e.config.ts"
            cfg.write_text(text, encoding="utf-8")
            pkg = None
            if package is not None:
                pkg = Path(tmp) / "package.json"
                pkg.write_text(json.dumps(package), encoding="utf-8")
            return e2e_guard.check_config(cfg, pkg)

    def ids(self, findings):
        return {f.split(":")[0].replace("FAIL ", "") for f in findings}

    def test_template_passes(self):
        good_pkg = {"devDependencies": {"e2e": "0.17.0", "@e2e-dev/web": "0.12.0"}}
        self.assertEqual(self.check(TEMPLATE.read_text(encoding="utf-8"), good_pkg), [])

    def test_removing_the_host_guard_fails(self):
        text = re.sub(r"// BEGIN host-guard.*?// END host-guard", "", TEMPLATE.read_text(encoding="utf-8"), flags=re.S)
        self.assertIn("config-host-guard-missing", self.ids(self.check(text)))

    def test_production_host_in_allow_list_fails(self):
        text = TEMPLATE.read_text(encoding="utf-8").replace(
            "const STAGING_HOSTS: string[] = [];", "const STAGING_HOSTS: string[] = ['polysimulator.com'];"
        )
        self.assertIn("config-production-in-allow-list", self.ids(self.check(text)))

    def test_hosted_engine_login_and_literal_key_fail(self):
        base = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("config-hosted-engine", self.ids(self.check("import { kernel } from '@e2e-dev/kernel';\n" + base)))
        self.assertIn("config-login", self.ids(self.check(base + "\nconst s = 'e2e login openai';\n")))
        self.assertIn("config-literal-key", self.ids(self.check(base + "\nconst c = { apiKey: 'abcdefgh1234' };\n")))

    def test_cache_default_must_be_read_only(self):
        text = TEMPLATE.read_text(encoding="utf-8").replace("'read-only'", "'read-write'")
        self.assertIn("config-cache-default", self.ids(self.check(text)))

    def test_package_pins_must_be_exact(self):
        template = TEMPLATE.read_text(encoding="utf-8")
        caret = {"devDependencies": {"e2e": "^0.17.0", "@e2e-dev/web": "0.12.0"}}
        self.assertIn("pin-mismatch", self.ids(self.check(template, caret)))
        missing = {"devDependencies": {"e2e": "0.17.0"}}
        self.assertIn("pin-missing", self.ids(self.check(template, missing)))
        hosted = {"devDependencies": {"e2e": "0.17.0", "@e2e-dev/web": "0.12.0", "@e2e-dev/eas": "1.0.0"}}
        self.assertIn("forbidden-package", self.ids(self.check(template, hosted)))

    def test_pins_file_matches_the_spike(self):
        self.assertEqual(e2e_guard.PINS["packages"]["e2e"], "0.17.0")
        self.assertEqual(e2e_guard.PINS["packages"]["@e2e-dev/web"], "0.12.0")
        self.assertEqual(e2e_guard.PINS["env"]["E2E_TELEMETRY_DISABLED"], "1")
        self.assertEqual(e2e_guard.PINS["model"]["model"], "qwen3.8-flash")
        self.assertFalse(e2e_guard.PINS["model"]["providerOptions"]["opencodeGo"]["enable_thinking"])

    def test_e2e_skill_file_and_catalog(self):
        repo_root = HERE.parent.parent
        skill_file = repo_root / "managed-skills" / "e2e" / "SKILL.md"
        self.assertTrue(skill_file.exists(), f"SKILL.md not found at {skill_file}")
        text = skill_file.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: e2e\n"), "SKILL.md must start with e2e frontmatter")
        self.assertIn("description:", text)
        self.assertIn("tester-army/e2e", text)
        self.assertIn("workflows/e2e/pins.json", text)
        self.assertIn("qwen3.8-flash", text)
        self.assertIn("e2e_receipt.py", text)
        self.assertIn("e2e_run.py", text)
        self.assertIn("e2e_guard.py", text)

        readme_file = repo_root / "managed-skills" / "README.md"
        self.assertTrue(readme_file.exists(), f"README.md not found at {readme_file}")
        readme_content = readme_file.read_text(encoding="utf-8")
        self.assertIn("| [`e2e`](./e2e/SKILL.md) |", readme_content)
        self.assertIn("super-board#486", readme_content)

    def test_e2e_skill_frontmatter_is_valid(self):
        repo_root = HERE.parent.parent
        skill_file = repo_root / "managed-skills" / "e2e" / "SKILL.md"
        text = skill_file.read_text(encoding="utf-8")
        parts = text.split("---\n", 2)
        self.assertGreaterEqual(len(parts), 3)
        frontmatter = parts[1]
        self.assertIn("name: e2e", frontmatter)
        self.assertIn("description:", frontmatter)
        desc_lines = [l for l in frontmatter.splitlines() if l.startswith("description:")]
        self.assertEqual(len(desc_lines), 1)
        self.assertGreater(len(desc_lines[0].split("description:")[1].strip()), 20)

    def test_policy_documents_flow_qa_boundary(self):
        policy = (HERE / "POLICY.md").read_text(encoding="utf-8")
        self.assertIn("Flow QA integration & replacement boundary", policy)
        self.assertIn("https://github.com/Wladefant/super-board/issues/487", policy)
        self.assertIn("What e2e replaces", policy)
        self.assertIn("What remains", policy)
        self.assertIn("github_pr_gate.py", policy)
        self.assertIn("390x844", policy)
        self.assertIn("1440x900", policy)

    def test_policy_documents_slice_1_and_slice_2(self):
        policy = (HERE / "POLICY.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/Wladefant/super-board/issues/475", policy)
        self.assertIn("https://github.com/Wladefant/super-board/issues/476", policy)
        self.assertIn("Windows spike findings and platform caveats", policy)
        self.assertIn("e2e@0.17.0", policy)
        self.assertIn("@e2e-dev/web@0.12.0", policy)
        self.assertIn("Telemetry is on by default upstream", policy)
        self.assertIn("Zero-secret cache replay", policy)
        self.assertIn("Mobile engine restriction", policy)

    def test_host_function(self):
        allowed = ["localhost", "127.0.0.1"]
        self.assertIsNone(e2e_guard.host_allowed("http://127.0.0.1:3000", allowed))
        forbidden = "E2E_HOST_NOT_ALLOWED:forbidden-production-host"
        for prod in ("https://polysimulator.com", "https://POLYSIMULATOR.COM.", "https://app.polysimulator.com:443/x",
                     "http://localhost@polysimulator.com", "https://x.zaraprptkegxqpvnsubu.supabase.co"):
            self.assertEqual(e2e_guard.host_allowed(prod, allowed + ["x.zaraprptkegxqpvnsubu.supabase.co"]), forbidden, prod)
        self.assertEqual(e2e_guard.host_allowed("https://example.org", allowed), "E2E_HOST_NOT_ALLOWED:not-in-allow-list")
        self.assertEqual(e2e_guard.host_allowed("http://127.0.0.1.nip.io", allowed), "E2E_HOST_NOT_ALLOWED:not-in-allow-list")

    def test_normalization_before_matching(self):
        allowed = ["localhost"]
        for ok in ("http://LOCALHOST:3000", "http://user:pw@localhost:3000/a", "http://app.localhost"):
            self.assertIsNone(e2e_guard.host_allowed(ok, allowed), ok)
        self.assertIsNotNone(e2e_guard.host_allowed("http://evillocalhost", allowed))

    def test_staging_host_under_the_product_domain_can_be_allowed(self):
        staging = ["staging.polysimulator.com"]
        self.assertIsNone(e2e_guard.host_allowed("https://staging.polysimulator.com", staging))
        self.assertIsNone(e2e_guard.host_allowed("https://x.staging.polysimulator.com", staging))
        self.assertEqual(
            e2e_guard.host_allowed("https://polysimulator.com", staging), "E2E_HOST_NOT_ALLOWED:forbidden-production-host"
        )
        self.assertEqual(
            e2e_guard.host_allowed("https://app.polysimulator.com", staging), "E2E_HOST_NOT_ALLOWED:forbidden-production-host"
        )
        self.assertEqual(
            e2e_guard.host_allowed("https://other.polysimulator.com", staging), "E2E_HOST_NOT_ALLOWED:not-in-allow-list"
        )
        template = TEMPLATE.read_text(encoding="utf-8").replace(
            "const STAGING_HOSTS: string[] = [];", "const STAGING_HOSTS: string[] = ['staging.polysimulator.com'];"
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "e2e.config.ts"
            cfg.write_text(template, encoding="utf-8")
            self.assertEqual(e2e_guard.check_config(cfg, None), [])

    def test_weakened_guard_block_is_modified(self):
        weak = TEMPLATE.read_text(encoding="utf-8").replace(
            "if (appRefusal) {", "if (false && appRefusal) {"
        )
        self.assertIn("config-host-guard-modified", self.ids(self.check(weak)))


def node_can_strip_types():
    if not shutil.which("node"):
        return False
    out = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW).stdout
    return bool(re.match(r"v(2[4-9]|[3-9]\d)\.", out.strip()))


@unittest.skipUnless(node_can_strip_types(), "node >= 24 is required to run the template block directly")
class TemplateHostGuardRunsTests(unittest.TestCase):
    """Run the template's own host-guard block under node (type stripping), not a copy of it."""

    def run_block(self, app_url, staging=""):
        text = TEMPLATE.read_text(encoding="utf-8")
        block = re.search(r"// BEGIN host-guard(.*?)// END host-guard", text, re.S).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "guard.mts"
            script.write_text(
                f"const STAGING_HOSTS: string[] = [{staging}];\n" + block + "\nconsole.log('HOST_OK ' + appHost);\n"
                "console.log('GUARD ' + NAV_GUARD.includes('E2E_HOST_NOT_ALLOWED'));\n",
                encoding="utf-8",
            )
            env = {"PATH": __import__("os").environ["PATH"], "SystemRoot": __import__("os").environ.get("SystemRoot", "")}
            if app_url:
                env["APP_URL"] = app_url
            return subprocess.run(
                ["node", str(script)], capture_output=True, text=True, timeout=60, env=env, creationflags=NO_WINDOW
            )

    def test_local_host_runs(self):
        proc = self.run_block("http://127.0.0.1:3000")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("HOST_OK 127.0.0.1", proc.stdout)
        self.assertIn("GUARD true", proc.stdout)

    def test_default_url_is_local(self):
        proc = self.run_block(None)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_production_host_throws_E2E_HOST_NOT_ALLOWED(self):
        for url in ("https://polysimulator.com", "https://app.polysimulator.com", "https://example.org",
                    "https://POLYSIMULATOR.COM.", "http://localhost@polysimulator.com"):
            proc = self.run_block(url)
            self.assertNotEqual(proc.returncode, 0, url)
            self.assertIn("E2E_HOST_NOT_ALLOWED", proc.stderr, url)

    def test_staging_subdomain_of_the_product_domain_is_allowed_when_listed(self):
        proc = self.run_block("https://staging.polysimulator.com:8443/x", "'staging.polysimulator.com'")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("HOST_OK staging.polysimulator.com", proc.stdout)
        proc = self.run_block("https://app.polysimulator.com", "'staging.polysimulator.com'")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("forbidden-production-host", proc.stderr)

    def refusal(self, url, staging="'staging.polysimulator.com'"):
        text = TEMPLATE.read_text(encoding="utf-8")
        block = re.search(r"// BEGIN host-guard(.*?)// END host-guard", text, re.S).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "req.mts"
            script.write_text(
                f"const STAGING_HOSTS: string[] = [{staging}];\n" + block
                + f"\nconsole.log('REFUSAL ' + String(requestHostRefusal({json.dumps(url)})));\n",
                encoding="utf-8",
            )
            env = {"PATH": os.environ["PATH"], "SystemRoot": os.environ.get("SystemRoot", "")}
            proc = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60, env=env, creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip().split("REFUSAL ")[-1]

    def test_request_level_refusal_for_every_subresource_kind(self):
        for url in ("https://polysimulator.com/api/x", "wss://app.polysimulator.com/socket",
                    "https://zaraprptkegxqpvnsubu.supabase.co/rest/v1/", "https://akamai-iad-prod.example.net/"):
            self.assertEqual(self.refusal(url), "forbidden-production-host", url)
        for url in ("https://example.org/pixel.png", "http://localhost.evil.test/"):
            self.assertEqual(self.refusal(url), "not-in-allow-list", url)
        self.assertEqual(self.refusal("not a url"), "unparseable-url")

    def test_request_level_allows_listed_hosts_and_inline_schemes(self):
        for url in ("http://127.0.0.1:3000/a", "http://localhost/x", "https://staging.polysimulator.com/api",
                    "data:image/png;base64,AAAA", "blob:http://127.0.0.1/abc", "about:blank"):
            self.assertEqual(self.refusal(url), "null", url)

    def run_request_guard(self, urls):
        """Load the real request-guard template with a fake @e2e-dev/web and a fake browser; return the route calls."""
        text = TEMPLATE.read_text(encoding="utf-8")
        block = re.search(r"// BEGIN host-guard(.*?)// END host-guard", text, re.S).group(1)
        guard = (HERE / "e2e.request-guard.template.ts").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text('{"type":"module"}', encoding="utf-8")
            web = root / "node_modules" / "@e2e-dev" / "web"
            web.mkdir(parents=True)
            (web / "package.json").write_text('{"name":"@e2e-dev/web","type":"module","exports":"./index.js"}', encoding="utf-8")
            (web / "index.js").write_text("export const beforeEach = (fn) => { globalThis.__hook = fn; };\n", encoding="utf-8")
            (root / "e2e.config.ts").write_text("const STAGING_HOSTS: string[] = ['staging.polysimulator.com'];\n" + block, encoding="utf-8")
            (root / "e2e.request-guard.ts").write_text(guard, encoding="utf-8")
            (root / "driver.mts").write_text(
                "import { installRequestGuard } from './e2e.request-guard.ts';\n"
                "installRequestGuard();\n"
                "const calls: string[] = [];\n"
                "const browser = { route: async (pattern: string, handler: (r: unknown) => Promise<void>) => {\n"
                "  calls.push('pattern ' + pattern);\n"
                f"  for (const url of {json.dumps(urls)}) {{\n"
                "    await handler({ request: { url, method: 'GET' },\n"
                "      abort: async () => { calls.push('abort ' + url); },\n"
                "      fallback: async () => { calls.push('fallback ' + url); },\n"
                "      continue: async () => { calls.push('continue ' + url); } });\n"
                "  }\n"
                "} };\n"
                "await (globalThis as any).__hook({ browser });\n"
                "console.log('CALLS ' + JSON.stringify(calls));\n",
                encoding="utf-8",
            )
            env = {"PATH": os.environ["PATH"], "SystemRoot": os.environ.get("SystemRoot", "")}
            proc = subprocess.run(["node", str(root / "driver.mts")], cwd=root, capture_output=True, text=True,
                                  timeout=60, env=env, creationflags=NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.split("CALLS ")[-1])

    def test_request_guard_aborts_refused_urls_and_falls_back_for_allowed_ones(self):
        refused = ["https://example.org/pixel.png", "https://polysimulator.com/api/x", "http://127.0.0.2:4173/frame"]
        allowed = ["http://127.0.0.1:4173/ok.txt", "https://staging.polysimulator.com/api", "data:text/plain,hi"]
        calls = self.run_request_guard(refused + allowed)
        self.assertEqual(calls[0], "pattern **")
        self.assertEqual(calls[1:], [f"abort {u}" for u in refused] + [f"fallback {u}" for u in allowed])
        self.assertFalse([c for c in calls if c.startswith("continue ")])


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        routes = self.server.routes
        status, headers, body = routes.get(self.path, (404, {}, b""))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class RedirectTests(unittest.TestCase):
    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.server.routes = {}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_clean_page_and_same_host_redirect_pass(self):
        self.server.routes = {"/": (302, {"Location": "/home"}, b""), "/home": (200, {}, b"ok")}
        self.assertIsNone(e2e_guard.check_redirects(self.base + "/", ["127.0.0.1"]))

    def test_redirect_to_production_is_refused(self):
        self.server.routes = {"/": (302, {"Location": "https://polysimulator.com/login"}, b"")}
        found = e2e_guard.check_redirects(self.base + "/", ["127.0.0.1"])
        self.assertIn("E2E_HOST_NOT_ALLOWED:forbidden-production-host", found)

    def test_redirect_to_unlisted_host_on_a_later_hop_is_refused(self):
        self.server.routes = {
            "/": (301, {"Location": "/a"}, b""),
            "/a": (307, {"Location": "https://example.org/"}, b""),
        }
        found = e2e_guard.check_redirects(self.base + "/", ["127.0.0.1"])
        self.assertIn("E2E_HOST_NOT_ALLOWED:not-in-allow-list", found)

    def test_redirect_loop_is_refused(self):
        self.server.routes = {"/": (302, {"Location": "/"}, b"")}
        self.assertEqual(e2e_guard.check_redirects(self.base + "/", ["127.0.0.1"]), "E2E_HOST_NOT_ALLOWED:too-many-redirects")

    def test_runner_refuses_before_starting_node(self):
        self.server.routes = {"/": (302, {"Location": "https://polysimulator.com/"}, b"")}
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "node_modules" / "e2e" / "dist" / "cli"
            bin_dir.mkdir(parents=True)
            (bin_dir / "bin.js").write_text("console.log('SHOULD_NOT_RUN');\n", encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = e2e_run.main(["--dir", tmp, "--no-slot", "--app-url", self.base + "/"])
            self.assertEqual(rc, 2)
            self.assertIn("forbidden-production-host", err.getvalue())
            self.assertNotIn("SHOULD_NOT_RUN", out.getvalue())

    def test_served_sha_read_does_not_follow_a_redirect(self):
        self.server.routes = {"/api/version": (302, {"Location": "https://polysimulator.com/api/version"}, b"")}
        with self.assertRaises(urllib.error.HTTPError):
            e2e_receipt.fetch_served_sha(self.base)
        self.server.routes = {"/api/version": (200, {"Content-Type": "application/json"}, json.dumps({"sha": HEAD}).encode())}
        self.assertEqual(e2e_receipt.fetch_served_sha(self.base), HEAD)

    def test_version_reader_prefers_commit_over_semver_and_deployment_id(self):
        payload = {"version": "1.0.0", "commit": HEAD, "deploymentId": "b" * 40, "sha": "c" * 40}
        self.server.routes = {"/api/version": (200, {}, json.dumps(payload).encode())}
        self.assertEqual(e2e_receipt.fetch_served_sha(self.base), HEAD)

    def test_version_reader_rejects_semver_only(self):
        self.server.routes = {"/api/version": (200, {}, json.dumps({"version": "1.0.0"}).encode())}
        with self.assertRaisesRegex(ValueError, "40-hex commit"):
            e2e_receipt.fetch_served_sha(self.base)

    def test_version_reader_keeps_validated_legacy_fields(self):
        for field in ("sha", "served_sha", "version", "git_sha", "commitSha"):
            with self.subTest(field=field):
                payload = {"commit": "invalid", "version": "1.0.0", field: HEAD.upper()}
                self.server.routes = {"/api/version": (200, {}, json.dumps(payload).encode())}
                self.assertEqual(e2e_receipt.fetch_served_sha(self.base), HEAD.upper())

    def test_version_reader_never_uses_deployment_id(self):
        payload = {"version": "1.0.0", "deploymentId": HEAD}
        self.server.routes = {"/api/version": (200, {}, json.dumps(payload).encode())}
        with self.assertRaisesRegex(ValueError, "40-hex commit"):
            e2e_receipt.fetch_served_sha(self.base)

    def test_served_sha_read_refuses_a_host_outside_the_allow_list(self):
        with self.assertRaises(ValueError):
            e2e_receipt.fetch_served_sha("https://polysimulator.com")

    def test_replay_is_required_by_default(self):
        vp = ["390x844", "1440x900"]
        dirty = make_report(model_calls=2)
        self.assertIn("replay_not_clean", e2e_receipt.evaluate(dirty, HEAD, HEAD, vp)["reasons"])
        missed = make_report(cache_mode="missed")
        self.assertIn("replay_not_clean", e2e_receipt.evaluate(missed, HEAD, HEAD, vp)["reasons"])
        self.assertEqual(e2e_receipt.evaluate(make_report(), HEAD, HEAD, vp)["reasons"], [])

    def test_allow_model_calls_opts_out_of_the_replay_check(self):
        vp = ["390x844", "1440x900"]
        ev = e2e_receipt.evaluate(make_report(model_calls=2), HEAD, HEAD, vp, require_replay=False)
        self.assertEqual(ev["reasons"], [])

    def test_cli_replay_default_and_opt_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            path.write_text(json.dumps(make_report(model_calls=3)), encoding="utf-8")
            base = ["--report", str(path), "--expected-sha", HEAD, "--served-sha", HEAD]
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(e2e_receipt.main(base), 1)
            self.assertIn("FLOW-QA-REASON replay_not_clean", out.getvalue())
            self.assertRegex(out.getvalue(), r"model_calls=[1-9]")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(e2e_receipt.main(base + ["--allow-model-calls"]), 0)
                self.assertEqual(e2e_receipt.main(base + ["--require-replay"]), 1)

    def test_opt_out_is_visible_in_the_receipt_and_default_is_not_marked(self):
        vp = ["390x844", "1440x900"]
        skipped = e2e_receipt.render(
            e2e_receipt.evaluate(make_report(model_calls=2), HEAD, HEAD, vp, require_replay=False), HEAD)
        self.assertIn("FLOW-QA: PASS " + HEAD, skipped)
        self.assertIn("E2E-REPLAY-CHECK skipped (--allow-model-calls)", skipped)
        clean = e2e_receipt.render(e2e_receipt.evaluate(make_report(), HEAD, HEAD, vp), HEAD)
        self.assertNotIn("E2E-REPLAY-CHECK", clean)

    def test_timeout_kills_the_whole_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "node_modules" / "e2e" / "dist" / "cli"
            bin_dir.mkdir(parents=True)
            (bin_dir / "bin.js").write_text("setInterval(() => {}, 1000);\n", encoding="utf-8")
            killed = []
            with mock.patch.object(e2e_run, "kill_tree", side_effect=lambda pid: (killed.append(pid), os.kill(pid, signal.SIGTERM))), \
                    mock.patch.object(e2e_run, "disable_telemetry"), redirect_stderr(io.StringIO()):
                rc = e2e_run.main(["--dir", tmp, "--no-slot", "--timeout", "-59", "--app-url", self.base + "/"])
            self.assertEqual(rc, 124)
            self.assertEqual(len(killed), 1)

    def test_runner_disables_telemetry_before_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "node_modules" / "e2e" / "dist" / "cli"
            bin_dir.mkdir(parents=True)
            (bin_dir / "bin.js").write_text("require('fs').appendFileSync('calls.log', process.argv.slice(2).join(' ') + '\\n');\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                rc = e2e_run.main(["--dir", tmp, "--no-slot", "--app-url", self.base + "/"])
            self.assertEqual(rc, 0)
            self.assertEqual((Path(tmp) / "calls.log").read_text().split("\n")[:2], ["telemetry disable", "run"])


class RunnerTests(unittest.TestCase):
    def test_replay_env_has_no_key_and_read_only_cache(self):
        env = e2e_run.build_env(False, {"E2E_MODEL_API_KEY": "leak", "E2E_OAUTH_CREDENTIALS": "leak", "A": "1"}, "k")
        self.assertNotIn("E2E_MODEL_API_KEY", env)
        self.assertNotIn("E2E_OAUTH_CREDENTIALS", env)
        self.assertEqual(env["E2E_CACHE_MODE"], "read-only")
        self.assertEqual(env["E2E_TELEMETRY_DISABLED"], "1")
        self.assertEqual(env["DO_NOT_TRACK"], "1")
        self.assertEqual(env["A"], "1")

    def test_record_env_carries_the_key_and_read_write(self):
        env = e2e_run.build_env(True, {}, "secret-key-value")
        self.assertEqual(env["E2E_MODEL_API_KEY"], "secret-key-value")
        self.assertEqual(env["E2E_CACHE_MODE"], "read-write")
        self.assertEqual(env["E2E_TELEMETRY_DISABLED"], "1")

    def test_key_reads_from_a_read_only_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "agent.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE auth_credentials (provider TEXT, credential_type TEXT, data TEXT, disabled_cause TEXT)")
            con.execute("INSERT INTO auth_credentials VALUES ('opencode-go','api_key','{\"key\":\"K-1\"}',NULL)")
            con.execute("INSERT INTO auth_credentials VALUES ('deepseek','api_key','{\"key\":\"K-2\"}',NULL)")
            con.commit()
            con.close()
            self.assertEqual(e2e_run.read_model_key(db), "K-1")
            self.assertIsNone(e2e_run.read_model_key(Path(tmp) / "missing.db"))

    def make_project(self, tmp):
        bin_dir = Path(tmp) / "node_modules" / "e2e" / "dist" / "cli"
        bin_dir.mkdir(parents=True)
        (bin_dir / "bin.js").write_text(
            "console.log(JSON.stringify({t:process.env.E2E_TELEMETRY_DISABLED,"
            "k:process.env.E2E_MODEL_API_KEY||null,c:process.env.E2E_CACHE_MODE,"
            "u:process.env.APP_URL||null,a:process.argv.slice(2)}));\n",
            encoding="utf-8",
        )

    @unittest.skipUnless(shutil.which("node"), "node is required")
    def test_wrapper_runs_replay_without_a_key_and_masks_the_key_in_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_project(tmp)
            out = io.StringIO()
            with mock.patch.dict("os.environ", {"E2E_MODEL_API_KEY": "ambient", "E2E_OAUTH_CREDENTIALS": "x"}), redirect_stdout(out):
                rc = e2e_run.main(["--dir", tmp, "--no-slot", "--app-url", "http://127.0.0.1:1", "--", "tests/a.e2e.ts"])
            self.assertEqual(rc, 0)
            seen = json.loads(out.getvalue())
            self.assertEqual(seen, {"t": "1", "k": None, "c": "read-only", "u": "http://127.0.0.1:1", "a": ["run", "tests/a.e2e.ts"]})

            out = io.StringIO()
            with mock.patch.object(e2e_run, "read_model_key", return_value="topsecret"), redirect_stdout(out):
                rc = e2e_run.main(["--dir", tmp, "--no-slot", "--record"])
            self.assertEqual(rc, 0)
            self.assertNotIn("topsecret", out.getvalue())
            self.assertIn("***", out.getvalue())
            self.assertIn('"c":"read-write"', out.getvalue())

    def test_record_without_a_key_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_project(tmp)
            err = io.StringIO()
            with mock.patch.object(e2e_run, "read_model_key", return_value=None), redirect_stderr(err):
                self.assertEqual(e2e_run.main(["--dir", tmp, "--no-slot", "--record"]), 2)

    def test_missing_install_refuses(self):
        with tempfile.TemporaryDirectory() as tmp, redirect_stderr(io.StringIO()):
            self.assertEqual(e2e_run.main(["--dir", tmp, "--no-slot"]), 2)


if __name__ == "__main__":
    unittest.main()
