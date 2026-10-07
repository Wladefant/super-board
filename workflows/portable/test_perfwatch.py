#!/usr/bin/env python3
"""test_perfwatch.py - N=3 gating, recovery, circuit breaker, production refusal, issue dedupe, caps, dry-run purity."""

import copy
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import perfwatch as pw

CFG = pw.load_config()
K = "shipnovo/web-home"
SIG = "ttfb_p50_ms"


def feed(state, values, key=K, sig=SIG, extra=None):
    """Run one evaluation per value. Returns the list of event lists."""
    out = []
    for i, v in enumerate(values):
        obs = {key: {sig: v}}
        obs.update(extra or {})
        ev, _ = pw.evaluate(state, obs, CFG, f"2026-10-07T{i:02d}:00:00Z")
        out.append(ev)
    return out


def learned(n=12, base=400.0):
    st = pw.new_state()
    feed(st, [base] * n)
    return st


class Gating(unittest.TestCase):
    def test_single_spike_opens_nothing(self):
        st = learned()
        evs = feed(st, [2000.0, 400.0, 400.0])
        self.assertEqual([e for e in evs if e], [])
        self.assertEqual(st["targets"][K]["status"], "ok")
        self.assertEqual(st["targets"][K]["signals"][SIG]["streak"], 0)

    def test_two_in_a_row_is_not_enough(self):
        st = learned()
        evs = feed(st, [2000.0, 2000.0, 400.0, 2000.0, 2000.0])
        self.assertEqual([e for e in evs if e], [])

    def test_three_in_a_row_confirms_once(self):
        st = learned()
        evs = feed(st, [2000.0, 2000.0, 2000.0, 2000.0, 2000.0])
        self.assertEqual([e["type"] for run in evs for e in run], ["confirmed"])
        self.assertEqual(len(evs[2]), 1)
        self.assertEqual(st["targets"][K]["status"], "confirmed")

    def test_breach_needs_relative_and_absolute_margin(self):
        st = learned(base=10.0)  # 10 ms -> 40 ms is +300 % but only 30 ms: under the 150 ms floor
        evs = feed(st, [40.0] * 5)
        self.assertEqual([e for e in evs if e], [])

    def test_baseline_ignores_breaching_samples(self):
        st = learned()
        feed(st, [2000.0, 2000.0])
        self.assertEqual(pw.baseline_of(st["targets"][K]["signals"][SIG], CFG), 400.0)

    def test_no_decision_while_learning(self):
        st = pw.new_state()
        evs = feed(st, [400.0, 400.0, 9000.0, 9000.0, 9000.0])
        self.assertEqual([e for e in evs if e], [])

    def test_recovery_needs_three_clean_runs(self):
        st = learned()
        feed(st, [2000.0] * 3)
        evs = feed(st, [400.0, 400.0])
        self.assertEqual([e for e in evs if e], [])
        self.assertEqual(st["targets"][K]["status"], "confirmed")
        evs = feed(st, [400.0])
        self.assertEqual([e["type"] for run in evs for e in run], ["recovered"])
        self.assertEqual(st["targets"][K]["status"], "ok")

    def test_unavailable_source_holds_the_streak(self):
        st = learned()
        feed(st, [2000.0, 2000.0])
        feed(st, [None])
        self.assertEqual(st["targets"][K]["signals"][SIG]["streak"], 2)
        evs = feed(st, [2000.0])
        self.assertEqual([e["type"] for run in evs for e in run], ["confirmed"])

    def test_second_signal_on_confirmed_target_is_added_not_reopened(self):
        st = learned()
        for i in range(12):
            pw.evaluate(st, {K: {"bytes": 20000.0}}, CFG, f"2026-10-06T{i:02d}:00:00Z")
        types = []
        for i in range(3):
            ev, _ = pw.evaluate(st, {K: {SIG: 2000.0, "bytes": 20000.0}}, CFG, f"2026-10-07T0{i}:00:00Z")
            types += [e["type"] for e in ev]
        self.assertEqual(types, ["confirmed"])
        types = []
        for i in range(3):
            ev, _ = pw.evaluate(st, {K: {SIG: 2000.0, "bytes": 90000.0}}, CFG, f"2026-10-08T0{i}:00:00Z")
            types += [e["type"] for e in ev]
        self.assertEqual(types, ["signals_added"])

    def test_container_signals_use_their_own_threshold(self):
        self.assertTrue(pw.is_breach(pw.metric_of("cpu_pct:shipnovo-app"), 60.0, 5.0, CFG["thresholds"]))
        self.assertFalse(pw.is_breach(pw.metric_of("cpu_pct:shipnovo-app"), 20.0, 1.0, CFG["thresholds"]))


class Breaker(unittest.TestCase):
    def test_mass_breach_changes_nothing(self):
        st = pw.new_state()
        keys = ["a/x", "b/x", "c/x", "d/x"]
        for i in range(12):
            pw.evaluate(st, {k: {SIG: 400.0} for k in keys}, CFG, f"2026-10-06T{i:02d}:00:00Z")
        for i in range(4):
            ev, suspect = pw.evaluate(st, {k: {SIG: 3000.0} for k in keys}, CFG, f"2026-10-07T0{i}:00:00Z")
            self.assertTrue(suspect)
            self.assertEqual(ev, [])
        self.assertTrue(all(t["signals"][SIG]["streak"] == 0 for t in st["targets"].values()))

    def test_one_target_breaching_is_not_suspect(self):
        st = pw.new_state()
        keys = ["a/x", "b/x", "c/x", "d/x"]
        for i in range(12):
            pw.evaluate(st, {k: {SIG: 400.0} for k in keys}, CFG, f"2026-10-06T{i:02d}:00:00Z")
        obs = {k: {SIG: 400.0} for k in keys}
        obs["a/x"][SIG] = 3000.0
        _, suspect = pw.evaluate(st, obs, CFG, "2026-10-07T00:00:00Z")
        self.assertFalse(suspect)


class Safety(unittest.TestCase):
    def test_production_hosts_are_refused(self):
        for url in ("https://polysimulator.com/", "https://www.polysimulator.com/", "https://app.polysimulator.com/",
                    "https://api.polysimulator.com/health", "https://zaraprptkegxqpvnsubu.supabase.co/",
                    "https://akamai-iad-prod.example.com/", "https://api.prod.example.com/"):
            with self.assertRaises(pw.Refused, msg=url):
                pw.check_url(url, ["polysimulator.com", "www.polysimulator.com", "app.polysimulator.com",
                                   "api.polysimulator.com", "zaraprptkegxqpvnsubu.supabase.co",
                                   "akamai-iad-prod.example.com", "api.prod.example.com"])

    def test_host_outside_config_and_plain_http_are_refused(self):
        with self.assertRaises(pw.Refused):
            pw.check_url("https://evil.example.com/", ["pinthread.dev"])
        with self.assertRaises(pw.Refused):
            pw.check_url("http://pinthread.dev/", ["pinthread.dev"])

    def test_staging_host_is_allowed(self):
        self.assertTrue(pw.check_url("https://app.staging.polysimulator.com/", ["app.staging.polysimulator.com"]))

    def test_config_with_production_is_rejected(self):
        bad = copy.deepcopy(CFG)
        bad["projects"][0]["hosts"].append("polysimulator.com")
        with self.assertRaises(pw.Refused):
            pw.validate_config(bad)
        bad = copy.deepcopy(CFG)
        bad["projects"][0]["targets"][3]["ref"] = "zaraprptkegxqpvnsubu"
        with self.assertRaises(pw.Refused):
            pw.validate_config(bad)

    def test_n_confirm_below_three_is_rejected(self):
        bad = copy.deepcopy(CFG)
        bad["n_confirm"] = 2
        with self.assertRaises(pw.PerfError):
            pw.validate_config(bad)

    def test_shipped_config_is_valid(self):
        pw.validate_config(CFG)
        self.assertEqual({p["name"] for p in CFG["projects"]}, {"polysimulator", "shipnovo", "pinthread", "mail-hub"})

    def test_redirect_is_a_failed_probe_unless_gated(self):
        r = {"status": 307}
        self.assertFalse(pw.response_ok(r, False))
        self.assertTrue(pw.response_ok({"status": 302}, True))
        self.assertFalse(pw.response_ok({"status": 502}, True))

    def test_budget_never_goes_negative(self):
        b = pw.Budget(2)
        self.assertEqual([b.take(), b.take(), b.take()], [True, True, False])


class Parsing(unittest.TestCase):
    def test_docker_stats(self):
        rows = pw.parse_docker_stats("app-1.x|0.25%|243.5MiB / 3.8GiB\nbad line\ndb|1.5%|1.5GiB / 2GiB\n")
        self.assertEqual([r["name"] for r in rows], ["app-1.x", "db"])
        self.assertAlmostEqual(rows[1]["mem_mib"], 1536.0)

    def test_container_missing_is_unavailable_not_zero(self):
        sig, notes = pw.container_signals({"shipnovo-app": "shipnovo-app-7vvuew"}, [{"name": "other", "cpu_pct": 1.0, "mem_mib": 1.0}])
        self.assertIsNone(sig["cpu_pct:shipnovo-app"])
        self.assertTrue(notes)


class FakeGh:
    def __init__(self, existing=None):
        self.calls, self.existing = [], existing or []

    def __call__(self, args, inp=None):
        self.calls.append((args, inp))
        path = args[1]
        if "/issues?" in path:
            return json.dumps(self.existing)
        if path.endswith("/issues") and "-X" not in args:
            return json.dumps({"number": 77, "html_url": "https://github.com/x/y/issues/77"})
        return "{}"

    def writes(self):
        return [c for c in self.calls if "/issues?" not in c[0][1]]


def confirmed_state(*keys):
    st = pw.new_state()
    for key in keys:
        feed(st, [400.0] * 12 + [2000.0] * 3, key=key)
        st["pending"].extend(e for e in [{"type": "confirmed", "key": key, "signals": [SIG], "at": "t"}])
    return st


class Publishing(unittest.TestCase):
    def test_creates_one_issue_with_marker_and_label(self):
        st, gh = confirmed_state(K), FakeGh()
        pub = pw.Publisher(CFG, gh)
        pub.publish(st)
        creates = gh.writes()
        self.assertEqual(len(creates), 1)
        payload = json.loads(creates[0][1])
        self.assertIn("<!-- perfwatch:key=shipnovo/web-home -->", payload["body"])
        self.assertEqual(payload["labels"], ["perfwatch"])
        self.assertIn("## Problem", payload["body"])
        self.assertEqual(st["pending"], [])
        self.assertEqual(st["targets"][K]["issue"]["number"], 77)

    def test_existing_open_issue_is_updated_not_duplicated(self):
        st = confirmed_state(K)
        existing = [{"number": 5, "state": "open", "html_url": "https://github.com/x/y/issues/5",
                     "body": pw.MARKER.format(key=K) + "\nold"}]
        gh = FakeGh(existing)
        pw.Publisher(CFG, gh).publish(st)
        writes = gh.writes()
        self.assertEqual(len(writes), 1)
        self.assertIn("-X", writes[0][0])
        self.assertIn("issues/5", writes[0][0][1])
        self.assertEqual(st["targets"][K]["issue"]["number"], 5)

    def test_issue_write_cap_keeps_the_rest_pending(self):
        keys = [f"p{i}/x" for i in range(5)]
        cfg = copy.deepcopy(CFG)
        cfg["projects"] = [{"name": f"p{i}", "repo": "o/r", "hosts": [], "targets": [{"name": "x", "kind": "http", "url": "https://h/"}]} for i in range(5)]
        st = confirmed_state(*keys)
        gh = FakeGh()
        pw.Publisher(cfg, gh).publish(st)
        self.assertEqual(len(gh.writes()), cfg["caps"]["issue_writes"])
        self.assertEqual(len(st["pending"]), 2)

    def test_open_issue_cap_boundary(self):
        cfg = copy.deepcopy(CFG)
        cfg["caps"]["open_issues"] = 2
        cfg["projects"] = [{"name": f"p{i}", "repo": "o/r", "hosts": [], "targets": [{"name": "x", "kind": "http", "url": "https://h/"}]} for i in range(3)]
        st = confirmed_state("p0/x", "p1/x", "p2/x")
        for k in ("p0/x", "p1/x"):
            st["targets"][k]["issue"] = {"repo": "o/r", "number": 1, "url": "u"}
            st["targets"][k]["status"] = "confirmed"
        st["pending"] = [e for e in st["pending"] if e["key"] == "p2/x"]
        gh = FakeGh()
        pw.Publisher(cfg, gh).publish(st)
        self.assertEqual(gh.writes(), [])
        self.assertEqual(len(st["pending"]), 1)

    def test_dry_run_writes_nothing(self):
        st, gh = confirmed_state(K), FakeGh()
        pub = pw.Publisher(CFG, gh, dry=True)
        pub.publish(st)
        self.assertEqual(gh.writes(), [])
        self.assertTrue(any("create issue" in a for a in pub.actions))


class Dispatch(unittest.TestCase):
    def _state(self):
        st = confirmed_state(K)
        st["targets"][K]["issue"] = {"repo": "r", "number": 1, "url": "https://github.com/r/1"}
        return st

    def test_host_ok_makes_it_pending_and_busy_host_defers(self):
        st = self._state()
        pw.refresh_dispatch(st, CFG, {"state": "no_spawn"}, "t")
        self.assertEqual(st["dispatch"][0]["status"], "deferred-host")
        pw.refresh_dispatch(st, CFG, {"state": "ok"}, "t")
        self.assertEqual(st["dispatch"][0]["status"], "pending")
        self.assertEqual(len(st["dispatch"]), 1)

    def test_dispatched_entry_is_kept_and_recovery_drops_pending(self):
        st = self._state()
        pw.refresh_dispatch(st, CFG, {"state": "ok"}, "t")
        st["dispatch"][0]["status"] = "dispatched"
        pw.refresh_dispatch(st, CFG, {"state": "ok"}, "t")
        self.assertEqual(st["dispatch"][0]["status"], "dispatched")
        st2 = self._state()
        pw.refresh_dispatch(st2, CFG, {"state": "ok"}, "t")
        st2["targets"][K]["status"] = "ok"
        pw.refresh_dispatch(st2, CFG, {"state": "ok"}, "t")
        self.assertEqual(st2["dispatch"], [])


class RunPurity(unittest.TestCase):
    def _collect(self, v):
        return lambda cfg, budget: ({K: {SIG: v}}, {K: []})

    def test_dry_run_leaves_the_state_dir_empty(self):
        with tempfile.TemporaryDirectory() as d:
            gh = FakeGh()
            lines = []
            pw.run(False, False, Path(d), CFG, collect=self._collect(400.0), gh=gh, host=lambda: {"state": "ok"}, out=lines.append)
            self.assertEqual(os.listdir(d), [])
            self.assertEqual(gh.calls, [])
            self.assertIn("PerfWatch digest", lines[0])

    def test_live_runs_reach_confirmation_in_exactly_three_breaches(self):
        with tempfile.TemporaryDirectory() as d:
            gh = FakeGh()
            t0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
            seq = [400.0] * 12 + [2000.0] * 3
            for i, v in enumerate(seq):
                pw.run(True, False, Path(d), CFG, collect=self._collect(v), gh=gh, host=lambda: {"state": "ok"},
                       now=t0 + dt.timedelta(hours=i))
                created = [c for c in gh.writes() if c[0][1].endswith("/o/r/issues") or c[0][1].endswith("shipnovo/issues")]
                self.assertEqual(len(created), 1 if i == len(seq) - 1 else 0, f"run {i}")
            st = json.loads((Path(d) / "state.json").read_text())
            self.assertEqual(st["dispatch"][0]["key"], K)
            self.assertTrue((Path(d) / "digest.md").exists())

    def test_min_interval_skips_a_second_live_run(self):
        with tempfile.TemporaryDirectory() as d:
            gh = FakeGh()
            t0 = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
            pw.run(True, False, Path(d), CFG, collect=self._collect(400.0), gh=gh, host=lambda: {"state": "ok"}, now=t0)
            pw.run(True, False, Path(d), CFG, collect=self._collect(400.0), gh=gh, host=lambda: {"state": "ok"},
                   now=t0 + dt.timedelta(minutes=10))
            self.assertEqual(json.loads((Path(d) / "state.json").read_text())["runs"], 1)


class Robustness(unittest.TestCase):
    def test_corrupt_state_is_backed_up_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "state.json").write_text("{broken", encoding="utf-8")
            st = pw.load_state(Path(d))
            self.assertEqual(st["runs"], 0)
            self.assertTrue(any(n.startswith("state.json.corrupt-") for n in os.listdir(d)))

    def test_lock_blocks_second_run_and_releases(self):
        with tempfile.TemporaryDirectory() as d:
            with pw.RunLock(Path(d)) as a:
                self.assertTrue(a.held)
                with pw.RunLock(Path(d)) as b:
                    self.assertFalse(b.held)
            with pw.RunLock(Path(d)) as c:
                self.assertTrue(c.held)

    def test_gh_timeout_keeps_event_pending(self):
        def boom(args, inp=None):
            raise pw.PerfError("gh timed out")
        st = confirmed_state(K)
        pub = pw.Publisher(CFG, boom)
        pub.publish(st)
        self.assertEqual(len(st["pending"]), 1)

    def test_find_issue_pages(self):
        pages = []

        def gh(args, inp=None):
            pages.append(args[1])
            if args[1].endswith("&page=1"):
                return json.dumps([{"number": i, "state": "open", "body": "x"} for i in range(100)])
            return json.dumps([{"number": 500, "state": "open", "html_url": "u", "body": pw.MARKER.format(key=K)}])
        hit = pw.Publisher(CFG, gh).find_issue("o/r", K)
        self.assertEqual(hit["number"], 500)
        self.assertEqual(len(pages), 2)


class ReplayTest(unittest.TestCase):
    def test_replay_refuses_production_head(self):
        with self.assertRaises(pw.Refused):
            pw.replay(CFG, "polysimulator/web-home", "https://app.staging.polysimulator.com", "https://polysimulator.com", 1)

    def test_replay_alternates_and_compares_same_path(self):
        seen = []

        class Op:
            pass

        def fake(url, opener, timeout=15):
            seen.append(url)
            return {"status": 200, "ttfb_ms": 100.0 if "head" in url else 200.0, "raw_len": 10, "body": b"", "headers": {}, "error": None}

        orig = pw.http_request
        pw.http_request = fake
        try:
            cfg = copy.deepcopy(CFG)
            text = pw.replay(cfg, "pinthread/embed-js", "https://pinthread.dev", "http://127.0.0.1:3000", 2, opener=Op(), sleep=lambda s: None)
        finally:
            pw.http_request = orig
        self.assertTrue(all(u.endswith("/embed.js") for u in seen))
        self.assertEqual(len(seen), 6)
        self.assertIn("| ttfb_p50_ms |", text)


if __name__ == "__main__":
    unittest.main()
