---
name: poly-quick-invocation-reference
description: "Quick invocation reference for PolySimulator orchestration tooling: authoritative locations, pinned PRs, coordinator/adapter/ledger/gate commands, restart and blocker declarations."
---

# Poly Quick Invocation Reference

Source of truth: `policies/default/AGENTS.md` §12 in [super-board](https://github.com/Wladefant/super-board) (trim carve-out, token audit 2026-09-26).

## Authoritative Locations & Pinned PRs
- **Portable Workflow Package:** `C:/Users/wkiri/development/wt-portable-workflow-core` (Superboard PR #74 head `693de37722e5d186b900d4fdb2d1e2dee9feb2fe`, branch `feat/portable-workflow-core`)
- **PolySimulator Hardened Policy:** `C:/Users/wkiri/development/.wt-policy-qa-safeguards` (PR #4545 head `2206e73904c7b77f98b8589d41fe2b3454899848`, branch `docs/4544-harden-agents-qa-safeguards`)
- **Installed Runtime Modules:** `~/.veyyon/workflows/` (`coordinator.py`, `superboard_adapter.py`, `ledger.py`, `preflight.py`, `github_pr_gate.py`, `model_routing.py`, `balance_loader.py`, `telegram_notifier.py`, `github_plan_renderer.py`, `project_adapter.py`)
- **Versioned Profile Policy:** Source: `policies/default/AGENTS.md` in [super-board](https://github.com/Wladefant/super-board). Change that source in a PR first; `install_github_native.py` installs exact source bytes and `--check` verifies SHA-256 equality against profile/runtime paths. Never make an invisible profile-only edit or restart sessions/rebind models as part of installation.
- **Authoritative Aggregation:** [Project 5](https://github.com/users/Wladefant/projects/5). Select a dedicated issue per deliverable; no fixed umbrella issue is a default.

## Short Command Reference
```bash
# 1. Bounded Single-Step Evaluation (reads GitHub intake; no dispatch)
python ~/.veyyon/workflows/coordinator.py --summary --no-sync-decisions

# 2. Superboard Execution Adapter Bounded Dispatch (Labeled fake fixture proof)
python ~/.veyyon/workflows/coordinator.py --dispatch --fake-executor --summary

# 3. Superboard Execution Adapter Safe Worker Probe (Real subprocess execution)
python ~/.veyyon/workflows/coordinator.py --dispatch --real-worker --summary

# 4. Standalone Adapter Invocation with Deduplicated Telegram Notification
python ~/.veyyon/workflows/superboard_adapter.py --notify-telegram --telegram-dry-run --summary

# 5. Local Request Ledger Queries & Recovery
python ~/.veyyon/workflows/ledger.py list
python ~/.veyyon/workflows/ledger.py next
python ~/.veyyon/workflows/ledger.py check --strict
python ~/.veyyon/workflows/ledger.py recover

# 6. Deterministic PR Gate & Review Approval Verification
python ~/.veyyon/workflows/github_pr_gate.py --pr <pr_url_or_number> --head-sha <sha>

# 7. Targeted Smoke Test Suites (100% Passing)
python ~/.veyyon/workflows/routing_smoke_test.py
python ~/.veyyon/workflows/test_superboard_adapter.py
python ~/.veyyon/workflows/coordinator_smoke_test.py
```

## Operational Boundaries, Restart & Blocker Declarations
1. **Session & Daemon Restart Requirement & Safe Ownership:**
   - Active Veyyon sessions and background worker brokers must be restarted (or a new session initialized) for updated profile configuration (`profiles/default/agent/config.yml`) and installed workflow adapters to take active effect; `profiles/default/agent/AGENTS.md` updates provide orchestration policy guidance for prompt context. No runtime hot-reloading exists for daemon process states or active model bindings; CLI flags and harness model bindings require session restart.
   - **Never run duplicate `--resume` on an active session UUID.** Always verify running instances via `Get-CimInstance Win32_Process -Filter "Name='veyyon.exe'"` prior to resuming. No background restart without safe single-owner handling.
   - **Roster check before replacing a lane:** a bare `job` poll shows only job-backed agents. Lanes revived over IRC keep running without a job entry and show up only under `job list: true` → "Running Agents — not job-backed". Run `job list: true` before declaring a lane (e.g. the merge sequencer) gone, spawning a successor, or telling workers to self-merge. (2026-09-26: missing this spawned a duplicate MergeSeq3 while MergeOpus2 had already merged PolySimulator #5613/#5618.)
2. **Corrected No-Percent Audit Limits:**
   - Audit boundaries and quality gates must be grounded in finite empirical cohorts and deterministic head-bound SHAs, never arbitrary percentage heuristics (e.g. no "audit X% of PRs" and no unprovable claims of "100% bug absence").
   - Defect resolution requires demonstrating the absence of the defect under the exact original failure scenario on the deployed target.
3. **Connected-Service Blocker: Stripe Test Configuration:**
   - Stripe test API credentials (`STRIPE_SECRET_KEY`) are not configured in the local workstation environment.
   - Preflight gate status: Stripe probes are evaluated as `not_applicable` for non-financial tasks and strictly **BLOCKED** for money-path operations. Staging exclusively uses isolated staging balance and order routes with authorized test accounts (SANDBOX available as optional safety); production access (`<prod-supabase-ref>`) is strictly prohibited.
