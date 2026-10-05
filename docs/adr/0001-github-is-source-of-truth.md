# 0001. GitHub is work truth; the request ledger is a cache

Status: accepted

## Context
Work state lived in session memory and local files. Both go stale or vanish when a lane or host dies.

## Decision
GitHub issues and Project 5 are the system of record. The request ledger is an execution and recovery cache. Intake reads GitHub with an authenticated call before scheduling. On an API failure, an incomplete connection or a missing structure, intake stops. It never falls back to the stale cache. Reporting writes an issue comment and verifies it through the API.

## Consequences
- A lane that cannot reach GitHub blocks; it does not guess from disk.
- The ledger stays small and disposable.
- A candidate that makes the ledger authoritative contradicts this ADR.

Evidence: [`workflows/portable/github_work_item.py`](../../workflows/portable/github_work_item.py) (module docstring), [`workflows/portable/coordinator.py`](../../workflows/portable/coordinator.py) ("Canonical System of Record").
