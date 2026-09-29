# Research: agent benchmark comparison 2026-09 incl. DeepSeek API worker tier

## 1. Scope & Original Request
**Request:** a current comparison of agentic-coding models against benchmarks and prices, to choose model routing for Veyyon lanes, including an evaluation of the **DeepSeek direct API as a worker tier next to the `opus` lane**. The first attempt (Gemini Flash) died on a provider `PROHIBITED_CONTENT` finish_reason, so this run used another provider.

This issue builds on earlier issues and does not redo them:
- [#192](https://github.com/Wladefant/super-board/issues/192): Astra vs Sol token economics. Not re-measured here.
- [#195](https://github.com/Wladefant/super-board/issues/195): review ROI and risk thresholds. Not re-measured.
- [#210](https://github.com/Wladefant/super-board/issues/210): OpenRouter free/cheap census, smoke tests and `or-*` roles. This issue re-scores those picks against current benchmarks.

**Out of scope:** config edits. Nothing in `config.yml` / `models.yml` was changed. Everything below is a recommendation only.

## 2. TL;DR
1. **Only one public coding-agent benchmark still covers the 2026-09 models: Terminal-Bench 4.0 as measured by Artificial Analysis (AA).** SWE-bench Verified, SWE-bench Pro, Aider polyglot, LiveCodeBench, BFCL and τ-bench have no scores for Opus 5.5, Fable 5.1, GPT-6, Gemini 3.8 Flash or DeepSeek V4.1 (coverage table in §7.C). So ranking and cost per solve below come from AA TB 4.0 and AA's measured cost per task.
2. **DeepSeek V4.1 Flash is not an Opus-tier worker.** On AA TB 4.0 it scores **26.8 %**, against **56.6 %** for Opus 5.5 high and **52.5 %** for Opus 5.5 medium. DeepSeek's own model card gives it 31.2 % against 51.8 % for Opus 5.0. Per solved task it is only about **2.1×** cheaper than Opus 5.5 high ($4.32 vs $9.05), and it fails about twice as often. Each failure also costs an orchestrator turn and a re-dispatch.
3. **It is a strong Flash-tier worker.** It beats Gemini 3.8 Flash (high) on TB 4.0: **26.8 % vs 19.7 %**. Its measured cost is **$1.16 vs $5.84 per task** and **$4.32 vs $29.66 per solved task** at peak price. Off-peak halves the DeepSeek number. It is the best current candidate for **Flash overflow**: Antigravity quota exhaustion, `PROHIBITED_CONTENT` refusals, or a dead sidecar.
4. **The #210 cheap tier points at a retired model.** `openrouter/deepseek/deepseek-v4-flash` is the 2026-04 V4-Flash (weights served by third parties, e.g. Baidu at $0.049/$0.098). DeepSeek retired V4-Flash on 2026-09-10, and AA measures that model (0420) at 2.5 % on TB 4.0. `cohere/north-mini-code:free` (the `or-task` primary) scores **0.5 %** on TB 4.0 with an AA Intelligence Index of 9.9. Keep it for triage and classification only, never for implementation or review.
5. **Data policy is the real gate for DeepSeek direct.** DeepSeek's privacy policy says it will *"directly collect, process and store your Personal Data in People's Republic of China"*. Our OpenRouter account guardrail already **rejects the first-party DeepSeek endpoint** with the reason *"Paid model training violation (account settings)"* (observed live, §7.D). The same V4.1-Flash open weights are served by US hosts through the existing OpenRouter key: tool calling was verified on **Together** and **DeepInfra**.

## 3. Current comparison (AA Terminal-Bench 4.0 + measured cost), 2026-09-25
Source: [artificialanalysis.ai/models](https://artificialanalysis.ai/models), per-model `intelligenceIndexEvaluations[terminalbench-4-0]` (score, `costPerTask`, `timePerTask`). **TB4 $/solved = TB4 $/task ÷ TB4 score**, i.e. the API list-price cost of one successful task. Prices are AA's list prices (DeepSeek at the **peak** rate $0.30/$1.20; off-peak is half). "with fallback" is AA's own label for the Anthropic entries.

| AA model | AA Intelligence Index | TB 4.0 % | TB 2.1 % | ITBench-SRE % | TB4 $/task | TB4 min/task | TB4 $/solved | $ in/out/cache per 1M |
|---|---|---|---|---|---|---|---|---|
| Claude Opus 5.5 (max with fallback) | 57.6 | 59.6 | — | 38.2 | 13.11 | — | 22.00 | 4/20/0.2 |
| Claude Opus 5.5 (xhigh with fallback) | 56.0 | 59.6 | — | — | 8.78 | 19.8 | 14.73 | 4/20/0.2 |
| GPT-6 Astra (max) | 52.7 | 59.1 | 88.4 | 48.6 | 8.50 | 23.1 | 14.38 | 10/50/1 |
| Claude Opus 5.5 (high with fallback) | 53.6 | 56.6 | — | — | 5.12 | 12.3 | 9.05 | 4/20/0.2 |
| GPT-6 Astra (high) | 50.9 | 54.0 | 89.9 | — | 4.05 | 9.4 | 7.49 | 10/50/1 |
| Claude Opus 5.5 (medium with fallback) | 51.2 | 52.5 | — | — | 4.04 | 9.9 | 7.70 | 4/20/0.2 |
| Claude Fable 5.1 (max with fallback) | 53.4 | 52.0 | 91.4 | 49.5 | 19.22 | 29.5 | 36.94 | 10/50/0.25 |
| Claude Fable 5.1 (high with fallback) | 51.2 | 52.0 | 89.9 | — | 11.64 | 21.3 | 22.38 | 10/50/0.25 |
| GPT-6 Astra (medium) | 49.6 | 49.5 | 89.5 | — | 4.43 | 10.3 | 8.95 | 10/50/1 |
| Claude Opus 5 (max) | 50.8 | 49.0 | 89.1 | — | 19.29 | 38.3 | 39.37 | 5/25/0.5 |
| Claude Fable 5.1 (medium with fallback) | 48.9 | 44.9 | 88.0 | — | 9.12 | 16.4 | 20.30 | 10/50/0.25 |
| GPT-6 Sol (max) | 47.5 | 43.9 | — | 49.4 | 4.00 | 17.7 | 9.11 | 2/10/0.2 |
| GPT-6 Astra (low) | 45.8 | 41.9 | 88.0 | — | 2.25 | 5.4 | 5.36 | 10/50/1 |
| GLM-5.3 (max) | 44.8 | 41.9 | 83.9 | 46.1 | 8.06 | 37.4 | 19.24 | 1.4/4.4/0.26 |
| GPT-5.6 Sol (max) | 47.0 | 39.9 | 88.0 | 56.2 | 8.09 | 17.2 | 20.28 | 4/20/0.4 |
| Qwen3.8 Max (0902) | 45.4 | 38.9 | 88.8 | — | 18.68 | 95.6 | 48.02 | 2/6/0.25 |
| GPT-5.6 Terra (max) | 42.1 | 35.4 | 88.0 | 51.0 | 6.39 | 24.8 | 18.07 | 2/12/0.2 |
| Claude Opus 5 (medium) | 44.8 | 34.3 | 86.1 | — | 9.18 | 17.4 | 26.74 | 5/25/0.5 |
| Muse Spark 1.3 (max) | 48.1 | 33.3 | 84.3 | 33.2 | 6.94 | 16.3 | 20.82 | 1.25/4.25/0.15 |
| GLM-5.3-Flash | 41.8 | 32.8 | 84.3 | 51.2 | 0.79 | 58.3 | 2.41 | 0.15/0.5/0.026 |
| Claude Opus 5.5 (low with fallback) | 42.3 | 31.3 | — | — | 2.08 | 4.9 | 6.64 | 4/20/0.2 |
| DeepSeek V4.1 Flash (max) | 39.5 | 26.8 | — | 46.9 | 1.16 | 14.7 | 4.32 | 0.3/1.2/0.006 |
| GPT-6 Sol (high) | 42.8 | 26.3 | — | — | 1.60 | 7.1 | 6.09 | 2/10/0.2 |
| Qwen3.8-Flash-Next | 39.8 | 25.3 | 86.1 | — | 0.88 | 58.3 | 3.48 | 0.15/0.47/0.016 |
| Gemini 3.8 Flash (high) | 40.9 | 19.7 | 87.6 | 52.5 | 5.84 | 10.2 | 29.66 | 0.75/3.75/0.075 |
| Gemini 3.8 Flash (medium) | 39.8 | 19.7 | 83.9 | — | 4.61 | — | 23.41 | 0.75/3.75/0.075 |
| GPT-6 Sol (medium) | 39.8 | 18.7 | — | — | 1.12 | 4.4 | 5.99 | 2/10/0.2 |
| Claude Sonnet 5 (max) | 38.2 | 14.1 | 80.5 | — | 19.69 | 40.2 | 139.27 | 2/10/0.2 |
| DeepSeek V4 Pro 0813 (max) | 36.0 | 14.1 | 78.7 | — | 3.76 | 28.2 | 26.55 | 1.32/3.96/0.044 |
| GPT-6 Luna (max) | 37.3 | 12.6 | — | — | 0.24 | 16.0 | 1.87 | 0.1/0.5/0.01 |
| Kimi K3 (max) | 43.6 | 12.6 | 85.0 | 47.7 | 5.09 | 53.4 | 40.30 | 3/15/0.3 |
| Gemini 3.8 Flash (low) | 33.5 | 10.1 | 83.1 | — | — | — | — | 0.75/3.75/0.075 |
| DeepSeek V4.1 Flash (Non-reasoning) | 24.7 | 5.6 | — | — | 1.06 | 15.7 | 19.01 | 0.3/1.2/0.006 |
| MiniMax-M3 | 29.2 | 2.0 | 65.2 | — | 3.04 | 19.3 | 150.47 | 0.3/1.2/0.06 |
| Nemotron 3.5 Lightning | 12.9 | 0.5 | 24.3 | — | 0.36 | 4.9 | 71.84 | 0.07/0.22/0.05 |
| North Mini Code | 9.9 | 0.5 | 35.6 | — | 0.00 | 8.9 | — | 0/0/None |

**How to read it:**
- **Frontier tier (≥ 50 % TB4):** cheapest per solve are **Astra high ($7.49)**, **Opus 5.5 medium ($7.70)** and **Opus 5.5 high ($9.05)**. Fable 5.1 costs 2-4× more per solve at the same or lower score. Keep Fable on orchestration only, as policy already says.
- **Mid tier (35-45 %):** **Astra low** (41.9 %, $5.36/solve, 5.4 min/task) is the cheapest route to about 40 % solve rate. GLM-5.3 max has the same score at 3.6× the cost.
- **Cheap tier (≤ 35 %):** **GLM-5.3-Flash** (32.8 %, $2.41/solve, but 58 min/task at about 43 tok/s), **DeepSeek V4.1 Flash** (26.8 %, $4.32/solve peak or about $2.16 off-peak, 14.7 min/task, 233 tok/s) and **GPT-6 Luna** (12.6 %, **$1.87/solve**, lowest ceiling).
- **Gemini 3.8 Flash (the current `task` default)** is the most expensive per solve in its class on list price: $29.66 at high, $23.41 at medium. It stays sensible **only because it runs on the Antigravity flat-rate subscription**, so its marginal token cost is zero while quota lasts. [INFERENCE] Every Flash failure, quota wall or content refusal is where a pay-per-token fallback matters, and DeepSeek V4.1 Flash is the cheapest capable one.
- DeepSeek V4.1 Flash burns **285k output tokens per TB4 task**, against 100k for Opus 5.5 high and 30k for Astra medium. It is cheap because tokens are cheap, not because it is terse. Wall-clock time per task (14.7 min) is comparable to Opus 5.5 high (12.3 min).

### Vendor-reported (DeepSeek model card, max effort, DeepSeek Harness Minimal)
Source: [huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash). These are **vendor numbers, not independently verified**.

| Benchmark | Opus-5.0 | GPT-5.6 Sol | K3 | GLM-5.3 | DS-V4-Pro | DS-V4.1-Flash |
|---|---|---|---|---|---|---|
| Terminal-Bench 2.1 | 89.1 | 88.8 | 88.3 | 88.2 | 87.9 | **90.6** |
| Terminal-Bench 4.0 | **51.8** | 39.9 | 12.6 | 37.9 | 12.4 | 31.2 |
| DeepSWE v1.1 | 74.0 | 73.0 | 67.5 | 66.9 | 62.7 | **74.2** |
| NL2Repo-Bench | **75.3** | 56.8 | 58.0 | 58.0 | 61.5 | 64.0 |
| ProgramBench | **37.0** | 23.0 | 17.5 | 19.0 | 15.5 | 20.3 |
| AutomationBench | 50.3 | 45.8 | 46.7 | 48.8 | 43.2 | **54.8** |

**Scaffold sensitivity**, same model and vendor card: DeepSWE v1.1 scores 74.2 in mini-SWE, **69.8 in Claude Code** and **65.6 in Codex**. Expect the Veyyon-harness number to sit below the headline figure. [INFERENCE] The pattern is "SWE-style single-issue repair near Opus, long-horizon terminal work (TB 3/4, ProgramBench, NL2Repo) far below Opus". That matches AA's independent TB 4.0 result.

## 4. Lane recommendations (by cost per solved task)
| Veyyon lane (current binding) | Recommendation | Why |
|---|---|---|
| `default` / orchestration (Opus 5.5 medium; policy says Fable 5.1 medium) | unchanged | Out of scope. Opus 5.5 medium is the cheaper per-solve of the two ($7.70 vs $20.30). |
| `opus`, `reviewer` (Opus 5.5 high) | **keep**. Do **not** substitute DeepSeek. | 56.6 % vs 26.8 % TB4. High-risk work (money, auth, concurrency, migrations per [#195](https://github.com/Wladefant/super-board/issues/195)) needs the ceiling. Astra high (54.0 %, $7.49/solve) is the per-solve-cheapest peer if Anthropic allowance runs short. |
| `slow` (Astra medium) | keep. Consider **Astra low** for bounded mid-risk slices. | Astra low: 41.9 % at $5.36/solve and 5.4 min/task. |
| `task`, `qa-verifier` (Gemini 3.8 Flash high) | keep primary. **Add DeepSeek V4.1 Flash as the first fallback.** | Higher TB4 than Flash (26.8 vs 19.7), about 5× cheaper per task at list price, different provider and safety filter (covers the `PROHIBITED_CONTENT` failure that killed the first run of this research). |
| `or-cheap`, `or-task` fallback (`deepseek/deepseek-v4-flash`) | **replace with `deepseek/deepseek-v4.1-flash`** pinned to a non-training host (Together/DeepInfra) | The configured id is the retired 2026-04 V4-Flash. V4.1 Flash is the maintained successor. |
| `or-task` primary (`cohere/north-mini-code:free`) | **restrict to triage and classification** | TB4 0.5 %, TB2.1 35.6 %, AA Index 9.9. Not fit for implementation. |
| `or-review` (`meta-llama/llama-3.3-70b-instruct`) | replace with `openai/gpt-6-luna` or `deepseek/deepseek-v4.1-flash` | Llama 3.3 70B is a 2024-12 model with no current agentic benchmark coverage. Luna costs $0.10/$0.50 with a measured TB4 score. Per [#195](https://github.com/Wladefant/super-board/issues/195), small-diff reviews are mostly exempt anyway. |
| batch / async cheap work | optional **GLM-5.3-Flash** | Best cheap-tier $/solve ($2.41) but slow (58 min/task), so only where latency does not matter. |

## 5. DeepSeek direct API: worker-tier evaluation
| Aspect | Finding | Source |
|---|---|---|
| Current model | `deepseek-flash` = **DeepSeek-V4.1-Flash** (released 2026-09-10; 552B MoE, 8B/16B active; MIT open weights). V4-Flash retired. **V4-Pro is being phased out:** since 2026-09-14 04:00 UTC all `deepseek-v4-pro` calls route to V4.1-Flash until V4.1-Pro launches. So today **there is no DeepSeek "Pro" tier to put next to Opus.** | [news 2026-09-10](https://api-docs.deepseek.com/news/news260910) |
| Price per 1M tokens (Flash) | cache hit **$0.003 off-peak / $0.006 peak**; cache miss $0.15 / $0.30; output $0.60 / $1.20. Peak = 01:00-04:00 and 06:00-10:00 UTC Mon-Fri; everything else is off-peak (50 %). | [pricing](https://api-docs.deepseek.com/quick_start/pricing) |
| Cache economics | Agent loops are cache-read dominated. DeepSeek's cache-hit price is **1/25 of Gemini 3.8 Flash ($0.075)** and **1/67 of Opus 5.5 ($0.20)**. | pricing pages / OpenRouter catalog |
| Limits | Concurrency **2,500** per account (Flash); no token-per-minute limit published; `user_id` isolation per lane is possible. Requests not started after 10 min are closed. | [rate limits](https://api-docs.deepseek.com/quick_start/rate_limit) |
| Context / output | 1M context, 384K max output, thinking mode on by default (effort 1-100) | pricing / model card |
| API surfaces | OpenAI Chat Completions, **Anthropic Messages** (`https://api.deepseek.com/anthropic`) and Responses API. Chat Completions **cannot insert tool calls mid-conversation**; Anthropic and Responses can. | [tool calls guide](https://api-docs.deepseek.com/guides/tool_calls) |
| Veyyon support | Built-in provider `deepseek` (`DEEPSEEK_API_KEY`; handbook `reference/providers.md`). The handbook `models-yml.md` warns that DeepSeek returns 400 on `tool_choice` when reasoning is on, so set `supportsToolChoice: false`. **Not configured here:** `DEEPSEEK_API_KEY` is unset and `agent.db` has no `deepseek` credential (checked 2026-09-25). `veyyon models deepseek` lists nothing. | local check |
| Data policy | DeepSeek stores and processes data **in the PRC**. OpenRouter classes the first-party DeepSeek endpoint as a paid-training endpoint, and our account guardrail blocks it. | [privacy policy](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html); live 404 in §7.D |
| Cost at worker scale | [INFERENCE] With AA's TB4 profile (about 285k output tokens per hard task): about **$1.16/task peak and $0.58 off-peak**, against $5.12 for Opus 5.5 high at API price. The routine lanes behind [#192](https://github.com/Wladefant/super-board/issues/192) spent roughly $6.7-17 per task on Sol/Astra. | AA |

**Verdict:** adopt V4.1 Flash as a **Flash-tier overflow and fallback worker**, not as an Opus-tier worker. Revisit when DeepSeek ships V4.1-Pro or AA publishes a V4.1-Pro TB 4.0 score.
**Preferred route:** the existing OpenRouter key with `deepseek/deepseek-v4.1-flash` pinned to a non-training US host. No new vendor account and no PRC data processing. Tool calling is verified below. Price: Together $0.30/$1.20 with $0.006 cache; DeepInfra fp8 $0.14/$0.42 with $0.0042 cache.
**DeepSeek direct** is cheaper still (off-peak cache $0.003) but needs an operator decision on the data policy (§9).

## 6. Dependencies & Parent Issue
Standalone research. It relates to [#192](https://github.com/Wladefant/super-board/issues/192), [#195](https://github.com/Wladefant/super-board/issues/195) and [#210](https://github.com/Wladefant/super-board/issues/210). There are no blockers for the research. Any routing change is a separate config task.

## 7. Verification Evidence
### A. Data pulled 2026-09-25
- AA model pages (JSON embedded in [artificialanalysis.ai/models](https://artificialanalysis.ai/models)): TB 4.0 / TB 2.1 / ITBench-SRE scores, per-evaluation `costPerTask`, `timePerTask`, `outputTokensPerTask`, prices.
- [OpenRouter `/api/v1/models`](https://openrouter.ai/api/v1/models) (460 models) and `/models/<id>/endpoints` for per-host prices, quantization and uptime.
- DeepSeek [pricing](https://api-docs.deepseek.com/quick_start/pricing), [rate limits](https://api-docs.deepseek.com/quick_start/rate_limit), [2026-09-10 release note](https://api-docs.deepseek.com/news/news260910), [model card](https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash).

### B. OpenRouter per-host prices for V4.1 Flash (selection, $/1M tokens: input / output / cache read)
| Host | Quantization | Price | Tools | 30-min uptime |
|---|---|---|---|---|
| DeepSeek (first party) | n/a | 0.15 / 0.60 / 0.003 | yes | 99.97 % (**blocked by our training guardrail**) |
| DeepInfra | fp8 | 0.14 / 0.42 / 0.0042 | yes | 99.90 % |
| Together | n/a | 0.30 / 1.20 / 0.006 | yes | 99.96 % |
| Fireworks | n/a | 0.22 / 0.66 / 0.007 | yes | 99.75 % |
| DekaLLM | n/a | 0.04 / 0.49 / 0.01 | **no** | 99.63 % |

### C. Benchmark coverage: why the table uses AA TB 4.0
| Benchmark | Latest data seen | 2026-09 models covered? | Top entries |
|---|---|---|---|
| SWE-bench Verified ([swebench.com](https://www.swebench.com)) | newest submission 2026-02-26 (180 rows) | **no** | 79.2 (Claude 4.5 Opus agents); bash-only: Claude 4.5 Opus high 76.8, Gemini 3 Flash 75.8, MiniMax M2.5 75.8 |
| SWE-bench Pro ([Scale](https://scale.com/leaderboard/swe_bench_pro_public)) | newest entry 2026-07-09 | **no** | Muse Spark 1.1 61.5, gpt-5.4 xHigh 59.1, Opus 4.6 51.9 (commercial set: 51.5 / 47.1) |
| Aider polyglot | newest entry 2025-10-03 | **no** | gpt-5 high 88.0; DeepSeek-V3.2-Exp 74.2 at $1.30 |
| LiveCodeBench | public release: 28 models, 2025-era | **no** | AA no longer reports it for current models |
| BFCL V4 | last updated 2026-04-12 | **no** | Claude Opus 4.5 77.5 %, Sonnet 4.5 73.2 %, DeepSeek-V3.2-Exp 56.7 % |
| τ³-bench Banking ([taubench.com](https://taubench.com)) | submissions to 2026-08-04 | partly (no Opus 5.5 / GPT-6 / DS V4.1) | Qwen 3.8 Max 55.2, Opus 5 48.7, Grok 4.5 47.9, GPT-5.6 Sol 46.9, Fable 5 39.7 |
| **Terminal-Bench 4.0 / 2.1 (AA)** | 2026-09-22 models present | **yes** | table in §3 |

### D. Live tool-calling smoke test (2026-09-25, existing OpenRouter key, provider pinned, `allow_fallbacks:false`)
Two-turn agent task: the model must call `read_file("backend/pnl.py")`, then fix a sign bug in `return (entry - exit) * qty` for a LONG position.

| Model @ host | Turn 1 | Turn 2 | Latency t1 / t2 | Cost |
|---|---|---|---|---|
| `deepseek/deepseek-v4.1-flash` @ **DeepSeek** | **HTTP 404**: *"Filter by Guardrails removed deepseek (Paid model training violation (account settings))"* | n/a | n/a | $0 |
| `deepseek/deepseek-v4.1-flash` @ **Together** | `tool_calls` → `read_file({"path":"backend/pnl.py"})` | `stop`, correct fix `return (exit - entry) * qty` | 0.41 s / 0.62 s | $0.00033 |
| `deepseek/deepseek-v4.1-flash` @ **DeepInfra** | `tool_calls` → `read_file(backend/pnl.py)` | `tool_calls` (asked for another file; valid agent behaviour) | 1.21 s / 1.58 s | $0.00017 |
| `deepseek/deepseek-v4-flash` (retired weights) @ Baidu | `tool_calls` → `read_file` | `stop`, correct fix | 1.32 s / 2.62 s | $0.00006 |

**Not verified here:** DeepSeek **direct** API calls (no `DEEPSEEK_API_KEY` exists), and a full Veyyon lane run on V4.1 Flash.

## 8. Next Action
1. Operator decision (§9).
2. A separate config task, after an explicit go-ahead:
   - replace `openrouter/deepseek/deepseek-v4-flash` with `openrouter/deepseek/deepseek-v4.1-flash` (host-pinned) in `or-cheap` / `or-task`;
   - restrict `or-task`'s free primary to triage;
   - add V4.1 Flash as the first fallback of `task` / `qa-verifier`;
   - then run one real Veyyon lane end to end on it (`veyyon --no-session --model openrouter/deepseek/deepseek-v4.1-flash -p ...`) and compare against Flash on the same slice.
3. Re-check AA when DeepSeek V4.1-Pro ships. It is the only DeepSeek candidate that could plausibly reach Opus-tier TB4.

## 9. Owner, Authorization & Constraints
- Owner: @Wladefant. Research lane: Veyyon model-routing research.
- No config was edited. The live test spent under $0.001 on the existing OpenRouter key.
- **Operator decision needed:** may PolySimulator / Superboard code and prompts go to (a) **DeepSeek direct** (data stored in the PRC; cheapest), (b) **V4.1 Flash via US hosts on OpenRouter** (recommended; no new account; keeps the no-training guardrail), or (c) neither?

