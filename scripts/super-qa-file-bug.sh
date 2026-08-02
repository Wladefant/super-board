#!/usr/bin/env bash
# super-qa-file-bug.sh — sanitized QA-failure filing.
#
# A failed QA run has exactly three dispositions, decided by
# `super_board_runtime.qa.disposition_qa_failure` (never re-derived here):
#
#   repairable          the current worker can fix it   → Building, no follow-up
#   external-input      a human/third party is needed   → Blocked,  no follow-up
#   outside-acceptance  outside this issue's criteria   → Blocked,  exactly one
#                                                          structured follow-up
#
# Anything else fails closed to Blocked with no follow-up. No disposition ever
# merges, closes the implementation issue, or moves a card to Done.
#
# Every published byte goes through the sanitizing publication boundary
# (`super-board-publish.py`), so raw logs and command output never reach GitHub
# from here.
#
# Usage:
#   super-qa-file-bug.sh --config <cfg> --issue-url <url> --pull-request <url>
#                        --tested-sha <sha> --failure-kind <kind> [--dry-run]
#
# Exit: 0 ok · 64 invalid invocation · 65 invalid config/input · 78 unsafe evidence.

set -euo pipefail

# shellcheck source=scripts/super-board-python.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/super-board-python.sh"
trap sb_tmp_cleanup EXIT

CONFIG=""; ISSUE_URL=""; PR_URL=""; TESTED_SHA=""; FAILURE_KIND=""; DRY_RUN=0

usage() {
  cat >&2 <<'EOF'
usage: super-qa-file-bug.sh --config <cfg> --issue-url <url> --pull-request <url>
                            --tested-sha <sha> --failure-kind <repairable|external-input|outside-acceptance>
                            [--dry-run]
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --config)       CONFIG="${2:-}"; shift 2 ;;
    --issue-url)    ISSUE_URL="${2:-}"; shift 2 ;;
    --pull-request) PR_URL="${2:-}"; shift 2 ;;
    --tested-sha)   TESTED_SHA="${2:-}"; shift 2 ;;
    --failure-kind) FAILURE_KIND="${2:-}"; shift 2 ;;
    --dry-run)      DRY_RUN=1; shift ;;
    -h|--help)      usage; exit 64 ;;
    *)              echo "super-qa-file-bug: unknown argument: $1" >&2; usage; exit 64 ;;
  esac
done

if [ -z "$CONFIG" ] || [ -z "$ISSUE_URL" ] || [ -z "$PR_URL" ] || [ -z "$TESTED_SHA" ]; then
  echo "super-qa-file-bug: --config, --issue-url, --pull-request and --tested-sha are required" >&2
  usage
  exit 64
fi

ARGS=(disposition
      --config "$(sb_native_path "$CONFIG")"
      --issue-url "$ISSUE_URL"
      --pull-request "$PR_URL"
      --tested-sha "$TESTED_SHA")
[ -n "$FAILURE_KIND" ] && ARGS+=(--failure-kind "$FAILURE_KIND")
[ "$DRY_RUN" -eq 1 ] && ARGS+=(--dry-run)

RC=0
OUT=$(sb_runtime super_board_runtime.qa "${ARGS[@]}") || RC=$?
if [ "$RC" -ne 0 ]; then
  echo "🛑 super-qa-file-bug: the failure disposition could not be decided — nothing was filed." >&2
  exit "$RC"
fi

# The follow-up issue is published through the ONE sanitizer. Nothing here
# writes to GitHub directly: `super-board-publish.py` renders the complete
# payload, redacts, rescans the redacted result, and exits 78 before any write
# if anything sensitive survived.
if echo "$OUT" | jq -e '.follow_up != null' >/dev/null 2>&1; then
  PAYLOAD=$(echo "$OUT" | jq '.follow_up | {surface, text}' | sb_config_file)
  PUB_RC=0
  PUB=$("$(sb_python)" -B "$SB_SCRIPTS_DIR/super-board-publish.py" \
          publish --input "$PAYLOAD" --json) || PUB_RC=$?
  if [ "$PUB_RC" -ne 0 ]; then
    echo "🛑 super-qa-file-bug: the follow-up payload was rejected at the publication boundary — nothing was filed." >&2
    exit "$PUB_RC"
  fi
  OUT=$(echo "$OUT" | jq --argjson pub "$PUB" '.follow_up.sanitized = $pub.text | .follow_up.redactions = $pub.redactions')
fi

printf '%s\n' "$OUT"
