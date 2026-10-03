# Gotchas: Fetch Before Dispatch

High-ROI operational failure modes when creating branches or dispatching implementation lanes.

## 1. Stale Base Branch Trap (Shipnovo Incident 2026-09-20)
- **Incident:** A frontend redesign was built on an unverified local checkout that was 110 commits and 588 files behind `origin/main`. Five independent lanes spent hours implementing changes on obsolete code, resulting in massive merge conflicts and required complete re-merging.
- **Rule:** Never dispatch implementation on an unverified base. Run `git fetch origin --prune` and verify `git log --oneline HEAD..origin/<default> | wc -l` is 0 before writing contracts.

## 2. Uncommitted Local Changes Wiped by Sync Merge
- **Trap:** Running `git merge origin/<default>` with uncommitted files in the working directory can cause Git to abort or leave untracked files in an ambiguous state.
- **Fix:** Commit local progress to a temporary commit or work branch first before merging forward.

## 3. Provider Quota Exhaustion at Dispatch Time
- **Trap:** Dispatching worker lanes without checking `veyyon usage --json`. A lane assigned to an exhausted provider fails immediately on connect (~60s timeout), wasting orchestrator turns.
- **Fix:** Verify active provider headroom before assigning lane models. Check sidecar health if Google Antigravity reports exhausted.

## 4. Local Port Collisions (Port 5432 / Dev Servers)
- **Trap:** Background dev servers or WSL relays holding port 5432 or 3000 block newly spawned worker tests.
- **Fix:** Verify port availability dynamically and terminate stale dev processes before dispatching waves.
