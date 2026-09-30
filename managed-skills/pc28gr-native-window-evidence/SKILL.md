---
name: pc28gr-native-window-evidence
description: "Diagnose native TestING windows and Azure DevOps runner actions with measured process identity, safe non-WScript input/capture, real readiness, and recording provenance"
---

# PC28GR native-window evidence

Read `skill://pc28gr-ssh-remote` for connection and quoting context, but the corrections below supersede its contradictory historical claims. This supplement grants no access beyond current operator authorization. Historical access exceptions apply only to their original assignment. On2026-09-21 the live operator authorized laptop testing and direct SSH transfer of the small update; the measured update was38,264,333bytes, not the estimated7MB. That did not authorize a new tunnel, bulk basis transfer, or an unrelated download route. Preserve all other safety boundaries.

## Inspect helpers before invoking them

- The retained laptop `Take-Screenshot.ps1` **-Window branch invokes WScript.Shell.SendKeys('%')**, discovered by source inspection on2026-09-21. Do NOT invoke that branch under the no-WScript/SendKeys contract. A familiar helper name is not evidence of safe implementation.
- Use inspected Win32/UIA alternatives: resolve actual owned windows by PID and EnumWindows/GetWindowRect, then capture with safe PrintWindow/BitBlt or desktop-region APIs. Inspect resulting image; a nonempty PNG is not proof of useful rendering. Do not claim an alternative was executed merely because these APIs exist.
- No scheduled tasks, WScript/cscript/VBS/SendKeys, hidden agent starters, autostart, or executable processes from TEMP. Put exact owned helper/output paths under Downloads, not new paths under legacy claude-named directories. Never alter a shared installed helper silently.

## Measure instead of assuming

- Measure SSH process SessionId, actual window station/input desktop, child executable/parent IDs and HWNDs where relevant. PC28GR returned **Session1/WinSta0/Default**, not0. Parent facts do not establish child facts.
- Exact title lookup can fail because of title changes/encoding; enumerate and bind the real PID/window rather than invent Session0 or policy causes. MainWindowHandle=0 alone does not prove no visible window.
- Resolve actual shortcut executable, arguments and working directory. Exercise that chain unchanged. Reconstructed XAML proves only WPF viability, not the product shortcut.
- Keep an already-authorized supervised SSH launch alive through observation when that launch requires it; do not create a new watchdog/service. Measure actual lifetime. Neither process creation nor a fixed short wait is readiness. An8-second missing-window observation was a false negative. Record heartbeat/window/child exit evidence.
- Missing GUI does not establish job-object cleanup, policy denial, Session0, or WPF failure. Windows SSH lifetime observations are case-specific, not a universal explanation.
- Multiple javaw PIDs do not prove multiple operator clicks or mutual blocking. Bind exact executable, command class, installation, creation time and window identity. **Parent PIDs can be reused**: a currently observed parent process created after its alleged child cannot establish the original launch provenance. On2026-09-21 PID10548 had been reused by WisprFlow after the Studio child was created.
- Startup readiness must identify the real main window, not merely a visible titled rectangle. The old838 predicate accepted a400x250 `SunAwtDialog` error dialog. Its correction requires `SunAwtFrame` AND the observed INGenious Studio title identity, allowing genuine project/version variations and rejecting dialog/splash/foreign windows. PID existence alone is not successful startup.

## Genuine Azure DevOps Web Runner action

Evidence: https://github.com/Wladefant/ing-qa-automation/issues/789#issuecomment-5767494534 . On2026-09-21 the authorized example case was4119478, plan14107871, suite14107890, point3866224, configuration195; never treat those historical IDs as permanent authorization.

- UIA Toggle/SelectionItem made the Execute checkbox look checked but left Run for web application disabled. A genuine guarded Win32 checkbox Off→On click delivered the application's selection event and enabled Run. Verify action state, not checkbox paint alone; do not infer missing permissions from that specific observation.
- The actual Run action opened a populated runner with the exact case title, two named steps, expected results and configuration. Its address was `_testExecution/Index` **without query parameters**. Opening the bare Index or merely the Execute page is not equivalent to this action; do not invent launch parameters.
- Bind organization/project/plan/suite/case/point/configuration before the action, verify foreground ownership and the unique visible target, then verify populated runner content. Never force disabled controls or click Outcome/Save/Save-and-close as part of opening.
- A manually opened matching window without product provenance is not automatically safe to reuse or duplicate. Preserve it unless the current owner has authorized and verified a safe handoff. Manual launch proof is separate from full new-helper execution and installed TestING-button proof.

## Specific reporting repair

For TestING javaw→Node→PowerShell, the original child exited0 with no heartbeat/window. detached:false alone and keeping Node alive alone failed. Attached PowerShell with drained standard streams worked in the isolated candidate. This is a measured application repair, not a universal Node/Windows rule.

Evidence: https://github.com/Wladefant/ing-qa-automation/issues/691#issuecomment-5760609738 and https://github.com/Wladefant/ing-qa-automation/pull/809 . Candidate4743f2435cd2c213c44c5dedfcbdefa165184b73 used ING_QA_REPO without changing installed shortcut exe/args/cwd; UIA Cancel gave PS2/Node2 and reports16→16. Later installed native desktop open/cancel proof is https://github.com/Wladefant/ing-qa-automation/issues/691#issuecomment-5762195752 . Do not equate startup/cancel proof with submission, attachment, cloud-transfer or paused-recording proof.

## Recording-state provenance

- Collapsed UI or a visible-row cap is not an inventory. Read actual recording indexes and distinguish recorder lifecycle from business/ADO outcome.
- On2026-09-21 all12 live4119478 indexes were independently byte-compared with the pre-reset backup:6done,4pausiert,2unvollstaendig, all unchanged. Earlier0open/12completed was unsupported UI inference; the UI caps displayed prior attempts at3.
- Never create synthetic recording folders/indexes to substitute for genuine paused continuation. Verify the same real belegsatzId, part sequence, steps and actual receipts after normal tester actions. A Fortsetzen-activated banner is not proof that recording resumed.
- Daemon readiness, artifact files and a successful photo API call do not prove native click recording. Exercise a real identifiable action and inspect its emitted Java/index semantics. A failed input helper cannot be relabeled successful because files were created.

## End-to-end proof, cloud limits and cleanup

- Inspect real product screenshots and redact identities before publication. Capture only owned/authorized windows.
- Verify cancellation leaves report storage unchanged. Verify successful submission with actual files/receipt, not just window construction.
- Local NTFS attributes, a reparse point, locally saved files and a running OneDrive process do **not** prove remote synchronization or that transfer is currently pending/in progress. Keep cloud state explicitly unconfirmed unless directly observed through an authorized mechanism. Offline attributes alone do not prove byte-read failure.
- A local original plus matching path/metadata is not remote byte identity when remote content cannot be read/hashed.
- Local launch.stop or agent cancellation does not establish remote cleanup. Track exact owned remote processes, close owned dialogs gracefully, and verify remaining owned processes/files. Preserve user processes and active recordings.
- A windowless JVM may still be alive. Revalidate PID/starttime/executable before bounded installed-JDK diagnostics. Non-daemon AWT/JavaFX threads are evidence, not alone a complete cause. Do not force-kill, inject System.exit, or waive the verified backup gate without the necessary explicit decision.
- Use exact owned cleanup paths only. PowerShell -LiteralPath does NOT expand wildcards: `Remove-Item -LiteralPath ...\*.ps1` does not delete all matching scripts, but is still not an acceptable cleanup recipe. Confirm actual effects before reporting deletion/data loss.
