[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Plan', 'Apply', 'Validate', 'Rollback')]
    [string]$Action,
    [string]$ProfilePath = 'C:\Users\wkiri\OneDrive\Documents\WindowsPowerShell\Microsoft.PowerShell_profile.ps1',
    [string]$JunctionPath = 'C:\Users\wkiri\.claude\skills\claudex-optimized',
    [string]$StatePath = 'C:\Users\wkiri\.claude\claudex-optimized',
    [string]$CanonicalSkillPath = 'C:\Users\wkiri\.claude\super-board-src\skills\claudex-optimized',
    [ValidateSet('', 'ApplyPostReplaceAclOnce', 'ApplyAndRestorePostReplaceAcl')]
    [string]$FixtureFailure = ''
)

$ErrorActionPreference = 'Stop'
$StartMarker = '# >>> claudex-optimized managed claude-codex >>>'
$EndMarker = '# <<< claudex-optimized managed claude-codex <<<'
$StateFile = Join-Path $StatePath 'state.json'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false, $true)
$ProfileRecoveryCopies = New-Object System.Collections.Generic.List[string]
$FixtureFailureCount = 0

function Get-FullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Test-PathWithin([string]$Child, [string]$Parent) {
    $childFull = Get-FullPath $Child
    $parentFull = Get-FullPath $Parent
    return $childFull.StartsWith($parentFull + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-RedactedPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return '<unavailable>' }
    $full = Get-FullPath $Path
    $homePath = Get-FullPath $HOME
    if ($full -ieq $homePath) { return '~' }
    if ($full.StartsWith($homePath + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        return '~\' + $full.Substring($homePath.Length + 1)
    }
    return '<outside-home>\' + (Split-Path -Leaf $full)
}

function Get-RedactedMessage([string]$Message) {
    $value = [string]$Message
    if (-not [string]::IsNullOrWhiteSpace($HOME)) { $value = $value.Replace($HOME, '~') }
    if (-not [string]::IsNullOrWhiteSpace([Environment]::UserName)) { $value = $value.Replace([Environment]::UserName, '<user>') }
    return $value
}

function Get-ManualRecoveryLocation {
    return "transaction state: $(Get-RedactedPath $StatePath); File.Replace copies: $(Get-RedactedPath (Split-Path -Parent $ProfilePath))"
}

function Assert-NoReparsePoints([string]$Path, [string]$Label) {
    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($full)
    $current = $root
    $relative = $full.Substring($root.Length)
    foreach ($segment in @($relative -split '[\\/]' | Where-Object { $_ -ne '' })) {
        $current = Join-Path $current $segment
        $item = Get-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue
        if ($null -ne $item -and (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw "$Label contains a reparse point and is not safe to access: $(Get-RedactedPath $current)"
        }
    }
}

function Get-ByteHash([byte[]]$Bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([System.BitConverter]::ToString($sha.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-EncodingDocument {
    $exists = Test-Path -LiteralPath $ProfilePath -PathType Leaf
    $bytes = if ($exists) { [System.IO.File]::ReadAllBytes($ProfilePath) } else { [byte[]]@() }
    $encoding = $Utf8NoBom
    $preambleLength = 0
    if ($bytes.Length -ge 4 -and $bytes[0] -eq 0x00 -and $bytes[1] -eq 0x00 -and $bytes[2] -eq 0xFE -and $bytes[3] -eq 0xFF) {
        $encoding = New-Object System.Text.UTF32Encoding($true, $true); $preambleLength = 4
    }
    elseif ($bytes.Length -ge 4 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE -and $bytes[2] -eq 0x00 -and $bytes[3] -eq 0x00) {
        $encoding = New-Object System.Text.UTF32Encoding($false, $true); $preambleLength = 4
    }
    elseif ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        $encoding = New-Object System.Text.UTF8Encoding($true, $true); $preambleLength = 3
    }
    elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) {
        $encoding = New-Object System.Text.UnicodeEncoding($true, $true, $true); $preambleLength = 2
    }
    elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) {
        $encoding = New-Object System.Text.UnicodeEncoding($false, $true, $true); $preambleLength = 2
    }
    $text = if ($bytes.Length -eq $preambleLength) { '' } else { $encoding.GetString($bytes, $preambleLength, $bytes.Length - $preambleLength) }
    $aclSddl = if ($exists) { (Get-Acl -LiteralPath $ProfilePath).Sddl } else { $null }
    return [pscustomobject]@{ Exists = $exists; Bytes = $bytes; Text = $text; Encoding = $encoding; PreambleLength = $preambleLength; AclSddl = $aclSddl }
}

function Convert-TextToBytes([string]$Text, [System.Text.Encoding]$Encoding, [int]$PreambleLength) {
    $body = $Encoding.GetBytes($Text)
    if ($PreambleLength -eq 0) { return $body }
    $preamble = $Encoding.GetPreamble()
    $combined = New-Object byte[] ($preamble.Length + $body.Length)
    [System.Array]::Copy($preamble, 0, $combined, 0, $preamble.Length)
    [System.Array]::Copy($body, 0, $combined, $preamble.Length, $body.Length)
    return $combined
}

function Set-FileAclSddl([string]$Path, [string]$Sddl) {
    if ([string]::IsNullOrEmpty($Sddl)) { return }
    if ((Get-FullPath $Path) -ieq (Get-FullPath $ProfilePath) -and $FixtureFailure -ne '') {
        $shouldFail = $FixtureFailure -eq 'ApplyAndRestorePostReplaceAcl' -or ($FixtureFailure -eq 'ApplyPostReplaceAclOnce' -and $script:FixtureFailureCount -eq 0)
        if ($shouldFail) {
            $script:FixtureFailureCount++
            throw 'Fixture-injected post-replace ACL failure.'
        }
    }
    $security = New-Object System.Security.AccessControl.FileSecurity
    $security.SetSecurityDescriptorSddlForm($Sddl)
    Set-Acl -LiteralPath $Path -AclObject $security
}

function Test-FileState([string]$Path, [byte[]]$ExpectedBytes, [string]$ExpectedAclSddl, [bool]$ExpectedExists = $true) {
    if (-not $ExpectedExists) { return -not (Test-Path -LiteralPath $Path) }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    if ((Get-ByteHash ([System.IO.File]::ReadAllBytes($Path))) -ne (Get-ByteHash $ExpectedBytes)) { return $false }
    if (-not [string]::IsNullOrEmpty($ExpectedAclSddl) -and (Get-Acl -LiteralPath $Path).Sddl -ne $ExpectedAclSddl) { return $false }
    return $true
}

function Assert-FileState([string]$Path, [byte[]]$ExpectedBytes, [string]$ExpectedAclSddl, [bool]$ExpectedExists = $true) {
    if (-not (Test-FileState $Path $ExpectedBytes $ExpectedAclSddl $ExpectedExists)) {
        throw 'Byte-hash or ACL verification failed after atomic profile operation.'
    }
}

function Remove-ProfileRecoveryCopies {
    $parent = Get-FullPath (Split-Path -Parent $ProfilePath)
    foreach ($path in @($ProfileRecoveryCopies | Select-Object -Unique)) {
        $full = Get-FullPath $path
        if (-not (Test-PathWithin $full $parent)) { throw 'Refusing to remove a recovery copy outside the profile directory.' }
        $name = Split-Path -Leaf $full
        if ($name -notmatch '^\.claudex-profile-[a-f0-9]+\.(tmp|bak)$') { throw 'Refusing to remove an unrecognized profile recovery copy.' }
        $item = Get-Item -LiteralPath $full -Force -ErrorAction SilentlyContinue
        if ($null -ne $item) {
            if ($item.PSIsContainer -or (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) { throw 'Refusing unsafe profile recovery cleanup.' }
            [System.IO.File]::Delete($full)
        }
    }
    $ProfileRecoveryCopies.Clear()
}

function Write-BytesAtomic([byte[]]$Bytes, [string]$AclSddl) {
    $parent = Split-Path -Parent $ProfilePath
    if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw "Profile parent does not exist: $(Get-RedactedPath $parent)" }
    $token = [guid]::NewGuid().ToString('N')
    $temp = Join-Path $parent ('.claudex-profile-' + $token + '.tmp')
    $replaceBackup = Join-Path $parent ('.claudex-profile-' + $token + '.bak')
    try {
        [System.IO.File]::WriteAllBytes($temp, $Bytes)
        Set-FileAclSddl $temp $AclSddl
        if (Test-Path -LiteralPath $ProfilePath -PathType Leaf) { [System.IO.File]::Replace($temp, $ProfilePath, $replaceBackup, $true) }
        else { [System.IO.File]::Move($temp, $ProfilePath) }
        Set-FileAclSddl $ProfilePath $AclSddl
        Assert-FileState $ProfilePath $Bytes $AclSddl $true
        if (Test-Path -LiteralPath $temp) { [System.IO.File]::Delete($temp) }
        if (Test-Path -LiteralPath $replaceBackup) { [System.IO.File]::Delete($replaceBackup) }
    }
    catch {
        if (Test-Path -LiteralPath $temp) { $ProfileRecoveryCopies.Add($temp) }
        if (Test-Path -LiteralPath $replaceBackup) { $ProfileRecoveryCopies.Add($replaceBackup) }
        throw
    }
}

function Remove-ProfileAtomic {
    if (-not (Test-Path -LiteralPath $ProfilePath -PathType Leaf)) { return }
    $parent = Split-Path -Parent $ProfilePath
    $temp = Join-Path $parent ('.claudex-profile-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [System.IO.File]::Move($ProfilePath, $temp)
        if (Test-Path -LiteralPath $ProfilePath) { throw 'Profile removal verification failed.' }
        [System.IO.File]::Delete($temp)
    }
    catch {
        if (Test-Path -LiteralPath $temp) { $ProfileRecoveryCopies.Add($temp) }
        throw
    }
}

function Get-Newline([string]$Text) { if ($Text.Contains("`r`n")) { return "`r`n" }; return "`n" }

function Get-DesiredBlock([string]$Newline) {
    $launchPath = Join-Path $JunctionPath 'scripts\launch.ps1'
    $escaped = $launchPath.Replace("'", "''")
    return @(
        $StartMarker,
        'function claude-codex {',
        '    $argvJson = ConvertTo-Json -InputObject @($args) -Compress',
        '    $encodedArgv = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($argvJson))',
        "    `$launch = '$escaped'",
        "    `$launchArgs = @('-EncodedArgv', `$encodedArgv)",
        "    if (`$env:CLAUDEX_OPTIMIZED_PROBE_ARGV -eq '1') { `$launchArgs = @('-ProbeArgvRoundTrip') + `$launchArgs }",
        '    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $launch @launchArgs',
        '    $global:LASTEXITCODE = $LASTEXITCODE',
        '}',
        $EndMarker
    ) -join $Newline
}

function Parse-Profile([string]$Text) {
    $tokens = $null; $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($Text, [ref]$tokens, [ref]$parseErrors)
    if ($parseErrors.Count -gt 0) { throw ('PowerShell profile has parse errors: ' + (($parseErrors | ForEach-Object { $_.Message }) -join '; ')) }
    $startMatches = [regex]::Matches($Text, [regex]::Escape($StartMarker))
    $endMatches = [regex]::Matches($Text, [regex]::Escape($EndMarker))
    if ($startMatches.Count -gt 1 -or $endMatches.Count -gt 1) { throw 'Duplicate claudex managed markers are not allowed.' }
    if (($startMatches.Count -eq 1) -xor ($endMatches.Count -eq 1)) { throw 'Malformed claudex managed marker pair.' }
    $functions = @($ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ieq 'claude-codex' }, $true))
    if ($functions.Count -gt 1) { throw 'Duplicate claude-codex function definitions are not allowed.' }
    if ($startMatches.Count -eq 1) {
        $start = $startMatches[0].Index; $end = $endMatches[0].Index + $EndMarker.Length
        if ($endMatches[0].Index -le $start) { throw 'Managed markers are out of order.' }
        if ($functions.Count -ne 1) { throw 'Managed markers must contain exactly one claude-codex function.' }
        $function = $functions[0]
        if (@($ast.EndBlock.Statements | Where-Object { $_ -eq $function }).Count -ne 1) { throw 'Managed claude-codex must be a direct top-level profile statement.' }
        if ($function.Extent.StartOffset -lt $start -or $function.Extent.EndOffset -gt $end) { throw 'claude-codex is outside the managed marker block.' }
        $innerStart = $startMatches[0].Index + $StartMarker.Length
        $inner = $Text.Substring($innerStart, $endMatches[0].Index - $innerStart)
        $innerTokens = $null; $innerErrors = $null
        $innerAst = [System.Management.Automation.Language.Parser]::ParseInput($inner, [ref]$innerTokens, [ref]$innerErrors)
        if ($innerErrors.Count -gt 0 -or $innerAst.EndBlock.Statements.Count -ne 1 -or -not ($innerAst.EndBlock.Statements[0] -is [System.Management.Automation.Language.FunctionDefinitionAst])) {
            throw 'Managed marker block contains ambiguous executable content.'
        }
        return [pscustomobject]@{ Start = $start; Length = $end - $start; Text = $Text.Substring($start, $end - $start); Kind = 'managed' }
    }
    if ($functions.Count -eq 1) {
        $function = $functions[0]
        if (@($ast.EndBlock.Statements | Where-Object { $_ -eq $function }).Count -ne 1) {
            throw 'Legacy claude-codex is accepted only as a direct top-level profile statement.'
        }
        return [pscustomobject]@{ Start = $function.Extent.StartOffset; Length = $function.Extent.EndOffset - $function.Extent.StartOffset; Text = $function.Extent.Text; Kind = 'legacy' }
    }
    return $null
}

function Get-JunctionInfo {
    if (-not (Test-Path -LiteralPath $JunctionPath)) { return [pscustomobject]@{ Exists = $false; Valid = $false; Target = $null } }
    $item = Get-Item -LiteralPath $JunctionPath -Force; $target = $item.Target
    if ($target -is [array]) { $target = $target[0] }
    $valid = $item.PSIsContainer -and (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) -and ($item.LinkType -eq 'Junction') -and ($null -ne $target) -and ((Get-FullPath $target) -ieq (Get-FullPath $CanonicalSkillPath))
    return [pscustomobject]@{ Exists = $true; Valid = $valid; Target = $target; LinkType = $item.LinkType }
}

function Assert-KnownJunction { $info = Get-JunctionInfo; if ($info.Exists -and -not $info.Valid) { throw "Refusing unknown existing path at $(Get-RedactedPath $JunctionPath)" }; return $info }
function Remove-OwnedJunction { $info = Assert-KnownJunction; if ($info.Exists) { [System.IO.Directory]::Delete($JunctionPath, $false) } }

function Ensure-StateDirectory {
    Assert-NoReparsePoints $StatePath 'StatePath'
    if (-not (Test-Path -LiteralPath $StatePath -PathType Container)) { $null = New-Item -ItemType Directory -Path $StatePath }
    Assert-NoReparsePoints $StatePath 'StatePath'
}

function Write-State([object]$State) {
    Ensure-StateDirectory
    Assert-NoReparsePoints $StateFile 'StateFile'
    $token = [guid]::NewGuid().ToString('N')
    $temp = Join-Path $StatePath ('.state-' + $token + '.tmp')
    $replaceBackup = Join-Path $StatePath ('.state-' + $token + '.bak')
    $bytes = $Utf8NoBom.GetBytes(($State | ConvertTo-Json -Depth 8))
    try {
        [System.IO.File]::WriteAllBytes($temp, $bytes)
        if (Test-Path -LiteralPath $StateFile -PathType Leaf) { [System.IO.File]::Replace($temp, $StateFile, $replaceBackup, $true) }
        else { [System.IO.File]::Move($temp, $StateFile) }
        if ((Get-ByteHash ([System.IO.File]::ReadAllBytes($StateFile))) -ne (Get-ByteHash $bytes)) { throw 'State byte-hash verification failed.' }
        if (Test-Path -LiteralPath $temp) { [System.IO.File]::Delete($temp) }
        if (Test-Path -LiteralPath $replaceBackup) { [System.IO.File]::Delete($replaceBackup) }
    }
    catch { throw }
}

function Remove-SafeStateFile {
    Assert-NoReparsePoints $StateFile 'StateFile'
    $item = Get-Item -LiteralPath $StateFile -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return }
    if ($item.PSIsContainer -or (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) { throw 'Refusing unsafe state-file cleanup.' }
    [System.IO.File]::Delete($StateFile)
}

function Remove-OwnedStateArtifacts {
    Assert-NoReparsePoints $StatePath 'StatePath'
    if (-not (Test-Path -LiteralPath $StatePath -PathType Container)) { return }
    foreach ($item in @(Get-ChildItem -LiteralPath $StatePath -Force)) {
        if ($item.Name -match '^\.state-[a-f0-9]+\.(tmp|bak)$') {
            if ($item.PSIsContainer -or (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) { throw 'Refusing unsafe state recovery cleanup.' }
            [System.IO.File]::Delete($item.FullName)
        }
    }
}

function Remove-OwnedTransactionTree([string]$TransactionPath) {
    if (-not (Test-PathWithin $TransactionPath $StatePath)) { throw 'Refusing transaction cleanup outside StatePath.' }
    Assert-NoReparsePoints $StatePath 'StatePath'
    Assert-NoReparsePoints $TransactionPath 'transactionPath'
    $item = Get-Item -LiteralPath $TransactionPath -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return }
    if (-not $item.PSIsContainer -or (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) { throw 'Refusing unsafe transaction cleanup.' }
    $children = @(Get-ChildItem -LiteralPath $TransactionPath -Force)
    foreach ($child in $children) {
        if ($child.Name -cne 'profile.bin' -or $child.PSIsContainer -or (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw 'Transaction directory contains an unexpected or unsafe entry; preserving it for manual recovery.'
        }
    }
    foreach ($child in $children) { [System.IO.File]::Delete($child.FullName) }
    [System.IO.Directory]::Delete($TransactionPath, $false)
    $transactionsPath = Split-Path -Parent $TransactionPath
    Assert-NoReparsePoints $transactionsPath 'transactionsPath'
    if ((Test-Path -LiteralPath $transactionsPath -PathType Container) -and @(Get-ChildItem -LiteralPath $transactionsPath -Force).Count -eq 0) {
        [System.IO.Directory]::Delete($transactionsPath, $false)
    }
}

function Write-Result([object]$Value) { $Value | ConvertTo-Json -Depth 8 -Compress }

if ($FixtureFailure -ne '' -and (
    (Get-FullPath $ProfilePath) -ieq (Get-FullPath 'C:\Users\wkiri\OneDrive\Documents\WindowsPowerShell\Microsoft.PowerShell_profile.ps1') -or
    (Get-FullPath $JunctionPath) -ieq (Get-FullPath 'C:\Users\wkiri\.claude\skills\claudex-optimized') -or
    (Get-FullPath $StatePath) -ieq (Get-FullPath 'C:\Users\wkiri\.claude\claudex-optimized')
)) {
    throw 'Fixture failure injection is forbidden for the real setup paths.'
}
if (-not (Test-Path -LiteralPath $CanonicalSkillPath -PathType Container)) { throw "Canonical skill path does not exist: $(Get-RedactedPath $CanonicalSkillPath)" }
$junction = Assert-KnownJunction
$document = Get-EncodingDocument
$existingBlock = Parse-Profile $document.Text
$newline = Get-Newline $document.Text
$desiredBlock = Get-DesiredBlock $newline

if ($Action -eq 'Plan') {
    Write-Result ([ordered]@{ action = 'plan'; profilePath = Get-RedactedPath $ProfilePath; profileBlock = if ($null -eq $existingBlock) { 'append' } else { "replace-$($existingBlock.Kind)" }; junctionPath = Get-RedactedPath $JunctionPath; canonicalSkillPath = Get-RedactedPath $CanonicalSkillPath; createJunction = -not $junction.Exists; preservesEncodingBomAcl = $true; modifiesAuthOrConfig = $false; gitOperation = $false })
    exit 0
}
if ($Action -eq 'Validate') {
    if (-not $junction.Exists -or -not $junction.Valid) { throw 'Expected exact claudex-optimized directory junction.' }
    if ($null -eq $existingBlock -or $existingBlock.Kind -ne 'managed') { throw 'Managed claude-codex profile block is missing.' }
    if ($existingBlock.Text -cne $desiredBlock) { throw 'Managed profile block differs from the tracked launcher wrapper.' }
    Write-Result ([ordered]@{ valid = $true; junctionTarget = Get-RedactedPath $junction.Target; profileManaged = $true }); exit 0
}
if ($Action -eq 'Apply') {
    Assert-NoReparsePoints $StatePath 'StatePath'
    Assert-NoReparsePoints $StateFile 'StateFile'
    if (Test-Path -LiteralPath $StateFile -PathType Leaf) { throw "Existing setup state must be rolled back first: $(Get-RedactedPath $StateFile)" }
    $appendSeparator = ''
    if ($null -ne $existingBlock) { $appliedText = $document.Text.Remove($existingBlock.Start, $existingBlock.Length).Insert($existingBlock.Start, $desiredBlock) }
    elseif ($document.Text.Length -eq 0) { $appliedText = $desiredBlock }
    else { $appendSeparator = if ($document.Text.EndsWith($newline)) { $newline } else { $newline + $newline }; $appliedText = $document.Text + $appendSeparator + $desiredBlock }
    $appliedBytes = Convert-TextToBytes $appliedText $document.Encoding $document.PreambleLength
    $createdJunction = -not $junction.Exists
    $transactionId = [guid]::NewGuid().ToString('N')
    Ensure-StateDirectory
    $transactionsPath = Join-Path $StatePath 'transactions'
    Assert-NoReparsePoints $transactionsPath 'transactionsPath'
    if (-not (Test-Path -LiteralPath $transactionsPath -PathType Container)) { $null = New-Item -ItemType Directory -Path $transactionsPath }
    Assert-NoReparsePoints $transactionsPath 'transactionsPath'
    $transactionPath = Join-Path $transactionsPath $transactionId
    $backupPath = Join-Path $transactionPath 'profile.bin'
    Assert-NoReparsePoints $transactionPath 'transactionPath'
    $null = New-Item -ItemType Directory -Path $transactionPath
    Assert-NoReparsePoints $transactionPath 'transactionPath'
    Assert-NoReparsePoints $backupPath 'backupPath'
    [System.IO.File]::WriteAllBytes($backupPath, $document.Bytes)
    if ((Get-ByteHash ([System.IO.File]::ReadAllBytes($backupPath))) -ne (Get-ByteHash $document.Bytes)) { throw 'Transaction backup verification failed.' }
    $state = [ordered]@{ version = 2; phase = 'pending'; profilePath = Get-FullPath $ProfilePath; junctionPath = Get-FullPath $JunctionPath; canonicalSkillPath = Get-FullPath $CanonicalSkillPath; transactionPath = Get-FullPath $transactionPath; backupPath = Get-FullPath $backupPath; profileExisted = $document.Exists; profileBeforeHash = Get-ByteHash $document.Bytes; profileAppliedHash = Get-ByteHash $appliedBytes; backupHash = Get-ByteHash $document.Bytes; originalAclSddl = $document.AclSddl; junctionCreatedByApply = $createdJunction }
    $junctionMutationStarted = $false
    $profileMutationStarted = $false
    try {
        Write-State $state
        if ($createdJunction) {
            $parent = Split-Path -Parent $JunctionPath
            if (-not (Test-Path -LiteralPath $parent -PathType Container)) { throw "Junction parent does not exist: $(Get-RedactedPath $parent)" }
            $null = New-Item -ItemType Junction -Path $JunctionPath -Target $CanonicalSkillPath
            $junctionMutationStarted = $true
            if (-not (Assert-KnownJunction).Valid) { throw 'Created junction failed validation.' }
        }
        $profileMutationStarted = $true
        Write-BytesAtomic $appliedBytes $document.AclSddl
        Assert-FileState $ProfilePath $appliedBytes $document.AclSddl $true
        $state.phase = 'applied'; Write-State $state
    }
    catch {
        $applyError = $_
        $restoreError = $null
        try {
            if ($profileMutationStarted -and -not (Test-FileState $ProfilePath $document.Bytes $document.AclSddl $document.Exists)) {
                if ($document.Exists) { Write-BytesAtomic $document.Bytes $document.AclSddl } else { Remove-ProfileAtomic }
            }
            Assert-FileState $ProfilePath $document.Bytes $document.AclSddl $document.Exists
        }
        catch { $restoreError = $_ }
        if ($null -ne $restoreError) {
            $location = Get-ManualRecoveryLocation
            throw "Apply failed and automatic restoration also failed. All transaction state and recovery copies were preserved for manual recovery ($location). Apply error: $(Get-RedactedMessage $applyError.Exception.Message) Restore error: $(Get-RedactedMessage $restoreError.Exception.Message)"
        }
        try {
            if ($junctionMutationStarted -and (Test-Path -LiteralPath $JunctionPath)) { Remove-OwnedJunction }
            Remove-ProfileRecoveryCopies
            Remove-OwnedTransactionTree $transactionPath
            Remove-OwnedStateArtifacts
            Remove-SafeStateFile
        }
        catch {
            throw "Apply failed but the original profile was restored and verified. Verified cleanup could not finish; recovery artifacts remain ($(Get-ManualRecoveryLocation)). Cleanup error: $(Get-RedactedMessage $_.Exception.Message)"
        }
        throw $applyError
    }
    Write-Result ([ordered]@{ applied = $true; junctionCreated = $createdJunction; profileManaged = $true; transaction = $transactionId }); exit 0
}
if ($Action -eq 'Rollback') {
    Assert-NoReparsePoints $StatePath 'StatePath'
    Assert-NoReparsePoints $StateFile 'StateFile'
    if (-not (Test-Path -LiteralPath $StateFile -PathType Leaf)) { throw "No claudex-optimized setup state exists at $(Get-RedactedPath $StateFile)" }
    $state = [System.IO.File]::ReadAllText($StateFile) | ConvertFrom-Json
    if ($state.version -ne 2 -or $state.phase -notin @('pending', 'applied')) { throw 'Rollback state version or phase is invalid.' }
    if ((Get-FullPath $ProfilePath) -ine $state.profilePath -or (Get-FullPath $JunctionPath) -ine $state.junctionPath -or (Get-FullPath $CanonicalSkillPath) -ine $state.canonicalSkillPath) { throw 'Rollback fixture paths do not match the recorded apply transaction.' }
    if (-not (Test-PathWithin ([string]$state.transactionPath) $StatePath) -or -not (Test-PathWithin ([string]$state.backupPath) ([string]$state.transactionPath))) { throw 'Rollback state points outside its transaction directory.' }
    if ((Split-Path -Leaf ([string]$state.backupPath)) -cne 'profile.bin') { throw 'Rollback backup filename is invalid.' }
    Assert-NoReparsePoints ([string]$state.transactionPath) 'transactionPath'
    Assert-NoReparsePoints ([string]$state.backupPath) 'backupPath'
    if (-not (Test-Path -LiteralPath $state.backupPath -PathType Leaf)) { throw 'Rollback backup is missing.' }
    $backupBytes = [System.IO.File]::ReadAllBytes($state.backupPath)
    if ((Get-ByteHash $backupBytes) -ne $state.backupHash) { throw 'Rollback backup hash mismatch.' }
    $currentExists = Test-Path -LiteralPath $ProfilePath -PathType Leaf
    $currentBytes = if ($currentExists) { [System.IO.File]::ReadAllBytes($ProfilePath) } else { [byte[]]@() }
    $currentHash = Get-ByteHash $currentBytes
    $profileIsBefore = if ($state.profileExisted) { $currentExists -and $currentHash -eq $state.profileBeforeHash } else { -not $currentExists }
    $profileIsApplied = $currentExists -and $currentHash -eq $state.profileAppliedHash
    if (-not $profileIsBefore -and -not $profileIsApplied) {
        if ($state.phase -eq 'pending') { throw 'Rollback refused: profile changed independently during pending apply.' }
        throw 'Rollback refused: profile changed independently after apply.'
    }
    if ($state.junctionCreatedByApply) {
        $rollbackJunction = Get-JunctionInfo
        if ($rollbackJunction.Exists -and -not $rollbackJunction.Valid) { throw 'Rollback refused: transaction-created junction was replaced by an unknown path.' }
    }
    try {
        if (-not $profileIsBefore) {
            if ($state.profileExisted) { Write-BytesAtomic $backupBytes ([string]$state.originalAclSddl) } else { Remove-ProfileAtomic }
        }
        Assert-FileState $ProfilePath $backupBytes ([string]$state.originalAclSddl) ([bool]$state.profileExisted)
    }
    catch {
        throw "Rollback restoration failed. Transaction state and every recovery copy were preserved for manual recovery ($(Get-ManualRecoveryLocation)). Restore error: $(Get-RedactedMessage $_.Exception.Message)"
    }
    if ($state.junctionCreatedByApply -and (Test-Path -LiteralPath $JunctionPath)) { Remove-OwnedJunction }
    Remove-ProfileRecoveryCopies
    Remove-OwnedTransactionTree ([string]$state.transactionPath)
    Remove-OwnedStateArtifacts
    Remove-SafeStateFile
    Write-Result ([ordered]@{ rolledBack = $true; profileRestored = $true; junctionRemoved = [bool]$state.junctionCreatedByApply }); exit 0
}
