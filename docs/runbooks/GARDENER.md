# Gardener Lane Runner (`gardener.py`)

The **Gardener** is an automated dead code, unused export, and technical debt analysis runner designed for autonomous agent fleets.

References:
- **Superboard Issue:** [#227: Research: High-Trust Agent Architecture & Verification Skills from @poteto](https://github.com/Wladefant/super-board/issues/227)
- **Adopt / Adapt / Build Master Matrix:** Idea 5 (The Gardener Role: Scheduled Cheap Cleanup Lanes)

---

## 1. Why This Exists: The "Gardener" Role

In an autonomous multi-agent environment (10–20 parallel Veyyon lanes operating across shared repositories), technical debt accumulates rapidly:
- Replaced components leave behind orphaned files.
- Refactored helpers leave behind unused exports and dead types.
- Backend modules accumulate unused imports and unreachable code blocks.

Without continuous weeding, dead code acts as a false precedent: subsequent LLM agents read dead code into their context windows, assume it is active, copy deprecated patterns, and introduce subtle regressions.

The **Gardener** provides a deterministic scanner that detects dead code using specialized AST tools, classifies findings into safe vs. framework-protected categories, and prepares bounded cleanup task specifications for cheap worker lanes (Spark or Gemini 3.8 Flash) to prune safely with zero runtime impact.

---

## 2. Integrated Tooling

The Gardener combines two industry-standard static analysis engines:

| Surface | Tool | Command | Scope |
|---|---|---|---|
| **Frontend** (Next.js / TypeScript) | [Knip](https://github.com/webpro-nl/knip) (v6.38+) | `npx knip --reporter json` | Unused files, unreferenced exports, dead types, unused dependencies |
| **Backend** (FastAPI / Python) | [Vulture](https://github.com/jendrikseipp/vulture) | `python -m vulture backend/ --min-confidence 80` | Dead functions, unused imports, unreachable code, dead variables |

---

## 3. Classification & Safety Guardrails

Static analysis tools inherently produce false positives if run naively against modern frameworks (e.g. Next.js App Router entrypoints or Pytest fixture injections). The Gardener applies strict deterministic classification rules:

### A. Preserved (Never Auto-Pruned)
1. **Next.js App Router Entrypoints:** Files matching `app/**/page.*`, `app/**/layout.*`, `app/**/route.*`, `loading.*`, `error.*`, `not-found.*`, `template.*`, etc. Default exports in these files are framework entrypoints.
2. **Framework & Tooling Configs:** `next.config.*`, `tailwind.config.*`, `postcss.config.*`, `middleware.ts`, `tsconfig.json`, `package.json`.
3. **Static Public Assets:** Anything in `public/**` (e.g. service workers, manifest).
4. **Test Files:** Files matching `*.test.*`, `*.spec.*`, or located in `__tests__/`. (Knip may flag them if not in default scan paths, but test runners execute them via glob patterns).
5. **Pytest Fixtures & Test Dummies:** Unused parameters in `tests/` or `conftest.py` are fixture injections or mock return holders, not dead code.
6. **Alembic Migration Configurations:** Variables in `backend/alembic/` (e.g. `revision`, `down_revision`, `fileConfig`, `target_metadata`).
7. **Package Re-Exports:** Imports in `__init__.py` files defining public package API surfaces.

### B. Safe to Prune
1. **Verified Dead Files:** Unreferenced components or utility files in `components/`, `lib/`, `hooks/`, `utils/` with zero incoming imports.
2. **Verified Dead Exports:** Exported helper functions or constants in internal modules never imported anywhere else. (The `export` keyword can be stripped or the symbol deleted if unused locally).
3. **Verified Dead Types:** Exported TypeScript `interface` or `type` definitions never referenced in any module.
4. **Unused Application Imports:** Unused Python imports in `backend/app/` (confidence $\ge 80\%$).
5. **Unreachable Code:** Code following unconditional `return`, `raise`, `while`, or `break` statements.

### C. Review Required
1. **Unused Dependencies in `package.json`:** Marked for manual review, as some packages are CLI-only, Tailwind plugins, or runtime peer dependencies (e.g. `sharp`, `autoprefixer`).

---

## 4. Bounded Cleanup Task Specification for Cheap Lanes

Rather than attempting to prune hundreds of findings in one risky commit, the Gardener prepares a **bounded batch** (default: 15 items) for execution by a cheap model lane:
- **Target Lanes:** `spark` (OpenAI Codex Spark) or `task` / `flash` (Gemini 3.8 Flash).
- **Task Contract:** Standardized format adhering to `## Goal`, `## Constraints`, `## Contract`, `## Target`, `## Change`, and `## Acceptance`.
- **Verification Gates:**
  1. `cd frontend && npx tsc --noEmit` MUST exit 0 with zero errors.
  2. Targeted unit tests for any touched backend files MUST pass.
  3. Git diff strictly confined to removing the targeted dead symbols and files.
  4. Pull request created against the base integration branch (`staging` or `main`), labeled `kind:gardener`, `risk:low`.

---

## 5. Usage & CLI Reference

Run from `workflows/portable/` or with full path:

```bash
# Dry run: Scan PolySimulator, classify findings, print summary table and task spec (read-only)
python workflows/portable/gardener.py --dry-run --repo-root /path/to/polysimulator

# Output rich Markdown report suitable for PR descriptions or issue comments
python workflows/portable/gardener.py --dry-run --markdown --repo-root /path/to/polysimulator

# Save structured JSON report and generated task spec to disk
python workflows/portable/gardener.py --dry-run \
  --repo-root /path/to/polysimulator \
  --output-json gardener_report.json \
  --task-spec-out cleanup_task.md

# Custom thresholds and batch sizes
python workflows/portable/gardener.py --dry-run \
  --repo-root /path/to/polysimulator \
  --min-confidence 85 \
  --max-items 20 \
  --target-lane spark
```

### Options:
- `--repo-root`: Target repository path (default: auto-detected PolySimulator checkout).
- `--frontend-dir`: Subdirectory for frontend code (default: `frontend`).
- `--backend-dir`: Subdirectory for backend code (default: `backend`).
- `--min-confidence`: Minimum confidence percentage for Vulture findings (default: `80`).
- `--max-items`: Maximum pruning candidates in generated task spec (default: `15`).
- `--target-lane`: Cheap model lane for cleanup execution: `spark`, `task`, or `flash` (default: `spark`).
- `--dry-run`: Read-only execution; prints findings summary and task spec without mutating files.
- `--live`: Create live GitHub issues for top prioritized candidates and enroll them into Superboard Project 5.
- `--issue-repo`: Target GitHub repository for created issues (default: `Bavariance/polysimulator`).
- `--max-new-issues`: Maximum number of live GitHub issues to create per run (default: 5, hard-capped at 5).
- `--scan-bugs-only`: Fast mode; bypasses heavy Knip/Vulture scan and executes closed-bug-to-lint-rule scan only.
- `--install-tasks`: Installs recurring Windows Task Scheduler (`schtasks`) jobs for daily full scan and hourly bug scan.
- `--log-dir`: Directory for saving execution logs and `.cmd` wrapper scripts (default: `C:/Users/wkiri/.veyyon/run/gardener`).
- `--skip-ram-check`: Bypasses host RAM utilization safety check (by default, halts when host RAM $\ge 90\%$).
- `--markdown`: Output full Markdown document to stdout.
- `--json`: Output raw JSON report to stdout.
- `--output-json <path>`: Write JSON report to specified file.
- `--task-spec-out <path>`: Write task specification Markdown to specified file.
- `--knip-report-file <path>`: Ingest pre-computed Knip JSON report (offline / fixture mode).
- `--vulture-report-file <path>`: Ingest pre-computed Vulture text report (offline / fixture mode).

---

## 6. Live Issue Dispatching & Superboard Integration

When executed with `--live`, the Gardener converts scan findings and bug-to-lint proposals into authoritative GitHub issues strictly adhering to the 9-point Superboard Issue Contract (`## Scope`, `## Acceptance Criteria`, `## Dependencies & Parent`, `## Owner`, `## State & Blockers`, `## Branch/PR/Exact Head`, `## Evidence`, `## Next Action`, `## Authorization`):

1. **Deterministic Deduplication:** Each issue embeds a comment `<!-- fingerprint: gardener:<category>:<hash> -->`. The Gardener checks open and closed issues in the target repo before creation, never recreating or reopening an existing or rejected issue.
2. **Hard Capping:** At most 5 issues are created per execution run (`--max-new-issues 5`).
3. **Metadata & Labels:**
   - Label: `kind:gardener`
   - Area: `area:frontend`, `area:api`, or `area:workflow`
   - Risk: `risk:low`
   - Milestone: Active open milestone (e.g. `Staging Stabilization & DDL Gate`)
   - Assignee: `Wladefant`
   - Project: Automatically enrolled into Wladefant Project 5 (`PVT_kwHOBL7E1c4Bd5R1`).

---

## 7. Bug-to-Lint-Rule Loop (Poteto Ratchets)

The Gardener continuously surveys recently closed bugs with merged pull requests (`gh issue list --state closed --label kind:bug`) and generates issue candidates proposing static analysis ratchets:
- **Target AST engines:** `ast-grep` (`rules/`, `sgconfig.yml`), custom ESLint rules (frontend), or Ruff / importlinter contracts (backend).
- **Contract:** Each proposed lint rule issue specifies positive control (fails on the pre-fix code pattern) and negative control (passes cleanly on current staging trunk).

---

## 8. Windows Task Scheduler Recurring Automation

The Gardener is configured to execute autonomously via Windows Task Scheduler without requiring human intervention or orchestrator presence:

```bash
# Install or update scheduled tasks
python workflows/portable/gardener.py --install-tasks --repo-root C:/Users/wkiri/development/wt-polysim-gardener-staging

# Query task status
schtasks /query /tn SuperboardGardenerDaily /fo LIST
schtasks /query /tn SuperboardGardenerBugLintHourly /fo LIST

# Manually trigger hourly bug scan
schtasks /run /tn SuperboardGardenerBugLintHourly
```

Tasks:
1. **`SuperboardGardenerDaily`**: Runs daily at 03:00 UTC. Executes full Knip + Vulture + workaround comments scan and creates up to 5 issues.
2. **`SuperboardGardenerBugLintHourly`**: Runs hourly. Executes fast closed-bug-to-lint scan and creates lint-guard issues.

Both tasks log execution output to `C:/Users/wkiri/.veyyon/run/gardener/gardener_<timestamp>.log`.

### Host RAM Safety Guard
Before executing any scan, `gardener.py` queries `host_status.py --json`. If host RAM utilization is $\ge 90\%$ (the `no_spawn` / `wait` threshold), the runner logs a warning and exits cleanly without spawning heavy analysis processes.

---

## 9. Verification & Automated Tests

A dedicated test suite validates the scanner, classifier, spec generator, deduplication, capping, and automation:

```bash
python workflows/portable/test_gardener.py
```

Test coverage (22 automated unit tests):
- Next.js App Router entrypoint detection & config file whitelisting.
- Knip classification for dead files, dead exports, dead types, and package dependencies.
- Vulture classification for app imports, pytest fixtures, Alembic metadata, and unreachable code.
- Priority-ordered bounded batch selection and contract formatting.
- 9-point Superboard Issue Contract body formatting with deterministic fingerprint metadata.
- Deduplication by fingerprint across open and closed issues.
- Hard cap enforcement ($\le 5$ live issues created per run).
- Untracked workaround comments scanner with positive/negative keyword matching.
- Host RAM safety check with mock threshold validation.
- End-to-end pipeline execution with mock fixture reports.
