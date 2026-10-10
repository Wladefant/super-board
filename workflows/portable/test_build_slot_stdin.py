"""Real CLI stdin contracts with an open, non-interactive caller pipe."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import build_slot


class TestRunStdin(unittest.TestCase):
    def probe(self, inherit=False, node=False):
        with tempfile.TemporaryDirectory() as run_dir:
            env = os.environ.copy()
            env.pop("BUILD_SLOT_HELD", None)
            env.update(BUILD_SLOT_AVAILABLE_GIB="64", BUILD_SLOT_RAM_PERCENT="20",
                       BUILD_SLOT_STAGGER_SECONDS="0")
            if node:
                script = Path(run_dir) / "stdin-probe.js"
                script.write_text("process.stdin.resume();process.stdin.on('end',()=>{console.log('stdin-eof');console.error('stderr-live')})", encoding="utf-8")
                child = [shutil.which("node"), str(script)]
            else:
                child = [sys.executable, "-c", "import sys; print('stdin-eof:'+sys.stdin.read()); print('stderr-live',file=sys.stderr)"]
            command = [sys.executable, str(Path(build_slot.__file__)), "--run-dir", run_dir,
                       "run", "stdin-probe", "--class", "light", "--timeout", "3",
                       "--run-timeout", "2"]
            if inherit:
                command.append("--stdin")
            proc = subprocess.Popen(command + ["--"] + child, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                if inherit:
                    out, err = proc.communicate("caller-input", timeout=10)
                else:
                    # Keep the pipe open until the child finishes. Inheritance would block EOF.
                    proc.wait(timeout=10)
                    proc.stdin.close()
                    proc.stdin = None
                    out, err = proc.communicate(timeout=2)
                self.assertEqual(proc.returncode, 0, "default_stdin_reader_must_finish: " + err)
                self.assertIn("stdin-eof", out)
                self.assertIn("stderr-live", err)
                if inherit:
                    self.assertIn("caller-input", out)
                self.assertFalse(any(Path(run_dir).glob("build-slot.lock*")), "finished_child_releases_slot")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate(timeout=5)

    def test_default_stdin_reader_must_finish(self):
        self.probe()

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_node_stdin_reader_must_finish(self):
        self.probe(node=True)

    def test_explicit_stdin_receives_caller_input(self):
        self.probe(inherit=True)

    def test_stdin_flag_before_and_after_lane_name(self):
        for args in (["run", "--stdin", "lane", "--", "node"],
                     ["run", "lane", "--stdin", "--", "node"]):
            self.assertTrue(build_slot.parse_args(args).stdin)


if __name__ == "__main__":
    unittest.main()
