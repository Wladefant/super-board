"""Task 10 — compare before mutate, and quarantine anything that moved.

Pure stdlib `unittest`. No network, no `gh`.

The rule this file pins: a mutation is authorized by state reread **at decision
time**, never by state captured during preflight. Between preflight and apply a
human can move a card, a workflow can rename a field, and GitHub can hand out
new option IDs. Writing anyway silently overwrites whoever was right.

Any difference quarantines with exit 3 and zero writes.

Run directly:
  python -B tests/test_compare_before_mutate.py
Or through discovery:
  python -m unittest discover -s tests -p 'test_compare_before_mutate.py' -v
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from super_board_runtime import EXIT_CONFLICT, EXIT_OK  # noqa: E402
from super_board_runtime.project import (  # noqa: E402
    MAX_PROJECT_PAGES,
    CurrentState,
    ExpectedState,
    MutationConflict,
    apply_project_mutation,
    compare_project_mutation,
    snapshot_project,
)

ITEM = "PVTI_kwNOTAREALITEMID"
CONTENT = "I_kwNOTAREALCONTENTID"
FIELD = "PVTSSF_kwNOTAREALFIELDID"
OPTION = "47fc9ee4"


def _expected(**overrides) -> ExpectedState:
    fields = {
        "item_node_id": ITEM,
        "content_node_id": CONTENT,
        "field_id": FIELD,
        "field_name": "Status",
        "option_id": OPTION,
        "option_name": "Review",
        "status": "QA",
        "repository_head": "a" * 40,
        "evidence_revision": "rev-7",
        "project_values": {"Status": "QA", "Area": "runtime"},
        "updated_at": "2026-08-02T10:00:00Z",
    }
    fields.update(overrides)
    return ExpectedState(**fields)  # type: ignore[arg-type]


def _current(**overrides) -> CurrentState:
    base = _expected()
    fields = {f: getattr(base, f) for f in base.__dataclass_fields__}
    fields.update(overrides)
    return CurrentState(**fields)  # type: ignore[arg-type]


class RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, payload):
        self.calls.append(dict(payload))
        return {"itemId": payload.get("item_node_id")}


class CompareTests(unittest.TestCase):
    def test_an_unchanged_record_applies(self) -> None:
        decision = compare_project_mutation(_expected(), _current())
        self.assertEqual(decision.action, "apply")
        self.assertIsNone(decision.reason_code)
        self.assertEqual(decision.exit_code, EXIT_OK)

    def test_the_decision_carries_the_ids_reread_at_decision_time(self) -> None:
        # Not the preflight IDs: the ones the comparison actually saw.
        current = _current(field_id="PVTSSF_REREAD", option_id="deadbeef")
        decision = compare_project_mutation(
            _expected(field_id="PVTSSF_REREAD", option_id="deadbeef"), current
        )
        self.assertEqual(decision.action, "apply")
        self.assertEqual(decision.field_id, "PVTSSF_REREAD")
        self.assertEqual(decision.option_id, "deadbeef")

    def _assert_quarantine(self, current, reason) -> None:
        decision = compare_project_mutation(_expected(), current)
        self.assertEqual(decision.action, "quarantine")
        self.assertEqual(decision.reason_code, reason)
        self.assertEqual(decision.exit_code, EXIT_CONFLICT)

    def test_a_different_item_node_id_quarantines(self) -> None:
        self._assert_quarantine(_current(item_node_id="PVTI_SOMETHINGELSE"), "item-node-id-mismatch")

    def test_a_different_content_node_id_quarantines(self) -> None:
        self._assert_quarantine(
            _current(content_node_id="I_SOMETHINGELSE"), "content-node-id-mismatch"
        )

    def test_a_missing_item_quarantines(self) -> None:
        self._assert_quarantine(_current(item_node_id=None), "item-unreadable")

    def test_a_changed_field_id_quarantines(self) -> None:
        # The field was renamed or recreated: the preflight ID is now a lie.
        self._assert_quarantine(_current(field_id="PVTSSF_NEWID"), "field-id-changed")

    def test_a_changed_option_id_quarantines(self) -> None:
        self._assert_quarantine(_current(option_id="ffffffff"), "option-id-changed")

    def test_a_changed_field_name_quarantines(self) -> None:
        self._assert_quarantine(_current(field_name="State"), "field-name-changed")

    def test_a_changed_repository_state_quarantines(self) -> None:
        self._assert_quarantine(_current(repository_head="b" * 40), "repository-state-changed")

    def test_a_changed_evidence_revision_quarantines(self) -> None:
        self._assert_quarantine(_current(evidence_revision="rev-8"), "evidence-revision-changed")

    def test_changed_project_values_quarantine(self) -> None:
        self._assert_quarantine(
            _current(project_values={"Status": "Blocked", "Area": "runtime"}),
            "project-values-changed",
        )

    def test_a_newer_record_is_never_overwritten(self) -> None:
        # A human or another automation touched the card after the manifest was
        # built. Their decision wins; ours quarantines.
        self._assert_quarantine(
            _current(updated_at="2026-08-02T11:30:00Z"), "record-changed-since-manifest"
        )

    def test_an_older_timestamp_also_quarantines_rather_than_guessing(self) -> None:
        self._assert_quarantine(
            _current(updated_at="2026-08-02T09:00:00Z"), "record-changed-since-manifest"
        )


class PreflightAuthorityTests(unittest.TestCase):
    """Preflight IDs are never mutation authority."""

    def test_a_field_id_that_changed_between_preflight_and_apply_quarantines(self) -> None:
        preflight = _expected()
        # Between preflight and apply, the Status field is recreated.
        reread = _current(field_id="PVTSSF_RECREATED", option_id="00000001")
        decision = compare_project_mutation(preflight, reread)
        self.assertEqual(decision.action, "quarantine")
        self.assertEqual(decision.reason_code, "field-id-changed")

        writer = RecordingWriter()
        with self.assertRaises(MutationConflict) as ctx:
            apply_project_mutation(decision, writer=writer, readback=lambda _: reread)
        self.assertEqual(ctx.exception.exit_code, EXIT_CONFLICT)
        self.assertEqual(writer.calls, [], "a quarantined decision must issue zero writes")


class ApplyTests(unittest.TestCase):
    def test_apply_writes_once_then_reads_back(self) -> None:
        writer = RecordingWriter()
        reads: list[str] = []

        def readback(decision):
            reads.append(decision.item_node_id)
            return _current(status="Review", project_values={"Status": "Review", "Area": "runtime"})

        decision = compare_project_mutation(
            _expected(), _current(), desired_status="Review"
        )
        result = apply_project_mutation(decision, writer=writer, readback=readback)
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(writer.calls[0]["field_id"], FIELD)
        self.assertEqual(writer.calls[0]["option_id"], OPTION)
        self.assertEqual(reads, [ITEM])
        self.assertTrue(result["applied"])

    def test_a_readback_that_disagrees_is_a_conflict(self) -> None:
        writer = RecordingWriter()
        decision = compare_project_mutation(_expected(), _current(), desired_status="Review")
        with self.assertRaises(MutationConflict) as ctx:
            apply_project_mutation(
                decision, writer=writer, readback=lambda _: _current(status="Blocked")
            )
        self.assertEqual(ctx.exception.reason, "readback-mismatch")
        self.assertEqual(ctx.exception.exit_code, EXIT_CONFLICT)

    def test_dry_run_issues_zero_writes(self) -> None:
        writer = RecordingWriter()
        decision = compare_project_mutation(_expected(), _current(), desired_status="Review")
        result = apply_project_mutation(
            decision, writer=writer, readback=lambda _: _current(status="Review"), dry_run=True
        )
        self.assertFalse(result["applied"])
        self.assertEqual(writer.calls, [])


class SnapshotTests(unittest.TestCase):
    def _page(self, nodes, cursor=None, has_next=False):
        return {
            "items": nodes,
            "pageInfo": {"endCursor": cursor, "hasNextPage": has_next},
            "fields": {"Status": {"id": FIELD, "options": {"Review": OPTION}}},
        }

    def test_pagination_walks_every_page(self) -> None:
        pages = [
            self._page([{"id": "PVTI_1"}], "c1", True),
            self._page([{"id": "PVTI_2"}], "c2", True),
            self._page([{"id": "PVTI_3"}], None, False),
        ]
        seen: list[str | None] = []

        def fetch(after):
            seen.append(after)
            return pages[len(seen) - 1]

        snapshot = snapshot_project("Bavariance", 1, fetch=fetch)
        self.assertEqual([i["id"] for i in snapshot.items], ["PVTI_1", "PVTI_2", "PVTI_3"])
        self.assertEqual(seen, [None, "c1", "c2"])
        self.assertFalse(snapshot.hit_cap)
        self.assertEqual(snapshot.fields["Status"]["id"], FIELD)

    def test_pagination_is_capped(self) -> None:
        snapshot = snapshot_project(
            "Bavariance", 1, fetch=lambda after: self._page([{"id": "PVTI_x"}], "forever", True)
        )
        self.assertEqual(len(snapshot.items), MAX_PROJECT_PAGES)
        self.assertTrue(snapshot.hit_cap)

    def test_a_missing_cursor_fails_closed_rather_than_refetching_page_one(self) -> None:
        # `hasNextPage: true` with no cursor is a board we cannot finish reading.
        # Stopping there and returning what we had marked a partial snapshot
        # complete — a board missing 200 cards that reconciliation would then
        # "fix". `after=None` would refetch page one forever, so neither
        # continuing nor stopping quietly is available: the walk has to refuse.
        with self.assertRaises(MutationConflict) as ctx:
            snapshot_project(
                "Bavariance", 1, fetch=lambda after: self._page([{"id": "PVTI_1"}], None, True)
            )
        self.assertEqual(ctx.exception.reason, "project-snapshot-incomplete")

    def test_an_empty_cursor_string_is_also_refused(self) -> None:
        with self.assertRaises(MutationConflict) as ctx:
            snapshot_project(
                "Bavariance", 1, fetch=lambda after: self._page([{"id": "PVTI_1"}], "", True)
            )
        self.assertEqual(ctx.exception.reason, "project-snapshot-incomplete")

    def test_the_last_page_still_ends_the_walk_cleanly(self) -> None:
        snapshot = snapshot_project(
            "Bavariance", 1, fetch=lambda after: self._page([{"id": "PVTI_1"}], None, False)
        )
        self.assertEqual(len(snapshot.items), 1)
        self.assertFalse(snapshot.hit_cap)

    def test_a_failed_page_is_never_a_partial_snapshot(self) -> None:
        def fetch(after):
            if after is None:
                return self._page([{"id": "PVTI_1"}], "c1", True)
            raise RuntimeError("GraphQL 502")

        with self.assertRaises(MutationConflict) as ctx:
            snapshot_project("Bavariance", 1, fetch=fetch)
        self.assertEqual(ctx.exception.reason, "project-snapshot-incomplete")


if __name__ == "__main__":
    unittest.main(verbosity=2)
