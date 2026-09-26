---
name: native-superboard-background-handoff
description: "Operate the installed portable Superboard workflow through responsive native background tasks, durable tickets, and script-owned validation."
---

# Native Superboard Background Handoff

Operational protocol for dispatching, recording, and completing native background work within Superboard.

## Navigation
- **Gotchas & Failure Modes**: Read `gotchas.md` for details on task handle URI rewriting and continuation driver traps.
- **Reference Flows**: See `references/task-handles.md` for ticket lifecycle states and continuation invocations.

## When to Use This Skill
- Operating Superboard continuation workflows (`continuation_driver.py`).
- Dispatching durable background work tickets and recording task handles.
- Completing and validating native agent deliverables.

## Core Protocol
1. **Run Driver:** Invoke `continuation_driver.py` for authorized request IDs with explicit `--state-dir` and `--repo-root`.
2. **Dispatch Native Background Task:** When a ticket is prepared, pass its prompt and result schema to a native background task. Never use an inline `agent(...)` bridge or implicit CLI.
3. **Record Handle:** Record the actual task handle using `worker_backend.py --record-native`. Preserve the literal `agent://` value verbatim.
4. **Complete with Authentic Evidence:** On completion, save the authentic structured result and execute `--complete-native`. Never fabricate checks, exits, or head hashes.
5. **Consume Result:** Re-invoke the continuation driver to advance the ticket state.
