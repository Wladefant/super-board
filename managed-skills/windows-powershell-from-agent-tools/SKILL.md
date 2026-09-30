---
name: windows-powershell-from-agent-tools
description: "Use before running any PowerShell (Get-CimInstance, Get-Process, Where-Object, $_ / $env:) from an agent tool on Windows; prevents the '$_ stripped by the shell' .CommandLine-not-recognized error flood"
---

# Running PowerShell safely from agent tools

## The bug
`bash` tool -> `powershell -Command "... Where-Object { $_.CommandLine -match 'x' }"` fails: the outer shell expands `$_` / `$var` to empty *before* PowerShell sees it. PowerShell then runs `{ .CommandLine -match 'x' }` and prints `The term '.CommandLine' is not recognized` **once per process** -> hundreds of error lines.

## Rules
1. NEVER inline PowerShell containing `$` in a `bash` tool command.
2. Preferred: py `eval` with list argv, script as a raw string:
```python
ps = r"""Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Depth 2"""
rows = json.loads(subprocess.run(["powershell","-NoProfile","-Command",ps],capture_output=True,text=True,timeout=60).stdout)
```
   Filter in Python, not with `Where-Object { $_ ... }`.
3. Or `write` a `.ps1` file and run `powershell -NoProfile -File <path>`.
4. Or use `psutil` directly in Python (no PowerShell at all).
5. Always set `timeout=` and cap output (`Select-Object -First N`, slicing in Python) so one error per object can't flood context.
6. If you see `'.CommandLine' is not recognized` / `'.Name' is not recognized`: STOP, this is rule 1; do not retry the same command.
