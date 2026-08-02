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
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.config import ConfigError, load_and_validate_config  # noqa: E402
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
            (root / "docs" / "why.md").write_text("never call gh pr merge\n", encoding="utf-8")
            self.assertTrue(scan_merge_prohibitions(root, allowlist=("docs/",)).clean)
            self.assertFalse(scan_merge_prohibitions(root, allowlist=()).clean)


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
        #       config, or a project board that could not be read. An unreadable
        #       board is deliberately NOT its own code: it is the same "we do not
        #       have usable input" halt, and it must never be confused with the
        #       empty board it used to be silently converted into.
        allowed_repeats = {"74", "65"}
        repeated = {code for code in codes if codes.count(code) > 1}
        self.assertEqual(
            repeated, allowed_repeats, f"colliding exit codes: {sorted(repeated)}"
        )


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
