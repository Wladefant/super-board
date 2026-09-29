# Consolidated Index: Governance, Multi-Board Integration & Agent Benchmarks (Batches 24 & 25)

**Date:** 2026-09-29  
**Branch:** `docs/batch-24-25-governance-research`  
**Author:** Wladimir Kirjanovs <wladefant@gmail.com>  

---

## Deliverables Summary

This delivery resolves the Superboard governance, multi-board architecture integration, and model routing benchmark deliverables spanning **Batch 24 (`lane-docs-superboard-governance`)** and **Batch 25 (`lane-docs-agent-benchmarks-routing`)** of `local://issue-triage.md`.

| Issue | Title | Deliverable Document | Scope & Focus | Status |
|---|---|---|---|:---:|
| [#255](https://github.com/Wladefant/super-board/issues/255) | audit(workflow): investigate instruction source routing routine lanes to Opus and purge stale prompt references | `docs/workflow/instruction-source-routing-audit.md` | Investigation of historical session `01a0a6a7` (73 Opus spawns), purge of stale `ag-opus` prompt references, strict reservation of Opus 5.5 for high-risk work | **Delivered** |
| [#49](https://github.com/Wladefant/super-board/issues/49) | Complete PolySimulator Superboard enrollment and system integration | `docs/superboard/polysimulator-integration.md` | Multi-board partitioning (`Bavariance/projects/1` upgrade, `Wladefant/projects/10` retirement), 588 cards classified, 7 safety proofs, sub-issue #2396 status | **Delivered** |
| [#211](https://github.com/Wladefant/super-board/issues/211) | Research: agent benchmark comparison 2026-09 incl. DeepSeek API worker tier | `docs/research/agent-benchmarks-2026-09.md` | AA Terminal-Bench 4.0 comparative analysis, DeepSeek V4.1 Flash worker tier evaluation, cost per solved task metrics, privacy/guardrail policy analysis | **Delivered** |
| [#203](https://github.com/Wladefant/super-board/issues/203) | Research & Recommendation: Effort Level Calibration for Orchestrator and Opus Subagents | `docs/research/effort-level-calibration.md` | Empirical analysis of 657 Opus subagents and 16k+ Main turns; recommendation to reduce Opus implementation effort to `medium` while keeping Reviewer at `high` | **Delivered** |
| [#192](https://github.com/Wladefant/super-board/issues/192) | Routing research: Astra vs Sol subagent token cost | `docs/research/subagent-routing-token-cost.md` | Telemetry analysis from `stats.db` (689k messages); proof that Sol is 61.5% cheaper per task; operator ruling implementation matrix | **Delivered** |

---

## Cross-Repository Coordination

Complementary PolySimulator governance, staging ancestry rules, and Level 3 agent readiness assessments are delivered concurrently in `Bavariance/polysimulator`:
- [Bavariance/polysimulator#5810](https://github.com/Bavariance/polysimulator/issues/5810): `docs/features/pro-tracker.md`
- [Bavariance/polysimulator#3261](https://github.com/Bavariance/polysimulator/issues/3261): `docs/workflow/completion-claims-staging-vs-branch.md`
- [Bavariance/polysimulator#2433](https://github.com/Bavariance/polysimulator/issues/2433): `docs/superboard/workflow-status.md`
- [Bavariance/polysimulator#3059](https://github.com/Bavariance/polysimulator/issues/3059): `docs/audit/code-quality-skills.md`
- [Bavariance/polysimulator#2607](https://github.com/Bavariance/polysimulator/issues/2607): `docs/readiness/level-3-assessment.md`
