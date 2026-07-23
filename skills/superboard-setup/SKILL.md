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
- Copy the `.github` board payload into the target repo (the payload lives at `payload/github/` in the super-board clone; `install.sh` also copies it, but do it explicitly here so it lands and commits in the same script):
```
mkdir -p .github/ISSUE_TEMPLATE .github/workflows
cp "$SB_SRC/payload/github/ISSUE_TEMPLATE/superboard-issue.yml" .github/ISSUE_TEMPLATE/
cp "$SB_SRC/payload/github/ISSUE_TEMPLATE/config.yml"           .github/ISSUE_TEMPLATE/
cp "$SB_SRC/payload/github/workflows/auto-add-to-project.yml"   .github/workflows/
# sed THIS board's URL into the guarded workflow's placeholder:
sed -i "s#__PROJECT_URL__#https://github.com/users/Wladefant/projects/<N>#" .github/workflows/auto-add-to-project.yml
```
  (`$SB_SRC` = the super-board clone, `~/.claude/super-board-src`.) This installs the structured Issue Form (enforced Context/Steps/Acceptance + Type dropdown + `environment-constraint` checkbox) and the guarded auto-add workflow. The workflow stays OFF until the operator sets `ENABLE_ADD_TO_PROJECT=true` + the `ADD_TO_PROJECT_PAT` secret (instructions are in the file header) - GitHub's built-in project auto-add (Step 2) is the primary path; this Action is the redundant backup.
- Commit ONLY the `.claude/` additions AND the `.github/` board payload, then push.

## Step 2 — Browser part (route to an Opus claude-in-chrome lane; the API cannot do this)

- Set the board Status options to EXACTLY these seven, with the standard descriptions:
  - Backlog — not started
  - Ready — approved and ready to be picked up by a worker
  - Building — a worker is actively implementing
  - QA — implementation done, under test
  - Review — awaiting human/code review
  - Done — merged and complete
  - Blocked — cannot proceed until something is unblocked
- Configure ALL built-in workflows (⋯ menu → Workflows) — set EVERY one deliberately, and READ each target back after saving; a wrong target silently corrupts the board:
  - **Auto-add to project**: enabled (repo, filter `is:issue is:open`).
  - **Item added to project**: enabled → Status: **Backlog**.
  - **Item closed**: enabled → Status: **Done**. ⚠ VERIFY THE TARGET COLUMN. On HeyLolo (2026-07-23) this workflow pointed at **Building** — and because **"Auto-close issue"** (Status=Done → close) was also on, the two formed a loop: set Done → auto-close → "Item closed" fires → card bounced to Building. Setting a card to Done actively reverted it, for two days, looking like cards "vanishing". Fingerprint of this failure: cards you move to Done reappear in another column within a minute. No API exposes workflow config, so the only check is reading the UI.
  - **Item reopened**: enabled → Status: **Building**.
  - **Pull request merged**: enabled → Status: **Done**.
  - **Auto-archive items**: **DISABLED**. Done cards are the system's visible history (anti-loop memory) — archiving hides them.
- SMOKE-VERIFY the workflow wiring before finishing: close a seeded test issue → its card must land in **Done** (not any other column) within a minute; reopen it → card returns to Building. If either lands elsewhere, the workflow target is wrong — fix it now, not later.
- GOTCHA: GitHub's visibility timer means Chrome must be FOREGROUNDED or the Authorize/Save buttons stay disabled.
- Create the four standard custom fields (Projects UI → "+" / "New field" in the table header of any view):
  - **Effort (tokens)** — type Number. Rough per-card effort/size for burn-up and prioritization.
  - **Target Date** — type Date. The date driving the Roadmap view.
  - **Priority** — type Single select, options `P1` / `P2` / `P3`.
  - **Test Area** — type Single select. NOTE: Test Area options are PER-PROJECT — do NOT hardcode; PROMPT THE OPERATOR for this project's areas (e.g. for a QA project: Login / Payments / Search / Reporting) and create those options. If the operator has none, create the field with no options and leave it for later.
- Create + save a **"Roadmap by Phase"** view: click the view tab "+" → set Layout = **Roadmap** → set the Date field = **Target Date** → slice/group by **Milestone** (markers = milestones) → Save as a new view named `Roadmap by Phase`. This gives the milestone-per-phase timeline; the saved-view link is shareable.
- Open the **Insights** tab (project → Insights) → add/select a **Burn-up** chart → group/filter by **Milestone** (optionally sum **Effort (tokens)**) → Save. Gives at-a-glance phase progress. (Insights is available on personal free/pro accounts.)

## Step 3 — Seed

- Create backlog issues. Each issue body has `## Context`, `## Steps`, `## Acceptance criteria` — with binary Given/When/Then acceptance criteria.
- Optionally seed history as closed + Done cards: closed issues bypass auto-add, so add them manually via `gh project item-add`, then `gh project item-edit` to set Status=Done. Discover the field id and option ids via `gh project field-list`.

## Board hygiene — the reconcile sweep (keep the board ALWAYS up to date)

The board is only trustworthy if card status matches issue reality. Two standing duties for every agent session working a board:

1. **Move the card the moment reality changes** — pick up = Building, implementation done = QA, awaiting human = Review, closed = Done. Closing an issue without its card landing in Done is a defect (the "Item closed → Done" workflow is the primary mechanism; `gh project item-edit` is the fallback the same minute).
2. **Run the reconcile sweep at session start and after any batch of closes.** It finds closed issues whose card is stranded outside Done (the exact corruption a mis-targeted "Item closed" workflow causes):

```bash
cat > /tmp/sweep.graphql <<'EOF'
query($endCursor: String) {
  user(login: "<OWNER>") {
    projectV2(number: <N>) {
      items(first: 100, after: $endCursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isArchived
          fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } }
          content { ... on Issue { number state repository { name } } }
        }
      }
    }
  }
}
EOF
# gh api graphql --paginate REQUIRES the cursor variable to be named $endCursor.
# Pipe the (concatenated-JSON) output through a JSONDecoder.raw_decode loop; flag
# every node where content.state == "CLOSED" && Status != "Done" && !isArchived,
# then fix each: gh project item-edit --project-id <PID> --id <itemId> \
#   --field-id <StatusFieldId> --single-select-option-id <DoneOptId>
```

(On Windows/git-bash: native `python.exe` cannot open `/tmp/...` — pipe the file via stdin, don't `open()` the MSYS path.) A sweep that finds >0 stranded cards means the workflow wiring is broken — re-run the Step 2 workflow verification, don't just patch the cards.

## Rules

- One board PER project.
- **WHY links matter (user, 2026-07-21): the board is the anti-loop memory.** Old issues get referenced when a similar problem returns — the links to dossiers, commits, and failed attempts are what stop the team from re-trying something already tried. Before solving any recurring symptom, SEARCH the board for prior cards on it and read their linked evidence first.
- Every commit/doc reference is a full clickable https:// link — NEVER a bare sha, NEVER a bare file path. This applies in chat with the user too: reference issues as full URLs, not "#N".
- **A doc link must RESOLVE before it goes on a card (HARD RULE, user-set 2026-07-21).** Referencing a doc by repo-relative path ("see docs/_session/<topic>/X.md") is a violation — the reader can't click it. Before referencing any doc on an issue/card/comment: (1) commit it, (2) push it to the branch that carries docs (e.g. the repo's docs/* branch on origin), (3) paste the full https://github.com/<owner>/<repo>/blob/<branch>/<path> URL. If a doc genuinely can't be pushed yet, paste its content into the issue body instead of naming the path. When a doc referenced earlier turns out to be link-less, fix the card the moment it's noticed — don't wait for the user to catch it.
- **Figma links on every design-tracking issue (user-set 2026-07-23).** Any issue whose work implements, rebuilds, or references a Figma design carries the FULL `https://www.figma.com/design/<fileKey>/...?node-id=<node>` URL in its `## Context` — added at creation, or the moment the design link becomes known (a dispatch brief that contains a Figma URL and an issue without it = a defect). Progress comments that reference specific frames link their node URLs too. Caught 2026-07-23: multiple design-driven cards (Explorer redesign, Quizzes) were dispatched with Figma nodes that never appeared on the issues.
- **Link EVERYTHING linkable (HARD RULE, user-set 2026-07-22).** If a thing has a canonical URL, every mention of it in a deliverable doc, card, comment, or report must be a clickable link: X handles → `[@handle](https://x.com/handle)`, GitHub users/repos/issues/commits → their https URLs, contracts/addresses/txs → block-explorer URLs, videos/channels → their URLs. A bare @handle, bare sha, bare address, or bare path is a defect ("if it's possible to be linked, the link should be there"). Exception: sections explicitly meant for copy-paste (e.g. a plain handle list for building an X List) stay plain. Run a link-lint pass over every deliverable doc before it ships; lanes producing docs must be told this rule in their prompt.
- **Documents as full GitHub blob URLs (user-locked 2026-07-22).** Whenever a repository document is referenced in any user-facing message, card, comment, or report, cite it as a FULL clickable `https://github.com/<owner>/<repo>/blob/main/<path>` URL — never a bare filename, never a relative path. Local absolute paths ONLY when the user must open/run the file locally (scripts, logs). Each project's `docs/README.md` is the master linked index (a Board feature-standard item) and is kept current whenever a doc is added or moved.
- `design`-labeled issues are human-owned and are NEVER dispatched to a worker.
- Ready is a live wire until the label filter ships — see https://github.com/Wladefant/soundcore-work-workflow/issues/26
- Token safety: Opus claude lanes do implementation (grok is reserved for X research and explicitly-requested jobs only); the session model is only for judgment + verification.
- Verify each phase with real `gh project view` / `gh project item-list` output — NEVER trust reports.

## Milestones & Labels

Milestones = roadmap phases. One milestone per roadmap phase (e.g. "Phase 0 - Install + Smoke", "Phase 4 - Governance track (on demand)"), created at seeding time. EVERY issue gets a milestone at creation. Never invent due dates - set a due date only when the roadmap actually commits to one.

Every issue gets a milestone AND at least one type label at creation time (gh issue create --label a,b --milestone "<phase>").

The standard 13-label taxonomy is created at seeding time (see Step 1 for the full `gh label create` commands). Type labels are universal across every project; domain labels are per-project examples to rename/adapt.

Discipline: every issue gets >=1 type label + domain labels at creation; labels are updated when scope changes (e.g. add `laptop` the moment a task turns out to need the work laptop). Prefer assigning the governance/on-demand phase to cross-phase history/risk cards rather than leaving them milestone-less. Environment-constraint labels like `laptop` double as dispatch filters: an agent session must not pick up a card labeled with an environment it does not have. Milestone views answer "how far is phase X" - keep them honest by closing issues only when their milestone-relevant work is truly done.
