#!/usr/bin/env bash
# Generate a project's own design skill -- the `<slug>-design` file that the
# design-prototyping skill looks for and that was missing for every project.
#
# Writes into the super-board clone (so it is versioned and shared) and
# junctions it into ~/.claude/skills (so Claude Code actually loads it).
#
# Usage:
#   PROJECT="PolySimulator" SLUG=polysim REPO=Bavariance/polysimulator \
#   BOARD=https://github.com/users/Wladefant/projects/3 \
#   PROD=https://polysimulator.com STAGING=https://staging.polysimulator.com \
#   TOKENS=frontend/app/globals.css \
#   bash scripts/new-project-design-skill.sh
#
# Every field is required. There are no invented defaults on purpose: a design
# skill carrying guessed tokens or a guessed staging URL is worse than none.

set -euo pipefail

for v in PROJECT SLUG REPO BOARD PROD STAGING TOKENS; do
  [ -n "${!v:-}" ] || { echo "missing required env var: $v" >&2; exit 1; }
done

SB_SRC="${SB_SRC:-$HOME/.claude/super-board-src}"
OUT_DIR="$SB_SRC/skills/${SLUG}-design"
mkdir -p "$OUT_DIR"

cat > "$OUT_DIR/SKILL.md" <<'TEMPLATE'
---
name: {{SLUG}}-design
description: "{{PROJECT}}'s own design context for the design-prototyping skill: real design tokens, brand direction, staging and production URLs, and the design-system source of truth. Use whenever design-prototyping runs on {{PROJECT}}, or when any {{PROJECT}} UI surface is designed, restyled, or reviewed."
---

# {{PROJECT}} design context

This is the per-project half of [`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md).
That skill owns the *method* -- Agent Native Design first, dark AND light, real
Mobbin references, no production edit before approval. This file owns the
*facts* about {{PROJECT}} that the method needs. Read both; never restate the
method here.

Surface routing is the [Agent Native operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md).
Work lives on the board: {{BOARD}}

## Where the truth lives

| Thing | Source of truth |
|---|---|
| Repo | https://github.com/{{REPO}} |
| Design tokens | `{{TOKENS}}` in that repo -- read it live, do not trust the table below if it disagrees |
| Production | {{PROD}} |
| Staging | {{STAGING}} |
| Shared toolkit | [Wladefant/design-kit](https://github.com/Wladefant/design-kit) -- prototype linter, gallery + pin-comment overlay, DTCG token schema |

**The token table below is a snapshot, not the source.** Re-read `{{TOKENS}}`
before designing; when it has drifted, fix this file in the same session rather
than designing against stale values.

## Design tokens

<!-- TOKENS:BEGIN -- replace this block with the real values from {{TOKENS}} -->
Not yet harvested. Read `{{TOKENS}}` in https://github.com/{{REPO}} and paste
the real custom properties here (colour, type scale, spacing, radius, shadow).
Do not invent values to fill this in.
<!-- TOKENS:END -->

## Brand direction

<!-- BRAND:BEGIN -- replace with the real, observed direction -->
Not yet written. Describe what the product actually looks and feels like today,
in a few concrete sentences a designer could act on, plus the things that are
deliberately off-limits. Base it on the live UI, not on aspiration.
<!-- BRAND:END -->

## Surfaces

<!-- SURFACES:BEGIN -- list the real named surfaces of this product -->
Not yet listed. Name the product's actual surfaces (the panels, pages and
modals a design task will be scoped to), so "work one surface at a time" has
something to point at.
<!-- SURFACES:END -->

## Checks before handing a design to code

- Both themes rendered and screenshotted, per design-prototyping.
- Values come from the token table above, not hard-coded hexes.
- Verified against {{STAGING}}, not only against the prototype.
- Approved by the operator before any production component is edited.
TEMPLATE

# Substitute. Use a delimiter that cannot appear in a URL or path.
sed -i \
  -e "s|{{PROJECT}}|$PROJECT|g" \
  -e "s|{{SLUG}}|$SLUG|g" \
  -e "s|{{REPO}}|$REPO|g" \
  -e "s|{{BOARD}}|$BOARD|g" \
  -e "s|{{PROD}}|$PROD|g" \
  -e "s|{{STAGING}}|$STAGING|g" \
  -e "s|{{TOKENS}}|$TOKENS|g" \
  "$OUT_DIR/SKILL.md"

echo "wrote $OUT_DIR/SKILL.md"

# Junction it into ~/.claude/skills so Claude Code loads it, matching how
# superboard-setup and claudex-optimized are wired. Junctions need no admin.
LINK="$HOME/.claude/skills/${SLUG}-design"
if [ -e "$LINK" ]; then
  echo "note: $LINK already exists -- left untouched"
else
  cmd.exe /c mklink /J "$(cygpath -w "$LINK")" "$(cygpath -w "$OUT_DIR")" >/dev/null
  echo "junctioned $LINK -> $OUT_DIR"
fi

echo
echo "NEXT: fill the three marked blocks (TOKENS, BRAND, SURFACES) from the real"
echo "repo and the real live UI, then commit the new skill to super-board."
