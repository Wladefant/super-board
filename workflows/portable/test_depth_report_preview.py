#!/usr/bin/env python3
"""
Tests for depth_report_preview.py: the served SHA a capture reads, and what the preview serves.
Tracking: https://github.com/Wladefant/super-board/issues/517
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import depth_report_preview as preview  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True, env=ENV).stdout.strip()


class TestServedIdentity(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        git(self.root, "init", "-q")
        self.template = self.root / "report-template.html"
        self.renderer = self.root / "depth_survey.py"
        self.template.write_text("<style></style>", encoding="utf-8")
        self.renderer.write_text("x = 1\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")

    def tearDown(self):
        self._tmp.cleanup()

    def test_clean_checkout_serves_its_head(self):
        got = preview.served_identity(self.template, self.renderer, "abc")
        self.assertEqual(got["sha"], git(self.root, "rev-parse", "HEAD"))
        self.assertFalse(got["dirty"])
        self.assertEqual(got["surveyed_sha"], "abc")

    def test_local_change_to_template_or_renderer_is_dirty(self):
        self.template.write_text("<style>body{}</style>", encoding="utf-8")
        self.assertTrue(preview.served_identity(self.template, self.renderer, "abc")["dirty"])
        git(self.root, "checkout", "--", str(self.template))
        self.renderer.write_text("x = 2\n", encoding="utf-8")
        self.assertTrue(preview.served_identity(self.template, self.renderer, "abc")["dirty"])

    def test_renderer_outside_the_checkout_is_dirty(self):
        with tempfile.TemporaryDirectory() as other:
            renderer = Path(other) / "depth_survey.py"
            renderer.write_text("x = 1\n", encoding="utf-8")
            self.assertTrue(preview.served_identity(self.template, renderer, "abc")["dirty"])

    def test_template_outside_any_checkout_has_no_sha(self):
        with tempfile.TemporaryDirectory() as other:
            template = Path(other) / "report-template.html"
            template.write_text("<style></style>", encoding="utf-8")
            got = preview.served_identity(template, self.renderer, "abc")
            self.assertEqual(got["sha"], "")
            self.assertTrue(got["dirty"])


class TestHandler(unittest.TestCase):
    def setUp(self):
        self.version = {"sha": "a" * 40, "dirty": False, "surveyed_sha": "b" * 40, "template": "t.html"}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), preview.make_handler(b"<p>report</p>", self.version))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_report_and_version_carry_the_served_sha(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=10) as res:
            self.assertEqual(res.read(), b"<p>report</p>")
            self.assertEqual(res.headers["x-served-sha"], "a" * 40)
        with urllib.request.urlopen(f"{self.base}/api/version", timeout=10) as res:
            self.assertEqual(json.loads(res.read()), self.version)

    def test_other_paths_are_404(self):
        with self.assertRaises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(f"{self.base}/fonts/x.woff2", timeout=10)
        self.assertEqual(err.exception.code, 404)


def port_is_free(port: int) -> bool:
    """True when a plain socket (no SO_REUSEADDR) can bind the port: nothing holds it any more."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


class TestServeUntilStopped(unittest.TestCase):
    def test_stopping_the_preview_releases_its_port(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), preview.make_handler(b"x", {"sha": "a" * 40}))
        port = server.server_port
        thread = threading.Thread(target=preview.serve_until_stopped, args=(server,), daemon=True)
        thread.start()
        self.assertFalse(port_is_free(port), "the port must be held while the preview serves")
        server.shutdown()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(port_is_free(port), "the port must be free once the preview stops")


if __name__ == "__main__":
    unittest.main()
