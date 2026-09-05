# Real worker execution and continuous stage progression

Two modules: `worker_backend.py` durably prepares and finalizes native background
tasks (the default), or runs one explicitly selected agent CLI;
`continuation_driver.py` drives the existing adapter over explicit authorized
requests and stops cleanly while native work is in flight.
Neither replaces the coordinator or the adapter, and neither merges or deploys.

---

## 1. The problem these close

The portable core could evaluate a step but not actually execute one. Its
dispatch path fell through to a labelled fixture whenever a real command was not
wired up, so a step that never ran anything still reported
`simulated successful build` and the gate advanced the request. Between exit
status `0` and the phrase `proven absent`, a request could travel
`implementation → QA → review` without a single command having been run.

Separately, nothing drove the adapter more than once. "Autonomous start to
finish" meant a human re-invoking a one-shot command once per stage.

So: real execution with evidence that cannot be faked, and a loop that stops for
the right reasons.

---

## 2. `worker_backend.py`

### Contract

```python
from worker_backend import WorkerBackend, WorkerRequest

backend = WorkerBackend(state_dir=state_dir)
request = WorkerRequest(
    request_id="req-1234",
    stage="qa",                    # build | qa | review
    repo_root="/path/to/repo",
    head_sha="<40 hex>",
    model="openai-codex/gpt-5.6-sol:high",
    agent_role="qa-verifier",
    prompt="...",
    criteria=["..."],
    task_type="bug",
)
ticket = backend.prepare_native(request)
# The host launches ticket.prompt with ticket.result_schema as a BACKGROUND task.
ticket = backend.record_native_dispatch(ticket.run_id, "agent://actual-handle")
outcome = backend.complete_native(
    ticket.run_id,
    "agent://actual-handle",
    structured_result,
)
```

The exact native API is:

```python
prepare_native(request) -> NativeDispatchTicket
record_native_dispatch(run_id: str, task_handle: str) -> NativeDispatchTicket
complete_native(
    run_id: str,
    task_handle: str,
    structured_result: Mapping[str, Any],
) -> WorkerOutcome
get_native_dispatch(run_id: str) -> NativeDispatchTicket
get_native_outcome(run_id: str) -> Optional[WorkerOutcome]
```

Preparation validates the request, repository, and exact checked-out head before
persisting a ticket. It does not launch work and is not successful execution.
The host uses its normal non-blocking task tool between `prepare` and `record`;
the portable core never invokes an agent CLI. `record` rechecks the binding and
durably records the actual `agent://` handle. `complete` requires that same
handle, rereads HEAD, and uses the one shared result validator. Identical
completion retries return the persisted outcome; changed-result or changed-
handle replays are rejected.

Runnable host command sequence:

```bash
python worker_backend.py --prepare-native --request-id req-1234 --stage qa \
  --repo-root /path/to/repo --head-sha <40-hex> --agent-role qa-verifier --json
# Launch the returned prompt/schema with the native BACKGROUND task tool.
python worker_backend.py --record-native --run-id <run-id> \
  --task-handle agent://<actual-handle> --json
# After automatic task delivery, save its actual structured result as result.json.
python worker_backend.py --complete-native --run-id <run-id> \
  --task-handle agent://<actual-handle> --result-file result.json --json
```

The result must contain `stage`, `request_id`, observed full `head_sha`,
`verdict`, `summary`, actual non-empty executed `checks`, and `artifacts`. Bug QA
also requires the re-executed `reproduction` record. The host must never
synthesize checks or timestamps.

`WorkerOutcome` field names are a published contract:

| field | meaning |
| --- | --- |
| `ok` | the only success signal; false whenever anything below refuses |
| `stage` | stage that was dispatched |
| `exit_code` | `0` after validated native completion, the explicit CLI's exit status, or `None` before/after refused execution |
| `command` | exact explicit CLI argv, or `[]` for native background execution |
| `head_sha` | the **observed** git HEAD after the run, never the claimed one |
| `evidence` | structured record, described below |
| `artifacts` | repo-relative paths that were verified to exist |
| `blocked_reason` | why it refused, in operator-actionable terms |
| `backend_name` | which configured backend ran |

`evidence` carries backend-generated runtime timestamps, executor name, bound
task handle, stage, request id, observed heads, routing model, structured result,
verdict, artifact digests, executed checks, and merge/deploy denial. Native
evidence uses `dispatch_kind: "native_background"`; task-supplied metadata
cannot replace these observations.

### Fail-closed rules

Every one of these produces `ok=False` with a populated `blocked_reason`, and
never a synthetic success:

1. Malformed request — no `stage`, no `request_id`, no usable `repo_root`, or no
   full 40-character `head_sha`. A bad packet blocks; it does not raise into the
   caller's step loop.
2. `repo_root` is not an existing Git repository with a resolvable `HEAD`, the
   requested commit does not resolve in that repository, or the checkout is not
   exactly on that commit. These checks happen before backend resolution, run
   directory creation, or worker dispatch.
3. A native record is missing/corrupt, is not in the required transition state,
   has a changed task handle, or is replayed with a changed result.
4. Repository/head binding changes before dispatch registration.
5. An explicitly selected CLI backend is unknown, misconfigured, absent from
   `PATH`, exits non-zero, or times out.
6. No structured result, or one that is not a mapping/JSON object.
7. `is_error: true` in an explicit CLI envelope, even alongside exit `0`.
8. Any required result field missing.
9. A verdict outside `pass | fail | blocked`. An honest `fail` or `blocked` is
   reported as-is and does not advance anything.
10. A result whose `stage` or `request_id` does not match what was dispatched.
11. `verdict: "pass"` with an empty `checks` list, or a check with no command,
    no integer exit code, or nothing observed. **Exit status alone is never
    evidence.**
12. A claimed `head_sha` that differs from the observed HEAD. The observed
    commit always wins, and is what the outcome reports.
13. For `qa` and `review`: HEAD moved during the run, or the tested commit is not
    the dispatched one. A verification stage must not mutate the tree it judges.
14. For `build`: a `pass` that produced neither a new commit nor any artifact.
15. A declared artifact that does not exist on disk.
16. For a bug at `qa`: no `reproduction` record, or one lacking a real command,
    a real integer exit code, a real observation, a scenario string, or with
    `still_reproduces` not literally `false`.

### Bug reproduction is re-executed, not asserted

`evidence.reproduction.verdict` is **derived** here, never taken from the model.
It only becomes `"absent"` after the re-executed scenario has already passed
validation: a real command, a real exit code, a real observation, and
`still_reproduces: false`. A worker writing `"verdict": "absent"` or the words
`proven absent` into its own answer cannot reach that value.

### Head binding

Before dispatch, `repo_root` must be a Git repository whose `HEAD` and resolved
requested commit both equal the required full `head_sha`. `head_sha` in the
outcome is then observed again with `git rev-parse HEAD` in the directory the
command actually ran in; it is never copied from the worker's claim. A build
legitimately advances the head and the new commit becomes the reported one;
`qa` and `review` must leave it exactly where they found it.

---

## 3. Native default and explicit CLI model translation

Native execution is the default for `build`, `qa`, and `review`. It receives the
routing model unchanged in `WorkerRequest.model`; the host owns native model
selection. The portable core does not import Veyyon and does not spawn a CLI.

Model translation applies only after an external backend is explicitly selected
with `WorkerRequest.backend`, `WorkerBackend(default_backend=...)`, or config
`default_backend` / `stage_backends`.

Each CLI backend declares a `model_map`. Resolution is:

- request names no model → the backend's `model_default`
- id present in `model_map` → the explicit mapped CLI name
- a bare name with no `/` and no `:` → passed through
- a harness-qualified id absent from a strict backend's map → **blocked**
- a harness-qualified id absent from a non-strict backend's map → passed
  through unchanged and recorded

---

## 4. Executors

| executor | selection | invocation |
| --- | --- | --- |
| `native` | implicit default | durable prepare → host background task → record handle → complete |
| `claude` | explicit | `claude -p ... --json-schema ...` |
| `claude-verify` | explicit | same CLI with verification tool grants |
| `codex` | explicit | `codex exec ... --output-schema ...` |
| `veyyon` | explicit | `veyyon -p --mode=json ...` |

The built-in external definitions are conveniences, not defaults. Select one
explicitly in configuration or on `WorkerRequest.backend`. Choosing a CLI never
changes the shared result validation, head binding, artifact verification, or
bug-reproduction rules.

### Permission modes, as measured

Neither documented Claude Code mode is sufficient alone, and both failures were
observed rather than assumed:

- `acceptEdits` alone — file writes permitted, **every Bash call denied**. A
  build worker writes the code and can substantiate nothing.
- `dontAsk` alone — Bash permitted, **every file write denied**. A build worker
  produces nothing.

`acceptEdits` together with an explicit `--allowedTools` list permits both, with
zero permission denials, and is the least privilege that actually works.

For verification stages, Bash is granted **in full** rather than narrowed to
patterns. A narrowed list was tried and rejected: `Bash(python*)` denies the
compound commands a tester naturally writes, such as
`python tests.py; echo EXIT:$?`, and denies PowerShell outright, so the worker
ends up asking a human for approval and returning `blocked` instead of verifying
anything. A tool allowlist is in any case the wrong place to enforce
immutability, because anything with a shell can write a file. The real guarantee
is enforced on the observable outcome: a `qa` or `review` result whose observed
HEAD moved is refused, which holds no matter how the tree was touched.

---

## 5. Configuring another harness

No code change is needed for another CLI. Resolution order, first hit wins:

1. explicit dict or JSON path passed to `WorkerBackend(config=...)`
2. `PORTABLE_WORKER_CONFIG` environment variable
3. `<state_dir>/worker_backends.json`
4. `project_config.metadata["portable_worker"]`
5. built-in CLI definitions, with `native` still the implicit default

User backends merge **over** the built-ins. To execute one, explicitly set
`default_backend`, a stage entry in `stage_backends`, or the request's `backend`.

```json
{
  "default_backend": "my-harness",
  "stage_backends": { "review": "codex" },
  "backends": {
    "my-harness": {
      "argv": ["my-agent", "run", "--model", "{model}",
               "--schema", "{schema_path}", "--out", "{result_path}", "{prompt}"],
      "result_source": "file",
      "result_path_template": "{work_dir}/result.json",
      "schema_mode": "file",
      "model_map": { "anthropic/claude-opus-5:high": "my-big-model" },
      "model_default": "my-small-model",
      "timeout_seconds": 900,
      "env": { "MY_AGENT_QUIET": "1" }
    }
  }
}
```

Placeholders, each substituted as a **whole argv token**: `{prompt}` `{model}`
`{agent_role}` `{stage}` `{request_id}` `{head_sha}` `{repo_root}`
`{schema_path}` `{schema_inline}` `{result_path}` `{work_dir}`
`{permission_mode}` `{allowed_tools}` `{sandbox_mode}` `{issue_url}` `{pr_url}`.

Substitution never splits or re-parses a token, which is why `shell=False` is
safe here: a prompt containing spaces, quotes or newlines travels as exactly one
argv entry and cannot become an extra argument.

The agent's required result shape is `python worker_backend.py --print-schema`.
It is handed to the CLI as `--json-schema` (Claude) or written out for
`--output-schema` (Codex), so the constraint is enforced by the CLI as well as
re-validated here.

---

## 6. `continuation_driver.py`

```python
backend = WorkerBackend(state_dir=state_dir)
adapter = SuperboardExecutionAdapter(
    state_dir=state_dir,
    fake_executor=False,
    worker_backend=backend,
    repo_root=repo_root,
)
outcome = ContinuationDriver(
    adapter=adapter,
    authorized_ids=["req-1234"],
    state_dir=state_dir,
).run()
```

The adapter is duck-typed: anything exposing
`run_step(request_id=..., real_worker=...)` and returning `status`,
`status_reason`, `worker_result`, `boundaries` will drive. The driver imports
nothing from the adapter, so load order does not matter and there is no circular
dependency.

### What it owns, and what it does not

It owns the loop, the journal, the lock, the signal handling, and the decision
gate. It owns **no** eligibility logic, routing, preflight, gating, or state
transition — all of those already live in the coordinator and the adapter, and
re-deriving any of them would make this a competing scheduler.

### Guarantees

**Authorized ids only.** Constructed with an explicit list; an empty list is an
error, not an invitation to find work. It never scans the ledger for candidates
and never invents a task when it runs out.

**State reloaded every step.** The request is re-read from the ledger before each
dispatch, so an external edit between steps is observed rather than overwritten
from a stale copy.

**Real progress or stop.** Progress is measured as a fingerprint of
`(state, head, evidence count, updated_at, blocker)` taken before and after each
step. A step that reports success and changes nothing parks the request after
exactly one attempt. The loop cannot spin.

**Park, never poll.** Blocked, errored, awaiting-authorization,
decision-blocked, unroutable and no-progress requests are parked with a reason
and left alone. One request parking does not stop the others.

**Restart resumes and never repeats.** Every dispatch is recorded in
`stage_attempts` as `completed`, `blocked`, `failed`, or `no_progress`. Only an
actually completed stage is added to `completed_stages` with the commit it
entered at; a blocker write that changes the ledger fingerprint is not
completion. A restart refuses to re-dispatch a completed stage at the same
commit. Failed and blocked attempts remain parked across restart and retry only
after the explicit human action `--unpark <id>`.

**One driver at a time.** Two layers, each covering what the other cannot: an
in-process registry of held paths, because the shared `FileLock` is thread-local
re-entrant and two drivers on one thread would otherwise both "acquire" it; and
the OS advisory lock, which the kernel releases if the process dies, so a crash
never wedges the next run. A second driver fails immediately naming the holder's
pid and run id.

**Signals are handled.** `SIGINT` and `SIGTERM` request a stop. The in-flight
step is allowed to finish so a worker is never orphaned mid-write, then the
journal is flushed, the lock released, and the original handlers restored.

**Merge and deploy are refused.** `awaiting authorization` is a terminal parking
state. If an adapter result ever reports `auto_merge_allowed` or
`auto_deploy_allowed` as true, the run aborts rather than continuing under a
boundary the driver does not recognise.

### Decision gating

Decisions are checked **before** dispatch, so a worker is not spent on a request
whose direction is unresolved. A decision blocks unless it is answered by a
genuine authorized human operator: status `answered`, an answer payload,
`provenance: "human_operator"`, `is_test` falsy, a non-empty interpretation, and
a responder on the decision's own `authorized_responders`. Synthetic,
agent-authored and unauthorized replies all keep it blocking, which is what the
decision workflow exists to enforce.

Bounded re-check is **off by default**. With `--decision-sync-attempts N` the
driver performs at most N re-checks, each preceded by a wait of at least 15
seconds (a floor no caller can lower), each doing one bounded sync through the
coordinator's own one-shot sync, and resumes only when a real authorized answer
is observed. The attempt count is finite, so it can never become an endless
poll, and a stop signal ends the wait immediately.

---

## 7. Recorded verification

Every claim below was executed, not modelled. Fixtures were used only where
named.

### Real agent CLI, isolated temporary repository

`/tmp/portable-e2e`, a local toy repo with no remote, no GitHub issue, no
Superboard card, and no staging or production access. Ledger and driver state in
`/tmp/portable-e2e-state`. Execution path: `ContinuationDriver.run()` →
`SuperboardExecutionAdapter.run_step` → `WorkerBackend.execute` →
`claude -p ... --output-format json --json-schema ...` (real subprocess,
`shell=False`).

Seed commit `327befcd1078c737dc2335eb44ca0d761d46e80c`, a `stringkit.py` with
`slugify` and no `titlecase`.

| step | stage | backend | transition | head |
| --- | --- | --- | --- | --- |
| 1 | build | `claude-build` | `implementation → QA` | `327befcd → 2335b1f0` |
| 2 | qa | `claude-verify` | `QA → review` | `2335b1f0` unchanged |
| 3 | review | `claude-verify` | `review → awaiting authorization` | `2335b1f0` unchanged |

Build produced a real commit `2335b1f0767b88cbb5231a132d7509ca8db96230` adding
`titlecase` plus `test_stringkit.py`, both digested by sha256, with checks
`python test_stringkit.py` (exit 0, "All tests passed.") and `git commit`
(exit 0). The routing model `google-antigravity/gemini-3.8-flash:high` was mapped
to `sonnet` and the mapping recorded in `evidence.model_note`.

QA ran independently against the exact build head with seven executed checks
including `git rev-parse HEAD`, `python test_stringkit.py`, and a
`git diff 327befc 2335b1f`. Review ran five, including `git show HEAD` and both
`python -m pytest` and plain `python`. Both left the head untouched.

The driver then **parked at `awaiting_authorization`** and merged nothing.

### Restart does not duplicate a completed stage

Run 1 was capped at one step and completed `build`. Run 2 reported
`resumed_from_journal=True` and its first step was `qa`; `build` did not appear.
A third run executed **0 steps** and re-parked at the human gate.

The journal guard was separately proven independent of the ledger: after a
completed stage was recorded and the ledger state then externally reverted to
`implementation` at the same commit, the driver dispatched nothing and reported
`already_completed`.

### Installed driver CLI repository boundary

The documented `continuation_driver.py` CLI was invoked as a subprocess against
a temporary real Git repository with an exact 40-character ledger head and a
configured child worker. With explicit `--repo-root`, the child executed and QA
advanced to review. The same CLI against an existing non-Git directory exited
non-zero, parked the attempt as failed, created no completed-stage record, and
the sentinel child worker was never executed. Omitting `--repo-root` exits 64
before adapter construction.

### Invalid worker output fails closed

Driven through the full stack against `/tmp/portable-liar`, using a scripted
backend that exits `0` and claims `verdict: "pass"` with the words
"original reproduction proven absent":

- fabricated head `000…0` → *"Head binding refused: worker claims commit 000… but
  the observed HEAD … is c952cf74…. The observed commit always wins."*
- truthful head, empty `checks` → *"Worker returned verdict 'pass' for stage
  'build' with no executed checks. Exit status alone is never evidence."*

In both cases the request stayed at `implementation`, the driver parked, and the
ledger was not advanced.

### Real backend failures, also fail-closed

Observed during this work rather than constructed:

- `codex` returned exit 1 with *"You've hit your usage limit … try again at
  Sep 7th"*. Blocked, no fallback. **This is a live external blocker: the Codex
  backend's own end-to-end path could not be exercised on this host.** Its
  `result_source: "file"` path is covered by `TestFileResultBackend`, which
  drives a real subprocess writing a real result file and asserts the schema was
  delivered to the worker, but the codex CLI itself was not reachable.
- `claude` returned exit 1 with `[claude-code:unrecognized_model]` when handed a
  routing id. Blocked; this is what produced the model translation in §3.
- An agent honestly reporting `verdict: "blocked"` because its permissions denied
  the commands it needed was blocked rather than credited.

### Suites

`test_worker_backend.py` 53 tests, `test_continuation_driver.py` 45 tests, both
green. Pre-existing suites re-run green with these modules in place:
`test_superboard_adapter.py`, `test_github_pr_gate.py`,
`test_telegram_notifier.py`, `coordinator_smoke_test.py`,
`routing_smoke_test.py`, `cross_repo_smoke_test.py`.

---

## 8. Quick reference

```bash
# What backends exist, and is each command installed?
python worker_backend.py --list-backends

# The result shape an agent worker must return
python worker_backend.py --print-schema

# Build the argv and validate it without running anything
python worker_backend.py --request-id req-1 --stage build --repo-root . \
  --head-sha <40-hex> --dry-run

# One real stage
python worker_backend.py --request-id req-1 --stage qa --repo-root . \
  --head-sha <40-hex> --model sonnet --prompt "verify X" --json

# Drive authorized requests continuously
python continuation_driver.py --request-id req-1 --state-dir <dir> \
  --repo-root <git-repository> --max-steps 12

# Bounded decision re-check: at most 3 tries, 60s apart, 15s floor
python continuation_driver.py --request-id req-1 --state-dir <dir> \
  --repo-root <git-repository> \
  --decision-sync-attempts 3 --decision-sync-interval 60

# Inspect and clear parking
python continuation_driver.py --state-dir <dir> --show-parked
python continuation_driver.py --state-dir <dir> --unpark req-1 --show-parked
```
