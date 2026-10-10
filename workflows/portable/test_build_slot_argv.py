"""Real CLI argv contracts, including Windows npm-generated Node shims."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import build_slot


class TestRunArgv(unittest.TestCase):
    def probe(self, shim=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "echo argv.js"
            script.write_text("console.log(JSON.stringify(process.argv.slice(2)))", encoding="utf-8")
            args = ["=>", "|", "&", "%PATH%", "^", "<", ">", "two words", 'a"b', "", "end\\", "雪"]
            child = [shutil.which("node"), str(script)]
            env = os.environ.copy()
            env.pop("BUILD_SLOT_HELD", None)
            env.update(BUILD_SLOT_AVAILABLE_GIB="64", BUILD_SLOT_RAM_PERCENT="20", BUILD_SLOT_STAGGER_SECONDS="0")
            if shim:
                launcher = root / (shim + ".cmd")
                launcher.write_text('@ECHO off\nSET dp0=%~dp0\n"node" "%dp0%\\echo argv.js" %*\n', encoding="utf-8")
                env["PATH"] = str(root) + os.pathsep + env["PATH"]
                child = [shim]
            command = [sys.executable, build_slot.__file__, "--run-dir", str(root / "run"),
                       "run", "argv-probe", "--class", "light", "--timeout", "3", "--run-timeout", "5", "--"]
            result = subprocess.run(command + child + args, env=env, capture_output=True,
                                    text=True, encoding="utf-8", timeout=15,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, "child_argv_must_be_byte_identical: " + result.stderr)
            echoed = [line for line in result.stdout.splitlines() if line.startswith('["')]
            self.assertEqual(len(echoed), 1, "child_argv_must_be_byte_identical: " + result.stdout)
            self.assertEqual(json.loads(echoed[0]), args, "child_argv_must_be_byte_identical")
            self.assertFalse(list(root.glob("run/build-slot.lock*")), "finished_child_releases_slot")

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_child_argv_must_be_byte_identical(self):
        self.probe()

    @unittest.skipUnless(sys.platform == "win32" and shutil.which("node"), "Windows Node shim contract")
    def test_npm_npx_next_node_shims_preserve_argv(self):
        for shim in ("npm", "npx", "next"):
            with self.subTest(shim=shim):
                self.probe(shim)

    @unittest.skipUnless(sys.platform == "win32", "Windows console contract")
    def test_child_has_no_console_window(self):
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env.pop("BUILD_SLOT_HELD", None)
            env.update(BUILD_SLOT_AVAILABLE_GIB="64", BUILD_SLOT_RAM_PERCENT="20", BUILD_SLOT_STAGGER_SECONDS="0")
            result = subprocess.run(
                [sys.executable, build_slot.__file__, "--run-dir", directory, "run", "console-probe",
                 "--class", "light", "--timeout", "3", "--run-timeout", "5", "--",
                 sys.executable, "-c", "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"],
                env=env, capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("0", result.stdout.splitlines(), "child_has_no_console_window")

    @unittest.skipUnless(sys.platform == "win32", "Windows batch compatibility")
    def test_other_batch_commands_keep_cmd_semantics(self):
        with tempfile.TemporaryDirectory(prefix="batch space ") as directory:
            batch = Path(directory) / "echo batch.cmd"
            batch.write_text("@echo off\necho %1\n", encoding="utf-8")
            env = os.environ.copy()
            env.pop("BUILD_SLOT_HELD", None)
            env.update(BUILD_SLOT_AVAILABLE_GIB="64", BUILD_SLOT_RAM_PERCENT="20", BUILD_SLOT_STAGGER_SECONDS="0",
                       SLOT_ARGV_MARKER="batch-expanded")
            result = subprocess.run(
                [sys.executable, build_slot.__file__, "--run-dir", str(Path(directory) / "run"),
                 "run", "batch-probe", "--class", "light", "--timeout", "3", "--run-timeout", "5",
                 "--", str(batch), "%SLOT_ARGV_MARKER%"],
                env=env, capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("batch-expanded", result.stdout.splitlines(), "batch_keeps_cmd_semantics")

    @unittest.skipUnless(sys.platform == "win32" and shutil.which("node"), "Windows explicit shell contract")
    def test_explicit_cmd_redirects_both_node_commands(self):
        with tempfile.TemporaryDirectory(prefix="explicit shell ") as directory:
            root = Path(directory)
            env = os.environ.copy()
            env.pop("BUILD_SLOT_HELD", None)
            env.update(BUILD_SLOT_AVAILABLE_GIB="64", BUILD_SLOT_RAM_PERCENT="20", BUILD_SLOT_STAGGER_SECONDS="0")
            result = subprocess.run(
                [sys.executable, build_slot.__file__, "--run-dir", str(root / "run"),
                 "run", "Def497V3", "--class", "light", "--timeout", "3", "--run-timeout", "5",
                 "--", "cmd", "/c", "node", "-e", "console.log('eslint-output'); console.error('eslint-error')",
                 ">", "_lwp_eslint.log", "2>&1", "&",
                 "node", "-e", "console.log('vitest-output'); console.error('vitest-error')",
                 ">", "_lwp_vitest.log", "2>&1"],
                cwd=directory, env=env, capture_output=True, text=True, timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "_lwp_eslint.log").read_text().splitlines(),
                             ["eslint-output", "eslint-error"], "explicit_cmd_redirects_first_command")
            self.assertEqual((root / "_lwp_vitest.log").read_text().splitlines(),
                             ["vitest-output", "vitest-error"], "explicit_cmd_redirects_second_command")

    @unittest.skipUnless(sys.platform == "win32" and shutil.which("node"), "Windows delayed expansion contract")
    def test_explicit_cmd_delayed_expansion_records_exit_code(self):
        with tempfile.TemporaryDirectory(prefix="delayed shell ") as directory:
            root = Path(directory)
            env = os.environ.copy()
            env.pop("BUILD_SLOT_HELD", None)
            env.update(BUILD_SLOT_AVAILABLE_GIB="64", BUILD_SLOT_RAM_PERCENT="20", BUILD_SLOT_STAGGER_SECONDS="0")
            for code in (0, 7):
                with self.subTest(code=code):
                    result = subprocess.run(
                        [sys.executable, build_slot.__file__, "--run-dir", str(root / "run"),
                         "run", "Def497V4", "--class", "light", "--timeout", "3", "--run-timeout", "5",
                         "--", "cmd", "/v:on", "/c", "node", "-e",
                         f"console.log('node-output'); process.exit({code})",
                         ">", "log", "2>&1", "&", "echo", "rc=!ERRORLEVEL!", ">>", "log"],
                        cwd=directory, env=env, capture_output=True, text=True, timeout=15,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual([line.strip() for line in (root / "log").read_text().splitlines()],
                                     ["node-output", f"rc={code}"], "explicit_cmd_expands_errorlevel")


if __name__ == "__main__":
    unittest.main()
