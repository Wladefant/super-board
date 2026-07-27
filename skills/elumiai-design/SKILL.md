---
name: elumiai-design
description: "Elumi AI website's own design context for the design-prototyping skill: real design tokens, brand direction, staging and production URLs, and the design-system source of truth. Use whenever design-prototyping runs on Elumi AI website, or when any Elumi AI website UI surface is designed, restyled, or reviewed."
---

# Elumi AI website design context

This is the per-project half of [`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md).
That skill owns the *method* -- Agent Native Design first, dark AND light, real
Mobbin references, no production edit before approval. This file owns the
*facts* about Elumi AI website that the method needs. Read both; never restate the
method here.

Surface routing is the [Agent Native operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md).
Work lives on the board: NOT YET PROVISIONED -- the Elumi AI website has no Superboard. Provision it first (superboard-setup Step 1), then replace this line.

## Where the truth lives

| Thing | Source of truth |
|---|---|
| Repo | https://github.com/Wladefant/elumiai-website |
| Design tokens | `styles.css` in that repo -- read it live, do not trust the table below if it disagrees |
| Production | https://elumi.ai (og:url declared in index.html) |
| Staging | NONE -- no staging URL in the repo. |
| Shared toolkit | [Wladefant/design-kit](https://github.com/Wladefant/design-kit) -- prototype linter, gallery + pin-comment overlay, DTCG token schema |

**The token table below is a snapshot, not the source.** Re-read `styles.css`
before designing; when it has drifted, fix this file in the same session rather
than designing against stale values.

Unlike the HeyLolo HQ site, this one *does* have a single shared stylesheet
(`styles.css`, linked as `/styles.css?v=3`) plus one `script.js`. A token edit
is a one-file edit. There is no build step and no framework.

## Design tokens

<!-- TOKENS:BEGIN -- harvested 2026-07-28 from styles.css @ 86bd517 -->
Harvested from [`styles.css`](https://github.com/Wladefant/elumiai-website/blob/main/styles.css).
Font loading is declared in [`index.html`](https://github.com/Wladefant/elumiai-website/blob/main/index.html).

**Read the base/override direction carefully:** the base `:root` block holds the
**dark** palette; `:root[data-theme="light"]` overrides a subset of it. Tokens
absent from the light block (e.g. `--turq`, `--violet`, `--coral`, `--pink`)
are inherited from the dark base and are therefore identical in both themes.

| Token | Dark (`:root` base) | Light (`[data-theme="light"]`) | Use |
|---|---|---|---|
| `--bg` | `#070810` | `#f7f9fc` | page ground |
| `--bg-2` | `#0a0c16` | `#eef2f9` | footer / secondary ground |
| `--ink` | `#eaecf5` | `#141a29` | primary text |
| `--muted` | `#9aa0b6` | `#57607a` | secondary text |
| `--muted-2` | `#6b7188` | `#8a93a8` | tertiary text |
| `--turq` | `#5ed3d1` | *(inherited)* | primary brand turquoise |
| `--turq-deep` | `#3bbcc0` | `#2ba6ab` | deepened turquoise (links on light) |
| `--violet` | `#8b7cf6` | *(inherited)* | secondary brand |
| `--coral` | `#ff9e7d` | *(inherited)* | tertiary accent |
| `--pink` | `#ff8fb1` | *(inherited)* | quaternary accent |
| `--line` | `rgba(255,255,255,.09)` | `rgba(18,28,52,.10)` | hairline border |
| `--line-2` | `rgba(255,255,255,.05)` | `rgba(18,28,52,.06)` | fainter border |
| `--card` | `rgba(255,255,255,.028)` | `rgba(18,28,52,.028)` | card fill |
| `--card-2` | `rgba(255,255,255,.045)` | `rgba(18,28,52,.05)` | raised card fill |
| `--nav-bg` | `rgba(7,8,16,.6)` | `rgba(255,255,255,.72)` | scrolled nav (backdrop-blurred) |
| `--badge-bg` | `rgba(255,255,255,.92)` | `#ffffff` | compliance badge plate |
| `--badge-bd` | `transparent` | `rgba(18,28,52,.1)` | badge border |

**Ambient-layer tokens** (this site's distinguishing feature — see brand):
`--grain-op` `.045` / `.03` · `--grain-blend` `overlay` / `multiply` ·
`--aurora-op` `1` / `.30` ·
`--halo-bg` `radial-gradient(circle,rgba(94,211,209,.30),rgba(139,124,246,.15) 45%,transparent 68%)`
/ `radial-gradient(circle,rgba(255,255,255,.96),rgba(228,244,243,.55) 42%,transparent 72%)` ·
`--shot-halo` `radial-gradient(closest-side,rgba(94,211,209,.12),transparent 78%)`
/ `radial-gradient(closest-side,rgba(255,255,255,.9),rgba(255,255,255,0) 80%)`.

**Shadow** — `--mascot-shadow` `0 30px 50px rgba(0,0,0,.5)` /
`0 22px 40px rgba(120,140,175,.22)` · `--shot-shadow`
`0 34px 60px rgba(0,0,0,.55)` / `0 22px 44px rgba(120,140,175,.2)`. Note these
*are* properly re-tuned per theme — dark uses black, light uses a blue-grey.

**Type** — `--font` `'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif`
for body; `--display` `'Space Grotesk', 'Inter', sans-serif` for the brand
wordmark, headings, marquee and timeline steps. Both are loaded from Google
Fonts in `index.html`: `Inter` at weights 400/500/600/700 and `Space Grotesk` at
500/600/700, with `display=swap` and `preconnect` to `fonts.googleapis.com` and
`fonts.gstatic.com`. `body` line-height is `1.55`. Headings are fluid:
`.hero-title` `clamp(2.9rem,7.2vw,5.4rem)` at `letter-spacing:-.035em` /
`line-height:.98`; `h2` `clamp(1.9rem,4.2vw,3.1rem)` at `-.03em` / `1.06`.

**Radius** — a single `--radius: 22px`. Buttons and the eyebrow/kicker pills use
a literal `99px` pill radius; the investor card uses a literal `32px`. Those two
are not tokenised.

**Layout** — `--maxw: 1180px`. Section padding is
`clamp(5rem,10vw,9rem) clamp(1.1rem,4vw,2.4rem)`.

**Motion** — one shared easing token, `--ease: cubic-bezier(.22,.61,.36,1)`,
used by essentially every transition and keyframe. Reuse it rather than
introducing a second curve.

**Spacing** — there is no spacing scale token; spacing is written literally in
`rem` at each call site. Do not invent one.
<!-- TOKENS:END -->

## Brand direction

<!-- BRAND:BEGIN -- observed 2026-07-28 from the real stylesheet and markup -->
A studio marketing site with real production values, and the whole identity
lives in its **ambient layers**: a fixed SVG-turbulence grain overlay, four
blurred colour blobs ("aurora") drifting on 22–31s loops, and a turquoise
cursor-following glow. Content sits above them on `z-index: 2`. Strip those and
the site is generic — they *are* the brand.

The palette is a four-colour spectrum — turquoise `#5ed3d1`, violet `#8b7cf6`,
coral `#ff9e7d`, pink `#ff8fb1` — with turquoise as the lead. It recurs as a
gradient rather than as flat fills: the primary button is
`linear-gradient(135deg, turq, turq-deep 55%, violet)`, the hero's emphasis word
is a four-stop animated gradient clipped to text, the timeline rule runs
turquoise → violet → coral, and the brand orb is a radial gradient with a
double-glow shadow that pulses on a 3.4s loop. Type pairs `Space Grotesk` for
display with `Inter` for body, and headings are set tight and negative-tracked.

Motion is generous and constant, which is the opposite of the PolySimulator
posture — bobbing mascot, twinkling sparks, a 24s dashed orbit, a marquee that
pauses on hover, scroll-reveal with staggered `nth-child` delays, and magnetic
buttons. That is on-brand here. It is still disciplined in one respect: a
`@media (prefers-reduced-motion: reduce)` block kills **all** animation
(`animation:none!important`) and forces `.reveal` to its resting state. Any new
motion must be covered by that switch.

Deliberately off-limits:
- Adding a build step or a framework. This is hand-written HTML + one CSS file +
  one JS file, served by nginx from a Dockerfile.
- A second easing curve. `--ease` is used site-wide.
- Motion that is not disabled under `prefers-reduced-motion`.

**Theme reality check — read this before designing:** the site ships a working
light/dark toggle (`#themeToggle`, sun/moon icons swapped via
`:root[data-theme="light"]`), and the preference persists to `localStorage`
under the key `elumi-theme`. **But the two halves disagree about the default.**
The CSS base `:root` is the *dark* palette, while the inline boot script in
`index.html` reads
`localStorage.getItem('elumi-theme') || 'light'` and stamps `data-theme` before
paint — so a first-time visitor gets **light**, even though the stylesheet's
unqualified default is dark. `<meta name="theme-color">` is `#f7f9fc`, which
agrees with the light default. So: both themes are genuinely supported and both
must be designed, but treat **light as the shipped first impression** and dark
as the CSS substrate. If you touch either default, reconcile all three places
(base `:root`, the boot script, `theme-color`) or they will drift apart.
<!-- BRAND:END -->

## Surfaces

<!-- SURFACES:BEGIN -- named in the real markup and stylesheet; scope one at a time -->
Sections of the single-page site (`index.html`), each a valid scope for one
design task:

- Fixed nav (`.nav`, `.nav.scrolled` blurred state), brand orb, theme toggle
- Hero — copy column (`.eyebrow`, `.hero-title` with `.grad`, `.hero-sub`,
  `.hero-cta`, `.hero-meta`) and art column (`.hero-art`, `.halo`,
  `.lolo-float` mascot, `.orbit`/`.pip`, `.spark`), plus `.scroll-hint`
- Marquee band (`.marquee`, pauses on hover)
- Studio cards (`#studio`, four `.card`s with inline-SVG data-URI icons
  `.i-conceive` `.i-build` `.i-safe` `.i-scale`)
- Flagship / heylolo section (`#heylolo`, `.flag-grid`, `.feat-list`, the four
  compliance `.badges`, parallax `.shot-back` / `.shot-front` screenshots)
- Approach timeline (`#approach`, four-step `.timeline` with gradient rule)
- Investor CTA card (`#invest`, `.invest-card`, `.invest-lolo`)
- Footer (`#contact`, `.foot-top`, `.foot-cols`, `.foot-bottom`)
- Ambient layers (`.grain`, `.aurora` + `.blob`, `.cursor-glow`) — cross-cutting,
  not a section, but a legitimate design scope of its own
- Legal pages (`.legal-wrap` and friends) — separate routes at `/imprint/`,
  `/privacy/`, `/terms/`

Derived from markup and CSS class names, so it is a floor, not a census.
Confirm scope against https://elumi.ai.
<!-- SURFACES:END -->

## Checks before handing a design to code

- Both themes rendered and screenshotted, per design-prototyping. Both are real
  here — and light is what a first-time visitor actually sees.
- Values come from the token table above, not hard-coded hexes.
- Any new motion is covered by the existing `prefers-reduced-motion` block.
- Verified against https://elumi.ai — there is no staging environment.
- Approved by the operator before any production file is edited.
