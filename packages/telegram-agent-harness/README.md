# Telegram Agent Harness (`@super-board/telegram-agent-harness`)

Universal, mobile-friendly Telegram operations surface for managing multi-agent coding sessions across Veyyon, Herdr, Claude Code, and Codex.

## Features

- **Standalone Machine Daemon**: Long-polls opted-in Telegram bot slots via a detached background service without requiring an open terminal window.
- **Lease & Slot Management**: Transparent bot leasing from SQLite (`bot_pool.db`) preventing HTTP 409 poller collisions across sessions.
- **Direct-Chat (DM) Mode**: 1-on-1 private chat with the operator for session steering and questions.
- **Supergroup Forum Topics Mode (Opt-In)**: Multiplexes multiple concurrent Veyyon sessions into a single Telegram Supergroup using native Telegram Forum Topics (`message_thread_id`).
- **Superboard Mini App Integration**: In-app Telegram web dashboard for interactive queue and session monitoring.

### Tables in forwarded replies

Telegram has no table element. Tables use a monospace block only when each line
fits 32 characters and the cells contain no links or issue references.
Other tables use one block per row. The first cell is bold, and each remaining
cell follows its column name. Links stay clickable in their own cells.
This conversion changes Telegram output only. Terminal Markdown stays unchanged.

### Question waits and steering

Only waiting `telegram_question` calls allow steering to stop the wait.
These are `action: "wait"` and `action: "ask"` with waiting enabled.
An interrupted wait returns the current pending question without answering or dropping it.
`get`, `resolve`, `drop`, and calls with `wait: false` keep their normal results.
This requires a host that forwards extension tools' `interruptible` declaration.
Reloading the static extension requires separate operator authorization.

### Agent outbound attachments

The public `telegram_attachment` tool sends a local file through the session's
current Telegram route. `kind: "auto"` sends `.jpg`, `.jpeg`, `.png`, and `.webp`
files with `sendPhoto`; PDFs and other files use `sendDocument`. The tool accepts
an optional caption and filename, keeps the bound `chat_id` and
`message_thread_id`, and leaves `message_thread_id` unset for direct chats.
Telegram Bot API uploads are limited here to 10 MiB for photos and 50 MiB for
documents; larger files fail before any network request.

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

Telegram does not intercept, validate, or approve tool calls. Native Veyyon permissions remain independent and unchanged. Sender authentication, topic/session ownership, ordinary operator questions and consequential non-tool decisions remain enforced. Old tool-approval buttons report that the feature is obsolete without granting permission or executing work; the Mini App has no tool-approval section.

After upgrading from a guard-bearing loader, reload the **static extension** in each original session using the host's `/reload-config` command. `/tg-reload` only replaces the dynamic runtime and cannot remove hooks registered by the old loader. Restart the standalone daemon through its existing supervisor when authorized to activate its stale-callback handling; installation alone does not replace code already in memory. Never resume a duplicate session.

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

### Automatic Topics (Auto-Attach)
In forum mode, the daemon automatically creates and binds Telegram forum topics for live top-level Veyyon sessions:
- **Live terminal IPC source of truth**: Discovers live interactive terminal sessions via local named pipe endpoints (`TerminalSessionControl`). Historical disk sessions never trigger topic creation.
- **Folder naming & ordinals**: Topics are named after the workspace folder (e.g. `super-board`). When multiple live sessions share a folder, subsequent topics receive an ordinal suffix (`super-board (2)`, `super-board (3)`).
- **Dead-route rebinding**: If a topic exists for a workspace whose bound session has exited, starting a new session in that folder automatically rebinds the existing topic and posts a `🔁 Rebound to session <id>` note.
- **Lifecycle & interval**: Runs on daemon startup, upon terminal owner discovery, and periodically every 10 seconds (`autoAttachIntervalMs`, default 10,000 ms). Can be disabled per slot with `"autoAttach": false`.
- **Session listing indicator**: `/sessions` marks sessions that already have an attached forum topic with a `📌` indicator.
- **Dry run**: Test or inspect actions without contacting Telegram via `bun daemon/main.ts --reconcile-once --dry-run`.

### Live Lane Panel (Opt-In)
Set `"lanePanel": true` on a forum slot in the daemon manifest to give every topic bound to a session one pinned panel message:
- **Content**: state (running with elapsed time, or idle since HH:MM), model, last action, child lanes with their state, and the open question that waits on the operator. An ended session leaves the panel saying so, without buttons.
- **Updates**: the message is edited in place, only when its content changed, at most every 30 s while the session runs and every 120 s while it idles. Edits go through the Telegram governor as `panel` calls, so they use the reserved panel budget and coalesce.
- **Buttons**: `Stop` ends the current turn (same path as `/stop`); `Steer` and `Follow-up` make the operator's next message in the topic a steer or a queued follow-up (armed for 5 minutes). Button tokens expire after 1 hour, when a newer panel replaces them, when the topic is rebound, or when the daemon restarts; a stale button answers "Expired".
- **Mini App**: set `"lanePanelMiniAppLink": "https://t.me/<bot>/<app>"` to add an `Open in Mini App` button that opens the Mini App on the topic. Without it the button is left out.

### Commands in Forum Mode
- `/topics` — List active topics and bound session status (`idle` / `running`)
- `/new [workspace]` — Start a new session in workspace and open a dedicated topic
- `/attach <session-id>` — Attach topic to an existing Veyyon session
- `/detach` or `/close` — Close current topic and detach session
- `/where` — Show bound session and turn status for the current topic
- `/sessions` — List all running sessions on the machine
- `/help` — Display command guide
