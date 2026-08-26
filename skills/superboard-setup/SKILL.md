---
name: superboard-setup
description: "Spin up Wlad's Superboard (GitHub-Projects agent pipeline from the Wladefant/super-board fork) on any project repo — board, columns, workflows, payload, config, labels. Use when the user says 'set up the board for <project>', 'superboard setup', or 'add this project to the board system'."
---

# Superboard Setup

Spin up Wlad's Superboard on a project: one board PER project. Route CLI work to an Opus claude subagent lane; route browser work to an Opus claude-in-chrome lane. The session model only does judgment + verification.

**Canonical home: this repo** (`skills/superboard-setup/SKILL.md`). The local `~/.claude/skills/superboard-setup` is a directory junction into a clone of this repo at `~/.claude/super-board-src` - edit here, `git commit` + `git push` to share, `git pull` to update. Never edit the local junction copy as a separate fork; there is one source of truth.

## Surface routing (apply automatically, do not wait to be told)

Full guide: [Agent Native operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md).

- **Work state** — issues, acceptance criteria, card status, priority → **Superboard**. Nothing else may hold a second Project status or completion ledger.
- **Model, context, tool profile, permission mode** → **Claudex**. Never set these in Agent Native, never improvise them in-session.
- **Reading/changing repos, running tests** → **Claude Code**.
- **Cockpit, transcripts, plans, recaps, previews, design, analytics, clips** → **Agent Native** (projection and presentation only; rebuildable, never authoritative).
- **Source, branches, PRs, checks, durable evidence, merge authority** → **GitHub**. Completion must be provable from GitHub alone.
- **UI/visual work** → Agent Native Design via the `design-prototyping` skill, before any production component is edited.
- **Standing constraint:** the public Agent Native cockpit never executes repository code — `AGENT_PROD_CODE_EXECUTION=off`, no Docker socket, no runner filesystem mount, no repository checkout, no trusted shell. Repo code runs only inside the private runner's ephemeral container/VM.

## New project bootstrap — the whole stack, in order

Use this when a project is starting from nothing. It is the map; Steps 0–5 below
are the detail. **The order is load-bearing** — each stage produces the thing the
next stage needs, and doing them out of order is how you end up with a deployed
app that no issue asked for.

**Board first.**

1. Identify the repo(s) — Step 0.
2. Create the board and link every repo; install payload, config and labels — Step 1.
3. Set Status options, workflows and custom fields in the browser; smoke-verify — Step 2.
4. Seed the backlog: issues with Context / Steps / binary acceptance criteria, each with a milestone and a type label — Step 3.

**Then repo and skills.**

5. Record which Agent Native surfaces the project uses, and generate its design skill — Step 4.

**Then deploy.**

6. Stand the app up on Dokploy — Step 5.

Two rules that decide where a task goes before you start:

- **Product work → that product's own board.** Global setup and tooling —
  Claudex, MCP servers, launchers, Superboard mechanics itself — goes to
  https://github.com/Wladefant/super-board and https://github.com/users/Wladefant/projects/5 instead.
- **No task exists outside an issue**, including this bootstrap. If you are
  doing stage 5 or 6, a card is already in Building.

## Step 0 — Identify the repo(s)

- Find the LIVE repo the user actually works in.
- Watch for forks: compare `pushedAt` of the fork vs its parent and prefer the FRESHER fork.
- A product may span multiple repos (e.g. a main/frontend repo + a backend repo) → still ONE board, with BOTH repos linked to it.

## Step 1 — CLI part (route to an Opus claude lane)

Collapse this into a SINGLE bash script the lane just executes (keeps the lane cheap and deterministic):

- Create the board: `gh project create --owner Wladefant --title "<Project>"`
- Link EACH repo via GraphQL mutation `linkProjectV2ToRepository` (note: `gh project link` may not exist in gh 2.39.1, so use the GraphQL mutation).
- Install the payload from https://github.com/Wladefant/super-board using its `install.sh` into the repo's `.claude/`.
- Write `.claude/super-board/configs/<slug>.json`. The executable contract is
  `scripts/super_board_runtime/config.py`; the field-by-field reference is
  [`config-schema.json`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/config-schema.json).
  A new board is written **switched off** and stays that way until the activation gate below
  is walked deliberately:
```json
{
  "version": 1,
  "project": { "owner": "Wladefant", "number": 0 },
  "repo": { "path": ".", "remote": "<owner>/<repo>" },
  "base_branch": "staging",
  "activation_mode": "off",
  "proof_issue_url": null,
  "worker_backend": "claude-p",
  "human_approves_merge": true,
  "merge_method": "rebase",
  "exclude_labels": ["design", "history"],
  "minimum_graphql_reserve": 1000,
  "max_workers": 2,
  "rebuild_cap": 2,
  "github_auth": {
    "mode": "interactive",
    "token_env_var": "SUPERBOARD_GITHUB_TOKEN",
    "login_env_var": "SUPERBOARD_GITHUB_LOGIN",
    "expected_login": null,
    "required_scopes": ["repo", "project", "read:org"]
  },
  "agent_native": { "enabled": false, "projection_only": true }
}
```
  Five of those keys are not decorative and are the ones people leave out — read
  "The shipped contract a new board inherits" below before changing any of them.
  There is no `columns` key: the lifecycle is fixed at seven statuses and is not
  configurable. `human_approves_merge` cannot be `false` and `merge_method` cannot be
  anything but `rebase` — either exits 65 at validation time. A config carrying anything
  that looks like credential material also exits 65: environment variables are named
  here, never valued.
- Validate it before committing — a partial config is worse than no config:
```
python scripts/super-board-config.py validate --config .claude/super-board/configs/<slug>.json --json
```
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
gh label create environment-constraint --force --color E99695 --description "Requires a specific machine or environment; doubles as a dispatch filter"
gh label create meeting-prep --force --color BFDADC --description "Preparation for a stakeholder meeting"
gh label create decision --force --color F9D0C4 --description "Blocked on or records a human decision"
gh label create risk --force --color B60205 --description "Documented open risk needing a policy call"
```
Note in prose that domain labels (ui, ado, test-data ...) are project-specific examples to rename per project, while type labels (build, docs, research, proof) are universal.

**`environment-constraint` is the canonical environment label; `laptop` is a preserved legacy
alias.** New projects create `environment-constraint` and use it everywhere. Boards created
before this release keep `laptop` working — `canonical_environment_label` in
`scripts/super_board_runtime/normalize.py` maps it onto `environment-constraint` during
intake, so an existing card labelled `laptop` normalizes correctly and nobody has to relabel
a backlog. Do not create `laptop` on a new board, and do not delete it from an old one; on a
board that carries both, the alias resolves to the same constraint.
- Copy the `.github` board payload into the target repo (the payload lives at `payload/github/` in the super-board clone; `install.sh` also copies it, but do it explicitly here so it lands and commits in the same script):
```
mkdir -p .github/ISSUE_TEMPLATE .github/workflows
cp "$SB_SRC/payload/github/ISSUE_TEMPLATE/superboard-issue.yml" .github/ISSUE_TEMPLATE/
cp "$SB_SRC/payload/github/ISSUE_TEMPLATE/config.yml"           .github/ISSUE_TEMPLATE/
cp "$SB_SRC/payload/github/workflows/auto-add-to-project.yml"   .github/workflows/
cp "$SB_SRC/payload/github/workflows/super-board-normalize.yml" .github/workflows/
# The board URL is a repository variable, not a placeholder in the file:
gh variable set SUPERBOARD_PROJECT_URL --body "https://github.com/users/Wladefant/projects/<N>"

# The normalizer plans against a COMPLETE board snapshot or it refuses, so it
# needs the board coordinates and a Projects READ credential. Until all four are
# set the workflow stays inert (the enable flag gates the job) rather than
# running and producing nothing:
gh variable set SUPERBOARD_PROJECT_OWNER  --body "<owner-login>"
gh variable set SUPERBOARD_PROJECT_NUMBER --body "<N>"
gh secret   set SUPERBOARD_PROJECT_READ_TOKEN   # Projects READ for that owner
gh variable set ENABLE_SUPERBOARD_NORMALIZE --body "true"
```
  **`SUPERBOARD_PROJECT_OWNER` is the owner LOGIN, and it may be a user or an
  organization.** Both shapes are live: the Master board
  <https://github.com/users/Wladefant/projects/5> is user-owned, and the product board
  <https://github.com/orgs/Bavariance/projects/1> is organization-owned. There is
  **no owner-type variable to set** — the board read is `repositoryOwner(login:)` with an
  inline fragment per concrete owner type (`super-board-project.py query`, the single
  authority for that query), so either kind resolves without being declared. That is
  deliberate: an owner-type input is one more value an operator can set wrong, and getting
  it wrong is invisible. A `user(login:)` query against an org board resolves to `null`,
  `null` reads as a board with no items, and the normalizer plans nothing while the job
  reports success. `super-board-project.py pages` refuses that shape outright — an
  unresolved owner (`project-owner-unresolved`) or an absent Project (`project-not-found`)
  exits 65 and writes no snapshot, and `set -euo pipefail` turns it into a failed job.
  (`$SB_SRC` = the super-board clone, `~/.claude/super-board-src`.) This installs the structured Issue Form (enforced Context / Steps / Acceptance criteria / Test Area / Priority / Work type / Environment constraint / Branch route / Milestone), the continuous intake-and-closure normalizer, and the fallback auto-add workflow. That fallback stays OFF - it is armed only through the re-enable gate documented in `references/onboard.md`, never by setup, install, or activation. GitHub's built-in project auto-add (Step 2) is the primary path.
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
- Configure ALL built-in workflows (⋯ menu → Workflows) — set EVERY one deliberately, and READ each target back after saving; a wrong target silently corrupts the board. These are the seven targets, and there are no others:

| Built-in workflow | Setting |
|---|---|
| Auto-add to project | **enabled** (repo, filter `is:issue is:open`) |
| Item added to project | **enabled** → Status: **Backlog** |
| Item closed | **enabled** → Status: **Done** |
| Item reopened | **enabled** → Status: **Backlog** |
| Pull request merged | **enabled** → Status: **Done** |
| Auto-archive items | **DISABLED** |
| Auto-close issue | **DISABLED** |

  Every one of the seven has shipped mis-set or unsaved on a real board. Read all seven back; these five carry the scars:

  - **Item closed**: enabled → Status: **Done**. This is a **transport transition, not a verdict.** The built-in workflow just gets the card out of the active columns; the closure normalizer (`normalize_closure`) re-examines it afterwards and demands evidence. A closed issue with no accepted completion evidence, no linked duplicate, and no stated not-planned decision is reopened and moved to **Blocked** with a corrective comment — so the column this workflow writes is provisional until closure normalization has validated it. Wiring this workflow at anything other than Done breaks that hand-off. ⚠ VERIFY THE TARGET COLUMN. On HeyLolo (2026-07-23) this workflow pointed at **Building** — and because **"Auto-close issue"** (Status=Done → close) was also on, the two formed a loop: set Done → auto-close → "Item closed" fires → card bounced to Building. Setting a card to Done actively reverted it, for two days, looking like cards "vanishing". Fingerprint of this failure: cards you move to Done reappear in another column within a minute. No API exposes workflow config, so the only check is reading the UI.
  - **Item reopened**: enabled → Status: **Backlog**. ⚠ THIS TARGET CHANGED IN 2.0 — earlier revisions of this file said **Building**, and that was wrong. A reopened issue is not automatically in-flight work: nobody is holding it, no branch was cut for this pass, no worker claimed it. Sending it to Building fabricates progress and lets a card claim a lane it does not have — and because `Ready` is now the only dispatchable status, a card parked in a fabricated Building is invisible to dispatch *and* counted as active. Reopened means "back in the queue": **Backlog**. This is also what the runtime plans for itself — `normalize_intake` sets `desired_status = "Backlog"` on the `reopened` event — so a board wired to Building fights its own normalizer on every reopen.
  - **Pull request merged**: enabled → Status: **Done**.
  - **Auto-archive items**: **DISABLED**. Done cards are the system's visible history (anti-loop memory) — archiving hides them.
  - **Auto-close issue: DISABLED.** ⚠ SECOND, NASTIER VARIANT (found on BOTH boards 2026-07-25): "Auto-close issue" shipped ON with trigger **Status updated -> Building**, while "Item closed" pointed at **Building** and "Pull request merged" also pointed at **Building**, and "Item reopened" was OFF/unsaved. That combination is silently destructive in a way the HeyLolo loop is not: **dragging any card into Building auto-closed its issue**, and closed issues pinned themselves at Building. Cards do NOT bounce, so the corruption is invisible to the reconcile sweep's usual fingerprint - the sweep instead reports closed issues stranded in QA/Building/Review (20 of them here). Lesson: EVERY workflow ships mis-set or unsaved until proven otherwise - read back all seven targets, never just the one you suspect.
- SMOKE-VERIFY the workflow wiring before finishing: close a seeded test issue → its card must land in **Done** (not any other column) within a minute; reopen it → card returns to **Backlog**. If either lands elsewhere, the workflow target is wrong — fix it now, not later.
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

## Step 4 — Project skills (route to an Opus claude lane)

The board now exists. Give the project the two things a session needs in order to
work on it without being told: which Agent Native surfaces it uses, and where its
design work lives.

**4a — Record the surfaces.** Amend the config written in Step 1 with these
ADDITIVE keys (leave every existing key exactly as Step 1 wrote it):

```json
{
  "design_skill": {
    "enabled": true,
    "label": "design",
    "skill": "<slug>-design"
  },
  "agent_native": {
    "enabled": true,
    "projection_only": true
  },
  "deploy": {
    "provider": "dokploy",
    "auto_deploy": false,
    "domain": "<app>.wladefant.de"
  }
}
```

⚠ **These three keys are MAPPINGS, not strings or flag bags. Corrected in 2.0 —
earlier revisions of this file were wrong and produced a config the runtime
refuses.** `design_skill` written as the bare string `"<slug>-design"` fails
validation with `design-skill-invalid` and exit 65; the executable contract is
`scripts/super_board_runtime/config.py` (`_normalize_design_skill`,
`_normalize_agent_native`, `_normalize_deploy`), and per
[DOCS-SYSTEM.md](https://github.com/Wladefant/super-board/blob/main/DOCS-SYSTEM.md)
the code wins whenever this file disagrees. Read the keys back from a
`super-board-config.py validate --json` run rather than trusting this snippet.

`agent_native.projection_only` may only ever be `true`; setting it `false` raises
`agent-native-must-be-projection-only`. The per-surface switches (design, plan,
analytics, clips) are **not** config keys — the real capability declaration lives
in the runtime's `payload/agent-native/super-board.json`. `deploy` reads
`provider` and `auto_deploy`; `domain` is carried for humans and is not consumed
by the validator. `minimum_graphql_reserve` is a TOP-LEVEL key, not nested inside
`github_auth`.

Set each surface from what the project will actually use — do not switch them all
on. Which surface is allowed to own what is not a per-project choice: it is the
[Agent Native operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md),
and this block only records which of them are in play. In particular Agent Native
stays a projection here — the board is still the only place work exists.

**4b — Generate the design skill.** The
[`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md)
skill looks for a per-project `<slug>-design` skill holding real tokens, brand and
staging URLs. Generate it:

```bash
PROJECT="<Project>" SLUG=<slug> REPO=<owner>/<repo> \
BOARD=https://github.com/users/Wladefant/projects/<N> \
PROD=https://<app>.wladefant.de STAGING=<staging url> \
TOKENS=<path to the real token file, e.g. app/globals.css> \
bash "$SB_SRC/skills/superboard-setup/scripts/new-project-design-skill.sh"
```

It writes `skills/<slug>-design/SKILL.md` into the super-board clone and junctions
it into `~/.claude/skills`, matching how `superboard-setup` and `claudex-optimized`
are wired — so it is versioned, shared, and actually loaded.

Every variable is required and there are no defaults, deliberately: a design skill
carrying a guessed token or a guessed staging URL is worse than no skill, because
it will be trusted. The generated file has three marked blocks — TOKENS, BRAND,
SURFACES — that are **not** filled by the script. Fill them from the repo's real
token file and the real running UI in the same session, then commit. See
[`polysim-design`](https://github.com/Wladefant/super-board/blob/main/skills/polysim-design/SKILL.md)
for what filled-in looks like.

If the project has no UI, skip 4b and set `"design_skill": {"enabled": false}`. A bare `null` is accepted (it normalizes to the disabled default) but the explicit mapping documents the intent.

## Step 5 — Deploy path (last, never first)

Full runbook: [Deploying a new app on Dokploy](https://github.com/Wladefant/super-board/blob/main/docs/runbooks/DOKPLOY-NEW-APP.md).
Three things that will cost you a day if they are not read before you start:

- **Pick the GitHub provider by repo OWNER.** Personal **Wladefant** repos MUST
  use `Dokploy-2025-10-26-hostinger` (githubId `Y4Ma48-dyFxauwE5Jo4L0`,
  installation 91677512). `Dokploy-Bavariance` (githubId
  `0-mOov2-Synn7Cl3JzfbP`) is installed on the **Bavariance org** and will
  silently appear to work on a *public* personal repo — it is only doing an
  anonymous clone — then break the moment the repo is private, with an error that
  looks like a credential problem and is not. **Never copy a `githubId` from an
  existing app's config**; that is exactly how the wrong one spreads. Read
  `gitProvider.getAll` and match the installation to the owner.
- **`*.wladefant.de` is a Cloudflare wildcard** — a new subdomain needs no DNS step.
- **Never add a Docker `HEALTHCHECK`** on this swarm; it caused a permanent 502
  crash-loop. Plain `nginx:alpine` works for static sites.

Deploying is not completion. The card moves on evidence in GitHub.

## The shipped contract a new board inherits

Setting up a board is not only creating columns — it is opting the project into a runtime
whose safety properties are non-negotiable and mostly invisible until they fire. Every one
of them exists because its absence produced a real failure. Read this section before
provisioning; a board configured against these rules will refuse to run, and the refusal
will look like a bug.

Two kinds of rule appear below, and the difference matters:

- **Enforced** — a named function refuses it, as of
  [v2.0.0](https://github.com/Wladefant/super-board/blob/main/RELEASE-NOTES.md). Nothing at
  runtime gets past it.
- **Governance** — a rule about how humans change the configuration, which no validator can
  see, because a config file does not know how it was edited or who reviewed it. Marked as
  governance wherever it appears. Calling one of these "enforced in code" is worse than
  saying nothing: it invites somebody to rely on a check that does not exist.

### Activation — a new board is OFF, and only a reviewed pull request moves it

`activation_mode` has exactly three values, and nothing at runtime can bypass them
(`scripts/super_board_runtime/activation.py`):

| Mode | What dispatches | `proof_issue_url` |
|---|---|---|
| `off` | Nothing. Not even a perfectly formed `Ready` card. | must be `null` |
| `proof-only` | Exactly one allowlisted issue. Every other card is refused with `activation-not-allowlisted`. | must be an exact issue URL inside `repo.remote`, and that issue must be OPEN |
| `active` | Normal dispatch; every eligibility rule below still applies. | must be `null` again |

**Enforced.** The climb is one rung at a time — `off` → `proof-only` → `active` — and
`validate_activation_transition` in the same module refuses a skipped rung with
`activation-ladder-skipped` (exit 65). Pass the mode the config declared BEFORE the change:

```
python -m super_board_runtime.activation --config <cfg> --previous-mode <old-mode>
```

Descending is always permitted, to any depth. The ladder slows a board being armed, never a
board being turned off.

**Governance, not enforced.** Each step should arrive as its own human-reviewed
configuration pull request — not a chat approval, not a local edit, not a flag on the run
command: a diff somebody read. No validator can check this, because a config file does not
know how it was edited. This section used to claim every rule under it was enforced in code;
this one never was. A new board is written at `off` and stays there until the installation
and repository gates have actually been walked on that project.

The mode is re-read from disk immediately before a claim and immediately before a launch, so
flipping a board back to `off` mid-run aborts the very next claim rather than the run after
it. There is no runtime command, flag, or environment variable that dispatches past the
mode.

`proof-only` is the proving ground: it exists so a board's first real dispatch is a single
known card you are watching, not a backlog. `repo` may not be `null` in `proof-only`,
because the allowlisted issue has to be proven to live inside this repository.

### Dispatch eligibility — `Ready` and nothing else

`scripts/super_board_runtime/eligibility.py` is one implementation shared by every dispatcher
path — the read-only planner, the headless dispatcher, and the dynamic workflow — precisely
so a card cannot be eligible in one and ineligible in another.

- **The status must be EXACTLY `Ready`.** No other status is dispatchable. There is no
  "eligible for the requested lane" concept, no dispatching out of Building to resume, no
  picking a card up from Blocked. This is the rule that makes a mis-wired "Item reopened"
  workflow expensive rather than cosmetic.
- **Only OPEN issue cards dispatch.** Pull-request cards never dispatch, draft cards never
  dispatch, and a closed issue whose Status drifted is skipped rather than built. A failed
  or missing state lookup is `issue-state-unavailable` — never a permissive fallback.
- A card carrying an assignee is already claimed; the assignee is the claim mutex.

### Excluded labels — enforced everywhere, not advisory

`exclude_labels` is part of the config schema and is enforced in **every** dispatcher path.
It used to be silently ignored by the dispatchers, which is why a `history` or `design` card
dragged into `Ready` would be built as if it were work; that hazard is closed.

`design` and `history` are **permanently non-dispatchable** whether or not they appear in
`exclude_labels` — listing them adds nothing and removing them takes nothing away. Anything
you list is added to those two. Comparison is case-insensitive after trimming, and rejection
short-circuits before the issue-state lookup, so an excluded card costs no API call at all.

### Branch routing — fail-closed, declared in the issue body

Every dispatchable issue declares exactly one explicit route, in a normalized line in its
body, and nothing else routes anything (`scripts/super_board_runtime/routing.py`):

```
Branch route: staging
```

That declaration is the Issue Form's `branch-route` field, it is parsed into the
`branch_route` value on the normalized intake form, and the config's `branch_routes` table
maps route labels onto branches. All three name the same decision; none of them is optional
for a card you intend to dispatch.

Ineligible, before a branch is created:

| Reason code | What it means |
|---|---|
| `route-declaration-missing` | no declaration at all |
| `route-declaration-unknown` | `default`, `main`, or any branch that is not a declared route |
| `route-declaration-duplicate` | two declarations — even two identical ones |
| `route-label-conflict` | the declaration and the card's route labels disagree |

Three things that have each been mistaken for a route and are not one: **a Test Area never
implies a route** (it is a QA slice, not a branch); a label on its own never implies a route;
whatever branch happens to be checked out never implies a route. **A design branch is never
a dispatch route** for any card, `design`-labelled or not. Two declarations means two
intentions, and picking one is guessing — so the card fails instead.

The check runs *before* `create_branch_for_route` calls the branch creator, so an ineligible
card never leaves a half-created branch behind. `verify_pull_request_base` re-checks the same
route before QA and before Review, because a base branch edited after the pull request was
opened silently re-targets the whole change.

### GraphQL quota — an immutable 1,000-point reserve

`minimum_graphql_reserve` is a floor of 1,000 points the pipeline will not spend. A
configuration may **RAISE** it; lowering it exits 65
(`scripts/super_board_runtime/quota.py`). The rules that go with it:

- **One cached inventory per runtime cycle.** The quota is read once per tick and every
  check inside that tick reuses it — the guard must not become the thing that drains the
  bucket.
- **Estimate the cost of a mutation before executing it.** `remaining - estimated_cost >=
  effective_floor` is required, and the estimate is mandatory: a missing or non-positive
  estimate is an error, not a free pass.
- **Bounded batches** — no more than 25 records per mutation batch, and pagination is capped.
- **Stop at the reserve.** Reaching it exits 75, cleanly. **Never retry-spin, and never sleep
  through a reset.** The old code assumed 5,000 points whenever `gh` failed, which is exactly
  how an empty bucket looked like a full one; an unreadable quota response now raises instead
  of failing open.
- **Prefer the built-in Project workflows over API item-adds.** The seven workflows in Step 2
  move cards for free; every item-add you script yourself spends the same bucket the lanes
  need.
- **Log only the four safe quota fields** — remaining points, estimated cost, effective
  floor, reset time. No token, header, cookie, or raw payload.

This supersedes the old 200-point threshold and the old "sleep until the reset" advice
wherever a runbook still repeats them. Both come from before the #381 worker storm, where
workers sharing one token bucket drained it because nothing in the worker contract told them
to watch it.

### GitHub identity — a machine-account classic PAT, or nothing

Unattended Projects v2 mutation accepts **only** a machine-account **classic** PAT carrying
scopes `repo`, `project`, `read:org`, supplied through the environment variable
`SUPERBOARD_GITHUB_TOKEN`, with the expected account login in `SUPERBOARD_GITHUB_LOGIN`
(`scripts/super_board_runtime/auth.py`). Interactive mode uses the signed-in session identity
and reads no environment credential at all.

**GitHub Apps cannot access personal Projects v2 at all** — only organization-owned ones. An
App installation token is refused outright rather than probed, because no amount of
capability checking can rescue it and the failure would land in the middle of a run. A
fine-grained PAT is refused for the same reason, as is any token whose OAuth scope header is
absent or unparseable. Any runbook that sends an operator to install an App for a personal
board is sending them down a path that cannot work, and the failure will read like a
permissions problem rather than an impossibility.

Everything fails closed (exit 69): a missing variable, the wrong login, a missing scope, an
inaccessible repository, an inaccessible Project, or a capability that cannot be confirmed.

**Reference environment variables by NAME only** — in the config, in issues, in cards, in
comments, in reports, and in anything you paste to the operator. Never a value, never a
prefix, never "it starts with". A config carrying anything credential-shaped exits 65 before
it runs.

### QA is bound to an exact SHA

QA proves one thing: *this exact commit* passed (`scripts/super_board_runtime/qa.py`).

1. Record the pull request's `headRefOid` **before** any test command runs.
2. Test exactly that SHA, in an isolated **locked** worktree detached at that commit. A
   mutable branch checkout is never QA authority — a branch can move under the run and the
   evidence would name a commit that was never tested.
3. Reread the head **after** the run.
4. Publish a **SHA-bound** check only when the reread head equals the tested SHA.
5. Release the lock and the worktree on **every** terminal path — success, test failure,
   exception, stale head, and signal.

**Later commits inherit no passing QA.** Every head change invalidates it. The current head
must equal the tested SHA before the card moves to Review, and again immediately before a
human merges. A missing, ambiguous, changed, or unreadable head refuses to run rather than
falling back to "whatever is checked out". On failure the card never merges and never moves
to the completion column: it goes to Building when the current worker can repair it, or to
Blocked when external input is needed.

### Merge is human-only and rebase-only

**The runtime never merges.** It creates branches, pushes commits, opens and updates pull
requests, runs QA and review, publishes sanitized evidence, moves a successful card to
Review — and then it stops. It never enables an automatic-merge setting, never closes an
implementation issue as a substitute for a merge, and never moves a card to the completion
column before a real human merge. Done is produced by the built-in workflows and the closure
normalizer *after* that merge.

Set these on every linked repository:

```
allow_rebase_merge: true
allow_squash_merge:  false
allow_merge_commit:  false
```

Squash collapses the TDD breadcrumb trail that `git blame` and `git bisect` depend on; a
merge commit hides it. Rebase keeps every commit. `human_approves_merge` must be `true` and
`merge_method` must be `rebase`, and there is no supported way to configure otherwise.

This is not a convention — `scan_merge_prohibitions`
(`scripts/super_board_runtime/review.py`) is a tree-wide scanner and a **release gate**: it
source-scans every executable runtime, workflow, skill, and reviewer path for all eight ways
a merge can happen, and any active occurrence fails the release. Run it against the installed
tree — `scan_merge_prohibitions(Path(".claude"))` — as well as against the source repository;
it excludes its own module intrinsically and distinguishes a prohibition statement from an
active instruction on its own, so it needs no file that installation leaves behind. The source
repository keeps a small allowlist for its own test fixtures, listing paths by name — never a
path heuristic, because "skip anything under docs/" is exactly how a real merge path hides in
a file called `docs/deploy-helper.sh`.

### The review gate — one parallel Codex fleet, every finding fixed

Exactly **ONE** parallel maximum-level **local** Codex fleet per code pull request: one
structured diff review plus three lenses — correctness, security, and performance /
design-consistency — run concurrently. **Every finding must be resolved, including nits.**
No confidence-threshold filtering, no "skip the low ones".

**Every lens redirects stdin: `codex exec … "<lens prompt>" < /dev/null > "$OUT" 2>&1`.**
`codex exec` with a prompt argument reads stdin when no terminal is attached — backgrounded,
in CI, or inside a subagent — and blocks forever, emitting exactly one line
(`Reading additional input from stdin...`) and then nothing: no error, no timeout, no exit.
**`codex exec review` is not affected**, because it takes no prompt argument, and that
asymmetry is what makes the failure easy to miss — the structured lens returns a normal
review while the prompted lenses sit frozen, so the fleet reports a quarter of its coverage
as if it were complete. It happened on this release's own review gate. To detect it, check
output byte counts about 60 seconds after launch (`wc -c *.txt`): a file frozen at ~39 bytes
is deadlocked, not thinking. `super_board_runtime.review` passes `stdin=DEVNULL` in code, and
a test pins it.

**No second fleet unless the operator asks.** CodeRabbit, Copilot, Greptile and the GitHub
`@codex` connector are non-binding and are **not** gates — the connector in particular
carries its own easily-exhausted review rate limit that has produced false "usage limit"
reports while the task budget was untouched. Documentation-only diffs are exempt: maximum-effort
lenses over a Markdown change burn usage for nothing. Only sanitized summaries are published;
raw lens output is unbounded text from a tool and goes through the publication boundary like
everything else.

### Closure needs evidence — closed is not the same as done

`normalize_closure` is the only actor allowed to produce the completion status, and only
after a confirmed external merge or an accepted disposition. A **closed issue** moves to the
completion column only with:

- accepted, typed and **linked** completion evidence; or
- a linked **duplicate** — the issue that survives; or
- a **not-planned** decision that says what was decided.

Anything else is **reopened, moved to Blocked, and given a corrective comment** naming what
is missing. A **closed-unmerged pull request** needs linked abandonment or supersession
evidence, else the card goes to Blocked; the runtime never reopens a closed pull request,
because the branch is the author's.

**Pre-activation historical evidence is never rewritten to manufacture acceptance.** Cards
that were closed before the board was activated keep their original evidence untouched. A
board that back-fills plausible evidence onto old cards is a board whose history proves
nothing.

### Compare before mutate, and recapture the delta every batch

A mutation is authorized by state reread **at decision time**, never by state captured during
preflight (`scripts/super_board_runtime/project.py`). Before applying any classification
record:

1. reread the item by its **immutable node ID** (not by issue number, not by title — a
   transferred or recreated issue is a different item);
2. reread repository state;
3. reread the Project field and option IDs by name, and the current values;
4. compare all of it against the manifest's expected preconditions;
5. **quarantine** anything that changed, with zero writes.

A record whose `updated_at` merely *differs* — in **either** direction — quarantines too: a
newer human or automation decision must never be overwritten, and an older timestamp means we
are reading something we do not understand. A decision that says "apply" still reads back
immediately after writing, and a readback that disagrees is a conflict, not a success.

**Capture created-OR-CHANGED deltas before every mutation batch** — not only at the start of
the run, and not only for newly created items. A batch that recaptures only new items will
happily overwrite an item a human edited thirty seconds ago.

Inventory reads are paginated, capped, and refuse to return a partial snapshot when a page
fails: a snapshot missing 200 cards looks exactly like a board that lost 200 cards, and
reconciliation built on it would "fix" the difference.

### One sanitizer, immediately before the GitHub write

There is exactly ONE sanitizer and it sits immediately before the write boundary
(`scripts/super_board_runtime/publication.py`). Not one per surface, not one per script —
one, because a second sanitizer is a second place to forget a category, and the forgotten
categories are the ones that leak. The order is not negotiable:

1. **Render** the complete payload — a secret can be split across two template fragments and
   neither fragment matches anything on its own;
2. **redact** known environment values and recognized secret patterns from that complete text;
3. **scan the complete redacted payload again** — redaction is best-effort, detection is the
   gate;
4. **fail closed** with **no partial write** — the writer is never called;
5. only then **write**.

It covers every GitHub-bound surface: issue bodies, pull-request bodies and comments, review
summaries, QA comments, checks, commit statuses, closure comments, release text, and Project
text fields and manifests. A surface not on that list cannot be published at all. Failure
reports name the category and the offset, never the matched value — a leak report that quotes
the leak is a second leak.

### The installer is pinned, checksummed, and provably idempotent

`install.sh` is versioned, **source-commit pinned**, checksummed, manifest-driven and
idempotent. It enumerates the payload up front and fails with
`install-payload-incomplete` before copying a single byte if an asset is missing; it refuses
a source tree whose HEAD is not exactly the pinned `--source-sha`; it checksums every
installed file into `.claude/super-board/install-manifest.json` alongside the release
version, source repository and SHA, config schema version, and install timestamp. Stale
removal only ever touches paths the **prior manifest owned**, so an operator's own files in
`.claude/` survive an upgrade. A downgrade is refused unless explicitly allowed.

It installs the **complete** asset set — including the comment sweeper
(`scripts/super-board-sweep-comments.mjs`) and the executable QA assets — and fails closed on
any missing or mismatched asset. Half a payload is worse than none: it produces a board
running a new skill against an old dispatcher, and the symptom surfaces three steps later as
a policy that should have been impossible to reach.

**Prove idempotency by snapshot → reinstall → snapshot → byte-for-byte compare**, not by a
working-tree `git diff`. The `git diff` is dominated by the first install's own output, so it
can read clean while the second install rewrites half the payload — and it cannot represent an
ownership shift at all.

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

- **One board PER project**, and a product's repos share one board. Above them sits a single
  Master Board ([#6](https://github.com/users/Wladefant/projects/6)) which receives **ONLY**
  abstract per-project program epics — one card per project, linking to that project's own
  board. Granular product cards never go on the Master Board; bulk-adding dev issues to it is
  how the cross-project view stops being readable.
- **Project card adds and status changes are top-level direct orchestration.** The session
  does them itself — GitHub MCP where it exposes Projects v2 mutations, otherwise targeted
  `gh api graphql` (`addProjectV2ItemById`, `updateProjectV2ItemFieldValue`) — and **never
  delegates a card move, card add, or status check to a subagent.** A subagent that reports
  "moved the card" has given you a claim, not a verified mutation.
- **WHY links matter (user, 2026-07-21): the board is the anti-loop memory.** Old issues get referenced when a similar problem returns — the links to dossiers, commits, and failed attempts are what stop the team from re-trying something already tried. Before solving any recurring symptom, SEARCH the board for prior cards on it and read their linked evidence first.
- Every commit/doc reference is a full clickable https:// link — NEVER a bare sha, NEVER a bare file path. This applies in chat with the user too: reference issues as full URLs, not "#N".
- **A doc link must RESOLVE before it goes on a card (HARD RULE, user-set 2026-07-21).** Referencing a doc by repo-relative path ("see docs/_session/<topic>/X.md") is a violation — the reader can't click it. Before referencing any doc on an issue/card/comment: (1) commit it, (2) push it to the branch that carries docs (e.g. the repo's docs/* branch on origin), (3) paste the full https://github.com/<owner>/<repo>/blob/<branch>/<path> URL. If a doc genuinely can't be pushed yet, paste its content into the issue body instead of naming the path. When a doc referenced earlier turns out to be link-less, fix the card the moment it's noticed — don't wait for the user to catch it.
- **Figma links on every design-tracking issue (user-set 2026-07-23).** Any issue whose work implements, rebuilds, or references a Figma design carries the FULL `https://www.figma.com/design/<fileKey>/...?node-id=<node>` URL in its `## Context` — added at creation, or the moment the design link becomes known (a dispatch brief that contains a Figma URL and an issue without it = a defect). Progress comments that reference specific frames link their node URLs too. Caught 2026-07-23: multiple design-driven cards (Explorer redesign, Quizzes) were dispatched with Figma nodes that never appeared on the issues.
- **Link EVERYTHING linkable (HARD RULE, user-set 2026-07-22).** If a thing has a canonical URL, every mention of it in a deliverable doc, card, comment, or report must be a clickable link: X handles → `[@handle](https://x.com/handle)`, GitHub users/repos/issues/commits → their https URLs, contracts/addresses/txs → block-explorer URLs, videos/channels → their URLs. A bare @handle, bare sha, bare address, or bare path is a defect ("if it's possible to be linked, the link should be there"). Exception: sections explicitly meant for copy-paste (e.g. a plain handle list for building an X List) stay plain. Run a link-lint pass over every deliverable doc before it ships; lanes producing docs must be told this rule in their prompt.
- **Documents as full GitHub blob URLs (user-locked 2026-07-22).** Whenever a repository document is referenced in any user-facing message, card, comment, or report, cite it as a FULL clickable `https://github.com/<owner>/<repo>/blob/main/<path>` URL — never a bare filename, never a relative path. Local absolute paths ONLY when the user must open/run the file locally (scripts, logs). Each project's `docs/README.md` is the master linked index (a Board feature-standard item) and is kept current whenever a doc is added or moved.
- `design`-labeled issues are human-owned and are NEVER dispatched to a worker. As of
  [v2.0.0](https://github.com/Wladefant/super-board/blob/main/RELEASE-NOTES.md) that is
  enforced in code, not trusted to discipline: `design` and `history` are permanently
  non-dispatchable in every dispatcher path. The old hazard — a `history` or `design` card
  dragged into `Ready` being built as work, tracked in
  https://github.com/Wladefant/soundcore-work-workflow/issues/26 — is closed. `Ready` is
  still the live wire in the sense that matters: it is the ONLY dispatchable status, so
  dragging a card there is the act of releasing it to a worker.
- Token safety: Opus claude lanes do implementation (grok is reserved for X research and explicitly-requested jobs only); the session model is only for judgment + verification.
- Verify each phase with real `gh project view` / `gh project item-list` output — NEVER trust reports.
- **Harvest all three GitHub surfaces** (user-set 2026-08-26). Issue comments, inline review comments, and PR review objects. Codex is `chatgpt-codex-connector[bot]`; Copilot is `copilot-pull-request-reviewer[bot]`. Silence from an exhausted bot is not approval. Contract: [`references/github-ops.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/github-ops.md).
- **Never self-approve.** GitHub 422s `APPROVE` and `REQUEST_CHANGES` on your own PR. Post `COMMENT` and tell the operator a non-author must click Approve. Re-resolve the full head SHA over REST before recording any approval.
- **Closing keywords close the issue.** `Closes` / `Fixes` / `Resolves #N` fire on merge even for a partial PR. Use `Part of` plus the full issue URL unless every acceptance criterion is met.
- **Nested spawning is disabled.** A subagent that tries to delegate dies with a preamble and no work. State that in every prompt. Announce file ownership over IRC; one worktree per writer; push early.
- **Never move a card to Done on inference.** Require the merged PR or closed-issue evidence. A card stuck in Building with no live branch and no open PR is a triage signal. Cross-check branches and open PRs before calling an issue unclaimed.

## Milestones & Labels

Milestones = roadmap phases. One milestone per roadmap phase (e.g. "Phase 0 - Install + Smoke", "Phase 4 - Governance track (on demand)"), created at seeding time. EVERY issue gets a milestone at creation. Never invent due dates - set a due date only when the roadmap actually commits to one.

Every issue gets a milestone AND at least one type label at creation time (gh issue create --label a,b --milestone "<phase>").

The standard 13-label taxonomy is created at seeding time (see Step 1 for the full `gh label create` commands). Type labels are universal across every project; domain labels are per-project examples to rename/adapt.

Discipline: every issue gets >=1 type label + domain labels at creation; labels are updated when scope changes (e.g. add `environment-constraint` the moment a task turns out to need a specific machine). Prefer assigning the governance/on-demand phase to cross-phase history/risk cards rather than leaving them milestone-less. `environment-constraint` doubles as a dispatch filter: an agent session must not pick up a card labeled with an environment it does not have. `laptop` is the preserved legacy alias for it and normalizes to the same constraint on existing boards; new boards use `environment-constraint`. Milestone views answer "how far is phase X" - keep them honest by closing issues only when their milestone-relevant work is truly done.
