[CmdletBinding()]
param(
    [string]$ProfilePath = 'C:\Users\wkiri\OneDrive\Documents\WindowsPowerShell\Microsoft.PowerShell_profile.ps1',
    [string]$JunctionPath = 'C:\Users\wkiri\.claude\skills\claudex-optimized',
    [string]$CanonicalSkillPath = 'C:\Users\wkiri\.claude\super-board-src\skills\claudex-optimized',
    [switch]$SkipProxyModelInventory
)

$ErrorActionPreference = 'Stop'
$StartMarker = '# >>> claudex-optimized managed claude-codex >>>'
$EndMarker = '# <<< claudex-optimized managed claude-codex <<<'

function Get-FullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Redact-Path([string]$Path) {
    if ([string]::IsNullOrEmpty($Path)) { return $null }
    $full = Get-FullPath $Path
    $homePath = Get-FullPath $HOME
    if ($full -ieq $homePath) { return '~' }
    if ($full.StartsWith($homePath + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        return '~\' + $full.Substring($homePath.Length + 1)
    }
    return '<outside-home>'
}

function Test-LocalPort([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $result = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(300)) { return $false }
        $client.EndConnect($result)
        return $true
    }
    catch { return $false }
    finally { $client.Dispose() }
}

$junction = [ordered]@{ exists = $false; valid = $false; target = $null; linkType = $null }
if (Test-Path -LiteralPath $JunctionPath) {
    $item = Get-Item -LiteralPath $JunctionPath -Force
    $target = $item.Target
    if ($target -is [array]) { $target = $target[0] }
    $junction.exists = $true
    $junction.target = if ($null -eq $target) { $null } else { Redact-Path $target }
    $junction.linkType = $item.LinkType
    $junction.valid = $item.PSIsContainer -and
        (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) -and
        ($item.LinkType -eq 'Junction') -and
        ($null -ne $target) -and
        ((Get-FullPath $target) -ieq (Get-FullPath $CanonicalSkillPath))
}

$profileManaged = $false
if (Test-Path -LiteralPath $ProfilePath -PathType Leaf) {
    $profileText = [System.IO.File]::ReadAllText($ProfilePath)
    $profileManaged = $profileText.Contains($StartMarker) -and $profileText.Contains($EndMarker)
}

$branch = $null
$upstream = $null
$dirty = $null
$aheadBehind = $null
if (Test-Path -LiteralPath (Join-Path $CanonicalSkillPath '..\..\.git')) {
    $branch = (& git -C $CanonicalSkillPath rev-parse --abbrev-ref HEAD 2>$null | Select-Object -First 1)
    $upstream = (& git -C $CanonicalSkillPath rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' 2>$null | Select-Object -First 1)
    $status = @(& git -C $CanonicalSkillPath status --porcelain --untracked-files=all 2>$null)
    $dirty = $status.Count -gt 0
    $counts = (& git -C $CanonicalSkillPath rev-list --left-right --count 'HEAD...@{upstream}' 2>$null | Select-Object -First 1)
    if ($null -ne $counts) { $aheadBehind = $counts.Trim() }
}

$launchPath = Join-Path $CanonicalSkillPath 'scripts\launch.ps1'
$childEnvironment = $null
if (Test-Path -LiteralPath $launchPath -PathType Leaf) {
    $probe = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $launchPath -ProbeEnvironment
    if ($LASTEXITCODE -eq 0) { $childEnvironment = $probe | ConvertFrom-Json }
}

$proxyListening = Test-LocalPort 8317
$models = [ordered]@{ queried = $false; luna = $false; terra = $false; sol = $false }
if ($proxyListening -and -not $SkipProxyModelInventory) {
    try {
        $headers = @{ Authorization = 'Bearer sk-dummy' }
        $response = Invoke-RestMethod -Uri 'http://127.0.0.1:8317/v1/models?limit=1000' -Headers $headers -TimeoutSec 2
        $ids = @($response.data | ForEach-Object { $_.id })
        $models.queried = $true
        $models.luna = $ids -contains 'gpt-5.6-luna'
        $models.terra = $ids -contains 'gpt-5.6-terra'
        $models.sol = $ids -contains 'gpt-5.6-sol'
    }
    catch {
        $models.queried = $false
    }
}

[ordered]@{
    junction = $junction
    repository = [ordered]@{ branch = $branch; upstream = $upstream; dirty = $dirty; aheadBehind = $aheadBehind }
    profileManaged = $profileManaged
    childEnvironment = $childEnvironment
    proxy = [ordered]@{ listening = $proxyListening; models = $models }
    liveAliasRoutingVerified = $false
    authOrConfigInspected = $false
} | ConvertTo-Json -Depth 8 -Compress
