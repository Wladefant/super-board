#!/usr/bin/env python3
"""
workflows/telegram_notifier.py — Portable Telegram Workflow Status Notification Adapter

A harness-agnostic, pure Python standard library notification adapter for multi-agent workflows.
Consumes portable CoordinatorPacket events or direct status updates and dispatches strictly
deduped, rate-limited HTML status cards with a single canonical details link.

Invariants:
1. Canonical Authority: Status is always anchored to GitHub Issues / PRs and Superboard.
   Telegram is strictly an outbound notification transport, never a parallel system of record.
2. Filtered Event Classes: milestone, blocker, decision, completion ONLY.
   Routine tool execution, subagent traces, and search/read chatter are strictly rejected.
3. Message Format: Compact escaped HTML, expandable detail, and one canonical Details link.
4. No Credential Leakage: Tokens, keys, and local file paths are strictly redacted.
   Bot tokens are loaded into private memory only and never echoed, printed, or persisted to logs.
5. Deduplication & Cooldown:
   - SHA256 event signatures deduplicate identical messages within a 24-hour window.
   - Per-request cooldown prevents notification spamming during active multi-turn iteration.
   - Global rate limiter prevents flooding the transport.
6. Generic Multi-Repo Transport:
   - Per-project destination configuration maps repository/project names to Telegram slots.
   - Strict project affinity: with a configured slot pool, a project with no affinity
     match is refused rather than delivered through another project's bot.
7. Reply Correlation:
   - Every delivered message is indexed by (bot_id, chat_id, message_id) against the
     originating session and request in the shared bot_pool.db, so the session bridge
     can route a reply back to its owner and refuse an unknown or stale one.
   - The pool database resolves the same way the TypeScript bridge resolves it: an
     explicit path (constructor argument or --pool-db), else VEYYON_POOL_DB, else the
     installed pool at ~/.veyyon/telegram/bot_pool.db when that file exists. Outside an
     installed pool there is nothing to correlate against, so correlation stays off
     rather than creating a database no bridge reads.
8. Fail-Closed Resilience: Network or API failures fail safely with structured receipts
   without halting or disrupting the coordinator execution flow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
try:
    from ledger import FileLock
except ImportError:
    from workflows.ledger import FileLock

# Supported event classes
VALID_EVENT_TYPES = {"milestone", "blocker", "decision", "completion", "question", "status"}

# Default timing intervals (in seconds)
DEFAULT_DEDUP_WINDOW_SECONDS = 86400  # 24 hours
DEFAULT_COOLDOWN_SECONDS = 300       # 5 minutes per request
DEFAULT_GLOBAL_MIN_INTERVAL = 30     # 30 seconds between outgoing messages
DEFAULT_REMINDER_CADENCE_SECONDS = 900.0  # 15 minutes bounded recurring cadence
DEFAULT_HTTP_TIMEOUT = 10            # 10 seconds timeout for Telegram Bot API

# Default paths
DEFAULT_MANIFEST_PATHS = [
    Path(__file__).resolve().parent.parent / "telegram" / "manifest.json",
    Path(__file__).resolve().parent / "manifest.json",
    Path.home() / ".veyyon" / "workflows" / "telegram" / "manifest.json",
    Path.home() / ".veyyon" / "telegram" / "manifest.json",
]
DEFAULT_CHANNELS_BASE = Path.home() / ".claude" / "channels"
DEFAULT_POOL_DB_PATH = Path.home() / ".veyyon" / "telegram" / "bot_pool.db"

# Shared with the TypeScript session bridge (coordinator.ts). Both writers must keep
# this definition byte-identical so a reply can be resolved by either side.
MESSAGE_CORRELATIONS_DDL = """
CREATE TABLE IF NOT EXISTS message_correlations (
    bot_id       TEXT NOT NULL,
    chat_id      TEXT NOT NULL,
    message_id   INTEGER NOT NULL,
    slot_id      TEXT NOT NULL,
    session_id   TEXT NOT NULL,
    request_id   TEXT,
    decision_id  TEXT,
    project_path TEXT,
    created_at   REAL NOT NULL,
    PRIMARY KEY (bot_id, chat_id, message_id)
)
"""

# Shared with TypeScript session bridge (coordinator.ts) for interactive Telegram button callbacks.
DECISION_CALLBACKS_DDL = """
CREATE TABLE IF NOT EXISTS decision_callbacks (
    callback_token TEXT PRIMARY KEY,
    decision_id    TEXT NOT NULL,
    choice_id      TEXT NOT NULL,
    session_id     TEXT NOT NULL,
    chat_id        TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    question_hash  TEXT NOT NULL,
    expires_at     REAL NOT NULL,
    consumed_at    REAL,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decision_callbacks_dec_choice
    ON decision_callbacks (decision_id, choice_id);
CREATE INDEX IF NOT EXISTS idx_decision_callbacks_session
    ON decision_callbacks (session_id);
"""

# Values of VEYYON_POOL_DB that mean "no correlation", so a caller inside an installed
# pool can turn recording off without editing code.
POOL_DB_OFF_VALUES = {"", "0", "off", "none", "disabled", "false"}


def resolve_pool_db_path(
    explicit: Optional[Path] = None,
    *,
    default_path: Optional[Path] = DEFAULT_POOL_DB_PATH,
    env: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[Path], str]:
    """Resolves the shared bot_pool.db to record outbound correlations in.

    Returns the path and the name of whatever decided it:

    ``explicit``       a caller named the path (constructor argument, --pool-db, config)
    ``env``            VEYYON_POOL_DB named it
    ``env_disabled``   VEYYON_POOL_DB explicitly turned correlation off
    ``installed``      the installed pool exists at the default path
    ``absent``         no pool database anywhere, so correlation is off

    An explicit path is honoured even if the file does not exist yet: naming it is the
    caller's statement that this is the pool. The default is honoured only when the file
    already exists, because a bot_pool.db conjured somewhere no bridge reads would
    silently swallow every correlation and make replies look routable when they are not.
    """
    if explicit:
        return Path(explicit), "explicit"

    environ = os.environ if env is None else env
    raw = environ.get("VEYYON_POOL_DB")
    if raw is not None:
        if raw.strip().lower() in POOL_DB_OFF_VALUES:
            return None, "env_disabled"
        return Path(raw.strip()), "env"

    if default_path is not None and Path(default_path).exists():
        return Path(default_path), "installed"

    return None, "absent"


@dataclass
class NotificationEvent:
    event_type: str
    project: str
    request_id: str
    summary: str
    canonical_link: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    session_id: Optional[str] = None
    slot_id: Optional[str] = None

    def validate(self) -> None:
        if self.event_type not in VALID_EVENT_TYPES:
            raise ValueError(
                f"Invalid event_type '{self.event_type}'. Must be one of: {sorted(VALID_EVENT_TYPES)}"
            )
        if not self.project:
            raise ValueError("project is required")
        if not self.summary:
            raise ValueError("summary is required")
        if not self.canonical_link:
            raise ValueError("canonical_link is required")


@dataclass
class DeliveryReceipt:
    delivered: bool
    status: str  # sent, deduped, cooldown, suppressed, blocked, dry_run, failed
    reason: str
    event_signature: str = ""
    message_id: Optional[int] = None
    chat_id: Optional[str] = None
    bot_id: Optional[str] = None
    timestamp_utc: float = field(default_factory=time.time)
    slot_id: Optional[str] = None
    session_id: Optional[str] = None
    correlation_status: str = "not_attempted"
    correlation_recorded: bool = False
    # Which rule decided the pool database: explicit, env, env_disabled, installed,
    # absent. Without it a "disabled" status is indistinguishable from a misconfigured
    # one at the caller.
    correlation_source: str = "absent"
    reply_markup: Optional[Dict[str, Any]] = None


class SecretSanitizer:
    """Sanitizes text to prevent accidental exposure of tokens, credentials, and paths."""

    _PATTERNS = [
        (re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}", re.IGNORECASE), "[REDACTED_BOT_TOKEN]"),
        (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"), "[REDACTED_GH_TOKEN]"),
        (re.compile(r"\bsbp_[A-Za-z0-9_-]{20,}\b"), "[REDACTED_SUPABASE_TOKEN]"),
        (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{15,}\b", re.IGNORECASE), "Bearer [REDACTED]"),
        (re.compile(r"(?:TELEGRAM_BOT_TOKEN\s*=\s*)[^\s\n]+", re.IGNORECASE), "TELEGRAM_BOT_TOKEN=[REDACTED]"),
        (re.compile(r"(?:password|secret|key|token)\s*[:=]\s*['\"][^'\"]+['\"]", re.IGNORECASE), "[REDACTED_SECRET]"),
        (re.compile(r"\b1247617658\b"), "[REDACTED_DESTINATION]"),
        (re.compile(r"(?:chat_id|destination|chat)\s*[:=]\s*['\"]?\d{8,}['\"]?", re.IGNORECASE), "chat_id=[REDACTED_DESTINATION]"),
        (re.compile(r"[A-Za-z]:\\[Uu]sers\\[^\\]+\\", re.IGNORECASE), r"C:\\Users\\<user>\\"),
        (re.compile(r"/home/[^/]+/", re.IGNORECASE), "/home/<user>/"),
    ]

    @classmethod
    def sanitize(cls, text: str) -> str:
        if not text:
            return ""
        result = text
        for pattern, replacement in cls._PATTERNS:
            result = pattern.sub(replacement, result)
        return result


class ProjectSlotResolver:
    """Resolves multi-repo project identifiers to destination Telegram slots and credentials."""

    def __init__(
        self,
        manifest_path: Optional[Path] = None,
        channels_dir: Optional[Path] = None,
    ):
        self.channels_dir = channels_dir or DEFAULT_CHANNELS_BASE
        self.manifest_path = manifest_path or self._locate_manifest()
        self._manifest_cache: Optional[Dict[str, Any]] = None

    def _locate_manifest(self) -> Optional[Path]:
        for candidate in DEFAULT_MANIFEST_PATHS:
            if candidate.exists():
                return candidate
        return None

    def _load_manifest(self) -> Dict[str, Any]:
        if self._manifest_cache is not None:
            return self._manifest_cache
        if self.manifest_path and self.manifest_path.exists():
            try:
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                self._manifest_cache = data
                return data
            except Exception:
                pass
        self._manifest_cache = {"slots": []}
        return self._manifest_cache

    def resolve_slot(
        self,
        project_or_repo: str,
        explicit_slot: Optional[str] = None,
        explicit_state_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolves project or repo to slot metadata: slotId, stateDir, preferredProjects."""
        if explicit_slot:
            state_dir = Path(explicit_state_dir) if explicit_state_dir else (self.channels_dir / explicit_slot)
            return {
                "slotId": explicit_slot,
                "stateDir": str(state_dir.resolve()),
                "source": "explicit",
            }

        # Normalize project string
        clean_proj = project_or_repo.strip().lower()
        if "/" in clean_proj:
            repo_name = clean_proj.split("/")[-1]
        else:
            repo_name = clean_proj

        manifest = self._load_manifest()
        slots = manifest.get("slots", [])

        # 1. Exact or preferred project match
        for s in slots:
            if not s.get("enabled", True):
                continue
            preferred = [p.lower() for p in s.get("preferredProjects", [])]
            if clean_proj in preferred or repo_name in preferred:
                return {
                    "slotId": s.get("slotId"),
                    "stateDir": s.get("stateDir"),
                    "source": "manifest_affinity",
                }

        # 2. Substring match
        for s in slots:
            if not s.get("enabled", True):
                continue
            preferred = [p.lower() for p in s.get("preferredProjects", [])]
            for p in preferred:
                if p in repo_name or repo_name in p:
                    return {
                        "slotId": s.get("slotId"),
                        "stateDir": s.get("stateDir"),
                        "source": "manifest_substring",
                    }

        # 3. Strict project affinity: with a configured pool, refuse rather than
        #    delivering this project's notification through another project's bot.
        if slots:
            return {
                "slotId": None,
                "stateDir": None,
                "source": "unresolved",
            }

        # 4. Fallback if manifest is missing
        default_dir = self.channels_dir / "telegram-polysim"
        return {
            "slotId": "telegram-polysim",
            "stateDir": str(default_dir),
            "source": "filesystem_fallback",
        }

    def load_token(self, state_dir: Path) -> Optional[str]:
        """Load Telegram bot token strictly into memory from state_dir/.env or environment.
        Never prints, logs, or persists the token.
        """
        env_file = state_dir / ".env"
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line.startswith("TELEGRAM_BOT_TOKEN="):
                        val = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if val:
                            return val
            except Exception:
                pass
        return os.environ.get("TELEGRAM_BOT_TOKEN")

    def load_allowed_destinations(self, state_dir: Path) -> List[str]:
        """Load verified owner chat destination IDs from access.json or environment."""
        explicit_chat = os.environ.get("TELEGRAM_NOTIFY_CHAT_ID")
        if explicit_chat:
            return [explicit_chat.strip()]

        access_file = state_dir / "access.json"
        if access_file.exists():
            try:
                data = json.loads(access_file.read_text(encoding="utf-8"))
                allowed = [str(x).strip() for x in data.get("allowFrom", []) if x]
                if allowed:
                    return allowed
            except Exception:
                pass

        # Check veyyon_chat_sessions.json as second fallback
        sessions_file = state_dir / "veyyon_chat_sessions.json"
        if sessions_file.exists():
            try:
                data = json.loads(sessions_file.read_text(encoding="utf-8"))
                return [str(k).strip() for k in data.keys() if k]
            except Exception:
                pass

        return []


class DeduplicationLedger:
    """Manages event deduplication, cooldowns, and dispatch history."""

    def __init__(
        self,
        state_file: Path,
        dedup_window: float = DEFAULT_DEDUP_WINDOW_SECONDS,
        cooldown_window: float = DEFAULT_COOLDOWN_SECONDS,
        min_global_interval: float = DEFAULT_GLOBAL_MIN_INTERVAL,
    ):
        self.state_file = state_file
        self.dedup_window = dedup_window
        self.cooldown_window = cooldown_window
        self.min_global_interval = min_global_interval

    def _load(self) -> Dict[str, Any]:
        if not self.state_file.exists():
            return {
                "version": 1,
                "last_dispatched_at": 0.0,
                "sent_signatures": {},    # sig -> timestamp
                "request_cooldowns": {},  # req_id -> timestamp
            }
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {
                "version": 1,
                "last_dispatched_at": 0.0,
                "sent_signatures": {},
                "request_cooldowns": {},
            }

    def _save(self, data: Dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_file.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp_path.replace(self.state_file)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    @classmethod
    def compute_signature(cls, event: NotificationEvent) -> str:
        norm_summary = " ".join(event.summary.strip().lower().split())
        raw = f"{event.event_type}:{event.project}:{event.request_id}:{norm_summary}:{event.canonical_link.strip()}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def check_eligible(self, event: NotificationEvent, now: Optional[float] = None) -> Tuple[bool, str, str]:
        """Checks if event is eligible for dispatch under dedup, cooldown, and rate limits.
        Returns: (eligible, status, reason)
        """
        now = now or time.time()
        data = self._load()
        sig = self.compute_signature(event)
        # 1. Exact signature deduplication or deliberate due reminder cadence check
        signatures = data.get("sent_signatures", {})
        last_sent = signatures.get(sig)
        if event.metadata.get("is_due_reminder"):
            cadence = float(event.metadata.get("cadence_seconds") or DEFAULT_REMINDER_CADENCE_SECONDS)
            topic = event.metadata.get("decision_id") or event.request_id
            if last_sent and (now - last_sent < cadence):
                elapsed = int(now - last_sent)
                rem = int(cadence - elapsed)
                return False, "not_due", f"Reminder for topic '{topic}' is not due yet ({rem}s remaining before next reminder)"
            # Deliberate due reminder has satisfied its cadence window; bypasses 24h dedup
        else:
            if last_sent and (now - last_sent < self.dedup_window):
                elapsed = int(now - last_sent)
                return False, "deduped", f"Identical event signature dispatched {elapsed}s ago (within {int(self.dedup_window)}s window)"

        # 2. Per-request cooldown (unless blocker, decision, or question)
        if event.event_type not in ("blocker", "decision", "question"):
            req_cooldowns = data.get("request_cooldowns", {})
            last_req_time = req_cooldowns.get(event.request_id)
            if last_req_time and (now - last_req_time < self.cooldown_window):
                elapsed = int(now - last_req_time)
                rem = int(self.cooldown_window - elapsed)
                return False, "cooldown", f"Request '{event.request_id}' is in cooldown ({rem}s remaining)"

        # 3. Global rate limiter (deliberate due reminders can bypass rate limit)
        if not event.metadata.get("is_due_reminder"):
            last_global = data.get("last_dispatched_at", 0.0)
            if last_global and (now - last_global < self.min_global_interval):
                elapsed = int(now - last_global)
                rem = int(self.min_global_interval - elapsed)
                return False, "suppressed", f"Global rate limit active ({rem}s remaining before next dispatch)"
        return True, "ready", "Eligible for dispatch"

    def record_dispatch(self, event: NotificationEvent, now: Optional[float] = None) -> str:
        now = now or time.time()
        data = self._load()
        sig = self.compute_signature(event)

        # Prune old signatures
        signatures = data.setdefault("sent_signatures", {})
        cutoff = now - self.dedup_window
        data["sent_signatures"] = {k: v for k, v in signatures.items() if v >= cutoff}

        data["sent_signatures"][sig] = now
        data.setdefault("request_cooldowns", {})[event.request_id] = now
        data["last_dispatched_at"] = now

        self._save(data)
        return sig


class OutboundCorrelationStore:
    """Shared (bot_id, chat_id, message_id) -> (session_id, request_id) index.

    Lives in the same bot_pool.db the TypeScript session bridge reads when deciding
    whether an inbound Telegram reply may be delivered. A reply whose target message
    has no correlation row, or whose row names a different session, is refused by the
    bridge, so recording here is what makes a reply to a workflow notification routable
    back to the session that owns the request.

    Only the originating session of a request may be recorded here. The session that
    happens to hold the bot lease is never substituted: it may own nothing related to
    this request, and binding to it would route someone else's reply into it.

    Resolution order is `resolve_pool_db_path`: an explicit path wins, then
    VEYYON_POOL_DB, then the installed pool when it exists. `source` records which of
    those answered, so a receipt can say why correlation is on or off.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        default_path: Optional[Path] = DEFAULT_POOL_DB_PATH,
    ):
        self.db_path, self.source = resolve_pool_db_path(db_path, default_path=default_path)

    @property
    def enabled(self) -> bool:
        return self.db_path is not None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def record(
        self,
        bot_id: str,
        chat_id: str,
        message_id: int,
        slot_id: str,
        session_id: str,
        request_id: Optional[str] = None,
        decision_id: Optional[str] = None,
        project_path: Optional[str] = None,
        now: Optional[float] = None,
    ) -> bool:
        if not self.enabled or not bot_id or not chat_id or message_id is None or not session_id:
            return False
        now = now if now is not None else time.time()
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as conn:
                with conn:
                    conn.execute(MESSAGE_CORRELATIONS_DDL)
                    conn.execute(
                        "INSERT INTO message_correlations ("
                        "bot_id, chat_id, message_id, slot_id, session_id, "
                        "request_id, decision_id, project_path, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(bot_id, chat_id, message_id) DO UPDATE SET "
                        "slot_id = excluded.slot_id, session_id = excluded.session_id, "
                        "request_id = excluded.request_id, decision_id = excluded.decision_id, "
                        "project_path = excluded.project_path, created_at = excluded.created_at",
                        (
                            str(bot_id),
                            str(chat_id),
                            int(message_id),
                            str(slot_id),
                            str(session_id),
                            request_id,
                            decision_id,
                            project_path,
                            float(now),
                        ),
                    )
            return True
        except (sqlite3.Error, OSError):
            return False

    def lookup(self, bot_id: str, chat_id: str, message_id: int) -> Optional[Dict[str, Any]]:
        if not self.enabled or not self.db_path.exists():
            return None
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT bot_id, chat_id, message_id, slot_id, session_id, request_id, "
                    "decision_id, project_path, created_at FROM message_correlations "
                    "WHERE bot_id = ? AND chat_id = ? AND message_id = ?",
                    (str(bot_id), str(chat_id), int(message_id)),
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        keys = (
            "bot_id",
            "chat_id",
            "message_id",
            "slot_id",
            "session_id",
            "request_id",
            "decision_id",
            "project_path",
            "created_at",
        )
        return dict(zip(keys, row))


class DecisionCallbackStore:
    """Shared decision callback store in bot_pool.db.
    Registers and tracks bounded opaque callback tokens for interactive Telegram buttons.
    """

    def __init__(
        self,
        db_path: Optional[Path] = None,
        default_path: Optional[Path] = DEFAULT_POOL_DB_PATH,
    ):
        self.db_path, self.source = resolve_pool_db_path(db_path, default_path=default_path)

    @property
    def enabled(self) -> bool:
        return self.db_path is not None

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def create_callback(
        self,
        decision_id: str,
        choice_id: str,
        session_id: str,
        chat_id: str,
        user_id: str,
        question_text: str,
        ttl_seconds: float = 86400.0,
        now: Optional[float] = None,
    ) -> Optional[str]:
        if not self.enabled or not decision_id or not choice_id or not session_id or not chat_id or not user_id:
            return None
        now_ts = now if now is not None else time.time()
        expires_at = now_ts + float(ttl_seconds)
        q_hash = hashlib.sha256(question_text.encode("utf-8")).hexdigest()
        token = f"cb:d_{uuid.uuid4().hex}"
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as conn:
                with conn:
                    conn.executescript(DECISION_CALLBACKS_DDL)
                    conn.execute(
                        "INSERT INTO decision_callbacks ("
                        "callback_token, decision_id, choice_id, session_id, "
                        "chat_id, user_id, question_hash, expires_at, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            token,
                            str(decision_id),
                            str(choice_id),
                            str(session_id),
                            str(chat_id),
                            str(user_id),
                            q_hash,
                            float(expires_at),
                            float(now_ts),
                        ),
                    )
            return token
        except (sqlite3.Error, OSError):
            return None

    def lookup(self, callback_token: str) -> Optional[Dict[str, Any]]:
        if not self.enabled or not self.db_path.exists():
            return None
        try:
            with closing(self._connect()) as conn:
                row = conn.execute(
                    "SELECT callback_token, decision_id, choice_id, session_id, "
                    "chat_id, user_id, question_hash, expires_at, consumed_at, created_at "
                    "FROM decision_callbacks WHERE callback_token = ?",
                    (str(callback_token),),
                ).fetchone()
        except sqlite3.Error:
            return None
        if not row:
            return None
        keys = (
            "callback_token",
            "decision_id",
            "choice_id",
            "session_id",
            "chat_id",
            "user_id",
            "question_hash",
            "expires_at",
            "consumed_at",
            "created_at",
        )
        return dict(zip(keys, row))

    def consume(self, callback_token: str, now: Optional[float] = None) -> bool:
        if not self.enabled or not self.db_path.exists():
            return False
        now_ts = now if now is not None else time.time()
        try:
            with closing(self._connect()) as conn:
                with conn:
                    res = conn.execute(
                        "UPDATE decision_callbacks SET consumed_at = ? "
                        "WHERE callback_token = ? AND consumed_at IS NULL",
                        (float(now_ts), str(callback_token)),
                    )
                    return res.rowcount > 0
        except sqlite3.Error:
            return False


def escape_html(text: str) -> str:
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def format_decision_presentation(
    problem: str,
    proposed_action: str,
    consequence_or_risk: str,
    details_url: Optional[str] = None,
    options: Optional[List[Any]] = None,
) -> str:
    """Formats a decision for Telegram in strict plain language.
    Guarantees:
    - Short plain-language problem
    - Concrete proposed action
    - Consequence / risk
    - Clearly labeled buttons / options summary
    - At most ONE optional details link
    - No request IDs, raw paths, or multiple URLs
    """
    clean_problem = SecretSanitizer.sanitize(str(problem or "").strip())
    clean_action = SecretSanitizer.sanitize(str(proposed_action or "").strip())
    clean_risk = SecretSanitizer.sanitize(str(consequence_or_risk or "").strip())

    lines = [
        f"❓ <b>Problem:</b> {escape_html(clean_problem)}",
        "",
        f"👉 <b>Proposed Action:</b> {escape_html(clean_action)}",
        "",
        f"⚠️ <b>Risk / Consequence:</b> {escape_html(clean_risk)}",
    ]

    if options:
        lines.append("")
        lines.append("<b>Choices:</b>")
        for opt in options:
            if isinstance(opt, dict):
                opt_id = opt.get("id", "")
                opt_label = opt.get("label") or opt.get("description", "")
                lines.append(f"• <b>Option {escape_html(opt_id)}</b>: {escape_html(opt_label)}")
            else:
                lines.append(f"• {escape_html(str(opt))}")

    if details_url:
        first_url = str(details_url).strip().split()[0]
        if first_url.startswith(("http://", "https://")):
            lines.append("")
            lines.append(f'🔗 <a href="{escape_html(first_url)}">View Details on GitHub</a>')

    return "\n".join(lines)


def build_decision_inline_keyboard(
    decision_id: str,
    options: List[Any],
    session_id: str,
    chat_id: str,
    user_id: str,
    question_text: str,
    callback_store: Optional[DecisionCallbackStore],
    ttl_seconds: float = 86400.0,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    if not callback_store or not callback_store.enabled or not options:
        return None
    buttons = []
    for opt in options:
        if isinstance(opt, dict):
            opt_id = str(opt.get("id", ""))
            opt_label = str(opt.get("label") or opt_id)
        else:
            opt_id = str(opt)
            opt_label = str(opt)
        token = callback_store.create_callback(
            decision_id=decision_id,
            choice_id=opt_id,
            session_id=session_id,
            chat_id=chat_id,
            user_id=user_id,
            question_text=question_text,
            ttl_seconds=ttl_seconds,
            now=now,
        )
        if token:
            btn_text = f"{opt_id}: {opt_label}" if opt_id and opt_id != opt_label else opt_label
            btn_text = btn_text[:40]
            buttons.append({"text": btn_text, "callback_data": token})
    if buttons:
        return {"inline_keyboard": [buttons]}
    return None

class TelegramNotificationAdapter:
    """Portable Telegram notification adapter for multi-agent workflows."""

    def __init__(
        self,
        resolver: Optional[ProjectSlotResolver] = None,
        ledger: Optional[DeduplicationLedger] = None,
        state_dir_override: Optional[Path] = None,
        correlation_store: Optional[OutboundCorrelationStore] = None,
        callback_store: Optional[DecisionCallbackStore] = None,
    ):
        self.resolver = resolver or ProjectSlotResolver()
        self.correlation_store = correlation_store or OutboundCorrelationStore()
        self.callback_store = callback_store or DecisionCallbackStore(db_path=self.correlation_store.db_path)
        if ledger:
            self.ledger = ledger
        else:
            state_file = (state_dir_override or Path.cwd()) / "telegram_notify_state.json"
            self.ledger = DeduplicationLedger(state_file)

    @classmethod
    def format_message(cls, event: NotificationEvent, plain_language: bool = False) -> str:
        """Render one quiet, escaped Telegram HTML card with one details link."""
        event.validate()

        presentation = {
            "milestone": ("🚀", "Milestone reached"),
            "blocker": ("🛑", "Blocked"),
            "decision": ("❓", "Decision needed"),
            "question": ("❓", "Question"),
            "status": ("📊", "Status update"),
            "completion": ("✅", "Completed"),
        }
        emoji, title = presentation.get(event.event_type, ("ℹ️", event.event_type.capitalize()))
        project = SecretSanitizer.sanitize(str(event.project).strip())
        summary = SecretSanitizer.sanitize(str(event.summary).strip())
        detail = SecretSanitizer.sanitize(
            str(event.metadata.get("long_detail") or event.metadata.get("detail") or "").strip()
        )

        lines = [
            f"{emoji} <b>{escape_html(title)}</b>",
            f"<i>{escape_html(project)}</i>",
            "",
        ]

        if event.event_type in ("decision", "question") and plain_language:
            problem = SecretSanitizer.sanitize(
                str(event.metadata.get("problem") or summary).strip()
            )
            action = SecretSanitizer.sanitize(
                str(
                    event.metadata.get("proposed_action")
                    or "Choose one option below, or reply with guidance."
                ).strip()
            )
            risk = SecretSanitizer.sanitize(
                str(
                    event.metadata.get("consequence_or_risk")
                    or "Dependent work remains paused until this is answered."
                ).strip()
            )
            lines.extend(
                [
                    f"• <b>What:</b> {escape_html(problem)}",
                    f"• <b>Action:</b> {escape_html(action)}",
                    f"• <b>Impact:</b> {escape_html(risk)}",
                ]
            )
        elif len(summary) > 280 or "\n" in summary:
            lines.append("• Full update below.")
            detail = "\n\n".join(part for part in (summary, detail) if part)
        else:
            lines.append(f"• {escape_html(summary)}")

        options = event.metadata.get("options")
        if event.event_type in ("decision", "question") and isinstance(options, list):
            for option in options:
                if isinstance(option, dict):
                    option_id = SecretSanitizer.sanitize(str(option.get("id") or "").strip())
                    option_label = SecretSanitizer.sanitize(
                        str(option.get("label") or option.get("description") or option_id).strip()
                    )
                    prefix = f"{option_id}: " if option_id and option_id != option_label else ""
                    lines.append(f"• <b>{escape_html(prefix + option_label)}</b>")

        if event.event_type in ("decision", "question"):
            lines.append("Reply with guidance at any time; free text does not approve an action.")

        if detail:
            lines.extend(["", f"<blockquote expandable>{escape_html(detail)}</blockquote>"])

        details_url = str(
            event.metadata.get("details_url") or event.canonical_link or ""
        ).strip().split()[0]
        if details_url.startswith(("http://", "https://")):
            lines.extend(["", f'<a href="{escape_html(details_url)}">Details</a>'])

        return "\n".join(lines)

    def test_connection(self, project: str = "polysimulator", slot_id: Optional[str] = None) -> Dict[str, Any]:
        """Read-only test to verify bot credentials and API reachability via getMe."""
        slot_info = self.resolver.resolve_slot(project, explicit_slot=slot_id)
        if slot_info.get("source") == "unresolved":
            return {
                "ok": False,
                "status": "blocked",
                "reason": (
                    f"No Telegram bot slot declares affinity for project '{project}'; "
                    "refusing to test another project's bot."
                ),
                "slot": None,
            }
        state_dir = Path(slot_info["stateDir"])
        token = self.resolver.load_token(state_dir)
        destinations = self.resolver.load_allowed_destinations(state_dir)

        if not token:
            return {
                "ok": False,
                "status": "blocked",
                "reason": f"No Telegram bot token found in {state_dir / '.env'} or environment variable",
                "slot": slot_info["slotId"],
                "state_dir": str(state_dir),
                "destinations": destinations,
            }

        url = f"https://api.telegram.org/bot{token}/getMe"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Veyyon-Coordinator/1.0"})
            with urllib.request.urlopen(req, timeout=DEFAULT_HTTP_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                result = data.get("result", {})
                return {
                    "ok": True,
                    "status": "connected",
                    "slot": slot_info["slotId"],
                    "state_dir": str(state_dir),
                    "bot_id": str(result.get("id")),
                    "bot_username": result.get("username"),
                    "bot_name": result.get("first_name"),
                    "configured_destinations": ["[CONFIGURED_DESTINATION]" for _ in destinations],
                }
        except urllib.error.HTTPError as e:
            return {
                "ok": False,
                "status": "http_error",
                "code": e.code,
                "reason": f"Telegram HTTP error {e.code}: {e.reason}",
                "slot": slot_info["slotId"],
            }
        except Exception as e:
            return {
                "ok": False,
                "status": "connection_error",
                "reason": f"Connection failed: {type(e).__name__}: {e}",
                "slot": slot_info["slotId"],
            }

    def notify(
        self,
        event: NotificationEvent,
        dry_run: bool = False,
        force: bool = False,
        explicit_slot: Optional[str] = None,
        explicit_chat_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> DeliveryReceipt:
        """Evaluates deduplication, formats message, and sends to verified owner destination."""
        event.validate()
        sig = DeduplicationLedger.compute_signature(event)

        # 1. Resolve project destination slot and credentials
        slot_info = self.resolver.resolve_slot(event.project, explicit_slot=explicit_slot)
        if slot_info.get("source") == "unresolved":
            return DeliveryReceipt(
                delivered=False,
                status="blocked",
                reason=(
                    f"No Telegram bot slot declares affinity for project '{event.project}'; "
                    "refusing to deliver through another project's bot."
                ),
                event_signature=sig,
            )

        state_dir = Path(slot_info["stateDir"])
        token = self.resolver.load_token(state_dir)
        allowed_chats = self.resolver.load_allowed_destinations(state_dir)

        target_chat = explicit_chat_id
        if not target_chat:
            if not allowed_chats:
                return DeliveryReceipt(
                    delivered=False,
                    status="blocked",
                    reason=f"No authorized destination chat ID configured for slot '{slot_info['slotId']}' (checked access.json)",
                    event_signature=sig,
                )
            target_chat = allowed_chats[0]

        # Verify chat is allowlisted
        if allowed_chats and str(target_chat) not in [str(c) for c in allowed_chats]:
            return DeliveryReceipt(
                delivered=False,
                status="blocked",
                reason=f"Target destination is not in allowlist for slot '{slot_info['slotId']}'",
                event_signature=sig,
                chat_id="[REDACTED_DESTINATION]",
            )

        if not token:
            return DeliveryReceipt(
                delivered=False,
                status="blocked",
                reason=f"Telegram bot token missing for slot '{slot_info['slotId']}' in {state_dir}",
                event_signature=sig,
                chat_id="[REDACTED_DESTINATION]",
            )

        # 2. Check deduplication & cooldown unless forced
        if not force:
            eligible, status, reason = self.ledger.check_eligible(event, now=now)
            if not eligible:
                return DeliveryReceipt(
                    delivered=False,
                    status=status,
                    reason=reason,
                    event_signature=sig,
                    chat_id="[REDACTED_DESTINATION]",
                )

        # A reply to this message must reach the session that OWNS the request, so the
        # binding comes from the event's originating session and nowhere else. It is
        # deliberately never taken from whichever session currently holds the bot lease:
        # that session may be unrelated to this request, and binding to it would hand it
        # someone else's reply. With no originating identity the message stays
        # uncorrelated and the session bridge refuses any reply to it.
        bound_session = event.session_id or None

        # 3. Format message
        message_text = self.format_message(
            event,
            plain_language=event.event_type in ("decision", "question"),
        )

        # Questions and decisions share the existing actor/chat/session-bound
        # callback store. Free-text replies remain guidance, never approval.
        reply_markup = None
        if event.event_type in ("decision", "question") and event.metadata.get("options"):
            reply_markup = build_decision_inline_keyboard(
                decision_id=str(event.metadata.get("decision_id") or event.request_id or ""),
                options=event.metadata.get("options") or [],
                session_id=bound_session or "unbound",
                chat_id=str(target_chat),
                user_id=str(target_chat),
                question_text=event.summary,
                callback_store=self.callback_store,
                ttl_seconds=86400.0,
                now=now,
            )

        # 4. Dry-run gate
        if dry_run:
            return DeliveryReceipt(
                delivered=True,
                status="dry_run",
                reason=f"[DRY-RUN] Would send message to slot '{slot_info['slotId']}': {message_text}",
                event_signature=sig,
                chat_id="[REDACTED_DESTINATION]",
                slot_id=slot_info["slotId"],
                session_id=bound_session,
                correlation_status="dry_run",
                correlation_source=self.correlation_store.source,
                reply_markup=reply_markup,
            )

        # 5. Network dispatch
        api_url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": str(target_chat),
            "text": message_text,
            "disable_web_page_preview": False,
        }
        payload["parse_mode"] = "HTML"
        if reply_markup:
            payload["reply_markup"] = reply_markup
        data_bytes = json.dumps(payload).encode("utf-8")

        try:
            req = urllib.request.Request(
                api_url,
                data=data_bytes,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Veyyon-Coordinator/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=DEFAULT_HTTP_TIMEOUT) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                if resp_data.get("ok"):
                    result = resp_data.get("result", {})
                    msg_id = result.get("message_id")
                    bot_id = str(result.get("from", {}).get("id", ""))
                    # The chat the API actually delivered to. `target_chat` may be a
                    # channel name from the allowlist, and the session bridge looks a
                    # reply up by the numeric chat id Telegram reports, so keying on the
                    # requested destination would produce a row no reply can ever match.
                    delivered_chat_id = str(result.get("chat", {}).get("id", target_chat))
                    self.ledger.record_dispatch(event, now=now)

                    correlation_status = "disabled" if not self.correlation_store.enabled else "unbound"
                    correlation_recorded = False
                    if self.correlation_store.enabled and bound_session and msg_id is not None:
                        correlation_recorded = self.correlation_store.record(
                            bot_id=bot_id,
                            chat_id=delivered_chat_id,
                            message_id=int(msg_id),
                            slot_id=str(slot_info["slotId"]),
                            session_id=bound_session,
                            request_id=event.request_id or None,
                            decision_id=event.metadata.get("decision_id"),
                            project_path=event.project or None,
                        )
                        correlation_status = "recorded" if correlation_recorded else "record_failed"

                    return DeliveryReceipt(
                        delivered=True,
                        status="sent",
                        reason=f"Notification delivered to channel slot '{slot_info['slotId']}' (message_id: {msg_id})",
                        event_signature=sig,
                        message_id=msg_id,
                        chat_id="[REDACTED_DESTINATION]",
                        bot_id=bot_id,
                        slot_id=slot_info["slotId"],
                        session_id=bound_session,
                        correlation_status=correlation_status,
                        correlation_recorded=correlation_recorded,
                        correlation_source=self.correlation_store.source,
                        reply_markup=reply_markup,
                    )
                else:
                    return DeliveryReceipt(
                        delivered=False,
                        status="failed",
                        reason=f"Telegram API error: {resp_data.get('description', 'Unknown error')}",
                        event_signature=sig,
                        chat_id="[REDACTED_DESTINATION]",
                    )
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode("utf-8")
                err_json = json.loads(err_body)
                desc = err_json.get("description", e.reason)
            except Exception:
                desc = e.reason
            return DeliveryReceipt(
                delivered=False,
                status="failed",
                reason=f"HTTP {e.code} delivery failure: {desc}",
                event_signature=sig,
                chat_id="[REDACTED_DESTINATION]",
            )
        except Exception as e:
            return DeliveryReceipt(
                delivered=False,
                status="failed",
                reason=f"Network delivery exception: {type(e).__name__}: {e}",
                event_signature=sig,
                chat_id="[REDACTED_DESTINATION]",
            )

    @classmethod
    def from_coordinator_packet(cls, packet: Dict[str, Any], project_override: Optional[str] = None) -> Optional[NotificationEvent]:
        """Translates a portable CoordinatorPacket dictionary into a NotificationEvent.
        Returns None if packet is routine chatter that should not emit a notification.
        """
        request = packet.get("request") or {}
        req_id = request.get("id") or "req-unknown"
        req_state = request.get("state") or "unknown"
        issue_url = request.get("issue_url") or "https://github.com/Bavariance/polysimulator"
        # Carried through so a reply to this notification resolves to the session that
        # owns the request rather than to whichever session holds the bot lease later.
        session_id = request.get("session") or request.get("session_id") or None
        project = project_override or packet.get("boundaries", {}).get("shared_authority", "Bavariance/polysimulator")
        if "Bavariance/polysimulator" in project:
            project = "Bavariance/polysimulator"

        # 1. Decision needed
        decision_status = packet.get("decision_status") or {}
        if decision_status.get("blocking_this_request"):
            decision_ids = decision_status.get("blocking_decision_ids", [])
            dec_str = ", ".join(decision_ids) if decision_ids else "pending decision"
            return NotificationEvent(
                event_type="decision",
                project=project,
                request_id=req_id,
                summary=f"Request paused awaiting human authorization on {dec_str}",
                canonical_link=issue_url,
                metadata={"decision_ids": decision_ids},
                session_id=session_id,
            )

        # 2. Blocker
        status = packet.get("status")
        preflight = packet.get("preflight") or {}
        if status == "blocked" or preflight.get("status") == "blocked":
            blockers = preflight.get("blockers") or []
            reason = "; ".join(blockers) if blockers else packet.get("status_reason", "preflight or dependency blocked")
            return NotificationEvent(
                event_type="blocker",
                project=project,
                request_id=req_id,
                summary=f"Request blocked by {reason}",
                canonical_link=issue_url,
                metadata={"blockers": blockers},
                session_id=session_id,
            )

        # 3. Completion
        if status in ("completed", "done") or req_state == "done":
            return NotificationEvent(
                event_type="completion",
                project=project,
                request_id=req_id,
                summary=f"Request completed and verified against all criteria",
                canonical_link=issue_url,
                session_id=session_id,
            )

        # 4. Milestone (advancement to review or integration)
        if req_state in ("review", "integration"):
            return NotificationEvent(
                event_type="milestone",
                project=project,
                request_id=req_id,
                summary=f"Request advanced to state '{req_state}' with verified criteria",
                canonical_link=issue_url,
                metadata={"state": req_state},
                session_id=session_id,
            )

        # Routine chatter (implementation, discovery, etc.) is dropped
        return None

    @classmethod
    def lookup_ledger_request(
        cls,
        req_id: str,
        ledger: Optional[Any] = None,
    ) -> Optional[Dict[str, Any]]:
        """Look up request state in ledger object or candidate ledger.json files."""
        if not req_id:
            return None
        if ledger and hasattr(ledger, "get_request"):
            try:
                return ledger.get_request(req_id)
            except Exception:
                pass
        # Candidate ledger files
        candidate_files = [
            Path.cwd() / "ledger.json",
            Path.cwd() / "request_ledger.json",
            Path(__file__).resolve().parent / "ledger.json",
            Path.home() / ".veyyon" / "workflows" / "ledger.json",
        ]
        for cpath in candidate_files:
            if cpath.exists():
                try:
                    data = json.loads(cpath.read_text(encoding="utf-8"))
                    requests = data.get("requests", {})
                    if isinstance(requests, dict) and req_id in requests:
                        return requests[req_id]
                    elif isinstance(requests, list):
                        for r in requests:
                            if isinstance(r, dict) and r.get("id") == req_id:
                                return r
                except Exception:
                    pass
        return None

    @classmethod
    def is_decision_notifiable(
        cls,
        decision: Any,
        ledger_request: Optional[Dict[str, Any]] = None,
        ledger: Optional[Any] = None,
        allow_legacy_unanswered: bool = False,
    ) -> Tuple[bool, str]:
        """Validates that a decision contract represents a genuine, active, non-synthetic,
        uncompleted decision awaiting human operator action.
        Uses typed decision status, explicit synthetic provenance/flags, and actual ledger
        request state. Substring checks on prompt/IDs are strictly avoided to allow legitimate
        questions about demo features or retiring APIs.
        """
        if hasattr(decision, "__dataclass_fields__"):
            d_dict = asdict(decision)
        elif hasattr(decision, "__dict__"):
            d_dict = decision.__dict__
        elif isinstance(decision, dict):
            d_dict = decision
        else:
            return False, f"Unsupported decision object type: {type(decision)}"

        dec_id = str(d_dict.get("decision_id") or "").strip()
        req_id = str(d_dict.get("request_id") or "").strip()
        raw_status = d_dict.get("status")
        if raw_status is None or not str(raw_status).strip():
            return False, f"Decision '{dec_id}' is missing required typed status; notification refused."
        status = str(raw_status).strip().lower()
        provenance = str(d_dict.get("provenance") or "").strip().lower()

        # 1. Authoritative Typed Status Check:
        # Actionable decision states awaiting human operator response
        if status not in ("pending", "clarification_requested"):
            if allow_legacy_unanswered and status == "rejected" and d_dict.get("answer") is None:
                # Retain legacy input-rejected records (stale replies rejected, question unanswered)
                pass
            else:
                return False, f"Decision '{dec_id}' has non-actionable typed status '{status}'; only active pending decisions may notify operator."
        # 2. Explicit Synthetic Provenance & Flags Check:
        if d_dict.get("is_synthetic") is True or d_dict.get("is_test") is True:
            return False, f"Decision '{dec_id}' has explicit synthetic/test flag set; human notification refused."

        if provenance == "synthetic_test":
            return False, f"Decision '{dec_id}' has explicit synthetic_test provenance; human notification refused."

        # Check audit trail for synthetic test probes answering/invalidating this decision
        audit_trail = d_dict.get("audit_trail", [])
        if isinstance(audit_trail, list):
            for entry in audit_trail:
                if isinstance(entry, dict) and entry.get("provenance") == "synthetic_test" and entry.get("status") in ("rejected", "resolved", "answered"):
                    return False, f"Decision '{dec_id}' was processed by a synthetic test probe; human notification refused."

        # 3. Linked Ledger Request State Check:
        resolved_ledger_req = ledger_request or cls.lookup_ledger_request(req_id, ledger=ledger)
        if resolved_ledger_req and isinstance(resolved_ledger_req, dict):
            req_state = str(resolved_ledger_req.get("state") or "").strip().lower()
            if req_state in ("done", "completed", "closed"):
                return False, f"Underlying request '{req_id}' is already in terminal state '{req_state}'; decision is obsolete."
            if resolved_ledger_req.get("task_type") == "synthetic" or resolved_ledger_req.get("is_synthetic") is True:
                return False, f"Underlying request '{req_id}' is marked as synthetic in ledger; human notification refused."

        return True, "Eligible for notification"

    @classmethod
    def from_decision(
        cls,
        decision: Any,
        project_override: Optional[str] = None,
        ledger_request: Optional[Dict[str, Any]] = None,
        ledger: Optional[Any] = None,
        strict: bool = False,
    ) -> Optional[NotificationEvent]:
        """Translates a DecisionContract or decision dictionary into a NotificationEvent.
        Strictly refuses retired, resolved, synthetic, or completed requests.
        """
        is_eligible, refusal_reason = cls.is_decision_notifiable(
            decision,
            ledger_request=ledger_request,
            ledger=ledger,
        )
        if not is_eligible:
            if strict:
                raise ValueError(f"Decision notification refused: {refusal_reason}")
            return None

        if hasattr(decision, "__dataclass_fields__"):
            d_dict = asdict(decision)
        elif hasattr(decision, "__dict__"):
            d_dict = decision.__dict__
        elif isinstance(decision, dict):
            d_dict = decision
        else:
            raise ValueError(f"Unsupported decision object type: {type(decision)}")

        dec_id = d_dict.get("decision_id") or "DEC-unknown"
        req_id_raw = d_dict.get("request_id")
        if req_id_raw and dec_id and req_id_raw != dec_id:
            req_id = f"{req_id_raw} ({dec_id})"
        else:
            req_id = req_id_raw or dec_id
        question = d_dict.get("question") or "Human decision required"
        options = d_dict.get("options") or []
        recommendation = d_dict.get("recommendation") or ""
        issue_url = (
            d_dict.get("issue_url")
            or d_dict.get("canonical_issue_url")
            or "https://github.com/Bavariance/polysimulator/issues/4543"
        )
        project = project_override or "Bavariance/polysimulator"
        # A decision reply must reach the session that raised it, so prefer the
        # decision's own session and fall back to the linked ledger request.
        resolved_request = ledger_request or cls.lookup_ledger_request(
            str(d_dict.get("request_id") or ""), ledger=ledger
        )

        opt_summaries = []
        for opt in options:
            if isinstance(opt, dict):
                opt_id = opt.get("id", "")
                opt_lbl = opt.get("label") or opt.get("description", "")
                opt_summaries.append(f"{opt_id}: {opt_lbl}" if opt_id else opt_lbl)
            else:
                opt_summaries.append(str(opt))
        opts_str = f" Options: {'; '.join(opt_summaries)}." if opt_summaries else ""
        rec_str = f" Recommended: {recommendation}." if recommendation else ""

        clean_q = re.sub(r"https?://\S+", "", str(question or "")).strip()
        clean_prompt = re.sub(r"https?://\S+", "", str(d_dict.get("prompt") or "")).strip()
        clean_prompt = re.sub(r"req-[a-zA-Z0-9_-]+(?:\s*\([^)]*\))?:\s*", "", clean_prompt).strip()
        clean_q = re.sub(r"req-[a-zA-Z0-9_-]+(?:\s*\([^)]*\))?:\s*", "", clean_q).strip()
        summary = f"{question.rstrip('.')}.{opts_str}{rec_str}".strip()
        return NotificationEvent(
            event_type="decision",
            project=project,
            request_id=req_id,
            summary=summary,
            canonical_link=issue_url,
            metadata={
                "decision_id": dec_id,
                "options": options,
                "recommendation": recommendation,
                "problem": clean_prompt or clean_q or "Human decision required to proceed.",
                "proposed_action": f"Choose between: {'; '.join(opt_summaries)}." if opt_summaries else "Select an option below.",
                "consequence_or_risk": "Work on dependent tasks remains suspended until an authorized choice is selected.",
                "details_url": issue_url,
                "plain_presentation": True,
            },
            session_id=(
                d_dict.get("session")
                or d_dict.get("session_id")
                or (resolved_request or {}).get("session")
                or None
            ),
        )

    @classmethod
    def load_decision_from_file(
        cls,
        decision_id: str,
        decisions_file: Optional[Path] = None,
    ) -> Optional[Dict[str, Any]]:
        """Load a decision dict by ID from a decisions.json file."""
        paths = [
            decisions_file,
            Path.cwd() / "decisions.json",
            Path(__file__).resolve().parent / "decisions.json",
            Path.home() / ".veyyon" / "workflows" / "decisions.json",
        ]
        for p in paths:
            if p and p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    decs = data.get("decisions", {})
                    if isinstance(decs, dict) and decision_id in decs:
                        return decs[decision_id]
                    elif isinstance(decs, list):
                        for d in decs:
                            if d.get("decision_id") == decision_id:
                                return d
                except Exception:
                    pass
        return None


@dataclass
class UnresolvedQuestion:
    decision_id: str
    request_id: str
    topic: str
    owner: str
    canonical_link: str
    question: str
    options: List[Any]
    recommendation: str
    cadence_seconds: float
    last_notified_at: Optional[float]
    next_reminder_at: Optional[float]
    reminder_count: int
    reminder_status: str  # "active", "answered", "stopped"
    stop_reason: Optional[str]
    session_id: Optional[str]
    is_due: bool
    seconds_remaining: int
    raw_status: str


class QuestionReminderManager:
    """Manages script-owned recurring reminders for unresolved operator questions.

    Adheres strictly to AGENTS.md policy:
    - Bounded 15-minute cadence (default 900s)
    - Persistent tracking across decisions.json and telegram_notify_state.json
    - Preserves deduplication for non-due messages while allowing deliberate due reminders
    - Clear answers and explicit stops remove ONLY the answered/stopped topic
    - Retains questions where answer is null, including legacy-poisoned status='rejected'
      records where only stale comments were rejected
    - No always-running model worker: pure script execution
    """

    def __init__(
        self,
        decisions_path: Optional[Path] = None,
        ledger_path: Optional[Path] = None,
        state_file: Optional[Path] = None,
        default_cadence: float = DEFAULT_REMINDER_CADENCE_SECONDS,
    ):
        self.decisions_path = Path(decisions_path) if decisions_path else self._locate_decisions_file()
        self.ledger_path = Path(ledger_path) if ledger_path else self._locate_ledger_file()
        self.state_file = Path(state_file) if state_file else (Path.cwd() / "telegram_notify_state.json")
        self.default_cadence = default_cadence

    @classmethod
    def _locate_decisions_file(cls) -> Path:
        candidates = [
            Path.cwd() / "decisions.json",
            Path(__file__).resolve().parent / "decisions.json",
            Path.home() / ".veyyon" / "workflows" / "decisions.json",
        ]
        for c in candidates:
            if c.exists():
                return c
        return Path.cwd() / "decisions.json"

    @classmethod
    def _locate_ledger_file(cls) -> Path:
        candidates = [
            Path.cwd() / "ledger.json",
            Path(__file__).resolve().parent / "ledger.json",
            Path.home() / ".veyyon" / "workflows" / "ledger.json",
        ]
        for c in candidates:
            if c.exists():
                return c
        return Path.cwd() / "ledger.json"

    def _load_decisions(self) -> Dict[str, Any]:
        if not self.decisions_path.exists():
            return {"version": 1, "decisions": {}}
        try:
            return json.loads(self.decisions_path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "decisions": {}}

    def _save_decisions(self, data: Dict[str, Any]) -> None:
        self.decisions_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.decisions_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp_path.replace(self.decisions_path)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    def _load_notify_state(self) -> Dict[str, Any]:
        if not self.state_file.exists():
            return {"version": 1, "last_dispatched_at": 0.0, "sent_signatures": {}, "question_reminders": {}}
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "last_dispatched_at": 0.0, "sent_signatures": {}, "question_reminders": {}}

    def _save_notify_state(self, data: Dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_file.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            tmp_path.replace(self.state_file)
        except Exception:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    def _save_decision_reminder_metadata_under_lock(
        self,
        decision_id: str,
        now: float,
        cadence_seconds: float,
        next_count: int,
    ) -> bool:
        """Re-reads decisions.json or ledger.json under FileLock and performs a metadata-only merge.
        Returns False if decision was answered/stopped or became terminal concurrently."""
        lock_file = str(self.decisions_path) + ".lock"
        with FileLock(lock_file):
            data = self._load_decisions()
            decs = data.get("decisions", {})
            if isinstance(decs, dict) and decision_id in decs:
                d = decs[decision_id]
                # Check for concurrent verified answer or stop: NEVER overwrite or resurrect!
                if d.get("answer") is not None or d.get("status") == "answered" or d.get("reminder_status") in ("answered", "stopped"):
                    return False

                # Metadata-only merge: update reminder timestamps/counts ONLY
                d["last_notified_at"] = now
                d["next_reminder_at"] = now + cadence_seconds
                d["reminder_count"] = next_count
                d["reminder_status"] = "active"

                self._save_decisions(data)
                return True

        # Check ledger.json
        if self.ledger_path.exists():
            ledger_lock = str(self.ledger_path) + ".lock"
            with FileLock(ledger_lock):
                try:
                    ldata = json.loads(self.ledger_path.read_text(encoding="utf-8"))
                except Exception:
                    return False
                found_dec = None
                for req in ldata.get("requests", {}).values():
                    for dec in req.get("decisions", []):
                        if str(dec.get("id") or "").strip() == decision_id:
                            found_dec = dec
                            break
                    if found_dec:
                        break
                if not found_dec:
                    return False
                if found_dec.get("answer") is not None or str(found_dec.get("status") or "").strip().lower() in ("answered", "resolved") or found_dec.get("reminder_status") in ("answered", "stopped"):
                    return False

                found_dec["last_notified_at"] = now
                found_dec["next_reminder_at"] = now + cadence_seconds
                found_dec["reminder_count"] = next_count
                found_dec["reminder_status"] = "active"
                self.ledger_path.write_text(json.dumps(ldata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                return True

        return False

    def _update_notify_state_reminder_under_lock(
        self,
        decision_id: str,
        reminder_dict: Dict[str, Any],
    ) -> None:
        """Re-reads notify_state.json under lock and updates only question_reminders,
        preserving fresh sent_signatures and last_dispatched_at written by adapter.notify."""
        lock_file = str(self.state_file) + ".lock"
        with FileLock(lock_file):
            state = self._load_notify_state()
            state.setdefault("question_reminders", {})[decision_id] = reminder_dict
            self._save_notify_state(state)

    def get_unresolved_questions(self, now: Optional[float] = None) -> List[UnresolvedQuestion]:
        now = now or time.time()
        dec_data = self._load_decisions()
        raw_decs = dec_data.get("decisions", {})

        ledger_requests = {}
        if self.ledger_path.exists():
            try:
                ldata = json.loads(self.ledger_path.read_text(encoding="utf-8"))
                reqs = ldata.get("requests", {})
                if isinstance(reqs, dict):
                    ledger_requests = reqs
                elif isinstance(reqs, list):
                    ledger_requests = {r.get("id"): r for r in reqs if isinstance(r, dict)}
            except Exception:
                pass

        notify_state = self._load_notify_state()
        persisted_reminders = notify_state.get("question_reminders", {})

        unresolved: List[UnresolvedQuestion] = []
        seen_decision_ids = set()
        if isinstance(raw_decs, dict):
            items = list(raw_decs.items())
        elif isinstance(raw_decs, list):
            items = [(d.get("decision_id"), d) for d in raw_decs if isinstance(d, dict)]
        else:
            items = []

        for dec_id, d in items:
            if not dec_id:
                continue

            if d.get("is_synthetic") is True or d.get("is_test") is True:
                continue
            if str(d.get("provenance") or "").strip().lower() == "synthetic_test":
                continue

            if d.get("answer") is not None or d.get("status") == "answered" or d.get("reminder_status") == "answered":
                continue

            if d.get("reminder_status") == "stopped" or d.get("stopped") is True:
                continue

            req_id = str(d.get("request_id") or "").strip()
            l_req = ledger_requests.get(req_id)
            if l_req:
                req_state = str(l_req.get("state") or "").strip().lower()
                if req_state in ("done", "completed", "closed"):
                    continue
                if l_req.get("task_type") == "synthetic" or l_req.get("is_synthetic") is True:
                    continue

            raw_status = str(d.get("status") or "pending").strip().lower()
            if raw_status not in ("pending", "clarification_requested"):
                if raw_status == "rejected" and d.get("answer") is None:
                    # Critical: legacy record where only stale comments were rejected, but question is UNANSWERED.
                    pass
                else:
                    continue

            seen_decision_ids.add(dec_id)
            if d.get("is_synthetic") is True or d.get("is_test") is True:
                continue
            if str(d.get("provenance") or "").strip().lower() == "synthetic_test":
                continue

            if d.get("answer") is not None or d.get("status") == "answered" or d.get("reminder_status") == "answered":
                continue

            if d.get("reminder_status") == "stopped" or d.get("stopped") is True:
                continue

            req_id = str(d.get("request_id") or "").strip()
            l_req = ledger_requests.get(req_id)
            if l_req:
                req_state = str(l_req.get("state") or "").strip().lower()
                if req_state in ("done", "completed", "closed"):
                    continue
                if l_req.get("task_type") == "synthetic" or l_req.get("is_synthetic") is True:
                    continue

            raw_status = str(d.get("status") or "pending").strip().lower()
            if raw_status not in ("pending", "clarification_requested"):
                if raw_status == "rejected" and d.get("answer") is None:
                    # Critical: legacy record where only stale comments were rejected, but question is UNANSWERED.
                    pass
                else:
                    continue

            rem_info = persisted_reminders.get(dec_id, {})
            cadence = float(d.get("reminder_cadence_seconds") or rem_info.get("cadence_seconds") or self.default_cadence)
            last_notified = d.get("last_notified_at") or rem_info.get("last_notified_at")
            reminder_count = int(d.get("reminder_count") or rem_info.get("reminder_count") or 0)
            next_reminder = d.get("next_reminder_at") or rem_info.get("next_reminder_at")

            if last_notified is not None and next_reminder is None:
                next_reminder = last_notified + cadence

            if last_notified is None:
                is_due = True
                seconds_remaining = 0
            elif next_reminder is not None:
                is_due = (now >= next_reminder)
                seconds_remaining = max(0, int(next_reminder - now))
            else:
                is_due = (now - last_notified >= cadence)
                seconds_remaining = max(0, int(cadence - (now - last_notified)))

            responders = d.get("authorized_responders") or []
            owner = responders[0] if responders else (l_req.get("owner") if l_req else "Operator")
            canonical_link = d.get("issue_url") or d.get("canonical_link") or "https://github.com/Bavariance/polysimulator"
            session_id = d.get("session") or d.get("session_id") or (l_req.get("session") if l_req else None)

            unresolved.append(
                UnresolvedQuestion(
                    decision_id=dec_id,
                    request_id=req_id,
                    topic=d.get("topic") or dec_id,
                    owner=owner,
                    canonical_link=canonical_link,
                    question=d.get("question") or "Operator decision required",
                    options=d.get("options") or [],
                    recommendation=d.get("recommendation") or "",
                    cadence_seconds=cadence,
                    last_notified_at=last_notified,
                    next_reminder_at=next_reminder,
                    reminder_count=reminder_count,
                    reminder_status=d.get("reminder_status", "active"),
                    stop_reason=d.get("stop_reason"),
                    session_id=session_id,
                    is_due=is_due,
                    seconds_remaining=seconds_remaining,
                    raw_status=raw_status,
                )
            )

        # 2. Include unanswered decision questions from open ledger requests (deduplicated by decision ID)
        for req_id, l_req in ledger_requests.items():
            if not isinstance(l_req, dict):
                continue
            if str(l_req.get("state") or "").strip().lower() in ("done", "completed", "closed"):
                continue
            if l_req.get("task_type") == "synthetic" or l_req.get("is_synthetic") is True:
                continue

            for dec in l_req.get("decisions", []):
                if not isinstance(dec, dict):
                    continue
                d_id = str(dec.get("id") or "").strip()
                if not d_id or d_id in seen_decision_ids:
                    continue

                # Check if answered
                if dec.get("answer") is not None or str(dec.get("status") or "").strip().lower() in ("answered", "resolved"):
                    continue

                # Check if stopped
                if dec.get("reminder_status") == "stopped" or dec.get("stopped") is True:
                    continue

                raw_status = str(dec.get("status") or "pending").strip().lower()
                if raw_status not in ("pending", "clarification_requested"):
                    continue

                rem_info = persisted_reminders.get(d_id, {})
                cadence = float(dec.get("reminder_cadence_seconds") or rem_info.get("cadence_seconds") or self.default_cadence)
                last_notified = dec.get("last_notified_at") or rem_info.get("last_notified_at")
                reminder_count = int(dec.get("reminder_count") or rem_info.get("reminder_count") or 0)
                next_reminder = dec.get("next_reminder_at") or rem_info.get("next_reminder_at")

                if last_notified is not None and next_reminder is None:
                    next_reminder = last_notified + cadence

                if last_notified is None:
                    is_due = True
                    seconds_remaining = 0
                elif next_reminder is not None:
                    is_due = (now >= next_reminder)
                    seconds_remaining = max(0, int(next_reminder - now))
                else:
                    is_due = (now - last_notified >= cadence)
                    seconds_remaining = max(0, int(cadence - (now - last_notified)))

                owner = dec.get("authorized_responder") or l_req.get("owner") or "Operator"
                canonical_link = (
                    l_req.get("github", {}).get("issue_url")
                    or "https://github.com/Bavariance/polysimulator/issues/4582#issuecomment-5559531516"
                )
                session_id = dec.get("session") or l_req.get("session") or None

                seen_decision_ids.add(d_id)
                unresolved.append(
                    UnresolvedQuestion(
                        decision_id=d_id,
                        request_id=req_id,
                        topic=dec.get("topic") or d_id,
                        owner=owner,
                        canonical_link=canonical_link,
                        question=dec.get("question") or "Operator decision required",
                        options=dec.get("options") or [],
                        recommendation=dec.get("recommendation") or "",
                        cadence_seconds=cadence,
                        last_notified_at=last_notified,
                        next_reminder_at=next_reminder,
                        reminder_count=reminder_count,
                        reminder_status=dec.get("reminder_status", "active"),
                        stop_reason=dec.get("stop_reason"),
                        session_id=session_id,
                        is_due=is_due,
                        seconds_remaining=seconds_remaining,
                        raw_status=raw_status,
                    )
                )

        return unresolved

    def dispatch_reminders(
        self,
        adapter: "TelegramNotificationAdapter",
        dry_run: bool = True,
        force: bool = False,
        now: Optional[float] = None,
        explicit_slot: Optional[str] = None,
        explicit_chat_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Scans unresolved questions, checks 15-minute cadence, and dispatches due reminders.

        Guarantees:
        - dry_run is strictly read-only: zero mutations to decisions.json or notify_state.
        - Live delivery performs metadata-only merge under FileLock; never overwrites concurrent verified answers.
        - Preserves fresh notify_state sent_signatures written by adapter.notify.
        - Resolves session_id explicitly without guessing or silent mis-binding.
        """
        now = now or time.time()
        questions = self.get_unresolved_questions(now=now)
        due_questions = [q for q in questions if q.is_due]

        if not due_questions:
            return {
                "status": "no_due_reminders",
                "unresolved_count": len(questions),
                "due_count": 0,
                "dispatched": [],
            }

        dispatched_receipts = []

        for q in due_questions:
            next_count = q.reminder_count + 1

            opt_summaries = []
            for opt in q.options:
                if isinstance(opt, dict):
                    opt_id = opt.get("id", "")
                    opt_lbl = opt.get("label") or opt.get("description", "")
                    opt_summaries.append(f"{opt_id}: {opt_lbl}" if opt_id else opt_lbl)
                else:
                    opt_summaries.append(str(opt))
            opts_str = f" Options: {'; '.join(opt_summaries)}." if opt_summaries else ""
            rec_str = f" Recommended: {q.recommendation}." if q.recommendation else ""

            summary = f"[Reminder #{next_count}] {q.question.rstrip('.')}.{opts_str}{rec_str}".strip()

            # Explicit session binding: explicit session takes precedence, then question-bound session
            bound_session = session_id or q.session_id or None

            event = NotificationEvent(
                event_type="question",
                project="polysimulator",
                request_id=q.request_id,
                summary=summary,
                canonical_link=q.canonical_link,
                metadata={
                    "is_due_reminder": True,
                    "reminder_count": next_count,
                    "decision_id": q.decision_id,
                    "cadence_seconds": q.cadence_seconds,
                    "topic": q.topic,
                },
                session_id=bound_session,
            )

            receipt = adapter.notify(
                event,
                dry_run=dry_run,
                force=force,
                explicit_slot=explicit_slot,
                explicit_chat_id=explicit_chat_id,
                now=now,
            )
            dispatched_receipts.append({
                "decision_id": q.decision_id,
                "topic": q.topic,
                "reminder_count": next_count,
                "receipt": asdict(receipt),
            })

            # CRITICAL: dry_run is STRICTLY READ-ONLY. Never advance timestamps or save state on dry_run!
            if not dry_run and receipt.delivered:
                # Metadata-only merge under FileLock: re-reads fresh decisions and checks terminal state
                metadata_updated = self._save_decision_reminder_metadata_under_lock(
                    decision_id=q.decision_id,
                    now=now,
                    cadence_seconds=q.cadence_seconds,
                    next_count=next_count,
                )
                if metadata_updated:
                    # Re-reads fresh notify_state under lock to preserve adapter.notify signatures
                    self._update_notify_state_reminder_under_lock(
                        decision_id=q.decision_id,
                        reminder_dict={
                            "decision_id": q.decision_id,
                            "request_id": q.request_id,
                            "topic": q.topic,
                            "owner": q.owner,
                            "canonical_link": q.canonical_link,
                            "cadence_seconds": q.cadence_seconds,
                            "last_notified_at": now,
                            "next_reminder_at": now + q.cadence_seconds,
                            "reminder_count": next_count,
                            "status": "active",
                            "stop_reason": None,
                        },
                    )

        return {
            "status": "reminders_dispatched",
            "unresolved_count": len(questions),
            "due_count": len(due_questions),
            "dispatched": dispatched_receipts,
        }

    def stop_topic(
        self,
        decision_id: str,
        reason: str = "Operator explicit stop",
        actor: str = "Operator",
    ) -> Dict[str, Any]:
        """Explicitly stop recurring reminders for a single topic through trusted operator decision handling.
        Leaves all other unresolved topics active.
        Does NOT forge an answer; records an explicit operator reminder stop in audit_trail and decision state."""
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        lock_file = str(self.decisions_path) + ".lock"
        found_in_decisions = False
        with FileLock(lock_file):
            dec_data = self._load_decisions()
            decs = dec_data.get("decisions", {})
            if isinstance(decs, dict) and decision_id in decs:
                d_record = decs[decision_id]
                d_record["reminder_status"] = "stopped"
                d_record["stop_reason"] = reason
                d_record["stopped_by"] = actor
                d_record["stopped_at"] = now_iso

                audit_entry = {
                    "timestamp": now_iso,
                    "action": "reminder_stop",
                    "actor": actor,
                    "reason": reason,
                    "provenance": "human_operator",
                }
                d_record.setdefault("audit_trail", []).append(audit_entry)
                self._save_decisions(dec_data)
                found_in_decisions = True

        if not found_in_decisions:
            found_in_ledger = False
            if self.ledger_path.exists():
                ledger_lock = str(self.ledger_path) + ".lock"
                with FileLock(ledger_lock):
                    try:
                        ldata = json.loads(self.ledger_path.read_text(encoding="utf-8"))
                    except Exception:
                        return {"ok": False, "error": f"Failed to load ledger: {self.ledger_path}"}
                    for req in ldata.get("requests", {}).values():
                        for dec in req.get("decisions", []):
                            if str(dec.get("id") or "").strip() == decision_id:
                                dec["reminder_status"] = "stopped"
                                dec["stop_reason"] = reason
                                dec["stopped_by"] = actor
                                dec["stopped_at"] = now_iso
                                found_in_ledger = True
                                break
                        if found_in_ledger:
                            break
                    if found_in_ledger:
                        self.ledger_path.write_text(json.dumps(ldata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            if not found_in_ledger:
                return {"ok": False, "error": f"Decision '{decision_id}' not found in decisions store or ledger"}
        self._update_notify_state_reminder_under_lock(
            decision_id,
            {
                "decision_id": decision_id,
                "status": "stopped",
                "stop_reason": reason,
                "stopped_by": actor,
                "stopped_at": now_iso,
            },
        )

        return {
            "ok": True,
            "decision_id": decision_id,
            "reminder_status": "stopped",
            "reason": reason,
            "actor": actor,
        }

    def run_reminder_loop(
        self,
        adapter: "TelegramNotificationAdapter",
        interval_seconds: float = 60.0,
        dry_run: bool = True,
        force: bool = False,
        explicit_slot: Optional[str] = None,
        explicit_chat_id: Optional[str] = None,
        session_id: Optional[str] = None,
        max_iterations: Optional[int] = None,
        stop_event: Optional[Any] = None,
        log_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        """Supervised recurring reminder loop entrypoint (pure Python, zero model worker overhead).
        Periodically evaluates canonical decisions.json and dispatches deliberate due reminders
        at the bounded 15-minute cadence.
        """
        log = log_callback or (lambda msg: print(f"[{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}] {msg}", flush=True))
        log(f"Supervised reminder loop started (interval={interval_seconds}s, cadence={self.default_cadence}s, dry_run={dry_run})")
        iterations = 0
        total_dispatched = 0

        while True:
            iterations += 1
            try:
                now = time.time()
                res = self.dispatch_reminders(
                    adapter=adapter,
                    dry_run=dry_run,
                    force=force,
                    now=now,
                    explicit_slot=explicit_slot,
                    explicit_chat_id=explicit_chat_id,
                    session_id=session_id,
                )
                due_count = res.get("due_count", 0)
                if due_count > 0:
                    total_dispatched += due_count
                    log(f"Iteration #{iterations}: Dispatched {due_count} due reminder(s)")
                    for d in res.get("dispatched", []):
                        rec = d.get("receipt", {})
                        log(f"  -> Topic: {d.get('topic')} (count #{d.get('reminder_count')}): {rec.get('status')} - {rec.get('reason')}")
                else:
                    unresolved = res.get("unresolved_count", 0)
                    log(f"Iteration #{iterations}: Checked {unresolved} question(s); 0 due")
            except Exception as e:
                log(f"Iteration #{iterations} error: {type(e).__name__}: {e}")

            if max_iterations is not None and iterations >= max_iterations:
                log(f"Reached max_iterations ({max_iterations}); exiting loop cleanly.")
                break

            if stop_event and getattr(stop_event, "is_set", lambda: False)():
                log("Stop event received; exiting loop cleanly.")
                break

            time.sleep(interval_seconds)

        return {
            "iterations": iterations,
            "total_dispatched": total_dispatched,
            "status": "stopped",
        }

def main() -> int:
    parser = argparse.ArgumentParser(description="Portable Telegram Notification Adapter")
    parser.add_argument("--project", default="polysimulator", help="Target project or repo name")
    parser.add_argument("--slot", default=None, help="Explicit slot identifier (e.g. telegram-polysim)")
    parser.add_argument("--chat-id", default=None, help="Explicit destination chat ID")
    parser.add_argument("--event-type", choices=sorted(VALID_EVENT_TYPES), help="Event type: milestone, blocker, decision, completion, question, status")
    parser.add_argument("--request-id", default="req-manual", help="Request ID (e.g. req-4543)")
    parser.add_argument("--summary", default="", help="One-sentence status summary")
    parser.add_argument("--link", default="", help="Canonical issue or PR URL")
    parser.add_argument("--links", nargs="*", default=[], help="Additional canonical URLs (e.g. PRs, issues)")
    parser.add_argument("--decision-id", default=None, help="Decision ID from decisions.json to notify")
    parser.add_argument("--decisions-file", default=None, help="Path to decisions.json file")
    parser.add_argument("--packet", default=None, help="Path to CoordinatorPacket JSON file")
    parser.add_argument("--test-connection", action="store_true", help="Perform read-only API check (getMe)")
    parser.add_argument("--dry-run", action="store_true", help="Format and check dedup without network send")
    parser.add_argument("--send", action="store_true", help="Execute live network delivery")
    parser.add_argument("--force", action="store_true", help="Bypass dedup and cooldown checks")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON receipt")
    parser.add_argument(
        "--session",
        default=None,
        help="Originating session id; binds this message so a reply routes back to that session",
    )
    parser.add_argument(
        "--pool-db",
        default=None,
        help=(
            "Path to the shared bot_pool.db holding the message correlation index. "
            "Defaults to VEYYON_POOL_DB, else the installed pool at "
            "~/.veyyon/telegram/bot_pool.db when it exists; set VEYYON_POOL_DB=off to record nothing"
        ),
    )
    parser.add_argument("--reminders", action="store_true", help="List all unresolved operator questions and reminder due status")
    parser.add_argument("--dispatch-reminders", action="store_true", help="Dispatch due reminders for unresolved operator questions")
    parser.add_argument("--stop-topic", default=None, help="Explicitly stop recurring reminders for a question topic ID")
    parser.add_argument("--stop-reason", default="Operator explicit stop", help="Reason for stopping reminders on a topic")
    parser.add_argument("--stop-actor", default="Operator", help="Actor identity recording the stop (e.g. Wladefant)")
    parser.add_argument("--cadence", type=float, default=None, help="Override reminder cadence seconds (default 900)")
    parser.add_argument("--loop", action="store_true", help="Run supervised recurring reminder loop (no model daemon)")
    parser.add_argument("--loop-interval", type=float, default=60.0, help="Check interval in seconds for the supervised loop (default 60)")
    parser.add_argument("--max-iterations", type=int, default=None, help="Maximum loop iterations (optional, for bounded testing)")

    args = parser.parse_args()
    adapter = TelegramNotificationAdapter(
        correlation_store=OutboundCorrelationStore(Path(args.pool_db) if args.pool_db else None),
    )

    if args.reminders or args.dispatch_reminders or args.stop_topic or args.loop:
        rem_mgr = QuestionReminderManager(
            decisions_path=Path(args.decisions_file) if args.decisions_file else None,
            default_cadence=args.cadence or DEFAULT_REMINDER_CADENCE_SECONDS,
        )
        if args.stop_topic:
            res = rem_mgr.stop_topic(args.stop_topic, reason=args.stop_reason, actor=args.stop_actor)
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"[STOPPED] Topic {args.stop_topic} stopped by {res.get('actor')}: {res.get('reason')}")
            return 0 if res.get("ok") else 1

        if args.loop:
            dry_run_mode = args.dry_run or (not args.send)
            loop_res = rem_mgr.run_reminder_loop(
                adapter=adapter,
                interval_seconds=args.loop_interval,
                dry_run=dry_run_mode,
                force=args.force,
                explicit_slot=args.slot,
                explicit_chat_id=args.chat_id,
                session_id=args.session,
                max_iterations=args.max_iterations,
            )
            if args.json:
                print(json.dumps(loop_res, indent=2))
            return 0
        if args.dispatch_reminders:
            dry_run_mode = args.dry_run or (not args.send)
            res = rem_mgr.dispatch_reminders(
                adapter=adapter,
                dry_run=dry_run_mode,
                force=args.force,
                explicit_slot=args.slot,
                explicit_chat_id=args.chat_id,
                session_id=args.session,
            )
            if args.json:
                print(json.dumps(res, indent=2))
            else:
                print(f"[REMINDERS] Dispatched {res.get('due_count')} due reminder(s) of {res.get('unresolved_count')} unresolved question(s).")
                for item in res.get("dispatched", []):
                    rec = item.get("receipt", {})
                    prefix = "[DELIVERED]" if rec.get("delivered") else f"[{rec.get('status', 'FAILED').upper()}]"
                    print(f"  {prefix} {item.get('topic')}: {rec.get('reason')}")
            return 0

        questions = rem_mgr.get_unresolved_questions()
        if args.json:
            print(json.dumps([asdict(q) for q in questions], indent=2))
        else:
            print(f"Unresolved Operator Questions ({len(questions)}):")
            for q in questions:
                due_label = "DUE NOW" if q.is_due else f"due in {q.seconds_remaining}s"
                print(f"  [{q.decision_id}] Owner: {q.owner} | Cadence: {int(q.cadence_seconds)}s | Status: {q.reminder_status} | Reminders sent: {q.reminder_count} | ({due_label})")
                print(f"    Question: {q.question[:100]}...")
                print(f"    Link: {q.canonical_link}")
        return 0

    if args.test_connection:
        result = adapter.test_connection(project=args.project, slot_id=args.slot)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            if result.get("ok"):
                print(f"[OK] Connected to bot @{result.get('bot_username')} ({result.get('bot_name')}) on slot '{result.get('slot')}'.")
                print(f"     Configured owner destinations: {result.get('configured_destinations')}")
            else:
                print(f"[ERROR] {result.get('reason')}")
        return 0 if result.get("ok") else 1

    event: Optional[NotificationEvent] = None

    if args.decision_id:
        dec_file = Path(args.decisions_file) if args.decisions_file else None
        dec_dict = TelegramNotificationAdapter.load_decision_from_file(args.decision_id, decisions_file=dec_file)
        if not dec_dict:
            print(f"ERROR: Decision '{args.decision_id}' not found in decisions.json", file=sys.stderr)
            return 1
        # from_decision automatically performs typed validation and ledger check
        event = TelegramNotificationAdapter.from_decision(dec_dict, project_override=args.project)
        if not event:
            is_valid, reason = TelegramNotificationAdapter.is_decision_notifiable(dec_dict)
            if args.json:
                print(json.dumps({
                    "delivered": False,
                    "status": "refused",
                    "reason": f"Decision notification refused: {reason}",
                    "chat_id": "[REDACTED_DESTINATION]",
                }, indent=2))
            else:
                print(f"[REFUSED] Decision notification refused: {reason}")
            return 1
        if args.links:
            event.metadata.setdefault("links", []).extend(args.links)
    elif args.packet:
        packet_path = Path(args.packet)
        if not packet_path.exists():
            print(f"ERROR: Packet file not found: {packet_path}", file=sys.stderr)
            return 1
        packet_data = json.loads(packet_path.read_text(encoding="utf-8"))
        event = TelegramNotificationAdapter.from_coordinator_packet(packet_data, project_override=args.project)
        if not event:
            if args.json:
                print(json.dumps({"delivered": False, "status": "filtered", "reason": "Routine event class filtered"}, indent=2))
            else:
                print("[INFO] Packet contains routine execution chatter; dropped per event filter rules.")
            return 0
        if args.links:
            event.metadata.setdefault("links", []).extend(args.links)
    elif args.event_type and args.summary and (args.link or args.links):
        primary_link = args.link or (args.links[0] if args.links else "")
        event = NotificationEvent(
            event_type=args.event_type,
            project=args.project,
            request_id=args.request_id,
            summary=args.summary,
            canonical_link=primary_link,
            metadata={"links": args.links} if args.links else {},
            session_id=args.session,
        )
    else:
        parser.print_help()
        return 1

    if args.session:
        event.session_id = args.session

    # Execute notification
    dry_run_mode = args.dry_run or (not args.send)
    receipt = adapter.notify(
        event,
        dry_run=dry_run_mode,
        force=args.force,
        explicit_slot=args.slot,
        explicit_chat_id=args.chat_id,
    )

    if args.json:
        print(json.dumps(asdict(receipt), indent=2))
    else:
        status_prefix = "[DELIVERED]" if receipt.delivered else f"[{receipt.status.upper()}]"
        print(f"{status_prefix} {receipt.reason}")

    return 0 if receipt.delivered else 1


if __name__ == "__main__":
    sys.exit(main())
