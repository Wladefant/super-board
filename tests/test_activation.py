"""Task 4 — off / proof-only / active dispatch modes.

Pure stdlib `unittest`. No network, no `gh`.

Run directly:
  python -B tests/test_activation.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_activation.py' -v
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

from super_board_runtime.activation import (  # noqa: E402
    ACTIVATION_LADDER,
    ACTIVATION_MODES,
    ActivationDecision,
    evaluate_activation,
    guard_stage,
    validate_activation_transition,
)
from super_board_runtime.config import ConfigError, load_and_validate_config  # noqa: E402
from super_board_runtime.eligibility import IssueSnapshot, evaluate_dispatch  # noqa: E402

CONFIG_CLI = _SCRIPTS / "super-board-config.py"
PROOF_URL = "https://github.com/Bavariance/polysimulator/issues/7"


def _payload(**overrides: object) -> dict:
    payload = {
        "version": 1,
        "variant": "full",
        "project": {"owner": "Bavariance", "number": 1},
        "repo": {"remote": "Bavariance/polysimulator"},
        "base_branch": "staging",
        "activation_mode": "off",
    }
    payload.update(overrides)
    return payload


def _write(tmpdir: Path, payload: dict) -> Path:
    path = Path(tmpdir) / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _config(**overrides: object):
    with tempfile.TemporaryDirectory() as tmp:
        return load_and_validate_config(_write(Path(tmp), _payload(**overrides)))


_UNSET = object()


def _issue(number: int = 7, url: object = _UNSET, **overrides: object) -> IssueSnapshot:
    base = {
        "url": (
            f"https://github.com/Bavariance/polysimulator/issues/{number}"
            if url is _UNSET
            else url
        ),
        "node_id": f"I_kwDOexample{number}",
        "number": number,
        "content_type": "Issue",
        "state": "OPEN",
        "title": "a perfectly formed card",
        # Routing is fail-closed, so a perfectly formed card declares its route.
        "body": "## Acceptance Criteria\n- [ ] it works\n\nBranch route: staging\n",
        "labels": (),
        "assignees": (),
        "status": "Ready",
        "milestone": None,
    }
    base.update(overrides)
    return IssueSnapshot(**base)  # type: ignore[arg-type]


class ModeEnumTests(unittest.TestCase):
    def test_the_three_modes_are_exactly_these(self) -> None:
        self.assertEqual(ACTIVATION_MODES, ("off", "proof-only", "active"))


class OffModeTests(unittest.TestCase):
    def test_off_permits_nothing_at_all(self) -> None:
        config = _config(activation_mode="off")
        for issue in (
            _issue(),
            _issue(status="Backlog"),
            _issue(labels=("design",)),
            _issue(number=999),
        ):
            with self.subTest(issue=issue.number, status=issue.status):
                decision = evaluate_activation(issue, config)
                self.assertIsInstance(decision, ActivationDecision)
                self.assertFalse(decision.permitted)
                self.assertEqual(decision.reason_code, "activation-off")
                self.assertEqual(decision.activation_mode, "off")

    def test_a_perfectly_formed_ready_issue_is_still_refused(self) -> None:
        decision = evaluate_dispatch(_issue(), _config(activation_mode="off"))
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.reason_codes, ("activation-off",))
        self.assertEqual(decision.activation_mode, "off")

    def test_runtime_commands_cannot_bypass_activation(self) -> None:
        # No argument, flag, or lane hint reaches past the gate: the only input
        # that changes the answer is the config itself.
        config = _config(activation_mode="off")
        for backend in ("claude-p", "workflow"):
            with self.subTest(worker_backend=backend):
                decision = evaluate_dispatch(_issue(), _config(activation_mode="off", worker_backend=backend))
                self.assertFalse(decision.eligible)
        self.assertFalse(evaluate_activation(_issue(), config).permitted)


class ProofOnlyModeTests(unittest.TestCase):
    def _proof_config(self):
        return _config(activation_mode="proof-only", proof_issue_url=PROOF_URL)

    def test_exactly_one_issue_is_allowlisted(self) -> None:
        decision = evaluate_activation(_issue(7), self._proof_config())
        self.assertTrue(decision.permitted, decision.reason_code)
        self.assertIsNone(decision.reason_code)
        self.assertEqual(decision.activation_mode, "proof-only")

    def test_every_other_issue_is_refused(self) -> None:
        config = self._proof_config()
        for issue in (_issue(8), _issue(70), _issue(6)):
            with self.subTest(number=issue.number):
                decision = evaluate_activation(issue, config)
                self.assertFalse(decision.permitted)
                self.assertEqual(decision.reason_code, "activation-not-allowlisted")

    def test_url_matching_is_normalized_not_fuzzy(self) -> None:
        config = self._proof_config()
        # Trailing slash and host casing normalize; a different repo does not.
        self.assertTrue(
            evaluate_activation(_issue(url=PROOF_URL + "/"), config).permitted
        )
        self.assertTrue(
            evaluate_activation(_issue(url="https://GitHub.com/Bavariance/PolySimulator/issues/7"), config).permitted
        )
        self.assertFalse(
            evaluate_activation(_issue(url="https://github.com/Wladefant/super-board/issues/7"), config).permitted
        )
        self.assertFalse(
            evaluate_activation(_issue(url="https://github.com/Bavariance/polysimulator/issues/70"), config).permitted
        )

    def test_a_card_without_a_url_fails_closed(self) -> None:
        decision = evaluate_activation(_issue(url=None), self._proof_config())
        self.assertFalse(decision.permitted)
        self.assertEqual(decision.reason_code, "activation-not-allowlisted")

    def test_eligibility_selects_at_most_the_allowlisted_issue(self) -> None:
        config = self._proof_config()
        self.assertTrue(evaluate_dispatch(_issue(7), config).eligible)
        other = evaluate_dispatch(_issue(8), config)
        self.assertFalse(other.eligible)
        self.assertEqual(other.reason_codes, ("activation-not-allowlisted",))

    def test_a_closed_proof_issue_fails_configuration_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp), _payload(activation_mode="proof-only", proof_issue_url=PROOF_URL)
            )
            with self.assertRaises(ConfigError) as ctx:
                load_and_validate_config(path, issue_state_lookup=lambda _u: "CLOSED")
            self.assertEqual(ctx.exception.reason, "proof-url-issue-not-open")

    def test_a_proof_issue_in_another_repository_exits_65(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp),
                _payload(
                    activation_mode="proof-only",
                    proof_issue_url="https://github.com/Wladefant/super-board/issues/7",
                ),
            )
            result = subprocess.run(
                [sys.executable, "-B", str(CONFIG_CLI), "validate", "--config", str(path), "--json"],
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 65)
        self.assertEqual(result.stdout.strip(), "")
        self.assertIn("proof-url-wrong-repository", result.stderr)


class ActiveModeTests(unittest.TestCase):
    def test_active_defers_entirely_to_evaluate_dispatch(self) -> None:
        config = _config(activation_mode="active")
        for issue in (_issue(), _issue(status="Blocked"), _issue(labels=("design",))):
            with self.subTest(status=issue.status, labels=issue.labels):
                decision = evaluate_activation(issue, config)
                self.assertTrue(decision.permitted)
                self.assertIsNone(decision.reason_code)
        self.assertTrue(evaluate_dispatch(_issue(), config).eligible)
        self.assertEqual(
            evaluate_dispatch(_issue(status="Blocked"), config).reason_codes, ("status-not-ready",)
        )


class ReEvaluationTests(unittest.TestCase):
    """Activation is re-read from disk immediately before claim and before launch."""

    def test_claim_and_launch_re_read_the_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), _payload(activation_mode="active"))
            for stage in ("claim", "launch"):
                with self.subTest(stage=stage):
                    decision = guard_stage(_issue(), path, planned_mode="active", stage=stage)
                    self.assertTrue(decision.permitted, decision.reason_code)

    def test_a_mode_change_between_plan_and_claim_aborts_the_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), _payload(activation_mode="active"))
            self.assertTrue(guard_stage(_issue(), path, planned_mode="active", stage="claim").permitted)
            # The operator flips the board off mid-run.
            path.write_text(json.dumps(_payload(activation_mode="off")), encoding="utf-8")
            decision = guard_stage(_issue(), path, planned_mode="active", stage="claim")
            self.assertFalse(decision.permitted)
            self.assertEqual(decision.reason_code, "activation-mode-changed")
            self.assertEqual(decision.activation_mode, "off")

    def test_a_mode_change_between_claim_and_launch_aborts_the_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp), _payload(activation_mode="proof-only", proof_issue_url=PROOF_URL)
            )
            self.assertTrue(
                guard_stage(_issue(7), path, planned_mode="proof-only", stage="launch").permitted
            )
            path.write_text(json.dumps(_payload(activation_mode="active")), encoding="utf-8")
            decision = guard_stage(_issue(7), path, planned_mode="proof-only", stage="launch")
            self.assertFalse(decision.permitted)
            self.assertEqual(decision.reason_code, "activation-mode-changed")

    def test_an_unreadable_config_fails_closed(self) -> None:
        decision = guard_stage(_issue(), Path("nowhere-at-all.json"), planned_mode="active", stage="claim")
        self.assertFalse(decision.permitted)
        self.assertEqual(decision.reason_code, "activation-config-invalid")

    def test_the_allowlist_is_re_checked_at_claim_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp), _payload(activation_mode="proof-only", proof_issue_url=PROOF_URL)
            )
            decision = guard_stage(_issue(8), path, planned_mode="proof-only", stage="claim")
            self.assertFalse(decision.permitted)
            self.assertEqual(decision.reason_code, "activation-not-allowlisted")


class ActivationLadderTests(unittest.TestCase):
    """`off` → `proof-only` → `active`, one rung at a time.

    The setup skill claimed every activation rule below it was "enforced in
    code", and this one was not: `config.py` validates the CURRENT mode and
    nothing else, so a board could go straight from `off` to `active` in one
    edit and no code would notice. The half of the rule a validator genuinely
    cannot see — that each step arrives as a human-reviewed pull request — is
    governance, and is now labelled as governance.
    """

    def test_the_ladder_is_the_three_modes_in_order(self) -> None:
        self.assertEqual(ACTIVATION_LADDER, ("off", "proof-only", "active"))
        self.assertEqual(set(ACTIVATION_LADDER), set(ACTIVATION_MODES))

    def test_one_rung_up_is_permitted(self) -> None:
        for previous, following in (("off", "proof-only"), ("proof-only", "active")):
            with self.subTest(step=(previous, following)):
                transition = validate_activation_transition(previous, following)
                self.assertTrue(transition.permitted)
                self.assertIsNone(transition.reason_code)

    def test_skipping_the_proof_rung_is_refused(self) -> None:
        transition = validate_activation_transition("off", "active")
        self.assertFalse(transition.permitted)
        self.assertEqual(transition.reason_code, "activation-ladder-skipped")

    def test_standing_still_is_not_a_transition(self) -> None:
        for mode in ACTIVATION_LADDER:
            with self.subTest(mode=mode):
                self.assertTrue(validate_activation_transition(mode, mode).permitted)

    def test_every_de_escalation_is_permitted(self) -> None:
        # Turning a board down, or off, must never be something code refuses.
        for previous, following in (
            ("active", "proof-only"),
            ("active", "off"),
            ("proof-only", "off"),
        ):
            with self.subTest(step=(previous, following)):
                transition = validate_activation_transition(previous, following)
                self.assertTrue(transition.permitted, transition.reason_code)

    def test_an_unknown_mode_on_either_side_is_refused(self) -> None:
        for previous, following in (("off", "on"), ("enabled", "active"), (None, "active")):
            with self.subTest(step=(previous, following)):
                transition = validate_activation_transition(previous, following)
                self.assertFalse(transition.permitted)
                self.assertEqual(transition.reason_code, "activation-mode-invalid")

    def test_the_cli_refuses_a_skipped_rung_with_exit_65(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), _payload(activation_mode="active"))
            result = subprocess.run(
                [
                    sys.executable, "-B", "-m", "super_board_runtime.activation",
                    "--config", str(path), "--previous-mode", "off",
                ],
                capture_output=True, text=True, cwd=str(_SCRIPTS),
            )
        self.assertEqual(result.returncode, 65, result.stdout)
        self.assertIn("activation-ladder-skipped", result.stderr)

    def test_the_cli_accepts_a_single_rung(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), _payload(activation_mode="active"))
            result = subprocess.run(
                [
                    sys.executable, "-B", "-m", "super_board_runtime.activation",
                    "--config", str(path), "--previous-mode", "proof-only",
                    "--issue-url", PROOF_URL,
                ],
                capture_output=True, text=True, cwd=str(_SCRIPTS),
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["transition_permitted"])


class SetupSkillHonestyTests(unittest.TestCase):
    SKILL = _REPO_ROOT / "skills" / "superboard-setup" / "SKILL.md"

    def test_the_ladder_names_the_validator_that_enforces_it(self) -> None:
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("validate_activation_transition", text)

    def test_the_human_review_half_is_labelled_governance(self) -> None:
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("governance", text.lower())


class ActivationCliTests(unittest.TestCase):
    def _run(self, config_path: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "super_board_runtime.activation",
                "--config",
                str(config_path),
                *args,
            ],
            capture_output=True,
            text=True,
            cwd=str(_SCRIPTS),
        )

    def test_cli_reports_a_refusal_as_machine_readable_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), _payload(activation_mode="off"))
            result = self._run(path, "--issue-url", PROOF_URL, "--stage", "claim")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["permitted"])
        self.assertEqual(payload["reason_code"], "activation-off")
        self.assertEqual(payload["stage"], "claim")

    def test_cli_detects_a_mode_change_against_the_planned_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp), _payload(activation_mode="off"))
            result = self._run(path, "--issue-url", PROOF_URL, "--planned-mode", "active")
        payload = json.loads(result.stdout)
        self.assertFalse(payload["permitted"])
        self.assertEqual(payload["reason_code"], "activation-mode-changed")

    def test_cli_exits_65_on_an_invalid_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"version": 1, "project": {"owner": "x", "number": 1}, "columns": []}), encoding="utf-8")
            result = self._run(path, "--issue-url", PROOF_URL)
        self.assertEqual(result.returncode, 65)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
