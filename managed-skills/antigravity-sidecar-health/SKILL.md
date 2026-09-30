---
name: antigravity-sidecar-health
description: "Diagnose and fix Flash/Gemini lane failures, bogus exhausted Google quota and broken context compaction by starting or restarting the Antigravity masking sidecar."
---

# Antigravity Masking Sidecar Health

Operational guide for diagnosing and maintaining the local Antigravity masking proxy.

## Navigation
- **Gotchas & Failure Modes**: Read `gotchas.md` first for details on bogus 429 errors, compaction failures, and deceptive usage readings.
- **Reference Commands**: See `references/sidecar-diagnostics.md` for health check URLs, Bun launch commands, and process supervisor specs.

## When to Use This Skill
- Flash lanes (`task`, `qa-verifier`) fail with `Unable to connect. Is the computer able to access the url?`.
- `veyyon usage` reports Google weekly window as `status: exhausted` despite no consumption.
- Context compaction silently fails or hangs during long orchestrator turns.
- Machine or harness was recently rebooted or restarted.

## Quick Health Check
```bash
curl -s --max-time 5 http://127.0.0.1:45123/health
```
Healthy response: `{"status":"ok","service":"veyyon-antigravity-sidecar", ...}`.

## Recovery Procedure
If dead or unreachable, start the sidecar via the process supervisor:
```
launch op=start name=antigravity-sidecar detached=true persist=true \
  application=C:\Users\wkiri\.bun\bin\bun.exe \
  args=["run","C:\\Users\\wkiri\\.veyyon\\sidecar\\antigravity-masking-proxy.ts"] \
  ready={"port":45123,"timeout":40}
```
Verify recovery:
```bash
veyyon --model google-antigravity/gemini-3.8-flash -p "Reply with exactly: FLASH38-OK"
```
