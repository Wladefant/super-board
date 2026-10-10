---
name: flow-qa
description: "Real user-flow QA for UI PRs in Chromium at 390x844, 390x420 and 1440x900. Drive flows with touch input against the served commit. Under default SUPERBOARD_MERGE_FIRST, verify post-deploy on staging with immediate revert on failure. Setting SUPERBOARD_MERGE_FIRST=0 restores pre-merge receipts."
---

# Flow QA

A page that opens is not a flow that works. Flow QA does what a user does: tap, type, hover,
swipe a sheet closed, switch tabs, open the keyboard. It checks each step.
Under default `SUPERBOARD_MERGE_FIRST`, approved staging UI PRs merge and deploy first. Flow QA verifies the live staging host against the deployed commit SHA. The lane reverts the commit immediately on failure. When `SUPERBOARD_MERGE_FIRST=0`, the UI merge gate (`github_pr_gate.py`) blocks a PolySimulator `staging` UI PR without a matching pre-merge `FLOW-QA` receipt.

## Files

- Runner: `workflows/portable/flow_qa_runner.mjs` (installed as `~/.veyyon/workflows/flow_qa_runner.mjs`, flows in `~/.veyyon/workflows/flows/`).
- Flow definitions: `workflows/portable/flows/<project>.json` (`polysimulator`, `shipnovo`, `superboard-week`).
  `shipnovo` defines the native article lifecycle flow (`article_lifecycle_native`) and the Komo review toolbar dialog pin flow (`komo-dialog-pin`).
  The flow creates a unique `QA-` article, tests virtual keyboard focus (390x420), exercises tabs, and saves.
  It opens row edit (`<sku> bearbeiten`) and dismisses the sheet via CDP touch swipe on mobile.
  On 1440x900, it uses desktop close controls.
  It deletes the article through row actions (`Weitere Aktionen für <sku>` -> `Artikel löschen` -> confirm).
  `superboard-week` runs against the Week view fixture preview
  (`bun scripts/week-preview.ts` in `packages/telegram-agent-harness`), which serves `/api/version`.
- Gate: `github_pr_gate.py`, function `evaluate_flow_qa_receipt`.

## Steps

### Default: Post-Deploy Verification (`SUPERBOARD_MERGE_FIRST`, default ON)

1. Merge the approved UI change to staging. The staging deployment starts automatically.
2. Confirm staging is serving the new merge commit via `/api/version` (40-hex SHA).
3. Run the runner against the live staging host under `build_slot.py run --class heavy --mem-gib 3`:

   ```bash
   node ~/.veyyon/workflows/flow_qa_runner.mjs --project polysimulator \
     --base-url https://staging.polysimulator.com --expected-sha <40-hex deployed sha> \
     --storage-state <signed-in staging state json> --output <dir> \
     --viewports 390x844,1440x900
   ```

4. The runner strictly refuses production URLs (`polysimulator.com`, `zaraprptkegxqpvnsubu`, `akamai-iad-prod`). It runs against staging only.
5. Read `<dir>/receipt.txt`. Confirm all assertions pass (`fail=0`, `pass>0`).
6. Pass: post the verification receipt to the tracking issue and PR.
7. Fail: the merging lane reverts the exact merge after confirming its served identity. Unknown or mismatched identity means: do not revert, ask Main.

### Restored Pre-Merge QA (`SUPERBOARD_MERGE_FIRST=0`)

1. Build the PR head locally and serve it (`build_slot.py acquire <name>`). Confirm the local server reports the head commit (`/api/version`).
2. Run the runner against the local server:

   ```bash
   node ~/.veyyon/workflows/flow_qa_runner.mjs --project polysimulator \
     --base-url http://127.0.0.1:<port> --expected-sha <40-hex head sha> \
     --storage-state <signed-in staging state json> --output <dir> \
     --viewports 390x844,1440x900
   ```

3. Read `<dir>/receipt.txt` and post the receipt lines as a PR comment before merge:

   ```
   FLOW-QA: PASS <served 40-hex sha>
   FLOW-QA-ASSERTIONS pass=N fail=0
   FLOW-QA-VIEWPORTS 390x844,1440x900
   ```

4. Delete `frontend/.next`, stop the server, and release the build slot.
## What the gate accepts

- The marker names the served sha, and that sha names the PR diff. A stale sha is rejected.
- `fail=0` and `pass` above zero.
- Both required viewports are listed.
- The newest receipt for this diff decides. A later FAIL or RETRACTED overrides an earlier PASS.

## Checks per step

visible, not covered (element at point, drilling into open shadow roots without host occlusion), tap target 44 px or more, no horizontal overflow,
focused input inside the viewport with the keyboard open, no document reload, swipe dismissal.
Actions include `goto`, `tap`, `type`, `hover` (center of element or x/y with settle wait), `keyboard-open`, `swipe`, `assert`, `upload`, and `cleanup`.
Optional `tap` and `type` steps skip absent targets with an `optional_skipped` check. Required steps still fail when their targets are absent.
Assert steps use locator semantics. On every poll, the runner queries the selector again. It never keeps an ElementHandle across polls. This detects dynamic node replacements correctly.
A `goto` returns at DOMContentLoaded, so the opened page can fire its own `load` event during the next
step. That late `load` keeps the document identity token and does not count as a reload. A new document does.
Mutations use a `QA-` prefix and each flow cleans up. Cleanup steps deleting threads or mutations strictly refuse any target not prefixed with `QA-`. A failed cleanup fails the run.
The runner accepts native dialogs (`confirm`, `alert`) like a user. When a step closes the page, the next
viewport starts on a new page with the same cookies. CDP calls have no own timeout, so always start the
runner through `build_slot.py run --timeout`.

## Failure

A FAIL receipt is a real result. Fix the UI and rerun on the new head. Never edit a receipt.
An infrastructure error (Chromium did not start, port busy) is not a PASS and not a UI FAIL.
Rerun once. If it repeats, report the error text.
