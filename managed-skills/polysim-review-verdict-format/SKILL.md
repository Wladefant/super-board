---
name: polysim-review-verdict-format
description: "Use when a review lane posts an APPROVE/REQUEST-CHANGES verdict on a Bavariance/polysimulator PR, so the Verify review freshness gate counts it (PR review, not issue comment; when to omit delta-from)"
---

# Posting a PolySimulator review verdict the gate counts

The `Verify review freshness` gate (`.github/workflows/review-freshness.yml`, `scripts/check_review_freshness.py`, `scripts/review_content.py`) reads verdicts **only from PR reviews** (`GET /repos/{o}/{r}/pulls/{pr}/reviews`). It ignores issue comments and fails closed.

## Post as a PR review
```
gh pr review <N> -R Bavariance/polysimulator --comment -b "$BODY"
```
The first non-empty line of the body is the verdict:
```
APPROVE <full 40-hex head sha>
reviewed-sha: <full 40-hex head sha>
<reasoning>
```
(or `REQUEST-CHANGES <sha>`). Resolve the head with `gh pr view <N> --json headRefOid` right before posting, and never copy a SHA from memory.

## When to include `delta-from:`
- Include `delta-from: <sha>` ONLY when `<sha>` already has an **APPROVE** review. That makes it a valid delta chain.
- After a REQUEST-CHANGES, post a **full** approval with NO `delta-from`. A delta-from that points at a rejected commit fails with the misleading message "diff changed since <sha>, delta review required", even when the content identity matches.

## After posting
Re-run the check with `gh run rerun <run-id>` (the workflow triggers on `pull_request_review`, but re-running confirms it). Report the review URL and the check conclusion.

Seen on 2026-09-27: #5589 and #5596 (bad delta-from), #5685 and #5682 (issue comments).
