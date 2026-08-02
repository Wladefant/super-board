#!/usr/bin/env bash
# super-board-wave-plan.sh — compute the next wave from board state.
#
# Read-only: no gh writes, no locks. Selection is NOT decided here — it is
# delegated to the shared runtime (`super_board_runtime.eligibility`), which the
# headless dispatcher and the dynamic workflow also use, so a card cannot be
# eligible in one path and ineligible in another.
#
# The contract that runtime enforces, in full:
#   • only issue cards (pull-request and draft cards never dispatch),
#   • never a `design` or `history` card, nor anything in config.exclude_labels,
#   • status EXACTLY `Ready` — Backlog, Building, QA, Review, Blocked, and Done
#     are all rejected with `status-not-ready`,
#   • no assignee (the assignee is the cross-machine claim mutex),
#   • the issue must be OPEN; a failed state lookup is ineligible, never
#     permissive,
#   • unambiguous branch route,
#   • activation must permit it.
#
# Usage:
#   super-board-wave-plan.sh --config <config.json> [--items <project-items.json>]
# Without --items, fetches live board state via `gh project item-list`.
# Stdout: {"activation_mode":…,"cards":[…],"decisions":[…],"exclude_labels":[…],…}
# Exit: 0 ok · 64 bad invocation · 65 bad config/items · 66 config not found
set -euo pipefail

# shellcheck source=scripts/super-board-python.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/super-board-python.sh"
trap sb_tmp_cleanup EXIT

CONFIG=""; ITEMS_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG="$2"; shift 2 ;;
    --items)  ITEMS_FILE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 64 ;;
  esac
done
[ -n "$CONFIG" ] && [ -e "$CONFIG" ] || { echo "config not found: ${CONFIG:-<unset>}" >&2; exit 66; }

# Read the config ONCE — $CONFIG may be a process substitution (test mode),
# which is a FIFO and cannot be read twice.
CONFIG_JSON=$(cat "$CONFIG")
VARIANT=$(echo "$CONFIG_JSON" | jq -r '.variant // "full"')
OWNER=$(echo "$CONFIG_JSON" | jq -r '.project.owner')
NUMBER=$(echo "$CONFIG_JSON" | jq -r '.project.number')

# Validate loudly: a typo (or missing key → literal "null") must not silently
# change which lanes exist. The variant selects lanes, never statuses.
case "$VARIANT" in
  full|qa-only) ;;
  *) echo "invalid variant in config: ${VARIANT} (expected full|qa-only)" >&2; exit 65 ;;
esac

if [ -n "$ITEMS_FILE" ]; then
  ITEMS=$(cat "$ITEMS_FILE")
else
  ITEMS=$(gh project item-list "$NUMBER" --owner "$OWNER" --format json --limit 500)
fi

# A process substitution is not openable by a native interpreter; materialize.
CONFIG_NATIVE=$(printf '%s' "$CONFIG_JSON" | sb_config_file)

printf '%s' "$ITEMS" | sb_runtime super_board_runtime.eligibility \
  --items - --config "$CONFIG_NATIVE"
