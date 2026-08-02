#!/usr/bin/env bash
# super-board-gh-guard.sh — worker-side quota etiquette.
#
# Sourced by super-board workers (Builder / Tester / Reviewer) before any burst
# of gh calls. The dispatcher's own guard only protects the dispatcher's ticks;
# workers run as independent claude -p sessions and share the same token bucket.
# Without this guard, a single Reviewer in adversarial mode can drain the bucket
# and the next tick of the dispatcher — and every other worker on the machine —
# opens to nothing left.
#
# The accounting lives in `super_board_runtime.quota`, so the worker guard, the
# dispatcher, and the planner cannot disagree about what is affordable:
#
#   • an immutable reserve of 1000 GraphQL points that is never spent (a config
#     may raise the floor, never lower it),
#   • `remaining - estimated_cost >= effective_floor` required before the call,
#   • reaching the reserve STOPS the worker (exit 75). It does not sleep through
#     the reset and it does not retry-spin,
#   • an unreadable quota is treated as exhausted. There is no fabricated
#     fallback capacity — assuming a full bucket whenever `gh` failed is exactly
#     how an empty bucket used to look full.
#
# Usage from a worker:
#   source scripts/super-board-gh-guard.sh
#   sb_gh_guard_check 103 [config]   # refuse the burst if it would break the reserve
#   sb_gh_budget_spend 5             # decrement worker-local call budget; halt if 0
#   sb_gh_guard_summary              # log the safe quota fields; cheap to call

# shellcheck source=scripts/super-board-python.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/super-board-python.sh"

# Constants
SB_GH_GUARD_DEFAULT_COST=100       # a ProjectsV2 item scan is ~103 points
SB_GH_GUARD_BUDGET_DEFAULT=150     # per-worker soft cap on gh calls
SB_GH_GUARD_SUBAGENT_BUDGET=50     # per adversarial-mode sub-agent cap
SB_GH_GUARD_STATE_FILE="${SB_GH_GUARD_STATE_FILE:-${TMPDIR:-/tmp}/super-board-gh-budget-$$}"

sb_gh_guard_check() {
  # Refuse a burst that would break the immutable reserve.
  # Arg 1: estimated cost in GraphQL points (default 100).
  # Arg 2: optional config path supplying a raised floor.
  # Returns 0 when affordable, 75 when the reserve is reached or the quota is
  # unreadable. The CALLER halts — this function never sleeps and never retries.
  local cost="${1:-$SB_GH_GUARD_DEFAULT_COST}" config="${2:-}" rc=0
  if [ -n "$config" ]; then
    sb_runtime super_board_runtime.quota check \
      --estimated-cost "$cost" --config "$(sb_native_path "$config")" >/dev/null || rc=$?
  else
    sb_runtime super_board_runtime.quota check --estimated-cost "$cost" >/dev/null || rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    echo "[gh-guard] halting: the next burst (~${cost} points) would break the GraphQL reserve, or the quota could not be read" >&2
    return 75
  fi
  return 0
}

sb_gh_guard_summary() {
  # One-line snapshot for worker exit messages. Emits only the four safe fields:
  # remaining points, estimated cost, effective floor, reset time.
  sb_runtime super_board_runtime.quota check --estimated-cost 1 >/dev/null 2>&1 && return 0
  return 0
}

sb_gh_budget_init() {
  # Initialize per-worker budget. Call once at worker start.
  local budget="${1:-$SB_GH_GUARD_BUDGET_DEFAULT}"
  echo "$budget" > "$SB_GH_GUARD_STATE_FILE"
}

sb_gh_budget_spend() {
  # Decrement budget by N (default 1). If exhausted, halt the worker.
  local cost="${1:-1}" remaining
  [ -f "$SB_GH_GUARD_STATE_FILE" ] || sb_gh_budget_init
  remaining=$(cat "$SB_GH_GUARD_STATE_FILE")
  remaining=$((remaining - cost))
  echo "$remaining" > "$SB_GH_GUARD_STATE_FILE"
  if [ "$remaining" -le 0 ]; then
    echo "[gh-guard] worker gh-call budget exhausted — halting to protect shared quota" >&2
    return 73
  fi
}

sb_gh_budget_remaining() {
  [ -f "$SB_GH_GUARD_STATE_FILE" ] || sb_gh_budget_init
  cat "$SB_GH_GUARD_STATE_FILE"
}
