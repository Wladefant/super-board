# Missing upstream dependencies in the pipeline skills

**Status: open, deliberately unfixed.** Verified 2026-07-28 against
`Wladefant/super-board` on `main`.

The four pipeline skills — `super-board`, `super-build`, `super-qa`,
`super-review` — were inherited from the `EricTechPro/super-board` fork. They
reference scripts, a design spec, slash commands, and skill owners that were
never carried across. The prose reads as if all of it ships. It does not.

This is fork debt, not damage from recent work. It is recorded here rather
than repaired because **repairing it would mean inventing it.** The spec that
defined these interfaces is itself one of the missing files, so any
replacement dispatcher would be a guess wearing the costume of an official
contract — worse than the current state, because it would remove the signal
that something is wrong.

## What is missing

### 1. The `/super-qa` dispatcher scripts

| Referenced as | Referenced by |
|---|---|
| `scripts/super-qa-dispatch.sh` | `skills/super-qa/SKILL.md`, `skills/super-qa/references/iteration-preamble.md` |
| `scripts/super-qa-file-bug.sh` | same, ~12 call sites total |

Verify:

```bash
ls scripts/super-qa-dispatch.sh scripts/super-qa-file-bug.sh
```

**Consequence: `/super-qa` cannot run.** Its algorithm is a loop around
`super-qa-dispatch.sh`; every red finding is filed by `super-qa-file-bug.sh`.
Neither exists. The sibling `skills/super-build/scripts/super-build-dispatch.sh`
*does* exist, which is why the absence is easy to miss.

### 2. The design spec cited as source of truth

`docs/superpowers/specs/2026-05-21-super-board-design.md` is cited by eight
files as the authority behind their behaviour. The whole `docs/superpowers/`
tree is absent.

```bash
ls docs/superpowers/specs/2026-05-21-super-board-design.md
```

**Consequence:** there is no document to resolve a disagreement about intended
behaviour. Each reference file is now its own source of truth, and where two
disagree, a human decides. The citations have been rewritten to say so.

### 3. The gstack advisor slash commands

`/plan-ceo-review`, `/plan-eng-review`, `/cso`, `/plan-design-review` — required
at every decision point by `skills/super-build/references/worker-preamble.md`,
originally with no fallback. Also `gstack:shape`, `gstack:clarify`, and the
`gstack` CLI itself.

```bash
ls ~/.claude/commands/ 2>/dev/null   # directory does not exist
command -v gstack                    # not on PATH
```

**Consequence, before the fix:** an unattended worker hitting any judgment call
would invoke a command that does not resolve, mid-run, after partial work.
`worker-preamble.md` now degrades the way its sibling
`references/gstack-voting.md` always did: inline role-play, then escalate.

### 4. Skill owners that never existed

`super-ux` and `super-orchestrator` were routed to as live fix owners and
gating authorities. Neither has ever existed in this repo or in any installed
skill directory.

```bash
ls skills/ | grep -E 'super-ux|super-orchestrator'
```

**Consequence:** bugs filed with `suggested owner: super-ux` were addressed to
nobody, and read as assigned. Those routes are now `unassigned — needs a human`.

### 5. `lint` Phase 4 clarifier routing

`skills/super-board/references/lint.md` routes underspecified issues to
`qa-test-planner`, `gstack:shape`, `gstack:clarify`, `investigate`, and
`gsd-discuss-phase`. None are installed.

**Consequence:** Phase 4 now instructs the linting session to write the
clarification itself and label the issue, rather than dispatch into a void.

### 6. Referenced-but-absent staging doc

`docs/super-orchestrator/STAGING-ENV.md` is cited twice by `super-qa` as the
setup playbook for a staging environment. It does not exist. The `no-staging-env`
halt it belongs to is still correct and still fires — only the pointer is dead.

## What was done instead of restoring

1. Each affected skill opens with a **preconditions block**: what must exist,
   the exact shell check, and the halt text when the check fails.
2. A missing dependency halts **before** any work — no partial bug filings, no
   dirty tree, no half-drained column.
3. Routing to a nonexistent owner was replaced with a real owner or an explicit
   `unassigned — needs a human` state.
4. `worker-preamble.md` was brought in line with `gstack-voting.md`, which had
   the graceful-degradation pattern all along. That asymmetry was itself a bug.

## If you are tempted to write the missing scripts

Don't, unless you are reconstructing them from a real source — the upstream
`EricTechPro/super-board` repo, or a working install that predates the fork.
A dispatcher written from the surrounding prose will be plausible, official
looking, and wrong in ways nobody can see until it silently mis-files bugs.
If you do reconstruct them from a real source, cite that source in the script
header and delete the corresponding section here.

## Tracking

- Integration audit: https://github.com/Wladefant/super-board/issues/38
- This remediation: https://github.com/Wladefant/super-board/issues/39
