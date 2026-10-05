#!/usr/bin/env python3
"""
depth_report_pair.py - refuse a before/after pair of depth-report captures that proves nothing.

    python workflows/portable/depth_report_pair.py --before <dir> --after <dir> [--only 390x844/dark ...]

Each dir is the --output of one `depth_report_capture.mjs` run. Refused: a failed capture, the same
served commit on both sides, a viewport/theme shot on one side only, an image whose sha256 no longer
matches its manifest, and an identical or near-identical pair (dHash distance 3 or less and under 0.05%
of pixels moved by more than 16 levels: the metric of Bavariance/polysimulator `control_polysim.py pair`).
`--only` compares just the shots a change touches. A pass prints the `SHOT` and `SHOT-PAIR` lines that
`github_pr_gate.py` parses. Needs Pillow. Rule: https://github.com/Wladefant/super-board/issues/421
Tracking: https://github.com/Wladefant/super-board/issues/558
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from github_pr_gate import SHOT_MIN_CHANGED_RATIO, SHOT_NEAR_IDENTICAL_BITS

SHA40_RE = re.compile(r"[0-9a-f]{40}")
PIXEL_DELTA = 16


def dhash(path: Path) -> int:
    """64-bit difference hash: each bit says whether a pixel of the 9x8 grey thumbnail outshines its right neighbour."""
    from PIL import Image

    with Image.open(path) as im:
        gray = im.convert("L").resize((9, 8), Image.Resampling.LANCZOS).tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (gray[row * 9 + col] > gray[row * 9 + col + 1])
    return bits


def changed_ratio(before: Path, after: Path) -> float:
    """Fraction of pixels whose colour moved by more than PIXEL_DELTA levels; 1.0 when the sizes differ."""
    from PIL import Image, ImageChops

    with Image.open(before) as b, Image.open(after) as a:
        if b.size != a.size:
            return 1.0
        diff = ImageChops.difference(b.convert("RGB"), a.convert("RGB")).convert("L")
        return sum(diff.histogram()[PIXEL_DELTA + 1:]) / float(b.size[0] * b.size[1])


def load_capture(label: str, root: Path) -> Tuple[str, Dict[str, Dict[str, str]], List[str]]:
    """(served sha, shots by "viewport/theme", problems) for one capture directory."""
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return "", {}, [f"{label}: no readable manifest.json in {root}: {exc}"]
    sha = str(manifest.get("served_sha", "")).lower()
    problems = []
    if not SHA40_RE.fullmatch(sha):
        problems.append(f"{label}: served sha {sha!r} is not 40-hex")
    if manifest.get("passed") is not True:
        problems.append(f"{label}: the capture failed its checks: {manifest.get('failed')}")
    shots = {}
    for shot in manifest.get("shots", []):
        key = f"{shot['viewport']}/{shot['theme']}"
        image = root / shot["file"]
        if not image.is_file():
            problems.append(f"{label} {key}: {image.name} is missing")
        elif hashlib.sha256(image.read_bytes()).hexdigest() != shot["sha256"]:
            problems.append(f"{label} {key}: {image.name} changed after capture (sha256 differs from the manifest)")
        else:
            shots[key] = {"path": image, "sha256": shot["sha256"]}
    return sha, shots, problems


def evaluate(before_dir: Path, after_dir: Path, only: Sequence[str] = ()) -> Tuple[List[str], List[str]]:
    """(problems, gate lines). The lines are only meaningful when problems is empty."""
    before_sha, before, problems = load_capture("before", Path(before_dir))
    after_sha, after, after_problems = load_capture("after", Path(after_dir))
    problems += after_problems
    if before_sha and before_sha == after_sha:
        problems.append(f"before and after were served by the same commit {before_sha[:12]}")
    for key in sorted(set(before) ^ set(after)):
        problems.append(f"{key}: shot only on the {'before' if key in before else 'after'} side")
    lines = []
    for key in list(only) or [key for key in before if key in after]:
        if key not in before or key not in after:
            problems.append(f"{key}: --only names a shot that is not on both sides")
            continue
        b, a = before[key], after[key]
        hashes = (dhash(b["path"]), dhash(a["path"]))
        distance = bin(hashes[0] ^ hashes[1]).count("1")
        ratio = changed_ratio(b["path"], a["path"])
        if b["sha256"] == a["sha256"]:
            problems.append(f"{key}: before and after images are identical (same sha256)")
        elif distance <= SHOT_NEAR_IDENTICAL_BITS and ratio < SHOT_MIN_CHANGED_RATIO:
            problems.append(f"{key}: before and after are near-identical (dHash distance {distance}, {ratio:.4%} of pixels changed)")
        # `expected` equals `served`: the capture writes a manifest only after the served SHA matched --expected-sha.
        for label, sha, shot, h in (("before", before_sha, b, hashes[0]), ("after", after_sha, a, hashes[1])):
            lines.append(f"SHOT {label} served={sha} expected={sha} viewport={key} sha256={shot['sha256']} phash={h:016x}")
        lines.append(f"SHOT-PAIR viewport={key} phash_dist={distance} changed_ratio={ratio:.4f}")
    return problems, lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Refuse a before/after pair of depth-report captures that proves nothing.")
    ap.add_argument("--before", required=True, help="capture directory of the base commit")
    ap.add_argument("--after", required=True, help="capture directory of the change")
    ap.add_argument("--only", action="append", default=[], help="viewport/theme to compare, e.g. 390x844/dark")
    a = ap.parse_args(argv)
    problems, lines = evaluate(Path(a.before), Path(a.after), a.only)
    if problems:
        print("DEPTH-REPORT-PAIR: FAIL\n" + "\n".join(f"  - {problem}" for problem in problems))
        return 1
    print(f"DEPTH-REPORT-PAIR: PASS {len(lines) // 3} pair(s)\n```text\n" + "\n".join(lines) + "\n```")
    return 0


if __name__ == "__main__":
    sys.exit(main())
