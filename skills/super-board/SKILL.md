---
name: super-board
description: GitHub-Project-driven autonomous pipeline. Five verbs — onboard, lint, status, run, stop — that take a Project board from empty to drained across Build → QA → Review → Done lanes, with graceful shutdown / resume. Use when the user says "super-board", "/super-board", "drain my GitHub project", "set up the autonomous loop", "kick off the headless build/QA pipeline", or "stop super-board".
---

# super-board — autonomous GitHub Project pipeline

## The design spec is missing — read this before citing it

This skill and its seven reference files used to cite
`docs/superpowers/specs/2026-05-21-super-board-design.md` as their source of
truth. **That file was never carried across from the upstream fork.** Verify:

```bash
ls docs/superpowers/specs/2026-05-21-super-board-design.md
```

Consequences, in order of how likely they are to bite you:

- **`SKILL.md` and the files under `references/` are now the source of truth.**
  Not a summary of one — the whole of it.
- **Where two reference files disagree, there is nothing to appeal to.** A human
  decides and writes the decision into the files. Do not reconstruct the spec's
  intent from surrounding prose and present it as settled.
- **Section pointers below (§4, §6, §7, §9…) are dead.** They are kept only
  because they say which part of a lost document a behaviour came from.

Background:
[missing upstream dependencies](https://github.com/Wladefant/super-board/blob/main/docs/reference/MISSING-UPSTREAM-DEPENDENCIES.md).

## Five verbs

| Verb | Where | What it does |
|---|---|---|
| `super-board onboard` | interactive | one-time setup wizard; writes `.claude/super-board/configs/<slug>.json` |
| `super-board lint` | interactive | walks active-pipeline issues, flags vague ACs, runs pre-flight readiness |
| `super-board status` | interactive (read-only) | snapshot of active config, column counts, in-flight workers |
| `super-board run` | headless | the autonomous loop; spawned via `scripts/super-board-run.sh`. Also the resume command — state lives on the board, not in process memory. Accepts a model-tier flag: `--low` (haiku/sonnet/opus ladder), default = medium (sonnet/opus/session), `--high` (opus/session — strongest models only). |
| `super-board stop` | interactive | graceful shutdown: posts "stopped mid-flight" comments on every in-flight issue + PR, releases assignee mutexes, kills workers + dispatcher. Next `super-board run` resumes. |

If invoked with no verb, ask which. (The detail lived in spec §8, which is gone
— asking is the whole behaviour; do not infer more.)

## The lifecycle is fixed

Exactly seven statuses, in this order, on every board and in every variant:

```
Backlog · Ready · Building · QA · Review · Blocked · Done
```

It is not configurable — there is no `columns` key — and **`Skipped` is not a status**.
The contract lives in `scripts/super_board_runtime/lifecycle.py`; anything offering
`Skipped` where a lifecycle value is expected is rejected (exit 65) rather than mapped
onto something else. Dispatch requires status **exactly `Ready`**.

`Review` is where the runtime stops. It is a **human handoff**, not a step the
runtime completes: the reviewer marks the pull request ready, writes a handoff
record, and leaves the card there. `Done` is written by the closure normalizer
after a confirmed external merge — never by a dispatcher, builder, tester,
reviewer, or workflow.

## Merging — a human does it, by rebase

The runtime has **no merge path**. It may not run a merge command, call a merge
REST endpoint, invoke a merge mutation or MCP merge tool, enable auto-merge,
squash, create a merge commit, close the implementation issue as a substitute
for a merge, or move a card to `Done`.

This is a release gate, not a convention:
`super_board_runtime.review.scan_merge_prohibitions` source-scans every
executable runtime, workflow, skill, and reviewer path for all eight mechanisms,
and any active occurrence fails.

It is runnable on an **installed** tree — point it at `.claude` — because it
needs nothing that only exists in the source repository: it recognises its own
module intrinsically, and it tells a prohibition statement ("the runtime never
enables auto-merge") from an active instruction ("enable auto-merge once CI is
green") by looking for a negation in the statement's own paragraph or list. A
match inside a fenced code block is never treated as prose, so a command cannot
be excused by the paragraph above it. The repository keeps a
`merge-scan-allowlist.txt` for its own test fixtures only — never a path
heuristic, and nothing in the payload depends on it.

Required configuration: `human_approves_merge: true`, `merge_method: "rebase"`.
Required repository settings: `allow_rebase_merge: true`,
`allow_squash_merge: false`, `allow_merge_commit: false`. Squash collapses the
TDD breadcrumb trail; rebase keeps every commit on trunk with its original
message, author, and date.

## Identity — who Superboard acts as

Two modes, verified by `scripts/super-board-auth.py preflight` before any scan or
mutation:

| Mode | Credential | Notes |
|---|---|---|
| `interactive` | the signed-in session identity | no environment credential is read or required |
| `unattended` | a machine-account **classic** PAT | read only from `SUPERBOARD_GITHUB_TOKEN`, login must equal `SUPERBOARD_GITHUB_LOGIN`, scopes `repo`, `project`, `read:org` |

Everything fails closed with exit 69: missing variable, wrong login, fine-grained
token, GitHub App token, absent or unparseable OAuth scope header, missing scope,
or any repository/Project capability that cannot be confirmed.

**GitHub Apps cannot access personal Projects v2 at all**, so an app installation
token is refused outright rather than probed — no capability check can rescue it,
and there is no `super-board-bot[bot]` identity. The claim mutex is always a real
user login. Token values never appear in output, logs, or evidence; environment
variables are referenced by NAME.

## Routing

| If user says | Load |
|---|---|
| `super-board onboard ...` | `references/onboard.md` |
| `super-board lint ...` | `references/lint.md` |
| `super-board status ...` | `references/status.md` |
| `super-board run ...` (default — `worker_backend` unset or `"claude-p"`) | `references/run.md` |
| `super-board run ...` with config `worker_backend: "workflow"` (opt-in; `claude-p` is the default) | `references/run-workflow.md` (lane lifecycles still come from `references/run.md`) |
| `super-board stop ...` / "stop the run" / "pause the loop" / "kill super-board" | `references/stop.md` |
| "resume" / "pick up where I left off" / "restart after stop" | `references/stop.md` (resume = run; no separate verb) |
| Anything about Block/Skip exits | `references/block-template.md` |
| Config structure questions | `references/config-schema.json` (the executable contract is `scripts/super_board_runtime/config.py`; validate with `python scripts/super-board-config.py validate --config <path> --json`) |
| Worker gh-call discipline / rate-limit recovery | `references/rate-limit-etiquette.md` (+ `scripts/super-board-gh-guard.sh`) |

Replaces: `super-work-trader` (rename + extension). The 3-lane mechanics are inherited; the front door (onboard / lint / status / stop) is new.

## Orchestrator vs worker — the cardinal rule

super-board is an **autonomous trader**. The interactive Claude session that invokes any of the five verbs is an **orchestrator**, not a worker. The orchestrator:

- Validates preconditions, then dispatches per the config's `worker_backend`: `"claude-p"` (default) → `nohup ./scripts/super-board-run.sh`, report PID + log path, exit; `"workflow"` → stay in-session and run the wave loop in `references/run-workflow.md` (launch workflow, reconcile, repeat). In both backends the orchestrator never does product work itself.
- Delegates all build / QA / review work to workers — headless `claude -p` (claude-p backend) or workflow lane agents (workflow backend).
- Performs card add/status moves itself; never delegates a card move or status check to a worker. Prefer GitHub MCP Projects v2 tools when loaded; otherwise use targeted top-level `gh api graphql` mutations (`addProjectV2ItemById`, `updateProjectV2ItemFieldValue`) and trust the returned item ID.
- Must NOT do product work itself, must NOT patch the dispatcher mid-run, must NOT wait for workers, must NOT hold context for multi-card progress.

If anything goes wrong during a run, the orchestrator captures the symptom and reports back — it does not silently expand the task into a fix. See `references/run.md` "Orchestrator delegation contract" for the full rule.
