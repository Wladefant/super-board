[CmdletBinding()]
param(
    [string]$ProfilePath = 'C:\Users\wkiri\OneDrive\Documents\WindowsPowerShell\Microsoft.PowerShell_profile.ps1',
    [string]$JunctionPath = 'C:\Users\wkiri\.claude\skills\claudex-optimized',
    [string]$CanonicalSkillPath = 'C:\Users\wkiri\.claude\super-board-src\skills\claudex-optimized',
    [string]$RoutingStatePath = (Join-Path $HOME '.claude\claudex-optimized\last-routing-probe.json'),
    [double]$RoutingStateMaxAgeHours = 24,
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

function Get-ObjectProperty($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Get-LastProbeMetadata($Attempt, [string]$StateStatus) {
    $metadata = [ordered]@{ stateStatus = $StateStatus }
    if ($null -eq $Attempt) { return $metadata }
    $timestampText = Get-ObjectProperty $Attempt 'timestamp_utc'
    $timestamp = [DateTimeOffset]::MinValue
    if ($timestampText -is [string] -and [DateTimeOffset]::TryParse(
        $timestampText,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal,
        [ref]$timestamp
    )) { $metadata.timestampUtc = $timestamp.ToUniversalTime().ToString('o') }
    $probe = Get-ObjectProperty $Attempt 'probe'
    if ($probe -in @('live-main-sol','live-stable-control','live-alias-luna','live-alias-terra','live-alias-sol','live-aliases')) { $metadata.probe = $probe }
    foreach ($mapping in @(
        @{ Source='verified'; Target='verified' },
        @{ Source='gateway_ingress_model_routing_verified'; Target='gatewayIngressModelRoutingVerified' },
        @{ Source='upstream_provider_verified'; Target='upstreamProviderVerified' }
    )) {
        $value = Get-ObjectProperty $Attempt $mapping.Source
        if ($value -is [bool]) { $metadata[$mapping.Target] = $value }
    }
    if ((Get-ObjectProperty $Attempt 'skill_version') -ceq '1') { $metadata.skillVersion = '1' }
    if ((Get-ObjectProperty $Attempt 'probe_schema_version') -eq 2) { $metadata.probeSchemaVersion = 2 }
    foreach ($mapping in @(
        @{ Source='claude_code_version'; Target='claudeCodeVersion' },
        @{ Source='cli_proxy_api_version'; Target='cliProxyApiVersion' }
    )) {
        $value = Get-ObjectProperty $Attempt $mapping.Source
        if ($null -eq $value) { $metadata[$mapping.Target] = $null }
        elseif ($value -is [string] -and $value -cmatch '^\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.-]+)?$') {
            $metadata[$mapping.Target] = $value
        }
    }
    return $metadata
}

function Get-RoutingProbeStatus([string]$Path, [double]$MaxAgeHours) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{ Verified = $false; LastProbe = (Get-LastProbeMetadata $null 'missing') }
    }
    try {
        $state = [System.IO.File]::ReadAllText($Path) | ConvertFrom-Json
    }
    catch {
        return [pscustomobject]@{ Verified = $false; LastProbe = (Get-LastProbeMetadata $null 'malformed') }
    }
    if ((Get-ObjectProperty $state 'state_schema_version') -ne 2) {
        return [pscustomobject]@{ Verified = $false; LastProbe = (Get-LastProbeMetadata $null 'malformed') }
    }
    $attempt = Get-ObjectProperty $state 'last_attempt'
    $aliases = Get-ObjectProperty $state 'last_successful_aliases'
    if ($null -eq $aliases) {
        return [pscustomobject]@{ Verified = $false; LastProbe = (Get-LastProbeMetadata $attempt 'missing-success') }
    }
    if ((Get-ObjectProperty $aliases 'probe') -cne 'live-aliases' -or
        (Get-ObjectProperty $aliases 'skill_version') -cne '1' -or
        (Get-ObjectProperty $aliases 'probe_schema_version') -ne 2 -or
        (Get-ObjectProperty $aliases 'verified') -isnot [bool] -or
        -not (Get-ObjectProperty $aliases 'verified') -or
        (Get-ObjectProperty $aliases 'gateway_ingress_model_routing_verified') -isnot [bool] -or
        -not (Get-ObjectProperty $aliases 'gateway_ingress_model_routing_verified') -or
        (Get-ObjectProperty $aliases 'upstream_provider_verified') -isnot [bool] -or
        (Get-ObjectProperty $aliases 'upstream_provider_verified')) {
        return [pscustomobject]@{ Verified = $false; LastProbe = (Get-LastProbeMetadata $attempt 'malformed') }
    }
    $timestampText = Get-ObjectProperty $aliases 'timestamp_utc'
    $timestamp = [DateTimeOffset]::MinValue
    if ($timestampText -isnot [string] -or -not [DateTimeOffset]::TryParse(
        $timestampText,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal,
        [ref]$timestamp
    )) {
        return [pscustomobject]@{ Verified = $false; LastProbe = (Get-LastProbeMetadata $attempt 'malformed') }
    }
    $age = [DateTimeOffset]::UtcNow - $timestamp
    if ($age.TotalMinutes -lt -5 -or $age.TotalHours -gt $MaxAgeHours) {
        return [pscustomobject]@{ Verified = $false; LastProbe = (Get-LastProbeMetadata $attempt 'stale') }
    }
    $routes = @(Get-ObjectProperty $aliases 'routes')
    if ($routes.Count -ne 6) {
        return [pscustomobject]@{ Verified = $false; LastProbe = (Get-LastProbeMetadata $attempt 'malformed') }
    }
    $expected = @{}
    foreach ($model in @('gpt-5.6-luna','gpt-5.6-terra','gpt-5.6-sol')) {
        foreach ($turn in @('initial','resume')) { $expected[($model + '|' + $turn)] = $true }
    }
    $seen = @{}
    $agentKeys = @{}
    foreach ($route in $routes) {
        $model = Get-ObjectProperty $route 'model'
        $turn = Get-ObjectProperty $route 'turn'
        $scope = Get-ObjectProperty $route 'scope'
        $agentKey = Get-ObjectProperty $route 'agent_key'
        $routeKey = [string]$model + '|' + [string]$turn
        if (-not $expected.ContainsKey($routeKey) -or $seen.ContainsKey($routeKey) -or $scope -cne 'subagent' -or
            $agentKey -isnot [string] -or $agentKey -cnotmatch '^[0-9a-f]{12}$') {
            return [pscustomobject]@{ Verified = $false; LastProbe = (Get-LastProbeMetadata $attempt 'malformed') }
        }
        $seen[$routeKey] = $true
        if ($agentKeys.ContainsKey($model) -and $agentKeys[$model] -cne $agentKey) {
            return [pscustomobject]@{ Verified = $false; LastProbe = (Get-LastProbeMetadata $attempt 'malformed') }
        }
        $agentKeys[$model] = $agentKey
    }
    if ($seen.Count -ne $expected.Count -or $agentKeys.Count -ne 3 -or @($agentKeys.Values | Sort-Object -Unique).Count -ne 3) {
        return [pscustomobject]@{ Verified = $false; LastProbe = (Get-LastProbeMetadata $attempt 'malformed') }
    }
    return [pscustomobject]@{ Verified = $true; LastProbe = (Get-LastProbeMetadata $attempt 'verified') }
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

$routingProbe = Get-RoutingProbeStatus $RoutingStatePath $RoutingStateMaxAgeHours
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
    liveAliasRoutingVerified = [bool]$routingProbe.Verified
    lastProbe = $routingProbe.LastProbe
    authOrConfigInspected = $false
} | ConvertTo-Json -Depth 8 -Compress
