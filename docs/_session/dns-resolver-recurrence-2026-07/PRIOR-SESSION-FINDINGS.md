# Intermittent DNS failures on Windows: prior-session distinction and confirmed MSI Center UDP leak

## Scope

This dossier records the investigation tracked by https://github.com/Wladefant/super-board/issues/17. It distinguishes two incidents that initially appeared related:

1. A **July 7–8, 2026 Claude/Warp process-accumulation cleanup**, which is the likely session the user remembered.
2. The **July 2026 intermittent DNS failure**, whose confirmed root cause was `MSI.CentralServer.exe` exhausting Windows UDP endpoints.

The first incident is useful process-hygiene precedent, but there is no evidence that it caused or fixed DNS failures. The second incident has direct WinSock, endpoint-owner, remediation, and post-fix evidence.

## Executive decision

- Do **not** attribute the DNS outage to the prior 33 orphaned Claude sessions.
- The current DNS failure was caused by `MSI.CentralServer.exe` holding roughly 16,000 UDP endpoints, nearly exhausting the Windows dynamic endpoint range.
- The narrow temporary recovery is to restart `MSI_Center_Service` with elevation, then verify that endpoint counts collapse and DNS/HTTPS recover.
- For durable prevention, update or clean-reinstall MSI Center using MSI's official procedure, or remove/disable it if its hardware-control features are unnecessary. No official MSI advisory was found confirming that this particular leak is fixed.

## Incident A: prior process-accumulation cleanup

### Source

Local Claude Code transcript:

`C:\Users\wkiri\.claude\projects\C--Users-wkiri-OneDrive-Desktop-ing\0b7725bd-9d61-4ef2-8573-1881c4afbbc9.jsonl`

Relevant activity occurred on July 7, 2026 from approximately 19:31–19:38 UTC, with continued Comet memory diagnosis on July 8 from approximately 09:40–09:44 UTC.

### User-visible symptom

Claude Code sessions appeared to remain alive even though their Warp terminal tabs were no longer visible.

### Evidence

- Parent tracing found 33 `claude.exe` processes whose terminal ancestry was gone.
- Those orphaned Claude processes consumed approximately 5.45 GB in aggregate.
- After removing the Claude trees, 65 pre-existing Node processes remained from Firebase, Expo, Jest, Vite, and build-watch workloads.
- The next-day Comet investigation found 27 `comet.exe` processes consuming approximately 3.19 GB. Major contributors were extension hosts across four profiles, a long-lived GPU process, and open tabs.

### Inventory command

```powershell
Get-Process |
    Where-Object { $_.ProcessName -match 'claude|node' } |
    Select-Object Id, ProcessName, StartTime, CPU,
        @{N='MemMB';E={[math]::Round($_.WorkingSet64/1MB)}} |
    Sort-Object StartTime |
    Format-Table -AutoSize
```

### Parent tracing

```powershell
Get-CimInstance Win32_Process -Filter "Name='claude.exe'" |
    ForEach-Object {
        $process = $_
        $parent = Get-Process -Id $process.ParentProcessId -ErrorAction SilentlyContinue
        [PSCustomObject]@{
            PID       = $process.ProcessId
            Started   = $process.CreationDate
            ParentPID = $process.ParentProcessId
            Parent    = if ($parent) { $parent.ProcessName } else { 'GONE (orphan)' }
            Cmd       = ($process.CommandLine -replace '\s+', ' ')
        }
    } |
    Sort-Object Started |
    Format-Table -AutoSize -Wrap
```

### Safe candidate discovery

The prior session protected its own full ancestor chain, excluded Chrome's native host, and skipped Claude processes that still had a live terminal grandparent:

```powershell
$protected = @()
$current = $PID
while ($current) {
    $protected += $current
    $parentPid = (
        Get-CimInstance Win32_Process -Filter "ProcessId=$current" `
            -ErrorAction SilentlyContinue
    ).ParentProcessId
    if (-not $parentPid -or $parentPid -eq $current) { break }
    $current = $parentPid
}

$claudes = Get-CimInstance Win32_Process -Filter "Name='claude.exe'"
$orphans = @()
foreach ($claude in $claudes) {
    if ($claude.CommandLine -match 'chrome-native-host') { continue }
    if (
        $protected -contains [int]$claude.ProcessId -or
        $protected -contains [int]$claude.ParentProcessId
    ) {
        continue
    }

    $parent = Get-CimInstance Win32_Process `
        -Filter "ProcessId=$($claude.ParentProcessId)" `
        -ErrorAction SilentlyContinue
    $grandparent = if ($parent) {
        Get-Process -Id $parent.ParentProcessId -ErrorAction SilentlyContinue
    }

    if (-not $grandparent) {
        $orphans += ,@([int]$claude.ProcessId, [int]$claude.ParentProcessId)
    }
}
```

The execution used a manually reviewed array of the 33 discovered `(Claude PID, parent PowerShell PID)` pairs. The historical PID values are deliberately omitted because they are stale and unsafe to reuse. Each exact Claude tree was terminated by PID, followed by its reviewed parent PowerShell PID. The transcript reported `Killed 33 claude trees` and left three intended Claude processes.

### Prevention recorded by the prior session

Use `/exit` or `Ctrl+C` before closing a Warp tab containing Claude Code. Closing the terminal tab without ending Claude can leave detached process trees.

### Evidence gap

Broad searches found no prior mention of any of the following outside the current recurrence:

- `WSAENOBUFS`
- WinSock error 10055
- “system lacked sufficient buffer space”
- `Get-NetUDPEndpoint`
- `MSI.CentralServer`
- `MSI_Center_Service`

There is no transcript evidence that WebSearch or Comet DNS failures recovered after the July 7 orphan cleanup. Therefore, that cleanup must not be presented as the DNS fix.

## Incident B: confirmed intermittent DNS failure

### Source

Local Claude Code transcript:

`C:\Users\wkiri\.claude\projects\C--Users-wkiri-development-elumi-kids\5246ce14-2fd6-4776-95fe-74ab524a870e.jsonl`

### User-visible symptoms

- Comet intermittently showed `ERR_NAME_NOT_RESOLVED` for ordinary sites such as Google.
- The same page sometimes worked again moments later.
- Claude Code WebSearch returned empty results.

### Direct failure evidence

`Resolve-DnsName` produced native error code **10055**:

> An operation on a socket could not be performed because the system lacked sufficient buffer space or because a queue was full.

Microsoft defines `WSAENOBUFS` 10055 as “No buffer space available”; socket creation or operations can fail when buffer or system resources are unavailable:

- [Windows Sockets Error Codes](https://learn.microsoft.com/en-us/windows/win32/winsock/windows-sockets-error-codes-2)
- [Microsoft troubleshooting for WSAENOBUFS 10055](https://learn.microsoft.com/en-us/troubleshoot/windows-client/networking/connect-tcp-greater-than-5000-error-wsaenobufs-10055)
- [Windows `socket` function errors](https://learn.microsoft.com/en-us/windows/win32/api/winsock2/nf-winsock2-socket)

### Endpoint-owner evidence

The endpoint inventory was:

```powershell
$udp = Get-NetUDPEndpoint -ErrorAction SilentlyContinue
$processNames = @{}
Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
    $processNames[[int]$_.Id] = $_.ProcessName
}

$udp |
    Group-Object OwningProcess |
    Sort-Object Count -Descending |
    Select-Object -First 30 |
    ForEach-Object {
        [PSCustomObject]@{
            PID     = [int]$_.Name
            Process = $processNames[[int]$_.Name]
            Count   = $_.Count
        }
    }
```

Results before remediation:

- Total UDP endpoints: **16,069**
- `MSI.CentralServer.exe` PID 8360: **16,018 endpoints**
- The allocated ports began at 49,152 and traversed almost the entire Windows dynamic range.
- `netsh int ipv4 show dynamicport tcp` showed a dynamic range beginning at 49,152 with 16,384 ports.
- Total TCP connections were only 559, so this was not ordinary TCP connection volume.
- The machine had roughly 800 processes, but the process count remained high after recovery; process count was therefore not the direct cause.

Lineage inspection showed:

```text
Windows service: MSI_Center_Service
└── MSI_Central_Service.exe (PID 4924)      <- service host process
    └── MSI.CentralServer.exe (PID 8360)    <- owned 16,018 UDP endpoints
```

The service is the ancestor, not the descendant. This is what makes the remediation
below work: restarting `MSI_Center_Service` tears down the socket-owning
`MSI.CentralServer.exe` beneath it. Reading the tree the other way round would imply a
service could be terminated by restarting its own child, which is not what happened.

Changing DNS servers was not a cure. Direct queries through both the VPN resolver and the physical-router resolver encountered local 10055 failures. The resource failure occurred on the client before DNS could operate reliably.

### Exact successful remediation

A non-elevated service restart and direct process termination both failed with access denied. The successful narrow action was an elevated restart of the exact owner service:

```powershell
$args = '-NoProfile -ExecutionPolicy Bypass -Command "Restart-Service -Name MSI_Center_Service -Force"'
$process = Start-Process powershell.exe `
    -Verb RunAs `
    -ArgumentList $args `
    -Wait `
    -PassThru

[PSCustomObject]@{ ExitCode = $process.ExitCode }
```

The elevated process exited with code 0.

### Verification

Immediately after restart:

- Total UDP endpoints fell from **16,069 to 55**.
- The respawned MSI Central Server initially owned **0** UDP endpoints and later only 16.
- 30 direct DNS queries through the physical router completed with **0 failures**.
- A second 20-query router batch completed with **0 failures**.
- 10 HTTPS calls to Google's connectivity endpoint completed with **10 HTTP 204 responses**.
- Claude Code WebSearch returned ordinary official search results again.

The system still had over 800 processes, which confirms that freeing the leaked UDP endpoints—not reducing the total process count—restored networking.

## Independent community corroboration

Recent community evidence describes the same MSI Central Server pattern:

- [MSI Center listens on too many UDP ports](https://www.reddit.com/r/MSI_Gaming/comments/1ppqgam/msi_center_listens_too_many_udp_ports/) — a December 2025 report with May and June 2026 follow-ups recording approximately 15,198 and 16,316 UDP sockets and DNS failure. Community reverse engineering points to repeated SSDP discovery after network-adapter changes, but MSI has not officially confirmed that implementation detail.
- [MSI Dragon Center DNS failure / TCP-IP failure](https://www.reddit.com/r/MSI_Gaming/comments/17z8j2z/msi_dragon_center_dns_failure_tcpip_failure/) — a November 2023 report describing thousands of accumulating UDP sockets followed by ephemeral-port and DNS failures.
- [MSI Center clean-install troubleshooting](https://us.msi.com/faq/4147) — MSI's official removal and clean-install procedure, updated July 14, 2026. It does not specifically acknowledge the UDP leak.

The close numerical match between the community reports and this machine's 16,018 endpoints strongly corroborates the diagnosis. It does not replace the local evidence, which independently identifies the process and proves recovery after service restart.

## Recurrence runbook

### 1. Confirm the signature

```powershell
$udp = Get-NetUDPEndpoint
$processNames = @{}
Get-Process | ForEach-Object { $processNames[[int]$_.Id] = $_.ProcessName }

$udp |
    Group-Object OwningProcess |
    Sort-Object Count -Descending |
    Select-Object -First 15 |
    ForEach-Object {
        [PSCustomObject]@{
            PID     = [int]$_.Name
            Process = $processNames[[int]$_.Name]
            Count   = $_.Count
        }
    }
```

If `MSI.CentralServer` owns thousands of endpoints and DNS returns error 10055, the signature matches.

### 2. Restart only the owner service

Use an elevated PowerShell session:

```powershell
Restart-Service -Name MSI_Center_Service -Force
```

Do not use a blanket `taskkill /IM node.exe`, `taskkill /IM claude.exe`, or process-name sweep. Those actions can terminate unrelated development sessions without addressing the endpoint owner.

### 3. Verify endpoint release

```powershell
$msi = Get-Process -Name 'MSI.CentralServer' -ErrorAction SilentlyContinue
$udp = Get-NetUDPEndpoint -ErrorAction SilentlyContinue

[PSCustomObject]@{
    TotalUdpEndpoints = @($udp).Count
    MsiUdpEndpoints   = @(
        $udp | Where-Object { $_.OwningProcess -in @($msi.Id) }
    ).Count
}
```

### 4. Verify DNS and HTTPS repeatedly

```powershell
1..30 | ForEach-Object {
    Resolve-DnsName www.google.com -DnsOnly -QuickTimeout -ErrorAction Stop |
        Out-Null
}

1..10 | ForEach-Object {
    $response = Invoke-WebRequest `
        -Uri 'https://www.google.com/generate_204' `
        -UseBasicParsing `
        -TimeoutSec 10
    if ($response.StatusCode -ne 204) {
        throw "Unexpected HTTP status: $($response.StatusCode)"
    }
}
```

### 5. Durable mitigation

1. Record the installed MSI Center and module versions.
2. Update MSI Center and all modules.
3. If recurrence continues, follow [MSI's official clean-install procedure](https://us.msi.com/faq/4147).
4. If MSI Center features are unnecessary, consider uninstalling it or disabling its service after separately confirming which fan, RGB, performance, and hardware controls would be lost.
5. Do not install unofficial patched MSI DLLs from community posts; MSI services run with elevated privileges.

## Related tracking

- Investigation: https://github.com/Wladefant/super-board/issues/17
- Superboard project: https://github.com/users/Wladefant/projects/5
