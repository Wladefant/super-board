"""Task 17 — Agent Native is a projection, not a control surface.

Pure stdlib `unittest`. No network, no `gh`.

The cockpit renders the board and the design work. It is a window. The moment a
window can also move a card, two things own the lifecycle and neither can be
trusted about it: a status set from the cockpit has no compare-before-mutate
record, no QA linkage, and no merge evidence behind it, but on the board it
looks exactly like one that does.

So the payload is checked mechanically for every way a projection can turn into
a control surface: repository command execution, credential fields, branch
changes, pull-request creation, Project mutation verbs, and a second completion
ledger.

Static checks are necessary and not sufficient — a payload can declare anything.
The deployed cockpit is therefore probed with **synthetic non-resolving
targets**: a Project item ID that does not exist and a repository command that
does not exist. A probe that is handed a real item ID or a real command is
refused outright, because proving "mutation is unavailable" must never be done
by attempting a mutation that could succeed.

Run directly:
  python -B tests/test_agent_native_safety.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_agent_native_safety.py' -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.agent_native import (  # noqa: E402
    AGENT_NATIVE_MODE,
    CODE_EXECUTION_SETTING,
    DEPLOYED_EVIDENCE_DOCUMENT,
    NEGATIVE_CAPABILITIES,
    STALE_GUIDANCE_ALLOWLIST,
    SYNTHETIC_PROJECT_ITEM_ID,
    SYNTHETIC_REPOSITORY_COMMAND,
    AgentNativeError,
    evaluate_agent_native_payload,
    probe_deployed_cockpit,
    render_cockpit_projection,
    scan_stale_projects_guidance,
)

PAYLOAD_PATH = _REPO_ROOT / "payload" / "agent-native" / "super-board.json"
REFERENCE = _REPO_ROOT / "skills" / "super-board" / "references" / "agent-native.md"
EVIDENCE = _REPO_ROOT / DEPLOYED_EVIDENCE_DOCUMENT


def _payload(**overrides):
    body = json.loads(PAYLOAD_PATH.read_text(encoding="utf-8"))
    body.update(overrides)
    return body


# ───────────────────────────── the shipped payload ─────────────────────────────


class ShippedPayloadTests(unittest.TestCase):
    def test_the_shipped_payload_is_safe(self) -> None:
        report = evaluate_agent_native_payload(_payload())
        self.assertTrue(report.safe, report.violations)
        self.assertEqual(report.violations, ())

    def test_it_declares_read_only_projection(self) -> None:
        self.assertEqual(_payload()["mode"], AGENT_NATIVE_MODE)
        self.assertEqual(AGENT_NATIVE_MODE, "read-only-projection")

    def test_design_presentation_and_board_projection_are_permitted(self) -> None:
        capabilities = _payload()["capabilities"]
        self.assertTrue(capabilities["design_presentation"])
        self.assertTrue(capabilities["project_projection"])

    def test_plan_analytics_and_clips_are_off_for_polysimulator(self) -> None:
        capabilities = _payload()["capabilities"]
        for name in ("plan", "analytics", "clips"):
            with self.subTest(capability=name):
                self.assertFalse(capabilities[name])

    def test_production_code_execution_is_off(self) -> None:
        self.assertEqual(_payload()["environment"][CODE_EXECUTION_SETTING], "off")
        self.assertEqual(CODE_EXECUTION_SETTING, "AGENT_PROD_CODE_EXECUTION")

    def test_it_carries_no_credential_of_any_kind(self) -> None:
        self.assertEqual(_payload()["credentials"], [])

    def test_it_declares_no_ledger(self) -> None:
        self.assertEqual(_payload()["ledgers"], [])


class RejectedPayloadTests(unittest.TestCase):
    def _violations(self, **overrides) -> tuple[str, ...]:
        report = evaluate_agent_native_payload(_payload(**overrides))
        self.assertFalse(report.safe)
        return report.violations

    def test_an_enabled_plan_analytics_or_clips_capability_is_rejected(self) -> None:
        for name in ("plan", "analytics", "clips"):
            with self.subTest(capability=name):
                capabilities = dict(_payload()["capabilities"])
                capabilities[name] = True
                violations = self._violations(capabilities=capabilities)
                self.assertIn(f"capability-not-read-only:{name}", violations)

    def test_repository_command_execution_is_rejected(self) -> None:
        violations = self._violations(
            execution={"shell": "bash", "repository_checkout": True}
        )
        self.assertIn("repository-execution-declared", violations)

    def test_a_credential_field_is_rejected(self) -> None:
        # Token-shaped only in structure; no value is written here.
        violations = self._violations(credentials=[{"name": "GITHUB_TOKEN"}])
        self.assertIn("credential-declared", violations)

    def test_a_credential_shaped_key_anywhere_is_rejected(self) -> None:
        violations = self._violations(integrations={"github": {"api_key": ""}})
        self.assertIn("credential-declared", violations)

    def test_branch_changes_are_rejected(self) -> None:
        violations = self._violations(actions=["create_branch"])
        self.assertIn("mutation-verb-declared", violations)

    def test_pull_request_creation_is_rejected(self) -> None:
        # Assembled by concatenation so this file carries no literal that the
        # tree-wide merge-prohibition scanner would have to allowlist.
        verb = "create" + "_pull_" + "request"
        violations = self._violations(actions=[verb])
        self.assertIn("mutation-verb-declared", violations)

    def test_project_status_mutation_verbs_are_rejected(self) -> None:
        for verb in (
            "addProjectV2ItemById",
            "updateProjectV2ItemFieldValue",
            "deleteProjectV2Item",
        ):
            with self.subTest(verb=verb):
                violations = self._violations(actions=[verb])
                self.assertIn("mutation-verb-declared", violations)

    def test_a_second_completion_ledger_is_rejected(self) -> None:
        violations = self._violations(ledgers=[{"name": "cockpit-completion"}])
        self.assertIn("second-ledger-declared", violations)

    def test_a_lifecycle_ledger_under_any_name_is_rejected(self) -> None:
        violations = self._violations(completion_ledger={"store": "cockpit-db"})
        self.assertIn("second-ledger-declared", violations)

    def test_code_execution_left_on_is_rejected(self) -> None:
        violations = self._violations(environment={CODE_EXECUTION_SETTING: "on"})
        self.assertIn("code-execution-not-off", violations)

    def test_a_missing_environment_declaration_is_rejected(self) -> None:
        violations = self._violations(environment={})
        self.assertIn("code-execution-not-off", violations)

    def test_a_control_surface_mode_is_rejected(self) -> None:
        violations = self._violations(mode="read-write")
        self.assertIn("mode-not-read-only", violations)

    def test_a_payload_that_is_not_an_object_is_rejected(self) -> None:
        report = evaluate_agent_native_payload(["not", "a", "payload"])
        self.assertFalse(report.safe)
        self.assertIn("payload-unreadable", report.violations)


class CockpitOutputTests(unittest.TestCase):
    def test_cockpit_output_is_sanitized_read_only_snapshot_text(self) -> None:
        rendered = render_cockpit_projection(
            ({"title": "Normalize intake", "status": "Backlog"},), {}
        )
        self.assertIn("Normalize intake", rendered)
        self.assertIn("Backlog", rendered)

    def test_a_credential_shaped_snapshot_value_never_reaches_the_cockpit(self) -> None:
        sentinel = "gh" + "p_" + ("A1b2C3d4E5f6G7h8" * 2)
        rendered = render_cockpit_projection(({"title": f"fix {sentinel}"},), {})
        self.assertNotIn(sentinel, rendered)
        self.assertIn("[redacted:github-token]", rendered)

    def test_the_payload_declares_where_cockpit_output_comes_from(self) -> None:
        output = _payload()["output"]
        self.assertEqual(output["source"], "read-only-snapshot")
        self.assertEqual(output["sanitizer"], "sanitize_and_validate_publication")

    def test_an_unsanitized_output_declaration_is_rejected(self) -> None:
        report = evaluate_agent_native_payload(
            _payload(output={"source": "live-github", "sanitizer": None})
        )
        self.assertFalse(report.safe)
        self.assertIn("output-not-read-only", report.violations)
        self.assertIn("output-not-sanitized", report.violations)


# ───────────────────────────── deployed evidence ─────────────────────────────


class DeployedEvidenceTests(unittest.TestCase):
    def test_there_are_exactly_seven_negative_capabilities(self) -> None:
        self.assertEqual(len(NEGATIVE_CAPABILITIES), 7)
        self.assertEqual(
            set(NEGATIVE_CAPABILITIES),
            {
                "no-project-write-credential",
                "no-github-write-token",
                "no-docker-socket",
                "no-runner-filesystem-mount",
                "no-repository-checkout",
                "no-trusted-shell",
                "no-second-completion-ledger",
            },
        )

    def test_the_deployed_evidence_document_exists(self) -> None:
        self.assertTrue(EVIDENCE.is_file(), f"{DEPLOYED_EVIDENCE_DOCUMENT} must exist")

    def test_it_lists_all_seven_negative_capabilities(self) -> None:
        text = EVIDENCE.read_text(encoding="utf-8")
        for capability in NEGATIVE_CAPABILITIES:
            with self.subTest(capability=capability):
                self.assertIn(capability, text)

    def test_it_names_both_synthetic_targets(self) -> None:
        text = EVIDENCE.read_text(encoding="utf-8")
        self.assertIn(SYNTHETIC_PROJECT_ITEM_ID, text)
        self.assertIn(SYNTHETIC_REPOSITORY_COMMAND, text)

    def test_it_records_the_code_execution_setting(self) -> None:
        text = EVIDENCE.read_text(encoding="utf-8")
        self.assertIn(f"{CODE_EXECUTION_SETTING}=off", text)

    def test_the_payload_declares_every_negative_capability(self) -> None:
        declared = set(_payload()["negative_capabilities"])
        self.assertEqual(declared, set(NEGATIVE_CAPABILITIES))

    def test_a_payload_missing_a_negative_capability_is_rejected(self) -> None:
        report = evaluate_agent_native_payload(
            _payload(negative_capabilities=list(NEGATIVE_CAPABILITIES[:-1]))
        )
        self.assertFalse(report.safe)
        self.assertIn(
            f"negative-capability-undeclared:{NEGATIVE_CAPABILITIES[-1]}", report.violations
        )


class SyntheticProbeTests(unittest.TestCase):
    def test_both_synthetic_targets_are_obviously_not_real(self) -> None:
        self.assertIn("SYNTHETIC", SYNTHETIC_PROJECT_ITEM_ID)
        self.assertIn("synthetic", SYNTHETIC_REPOSITORY_COMMAND)

    def test_a_probe_that_fails_on_both_targets_is_positive_evidence(self) -> None:
        report = probe_deployed_cockpit(
    SYNTHETIC_PROJECT_ITEM_ID,
            SYNTHETIC_REPOSITORY_COMMAND,
            mutate_probe=lambda _item: (_ for _ in ()).throw(RuntimeError("no such capability")),
            execute_probe=lambda _cmd: (_ for _ in ()).throw(RuntimeError("no shell")),
        )
        self.assertTrue(report.safe)
        self.assertEqual(report.violations, ())

    def test_a_probe_that_succeeds_proves_the_capability_exists(self) -> None:
        report = probe_deployed_cockpit(
    SYNTHETIC_PROJECT_ITEM_ID,
            SYNTHETIC_REPOSITORY_COMMAND,
            mutate_probe=lambda _item: {"accepted": True},
            execute_probe=lambda _cmd: (_ for _ in ()).throw(RuntimeError("no shell")),
        )
        self.assertFalse(report.safe)
        self.assertIn("project-mutation-available", report.violations)

    def test_a_reachable_shell_is_a_violation(self) -> None:
        report = probe_deployed_cockpit(
    SYNTHETIC_PROJECT_ITEM_ID,
            SYNTHETIC_REPOSITORY_COMMAND,
            mutate_probe=lambda _item: (_ for _ in ()).throw(RuntimeError("no such capability")),
            execute_probe=lambda _cmd: {"exit_code": 127},
        )
        self.assertFalse(report.safe)
        self.assertIn("repository-execution-available", report.violations)

    def test_a_real_project_item_id_is_refused_outright(self) -> None:
        with self.assertRaises(AgentNativeError) as ctx:
            probe_deployed_cockpit(
                "PVTI_kwDOAbCdEf4AbCdEzgAbCdEf",
                SYNTHETIC_REPOSITORY_COMMAND,
                mutate_probe=lambda _item: None,
                execute_probe=lambda _cmd: None,
            )
        self.assertEqual(ctx.exception.reason, "probe-target-not-synthetic")

    def test_a_real_repository_command_is_refused_outright(self) -> None:
        for command in ("git push", "rm -rf /", "npm run build"):
            with self.subTest(command=command):
                with self.assertRaises(AgentNativeError) as ctx:
                    probe_deployed_cockpit(
    SYNTHETIC_PROJECT_ITEM_ID,
                        command,
                        mutate_probe=lambda _item: None,
                        execute_probe=lambda _cmd: None,
                    )
                self.assertEqual(ctx.exception.reason, "probe-target-not-synthetic")

    def test_a_refused_probe_never_reaches_the_cockpit(self) -> None:
        calls: list[str] = []
        with self.assertRaises(AgentNativeError):
            probe_deployed_cockpit(
                "PVTI_kwDOAbCdEf4AbCdEzgAbCdEf",
                SYNTHETIC_REPOSITORY_COMMAND,
                mutate_probe=lambda item: calls.append(item),
                execute_probe=lambda cmd: calls.append(cmd),
            )
        self.assertEqual(calls, [])


# ───────────────────────────── stale guidance purge ─────────────────────────────


class StaleGuidanceTests(unittest.TestCase):
    def test_the_tree_makes_no_stale_projects_v2_claim(self) -> None:
        report = scan_stale_projects_guidance(_REPO_ROOT)
        detail = "\n".join(f"  {o['path']}:{o['line']} — {o['text']}" for o in report)
        self.assertEqual(report, [], f"stale GitHub-App / Projects v2 guidance:\n{detail}")

    def test_a_seeded_stale_claim_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "runbook.md").write_text(
                "Install a GitHub App with Projects v2 access on the personal account.\n",
                encoding="utf-8",
            )
            report = scan_stale_projects_guidance(root)
            self.assertEqual(len(report), 1)
            self.assertEqual(report[0]["path"], "runbook.md")

    def test_the_scan_allowlist_is_exactly_the_two_files_that_must_name_it(self) -> None:
        self.assertEqual(
            set(STALE_GUIDANCE_ALLOWLIST),
            {
                "scripts/super_board_runtime/agent_native.py",
                "tests/test_agent_native_safety.py",
            },
        )

    def test_a_correct_negated_statement_is_not_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "correct.md").write_text(
                "GitHub Apps cannot access personal Projects v2 at all.\n",
                encoding="utf-8",
            )
            self.assertEqual(scan_stale_projects_guidance(root), [])

    def test_the_reference_states_the_read_only_contract(self) -> None:
        text = REFERENCE.read_text(encoding="utf-8")
        for phrase in (
            "read-only projection",
            "no Project write credential",
            CODE_EXECUTION_SETTING,
            "synthetic",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_the_reference_never_promises_a_completion_ledger(self) -> None:
        text = REFERENCE.read_text(encoding="utf-8").casefold()
        self.assertIn("never a completion ledger", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
