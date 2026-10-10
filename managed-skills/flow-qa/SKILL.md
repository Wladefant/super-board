---
name: flow-qa
description: "Real user-flow QA for UI PRs: drive the served build in Chromium at 390x844, 390x420 (keyboard open) and 1440x900, in light and dark, with touch input; post a FLOW-QA receipt bound to the served commit. Use before merging any PolySimulator or Shipnovo UI change, and whenever a lane must prove a user path works, not only that a page opens."
---

# Flow QA

A page that opens is not a flow that works. Flow QA does what a user does: tap, type, hover,
swipe a sheet closed, switch tabs, open the keyboard. It checks each step.
The UI merge gate (`github_pr_gate.py`) blocks a PolySimulator `staging` UI PR without a
matching `FLOW-QA` receipt.

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

## Shipnovo account scope

Use `--fixture-state empty|populated|disconnected|limited --read-only` with the matching signed-in testbed storage state.
The default fixture suite discovers conversations and orders from the account UI. It never uses record UUIDs.
Each state runs its readiness checks. Missing records fail with `account not ready: <missing requirement>`.
The populated suite includes the messages list, conversation, focused composer, send control, and return path.
Run all three default viewports in both themes. Do not click send or save.
Read-only mode blocks write requests. Check that opening a thread produces no error toast or fallback.
The composer loads its workspace through a POST server action that performs only SELECTs.
For the reviewed testbed revision, `--read-only-workspace` allows only that exact action ID, same-origin `/messages`, and a discovered read conversation ID.
It rejects every other action, body field, host, and path. The receipt records `READ-ONLY-ACTION`.
Before this option, verify the full served call path and capture tenant-scoped message/workspace count and row hashes.
Repeat that snapshot after the run. Require identical counts and hashes. Never allow draft saving or provider calls.
Never open the unread-product-question fixture. Use buyer-question or order-follow-up instead.
The limited suite checks readable messages, absent reply controls, and the access-denied redirect from account settings.
Every receipt labels `scope: fixture <state> on testbed <served sha>` or `scope: live account`.
A fixture receipt proves only the synthetic testbed scenario. It never proves the original operator scenario.
Legacy mutating flows remain available outside read-only mode for authorized accounts.
When a Windows Node wrapper imports the runner, use `pathToFileURL` or a `file:///C:/...` URL.
Bare `C:/...` ESM imports fail before browser launch or assertions.

## Steps

1. Build the PR head and serve it. Use `build_slot.py acquire <name>` for the build and the
   server. Confirm the server reports the head commit (`/api/version`, `git_sha`, 40 hex).
2. Run the runner from the repo that holds the flow files:

   ```
   node ~/.veyyon/workflows/flow_qa_runner.mjs --project polysimulator \
     --base-url http://127.0.0.1:<port> --expected-sha <40-hex head sha> \
     --storage-state <signed-in staging state json> --output <dir> \
     --viewports 390x844,1440x900
   ```

   Use the default viewports (all three) for a full run. The gate needs 390x844 and 1440x900.
3. The runner refuses to start when the served sha differs from `--expected-sha`.
   It refuses PolySimulator production URLs. It runs staging only.
4. Read `<dir>/receipt.txt`. It holds exactly the lines the gate parses:

   ```
   FLOW-QA: PASS <served 40-hex sha>
   FLOW-QA-ASSERTIONS pass=N fail=0
   FLOW-QA-VIEWPORTS 390x844,1440x900
   ```

5. Post those lines as one PR comment. Add the screenshot table if the PR is visual.
   Do not edit the lines. Do not post a PASS the runner did not print.
6. Delete `frontend/.next` and stop the server. Release the build slot.

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
