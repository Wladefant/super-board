---
name: veyyon-worktree-node-modules-safety
description: "Rules for Veyyon wt-* worktrees and PolySimulator .wt-* worktrees whose node_modules links a shared tree: no install in a worktree, unlink the link (rmdir, no /s) before git worktree remove, workspace-package edits invisible to local tests, and how to recover"
---

# Veyyon worktree node_modules safety

Veyyon lane worktrees (`C:/Users/wkiri/development/wt-*`) symlink `node_modules` to the shared `C:/Users/wkiri/development/veyyon/node_modules`. Every lane shares that tree.

## Rules
1. Never run `bun install` inside a `wt-*` worktree. It repoints the ~28 workspace links (`@veyyon/*`) in the shared tree at that worktree, and every other lane then fails with `Cannot find package '@veyyon/catalog/models'` or `@veyyon/kernel`.
2. Install only from the main checkout (`C:/Users/wkiri/development/veyyon`), and only when no lane is in the middle of a test run.
3. Before deleting a worktree, unlink its node_modules first: `cmd /c rmdir <wt>\node_modules`, then `git worktree remove <wt>`. Windows PowerShell 5.1 `Remove-Item -Recurse` follows directory symlinks and wipes the shared tree. This happened on 2026-09-26 at 22:47Z.
4. **Verify the unlink before any `git worktree remove`**, in every repo (Veyyon, Shipnovo/`bendhltool-wt`, PolySimulator). The Veyyon `bash` tool strips backslashes, so `cmd /c rmdir C:\...\node_modules` fails with "The system cannot find the file specified" and leaves the junction in place. `git worktree remove --force` then follows the junction and empties the target (2026-09-27 ~21:30Z: a Review67 cleanup emptied `bendhltool-wt/TemplatesFix/node_modules`, and `FmtFix` and `Shots60Main` junctioned onto it). Unlink with Node, `require("fs").rmdirSync("<wt>/node_modules")` (it removes only the junction), check that `fs.existsSync("<wt>/node_modules")` is `false`, and only then remove the worktree. Recovery: `npx -y npm@10 ci --no-audit --no-fund` in the emptied target, if its lockfile matches.

## Workspace-package edits are invisible to local tests
Because the shared links resolve `@veyyon/*` to the MAIN clone's source, an edit to `kernel/`, `packages/agent`, `packages/ai` etc. inside a `wt-*` worktree is NOT seen by `bun test` run in that worktree. Symptom: the new API is missing (e.g. `configSourceStamp is not a function`, 2026-09-27, [Wladefant/veyyon#110](https://github.com/Wladefant/veyyon/issues/110)), or a test fails identically with and without your change. CI does not have this problem.
- To test such an edit locally, add a temporary inner junction that shadows the shared link for the consuming package only: `cmd /c "mkdir packages\coding-agent\node_modules\@veyyon && mklink /J packages\coding-agent\node_modules\@veyyon\kernel <wt>\kernel"`. Then remove it with `rmdir`, innermost first, before you yield.
- Also read local failures in edited workspace packages with this in mind. A "baseline" comparison is only valid if both runs resolved the same package source.

## Recovery
- Workspace links pointing at a wt-* path: run `bun install` in the main checkout, then check that the `@veyyon/*` links resolve to `veyyon/*`.
- Wiped tree: `bun install` in the main checkout, check that top-level entries (about 300) and `.bin` are present, then run one focused test.
- Tell the other lanes that failures inside the incident window were environmental.

## PolySimulator worktrees (frontend/node_modules from the protected store)

Since 2026-09-27 PolySimulator lane worktrees link `frontend/node_modules` to a protected store outside every git checkout: `C:/Users/wkiri/.veyyon/shared/polysim-frontend-deps/<key>/node_modules`, one immutable install per lockfile (`<key>` = first 16 hex of sha256 of the LF-normalized `package-lock.json`). The store's `node_modules` carries a deny ACE `Everyone:(OI)(CI)(DENY)(DE,DC)`, so nothing inside it can be deleted or renamed. A recursive delete that follows a junction fails with access denied instead of wiping the tree. Reading, running node/tsc/next/tsx and deleting the junction itself still work.

Why: `git worktree remove --force` (Git for Windows 2.49, measured 2026-09-27) follows a `frontend/node_modules` junction and deletes the target's contents. Lanes had junctioned to other lanes' worktrees (`.wt-review-5576`, `.wt-surface-manager`, `wt/5553-3633288`, often in chains), so one removal emptied the tree for dozens of lanes (2026-09-27 ~13:50Z and ~16:35-16:55Z). `git clean -ffdx`, `rm -rf`, `rmdir /s`, `Remove-Item -Recurse` and `shutil.rmtree` did not follow junctions in the same test.

1. **Link:** `python C:/Users/wkiri/.veyyon/workflows/polysim_frontend_deps.py link <wt>`. It picks the store matching the worktree's lockfile. If none exists, build it: `... sync --worktree <wt>` (or `sync --repo C:/Users/wkiri/development/polysimulator --ref origin/staging`). Never junction to another worktree and never run `npm ci`/`npm install` through a store link (it fails on the ACL by design).
2. **Remove:** `python C:/Users/wkiri/.veyyon/workflows/wt_remove.py <wt> [--force]`. It refuses the main worktree, worktrees other lanes link into, and worktrees with a process running inside. It unlinks every junction/symlink first and then runs `git worktree remove`. Never call `git worktree remove`, `Remove-Item -Recurse` or `rm -rf` on a worktree directly.
3. **Status and maintenance:** `polysim_frontend_deps.py status` lists stores and every worktree's link; `relink-all [--apply]` repoints old worktree-to-worktree junctions to their store (skips busy worktrees); `prune [--apply]` deletes stores nothing links to (never the newest). `unprotect <key>` is for maintenance only; re-run `protect <key>` right after.
4. **Recovery of a worktree-owned install** (a real `frontend/node_modules`, e.g. the main clone): confirm no ancestor `node_modules`, check `package-lock.json` matches the ref you want, run `npm ci --no-audit --no-fund --prefer-offline` in its `frontend/`, confirm `git status` shows the lockfile unchanged, then tell the lanes that failures in the window were environmental.
