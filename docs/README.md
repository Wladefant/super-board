# Super Board documentation

## Status

- [Agent Native is PARKED (2026-08-02)](https://github.com/Wladefant/super-board/blob/main/docs/runbooks/AGENT-NATIVE-PARKED.md) — the self-hosted stack was stopped, not deleted; what was done, exact IDs to restart it, and what to fix first.

## Start here

- [START HERE — what actually works right now](https://github.com/Wladefant/super-board/blob/main/docs/START-HERE.md) — **the front door.** What is live and verified by a real HTTP response, what is deployed but unproven, what is still pending, and the five open decisions with their tradeoffs — plus the shortest path to starting work on any product. Status is timestamped; re-check commands included.

## Release and runtime contracts

- [Version reconciliation](https://github.com/Wladefant/super-board/blob/main/docs/version-reconciliation.md) — what each of the four disagreeing version sources said, which one is the current release and why, why the skill mirror lagged, why v1.3.0–v1.7.1 are explicitly untagged rather than retro-tagged, and the written rule that produced the next number.
- [Release notes](https://github.com/Wladefant/super-board/blob/main/RELEASE-NOTES.md) — every release, newest first. The newest section is the active contract; older sections are history and are not rewritten.
- [Agent Native deployed evidence contract](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-DEPLOYED-EVIDENCE.md) — what a deployed cockpit must record before a board is activated against it: the code-execution setting, all seven negative capabilities, and the two synthetic non-resolving probe targets.

### Runtime reference (installed alongside the `super-board` skill)

- [`references/onboard.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/onboard.md) — the setup wizard, and the re-enable gate for the fallback auto-add workflow.
- [`references/run.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/run.md) — lane lifecycles, identity preflight, and the halt gates.
- [`references/run-workflow.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/run-workflow.md) — the opt-in in-session workflow backend. `claude-p` is the default.
- [`references/status.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/status.md) — the status renderer, including tested SHA and QA invalidation.
- [`references/lint.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/lint.md) — pre-flight readiness checks on active-pipeline issues.
- [`references/stop.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/stop.md) — graceful shutdown and resume.
- [`references/block-template.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/block-template.md) — the Blocked-card template.
- [`references/agent-native.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/agent-native.md) — the read-only projection contract for the cockpit.
- [`references/rate-limit-etiquette.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/rate-limit-etiquette.md) — GraphQL cost discipline behind the immutable reserve.
- [`references/config-schema.json`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/config-schema.json) — the configuration contract, documented for humans. `scripts/super_board_runtime/config.py` is the executable version and wins on disagreement.

## Architecture

- [Agent Native production layer for Superboard and Claudex](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-SUPERBOARD-PRODUCTION.md) — decision, ownership boundaries, secure VPS topology, evidence contract, and constrained real-project pilot for a reusable web/SaaS coding cockpit.
- [Agent Native operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md) — the short operator version: capability→surface routing table, what must never move into Agent Native, the standing no-code-execution constraint for the public cockpit, and the deployed app URLs.

## Reference

- [Missing upstream dependencies in the pipeline skills](https://github.com/Wladefant/super-board/blob/main/docs/reference/MISSING-UPSTREAM-DEPENDENCIES.md) — **read before running `/super-qa` or trusting a spec citation.** The scripts, design spec, slash commands and skill owners the inherited pipeline skills reference but that were never carried across the fork, why they are deliberately not being reconstructed, and how each skill now halts instead of failing midway.
- [Superboard GraphQL IDs](https://github.com/Wladefant/super-board/blob/main/docs/reference/BOARD-IDS.md) — single lookup table of every board's project node ID, Status field ID and seven Status option IDs, plus repo node IDs, why Backlog/Ready/Building share IDs across boards, and the two mutation traps (numeric-looking option IDs, and `updateProjectV2Field` replacing the whole option set).

## Runbooks

- [Deploying a new app on Dokploy](https://github.com/Wladefant/super-board/blob/main/docs/runbooks/DOKPLOY-NEW-APP.md) — the deploy leg of the new-project bootstrap: pick the GitHub provider by repo owner (the personal-vs-Bavariance trap that fails silently on private repos), Cloudflare wildcard domains, the no-HEALTHCHECK constraint, and the build-type MCP quirk.

## Skills

- [`superboard-setup`](https://github.com/Wladefant/super-board/blob/main/skills/superboard-setup/SKILL.md) — provisions a new project across the whole stack: board, columns, workflows, labels, milestones, payload and config, then project skills, then the deploy path. Start here for a new project.
- [`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md) — the design method: Agent Native Design first, dark and light, real references, no production edit before approval.
- [`claudex-optimized`](https://github.com/Wladefant/super-board/blob/main/skills/claudex-optimized/SKILL.md) — model routing, context preflight and recovery.

### Per-project design skills

Each supplies the real tokens, brand, surfaces and house-ban conflicts for one product,
and is consumed by `design-prototyping`. Generated by
[`new-project-design-skill.sh`](https://github.com/Wladefant/super-board/blob/main/skills/superboard-setup/scripts/new-project-design-skill.sh).

- [`polysim-design`](https://github.com/Wladefant/super-board/blob/main/skills/polysim-design/SKILL.md) — PolySimulator, harvested from `Bavariance/polysimulator`. Board [10](https://github.com/users/Wladefant/projects/10).
- [`shipnovo-design`](https://github.com/Wladefant/super-board/blob/main/skills/shipnovo-design/SKILL.md) — Shipnovo, harvested from `Wladefant/shipnovo`. Board [11](https://github.com/users/Wladefant/projects/11).
- [`elumiai-design`](https://github.com/Wladefant/super-board/blob/main/skills/elumiai-design/SKILL.md) — Elumi AI website, harvested from `Wladefant/elumiai-website`. Board [12](https://github.com/users/Wladefant/projects/12).
- [`fnsku-design`](https://github.com/Wladefant/super-board/blob/main/skills/fnsku-design/SKILL.md) — FNSKU Warehouse Scanner, harvested from `Wladefant/FNSKUWarehouseScanner`. Board [13](https://github.com/users/Wladefant/projects/13).
- [`heylolo-hq-design`](https://github.com/Wladefant/super-board/blob/main/skills/heylolo-hq-design/SKILL.md) — HeyLolo Business HQ, harvested from `Wladefant/heylolo-hq`. Board [4](https://github.com/users/Wladefant/projects/4) is unconfirmed for HQ cards; verify before filing.

### Pipeline skills (installed into a target project by `install.sh`)

- [`super-board`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/SKILL.md) — the five verbs: onboard, lint, status, run, stop.
- [`super-build`](https://github.com/Wladefant/super-board/blob/main/skills/super-build/SKILL.md) — headless parallel executor for `Ready` cards.
- [`super-qa`](https://github.com/Wladefant/super-board/blob/main/skills/super-qa/SKILL.md) — bug-bash, evidence capture and issue filing.
- [`super-review`](https://github.com/Wladefant/super-board/blob/main/skills/super-review/SKILL.md) — merge-readiness judgment and fix routing.

## System diagnostics

- [Intermittent DNS failures: prior-session distinction and confirmed MSI Center UDP leak](https://github.com/Wladefant/super-board/blob/main/docs/_session/dns-resolver-recurrence-2026-07/PRIOR-SESSION-FINDINGS.md) — separates the earlier orphaned Claude-process cleanup from the confirmed WinSock 10055 incident, records the MSI Central Server endpoint leak, and provides a narrow recurrence runbook.

## Claudex and Antigravity research

- [Antigravity risk, model inventory, and Claude Code routing](https://github.com/Wladefant/super-board/blob/main/docs/_session/claudex-antigravity-research/ANTIGRAVITY-RISK-MODELS-AND-ROUTING.md) — policy boundary, model inventory, gateway discovery, subagent routing, Codex context limits, and quota-first recommendations.
- [Local session and failure analysis](https://github.com/Wladefant/super-board/blob/main/docs/_session/claudex-antigravity-research/LOCAL-SESSION-AND-FAILURE-ANALYSIS.md) — metadata-only incident analysis separating the Grok authentication failure from the Sol subagent context failure.
- [Claudex optimized skill proposal](https://github.com/Wladefant/super-board/blob/main/docs/_session/claudex-antigravity-research/CLAUDEX-OPTIMIZED-SKILL-PROPOSAL.md) — implementation-ready design for a safe, quota-first future Claude Code skill with context preflight, minimal tools, empirical routing tests, and rollback.
