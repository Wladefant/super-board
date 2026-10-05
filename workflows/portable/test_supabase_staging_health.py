#!/usr/bin/env python3
"""test_supabase_staging_health.py - change detection, throttle, dedupe, dry-run purity, scope guard."""

import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import supabase_staging_health as h


def snap(sec=None, perf=None, pg=None, edge=None, log_error=None):
    return {
        "window": {"start": "2026-10-05T00:00:00Z", "end": "2026-10-05T06:00:00Z"},
        "advisors": {"security": sec or {}, "performance": perf or {}},
        "logs": {} if log_error else {"sources": {"postgres_logs": 10}, "postgres_errors": pg or {}, "edge_5xx": edge or {}},
        "log_error": log_error,
    }


class Diff(unittest.TestCase):
    def test_identical_snapshots_have_no_changes(self):
        s = snap(sec={"a": "rls|INFO"}, pg={"ERROR/57014": 600})
        self.assertEqual(h.diff(s, s), [])

    def test_first_snapshot_is_a_baseline(self):
        self.assertEqual(len(h.diff(None, snap())), 1)

    def test_new_and_resolved_advisor_findings(self):
        prev = snap(sec={"a": "rls|INFO", "b": "rls|INFO"})
        cur = snap(sec={"b": "rls|INFO", "c": "ext|WARN"})
        text = "\n".join(h.diff(prev, cur))
        self.assertIn("security advisor new: 1", text)
        self.assertIn("c (ext|WARN)", text)
        self.assertIn("security advisor resolved: 1", text)

    def test_level_change_is_reported(self):
        text = "\n".join(h.diff(snap(perf={"x": "idx|INFO"}), snap(perf={"x": "idx|WARN"})))
        self.assertIn("level changed", text)

    def test_log_count_noise_ignored_but_big_move_reported(self):
        prev = snap(pg={"ERROR/57014": 600})
        self.assertEqual(h.diff(prev, snap(pg={"ERROR/57014": 900})), [])
        self.assertEqual(h.diff(prev, snap(pg={"ERROR/57014": 150})), [])
        self.assertTrue(h.diff(prev, snap(pg={"ERROR/57014": 3100})))
        self.assertTrue(h.diff(prev, snap(pg={"ERROR/57014": 100})))

    def test_new_and_gone_log_signature(self):
        lines = h.diff(snap(pg={"ERROR/57014": 5}), snap(pg={"FATAL/25P03": 2}))
        self.assertTrue(any("new signature FATAL/25P03" in l for l in lines))
        self.assertTrue(any("signature gone ERROR/57014" in l for l in lines))

    def test_log_outage_reported_once(self):
        ok, bad = snap(), snap(log_error="logs query failed: x")
        self.assertTrue(h.diff(ok, bad))
        self.assertEqual(h.diff(bad, bad), [])
        self.assertIn("works again", "\n".join(h.diff(bad, ok)))


class Throttle(unittest.TestCase):
    def test_too_soon_boundary(self):
        now = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)
        self.assertTrue(h.too_soon({"last_run_utc": "2026-10-05T06:01:00Z"}, now, 6))
        self.assertFalse(h.too_soon({"last_run_utc": "2026-10-05T06:00:00Z"}, now, 6))
        self.assertFalse(h.too_soon({}, now, 6))

    def test_window_is_hour_aligned_and_explicit(self):
        now = dt.datetime(2026, 10, 5, 12, 47, 9, tzinfo=dt.timezone.utc)
        self.assertEqual(h.log_window(now, 6), ("2026-10-05T06:00:00Z", "2026-10-05T12:00:00Z"))


class RunFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.patches = [
            mock.patch.object(h, "load_pat", return_value="x"),
            mock.patch.object(h, "verify_staging_only"),
            mock.patch.object(h, "STATE_DIR", self.tmp),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def run_with(self, cur, live=True, force=False):
        posts = []
        with mock.patch.object(h, "collect", return_value=cur):
            rc = h.run(live, force, self.tmp, 6.0, 6, post=lambda b: posts.append(b) or "https://github.com/x/y/issues/1#c")
        self.assertEqual(rc, 0)
        return posts

    def test_dry_run_writes_nothing_and_posts_nothing(self):
        self.assertEqual(self.run_with(snap(), live=False), [])
        self.assertFalse((self.tmp / "state.json").exists())

    def test_posts_only_on_change_then_throttles(self):
        a = snap(sec={"a": "rls|INFO"})
        self.assertEqual(len(self.run_with(a)), 1)  # baseline
        self.assertEqual(self.run_with(a, force=True), [])  # unchanged -> silent
        self.assertEqual(self.run_with(snap(sec={"a": "rls|INFO", "b": "rls|INFO"})), [])  # under 6 h -> skipped
        self.assertEqual(len(self.run_with(snap(sec={"a": "rls|INFO", "b": "rls|INFO"}), force=True)), 1)

    def test_failed_post_keeps_old_snapshot_so_change_is_retried(self):
        a = snap(sec={"a": "rls|INFO"})
        self.run_with(a)
        b = snap(sec={"a": "rls|INFO", "b": "rls|INFO"})
        with mock.patch.object(h, "collect", return_value=b):
            with self.assertRaises(h.HealthError):
                h.run(True, True, self.tmp, 6.0, 6, post=mock.Mock(side_effect=h.HealthError("boom")))
        self.assertEqual(len(self.run_with(b, force=True)), 1)

    def test_comment_never_contains_pat(self):
        body = h.render_comment(snap(sec={"a": "rls|INFO"}), ["x"], "2026-10-05T00:00:00Z")
        self.assertNotIn("sbp_", body)


class ScopeGuard(unittest.TestCase):
    def test_production_visible_token_is_refused(self):
        with mock.patch.object(h, "api_get", return_value=[{"id": h.STAGING_REF}, {"id": "other"}]):
            with self.assertRaises(h.HealthError):
                h.verify_staging_only("t")

    def test_staging_only_token_passes(self):
        with mock.patch.object(h, "api_get", return_value=[{"id": h.STAGING_REF}]):
            h.verify_staging_only("t")


if __name__ == "__main__":
    unittest.main()
