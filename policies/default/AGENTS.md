# Profile Orchestration Policy — Default Profile

Live operator instructions in the active session override this file, profile documentation, and project policies. Applies to all projects under the `default` profile.

## 1. Authority

1. **User Primacy:** live operator instructions supersede standing user, profile, and project documentation.
2. **Loaded Skill Overrides:** Project constraints override loaded-skill clauses: all integrations use merge commits (`--no-ff`), never squash/rebase/rebase-merge; PolySimulator production (`zaraprptkegxqpvnsubu`, `akamai-iad-prod`) is strictly off-limits.

## 2. Orchestration & Routing
(a) **Fetch before dispatch:** fetch, compare HEAD to the default branch, merge the tip forward (`--no-ff`) first if behind, put the base SHA in every lane task (skill `fetch-before-dispatch`). (b) **Sidecar before quota claims:** check the Antigravity sidecar health endpoint (127.0.0.1:45123) and restart it BEFORE reporting Google quota exhausted (skill `antigravity-sidecar-health`). Repeat-failure limit: §13.

- **Orchestrator Role:** Claude Fable 5.1 at **medium**. Main ONLY decomposes, dispatches, replenishes, answers, tracks blockers — never solo stretches, product edits, reports, or tooling; every substantial step is a subagent.
- **Main Protocol (2026-09-08):** (1) decompose, dispatch, track blockers, report milestones; (2) zero solo execution; (3) replenish in one 3-8-lane batch; (4) silence default; (5) `job` polls 120-240 s (<~270 s); (6) lanes summarize large artifacts; (7) 200k context cap; (8) static tool schema; (9) hard lane cap while RAM-starved: 4. Fable bills turns × context — these keep the bill low.
- **Ship, Don't Build:** live staging code is the only progress measure; no ledgers/adapters/coordinators/watchdogs/policy rewrites unless asked for that artifact; a PR passing focused tests and review (or review-exempt) merges same cycle; re-reviewing unchanged heads is waste.
- **Backlog Cadence:** keep 15+ lanes on triaged slices (open PRs first: sync, focused tests, review, merge; then issues), batch 3-5 small PRs per lane, report per milestone not per lane.
- **Bounded Concurrency & Host RAM Hygiene:** the worker-count floors above are bounded by measured host RAM. Measure real RAM before every spawn: at **>=95%** spawn no new workers; at **>=85%** reap finished dev servers and idle processes before adding tasks. Every lane MUST terminate any dev server it launches (`node.exe`, `next dev`, `start-server`) and verify the port is released before yielding.
- **Exclusive Build & Browser Slot (`build_slot.py`):** `next build`, `next start` and dev Chromium are arbitrated by `python C:/Users/wkiri/.veyyon/workflows/build_slot.py acquire <name> [--timeout SEC]` / `release <name>`. Lanes MUST call the script directly and MUST NEVER DM Main for slot access. The lock is atomic (`os.mkdir` at `~/.veyyon/run/build-slot.lock`), Windows-safe (no `fcntl`), FIFO-ordered (`~/.veyyon/run/build-slot.queue.json`), auto-reclaims stale locks (dead PID or age >= 30 min), and refuses acquisition at >=85% host RAM unless `--force`.
- **Model Routing:** ladders/pacing/allowance guards live in `model_routing.py` (changed in PRs, never ad hoc). Check `veyyon usage --json` before every wave, spending soonest-resetting windows first ([#210](https://github.com/Wladefant/super-board/issues/210)). High-risk never falls back to Flash tiers or free models. Routed roles need `config.yml` `modelRoles` entries (`ROLE_MODEL_PINS` is the source).
- **Verify Actual Routing:** Check the live worker roster and role mapping before claiming a model executed; preserve partial work on reassignment.
- **Two-Strike Escalation:** On the same slice, escalate after the second failure (§13); preserve partial work.
- **Parallel Dispatch (2026-09-14, #135):** homogeneous triage fan-out batches are refused. Partition by domain with distinct `# Target`s, externalize inputs to `local://`, single-`task` calls per item, never retry a refused batch shape (managed skill `parallel-lane-dispatch`).
- **Four-Stage Loop (2026-09-26, #228):** Spec (low: boundaries, interfaces, acceptance criteria) → Scaffold (low: skeletons, types, fixtures) → Implement (medium: core logic, all call sites, no dead shims) → Verify (high, 32k+ thinking tokens: adversarial checks, negative controls, real scenarios).

## 3. Operator Communication & Standing Decisions

- **Answer & Follow-Through:** answer all outstanding questions together (last verified deploy, why newer code isn't deployed, failing gates, needed actions); execute authorized merges on green gates; local fixes and queued deploys are never live completion.
- **Persistent Questions (2026-09-06):** track unanswered questions durably (topic, owner, link, reminders); verify the session bot/chat first. Re-ask on a bounded cadence until answered or dropped; silence never means rejection or authorization.
- **Understandable Questions:** plain-language problem, concrete action, consequence, one explicit question with labeled choices, ≤1 details link; no IDs/paths/URL lists. If told unclear: keep unanswered, clarify, never repeat verbatim.
- **Hands-On Steps → Telegram (2026-09-08):** steps needing the operator's hands/eyes get the exact one-line instruction on the verified Telegram route BEFORE/AT the moment needed, then wait for the observable result; send outcomes too; terminal-only prompts count as never delivered.
- **Answer Where the Operator Wrote (2026-09-08):** A message that arrived through Telegram MUST get its answer sent back to Telegram, not only rendered in the terminal. Terminal-only answers count as unanswered.
- **Telegram Formatting (2026-09-08):** full rich surface — HTML mode with escaping, bold headers, bullets, `<code>`/`<pre>`, blockquotes, media groups, inline buttons; no raw markdown artefacts.
- **Every Mention Is a Link (2026-09-08):** every tagged artifact is a clickable link (Markdown on GitHub, `<a>` on Telegram); visual evidence embeds as images. Write `owner/repo#N` or the full URL.
- **Operator UI Preferences Are Standing Decisions:** Design-staging comments and live instructions about rejected UI are settled decisions. Do not reintroduce a rejected element; when touching adjacent UI, check the operator's issue/PR comments for such rulings first.

## 4. Scheduled Execution Hygiene

- **Independent Sections:** each section is an independent deliverable (owner, criteria, branch/PR, next action); todo order is NOT dependency; when one blocks, continue the others and record the blocker.
- **Scope Confinement:** feature/bug PRs stay in-domain; `.env.defaults.*`, `docker-compose.*`, `alembic/`, `scripts/env_sync*` enter a PR only when authorized and strictly coherent; no opportunistic infra bundling.
- **Contract Propagation:** a shared helper/API/schema change is incomplete until every producer, consumer, adapter, test double, and doc surface is enumerated and coordinated with its owner; tests assert independent behavior, not values derived from the helper.
- **Engineering Principles:** simplest thing that meets the requirement; layered growth; modular concerns; established/in-project dependencies over new packages; proven patterns; clean cutover, no dead shims (DDL keeps fwd/back compatibility).

## 5. GitHub-Native Work Contract

**GitHub is the source of truth.** Every deliverable lives in a dedicated issue assigned to an accountable human: one canonical `kind:` label, applicable `area:`/`risk:` labels, an open milestone, a [Project 5](https://github.com/users/Wladefant/projects/5) card ([taxonomy](https://github.com/Wladefant/super-board/issues/113); no parallel labels).

- **Milestones** are capability/integration boundaries; never silently close unresolved issues — move them. Release tags are an open question, not a completion gate. **Discussions are not used.**
- **Native hierarchy:** parents are weightless anchors (no branch, code, or PR); work lives in sub-issues one level deep; standalone tasks state why no parent applies; use native blocking dependencies, never a markdown checklist; every child self-contained.
- **Adopt-or-Reject:** Every recommendation/requirement is adopted (shipped + wired into default use, cite adopted-at) or rejected (recorded rule). Never close a parent with open sub-issues; enforced by close-point guards: no closing keywords on parent PRs, plus a guard in ledger.py, the project adapters and GitHub-ops skills that refuses to close a parent with open sub-issues.
- **Issue contract:** the body carries (1) Scope, (2) Acceptance Criteria, (3) Dependencies & Parent, (4) Owner, (5) State & Blockers, (6) Branch/PR/Exact Head, (7) Evidence, (8) Next Action, (9) Authorization; never infer permission from silence. Evidence in comments; handoffs return URLs; unresolved work survives compaction.
- **Cache, never authority:** `ledger.py` is an execution cache, not truth; remote status never fabricates proof. API data must be current — missing/partial blocks for reconciliation, no stale fallback. The issue alone must suffice; unpublished intake is published first; unresolved records stay visible as blocked; `goal` has no update op.
- **Platform constraints:** PolySimulator's default branch is `main`, but work targets `staging`; closing keywords are inert for staging PRs — explicit links and evidence-gated closure required. No native merge queues; Project 5 plus the deterministic gate are the visible queue; merge-commit-only stays.

## 6. Review & Merge Integrity

- **Risk-Based Review ([#195](https://github.com/Wladefant/super-board/issues/195)):** review REQUIRED iff high-risk domain (`risk:high`, money/billing/auth/concurrency/migration labels or paths) or >250 changed lines excl. lockfiles/generated; else EXEMPT on green CI (+ browser QA for UI).
- **Review Discipline:** no review on red/pending CI. Content identity (patch-id + diff sha256) survives sync merges and evidence pushes; real content changes get a delta review only (`delta-from: <sha>`), if still review-required. A sync lane's approval is self-approval, never counts.
- **Content identity mechanics** (patch-id computation, delta chains, `reviewed-sha:` resolution) live in the PolySimulator repo `AGENTS.md` §4; reuse, never restate.
- **CI Hygiene:** ≤5 min wait, then proceed on local gates. Deploy-critical checks (`DEPLOY_CRITICAL_CHECKS` in `github_pr_gate.py`) never time out — a merged broken build takes staging down ([#5535](https://github.com/Bavariance/polysimulator/issues/5535)); FAILED blocks integration. `cancel-in-progress` groups mandatory.
- **Gate Integrity:** for PolySimulator `staging` and `super-board@main` the approval click is **waived** — single-author identity makes it unobtainable; review-exempt PRs pass on CI + QA, review-required still need independent automated review bound to content identity. An author review or `COMMENTED` **never** counts; under the waiver it lands only with an explicit approval verdict matching content identity in a valid delta chain. Elsewhere: strict non-author `APPROVED`.
- **Production Exclusion:** production off-limits (`zaraprptkegxqpvnsubu`, `akamai-iad-prod`); the approval-click waiver covers only PolySimulator staging and `Wladefant/super-board@main`; authorized workflow PR merges (#74, #4545) still need gates satisfied.
- **Merge Queue:** end-state is merged+verified, ready on green gates, or explicitly blocked (failing gate, owner, next step); a local commit/push/QA pass never counts as completion. Keep the queue on issue + Project 5; execute authorized merges on green gates.
- **MergeSequencer:** a dedicated lane serializes backend/infra merges (`backend/`, `docker-compose*`, `deploy/`) ≥10 min apart; workers send `ready to merge <PR> head <sha>` over IRC and never merge; MergeSequencer commits, attests, hands back for live verification.
- **Small Frontend PRs:** <250 lines, backend/ops-free may self-merge once local gates pass and CI is clean.

## 7. Persistent Signed-In QA & Durable Defect Verification

- **Defect Intake:** before dispatch, record intake (original prompt, repro steps, acceptance criteria); bugs never drop until QA confirms absent.
- **Defect Closure:** resolution requires re-running the exact original failure scenario, regression paths, and documenting proof on head SHA + live environment; generic suite passes and "related fix" commits are NOT proof.
- **Recurrence:** reopen immediately; an unreproducible bug stays `needs-reproduction` unless the operator directs otherwise; no unprovable claims of universal absence.
- **Context-Appropriate Verification:** frontend via real browser; backend/daemon/migration/CLI via faithful tests; native Windows GUI via native input. Docker is optional tooling — never re-ask; scope missing-environment blockers to the exact capability (real pooler, never wire mocks).
- **Browser & Staging QA:** user-facing changes verify via `browser` in Chromium against a responsive, authenticated backend; staging QA stays signed-in and exercises real flows (orders, cancels, balances).
- **Viewports:** frontend evidence covers desktop (1440px) and mobile (320px/390px); mobile tweaks never harm desktop legibility.
- **SSR Hydration:** SSR components hydrate with no focus leakage (closed drawers carry `inert`); inspect streaming feeds during active updates.
- **Deceptive Mocks banned:** no fake-DOM unit tests; async deps get faithful awaitables (`AsyncMock`, faithful fakes) that propagate exceptions, never synchronous mocks that swallow `await`.

## 8. GitHub Reporting & Visual Assets

- **GitHub-Only Reporting:** `local://` banned for reports/audits/evidence — publish on the dedicated issue/PR and link it (long docs blob-linked with full 40-hex SHA); Telegram carries concise linked summaries, never raw reports. Visual plan standards: `IntegrateVisualPlanWorkflow`.
- **Evidence Upload:** visuals via `gh image`, Release assets, or commit-pinned `github.com/<owner>/<repo>/raw/<40-hex>/<path>`; prohibited: `raw.githubusercontent.com`, relative paths, unpushed branches. After posting, reload and confirm each image renders.

## 9. Superboard & Issue Tracking

Every deliverable links its own GitHub issue and Superboard card (criteria, exact head SHA, evidence; reuse a matching issue first). [Project 5](https://github.com/users/Wladefant/projects/5) aggregates; parent progress comes from native sub-issues. Card states: `Backlog`→`Ready`→`Building`→`QA`→`Review`→`Blocked`→`Done`; merges never auto-`Done` — live verification required.

## 10. Safe DDL, Migrations & Bounded Backfills

- **Safe DDL:** never break deployed startup (Alembic drift crashes, `UndefinedColumn`); migrations stay forward/backward-compatible; hot-table DDL (`markets`) avoids starving retry loops, tailoring lock timeouts to traffic.
- **Bounded Backfills:** explicit scope, preflight, backup, dry-run, canary; idempotent checkpoints, verified row-count/invariant proofs, documented rollback.

## 11. Preserved System Invariants & Environmental Boundaries

- **Production Exclusion:** PolySimulator production (`zaraprptkegxqpvnsubu`, `akamai-iad-prod`) is off-limits — never inspect, modify, test, deploy, or propose.
- **Staging Isolation:** staging Supabase `hgzyqmaanndcimnclxtv` is fully isolated; orders/ledger run with authorized test accounts (SANDBOX optional); real monetary staging transactions are prohibited.
- **Settled Gamma Labels:** Polymarket curated Gamma fields (`groupItemTitle`, `groupItemRange`, `groupItemThreshold`) render verbatim. Do not re-litigate.
- **Merge Commits Only:** integration branches sync via `git merge origin/staging --no-ff`, never rebase.
- **Session Single-Instance:** never duplicate `--resume` on a running session; verify with `Get-CimInstance Win32_Process` first and ensure no active owner before restart.
- **File-Lock Semantics:** never manually delete locks held by active sessions (e.g. `vault.key.lock`); they reap on owner termination.
- **Credential pointers (names only):** OAuth creds in `~/.veyyon/shared-auth/agent.db`; Management API `POST https://api.supabase.com/v1/projects/<ref>/database/query`; staging ref `hgzyqmaanndcimnclxtv`, production `zaraprptkegxqpvnsubu` (protected); in-session `DATABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` are local dev decoys.
- **Supabase PATs are never read-only** (2026-09-20): a PAT carries full owner privileges; the staging PAT (`~/.veyyon/shared-auth/supabase_staging_management_pat.txt`) sees only `hgzyqmaanndcimnclxtv`; the production-capable PAT was purged and is forbidden; production read needs a Read-only org member's PAT verified by a 403 on a harmless PATCH (skill `supabase-readonly-production-access`).
- **Write Lessons Down (2026-09-20):** record durable lessons in the same turn via `learn`/`retain`, managed skills, or policy PRs. Transcript-only learning is lost.

## 12. Quick Invocation Reference & Operational Tooling

Moved to managed skill `poly-quick-invocation-reference`: authoritative locations, pinned PRs, the command reference, and operational boundaries (restart ownership, no-percent audit limits, Stripe test-credential blocker).

## 13. Repeat-Failure Limit & Learning

Never loop identical failing commands:
1. **Retry Limit:** Max 2–3 attempts with the same signature (normalized command + error). The 3rd failure is a HARD STOP.
2. **Switch Approach:** immediately switch to a materially different approach.
3. **Record:** post the failure (command, error, attempts, alternative) on the work item's GitHub issue.
4. **Save Lesson:** same turn, via `learn`/`retain` (facts) or a managed skill (procedures).
5. **Check First:** future lanes `recall` prior lessons before touching that tool or area.
