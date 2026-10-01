# Gotchas: Shared Worktree Lane Safety

Critical failure modes and crash signatures in concurrent worktree operations.

## 1. Concurrent Worktree Creation Crash (2026-09-18 Incident)
- **Incident:** 10–14 lanes simultaneously ran `git worktree add` on Windows. Dozens of `git.exe` processes hung in I/O-bound lock contention, pushing host RAM to 88% and crashing the harness three times in one morning. Every running lane lost progress, and worktrees were left half-created with every file showing as deleted (`D`) in `git status`.
- **Fix:** Orchestrator must create worktrees serially in a background helper before lane dispatch. Never allow subagents to execute `git worktree add` concurrently.

## 2. Half-Created Worktrees & Orphan Lock Files
- **Symptom:** Worktree directory exists but `git status` shows all files deleted, or Git reports `fatal: '.git/worktrees/<name>/locked' exists`.
- **Recovery:** Kill orphan `git.exe` processes whose parent PID is dead, run `git worktree prune`, delete corrupted worktree directories, and recreate cleanly.

## 3. PowerShell Variable Stripping in Bash Tool
- **Symptom:** Inline commands like `powershell -Command "$_.FreePhysicalMemory"` fail with:
  `The term '.FreePhysicalMemory' is not recognized as the name of a cmdlet...`
- **Root Cause:** The bash execution environment interprets and strips `$_` and `$var` before passing the string to `powershell.exe`.
- **Fix:** Write the logic to a `.ps1` file and invoke via `powershell.exe -NoProfile -ExecutionPolicy Bypass -File script.ps1`, or execute via Python `ctypes` / `eval`.

## 4. Worktree Leaking & Unreleased Dev Ports
- **Trap:** A lane terminates but leaves its Next.js dev server running on a bound port, consuming 3–5 GiB RAM and blocking subsequent test runs.
- **Rule:** Every lane launching a dev server MUST shut it down and verify the port is released before yielding.
