# Operator Request Inventory & Traced Requirements: Telegram Agent-Management Harness

**Date:** 2026-09-08  
**Author / Lane:** `TgHarnessPrompts`  
**Target:** Recovery of the operator's original request(s) for a Telegram-native agent-management harness, verbatim chronological statements, architectural survey, and traced consolidated requirements.

---

## 1. Executive Summary & Context

The operator has requested a **full, Telegram-native agent-management harness** ("something very similar to T3 Code, but in Telegram") to supervise, inspect, prompt, steer, and interact with coding agents running locally on their PC workstation.

This document recovers every operator statement from September 1, 2026 onward across session transcripts, GitHub issues (#4622, #4543), Mnemopi long-term memory, standing orchestration policies, and live operator steering interjections. Each statement is presented verbatim with exact timestamps, session IDs, transcript line numbers, and file paths. From these statements, a consolidated requirements specification is derived, with every requirement traced directly to its origin quote.

In accordance with operator steering received on 2026-09-08:
1. The design must **NOT be over-engineered** (avoiding endless layers of ledgers, speculative adapters, or review ceremony).
2. The design must be **harness-agnostic**, using a thin adapter contract (`list sessions/agents + state`, `prompt`, `answer/approve`, `abort`, `artifacts/usage`) compatible across Veyyon, Herdr, Pi/omp, Claude Code, and Codex.
3. It explicitly surveys existing reference implementations (e.g., `ccgram`, Claude Code Telegram bridges, grammY/Telegraf bot frameworks, and Herdr socket API integration).

---

## 2. Research Methodology & Evidence Sources

- **Session Transcripts Stream-Parsed:** 6,633 session `.jsonl` files in `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/` were evaluated. All 89 root session files were streamed line-by-line (never loaded whole into memory) for user messages matching `telegram` + (`harness`|`management`|`agents`|`t3`|`t3code`|`control`|`manage`|`bot`).
- **Primary Active Sessions:**
  - `01a0496f-64f6-733e-a9a6-89f15fc2a437.jsonl` (Active main session, 77.4 MB): contains all live September 2026 operator interactions regarding Telegram harness requirements, inbound steering, anti-spam constraints, post-specific replies, and crash monitoring.
  - `01a019b6-8d29-7000-9adb-e88becc9e167.jsonl` (Historical main session, 126.6 MB): verified clean (0 telegram messages in Sep 2026; ran Aug 19 – Sep 4).
  - `01a0198b-e17e-7000-b245-abbd01f6a687.jsonl` (Historical session): contains earlier foundational operator sentiment regarding Telegram setup importance.
- **GitHub Issues:**
  - `Bavariance/polysimulator#4622`: *feat(harness): Session-isolated Telegram inbound input and routing* (dedicated tracking issue for inbound input, post-specific replies, session affinity, and lease exclusion).
  - `Bavariance/polysimulator#4543`: *feat(harness): Request-to-Evidence Ledger, Superboard taxonomy & asynchronous decision workflow* (records Telegram one-sentence notifier, decision contract specifications, and synthetic test boundaries).
- **Mnemopi Long-Term Memories:**
  - `memory://48076783b5cd8a47` (2026-09-07): "Implement Telegram native management system/harness for Veyyon"
  - `memory://8046d8c83f1041e1` (2026-09-07): "User wants a telegram-native management system/harness for Veyyon"
  - `memory://d9e45ab7cc902896` (2026-09-08): "Wants telegram-native management/harness support"
  - `memory://294aa21e00a23778` (2026-09-08): "Replies to specific Telegram posts: preserve which post you replied to and route your full reply to the correct session or task."
- **Live Operator Steering Interjection:**
  - Received 2026-09-08 09:27 UTC via parent agent `main:01a0496f-64f6-733e-a9a6-89f15fc2a437`.

---

## 3. Verbatim Chronological Record of Operator Statements

### [Q-01] Foundational Telegram Setup Valuation
- **Timestamp:** 2026-08-19T13:12:45.946Z
- **Source File:** `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-19T10-23-12-765Z_01a0198b-e17e-7000-b245-abbd01f6a687.jsonl`
- **Line Number:** 267
- **Verbatim Text:**
> "also teh telegram setup is extraordinaly improtant to me just liek i used with claude code th whole time, i think you can read much mroe in teh global claude.md setup maybe you can"

### [Q-02] Inbound Reception & Concurrent Session Isolation
- **Timestamp:** 2026-09-05T23:10:27.175Z
- **Source File:** `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437.jsonl`
- **Line Number:** 13946
- **Verbatim Text:**
> "also what about the telegram emssage i sedn direclty in teelgram do you receive it them as input i think tha taws aimpletned is it still therer? why do you not process telegram replies lets also wrok with that and rmeber that we can have mutlipe session in teh same rpoejct and of couse replies are normally only for one session etc. but i think that was impeltend more or less fine"

### [Q-03] Verification Defect Closure Mandate
- **Timestamp:** 2026-09-06T11:01:02.375Z
- **Source File:** `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437.jsonl`
- **Line Number:** 17677
- **Verbatim Text:**
> "also you did not even imepltntin teh telegram inboudn messages seeminly gand so omuc more, did nt we way oune of teh main ruels si taht o never clsoe anythgin util you are 100 percent sure it works and is fixed etc."

### [Q-04] Question UX: Plain Language, No Link Dumps
- **Timestamp:** 2026-09-06T13:41:11.586Z
- **Source File:** `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437.jsonl`
- **Line Number:** 19459
- **Verbatim Text:**
> "also teh qeustion i get sent to telegram are not good withotu clear ifnroamtion weithotu clear question and too many links o undersnta anythgin. so thibnk about it liek this i did not udnerstn any queiston sent you and can not sresovle one blcoe for now. and did you finally buti la system so that no todo s were forgot?"

### [Q-05] Restart Instructions & Durable Blocker Documentation
- **Timestamp:** 2026-09-06T22:13:45.149Z
- **Source File:** `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437.jsonl`
- **Line Number:** 21576
- **Verbatim Text:**
> "so firstly give a me a new doucmten of every new and all teh blcoerks and so and eveyrhgin you need aetc. so taht i can rstart with telegram input support etc. and then i can udnersta where the are problem coudl be also"

### [Q-06] Anti-Spam Mandate: No Tool Call Spam in Telegram
- **Timestamp:** 2026-09-06T22:48:32.157Z
- **Source File:** `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437.jsonl`
- **Line Number:** 21760
- **Verbatim Text:**
> "inboud seems to work quite well whi is very cool so contienuw erhe y;ou left off i want you to constantly run 15 agnets finsihgi every single thing on teh agenda every singe thign, and also havin every signel tool call on telegram is nto nromal so lets not amkes. into the gaol 15 agent workign on all teh topics including 3 4 56 adn so o nev ery single topi c shodul be worked on in parallel and every single tool call in telgram is not normal and that is hwy id doi not liek it, aslo kalso al the 4. soemthgin prs kan be mrged every singel one and make now make finally sure to work witl every epseicll teh hcatgtp codex mdoels and gemin e.8 flash to alway have agent runnign and wokrign on all thsoe rpoblm and dotn foreget merging"

### [Q-07] Foundational System Request: Telegram-Native Agent Harness
- **Timestamp:** 2026-09-07T22:13:07.758Z
- **Source File:** `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437.jsonl`
- **Line Number:** 23211
- **Verbatim Text:**
> "I want you to get really deep into this stuff into the telegram implementation go into the telegram imentetion that should rpbaley be part of the superboard system I want you to almost create a full veryyon irreclty in telegram start at least 3 agents all sol high to research and iment telegram stuff I want this to be top of the shelf. You should get code or rather find things the top implantation I want you to adjust veyyon accordingly too now that we hab the fork and overall try to push everything to our forks now in detail, I want almost Egyptian veyyon and the Rui can do here too if possible firstly let's tweet right basic desires like seeing and reading agents and so on but then we can kinda expand into creating new seeeeiin and so so much more I want a telegram native kind of amanhemrnt system or like harness which of course always runs and fully on pc but with full support on telegram including images for example does it have it already?"

*(Note on transcription: "Egyptian veyyon and the Rui" = "everything veyyon and the GUI", "tweet right" = "treat right", "seeeeiin" = "sessions", "amanhemrnt" = "management").*

### [Q-08] Dedicated Crash Notification Agent
- **Timestamp:** 2026-09-07T22:55:13.195Z
- **Source File:** `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437.jsonl`
- **Line Number:** 23536
- **Verbatim Text:**
> "Crete a systems so I get a telegram messsge when the session crashes ima seperate agne r"

### [Q-09] Post-Specific Replies, Full Status Updates & Allowance Usage
- **Timestamp:** 2026-09-08T07:24:05.427Z
- **Source File:** `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437.jsonl`
- **Line Number:** 24317
- **Verbatim Text:**
> "I want to be able to reply also to specific telegram posts and give a full status update and the usage right now"

### [Q-10] Avoid Over-Engineering; Keep Telegram Reports
- **Timestamp:** 2026-09-08T07:47:27.345Z
- **Source File:** `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437.jsonl`
- **Line Number:** 24549
- **Verbatim Text:**
> "i am very very unhappy with gpt 6 astra orchstrating and i want you to anlayze th ewhoel last 4 days or so to understandhn where the oerrs are and so on. it was a bi overengieenred and alsomst not work was doen at teh end. keep in mind fable 5.1 is crazy expensive. so firstly we shoudl nwo never use opus 5 models, almost never. becuase we need all models for fable 5.1 also i want you to resrach waht theo uses shoudl i use fabl 5.1 medium or high for orehstroat what can you see is better. also based on the session analysie adjust everyghin needed to fully impelntt and o the work liek i said. also i want you to anlayze overalll of course always with subanget becuase rember orchstraot use as less tokens as possible liek amost nothgin and doa all ork and even strong thinkin through mdoels liek gpt 6 astra, but lets change teh config o gpt6 astra to med as there is alsmot not diffetn between med and high. dont foreget to merge and test . also as you can clearly see we ahve a massive amoutsn of backlog which you shodlfinally wrokt rhoguh so we can look intot eh future finally dont ofeget about telegram reporst etc. remeber that"

### [Q-11] Real-Time Alert for Every Hands-On PC Step
- **Timestamp:** 2026-09-08T07:55:01.409Z
- **Source File:** `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437.jsonl`
- **Line Number:** 24623
- **Verbatim Text:**
> "i lcoasd and then another chrome windows openee. also those thigns need to boe sent to telegram so i sthat seen them that you want to close teh chroem etc. other wise i dont see them remeber that and write it dwon ina getsn md"

### [Q-12] Live Proof of Contextual Telegram Reply
- **Timestamp:** 2026-09-08T09:13:38.227Z
- **Source File:** `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437.jsonl`
- **Line Number:** 25061
- **Verbatim Text:**
> "[Replying to Telegram post #1358 | task: req-4582-telegram-input]  
> [Original post: \"[Status Update] polysimulator req-4582-telegram-input: Reply test: please reply to THIS post with any word so I can confirm post-specific replies work. https://github.com/Bavariance/polysimulator/issues/4622\"]  
> [Note: Free-text reply; not automatic approval or authorization.]  
>  
> yes"

### [Q-13] The T3 Code Parity Directive
- **Timestamp:** 2026-09-08T09:15:56.497Z
- **Source File:** `C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-polysimulator/2026-08-28T17-33-52-246Z_01a0496f-64f6-733e-a9a6-89f15fc2a437.jsonl`
- **Line Number:** 25068
- **Verbatim Text:**
> "remember taht i want you to imepltnet the full kidna harness system in teh telgram i dont know i fyou saved it comeptley pelase find teh promtp whre i said it. i was sayng that i want teh full system here kidn aof agent management. beucase telegram bots are raelly great and a very deep functilatiy so i woudl ove to use it proelry. to be hoest soemethign very simluar to t3code but int elgram mthat is a big thign a very big thignk i woudl say so plan accoridnlgy and first reserach accordingly liek at laet 3 agents only for this one woudl say"

### [Q-14] Parent / Live Operator Steering Interjection
- **Timestamp:** 2026-09-08T09:27:00.000Z
- **Source:** Parent agent `main:01a0496f-64f6-733e-a9a6-89f15fc2a437` IRC message
- **Verbatim Text:**
> "Operator steering for the Telegram harness research (TgHarnessPrompts, T3CodeResearch, TgBotApiResearch, TgHarnessSurface): (1) also survey existing deep Telegram agent-control implementations on GitHub/elsewhere (e.g. Claude Code/Codex/opencode/Pi Telegram bridges, grammY/telegraf-based agent bots, herdr plugins) and note what is reusable; (2) do NOT over-engineer; (3) design it harness-AGNOSTIC: a thin adapter contract (list sessions/agents+state, prompt, answer/approve, abort, artifacts/usage) so the same Telegram layer works with Veyyon, Pi/omp, Claude Code, Codex, Herdr-managed agents etc. — Herdr's socket API may itself be the best universal backend; TgHarnessSurface evaluate that explicitly instead of deep Veyyon integration. Others: ignore."

---

## 4. Architectural Analysis: T3 Code vs. Telegram Agent Harness

In **[Q-13]**, the operator explicitly stated: *"something very similar to t3code but in telegram"*.

### What T3 Code (`pingdotgg/t3code`) Does:
1. **Three-Panel UI Organization:**
   - Left panel: Projects (Git repositories).
   - Middle panel: Task threads / sessions per project.
   - Right panel: Interactive interaction area (prompts, plans, streaming output, tool inspection).
2. **Model & Agent Configuration:**
   - Multi-agent provider dropdown (OpenAI Codex, Anthropic Claude Code, OpenCode, Gemini).
   - Reasoning effort selector (low / medium / high planning depth).
   - Execution modes: Chat mode (ad hoc questions) vs Plan mode (formal plan before execution).
   - Permission mode: Full access (unsupervised) vs Supervised (tool approval required).
3. **Task Isolation & Execution:**
   - Automatic `git worktree` isolation per thread so parallel tasks do not touch main or collide.
   - Live plan tracking with step-by-step completion checks.
4. **Interactive Decisions & Review:**
   - Supervised mode prompts user to approve commands/file writes before execution.
   - Turn-by-turn unified and split diff viewer.
   - Commit, push, and create GitHub PR in a single chained action.
5. **Session Persistence:**
   - Threads persist across restarts; context is retained.

### Translating T3 Code to the Telegram Bot Surface:
| T3 Code Desktop Capability | Telegram Harness Native Mapping |
| :--- | :--- |
| **Projects & Threads List** | Telegram Forum Topics (one topic per project/worktree or one topic per agent thread) or `/sessions` inline selector menu. |
| **Model & Mode Selector** | Inline Keyboard buttons (`/model`, `/mode plan|chat`, `/supervision full|ask`). |
| **Prompting & Plan View** | Direct message input to thread topic; initial message pinned with updating checklist. |
| **Tool Call / Execution Display** | *Anti-Spam Filtered:* Only high-level milestones and decision requests sent as messages; detailed tool calls collapsed or queryable via inline buttons (`[View Logs]`). |
| **Supervised Tool Approval** | Inline keyboard callback buttons (`[Approve]`, `[Reject]`, `[Edit]`) + unrestricted free-text replies. |
| **Diff & Artifact Viewer** | Rendered diff summaries sent as monospaced markdown / snippet files (`.patch`), or rendered preview images. |
| **Screenshot / UI Inspection** | Telegram native photo/media messages (`sendPhoto`, `sendMediaGroup`). |
| **Git Worktree / PR Actions** | `/pr` command or `[Create PR]` inline button invoking Git/gh CLI directly in the worktree. |

---

## 5. Survey of Existing Deep Telegram Agent Implementations

Per operator steering **[Q-14]**, existing implementations offer concrete reusable patterns:

### 1. `alexei-led/ccgram` (CCGram v1–v4)
- **Architecture:** Bridge between Telegram Bot API and local agent processes via `tmux` or `herdr`.
- **Reusable Mechanisms:**
  - **Telegram Forum Topics Mapping:** CCGram binds each Telegram topic in a supergroup to a distinct agent session or tmux/herdr pane. This provides instant multi-agent switching without cluttering a single chat.
  - **Status & Control via Commands:** `/status`, `/attach`, `/abort`, `/detach`.
  - **Terminal Scrollback & Output Paging:** Buffers agent stdout, flushes on idle, and trims output to Telegram's 4096-character limit without dropping context.
  - **Herdr Integration (v4):** Connects to Herdr's UNIX domain socket / named pipe to list panes, inspect status (`working`, `blocked`, `idle`), send input, and reap terminated panes.

### 2. Claude Code Telegram Bridges (`RichardAtCT/claude-code-telegram`, `Nickqiaoo/chatcode`, Claude Code Channels)
- **Architecture:** Interfacing Claude Code's CLI or headless server with Telegram bots.
- **Reusable Mechanisms:**
  - **Approval Interception:** Intercepts Claude Code's permission prompts (e.g. bash execution, file writes) and converts them into Telegram inline buttons (`Accept (y)`, `Deny (n)`, `Always allow`).
  - **Session Affinity & Token Storage:** Links Telegram chat IDs to local session tokens to prevent multi-user hijacking.

### 3. `herdr` Universal Agent Runtime (`herdr-veyyon-integration`)
- **Architecture:** Rust-based background multiplexer for coding agents (Claude Code, Codex, Veyyon, Pi, Opencode).
- **Socket API Capabilities:**
  - `list_panes()`: returns pane ID, running agent type, working directory, and live status (`working`, `blocked`, `idle`, `exited`).
  - `send_keys(pane_id, text)`: injects input cleanly into any agent's PTY.
  - `capture_pane(pane_id, lines)`: reads terminal buffer.
  - `create_pane(cwd, command)`: creates a fresh isolated agent session in a dedicated worktree.
- **Verdict for Telegram Harness:** Herdr's socket API provides the exact universal backend needed for a harness-agnostic architecture. Instead of deep Veyyon-specific hooks, a Telegram bridge talking to Herdr's socket can control Veyyon, Codex, Claude Code, and Pi interchangeably!

---

## 6. Consolidated Traced Requirements Matrix

Each requirement below is derived directly from the verbatim operator statements (**[Q-01]** through **[Q-14]**).

### Category 1: Architecture, Deployment & Harness-Agnostic Design

| Req ID | Requirement Description | Traced Quote(s) | Operational Specification |
| :--- | :--- | :--- | :--- |
| **REQ-ARCH-01** | **Local PC Execution with Remote Telegram Control** | **[Q-07]**, **[Q-14]** | The agent management harness executes fully on the operator's PC workstation; the Telegram bot acts as an authenticated remote interface, not a cloud replacement. |
| **REQ-ARCH-02** | **Harness-Agnostic Thin Adapter Contract** | **[Q-14]** | The Telegram harness must not couple exclusively to Veyyon internal ASTs. It must expose a generic adapter contract: `list_sessions()`, `prompt()`, `answer_decision()`, `abort()`, `get_artifacts()`, `get_usage()`. |
| **REQ-ARCH-03** | **Herdr Universal Backend Integration** | **[Q-14]** | Support Herdr's socket API as a primary universal provider, enabling seamless control of Veyyon, Claude Code, Codex, and Pi under one Telegram bot interface. |
| **REQ-ARCH-04** | **Strict Avoidance of Over-Engineering** | **[Q-10]**, **[Q-14]** | Do NOT build sprawling multi-tiered ledger abstractions, complex synthetic state machines, or endless review layers. Keep the core engine lean, responsive, and maintainable. |

### Category 2: Session & Agent Fleet Management (T3 Code Parity)

| Req ID | Requirement Description | Traced Quote(s) | Operational Specification |
| :--- | :--- | :--- | :--- |
| **REQ-FLEET-01** | **Session & Agent Visibility** | **[Q-07]**, **[Q-13]** | Operators must be able to view all active projects, sessions, running subagents, and their real-time execution states (`working`, `blocked`, `idle`, `crashed`). |
| **REQ-FLEET-02** | **Session Creation & Spawning** | **[Q-07]**, **[Q-13]** | Operators can launch new agent sessions or subagents directly from Telegram, specifying project path, branch/worktree, model, and initial prompt. |
| **REQ-FLEET-03** | **Session & Project Isolation** | **[Q-02]**, **[Q-12]** | Multiple concurrent sessions in the same or different projects must remain strictly isolated in Telegram. Inbound messages and replies must route exclusively to the bound session without cross-talk. |
| **REQ-FLEET-04** | **Agent Abort / Cancellation** | **[Q-07]**, **[Q-14]** | Capability to abort, interrupt, or cancel runaway or stuck agent sessions safely from Telegram (`/abort <id>`). |

### Category 3: Interactive Supervision, Decisions & Approvals

| Req ID | Requirement Description | Traced Quote(s) | Operational Specification |
| :--- | :--- | :--- | :--- |
| **REQ-UX-01** | **Understandable Questions, Zero Link Dumps** | **[Q-04]** | When an agent blocks for a decision, the Telegram notification must explain the problem in plain language, describe proposed actions and risks, and ask one concrete question. Link dumps and raw internal IDs are forbidden in notification bodies. |
| **REQ-UX-02** | **Clickable Choices with Unrestricted Free Text** | **[Q-04]**, **[Q-09]**, **[Q-12]** | Decisions must present labeled inline keyboard buttons (e.g. `[Option A]`, `[Option B]`), but the operator must **always** be able to reply with unrestricted free text to provide guidance. |
| **REQ-UX-03** | **Post-Specific Contextual Replies** | **[Q-09]**, **[Q-12]** | When the operator uses Telegram's native "Reply" feature to answer an earlier post, the system must extract the original post's task/decision ID and deliver the reply directly to that specific task context. |
| **REQ-UX-04** | **Immediate Real-Time Alerts for Hands-On Steps** | **[Q-11]** | Whenever an agent/tool requires physical operator interaction on the PC (e.g. logging into Chrome, closing an external window, entering an MFA code), send an immediate 1-line prompt to Telegram. |

### Category 4: Real-Time Visibility, Status & Anti-Spam Hygiene

| Req ID | Requirement Description | Traced Quote(s) | Operational Specification |
| :--- | :--- | :--- | :--- |
| **REQ-NOTIF-01** | **Strict Anti-Spam: Suppress Raw Tool Calls** | **[Q-06]** | Individual tool calls (file reads, edits, bash commands, searches) must **NEVER** generate separate Telegram messages. Only milestones, blockers, questions, and completions are sent. |
| **REQ-NOTIF-02** | **Dedicated Out-of-Band Crash Alerts** | **[Q-08]** | An independent watchdog or separate process must monitor host session PIDs and transmit an immediate alert to Telegram if the main agent process crashes or exits unexpectedly. |
| **REQ-NOTIF-03** | **On-Demand Status & Allowance Usage Snapshots** | **[Q-09]**, **[Q-10]** | Operators can request `/status` or `/usage` at any time to receive a concise report of completed/pending tasks, active blockers, and provider allowance reset windows. |

### Category 5: Media, Screenshots & Artifacts

| Req ID | Requirement Description | Traced Quote(s) | Operational Specification |
| :--- | :--- | :--- | :--- |
| **REQ-MEDIA-01** | **Outbound Visual Evidence (Screenshots & Diffs)** | **[Q-07]** | Agents must be able to send captured browser/GUI screenshots and diff summaries directly to Telegram as native photos or document attachments. |
| **REQ-MEDIA-02** | **Inbound Media Input from Telegram** | **[Q-07]** | Operators can send photos, mockups, or documents from Telegram into an active agent session to guide visual UI debugging or requirement specification. |

---

## 7. Next Steps & Handoff to Research Lanes

- **To `T3CodeResearch`:** Use Section 4 as the baseline requirement mapping to formalize the full command and view parity spec between T3 Code and Telegram Bot UI.
- **To `TgBotApiResearch`:** Evaluate Telegram Bot API primitives (Forum Topics vs Bot Chats, Inline Keyboards, Web Apps / Mini Apps, Callback Queries, Media Groups, and Rate Limiting) against Category 2 and Category 5.
- **To `TgHarnessSurface`:** Evaluate the Herdr Socket API (`REQ-ARCH-03`) and thin adapter contract (`REQ-ARCH-02`) against native Veyyon extension hooks, validating the harness-agnostic architecture.
