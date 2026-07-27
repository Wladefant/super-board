# Super Board documentation

## Architecture

- [Agent Native production layer for Superboard and Claudex](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-SUPERBOARD-PRODUCTION.md) — decision, ownership boundaries, secure VPS topology, evidence contract, and constrained real-project pilot for a reusable web/SaaS coding cockpit.
- [Agent Native operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md) — the short operator version: capability→surface routing table, what must never move into Agent Native, the standing no-code-execution constraint for the public cockpit, and the deployed app URLs.

## Reference

- [Superboard GraphQL IDs](https://github.com/Wladefant/super-board/blob/main/docs/reference/BOARD-IDS.md) — single lookup table of every board's project node ID, Status field ID and seven Status option IDs, plus repo node IDs, why Backlog/Ready/Building share IDs across boards, and the two mutation traps (numeric-looking option IDs, and `updateProjectV2Field` replacing the whole option set).

## Runbooks

- [Deploying a new app on Dokploy](https://github.com/Wladefant/super-board/blob/main/docs/runbooks/DOKPLOY-NEW-APP.md) — the deploy leg of the new-project bootstrap: pick the GitHub provider by repo owner (the personal-vs-Bavariance trap that fails silently on private repos), Cloudflare wildcard domains, the no-HEALTHCHECK constraint, and the build-type MCP quirk.

## Skills

- [`superboard-setup`](https://github.com/Wladefant/super-board/blob/main/skills/superboard-setup/SKILL.md) — provisions a new project across the whole stack: board, columns, workflows, labels, milestones, payload and config, then project skills, then the deploy path. Start here for a new project.
- [`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md) — the design method: Agent Native Design first, dark and light, real references, no production edit before approval.
- [`polysim-design`](https://github.com/Wladefant/super-board/blob/main/skills/polysim-design/SKILL.md) — worked example of a per-project design skill (real tokens, brand and surfaces harvested from the PolySimulator repo).
- [`claudex-optimized`](https://github.com/Wladefant/super-board/blob/main/skills/claudex-optimized/SKILL.md) — model routing, context preflight and recovery.

## System diagnostics

- [Intermittent DNS failures: prior-session distinction and confirmed MSI Center UDP leak](https://github.com/Wladefant/super-board/blob/main/docs/_session/dns-resolver-recurrence-2026-07/PRIOR-SESSION-FINDINGS.md) — separates the earlier orphaned Claude-process cleanup from the confirmed WinSock 10055 incident, records the MSI Central Server endpoint leak, and provides a narrow recurrence runbook.

## Claudex and Antigravity research

- [Antigravity risk, model inventory, and Claude Code routing](https://github.com/Wladefant/super-board/blob/main/docs/_session/claudex-antigravity-research/ANTIGRAVITY-RISK-MODELS-AND-ROUTING.md) — policy boundary, model inventory, gateway discovery, subagent routing, Codex context limits, and quota-first recommendations.
- [Local session and failure analysis](https://github.com/Wladefant/super-board/blob/main/docs/_session/claudex-antigravity-research/LOCAL-SESSION-AND-FAILURE-ANALYSIS.md) — metadata-only incident analysis separating the Grok authentication failure from the Sol subagent context failure.
- [Claudex optimized skill proposal](https://github.com/Wladefant/super-board/blob/main/docs/_session/claudex-antigravity-research/CLAUDEX-OPTIMIZED-SKILL-PROPOSAL.md) — implementation-ready design for a safe, quota-first future Claude Code skill with context preflight, minimal tools, empirical routing tests, and rollback.
