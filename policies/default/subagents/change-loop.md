<!-- change-loop:begin -->
Change loop (Wladefant/super-board#493, profile policy section 14): before you edit, read the repo's `Project stage:` line in AGENTS.md or CLAUDE.md. A missing line means `live`.
Greenfield: no shims, aliases or data migrations; replace the code and update every caller in the same change; never keep old code "just in case". Live: keep compatibility, migrations and backfills.
Order for each change: docs first, tests seen failing, the smallest implementation, then delete what the change made unused (ask when unsure).
In either stage, never delete, truncate, overwrite or reset a database or any data without asking first. Asking means you stop and put the question to the operator before the command runs; a backup does not replace the question. In a non-interactive run, change nothing and end with the question.
Before you say done: run the tests and read the output.
End your final message with `Deleted:` (a list or `none`) and `Not run:` (skipped checks or `none`).
<!-- change-loop:end -->
