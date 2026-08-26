# Version reconciliation

Four sources claimed to say what version this repository is, and they disagreed.
This document records what each one said, decides which is the current release
and why, and states the rule that produced the next number. The executable form
of that rule is `scripts/super_board_runtime/release.py`; where the two ever
disagree, the module wins and this file is the bug.

## What each source said

All four were read at commit
`2c98f475edc37c183094a2d79341e7e5a6d950c2` (the head of
`feat/superboard-runtime-hardening` immediately before this reconciliation).

| Source | Ref read | Declared value |
| --- | --- | --- |
| `VERSION` | working tree at `2c98f475edc37c183094a2d79341e7e5a6d950c2` | `1.7.1` |
| `skills/super-board/VERSION` | working tree at `2c98f475edc37c183094a2d79341e7e5a6d950c2` | `1.6.0` |
| `RELEASE-NOTES.md` newest heading | working tree at `2c98f475edc37c183094a2d79341e7e5a6d950c2` | `v1.7.1` |
| Only published Git tag | `refs/tags/v1.2.0`, tagged 2026-05-24 | `v1.2.0` |

## Which one is the current release

**`1.7.1`.**

`VERSION` and the newest `RELEASE-NOTES.md` heading are both updated as part of
cutting a release, so together they are the claim about what the code *is*, and
they agree. The release notes are also corroborated by the merged history: the
`v1.7.1` entry describes the `JSON.parse` fix at the wave-launch args guard, the
`v1.7.0` entry describes the model-tier ladder, and the `v1.6.0` entry describes
the backend flip — each of those changes is present in the tree. There is no
release-notes entry describing work that never landed, and no landed feature
after `v1.7.1` that carries its own heading.

Reconciliation refuses to guess: if those two content sources had disagreed,
`reconcile_current_release` raises `release-content-sources-disagree` rather
than picking the larger number.

## Why `skills/super-board/VERSION` lagged

It said `1.6.0` because it was last touched when v1.6.0 was cut and then never
bumped again. It is a **mirror**, not an independent claim — nothing reads it to
decide behaviour — so a stale value there is evidence of a missed bump, not of
an older release. Two releases went out with the mirror wrong and nothing caught
it, because nothing was checking.

**From this release on the skill version is pinned to the root version**, and
`tests/test_version_identity.py` fails the suite if the two ever differ. That is
the whole fix: not a policy, a test.

## Why the tag history stops at v1.2.0

`v1.2.0` is the only published tag. Releases v1.3.0 through v1.7.1 were cut in
the tree — `VERSION` bumped, release notes written — but never tagged, so there
is no immutable ref pointing at the commit each one shipped from.

**Those releases are declared explicitly untagged. They are not retro-tagged.**
Creating `v1.5.0` today would attach a tag to whichever commit somebody guessed
was right, and a tag that points at the wrong commit is worse than a missing
one: a missing tag is visibly missing, while a wrong tag answers "what shipped?"
confidently and incorrectly. The gap is recorded here instead, which is the
honest artefact.

Tagging resumes with this release, under its own approval gate.

## The rule that produced the next number

> The next release is a **major** bump above the reconciled current release when
> it changes a documented contract in a backward-incompatible way, a **patch**
> bump when it only fixes defects — restoring behaviour an earlier release
> already promised, with no new contract and no new surface — and a **minor**
> bump otherwise.

Executable as
`derive_next_release(current, backward_incompatible=..., defect_fix_only=...)`;
all three branches are tested. The two flags cannot both be true: a change that
breaks a documented contract is not a defect fix however it was discovered, and
allowing the combination would silently take the smaller bump.

This release is backward-incompatible in four separate ways:

1. **Dispatch eligibility** — `Ready` is now the only dispatchable status, and an
   excluded label fails closed. Boards that relied on other columns dispatching
   will stop dispatching.
2. **Merge behaviour** — the runtime no longer merges anything, under any
   configuration. `human_approves_merge: false` is refused, and `merge_method`
   must be `rebase`. Configurations that auto-merged will not load.
3. **The status contract** — the lifecycle is exactly seven states and is not
   configurable. `Skipped` is refused rather than mapped, so a board that still
   carries it must be reconciled before the runtime will act on it.
4. **The configuration schema** — the immutable GraphQL reserve, the activation
   mode, and the branch-route declaration are now required contracts; the old
   defaults no longer validate.

Reconciled current release `1.7.1` + backward-incompatible ⇒ **`2.0.0`**.

## The release after that: `2.0.1`

`2.0.0` shipped. Its own safety proofs were then run against a real installation
rather than against the repository they shipped from, and three defects came
back:

1. The installed payload instructed QA workers to build a merge-eligibility
   label and a merge-on-green gate — a concept the same payload forbids.
2. `scan_merge_prohibitions` could not report clean on an installed tree,
   because its only exclusion mechanism was a repository-root file that is not
   part of the payload. The gate existed in the repository and evaporated on
   installation.
3. The planner capped its board scan at 500 items and never said so, on a board
   holding 591.

All three restore behaviour `2.0.0` already promised. No documented contract
changes, nothing new to adopt, no migration. Reading the sources the same way:

| Source | Declared value |
| --- | --- |
| `VERSION` | `2.0.0` |
| `skills/super-board/VERSION` | `2.0.0` (pinned since the reconciliation, and asserted equal) |
| `RELEASE-NOTES.md` newest heading | `v2.0.0` |
| Only published Git tag | `v1.2.0` — `2.0.0` was never tagged, so the tag still does not vote |

The content sources agree, so the reconciled current release is `2.0.0`.

Reconciled current release `2.0.0` + defect-fixes-only ⇒ **`2.0.1`**.

`2.0.1` is likewise not tagged here; publication stays behind
`authorize_release_publication`.

## The release after that: `2.1.0`

`2.0.1` shipped. A live day against
[Bavariance/polysimulator](https://github.com/Bavariance/polysimulator)
produced an operator contract the runtime did not yet name: three GitHub
comment surfaces, the self-approval 422, the closing-keyword trap, SHA
re-resolution, nested-spawn death, and Done-column evidence. That is a new
surface for agents to follow, not a defect restore and not a break of a 2.0
runtime contract.

| Source | Declared value |
| --- | --- |
| `VERSION` | `2.0.1` |
| `skills/super-board/VERSION` | `2.0.1` |
| `RELEASE-NOTES.md` newest heading | `v2.0.1` |
| Only published Git tag | `v1.2.0` — still does not vote |

The content sources agree, so the reconciled current release is `2.0.1`.

Reconciled current release `2.0.1` + compatible new contract ⇒ **`2.1.0`**.

`2.1.0` is likewise not tagged here; publication stays behind
`authorize_release_publication`.

## The release after that: `2.2.0`

`2.1.0` shipped. The same live day against
[Bavariance/polysimulator](https://github.com/Bavariance/polysimulator)
produced a second operator contract the runtime did not yet name: ten
bug-hunting classes, the meta-lesson that a named class is swept before
the next ticket, and three review heuristics that caught sibling holes
in "fixed" PRs. That is a new surface for agents to follow, not a
defect restore and not a break of a 2.0 runtime contract. Process
discipline stayed in `github-ops.md`; finding bugs is the complement.

| Source | Declared value |
| --- | --- |
| `VERSION` | `2.1.0` |
| `skills/super-board/VERSION` | `2.1.0` |
| `RELEASE-NOTES.md` newest heading | `v2.1.0` |
| Only published Git tag | `v1.2.0` — still does not vote |

The content sources agree, so the reconciled current release is `2.1.0`.

Reconciled current release `2.1.0` + compatible new contract ⇒ **`2.2.0`**.

`2.2.0` is likewise not tagged here; publication stays behind
`authorize_release_publication`.

## What is now enforced

- `VERSION`, `skills/super-board/VERSION`, and the newest `RELEASE-NOTES.md`
  heading must be byte-identical after stripping a leading `v`.
- Active guidance may not advertise anything this release retired — `Skipped`,
  squash merging, runtime merging, a 200-point reserve, or `workflow` as the
  default backend. `scan_retired_release_claims` fails the suite if one comes
  back.
- Tagging and publishing are outward-facing and sit behind
  `authorize_release_publication`, which refuses without an explicit operator
  approval. Nothing in the runtime creates a tag by itself.
