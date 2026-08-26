# super-board run — full contract

> **Source of truth: this file.** It used to defer to spec
> `docs/superpowers/specs/2026-05-21-super-board-design.md` §7 "Verb 3 —
> super-board run", which is missing from this fork. See `SKILL.md` for what
> that changes.

**Where it runs:** headless. Spawned as a `nohup`-backgrounded process; the current Claude session exits immediately after dispatch. The runner script (`scripts/super-board-run.sh`) is a pure shell while-loop that dispatches one `claude -p` worker per lane and never holds Claude session state. Each `claude -p` worker is its own short-lived headless context — load this file at the start of every lane.

## Orchestrator delegation contract (NON-NEGOTIABLE)

**Super-board is an autonomous trader. The interactive Claude session that invokes `super-board run` is an orchestrator, NOT a worker.** Its only jobs are:

1. Verify preconditions (see table below).
2. Spawn the headless `nohup ./scripts/super-board-run.sh <slug> &` runner.
3. Report runner PID + log path back to the user.
4. Exit.

The orchestrator MUST NOT:

- Build, test, review, or fix issues itself. All product work is delegated to `claude -p` workers via `dispatch_lane`.
- Patch the dispatcher script or skill files unless the user explicitly asked for that change. Drift fixes belong in a follow-up issue, not a side-edit during a run dispatch.
- Wait for workers to finish. Workers run in their own contexts and write evidence back to the issue + PR. The orchestrator's output to the user is the dispatch confirmation, not the run result.
- Hold context for multi-card progress. Workers own the state machine via the GitHub Project board + assignee mutex + inflight lock files.

If a problem surfaces during the run (bug in the dispatcher, missing skill, stuck card), the orchestrator should: (a) capture the symptom, (b) tell the user what they observed, (c) wait for explicit approval before touching anything. **Do not silently expand the task into "fix the dispatcher while you're at it" — that's the orchestrator becoming a worker.**

When the user asks for diagnostics or a fix, prefer dispatching a focused `claude -p` worker (or named subagent) over doing the work in the orchestrator session, so the orchestrator's context stays small and the work stays inspectable in its own log.

## Intro shown when run starts

```
🤖 super-board run
─────────────────────────────────────────────────────────
Purpose: the autonomous loop. Drains your GitHub Project board.

Progress: ✅ onboard  →  ✅ lint  →  🤖 run (you are here)
─────────────────────────────────────────────────────────
```

## Preconditions (verified before any worker dispatches)

| Check | Action on fail |
|---|---|
| Active config exists | Halt: "Run `super-board onboard` first." |
| Project + required columns exist | Halt: "Project / columns missing. Run `super-board onboard` to repair." |
| Identity preflight passes | `super-board-auth.py preflight --config <cfg> --mode <interactive|unattended>` must exit 0 before any scan or mutation. Halt on 69: re-auth interactively with `gh auth refresh -s repo,project,read:org`, or fix the machine account's classic PAT named by `SUPERBOARD_GITHUB_TOKEN`. Fine-grained PATs and GitHub App tokens are refused — apps cannot access personal Projects v2. |
| `pre-flight.md` all items `[✓]` | Halt: "Pre-flight incomplete — fix these: [list]." |
| No issues missing ACs in active columns | Halt: "N issues need clarification. Run `super-board lint`." |
| Full variant: clean git working tree on base branch | Halt: "Working tree dirty. Stash or commit before running." |
| Stale worktree scan | Auto-clean: for each dir in `.worktrees/`, if its branch no longer exists OR no `loop:in-*` label on its issue, `git worktree remove --force` it. Log each removal in the run manifest. Halt only if a removal fails. |
| Production-merge guard | If `base_branch == "main"` AND `human_approves_merge == false` AND production-detection signals fire (see §5 step 8), halt with **exit 76**: `🛡 Refusing to start: this board targets production main with human_approves_merge=false. Every merge is performed by a human, by rebase. Set human_approves_merge: true, or point base_branch at a staging branch.` **Exit 76, not 75** — 75 is the immutable GraphQL reserve, and two halts sharing one code makes the halt unreadable to every caller downstream. |
| Orphan-worker scan (added 2026-05-22 after #381 worker storm) | `pgrep -f 'claude -p .*super-board run'` must return zero. If any super-board worker is already alive from a prior crashed run, halt with: `🛑 ${N} super-board workers already running. Stop them first: pkill -f 'claude -p .*super-board run'`. The dispatcher must never run while orphan workers exist — they will collide on assignee claims and produce duplicate PRs. |
| GraphQL reserve guard | Before each tick, read the quota once per cycle and require `remaining - estimated_cost >= effective_floor`, where the floor is the immutable 1000-point reserve (a config may raise it, never lower it). Reaching the reserve — or being unable to read the quota at all — stops the runner cleanly with exit 75. No sleep-through-reset, no retry loop, no fabricated fallback capacity. |

## Lane mapping by variant

Full variant — 3 lanes:

```
Builder:   Ready → Building → QA
           worker   = claude -p with super-build skill
           worktree = .worktrees/issue-<N>-build/
           branch   = issue-<N>-<slug>  (created here, persists across lanes)

Tester:    QA → Review
           worker   = claude -p with super-qa skill (issue-scoped mode)
           worktree = .worktrees/issue-<N>-qa/
           branch   = same issue-<N>-<slug>  (checked out, tests appended)

Reviewer:  Review → Done
           worker   = claude -p with super-review skill
           worktree = .worktrees/issue-<N>-review/
           branch   = same issue-<N>-<slug>  (a human rebase-merges on approval)
```

QA-only variant — 2 lanes:

```
Tester:    Ready → QA → Review
           worker   = claude -p with super-qa skill
                       (URL-target mode if target.type=url)
           worktree = .worktrees/issue-<N>-qa/  (or none if URL-only)
           branch   = issue-<N>-<slug>          (or none if URL-only)

Reviewer:  Review → Done
           worker   = claude -p with super-review skill
                       (QA-only mode: reviews QA report quality, not code diff)
           worktree = .worktrees/issue-<N>-review/  (or none if URL-only)
           branch   = same issue-<N>-<slug>          (or none if URL-only)
```

## Dispatch allocation model — one worker per lane, not per backlog

`super-board run` allocates headless Claude capacity by **lane**, not by how many cards are stacked in one column.

- **Full variant max concurrency:** 3 workers total — at most one Builder, one Tester, and one Reviewer at the same time.
- **QA-only variant max concurrency:** 2 workers total — at most one Tester and one Reviewer at the same time.
- **Never dispatch multiple workers from the same column just because that column has a backlog.** If all cards are in `Ready`, Full dispatches exactly one Builder until that Builder exits; QA-only dispatches exactly one Tester until that Tester exits.
- **Mixed-column example:** if `Ready`, `QA`, and `Review` each contain cards and all lanes are idle, Full dispatches three workers: one Builder from `Ready`, one Tester from `QA`, and one Reviewer from `Review`.
- **Downstream-first priority:** when multiple lanes are available, dispatch Review before QA before Ready/Build, so finished work gets closed before new build work starts.
- **Lane idle gate:** a lane is eligible only when its prior worker process has exited. The runner must track lane PIDs or an equivalent lane lease; GitHub issue assignees are per-card mutexes, not lane-capacity controls.

This means a board with 30 cards in `Ready` and zero elsewhere does **not** start 3 Builder sessions. It starts one Builder. As that Builder moves its card to `QA` and exits, the next tick can start one new Builder for the next `Ready` card and one Tester for the first `QA` card, because those are different lanes.

## Branch + PR model — one branch, one PR per issue

Format: **`issue-<N>-<kebab-title>`** (no lane prefix).

Example: `issue-42-add-chat-streaming`.

There is **exactly one branch per issue** and **exactly one PR per issue**. All three lanes work on the same branch in their own worktrees:

- Builder creates the branch off `config.base_branch`, writes code, commits + pushes, opens a **draft PR**.
- Tester checks out the same branch in a fresh worktree, adds tests, commits to the same branch, pushes.
- Reviewer **never merges**. On approval it marks the PR ready for review, moves the card to Review, and stops; a human rebase-merges. The runtime has no merge path, no auto-merge, and no substitute for a merge (it may not close the implementation issue in place of one).

## PR description template

Builder writes this when opening the PR. Each lane updates the relevant section on exit.

```markdown
## Issue
Part of https://github.com/<owner>/<repo>/issues/<N> — <title>

Use a closing keyword (`Closes` / `Fixes` / `Resolves #<N>`) only when the
diff satisfies every acceptance-criterion checkbox on that issue. A partial
PR that says `Closes #N` still closes the issue on merge. See
`references/github-ops.md`.

## Acceptance Criteria
- [ ] AC1: <text>
- [ ] AC2: <text>
- ...

## Iteration history

| #  | Lane     | Result      | When    | Detail                          |
|----|----------|-------------|---------|----------------------------------|
| 1  | Builder  | ✅ done     | <time>  | <commit-sha>                     |
| 2  | Tester   | ❌/✅ vN    | <time>  | runs/issue-<N>-qa-vN/            |
| 3  | Reviewer | ✅ ready    | <time>  | awaiting human rebase-merge      |

## Evidence folders
- `docs/super-board/runs/issue-<N>-qa-v1/`
- ...

## Status
<one-line current state, updated by each lane on exit>
```

Tester ticks AC checkboxes on pass.

## PR review-comment threads — prefix + resolution protocol

Reviewer always uses **line-level review comments** (resolvable threads). Every thread MUST be prefixed with `[builder]` or `[QA]` to indicate which lane owns the fix.

Examples:

```
src/api/stream.ts:54   [builder] uses `new Date()` — replace with `clock.now()`.
e2e/streaming/ttfb.spec.ts:18   [QA] spec asserts status only — add TTFB assertion.
```

**Resolution rules (each lane only scans the current branch's PR):**

| Lane exiting | Must resolve | Refusal action |
|---|---|---|
| Builder (Building → QA) | All `[builder]` threads on this PR | Stay in Building, fix, then exit |
| Tester (QA → Review) | All `[QA]` threads on this PR | Stay in QA, fix, then exit |
| Reviewer (approving merge) | ALL threads on this PR | Bounce: `[builder]` open → Ready; `[QA]` open → QA |

Threads are resolved via `gh api graphql` `resolveReviewThread` mutation when the fix is committed.

## Lane lifecycles (Full variant, per card)

### Builder (first pass)

1. Create worktree `.worktrees/issue-<N>-build/` off `config.base_branch`.
2. Create branch `issue-<N>-<slug>` from `config.base_branch`.
3. Read issue body + ALL comments + PROJECT.md.
4. Implement smallest safe change covering ACs.
5. Commit + push (always).
6. Open draft PR linked to the issue with the PR description template.
7. Post a 🔨 PR timeline comment with files/commits/summary.
8. Post a short status comment on the issue with the PR URL.
9. Clean up worktree. Keep branch + PR open.
10. Move card Building → QA.

### Builder (rebuild)

1. Worktree as above.
2. Read PR review threads on this branch's PR; filter `[builder]` prefix.
3. For each unresolved `[builder]` thread: read file:line + suggested fix, apply, resolve thread via graphql mutation.
4. Address any new failure feedback from Tester's latest `❌` comment.
5. Commit + push to same branch.
6. Verify ALL `[builder]` threads are resolved. If not, return to step 3.
7. Post 🔨 PR + issue comments. Move Building → QA. Clean up worktree.

### Tester (first pass — repo-backed)

1. Pull latest of base; checkout `issue-<N>-<slug>` into worktree `.worktrees/issue-<N>-qa/`.
2. URL-only variant: skip worktree + pull, run a health-check on `target.url`. If unhealthy → Block.
3. Read issue + PR + Builder's handoff comment.
4. Build issue-scoped test plan: one observable test per AC.
5. Run the tests. Capture evidence to `docs/super-board/runs/issue-<N>-qa-v<N>/`. For UI/visual ACs, capture screenshots at the standard viewports (1920×1080 desktop, 1024×768 tablet, 375×667 mobile). Commit the screenshots to the issue branch BEFORE writing the comment (the markdown image URLs depend on the files being present on the branch).
6. **Pass** → commit test files + screenshots to same branch + push → 🔍 PR comment with results + evidence path **+ inline screenshot embeds** (see "Screenshot embed format" below) → 🔍 issue comment with the SAME inline screenshot embeds → move card QA → Review. Clean up worktree.
7. **Fail** → 🔍 PR comment with per-AC expected/actual + repro file:line + evidence path + "what fixed should look like" **+ inline screenshot embeds of the broken state** → 🔍 issue comment with the same inline screenshots (showing what's wrong) → increment rebuild counter → move card QA → Ready (label `loop:rebuild-N`). Clean up worktree.

#### Screenshot embed format (mandatory on every QA exit — added 2026-05-22)

Inline screenshots in the GitHub comment using raw-URL markdown so they render directly on the issue/PR page without anyone having to clone the repo:

```markdown
### Visual evidence

| Viewport | Screenshot |
|---|---|
| Desktop 1920×1080 | ![desktop](https://github.com/<OWNER>/<REPO>/raw/<BRANCH>/docs/super-board/runs/issue-<N>-qa-v<V>/desktop.png) |
| Tablet 1024×768  | ![tablet](https://github.com/<OWNER>/<REPO>/raw/<BRANCH>/docs/super-board/runs/issue-<N>-qa-v<V>/tablet.png) |
| Mobile 375×667   | ![mobile](https://github.com/<OWNER>/<REPO>/raw/<BRANCH>/docs/super-board/runs/issue-<N>-qa-v<V>/mobile.png) |
```

Substitution rules:
- `<OWNER>/<REPO>` — read from `git remote get-url origin` (parse owner/name).
- `<BRANCH>` — the issue branch (`issue-<N>-<slug>`), NOT the merge target. The branch must already contain the screenshots when you post the comment.
- `<N>` and `<V>` — issue number + QA version (`v1`, `v2`, ...).
- File names — keep them stable and descriptive (`desktop.png`, `tablet.png`, `mobile.png`, or `before-fix-desktop.png` / `after-fix-desktop.png` for rebuild-pass cases).

For non-visual ACs (API tests, migration SQL, etc.), skip the screenshot block but keep the evidence-path line. Tests output (logs, REPORT.md) still goes in the evidence folder.

If a screenshot file is >5MB, downscale to ≤1920px wide before committing; GitHub's image rendering chokes on huge images.

### Tester (rebuild — when Reviewer bounced for `[QA]` thread fixes)

1. Worktree as above.
2. Read PR review threads; filter `[QA]` prefix.
3. For each unresolved `[QA]` thread: apply fix to test files, resolve thread.
4. Re-run full test suite for the ticket.
5. Save evidence to `runs/issue-<N>-qa-v<N+1>/`.
6. Commit + push. Verify ALL `[QA]` threads are resolved.
7. Post 🔍 PR + issue comments. Move QA → Review. Clean up worktree.

### Reviewer

1. Worktree `.worktrees/issue-<N>-review/` from current state of `issue-<N>-<slug>`.
2. **Gate 1** — scan PR threads. If ANY unresolved:
   - `[builder]` open → comment, move card Review → Ready.
   - `[QA]` open → comment, move card Review → QA.
   - Both open → bounce to whichever is older; the other gets picked up later.
   - Clean up worktree, exit.
3. Read PR (code + test files + description), spot-check Tester's evidence (one screenshot at least), read CLAUDE.md / AGENTS.md.
4. Review the code (logic, conventions). Review the tests (right thing tested? testable assertions? meaningful coverage?).
5. **Reviewer-side test rerun** (always — closes the Tester self-verification gap):
   - Pull `issue-<N>-<slug>` into the review worktree.
   - Re-run the test command Tester used (recorded in Tester's PR handoff comment as `Local tests:` line).
   - Tests green → continue to step 6.
   - Tests red → open new `[QA]`-prefixed PR thread quoting the failure output, move card Review → QA with `loop:rebuild-N`, exit. Tester wrote a broken suite; QA owns the fix.
6. **Adversarial mode** (per `truth_gate`): when triggered, spawn 2 sub-agents in parallel:
   - **Code-grounder** — verify cited file:line still exists and matches claims.
   - **Historian** — `git blame` the changed lines, check for ADRs / prior incidents.
   - **Budget cap (added 2026-05-22): each sub-agent ≤50 gh calls.** Prefer local `git blame` / `git log` over `gh api graphql`. If a sub-agent needs >50 calls to reach confidence, it returns `confidence: "insufficient_data"` and the Reviewer flags the card as 🛡 truth-check inconclusive instead of burning the shared quota. See `rate-limit-etiquette.md`.
   - Aggregate into a confidence score (0-100). Compare against `config.truth_threshold` (default 70).
   - **Below threshold** — Reviewer MUST NOT approve. Open a `[review]`-prefixed PR thread quoting the lowest-confidence sub-agent finding, write the full Block template comment (see §4 Block/Skip), move card Review → Blocked with reason tag 🛡 truth-check failed (confidence X/100). The card stays Blocked until human review; the bot's "Why I cannot decide" line names the specific sub-agent finding it could not confirm.
   - **Above threshold** — continue to step 7.
   - **No Reproducer needed** — Tester's tests were re-run in step 5.
7. Decide per finding:
   - **No findings + threads clean + truth ≥ threshold + tests green** → human-merge handoff (draft-aware):
     1. Check draft status: `gh pr view <PR> --json isDraft -q '.isDraft'`.
     2. If draft: mark ready-for-review (`gh pr ready <PR>`). If that fails (checks failing, conflicts, permissions) — do **not** leave the card in Review to be silently re-picked; move it to Blocked with a `🛡`-tagged comment (see `block-template.md`) stating the specific failure (e.g. `PR #N still draft: <reason>`).
     3. If not draft: mark ready (no-op if already ready).
     4. Leave the card in Review and stop. A human rebase-merges.
     **Invariant:** the runtime never merges, never enables auto-merge, never closes the
     implementation issue as a substitute for a merge, and never moves a card to Done. Done is
     produced by the built-in workflows and the closure normalizer *after* a real human merge.
   - **Code-side new finding** → open new `[builder]`-prefixed PR thread, comment, move card Review → Ready (label `loop:rebuild-N`).
   - **Test-side new finding** → open new `[QA]`-prefixed PR thread, comment, move card Review → QA (label `loop:rebuild-N`).
   - **CI-budget block (💳)** — if remote CI jobs `failed_to_start` due to `Actions budget` AND local evidence is strong (truth ≥ threshold, Tester suite green on rerun in step 5, all `[builder]`/`[QA]` threads clean) → mark the PR ready and hand off to the human anyway; do NOT move to Blocked. Add a `🛡 → ✅ CI-budget bypass` comment to both the PR and the issue citing: (a) the failed CI run ID, (b) the Tester pass-count, (c) the truth-gate score. Reason: CI failure-to-start ≠ test failure; with strong local evidence, parking the card wastes pipeline time. The bypass affects the handoff only — it never merges. This bypass is ONLY for `💳` — never for `🛡` truth-fail, `🔐` missing creds, or `🧑` human-only decisions.
   - **Human-gate / Blocker (schema, API contract, money, auth, migration) / rebuild cap hit (config.rebuild_cap)** → write the full Block template (see §4), move card Review → Blocked.
8. Clean up worktree.

## Commenting cadence (issue + PR, every lane)

Every lane writes BOTH on every exit:

- **Issue comment**: short status, with the PR URL + (if applicable) the evidence folder path. The final ✅ comment also carries the full iteration path (e.g. `Build → QA fail v1 → Build → QA pass v2 → Review ✅`).
- **PR timeline comment**: structured handoff for the next agent — issue ref, branch, commit, files, summary, evidence path, what-fixed-looks-like (on failure), next lane.

Sample issue comment (Builder exit):

```
🔨 super-board · Build done
   PR:  #87
   Next: QA
```

Sample PR timeline comment (Builder exit):

```
🔨 Builder — complete
Issue:        #42
Base branch:  staging
Branch:       issue-42-add-chat-streaming
Commit:       abc1234
Files:
  • src/api/stream.ts             (new)
  • src/lib/sse-client.ts         (new)
  • src/components/Chat.tsx       (edit, 12 lines)
Summary:      Added /api/stream endpoint + client-side SSE consumer.
              No schema changes. No new deps.
Local tests:  npm test --run streaming  PASS (4 tests)
Next:         Tester (QA)
```

Sample issue comment (Tester fail rebuild — with mandatory inline screenshots for any UI-touching AC):

```
🔍 super-board · QA fail · v1
   PR:  #87
   Failed: AC1 (TTFB 1240ms), AC2 (CLS 0.18)
   Evidence: docs/super-board/runs/issue-42-qa-v1/
   Next: Rebuild

### Visual evidence (broken state)

| Viewport | Screenshot |
|---|---|
| Desktop 1920×1080 | ![desktop](https://github.com/<OWNER>/<REPO>/raw/<BRANCH>/docs/super-board/runs/issue-<N>-qa-v<V>/desktop.png) |
| Mobile 375×667    | ![mobile](https://github.com/<OWNER>/<REPO>/raw/<BRANCH>/docs/super-board/runs/issue-<N>-qa-v<V>/mobile.png) |
```

Pass-state comment uses the same `### Visual evidence` block but with screenshots of the **working** UI per AC.

Sample issue comment (Reviewer approve + merge):

```
✅ super-board · Review approved · Merged
   PR:  #87
   Path: Build → QA fail v1 → Build → QA pass v2 → Review ✅
   Truth check: 95/100
   Merge: ef67890 on staging
   Status: Done
```

Block/Skip exit comments use the 🛡 / 🤷 emojis with a 1-line reason and reference the PR.

## Per-tick logic (~30s cadence)

```
1. Refresh project items + column counts via gh api
2. Re-validate preconditions (auth, pre-flight, columns)
3. Downstream-first dispatch by lane capacity:
   ├─ Review has cards + Reviewer idle → dispatch top of Review
   ├─ QA has cards + Tester idle       → dispatch top of QA
   └─ (Full only) Ready has cards + Builder idle → dispatch top of Ready

   Allocation rule: at most ONE worker per lane at a time.
   Full max concurrency = 3 total workers (Builder + Tester + Reviewer),
   but never 3 Builders from a Ready backlog. QA-only max concurrency = 2
   total workers (Tester + Reviewer), but never 2 Testers from a Ready/QA
   backlog. GitHub issue assignees prevent two workers claiming the same card;
   the runner still must track lane idleness separately to prevent multiple
   same-lane workers.
4. Wait for any lane to finish OR 30s timeout
5. Re-read project state (lane may have moved a card)
6. Loop until done conditions met
```

## Worker contract (every lane on claim)

Claim is **add-then-verify** on the GitHub Issue assignee. It is NOT a
compare-and-set: a GitHub issue accepts up to ten assignees, so
`gh issue edit --add-assignee` succeeds whether or not someone else already
claimed the card, and two dispatchers both saw exit 0. The claim is the
read-back. Labels are descriptive only.

```
1. ATTEMPT CLAIM (add, then verify):
   ├─ `gh issue edit <N> --add-assignee <notifications.bot_identity>`
   ├─ `gh issue view <N> --json assignees` → require EXACTLY
   │      [<notifications.bot_identity>]
   ├─ Any other assignee set → race lost: remove own assignee again
   │      (`gh issue edit --remove-assignee`) and skip this dispatch.
   ├─ Verification read fails → treat as lost: remove own assignee and skip.
   └─ On success → continue. Apply descriptive label
       (loop:in-build / loop:in-qa / loop:in-review) for UI clarity only.
2. SANITY CHECK: issue body has `## Acceptance Criteria` with ≥1 bullet
   ├─ Missing → write the full Block template (see §4) with reason ❓
   │           release claim (`gh issue edit --remove-assignee`)
   │           move card to Blocked, continue with next card.
   └─ Present → proceed.
3. Do the lane's work (build / QA / review).
4. Comment evidence on issue + PR (structured handoff).
5. Move card to next column (or Blocked with the full §4 template).
6. RELEASE CLAIM (`gh issue edit --remove-assignee <notifications.bot_identity>`) and remove descriptive label.
```

The claim identity in `notifications.bot_identity` is a real user login — the machine account for unattended runs, or the user's own account on solo projects. It is never a GitHub App: apps cannot access personal Projects v2, so an app identity could not move the card it just claimed. GitHub serializes assignee writes per issue, which is what makes the read-back conclusive: both racers end up seeing the same list, and only one of them sees itself alone.

### Anti-zombie addendum (added 2026-05-22 after #381 worker storm)

Worker-side assignee claim alone is **not sufficient** — `claude -p` cold-start takes 10–30s, during which the dispatcher's next tick can fire another worker for the same card. The 2026-05-21 first-run produced **7 racing workers** (3 on #381, 4 on #382) before the dispatcher died from rate limit.

The dispatcher MUST also:

1. **Lock, claim, THEN spawn — in that order** — the dispatcher creates
   `.claude/super-board/inflight/<issue-N>` atomically (`set -o noclobber`)
   before it claims the assignee and before it spawns anything, and drops the
   lock again on every refusal. Writing the lock after the spawn left two open
   windows: a concurrent dispatcher could pass its own lock check and launch a
   duplicate worker, and a crash between spawn and write left a live worker
   nothing tracked, reaped, or stopped.
2. **Then claim the assignee** — `try_claim_assignee` adds and verifies (above);
   only a verified claim proceeds to `nohup claude -p`. Closes the cold-start race.
3. **Record the PID into the lock** — the lock holds `PID=` empty until the
   worker exists, then carries it. A lock with no PID is honoured for
   `spawn_grace_seconds` (default 120) and reaped after, so a crash in the
   claim→spawn window cannot wedge the card forever.
4. **Cap one worker per lane** — track `BUILD_PID` / `QA_PID` / `REVIEW_PID`; do not dispatch to a lane whose prior PID is still alive.
5. **Reap stale locks each tick** — `reap_finished_locks` removes any lock whose PID no longer exists.
6. **Orphan-scan on startup** — refuse to start if any `claude -p .*super-board run` worker is already running from a prior crashed dispatcher.
7. **Cache `gh project item-list` per tick** — one API call per tick, not per column lookup. Cuts rate consumption ~7×.
8. **Release everything on INT/TERM** — a stop signal stops the in-flight
   workers, removes their locks, and removes their assignee claims. A signal
   handler that only cleans temp files leaves cards claimed by processes that no
   longer exist, and the planner skips those cards forever.

The three locks (assignee read-back, in-flight file, lane PID) are defense in depth: any one of them alone has a race window; together they make a duplicate dispatch effectively impossible.

## Halt gates

| Gate | Action |
|---|---|
| `config.rebuild_cap` reached on same card + same root-cause hash | Move card to Blocked with full §4 template (reason 🛡), continue run |
| No card progresses for 3 ticks AND no lane is idle | Halt, dump state |
| Auth expires mid-run | Halt, ping user with refresh instruction |
| Pre-flight check fails on re-validation | Halt with the specific missing item |
| Merge conflict that lane can't resolve safely | Move card to Blocked (reason 🛡), continue run |
| User-defined time/budget window reached | Graceful halt: finish in-flight workers, no new dispatches |
| Destructive action would be required (prod deploy, db drop, secret rotation) | Halt, never proceed; move card to Blocked with reason 🛡 |
| Block-rate alert: Blocked count > `config.block_rate_alert_pct` of initial Ready | Send breakdown notification (Telegram/channel), continue run |

### Root-cause hash (used by the rebuild-cap gate)

To detect "same root cause" without depending on a model classifier, each lane writes a deterministic hash to its failure handoff comment:

```
root-cause-hash: <sha256 first 12 hex chars>
```

The hash inputs (joined with `|`):

1. Lane (`build` / `qa` / `review`).
2. Error class — first matched: `test_assertion`, `test_runtime`, `build_fail`, `lint_fail`, `merge_conflict`, `network`, `auth`, `quota`, `other`.
3. First 3 unique `file:line` frames from the captured stack trace / test output (sorted, normalized — strip absolute paths to repo-relative).

Two consecutive failures on the same card with **identical hash** AND **same lane** count as the same root cause. Hits `rebuild_cap` → Blocked.

Different hash on the same card resets the counter — the bot recognizes that progress has been made even if the card hasn't reached Review yet.

## Done conditions

The loop exits cleanly when:
- All active-pipeline columns are empty; OR
- Only Blocked/Done cards remain; OR
- A halt gate fires.

## Run manifest

```
docs/super-board/runs/<YYYY-MM-DD>-<slug>.md
```

Records: config used, variant, columns, target, per-card history (claim → completion → next column with evidence links), halt gates, final counts, per-lane wall-clock, resume command.

## Notification cadence

- **Start** — project, variant, initial column counts, link to live status: `super-board status`.
- **Per card completion** — `✅ Tester → #41 passed all checks → Review`.
- **Per Block/Skip** — short headline + reason tag (`⛔ #19 blocked → 🔐 missing OPENAI_API_KEY`) + link to the full §4 template comment on the issue.
- **Every 10 dispatches** — brief column-count snapshot.
- **Block-rate alert** — when Blocked count exceeds `config.block_rate_alert_pct` of initial Ready: ping with breakdown ("11/30 Ready cards blocked: 🔐×6, 💳×3, ❓×2 — see project board"). One-shot per run (does not re-fire).
- **Final** — end column counts, total moved to Done, blockers (with reason-tag breakdown), total wall-clock.

## Worker rate-limit etiquette (read before any burst of gh calls)

Workers share the dispatcher's gh-auth token bucket. The dispatcher's `gh_rate_guard` does NOT protect worker traffic. Every worker MUST follow `rate-limit-etiquette.md` (in this directory):

- Source `scripts/super-board-gh-guard.sh` at worker start.
- Call `sb_gh_guard_check <estimated-cost>` before any burst of gh calls (thread reads, sub-agent spawn, exit verification). The argument is the ESTIMATED COST of the burst in GraphQL points, not a threshold. It stops the worker with exit 75 when the call would break the immutable 1,000-point reserve, or when the quota cannot be read at all.
- Take one quota reading per cycle and spend against it. Re-reading before every call makes the guard the thing that drains the bucket.
- Adversarial sub-agents are capped at 50 gh calls each — prefer `git blame` (local) over `gh api graphql`.
- Final PR handoff comment MUST include `gh-quota-on-exit: graphql=<remaining> floor=<effective-floor> reset=<time>` — capture it with `sb_gh_guard_summary 2>&1`, which prints it on stderr and always returns 0 so a worker's last act cannot change its exit status. Those four fields are the only ones that may be logged; a quota that cannot be read prints the `unavailable` marker rather than a number.
- On 403 / secondary-rate-limit: halt exactly as for exit 75 — write the halt comment, release the claim, exit. The runtime never waits out a reset and never retry-spins.

## Worker self-check (mandatory on exit, every lane)

Before releasing the claim assignee and exiting, every worker MUST verify:

- [ ] Issue comment AND PR comment both written (per "Commenting cadence" above).
- [ ] Card column move's mutation returned success. **Do NOT re-query `gh project item-list` for verification** — trust the mutation exit code (the 500-item GraphQL refetch was the per-worker quota tax; see `rate-limit-etiquette.md` §5). If the mutation returned non-zero, call `sb_gh_guard_check` with the retry's estimated cost, retry the move ONCE, and if it still fails, leave the assignee in place and write a halt comment.
- [ ] Claim assignee released (`gh issue edit --remove-assignee <bot_identity>`) and the descriptive `loop:in-*` label removed.
- [ ] On failure handoff: `root-cause-hash:` line is present in the PR handoff comment (per "Root-cause hash" above).
- [ ] On Block exit: the full template from `block-template.md` is populated on BOTH the issue and the PR (if a PR exists); the reason emoji is one of the nine in the vocabulary table (🔐 💳 🔑 ❓ 🛡 🧑 🤷 📦 🎨).
- [ ] `gh-quota-on-exit:` line appended to PR handoff comment.

A worker that cannot satisfy this checklist must NOT release its claim. It either fixes the gap and re-checks, or — if the gap itself is structural (e.g., GitHub API refusing to move the card) — it leaves the assignee in place and writes a halt comment so the runner's "no progress for 3 ticks" gate can fire deterministically.

### Known issue — multi-attempt card moves (added 2026-05-22)

During the 2026-05-21 production run, several workers exited cleanly (process terminated, in-flight lock reaped) but **had not moved their card to the next column**. The dispatcher correctly re-dispatched (lane idle + card still in source column = re-fire) — but each retry burned a full lane cycle (~10 min) before the next worker tried again. The #382 Reviewer took **5 attempts × ~10 min = ~50 min** to move a card that the first attempt should have moved.

Suspected causes:
- Worker hit a transient gh API error on the column-move mutation, didn't retry the mutation, exited "cleanly" thinking it had moved the card.
- Worker's super-build/super-qa/super-review skill silently caught the move error and proceeded to assignee-release without surfacing the failure.

**Mitigation for now:** the dispatcher's `reap_finished_locks` + assignee sweep + re-dispatch keep the pipeline rolling, so this is a wall-clock issue, not a correctness issue. A worker that doesn't move the card will eventually have another worker do it.

**Real fix (TODO):** require workers to call `sb_gh_guard_check` (or equivalent retry-with-backoff) around the column-move mutation, and to write a `move-mutation-result: ok|err|skipped` line in the PR handoff comment so the dispatcher can log retries and budget for them. See follow-up issue (file via `/super-board run`-time review).

### Known issue — lane-zombie workers (added 2026-05-24, auto-remediated)

**Symptom seen on fitbox-v4 first run:** a Tester worker successfully moved #81 QA → Review and the Reviewer subsequently merged it to Done — but the original Tester's `claude -p` process kept running for 30+ minutes afterward. The dispatcher's `lane_idle()` check uses `kill -0` on the lane PID and saw the zombie as "still busy," so the QA lane never re-dispatched. By the time it was noticed, the QA column had grown 1 → 2 → 3 cards with no Tester picking them up.

Different from the multi-attempt-move issue above: there the worker exited without moving the card; here the worker moved the card but didn't exit.

**Auto-remediation (now implemented in `super-board-run.sh` as anti-zombie control #7):**

Every tick — both cheap and expensive — `sweep_lane_zombies` runs `check_lane_zombie` for each lane. For each lane whose PID is alive AND whose claimed issue's current column is NOT in the lane's expected source set:

| Lane    | Expected source columns | Anything else means → |
|---------|-------------------------|-----------------------|
| build   | Ready, Building         | zombie                |
| qa      | QA                      | zombie                |
| review  | Review                  | zombie                |

On zombie detection: `SIGTERM` + 1s + `SIGKILL` the PID, remove the inflight lock, idempotently sweep the assignee, clear the lane PID/issue vars, log `💀 zombie <lane> worker on #<N> (pid=<P>) — card moved to '<col>'; killing`. Uses the cached project items only — zero extra API calls per tick.

Trade-off: if someone (human or external tool) manually moves a card out of its source column while a worker is legitimately mid-work, the watchdog will kill that worker. Acceptable — manual board edits during an active run are an anti-pattern, and the worker would fail on its own column-move anyway.
