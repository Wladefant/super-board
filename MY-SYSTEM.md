# Wlad's Superboard

Per-project agent-driven development on top of this [Wladefant/super-board](https://github.com/Wladefant/super-board) fork of [EricTechPro/super-board](https://github.com/EricTechPro/super-board).

## Five design deltas vs upstream

### 1. Token-safe lanes

Builder and QA lanes route through the `grok` CLI (xAI quota) and the `codex` CLI (cross-vendor) rather than always spending Anthropic quota. Claude is reserved for judgment and review — the Reviewer role and adversarial truth-gate checks.

This follows the **fable-advisor orchestration doctrine**: the orchestrating session delegates mechanical / expensive work to cheaper or differently-metered agents and keeps the expensive model for synthesis and judgment calls.

The Reviewer's *judgment* is Claude's; the Reviewer's *binding gate* is a parallel maximum-level local Codex fleet whose every finding must be resolved. Two model families, working the same diff — that is the point of the split, not a cost accident.

### 2. Idea → auto-decomposed issues

Write **one** short core idea. The system expands it into multiple lint-passing, detailed GitHub issues with explicit acceptance criteria (via `super-board lint` readiness checks) before any card reaches `Ready`.

### 3. Roadmap always on the board

Epics and milestones stay visible on the **same** GitHub Project the run loop drains. The roadmap is board state, not a separate document that drifts.

### 4. One board per project, plus a life-level Master Board

Every project gets its own GitHub Project + its own `.claude/super-board/configs/<slug>.json`. Same system, same verbs, everywhere. **A product's repos share ONE board** — a frontend repo and a backend repo are one product, so both are linked to the same Project rather than getting a board each. Above them sits a single **Master Board** ([#6](https://github.com/users/Wladefant/projects/6)) for the cross-project life view. The Master Board holds **only** (a) personal/life task cards and (b) one abstract per-project program epic, each linking to that project's own per-project board. Granular product cards live **exclusively** on their per-project boards — never bulk-add dev issues to the Master Board (#6).

**Board moves are direct orchestration.** Adding a card, changing its Project status, or verifying one is done by the top-level session itself — GitHub MCP where it exposes Projects v2 mutations, otherwise targeted `gh api graphql` (`addProjectV2ItemById`, `updateProjectV2ItemFieldValue`), reading the returned item ID back. **Never spawn a subagent for a card move, a card add, or a status check.** A subagent's report is a claim; the returned item ID is the mutation.

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
- environment-constraint (E99695): Requires a specific machine/environment; doubles as a dispatch filter. **Canonical name.** `laptop` is a PRESERVED LEGACY ALIAS that maps onto it (`canonical_environment_label` in `scripts/super_board_runtime/normalize.py`), so boards created before v2.0.0 keep working without relabelling a backlog. New boards create `environment-constraint` and nothing else.
- meeting-prep (BFDADC): Preparation for a stakeholder meeting
- decision (F9D0C4): Blocked on or records a human decision
- risk (B60205): Documented open risk needing a policy call

Plus the two system labels design and history created by the base setup.

- Note that type labels are universal; domain labels are project-specific examples to rename/adapt.
- Discipline: every issue gets >=1 type label + relevant domain labels at creation; labels are updated when scope changes. `environment-constraint` doubles as a dispatch filter - an agent session must not pick up a card labeled with an environment it does not have.

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
10. **Comment sweep** — `scripts/super-board-sweep-comments.mjs`, the board's **inbound channel** (see below). A board you can only write to is half a system.

Items 7–8 ship as repo payload at `payload/github/` in this repo and are copied into each target repo's `.github/` by `install.sh` and by `superboard-setup` Step 1 (which also `sed`s the board URL into the workflow placeholder). Items 4–6 are browser-only (`superboard-setup` Step 2). A board missing any of 1–8 is under-equipped; bring it up to standard rather than inventing a per-project variant.

## Board audit — run this before trusting a board

A board drifts silently. Provisioning the nine items once does not keep them true, and the
failure mode is invisible: the board still *looks* fine. Real drift found on the ing board
after three weeks of heavy use — **42 closed issues still sitting in `Review`/`Building`**,
three of four custom fields provisioned but **never populated on a single card**, the project
**missing from the Master Board entirely**, and stale single-select options naming a component
that had since been retired.

Audit with these, and fix what they surface:

```bash
# 1. Closed issues whose card never moved to Done  (expect: none)
gh api graphql -f query='{user(login:"OWNER"){projectV2(number:N){items(first:100){nodes{
  fieldValues(first:20){nodes{... on ProjectV2ItemFieldSingleSelectValue{name field{
  ... on ProjectV2SingleSelectField{name}}}}} content{... on Issue{number state}}}}}}}' \
  --jq '.data.user.projectV2.items.nodes[]|select(.content.state=="CLOSED")
        |{n:.content.number,st:((.fieldValues.nodes[]|select(.field.name=="Status")|.name)//"none")}
        |select(.st!="Done")|"#\(.n) \(.st)"'

# 2. Custom-field coverage — a field used on 0 cards is a field that does not exist
gh api graphql -f query='{user(login:"OWNER"){projectV2(number:N){items(first:100){nodes{
  fieldValues(first:25){nodes{
  ... on ProjectV2ItemFieldSingleSelectValue{field{... on ProjectV2SingleSelectField{name}}}
  ... on ProjectV2ItemFieldNumberValue{field{... on ProjectV2Field{name}}}
  ... on ProjectV2ItemFieldDateValue{field{... on ProjectV2Field{name}}}}}}}}}}' \
  --jq '[.data.user.projectV2.items.nodes[].fieldValues.nodes[]|.field.name|select(.)]
        |group_by(.)|map({f:.[0],n:length})|.[]|"\(.f)\t\(.n)"'

# 3. Open issues with no milestone  (expect: none)
gh issue list --repo OWNER/REPO --state open --json number,milestone \
  --jq '.[]|select(.milestone==null)|"#\(.number) NO MILESTONE"'

# 4. Is this project represented on the Master Board (#6) by exactly one epic card?
gh api graphql -f query='{user(login:"OWNER"){projectV2(number:6){items(first:50){nodes{
  content{... on DraftIssue{title} ... on Issue{title}}}}}}}' --jq '..|.title? //empty'

# 5. Labels actually in use vs the taxonomy — catches drift toward GitHub's defaults
gh issue list --repo OWNER/REPO --state all --limit 200 --json labels \
  --jq '[.[].labels[].name]|group_by(.)|map({l:.[0],n:length})|sort_by(-.n)|.[]|"\(.n)\t\(.l)"'
```

Rules the audit enforces:

- **Closed ⇒ `Done`.** A closed issue parked in `Review` makes the board lie about what is left.
- **A field nobody fills is worse than no field** — it fakes rigour and breaks Insights (an
  Effort-weighted burn-up over empty Effort values charts nothing).
- **Every open issue carries a milestone**, or it is invisible to the roadmap.
- **Single-select options are living config.** When a component is retired, its option goes too.
- **Every project appears on the Master Board as exactly one epic card**, and granular dev
  issues never do.

## The inbound channel: comments are how the human talks to the board

The board was write-only for agents and read-only for the human. **Comments close the loop
in the other direction.** Leave a comment on any card at any time — a correction, a decision,
a "no, do it this way" — and the next session picks up every one of them in a single pass:

```bash
node scripts/super-board-sweep-comments.mjs            # new since the last sweep
node scripts/super-board-sweep-comments.mjs --mine     # only what OTHER people wrote
node scripts/super-board-sweep-comments.mjs --all      # the whole history
node scripts/super-board-sweep-comments.mjs --repo owner/name   # any board
```

Why it is part of the standard, not a convenience script:

- **Nothing is missed.** It sweeps three surfaces: issue comments, inline
  pull-request review comments, *and* PR review objects (`GET
  /pulls/{n}/reviews`). The third is where Codex
  (`chatgpt-codex-connector[bot]`) and Copilot
  (`copilot-pull-request-reviewer[bot]`) post their verdict; fetching only
  issue comments is how
  [PR #3099](https://github.com/Bavariance/polysimulator/pull/3099) merged
  with unaddressed findings. A watermark per project means a repeat run
  shows only what is new; `--peek` inspects without consuming. The harvest
  contract is
  [`references/github-ops.md`](./skills/super-board/references/github-ops.md).
- **It surfaces other people.** `--mine` filters to what collaborators said. On the ing board
  its first run immediately surfaced a teammate's open PR and two upstream defects that no
  session had noticed.
- **It removes the need to re-state instructions.** The human comments on the card, in context,
  next to the acceptance criteria — instead of repeating context in chat.

**Session-start discipline:** checking the board now means *board state + comment sweep*, not
board state alone.

**On reactivity — be honest about the limits.** Nothing can push into a Claude session from
outside; GitHub cannot wake an agent. Three tiers, in increasing durability:

1. **On demand** — the sweep above. Always available, zero setup.
2. **Live, within a session** — a `Monitor` polling for new comments streams them into the
   running session as they appear. Dies with the session.
3. **Always-on** — a GitHub Actions workflow on `issue_comment` can react mechanically
   (label, move the card to `Blocked`, acknowledge) with no agent involved, so a comment left
   while nothing is running still changes board state. The agent reconciles on the next sweep.

Tier 3 plus a marker convention (`@claude` in the comment, or an `agent` label) is the closest
thing to true reactivity that does not require an always-running session.

## Production hardening (ported from ops)

| Issue | Fix | What it does |
|---|---|---|
| [#8](https://github.com/EricTechPro/super-board/issues/8) | No-progress / Done-count halt gate | Halts after N expensive ticks with zero increase in the `Done` column (catches zero-merge token runaways). Independent of the existing “no dispatch while idle” gate. |
| [#9](https://github.com/EricTechPro/super-board/issues/9) | OPEN-only dispatch | `top_card_in_column` skips CLOSED issues whose board Status drifted; tries the next candidate. |
| [#10](https://github.com/EricTechPro/super-board/issues/10) | Draft-PR ready + merge path | Reviewer marks draft PRs ready before merge; never moves a card to Done while its PR is unmerged. |
| [#13](https://github.com/EricTechPro/super-board/issues/13) | Windows-safe locks + run ceilings | `stale_lock_seconds` on Windows/MSYS when `kill -0` can't verify PIDs; hard `max_dispatches` / `max_hours` ceilings with drain. |

Config keys (all optional, defaults in parentheses): `noprogress_halt_ticks` (10), `max_dispatches` (20), `max_hours` (3), `stale_lock_seconds` (900). See [`config-schema.json`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/config-schema.json).

**Hazard closed in v2.0.0:** label-filtering used to be missing from the dispatchers — a `history` or `design` card dragged into `Ready` was dispatched as buildable work ([soundcore-work-workflow#26](https://github.com/Wladefant/soundcore-work-workflow/issues/26)). `exclude_labels` is now part of the config schema and is enforced in EVERY dispatcher path — the read-only planner, the headless dispatcher and the dynamic workflow share one implementation — and `design` + `history` are permanently non-dispatchable whether or not they are listed.

## v2.0.0 is backward-incompatible — what breaks, and what an existing board must do

This release changes documented contracts in ways an existing board will notice. Full detail
in [RELEASE-NOTES.md](./RELEASE-NOTES.md); the provisioning mechanics and the reasoning behind
each rule are in
[`skills/superboard-setup/SKILL.md`](https://github.com/Wladefant/super-board/blob/main/skills/superboard-setup/SKILL.md).

**What breaks:**

1. **Dispatch now requires exactly `Ready`.** No other status is dispatchable — there is no
   "resume from Building", no picking a card up out of Blocked, and no lane-specific
   eligibility. Only OPEN issue cards dispatch; pull-request cards never do. Boards that
   parked work in Building expecting it to be picked back up will find it stranded.
2. **Merge behaviour is human-only and rebase-only.** The runtime never merges, never enables
   an automatic merge setting, never closes an implementation issue instead of merging it, and
   never writes the completion column before a real human merge. `human_approves_merge` must
   be `true` and `merge_method` must be `rebase`; anything else exits 65. Repositories must be
   set to `allow_rebase_merge: true`, `allow_squash_merge: false`, `allow_merge_commit: false`.
   A tree-wide scanner enforces this and fails the release gate on any active merge mechanism.
3. **The status contract is the canonical seven** — `Backlog · Ready · Building · QA · Review ·
   Blocked · Done` — and is not configurable. The retired eighth option is refused wherever a
   lifecycle value is expected, rather than being quietly mapped onto something else.
4. **The config schema changed.** `activation_mode` is required in practice (a board with no
   activation decision is `off`), `human_approves_merge`/`merge_method` are constrained,
   `minimum_graphql_reserve` has an immutable floor, `exclude_labels` is now enforced instead
   of ignored, and `github_auth` names environment variables rather than carrying values. A
   config containing anything credential-shaped exits 65.

**Migration checklist for an existing board, in order:**

1. Reinstall the payload at the pinned release and confirm the install manifest wrote a
   SHA-256 for every file.
2. Set `activation_mode` to `"off"` and `proof_issue_url` to `null`, then validate:
   `python scripts/super-board-config.py validate --config <path> --json`.
3. Set `human_approves_merge: true` and `merge_method: "rebase"`, and pin the three repository
   merge settings above on every linked repo.
4. Remove any Status option that is not one of the canonical seven — the retired eighth in
   particular — and reconcile the cards holding it onto a real status.
5. Fix the built-in workflows — in particular **Item reopened → Backlog**, which earlier
   revisions of the setup doctrine wrongly set to Building — and read all seven targets back.
6. Add `environment-constraint` to the label set. Leave `laptop` in place; it is an alias now,
   not a duplicate.
7. Add a `Branch route:` declaration to every issue you intend to dispatch. Without one the
   card is ineligible and no branch is created.
8. Run the reconcile sweep and the board audit below, and fix what they surface.
9. Only then open the configuration pull request that moves `activation_mode` from `off` to
   `proof-only`, naming the one issue you will watch.

## The contract every board inherits — the short version

Four config keys carry most of the safety, and none of them used to be documented here:

- **`activation_mode`** — `off` → `proof-only` → `active`. A new board is `off`. Each
  transition needs its own human-reviewed configuration pull request. `proof_issue_url` is
  `null` in `off`, an exact issue URL in `proof-only`, and `null` again in `active`. Nothing at
  runtime — no flag, no command, no environment variable — dispatches past the mode, and in
  `proof-only` only the exact allowlisted issue may be selected.
- **The `Branch route:` declaration** — the Issue Form's `branch-route` field, carried as
  `branch_route` on the normalized intake form. Every dispatchable issue declares exactly one
  explicit route in its body, and **the declaration IS the base branch**. Missing, omitted,
  `default`, unknown, duplicated or conflicting declarations are INELIGIBLE and fail
  **before** a branch is created. A Test Area never implies a route. A design branch is never
  a dispatch route. The config's `branch_routes` is a **validation table, not a routing
  table**: it names the label the Frankfurt route must also carry, turns two labels naming
  different branches into a conflict, and refuses a label pointed at a branch that can never
  dispatch. It never selects the branch — a table that did would be a second routing
  authority, and the more permissive of two authorities is the one that acts.
- **`minimum_graphql_reserve`** — an immutable 1,000-point floor. Configuration may RAISE it,
  never lower it. One cached inventory per runtime cycle; estimate the cost before executing;
  bounded batches; stop at the reserve (exit 75). **Never retry-spin, never sleep through a
  reset** — and prefer the built-in Project workflows over scripted API item-adds. This
  supersedes the old 200-point threshold and the old sleep-until-reset advice wherever a
  runbook still repeats them; both predate the #381 worker storm, where lanes sharing one
  token bucket drained it because nothing told them to watch it.
- **`exclude_labels`** — enforced in EVERY dispatcher path, with `design` and `history`
  permanently non-dispatchable.

Four more rules that are not config and are not optional:

- **Identity.** Unattended Projects v2 mutation accepts only a machine-account CLASSIC PAT with
  scopes `repo`, `project`, `read:org`, supplied via `SUPERBOARD_GITHUB_TOKEN` with the
  expected login in `SUPERBOARD_GITHUB_LOGIN`. **GitHub Apps cannot access personal Projects v2
  at all**; fine-grained and scope-ambiguous tokens are refused. Missing variable, wrong login,
  missing scope, inaccessible repo or Project, unconfirmable permission — all fail closed.
  Environment variables are referenced by NAME, never by value, anywhere.
- **QA is bound to an exact SHA.** Record the pull request head before testing, test that SHA in
  an isolated locked worktree, reread the head afterwards, publish a SHA-bound check, and release
  locks and worktrees on every terminal path. Later commits inherit no passing QA; every head
  change invalidates it; the head must equal the tested SHA before Review and again immediately
  before a human merges.
- **Review is one Codex fleet.** Exactly one parallel maximum-level local Codex fleet per code
  pull request — a structured diff review plus correctness, security, and performance /
  design-consistency lenses — and every finding including nits is resolved. No second fleet
  unless the operator asks. CodeRabbit, Copilot, Greptile and the GitHub `@codex` connector are
  non-binding and are not gates. Documentation-only diffs are exempt.
- **Closed is not the same as done.** A closed issue reaches the completion column only with
  accepted completion evidence, a linked duplicate, or a stated not-planned disposition;
  otherwise it is reopened, moved to Blocked, and given a corrective comment. A closed-unmerged
  pull request needs linked abandonment or supersession evidence, else Blocked. Pre-activation
  historical evidence is never rewritten to manufacture acceptance.

And two habits that keep the board honest under concurrency: **compare before mutate** — reread
the item by immutable node ID, reread repository state, reread Project values, compare against
the manifest preconditions, and quarantine anything that changed rather than overwriting a newer
human or automation decision — and **capture created-OR-CHANGED deltas before every mutation
batch**, not only at the start of a run and not only for newly created items.

Everything published to GitHub passes one sanitizer immediately before the write boundary:
render the complete payload, redact, scan the complete redacted payload, fail closed with no
partial write, then write. It covers every GitHub-bound surface — issue and pull-request bodies,
comments, review summaries, QA comments, checks, commit statuses, closure comments, release text,
and Project text fields and manifests.

## Spin up a new project

1. **Install** the `.claude/` payload (skills + scripts) into the project (release zip or copy from this fork).
2. **Create a GitHub Project** with Status columns: `Backlog` / `Ready` / `Building` / `QA` / `Review` / `Done` / `Blocked`.
3. **Write** `.claude/super-board/configs/<slug>.json` pointing at that project, with `activation_mode` at `"off"`. `worker_backend` defaults to `"claude-p"` (headless workers); the in-session dynamic-workflow backend is explicit opt-in.
4. **Run verbs** (in order):
   - `onboard` — wizard / config + preconditions
   - `lint` — readiness checks on acceptance criteria
   - `run` — drain the board
   - `status` — read-only snapshot anytime
   - `stop` — graceful shutdown of in-flight workers

Docs convention for session notes vs canon: [DOCS-SYSTEM.md](./DOCS-SYSTEM.md).

