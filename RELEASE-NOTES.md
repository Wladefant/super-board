# Release notes

## v2.0.0 — 2026-08-02

**Version identity reconciled.** Four sources disagreed — `VERSION` said 1.7.1,
`skills/super-board/VERSION` said 1.6.0, the newest release-notes heading said
v1.7.1, and the only published tag said v1.2.0. The reconciliation, the evidence
behind it, and the rule that produced this number are written down in
[`docs/version-reconciliation.md`](./docs/version-reconciliation.md): the
content sources (`VERSION` + newest heading) decide the current release, the
skill mirror and the tag never vote, and a backward-incompatible contract change
takes the next major. 1.7.1 + backward-incompatible ⇒ 2.0.0. Releases v1.3.0
through v1.7.1 are declared explicitly untagged rather than retro-tagged.

### Backward-incompatible contracts

- **Seven-state lifecycle, not configurable.** `Backlog · Ready · Building · QA ·
  Review · Blocked · Done`. `Skipped` is refused where a lifecycle value is
  expected instead of being quietly mapped onto something else.
- **`Ready` is the only dispatchable status**, and an excluded label fails
  closed.
- **The runtime never merges.** A successful Review hands off and stops; a human
  rebase-merges; the closure normalizer writes the completion column afterwards.
  `human_approves_merge: false` is refused, `merge_method` must be `rebase`, and
  a tree-wide scanner fails the build on any of the eight merge mechanisms in an
  executable path. Squash merging is not available: it collapses the TDD
  breadcrumb trail that `git blame` and `git bisect` depend on.
- **`claude-p` is the default backend again.** The in-session dynamic-workflow
  backend is explicit opt-in.
- **Three activation modes** — `off`, `proof-only`, `active`. A board stays at
  `off` until every installation and repository gate passes, and every transition
  along `off` → `proof-only` → `active` takes its own human-reviewed
  configuration pull request; `proof-only` restricts work to the single
  allowlisted issue named by `proof_issue_url`, which is `null` in the other two
  modes. Nothing at runtime dispatches past the mode.
- **An immutable 1,000-point GraphQL reserve.** It cannot be configured downward,
  and a run that would break it halts with exit 75 rather than borrowing against
  it. The old 200-point threshold and the 5,000-point fallback are gone.
- **Exact-SHA QA with invalidation.** Evidence is bound to the commit it was
  produced on; a pull-request head that moves discards the result rather than
  letting it attest to a commit nobody tested.
- **Fail-closed branch routing.** An issue declares its route exactly once, as
  `staging` or `staging-frankfurt`; anything else refuses to create a branch.
- **The local Codex review gate.** Review runs a parallel maximum-level local
  Codex fleet and fixes every finding, at every severity.

### New

- **Continuous intake normalization.** Every issue and pull-request event in the
  trigger set, plus a bounded periodic sweep, re-normalizes the card. The
  canonical issue form now requires Context, Steps, binary Acceptance criteria,
  Test Area, Priority, Work type, Environment constraint (canonical
  `environment-constraint` taxonomy, `laptop` preserved as a mapped legacy
  alias), Branch route, and a Milestone or a concrete Blocked reason. Incomplete
  intake is never promoted to `Ready`. The intake normalizer confirms Project
  membership exactly once by immutable content node ID before any field update.
- **Evidence-gated closure.** The closure normalizer moves a card to the
  completion column only for a merged pull request, typed and linked completion
  evidence, a linked duplicate, or a not-planned decision that says what was
  decided. Anything else is reopened, moved to Blocked, and given a sanitized
  corrective comment. Pre-activation closures keep their original evidence
  untouched.
- **A pinned, verifiable installer.** `install.sh` takes an explicit release
  contract, refuses a source tree that is not at the pinned commit, and writes an
  install manifest recording the release, the source SHA, the schema version, and
  a SHA-256 for every installed file. Reinstalling at the same release is proved
  idempotent by comparing two tree snapshots, and stale removal only ever touches
  files the prior manifest owned.
- **A guarded fallback auto-add.** Installed disabled, membership decided by
  immutable content node ID, identity and quota preflight before any insertion,
  and a documented re-enable gate that neither the installer nor an
  activation-mode change can satisfy.
- **Agent Native is a read-only projection.** No Project write credential, no
  repository execution, no second completion ledger; unavailability is proved
  against synthetic non-resolving targets.
- **One sanitizer at one publication boundary.** Payloads are rendered whole,
  then redacted, then rescanned, and a failure refuses the write instead of
  publishing a partial one.

### Fixed

- `skills/super-board/VERSION` no longer drifts from the root version — the test
  suite fails if the two disagree.
- Active guidance no longer advertises `Skipped`, squash merging, runtime
  merging, a 200-point reserve, or `workflow` as the default backend; a scanner
  fails the suite if one comes back.
- The board-URL substitution marker is gone from the fallback workflow payload;
  the URL is a repository variable now, so a placeholder nobody rewrites can no
  longer become a live misconfiguration.

### Migrating an existing board

This release is backward-incompatible. Four things break on a board that was
running v1.x, in plain terms:

- **Dispatch now requires exactly `Ready`.** Nothing else is dispatchable —
  there is no resume-from-Building, no pick-up-from-Blocked, and no
  lane-specific eligibility — and only OPEN issue cards dispatch at all. Work
  parked in another column waiting to be picked back up will simply sit there.
- **Merge behaviour is human-only and rebase-only.** The runtime has no merge
  path and no substitute for one, `human_approves_merge: false` is refused, and
  `merge_method` must be `rebase`. Repositories must be pinned to
  `allow_rebase_merge: true`, `allow_squash_merge: false`,
  `allow_merge_commit: false`.
- **The status contract is the canonical seven**, not configurable, with
  `Skipped` removed rather than remapped.
- **The config schema changed** — `activation_mode` and `proof_issue_url` are
  part of the contract, `minimum_graphql_reserve` has an immutable floor,
  `exclude_labels` is enforced instead of ignored, and `github_auth` names
  environment variables rather than carrying values. A config holding anything
  credential-shaped exits 65.

In order:

1. Reinstall the payload at the pinned release; confirm the install manifest
   recorded a SHA-256 for every installed file.
2. Set `activation_mode` to `"off"` and `proof_issue_url` to `null`, then run
   `python scripts/super-board-config.py validate --config <path> --json`.
3. Set `human_approves_merge: true` and `merge_method: "rebase"`, and pin the
   three repository merge settings above on every linked repository.
4. Remove any Status option outside the canonical seven and reconcile the cards
   holding it.
5. Fix the built-in Project workflows — in particular **Item reopened →
   Backlog**, which earlier setup guidance wrongly pointed at Building — and
   read all seven targets back.
6. Add the `environment-constraint` label; leave `laptop` in place as the
   mapped alias.
7. Add a `Branch route:` declaration to every issue you intend to dispatch;
   without one the card is ineligible and no branch is created.
8. Run the reconcile sweep and the board audit, and fix what they surface.
9. Only then open the configuration pull request moving `activation_mode` from
   `off` to `proof-only`, naming the one issue you will watch.

### Not done here

Creating the `v2.0.0` tag and publishing the GitHub release are outward-facing
and sit behind their own explicit approval. The tooling and the check exist
(`authorize_release_publication`, `verify_release_tag`); neither has been run.

## v1.7.1 — 2026-06-11

### Fix: tolerate JSON-string `args` at wave launch

The Workflow tool can deliver `args` as a JSON-encoded string. The wave script
now normalizes (`JSON.parse` when given a string) before validating, instead of
dying at the args guard. Found live on the magnetgate board.

## v1.7.0 — 2026-06-11

### Model-tier flags for `super-board run`

`super-board run` now takes a model ladder: `--low` (haiku/sonnet/opus by card
complexity), default medium (sonnet/opus/session model), `--high` (opus floor,
session model above). Config key `model_tier` sets the default; the flag wins.
The classify router stays on haiku except on `--high` runs (sonnet). Haiku
never does lane work outside an explicit `--low` run.

## v1.6.0 — 2026-06-10

### Workflow backend is now the default; claude-p is explicit opt-in

`"worker_backend"` now defaults to `"workflow"` — `/super-board run <slug>` drains the board in-session via the `super-board-wave` dynamic workflow unless the config explicitly sets `"claude-p"`. The legacy dispatcher (`super-board-run.sh`) refuses to run (exit 78) for any config that doesn't opt in, so a stale habit or old script can't silently spawn headless `claude -p` workers.

### Hardened mutual exclusion, claims, and crash recovery (PR #3 review findings)

- **Reaper no longer eats the wave lock** — `reap_finished_locks()` skips non-numeric basenames in `inflight/`, so `workflow-wave.lock` survives coexistence instead of being deleted within one tick.
- **Per-tick mutex re-check** — the legacy dispatcher re-checks `workflow-wave.lock` every tick (exit 74), closing the TOCTOU window left by the startup-only check; the workflow side now locks first (atomic noclobber), then looks for a legacy run.
- **Claims are verified** — after `--add-assignee`, the orchestrator re-reads assignees and proceeds only if it's the sole assignee (adding never fails on a contested card, so the add alone is not a mutex).
- **Crash-recovery sweep** — on start, the workflow backend strips leaked bot assignees so a crashed orchestrator can't silently stop the board from draining.
- **Allowlist completeness** — added `gh issue view` / `gh pr view|diff|checks` (the classify and Reviewer prompts require them); documented that auto-merge boards are attended-only unless `gh pr merge` is consciously allowlisted.
- **Loud variant validation** — the wave planner exits 65 on an unknown `variant` instead of silently dropping the QA column.

## v1.5.0 — 2026-06-10

### Dynamic-workflow worker backend

New `worker_backend` config key selects how cards get worked: `"claude-p"` (default, unchanged — headless workers via `super-board-run.sh`) or `"workflow"` (opt-in — waves drained in-session via the `workflows/super-board-wave.js` dynamic workflow).

- **In-session waves** — `workflows/super-board-wave.js` runs a classify → build → qa → review pipeline per card. Lane lifecycles, branch/PR model, and Block templates are unchanged from `references/run.md`; only the dispatcher differs. See `skills/super-board/references/run-workflow.md`.
- **Backlog-aware wave selection** — `scripts/super-board-wave-plan.sh` picks one card per non-empty column downstream-first (Review → QA → Ready), then fills the remaining `max_workers` slots from the most backlogged column. Extra Review slots are unlocked only when `human_approves_merge: true`.
- **Review-lane mutex** — on auto-merge boards the workflow serializes Review-lane agents, so concurrent merges can't race.
- **Backend mutual exclusion** — the workflow backend writes `.claude/super-board/inflight/workflow-wave.lock`; the legacy dispatcher refuses to start while it exists (exit 74).
- **Tests** — 6-scenario suite at `tests/test-wave-plan.sh` pins the wave planner's selection logic against fixtures, no `gh` calls.

Why: replaces `nohup claude -p` dispatch ahead of the June 15 Agent SDK billing split. The legacy `claude-p` backend remains the default — nothing changes unless you opt in.

## v1.4.0 — 2026-05-27

### Pure-Python `super-board status` renderer (~50× faster)

The status snapshot now renders via `.claude/bin/super-board-status.py` instead of being assembled token-by-token by the model. Same locked 80-column kanban template; ~1.3s instead of ~1min per invocation.

Pure Python 3 stdlib + `gh` CLI. No bash, no jq. Works on macOS, Linux, and Windows.

Highlights:

- Handles both user-owned and organization-owned GitHub Projects (`repositoryOwner { ... on ProjectV2Owner }`).
- Paginates project items via cursor + endCursor, with a 2000-card ceiling and a truncation warning past that.
- Defensive input handling: slug-arg sanitization rejects `..` and other path-traversal sentinels; issue-title control-char strip prevents hostile titles from emitting escape sequences into the kanban frame.
- Lane-handoff fix: clean Build → QA → Review handoffs no longer leave phantom in-flight entries from the prior lane.
- Cross-platform CI (`.github/workflows/cross-platform.yml`): smoke matrix on ubuntu/macos/windows × py3.10/3.12, plus 22 parser fixture tests that pin the regexes against real dispatcher log lines.

Agents that invoke the `super-board` skill will now prefer the script and print its stdout verbatim. The locked template spec in `references/status.md` is retained as fallback / change-control documentation.

Contributed by @LucariusWest (#2).

## v1.3.0 — 2026-05-24

### New verb: `super-board stop`

Graceful shutdown of an in-flight run. One command, no manual `pkill` choreography, full context preserved on the board so the next `super-board run` resumes cleanly.

What it does, in order:

1. Inventories in-flight workers from `.claude/super-board/inflight/<issue-N>` lock files.
2. For each one, posts a `🛑 super-board · stopped mid-flight` comment on the issue **and** its PR, including lane, worker PID, UTC timestamp, last pushed commit (the "resume point"), and the literal resume command.
3. Releases the GitHub assignee mutex on each claimed issue + clears `loop:in-build`/`loop:in-qa`/`loop:in-review` descriptive labels.
4. SIGTERM → 1s → SIGKILL the worker PIDs.
5. Sweeps any untracked `claude -p .*super-board` orphan workers (defense against crashed-dispatcher leftovers).
6. Kills the dispatcher loop (`super-board-run.sh`).
7. Removes in-flight lock files. Leaves worktrees, branches, and PRs in place.

**Resume = run.** There is no separate `super-board resume` verb on purpose. The board is the state — cards sit in whichever column they were in when stopped, branches and PRs persist, and `super-board run <slug>` re-claims the same cards on its next tick. Each previously-in-flight card costs one extra lane cycle on resume.

What stop does NOT do (deliberate):

- Doesn't wait for workers to reach a clean stopping point — `claude -p` has no SIGTERM handler that flushes a partial commit. Any uncommitted edits in worker worktrees are discarded; the last **pushed** commit is the resume floor.
- Doesn't touch worktrees — the next worker re-checks-out the same branch faster.
- Doesn't touch branches or PRs.

### Lock file format upgrade (backwards-compatible)

The dispatcher now writes lock files as bash-assignment style:

```
PID=12345
LANE=qa
STARTED=2026-05-24T18:42:11Z
```

This lets `super-board stop` recover the lane name + dispatch time without an extra `gh` call. A new `read_lock` helper handles both v1.3.0+ and legacy single-line-PID formats, so an upgrade mid-run is safe — existing locks keep working until the dispatcher rewrites them on the next dispatch.

### Routing

`SKILL.md` now lists five verbs. `references/stop.md` is the full contract. New routing rows: `stop`, `pause`, `kill`, and `resume`/`pick up where I left off` (all route to `stop.md`, since resume is just `run` again).

## v1.2.0 — 2026-05-24

First public release.

### Worker-storm fixes (post-incident #381, originally landed in EricTechPro/BookKeepingApp 2026-05-22)

- **PID tracking + per-lane lockfile.** The dispatcher tracks `BUILD_PID`/`QA_PID`/`REVIEW_PID` and refuses to dispatch into a lane whose worker is still alive. Closes the 10–30s `claude -p` cold-start race that produced 7 racing workers on the very first run.
- **In-flight lockfiles** at `.claude/super-board/inflight/<issue-N>` containing the worker PID. `top_card_in_column` skips any issue with a live lock even before the assignee write propagates. Reaped each tick via PID liveness check.
- **Atomic assignee claim BEFORE worker spawn.** `try_claim_assignee` runs in the dispatcher and only proceeds to `nohup claude -p` if it wins the assignee write.
- **Orphan scan on startup.** Refuses to start if any `claude -p .*super-board run` worker is already alive from a prior crashed dispatcher run.

### Rate-limit fixes

- **Tick interval bumped 30s → 120s.** ProjectsV2 GraphQL query is ~103 points regardless of board size; 120s keeps usage at ~3.1k/hr, comfortably under the 5k/hr GraphQL budget.
- **Rate-limit guard** sleeps until reset when GraphQL remaining drops below 200.
- **Per-tick project-items cache** — one `gh project item-list` per tick, not per column lookup. ~7× quota cut.
- **Worker rate-limit etiquette** — sub-agent gh-call budgets, local `git blame` preference, `gh-quota-on-exit:` line required on every PR handoff comment.

### QA evidence

- **Mandatory inline screenshot embeds** on every QA exit (pass and fail) at standard viewports (1920×1080, 1024×768, 375×667). Screenshots committed to the issue branch BEFORE the GitHub comment is posted, so they render in-page.
- **`docs/super-board/runs/**/*.{png,jpg,webp,html,log,patch,diff,zip,trace}` gitignored** by default. Keep `.md` and `.json` summaries tracked for audit trail; drop the heavy artifacts. Users adopting on existing repos: `git rm --cached docs/super-board/runs/**/*.png` etc. to untrack what's already in.

### Documentation fixes

- **Card-locking semantics corrected.** The original spec said the GitHub assignee write was the lock. In practice it doesn't hold up — assigning yourself something you already have is a no-op on a solo account, and GH issues accept multiple assignees, so it never blocked a second worker. The real lock is the local `.claude/super-board/inflight/<N>` lockfile + per-lane PID tracking. Docs updated throughout.

### Other

- **Multi-attempt card-move guard.** Workers must call `sb_gh_guard_check` (or equivalent retry-with-backoff) around the column-move mutation and write a `move-mutation-result: ok|err|skipped` line in the PR handoff comment. Lets the dispatcher log retries and budget for them instead of silently re-dispatching every 10 min.
- **CI-budget bypass (💳).** If remote CI jobs `failed_to_start` due to Actions budget AND local-evidence is strong (truth gate passed, Tester clean, all threads clean), the Reviewer can squash-merge on local evidence with a `🛡 → ✅ CI-budget bypass` comment citing the failed run ID, Tester pass-count, and truth-gate score. Only for `💳` — never for `🛡` truth-fail, `🔐` missing creds, or `🧑` human-only decisions.
