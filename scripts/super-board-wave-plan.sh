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
# Without --items, reads the board's declared item count with `gh project view`
# and sizes `gh project item-list` to it, so the scan is complete rather than
# capped. When that count cannot be read the fetch falls back to
# `--limit ${PLAN_ITEM_LIMIT:-500}` and the bound is DECLARED — `coverage` on
# stdout and a warning on stderr — never applied silently.
# Stdout: {"activation_mode":…,"cards":[…],"coverage":{…},"decisions":[…],…}
# Exit: 0 ok · 64 bad invocation · 65 bad config/items · 66 config not found · 75 reserve reached
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
# 65, matching every other entry point: a config that is not there is the
# same unusable input as a config that does not validate. 66 is not a code
# this runtime defines.
[ -n "$CONFIG" ] && [ -e "$CONFIG" ] || { echo "config not found: ${CONFIG:-<unset>} — exiting 65" >&2; exit 65; }

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

# Coverage arguments handed to the eligibility layer, which turns them into the
# `coverage` block on stdout and the truncation warning on stderr.
COVERAGE_ARGS=()

if [ -n "$ITEMS_FILE" ]; then
  ITEMS=$(cat "$ITEMS_FILE")
else
  # A live board read costs real GraphQL points, so it is estimated against the
  # immutable reserve BEFORE it is spent. Reaching the reserve — or being unable
  # to read the quota — stops here with exit 75 rather than issuing the query.
  PLAN_ESTIMATED_COST=${PLAN_ESTIMATED_COST:-110}
  CONFIG_FOR_QUOTA=$(printf '%s' "$CONFIG_JSON" | sb_config_file)
  if ! sb_runtime super_board_runtime.quota check \
        --estimated-cost "$PLAN_ESTIMATED_COST" --config "$CONFIG_FOR_QUOTA" >/dev/null; then
    echo "super-board-wave-plan: refusing to scan the board — the GraphQL reserve is protected." >&2
    exit 75
  fi

  # Read the board's declared size first, then size the fetch to it. The scan
  # used to be hard-capped at `--limit 500` with no mention of the cap
  # anywhere: on a 591-card board the planner reported 500 decisions, and the
  # 91 cards it never looked at were indistinguishable from 91 it looked at and
  # rejected. `gh project view` is a single cheap query and it is what turns a
  # bounded scan into a complete one.
  BOARD_TOTAL=$(gh project view "$NUMBER" --owner "$OWNER" --format json 2>/dev/null \
                  | jq -r '.items.totalCount // empty' 2>/dev/null || true)
  case "$BOARD_TOTAL" in
    ''|*[!0-9]*) BOARD_TOTAL="" ;;
  esac

  if [ -n "$BOARD_TOTAL" ]; then
    # +1 so a card added between the two reads is still fetched, and the
    # coverage comparison below reports the shortfall honestly if more arrive.
    FETCH_LIMIT=$((BOARD_TOTAL + 1))
    [ "$FETCH_LIMIT" -ge 1 ] || FETCH_LIMIT=1
    COVERAGE_ARGS=(--items-total "$BOARD_TOTAL")
  else
    # The total could not be read. Fall back to a cap and DECLARE it: a full
    # page is the only evidence of truncation available, and assuming a full
    # page means a complete board is the failure this whole block exists to
    # prevent.
    FETCH_LIMIT=${PLAN_ITEM_LIMIT:-500}
    echo "super-board-wave-plan: could not read the board's item count; falling back to --limit ${FETCH_LIMIT}." >&2
    COVERAGE_ARGS=(--items-limit "$FETCH_LIMIT")
  fi

  ITEMS=$(gh project item-list "$NUMBER" --owner "$OWNER" --format json --limit "$FETCH_LIMIT")
fi

# A process substitution is not openable by a native interpreter; materialize.
CONFIG_NATIVE=$(printf '%s' "$CONFIG_JSON" | sb_config_file)

printf '%s' "$ITEMS" | sb_runtime super_board_runtime.eligibility \
  --items - --config "$CONFIG_NATIVE" ${COVERAGE_ARGS+"${COVERAGE_ARGS[@]}"}
