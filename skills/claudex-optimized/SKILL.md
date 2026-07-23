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

Run `scripts/audit.ps1`. Report only its redacted junction, branch/dirty state, managed-profile presence, child mappings, tool-search state, local proxy/model availability, and persisted routing status. `liveAliasRoutingVerified` is true only when the read-only audit finds a fresh, schema-valid successful `live-aliases` matrix with distinct stable hashed identities for Luna, Terra, and Sol initial+resume turns.

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

Without the exact approval flag, refuse. With it, run only the direct `live-stable-control` command. The command first authenticates the already-running approved 8317 owner and inventory, then places a temporary local forwarding/capture gateway in front of it and executes a bounded initial+resume control probe with `-StableSubagentModel gpt-5.6-luna`. It records only sanitized gateway-ingress model, hashed agent identity, terminal HTTP/SSE structure, and any response provider metadata. A CLI exit before the first captured request includes only bounded redacted stderr structure, stdout/stderr character counts, and structured error types—never raw stdout, prompts, secrets, or paths. Missing, duplicate, unexpected, fallback, nonterminal, or observed provider-mismatch evidence exits nonzero after 120 seconds at most.

### `test alias-luna|alias-terra|alias-sol [--approve-live-model-calls]`

Without the exact approval flag, refuse. With it, run the matching direct command: `live-alias-luna`, `live-alias-terra`, or `live-alias-sol`. Each command starts its own main Sol controller process with exactly one inline agent: Haiku for Luna, Sonnet for Terra, or Opus for Sol. The controller must call `Agent` exactly once with `run_in_background: true`, copy the returned active ID, immediately send exactly one `SendMessage` continuation to that exact ID, then wait for and collect completion. Verification requires exactly two successful subagent requests on the expected routed model with the same hashed identity. Bounded auxiliary main Luna traffic and main Sol controller requests remain allowed; missing, duplicate, unexpected, fallback, nonterminal, mismatched-identity, wrong-target, or observed provider-mismatch evidence fails closed.

### `test aliases [--approve-live-model-calls]`

Without the exact approval flag, refuse. With it, run `live-aliases`, which is only an orchestrator: it executes `live-alias-luna`, `live-alias-terra`, and `live-alias-sol` serially as three independent Claude CLI processes, with a short bounded cooldown between live processes. It never uses the earlier combined three-alias controller prompt. Aggregate verification succeeds only if every independent probe passes. The capture proves routed models at gateway ingress. If CLIProxyAPI does not expose provider resolution in ordinary response metadata, report that the upstream provider remains unverified; never enable proxy debug or overclaim.

## Persisted routing status

Approval-gated live probe commands atomically replace `~/.claude/claudex-optimized/last-routing-probe.json` after each attempt. Tests must pass `--state-path <fixture>` so they never touch the real runtime state. The compact state contains only UTC timestamp and schema/skill versions, safely available Claude Code/CLIProxyAPI version strings, probe name and verification booleans, gateway-ingress model routes, turn/scope labels, and hashed agent keys. It never stores prompts, content, raw identifiers, credentials, or paths.

The file keeps `last_attempt`, independently retained `last_successful_alias_results`, and the assembled `last_successful_aliases` matrix. A successful direct alias command updates only its own retained result. The matrix may be assembled from all three successes in one `live-aliases` invocation or from recent separate attempts only when skill/schema and safely detected CLI/proxy versions match. Its timestamp is the oldest component timestamp, so freshness cannot be extended by refreshing only one alias. A failed attempt updates `last_attempt` but cannot erase a prior per-alias success or prior successful matrix. `scripts/audit.ps1` reads the file without modifying it, rejects missing, malformed, stale, or incomplete matrices, and emits compact `lastProbe` metadata plus `liveAliasRoutingVerified`. Gateway ingress proves alias-to-model routing only. `upstream_provider_verified` remains false unless ordinary response metadata exposes it; without proxy debug, the provider is unverified.

Do not create or fabricate the runtime state file from historical evidence. It is written only by a fresh approved probe or by an explicitly supplied sanitized record outside this skill's normal probe flow.

## Launcher contract

The managed `claude-codex` function serializes its exact PowerShell `$args` array as UTF-8 JSON plus Base64 and passes it to nested `powershell.exe -NoProfile`; raw re-tokenization is not the contract. This preserves spaces, quotes, empty strings, Unicode, and standalone `--`. The child sets the local gateway, existing dummy downstream token, aliases, tool search, effort, concurrency, and gateway discovery; removes global subagent/Fable and surplus model overrides; authenticates the approved 8317 listener owner executable, approved config argument, and Luna/Terra/Sol inventory; reconstructs the native Windows command line with CommandLineToArgvW-compatible quoting so empty values and inline JSON survive Windows PowerShell 5.1; invokes `claude --model opus` so Claude Code retains its Opus capability identity while `ANTHROPIC_DEFAULT_OPUS_MODEL` resolves the request to `gpt-5.6-sol`; and returns Claude's exit code. A non-approved loopback URL is accepted only by explicit validation/probe mode and is never auto-started. `-ProbeEnvironment` and the encoded-argv test hook start neither Claude nor the proxy.
