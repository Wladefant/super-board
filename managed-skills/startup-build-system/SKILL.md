---
name: startup-build-system
description: "How we build a startup product with agent lanes and add features. Covers stages, lane routing and briefs, and the feature loop (issue, red test, fix, review, merge, live check). Also covers lane databases, the merge train with its Dokploy fast-merge gate, revert on failure, QA and costly anti-patterns. Source: Shipnovo Main session, 2026-09-20 to 2026-10-10."
---

> Source of truth: [`managed-skills/startup-build-system/SKILL.md`](https://github.com/Wladefant/super-board/blob/main/managed-skills/startup-build-system/SKILL.md) in Wladefant/super-board. Edit it there and merge. Then `python scripts/install-managed-skills.py startup-build-system` copies it to `~/.veyyon/profiles/default/agent/managed-skills/`. The installer backs up a local edit and then overwrites it.

# Startup build system

Use this skill when you plan, build or extend a startup product with Veyyon lanes.
It records what worked on Shipnovo (https://shipnovo.app, repo Wladefant/shipnovo) from 2026-09-20 to 2026-10-10.
The numbers behind it are in https://github.com/Wladefant/super-board/issues/749 (case study) and https://github.com/Wladefant/super-board/issues/747 (waits, checks, wake-ups).

Related skills: `startup-launch-setup` (accounts, analytics, legal), `competitor-deep-dive` (research), `flow-qa` (user-flow QA), `fetch-before-dispatch`, `veyyon-worktree-node-modules-safety`, `ste-writing`.

## 1. Stages

1. **Day 0: spec.** One page: users, the 5 core flows, what is out of scope. The operator agrees before any code. AGENTS.md gets `Project stage: greenfield`.
2. **Day 0: accounts.** Skill `startup-launch-setup`, phase A and steps 1 to 4.
3. **Day 1: scaffold in one lane.** One strong lane builds auth, DB, row-level security, design tokens, the 5 flows as stubs, and the Dokploy deploy. Shipnovo did this in one day (2026-06-18).
4. **Days 2 to 7: one lane per flow.** Each lane owns one flow end to end. At most 10 to 20 lanes.
5. **Week 2: launch setup.** Skill `startup-launch-setup`, steps 5 to 13. Billing before the first paying user.
6. **First real user: switch to `live`.** From then on keep compatibility, migrations and backfills (profile AGENTS.md §14).

## 2. Lanes: routing, size and briefs

- Route by task class, not by subject:
  - Flash `task`/`qa-verifier`: builds, test runs, captures, syncs, receipts, issue text.
  - Sonnet or Codex Sol: implementation and review.
  - Opus: UI and visual judgment only.
  - Astra: never for normal slices. It cost $648 per merge against $11 for Sonnet (#749 §5.1).
- Cap at 40 running lanes. Merges per hour stayed flat above 30 to 50 lanes. Add a lane only while merges per lane-hour stay above 0.12 (#749 §10).
- Read `veyyon usage --json` before each wave. Put no new lane on a pool under 10%.
- Every brief has `# Target`, `# Change`, `# Acceptance`, the repo stage, the base SHA and the files the lane owns. Two lanes never own the same file. Example: the wave-2 design contract gave ShellLane, OrdersLane, ShippingLane, InsightsLane and CommerceLane disjoint folder lists.
- A lane messages Main only with a blocker, a question or its final result. Progress reports wake Main for nothing: 849 IRC wake-ups in 3 days, 41% of them no-op (#747 §9).
- A lane runs short commands in the foreground with a timeout. It uses `launch` only for a server that runs until stopped. Until the harness routes job results to the owner, every lane job exit wakes Main (#747 §9.4).

## 3. The feature loop

Every feature and every bug fix goes through these steps, in this order.

1. **Issue.** Scope, acceptance criteria with unticked boxes, owner, stage. One topic per issue. One native sub-issue per sub-topic.
2. **Red test.** Write the test for the behaviour. Run it on the base commit and see it fail with a named assertion. A nonzero exit code alone proves nothing.
3. **Smallest fix.** Make the red test pass. Update every caller in the same change.
4. **Targeted tests on a lane database** (section 4). Run only the tests for the touched folders, through `build_slot.py run` with a declared class.
5. **Review only when needed.** Required for money, auth, concurrency, migrations, or more than 250 changed lines. Never re-review after a sync merge.
6. **Merge through the merge train** (section 5).
7. **Live check.** Re-run the original failing scenario on the live app. Post the evidence on the issue.
8. **Close** only when every acceptance box is ticked against linked evidence. Never close in the run that created the issue.

Example: checkout on plan selection [Wladefant/shipnovo#1188](https://github.com/Wladefant/shipnovo/issues/1188). Lane BillingRedTests2 pushed the red tests (`3e8b5006`). BillingFixGreen made them pass: unit 21/21, DB 38/38 (`1fc22929`). A separate review lane then checked the diff before the merge train took it.

## 4. Tests on lane databases

- Each lane creates its own Postgres database on the shared lane server (127.0.0.1:5433) with the app role (`shipnovo_app`), never the admin role. The database name carries the lane name.
- The lane prints the target before any DROP. It drops only the database it created, and it drops it when done.
- Compare failures with a control run on `main`. Example: [Wladefant/shipnovo#1053](https://github.com/Wladefant/shipnovo/pull/1053) had 1,498 passes and 11 failures. The same 11 failed on `main`, so the PR caused none.
- Run tsc, vitest, eslint and prettier through `build_slot.py run --class <light|medium|heavy> --mem-gib <n>`. Small runs over 1 to 5 files: `--class medium --mem-gib 1.0`. A full `tsc --noEmit` on Shipnovo needs about 2.5 GiB of heap: declare `--mem-gib 3`.
- No local `next build`, `next start` or `next dev` for QA. Dokploy builds the app (section 5). A worktree that must serve a build uses `next build --webpack` and `next start -p <own port>` through the slot, never port 3000.

## 5. Merge train and the fast-merge gate

Operator rule from 2026-10-10 ("just merge, merge, merge" and "just check in Dokploy"):

1. One merge lane (MergeTrain) merges PRs one at a time. Worker lanes never merge. They send "ready to merge <PR> head <sha>".
2. Merge with a merge commit (`--no-ff`) and `--match-head-commit <sha>`.
3. One PR per deploy, so a failure has one cause. Migrations go in number order.
4. Watch the Dokploy build and deploy.
5. Pass when the live `/api/version` shows the merge SHA and `/api/health` gives 200. For a migration, add a read-only database check.
6. **Fail: revert at once**, then reland. Example: [Wladefant/shipnovo#1186](https://github.com/Wladefant/shipnovo/pull/1186) broke the Dokploy build with a type error in a test mock ("Property 'ok' is missing"). [Wladefant/shipnovo#1207](https://github.com/Wladefant/shipnovo/pull/1207) reverted it within the cycle, and lane Reland1186 fixed the mock and re-landed it.
7. No local `tsc` or local build before the merge. Dokploy is the build check.

## 6. QA

- QA does what a user does. Skill `flow-qa` at 390x844, 390x420 (keyboard open) and 1440x900, light and dark, on the live app or a Dokploy preview.
- Hunt the patterns the operator found himself on his phone:
  - overlays without a close action, and badge counts that differ from the list
  - duplicate or dead buttons, and content behind fixed bars
  - file pickers that do not open on touch
  - hidden primary actions, and missing core actions such as Edit
  - tap targets under 44 px.
- Before/after pairs come from a capture script that reads the served SHA from the server. It refuses identical images and refuses mockups. A change with no visible difference says so and posts no pair.

## 7. Anti-patterns that cost us time

| Anti-pattern | What happened | Rule |
|---|---|---|
| DNS proxy flip as a side step | 2026-10-10: a lane switched the live A records to proxied for Web Analytics. Main reverted it within a minute. | DNS-only stays. Proxying needs its own plan and the operator's OK (`startup-launch-setup` §2). |
| tsc memory confusion | `build_slot.py` capped every medium job at a 1,536 MiB heap, even with `--mem-gib 3`. Shipnovo tsc died with rc 134. Lanes read it as a code error. | Heap follows the reservation (fix on branch `feat/workflow-rules-747`, commit `d7877a5`). An rc 134 or a heap error is memory, not a type error. |
| Wake-up noise | Main took 2,255 turns in 3 days. 57% did nothing. Lane job exits and progress IRC caused 68% of them. | Section 2: no progress IRC, foreground short commands (#747 §9). |
| Waiting on local builds | Build freeze, RAM at 97 to 99%, 400 s waits for a slot. | Dokploy builds. Local runs only for targeted tests (#747 §2). |
| Fake or identical screenshots | Before and after images were byte-identical or mockups. | Section 6 capture rules. |
| Closing on unticked boxes | 12 competitor issues closed with empty acceptance boxes. | Section 3, step 8. |
| Lane deaths on quota | 477 of 2,745 Shipnovo lanes died (17%), 217 of them on 2026-10-09. | Check usage before a wave. Park the slice and reuse its worktree. |
| Tool traps | `cmd \| tail` killed the host process. A hung cell in the shared py kernel killed all lanes' state. The Windows bash tool sometimes returns no output. | No `\| tail`. Unique variable prefixes, no `reset`, a timeout on every subprocess. Use `launch` (pty false) or JS `Bun.$` with a timeout when bash returns nothing. |

## 8. Clean up

- After the last build, delete the worktree's `.next`. Remove the worktree when its PR is merged or closed. Unlink `node_modules` with `rmdir` first.
- Stop every server you started and check that its port is free.
- Drop every lane database you created.

## Capture proven practices

After the live check, follow `startup-launch-setup`, section "Capture and transfer proven practices".
Read root `practices.json` in `Wladefant/super-board` before planning the next feature.
Keep conditions and project decisions advisory. Update the matching startup skill, not a second playbook.
Pulse may later read the registry through its owner. This change does not modify Pulse.
