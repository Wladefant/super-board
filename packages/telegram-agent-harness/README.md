# Telegram Agent Harness (`@super-board/telegram-agent-harness`)

Universal, mobile-friendly Telegram operations surface for managing multi-agent coding sessions across Veyyon, Herdr, Claude Code, and Codex.

## Features

- **Standalone Machine Daemon**: Long-polls opted-in Telegram bot slots via a detached background service without requiring an open terminal window.
- **Lease & Slot Management**: Transparent bot leasing from SQLite (`bot_pool.db`) preventing HTTP 409 poller collisions across sessions.
- **Direct-Chat (DM) Mode**: 1-on-1 private chat with the operator for session steering, approvals, and questions.
- **Supergroup Forum Topics Mode (Opt-In)**: Multiplexes multiple concurrent Veyyon sessions into a single Telegram Supergroup using native Telegram Forum Topics (`message_thread_id`).
- **Superboard Mini App Integration**: In-app Telegram web dashboard for interactive queue and session monitoring.

### Mini App session lifecycle

The relay serves the browser modules and forwards authenticated requests to the
local Mini App API. Raw Telegram `initData` is validated server-side following
[Telegram's validation contract](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app);
client-provided user IDs are not authorization.

App sessions carry a random identity signed together with the actor and issuance
time. `POST /api/logout` revokes only the authenticated bearer session, persisting
its digest until absolute expiry in the slot-local revocation registry. Other
sessions remain valid. This is not actor-wide revocation: a still-valid Telegram
launch credential can establish a new session. Removing an actor from the
server allowlist rejects all that actor's sessions.

HTTP 401 clears the browser's cached session even when the response is not JSON.
The next explicit request attempts a fresh exchange; mutations are never replayed.
Expired Telegram launch credentials require reopening the app from Telegram.
The new session format intentionally rejects old bearer credentials, so deploying
the relay and local Mini App API requires coordinated installation; mismatched
versions fail closed. No installation or daemon restart is implicit in this change.

---

## Supergroup Forum Topics Mode (Opt-In Prototype)

Forum mode allows an operator to manage multiple concurrent Veyyon sessions inside a single Telegram Supergroup with native forum topics.

### Key Invariants
- **One topic per Veyyon session**: Created via `createForumTopic` on `/new` or `/attach`.
- **Reply routing by `message_thread_id`**: Updates and messages sent inside a topic are routed strictly to its bound session.
- **Topic closure on session end**: When a session finishes or `/detach`/`/close` is executed, `closeForumTopic` closes the thread.
- **Topic listing**: `/topics` command displays all active forum topics and their current turn status.
- **Opt-in only**: Never enabled on live slots by default. Requires explicit `"mode": "forum"` in `manifest.json`.

### Setup Guide
1. **Create Supergroup**: In Telegram, create a group and ensure it is a Supergroup.
2. **Enable Topics**: Open Group Settings (`Edit` -> `Topics`) and turn **Topics** ON (`is_forum: true`).
3. **Add Bot as Admin**: Add your bot (e.g. `@superboarddevbot`) to the supergroup and grant Admin privileges with **Manage Topics** (`can_manage_topics`).
4. **Get Supergroup Chat ID**: Find the supergroup chat ID (typically `-100...`).
5. **Configure Manifest**: In `~/.veyyon/telegram/manifest.json`:
   ```json
   {
     "slotId": "telegram-superboard",
     "stateDir": "C:/Users/wkiri/.claude/channels/telegram-superboard",
     "enabled": true,
     "daemon": true,
     "mode": "forum",
     "forumChatId": "-1001234567890",
     "defaultProject": "C:/Users/wkiri/development/super-board"
   }
   ```
6. **Start or Restart Daemon**:
   ```bash
   bun daemon/main.ts run
   ```

### Commands in Forum Mode
- `/topics` — List active topics and bound session status (`idle` / `running`)
- `/new [workspace]` — Start a new session in workspace and open a dedicated topic
- `/attach <session-id>` — Attach topic to an existing Veyyon session
- `/detach` or `/close` — Close current topic and detach session
- `/where` — Show bound session and turn status for the current topic
- `/sessions` — List all running sessions on the machine
- `/help` — Display command guide
