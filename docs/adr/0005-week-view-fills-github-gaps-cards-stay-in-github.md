# 0005. The Week view fills GitHub's gaps; cards stay in GitHub

Status: accepted

## Context
GitHub Projects (v2) has three layouts: Table, Board and Roadmap. It has no calendar layout.

A live check of the boards on 2026-10-06 ([#483](https://github.com/Wladefant/super-board/issues/483)) covered 14 user Projects plus Bavariance Project 1. Roadmap views exist on Projects 2, 3, 5 and 7 and on Bavariance 1. 9 of the 15 boards have no date or iteration field, so a Roadmap cannot place their cards. The Roadmap zoom levels are Month, Quarter and Year. Date fields hold a day, not a time.

## Decision
The hosted Week view builds only what GitHub cannot show:

| Gap | GitHub covers it | Week view fills it | Reason |
| --- | --- | --- | --- |
| Time of day (hour blocks) | no | yes | Roadmap zoom is Month, Quarter or Year; date fields hold a day |
| Agent sessions and lanes (model, hours, first message) | no | yes | Projects hold issues and PRs, not sessions |
| Hours per project, parallel time counted once | no | yes | A Number field can sum, but cannot merge overlapping time |
| Sessions stopped with no commit | no | yes | No session data in GitHub |
| One view across boards and owners | no | yes | Each Project has one owner and its own fields and views |
| Weekly report text | no | yes | Insights shows charts, not text |
| Weekly report pushed to Telegram | no | yes, [#632](https://github.com/Wladefant/super-board/issues/632) | GitHub cannot send to Telegram |
| Open as a Telegram Mini App with the existing auth | no | yes | 12 of 14 user Projects are private and need a GitHub sign-in |

GitHub keeps card status, assignees, milestones, sub-issue progress, per-board Roadmap and Iteration planning, and every card edit.

Rule: in v1 the Week view is read-only for cards. It never writes to GitHub. Each card links to its GitHub item ("Open on GitHub"), and edits happen there.

## Consequences
- A request to edit, move or re-date a card in the Week view is out of scope for v1. Point it at the GitHub item.
- Do not rebuild a Table, Board or Roadmap view in the Week view. Only a new gap that GitHub does not cover reopens this.
- The Week view reads boards through the GitHub reader behind the quota guard, and the daemon sends no mutation.

Evidence: [#483](https://github.com/Wladefant/super-board/issues/483) (live board check and screenshots), [`week/week.js`](../../packages/telegram-agent-harness/week/week.js) (`Open on GitHub` link), [`daemon/week-github.ts`](../../packages/telegram-agent-harness/daemon/week-github.ts) (read-only reader).
