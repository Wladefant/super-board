# Bug hunting — name the class, then sweep it

Hard-won on 2026-08-26 against
[Bavariance/polysimulator](https://github.com/Bavariance/polysimulator).
[`github-ops.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/github-ops.md)
is how to harvest, review, close, and lane. This file is the complement:
how to FIND bugs, not how to file them. Organised by the shape an agent
can search for, not as a dump of today's tickets. Full clickable URLs
only — never a bare `#N` or a SHA carried forward from a summary.

Lane agents load this file when they fix a defect, review a fix, write a
test, or are told to hunt a class. A confirmed instance with a decisive
check beats a long list of greps. Every candidate is judged against the
real code before it is called a bug.

## The meta-lesson

When you fix a bug, **name its class and hunt the class before moving
on.** One fix is a fix; one class swept is a real improvement. Today's
defects were not ten unrelated accidents — they were ten shapes, and
each shape almost certainly had more instances in the same tree. An
agent that closes the ticket it was handed and walks away leaves the
siblings live.

The hunt is not "grep until tired." It is: write the one-line signature,
walk every candidate, run the decisive check, and keep only what the
check confirms. Quality over volume.

## Review heuristics that actually caught things

These generalise. They are how three separate "fixes" today each traded
one hole for another, and how a green result hid a still-broken path.

1. **Attack the fix's NEW shape**, rather than only confirming the old
   bug is gone. The old hole is the one the author already thought
   about. The new hole is the one the patch just cut: a cache that now
   skips a filter, a count that is no longer recomputed, a cursor that
   now advances on a different path. Three separate fixes today each
   closed the reported defect and opened a sibling of a different class.
2. **Ask what a green result would look like if the code were still
   broken.** A test that cannot fail, a probe that treats "no search
   hit" as success, a concurrency test that was green on the pre-fix
   head — those are class 5, not evidence. If the broken behaviour
   would still produce the same green, the result is worthless.
3. **Treat a claim in a PR body as a hypothesis to verify, not a fact.**
   "This no longer blocks the loop", "the overlay now runs on the cache
   hit", "the trigger does not exist" — re-resolve against the code,
   the live head SHA, and a decisive check. A SHA carried in a summary
   can turn out not to be a commit at all; a sentence in a PR body can
   turn out not to describe the diff.

## The ten classes

For each class: the one-line signature an agent can search for, why it
happens, the observable consequence, the decisive check that confirms or
refutes an instance, and a real example from 2026-08-26.

### 1. Blocking work on the async event loop

**Signature.** A sync DB or Redis call reached from an `async def` with
no `asyncio.to_thread` (and no other off-loop executor).

**Why.** SQLAlchemy 2.0 in this stack is sync. Redis client calls are
sync unless the code explicitly uses the async client. An `async def`
that looks concurrent still monopolises the one event loop for the
duration of every blocking call.

**Consequence.** The whole loop freezes — health checks, websockets,
every other request on that worker. AGENTS.md §5 documents a real
incident. Observed monopolisation was on the order of ~30s.

**Decisive check.** Quantify the block against the real pool and
statement timeouts. A call that holds the loop for longer than
`DB_STATEMENT_TIMEOUT_MS` (or that saturates `DB_POOL_SIZE` while other
async work waits) is confirmed. A sync call that is already inside
`asyncio.to_thread` / a worker thread is refuted. Timing the function
in isolation is not enough; measure it on the loop, with the pool the
process actually uses.

**Example.** Search-lifecycle cache path
([PR #3180](https://github.com/Bavariance/polysimulator/pull/3180));
`refresh_market_list_cache`
([PR #3210](https://github.com/Bavariance/polysimulator/pull/3210)).

### 2. A cache-hit path that skips a correctness filter the compute path applies

**Signature.** SSR, Redis, or a legacy blob returns an already-filtered
body, and a later overlay / ACL / lifecycle filter runs only on the
compute (miss) path.

**Why.** The filter was added to the function that builds the payload.
The cache was taught to store that payload, then a second filter (or a
replacement for the first) was added on the way out — but only after
the miss. The hit returns the stored body and never re-enters the new
code.

**Consequence.** Stale or forbidden rows leak through the fast path.
Users see closed events on Active search, or an overlay that the miss
path would have dropped.

**Decisive check.** Force a hit and a miss for the same key and diff
the bodies after every overlay. If the hit is missing a filter the miss
applies, the instance is confirmed. Reading only the miss path, or
only the cache writer, is not a check.

**Example.** The
[#3004](https://github.com/Bavariance/polysimulator/issues/3004) leak:
SSR and legacy caches returned an already-filtered body before the
overlay ran.

### 3. A derived flag or count left stale after its source collection is rewritten

**Signature.** `has_x`, `*_count`, `total`, `length` (or any boolean /
integer derived from a collection) is computed, then the collection is
filtered / sliced / rewritten, and the derived value is not recomputed.

**Why.** The flag was true of the pre-filter collection. The write that
drops members does not know the flag exists, or the count is captured
for a pagination envelope before the window is applied.

**Consequence.** UI chrome and API envelopes lie: a sports tab appears
with no sports rows; `market_count` outlives the members; `total`
counts events the page no longer returns.

**Decisive check.** Mutate the source collection (drop the last
matching member, drop an event, shrink the window) and assert the
derived value changed. If it is still the old number or still `true`,
confirmed. If the value is recomputed from the post-rewrite collection
on every return path, refuted.

**Example.** `has_sports`
([#3191](https://github.com/Bavariance/polysimulator/issues/3191));
`market_count` after member drops; `total` after event drops.

### 4. A checkpoint or cursor advanced before the work it records is known to have succeeded

**Signature.** A cursor, watermark, `last_seen`, `sync_token`, or
Redis/DB checkpoint is written, THEN the work it records runs — or is
written inside a `try` that still commits on a later failure.

**Why.** At-least-once loops want to make progress. The easy way to
avoid redoing work is to move the cursor first. A crash or a failed
write after that skip is permanent.

**Consequence.** The skipped window never runs again. Settlements,
snapshots, or webhook side-effects vanish with no retry.

**Decisive check.** Fail the work after the cursor write (raise, kill
the process, roll back the payload write but not the cursor) and
re-run. If the failed item is not retried, confirmed. The cursor may
advance only after the payload write is known to have succeeded — same
transaction, or a write that is itself conditional on success.

**Example.**
[#3205](https://github.com/Bavariance/polysimulator/issues/3205).

### 5. A test that pins a false premise, or that passes against pre-fix code

**Signature.** A test asserts a trigger / column / branch / behaviour
"does not exist" when it does; a concurrency or frontend test that is
byte-identical across the broken head and the fix; a probe whose pass
condition is the absence of a search hit.

**Why.** The test was written from a comment, a PR body, or a mental
model of the schema, not from the live object. Or it was added to lock
the fix and happened to be green on the parent commit too.

**Consequence.** CI is green while the bug is live. A later regression
cannot turn it red. Reviewers treat the green as proof.

**Decisive check.** Break the behaviour and see if the test still
passes. Checkout the pre-fix head and run it; or invert the assertion
target (drop the trigger, keep the leak, swap the overlay off) and
watch. If it is still green, the test is not a test. A test that fails
on the broken head and passes only on the fix is the only kind that
counts.

**Example.** `test_tier_api_wallet_baseline.py` asserting a trigger
does not exist when it does; a concurrency test that passed on the
pre-fix head; frontend tests identical across heads.

### 6. Two intervals or two instants that are assumed equal but are not

**Signature.** A "window" compared to a "snapshot"; a label timestamp
compared to a write timestamp; an insert time compared to a compute
time — treated as the same instant or the same interval.

**Why.** The names rhyme (`window_end`, `snapshot_at`, `created_at`,
`computed_at`) so the code uses one where the other is required. Clocks
move; batch jobs straddle boundaries; a label is printed before the
row is committed.

**Consequence.** Rows fall out of the window they belong in, or land in
the next one. Four rounds of the same PR each closed one mismatch and
revealed the next pair.

**Decisive check.** Name both instants and prove they are the same one.
If you cannot write `instant_a is instant_b` with a shared clock
source, they are not equal — use the one the invariant actually names.
A test that freezes time and still uses two fields has not proved
equality; it has hidden the gap.

**Example.**
[PR #3201](https://github.com/Bavariance/polysimulator/pull/3201), four
rounds: fixed window versus snapshot, label versus write time, insert
versus compute time.

### 7. A dual-write paired with a database trigger that also writes

**Signature.** Application code updates `accounts.balance` and
`wallets.balance` (or any mirrored pair) in the same transaction, while
a trigger such as `trg_sync_account_to_wallet` already copies one onto
the other. SET, not ADD.

**Why.** The trigger was added so raw SQL and old call sites would stay
correct. New code learned the wallet table existed and started writing
it too. SET-to-computed-value rather than ADD-delta means a second
writer does not double — it clobbers.

**Consequence.** Drift is overwritten rather than doubled. A later
correction that "only updates wallets" is silently replaced by the
trigger, or a dual SET fights the trigger and the last writer wins.
The 2026-05-08 incident (user 10514) was the double-credit form of the
same shape.

**Decisive check.** Perform the application write in a transaction and
read both columns before commit and after. If both the trigger and the
application assigned the same column, confirmed. The fix is one writer:
either the application updates only the source column, or the trigger
is the only mirror — never both.

**Example.** `accounts.balance` / `api_balance` mirrored by
`trg_sync_account_to_wallet`.

### 8. Select-then-insert instead of an atomic upsert, under at-least-once delivery

**Signature.** `SELECT` to see if a row exists, then `INSERT` (or
INSERT then a second INSERT on a related table) on a path that a
webhook, retry, or inbox can run more than once.

**Why.** The happy path is one delivery. Stripe (and every other
webhook) is at-least-once. Two workers, or a retry after a timeout
where the first write actually landed, both pass the SELECT.

**Consequence.** Duplicate grants, duplicate top-ups, unique-violation
500s that look like "Stripe is broken", or a silently skipped second
delivery that needed to be idempotent.

**Decisive check.** Deliver the same payload twice, overlapping in
time. If the second delivery inserts a second row, errors, or skips
required side-effects, confirmed. An `INSERT … ON CONFLICT` (or
equivalent) that makes the second delivery a no-op with the same
observable result is the refutation.

**Example.**
[PR #3211](https://github.com/Bavariance/polysimulator/pull/3211).

### 9. An env var added to `.env.defaults*` but missing from a compose `environment:` block

**Signature.** A new `FOO=` in `.env.defaults` / `.env.defaults.staging`
/ `.env.defaults.prod` that does not also appear in every relevant
compose YAML `environment:` list.

**Why.** `env_file:` loads vars into Compose interpolation. The
container only receives what `environment:` names. Adding the default
file feels like shipping the var. It is not.

**Consequence.** The var is silently dropped. The process runs on the
code default or on empty, and staging "has the config" in git while
production does not. AGENTS.md §5; incident PR
[#984](https://github.com/Bavariance/polysimulator/pull/984) → fix
[#986](https://github.com/Bavariance/polysimulator/pull/986).

**Decisive check.** After deploy (or in a local compose config dump),
`docker exec <container> env | grep <VAR>` — or the equivalent
`docker compose config` interpolation. Presence in `.env.defaults*` is
not evidence. Absence from every `environment:` block is confirmation.

**Example.** The class itself is the 2026-08-26 lesson; the historical
incident is
[PR #984](https://github.com/Bavariance/polysimulator/pull/984) /
[PR #986](https://github.com/Bavariance/polysimulator/pull/986).

### 10. A closing keyword on a PR that only partly satisfies its issue

**Signature.** PR body or commit message contains `Closes` / `Fixes` /
`Resolves #N` (or `fix(#N)`) while any acceptance-criterion checkbox on
the issue is unmet.

**Why.** Templates default to a closing keyword. Agents copy
`Resolves #<N>`. GitHub fires the close on merge regardless of how
much of the issue landed.

**Consequence.** Live work disappears from the board. A half-fixed
card is `Done`. Two PRs on 2026-08-26 would have silently closed
half-fixed issues.

**Decisive check.** Diff the PR against **every** AC checkbox. If any
AC is unmet, the keyword is the bug — replace it with `Part of` plus
the full issue URL (e.g.
`Part of https://github.com/Bavariance/polysimulator/issues/3004`)
before merge. Process contract:
[`github-ops.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/github-ops.md)
§ Closing an issue.

**Example.** The closing-keyword trap recorded in
[`github-ops.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/github-ops.md)
from the same day; the Superboard PR template historically used
`Resolves #<N>`.

## How to run a hunt

1. Name the class in the PR or the issue comment — one of the ten, or
   a new named shape if none fit. A nameless fix is how siblings
   survive.
2. Write the signature as a search you can actually run (a call shape,
   a derived field next to a rewrite, a SELECT followed by an INSERT).
3. Walk every candidate against the real code. Discard greps that are
   not the shape.
4. Run the decisive check. Keep only what it confirms.
5. File or fix the confirmed instances; do not wait for a second day
   to rediscover them.
