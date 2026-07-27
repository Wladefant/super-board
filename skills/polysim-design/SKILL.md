---
name: polysim-design
description: "PolySimulator's own design context for the design-prototyping skill: real design tokens, brand direction, staging and production URLs, and the design-system source of truth. Use whenever design-prototyping runs on PolySimulator, or when any PolySimulator UI surface is designed, restyled, or reviewed."
---

# PolySimulator design context

This is the per-project half of [`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md).
That skill owns the *method* -- Agent Native Design first, dark AND light, real
Mobbin references, no production edit before approval. This file owns the
*facts* about PolySimulator that the method needs. Read both; never restate the
method here.

Surface routing is the [Agent Native operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md).
Work lives on the board: PolySimulator https://github.com/users/Wladefant/projects/10 -- the board is owned by the Wladefant user while `Bavariance/polysimulator` is an org repo, so GitHub refuses the repo link; add issues to the board by node ID. IDs in [BOARD-IDS](https://github.com/Wladefant/super-board/blob/main/docs/reference/BOARD-IDS.md).

## Where the truth lives

| Thing | Source of truth |
|---|---|
| Repo | https://github.com/Bavariance/polysimulator |
| Design tokens | `frontend/app/globals.css` in that repo -- read it live, do not trust the table below if it disagrees |
| Production | https://polysimulator.com |
| Staging | https://staging.polysimulator.com |
| Shared toolkit | [Wladefant/design-kit](https://github.com/Wladefant/design-kit) -- prototype linter, gallery + pin-comment overlay, DTCG token schema |

**The token table below is a snapshot, not the source.** Re-read `frontend/app/globals.css`
before designing; when it has drifted, fix this file in the same session rather
than designing against stale values.

## Design tokens

<!-- TOKENS:BEGIN -- harvested 2026-07-28 from frontend/app/globals.css @ 3c82c13 -->
Harvested from [`frontend/app/globals.css`](https://github.com/Bavariance/polysimulator/blob/main/frontend/app/globals.css).

**Colour**

| Token | Value | Use |
|---|---|---|
| `--color-bg` | `#020512` | page background (body paints `#050912`) |
| `--color-surface-1` | `#0b1120` | primary raised surface |
| `--color-surface-2` | `#17191a` | secondary surface |
| `--color-surface-3` | `#161f2f` | tertiary surface |
| `--color-surface-4` | `#0d141b` | recessed surface |
| `--color-border` | `rgba(255,255,255,0.06)` | hairline borders |
| `--color-brand` | `#00744e` | brand green |
| `--color-brand-accent` | `#39dcb2` | accent / focus |
| `--color-text-primary` | `#dcdbd8` | body text |
| `--color-text-secondary` | `#cbc7c3` | secondary text |
| `--color-text-muted` | `#9daab8` | muted text |
| `--color-success-bg` / `--color-success-text` | `#1b4e2e` / `#55d889` | gains |
| `--color-danger-bg` / `--color-danger-text` | `#4e1b1b` / `#ff7a7a` | losses |

Three accent tokens live in `tailwind.config.ts`, not in the CSS, and are
referenced by name from the CSS comments: `accent.green #2dd6a0`,
`accent.amber #f5c769`, `accent.blue #4f8dff`. Read that file too before using
them; the CSS is not their source.

**Type** — `DM Sans` via `--font-sans`, then system stack. Sizes `--font-size-xs`
`0.7rem` · `sm` `0.85rem` · `base` `0.95rem` · `md` `1.05rem` · `lg` `1.25rem` ·
`xl` `2rem` · `2xl` `3rem` (drops to `2.4rem` under 860px). Line heights
`--line-height-tight` `1.1`, `--line-height-base` `1.4`. `font-synthesis: none`
on the weight utilities; `font-variant-numeric: tabular-nums` on `body`.

**Spacing** — `--space-1..8` = `4 8 12 16 24 32 48 64` px.

**Radius** — `--radius-sm` `6px` · `md` `8px` · `lg` `14px` · `xl` `16px`.

**Shadow** — `--shadow-sm` `0 1px 2px rgba(0,0,0,0.5)` · `--shadow-md`
`0 4px 12px rgba(0,0,0,0.4)` · `--shadow-focus`
`0 0 0 2px rgba(57,220,178,0.4)`. Layout cap `--layout-max: 1100px`.
<!-- TOKENS:END -->

## Brand direction

<!-- BRAND:BEGIN -- observed 2026-07-28 from the real stylesheet -->
Dark-first trading terminal, not a consumer app. Near-black navy ground with
low-contrast raised surfaces and 6%-white hairlines; a single teal-green brand
pair (`#00744e` / `#39dcb2`) carries identity, and red/green are reserved for
PnL meaning rather than decoration. Numbers are the content: tabular figures
throughout, tight leading, a wide type scale that jumps straight from body text
to `2rem`/`3rem` display for balances and headline stats.

Motion is deliberately restrained and the repo argues the case in its own
comments: entrances are "a calm rise + settle (no bounce; this lives in a
fintech app, not a game)", the plan badge is "a small struck medal, not a
saturated gradient sticker", and its sheen is "a glint, not a shimmer" with a
long quiet gap. Everything animates on transform/opacity only, and **every**
animation has a `prefers-reduced-motion` off-switch. Match that bar or drop the
motion.

Deliberately off-limits: colour that pulses or breathes for its own sake;
hard-coded hexes where a token exists (a Codex review already caught Tailwind
`amber-400` standing in for the `accent.amber` token); broad substring
selectors like `[class*="rounded-2xl"]` that silently deform unrelated controls
— that exact pattern shipped a 180px-tall sign-in button on 2026-06-09 and is
now an explicit opt-in class.

**Theme reality check:** `:root` is dark and there is no light block — the
product ships dark-only today. `design-prototyping` still requires you to
present a light theme alongside the dark one; treat light as a proposal that
needs its own token set and operator sign-off, not as something already
supported.
<!-- BRAND:END -->

## Surfaces

<!-- SURFACES:BEGIN -- named in the real stylesheet; scope one at a time -->
Surfaces the stylesheet actually names, each a valid scope for one design task:

- Markets grid + market card (`.markets-grid-container`, `.market-card-image`,
  `.markets-section`) — including the search/filter shimmer state
- Order book (`.orderbook-scrollbar`)
- Leaderboard widget (`.leaderboard-widget`)
- Profile hero + subscription plan badge (`.metal-badge`, Pro / Pro+ tiers)
- Pricing page bullets and their UTM arrival highlight (`.animate-pulse-bullet`)
- Sign-in OTP (the surface the substring-selector bug deformed)
- Admin analytics panel (`.admin-bar-grow`)
- Crypto UpDown next-round autoplay card (`.updown-nextround`)
- Season launch welcome overlay (`.s2w-*`)
- CTA panels (`.cta-panel`), quick links row, footer
- Skeleton/loading states (`.skeleton-shimmer`, `.card-enter`)

This list is derived from CSS, so it is a floor, not a census — surfaces with no
bespoke CSS will not appear here. Confirm scope against the running app.
<!-- SURFACES:END -->

## House-ban conflicts

<!-- BANS:BEGIN -- audited 2026-07-28 against main @ 3c82c13 -->
What PolySimulator **actually ships** against the five hard bans in
[`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md#house-style--the-hard-bans),
audited over 274 `.tsx` files at
[`3c82c13`](https://github.com/Bavariance/polysimulator/commit/3c82c13e21bf1eb210506c7b82eebaf219b55faf).
This records reality and is never edited to make the product look compliant. For anything
you design new,
[the ban wins](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md#when-a-real-token-collides-with-a-hard-ban)
— drop the pattern, keep the tokens above unchanged. **No named brand exceptions exist**
for this product.

**1. Left-edge accent rails — VIOLATED, pervasively and by design.** There is a module
whose only job is emitting them: `frontend/lib/categoryStripe.ts:5-15` maps categories to
`border-l-orange-500/60`, `border-l-blue-500/60` etc., consumed by
`frontend/components/MarketCard.tsx:155` (`border border-l-2 … ${getCategoryStripe(...)}`)
on every card in the market grid, and by `GroupedMarketCard.tsx:354`. Also
`components/market/PositionsPanel.tsx:509` and `app/admin/b2b-api/page.tsx:115`
(`border-l-2 border-violet-300/40`). **Category identity currently rides on the rail** —
a redesign has to re-encode that signal (a category chip, or the label's own colour) rather
than just deleting the border. `globals.css` itself is clean: zero `::before`, zero
`border-left` in 609 lines.

**2. Arrows / chevrons — VIOLATED, pervasively.** 269 lines across 87 files contain an
arrow glyph; decorative CTA arrows dominate: `app/about/page.tsx:118`
`View all paper trading FAQs →`, `app/backtesting/page.tsx:300` `Start Paper Trading Free →`
and `:544` `buttonLabel="Notify Me at Launch →"`, plus `Compare plans →`,
`Full roadmap →`, `Open preview →`, `Full reference ↗`, and an animated one at
`app/integrations/telegram/TelegramClient.tsx:663`
(`<span className="transition group-hover:translate-x-0.5">→</span>`). Icons are
`react-icons/fi`: decorative uses include `FiArrowUpRight` on a guide link and
`FiChevronRight` as a row affordance; structural uses (pagination `FiChevronLeft/Right`,
`FiArrowUp/Down` sort state, `FiChevronDown` with `group-open:rotate-180` on `<details>`)
are a separate class of thing. Many `→` hits are inside code comments and don't count.

**3. Gradient clichés / gradient text / generic fonts — gradients VIOLATED; font CLEAN.**
- *Gradient text*: four files — `components/CompactHero.tsx:52`
  (`text-transparent bg-clip-text bg-gradient-to-r from-accent-green to-emerald-400` on
  "Risk-Free"), `app/markets/[id]/radar/page.tsx:303` (`from-purple-400 to-cyan-400` on
  "AI ANALYSIS"), `app/leaderboard/S1HallOfFame.tsx:427`,
  `app/topup/success/TopupSuccessClient.tsx:343`.
- *Washes*: 99 `bg-gradient-to-*` lines, squarely in the banned hue family —
  `app/page.tsx:162` `from-purple-500/20 to-pink-500/20`; `app/backtesting/page.tsx:531`
  `from-purple-500/20 via-accent-blue/10 to-accent-green/20`;
  `app/profile/[username]/page.tsx:380` `from-indigo-500/10 …`;
  `app/markets/[id]/radar/page.tsx:300,330` `from-purple-500 to-cyan-500`.
- *Font*: **not a violation.** `--font-sans` is never defined in the repo, so the real first
  face is **DM Sans** for body *and* headings (`globals.css:6`, `tailwind.config.ts:39`),
  loaded by a raw Google Fonts `<link>` at `app/layout.tsx:166` rather than `next/font` —
  deliberate, per the comment at `:22-34` (the build host could not reach
  `fonts.gstatic.com`). **Stale artifact to be aware of:** `app/layout.tsx:221-226`
  preloads an **Inter** `.woff2`, but Inter appears in no `font-family` anywhere — a dead
  preload, not a type choice. Don't read it as an Inter dependency, and don't "restore" it.

**4. Emoji — VIOLATED, pervasively.** 375 lines across 69 files. Rendered instances include
`app/leaderboard/page.tsx:59` `<h1>🏆 Leaderboard</h1>`, rank medals `👑 🥈 🥉` in
`LeaderboardClient.tsx:146-148` (plus `🏁` and empty-state `🔍`), six emoji-prefixed FAQ
section headers (`app/faq/page.tsx:171-243`), a constants array literally keyed `emoji:`
rendered into feature cards (`app/backtesting/page.tsx:220-240,393-403`), and
`app/admin/page.tsx:842` `Hot 🔥`. Note a chunk of the raw count is *comments in the
OG-image routes explaining that emoji were removed* because they tofu'd — those routes now
use SVG marks and are not violations.

**5. Eyebrows / kickers / overlines — VIOLATED, pervasively, and one is a named component.**
`app/api-keys/components/primitives.tsx:130-149` ships a `kicker` prop with the comment
"Section heading with an eyebrow kicker", rendering
`font-mono text-[10px] uppercase tracking-[0.18em]` directly above the `<h2>`. The same
shape is hand-rolled ~20 times above an `h1/h2/h3` — `app/api-trading/page.tsx:567→570`
(repeated at `:657,:681,:737,:881`), `app/company/page.tsx:168→171`,
`app/admin/b2b-api/page.tsx:255`, `app/admin/page.tsx:705`, `app/blog/[slug]/page.tsx:90`,
`app/roadmap/page.tsx:279`, and all five `app/integrations/*Client.tsx`. `eyebrow` is also
a first-class prop of the OG-image brand lockup (`app/api/og/_shared/layout.tsx:51-87`).
Removing the pattern means changing `primitives.tsx` and the OG lockup, not just page copy.
<!-- BANS:END -->

## Checks before handing a design to code

- Both themes rendered and screenshotted, per design-prototyping.
- Values come from the token table above, not hard-coded hexes.
- Verified against https://staging.polysimulator.com, not only against the prototype.
- Approved by the operator before any production component is edited.
