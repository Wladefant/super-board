---
name: ste-writing
description: "House style for operator-facing text (final answers, reports, Telegram messages, PR and issue text, lane handoffs): about 80% of the way to ASD-STE100 Simplified Technical English. Short sentences, one idea each, active voice, plain words, steps as numbered lists. Use together with unslop-writing before you send or post text a human must read quickly. Not for code, quoted errors, commit-message identity rules, or creative copy."
---

# STE Writing (80% house style)

Operator-facing text must be fast to read. Write it about 80% of the way to
ASD-STE100 (Simplified Technical English), the controlled English made for
aerospace maintenance manuals. Andrej Karpathy suggested this, and he said to
soften it because the full spec is strict.

Credit: adapted from [prithivrajmu/asd-ste100](https://github.com/prithivrajmu/asd-ste100)
(MIT, Copyright prithivrajmu), which started from
[danyuchn/asd-ste100-skill](https://github.com/danyuchn/asd-ste100-skill) (MIT).
Idea: [Karpathy, 2 Oct 2026](https://x.com/karpathy/status/2105819303471976479).
This file does not contain the ASD dictionary. ASD does not permit redistribution.
This style is not certified STE.

## Where it applies

Final answers, Telegram messages, status reports, PR and issue bodies and comments,
handoff comments, review verdicts, docs written for the operator.

It does not apply to: code, identifiers, commands, quoted error text, logs,
commit identity rules, or machine-parsed formats (JSON, ledger fields, QA-RECEIPT lines).

## Relation to unslop-writing

The two skills do different jobs. Apply both.

- `unslop-writing` removes content problems: puffery, hedging, filler, em dashes, emojis, pleasantries.
- `ste-writing` fixes sentence structure and word choice: length, voice, one idea, one word per meaning.
- Where both speak (active voice, plain verbs such as "use" for "utilize"), they agree.
- Channel rules from `unslop-writing` stay in force: Telegram HTML formatting, every issue/PR/commit mention is a full link, commit author identity.
- Order: write the facts, shorten to STE, then run the unslop scan.

## The 80% rules

1. One idea in each sentence.
2. Aim for 20 words or fewer. Never go above 25. In procedures use 20 words or fewer.
3. Use the active voice. Name who acts: "The gate rejects the PR", not "The PR was rejected".
4. Use simple tenses: present, past, future with "will". Keep a present perfect only when it adds meaning ("the job has finished", so the output is ready now).
5. Use the verb, not a noun made from it: "check the log", not "perform a check of the log".
6. Use one word for one thing. Do not switch to a synonym in the middle of a text (not "lane", then "worker", then "agent" for the same thing).
7. Use plain words: "use", "start", "stop", "before", "because", "to". Not "utilize", "commence", "prior to", "in order to".
8. Use a single verb, not a phrasal verb: "start", not "spin up". "Find", not "figure out".
9. No semicolons. Write two sentences.
10. Maximum 3 nouns in a row. Break a longer noun chain with "of", "for", or a verb.
11. No idioms or figures of speech ("smoking gun", "fire off", "land on").
12. Steps go in a numbered list, one instruction in each step, in the imperative.
13. Three or more parallel items go in a list or a table.
14. Paragraphs have 6 sentences or fewer. Put the most important point, the result or the decision needed, first.
15. Say the state in the first line: done, blocked, or needs a decision.

Keep technical terms, code names, paths, and contractions.

## Guard rails (these win over the rules above)

- Keep every fact: numbers, conditions, exceptions, SHAs, URLs, scope limits. Use two sentences, not a lost fact.
- Keep the confidence of the source. "May have failed" stays "may have failed". Mark inference as inference.
- Add no facts: no new cause, frequency, or mechanism.
- Keep code, identifiers, commands, and quoted errors unchanged.
- Do not announce the style. Do not list the rules you applied.
- Stop when it is clear. Clarity is the goal, not the shortest text.

## Strict mode (100%)

Use only when the operator asks for "strict STE" or the text is a safety-critical procedure
(data loss, security risk, outage). Then also: no "-ing" forms except technical nouns,
no contractions, keep articles, and start a risk step with WARNING or CAUTION, then the command, then the risk.

## Before and after

Before: "I wasn't able to determine yet why Main died again; it went down at 15:46 UTC with nothing in the logs, and this has now happened 13 times within the last week, always being terminated externally by Windows before our code could log anything."

After:
Main stopped again at 15:46 UTC. I do not know the cause yet. The logs show no error.
This is the 13th stop in 7 days. Each time, Windows ended the process from outside, before Veyyon could write a log entry.
Details: <full link to the issue comment>.

## Self-check before you send

1. Does line 1 say the state (done, blocked, needs a decision)?
2. Is any sentence longer than 25 words? Split it.
3. Is any sentence passive with a hidden actor? Name the actor.
4. Does one thing have two names? Pick one.
5. Is every fact, link, and hedge from the source still there?
