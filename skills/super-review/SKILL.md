---
name: super-review
description: Super Review canonical workflow for PR/code/architecture readiness across EricTechOS apps. Use when the user says "Super Review", "review this branch", "review loop", "make sure this is merge-ready", "PR review", or asks for code, architecture, security, QA evidence, or release-readiness judgment. Produces findings and routes fixes to Super Build or Super QA (design work is marked unassigned — there is no Super UX skill) instead of silently pushing changes unless explicitly authorized.
---

# Super Review — PR/code readiness reviewer

**Super Review** is the EricTechOS reviewer/logger workflow. It checks whether a branch or PR is safe to merge, records actionable findings, and routes fixes to the right Super workflow.

Super Review should be conservative with claims: only say **merge-ready** when review evidence is clean and verification has passed. If evidence is missing, say what is unverified.

## When to use

Use this skill for:

- PR or branch review before merge.
- Code, architecture, security, data-model, or migration judgment.
- Release-readiness checks after Super Build or Super QA.
- A final pass that needs risks, blockers, and human gates summarized.

## Preconditions — who you are allowed to route to

Check which downstream owners actually exist before you promise one:

```bash
ls skills/ | grep -E '^super-(build|qa|review|ux|orchestrator)$'
```

**`super-ux` and `super-orchestrator` do not exist** and never shipped with this
fork (see
[missing upstream dependencies](https://github.com/Wladefant/super-board/blob/main/docs/reference/MISSING-UPSTREAM-DEPENDENCIES.md)).
This does not halt a review — reviewing is useful regardless of who fixes it —
but **naming a nonexistent owner is worse than naming none**, because a
blocker routed to `Super UX` reads as assigned and is in fact abandoned.

Do **not** use this as the primary implementation workflow. Route fixes to:

- **Super Build** for feature/task implementation from GitHub Project `Ready` issues.
- **Super QA** for functional bugs, broken behavior, failing Playwright paths, or missing QA coverage.
- **`unassigned — needs a human`** for visual fidelity, layout, screenshots, wireframes, or design-system drift. This was Super UX's lane; there is no automated owner for it. Say so explicitly in the report rather than leaving the finding unrouted.

There is also no orchestrator to hand your report to. Whoever invoked
`/super-review` is the router — address the report to them.

## Inputs

Accept any of these inputs:

- current branch or local diff;
- GitHub PR number or URL;
- commit range;
- user-provided file list;
- QA report or screenshots (the Super Orchestrator manifest this also listed is not a real input — no such skill exists to produce one);
- release goal / done definition.

If the input is ambiguous, default to reviewing the current branch against its upstream/base branch. Ask only when the base branch, PR, or target scope materially changes the result.

## Review flow

1. **Establish scope**
   - Identify branch, base branch, PR, changed files, and user goal.
   - Check working tree status before reviewing.
   - If there are unrelated dirty files, stop and ask before touching them.

2. **Inspect changes**
   - Read the diff and the affected modules.
   - Check app-specific conventions from the nearest `CLAUDE.md` / `AGENTS.md`, especially:
     - `clock.now()` instead of `new Date()`;
     - services own business logic, repositories own data access;
     - job handlers own outer transactions;
     - money uses `numeric(12,2)`;
     - calendar days use `date`, not `timestamptz`;
     - structured `AppError({ error_code, context })`;
     - jsonb writes are Zod-validated.

3. **Classify findings**
   - **Blocker:** correctness, data loss, security, auth, migrations, money, customer-visible broken behavior, or failing required tests.
   - **Should fix:** maintainability, missing tests, risky edge cases, accessibility, i18n, observability, or design drift that is clearly in scope.
   - **Nit / optional:** style or cleanup that does not block merge.
   - **Human gate:** product/design/ops decision that cannot be safely guessed.

4. **Route fixes**
   - If a blocker is an implementation task, hand it to **Super Build**.
   - If a blocker is a functional regression, hand it to **Super QA**.
   - If a blocker is visual/design fidelity, mark it `unassigned — needs a human`. There is no Super UX skill to hand it to.
   - If the user explicitly authorizes Super Review to fix, make the smallest safe patch, verify it, and clearly report that review also changed code.

5. **Verify evidence**
   - Run the smallest meaningful verification for the touched area.
   - Prefer targeted tests first; run broader suites when the change crosses boundaries.
   - For upload/import flows, do not call it complete from UI success or HTTP 200 alone; verify jobs reach terminal state and destination records are saved.
   - If verification is skipped, state why and mark merge-readiness as unverified.

6. **Report**
   - Lead with the final status: `merge-ready`, `blocked`, `human-gated`, or `unverified`.
   - Include findings grouped by severity.
   - Include verification commands and results.
   - Include which Super workflow should own each fix.

## Output format

```markdown
## Super Review result: <merge-ready | blocked | human-gated | unverified>

- Scope: <branch/PR/files reviewed>
- Base: <base branch/commit if known>
- Verification: <commands + pass/fail/skipped>

### Blockers
- [ ] <finding> → route to <Super Build | Super QA | unassigned — needs a human>

### Should fix
- [ ] <finding> → route to <workflow>

### Human gates
- <decision needed>

### Merge-readiness
<clear statement of whether this can merge now, and why>
```

For Telegram summaries, keep it short and phone-friendly:

```markdown
**Super Review: blocked ⚠️**

- **Scope:** PR #123 / current branch
- **Blockers:** 2
- **Verified:** `npm test -- --run imports`
- **Next:** route functional bug to Super QA, schema decision to human gate
```

## Review Loop behavior

**This loop is manual.** It was written for a Super Orchestrator that would
drive it automatically; that skill does not exist here, so a human performs
step 2 and step 4. Say that in your report — do not report "handed to the
orchestrator" and stop.

1. Super Review inspects branch/PR and writes findings.
2. **A human** routes each actionable finding to Super Build, Super QA, or keeps it as `unassigned — needs a human`.
3. The owning workflow fixes and verifies its scope.
4. **A human** re-invokes Super Review against the updated branch.
5. Stop only when no blocking review findings remain, or unresolved items are explicitly human-gated.

Super Review should not silently push fixes during Review Loop unless the user or orchestrator explicitly grants that authority.

## Common pitfalls

- Calling a branch **fixed** or **merge-ready** before tests or evidence prove it.
- Treating UI success or HTTP 200 as enough evidence for background jobs, uploads, or imports.
- Mixing reviewer findings with broad refactors.
- Creating duplicate GitHub issues without checking whether the finding is already tracked.
- Letting Super Review become another alias for Super Build; keep review authority separate from implementation authority.

## Done condition

Super Review is done when one of these is true:

- no blocking findings remain and the branch/PR has enough verification evidence to call it merge-ready;
- all unresolved findings are explicitly human-gated;
- required evidence cannot be collected because tooling/service access is unavailable, and the output clearly marks the result as unverified.

## super-board integration

When invoked by super-board (env `SUPER_BOARD_RUN=1` or invocation contains "super-board run"):

### State protocol
- Read from issue + PR comments + PR review threads.
- Respect handed-down worktree at `.worktrees/issue-<N>-review/` and branch `issue-<N>-<slug>`.

### Two variant modes
- **Full variant:** review the diff (code + tests).
- **QA-only variant:** review the QA report quality, not the code diff. (No diff exists in QA-only-URL.)

### Lifecycle (Reviewer)
See `.claude/skills/super-board/references/run.md` → Reviewer. Summary of 8 sub-steps:

1. Worktree from current state of `issue-<N>-<slug>`.
2. **Gate 1 — thread scan.** If ANY unresolved PR thread:
   - `[builder]` open → comment, move card Review → Ready.
   - `[QA]` open → comment, move card Review → QA.
   - Both open → bounce to whichever is older.
   - Clean up worktree, exit.
3. Read PR + spot-check Tester evidence + read CLAUDE.md / AGENTS.md.
4. Review code + tests.
5. **Reviewer-side test rerun (always — closes Tester self-verification gap):**
   - Pull `issue-<N>-<slug>` into review worktree.
   - Re-run the EXACT command from Tester's PR `Local tests:` line.
   - Green → continue. Red → open new `[QA]`-prefixed thread quoting failure, move card Review → QA with `loop:rebuild-N`, exit.
6. **Adversarial mode** (per `config.truth_gate` — `off` / `non-trivial` / `always`, default `non-trivial`): see section below.
7. Decide per finding:
   - **No findings + threads clean + truth ≥ threshold + tests green** → **human-merge handoff.** Mark the pull request ready for review (`gh pr ready <PR>`), leave the card in `Review`, write the handoff record, and stop. Do **not** merge, do **not** delete the branch, do **not** close the issue, do **not** move the card to `Done`. See "The runtime never merges" below.
   - **Code-side new finding** → new `[builder]`-prefixed thread, move card Review → Ready (`loop:rebuild-N`).
   - **Test-side new finding** → new `[QA]`-prefixed thread, move card Review → QA (`loop:rebuild-N`).
   - **Blocker (schema, contract, money, auth, migration) or rebuild cap hit** → full §4 Block template, move card Review → Blocked.
8. Clean up worktree.

### The local Codex gate — one parallel fleet per code pull request

The binding review gate is a **local** Codex fleet, run from the pull request's
worktree. Claude writes the code, Codex reviews it from four angles in parallel,
Claude fixes every finding. Two models, working together.

Run it once, through `scripts/super-board-codex-review.py run --base <base>
--worktree <path> --pull-request <url>`, which issues exactly these four
commands concurrently:

```bash
codex exec review --base "$(git merge-base origin/<base> HEAD)" \
  -m gpt-5.5 -c model_reasoning_effort="high" < /dev/null > structured.txt 2>&1
codex exec -m gpt-5.5 -c model_reasoning_effort="high" -s read-only "<correctness lens>" \
  < /dev/null > correctness.txt 2>&1
codex exec -m gpt-5.5 -c model_reasoning_effort="high" -s read-only "<security lens>" \
  < /dev/null > security.txt 2>&1
codex exec -m gpt-5.5 -c model_reasoning_effort="high" -s read-only "<perf and design-consistency lens>" \
  < /dev/null > perf.txt 2>&1
```

#### ALWAYS redirect stdin. `codex exec "<prompt>"` deadlocks without it

`codex exec` with a prompt argument reads stdin when no terminal is attached —
backgrounded, in CI, or inside a subagent — and **blocks forever**. It emits
exactly one line first:

```
Reading additional input from stdin...
```

and then nothing at all: no error, no timeout, no exit. `< /dev/null` turns that
into an immediate EOF. `super_board_runtime.review` passes `stdin=DEVNULL` in
code for the same reason, and a test pins it.

**`codex exec review` is NOT affected**, because it takes no prompt argument.
That asymmetry is what makes the failure so easy to miss: the structured lens
returns a normal-looking review while the three prompted lenses sit frozen, and
a fleet reports a quarter of its coverage as if it were all of it. It happened
on this release's own review gate — three of four lenses silently did nothing
while appearing to run.

**How to detect it:** check output byte counts about 60 seconds after launch.

```bash
wc -c *.txt     # a file frozen at ~39 bytes is deadlocked, not thinking
```

A lens that is genuinely working grows. A lens holding exactly the length of
that one line has not started and never will. Kill the fleet, add the
redirection, and run it again — a partial fleet is not a gate, and it is
indistinguishable from a passing one unless somebody counts the bytes.

Non-negotiable, each for a concrete reason:

- **`codex exec review` never receives a custom prompt.** The CLI rejects the
  combination, so a prompt there does not "add context" — it loses the entire
  structured diff review. Passing one is refused with
  `codex-review-prompt-conflict`.
- **The base is the merge base, not the base branch.** `git merge-base
  origin/<base> HEAD` scopes the review to this branch's commits instead of
  every unrelated change that landed on the base since the fork point.
- **Model `gpt-5.5`, `model_reasoning_effort="high"`, on every lens.** Anything
  weaker is a cheaper review pretending to be the gate, and fails with
  `codex-model-invalid` / `codex-reasoning-effort-invalid`. Note the
  `~/.codex/config.toml` default lags the newest model — override it.
- **Every finding is resolved, including nits.** No confidence threshold, no
  "skip the low ones". A finding is resolved only when it is fixed or disproved
  with committed evidence, recorded as `--resolution <file:line>=<commit>`. An
  advisory review is not a gate.
- **One fleet per pull request.** A second automatic run costs the same usage
  and reviews the same code; it is refused with `codex-fleet-already-run`.
  Re-review only when the user explicitly asks (`--force-rerun`).
- **Documentation-only diffs are exempt** (`documentation-only-exempt`): four
  maximum-effort lenses over a Markdown change burn usage for nothing.
- **Only sanitized summaries are published.** Raw lens output is unbounded text
  produced by a model reading the whole worktree — exactly the shape of payload
  that carries a secret by accident. It is written to local disk outside the Git
  tree; the summary goes through the publication boundary.

**CodeRabbit, Copilot, Greptile, and the GitHub `@codex` connector are not
gates.** They may comment; nothing waits on them. The connector in particular
has its own easily-exhausted review rate limit, and treating it as the gate
produces a false "usage limit" stop while the task budget is untouched.

### The runtime never merges — the review ends at the handoff

A clean review produces a **record**, not a merge. Concretely, Reviewer's best
possible outcome is:

- the pull request is marked ready for review;
- the card stays in `Review`;
- a handoff record is written carrying the issue URL, the pull request URL, the
  tested SHA, `merge_ready`, and `merge_method: "rebase"`;
- **a human rebase-merges.**

Reviewer may not, on any path:

- run a merge command, call a merge REST endpoint, or invoke a merge mutation or
  MCP merge tool;
- enable auto-merge;
- squash, or create a merge commit;
- close the implementation issue as a substitute for a merge;
- move the card to `Done`.

`Done` is written by the closure normalizer, and only after a confirmed external
merge. `merge_ready: true` means "a human may now merge this", never "this was
merged".

This is enforced, not merely stated:
`super_board_runtime.review.scan_merge_prohibitions` source-scans every
executable runtime, workflow, skill, and reviewer path — including this file —
for all eight merge mechanisms, and any active occurrence fails the release gate.
Before reporting merge-ready, `validate_merge_handoff` must also agree: the live
head must still equal the tested SHA and the `superboard/exact-sha-qa` check on
that commit must have concluded success.

### Prefix discipline
- Every new review comment Reviewer writes MUST be prefixed `[builder]`, `[QA]`, or `[review]`.
- Unprefixed **top-level** human PR comments → treat as 🧑 Block reason. Move card Review → Blocked with the full §4 template.
- Inline human review-thread replies → context only, no Block.

### `super-truth` is folded into super-review
The standalone `super-truth` skill is removed. (The removal was recorded in spec §10 item 8.9 of `docs/superpowers/specs/2026-05-21-super-board-design.md`, which is itself missing from this fork — the pointer is dead, the removal stands.) The adversarial pattern is now built in — see next section.

## Adversarial mode (folded from super-truth)

Activated per `config.truth_gate`:
- `off` — never adversarial.
- `non-trivial` (default) — diff ≥10 lines OR labels in `{security, migration, payments, auth}` trigger adversarial.
- `always` — every card.

When activated, spawn 2 sub-agents in parallel:
- **Code-grounder.** Verify cited file:line still exists and matches claims.
- **Historian.** `git blame` the changed lines; check for ADRs / prior incidents.

Each sub-agent returns a confidence score `0–100`.

**Aggregation rule: take the MINIMUM of the two scores.** Rationale: one strong skeptic should be enough to block.

Compare aggregate to `config.truth_threshold` (default `70`):
- **Below threshold** → Reviewer MUST NOT approve. Open `[review]`-prefixed PR thread quoting the lowest-confidence sub-agent finding. Write the full §4 Block template comment. Move card Review → Blocked with reason 🛡 truth-check failed (confidence X/100). The bot's "Why I cannot decide" line names the specific sub-agent finding it could not confirm.
- **Above threshold** → continue to approval decision.

### Block/Skip exits use the §4 mandatory template
Same rule as super-build/super-qa.
