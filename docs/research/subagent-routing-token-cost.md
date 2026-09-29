# Subagent Routing Research: GPT-6 Astra vs GPT-5.6 Sol Token Economics & Model Allocation

**Author:** Veyyon Research Lane (resumed from `history://AstraSolRoutingResearch`)  
**Target Repository:** `Wladefant/super-board`  
**Tracking Issue:** [#192](https://github.com/Wladefant/super-board/issues/192)  
**Date:** 2026-09-22  

---

## 1. Executive Summary & Context

To curb rapid allowance exhaustion and high per-task spend on `openai-codex/gpt-6-astra`, this report analyzes the empirical token economics of **GPT-6 Astra** vs **GPT-5.6 Sol**, **Claude Opus 5.5**, **Gemini 3.8 Flash**, and **GPT-5.3 Codex Spark** using local runtime telemetry from `C:/Users/wkiri/.veyyon/profiles/default/stats.db` and official CLI model specifications (`veyyon models --json --no-extensions`).

### Standing Operator Ruling (2026-09-22)
The operator has established the following allocation boundaries:
1. **Astra (`openai-codex/gpt-6-astra:medium`) is strictly reserved for reviews only.** (No long multi-turn implementation loops).
2. **Opus 5.5 (`anthropic/claude-opus-5-5:high`) is designated for hard tasks** (auth, concurrency, money paths, migrations, and two-strike escalations).
3. **Gemini 3.8 Flash (`google-antigravity/gemini-3.8-flash:high`) is the default workhorse for easy/routine tasks** (implementation, bug fixes, triage, routine QA).
4. **Sol (`openai-codex/gpt-5.6-sol:high`) is proposed as an explicit subagent role** for heavy analytical modeling or non-Claude deep thinking at 55%–61% lower cost than Astra.

---

## 2. Measurement Methodology & Source Commands

Data was extracted directly from the workstation SQLite database tracking all model invocations and token usage:
- **Telemetry Database:** `C:/Users/wkiri/.veyyon/profiles/default/stats.db` (`messages` table: 689,401 rows total).
- **CLI Commands Executed:**
  - `python -c "import sqlite3; conn = sqlite3.connect(rC:/Users/wkiri/.veyyon/profiles/default/stats.db); ..."` (queries executed for 14-day window `>= 2026-09-08` and all-time records).
  - `veyyon models --json --no-extensions` (authoritative model pricing, context windows, and effort levels).
  - `veyyon usage --json` (active quota windows).

---

## 3. Catalog Unit Pricing & Specification Comparison

*Source: `veyyon models --json --no-extensions`*

| Model ID | Provider | Input ($/1M) | Output ($/1M) | Cache Read ($/1M) | Cache Write ($/1M) | Context Window | Max Output |
|---|---|---|---|---|---|---|---|
| `openai-codex/gpt-6-astra` | `openai-codex` | **$10.00** | **$50.00** | **$1.00** | **$12.50** | 272,000 | 128,000 |
| `openai-codex/gpt-5.6-sol` | `openai-codex` | **$4.00** | **$20.00** | **$0.40** | **$5.00** | 372,000 | 128,000 |
| `anthropic/claude-opus-5-5` | `anthropic` | **$4.00** | **$20.00** | **$0.20** | **$5.00** | 1,000,000 | 128,000 |
| `google-antigravity/gemini-3.8-flash` | `google-antigravity` | **$0.00** | **$0.00** | **$0.00** | **$0.00** | 1,048,576 | 65,536 |
| `openai-codex/gpt-5.3-codex-spark` | `openai-codex` | **$1.75** | **$14.00** | **$0.175** | **$0.00** | 128,000 | 64,000 |
| `anthropic/claude-fable-5-1` | `anthropic` | **$10.00** | **$50.00** | **$1.00** | **$12.50** | 1,000,000 | 128,000 |

**Key Unit Pricing Takeaways:**
- **Sol is 60% cheaper than Astra across all token categories** ($4/M in vs $10/M; $20/M out vs $50/M; $0.40/M cache read vs $1.00/M).
- **Sol provides a 36.8% larger context window** (372k vs 272k tokens).
- **Opus 5.5 offers 5× cheaper cache reads than Astra** ($0.20/M vs $1.00/M) and an enormous 1,000,000 token context window.
- **Gemini 3.8 Flash carries $0.00 marginal token cost** under the Antigravity Ultra subscription flat rate.

---

## 4. Empirical Findings Table: Measured Tokens & Cost Per Task

### Table A: 14-Day Production Dataset (2026-09-08 to 2026-09-22)
*Measured directly from `stats.db` across all sessions and tasks:*

| Model | Completed Tasks (Sessions) | Total Turns | Avg Turns / Task | Avg Input Tokens / Task | Avg Output Tokens / Task | Avg Cache Read / Task | Avg Total Tokens / Task | Calculated Catalog Cost / Task | Total Catalog Spend |
|---|---|---|---|---|---|---|---|---|---|
| **`gpt-6-astra`** | 413 | 43,962 | 106.4 | 333,914 | 22,634 | 11,406,874 | **11,763,422** | **$15.88** | **$6,557.49** |
| **`gpt-5.6-sol`** | 48 | 6,418 | 133.7 | 497,365 | 28,614 | 15,571,995 | **16,097,974** | **$8.79** | **$421.95** |
| **`claude-opus-5`** *(historical baseline)* | 383 | 45,273 | 118.2 | 236 | 66,164 | 15,938,140 | **16,267,410** | **$11.27** | **$4,315.37** |
| **`claude-opus-5-5`** *(projected on Opus profile)* | *[Inferred]* | *[Inferred]* | 118.2 | 236 | 66,164 | 15,938,140 | 16,267,410 | **$4.51** | *[Inferred]* |
| **`claude-opus-5-5`** *(projected on Astra profile)* | *[Inferred]* | *[Inferred]* | 106.4 | 333,914 | 22,634 | 11,406,874 | 11,763,422 | **$4.07** | *[Inferred]* |
| **`gemini-3.8-flash`** | 1,085 | 152,551 | 140.6 | 1,421,215 | 63,299 | 15,647,217 | **17,131,731** | **$0.00** *(flat-rate)* | **$0.00** |
| **`gpt-5.3-codex-spark`** | 11 | 549 | 49.9 | 130,296 | 61,903 | 3,540,899 | **3,733,098** | **$1.71** | **$18.86** |

---

### Table B: All-Time Comparative Dataset (Astra vs Sol)
*Measured across the entire history stored in `stats.db`:*

| Metric | `gpt-6-astra` (All-Time) | `gpt-5.6-sol` (All-Time) | Comparison (Sol vs Astra) |
|---|---|---|---|
| **Total Recorded Tasks (Sessions)** | 414 | 738 | Large empirical sample |
| **Total Message Turns** | 47,061 | 71,814 | Measured in DB |
| **Average Turns per Task** | 113.7 turns | 97.3 turns | **Sol takes 14.4% fewer turns** |
| **Average Input Tokens per Task** | 369,829 | 482,884 | +30.6% input on Sol |
| **Average Output Tokens per Task** | 25,591 | 22,491 | **Sol generates 12.1% fewer output tokens** |
| **Average Cache Read Tokens per Task** | 12,390,238 | 10,767,591 | **Sol reads 13.1% fewer cache tokens** |
| **Average Total Tokens per Task** | 12,785,657 | 11,272,965 | **Sol burns 11.8% fewer total tokens** |
| **Average Cost per Task** | **$17.37** | **$6.69** | **Sol is 61.5% CHEAPER per task ($10.68 savings/task)** |
| **Total Computed Catalog Spend** | **$7,190.38** | **$4,936.03** | Astra burned 45% more $ in 44% fewer tasks |

---

## 5. Token & Cost Dynamics Analysis

1. **Why Sol is 55%–61% Cheaper Per Task:**
   - On full multi-turn tasks, cache-read tokens account for 95%+ of token volume. At $0.40/M (Sol) vs $1.00/M (Astra), cache reads alone cut $6.80 to $7.40 off every task.
   - Generation/output is billed at $20/M on Sol vs $50/M on Astra. Even when Sol generates comparable output (~22k–28k tokens), Sol costs ~$0.45–$0.57 vs Astra’s ~$1.13–$1.28.
2. **Context Window Headroom:**
   - Astra’s 272k limit causes earlier truncation and context compaction turnover compared to Sol’s 372k window.
3. **The Trap of Multi-Turn Implementation on Astra:**
   - In open-ended implementation loops (100+ turns), Astra rapidly drains the rolling 5-hour and cumulative 7-day Codex allowance. A single task burning ~12M tokens on Astra consumes disproportionate subscription compute.
4. **Opus 5.5 Superiority for Hard Tasks:**
   - With $0.20/M cache read rates (2× cheaper than Sol, 5× cheaper than Astra) and a 1M context window, Opus 5.5 costs only **~$4.07 to $4.51 per task**, while providing top-tier architectural reasoning for auth, migrations, and concurrency.
5. **Astra Review Advantage:**
   - Code review PR passes are bounded (typically 5–15 turns, ~1.5M cache read tokens), resulting in review costs of **~$1.50–$3.00 per review**. Reserving Astra exclusively for review leverages its adversarial critical reasoning without incurring runaway 100-turn loop costs.

---

## 6. Prior Art & External Citations

This investigation builds upon and validates prior internal research and external benchmarks:

1. **Internal Research (2026-09-10):**
   - *Report:* `local://astra-xhigh-token-cost-research.md` (committed during PolySimulator incident investigation).
   - *Key Finding:* "Astra xhigh burns fewer tokens than medium" is strictly conditional and cost-negative in API billing due to the 50:1 price asymmetry of output/reasoning tokens ($50/M) vs cached input ($1/M).
2. **[Artificial Analysis (2026-09-09) - Benchmarking GPT-6 Astra](https://artificialanalysis.ai/articles/benchmarking-gpt-6-astra):**
   - Benchmarked `gpt-6-astra` across reasoning effort tiers (`low` through `max`).
   - Found cost per task scales monotonically from $0.82 (low) to $3.26 (max) on standardized benchmarks. While high reasoning reduced turn count on complex synthetic benchmarks (24 turns vs 45 turns on Sol), the output token explosion (27k reasoning tokens on max) overwhelmed turn savings.
3. **[P. Espitia (2026-07-03) - Effort Levels in Practice](https://dev.to/pavelespitia/effort-levels-in-practice-i-benchmarked-low-through-max-on-real-tasks-7lf):**
   - Evaluated agentic reasoning loops; showed that reasoning effort multipliers only decrease net token counts in rare ambiguous loops where turn count drops by >60%. In ordinary multi-file tasks, higher effort increases net token burn.
4. **[PointFive Research (arXiv:2607.12161v5)](https://arxiv.org/html/2607.12161v5):**
   - Analyzed 5,493 agentic coding runs across enterprise suites. Demonstrated that cache read accounts for ~87% of context traffic, and that trading cache read turns for $50/M output tokens reliably inflates total dollar costs.
5. **[OpenAI Codex Issue #32250](https://github.com/openai/codex/issues/32250):**
   - Documented user telemetry where high/ultra reasoning effort on Astra exhausted weekly quotas in under 20% of standard session lifespans.
6. **[Codex Usage Guide (2026-09)](https://www.codexusage.dev/limits/astra):**
   - Documented dynamic compute metering across rolling 5-hour and 7-day weekly windows.

---

## 7. Recommended Task Routing Matrix

Reflecting the **2026-09-22 Operator Ruling**:

| Task Type | Recommended Role | Model & Effort Selector | Expected Cost / Task | Rationale & Guardrails |
|---|---|---|---|---|
| **Easy / Routine Implementation** | `task` | `google-antigravity/gemini-3.8-flash:high` | **$0.00** *(flat-rate)* | Fast execution, 1.05M ctx, zero incremental API cost. Default for all ordinary features and bugfixes. |
| **Routine Scenario QA & Viewports** | `qa-verifier` | `google-antigravity/gemini-3.8-flash:high` | **$0.00** *(flat-rate)* | Browser proof, dual-viewport rendering, contract checks do not benefit from expensive reasoning tokens. |
| **Small-Diff Reviews & Formatting** | `spark` / `codex-reviewer` | `openai-codex/gpt-5.3-codex-spark:medium` | **~$0.50 – $1.70** | Consumes free Spark weekly allowance; ideal for trivial diffs, linting, and formatting. |
| **Independent Exact-Head Review** | `reviewer` | `openai-codex/gpt-6-astra:medium` | **~$1.50 – $3.00** *(review turns)* | **Astra reserved for reviews only.** Bounded turn count eliminates runaway spend while providing deep adversarial bug detection. |
| **Hard Tasks (Auth, Concurrency, Money Paths, Migrations)** | `opus` / `codex-worker` | `anthropic/claude-opus-5-5:high` | **~$4.07 – $4.51** | **Opus 5.5 for hard tasks.** 1M context window, $0.20/M cache read, exceptional deep code reasoning. Two-strike escalation target. |
| **Analytical & Modeling Work** | `sol` *(proposed)* | `openai-codex/gpt-5.6-sol:high` | **~$6.69 – $8.79** | 61% cheaper than Astra, 372k context; ideal alternative for non-review tasks when Claude allowance is guarded. |

---

## 8. Proposed `config.yml` Snippet (Read-Only Proposal)

> **NOTICE:** In strict adherence to user instructions and safety constraints, this is a **read-only proposal**. No live configuration files have been edited.

To implement the operator ruling in `C:/Users/wkiri/.veyyon/profiles/default/agent/config.yml`:

```yaml
# =============================================================================
# PROPOSED SUBAGENT ROUTING (Operator Ruling 2026-09-22)
# - Astra reserved strictly for reviews
# - Opus 5.5 for hard tasks (auth, concurrency, money paths, migrations)
# - Gemini 3.8 Flash for easy/routine tasks
# - Sol 5.6 for analytical modeling / low-cost Codex reasoning
# - Spark for small-diff reviews & fast checks
# =============================================================================

modelRoles:
  # Interactive orchestrator
  default: anthropic/claude-fable-5-1:medium

  # Fast/cheap routine work: Gemini 3.8 Flash
  smol: google-antigravity/gemini-3.1-flash-lite,google-antigravity/gemini-3.8-flash:high

  # Hard reasoning / analysis: Opus 5.5 (or Sol as secondary)
  slow: anthropic/claude-opus-5-5:high,openai-codex/gpt-5.6-sol:high

  # Vision & Plan
  vision: google-antigravity/gemini-3.8-flash:high
  plan: google-antigravity/gemini-3.8-flash:high
  designer: google-antigravity/gemini-3.8-flash:high

  # Free ultra-fast allowance: Spark
  spark: openai-codex/gpt-5.3-codex-spark:medium

agent:
  delegation: required
  maxConcurrency: 10
  agents:
    # Easy/Routine Tasks: Gemini 3.8 Flash (High Effort)
    task:
      enabled: true
      model: google-antigravity/gemini-3.8-flash:high

    # Verification & QA: Gemini 3.8 Flash (High Effort)
    qa-verifier:
      enabled: true
      model: google-antigravity/gemini-3.8-flash:high

    # Exact-Head Reviews ONLY: Astra Medium
    # (Astra is restricted to review passes; no multi-turn implementation loops)
    reviewer:
      enabled: true
      model: openai-codex/gpt-6-astra:medium
      thinkingLevel: medium

    # Hard Tasks: Claude Opus 5.5 High
    # (Auth, Concurrency, Money Paths, Alembic Migrations, Two-Strike Escalations)
    opus:
      enabled: true
      model: anthropic/claude-opus-5-5:high
      thinkingLevel: high

    codex-worker:
      enabled: true
      model: anthropic/claude-opus-5-5:high
      thinkingLevel: high

    # Proposed Sol Subagent Role: GPT-5.6 Sol High
    # (60% cheaper than Astra; 372k context; heavy analytical/simulation tasks)
    sol:
      enabled: true
      model: openai-codex/gpt-5.6-sol:high
      thinkingLevel: high

    # Small-Diff Reviews & Quick Second Opinion: Spark Medium
    codex-reviewer:
      enabled: true
      model: openai-codex/gpt-5.3-codex-spark:medium
      thinkingLevel: medium

    spark:
      enabled: true
      model: openai-codex/gpt-5.3-codex-spark:medium
      thinkingLevel: medium

    # Read-only Reasoning & Analytical Spikes
    thinker:
      enabled: true
      model: openai-codex/gpt-5.6-sol:high
      thinkingLevel: high

defaultEffort:
  google-antigravity/gemini-3.8-flash: high
  openai-codex/gpt-6-astra: medium
  openai-codex/gpt-5.6-sol: high
  openai-codex/gpt-5.3-codex-spark: medium
  anthropic/claude-opus-5-5: high
  anthropic/claude-fable-5-1: medium
```

---

## 9. Explicit Distinction: Measured vs. Inferred Data

To maintain audit integrity per repository policy:

| Item | Status | Source / Derivation |
|---|---|---|
| **Astra 14d & All-Time Turns, Tokens, Sessions** | **MEASURED** | SQLite `stats.db` queries on 47,061 Astra messages |
| **Sol 14d & All-Time Turns, Tokens, Sessions** | **MEASURED** | SQLite `stats.db` queries on 71,814 Sol messages |
| **Flash 3.8 & Opus 5 14d Turns & Tokens** | **MEASURED** | SQLite `stats.db` queries across 152k Flash & 45k Opus turns |
| **Catalog Unit Pricing & Context Windows** | **MEASURED** | `veyyon models --json --no-extensions` |
| **Astra & Sol Historical Catalog Cost ($)** | **CALCULATED** | Measured token types multiplied by authoritative catalog unit rates |
| **Opus 5.5 Cost Per Task ($4.07 – $4.51)** | **INFERRED** | Catalog rates ($4/$20/$0.20) projected onto measured Opus 5 & Astra turn/token profiles |
| **Astra Review-Only Cost ($1.50 – $3.00)** | **INFERRED** | Bounded review workflow model (5–15 turns, ~1.5M cache reads) derived from PR review audit data |

