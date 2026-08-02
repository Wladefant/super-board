"""Task 5 — interactive and unattended GitHub identity verification.

Pure stdlib `unittest`. No network, no `gh`, no credential material anywhere in
this file: every "token" below is an obvious non-secret sentinel, and the tests
assert that no code path ever echoes one.

Run directly:
  python -B tests/test_auth.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_auth.py' -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.auth import (  # noqa: E402
    LOGIN_ENV_VAR,
    REQUIRED_SCOPES,
    TOKEN_ENV_VAR,
    AuthReport,
    classify_token,
    verify_github_identity,
)
from super_board_runtime.config import load_and_validate_config  # noqa: E402

# The hyphenated CLI filename cannot be imported normally.
_AUTH_CLI = _SCRIPTS / "super-board-auth.py"
_spec = importlib.util.spec_from_file_location("super_board_auth_cli", _AUTH_CLI)
assert _spec is not None and _spec.loader is not None
auth_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(auth_cli)

# Obvious non-secrets. These are shaped like real tokens only in their prefix.
CLASSIC = "ghp_" + "NOTAREALTOKEN" + "0" * 23
FINE_GRAINED = "github_pat_" + "NOTAREALTOKEN" + "0" * 20
APP_INSTALLATION = "ghs_" + "NOTAREALTOKEN" + "0" * 23
SENTINEL = "ghp_" + "SENTINELVALUEMUSTNEVERBEPRINTED12345"


def _config_payload(**overrides: object) -> dict:
    payload = {
        "version": 1,
        "variant": "full",
        "project": {"owner": "Bavariance", "number": 1},
        "repo": {"remote": "Bavariance/polysimulator"},
        "base_branch": "staging",
        "activation_mode": "off",
        "github_auth": {
            "mode": "unattended",
            "expected_login": "superboard-machine",
            "required_projects": [
                {"name": "superboard-system", "owner": "Wladefant", "number": 5},
                {"name": "master-board", "owner": "Wladefant", "number": 6},
            ],
        },
    }
    payload.update(overrides)
    return payload


def _config(**overrides: object):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(_config_payload(**overrides)), encoding="utf-8")
        return load_and_validate_config(path)


class FakeProbe:
    """Mock GitHub probe. Records what it was asked, never what it was given."""

    def __init__(
        self,
        *,
        login: str | None = "superboard-machine",
        scopes: tuple[str, ...] | None = REQUIRED_SCOPES,
        scope_header_present: bool = True,
        capabilities: dict[str, bool] | None = None,
        fail: bool = False,
    ) -> None:
        self.login = login
        self.scopes = scopes
        self.scope_header_present = scope_header_present
        self.capabilities = capabilities or {}
        self.fail = fail
        self.identity_calls: list[bool] = []  # True when a token was supplied
        self.capability_calls: list[str] = []

    def identity(self, token):
        self.identity_calls.append(token is not None)
        if self.fail:
            return None
        return {
            "login": self.login,
            "scopes": self.scopes,
            "scope_header_present": self.scope_header_present,
        }

    def capability(self, name, target, token):
        self.capability_calls.append(name)
        return self.capabilities.get(name, True)


def _env(**overrides: str) -> dict[str, str]:
    env = {TOKEN_ENV_VAR: CLASSIC, LOGIN_ENV_VAR: "superboard-machine"}
    env.update(overrides)
    return {key: value for key, value in env.items() if value is not None}


class TokenClassTests(unittest.TestCase):
    def test_classification_is_prefix_only_and_never_needs_the_value_logged(self) -> None:
        self.assertEqual(classify_token(CLASSIC), "classic")
        self.assertEqual(classify_token(FINE_GRAINED), "fine-grained")
        self.assertEqual(classify_token(APP_INSTALLATION), "github-app")
        self.assertEqual(classify_token("ghu_" + "x" * 36), "github-app")
        self.assertEqual(classify_token("a" * 40), "classic")
        self.assertEqual(classify_token("nonsense"), "unknown")
        self.assertEqual(classify_token(None), "absent")


class InteractiveModeTests(unittest.TestCase):
    def test_interactive_resolves_the_session_identity_without_any_env_credential(self) -> None:
        probe = FakeProbe(login="LucariusWest")
        report = verify_github_identity(_config(), "interactive", env={}, probe=probe)
        self.assertIsInstance(report, AuthReport)
        self.assertTrue(report.ok, report.reason_code)
        self.assertEqual(report.login, "LucariusWest")
        self.assertEqual(report.token_class, "session")
        self.assertEqual(probe.identity_calls, [False], "no token may be passed in interactive mode")

    def test_interactive_still_requires_capabilities(self) -> None:
        probe = FakeProbe(login="LucariusWest", capabilities={"master-board": False})
        report = verify_github_identity(_config(), "interactive", env={}, probe=probe)
        self.assertFalse(report.ok)
        self.assertEqual(report.reason_code, "capability-missing:master-board")

    def test_an_unresolvable_session_identity_fails_closed(self) -> None:
        report = verify_github_identity(_config(), "interactive", env={}, probe=FakeProbe(fail=True))
        self.assertFalse(report.ok)
        self.assertEqual(report.reason_code, "identity-unavailable")


class UnattendedModeTests(unittest.TestCase):
    def test_the_token_and_login_come_only_from_the_named_environment_variables(self) -> None:
        self.assertEqual(TOKEN_ENV_VAR, "SUPERBOARD_GITHUB_TOKEN")
        self.assertEqual(LOGIN_ENV_VAR, "SUPERBOARD_GITHUB_LOGIN")
        probe = FakeProbe()
        report = verify_github_identity(_config(), "unattended", env=_env(), probe=probe)
        self.assertTrue(report.ok, report.reason_code)
        self.assertEqual(report.login, "superboard-machine")
        self.assertEqual(report.token_class, "classic")
        self.assertEqual(probe.identity_calls, [True])

        # Any other variable is invisible to the runtime.
        stray = {"GH_TOKEN": CLASSIC, "GITHUB_TOKEN": CLASSIC, LOGIN_ENV_VAR: "superboard-machine"}
        report = verify_github_identity(_config(), "unattended", env=stray, probe=FakeProbe())
        self.assertFalse(report.ok)
        self.assertEqual(report.reason_code, "token-env-missing")

    def test_a_missing_expected_login_fails_closed(self) -> None:
        env = {TOKEN_ENV_VAR: CLASSIC}
        report = verify_github_identity(
            _config(github_auth={"mode": "unattended"}), "unattended", env=env, probe=FakeProbe()
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.reason_code, "expected-login-missing")

    def test_a_login_mismatch_is_identity_mismatch(self) -> None:
        probe = FakeProbe(login="someone-else")
        report = verify_github_identity(_config(), "unattended", env=_env(), probe=probe)
        self.assertFalse(report.ok)
        self.assertEqual(report.reason_code, "identity-mismatch")
        self.assertEqual(report.login, "someone-else")

    def test_the_env_login_overrides_the_configured_expected_login(self) -> None:
        probe = FakeProbe(login="other-machine")
        report = verify_github_identity(
            _config(), "unattended", env=_env(**{LOGIN_ENV_VAR: "other-machine"}), probe=probe
        )
        self.assertTrue(report.ok, report.reason_code)


class TokenClassRejectionTests(unittest.TestCase):
    def test_a_fine_grained_token_is_rejected_before_any_scan_or_mutation(self) -> None:
        probe = FakeProbe()
        report = verify_github_identity(
            _config(), "unattended", env=_env(**{TOKEN_ENV_VAR: FINE_GRAINED}), probe=probe
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.reason_code, "token-class-not-classic")
        self.assertEqual(report.token_class, "fine-grained")
        self.assertEqual(probe.identity_calls, [], "class is decided before anything is probed")
        self.assertEqual(probe.capability_calls, [])

    def test_a_github_app_token_is_rejected_because_apps_cannot_reach_personal_projects(self) -> None:
        probe = FakeProbe()
        report = verify_github_identity(
            _config(), "unattended", env=_env(**{TOKEN_ENV_VAR: APP_INSTALLATION}), probe=probe
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.reason_code, "token-class-not-classic")
        self.assertEqual(report.token_class, "github-app")
        self.assertEqual(probe.identity_calls, [])
        self.assertIn("cannot access personal Projects v2", auth_cli.explain(report))

    def test_an_absent_scope_header_is_scope_ambiguous_and_capability_probing_cannot_rescue_it(self) -> None:
        probe = FakeProbe(scopes=None, scope_header_present=False)
        report = verify_github_identity(_config(), "unattended", env=_env(), probe=probe)
        self.assertFalse(report.ok)
        self.assertEqual(report.reason_code, "scope-ambiguous")
        self.assertEqual(probe.capability_calls, [], "no capability probe may run after scope-ambiguous")

    def test_a_present_but_unparseable_scope_header_is_scope_ambiguous(self) -> None:
        probe = FakeProbe(scopes=None, scope_header_present=True)
        report = verify_github_identity(_config(), "unattended", env=_env(), probe=probe)
        self.assertFalse(report.ok)
        self.assertEqual(report.reason_code, "scope-ambiguous")

    def test_each_missing_required_scope_is_insufficient_scope(self) -> None:
        for scopes in (("repo", "project"), ("repo", "read:org"), ("project", "read:org"), ()):
            with self.subTest(scopes=scopes):
                probe = FakeProbe(scopes=scopes)
                report = verify_github_identity(_config(), "unattended", env=_env(), probe=probe)
                self.assertFalse(report.ok)
                self.assertEqual(report.reason_code, "insufficient-scope")
                self.assertEqual(probe.capability_calls, [])


class CapabilityTests(unittest.TestCase):
    def test_every_capability_is_checked(self) -> None:
        probe = FakeProbe()
        report = verify_github_identity(_config(), "unattended", env=_env(), probe=probe)
        self.assertTrue(report.ok, report.reason_code)
        self.assertEqual(
            sorted(report.capabilities),
            ["master-board", "project", "repository", "superboard-system"],
        )
        self.assertTrue(all(report.capabilities.values()))

    def test_each_missing_capability_names_itself(self) -> None:
        for name in ("repository", "project", "superboard-system", "master-board"):
            with self.subTest(capability=name):
                probe = FakeProbe(capabilities={name: False})
                report = verify_github_identity(_config(), "unattended", env=_env(), probe=probe)
                self.assertFalse(report.ok)
                self.assertEqual(report.reason_code, f"capability-missing:{name}")
                self.assertIs(report.capabilities[name], False)

    def test_a_capability_probe_that_raises_is_treated_as_missing(self) -> None:
        class ExplodingProbe(FakeProbe):
            def capability(self, name, target, token):
                self.capability_calls.append(name)
                raise RuntimeError("gh exited 1")

        report = verify_github_identity(_config(), "unattended", env=_env(), probe=ExplodingProbe())
        self.assertFalse(report.ok)
        self.assertTrue(report.reason_code.startswith("capability-missing:"))


class RedactionTests(unittest.TestCase):
    def test_no_code_path_prints_the_token_the_environment_or_the_command_line(self) -> None:
        config_payload = json.dumps(_config_payload())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(config_payload, encoding="utf-8")
            env = {TOKEN_ENV_VAR: SENTINEL, LOGIN_ENV_VAR: "superboard-machine"}
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = auth_cli.main(
                    ["preflight", "--config", str(path), "--mode", "unattended", "--json"],
                    env=env,
                    probe=FakeProbe(login="someone-else"),
                )
        combined = out.getvalue() + err.getvalue()
        self.assertEqual(code, 69)
        self.assertNotIn(SENTINEL, combined)
        self.assertNotIn("SENTINELVALUE", combined)
        self.assertNotIn(TOKEN_ENV_VAR + "=", combined)
        self.assertIn("identity-mismatch", combined)
        # The variable NAME is fine — that is how an operator fixes it.
        self.assertIn(LOGIN_ENV_VAR, combined)

    def test_the_report_object_itself_carries_no_token_material(self) -> None:
        report = verify_github_identity(
            _config(), "unattended", env=_env(**{TOKEN_ENV_VAR: SENTINEL}), probe=FakeProbe()
        )
        self.assertNotIn(SENTINEL, json.dumps(report.to_dict()))
        self.assertNotIn(SENTINEL, repr(report))


class AuthCliTests(unittest.TestCase):
    def test_preflight_succeeds_and_prints_sorted_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(_config_payload()), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = auth_cli.main(
                    ["preflight", "--config", str(path), "--mode", "unattended", "--json"],
                    env=_env(),
                    probe=FakeProbe(),
                )
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["login"], "superboard-machine")
        self.assertEqual(list(payload), sorted(payload))

    def test_a_fine_grained_token_exits_69_end_to_end_without_echoing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(_config_payload()), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(_AUTH_CLI),
                    "preflight",
                    "--config",
                    str(path),
                    "--mode",
                    "unattended",
                    "--json",
                ],
                capture_output=True,
                text=True,
                env={
                    "PATH": "",
                    "SYSTEMROOT": "",
                    TOKEN_ENV_VAR: FINE_GRAINED,
                    LOGIN_ENV_VAR: "superboard-machine",
                },
            )
        self.assertEqual(result.returncode, 69, result.stderr)
        self.assertNotIn(FINE_GRAINED, result.stdout + result.stderr)
        self.assertIn("token-class-not-classic", result.stderr)

    def test_an_unknown_mode_is_an_invalid_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(_config_payload()), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-B", str(_AUTH_CLI), "preflight", "--config", str(path), "--mode", "app"],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout.strip(), "")

    def test_an_invalid_config_exits_65(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"version": 1, "project": {"owner": "x", "number": 1}, "columns": []}), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = auth_cli.main(["preflight", "--config", str(path), "--mode", "unattended"], env={}, probe=FakeProbe())
        self.assertEqual(code, 65)
        self.assertEqual(out.getvalue().strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
