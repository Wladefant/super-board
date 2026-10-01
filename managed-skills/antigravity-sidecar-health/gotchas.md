# Gotchas: Antigravity Sidecar Health

Operational failure modes and misleading telemetry caused by a dead or unresponsive sidecar.

## 1. Bogus "Exhausted" Provider Reading in `veyyon usage`
- **Symptom:** `veyyon usage --json` shows provider `google-antigravity`, weekly window `status: exhausted`, `usedFraction: 1` even when the daily window is 0% and the actual Google quota was barely touched.
- **Root Cause:** Without the masking sidecar intercepting requests on 127.0.0.1:45123, Google endpoints return HTTP 429 for unmasked CloudCode requests. Veyyon interprets repeated 429 status codes as window exhaustion.
- **Trap:** Orchestrator mistakenly concludes Google quota is depleted and re-routes all lanes to expensive models or halts work.
- **Fix:** Check `curl http://127.0.0.1:45123/health` before trusting any Google exhaustion status.

## 2. Immediate Flash Lane Connection Timeouts
- **Symptom:** Spawned `task` / `qa-verifier` lanes running on Gemini 3.8 Flash fail in ~60 seconds with:
  `Unable to connect. Is the computer able to access the url?`
- **Fix:** Restart the sidecar proxy via `launch op=start ...`.

## 3. Silent Context Compaction Breakage
- **Symptom:** Long orchestration sessions fail to compact context or exceed token limits.
- **Root Cause:** The compaction model in `config.yml` is `google-antigravity/gemini-3.8-flash`. If the sidecar is down, compaction requests fail silently.

## 4. Script Execution Failures with `Start-Process` (Exit 7)
- **Trap:** Running `start-sidecar.ps1` directly inside the harness often fails with exit code 7 due to Windows process elevation restrictions.
- **Fix:** Launch Bun directly via the `launch` tool with `detached=true persist=true`.
