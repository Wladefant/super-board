#!/usr/bin/env python3
"""test_hosting_audit.py - schema, every drift rule with its negative control, issue-edit dedupe, hidden task."""

import copy
import json
import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import hosting_audit as h
import install_hosting_audit_task as t


def comp(name="web", host="dokploy-app", url="https://a.example.de", did="APP1", cost=3.0, kind="web"):
    return {"name": name, "kind": kind, "host": host, "url": url, "dokploy_app_id": did, "monthly_cost_usd": cost}


def man(project="demo", comps=None, sb=None, exempt=None, step=None):
    m = {
        "schema": 1, "project": project, "stage": "live", "dns": {"provider": "cloudflare"},
        "components": comps if comps is not None else [comp()],
        "supabase": sb or {"in_use": False}, "next_migration_step": step, "exempt": exempt,
    }
    m["_source"] = "test"
    return m


def dk(id_="APP1", name="web", status="done", kind="application", project="demo"):
    return {"id": id_, "name": name, "kind": kind, "status": status, "project": project, "env": "production", "hosts": []}


def rules(findings):
    return sorted(f["rule"] for f in findings)


class Schema(unittest.TestCase):
    def test_valid_manifest_has_no_errors(self):
        self.assertEqual(h.validate_manifest(man()), [])

    def test_each_required_field_is_enforced(self):
        for mutate, needle in (
            (lambda m: m.update(schema=2), "schema"),
            (lambda m: m.update(project=""), "project"),
            (lambda m: m.update(stage="beta"), "stage"),
            (lambda m: m.update(dns={"provider": "route53"}), "dns.provider"),
            (lambda m: m["components"][0].update(host="vercel"), "host"),
            (lambda m: m["components"][0].update(kind="blob"), "kind"),
            (lambda m: m["components"][0].update(url="ftp://x"), "url"),
            (lambda m: m["components"][0].update(monthly_cost_usd=-1), "monthly_cost_usd"),
            (lambda m: m["components"][0].update(dokploy_app_id=None), "needs dokploy_app_id"),
            (lambda m: m.update(supabase={"in_use": True}), "used_for"),
            (lambda m: m.update(exempt={"reason": " "}), "exempt"),
        ):
            m = copy.deepcopy(man())
            mutate(m)
            errs = h.validate_manifest(m)
            self.assertTrue(any(needle in e for e in errs), (needle, errs))

    def test_duplicate_component_names_rejected(self):
        errs = h.validate_manifest(man(comps=[comp(), comp(did="APP2")]))
        self.assertTrue(any("duplicated" in e for e in errs))

    def test_bool_is_not_a_cost(self):
        errs = h.validate_manifest(man(comps=[comp(cost=True)]))
        self.assertTrue(any("monthly_cost_usd" in e for e in errs))

    def test_parse_reports_source_on_bad_json(self):
        m, errs = h.parse_manifest("{nope", "github:x/y")
        self.assertIsNone(m)
        self.assertTrue(errs[0].startswith("github:x/y"))


class Drift(unittest.TestCase):
    def test_clean_state_has_no_findings(self):
        f = h.find_drift([man()], [dk()], {"https://a.example.de": 200}, None)
        self.assertEqual(f, [])

    def test_live_app_in_no_manifest(self):
        f = h.find_drift([man()], [dk(), dk("APP9", "stray")], {}, None)
        self.assertEqual(rules(f), [h.R_UNMANIFESTED])
        self.assertEqual(f[0]["subject"], "stray")

    def test_stopped_app_in_no_manifest_is_not_drift(self):
        self.assertEqual(h.find_drift([man()], [dk(), dk("APP9", "stray", status="idle")], {}, None), [])

    def test_protected_production_item_is_not_audited(self):
        self.assertEqual(h.find_drift([man()], [dk(), dk("P1", "Production", kind="compose", project="PolySimulator")], {}, None), [])

    def test_url_down_on_no_answer_and_5xx_but_not_on_404(self):
        urls = {"https://a.example.de": None}
        self.assertEqual(rules(h.find_drift([man()], [dk()], urls, None)), [h.R_URL_DOWN])
        urls = {"https://a.example.de": 502}
        self.assertEqual(rules(h.find_drift([man()], [dk()], urls, None)), [h.R_URL_DOWN])
        for ok in (200, 301, 401, 404):
            self.assertEqual(h.find_drift([man()], [dk()], {"https://a.example.de": ok}, None), [])

    def test_protected_url_is_never_reported_down(self):
        m = man(comps=[comp(url="https://polysimulator.com", did="APP1")])
        self.assertEqual(h.find_drift([m], [dk()], {"https://polysimulator.com": None}, None), [])
        self.assertEqual(h.probe_urls(["https://polysimulator.com", "https://www.polysimulator.com"], opener=self._boom), {})

    @staticmethod
    def _boom(*a, **k):
        raise AssertionError("a protected host was probed")

    def test_staging_hosts_are_probed_but_production_hosts_are_not(self):
        self.assertFalse(h.is_protected_host("https://staging.polysimulator.com"))
        self.assertFalse(h.is_protected_host("https://app.staging.polysimulator.com"))
        for u in ("https://polysimulator.com", "https://app.polysimulator.com", "https://api.polysimulator.com"):
            self.assertTrue(h.is_protected_host(u), u)

    def test_supabase_flagged_by_flag_or_component(self):
        m = man(sb={"in_use": True, "used_for": ["auth", "db"]})
        f = h.find_drift([m], [dk()], {}, None)
        self.assertEqual(rules(f), [h.R_SUPABASE])
        self.assertIn("auth, db", f[0]["detail"])
        m = man(comps=[comp(), comp("db", "supabase", None, None, None, "db")])
        self.assertEqual(rules(h.find_drift([m], [dk()], {}, None)), [h.R_SUPABASE])

    def test_no_home_needs_a_reason(self):
        m = man(comps=[comp("site", "other", None, None, None, "static")])
        self.assertEqual(rules(h.find_drift([m], None, {}, None)), [h.R_NO_HOME])
        m = man(comps=[comp("site", "other", None, None, None, "static")], exempt={"reason": "Customer hosts it."})
        self.assertEqual(h.find_drift([m], None, {}, None), [])

    def test_cloudflare_home_counts(self):
        m = man(comps=[comp("site", "cloudflare-pages", "https://s.example.dev", None, 0, "static")])
        self.assertEqual(h.find_drift([m], None, {"https://s.example.dev": 200}, [{"host": "cloudflare-pages", "name": "site"}]), [])

    def test_stale_dokploy_id(self):
        f = h.find_drift([man()], [dk("OTHER", "other", status="idle")], {}, None)
        self.assertEqual(rules(f), [h.R_STALE_ID])

    def test_cloudflare_resource_in_no_manifest(self):
        f = h.find_drift([man()], [dk()], {}, [{"host": "cloudflare-r2", "name": "stray-bucket"}])
        self.assertEqual(rules(f), [h.R_CF_UNMANIFESTED])

    def test_unreachable_sources_produce_no_false_drift(self):
        self.assertEqual(h.find_drift([man()], None, {}, None), [])

    def test_missing_and_invalid_manifests(self):
        f = h.find_drift([man()], [dk()], {}, None, missing_repos=["Wladefant/x"], invalid=["github:Wladefant/y: stage must be one of"])
        self.assertEqual(rules(f), [h.R_INVALID, h.R_MISSING])


class Sources(unittest.TestCase):
    def test_dokploy_inventory_keeps_only_safe_fields(self):
        secret = "DATABASE_URL=postgres://u:hunter2@db/x"

        def fetch(proc, params=None):
            if proc == "project.all":
                return [{"name": "P", "environments": [{"name": "production", "applications": [
                    {"applicationId": "A", "name": "app", "applicationStatus": "done", "env": secret}],
                    "compose": [{"composeId": "C", "name": "stack", "composeStatus": "idle", "env": secret}]}]}]
            return [{"host": "app.example.de", "env": secret}]

        items = h.dokploy_inventory("k", fetch=fetch)
        self.assertEqual([(i["id"], i["status"], i["hosts"]) for i in items],
                         [("A", "done", ["app.example.de"]), ("C", "idle", [])])
        self.assertNotIn("hunter2", json.dumps(items))

    def test_cloudflare_inventory_reads_four_resource_kinds(self):
        data = {
            "/accounts": {"result": [{"id": "acc"}]},
            "/accounts/acc/pages/projects": {"result": [{"name": "site"}]},
            "/accounts/acc/workers/scripts": {"result": [{"id": "worker1"}]},
            "/accounts/acc/r2/buckets": {"result": {"buckets": [{"name": "bkt"}]}},
            "/accounts/acc/d1/database": {"result": [{"name": "db1"}]},
        }
        inv = h.cloudflare_inventory("t", fetch=lambda p: data[p])
        self.assertEqual(sorted((r["host"], r["name"]) for r in inv), [
            ("cloudflare-d1", "db1"), ("cloudflare-pages", "site"), ("cloudflare-r2", "bkt"), ("cloudflare-workers", "worker1")])


class IssueEdit(unittest.TestCase):
    def block(self, ts="2026-10-05 06:15 UTC", findings=()):
        return h.render_block([man()], list(findings), {"https://a.example.de": 200}, [], ts).replace("Last audit: 2026-10-05 06:15 UTC", f"Last audit: {ts}")

    def test_same_result_with_new_timestamp_makes_no_edit(self):
        body = h.splice_block("Intro text.", self.block("2026-10-05 06:15 UTC"))
        self.assertIsNone(h.plan_issue_edit(body, self.block("2026-10-06 06:15 UTC")))

    def test_changed_result_replaces_only_the_block(self):
        body = "Intro text.\n\n" + self.block() + "\n\nOutro text."
        f = [{"rule": h.R_SUPABASE, "project": "demo", "subject": "supabase", "detail": "still in use for auth"}]
        new = h.plan_issue_edit(body, self.block(findings=f))
        self.assertIn("supabase-in-use", new)
        self.assertTrue(new.startswith("Intro text."))
        self.assertTrue(new.rstrip().endswith("Outro text."))
        self.assertEqual(new.count(h.BLOCK_START), 1)

    def test_body_without_markers_gets_the_block_appended(self):
        new = h.plan_issue_edit("Plain body.", self.block())
        self.assertTrue(new.startswith("Plain body."))
        self.assertIn(h.BLOCK_END, new)

    def test_block_lists_components_cost_and_next_step(self):
        text = h.render_block([man(step="Move DB.", comps=[comp(cost=None)])], [], {}, [], "now")
        self.assertIn("1 component(s) with no cost yet", text)
        self.assertIn("Move DB.", text)


class HiddenTask(unittest.TestCase):
    def test_task_starts_pythonw_daily_with_live_flag(self):
        argv = t.schtasks_argv(r"C:\x\hosting_audit.py")
        self.assertEqual(argv[:2], ["schtasks", "/create"])
        self.assertIn("daily", argv)
        tr = argv[argv.index("/tr") + 1]
        self.assertIn("pythonw.exe", tr)
        self.assertTrue(tr.endswith("--live"))

    def test_console_python_is_refused(self):
        with self.assertRaises(ValueError):
            t.schtasks_argv(r"C:\x\hosting_audit.py", pythonw=r"C:\py\python.exe")


if __name__ == "__main__":
    unittest.main()
