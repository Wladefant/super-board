# test_herdr_watchdog.ps1
# Isolated end-to-end proof for herdr_watchdog.ps1 (super-board#394). Starts a throwaway herdr
# SESSION (never the default session), kills its server with no client attached and checks the
# watchdog behaviour: restart, backoff, crash-loop cap, and no restart after a clean stop.
# Touches only the session named below. Exit code 0 = all checks passed.
param(
    [string]$HerdrExe = '',
    [string]$Session = ('wdproof-' + [guid]::NewGuid().ToString('N').Substring(0, 6)),
    [int]$BackoffBaseSeconds = 6,
    [int]$MaxStarts = 3
)
$ErrorActionPreference = 'Stop'
if (-not $HerdrExe) { $HerdrExe = (Get-Command herdr -ErrorAction Stop).Source }
if ($Session -eq 'default') { throw 'refusing to run against the default session' }
$watchdog = Join-Path $PSScriptRoot 'herdr_watchdog.ps1'
$dir = Join-Path $env:APPDATA (Join-Path 'herdr' (Join-Path 'sessions' $Session))
$wlog = Join-Path $dir 'watchdog.log'
$script:fail = 0

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { Write-Output ("PASS {0} {1}" -f $name, $detail) } else { Write-Output ("FAIL {0} {1}" -f $name, $detail); $script:fail++ }
}
function Get-SessionServers {
    , @(Get-CimInstance Win32_Process -Filter "Name='herdr.exe'" | Where-Object { $_.CommandLine -match ('--session\s+"?{0}"?\s+server(\s|$)' -f $Session) })
}
function Invoke-Watchdog {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $watchdog -Session $Session -HerdrExe $HerdrExe `
        -BackoffBaseSeconds $BackoffBaseSeconds -MaxStarts $MaxStarts | Out-Null
}
function Wait-ServerUp([int]$Seconds = 20) {
    $t = [Diagnostics.Stopwatch]::StartNew()
    while ($t.Elapsed.TotalSeconds -lt $Seconds) {
        $s = Get-SessionServers
        if ($s.Count -gt 0 -and (Test-Path (Join-Path $dir 'herdr.sock'))) { return $s[0] }
        Start-Sleep -Milliseconds 500
    }
    return $null
}
function Kill-Server($srv) { Stop-Process -Id $srv.ProcessId -Force; Start-Sleep -Milliseconds 800 }
function Log-Tail { if (Test-Path $wlog) { Get-Content $wlog | ForEach-Object { "    watchdog.log: $_" } } }

try {
    Write-Output "session=$Session exe=$HerdrExe"
    Start-Process -FilePath $HerdrExe -ArgumentList @('--session', $Session, 'server') -WindowStyle Hidden | Out-Null
    $s0 = Wait-ServerUp
    Check 'isolated server starts' ($null -ne $s0) ("pid=" + $s0.ProcessId)
    if (-not $s0) { throw 'no server' }
    Invoke-Watchdog   # records the server as seen

    # 1. kill -9 with no client attached: restart on the next poll
    Kill-Server $s0
    $t = [Diagnostics.Stopwatch]::StartNew()
    Invoke-Watchdog
    $s1 = Wait-ServerUp
    Check 'kill -9 -> server restarted with no client' (($null -ne $s1) -and ($s1.ProcessId -ne $s0.ProcessId)) ("old=$($s0.ProcessId) new=$($s1.ProcessId) after=$([int]$t.Elapsed.TotalSeconds)s")
    Check 'restart within base backoff' ($t.Elapsed.TotalSeconds -le $BackoffBaseSeconds + 15) ("elapsed=$([int]$t.Elapsed.TotalSeconds)s")
    Invoke-Watchdog

    # 2. immediate second kill: backoff must hold the restart back, then allow it
    Kill-Server $s1
    Invoke-Watchdog
    Check 'second crash waits out the backoff' ((Get-SessionServers).Count -eq 0) 'no server right after the second kill'
    $deadline = (Get-Date).AddSeconds($BackoffBaseSeconds + 20)
    $s2 = $null
    while ((Get-Date) -lt $deadline -and -not $s2) { Start-Sleep -Seconds 2; Invoke-Watchdog; $s2 = Wait-ServerUp 3 }
    Check 'second crash restarted after backoff' (($null -ne $s2) -and ($s2.ProcessId -ne $s1.ProcessId)) ("new=$($s2.ProcessId)")
    Invoke-Watchdog

    # 3. crash loop: third kill takes the start budget to MaxStarts, next kill must not restart
    Kill-Server $s2
    $deadline = (Get-Date).AddSeconds($BackoffBaseSeconds * 2 + 25)
    $s3 = $null
    while ((Get-Date) -lt $deadline -and -not $s3) { Start-Sleep -Seconds 2; Invoke-Watchdog; $s3 = Wait-ServerUp 3 }
    Check 'third crash restarted' ($null -ne $s3)
    Invoke-Watchdog
    Kill-Server $s3
    for ($i = 0; $i -lt 4; $i++) { Start-Sleep -Seconds 2; Invoke-Watchdog }
    Check 'crash-loop cap stops restarts' ((Get-SessionServers).Count -eq 0) ("starts capped at $MaxStarts")
    Check 'cap is logged' ([bool](Select-String -Path $wlog -Pattern 'crash-loop cap' -Quiet))
} finally {
    Log-Tail
}

# 4. deliberate stop is not undone (fresh state, fresh server)
Remove-Item (Join-Path $dir 'watchdog-state.json') -ErrorAction SilentlyContinue
Start-Process -FilePath $HerdrExe -ArgumentList @('--session', $Session, 'server') -WindowStyle Hidden | Out-Null
$s4 = Wait-ServerUp
Check 'server up for stop test' ($null -ne $s4)
if ($s4) {
    Invoke-Watchdog
    & $HerdrExe --session $Session server stop 2>&1 | Out-Null
    $t = [Diagnostics.Stopwatch]::StartNew()
    while ((Get-SessionServers).Count -gt 0 -and $t.Elapsed.TotalSeconds -lt 15) { Start-Sleep -Milliseconds 500 }
    for ($i = 0; $i -lt 3; $i++) { Start-Sleep -Seconds 2; Invoke-Watchdog }
    Check 'clean `server stop` is not restarted' ((Get-SessionServers).Count -eq 0)
    Check 'deliberate stop is logged' ([bool](Select-String -Path $wlog -Pattern 'deliberate stop' -Quiet))
}

# 5. PID reuse: a clean-shutdown line from an OLDER process with the same PID must not hide a crash.
if ($s4) {
    $future = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() + 3600
    @{ wasUp = $true; stopped = $false; lastPids = @($s4.ProcessId); lastStarted = @($future); starts = @() } |
        ConvertTo-Json | Set-Content (Join-Path $dir 'watchdog-state.json')
    Invoke-Watchdog
    $s5 = Wait-ServerUp
    Check 'stale shutdown line of a reused PID does not block restart' ($null -ne $s5)
}

# cleanup: only this session
foreach ($p in (Get-SessionServers)) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
& $HerdrExe session delete $Session 2>&1 | Out-Null
Write-Output ("RESULT failures={0}" -f $script:fail)
exit $script:fail
