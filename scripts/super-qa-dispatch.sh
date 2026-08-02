#!/usr/bin/env bash
# super-qa-dispatch.sh — exact-SHA QA entry point.
#
# The QA lane's only supported way in. It does four things in a fixed order and
# refuses at the first one that cannot be proven:
#
#   1. Validate the config (exit 65 on anything invalid) — before any lookup.
#   2. Resolve the linked pull request and RECORD `headRefOid` before running
#      anything. A missing, non-SHA, or changed head refuses (exit 65); QA never
#      falls back to whatever happens to be checked out.
#   3. Fetch exactly that SHA into a detached, per-item LOCKED worktree and run
#      the QA commands there. A mutable branch checkout is refused outright.
#   4. Reread the head. Success publishes the SHA-bound status
#      `superboard/exact-sha-qa` on the tested commit only when the reread SHA
#      is unchanged; otherwise the result is discarded.
#
# Every policy decision above lives in `super_board_runtime.qa`, which the
# Python tests pin directly — this script is the shell surface, not a second
# implementation.
#
# The runtime never merges and never moves a card to Done — see
# `super_board_runtime.review` for the enforced prohibition. A QA failure goes
# to Building or Blocked; see super-qa-file-bug.sh.
#
# Usage:
#   super-qa-dispatch.sh --config <cfg> --issue-url <url> --pull-request <url>
#                        [--expected-sha <sha>] [--checkout detached]
#                        [--worktree-root <dir>] [--pr-payload <file>]
#                        [--dry-run] [-- <qa command...>]
#
# Exit: 0 ok · 64 invalid invocation · 65 invalid config/input · 69 identity.

set -euo pipefail

# shellcheck source=scripts/super-board-python.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/super-board-python.sh"
trap sb_tmp_cleanup EXIT

CONFIG=""; ISSUE_URL=""; PR_URL=""; EXPECTED_SHA=""; CHECKOUT="detached"
PR_PAYLOAD=""; WORKTREE_ROOT=".worktrees/super-qa"; DRY_RUN=0
QA_COMMAND=()

usage() {
  cat >&2 <<'EOF'
usage: super-qa-dispatch.sh --config <cfg> --issue-url <url> --pull-request <url>
                            [--expected-sha <sha>] [--checkout detached]
                            [--worktree-root <dir>] [--pr-payload <file>]
                            [--dry-run] [-- <qa command...>]
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --config)        CONFIG="${2:-}"; shift 2 ;;
    --issue-url)     ISSUE_URL="${2:-}"; shift 2 ;;
    --pull-request)  PR_URL="${2:-}"; shift 2 ;;
    --expected-sha)  EXPECTED_SHA="${2:-}"; shift 2 ;;
    --checkout)      CHECKOUT="${2:-}"; shift 2 ;;
    --pr-payload)    PR_PAYLOAD="${2:-}"; shift 2 ;;
    --worktree-root) WORKTREE_ROOT="${2:-}"; shift 2 ;;
    --dry-run)       DRY_RUN=1; shift ;;
    --)              shift; QA_COMMAND=("$@"); break ;;
    -h|--help)       usage; exit 64 ;;
    *)               echo "super-qa-dispatch: unknown argument: $1" >&2; usage; exit 64 ;;
  esac
done

if [ -z "$CONFIG" ] || [ -z "$ISSUE_URL" ] || [ -z "$PR_URL" ]; then
  echo "super-qa-dispatch: --config, --issue-url and --pull-request are all required" >&2
  usage
  exit 64
fi

CONFIG_NATIVE=$(sb_native_path "$CONFIG")

# ── Step 1: the config gate. An invalid config stops before any lookup.
if ! "$(sb_python)" -B "$SB_SCRIPTS_DIR/super-board-config.py" \
      validate --config "$CONFIG_NATIVE" --json >/dev/null; then
  echo "🛑 super-qa-dispatch: config did not validate — nothing was resolved." >&2
  exit 65
fi

# ── Step 2: resolve and RECORD the exact head SHA, before anything runs.
RESOLVE_ARGS=(resolve --pull-request "$PR_URL" --checkout "$CHECKOUT")
[ -n "$EXPECTED_SHA" ] && RESOLVE_ARGS+=(--expected-sha "$EXPECTED_SHA")
[ -n "$PR_PAYLOAD" ] && RESOLVE_ARGS+=(--payload "$(sb_native_path "$PR_PAYLOAD")")

RC=0
HEAD_JSON=$(sb_runtime super_board_runtime.qa "${RESOLVE_ARGS[@]}") || RC=$?
if [ "$RC" -ne 0 ]; then
  echo "🛑 super-qa-dispatch: the exact head SHA could not be established — refusing to run QA." >&2
  exit "$RC"
fi

TESTED_SHA=$(echo "$HEAD_JSON" | jq -r '.tested_sha')
BASE_REF=$(echo "$HEAD_JSON" | jq -r '.base_ref // empty')
CHECK_CONTEXT=$(echo "$HEAD_JSON" | jq -r '.check_context')

emit() {
  jq -n --arg sha "$TESTED_SHA" --arg base "$BASE_REF" --arg ctx "$CHECK_CONTEXT" \
        --arg issue "$ISSUE_URL" --arg pr "$PR_URL" --arg checkout "$CHECKOUT" \
        --arg result "$1" --argjson dry "$2" --argjson writes "$3" \
        '{issue_url:$issue, pull_request_url:$pr, tested_sha:$sha, base_ref:$base,
          check_context:$ctx, checkout:$checkout, result:$result,
          dry_run:($dry == 1), github_writes:$writes}'
}

# ── Dry run stops here: the head is proven, and zero GitHub writes were issued.
if [ "$DRY_RUN" -eq 1 ]; then
  emit resolved 1 0
  exit 0
fi

# ── Step 3: run QA inside the detached, locked worktree at the tested SHA.
if [ "${#QA_COMMAND[@]}" -eq 0 ]; then
  echo "super-qa-dispatch: no QA command was supplied after --; nothing to attest to." >&2
  exit 64
fi

mkdir -p "$WORKTREE_ROOT"
LOCK_DIR="$WORKTREE_ROOT/locks"
mkdir -p "$LOCK_DIR"
ITEM_KEY=$(echo "$ISSUE_URL" | tr -c 'A-Za-z0-9._-' '-')
LOCK="$LOCK_DIR/${ITEM_KEY}.lock"
WORKTREE="$WORKTREE_ROOT/worktrees/${ITEM_KEY}-${TESTED_SHA:0:12}"

if ! (set -o noclobber; printf '%s\n' "$TESTED_SHA" > "$LOCK") 2>/dev/null; then
  echo "🛑 super-qa-dispatch: another QA run holds the lock for ${ISSUE_URL}." >&2
  exit 65
fi

# Released on EVERY terminal path: success, failure, and signals.
cleanup_worktree() {
  git worktree remove --force "$WORKTREE" >/dev/null 2>&1 || true
  rm -rf "$WORKTREE"
  rm -f "$LOCK"
  sb_tmp_cleanup
}
# A signal trap that only cleans up does NOT stop the script: bash resumes at
# the next statement, so the run would carry on with its worktree and its lock
# already deleted — and could publish a status for a run that was cancelled.
# Clean up, then leave with the conventional 128+signal status.
on_signal() { cleanup_worktree; trap - EXIT; exit "$1"; }
trap cleanup_worktree EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

git fetch origin "$TESTED_SHA" >/dev/null 2>&1 || {
  echo "🛑 super-qa-dispatch: could not fetch ${TESTED_SHA}." >&2
  exit 65
}
git worktree add --detach "$WORKTREE" "$TESTED_SHA" >/dev/null || {
  echo "🛑 super-qa-dispatch: could not create the detached QA worktree." >&2
  exit 65
}

QA_RC=0
( cd "$WORKTREE" && "${QA_COMMAND[@]}" ) || QA_RC=$?

# ── Step 4: reread the head. A commit that moved discards the result.
RC=0
sb_runtime super_board_runtime.qa resolve --pull-request "$PR_URL" \
  --expected-sha "$TESTED_SHA" ${PR_PAYLOAD:+--payload "$(sb_native_path "$PR_PAYLOAD")"} \
  >/dev/null || RC=$?
if [ "$RC" -ne 0 ]; then
  echo "🛑 super-qa-dispatch: the head moved during the run — discarding the result." >&2
  emit discarded 0 0
  exit 65
fi

if [ "$QA_RC" -ne 0 ]; then
  echo "super-qa-dispatch: QA failed on ${TESTED_SHA}. File the disposition with super-qa-file-bug.sh." >&2
  emit failure 0 0
  exit 0
fi

# ── Step 5: publish the SHA-bound status on the tested commit.
# Without this write a passing pull request can never become merge-ready: the
# merge handoff requires exactly this check, on exactly this SHA, to have
# concluded success. The payload goes through the publication boundary inside
# `super_board_runtime.qa`; nothing is written from here.
PUBLISH_ARGS=(publish-status
              --config "$CONFIG_NATIVE"
              --issue-url "$ISSUE_URL"
              --pull-request "$PR_URL"
              --tested-sha "$TESTED_SHA"
              --current-head-sha "$TESTED_SHA")
[ -n "$BASE_REF" ] && PUBLISH_ARGS+=(--base-ref "$BASE_REF")

RC=0
sb_runtime super_board_runtime.qa "${PUBLISH_ARGS[@]}" >/dev/null || RC=$?
if [ "$RC" -ne 0 ]; then
  echo "🛑 super-qa-dispatch: QA passed on ${TESTED_SHA} but the SHA-bound status could not be published." >&2
  emit unpublished 0 0
  exit "$RC"
fi

emit success 0 1
