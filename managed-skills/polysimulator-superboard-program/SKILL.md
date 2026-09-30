---
name: polysimulator-superboard-program
description: "Operate PolySimulator through Superboard, parallel exact-head gates, live Dokploy/Supabase diagnostics, safe staging promotion, and isolated feature branches."
---

# PolySimulator Superboard Program

Use this skill when operating the long-running PolySimulator engineering program, staging promotion, production incident triage, or Kalshi/Backtesting workstreams.

## Source of truth

1. Resume from GitHub Project #1 (Superboard), GitHub issues, PRs, branches, review threads, CI, deployed revisions, Dokploy logs, and Supabase diagnostics—not chat memory.
2. Before implementation, reuse or create one authoritative issue and Project card with acceptance criteria, dependencies, branch/worktree, current SHA, owner, tests, PR, and blocker.
3. Keep card states accurate: Backlog, Ready, Building, QA, Review, Blocked, Done.
4. Post updates only when state, evidence, authoritative head, or blocker changes.

## Agent routing

Use the enabled role-aligned roster: `orchestrator`, `fast`, `thinker`, `vision`, `architect`, `ui-designer`, `commit`, `tiny`, and `advisor`.

- For a broad backlog, run 10–15 genuinely independent slices concurrently under one orchestrator when RAM policy permits; slices need not converge if ownership and contracts are isolated.
- Use `fast` for mapping, routine implementation, focused tests, and project operations.
- Use `thinker` for concurrency, settlement, money paths, difficult cross-file fixes, and infrastructure.
- Use `architect` for consequential design and dependency planning.
- Use `advisor` for independent adversarial exact-head merge gating.
- Use `ui-designer` and `vision` for user-facing and visual verification.
- Do not assume a fixed provider mix. Admit agents only when independent work exists and current RAM policy permits.
- Cancel or redirect stale review work whenever the authoritative reviewed SHA changes.

## Branch and worktree contract

- `staging` remains the normal PolySimulator integration branch. Never push directly to `staging` or `main`.
- `kalshi` and `backtesting` are long-lived, always-current feature integration branches. Their feature work targets those branches, never `staging` or `main`.
- Create every change in an isolated worktree and issue-scoped feature branch.
- Synchronize the final pre-revert `staging` tip into each feature branch before additive staging extraction. Never rewrite published `staging` history.
- Remove already-landed feature work from staging through separate reviewed additive revert PRs. Merge the revert-bearing staging history into each feature branch, then revert the relevant staging revert there once so future staging merges do not replay removal.
- Never merge Kalshi or Backtesting into `staging` or `main` without explicit operator authorization and completed architecture, data, security, and UI gates.

## Database isolation

- Never run Kalshi or Backtesting development, migrations, backfills, or destructive tests against shared Supabase project `<prod-supabase-ref>`.
- Provision distinct PostgreSQL services, databases, roles, credentials, networks, volumes, backups, and connection URLs for each feature. Keep production unreachable unless explicitly required.
- Inject isolated URLs only into their feature deployments and jobs. Never replace staging or production database variables during feature setup.
- Historical backfills require read-only preflight, backup, dry-run, canary, idempotent checkpoints, bounded batches, row-count/invariant proof, resume, and rollback.
- Verify isolation with sanitized host/database identity. Never print credentials.

## Dokploy and live diagnostics

- Dokploy's control plane is `https://hosting.wladefant.de`. Never access Hetzner.
- PolySimulator staging runs on Akamai/Linode IAD and may be inspected through the Hostinger Dokploy API. Read-only Akamai staging log access is required during incident diagnosis.
- Staging compose is `TU7b_dY9l9_nCas6YBNwj`; production compose `vpyL-7TDEUREH6Uo_y1sb` is never a staging target. Confirm resource identity before every operation.
- A merge to `staging` does not deploy. Follow the current staging runbook and manual deployment path only with operator authorization.
- Before diagnosing a live problem, query an explicit UTC window of relevant Dokploy deployment/container logs and Supabase unified logs/advisors. Report concrete counts, first/last timestamps, routes/error classes, health, and limits without PII.
- Dokploy mutations require explicit live operator authorization. A success/queued response is not deployment proof.
- Treat `deployments[]` as incomplete and unsorted: sort by `createdAt`, inspect actual container/image creation state, verify cache-busted frontend/backend revision, prove commit ancestry, and run behavioral UI/API probes.
- Keep credentials only in owner-only environment/vault facilities. Never echo them, put literal keys in MCP config, commit them, or infer availability from a configured variable name.

## Per-issue execution

1. Search issues, PRs, commits, internal docs, live logs, and advisors for prior art and current evidence.
2. Map production topology and every affected caller before editing.
3. Define adversarial tests for plausible failures before or alongside implementation.
4. Implement at the source using existing conventions; remove obsolete parallel paths.
5. Verify actual behavior with focused tests and a smoke scenario.
6. Push a fresh feature head and open/update the correctly based PR.
7. Independently resolve the authoritative GitHub PR head. Require a resolvable full 40-character SHA; never trust an agent-reported or PR-body SHA by itself.
8. Record exact SHA and evidence on the issue, PR, and Project card.
9. Run independent review against that exact SHA; address every actionable finding in fresh commits.
10. Rebase-merge only after all gates pass. Never squash. Never merge to `main` without explicit operator authorization.
11. After every staging merge, wait for the real manual deployment and attach strong live UI/API plus relevant log evidence before closing the issue/card.

## Repeat-failure learning

- When the same workflow, evidence, worktree, credential, or deployment failure occurs twice, stop repeating it.
- Record the root cause and a concrete guardrail in AGENTS.md, this skill, a runbook, or durable memory as appropriate.
- Re-isolate or reassign the lane, change the workflow, and verify the new guardrail. Do not accept a third unverified claim.
- Common guardrails: independently resolve PR heads, never mix worktrees, never trust Dokploy queue acknowledgements, and distinguish configured credential names from working authentication.

## Feature completion gates

Do not call a feature ready from isolated engine tests. Require end-to-end evidence for official discovery/pagination/authentication/lifecycle; normalized interfaces; pricing/order books; placement/cancellation/partial fills/fees/retries/idempotency; wallet/portfolio/ledger/reservations; settlement/restart recovery; API/WebSocket delivery; UI/accessibility/mobile; secret/rate-limit/failure isolation; and historical/live parity on the isolated database.

## Evidence standard

Every completion claim names the exact branch and authoritative full SHA, commands or live probes run, observed output, UTC log window, unresolved blockers, and whether evidence came from implementation, independent review, CI, deployed UI/API, Dokploy logs, or Supabase diagnostics.
