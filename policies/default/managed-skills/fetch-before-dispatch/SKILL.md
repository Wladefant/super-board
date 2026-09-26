---
name: fetch-before-dispatch
description: "Mandatory git preflight before dispatching implementation lanes or starting multi-file work: fetch origin, compare with default branch, merge forward first."
---

# Fetch Before Dispatch

Mandatory preflight procedure to ensure all work branches and task contracts are grounded on the latest remote tip.

## Navigation
- **Gotchas & Failure Modes**: Read `gotchas.md` for real failure modes (e.g. shipnovo 110-commit stale branch trap).
- **Reference Procedures**: See `references/git-preflight.md` for exact CLI command sequences.

## When to Use This Skill
- Before writing any lane task contract or spawning implementation subagents.
- Before starting multi-file implementation or architectural refactors.
- When opening or rebasing a feature branch against the integration trunk (`staging` or `main`).

## Core Preflight Procedure
1. `git fetch origin --prune`
2. Compare HEAD with deploy branch: `git log --oneline HEAD..origin/<default> | wc -l`.
3. If behind: merge `origin/<default>` forward (`git merge --no-ff origin/<default>`) and resolve conflicts locally first.
4. Record the resulting 40-character base SHA in the lane contract: "build on <40-hex>; do not touch files outside your ownership".
5. Verify provider allowances and local service ports (e.g. WSL relay on 5432) before dispatching.
