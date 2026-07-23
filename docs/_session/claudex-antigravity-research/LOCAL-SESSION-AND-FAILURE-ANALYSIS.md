# Local session and failure analysis

Issue: https://github.com/Wladefant/super-board/issues/10

## Scope and handling constraints

This analysis is intentionally limited to metadata from:

- `C:/Users/wkiri/.cli-proxy-api/auth/logs/error-v1-messages-2026-07-23*.log`
- `C:/Users/wkiri/.cli-proxy-api/daemon*.log`

A local Python parser read each error log and emitted only structural metadata: timestamps, byte counts, status codes, provider/model names, exact API error strings, request `Content-Length`, message/tool counts, and presence of the Claude Code agent header. Request bodies were parsed locally only to extract those top-level fields; prompts, system text, tool definitions, message content, credentials, emails, account IDs, session IDs, and agent IDs were not emitted. Correlation tags used below are synthetic labels, not raw identifiers.

No Claude session JSONL was opened. No proxy configuration, authentication state, credentials, or processes were changed. `daemon.err.log` is empty (0 bytes).

## Verdict

There are **two independent failure families**, and the available detailed evidence reverses the proposed attribution:

1. **Main-request xAI/Grok availability failure:** six captured requests from one main Claude Code session were about 1.839 MB each, but they did **not** receive a context-window response. The proxy rejected them locally with HTTP 503 and the exact error `auth_unavailable: no auth available (providers=xai, model=grok-4.5)`. No upstream HTTP request was recorded.
2. **Subagent context-window failure:** four later requests from one subagent were about 1.481–1.501 MB each and were routed to ChatGPT Codex as `gpt-5.6-sol`. The upstream returned HTTP 400 with the exact error `Your input exceeds the context window of this model. Please adjust your input and try again.`

Therefore, the precise claim that “the approximately 1.86 MB request got the explicit context-window 400” is **not supported by the retained detailed logs**. The approximately 1.84 MB captured request got the Grok auth-availability 503. The explicit context-window 400 belongs to a later, smaller, clearly marked subagent request.

The daemon does show two earlier HTTP 400 responses at 10:04:52 and 10:04:57, before successful HTTP 200 responses at 10:05:03 and 10:05:14. Their detailed error logs are not among the bounded files, so their model, request length, and exact error cannot be proven. They may be the original “clearing the chat fixed it” incident, but that remains unverified under this evidence boundary.

## Per-file compact summary

`Proxy HTTP` is the client-facing status recorded by the daemon. `Upstream HTTP` is the provider response captured in the error log. For streamed context failures, the proxy had already opened an SSE response with HTTP 200 before forwarding an `event: error`; the paired non-streaming attempt exposed HTTP 400 directly.

| Timestamp | File | File bytes | Proxy HTTP | Upstream HTTP | Provider / model | Requested model | Content-Length | Scope | Exact error |
|---|---|---:|---:|---:|---|---|---:|---|---|
| 10:07:59.768 +02:00 | `error-v1-messages-2026-07-23T100759-604f6ff2.log` | 1,840,761 | 503 | No upstream request | xAI / `grok-4.5` | `claude-fable-5-dd-5.4-korg` | 1,838,974 | Main | `auth_unavailable: no auth available (providers=xai, model=grok-4.5)` |
| 10:08:01.943 +02:00 | `error-v1-messages-2026-07-23T100801-3beab66d.log` | 1,840,761 | 503 | No upstream request | xAI / `grok-4.5` | `claude-fable-5-dd-5.4-korg` | 1,838,974 | Main | `auth_unavailable: no auth available (providers=xai, model=grok-4.5)` |
| 10:08:06.514 +02:00 | `error-v1-messages-2026-07-23T100806-affa882f.log` | 1,840,761 | 503 | No upstream request | xAI / `grok-4.5` | `claude-fable-5-dd-5.4-korg` | 1,838,974 | Main | `auth_unavailable: no auth available (providers=xai, model=grok-4.5)` |
| 10:08:16.117 +02:00 | `error-v1-messages-2026-07-23T100816-5e488f3f.log` | 1,840,760 | 503 | No upstream request | xAI / `grok-4.5` | `claude-fable-5-dd-5.4-korg` | 1,838,974 | Main | `auth_unavailable: no auth available (providers=xai, model=grok-4.5)` |
| 10:08:33.380 +02:00 | `error-v1-messages-2026-07-23T100833-f9e9dc48.log` | 1,840,761 | 503 | No upstream request | xAI / `grok-4.5` | `claude-fable-5-dd-5.4-korg` | 1,838,974 | Main | `auth_unavailable: no auth available (providers=xai, model=grok-4.5)` |
| 10:09:10.698 +02:00 | `error-v1-messages-2026-07-23T100910-209cda3f.log` | 1,840,761 | 503 | No upstream request | xAI / `grok-4.5` | `claude-fable-5-dd-5.4-korg` | 1,838,974 | Main | `auth_unavailable: no auth available (providers=xai, model=grok-4.5)` |
| 10:27:19.862 +02:00 | `error-v1-messages-2026-07-23T102729-abeee0d5.log` | 3,008,805 | 200 SSE | 400 | ChatGPT Codex / `gpt-5.6-sol` | `gpt-5.6-sol` | 1,501,200 | Subagent | `Your input exceeds the context window of this model. Please adjust your input and try again.` |
| 10:27:29.338 +02:00 | `error-v1-messages-2026-07-23T102739-fb86d5d8.log` | 3,008,953 | 400 | 400 | ChatGPT Codex / `gpt-5.6-sol` | `gpt-5.6-sol` | 1,501,186 | Subagent | `Your input exceeds the context window of this model. Please adjust your input and try again.` |
| 10:27:53.152 +02:00 | `error-v1-messages-2026-07-23T102801-7ff2f313.log` | 2,966,961 | 200 SSE | 400 | ChatGPT Codex / `gpt-5.6-sol` | `gpt-5.6-sol` | 1,480,701 | Subagent | `Your input exceeds the context window of this model. Please adjust your input and try again.` |
| 10:28:01.231 +02:00 | `error-v1-messages-2026-07-23T102814-0ec83e3c.log` | 2,967,108 | 400 | 400 | ChatGPT Codex / `gpt-5.6-sol` | `gpt-5.6-sol` | 1,480,687 | Subagent | `Your input exceeds the context window of this model. Please adjust your input and try again.` |

The approximately 3.0 MB context-error log files contain both the approximately 1.5 MB downstream Claude-shaped request and its approximately 1.5 MB translated upstream Codex request. The file size is therefore not the request size; `Content-Length` is the relevant request byte count.

## Structural correlation without content

### Main/Grok group

All six detailed Grok failures share one redacted session correlation group and have no `X-Claude-Code-Agent-Id`, so they concern the main request rather than a subagent. Each request has the same structural shape:

- 467 messages
- 195 tools
- 3 system blocks
- streaming enabled
- request JSON bytes exactly equal to `Content-Length`: 1,838,974
- no upstream request section, consistent with rejection at provider-auth selection before network dispatch

### Codex/subagent group

All four context failures share one different session correlation group and the same redacted `X-Claude-Code-Agent-Id`, so they are repeated attempts by one subagent. The pairs expose the retry behavior:

- First pair: 57 messages, 176 downstream tools, 1,501,200/1,501,186 request bytes; translated upstream request 1,505,226 bytes with 177 tools.
- Second pair after partial reduction: 52 messages, 176 downstream tools, 1,480,701/1,480,687 request bytes; translated upstream request 1,483,881 bytes with 177 tools.
- Within each pair, the first attempt is streaming and surfaces the provider error inside an HTTP 200 SSE stream; the second attempt is non-streaming and returns HTTP 400 directly.
- Reducing five messages and approximately 20 KB was insufficient. Every upstream attempt returned the same explicit context-window error.

This is evidence of a size/context-budget problem, not evidence of malformed JSON or broken tool-call state. The metadata cannot determine which portions of the request consumed the model’s token window.

## Timeline

- **09:55:32** — daemon records the xAI executor selecting `https://cli-chat-proxy.grok.com/v1`.
- **09:55:34** — that xAI-routed request completes with HTTP 402. The exact 402 response body is not retained in the bounded detailed logs.
- **09:58:34–09:58:35** — daemon records immediate HTTP 503 failures, indicating provider availability had already degraded before the six retained detailed failures.
- **10:04:52 and 10:04:57** — daemon records two HTTP 400 requests. No retained detailed log proves their model, request size, or exact error.
- **10:05:03 and 10:05:14** — daemon records successful HTTP 200 requests. These are consistent with recovery after a session/model change, but there is no metadata-only event identifying “clear chat,” so causation cannot be established.
- **10:07:57–10:09:10** — eight daemon HTTP 503 entries appear; six have retained detailed logs. Those six are identical approximately 1.839 MB main requests routed by the requested alias to xAI/Grok and rejected with `auth_unavailable`. An interleaved request succeeds at 10:08:24, and later requests also succeed, showing the proxy as a whole was not down.
- **10:27:19–10:28:14** — one Codex subagent makes two streaming/non-streaming attempt pairs. All four upstream attempts fail with the explicit context-window HTTP 400. Other interleaved requests return HTTP 200, again showing a request-specific rather than daemon-wide outage.

## Why clearing the chat could fix only one failure

A fresh chat changes main-session state: it removes accumulated main conversation history and normally resets session-local model selection/routing to launcher defaults. That can resolve a context-window failure caused by an oversized existing chat, and it can avoid a stale per-session model pin if the new chat chooses a different route.

It does **not** repair provider authentication, restore exhausted provider capacity, or change the construction of a separately spawned subagent request. The xAI/Grok failure occurred before an upstream request because no eligible xAI auth was available. A new chat could avoid that route, but it could not make that auth eligible again. Likewise, the later subagent independently sent approximately 1.5 MB plus 176 tool definitions to `gpt-5.6-sol`; clearing the parent’s previous chat did not enforce a smaller subagent context budget.

The evidence therefore supports this operational explanation:

- If the unretained 10:04 HTTP 400s were the original main-chat context overflow, clearing the chat plausibly fixed that specific failure by resetting history.
- The subsequent Grok `auth_unavailable` failures were a different provider-availability/routing problem.
- The later Codex failures were a different subagent context-budget problem.

The exact causal link between the user action “clear chat” and any particular successful daemon request cannot be proven without session content or a session lifecycle log, both intentionally excluded.

## Root-cause assessment

| Claim | Assessment | Confidence |
|---|---|---|
| There were multiple independent failures. | Verified: main xAI auth availability 503 and subagent Codex context 400 have different sessions, scopes, providers, statuses, and exact errors. | High |
| The retained approximately 1.84 MB main request exceeded context. | Refuted by its exact retained response; it failed at xAI auth selection before upstream dispatch. Its size may still be undesirable, but it was not the recorded cause. | High |
| A later subagent exceeded the `gpt-5.6-sol` context window. | Explicit upstream error on four attempts. | High |
| The xAI route became unavailable after the 09:55 HTTP 402. | Strong temporal evidence: xAI dispatch then 402, followed by fast 503s and later exact `no auth available`. The exact reason the proxy marked auth unavailable is not logged. | Medium |
| The failure was malformed translated history/tool state. | No supporting metadata. Upstream explicitly classified the Codex requests as context-window overflow. | Low / unsupported |
| The alias-to-Grok route was stale rather than intended. | The alias resolved to xAI/Grok, but the bounded logs do not include routing configuration or the user’s intended selection. | Unproven |
| The xAI auth failure specifically means expired token, revoked token, exhausted quota, or account enforcement. | The logs expose only HTTP 402 followed by `no auth available`; they do not identify which auth state caused it. | Unproven |

## Recovery runbook

### Context-window error

1. Treat the exact upstream context-window error as non-retryable without changing the input. Replaying the same request only repeats the failure.
2. For a main request, start a fresh chat or compact/prune the session, then verify that the next request’s `Content-Length` and token count materially decrease.
3. For a subagent, start a fresh subagent with a bounded brief. Do not forward the entire parent transcript, large research dumps, or every available tool schema.
4. Reduce the subagent tool set to the tools needed for its task. In this incident, 176 downstream/177 upstream tools were included on every failed attempt.
5. Preflight with the target provider’s tokenizer or token-count endpoint where available. Bytes are only a heuristic; the provider enforces tokens and model-specific context rules.
6. When streaming, inspect SSE `event: error` payloads even if the HTTP status is 200. Do not classify HTTP 200 alone as success.

### `auth_unavailable` / provider-route error

1. Stop identical retries. These six attempts were byte-identical and failed before upstream dispatch.
2. Confirm the selected alias and resolved provider/model in metadata. Here, `claude-fable-5-dd-5.4-korg` resolved to xAI `grok-4.5`.
3. Route the request to a model with an eligible credential/capacity, or wait for the provider account/cooldown to recover.
4. Re-authentication, credential edits, daemon restarts, or config changes require explicit approval; none were performed here.
5. If an upstream 402 precedes `no auth available`, inspect the provider/account status and exact retained response before assuming token expiry. A 402 can cause provider eligibility/cooldown handling, but the exact cause must come from the missing upstream response or provider account telemetry.

## Prevention

- Log separate fields for requested alias, resolved provider, resolved model, main/subagent scope, request bytes, estimated tokens, downstream HTTP status, upstream HTTP status, and SSE terminal error.
- Add a pre-dispatch context budget for every model route, especially subagents. Reserve output headroom and reject or compact before sending.
- Give subagents minimal task context and a minimal tool subset instead of cloning the complete parent harness.
- Detect repeated requests with the same session, model, body length/hash, and non-retryable error; fail fast rather than retrying unchanged input.
- Health-check route eligibility before binding a session to a provider alias. If no credential is eligible, choose an approved fallback or return a clear routing error before serializing a multi-megabyte request.
- Keep main-model and subagent-model routing explicit in logs. A fresh main chat must not be assumed to reset or repair independent subagent routing.
- In monitoring, treat streamed HTTP 200 plus an SSE `error` event as failure and retain the upstream status separately.

## What cannot be proven without reading content or broader state

The bounded metadata cannot establish:

- the true token count of any request;
- which prompt, message, tool definition, image, or tool result consumed the context window;
- whether any history/tool block was semantically malformed despite being valid JSON;
- the exact error behind the daemon-only 10:04 HTTP 400 responses;
- whether those 10:04 responses were the incident fixed by clearing the chat;
- what launcher/session event represented “clear chat,” or which later HTTP 200 was causally produced by it;
- whether the alias-to-Grok mapping was intended, inherited, or stale;
- whether xAI auth became unavailable due to quota, billing, cooldown, expiration, revocation, enforcement, or another eligibility rule;
- the exact body and meaning of the 09:55 HTTP 402;
- whether a proxy configuration change would prevent recurrence, because configuration/auth files were deliberately not inspected or mutated.
