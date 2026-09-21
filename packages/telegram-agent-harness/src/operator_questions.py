"""Telegram question transport over the existing decision workflow and callback store.

A transport answer is guidance, NEVER a native approval grant or ledger unblock.
JSON stdin/stdout keeps prose and credentials out of shell argument parsing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
import uuid

SOURCE_WORKFLOWS = Path(__file__).resolve().parents[3] / "workflows" / "portable"
sys.path.insert(0, str(SOURCE_WORKFLOWS if SOURCE_WORKFLOWS.is_dir() else Path.home() / ".veyyon" / "workflows"))
from decision_workflow import DecisionManager, FileLock, get_iso_timestamp
from telegram_notifier import DecisionCallbackStore, NotificationEvent, build_decision_inline_keyboard, render_card


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
                    "reminder_status": "active", "cadence_seconds": max(900, float(payload.get("cadence_seconds", 900))),
                    "next_reminder_at": now, "reminder_count": 0,
                    "transport": {**route, "kind": "operator_question", "selection": None,
                                  "events": [], "problem": payload.get("problem", ""),
                                  "impact": payload.get("impact", ""), "details_url": payload.get("details_url")},
                }
                questions[identifier] = record
            elif operation == "due":
                return {"questions": [q for q in questions.values() if self._owns(q, route)
                        and q.get("status") != "answered" and q.get("next_reminder_at", 0) <= now]}
            else:
                identifier = payload.get("id")
                record = questions.get(identifier)
                if not record or not self._owns(record, route):
                    raise ValueError("Question is unavailable on this session and operator route")
                if operation == "wait":
                    if record.get("status") == "answered" and record.get("answer"):
                        return {"question": record, "status": "answered", "answer": record["answer"]}
                    timeout = max(0.05, float(payload.get("timeout", 60.0)))
                    poll_interval = max(0.02, float(payload.get("poll_interval", 0.05)))
                    return self._wait_for_answer(identifier, route, timeout, poll_interval)
                if operation == "answer":
                    event_id = str(payload["event_id"])
                    events = record["transport"]["events"]
                    if not any(event["id"] == event_id for event in events):
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
                            record["reminder_status"] = "answered"
                        events.append({"id": event_id, "choice": choice, "text": payload.get("text"), "at": now})
                elif operation == "sent":
                    record["last_notified_at"] = now
                    record["next_reminder_at"] = now + record["cadence_seconds"]
                    record["reminder_count"] += 1
                    record["transport"]["message_id"] = payload["message_id"]
                elif operation != "get":
                    raise ValueError("Unknown question operation")
            if operation != "get":
                self.manager._save_data_unlocked(data)
        return {"question": record}

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
                if record.get("status") == "answered" and record.get("answer"):
                    return {"question": record, "status": "answered", "answer": record["answer"]}
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


def main():
    request = json.load(sys.stdin)
    service = OperatorQuestions(request["decisions_path"], request["pool_path"])
    result = service.run(request["operation"], request.get("payload", {}), request["route"])
    if request.get("card") and "question" in result and result["question"]["status"] != "answered":
        result["card"] = service.card(result["question"])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)
