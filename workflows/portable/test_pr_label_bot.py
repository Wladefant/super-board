#!/usr/bin/env python3
"""test_pr_label_bot.py - size boundaries, path risk, ownership, idempotence, bounds."""

import json
import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import pr_label_bot as bot


def pr(number, files, labels=()):
    return {"number": number, "labels": [{"name": l} for l in labels], "files": files,
            "additions": sum(f["additions"] for f in files), "deletions": sum(f["deletions"] for f in files)}


def f(path, a=1, d=0):
    return {"path": path, "additions": a, "deletions": d}


class Sizes(unittest.TestCase):
    def test_boundaries(self):
        cases = {0: "size:XS", 9: "size:XS", 10: "size:S", 29: "size:S", 30: "size:M", 99: "size:M",
                 100: "size:L", 249: "size:L", 250: "size:XL", 499: "size:XL", 500: "size:XXL"}
        for lines, want in cases.items():
            self.assertEqual(bot.size_label(lines), want, lines)

    def test_lockfiles_and_generated_excluded(self):
        p = pr(1, [f("src/a.py", 5, 0), f("package-lock.json", 900, 100), f("api/x.generated.ts", 400, 0)])
        self.assertEqual(bot.effective_lines(p), 5)
        self.assertEqual(bot.plan_pr(p)["size"], "size:XS")

    def test_capped_file_list_falls_back_to_totals(self):
        files = [f(f"f{i}.py", 5, 0) for i in range(100)]
        p = pr(2, files)
        self.assertEqual(bot.plan_pr(p)["size"], "size:XXL")  # 500 lines


class Plans(unittest.TestCase):
    def test_stale_size_swapped_and_idempotent(self):
        p = pr(3, [f("a.py", 40, 0)], labels=["size:XS", "kind:bug"])
        plan = bot.plan_pr(p)
        self.assertEqual((plan["add"], plan["remove"]), (["size:M"], ["size:XS"]))
        done = pr(3, [f("a.py", 40, 0)], labels=["size:M", "kind:bug"])
        plan2 = bot.plan_pr(done)
        self.assertEqual((plan2["add"], plan2["remove"]), ([], []))

    def test_size_exempt_is_respected(self):
        plan = bot.plan_pr(pr(4, [f("a.py", 900, 0)], labels=["size:exempt"]))
        self.assertEqual((plan["size"], plan["add"], plan["remove"]), (None, [], []))

    def test_risk_labels_add_only(self):
        p = pr(5, [f("alembic/versions/1.py"), f("backend/billing/x.py"), f("auth/token.py")], labels=["risk:low", "size:XS"])
        plan = bot.plan_pr(p)
        self.assertEqual(sorted(plan["add"]), ["risk:high", "risk:migration", "risk:money-path"])
        self.assertNotIn("risk:low", plan["remove"])

    def test_ordinary_paths_get_no_risk(self):
        self.assertEqual(bot.risk_labels(pr(6, [f("docs/readme.md"), f("src/ui/button.tsx")])), [])


class Sweep(unittest.TestCase):
    def fake(self, prs, existing_labels, log):
        def runner(cmd, timeout):
            log.append(cmd)
            if cmd[:3] == ["gh", "pr", "list"]:
                return json.dumps(prs)
            if cmd[:3] == ["gh", "label", "list"]:
                return json.dumps([{"name": n} for n in existing_labels])
            if cmd[:3] == ["gh", "pr", "view"]:
                n = int(cmd[3])
                plan = next(p for p in prs if p["number"] == n)
                return json.dumps({"labels": [{"name": "size:M"}]})
            return ""
        return runner

    def test_dry_run_writes_nothing(self):
        log = []
        rep = bot.run_sweep("o/r", False, 5, runner=self.fake([pr(1, [f("a.py", 40)])], [], log))
        self.assertEqual(len(rep["planned"]), 1)
        self.assertFalse(any(c[:3] in (["gh", "pr", "edit"], ["gh", "label", "create"]) for c in log))

    def test_live_is_bounded_and_reads_back(self):
        log = []
        prs = [pr(i, [f("a.py", 40)]) for i in range(1, 6)]
        rep = bot.run_sweep("o/r", True, 2, runner=self.fake(prs, ["size:M"], log))
        self.assertEqual(len([c for c in log if c[:3] == ["gh", "pr", "edit"]]), 2)
        self.assertEqual(rep["deferred_over_cap"], 3)
        self.assertEqual(rep["applied"], [1, 2])

    def test_readback_failure_is_reported(self):
        log = []
        rep = bot.run_sweep("o/r", True, 5, runner=self.fake([pr(1, [f("a.py", 400)])], ["size:XL"], log))
        self.assertEqual(rep["readback_failed"], [1])  # fake readback returns size:M, not XL

    def test_missing_size_label_is_created_live_only(self):
        log = []
        bot.run_sweep("o/r", True, 5, runner=self.fake([pr(1, [f("a.py", 40)])], [], log))
        self.assertTrue(any(c[:3] == ["gh", "label", "create"] and "size:M" in c for c in log))


if __name__ == "__main__":
    unittest.main()
