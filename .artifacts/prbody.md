Closes #459, #460, #461. Part of #458.

Adds the Flow QA runner, the FLOW-QA receipt gate and the managed skill `flow-qa`.

- `workflows/portable/flow_qa_runner.mjs`: drives declarative flows in Chromium with CDP touch at 390x844, 390x420 and 1440x900. Refuses production and a served sha that differs from the expected one. Prints the receipt lines the gate parses (`receipt.txt`).
- `github_pr_gate.py`: staging UI PRs need a `FLOW-QA: PASS <served sha>` receipt with `fail=0`, `pass>0` and both required viewports. A stale sha, a failure or a thin run is rejected. A later FAIL or RETRACTED overrides an earlier PASS.
- `managed-skills/flow-qa/SKILL.md`.

Local checks on head 53d1eb605bcef3f2018bf28ac62e52f78d8652d4:
- `node --test workflows/portable/test_flow_qa_runner.mjs`: 23 pass, 0 fail.
- `python -m pytest workflows/portable/test_github_pr_gate.py`: 60 passed. Includes a negative control for a stale-sha receipt.

Review: not required (tooling, no money/auth/migration path); merge on local checks per policy.
