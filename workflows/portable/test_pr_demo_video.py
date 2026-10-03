#!/usr/bin/env python3
"""test_pr_demo_video.py - block building, fail-closed stages, step validation."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import evidence_lint
import pr_demo_video as pdv

SHA = "c" * 40
MP4 = "https://github.com/user-attachments/assets/f557e505-4cd9-48b3-bb4f-91860564058a"
GIF = "https://github.com/user-attachments/assets/0aaaaaaa-4cd9-48b3-bb4f-91860564058a"


class Block(unittest.TestCase):
    def test_block_has_bare_mp4_line_gif_and_passes_lint(self):
        block = pdv.build_block(SHA, MP4, GIF, "cap", "https://example.com")
        self.assertIn(f"\n{MP4}\n", block)  # bare on its own line => inline player
        self.assertIn(f"![demo preview]({GIF})", block)
        self.assertIn(SHA, block)
        self.assertTrue(block.startswith(pdv.MARKER))
        self.assertEqual(evidence_lint.lint_text(block), [])

    def test_short_sha_rejected(self):
        with self.assertRaises(pdv.DemoError):
            pdv.build_block("abc123", MP4, GIF, "", "u")


class Stages(unittest.TestCase):
    def test_upload_without_attachment_url_fails_closed(self):
        with mock.patch.object(pdv, "run", return_value="Error: token\n"):
            with self.assertRaises(pdv.DemoError):
                pdv.upload(Path("x.mp4"), "o/r")

    def test_upload_parses_url(self):
        with mock.patch.object(pdv, "run", return_value=f"{MP4}\n"):
            self.assertEqual(pdv.upload(Path("x.mp4"), "o/r"), MP4)

    def test_convert_needs_ffmpeg(self):
        with mock.patch.object(pdv.shutil, "which", return_value=None):
            with self.assertRaises(pdv.DemoError):
                pdv.convert(Path("x.webm"))

    def test_record_rejects_bad_duration_before_launching(self):
        for s in (0, pdv.MAX_SECONDS + 1):
            with self.assertRaises(pdv.DemoError):
                pdv.record("https://example.com", Path("."), [], s, 800, 600)

    def test_old_playwright_rejected(self):
        with mock.patch("importlib.metadata.version", return_value="1.46.0"):
            with self.assertRaises(pdv.DemoError):
                pdv.check_playwright_version()
        with mock.patch("importlib.metadata.version", return_value="1.57.0"):
            pdv.check_playwright_version()

    def test_browser_env_override_wins(self):
        with mock.patch.dict(os.environ, {"PR_DEMO_BROWSER": "X:/chrome.exe"}):
            self.assertEqual(pdv.find_browser(), "X:/chrome.exe")

    def test_unknown_step_action_rejected(self):
        with self.assertRaises(pdv.DemoError):
            pdv.apply_steps(mock.Mock(), [{"action": "teleport"}])

    def test_steps_dispatch(self):
        page = mock.Mock()
        pdv.apply_steps(page, [{"action": "click", "selector": "a"}, {"action": "scroll", "y": 300}, {"action": "wait", "ms": 5}])
        page.click.assert_called_once()
        page.mouse.wheel.assert_called_once_with(0, 300)

    def test_main_does_not_post_when_upload_fails(self):
        with mock.patch.object(pdv, "record", return_value=Path("a.webm")), \
             mock.patch.object(pdv, "convert", return_value={"mp4": Path("a.mp4"), "gif": Path("a.gif")}), \
             mock.patch.object(pdv, "upload", side_effect=pdv.DemoError("no token")), \
             mock.patch.object(pdv, "post") as post:
            rc = pdv.main(["--url", "https://e.com", "--repo", "o/r", "--pr", "1", "--sha", SHA, "--post"])
        self.assertEqual(rc, 1)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
