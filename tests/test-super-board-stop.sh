#!/usr/bin/env bash
# Tests scripts/super-board-stop.sh. No real gh, no real git, no real pgrep.
#
# What these scenarios pin:
#   • only issue locks are issue locks — the workflow backend's wave lock is not
#     commented on, killed, or deleted by the legacy dispatcher's stop;
#   • the stop comment is a GitHub-bound payload like any other: it is sanitized
#     ONCE, before either write, and a payload the boundary rejects produces
#     ZERO comments rather than one on the issue and none on the PR.
set -euo pipefail
cd "$(dirname "$0")"

STOP_SCRIPT="$(cd .. && pwd)/scripts/super-board-stop.sh"
TMP="$(pwd)/.tmp-super-board-stop"   # repo-relative: MSYS /tmp is not openable natively

fail() { echo "FAIL: $1" >&2; exit 1; }
rm -rf "$TMP"; mkdir -p "$TMP/bin" "$TMP/work"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/work/.claude/super-board/configs" "$TMP/work/.claude/super-board/inflight"
cat > "$TMP/work/.claude/super-board/configs/testboard.json" <<'EOF'
{
  "version": 1,
  "variant": "full",
  "activation_mode": "active",
  "base_branch": "staging",
  "project": { "owner": "test-owner", "number": 99 },
  "repo": { "remote": "test-owner/test-repo" },
  "bot_identity": "super-board-bot"
}
EOF

cat > "$TMP/bin/gh" <<STUB
#!/usr/bin/env bash
printf '%s\n' "\$*" >> "$TMP/gh-calls.txt"
if [ "\$1" = pr ] && [ "\$2" = list ]; then printf '7\n'; fi
exit 0
STUB
cat > "$TMP/bin/git" <<STUB
#!/usr/bin/env bash
case "\$1" in
  branch) printf '  remotes/origin/issue-31-thing\n' ;;
  log)    cat "$TMP/commit-subject" ;;
  *)      : ;;
esac
exit 0
STUB
cat > "$TMP/bin/pgrep" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
chmod +x "$TMP/bin/gh" "$TMP/bin/git" "$TMP/bin/pgrep"
export PATH="$TMP/bin:$PATH"

INFLIGHT="$TMP/work/.claude/super-board/inflight"
seed_locks() {
  rm -f "$INFLIGHT"/*
  printf 'PID=\nLANE=build\nSTARTED=x\n' > "$INFLIGHT/31"
  printf 'SLUG=otherboard\nSTARTED=x\n' > "$INFLIGHT/workflow-wave.lock"
  : > "$TMP/gh-calls.txt"
}

cd "$TMP/work"

# ── Scenario 1 — the workflow-wave lock is not an issue.
# Commenting on it, killing it, and deleting it dissolves the mutual exclusion
# between the two backends in the middle of a live wave.
seed_locks
printf 'abc1234 add the thing\n' > "$TMP/commit-subject"
bash "$STOP_SCRIPT" testboard >/dev/null 2>&1 || fail "stop exited non-zero on a clean stop"
grep -q "workflow-wave.lock" "$TMP/gh-calls.txt" \
  && fail "the wave lock was treated as an issue: $(grep workflow-wave.lock "$TMP/gh-calls.txt")"
[ -f "$INFLIGHT/workflow-wave.lock" ] || fail "the wave lock was deleted by the legacy stop"
[ ! -f "$INFLIGHT/31" ] || fail "the issue lock was not cleared"
grep -q "issue comment 31" "$TMP/gh-calls.txt" || fail "no stop comment was posted on #31"

# ── Scenario 2 — the stop comment is sanitized before it is written.
seed_locks
TOKEN="gh""p_$(printf 'N%.0s' $(seq 36))"
printf 'abc1234 wire up %s in the runner\n' "$TOKEN" > "$TMP/commit-subject"
bash "$STOP_SCRIPT" testboard >/dev/null 2>&1 || fail "stop exited non-zero with a redactable secret"
grep -q "$TOKEN" "$TMP/gh-calls.txt" \
  && fail "a credential in the commit subject was written to GitHub verbatim"
grep -q "issue comment 31" "$TMP/gh-calls.txt" || fail "a sanitized comment should still be posted"

# ── Scenario 3 — a payload the boundary REFUSES produces zero comments.
# Not one on the issue and none on the PR: the issue write used to happen first,
# so a refusal could only ever be partial. A commit subject carrying bytes the
# boundary cannot interpret as text is refused rather than published.
seed_locks
printf 'abc1234 subject with \001 an uninterpretable byte\n' > "$TMP/commit-subject"
RC=0
bash "$STOP_SCRIPT" testboard >/dev/null 2>&1 || RC=$?
grep -q "comment" "$TMP/gh-calls.txt" \
  && fail "a refused payload still produced a comment: $(grep comment "$TMP/gh-calls.txt")"
grep -q -- "--remove-assignee" "$TMP/gh-calls.txt" \
  || fail "the claim must still be released when the comment is refused"

echo "PASS: test-super-board-stop.sh (3 scenarios)"
