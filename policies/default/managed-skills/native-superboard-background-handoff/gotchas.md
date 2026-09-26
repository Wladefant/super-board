# Gotchas: Native Background Handoff

Operational failure modes when recording native task handles and running continuation drivers.

## 1. Task Handle URI Rewriting by Shell/Harness
- **Symptom:** `worker_backend.py --record-native` fails to match running agent IDs or truncates handle paths.
- **Root Cause:** The execution environment or shell may attempt to interpret or resolve `agent://<id>` as a filesystem URL or protocol scheme when passed via unquoted arguments.
- **Fix:** Quote the task handle strictly in CLI arguments (e.g. `"--handle" "agent://<id>"`), or pass via JSON stdin / direct argv.

## 2. Prepared / Dispatched Does Not Mean Success
- **Trap:** Treating a ticket marked "prepared" or "dispatched" as completed work.
- **Rule:** A dispatched ticket remains strictly in `pending` status until an authentic structured JSON result with verified exit checks has been recorded via `--complete-native`.

## 3. Unauthenticated GitHub Proof vs API Proof
- **Trap:** Claiming private repository verification via anonymous HTTP curl or unauthenticated URL fetching.
- **Rule:** Verification evidence for private repositories requires authenticated GitHub API access (`gh` CLI or octokit) with proper OAuth tokens; public raw URLs fail with HTTP 404/403.

## 4. Fabricating Checks or Adjusting Exit Codes
- **Trap:** Altering a non-zero exit code to `0` in the result payload so that validation passes.
- **Rule:** Backend strictly rejects unexplained failing checks, but fabricating an exit code violates audit invariants. If a command was expected to fail, state `purpose: baseline` or `purpose: negative_control` with `expected_exit_code`.
