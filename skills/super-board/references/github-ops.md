# GitHub ops — harvest, review, close, lane, board

Hard-won on 2026-08-26 against [Bavariance/polysimulator](https://github.com/Bavariance/polysimulator).
Organised by the moment an agent needs the rule, not as a dump. Full clickable
URLs only — never a bare `#N` or a SHA carried forward from a summary.

Lane agents load this file when they harvest bot findings, post a review, write
a closing keyword, dispatch a nested worker, or move a card. The executable
comment-sweep is `scripts/super-board-sweep-comments.mjs`; if that script and
this file ever disagree, the script is the bug.

## Harvesting bot findings

Codex posts as [`chatgpt-codex-connector[bot]`](https://github.com/apps/chatgpt-codex-connector).
Copilot posts as [`copilot-pull-request-reviewer[bot]`](https://github.com/apps/copilot-pull-request-reviewer).
They typically arrive 5–15 minutes after a push. CodeRabbit skips non-default
bases.

Findings land on **three surfaces**. Fetching only issue comments misses most of
them, which is how
[PR #3099](https://github.com/Bavariance/polysimulator/pull/3099) merged with
unaddressed findings:

| Surface | REST | What it holds |
|---|---|---|
| Issue / PR conversation comments | `GET /repos/{owner}/{repo}/issues/{n}/comments` (or the repo-wide `…/issues/comments`) | Timeline chatter. Easy to fetch. Incomplete. |
| Inline review comments | `GET /repos/{owner}/{repo}/pulls/{n}/comments` (or the repo-wide `…/pulls/comments`) | Line-anchored threads. |
| PR review objects | `GET /repos/{owner}/{repo}/pulls/{n}/reviews` | The review itself — APPROVE / COMMENT / CHANGES_REQUESTED plus the summary body. **Not** an issue comment and **not** an inline comment. |

A harvest that does not hit all three is not a harvest. Bot findings are
implemented **first**, before any independent review. A merged PR's unaddressed
findings are live trunk defects: open a new branch, do not pretend the merge
cleared them.

Both bots can be quota-exhausted at once. Observed strings: Copilot "reached
their quota limit"; Codex "usage limits for code reviews"; CodeRabbit silent on
a non-default base. **Silence from an exhausted bot is not approval.** Wait, or
treat the review as missing. The local Codex fleet in
[`super-review`](https://github.com/Wladefant/super-board/blob/main/skills/super-review/SKILL.md)
is the binding gate; the GitHub bots are inbound findings, not a green light.

## Posting a review

GitHub returns HTTP 422 `Can not approve your own pull request` and also
refuses `REQUEST_CHANGES` on a PR you opened. Self-approval is impossible.
Self-requesting-changes is also impossible.

Post the review as `COMMENT` (`gh pr review --comment`, or REST
`event: COMMENT`) and put the verdict in the body. Tell the operator that a
**non-author** must click Approve. Do not retry APPROVE or REQUEST_CHANGES
hoping the 422 was transient.

Before recording any approval, SHA, or "merge-ready" claim, resolve the
authoritative full head over REST:

```bash
gh api repos/{owner}/{repo}/pulls/{n} --jq .head.sha
```

A SHA carried forward in a summary can turn out not to be a commit at all.
Re-resolve during long runs — heads move. The exact-SHA QA contract in
`scripts/super_board_runtime/qa.py` already refuses a moved head; the same
discipline applies to every handoff comment and every review body.

## Closing an issue

A PR body that says `Closes #N` (or `Fixes` / `Resolves`) closes the issue on
merge even when the PR implements only part of it. Two PRs on 2026-08-26 would
have silently closed half-fixed issues.

Before allowing a closing keyword:

1. Diff the PR against **every** acceptance-criterion checkbox on the issue.
2. If any AC is unmet, do **not** use a closing keyword. Write `Part of`
   plus the full issue URL, e.g.
   `Part of https://github.com/Bavariance/polysimulator/issues/3004`.
3. The Superboard PR template in `run.md` historically used `Resolves #<N>`.
   That is a closing keyword. Use it only when the diff satisfies every AC;
   otherwise replace it with `Part of` plus the full URL.

Never close an issue as a substitute for a merge. The runtime already forbids
that path; a closing keyword is the remaining way a partial PR can still
retire a card.

## Running lanes

Nested spawning is disabled in this harness (depth 0). A subagent that tries
to `task` / `spawn_subagent` dies with a preamble and no work. **State that
in every worker prompt.** Super-review adversarial mode still *describes*
spawning Code-grounder and Historian — when nested spawn is off, the reviewer
runs those checks itself rather than delegating.

Announce file ownership over IRC before editing a shared file. Give each
writer its own worktree. Session restarts kill in-flight lanes, so lanes
**push early** rather than saving everything for a final report. Cancelling a
lane mid-write loses finished-but-unrecorded governance work — the last
pushed commit is the resume floor (same contract as `references/stop.md`).

## Moving cards

Never move a card to `Done` on inference. Require the merged PR (full URL +
re-resolved merge commit SHA) or a closed issue whose closure evidence is
already on the card. The closure normalizer is the only writer of `Done`; a
dispatcher, builder, tester, reviewer, or workflow that moves a card there is
a contract break.

A card stuck in `Building` with no live branch and no open PR is a real
signal worth triaging — not a stale lock to ignore. Cross-check candidate
work against existing branches and open PRs before calling an issue
unclaimed.

A verification instrument can lie. A probe that exited 0 while the bug was
present did so because *absence of a search hit* was read as success; the
same commit had exited 1 ten minutes earlier. A verification that cannot
fail is worthless, and one that passes for the wrong reason is worse. Prove
a probe fails on today's broken state before trusting a future green.
