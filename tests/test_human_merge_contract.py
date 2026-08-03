"""Task 11 — the runtime never merges. A human does, by rebase.

Pure stdlib `unittest`. No network, no `gh`.

`scan_merge_prohibitions` is a release gate, not a lint: it source-scans every
executable runtime, workflow, skill, and reviewer path for all eight ways a
merge can happen, and ANY active occurrence fails. Documentation that describes
the prohibition and fixtures that intentionally seed it are excluded by an
explicit allowlist FILE — never by a loose path heuristic, because a heuristic
like "skip anything under docs/" is exactly how a real merge path hides.

Run directly:
  python -B tests/test_human_merge_contract.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_human_merge_contract.py' -v
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.config import ConfigError, load_and_validate_config  # noqa: E402
from super_board_runtime.install_manifest import plan_install_payload  # noqa: E402
from super_board_runtime.review import (  # noqa: E402
    ALLOWLIST_FILENAME,
    DONE_WRITER,
    MERGE_MECHANISMS,
    REQUIRED_REPOSITORY_SETTINGS,
    MergeContractError,
    load_allowlist,
    may_write_done,
    review_handoff,
    scan_merge_prohibitions,
    scan_retired_status,
    verify_human_merge_config,
    verify_repository_settings,
)

SEED = _REPO_ROOT / "tests" / "fixtures" / "merge-prohibition-seed"


def _config(**overrides: object):
    payload = {
        "version": 1,
        "project": {"owner": "Bavariance", "number": 1},
        "repo": {"remote": "Bavariance/polysimulator"},
        "base_branch": "staging",
        "activation_mode": "off",
    }
    payload.update(overrides)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return load_and_validate_config(path)


class SeededTreeTests(unittest.TestCase):
    def test_all_eight_mechanisms_are_detected(self) -> None:
        report = scan_merge_prohibitions(SEED, allowlist=())
        self.assertFalse(report.clean)
        found = {occurrence.mechanism for occurrence in report.occurrences}
        self.assertEqual(
            found,
            set(MERGE_MECHANISMS),
            f"undetected mechanisms: {sorted(set(MERGE_MECHANISMS) - found)}",
        )

    def test_every_occurrence_records_path_line_and_mechanism(self) -> None:
        report = scan_merge_prohibitions(SEED, allowlist=())
        for occurrence in report.occurrences:
            self.assertTrue(occurrence.path)
            self.assertGreater(occurrence.line, 0)
            self.assertIn(occurrence.mechanism, MERGE_MECHANISMS)

    def test_there_are_exactly_eight_scanned_mechanisms(self) -> None:
        self.assertEqual(len(MERGE_MECHANISMS), 8)


class RealTreeTests(unittest.TestCase):
    def test_the_repository_has_zero_active_merge_paths(self) -> None:
        report = scan_merge_prohibitions(_REPO_ROOT)
        detail = "\n".join(
            f"  {o.path}:{o.line} — {o.mechanism}" for o in report.occurrences
        )
        self.assertTrue(report.clean, f"active merge paths found:\n{detail}")

    def test_the_repository_has_no_active_skipped_status(self) -> None:
        report = scan_retired_status(_REPO_ROOT)
        detail = "\n".join(f"  {o.path}:{o.line}" for o in report.occurrences)
        self.assertTrue(report.clean, f"`Skipped` still on an active surface:\n{detail}")


class InstalledTreeTests(unittest.TestCase):
    """The gate has to be runnable where the runtime actually runs.

    `merge-scan-allowlist.txt` lives at the REPOSITORY root and is not part of
    the payload, so on an installed tree it does not exist. With no allowlist
    the scanner flagged its own source — twelve occurrences of the patterns it
    is built out of — and could never report clean. A safety gate that exists in
    the repository and evaporates on installation is the exact inverse of a
    safety gate.

    These two tests are the pair that matters: the first proves the gate can
    pass on an installed tree, the second proves it still bites there.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.tree = Path(cls._tmp.name)
        for item in plan_install_payload(_REPO_ROOT):
            destination = cls.tree / item.target
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((_REPO_ROOT / item.source).read_bytes())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_the_installed_payload_scans_clean_with_no_repository_root(self) -> None:
        self.assertFalse(
            (self.tree / ALLOWLIST_FILENAME).exists(),
            "an installed tree has no repository-root allowlist; the fixture must not fake one",
        )
        report = scan_merge_prohibitions(self.tree / ".claude")
        detail = "\n".join(f"  {o.path}:{o.line} — {o.mechanism}" for o in report.occurrences)
        self.assertTrue(report.clean, f"active merge paths on an installed tree:\n{detail}")

    def test_an_active_merge_mechanism_in_the_installed_tree_is_caught(self) -> None:
        rogue = self.tree / ".claude" / "bin" / "rogue-lane.sh"
        rogue.write_text("#!/usr/bin/env bash\ngh pr merge \"$1\" --rebase\n", encoding="utf-8")
        try:
            report = scan_merge_prohibitions(self.tree / ".claude")
            self.assertFalse(report.clean, "the gate stopped biting on an installed tree")
            self.assertEqual(
                [(o.path, o.mechanism) for o in report.occurrences],
                [("bin/rogue-lane.sh", "cli-merge-subcommand")],
            )
        finally:
            rogue.unlink()


class SelfExclusionTests(unittest.TestCase):
    """The scanner must not flag its own implementation — intrinsically.

    Self-exclusion used to come from a line in an external file. That file is
    not shipped, so the exclusion did not exist where the scan was run. The
    scanner now recognises its own module by the package-relative path it was
    imported from, which travels with the code instead of beside it.
    """

    def test_the_scanner_does_not_flag_its_own_source(self) -> None:
        report = scan_merge_prohibitions(_REPO_ROOT / "scripts", allowlist=())
        flagged = {o.path for o in report.occurrences}
        self.assertNotIn("super_board_runtime/review.py", flagged)

    def test_self_exclusion_follows_the_module_to_an_installed_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "bin" / "super_board_runtime"
            package.mkdir(parents=True)
            (package / "review.py").write_bytes(
                (_REPO_ROOT / "scripts" / "super_board_runtime" / "review.py").read_bytes()
            )
            self.assertTrue(scan_merge_prohibitions(root, allowlist=()).clean)

    def test_an_impostor_at_the_scanner_path_is_still_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "super_board_runtime"
            package.mkdir(parents=True)
            (package / "review.py").write_text(
                "#!/usr/bin/env python3\nrun('gh pr merge 42 --rebase')\n", encoding="utf-8"
            )
            report = scan_merge_prohibitions(root, allowlist=())
            self.assertFalse(
                report.clean,
                "a file that merely occupies the scanner's path is not the scanner",
            )


class ProhibitionStatementTests(unittest.TestCase):
    """A statement of the rule is not an instance of the thing it forbids.

    The pattern heuristic could not tell prose asserting "the runtime never
    enables auto-merge" from an instruction to enable it, so every document
    that stated the rule had to be named in the allowlist. That is how five
    dead exclusions accumulated, and how a real merge path in an allowlisted
    file would have gone unseen.

    The distinction is negation in the statement's own scope, and it is
    deliberately NOT available inside a fenced code block — a command cannot be
    excused by the paragraph above it.
    """

    def _scan(self, name: str, body: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_text(body, encoding="utf-8")
            return scan_merge_prohibitions(Path(tmp), allowlist=())

    def test_a_negated_sentence_is_a_prohibition(self) -> None:
        report = self._scan("rule.md", "The runtime never enables auto-merge.\n")
        self.assertTrue(report.clean)

    def test_a_list_inherits_the_negation_from_its_introduction(self) -> None:
        body = "Reviewer may not, on any path:\n\n- enable auto-merge;\n- squash.\n"
        self.assertTrue(self._scan("rule.md", body).clean)

    def test_an_unnegated_instruction_is_still_an_active_mechanism(self) -> None:
        report = self._scan("lane.md", "When CI is green, enable auto-merge on the PR.\n")
        self.assertFalse(report.clean)
        self.assertEqual(report.occurrences[0].mechanism, "auto-merge-enablement")

    def test_a_fenced_command_is_never_excused_by_surrounding_prose(self) -> None:
        body = "The runtime never merges and never enables auto-merge:\n\n```bash\ngh pr merge 42 --rebase\n```\n"
        report = self._scan("rule.md", body)
        self.assertFalse(report.clean, "a fenced command is an instruction, not prose")
        self.assertEqual(
            [o.mechanism for o in report.occurrences], ["cli-merge-subcommand"]
        )

    def test_prose_relief_does_not_apply_to_executable_lines(self) -> None:
        body = "# the runtime never merges; this is not allowed\ngh pr merge 42 --rebase\n"
        report = self._scan("lane.sh", body)
        self.assertFalse(report.clean)
        self.assertEqual([o.line for o in report.occurrences], [2])

    def test_a_negated_comment_in_source_is_a_prohibition(self) -> None:
        report = self._scan("lane.sh", "# never call `gh pr merge` from a lane\ntrue\n")
        self.assertTrue(report.clean)

    def test_an_unrelated_earlier_paragraph_does_not_excuse_a_later_one(self) -> None:
        body = "Nothing here is permitted.\n\nEnable auto-merge once CI is green.\n"
        self.assertFalse(self._scan("lane.md", body).clean)


class ConfigAssignmentTests(unittest.TestCase):
    """Assigning a config value is not a merge invocation.

    `merge_method=merge_method,` in `config.py` matched `merge_method\\s*[:=]\\s*
    ["']?(squash|merge)` because `merge` is a prefix of the identifier
    `merge_method`. The line passes a validated, rebase-only value into a
    dataclass; it merges nothing.
    """

    def _scan(self, body: str):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.py"
            path.write_text(body, encoding="utf-8")
            return scan_merge_prohibitions(Path(tmp), allowlist=())

    def test_passing_a_variable_through_is_not_a_merge_method_literal(self) -> None:
        self.assertTrue(self._scan("    merge_method=merge_method,\n").clean)
        self.assertTrue(self._scan("    merge_method = merge_method_default\n").clean)

    def test_a_literal_squash_or_merge_value_is_still_caught(self) -> None:
        for body in (
            'PAYLOAD = {"merge_method": "squash"}\n',
            'PAYLOAD = {"merge_method": "merge"}\n',
            "merge_method=squash\n",
        ):
            with self.subTest(body=body.strip()):
                report = self._scan(body)
                self.assertFalse(report.clean, body)
                self.assertEqual(report.occurrences[0].mechanism, "squash-or-merge-commit")


class AllowlistTests(unittest.TestCase):
    def test_the_allowlist_is_an_explicit_file(self) -> None:
        path = _REPO_ROOT / ALLOWLIST_FILENAME
        self.assertTrue(path.is_file(), f"{ALLOWLIST_FILENAME} must exist at the repository root")
        entries = load_allowlist(_REPO_ROOT)
        self.assertTrue(entries, "the allowlist must name the excluded paths explicitly")
        # The seed tree and this test file are excluded by NAME, not by a
        # "tests/ is probably fine" heuristic.
        self.assertIn("tests/fixtures/merge-prohibition-seed/", entries)
        self.assertIn("tests/test_human_merge_contract.py", entries)

    def test_a_path_outside_the_allowlist_is_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rogue.sh").write_text("gh pr merge 42 --rebase\n", encoding="utf-8")
            report = scan_merge_prohibitions(root, allowlist=("something/else",))
            self.assertFalse(report.clean)
            self.assertEqual(report.occurrences[0].mechanism, "cli-merge-subcommand")

    def test_an_allowlisted_path_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "rogue.sh").write_text("gh pr merge 42 --rebase\n", encoding="utf-8")
            report = scan_merge_prohibitions(root, allowlist=("rogue.sh",))
            self.assertTrue(report.clean)

    def test_a_directory_entry_excludes_its_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            # An unnegated instruction — the subtree exclusion has to be what
            # suppresses this, not the prohibition-statement rule.
            (root / "docs" / "why.md").write_text(
                "Finish the release with `gh pr merge`.\n", encoding="utf-8"
            )
            self.assertTrue(scan_merge_prohibitions(root, allowlist=("docs/",)).clean)
            self.assertFalse(scan_merge_prohibitions(root, allowlist=()).clean)


class ShippedPayloadAutoMergeTests(unittest.TestCase):
    """The payload may STATE the merge prohibition. It may not build the concept.

    A worker instruction that classifies a pull request as "auto-merge eligible",
    stamps a literal `auto-merge-candidate` label on it, and then runs an
    "Auto-merge gate" has established an auto-merge mechanism in everything but
    the final call. Nothing consuming the label today is not a defence — it is
    the reason the next person to wire something to it creates a real merge path
    without ever editing a line that looks like a merge.

    The shipped contract is `human_approves_merge: true`, `merge_method: rebase`,
    and every pull request stopping at `Review` for a human. There is no
    auto-merge eligibility, no auto-merge label, and no auto-merge gate anywhere
    in it.
    """

    #: Literals that only exist if somebody built the concept. Assembled from
    #: fragments so this file's own assertions are not themselves the string a
    #: future grep of the payload trips over.
    _LABEL = "auto-merge" + "-candidate"

    FORBIDDEN = (
        ("auto-merge label", re.compile(re.escape(_LABEL), re.IGNORECASE)),
        ("auto-merge gate", re.compile(r"auto[-_ ]merge\s+gate", re.IGNORECASE)),
        (
            "auto-merge eligibility",
            re.compile(r"auto[-_ ]merge\s+(?:elig|candidat)", re.IGNORECASE),
        ),
        (
            "merge-eligibility classification",
            re.compile(r"merge[-_ ]elig\w*", re.IGNORECASE),
        ),
    )

    def _payload_sources(self) -> list[Path]:
        return [
            _REPO_ROOT / item.source
            for item in plan_install_payload(_REPO_ROOT)
            if Path(item.source).suffix.lower()
            in {".md", ".sh", ".py", ".js", ".mjs", ".yml", ".yaml"}
        ]

    def test_the_payload_establishes_no_auto_merge_concept(self) -> None:
        offences: list[str] = []
        for path in self._payload_sources():
            relative = path.relative_to(_REPO_ROOT).as_posix()
            for number, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
            ):
                for name, pattern in self.FORBIDDEN:
                    if pattern.search(line):
                        offences.append(f"  {relative}:{number} — {name}")
        self.assertEqual(
            offences,
            [],
            "the shipped payload establishes an auto-merge concept it forbids:\n"
            + "\n".join(offences),
        )

    def test_the_qa_iteration_preamble_hands_a_passing_pull_request_to_review(self) -> None:
        text = (
            _REPO_ROOT / "skills" / "super-qa" / "references" / "iteration-preamble.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Review", text)
        self.assertRegex(
            text,
            r"human[^.\n]{0,40}rebase[- ]merge",
            "the preamble must say a human rebase-merges",
        )

    def test_the_qa_iteration_preamble_creates_no_label_on_the_command_line(self) -> None:
        text = (
            _REPO_ROOT / "skills" / "super-qa" / "references" / "iteration-preamble.md"
        ).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "--label" in line:
                self.assertNotRegex(
                    line,
                    r"auto[-_ ]merge",
                    f"a merge-eligibility label is still applied: {line.strip()}",
                )


class ConfigContractTests(unittest.TestCase):
    def test_human_approves_merge_must_be_true(self) -> None:
        verify_human_merge_config(_config())
        with self.assertRaises(MergeContractError) as ctx:
            verify_human_merge_config(_config(human_approves_merge=False))
        self.assertEqual(ctx.exception.reason, "human-approves-merge-required")

    def test_merge_method_must_be_rebase(self) -> None:
        self.assertEqual(_config().merge_method, "rebase")
        # The config layer already refuses anything else outright.
        with self.assertRaises(ConfigError) as ctx:
            _config(merge_method="squash")
        self.assertEqual(ctx.exception.reason, "merge-method-must-be-rebase")

    def test_the_required_repository_settings_are_pinned(self) -> None:
        self.assertEqual(
            REQUIRED_REPOSITORY_SETTINGS,
            {
                "allow_merge_commit": False,
                "allow_rebase_merge": True,
                "allow_squash_merge": False,
            },
        )

    def test_repository_settings_are_verified(self) -> None:
        verify_repository_settings(dict(REQUIRED_REPOSITORY_SETTINGS))
        for key, wrong in (
            ("allow_rebase_merge", False),
            ("allow_squash_merge", True),
            ("allow_merge_commit", True),
        ):
            with self.subTest(setting=key):
                settings = dict(REQUIRED_REPOSITORY_SETTINGS)
                settings[key] = wrong
                with self.assertRaises(MergeContractError) as ctx:
                    verify_repository_settings(settings)
                self.assertEqual(ctx.exception.reason, f"repository-setting-invalid:{key}")

    def test_an_unreadable_setting_fails_closed(self) -> None:
        settings = dict(REQUIRED_REPOSITORY_SETTINGS)
        del settings["allow_squash_merge"]
        with self.assertRaises(MergeContractError) as ctx:
            verify_repository_settings(settings)
        self.assertEqual(ctx.exception.reason, "repository-setting-invalid:allow_squash_merge")


class AllowlistHygieneTests(unittest.TestCase):
    """Every exclusion must be load-bearing.

    An entry that excludes a file carrying no scanned literal is dead weight
    that still excludes the file — so on the day somebody adds a real merge path
    to it, the gate stays green and nobody is told. That is exactly the failure
    the "no path heuristic" rule at the top of the allowlist exists to prevent,
    reached one stale line at a time instead of all at once.
    """

    def test_no_allowlist_entry_is_stale(self) -> None:
        entries = list(load_allowlist(_REPO_ROOT))
        self.assertTrue(entries, "the allowlist is empty")
        stale: list[str] = []
        for entry in entries:
            others = [other for other in entries if other != entry]
            hits = [
                occurrence.path
                for report in (
                    scan_merge_prohibitions(_REPO_ROOT, allowlist=others),
                    scan_retired_status(_REPO_ROOT, allowlist=others),
                )
                for occurrence in report.occurrences
            ]
            covered = any(
                path == entry.rstrip("/") or path.startswith(entry) for path in hits
            )
            if not covered:
                stale.append(entry)
        self.assertEqual(
            stale,
            [],
            "these exclusions no longer exclude anything and must be deleted:\n"
            + "\n".join(stale),
        )


class ExitCodeCollisionTests(unittest.TestCase):
    """The production-merge guard needs its own code.

    It used to exit 75, which G9 assigns to "quota unavailable or the immutable
    GraphQL reserve was reached". Two unrelated halts sharing one code makes the
    halt unreadable to every caller downstream: a supervisor cannot tell "wait
    for the quota window" from "this board is misconfigured for production".
    """

    def setUp(self) -> None:
        self.run_sh = (_REPO_ROOT / "scripts" / "super-board-run.sh").read_text(encoding="utf-8")

    def test_the_production_merge_guard_no_longer_exits_75(self) -> None:
        guard = self.run_sh.split("# Production-merge guard.", 1)[1].split("\nfi\n", 1)[0]
        self.assertIn("exit 76", guard)
        self.assertNotIn("exit 75", guard)

    def test_75_still_belongs_to_the_graphql_reserve(self) -> None:
        quota = self.run_sh.split("gh_quota_guard() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("exit 75", quota)

    def test_every_dispatcher_halt_code_is_distinct(self) -> None:
        import re

        codes = re.findall(r"^\s*exit (\d+)$", self.run_sh, re.MULTILINE)
        # A code may repeat only when both halts mean the SAME thing — the rule
        # exists so a supervisor can read the code, not so codes are unique:
        #   74  workflow-wave mutual exclusion, at startup and again mid-run;
        #   65  the runtime's input contract could not be satisfied — an invalid
        #       config, a config that is not there at all, or a project board
        #       that could not be read. An unreadable board is deliberately NOT
        #       its own code: it is the same "we do not have usable input" halt,
        #       and it must never be confused with the empty board it used to be
        #       silently converted into.
        #   64  the wrong command surface — no config slug to run, and running a
        #       workflow-backend board on the `claude -p` dispatcher. Both say
        #       "this invocation is not the one to make", which is what 64 means.
        allowed_repeats = {"74", "65", "64"}
        repeated = {code for code in codes if codes.count(code) > 1}
        self.assertEqual(
            repeated, allowed_repeats, f"colliding exit codes: {sorted(repeated)}"
        )


class ExitCodeContractTests(unittest.TestCase):
    """Every shell entry point halts with a code the contract defines.

    `super_board_runtime.__init__` publishes the contract, and a halt outside it
    is unreadable to the supervisor that has to act on it: 66 is not "invalid
    config" anywhere in this runtime, 73 is not "budget exhausted", and 78 is
    reserved for evidence rejected at the publication boundary — using it for a
    board pointed at the wrong backend told operators a secret had leaked.
    """

    #: 0 success · 3 conflict, nothing changed · 64 invalid invocation ·
    #: 65 invalid configuration or input · 69 auth/identity · 75 quota reserve ·
    #: 76 production-merge guard · 78 unsafe evidence.
    CONTRACT = {"0", "1", "3", "64", "65", "69", "75", "76", "78"}

    #: Dispatcher-local mutual-exclusion halts, documented in
    #: `super-board-run.sh` at the point they fire. They are NOT contract codes
    #: and no runtime CLI returns them; they exist so a supervisor can tell
    #: "claude workers are already running" (73) from "a workflow wave holds the
    #: board" (74), which no contract code distinguishes.
    DISPATCHER_MUTUAL_EXCLUSION = {"73", "74"}

    ENTRY_POINTS = (
        "scripts/super-board-run.sh",
        "scripts/super-board-stop.sh",
        "scripts/super-board-wave-plan.sh",
        "scripts/super-board-gh-guard.sh",
        "scripts/super-qa-dispatch.sh",
        "scripts/super-qa-file-bug.sh",
    )

    def test_no_entry_point_halts_outside_the_contract(self) -> None:
        import re

        pattern = re.compile(r"\b(?:exit|return) (\d+)\b")
        allowed = self.CONTRACT | self.DISPATCHER_MUTUAL_EXCLUSION
        for relative in self.ENTRY_POINTS:
            source = (_REPO_ROOT / relative).read_text(encoding="utf-8")
            for line in source.splitlines():
                if line.lstrip().startswith("#"):
                    continue  # prose about a code is not a halt with it
                for code in pattern.findall(line):
                    with self.subTest(entry_point=relative, code=code):
                        self.assertIn(
                            code,
                            allowed,
                            f"{relative} halts with {code}, which the exit-code contract "
                            f"does not define",
                        )

    def test_a_missing_config_is_65_everywhere(self) -> None:
        for relative in (
            "scripts/super-board-run.sh",
            "scripts/super-board-stop.sh",
            "scripts/super-board-wave-plan.sh",
        ):
            with self.subTest(entry_point=relative):
                source = (_REPO_ROOT / relative).read_text(encoding="utf-8")
                found = [
                    line for line in source.splitlines() if "config not found" in line
                ]
                self.assertTrue(found, f"{relative} no longer refuses a missing config")
                for line in found:
                    if line.lstrip().startswith("#"):
                        continue
                    self.assertIn("65", line, "a missing config is an invalid input contract")

    def test_the_wrong_backend_is_a_command_surface_halt_not_unsafe_evidence(self) -> None:
        run_sh = (_REPO_ROOT / "scripts" / "super-board-run.sh").read_text(encoding="utf-8")
        guard = run_sh.split('if [ "$WORKER_BACKEND" != "claude-p" ]; then', 1)[1].split(
            "\nfi\n", 1
        )[0]
        self.assertIn("exit 64", guard)
        self.assertNotIn(
            "exit 78",
            guard,
            "78 means evidence was rejected at the publication boundary; a board on the "
            "wrong backend has published nothing",
        )

    def test_an_exhausted_worker_budget_is_the_quota_code(self) -> None:
        guard = (_REPO_ROOT / "scripts" / "super-board-gh-guard.sh").read_text(encoding="utf-8")
        spend = guard.split("sb_gh_budget_spend() {", 1)[1].split("\n}\n", 1)[0]
        # Prose about the retired code is not a halt with it.
        code = "\n".join(
            line for line in spend.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertIn("return 75", code, "budget exhaustion is a quota halt")
        self.assertNotIn("return 73", code)


class HandoffTests(unittest.TestCase):
    def test_a_successful_review_stops_in_review_with_a_handoff_record(self) -> None:
        record = review_handoff(
            issue_url="https://github.com/Bavariance/polysimulator/issues/123",
            pull_request_url="https://github.com/Bavariance/polysimulator/pull/456",
            tested_sha="a" * 40,
            merge_ready=True,
        )
        self.assertEqual(record.next_status, "Review")
        self.assertFalse(record.merged)
        self.assertTrue(record.merge_ready)
        self.assertEqual(record.merge_method, "rebase")
        self.assertEqual(record.awaiting, "human-rebase-merge")

    def test_a_review_that_is_not_merge_ready_still_stops_in_review(self) -> None:
        record = review_handoff(
            issue_url="https://github.com/Bavariance/polysimulator/issues/123",
            pull_request_url="https://github.com/Bavariance/polysimulator/pull/456",
            tested_sha="a" * 40,
            merge_ready=False,
            reason_code="head-moved",
        )
        self.assertEqual(record.next_status, "Review")
        self.assertFalse(record.merge_ready)
        self.assertEqual(record.reason_code, "head-moved")

    def test_a_handoff_can_never_report_done(self) -> None:
        for merge_ready in (True, False):
            record = review_handoff(
                issue_url="u", pull_request_url="p", tested_sha="a" * 40, merge_ready=merge_ready
            )
            self.assertNotEqual(record.next_status, "Done")
            self.assertFalse(record.merged)


class DoneOwnershipTests(unittest.TestCase):
    def test_only_the_closure_normalizer_may_write_done(self) -> None:
        self.assertEqual(DONE_WRITER, "closure-normalizer")
        self.assertTrue(may_write_done(DONE_WRITER, merged_externally=True))

    def test_no_runtime_actor_may_write_done(self) -> None:
        for actor in ("dispatcher", "builder", "tester", "reviewer", "workflow", "super-review"):
            with self.subTest(actor=actor):
                self.assertFalse(may_write_done(actor, merged_externally=True))

    def test_the_normalizer_may_not_write_done_without_a_confirmed_merge(self) -> None:
        self.assertFalse(may_write_done(DONE_WRITER, merged_externally=False))


class ProgressReportingTests(unittest.TestCase):
    def test_handoff_in_review_is_reported_separately_from_done(self) -> None:
        record = review_handoff(
            issue_url="u", pull_request_url="p", tested_sha="a" * 40, merge_ready=True
        )
        body = record.to_dict()
        self.assertIn("awaiting", body)
        self.assertEqual(body["awaiting"], "human-rebase-merge")
        self.assertEqual(body["completed_by"], None)
        self.assertNotEqual(body["next_status"], "Done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
