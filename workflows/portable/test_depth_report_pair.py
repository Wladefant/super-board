#!/usr/bin/env python3
"""
Tests for depth_report_pair.py: which before/after depth-report captures may be posted as a pair.
Tracking: https://github.com/Wladefant/super-board/issues/558
"""

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import depth_report_pair as pair  # noqa: E402
import github_pr_gate  # noqa: E402

BEFORE_SHA = "50c3de20a490455079c27337003893e5f4cc8d89"
AFTER_SHA = "bfaf3c27735a8ce9b056a228663ab408e2628ac2"
KEYS = [("1440x900", "light"), ("1440x900", "dark"), ("390x844", "light"), ("390x844", "dark")]


def page(theme: str, changed: bool = False, dots: int = 0) -> Image.Image:
    """A fake report page: a header bar, plus a large card when `changed`, plus `dots` single pixels."""
    im = Image.new("RGB", (200, 300), "white" if theme == "light" else "black")
    draw = ImageDraw.Draw(im)
    draw.rectangle((0, 0, 199, 30), fill=(40, 90, 200))
    if changed:
        draw.rectangle((20, 60, 180, 200), fill=(200, 60, 40))
    for i in range(dots):
        im.putpixel((5 + i, 280), (255, 0, 0))
    return im


def write_capture(root: Path, sha: str, pages: dict, passed: bool = True) -> Path:
    """Write PNGs plus the manifest.json that depth_report_capture.mjs writes."""
    root.mkdir(parents=True, exist_ok=True)
    shots = []
    for (viewport, theme), im in pages.items():
        name = f"depth-report-{viewport}-{theme}-{sha[:12]}.png"
        im.save(root / name)
        digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        shots.append({"viewport": viewport, "theme": theme, "file": name, "sha256": digest, "checks": []})
    manifest = {"served_sha": sha, "surveyed_sha": sha, "template": "t", "passed": passed, "failed": [], "shots": shots}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


class TestPair(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def captures(self, before_sha=BEFORE_SHA, after_sha=AFTER_SHA, after_pages=None, name="", **kw):
        before = write_capture(self.root / f"before{name}", before_sha, {k: page(k[1]) for k in KEYS})
        after_pages = after_pages or {k: page(k[1], changed=True) for k in KEYS}
        return before, write_capture(self.root / f"after{name}", after_sha, after_pages, **kw)

    def assertRefused(self, problems, needle):
        self.assertTrue(any(needle in p for p in problems), f"no problem mentions {needle!r}: {problems}")

    def test_report_change_passes_pair_check_but_is_not_authenticated_product_evidence(self):
        problems, lines = pair.evaluate(*self.captures())
        self.assertEqual(problems, [])
        body = "| **1440** | ![before 1440](b.png) | ![after 1440](a.png) |\n\n```text\n" + "\n".join(lines) + "\n```\n"
        self.assertTrue(github_pr_gate.shot_provenance_problems(body, lambda sha: sha == AFTER_SHA, True))

    def test_a_shared_or_malformed_served_sha_is_refused(self):
        self.assertRefused(pair.evaluate(*self.captures(after_sha=BEFORE_SHA))[0], "same commit")
        self.assertRefused(pair.evaluate(*self.captures(after_sha="bfaf3c2", name="2"))[0], "40-hex")

    def test_a_failed_capture_or_a_shot_on_one_side_only_is_refused(self):
        self.assertRefused(pair.evaluate(*self.captures(passed=False))[0], "capture failed")
        after_pages = {k: page(k[1], changed=True) for k in KEYS[:3]}
        self.assertRefused(pair.evaluate(*self.captures(after_pages=after_pages, name="2"))[0], "390x844/dark")

    def test_an_image_changed_after_capture_is_refused(self):
        before, after = self.captures()
        manifest = json.loads((after / "manifest.json").read_text(encoding="utf-8"))
        page("light").save(after / manifest["shots"][0]["file"])
        self.assertRefused(pair.evaluate(before, after)[0], "changed after capture")

    def test_identical_and_near_identical_images_are_refused(self):
        self.assertRefused(pair.evaluate(*self.captures(after_pages={k: page(k[1]) for k in KEYS}))[0], "identical")
        problems = pair.evaluate(*self.captures(after_pages={k: page(k[1], dots=3) for k in KEYS}, name="2"))[0]
        self.assertEqual(len(problems), 4)
        self.assertRefused(problems, "near-identical")

    def test_only_compares_the_pairs_the_change_touches(self):
        before, after = self.captures(after_pages={k: page(k[1], changed=k[1] == "dark") for k in KEYS})
        self.assertEqual(len(pair.evaluate(before, after)[0]), 2)
        problems, lines = pair.evaluate(before, after, only=["1440x900/dark", "390x844/dark"])
        self.assertEqual(problems, [])
        self.assertEqual([line.split()[1] for line in lines if line.startswith("SHOT-PAIR")],
                         ["viewport=1440x900/dark", "viewport=390x844/dark"])
        self.assertRefused(pair.evaluate(before, after, only=["800x600/dark"])[0], "800x600/dark")

    def test_the_command_exits_nonzero_on_a_refusal_and_zero_on_a_pass(self):
        before, after = self.captures()
        for args, rc, expected in ((after, 0, "DEPTH-REPORT-PAIR: PASS 4 pair(s)"), (before, 1, "DEPTH-REPORT-PAIR: FAIL")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(pair.main(["--before", str(before), "--after", str(args)]), rc)
            self.assertIn(expected, out.getvalue())
            self.assertEqual(f"SHOT after served={AFTER_SHA}" in out.getvalue(), rc == 0)


if __name__ == "__main__":
    unittest.main()
