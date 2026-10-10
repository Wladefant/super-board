# e2e platform, model and secrets policy

Parent: https://github.com/Wladefant/super-board/issues/473.
Slice 1 (Windows spike and version pin): https://github.com/Wladefant/super-board/issues/475.
Slice 2 (Model and secrets policy): https://github.com/Wladefant/super-board/issues/476.
Slice 4 (Flow QA integration and replacement boundary): https://github.com/Wladefant/super-board/issues/487.
Pins: `pins.json`. Template: `e2e.config.template.ts`. Checks: `e2e_guard.py`. Runner: `e2e_run.py`.

## Windows spike findings and platform caveats

Tested on Windows 11 Pro (10.0.26200), Node v24.12.0, npm 11.6.2.
Evidence: https://github.com/Wladefant/super-board/issues/475#issuecomment-5989506714.

### Pinned package versions
- `e2e@0.17.0` (runner, SDK, CLI)
- `@e2e-dev/web@0.12.0` (Playwright web engine)
- `ai@7.0.0`
- `zod@4.1.8`
- `@ai-sdk/openai-compatible@3.0.62`

We use exact versions only. No caret or tilde is allowed.
`pins.json` defines these pins. `e2e_guard.py check_package_pins` enforces them.

### Windows platform caveats
1. Telemetry is on by default upstream. Set `E2E_TELEMETRY_DISABLED=1` and `DO_NOT_TRACK=1` in the environment. Run `e2e telemetry disable` once per machine.
2. Hidden console windows: `npx e2e init` calls `spawnSync(manager, ['install'], { shell: true })` without `windowsHide: true`. That call opens visible console windows on Windows. Lanes must never call `e2e init` interactively. Use `e2e.config.template.ts`. Pass `windowsHide: true` in Node or `creationflags=CREATE_NO_WINDOW` in Python for every subprocess.
3. Model route thinking mode: OpenCode Go rejects thinking mode in `tool_choice`. Set `providerOptions: { opencodeGo: { enable_thinking: false } }`. The key `opencode-go` is deprecated.
4. Zero-secret cache replay: cache replay (`cache: 'read-only'`) requires no model API key in the environment. CI replay jobs run with zero secrets.
5. Mobile engine restriction: `@e2e-dev/mobile` requires an iOS simulator or Android emulator. iOS simulators cannot run on Windows hosts. The mobile engine is forbidden on Windows.
6. Resource management: run browser tests through `build_slot.py acquire/release` with slot `e2e-run`. Delete throwaway test directories after the run.

## Model routes

| Route | Status | Reason |
|---|---|---|
| OpenCode Go, `qwen3.8-flash`, API key, via `@ai-sdk/openai-compatible`, `enable_thinking: false` | Allowed (default) | Flash class. Agent step with tool calls proven (3 model calls, 14.1k tokens). Evidence: https://github.com/Wladefant/super-board/issues/476#issuecomment-5989506961 |
| Cached replay (`cache: 'read-only'`) | Allowed (default) | 0 model calls and no key needed. CI replay jobs carry no secret. |
| Other API-key providers (OpenRouter, OpenCode Zen) | Allowed after a one-run check | Keys exist in the shared auth store. Not exercised yet. |
| DeepSeek | Not usable now | Key valid, account returns insufficient balance. |
| `e2e login` (ChatGPT, Copilot, OpenCode Console, SuperGrok) | Forbidden | Subscription routes. ChatGPT routes are off by policy since the 2026-09-26 invalidation. Unattended bursts through a subscription caused it. |
| `@e2e-dev/kernel`, `@e2e-dev/eas` | Forbidden | Hosted engines send the app to a third party. |
| `@e2e-dev/mobile` | Forbidden for now | Needs an iOS simulator. Not runnable on Windows. |
| Antigravity sidecar | Not tested for this use | No claim made. Add a row only with a run. |

## Rules

1. Telemetry is off everywhere: `E2E_TELEMETRY_DISABLED=1` and `DO_NOT_TRACK=1` (set by `e2e_run.py` and CI), and `e2e telemetry disable` once per machine.
2. Replay is the default. Record runs (`e2e_run.py --record`) spend tokens, run through `build_slot.py`, and need the OpenCode Go key from `~/.veyyon/shared-auth/agent.db`. The key reaches the child process through `E2E_MODEL_API_KEY` only, and the runner masks it in all output.
3. Logins use `credentials.user()` declared in the config. No password or key appears in test code.
4. `~/.config/e2e/oauth.json`, `E2E_OAUTH_CREDENTIALS`, `.env*` and `.e2e/oauth*` are never created or committed. `e2e_guard.py staged` fails the commit (`FAIL oauth-file`, `FAIL env-file`).
5. `.e2e/cache` can hold page content. A repo commits it only after `e2e_guard.py tree --allow-cache` finds no secret (slice 8 writes the per-repo rule).
6. The config fails closed. The app host must be `localhost`, `127.0.0.1` or a listed staging host. Production hostnames are refused by exact name (`polysimulator.com`, `api.polysimulator.com`, ...) and by marker (`zaraprptkegxqpvnsubu`, `akamai-iad-prod`). Failing id: `E2E_HOST_NOT_ALLOWED`.
7. Never run `e2e --version`, `e2e init` or `e2e login` from a lane. `init` spawns `npm install` with no hidden window.
8. Heavy runs go through `build_slot.py`, one browser at a time, with a timeout.

## Flow QA integration & replacement boundary

Parent: https://github.com/Wladefant/super-board/issues/473. Slice 4: https://github.com/Wladefant/super-board/issues/487.
Receipt generator: `workflows/e2e/e2e_receipt.py`. Merge gate reader: `workflows/portable/github_pr_gate.py`.

### What e2e replaces
1. Ad-hoc browser test scripts: instead of custom Playwright or Puppeteer driver scripts, lanes use `e2e` with pinned packages (`e2e@0.17.0`, `@e2e-dev/web@0.12.0`) and cached replay.
2. Separate test runners for interactive web flows: `e2e` runs recorded flows and agentic navigation with zero model calls on replay.

### What remains (Flow QA invariants)
1. Single authoritative gate: `github_pr_gate.py` remains the only merge gate reader. It consumes the `FLOW-QA: PASS <served-sha>` receipt produced by `e2e_receipt.py`. Under default `SUPERBOARD_MERGE_FIRST`, approved staging changes merge and deploy first, then post-deploy Flow QA verifies the deployed SHA (`/api/version`) on staging, with immediate revert on failure. Setting `SUPERBOARD_MERGE_FIRST=0` restores the pre-merge gate requirement. Production hosts remain refused.
2. Required viewport matrix: tests must cover `390x844` (mobile portrait), `390x420` (mobile with keyboard open), and `1440x900` (desktop).
3. Tap targets and layout rules: 44 px minimum tap targets, no horizontal scroll overflow, and active focus visibility.
4. Content-bound served SHA: the receipt binds the test run to `/api/version` on the served host. A mismatched or unverified SHA causes `FLOW-QA-REASON served_sha_mismatch` and fails the gate.
5. Deterministic scripted flows: `workflows/portable/flow_qa_runner.mjs` remains available for lightweight YAML flow definitions (`flow.yaml`). `e2e` handles full agentic and stateful browser flows; both bridge into the same `FLOW-QA:` receipt format.
