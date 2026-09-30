# Gotchas: Parallel Lane Dispatch

High-ROI operational failure modes, signatures, and anti-patterns for multi-lane dispatch.

## 1. Look-Alike Prompt Refusal
- **Symptom:** `task(tasks=[...])` with 3+ lanes returns:
  `Refused N parallel triage agents. Homogeneous triage is a batched lookup/classification task...`
- **Root Cause:** The harness inspects task prompt AST and lexical structure. When task bodies share similar verbs, identical acceptance criteria, and similar length, the harness classifies the batch as homogeneous triage even if the lanes touch different files.
- **Trap:** Retrying with regrouped batches or slightly reworded prompts in the SAME `tasks=[...]` array will trigger the rejection again, burning orchestrator turns.
- **Fix:** Dispatch as separate single `task` calls in consecutive turns, or partition by functional domain with external input files (`local://<name>.md`).

## 2. Turn-Waste Trap on Consecutive Refusals
- **Trap:** Orchestrator attempts 2–3 retries of the refused batch with minor cosmetic prompt tweaks. Each retry consumes a full orchestrator turn and high-effort reasoning context.
- **Rule:** Never repeat a refused batch shape. If the harness rejects a batch once, immediately switch to sequential single-lane dispatch in the very next turn.

## 3. Misclassifying True Lookups as Verification Tasks
- **Trap:** Spawning 5 parallel subagents where each subagent only calls `search` or `gh issue view` on one item.
- **Fix:** If no per-item code modification or complex verification is required, execute the lookup directly in the parent lane (e.g. `gh issue list` or `search`), consolidate results, and delegate to a single classification worker.

## 4. Worktree Collisions on Concurrent Creation
- **Trap:** Concurrently dispatching writing lanes that all attempt `git worktree add` at the same time.
- **Fix:** Main must create worktrees serially prior to dispatch (see `shared-worktree-lane-safety`), or assign distinct existing worktrees.
