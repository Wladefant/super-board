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

### 4. One board per project, plus a life-level Master Board

Every project gets its own GitHub Project + its own `.claude/super-board/configs/<slug>.json`. Same system, same verbs, everywhere. Above them sits a single **Master Board** ([#6](https://github.com/users/Wladefant/projects/6)) for the cross-project life view. The Master Board holds **only** (a) personal/life task cards and (b) one abstract epic card per project, each linking to that project's own per-project board. Granular dev issues live **exclusively** on their per-project boards — never bulk-add dev issues to the Master Board (#6).

### 5. Design collaboration

Issues labeled `design` are **human-designer-owned**. The designer moves her own cards through the lanes and pastes the Figma link into the issue; agent lanes NEVER dispatch a `design`-labeled issue. An implementation issue becomes `Ready` only once its linked `design` issue is `Done` — design lands first, build follows. This delta is core for designer-fronted projects (e.g. HeyLolo) and dormant everywhere else, where no `design` cards exist.

## Linking rule

Every commit, doc, or post reference on a card or in a report is a full clickable `https://` link — never a bare sha. Link a commit as [`sha`](https://github.com/.../commit/<sha>), an issue as its `https://github.com/.../issues/N` URL, and so on. A bare sha or bare `#N` is an unclickable dead end on the board; the link is the receipt.

## Full history lives on the board

Past issues, fixes, and developments stay as `Done` cards. The board doubles as the changelog — no separate write-up required for “what shipped.”

## Milestones & Labels

Milestones = roadmap phases. One milestone per roadmap phase (e.g. "Phase 0 - Install + Smoke", "Phase 4 - Governance track (on demand)"), created at seeding time. EVERY issue gets a milestone at creation. Never invent due dates - set a due date only when the roadmap actually commits to one.

Every issue gets a milestone AND at least one type label at creation time (gh issue create --label a,b --milestone "<phase>").

The standard 13-label taxonomy proven on the ing board, grouped, each with its hex color and a one-line description. Use EXACTLY these (colors and descriptions must match; NO em-dashes, NO en-dashes, use a plain hyphen or colon only):

Type labels (universal across every project):
- build (1D76DB): Implementation work producing code or working artifacts
- docs (0E8A16): Documentation, guides, handouts
- research (5319E7): Sourced research with web and X evidence
- proof (FBCA04): Evidence task: prove a claim against the real system

Domain labels (per-project examples; adapt names to the project, keep the pattern):
- ui (C5DEF5): Product/tester interface surface
- ado (0052CC): External integration (e.g. Azure DevOps)
- test-data (D93F0B): Test data pools, claiming, fixtures
- security (B60205): Secret handling, redaction, disclosure
- governance (D4C5F9): Governance, compliance, BIA track

Process labels:
- laptop / environment-constraint (E99695): Requires a specific machine/environment; doubles as a dispatch filter
- meeting-prep (BFDADC): Preparation for a stakeholder meeting
- decision (F9D0C4): Blocked on or records a human decision
- risk (B60205): Documented open risk needing a policy call

Plus the two system labels design and history created by the base setup.

- Note that type labels are universal; domain labels are project-specific examples to rename/adapt.
- Discipline: every issue gets >=1 type label + relevant domain labels at creation; labels are updated when scope changes. Environment-constraint labels like `laptop` double as dispatch filters - an agent session must not pick up a card labeled with an environment it does not have.

**Single source of truth for the doctrine.** Any change to the milestones/labels doctrine or the setup flow lands in ONE place: `skills/superboard-setup/SKILL.md` in this repo. Do not fork-edit local copies. Installed payload copies in each project refresh by re-running `install.sh` (or `git pull` on a junctioned clone); the local `~/.claude/skills/superboard-setup` is a directory junction into a clone of this repo, so editing the canonical file and pushing is the only supported way to evolve the taxonomy.

## Board feature standard

The labels + milestones above are the floor, not the whole spec. A **fully-equipped Superboard** — what `superboard-setup` provisions and what every project board should converge to — has ALL of the following. This is the one canonical checklist; the setup mechanics live in `skills/superboard-setup/SKILL.md`.

1. **Status columns** — exactly seven: `Backlog` / `Ready` / `Building` / `QA` / `Review` / `Done` / `Blocked`.
2. **Milestones** — one per roadmap phase; every issue carries one at creation. Due dates only when the roadmap actually commits to one.
3. **Labels** — the 13-label taxonomy (type labels universal; domain labels per-project) plus the system labels `design` + `history`.
4. **Custom fields** — four, created in the Projects UI:
   - **Effort (tokens)** (Number) — per-card size for burn-up + prioritization.
   - **Target Date** (Date) — drives the Roadmap view.
   - **Priority** (Single select) — `P1` / `P2` / `P3`.
   - **Test Area** (Single select) — options are PER-PROJECT (prompt the operator; never hardcode).
5. **Saved "Roadmap by Phase" view** — Roadmap layout, Date = Target Date, sliced/marked by Milestone. The milestone-per-phase timeline stays on the board, not in a drifting doc.
6. **Insights burn-up by milestone** — Insights tab, Burn-up chart grouped by Milestone (optionally summing Effort (tokens)). At-a-glance phase progress; personal free/pro accounts support it.
7. **Structured Issue Form** — `.github/ISSUE_TEMPLATE/superboard-issue.yml`: enforced Context / Steps / Acceptance criteria + a Type dropdown + an `environment-constraint` checkbox. Blank issues stay enabled (`config.yml`) because agents create issues via `gh` CLI.
8. **Guarded auto-add Actions workflow** — `.github/workflows/auto-add-to-project.yml`: a redundant backup to GitHub's built-in project auto-add, DISABLED BY DEFAULT behind `ENABLE_ADD_TO_PROJECT`. Primary auto-add is the built-in project workflow (item 1's board settings); this Action is belt-and-suspenders and stores no token.
9. **`docs/README.md` master linked index** — every doc referenced as a full clickable blob URL (`https://github.com/<owner>/<repo>/blob/main/<path>`); kept current whenever a doc is added or moved.

Items 7–8 ship as repo payload at `payload/github/` in this repo and are copied into each target repo's `.github/` by `install.sh` and by `superboard-setup` Step 1 (which also `sed`s the board URL into the workflow placeholder). Items 4–6 are browser-only (`superboard-setup` Step 2). A board missing any of 1–8 is under-equipped; bring it up to standard rather than inventing a per-project variant.

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

