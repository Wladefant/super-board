# Claudex optimized skill proposal

**Status:** implementation-ready design only; no launcher, settings, gateway, authentication, credential, daemon, or source changes are part of this document  
**Issue:** [Wladefant/super-board#10](https://github.com/Wladefant/super-board/issues/10)  
**Evidence base:** [Antigravity risk, model inventory, and Claude Code routing](https://github.com/Wladefant/super-board/blob/main/docs/_session/claudex-antigravity-research/ANTIGRAVITY-RISK-MODELS-AND-ROUTING.md) and [Local session and failure analysis](https://github.com/Wladefant/super-board/blob/main/docs/_session/claudex-antigravity-research/LOCAL-SESSION-AND-FAILURE-ANALYSIS.md)

## Decision summary

Build a future Claude Code skill named `claudex-optimized` that audits, plans, tests, applies, and rolls back a quota-first claudex profile without ever putting Antigravity or Gemini CLI OAuth into CLIProxyAPI or any other third-party harness.

The skill should provide two deliberately separate operating lanes:

1. **Official Antigravity lane:** a handoff-only lane for Google Ultra subscription capacity. The skill may recommend a model and generate a bounded task brief, but it must not authenticate, proxy, invoke, wrap, impersonate, or reuse Antigravity/Gemini CLI OAuth.
2. **Claudex lane:** Claude Code through the existing single CLIProxyAPI gateway, using ChatGPT-authenticated Codex models with a conservative **272,000-token subscription context envelope**, deferred tool discovery, strict task-specific tool allowlists, context preflight, and deterministic failure recovery.

For subagents, ship the verified **global override** as the stable mode. Keep heterogeneous built-in alias routing behind an explicit experiment until proxy logs prove that each alias reaches its intended upstream model.

## Goals

1. Consume available capacity in a compliant order without treating Antigravity OAuth as a gateway credential.
2. Make ordinary claudex work default to lower-cost Codex roles and escalate to Sol only on evidence.
3. Prevent subagent context failures by budgeting against the ChatGPT/Codex product's 272K window rather than the GPT-5.6 Sol API model's 1.05M window.
4. Enable deferred tool discovery and prevent complete MCP/tool catalogs from being copied into every request.
5. Give every subagent the smallest explicit tool allowlist that can complete its task.
6. Classify SSE, context, routing, authentication, quota, and unknown-model failures before retrying.
7. Make routing claims empirical: every model/alias/subagent claim must be backed by proxy metadata from a real probe.
8. Preserve a reversible path: produce a plan and backups before any future approved mutation, then support one-command rollback.

## Non-goals

1. Do not create a compliant-sounding wrapper around prohibited Antigravity or Gemini CLI OAuth reuse.
2. Do not add, refresh, import, export, inspect, copy, or repair OAuth credentials.
3. Do not restart or reconfigure CLIProxyAPI automatically.
4. Do not promise all models returned by `/v1/models` are usable, tool-compatible, quota-eligible, or picker-visible.
5. Do not promise Gemini 3.6 support through CLIProxyAPI until each exact live model ID passes a real call.
6. Do not expose every non-Claude model in Claude Code's picker through mass `claude-*` aliases.
7. Do not infer token counts from request bytes.
8. Do not treat HTTP 200 as success when an SSE stream terminates with `event: error`.
9. Do not retry a byte-identical request after `context_length_exceeded` or `auth_unavailable`.
10. Do not provide per-subagent base URLs; Claude Code's base URL remains session-wide.
11. Do not modify application repositories, source code, project settings, or daemon state as part of skill installation.

## Safety boundary

### Hard prohibition

The skill must never use Antigravity OAuth or Gemini CLI OAuth in Claude Code, CLIProxyAPI, OpenCode, OpenClaw, or another third-party harness. Google's [Antigravity Additional Terms](https://antigravity.google/terms), [Antigravity FAQ](https://antigravity.google/docs/faq), [Gemini CLI terms/privacy page](https://geminicli.com/docs/resources/tos-privacy/), and [Gemini CLI FAQ](https://geminicli.com/docs/resources/faq/) explicitly prohibit this pattern. Google's [official enforcement announcement](https://github.com/google-gemini/gemini-cli/discussions/20632) confirms active enforcement against third-party tools and proxies that harvest or piggyback on those OAuth credentials.

This boundary is not configurable. There is no `--unsafe-antigravity`, hidden override, acknowledgment flag, low-volume exception, secondary-account mode, telemetry impersonation mode, fixed-IP exception, or prompt-cloaking mode.

### Allowed Google routes

For third-party harness use, the skill may recommend only supported API routes:

- [Google AI Studio / Gemini Developer API](https://ai.google.dev/) credentials, with API/free-tier quota or metered billing.
- [Vertex AI](https://cloud.google.com/vertex-ai) credentials, with Google Cloud project quota and billing.

These routes must be labeled as metered/API capacity, not Google Ultra subscription capacity.

### Official-Antigravity lane

The official-Antigravity lane is an orchestration boundary, not a gateway route. The skill may:

- recommend an official Antigravity model role using Google's [current Antigravity model inventory](https://antigravity.google/docs/models);
- generate a concise, self-contained handoff brief;
- state the expected artifact and acceptance checks;
- tell the user to run the brief in the official Antigravity app or CLI;
- later accept a user-supplied result or artifact for independent review in claudex.

The skill must not:

- launch Antigravity as a child process;
- pass prompts into Antigravity automatically;
- read Antigravity OAuth state;
- proxy Antigravity traffic;
- scrape Antigravity output from an authenticated session;
- claim that an official-Antigravity handoff is a claudex subagent.

## Operating architecture

```text
Task request
   |
   v
claudex-optimized route
   |
   +-- official-antigravity recommendation
   |      -> emit bounded handoff brief only
   |      -> user runs it in Google's official surface
   |
   +-- claudex recommendation
          -> context preflight
          -> tool allowlist selection
          -> quota-first Codex role
          -> main or subagent execution through one gateway
          -> SSE/error classifier
          -> rotate, downgrade, escalate, wait, or stop
```

## Model roles

### Official Antigravity roles

The skill should use the following role order only for a manual handoff into Google's official surface:

| Priority | Official model role | Default work |
|---:|---|---|
| 1 | Gemini 3.6 Flash Low/Medium | Broad research, summaries, extraction, mechanical coding, first-pass implementation |
| 2 | Gemini 3.6 Flash High | Moderate reasoning and agent loops requiring stronger judgment |
| 3 | Gemini 3.5 Flash | Compatibility/regression fallback when 3.6 is unsuitable |
| 4 | Gemini 3.1 Pro Low | Architecture, migrations, difficult debugging, long-context synthesis |
| 5 | Gemini 3.1 Pro High | Hardest unresolved work only |
| 6 | Claude Sonnet 4.6 | Separate non-Gemini quota when its behavior is specifically preferred |
| 7 | Claude Opus 4.6 | Final high-stakes judgment or failed lower tiers |

Gemini 3.6 is current in official Antigravity, while CLIProxyAPI v7.2.96 support is partial and must not be assumed from the bundled registry. See Google's [Gemini 3.6 Flash announcement](https://antigravity.google/blog/gemini-3-6-flash-in-google-antigravity), [CLIProxyAPI issue #4494](https://github.com/router-for-me/CLIProxyAPI/issues/4494), and [CLIProxyAPI issue #4506](https://github.com/router-for-me/CLIProxyAPI/issues/4506).

### Codex roles in claudex

The claudex lane should be role-first and quota-first:

| Role | Default model | Use for | Escalation trigger |
|---|---|---|---|
| Bulk | GPT-5.6 Luna | File inventory, search, extraction, simple tests, mechanical edits, bounded research | Validation failure, repeated factual error, or unresolved dependency |
| Standard | GPT-5.6 Terra | Ordinary implementation, QA, documentation, medium research | Cross-system reasoning, difficult debugging, or conflicting evidence |
| Strong | GPT-5.6 Sol | Hard implementation, research synthesis, architecture support, quality-first reasoning | Independent final judgment or commitment-boundary review is still needed |
| Independent review | Normal first-party Claude Code | High-stakes review, connector/channel-dependent work, final commitment judgment | No automatic escalation beyond the user-approved first-party lane |

Sol must not be the default merely because a task is long. Escalation requires at least one concrete signal:

- a failed validation or test after a reasonable lower-tier attempt;
- unresolved cross-file or cross-system dependency;
- architectural ambiguity;
- contradictory evidence;
- a security, release, migration, or irreversible commitment boundary.

## Subscription context budget

### Product boundary

The skill must distinguish two different GPT-5.6 Sol products:

- The [OpenAI API model page](https://developers.openai.com/api/docs/models/gpt-5.6-sol) advertises a 1,050,000-token context window.
- The current [native Codex model catalog](https://github.com/openai/codex/blob/4462b9deef211723b781b426f5e5d36a5777115f/codex-rs/models-manager/models.json) budgets ChatGPT-authenticated Sol, Terra, and Luna at **272,000 tokens**.

Claudex uses the subscription/Codex route, so the skill must budget against **272,000**, not 1.05M and not CLIProxyAPI's conflicting 372K metadata.

### Default envelope

Use these initial operational thresholds, configurable only through the skill's non-secret policy manifest:

| Threshold | Tokens | Behavior |
|---|---:|---|
| Route envelope | 272,000 | Never claim more for ChatGPT-authenticated Codex without a successful empirical probe and current native catalog evidence |
| Warning | 180,000 estimated input tokens | Report component pressure and recommend compaction/rotation before more large tool results are added |
| Admission target | 190,000 estimated input tokens | Normal maximum for starting a new main or subagent request |
| Hard input ceiling | 208,000 estimated input tokens | Block dispatch and rotate/compact; leaves 64K for reasoning/output headroom |
| Emergency floor | Unknown or unmeasurable | Refuse to call the request safe; use conservative component estimates and rotate if either request shape or tool catalog is large |

The 64K reserve is an operational safety margin, not a claim about the upstream provider's exact output reservation algorithm.

### Preflight report

Before a Codex dispatch, the future skill should emit:

```text
route: codex-subscription/gpt-5.6-<role>
window: 272000
estimated input:
  system: N
  messages: N
  tool schemas: N
  tool results: N
  files/images: N or unknown
  translation overhead: observed bytes only, not token-equated
reserved headroom: 64000
verdict: admit | warn | rotate | block
```

Use the target provider's tokenizer or token-count endpoint when available. If exact counting is unavailable, label the result `estimated`; never convert bytes to a definitive token count.

### Context rotation

Rotate rather than endlessly trim in place when any condition is true:

1. Estimated input exceeds 208K.
2. Estimated input exceeds 180K and the next step is expected to add a large tool result, file, image, or research dump.
3. A subagent has accumulated unrelated parent history.
4. A context error has already occurred.
5. The request carries more tools than the selected role allowlist.
6. Two compaction attempts have not reduced estimated input below 190K.

Rotation creates a fresh main chat or fresh subagent with:

- the task goal;
- constraints;
- current decisions;
- only the evidence needed for the next step;
- artifact/file pointers instead of pasted raw dumps;
- explicit binary completion criteria;
- the role-specific tool allowlist.

It must not forward the complete parent transcript by default.

## Tool search and strict allowlists

### Tool search

The skill's audit must fail if `ENABLE_TOOL_SEARCH=false` is active for a large claudex tool surface. The planned apply action should remove that override or set tool search to `true`/`auto`, subject to user approval.

Claude Code's [MCP tool-search documentation](https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search) says disabling tool search loads all tool definitions into context. Anthropic's [tool-search guide](https://code.claude.com/docs/en/agent-sdk/tool-search) estimates that 50 tool definitions can consume roughly 10K–20K tokens; the failed local subagent carried 176 downstream and 177 upstream tools.

### Role allowlists

No role receives wildcard MCP access. The default profiles are:

| Profile | Explicit allowed tools | Explicitly excluded by default |
|---|---|---|
| `research-readonly` | `Read`, `Glob`, `Grep`, `WebSearch`, `WebFetch` | File writes, shell mutation, GitHub writes, deployment tools, credential/auth tools |
| `implementation-local` | `Read`, `Glob`, `Grep`, `Edit`, `Write`, `Bash` | Unnamed MCP servers, remote writes, deployment, credential/auth tools |
| `review-readonly` | `Read`, `Glob`, `Grep`, read-only `Bash` commands | `Edit`, `Write`, remote mutation, deployment, credential/auth tools |
| `routing-probe` | only the agent/model invocation surface plus metadata/log inspection | Source edits, remote writes, credential/auth mutation, daemon control |

Rules:

1. MCP tools are deny-by-default and added individually by exact server/tool name when the task requires them.
2. A task may use a narrower subset than its profile, never a broader implicit set.
3. More than 12 eagerly loaded tools requires an explicit `--expanded-tools` acknowledgment in experiment mode.
4. Tool-search/deferred tools do not count as eagerly loaded until selected.
5. The preflight report must show eager and deferred tool counts separately.
6. Research agents must not inherit implementation, deployment, or board-write tools unless the task explicitly requires them.
7. No profile ever includes authentication or credential-management tools.

## Subagent routing modes

### Stable mode: global subagent override

Use `CLAUDE_CODE_SUBAGENT_MODEL=<exact gateway model ID>` when one subagent model is desired for the entire session. This is the only arbitrary-ID subagent route verified by the existing claudex experiment.

Behavior:

- explicit and deterministic;
- applies session-wide to subagents;
- overrides per-invocation and frontmatter model choices;
- suitable for `bulk`, `standard`, or `strong` sessions where all subagents share one role;
- must be reported in `status` output so the user knows heterogeneous routing is disabled.

### Experiment mode: heterogeneous built-in aliases

The heterogeneous experiment must:

1. Leave `CLAUDE_CODE_SUBAGENT_MODEL` unset or set to `inherit`.
2. Map the built-in `haiku`, `sonnet`, `opus`, and optional `fable` slots through `ANTHROPIC_DEFAULT_HAIKU_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, and `ANTHROPIC_DEFAULT_FABLE_MODEL`.
3. Use only the built-in alias names in agent frontmatter and per-invocation model selection.
4. Keep one session-wide `ANTHROPIC_BASE_URL`; the gateway performs upstream provider selection by model field.
5. Verify every slot through proxy logs before marking the experiment successful.

Initial role mapping candidate:

| Claude Code alias | Gateway role | Candidate Codex model |
|---|---|---|
| `haiku` | Bulk | GPT-5.6 Luna |
| `sonnet` | Standard | GPT-5.6 Terra |
| `opus` | Strong | GPT-5.6 Sol |
| `fable` | Optional highest/long-horizon | Disabled until a separately approved model is chosen and tested |

Constraints:

- maximum four semantic slots;
- alias names can cause Claude-family capability assumptions;
- raw arbitrary non-Claude IDs in subagent frontmatter remain unsupported for production use until a new empirical test passes;
- a different base URL per subagent is not available; see [Claude Code issue #38698](https://github.com/anthropics/claude-code/issues/38698).

### Experiment success rule

A heterogeneous alias experiment passes only if all of the following are captured for each alias:

- requested alias;
- resolved gateway model;
- resolved provider;
- `X-Claude-Code-Agent-Id` present for the subagent request;
- successful terminal response with no SSE error;
- no silent fallback to the parent model;
- expected model remains selected on a resumed/follow-up subagent turn.

Until then, the skill must recommend stable global override mode.

## SSE and API error handling

### Terminal-state classifier

The skill must classify the final outcome from both HTTP and SSE layers:

| Signal | Classification | Retry policy |
|---|---|---|
| HTTP 200 + normal terminal SSE completion | Success | None |
| HTTP 200 + SSE `event: error` containing `context_length_exceeded` | Context failure | Non-retryable until input is materially reduced or context is rotated |
| HTTP 400 + `context_length_exceeded` | Context failure | Same as above |
| HTTP 503 + `auth_unavailable` and no upstream request | Route/auth-eligibility failure | Stop identical retries; select an approved eligible route or wait |
| HTTP 402 preceding later `auth_unavailable` | Provider/account state unknown | Do not guess token expiry; require provider/account telemetry or retained response evidence |
| HTTP 429 | Quota/rate limit | Honor wait/cooldown or move to the next approved quota lane |
| Unknown provider/model | Catalog/routing mismatch | Refresh approved model inventory and run a bounded probe; do not alias blindly |
| Network interruption without terminal event | Incomplete/unknown | Mark partial output untrusted; reconnect/retry only under an idempotent policy |

### Duplicate retry guard

Hash the non-secret request structure and retain:

- synthetic session correlation;
- main/subagent scope;
- requested model;
- resolved provider/model when available;
- request byte length;
- estimated token components;
- eager/deferred tool counts;
- terminal HTTP/SSE error class.

If the same session, model, body hash/length, and non-retryable error recur, stop before a second identical dispatch. A retry becomes eligible only after a measurable change such as fewer messages, fewer eager tools, a fresh context, or a different approved route.

## Failure recovery

### `context_length_exceeded`

1. Mark all partial streamed output untrusted.
2. Do not replay the same streaming request as non-streaming.
3. Produce a component pressure report.
4. Enable/depend on tool search and reduce eager tools to the role allowlist.
5. Rotate the subagent or main chat with a bounded brief.
6. Re-run preflight; require input below 190K for a normal retry and never above 208K.
7. If true 1M Sol capacity is required, recommend the separately metered OpenAI API product; do not claim subscription OAuth provides it.

### `auth_unavailable`

1. Stop identical retries immediately.
2. Show requested alias and resolved provider/model.
3. State whether the failure occurred before upstream dispatch.
4. Select the next approved claudex route with eligible credentials/capacity, or wait.
5. Never reauthenticate, edit credentials, or restart the daemon without a separate explicit user-approved operation.

### Quota or 429

1. Record the affected provider/model and retry timing when available.
2. Prefer waiting within the same low-cost role if the task is not urgent.
3. Otherwise move to the next approved lane in quota order.
4. Do not escalate to Sol solely because Luna/Terra is rate-limited if official Antigravity handoff or waiting is acceptable.
5. Never interpret Antigravity `remainingFraction` as authoritative admission proof; generation can still return 429, as reported in [CLIProxyAPI issue #1015](https://github.com/router-for-me/CLIProxyAPI/issues/1015).

### Unknown model or partial proxy support

1. Treat catalog visibility as discovery only.
2. Run a minimal text probe with no MCP tools.
3. Add one required capability at a time: tool use, web, image input, then subagent selection.
4. Record exact model IDs returned by the live proxy; do not guess IDs from display names.
5. Keep the model disabled in the policy manifest until every required capability passes.

## Quota-first routing algorithm

```text
1. Classify task:
   bulk | standard | strong | commitment-boundary

2. Decide surface:
   if official Antigravity workflow is acceptable and Ultra-first is desired:
       emit official-Antigravity handoff brief
       stop; do not invoke or authenticate it
   else:
       continue in claudex

3. Select Codex role:
   bulk -> Luna
   standard -> Terra
   strong -> Sol
   commitment-boundary -> first-party Claude review, if explicitly available/approved

4. Select tool profile:
   research-readonly | implementation-local | review-readonly | routing-probe

5. Preflight:
   enable/depend on tool search
   count/estimate components
   admit <=190K
   warn 180K+
   block >208K

6. Execute and classify terminal HTTP + SSE state.

7. Recover:
   context -> shrink/rotate
   auth unavailable -> route/wait
   quota -> wait/next approved lane
   unknown model -> bounded capability probe
   validation failure -> escalate one role
```

## Skill command UX

The future skill should expose one primary slash command with explicit subcommands:

### `/claudex-optimized status`

Read-only. Prints:

- current main model and gateway base URL presence, with secrets redacted;
- stable global subagent override state;
- built-in alias mappings;
- tool-search state;
- discovered eager/deferred tool counts;
- active context thresholds;
- policy warnings, especially any Antigravity/Gemini CLI OAuth route indicators;
- last routing probe result if stored.

### `/claudex-optimized route <task summary>`

Read-only. Returns:

- recommended surface: official Antigravity handoff, claudex Luna/Terra/Sol, or first-party Claude review;
- reason and escalation trigger;
- tool profile;
- context risk;
- exact next command.

If official Antigravity is recommended, output a bounded handoff brief and stop.

### `/claudex-optimized preflight [main|subagent] [--profile <name>]`

Read-only. Serializes or estimates the next request without dispatching it and reports component budgets, eager/deferred tool counts, selected route, and `admit|warn|rotate|block`.

### `/claudex-optimized plan [stable|aliases]`

Read-only. Produces an exact proposed diff for launcher/settings/non-secret gateway aliases, identifies files that would change, records current values in a rollback manifest, and makes no mutation.

### `/claudex-optimized test baseline`

Runs only bounded, low-context probes against already configured approved routes. No credential or daemon changes.

### `/claudex-optimized test global-subagent`

Tests the verified global override path with one tiny subagent task and captures requested/resolved model metadata.

### `/claudex-optimized test aliases`

Runs the heterogeneous built-in alias experiment. Requires explicit acknowledgment that it is experimental and must automatically restore the pre-test process environment afterward.

### `/claudex-optimized apply stable`

Future write action. Requires explicit user approval after showing the plan. Enables tool search, installs conservative thresholds, and configures one global subagent model. It must not alter credentials, auth files, or daemon state.

### `/claudex-optimized apply aliases`

Future experimental write action. Requires a fully passing alias test matrix and explicit approval. Writes only approved alias mappings and leaves the global override unset/inherit.

### `/claudex-optimized rollback`

Restores the last skill-created rollback manifest exactly. Refuses if a target file changed independently after the skill's apply step unless the user explicitly resolves the conflict.

### `/claudex-optimized recover <error-log-path>`

Read-only by default. Parses structural metadata only, redacts credentials/identifiers/content, classifies the failure, and recommends a recovery action. It must not read request prompt bodies beyond the minimum local structural fields required for counts and classification.

## Files and settings a future implementation would modify

No files below are modified by this proposal. A future approved implementation should constrain itself to this inventory.

### Skill package

- `C:/Users/wkiri/.claude/skills/claudex-optimized/SKILL.md` — command contract, safety rules, workflow.
- `C:/Users/wkiri/.claude/skills/claudex-optimized/scripts/audit.ps1` — read-only effective-setting inspection.
- `C:/Users/wkiri/.claude/skills/claudex-optimized/scripts/preflight.py` — structural request budget report with no prompt-content emission.
- `C:/Users/wkiri/.claude/skills/claudex-optimized/scripts/probe-routing.py` — bounded main/subagent/alias probes and metadata capture.
- `C:/Users/wkiri/.claude/skills/claudex-optimized/references/policy.json` — non-secret role, threshold, allowlist, and escalation policy.

### User-controlled runtime settings, only during an approved apply

- `C:/Users/wkiri/OneDrive/Documents/WindowsPowerShell/Microsoft.PowerShell_profile.ps1` — the claudex launcher function only: tool-search state, global subagent override, and/or built-in alias environment variables.
- `C:/Users/wkiri/.claude/settings.json` — only if a Claude Code user setting is required for model allowlisting/discovery; no unrelated settings.
- The active CLIProxyAPI non-secret configuration file, discovered at runtime — only approved OAuth model aliases or routing metadata. The skill must never edit auth stores or credential material.
- `C:/Users/wkiri/.claude/claudex-optimized/state.json` — skill-owned non-secret current policy/version state.
- `C:/Users/wkiri/.claude/claudex-optimized/rollback/<timestamp>/manifest.json` and exact pre-change file copies — rollback material with secret-containing files excluded.

### Settings the skill would manage

- `ENABLE_TOOL_SEARCH`: must not be `false` for the large claudex tool surface; target `true`, `auto`, or unset according to the approved launcher behavior.
- `CLAUDE_CODE_SUBAGENT_MODEL`: exact gateway model in stable mode; unset or `inherit` in alias experiment mode.
- `ANTHROPIC_DEFAULT_HAIKU_MODEL`: bulk alias candidate.
- `ANTHROPIC_DEFAULT_SONNET_MODEL`: standard alias candidate.
- `ANTHROPIC_DEFAULT_OPUS_MODEL`: strong alias candidate.
- `ANTHROPIC_DEFAULT_FABLE_MODEL`: disabled by default until separately approved and tested.
- `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY`: optional discovery support, with the warning that Claude Code filters discovered IDs to `claude*`/`anthropic*`; see the [gateway protocol](https://code.claude.com/docs/en/llm-gateway-protocol#model-discovery).
- `availableModels`: filter only; never presented as a way to create arbitrary models.

### Files and state the skill must never modify

- CLIProxyAPI authentication directories or OAuth token files.
- Google, OpenAI, Anthropic, or other provider credentials.
- Antigravity or Gemini CLI login state.
- CLIProxyAPI daemon binaries, services, process state, or logs.
- Application source repositories.
- Unrelated Claude Code launcher functions or user settings.

## Empirical test matrix

Every test uses a synthetic, non-sensitive prompt and logs only structural metadata. No test may authenticate a new provider, mutate credentials, restart a daemon, or use Antigravity OAuth.

| ID | Test | Setup | Expected evidence | Pass condition |
|---|---|---|---|---|
| T01 | Baseline main route | Existing approved claudex route; tiny prompt; no MCP tools | Requested ID, resolved provider/model, main scope, terminal status | One successful terminal response and expected resolved model |
| T02 | Baseline global subagent override | Set process-local `CLAUDE_CODE_SUBAGENT_MODEL` to Luna; tiny subagent task | `X-Claude-Code-Agent-Id`, requested/resolved model | Subagent resolves to Luna and completes |
| T03 | Global override precedence | Global override Luna plus per-invocation `opus` | Proxy request metadata | Global override wins and is reported as intentional |
| T04 | Raw arbitrary frontmatter negative control | Process-local agent definition requests an exact non-Claude ID | Proxy request metadata | Test records whether ID survives; production remains blocked if it silently falls back |
| T05 | Alias `haiku` | Global override unset/inherit; process-local `haiku -> Luna` | Requested alias and resolved Luna | Correct subagent route, no parent fallback |
| T06 | Alias `sonnet` | `sonnet -> Terra` | Requested alias and resolved Terra | Correct subagent route, no parent fallback |
| T07 | Alias `opus` | `opus -> Sol` | Requested alias and resolved Sol | Correct subagent route, no parent fallback |
| T08 | Alias resume persistence | Resume/follow up each T05–T07 subagent | Second request model metadata | Same intended route survives follow-up |
| T09 | Tool-search delta | Same bounded prompt with tool search disabled versus enabled, in an isolated process | Eager/deferred tool counts and serialized request size/token estimate | Enabled mode materially reduces eager schemas and remains functional |
| T10 | Strict allowlist | Research task under `research-readonly` | Tool inventory and attempted calls | Only five named read/search tools are available; write/mutation tools absent |
| T11 | 272K warning | Synthetic preflight estimate of 180,000 | Budget report | Verdict is `warn`, not dispatch-as-safe |
| T12 | 272K admission target | Synthetic estimate of 190,000 | Budget report | Verdict is `admit` with warning context if appropriate |
| T13 | 272K hard ceiling | Synthetic estimate of 208,001 | Budget report | Dispatch is blocked and rotation brief is generated |
| T14 | Context failure recovery | Replay a captured structural fixture classified as `context_length_exceeded`; no live oversized request | Classifier output | No automatic retry; recommends tool reduction plus rotation |
| T15 | SSE error recognition | Fixture: HTTP 200 stream ending in `event: error` | HTTP and SSE classifications | Final result is failure, never success |
| T16 | Auth-unavailable recovery | Fixture matching pre-upstream HTTP 503 `auth_unavailable` | Classifier output | No identical retry, no credential edit, recommends approved route/wait |
| T17 | Duplicate retry guard | Submit same structural non-retryable fixture twice | Request hash comparison | Second dispatch is suppressed |
| T18 | Unknown model probe | Exact live ID with tiny prompt, zero MCP tools | Provider/model and error | Model stays disabled unless text probe succeeds |
| T19 | Capability ladder | For a newly admitted model: text, one tool, web if required, image if required, subagent | Per-capability evidence | Model enabled only for capabilities that pass |
| T20 | Official Antigravity boundary | Route a task classified as official-Antigravity suitable | Command trace | Skill emits a handoff brief and performs no authenticated invocation |
| T21 | Rollback integrity | Apply changes to disposable fixtures, then rollback | File hashes before/apply/rollback | Restored hashes equal original hashes |
| T22 | External-edit conflict | Modify a fixture after apply and before rollback | Hash mismatch | Rollback refuses destructive overwrite and reports the conflict |

### Test artifacts

Store only:

- skill version;
- timestamp;
- Claude Code and CLIProxyAPI versions;
- test ID;
- requested alias/model;
- resolved provider/model;
- main/subagent scope;
- synthetic correlation ID;
- request bytes;
- estimated token components;
- eager/deferred tool counts;
- HTTP status;
- SSE terminal status/error class;
- pass/fail.

Do not store prompts, system text, tool schemas, message content, credentials, email/account identifiers, raw session IDs, or raw agent IDs.

## Rollout

### Phase 0 — document and audit

- Install only the skill package.
- `status`, `route`, `preflight`, `plan`, and fixture-based `recover` are available.
- No launcher or gateway mutation.
- Run T01, T09–T17, and T20 against fixtures or bounded approved probes.

**Exit:** audit output is accurate, safety boundary is enforced, and no secret-bearing file is read or written.

### Phase 1 — stable single-subagent model

- Enable/depend on tool search.
- Apply strict role allowlists.
- Configure one global subagent model, initially Luna or Terra according to workload.
- Enforce 272K thresholds and duplicate retry guard.

**Exit:** T01–T03 and T09–T17 pass for three consecutive sessions with no context overflow and no silent model fallback.

### Phase 2 — heterogeneous alias experiment

- Process-local experiment first; no persistent launcher change.
- Run T04–T08.
- Compare proxy evidence to intended alias mapping.

**Exit:** every alias and resumed subagent turn reaches the intended model. Any silent fallback fails the phase.

### Phase 3 — persistent alias rollout

- Persist only mappings that passed Phase 2.
- Keep `fable` disabled unless separately approved.
- Retain stable global override as the rollback mode.

**Exit:** T05–T08 pass after a fresh launcher session and normal workloads remain below context thresholds.

### Phase 4 — optional supported metered providers

- Separate explicit project for Google AI Studio/Vertex or OpenAI API capacity.
- No claim that this consumes Ultra or ChatGPT subscription quota.
- Separate approval, cost, and credential workflow outside this skill's default rollout.

## Rollback

1. Every future `apply` must first create a timestamped manifest containing target paths, original hashes, original relevant values, and exact backups.
2. Rollback restores only files changed by that apply transaction.
3. Process-local experiment variables are always restored automatically at command exit.
4. Persistent rollback order:
   1. restore launcher function block;
   2. restore Claude user settings block if changed;
   3. restore non-secret gateway alias/routing block if changed;
   4. clear skill-owned state for that transaction;
   5. run read-only `status` and T01 baseline.
5. Never roll back by deleting a complete settings or gateway file.
6. If post-apply independent edits changed a target hash, stop and report a three-way conflict; do not overwrite.
7. Rollback must not touch credentials, auth files, daemon state, or logs.

## Corrected incident attribution

The skill documentation, diagnostics, and fixtures must preserve the corrected attribution exactly:

- The approximately **1.84 MB main request** was routed to xAI/Grok 4.5 and failed locally with HTTP 503 `auth_unavailable: no auth available`; it did **not** produce the retained context-window error.
- The approximately **1.48–1.50 MB Sol subagent request** was routed through ChatGPT-authenticated Codex as `gpt-5.6-sol` and received the explicit upstream context-window HTTP 400, including the streaming HTTP 200 + SSE error form.
- Bytes are not tokens. The local evidence proves the error classes and request scopes, not an exact token count.

Any UI or report that says “the 1.84/1.86 MB Sol request exceeded context” fails acceptance.

## Binary acceptance criteria

### Safety

- [ ] Given any command, when the skill detects or is asked to create Antigravity/Gemini CLI OAuth use in a third-party harness, then it refuses and points to the official-Antigravity handoff or supported API-key/Vertex alternatives.
- [ ] Given official Antigravity is selected, when routing completes, then no authenticated process, proxy call, credential read, or automatic prompt submission occurs.
- [ ] Given an apply or rollback, when it finishes, then no credential, auth, daemon, log, or application-source file has changed.

### Routing

- [ ] Given stable mode, when a subagent runs, then proxy evidence shows the configured global subagent model.
- [ ] Given alias experiment mode, when `haiku`, `sonnet`, and `opus` subagents run and resume, then each resolves to its mapped upstream model with no silent parent fallback.
- [ ] Given `CLAUDE_CODE_SUBAGENT_MODEL` is set, when a per-invocation model differs, then status output warns that the global override takes precedence.
- [ ] Given a raw arbitrary non-Claude frontmatter ID has not passed T04, when persistent alias rollout is requested, then the skill refuses to depend on that raw-ID path.

### Context and tools

- [ ] Given ChatGPT-authenticated Sol/Terra/Luna, when preflight runs, then the displayed route envelope is 272,000 tokens.
- [ ] Given estimated input is 208,001 tokens, when dispatch is attempted, then dispatch is blocked and a rotation brief is produced.
- [ ] Given estimated input is at or below 190,000 tokens and all other checks pass, when dispatch is attempted, then it is admitted.
- [ ] Given tool search is disabled and the eager catalog is large, when audit runs, then it reports a blocking finding.
- [ ] Given a research subagent, when its tool inventory is inspected, then only the explicit `research-readonly` tools are eagerly available and mutation/credential tools are absent.
- [ ] Given an unnamed MCP tool, when a role starts, then that tool is unavailable until explicitly allowlisted.

### Errors and recovery

- [ ] Given HTTP 200 with terminal SSE `event: error`, when classified, then the result is failure.
- [ ] Given `context_length_exceeded`, when recovery runs, then no unchanged automatic retry occurs.
- [ ] Given pre-upstream `auth_unavailable`, when recovery runs, then no unchanged retry, credential edit, or daemon restart occurs.
- [ ] Given the same structural request and non-retryable error recur, when dispatch is attempted again, then the duplicate retry guard stops it.
- [ ] Given unknown model catalog visibility only, when routing is requested, then the model remains disabled until its required capability probes pass.

### Incident accuracy

- [ ] Given a generated diagnostic report, when it describes the retained incident, then it attributes the approximately 1.84 MB main failure to Grok auth availability and the approximately 1.48–1.50 MB subagent failure to Sol context overflow.
- [ ] Given any request size in bytes, when the skill reports context usage, then it labels tokens as measured or estimated and never presents byte size as an exact token count.

### Rollout and rollback

- [ ] Given `plan`, when it completes, then it lists every future target path and exact proposed value without modifying files.
- [ ] Given `apply`, when it starts, then a rollback manifest and exact backups exist before the first mutation.
- [ ] Given no independent edits after apply, when rollback completes, then all target file hashes equal their pre-apply hashes.
- [ ] Given an independent post-apply edit, when rollback runs, then it refuses to overwrite and reports the conflict.

## Recommendation

Implement the skill in two releases. Release 1 should be read-mostly plus the stable global-subagent path: safety refusal, route recommendation, official-Antigravity handoff generation, tool-search audit, strict allowlists, 272K preflight, SSE/error classification, duplicate retry prevention, and rollback-ready planning. Release 2 should add persistent heterogeneous alias routing only after T05–T08 prove the built-in aliases survive both initial and resumed subagent calls on the installed Claude Code and CLIProxyAPI versions.

Do not make Antigravity OAuth support an option in either release.
