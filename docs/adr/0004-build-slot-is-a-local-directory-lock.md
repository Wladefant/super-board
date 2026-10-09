# 0004. The build slot is a local atomic directory lock with a FIFO queue

Status: accepted

## Context
Parallel lanes need exclusive use of `next build`, `next start` and dev Chromium. The slot used to be handed over by orchestrator chat messages ("BUILD SLOT TAKEN/FREE"), which the script replaces with a local, crash-resilient lock.

## Decision
Arbitrate the slot with `os.mkdir` on a lock directory plus a FIFO queue file with heartbeats. Windows uses `msvcrt`, not `fcntl`. Dead owners and expired token leases are reclaimed with a logged notice. Release by a non-owner is refused.

Stable guard files serialize directory creation, metadata publication, reclaim and release.
Windows uses a byte-range file lock. POSIX uses `flock`.
The operating system releases the guard when its process ends.
Reentrant guard state belongs to one PID and one thread. Fork children must acquire their own OS guard.
Guard files stay at fixed paths and are never renamed or deleted.
Slot heartbeats use the same guard, so reclaim reads a current lease.
Reclaim checks identity before and after an atomic tombstone rename.
Release deletes only its detached, token-matched directory.
All processes must use this protocol. Install it only when no slots are held.

Queue management invariants:
- The short-lived atomic queue lock (`build-slot-queue.lock`) protects queue operations. Waiters reclaim stale queue locks when the holding PID dies. Waiters also reclaim locks held by a live PID longer than 120 seconds. Reclamation renames the lock to a unique tombstone and checks the token before deletion. The lock owner records a unique token in metadata and confirms this token before removing the directory. This check stops an expired owner from deleting a successor lock. Critical sections inside the queue lock run no RAM probes, process waits, or sleeps.
- Queue read errors raise or retry on transient file access/sharing errors and JSON decode collisions; a missing file returns an empty list only on initial queue creation.
- Queue heartbeats expire after 60 seconds, even with a live shared PID. Only entries without a heartbeat use the 1800-second fallback.
- When an active `acquire` loop detects a missing queue entry during heartbeat validation, re-enqueue recovery preserves the original enqueue timestamp to protect FIFO fairness.
- Manual acquire holders expire after 30 minutes of heartbeat silence, or acquisition age if no heartbeat exists. A genuine live run wrapper never expires on age or heartbeat silence. Dead or recycled wrappers permit reclaim. This preserves the slot while a wrapped command runs.
- Contenders time out when a live process holds the queue lock for less than 120 seconds. Contenders safely reclaim locks held longer than 120 seconds through a unique tombstone directory rename.
- A queue-lock timeout is a transient fault, not a verdict on the waiter. `acquire` retries enqueue with jittered backoff until the caller's own `--timeout` ends, then returns `False`; it never raises and never drops the waiter's place. Queue cleanup after a slot is settled (post-acquire, release, abort) retries for a grace period (8 s, shorter than the release deadline). If it still fails, `clean_queue` sweeps the entry, because its PID is dead or its token holds a slot. Waiters poll the queue without the lock and take it only when a write is needed, so 30 pollers do not starve the writers. Lock polling uses jittered, growing delays. The queue-lock owner retries deleting its lock dir (waiters hold `info.json` open, and Windows refuses to delete an open file), so a release never leaves a lock behind for as long as a long-lived `run` wrapper lives. A queue lock is held for milliseconds and never across a wrapped command. A lock held longer than 120 s, even by a live PID, was leaked and is reclaimed. Each queue-lock attempt is capped at the time left to the caller's `--timeout`; cleanup after a grant or a give-up retries for at most 1 s, and `clean_queue` sweeps any residue.
- Operator build freeze: when 'build-freeze' exists in the run directory, 'acquire' and 'run' commands immediately abort with exit code 75. They print 'build freeze active (<reason>)' to stderr (or 'reason unavailable' if reading fails). Waiting queues and command invocations do not start. If 'build-freeze' appears while a waiter is already waiting in the FIFO queue, that queued waiter immediately exits with exit code 75 (raises `SystemExit(75)`). 'release' and 'status' remain unaffected.
- Acquisition checks freeze during queue retries and mutex waits. Release and queue cleanup do not cancel on freeze.

Memory admission and job classification invariants:
- `get_available_ram_gib()` determines available host memory. It checks system RAM and honors the `BUILD_SLOT_AVAILABLE_GIB` override.
- Admission keeps a 3 GiB floor: `available_ram - ramp_reservations - new_reservation >= 3.0`.
- Count held reservations during ramp-up only: heavy for 300 seconds, medium for 120 seconds, and light or browser for 60 seconds.
- After ramp-up, the job's actual memory already reduces available RAM. Do not subtract its full reservation again.
- Missing, corrupt or future acquisition times keep the full reservation charge.
- Status reports total effective `reserved_gib` and current `ramp_reservations_gib` separately.
- Jobs belong to four classes: `heavy`, `medium`, `light`, and `browser`.
- Next builds and Next servers are heavy. Chrome-only QA is browser. TypeScript, Vitest, Wrangler, and workerd are medium. Other commands are light.
- Heavy reservations have a 5 GiB minimum, even with explicit `--mem-gib 3`. Larger explicit reservations remain unchanged. Medium defaults to 1.5 GiB, light to 0.5 GiB, and browser to 1.1 GiB.
- `acquire` defaults to light because it has no child command. Use `--class heavy` for manual builds or servers. Use `--class browser` only for headless QA without a build or server.
- Both commands accept `--class` and `--mem-gib` to override classification and reservation.
- Active slots record their reserved memory. Legacy slots without reservation metadata count as heavy with a 5 GiB minimum.
- Unknown classes in shared queue or holder metadata count as heavy. They use the heavy minimum reservation, heavy cap, ramp window, stagger, and aging rules. Readers must not fail when a newer process writes a class they do not know. CLI class validation remains strict.
- At most one `heavy` job may run concurrently across all slots. A second heavy job must wait in queue even if enough free RAM exists.
- Browser jobs do not count toward the heavy cap. They can run beside a heavy build when both reservations preserve the RAM floor. A browser `run` refuses an obvious Next build or server command.
- `--force` requires `BUILD_SLOT_ALLOW_FORCE=1`. Without it, acquisition fails. Authorized force logs the override and bypasses memory admission.
- Even when `BUILD_SLOT_ALLOW_FORCE=1` is set, `--force` cannot bypass the one-heavy job concurrency cap.
- The obsolete idle bypass is removed. Queue wait duration never bypasses host memory safety invariants.
- Admission reads the stagger under the slot guard. The queue head and every heavy candidate retain the stagger. A smaller non-heavy job can backfill a stagger-blocked head when its reservation fits. Only heavy grants advance the stagger timestamp, so backfill does not extend the head's delay.
- Waiters print resource refusal reasons and include the last reason in timeout output.
- Smaller jobs can backfill a resource- or stagger-blocked head without changing its position or enqueue time.
- After 20 minutes, a heavy head reserves a 60-second drain window every 180 seconds when no heavy job runs. Smaller jobs can backfill between windows until the head reaches 40 minutes. At 40 minutes, stop backfill when no heavy job runs and projected memory can fit the head. Existing holders must drain before smaller admissions resume. The head keeps first admission whenever it fits.
- An impossible head does not pause backfill. Output reports its required memory and maximum possible budget.

Command execution deadline and process tree invariants:
- `run` introduces `--run-timeout` (default 1800 seconds / 30 minutes) for child command execution.
- The existing `--timeout` parameter applies only to FIFO queue wait time.
- When `--run-timeout` expires, the arbiter terminates the entire process tree (killing child and grandchild processes) and exits with exit code 124.
- Windows uses hidden, bounded `taskkill /T /F`. The wrapper releases its slot after completion, timeout, or a handled exit.
- Windows assigns the kill-on-close Job Object at process creation through `PROC_THREAD_ATTRIBUTE_JOB_LIST`. No child exists outside the Job.
- Forced wrapper termination kills descendants, including during launch before child metadata is published.
- Stale reclaim retains reservations while the recorded child or named Job has active processes.

Environment variable tuning invariants:
- Commands executed under `run` receive tuned environment variables:
  - `NODE_OPTIONS`: preserves any existing `--max-old-space-size`. If unset, sets `--max-old-space-size=3072` for `heavy` jobs and `--max-old-space-size=1536` for `medium` jobs.
  - Detected Vitest commands receive `VITEST_MAX_WORKERS=2` unless the caller sets it. Also pass `--maxWorkers=2` to Vitest.

Status and observability invariants:
- `status` reports `memory_budget` with `available_gib`, `reserved_gib`, `floor_gib`, and `free_budget_gib`.
- Slot status entries in `status()["slots"]` include `job_class` and `mem_gib`.

## Consequences
- Lanes call the script directly and never ask the orchestrator for a slot.
- A candidate that moves the lock behind a network service must show what varies across that seam. One adapter is a hypothetical seam.

Evidence: [`workflows/portable/build_slot.py`](../../workflows/portable/build_slot.py) (module docstring).
