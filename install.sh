#!/usr/bin/env bash
# super-board installer — pinned, versioned, idempotent, verifiable.
#
# Installation is a release event, not a file copy. Every run states which
# commit of this repository it is installing, which release number that commit
# claims, and which design-skill revision travels with it. All three end up in
# `.claude/super-board/install-manifest.json` next to a SHA-256 of every
# installed file, so "what is actually installed here?" is answerable later
# without guessing.
#
# Usage:
#   ./install.sh --repo-root PATH \
#                --user-home PATH \
#                --source-sha SHA \
#                --release-version VERSION \
#                --design-skill-source URL \
#                --design-skill-sha SHA \
#                --design-skill-checksum SHA256 \
#                [--slug NAME] [--allow-downgrade]
#
#   --repo-root              the repository to install into
#   --user-home              the home directory this machine's state belongs to;
#                            passed explicitly because $HOME differs under CI,
#                            MSYS, and sudo, and a wrong guess writes state into
#                            somebody else's profile
#   --source-sha             the commit of THIS tree that is being installed;
#                            a HEAD that differs is refused with exit 65
#   --release-version        must equal the VERSION file in this tree
#   --design-skill-*         source, revision, and checksum of the design skill
#                            that this release is pinned against; recorded as
#                            three separate manifest fields
#   --slug                   configuration slug (default: the repo-root name)
#   --allow-downgrade        documented override: install a release OLDER than
#                            the one already installed. Without it a downgrade
#                            is refused, because silently going backwards is how
#                            a board ends up running a policy that was fixed.
#
# Copying, checksumming, executable bits, and stale-file pruning are delegated
# to `scripts/super-board-install-verify.py`, so there is exactly one
# implementation of the layout and this script cannot drift from it.
#
# Exit: 0 installed and verified · 64 invalid invocation · 65 the release
#       contract was not satisfied (SHA mismatch, incomplete payload, refused
#       downgrade, or failed verification).

set -euo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/super-board-python.sh
. "$SOURCE_ROOT/scripts/super-board-python.sh"

REPO_ROOT=""
USER_HOME=""
SOURCE_SHA=""
RELEASE_VERSION=""
DESIGN_SKILL_SOURCE=""
DESIGN_SKILL_SHA=""
DESIGN_SKILL_CHECKSUM=""
SLUG=""
ALLOW_DOWNGRADE=""

usage() {
  sed -n '2,44p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

die_usage() {
  echo "install.sh: $1" >&2
  usage
  exit 64
}

while [ $# -gt 0 ]; do
  case "$1" in
    --repo-root) REPO_ROOT="${2:-}"; shift 2 ;;
    --user-home) USER_HOME="${2:-}"; shift 2 ;;
    --source-sha) SOURCE_SHA="${2:-}"; shift 2 ;;
    --release-version) RELEASE_VERSION="${2:-}"; shift 2 ;;
    --design-skill-source) DESIGN_SKILL_SOURCE="${2:-}"; shift 2 ;;
    --design-skill-sha) DESIGN_SKILL_SHA="${2:-}"; shift 2 ;;
    --design-skill-checksum) DESIGN_SKILL_CHECKSUM="${2:-}"; shift 2 ;;
    --slug) SLUG="${2:-}"; shift 2 ;;
    --allow-downgrade) ALLOW_DOWNGRADE="1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die_usage "unknown argument: $1" ;;
  esac
done

for pair in \
  "--repo-root:$REPO_ROOT" \
  "--user-home:$USER_HOME" \
  "--source-sha:$SOURCE_SHA" \
  "--release-version:$RELEASE_VERSION" \
  "--design-skill-source:$DESIGN_SKILL_SOURCE" \
  "--design-skill-sha:$DESIGN_SKILL_SHA" \
  "--design-skill-checksum:$DESIGN_SKILL_CHECKSUM"; do
  if [ -z "${pair#*:}" ]; then
    die_usage "${pair%%:*} is required"
  fi
done

if [ ! -d "$REPO_ROOT" ]; then
  echo "install.sh: --repo-root does not exist: $REPO_ROOT" >&2
  exit 65
fi

# The release number is not a free-text label: it must be the one this tree
# declares, or the manifest would record a provenance that is not true.
DECLARED_VERSION="$(tr -d ' \t\r\n' < "$SOURCE_ROOT/VERSION")"
if [ "$RELEASE_VERSION" != "$DECLARED_VERSION" ]; then
  echo "install.sh: --release-version $RELEASE_VERSION does not match the VERSION file ($DECLARED_VERSION)" >&2
  exit 65
fi

# The pinned commit. A tree at a different HEAD may contain anything.
HEAD_SHA="$(git -C "$SOURCE_ROOT" rev-parse HEAD 2>/dev/null || true)"
if [ -z "$HEAD_SHA" ]; then
  echo "install.sh: the source tree's HEAD could not be read; refusing to install a release whose provenance cannot be stated" >&2
  exit 65
fi
if [ "$HEAD_SHA" != "$SOURCE_SHA" ]; then
  echo "install.sh: the source tree is at $HEAD_SHA, not the pinned $SOURCE_SHA" >&2
  exit 65
fi

PY="$(sb_python)" || exit 65
VERIFY="$SOURCE_ROOT/scripts/super-board-install-verify.py"
MANIFEST="$REPO_ROOT/.claude/super-board/install-manifest.json"

set -- \
  --source-root "$(sb_native_path "$SOURCE_ROOT")" \
  --repo-root "$(sb_native_path "$REPO_ROOT")" \
  --user-home "$USER_HOME" \
  --source-sha "$SOURCE_SHA" \
  --release-version "$RELEASE_VERSION" \
  --design-skill-source "$DESIGN_SKILL_SOURCE" \
  --design-skill-sha "$DESIGN_SKILL_SHA" \
  --design-skill-checksum "$DESIGN_SKILL_CHECKSUM" \
  --skip-source-check
[ -n "$SLUG" ] && set -- "$@" --slug "$SLUG"
[ -n "$ALLOW_DOWNGRADE" ] && set -- "$@" --allow-downgrade

echo "→ installing release $RELEASE_VERSION from $SOURCE_SHA into $REPO_ROOT"
PYTHONPATH="$(sb_native_path "$SOURCE_ROOT/scripts")" "$PY" -B "$VERIFY" install "$@"

echo "→ verifying the installed tree against its manifest"
PYTHONPATH="$(sb_native_path "$SOURCE_ROOT/scripts")" "$PY" -B "$VERIFY" verify \
  --manifest "$(sb_native_path "$MANIFEST")" \
  --repo-root "$(sb_native_path "$REPO_ROOT")" \
  --json

cat <<'NEXT'

✓ installed and verified. next steps:
  1. fill in the project identity in .claude/super-board/configs/<slug>.json
     (it ships with activation_mode "off" on purpose)
  2. from inside Claude Code, run /super-board run <slug>

see README.md for the config schema.
NEXT
