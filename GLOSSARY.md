# Glossary

Domain nouns for super-board. The `improve-codebase-architecture` and `codebase-design` skills read this file, so candidate titles and review findings use these words. Design vocabulary (module, interface, depth, seam, adapter, leverage, locality) lives in [`skills/codebase-design/SKILL.md`](skills/codebase-design/SKILL.md), not here.

| Term | Meaning | Where |
| --- | --- | --- |
| **Request ledger** | Durable local record of a request's state. An execution and recovery cache, never the source of truth. | [`workflows/portable/coordinator.py`](workflows/portable/coordinator.py), [`workflows/portable/github_work_item.py`](workflows/portable/github_work_item.py) |
| **Next-work packet** | The one compact, machine-readable result the coordinator emits per run: `ready`, `wait`, `block` or `done`. | [`workflows/portable/coordinator.py`](workflows/portable/coordinator.py) |
| **Work item** | A GitHub issue with the nine required headings (Scope, Acceptance Criteria, Dependencies & Parent Issue, Owner, State, Branch/PR/Head, Evidence, Next Action, Authorization). | [`workflows/portable/github_work_item.py`](workflows/portable/github_work_item.py) |
| **Project 5** | The GitHub Project board that aggregates work items and their card states. | [`workflows/portable/github_work_item.py`](workflows/portable/github_work_item.py), [`docs/reference/BOARD-IDS.md`](docs/reference/BOARD-IDS.md) |
| **Question contract** | A decision put to the operator as a GitHub issue comment, with options, a recommendation and a bounded scope. Agent-authored comments never answer it. | [`workflows/portable/decision_workflow.py`](workflows/portable/decision_workflow.py) |
| **Build slot** | The exclusive, FIFO-queued lock for `next build`, `next start` and dev Chromium across parallel lanes. | [`workflows/portable/build_slot.py`](workflows/portable/build_slot.py) |
| **Close guard** | The check that refuses to close an issue with an open `- [ ]` box or one younger than 10 minutes. | [`workflows/portable/close_guard.py`](workflows/portable/close_guard.py) |
| **PR gate** | The deterministic CI-and-review check on a pull request, pinned to the head and base, with no self-approval. | [`workflows/portable/github_pr_gate.py`](workflows/portable/github_pr_gate.py) |
| **Gardener** | The role and script that finds dead code and tech debt and prepares bounded cleanup specs labelled `kind:gardener`. | [`workflows/portable/gardener.py`](workflows/portable/gardener.py) |
| **Depth survey** | The report-only scan (`gardener.py --survey-depth`) that ranks shallow modules by recent change and applies the deletion test. | [`workflows/portable/depth_survey.py`](workflows/portable/depth_survey.py) |
| **Deletion-test result** | One of `pass-through`, `concentrates`, `inconclusive`, recorded on every survey candidate and review finding. | [`skills/improve-codebase-architecture/SKILL.md`](skills/improve-codebase-architecture/SKILL.md) |
| **Feature map** | Machine-readable index of features: entry files, tests, owning issue, risk. | [`workflows/portable/feature_map.py`](workflows/portable/feature_map.py) |
| **Evidence lint** | The check that allows only two media link forms and requires the `Deleted:` and `Not run:` lines in a done report. | [`workflows/portable/evidence_lint.py`](workflows/portable/evidence_lint.py) |
| **Adoption audit** | The check that every recommendation is adopted (`adopted-at:`) or rejected (`rejected:`) and that no parent closes with open sub-issues. | [`workflows/portable/adoption_audit.py`](workflows/portable/adoption_audit.py) |
| **Continuation driver** | The loop that repeats the adapter's single step for an explicit list of authorized request ids. It owns no routing or gates. | [`workflows/portable/continuation_driver.py`](workflows/portable/continuation_driver.py) |
| **Spawn state** | Host RAM and disk classification: `ok`, `reap`, `wait`, `no_spawn`. | [`workflows/portable/host_status.py`](workflows/portable/host_status.py) |
| **Lane** | A background worker agent (`super-build`, `super-qa`, `super-review`) that does product work. The orchestrator never does. | [`CLAUDE.md`](CLAUDE.md), [`skills/super-board/SKILL.md`](skills/super-board/SKILL.md) |
