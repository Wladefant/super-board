# 0002. The coordinator and reviewers never merge, deploy or spawn themselves

Status: accepted

## Context
An autonomous loop that merges, deploys or starts more workers can compound one error across many items before a person sees it.

## Decision
The coordinator makes one bounded evaluation step and emits a recommendation. It does not auto-merge, auto-deploy or self-spawn, and it needs no native scheduler. The reviewer ends at a handoff record; a human merges.

## Consequences
- The continuation driver wraps the adapter's single step and owns no routing or gates.
- Merge-prohibition scanning of runtime and skill sources is part of the release gate.
- A candidate that merges the continuation driver into the coordinator needs to keep both invariants.

Evidence: [`workflows/portable/coordinator.py`](../../workflows/portable/coordinator.py) ("Inviolable Architectural Invariants"), [`workflows/portable/continuation_driver.py`](../../workflows/portable/continuation_driver.py), [`skills/super-review/SKILL.md`](../../skills/super-review/SKILL.md) ("The runtime never merges").
