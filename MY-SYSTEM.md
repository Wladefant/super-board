# Wlad's Superboard

Per-project agent-driven development on top of this [Wladefant/super-board](https://github.com/Wladefant/super-board) fork of [EricTechPro/super-board](https://github.com/EricTechPro/super-board).

## Five design deltas vs upstream

### 1. Token-safe lanes

Builder and QA lanes route through the `grok` CLI (xAI quota) and the `codex` CLI (cross-vendor) rather than always spending Anthropic quota. Claude is reserved for judgment and review — the Reviewer role and adversarial truth-gate checks.

This follows the **fable-advisor orchestration doctrine**: the orchestrating session delegates mechanical / expensive work to cheaper or differently-metered agents and keeps the expensive model for synthesis and judgment calls.

### 2. Idea → auto-decomposed issues

Write **one** short core idea. The system expands it into multiple lint-passing, detailed GitHub issues with explicit acceptance criteria (via `super-board lint` readiness checks) before any card reaches `Ready`.

### 3. Roadmap always on the board

Epics and milestones stay visible on the **same** GitHub Project the run loop drains. The roadmap is board state, not a separate document that drifts.

### 4. One board per project, no master board

Every project gets its own GitHub Project + its own `.claude/super-board/configs/<slug>.json`. Same system, same verbs, everywhere — deliberately no cross-project umbrella board.

### 5. Design collaboration

Issues labeled `design` are **human-designer-owned**. The designer moves her own cards through the lanes and pastes the Figma link into the issue; agent lanes NEVER dispatch a `design`-labeled issue. An implementation issue becomes `Ready` only once its linked `design` issue is `Done` — design lands first, build follows. This delta is core for designer-fronted projects (e.g. HeyLolo) and dormant everywhere else, where no `design` cards exist.

## Linking rule

Every commit, doc, or post reference on a card or in a report is a full clickable `https://` link — never a bare sha. Link a commit as [`sha`](https://github.com/.../commit/<sha>), an issue as its `https://github.com/.../issues/N` URL, and so on. A bare sha or bare `#N` is an unclickable dead end on the board; the link is the receipt.

## Full history lives on the board

Past issues, fixes, and developments stay as `Done` cards. The board doubles as the changelog — no separate write-up required for “what shipped.”

## Production hardening (ported from ops)

| Issue | Fix | What it does |
|---|---|---|
| [#8](https://github.com/EricTechPro/super-board/issues/8) | No-progress / Done-count halt gate | Halts after N expensive ticks with zero increase in the `Done` column (catches zero-merge token runaways). Independent of the existing “no dispatch while idle” gate. |
| [#9](https://github.com/EricTechPro/super-board/issues/9) | OPEN-only dispatch | `top_card_in_column` skips CLOSED issues whose board Status drifted; tries the next candidate. |
| [#10](https://github.com/EricTechPro/super-board/issues/10) | Draft-PR ready + merge path | Reviewer marks draft PRs ready before merge; never moves a card to Done while its PR is unmerged. |
| [#13](https://github.com/EricTechPro/super-board/issues/13) | Windows-safe locks + run ceilings | `stale_lock_seconds` on Windows/MSYS when `kill -0` can't verify PIDs; hard `max_dispatches` / `max_hours` ceilings with drain. |

Config keys (all optional, defaults in parentheses): `noprogress_halt_ticks` (10), `max_dispatches` (20), `max_hours` (3), `stale_lock_seconds` (900). See `skills/super-board/references/config-schema.json`.

**Known hazard:** label-filtering is not yet implemented in the dispatchers — a `history` or `design` card dragged into `Ready` would be dispatched as buildable work. Tracked in [soundcore-work-workflow#26](https://github.com/Wladefant/soundcore-work-workflow/issues/26).

## Spin up a new project

1. **Install** the `.claude/` payload (skills + scripts) into the project (release zip or copy from this fork).
2. **Create a GitHub Project** with Status columns: `Backlog` / `Ready` / `Building` / `QA` / `Review` / `Done` / `Blocked`.
3. **Write** `.claude/super-board/configs/<slug>.json` pointing at that project (and set `worker_backend` if you need the legacy `claude-p` dispatcher).
4. **Run verbs** (in order):
   - `onboard` — wizard / config + preconditions
   - `lint` — readiness checks on acceptance criteria
   - `run` — drain the board
   - `status` — read-only snapshot anytime
   - `stop` — graceful shutdown of in-flight workers

Docs convention for session notes vs canon: [DOCS-SYSTEM.md](./DOCS-SYSTEM.md).
