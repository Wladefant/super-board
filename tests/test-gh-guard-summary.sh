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

echo "PASS: test-gh-guard-summary.sh (7 scenarios)"
