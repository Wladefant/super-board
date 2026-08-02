"""Task 15 — installation is pinned, complete, verifiable, and idempotent.

Pure stdlib `unittest`. No network, no `gh`.

An installer that "mostly works" is worse than one that fails: half a payload
produces a board that dispatches with an old dispatcher and a new skill, and the
symptom shows up three steps later as a policy that should have been impossible.
So this installer is manifest-driven and fails closed — a missing source asset
is `install-payload-incomplete`, and a source tree whose HEAD is not the pinned
SHA is refused outright.

**The idempotency proof, and why it is done this way.** Install once, snapshot
the installed tree by path and checksum, install the same release again,
snapshot again, and compare the two snapshots. Zero added, removed, changed, or
ownership-shifted entries. The proof deliberately does NOT diff the working
tree against the pre-install checkout: that diff is dominated by the first
install's own output, so it can be "clean" while the second install quietly
rewrites half the payload — and it cannot see an ownership shift at all.

Run directly:
  python -B tests/test_install_manifest.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_install_manifest.py' -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime.install_manifest import (  # noqa: E402
    CONFIG_SCHEMA_VERSION,
    FORBIDDEN_INSTALL_PATHS,
    INSTALLED_BIN_SCRIPTS,
    INSTALLED_GITHUB_PAYLOAD,
    INSTALLED_SKILLS,
    INSTALLED_WORKFLOWS,
    MANIFEST_RELATIVE_PATH,
    SOURCE_REPOSITORY,
    InstalledFile,
    InstallError,
    InstallManifest,
    build_install_manifest,
    compare_install_snapshots,
    install_payload,
    is_downgrade,
    plan_install_payload,
    snapshot_install_tree,
    stale_files,
    verify_install_manifest,
    verify_source_sha,
)

SOURCE_SHA = "a" * 40
DESIGN_SOURCE = "https://github.com/Wladefant/super-board-design"
DESIGN_SHA = "b" * 40
DESIGN_CHECKSUM = "c" * 64
INSTALL_SH = _REPO_ROOT / "install.sh"
INSTALL_TEST_SH = _REPO_ROOT / "tests" / "test-install.sh"
VERIFY_CLI = _REPO_ROOT / "scripts" / "super-board-install-verify.py"


def _install(target: Path, *, release_version: str = "9.0.0", **overrides) -> InstallManifest:
    kwargs = {
        "source_sha": SOURCE_SHA,
        "release_version": release_version,
        "design_skill_source": DESIGN_SOURCE,
        "design_skill_sha": DESIGN_SHA,
        "design_skill_checksum": DESIGN_CHECKSUM,
        "user_home": str(target / "home"),
        "slug": "polysimulator",
        "installed_at": "2026-08-02T12:00:00Z",
    }
    kwargs.update(overrides)
    return install_payload(_REPO_ROOT, target, **kwargs)


class _Target:
    """A throwaway repository root to install into."""

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        return Path(self._tmp.name)

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


# ───────────────────────────── the payload ─────────────────────────────


class PayloadCompletenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.items = plan_install_payload(_REPO_ROOT)
        self.targets = {item.target for item in self.items}

    def test_every_skill_is_installed(self) -> None:
        for skill in INSTALLED_SKILLS:
            with self.subTest(skill=skill):
                prefix = f".claude/skills/{skill}/"
                self.assertTrue(
                    any(target.startswith(prefix) for target in self.targets),
                    f"{skill} is not in the payload",
                )

    def test_every_entry_point_is_installed_into_claude_bin(self) -> None:
        for script in INSTALLED_BIN_SCRIPTS:
            with self.subTest(script=script):
                self.assertIn(f".claude/bin/{script}", self.targets)

    def test_the_comment_sweep_and_qa_assets_are_installed_and_executable(self) -> None:
        for script in (
            "super-board-sweep-comments.mjs",
            "super-qa-dispatch.sh",
            "super-qa-file-bug.sh",
        ):
            with self.subTest(script=script):
                item = next(i for i in self.items if i.target == f".claude/bin/{script}")
                self.assertTrue(item.executable, f"{script} must be installed executable")

    def test_the_runtime_package_is_installed_beside_the_entry_points(self) -> None:
        self.assertIn(".claude/bin/super_board_runtime/__init__.py", self.targets)
        self.assertIn(".claude/bin/super_board_runtime/normalize.py", self.targets)

    def test_the_dynamic_workflow_is_installed(self) -> None:
        for workflow in INSTALLED_WORKFLOWS:
            with self.subTest(workflow=workflow):
                self.assertIn(f".claude/workflows/{workflow}", self.targets)

    def test_the_github_payload_is_installed(self) -> None:
        for relative in INSTALLED_GITHUB_PAYLOAD:
            with self.subTest(payload=relative):
                self.assertIn(f".github/{relative}", self.targets)

    def test_the_schema_reference_travels_with_the_skill(self) -> None:
        self.assertIn(".claude/skills/super-board/references/config-schema.json", self.targets)

    def test_nothing_is_installed_outside_claude_and_github(self) -> None:
        for target in sorted(self.targets):
            with self.subTest(target=target):
                self.assertTrue(target.startswith((".claude/", ".github/")))

    def test_a_missing_source_asset_fails_closed(self) -> None:
        with _Target() as tmp:
            fake = tmp / "source"
            shutil.copytree(_REPO_ROOT / "scripts", fake / "scripts")
            shutil.copytree(_REPO_ROOT / "skills", fake / "skills")
            shutil.copytree(_REPO_ROOT / "payload", fake / "payload")
            shutil.copytree(_REPO_ROOT / "workflows", fake / "workflows")
            (fake / "scripts" / "super-board-sweep-comments.mjs").unlink()
            with self.assertRaises(InstallError) as ctx:
                plan_install_payload(fake)
            self.assertEqual(ctx.exception.reason, "install-payload-incomplete")


class ForbiddenPathTests(unittest.TestCase):
    def test_the_retired_paths_are_named_explicitly(self) -> None:
        self.assertEqual(
            set(FORBIDDEN_INSTALL_PATHS),
            {".claude/super-board/config.json", ".claude/super-board/scripts/"},
        )

    def test_no_payload_item_targets_a_retired_path(self) -> None:
        for item in plan_install_payload(_REPO_ROOT):
            for forbidden in FORBIDDEN_INSTALL_PATHS:
                with self.subTest(target=item.target, forbidden=forbidden):
                    self.assertFalse(item.target.startswith(forbidden.rstrip("/")))

    def test_a_real_install_produces_neither_retired_path(self) -> None:
        with _Target() as tmp:
            _install(tmp)
            self.assertFalse((tmp / ".claude" / "super-board" / "config.json").exists())
            self.assertFalse((tmp / ".claude" / "super-board" / "scripts").exists())

    def test_the_installer_script_never_names_a_retired_path(self) -> None:
        text = INSTALL_SH.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_INSTALL_PATHS:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)


# ───────────────────────────── the manifest ─────────────────────────────


class ManifestContentTests(unittest.TestCase):
    def test_the_manifest_records_the_pinned_release_identity(self) -> None:
        manifest = build_install_manifest(
            SOURCE_SHA,
            "9.0.0",
            (InstalledFile(".claude/bin/x.sh", "d" * 64, "bin", True),),
            design_skill_source=DESIGN_SOURCE,
            design_skill_sha=DESIGN_SHA,
            design_skill_checksum=DESIGN_CHECKSUM,
            installed_at="2026-08-02T12:00:00Z",
        )
        body = manifest.to_dict()
        self.assertEqual(body["release_version"], "9.0.0")
        self.assertEqual(body["source_sha"], SOURCE_SHA)
        self.assertEqual(body["source_repository"], SOURCE_REPOSITORY)
        self.assertEqual(body["config_schema_version"], CONFIG_SCHEMA_VERSION)
        self.assertEqual(body["installed_at"], "2026-08-02T12:00:00Z")
        self.assertEqual(body["files"][0]["sha256"], "d" * 64)

    def test_the_design_skill_is_recorded_in_three_separate_fields(self) -> None:
        manifest = build_install_manifest(
            SOURCE_SHA, "9.0.0", (),
            design_skill_source=DESIGN_SOURCE,
            design_skill_sha=DESIGN_SHA,
            design_skill_checksum=DESIGN_CHECKSUM,
        )
        body = manifest.to_dict()
        self.assertEqual(body["design_skill_source"], DESIGN_SOURCE)
        self.assertEqual(body["design_skill_sha"], DESIGN_SHA)
        self.assertEqual(body["design_skill_checksum"], DESIGN_CHECKSUM)

    def test_the_manifest_is_deterministically_key_sorted(self) -> None:
        with _Target() as tmp:
            manifest = _install(tmp)
            text = (tmp / MANIFEST_RELATIVE_PATH).read_text(encoding="utf-8")
            body = json.loads(text)
            self.assertEqual(text, json.dumps(body, indent=2, sort_keys=True) + "\n")
            paths = [entry["path"] for entry in body["files"]]
            self.assertEqual(paths, sorted(paths))

    def test_every_installed_file_carries_a_checksum(self) -> None:
        with _Target() as tmp:
            manifest = _install(tmp)
            self.assertTrue(manifest.files)
            for entry in manifest.files:
                with self.subTest(path=entry.path):
                    self.assertEqual(len(entry.sha256), 64)
                    self.assertTrue(entry.owner)

    def test_the_manifest_round_trips(self) -> None:
        with _Target() as tmp:
            manifest = _install(tmp)
            self.assertEqual(
                InstallManifest.from_dict(manifest.to_dict()).to_dict(), manifest.to_dict()
            )


class GeneratedStateTests(unittest.TestCase):
    def test_the_installed_layout_carries_a_config_and_an_active_pointer(self) -> None:
        with _Target() as tmp:
            _install(tmp)
            self.assertTrue((tmp / ".claude/super-board/configs/polysimulator.json").is_file())
            self.assertTrue((tmp / ".claude/super-board/active").is_file())

    def test_the_installed_config_starts_deactivated(self) -> None:
        with _Target() as tmp:
            _install(tmp)
            config = json.loads(
                (tmp / ".claude/super-board/configs/polysimulator.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(config["activation_mode"], "off")
            self.assertEqual(config["version"], CONFIG_SCHEMA_VERSION)

    def test_an_operator_edited_config_is_never_overwritten(self) -> None:
        with _Target() as tmp:
            _install(tmp)
            path = tmp / ".claude/super-board/configs/polysimulator.json"
            path.write_text('{"version": 1, "edited": true}\n', encoding="utf-8")
            _install(tmp)
            self.assertIn("edited", path.read_text(encoding="utf-8"))


# ───────────────────────────── source pinning ─────────────────────────────


class SourcePinningTests(unittest.TestCase):
    def test_a_matching_head_is_accepted(self) -> None:
        verify_source_sha(_REPO_ROOT, SOURCE_SHA, head_reader=lambda _root: SOURCE_SHA)

    def test_a_head_that_differs_is_rejected(self) -> None:
        with self.assertRaises(InstallError) as ctx:
            verify_source_sha(_REPO_ROOT, SOURCE_SHA, head_reader=lambda _root: "f" * 40)
        self.assertEqual(ctx.exception.reason, "install-source-sha-mismatch")
        self.assertEqual(ctx.exception.exit_code, 65)

    def test_an_unreadable_head_is_rejected(self) -> None:
        def explode(_root):
            raise OSError("no git here")

        with self.assertRaises(InstallError) as ctx:
            verify_source_sha(_REPO_ROOT, SOURCE_SHA, head_reader=explode)
        self.assertEqual(ctx.exception.exit_code, 65)


class DowngradeTests(unittest.TestCase):
    def test_a_lower_release_is_a_downgrade(self) -> None:
        self.assertTrue(is_downgrade("2.0.0", "1.9.9"))
        self.assertTrue(is_downgrade("1.10.0", "1.9.0"))

    def test_the_same_or_a_higher_release_is_not(self) -> None:
        self.assertFalse(is_downgrade("1.0.0", "1.0.0"))
        self.assertFalse(is_downgrade("1.0.0", "2.0.0"))

    def test_a_downgrade_is_refused_without_the_override(self) -> None:
        with _Target() as tmp:
            _install(tmp, release_version="9.0.0")
            with self.assertRaises(InstallError) as ctx:
                _install(tmp, release_version="8.0.0")
            self.assertEqual(ctx.exception.reason, "install-downgrade-refused")

    def test_the_documented_override_permits_it(self) -> None:
        with _Target() as tmp:
            _install(tmp, release_version="9.0.0")
            manifest = _install(tmp, release_version="8.0.0", allow_downgrade=True)
            self.assertEqual(manifest.release_version, "8.0.0")

    def test_the_installer_documents_the_override_flag(self) -> None:
        text = INSTALL_SH.read_text(encoding="utf-8")
        self.assertIn("--allow-downgrade", text)


# ───────────────────────────── verification ─────────────────────────────


class VerificationTests(unittest.TestCase):
    def test_a_clean_install_verifies(self) -> None:
        with _Target() as tmp:
            _install(tmp)
            report = verify_install_manifest(tmp / MANIFEST_RELATIVE_PATH, tmp)
            self.assertTrue(report.ok, report.to_dict())
            self.assertEqual(report.changed, ())
            self.assertEqual(report.missing, ())
            self.assertEqual(report.extra, ())

    def test_a_changed_installed_file_is_detected(self) -> None:
        with _Target() as tmp:
            _install(tmp)
            (tmp / ".claude/bin/super-board-run.sh").write_text("tampered\n", encoding="utf-8")
            report = verify_install_manifest(tmp / MANIFEST_RELATIVE_PATH, tmp)
            self.assertFalse(report.ok)
            self.assertIn(".claude/bin/super-board-run.sh", report.changed)
            self.assertEqual(report.reason_code, "install-verification-failed")

    def test_a_missing_installed_file_is_detected(self) -> None:
        with _Target() as tmp:
            _install(tmp)
            (tmp / ".claude/bin/super-board-sweep-comments.mjs").unlink()
            report = verify_install_manifest(tmp / MANIFEST_RELATIVE_PATH, tmp)
            self.assertFalse(report.ok)
            self.assertIn(".claude/bin/super-board-sweep-comments.mjs", report.missing)

    def test_an_extra_manifest_entry_is_detected(self) -> None:
        with _Target() as tmp:
            _install(tmp)
            path = tmp / MANIFEST_RELATIVE_PATH
            body = json.loads(path.read_text(encoding="utf-8"))
            body["files"].append(
                {
                    "path": ".claude/super-board/scripts/rogue.sh",
                    "sha256": "e" * 64,
                    "owner": "bin",
                    "executable": True,
                }
            )
            body["files"].sort(key=lambda entry: entry["path"])
            path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            report = verify_install_manifest(path, tmp)
            self.assertFalse(report.ok)
            self.assertIn(".claude/super-board/scripts/rogue.sh", report.extra)

    def test_a_missing_manifest_fails_closed(self) -> None:
        with _Target() as tmp:
            with self.assertRaises(InstallError) as ctx:
                verify_install_manifest(tmp / MANIFEST_RELATIVE_PATH, tmp)
            self.assertEqual(ctx.exception.reason, "install-manifest-unreadable")

    def test_the_verification_cli_exists_and_declares_its_interface(self) -> None:
        text = VERIFY_CLI.read_text(encoding="utf-8")
        for token in ("verify", "--manifest", "--repo-root", "--json"):
            with self.subTest(token=token):
                self.assertIn(token, text)


# ───────────────────────────── stale removal ─────────────────────────────


class StaleRemovalTests(unittest.TestCase):
    def _manifest(self, *paths: str) -> InstallManifest:
        return build_install_manifest(
            SOURCE_SHA,
            "9.0.0",
            tuple(InstalledFile(path, "d" * 64, "bin", False) for path in paths),
            design_skill_source=DESIGN_SOURCE,
            design_skill_sha=DESIGN_SHA,
            design_skill_checksum=DESIGN_CHECKSUM,
        )

    def test_only_files_the_prior_manifest_owned_are_stale(self) -> None:
        previous = self._manifest(".claude/bin/a.sh", ".claude/bin/gone.sh")
        current = self._manifest(".claude/bin/a.sh")
        self.assertEqual(stale_files(previous, current), (".claude/bin/gone.sh",))

    def test_an_unowned_file_is_never_stale(self) -> None:
        previous = self._manifest(".claude/bin/a.sh")
        current = self._manifest(".claude/bin/a.sh")
        self.assertEqual(stale_files(previous, current), ())

    def test_a_reinstall_preserves_unowned_repository_files(self) -> None:
        with _Target() as tmp:
            _install(tmp)
            stray = tmp / ".claude" / "bin" / "operator-note.txt"
            stray.write_text("mine\n", encoding="utf-8")
            keep = tmp / ".github" / "workflows" / "ci.yml"
            keep.write_text("name: ci\n", encoding="utf-8")
            _install(tmp)
            self.assertTrue(stray.is_file())
            self.assertEqual(keep.read_text(encoding="utf-8"), "name: ci\n")


# ───────────────────────────── the idempotency proof ─────────────────────────────


class IdempotencyProofTests(unittest.TestCase):
    def test_two_installs_at_the_same_release_leave_an_identical_tree(self) -> None:
        with _Target() as tmp:
            first_manifest = _install(tmp)
            first = snapshot_install_tree(tmp, first_manifest)
            second_manifest = _install(tmp)
            second = snapshot_install_tree(tmp, second_manifest)

            drift = compare_install_snapshots(first, second)
            self.assertEqual(drift.added, ())
            self.assertEqual(drift.removed, ())
            self.assertEqual(drift.changed, ())
            self.assertEqual(drift.ownership_shifted, ())
            self.assertTrue(drift.clean)
            self.assertEqual(second.to_dict(), first.to_dict())

    def test_the_snapshot_is_deterministic_and_key_sorted(self) -> None:
        with _Target() as tmp:
            manifest = _install(tmp)
            snapshot = snapshot_install_tree(tmp, manifest)
            keys = list(snapshot.to_dict()["entries"])
            self.assertEqual(keys, sorted(keys))
            self.assertEqual(
                snapshot_install_tree(tmp, manifest).to_dict(), snapshot.to_dict()
            )

    def test_the_snapshot_excludes_the_manifest_itself(self) -> None:
        # The manifest carries the install timestamp, so it legitimately differs
        # between two installs. It is the ledger, not an installed file.
        with _Target() as tmp:
            manifest = _install(tmp)
            self.assertNotIn(
                MANIFEST_RELATIVE_PATH, snapshot_install_tree(tmp, manifest).to_dict()["entries"]
            )

    def test_a_changed_file_shows_up_as_drift(self) -> None:
        with _Target() as tmp:
            manifest = _install(tmp)
            first = snapshot_install_tree(tmp, manifest)
            (tmp / ".claude/bin/super-board-run.sh").write_text("drifted\n", encoding="utf-8")
            drift = compare_install_snapshots(first, snapshot_install_tree(tmp, manifest))
            self.assertFalse(drift.clean)
            self.assertIn(".claude/bin/super-board-run.sh", drift.changed)

    def test_an_added_file_shows_up_as_drift(self) -> None:
        with _Target() as tmp:
            manifest = _install(tmp)
            first = snapshot_install_tree(tmp, manifest)
            (tmp / ".claude/bin/extra.sh").write_text("new\n", encoding="utf-8")
            drift = compare_install_snapshots(first, snapshot_install_tree(tmp, manifest))
            self.assertFalse(drift.clean)
            self.assertIn(".claude/bin/extra.sh", drift.added)

    def test_a_removed_file_shows_up_as_drift(self) -> None:
        with _Target() as tmp:
            manifest = _install(tmp)
            first = snapshot_install_tree(tmp, manifest)
            (tmp / ".claude/workflows/super-board-wave.js").unlink()
            drift = compare_install_snapshots(first, snapshot_install_tree(tmp, manifest))
            self.assertFalse(drift.clean)
            self.assertIn(".claude/workflows/super-board-wave.js", drift.removed)

    def test_an_ownership_shift_shows_up_as_drift(self) -> None:
        with _Target() as tmp:
            manifest = _install(tmp)
            first = snapshot_install_tree(tmp, manifest)
            reassigned = tuple(
                InstalledFile(entry.path, entry.sha256, "reassigned", entry.executable)
                if entry.path.endswith("super-board-run.sh")
                else entry
                for entry in manifest.files
            )
            shifted = snapshot_install_tree(
                tmp, build_install_manifest(
                    SOURCE_SHA, "9.0.0", reassigned,
                    design_skill_source=DESIGN_SOURCE,
                    design_skill_sha=DESIGN_SHA,
                    design_skill_checksum=DESIGN_CHECKSUM,
                )
            )
            drift = compare_install_snapshots(first, shifted)
            self.assertFalse(drift.clean)
            self.assertIn(".claude/bin/super-board-run.sh", drift.ownership_shifted)

    def test_the_proof_never_diffs_against_the_pre_install_checkout(self) -> None:
        # The accepted method is snapshot-compare-snapshot. A `git diff` against
        # the checkout is dominated by the FIRST install's own output, so it can
        # read clean while the second install rewrites half the payload — and it
        # cannot see an ownership shift at all.
        sources = [
            (_SCRIPTS / "super_board_runtime" / "install_manifest.py").read_text(encoding="utf-8"),
            INSTALL_TEST_SH.read_text(encoding="utf-8"),
        ]
        for text in sources:
            for banned in ("git diff", "--exit-code", "git status"):
                with self.subTest(banned=banned):
                    self.assertNotIn(banned, text)

    def test_the_shell_proof_reports_the_documented_tail(self) -> None:
        text = INSTALL_TEST_SH.read_text(encoding="utf-8")
        for token in (
            "install_1_files=",
            "install_2_files=",
            "added=",
            "removed=",
            "changed=",
            "ownership_shifted=",
            "clean=",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)


# ───────────────────────────── the installer entry point ─────────────────────────────


class InstallerInterfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = INSTALL_SH.read_text(encoding="utf-8")

    def test_every_declared_flag_is_accepted(self) -> None:
        for flag in (
            "--repo-root",
            "--user-home",
            "--source-sha",
            "--release-version",
            "--design-skill-source",
            "--design-skill-sha",
            "--design-skill-checksum",
        ):
            with self.subTest(flag=flag):
                self.assertIn(flag, self.text)

    def test_the_installer_fails_closed_on_a_bad_contract(self) -> None:
        self.assertIn("exit 65", self.text)
        self.assertIn("exit 64", self.text)

    def test_the_installer_delegates_to_the_manifest_runtime(self) -> None:
        self.assertIn("super-board-install-verify.py", self.text)

    def test_the_installer_verifies_what_it_installed(self) -> None:
        self.assertIn("verify", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
