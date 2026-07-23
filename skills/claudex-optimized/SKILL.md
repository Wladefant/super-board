---
name: claudex-optimized
description: "Audit and operate the process-local Claude Code-to-Codex launcher: Luna/Terra/Sol aliases, deferred tool search, 272K preflight, redacted recovery, fixture-safe setup, and zero-quota routing probes. Use for claudex status, routing, preflight, recovery, setup, sync, or routing tests."
---

# Claudex Optimized

This is a **user-level** skill whose canonical source is `skills/claudex-optimized/` in the tracked Superboard clone. It is deliberately excluded from `install.sh`, which installs project-scoped Superboard skills only.

## Safety boundary

- All model mappings and gateway variables belong only to the nested `powershell.exe -NoProfile` child launched by `scripts/launch.ps1`.
- Never edit Claude global settings, CLIProxyAPI configuration/authentication, provider credentials, OAuth state, or unrelated profile functions. The launcher may preserve the previously approved behavior of starting only `C:\Users\wkiri\.cli-proxy-api\cli-proxy-api.exe` with only `C:\Users\wkiri\.cli-proxy-api\config.yaml` when port 8317 is closed; it must never edit either path. An existing or newly started 8317 listener is trusted only when its owning process executable is that exact binary, its command line uses that exact config, and the authenticated model endpoint proves Luna, Terra, and Sol are ready.
- Never commit, push, pull, or publish automatically.
- Antigravity is an official-surface handoff only. Never invoke, authenticate, scrape, proxy, or reuse Antigravity/Gemini CLI OAuth through a third-party harness. Supported API-key or Vertex routes are separate metered products, not Ultra subscription capacity.
- Read thresholds, roles, tool profiles, and forbidden targets from `references/policy.json`.
- Do not claim live alias routing is verified unless fresh proxy evidence proves initial and resumed Haiku/Luna, Sonnet/Terra, and Opus/Sol routes.

## Commands

Route the first argument exactly:

### `status`

Run `scripts/audit.ps1`. Report only its redacted junction, branch/dirty state, managed-profile presence, child mappings, tool-search state, and local proxy/model availability. Keep `liveAliasRoutingVerified: false` unless empirical evidence exists.

### `route <task summary>`

Classify the task using `policy.json`:

- Luna / `research-readonly` for inventory, extraction, simple tests, or bounded research.
- Terra / `implementation-local` for ordinary implementation, QA, docs, or medium research.
- Sol only for hard implementation, architecture support, conflicting evidence, or a failed lower-tier validation.
- `review-readonly` for independent review.
- If official Antigravity is appropriate, emit a self-contained handoff brief and stop. Do not launch it.

Return the selected surface, role, tool profile, escalation trigger, context risk, and next command.

### `preflight [main|subagent] [--profile <profile>]`

Use `scripts/preflight.py budget` with structural estimates only. The verdict contract is:

- `<180000`: `admit`
- `180000..190000`: `warn`
- `190001..208000`: `rotate`
- `>208000`: `block`

Unknown estimates with a large eager catalog rotate or block. Never convert request bytes into an exact token count. Show eager and deferred tools separately.

### `recover <error-log-path>`

Run `scripts/preflight.py recover <path>`. Emit only the redacted category, retryability, and recovery action. Never emit prompts, system text, tool schemas, secrets, emails, or raw request/session/agent IDs. An HTTP 200 stream ending in `event: error` is a failure.

### `setup plan|apply|validate|rollback`

Run `scripts/setup.ps1 -Action <Plan|Apply|Validate|Rollback>`.

- `plan` and `validate` are read-only.
- `apply` may manage only the exact user junction, the AST-validated marker-delimited `claude-codex` profile block, and transaction state under `~/.claude/claudex-optimized`.
- Profile writes preserve the original encoding, BOM, ACL, and unrelated bytes through a same-directory temporary file and atomic replacement. File.Replace recovery copies, the transaction backup, and state are deleted only after byte-hash plus ACL verification. If apply restoration fails, all recovery artifacts remain and the error reports a redacted manual-recovery location.
- `rollback` validates every hash/path/junction precondition before mutation, rejects reparse points anywhere in state/transaction/backup paths, and restores exact profile bytes. Owned links and transaction files are removed non-recursively. A missing transaction-created junction is idempotent; an unknown replacement blocks rollback.
- Fixture path and failure-injection parameters exist for tests; failure injection is forbidden for the exact real profile, installed junction, and state paths.

### `sync status`

Run `scripts/audit.ps1` and report canonical branch, upstream, dirty skill files, ahead/behind state, and junction target. The installed user skill is a directory junction into the tracked clone, so an edit through either path is the same working-tree edit. Publication remains explicit `git add` / `git commit` / `git push` performed by the user or a separately approved workflow.

### `test local-tool-search`

Run:

```text
python scripts/probe-routing.py local-tool-search --tools 176
```

This starts the real installed Claude Code CLI only against a disposable fake Anthropic gateway and a temporary MCP stdio server advertising 176 synthetic tools. It consumes no provider quota and stores only sanitized request structure. Fixture subprocess cleanup is scoped to descendants of that exact Claude invocation whose command line names the fixture directory; directory cleanup then waits up to 15 seconds with bounded backoff for Windows handle release and fails if the tree truly remains. It passes only when captured requests prove catalog conservation and material eager-schema reduction. If the installed CLI cannot expose that evidence, report `verified: false` rather than inventing counts.

### `test stable-control [--approve-live-model-calls]`

Without the exact approval flag, refuse. With it, run only the direct `live-stable-control` command. The command first authenticates the already-running approved 8317 owner and inventory, then places a temporary local forwarding/capture gateway in front of it and executes a bounded initial+resume control probe with `-StableSubagentModel gpt-5.6-luna`. It records only sanitized gateway-ingress model, hashed agent identity, terminal HTTP/SSE structure, and any response provider metadata. Missing, duplicate, unexpected, fallback, nonterminal, or observed provider-mismatch evidence exits nonzero after 120 seconds at most.

### `test aliases [--approve-live-model-calls]`

Without the exact approval flag, refuse. With it, run only the direct `live-aliases` command through the same temporary forwarding/capture gateway. It executes inline Haiku/Sonnet/Opus agents, requires exactly one initial and one resumed request for Luna/Terra/Sol with stable per-agent identity, and fails closed on missing, duplicate, unexpected, fallback, nonterminal, or observed provider-mismatch evidence. The capture proves the routed model at gateway ingress. If CLIProxyAPI does not expose provider resolution in ordinary response metadata, report that the upstream provider remains unverified; never enable proxy debug or overclaim.

## Launcher contract

The managed `claude-codex` function serializes its exact PowerShell `$args` array as UTF-8 JSON plus Base64 and passes it to nested `powershell.exe -NoProfile`; raw re-tokenization is not the contract. This preserves spaces, quotes, empty strings, Unicode, and standalone `--`. The child sets the local gateway, existing dummy downstream token, aliases, tool search, effort, concurrency, and gateway discovery; removes global subagent/Fable and surplus model overrides; authenticates the approved 8317 listener owner executable, approved config argument, and Luna/Terra/Sol inventory; forwards the decoded array to `claude --model opus` so Claude Code retains its Opus capability identity while `ANTHROPIC_DEFAULT_OPUS_MODEL` resolves the request to `gpt-5.6-sol`; and returns Claude's exit code. A non-approved loopback URL is accepted only by explicit validation/probe mode and is never auto-started. `-ProbeEnvironment` and the encoded-argv test hook start neither Claude nor the proxy.
