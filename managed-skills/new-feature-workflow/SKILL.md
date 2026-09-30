---
name: new-feature-workflow
description: "Starts every task in an isolated Git worktree branched from origin/staging with preflight sync, dependency isolation, and build-slot coordination. Use at the start of any new feature, bugfix, or task before writing code."
---

# New Feature Workflow

Starts every feature, bug fix, or refactoring task in an isolated Git worktree branched from `origin/staging`. Ensures multi-agent concurrency safety, dependency hygiene, and clean integration.

## Golden Invariants

- **Base Trunk is `origin/staging`:** ALWAYS branch from `origin/staging` (the development trunk). NEVER branch from or push directly to `main` (PolySimulator production is strictly off-limits).
- **Merge Commits Only:** All branches sync and merge with `--no-ff` merge commits. NEVER squash, NEVER rebase, and NEVER rebase-merge.
- **Dedicated Worktree per Lane:** One worktree and one branch per task. NEVER share, adopt, or modify another lane's worktree.
- **NEVER `git stash pop`:** Stashes in shared worktrees can collide across checkouts or corrupt working states. Keep work in committed checkpoint branches instead.
- **Build Slot Coordination:** Heavy compilation, dev servers, and Next.js builds must acquire the exclusive slot via `python C:/Users/wkiri/.veyyon/workflows/build_slot.py acquire <task-name>` and release it when finished.
- **Staging-Only Execution:** All work targets staging (`hgzyqmaanndcimnclxtv`). Production (`<prod-supabase-ref>`, `<prod-host>`) is strictly off-limits.

## Execution Steps

### 1. Preflight Fetch & Sync (`fetch-before-dispatch`)
Before creating any branch or worktree, verify the local repository is synchronized with the remote tip:
```bash
git fetch origin --prune
git log --oneline HEAD..origin/staging | wc -l
```
If behind staging, merge `origin/staging` forward first (`git merge --no-ff origin/staging`) and record the resulting 40-character base SHA.

### 2. Scope & Overlap Check
Scan active pull requests and open issues to ensure your task does not modify files concurrently owned by another lane:
```bash
gh pr list --state open --limit 20
gh pr diff <pr-number> --name-only
```
If your planned changes overlap with an active PR, coordinate file boundaries or select a distinct slice before proceeding.

Then locate the feature before searching (#227, #244):
```bash
python C:/Users/wkiri/.veyyon/workflows/feature_map.py query <term>   # from the repo root
```
Record the matched entry files, tests, owning issue and risk in your first note. If nothing matches, record `feature-map: no match for <term>` and add the feature with `feature_map.py generate` in the same PR.

### 3. Task & Branch Naming
Use a descriptive kebab-case name with the issue number or unique suffix:
- Feature: `feat/4521-orderbook-depth`
- Bug fix: `fix/4630-calendar-snapshot-rls`
- Worktree folder: `../.wt-<task-name>` (or `.wt-<task-name>` outside git-tracked trees)

### 4. Create Isolated Worktree
Create the worktree directly from the verified `origin/staging` base:
```bash
git worktree add ../.wt-<task-name> -b feat/<task-name> origin/staging
cd ../.wt-<task-name>
git branch --show-current  # Verify current branch is feat/<task-name>
```

### 5. Dependency & Process Isolation Rules
Host-level collisions can corrupt builds and starve machine resources. Follow these critical safeguards:
1. **Node Modules Isolation:**
   - **NEVER** run `npm install` where it can resolve `node_modules` from an ancestor checkout.
   - Committed lockfiles must NOT contain relative `packages` paths starting with `../` or `link: true`.
   - PolySimulator: link the protected shared store instead of installing: `python C:/Users/wkiri/.veyyon/workflows/polysim_frontend_deps.py link <worktree>` (build a missing store with `... sync --worktree <worktree>`). Never junction `frontend/node_modules` to another worktree. Details: skill `veyyon-worktree-node-modules-safety`.
2. **Unique Dev Server Port:**
   - Always bind dev servers to an explicit, unique port (e.g. `PORT=3055 npm run dev`).
   - Confirm listener PID belongs to your process:
     `Get-CimInstance Win32_Process -Filter "Name='node.exe'"`
3. **RAM & Process Cleanup:**
   - Terminate all dev servers and child processes before yielding. Verify port release.

### 6. Build Slot Arbitration for Heavy Tasks
When building production bundles or starting long-lived dev servers:
```bash
# Acquire slot
python C:/Users/wkiri/.veyyon/workflows/build_slot.py acquire <task-name>

# Execute build or test flow
cd frontend && npm run build

# Release slot
python C:/Users/wkiri/.veyyon/workflows/build_slot.py release <task-name>
```

### 7. Pull Request & Integration
1. Commit with clear, human-oriented messages and repository-authorized author:
   `Wladimir Kirjanovs <wladefant@gmail.com>`
2. Push branch: `git push -u origin feat/<task-name>`
3. Open PR targeting `base: staging`:
   `gh pr create --base staging --title "..." --body "..."`
4. Attach required verification evidence. For PolySimulator PRs touching `frontend/**` or trading/order paths, that means the Control Glass receipt against a server that serves the PR head: `python scripts/qa/control_polysim.py receipt --pr <N> --base-url <url>`. It posts `QA-RECEIPT: PASS|FAIL <served-sha>`. A receipt whose served SHA isn't the head doesn't count.
5. If the task fixed a `kind:bug` issue, run `python C:/Users/wkiri/.veyyon/workflows/gardener.py --scan-bugs-only --bug <N> --live --issue-repo <owner/repo>` before yielding. It files a lint-rule proposal proven against the pre-fix and post-fix lines, or records `no-rule: <reason>`. Suspended until the fix for https://github.com/Wladefant/super-board/pull/290 merges (its dry-run posted real comments); until then, record `gardener: suspended (#290)`.

### 8. Worktree Teardown
Keep the worktree until the PR is merged or closed in case delta reviews or adjustments are needed. Once merged into staging:
```bash
python C:/Users/wkiri/.veyyon/workflows/wt_remove.py ../.wt-<task-name>   # unlinks every junction first; add --force for a dirty tree
git branch -d feat/<task-name>
```
