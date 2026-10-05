"""Telegram question transport over the existing decision workflow and callback store.

A transport answer is guidance, NEVER a native approval grant or ledger unblock.
JSON stdin/stdout keeps prose and credentials out of shell argument parsing.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
import re
from pathlib import Path
import sys
import time
import uuid

SOURCE_WORKFLOWS = Path(__file__).resolve().parents[3] / "workflows" / "portable"
sys.path.insert(0, str(SOURCE_WORKFLOWS if SOURCE_WORKFLOWS.is_dir() else Path.home() / ".veyyon" / "workflows"))
from decision_workflow import DecisionManager, FileLock, get_iso_timestamp
from telegram_notifier import DecisionCallbackStore, NotificationEvent, build_decision_inline_keyboard, render_card


_STOPWORDS = frozenset(
    "this that with from have will your what when which should would could there their about into then than "
    "also just more some only been were they them does need want like make made over under each very".split())


def _terms(*texts: str) -> set:
    words = set()
    for text in texts:
        for word in re.findall(r"[a-z0-9][a-z0-9_\-]{3,}", str(text).lower()):
            if word not in _STOPWORDS:
                words.add(word)
    return words


def _epoch(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def possible_answers(questions: list, messages: list, min_shared: int = 2) -> list:
    """Pending questions that a later operator message may already answer. Read-only.

    A match is a suggestion, never an answer: the caller reads the message, then calls `resolve`
    (and states which question it closed and how it read the reply) or asks the question anyway.
    A message qualifies when it arrived after the question and shares at least `min_shared`
    distinctive words with the question text, its problem and its option labels.
    """
    found = []
    for question in questions:
        if question.get("status") != "pending":
            continue
        transport = question.get("transport", {})
        created = transport.get("created_ts") or _epoch(question["created_at"])
        wanted = _terms(question.get("question", ""), transport.get("problem", ""),
                        *[option.get("label", "") for option in question.get("options", [])])
        hits = []
        for message in messages:
            if _epoch(message["at"]) < created:
                continue
            shared = sorted(wanted & _terms(message.get("text", "")))
            if len(shared) >= min_shared:
                hits.append({"message_id": message.get("id"), "shared": shared, "text": message.get("text", "")})
        if hits:
            hits.sort(key=lambda hit: -len(hit["shared"]))
            found.append({"decision_id": question["decision_id"], "question": question["question"],
                          "possibly_answered_by": hits})
    return found


class OperatorQuestions:
    def __init__(self, decisions_path: str, pool_path: str):
        self.manager = DecisionManager(decisions_path=decisions_path)
        self.callbacks = DecisionCallbackStore(Path(pool_path))

    def run(self, operation: str, payload: dict, route: dict) -> dict:
        # The caller supplies the currently leased route, never a question-supplied target.
        required = ("session_id", "chat_id", "user_id")
        if any(not str(route.get(key, "")).strip() for key in required):
            raise ValueError("A session-bound operator route is required")
        now = time.time()
        with FileLock(self.manager.lock_path):
            data = self.manager._load_data_unlocked()
            questions = data["decisions"]
            if operation == "register":
                question = str(payload.get("question", "")).strip()
                options = payload.get("options", [])
                if not question or len(question) > 600 or not 1 <= len(options) <= 8:
                    raise ValueError("Use a clear question (up to 600 characters) and 1–8 options")
                ids = [str(option.get("id", "")) for option in options]
                if len(set(ids)) != len(ids) or any(not value or value.startswith("__") for value in ids):
                    raise ValueError("Options need unique nonempty identifiers; __ is reserved")
                if payload.get("recommendation") not in ids:
                    raise ValueError("Recommendation must identify one of the options")
                for option in options:
                    if not option.get("label") or len(option["label"]) > 60 or len(option.get("description", "")) > 240:
                        raise ValueError("Each option needs a readable label (up to 60 characters) and short description (up to 240)")
                identifier = "tq:" + uuid.uuid4().hex
                record = {
                    "decision_id": identifier, "request_id": payload.get("request_id", identifier),
                    "question": question, "prompt": payload.get("problem", question), "options": options,
                    "recommendation": payload["recommendation"], "blocking_dependencies": [],
                    "authorized_responders": [route["user_id"]], "session_id": route["session_id"],
                    "status": "pending", "answer": None, "created_at": get_iso_timestamp(),
                    "transport": {**route, "kind": "operator_question", "selection": None,
                                  "events": [], "problem": payload.get("problem", ""),
                                  "impact": payload.get("impact", ""), "details_url": payload.get("details_url"),
                                  "created_ts": now, "message_id": None, "topic_message_id": None,
                                  "topic_card_at": None, "session_finalized": False,
                                  "wait": payload.get("wait") is not False, "answer_pushed": False},
                }
                questions[identifier] = record
            else:
                identifier = payload.get("id")
                record = questions.get(identifier)
                if not record or not self._owns(record, route):
                    raise ValueError("Question is unavailable on this session and operator route")
                if operation == "wait":
                    if record.get("status") in ("answered", "dropped"):
                        return self._closed_result(record)
                    timeout = max(0.05, float(payload.get("timeout", 60.0)))
                    poll_interval = max(0.02, float(payload.get("poll_interval", 0.05)))
                    return self._wait_for_answer(identifier, route, timeout, poll_interval)
                if operation == "answer":
                    event_id = str(payload["event_id"])
                    events = record["transport"]["events"]
                    if not any(event["id"] == event_id for event in events):
                        if record["status"] == "dropped":
                            raise ValueError("Question was dropped; it takes no more answers")
                        if record["status"] == "answered":
                            raise ValueError("Answer already sent; reopen the question to change it")
                        choice = payload.get("choice")
                        if choice and choice not in [o["id"] for o in record["options"]] + ["__send"]:
                            raise ValueError("That option does not belong to this question")
                        if choice and choice != "__send":
                            record["transport"]["selection"] = choice
                        else:
                            text = str(payload.get("text", "")).strip()
                            selected = record["transport"]["selection"]
                            if not selected and not text:
                                raise ValueError("Select an option or reply in your own words first")
                            record["answer"] = {"question_id": identifier, "choice_id": selected,
                                                "text": text, "origin": "telegram_account", "actor_id": route["user_id"],
                                                "authorization": False, "answered_at": get_iso_timestamp()}
                            record["status"] = "answered"
                        events.append({"id": event_id, "choice": choice, "text": payload.get("text"), "at": now})
                elif operation == "resolve":
                    # The operator answered somewhere else: the terminal, or in prose the agent heard.
                    if record["status"] == "pending":
                        text = str(payload.get("text", "")).strip()
                        choice = payload.get("choice")
                        if choice is not None and choice not in [o["id"] for o in record["options"]]:
                            raise ValueError("That option does not belong to this question")
                        if not text and not choice:
                            raise ValueError("Give the answer text or the chosen option")
                        record["answer"] = {"question_id": identifier, "choice_id": choice, "text": text,
                                            "origin": "agent_recorded", "actor_id": route["user_id"],
                                            "authorization": False, "answered_at": get_iso_timestamp()}
                        record["status"] = "answered"
                elif operation == "drop":
                    if record["status"] == "pending":
                        reason = str(payload.get("reason", "")).strip()
                        if not reason:
                            raise ValueError("Give the reason the question is dropped")
                        record["status"] = "dropped"
                        record["drop"] = {"reason": reason, "dropped_at": get_iso_timestamp()}
                elif operation == "sent":
                    record["transport"]["message_id"] = payload["message_id"]
                elif operation != "get":
                    raise ValueError("Unknown question operation")
            if operation != "get":
                self.manager._save_data_unlocked(data)
        return {"question": record}

    def cache(self, payload: dict) -> dict:
        """Daemon-owned Telegram message ids. Cache only: the question state never depends on them."""
        allowed = ("topic_message_id", "topic_card_at", "session_finalized", "answer_pushed")
        with FileLock(self.manager.lock_path):
            data = self.manager._load_data_unlocked()
            record = data["decisions"].get(payload.get("id"))
            if not record or record.get("transport", {}).get("kind") != "operator_question":
                raise ValueError("Unknown question")
            for key in allowed:
                if key in payload:
                    record["transport"][key] = payload[key]
            self.manager._save_data_unlocked(data)
        return {"question": record}

    @staticmethod
    def _closed_result(record: dict) -> dict:
        if record.get("status") == "dropped":
            return {"question": record, "status": "dropped", "drop": record.get("drop")}
        return {"question": record, "status": "answered", "answer": record["answer"]}

    @staticmethod
    def _owns(record: dict, route: dict) -> bool:
        transport = record.get("transport", {})
        return transport.get("kind") == "operator_question" and all(
            transport.get(key) == route.get(key) for key in ("session_id", "chat_id", "user_id"))

    def _wait_for_answer(self, identifier: str, route: dict, timeout: float, poll_interval: float) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll_interval)
            with FileLock(self.manager.lock_path):
                data = self.manager._load_data_unlocked()
                record = data["decisions"].get(identifier)
                if not record or not self._owns(record, route):
                    raise ValueError("Question is unavailable on this session and operator route")
                if record.get("status") in ("answered", "dropped"):
                    return self._closed_result(record)
        with FileLock(self.manager.lock_path):
            data = self.manager._load_data_unlocked()
            record = data["decisions"].get(identifier)
            return {"question": record, "status": "pending", "timed_out": True}

    def card(self, record: dict) -> dict:
        transport = record["transport"]
        selected = transport["selection"]
        options = record["options"] if not selected else [{"id": "__send", "label": "Send answer"}]
        markup = build_decision_inline_keyboard(record["decision_id"], options, record["session_id"],
                  transport["chat_id"], transport["user_id"], record["question"], self.callbacks)
        recommendation = next(o["label"] for o in record["options"] if o["id"] == record["recommendation"])
        text = render_card(NotificationEvent(event_type="question", project="Operator question", request_id=record["request_id"],
               summary=record["question"], canonical_link=transport.get("details_url"), metadata={
                   "question": record["question"], "problem": transport["problem"], "options": record["options"],
                   "proposed_action": "Select an option, then Send answer; or reply in your own words.",
                   "consequence_or_risk": transport["impact"] or "Work waits for your answer. Silence changes nothing.",
                   "recommendation": recommendation,
                   "long_detail": ("Selected: " + next(o["label"] for o in record["options"] if o["id"] == selected)
                                   + ". Reply to this message to add context and send the combined answer.") if selected else None,
               }))
        return {"text": text, "reply_markup": markup, "id": record["decision_id"]}

    def card_for(self, identifier: str) -> dict:
        """A fresh card for a pending question; the daemon posts it in the Questions topic."""
        with FileLock(self.manager.lock_path):
            record = self.manager._load_data_unlocked()["decisions"].get(identifier)
        if not record or record.get("transport", {}).get("kind") != "operator_question" or record["status"] != "pending":
            raise ValueError("Question is not open")
        return {"question": record, "card": self.card(record)}


def main():
    request = json.load(sys.stdin)
    service = OperatorQuestions(request["decisions_path"], request["pool_path"])
    operation = request["operation"]
    # Daemon-owned operations act on any question; they never answer or change one.
    if operation == "cache":
        result = service.cache(request.get("payload", {}))
    elif operation == "card_for":
        result = service.card_for(request["payload"]["id"])
    elif operation == "possible_answers":
        payload = request["payload"]
        pending = [q for q in service.manager._load_data_unlocked()["decisions"].values()
                   if q.get("transport", {}).get("kind") == "operator_question"]
        result = {"matches": possible_answers(pending, payload["messages"], int(payload.get("min_shared", 2)))}
    else:
        result = service.run(operation, request.get("payload", {}), request["route"])
        if request.get("card") and "question" in result and result["question"]["status"] == "pending":
            result["card"] = service.card(result["question"])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)
