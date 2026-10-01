# Reference: Parallel Lane Dispatch Patterns

Detailed dispatch patterns, domain partitioning strategies, and harness contract specifications.

## Valid Batch vs Refused Batch Comparison

| Dispatch Shape | Harness Behavior | Recommended Strategy |
|---|---|---|
| `task(tasks=[L1, L2, L3])` with look-alike triage/audit tasks | **Refused** (`Refused N parallel triage agents...`) | Split into single `task` calls in consecutive turns or batch into 1 classification lane |
| `task(tasks=[L1, L2])` with distinct implementation contracts (e.g. backend fix + doc update) | **Accepted** | Valid multi-task batch; ensure non-overlapping file ownership |
| Sequential single `task(tasks=[L1])`, then `task(tasks=[L2])` | **Accepted** | Standard pattern for verification sweeps across domains |

## Domain Partitioning Strategy
When sweeping across large backlogs or multi-file inspections:
1. Group targets by architectural boundary:
   - Domain A: Backend / Core API
   - Domain B: Frontend UI / Responsive viewports
   - Domain C: Packaging / Portable workflow tooling
   - Domain D: Policy / Documentation
2. Write per-domain worklists to `local://<domain>-inventory.md`.
3. In each task prompt, reference the local file: `Read local://<domain>-inventory.md and execute verification for these items`.
4. Ensure each domain defines its own unique `# Target`, `# Change`, and `# Acceptance` criteria.
