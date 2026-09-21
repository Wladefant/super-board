#!/usr/bin/env python3
"""test_install_telegram_harness.py — test suite for install-telegram-harness.py."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# Add scripts directory to sys.path
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import importlib
install_module = importlib.import_module("install-telegram-harness")

EXIT_OK = install_module.EXIT_OK
EXIT_DRIFT = install_module.EXIT_DRIFT
EXIT_CONFIG = install_module.EXIT_CONFIG


class TestInstallTelegramHarness(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="test_install_telegram_")
        self.tmp_path = Path(self.tmp_dir)
        self.source_root = _SCRIPTS_DIR.parent
        self.target = self.tmp_path / "target"
        self.target.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_fresh_install_clean(self):
        """Test clean installation to an empty target directory."""
        code = install_module.main([
            "--source-root", str(self.source_root),
            "--target", str(self.target),
            "--allow-dirty",
        ])
        self.assertEqual(code, EXIT_OK)

        # Assert key extension and harness files exist
        self.assertTrue((self.target / "guard.ts").is_file())
        self.assertTrue((self.target / "index.ts").is_file())
        self.assertTrue((self.target / "sanitizer.ts").is_file())
        self.assertTrue((self.target / "harness" / "index.ts").is_file())
        self.assertTrue((self.target / "harness" / "installed-commands.ts").is_file())
        self.assertTrue((self.target / "harness" / "telegram-router.ts").is_file())

        # Assert manifest exists and is valid
        manifest_path = self.target / "install-manifest.json"
        self.assertTrue(manifest_path.is_file())
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIn("source_sha", data)
        self.assertIn("source_repository", data)
        self.assertIn("installed_at", data)
        self.assertIn("files", data)
        self.assertGreater(len(data["files"]), 15)

        # Assert sha256 in manifest matches actual files on disk
        for entry in data["files"]:
            file_path = self.target / entry["path"]
            self.assertTrue(file_path.is_file(), f"File {entry['path']} missing on disk")
            self.assertEqual(
                install_module.sha256_file(file_path),
                entry["sha256"],
                f"Hash mismatch for {entry['path']}",
            )

        # Assert --check exits 0 on clean install
        check_code = install_module.main([
            "--source-root", str(self.source_root),
            "--target", str(self.target),
            "--check",
        ])
        self.assertEqual(check_code, EXIT_OK)

        # Assert no backup was created on fresh install
        backup_dir = self.target / ".backups"
        self.assertFalse(backup_dir.exists())

    def test_preserve_runtime_state_and_backup(self):
        """Test that runtime state is preserved and pre-overwrite backup is created."""
        # Pre-create runtime state files in target
        manifest_content = '{"version": 1, "slots": [{"slotId": "test-slot"}]}'
        (self.target / "manifest.json").write_text(manifest_content, encoding="utf-8")

        db_content = b"SQLITE_FAKE_DB_BYTES\x00\x01\x02"
        (self.target / "bot_pool.db").write_bytes(db_content)
        (self.target / "bot_pool.db-wal").write_bytes(b"WAL_BYTES")
        (self.target / "bot_pool.db-shm").write_bytes(b"SHM_BYTES")

        ps1_content = "Write-Host 'Do not overwrite me!'"
        (self.target / "veyyon-telegram.ps1").write_text(ps1_content, encoding="utf-8")

        bridge_content = "# bridge script runtime"
        (self.target / "veyyon_telegram_bridge.py").write_text(bridge_content, encoding="utf-8")

        guard_js_content = "// js guard runtime"
        (self.target / "veyyon_telegram_guard.js").write_text(guard_js_content, encoding="utf-8")

        notify_state_content = '{"last_id": 42}'
        (self.target / "telegram_notify_state.json").write_text(notify_state_content, encoding="utf-8")

        # Create an old version of guard.ts to be overwritten
        old_guard_content = "// OLD DEPRECATED GUARD VERSION 1.0"
        (self.target / "guard.ts").write_text(old_guard_content, encoding="utf-8")

        # Run installation
        code = install_module.main([
            "--source-root", str(self.source_root),
            "--target", str(self.target),
            "--allow-dirty",
        ])
        self.assertEqual(code, EXIT_OK)

        # Assert all runtime state files remain completely unchanged
        self.assertEqual((self.target / "manifest.json").read_text(encoding="utf-8"), manifest_content)
        self.assertEqual((self.target / "bot_pool.db").read_bytes(), db_content)
        self.assertEqual((self.target / "bot_pool.db-wal").read_bytes(), b"WAL_BYTES")
        self.assertEqual((self.target / "bot_pool.db-shm").read_bytes(), b"SHM_BYTES")
        self.assertEqual((self.target / "veyyon-telegram.ps1").read_text(encoding="utf-8"), ps1_content)
        self.assertEqual((self.target / "veyyon_telegram_bridge.py").read_text(encoding="utf-8"), bridge_content)
        self.assertEqual((self.target / "veyyon_telegram_guard.js").read_text(encoding="utf-8"), guard_js_content)
        self.assertEqual((self.target / "telegram_notify_state.json").read_text(encoding="utf-8"), notify_state_content)

        # Assert guard.ts was updated to new source content
        self.assertNotEqual((self.target / "guard.ts").read_text(encoding="utf-8"), old_guard_content)

        # Assert backup was created
        backups_parent = self.target / ".backups"
        self.assertTrue(backups_parent.is_dir())
        backup_folders = list(backups_parent.iterdir())
        self.assertEqual(len(backup_folders), 1)

        backup_dir = backup_folders[0]
        # Backup should contain the old version of guard.ts
        backup_guard = backup_dir / "guard.ts"
        self.assertTrue(backup_guard.is_file())
        self.assertEqual(backup_guard.read_text(encoding="utf-8"), old_guard_content)

        # Backup should NOT contain bot_pool.db* or .backups
        self.assertFalse((backup_dir / "bot_pool.db").exists())
        self.assertFalse((backup_dir / "bot_pool.db-wal").exists())
        self.assertFalse((backup_dir / ".backups").exists())

    def test_check_detects_drifted_files(self):
        """Test that --check detects modified and missing files and exits with 1."""
        # 1. Clean install first
        code = install_module.main([
            "--source-root", str(self.source_root),
            "--target", str(self.target),
            "--allow-dirty",
        ])
        self.assertEqual(code, EXIT_OK)

        # 2. Modify one file
        guard_file = self.target / "guard.ts"
        guard_file.write_text("// DRIFTED CODE", encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf):
            check_code = install_module.main([
                "--source-root", str(self.source_root),
                "--target", str(self.target),
                "--check",
            ])
        self.assertEqual(check_code, EXIT_DRIFT)
        output = buf.getvalue()
        self.assertIn("guard.ts (modified)", output)

        # 3. Restore guard.ts and delete another file
        install_module.main([
            "--source-root", str(self.source_root),
            "--target", str(self.target),
            "--allow-dirty",
        ])
        missing_file = self.target / "harness" / "telegram-router.ts"
        missing_file.unlink()

        buf = io.StringIO()
        with redirect_stdout(buf):
            check_code = install_module.main([
                "--source-root", str(self.source_root),
                "--target", str(self.target),
                "--check",
            ])
        self.assertEqual(check_code, EXIT_DRIFT)
        output = buf.getvalue()
        self.assertIn("harness/telegram-router.ts (missing)", output)

    def test_dry_run_writes_nothing(self):
        """Test that --dry-run simulates the installation without modifying files or creating backups."""
        initial_file = self.target / "guard.ts"
        initial_content = "// Initial guard before dry-run"
        initial_file.write_text(initial_content, encoding="utf-8")

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = install_module.main([
                "--source-root", str(self.source_root),
                "--target", str(self.target),
                "--dry-run",
                "--allow-dirty",
            ])
        self.assertEqual(code, EXIT_OK)
        output = buf.getvalue()
        self.assertIn("[dry-run] Would overwrite: guard.ts", output)
        self.assertIn("[dry-run] Would write manifest:", output)

        # Target should NOT be modified
        self.assertEqual(initial_file.read_text(encoding="utf-8"), initial_content)
        self.assertFalse((self.target / "install-manifest.json").exists())
        self.assertFalse((self.target / ".backups").exists())
        self.assertFalse((self.target / "harness").exists())

    def test_dirty_working_tree_refused(self):
        """Test that installation refuses dirty git working tree unless --allow-dirty."""
        # Create a mini git repo in temp dir
        repo_dir = self.tmp_path / "fake_repo"
        repo_dir.mkdir()
        pkg_dir = repo_dir / "packages" / "telegram-agent-harness"
        (pkg_dir / "extension").mkdir(parents=True)
        (pkg_dir / "src").mkdir(parents=True)
        (pkg_dir / "extension" / "guard.ts").write_text("// test", encoding="utf-8")
        (pkg_dir / "src" / "index.ts").write_text("// test", encoding="utf-8")

        # Init git repo and commit
        subprocess.run(["git", "-C", str(repo_dir), "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test User"], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "initial"], check=True, capture_output=True)

        # Dirty the repo
        (pkg_dir / "extension" / "guard.ts").write_text("// dirty change", encoding="utf-8")

        # Run without --allow-dirty -> should fail with EXIT_CONFIG (65)
        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            code = install_module.main([
                "--source-root", str(repo_dir),
                "--target", str(self.target),
            ])
        self.assertEqual(code, EXIT_CONFIG)
        self.assertIn("Refusing to install from a dirty working tree", err_buf.getvalue())

        # Run with --allow-dirty -> should succeed with EXIT_OK (0)
        code_dirty_allowed = install_module.main([
            "--source-root", str(repo_dir),
            "--target", str(self.target),
            "--allow-dirty",
        ])
        self.assertEqual(code_dirty_allowed, EXIT_OK)

    def test_conditional_tests_sync(self):
        """Test conditional synchronization of tests and harness/tests directories."""
        # Case 1: target has neither tests/ nor harness/tests/
        code = install_module.main([
            "--source-root", str(self.source_root),
            "--target", str(self.target),
            "--allow-dirty",
        ])
        self.assertEqual(code, EXIT_OK)
        self.assertFalse((self.target / "tests").exists())
        self.assertFalse((self.target / "harness" / "tests").exists())

        # Case 2: target already has tests/
        target2 = self.tmp_path / "target2"
        (target2 / "tests").mkdir(parents=True)
        code2 = install_module.main([
            "--source-root", str(self.source_root),
            "--target", str(target2),
            "--allow-dirty",
        ])
        self.assertEqual(code2, EXIT_OK)
        self.assertTrue((target2 / "tests" / "guard.test.ts").is_file())
        self.assertFalse((target2 / "harness" / "tests").exists())

        # Case 3: target already has harness/tests/
        target3 = self.tmp_path / "target3"
        (target3 / "harness" / "tests").mkdir(parents=True)
        code3 = install_module.main([
            "--source-root", str(self.source_root),
            "--target", str(target3),
            "--allow-dirty",
        ])
        self.assertEqual(code3, EXIT_OK)
        self.assertTrue((target3 / "harness" / "tests" / "sanitizer.test.ts").is_file())
        self.assertFalse((target3 / "tests").exists())

    def test_backup_names_previous_sha(self):
        """Test that backup directory includes the previous source SHA from install-manifest.json."""
        # Pre-create install-manifest.json with known SHA
        manifest = {
            "source_repository": "https://github.com/Wladefant/super-board",
            "source_sha": "c0ffee1234567890abcdef",
            "installed_at": "2026-08-01T00:00:00Z",
            "files": [],
        }
        (self.target / "install-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.target / "guard.ts").write_text("// to overwrite", encoding="utf-8")

        code = install_module.main([
            "--source-root", str(self.source_root),
            "--target", str(self.target),
            "--allow-dirty",
        ])
        self.assertEqual(code, EXIT_OK)

        backups = list((self.target / ".backups").iterdir())
        self.assertEqual(len(backups), 1)
        backup_name = backups[0].name
        self.assertTrue(backup_name.endswith("-c0ffee1234567890abcdef"))

    def test_daemon_tree_and_launcher_installed(self):
        """The daemon modules and its launcher are part of every install, and --check covers them."""
        code = install_module.main([
            "--source-root", str(self.source_root),
            "--target", str(self.target),
            "--allow-dirty",
        ])
        self.assertEqual(code, EXIT_OK)

        for name in ("main.ts", "config.ts", "runtime.ts", "router.ts", "session-control.ts", "store.ts"):
            self.assertTrue((self.target / "daemon" / name).is_file(), f"daemon/{name} missing")
        self.assertTrue((self.target / "veyyon-telegram-daemon.ps1").is_file())
        self.assertTrue((self.target / "veyyon-gui-host.ps1").is_file())

        installed = json.loads((self.target / "install-manifest.json").read_text(encoding="utf-8"))
        recorded = {entry["path"] for entry in installed["files"]}
        self.assertIn("daemon/main.ts", recorded)
        self.assertIn("veyyon-telegram-daemon.ps1", recorded)
        self.assertIn("veyyon-gui-host.ps1", recorded)

        # --check must pass on the freshly installed tree, which only holds when it
        # compares against the rewritten bytes rather than the raw source file.
        self.assertEqual(install_module.main([
            "--source-root", str(self.source_root),
            "--target", str(self.target),
            "--check",
        ]), EXIT_OK)

        # ...and must report drift once a daemon file or the launcher is touched.
        (self.target / "daemon" / "runtime.ts").write_text("// DRIFTED", encoding="utf-8")
        (self.target / "veyyon-telegram-daemon.ps1").write_text("# DRIFTED", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(install_module.main([
                "--source-root", str(self.source_root),
                "--target", str(self.target),
                "--check",
            ]), EXIT_DRIFT)
        output = buf.getvalue()
        self.assertIn("daemon/runtime.ts (modified)", output)
        self.assertIn("veyyon-telegram-daemon.ps1 (modified)", output)

        (self.target / "daemon" / "main.ts").unlink()
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(install_module.main([
                "--source-root", str(self.source_root),
                "--target", str(self.target),
                "--check",
            ]), EXIT_DRIFT)
        self.assertIn("daemon/main.ts (missing)", buf.getvalue())

    def test_daemon_imports_retargeted_at_installed_layout(self):
        """Installed daemon modules import the flattened tree, not the source package layout."""
        code = install_module.main([
            "--source-root", str(self.source_root),
            "--target", str(self.target),
            "--allow-dirty",
        ])
        self.assertEqual(code, EXIT_OK)

        for name in ("main.ts", "config.ts", "runtime.ts", "router.ts", "session-control.ts", "store.ts"):
            text = (self.target / "daemon" / name).read_text(encoding="utf-8")
            self.assertNotIn('from "../extension/', text, f"daemon/{name} still imports ../extension/")
            self.assertNotIn('from "../src/', text, f"daemon/{name} still imports ../src/")

        # Every rewritten specifier must resolve to a file the installer actually placed.
        for name in ("main.ts", "config.ts", "runtime.ts", "router.ts", "session-control.ts", "store.ts"):
            source = (self.target / "daemon" / name)
            for line in source.read_text(encoding="utf-8").splitlines():
                match = re.search(r'from "\.\./([^"]+)"', line)
                if not match:
                    continue
                resolved = self.target / f"{match.group(1)}.ts"
                self.assertTrue(resolved.is_file(), f"daemon/{name}: '{match.group(1)}' resolves to nothing")

    def test_rewrite_daemon_imports_is_exact(self):
        """The rewrite maps each source directory onto its installed location."""
        rewritten = install_module.rewrite_daemon_imports(
            'import { a } from "../extension/coordinator";\n'
            'import { b } from "../extension/harness/command-runner";\n'
            'import { c } from "../src/gui-host-client";\n'
            'import { d } from "./config";\n'
        )
        self.assertEqual(
            rewritten,
            'import { a } from "../coordinator";\n'
            'import { b } from "../harness/command-runner";\n'
            'import { c } from "../harness/gui-host-client";\n'
            'import { d } from "./config";\n',
        )

    def test_daemon_runtime_state_is_protected(self):
        """Live daemon pid/log/db state survives an install and is kept out of backups."""
        run_dir = self.target / "daemon" / "run"
        run_dir.mkdir(parents=True)
        (run_dir / "daemon.pid").write_text("4242", encoding="utf-8")
        (run_dir / "daemon.log").write_text("[ts] Slot telegram-ing polling\n", encoding="utf-8")
        (self.target / "daemon.db").write_bytes(b"SQLITE_FAKE_DAEMON_DB")
        (self.target / "daemon.log").write_text("launcher log\n", encoding="utf-8")
        (self.target / "guard.ts").write_text("// to overwrite", encoding="utf-8")

        self.assertEqual(install_module.main([
            "--source-root", str(self.source_root),
            "--target", str(self.target),
            "--allow-dirty",
        ]), EXIT_OK)

        self.assertEqual((run_dir / "daemon.pid").read_text(encoding="utf-8"), "4242")
        self.assertEqual((self.target / "daemon.db").read_bytes(), b"SQLITE_FAKE_DAEMON_DB")
        self.assertEqual((self.target / "daemon.log").read_text(encoding="utf-8"), "launcher log\n")

        backup_dir = next((self.target / ".backups").iterdir())
        self.assertFalse((backup_dir / "daemon.db").exists())
        self.assertFalse((backup_dir / "daemon" / "run" / "daemon.pid").exists())

    def test_operator_launcher_still_protected(self):
        """Naming the daemon launcher installer-owned must not unprotect other .ps1 files."""
        self.assertTrue(install_module.is_protected_rel_path("veyyon-telegram.ps1"))
        self.assertTrue(install_module.is_protected_rel_path("veyyon-polysim.ps1"))
        self.assertFalse(install_module.is_protected_rel_path("veyyon-telegram-daemon.ps1"))
        self.assertFalse(install_module.is_protected_rel_path("veyyon-gui-host.ps1"))
        self.assertFalse(install_module.is_protected_rel_path("daemon/main.ts"))
        self.assertTrue(install_module.is_protected_rel_path("daemon/run/daemon.pid"))

    def test_invalid_usage_exits_64(self):
        """Test that invalid argument flags exit with code 64."""
        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            with self.assertRaises(SystemExit) as cm:
                install_module.main(["--nonexistent-flag"])
            self.assertEqual(cm.exception.code, install_module.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
