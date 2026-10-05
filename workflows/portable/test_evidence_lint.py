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


class DoneReport(unittest.TestCase):
    def kinds(self, body, deleted=None):
        return sorted(v.form for v in el.lint_done_report(body, deleted))

    def test_body_without_the_lines_fails(self):
        self.assertEqual(self.kinds("Summary only."), ["missing-deleted", "missing-not-run"])

    def test_body_with_none_lines_passes(self):
        self.assertEqual(self.kinds("Deleted: none\nNot run: none\n"), [])

    def test_lists_and_markdown_decoration_pass(self):
        self.assertEqual(self.kinds("- **Deleted:** old.py, docs/old.md\n* Not run: e2e (no browser)\n"), [])

    def test_empty_template_lines_count_as_missing(self):
        self.assertEqual(self.kinds("Deleted: \nNot run: \n"), ["missing-deleted", "missing-not-run"])

    def test_html_comment_and_code_fence_do_not_count(self):
        body = "<!-- Deleted: none -->\n```\nNot run: none\n```\n"
        self.assertEqual(self.kinds(body), ["missing-deleted", "missing-not-run"])

    def test_deleted_none_with_deleted_files_is_flagged(self):
        self.assertEqual(self.kinds("Deleted: none\nNot run: none", ["a/old.py"]), ["deleted-mismatch"])

    def test_deleted_list_with_deleted_files_passes(self):
        self.assertEqual(self.kinds("Deleted: a/old.py\nNot run: none", ["a/old.py"]), [])

    def test_deleted_paths_come_from_change_type(self):
        files = [{"path": "a", "changeType": "DELETED"}, {"path": "b", "changeType": "MODIFIED"}]
        self.assertEqual(el._deleted_paths(files), ["a"])

    def test_cli_warn_only_exits_zero_and_strict_exits_one(self):
        import io, contextlib, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf8") as fh:
            fh.write("no report here")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(el.main(["done-report", fh.name, "--warn-only"]), 0)
            self.assertEqual(el.main(["done-report", fh.name]), 1)
        os.unlink(fh.name)
        self.assertIn("WARN", buf.getvalue())
        self.assertIn("FAIL", buf.getvalue())

    def test_recap_template_always_renders_both_lines(self):
        import github_plan_templates as gpt
        recap = gpt.GitHubPrRecap(pr_number=1, head_sha="a" * 40, base_branch="main", title="t", summary="s")
        self.assertEqual(el.lint_done_report(recap.render()), [])
        recap.deleted = ["x.py"]
        self.assertIn("Deleted: x.py", recap.render())


if __name__ == "__main__":
    unittest.main()
