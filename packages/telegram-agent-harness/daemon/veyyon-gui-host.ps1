# veyyon-gui-host.ps1 — start, stop and inspect the Veyyon GUI engine host server.
#
# The GUI host is what serves the desktop JSON wire protocol on tcp:127.0.0.1:7699
# for Veyyon desktop clients and the Telegram daemon. On Windows it runs as
# `veyyon.exe gui tcp:127.0.0.1:7699` detached via Start-Process so it survives
# the shell that started it.
#
# Verbs:
#   start         Launch the GUI host detached, appending output to gui-host.startup.log.
#   stop          Stop the running GUI host managed by this launcher.
#   restart       Stop and restart the GUI host.
#   status        Report whether the GUI host is running and port 7699 is listening.
#   check         Report configured paths, executable availability, and port status.
#   install-task  Register a Scheduled Task that runs `start` at logon.
#   remove-task   Unregister that Scheduled Task.
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "restart", "status", "check", "install-task", "remove-task", "help")]
    [string]$Command = "status",

    # Endpoint the GUI host should listen on or connect to.
    [string]$Endpoint = "tcp:127.0.0.1:7699",

    # Seconds `start` waits for the GUI host to bind its port before reporting.
    [int]$StartTimeoutSeconds = 30,

    [string]$TaskName = "VeyyonGuiHost"
)

$ErrorActionPreference = "Stop"

$HostAddress = "127.0.0.1"
$Port = 7699
if ($Endpoint -and $Endpoint -match "tcp:([^:]+):(\d+)") {
    $HostAddress = $matches[1]
    $Port = [int]$matches[2]
}

$StartupLogPath = Join-Path $PSScriptRoot "gui-host.startup.log"
$RunDir = if ($env:VEYYON_TELEGRAM_DAEMON_DIR) {
    $env:VEYYON_TELEGRAM_DAEMON_DIR
} else {
    Join-Path $PSScriptRoot "daemon\run"
}
$PidPath = Join-Path $RunDir "gui-host.pid"
$LauncherPidPath = Join-Path $RunDir "gui-host-launcher.pid"

function Resolve-VeyyonExe {
    if ($env:VEYYON_EXE -and (Test-Path -PathType Leaf $env:VEYYON_EXE)) {
        return $env:VEYYON_EXE
    }
    $cmd = Get-Command veyyon.exe, veyyon -CommandType Application -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -and (Test-Path -PathType Leaf $_.Source) } |
        Select-Object -First 1
    if ($cmd) {
        return $cmd.Source
    }
    $candidates = @(
        "$env:LOCALAPPDATA\veyyon\veyyon.exe",
        "$HOME\AppData\Local\veyyon\veyyon.exe",
        "C:\Users\$env:USERNAME\AppData\Local\veyyon\veyyon.exe",
        "$env:ProgramFiles\veyyon\veyyon.exe",
        "${env:ProgramFiles(x86)}\veyyon\veyyon.exe"
    )
    foreach ($cand in $candidates) {
        if ($cand -and (Test-Path -PathType Leaf $cand)) {
            return $cand
        }
    }
    return $null
}

function Test-PortOpen([string]$address = "127.0.0.1", [int]$targetPort = 7699) {
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect($address, $targetPort, $null, $null)
        $wait = $async.AsyncWaitHandle.WaitOne(1000, $false)
        if (-not $wait) {
            $client.Close()
            return $false
        }
        $client.EndConnect($async)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

function Get-LivePid([string]$filePath) {
    if (-not (Test-Path -PathType Leaf $filePath)) {
        return $null
    }
    $recorded = (Get-Content -Raw -Path $filePath -ErrorAction SilentlyContinue)
    if ($recorded) { $recorded = $recorded.Trim() }
    $parsed = 0
    if (-not [int]::TryParse($recorded, [ref]$parsed) -or $parsed -le 0) {
        return $null
    }
    if (Get-Process -Id $parsed -ErrorAction SilentlyContinue) {
        return $parsed
    }
    return $null
}

function Get-ProcessCommandLine([int]$pidToInspect) {
    try {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $pidToInspect" -ErrorAction SilentlyContinue
        if ($cim -and $cim.CommandLine) {
            return $cim.CommandLine
        }
    } catch {}
    return $null
}

function Test-GuiHostCommandLine([string]$cmdLine, [string]$targetEndpoint) {
    if (-not $cmdLine) { return $false }
    $normCmd = $cmdLine.Replace('\', '/').ToLowerInvariant()
    $endpointPattern = if ($targetEndpoint) {
        $targetEndpoint.Replace('\', '/').ToLowerInvariant()
    } else {
        "tcp:127.0.0.1:7699"
    }
    return ($normCmd.Contains("gui") -and $normCmd.Contains($endpointPattern))
}

function Find-GuiHostProcessByScan([string]$targetEndpoint) {
    try {
        $candidates = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.ProcessId -ne $PID -and
            $_.Name -notlike "powershell*" -and
            $_.Name -notlike "pwsh*" -and
            $_.Name -notlike "cmd*" -and
            $_.CommandLine -and
            (Test-GuiHostCommandLine $_.CommandLine $targetEndpoint)
        }
        if (-not $candidates) {
            return $null
        }
        $hostProc = $candidates | Select-Object -First 1
        if ($hostProc) {
            return $hostProc.ProcessId
        }
    } catch {}
    return $null
}

function Get-LiveGuiHostPid {
    $foundPid = $null
    $staleFound = $false
    $staleValue = $null

    if (Test-Path -PathType Leaf $PidPath) {
        $recorded = (Get-Content -Raw -Path $PidPath -ErrorAction SilentlyContinue)
        if ($recorded) { $recorded = $recorded.Trim() }
        $parsed = 0
        if ([int]::TryParse($recorded, [ref]$parsed) -and $parsed -gt 0) {
            $proc = Get-Process -Id $parsed -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -notlike "powershell*" -and $proc.ProcessName -notlike "pwsh*" -and $proc.ProcessName -notlike "cmd*") {
                $cmdLine = Get-ProcessCommandLine $parsed
                if ($cmdLine -and (Test-GuiHostCommandLine $cmdLine $Endpoint)) {
                    $foundPid = $parsed
                } else {
                    $staleFound = $true
                    $staleValue = $parsed
                }
            } else {
                $staleFound = $true
                $staleValue = $parsed
            }
        } else {
            $staleFound = $true
            $staleValue = $recorded
        }
    }

    if ($foundPid) {
        return $foundPid
    }

    if ($staleFound) {
        Write-Host "Deleting stale PID file $PidPath (recorded PID '$staleValue' is not a live GUI host process)." -ForegroundColor Yellow
        Remove-Item -Path $PidPath -Force -ErrorAction SilentlyContinue
    }

    $recoveredPid = Find-GuiHostProcessByScan $Endpoint
    if ($recoveredPid) {
        return $recoveredPid
    }

    return $null
}

function Start-GuiHost {
    $runningPid = Get-LiveGuiHostPid
    $portListening = Test-PortOpen $HostAddress $Port
    if ($runningPid -and $portListening) {
        Write-Host "GUI host already running (pid $runningPid, port $Port listening)." -ForegroundColor Yellow
        return 0
    }

    $veyyonExe = Resolve-VeyyonExe
    if (-not $veyyonExe) {
        Write-Host "Could not find the veyyon executable; expected at %LOCALAPPDATA%\veyyon\veyyon.exe or on PATH." -ForegroundColor Red
        return 1
    }

    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

    $inner = "& '{0}' gui '{1}' *>> '{2}'" -f $veyyonExe, $Endpoint, $StartupLogPath
    $launcher = Start-Process -FilePath "powershell.exe" `
        -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $inner `
        -WorkingDirectory $PSScriptRoot `
        -WindowStyle Hidden -PassThru
    Set-Content -Path $LauncherPidPath -Value $launcher.Id -Encoding ascii

    $deadline = (Get-Date).AddSeconds($StartTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $childProc = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
            $_.ParentProcessId -eq $launcher.Id -and
            $_.Name -notlike "powershell*" -and
            $_.Name -notlike "pwsh*" -and
            $_.Name -notlike "cmd*"
        } | Select-Object -First 1

        $detectedPid = if ($childProc) { $childProc.ProcessId } else { Find-GuiHostProcessByScan $Endpoint }
        $isOpen = Test-PortOpen $HostAddress $Port

        if ($detectedPid -and $isOpen) {
            Set-Content -Path $PidPath -Value $detectedPid -Encoding ascii -Force
            Write-Host "GUI host started (pid $detectedPid, port $Port listening)." -ForegroundColor Green
            Write-Host "Startup log: $StartupLogPath"
            return 0
        }

        if (-not (Get-Process -Id $launcher.Id -ErrorAction SilentlyContinue) -and -not $detectedPid) {
            Write-Host "GUI host launcher exited before server bound port. Last lines of ${StartupLogPath}:" -ForegroundColor Red
            Get-Content -Path $StartupLogPath -Tail 20 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $_" }
            return 1
        }
        Start-Sleep -Milliseconds 500
    }

    Write-Host "GUI host did not bind port $Port within $StartTimeoutSeconds s. Logs: $StartupLogPath" -ForegroundColor Red
    return 1
}

function Stop-GuiHost {
    $managedPid = Get-LivePid $PidPath
    $livePid = Get-LiveGuiHostPid

    if (-not $managedPid -and -not $livePid) {
        Write-Host "No running GUI host."
        $launcherPid = Get-LivePid $LauncherPidPath
        if ($launcherPid) {
            Stop-Process -Id $launcherPid -Force -ErrorAction SilentlyContinue
        }
        Remove-Item -Path $LauncherPidPath -Force -ErrorAction SilentlyContinue
        Remove-Item -Path $PidPath -Force -ErrorAction SilentlyContinue
        return 0
    }

    # If the process running is not managed by this launcher (e.g. legacy PID 11752 started externally),
    # do not terminate it without explicit adoption to protect unmanaged services.
    if (-not $managedPid -and $livePid) {
        Write-Host "External GUI host process $livePid is running (unmanaged by this launcher). Not stopping it." -ForegroundColor Yellow
        return 0
    }

    $pidToStop = if ($managedPid) { $managedPid } else { $livePid }
    Stop-Process -Id $pidToStop -Force -ErrorAction SilentlyContinue

    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $pidToStop -ErrorAction SilentlyContinue)) {
            break
        }
        Start-Sleep -Milliseconds 500
    }

    if (Get-Process -Id $pidToStop -ErrorAction SilentlyContinue) {
        Write-Host "GUI host PID $pidToStop did not exit within 15 s; terminating process." -ForegroundColor Yellow
        Stop-Process -Id $pidToStop -Force -ErrorAction SilentlyContinue
    }

    $launcherPid = Get-LivePid $LauncherPidPath
    if ($launcherPid) {
        Stop-Process -Id $launcherPid -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -Path $LauncherPidPath -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $PidPath -Force -ErrorAction SilentlyContinue
    Write-Host "GUI host stopped." -ForegroundColor Green
    return 0
}

function Show-Status {
    $livePid = Get-LiveGuiHostPid
    $portOpen = Test-PortOpen $HostAddress $Port
    if ($livePid -and $portOpen) {
        Write-Host "GUI host: running (pid $livePid, port $Port listening)" -ForegroundColor Green
        return 0
    }
    if ($livePid -and -not $portOpen) {
        Write-Host "GUI host: process running (pid $livePid), but port $Port is not listening" -ForegroundColor Yellow
        return 1
    }
    if ($portOpen) {
        Write-Host "GUI host: port $Port is listening, but no process identified" -ForegroundColor Yellow
        return 0
    }
    Write-Host "GUI host: not running" -ForegroundColor Red
    return 1
}

function Check-GuiHost {
    Write-Host "Veyyon GUI host diagnostic check" -ForegroundColor Cyan
    $exe = Resolve-VeyyonExe
    if ($exe) {
        Write-Host "  Executable: $exe (found)" -ForegroundColor Green
    } else {
        Write-Host "  Executable: not found" -ForegroundColor Red
    }
    Write-Host "  Endpoint: $Endpoint"
    Write-Host "  Port ($Port): $(if (Test-PortOpen $HostAddress $Port) { 'listening' } else { 'closed' })"
    $livePid = Get-LiveGuiHostPid
    if ($livePid) {
        Write-Host "  Process: running (pid $livePid)" -ForegroundColor Green
    } else {
        Write-Host "  Process: not running" -ForegroundColor Yellow
    }
    return 0
}

function Install-GuiHostTask {
    $self = Join-Path $PSScriptRoot "veyyon-gui-host.ps1"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$self`" start"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Description "Starts the Veyyon GUI host server at logon for local desktop and Telegram daemon clients." `
        -Force | Out-Null
    Write-Host "Registered Scheduled Task '$TaskName' to run at logon for $env:USERNAME." -ForegroundColor Green
    return 0
}

function Remove-GuiHostTask {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "Removed Scheduled Task '$TaskName'." -ForegroundColor Green
    return 0
}

switch ($Command) {
    "start" { exit (Start-GuiHost) }
    "stop" { exit (Stop-GuiHost) }
    "restart" {
        $stopCode = Stop-GuiHost
        exit (Start-GuiHost)
    }
    "status" { exit (Show-Status) }
    "check" { exit (Check-GuiHost) }
    "install-task" { exit (Install-GuiHostTask) }
    "remove-task" { exit (Remove-GuiHostTask) }
    default {
        Write-Host "Veyyon GUI engine host launcher" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "  .\veyyon-gui-host.ps1 start [-Endpoint tcp:127.0.0.1:7699]"
        Write-Host "  .\veyyon-gui-host.ps1 stop"
        Write-Host "  .\veyyon-gui-host.ps1 restart"
        Write-Host "  .\veyyon-gui-host.ps1 status"
        Write-Host "  .\veyyon-gui-host.ps1 check"
        Write-Host "  .\veyyon-gui-host.ps1 install-task [-TaskName VeyyonGuiHost]"
        Write-Host "  .\veyyon-gui-host.ps1 remove-task"
        Write-Host ""
        Write-Host "Startup log: $StartupLogPath"
        exit 0
    }
}
