---
name: chatgpt-web-bridge-operate
description: "Start, supervise, verify, and troubleshoot the durable codex-chatgpt-web bridge on 127.0.0.1:17841 in one step"
---

> [!CAUTION]
> **STOP — CHATGPT-WEB BRIDGE IS PERMANENTLY BANNED & DISABLED (2026-09-26)**
> **DO NOT START OR SUPERVISE THIS BRIDGE. DO NOT DISPATCH `chatgpt-web/*` LANES.**
> High-volume automated browser requests through this bridge (up to 28 requests in 7 minutes and 57/hr with up to 70k tokens) triggered OpenAI anti-abuse session invalidation and account penalties, resulting in complete removal of ChatGPT Pro subscription access.
> The bridge is shut down, loopback port 17841 is freed, daemon auto-restart is stopped, subagents `web-task` and `web-thinker` are disabled, and all fallback chains have been stripped of `chatgpt-web/*`.
> ONLY the official OpenAI Codex OAuth path (`openai-codex/*`) is permitted.

# ChatGPT-Web Bridge Operational Guide

The bridge runs under `C:/Users/wkiri/development/codex-chatgpt-web` as a supervised `launch` process on `127.0.0.1:17841`, providing local OpenAI Responses API emulation backed by a live ChatGPT web session.

## 1. Preflight / Bundle Build
Before starting or restarting the bridge, always rebuild the gitignored browser-helper bundle:
```bash
cd C:/Users/wkiri/development/codex-chatgpt-web
bun run scripts/build-browser-helper.ts .launcher-runtime/browser-helper.cjs
```
*(Trap: the bundle is gitignored; editing source without rebuilding silently runs stale code in the browser worker).*

## 2. One-Step Durable Start
Start the process using the `launch` tool with persistence and failure recovery:
```json
launch(
  op: "start",
  name: "chatgpt-web-bridge",
  application: "C:/Users/wkiri/.bun/bin/bun.exe",
  args: ["run", "src/cli.ts", "serve"],
  cwd: "C:/Users/wkiri/development/codex-chatgpt-web",
  env: {
    "CODEX_CHATGPT_WEB_BROWSER_DIAGNOSTICS": "1"
  },
  persist: true,
  restart: "on-failure",
  ready: {
    "port": 17841,
    "timeout": 30
  }
)
```

## 3. Readiness Checks
1. **Health Endpoint:**
   ```bash
   curl -s -i http://127.0.0.1:17841/health
   ```
   Returns HTTP 200 OK with `{"status":"ok","service":"codex-chatgpt-web",...}`. Both `/health` and `/healthz` are supported.

2. **Models Endpoint (/v1/models):**
   - **Unauthenticated probe:**
     ```bash
     curl -s -i http://127.0.0.1:17841/v1/models
     ```
     Returns HTTP 502 with `{"error":{"message":"Native Codex passthrough requires the incoming Bearer authorization"}}`.
     *(Trap: HTTP 502 on unauthenticated GET /v1/models is expected behavior! It proxies to upstream OpenAI which requires a Bearer token. It does NOT mean the bridge is broken).*
   - **Authenticated probe:**
     ```bash
     python -c "import sqlite3, json, subprocess; con=sqlite3.connect('C:/Users/wkiri/.veyyon/shared-auth/agent.db'); t=json.loads(con.execute(\"SELECT data FROM auth_credentials WHERE provider='openai-codex' AND disabled_cause IS NULL LIMIT 1\").fetchone()[0])['access']; subprocess.run(['curl.exe','-s','-i','-H',f'Authorization: Bearer {t}','http://127.0.0.1:17841/v1/models?client_version=0.147.0'])"
     ```
     Returns HTTP 200 OK with all 10 models (5 ChatGPT-Web + 5 native Codex).

3. **End-to-End Model Round-Trip:**
   ```bash
   "C:/Users/wkiri/AppData/Local/veyyon/veyyon.exe" -p --model chatgpt-web/medium --session-dir "C:/Users/wkiri/lanes/bridge-test/probe" "Say hello in one word."
   ```
   Expected output: `Working...` followed by `Hello` (typically ~10-25 s).

## 4. Root Causes & Traps (Post-Restart Audit 2026-09-26)
1. **The 502 on unauthenticated /v1/models Trap:**
   `GET /v1/models` forwards to upstream Codex backend API (`chatgpt.com/backend-api/codex/models`). Upstream requires Bearer authentication and `client_version`. When probed unauthenticated via `curl`, the bridge throws `Native Codex passthrough requires the incoming Bearer authorization` and returns HTTP 502. This is normal passthrough behavior, not a server failure.
2. **The 404 on /health:**
   The native server path was `/healthz`. Probing `/health` returned 404 `Not found`. `/health` is now aliased to `/healthz` in `src/server.ts` so both return 200 OK.
3. **The Stale Browser-Helper Bundle Trap:**
   `.launcher-runtime/browser-helper.cjs` is gitignored. When source files like `browser-worker.ts` are edited (e.g. commit `f0baa50` adding response stall detection), the helper bundle MUST be rebuilt with `bun run scripts/build-browser-helper.ts .launcher-runtime/browser-helper.cjs`. Omitting this build causes the browser worker to execute stale code silently.
4. **Process Supervision Directory Scoping:**
   `launch` process names (`chatgpt-web-bridge`) are scoped per project working directory. Probing `launch(op="list")` from another working directory will not show `chatgpt-web-bridge`. Always inspect from `C:/Users/wkiri/development/codex-chatgpt-web`.
5. **Concurrency Limits & Serialization:**
   `MAX_CHATGPT_BROWSER_TABS = 5` sets the tab limit. In managed Chrome mode (`browserHost !== "launcher"`), turns queue and execute exclusively via `this.managedTurnTail`. Multiple lanes can submit concurrent requests (up to 5), but their browser UI interactions run sequentially one-by-one through the shared Chrome tab.
6. **Response Stall Detection & Auto-Retry:**
   When ChatGPT stops emitting text or tool progress for longer than `responseStallTimeoutMs` (default `CHATGPT_RESPONSE_STALL_TIMEOUT_MS = 180_000`, 3 minutes), the browser worker raises `chatGptResponseStalledError` (HTTP 504, `retryable: true`, code `chatgpt_response_stalled`). Unlike fatal `chatgpt_submitted_turn_failed` (HTTP 502, `retryable: false`), stalled responses are cleanly retired and automatically retried in a fresh browser conversation by `chatGptWebTurnRetryPolicy`.
7. **Image Generation Support (PR #22):**
   PR #22 (`feat/image-generation`) captures images generated by ChatGPT in the response DOM and downloads them to `~/.codex-chatgpt-web/browser/images`, emitting Markdown links so Codex agents can read and process generated visual artifacts.

## 5. Troubleshooting
- **Port 17841 Down (`curl` returns 000, exit 7):** Check `launch(op="describe", name="chatgpt-web-bridge")` from `C:/Users/wkiri/development/codex-chatgpt-web`. If stopped or terminated, restart using the command in §2.
- **`chatgpt_submitted_turn_failed` / `chatgpt_submission_ambiguous`:**
  Check `~/.codex-chatgpt-web/diagnostics/browser-turns/<traceId>/` for DOM JSON and screenshots at each stage.
  - `19-turn-failed.png` / `18-response-visible.png` show exact browser rendering.
  - If error is `ChatGPT opened another user turn while the bound assistant response was detached`: fixed in PR #21 (ensure `codex-chatgpt-web` has commit `84b8848` or PR #21 merged and browser-helper rebuilt).
- **Long Multi-Tool Chains & Trace Retirement (e.g. `UpDown5558Web` trace `9e2154f5d395`):**
  ChatGPT web executes all tool calls within a single turn through the `@codex` connector. Trace `9e2154f5d395` completed 11 consecutive tool calls (`read`, `bash`, `set_cwd`) across ~112 seconds before the turn aborted. In-flight tool progress actively updates `lastProgressAt`, preventing premature stall triggers during multi-tool execution chains.
- **Session Signed Out:**
  If browser screenshot in diagnostics shows login screen, message the operator on Telegram (`lane_id ChatgptBridgeUp`) with the one-line instruction to log in to ChatGPT in the managed Chrome profile.
- **Image Generation Capture (PR [#22](https://github.com/Wladefant/codex-chatgpt-web/pull/22), merged `20a891b`):**
  When a ChatGPT answer renders a generated image, the browser worker records large (>=256px, http) images from the answer DOM and, at completion, downloads them in parallel through the browser context into the configured image output directory (default `~/.codex-chatgpt-web/browser/images`, override via `chatgptWeb.imageOutputPath`). Files land as `chatgpt-image-<traceId>-<n>.<ext>` and the answer appends `![ChatGPT image N](<file:///...>)` Markdown; a failed download becomes visible text (`ChatGPT image N download failed: ...`) and never fails the turn. Config option: `chatgptWeb.imageOutputPath` in the provider config.
- **CRITICAL — image generation is DISABLED in the bridge's Temporary Chat surface:**
  Every bridge turn runs on `https://chatgpt.com/?temporary-chat=true` (hardwired in `prepareTemporaryChatSurface`). ChatGPT's image-generation tool is unavailable in temporary chats — the model replies "image generation isn't available in this temporary chat. Please switch to a regular ChatGPT chat" (observed 2026-09-27 via `chatgpt-web/medium`, trace `35c8fe5b51c8-2d483142`; documented on [PR #22 comment](https://github.com/Wladefant/codex-chatgpt-web/pull/22#issuecomment-5843535245)). So image prompts through the bridge currently return text-only answers and NO image file is produced. Fixing this needs a configurable regular-chat mode (`chatgptWeb.regularChat`-style option navigating `https://chatgpt.com/` and relaxing `assertTemporaryChatPage` + onboarding handling) — an operator decision, since Temporary Chat isolation is deliberate. Do NOT re-run image prompts through the bridge expecting an image until that mode exists.
