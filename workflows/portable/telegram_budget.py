#!/usr/bin/env python3
"""Bot API send budget shared with the TypeScript governor.

Telegram counts sends per bot token, so the daemon, the session extensions and
telegram_notifier.py must see each other's sends. They do through one small JSON
file per bot under ``~/.veyyon/run/telegram-budget/`` (override with
``VEYYON_TELEGRAM_BUDGET_DIR``; the value ``off`` disables sharing), guarded by an
atomic ``mkdir`` lock directory.

This is the same file format and the same ``reserve`` arithmetic as
packages/telegram-agent-harness/extension/telegram-budget.ts. Keep the two in step.
Times are epoch milliseconds.

The lock is held for one read-compute-write. A holder that died leaves a lock that
is ignored after STALE_LOCK_SECONDS. If the lock cannot be taken in
LOCK_TIMEOUT_SECONDS or the file system fails, the caller sends anyway: a late
message beats a lost one.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

LOCK_TIMEOUT_SECONDS = 2.0
STALE_LOCK_SECONDS = 10.0

CHAT_INTERVAL_MS = 1000
GROUP_LIMIT = 20
WINDOW_MS = 60_000
PANEL_RESERVE = 12


@dataclass
class Reservation:
    wait_ms: int  # 0 when the send was booked; otherwise how long to wait before asking again
    blocked_ms: int  # how long a 429 still blocks this chat (or the bot)
    reason: str


def default_budget_dir() -> Path:
    override = os.environ.get("VEYYON_TELEGRAM_BUDGET_DIR")
    return Path(override) if override else Path.home() / ".veyyon" / "run" / "telegram-budget"


def is_group_chat(chat_id: str) -> bool:
    return chat_id.startswith("-")


def reserve(
    state: Dict[str, Any],
    chat: str,
    kind: str,
    now: float,
    *,
    chat_interval_ms: int = CHAT_INTERVAL_MS,
    group_limit: int = GROUP_LIMIT,
    window_ms: int = WINDOW_MS,
    panel_reserve: int = PANEL_RESERVE,
) -> Reservation:
    """Books one send of ``kind`` into ``chat`` at ``now`` if allowed, else says how long to wait."""
    ledger = state.setdefault("chats", {}).setdefault(chat, {"lastAt": 0, "blockedUntil": 0, "sends": []})
    # A clock that moved backwards must not freeze the chat: never trust a time in the future.
    ledger["lastAt"] = min(ledger.get("lastAt", 0), now)
    ledger["sends"] = [[min(at, now), k] for at, k in ledger.get("sends", []) if now - at < window_ms]
    blocked = max(state.get("botBlockedUntil", 0), ledger.get("blockedUntil", 0))
    spaced = ledger["lastAt"] + chat_interval_ms
    ready_at = max(blocked, spaced)
    reason = "retry_after" if blocked > spaced else "chat interval"
    if is_group_chat(chat):
        panel = kind == "panel"
        limit = group_limit if panel else group_limit - panel_reserve
        counted = ledger["sends"] if panel else [s for s in ledger["sends"] if s[1] != "panel"]
        if len(counted) >= limit:
            frees = counted[len(counted) - limit][0] + window_ms
            if frees > ready_at:
                ready_at = frees
                reason = "group window"
    blocked_ms = max(0, int(blocked - now))
    if ready_at <= now:
        ledger["sends"].append([now, "panel" if kind == "panel" else "message"])
        ledger["lastAt"] = now
        return Reservation(0, blocked_ms, "booked")
    return Reservation(int(ready_at - now + 0.999999), blocked_ms, reason)


def record_rate_limit(state: Dict[str, Any], chat: Optional[str], until: float) -> None:
    """Records a 429: blocks ``chat``, or the whole bot when the call was bound to no chat."""
    if chat is None:
        state["botBlockedUntil"] = max(state.get("botBlockedUntil", 0), until)
        return
    ledger = state.setdefault("chats", {}).setdefault(chat, {"lastAt": 0, "blockedUntil": 0, "sends": []})
    ledger["blockedUntil"] = max(ledger.get("blockedUntil", 0), until)


class SharedBudget:
    """The shared budget file of one bot."""

    def __init__(self, bot_id: str, directory: Optional[Path] = None) -> None:
        directory = directory or default_budget_dir()
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in bot_id)
        self.file = directory / f"{safe}.json"
        self.lock = directory / f"{safe}.lock"

    def update(self, fn: Callable[[Dict[str, Any]], Any], now_ms: Optional[float] = None) -> Any:
        """Runs ``fn`` on the state under the lock and saves it. Returns None when the file is unusable."""
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            if not self._acquire():
                return None
            try:
                state = self._read()
                result = fn(state)
                self._write(state, time.time() * 1000 if now_ms is None else now_ms)
                return result
            finally:
                try:
                    os.rmdir(self.lock)
                except OSError:
                    pass
        except Exception:
            return None

    def _acquire(self) -> bool:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                os.mkdir(self.lock)
                return True
            except FileExistsError:
                pass
            except OSError:
                return False
            try:
                if time.time() - os.stat(self.lock).st_mtime > STALE_LOCK_SECONDS:
                    os.rmdir(self.lock)
                    continue
            except OSError:
                continue
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.002)

    def _read(self) -> Dict[str, Any]:
        try:
            parsed = json.loads(self.file.read_text(encoding="utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("chats"), dict):
                return {"botBlockedUntil": float(parsed.get("botBlockedUntil") or 0), "chats": parsed["chats"]}
        except (OSError, ValueError):
            pass
        return {"botBlockedUntil": 0, "chats": {}}

    def _write(self, state: Dict[str, Any], now: float) -> None:
        state["chats"] = {
            chat: ledger for chat, ledger in state["chats"].items()
            if ledger.get("sends") or ledger.get("blockedUntil", 0) >= now
        }
        tmp = self.file.with_name(f"{self.file.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, self.file)


class BudgetBlocked(Exception):
    """The send would have to wait longer than the caller can afford. Nothing was sent."""

    def __init__(self, wait_ms: int, reason: str) -> None:
        super().__init__(f"Telegram budget: {reason}, the send would wait {wait_ms / 1000:.1f}s; not sent")
        self.wait_ms = wait_ms
        self.reason = reason


def acquire(
    budget: Optional[SharedBudget],
    chat: str,
    kind: str,
    *,
    max_wait_ms: float,
    now_ms: Callable[[], float] = lambda: time.time() * 1000,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Blocks until one send into ``chat`` is booked in the shared budget.

    Raises BudgetBlocked, without sleeping, when the wait would exceed ``max_wait_ms``.
    Without a usable budget file the send goes ahead unbooked.
    """
    if budget is None:
        return
    waited = 0.0
    while True:
        now = now_ms()
        claim = budget.update(lambda state: reserve(state, chat, kind, now), now)
        if claim is None or claim.wait_ms <= 0:
            return
        if waited + claim.wait_ms > max_wait_ms:
            raise BudgetBlocked(claim.wait_ms, claim.reason)
        sleep(claim.wait_ms / 1000)
        waited += claim.wait_ms


def budget_for(bot_id: str) -> Optional[SharedBudget]:
    """The shared budget of one bot, or None when VEYYON_TELEGRAM_BUDGET_DIR=off."""
    if os.environ.get("VEYYON_TELEGRAM_BUDGET_DIR") == "off":
        return None
    return SharedBudget(bot_id)
