# Worker rate-limit etiquette

The dispatcher's quota guard only protects the dispatcher's own ticks. Workers (Builder / Tester / Reviewer) run as independent `claude -p` sessions and **share the same token bucket** — one machine-account classic PAT. (A GitHub App would have a larger bucket and is still not an option: apps cannot access personal Projects v2.) The worker storm of 2026-05-21, recorded on [issue 381](https://github.com/Wladefant/super-board/issues/381), drained that bucket because nothing in the worker contract told workers to watch the quota. That incident is why this file exists.

The remedy has changed. The first answer to issue 381 was a threshold check that paused until the hour rolled over — and pausing is exactly what let a drained bucket look survivable: the workers that were waiting were still holding lanes, and the ones that woke up drained it again. The shipped runtime holds a reserve it never spends and **halts** instead.

This file is the worker-side contract. Every worker MUST follow it. The executable contract is [`scripts/super_board_runtime/quota.py`](https://github.com/Wladefant/super-board/blob/main/scripts/super_board_runtime/quota.py); where this file and that module disagree, the module wins and this file is the bug.

## 1. The reserve is a floor of 1,000 points, and it is immutable

- The runtime keeps a reserve of 1,000 GraphQL points that it will never spend.
- A config MAY **raise** the floor through `minimum_graphql_reserve`. It may never lower it — a configured value under the floor is a configuration error and exits 65. (The accounting clamps it back up as a second line of defence, so a hand-edited config cannot buy itself headroom.)
- Before any mutation, `remaining - estimated_cost >= effective_floor` must hold.
- Reaching the reserve stops the worker with **exit 75**. So does a quota that cannot be read: an unreadable or malformed quota response counts as exhausted, never as "probably fine". Nothing fails open, and there is no fabricated fallback capacity.

## 2. Source the guard at worker start

```bash
source scripts/super-board-gh-guard.sh
sb_gh_budget_init 150      # per-worker soft cap on gh calls
sb_gh_guard_check 103      # ESTIMATED COST of the coming burst, in GraphQL points
sb_gh_guard_summary        # log the safe quota fields for the run manifest
```

**The numeric argument changed meaning — read old call sites with care.** It used to be a threshold: *pause while the remaining balance sits under this number.* It is now the **estimated cost of the burst you are about to run**. A call site that still passes `200` keeps working, but it is no longer asking what its author meant: it now asks whether an estimated cost of that size still clears the floor. A ProjectsV2 item scan is ~103; that is the number to reach for when you are unsure, not a leftover threshold.

Estimate before you execute. `sb_gh_guard_check` refuses a cost it cannot afford; it cannot refuse a cost you never told it about.

## 3. One quota inventory per cycle — never re-fetch per operation

The runtime reads the quota **once** per cycle (`QuotaCycle.begin_cycle()`), and every check inside that cycle reuses the single reading. Workers do the same: source the guard, take one reading, spend against it. Polling the quota before every call turns the guard into the thing that drains the bucket.

When the reading says the next burst does not fit, that is the answer for the whole cycle. Do not re-read hoping for a different number.

## 4. Halting is the correct behaviour — never sleep, never retry-spin

- **Never sleep through a reset.** No worker waits for the hour to roll over; a worker that is waiting is a lane that is not free.
- **Never retry-spin.** The guard does not retry, and neither does the caller. One refusal ends the burst.
- On exit 75, write the halt comment ("quota reserve reached, releasing claim"), release the claim assignee, and exit. The orphan scan and the reaper recover the lock; the dispatcher re-tries on a later tick, when the quota has genuinely recovered.

Halting is not a failure to handle the problem. It *is* the handling: it is the only response that leaves budget for the dispatcher's next tick and for every other worker sharing the bucket.

## 5. Spend less: prefer built-in Project workflows and local git

- **Prefer GitHub's built-in Project workflows** for anything they can do — auto-add, item status defaults, closure. They run server-side at zero API cost. An API item-add spends from the same bucket as the work.
- **Skip the `gh project item-list` self-check re-query.** The self-check item "Card column move re-read and verified" once required `gh project item-list --limit 500` on every worker exit — a large GraphQL hit per worker per lane transition, for very little signal. Trust the column-move mutation's exit code instead. If `gh project item-edit ...` returned non-zero, estimate the retry and call `sb_gh_guard_check` with that estimate before the single retry. If it still fails, write the halt comment and exit; do not re-query the whole board.
- **Prefer `git blame` / `git log` (local, free) over `gh api graphql`** for anything that does not need fresh remote state.
- **Bounded batches.** No mutation batch carries more than 25 records, and pagination is capped. A batch that needs more than that is two operations, each estimated separately.

## 6. Adversarial mode — sub-agent gh-call cap

When `truth_gate` triggers adversarial mode, each sub-agent (Code-grounder, Historian) MUST stay within `SB_GH_GUARD_SUBAGENT_BUDGET` (default 50) gh calls. The Reviewer passes this budget to each sub-agent via prompt:

> Adversarial sub-agent budget: ≤50 gh calls total. Prefer `git blame` (local) over `gh api graphql` (remote). If you need more than 50 calls to reach a confidence score, return `confidence: "insufficient_data"` and let the Reviewer flag the card as 🛡 truth-check inconclusive — do NOT burn through quota.

## 7. A 403 or a secondary rate limit means stop, not wait

If a `gh` call returns 403, or a body containing `secondary rate limit`, the account is already over the line. Do not retry, and do not wait it out — treat it exactly like exit 75: write the halt comment, release the claim, exit. Whatever the burst was going to accomplish, the next tick can accomplish it with a quota that actually exists.

## 8. Log only the safe quota fields

Four fields are loggable, and only these four: **remaining points, estimated cost, effective floor, reset time.** Never a token, a header, a cookie, an account rate limit, or a raw API payload. `sb_gh_guard_summary` emits exactly the permitted set — use it rather than assembling your own line.

Every worker's PR handoff comment MUST end with:

```
gh-quota-on-exit: graphql=<remaining> floor=<effective-floor> reset=<time>
```

This gives the run manifest visibility into which lane is the heaviest consumer over time. It carries no capacity claim beyond what was actually read.

## 9. Per-worker hard cap

`sb_gh_budget_spend` decrements a per-worker call counter. Default 150. If exhausted, the helper returns non-zero — the worker writes a halt comment ("worker-budget exhausted, releasing claim") and exits gracefully. This cap is worker-local bookkeeping and sits *above* the shared reserve: clearing your own budget never entitles you to spend into the floor.

---

Pointer: the dispatcher-side guard lives in `super-board-run.sh::gh_rate_guard`, and both sides route their accounting through `super_board_runtime.quota` so the worker guard, the dispatcher, and the planner cannot disagree about what is affordable. The two together are defence in depth — the dispatcher stops before its next tick, the worker stops before its next burst, and neither of them waits for a reset.
