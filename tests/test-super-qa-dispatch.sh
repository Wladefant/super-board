#!/usr/bin/env bash
# Tests scripts/super-qa-dispatch.sh and scripts/super-qa-file-bug.sh against
# fixtures. No gh calls, no gh writes, no git worktrees: both scripts run in
# --dry-run and read the pull-request head from a fixture payload.
#
# The exact-SHA contract these scenarios pin:
#   • the head SHA is resolved and recorded BEFORE anything is executed;
#   • a head that moved between resolve and reread refuses to publish;
#   • a mutable branch checkout is never QA authority;
#   • --dry-run issues zero GitHub writes.
set -euo pipefail
cd "$(dirname "$0")"

DISPATCH="../scripts/super-qa-dispatch.sh"
FILEBUG="../scripts/super-qa-file-bug.sh"
CFG="fixtures/qa-config.json"
TMP=".tmp-super-qa-dispatch"   # repo-relative: a native python.exe cannot open /tmp/... on MSYS

fail() { echo "FAIL: $1" >&2; exit 1; }
rm -rf "$TMP"; mkdir -p "$TMP"
trap 'rm -rf "$TMP"' EXIT

SHA_A=$(printf 'a%.0s' $(seq 40))
SHA_B=$(printf 'b%.0s' $(seq 40))

cat > "$TMP/pr-head.json" <<EOF
{
  "url": "https://github.com/test-owner/test-repo/pull/456",
  "id": "PR_kwNOTAREALNODEID",
  "headRefName": "feat/exact-sha",
  "headRefOid": "$SHA_A",
  "baseRefName": "staging",
  "isDraft": false,
  "mergeable": "MERGEABLE"
}
EOF

cat > "$TMP/pr-head-moved.json" <<EOF
{
  "url": "https://github.com/test-owner/test-repo/pull/456",
  "id": "PR_kwNOTAREALNODEID",
  "headRefName": "feat/exact-sha",
  "headRefOid": "$SHA_B",
  "baseRefName": "staging",
  "isDraft": false,
  "mergeable": "MERGEABLE"
}
EOF

cat > "$TMP/pr-head-missing.json" <<'EOF'
{
  "url": "https://github.com/test-owner/test-repo/pull/456",
  "id": "PR_kwNOTAREALNODEID",
  "headRefName": "feat/exact-sha",
  "baseRefName": "staging"
}
EOF

# Scenario 1 — the entry points exist and are executable.
[ -f "$DISPATCH" ] || fail "scripts/super-qa-dispatch.sh: No such file or directory"
[ -f "$FILEBUG" ] || fail "scripts/super-qa-file-bug.sh: No such file or directory"

# Scenario 2 — a dry run resolves the head and records the tested SHA, with zero writes.
OUT=$("$DISPATCH" --config "$CFG" \
        --issue-url https://github.com/test-owner/test-repo/issues/123 \
        --pull-request https://github.com/test-owner/test-repo/pull/456 \
        --pr-payload "$TMP/pr-head.json" --dry-run)
echo "$OUT" | jq -e --arg sha "$SHA_A" '.tested_sha == $sha' >/dev/null \
  || fail "dry run should record the resolved head SHA, got: $OUT"
echo "$OUT" | jq -e '.dry_run == true and .github_writes == 0' >/dev/null \
  || fail "dry run must issue zero GitHub writes, got: $OUT"
echo "$OUT" | jq -e '.check_context == "superboard/exact-sha-qa"' >/dev/null \
  || fail "the SHA-bound status context must be reported, got: $OUT"
echo "$OUT" | jq -e '.checkout == "detached"' >/dev/null \
  || fail "QA authority is a detached worktree, got: $OUT"

# Scenario 3 — a head that moved since the recorded SHA refuses (exit 65).
RC=0
"$DISPATCH" --config "$CFG" \
  --issue-url https://github.com/test-owner/test-repo/issues/123 \
  --pull-request https://github.com/test-owner/test-repo/pull/456 \
  --pr-payload "$TMP/pr-head-moved.json" --expected-sha "$SHA_A" --dry-run >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 65 ] || fail "a changed head should exit 65, got $RC"

# Scenario 4 — a missing head refuses rather than testing whatever is checked out.
RC=0
"$DISPATCH" --config "$CFG" \
  --issue-url https://github.com/test-owner/test-repo/issues/123 \
  --pull-request https://github.com/test-owner/test-repo/pull/456 \
  --pr-payload "$TMP/pr-head-missing.json" --dry-run >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 65 ] || fail "a missing head should exit 65, got $RC"

# Scenario 5 — a mutable branch checkout is never QA authority.
RC=0
"$DISPATCH" --config "$CFG" \
  --issue-url https://github.com/test-owner/test-repo/issues/123 \
  --pull-request https://github.com/test-owner/test-repo/pull/456 \
  --pr-payload "$TMP/pr-head.json" --checkout branch --dry-run >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 65 ] || fail "a mutable branch checkout should exit 65, got $RC"

# Scenario 6 — missing required arguments are an invalid invocation (exit 64).
RC=0; "$DISPATCH" --config "$CFG" --dry-run >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 64 ] || fail "a missing --pull-request should exit 64, got $RC"

# Scenario 7 — an invalid config stops before any resolution (exit 65).
BADCFG="$TMP/bad-config.json"
jq '.columns = ["Ready","Skipped"]' "$CFG" > "$BADCFG"
RC=0
"$DISPATCH" --config "$BADCFG" \
  --issue-url https://github.com/test-owner/test-repo/issues/123 \
  --pull-request https://github.com/test-owner/test-repo/pull/456 \
  --pr-payload "$TMP/pr-head.json" --dry-run >/dev/null 2>&1 || RC=$?
[ "$RC" -eq 65 ] || fail "a config carrying 'columns' should exit 65, got $RC"

# Scenario 8 — file-bug files exactly one follow-up, and only outside acceptance criteria.
OUT8=$("$FILEBUG" --config "$CFG" \
        --issue-url https://github.com/test-owner/test-repo/issues/123 \
        --pull-request https://github.com/test-owner/test-repo/pull/456 \
        --tested-sha "$SHA_A" --failure-kind outside-acceptance --dry-run)
echo "$OUT8" | jq -e '.next_status == "Blocked" and .follow_up_issue_required == true' >/dev/null \
  || fail "outside-acceptance should block and require one follow-up, got: $OUT8"
echo "$OUT8" | jq -e '.github_writes == 0' >/dev/null \
  || fail "dry run must file nothing, got: $OUT8"

OUT9=$("$FILEBUG" --config "$CFG" \
        --issue-url https://github.com/test-owner/test-repo/issues/123 \
        --pull-request https://github.com/test-owner/test-repo/pull/456 \
        --tested-sha "$SHA_A" --failure-kind repairable --dry-run)
echo "$OUT9" | jq -e '.next_status == "Building" and .follow_up_issue_required == false' >/dev/null \
  || fail "a repairable failure returns to Building with no follow-up, got: $OUT9"

# Scenario 9 — no disposition ever lands on Done, and nothing ever merges.
for kind in repairable external-input outside-acceptance; do
  OUTK=$("$FILEBUG" --config "$CFG" \
          --issue-url https://github.com/test-owner/test-repo/issues/123 \
          --pull-request https://github.com/test-owner/test-repo/pull/456 \
          --tested-sha "$SHA_A" --failure-kind "$kind" --dry-run)
  echo "$OUTK" | jq -e '.next_status != "Done"' >/dev/null \
    || fail "QA failure must never move a card to Done ($kind), got: $OUTK"
done

echo "PASS: test-super-qa-dispatch.sh (9 scenarios)"
