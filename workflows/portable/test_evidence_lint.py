#!/usr/bin/env python3
"""test_evidence_lint.py - every rejected link form, every accepted form, HTML media extraction."""

import os
import shutil
import sys
import tempfile
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


class PairComparison(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.tmp_dir = tempfile.mkdtemp(prefix="el_pair_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_png(self, name, size=(100, 100), color=(30, 60, 90), extra_info=None):
        from PIL import Image, PngImagePlugin
        im = Image.new("RGB", size, color)
        path = os.path.join(self.tmp_dir, name)
        info = PngImagePlugin.PngInfo()
        if extra_info:
            for k, v in extra_info.items():
                info.add_text(k, v)
        im.save(path, format="PNG", pnginfo=info)
        return path

    def test_pair_byte_identical_rejected(self):
        p1 = self._make_png("base.png")
        p2 = os.path.join(self.tmp_dir, "copy.png")
        shutil.copyfile(p1, p2)
        ok, reason, ratio = el.compare_pair(p1, p2)
        self.assertFalse(ok)
        self.assertIn("byte-identical", reason)
        rc = el.main(["pair", p1, p2])
        self.assertEqual(rc, 1)

    def test_pair_reencoded_same_pixels_rejected(self):
        p1 = self._make_png("p1.png", extra_info={"meta": "first"})
        p2 = self._make_png("p2.png", extra_info={"meta": "second"})
        # Verify bytes differ but pixels are identical
        with open(p1, "rb") as f1, open(p2, "rb") as f2:
            self.assertNotEqual(f1.read(), f2.read())
        ok, reason, ratio = el.compare_pair(p1, p2)
        self.assertFalse(ok)
        self.assertIn("reencoded-identical", reason)
        self.assertEqual(ratio, 0.0)
        rc = el.main(["pair", p1, p2])
        self.assertEqual(rc, 1)

    def test_pair_tiny_change_rejected(self):
        from PIL import Image
        # 100x100 = 10,000 pixels. 1 pixel changed = 0.0001 < 0.0005
        p1 = self._make_png("tiny1.png")
        im = Image.open(p1)
        im.putpixel((0, 0), (31, 60, 90))
        p2 = os.path.join(self.tmp_dir, "tiny2.png")
        im.save(p2, format="PNG")
        ok, reason, ratio = el.compare_pair(p1, p2)
        self.assertFalse(ok)
        self.assertIn("near-identical", reason)
        self.assertLess(ratio, 0.0005)
        rc = el.main(["pair", p1, p2])
        self.assertEqual(rc, 1)

    def test_pair_real_change_accepted(self):
        from PIL import Image
        # 100x100 = 10,000 pixels. 100 pixels changed = 0.01 >= 0.0005
        p1 = self._make_png("real1.png")
        im = Image.open(p1)
        for x in range(10):
            for y in range(10):
                im.putpixel((x, y), (200, 200, 200))
        p2 = os.path.join(self.tmp_dir, "real2.png")
        im.save(p2, format="PNG")
        ok, reason, ratio = el.compare_pair(p1, p2)
        self.assertTrue(ok)
        self.assertIn("ok", reason)
        self.assertGreaterEqual(ratio, 0.0005)
        rc = el.main(["pair", p1, p2])
        self.assertEqual(rc, 0)

    def test_pair_dimension_mismatch_rejected(self):
        p1 = self._make_png("dim1.png", size=(100, 100))
        p2 = self._make_png("dim2.png", size=(100, 120))
        ok, reason, ratio = el.compare_pair(p1, p2)
        self.assertFalse(ok)
        self.assertIn("dimension-mismatch", reason)
        rc = el.main(["pair", p1, p2])
        self.assertEqual(rc, 1)

    def test_pair_pr836_fixtures_rejected(self):
        pairs_json = r"C:/Users/wkiri/.veyyon/tmp/qas-pairs.json"
        if not os.path.exists(pairs_json):
            self.skipTest("qas-pairs.json not on host")
        import json
        with open(pairs_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        pr836 = [d for d in data if d.get("pr") == 836]
        self.assertEqual(len(pr836), 8)
        for entry in pr836:
            p1, p2 = entry["paths"]
            if not (os.path.exists(p1) and os.path.exists(p2)):
                self.skipTest(f"Missing fixture file {p1} or {p2}")
            ok, reason, ratio = el.compare_pair(p1, p2)
            self.assertFalse(ok, f"Expected PR836 pair to be rejected: {p1} vs {p2}")
            self.assertTrue("byte-identical" in reason or "near-identical" in reason)
            rc = el.main(["pair", p1, p2])
            self.assertEqual(rc, 1)

    def test_pair_invalid_thresholds_rejected(self):
        p1 = self._make_png("thresh1.png")
        p2 = self._make_png("thresh2.png")
        for invalid in (float("nan"), float("inf"), float("-inf"), -0.01, 0.0, 0.0001, 0.000499):
            ok, reason, ratio = el.compare_pair(p1, p2, threshold=invalid)
            self.assertFalse(ok, f"Expected threshold {invalid} to be rejected")
            self.assertIn("invalid-threshold", reason)
            self.assertIsNone(ratio)
            rc = el.main(["pair", p1, p2, f"--threshold={invalid}"])
            self.assertEqual(rc, 1, f"Expected CLI exit 1 for threshold {invalid}")

        # Strict higher threshold is fine
        from PIL import Image
        im = Image.open(p1)
        for x in range(10):
            for y in range(10):
                im.putpixel((x, y), (200, 200, 200))
        p_changed = os.path.join(self.tmp_dir, "thresh_changed.png")
        im.save(p_changed, format="PNG")
        # 100 pixels / 10000 = 0.01. Valid for 0.0005, but fails strict threshold 0.05
        ok_loose, reason_loose, ratio_loose = el.compare_pair(p1, p_changed, threshold=0.0005)
        self.assertTrue(ok_loose)
        ok_strict, reason_strict, ratio_strict = el.compare_pair(p1, p_changed, threshold=0.05)
        self.assertFalse(ok_strict)
        self.assertIn("near-identical", reason_strict)


class VerifyPostedPairs(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="el_vp_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _make_png(self, name, size=(100, 100), color=(30, 60, 90)):
        from PIL import Image
        im = Image.new("RGB", size, color)
        path = os.path.join(self.tmp_dir, name)
        im.save(path, format="PNG")
        return path

    def test_verify_posted_two_pairs_second_invalid(self):
        from PIL import Image
        # Pair 1: real change
        p1_before = self._make_png("p1_b.png")
        im = Image.open(p1_before)
        for i in range(20):
            im.putpixel((i, i), (255, 0, 0))
        p1_after = os.path.join(self.tmp_dir, "p1_a.png")
        im.save(p1_after)

        # Pair 2: byte-identical
        p2_before = self._make_png("p2_b.png", color=(10, 20, 30))
        p2_after = os.path.join(self.tmp_dir, "p2_a.png")
        shutil.copyfile(p2_before, p2_after)

        u_p1_b = "https://github.com/user-attachments/assets/11111111-1111-1111-1111-111111111111"
        u_p1_a = "https://github.com/user-attachments/assets/22222222-2222-2222-2222-222222222222"
        u_p2_b = "https://github.com/user-attachments/assets/33333333-3333-3333-3333-333333333333"
        u_p2_a = "https://github.com/user-attachments/assets/44444444-4444-4444-4444-444444444444"

        with open(p1_before, "rb") as f: d_p1_b = f.read()
        with open(p1_after, "rb") as f: d_p1_a = f.read()
        with open(p2_before, "rb") as f: d_p2_b = f.read()
        with open(p2_after, "rb") as f: d_p2_a = f.read()

        url_map = {u_p1_b: d_p1_b, u_p1_a: d_p1_a, u_p2_b: d_p2_b, u_p2_a: d_p2_a}

        html = f"""
        <table>
          <thead>
            <tr><th>Viewport</th><th>Before</th><th>After</th></tr>
          </thead>
          <tbody>
            <tr><td>Desktop</td><td><img src="{u_p1_b}"></td><td><img src="{u_p1_a}"></td></tr>
            <tr><td>Mobile</td><td><img src="{u_p2_b}"></td><td><img src="{u_p2_a}"></td></tr>
          </tbody>
        </table>
        """
        orig_fetch = el.fetch_rendered_html
        orig_check = el.check_media_url
        orig_bytes = el.fetch_media_bytes
        try:
            el.fetch_rendered_html = lambda url, timeout=60: html
            el.check_media_url = lambda url, timeout=30: (True, "HTTP 200 image/png")
            el.fetch_media_bytes = lambda url, timeout=30: url_map[url]
            passed, results = el.verify_posted("https://github.com/o/r/issues/1#issuecomment-100")
            self.assertFalse(passed)
            pair_results = [r for r in results if r.get("tag") == "pair"]
            self.assertEqual(len(pair_results), 2)
            self.assertTrue(pair_results[0]["ok"])
            self.assertFalse(pair_results[1]["ok"])
            self.assertIn("byte-identical", pair_results[1]["detail"])
        finally:
            el.fetch_rendered_html = orig_fetch
            el.check_media_url = orig_check
            el.fetch_media_bytes = orig_bytes
    def test_verify_posted_side_by_side_gallery_not_mistaken_for_pair(self):
        p1 = self._make_png("g1.png")
        p2 = self._make_png("g2.png")
        u1 = "https://github.com/user-attachments/assets/55555555-5555-5555-5555-555555555555"
        u2 = "https://github.com/user-attachments/assets/66666666-6666-6666-6666-666666666666"
        html = f"""
        <table>
          <thead>
            <tr><th>Mobile (390px)</th><th>Desktop (1440px)</th></tr>
          </thead>
          <tbody>
            <tr><td><img src="{u1}"></td><td><img src="{u2}"></td></tr>
          </tbody>
        </table>
        """
        orig_fetch = el.fetch_rendered_html
        orig_check = el.check_media_url
        try:
            el.fetch_rendered_html = lambda url, timeout=60: html
            el.check_media_url = lambda url, timeout=30: (True, "HTTP 200 image/png")
            passed, results = el.verify_posted("https://github.com/o/r/issues/1#issuecomment-100")
            self.assertTrue(passed)
            pair_results = [r for r in results if r.get("tag") == "pair"]
            self.assertEqual(len(pair_results), 0)
        finally:
            el.fetch_rendered_html = orig_fetch
            el.check_media_url = orig_check
    def test_verify_posted_ordered_images_with_generic_alt(self):
        from PIL import Image
        p1_b = self._make_png("gen1_b.png")
        im = Image.open(p1_b)
        for i in range(15):
            im.putpixel((i, i), (255, 255, 0))
        p1_a = os.path.join(self.tmp_dir, "gen1_a.png")
        im.save(p1_a)

        u1_b = "https://github.com/user-attachments/assets/77777777-7777-7777-7777-777777777777"
        u1_a = "https://github.com/user-attachments/assets/88888888-8888-8888-8888-888888888888"
        with open(p1_b, "rb") as f: d1_b = f.read()
        with open(p1_a, "rb") as f: d1_a = f.read()
        url_map = {u1_b: d1_b, u1_a: d1_a}

        html = f"""
        <table>
          <thead>
            <tr><th>Screen</th><th>Before</th><th>After</th></tr>
          </thead>
          <tbody>
            <tr><td>Settings</td><td><img src="{u1_b}" alt="image"></td><td><img src="{u1_a}" alt="image"></td></tr>
          </tbody>
        </table>
        """
        orig_fetch = el.fetch_rendered_html
        orig_check = el.check_media_url
        orig_bytes = el.fetch_media_bytes
        try:
            el.fetch_rendered_html = lambda url, timeout=60: html
            el.check_media_url = lambda url, timeout=30: (True, "HTTP 200 image/png")
            el.fetch_media_bytes = lambda url, timeout=30: url_map[url]
            passed, results = el.verify_posted("https://github.com/o/r/issues/1#issuecomment-100")
            self.assertTrue(passed)
            pair_results = [r for r in results if r.get("tag") == "pair"]
            self.assertEqual(len(pair_results), 1)
            self.assertTrue(pair_results[0]["ok"])
        finally:
            el.fetch_rendered_html = orig_fetch
            el.check_media_url = orig_check
            el.fetch_media_bytes = orig_bytes
    def test_verify_posted_two_pairs_second_malformed(self):
        from PIL import Image
        # Pair 1: real change
        p1_b = self._make_png("m1_b.png")
        im = Image.open(p1_b)
        for i in range(20):
            im.putpixel((i, i), (255, 0, 0))
        p1_a = os.path.join(self.tmp_dir, "m1_a.png")
        im.save(p1_a)

        # Pair 2: before has image, after has NO image (row missing one side)
        p2_b = self._make_png("m2_b.png", color=(10, 20, 30))

        u1_b = "https://github.com/user-attachments/assets/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        u1_a = "https://github.com/user-attachments/assets/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        u2_b = "https://github.com/user-attachments/assets/cccccccc-cccc-cccc-cccc-cccccccccccc"

        with open(p1_b, "rb") as f: d1_b = f.read()
        with open(p1_a, "rb") as f: d1_a = f.read()
        with open(p2_b, "rb") as f: d2_b = f.read()

        url_map = {u1_b: d1_b, u1_a: d1_a, u2_b: d2_b}

        html = f"""
        <table>
          <thead>
            <tr><th>Viewport</th><th>Before</th><th>After</th></tr>
          </thead>
          <tbody>
            <tr><td>Desktop</td><td><img src="{u1_b}"></td><td><img src="{u1_a}"></td></tr>
            <tr><td>Mobile</td><td><img src="{u2_b}"></td><td></td></tr>
          </tbody>
        </table>
        """
        orig_fetch = el.fetch_rendered_html
        orig_check = el.check_media_url
        orig_bytes = el.fetch_media_bytes
        try:
            el.fetch_rendered_html = lambda url, timeout=60: html
            el.check_media_url = lambda url, timeout=30: (True, "HTTP 200 image/png")
            el.fetch_media_bytes = lambda url, timeout=30: url_map[url]
            passed, results = el.verify_posted("https://github.com/o/r/issues/1#issuecomment-100")
            self.assertFalse(passed)
            pair_results = [r for r in results if r.get("tag") == "pair"]
            self.assertEqual(len(pair_results), 2)
            self.assertTrue(pair_results[0]["ok"])
            self.assertFalse(pair_results[1]["ok"])
            self.assertTrue(
                "unequal" in pair_results[1]["detail"] or "malformed" in pair_results[1]["detail"],
                f"Expected unequal/malformed detail, got {pair_results[1]['detail']}",
            )
        finally:
            el.fetch_rendered_html = orig_fetch
            el.check_media_url = orig_check
            el.fetch_media_bytes = orig_bytes

    def test_verify_posted_row_holding_two_images_vs_one_fails_closed(self):
        from PIL import Image
        p1_b = self._make_png("two1_b.png")
        p2_b = self._make_png("two2_b.png")
        p_a = self._make_png("two_a.png")

        u1_b = "https://github.com/user-attachments/assets/dddddddd-dddd-dddd-dddd-dddddddddddd"
        u2_b = "https://github.com/user-attachments/assets/eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        u_a = "https://github.com/user-attachments/assets/ffffffff-ffff-ffff-ffff-ffffffffffff"

        with open(p1_b, "rb") as f: d1_b = f.read()
        with open(p2_b, "rb") as f: d2_b = f.read()
        with open(p_a, "rb") as f: d_a = f.read()

        url_map = {u1_b: d1_b, u2_b: d2_b, u_a: d_a}

        html = f"""
        <table>
          <thead>
            <tr><th>Viewport</th><th>Before</th><th>After</th></tr>
          </thead>
          <tbody>
            <tr><td>Desktop</td><td><img src="{u1_b}"><img src="{u2_b}"></td><td><img src="{u_a}"></td></tr>
          </tbody>
        </table>
        """
        orig_fetch = el.fetch_rendered_html
        orig_check = el.check_media_url
        orig_bytes = el.fetch_media_bytes
        try:
            el.fetch_rendered_html = lambda url, timeout=60: html
            el.check_media_url = lambda url, timeout=30: (True, "HTTP 200 image/png")
            el.fetch_media_bytes = lambda url, timeout=30: url_map[url]
            passed, results = el.verify_posted("https://github.com/o/r/issues/1#issuecomment-100")
            self.assertFalse(passed)
            pair_results = [r for r in results if r.get("tag") == "pair"]
            self.assertEqual(len(pair_results), 1)
            self.assertFalse(pair_results[0]["ok"])
            self.assertTrue(
                "unequal" in pair_results[0]["detail"] or "malformed" in pair_results[0]["detail"],
                f"Expected unequal/malformed detail, got {pair_results[0]['detail']}",
            )
        finally:
            el.fetch_rendered_html = orig_fetch
            el.check_media_url = orig_check
            el.fetch_media_bytes = orig_bytes


class FetchMediaBytesAuth(unittest.TestCase):
    def setUp(self):
        self.orig_gh_token = os.environ.get("GH_TOKEN")
        self.orig_github_token = os.environ.get("GITHUB_TOKEN")
        os.environ["GH_TOKEN"] = "test-token-safe-do-not-leak"
        os.environ.pop("GITHUB_TOKEN", None)

    def tearDown(self):
        if self.orig_gh_token is not None:
            os.environ["GH_TOKEN"] = self.orig_gh_token
        else:
            os.environ.pop("GH_TOKEN", None)
        if self.orig_github_token is not None:
            os.environ["GITHUB_TOKEN"] = self.orig_github_token
        else:
            os.environ.pop("GITHUB_TOKEN", None)

    def test_fetch_media_bytes_token_hostname_allowlist(self):
        captured = []

        class DummyResp:
            def read(self):
                return b"dummy-data"
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        class DummyOpener:
            def open(self, req, timeout=30):
                captured.append(req)
                return DummyResp()

        orig_build_opener = el.urllib.request.build_opener
        try:
            el.urllib.request.build_opener = lambda *handlers: DummyOpener()

            # Allowed hosts: github.com and api.github.com
            el.fetch_media_bytes("https://github.com/user-attachments/assets/123")
            req1 = captured[-1]
            self.assertEqual(req1.headers.get("Authorization"), "token test-token-safe-do-not-leak")

            el.fetch_media_bytes("https://api.github.com/repos/o/r/raw/abc")
            req2 = captured[-1]
            self.assertEqual(req2.headers.get("Authorization"), "token test-token-safe-do-not-leak")

            # Forbidden hosts: evilgithub.com, github.com.attacker.test, path spoofing, signed private URLs
            forbidden_urls = [
                "https://evilgithub.com/asset.png",
                "https://github.com.attacker.test/asset.png",
                "https://attacker.test/github.com/image",
                "https://private-user-images.githubusercontent.com/pic.png?jwt=safe",
            ]
            for url in forbidden_urls:
                el.fetch_media_bytes(url)
                req = captured[-1]
                self.assertNotIn(
                    "Authorization",
                    req.headers,
                    f"Authorization header must not be sent to {url}",
                )
        finally:
            el.urllib.request.build_opener = orig_build_opener

    def test_redirect_strips_authorization_header(self):
        req = el.urllib.request.Request(
            "https://github.com/asset",
            headers={"Authorization": "token test-token-safe-do-not-leak"},
        )
        handler = el._AuthRedirectHandler()
        new_req = handler.redirect_request(
            req, None, 302, "Found", {}, "https://private-user-images.githubusercontent.com/pic"
        )
        self.assertNotIn("Authorization", new_req.headers)
        self.assertNotIn("authorization", new_req.headers)
        if hasattr(new_req, "unredirected_hdrs"):
            self.assertNotIn("Authorization", new_req.unredirected_hdrs)
            self.assertNotIn("authorization", new_req.unredirected_hdrs)
if __name__ == "__main__":
    unittest.main()
