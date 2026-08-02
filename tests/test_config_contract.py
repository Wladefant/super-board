"""Task 2 — the unified configuration and lifecycle contract.

Pure stdlib `unittest`. No network, no `gh`, no third-party packages.

Run directly:
  python -B tests/test_config_contract.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_config_contract.py' -v
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.config import (  # noqa: E402
    ACTIVATION_MODES,
    MINIMUM_GRAPHQL_RESERVE,
    ConfigError,
    NormalizedConfig,
    load_and_validate_config,
    normalized_config_to_json,
)
from super_board_runtime.lifecycle import (  # noqa: E402
    LIFECYCLE_STATUSES,
    LifecycleError,
    canonicalize_status,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONFIG_CLI = _SCRIPTS / "super-board-config.py"


def _write_config(tmpdir: str, payload: dict) -> Path:
    path = Path(tmpdir) / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _base_payload(**overrides: object) -> dict:
    payload = {
        "version": 1,
        "variant": "full",
        "project": {"owner": "Bavariance", "number": 1},
        "repo": {"remote": "Bavariance/polysimulator"},
        "base_branch": "staging",
    }
    payload.update(overrides)
    return payload


def _load(payload: dict, **kwargs: object) -> NormalizedConfig:
    with tempfile.TemporaryDirectory() as tmp:
        return load_and_validate_config(_write_config(tmp, payload), **kwargs)


def _reason(testcase: unittest.TestCase, payload: dict, expected: str, **kwargs: object) -> None:
    with testcase.assertRaises(ConfigError) as ctx:
        _load(payload, **kwargs)
    testcase.assertEqual(ctx.exception.reason, expected)


class LifecycleContractTests(unittest.TestCase):
    def test_lifecycle_statuses_are_exactly_the_seven_canonical_names_in_order(self) -> None:
        self.assertEqual(
            LIFECYCLE_STATUSES,
            ("Backlog", "Ready", "Building", "QA", "Review", "Blocked", "Done"),
        )

    def test_canonicalize_status_trims_and_is_case_insensitive(self) -> None:
        self.assertEqual(canonicalize_status("  ready "), "Ready")
        self.assertEqual(canonicalize_status("qa"), "QA")
        self.assertEqual(canonicalize_status("BLOCKED"), "Blocked")

    def test_skipped_is_rejected_as_a_lifecycle_value(self) -> None:
        with self.assertRaises(LifecycleError) as ctx:
            canonicalize_status("Skipped")
        self.assertEqual(ctx.exception.reason, "lifecycle-status-skipped")
        with self.assertRaises(LifecycleError):
            canonicalize_status("  skipped  ")

    def test_unknown_status_is_rejected(self) -> None:
        with self.assertRaises(LifecycleError) as ctx:
            canonicalize_status("In Progress")
        self.assertEqual(ctx.exception.reason, "lifecycle-status-unknown")


class ConfigContractTests(unittest.TestCase):
    def test_columns_key_is_rejected(self) -> None:
        _reason(
            self,
            _base_payload(columns=["Ready", "QA", "Review", "Done"]),
            "columns-removed-lifecycle-is-fixed",
        )

    def test_skipped_is_rejected_wherever_a_lifecycle_value_is_accepted(self) -> None:
        _reason(
            self,
            _base_payload(status_aliases={"Parked": "Skipped"}),
            "lifecycle-status-skipped",
        )

    def test_defaults(self) -> None:
        config = _load(_base_payload())
        self.assertEqual(config.worker_backend, "claude-p")
        self.assertIs(config.human_approves_merge, True)
        self.assertEqual(config.merge_method, "rebase")
        self.assertEqual(config.activation_mode, "off")
        self.assertIsNone(config.proof_issue_url)
        self.assertEqual(config.minimum_graphql_reserve, 1000)
        self.assertEqual(MINIMUM_GRAPHQL_RESERVE, 1000)

    def test_permanently_excluded_labels_are_always_present(self) -> None:
        config = _load(_base_payload())
        self.assertIn("design", config.exclude_labels)
        self.assertIn("history", config.exclude_labels)
        config = _load(_base_payload(exclude_labels=["  Wontfix "]))
        self.assertEqual(config.exclude_labels, ("design", "history", "wontfix"))

    def test_activation_mode_enum(self) -> None:
        self.assertEqual(ACTIVATION_MODES, ("off", "proof-only", "active"))
        _reason(self, _base_payload(activation_mode="on"), "activation-mode-invalid")

    def test_proof_issue_url_must_be_null_off_and_active(self) -> None:
        url = "https://github.com/Bavariance/polysimulator/issues/7"
        _reason(
            self,
            _base_payload(activation_mode="off", proof_issue_url=url),
            "proof-url-must-be-null",
        )
        _reason(
            self,
            _base_payload(activation_mode="active", proof_issue_url=url),
            "proof-url-must-be-null",
        )

    def test_proof_only_requires_an_issue_url_inside_the_configured_repository(self) -> None:
        _reason(self, _base_payload(activation_mode="proof-only"), "proof-url-required")
        _reason(
            self,
            _base_payload(
                activation_mode="proof-only",
                proof_issue_url="https://github.com/Bavariance/polysimulator/pull/7",
            ),
            "proof-url-invalid",
        )
        _reason(
            self,
            _base_payload(
                activation_mode="proof-only",
                proof_issue_url="https://github.com/Wladefant/super-board/issues/7",
            ),
            "proof-url-wrong-repository",
        )
        config = _load(
            _base_payload(
                activation_mode="proof-only",
                proof_issue_url="https://github.com/Bavariance/polysimulator/issues/7/",
            )
        )
        self.assertEqual(
            config.proof_issue_url,
            "https://github.com/Bavariance/polysimulator/issues/7",
        )

    def test_proof_only_url_must_point_at_an_open_issue(self) -> None:
        payload = _base_payload(
            activation_mode="proof-only",
            proof_issue_url="https://github.com/Bavariance/polysimulator/issues/7",
        )
        _reason(self, payload, "proof-url-issue-not-open", issue_state_lookup=lambda _u: "CLOSED")
        _reason(self, payload, "proof-url-state-unavailable", issue_state_lookup=lambda _u: None)
        config = _load(payload, issue_state_lookup=lambda _u: "OPEN")
        self.assertEqual(config.activation_mode, "proof-only")

    def test_merge_method_enum_rejects_squash_and_merge(self) -> None:
        _reason(self, _base_payload(merge_method="squash"), "merge-method-must-be-rebase")
        _reason(self, _base_payload(merge_method="merge"), "merge-method-must-be-rebase")
        _reason(self, _base_payload(merge_method="fast-forward"), "merge-method-invalid")
        self.assertEqual(_load(_base_payload(merge_method="rebase")).merge_method, "rebase")

    def test_worker_backend_enum(self) -> None:
        _reason(self, _base_payload(worker_backend="claude"), "worker-backend-invalid")
        self.assertEqual(_load(_base_payload(worker_backend="workflow")).worker_backend, "workflow")

    def test_graphql_reserve_may_be_raised_never_lowered(self) -> None:
        _reason(self, _base_payload(minimum_graphql_reserve=200), "graphql-reserve-below-floor")
        self.assertEqual(
            _load(_base_payload(minimum_graphql_reserve=2500)).minimum_graphql_reserve, 2500
        )

    def test_branch_routes_are_validated_and_surfaced(self) -> None:
        config = _load(_base_payload(branch_routes={"route:main": "main", "route:staging": "staging"}))
        self.assertEqual(dict(config.branch_routes), {"route:main": "main", "route:staging": "staging"})
        _reason(self, _base_payload(branch_routes={"route:main": ""}), "branch-routes-invalid")
        _reason(self, _base_payload(branch_routes=["main"]), "branch-routes-invalid")

    def test_design_skill_agent_native_and_deploy_are_validated_and_surfaced(self) -> None:
        config = _load(
            _base_payload(
                design_skill={"enabled": True, "label": "design"},
                agent_native={"enabled": True, "projection_only": True},
                deploy={"provider": "dokploy", "auto_deploy": False},
            )
        )
        self.assertEqual(dict(config.design_skill), {"enabled": True, "label": "design"})
        self.assertEqual(dict(config.agent_native), {"enabled": True, "projection_only": True})
        self.assertEqual(dict(config.deploy), {"provider": "dokploy", "auto_deploy": False})
        _reason(self, _base_payload(design_skill={"enabled": "yes"}), "design-skill-invalid")
        _reason(
            self,
            _base_payload(agent_native={"enabled": True, "projection_only": False}),
            "agent-native-must-be-projection-only",
        )
        _reason(self, _base_payload(deploy={"auto_deploy": "no"}), "deploy-invalid")

    def test_github_auth_is_validated_and_surfaced(self) -> None:
        config = _load(_base_payload())
        self.assertEqual(config.github_auth["token_env_var"], "SUPERBOARD_GITHUB_TOKEN")
        self.assertEqual(config.github_auth["login_env_var"], "SUPERBOARD_GITHUB_LOGIN")
        self.assertEqual(tuple(config.github_auth["required_scopes"]), ("repo", "project", "read:org"))
        _reason(
            self,
            _base_payload(github_auth={"token_env_var": "GH_TOKEN"}),
            "github-auth-token-env-var-invalid",
        )
        _reason(
            self,
            _base_payload(github_auth={"required_scopes": ["repo", "read:org"]}),
            "github-auth-scope-incomplete",
        )
        _reason(self, _base_payload(github_auth={"mode": "app"}), "github-auth-mode-invalid")

    def test_inline_credentials_are_rejected(self) -> None:
        # Reference env vars by NAME only; a literal credential in a config file
        # is a publication accident waiting to happen.
        _reason(
            self,
            _base_payload(github_auth={"token": "ghp_" + "x" * 36}),
            "config-inline-credential",
        )

    def test_missing_and_malformed_files(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_and_validate_config(Path("does-not-exist-anywhere.json"))
        self.assertEqual(ctx.exception.reason, "config-not-found")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_and_validate_config(path)
            self.assertEqual(ctx.exception.reason, "config-not-json")

    def test_normalized_config_to_json_is_sorted_and_round_trips(self) -> None:
        config = _load(_base_payload())
        rendered = normalized_config_to_json(config)
        parsed = json.loads(rendered)
        self.assertEqual(list(parsed), sorted(parsed))
        self.assertEqual(parsed["activation_mode"], "off")
        self.assertEqual(parsed["base_branch"], "staging")
        self.assertEqual(parsed["exclude_labels"], ["design", "history"])


class ConfigCliTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-B", str(CONFIG_CLI), *args],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )

    def test_valid_fixture_prints_sorted_json_and_exits_zero(self) -> None:
        result = self._run(
            "validate", "--config", str(FIXTURES / "config-valid-polysimulator.json"), "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertEqual(list(parsed), sorted(parsed))
        self.assertEqual(parsed["project_owner"], "Bavariance")
        self.assertEqual(parsed["activation_mode"], "off")

    def test_skipped_fixture_exits_65_with_empty_stdout(self) -> None:
        result = self._run(
            "validate", "--config", str(FIXTURES / "config-invalid-skipped.json"), "--json"
        )
        self.assertEqual(result.returncode, 65)
        self.assertEqual(result.stdout.strip(), "")
        self.assertIn("lifecycle-status-skipped", result.stderr)

    def test_columns_fixture_exits_65_with_empty_stdout(self) -> None:
        result = self._run(
            "validate", "--config", str(FIXTURES / "config-invalid-columns.json"), "--json"
        )
        self.assertEqual(result.returncode, 65)
        self.assertEqual(result.stdout.strip(), "")
        self.assertIn("columns-removed-lifecycle-is-fixed", result.stderr)

    def test_unknown_subcommand_exits_64(self) -> None:
        result = self._run("frobnicate", "--config", "x")
        self.assertEqual(result.returncode, 64)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
