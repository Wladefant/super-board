#!/usr/bin/env bash
# super-board installer.
# Copies skills/ + scripts/ into the target project's .claude/ tree.
#
# Usage:
#   ./install.sh [target-project-dir]
# Defaults to the current working directory.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$PWD}"

if [ ! -d "$TARGET" ]; then
  echo "target directory not found: $TARGET" >&2
  exit 64
fi

if [ ! -d "$TARGET/.claude" ]; then
  echo "creating $TARGET/.claude (didn't exist)"
fi

mkdir -p "$TARGET/.claude/skills" "$TARGET/.claude/bin"

echo "→ installing skills into $TARGET/.claude/skills/"
for skill in super-board super-build super-qa super-review; do
  if [ -d "$REPO_ROOT/skills/$skill" ]; then
    cp -R "$REPO_ROOT/skills/$skill" "$TARGET/.claude/skills/"
    echo "    ✓ $skill"
  else
    echo "    ✗ missing $skill in repo — skipping" >&2
  fi
done

echo "→ installing dispatcher scripts into $TARGET/.claude/bin/"
for script in super-board-run.sh super-board-gh-guard.sh super-board-status.py super-board-wave-plan.sh; do
  if [ -f "$REPO_ROOT/scripts/$script" ]; then
    cp "$REPO_ROOT/scripts/$script" "$TARGET/.claude/bin/"
    chmod +x "$TARGET/.claude/bin/$script"
    echo "    ✓ $script"
  fi
done

echo "→ installing dynamic workflow into $TARGET/.claude/workflows/"
mkdir -p "$TARGET/.claude/workflows"
if [ -f "$REPO_ROOT/workflows/super-board-wave.js" ]; then
  cp "$REPO_ROOT/workflows/super-board-wave.js" "$TARGET/.claude/workflows/"
  echo "    ✓ super-board-wave.js"
fi

echo "→ installing .github board payload (issue form + guarded auto-add workflow) into $TARGET/.github/"
if [ -d "$REPO_ROOT/payload/github" ]; then
  mkdir -p "$TARGET/.github/ISSUE_TEMPLATE" "$TARGET/.github/workflows"
  cp "$REPO_ROOT/payload/github/ISSUE_TEMPLATE/superboard-issue.yml" "$TARGET/.github/ISSUE_TEMPLATE/"
  cp "$REPO_ROOT/payload/github/ISSUE_TEMPLATE/config.yml" "$TARGET/.github/ISSUE_TEMPLATE/"
  cp "$REPO_ROOT/payload/github/workflows/auto-add-to-project.yml" "$TARGET/.github/workflows/"
  echo "    ✓ .github issue form + config + guarded auto-add workflow"
  echo "    ! next: replace __PROJECT_URL__ in .github/workflows/auto-add-to-project.yml with this board's URL (superboard-setup Step 1 does this via sed), then enable the guard — see the header of that file"
else
  echo "    ✗ missing payload/github in repo — skipping" >&2
fi

echo
echo "✓ installed. next steps:"
echo "  1. write a config at $TARGET/.claude/super-board/configs/<slug>.json"
echo "  2. from inside Claude Code, run /super-board run <slug>"
echo
echo "see README.md for the config schema."
