#!/usr/bin/env bash
# tests/test-install.sh — the installer's end-to-end contract.
#
# The headline scenario is the idempotency proof, and it is done exactly one
# way: install, snapshot the installed tree by path and checksum, install the
# SAME release again, snapshot again, compare snapshot 2 against snapshot 1.
# Zero added, removed, changed, or ownership-shifted entries.
#
# It is not a comparison against the pre-install checkout. That comparison is
# dominated by the first install's own output, so it can read clean while the
# second install quietly rewrites half the payload — and it cannot represent an
# ownership shift at all.
#
# Also covered: the payload is complete, retired layout paths are never
# produced, files the installer does not own survive a reinstall, a downgrade is
# refused, a source tree at the wrong commit is refused, and a tampered file is
# caught by verification.
#
# No network, no `gh`, no writes outside a temporary directory.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/super-board-python.sh
. "$ROOT/scripts/super-board-python.sh"

PY="$(sb_python)" || exit 65
VERIFY="$ROOT/scripts/super-board-install-verify.py"
export PYTHONPATH="$(sb_native_path "$ROOT/scripts")"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/super-board-install.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
TARGET="$TMP/repo"
mkdir -p "$TARGET" "$TMP/home"

SOURCE_SHA="$(git -C "$ROOT" rev-parse HEAD)"
RELEASE_VERSION="$(tr -d ' \t\r\n' < "$ROOT/VERSION")"
DESIGN_SOURCE="https://github.com/Wladefant/super-board"
DESIGN_SHA="$SOURCE_SHA"
DESIGN_CHECKSUM="$("$PY" -B -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "$DESIGN_SOURCE")"
MANIFEST="$TARGET/.claude/super-board/install-manifest.json"

failures=0
fail() { echo "FAIL: $*" >&2; failures=$((failures + 1)); }
ok() { echo "  ok  $*"; }

sb() { "$PY" -B "$VERIFY" "$@"; }

install_release() {
  bash "$ROOT/install.sh" \
    --repo-root "$TARGET" \
    --user-home "$TMP/home" \
    --source-sha "$SOURCE_SHA" \
    --release-version "$RELEASE_VERSION" \
    --design-skill-source "$DESIGN_SOURCE" \
    --design-skill-sha "$DESIGN_SHA" \
    --design-skill-checksum "$DESIGN_CHECKSUM" \
    --slug superboard-test \
    "$@" > "$TMP/install.log" 2>&1
}

entries_in() {
  "$PY" -B -c "import json,sys;print(len(json.load(open(sys.argv[1],encoding='utf-8'))['entries']))" "$1"
}

field_of() {
  "$PY" -B -c "import json,sys;print(len(json.load(open(sys.argv[1],encoding='utf-8'))[sys.argv[2]]))" "$1" "$2"
}

# ── 1. a fresh install lands the complete payload ───────────────────────────
if install_release; then ok "fresh install succeeded"; else fail "fresh install failed"; cat "$TMP/install.log" >&2; fi

for required in \
  ".claude/skills/super-board/SKILL.md" \
  ".claude/skills/super-build" \
  ".claude/skills/super-qa" \
  ".claude/skills/super-review" \
  ".claude/bin/super-board-run.sh" \
  ".claude/bin/super-board-stop.sh" \
  ".claude/bin/super-board-gh-guard.sh" \
  ".claude/bin/super-board-status.py" \
  ".claude/bin/super-board-wave-plan.sh" \
  ".claude/bin/super-board-sweep-comments.mjs" \
  ".claude/bin/super-qa-dispatch.sh" \
  ".claude/bin/super-qa-file-bug.sh" \
  ".claude/bin/super_board_runtime/__init__.py" \
  ".claude/workflows/super-board-wave.js" \
  ".claude/super-board/configs/superboard-test.json" \
  ".claude/super-board/active" \
  ".claude/super-board/install-manifest.json" \
  ".github/ISSUE_TEMPLATE/superboard-issue.yml" \
  ".github/workflows/auto-add-to-project.yml" \
  ".github/workflows/super-board-normalize.yml"; do
  if [ -e "$TARGET/$required" ]; then ok "installed $required"; else fail "missing $required"; fi
done

# ── 2. the retired layout paths are never produced ──────────────────────────
for retired in ".claude/super-board/config.json" ".claude/super-board/scripts"; do
  if [ -e "$TARGET/$retired" ]; then fail "$retired was produced"; else ok "absent $retired"; fi
done

# ── 3. files the installer does not own must survive a reinstall ────────────
echo "operator note" > "$TARGET/.claude/bin/operator-note.txt"
mkdir -p "$TARGET/.github/workflows"
echo "name: ci" > "$TARGET/.github/workflows/ci.yml"

# ── 4. the idempotency proof: snapshot, reinstall, snapshot, compare ────────
sb snapshot --manifest "$(sb_native_path "$MANIFEST")" \
            --repo-root "$(sb_native_path "$TARGET")" \
            --out "$(sb_native_path "$TMP/snapshot-1.json")" > /dev/null

if install_release; then ok "second install succeeded"; else fail "second install failed"; cat "$TMP/install.log" >&2; fi

sb snapshot --manifest "$(sb_native_path "$MANIFEST")" \
            --repo-root "$(sb_native_path "$TARGET")" \
            --out "$(sb_native_path "$TMP/snapshot-2.json")" > /dev/null

DRIFT="$TMP/drift.json"
if sb compare --first "$(sb_native_path "$TMP/snapshot-1.json")" \
              --second "$(sb_native_path "$TMP/snapshot-2.json")" \
              --json > "$DRIFT" 2>"$DRIFT.err"; then
  ok "the second install produced no drift"
else
  cp "$DRIFT.err" "$DRIFT"
  fail "the second install produced drift"
fi

[ -f "$TARGET/.claude/bin/operator-note.txt" ] && ok "unowned .claude file survived" || fail "unowned .claude file was removed"
[ "$(cat "$TARGET/.github/workflows/ci.yml")" = "name: ci" ] && ok "unowned CI workflow survived" || fail "unowned CI workflow was rewritten"

# ── 5. a downgrade is refused without the documented override ───────────────
set +e
sb install --source-root "$(sb_native_path "$ROOT")" \
           --repo-root "$(sb_native_path "$TARGET")" \
           --user-home "$TMP/home" --source-sha "$SOURCE_SHA" \
           --release-version "0.0.1" \
           --design-skill-source "$DESIGN_SOURCE" --design-skill-sha "$DESIGN_SHA" \
           --design-skill-checksum "$DESIGN_CHECKSUM" \
           --slug superboard-test --skip-source-check > /dev/null 2>&1
downgrade_status=$?
set -e
[ "$downgrade_status" -eq 65 ] && ok "downgrade refused (exit 65)" || fail "downgrade returned $downgrade_status, expected 65"

# ── 6. a source tree at the wrong commit is refused ─────────────────────────
set +e
bash "$ROOT/install.sh" --repo-root "$TARGET" --user-home "$TMP/home" \
  --source-sha "0000000000000000000000000000000000000000" \
  --release-version "$RELEASE_VERSION" \
  --design-skill-source "$DESIGN_SOURCE" --design-skill-sha "$DESIGN_SHA" \
  --design-skill-checksum "$DESIGN_CHECKSUM" > /dev/null 2>&1
pinned_status=$?
set -e
[ "$pinned_status" -eq 65 ] && ok "unpinned source refused (exit 65)" || fail "unpinned source returned $pinned_status, expected 65"

# ── 7. verification catches a tampered installed file ───────────────────────
echo "tampered" > "$TARGET/.claude/bin/super-board-run.sh"
set +e
sb verify --manifest "$(sb_native_path "$MANIFEST")" --repo-root "$(sb_native_path "$TARGET")" --json > /dev/null 2>&1
verify_status=$?
set -e
[ "$verify_status" -eq 65 ] && ok "tampered file detected (exit 65)" || fail "verify returned $verify_status, expected 65"

# ── the documented proof tail ───────────────────────────────────────────────
echo
echo "install_1_files=$(entries_in "$TMP/snapshot-1.json")"
echo "install_2_files=$(entries_in "$TMP/snapshot-2.json")"
echo "added=$(field_of "$DRIFT" added) removed=$(field_of "$DRIFT" removed) changed=$(field_of "$DRIFT" changed) ownership_shifted=$(field_of "$DRIFT" ownership_shifted)"
echo "clean=$("$PY" -B -c "import json,sys;print(str(json.load(open(sys.argv[1],encoding='utf-8'))['clean']).lower())" "$DRIFT")"

echo
if [ "$failures" -eq 0 ]; then
  echo "PASS: test-install.sh"
else
  echo "FAIL: test-install.sh ($failures failure(s))" >&2
  exit 1
fi
