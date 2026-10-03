import importlib.util
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("operator_questions", Path(__file__).parents[1] / "src" / "operator_questions.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from telegram_notifier import QuestionReminderManager


class OperatorQuestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.decisions = str(Path(self.tmp.name) / "decisions.json")
        self.pool = str(Path(self.tmp.name) / "pool.db")
        self.service = module.OperatorQuestions(self.decisions, self.pool)
        self.route = {"session_id": "disposable-session", "chat_id": "1", "user_id": "1"}

    def ask(self, question="Which layout?"):
        return self.service.run("register", {"question": question, "options": [
            {"id": "a" * 100, "label": "Compact", "description": "Keeps all lanes visible"},
            {"id": "b", "label": "Spacious", "description": "Larger touch targets"}],
            "recommendation": "b"}, self.route)["question"]

    def test_two_questions_keep_button_answers_separate(self):
        first, second = self.ask(), self.ask("Which grouping?")
        for record, choice in [(second, "b"), (first, "a" * 100)]:
            self.service.run("answer", {"id": record["decision_id"], "event_id": "click" + choice, "choice": choice}, self.route)
            result = self.service.run("answer", {"id": record["decision_id"], "event_id": "send" + choice, "choice": "__send"}, self.route)
            self.assertEqual(result["question"]["answer"]["choice_id"], choice)
            self.assertFalse(result["question"]["answer"]["authorization"])

    def test_prose_only_and_option_plus_prose_are_question_answers(self):
        for choice in [None, "b"]:
            record = self.ask()
            if choice:
                self.service.run("answer", {"id": record["decision_id"], "event_id": "select", "choice": choice}, self.route)
            answer = self.service.run("answer", {"id": record["decision_id"], "event_id": "reply", "text": "Use larger targets but keep both search bars."}, self.route)["question"]["answer"]
            self.assertEqual(answer["choice_id"], choice)
            self.assertEqual(answer["text"], "Use larger targets but keep both search bars.")

    def test_restart_keeps_unanswered_question(self):
        record = self.ask()
        self.service.run("sent", {"id": record["decision_id"], "message_id": 17}, self.route)
        restarted = module.OperatorQuestions(self.decisions, self.pool)
        persisted = restarted.run("get", {"id": record["decision_id"]}, self.route)["question"]
        self.assertEqual(persisted["status"], "pending")
        self.assertIsNone(persisted["answer"])
        self.assertNotIn("next_reminder_at", persisted)

    def test_resolve_closes_a_question_answered_elsewhere_once(self):
        record = self.ask()
        closed = self.service.run("resolve", {"id": record["decision_id"], "choice": "b"}, self.route)["question"]
        self.assertEqual((closed["status"], closed["answer"]["choice_id"], closed["answer"]["origin"]), ("answered", "b", "agent_recorded"))
        again = self.service.run("resolve", {"id": record["decision_id"], "text": "later"}, self.route)["question"]
        self.assertEqual(again["answer"], closed["answer"])
        with self.assertRaisesRegex(ValueError, "answer text or the chosen option"):
            self.service.run("resolve", {"id": self.ask("Another?")["decision_id"]}, self.route)
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.service.run("resolve", {"id": self.ask("Third?")["decision_id"], "choice": "zzz"}, self.route)

    def test_drop_needs_a_reason_and_ends_waiting(self):
        record = self.ask()
        with self.assertRaisesRegex(ValueError, "reason"):
            self.service.run("drop", {"id": record["decision_id"]}, self.route)
        dropped = self.service.run("drop", {"id": record["decision_id"], "reason": "obsolete"}, self.route)["question"]
        self.assertEqual((dropped["status"], dropped["drop"]["reason"]), ("dropped", "obsolete"))
        self.assertEqual(self.service.run("wait", {"id": record["decision_id"], "timeout": 0.3}, self.route)["status"], "dropped")
        with self.assertRaisesRegex(ValueError, "dropped"):
            self.service.run("answer", {"id": record["decision_id"], "event_id": "e", "text": "x"}, self.route)

    def test_daemon_cache_and_card_for_serve_open_questions_only(self):
        record = self.ask()
        cached = self.service.cache({"id": record["decision_id"], "topic_message_id": 99, "topic_card_at": 5.0, "message_id": 7})
        self.assertEqual(cached["question"]["transport"]["topic_message_id"], 99)
        self.assertNotEqual(cached["question"]["transport"].get("message_id"), 7)  # only cache fields are writable
        card = self.service.card_for(record["decision_id"])
        self.assertEqual(card["question"]["decision_id"], record["decision_id"])
        self.assertTrue(card["card"]["reply_markup"]["inline_keyboard"])
        self.service.run("resolve", {"id": record["decision_id"], "choice": "b"}, self.route)
        with self.assertRaisesRegex(ValueError, "not open"):
            self.service.card_for(record["decision_id"])

    def test_context_labels_and_opaque_tokens_use_existing_store(self):
        record = self.ask()
        card = self.service.card(record)
        buttons = card["reply_markup"]["inline_keyboard"]
        self.assertEqual(len(buttons), 2)
        self.assertTrue(all(len(row) == 1 for row in buttons))
        for row in buttons:
            self.assertLessEqual(len(row[0]["callback_data"].encode()), 64)
        with closing(sqlite3.connect(self.pool)) as db:
            rows = db.execute("select decision_id,choice_id,session_id from decision_callbacks").fetchall()
        self.assertEqual({row[1] for row in rows}, {"a" * 100, "b"})
        self.assertTrue(all(row[0] == record["decision_id"] for row in rows))
        self.assertIn("<blockquote expandable>", card["text"])
        self.assertIn("<b>Recommended:</b> Spacious", card["text"])
        self.assertIn("Reply to this message", card["text"])

    def test_wrong_session_cannot_answer_and_replay_is_idempotent(self):
        record = self.ask()
        with self.assertRaisesRegex(ValueError, "unavailable"):
            self.service.run("answer", {"id": record["decision_id"], "event_id": "bad", "text": "help"}, {**self.route, "session_id": "other"})
        payload = {"id": record["decision_id"], "event_id": "reply", "text": "Keep it compact"}
        first = self.service.run("answer", payload, self.route)["question"]["answer"]
        self.assertEqual(self.service.run("answer", payload, self.route)["question"]["answer"], first)
        with self.assertRaisesRegex(ValueError, "already sent"):
            self.service.run("answer", {**payload, "event_id": "another"}, self.route)

    def test_global_reminders_cannot_duplicate_channel_owned_questions(self):
        record = self.ask()
        root = Path(self.tmp.name)
        reminders = QuestionReminderManager(decisions_path=Path(self.decisions),
            ledger_path=root / "ledger.json", state_file=root / "notify.json")
        self.assertEqual([q.decision_id for q in reminders.get_unresolved_questions(force=True)], [record["decision_id"]])
        adapter = Mock()
        self.assertEqual(reminders.dispatch_reminders(adapter, force=True)["due_count"], 0)
        adapter.notify.assert_not_called()

    def test_wait_times_out_and_returns_pending(self):
        record = self.ask()
        result = self.service.run("wait", {"id": record["decision_id"], "timeout": 0.3}, self.route)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result.get("timed_out"))
        self.assertEqual(result["question"]["decision_id"], record["decision_id"])


if __name__ == "__main__":
    unittest.main()
