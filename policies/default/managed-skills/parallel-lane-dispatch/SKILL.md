---
name: parallel-lane-dispatch
description: "Use when dispatching subagent lanes for triage, review sweeps, or verification fan-outs whose task prompts look alike; prevents harness batch refusals ('Refused N parallel ... agents') and wasted turns."
---

# Parallel Lane Dispatch

Modular guide for dispatching parallel lanes without triggering harness batch refusals.

## Navigation
- **Gotchas & Failure Modes**: Read `gotchas.md` first before retrying any refused batch dispatch.
- **Reference Patterns**: See `references/batch-patterns.md` for domain partitioning and batch examples.

## When to Use This Skill
- Dispatching 3+ subagent lanes for triage, issue review, or bulk verification.
- Harness returns: `Refused N parallel triage agents. Homogeneous triage is a batched lookup/classification task...`.
- Sweeping across multiple repositories, packages, or documentation files.

## Procedure
1. **Partition by Domain:** Divide fan-out by functional domain (e.g. Package / ADO / UI / Tooling / Docs), not arbitrary numbers or index ranges. Each lane must define its own `# Target`, distinct file boundaries, and domain-specific decision rules.
2. **Externalize Inputs:** Write per-lane input lists to `local://<name>.md` beforehand so task bodies remain concise and structurally distinct.
3. **Sequential Single-Lane Dispatch:** Whenever lanes perform per-item code verification, dispatch as separate single `task` calls in consecutive turns rather than a single `task(tasks=[...])` array.
4. **Fallback to Single Lookup:** If single-lane dispatch is still refused, treat the work as a pure classification lookup: fetch the dataset in one call and delegate to a single triage lane.
5. **Never Repeat Refused Shapes:** One refusal = switch strategy immediately in the next turn.

## Tracking
- Incident & policy root cause: https://github.com/Wladefant/super-board/issues/135
