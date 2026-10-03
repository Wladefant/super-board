# Outer-Loop Webhook Intake & Event-Driven Queue Consumption

Follow-up to @poteto High-Trust Agent Architecture (Issue #227 Technique 6 / Task 5; delivered in #246).

---

## 1. The Problem: Polling Waste and Untriaged Churn

In traditional agent orchestration, the Main orchestrator repeatedly queries the GitHub issues API and Project 5 GraphQL endpoints on a periodic timer ("polling loop"). This creates three severe failure modes:

1. **Token & Context Exhaustion:** Each polling turn consumes expensive model tokens (e.g. Claude Fable or Opus) reading large lists of unchanged issues and cards.
2. **Rate Limit Depletion:** Continuous board and issue scans burn shared GraphQL points, threatening the immutable 1,000-point reserve (AGENTS.md §5 / CLAUDE.md §Worker rules).
3. **Untriaged Intake Bottleneck:** When an operator files an issue or posts feedback in a comment (e.g. *"approved, go ahead"* or *"hold off, blocked"*), the card sits idle until the next polling sweep, or enters execution with missing labels, wrong milestones, or invalid card states.

---

## 2. The Solution: Event-Driven Outer-Loop Intake

Instead of polling, GitHub Actions acts as an immediate outer-loop webhook intake via `.github/workflows/outer-loop-intake.yml` invoking `workflows/portable/outer_loop_intake.py`.

### Trigger Events
The workflow triggers instantly upon:
- `issues`: `opened`, `edited`, `labeled`, `unlabeled`, `reopened`
- `issue_comment`: `created`, `edited` (filtering out pull request comments)

It runs under an isolated concurrency group:
```yaml
concurrency:
  group: outer-loop-intake-${{ github.event.issue.number || github.ref }}
  cancel-in-progress: true
```

### Deterministic Intake Pipeline
When an event fires, `outer_loop_intake.py` executes:

1. **Taxonomy Enforcement:**
   - **Kind (`kind:`)**: Enforces **exactly one** canonical kind label:
     `kind:bug`, `kind:feature`, `kind:task`, `kind:research`, `kind:docs`, `kind:governance`, `kind:incident`.
     Upgrades legacy labels (e.g. `bug` $\rightarrow$ `kind:bug`, `enhancement` $\rightarrow$ `kind:feature`), deduplicates conflicting multiple kinds, or infers from text patterns if missing.
   - **Area (`area:`)**: Ensures at least one canonical area label (`area:workflow`, `area:harness`, `area:bridge`, `area:sync`, `area:ui`, `area:infra`, `area:security`).
   - **Risk (`risk:`)**: Ensures at least one canonical risk label (`risk:money-path`, `risk:migration`, `risk:high`, `risk:medium`, `risk:low`).

2. **Milestone Assignment:**
   - Preserves existing open milestones (idempotent).
   - If missing, matches issue title and body against active capability milestones (e.g. `GitHub System Integration`, `Phase 2 - Tooling + quota`, `Phase 1 - System hardening`, `Phase 3 - Docs + rollout`).

3. **Project 5 Enrollment:**
   - Checks if the issue is enrolled in Superboard Project 5 (`https://github.com/users/Wladefant/projects/5`).
   - If not enrolled, calls `addProjectV2ItemById` to add it.

4. **Lifecycle State Derivation:**
   - Maps closed issues to `Done`.
   - Parses **operator comments** (from `Wladefant`):
     - `/ready`, `approved`, `go ahead`, `dispatch` $\rightarrow$ `Ready`
     - `/blocked`, `on hold`, `paused` $\rightarrow$ `Blocked`
     - `/building`, `wip`, `in progress` $\rightarrow$ `Building`
     - `/qa`, `needs-qa` $\rightarrow$ `QA`
     - `/review`, `in review` $\rightarrow$ `Review`
     - `/done`, `verified`, `landed` $\rightarrow$ `Done`
   - Detects blockers (`state:blocked`, `state:needs-decision`, open dependencies) $\rightarrow$ `Blocked`.
   - Evaluates completeness (Scope + Acceptance criteria + owner) $\rightarrow$ `Ready`; otherwise `Backlog`.
   - Updates Project 5 `Status` via `updateProjectV2ItemFieldValue`.

5. **Idempotence & Safety:**
   - If labels, milestone, enrollment, and status already match target state, **0 writes are performed**.
   - Supports `--dry-run` returning structured JSON without mutations.

---

## 3. How Main Consumes the Triaged Queue Instead of Polling

With the outer loop handling triage at ingress, Main's consumption model shifts from **periodic polling** to **demand-driven, zero-discovery queue consumption**:

```
[GitHub Event: Issue/Comment]
               │
               ▼
┌──────────────────────────────┐
│  .github/workflows/          │
│  outer-loop-intake.yml       │
│  (cancel-in-progress group)  │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│  workflows/portable/         │
│  outer_loop_intake.py        │
│  - Enrolls in Project 5      │
│  - Infers kind/area/risk     │
│  - Sets Milestone            │
│  - Sets Status to "Ready"    │
└──────────────┬───────────────┘
               │
               ▼
┌──────────────────────────────┐
│  Project 5 "Ready" Column    │
│  (Pre-triaged, zero defect)  │
└──────────────┬───────────────┘
               │ (Event Signal: Telegram / Webhook / Task Completion)
               ▼
┌──────────────────────────────┐
│  Main Orchestrator           │
│  - NO polling timer          │
│  - NO exploratory discovery  │
│  - Pulls 1 Ready card on     │
│    worker completion         │
│  - Launches background lane  │
└──────────────────────────────┘
```

### Protocol Rules for Main:

1. **Zero Discovery Overhead:**
   Main does not search, classify, or validate taxonomy on newly arrived issues. If an issue is in the `Ready` column of Project 5, Main knows it already has:
   - Exactly one valid `kind:` label
   - At least one `area:` and `risk:` label
   - An active open milestone
   - A defined scope and acceptance criteria
   - Zero active blockers

2. **Demand-Driven Consumption (No Timer Polling):**
   Main checks for new work **only when an active worker lane finishes** and capacity is available under host RAM limits (`<85%` RAM). If all lanes are busy or host RAM is starved, Main remains idle.

3. **Event-Driven Signal Delivery:**
   When the operator files a critical issue or posts an approval comment:
   - The outer-loop intake moves the card to `Ready`.
   - Signal is delivered to Main via Telegram notification or workflow completion event.
   - Main wakes up in response to the signal, claims the single top card from `Ready`, dispatches the worker, and returns to sleep.

4. **Claim and Dispatch:**
   - Main queries only the `Ready` status on Project 5 for the next card.
   - Claims the card via `worker_backend.py --prepare-native` and dispatches the task.
   - Zero token waste, zero rate limit burn, instant response to operator input.
