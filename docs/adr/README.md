# Architecture decision records

Each file records one decision that later work must not re-open without new evidence. The `improve-codebase-architecture` skill reads this directory and skips a candidate that contradicts an ADR, unless the friction justifies reopening it (the skill marks such a candidate "ADR conflict").

Format: `NNNN-short-title.md` with Status, Context, Decision, Consequences. Keep each to one screen. Add a record when a candidate is rejected for a load-bearing reason; skip ephemeral reasons. Domain nouns come from [`GLOSSARY.md`](../../GLOSSARY.md).

| ADR | Decision |
| --- | --- |
| [0001](0001-github-is-source-of-truth.md) | GitHub is work truth; the request ledger is a cache |
| [0002](0002-coordinator-never-merges-or-self-spawns.md) | The coordinator and reviewers never merge, deploy or spawn themselves |
| [0003](0003-depth-survey-is-report-only.md) | The depth survey only reports; a person picks the candidate |
| [0004](0004-build-slot-is-a-local-directory-lock.md) | The build slot is a local atomic directory lock with a FIFO queue |
