#!/usr/bin/env python3
"""
test_worker_backend.py - Contract tests for the real worker execution backend.

Each test defends a fail-closed rule that, if it broke, would let a step advance
a request without having proven anything. That is the exact regression this
module exists to prevent, so the tests are written against observable outcomes
(ok, blocked_reason, head_sha, artifacts) and never against source text.

The backend under test shells out to real commands. To keep these tests
deterministic, hermetic and free of model calls, most of them configure a
*custom backend* whose argv is a short python script. That is not a mock of the
backend: it is the module's documented user-configuration path, exercised for
real, with a real subprocess, a real exit code and a real structured result.
The live agent-CLI path is proven separately by an end-to-end run, which is
recorded in PORTABLE.md.
"""

from __future__ import annotations

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

from worker_backend import (  # noqa: E402
    BackendSpec,
    WorkerBackend,
    WorkerRequest,
    agent_result_schema,
    load_backend_config,
)

PY = sys.executable


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, shell=False,
    )


def _make_repo(root: str) -> str:
    """A real, isolated git repository with one commit."""
    os.makedirs(root, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@localhost")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    with open(os.path.join(root, "seed.txt"), "w", encoding="utf-8") as fh:
        fh.write("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return _git(root, "rev-parse", "HEAD").stdout.strip()


class _Fixture(unittest.TestCase):
    """Shared temp repo, plus a helper to declare scripted custom backends."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wb_test_")
        self.repo = os.path.join(self.tmp, "repo")
        self.head = _make_repo(self.repo)
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _script_backend(self, body: str, name: str = "scripted", **spec_extra):
        """
        Declare a custom backend whose argv runs `body` as a real python program.

        The script receives the rendered prompt as argv[1] and may print a
        structured result to stdout, write files, commit, or exit non-zero.
        """
        path = os.path.join(self.tmp, f"{name}.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        spec = {
            "argv": [PY, path, "{prompt}", "{request_id}", "{stage}", "{repo_root}"],
            "result_source": "stdout_json",
            "stdout_result_keys": ["structured_output"],
            "schema_mode": "none",
        }
        spec.update(spec_extra)
        return {"default_backend": name, "backends": {name: spec}}

    def _backend(self, config):
        return WorkerBackend(config=config, state_dir=self.state)

    def _request(self, **over):
        base = dict(
            request_id="req-test",
            stage="qa",
            repo_root=self.repo,
            head_sha=self.head,
            model="test-model",
            agent_role="worker",
            prompt="verify the seed file",
        )
        base.update(over)
        return WorkerRequest(**base)


# ---------------------------------------------------------------------------
# Request and configuration validation
# ---------------------------------------------------------------------------

class TestRequestValidation(_Fixture):

    def test_malformed_request_blocks_instead_of_raising(self):
        """A caller's malformed packet must not crash the step loop."""
        backend = self._backend(self._script_backend("import sys\n"))
        for bad, needle in (
            (WorkerRequest(request_id="", stage="qa", repo_root=self.repo), "request_id"),
            (WorkerRequest(request_id="r", stage="deploy", repo_root=self.repo), "stage"),
            (WorkerRequest(request_id="r", stage="qa", repo_root=""), "repo_root"),
        ):
            out = backend.execute(bad)
            self.assertFalse(out.ok)
            self.assertIn(needle, out.blocked_reason)

    def test_request_accepted_as_plain_mapping(self):
        """
        The adapter builds its own dict rather than importing this module, so a
        mapping must be accepted exactly like the dataclass.
        """
        backend = self._backend(self._script_backend("import sys\n"))
        out = backend.execute({"request_id": "", "stage": "qa", "repo_root": self.repo})
        self.assertFalse(out.ok)
        self.assertIn("request_id", out.blocked_reason)

    def test_short_head_sha_is_refused(self):
        """Evidence must bind to a full sha; an abbreviation cannot be compared safely."""
        backend = self._backend(self._script_backend("import sys\n"))
        out = backend.execute(self._request(head_sha=self.head[:12]))
        self.assertFalse(out.ok)
        self.assertIn("40-character", out.blocked_reason)

    def test_nonexistent_repo_root_blocks(self):
        backend = self._backend(self._script_backend("import sys\n"))
        out = backend.execute(self._request(repo_root=os.path.join(self.tmp, "nope")))
        self.assertFalse(out.ok)
        self.assertIn("does not exist", out.blocked_reason)

    def test_existing_non_git_directory_blocks_before_sentinel_worker_launch(self):
        """The original invalid-repo reproduction must never reach the worker process."""
        invalid_repo = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(invalid_repo)
        sentinel = os.path.join(self.tmp, "worker-was-launched")
        body = (
            "from pathlib import Path\n"
            f"Path({sentinel!r}).write_text('launched', encoding='utf-8')\n"
        )
        backend = self._backend(self._script_backend(body))
        out = backend.execute(self._request(repo_root=invalid_repo))
        self.assertFalse(out.ok)
        self.assertIn("not a git repository", out.blocked_reason)
        self.assertFalse(os.path.exists(sentinel), "invalid target launched the worker")
        self.assertEqual(out.command, [])
        self.assertFalse(os.path.exists(os.path.join(self.state, "worker_runs")))

    def test_missing_expected_head_blocks_before_worker_launch(self):
        sentinel = os.path.join(self.tmp, "worker-was-launched")
        body = (
            "from pathlib import Path\n"
            f"Path({sentinel!r}).write_text('launched', encoding='utf-8')\n"
        )
        backend = self._backend(self._script_backend(body))
        out = backend.execute(self._request(head_sha=None))
        self.assertFalse(out.ok)
        self.assertIn("'head_sha' is required", out.blocked_reason)
        self.assertFalse(os.path.exists(sentinel))

    def test_unresolvable_expected_head_blocks_before_worker_launch(self):
        sentinel = os.path.join(self.tmp, "worker-was-launched")
        body = (
            "from pathlib import Path\n"
            f"Path({sentinel!r}).write_text('launched', encoding='utf-8')\n"
        )
        backend = self._backend(self._script_backend(body))
        out = backend.execute(self._request(head_sha="f" * 40))
        self.assertFalse(out.ok)
        self.assertIn("does not resolve to a commit", out.blocked_reason)
        self.assertFalse(os.path.exists(sentinel))

    def test_unknown_backend_names_the_configured_ones(self):
        backend = self._backend(self._script_backend("import sys\n"))
        out = backend.execute(self._request(backend="not-a-backend"))
        self.assertFalse(out.ok)
        self.assertIn("not-a-backend", out.blocked_reason)
        self.assertIn("Configured backends", out.blocked_reason)

    def test_missing_executable_blocks_and_says_so(self):
        """A backend whose command is absent must block, never fall back."""
        backend = self._backend({
            "default_backend": "ghost",
            "backends": {"ghost": {"argv": ["definitely-not-installed-xyz", "{prompt}"]}},
        })
        out = backend.execute(self._request())
        self.assertFalse(out.ok)
        self.assertIn("not found on PATH", out.blocked_reason)
        self.assertIn("no fixture fallback", out.blocked_reason)


class TestConfiguration(_Fixture):

    def test_user_backends_merge_over_defaults(self):
        """Declaring one harness must not hide the built-in ones."""
        cfg = load_backend_config(config={"backends": {"mine": {"argv": ["x"]}}})
        self.assertIn("mine", cfg["backends"])
        for builtin in ("claude", "codex", "veyyon"):
            self.assertIn(builtin, cfg["backends"])

    def test_stage_backend_override_selects_per_stage(self):
        cfg = {
            "default_backend": "a",
            "stage_backends": {"review": "b"},
            "backends": {"a": {"argv": ["x"]}, "b": {"argv": ["y"]}},
        }
        backend = WorkerBackend(config=cfg, state_dir=self.state)
        self.assertEqual(backend.resolve_backend("build")[0].name, "a")
        self.assertEqual(backend.resolve_backend("review")[0].name, "b")

    def test_env_var_config_is_honoured(self):
        path = os.path.join(self.tmp, "wb.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"default_backend": "fromenv",
                       "backends": {"fromenv": {"argv": ["x"]}}}, fh)
        old = os.environ.get("PORTABLE_WORKER_CONFIG")
        os.environ["PORTABLE_WORKER_CONFIG"] = path
        try:
            cfg = load_backend_config()
            self.assertEqual(cfg["default_backend"], "fromenv")
            self.assertIn("PORTABLE_WORKER_CONFIG", cfg["source"])
        finally:
            if old is None:
                os.environ.pop("PORTABLE_WORKER_CONFIG", None)
            else:
                os.environ["PORTABLE_WORKER_CONFIG"] = old

    def test_project_config_metadata_is_an_extension_point(self):
        """A harness can be declared through the project config without new plumbing."""
        class FakeProjectConfig:
            metadata = {"portable_worker": {"default_backend": "viameta",
                                            "backends": {"viameta": {"argv": ["x"]}}}}
        cfg = load_backend_config(project_config=FakeProjectConfig())
        self.assertEqual(cfg["default_backend"], "viameta")
        self.assertEqual(cfg["source"], "project-config-metadata")

    def test_empty_argv_is_rejected_at_spec_build(self):
        with self.assertRaises(ValueError):
            BackendSpec.from_dict("bad", {"argv": []})

    def test_bad_result_source_is_rejected(self):
        with self.assertRaises(ValueError):
            BackendSpec.from_dict("bad", {"argv": ["x"], "result_source": "telepathy"})

    def test_prompt_travels_as_one_argv_token(self):
        """
        Whole-token substitution is the reason shell=False is safe here: a prompt
        containing quotes, spaces and newlines must not become extra arguments.
        """
        backend = self._backend({
            "default_backend": "echoish",
            "backends": {"echoish": {"argv": ["cmd", "--p", "{prompt}", "--tail"]}},
        })
        spec, _ = backend.resolve_backend("qa")
        argv = backend._substitute(spec.argv, {"prompt": 'a b "c"\nd; rm -rf /'})
        self.assertEqual(len(argv), 4)
        self.assertEqual(argv[2], 'a b "c"\nd; rm -rf /')
        self.assertEqual(argv[3], "--tail")

    def test_non_strict_unmapped_model_passes_through_without_substitution(self):
        backend = self._backend(self._script_backend(
            "import sys\n", strict_model=False, model_default="quiet-default",
        ))
        spec, err = backend.resolve_backend("qa")
        self.assertIsNone(err)
        requested = "vendor/model:high"
        resolved, note, model_err = backend.resolve_model(spec, requested)
        self.assertIsNone(model_err)
        self.assertEqual(resolved, requested)
        self.assertIn("passed through unchanged", note)
        self.assertNotIn("quiet-default", note)

    def test_explicit_mapping_is_retained_for_non_strict_backend(self):
        backend = self._backend(self._script_backend(
            "import sys\n",
            strict_model=False,
            model_default="quiet-default",
            model_map={"vendor/model:high": "exact-cli-name"},
        ))
        spec, err = backend.resolve_backend("qa")
        self.assertIsNone(err)
        resolved, note, model_err = backend.resolve_model(spec, "vendor/model:high")
        self.assertIsNone(model_err)
        self.assertEqual(resolved, "exact-cli-name")
        self.assertIn("mapped to 'exact-cli-name'", note)


# ---------------------------------------------------------------------------
# Execution result validation: the fail-closed core
# ---------------------------------------------------------------------------

PASS_RESULT = """
import json, subprocess, sys
prompt, request_id, stage, repo_root = sys.argv[1:5]
head = subprocess.run(["git","rev-parse","HEAD"], cwd=repo_root,
                      capture_output=True, text=True).stdout.strip()
print(json.dumps({"structured_output": {
    "stage": stage,
    "request_id": request_id,
    "head_sha": head,
    "verdict": "pass",
    "summary": "verified",
    "checks": [{"name": "read seed", "command": ["cat", "seed.txt"],
                "exit_code": 0, "observed": "seed"}],
    "artifacts": [],
}}))
"""


class TestExitStatusIsNotEvidence(_Fixture):

    def test_nonzero_exit_blocks(self):
        backend = self._backend(self._script_backend(
            "import sys\nprint('{}')\nsys.exit(3)\n"))
        out = backend.execute(self._request())
        self.assertFalse(out.ok)
        self.assertEqual(out.exit_code, 3)
        self.assertIn("exited 3", out.blocked_reason)

    def test_exit_zero_with_no_output_blocks(self):
        """The precise hole this module closes: success status, zero evidence."""
        backend = self._backend(self._script_backend("import sys\n"))
        out = backend.execute(self._request())
        self.assertFalse(out.ok)
        self.assertIn("no parseable JSON", out.blocked_reason)

    def test_exit_zero_with_non_result_json_blocks(self):
        backend = self._backend(self._script_backend(
            "import json\nprint(json.dumps({'hello': 'world'}))\n"))
        out = backend.execute(self._request())
        self.assertFalse(out.ok)
        self.assertIn("not a structured worker", out.blocked_reason)

    def test_timeout_blocks(self):
        backend = self._backend(self._script_backend(
            "import time\ntime.sleep(30)\n", timeout_seconds=1))
        out = backend.execute(self._request())
        self.assertFalse(out.ok)
        self.assertIn("timeout", out.blocked_reason)

    def test_is_error_envelope_blocks_despite_exit_zero(self):
        """Real CLIs report failure in the envelope while still exiting 0."""
        backend = self._backend(self._script_backend(
            "import json\nprint(json.dumps({'is_error': True, 'subtype': 'error_max_turns'}))\n"))
        out = backend.execute(self._request())
        self.assertFalse(out.ok)
        self.assertIn("is_error", out.blocked_reason)

    def test_log_noise_before_json_is_tolerated(self):
        """
        Real CLIs interleave hook warnings and progress lines with their JSON.
        The envelope must still be recovered - and it must be the envelope, not
        some nested object that happens to parse on its own.
        """
        backend = self._backend(self._script_backend(
            "print('WARN: some hook failed')\n"
            "print('{partial brace noise')\n" + PASS_RESULT))
        out = backend.execute(self._request())
        self.assertTrue(out.ok, out.blocked_reason)
        self.assertEqual(out.evidence["summary"], "verified")


class TestResultContract(_Fixture):

    def _run_with_result(self, result_obj, **req_over):
        # The result is embedded as a JSON *string* and parsed inside the script.
        # Interpolating json.dumps output straight into python source breaks the
        # moment the payload contains a boolean, because JSON true/false are not
        # python literals.
        body = (
            "import json\n"
            "print(json.dumps({'structured_output': json.loads(%r)}))\n"
            % json.dumps(result_obj)
        )
        backend = self._backend(self._script_backend(body))
        return backend.execute(self._request(**req_over))

    def _good(self, **over):
        base = {
            "stage": "qa",
            "request_id": "req-test",
            "head_sha": self.head,
            "verdict": "pass",
            "summary": "ok",
            "checks": [{"name": "c", "command": ["cat", "seed.txt"],
                        "exit_code": 0, "observed": "seed"}],
            "artifacts": [],
        }
        base.update(over)
        return base

    def test_wellformed_pass_is_accepted(self):
        out = self._run_with_result(self._good())
        self.assertTrue(out.ok, out.blocked_reason)
        self.assertEqual(out.head_sha, self.head)
        self.assertEqual(out.evidence["verdict"], "pass")

    def test_each_missing_required_field_blocks(self):
        for key in ("stage", "request_id", "head_sha", "verdict", "summary", "checks", "artifacts"):
            payload = self._good()
            payload.pop(key)
            out = self._run_with_result(payload)
            self.assertFalse(out.ok, f"missing {key} was accepted")
            self.assertIn("missing required field", out.blocked_reason)

    def test_pass_with_no_checks_blocks(self):
        """A pass verdict backed by nothing executed is the core refusal."""
        out = self._run_with_result(self._good(checks=[]))
        self.assertFalse(out.ok)
        self.assertIn("no executed checks", out.blocked_reason)

    def test_check_without_command_blocks(self):
        out = self._run_with_result(self._good(
            checks=[{"name": "c", "command": [], "exit_code": 0, "observed": "x"}]))
        self.assertFalse(out.ok)
        self.assertIn("no executed command", out.blocked_reason)

    def test_check_without_exit_code_blocks(self):
        out = self._run_with_result(self._good(
            checks=[{"name": "c", "command": ["cat"], "exit_code": "0", "observed": "x"}]))
        self.assertFalse(out.ok)
        self.assertIn("integer exit_code", out.blocked_reason)

    def test_check_without_observation_blocks(self):
        out = self._run_with_result(self._good(
            checks=[{"name": "c", "command": ["cat"], "exit_code": 0, "observed": "   "}]))
        self.assertFalse(out.ok)
        self.assertIn("nothing observed", out.blocked_reason)

    def test_unknown_verdict_blocks(self):
        out = self._run_with_result(self._good(verdict="probably-fine"))
        self.assertFalse(out.ok)
        self.assertIn("not one of", out.blocked_reason)

    def test_fail_and_blocked_verdicts_do_not_advance(self):
        """An honest negative result is reported, not converted into progress."""
        for verdict in ("fail", "blocked"):
            out = self._run_with_result(self._good(verdict=verdict, summary="did not hold"))
            self.assertFalse(out.ok)
            self.assertIn(verdict, out.blocked_reason)
            self.assertIn("did not hold", out.blocked_reason)

    def test_result_for_another_stage_blocks(self):
        out = self._run_with_result(self._good(stage="build"), stage="qa")
        self.assertFalse(out.ok)
        self.assertIn("wrong stage", out.blocked_reason)

    def test_result_for_another_request_blocks(self):
        out = self._run_with_result(self._good(request_id="req-somebody-else"))
        self.assertFalse(out.ok)
        self.assertIn("req-somebody-else", out.blocked_reason)


class TestHeadBinding(_Fixture):

    def _run(self, result_obj, **req_over):
        body = ("import json\n"
                "print(json.dumps({'structured_output': json.loads(%r)}))\n"
                % json.dumps(result_obj))
        backend = self._backend(self._script_backend(body))
        return backend.execute(self._request(**req_over))

    def _good(self, **over):
        base = {
            "stage": "qa", "request_id": "req-test", "head_sha": self.head,
            "verdict": "pass", "summary": "ok",
            "checks": [{"name": "c", "command": ["cat", "seed.txt"],
                        "exit_code": 0, "observed": "seed"}],
            "artifacts": [],
        }
        base.update(over)
        return base

    def test_claimed_head_must_match_observed(self):
        """A worker cannot name a commit it did not run against."""
        fake = "0" * 40
        out = self._run(self._good(head_sha=fake))
        self.assertFalse(out.ok)
        self.assertIn("observed HEAD", out.blocked_reason)
        self.assertEqual(out.head_sha, self.head, "reported head must be the observed one")

    def test_non_sha_claim_blocks(self):
        out = self._run(self._good(head_sha="HEAD"))
        self.assertFalse(out.ok)
        self.assertIn("40-character", out.blocked_reason)

    def test_wrong_checkout_refused_before_execution(self):
        """A stage bound to a commit the tree is not on never runs at all."""
        other = "1" * 40
        backend = self._backend(self._script_backend(PASS_RESULT))
        out = backend.execute(self._request(head_sha=other))
        self.assertFalse(out.ok)
        self.assertIn("before execution", out.blocked_reason)
        self.assertIsNone(out.exit_code, "the command must not have been executed")

    def test_verification_stage_that_moves_head_is_invalid(self):
        """QA must not mutate the tree it is judging; if it does, discard the result."""
        mover = """
import json, subprocess, sys
prompt, request_id, stage, repo_root = sys.argv[1:5]
open(repo_root + "/sneaky.txt", "w").write("x\\n")
subprocess.run(["git","add","-A"], cwd=repo_root, capture_output=True)
subprocess.run(["git","commit","-q","-m","sneaky"], cwd=repo_root, capture_output=True)
head = subprocess.run(["git","rev-parse","HEAD"], cwd=repo_root,
                      capture_output=True, text=True).stdout.strip()
print(json.dumps({"structured_output": {
    "stage": stage, "request_id": request_id, "head_sha": head,
    "verdict": "pass", "summary": "snuck a commit in",
    "checks": [{"name": "c", "command": ["cat","seed.txt"], "exit_code": 0, "observed": "seed"}],
    "artifacts": []}}))
"""
        backend = self._backend(self._script_backend(mover, name="mover"))
        out = backend.execute(self._request(stage="qa"))
        self.assertFalse(out.ok)
        self.assertIn("HEAD moved", out.blocked_reason)


class TestBuildMustProduceSomething(_Fixture):

    def test_build_with_no_commit_and_no_artifact_blocks(self):
        body = ("import json, subprocess, sys\n"
                "repo_root = sys.argv[4]\n"
                "head = subprocess.run(['git','rev-parse','HEAD'], cwd=repo_root,"
                " capture_output=True, text=True).stdout.strip()\n"
                "print(json.dumps({'structured_output': {"
                "'stage':'build','request_id':'req-test','head_sha':head,"
                "'verdict':'pass','summary':'did nothing',"
                "'checks':[{'name':'c','command':['cat','seed.txt'],'exit_code':0,"
                "'observed':'seed'}],'artifacts':[]}}))\n")
        backend = self._backend(self._script_backend(body, name="noop_build"))
        out = backend.execute(self._request(stage="build"))
        self.assertFalse(out.ok)
        self.assertIn("proven nothing", out.blocked_reason)

    def test_build_that_commits_is_accepted_and_reports_the_new_head(self):
        builder = """
import json, subprocess, sys
prompt, request_id, stage, repo_root = sys.argv[1:5]
open(repo_root + "/feature.txt", "w").write("feature\\n")
subprocess.run(["git","add","-A"], cwd=repo_root, capture_output=True)
subprocess.run(["git","commit","-q","-m","feat"], cwd=repo_root, capture_output=True)
head = subprocess.run(["git","rev-parse","HEAD"], cwd=repo_root,
                      capture_output=True, text=True).stdout.strip()
print(json.dumps({"structured_output": {
    "stage": stage, "request_id": request_id, "head_sha": head,
    "verdict": "pass", "summary": "added feature.txt",
    "checks": [{"name": "verify file", "command": ["cat","feature.txt"],
                "exit_code": 0, "observed": "feature"}],
    "artifacts": [{"path": "feature.txt", "role": "created"}]}}))
"""
        backend = self._backend(self._script_backend(builder, name="builder"))
        out = backend.execute(self._request(stage="build"))
        self.assertTrue(out.ok, out.blocked_reason)
        self.assertNotEqual(out.head_sha, self.head, "build must report the new commit")
        self.assertEqual(out.artifacts, ["feature.txt"])
        self.assertIn("feature.txt", out.evidence["artifact_digests"])
        self.assertEqual(len(out.evidence["artifact_digests"]["feature.txt"]), 64)

    def test_declared_artifact_that_does_not_exist_blocks(self):
        body = ("import json, subprocess, sys\n"
                "repo_root = sys.argv[4]\n"
                "head = subprocess.run(['git','rev-parse','HEAD'], cwd=repo_root,"
                " capture_output=True, text=True).stdout.strip()\n"
                "print(json.dumps({'structured_output': {"
                "'stage':'build','request_id':'req-test','head_sha':head,"
                "'verdict':'pass','summary':'claimed a file',"
                "'checks':[{'name':'c','command':['ls'],'exit_code':0,'observed':'x'}],"
                "'artifacts':[{'path':'imaginary.txt','role':'created'}]}}))\n")
        backend = self._backend(self._script_backend(body, name="liar"))
        out = backend.execute(self._request(stage="build"))
        self.assertFalse(out.ok)
        self.assertIn("imaginary.txt", out.blocked_reason)
        self.assertIn("does not exist", out.blocked_reason)


class TestBugReproductionEvidence(_Fixture):
    """
    A bug is closed by re-running the original failing scenario. A keyword, a
    bare boolean or a passing generic suite is not proof.
    """

    def _run(self, repro, verdict="pass"):
        result = {
            "stage": "qa", "request_id": "req-bug-test", "head_sha": self.head,
            "verdict": verdict, "summary": "checked",
            "checks": [{"name": "suite", "command": ["cat", "seed.txt"],
                        "exit_code": 0, "observed": "seed"}],
            "artifacts": [],
        }
        if repro is not None:
            result["reproduction"] = repro
        body = ("import json\n"
                "print(json.dumps({'structured_output': json.loads(%r)}))\n"
                % json.dumps(result))
        backend = self._backend(self._script_backend(body, name="bugqa"))
        return backend.execute(self._request(request_id="req-bug-test", stage="qa",
                                             task_type="bug"))

    def test_bug_qa_without_reproduction_record_blocks(self):
        out = self._run(None)
        self.assertFalse(out.ok)
        self.assertIn("without a 'reproduction' record", out.blocked_reason)

    def test_keyword_claim_is_not_accepted_as_reproduction(self):
        """'proven absent' in prose must not close a bug."""
        out = self._run({"scenario": "proven absent", "still_reproduces": False,
                         "command": [], "exit_code": 0, "observed": "proven absent"})
        self.assertFalse(out.ok)
        self.assertIn("names no command", out.blocked_reason)

    def test_reproduction_missing_exit_code_blocks(self):
        out = self._run({"scenario": "click buy", "command": ["python", "repro.py"],
                         "observed": "no error", "still_reproduces": False})
        self.assertFalse(out.ok)
        self.assertIn("missing 'exit_code'", out.blocked_reason)

    def test_still_reproducing_bug_blocks(self):
        out = self._run({"scenario": "click buy", "command": ["python", "repro.py"],
                         "exit_code": 1, "observed": "still throws", "still_reproduces": True})
        self.assertFalse(out.ok)
        self.assertIn("still reproduces", out.blocked_reason)

    def test_reexecuted_reproduction_is_accepted_and_verdict_is_derived(self):
        out = self._run({"scenario": "click buy on mobile", "command": ["python", "repro.py"],
                         "exit_code": 0, "observed": "no exception, order accepted",
                         "still_reproduces": False})
        self.assertTrue(out.ok, out.blocked_reason)
        repro = out.evidence["reproduction"]
        self.assertEqual(repro["verdict"], "absent")
        self.assertEqual(repro["derived_by"], "worker_backend._validate_result")

    def test_agent_cannot_supply_absent_verdict_itself(self):
        """
        A worker writing verdict "absent" into its own reproduction block, while
        the scenario still reproduces, must not close the bug.
        """
        out = self._run({"scenario": "click buy", "command": ["python", "repro.py"],
                         "exit_code": 1, "observed": "still throws",
                         "still_reproduces": True, "verdict": "absent"})
        self.assertFalse(out.ok)
        self.assertIn("still reproduces", out.blocked_reason)


class TestFileResultBackend(_Fixture):
    """The codex-style path, where the structured answer is written to a file."""

    def _file_backend(self, body, name="filed"):
        path = os.path.join(self.tmp, f"{name}.py")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return {
            "default_backend": name,
            "backends": {name: {
                "argv": [PY, path, "{result_path}", "{schema_path}", "{request_id}",
                         "{stage}", "{repo_root}"],
                "result_source": "file",
                "result_path_template": "{work_dir}/out.json",
                "schema_mode": "file",
            }},
        }

    def test_file_result_is_read_and_validated(self):
        body = """
import json, subprocess, sys
result_path, schema_path, request_id, stage, repo_root = sys.argv[1:6]
schema = json.load(open(schema_path, encoding="utf-8"))
assert "verdict" in schema["properties"], "schema was not delivered to the worker"
head = subprocess.run(["git","rev-parse","HEAD"], cwd=repo_root,
                      capture_output=True, text=True).stdout.strip()
json.dump({"stage": stage, "request_id": request_id, "head_sha": head,
           "verdict": "pass", "summary": "read from file",
           "checks": [{"name": "c", "command": ["cat","seed.txt"],
                       "exit_code": 0, "observed": "seed"}],
           "artifacts": []}, open(result_path, "w", encoding="utf-8"))
"""
        backend = self._backend(self._file_backend(body))
        out = backend.execute(self._request())
        self.assertTrue(out.ok, out.blocked_reason)
        self.assertEqual(out.evidence["summary"], "read from file")

    def test_missing_result_file_blocks(self):
        backend = self._backend(self._file_backend("import sys\n", name="nofile"))
        out = backend.execute(self._request())
        self.assertFalse(out.ok)
        self.assertIn("wrote no structured result", out.blocked_reason)


class TestDryRunAndSchema(_Fixture):

    def test_dry_run_builds_argv_without_executing(self):
        backend = WorkerBackend(config=self._script_backend(PASS_RESULT),
                                state_dir=self.state, dry_run=True)
        out = backend.execute(self._request())
        self.assertFalse(out.ok, "a dry run has proven nothing and must not be ok")
        self.assertTrue(out.evidence["dry_run"])
        self.assertIsNone(out.exit_code)
        self.assertTrue(out.command)

    def test_schema_requires_the_evidence_fields(self):
        schema = agent_result_schema()
        for key in ("stage", "request_id", "head_sha", "verdict", "summary", "checks", "artifacts"):
            self.assertIn(key, schema["required"])
        self.assertEqual(schema["properties"]["verdict"]["enum"], ["pass", "fail", "blocked"])

    def test_builtin_backends_carry_a_structured_result_flag(self):
        """Every shipped backend must ask its CLI for machine-readable output."""
        from worker_backend import DEFAULT_BACKENDS
        for name, spec in DEFAULT_BACKENDS.items():
            argv = " ".join(spec["argv"])
            self.assertTrue(
                "json" in argv or "schema" in argv,
                f"backend {name} does not request structured output",
            )

    def test_evidence_always_denies_merge_and_deploy(self):
        backend = self._backend(self._script_backend(PASS_RESULT))
        out = backend.execute(self._request())
        self.assertTrue(out.ok, out.blocked_reason)
        self.assertIs(out.evidence["auto_merge_allowed"], False)
        self.assertIs(out.evidence["auto_deploy_allowed"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
