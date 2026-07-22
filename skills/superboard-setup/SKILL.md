---
name: superboard-setup
description: "Spin up Wlad's Superboard (GitHub-Projects agent pipeline from the Wladefant/super-board fork) on any project repo — board, columns, workflows, payload, config, labels. Use when the user says 'set up the board for <project>', 'superboard setup', or 'add this project to the board system'."
---

# Superboard Setup

Spin up Wlad's Superboard on a project: one board PER project. Route CLI work to an Opus claude subagent lane; route browser work to an Opus claude-in-chrome lane. The session model only does judgment + verification.

**Canonical home: this repo** (`skills/superboard-setup/SKILL.md`). The local `~/.claude/skills/superboard-setup` is a directory junction into a clone of this repo at `~/.claude/super-board-src` - edit here, `git commit` + `git push` to share, `git pull` to update. Never edit the local junction copy as a separate fork; there is one source of truth.

## Step 0 — Identify the repo(s)

- Find the LIVE repo the user actually works in.
- Watch for forks: compare `pushedAt` of the fork vs its parent and prefer the FRESHER fork.
- A product may span multiple repos (e.g. a main/frontend repo + a backend repo) → still ONE board, with BOTH repos linked to it.

## Step 1 — CLI part (route to an Opus claude lane)

Collapse this into a SINGLE bash script the lane just executes (keeps the lane cheap and deterministic):

- Create the board: `gh project create --owner Wladefant --title "<Project>"`
- Link EACH repo via GraphQL mutation `linkProjectV2ToRepository` (note: `gh project link` may not exist in gh 2.39.1, so use the GraphQL mutation).
- Install the payload from https://github.com/Wladefant/super-board using its `install.sh` into the repo's `.claude/`.
- Write `.claude/super-board/configs/<slug>.json` containing: owner, project number, base_branch, max_workers 2, rebuild_cap 2, human_approves_merge true, worker_backend "claude-p", exclude_labels ["history","design"].
- Create labels `design` and `history` in EVERY linked repo. IMPORTANT: NO em-dashes in label descriptions (gh 2.39.1 silently fails on them).
```
gh label create design --force --color BFD4F2 --description "Human designer owned. Agents never dispatch or edit."
gh label create history --force --color EEEEEE --description "Historical record card. Not dispatchable work."
gh label create build --force --color 1D76DB --description "Implementation work producing code or working artifacts"
gh label create docs --force --color 0E8A16 --description "Documentation, guides, handouts"
gh label create research --force --color 5319E7 --description "Sourced research with web and X evidence"
gh label create proof --force --color FBCA04 --description "Evidence task: prove a claim against the real system"
gh label create ui --force --color C5DEF5 --description "Product or tester interface surface"
gh label create ado --force --color 0052CC --description "External integration such as Azure DevOps"
gh label create test-data --force --color D93F0B --description "Test data pools, claiming, fixtures"
gh label create security --force --color B60205 --description "Secret handling, redaction, disclosure"
gh label create governance --force --color D4C5F9 --description "Governance, compliance, BIA track"
gh label create laptop --force --color E99695 --description "Requires a specific machine or environment; doubles as a dispatch filter"
gh label create meeting-prep --force --color BFDADC --description "Preparation for a stakeholder meeting"
gh label create decision --force --color F9D0C4 --description "Blocked on or records a human decision"
gh label create risk --force --color B60205 --description "Documented open risk needing a policy call"
```
Note in prose that domain labels (ui, ado, test-data ...) are project-specific examples to rename per project, while type labels (build, docs, research, proof) are universal.
- Commit ONLY the `.claude/` additions, then push.

## Step 2 — Browser part (route to an Opus claude-in-chrome lane; the API cannot do this)

- Set the board Status options to EXACTLY these seven, with the standard descriptions:
  - Backlog — not started
  - Ready — approved and ready to be picked up by a worker
  - Building — a worker is actively implementing
  - QA — implementation done, under test
  - Review — awaiting human/code review
  - Done — merged and complete
  - Blocked — cannot proceed until something is unblocked
- Enable the "Auto-add to project" workflow (repo, filter `is:issue is:open`).
- Enable the "Item added to project" workflow → set Status to Backlog.
- GOTCHA: GitHub's visibility timer means Chrome must be FOREGROUNDED or the Authorize/Save buttons stay disabled.

## Step 3 — Seed

- Create backlog issues. Each issue body has `## Context`, `## Steps`, `## Acceptance criteria` — with binary Given/When/Then acceptance criteria.
- Optionally seed history as closed + Done cards: closed issues bypass auto-add, so add them manually via `gh project item-add`, then `gh project item-edit` to set Status=Done. Discover the field id and option ids via `gh project field-list`.

## Rules

- One board PER project.
- **WHY links matter (user, 2026-07-21): the board is the anti-loop memory.** Old issues get referenced when a similar problem returns — the links to dossiers, commits, and failed attempts are what stop the team from re-trying something already tried. Before solving any recurring symptom, SEARCH the board for prior cards on it and read their linked evidence first.
- Every commit/doc reference is a full clickable https:// link — NEVER a bare sha, NEVER a bare file path. This applies in chat with the user too: reference issues as full URLs, not "#N".
- **A doc link must RESOLVE before it goes on a card (HARD RULE, user-set 2026-07-21).** Referencing a doc by repo-relative path ("see docs/_session/<topic>/X.md") is a violation — the reader can't click it. Before referencing any doc on an issue/card/comment: (1) commit it, (2) push it to the branch that carries docs (e.g. the repo's docs/* branch on origin), (3) paste the full https://github.com/<owner>/<repo>/blob/<branch>/<path> URL. If a doc genuinely can't be pushed yet, paste its content into the issue body instead of naming the path. When a doc referenced earlier turns out to be link-less, fix the card the moment it's noticed — don't wait for the user to catch it.
- **Link EVERYTHING linkable (HARD RULE, user-set 2026-07-22).** If a thing has a canonical URL, every mention of it in a deliverable doc, card, comment, or report must be a clickable link: X handles → `[@handle](https://x.com/handle)`, GitHub users/repos/issues/commits → their https URLs, contracts/addresses/txs → block-explorer URLs, videos/channels → their URLs. A bare @handle, bare sha, bare address, or bare path is a defect ("if it's possible to be linked, the link should be there"). Exception: sections explicitly meant for copy-paste (e.g. a plain handle list for building an X List) stay plain. Run a link-lint pass over every deliverable doc before it ships; lanes producing docs must be told this rule in their prompt.
- `design`-labeled issues are human-owned and are NEVER dispatched to a worker.
- Ready is a live wire until the label filter ships — see https://github.com/Wladefant/soundcore-work-workflow/issues/26
- Token safety: Opus claude lanes do implementation (grok is reserved for X research and explicitly-requested jobs only); the session model is only for judgment + verification.
- Verify each phase with real `gh project view` / `gh project item-list` output — NEVER trust reports.

## Milestones & Labels

Milestones = roadmap phases. One milestone per roadmap phase (e.g. "Phase 0 - Install + Smoke", "Phase 4 - Governance track (on demand)"), created at seeding time. EVERY issue gets a milestone at creation. Never invent due dates - set a due date only when the roadmap actually commits to one.

Every issue gets a milestone AND at least one type label at creation time (gh issue create --label a,b --milestone "<phase>").

The standard 13-label taxonomy is created at seeding time (see Step 1 for the full `gh label create` commands). Type labels are universal across every project; domain labels are per-project examples to rename/adapt.

Discipline: every issue gets >=1 type label + domain labels at creation; labels are updated when scope changes (e.g. add `laptop` the moment a task turns out to need the work laptop). Prefer assigning the governance/on-demand phase to cross-phase history/risk cards rather than leaving them milestone-less. Environment-constraint labels like `laptop` double as dispatch filters: an agent session must not pick up a card labeled with an environment it does not have. Milestone views answer "how far is phase X" - keep them honest by closing issues only when their milestone-relevant work is truly done.
