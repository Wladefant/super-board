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
#   sb_gh_guard_begin_cycle          # take THE reading for this cycle (see §3)
#   sb_gh_guard_check 103 [config]   # refuse the burst if it would break the reserve
#   sb_gh_budget_spend 5             # decrement worker-local call budget; 75 if 0
#   sb_gh_guard_summary [config]     # print the gh-quota-on-exit line on stderr

# shellcheck source=scripts/super-board-python.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/super-board-python.sh"

# Constants
SB_GH_GUARD_DEFAULT_COST=100       # a ProjectsV2 item scan is ~103 points
SB_GH_GUARD_BUDGET_DEFAULT=150     # per-worker soft cap on gh calls
SB_GH_GUARD_SUBAGENT_BUDGET=50     # per adversarial-mode sub-agent cap

# The worker-local budget file. Deliberately NOT a name derived from the PID:
# `${TMPDIR:-/tmp}/super-board-gh-budget-$$` is guessable, so anyone else on the
# machine could create it first — as a symlink to a file this script then
# truncates and overwrites — and could read a worker's budget whenever they
# liked. It now lives inside the private 0700 directory `super-board-python.sh`
# creates for this run, which is unguessable and is removed by `sb_tmp_cleanup`,
# so the file cannot outlive the worker either.
#
# A caller may still pin the path by exporting `SB_GH_GUARD_STATE_FILE`. That
# path is chmod'ed but NOT removed at exit: the caller chose it, so the caller
# owns its lifetime.
SB_GH_GUARD_STATE_FILE="${SB_GH_GUARD_STATE_FILE:-$(sb_tmp_dir)/gh-budget}"

sb_gh_guard_ensure_state_file() {
  [ -f "$SB_GH_GUARD_STATE_FILE" ] || : > "$SB_GH_GUARD_STATE_FILE" || return 65
  chmod 600 "$SB_GH_GUARD_STATE_FILE" 2>/dev/null || true
}

# ───────────────────────────── one inventory per cycle ───────────────────────
#
# `rate-limit-etiquette.md` §3 and `quota.py` both state the rule: the quota is
# read ONCE per cycle and every check inside that cycle reuses that reading,
# because a guard that polls before every call becomes the thing that drains the
# bucket. `QuotaCycle` implements it on the Python side; the worker guard did
# not, so every `sb_gh_guard_check` shelled out to the runtime and the runtime
# ran `gh api rate_limit` again — the documented contract, contradicted by the
# component the document was written for.
#
# The cycle's reading is cached in the private 0700 run directory. A worker that
# never calls `sb_gh_guard_begin_cycle` gets one on its first check, which is
# what "source the guard, take one reading, spend against it" describes.
#
# The cache never softens a refusal — `require_graphql_budget` reads it exactly
# as it would read a live response, and a reading that says the burst does not
# fit is the answer for the whole cycle. Per-call accounting is the worker-local
# budget (`sb_gh_budget_spend`), not a re-read.
SB_GH_GUARD_QUOTA_CACHE="${SB_GH_GUARD_QUOTA_CACHE:-$(sb_tmp_dir)/gh-quota.json}"

sb_gh_guard_begin_cycle() {
  # Start a new cycle: discard the previous reading and take one now. Call at
  # the top of a dispatcher tick or at worker start.
  rm -f "$SB_GH_GUARD_QUOTA_CACHE"
  sb_gh_guard_cache_quota
}

sb_gh_guard_cache_quota() {
  # Ensure this cycle has a cached inventory. Returns non-zero when the quota
  # could not be read at all, which leaves the cache absent so the check below
  # falls through to a live read — and a live read that also fails is exhausted,
  # never a fabricated balance.
  [ -s "$SB_GH_GUARD_QUOTA_CACHE" ] && return 0
  local raw
  raw=$("${SUPERBOARD_GH:-gh}" api rate_limit 2>/dev/null) || return 1
  [ -n "$raw" ] || return 1
  printf '%s' "$raw" > "$SB_GH_GUARD_QUOTA_CACHE" || return 1
  chmod 600 "$SB_GH_GUARD_QUOTA_CACHE" 2>/dev/null || true
  return 0
}

sb_gh_guard_check() {
  # Refuse a burst that would break the immutable reserve.
  #   sb_gh_guard_check <cost> [config]
  #   sb_gh_guard_check <cost> [--config PATH] [--payload PATH]
  # `--payload` reads a `gh api rate_limit` document instead of calling GitHub,
  # so the reserve check is exercisable without a token or a network. An
  # explicit `--payload` always outranks this cycle's cached inventory.
  # Returns 0 when affordable, 75 when the reserve is reached or the quota is
  # unreadable. The CALLER halts — this function never sleeps and never retries.
  local cost="${1:-$SB_GH_GUARD_DEFAULT_COST}" rc=0 payload_given=0
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
      --payload) args+=(--payload "$(sb_native_path "${2:-}")"); payload_given=1; shift 2 ;;
      *)
        echo "[gh-guard] unknown option: $1" >&2
        return 64 ;;
    esac
  done
  if [ "$payload_given" -eq 0 ] && sb_gh_guard_cache_quota; then
    args+=(--payload "$(sb_native_path "$SB_GH_GUARD_QUOTA_CACHE")")
  fi
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
  sb_gh_guard_ensure_state_file || return 65
  echo "$budget" > "$SB_GH_GUARD_STATE_FILE"
}

sb_gh_budget_spend() {
  # Decrement budget by N (default 1). If exhausted, halt the worker.
  #
  # Returns 75, the quota code — the same one `sb_gh_guard_check` returns when
  # the GraphQL reserve is reached. Both mean "stop spending shared quota", and
  # a worker's supervisor acts on them identically. It used to return 73, which
  # this runtime's exit-code contract does not define at all, so the halt was
  # unreadable to every caller downstream.
  local cost="${1:-1}" remaining
  sb_gh_guard_ensure_state_file || return 65
  # `-s`, not `-f`: `mktemp` leaves the file existing but empty.
  [ -s "$SB_GH_GUARD_STATE_FILE" ] || sb_gh_budget_init
  remaining=$(cat "$SB_GH_GUARD_STATE_FILE")
  remaining=$((remaining - cost))
  echo "$remaining" > "$SB_GH_GUARD_STATE_FILE"
  if [ "$remaining" -le 0 ]; then
    echo "[gh-guard] worker gh-call budget exhausted — halting to protect shared quota" >&2
    return 75
  fi
}

sb_gh_budget_remaining() {
  sb_gh_guard_ensure_state_file || return 65
  [ -s "$SB_GH_GUARD_STATE_FILE" ] || sb_gh_budget_init
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
  # Executed, so nobody else owns the cleanup: a budget file created here would
  # otherwise outlive the process that made it.
  trap sb_tmp_cleanup EXIT
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
