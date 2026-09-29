# Workflow Audit: Investigation of Instruction Sources Routing Routine Lanes to Opus & Prompt Purge

**Tracking Issue:** [Wladefant/super-board#255](https://github.com/Wladefant/super-board/issues/255)  
**Date:** 2026-09-29  
**Author:** Wladimir Kirjanovs <wladefant@gmail.com>  
**Status:** Merged & Enforced  

---

## 1. Executive Summary & Incident Context

On 2026-09-26, the operator identified an anomaly in agent routing: routine implementation, triage, and test lanes were being dispatched to expensive Claude Opus models (`claude-opus-5-5:high` and legacy `ag-opus`) instead of cheap worker tiers (Gemini 3.8 Flash, DeepSeek V4.1 / `ds-task`, OpenCode Go / `go-task`).

The operator instructed:
> *"It seems to be on an old Superboard version or workflow version or what exactly? That needs to be adjusted."* (Telegram, 2026-09-26T10:13Z)

This audit investigated the system prompts, loaded context files, active subagent definitions, and stale worktree configurations across `~/.veyyon/profiles/default/` and associated repositories. We specifically examined historical session `01a0a6a7` (`C:/Users/wkiri/.veyyon/profiles/default/agent/sessions/-development-veyyon/2026-09-15T20-00-14-140Z_01a0a6a7-dd3c-77fc-9aa4-647747d2031a.jsonl`) to isolate the root cause, quantify the misrouting, and eliminate obsolete prompt references.

---

## 2. Forensic Analysis of Session `01a0a6a7`

### A. Session Characteristics & Subagent Spawn Census
Analysis of `01a0a6a7-dd3c-77fc-9aa4-647747d2031a.jsonl` (3,340 messages, 248 subagent spawns) revealed an empirical distribution of subagent roles:

| Subagent Role | Spawn Count | Share (%) | Intended Tier | Actual Tier Observed |
|---|---|---|---|---|
| `task` | 138 | 55.6% | Cheap (Flash) | Flash (`gemini-3.8-flash:high`) |
| `opus` | 34 | 13.7% | High-risk fallback only | Expensive Opus 5.5 implementation |
| `reviewer` | 31 | 12.5% | Gating review | Opus 5.5 review |
| `ds-task` | 12 | 4.8% | Cheap worker / review | DeepSeek API |
| `codex-reviewer` | 11 | 4.4% | Reviews | OpenAI Codex / Astra |
| `codex-worker` | 10 | 4.0% | Implementation | OpenAI Codex / Sol |
| `ag-opus` | 8 | 3.2% | Retired Antigravity Opus | Opus 5.0 / 5.5 via Antigravity |
| `go-task` | 3 | 1.2% | Cheap worker | OpenCode Go |
| `go-deep` | 1 | 0.4% | Reasoning | OpenCode Go |
| **Total Spawns** | **248** | **100.0%** | — | **73 spawns (29.4%) on Opus tiers** |

Nearly 30% of all subagent spawns ran on Opus models (`opus`, `reviewer`, `ag-opus`), including routine tasks such as GUI scrollbar debugging (`Mausrad554`), unit test verification, and documentation updates.

### B. Identified Instruction Leakage Points
1. **Tool Schema Exposure (`task.json` / `Available Agents`)**:
   The `task` tool schema presented `astra-ux, codex-reviewer, codex-worker, opus, qa-verifier, reviewer, task, thinker` as universally available. When subagent prompt definitions existed on disk (`~/.veyyon/subagents/opus.md`), the orchestrator treated `opus` as a standard implementation lane rather than an emergency fallback.
2. **Ambiguous Delegation Guidance**:
   The prompt clause *"Move up a lane only when the agent must discover, build, and verify on its own"* was interpreted by the orchestrator as an invitation to route moderately complex tasks (multi-file investigation) to `opus`, even when acceptance criteria were fully bounded.
3. **Stale Worktree Instructions (`wt-refusal`, unmerged branches)**:
   Earlier branches in `super-board` and `polysimulator` contained historical references to Grok-first or Opus-first execution workflows dating back to 2026-07 and 2026-08 before the Antigravity Flash / DeepSeek routing tier was established.

---

## 3. Policy & System Remediation

To permanently prevent routine tasks from routing to Opus, four structural changes were enforced:

### A. Retirement of `ag-opus`
Per operator ruling (2026-09-27 ~15:00Z, verbatim: *"reember that there is no ago opus anymore and dont even try to use ti anymore it is not the firht time you now try to use it"*):
- `ag-opus` is completely removed from all routing ladders, role mappings, and subagent configs.
- The Antigravity sidecar quota for Anthropic models is permanently exhausted; attempting to dispatch `ag-opus` results in immediate failure or quota stalls.

### B. Strict Reservation of Opus 5.5 (`reviewer`)
Opus 5.5 (`anthropic/claude-opus-5-5:high` via Direct Anthropic API) is strictly reserved for super-hard work:
- Money paths, billing, atomic ledger integrity, and wallet balance operations.
- First-pass database migrations and destructive schema modifications.
- Cross-cutting architectural changes affecting core matching engines or daemon event loops.
- Routine reviews and delta reviews default to `ds-task` (DeepSeek) or Flash.

### C. Flash-First Implementation Standard
All routine implementation, bug reproduction, UI tweaking, test authoring, and documentation generation are assigned to:
- `task` (Gemini 3.8 Flash via Antigravity sidecar at 127.0.0.1:45123).
- `qa-verifier` (Gemini 3.8 Flash for browser-facing visual QA).
- `go-task` (OpenCode Go) when allowance permits.
- `ds-task` (DeepSeek API) for routine reviews and secondary triage.

### D. Enforced Subagent Nesting for Opus Lanes
When an Opus lane (`reviewer`) is required for architectural or high-risk oversight, it MUST spawn child `task`/`qa-verifier` subagents for mechanical operations (builds, test runs, screenshots, uploads, file formatting) to preserve scarce Anthropic tokens:
- Configured via `~/.veyyon/subagents/reviewer.md` (`spawns: task, qa-verifier`).
- Child tasks explicitly inherit `agent: "task"` (Gemini 3.8 Flash).

---

## 4. Verification & Audit Checklist

- [x] Session `01a0a6a7` inspected; 248 spawns analyzed; 73 Opus-tier spawns cataloged.
- [x] Stale references to `ag-opus` purged from active workflows and subagent templates.
- [x] `model_routing.py` and profile `AGENTS.md` §2 verified to reflect the four-tier routing ladder.
- [x] Nested delegation enabled and validated for `reviewer` lanes.
- [x] Current host allowance checked via `veyyon usage --json` (Anthropic 7-day usage at 2%, Google Daily at 81.5% and 0.4% across accounts).

This audit closes [Wladefant/super-board#255](https://github.com/Wladefant/super-board/issues/255).
