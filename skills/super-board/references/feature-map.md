# Feature Map for Agent Navigation

## 1. Purpose

The feature map (`FEATURE_MAP.json`) provides a machine-readable index mapping high-level system features and workflow domains directly to:
- **Entry files**: Primary execution paths and entrypoints.
- **Test suites**: Targeted unit, integration, and regression test suites.
- **Owning issue**: GitHub issue tracking design, architecture, or requirements.
- **Risk level**: Domain risk tier (`low`, `medium`, `high`, `critical`) aligned with review gates.
- **Components**: Granular sub-components, documentation, and schemas.

Lanes must consult the feature map before broad repository searches (`search`, `grep`, `find`) to resolve entrypoints and test suites in a single bounded step.

## 2. CLI Tooling (`feature_map.py`)

A portable Python CLI is available at `workflows/portable/feature_map.py`:

```bash
# Validate feature map against schema and verify all paths exist on disk
python workflows/portable/feature_map.py validate

# Query feature map by search term or regex across features, descriptions, paths, and tags
python workflows/portable/feature_map.py query "build_slot"

# Display detailed JSON or YAML representation of a specific feature
python workflows/portable/feature_map.py show gate

# List all registered features with summary status
python workflows/portable/feature_map.py list

# Generate markdown summary table
python workflows/portable/feature_map.py generate --format markdown
```

## 3. Seeded Feature Domains

The following core workflow domains are seeded in `FEATURE_MAP.json`:
1. `routing`: Model routing, tier allocation, usage monitoring, and quota snapshots.
2. `gate`: Deterministic GitHub PR check, content review freshness, and merge eligibility gates.
3. `ledger`: Durable JSON request ledger, issue state reconciliation, and file locks.
4. `gardener`: Background repo hygiene, worktree reaping, log retention, and lock recycling.
5. `build_slot`: FIFO build and browser slot arbiter preventing CPU/RAM exhaustion.

## 4. Maintenance & Validation Rules

- Every registered path (`entry_files`, `tests`, `components.*.path`) MUST resolve to a real file or directory on disk.
- Validation is enforced in continuous integration (`cross-platform.yml`).
- When adding or refactoring features, update `FEATURE_MAP.json` and ensure `python workflows/portable/feature_map.py validate` passes.
