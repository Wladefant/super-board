---
name: veyyon-main-crash-prevention
description: "Diagnose a Veyyon Main session that dies taking every lane with it, in either of two shapes: a LOGGED fatal (`Session exit recorded` reason unhandled_rejection, e.g. EBUSY on puppeteer_dev_chrome_profile after a browser-tool launch) or a SILENT death with no `Session exit recorded` line at all (process terminated below JavaScript: external kill, OOM, or native abort). Includes the discriminator set that separates them."
---

# Veyyon Main crash: browser launch EBUSY (2026-09-22)

## Signature
In `~/.veyyon/profiles/default/logs/veyyon.<date>.log`:
- `"message":"Unhandled rejection"` with `EBUSY: resource busy or locked, rm '...\Temp\puppeteer_dev_chrome_profile-XXXX'` at `cleanUserDataDir`
- immediately followed by `"Session exit recorded" ... "reason":"unhandled_rejection","kind":"fatal"`.
Lanes run in-process, so one lane calling the `browser` tool (headless) kills Main and every lane.
Observed 2026-09-22: 18:10:50Z, 19:26:24Z, 19:33:43Z (pids 22576, 26428, 6248), all this exact signature.

## Chain
1. Chromium starts but never exposes DevTools. Trigger seen: `RemoteDebuggingAllowed = 0` under
   `HKCU\SOFTWARE\Policies\Google\Chrome` and `...\Microsoft\Edge` (written 2026-09-22T14:07:07Z; chrome prints
   `DevTools remote debugging is disallowed by the system admin.`). Check:
   `reg query HKCU\SOFTWARE\Policies\Google\Chrome /v RemoteDebuggingAllowed` (also HKLM).
2. puppeteer-core `BrowserLauncher.launch` catch runs `void browserCloseCallback()`; the exit hook `rm`s its temp
   profile while Chromium still holds it -> EBUSY -> nobody awaits it -> unhandled rejection -> fatal exit.

## Fix
- Source: veyyon commit `6a9a2d8b4365dd58a8793e0b756cea0994d6ba7b` on branch `fix/browser-launch-profile-ebusy-crash`
  (`packages/coding-agent/src/tools/web/browser/launch.ts`): pass an owned `TempDir` as `userDataDir`, so puppeteer never
  removes it; failed launch now returns a ToolError naming the policy. Takes effect only after the binary at
  `%LOCALAPPDATA%\veyyon\veyyon.exe` is rebuilt/installed AND Main is restarted.
- Policy removal needs an elevated shell (non-admin `reg delete` -> Access is denied). Backups:
  `~/.veyyon/run/crashdiag/backup-HKCU-Policies-*.reg`.

## Until the fixed binary runs in Main
- Do not dispatch lanes that use the `browser` tool with the default headless launch; use `read` for pages, or
  `app.cdp_url` to an already-running browser (no launch, no profile cleanup).
- Repro (safe, standalone bun): `~/.veyyon/run/crashdiag/repro.ts` from `packages/coding-agent`.
  Before fix: `[Unhandled Rejection] EBUSY`. After: clean ToolError, no profile left, exit 0.

## Second signature: SILENT death, no exit record (2026-09-25)

Same impact as the EBUSY crash — Main dies and every in-process lane dies with it — but the log
carries no account of it. Distinguish the two before doing anything else.

### Signature
- `~/.veyyon/profiles/<name>/logs/veyyon.<date>.log`: the process's last JSON line is something
  ordinary (`Schema failed validation, using fallback`, `Usage fetch resolved`, …) and there is
  **no `Session exit recorded` line for that pid**, no `level: error`/`fatal` line.
- A *new* Main pid starts seconds-to-minutes later, usually resuming the **same** session id.
- Every lane transcript in the session dir ends mid-turn within the same second.

### Discriminators (run these before forming a hypothesis)
1. **Did the exit reach JavaScript?** `Session exit recorded` is written by `#recordSessionExit`
   (`packages/coding-agent/src/session/agent-session.ts`), driven by the postmortem handler table
   (`packages/utils/src/postmortem.ts`: `exit`, `SIGINT`, `SIGTERM`, `SIGHUP`, `uncaughtException`,
   `unhandledRejection`). *Any* orderly exit — fatal or not — leaves the line. Its absence proves
   the process was terminated **below** JavaScript: external `TerminateProcess`, native abort, or OOM.
2. **Memory / handles / threads.** `~/.veyyon/telemetry/commit.csv` has per-pid rows
   (`time,pid,name,privateMB,wsMB,handles,threads,commitTotalMB,commitLimitMB`). Flat private bytes,
   flat handles, and system commit well under `commitLimitMB` rule out an OOM kill and any leak.
   (2026-09-25: 1,885 MB private / 581 handles / 77 threads, commit 31.6 GB of 57.1 GB = 44 %.)
3. **Was there a native trace?** The win32 terminal stderr guard (shipped in veyyon PR #77 /
   `d94dfa7a`) re-points `STD_ERROR_HANDLE` at the day's log, so a Bun panic's stderr text lands there.
   It is wired in `hosts/terminal/engine/src/terminal.ts`. Check for **non-JSON lines** in the log
   (a JSON parser over every line; count failures). Zero non-JSON lines ⇒ no runtime panic was printed ⇒
   the external-kill class is what remains.
4. **Marker coverage.** `~/.veyyon/profiles/<name>/logs/inflight/<pid>-*.json` holds one file per
   in-flight **tool call**, deleted when the call returns. Leftover markers for the dead pid name the
   abandoned call, and the next launch logs `Previous session died with a tool call in flight`.
   **No marker means no tool call was in flight** — a death inside a provider turn is invisible to it
   (this is the known coverage gap; see the phase-heartbeat ask on issue #73).
5. **Crash artifacts.** `WerSvc` is stopped+disabled (`Start = 4`) on this workstation and
   `%LOCALAPPDATA%\CrashDumps` is empty, so *absence* of a WER event or dump is not evidence.
   Same for `~/.veyyon/run/terminals/<pid>-*.json`: a graceful exit removes its entry, so a lingering
   entry for the dead pid is weak corroboration of an exit path that never ran — weak, because stale
   entries are common.
6. **Was it tree-wide?** Check a detached sibling that should outlive Main (e.g. the Antigravity
   sidecar: `curl -s http://127.0.0.1:45123/health` reports `uptimeSec`). A surviving sibling rules out
   a job-object/process-group kill of everything.

### Cause ladder this leaves
- **External hard kill** (`taskkill /F`, `Stop-Process -Force`, libuv's Windows SIGTERM emulation):
  writes nothing anywhere — the only class no in-process mitigation can record. Needs an
  out-of-process supervisor to attribute and prevent.
- **Console/pane close** (`CTRL_CLOSE_EVENT`): Windows maps it to none of the handled signals, so it is
  silent by construction. Fix is a `SetConsoleCtrlHandler` FFI shim (same `bun:ffi` pattern as the guard).
- **Native abort that lost its own write**: possible, unfalsifiable; the #77 test itself notes a kill
  can take the filesystem write with it.

### Reporting
Report on the open harness issue rather than opening a competing one:
https://github.com/Wladefant/veyyon/issues/73 (silent death below JavaScript; the S slice landed in
PR #77 and is blind to a death with no tool call in flight). Include the pid, the exact last log line,
the telemetry row, the non-JSON-line count, the `logs/inflight` state and the successor pid.
Note `WerSvc Start = 4` so nobody reads the missing WER event as meaning anything.
