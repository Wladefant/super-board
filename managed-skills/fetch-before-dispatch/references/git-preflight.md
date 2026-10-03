# Reference: Git Preflight Command Sequences

Exact command sequences for preflight verification before lane dispatch.

## Standard Preflight Checklist

```bash
# 1. Prune stale remote tracking refs and fetch latest
git fetch origin --prune

# 2. Check current status and divergence from default branch (e.g. staging or main)
git status -sb
git log --oneline HEAD..origin/staging | wc -l

# 3. If behind by >0 commits, merge forward with merge commit (--no-ff)
git merge --no-ff origin/staging

# 4. Resolve any merge conflicts, run tests, and commit
git status

# 5. Extract exact 40-character base SHA for lane contracts
git rev-parse HEAD
```

## Worktree Branch Grounding
When creating isolated worktrees for worker lanes:
```bash
# Ground fresh worktree directly from fetched remote tip
git worktree add -b feat/<topic> <path-to-worktree> origin/staging
```
