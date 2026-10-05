---
name: e2e
description: "Write and run agentic end-to-end tests with tester-army/e2e (the `e2e` runner and `@e2e-dev/web`) the Superboard way: pinned versions, cached replay by default, host allow-list with a request-level abort, a FLOW-QA receipt from the report. Use before you write or run any e2e test, when a repo has e2e.config.ts, when asked for browser or agentic UI tests, or when an e2e run fails."
---

# e2e (tester-army/e2e) in Superboard

This is the house wrapper. The upstream skill text was reviewed first (notes at the end). Read the upstream topics from the installed package, not from the web:
`npx e2e guide <topic>` (topics: `setup`, `writing-tests`, `agent`, `running`, `explore`, `debugging`, `mcp`, `bug-bash`) or `node_modules/e2e/docs`. Where upstream and this page differ, this page wins.

Source of truth in the repo: `workflows/e2e/` of https://github.com/Wladefant/super-board (`POLICY.md`, `pins.json`, `e2e.config.template.ts`, `e2e.request-guard.template.ts`, `e2e_run.py`, `e2e_receipt.py`, `e2e_guard.py`). Parent issue: https://github.com/Wladefant/super-board/issues/473.

## Setup (once per repo)

1. Install exactly the pinned versions from `workflows/e2e/pins.json` (`e2e`, `@e2e-dev/web`, `ai`, `@ai-sdk/openai-compatible`, `zod`). No `^` and no `latest`. Never run `e2e init`, `e2e --version` or `e2e login` from a lane.
2. Copy `e2e.config.template.ts` to `e2e.config.ts` and `e2e.request-guard.template.ts` to `e2e.request-guard.ts`. Edit only `STAGING_HOSTS` and the `app` block. `python workflows/e2e/e2e_guard.py config e2e.config.ts --package-json package.json` must print no FAIL line.
3. Every test file starts with `import { installRequestGuard } from '../e2e.request-guard.ts'; installRequestGuard();`. It aborts, with `browser.route` and `route.abort`, every request to a host outside the allow-list (images, scripts, fetch, websockets, iframes). Never call `route.continue()` on an external URL; use `route.fallback()`.
4. Register the MCP server in the project, never globally (it needs the project config). `.mcp.json` in the project root: `{"mcpServers":{"e2e":{"command":"node","args":["node_modules/e2e/dist/cli/bin.js","mcp"],"env":{"E2E_TELEMETRY_DISABLED":"1","DO_NOT_TRACK":"1"}}}}`. Tools: `open_session`, `observe`, `locate`, `call`, `tools`. `locate` returns the exact `screen.getByRole(...)` line for a node; use it before you write a locator.

## Run

- Replay is the default and needs no key: `python workflows/e2e/e2e_run.py --dir <project> --app-url <url>`. A cache miss fails the run; it never spends tokens.
- Record only when a test is new or changed: add `--record` (OpenCode Go `qwen3.8-flash`, our API key, masked in output). Commit `.e2e/cache` only after `e2e_guard.py tree --allow-cache` finds no secret.
- Heavy runs go through `build_slot.py` (the wrapper does it), one browser at a time, with a timeout. Stop the app server you started and free its port.
- Receipt: `python workflows/e2e/e2e_receipt.py --report <project>/.e2e/report.json --expected-sha <served 40-hex sha> --base-url <url>`. It prints `FLOW-QA: PASS|FAIL <sha>`. Replay must be clean (0 model calls, 0 cache misses) or it prints `FLOW-QA-REASON replay_not_clean`; `--allow-model-calls` is only for a record run.
- Targets are named by viewport (`390x844`, `390x420`, `1440x900`); the receipt needs `390x844` and `1440x900`.

## Rules

1. Staging and local hosts only. Production hosts are refused at config load, at redirect time, in the page and per request. Never try to get around the guard.
2. No subscription logins (`e2e login`), no hosted engines (`@e2e-dev/kernel`, `@e2e-dev/eas`), no `@e2e-dev/mobile` on Windows.
3. Telemetry stays off. The wrapper sets `E2E_TELEMETRY_DISABLED=1` and `DO_NOT_TRACK=1`.
4. Logins use `credentials.user()` from the config. No password or key in test code, `.env`, or `.e2e/`.
5. A test counts when its report has assertions (`FLOW-QA-ASSERTIONS pass=N fail=0`, N > 0). An exit code is not proof.
6. Prove a new test can fail: break the thing it checks and show the named assertion fail.

## Review notes on the upstream skill text (https://github.com/tester-army/e2e, `skills/e2e`, reviewed 2026-10-05)

Scanned for prompt injection (override phrases, hidden instructions, off-domain URLs): none found.

| Upstream text | Decision | Reason |
|---|---|---|
| `npx e2e login ...` (setup.md, running.md) | Rejected | Subscription routes. ChatGPT routes are off by policy since 2026-09-26. |
| `npx e2e feedback ...` (SKILL.md "Feedback") | Rejected | It sends data to a third party. Report e2e problems on the repo's issue instead. |
| `npm install --save-dev e2e @e2e-dev/web ai@^7` and other unpinned installs | Changed | Exact pins from `pins.json`. |
| Example model `gateway('openai/gpt-6-luna-fast')` (Vercel gateway) | Changed | OpenCode Go `qwen3.8-flash` through `@ai-sdk/openai-compatible`, thinking off. |
| `e2e init`, `e2e --version` | Rejected | `init` runs `npm install` with a visible console window; both are covered by the pins. |
| Run with the default cache setting | Changed | `cache: 'read-only'` unless `E2E_CACHE_MODE=read-write` is set by `e2e_run.py --record`. |
| Config without a host allow-list | Changed | The template's host-guard block plus the request guard. Not optional. |
| `e2e mcp` registered with `claude mcp add e2e -- npx e2e mcp` | Changed | Per-project `.mcp.json` that calls the pinned `node_modules` binary, not `npx`. |
| Everything else (locators, `agent.act` and `agent.assert` rules, report format, debugging, bug bash) | Kept | Read upstream through `npx e2e guide <topic>`. |
