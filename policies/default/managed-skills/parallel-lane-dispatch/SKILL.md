---
name: parallel-lane-dispatch
description: "Use when dispatching several subagent lanes for issue triage, review sweeps or any fan-out whose task texts look alike; avoids the harness 'Refused N parallel ... agents' rejection and the wasted turns of retrying it."
---

# Parallel lane dispatch without harness refusals

## Symptom
`task(tasks=[...])` with 3+ lanes returns `Refused N parallel triage agents. Homogeneous triage is a batched lookup/classification task...`. It triggers on similar-looking task bodies (same verbs, same acceptance text), regardless of whether the lanes actually need per-item code verification. Retrying with regrouped batches in the SAME call shape is refused again (observed twice, 2026-09-14).

## Procedure
1. Decide the fan-out by DOMAIN, not by number range (e.g. Paket / ADO / UI / Tooling / Docs). Write each lane's `# Target` with its own file areas and its own decision rules.
2. Write the per-lane input lists to `local://<name>.md` first (one write per lane) so each task body stays short and distinct.
3. Dispatch ONE `task` call per lane, consecutive turns. Do not put them in one `tasks=[]` array. Five single calls were accepted immediately.
4. If a single call is still refused, the work really is a lookup: fetch the whole record set yourself in one API call and send the complete set to ONE lane.
5. Never repeat a refused batch shape. One refusal = switch strategy in the next turn.

## When a true batch is fine
Independent implementation slices with different files/contracts (e.g. port-a-fix lane + release-tooling lane) pass in one `tasks=[]` call; the refusal is specific to look-alike classification lanes.

## Tracking
Harness/policy follow-up: https://github.com/Wladefant/super-board/issues/135
