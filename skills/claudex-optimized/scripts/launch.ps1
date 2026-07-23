[CmdletBinding(PositionalBinding = $false)]
param(
    [switch]$ProbeEnvironment,
    [switch]$ProbeArgvRoundTrip,
    [switch]$ValidateGatewayOnly,
    [switch]$ProbeGateway,
    [switch]$RequireExistingGateway,
    [string]$EncodedArgv,
    [ValidateSet('', 'gpt-5.6-luna')]
    [string]$StableSubagentModel = '',
    [string]$GatewayBaseUrl = 'http://127.0.0.1:8317',
    [int]$HealthTimeoutSeconds = 20,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ClaudeArgs
)

$ErrorActionPreference = 'Stop'
$ApprovedBaseUrl = 'http://127.0.0.1:8317'
$ApprovedProxyBinary = 'C:\Users\wkiri\.cli-proxy-api\cli-proxy-api.exe'
$ApprovedProxyConfig = 'C:\Users\wkiri\.cli-proxy-api\config.yaml'
$ExpectedModels = @('gpt-5.6-luna', 'gpt-5.6-terra', 'gpt-5.6-sol')

$remove = @(
    'CLAUDE_CODE_SUBAGENT_MODEL',
    'ANTHROPIC_DEFAULT_FABLE_MODEL',
    'ANTHROPIC_API_KEY',
    'ANTHROPIC_MODEL',
    'ANTHROPIC_SMALL_FAST_MODEL',
    'CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS'
)
foreach ($name in $remove) { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }

$env:ANTHROPIC_BASE_URL = $GatewayBaseUrl
$env:ANTHROPIC_AUTH_TOKEN = 'sk-dummy'
$env:ANTHROPIC_DEFAULT_HAIKU_MODEL = 'gpt-5.6-luna'
$env:ANTHROPIC_DEFAULT_SONNET_MODEL = 'gpt-5.6-terra'
$env:ANTHROPIC_DEFAULT_OPUS_MODEL = 'gpt-5.6-sol'
$env:ENABLE_TOOL_SEARCH = 'true'
$env:CLAUDE_CODE_ALWAYS_ENABLE_EFFORT = '1'
$env:CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY = '3'
$env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = '1'
if ($StableSubagentModel) { $env:CLAUDE_CODE_SUBAGENT_MODEL = $StableSubagentModel }

function Decode-Argv([string]$Encoded, [string[]]$Fallback) {
    if ([string]::IsNullOrEmpty($Encoded)) { return @($Fallback) }
    try {
        $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Encoded))
        $value = ConvertFrom-Json -InputObject $json
        if ($null -eq $value) { return @() }
        return @($value | ForEach-Object { [string]$_ })
    }
    catch { throw 'Encoded argv is invalid; refusing lossy argument forwarding.' }
}
$EffectiveClaudeArgs = @(Decode-Argv $EncodedArgv $ClaudeArgs)

function ConvertTo-WindowsCommandLineArgument([string]$Argument) {
    if ($null -eq $Argument) { $Argument = '' }
    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') { return $Argument }
    $builder = New-Object System.Text.StringBuilder
    $null = $builder.Append('"')
    $backslashes = 0
    foreach ($character in $Argument.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }
        if ($character -eq '"') {
            if ($backslashes -gt 0) { $null = $builder.Append(('\' * ($backslashes * 2))) }
            $null = $builder.Append('\"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            $null = $builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        $null = $builder.Append($character)
    }
    if ($backslashes -gt 0) { $null = $builder.Append(('\' * ($backslashes * 2))) }
    $null = $builder.Append('"')
    return $builder.ToString()
}

function Invoke-ClaudeExact([string[]]$Arguments) {
    $command = Get-Command claude -CommandType Application -ErrorAction Stop
    $start = New-Object System.Diagnostics.ProcessStartInfo
    $start.FileName = $command.Source
    $start.UseShellExecute = $false
    $start.Arguments = (@($Arguments | ForEach-Object { ConvertTo-WindowsCommandLineArgument ([string]$_) }) -join ' ')
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $start
    try {
        $null = $process.Start()
        $process.WaitForExit()
        return $process.ExitCode
    }
    finally { $process.Dispose() }
}

if ($ProbeArgvRoundTrip) {
    ConvertTo-Json -InputObject @($EffectiveClaudeArgs) -Compress
    exit 0
}

if ($ProbeEnvironment) {
    [ordered]@{
        ANTHROPIC_BASE_URL = $env:ANTHROPIC_BASE_URL
        ANTHROPIC_AUTH_TOKEN = '<redacted-set>'
        ANTHROPIC_DEFAULT_HAIKU_MODEL = $env:ANTHROPIC_DEFAULT_HAIKU_MODEL
        ANTHROPIC_DEFAULT_SONNET_MODEL = $env:ANTHROPIC_DEFAULT_SONNET_MODEL
        ANTHROPIC_DEFAULT_OPUS_MODEL = $env:ANTHROPIC_DEFAULT_OPUS_MODEL
        ENABLE_TOOL_SEARCH = $env:ENABLE_TOOL_SEARCH
        CLAUDE_CODE_ALWAYS_ENABLE_EFFORT = $env:CLAUDE_CODE_ALWAYS_ENABLE_EFFORT
        CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY = $env:CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY
        CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY = $env:CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY
        CLAUDE_CODE_SUBAGENT_MODEL = $env:CLAUDE_CODE_SUBAGENT_MODEL
        ANTHROPIC_DEFAULT_FABLE_MODEL = $env:ANTHROPIC_DEFAULT_FABLE_MODEL
        ANTHROPIC_API_KEY = $env:ANTHROPIC_API_KEY
        ANTHROPIC_MODEL = $env:ANTHROPIC_MODEL
        ANTHROPIC_SMALL_FAST_MODEL = $env:ANTHROPIC_SMALL_FAST_MODEL
        CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS = $env:CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS
    } | ConvertTo-Json -Compress
    exit 0
}

function Test-LocalPort([uri]$Uri) {
    $port = if ($Uri.IsDefaultPort) { if ($Uri.Scheme -eq 'https') { 443 } else { 80 } } else { $Uri.Port }
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $result = $client.BeginConnect($Uri.Host, $port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($result)
        return $true
    }
    catch { return $false }
    finally { $client.Dispose() }
}

function Test-LoopbackHost([string]$HostName) {
    if ($HostName -in @('localhost', '127.0.0.1', '::1')) { return $true }
    $address = $null
    if ([System.Net.IPAddress]::TryParse($HostName, [ref]$address)) {
        return [System.Net.IPAddress]::IsLoopback($address)
    }
    return $false
}

function Test-ApprovedConfigArgument([string]$CommandLine) {
    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $false }
    $index = $CommandLine.IndexOf($ApprovedProxyConfig, [System.StringComparison]::OrdinalIgnoreCase)
    if ($index -lt 0) { return $false }
    $prefix = $CommandLine.Substring(0, $index)
    if (-not [regex]::IsMatch($prefix, '(?i)(?:^|\s)-config(?:\s+|=)["'']?$')) { return $false }
    $suffix = $CommandLine.Substring($index + $ApprovedProxyConfig.Length)
    return [regex]::IsMatch($suffix, '^["'']?(?:\s|$)')
}

function Get-ApprovedGatewayOwner([uri]$Uri) {
    $port = if ($Uri.IsDefaultPort) { if ($Uri.Scheme -eq 'https') { 443 } else { 80 } } else { $Uri.Port }
    $listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop)
    $ownerIds = @($listeners | ForEach-Object { [int]$_.OwningProcess } | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
    if ($ownerIds.Count -ne 1) { throw 'Approved gateway listener does not have one unambiguous owning process.' }
    $process = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ownerIds[0]) -ErrorAction Stop
    if ($null -eq $process -or [string]::IsNullOrWhiteSpace([string]$process.ExecutablePath)) {
        throw 'Approved gateway listener executable path could not be authenticated.'
    }
    if (([System.IO.Path]::GetFullPath([string]$process.ExecutablePath)) -ine ([System.IO.Path]::GetFullPath($ApprovedProxyBinary))) {
        throw 'Approved gateway port is owned by an unexpected executable.'
    }
    if (-not (Test-ApprovedConfigArgument ([string]$process.CommandLine))) {
        throw 'Approved gateway process is not using the approved config path.'
    }
    return [pscustomobject]@{ Verified = $true; ProcessId = $ownerIds[0] }
}

function Get-GatewayHealth([string]$BaseUrl) {
    try {
        $headers = @{ Authorization = 'Bearer sk-dummy' }
        $response = Invoke-RestMethod -Uri ($BaseUrl.TrimEnd('/') + '/v1/models?limit=1000') -Headers $headers -TimeoutSec 2
        $ids = @($response.data | ForEach-Object { [string]$_.id })
        $missing = @($ExpectedModels | Where-Object { $ids -notcontains $_ })
        return [pscustomobject]@{ Ready = $missing.Count -eq 0; Missing = $missing }
    }
    catch { return [pscustomobject]@{ Ready = $false; Missing = $ExpectedModels } }
}

$gatewayUri = [uri]$GatewayBaseUrl
$isApprovedGateway = $GatewayBaseUrl -eq $ApprovedBaseUrl
if (-not $isApprovedGateway) {
    if (-not ($ValidateGatewayOnly -or $ProbeGateway)) {
        throw 'Non-approved gateway endpoints are allowed only in explicit validation or probe mode.'
    }
    if (-not (Test-LoopbackHost $gatewayUri.Host)) {
        throw 'Validation and probe gateways must be local loopback endpoints.'
    }
    if (-not (Test-LocalPort $gatewayUri)) {
        throw 'Non-approved validation/probe gateway is closed; auto-start is forbidden.'
    }
    $health = Get-GatewayHealth $GatewayBaseUrl
    if (-not $health.Ready) { throw ('Authenticated probe gateway model validation failed; missing: ' + ($health.Missing -join ', ')) }
    $ownerVerified = $false
}
else {
    $ownerVerified = $false
    if (Test-LocalPort $gatewayUri) {
        $null = Get-ApprovedGatewayOwner $gatewayUri
        $ownerVerified = $true
        $health = Get-GatewayHealth $GatewayBaseUrl
        if (-not $health.Ready) { throw ('Approved gateway owner is valid but authenticated model validation failed; missing: ' + ($health.Missing -join ', ')) }
    }
    else {
        if ($RequireExistingGateway) { throw 'Approved gateway must already be running for this operation; auto-start is disabled.' }
        if (-not (Test-Path -LiteralPath $ApprovedProxyBinary -PathType Leaf) -or -not (Test-Path -LiteralPath $ApprovedProxyConfig -PathType Leaf)) {
            throw 'Approved CLIProxyAPI binary or config path is missing.'
        }
        $null = Start-Process -WindowStyle Hidden -FilePath $ApprovedProxyBinary -ArgumentList '-config', $ApprovedProxyConfig
        $deadline = [DateTime]::UtcNow.AddSeconds($HealthTimeoutSeconds)
        $health = $null
        do {
            Start-Sleep -Milliseconds 250
            if (Test-LocalPort $gatewayUri) {
                $null = Get-ApprovedGatewayOwner $gatewayUri
                $ownerVerified = $true
                $health = Get-GatewayHealth $GatewayBaseUrl
            }
        } while ((-not $ownerVerified -or $null -eq $health -or -not $health.Ready) -and [DateTime]::UtcNow -lt $deadline)
        if (-not $ownerVerified -or $null -eq $health -or -not $health.Ready) {
            $missing = if ($null -eq $health) { $ExpectedModels } else { $health.Missing }
            throw ('Approved CLIProxyAPI did not become ready with authenticated owner identity and Luna/Terra/Sol before timeout; missing: ' + ($missing -join ', '))
        }
    }
}

if ($ValidateGatewayOnly) {
    [ordered]@{ ready = $true; expectedModels = $ExpectedModels; baseUrl = $GatewayBaseUrl; approvedOwnerVerified = $ownerVerified; probeMode = [bool]$ProbeGateway } | ConvertTo-Json -Compress
    exit 0
}

try {
    $code = Invoke-ClaudeExact (@('--model', 'opus') + $EffectiveClaudeArgs)
    exit $code
}
catch [System.Management.Automation.CommandNotFoundException] { exit 127 }
