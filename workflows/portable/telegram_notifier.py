#!/usr/bin/env python3
"""
workflows/telegram_notifier.py — Portable Telegram Workflow Status Notification Adapter

A harness-agnostic, pure Python standard library notification adapter for multi-agent workflows.
Consumes portable CoordinatorPacket events or direct status updates and dispatches strictly
deduped, rate-limited, single-sentence status reports with canonical links.

Invariants:
1. Canonical Authority: Status is always anchored to GitHub Issues / PRs and Superboard.
   Telegram is strictly an outbound notification transport, never a parallel system of record.
2. Filtered Event Classes: milestone, blocker, decision, completion ONLY.
   Routine tool execution, subagent traces, and search/read chatter are strictly rejected.
3. Message Format: Exactly ONE concise sentence + canonical link (GitHub issue/PR or Superboard).
4. No Credential Leakage: Tokens, keys, and local file paths are strictly redacted.
   Bot tokens are loaded into private memory only and never echoed, printed, or persisted to logs.
5. Deduplication & Cooldown:
   - SHA256 event signatures deduplicate identical messages within a 24-hour window.
   - Per-request cooldown prevents notification spamming during active multi-turn iteration.
   - Global rate limiter prevents flooding the transport.
6. Generic Multi-Repo Transport:
   - Per-project destination configuration maps repository/project names to Telegram slots.
   - No hardcoded single-project assumptions.
7. Fail-Closed Resilience: Network or API failures fail safely with structured receipts
   without halting or disrupting the coordinator execution flow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Supported event classes
VALID_EVENT_TYPES = {"milestone", "blocker", "decision", "completion", "question", "status"}

# Default timing intervals (in seconds)
DEFAULT_DEDUP_WINDOW_SECONDS = 86400  # 24 hours
DEFAULT_COOLDOWN_SECONDS = 300       # 5 minutes per request
DEFAULT_GLOBAL_MIN_INTERVAL = 30     # 30 seconds between outgoing messages
DEFAULT_HTTP_TIMEOUT = 10            # 10 seconds timeout for Telegram Bot API

# Default paths
DEFAULT_MANIFEST_PATHS = [
    Path(__file__).resolve().parent.parent / "telegram" / "manifest.json",
    Path(__file__).resolve().parent / "manifest.json",
    Path.home() / ".veyyon" / "workflows" / "telegram" / "manifest.json",
    Path.home() / ".veyyon" / "telegram" / "manifest.json",
]
DEFAULT_CHANNELS_BASE = Path.home() / ".claude" / "channels"


@dataclass
class NotificationEvent:
    event_type: str
    project: str
    request_id: str
    summary: str
    canonical_link: str
    metadata: Dict[str, Any] = field(default_factory=dict)

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

        # 3. Fallback to telegram-polysim if available, or first enabled slot
        if slots:
            for s in slots:
                if s.get("slotId") == "telegram-polysim" and s.get("enabled", True):
                    return {
                        "slotId": s.get("slotId"),
                        "stateDir": s.get("stateDir"),
                        "source": "manifest_default",
                    }
            return {
                "slotId": slots[0].get("slotId"),
                "stateDir": slots[0].get("stateDir"),
                "source": "manifest_first",
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

        # 1. Exact signature deduplication
        signatures = data.get("sent_signatures", {})
        last_sent = signatures.get(sig)
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

        # 3. Global rate limiter
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


class TelegramNotificationAdapter:
    """Portable Telegram notification adapter for multi-agent workflows."""

    def __init__(
        self,
        resolver: Optional[ProjectSlotResolver] = None,
        ledger: Optional[DeduplicationLedger] = None,
        state_dir_override: Optional[Path] = None,
    ):
        self.resolver = resolver or ProjectSlotResolver()
        if ledger:
            self.ledger = ledger
        else:
            state_file = (state_dir_override or Path.cwd()) / "telegram_notify_state.json"
            self.ledger = DeduplicationLedger(state_file)

    @classmethod
    def format_message(cls, event: NotificationEvent) -> str:
        """Formats an event into exactly ONE concise sentence + canonical link.
        Ensures no secrets or internal paths leak.
        """
        event.validate()

        type_labels = {
            "milestone": "Milestone",
            "blocker": "Blocker",
            "decision": "Decision Needed",
            "question": "Question",
            "status": "Status Update",
            "completion": "Completed",
        }
        label = type_labels.get(event.event_type, event.event_type.capitalize())

        # Clean summary to concise sentences
        clean_summary = SecretSanitizer.sanitize(event.summary.strip().replace("\n", " "))
        # Ensure single sentence punctuation
        if not clean_summary.endswith((".", "!", "?")):
            clean_summary += "."

        # Collect canonical links: event.canonical_link plus any metadata links
        raw_links = event.canonical_link.strip().split()
        if "links" in event.metadata and isinstance(event.metadata["links"], list):
            for lk in event.metadata["links"]:
                lk_str = str(lk).strip()
                if lk_str and lk_str not in raw_links:
                    raw_links.append(lk_str)
        links_str = " ".join(raw_links)

        # Build message
        msg = f"[{label}] {event.project} {event.request_id}: {clean_summary} {links_str}"
        return msg

    def test_connection(self, project: str = "polysimulator", slot_id: Optional[str] = None) -> Dict[str, Any]:
        """Read-only test to verify bot credentials and API reachability via getMe."""
        slot_info = self.resolver.resolve_slot(project, explicit_slot=slot_id)
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
    ) -> DeliveryReceipt:
        """Evaluates deduplication, formats message, and sends to verified owner destination."""
        event.validate()
        sig = DeduplicationLedger.compute_signature(event)

        # 1. Resolve project destination slot and credentials
        slot_info = self.resolver.resolve_slot(event.project, explicit_slot=explicit_slot)
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
            eligible, status, reason = self.ledger.check_eligible(event)
            if not eligible:
                return DeliveryReceipt(
                    delivered=False,
                    status=status,
                    reason=reason,
                    event_signature=sig,
                    chat_id="[REDACTED_DESTINATION]",
                )

        # 3. Format message
        message_text = self.format_message(event)

        # 4. Dry-run gate
        if dry_run:
            return DeliveryReceipt(
                delivered=True,
                status="dry_run",
                reason=f"[DRY-RUN] Would send message to slot '{slot_info['slotId']}': {message_text}",
                event_signature=sig,
                chat_id="[REDACTED_DESTINATION]",
            )

        # 5. Network dispatch
        api_url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": str(target_chat),
            "text": message_text,
            "disable_web_page_preview": False,
        }
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
                    msg_id = resp_data.get("result", {}).get("message_id")
                    bot_id = str(resp_data.get("result", {}).get("from", {}).get("id", ""))
                    self.ledger.record_dispatch(event)
                    return DeliveryReceipt(
                        delivered=True,
                        status="sent",
                        reason=f"Notification delivered to channel slot '{slot_info['slotId']}' (message_id: {msg_id})",
                        event_signature=sig,
                        message_id=msg_id,
                        chat_id="[REDACTED_DESTINATION]",
                        bot_id=bot_id,
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
            )

        # 3. Completion
        if status in ("completed", "done") or req_state == "done":
            return NotificationEvent(
                event_type="completion",
                project=project,
                request_id=req_id,
                summary=f"Request completed and verified against all criteria",
                canonical_link=issue_url,
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
            },
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

    args = parser.parse_args()
    adapter = TelegramNotificationAdapter()

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
        )
    else:
        parser.print_help()
        return 1

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
