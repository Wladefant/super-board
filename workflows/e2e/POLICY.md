# e2e model and secrets policy

Parent: https://github.com/Wladefant/super-board/issues/473. Slice 2: https://github.com/Wladefant/super-board/issues/476.
Pins: `pins.json`. Template: `e2e.config.template.ts`. Checks: `e2e_guard.py`. Runner: `e2e_run.py`.

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
