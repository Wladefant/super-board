#!/usr/bin/env python3
"""test_evidence_lint.py - every rejected link form, every accepted form, HTML media extraction."""

import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import evidence_lint as el

UUID = "f557e505-4cd9-48b3-bb4f-91860564058a"
SHA = "a" * 40


def forms(text):
    return sorted(v.form for v in el.lint_text(text))


class LintForms(unittest.TestCase):
    def test_good_forms_pass(self):
        text = (
            f"![after 1440](https://github.com/user-attachments/assets/{UUID})\n"
            f"![pinned](https://github.com/o/r/raw/{SHA}/ev/a.png)\n"
            f"https://github.com/user-attachments/assets/{UUID}\n"
        )
        self.assertEqual(el.lint_text(text), [])

    def test_raw_githubusercontent_rejected(self):
        self.assertEqual(forms(f"![x](https://raw.githubusercontent.com/o/r/{SHA}/a.png)"), ["raw-githubusercontent"])

    def test_local_and_relative_rejected(self):
        self.assertEqual(forms("![x](C:\\tmp\\a.png)"), ["local-path"])
        self.assertEqual(forms("![x](file:///tmp/a.png)"), ["local-path"])
        self.assertEqual(forms("![x](.artifacts/a.png)"), ["relative-path"])

    def test_branch_named_raw_and_blob_rejected(self):
        self.assertEqual(forms("![x](https://github.com/o/r/raw/main/a.png)"), ["unpinned-ref"])
        self.assertEqual(forms("![x](https://github.com/o/r/blob/main/a.png)"), ["unpinned-ref"])
        self.assertEqual(forms(f"![x](https://github.com/o/r/raw/{'b' * 39}/a.png)"), ["unpinned-ref"])

    def test_release_asset_and_foreign_host_rejected(self):
        self.assertEqual(forms("![x](https://github.com/o/r/releases/download/v1/a.png)"), ["release-asset"])
        self.assertEqual(forms("![x](https://0x0.st/abc.png)"), ["foreign-host"])

    def test_html_media_tags_rejected(self):
        self.assertEqual(forms(f'<img src="https://github.com/user-attachments/assets/{UUID}" width=300>'), ["html-media-tag"])
        self.assertEqual(forms("<video src='x'></video>"), ["html-media-tag"])

    def test_bare_foreign_media_url_rejected(self):
        self.assertEqual(forms("see https://example.com/demo.mp4 for the demo"), ["foreign-host"])

    def test_code_fences_and_inline_code_are_ignored(self):
        text = "Do not use `![x](C:\\a.png)`.\n```\n<img src=x>\nhttps://raw.githubusercontent.com/a/b\n```\n"
        self.assertEqual(el.lint_text(text), [])

    def test_approved_pinned_url_containing_banned_host_text_passes(self):
        self.assertEqual(el.lint_text(f"![x](https://github.com/o/r/raw/{SHA}/notes-on-raw.githubusercontent.com.png)"), [])

    def test_extensionless_banned_host_in_reference_style_is_rejected(self):
        text = "![x][ref]\n\n[ref]: https://i.ibb.co/Xy/shot\n"
        self.assertEqual(forms(text), ["foreign-host"])

    def test_plain_links_are_not_media(self):
        self.assertEqual(el.lint_text("See https://github.com/o/r/issues/1 and https://example.com/page"), [])


class HtmlExtraction(unittest.TestCase):
    def test_extracts_img_video_source(self):
        html = (
            '<p><img src="https://camo.example/a.png"></p>'
            '<video src="https://github.com/user-attachments/assets/x"></video>'
            '<video><source src="https://h/v.mp4"></video><img src="/relative.png">'
        )
        self.assertEqual(
            el.extract_media_urls(html),
            [("img", "https://camo.example/a.png"), ("video", "https://github.com/user-attachments/assets/x"), ("source", "https://h/v.mp4")],
        )

    def test_verify_fails_closed_without_media(self):
        orig = el.fetch_rendered_html
        el.fetch_rendered_html = lambda url, timeout=60: "<p>no media</p>"
        try:
            passed, results = el.verify_posted("https://github.com/o/r/issues/1", retries=1, delay=0)
        finally:
            el.fetch_rendered_html = orig
        self.assertFalse(passed)
        self.assertEqual(results, [])

    def test_verify_requires_every_media_ok(self):
        orig_f, orig_c = el.fetch_rendered_html, el.check_media_url
        el.fetch_rendered_html = lambda url, timeout=60: '<img src="https://h/a.png"><img src="https://h/b.png">'
        el.check_media_url = lambda url, timeout=30: (url.endswith("a.png"), "stub")
        try:
            passed, results = el.verify_posted("https://github.com/o/r/issues/1", retries=1, delay=0)
        finally:
            el.fetch_rendered_html, el.check_media_url = orig_f, orig_c
        self.assertFalse(passed)
        self.assertEqual([r["ok"] for r in results], [True, False])

    def test_bad_url_rejected(self):
        with self.assertRaises(ValueError):
            el.fetch_rendered_html("https://example.com/x")


if __name__ == "__main__":
    unittest.main()
