#!/usr/bin/env python3
"""Portable Telegram-facing views over the existing Superboard workflow.

This module is deliberately transport-free.  A Telegram adapter supplies a verified
chat/user/session binding and the native Veyyon read model; this facade only reads
those models and delegates decisions to the existing DecisionManager.  It owns no
poller, worker lifecycle, scheduler, credentials, or session creation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

try:
    from decision_workflow import DecisionManager, ProvenanceType
    from ledger import RequestLedger
    from telegram_notifier import SecretSanitizer
except ImportError as exc:  # pragma: no cover - import diagnostics
    raise ImportError(f"telegram_workflow failed to import portable workflow modules: {exc}")


MAX_PAGE_SIZE = 20
MAX_TEXT_LENGTH = 1200
MAX_NATIVE_SNAPSHOT_ITEMS = 200


@dataclass(frozen=True)
class TelegramIdentity:
    """Identity already authenticated and session-bound by the Telegram adapter."""

    chat_id: str
    user_id: str
    session_id: str
    actor: str


@dataclass(frozen=True)
class NativeControlAuth:
    """Exact authentication envelope accepted by Veyyon's native bridge."""

    auth_token: str
    actor_id: str
    chat_id: str
    session_id: str

    def to_native(self) -> Dict[str, str]:
        return {
            "authToken": self.auth_token,
            "actorId": self.actor_id,
            "chatId": self.chat_id,
            "sessionId": self.session_id,
        }


class NativeControlSnapshotConsumer:
    """Consumes the real native bridge contract into the portable snapshot shape."""

    def __init__(
        self,
        native_control: Any,
        auth: NativeControlAuth,
        sessions: Optional[Callable[[], Sequence[Mapping[str, Any]]]] = None,
    ):
        if not callable(getattr(native_control, "getSessionIdentity", None)):
            raise TypeError("native_control must expose getSessionIdentity")
        if not callable(getattr(native_control, "listAgents", None)):
            raise TypeError("native_control must expose listAgents")
        self.native_control = native_control
        self.auth = auth
        self.sessions = sessions

    def __call__(self) -> Mapping[str, Any]:
        native_auth = self.auth.to_native()
        identity = self.native_control.getSessionIdentity(dict(native_auth))
        if not isinstance(identity, Mapping):
            raise TypeError("native control returned an invalid identity response")

        agents: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        seen_cursors = set()
        while len(agents) < MAX_NATIVE_SNAPSHOT_ITEMS:
            request: Dict[str, Any] = {**native_auth, "limit": MAX_PAGE_SIZE}
            if cursor is not None:
                request["cursor"] = cursor
            page = self.native_control.listAgents(request)
            if not isinstance(page, Mapping):
                raise TypeError("native control returned an invalid agent page")
            items = page.get("items")
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                raise TypeError("native control listAgents response must contain items")
            agents.extend(
                dict(item)
                for item in items
                if isinstance(item, Mapping)
            )
            agents = agents[:MAX_NATIVE_SNAPSHOT_ITEMS]
            next_cursor = page.get("nextCursor") or page.get("next_cursor")
            if next_cursor is None:
                break
            cursor = str(next_cursor)
            if not cursor or cursor in seen_cursors:
                raise ValueError("native control returned a repeated pagination cursor")
            seen_cursors.add(cursor)

        return {
            "identity": dict(identity),
            "agents": agents,
            "sessions": [
                dict(item)
                for item in (self.sessions() if self.sessions else [])
                if isinstance(item, Mapping)
            ],
        }

@dataclass
class WorkflowPage:
    kind: str
    session_id: str
    items: List[Dict[str, Any]]
    next_cursor: Optional[str]
    total: int

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TelegramWorkflowFacade:
    """Bounded read views and authentic decision forwarding for Telegram clients.

    ``binding_verifier`` is mandatory and fail-closed. ``native_snapshot`` is an
    injected read-only adapter over Veyyon's canonical agent/session APIs. It must
    include the result of native ``getSessionIdentity`` beside ``listAgents``.
    Neither dependency is recreated or persisted here.
    """

    _AGENT_FIELDS = (
        "id", "name", "status", "role", "provider", "model", "progress",
        "current_task", "result_summary", "session_id", "updated_at",
    )
    _SESSION_FIELDS = (
        "id", "name", "status", "selected", "main_agent_id", "updated_at",
    )

    def __init__(
        self,
        ledger: RequestLedger,
        decisions: DecisionManager,
        coordinator: Any,
        native_snapshot: Callable[[], Mapping[str, Any]],
        binding_verifier: Callable[[TelegramIdentity], bool],
    ):
        if not callable(native_snapshot) or not callable(binding_verifier):
            raise TypeError("native_snapshot and binding_verifier must be callable")
        self.ledger = ledger
        self.decisions = decisions
        self.coordinator = coordinator
        self.native_snapshot = native_snapshot
        self.binding_verifier = binding_verifier

    def _authorize(self, identity: TelegramIdentity) -> None:
        if not all((identity.chat_id, identity.user_id, identity.session_id, identity.actor)):
            raise PermissionError("Telegram identity binding is incomplete")
        if not self.binding_verifier(identity):
            raise PermissionError("Telegram actor/chat/session binding was not verified")
    def _verified_snapshot(self, identity: TelegramIdentity) -> Mapping[str, Any]:
        snapshot = self.native_snapshot()
        native_identity = snapshot.get("identity") or {}
        session_id = str(native_identity.get("id") or native_identity.get("sessionId") or "")
        actor_id = str(native_identity.get("actorId") or native_identity.get("actor_id") or "")
        chat_id = str(native_identity.get("chatId") or native_identity.get("chat_id") or "")
        if (
            session_id != identity.session_id
            or actor_id not in {identity.user_id, identity.actor}
            or chat_id != identity.chat_id
        ):
            raise PermissionError("Native snapshot identity does not match Telegram binding")
        return snapshot


    @staticmethod
    def _page_bounds(cursor: Optional[str], limit: int) -> tuple[int, int]:
        try:
            start = int(cursor or "0")
        except (TypeError, ValueError):
            raise ValueError("cursor must be a non-negative integer")
        if start < 0:
            raise ValueError("cursor must be a non-negative integer")
        return start, max(1, min(int(limit), MAX_PAGE_SIZE))

    @staticmethod
    def _safe_text(value: Any, limit: int = MAX_TEXT_LENGTH) -> str:
        text = SecretSanitizer.sanitize(str(value or ""))
        text = re.sub(
            r"(?i)\b(password|secret|token|api[_-]?key)\s*[\"']?\s*[:=]\s*[^\s,;]+",
            r"\1=[REDACTED]",
            text,
        )
        text = re.sub(r"(?i)\b[A-Z]:\\(?:[^\\\s]+\\)+", r"C:\\<path>\\", text)
        text = re.sub(r"(?<![:/A-Za-z0-9._~-])/(?:[^/\s,;]+/)+[^/\s,;]+", "/<path>", text)
        return text[:limit]

    @classmethod
    def _native_item(cls, raw: Mapping[str, Any], fields: Sequence[str]) -> Dict[str, Any]:
        item: Dict[str, Any] = {}
        for key in fields:
            if key in raw and raw[key] is not None:
                value = raw[key]
                item[key] = value if isinstance(value, (bool, int, float)) else cls._safe_text(value)
        return item
    @classmethod
    def _native_agent_item(
        cls, raw: Mapping[str, Any], session_id: str
    ) -> Dict[str, Any]:
        normalized = dict(raw)
        aliases = {
            "updatedAt": "updated_at",
            "currentTask": "current_task",
            "resultSummary": "result_summary",
            "result": "result_summary",
            "summary": "progress",
            "sessionId": "session_id",
        }
        for source, target in aliases.items():
            if source in normalized and target not in normalized:
                normalized[target] = normalized[source]
        normalized.setdefault("session_id", session_id)
        return cls._native_item(normalized, cls._AGENT_FIELDS)


    @staticmethod
    def _workspace_label(raw: Mapping[str, Any]) -> Optional[str]:
        workspace = raw.get("workspace_label") or raw.get("workspace") or raw.get("cwd")
        if not workspace:
            return None
        return os.path.basename(str(workspace).rstrip("/\\").replace("\\", "/"))[:120]

    def _page(
        self,
        identity: TelegramIdentity,
        kind: str,
        items: List[Dict[str, Any]],
        cursor: Optional[str],
        limit: int,
    ) -> Dict[str, Any]:
        start, size = self._page_bounds(cursor, limit)
        selected = items[start : start + size]
        next_cursor = str(start + size) if start + size < len(items) else None
        return WorkflowPage(kind, identity.session_id, selected, next_cursor, len(items)).to_dict()

    def list_agents(
        self, identity: TelegramIdentity, cursor: Optional[str] = None, limit: int = 10
    ) -> Dict[str, Any]:
        self._authorize(identity)
        snapshot = self._verified_snapshot(identity)
        agents = [
            self._native_agent_item(item, identity.session_id)
            for item in snapshot.get("agents", [])
            if (
                not item.get("session_id") and not item.get("sessionId")
            ) or str(item.get("session_id") or item.get("sessionId")) == identity.session_id
        ]
        return self._page(identity, "agents", agents, cursor, limit)

    def list_sessions(
        self, identity: TelegramIdentity, cursor: Optional[str] = None, limit: int = 10
    ) -> Dict[str, Any]:
        self._authorize(identity)
        snapshot = self._verified_snapshot(identity)
        raw_sessions = list(snapshot.get("sessions", []))
        if not raw_sessions:
            raw_sessions = [{"id": identity.session_id, "status": "running"}]
        sessions = []
        for raw in raw_sessions:
            candidate_id = str(raw.get("id") or "")
            candidate = TelegramIdentity(
                chat_id=identity.chat_id,
                user_id=identity.user_id,
                session_id=candidate_id,
                actor=identity.actor,
            )
            if not candidate_id or not self.binding_verifier(candidate):
                continue
            item = self._native_item(raw, self._SESSION_FIELDS)
            workspace_label = self._workspace_label(raw)
            if workspace_label:
                item["workspace_label"] = self._safe_text(workspace_label, 120)
            item["selected"] = item.get("id") == identity.session_id
            sessions.append(item)
        return self._page(identity, "sessions", sessions, cursor, limit)

    @classmethod
    def _task_item(cls, request: Mapping[str, Any], detail: bool = False) -> Dict[str, Any]:
        github = request.get("github") or {}
        superboard = request.get("superboard") or {}
        item: Dict[str, Any] = {
            "id": cls._safe_text(request.get("id"), 160),
            "state": cls._safe_text(request.get("state"), 80),
            "owner": cls._safe_text(request.get("owner"), 160),
            "prompt": cls._safe_text(request.get("prompt"), 500),
            "next_action": cls._safe_text(request.get("next_action"), 500),
            "issue_url": cls._safe_text(github.get("issue_url"), 500),
            "superboard_status": cls._safe_text(superboard.get("status"), 80),
        }
        if detail:
            item.update(
                {
                    "blocker": cls._safe_text(request.get("blocker"), 500),
                    "head": cls._safe_text(request.get("head"), 80),
                    "dependencies": [cls._safe_text(v, 160) for v in request.get("dependencies", [])[:20]],
                    "decision_blockers": [cls._safe_text(v, 160) for v in request.get("decision_blockers", [])[:20]],
                    "acceptance_criteria": [
                        {
                            "id": cls._safe_text(c.get("id"), 80),
                            "description": cls._safe_text(c.get("description"), 500),
                            "status": cls._safe_text(c.get("status"), 80),
                        }
                        for c in request.get("acceptance_criteria", [])[:20]
                    ],
                }
            )
        return item

    @staticmethod
    def _require_session_request(
        identity: TelegramIdentity, request: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        if str(request.get("session") or "") != identity.session_id:
            raise PermissionError("Superboard request is not bound to the selected session")
        return request

    def list_tasks(
        self,
        identity: TelegramIdentity,
        cursor: Optional[str] = None,
        limit: int = 10,
        state: Optional[str] = None,
    ) -> Dict[str, Any]:
        self._authorize(identity)
        requests = [
            request
            for request in self.ledger.list_requests(state=state)
            if str(request.get("session") or "") == identity.session_id
        ]
        items = [self._task_item(request) for request in requests]
        return self._page(identity, "tasks", items, cursor, limit)

    def task_detail(self, identity: TelegramIdentity, request_id: str) -> Dict[str, Any]:
        self._authorize(identity)
        request = self._require_session_request(
            identity, self.ledger.get_request(request_id)
        )
        return {
            "kind": "task",
            "session_id": identity.session_id,
            "item": self._task_item(request, detail=True),
        }

    def task_status(self, identity: TelegramIdentity, request_id: str) -> Dict[str, Any]:
        """Return a side-effect-free ledger health view; never sync or notify."""
        self._authorize(identity)
        request = self._require_session_request(
            identity, self.ledger.get_request(request_id)
        )
        payload = self.ledger.check_request(request_id)
        return {
            "kind": "coordinator_status",
            "session_id": identity.session_id,
            "status": self._safe_text(payload.get("status"), 80),
            "status_reason": self._safe_text(
                "; ".join(list(payload.get("issues") or []) + list(payload.get("warnings") or [])),
                500,
            ),
            "next_action": self._safe_text(payload.get("next_action"), 500),
            "request": self._task_item(request),
            "decision_status": {
                "pending_count": len(payload.get("decision_blockers") or []),
                "blocking_this_request": bool(payload.get("decision_blockers")),
                "blocking_decision_ids": [
                    self._safe_text(value, 160)
                    for value in (payload.get("decision_blockers") or [])[:20]
                ],
            },
        }

    def list_decisions(
        self, identity: TelegramIdentity, cursor: Optional[str] = None, limit: int = 10
    ) -> Dict[str, Any]:
        self._authorize(identity)
        session_request_ids = {
            request.get("id")
            for request in self.ledger.list_requests()
            if str(request.get("session") or "") == identity.session_id
        }
        decisions = []
        for decision in self.decisions.list_decisions():
            if decision.get("request_id") not in session_request_ids:
                continue
            decisions.append(
                {
                    "decision_id": self._safe_text(decision.get("decision_id"), 160),
                    "request_id": self._safe_text(decision.get("request_id"), 160),
                    "status": self._safe_text(decision.get("status"), 80),
                    "question": self._safe_text(decision.get("question"), 700),
                    "options": [
                        {
                            "id": self._safe_text(option.get("id"), 40),
                            "label": self._safe_text(option.get("label"), 300),
                            "tradeoffs": self._safe_text(option.get("tradeoffs"), 500),
                        }
                        for option in decision.get("options", [])[:10]
                    ],
                    "issue_url": self._safe_text(decision.get("issue_url"), 500),
                }
            )
        return self._page(identity, "decisions", decisions, cursor, limit)

    def submit_decision(
        self,
        identity: TelegramIdentity,
        decision_id: str,
        reply_text: str,
        update_id: str,
        callback_query_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Forward a button choice or unrestricted free text to DecisionManager.

        A stable Telegram update-derived comment id makes callback replay handling
        deterministic.  Callback acknowledgement remains the transport's job.
        """
        self._authorize(identity)
        if not str(reply_text or "").strip():
            raise ValueError("decision reply cannot be empty")
        if not str(update_id or "").strip():
            raise ValueError("Telegram update_id is required for replay safety")
        decision = self.decisions.get_decision(decision_id)
        request = self.ledger.get_request(decision.get("request_id"))
        self._require_session_request(identity, request)
        for dependency_id in decision.get("blocking_dependencies", []):
            dependency = self.ledger.get_request(dependency_id)
            self._require_session_request(identity, dependency)
        authorized_responders = list(decision.get("authorized_responders") or [])
        if len(authorized_responders) != 1:
            raise ValueError(
                "Telegram decisions require exactly one authorized responder until "
                "the shared ledger supports responder sets"
            )
        replay_key = f"telegram:{identity.chat_id}:{update_id}"
        result = self.decisions.process_reply(
            decision_id=decision_id,
            reply_text=reply_text,
            responder=identity.actor,
            comment_id=replay_key,
            comment_url=None,
            provenance=ProvenanceType.HUMAN_OPERATOR,
        )
        return {
            "kind": "decision_result",
            "session_id": identity.session_id,
            "decision_id": decision_id,
            "callback_query_id": self._safe_text(callback_query_id, 160),
            "status": self._safe_text(result.get("status"), 80),
            "interpretation": self._safe_text(result.get("interpretation"), 700),
            "rejection_reason": self._safe_text(result.get("rejection_reason"), 700),
            "idempotent_replay": bool(result.get("idempotent_replay")),
            "unblocked_requests": [self._safe_text(v, 160) for v in result.get("unblocked_requests", [])],
        }

    @staticmethod
    def capability_inventory() -> Dict[str, Any]:
        return {
            "stage_1_read_only": [
                "agent list/status/progress/result summary",
                "task list/detail/coordinator status",
                "session list with selected session and workspace label",
                "pending decisions and bounded pagination",
            ],
            "stage_2_authenticated_actions": [
                "inline option or unrestricted free-text decision reply",
                "verified actor/chat/session binding",
                "replay-safe update identity delegated to DecisionManager",
            ],
            "stage_3_native_controls": [
                "safe session creation through Veyyon native APIs",
                "steer/cancel controls through existing approval and native action contracts",
                "photo/document input and output through Telegram file transport",
            ],
            "excluded_ownership": [
                "polling/webhook supervision",
                "worker scheduling or lifecycle",
                "credential storage",
                "native Veyyon bridge implementation",
            ],
        }
