$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Skill = Join-Path $Root 'skills\claudex-optimized'
$Launch = Join-Path $Skill 'scripts\launch.ps1'
$Setup = Join-Path $Skill 'scripts\setup.ps1'
$Audit = Join-Path $Skill 'scripts\audit.ps1'

function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw "ASSERT: $Message" } }
function Assert-Equal($Actual, $Expected, [string]$Message) { if ($Actual -ne $Expected) { throw "ASSERT: $Message (actual='$Actual', expected='$Expected')" } }
function Get-EnvValue([string]$Name) { $item = Get-Item "Env:$Name" -ErrorAction SilentlyContinue; if ($null -eq $item) { return $null }; return $item.Value }
function Get-ByteHash([byte[]]$Bytes) { $sha = [Security.Cryptography.SHA256]::Create(); try { return [BitConverter]::ToString($sha.ComputeHash($Bytes)) } finally { $sha.Dispose() } }

function Invoke-Process {
    param([string]$FileName, [string[]]$Arguments)
    $quoted = $Arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + $_.Replace('"', '\"') + '"' } else { $_ } }
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $FileName
    $start.Arguments = $quoted -join ' '
    $start.UseShellExecute = $false
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    $null = $process.Start()
    $stdout = $process.StandardOutput.ReadToEnd(); $stderr = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    return [pscustomobject]@{ ExitCode = $process.ExitCode; Output = ($stdout + $stderr).Trim() }
}

function Invoke-SetupFixture {
    param([string]$Action, [string]$Profile, [string]$Junction, [string]$State, [string]$Canonical, [string]$FixtureFailure = '')
    $arguments = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$Setup,'-Action',$Action,'-ProfilePath',$Profile,'-JunctionPath',$Junction,'-StatePath',$State,'-CanonicalSkillPath',$Canonical)
    if ($FixtureFailure) { $arguments += @('-FixtureFailure', $FixtureFailure) }
    return Invoke-Process 'powershell.exe' $arguments
}

function Invoke-AuditFixture {
    param([string]$Profile, [string]$Junction, [string]$Canonical, [string]$RoutingState)
    $arguments = @(
        '-NoProfile','-ExecutionPolicy','Bypass','-File',$Audit,
        '-ProfilePath',$Profile,'-JunctionPath',$Junction,'-CanonicalSkillPath',$Canonical,
        '-RoutingStatePath',$RoutingState,'-RoutingStateMaxAgeHours','24','-SkipProxyModelInventory'
    )
    return Invoke-Process 'powershell.exe' $arguments
}

function New-FixtureCase([string]$Name) {
    $case = Join-Path $tempRoot $Name
    $canonical = Join-Path $case 'canonical\claudex-optimized'
    $junction = Join-Path $case 'installed\claudex-optimized'
    $profile = Join-Path $case 'profile\profile.ps1'
    $state = Join-Path $case 'state'
    $null = New-Item -ItemType Directory -Path (Join-Path $canonical 'scripts') -Force
    Copy-Item -LiteralPath $Launch -Destination (Join-Path $canonical 'scripts\launch.ps1')
    $null = New-Item -ItemType Directory -Path (Split-Path -Parent $junction) -Force
    $null = New-Item -ItemType Directory -Path (Split-Path -Parent $profile) -Force
    $fixtureJunctions.Add($junction)
    return [pscustomobject]@{ Canonical=$canonical; Junction=$junction; Profile=$profile; State=$state }
}

function Remove-FixtureJunction([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { [IO.Directory]::Delete($Path) }
    }
}

$envNames = @(
    'CLAUDE_CODE_SUBAGENT_MODEL','ANTHROPIC_DEFAULT_FABLE_MODEL','ANTHROPIC_API_KEY','ANTHROPIC_MODEL',
    'ANTHROPIC_SMALL_FAST_MODEL','CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS','ANTHROPIC_DEFAULT_HAIKU_MODEL',
    'ANTHROPIC_DEFAULT_SONNET_MODEL','ANTHROPIC_DEFAULT_OPUS_MODEL','ENABLE_TOOL_SEARCH',
    'ANTHROPIC_BASE_URL','ANTHROPIC_AUTH_TOKEN','CLAUDE_CODE_ALWAYS_ENABLE_EFFORT',
    'CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY','CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY',
    'CLAUDEX_OPTIMIZED_PROBE_ARGV'
)
$envSnapshot = @{}
foreach ($name in $envNames) {
    $item = Get-Item "Env:$name" -ErrorAction SilentlyContinue
    $envSnapshot[$name] = [pscustomobject]@{ Exists = $null -ne $item; Value = if ($null -eq $item) { $null } else { $item.Value } }
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("claudex-tests-" + [guid]::NewGuid().ToString('N'))
$fixtureJunctions = New-Object System.Collections.Generic.List[string]
$gatewayProcesses = New-Object System.Collections.Generic.List[System.Diagnostics.Process]

try {
    $null = New-Item -ItemType Directory -Path $tempRoot
    foreach ($script in @($Launch, $Setup, $Audit)) { $null = [scriptblock]::Create([IO.File]::ReadAllText($script)) }
    $launchText = [IO.File]::ReadAllText($Launch)
    $setupText = [IO.File]::ReadAllText($Setup)
    Assert-True ($setupText.Contains('[System.IO.File]::Replace($temp, $ProfilePath, $replaceBackup, $true)')) 'profile replacement remains same-directory atomic'
    Assert-True ($setupText.Contains('[System.IO.File]::Replace($temp, $StateFile, $replaceBackup, $true)')) 'state replacement remains same-directory atomic'
    Assert-True (-not $setupText.Contains('Remove-Item -LiteralPath $state.transactionPath -Recurse')) 'transaction cleanup is not recursive'
    Assert-True ($setupText.Contains('Assert-NoReparsePoints ([string]$state.transactionPath)')) 'rollback rejects transaction reparse points before access'
    $gatewayBoundary = $launchText.IndexOf('$gatewayUri = [uri]$GatewayBaseUrl', [StringComparison]::Ordinal)
    Assert-True ($launchText.IndexOf('if ($ProbeArgvRoundTrip)', [StringComparison]::Ordinal) -lt $gatewayBoundary) 'argv probe exits before gateway access'
    Assert-True ($launchText.IndexOf('if ($ProbeEnvironment)', [StringComparison]::Ordinal) -lt $gatewayBoundary) 'environment probe exits before gateway access'
    Assert-True ($launchText.Contains("`$ApprovedProxyBinary = 'C:\Users\wkiri\.cli-proxy-api\cli-proxy-api.exe'")) 'approved daemon binary remains exact'
    Assert-True ($launchText.Contains("`$ApprovedProxyConfig = 'C:\Users\wkiri\.cli-proxy-api\config.yaml'")) 'approved daemon config remains exact'
    Assert-True ($launchText.Contains("Start-Process -WindowStyle Hidden -FilePath `$ApprovedProxyBinary -ArgumentList '-config', `$ApprovedProxyConfig")) 'only the approved daemon command is startable'
    Assert-True ($launchText.Contains('Get-CimInstance Win32_Process')) 'approved listener owner executable and command line are authenticated'
    Assert-True ($launchText.Contains('Get-NetTCPConnection -LocalPort $port -State Listen')) 'approved listener PID comes from the listening socket'
    Assert-True ($launchText.Contains("`$deadline = [DateTime]::UtcNow.AddSeconds(`$HealthTimeoutSeconds)")) 'approved daemon startup has a bounded deadline'
    Assert-True ($launchText.Contains('while ((-not $ownerVerified -or $null -eq $health -or -not $health.Ready)')) 'approved daemon startup polls owner identity and inventory'
    Assert-True ($launchText.Contains("`$code = Invoke-ClaudeExact (@('--model', 'opus') + `$EffectiveClaudeArgs)")) 'main CLI retains Opus identity through exact native argv forwarding'
    Assert-True ($launchText.Contains('ConvertTo-WindowsCommandLineArgument')) 'native forwarding preserves empty and JSON arguments on Windows PowerShell 5.1'
    Write-Output '  ok  PowerShell syntax and no-touch probe ordering parse'

    $sentinels = [ordered]@{
        CLAUDE_CODE_SUBAGENT_MODEL='parent-subagent-sentinel'; ANTHROPIC_DEFAULT_FABLE_MODEL='parent-fable-sentinel';
        ANTHROPIC_API_KEY='parent-api-key-sentinel'; ANTHROPIC_MODEL='parent-model-sentinel';
        ANTHROPIC_SMALL_FAST_MODEL='parent-small-sentinel'; CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS='parent-beta-sentinel';
        ANTHROPIC_DEFAULT_HAIKU_MODEL='parent-haiku-sentinel'; ANTHROPIC_DEFAULT_SONNET_MODEL='parent-sonnet-sentinel';
        ANTHROPIC_DEFAULT_OPUS_MODEL='parent-opus-sentinel'; ENABLE_TOOL_SEARCH='parent-tool-search-sentinel';
        ANTHROPIC_BASE_URL='parent-base-url-sentinel'; ANTHROPIC_AUTH_TOKEN='parent-auth-token-sentinel';
        CLAUDE_CODE_ALWAYS_ENABLE_EFFORT='parent-effort-sentinel'; CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY='parent-concurrency-sentinel';
        CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY='parent-discovery-sentinel'
    }
    foreach ($entry in $sentinels.GetEnumerator()) { Set-Item "Env:$($entry.Key)" $entry.Value }
    $probe = (& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Launch -ProbeEnvironment) | ConvertFrom-Json
    Assert-Equal $LASTEXITCODE 0 'launch probe exits zero'
    Assert-Equal $probe.ANTHROPIC_BASE_URL 'http://127.0.0.1:8317' 'child base URL'
    Assert-Equal $probe.ANTHROPIC_DEFAULT_HAIKU_MODEL 'gpt-5.6-luna' 'Haiku maps to Luna'
    Assert-Equal $probe.ANTHROPIC_DEFAULT_SONNET_MODEL 'gpt-5.6-terra' 'Sonnet maps to Terra'
    Assert-Equal $probe.ANTHROPIC_DEFAULT_OPUS_MODEL 'gpt-5.6-sol' 'Opus maps to Sol'
    Assert-Equal $probe.ENABLE_TOOL_SEARCH 'true' 'tool search is true'
    Assert-Equal $probe.ANTHROPIC_AUTH_TOKEN '<redacted-set>' 'child auth token is set and redacted'
    Assert-Equal $probe.CLAUDE_CODE_ALWAYS_ENABLE_EFFORT '1' 'child effort override'
    Assert-Equal $probe.CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY '3' 'child concurrency override'
    Assert-Equal $probe.CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY '1' 'child gateway discovery'
    Assert-True ($null -eq $probe.CLAUDE_CODE_SUBAGENT_MODEL) 'global subagent mapping absent'
    Assert-True ($null -eq $probe.ANTHROPIC_DEFAULT_FABLE_MODEL) 'Fable mapping absent'
    foreach ($entry in $sentinels.GetEnumerator()) { Assert-Equal (Get-EnvValue $entry.Key) $entry.Value "parent preserved: $($entry.Key)" }
    Write-Output '  ok  child environment isolation'

    $fixture = New-FixtureCase 'byte-exact'
    $originalText = "# before`r`nfunction keep-me { 'unchanged' }`r`nfunction claude-codex {`r`n    claude @args`r`n}`r`n# after`r`n"
    $encoding = New-Object Text.UTF8Encoding($true)
    [IO.File]::WriteAllText($fixture.Profile, $originalText, $encoding)
    $originalBytes = [IO.File]::ReadAllBytes($fixture.Profile)
    $originalAcl = (Get-Acl -LiteralPath $fixture.Profile).Sddl
    $plan = Invoke-SetupFixture Plan $fixture.Profile $fixture.Junction $fixture.State $fixture.Canonical
    Assert-Equal $plan.ExitCode 0 'setup plan succeeds'
    Assert-Equal (Get-ByteHash ([IO.File]::ReadAllBytes($fixture.Profile))) (Get-ByteHash $originalBytes) 'plan preserves bytes'
    Assert-True (-not (Test-Path -LiteralPath $fixture.Junction)) 'plan does not create junction'
    Assert-True (-not (Test-Path -LiteralPath $fixture.State)) 'plan does not create transaction state'
    $apply = Invoke-SetupFixture Apply $fixture.Profile $fixture.Junction $fixture.State $fixture.Canonical
    Assert-Equal $apply.ExitCode 0 "setup apply succeeds: $($apply.Output)"
    $appliedBytes = [IO.File]::ReadAllBytes($fixture.Profile)
    Assert-True ($appliedBytes[0] -eq 0xEF -and $appliedBytes[1] -eq 0xBB -and $appliedBytes[2] -eq 0xBF) 'UTF-8 BOM preserved'
    Assert-Equal (Get-Acl -LiteralPath $fixture.Profile).Sddl $originalAcl 'ACL preserved after apply'
    $stateJson = [IO.File]::ReadAllText((Join-Path $fixture.State 'state.json')) | ConvertFrom-Json
    Assert-True ($stateJson.backupPath.StartsWith([IO.Path]::GetFullPath($fixture.State), [StringComparison]::OrdinalIgnoreCase)) 'backup stored under fixture state'
    Assert-Equal (Get-ByteHash ([IO.File]::ReadAllBytes($stateJson.backupPath))) (Get-ByteHash $originalBytes) 'transaction backup is byte exact'
    $profileHashBeforeValidate = Get-ByteHash ([IO.File]::ReadAllBytes($fixture.Profile))
    $stateHashBeforeValidate = Get-ByteHash ([IO.File]::ReadAllBytes((Join-Path $fixture.State 'state.json')))
    Assert-Equal (Invoke-SetupFixture Validate $fixture.Profile $fixture.Junction $fixture.State $fixture.Canonical).ExitCode 0 'validate succeeds'
    Assert-Equal (Get-ByteHash ([IO.File]::ReadAllBytes($fixture.Profile))) $profileHashBeforeValidate 'validate preserves profile bytes'
    Assert-Equal (Get-ByteHash ([IO.File]::ReadAllBytes((Join-Path $fixture.State 'state.json')))) $stateHashBeforeValidate 'validate preserves state bytes'

    $env:CLAUDEX_OPTIMIZED_PROBE_ARGV = '1'
    $invokeScript = Join-Path $tempRoot 'invoke-profile.ps1'
    $invokeBody = @'
param([string]$Profile)
. $Profile
$unicode = -join @([char]0x47,[char]0x72,[char]0xFC,[char]0xDF,[char]0x65,[char]0x4E16,[char]0x754C)
claude-codex 'space value' 'quote"inside' '' $unicode '--'
'@
    [IO.File]::WriteAllText($invokeScript, $invokeBody, (New-Object Text.UTF8Encoding($true)))
    $roundTrip = Invoke-Process 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-File',$invokeScript,$fixture.Profile)
    Assert-Equal $roundTrip.ExitCode 0 "encoded argv probe succeeds: $($roundTrip.Output)"
    $parsedArgv = ConvertFrom-Json -InputObject $roundTrip.Output
    $argv = @($parsedArgv)
    $unicodeExpected = -join @([char]0x47,[char]0x72,[char]0xFC,[char]0xDF,[char]0x65,[char]0x4E16,[char]0x754C)
    $expectedArgv = @('space value','quote"inside','',$unicodeExpected,'--')
    Assert-Equal $argv.Count $expectedArgv.Count 'argv count preserved'
    for ($i=0; $i -lt $expectedArgv.Count; $i++) { Assert-Equal $argv[$i] $expectedArgv[$i] "argv[$i] preserved" }
    Remove-Item Env:CLAUDEX_OPTIMIZED_PROBE_ARGV -ErrorAction SilentlyContinue

    $rollback = Invoke-SetupFixture Rollback $fixture.Profile $fixture.Junction $fixture.State $fixture.Canonical
    Assert-Equal $rollback.ExitCode 0 "rollback succeeds: $($rollback.Output)"
    Assert-Equal (Get-ByteHash ([IO.File]::ReadAllBytes($fixture.Profile))) (Get-ByteHash $originalBytes) 'rollback restores exact bytes'
    Assert-Equal (Get-Acl -LiteralPath $fixture.Profile).Sddl $originalAcl 'rollback restores ACL'
    Assert-True (-not (Test-Path -LiteralPath $fixture.Junction)) 'rollback removes junction'

    $encodingCases = @(
        @{ Name='utf8-nobom'; Encoding=(New-Object Text.UTF8Encoding($false)) },
        @{ Name='utf8-bom'; Encoding=(New-Object Text.UTF8Encoding($true)) },
        @{ Name='utf16le'; Encoding=(New-Object Text.UnicodeEncoding($false,$true,$true)) },
        @{ Name='utf16be'; Encoding=(New-Object Text.UnicodeEncoding($true,$true,$true)) },
        @{ Name='utf32le'; Encoding=(New-Object Text.UTF32Encoding($false,$true,$true)) },
        @{ Name='utf32be'; Encoding=(New-Object Text.UTF32Encoding($true,$true,$true)) }
    )
    foreach ($case in $encodingCases) {
        $encoded = New-FixtureCase ("encoding-" + $case.Name)
        $text = "# $($case.Name) Ω 世界`r`nfunction keep-encoding { 'yes' }`r`n"
        [IO.File]::WriteAllText($encoded.Profile, $text, $case.Encoding)
        $beforeBytes = [IO.File]::ReadAllBytes($encoded.Profile)
        $beforeAcl = (Get-Acl -LiteralPath $encoded.Profile).Sddl
        $applied = Invoke-SetupFixture Apply $encoded.Profile $encoded.Junction $encoded.State $encoded.Canonical
        Assert-Equal $applied.ExitCode 0 "$($case.Name) apply succeeds: $($applied.Output)"
        $afterBytes = [IO.File]::ReadAllBytes($encoded.Profile)
        $preamble = $case.Encoding.GetPreamble()
        for ($i=0; $i -lt $preamble.Length; $i++) { Assert-Equal $afterBytes[$i] $preamble[$i] "$($case.Name) preamble byte $i preserved" }
        if ($preamble.Length -eq 0) { Assert-True (-not ($afterBytes.Length -ge 3 -and $afterBytes[0] -eq 0xEF -and $afterBytes[1] -eq 0xBB -and $afterBytes[2] -eq 0xBF)) "$($case.Name) remains BOM-free" }
        Assert-Equal (Get-Acl -LiteralPath $encoded.Profile).Sddl $beforeAcl "$($case.Name) ACL preserved after apply"
        Assert-Equal (Invoke-SetupFixture Validate $encoded.Profile $encoded.Junction $encoded.State $encoded.Canonical).ExitCode 0 "$($case.Name) validate succeeds"
        Assert-Equal (@(Get-ChildItem -LiteralPath $encoded.State -Force | Where-Object { $_.Name -like '.state-*' }).Count) 0 "$($case.Name) atomic state write leaves no temp files"
        Assert-Equal (Invoke-SetupFixture Rollback $encoded.Profile $encoded.Junction $encoded.State $encoded.Canonical).ExitCode 0 "$($case.Name) rollback succeeds"
        Assert-Equal (Get-ByteHash ([IO.File]::ReadAllBytes($encoded.Profile))) (Get-ByteHash $beforeBytes) "$($case.Name) rollback restores exact bytes"
        Assert-Equal (Get-Acl -LiteralPath $encoded.Profile).Sddl $beforeAcl "$($case.Name) rollback restores ACL"
    }
    $invalidEncoding = New-FixtureCase 'invalid-bomless-encoding'
    [IO.File]::WriteAllBytes($invalidEncoding.Profile, [byte[]]@(0x23,0x20,0x80,0x0A))
    $invalidEncodingHash = Get-ByteHash ([IO.File]::ReadAllBytes($invalidEncoding.Profile))
    Assert-True ((Invoke-SetupFixture Plan $invalidEncoding.Profile $invalidEncoding.Junction $invalidEncoding.State $invalidEncoding.Canonical).ExitCode -ne 0) 'invalid BOM-less UTF-8 fails closed'
    Assert-Equal (Get-ByteHash ([IO.File]::ReadAllBytes($invalidEncoding.Profile))) $invalidEncodingHash 'invalid encoding remains byte exact'
    Assert-True (-not (Test-Path -LiteralPath $invalidEncoding.State)) 'invalid encoding creates no state'

    $recoverOnce = New-FixtureCase 'apply-acl-failure-restored'
    [IO.File]::WriteAllText($recoverOnce.Profile, '# original one-shot ACL failure')
    $recoverOnceBytes = [IO.File]::ReadAllBytes($recoverOnce.Profile)
    $recoverOnceAcl = (Get-Acl -LiteralPath $recoverOnce.Profile).Sddl
    $recoverOnceResult = Invoke-SetupFixture Apply $recoverOnce.Profile $recoverOnce.Junction $recoverOnce.State $recoverOnce.Canonical 'ApplyPostReplaceAclOnce'
    Assert-True ($recoverOnceResult.ExitCode -ne 0) 'fixture injects one post-replace ACL failure'
    Assert-Equal (Get-ByteHash ([IO.File]::ReadAllBytes($recoverOnce.Profile))) (Get-ByteHash $recoverOnceBytes) 'one-shot failure restores original bytes'
    Assert-Equal (Get-Acl -LiteralPath $recoverOnce.Profile).Sddl $recoverOnceAcl 'one-shot failure restores original ACL'
    Assert-True (-not (Test-Path -LiteralPath (Join-Path $recoverOnce.State 'state.json'))) 'verified restoration removes transaction state'
    Assert-Equal (@(Get-ChildItem -LiteralPath (Split-Path -Parent $recoverOnce.Profile) -Filter '.claudex-profile-*' -Force).Count) 0 'verified restoration removes File.Replace recovery copies'
    Assert-True (-not (Test-Path -LiteralPath $recoverOnce.Junction)) 'verified restoration removes transaction-created junction'

    $recoverFailed = New-FixtureCase 'apply-and-restore-acl-failure'
    [IO.File]::WriteAllText($recoverFailed.Profile, '# original persistent ACL failure')
    $recoverFailedResult = Invoke-SetupFixture Apply $recoverFailed.Profile $recoverFailed.Junction $recoverFailed.State $recoverFailed.Canonical 'ApplyAndRestorePostReplaceAcl'
    Assert-True ($recoverFailedResult.ExitCode -ne 0) 'persistent post-replace ACL failure is reported'
    Assert-True ($recoverFailedResult.Output.Contains('manual recovery')) 'failed restoration emits manual recovery guidance'
    Assert-True ($recoverFailedResult.Output -notmatch [regex]::Escape($recoverFailed.State)) 'manual recovery location is redacted instead of exposing the raw state path'
    $recoverFailedStateFile = Join-Path $recoverFailed.State 'state.json'
    Assert-True (Test-Path -LiteralPath $recoverFailedStateFile -PathType Leaf) 'failed restoration preserves state file'
    $recoverFailedState = [IO.File]::ReadAllText($recoverFailedStateFile) | ConvertFrom-Json
    Assert-True (Test-Path -LiteralPath $recoverFailedState.backupPath -PathType Leaf) 'failed restoration preserves transaction backup'
    Assert-True (Test-Path -LiteralPath $recoverFailedState.transactionPath -PathType Container) 'failed restoration preserves transaction directory'
    Assert-True (@(Get-ChildItem -LiteralPath (Split-Path -Parent $recoverFailed.Profile) -Filter '.claudex-profile-*.bak' -Force).Count -ge 1) 'failed restoration preserves File.Replace backup copies'
    Write-Output '  ok  byte-exact profile transaction, encoding/BOM/ACL, apply recovery, and encoded argv'

    foreach ($invalid in @(
        @{Name='parse-error'; Text='function claude-codex {'},
        @{Name='duplicate-function'; Text="function claude-codex {}`nfunction claude-codex {}"},
        @{Name='nested-legacy-function'; Text="function wrapper { function claude-codex {} }"},
        @{Name='conditional-legacy-function'; Text="if (`$true) { function claude-codex {} }"},
        @{Name='malformed-marker'; Text="# >>> claudex-optimized managed claude-codex >>>`nfunction claude-codex {}"},
        @{Name='duplicate-marker'; Text="# >>> claudex-optimized managed claude-codex >>>`n# >>> claudex-optimized managed claude-codex >>>`nfunction claude-codex {}`n# <<< claudex-optimized managed claude-codex <<<"},
        @{Name='ambiguous-managed'; Text="# >>> claudex-optimized managed claude-codex >>>`n`$x=1`nfunction claude-codex {}`n# <<< claudex-optimized managed claude-codex <<<"}
    )) {
        $bad = New-FixtureCase $invalid.Name
        [IO.File]::WriteAllText($bad.Profile, $invalid.Text)
        $result = Invoke-SetupFixture Plan $bad.Profile $bad.Junction $bad.State $bad.Canonical
        Assert-True ($result.ExitCode -ne 0) "$($invalid.Name) is rejected"
    }
    Write-Output '  ok  AST parse errors, duplicate definitions, and marker ambiguity rejected'

    $missing = New-FixtureCase 'missing-junction'
    [IO.File]::WriteAllText($missing.Profile, '# fixture')
    Assert-Equal (Invoke-SetupFixture Apply $missing.Profile $missing.Junction $missing.State $missing.Canonical).ExitCode 0 'missing-junction apply'
    Remove-FixtureJunction $missing.Junction
    Assert-Equal (Invoke-SetupFixture Rollback $missing.Profile $missing.Junction $missing.State $missing.Canonical).ExitCode 0 'missing transaction junction is idempotent'

    $partial = New-FixtureCase 'partial-rollback'
    [IO.File]::WriteAllText($partial.Profile, '# original partial')
    $partialBefore = [IO.File]::ReadAllBytes($partial.Profile)
    Assert-Equal (Invoke-SetupFixture Apply $partial.Profile $partial.Junction $partial.State $partial.Canonical).ExitCode 0 'partial rollback apply'
    [IO.File]::WriteAllBytes($partial.Profile, $partialBefore)
    Assert-Equal (Invoke-SetupFixture Rollback $partial.Profile $partial.Junction $partial.State $partial.Canonical).ExitCode 0 'already-restored profile rollback is idempotent'
    Assert-True (-not (Test-Path -LiteralPath $partial.Junction)) 'partial rollback still removes owned junction'

    $replacement = New-FixtureCase 'unknown-replacement'
    [IO.File]::WriteAllText($replacement.Profile, '# fixture')
    Assert-Equal (Invoke-SetupFixture Apply $replacement.Profile $replacement.Junction $replacement.State $replacement.Canonical).ExitCode 0 'replacement apply'
    Remove-FixtureJunction $replacement.Junction
    $null = New-Item -ItemType Directory -Path $replacement.Junction
    $appliedHash = Get-ByteHash ([IO.File]::ReadAllBytes($replacement.Profile))
    $blocked = Invoke-SetupFixture Rollback $replacement.Profile $replacement.Junction $replacement.State $replacement.Canonical
    Assert-True ($blocked.ExitCode -ne 0) 'unknown junction replacement blocks rollback'
    Assert-Equal (Get-ByteHash ([IO.File]::ReadAllBytes($replacement.Profile))) $appliedHash 'profile untouched when junction precondition fails'

    $tampered = New-FixtureCase 'tampered-state-path'
    [IO.File]::WriteAllText($tampered.Profile, '# fixture')
    Assert-Equal (Invoke-SetupFixture Apply $tampered.Profile $tampered.Junction $tampered.State $tampered.Canonical).ExitCode 0 'tampered-state apply'
    $tamperedHash = Get-ByteHash ([IO.File]::ReadAllBytes($tampered.Profile))
    $tamperedStatePath = Join-Path $tampered.State 'state.json'
    $tamperedState = [IO.File]::ReadAllText($tamperedStatePath) | ConvertFrom-Json
    $outsideBackup = Join-Path (Split-Path -Parent $tampered.State) 'outside-profile.bin'
    [IO.File]::WriteAllBytes($outsideBackup, [IO.File]::ReadAllBytes($tamperedState.backupPath))
    $tamperedState.backupPath = $outsideBackup
    [IO.File]::WriteAllText($tamperedStatePath, ($tamperedState | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
    $tamperedRollback = Invoke-SetupFixture Rollback $tampered.Profile $tampered.Junction $tampered.State $tampered.Canonical
    Assert-True ($tamperedRollback.ExitCode -ne 0) 'out-of-transaction backup path blocks rollback'
    Assert-Equal (Get-ByteHash ([IO.File]::ReadAllBytes($tampered.Profile))) $tamperedHash 'profile untouched when state path validation fails'
    Remove-Item -LiteralPath $outsideBackup -Force -Confirm:$false

    $transactionLink = New-FixtureCase 'transaction-reparse'
    [IO.File]::WriteAllText($transactionLink.Profile, '# transaction reparse fixture')
    Assert-Equal (Invoke-SetupFixture Apply $transactionLink.Profile $transactionLink.Junction $transactionLink.State $transactionLink.Canonical).ExitCode 0 'transaction reparse apply'
    $transactionLinkState = [IO.File]::ReadAllText((Join-Path $transactionLink.State 'state.json')) | ConvertFrom-Json
    $transactionExternal = Join-Path $tempRoot 'transaction-external-target'
    $null = New-Item -ItemType Directory -Path $transactionExternal
    $transactionSentinel = Join-Path $transactionExternal 'sentinel.txt'
    [IO.File]::WriteAllText($transactionSentinel, 'must survive')
    Remove-Item -LiteralPath $transactionLinkState.transactionPath -Recurse -Force -Confirm:$false
    $null = New-Item -ItemType Junction -Path $transactionLinkState.transactionPath -Target $transactionExternal
    $fixtureJunctions.Add([string]$transactionLinkState.transactionPath)
    $transactionBlocked = Invoke-SetupFixture Rollback $transactionLink.Profile $transactionLink.Junction $transactionLink.State $transactionLink.Canonical
    Assert-True ($transactionBlocked.ExitCode -ne 0) 'transaction directory reparse blocks rollback before access'
    Assert-Equal ([IO.File]::ReadAllText($transactionSentinel)) 'must survive' 'transaction reparse target is not deleted'
    Remove-FixtureJunction ([string]$transactionLinkState.transactionPath)

    $stateComponentLink = New-FixtureCase 'state-component-reparse'
    [IO.File]::WriteAllText($stateComponentLink.Profile, '# state component reparse fixture')
    Assert-Equal (Invoke-SetupFixture Apply $stateComponentLink.Profile $stateComponentLink.Junction $stateComponentLink.State $stateComponentLink.Canonical).ExitCode 0 'state component reparse apply'
    $stateComponentState = [IO.File]::ReadAllText((Join-Path $stateComponentLink.State 'state.json')) | ConvertFrom-Json
    $transactionsComponent = Join-Path $stateComponentLink.State 'transactions'
    $stateExternal = Join-Path $tempRoot 'state-external-target'
    $externalTransaction = Join-Path $stateExternal (Split-Path -Leaf ([string]$stateComponentState.transactionPath))
    $null = New-Item -ItemType Directory -Path $externalTransaction -Force
    $stateSentinel = Join-Path $stateExternal 'sentinel.txt'
    [IO.File]::WriteAllText($stateSentinel, 'must also survive')
    [IO.File]::WriteAllBytes((Join-Path $externalTransaction 'profile.bin'), [byte[]]@(1,2,3))
    Remove-Item -LiteralPath $transactionsComponent -Recurse -Force -Confirm:$false
    $null = New-Item -ItemType Junction -Path $transactionsComponent -Target $stateExternal
    $fixtureJunctions.Add($transactionsComponent)
    $stateComponentBlocked = Invoke-SetupFixture Rollback $stateComponentLink.Profile $stateComponentLink.Junction $stateComponentLink.State $stateComponentLink.Canonical
    Assert-True ($stateComponentBlocked.ExitCode -ne 0) 'StatePath transaction component reparse blocks rollback before access'
    Assert-Equal ([IO.File]::ReadAllText($stateSentinel)) 'must also survive' 'StatePath reparse target is not deleted'
    Remove-FixtureJunction $transactionsComponent
    Write-Output '  ok  rollback validates paths and rejects transaction/state reparse containment escapes'

    $conflict = New-FixtureCase 'conflict'
    [IO.File]::WriteAllText($conflict.Profile, '# fixture')
    Assert-Equal (Invoke-SetupFixture Apply $conflict.Profile $conflict.Junction $conflict.State $conflict.Canonical).ExitCode 0 'conflict apply'
    [IO.File]::AppendAllText($conflict.Profile, "`r`n# independent")
    Assert-True ((Invoke-SetupFixture Rollback $conflict.Profile $conflict.Junction $conflict.State $conflict.Canonical).ExitCode -ne 0) 'profile hash conflict blocks rollback'
    Write-Output '  ok  rollback profile conflict refusal'

    $serverScript = Join-Path $tempRoot 'fake-health.py'
    $serverSource = @'
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
port=int(sys.argv[1]); mode=sys.argv[2]; complete=mode=='complete'
class H(BaseHTTPRequestHandler):
 def log_message(self,*a): pass
 def do_GET(self):
  expected='Bearer wrong-listener-token' if mode=='wrong-auth' else 'Bearer sk-dummy'
  if self.headers.get('Authorization') != expected: self.send_response(401); self.end_headers(); return
  models=['gpt-5.6-luna','gpt-5.6-terra'] + (['gpt-5.6-sol'] if complete else [])
  body=json.dumps({'data':[{'id':m} for m in models]}).encode(); self.send_response(200); self.send_header('content-type','application/json'); self.send_header('content-length',str(len(body))); self.end_headers(); self.wfile.write(body)
ThreadingHTTPServer(('127.0.0.1',port),H).serve_forever()
'@
    [IO.File]::WriteAllText($serverScript, $serverSource, (New-Object Text.UTF8Encoding($false)))
    foreach ($mode in @('complete','missing','wrong-auth')) {
        $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,0); $listener.Start(); $port = ($listener.LocalEndpoint).Port; $listener.Stop()
        $process = Start-Process -FilePath python -ArgumentList $serverScript,$port,$mode -WindowStyle Hidden -PassThru
        $gatewayProcesses.Add($process)
        for ($i=0; $i -lt 40; $i++) { try { $client=New-Object Net.Sockets.TcpClient('127.0.0.1',$port); $client.Dispose(); break } catch { Start-Sleep -Milliseconds 50 } }
        $health = Invoke-Process 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-File',$Launch,'-ValidateGatewayOnly','-GatewayBaseUrl',"http://127.0.0.1:$port")
        if ($mode -eq 'complete') { Assert-Equal $health.ExitCode 0 "complete fake gateway accepted: $($health.Output)" }
        else { Assert-True ($health.ExitCode -ne 0) 'gateway missing Sol is rejected' }
        if ($mode -eq 'complete') {
            $unapprovedLaunch = Invoke-Process 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-File',$Launch,'-GatewayBaseUrl',"http://127.0.0.1:$port")
            Assert-True ($unapprovedLaunch.ExitCode -ne 0) 'non-approved fake gateway is rejected outside explicit validation/probe mode'
        }
        Stop-Process -Id $process.Id -Force -Confirm:$false
    }

    $ownerListener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,0); $ownerListener.Start(); $ownerPort = ($ownerListener.LocalEndpoint).Port; $ownerListener.Stop()
    $ownerConfig = Join-Path $tempRoot 'approved-fixture-config.yaml'
    [IO.File]::WriteAllText($ownerConfig, 'fixture: true')
    $pythonExe = (Get-Command python).Source
    $ownerProcess = Start-Process -FilePath $pythonExe -ArgumentList $serverScript,$ownerPort,'complete','-config',$ownerConfig -WindowStyle Hidden -PassThru
    $gatewayProcesses.Add($ownerProcess)
    for ($i=0; $i -lt 40; $i++) { try { $client=New-Object Net.Sockets.TcpClient('127.0.0.1',$ownerPort); $client.Dispose(); break } catch { Start-Sleep -Milliseconds 50 } }
    $ownerBaseUrl = "http://127.0.0.1:$ownerPort"
    $ownerLaunch = Join-Path $tempRoot 'launch-approved-owner.ps1'
    $ownerLaunchText = $launchText.Replace("`$ApprovedBaseUrl = 'http://127.0.0.1:8317'", "`$ApprovedBaseUrl = '$ownerBaseUrl'")
    $ownerLaunchText = $ownerLaunchText.Replace("`$ApprovedProxyBinary = 'C:\Users\wkiri\.cli-proxy-api\cli-proxy-api.exe'", "`$ApprovedProxyBinary = '$pythonExe'")
    $ownerLaunchText = $ownerLaunchText.Replace("`$ApprovedProxyConfig = 'C:\Users\wkiri\.cli-proxy-api\config.yaml'", "`$ApprovedProxyConfig = '$ownerConfig'")
    [IO.File]::WriteAllText($ownerLaunch, $ownerLaunchText, (New-Object Text.UTF8Encoding($false)))
    $ownerValid = Invoke-Process 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-File',$ownerLaunch,'-ValidateGatewayOnly','-GatewayBaseUrl',$ownerBaseUrl)
    Assert-Equal $ownerValid.ExitCode 0 "approved listener owner executable/config accepted: $($ownerValid.Output)"
    Assert-True ((ConvertFrom-Json $ownerValid.Output).approvedOwnerVerified) 'approved owner validation is reported'

    $wrongOwnerLaunch = Join-Path $tempRoot 'launch-wrong-owner.ps1'
    [IO.File]::WriteAllText($wrongOwnerLaunch, $ownerLaunchText.Replace("`$ApprovedProxyBinary = '$pythonExe'", "`$ApprovedProxyBinary = 'C:\not-approved\proxy.exe'"), (New-Object Text.UTF8Encoding($false)))
    Assert-True ((Invoke-Process 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-File',$wrongOwnerLaunch,'-ValidateGatewayOnly','-GatewayBaseUrl',$ownerBaseUrl)).ExitCode -ne 0) 'wrong approved-port owner executable fails closed'

    $wrongConfigLaunch = Join-Path $tempRoot 'launch-wrong-config.ps1'
    [IO.File]::WriteAllText($wrongConfigLaunch, $ownerLaunchText.Replace("`$ApprovedProxyConfig = '$ownerConfig'", "`$ApprovedProxyConfig = 'C:\not-approved\config.yaml'"), (New-Object Text.UTF8Encoding($false)))
    Assert-True ((Invoke-Process 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-File',$wrongConfigLaunch,'-ValidateGatewayOnly','-GatewayBaseUrl',$ownerBaseUrl)).ExitCode -ne 0) 'wrong approved-port config argument fails closed'
    Stop-Process -Id $ownerProcess.Id -Force -Confirm:$false
    $requireExisting = Invoke-Process 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-File',$ownerLaunch,'-ValidateGatewayOnly','-RequireExistingGateway','-GatewayBaseUrl',$ownerBaseUrl)
    Assert-True ($requireExisting.ExitCode -ne 0) 'approval-gated probes cannot auto-start a closed approved gateway'

    $closedListener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback,0); $closedListener.Start(); $closedPort = ($closedListener.LocalEndpoint).Port; $closedListener.Stop()
    $closedHealth = Invoke-Process 'powershell.exe' @('-NoProfile','-ExecutionPolicy','Bypass','-File',$Launch,'-ValidateGatewayOnly','-GatewayBaseUrl',"http://127.0.0.1:$closedPort")
    Assert-True ($closedHealth.ExitCode -ne 0) 'closed non-approved gateway refuses auto-start'
    Write-Output '  ok  authenticated gateway model health and wrong-listener validation'

    $routingStateDir = Join-Path $tempRoot 'routing-state'
    $null = New-Item -ItemType Directory -Path $routingStateDir
    $missingRoutingState = Join-Path $routingStateDir 'missing.json'
    $missingAudit = Invoke-AuditFixture $fixture.Profile $fixture.Junction $fixture.Canonical $missingRoutingState
    Assert-Equal $missingAudit.ExitCode 0 "missing routing state audit succeeds: $($missingAudit.Output)"
    $missingAuditJson = $missingAudit.Output | ConvertFrom-Json
    Assert-True (-not $missingAuditJson.liveAliasRoutingVerified) 'missing routing state reports false'
    Assert-Equal $missingAuditJson.lastProbe.stateStatus 'missing' 'missing routing state is identified'

    $malformedRoutingState = Join-Path $routingStateDir 'malformed.json'
    [IO.File]::WriteAllText($malformedRoutingState, '{not-json', (New-Object Text.UTF8Encoding($false)))
    $malformedAudit = Invoke-AuditFixture $fixture.Profile $fixture.Junction $fixture.Canonical $malformedRoutingState
    Assert-Equal $malformedAudit.ExitCode 0 "malformed routing state audit succeeds: $($malformedAudit.Output)"
    $malformedAuditJson = $malformedAudit.Output | ConvertFrom-Json
    Assert-True (-not $malformedAuditJson.liveAliasRoutingVerified) 'malformed routing state reports false'
    Assert-Equal $malformedAuditJson.lastProbe.stateStatus 'malformed' 'malformed routing state is identified'

    $aliasRoutes = @(
        [ordered]@{ model='gpt-5.6-luna'; turn='initial'; scope='subagent'; agent_key='a1b2c3d4e5f6' },
        [ordered]@{ model='gpt-5.6-luna'; turn='resume'; scope='subagent'; agent_key='a1b2c3d4e5f6' },
        [ordered]@{ model='gpt-5.6-terra'; turn='initial'; scope='subagent'; agent_key='b1c2d3e4f5a6' },
        [ordered]@{ model='gpt-5.6-terra'; turn='resume'; scope='subagent'; agent_key='b1c2d3e4f5a6' },
        [ordered]@{ model='gpt-5.6-sol'; turn='initial'; scope='subagent'; agent_key='c1d2e3f4a5b6' },
        [ordered]@{ model='gpt-5.6-sol'; turn='resume'; scope='subagent'; agent_key='c1d2e3f4a5b6' }
    )
    $freshTimestamp = [DateTimeOffset]::UtcNow.ToString('o')
    $freshAliases = [ordered]@{
        timestamp_utc=$freshTimestamp; skill_version='1'; probe_schema_version=2
        claude_code_version='2.1.217'; cli_proxy_api_version=$null; probe='live-aliases'
        verified=$true; gateway_ingress_model_routing_verified=$true; upstream_provider_verified=$false
        routes=$aliasRoutes
    }
    $failedAttempt = [ordered]@{
        timestamp_utc=$freshTimestamp; skill_version='1'; probe_schema_version=2
        claude_code_version='2.1.217'; cli_proxy_api_version=$null; probe='live-alias-terra'
        verified=$false; gateway_ingress_model_routing_verified=$false; upstream_provider_verified=$false
        routes=@()
    }
    $verifiedRoutingState = Join-Path $routingStateDir 'verified.json'
    [IO.File]::WriteAllText(
        $verifiedRoutingState,
        ([ordered]@{ state_schema_version=2; last_attempt=$failedAttempt; last_successful_aliases=$freshAliases } | ConvertTo-Json -Depth 8 -Compress),
        (New-Object Text.UTF8Encoding($false))
    )
    $verifiedAudit = Invoke-AuditFixture $fixture.Profile $fixture.Junction $fixture.Canonical $verifiedRoutingState
    Assert-Equal $verifiedAudit.ExitCode 0 "verified routing state audit succeeds: $($verifiedAudit.Output)"
    $verifiedAuditJson = $verifiedAudit.Output | ConvertFrom-Json
    Assert-True $verifiedAuditJson.liveAliasRoutingVerified 'fresh exact alias matrix reports true even after a later failed probe'
    Assert-Equal $verifiedAuditJson.lastProbe.stateStatus 'verified' 'verified routing state is identified'
    Assert-Equal $verifiedAuditJson.lastProbe.probe 'live-alias-terra' 'lastProbe reports the compact direct-alias last-attempt metadata'
    Assert-True (-not $verifiedAuditJson.lastProbe.verified) 'lastProbe preserves the later failed attempt status'
    Assert-True (-not $verifiedAuditJson.lastProbe.upstreamProviderVerified) 'provider remains explicitly unverified'

    $staleAliases = [ordered]@{}
    foreach ($entry in $freshAliases.GetEnumerator()) { $staleAliases[$entry.Key] = $entry.Value }
    $staleAliases.timestamp_utc = [DateTimeOffset]::UtcNow.AddDays(-2).ToString('o')
    $staleRoutingState = Join-Path $routingStateDir 'stale.json'
    [IO.File]::WriteAllText(
        $staleRoutingState,
        ([ordered]@{ state_schema_version=2; last_attempt=$staleAliases; last_successful_aliases=$staleAliases } | ConvertTo-Json -Depth 8 -Compress),
        (New-Object Text.UTF8Encoding($false))
    )
    $staleAudit = Invoke-AuditFixture $fixture.Profile $fixture.Junction $fixture.Canonical $staleRoutingState
    Assert-Equal $staleAudit.ExitCode 0 "stale routing state audit succeeds: $($staleAudit.Output)"
    $staleAuditJson = $staleAudit.Output | ConvertFrom-Json
    Assert-True (-not $staleAuditJson.liveAliasRoutingVerified) 'stale routing state reports false'
    Assert-Equal $staleAuditJson.lastProbe.stateStatus 'stale' 'stale routing state is identified'

    Assert-True ($verifiedAudit.Output -notmatch [regex]::Escape($HOME)) 'audit output does not expose home prefix'
    Assert-True ($verifiedAudit.Output -notmatch [regex]::Escape([Environment]::UserName)) 'audit output does not expose username paths'
    Write-Output '  ok  audit routing-state validation and path redaction'

    Write-Output "`n8/8 PowerShell groups passed."
}
finally {
    foreach ($process in $gatewayProcesses) { if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -Confirm:$false } }
    foreach ($path in $fixtureJunctions) { Remove-FixtureJunction $path }
    if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force -Confirm:$false }
    foreach ($name in $envNames) {
        $snapshot = $envSnapshot[$name]
        if ($snapshot.Exists) { Set-Item "Env:$name" $snapshot.Value }
        else { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
    }
}
