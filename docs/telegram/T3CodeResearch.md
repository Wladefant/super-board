# T3 Code & Modern AI Agent Management UIs: Comparative Research, Ecosystem Survey & Harness-Agnostic Telegram Contract

**Author:** T3CodeResearch Lane (Veyyon Subagent)  
**Date:** 2026-09-08  
**Scope:** Deep competitive analysis of T3 Code (Theo Browne / Ping.gg) and seven comparable agent-management interfaces (Herdr, Claude Code Remote Control, Cursor 3 Glass, OpenCode Share, Conductor.build, Vibe Kanban, and OpenAI Codex App/Web). Expanded with an ecosystem survey of existing Telegram agent bridges on GitHub, reusable architecture patterns, and a thin, harness-agnostic adapter contract.

---

## 1. Executive Summary & Paradigm Shift

Between 2024 and 2026, AI coding tools transitioned through three distinct architectural eras:
1. **Single-Agent Autocomplete / Chat Sidebar** (GitHub Copilot, Cursor Composer 1/2, ChatGPT web): Synchronous, single-threaded, locking the developer in an edit-and-wait loop.
2. **Parallel Agent Execution via Worktrees** (T3 Code, Conductor, Cursor 3 Glass, BloopAI Vibe Kanban, OpenAI Codex App): Treating human attention as the scarce resource. One human directs 3 to 15 concurrent agents working in isolated `git worktree` environments or cloud sandboxes.
3. **Headless Remote Control & Ambient Management** (Claude Code Remote Control, T3 Connect, Herdr SSH, Telegram Agent Bridges): Decoupling the compute host (local workstation or cloud server) from the human interface surface (mobile app, web app, terminal, chat platform).

The operator's goal—**"something very similar to T3 Code, but in Telegram"**—is architecturally compelling:
- Telegram provides native **Supergroup Forum Topics** (`message_thread_id`), mapping 1:1 to isolated agent sessions / worktrees.
- Telegram provides **In-Place Message Editing** (`editMessageText`), enabling live status cards without notification noise.
- Telegram provides **Inline Keyboards** (`InlineKeyboardMarkup`), enabling one-tap tool approvals and git operations from a smartwatch or phone lockscreen.
- Telegram provides **Media Albums** (`sendMediaGroup`), rendering multi-viewport screenshots (1440px desktop + 390px mobile) natively.
- Telegram provides **Telegram Mini Apps** (TMA / WebApp), offering embedded split diff viewers and interactive xterm.js terminals when rich webviews are strictly needed.

Crucially, this Telegram harness **MUST NOT BE OVER-ENGINEERED or tightly coupled to a single proprietary runtime**. By defining a **thin, harness-agnostic adapter contract**, the identical Telegram bot layer seamlessly manages Veyyon sessions, Claude Code, OpenAI Codex, OpenCode, or Herdr-managed agent panes.

---

## 2. In-Depth Product Deconstructions

---

### 2.1. T3 Code (Theo Browne / Ping.gg)

- **Primary Repository / Site:** `https://github.com/pingdotgg/t3code` | `https://t3.codes/` | `https://app.t3.codes`
- **Primary Sources:**
  - *pingdotgg/t3code README.md* (2026): "T3 Code is an 'agent harness control surface'. It enables control of the agents on your machine with a best-in-class mobile app (iOS, Android), web app, and Electron-based desktop app."
  - *BetterStack Guide: An Open-Source GUI for Managing AI Coding Agents* (2026-03): Detailed architecture breakdown.
  - *T3 Code Documentation: Permission Modes, Remote Access, Internals* (`docs/user/permission-modes.md`, `docs/user/remote-access.md`, `docs/internals/overview.md`).

#### What a User Can SEE:
- **Three-Panel Layout:**
  - *Left Sidebar:* Local Git repositories / projects, with project favicons, active branch, and uncommitted status badges.
  - *Middle Panel (Thread List):* All threads/sessions for the selected project. Each thread represents a distinct task or conversation with status indicators (`working`, `idle`, `waiting for approval`, `error`) and pull request status badges (`open`, `merged`, `closed`).
  - *Right Main Panel:* Interactive conversation, live plan visualization, step-by-step progress, and real-time tool calls.
- **Diff Viewer:** Turn-by-turn diffs showing exact file modifications at each step of the agent's plan, with toggles for unified and split side-by-side views.
- **Checkpoints:** Workspace snapshots captured via hidden Git refs without cluttering user branch history, allowing rollback to any prior turn.
- **Integrated Terminal & Quick Actions:** Embedded terminal tabs, plus project-scoped one-click action buttons (e.g. `npm run dev`, `cargo test`) that run shell commands in the thread's workspace.
- **Usage & Settings:** Active model identifier, reasoning effort level, BYOK API status, and environment resource utilization (CPU/RAM across connected machines).

#### What a User Can DO:
- **Start Agent & Thread:** Launch a new thread from prompt, issue, or cloned repository.
- **Configure Execution Mode:**
  - *Model Selector:* Dropdown supporting Codex, Claude Code, Cursor, Grok Build, OpenCode, and Antigravity.
  - *Reasoning Level:* Slider/toggle controlling planning depth before execution.
  - *Chat vs. Plan Mode:* Chat for iterative Q&A; Plan mode for structured codebase inspection and execution checklists.
  - *Permission Modes:* Four distinct levels: `Supervised` (approvals required for edits and commands), `Auto-accept edits` (file changes automatic, commands prompt), `Auto` (provider heuristic auto-review), and `Full access` (unsupervised).
- **Human-in-the-Loop Interventions:** Approve or reject tool calls, answer agent questions inline, or select "Always allow this session" for recurring command patterns.
- **Stop / Cancel:** Abort mid-turn execution immediately without killing the background server.
- **Source Control Automation:** Single-click **"Commit, Push, & Create PR"** that auto-generates commit messages, pushes feature branches, and opens GitHub/GitLab/Bitbucket/Azure PRs with pre-filled AI summaries.

#### Multi-Agent Orchestration:
- **Git Worktree Isolation:** Native `git worktree` integration. Each new thread can launch into an isolated worktree on a dedicated branch. Multiple agents execute simultaneously across different features without branch or lockfile collision.
- **T3 Connect Remote Relay:** Client-server RPC architecture. Background server runs on workstation or remote host (`npx t3 serve`); mobile (iOS/Android) and web clients connect via authenticated secure relay, local LAN QR pairing, Tailscale HTTPS, or desktop SSH tunnels. Multi-machine thread load balancing distributes tasks across available host machines based on CPU/memory telemetry.

---

### 2.2. Herdr (Herdr Dev)

- **Primary Repository / Site:** `https://github.com/herdrdev/herdr` | `https://herdr.dev/`
- **Local Integration Reference:** `C:/Users/wkiri/development/herdr-veyyon-integration`
- **Primary Sources:**
  - *BetterStack Guide: Herdr: Terminal Multiplexer with Built-in AI Agent State Awareness* (2026-06): "A terminal-native agent runtime and multiplexer built in Rust with the Ratatui TUI library... processes running in Herdr panes are identified as agents and their state (working, idle, blocked) is displayed in a sidebar."
  - *Herdr Documentation:* `https://herdr.dev/docs/agent-automation/`
  - *Herdr IPC Source Code:* `src/api/mod.rs`, `src/api/client.rs`, `src/api/server.rs` (Interprocess local socket IPC).

#### What a User Can SEE:
- **TUI Dashboard (Ratatui / Rust):**
  - *Spaces Sidebar (Left):* Workspaces, each scoped to a project context.
  - *Agents List (Bottom Left):* Detected AI agent processes with real-time status badges:
    - `working`: Agent actively executing tools / generating code.
    - `idle`: Ready to accept next prompt.
    - `blocked`: Waiting for human input (permission prompts, CLI confirmation).
  - *Main Area:* Tiled terminal panes (split vertical/horizontal) running live shell/agent processes.
  - *Navigator (Bottom):* Keybinding hints.
- **System Toasts & Sound Notifications:** Audio chimes and OS popups when an agent transitions from `working` to `blocked` or `idle` in a background pane.

#### What a User Can DO:
- Manage panes and workspaces using tmux-like keybindings (`Ctrl+b` prefix) or mouse clicks.
- Switch focus directly to any blocked agent to provide CLI input or approve permissions.
- Remote control via `herdr --remote ssh://user@remote-host`.

#### Multi-Agent Orchestration & Socket API:
- **Herdr Socket API (`HERDR_SOCKET_PATH`):** Exposes an IPC socket over `interprocess::local_socket`.
- Supports direct programmatic methods: `AgentStart`, `AgentPrompt`, `AgentSendKeys`, `WorktreeCreate`, `WorktreeOpen`, `WorktreeRemove`, `PaneReportAgentSession`, `PaneClose`.
- **Universal Multiplexer:** Herdr can host Claude Code, Codex, OpenCode, Veyyon, or raw shell scripts inside terminal panes. Its socket API can serve as a universal backend for external bridges.

---

### 2.3. Claude Code Remote Control (Anthropic)

- **Primary URL:** `https://code.claude.com/docs/en/remote-control`
- **Primary Sources:**
  - *Anthropic Official Documentation: Continue local sessions from any device with Remote Control* (v2.1.248, 2026): "Connects claude.ai/code or the Claude app for iOS and Android to a Claude Code session running on your machine... filesystem access and code execution stay on your machine."
  - *VentureBeat / DataCamp Guides* (2026-03).

#### What a User Can SEE:
- **Session List on Web (`claude.ai/code`) & Mobile App:** List of online sessions with computer icon, host prefix (e.g. `myhost-graceful-unicorn`), and live status dot (green = connected).
- **Live Streamed Conversation & Tool Output:** Full turn execution, compaction progress, tool arguments, and stdout streams mirrored from local terminal.
- **Subagent & Dynamic Workflow Status:** Hierarchy of active subagents and workflows running in background.
- **Diff Pane:** Live diff of uncommitted local changes, or branch diff against default branch if working tree is clean.
- **Proactive Mobile Reminders:** Terminal triggers notifications when a turn runs long ("Still working — check in from phone") or after multiple permission prompts ("Approve tool calls from phone").

#### What a User Can DO:
- **Remote Prompting & Stepping:** Send prompts mid-turn (queued automatically) or start new turns from phone or browser.
- **Approve Permissions:** Remote approval of file writes, bash commands, and MCP tool invocations.
- **Remote Photo/File Uploads:** Take photo on phone or attach screenshot; Claude Code streams it to local session as native multimodal input or downloads files to project directory.
- **Model Switching:** Switch model dynamically (`/model`) from remote device.
- **Stop / Archive:** Cancel running turns or archive sessions remotely.

#### Multi-Agent Orchestration:
- **`--spawn worktree` Mode:** Running `claude remote-control --spawn worktree --capacity 32` enables concurrent on-demand sessions. Each incoming remote task is allocated an isolated Git worktree automatically.

---

### 2.4. Cursor 3 "Glass" / Background & Cloud Agents (Anysphere)

- **Primary URL:** `https://cursor.com/changelog/3-0` | `https://cursor.com/docs/cloud-agent`
- **Primary Sources:**
  - *DEV.to / Gabriel Anhaia: Cursor 3 'Glass' Replaced Composer with an Agents Window* (2026-04-27): "April 2, 2026, Cursor shipped version 3 under the codename 'Glass'... Composer is gone. In its place: an Agents Window where you spin up multiple agents in parallel... closer in shape to a small Kubernetes dashboard than to a chat window."
  - *Cursor Documentation: Cloud Agents & Models & Pricing* (2026-04).

#### What a User Can SEE:
- **Agents Window:** Full-screen dashboard showing tiled Agent Tabs in side-by-side or multi-column grid layout.
- **Per-Agent Scratchpad & Diffs:** Each agent maintains its own scratchpad, diff inspector, and conversation log.
- **Design Mode:** Interactive rendered browser preview where users click/annotate DOM elements ("make this button align with that one") to feed structured UI feedback directly to agents.
- **Token & Cost Telemetry:** Real-time visibility into frontier model Max Mode multipliers and cumulative token consumption per tab.

#### What a User Can DO:
- Launch parallel agents into local worktrees, cloud sandboxes, or remote SSH targets.
- Steer agents via direct annotations in the rendered UI preview.
- Trigger merge reconciliation agents to resolve multi-worktree diffs.

#### Multi-Agent Orchestration:
- Designed specifically for multi-file refactors (e.g. Agent 1 on API endpoint, Agent 2 on audit logger, Agent 3 on test suite). Worktrees prevent collisions; a follow-up "merge agent" reconciles the three worktree branches into a clean PR.

---

### 2.5. OpenCode Share (Anomaly Co.)

- **Primary URL:** `https://opencode.ai/docs/share/`
- **Primary Sources:**
  - *OpenCode Official Docs: Share* (2026-09-08): "OpenCode’s share feature allows you to create public links to your OpenCode conversations... makes the conversation accessible via the shareable link — `opncd.ai/s/<share-id>`."

#### What a User Can SEE:
- Clean web view of the entire conversation transcript, tool invocations, code blocks, diffs, and session metadata.
- Read-only shared view for team review and debugging.

#### What a User Can DO:
- Run `/share` in CLI to copy public/team link to clipboard.
- Configure `share: "auto"`, `"manual"`, or `"disabled"` in `opencode.json`.
- Run `/unshare` to instantly revoke URL and delete cloud-stored transcript.

---

### 2.6. Conductor (Melty Labs)

- **Primary URL:** `https://www.conductor.build/`
- **Primary Sources:**
  - *CodePick Guide: Conductor.build: Run a Team of Parallel AI Coding Agents on Your Mac* (2026-05-16): "Pioneered the 'agentic parallel runner' category... orchestrates parallel Claude Code and Codex agents, each working in an isolated git worktree."

#### What a User Can SEE:
- Multi-agent workspace dashboard displaying status, active command, current file being edited, and blocker state across 3 to 5 simultaneous workspaces.
- Side-by-side diff viewers for each workspace branch.

#### What a User Can DO:
- Click "New Workspace" to automatically execute `git worktree add` on a fresh branch.
- Start Claude Code or Codex inside that workspace using local session credentials (BYOL).
- Linear issue picker: select Linear ticket to automatically initialize workspace name, branch, and agent prompt.
- In-app diff review, GitHub PR creation, and branch merge.

---

### 2.7. Vibe Kanban (BloopAI)

- **Primary Repository / Site:** `https://github.com/BloopAI/vibe-kanban` | `https://vibekanban.com/`
- **Primary Sources:**
  - *BloopAI/vibe-kanban README.md* (2026): "Kanban board for orchestrating coding agents... Use kanban issues to plan work... create workspaces where coding agents can execute."

#### What a User Can SEE:
- **Kanban Board:** Columns for backlog, planning, running workspaces, review, and done.
- **Workspace Inspector:** Dedicated view per workspace containing an isolated Git branch, embedded terminal, dev server output, and unified diff viewer.
- **Embedded Browser Preview:** Built-in Chromium preview with devtools, inspect mode, and responsive device emulation (mobile vs desktop).

#### What a User Can DO:
- Move tasks across columns; assign tasks to coding agents (supporting 10+ CLIs: Claude Code, Codex, Gemini CLI, Cursor, OpenCode, Amp).
- Review diffs and leave inline comments that are sent back to the agent as corrective prompts.
- Open PRs with AI-generated titles and descriptions.

---

### 2.8. OpenAI Codex App & Cloud (OpenAI)

- **Primary URL:** `https://openai.com/index/introducing-the-codex-app/`
- **Primary Sources:**
  - *OpenAI Announcement: Introducing the Codex app* (2026-02-02, Windows update 2026-03-04): "A command center for AI coding and software development with multiple agents, parallel workflows, and long-running tasks."

#### What a User Can SEE:
- **Project & Thread Command Center:** Multi-tasking sidebar organizing parallel agent threads by project.
- **Turn-by-turn Diffs:** Inline file changes with comment capability and "Open in Editor" links.
- **Skills Library UI:** Dedicated interface to browse, enable, and inspect agent skills (Figma UI implementation, Linear issue triage, cloud deployments, image generation).

#### What a User Can DO:
- Direct teams of agents across design, build, test, and cloud deploy phases.
- Invoke skills explicitly (`$skill-name`) or let the agent auto-select skills based on prompt.
- Review diffs and comment inline.

---

## 3. Survey of Existing Telegram Agent Bridges & Reusable Patterns

A targeted audit of open-source Telegram coding agent bridges reveals several proven architectural patterns and concrete reusable components:

### 3.1. Existing Implementations Survey

1. **`terranc/claude-telegram-bot-bridge`**
   - *Architecture:* Node.js (`node-telegram-bot-api`) spawning `claude` CLI in a child process.
   - *Approval Mechanism:* Regex parsing of stdout for CLI confirmation prompts (`[y/n]`, tool execution confirmation). Injects an `InlineKeyboardMarkup` with Approve/Deny buttons; on callback query, writes `y\n` or `n\n` into the child process's stdin.
   - *Reusable Value:* Clean regex patterns for detecting interactive approval prompts on terminal agents without native API hooks.

2. **`RichardAtCT/claude-code-telegram`**
   - *Architecture:* Python `python-telegram-bot` (v20+ async) wrapper.
   - *Throttled Streaming:* Avoids Telegram rate-limit 429 errors by buffering agent stdout tokens and editing the current message via `edit_message_text` on a strict 1.5-second debounce timer.
   - *Topic Routing:* Utilizes Telegram supergroup forum topics (`message_thread_id`) to map one topic per Claude Code session.
   - *Reusable Value:* Production-hardened streaming debounce loop; Forum Topic message routing.

3. **`MackDing/CodexClaw`**
   - *Architecture:* Multi-backend agent router supporting both Claude Code and OpenAI Codex CLI.
   - *Model & Mode Switching:* Implements `/model`, `/supervised`, and `/plan` bot commands that dynamically alter backend CLI flags or session JSON configuration.
   - *Reusable Value:* Normalized command dispatch for multi-provider switching.

4. **`benedict2310/telecodex`**
   - *Architecture:* TypeScript bridge for OpenAI Codex SDK.
   - *Diff Handling:* Formats `git diff` output into truncated syntax-highlighted code blocks (`<pre><code class="language-diff">`), attaching larger diffs as `.patch` documents.
   - *Reusable Value:* Telegram-safe diff truncation and formatting helper.

5. **`opencode-telegram-bot` & `cc-telegram-bridge`**
   - *Architecture:* Connects to OpenCode's local HTTP/JSON-RPC server rather than scraping terminal stdout.
   - *Structured Decisions:* Receives structured JSON approval requests from OpenCode (`tool_call`, `arguments`, `risk_level`), eliminating flaky terminal regex scraping.
   - *Reusable Value:* Proof that structured RPC integration is vastly superior to raw terminal PTY scraping.

6. **Local Workstation Stack (`C:/Users/wkiri/.veyyon/telegram` & `Wladefant/super-board`)**
   - *Existing Modules:* `poller.ts`, `types.ts`, `coordinator.ts`, `bot_pool.db` (SQLite WAL).
   - *Capabilities:* SQLite-backed bot token pool, lease status management, session affinity, durable outbound message correlation, and decision callback resolution (`reject_expired`, `reject_already_consumed`).
   - *Reusable Value:* 100% reusable local storage and poller foundation. Only needs the thin harness-agnostic adapter layer on top.

---

## 4. The Thin, Harness-Agnostic Adapter Contract

To ensure the Telegram bridge does **NOT over-engineer** and works universally across Veyyon, Herdr, Claude Code, OpenAI Codex, or OpenCode, we define a lean TypeScript/Python adapter interface.

### 4.1. Core Data Schema

```typescript
// 1. Unified Agent & Session State
export interface AgentState {
  sessionId: string;          // Unique session/thread identifier
  name: string;               // Display title (e.g. "PR-4545: Policy QA")
  project: string;            // Repository or workspace name
  state: "working" | "blocked" | "idle" | "error";
  currentStep?: string;       // Concise active task (e.g. "Running tsc --noEmit")
  activeModel?: string;       // e.g. "gemini-3.8-flash", "codex-spark", "sonnet-4.6"
  worktreePath?: string;      // Absolute worktree path on host
  branch?: string;            // Git branch name
  updatedAt: number;          // Epoch timestamp
}

// 2. Structured Decision / Approval Gate
export interface PendingDecision {
  decisionId: string;         // Short UUID or hash (fits in 64-byte Telegram callback_data)
  sessionId: string;          // Target session
  prompt: string;             // Human-readable decision explanation
  toolName?: string;          // e.g. "bash", "edit", "database_migration"
  toolArgs?: Record<string, unknown>; // Arguments being inspected
  options: Array<{
    id: string;               // e.g. "approve", "reject", "always_allow"
    label: string;            // e.g. "✅ Approve", "❌ Reject"
    style?: "primary" | "danger" | "default";
  }>;
  expiresAt?: number;
}

// 3. Artifacts & Diffs
export interface ArtifactSummary {
  sessionId: string;
  diffSummary?: string;       // Formatted diff text (< 50 lines)
  diffPatchPath?: string;     // Path to full .patch file on disk
  screenshots?: string[];     // Paths to PNG/JPEG screenshots
  webAppUrl?: string;         // Optional Mini App URL for interactive split diff
}

// 4. Usage & Telemetry
export interface UsageReport {
  sessionId?: string;
  totalTokens?: number;
  inputTokens?: number;
  outputTokens?: number;
  costEstimateUsd?: number;
  quotaWindowReset?: string;  // e.g. "4h 58m remaining in 5h window"
}
```

### 4.2. Universal Adapter Interface (`AgentHarnessAdapter`)

```typescript
export interface AgentHarnessAdapter {
  // --- Discovery ---
  listSessions(): Promise<AgentState[]>;
  getSession(sessionId: string): Promise<AgentState | null>;

  // --- Steering & Human-in-the-Loop ---
  prompt(sessionId: string, text: string, attachments?: string[]): Promise<void>;
  answer(sessionId: string, text: string): Promise<void>;
  approve(sessionId: string, decisionId: string, actionId: string, reason?: string): Promise<void>;
  abort(sessionId: string): Promise<void>;

  // --- Artifacts & Telemetry ---
  getArtifacts(sessionId: string): Promise<ArtifactSummary>;
  getUsage(sessionId?: string): Promise<UsageReport>;

  // --- Push Event Hooks (Adapter -> Telegram Poller) ---
  onStateChange?(handler: (state: AgentState) => void): void;
  onDecisionRequired?(handler: (decision: PendingDecision) => void): void;
  onArtifactReady?(handler: (artifact: ArtifactSummary) => void): void;
}
```

### 4.3. Backend Evaluation: Herdr Socket API as Universal Backend

Evaluating Herdr's local socket API (`herdr-veyyon-integration/src/api/mod.rs`):
- **Why Herdr is an ideal universal backend:** Herdr acts as a terminal multiplexer that hosts CLI agents in panes. It already detects process states (`working`, `idle`, `blocked`) and exposes `AgentStart`, `AgentPrompt`, `AgentSendKeys`, `WorktreeCreate`, and `PaneReportAgentSession` over an interprocess local socket.
- **Architectural Advantage:** Instead of building custom adapters for Claude Code, Codex, and OpenCode, a single **HerdrAdapter** can talk to Herdr's socket API. Any agent running inside a Herdr pane is immediately visible, steerable, and approvable through the Telegram harness without touching agent internals.

---

## 5. Feature Matrix: T3 Code vs. Telegram-Feasible (Ranked by Value)

| Rank | Capability | T3 Code Implementation | Telegram-Feasible Implementation | Feasibility Level | Value | Primary Rationale & UX Impact |
| :---: | :--- | :--- | :--- | :---: | :---: | :--- |
| **1** | **Action & Permission Approvals** | Modal prompt in chat; Supervised / Auto-accept / Full access | `InlineKeyboardMarkup` decision card with `[ Approve ]`, `[ Reject ]`, `[ Always Allow ]` + `answerCallbackQuery` toast | **100% Native** | **CRITICAL** | Core operator bottleneck. Enables instant one-tap approvals on smartwatch or mobile lockscreen without opening an IDE. |
| **2** | **Session / Thread Multiplexing** | Three-panel layout with project sidebar and middle thread list | Supergroup **Forum Topics** (`message_thread_id`). 1 topic per agent session / worktree; General topic (#1) as status board | **100% Native** | **CRITICAL** | Replaces multi-pane desktop UI with Telegram's native multi-topic mobile UI. Unread badges indicate blocked agents immediately. |
| **3** | **Live Agent State Tracking** | Turn progress spinner, plan steps, and status badges (working/idle/blocked) | Pinned message per Forum Topic updated via `editMessageText`: status emoji (`🟢/🟡/⚪/🔴`), tool, elapsed time, current plan step | **100% Native** | **HIGH** | Eliminates polling and terminal checking. A glance at the pinned header shows whether an agent is grinding or blocked. |
| **4** | **Prompting & Question Answering** | Composer input box with Plan vs Chat mode | Direct message sent to topic; agent questions prompt `ForceReply` or reply-to correlation to route directly to active session | **100% Native** | **HIGH** | Operator can steer agents or answer clarifying questions on the go via standard mobile messaging. |
| **5** | **Screenshots & Visual Verification** | Snapshot tool / embedded browser preview | `sendPhoto` for single captures; `sendMediaGroup` for dual-viewport responsive albums (desktop 1440px + mobile 390px) | **100% Native** | **HIGH** | Direct image rendering in Telegram stream without external hosting requirements. Operator sees immediate proof. |
| **6** | **Git PR & Commit Automation** | One-click "Commit, Push, & Create PR" with auto-generated title/body | Inline buttons `[ 🚀 Commit & Push ]`, `[ 🔀 Create PR ]`; bot invokes git/GitHub API and returns clickable PR URL | **100% Native** | **HIGH** | One-tap shipping directly from phone when an agent finishes its task cleanly. |
| **7** | **Inbound Mobile Multimodal** | File attachment picker in desktop app | Operator takes photo or screenshot on phone, sends directly to topic with caption; bot downloads via `getFile` and passes to vision model | **100% Native** | **HIGH** | Unrivaled mobile workflow: snap photo of whiteboard/UI bug -> agent executes fix. |
| **8** | **Git Worktree Parallelism** | Automatic `git worktree add` for each new thread | Backend harness automatically allocates `.wt-<session_id>` worktree upon topic creation, binding topic ID to worktree | **100% Native (Backend)** | **HIGH** | Guarantees complete filesystem isolation across concurrent agents without terminal collisions. |
| **9** | **Diff Review** | Turn-by-turn unified and side-by-side split diff viewer | Short diffs in `<pre><code class="language-diff">`; medium diffs as `.patch` via `sendDocument`; massive diffs via Telegram Mini App (TMA) | **Hybrid (Chat + TMA)** | **HIGH** | Short diffs read natively in chat; complex 50-file diffs open in a smooth embedded Telegram Mini App sheet. |
| **10** | **Stop / Pause / Cancel** | Stop button in composer to abort active turn | Inline button `[ ⏹️ Stop Agent ]` or `/stop` command in topic; signals local process tree gracefully | **100% Native** | **MEDIUM** | Instant interrupt capability prevents wasted token spend. |
| **11** | **Usage, Quota & Budget Alerts** | BYOK spend tracking and connected machine resource telemetry | Live token/cost counter on pinned status card; `/usage` command; proactive notifications when approaching provider rate limits | **100% Native** | **MEDIUM** | Crucial for managing 5-hour reset windows and subscription rate limits (e.g. Codex/Claude). |
| **12** | **Model & Reasoning Switching** | Dropdown selector for Codex, Claude, Cursor, OpenCode, Antigravity | Inline keyboard menu `[ 🤖 Model: Flash ▾ ]` opening model selection grid; updates active session config | **100% Native** | **MEDIUM** | Enables Flash-first default with on-demand escalation to Codex or Sonnet. |
| **13** | **Integrated Terminal / Shell** | Embedded xterm pane inside application | Command stdout summaries rendered in code blocks; interactive PTY supported via Telegram Mini App running xterm.js over WebSocket | **TMA Required for PTY** | **MEDIUM** | Telegram chat does not have a native VT100 widget; TMA provides identical web experience. |
| **14** | **Skills & Quick Actions** | Project quick actions (`npm run dev`) and `@skill-name` prompts | Custom bot commands (`/test`, `/lint`) and persistent inline action buttons per topic | **100% Native** | **MEDIUM** | Provides one-tap execution of frequent development commands. |
| **15** | **Kanban / Superboard View** | Not found in T3 Code core (present in Vibe Kanban & Conductor) | Summary matrix pinned in General topic; full interactive Kanban board via Telegram Mini App | **Hybrid (Chat + TMA)** | **LOW** | Daily oversight is better served by topic alerts; a full Kanban board is accessible via Mini App button when desired. |

---

## 6. Unsourced Claims Audit

- **T3 Code Built-in Kanban Board:** `not found` (T3 Code uses a 3-panel thread list; Kanban boards are found in Vibe Kanban, Multica, and Superboard, not T3 Code).
- **T3 Code Interactive Terminal in Mobile App:** `not found` (T3 Code mobile app supports thread viewing, diffs, and prompts, but interactive PTY terminal is desktop-only).

---

## 7. Sourced References & Citations

1. **T3 Code:**
   - GitHub Repository: `https://github.com/pingdotgg/t3code` (Ping.gg, 2026)
   - Official Site: `https://t3.codes/` | Web App: `https://app.t3.codes`
   - BetterStack: *An Open-Source GUI for Managing AI Coding Agents* (March 2026, `https://betterstack.com/community/guides/ai/t3-code/`)
2. **Herdr:**
   - GitHub Repository: `https://github.com/herdrdev/herdr` (Herdr Dev, 2026)
   - Local Source: `C:/Users/wkiri/development/herdr-veyyon-integration` (`src/api/mod.rs`)
   - BetterStack: *Herdr: Terminal Multiplexer with Built-in AI Agent State Awareness* (June 2026, `https://betterstack.com/community/guides/ai/herdr-ai-agent/`)
3. **Claude Code Remote Control:**
   - Anthropic Official Documentation: `https://code.claude.com/docs/en/remote-control` (Claude Code v2.1.248, 2026)
4. **Cursor 3 "Glass":**
   - Cursor Changelog: `https://cursor.com/changelog/3-0` | `https://cursor.com/docs/cloud-agent` (April 2026)
   - DEV.to (Gabriel Anhaia): *Cursor 3 'Glass' Replaced Composer with an Agents Window* (April 27, 2026, `https://dev.to/gabrielanhaia/cursor-3-glass-replaced-composer-with-an-agents-window-1pcg`)
5. **OpenCode Share:**
   - OpenCode Documentation: `https://opencode.ai/docs/share/` (Anomaly Co., September 2026)
6. **Conductor.build:**
   - CodePick Guide: *Conductor.build: Run a Team of Parallel AI Coding Agents on Your Mac* (May 16, 2026, `https://codepick.dev/en/guides/conductor-build-intro/`)
7. **Vibe Kanban:**
   - BloopAI/vibe-kanban README: `https://github.com/BloopAI/vibe-kanban` (2026)
8. **OpenAI Codex App:**
   - OpenAI Announcement: *Introducing the Codex app* (February 2, 2026, `https://openai.com/index/introducing-the-codex-app/`)
9. **Telegram Bot Agent Bridges:**
   - `terranc/claude-telegram-bot-bridge` (2026)
   - `RichardAtCT/claude-code-telegram` (2026)
   - `MackDing/CodexClaw` (2026)
   - `benedict2310/telecodex` (2026)
   - `C:/Users/wkiri/.veyyon/telegram` (`types.ts`, `poller.ts`, `bot_pool.db`)
