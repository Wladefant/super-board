---
name: pc28gr-laptop-fast-lane
description: "PC28GR rule set: agent runs gh release download ON the laptop as a direct command (never via a script, never scheduled), copies into the SharePoint library with copy, installs and checks over ssh with short calls; scp only for small scripts"
---

# PC28GR fast lane (user rules 2026-09-21, updated 2026-09-24, ABSOLUT)

## 0. Why the 2026-09-11 alarm happened — never repeat it
The ING security alarm was caused by **scheduling**: a scheduled task ran `wscript.exe` -> `.vbs` starter -> `gh release download`. Not gh itself. **Scheduling is always a security problem** (user 2026-09-24): no scheduled tasks, no autostart, no watchdog, no invisible starter (wscript/cscript/.vbs/.js, `-WindowStyle Hidden`, `-EncodedCommand`) — ever, for anything. A direct, visible command run at that moment is fine. Repo rule: CLAUDE.md rules 2 and 4 (PRs #923, #924).

## 1. Release staging into SharePoint — direct commands only, NO script
- **Big files NEVER over scp** — not even a 38 MB update ZIP. **`scp` only for small script files** (few KB), nothing else.
- The release step must NOT go through a script (no `.ps1`/`.cmd` wrapper). Each step is one visible ssh command:
  1. `ssh pc28gr "cmd /c gh release download <tag> -R Wladefant/ing-qa-automation -D C:\Users\PC28GR\Downloads\rel-<sha8> --clobber && echo DL-OK || echo DL-FEHLER"` (timeout 45; laptop gh is logged in as `Wladefant`, repo scope).
  2. `copy /y` from `Downloads\rel-<sha8>` into `C:\Users\PC28GR\OneDrive - ING\Testmanagement-Service (GRDE601146) - TestING\Updates\` — ZIP and `.sha256` first, `latest.json` LAST, as a SEPARATE call; `MINI-INSTALLIEREN.cmd` -> `...\Installation\TestING-Installer.cmd` (only if its hash differs).
  3. `certutil -hashfile "<file in library>" SHA256 | findstr /v :` for each copied file; compare against the release sha256 list.
  4. `rmdir /s /q C:\Users\PC28GR\Downloads\rel-<sha8>`.
- **Quoting that works (measured 2026-09-24, a7f32cc9):** wrap the remote command in SINGLE quotes in bash and use plain double quotes for the cmd paths with spaces:
  `ssh pc28gr 'cmd /c copy /y C:\...\x.zip "C:\Users\PC28GR\OneDrive - ING\Testmanagement-Service (GRDE601146) - TestING\Updates\" && echo CP-OK'`
  The doubled-quote form `""...""` inside a double-quoted ssh string FAILED (certutil "Expected no more than 2 args, received 7").
- 2026-09-24 (884d8e20) was staged with a shipped `.ps1` before this rule was sharpened — do not repeat that.

## 1b. After shipping: issues auto-close — reopen them
PRs into `main` with `Fixes #N` auto-close the issues on merge. That is NOT done (CLAUDE.md: nothing counts until verified on the laptop). After staging: reopen each issue with a comment linking the release + change doc and "wartet auf Laptop-Pruefung", and move its Project 7 card to QA (`updateProjectV2ItemFieldValue`, Status option QA).

## 2. Everything else the agent does itself over ssh, properly
Unpack, `INSTALLIEREN.cmd -Unattended`, read `VERSION.json` + newest `installation-*.log`, kill lingering windowless javaw, screenshots. No asking the user to click when the agent can run it (except a running Studio: skill `pc28gr-ssh-remote` §6b). When the operator is actively working in Studio, leave the update to him (close Studio first, then double-click TestING).

## 3. Properly = fast and non-blocking
- One ssh call at a time, `timeout` <= 45, foreground, output <= 40 lines. Never a multi-minute foreground wait, never async ssh (veyyon#73).
- Long remote work runs as a visible minimised console with output to a file and returns at once:
  `ssh pc28gr 'cmd /c start "" /min cmd /c "<cmd> > C:\Users\PC28GR\Downloads\lauf.txt 2>&1"'`; poll `type ...\lauf.txt | findstr /i "Fertig Fehler"` in later short calls. Never scheduled, never hidden.
- Batch checks into ONE call with `& echo ---` separators.

## 4. Install recipe (zip already in Downloads)
```
cmd /c powershell -NoProfile -Command "Expand-Archive -Force $env:USERPROFILE\Downloads\TestING-Update-<Stand>.zip $env:USERPROFILE\Downloads\rel-<sha8>"
start "" /min cmd /c "%USERPROFILE%\Downloads\rel-<sha8>\TestING-Update-<Stand>\INSTALLIEREN.cmd -Unattended > %USERPROFILE%\Downloads\install-<sha8>.txt 2>&1"
```
Proof = `%LOCALAPPDATA%\ING-Testautomatisierung\VERSION.json` version + newest `protokolle\installation-*.log` ending `Fertig. Alles geprueft und in Ordnung.`

## 5. Studio processes
- javaw without an INGenious window -> `taskkill /f /pid`. Visible Studio window -> one Telegram line, wait.
- Hidden powershell / Keep-Awake / devtunnel / scheduled task found: report on Telegram, never add new ones.

## 6. Lanes
- Lanes never touch the laptop. Lanes commit+push as soon as the primary harness is green.
- Opus lanes on large tools changes (ado-upload.mjs) can hit their request budget mid-work: tell them to commit and push after every point, and check the pushed branch (`git ls-remote upstream <branch>`) before re-dispatching.
