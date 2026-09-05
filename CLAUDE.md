# super-board — agent-facing notes

This repo tracks five skills under `skills/`:

- `super-board` — orchestrator (you, when invoked via `/super-board run`)
- `super-build` — headless builder worker
- `super-qa` — headless QA worker
- `super-review` — headless reviewer worker
- `claudex-optimized` — user-level, process-local Claude Code-to-Codex launcher policy and diagnostics; intentionally excluded from `install.sh`

## The cardinal rule

**`super-board` is an autonomous trader. The interactive Claude session that invokes `/super-board run` is an *orchestrator*, NOT a worker.** Its only jobs are:

1. Verify preconditions (clean git, no orphan workers, GraphQL quota, etc.).
2. Dispatch per the config's `worker_backend`:
   - `"workflow"` (default) — stay in-session and run the wave loop in `skills/super-board/references/run-workflow.md`: plan a wave, claim assignees, launch the `super-board-wave` dynamic workflow, reconcile, repeat. Lane agents inside the workflow do all product work.
   - `"claude-p"` (legacy, explicit opt-in only) — spawn the headless runner `nohup scripts/super-board-run.sh <slug> &`, report PID + log path, exit. The runner refuses to start (exit 78) unless the config sets this value.
3. Report back to the user (dispatch confirmation, or one status line per wave).

For the portable core, use the native background protocol documented in
`workflows/portable/WORKER_EXECUTION.md`: scripts prepare and validate a task,
the host launches a background agent, and scripts reconcile its completion.
Keep routing, gates, durable state, and notification deduplication in scripts;
skills and host instructions should be thin entrypoints. External agent CLIs
remain explicit standalone options, not requirements for native host execution.

The orchestrator MUST NOT:

- Build, test, review, or fix issues itself during a dispatched run. Product work belongs to the configured background workers; `claude -p` is only the explicit legacy option.
- Patch the dispatcher script or skill files mid-run, even if it sees a problem. Capture the symptom and tell the user; wait for explicit approval.
- Block the interactive session with inline worker execution. Launch background workers and reconcile their completion events while remaining available to the operator.
- Hold context for multi-card progress. State lives on the GitHub Project board + the inflight lockfiles, not in the orchestrator's session.

If a problem surfaces during the run, the orchestrator's reply is: "I saw X. Want me to dig in or stop the runner?" — not "I went ahead and fixed it."

## Worker rules

Workers (`super-build`, `super-qa`, `super-review`) share the dispatcher's `gh` token bucket. They MUST:

- Source `scripts/super-board-gh-guard.sh` at worker start.
- Call `sb_gh_guard_check <estimated-cost>` before any burst of `gh` calls. The argument is the estimated cost of the burst in GraphQL points, not a threshold; a ProjectsV2 item scan is ~103.
- Hold the immutable reserve of 1,000 points. Reaching it — or failing to read the quota at all — halts the worker with exit 75. Never sleep through a reset, never retry-spin.
- Take one quota reading per cycle and spend against it, rather than re-reading before every call.
- Prefer local `git blame` / `git log` over `gh api graphql` for any sub-agent that doesn't need fresh state, and prefer GitHub's built-in Project workflows (server-side, zero API cost) over API item-adds.
- Cap adversarial sub-agents at 50 gh calls each. If a sub-agent runs out, it returns `confidence: insufficient_data` rather than burning the shared quota.
- Append `gh-quota-on-exit: graphql=<remaining> floor=<effective-floor> reset=<time>` to the PR handoff comment. Those four fields are the only quota fields that may be logged — never a token, header, cookie, or raw payload.

See `skills/super-board/references/rate-limit-etiquette.md` for the full discipline.

## Installation contract

This repo is consumed by dropping its `.claude/`-shaped tree into a target project. The release zip is laid out so:

```
.claude/
├── skills/super-board/...
├── skills/super-build/...
├── skills/super-qa/...
├── skills/super-review/...
├── workflows/super-board-wave.js
└── bin/super-board-run.sh
    bin/super-board-gh-guard.sh
    bin/super-board-wave-plan.sh
```

The orchestrator skill expects `scripts/super-board-run.sh` to exist on the project's path. The release zip places it at `.claude/bin/`; users who prefer can symlink to `scripts/`.

`claudex-optimized` has a different installation contract: its canonical source stays in this clone and `setup.ps1` may create only the exact user-level directory junction plus its marker-delimited `claude-codex` profile block. It must not edit Claude global settings, CLIProxyAPI config/auth, credentials, or unrelated profile text, and it never runs git publication commands.
