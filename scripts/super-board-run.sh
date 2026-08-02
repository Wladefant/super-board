#!/usr/bin/env bash
# super-board-run.sh — headless autonomous runner.
# Spawned as `nohup scripts/super-board-run.sh <config-slug> &`.
# Pure shell while-loop. Dispatches `claude -p` workers per lane.
# Holds NO Claude session state — re-reads GitHub on every tick.
#
# Anti-zombie controls (added 2026-05-22 after #381 worker-storm incident):
#   1. Orphan scan on startup — refuses to start if super-board claude workers already running.
#   2. Issue-level lock files in .claude/super-board/inflight/<N> — survives runner restart.
#   3. Atomic GitHub assignee claim BEFORE spawning worker (closes 10-30s claude -p cold-start race).
#   4. Quota guard — refuses a tick that would break the immutable GraphQL reserve
#      (1000 points, raisable by config) and exits 75. Never sleeps through a reset.
#   5. Per-tick project-items cache — one gh call per tick, not per column lookup.
#   6. Tick interval bumped from 30s → 120s (GraphQL ProjectsV2 query is ~103 pts; 120s keeps
#      hourly usage well inside the account bucket while leaving the reserve untouched).
#   7. Lane-zombie watchdog (added 2026-05-24 after fitbox-v4 first-run hang) — kills lane PIDs whose
#      claimed issue has already moved out of the lane's expected source column. The worker's logical
#      work is done; if the claude -p process lingers, lane appears busy forever and downstream cards
#      pile up unprocessed. Uses the project-items cache so it costs zero extra API calls per tick.
#
# Dispatch eligibility is NOT decided here. It is delegated to the shared runtime
# (`super_board_runtime.eligibility`), which the read-only planner and the dynamic
# workflow also use, so a card cannot be eligible in one path and ineligible in
# another. Consequences worth knowing before reading the loop:
#   • Status must be EXACTLY `Ready`. There is no "eligible for the requested
#     lane": Backlog, Building, QA, Review, Blocked, and Done are all rejected.
#     A worker carries its own card forward through its lifecycle; the dispatcher
#     does not re-pick cards out of QA or Review.
#   • `design` and `history` cards are never dispatchable, whatever the config says.
#   • A failed issue-state lookup makes a card ineligible. Nothing fails open.

set -euo pipefail

# shellcheck source=scripts/super-board-python.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/super-board-python.sh"
trap sb_tmp_cleanup EXIT

# ───────────────────────────── args + paths ─────────────────────────────
CONFIG_SLUG="${1:-}"
if [ -z "$CONFIG_SLUG" ]; then
  if [ -f .claude/super-board/active ]; then
    CONFIG_SLUG=$(cat .claude/super-board/active)
  else
    echo "usage: $0 <config-slug>  (or set .claude/super-board/active)" >&2
    exit 64
  fi
fi

CONFIG_PATH=".claude/super-board/configs/${CONFIG_SLUG}.json"
if [ ! -f "$CONFIG_PATH" ]; then
  echo "config not found: $CONFIG_PATH" >&2
  exit 66
fi

# ───────────────────────────── config read ─────────────────────────────
# The normalized config is the single source of truth for every policy value.
# An invalid config stops the dispatcher before it can touch GitHub (exit 65).
CONFIG_NATIVE=$(sb_native_path "$CONFIG_PATH")
if ! NORMALIZED_CONFIG=$("$(sb_python)" -B "$SB_SCRIPTS_DIR/super-board-config.py" \
      validate --config "$CONFIG_NATIVE" --json); then
  echo "🛑 refusing to start: config did not validate — see the diagnostics above." >&2
  exit 65
fi
cfg() { echo "$NORMALIZED_CONFIG" | jq -r "$1"; }

PROJECT_OWNER=$(cfg '.project_owner')
PROJECT_NUMBER=$(cfg '.project_number')
BASE_BRANCH=$(cfg '.base_branch')
HUMAN_APPROVES=$(cfg '.human_approves_merge')
REBUILD_CAP=$(cfg '.rebuild_cap')
MAX_WORKERS=$(cfg '.max_workers')
WORKER_BACKEND=$(cfg '.worker_backend')
ACTIVATION_MODE=$(cfg '.activation_mode')

# Operational knobs that carry no policy meaning stay on the raw config.
VARIANT=$(jq -r '.variant // "full"' "$CONFIG_PATH")
BLOCK_ALERT_PCT=$(jq -r '.block_rate_alert_pct // 30' "$CONFIG_PATH")
TICK_SECONDS=$(jq -r '.tick_seconds // 120' "$CONFIG_PATH")
BOT_LOGIN=$(jq -r '.notifications.bot_identity // .bot_identity // ""' "$CONFIG_PATH")
NOPROGRESS_HALT_TICKS=$(jq -r '.noprogress_halt_ticks // 10' "$CONFIG_PATH")
MAX_DISPATCHES=$(jq -r '.max_dispatches // 20' "$CONFIG_PATH")
MAX_HOURS=$(jq -r '.max_hours // 3' "$CONFIG_PATH")
STALE_LOCK_SECONDS=$(jq -r '.stale_lock_seconds // 900' "$CONFIG_PATH")
# How long a lock may hold a card with no worker PID recorded yet — the window
# between claiming the lock and the worker being spawned.
SPAWN_GRACE_SECONDS=$(jq -r '.spawn_grace_seconds // 120' "$CONFIG_PATH")

# This dispatcher is the "claude-p" backend. A board configured for the
# in-session workflow backend must not be drained here.
if [ "$WORKER_BACKEND" != "claude-p" ]; then
  echo "🛑 board '${CONFIG_SLUG}' uses the workflow backend (worker_backend=${WORKER_BACKEND})." >&2
  echo "    Run it in-session: /super-board run ${CONFIG_SLUG}  (see references/run-workflow.md)" >&2
  echo "    To use this dispatcher, set \"worker_backend\": \"claude-p\" in the config." >&2
  exit 78
fi

RUN_DATE=$(date +%Y-%m-%d)
RUN_MANIFEST="docs/super-board/runs/${RUN_DATE}-${CONFIG_SLUG}.md"
INFLIGHT_DIR=".claude/super-board/inflight"
mkdir -p "docs/super-board/runs" .worktrees "$INFLIGHT_DIR"

TOTAL_DISPATCHES=0
TOTAL_REAPS=0

# ───────────────────────────── helpers ─────────────────────────────
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" | tee -a "$RUN_MANIFEST"; }

sb_is_windows() {
  case "${OSTYPE:-$(uname -s 2>/dev/null || echo '')}" in
    msys*|MINGW*|MSYS*|cygwin*|CYGWIN*) return 0 ;;
    *) return 1 ;;
  esac
}

PROJECT_ITEMS_JSON=""
fetch_project_items() {
  # One gh call per tick; all column lookups read from this cache.
  #
  # Fails CLOSED. A failed read used to become `{"items":[]}`, and an empty
  # board is indistinguishable from a drained one: every column counted zero,
  # the done condition fired, and the runner exited reporting success while the
  # board it could not read was full of work. An unreadable board is an error,
  # never a state.
  local raw rc=0
  raw=$(gh project item-list "$PROJECT_NUMBER" --owner "$PROJECT_OWNER" \
          --format json --limit 500 2>/dev/null) || rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$raw" ] \
     || ! printf '%s' "$raw" | jq -e '(.items | type) == "array"' >/dev/null 2>&1; then
    PROJECT_ITEMS_JSON=""
    return 1
  fi
  PROJECT_ITEMS_JSON="$raw"
  return 0
}

halt_unreadable_board() {
  # The project snapshot is this runtime's input. An input contract that cannot
  # be satisfied is exit 65 — the same code an unreadable config produces.
  log "🛑 halting — the project board could not be read, and an unreadable board is NOT an empty one."
  log "    Nothing is dispatched and no done condition is inferred. Draining in-flight workers, then exiting with 65."
  drain_in_flight
  exit 65
}

column_count() {
  echo "$PROJECT_ITEMS_JSON" | jq --arg col "$1" '[.items[] | select(.status == $col)] | length'
}

ELIGIBLE_CARDS_JSON="[]"
refresh_eligible_cards() {
  # ONE runtime call per tick decides which cards may be dispatched at all.
  # Every gate lives in super_board_runtime.eligibility, so this dispatcher
  # cannot drift from the planner or the workflow. Fails closed: if the
  # evaluation itself fails, nothing is dispatched this tick.
  local plan rc=0
  plan=$(printf '%s' "$PROJECT_ITEMS_JSON" | sb_runtime super_board_runtime.eligibility \
           --items - --config "$CONFIG_NATIVE" 2>/dev/null) || rc=$?
  if [ "$rc" -ne 0 ]; then
    ELIGIBLE_CARDS_JSON="[]"
    log "🛑 eligibility evaluation failed (exit ${rc}) — dispatching nothing this tick (fail closed)"
    return 0
  fi
  ELIGIBLE_CARDS_JSON=$(echo "$plan" | jq -c '.cards // []')
  local skipped
  skipped=$(echo "$plan" | jq -r '
    [.decisions[] | select(.eligible == false) | .reason_codes[]] | group_by(.)
    | map("\(.[0])×\(length)") | join(" ")')
  [ -n "$skipped" ] && log "eligibility — dispatchable=$(echo "$ELIGIBLE_CARDS_JSON" | jq 'length') skipped: ${skipped}"
  return 0
}

next_dispatchable_card() {
  # First runtime-eligible card without a local in-flight lock. No fail-open
  # branch exists: a card the runtime did not return is not dispatched, period.
  local issue
  for issue in $(echo "$ELIGIBLE_CARDS_JSON" | jq -r '.[].number // empty'); do
    if issue_locked "$issue"; then
      continue
    fi
    echo "$issue"
    return 0
  done
}

read_lock() {
  # Reads $INFLIGHT_DIR/$1 (bash-assignment format) into PID/LANE/STARTED.
  # Sets empty strings if the file is missing or legacy single-PID format.
  local lock="$INFLIGHT_DIR/$1"
  PID=""; LANE=""; STARTED=""
  [ -f "$lock" ] || return 1
  if grep -q '^PID=' "$lock" 2>/dev/null; then
    # shellcheck disable=SC1090
    . "$lock" 2>/dev/null || true
  else
    # Legacy format (pre v1.3.0): single line PID only.
    PID=$(cat "$lock" 2>/dev/null || echo "")
  fi
  return 0
}

lock_age_seconds() {
  local mtime
  mtime=$(date -r "$1" +%s 2>/dev/null || echo 0)
  echo $(( $(date +%s) - mtime ))
}

issue_locked() {
  # Returns 0 if the issue has a live in-flight lock; cleans stale locks.
  # On Windows/MSYS, unverifiable PIDs are treated as alive until stale_lock_seconds.
  local issue="$1" lock="$INFLIGHT_DIR/$1"
  [ -f "$lock" ] || return 1
  read_lock "$issue"
  if [ -z "${PID:-}" ]; then
    # Claimed, worker not spawned yet. The lock is written before the claim so
    # a second dispatcher cannot start a duplicate worker in that window; it is
    # held only briefly so a crash between claim and spawn cannot wedge the card.
    if [ "$(lock_age_seconds "$lock")" -lt "$SPAWN_GRACE_SECONDS" ]; then
      return 0
    fi
    log "reaped a claim lock for #${issue} that never produced a worker"
    rm -f "$lock"
    return 1
  fi
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    return 0
  fi
  if [ -n "$PID" ] && sb_is_windows; then
    local lock_mtime now age
    lock_mtime=$(date -r "$lock" +%s 2>/dev/null || echo 0)
    now=$(date +%s)
    age=$((now - lock_mtime))
    if [ "$age" -lt "$STALE_LOCK_SECONDS" ]; then
      log "cannot verify PID ${PID} for #${issue} on Windows — treating as alive, skipping reap (age=${age}s < ${STALE_LOCK_SECONDS}s)"
      return 0
    fi
    log "reaped unverifiable Windows lock for #${issue} (pid=${PID}, age=${age}s ≥ ${STALE_LOCK_SECONDS}s)"
  fi
  rm -f "$lock"
  return 1
}

lane_idle() {
  local pid="${1:-}"
  [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null
}

# One ProjectsV2 item scan plus the tick's incidental calls. Estimated BEFORE
# spending, per the reserve contract.
TICK_ESTIMATED_COST=${TICK_ESTIMATED_COST:-120}

gh_quota_guard() {
  # Refuse the tick if it would break the immutable GraphQL reserve, or if the
  # quota cannot be read at all. Stops cleanly with exit 75: no sleep through the
  # reset, no retry spin, and no fabricated fallback capacity.
  local rc=0 line
  line=$(sb_runtime super_board_runtime.quota check \
           --estimated-cost "$TICK_ESTIMATED_COST" --config "$CONFIG_NATIVE" 2>&1 >/dev/null) || rc=$?
  if [ "$rc" -ne 0 ]; then
    log "🛑 halting — the GraphQL reserve is protected and this tick cannot be afforded."
    log "    $(echo "$line" | grep -o '\[quota\].*' | head -1)"
    log "    Draining in-flight workers, then exiting with 75."
    drain_in_flight
    exit 75
  fi
}

try_claim_assignee() {
  # Add-then-VERIFY claim. Returns 0 only when this dispatcher owns the card.
  # Not attempted when bot_identity is unset (solo single-user runs rely on
  # local locks only).
  #
  # `gh issue edit --add-assignee` is NOT a mutex. A GitHub issue accepts up to
  # ten assignees, so the add succeeds whether or not someone else has already
  # claimed the card — two orchestrators both saw exit 0 and both dispatched a
  # worker onto the same issue. The add is only half a claim; the other half is
  # reading the assignee list back and requiring it to be exactly us. On any
  # other set, the assignee we just added is removed again and the card is
  # skipped: losing a race must not leave a claim behind.
  local issue="$1" assignees
  [ -z "$BOT_LOGIN" ] && return 0
  gh issue edit "$issue" --add-assignee "$BOT_LOGIN" >/dev/null 2>&1 || {
    log "claim failed on #${issue} (race or gh api error) — skipping this tick"
    return 1
  }
  assignees=$(gh issue view "$issue" --json assignees \
                -q '[.assignees[].login] | join(",")' 2>/dev/null) || {
    log "claim on #${issue} could not be verified — releasing it and skipping (fail closed)"
    release_claim "$issue"
    return 1
  }
  assignees=$(printf '%s' "$assignees" | tr -d '[:space:]')
  if [ "$assignees" != "$BOT_LOGIN" ]; then
    log "claim race lost on #${issue} (assignees: ${assignees:-none}) — releasing ours and skipping"
    release_claim "$issue"
    return 1
  fi
  return 0
}

card_issue_url() {
  echo "$ELIGIBLE_CARDS_JSON" | jq -r --argjson n "$1" '.[] | select(.number == $n) | .issue_url // empty' | head -1
}

activation_permits() {
  # $1 = issue number, $2 = stage (claim|launch).
  # Activation is re-read from disk at BOTH mutation boundaries, so an operator
  # who flips the board off mid-run aborts the very next claim. Fails closed on
  # any error: no decision means no dispatch.
  local issue="$1" stage="$2" url payload permitted reason mode rc=0
  url=$(card_issue_url "$issue")
  payload=$(sb_runtime super_board_runtime.activation \
      --config "$CONFIG_NATIVE" --issue-number "$issue" \
      ${url:+--issue-url "$url"} \
      --planned-mode "$PLANNED_ACTIVATION_MODE" --stage "$stage" 2>/dev/null) || rc=$?
  if [ "$rc" -ne 0 ]; then
    log "🛑 activation re-check failed for #${issue} at ${stage} (exit ${rc}) — aborting (fail closed)"
    return 1
  fi
  permitted=$(echo "$payload" | jq -r '.permitted')
  reason=$(echo "$payload" | jq -r '.reason_code // "unknown"')
  mode=$(echo "$payload" | jq -r '.activation_mode')
  if [ "$permitted" != "true" ]; then
    log "🛑 activation refused #${issue} at ${stage} — mode=${mode} reason=${reason}; no claim, no launch"
    return 1
  fi
  return 0
}

release_claim() {
  [ -n "$BOT_LOGIN" ] || return 0
  gh issue edit "$1" --remove-assignee "$BOT_LOGIN" >/dev/null 2>&1 || true
}

sb_publish() {
  # $1 = publication surface, $2 = path to the rendered payload.
  # The ONLY way anything leaves this dispatcher for GitHub. The payload is
  # rendered first, then redacted, then the complete redacted result is scanned
  # again, and exit 78 means nothing was written. Never call `gh issue comment`
  # or any other write path directly from here — a second path is a second place
  # to forget a secret category.
  local surface="$1" file="$2" payload rc=0
  payload=$(jq -n --arg s "$surface" --rawfile t "$file" '{surface:$s, text:$t}' | sb_config_file)
  "$(sb_python)" -B "$SB_SCRIPTS_DIR/super-board-publish.py" \
    publish --input "$payload" --json || rc=$?
  return "$rc"
}

publish_run_manifest() {
  # The run manifest carries GitHub-controlled issue titles and worker output,
  # so it is sanitized as ONE complete payload before it is surfaced anywhere.
  local rc=0 out
  out=$(sb_publish dispatch-manifest "$RUN_MANIFEST") || rc=$?
  if [ "$rc" -eq 78 ]; then
    log "🛑 the run manifest was rejected at the publication boundary — it is NOT safe to share. Nothing was published."
    return 78
  fi
  if [ "$rc" -ne 0 ]; then
    log "🛑 the run manifest could not be sanitized (exit ${rc}) — treating it as unpublishable (fail closed)."
    return "$rc"
  fi
  echo "$out" | jq -r '.text' > "${RUN_MANIFEST}.sanitized"
  log "run manifest sanitized → ${RUN_MANIFEST}.sanitized ($(echo "$out" | jq '.redactions | length') redaction(s))"
  return 0
}

report_qa_evidence() {
  # $1 = issue number, $2 = ledger entry path, $3 = pull request URL.
  # Read-only freshness check for a card sitting in QA or Review. Emits the
  # `qa-evidence` manifest line the status renderer reads, so an operator can
  # see tested-vs-current SHA instead of a bare "Review".
  local issue="$1" ledger="$2" pr="$3" payload tested current invalid
  [ -f "$ledger" ] || return 0
  payload=$(sb_runtime super_board_runtime.qa merge-handoff \
              --ledger "$(sb_native_path "$ledger")" --pull-request "$pr" \
              --check-conclusion "${QA_CHECK_CONCLUSION:-}" 2>/dev/null) || payload=""
  [ -n "$payload" ] || return 0
  tested=$(echo "$payload" | jq -r '.tested_sha // empty')
  current=$(echo "$payload" | jq -r '.current_head_sha // empty')
  [ -n "$tested" ] && [ -n "$current" ] || return 0
  invalid=no
  [ "$tested" = "$current" ] || invalid=yes
  log "qa-evidence issue=#${issue} tested=${tested} current=${current} invalidated=${invalid}"
}

merge_handoff_ready() {
  # $1 = ledger entry path, $2 = pull request URL, $3 = required-check conclusion.
  # The last gate before a HUMAN merges. Read-only: it rereads the head,
  # compares it with the last successful tested SHA, and verifies the SHA-bound
  # required check. It never merges — the runtime has no merge path at all — it
  # only decides whether this card may be REPORTED as merge-ready.
  local payload ready reason rc=0
  payload=$(sb_runtime super_board_runtime.qa merge-handoff \
              --ledger "$(sb_native_path "$1")" --pull-request "$2" \
              --check-conclusion "$3" 2>/dev/null) || rc=$?
  if [ "$rc" -ne 0 ]; then
    log "🛑 merge handoff refused for $2 — the head or the required check could not be read (fail closed)"
    return 1
  fi
  ready=$(echo "$payload" | jq -r '.merge_ready')
  reason=$(echo "$payload" | jq -r '.reason_code // "unknown"')
  if [ "$ready" != "true" ]; then
    log "🛑 not merge-ready: $2 — ${reason}. The card stays in Review; a human merges nothing yet."
    return 1
  fi
  log "✅ merge handoff verified for $2 — tested SHA is still the head and ${QA_CHECK_CONTEXT:-superboard/exact-sha-qa} passed. A human rebase-merges."
  return 0
}

take_issue_lock() {
  # Create the in-flight lock ATOMICALLY, before anything is claimed or spawned.
  # `set -o noclobber` makes the create-or-fail one operation, so two
  # dispatchers racing for the same card cannot both proceed. The PID is not
  # known yet and is recorded by `record_worker_pid` once it is.
  local issue="$1" lane="$2"
  (set -o noclobber
   printf 'PID=\nLANE=%s\nSTARTED=%s\n' "$lane" "$(date -u +%FT%TZ)" \
     > "$INFLIGHT_DIR/$issue") 2>/dev/null
}

record_worker_pid() {
  # v1.3.0+ lock format: bash-assignment style so `super-board stop` can source
  # it to recover lane + dispatch time. issue_locked()/reap_finished_locks()
  # still work because PID= is the first line.
  local issue="$1" lane="$2" pid="$3"
  printf 'PID=%s\nLANE=%s\nSTARTED=%s\n' "$pid" "$lane" "$(date -u +%FT%TZ)" \
    > "$INFLIGHT_DIR/$issue"
}

drop_issue_lock() {
  rm -f "$INFLIGHT_DIR/$1"
}

dispatch_lane() {
  # $1 = lane (build|qa|review); $2 = issue number
  local lane="$1" issue="$2" prompt pid
  if issue_locked "$issue"; then
    log "skip dispatch lane=${lane} issue=#${issue} — already locked"
    return 0
  fi
  # The lock comes FIRST — before the claim, before the worker. It used to be
  # written after the spawn, which left two windows open: a concurrent
  # dispatcher could pass its own `issue_locked` check and launch a second
  # worker onto the same card, and a crash between the spawn and the write left
  # a live worker that nothing tracked, reaped, or stopped. Every refusal below
  # drops the lock again.
  if ! take_issue_lock "$issue" "$lane"; then
    log "skip dispatch lane=${lane} issue=#${issue} — another dispatcher took the lock first"
    return 0
  fi
  # Boundary 1: immediately before the claim.
  if ! activation_permits "$issue" claim; then
    drop_issue_lock "$issue"
    return 0
  fi
  if ! try_claim_assignee "$issue"; then
    drop_issue_lock "$issue"
    return 0
  fi
  # Boundary 2: immediately before the launch. A mode flip during the claim
  # window releases the claim again rather than launching a worker.
  if ! activation_permits "$issue" launch; then
    release_claim "$issue"
    drop_issue_lock "$issue"
    return 0
  fi
  # The declared route travels with the card. A worker never infers a branch
  # from a Test Area, from geography in the prose, or from the current checkout.
  local route
  route=$(echo "$ELIGIBLE_CARDS_JSON" | jq -r --argjson n "$issue" \
            '.[] | select(.number == $n) | .selected_base_branch // empty' | head -1)
  if [ -z "$route" ]; then
    log "🛑 refusing to dispatch #${issue} — no declared branch route survived eligibility (fail closed)"
    release_claim "$issue"
    drop_issue_lock "$issue"
    return 0
  fi
  case "$route" in
    designstaging|main|master|default)
      log "🛑 refusing to dispatch #${issue} — '${route}' is never a dispatch route"
      release_claim "$issue"
      drop_issue_lock "$issue"
      return 0 ;;
  esac
  local route_note="Base branch: ${route} (declared route — never infer another one)."
  case "$lane" in
    build)  prompt="Run super-build on issue #${issue} for super-board run. Read .claude/skills/super-board/references/run.md → Builder lifecycle. ${route_note} Config: ${CONFIG_PATH}." ;;
    qa)     prompt="Run super-qa on issue #${issue} for super-board run. Read .claude/skills/super-board/references/run.md → Tester lifecycle. ${route_note} Config: ${CONFIG_PATH}." ;;
    review) prompt="Run super-review on issue #${issue} for super-board run. Read .claude/skills/super-board/references/run.md → Reviewer lifecycle. ${route_note} Config: ${CONFIG_PATH}." ;;
    *)
      log "unknown lane: $lane"
      release_claim "$issue"
      drop_issue_lock "$issue"
      return 1 ;;
  esac
  pid=""
  nohup claude -p "$prompt" >/dev/null 2>&1 &
  pid=$!
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    log "🛑 the worker for #${issue} did not start — releasing the claim and the lock"
    release_claim "$issue"
    drop_issue_lock "$issue"
    return 0
  fi
  record_worker_pid "$issue" "$lane" "$pid"
  case "$lane" in
    build) BUILD_PID="$pid"; BUILD_ISSUE="$issue" ;;
    qa) QA_PID="$pid"; QA_ISSUE="$issue" ;;
    review) REVIEW_PID="$pid"; REVIEW_ISSUE="$issue" ;;
  esac
  TOTAL_DISPATCHES=$((TOTAL_DISPATCHES + 1))
  log "dispatch lane=${lane} issue=#${issue} pid=${pid} claim=${BOT_LOGIN:-local-only} activation=${PLANNED_ACTIVATION_MODE}"
}

issue_status() {
  # Lookup issue #$1 in the cached project items; emit its current column name (or empty).
  echo "$PROJECT_ITEMS_JSON" | jq -r --arg n "$1" '
    .items[] | select(.content.number == ($n | tonumber)) | .status' | head -1
}

check_lane_zombie() {
  # $1 = lane name (build|qa|review); $2 = space-separated list of expected source columns.
  # If the lane's worker PID is alive but its claimed issue has already moved to a column
  # NOT in the expected source set, the worker's logical work is done — kill the zombie
  # process and free the lane. Uses cached project items only (no extra API calls).
  local lane="$1" expected="$2" pid="" issue=""
  case "$lane" in
    build)  pid="$BUILD_PID";  issue="$BUILD_ISSUE" ;;
    qa)     pid="$QA_PID";     issue="$QA_ISSUE" ;;
    review) pid="$REVIEW_PID"; issue="$REVIEW_ISSUE" ;;
    *) return 1 ;;
  esac
  [ -z "$pid" ] && return 0
  [ -z "$issue" ] && return 0
  kill -0 "$pid" 2>/dev/null || return 0   # already dead → reap_finished_locks handles it
  local cur found=0 col
  cur=$(issue_status "$issue")
  [ -z "$cur" ] && return 0                # not in cache (closed/deleted/race) → don't kill
  for col in $expected; do
    [ "$cur" = "$col" ] && found=1
  done
  if [ "$found" -eq 0 ]; then
    log "💀 zombie ${lane} worker on #${issue} (pid=${pid}) — card moved to '${cur}'; killing"
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
    rm -f "$INFLIGHT_DIR/$issue"
    [ -n "$BOT_LOGIN" ] && gh issue edit "$issue" --remove-assignee "$BOT_LOGIN" >/dev/null 2>&1 || true
    case "$lane" in
      build)  BUILD_PID="";  BUILD_ISSUE="" ;;
      qa)     QA_PID="";     QA_ISSUE="" ;;
      review) REVIEW_PID=""; REVIEW_ISSUE="" ;;
    esac
  fi
}

sweep_lane_zombies() {
  check_lane_zombie build  "Ready Building"
  check_lane_zombie qa     "QA"
  check_lane_zombie review "Review"
}

reap_finished_locks() {
  # Sweep inflight/ for dead PIDs; remove locks AND sweep stale assignees so the
  # next dispatch can re-claim the card if the worker crashed without releasing.
  # The assignee remove is idempotent — no-op if the worker exited cleanly.
  # On Windows/MSYS, unverifiable PIDs are treated as alive until stale_lock_seconds.
  local lock issue
  for lock in "$INFLIGHT_DIR"/*; do
    [ -f "$lock" ] || continue
    issue=$(basename "$lock")
    # Issue locks only: basenames are issue numbers. Anything else (e.g. the
    # workflow backend's workflow-wave.lock) is not ours to reap — deleting it
    # would dissolve the backend mutual exclusion mid-run.
    case "$issue" in *[!0-9]*|'') continue ;; esac
    read_lock "$issue"
    if [ -z "${PID:-}" ]; then
      # A lock taken but not yet spawned into. Held for the spawn grace window,
      # then reaped so a crash in that window cannot wedge the card forever.
      if [ "$(lock_age_seconds "$lock")" -lt "$SPAWN_GRACE_SECONDS" ]; then
        continue
      fi
      log "reaped a claim lock for #${issue} that never produced a worker"
      rm -f "$lock"
      TOTAL_REAPS=$((TOTAL_REAPS + 1))
      [ -n "$BOT_LOGIN" ] && gh issue edit "$issue" --remove-assignee "$BOT_LOGIN" >/dev/null 2>&1 || true
      continue
    fi
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
      continue
    fi
    if [ -n "$PID" ] && sb_is_windows; then
      local lock_mtime now age
      lock_mtime=$(date -r "$lock" +%s 2>/dev/null || echo 0)
      now=$(date +%s)
      age=$((now - lock_mtime))
      if [ "$age" -lt "$STALE_LOCK_SECONDS" ]; then
        log "cannot verify PID ${PID} for #${issue} on Windows — treating as alive, skipping reap (age=${age}s < ${STALE_LOCK_SECONDS}s)"
        continue
      fi
      log "reaped unverifiable Windows lock for #${issue} (pid=${PID}, age=${age}s ≥ ${STALE_LOCK_SECONDS}s)"
    fi
    rm -f "$lock"
    TOTAL_REAPS=$((TOTAL_REAPS + 1))
    if [ -n "$BOT_LOGIN" ]; then
      gh issue edit "$issue" --remove-assignee "$BOT_LOGIN" >/dev/null 2>&1 || true
      log "reaped stale lock + swept assignee on #${issue} (pid=${PID:-empty})"
    else
      log "reaped stale lock for #${issue} (pid=${PID:-empty})"
    fi
  done
}

drain_in_flight() {
  # Wait for in-flight lane workers to exit (or bound the wait) after a hard ceiling.
  local wait_ticks=0 max_drain_ticks=30
  while [ "$wait_ticks" -lt "$max_drain_ticks" ]; do
    reap_finished_locks
    if lane_idle "${BUILD_PID:-}" && lane_idle "${QA_PID:-}" && lane_idle "${REVIEW_PID:-}"; then
      return 0
    fi
    wait_ticks=$((wait_ticks + 1))
    log "draining in-flight workers (${wait_ticks}/${max_drain_ticks})..."
    sleep "$TICK_SECONDS"
  done
  log "⚠ drain timed out after ${max_drain_ticks} ticks — exiting anyway"
}

stop_and_release_in_flight() {
  # Stop every worker this dispatcher owns and give back everything it holds:
  # the GitHub assignee claim and the local lock, per issue. Used on INT/TERM,
  # where waiting out a full drain (up to max_drain_ticks × tick_seconds) is not
  # an option — an operator who pressed Ctrl-C is not going to wait an hour, and
  # walking away leaves cards claimed by a process that no longer exists, which
  # the planner then skips forever.
  local lock issue
  for lock in "$INFLIGHT_DIR"/*; do
    [ -f "$lock" ] || continue
    issue=$(basename "$lock")
    # Issue locks only. The workflow backend's workflow-wave.lock is not ours;
    # deleting it would dissolve the backend mutual exclusion mid-run.
    case "$issue" in *[!0-9]*|'') continue ;; esac
    read_lock "$issue"
    if [ -n "${PID:-}" ] && kill -0 "$PID" 2>/dev/null; then
      kill "$PID" 2>/dev/null || true
      sleep 1
      kill -9 "$PID" 2>/dev/null || true
    fi
    release_claim "$issue"
    rm -f "$lock"
    log "released #${issue} on stop (pid=${PID:-none})"
  done
  BUILD_PID=""; BUILD_ISSUE=""
  QA_PID=""; QA_ISSUE=""
  REVIEW_PID=""; REVIEW_ISSUE=""
}

on_terminate() {
  # A signal used to run `sb_tmp_cleanup` and nothing else, so INT/TERM removed
  # a few temp files and left every worker running, every assignee claimed, and
  # every lock in place — the board looked busy to the next dispatcher forever.
  local code="$1"
  trap - EXIT INT TERM
  log "🛑 stop signal received — stopping in-flight workers and releasing claims."
  stop_and_release_in_flight
  sb_tmp_cleanup
  exit "$code"
}

BUILD_PID=""; BUILD_ISSUE=""
QA_PID=""; QA_ISSUE=""
REVIEW_PID=""; REVIEW_ISSUE=""
# The mode the run was launched with. Both mutation boundaries re-read the mode
# from disk and compare it against this, so a board flipped off mid-run aborts
# the very next claim.
PLANNED_ACTIVATION_MODE="$ACTIVATION_MODE"

# Signals are handled from here on: everything above only defines behaviour.
trap 'on_terminate 130' INT
trap 'on_terminate 143' TERM

# Library mode: define every function, then stop. The tests source this file to
# exercise one guarantee at a time instead of only as an emergent property of a
# two-minute loop. Nothing below this line runs when it is set.
if [ -n "${SB_RUN_LIB_ONLY:-}" ]; then
  return 0 2>/dev/null || exit 0
fi

# ───────────────────────────── preconditions ─────────────────────────────
log "super-board run started — config=${CONFIG_SLUG} variant=${VARIANT} base=${BASE_BRANCH} activation=${ACTIVATION_MODE} tick=${TICK_SECONDS}s max_workers=${MAX_WORKERS} noprogress_halt_ticks=${NOPROGRESS_HALT_TICKS} max_dispatches=${MAX_DISPATCHES} max_hours=${MAX_HOURS}"

# ── Identity preflight: prove who we are BEFORE any scan or mutation.
# Nothing fails open — a token we cannot classify, pin, or scope is a stop.
AUTH_MODE=$(cfg '.github_auth.mode')
if ! AUTH_REPORT=$("$(sb_python)" -B "$SB_SCRIPTS_DIR/super-board-auth.py"       preflight --config "$CONFIG_NATIVE" --mode "$AUTH_MODE" --json); then
  log "🛑 refusing to start: GitHub identity preflight failed (mode=${AUTH_MODE}) — see the diagnostics above."
  exit 69
fi
log "identity verified — mode=${AUTH_MODE} login=$(echo "$AUTH_REPORT" | jq -r '.login') token_class=$(echo "$AUTH_REPORT" | jq -r '.token_class')"

# Orphan-worker guard. `|| true` defends against pipefail when pgrep finds nothing.
ORPHANS=$(pgrep -f 'claude -p .*super-board run' 2>/dev/null | grep -v "^$$\$" | wc -l | tr -d ' ' || true)
ORPHANS=${ORPHANS:-0}
if [ "$ORPHANS" -gt 0 ]; then
  log "🛑 refusing to start: ${ORPHANS} super-board claude workers already running."
  log "    Stop them first: pkill -f 'claude -p .*super-board run'"
  log "    Then re-run: $0 $CONFIG_SLUG"
  exit 73
fi

# Workflow-backend mutual exclusion (see references/run-workflow.md §Preconditions).
WAVE_LOCK=".claude/super-board/inflight/workflow-wave.lock"
if [ -f "$WAVE_LOCK" ]; then
  log "🛑 refusing to start: workflow-backend wave in flight ($WAVE_LOCK exists)."
  log "    If no wave is actually running, remove the stale lock: rm $WAVE_LOCK"
  exit 74
fi

# Production-merge guard. `human_approves_merge: false` is refused outright on a
# production base branch: the runtime has no merge path, so a board configured
# as if it did is a board whose contract nobody has re-read.
#
# Exit 76, not 75. 75 is the immutable GraphQL reserve (G9), and two different
# halts sharing one code makes the halt unreadable to every caller downstream.
if [ "$BASE_BRANCH" = "main" ] && [ "$HUMAN_APPROVES" = "false" ]; then
  if rg -qU 'on:\s*\n?\s*push:\s*\n?\s*branches:[^a-z]*main' .github/workflows 2>/dev/null \
     || [ -f vercel.json ] || [ -f netlify.toml ]; then
    log "🛡 refusing to start: this board targets production main with human_approves_merge=false."
    log "    Every merge is performed by a human, by rebase. Set human_approves_merge: true,"
    log "    or point base_branch at a staging branch. Exiting 76."
    exit 76
  fi
fi

# Stale-worktree scan.
if [ -d .worktrees ]; then
  for wt in .worktrees/*/; do
    [ -d "$wt" ] || continue
    branch=$(git -C "$wt" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    if [ -z "$branch" ] || ! git rev-parse --verify "$branch" >/dev/null 2>&1; then
      log "stale worktree: $wt (branch '$branch' missing) — removing"
      git worktree remove --force "$wt" 2>/dev/null || rm -rf "$wt"
    fi
  done
fi

# Reap any leftover stale locks from a previous crashed run.
reap_finished_locks

# ───────────────────────────── main loop ─────────────────────────────
gh_quota_guard
fetch_project_items || halt_unreadable_board
INITIAL_READY=$(column_count "Ready")
log "initial Ready count: $INITIAL_READY"

NO_PROGRESS_TICKS=0
NOMERGE_TICKS=0
PREV_DONE_COUNT=$(column_count "Done")
RUN_START_EPOCH=$(date +%s)

while true; do
  # Workflow-backend mutual exclusion, re-checked every tick: the startup
  # check alone leaves a TOCTOU window where a workflow run starting at the
  # same moment as this dispatcher is never detected by either side.
  if [ -f "$WAVE_LOCK" ]; then
    log "🛑 workflow-backend wave appeared mid-run ($WAVE_LOCK) — halting for mutual exclusion."
    log "    Resume after the wave: $0 $CONFIG_SLUG"
    exit 74
  fi

  reap_finished_locks  # cheap local sweep; runs every tick

  # ── Hard ceiling: wall-clock (every tick, no API).
  ELAPSED=$(( $(date +%s) - RUN_START_EPOCH ))
  MAX_SECONDS=$(( MAX_HOURS * 3600 ))
  if [ "$ELAPSED" -ge "$MAX_SECONDS" ]; then
    log "🛑 halt — reached max_hours (${MAX_HOURS}) — draining in-flight workers then exiting (dispatches=${TOTAL_DISPATCHES}, reaps=${TOTAL_REAPS})"
    drain_in_flight
    break
  fi

  # ── Hard ceiling: dispatch budget (stop new dispatches; drain; exit).
  if [ "$TOTAL_DISPATCHES" -ge "$MAX_DISPATCHES" ]; then
    log "🛑 halt — reached max_dispatches (${MAX_DISPATCHES}) — draining in-flight workers then exiting (dispatches=${TOTAL_DISPATCHES}, reaps=${TOTAL_REAPS})"
    drain_in_flight
    break
  fi

  # ── Zombie sweep against the LAST cached project state (no extra API).
  #    Catches workers whose card already moved out of the lane's source column
  #    but whose claude -p process didn't exit. Runs every tick, even cheap ones,
  #    so a cap-reached pipeline can still self-heal when one lane is a zombie.
  sweep_lane_zombies

  # ── Free pre-check: count active lanes from local PIDs (no API calls).
  BUILD_IDLE=1; QA_IDLE=1; REVIEW_IDLE=1
  lane_idle "$BUILD_PID" || BUILD_IDLE=0
  lane_idle "$QA_PID" || QA_IDLE=0
  lane_idle "$REVIEW_PID" || REVIEW_IDLE=0

  ACTIVE_WORKERS=0
  [ "$BUILD_IDLE" -eq 1 ] || ACTIVE_WORKERS=$((ACTIVE_WORKERS + 1))
  [ "$QA_IDLE" -eq 1 ] || ACTIVE_WORKERS=$((ACTIVE_WORKERS + 1))
  [ "$REVIEW_IDLE" -eq 1 ] || ACTIVE_WORKERS=$((ACTIVE_WORKERS + 1))

  # ── Cheap-tick path: workers at cap → skip GraphQL fetch entirely.
  #    The board can't change in a way that helps us until a lane frees up.
  if [ "$ACTIVE_WORKERS" -ge "$MAX_WORKERS" ]; then
    log "tick — cap reached (${ACTIVE_WORKERS}/${MAX_WORKERS} busy) — skipping GraphQL fetch, sleeping ${TICK_SECONDS}s"
    sleep "$TICK_SECONDS"
    continue
  fi

  # ── Expensive-tick path: we have capacity, fetch real state.
  gh_quota_guard
  fetch_project_items || halt_unreadable_board
  refresh_eligible_cards

  # Re-sweep zombies against fresh cache; the previous sweep used stale data.
  sweep_lane_zombies
  BUILD_IDLE=1; QA_IDLE=1; REVIEW_IDLE=1
  lane_idle "$BUILD_PID" || BUILD_IDLE=0
  lane_idle "$QA_PID" || QA_IDLE=0
  lane_idle "$REVIEW_PID" || REVIEW_IDLE=0
  ACTIVE_WORKERS=0
  [ "$BUILD_IDLE" -eq 1 ] || ACTIVE_WORKERS=$((ACTIVE_WORKERS + 1))
  [ "$QA_IDLE" -eq 1 ] || ACTIVE_WORKERS=$((ACTIVE_WORKERS + 1))
  [ "$REVIEW_IDLE" -eq 1 ] || ACTIVE_WORKERS=$((ACTIVE_WORKERS + 1))

  READY=$(column_count "Ready")
  BUILDING=0
  [ "$VARIANT" = "full" ] && BUILDING=$(column_count "Building")
  QA=$(column_count "QA")
  REVIEW=$(column_count "Review")
  BLOCKED=$(column_count "Blocked")
  DONE=$(column_count "Done")

  log "tick — activation=${PLANNED_ACTIVATION_MODE} Ready=$READY Building=$BUILDING QA=$QA Review=$REVIEW Blocked=$BLOCKED Done=$DONE lanes: b_idle=$BUILD_IDLE(#${BUILD_ISSUE:-_}) q_idle=$QA_IDLE(#${QA_ISSUE:-_}) r_idle=$REVIEW_IDLE(#${REVIEW_ISSUE:-_})"

  # Done-count progress gate (issue #8): fires independent of lane occupancy.
  # Catches zero-merge token runaways (issue #8).
  if [ "$DONE" -gt "$PREV_DONE_COUNT" ]; then
    NOMERGE_TICKS=0
    PREV_DONE_COUNT=$DONE
  else
    NOMERGE_TICKS=$((NOMERGE_TICKS + 1))
    if [ "$NOMERGE_TICKS" -ge "$NOPROGRESS_HALT_TICKS" ]; then
      log "🛑 halt — zero landed progress for ${NOPROGRESS_HALT_TICKS} ticks (Done delta=0, dispatches=${TOTAL_DISPATCHES}, reaps=${TOTAL_REAPS})"
      break
    fi
  fi

  if [ "$READY" -eq 0 ] && [ "$BUILDING" -eq 0 ] && [ "$QA" -eq 0 ] && [ "$REVIEW" -eq 0 ] \
     && [ "$BUILD_IDLE" -eq 1 ] && [ "$QA_IDLE" -eq 1 ] && [ "$REVIEW_IDLE" -eq 1 ]; then
    log "✅ all active-pipeline columns empty and all lanes idle — exiting cleanly"
    break
  fi

  if [ "${BLOCK_ALERT_SENT:-0}" -eq 0 ] && [ "$INITIAL_READY" -gt 0 ] && [ "$BLOCK_ALERT_PCT" -gt 0 ]; then
    PCT=$(( BLOCKED * 100 / INITIAL_READY ))
    if [ "$PCT" -ge "$BLOCK_ALERT_PCT" ]; then
      log "⚠ block-rate alert: ${BLOCKED}/${INITIAL_READY} (${PCT}%)"
      BLOCK_ALERT_SENT=1
    fi
  fi

  PROGRESS=0

  # ACTIVE_WORKERS already computed at top of loop (free pre-check).
  can_dispatch() {
    [ "$ACTIVE_WORKERS" -lt "$MAX_WORKERS" ]
  }

  # There is no "eligible for the requested lane". The runtime hands back the
  # cards that may be dispatched AT ALL — status exactly Ready, open issue, not
  # excluded, not claimed — and the variant decides which lane carries them in.
  # A worker then carries its own card forward through its lifecycle; the
  # dispatcher never re-picks a card out of QA or Review.
  ENTRY_LANE=build
  [ "$VARIANT" = "qa-only" ] && ENTRY_LANE=qa
  ENTRY_LANE_IDLE=$BUILD_IDLE
  [ "$ENTRY_LANE" = "qa" ] && ENTRY_LANE_IDLE=$QA_IDLE

  if can_dispatch && [ "$ENTRY_LANE_IDLE" -eq 1 ]; then
    card=$(next_dispatchable_card)
    if [ -n "${card:-}" ]; then
      dispatch_lane "$ENTRY_LANE" "$card"
      PROGRESS=1
      ACTIVE_WORKERS=$((ACTIVE_WORKERS + 1))
    fi
  fi

  if [ "${DOWNSTREAM_NOTE_SENT:-0}" -eq 0 ] && { [ "$QA" -gt 0 ] || [ "$REVIEW" -gt 0 ]; }; then
    log "note — ${QA} QA / ${REVIEW} Review card(s) present. Only status 'Ready' is dispatchable; a lane worker carries its own card forward. Cards parked downstream are not re-dispatched."
    DOWNSTREAM_NOTE_SENT=1
  fi

  if [ "$PROGRESS" -eq 0 ]; then
    if [ "$BUILD_IDLE" -eq 0 ] || [ "$QA_IDLE" -eq 0 ] || [ "$REVIEW_IDLE" -eq 0 ]; then
      NO_PROGRESS_TICKS=0
    else
      NO_PROGRESS_TICKS=$((NO_PROGRESS_TICKS + 1))
      if [ "$NO_PROGRESS_TICKS" -ge 3 ]; then
        log "🛑 halt — no card progressed for 3 ticks while all lanes idle (dispatches=${TOTAL_DISPATCHES}, reaps=${TOTAL_REAPS})"
        break
      fi
    fi
  else
    NO_PROGRESS_TICKS=0
  fi

  sleep "$TICK_SECONDS"
done

log "super-board run finished. manifest: $RUN_MANIFEST"
publish_run_manifest || true
