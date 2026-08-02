"""Task 12 — exactly one parallel maximum-level local Codex fleet per code PR.

Pure stdlib `unittest`. Codex is never actually invoked: the runner is injected
and the tests assert on the commands that WOULD have been issued.

The contract: four lenses, in parallel, every one on the newest model at maximum
reasoning effort. The structured-diff lens never receives a custom prompt — the
CLI rejects the combination, so passing one silently loses the whole structured
review. Every finding of every severity, nits included, stays unresolved until
it is addressed or disproved with committed evidence. The fleet runs once.

Run directly:
  python -B tests/test_codex_review.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_codex_review.py' -v
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.review import (  # noqa: E402
    CODEX_LENSES,
    CODEX_MODEL,
    CODEX_REASONING_EFFORT,
    CodexGateError,
    Finding,
    build_lens_command,
    is_documentation_only,
    parse_findings,
    raw_output_dir,
    resolve_findings,
    resolve_merge_base,
    run_codex_fleet,
)

MERGE_BASE_SHA = "9f4c1b7e2a6d8053c1f4b9a70e2d5c83a6b1042f"

_CLI = _SCRIPTS / "super-board-codex-review.py"
_spec = importlib.util.spec_from_file_location("super_board_codex_review_cli", _CLI)
assert _spec is not None and _spec.loader is not None
codex_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(codex_cli)

CLEAN = ""
ONE_NIT = "scripts/x.py:12 — nit — rename `tmp` to something meaningful\n"
ONE_P1 = "scripts/x.py:40 — P1 — the head is read after the tests run\n"


#: How long a rendezvous waits before declaring the fleet non-concurrent. It is
#: a DEADLINE, not a duration the test spends: all four lenses release the
#: barrier the instant the fourth arrives. Generous, because a loaded machine is
#: slow, not sequential.
RENDEZVOUS_TIMEOUT_S = 30.0


class RecordingRunner:
    """Injected Codex runner. Records commands and observed concurrency.

    Concurrency is proved by RENDEZVOUS, not by timing. Sleeping 0.02s in each
    lens and then asserting the observed peak was 4 asks whether four threads
    happened to overlap inside a 20ms window — true on an idle machine, false
    under load, which is how this flaked. A `threading.Barrier` sized to the
    fleet cannot be satisfied unless all four lenses really are in flight at the
    same moment: the fourth arrival is what releases the first three.
    """

    def __init__(self, output=CLEAN, exit_code=0, delay=0.02, rendezvous=0) -> None:
        self.output = output
        self.exit_code = exit_code
        self.delay = delay
        self.commands: list[tuple[str, ...]] = []
        self._lock = threading.Lock()
        self._live = 0
        self.max_concurrent = 0
        self._barrier = threading.Barrier(rendezvous) if rendezvous else None
        self.rendezvous_reached = False

    def __call__(self, command, cwd):
        with self._lock:
            self.commands.append(tuple(command))
            self._live += 1
            self.max_concurrent = max(self.max_concurrent, self._live)
        if self._barrier is not None:
            # Returns only once every lens has arrived. If the fleet were
            # sequential this raises BrokenBarrierError at the deadline, and the
            # lens is recorded as failed — never as a slow pass.
            self._barrier.wait(timeout=RENDEZVOUS_TIMEOUT_S)
            self.rendezvous_reached = True
        else:
            time.sleep(self.delay)
        with self._lock:
            self._live -= 1
        out = self.output(command) if callable(self.output) else self.output
        return {"exit_code": self.exit_code, "stdout": out, "stderr": ""}


def _fleet(**kwargs):
    defaults = {
        "base_ref": "staging",
        "worktree": _REPO_ROOT,
        "documentation_only": False,
        "runner": RecordingRunner(),
        "pull_request_url": "https://github.com/Bavariance/polysimulator/pull/456",
        "merge_base_resolver": lambda base_ref, worktree: MERGE_BASE_SHA,
    }
    defaults.update(kwargs)
    with tempfile.TemporaryDirectory() as tmp:
        defaults.setdefault("ledger", Path(tmp) / "fleet-ledger.json")
        if not defaults["ledger"].parent.exists():
            defaults["ledger"].parent.mkdir(parents=True)
        return run_codex_fleet(**defaults)


class FleetShapeTests(unittest.TestCase):
    def test_exactly_four_named_lenses(self) -> None:
        self.assertEqual(
            CODEX_LENSES,
            ("structured-diff", "correctness", "security", "performance-design-consistency"),
        )

    def test_all_four_lenses_run_in_parallel(self) -> None:
        runner = RecordingRunner(rendezvous=len(CODEX_LENSES))
        report = _fleet(runner=runner)
        self.assertEqual(len(report.lenses), 4)
        self.assertEqual({lens.name for lens in report.lenses}, set(CODEX_LENSES))
        self.assertEqual(len(runner.commands), 4)
        self.assertTrue(
            runner.rendezvous_reached,
            "the four lenses never met at the barrier — the fleet is not concurrent",
        )
        self.assertEqual(
            runner.max_concurrent, 4, "the fleet must run its four lenses concurrently"
        )
        self.assertTrue(report.passed, "a rendezvous that broke would fail every lens")

    def test_a_clean_fleet_passes(self) -> None:
        report = _fleet()
        self.assertTrue(report.passed)
        self.assertEqual(report.findings, ())
        self.assertIsNone(report.reason_code)


class StdinDeadlockTests(unittest.TestCase):
    """A lens must never be able to sit waiting on stdin.

    `codex exec "<prompt>"` reads stdin when no terminal is attached —
    backgrounded, in CI, or inside a subagent — and blocks forever. It prints
    one line, `Reading additional input from stdin...`, and then nothing: no
    error, no timeout, no exit. `codex exec review` takes no prompt argument and
    is unaffected, which is what makes the failure so easy to miss: the
    structured lens returns a normal review while the three prompted lenses are
    frozen, and the fleet reports a quarter of its coverage as if it were all of
    it. This is a real incident from this release's own review gate.
    """

    def test_the_default_runner_closes_stdin(self) -> None:
        import subprocess as sp

        seen: dict[str, object] = {}

        def fake_run(command, **kwargs):
            seen.update(kwargs)
            seen["command"] = command

            class _Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Result()

        original = sp.run
        review_module = sys.modules[run_codex_fleet.__module__]
        review_module.subprocess.run = fake_run
        try:
            review_module._default_runner(("codex", "exec", "a prompt"), Path("."))
        finally:
            review_module.subprocess.run = original

        self.assertIs(
            seen.get("stdin"),
            sp.DEVNULL,
            "a lens spawned with an inherited stdin can block forever on it",
        )

    def test_a_lens_that_hangs_is_never_reported_as_a_pass(self) -> None:
        # The shape of the incident: three lenses produce nothing at all. A
        # fleet must not call that a pass just because no lens errored.
        def silent_unless_structured(command):
            return "" if "review" not in command else ONE_NIT

        report = _fleet(
            runner=RecordingRunner(output=silent_unless_structured, exit_code=1)
        )
        self.assertFalse(report.passed)
        self.assertEqual(report.reason_code, "codex-lens-failed")


class LensCommandTests(unittest.TestCase):
    def _command_for(self, lens, commands):
        for command in commands:
            if lens == "structured-diff" and "review" in command:
                return command
            if lens != "structured-diff" and lens in " ".join(command):
                return command
        raise AssertionError(f"no command issued for {lens}")

    def test_structured_diff_uses_codex_exec_review_against_the_merge_base(self) -> None:
        runner = RecordingRunner()
        _fleet(runner=runner)
        command = self._command_for("structured-diff", runner.commands)
        joined = " ".join(command)
        self.assertIn("codex exec review", joined)
        self.assertIn("--base", joined)
        self.assertIn(MERGE_BASE_SHA, command)

    def test_the_base_argument_is_a_resolved_sha_not_a_shell_substitution(self) -> None:
        """The fleet runs argv directly — there is no shell to expand `$(...)`.

        Handing `codex` the literal string `"$(git merge-base origin/x HEAD)"`
        makes it review against a ref that does not exist, so the structured
        lens reviews nothing and the gate reports a pass it never performed.
        """
        runner = RecordingRunner()
        _fleet(runner=runner)
        command = self._command_for("structured-diff", runner.commands)
        base = command[command.index("--base") + 1]
        self.assertEqual(base, MERGE_BASE_SHA)
        for token in command:
            self.assertNotIn("$(", token)
            self.assertNotIn("merge-base", token)

    def test_building_a_structured_diff_command_requires_a_resolved_sha(self) -> None:
        with self.assertRaises(CodexGateError) as ctx:
            build_lens_command("structured-diff", "staging", merge_base=None)
        self.assertEqual(ctx.exception.reason, "codex-merge-base-unresolved")

        for bogus in ("", "   ", "origin/staging", "$(git merge-base origin/staging HEAD)"):
            with self.subTest(merge_base=bogus):
                with self.assertRaises(CodexGateError) as ctx:
                    build_lens_command("structured-diff", "staging", merge_base=bogus)
                self.assertEqual(ctx.exception.reason, "codex-merge-base-unresolved")


    def test_structured_diff_never_carries_a_custom_prompt(self) -> None:
        runner = RecordingRunner()
        _fleet(runner=runner)
        command = self._command_for("structured-diff", runner.commands)
        # `codex exec review` and a custom prompt are mutually exclusive: the
        # CLI rejects the combination, so a prompt here loses the whole review.
        self.assertNotIn("-s", command)
        for token in command:
            self.assertNotIn("lens", token.lower())

    def test_supplying_a_prompt_to_structured_diff_is_rejected(self) -> None:
        with self.assertRaises(CodexGateError) as ctx:
            build_lens_command("structured-diff", "staging", prompt="review this carefully")
        self.assertEqual(ctx.exception.reason, "codex-review-prompt-conflict")

        with self.assertRaises(CodexGateError) as ctx:
            _fleet(prompts={"structured-diff": "review this carefully"})
        self.assertEqual(ctx.exception.reason, "codex-review-prompt-conflict")

    def test_the_other_three_lenses_use_plain_exec_read_only_with_a_prompt(self) -> None:
        runner = RecordingRunner()
        _fleet(runner=runner)
        for lens in ("correctness", "security", "performance-design-consistency"):
            with self.subTest(lens=lens):
                command = self._command_for(lens, runner.commands)
                joined = " ".join(command)
                self.assertIn("codex exec", joined)
                self.assertNotIn("codex exec review", joined)
                self.assertIn("-s", command)
                self.assertIn("read-only", command)
                self.assertIn(lens, joined)

    def test_every_lens_uses_the_newest_model_at_maximum_effort(self) -> None:
        report = _fleet()
        for lens in report.lenses:
            with self.subTest(lens=lens.name):
                self.assertEqual(lens.model, CODEX_MODEL)
                self.assertEqual(lens.reasoning_effort, CODEX_REASONING_EFFORT)
                joined = " ".join(lens.command)
                self.assertIn(f"-m {CODEX_MODEL}", joined)
                self.assertIn(f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"', joined)

    def test_a_weaker_model_or_effort_fails_the_gate(self) -> None:
        for kwargs, reason in (
            ({"model": "gpt-5.4"}, "codex-model-invalid"),
            ({"model": "gpt-4o"}, "codex-model-invalid"),
            ({"reasoning_effort": "medium"}, "codex-reasoning-effort-invalid"),
            ({"reasoning_effort": "low"}, "codex-reasoning-effort-invalid"),
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(CodexGateError) as ctx:
                    _fleet(**kwargs)
                self.assertEqual(ctx.exception.reason, reason)


class MergeBaseResolutionTests(unittest.TestCase):
    def test_the_resolver_runs_git_merge_base_and_returns_the_sha(self) -> None:
        seen: list[tuple[str, ...]] = []

        def runner(command, cwd):
            seen.append(tuple(command))
            return {"exit_code": 0, "stdout": MERGE_BASE_SHA + "\n", "stderr": ""}

        sha = resolve_merge_base("staging", _REPO_ROOT, runner=runner)
        self.assertEqual(sha, MERGE_BASE_SHA)
        self.assertEqual(seen, [("git", "merge-base", "origin/staging", "HEAD")])

    def test_an_unresolvable_merge_base_fails_closed(self) -> None:
        def failing(command, cwd):
            return {"exit_code": 128, "stdout": "", "stderr": "not a valid object name"}

        with self.assertRaises(CodexGateError) as ctx:
            resolve_merge_base("staging", _REPO_ROOT, runner=failing)
        self.assertEqual(ctx.exception.reason, "codex-merge-base-unresolved")

    def test_garbage_on_stdout_is_not_accepted_as_a_sha(self) -> None:
        def chatty(command, cwd):
            return {"exit_code": 0, "stdout": "fatal: your branch is behind\n", "stderr": ""}

        with self.assertRaises(CodexGateError) as ctx:
            resolve_merge_base("staging", _REPO_ROOT, runner=chatty)
        self.assertEqual(ctx.exception.reason, "codex-merge-base-unresolved")

    def test_the_fleet_refuses_to_run_when_the_merge_base_cannot_be_resolved(self) -> None:
        def unresolvable(base_ref, worktree):
            raise CodexGateError("codex-merge-base-unresolved", "no merge base")

        runner = RecordingRunner()
        with self.assertRaises(CodexGateError) as ctx:
            _fleet(runner=runner, merge_base_resolver=unresolvable)
        self.assertEqual(ctx.exception.reason, "codex-merge-base-unresolved")
        self.assertEqual(runner.commands, [], "no lens may run without a real base")

    def test_plan_only_also_carries_the_resolved_sha(self) -> None:
        report = _fleet(plan_only=True)
        structured = next(l for l in report.lenses if l.name == "structured-diff")
        self.assertIn(MERGE_BASE_SHA, structured.command)


class FindingTests(unittest.TestCase):
    def test_findings_are_parsed_with_severity_and_location(self) -> None:
        findings = parse_findings("correctness", ONE_P1 + ONE_NIT)
        self.assertEqual([f.severity for f in findings], ["P1", "nit"])
        self.assertEqual(findings[0].location, "scripts/x.py:40")
        self.assertTrue(all(not f.resolved for f in findings))

    def test_a_nit_blocks_the_gate_exactly_like_a_p1(self) -> None:
        for output in (ONE_NIT, ONE_P1):
            with self.subTest(output=output.strip()):
                report = _fleet(runner=RecordingRunner(output=output))
                self.assertFalse(report.passed)
                self.assertEqual(report.reason_code, "codex-findings-unresolved")

    def test_findings_from_every_lens_are_collected_and_deduped(self) -> None:
        report = _fleet(runner=RecordingRunner(output=ONE_NIT))
        # The same finding surfaced by four lenses is one finding to fix.
        self.assertEqual(len(report.findings), 1)
        self.assertEqual(report.findings[0].severity, "nit")

    def test_a_finding_is_resolved_only_with_committed_evidence(self) -> None:
        findings = parse_findings("correctness", ONE_NIT)
        self.assertFalse(resolve_findings(findings, {})[0].resolved)
        self.assertFalse(
            resolve_findings(findings, {"scripts/x.py:12": ""})[0].resolved,
            "an empty evidence string does not resolve a finding",
        )
        resolved = resolve_findings(findings, {"scripts/x.py:12": "commit a1b2c3d"})
        self.assertTrue(resolved[0].resolved)
        self.assertEqual(resolved[0].evidence, "commit a1b2c3d")

    def test_resolving_every_finding_passes_the_gate(self) -> None:
        report = _fleet(
            runner=RecordingRunner(output=ONE_NIT),
            resolutions={"scripts/x.py:12": "commit a1b2c3d"},
        )
        self.assertTrue(report.passed)

    def test_a_failed_lens_fails_the_gate_even_with_no_findings(self) -> None:
        report = _fleet(runner=RecordingRunner(exit_code=2))
        self.assertFalse(report.passed)
        self.assertEqual(report.reason_code, "codex-lens-failed")


class SingleRunTests(unittest.TestCase):
    def test_a_second_automatic_fleet_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            first = _fleet(ledger=ledger)
            self.assertTrue(first.passed)
            with self.assertRaises(CodexGateError) as ctx:
                _fleet(ledger=ledger)
            self.assertEqual(ctx.exception.reason, "codex-fleet-already-run")

    def test_an_explicit_user_re_review_is_permitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            _fleet(ledger=ledger)
            second = _fleet(ledger=ledger, force_rerun=True)
            self.assertTrue(second.passed)

    def test_a_different_pull_request_runs_its_own_fleet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "ledger.json"
            _fleet(ledger=ledger)
            other = _fleet(
                ledger=ledger,
                pull_request_url="https://github.com/Bavariance/polysimulator/pull/457",
            )
            self.assertTrue(other.passed)


class DocumentationExemptionTests(unittest.TestCase):
    def test_a_documentation_only_diff_is_exempt(self) -> None:
        runner = RecordingRunner()
        report = _fleet(documentation_only=True, runner=runner)
        self.assertTrue(report.passed)
        self.assertEqual(report.lenses, ())
        self.assertEqual(report.reason_code, "documentation-only-exempt")
        self.assertEqual(runner.commands, [], "an exempt diff must not invoke Codex")

    def test_documentation_only_detection(self) -> None:
        self.assertTrue(is_documentation_only(["docs/a.md", "README.md", "CLAUDE.md"]))
        self.assertFalse(is_documentation_only(["docs/a.md", "scripts/run.sh"]))
        self.assertFalse(is_documentation_only([]), "an empty diff is not an exemption")


class RawOutputTests(unittest.TestCase):
    def test_raw_output_is_written_outside_the_git_tree(self) -> None:
        directory = raw_output_dir()
        self.assertFalse(
            str(directory).startswith(str(_REPO_ROOT)),
            f"raw Codex output must not land inside the repository: {directory}",
        )

    def test_the_published_summary_carries_no_raw_output(self) -> None:
        secret_shaped = "gh" + "p_" + ("N" * 36)
        report = _fleet(runner=RecordingRunner(output=f"{ONE_NIT}debug: {secret_shaped}\n"))
        summary = report.published_summary()
        self.assertNotIn(secret_shaped, summary)
        self.assertIn("nit", summary)


class CliTests(unittest.TestCase):
    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = codex_cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_the_cli_reports_the_planned_fleet_without_running_codex(self) -> None:
        code, out, _ = self._run(
            [
                "run",
                "--base",
                "staging",
                "--worktree",
                str(_REPO_ROOT),
                "--merge-base",
                MERGE_BASE_SHA,
                "--plan-only",
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        body = json.loads(out)
        self.assertEqual(len(body["lenses"]), 4)
        for lens in body["lenses"]:
            self.assertEqual(lens["model"], CODEX_MODEL)
            self.assertEqual(lens["reasoning_effort"], CODEX_REASONING_EFFORT)
        structured = next(l for l in body["lenses"] if l["name"] == "structured-diff")
        self.assertIn(MERGE_BASE_SHA, structured["command"])

    def test_the_cli_refuses_a_merge_base_that_is_not_a_commit(self) -> None:
        code, _, err = self._run(
            [
                "run",
                "--base",
                "staging",
                "--worktree",
                str(_REPO_ROOT),
                "--merge-base",
                "origin/staging",
                "--plan-only",
                "--json",
            ]
        )
        self.assertEqual(code, 65)
        self.assertIn("codex-merge-base-unresolved", err)

    def test_the_cli_reports_the_documentation_exemption(self) -> None:
        code, out, _ = self._run(
            [
                "run",
                "--base",
                "staging",
                "--worktree",
                str(_REPO_ROOT),
                "--changed-file",
                "docs/a.md",
                "--changed-file",
                "README.md",
                "--merge-base",
                MERGE_BASE_SHA,
                "--plan-only",
                "--json",
            ]
        )
        self.assertEqual(code, 0)
        body = json.loads(out)
        self.assertEqual(body["reason_code"], "documentation-only-exempt")
        self.assertEqual(body["lenses"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
