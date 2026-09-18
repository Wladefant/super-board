# veyyon-telegram-daemon.ps1 — start, stop and inspect the standalone Telegram bot daemon.
#
# The daemon is what keeps an opted-in bot answering when no Veyyon session is
# open. It is a long-lived `bun daemon/main.ts run`, so on Windows it needs a
# launcher that survives the shell that started it: `Start-Process` creates an
# independent process rather than a child bound to this console's lifetime.
#
# Paths are relative to the INSTALLED tree: the installer places this file at
# ~/.veyyon/telegram/ (beside manifest.json), with the daemon modules under
# ~/.veyyon/telegram/daemon/. Run it from there, not from the source package.
#
# Verbs:
#   start         Launch the daemon detached, appending stdout and stderr to daemon.log.
#   stop          Ask the running daemon to release its leases and exit.
#   status        Print the daemon's own `status` output, including its status JSON.
#   check         Report whether this machine is configured (slots opted in, GUI host found).
#   install-task  Register a Scheduled Task that runs `start` at logon.
#   remove-task   Unregister that Scheduled Task.
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "status", "check", "install-task", "remove-task", "help")]
    [string]$Command = "status",

    # Endpoint the daemon should drive sessions through. Without it the daemon
    # falls back to its own discovery: VEYYON_GUI_HOST_ENDPOINT, then
    # gui-host.endpoint under the active profile's agent dir, then ~/.veyyon.
    [string]$Endpoint,

    # Seconds `start` waits for the daemon to publish a live pid before reporting.
    [int]$StartTimeoutSeconds = 30,

    [string]$TaskName = "VeyyonTelegramDaemon"
)

$ErrorActionPreference = "Stop"

$DaemonEntry = Join-Path $PSScriptRoot "daemon\main.ts"
# The daemon's own log, which the daemon appends to itself.
$LogPath = Join-Path $PSScriptRoot "daemon.log"
# Where the detached wrapper's stdout and stderr go. A separate file and NOT
# daemon.log: a PowerShell redirect holds the file exclusively on Windows, so the
# daemon's appends to a redirected daemon.log failed silently and it stayed empty
# for the process's whole lifetime. This one catches what happens before the
# daemon can log at all — a bun module-resolution error on a bad install.
$StartupLogPath = Join-Path $PSScriptRoot "daemon.startup.log"
# The daemon reads VEYYON_TELEGRAM_DAEMON_DIR for its own run dir, so a launcher
# that assumed the default would wait out its whole timeout looking for a pid
# file the daemon was writing somewhere else.
$RunDir = if ($env:VEYYON_TELEGRAM_DAEMON_DIR) {
    $env:VEYYON_TELEGRAM_DAEMON_DIR
} else {
    Join-Path $PSScriptRoot "daemon\run"
}
$DaemonPidPath = Join-Path $RunDir "daemon.pid"
$LauncherPidPath = Join-Path $RunDir "launcher.pid"

function Resolve-VeyyonBun {
    $bunCmd = Get-Command bun.exe, bun -CommandType Application -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -and (Test-Path -PathType Leaf $_.Source) } |
        Select-Object -First 1
    if ($bunCmd) {
        return $bunCmd.Source
    }
    $candidates = @(
        "$HOME\.bun\bin\bun.exe",
        "$env:LOCALAPPDATA\bun\bin\bun.exe",
        "C:\Users\$env:USERNAME\.bun\bin\bun.exe"
    )
    foreach ($cand in $candidates) {
        if ($cand -and (Test-Path -PathType Leaf $cand)) {
            return $cand
        }
    }
    return $null
}

function Get-LivePid([string]$pidPath) {
    if (-not (Test-Path -PathType Leaf $pidPath)) {
        return $null
    }
    $recorded = (Get-Content -Raw -Path $pidPath -ErrorAction SilentlyContinue).Trim()
    $parsed = 0
    if (-not [int]::TryParse($recorded, [ref]$parsed) -or $parsed -le 0) {
        return $null
    }
    if (Get-Process -Id $parsed -ErrorAction SilentlyContinue) {
        return $parsed
    }
    return $null
}

function Invoke-DaemonVerb([string]$verb) {
    $bun = Resolve-VeyyonBun
    if (-not $bun) {
        Write-Host "Could not find the bun executable; the daemon runs on bun." -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path -PathType Leaf $DaemonEntry)) {
        Write-Host "Daemon entrypoint missing: $DaemonEntry. Run scripts/install-telegram-harness.py." -ForegroundColor Red
        exit 1
    }
    # Captured into a variable, then printed with Write-Host: a function's return
    # value in PowerShell is everything it emitted, so `& $bun ...` writing to the
    # pipeline made this return the daemon's output lines with the exit code last,
    # and `exit (Invoke-DaemonVerb "check")` exited 0 for a daemon that reported
    # 78 while printing nothing at all. Piping into ForEach-Object fixed the
    # printing but not the code: $LASTEXITCODE read after a piped native command
    # was 0, so `check` still exited 0 on a daemon exiting 78. Assigning the call
    # ends it before anything else can move $LASTEXITCODE.
    #
    # $ErrorActionPreference is "Stop" for this script, and under it a native
    # command's stderr line becomes a NativeCommandError record: the daemon's own
    # diagnosis ("No slot opted in...") came back wrapped in a PowerShell error
    # naming a line in this launcher, which reads like the launcher broke. Stderr
    # from the daemon is output, not a PowerShell failure.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $bun $DaemonEntry $verb 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
    foreach ($line in $output) { Write-Host $line }
    return $code
}

function Start-Daemon {
    $bun = Resolve-VeyyonBun
    if (-not $bun) {
        Write-Host "Could not find the bun executable; the daemon runs on bun." -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path -PathType Leaf $DaemonEntry)) {
        Write-Host "Daemon entrypoint missing: $DaemonEntry. Run scripts/install-telegram-harness.py." -ForegroundColor Red
        exit 1
    }

    $running = Get-LivePid $DaemonPidPath
    if ($running) {
        Write-Host "Telegram daemon already running (pid $running)." -ForegroundColor Yellow
        return 0
    }

    if ($Endpoint) {
        $env:VEYYON_GUI_HOST_ENDPOINT = $Endpoint
    }
    # The daemon appends its own lines to this exact file, so it and this launcher
    # agree on one log wherever this tree lives.
    $env:VEYYON_TELEGRAM_DAEMON_LOG = $LogPath

    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

    # The wrapper is powershell and not `cmd /c`: `Start-Process` re-quotes each
    # -ArgumentList element, and a cmd command line carrying nested quotes plus
    # `>>` and `2>&1` arrived mangled, so nothing ran at all and the launcher
    # reported the daemon as exited over an empty log. PowerShell's `*>>` merges
    # every stream into one file and the command survives one round of re-quoting.
    $inner = "& '{0}' '{1}' run *>> '{2}'" -f $bun, $DaemonEntry, $StartupLogPath
    $launcher = Start-Process -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $inner `
        -WorkingDirectory $PSScriptRoot `
        -WindowStyle Hidden -PassThru
    Set-Content -Path $LauncherPidPath -Value $launcher.Id -Encoding ascii

    $deadline = (Get-Date).AddSeconds($StartTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $daemonPid = Get-LivePid $DaemonPidPath
        if ($daemonPid) {
            Write-Host "Telegram daemon started (pid $daemonPid, launcher $($launcher.Id))." -ForegroundColor Green
            Write-Host "Log: $LogPath"
            return 0
        }
        if (-not (Get-Process -Id $launcher.Id -ErrorAction SilentlyContinue)) {
            Write-Host "Daemon exited before it started polling. Last lines of ${StartupLogPath}:" -ForegroundColor Red
            Get-Content -Path $StartupLogPath -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" }
            return 1
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "Daemon did not publish a pid within $StartTimeoutSeconds s. Logs: $LogPath, $StartupLogPath" -ForegroundColor Red
    return 1
}

function Stop-Daemon {
    $code = Invoke-DaemonVerb "stop"
    # The daemon's own `stop` signals the poller so leases are released; the cmd.exe
    # wrapper exits with it. A wrapper that outlived the daemon is reaped here so a
    # later `start` is not blocked by a stale launcher.
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-LivePid $DaemonPidPath)) { break }
        Start-Sleep -Milliseconds 500
    }
    $launcherPid = Get-LivePid $LauncherPidPath
    if ($launcherPid -and -not (Get-LivePid $DaemonPidPath)) {
        Stop-Process -Id $launcherPid -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -Path $LauncherPidPath -Force -ErrorAction SilentlyContinue
    return $code
}

function Install-DaemonTask {
    $self = Join-Path $PSScriptRoot "veyyon-telegram-daemon.ps1"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$self`" start"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "Keeps the Veyyon Telegram bot daemon polling so opted-in bots answer with no session open." `
        -Force | Out-Null
    Write-Host "Registered Scheduled Task '$TaskName' to run at logon for $env:USERNAME." -ForegroundColor Green
    return 0
}

function Remove-DaemonTask {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "Removed Scheduled Task '$TaskName'." -ForegroundColor Green
    return 0
}

switch ($Command) {
    "start" { exit (Start-Daemon) }
    "stop" { exit (Stop-Daemon) }
    "restart" {
        Stop-Daemon | Out-Null
        exit (Start-Daemon)
    }
    "status" { exit (Invoke-DaemonVerb "status") }
    "check" { exit (Invoke-DaemonVerb "check") }
    "install-task" { exit (Install-DaemonTask) }
    "remove-task" { exit (Remove-DaemonTask) }
    default {
        Write-Host "Veyyon Telegram bot daemon" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "  .\veyyon-telegram-daemon.ps1 start [-Endpoint tcp:127.0.0.1:7654]"
        Write-Host "  .\veyyon-telegram-daemon.ps1 stop"
        Write-Host "  .\veyyon-telegram-daemon.ps1 restart"
        Write-Host "  .\veyyon-telegram-daemon.ps1 status"
        Write-Host "  .\veyyon-telegram-daemon.ps1 check"
        Write-Host "  .\veyyon-telegram-daemon.ps1 install-task [-TaskName VeyyonTelegramDaemon]"
        Write-Host "  .\veyyon-telegram-daemon.ps1 remove-task"
        Write-Host ""
        Write-Host "Log: $LogPath"
        exit 0
    }
}
