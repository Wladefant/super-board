# 0004. The build slot is a local atomic directory lock with a FIFO queue

Status: accepted

## Context
Parallel lanes need exclusive use of `next build`, `next start` and dev Chromium. The slot used to be handed over by orchestrator chat messages ("BUILD SLOT TAKEN/FREE"), which the script replaces with a local, crash-resilient lock.

## Decision
Arbitrate the slot with `os.mkdir` on a lock directory plus a FIFO queue file with heartbeats. Windows uses `msvcrt`, not `fcntl`. Dead owners and expired token leases are reclaimed with a logged notice. Release by a non-owner is refused.

Stable guard files serialize directory creation, metadata publication, reclaim and release.
Windows uses a byte-range file lock. POSIX uses `flock`.
The operating system releases the guard when its process ends.
Guard files stay at fixed paths and are never renamed or deleted.
Slot heartbeats use the same guard, so reclaim reads a current lease.
Reclaim checks identity before and after an atomic tombstone rename.
Release deletes only its detached, token-matched directory.
All processes must use this protocol. Install it only when no slots are held.

Queue management invariants:
- The short-lived atomic queue lock (`build-slot-queue.lock`) protects queue operations. Waiters reclaim stale queue locks when the holding PID dies. Waiters also reclaim locks held by a live PID longer than 120 seconds. Reclamation renames the lock to a unique tombstone and checks the token before deletion. The lock owner records a unique token in metadata and confirms this token before removing the directory. This check stops an expired owner from deleting a successor lock. Critical sections inside the queue lock run no RAM probes, process waits, or sleeps.
- Queue read errors raise or retry on transient file access/sharing errors and JSON decode collisions; a missing file returns an empty list only on initial queue creation.
- Live queue entries retain their place for up to 30 minutes of token heartbeat silence. Dead PIDs are reclaimed regardless of heartbeat freshness.
- When an active `acquire` loop detects a missing queue entry during heartbeat validation, re-enqueue recovery preserves the original enqueue timestamp to protect FIFO fairness.
- Manual acquire holders expire after 30 minutes of heartbeat silence, or acquisition age if no heartbeat exists. A genuine live run wrapper never expires on age or heartbeat silence. Dead or recycled wrappers permit reclaim. This preserves the slot while a wrapped command runs.
- Contenders time out when a live process holds the queue lock for less than 120 seconds. Contenders safely reclaim locks held longer than 120 seconds through a unique tombstone directory rename.
- A queue-lock timeout is a transient fault, not a verdict on the waiter. `acquire` retries enqueue with jittered backoff until the caller's own `--timeout` ends, then returns `False`; it never raises and never drops the waiter's place. Queue cleanup after a slot is settled (post-acquire, release, abort) retries for a grace period (8 s, shorter than the release deadline). If it still fails, `clean_queue` sweeps the entry, because its PID is dead or its token holds a slot. Waiters poll the queue without the lock and take it only when a write is needed, so 30 pollers do not starve the writers. Lock polling uses jittered, growing delays. The queue-lock owner retries deleting its lock dir (waiters hold `info.json` open, and Windows refuses to delete an open file), so a release never leaves a lock behind for as long as a long-lived `run` wrapper lives. A queue lock is held for milliseconds and never across a wrapped command. A lock held longer than 120 s, even by a live PID, was leaked and is reclaimed. Each queue-lock attempt is capped at the time left to the caller's `--timeout`; cleanup after a grant or a give-up retries for at most 1 s, and `clean_queue` sweeps any residue.

## Consequences
- Lanes call the script directly and never ask the orchestrator for a slot.
- A candidate that moves the lock behind a network service must show what varies across that seam. One adapter is a hypothetical seam.

Evidence: [`workflows/portable/build_slot.py`](../../workflows/portable/build_slot.py) (module docstring).
