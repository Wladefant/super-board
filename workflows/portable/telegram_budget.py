#!/usr/bin/env python3
"""Bot API send budget shared with the TypeScript governor.

Telegram counts sends per bot token, so the daemon, the session extensions and
telegram_notifier.py must see each other's sends. They do through one small JSON
file per bot under ``~/.veyyon/run/telegram-budget/`` (override with
``VEYYON_TELEGRAM_BUDGET_DIR``; the value ``off`` disables sharing), guarded by an
lock file that names its holder (pid and a random token).

This is the same file format and the same ``reserve`` arithmetic as
packages/telegram-agent-harness/extension/telegram-budget.ts. Keep the two in step.
Times are epoch milliseconds.

The lock is held for one read-compute-write, never across a Telegram call. A holder
that died is recognised by its pid being gone; a live holder is never evicted. A pid reused by an unrelated
process keeps a dead holder's lock alive: callers then fail open after the deadline until the file is deleted. If the lock cannot be taken in
LOCK_TIMEOUT_SECONDS or the file system fails, the caller sends anyway: a late
message beats a lost one.
"""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

LOCK_TIMEOUT_SECONDS = 2.0
# A lock that names no holder (not written by this code: empty or garbage) is abandoned after this long.
UNNAMED_LOCK_SECONDS = 1.0

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


def pid_alive(pid: int) -> bool:
    """True while a process with this pid exists. Never signals it (os.kill(pid, 0) would kill on Windows)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # access denied: exists
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


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
            token = self._acquire()
            if token is None:
                return None
            try:
                state = self._read()
                result = fn(state)
                self._write(state, time.time() * 1000 if now_ms is None else now_ms)
                return result
            finally:
                self._release(token)
        except Exception:
            return None

    def _acquire(self) -> Optional[str]:
        """Creates the lock file (O_EXCL) naming this process. None when the deadline passes.

        Every pass of the loop checks the deadline, whatever shape the lock path is in.
        """
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        token = f"{os.getpid()}.{uuid.uuid4()}"
        while True:
            got = self._try_lock(token)
            if got == "held":
                return token
            if got == "failed":
                return None
            self._sweep_if_abandoned()
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.002 + random.random() * 0.003)

    def _try_lock(self, token: str) -> str:
        # The holder's name must be in the file from the moment the lock exists, or a crash between create and
        # write would leave a lock nobody can attribute. So write a private file and hard-link it into place:
        # the link is atomic, fails if the lock exists, and the lock is never seen empty.
        tmp = f"{self.lock}.{token}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "token": token, "at": time.time() * 1000}))
            os.link(tmp, self.lock)
            return "held"
        except (FileExistsError, PermissionError):
            return "busy"
        except OSError:
            return "failed"
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def _sweep_if_abandoned(self) -> None:
        """Removes the lock when its holder's pid is gone. A live holder is never removed, however old its lock.

        The lock is claimed by an atomic rename, so only one sweeper acts on it. If the claimed file turns out
        not to be the one judged dead (another process swept and took the lock meanwhile), it is linked straight
        back. Never raises.
        """
        try:
            st = os.stat(self.lock)
            age = time.time() - st.st_mtime
            if os.path.isdir(self.lock):
                # Left by the earlier mkdir-based lock, which carried no holder name.
                if age > UNNAMED_LOCK_SECONDS * 10:
                    os.rmdir(self.lock)
                return
            raw = Path(self.lock).read_text(encoding="utf-8")
            pid = None
            try:
                pid = json.loads(raw).get("pid")
            except (ValueError, AttributeError):
                pass  # Not one of ours (empty or garbage): judged by age alone.
            abandoned = (not pid_alive(pid)) if isinstance(pid, int) else age > UNNAMED_LOCK_SECONDS
            if not abandoned:
                return
            claimed = f"{self.lock}.{os.getpid()}.{uuid.uuid4()}.dead"
            os.rename(self.lock, claimed)
            try:
                if Path(claimed).read_text(encoding="utf-8") != raw:
                    os.link(claimed, self.lock)
            finally:
                os.unlink(claimed)
        except Exception:
            pass

    def _release(self, token: str) -> None:
        # On Windows a waiter reading the lock holds it open, and unlink then fails with PermissionError, so retry.
        for _ in range(400):
            try:
                if json.loads(Path(self.lock).read_text(encoding="utf-8")).get("token") != token:
                    return  # Already swept as abandoned; nothing of ours is left to remove.
                os.unlink(self.lock)
                return
            except FileNotFoundError:
                return
            except (OSError, ValueError):
                time.sleep(0.005)

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
