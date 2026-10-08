# 0004. The build slot is a local atomic directory lock with a FIFO queue

Status: accepted

## Context
Parallel lanes need exclusive use of `next build`, `next start` and dev Chromium. The slot used to be handed over by orchestrator chat messages ("BUILD SLOT TAKEN/FREE"), which the script replaces with a local, crash-resilient lock.

## Decision
Arbitrate the slot with `os.mkdir` on a lock directory plus a FIFO queue file with heartbeats. No `fcntl`, so it works on Windows. A stale lock (dead owner) is reclaimed with a logged notice. A live holder is never reclaimed. Release by a non-owner is refused.

Queue management invariants:
- The short-lived atomic queue lock (`build-slot-queue.lock`) protects queue operations. Stale queue locks are reclaimed only when the holding PID is dead; living PIDs are never reclaimed on age. The lock writer writes a unique ownership token in lock metadata and verifies the token matches before releasing the directory, ensuring a slow releaser never deletes a successor's lock directory.
- Queue read errors raise or retry on transient file access/sharing errors and JSON decode collisions; a missing file returns an empty list only on initial queue creation.
- Living PID queue entries are preserved despite delayed heartbeats; stale queue entry reclamation purges entries only when their PID is confirmed dead or when untracked legacy entries expire.
- When an active `acquire` loop detects a missing queue entry during heartbeat validation, re-enqueue recovery preserves the original enqueue timestamp to protect FIFO fairness.
- Slot reclamation also requires a dead owner PID. A delayed heartbeat never proves that a live build stopped.
- A contender times out if a live process holds the queue lock. The contender leaves that lock intact.

## Consequences
- Lanes call the script directly and never ask the orchestrator for a slot.
- A candidate that moves the lock behind a network service must show what varies across that seam. One adapter is a hypothetical seam.

Evidence: [`workflows/portable/build_slot.py`](../../workflows/portable/build_slot.py) (module docstring).
