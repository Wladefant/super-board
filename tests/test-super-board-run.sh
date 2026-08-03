#!/usr/bin/env bash
# Tests the dispatcher's fail-closed and lifecycle guarantees in
# scripts/super-board-run.sh. No real gh, no real claude, no network.
#
# The script is sourced with SB_RUN_LIB_ONLY=1, which defines every function and
# returns before the preconditions — so each guarantee can be exercised on its
# own instead of only as an emergent property of a 120-second loop.
#
# What these scenarios pin:
#   • an unreadable board is never mistaken for an empty board;
#   • an issue lock exists BEFORE a worker does, and is removed if it never does;
#   • an assignee add is not a mutex — the claim is verified, and a lost race
#     releases what it added;
#   • INT/TERM stop the workers and release the claims and locks they hold.
set -euo pipefail
cd "$(dirname "$0")"

RUN_SCRIPT="$(cd .. && pwd)/scripts/super-board-run.sh"
TMP="$(pwd)/.tmp-super-board-run"   # repo-relative: MSYS /tmp is not openable natively

fail() { echo "FAIL: $1" >&2; exit 1; }
rm -rf "$TMP"; mkdir -p "$TMP/bin" "$TMP/work"
trap 'rm -rf "$TMP"' EXIT

BOT="super-board-bot"

# ── A project the dispatcher can read: config on disk, inflight dir, manifest dir.
mkdir -p "$TMP/work/.claude/super-board/configs" "$TMP/work/.claude/super-board/inflight" \
         "$TMP/work/docs/super-board/runs"
cat > "$TMP/work/.claude/super-board/configs/testboard.json" <<EOF
{
  "version": 1,
  "variant": "full",
  "activation_mode": "active",
  "base_branch": "staging",
  "worker_backend": "claude-p",
  "max_workers": 3,
  "project": { "owner": "test-owner", "number": 99 },
  "repo": { "remote": "test-owner/test-repo" },
  "bot_identity": "$BOT"
}
EOF

# ── Stubs. Each reads its scripted behaviour from files under $TMP so a scenario
#    can change it without rewriting the stub.
cat > "$TMP/bin/gh" <<STUB
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$TMP/gh-calls.txt"
if [ "\$1" = project ]; then
  cat "$TMP/gh-project-out" 2>/dev/null || true
  exit "\$(cat "$TMP/gh-project-rc" 2>/dev/null || echo 0)"
fi
if [ "\$1" = issue ] && [ "\$2" = view ]; then
  cat "$TMP/gh-assignees" 2>/dev/null || true
  exit "\$(cat "$TMP/gh-view-rc" 2>/dev/null || echo 0)"
fi
exit "\$(cat "$TMP/gh-edit-rc" 2>/dev/null || echo 0)"
STUB
cat > "$TMP/bin/claude" <<STUB
#!/usr/bin/env bash
# Records what the inflight directory looked like the instant the worker began.
ls .claude/super-board/inflight > "$TMP/claude-saw-locks.txt" 2>&1 || true
sleep "\$(cat "$TMP/claude-sleep" 2>/dev/null || echo 5)"
STUB
chmod +x "$TMP/bin/gh" "$TMP/bin/claude"
export PATH="$TMP/bin:$PATH"

echo '{"items":[]}' > "$TMP/gh-project-out"
echo 0 > "$TMP/gh-project-rc"
echo 0 > "$TMP/gh-edit-rc"
echo 0 > "$TMP/gh-view-rc"
printf '%s\n' "$BOT" > "$TMP/gh-assignees"
: > "$TMP/gh-calls.txt"

cd "$TMP/work"
# shellcheck disable=SC1090
SB_RUN_LIB_ONLY=1 . "$RUN_SCRIPT" testboard \
  || fail "the dispatcher could not be sourced in library mode"

INFLIGHT=".claude/super-board/inflight"

reset_state() { rm -f "$INFLIGHT"/*; : > "$TMP/gh-calls.txt"; }

# ── Scenario 1 — an unreadable board is not an empty board.
echo 1 > "$TMP/gh-project-rc"
RC=0; fetch_project_items || RC=$?
[ "$RC" -ne 0 ] || fail "a failed board read must not report success"
[ "$PROJECT_ITEMS_JSON" != '{"items":[]}' ] \
  || fail "a failed board read was turned into an empty board — the runner would exit 'done'"
echo 0 > "$TMP/gh-project-rc"

# ── Scenario 2 — unparseable board output is refused too.
printf 'not json at all\n' > "$TMP/gh-project-out"
RC=0; fetch_project_items || RC=$?
[ "$RC" -ne 0 ] || fail "unparseable board output must fail closed"
echo '{"items":[]}' > "$TMP/gh-project-out"

# ── Scenario 3 — a readable board still succeeds.
RC=0; fetch_project_items || RC=$?
[ "$RC" -eq 0 ] || fail "a readable board must succeed, got $RC"
[ "$PROJECT_ITEMS_JSON" = '{"items":[]}' ] || fail "the readable board payload was not cached"

# ── Scenario 3b — a board read that came back exactly at the cap SAYS SO.
#    The per-tick read stays capped on purpose (a second query per tick is the
#    quota tax rate-limit-etiquette.md §5 removed), so the cap has to be
#    announced: cards past it are not being considered, and silence about that
#    reads as a scan over the whole board.
PROJECT_ITEM_LIMIT=3
printf '{"items":[{"id":"a"},{"id":"b"},{"id":"c"}]}\n' > "$TMP/gh-project-out"
# Redirected, never `$(...)`: a command substitution runs in a subshell and the
# flag this scenario reads would die with it.
fetch_project_items > "$TMP/cap.log" 2>&1 || fail "a full page is still a readable board"
[ "$PROJECT_ITEMS_TRUNCATED" -eq 1 ] || fail "a page at the cap must be flagged truncated"
grep -q "TRUNCATED" "$TMP/cap.log" \
  || fail "the cap must be announced in the run log, got: $(cat "$TMP/cap.log")"

printf '{"items":[{"id":"a"},{"id":"b"}]}\n' > "$TMP/gh-project-out"
fetch_project_items > "$TMP/under.log" 2>&1 || fail "a short page is a readable board"
[ "$PROJECT_ITEMS_TRUNCATED" -eq 0 ] || fail "a page under the cap is not truncated"
grep -q "TRUNCATED" "$TMP/under.log" \
  && fail "a complete read must not claim truncation"
PROJECT_ITEM_LIMIT=500
echo '{"items":[]}' > "$TMP/gh-project-out"
fetch_project_items || fail "board read must still work after the cap scenarios"

# ── Scenario 4 — the claim is VERIFIED. GitHub issues accept several assignees,
#    so `--add-assignee` succeeding proves nothing about who owns the card.
reset_state
printf '%s\n' "$BOT,someone-else" > "$TMP/gh-assignees"
RC=0; try_claim_assignee 12 || RC=$?
[ "$RC" -ne 0 ] || fail "a card assigned to someone else must not be claimed"
grep -q -- "--remove-assignee" "$TMP/gh-calls.txt" \
  || fail "a lost claim race must release the assignee it just added"

# ── Scenario 5 — an uncontested claim wins.
reset_state
printf '%s\n' "$BOT" > "$TMP/gh-assignees"
RC=0; try_claim_assignee 12 || RC=$?
[ "$RC" -eq 0 ] || fail "an uncontested claim should win, got $RC"
grep -q -- "--remove-assignee" "$TMP/gh-calls.txt" \
  && fail "an uncontested claim must not release itself"

# ── Scenario 6 — an unverifiable claim is refused, not assumed won.
reset_state
echo 1 > "$TMP/gh-view-rc"
RC=0; try_claim_assignee 12 || RC=$?
[ "$RC" -ne 0 ] || fail "an unverifiable claim must be refused"
echo 0 > "$TMP/gh-view-rc"

# ── Scenario 7 — the lock exists BEFORE the worker does.
#    A worker spawned before its lock is written is a worker a second dispatcher
#    can duplicate, and a crash in that window leaves it untracked forever.
reset_state
rm -f "$TMP/claude-saw-locks.txt"
echo 3 > "$TMP/claude-sleep"
ELIGIBLE_CARDS_JSON='[{"number":12,"selected_base_branch":"staging","issue_url":"https://github.com/test-owner/test-repo/issues/12"}]'
dispatch_lane build 12
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -s "$TMP/claude-saw-locks.txt" ] && break
  sleep 0.5
done
grep -q '^12$' "$TMP/claude-saw-locks.txt" 2>/dev/null \
  || fail "the worker started before its lock existed: $(cat "$TMP/claude-saw-locks.txt" 2>/dev/null)"
grep -q '^PID=[0-9][0-9]*$' "$INFLIGHT/12" \
  || fail "the lock must carry the worker PID once it is known: $(cat "$INFLIGHT/12")"
kill "$BUILD_PID" 2>/dev/null || true
BUILD_PID=""; BUILD_ISSUE=""

# ── Scenario 8 — a refused dispatch leaves no lock behind.
reset_state
ELIGIBLE_CARDS_JSON='[{"number":13,"issue_url":"https://github.com/test-owner/test-repo/issues/13"}]'
dispatch_lane build 13   # no selected_base_branch → routing refused it
[ ! -f "$INFLIGHT/13" ] || fail "a refused dispatch left a lock behind"

# ── Scenario 9 — INT and TERM are handled, not just EXIT. A dispatcher that
#    only cleans temp files on a signal leaves workers, locks and claims alive.
trap -p INT | grep -q . || fail "no INT handler is installed"
trap -p TERM | grep -q . || fail "no TERM handler is installed"

# ── Scenario 10 — stopping releases every issue lock and claim it owns, kills
#    the worker, and does not touch the workflow backend's lock.
reset_state
printf 'SLUG=other\n' > "$INFLIGHT/workflow-wave.lock"
sleep 30 & STOP_PID=$!
printf 'PID=%s\nLANE=build\nSTARTED=x\n' "$STOP_PID" > "$INFLIGHT/21"
stop_and_release_in_flight
[ ! -f "$INFLIGHT/21" ] || fail "an owned issue lock survived the stop"
[ -f "$INFLIGHT/workflow-wave.lock" ] || fail "the workflow-wave lock is not ours to delete"
kill -0 "$STOP_PID" 2>/dev/null && fail "the in-flight worker survived the stop"
grep -q -- "--remove-assignee" "$TMP/gh-calls.txt" || fail "the claim was not released on stop"
rm -f "$INFLIGHT/workflow-wave.lock"

# ── Scenario 11 — the tick's six status counters come from ONE scan.
#    Every counter used to run its own `jq` pass over the same cached board, so
#    a 500-card board was walked six times a tick for numbers one walk produces.
reset_state
cat > "$TMP/gh-project-out" <<'BOARD'
{"items":[
 {"status":"Ready","content":{"number":1}},
 {"status":"Ready","content":{"number":2}},
 {"status":"Building","content":{"number":3}},
 {"status":"QA","content":{"number":4}},
 {"status":"Review","content":{"number":5}},
 {"status":"Blocked","content":{"number":6}},
 {"status":"Done","content":{"number":7}},
 {"status":"Done","content":{"number":8}}
]}
BOARD
fetch_project_items || fail "the board fixture could not be read"
[ "$(column_count Ready)"    = "2" ] || fail "Ready counted $(column_count Ready), expected 2"
[ "$(column_count Building)" = "1" ] || fail "Building counted $(column_count Building)"
[ "$(column_count QA)"       = "1" ] || fail "QA counted $(column_count QA)"
[ "$(column_count Review)"   = "1" ] || fail "Review counted $(column_count Review)"
[ "$(column_count Blocked)"  = "1" ] || fail "Blocked counted $(column_count Blocked)"
[ "$(column_count Done)"     = "2" ] || fail "Done counted $(column_count Done)"
[ "$(column_count Backlog)"  = "0" ] || fail "an absent column must count 0, not empty"

# The counting itself happens once, inside the fetch. `column_count` reads the
# map — it must not be able to walk the board again.
grep -q 'PROJECT_STATUS_COUNTS' "$RUN_SCRIPT" \
  || fail "the per-tick status-count map is gone"
COLUMN_BODY=$(sed -n '/^column_count() {/,/^}/p' "$RUN_SCRIPT")
echo "$COLUMN_BODY" | grep -q 'jq' \
  && fail "column_count still scans the board itself: $COLUMN_BODY"
echo '{"items":[]}' > "$TMP/gh-project-out"

echo "PASS: test-super-board-run.sh (12 scenarios)"
