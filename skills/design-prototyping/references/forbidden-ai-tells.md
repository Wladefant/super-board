> ## ⚠ CORRECTION 2026-06-20 PM — the operator prefers the RICH betting look
> I over-applied this list and shipped a flat/editorial "Wikipedia" page; the operator REJECTED
> that and confirmed they like the **rich, dark betting/trading aesthetic** (the `trade-panel`,
> `crypto-updown`, `leaderboard` prototypes). This file is now NARROWED. **Only these stay HARD
> bans:** (1) **colored left-edge accent rails** · (2) **arrows/chevrons of any kind** ·
> (3) **purple/indigo→blue & teal gradient clichés, gradient text, generic fonts** (Inter/Roboto/
> Arial) · (4) **emoji**. Everything else below — atmosphere/radial depth, accent glow, mono
> numbers, the trade-panel accent recolor, tasteful cards — is DEMOTED to "use with taste, don't
> overuse"; it's part of the liked look, NOT banned. **Do NOT flatten the design to avoid "AI
> slop." Keep it rich, vibrant, dense — an unmistakable betting product.**

> ## ⚠ LEGIBILITY RULE 2026-06-20 — operator-flagged TWICE
> **Never put low-contrast grey text on a same-family background** — especially the small uppercase
> "eyebrow"/overline labels (e.g. a `~44% L` grey micro-label sitting on a `~10% L` dark panel; or a
> mid-grey label on near-white in the light theme). Small/secondary text needs **more** contrast than
> body text, not less, because it's already small. The operator has now rejected this twice. Fix with
> one of: (a) raise the label's lightness to clear WCAG AA for small text (≈4.5:1) — in practice use
> the **"dim"** ink (~62–66% L on dark / ~37–44% L on light) for ANY text a human is meant to read,
> never the **"faint"** ink; (b) color the eyebrow with the **brand/accent** instead of grey; or
> (c) drop the all-caps-grey eyebrow and let the heading lead. Reserve the faintest ink ONLY for
> genuinely decorative, non-read marks (e.g. a chart gridline). Applies to BOTH themes.
> **Self-check:** grep the file for `ink-faint` / `muted-foreground` (or whatever the faintest token
> is) used on any label, eyebrow, axis, or caption — and upgrade it.

> ## ⚠ EYEBROW BAN 2026-06-28 — ABSOLUTE, operator-confirmed
> **Eyebrows are forbidden. Full stop.** No small label sitting above a heading — not uppercase,
> not letter-spaced, not monospace, not accent-colored, not grey, none. The operator's exact words:
> *"eyebrows einer der wichtigsten sachen, ich will wirklich keine eyebrows jemals sehen überhaupt."*
> This **overrides** options (b)/(c) of the legibility rule above: do NOT "recolor the eyebrow with the
> accent" and do NOT keep any overline label at all. **The heading leads, alone.** A section's place on
> the page is its label; it needs no kicker. Mechanical self-check before done: grep for an `.eyebrow`
> class and for `text-transform:uppercase` + `letter-spacing` on any small label/span above a heading —
> there must be **ZERO**. If you think a section needs a category label, it does not. Delete it.

> ## ⚠ THE HOUSE POLISH STANDARD IS MANDATORY 2026-06-28 — operator-confirmed
> Every time this design skill is used, also apply the **house polish standard** — the eight
> checkable rules in SKILL.md → "House polish standard (mandatory, both paths)": concentric radii,
> tabular-nums on changing figures, scale-0.96 press feedback, staggered interruptible reveals,
> layered shadows, subtle image outlines, glassy elevated surfaces, full reduced-motion honouring.
> Operator: *"we should definitely use this skill every time the upstream design skill is used, it
> does not change anything but makes everything so much better"* — and he specifically praised the
> **smoothness and glassiness** it adds. It is not optional polish; it is house standard.
> (This content was previously delegated to a separate `make-interfaces-feel-better` skill that no
> longer exists anywhere on this machine; it now lives inline in SKILL.md so there is no dangling
> dependency. Do not re-add an external skill invocation here.)

# FORBIDDEN — the "Opus"/AI design tells (historical list — see correction banner above)

The operator can spot AI-generated UI instantly and finds it cheap. These are the
recognizable habits this model defaults to ("typical Opus things"). **Treat every item
here as a hard ban, not a preference.** If you're reaching for one of these, stop and do
the alternative. Grounded in operator feedback (2026-06-20, after the toasts page) + research
on LLM design tells.

## The bans

1. **Side-/state-aware color recoloring of a whole component.** The "pick Up → the entire
   panel turns green; pick Down → it all turns red" move. **Explicitly forbidden by the
   operator.** Color carries meaning only on the *one* element that owns it (the P&L number,
   the specific button you're about to press) — never wash the container, border, glow, rail,
   and background all at once. One small accent, used sparingly.
2. **Colored left-edge accent bars / rails** on cards, toasts, alerts, table rows ("the color
   sides on the left side"). **Operator-flagged.** Use a small leading icon or a tinted title,
   not a vertical color stripe.
3. **Atmospheric backgrounds: radial-gradient meshes + faint grid overlay + grain.** This was
   slapped on all 26 prototypes — the single biggest tell. Default to a clean, flat, calm
   surface. Texture only when the concept genuinely calls for it, and then make it specific.
4. **Top "glow rail"** — the gradient accent line across the top edge of a panel/card. Banned.
5. **Neon glows / colored box-shadows** (`box-shadow: 0 0 Npx rgba(accent…)`) as decoration.
   Use real, soft, neutral elevation shadows (or none) — not a glow in the accent color.
6. **Wrapping everything in cards, and nesting cards in cards.** Bento-box grids. Prefer
   dividers, whitespace, and typographic grouping. A flat section with a rule beats a card.
7. **A big rounded icon tile beside/above every heading** (tinted square + outline icon).
   Template tell. Drop it; let the text lead.
8. **Pulsing/blinking "live" dots and expanding ping rings** as ambient decoration. A status
   dot can be a solid dot. Reserve any motion for a genuinely live, changing value.
9. **Monospace for *all* numbers.** JetBrains Mono on every figure is now an AI tell. Use the
   body/numeric font for headline numbers; reserve a mono **only** for dense tabular columns
   that must align (an order book, a ledger) — and even there, consider a grotesk with tnum.
10. **Indigo/purple→blue and teal→green gradients; gradient text; "living gradient" meshes.**
    Banned. Solid, intentional color.
11. **Outline (stroke) icons everywhere by default, pill-shaped everything, and 14px-rounded
    everything.** Vary radius with intent; mix icon styles to fit; sharp corners are allowed.
12. **Uniform `translateY(-2px)` + `filter:brightness(1.06)` hover on every element.** Generic.
    Give hovers real, specific feedback or none.
13. **Pure `#000`/flat gray, and generic fonts** (Inter, Roboto, Arial, Open Sans, system-ui
    as the *design* face). Use tinted neutrals and a distinctive type pairing.
14. **Centered hero + huge gradient headline + three feature cards with icon tiles.** The
    landing-page median. Avoid.
15. **ARROWS — banned forever (operator, emphatic).** No arrow glyphs anywhere: not the `→`
    trailing every link / "View all →" / CTA, not `↗` external-link arrows, not `↘ ↑ ↓ ←`,
    not chevron icons used as decoration or "see more". They're a top AI tell. Links are just
    links (color/underline on hover); "see more" is a plain text link with no glyph. For
    genuine up/down *direction* (a price moving), use the word ("Up"/"Down") + color, or a
    small triangle/caret only if truly essential — default to no arrow at all.

## Do instead (the spirit)
- **Hierarchy through type and space, not color and boxes.** Weight, size, and whitespace do
  the work; color is a rare accent.
- **Flat, confident surfaces.** Borders/dividers over cards. Calm backgrounds.
- **One distinctive idea per surface** that's specific to *this* product (a prediction-market
  sim), not a motif copy-pasted across every screen.
- **Restraint.** If a flourish doesn't earn its place, cut it. The operator praised "you don't
  use AI design things" — keep earning that.

## Self-check before declaring a prototype done
Grep your file and your eyes for: any arrow glyph (`→ ← ↗ ↘ ↑ ↓`) or chevron icon, a
`radial-gradient` background, `box-shadow:0 0`, a left-border accent rail, `--side`/state
recolor, `JetBrains Mono` on non-tabular numbers, pulsing dots, an icon tile next to every
heading. If present without a specific reason, remove it.
