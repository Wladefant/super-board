# herdr_watchdog.ps1
# Restarts the herdr headless server after an ABNORMAL exit, whether or not a client is attached
# (super-board#394; the 2026-10-03 crash left the server down 76 minutes because no client was open).
#
# Run it once a minute from a scheduled task. It keeps a small state file between runs:
#   - server seen up on a check, gone on the next, and its log has no clean `app.shutdown` line
#     for that PID  -> abnormal exit -> restart.
#   - clean `app.shutdown` line for every last-seen server PID (a deliberate `herdr server stop`)
#     -> never restarted; the watchdog stays quiet until a server is seen again.
#   - server never seen by this state file (fresh install or deleted state): restart only when
#     in-flight work exists (open client or a Veyyon session file written in the last 30 minutes),
#     which is the old rule.
# Restarts use bounded exponential backoff (BackoffBaseSeconds * 2^(starts-1), capped at
# BackoffCapSeconds) and a crash-loop cap (MaxStarts starts per WindowMinutes). Every decision
# writes a line to watchdog.log.
# -Session watches one named session (`herdr --session <name> server`); without it the default
# session is watched and named sessions are ignored.
# -DryRun prints the decision, starts nothing and does not write state.
param(
    [switch]$DryRun,
    [string]$Session = '',
    [string]$StateDir = '',
    [string]$HerdrExe = '',
    [int]$BackoffBaseSeconds = 15,
    [int]$BackoffCapSeconds = 300,
    [int]$MaxStarts = 5,
    [int]$WindowMinutes = 30
)

$ErrorActionPreference = 'Stop'
if (-not $StateDir) {
    $StateDir = Join-Path $env:APPDATA 'herdr'
    if ($Session) { $StateDir = Join-Path $StateDir (Join-Path 'sessions' $Session) }
}
$state = Join-Path $StateDir 'watchdog-state.json'
$log = Join-Path $StateDir 'watchdog.log'
$serverLog = Join-Path $StateDir 'herdr-server.log'

# One run at a time per watched session: overlapping runs must not both start a server.
$mutex = New-Object System.Threading.Mutex($false, ('Global\herdr-watchdog-' + $(if ($Session) { $Session } else { 'default' })))
$locked = $false
try { $locked = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $locked = $true }
if (-not $locked) { Write-Output 'another herdr_watchdog run is active; skipping'; exit 0 }
function Write-Log([string]$m) {
    $line = '{0} {1}' -f (Get-Date).ToUniversalTime().ToString('o'), $m
    if ($DryRun) { Write-Output $line; return }
    try { Add-Content -Path $log -Value $line } catch { Write-Error -ErrorAction Continue ("watchdog log write failed: " + $_) }
}

function Get-RecentVeyyonSession([int]$Minutes = 30) {
    $cutoff = (Get-Date).AddMinutes(-$Minutes).ToUniversalTime()
    $sessionPaths = @(
        (Join-Path $env:USERPROFILE '.veyyon\profiles\*\sessions'),
        (Join-Path $env:USERPROFILE '.veyyon\profiles\*\agent\sessions')
    )
    $sessionDirs = @(Resolve-Path $sessionPaths -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Path -Unique)
    foreach ($pattern in @('*.jsonl', '*.json')) {
        foreach ($dir in $sessionDirs) {
            if (-not (Test-Path -LiteralPath $dir)) { continue }
            try {
                foreach ($file in [System.IO.Directory]::EnumerateFiles($dir, $pattern, [System.IO.SearchOption]::AllDirectories)) {
                    $fi = [System.IO.FileInfo]::new($file)
                    if ($fi.LastWriteTimeUtc -gt $cutoff) { return $fi }
                }
            } catch { }
        }
    }
    return $null
}

# True when the server log holds the clean-exit line herdr writes on `server stop` for this exact
# process: same PID and a timestamp at or after the process start (a reused PID cannot match an old line).
function Test-CleanShutdown([int]$ServerPid, [long]$StartedUnix) {
    if (-not (Test-Path -LiteralPath $serverLog)) { return $false }
    $fs = [System.IO.File]::Open($serverLog, 'Open', 'Read', 'ReadWrite')
    try {
        $len = $fs.Length
        $take = [Math]::Min($len, 262144)
        $fs.Seek($len - $take, 'Begin') | Out-Null
        $buf = New-Object byte[] $take
        $read = $fs.Read($buf, 0, $take)
        $text = [System.Text.Encoding]::UTF8.GetString($buf, 0, $read)
    } finally { $fs.Dispose() }
    $re = '(?m)^(\S+)\s+\S+\s+.*?app\.shutdown" subsystem="server" outcome="completed" pid={0}(\D|$)' -f $ServerPid
    foreach ($m in [regex]::Matches($text, $re)) {
        try {
            $at = [DateTimeOffset]::Parse($m.Groups[1].Value, [Globalization.CultureInfo]::InvariantCulture).ToUnixTimeSeconds()
            if ($at -ge $StartedUnix - 5) { return $true }
        } catch { }
    }
    return $false
}

function Test-SessionMatch([string]$cmd) {
    if ($Session) { return $cmd -match ('--session\s+"?{0}"?\s+server(\s|$)' -f [regex]::Escape($Session)) }
    return ($cmd -match '\sserver(\s|$)') -and ($cmd -notmatch '--session\s')
}

$procs = @(Get-CimInstance Win32_Process -Filter "Name='herdr.exe'")
$servers = @($procs | Where-Object { Test-SessionMatch $_.CommandLine })
$clients = @($procs | Where-Object { $_.CommandLine -notmatch '\sserver(\s|$)' -and $_.CommandLine -notmatch '\s(update|status|api|pane|tab|workspace|agent|session|machine|integration|worktree|notification|config|channel)\b' })
$blind = @($procs | Where-Object { -not $_.CommandLine }).Count
$st = @{ wasUp = $false; stopped = $false; lastPids = @(); lastStarted = @(); starts = @() }
if (Test-Path $state) {
    try {
        $j = Get-Content $state -Raw | ConvertFrom-Json
        $st = @{ wasUp = [bool]$j.wasUp; stopped = [bool]$j.stopped; lastPids = @($j.lastPids | Where-Object { $null -ne $_ }); lastStarted = @($j.lastStarted | Where-Object { $null -ne $_ }); starts = @($j.starts | Where-Object { $null -ne $_ }) }
    } catch { }
}
$now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$st.starts = @($st.starts | Where-Object { $_ -gt ($now - $WindowMinutes * 60) })

if ($servers.Count -gt 0) {
    $st.wasUp = $true
    $st.stopped = $false
    $st.lastPids = @($servers | ForEach-Object { [int]$_.ProcessId })
    $st.lastStarted = @($servers | ForEach-Object { [DateTimeOffset]::new($_.CreationDate).ToUnixTimeSeconds() })
    Write-Log ("ok servers={0} clients={1}" -f $servers.Count, $clients.Count)
} elseif ($blind -gt 0) {
    Write-Log ("no-restart: {0} herdr process(es) with unreadable command line, cannot rule out a running server" -f $blind)
} elseif ($st.stopped) {
    Write-Log "no-restart: server was stopped deliberately and has not been seen since"
} else {
    $abnormal = $false
    $reason = ''
    if ($st.wasUp) {
        $unclean = @()
        for ($i = 0; $i -lt $st.lastPids.Count; $i++) {
            if (-not (Test-CleanShutdown $st.lastPids[$i] ([long]$st.lastStarted[$i]))) { $unclean += $st.lastPids[$i] }
        }
        if ($unclean.Count -gt 0) {
            $abnormal = $true
            $reason = 'server exited without a clean shutdown (pid {0}), clients={1}' -f ($unclean -join ','), $clients.Count
        } else {
            $st.wasUp = $false
            $st.stopped = $true
            Write-Log ("no-restart: deliberate stop (clean shutdown logged for pid {0})" -f ($st.lastPids -join ','))
        }
    } else {
        $why = @()
        if ($clients.Count -gt 0) { $why += ("{0} client(s) open" -f $clients.Count) }
        $recent = Get-RecentVeyyonSession -Minutes 30
        if ($recent) { $why += ("active Veyyon session modified in last 30m ({0})" -f $recent.Name) }
        if ($why.Count -gt 0) {
            $abnormal = $true
            $reason = 'server missing, never seen by watchdog, in-flight work: ' + ($why -join ', ')
        } else {
            Write-Log "no-restart: server missing, never seen by watchdog, no in-flight work"
        }
    }

    if ($abnormal) {
        $n = $st.starts.Count
        if ($n -ge $MaxStarts) {
            Write-Log ("crash-loop cap: {0} starts in {1} min, not restarting ({2})" -f $n, $WindowMinutes, $reason)
        } else {
            $wait = 0
            if ($n -gt 0) {
                $wait = [Math]::Min($BackoffCapSeconds, $BackoffBaseSeconds * [Math]::Pow(2, $n - 1))
                $wait = [int]($wait - ($now - ($st.starts | Measure-Object -Maximum).Maximum))
            }
            if ($wait -gt 0) {
                Write-Log ("backoff: next restart allowed in {0}s, start {1} of {2} ({3})" -f $wait, ($n + 1), $MaxStarts, $reason)
            } elseif ($DryRun) {
                Write-Log ("would start herdr server, start {0} of {1} ({2})" -f ($n + 1), $MaxStarts, $reason)
            } else {
                $again = @(Get-CimInstance Win32_Process -Filter "Name='herdr.exe'" | Where-Object { Test-SessionMatch $_.CommandLine })
                if ($again.Count -gt 0) {
                    Write-Log ("no-restart: server appeared before start (pid {0})" -f (($again | ForEach-Object { $_.ProcessId }) -join ','))
                } else {
                    $exe = if ($HerdrExe) { $HerdrExe } else { (Get-Command herdr -ErrorAction Stop).Source }
                    $argv = if ($Session) { @('--session', $Session, 'server') } else { @('server') }
                    $p = Start-Process -FilePath $exe -ArgumentList $argv -WindowStyle Hidden -PassThru
                    $st.starts += $now
                    # Persist the start before logging, so a log failure cannot bypass backoff and the cap.
                    $st | ConvertTo-Json | Set-Content $state
                    Write-Log ("restarted herdr server pid={0} via {1}, start {2} of {3} ({4})" -f $p.Id, $exe, ($n + 1), $MaxStarts, $reason)
                }
            }
        }
    }
}
if (-not $DryRun) { $st | ConvertTo-Json | Set-Content $state }
