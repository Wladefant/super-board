---
name: complete-request-inventory-recovery
description: "Resume a multi-section program without losing prior tasks: reconcile GitHub and the durable request ledger, append the complete todo inventory, and dispatch independent native workers"
---

# Complete request inventory recovery

Use when an operator resumes a multi-section program, reports missing sections, or adds a new request while older work remains.

1. Read the installed native-superboard-background-handoff skill and relevant project skills. Current operator scope, production boundaries, model routing and merge policy override historical skill clauses.
2. Inspect the existing session todo board without replacing it. Run the installed request ledger's `list` command and recover every open request's original prompt, acceptance criteria, current head, evidence, owner, dependencies, blocker and next action. GitHub issues and project cards remain authoritative; the local ledger is a recovery cache.
3. Reconcile the full inventory with the authoritative GitHub issue bodies and retained original requests. Do not assume a short recap or a board showing only configuration work is complete. Retain duplicates as explicit linked items until their relationship is established; never silently delete an unresolved item.
4. Append missing sections and tasks to the existing board. Preserve per-criterion acceptance text in a durable JSON artifact and a GitHub inventory report. Label unapproved product choices and external access prerequisites as blockers, not completed work. A new request never authorizes dropping old scope.
5. Define disjoint section ownership and real data/interface dependencies. Todo phase order is not a dependency. Check RAM before spawning; dispatch genuinely independent sections in one native task batch within current capacity. Flash handles bounded recovery, implementation and QA; stronger implementation or independent review is selective, not a fixed wave for every task.
6. Reuse valid unchanged-head evidence. Recovery should produce exact runnable implementation/QA tickets, not restart all prior reviews. Follow the existing prepare-native, actual-handle registration, authentic-result completion and continuation-driver protocol; do not rebuild it in prompts or introduce nested CLI agents.
7. Persist owner/blocker/next-action changes through the existing ledger utility, preserving criteria, authorization and history. Its `--criteria` JSON accepts objects with `id` and `description`, not an array of strings. Slow finite CLI mutations may run as background commands; consume actual completion results before claiming persistence. Never delete active locks or treat a timeout as proof that no change landed.
8. Keep independent work moving when one section blocks. A missing login, CI access decision or tooling failure is not a prerequisite for unrelated source work. Do not bypass a genuine publication safety boundary: a feature push can itself trigger prohibited CI targets.
9. Advance every item to its actual outcome: verified completion, authorized merge-ready state, or explicit blocker with owner and next action. A local commit, prepared ticket, passing unit test or merge alone does not establish live acceptance.
10. Publish comprehensive progress/evidence on GitHub first and read it back, then send concise links. On the next continuation, reconcile again using the preserved full inventory rather than replacing it with the latest subtask.
11. Mechanical lane inventory verification: When recovering stopped or crashed lanes, never infer worktrees, branches, or PRs from prompt text, markdown headings, or lane names. Read the lane's actual last tool calls (`set_cwd`, `cwd` in bash, `git -C <path>`). Mechanically verify every worktree with `git -C <wt> rev-parse --abbrev-ref HEAD` and `git worktree list`, and verify every PR with `gh pr view <N> --json state,headRefName,headRefOid`. Any missing worktree, closed/merged PR, or branch mismatch must be explicitly marked UNVERIFIED (e.g. `UNVERIFIED (missing worktree: <path>)`, `UNVERIFIED (PR #N merged: <title>)`). Use `python workflows/portable/lane_inventory.py audit` to produce mechanically verified recovery inventories.

## Evidence limits

A policy instruction is not executable scheduler enforcement. Configured capacity is not proof that every worker ran. Client telemetry is not proof of executed transactions. A stale browser cookie is not proof that supported vault-backed test authentication is unavailable. Never upgrade one of these into a stronger completion claim.
