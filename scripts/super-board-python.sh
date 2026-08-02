#!/usr/bin/env bash
# super-board-python.sh — one place that knows how to reach the shared runtime.
#
# Sourced by the shell entry points so every dispatcher path calls the SAME
# Python policy modules under `scripts/super_board_runtime/`:
#
#   source "$(dirname "$0")/super-board-python.sh"
#   sb_runtime super_board_runtime.eligibility --items - --config "$cfg"
#
# Windows/MSYS notes (why this file exists at all):
#   • A native python.exe cannot open MSYS paths like /tmp/x or /dev/fd/63, so
#     `sb_native_path` converts with cygpath and `sb_config_file` materializes
#     process substitutions (`--config <(...)`) into a real file first.
#   • PYTHONPATH must likewise carry a native path.
#
# Nothing here writes to GitHub and nothing here echoes an environment value.

# Directory holding `super_board_runtime/` — resolved from this file, so it is
# correct whether the caller is the repo checkout or an installed `.claude/bin`.
SB_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

sb_native_path() {
  # Echo a path a native interpreter can open. No-op off Windows.
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  else
    printf '%s' "$1"
  fi
}

sb_python() {
  # Echo the first interpreter that is genuinely Python 3.11+.
  if [ -n "${SB_PYTHON:-}" ]; then
    printf '%s' "$SB_PYTHON"
    return 0
  fi
  local candidate
  for candidate in "${SUPER_BOARD_PYTHON:-}" python3 python; do
    [ -n "$candidate" ] || continue
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      SB_PYTHON="$candidate"
      printf '%s' "$SB_PYTHON"
      return 0
    fi
  done
  echo "super-board: no Python 3.11+ interpreter found (set SUPER_BOARD_PYTHON)" >&2
  return 65
}

sb_runtime() {
  # Run a runtime module: sb_runtime <module> [args...]
  local py
  py=$(sb_python) || return $?
  PYTHONPATH="$(sb_native_path "$SB_SCRIPTS_DIR")" "$py" -B -m "$@"
}

# ───────────────────────────── temp files ─────────────────────────────
#
# What lands in these files is exactly what must not be left lying in shared
# temp space: rendered publication payloads, which carry the caller's whole
# environment on their way to the sanitizer.
#
# Cleanup used to track paths in a space-delimited STRING, so a path under
# `/tmp/dir with spaces/` split into three nonexistent paths and `rm -f` removed
# none of them. An array fixes that — but not the larger half of the same bug:
# every real caller invokes `sb_config_file` inside a pipeline and a command
# substitution (`payload=$(jq … | sb_config_file)`), both of which run in a
# SUBSHELL, so any registration the function performed died with it and NOTHING
# was ever cleaned up, spaces or no spaces.
#
# So the unit of cleanup is a private DIRECTORY, created once here in the
# sourcing shell with mode 0700, before any subshell exists. Files land inside
# it, so registration is implicit and cannot be lost. `SB_TMP_FILES` remains for
# paths a caller creates elsewhere and registers by hand.
if [ -z "${SB_TMP_DIR:-}" ] || [ ! -d "${SB_TMP_DIR:-}" ]; then
  SB_TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/super-board.XXXXXX") || return 65 2>/dev/null || exit 65
  chmod 700 "$SB_TMP_DIR" 2>/dev/null || true
fi
SB_TMP_FILES=()

sb_tmp_dir() {
  # Echo the private per-run temp directory.
  printf '%s' "$SB_TMP_DIR"
}

sb_tmp_register() {
  # Track one path created OUTSIDE `SB_TMP_DIR` for `sb_tmp_cleanup`. Only
  # useful from the sourcing shell — a subshell's registration cannot survive.
  SB_TMP_FILES+=("$1")
}

sb_config_file() {
  # Echo a natively-openable path holding the given config CONTENT (stdin).
  # The file is inside `SB_TMP_DIR`, so `sb_tmp_cleanup` removes it however
  # deep in subshells this was called.
  local tmp
  tmp=$(mktemp "$SB_TMP_DIR/config.XXXXXX") || return 65
  chmod 600 "$tmp" 2>/dev/null || true
  cat > "$tmp" || return 65
  sb_native_path "$tmp"
}

sb_tmp_cleanup() {
  local file
  # `${arr+"${arr[@]}"}` so an empty array is not an unbound-variable error
  # under `set -u` on the bash versions this runs on.
  for file in ${SB_TMP_FILES+"${SB_TMP_FILES[@]}"}; do
    rm -f "$file"
  done
  SB_TMP_FILES=()
  [ -n "${SB_TMP_DIR:-}" ] && [ -d "${SB_TMP_DIR:-}" ] && rm -rf "$SB_TMP_DIR"
  return 0
}
