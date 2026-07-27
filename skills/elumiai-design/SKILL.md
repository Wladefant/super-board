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
Work lives on the board: Elumi AI Website https://github.com/users/Wladefant/projects/12 -- linked to `Wladefant/elumiai-website`. IDs in [BOARD-IDS](https://github.com/Wladefant/super-board/blob/main/docs/reference/BOARD-IDS.md).

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

## House-ban conflicts

<!-- BANS:BEGIN -- audited 2026-07-28 against main @ 86bd517 -->
This site is the reason the precedence rule exists. What it **actually ships** against the
five hard bans in
[`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md#house-style--the-hard-bans),
audited across `index.html`, `styles.css`, `script.js` and the three legal pages at
[`86bd517`](https://github.com/Wladefant/elumiai-website/commit/86bd51731536623c65d0dc5b393283cd9c0aadd4).

This is a record of reality and is never edited to make the site look compliant. For
anything you design new,
[the ban wins](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md#when-a-real-token-collides-with-a-hard-ban):
drop the pattern, keep the tokens above unchanged.

**1. Left-edge accent rails — CLEAN.** Zero `border-left` in any file. The one accent bar
is *horizontal*: `styles.css:235` `.card::after{top:-1px;left:0;height:2px;width:24px;
background:linear-gradient(90deg,var(--turq),var(--violet))}`. `.nav-links a::after` is an
underline; `.flag-art::before` / `.invest-card::before` are radial halos.

**2. Arrows / chevrons — VIOLATED (site-wide pattern, 5 sites).** Right-arrow SVGs inside
both primary CTAs — `index.html:78` and `:169`,
`<svg viewBox="0 0 24 24" class="ico"><path d="M5 12h14M13 6l6 6-6 6"/></svg>` — reinforced
by the hover nudge `styles.css:107` `.btn:hover .ico{transform:translateX(4px)}`. Plus
`&larr; Back to Elumi AI` on all three legal pages (`imprint.html:38`, `privacy.html:38`,
`terms.html:38`). Borderline, not counted: `styles.css:242` `.i-build::after` draws a
chevron *pair* that reads as code brackets `< >`, a card icon rather than navigation.

**3. Gradient clichés / gradient text / generic fonts — VIOLATED, all three clauses.**
- *Gradient text*: `styles.css:177-181` `.grad` — `linear-gradient(100deg,var(--turq),
  var(--violet) 45%,var(--coral) 80%,var(--turq))` with `background-clip:text;
  color:transparent`, applied to the word "love." in the hero `<h1>` (`index.html:69`).
  One instance, but it is the headline.
- *Teal→violet washes*: the `--turq #5ed3d1` → `--violet #8b7cf6` ramp drives every primary
  button (`styles.css:109` `.btn-primary{background:linear-gradient(135deg,…)}`), the
  aurora blobs `.b1`/`.b2`, `.timeline::before`, `.card::after` and `.invest-card`.
- *Fonts*: Inter and Space Grotesk are loaded from Google Fonts at `index.html:26`. The
  **display** face is `--display:'Space Grotesk','Inter',sans-serif` — distinctive, and
  compliant; it drives `h1`, `h2`, `.card h3`, `.brand`, `.marquee-track`, `.t-step`. The
  **body** face is `--font:'Inter',…` (`styles.css:30`), applied at `styles.css:61` —
  **Inter is first, and that is the violation.** Replacing the body face does not touch the
  display face.

> **NAMED BRAND EXCEPTION — the orb mark only.** `favicon.svg` and `.brand-orb`
> (`styles.css:130-135`) render the same teal→violet radial, and that mark **is** the Elumi
> AI identity. The gradient is therefore permitted **on the logo/orb and nowhere else**.
> It does **not** licence `.btn-primary`, `.grad` hero text, the aurora blobs or any other
> surface — those are decorative uses of the brand ramp and a redesign drops them. This is
> the only named exception for this product; anything else needs the operator to approve it
> and be written in here first.

**4. Emoji — CLEAN.** No pictographic emoji (U+1F300–1FAFF) in HTML, CSS or JS. Two
Dingbats-block *typographic* glyphs exist and are recorded so nobody re-litigates them:
`<em>✳</em>` (U+2733) as a marquee separator (`index.html:104-105`, 14×) and
`<span class="tick" aria-hidden="true">✓</span>` (U+2713, `index.html:155-157`, 3×). Both
are monochrome with no VS16 presentation selector — not emoji, but do not add more.

**5. Eyebrows / kickers / overlines — VIOLATED, and this is the most systematic one.**
Every major section carries one:
- `.eyebrow` (`styles.css:165-169`, `.16em` uppercase) → `index.html:64`
  `<p class="eyebrow reveal"><span class="dot"></span>AI product studio</p>` immediately
  before the `<h1>`.
- `.kicker` (`styles.css:224`, `.16em` uppercase, turquoise) → **4 uses**, each directly
  before an `<h2>`: "The studio" (`:112`), "Flagship product" (`:146`), "How we work"
  (`:187`), "For investors" (`:223`).
- `.legal-eyebrow` (`styles.css:368`, `.18em` uppercase) → `<p>Legal</p>` before the `<h1>`
  on all three legal pages.
- The same shape unnamed: `.t-step` (`styles.css:279`, `.12em` uppercase) sits immediately
  before each timeline `<h3>` — "Step 01"–"Step 04".

Any redesigned section here ships with the heading leading alone. Expect a mixed page while
the migration is partial — that is the correct interim state, not a regression.
<!-- BANS:END -->

## Checks before handing a design to code

- Both themes rendered and screenshotted, per design-prototyping. Both are real
  here — and light is what a first-time visitor actually sees.
- Values come from the token table above, not hard-coded hexes.
- Any new motion is covered by the existing `prefers-reduced-motion` block.
- Verified against https://elumi.ai — there is no staging environment.
- Approved by the operator before any production file is edited.
