# Block exit template

> **Source of truth: this file.** It used to defer to spec
> `docs/superpowers/specs/2026-05-21-super-board-design.md` §4 "Cross-cutting:
> Block & Skip exits", which is missing from this fork — the §4 pointer is dead.
> See `SKILL.md` for what that changes.

`Blocked` sits AFTER `Done` on the board — it is not a workflow step, it is the exit ramp.

There is exactly one exit ramp. The lifecycle is fixed at seven statuses — Backlog · Ready · Building · QA · Review · Blocked · Done — and the parked status that used to sit beside `Blocked` was retired with the 2.0 lifecycle: `scripts/super_board_runtime/lifecycle.py` refuses it, and a config that names it as a lifecycle value exits 65. A card that is not actionable in this loop is therefore blocked with the 🤷 out-of-scope reason tag, not parked in a status that no longer exists.

## When / who moves cards there

| Column  | When                                                        | Who moves cards there              |
|---------|-------------------------------------------------------------|------------------------------------|
| Blocked | Card needs human action, or is not actionable in this loop   | Any lane, from any workflow column |

Once moved, the card is out of the loop. Human drags it back to `Ready` when unblocked.

## Required Block comment template (mandatory on every transition into Blocked)

The bot must write a structured comment on **both the issue and the PR** (if a PR exists) explaining *why* it moved the card and *what it couldn't safely decide*. Format:

```
🛡 super-board · <lane> · BLOCKED
─────────────────────────────────────
Card:        #<N> <title>
PR:          #<P> (if exists)
Reason tag:  <emoji from table below>
Why blocked: <concrete; 1 line — name the specific thing that is missing or wrong>
What blocks: <what specific external action would change this — credentials, perms, decisions>
Why I (bot) cannot decide:
             <one line explaining the decision the bot refuses to make on its own —
              "involves billing config; this is a customer money decision",
              "requires choosing between two valid auth providers; ambiguous from spec",
              "would drop a Postgres table; destructive, needs human sign-off">
To unblock:  <concrete action the human can take, in their own checklist form>
             [ ] <step 1>
             [ ] <step 2>
Move back:   drag this card to Ready after the steps above are done
```

An out-of-scope exit uses the same template and the same `BLOCKED` headline, carries the 🤷 reason tag, and answers `Why blocked` with why the card is out of scope for this loop and `What blocks` with the decision that would bring it back in.

## Reason emoji vocabulary

| Emoji | Class                       | Examples                                                                 |
|-------|-----------------------------|--------------------------------------------------------------------------|
| 🔐    | Credentials / secrets       | missing API key, expired token, no test login                            |
| 💳    | Billing / quota             | paid API rate-limit hit, free tier exhausted, requires plan upgrade      |
| 🔑    | Permissions / access        | gh scope denied, org admin required, write access missing                |
| ❓    | Ambiguity / spec gap        | two valid interpretations, AC contradicts PROJECT.md, dependency unclear |
| 🛡    | Safety / destructive        | would drop a table, would push to prod, would rotate live secrets        |
| 🧑    | Human review needed         | unresolved human PR comment, design decision, branding choice            |
| 🤷    | Out-of-scope                | wrong project, deferred to other milestone, manual-only ticket           |
| 📦    | Wrong-place                 | belongs on a different board / repo                                      |
| 🎨    | Pure design                 | no measurable AC; needs design pass first                                |

## Hard rule

**The bot is forbidden from moving any card to Blocked *without* this full template populated. A 1-line "needs creds" comment is a contract violation and fails Reviewer's thread gate.**
