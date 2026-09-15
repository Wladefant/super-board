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

    def test_restart_keeps_unanswered_question_and_bounded_cadence(self):
        record = self.ask()
        self.service.run("sent", {"id": record["decision_id"], "message_id": 17}, self.route)
        restarted = module.OperatorQuestions(self.decisions, self.pool)
        persisted = restarted.run("get", {"id": record["decision_id"]}, self.route)["question"]
        self.assertEqual(persisted["status"], "pending")
        self.assertIsNone(persisted["answer"])
        self.assertGreaterEqual(persisted["next_reminder_at"] - persisted["last_notified_at"], 900)
        self.assertEqual(restarted.run("due", {}, self.route)["questions"], [])

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
        self.assertEqual(self.service.run("due", {}, self.route)["questions"][0]["decision_id"], record["decision_id"])


if __name__ == "__main__":
    unittest.main()
