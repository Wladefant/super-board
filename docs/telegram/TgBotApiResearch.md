# Telegram Bot API Deep Capability Research & Universal Agent Harness Architecture

**Date:** 2026-09-08  
**Author:** Antigravity (TgBotApiResearch lane)  
**Objective:** Comprehensive research on the Telegram Bot API (v7.x, v8.x, v9.x, and v10.x), survey of existing open-source Telegram agent bridges (Pi, Claude Code, Codex, grammY, Herdr), and specification of a thin, harness-agnostic architecture ("T3 Code inside Telegram") for multi-agent management.

---

## Executive Summary

The Telegram Bot API provides an ideal operational surface for multi-agent fleet management on mobile and desktop:
1. **Topic-Based Session Multiplexing**: Forum Supergroups (`is_forum: true`) and Bot DM Threaded Mode (`has_topics_enabled: true`) provide clean per-session isolation (`message_thread_id`), completely solving context interleaving.
2. **In-Place Status Boards**: Dynamic updates using `editMessageText` and `editMessageReplyMarkup` enable live agent progress meters, tool execution cards, and self-updating status cards without notification spam.
3. **Structured Approvals & Free-Text Steering**: Inline keyboards (`InlineKeyboardMarkup` + `callback_data` max 64 bytes) handle 1-tap approvals and selections; `ForceReply` provides an automated fallback for free-text steering.
4. **Rich Artifact & Media Delivery**: UI screenshots (`sendPhoto`), diff/log attachments (`sendDocument`), and multi-image albums (`sendMediaGroup`).
5. **Interactive Telegram Mini Apps (Web Apps)**: Embedded webviews launched via `web_app` buttons authenticated cryptographically via `initData` HMAC-SHA256, unlocking mobile-friendly terminal replays, side-by-side diff viewers, and Superboard kanban boards.
6. **Universal Backend via Herdr / Thin Adapter**: By decoupling Telegram transport from any single harness and targeting a thin, 6-method adapter contract (`list`, `prompt`, `decide`, `abort`, `read`, `artifacts`), the same Telegram harness controls Veyyon, Pi/omp, Claude Code, Codex, and Herdr-managed agents seamlessly.

---

## 1. Telegram Bot API Capability Catalog & Constraints

### 1.1 Bot API Evolution (8.x, 9.x, 10.x)

| Version | Release Date | Key Features Relevant to Agent Harness |
| :--- | :--- | :--- |
| **Bot API 8.0** | Nov 2024 | Mini App full-screen mode (`requestFullscreen`), device orientation/storage, native file download prompts (`downloadFile`), `PreparedInlineMessage`. |
| **Bot API 9.0** | Apr 2025 | Mini App `DeviceStorage` (5MB persistent local storage) and `SecureStorage` (iOS Keychain / Android Keystore for sensitive tokens/secrets). |
| **Bot API 9.3** | Dec 2025 | **Topics in Private Chats (Threaded Mode)**: `has_topics_enabled` in `User`, `createForumTopic` in private DMs, `sendMessageDraft` for streaming draft responses. |
| **Bot API 9.4** | Feb 2026 | Custom emojis in bot messages; `style` (button colors) on `InlineKeyboardButton` and `KeyboardButton`. |
| **Bot API 10.0** | May 2026 | Connected Business/Secretary bots opened to all accounts without Telegram Premium; `deleteMessageReaction`. |
| **Bot API 10.1** | Jun 2026 | **Rich Messages**: Structured formatting blocks (`InputRichBlockCode`, `InputRichBlockTable`, `InputRichBlockThinking`, `InputRichBlockDetails`) and `sendRichMessageDraft`. |
| **Bot API 10.3** | Aug 2026 | `DisabledButton` state, `RichBlockExpandableBlockQuotation`, `sendMessageDraft` controls (`can_stop`, `keep_on_stop`). |

---

### 1.2 Topic & Thread Multiplexing

Telegram supports two distinct mechanisms for thread isolation:

#### A. Forum Supergroups (`createForumTopic`)
- **Prerequisite**: Supergroup with `is_forum: true` (enabled in group settings; bot must have `can_manage_topics` administrator right).
- **API Methods**:
  - `createForumTopic(chat_id, name, icon_color?, icon_custom_emoji_id?)` -> returns `ForumTopic` with `message_thread_id`.
  - `editForumTopic(chat_id, message_thread_id, name?, icon_custom_emoji_id?)`.
  - `closeForumTopic(chat_id, message_thread_id)`: Marks topic closed/resolved; users cannot write unless reopened.
  - `reopenForumTopic(chat_id, message_thread_id)`.
  - `deleteForumTopic(chat_id, message_thread_id)`: Deletes topic and all contained messages permanently.
  - `unpinAllForumTopicMessages(chat_id, message_thread_id)`.
- **Targeting**: Pass `message_thread_id` to any sending method (`sendMessage`, `sendPhoto`, `sendDocument`, `sendChatAction`).
- **Use Case**: Shared team or multi-project operations where multiple operators monitor agents simultaneously.

#### B. Bot DM Threaded Mode (Bot API 9.3+)
- **Prerequisite**: Enabled via BotFather Mini App (*Bot Settings > Thread Settings > Threaded Mode: ON*). Verified via `getMe().has_topics_enabled === true`.
- **Behavior**: The 1-on-1 private chat with the bot splits into separate topics directly in the user's client.
- **Targeting**: `createForumTopic` works directly in private chats when `has_topics_enabled` is true. `message_thread_id` routes messages directly to specific topics inside the DM.
- **Use Case**: Solo operator managing multiple parallel agent sessions privately on phone/desktop.

---

### 1.3 Keyboards, Decisions & User Input

#### Inline Keyboards & Callback Queries
- **Structure**: `InlineKeyboardMarkup` containing rows of `InlineKeyboardButton`.
- **Fields**:
  - `text`: Button label.
  - `callback_data`: **CRITICAL CONSTRAINT: 1 to 64 bytes**. Exceeding 64 bytes causes API error `BUTTON_DATA_INVALID`. Must use compact token/hash pointers (e.g. `d:<uuid_short>:<opt_id>`).
  - `web_app`: `WebAppInfo` with HTTPS URL for Mini App popups.
  - `copy_text`: `CopyTextButton` to copy text to clipboard (e.g. git commit SHA, CLI command).
- **Callback Lifecycle**:
  - User taps button -> Telegram sends `Update` with `callback_query`.
  - Bot MUST invoke `answerCallbackQuery(callback_query_id, text?, show_alert?, url?, cache_time?)`.
    - `show_alert: false`: Shows quick non-modal toast banner at the top of the chat.
    - `show_alert: true`: Shows blocking modal dialog with an OK button (ideal for confirmation/warning).
  - Bot edits original message via `editMessageText` or `editMessageReplyMarkup` to show resolution and disable/remove buttons.

#### Reply Keyboards & ForceReply
- **ReplyKeyboardMarkup**: Displays custom keyboard replacing native device keyboard.
  - `is_persistent: true`: Remains visible across turns.
  - `resize_keyboard: true`: Shrinks keyboard height to fit button labels.
  - `one_time_keyboard: true`: Auto-hides after first button press.
  - `input_field_placeholder`: Gray hint text in message input bar.
- **ForceReply**:
  - `force_reply: true`, `input_field_placeholder: "Enter prompt for Agent X..."`, `selective: true`.
  - Causes the Telegram client to automatically focus the text input and set `reply_to_message_id` targeting the bot's prompt message.
  - Enables effortless contextual steering without operator needing to manually swipe/reply.

---

### 1.4 In-Place Message Editing & Live Status Boards

- **Methods**: `editMessageText`, `editMessageCaption`, `editMessageMedia`, `editMessageReplyMarkup`.
- **Limits & Constraints**:
  - Max text length: **4096 UTF-16 code units**.
  - Rate limit: **~1 to 3 edits per second per message**. Exceeding this triggers HTTP 429 `Too Many Requests` with `retry_after`.
  - Message age: Cannot edit messages older than **48 hours**.
  - Message identity: Requires `chat_id` and `message_id` (or `inline_message_id`).
- **Best Practice for Agent Progress**:
  - Debounce edits (1000–1500ms intervals).
  - Use visual progress bars (e.g. `[██████░░░░] 60%`), elapsed duration clocks, and current tool step indicators.
  - On turn completion, perform final edit stamping ✅ / ❌ / 🛑.

---

### 1.5 Media, Screenshots, Diffs & File Limits

| Media Type | Method | Upload Limit (Bot API) | Download Limit (`getFile`) | Constraints / Notes |
| :--- | :--- | :--- | :--- | :--- |
| **Photo** | `sendPhoto` | 10 MB (file) / 50 MB (URL) | ~20 MB | Auto-compressed by Telegram; caption max 1024 chars. Ideal for UI test screenshots. |
| **Document** | `sendDocument` | 50 MB (file) / 2000 MB (local server) | **20 MB limit** via official Bot API | Uncompressed. Ideal for code diffs (`.diff`, `.patch`), logs (`.log`, `.json`), or full terminal session captures. |
| **Media Group** | `sendMediaGroup` | Up to 10 items | N/A | Combines 2–10 photos/docs into a single album. Ideal for before/after visual QA comparisons. |
| **Voice Note** | `sendVoice` | 50 MB | 20 MB | Must be `.ogg` with OPUS codec. |
| **Video Note** | `sendVideoNote` | 50 MB | 20 MB | Circular 1:1 video, max 60s, max 640x640 resolution. |

---

### 1.6 Telegram Mini Apps (Web Apps) for Rich UIs

Mini Apps run full HTML5/CSS/JavaScript web applications directly inside the Telegram client (modal webview or full-screen).

#### A. Launching Mini Apps
1. **Inline Button**: `InlineKeyboardButton(text="View Diff", web_app=WebAppInfo(url="https://..."))`.
2. **Menu Button**: `setChatMenuButton` sets global bottom-left menu button to launch the Mini App.
3. **Direct Link**: `https://t.me/botusername/appname?startapp=session_id`.

#### B. Cryptographic Authentication (`initData` HMAC-SHA256)
When a Mini App opens, Telegram injects `Telegram.WebApp.initData` into the webview. The backend verifies authentic operator identity without passwords:

```
data_check_string = key1=value1\nkey2=value2... (all keys except 'hash', sorted alphabetically)
secret_key = HMAC_SHA256(bot_token, "WebAppData")
calculated_hash = hex(HMAC_SHA256(data_check_string, secret_key))
is_valid = (calculated_hash == received_hash)
```
- Replay attack prevention: Verify `auth_date` is within acceptable window (e.g. < 24h).
- Contains operator user ID, first name, username, and query context.

#### C. Ideal Use Cases in Agent Management
- **Side-by-Side Diff Viewer**: Syntax-highlighted visual diffs for pending PRs with inline line-by-line commenting.
- **Live Terminal / REPL**: Stream live session terminal output via WebSockets into an xterm.js container.
- **Superboard Kanban**: Mobile-friendly board showing card transitions (`Building` -> `QA` -> `Review` -> `Done`).
- **Config & Model Switcher**: Slider controls for model parameters, provider quotas, and rate limit overrides.

---

### 1.7 Bot API Webhooks vs. Long Polling

| Feature | Long Polling (`getUpdates`) | Webhook (`setWebhook`) |
| :--- | :--- | :--- |
| **Network Setup** | Outbound HTTPS requests only; no public IP, open ports, or domain needed. | Inbound HTTPS required; needs public IP/domain, valid SSL cert (ports 443, 80, 88, 8443). |
| **Local Dev / Desktop** | **Ideal**: Runs behind NAT/firewalls on developer laptops and headless workstations. | Needs reverse tunnel (ngrok, cloudflared) or public ingress. |
| **Latency** | Immediate when connection open (long-poll timeout 20–50s). | Immediate on update arrival. |
| **Reliability** | Controlled offset recovery; redrives missed updates on process restart. | Telegram retries on non-200 with backoff; dropped if server unreachable. |
| **Concurrency** | Single polling process per bot token; concurrent pollers cause 409 Conflict. | Scalable across serverless functions or web worker clusters. |
| **Security** | Internal only; token kept local. | Requires validating `X-Telegram-Bot-Api-Secret-Token`. |
| **Verdict for Veyyon** | **Long Polling is superior** for local workstations and private dev servers. | Reserved for hosted/cloud multi-tenant deployments. |

---

### 1.8 Commands, Scopes, Reactions & Formatting Pitfalls

#### Scoped Commands (`setMyCommands`)
- Commands must be 1–32 characters, lowercase alphanumeric + underscores.
- **Scopes**:
  - `BotCommandScopeDefault`: Global fallback.
  - `BotCommandScopeAllPrivateChats`: Clean menu for private DMs (`/status`, `/agents`, `/cancel`, `/help`).
  - `BotCommandScopeChat(chat_id)`: Tailored commands for specific forum supergroup.
  - `BotCommandScopeChatAdministrators(chat_id)`: Restricts destructive commands (`/release`, `/stop_all`) to admins.

#### Message Reactions (Bot API 7.0+)
- `setMessageReaction(chat_id, message_id, reaction=[ReactionTypeEmoji(emoji="👍")])`.
- Perfect for instant acknowledgment of inbound steering commands without sending a full text message.

#### Business & Paid Features to IGNORE
- **Ignore**: Telegram Stars, paid gifts, paid media (`sendPaidMedia`), Star subscriptions, business stories, channel direct messages.
- Note on Secretary/Business Mode: Useful for personal assistant bots answering external contacts (as in `pi-telegram-manager`), but unnecessary and overly complex for a private internal agent harness.

#### Formatting Pitfalls: MarkdownV2 vs. HTML

| Aspect | MarkdownV2 | HTML (`parse_mode: "HTML"`) |
| :--- | :--- | :--- |
| **Reserved Characters** | **18 characters must be escaped everywhere**: `_ * [ ] ( ) ~ > # + - = | { } . ! \` | **Only 3 characters require escaping**: `&` (`&amp;`), `<` (`&lt;`), `>` (`&gt;`). |
| **Failure Mode** | Any unescaped dot in a path (`C:\app\file.ts`) or version (`v1.0.0`) throws HTTP 400 `Bad Request: can't parse entities`. | Unescaped text inside tags renders safely or drops tag; predictable sanitization. |
| **Code Blocks** | Requires triple backticks with precise closing. | `<pre><code class="language-ts">...</code></pre>` is robust and clean. |
| **Expandable Quotes** | `**>Expandable quote**` | `<blockquote expandable>...</blockquote>` (Bot API 7.5+). |
| **Recommendation** | **DO NOT USE in automated code/agent pipelines.** | **MANDATORY**: Standardize on HTML parsing with strict `escapeHtml()`. |

---

### 1.9 Telegram Rate Limits & Platform Ceilings

- **Global Bot Limit**: **30 messages per second** across all chats combined.
- **Per-Chat Private Limit**: **1 message per second** (short bursts of up to 3 messages permitted, but sustained bursts trigger 429).
- **Group / Supergroup Limit**: **20 messages per minute**.
- **Message Edit Limit**: **~1–3 edits per second** per message.
- **Max Message Length**: **4,096 characters** (UTF-16 code units).
- **Max Media Caption**: **1,024 characters**.
- **Max Callback Data**: **64 bytes**.
- **Max Bot Upload File Size**: **50 MB** (via standard Bot API cloud endpoint).
- **Max Bot Download File Size**: **20 MB** (via `getFile` endpoint).

---

## 2. Survey of Existing Telegram Agent-Control Implementations

| Project / Implementation | Architecture & Core Tech | Reusable Elements | Limitations / Gaps |
| :--- | :--- | :--- | :--- |
| **`m62624/pi-telegram-manager`** | Node/TS extension for Pi coding agent. Uses Bot API 9.3+ `has_topics_enabled` (3 topics: personal, manager, log). Long polling, Plugmem memory, PTY mirroring. | - Tool activity cards (`assistant.toolActivity`) with live elapsed clocks and folding.<br>- Auto-attaching large tool outputs as `.txt`/`.log` files (up to 25MB).<br>- Dynamic topic rotation per session.<br>- Inbound file/image disk download and path resolution. | Coupled to Pi agent runtime; includes heavy Business/Secretary mode not needed for internal agent control. |
| **Claude Code Telegram Bridges** (`claude-code-telegram`, community scripts) | Spawns `claude` CLI inside PTY (`node-pty`), scrapes stdout, streams to Telegram DM via `editMessageText`. Regex parses permission prompts (`[y/n]`) into inline buttons. | - Interactive permission interception via inline keyboards (`[Approve] [Reject]`).<br>- ANSI escape sequence stripping.<br>- Simple PTY lifecycle control. | Screen-scraping ANSI terminal output is notoriously fragile; no native subagent awareness; flat DM only (no topics). |
| **grammY Agent Ecosystem** (`grammyjs/runner`, `conversations`, `menu`) | Modern TypeScript bot framework with concurrent runner, stateful conversational plugins, and fluent menus. | - `@grammyjs/runner`: High-throughput concurrent update processing with automatic head-of-line unblocking.<br>- `@grammyjs/auto-retry`: Battle-tested exponential backoff for Telegram HTTP 429 rate limits.<br>- `@grammyjs/menu`: Declarative inline keyboard generation with automated callback routing. | Framework/SDK only; requires domain-specific agent integration layer. |
| **Herdr Multi-Agent Engine** (`herdr-veyyon-integration`) | Rust-based terminal agent multiplexer and supervisor. Exposes IPC socket with structured JSON-RPC API across workspaces, tabs, and panes. | - Unified `AgentStatus` lifecycle (`Idle`, `Working`, `Blocked`, `Done`).<br>- `AgentPromptParams` with status-wait triggers.<br>- `AgentReadParams` with structured formats (Text, Ansi, stripped).<br>- Universal agent supervision (Veyyon, Pi, Claude Code, Codex). | Native terminal/GUI multiplexer; currently lacks a native Telegram bot interface. |

---

## 3. Existing Stack Inventory (`C:/Users/wkiri/.veyyon/telegram`)

### 3.1 Implemented Capabilities

| Component | Source File | Implemented Features |
| :--- | :--- | :--- |
| **Bot Pool Coordinator** | `coordinator.ts`, `bot_pool.db` | - Atomic lease acquisition and release with TTL heartbeats (20s TTL, 5s tick).<br>- Project affinity routing (`preferredProjects` matched against `projectCwd`).<br>- Win32 process liveness verification via FFI (`kernel32.dll` `OpenProcess`, `GetProcessTimes`).<br>- Multi-slot manifest support (`manifest.json`: polysim, soundcore, dubai-holding, etc.). |
| **Outbound Correlation Bridge** | `types.ts`, `coordinator.ts` | - Records every outbound message: `(bot_id, chat_id, message_id) -> (session_id, request_id, decision_id)`.<br>- Reply provenance gate: verifies `reply_to_message_id` matches current session, rejecting foreign/stale replies fail-closed. |
| **Decision Callbacks** | `types.ts`, `poller.ts`, `coordinator.ts` | - Validates tokens from inline button callbacks against `decision_callbacks` table.<br>- Consumes token, updates Telegram message to "Decision Resolved", and executes `decision_workflow.py resolve-callback`. |
| **Long Poller & Ingest Ledger** | `poller.ts` | - Transactional update ledger (`update_ledger` table in WAL mode).<br>- Monotonic offset tracking with redrive of pending updates.<br>- Allowlist DM policy enforcement (`accessConfig.allowFrom`).<br>- Inbound routing: idle sessions receive user messages; busy sessions receive steering inputs. |
| **Message Streaming** | `index.ts` | - Multi-chunk message splitter (`chunkMessage` at 3800 chars).<br>- Debounced in-place message editing (1500ms debounce) for live assistant generation. |
| **Security & Tool Guard** | `guard.ts`, `sanitizer.ts` | - `DangerousToolGuard` blocks high-risk operations (git push, delete, secret access) initiated from remote Telegram turns.<br>- `redactSecrets` filters tokens and sensitive patterns before dispatch. |

### 3.2 Critical Gaps in Existing Implementation

1. **No Topic / Forum Support**: All messages are dispatched to a single flat DM (`primaryChatId`). Cannot isolate concurrent sessions or background agents.
2. **Subagent Exclusion**: `index.ts` explicitly rejects subagents via `isSubagent(ctx)` and binds only to the root interactive session. No visibility into subagent worker swarms.
3. **No Inbound Media Handling**: Inbound photos/documents are collapsed to text strings (`"<photo>"`, `"<file: name>"`). Files are never downloaded, saved to disk, or fed to vision/file inspection tools.
4. **No Outbound Media Pipeline**: No capability to send screenshots, diff patches, test recordings, or full log files.
5. **No Mini App (Web App) Integration**: Lacks webview interfaces for diffs, kanban, or terminal streams.
6. **No Pinned Fleet Dashboard**: Status is only returned on on-demand `/status` commands; lacks a persistent pinned overview card.
7. **No Scoped Bot Commands**: Bot commands are not registered with Telegram via `setMyCommands`.
8. **No Message Reactions**: Cannot send emoji reactions for quick non-text acknowledgments.

---

## 4. Thin, Harness-Agnostic Adapter Contract

To ensure the Telegram harness does not over-engineer or tightly couple to Veyyon's internal daemon, the interface between the Telegram transport layer and the agent backend is defined by a clean, 6-operation contract:

```typescript
export interface AgentBackendAdapter {
  /** 1. Enumerate active sessions, background workers, and their status */
  listAgents(): Promise<AgentSummary[]>;

  /** 2. Inject user instruction or steering prompt into an agent */
  promptAgent(targetId: string, text: string, options?: { steer?: boolean }): Promise<PromptResult>;

  /** 3. Resolve a structured pending decision or approval */
  resolveDecision(decisionId: string, choiceId: string, rationale?: string): Promise<boolean>;

  /** 4. Abort or interrupt an active turn or tool execution */
  abortAgent(targetId: string): Promise<boolean>;

  /** 5. Read recent output stream or status for an agent */
  readOutput(targetId: string, lines?: number): Promise<{ text: string; status: AgentStatus }>;

  /** 6. Fetch artifacts (screenshots, diffs, logs) and usage metrics */
  getArtifacts(targetId: string): Promise<AgentArtifact[]>;
}

export type AgentStatus = "idle" | "working" | "blocked" | "done" | "unknown";

export interface AgentSummary {
  id: string;
  name: string;
  topic?: string;
  status: AgentStatus;
  cwd: string;
  model?: string;
  activeTool?: string;
  blocker?: string;
}

export interface AgentArtifact {
  id: string;
  type: "photo" | "document" | "diff";
  localPath: string;
  caption?: string;
}
```

### Herdr Socket API as the Ideal Universal Backend
Herdr already implements this exact schema over its local IPC socket (`src/api/schema/agents.rs`):
- `agents.list` -> maps directly to `listAgents()` (`AgentInfo`: status, PID, cwd, title, tokens).
- `agents.prompt` -> maps directly to `promptAgent()` (`AgentPromptParams`).
- `agents.read` -> maps directly to `readOutput()` (`AgentReadParams`: Text/Ansi/stripped).
- `agents.wait` -> enables Telegram to wait on agent state transitions without polling.
- `agents.send_keys` -> enables sending Ctrl+C or input sequences to abort or unblock agents.

Targeting this contract means the Telegram harness can run identically against:
- **Veyyon**: via PR #946 native control host or extension.
- **Herdr**: via Herdr Unix socket / named pipe.
- **Claude Code / Pi / Codex**: via local adapter script wrapping the CLI / PTY.

---

## 5. Recommended Architecture: Veyyon Telegram Agent Management Harness ("T3 Code in Telegram")

```
                                  TELEGRAM CLIENT (Mobile / Desktop)
                                                │
                 ┌──────────────────────────────┼──────────────────────────────┐
                 ▼                              ▼                              ▼
      [Forum Topic: Fleet]           [Forum Topic: Task-123]         [Mini App: Diff/Terminal]
     - Pinned Live Dashboard        - Agent Conversation            - Full side-by-side diff
     - High-level controls          - Live Tool Cards               - Interactive Monaco view
     - Blocker Notifications        - Inline Decision Buttons       - Terminal replay stream
                 │                              │                              │
                 │ (Long Polling / Updates)     │ (Replies & Callbacks)        │ (initData HMAC Auth)
                 ▼                              ▼                              ▼
  ┌──────────────────────────────────────────────────────────────────────────────────────────┐
  │                           TELEGRAM NATIVE CONTROL HARNESS                                │
  │                                                                                          │
  │   ┌─────────────────────┐   ┌───────────────────────────┐   ┌─────────────────────────┐  │
  │   │  Bot Pool Lease Mgr │   │  Topic & Session Router   │   │  Mini App Static Server │  │
  │   │  (bot_pool.db)      │   │  (Thread ID <-> Target ID)│   │  (Local Port / Tunnel)  │  │
  │   └─────────────────────┘   └───────────────────────────┘   └─────────────────────────┘  │
  │                                           │                                              │
  │                                           ▼                                              │
  │                       ┌───────────────────────────────────────┐                          │
  │                       │   Thin AgentBackendAdapter Contract   │                          │
  │                       └───────────────────┬───────────────────┘                          │
  └───────────────────────────────────────────┼──────────────────────────────────────────────┘
                                              ▼
               ┌──────────────────────────────┼──────────────────────────────┐
               ▼                              ▼                              ▼
      [Herdr Socket API]           [Veyyon Native Bridge]         [Claude Code / Pi PTY]
      (Supervised fleet)           (PR #946 In-Process)           (Direct CLI processes)
```

### 5.1 Topology & Chat Structure
- **Forum Supergroup** (`is_forum: true`):
  - **General / Fleet Topic (`message_thread_id: 1` or null)**:
    - Single **Pinned Message** continuously updated in place with fleet health: active sessions, RAM usage, model allowances, active PRs, and top-level blockers.
    - Consolidated notifications for milestone completions and critical system alerts.
  - **Dynamic Session/Agent Topics**:
    - Created on demand via `createForumTopic` when a task starts: `[PR #4543] BackendFix`, `[Review] PolicyGate`.
    - All prompt interactions, tool cards, streaming text, and agent steering occur strictly within that topic.
    - On task completion, topic is renamed with prefix `[Done]` and closed (`closeForumTopic`) to archive it cleanly.

### 5.2 Live Agent Status & Tool Cards
- **Tool Lifecycle Mirroring**:
  - `tool_call` event -> Sends or edits tool status card: `⚙️ Running bash: npm run test...`
  - `tool_end` event -> Edits card in place: `✅ bash: npm run test (passed in 3.4s)`.
  - Output overflow -> If output > 2,000 chars, creates `.log` or `.diff` file and sends via `sendDocument`.

### 5.3 Decision & Approval Pipeline
- When an agent reaches a decision blocker (e.g. migration apply, PR merge, architecture choice):
  - Bot sends a formatted HTML card with an `InlineKeyboardMarkup`:
    - `[✅ Approve & Merge]` (`callback_data: "d:<id>:app"`)
    - `[❌ Request Changes]` (`callback_data: "d:<id>:rej"`)
    - `[🔍 View Diff]` (`web_app: { url: "https://.../diff/<id>" }`)
  - Bot simultaneously arms a `ForceReply` prompt message for optional free-text rationale.
  - Tapping a button fires `answerCallbackQuery` immediately, updates the card to "Resolved: Approved", and injects the decision into the engine.

### 5.4 Mini App Integration (Diffs & Terminals)
- Run a lightweight HTTP server in the local host (e.g. `http://127.0.0.1:4040` exposed via local network or Cloudflare Tunnel).
- Host a React/Preact bundle implementing:
  1. **Monaco / Diff Viewer**: Inspect PR diffs with file trees.
  2. **Terminal Stream**: Connects via WebSocket to stream subagent logs.
- Authenticate via `Telegram.WebApp.initData` using standard HMAC-SHA256 validation against the bot token.

### 5.5 Media & Vision Pipeline
- **Screenshots**: Automated QA agents send UI screenshots via `sendPhoto` directly into the session topic.
- **Before/After Comparisons**: Sent via `sendMediaGroup` (2 photos side by side).
- **Inbound Operator Images**: If operator uploads a mobile screenshot or diagram in the topic, poller downloads file via `getFile`, saves to disk, and dispatches to agent with `inspect_image` tool.

---

## 6. Comparison Table: Existing vs. Gaps vs. Proposed

| Capability Area | Installed Extension (`~/.veyyon/telegram`) | `pi-telegram-manager` | Proposed Harness Architecture |
| :--- | :--- | :--- | :--- |
| **Channel Model** | Single flat DM | DM with 3 topics (personal, manager, log) | Forum Supergroup + Bot DM topics (1 topic per session/agent) |
| **Agent Multiplexing** | Root session only; subagents blocked | Single session only (no swarm) | Full multi-agent fleet multiplexed by `message_thread_id` |
| **Status Boards** | On-demand text replies to `/status` | Pinned mode message in personal topic | Continuously edited pinned fleet dashboard + live agent tool cards |
| **Approvals** | Inline decision buttons + reply correlation | None (runs autonomously or interactive DM) | Inline decision buttons + `ForceReply` fallback + Mini App review |
| **Media Handling** | Stubs inbound media as text strings | Downloads files & photos to disk; tool outputs as docs | Full bidirectional media: screenshots (`sendPhoto`), albums (`sendMediaGroup`), diffs (`sendDocument`), vision ingest |
| **Rich UI / Web Apps**| None | None | Mini App with `initData` HMAC auth for interactive diffs and terminals |
| **Command System** | Hand-parsed in text handler (`/status`, etc.) | Terminal and chat commands | Registered scoped commands via `setMyCommands` |
| **Reactions** | None | None | `setMessageReaction` for instant non-verbal acknowledgments |
| **Harness Binding** | Deeply coupled to Veyyon Extension API | Deeply coupled to Pi agent runtime | Decoupled thin `AgentBackendAdapter` (Herdr, Veyyon, Pi, Claude) |
