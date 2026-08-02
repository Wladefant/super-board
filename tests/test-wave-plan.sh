#!/usr/bin/env bash
# Tests super-board-wave-plan.sh against fixtures. No gh calls, no gh writes.
#
# The planner shares ONE eligibility implementation with the dispatcher
# (scripts/super_board_runtime/eligibility.py), so these scenarios pin the same
# contract the dispatcher obeys: only OPEN, unassigned, unexcluded issue cards
# whose status is exactly `Ready` are ever planned.
set -euo pipefail
cd "$(dirname "$0")"
PLAN="../scripts/super-board-wave-plan.sh"

fail() { echo "FAIL: $1" >&2; exit 1; }

OUT=$("$PLAN" --config fixtures/wave-config.json --items fixtures/wave-items.json)

# Scenario 1 — only Ready cards, board order, assigned #11 skipped as claimed
echo "$OUT" | jq -e '[.cards[].number] == [12,14]' >/dev/null || fail "expected Ready cards 12,14, got: $OUT"
echo "$OUT" | jq -e '[.cards[].status] == ["Ready","Ready"]' >/dev/null || fail "planned cards must all be Ready, got: $OUT"
echo "$OUT" | jq -e '[.decisions[] | select(.issue_number == 11) | .reason_codes[]] == ["already-claimed"]' >/dev/null \
  || fail "assigned #11 should be rejected as already-claimed, got: $OUT"

# Scenario 2 — downstream columns are NOT dispatchable: Review #10 and QA #13 rejected
echo "$OUT" | jq -e '[.decisions[] | select(.issue_number == 10) | .reason_codes[]] == ["status-not-ready"]' >/dev/null \
  || fail "Review #10 must be rejected with status-not-ready, got: $OUT"
echo "$OUT" | jq -e '[.decisions[] | select(.issue_number == 13) | .reason_codes[]] == ["status-not-ready"]' >/dev/null \
  || fail "QA #13 must be rejected with status-not-ready, got: $OUT"
echo "$OUT" | jq -e '[.decisions[] | select(.issue_number == 9) | .reason_codes[]] == ["status-not-ready"]' >/dev/null \
  || fail "Done #9 must be rejected with status-not-ready, got: $OUT"

# Scenario 3 — the lane variant does not change eligibility
QA_ONLY=$(jq '.variant = "qa-only"' fixtures/wave-config.json)
OUT2=$("$PLAN" --config <(echo "$QA_ONLY") --items fixtures/wave-items.json)
echo "$OUT2" | jq -e '[.cards[].number] == [12,14]' >/dev/null || fail "qa-only should select the same Ready cards, got: $OUT2"

# Scenario 4 — max_workers cap
CAPPED=$(jq '.max_workers = 1' fixtures/wave-config.json)
OUT3=$("$PLAN" --config <(echo "$CAPPED") --items fixtures/wave-items.json)
echo "$OUT3" | jq -e '[.cards[].number] == [12]' >/dev/null || fail "cap=1 should keep only Ready #12, got: $OUT3"

# Scenario 5 — excluded labels are never planned (design + history, trimmed + case-folded)
OUT4=$("$PLAN" --config fixtures/wave-config.json --items fixtures/wave-items-excluded.json)
echo "$OUT4" | jq -e '[.cards[].number] == [42]' >/dev/null || fail "only #42 is dispatchable, got: $OUT4"
for n in 40 41 43; do
  echo "$OUT4" | jq -e --argjson n "$n" '[.decisions[] | select(.issue_number == $n) | .reason_codes[]] == ["excluded-label"]' >/dev/null \
    || fail "#$n should be rejected with excluded-label, got: $OUT4"
done

# Scenario 6 — pull-request cards can never dispatch
echo "$OUT4" | jq -e '[.decisions[] | select(.issue_number == 44) | .reason_codes[]] == ["content-type-not-issue"]' >/dev/null \
  || fail "PR card #44 should be rejected with content-type-not-issue, got: $OUT4"

# Scenario 7 — invalid variant fails loudly (exit 65), never silently qa-only
BAD=$(jq '.variant = "fulll"' fixtures/wave-config.json)
RC=0; "$PLAN" --config <(echo "$BAD") --items fixtures/wave-items.json >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 65 ] || fail "invalid variant should exit 65, got $RC"

# Scenario 8 — the planner enforces the config contract: `columns` is gone
COLS=$(jq '.columns = ["Ready","QA","Review"]' fixtures/wave-config.json)
RC=0; "$PLAN" --config <(echo "$COLS") --items fixtures/wave-items.json >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 65 ] || fail "a config carrying 'columns' should exit 65, got $RC"

# Scenario 9 — empty board → cards:[] (run-workflow.md done condition depends on this shape)
OUT9=$("$PLAN" --config fixtures/wave-config.json --items <(echo '{"items":[]}'))
echo "$OUT9" | jq -e '.cards == []' >/dev/null || fail "empty board should yield cards:[], got: $OUT9"

# Scenario 10 — activation off dispatches nothing, however perfect the board is
OFF=$(jq '.activation_mode = "off"' fixtures/wave-config.json)
OUT10=$("$PLAN" --config <(echo "$OFF") --items fixtures/wave-items.json)
echo "$OUT10" | jq -e '.cards == []' >/dev/null || fail "activation off must plan nothing, got: $OUT10"
echo "$OUT10" | jq -e '[.decisions[] | select(.issue_number == 12) | .reason_codes[]] == ["activation-off"]' >/dev/null \
  || fail "an otherwise-eligible card should be refused with activation-off, got: $OUT10"

# Scenario 11 — proof-only plans at most the single allowlisted issue
PROOF=$(jq '.activation_mode = "proof-only"
            | .repo = {"remote":"test-owner/test-repo"}
            | .proof_issue_url = "https://github.com/test-owner/test-repo/issues/14"' fixtures/wave-config.json)
OUT11=$("$PLAN" --config <(echo "$PROOF") --items fixtures/wave-items.json)
echo "$OUT11" | jq -e '[.cards[].number] == [14]' >/dev/null || fail "proof-only should plan only #14, got: $OUT11"
echo "$OUT11" | jq -e '[.decisions[] | select(.issue_number == 12) | .reason_codes[]] == ["activation-not-allowlisted"]' >/dev/null \
  || fail "#12 is not allowlisted and should say so, got: $OUT11"

# Scenario 12 — proof-only with a URL outside the configured repository is a config error
BADPROOF=$(jq '.activation_mode = "proof-only"
               | .repo = {"remote":"test-owner/test-repo"}
               | .proof_issue_url = "https://github.com/someone-else/other/issues/14"' fixtures/wave-config.json)
RC=0; "$PLAN" --config <(echo "$BADPROOF") --items fixtures/wave-items.json >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 65 ] || fail "a proof URL outside the configured repo should exit 65, got $RC"

echo "PASS: test-wave-plan.sh (12 scenarios)"
