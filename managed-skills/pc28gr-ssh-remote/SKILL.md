---
name: pc28gr-ssh-remote
description: "How to drive the ING work laptop PC28GR over SSH/scp correctly: alias and port, scp -O, quoting through bash/ssh/cmd/PowerShell, why processes die on disconnect and how to keep them alive, screenshots, log locations, which OneDrive folder is the real team library, and the forbidden patterns"
---

# PC28GR over SSH — the recipes that actually work

The work laptop **PC28GR** is reachable as the SSH alias `pc28gr` (127.0.0.1 port **2222**, a
forwarded tunnel). SSH and `scp` are explicitly allowed (operator correction 2026-09-15: the
2026-09-11 security incident concerned only the VBS/scheduled-task starters, not SSH). Every
lane so far has burned turns on the same problems: quoting, `scp -O`, processes dying on
disconnect, screenshots of black console windows, and staging files into the wrong OneDrive
folder. All are solved below.

Everything here was measured on the machine, not inferred.

## New work PC (replaces PC28GR) — state 2026-10-02, route NOT yet proven

The operator's new work PC gets access **only** through a VS Code Remote Tunnel that the
operator starts by hand in a visible cmd window: `code tunnel --accept-server-license-terms
--name <name>` signed in with **GitHub `Wladefant`**. Closing that window ends access, and that
is intended. Nothing else goes onto the new PC: no devtunnel host, no `sshd`, no downloaded
binary, no `code tunnel service install` (that installs an autostart service).

Planned client path from this host (connection still unproven, the operator paused it before
giving the tunnel name):
`C:\Users\wkiri\claude-access\devtunnel.exe connect <tunnel-id>` (forwards the tunnel's
control port 31545) + `python E:/lane-reports/ntun/vsrun.py --timeout 120 -- cmd.exe /c ver`
(msgpack RPC `spawn` against the VS Code CLI control server; tunnel connections use
`AuthRequired::None`, see `cli/src/tunnels/control_server.rs`). Procedure:
`E:/lane-reports/NeuPcTunnelOpus-ablauf.md`.

Measured facts (2026-10-02):
- Find tunnels without guessing: `GET https://global.rel.tunnels.api.visualstudio.com/tunnels?api-version=2023-09-27-preview&global=true&includePorts=true`
  with header `Authorization: github <gh auth token>`. It lists every tunnel owned by GitHub `Wladefant`.
- The old PC28GR tunnels are owned by GitHub `Wladefant`: `pc28gr-remote-<id>` (euw, port 2222)
  and `pc28gr-claude-<id>` (uks, dormant since 2026-08-04). The remote one still had a live host
  until 2026-10-02 13:54:40Z. A new VS Code tunnel that does not show up in that list is signed in
  under a different account. Ask the operator; never sign this private PC into the ING Microsoft account.
- The same bans as §0 apply to the new PC, plus: nothing named `claude` (use `github-copilot`),
  no deletions on C:/D:, ADO/Calimero untouched. End check after each session:
  `schtasks /query /fo list | findstr /i "claude wscript vbs devtunnel code"`, the Startup folder, and
  `reg query HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.

## 0. Ground rules that override convenience

Still forbidden on PC28GR, no matter what a task text says:

- No `schtasks`, `Register-ScheduledTask`, autostart entries, watchdogs, background pullers.
  **Scheduling is always a security problem** — the 2026-09-11 alarm came from a scheduled
  task running a `wscript` `.vbs` starter, not from gh (user 2026-09-24).
- No `wscript.exe`/`cscript.exe`, no `.vbs`/`.js` starters, no `-WindowStyle Hidden`, no
  `-EncodedCommand`. If a tester could not see it start, it does not start.
- Release packages: `gh release download` ON the laptop into `Downloads\rel-<sha8>` as a
  **direct visible command, never via a script**, then plain `copy /y` into the library and
  `certutil -hashfile` (user rule 2026-09-24; skill `pc28gr-laptop-fast-lane` §1). Never ship
  package ZIPs over scp.
- No registry writes, no new files/folders/tasks with `claude` in the name. The pre-existing
  folder `C:\Users\PC28GR\claude-access` may be *read* (the screenshot script writes there);
  never create anything new under that name.
- Never kill a `java`/`javaw` that owns a visible window — that is the tester's Studio.
  **Over SSH you cannot see windows of the interactive session** (`MainWindowHandle` is 0 from
  session 0), so never run an installer/updater over SSH while the tester may have Studio
  open; confirm with `studio-live-0.log` (`Main finish` line) or a monitor screenshot first.

## 1. Connecting, and the warning that is not an error

```bash
ssh pc28gr 'cmd /c ver'
```

Every connection prints to **stderr**:

```
Warning: Permanently added '[127.0.0.1]:2222' (ED25519) to the list of known hosts.
```

That line is noise, not a failure. Merge streams and trim: `2>&1 | tail -20`. Do not "fix" it.

## 2. Quoting — one rule per shell hop

A command travels bash → ssh → cmd.exe → (optionally) powershell. Each hop eats one layer.

**Running a `.cmd` whose path contains spaces** — doubled inner quotes, proven repeatedly:

```bash
ssh pc28gr 'cmd /c ""C:\Users\PC28GR\OneDrive - ING\Testmanagement-Service (GRDE601146) - TestING\Installation\TestING-Installer.cmd"" -Unattended'
```

**Never inline non-trivial PowerShell.** It looks like it works and then silently corrupts
paths: a `"…\"` at the end of an argument loses its backslash, and
`$env:LOCALAPPDATA + "ING-Testautomatisierung\"` + `"protokolle"` became
`…ING-Testautomatisierungprotokolle` — `Get-ChildItem` then reports a path that "does not
exist" and you debug the wrong machine. A bare `Kandidat` word inside a nested quote was even
executed as a command name. `ssh pc28gr powershell -Command "... | Select-String ..."` fails
with `'Select-String' is not recognized` because cmd splits the pipe.

**Do this instead — write the script locally, ship it, run it by path:**

```bash
# 1. author it locally with the write tool, e.g. .tmp/Beweis.ps1
scp -O Beweis.ps1 'pc28gr:C:/Users/PC28GR/Downloads/Beweis.ps1'
ssh pc28gr 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\PC28GR\Downloads\Beweis.ps1 -Fenster'
```

`Downloads` is the right home for it (it is the tester's own path); `%TEMP%` is not — processes
running out of `%TEMP%` look like malware. Inside the `.ps1` use `Join-Path` and `-LiteralPath`
and you never fight quoting again.

**Exit codes: `%ERRORLEVEL%` on the same command line is a lie.**

```bash
# WRONG — cmd expands %ERRORLEVEL% while parsing the line, so it prints the value from before
ssh pc28gr 'cmd /c "thing.cmd & echo EXIT=%ERRORLEVEL%"'
# RIGHT
ssh pc28gr 'cmd /c ""C:\path\thing.cmd"" && echo ERGEBNIS-OK || echo ERGEBNIS-FEHLER'
```

## 3. `scp` — always `-O`, forward slashes, no inner double quotes

The laptop's SCP needs the legacy protocol: **`scp -O`**. Without it, or with a remote path
wrapped in inner double quotes, you get `protocol error: filename does not match request`.

```bash
# works (single quotes around the whole remote arg, forward slashes, no inner ")
scp -O 'pc28gr:C:/Users/PC28GR/claude-access/screenshots/window-20260915-220146.png' studio.png
scp -O 'pc28gr:C:/Users/PC28GR/claude-access/screenshots/window-*.png' .        # globs are fine
scp -O Beweis.ps1 'pc28gr:C:/Users/PC28GR/Downloads/Beweis.ps1'
# scp ONLY for small script files (user rule 2026-09-24). Never packages, ZIPs, json or other
# data — releases are fetched with a direct gh release download on the laptop itself.

# fails: protocol error: filename does not match request
scp -O 'pc28gr:"C:/Users/PC28GR/AppData/Local/ING-Testautomatisierung/VERSION.json"' out.json
```

**Paths with spaces** (the OneDrive library is full of them) are the one case where `scp` is not
worth the fight. Two reliable ways around it:

```bash
# text files: stream them through ssh instead of scp
ssh pc28gr 'cmd /c type "C:\Users\PC28GR\AppData\Local\ING-Testautomatisierung\VERSION.json"' > VERSION.json

# binaries (release packages): direct `gh release download` on the laptop into
# Downloads\rel-<sha8>, then plain `copy /y` into the library + `certutil -hashfile` — no script
```

## 3b. Which OneDrive folder is the real team library (measured 2026-09-16)

The tester library is SharePoint site `GRDE601146_FS`, `Shared Documents/02 Releases/
Testautomaten/TestING` (siteId `bd46dfc0-…`, listId `af4085f3-…`, see
`tools/paket/TestING-sync.txt`). On the laptop it can appear under **different local roots
depending on how the operator connected it**, and the resolvers (`tools/ablage.mjs`,
`update-folder.ps1`, `TestING-Installer.cmd`) pick the first match — so check before staging:

```powershell
Get-ChildItem 'HKCU:\Software\SyncEngines\Providers\OneDrive' | % { $p = Get-ItemProperty $_.PSPath; "$($p.MountPoint) => $($p.UrlNamespace)" }
```

- `UrlNamespace` starting `https://ing.sharepoint.com/sites/GRDE601146_FS` → **team library, use it**.
  Current root (direct sync of the TestING folder):
  `C:\Users\PC28GR\OneDrive - ING\Testmanagement-Service (GRDE601146) - TestING\` with
  `Updates\`, `Installation\`, `Basispaket\`, `Belege\`, `Problemmeldungen\`.
- `UrlNamespace` under `ing-my.sharepoint.com/personal/…` → the operator's personal drive. A
  folder there named like the library is either an OneDrive **shortcut** ("Verknuepfung zu
  Meine Dateien", jumpLinkType 7 in `SyncEngineDatabase.db`; files DO reach the team library,
  the personal URL is only the alias) or an **orphan** left after "Synchronisierung beenden" /
  shortcut removal. Edge case seen 2026-09-15/16: assets staged into the shortcut folder
  vanished when the operator removed the shortcut; the real library still showed the previous
  `latest.json`. Re-stage from the local build output and verify with `Get-FileHash`.
- Old roots that still exist and are NOT targets: `…\OneDrive - ING\ING-Testautomatisierung-ALT`,
  `C:\Users\PC28GR\ING\Testmanagement-Service - Dokumente` (disconnected 2026-09-15 10:21).
- File attribute `0x420` (Reparse+Archive) = local and synced; `0x401020`/`0x1020` with the
  Offline bit = online-only placeholder, content not on disk.

## 4. Disconnect kills everything you started — the single biggest trap

**Windows OpenSSH terminates the whole process tree when the SSH session ends**, including
grandchildren that `start` detached. Measured: `cmd /c start "" STUDIO-STARTEN.cmd` reported
`Studio lauft (PID 33452)`, the ssh call returned, and 75 s later there was no `javaw` at all
and no window.

Consequences, and what to do:

- **A foreground call is safe.** An installer that finishes inside one `ssh` invocation is fine
  — just give the harness tool a generous timeout (a full TestING install is ~90 s; allow
  2400 s for the slow path, and `backgroundAfter` if you want the turn back).
- **Anything that must outlive the command needs the session held open.** Launch it with a
  supervised process and keep the connection alive:

  ```
  launch op=start name=studio-halt application=ssh args=[
    "pc28gr",
    "cmd /c \"\"C:\\Users\\PC28GR\\AppData\\Local\\ING-Testautomatisierung\\STUDIO-STARTEN.cmd\"\" & ping -n 3000 127.0.0.1 >nul"
  ] ready={"log":"Studio lauft","timeout":120}
  ```

  `ping -n 3000 127.0.0.1 >nul` is a visible, boring, ~50-minute keep-alive — no scheduled task,
  no hidden window, nothing named claude. The GUI lives exactly as long as that lane lives.
- **Do not "fix" this with autostart or a scheduled task.** If the tester should keep the app,
  tell the human to double-click the desktop shortcut; that is the only permanent starter.
- `cmd /c` under `sshd` is a **non-hidden** process, so it satisfies a "visible console run"
  requirement even though you started it remotely.
- When the tester closes the Studio window, the `javaw` may linger without a window (seen
  2026-09-15 23:08 → still alive 00:17). The installer's headless-kill of such a process is
  correct; a `Main finish` line in `studio-live-0.log` is the proof the window was closed.

## 5. Screenshots — look at GUIs, never at consoles

The script lives on the laptop:
`C:\Users\PC28GR\ing-qa-automation\tools\remote\Take-Screenshot.ps1`

```bash
ssh pc28gr 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\PC28GR\ing-qa-automation\tools\remote\Take-Screenshot.ps1 -ListWindows'
ssh pc28gr 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\PC28GR\ing-qa-automation\tools\remote\Take-Screenshot.ps1 -Window "INGenious Playwright Studio"'
ssh pc28gr 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\PC28GR\ing-qa-automation\tools\remote\Take-Screenshot.ps1 -Monitor 0'
```

- Last output line is machine-readable: `RESULT|<path>|<bytes>|<WxH>|<mode>` or
  `ERROR|<CODE>|<msg>`. Exit codes: 0 ok, 2 no display, 3 window not found, 4 window
  **ambiguous**, 5 capture failed, 6 bad args, 7 screen locked.
- PNGs land in `C:\Users\PC28GR\claude-access\screenshots\`. Fetch with `scp -O` (see §3) and
  then **actually inspect the image** — a file that exists proves nothing.
- `WINDOW_AMBIGUOUS` (two windows share the title, e.g. two `TestING einrichten` consoles):
  either make the substring longer or fall back to `-Monitor 0`.
- **The SSH session sees the real interactive desktop.** `-ListWindows` from SSH returned the
  physical monitor (1920x1200) and the user's Edge/Teams/Explorer windows, and a `javaw`
  started over SSH rendered a full window there (`Calimero - INGenious Playwright Studio 3.0.0`,
  1920x1152, panel completely drawn). There is no headless-desktop problem for screenshots —
  only `Process.MainWindowHandle` is session-blind (see §0).
- **A console started over SSH screenshots as a black rectangle.** Its stdout goes to the SSH
  pipe, not to the window buffer — the window exists and carries the title, the interior is
  empty. Never try to prove console output with a picture: keep the terminal text and quote
  the log file. Pictures are for GUIs.

### 5a. "Look at this" = the tester's own last screenshot (verified recipe 2026-09-24)
When the user says "this is not good" / "look at this" without an attachment, it refers to his
newest Win+Shift+S screenshot. `tools/remote/` is deleted (CLAUDE.md rule 5); use this 3-call recipe,
nothing else:
```bash
# 1. newest 3 files (escape $ as \$ inside the bash double-quoted string)
ssh pc28gr "powershell -NoProfile -Command \"Get-ChildItem 'C:\Users\PC28GR\OneDrive - ING\Bilder\Screenshots' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 3 | ForEach-Object { \$_.LastWriteTime.ToString('HH:mm:ss') + '|' + \$_.Length + '|' + \$_.Name }\""
# 2. copy to a space-free name (PowerShell -LiteralPath with single quotes), scp it, delete the copy
ssh pc28gr "powershell -NoProfile -Command \"Copy-Item -LiteralPath 'C:\Users\PC28GR\OneDrive - ING\Bilder\Screenshots\<NAME>.png' -Destination C:\Users\PC28GR\Downloads\s3.png\"" && scp -O pc28gr:C:/Users/PC28GR/Downloads/s3.png E:/tmp-screens/<name>.png && ssh pc28gr "cmd /c del C:\Users\PC28GR\Downloads\s3.png"
# 3. inspect_image on the local file
```

### 5b. CLI mistakes already made — never repeat (logged 2026-09-24)
| Mistake | Symptom | Do instead |
|---|---|---|
| `ssh pc28gr "powershell -Command -" < local.ps1` | silently prints nothing | inline `-Command "..."` (short) or scp the small .ps1 + `-File` |
| `scp -O "pc28gr:C:/…/OneDrive - ING/…png"` | splits at spaces: `No such file or directory` ×5 | copy to a space-free path first (5a step 2) |
| `scp -O "pc28gr:\"C:/… with spaces…\""` | `protocol error: filename does not match request` | same: copy to a space-free path first |
| `cmd /c copy /y ""C:\…OneDrive - ING\…"" dest` via bash→ssh | `The system cannot find the file specified` | PowerShell `Copy-Item -LiteralPath '…'` (single quotes survive all hops) |
| local `powershell -Command "… \$_.Line …"` through the veyyon bash tool | `$_` eaten → `.Line is not recognized` ×N | local files: use the `search`/`read` tools, never local PowerShell one-liners |
| `findstr` with several `/c:` for a log slice | output too large, gets compacted away | `powershell Get-Content <log> -Tail 15` + `Select-String -Pattern` with one regex |
| inline ssh PowerShell with `-replace` + backtick escapes (`` `$1`n ``) or nested `\"` | ParserError `Missing expression after ','` | anything beyond one pipeline: write a small .ps1 locally, `scp -O` to Downloads, `-File`, then `del` (worked 2026-09-24, E:/tmp-screens/StartAdresse.ps1) |
| Select-String output of long log lines | veyyon truncates each line at ~130 chars (`…`), the value you need is cut | in the .ps1, strip the prefix and wrap the rest into 90-char chunks |
| starting a lane without re-reading these rows | same error again next session | read §2, §3, §5a, §5b before the first ssh call of a session |

## 6. Where the truth is written (read logs before reading code)

| What | Path on PC28GR |
|---|---|
| Installer runs (the `.cmd`) | `%LOCALAPPDATA%\ING-Testautomatisierung\protokolle\installer-*.log` |
| Setup phase (`INSTALLIEREN.ps1`) | `%LOCALAPPDATA%\ING-Testautomatisierung\protokolle\installation-*.log` |
| Studio/plugin live (JUL) | `%LOCALAPPDATA%\ING-Testautomatisierung\protokolle\studio-live-0.log` — exists only when Studio was started via `STUDIO-STARTEN.cmd` |
| Every ADO upload attempt | `C:\Users\PC28GR\ingenious\companion-logs\ado-upload.log` + one JSON receipt per try |
| Installed state | `%LOCALAPPDATA%\ING-Testautomatisierung\VERSION.json` |
| Panel state | `%LOCALAPPDATA%\IngQaAutopilot\` |

Always start Studio through `STUDIO-STARTEN.cmd`; a bare `ingenious.bat`/`javaw` writes no live
log and the next diagnosis is blind.

Fetching logs is a text stream, so use §3's `type` form:

```bash
ssh pc28gr 'cmd /c dir /b /o-d "C:\Users\PC28GR\AppData\Local\ING-Testautomatisierung\protokolle\installer-*.log"' | head -3
ssh pc28gr 'cmd /c type "C:\Users\PC28GR\AppData\Local\ING-Testautomatisierung\protokolle\installer-20260915-215126.log"' > installer.log
```

## 6b. A running Studio cannot be closed from SSH — plan for the human (measured 2026-09-20)

`INSTALLIEREN.ps1` refuses to install over an open Studio (exit 2), and an open Studio on
PC28GR **cannot be ended from a script**. Measured against `javaw` PID 10384 (window
`TestING - INGenious Playwright Studio 3.0.0 (Open Source)`), all three graceful routes were
sent and none ended the process:

```
PostMessage WM_CLOSE                       -> beendet=False after 60 s
SendMessageTimeout WM_SYSCOMMAND/SC_CLOSE  -> beendet=False after 30 s
[Process]::CloseMainWindow()               -> beendet=False after 30 s
'Main finish' in studio-live-0.log: 54, unchanged (no shutdown happened)
```

What actually happens: Studio **answers** the close request with a modal window that waits for
a human — `Quit INGenious Studio / Do you want to save the Project before quitting?` with
`[Cancel] [Don't Save] [Save & Quit]`. It is a second top-level window of the same PID (listed
as `titel='Quit INGenious Studio'`, 520x200) and it is **not operable by program**:

```
Wurzel: 'TestING - INGenious Playwright Studio 3.0.0 (Open Source)' (ControlType.Window)
Nachkommen sichtbar fuer UIA: 1
  [ControlType.Pane] 'Quit INGenious Studio'
```

UI Automation sees the frame and no buttons (Java without Access Bridge), keystroke/click
synthesis is forbidden (§0), and killing a `javaw` that owns a window is forbidden too. So:

- **Ask the operator BEFORE touching a running Studio**, in the same message that names the
  package path and the double-click recipe — not after you have opened a modal dialog he must
  now clear. Sending the close request leaves his Studio blocked until somebody clicks.
- A laptop lane that finds Studio open reports the install as **"wartet auf Bediener-Klick"**,
  leaves the package unpacked in `Downloads\rel-<sha8>\`, and ships a wait-then-install `.ps1`
  so the rest runs unattended after the click.
- Substitute proof meanwhile: the offline harnesses
  `tools/paket/test-installer-update-ohne-java.ps1` and
  `tools/paket/test-installer-laufzeit-skip.ps1` reproduce the update-without-`java\` case on
  any machine and print per-line `[ OK ]` verdicts. Label them harness, never laptop proof.

## 6c. Two desktop traps on PC28GR (measured 2026-09-20)

- **The desktop is OneDrive-redirected.** `C:\Users\PC28GR\Desktop\*.lnk` answers
  `File Not Found`; the tester's shortcuts (`TestING.lnk`, `TestING aktualisieren.lnk`,
  `TestING - Problem melden.lnk`) live in `C:\Users\PC28GR\OneDrive - ING\Desktop`. Look there
  before claiming the installer created no shortcut, and write that path into tester docs.
- **A full-desktop screenshot is usually useless** — the operator's cmd/HP windows cover the
  icons. Open the folder instead: `cmd /c start "" explorer.exe "<path>"` does nothing over
  SSH, but `(New-Object -ComObject Shell.Application).Open('<path>')` from a shipped `.ps1`
  opens a real window (class `CabinetWClass`) that screenshots cleanly with `-Window "Desktop"`.
- **Never broadcast `WM_CLOSE` to every `CabinetWClass` window.** Explorer on Windows 11 is
  tabbed: the message closed the active **tab** of the operator's own window (title went from
  `Wladimir - ING and 1 more tab` to `Wladimir - ING`). Remember the hwnd you opened and close
  only that one.

## 7. Five-minute checklist for a laptop lane

1. `ssh pc28gr 'cmd /c ver'` — connection up, ignore the known-hosts warning.
2. `Get-Process java,javaw` via a shipped `.ps1` — is a tester's Studio open? If it has a
   window, leave it alone and do not install over it (file locks). Remember the window is
   invisible to SSH: use the live log / screenshot to decide. Open Studio means the install
   needs the operator's hands — read §6b before you send a single close message.
3. Ship any non-trivial PowerShell as a `.ps1` to `C:/Users/PC28GR/Downloads/`; never inline it.
4. Resolve the team-library root from the registry (§3b) before staging anything.
5. Run the real thing in the foreground with a long timeout; use `&& echo OK || echo FEHLER`
   for the verdict, never `%ERRORLEVEL%` on the same line.
6. Need something to stay alive (Studio, a server)? Hold the session with the `launch` +
   `ping -n` recipe from §4.
7. Pull logs with `type`, pull PNGs with `scp -O`, and look at the pictures.
