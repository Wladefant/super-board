# 0003. The depth survey only reports; a person picks the candidate

Status: accepted

## Context
Automatic refactors of "shallow" modules risk deleting code whose complexity reappears in its callers.

## Decision
The depth survey reads git history and source and never edits code. Every candidate carries a deletion-test result (`pass-through`, `concentrates` or `inconclusive`). The report is one offline HTML file attached to a GitHub issue. The skill stops after the report and asks which candidate to explore. Whether the survey may file issues on its own is an open operator decision, tracked in https://github.com/Wladefant/super-board/issues/514; until the operator decides, it does not.

## Consequences
- Scheduled survey runs are always report-only.
- A grilling pass starts only after a person picks one candidate.

Evidence: [`workflows/portable/depth_survey.py`](../../workflows/portable/depth_survey.py) (module docstring), [`skills/improve-codebase-architecture/SKILL.md`](../../skills/improve-codebase-architecture/SKILL.md) ("Modes").
