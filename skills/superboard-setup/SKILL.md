---
name: superboard-setup
description: "Spin up Wlad's Superboard (GitHub-Projects agent pipeline from the Wladefant/super-board fork) on any project repo — board, columns, workflows, payload, config, labels. Use when the user says 'set up the board for <project>', 'superboard setup', or 'add this project to the board system'."
---

# Superboard Setup

Spin up Wlad's Superboard on a project: one board PER project. Route CLI work to an Opus claude subagent lane; route browser work to an Opus claude-in-chrome lane. The session model only does judgment + verification.

Install for yourself: copy this `superboard-setup/` directory into `~/.claude/skills/`.

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
- Every commit/doc reference is a full clickable https:// link — NEVER a bare sha.
- `design`-labeled issues are human-owned and are NEVER dispatched to a worker.
- Ready is a live wire until the label filter ships — see https://github.com/Wladefant/soundcore-work-workflow/issues/26
- Token safety: Opus claude lanes do implementation (grok is reserved for X research and explicitly-requested jobs only); the session model is only for judgment + verification.
- Verify each phase with real `gh project view` / `gh project item-list` output — NEVER trust reports.
