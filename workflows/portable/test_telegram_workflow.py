#!/usr/bin/env python3
"""Focused behavior tests for the transport-free Telegram workflow facade."""

import tempfile
import unittest
from pathlib import Path

from decision_workflow import DecisionContract, DecisionManager, DecisionScope
from ledger import RequestLedger
from telegram_workflow import (
    MAX_PAGE_SIZE,
    NativeControlAuth,
    NativeControlSnapshotConsumer,
    TelegramIdentity,
    TelegramWorkflowFacade,
)


class _Packet:
    def __init__(self, request):
        self.request = request

    def to_dict(self):
        return {
            "status": "ready",
            "status_reason": "request is eligible",
            "next_action": "dispatch through existing adapter",
            "request": self.request,
            "decision_status": {"pending_count": 0},
        }


class _Coordinator:
    def __init__(self, ledger):
        self.ledger = ledger
        self.calls = []

    def evaluate_step(self, request_id=None):
        self.calls.append(request_id)
        return _Packet(self.ledger.get_request(request_id))


class TelegramWorkflowFacadeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ledger_path = root / "ledger.json"
        self.decisions_path = root / "decisions.json"
        self.ledger = RequestLedger(str(self.ledger_path))
        for i in range(25):
            self.ledger.add_request(
                req_id=f"REQ-{i:02d}",
                prompt=f"Task {i} token=super-secret C:\\private\\checkout",
                session="session-main",
                project="Wladefant/super-board",
                acceptance_criteria=[{"id": "AC-1", "description": "observable result"}],
                owner="WorkflowLane",
                task_type="local",
                github_repo="Wladefant/super-board",
                issue_number=75,
            )
        self.manager = DecisionManager(
            decisions_path=str(self.decisions_path), ledger_path=str(self.ledger_path)
        )
        self.coordinator = _Coordinator(self.ledger)
        self.identity = TelegramIdentity(
            chat_id="chat-1", user_id="user-1", session_id="session-main", actor="Wladefant"
        )
        snapshot = {
            "identity": {"id": "session-main", "actorId": "Wladefant", "chatId": "chat-1"},
            "agents": [
                {
                    "id": "agent-1",
                    "status": "running",
                    "summary": "Reading files",
                    "updatedAt": 123,
                    "secret": "must-not-leak",
                },
                {"id": "other", "status": "idle", "session_id": "other-session"},
            ],
            "sessions": [
                {"id": "session-main", "status": "running", "cwd": "C:\\safe\\project"},
                {"id": "other-session", "status": "idle", "cwd": "/srv/other"},
            ],
        }
        self.facade = TelegramWorkflowFacade(
            ledger=self.ledger,
            decisions=self.manager,
            coordinator=self.coordinator,
            native_snapshot=lambda: snapshot,
            binding_verifier=lambda identity: identity == self.identity,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_native_control_consumer_calls_exact_host_contract(self):
        calls = []

        class NativeControl:
            def getSessionIdentity(self, auth):
                calls.append(("identity", auth))
                return {"id": "session-main", "actorId": "Wladefant", "chatId": "chat-1"}

            def listAgents(self, request):
                calls.append(("agents", request))
                return {
                    "items": [
                        {
                            "id": "agent-native",
                            "name": "Native worker",
                            "status": "running",
                            "updatedAt": 123,
                        }
                    ]
                }

        consumer = NativeControlSnapshotConsumer(
            NativeControl(),
            NativeControlAuth(
                auth_token="native-token",
                actor_id="Wladefant",
                chat_id="chat-1",
                session_id="session-main",
            ),
        )
        snapshot = consumer()
        self.assertEqual("session-main", snapshot["identity"]["id"])
        self.assertEqual(["agent-native"], [item["id"] for item in snapshot["agents"]])
        self.assertEqual("native-token", calls[0][1]["authToken"])
        self.assertEqual(MAX_PAGE_SIZE, calls[1][1]["limit"])

    def test_binding_is_fail_closed_for_every_view(self):
        invalid = TelegramIdentity("chat-2", "user-1", "session-main", "Wladefant")
        with self.assertRaises(PermissionError):
            self.facade.list_agents(invalid)
        with self.assertRaises(PermissionError):
            self.facade.list_tasks(invalid)

    def test_native_snapshot_identity_must_match_verified_binding(self):
        self.facade.native_snapshot = lambda: {
            "identity": {"id": "other-session", "actorId": "Wladefant", "chatId": "chat-1"},
            "agents": [],
        }
        with self.assertRaises(PermissionError):
            self.facade.list_agents(self.identity)

    def test_agent_and_session_views_are_scoped_sanitized_and_identified(self):
        agents = self.facade.list_agents(self.identity)
        self.assertEqual("session-main", agents["session_id"])
        self.assertEqual(["agent-1"], [item["id"] for item in agents["items"]])
        self.assertNotIn("secret", agents["items"][0])

        sessions = self.facade.list_sessions(self.identity)
        selected = [item for item in sessions["items"] if item["selected"]]
        self.assertEqual("session-main", selected[0]["id"])
        self.assertEqual("project", selected[0]["workspace_label"])
        self.assertNotIn("cwd", selected[0])

    def test_task_pagination_is_bounded_and_text_is_sanitized(self):
        page = self.facade.list_tasks(self.identity, limit=999)
        self.assertEqual(MAX_PAGE_SIZE, len(page["items"]))
        self.assertEqual("20", page["next_cursor"])
        self.assertEqual(25, page["total"])
        self.assertNotIn("super-secret", page["items"][0]["prompt"])
        self.assertNotIn("C:\\private", page["items"][0]["prompt"])

        second = self.facade.list_tasks(self.identity, cursor=page["next_cursor"], limit=999)
        self.assertEqual(5, len(second["items"]))
        self.assertIsNone(second["next_cursor"])

    def test_status_reads_ledger_health_without_coordinator_side_effects(self):
        result = self.facade.task_status(self.identity, "REQ-00")
        self.assertEqual([], self.coordinator.calls)
        self.assertEqual("HEALTHY", result["status"])
        self.assertEqual("session-main", result["session_id"])

    def test_authentic_decision_choice_unblocks_and_replay_is_idempotent(self):
        decision = DecisionContract(
            decision_id="DEC-1",
            request_id="REQ-00",
            prompt="Choose implementation shape",
            question="Which existing adapter should be reused?",
            options=[
                {"id": "A", "label": "Existing coordinator", "description": "Reuse it", "tradeoffs": "No parallel lifecycle"},
                {"id": "B", "label": "New scheduler", "description": "Duplicate it", "tradeoffs": "Unsafe"},
            ],
            recommendation="Option A: Existing coordinator",
            blocking_dependencies=["REQ-00"],
            authorized_responders=["Wladefant"],
            decision_scope=DecisionScope.IMPLEMENTATION_STRATEGY,
            issue_number=75,
            issue_url="https://github.com/Wladefant/super-board/issues/75",
        )
        self.manager.register_question(decision)

        first = self.facade.submit_decision(
            self.identity, "DEC-1", "Option A — keep the coordinator", update_id="100", callback_query_id="cb-1"
        )
        self.assertEqual("answered", first["status"])
        self.assertEqual(["REQ-00"], first["unblocked_requests"])

        replay = self.facade.submit_decision(
            self.identity, "DEC-1", "Option A — keep the coordinator", update_id="100", callback_query_id="cb-1"
        )
        self.assertTrue(replay["idempotent_replay"])

    def test_unrestricted_free_text_reaches_existing_clarification_path(self):
        decision = DecisionContract(
            decision_id="DEC-FREE",
            request_id="REQ-01",
            prompt="Choose implementation shape",
            question="Which option?",
            options=[{"id": "A", "label": "Existing coordinator", "description": "Reuse", "tradeoffs": "None"}],
            recommendation="Option A",
            blocking_dependencies=["REQ-01"],
            authorized_responders=["Wladefant"],
            decision_scope=DecisionScope.IMPLEMENTATION_STRATEGY,
        )
        self.manager.register_question(decision)
        result = self.facade.submit_decision(
            self.identity,
            "DEC-FREE",
            "Custom proposal with expanded context; do not force buttons.",
            update_id="101",
        )
        self.assertEqual("clarification_requested", result["status"])
        self.assertIn("Please clarify", self.manager.get_decision("DEC-FREE")["clarification_prompt"])


if __name__ == "__main__":
    unittest.main()
