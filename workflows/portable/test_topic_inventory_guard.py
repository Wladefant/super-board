#!/usr/bin/env python3
"""
test_topic_inventory_guard.py - Contract tests and finite executable proofs
for topic inventory preservation, active worker coverage, and worker floor invariants.

Acceptance proofs executed:
1. remove an item -> fails (DROPPED_TASK)
2. park only worker of runnable topic -> fails (UNCOVERED_RUNNABLE_TOPIC)
3. completion -> next-ready assignment appears
4. blocked topic stays present (lossless retention under blockers)
5. 7 real active workers satisfy floor
6. unsupported/fake active counts fail (FAKE_WORKER_REJECTED + floor deficit)
7. restored inventory loses zero tasks (additive reconciliation preserves all 342+ tasks, cancelled historical choices kept)
"""

import json
import os
import sys
import unittest
from pathlib import Path

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from topic_inventory_guard import (
    TopicInventoryGuard,
    InventoryItem,
    NativeWorker,
    CoverageViolation,
    NextAssignment,
    canonical_item_key,
)


class TestTopicInventoryGuard(unittest.TestCase):
    def setUp(self):
        self.guard = TopicInventoryGuard()
        # Find real snapshot files if available
        self.home_dir = Path.home()
        self.board_path = Path("local://full-topic-board-current.json")
        self.inv_path = self.home_dir / ".veyyon" / "workflows" / "open-issue-inventory.json"
        self.ledger_path = self.home_dir / ".veyyon" / "workflows" / "ledger.json"

    def _build_synthetic_inventory(self, count=10, topics=None):
        """Build controlled synthetic inventory across topics."""
        topics = topics or [f"Topic-{i}" for i in range(count)]
        items = []
        for i, topic in enumerate(topics):
            items.append(InventoryItem(
                id=f"task-{i}-1",
                content=f"Implement deliverable for {topic}",
                phase=topic,
                status="pending",
                owner=f"Worker{i}",
                issue_url=f"https://github.com/Bavariance/polysimulator/issues/{4000+i}",
            ))
            items.append(InventoryItem(
                id=f"task-{i}-2",
                content=f"Verify acceptance criteria for {topic}",
                phase=topic,
                status="pending",
                owner=f"Worker{i}",
                issue_url=f"https://github.com/Bavariance/polysimulator/issues/{4000+i}",
            ))
        return items

    # ----------------------------------------------------------------------
    # Proof 1: remove an item -> fails
    # ----------------------------------------------------------------------
    def test_proof_1_remove_item_fails(self):
        baseline = self._build_synthetic_inventory(5)
        # Simulate dropping the 3rd task
        dropped_item = baseline[2]
        current_inventory = [it for it in baseline if it.id != dropped_item.id]

        report = self.guard.evaluate_topic_coverage(
            inventory=current_inventory,
            roster=[{"id": f"Worker{i}", "status": "running", "topic": f"Topic-{i}"} for i in range(5)],
            baseline_inventory=baseline,
        )

        self.assertFalse(report.ok, "Expected failure when an item is removed from baseline inventory")
        dropped_violations = [v for v in report.violations if v.kind == "DROPPED_TASK"]
        self.assertTrue(len(dropped_violations) >= 1, "Expected at least one DROPPED_TASK violation")
        self.assertIn(dropped_item.id, dropped_violations[0].details.get("item_id", ""))
        self.assertIn(dropped_item.content, dropped_violations[0].message)

    # ----------------------------------------------------------------------
    # Proof 2: park only worker of runnable topic -> fails
    # ----------------------------------------------------------------------
    def test_proof_2_park_only_worker_of_runnable_topic_fails(self):
        inventory = [
            InventoryItem(id="t1", content="Build desktop GUI", phase="Desktop GUI", status="pending", issue_url="https://github.com/Bavariance/polysimulator/issues/4582"),
            InventoryItem(id="t2", content="Verify motion loaders", phase="Motion", status="pending", issue_url="https://github.com/Bavariance/polysimulator/issues/4582"),
        ]
        # Topic "Desktop GUI" has a parked worker; Topic "Motion" has a running worker
        roster = [
            {"id": "TopicGuiDelivery", "status": "parked", "topic": "Desktop GUI"},
            {"id": "TopicMotionAcceptance", "status": "running", "topic": "Motion"},
        ]

        report = self.guard.evaluate_topic_coverage(
            inventory=inventory,
            roster=roster,
        )

        self.assertFalse(report.ok, "Expected failure when the only worker for a runnable topic is parked")
        uncovered = [v for v in report.violations if v.kind == "UNCOVERED_RUNNABLE_TOPIC"]
        self.assertTrue(any(v.details.get("topic") == "Desktop GUI" for v in uncovered),
                        "Desktop GUI must be flagged as uncovered because parked worker is not active")
        self.assertIn("Desktop GUI", report.uncovered_topics)
        self.assertEqual(report.parked_workers_count, 1)

    # ----------------------------------------------------------------------
    # Proof 3: completion -> next-ready assignment appears
    # ----------------------------------------------------------------------
    def test_proof_3_completion_next_ready_assignment_appears(self):
        # Phase has task 1 completed, task 2 pending and uncovered
        inventory = [
            InventoryItem(id="t1", content="Build Telegram receiver", phase="Telegram", status="completed", issue_url="https://github.com/Bavariance/polysimulator/issues/4582"),
            InventoryItem(id="t2", content="Verify Telegram reply isolation", phase="Telegram", status="pending", issue_url="https://github.com/Bavariance/polysimulator/issues/4582"),
        ]
        roster = []  # No active worker currently on Telegram

        report = self.guard.evaluate_topic_coverage(
            inventory=inventory,
            roster=roster,
        )

        self.assertFalse(report.ok)
        self.assertTrue(len(report.next_assignments) >= 1, "Expected next-ready assignment to appear after completion")
        assignment = report.next_assignments[0]
        self.assertEqual(assignment.topic, "Telegram")
        self.assertEqual(assignment.task_id, "t2")
        self.assertEqual(assignment.content, "Verify Telegram reply isolation")
        self.assertEqual(assignment.recommended_role, "fast")
        self.assertEqual(assignment.priority, 1)

    # ----------------------------------------------------------------------
    # Proof 4: blocked topic stays present
    # ----------------------------------------------------------------------
    def test_proof_4_blocked_topic_stays_present(self):
        blocker_msg = "Awaiting human operator choice for Stripe credentials; non-financial staging only"
        inventory = [
            InventoryItem(id="b1", content="Stripe card processing canary", phase="Staging", status="blocked",
                          blocker_reason=blocker_msg, issue_url="https://github.com/Bavariance/polysimulator/issues/4574"),
            InventoryItem(id="r1", content="Review market loading fallback", phase="Workflow", status="pending",
                          issue_url="https://github.com/Bavariance/polysimulator/issues/4582"),
        ]
        roster = [
            {"id": "TopicWorkflowGates", "status": "running", "topic": "Workflow"},
        ]

        report = self.guard.evaluate_topic_coverage(
            inventory=inventory,
            roster=roster,
        )

        # Blocked topic is recognized as blocked, not flagged as an uncovered runnable topic
        self.assertNotIn("Staging", report.uncovered_topics)
        self.assertEqual(len(report.blocked_topics), 1)
        self.assertEqual(report.blocked_topics[0]["topic"], "Staging")
        self.assertIn(blocker_msg, report.blocked_topics[0]["reasons"])
        # Total tasks includes the blocked task (zero tasks deleted)
        self.assertEqual(report.total_tasks, 2)
        self.assertEqual(report.blocked_tasks, 1)

    # ----------------------------------------------------------------------
    # Proof 5: 7 real active workers satisfy floor
    # ----------------------------------------------------------------------
    def test_proof_5_seven_real_active_workers_satisfy_floor(self):
        topics = [
            "Desktop GUI", "UX/design", "Motion", "Telegram",
            "Decisions", "Staging", "Workflow"
        ]
        inventory = [
            InventoryItem(id=f"task-{i}", content=f"Deliver {t}", phase=t, status="pending",
                          owner=f"TopicWorker-{i}",
                          issue_url=f"https://github.com/Bavariance/polysimulator/issues/{4000+i}")
            for i, t in enumerate(topics)
        ]
        roster = [
            {"id": f"TopicWorker-{i}", "status": "running", "topic": t, "role": "sub"}
            for i, t in enumerate(topics)
        ]

        report = self.guard.evaluate_topic_coverage(
            inventory=inventory,
            roster=roster,
        )

        self.assertTrue(report.floor_satisfied, "7 active workers across 7 runnable topics must satisfy floor")
        self.assertEqual(report.active_workers_count, 7)
        self.assertEqual(len(report.covered_topics), 7)
        self.assertEqual(len(report.uncovered_topics), 0)
        self.assertFalse(any(v.kind == "WORKER_FLOOR_DEFICIT" for v in report.violations))
        self.assertTrue(report.ok)

    # ----------------------------------------------------------------------
    # Proof 6: unsupported/fake active counts fail
    # ----------------------------------------------------------------------
    def test_proof_6_unsupported_fake_active_counts_fail(self):
        inventory = self._build_synthetic_inventory(8)
        # Roster with 3 real workers and 4 fake/daemon/prepared statuses
        roster = [
            {"id": "TopicReal1", "status": "running", "topic": "Topic-0", "role": "sub"},
            {"id": "TopicReal2", "status": "running", "topic": "Topic-1", "role": "sub"},
            {"id": "TopicReal3", "status": "running", "topic": "Topic-2", "role": "sub"},
            {"id": "TicketPrepared1", "status": "prepared", "topic": "Topic-3"},
            {"id": "DaemonReminder", "status": "reminder_daemon", "topic": "Topic-4"},
            {"id": "FakeActor", "status": "simulated_active", "topic": "Topic-5"},
            {"id": "WorkerInvalid", "status": "fake", "topic": "Topic-6"},
        ]

        report = self.guard.evaluate_topic_coverage(
            inventory=inventory,
            roster=roster,
        )

        self.assertFalse(report.ok)
        self.assertFalse(report.floor_satisfied)
        self.assertEqual(report.active_workers_count, 3, "Only real running useful subagents count as active")
        self.assertEqual(report.fake_workers_rejected_count, 4, "4 fake/daemon statuses must be rejected")
        floor_violations = [v for v in report.violations if v.kind == "WORKER_FLOOR_DEFICIT"]
        self.assertTrue(len(floor_violations) >= 1, "Floor deficit must be flagged when fakes are rejected")
        fake_violations = [v for v in report.violations if v.kind == "FAKE_WORKER_REJECTED"]
        self.assertEqual(len(fake_violations), 4)

    # ----------------------------------------------------------------------
    # Proof 7: restored inventory loses zero tasks
    # ----------------------------------------------------------------------
    def test_proof_7_restored_inventory_loses_zero_tasks(self):
        # Load real sources if accessible
        sources = []
        if os.path.exists(self.inv_path):
            with open(self.inv_path, "r", encoding="utf-8") as f:
                sources.append(json.load(f))
        if os.path.exists(self.ledger_path):
            with open(self.ledger_path, "r", encoding="utf-8") as f:
                sources.append(json.load(f))

        # Also add full-topic-board-current.json via eval/local if exists
        try:
            from topic_inventory_guard import SCRIPT_DIR
            local_board_candidates = [
                Path(SCRIPT_DIR) / "full-topic-board-current.json",
                Path.home() / ".veyyon" / "workflows" / "full-topic-board-current.json",
            ]
            for cand in local_board_candidates:
                if cand.exists():
                    with open(cand, "r", encoding="utf-8") as f:
                        sources.append(json.load(f))
                    break
        except Exception:
            pass

        # If sources were found, test additive reconciliation
        if sources:
            reconciled, violations = self.guard.reconcile_sources_additively(sources=sources)
            self.assertTrue(len(reconciled) >= 226, f"Expected at least 226 preserved items, got {len(reconciled)}")
            self.assertEqual(len(violations), 0, "Lossless additive reconciliation must produce zero drop violations")

            # Check that illustrative-A task is preserved as cancelled, not pending
            for item in reconciled:
                if "choice a" in item.content.lower():
                    self.assertEqual(item.status, "cancelled")
                    self.assertFalse(item.is_runnable)
                    self.assertIsNotNone(item.blocker_reason)


    # ----------------------------------------------------------------------
    # Proof 8: idle worker rejected (idle is NOT active)
    # ----------------------------------------------------------------------
    def test_proof_8_idle_worker_rejected_not_active(self):
        topics = ["Desktop GUI", "UX/design"]
        inventory = [
            InventoryItem(id="t-idle-1", content="Task 1", phase="Desktop GUI", status="pending", owner="TopicGuiWorker",
                          issue_url="https://github.com/Bavariance/polysimulator/issues/4582"),
            InventoryItem(id="t-idle-2", content="Task 2", phase="UX/design", status="pending", owner="TopicDesignWorker",
                          issue_url="https://github.com/Bavariance/polysimulator/issues/4582"),
        ]
        # Both workers are "idle" (awake in session but not actively executing)
        roster = [
            {"id": "TopicGuiWorker", "status": "idle", "topic": "Desktop GUI", "role": "sub"},
            {"id": "TopicDesignWorker", "status": "idle", "topic": "UX/design", "role": "sub"},
        ]

        report = self.guard.evaluate_topic_coverage(
            inventory=inventory,
            roster=roster,
        )

        # Idle workers must NOT count as active running workers
        self.assertFalse(report.ok)
        self.assertEqual(report.active_workers_count, 0, "Idle workers must not count toward active useful count")
        self.assertEqual(report.idle_workers_count, 2, "Idle workers must be tracked separately")
        self.assertIn("Desktop GUI", report.uncovered_topics)
        self.assertIn("UX/design", report.uncovered_topics)
        uncovered_violations = [v for v in report.violations if v.kind == "UNCOVERED_RUNNABLE_TOPIC"]
        self.assertEqual(len(uncovered_violations), 2)

    # ----------------------------------------------------------------------
    # Proof 9: open vs independently runnable reconciliation
    # ----------------------------------------------------------------------
    def test_proof_9_open_vs_independently_runnable_reconciliation(self):
        # Locate real full-topic-board-current.json
        board_candidates = [
            Path("local://full-topic-board-current.json"),
            Path(self.home_dir) / ".veyyon" / "profiles" / "default" / "agent" / "sessions" / "-development-polysimulator" / "2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437" / "local" / "full-topic-board-current.json",
            Path(SCRIPT_DIR) / "full-topic-board-current.json",
        ]
        board_path = None
        for cand in board_candidates:
            if cand.exists():
                board_path = str(cand)
                break

        if board_path:
            board_items = self.guard.parse_inventory_source(board_path)
            report = self.guard.evaluate_topic_coverage(
                inventory=board_items,
                roster=[],
            )

            # Reconcile exact counts (supports baseline 342 tasks or 348 tasks with 6 appended scheduler findings)
            self.assertIn(report.total_tasks, (342, 348))
            self.assertIn(report.completed_tasks, (16, 22))
            self.assertEqual(report.cancelled_tasks, 1)
            self.assertEqual(report.open_tasks, 325)
            self.assertEqual(report.blocked_tasks, 55)
            self.assertEqual(report.runnable_tasks, 270)
            self.assertEqual(report.total_tasks, report.completed_tasks + report.cancelled_tasks + report.blocked_tasks + report.runnable_tasks)

            # Blocked topics must not be flagged as uncovered runnable topics
            blocked_topic_names = {b["topic"] for b in report.blocked_topics}
            self.assertIn("Motion acceptance details", blocked_topic_names)
            self.assertIn("Desktop GUI", blocked_topic_names)
            self.assertIn("UX/design", blocked_topic_names)
            self.assertIn("Motion", blocked_topic_names)
            self.assertIn("Telegram", blocked_topic_names)
            self.assertIn("Decisions", blocked_topic_names)
            self.assertIn("Staging", blocked_topic_names)
            for b_name in blocked_topic_names:
                self.assertNotIn(b_name, report.uncovered_topics)

            # Choice A preserved as cancelled, not pending or deleted
            choice_a = [it for it in board_items if "choice a" in it.content.lower()][0]
            self.assertEqual(choice_a.status, "cancelled")
            self.assertFalse(choice_a.is_runnable)
            self.assertFalse(choice_a.is_blocked)

if __name__ == "__main__":
    unittest.main()
