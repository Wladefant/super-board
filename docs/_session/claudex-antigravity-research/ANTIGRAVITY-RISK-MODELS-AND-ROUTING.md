# Antigravity risk, model inventory, and Claude Code routing

**Research date:** 2026-07-23  
**Issue:** [Wladefant/super-board#10](https://github.com/Wladefant/super-board/issues/10)  
**Baseline setup:** [Wladefant/super-board#9](https://github.com/Wladefant/super-board/issues/9), especially the [2026-07-22 empirical routing results](https://github.com/Wladefant/super-board/issues/9#issuecomment-5049717869)  
**Products examined:** Google Antigravity / Gemini CLI, CLIProxyAPI v7.2.96, Claude Code v2.1.218, ChatGPT-authenticated Codex, and GPT-5.6 Sol

## Executive verdict

### 1. Antigravity subscription OAuth through CLIProxyAPI is not a safe or supported way to consume Google AI Ultra quota

Google's current official position is explicit:

- The [Antigravity Additional Terms](https://antigravity.google/terms) say that using third-party software, tools, or services to access Antigravity is a breach, specifically giving use of another tool with Antigravity OAuth as an example, and say this may justify suspension or termination.
- The [Antigravity FAQ](https://antigravity.google/docs/faq) specifically names **Claude Code, OpenClaw, and OpenCode**, says an Antigravity login must not be used by those clients, and recommends a **Vertex AI or Google AI Studio API key** instead.
- The [Gemini CLI terms/privacy page](https://geminicli.com/docs/resources/tos-privacy/) and [Gemini CLI FAQ](https://geminicli.com/docs/resources/faq/) apply the same rule to harvesting or piggybacking on Gemini CLI OAuth.
- Google's [February 27, 2026 official incident post](https://github.com/google-gemini/gemini-cli/discussions/20632) says enforcement targeted “the use of 3rd party tools or proxies to access Antigravity resources and quotas” and clients that “harvest or piggyback on Gemini CLI's OAuth authentication.” It also says a second ToS flag can cause a permanent ban.

**Verdict:** account/service enforcement risk is **high-confidence and active**, not historical folklore. CLIProxyAPI's `--antigravity-login` is a technically implemented route, but it is **policy-unsupported** when used to power Claude Code or another third-party client. No request fingerprint, low-volume policy, fixed IP, prompt cloak, or client impersonation makes the route compliant.

The usual enforcement scope in reports is loss of Antigravity, Gemini CLI, Gemini Code Assist, and related AI access—not necessarily deletion of Gmail or the whole Google identity. That distinction reduces the blast radius, but it does not make the risk acceptable for a paid Ultra account.

### 2. Use Google subscription capacity only in Google's official Antigravity surfaces

The compliant boundary is:

| Route | Uses Ultra subscription capacity | Officially supported for Claude Code / third-party agents | Recommendation |
|---|---:|---:|---|
| Official Antigravity app / CLI | Yes | Not a gateway; it is the official client | **Use for Ultra-capacity work** |
| CLIProxyAPI `--antigravity-login` → Claude Code | Yes | **No; explicitly prohibited** | **Do not authenticate or use** |
| Reused Gemini CLI OAuth → CLIProxyAPI / Claude Code | Uses Gemini CLI/Code Assist backend quota | **No; explicitly prohibited** | **Do not use** |
| Google AI Studio / Gemini Developer API key | No subscription quota; metered API/free-tier quota | **Yes** | Supported Google route for third-party clients |
| Vertex AI credentials / service account | No subscription quota; GCP project billing/quota | **Yes** | Supported enterprise/project route |

Therefore a genuinely compliant “Antigravity-first inside claudex” policy does not exist. A compliant quota-first workflow can use official Antigravity as a separate first lane, then use claudex/Codex for work that must run in the Claude Code harness.

### 3. Gemini 3.5 Flash is real and still current, but it is no longer Google's newest Flash model

- Google introduced [Gemini 3.5 Flash in Antigravity](https://www.antigravity.google/blog/gemini-3-5-flash-in-google-antigravity) on 2026-05-19.
- The current [Antigravity model list](https://antigravity.google/docs/models) still includes Gemini 3.5 Flash Low, Medium, and High.
- Google introduced [Gemini 3.6 Flash in Antigravity](https://antigravity.google/blog/gemini-3-6-flash-in-google-antigravity) on 2026-07-21, and it is now the newer workhorse model.

CLIProxyAPI v7.2.96's bundled Antigravity registry still tops out at Gemini 3.5 variants. Runtime 3.6 support is partial and volatile: [issue #4494](https://github.com/router-for-me/CLIProxyAPI/issues/4494) initially reported `Unknown Provider/Model`; [issue #4506](https://github.com/router-for-me/CLIProxyAPI/issues/4506) says the High route later worked while Medium, Low, and Tiered still failed. Do not claim that all 3.6 variants are available through the proxy until the authenticated `/v1/models` result and a real call prove each one.

### 4. Claude Code cannot automatically discover all CLIProxyAPI models

Claude Code's [gateway protocol](https://code.claude.com/docs/en/llm-gateway-protocol#model-discovery) calls `GET /v1/models?limit=1000`, but it ignores discovered IDs that do not begin with `claude` or `anthropic`. CLIProxyAPI's native IDs such as `gemini-3.5-flash-low`, `gemini-pro-agent`, and `gpt-5.6-sol` therefore do **not** all appear automatically in `/model` even when CLIProxyAPI returns them.

They remain technically callable if CLIProxyAPI accepts them:

- `claude --model <exact-id>` passes the model string through behind a custom `ANTHROPIC_BASE_URL`.
- `/model <exact-id>` also passes arbitrary strings through behind a gateway.
- `ANTHROPIC_CUSTOM_MODEL_OPTION` can add one arbitrary picker entry.
- Built-in aliases can be remapped with `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, and `ANTHROPIC_DEFAULT_FABLE_MODEL`.
- CLIProxyAPI can expose OAuth model aliases, including `claude-*` aliases, but aliasing every non-Claude model into the discovery prefix is maintenance-heavy and can trigger Claude-specific capability assumptions.

### 5. Mixed-model subagents are possible only within one session-wide gateway, and current behavior is inconsistent for arbitrary IDs

The current official [subagent model precedence](https://code.claude.com/docs/en/sub-agents#choose-a-model) is:

1. `CLAUDE_CODE_SUBAGENT_MODEL`
2. Per-invocation `model` on the Agent tool
3. Agent definition `model:` frontmatter
4. Parent conversation model

Important changes:

- Since Claude Code v2.1.196, `CLAUDE_CODE_SUBAGENT_MODEL=inherit` behaves like leaving it unset; it no longer suppresses per-invocation or frontmatter selection.
- Since v2.1.211, an explicit per-invocation model survives resumed/follow-up subagent turns. The [v2.1.211 release](https://github.com/anthropics/claude-code/releases/tag/v2.1.211) also fixed custom-gateway auth being lost when background workers respawn.
- The current release is [v2.1.218](https://github.com/anthropics/claude-code/releases/tag/v2.1.218). Its changes do not include a fix for arbitrary non-Claude subagent model IDs.

However, issue 9's live Claude Code 2.1.217 + CLIProxyAPI 7.2.95 test is stronger than documentation for this exact setup: `model: gpt-5.6-luna` in frontmatter was silently dropped and the subagent fell back to `claude-opus-4-8`, while `CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.6-luna` worked. The official docs now say frontmatter accepts the same values as `--model`, but that is not what the proxy log showed.

**Operational conclusion:**

- One global subagent model through `CLAUDE_CODE_SUBAGENT_MODEL` is verified.
- Arbitrary gateway IDs in frontmatter are **not reliable** in the current claudex setup.
- Heterogeneous per-agent routing should be tested through **built-in alias indirection**, not raw gateway IDs: map `opus`, `sonnet`, `haiku`, and `fable` to four gateway IDs, leave `CLAUDE_CODE_SUBAGENT_MODEL` unset or `inherit`, and use only those aliases in agent definitions/invocations. This is a strong candidate, not yet an empirical result for claudex.
- A different `ANTHROPIC_BASE_URL` per subagent is not supported. [Claude Code issue #38698](https://github.com/anthropics/claude-code/issues/38698) remains open for per-agent provider/base-URL routing.

### 6. GPT-5.6 Sol's API model is 1M-class, but ChatGPT-authenticated Codex currently exposes a much smaller effective window

The user's objection is correct in one context and incorrect in another:

- The official [GPT-5.6 Sol API model page](https://developers.openai.com/api/docs/models/gpt-5.6-sol) lists a **1,050,000-token context window** and **128,000 maximum output tokens**.
- The current native Codex source catalog sets `gpt-5.6-sol` to `context_window: 272000` and `max_context_window: 272000` in [OpenAI's Codex model catalog](https://github.com/openai/codex/blob/4462b9deef211723b781b426f5e5d36a5777115f/codex-rs/models-manager/models.json). The same 272K product limit is present for Terra and Luna.
- CLIProxyAPI's v7.2.96 registry instead advertises `context_length: 372000` and `max_completion_tokens: 128000` for subscription Codex models in its [model registry](https://github.com/router-for-me/CLIProxyAPI/blob/285322cd97add6b21f60c267debec44fbec74060/internal/registry/models/models.json). That metadata conflicts with the native Codex product catalog and must not be treated as the upstream limit.

The retained local logs show an upstream Codex error on a roughly 1.48–1.50 MB Claude-shaped request, but bytes are not tokens. The exact token count was not logged. The correct conclusion is: **the request exceeded the ChatGPT Codex route's effective model window; its byte size alone does not prove why.**

---

## Evidence hierarchy and enforcement assessment

### Official policy: high confidence

#### Antigravity-specific terms

The [Antigravity Additional Terms](https://antigravity.google/terms) say:

> “Using third party software, tools, or services to access the Service” is a breach.

The page gives using another client with Antigravity OAuth as an example and says such actions may be grounds for suspension or termination.

The [Antigravity FAQ](https://antigravity.google/docs/faq) is even more direct: it names Claude Code, OpenClaw, and OpenCode, prohibits using an Antigravity login with them, and directs developers to a Vertex or AI Studio API key.

#### Gemini CLI-specific policy

The [Gemini CLI terms/privacy page](https://geminicli.com/docs/resources/tos-privacy/) says direct access to Gemini CLI's underlying services through third-party software using Gemini CLI OAuth violates applicable terms and may cause suspension or termination. The [Gemini CLI FAQ](https://geminicli.com/docs/resources/faq/) names the same third-party clients and recommends a Gemini Developer API / AI Studio or Vertex route.

#### General Google API/OAuth rules

Google's [API Terms](https://developers.google.com/terms/) say credentials identify a specific API client, must be kept confidential, and other API clients must be prevented from using them. They also permit Google to suspend API access when it reasonably believes the terms were violated. Google's [OAuth policies](https://developers.google.com/identity/protocols/oauth2/policies) require each application to register and accurately represent its own client identity rather than reuse another application's client credentials.

These general terms reinforce the product-specific rule, but the Antigravity and Gemini CLI pages are the decisive sources.

### Official enforcement history: high confidence

Google's [February 27 announcement](https://github.com/google-gemini/gemini-cli/discussions/20632) says:

- accounts had been flagged for using third-party tools or proxies to access Antigravity resources/quotas;
- the same backend enforcement also disabled Gemini CLI and Gemini Code Assist;
- Google ran a system-wide automated unban for recently flagged accounts;
- future first-time flags would enter a recertification/remediation process;
- a second ToS violation could lead to a permanent ban.

The [Antigravity changelog](https://www.antigravity.google/changelog) shows that enforcement infrastructure remains maintained: on 2026-02-26 Google added a suspension-remediation UI, and on 2026-06-25 it improved the reliability and response time of account-status checks for ToS violations.

### Firsthand reports: medium confidence individually, high confidence as a pattern

| Date | Source | Firsthand claim | Evidentiary weight |
|---|---|---|---|
| 2026-02-18 | [CLIProxyAPI #1637](https://github.com/router-for-me/CLIProxyAPI/issues/1637) | Three accounts received `403` stating the service was disabled for ToS violation; Gemini and Antigravity were blocked. Later comments reported multiple Pro accounts affected and at least one unban on 2026-02-27. | Strong firsthand cluster; causality is self-reported |
| 2026-03-03 | [CLIProxyAPI #1803](https://github.com/router-for-me/CLIProxyAPI/issues/1803) | Reporter says the account was permanently suspended after Gemini CLI + Antigravity use and Google cited “logging in using third-party software.” | Strong wording, single reporter |
| 2026-03-03 | [CLIProxyAPI #1814](https://github.com/router-for-me/CLIProxyAPI/issues/1814) | Pro user says account was suspended shortly after CLIProxyAPI Antigravity login. | Correlation, single reporter |
| 2026-05-21 | [Google AI forum appeal](https://discuss.ai.google.dev/t/request-for-review-of-gemini-cli-antigravity-gemini-code-assist-suspension/146723) | User explicitly mentions CPA/CLIProxyAPI-style forwarding and reused Gemini CLI authentication; no resolution posted. | Firsthand, no official reply |
| 2026-05-21 to 2026-06-06 | [One-month unresolved follow-up](https://discuss.ai.google.dev/t/follow-up-after-1-month-gemini-antigravity-gemini-cli-suspension-still-unresolved/146981) | Pro user attributes suspension to an unofficial quota/context extension; reports Gemini, Gemini CLI, and Antigravity still blocked after a month. | Firsthand, unresolved |
| 2026-07-21 to 2026-07-22 | [Antigravity forum category](https://discuss.ai.google.dev/c/antigravity/64) | Recent appeal and false-positive threads continue to report ToS suspension/403 states. | Confirms enforcement is still active; causes vary |

### Risk rating

| Claim | Confidence | Assessment |
|---|---|---|
| Google policy prohibits Antigravity login/OAuth in Claude Code or CLIProxyAPI. | **High** | Explicit official terms and FAQ |
| Google policy prohibits reuse of Gemini CLI OAuth in third-party tools/proxies. | **High** | Explicit official Gemini CLI policy and incident announcement |
| Google still enforces these rules in 2026. | **High** | Official remediation/checking changes plus current appeals |
| Every CLIProxyAPI Antigravity user will be suspended. | **Low / false as a universal claim** | Enforcement is probabilistic and detection-dependent |
| Low volume, one account, fixed IP, or prompt cloaking makes the route safe. | **Low / unsupported** | None changes the official prohibition; reports include ordinary paid users |
| Suspension always deletes the whole Google account. | **Low / misleading** | Reports commonly show AI services blocked while email remains usable |
| First flags may be remediated/unbanned. | **Medium-high** | Official reset/remediation process plus firsthand unban reports |
| Repeat violations can become permanent. | **High** | Explicit official statement |

---

## CLIProxyAPI Google authentication routes

The latest release at research time is [CLIProxyAPI v7.2.96](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v7.2.96), published 2026-07-23.

### Current v7.2.96 routes

#### Antigravity subscription OAuth — technically available, policy-unsupported

The v7.2.96 [server entrypoint](https://github.com/router-for-me/CLIProxyAPI/blob/285322cd97add6b21f60c267debec44fbec74060/cmd/server/main.go) implements:

```text
--antigravity-login
--no-browser
--oauth-callback-port
```

The [CLIProxyAPI Antigravity guide](https://help.router-for.me/configuration/provider/antigravity.html) documents a default callback port of `51121`.

This route stores a Google OAuth credential for the `antigravity` provider and calls Google's private Cloud Code / Antigravity backend. CLIProxyAPI's [model-fetch helper](https://github.com/router-for-me/CLIProxyAPI/blob/285322cd97add6b21f60c267debec44fbec74060/cmd/fetch_antigravity_models/main.go) uses:

- `https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels`
- daily/sandbox fallbacks
- an Antigravity-style user agent
- the stored OAuth access token

That implementation detail explains why the route consumes Antigravity account capacity. It does not make the route authorized for a third-party harness.

#### Gemini CLI OAuth — legacy route no longer exposed by v7.2.96

The current source has no `--gemini-login` and no legacy generic `--login` flow. Older CLIProxyAPI builds had a direct Gemini OAuth path, but v7.2.96's Google OAuth CLI route is Antigravity only. Current documentation similarly centers Antigravity OAuth rather than Gemini CLI OAuth.

This removal does not create a safe loophole: importing or reusing an old Gemini CLI OAuth credential remains prohibited by Google's Gemini CLI policy.

#### Google AI Studio / Gemini Developer API key — supported third-party route

CLIProxyAPI's [configuration example](https://github.com/router-for-me/CLIProxyAPI/blob/285322cd97add6b21f60c267debec44fbec74060/config.example.yaml) supports:

- `gemini-api-key`
- `interactions-api-key`
- aliases, exclusions, custom base URLs, headers, and per-key proxies

This is the route Google's FAQ recommends for third-party coding agents. It uses API/free-tier quota and billing, not Ultra subscription quota.

#### Vertex AI credentials — supported third-party route

The v7.2.96 CLI exposes `--vertex-import` for a Vertex service-account JSON file, and configuration supports Vertex-compatible API credentials. This uses the Google Cloud project and its quota/billing, not the consumer Ultra subscription pool.

### Safety boundary

Do not treat “CLIProxyAPI supports the login command” as “Google supports the usage.” The implementation and the provider's authorization policy answer different questions:

- **Implementation:** the request can be made.
- **Policy:** Google says the request must not be made by a third-party client with Antigravity/Gemini CLI OAuth.

---

## Antigravity and Gemini model inventory

### Current official Antigravity selector

The current [Antigravity models page](https://antigravity.google/docs/models) lists:

- Gemini 3.6 Flash — Low, Medium, High
- Gemini 3.5 Flash — Low, Medium, High
- Gemini 3.1 Pro — Low, High
- Claude Sonnet 4.6 (Thinking)
- Claude Opus 4.6 (Thinking)
- GPT-OSS 120B (Medium)

Gemini 3.6 Flash, Gemini 3.5 Flash, and Gemini 3.1 Pro are listed for Free/Plus, Pro, Ultra, and Enterprise. Claude and GPT-OSS are listed for consumer plans but not Enterprise on that page.

### CLIProxyAPI v7.2.96 bundled Antigravity registry

CLIProxyAPI's [v7.2.96 model registry](https://github.com/router-for-me/CLIProxyAPI/blob/285322cd97add6b21f60c267debec44fbec74060/internal/registry/models/models.json) contains these 11 Antigravity IDs:

| Proxy model ID | Display name | Context metadata | Max output metadata | Recommended role if used in official Antigravity |
|---|---|---:|---:|---|
| `claude-opus-4-6-thinking` | Claude Opus 4.6 (Thinking) | 200K | 64K | Highest-stakes architecture, adversarial review, difficult synthesis |
| `claude-sonnet-4-6` | Claude Sonnet 4.6 (Thinking) | 200K | 64K | Strong implementation/review lane; reserve from bulk work |
| `gemini-3-flash` | Gemini 3 Flash | 1,048,576 | 65,536 | Legacy fast generalist; prefer newer Flash where available |
| `gemini-3-flash-agent` | Gemini 3.5 Flash (High) | 1,048,576 | 65,536 | Fast agentic coding with deeper reasoning |
| `gemini-3.1-flash-image` | Gemini 3.1 Flash Image | Not declared | Not declared | Image generation/editing, not a general Claude Code main model |
| `gemini-pro-agent` | Gemini 3.1 Pro (High) | 1,048,576 | 65,535 | Architecture, long-codebase reasoning, hard migrations |
| `gemini-3.1-pro-low` | Gemini 3.1 Pro (Low) | 1,048,576 | 65,535 | Pro-quality lane with lower reasoning spend |
| `gpt-oss-120b-medium` | GPT-OSS 120B (Medium) | 114K | 32K | Generic fallback; less evidence for complex agent reliability |
| `gemini-3.1-flash-lite` | Gemini 3.1 Flash Lite | 1,048,576 | 65,535 | Cheap/high-volume classification, extraction, mechanical work |
| `gemini-3.5-flash-low` | Gemini 3.5 Flash (Medium) | 1,048,576 | 65,535 | Default high-throughput worker candidate |
| `gemini-3.5-flash-extra-low` | Gemini 3.5 Flash (Low) | 1,048,576 | 65,535 | Lowest-cost/simple worker candidate |

The IDs are not always intuitive: `gemini-3-flash-agent` is displayed as Gemini 3.5 Flash High, and `gemini-3.5-flash-low` is displayed as Medium. Routing must use exact IDs returned by the live proxy, not guessed names.

### Gemini 3.6 support status

Google's [3.6 announcement](https://antigravity.google/blog/gemini-3-6-flash-in-google-antigravity) says it improves coding, knowledge work, multimodal performance, precise edits, and execution-loop efficiency over 3.5 Flash. Google's broader [3.6 launch post](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-6-flash-3-5-flash-lite-3-5-flash-cyber/) reports 17% fewer output tokens on one comparison and stronger DeepSWE, MLE Bench, and OSWorld results.

As of v7.2.96:

- no `gemini-3.6` string exists in the bundled CLIProxyAPI model registry;
- [issue #4494](https://github.com/router-for-me/CLIProxyAPI/issues/4494) shows initial unknown-model failures;
- [issue #4506](https://github.com/router-for-me/CLIProxyAPI/issues/4506) says a High route worked later, but Medium/Low/Tiered did not.

**Conclusion:** 3.6 is current in official Antigravity, but CLIProxyAPI support is incomplete and should be treated as runtime-discovered, not guaranteed by v7.2.96.

### Gemini 3.5 Flash capabilities

Google's [Gemini 3.5 feature documentation](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.5) documents:

- about 1M input tokens and 65K output tokens;
- `minimal`, `low`, `medium`, and `high` thinking levels;
- function calling and combined tools;
- Google Search grounding;
- URL context, file search, Maps grounding, code execution, structured output, batch, and caching;
- multimodal input, including media returned by functions;
- preserved reasoning context in the Interactions API, or thought-signature replay in GenerateContent.

Caveats:

- not every native Gemini feature survives translation through Claude Code's Anthropic Messages format;
- native image output is not established by the Claude Code gateway path;
- Google's page has conflicting wording on Computer Use support;
- July 2026 firsthand reports repeatedly criticize 3.5 Flash for hallucinated repository details, shallow verification, premature “done” claims, and shortcut-taking. These reports are anecdotal, but they are consistent enough to justify mandatory tests/review for code changes.

### Gemini 3.1 Pro capabilities

The official [Gemini 3.1 Pro model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview) documents:

- 1,048,576 input tokens and 65,536 output tokens;
- text, image, video, audio, and PDF input;
- code execution, function calling, structured output, thinking, URL context, Google Search grounding, Maps grounding, caching, batch/flex/priority inference;
- a `gemini-3.1-pro-preview-customtools` variant optimized for Bash and coding-agent tools such as file viewing and code search.

Google positions it for software engineering, architecture, reliable multi-step execution, precise tool use, grounding, and factual consistency. It is the better fit for hard architecture/migration work than a Flash model, but it consumes the shared Gemini allowance more quickly.

### Tool, web, and image behavior through CLIProxyAPI

CLIProxyAPI's [README](https://github.com/router-for-me/CLIProxyAPI) advertises function/tool support and text/image multimodal input. Its Antigravity service fetches upstream `webSearchModelIds` and marks matching models as web-search capable in [antigravity_models.go](https://github.com/router-for-me/CLIProxyAPI/blob/285322cd97add6b21f60c267debec44fbec74060/sdk/cliproxy/antigravity_models.go).

This does **not** imply full feature parity in Claude Code:

- Text and image input can be translated.
- Function tools can be translated.
- Server-side web search depends on the model, upstream hints, and protocol translation; verify it per route.
- Image generation is a distinct capability. A listed image model may be callable through a Gemini/images endpoint but not render a useful image artifact through Claude Code's ordinary `/v1/messages` chat loop.
- Native Gemini features such as Computer Use, code execution, URL context, or Files may be absent unless CLIProxyAPI explicitly maps them.

---

## Antigravity quota behavior

Google's current [Antigravity plans documentation](https://antigravity.google/docs/plans?hl=en) says:

- Pro and Ultra baseline quota refreshes every five hours until the weekly cap is reached;
- Ultra has the highest baseline and weekly caps;
- actual consumption depends on how much work the agent performs;
- exact prompt/token allowances are not published;
- Pro/Ultra can optionally use purchased/promotional AI credits as overage.

Google's [May 19 plan update](https://www.antigravity.google/blog/changes-to-antigravity-plans) says:

- Gemini Flash and Pro now draw from a unified Gemini pool;
- usage is weighted by API-price ratios;
- the $100 Ultra tier receives about 5× the Pro allocation;
- the $200 Ultra tier receives about 20× the Pro allocation;
- non-Gemini models retain separate fixed limits.

CLIProxyAPI's configuration has a `quota-exceeded` block with project switching, preview-model switching, and an `antigravity-credits` fallback for Claude models. That is proxy behavior, not a guarantee of Google quota availability.

### Quota telemetry is not a perfect admission signal

[CLIProxyAPI issue #1015](https://github.com/router-for-me/CLIProxyAPI/issues/1015) documents 17 Antigravity accounts returning HTTP 429 on generation even while `fetchAvailableModels` showed 60–100% remaining. That incident may reflect hidden short-window throttling, abuse controls, a different quota pool, or stale quota telemetry. It demonstrates that `remainingFraction` is informative but not authoritative.

### Quota-first policy inside official Antigravity

If using Google's official client, the sensible order is:

1. **Gemini 3.6 Flash Low/Medium** for mechanical coding, searches, summarization, extraction, broad research, and first-pass implementation.
2. **Gemini 3.6 Flash High** for moderate reasoning and agent loops requiring stronger judgment.
3. **Gemini 3.5 Flash** only when 3.6 has a regression, a compatibility issue, or independent quota availability; it is real but no longer the first default.
4. **Gemini 3.1 Pro Low** for architecture, difficult debugging, migration design, and long-context synthesis.
5. **Gemini 3.1 Pro High** only for the hardest unresolved work.
6. **Claude Sonnet 4.6** for implementation/review work where its behavior is specifically preferred and its separate third-party-model quota is available.
7. **Claude Opus 4.6** for final high-stakes judgment, adversarial review, or tasks where lower tiers failed.
8. **GPT-OSS 120B** as a fallback or experimentation lane, not the default for important code without evaluation.

The policy minimizes premium-reasoning waste by escalating on evidence rather than starting on Pro/Opus.

### Compliant cross-product policy for Wlad's setup

Because Ultra OAuth cannot safely be injected into claudex:

1. Run broad research, mechanical drafting, or independent implementation in **official Antigravity** first when its workflow is acceptable.
2. Use **claudex with Codex subscription OAuth** for work that specifically benefits from Claude Code's harness, tools, and orchestration.
3. Use normal first-party Claude Code sessions where channels, Remote Control, connectors, and Claude-specific behavior matter.
4. Use Google AI Studio/Vertex through a gateway only when API billing/quota is explicitly accepted; do not describe it as consuming Ultra subscription capacity.
5. Reserve premium models for an escalation lane, not as the default subagent model.

---

## Claude Code model discovery and picker behavior

### Gateway discovery contract

Claude Code v2.1.129+ supports gateway discovery through `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`. Per the [gateway connection guide](https://code.claude.com/docs/en/llm-gateway-connect#add-gateway-models-to-the-model-picker) and [protocol reference](https://code.claude.com/docs/en/llm-gateway-protocol#model-discovery):

- request: `GET /v1/models?limit=1000`;
- timeout: 3 seconds;
- redirects: treated as failure;
- authentication: one gateway credential header plus custom headers;
- fields consumed: `id` and optional `display_name`;
- accepted ID prefix: only `claude` or `anthropic`;
- cache: `%USERPROFILE%\.claude\cache\gateway-models.json` on Windows;
- `availableModels` is an allowlist and can further filter results.

Therefore the issue 9 suggestion that discovery would automatically populate all ten Codex IDs was too optimistic. Raw `gpt-*` and `gemini-*` IDs are filtered by current Claude Code.

### Ways to expose non-Claude model IDs

| Method | Picker visibility | Number of models | Main model | Subagents | Caveats |
|---|---:|---:|---:|---:|---|
| `--model <id>` | No persistent picker row required | Unlimited over separate launches | Yes | Parent/inherit only unless separately routed | Verified pass-through behind gateway |
| `/model <id>` | Typed directly | Unlimited | Yes | Does not configure heterogeneous subagents | Pass-through behind gateway |
| `ANTHROPIC_CUSTOM_MODEL_OPTION` | One custom picker row | One | Yes | Not proven for arbitrary frontmatter | Must restart; allowlist must include it |
| `ANTHROPIC_DEFAULT_*_MODEL` alias mapping | Built-in alias rows | Up to four semantic slots | Yes | **Best candidate for heterogeneous aliases** | Models appear under Claude family names |
| CLIProxyAPI OAuth model alias to `claude-*` | Gateway discovery can list it | Potentially many | Yes | Raw full-ID behavior still must be tested | Can trigger Claude-specific capability assumptions |
| `availableModels` | Filters existing rows | N/A | Yes | Yes | It does not create arbitrary models by itself |

### Can every Antigravity model be called through Claude Code?

Three separate conditions must hold:

1. CLIProxyAPI must route the exact model ID to an eligible credential.
2. Its Anthropic→provider translator must preserve enough of the request for that model.
3. Claude Code must be told the model ID through a launch flag, typed selection, alias, or custom picker row.

A model appearing in CLIProxyAPI `/v1/models` proves only catalog visibility. It does not prove tool compatibility, image output, context sizing, quota availability, or subagent selection.

---

## Subagent routing behind `ANTHROPIC_BASE_URL`

### What the docs promise

The current [subagent docs](https://code.claude.com/docs/en/sub-agents#choose-a-model) say `model:` accepts:

- `opus`, `sonnet`, `haiku`, or `fable`;
- a full model ID accepted by `--model`;
- `inherit`;
- omission, which defaults to inherit.

The current [model configuration docs](https://code.claude.com/docs/en/model-config) say that behind an LLM gateway or custom `ANTHROPIC_BASE_URL`, the gateway defines model names and Claude Code passes arbitrary strings without normal Anthropic model validation.

### What the issue 9 proxy log proved

On Claude Code 2.1.217 → CLIProxyAPI 7.2.95:

- main `--model gpt-5.6-sol` worked;
- main `--model gpt-5.6-luna` worked;
- frontmatter `model: gpt-5.6-luna` did **not** work;
- the subagent request used `claude-opus-4-8` and failed with unknown provider;
- setting `CLAUDE_CODE_SUBAGENT_MODEL=gpt-5.6-luna` did work and produced a clean Sol-main/Luna-subagent split.

This means there is a resolution layer between the documented frontmatter value and the actual API request that can discard an arbitrary non-Claude model before it reaches the gateway.

### Why Agent-tool arbitrary IDs remain suspect

Historical and current issue evidence shows the Agent/Task tool schema has often been narrower than the main-model selector:

- [Claude Code #18873](https://github.com/anthropics/claude-code/issues/18873) documented full IDs rejected while short aliases reached the wrong model path.
- [Claude Code #34821](https://github.com/anthropics/claude-code/issues/34821) documented the Agent/Task `model` parameter as a hardcoded alias enum and requested custom gateway aliases; it was closed as not planned.
- [Claude Code #5680](https://github.com/anthropics/claude-code/issues/5680) documented custom gateway subagents falling back to the default model in headless/SDK mode.
- [Claude Code #65863](https://github.com/anthropics/claude-code/issues/65863) remains open for a custom Anthropic-compatible provider where the main model works and Agent-spawn requests fail on a different request path.

The docs have improved since those reports, but the issue 9 empirical failure happened on 2.1.217, one release before current. Treat the discrepancy as a live compatibility problem.

### Best candidate for heterogeneous routing: alias indirection

A single gateway can route different model IDs while the Claude Code Agent tool uses only built-in aliases:

```text
opus   -> premium reasoning model
sonnet -> standard implementation model
haiku  -> high-volume mechanical model
fable  -> optional long-horizon/highest model
```

For example, a future approved claudex profile could map:

```text
opus   -> gemini-pro-agent or gpt-5.6-sol
sonnet -> gemini-3-flash-agent or gpt-5.6-terra
haiku  -> gemini-3.5-flash-extra-low or gpt-5.6-luna
fable  -> claude-opus-4-6-thinking or another explicitly chosen premium model
```

Then agent frontmatter/invocations use only `opus`, `sonnet`, `haiku`, or `fable`. This avoids arbitrary-ID validation in the Agent tool while allowing the gateway to see distinct upstream IDs.

Constraints:

- `CLAUDE_CODE_SUBAGENT_MODEL` must be unset or `inherit`, otherwise it overrides all lanes.
- This gives at most four first-class slots.
- Claude thinks in Claude family semantics, so descriptions and routing instructions must explicitly state the real mapped role.
- The mapping remains session-wide and base-URL-wide.
- It must be verified with proxy logs before adoption; it is not yet an issue 9 empirical result.

### No native per-agent provider/base URL

`ANTHROPIC_BASE_URL` is session-wide. Claude Code cannot keep the main model on one provider and give a subagent a different endpoint. [Issue #38698](https://github.com/anthropics/claude-code/issues/38698) asks for exactly this feature and remains open.

A multi-provider gateway can still perform heterogeneous routing by `model` field because all requests reach the same base URL. The gateway, not Claude Code, owns provider selection.

---

## GPT-5.6 Sol context mismatch and the local failure

This section complements the local metadata-only analysis stored beside this dossier in `LOCAL-SESSION-AND-FAILURE-ANALYSIS.md`.

### Do not infer tokens from 1.86 MB

The retained incident has two different byte-size groups:

- about **1.839 MB** main requests routed to Grok 4.5 failed locally with `auth_unavailable`; they were not the context-window error;
- about **1.481–1.501 MB** subagent requests routed to `gpt-5.6-sol` reached ChatGPT Codex and received the explicit context-window error.

The roughly 3.0 MB error-log files contain both the downstream Claude request and translated upstream Codex request. Their file size is not the request size; `Content-Length` is the meaningful byte count.

Bytes and model tokens are not interchangeable:

- prose often averages around 3–5 bytes per token;
- source code, JSON, XML, escaped strings, and identifiers can be closer to 2–4 bytes per token;
- Base64 and some structured content can tokenize less efficiently;
- tool definitions, system instructions, history, tool results, images/documents, and generated reasoning state all contribute differently.

A 1.5 MB structured coding-agent request could plausibly exceed 272K tokens, but it cannot be assigned an exact token count from byte size alone.

### Native API capacity versus Codex product capacity

#### OpenAI API model

The [GPT-5.6 Sol API page](https://developers.openai.com/api/docs/models/gpt-5.6-sol) states:

- context: 1,050,000 tokens;
- maximum output: 128,000 tokens;
- requests above 272K input tokens receive higher API pricing for the full request.

That is the API product's model envelope.

#### ChatGPT-authenticated Codex subscription

The current native Codex catalog in [openai/codex](https://github.com/openai/codex/blob/4462b9deef211723b781b426f5e5d36a5777115f/codex-rs/models-manager/models.json) says:

```json
{
  "slug": "gpt-5.6-sol",
  "context_window": 272000,
  "max_context_window": 272000
}
```

The same value appears for Terra and Luna. This is primary-source evidence that the ChatGPT/Codex product route currently budgets a 272K window even though the API model is 1M-class.

This pattern has precedent: [OpenAI Codex issue #9429](https://github.com/openai/codex/issues/9429) described 400K-class GPT-5.2 models using an effective 272K input window after reserving up to 128K for output. [Issue #19464](https://github.com/openai/codex/issues/19464) similarly asked OpenAI to expose a model's full 1M API context to subscription-authenticated Codex, confirming that API capacity and Codex product capacity can differ.

#### CLIProxyAPI metadata mismatch

CLIProxyAPI v7.2.96 advertises 372K context plus 128K output for GPT-5.6 subscription models. This differs from current OpenAI Codex source. The proxy registry is therefore not authoritative for the actual upstream admission limit.

A prior [CLIProxyAPI large-prompt issue #636](https://github.com/router-for-me/CLIProxyAPI/issues/636) reached the same architectural conclusion: the proxy's advertised model metadata did not establish the effective upstream route limit. The actual cloud/Codex backend decided admission.

### Exact local upstream error

The four retained Codex attempts returned:

```json
{
  "error": {
    "message": "Your input exceeds the context window of this model. Please adjust your input and try again.",
    "type": "invalid_request_error",
    "param": "input",
    "code": "context_length_exceeded"
  }
}
```

The local log retained the message but did not disclose `requested_tokens` and `limit_tokens`. [OpenAI Codex issue #8190](https://github.com/openai/codex/issues/8190) shows the same error shape for an earlier model. Other OpenAI constraints can be more explicit—for example, [issue #23694](https://github.com/openai/codex/issues/23694) reported `array_above_max_length` with actual and maximum input-item counts—but the context error does not provide N versus M.

### What occupied the failed request

Metadata from the local logs:

- 57 messages in the first attempt pair, then 52 after partial reduction;
- 176 downstream tools;
- 177 translated upstream tools;
- about 1.50 MB downstream and about 1.505 MB upstream in the first pair;
- about 1.481 MB downstream and about 1.484 MB upstream after trimming five messages.

The translated request was only a few kilobytes larger. That argues against a simple “proxy duplicated the whole history” bug.

CLIProxyAPI's [Claude→Codex translator](https://github.com/router-for-me/CLIProxyAPI/blob/285322cd97add6b21f60c267debec44fbec74060/internal/translator/codex/claude/codex_claude_request.go):

- filters the Claude Code attribution system block;
- converts remaining system text once into a developer message;
- converts messages and tool results once into Responses input items;
- converts each tool schema once;
- maps thinking to Codex reasoning;
- defaults reasoning to `medium` and requests encrypted reasoning content.

No obvious full-history or full-tool-array duplication is visible in that translation path. The dominant known pressure is the very large tool catalog plus accumulated subagent history.

### `ENABLE_TOOL_SEARCH=false` is a major context amplifier

The issue 9 launcher set `ENABLE_TOOL_SEARCH=false`. Current [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search) says this disables deferred tool discovery and loads **all tool definitions into context on every turn**. Anthropic's [tool-search guide](https://code.claude.com/docs/en/agent-sdk/tool-search) estimates that 50 tool definitions can consume roughly 10K–20K tokens; this incident sent 176/177 tools.

That setting is reasonable only for a very small toolset. In claudex it likely consumed a substantial share of the 272K Codex window before messages and tool results were counted.

### Claude Code's own gateway context accounting can also be wrong

Claude Code often treats unknown custom-gateway models as 200K because gateway discovery consumes names but not arbitrary context metadata. Relevant reports:

- [#68522](https://github.com/anthropics/claude-code/issues/68522): no reliable way to declare a custom gateway model above 200K; `/v1/models` context metadata is ignored.
- [#46416](https://github.com/anthropics/claude-code/issues/46416): third-party Anthropic-compatible providers fall back to a hardcoded 200K context.
- [#77247](https://github.com/anthropics/claude-code/issues/77247): known 1M Claude models behind a gateway can still be budgeted at 200K because provider detection fails; `CLAUDE_CODE_USE_GATEWAY=1` can fix that particular known-Claude case.

For `gpt-5.6-sol` over ChatGPT OAuth, a 200K client budget is not the cause of the upstream 272K limit, but inconsistent client budgeting can produce premature compaction or let a subagent request grow in unexpected ways if its auto-compact path differs.

### Likely root cause ranking

| Hypothesis | Assessment | Confidence |
|---|---|---|
| The 1.5 MB subagent request exceeded the ChatGPT Codex route's effective window. | Explicit upstream error; native Codex catalog is 272K. | **High** |
| GPT-5.6 Sol should have accepted it because the API model is 1.05M. | Wrong product boundary: API and ChatGPT Codex subscription expose different effective windows. | **High** |
| The 1.86 MB main request was the context failure. | Refuted by retained logs; it failed on Grok auth availability before upstream dispatch. | **High** |
| Tool schemas materially contributed. | 176/177 tools and tool search disabled; tool schemas count as input. | **High** |
| CLIProxyAPI duplicated the entire request. | Translated request grew only a few KB; source converts structures once. | **Low / unsupported** |
| Malformed history/tool-call state caused the provider error. | Provider explicitly returned context overflow; no structural error observed. | **Low** |
| Proxy model metadata correctly represented the upstream limit. | It conflicts with the native Codex catalog and actual rejection. | **Low** |

### Remediation runbook

No configuration was changed during this research. For a future approved hardening pass:

1. **Remove `ENABLE_TOOL_SEARCH=false` from claudex** or set tool search to `true`/`auto`; do not preload 176 schemas.
2. Give each subagent an explicit minimal tool allowlist. A research agent should not inherit every MCP tool.
3. Spawn fresh, bounded subagents with a compact brief; do not carry the full parent transcript or large raw research dumps.
4. Treat `context_length_exceeded` as non-retryable without input reduction. The streaming/non-streaming retry pair simply replayed the same oversized request.
5. Run `/compact` or start a new chat/subagent before the request reaches the effective Codex limit.
6. Budget against **272K for ChatGPT-authenticated GPT-5.6 Sol** until a live native Codex catalog or successful long-context probe proves a higher product limit.
7. Reserve output/reasoning headroom. Do not budget all 272K to user/history/tool input.
8. Inspect SSE `event: error` even when the HTTP response has already opened with status 200.
9. Record estimated tokens by component: system, messages, tools, tool results, images/files, and translated request. Bytes alone are insufficient.
10. If true 1M Sol context is required, use the metered OpenAI API route whose official model page advertises 1.05M; do not assume ChatGPT subscription OAuth exposes it.
11. Keep CLIProxyAPI and Claude Code updated, but do not expect v2.1.218 or CLIProxyAPI v7.2.96 to remove the upstream Codex product limit.

---

## Proposed quota-first routing policy

### Policy A — recommended and compliant

| Priority | Lane | Use for | Escalate when |
|---:|---|---|---|
| 1 | Official Antigravity: Gemini 3.6 Flash Low/Medium | Bulk research, summaries, extraction, mechanical coding, first-pass implementation | Verification fails, architecture uncertainty, repeated hallucination |
| 2 | Official Antigravity: Gemini 3.6 Flash High | Moderate reasoning, agent loops, broader changes | Cross-system architecture or hard debugging remains unresolved |
| 3 | Official Antigravity: Gemini 3.1 Pro Low/High | Architecture, migrations, hard debugging, long-context synthesis | Independent review or different model family is needed |
| 4 | claudex: GPT-5.6 Luna/Terra | High-volume harness work and ordinary implementation | Quality/complexity requires Sol |
| 5 | claudex: GPT-5.6 Sol | Hard implementation, research synthesis, quality-first reasoning | Final high-stakes review needs first-party Claude or another independent model |
| 6 | Normal Claude Code: Opus/Fable as appropriate | Commitment-boundary judgment, adversarial review, channel/connector-dependent work | N/A |

This consumes Ultra quota first without putting Ultra OAuth inside CLIProxyAPI.

### Policy B — technically possible but not recommended

If someone knowingly accepts Google's enforcement risk, CLIProxyAPI can technically route Antigravity models through Claude Code. A capacity-first order would be Flash Lite/Low → Flash Medium/High → Pro Low/High → separate Claude quotas. However:

- the route itself violates official terms;
- the risk exists before considering model choice or volume;
- repeated flags can become permanent;
- using a secondary account, cloaking prompts, changing IP behavior, or impersonating official telemetry would be evasion, not a safety policy.

This dossier does not recommend or provide an implementation for that path.

### Harness model policy if alias indirection is approved later

A practical four-slot claudex layout should optimize for role rather than provider brand:

| Claude Code alias | Real gateway role | Default workload | Premium protection |
|---|---|---|---|
| `haiku` | Cheapest reliable high-volume model | Search, extraction, file inventory, simple tests, mechanical edits | Never use premium reasoning here |
| `sonnet` | Balanced implementation model | Normal coding, docs, QA, medium research | Default worker lane |
| `opus` | Strong reasoning model | Architecture, difficult debugging, review | Use only after a concrete trigger |
| `fable` | Highest/long-horizon model if needed | Multi-hour synthesis or final judgment | Explicit opt-in only |

Use an explicit escalation trigger: failed validation, unresolved cross-file dependency, architectural ambiguity, conflicting evidence, or a security/release commitment. Do not escalate just because a task is long.

---

## Concrete answers to issue 10

### Does Google still ban or suspend accounts for unofficial Antigravity/Gemini OAuth use?

**Yes.** Official terms prohibit it, official enforcement/remediation remains active, and dated firsthand reports continue. Confidence: **high**.

### Is an Antigravity Ultra login through CLIProxyAPI a supported route?

**Technically supported by CLIProxyAPI, explicitly unsupported by Google for third-party clients.** Do not conflate those meanings of “supported.”

### What is the supported Google route for Claude Code?

A **Google AI Studio / Gemini Developer API key** or **Vertex AI credentials**, with API/project quota and billing. These do not consume the consumer Ultra subscription pool.

### Is Gemini 3.5 Flash real/current?

**Yes, but it is no longer newest.** It remains in Antigravity alongside the newer Gemini 3.6 Flash.

### Can CLIProxyAPI expose all Antigravity models to Claude Code's picker through discovery?

**No, not under their native IDs.** Claude Code filters gateway-discovered IDs to `claude*`/`anthropic*`. Exact IDs can still be launched/typed, mapped to built-in aliases, or aliased by the gateway.

### Can every catalog model be used successfully in Claude Code?

**Not proven.** Catalog visibility does not prove protocol/tool/image compatibility, quota eligibility, or context behavior. Gemini 3.6 support is currently partial.

### Can subagents use arbitrary heterogeneous gateway model IDs?

**Documentation says more than the current claudex test delivered.** A global arbitrary subagent model is verified through `CLAUDE_CODE_SUBAGENT_MODEL`; arbitrary frontmatter failed. Built-in alias indirection is the best next experiment for heterogeneous models through one gateway.

### Can subagents use different base URLs/providers natively?

**No.** The base URL is session-wide; heterogeneous providers require one multi-provider gateway.

### Did the 1.86 MB request prove GPT-5.6 Sol lacks 1M context?

**No.** The retained 1.84 MB request was a Grok auth failure. A later 1.48–1.50 MB Codex subagent request got the context error. Bytes are not tokens.

### Why can 1M-class GPT-5.6 Sol reject the Codex request?

Because the 1.05M figure belongs to the API model. Current ChatGPT-authenticated native Codex source budgets GPT-5.6 Sol at **272K**, and the local request also carried 176/177 tool definitions with tool search disabled.

---

## Recommended decision

1. **Do not add Antigravity or Gemini CLI OAuth to CLIProxyAPI.**
2. **Do not promise an Antigravity-first claudex profile.** Use official Antigravity as the Ultra lane and claudex as a separate Codex lane.
3. **Harden claudex context use before further subagent tests:** enable deferred tool search, restrict agent tools, and budget GPT-5.6 subscription requests at 272K.
4. **Update the model inventory language:** Gemini 3.6 Flash is current; 3.5 Flash is still available but no longer newest; CLIProxyAPI 3.6 support is partial.
5. **Retest heterogeneous subagents using built-in alias indirection on Claude Code v2.1.218+ with proxy-log proof.** Do not rely on raw arbitrary frontmatter IDs until that test passes.
6. **Keep a metered API-key option separate and explicit** if true 1M GPT-5.6 or supported Gemini API integration is required.

---

## Primary sources

### Google policy and products

- [Google Antigravity Additional Terms](https://antigravity.google/terms)
- [Google Antigravity FAQ](https://antigravity.google/docs/faq)
- [Google Antigravity plans and quotas](https://antigravity.google/docs/plans?hl=en)
- [Changes to Antigravity plans](https://www.antigravity.google/blog/changes-to-antigravity-plans)
- [Antigravity model inventory](https://antigravity.google/docs/models)
- [Antigravity changelog](https://www.antigravity.google/changelog)
- [Gemini CLI terms/privacy](https://geminicli.com/docs/resources/tos-privacy/)
- [Gemini CLI FAQ](https://geminicli.com/docs/resources/faq/)
- [Official Antigravity bans/reinstatement announcement](https://github.com/google-gemini/gemini-cli/discussions/20632)
- [Google APIs Terms](https://developers.google.com/terms/)
- [Google OAuth 2.0 policies](https://developers.google.com/identity/protocols/oauth2/policies)

### Google models

- [Gemini 3.6 Flash in Antigravity](https://antigravity.google/blog/gemini-3-6-flash-in-google-antigravity)
- [Google Gemini 3.6 launch post](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-6-flash-3-5-flash-lite-3-5-flash-cyber/)
- [Gemini 3.5 Flash in Antigravity](https://www.antigravity.google/blog/gemini-3-5-flash-in-google-antigravity)
- [Gemini 3.5 capabilities](https://ai.google.dev/gemini-api/docs/whats-new-gemini-3.5)
- [Gemini 3.1 Pro model page](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-pro-preview)

### CLIProxyAPI

- [CLIProxyAPI repository](https://github.com/router-for-me/CLIProxyAPI)
- [CLIProxyAPI v7.2.96](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v7.2.96)
- [v7.2.96 server/login flags](https://github.com/router-for-me/CLIProxyAPI/blob/285322cd97add6b21f60c267debec44fbec74060/cmd/server/main.go)
- [v7.2.96 configuration example](https://github.com/router-for-me/CLIProxyAPI/blob/285322cd97add6b21f60c267debec44fbec74060/config.example.yaml)
- [v7.2.96 model registry](https://github.com/router-for-me/CLIProxyAPI/blob/285322cd97add6b21f60c267debec44fbec74060/internal/registry/models/models.json)
- [Antigravity model fetch helper](https://github.com/router-for-me/CLIProxyAPI/blob/285322cd97add6b21f60c267debec44fbec74060/cmd/fetch_antigravity_models/main.go)
- [Antigravity capability fetch](https://github.com/router-for-me/CLIProxyAPI/blob/285322cd97add6b21f60c267debec44fbec74060/sdk/cliproxy/antigravity_models.go)
- [Claude→Codex translator](https://github.com/router-for-me/CLIProxyAPI/blob/285322cd97add6b21f60c267debec44fbec74060/internal/translator/codex/claude/codex_claude_request.go)
- [Antigravity provider guide](https://help.router-for.me/configuration/provider/antigravity.html)

### Claude Code

- [Model configuration](https://code.claude.com/docs/en/model-config)
- [Subagent model selection](https://code.claude.com/docs/en/sub-agents#choose-a-model)
- [Gateway connection](https://code.claude.com/docs/en/llm-gateway-connect)
- [Gateway protocol and discovery](https://code.claude.com/docs/en/llm-gateway-protocol#model-discovery)
- [MCP tool search](https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search)
- [Agent SDK tool-search guide](https://code.claude.com/docs/en/agent-sdk/tool-search)
- [Claude Code v2.1.218](https://github.com/anthropics/claude-code/releases/tag/v2.1.218)
- [Claude Code v2.1.211](https://github.com/anthropics/claude-code/releases/tag/v2.1.211)

### OpenAI / Codex

- [GPT-5.6 Sol API model](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
- [Current native Codex model catalog](https://github.com/openai/codex/blob/4462b9deef211723b781b426f5e5d36a5777115f/codex-rs/models-manager/models.json)
- [Codex issue #9429: 272K effective context](https://github.com/openai/codex/issues/9429)
- [Codex issue #19464: API 1M versus subscription product window](https://github.com/openai/codex/issues/19464)
- [Codex issue #8190: context error format](https://github.com/openai/codex/issues/8190)
- [Codex issue #23694: explicit input-array item limit](https://github.com/openai/codex/issues/23694)

### Dated firsthand enforcement reports

- [CLIProxyAPI #1637](https://github.com/router-for-me/CLIProxyAPI/issues/1637)
- [CLIProxyAPI #1803](https://github.com/router-for-me/CLIProxyAPI/issues/1803)
- [CLIProxyAPI #1814](https://github.com/router-for-me/CLIProxyAPI/issues/1814)
- [Google AI forum suspension review request](https://discuss.ai.google.dev/t/request-for-review-of-gemini-cli-antigravity-gemini-code-assist-suspension/146723)
- [Google AI forum one-month unresolved follow-up](https://discuss.ai.google.dev/t/follow-up-after-1-month-gemini-antigravity-gemini-cli-suspension-still-unresolved/146981)
