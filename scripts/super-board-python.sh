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

SB_TMP_FILES=""

sb_config_file() {
  # Echo a natively-openable path holding the given config CONTENT (stdin).
  # Registers the temp file for `sb_tmp_cleanup`.
  local tmp
  tmp=$(mktemp "${TMPDIR:-/tmp}/super-board-config.XXXXXX") || return 65
  cat > "$tmp" || return 65
  SB_TMP_FILES="$SB_TMP_FILES $tmp"
  sb_native_path "$tmp"
}

sb_tmp_cleanup() {
  local file
  for file in $SB_TMP_FILES; do
    rm -f "$file"
  done
  SB_TMP_FILES=""
}
