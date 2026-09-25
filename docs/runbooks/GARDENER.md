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
- `--markdown`: Output full Markdown document to stdout.
- `--json`: Output raw JSON report to stdout.
- `--output-json <path>`: Write JSON report to specified file.
- `--task-spec-out <path>`: Write task specification Markdown to specified file.
- `--knip-report-file <path>`: Ingest pre-computed Knip JSON report (offline / fixture mode).
- `--vulture-report-file <path>`: Ingest pre-computed Vulture text report (offline / fixture mode).

---

## 6. Verification & Automated Tests

A dedicated test suite validates the scanner, classifier, and spec generator:

```bash
python workflows/portable/test_gardener.py
```

Test coverage:
- Next.js App Router entrypoint detection & config file whitelisting.
- Knip classification for dead files, dead exports, dead types, and package dependencies.
- Vulture classification for app imports, pytest fixtures, Alembic metadata, and unreachable code.
- Priority-ordered bounded batch selection and contract formatting.
- End-to-end pipeline execution with mock fixture reports.
