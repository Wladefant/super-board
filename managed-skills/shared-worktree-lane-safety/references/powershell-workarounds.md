# Reference: Safe Process Management and PowerShell Workarounds

Patterns for Windows host process inspection and safe cleanup.

## Safe Host RAM Inspection via Python
Avoid PowerShell variable-stripping bugs in the bash tool by querying Windows memory via Python:

```python
import ctypes

class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]

stat = MEMORYSTATUSEX()
stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
print(f"RAM load: {stat.dwMemoryLoad}%")
```

## Reaping Orphan Processes
To clean up orphan processes after a crash or worktree lockup:
```powershell
# In a dedicated script file (reap.ps1)
Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -in @('git.exe', 'node.exe')) -and
    ((Get-Process -Id $_.ParentProcessId -ErrorAction SilentlyContinue) -eq $null)
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```
Followed by:
```bash
git worktree prune
```
