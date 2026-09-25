---
name: shared-worktree-lane-safety
description: "Rules for running background coding lanes safely against git worktrees without concurrent checkout collisions, I/O hangs, or repository corruption."
---

# Shared Worktree Lane Safety

Operational rules for parallel agent execution in multi-worktree environments.

## Navigation
- **Gotchas & Crash Modes**: Read `gotchas.md` first for details on concurrent checkout hangs and PowerShell variable stripping.
- **Reference Workarounds**: See `references/powershell-workarounds.md` for clean script execution and process reaping commands.

## When to Use This Skill
- Running 3+ concurrent implementation or coding lanes.
- Managing multiple git worktrees on Windows hosts.
- Debugging frozen git operations or corrupted worktree indexes.

## Core Rules
1. **Never Create Worktrees Concurrently:** Main must create worktrees serially prior to lane dispatch. Concurrent `git worktree add` commands cause disk I/O lockups and harness crashes.
2. **Read-Only Lanes Share Working Directory:** Triage and verification lanes that do not modify code do NOT need isolated worktrees. They operate read-only in the main checkout.
3. **RAM Hygiene:** Measure system RAM before spawning lanes. At >=85% RAM, reap idle dev servers and orphan node/git processes. At >=95% RAM, spawn no new workers.
4. **Clean Worktree Teardown:** Lanes must never delete their own worktree while running. Teardown is owned by the orchestrator after lane completion.
