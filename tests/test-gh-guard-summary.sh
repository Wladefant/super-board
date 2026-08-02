#!/usr/bin/env bash
# Tests scripts/super-board-gh-guard.sh::sb_gh_guard_summary. No gh writes.
#
# `rate-limit-etiquette.md` §8 and `run.md` both tell every worker to call this
# function to produce the `gh-quota-on-exit:` line that ends its PR handoff
# comment. The function used to run the quota check with BOTH streams sent to
# /dev/null and then `return 0`, so it emitted nothing at all and the promise
# was unkeepable. What these scenarios pin:
#
#   • the documented line is actually produced, on stderr;
#   • only the safe fields appear — no token, header, cookie, or raw payload;
#   • an unreadable quota degrades to a clear unavailable marker, not silence;
#   • the call is NON-FATAL: it never changes a worker's exit status, even
#     under `set -e`, and even when the quota cannot be read.
set -euo pipefail
cd "$(dirname "$0")"

GUARD="../scripts/super-board-gh-guard.sh"
TMP=".tmp-gh-guard-summary"   # repo-relative: a native python.exe cannot open /tmp/... on MSYS

fail() { echo "FAIL: $1" >&2; exit 1; }
rm -rf "$TMP"; mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT

# A rate_limit payload with a known GraphQL balance. reset is a fixed epoch so
# the rendered `reset=` field is deterministic.
cat > "$TMP/rate-limit.json" <<'EOF'
{ "resources": { "graphql": { "limit": 5000, "remaining": 4200, "reset": 1780000000 } } }
EOF
cat > "$TMP/rate-limit-broken.json" <<'EOF'
{ "resources": {} }
EOF

# The documented shape, either populated or explicitly unavailable.
LINE_RE='^gh-quota-on-exit: (graphql=[0-9]+ floor=[0-9]+ reset=[^ ]+|unavailable )'

# ── Scenario 1 — the summary line is emitted on stderr, and only on stderr ──
# shellcheck source=../scripts/super-board-gh-guard.sh
. "$GUARD"

set +e
OUT=$(sb_gh_guard_summary 2>"$TMP/err.txt")
RC=$?
set -e
ERR=$(cat "$TMP/err.txt")

[ "$RC" -eq 0 ] || fail "a summary call must never change the exit status, got $RC"
[ -n "$ERR" ] || fail "sb_gh_guard_summary emitted nothing; the documented gh-quota-on-exit line is missing"
echo "$ERR" | grep -Eq "$LINE_RE" \
  || fail "stderr does not carry the documented line, got: $ERR"
[ "$(echo "$ERR" | grep -c 'gh-quota-on-exit:')" -eq 1 ] \
  || fail "exactly one summary line is expected, got: $ERR"
[ -z "$OUT" ] || fail "the summary is a log line and belongs on stderr, but stdout carried: $OUT"

# ── Scenario 2 — only the safe fields, never a credential or a raw payload ──
for forbidden in token Authorization Bearer cookie Cookie x-api-key resources limit; do
  echo "$ERR" | grep -qi "$forbidden" \
    && fail "the summary leaked a field it may never log ($forbidden): $ERR"
done
echo "$ERR" | grep -q 'estimated_cost' \
  && fail "the exit summary reports no cost estimate; it is not spending anything: $ERR"

# ── Scenario 3 — non-fatal under `set -e`, even when nothing can be read ──
# The harshest available case: the runtime itself is unreachable, so no quota
# reading is possible at all. A worker calling this on its way out must still
# reach its own exit, and must still see a line.
cat > "$TMP/exit-path.sh" <<EOF
set -euo pipefail
SB_PYTHON="/nonexistent-interpreter-for-this-test"
. "$PWD/$GUARD"
sb_gh_guard_summary 2>"$PWD/$TMP/err-nogh.txt"
echo "WORKER-REACHED-EXIT"
EOF
set +e
NOGH=$(bash "$TMP/exit-path.sh" 2>/dev/null)
NOGH_RC=$?
set -e
[ "$NOGH_RC" -eq 0 ] || fail "an unreadable quota must not fail the worker, got $NOGH_RC"
echo "$NOGH" | grep -q 'WORKER-REACHED-EXIT' \
  || fail "the worker never reached its own exit after calling the summary"
NOGH_ERR=$(cat "$TMP/err-nogh.txt")
echo "$NOGH_ERR" | grep -q 'gh-quota-on-exit: unavailable' \
  || fail "an unreadable quota must degrade to a clear unavailable marker, got: $NOGH_ERR"

# ── Scenario 4 — a readable quota renders the real numbers ──
# Drives the same runtime command the guard runs, with a fixture payload, so the
# rendered fields are deterministic on every platform.
# shellcheck source=../scripts/super-board-python.sh
. "../scripts/super-board-python.sh"
set +e
READABLE=$(sb_runtime super_board_runtime.quota summary \
  --payload "$(sb_native_path "$PWD/$TMP/rate-limit.json")" 2>&1)
READABLE_RC=$?
set -e
[ "$READABLE_RC" -eq 0 ] || fail "a summary must exit 0 even so, got $READABLE_RC"
echo "$READABLE" | grep -q 'gh-quota-on-exit: graphql=4200 floor=1000 reset=' \
  || fail "a readable quota must render its real fields, got: $READABLE"
echo "$READABLE" | grep -q 'reset=2026-05-28T20:26:40Z' \
  || fail "the reset time must be the RFC 3339 rendering of the payload, got: $READABLE"

# ── Scenario 5 — a malformed payload is unavailable, not a fabricated balance ──
set +e
BROKEN=$(sb_runtime super_board_runtime.quota summary \
  --payload "$(sb_native_path "$PWD/$TMP/rate-limit-broken.json")" 2>&1)
BROKEN_RC=$?
set -e
[ "$BROKEN_RC" -eq 0 ] || fail "a malformed payload must still not be fatal, got $BROKEN_RC"
echo "$BROKEN" | grep -q 'gh-quota-on-exit: unavailable' \
  || fail "a malformed payload must read as unavailable, got: $BROKEN"

# ── Scenario 6 — the guard has a real `check` entrypoint when EXECUTED ──
# `payload/github/workflows/auto-add-to-project.yml` runs
# `bash .claude/bin/super-board-gh-guard.sh check` from Python with check=True.
# A script that only defines functions exits 0 on any argument, so the reserve
# was reported as respected without ever being consulted.
cat > "$TMP/affordable.json" <<'EOF'
{ "resources": { "graphql": { "limit": 5000, "remaining": 4200, "reset": 1780000000 } } }
EOF
cat > "$TMP/exhausted.json" <<'EOF'
{ "resources": { "graphql": { "limit": 5000, "remaining": 900, "reset": 1780000000 } } }
EOF

set +e
bash "$GUARD" check 100 --payload "$(sb_native_path "$PWD/$TMP/affordable.json")" >/dev/null 2>&1
AFFORD_RC=$?
set -e
[ "$AFFORD_RC" -eq 0 ] || fail "an affordable burst must pass the executed guard, got $AFFORD_RC"

set +e
bash "$GUARD" check 100 --payload "$(sb_native_path "$PWD/$TMP/exhausted.json")" >/dev/null 2>&1
EXHAUST_RC=$?
set -e
[ "$EXHAUST_RC" -eq 75 ] || fail "a burst that breaks the reserve must exit 75, got $EXHAUST_RC"

set +e
bash "$GUARD" >/dev/null 2>&1; NOARG_RC=$?
bash "$GUARD" definitely-not-a-command >/dev/null 2>&1; UNKNOWN_RC=$?
set -e
[ "$NOARG_RC" -eq 64 ] || fail "the executed guard needs a command, expected 64, got $NOARG_RC"
[ "$UNKNOWN_RC" -eq 64 ] || fail "an unknown command is an invalid invocation (64), got $UNKNOWN_RC"

# ── Scenario 7 — the workflow's own invocation reaches that entrypoint ──
# The unit test for `evaluate_fallback_auto_add` injects a Python callback, so
# it cannot see a workflow that shells out to a command that does not exist.
WORKFLOW="../payload/github/workflows/auto-add-to-project.yml"
INVOCATION=$(grep -o '"bash", "[^"]*super-board-gh-guard.sh"[^]]*' "$WORKFLOW" || true)
[ -n "$INVOCATION" ] || fail "the fallback workflow no longer invokes the gh guard at all"
echo "$INVOCATION" | grep -q '"check"' \
  || fail "the workflow must call the guard's real check entrypoint, got: $INVOCATION"
echo "$INVOCATION" | grep -Eq '"[0-9]+"' \
  || fail "the workflow must pass an estimated cost to the guard, got: $INVOCATION"

# ── Scenario 8 — the worker budget file is not a guessable shared-temp path ──
# It used to be `${TMPDIR:-/tmp}/super-board-gh-budget-$$`. A PID is guessable,
# so anyone else on the machine can create that name first — as a symlink to
# something this script then truncates — and can read a worker's state at will.
cat > "$TMP/state-path.sh" <<EOF
set -euo pipefail
. "$PWD/$GUARD"
sb_gh_budget_init 12
printf '%s\n' "\$SB_GH_GUARD_STATE_FILE"
EOF
STATE_PATH=$(bash "$TMP/state-path.sh")
[ -n "$STATE_PATH" ] || fail "the guard reports no budget state file at all"
case "$STATE_PATH" in
  *super-board-gh-budget-[0-9]*) fail "the budget file is still a PID-predictable path: $STATE_PATH" ;;
esac

# Created 0600, and gone once the worker exits.
cat > "$TMP/state-mode.sh" <<EOF
set -euo pipefail
trap sb_tmp_cleanup EXIT
. "$PWD/$GUARD"
trap sb_tmp_cleanup EXIT
sb_gh_budget_init 12
ls -l "\$SB_GH_GUARD_STATE_FILE" | cut -c1-10
printf '%s\n' "\$SB_GH_GUARD_STATE_FILE"
EOF
MODE_OUT=$(bash "$TMP/state-mode.sh")
MODE=$(echo "$MODE_OUT" | head -1)
LEFTOVER=$(echo "$MODE_OUT" | tail -1)
case "$MODE" in
  -rw-------*) ;;
  *) echo "note: filesystem does not report POSIX modes ($MODE); skipping the 0600 assertion" >&2 ;;
esac
[ -e "$LEFTOVER" ] && fail "the budget file survived the worker's exit: $LEFTOVER"

# ── Scenario 9 — a rendered config is actually removed, spaces and all ──
# Two bugs in one line. `sb_tmp_cleanup` iterated an unquoted space-delimited
# STRING, so a path under `dir with spaces/` split into three nonexistent paths
# and `rm -f` removed none of them. And every real caller runs `sb_config_file`
# inside a pipeline AND a command substitution — `payload=$(jq … |
# sb_config_file)` — both subshells, so the registration died with the subshell
# and nothing was ever cleaned up at all. What was left behind is a rendered
# publication payload, which carries the caller's environment.
#
# The invocation below is the real one: a pipeline, under a TMPDIR with a space.
SPACED="$PWD/$TMP/dir with spaces"
mkdir -p "$SPACED"
cat > "$TMP/spaced.sh" <<EOF
set -euo pipefail
export TMPDIR="$SPACED"
. "$PWD/../scripts/super-board-python.sh"
PAYLOAD=\$(printf 'secret-bearing config\n' | sb_config_file)
[ -n "\$PAYLOAD" ] || { echo "sb_config_file produced no path" >&2; exit 1; }
find "$SPACED" -type f | wc -l | tr -d ' '
sb_tmp_cleanup
EOF
CREATED=$(bash "$TMP/spaced.sh")
[ "$CREATED" -ge 1 ] || fail "the scenario never created a temp file to clean up"
REMAINING=$(find "$SPACED" -mindepth 1 | wc -l | tr -d ' ')
[ "$REMAINING" -eq 0 ] \
  || fail "a temp file created in a subshell under a spaced path survived cleanup ($REMAINING left in $SPACED)"

# ── Scenario 10 — one cached quota inventory per cycle, not one per check ──
# `rate-limit-etiquette.md` §3 and `quota.py` both say the quota is read ONCE per
# cycle and every check inside it reuses that reading, because a guard that polls
# before every call becomes the thing that drains the bucket. The worker guard
# shelled out to the runtime on every `sb_gh_guard_check`, and the runtime ran
# `gh api rate_limit` every time.
FAKE_BIN="$PWD/$TMP/fake-gh"
cat > "$FAKE_BIN" <<EOF
#!/usr/bin/env bash
echo call >> "$PWD/$TMP/gh-calls.txt"
cat "$PWD/$TMP/rate-limit.json"
EOF
chmod +x "$FAKE_BIN"
: > "$TMP/gh-calls.txt"

cat > "$TMP/one-inventory.sh" <<EOF
set -euo pipefail
export SUPERBOARD_GH="$FAKE_BIN"
. "$PWD/$GUARD"
trap sb_tmp_cleanup EXIT
sb_gh_guard_begin_cycle
sb_gh_guard_check 100
sb_gh_guard_check 100
sb_gh_guard_check 100
EOF
bash "$TMP/one-inventory.sh" >/dev/null 2>&1 \
  || fail "three affordable checks in one cycle must all pass"
CALLS=$(wc -l < "$TMP/gh-calls.txt" | tr -d ' ')
[ "$CALLS" -eq 1 ] \
  || fail "three checks in one cycle read the quota $CALLS times; the contract is one"

# A new cycle takes a new reading — the cache is per cycle, not forever.
: > "$TMP/gh-calls.txt"
cat > "$TMP/two-cycles.sh" <<EOF
set -euo pipefail
export SUPERBOARD_GH="$FAKE_BIN"
. "$PWD/$GUARD"
trap sb_tmp_cleanup EXIT
sb_gh_guard_begin_cycle; sb_gh_guard_check 100
sb_gh_guard_begin_cycle; sb_gh_guard_check 100
EOF
bash "$TMP/two-cycles.sh" >/dev/null 2>&1 || fail "two cycles of affordable checks must pass"
CYCLE_CALLS=$(wc -l < "$TMP/gh-calls.txt" | tr -d ' ')
[ "$CYCLE_CALLS" -eq 2 ] \
  || fail "two cycles must take two readings, got $CYCLE_CALLS"

# The cache never softens a refusal, and an explicit --payload still wins.
cat > "$TMP/cached-refusal.sh" <<EOF
set -euo pipefail
export SUPERBOARD_GH="$FAKE_BIN"
. "$PWD/$GUARD"
trap sb_tmp_cleanup EXIT
sb_gh_guard_begin_cycle
sb_gh_guard_check 100 --payload "\$(sb_native_path "$PWD/$TMP/exhausted.json")"
EOF
set +e
bash "$TMP/cached-refusal.sh" >/dev/null 2>&1
CACHED_RC=$?
set -e
[ "$CACHED_RC" -eq 75 ] \
  || fail "an explicit --payload must outrank the cycle cache, expected 75, got $CACHED_RC"

echo "PASS: test-gh-guard-summary.sh (10 scenarios)"
