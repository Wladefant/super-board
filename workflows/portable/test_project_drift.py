#!/usr/bin/env python3
"""test_project_drift.py - detection classes, repair rules, bounds, quota halt, idempotence."""

import os
import sys
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import project_drift as pd

REPO = "o/r"


def it(n, state="open", is_pr=False, labels=(), milestone="M1", merged=False):
    return {"number": n, "node_id": f"N{n}", "state": state, "is_pr": is_pr, "merged": merged,
            "labels": list(labels), "milestone": milestone}


def entry(n, status="Backlog", kind=None, area=None, risk=None):
    return {"item_id": f"I{n}", "type": "Issue", "state": "OPEN", "Status": status, "Kind": kind, "Area": area, "Risk": risk}


def codes(findings):
    return sorted((f["code"], f["number"]) for f in findings)


class Detect(unittest.TestCase):
    def test_open_missing_is_added_with_status_and_label_fields(self):
        f = pd.detect(REPO, [it(1, labels=["kind:bug", "area:ui", "risk:high"])], {})
        self.assertEqual(codes(f), [("missing-from-project", 1)])
        self.assertEqual(f[0]["repair"], ("add", [("Status", "Backlog"), ("Area", "UI"), ("Kind", "Bug"), ("Risk", "High")]))

    def test_open_pr_missing_gets_review_status(self):
        f = pd.detect(REPO, [it(2, is_pr=True, labels=["kind:task"])], {})
        self.assertEqual(f[0]["repair"][1][0], ("Status", "Review"))

    def test_closed_missing_is_history_not_repaired(self):
        f = pd.detect(REPO, [it(3, state="closed")], {})
        self.assertEqual(codes(f), [("closed-not-on-board", 3)])
        self.assertIsNone(f[0]["repair"])

    def test_closed_issue_not_done_repaired_but_merged_pr_is_report_only(self):
        board = {(REPO, 4): entry(4, "Building"), (REPO, 5): entry(5, "QA")}
        f = pd.detect(REPO, [it(4, state="closed", labels=["kind:task"]), it(5, state="closed", is_pr=True, merged=True)], board)
        self.assertEqual(codes(f), [("closed-not-done", 4), ("field-unset", 4), ("merged-not-done", 5)])
        by = {x["number"]: x for x in f if x["code"] != "field-unset"}
        self.assertEqual(by[4]["repair"], [("Status", "Done")])
        self.assertIsNone(by[5]["repair"])  # policy: merges never auto-Done

    def test_open_but_done_and_status_unset(self):
        board = {(REPO, 6): entry(6, "Done"), (REPO, 7): entry(7, None)}
        f = pd.detect(REPO, [it(6), it(7, is_pr=True)], board)
        by = {x["number"]: x for x in f if x["code"] != "no-kind-label"}
        self.assertEqual(by[6]["code"], "open-but-done")
        self.assertEqual(by[6]["repair"], [("Status", "Backlog")])
        self.assertEqual((by[7]["code"], by[7]["repair"]), ("status-unset", [("Status", "Review")]))

    def test_field_unset_repaired_mismatch_reported_multi_label_ok(self):
        board = {
            (REPO, 8): entry(8, kind=None, area="Infra"),
            (REPO, 9): entry(9, kind="Bug", area="Sync"),
            (REPO, 10): entry(10, kind="Bug", area="Infra"),
        }
        items = [it(8, labels=["kind:bug", "area:infra"]), it(9, labels=["kind:bug", "area:ui"]),
                 it(10, labels=["kind:bug", "area:ui", "area:infra"])]
        f = pd.detect(REPO, items, board)
        self.assertEqual(codes(f), [("field-mismatch", 9), ("field-unset", 8)])
        self.assertEqual([x for x in f if x["number"] == 8][0]["repair"], [("Kind", "Bug")])

    def test_report_only_hygiene(self):
        board = {(REPO, 11): entry(11, kind="Task")}
        f = pd.detect(REPO, [it(11, labels=["area:ui"], milestone=None)], board)
        self.assertIn(("no-kind-label", 11), codes(f))
        self.assertIn(("no-milestone", 11), codes(f))

    def test_risk_prefers_worst_label(self):
        self.assertEqual(pd.label_fields(["risk:low", "risk:money-path"])["Risk"][0], "Money Path")

    def test_clean_board_has_no_findings(self):
        board = {(REPO, 12): entry(12, kind="Bug", area="UI", risk="High")}
        self.assertEqual(pd.detect(REPO, [it(12, labels=["kind:bug", "area:ui", "risk:high"])], board), [])


SCHEMA = {"project_id": "P", "fields": {n: {"id": f"F{n}", "options": {v: f"O{v}" for v in
          ["Backlog", "Review", "Done", "Bug", "UI", "High", "Workflow", "Feature"]}} for n in pd.SELECT_FIELDS}}


class FakeGql:
    def __init__(self):
        self.calls = []
        self.state = {}

    def __call__(self, query, variables):
        self.calls.append(query.split("(")[0].split()[-1] if "mutation" in query else "query")
        if "addProjectV2ItemById" in query:
            return {"data": {"addProjectV2ItemById": {"item": {"id": "NEW" + variables["content"]}}}}
        if "updateProjectV2ItemFieldValue" in query:
            fld = next(n for n in pd.SELECT_FIELDS if SCHEMA["fields"][n]["id"] == variables["field"])
            self.state.setdefault(variables["item"], {})[fld.lower()] = {"name": variables["option"][1:]}
            return {"data": {}}
        return {"data": {"node": self.state.get(variables["item"], {})}}

    def mutations(self):
        return len([c for c in self.calls if c != "query"])


class Apply(unittest.TestCase):
    def test_add_and_sets_are_one_unit_and_read_back(self):
        items = [it(1, labels=["kind:bug", "area:ui"])]
        findings = pd.detect(REPO, items, {})
        g = FakeGql()
        res = pd.apply_repairs(findings, {(REPO, 1): items[0]}, {}, SCHEMA, g, 50)
        self.assertEqual(res["repaired"], [f"{REPO}#1"])
        self.assertEqual(res["writes"], 4)  # add + Status + Area + Kind
        self.assertEqual(res["failed"], [])

    def test_cap_defers_whole_items_never_half_applies(self):
        items = [it(n, labels=["kind:bug", "area:ui"]) for n in (1, 2, 3)]
        findings = pd.detect(REPO, items, {})
        g = FakeGql()
        res = pd.apply_repairs(findings, {(REPO, i["number"]): i for i in items}, {}, SCHEMA, g, 9)
        self.assertEqual(len(res["repaired"]), 2)
        self.assertEqual(res["deferred_items"], 1)
        self.assertLessEqual(res["writes"], 9)

    def test_readback_mismatch_is_reported_failed(self):
        items = [it(1, labels=["kind:bug"])]
        findings = pd.detect(REPO, items, {})
        g = FakeGql()
        orig = g.__call__

        class Lying(FakeGql):
            def __call__(self, query, variables):
                if "ProjectV2Item {" in query and "node(id" in query:
                    return {"data": {"node": {}}}
                return orig(query, variables)

        res = pd.apply_repairs(findings, {(REPO, 1): items[0]}, {}, SCHEMA, Lying(), 50)
        self.assertEqual(res["repaired"], [])
        self.assertEqual(len(res["failed"]), 1)


def fake_gql_factory(board_nodes):
    def gql(query, variables):
        if "fields(first: 50)" in query:
            return {"data": {"repositoryOwner": {"projectV2": {"id": "P", "fields": {"nodes": [
                {"id": f"F{n}", "name": n, "options": [{"id": f"O{v}", "name": v} for v in ["Backlog", "Review", "Done", "Bug", "UI"]]}
                for n in pd.SELECT_FIELDS]}}}}}
        if "items(first: 100" in query:
            return {"data": {"repositoryOwner": {"projectV2": {"items": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": board_nodes}}}}}
        raise AssertionError("unexpected query in dry-run: " + query[:40])
    return gql


class Sweep(unittest.TestCase):
    def test_dry_run_makes_zero_mutations_and_reports_counts(self):
        rep = pd.run_sweep([REPO, "x/y"], [REPO], "own", 5, False, 10, gql=fake_gql_factory([]),
                           lister=lambda r: [it(1, labels=["kind:bug"])] if r == REPO else [it(2)], quota=lambda: 5000)
        self.assertEqual(rep["applied"], {"writes": 0})
        self.assertEqual(rep["findings"]["missing-from-project"], 2)
        self.assertEqual(rep["repairable"], 1)  # only the repair repo is repairable
        self.assertEqual(rep["by_repo"]["x/y"]["missing-from-project"], 1)

    def test_quota_near_reserve_halts(self):
        with self.assertRaises(pd.QuotaError):
            pd.run_sweep([REPO], [REPO], "own", 5, True, 10, gql=fake_gql_factory([]), lister=lambda r: [], quota=lambda: 1100)

    def test_second_live_run_is_idempotent(self):
        node = {"id": "I1", "content": {"__typename": "Issue", "number": 1, "state": "OPEN", "repository": {"nameWithOwner": REPO}},
                "status": {"name": "Backlog"}, "kind": {"name": "Bug"}, "area": None, "risk": None}
        rep = pd.run_sweep([REPO], [REPO], "own", 5, True, 10, gql=fake_gql_factory([node]),
                           lister=lambda r: [it(1, labels=["kind:bug"])], quota=lambda: 5000)
        self.assertEqual(rep["repairable"], 0)
        self.assertEqual(rep["applied"]["writes"], 0)


if __name__ == "__main__":
    unittest.main()
