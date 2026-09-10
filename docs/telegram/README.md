# Telegram Agent-Management Harness Documentation

This directory contains research, architectural specifications, operator requirements, and milestone plans for the Telegram Agent-Management Harness ("T3 Code inside Telegram").

The goal of this system is to provide a universal, mobile-first, and desktop-friendly Telegram operations surface for managing multi-agent coding sessions across diverse harnesses (Veyyon, Herdr, Claude Code, Pi/omp, Codex).

---

## Documents Index

| Document | Description | Scope & Focus |
| :--- | :--- | :--- |
| **[`TgBotApiResearch.md`](./TgBotApiResearch.md)** | **Telegram Bot API Deep Capability Research & Universal Agent Harness Architecture** | Comprehensive analysis of Bot API capabilities (v7.x, v8.x, v9.x, and v10.x), Forum Supergroups (`createForumTopic`), Bot DM Threaded Mode (`has_topics_enabled`), inline keyboard callbacks, debounced in-place editing, Mini Apps (`initData`), and the 6-method thin `AgentBackendAdapter` specification (`list`, `prompt`, `decide`, `abort`, `read`, `artifacts`). |
| **[`T3CodeResearch.md`](./T3CodeResearch.md)** | **T3 Code & Modern AI Agent Management UIs: Comparative Survey & Contract** | Comparative study of T3 Code (Theo Browne / Ping.gg), Herdr, Claude Code Remote Control, Cursor Glass, and Conductor.build. Maps multi-worktree panels, status boards, terminal replays, and diff viewers into Telegram topics, native media, and webviews. |
| **[`TgHarnessPlan.md`](./TgHarnessPlan.md)** | **Telegram Agent-Management Harness — Implementation Plan** | Multi-phase delivery roadmap: **M1** (See, prompt, and answer existing agents), **M2** (Session rooms and quiet live oversight), **M3** (Start isolated work and exchange evidence), and **M4** (Telegram Mini App interactive UI). |
| **[`TgHarnessPrompts.md`](./TgHarnessPrompts.md)** | **Operator Request Inventory & Traced Requirements** | Full chronological inventory of operator requirements and requests from session transcripts (August 19, 2026 to September 8, 2026) with requirement IDs and implementation traceability matrix. |
| **[`TgHarnessSurface.md`](./TgHarnessSurface.md)** | **Veyyon-Side Surface Inventory & Harness-Agnostic Architecture** | Complete audit of Veyyon runtime extension surfaces, SQLite databases (`bot_pool.db`, `veyyon_bridge_state.db`), sanitizers, process lifecycles, and adapter boundaries. |
| **[`HerdrIntegration-research.md`](./HerdrIntegration-research.md)** | **Herdr 0.9.0 / Veyyon Integration Research** | Integration analysis with Herdr daemon, unix domain socket IPC, streaming agent state, and multi-session routing. |

---

## Architecture Summary

```
                  ┌─────────────────────────────────────┐
                  │          Telegram Bot API           │
                  │ (Forum Topics / DMs / Keyboards)    │
                  └──────────────────┬──────────────────┘
                                     │ Long Polling / Webhook
                                     ▼
                  ┌─────────────────────────────────────┐
                  │    TypeScript Bridge / Poller       │
                  │  (packages/telegram-agent-harness)  │
                  └──────────────────┬──────────────────┘
                                     │
           ┌─────────────────────────┴─────────────────────────┐
           ▼                                                   ▼
┌──────────────────────┐                           ┌──────────────────────┐
│  Veyyon Extension    │                           │    Herdr Adapter     │
│ (In-process session) │                           │  (Daemon IPC Socket) │
└──────────────────────┘                           └──────────────────────┘
```

### Key Operational Invariants
1. **Never print or log bot tokens**: Tokens are stored strictly in operator-managed local channel storage (`~/.claude/channels/telegram-<slot>/.token`).
2. **Proper Telegram HTML Formatting**: All messages use HTML parse mode with valid tags (`<b>`, `<code>`, `<a href="...">`, `<pre>`), avoiding raw Markdown artifacts.
3. **In-place updates**: Live status cards update via `editMessageText` and `editMessageReplyMarkup` to avoid notification noise.
4. **Idempotent Callbacks**: Inline button callback data tokens are consumed atomically to prevent race conditions or double-execution.
