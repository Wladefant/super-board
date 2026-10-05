# 0004. The build slot is a local atomic directory lock with a FIFO queue

Status: accepted

## Context
Parallel lanes ran `next build`, `next start` and dev Chromium at once and exhausted RAM. Slot hand-off by chat messages ("BUILD SLOT TAKEN/FREE") was lost when a lane died.

## Decision
Arbitrate the slot with `os.mkdir` on a lock directory plus a FIFO queue file with heartbeats. No `fcntl`, so it works on Windows. A stale lock (dead owner) is reclaimed with a logged notice. A live holder is never reclaimed. Release by a non-owner is refused.

## Consequences
- Lanes call the script directly and never ask the orchestrator for a slot.
- A candidate that moves the lock behind a network service adds a second adapter at a seam with one real caller; show what varies first.

Evidence: [`workflows/portable/build_slot.py`](../../workflows/portable/build_slot.py) (module docstring).
