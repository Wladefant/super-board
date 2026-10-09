# Installed portable workflow runtime

Durable source: [Wladefant/super-board portable workflow package](https://github.com/Wladefant/super-board/tree/main/workflows/portable). Verify per-file source and installed hashes when refreshing selected modules; an old package installation receipt is not proof that every installed file still matches it.

## Canonical operating instructions

- [Portable core, state-preserving export and activation](PORTABLE.md)
- [Native background execution, recovery and explicit notifications](WORKER_EXECUTION.md)
- [Gardener Lane Runner: dead code and unused export cleanup](https://github.com/Wladefant/super-board/blob/main/docs/runbooks/GARDENER.md)
- Reuse or create one dedicated issue per independently actionable deliverable; no shared tracking issue is an intake default.
- [PolySimulator Project board: cross-issue status, ordering and grouping](https://github.com/orgs/Bavariance/projects/1); use the configured Project board for other repositories. An optional master issue is only a convenience checklist of links, never a work specification or alternate tracker.

Use the scripts rather than reproducing their routing, preflight, validation or recovery logic in agent prompts. Native background dispatch is the default; an external agent CLI requires explicit selection. Preparation and dispatch do not count as completed work. Telegram delivery is explicit, rate-limited and outbound; actionable questions link to GitHub rather than accepting inbound Telegram replies.

Run these read-only commands from this directory:

```bash
python ledger.py list
python continuation_driver.py --help
python worker_backend.py --list-backends
python gardener.py --dry-run
python adoption_audit.py --json
python outer_loop_intake.py --help
python verify.py --help
```

For state progression, use the canonical native protocol with an explicitly authorized request, repository and state directory. Merge authorization does not waive current-head QA, independent review or applicable CI.

## State and verification

Preserve `ledger.json`, `decisions.json`, `continuation_journal.json`, `worker_runs/`, notification state, preflight evidence and local configuration during upgrades. Never overwrite the directory wholesale. The ledger is a lossless cross-topic index of dedicated issues, not a replacement for them; unresolved work survives compaction and restarts and is never silently dropped.

The maintained source-bound ledger regression command is `python test_ledger_gates.py`; the repository CI lists the remaining portable contract suites. The older workstation `smoke_test.py` was preserved as an existing local file, not used as current acceptance evidence. Its historical transition fixtures do not satisfy the hardened evidence gates.
The Gardener dead code and unused export regression command is `python test_gardener.py`.
The Verification CLI and smoke test gate regression command is `python test_verify.py`.
The Adoption Audit regression command is `python test_adoption_audit.py`.
The Outer-Loop webhook intake regression command is `python test_outer_loop_intake.py`.
The Theo/T3 workflow tools (issue #208) each have a regression command: `python test_evidence_lint.py` (evidence link lint and posted-media check), `python test_pr_demo_video.py` (PR demo video step), `python test_pr_label_bot.py` (size and risk labels), `python test_project_drift.py` (Project 5 drift sweep). `install_upkeep_task.py` registers the bounded hourly sweep `SuperboardUpkeepHourly` (label bot plus drift repair, super-board only; PolySimulator is report-only).
The Flow QA runner regression command is `node --test test_flow_qa_runner.mjs`.
Mobile tap-target checks measure the effective `hitRect` first, including clickable pseudo-elements.
They use the visible `rect` only when no effective hit area is available.
The runner binds receipts to the measured server SHA from `/api/version`. An untrusted caller `bindSha` cannot override the measured SHA.
Version readers prefer a full 40-hex `commit`. They accept legacy `sha`, `served_sha`, `version`, `git_sha`, and `commitSha` only as full 40-hex values.
SemVer and `deploymentId` do not identify served content. A payload without a valid commit fails closed.
After merge, install only these readers with `python workflows/portable/install_github_native.py --source-root . --sha-readers-only`.
Add `--check` to verify byte parity without changing files. This mode preserves unrelated workflows and the live profile.
The runner records cleanup results and assertions in report evidence and counters. A failing cleanup assertion marks the report and receipt as failed.
The runner rejects unsupported viewports and empty theme lists. Zero-executed coverage cannot print `PASS`.
`formatReceipt` prints `FAIL` if any assertion fails, cleanup fails, or coverage is missing or unsupported.
