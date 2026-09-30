---
name: chatgpt-web-bridge-live-test
description: "Run or debug the codex-chatgpt-web bridge live 3-turn test with Veyyon (chatgpt-web/medium), including the stale browser-helper bundle trap and diagnostics"
---

> [!CAUTION]
> **STOP — CHATGPT-WEB BRIDGE TESTING IS PERMANENTLY BANNED & DISABLED (2026-09-26)**
> **DO NOT RUN LIVE TESTS AGAINST `chatgpt-web/*` MODELS.**
> Excessive automated browser requests through the bridge triggered OpenAI session invalidation and removal of the operator's ChatGPT Pro subscription access.

# ChatGPT-Web bridge live test

Repo: `C:/Users/wkiri/development/codex-chatgpt-web` (fork of miuuyy/codex-chatgpt-web). Lanes check out under `C:/Users/wkiri/lanes/<name>`.

## Before every live run
1. `bun run scripts/build-browser-helper.ts .launcher-runtime/browser-helper.cjs` — the bundle is gitignored; a stale bundle silently runs old code.
2. Start the bridge as a durable `launch` process: `name: "chatgpt-web-bridge"`, `application: "C:/Users/wkiri/.bun/bin/bun.exe"`, `args: ["run", "src/cli.ts", "serve"]`, `persist: true`, `restart: "on-failure"`, `ready: { port: 17841, timeout: 30 }`, `env: { CODEX_CHATGPT_WEB_BROWSER_DIAGNOSTICS: "1" }` (screenshots + DOM JSON per stage under `~/.codex-chatgpt-web/diagnostics/browser-turns/<turn>/`). See `skill://chatgpt-web-bridge-operate` for full details.
3. Operator's ChatGPT browser login must be valid; only use it through the bridge.

## Run
`%LOCALAPPDATA%/veyyon/veyyon.exe -p --model chatgpt-web/medium --session-dir C:/Users/wkiri/lanes/bridge-test/<n> "<prompt>"`, 3 consecutive turns (ALPHA / BETA+previous / list both; use `-c` for turns 2 and 3), 180 s cap each. Expect ~85-100 s per turn. veyyon >= 1.5.0 (4f321c2) needs no `CODEX_CHATGPT_WEB_OAUTH_TOKEN`; older binaries: read the openai-codex row from `~/.veyyon/shared-auth/agent.db` into process env only, never print it.

## Known failure codes
- `chatgpt_submission_ambiguous` — send-stage confirmation; fixed in PR #13 (navigation/Stop-button evidence polled from Node, not an in-page observer that dies on SPA route change).
- `chatgpt_submitted_turn_failed` — response-stage turn selectors; fixed in PR #15 (`data-turn-id`, `data-message-author-role`, legacy `data-testid^=conversation-turn-` fallback). False-positive unaccepted user turn rejection in `reconcileAssistantTurnBinding` fixed in PR #21. If it recurs, read `19-turn-failed.json` in the diagnostics dir for the current DOM.

## Teardown
Stop the launch process, verify `Get-NetTCPConnection -LocalPort 17841` is empty. Report per-turn seconds/exit/response on the tracking issue.
