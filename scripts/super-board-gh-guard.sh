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
#   sb_gh_guard_summary [config]     # print the gh-quota-on-exit line on stderr

# shellcheck source=scripts/super-board-python.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/super-board-python.sh"

# Constants
SB_GH_GUARD_DEFAULT_COST=100       # a ProjectsV2 item scan is ~103 points
SB_GH_GUARD_BUDGET_DEFAULT=150     # per-worker soft cap on gh calls
SB_GH_GUARD_SUBAGENT_BUDGET=50     # per adversarial-mode sub-agent cap
SB_GH_GUARD_STATE_FILE="${SB_GH_GUARD_STATE_FILE:-${TMPDIR:-/tmp}/super-board-gh-budget-$$}"

sb_gh_guard_check() {
  # Refuse a burst that would break the immutable reserve.
  #   sb_gh_guard_check <cost> [config]
  #   sb_gh_guard_check <cost> [--config PATH] [--payload PATH]
  # `--payload` reads a `gh api rate_limit` document instead of calling GitHub,
  # so the reserve check is exercisable without a token or a network.
  # Returns 0 when affordable, 75 when the reserve is reached or the quota is
  # unreadable. The CALLER halts — this function never sleeps and never retries.
  local cost="${1:-$SB_GH_GUARD_DEFAULT_COST}" rc=0
  [ $# -gt 0 ] && shift
  local args=(check --estimated-cost "$cost")
  # A bare second positional is the config path (the original signature).
  if [ $# -gt 0 ] && [ -n "$1" ] && [ "${1#-}" = "$1" ]; then
    args+=(--config "$(sb_native_path "$1")")
    shift
  fi
  while [ $# -gt 0 ]; do
    case "$1" in
      --config)  args+=(--config "$(sb_native_path "${2:-}")"); shift 2 ;;
      --payload) args+=(--payload "$(sb_native_path "${2:-}")"); shift 2 ;;
      *)
        echo "[gh-guard] unknown option: $1" >&2
        return 64 ;;
    esac
  done
  sb_runtime super_board_runtime.quota "${args[@]}" >/dev/null || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "[gh-guard] halting: the next burst (~${cost} points) would break the GraphQL reserve, or the quota could not be read" >&2
    return 75
  fi
  return 0
}

sb_gh_guard_summary() {
  # The worker exit line `rate-limit-etiquette.md` §8 and `run.md` require:
  #
  #   gh-quota-on-exit: graphql=<remaining> floor=<effective-floor> reset=<time>
  #
  # Printed on STDERR, where the guard's other diagnostics go. Capture it for a
  # PR handoff comment with `sb_gh_guard_summary 2>&1`.
  #
  # Arg 1: optional config path supplying a raised floor.
  #
  # Only those three safe fields ever appear — never a token, header, cookie or
  # raw payload, and never the runtime's stderr, which is why the call below
  # discards its own error stream rather than forwarding it.
  #
  # NON-FATAL by construction. A summary is a worker's last act; it must not be
  # able to change the status that worker exits with. Every failure path — no
  # Python, no `gh`, an unreadable or malformed quota — lands on the unavailable
  # marker and returns 0. Silence is the one outcome that is not allowed: this
  # function used to send both streams to /dev/null and emit nothing at all,
  # which made the documented line impossible to produce.
  local config="${1:-}" line=""
  if [ -n "$config" ]; then
    line=$(sb_runtime super_board_runtime.quota summary \
      --config "$(sb_native_path "$config")" 2>/dev/null) || line=""
  else
    line=$(sb_runtime super_board_runtime.quota summary 2>/dev/null) || line=""
  fi
  case "$line" in
    "gh-quota-on-exit:"*) ;;
    *) line="gh-quota-on-exit: unavailable (quota could not be read)" ;;
  esac
  printf '%s\n' "$line" >&2
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

# ───────────────────────────── executed as a command ─────────────────────────
#
# Sourcing is the primary use, but not every caller can source a shell library:
# a GitHub Actions step and a Python `subprocess.run` both invoke this file as a
# program. Without a CLI they invoked a file that defines functions and exits 0,
# so `super-board-gh-guard.sh check` reported the reserve as respected without
# ever consulting it — a preflight that always passes.
#
# Exit codes are the functions' own: 0 affordable · 75 the reserve is reached or
# the quota is unreadable · 64 invalid invocation.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  sb_gh_guard_main() {
    case "${1:-}" in
      check)
        shift
        sb_gh_guard_check "$@"
        ;;
      summary)
        shift
        sb_gh_guard_summary "$@"
        ;;
      budget-remaining)
        sb_gh_budget_remaining
        ;;
      *)
        echo "usage: super-board-gh-guard.sh <check|summary|budget-remaining> [args]" >&2
        return 64
        ;;
    esac
  }
  sb_gh_guard_main "$@"
  exit $?
fi
