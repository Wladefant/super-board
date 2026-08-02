# Agent Native — deployed evidence contract

> Companion to `skills/super-board/references/agent-native.md` and
> `payload/agent-native/super-board.json`. The payload says what the cockpit is
> allowed to be; **this document is where the deployed instance proves it.**

A payload can declare anything. What matters is the running cockpit, so each
deployment records its answers here, dated, before the board is activated
against it. The product-side copy of a completed run is filed in the consuming
repository's evidence tree; this file defines what a completed run must contain
and is checked by `tests/test_agent_native_safety.py`.

## Required setting

```
AGENT_PROD_CODE_EXECUTION=off
```

Recorded from the deployed environment, not from a template.

## The seven negative capabilities

Every one is stated as an absence, because "it only projects" is a claim while
"it holds no write token" is checkable.

| Capability | What is recorded |
| --- | --- |
| `no-project-write-credential` | The credential inventory of the deployment, showing no Projects v2 write credential of any kind. |
| `no-github-write-token` | The same inventory, showing no GitHub token with write scope. |
| `no-docker-socket` | The container's mount list, showing no Docker socket. |
| `no-runner-filesystem-mount` | The same mount list, showing no runner or workspace filesystem mounted in. |
| `no-repository-checkout` | The deployed filesystem, showing no checkout of any product repository. |
| `no-trusted-shell` | The result of the synthetic execution probe below. |
| `no-second-completion-ledger` | The deployment's storage inventory, showing no completion or lifecycle ledger — the board is the only one. |

## The two synthetic targets

Unavailability is demonstrated against targets that cannot resolve anywhere.
Never a real Project item ID; never a real repository command. Proving a
mutation is unavailable must not be done by attempting a mutation that could
succeed.

```
Project item ID:      PVTI_SYNTHETIC_TARGET_THAT_DOES_NOT_RESOLVE
Repository command:   superboard-synthetic-command-that-does-not-exist
```

`super_board_runtime.agent_native.probe_deployed_cockpit` refuses any other
target with `probe-target-not-synthetic` and calls neither probe.

## How a run is read

- Both probes **fail** → positive evidence; `safe` is true and `violations` is
  empty.
- Either probe is **accepted** → the capability exists. `project-mutation-available`
  or `repository-execution-available` is recorded, and the board is not
  activated against that deployment until it is removed.

## What this evidence does not cover

It says nothing about what the cockpit *displays*. Display safety is the
publication boundary's job: cockpit output is rendered from read-only snapshots
through `sanitize_and_validate_publication`, exactly like anything bound for
GitHub.
