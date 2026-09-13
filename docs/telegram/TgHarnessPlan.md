# Telegram agent-management harness — implementation plan

## 1. Vision
Manage the agents on my PC from Telegram, not by watching the terminal: “i want teh full system here kidn aof agent management”; “soemethign very simluar to t3code but int elgram”; and “I want to be able to reply also to specific telegram posts and give a full status update and the usage right now”. Start with seeing agents, sending instructions, answering decisions and viewing evidence; expand to creating sessions only after that works. Keep the existing desktop information density, keep free-text replies, and do not turn every tool call into a notification. These are verbatim excerpts from the recovered operator requests, not corrected transcriptions.

Moved from [Bavariance/polysimulator#4794](https://github.com/Bavariance/polysimulator/issues/4794).

This is a dedicated implementation proposal, not permission to implement, merge or deploy. Owner: harness implementation lane after approval; state: awaiting M1 approval; code/PR/head: none yet.

## 2. Architecture — reuse first
- Run one standalone bot daemon on the PC using the installed extension's Bun/TypeScript long-polling transport: outbound HTTPS only, no public server, new framework, cloud service or second coordinator. It survives an agent exit; it cannot report while the PC itself is offline.
- Keep one thin **AgentHarnessAdapter** contract: **list_sessions/state, prompt, answer/approve, abort, artifacts, usage**. Every result names its backend/session and freshness; unsupported operations say unavailable. Backend-specific code stays behind this contract; do not pretend terminal input is structured approval.
- **HerdrAdapter** is the universal backend candidate: socket discovery/state and targeted prompts for existing panes first; later Pi and Claude Code through Herdr, not new direct adapters. Its terminal state does not supply reliable typed decisions or provider quotas by itself. Validate the installed wire protocol rather than copying illustrative method names from research.
- **VeyyonAdapter** uses the GUI-host socket for rich session/subagent state, targeted prompts and typed decisions; later diffs and usage. Existing source is not proof that a running host exposes it: verify connectivity and session targeting before M1 work. Identify a Veyyon session seen through both backends once, or label uncertain duplicates rather than guessing.
- **REUSE** installed session affinity, durable reply provenance, decision callbacks, media sending/redaction and exclusive bot lease/update recovery. **NEW**: standalone ownership, the adapter boundary, two minimal socket clients and command/card rendering. Extend existing storage only for backend/session and topic/card references; do not build another task ledger. Keep Superboard/GitHub as work truth.
- Telegram UX: **forum topic per session**, one **live-edited pinned status card**, and a compact fleet list; names, project, state and last update visible without opening details. M1 uses the existing private chat and explicit session picker; M2 adds topics after group/user authorization and topic/pin permissions are verified. Never simply relax the existing private-chat guard.
- Decisions use clearly labeled **inline buttons plus free text always**: explain the problem, proposed action and risk in plain language; at most one optional details link. A text reply is guidance, not automatic authorization. Use native media for screenshots and short diff summaries plus full document attachments; no tool-call message stream. A **Mini App is M4 only**.

## 3. Milestones
Sizes are relative implementation scope, not delivery guarantees. Only M1 is proposed for approval now; later milestones remain planned and require separate approval.

### M1 — See, prompt and answer existing agents · M
**Outcome:** A smallest usable end-to-end slice in the existing private bot chat, targeted for one working day once the existing Herdr and Veyyon sockets are available.
**Scope (files/repo):** Harness-source fork's installed Telegram extension modules (poller, coordinator, types, entrypoint and existing focused tests), plus a daemon entrypoint and two thin adapter modules. Bavariance/polysimulator holds this tracking issue only; no product code changes. Reuse existing decision/media helpers, not a replacement bridge.
**Out of scope:** Topics, session creation, provider switching, full transcripts, quota dashboards, generic terminal approval parsing, native host refactors, Mini App and Git writes.
**Acceptance:**
- `/agents` shows one real Herdr-managed agent and one real Veyyon session with live state; a disconnected socket displays unavailable/stale, never idle. Record the observed state against the local host.
- `/prompt` selects either session and delivers a harmless prompt exactly to that target; the resulting response is visible in Telegram. Busy delivery is explicitly reported as queued/steered only if supported, otherwise rejected without injection.
- A real Veyyon or existing workflow decision displays context, labeled buttons and a free-text option; a button resolves only its bound decision once. Free text reaches that decision as guidance; expired, repeated and wrong-user actions cannot approve anything. Herdr-only untyped prompts remain text-only, with no invented Approve button.
- Native reply-to an earlier post still routes to its original session with quoted provenance, even after selecting another session; an unbound/ambiguous reply asks for a target rather than guessing. Reuse the completed routing work, but run this regression in the new daemon.
- One actual session screenshot appears as a native Telegram image with the correct session caption; a large or unreadable image can be opened as its original document, without revealing secrets or unrelated files.
- Restart the daemon: the lease prevents a second poller, consumed input/decisions are not replayed, and ordinary prompts still work. Demonstrate commands, decisions, replies and image readability in real Telegram desktop and mobile clients; Main coordinates operator-visible sends. No synthetic transport-only pass counts as completion.
**Dependencies:** Existing authorized bot route/lease, existing reply-routing implementation, accessible Herdr socket and Veyyon GUI-host socket with typed interaction support, one harmless pending decision and screenshot. If a socket requires native repair, report that prerequisite separately: do not silently substitute a mock or enlarge the one-day slice. Cutover explicitly releases the old poller; rollback restores its ownership without two owners.

### M2 — Session rooms and quiet live oversight · M
**Outcome:** Switch between sessions, see live progress and stop a task without scrolling through tool noise.
**Scope (files/repo):** Same harness Telegram router/card renderer and adapters; reuse existing status, usage and crash-notification integrations where available. No new ledger or monitoring service.
**Out of scope:** Session spawning, arbitrary shell execution, fleet-wide kill, model changes and Mini App.
**Acceptance:**
- Two concurrent sessions have separate forum topics and pinned cards; messages, callbacks and replies cannot cross sessions. Unauthorized group members are rejected even in an authorized group; private-chat selection remains usable if topics are unavailable.
- Cards update observed working/blocked/idle/offline states without a new message for each tool call. `/status` shows completed work, outstanding tasks and blockers from available sources; `/usage` reports source/time, allowance remaining and reset windows, or explicitly unavailable. Subscription allowance is not presented as a per-token bill.
- Stop targets one chosen active session/turn and visibly confirms its observed result; another running session continues. Reconnecting refreshes stale cards rather than claiming a stopped or disconnected process is healthy.
- An actual unexpected agent exit causes one out-of-process alert while the bot remains alive; a requested stop is distinguished from a crash. A hands-on PC step produces a clear one-line instruction at the time needed and a subsequent outcome, without raw tool-call spam.
**Dependencies:** M1 routing/ownership; bot topic/pin rights and explicit actor/chat allowlists; backend event or bounded refresh support; existing usage/watchdog sources where available. Persist pending questions and deduplicated reminders in the existing decision workflow, not a second reminder system.

### M3 — Start isolated work and exchange evidence · L
**Outcome:** Create or resume a session from Telegram, guide it with a screenshot and inspect the resulting changes.
**Scope (files/repo):** Same harness adapters/commands and existing backend session/worktree services; Veyyon rich artifact operations; Pi and Claude Code compatibility through Herdr. Backend changes, if needed, receive their own scoped PRs in the respective forks.
**Out of scope:** New direct Pi/Claude adapters, blanket permission escalation, automatic merges/deployments, general-purpose remote shell and interactive web terminal.
**Acceptance:**
- Create/resume selects an allowlisted project and supported model/mode, shows the chosen settings, then opens the correct session; two new coding tasks use different worktrees. Unsupported settings are refused rather than silently ignored; subagent creation/list/stop is offered only where natively supported.
- Repeat prompt, reply routing and targeted stop with a real Pi session and a real Claude Code session through Herdr; the same Telegram commands work without provider-specific bot code.
- A photo/document with caption reaches only the selected session through its supported attachment interface; unsupported media ingestion and oversize/unsafe files produce an explicit error, not a fake successful text-only delivery.
- A real changed-file summary, full diff document and screenshot album can be inspected on mobile and desktop, preserving full files for download and a compact desktop summary. PR/work status links point to the actual existing GitHub records; no Git mutation is implied by viewing evidence.
**Dependencies:** M1 contract and verified backend creation/worktree/media capabilities; M2 topic mapping for session-room creation. Artifact rendering can proceed independently once M1 routing is available; do not serialize it behind unrelated M2 usage work.

### M4 — Optional richer inspection, not a second IDE · L
**Outcome:** Open a Mini App only when Telegram attachments are insufficient for inspecting a long transcript or side-by-side diff.
**Scope (files/repo):** A small viewer in the harness-source fork, consuming the same read-only artifact/session contract; explicit approved HTTPS hosting arrangement before implementation.
**Out of scope:** Terminal emulator, another agent runtime, new kanban database, billing, broad configuration console and replacing native chat.
**Acceptance:**
- Open a session's long transcript or diff from Telegram and inspect the correct content; ordinary buttons, free text and downloadable files continue to work without opening the Mini App.
- Verify Telegram-signed identity, freshness and session authorization server-side; expired/replayed credentials and another user's session are rejected. No bot token reaches the browser.
- Real interface proof at 1440px desktop and 320px/390px mobile: desktop retains dense side-by-side review; mobile has readable single-column navigation with no clipped controls.
**Dependencies:** M3 artifacts, separately approved hosting/security scope and demonstrated need beyond native media. M4 does not block M1–M3.

## 4. Risks and limits
- **Rate limits:** Coalesce status edits, prioritize questions/results, obey Telegram `429 retry_after`, and cap/paginate text to the API limit; do not assume a guaranteed edits-per-second allowance. A delayed card shows its last refresh. See [Bot FAQ](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this).
- **Callbacks:** `callback_data` is 1–64 bytes, not 64 characters. Send a short opaque lookup token; bind the stored action to actor/chat/session/decision and expiry, acknowledge the callback promptly and reject duplicate consumption. See [InlineKeyboardButton](https://core.telegram.org/bots/api#inlinekeyboardbutton).
- **48-hour edit window:** The research overgeneralized this: Telegram documents 48 hours for business messages not sent by the bot and without an inline keyboard, not a blanket age limit on the bot's own status cards. If an edit is refused or a card disappears, replace/re-pin it and update the reference. See [editMessageText](https://core.telegram.org/bots/api#editmessagetext).
- **Single poller per token:** Reuse the exclusive lease and durable offset; do not run the old extension and new daemon against one token simultaneously. Long polling and an active webhook are mutually exclusive. See [getUpdates](https://core.telegram.org/bots/api#getupdates).
- **Secrets and permissions:** Bot tokens stay in the existing operator-managed secret store; local sockets remain local and access-restricted. Telegram bot chats are not end-to-end encrypted: redact sensitive content, restrict artifact roots/types/sizes and validate actors on every command/callback. Never expose arbitrary file reads or treat terminal text as permission to execute a command.
- **Backend truth:** Research maps source capabilities, not proven installed socket compatibility. Herdr terminal heuristics may be ambiguous; mark unknown rather than infer success, approval or quota. No automatic hot-switch of running models is promised.

## 5. Sources and approval
Sources: [T3 Code](https://github.com/pingdotgg/t3code), [Herdr source](https://github.com/herdrdev/herdr) and [automation docs](https://herdr.dev/docs/agent-automation/), [ccgram reuse reference](https://github.com/alexei-led/ccgram), [Claude Code Telegram bridge](https://github.com/RichardAtCT/claude-code-telegram), [existing Pi Telegram manager](https://github.com/Wladefant/pi-telegram-manager), [Telegram topics](https://core.telegram.org/bots/api#createforumtopic), [media](https://core.telegram.org/bots/api#sendphoto), and [Mini App validation](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app). Borrow relevant interaction patterns; do not import their tool-call spam, terminal-scraping approvals or whole frameworks into the installed extension.

**Approve M1 as scoped so you can list live agents, prompt a chosen session, answer decisions and view a screenshot from Telegram, with no session creation or Mini App yet — A: approve M1 / B: change M1 (say what)?**
