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
jq '.columns = ["Ready","QA"]' "$CFG" > "$BADCFG"
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

# ── Stubs for the scenarios that run the full pipeline. No real git, no real gh.
# `git` is intercepted on PATH; `gh` is intercepted through SUPERBOARD_GH,
# because the runtime spawns it from Python and a native interpreter will not
# resolve an extensionless script on Windows.
mkdir -p "$TMP/bin"
CALLS="$PWD/$TMP/gh-calls.txt"
STDIN_LOG="$PWD/$TMP/gh-stdin.txt"
cat > "$TMP/bin/git" <<'STUB'
#!/usr/bin/env bash
if [ "$1" = worktree ] && [ "$2" = add ]; then mkdir -p "$4"; exit 0; fi
exit 0
STUB
cat > "$TMP/bin/gh" <<STUB
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$CALLS"
cat >> "$STDIN_LOG" 2>/dev/null || true
printf '%s' '{"url":"https://github.com/test-owner/test-repo/commit/x"}'
STUB
chmod +x "$TMP/bin/git" "$TMP/bin/gh"

if command -v cygpath >/dev/null 2>&1; then
  # A native python.exe runs an absolute .cmd, but never an extensionless script.
  printf '@echo off\r\nbash "%s" %%*\r\n' "$(cygpath -m "$PWD/$TMP/bin/gh")" > "$TMP/bin/gh.cmd"
  export SUPERBOARD_GH="$(cygpath -w "$PWD/$TMP/bin/gh.cmd")"
else
  export SUPERBOARD_GH="$PWD/$TMP/bin/gh"
fi

# Scenario 10 — a successful run publishes the SHA-bound status on the tested
# commit. Without it a passing pull request can never become merge-ready: the
# merge handoff requires that exact check on that exact SHA.
: > "$TMP/gh-calls.txt"; : > "$TMP/gh-stdin.txt"
OUT10=$(PATH="$PWD/$TMP/bin:$PATH" "$DISPATCH" --config "$CFG" \
        --issue-url https://github.com/test-owner/test-repo/issues/123 \
        --pull-request https://github.com/test-owner/test-repo/pull/456 \
        --pr-payload "$TMP/pr-head.json" \
        --worktree-root "$TMP/wt" -- true)
echo "$OUT10" | jq -e '.result == "success"' >/dev/null \
  || fail "a passing QA command should report success, got: $OUT10"
echo "$OUT10" | jq -e '.github_writes == 1' >/dev/null \
  || fail "a successful run publishes exactly one SHA-bound status, got: $OUT10"
grep -q "statuses/$SHA_A" "$TMP/gh-calls.txt" \
  || fail "the status must be written on the tested commit, got: $(cat "$TMP/gh-calls.txt")"
grep -q "superboard/exact-sha-qa" "$TMP/gh-stdin.txt" \
  || fail "the published status must carry the SHA-bound context, got: $(cat "$TMP/gh-stdin.txt")"

# Scenario 11 — a failing QA command publishes nothing.
: > "$TMP/gh-calls.txt"
OUT11=$(PATH="$PWD/$TMP/bin:$PATH" "$DISPATCH" --config "$CFG" \
        --issue-url https://github.com/test-owner/test-repo/issues/123 \
        --pull-request https://github.com/test-owner/test-repo/pull/456 \
        --pr-payload "$TMP/pr-head.json" \
        --worktree-root "$TMP/wt" -- false)
echo "$OUT11" | jq -e '.result == "failure" and .github_writes == 0' >/dev/null \
  || fail "a failed QA run publishes no passing status, got: $OUT11"
[ ! -s "$TMP/gh-calls.txt" ] || fail "a failed QA run must issue zero writes, got: $(cat "$TMP/gh-calls.txt")"

# Scenario 12 — SIGTERM cleans up AND exits. A trap that only cleans up lets
# execution continue after the worktree and the lock have been deleted.
PATH="$PWD/$TMP/bin:$PATH" "$DISPATCH" --config "$CFG" \
  --issue-url https://github.com/test-owner/test-repo/issues/123 \
  --pull-request https://github.com/test-owner/test-repo/pull/456 \
  --pr-payload "$TMP/pr-head.json" \
  --worktree-root "$TMP/wt-signal" -- sleep 20 >/dev/null 2>&1 &
SIG_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  [ -d "$TMP/wt-signal/locks" ] && break
  sleep 0.5
done
kill -TERM "$SIG_PID" 2>/dev/null || true
SIG_RC=0; wait "$SIG_PID" || SIG_RC=$?
[ "$SIG_RC" -eq 143 ] || fail "a TERM during QA should exit 143, got $SIG_RC"
[ -z "$(ls -A "$TMP/wt-signal/locks" 2>/dev/null)" ] || fail "the QA lock survived a TERM"

# Scenario 13 — an out-of-scope failure actually FILES the promised follow-up.
# Sanitizing a payload and then discarding it leaves the card Blocked with a
# follow-up that exists only in a JSON line nobody reads.
: > "$TMP/gh-calls.txt"; : > "$TMP/gh-stdin.txt"
OUT13=$("$FILEBUG" --config "$CFG" \
        --issue-url https://github.com/test-owner/test-repo/issues/123 \
        --pull-request https://github.com/test-owner/test-repo/pull/456 \
        --tested-sha "$SHA_A" --failure-kind outside-acceptance)
echo "$OUT13" | jq -e '.github_writes == 1' >/dev/null \
  || fail "an out-of-scope failure files exactly one follow-up, got: $OUT13"
grep -q "repos/test-owner/test-repo/issues" "$TMP/gh-calls.txt" \
  || fail "the follow-up must be created on the configured repo, got: $(cat "$TMP/gh-calls.txt")"
grep -q '"title"' "$TMP/gh-stdin.txt" \
  || fail "a created issue needs a title, got: $(cat "$TMP/gh-stdin.txt")"
grep -q "$SHA_A" "$TMP/gh-stdin.txt" \
  || fail "the follow-up must name the tested SHA, got: $(cat "$TMP/gh-stdin.txt")"

# Scenario 14 — the other two dispositions still write nothing at all.
for kind in repairable external-input; do
  : > "$TMP/gh-calls.txt"
  OUT14=$("$FILEBUG" --config "$CFG" \
          --issue-url https://github.com/test-owner/test-repo/issues/123 \
          --pull-request https://github.com/test-owner/test-repo/pull/456 \
          --tested-sha "$SHA_A" --failure-kind "$kind")
  echo "$OUT14" | jq -e '.github_writes == 0' >/dev/null \
    || fail "$kind must file nothing, got: $OUT14"
  [ ! -s "$TMP/gh-calls.txt" ] || fail "$kind issued a GitHub write: $(cat "$TMP/gh-calls.txt")"
done

echo "PASS: test-super-qa-dispatch.sh (14 scenarios)"
