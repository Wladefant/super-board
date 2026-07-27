---
name: heylolo-hq-design
description: "HeyLolo Business HQ's own design context for the design-prototyping skill: real design tokens, brand direction, staging and production URLs, and the design-system source of truth. Use whenever design-prototyping runs on HeyLolo Business HQ, or when any HeyLolo Business HQ UI surface is designed, restyled, or reviewed."
---

# HeyLolo Business HQ design context

This is the per-project half of [`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md).
That skill owns the *method* -- Agent Native Design first, dark AND light, real
Mobbin references, no production edit before approval. This file owns the
*facts* about HeyLolo Business HQ that the method needs. Read both; never restate the
method here.

Surface routing is the [Agent Native operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md).
Work lives on the board: HeyLolo product board https://github.com/users/Wladefant/projects/4 -- NOT CONFIRMED that HQ cards live there; verify before filing.

## Where the truth lives

| Thing | Source of truth |
|---|---|
| Repo | https://github.com/Wladefant/heylolo-hq |
| Design tokens | `index.html` (inline `<style>` block) in that repo -- read it live, do not trust the table below if it disagrees |
| Production | https://hq.wladefant.de (nginx basic auth) |
| Staging | NONE -- there is no staging environment; a push to main auto-deploys straight to production. |
| Shared toolkit | [Wladefant/design-kit](https://github.com/Wladefant/design-kit) -- prototype linter, gallery + pin-comment overlay, DTCG token schema |

**The token table below is a snapshot, not the source.** Re-read `index.html`
before designing; when it has drifted, fix this file in the same session rather
than designing against stale values.

## ⚠ There is no shared stylesheet — read this first

This is an internal, static, nginx-served knowledge base. **Every page is a
standalone HTML file with its own inline `<style>` block, and the token block is
copy-pasted into each one.** The repo contains no `.css` file at all (`assets/`
holds only `company/` images).

Practical consequence: *changing a token is not a one-file edit.* A GitHub code
search for `accent-wash` matched `tina.html` plus pages under `companies/`,
`docs/` and `investors/` — and that result set was capped, so the real number of
copies is higher than what was enumerated. Before proposing any token change,
enumerate every file that carries a copy and change them together, or the site
drifts page by page.

The values below were read directly out of `index.html`. Other pages were **not**
individually verified in this session; do not assume a given page is identical
until you have opened it.

## Design tokens

<!-- TOKENS:BEGIN -- harvested 2026-07-28 from the index.html inline <style> @ 6ea8eb3 -->
Harvested from [`index.html`](https://github.com/Wladefant/heylolo-hq/blob/main/index.html).

**Colour — light (`:root`) / dark**

| Token | Light | Dark | Use |
|---|---|---|---|
| `--page` | `#f6f8f8` | `#0e1211` | page ground |
| `--surface` | `#fdfefe` | `#171d1c` | cards / panels |
| `--chart-surface` | `#fbfcfc` | `#1a201f` | chart plot area |
| `--ink` | `#10201f` | `#f4f7f6` | primary text |
| `--ink-2` | `#4c5a58` | `#b9c4c1` | secondary text |
| `--muted` | `#8a938f` | `#8a938f` | muted text (**identical in both modes**) |
| `--grid` | `#e2e6e4` | `#2a3230` | chart gridlines |
| `--baseline` | `#c2c9c6` | `#3a4341` | chart baseline |
| `--ring` | `rgba(16,32,31,0.10)` | `rgba(255,255,255,0.10)` | focus / hairline |
| `--accent` | `#0f8a8a` | `#2cb5b5` | teal accent |
| `--accent-ink` | `#0b6c6c` | `#55cfcf` | accent text + links |
| `--accent-wash` | `rgba(15,138,138,0.08)` | `rgba(44,181,181,0.10)` | accent fill |

**Status colours.** `--good` `#0ca30c` · `--warn` `#fab219` · `--serious`
`#ec835a` · `--critical` `#d03b3b` are declared **only in `:root`** and are not
redefined for dark — only their text/wash companions are: `--good-text`
`#006300` (light) / `#0ca30c` (dark), `--warn-wash` `rgba(250,178,25,0.12)` /
`rgba(250,178,25,0.10)`, `--critical-wash` `rgba(208,59,59,0.08)` /
`rgba(208,59,59,0.12)`. So the solid status hues are shared across both themes
by design; only their contrast partners flip.

**Chart series** `--s1`…`--s8` — light: `#2a78d6` `#1baf7a` `#eda100` `#008300`
`#4a3aa7` `#e34948` `#e87ba4` `#eb6834`; dark: `#3987e5` `#199e70` `#c98500`
`#008300` `#9085e9` `#e66767` `#d55181` `#d95926` (note `--s4` is `#008300` in
both).

**Funnel ramp** `--f1`…`--f4` — light: `#86b6ef` `#5598e7` `#2a78d6` `#1c5cab`;
dark: `#3987e5` `#2a78d6` `#256abf` `#184f95`.

**Type** — there is **no webfont**. `body` uses the CSS `font` shorthand:
`16px/1.62 system-ui, -apple-system, "Segoe UI", sans-serif`, with
`-webkit-font-smoothing: antialiased`. There is no `font-family` declaration and
no `fonts.googleapis.com` link anywhere in `index.html`. Do not introduce one
without saying so — it changes the page's load profile as well as its look.

**Radius / spacing / shadow** — **none exist as tokens.** Grepping `index.html`
for `--radius*` and `--space*` returns nothing; corner radii and spacing are
written literally at each call site. This is the honest state of the system, not
an omission from this file. If a design needs a radius scale, propose creating
one rather than pretending to read one.
<!-- TOKENS:END -->

## Brand direction

<!-- BRAND:BEGIN -- observed 2026-07-28 from the real index.html -->
An internal business dashboard, not a marketing site — it sits behind nginx
basic auth and is read by the operator and a small circle. The palette is a
near-neutral desaturated green-grey (`#f6f8f8` light, `#0e1211` dark) with a
single teal accent, and it is built around **charts**: the token set spends most
of its vocabulary on `--chart-surface`, `--grid`, `--baseline`, an eight-colour
categorical series ramp and a four-step funnel ramp. Design work here is
usually data-display work, so the `dataviz` skill's concerns — legibility of
series colours, axis and legend treatment — matter more than surface polish.

Type is comfortable rather than dense: 16px at 1.62 leading on a system stack,
which suits long-form knowledge-base reading.

Deliberately off-limits:
- Adding a webfont, a CSS framework, or a build step. This is deliberately a
  plain static site that deploys by replacing an HTML file and pushing.
- Editing one page's tokens in isolation. See the warning above — the block is
  duplicated, and a partial edit is how the site drifts.
- Treating it as a public brand surface. It is behind basic auth and contains
  investor and pricing material; it does not need to sell anything.

**Theme reality check:** this product genuinely ships **both** themes, and does
it twice over: `@media (prefers-color-scheme: dark)` follows the OS, and
`:root[data-theme="dark"]` / `:root[data-theme="light"]` allow an explicit
override. Light is the base `:root` declaration. Both themes are real targets
and both must be checked — and any new token needs a value in the base block,
the media query, *and* both `data-theme` blocks, which is four places per token.
<!-- BRAND:END -->

## Surfaces

<!-- SURFACES:BEGIN -- the real pages in the repo; scope one at a time -->
Each standalone page is its own surface and its own scope:

**Top level** — `index.html` (the HQ dashboard and the token source of truth),
`features.html`, `pricing.html`, `pricing-brief.html`, `markets.html`,
`gallery.html`, `act-now.html`, `tina.html`, `paywall-content-spec.html`,
`investors.html`, `investors-full.html`, `investors-simple.html`,
`investor-playbook.html`.

**Sub-directories** — `companies/` (competitor profiles: `amira`, `buddy-ai`,
`curiosities`, `lunii`, `sparkli`), `investors/` (target profiles: `adjacent`,
`goodwater`, `openai-startup-fund`), `docs/` (business model, market/TAM,
growth playbooks, feature specs, investor materials, meeting notes),
`company/`, `gallery/`, `tools/`.

Note the README calls this a *"static single-file knowledge-base site"* — that
description is now out of date, since the repo holds a dozen top-level pages
plus several populated sub-directories. Trust the file listing over the README.

Derived from the repo file listing, so it is a census of *pages*, not of the
components inside them. Confirm scope against the running site.
<!-- SURFACES:END -->

## House-ban conflicts

<!-- BANS:BEGIN -- audited 2026-07-28 against main @ 6ea8eb3 -->
What HeyLolo Business HQ **actually ships** against the five hard bans in
[`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md#house-style--the-hard-bans),
audited at [`6ea8eb3`](https://github.com/Wladefant/heylolo-hq/commit/6ea8eb3a571b47003347b0a508239fe0f3294fd9)
across all 96 standalone HTML pages. This is a record of reality, never edited to make the
site look compliant. For anything you design new,
[the ban wins](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md#when-a-real-token-collides-with-a-hard-ban)
— drop the pattern, keep the tokens above unchanged. **No named brand exceptions exist
here**; this is an internal knowledge base and nothing below is a brand mark.

Remember the no-shared-stylesheet problem above: every violation is copy-pasted into every
page that uses it, so "remove the eyebrows" is a 49-file change, not a one-file change.

**1. Left-edge accent rails — VIOLATED, systemically. The worst offender of the five.**
**250 `border-left` declarations across 94 of 96 files** — it is the house callout idiom,
not incidental decoration: `index.html:181` `.decision{border-left:4px solid var(--warn);
background:var(--warn-wash)}`; `index.html:248` `.placeholder{border-left:4px solid
var(--accent)}`; `act-now.html:71` `border-left:5px solid var(--accent)`; `tina.html:73`
`.say{border-left:4px solid var(--accent)}`. The recurring family is
`.decision` / `.reco` / `.risk` / `.warnbox` / `.angle` / `.say` / `.placeholder`. A
redesigned callout needs a different emphasis device — background wash plus a weighted
label, not a rail. Not a violation: `index.html:229` `.timeline{border-left:2px solid
var(--grid)}` is a structural neutral rule.

**2. Arrows / chevrons — VIOLATED, widely, but read the character of it.** 324 Unicode
arrow glyphs across 55 files plus 317 `&rarr;`/`&larr;` entities across 44. **The majority
are prose connectors inside content** — "Trial→paid", "BITKRAFT→Buddy.ai",
`act-now.html:213` "plan the Dubai&rarr;GmbH flip" — which is writing, not UI chrome. The
genuine UI offenders are narrower: the `→ <a class="issue" …>` link prefix repeated ~34×
on `features.html:155-166`, and the `&larr; Companies` back-links across `companies/` and
`investors/` (e.g. `companies/amira.html:111`). `index.html` itself has exactly one, in a
JS data string (`:2175`). **Zero SVG arrows** — the repo contains no `<svg>` at all.

**3. Gradient clichés / gradient text / generic fonts — gradients CLEAN, font VIOLATED.**
- *Gradient washes*: **ABSENT** — zero `linear-gradient`, `radial-gradient` or
  `conic-gradient` in any HTML file.
- *Gradient text*: **ABSENT** — zero `background-clip:text` / `-webkit-text-fill-color`.
- *Font*: **VIOLATED in both roles.** `index.html:67`
  `font: 16px/1.62 system-ui, -apple-system, "Segoe UI", sans-serif;` — `system-ui` is
  first, and this stack (whitespace variants included) appears ~99×; one variant adds
  Roboto. **No distinctive display face exists** — headings inherit the same system stack,
  so unlike Elumi AI there is no compliant display half. No webfont is loaded anywhere
  (zero `fonts.googleapis.com`, zero `@font-face`). The only alternates are
  `ui-monospace,SFMono-Regular,Menlo,monospace` (91×, for code and figures) and
  `ui-rounded,system-ui,sans-serif` (4×).

**4. Emoji — VIOLATED (147 pictographic emoji across 52 files), but concentrated.**
`features.html:268` defines a workstream legend — `🎨 <strong>Brandy (design)</strong> ·
📱 <strong>Wlad app (FE)</strong> · 🏛 ops/raise.` — and `:272-302` uses those three as row
markers throughout the weekly plan (38 on that page). Heaviest by volume:
`docs/kids-ai-apps-feature-insights-what-users-want.html` and `docs/competitor-analysis.html`.
**`index.html` has zero true emoji** — its symbol-range hits are `&#9733;` star entities and
⚠-class dingbats. So the dashboard itself is clean; the problem lives in `features.html`
and `docs/`.

**5. Eyebrows / overlines — VIOLATED, systemically.** `class="eyebrow"` **77×** across
**49 files**. The rule (`index.html:136-139`, `12px / 700 / letter-spacing .14em /
uppercase`) is copy-pasted into `tina.html:52` and ~48 other pages, and it sits directly
above headings: `index.html:358` `<p class="eyebrow">HeyLolo · ElumiAI · Business HQ ·
updated 5 July 2026</p>` before the `<h1>`, then `:441`, `:457`, `:464`, `:484` before
`<h2>`s.

> **`class="kicker"` here is a NAME COLLISION, not a violation.** `index.html:166`
> `.kicker{font-size:15px;color:var(--ink-2);max-width:70ch;margin-top:0}` is a
> **standfirst/deck paragraph placed *after* the `<h2>`** (`:459`, `:466`, `:486`), 39 uses.
> It is not an overline and the ban's "above a heading" clause is not breached. Do not
> "fix" it — a deck under a heading is good typography. Verify placement before touching
> anything named `kicker` on this site.
<!-- BANS:END -->

## Checks before handing a design to code

- Both themes rendered and screenshotted, per design-prototyping. Both are real here.
- Values come from the token table above, not hard-coded hexes.
- Every page carrying a copy of a changed token has been updated together.
- Verified against https://hq.wladefant.de (basic auth) — there is no staging,
  so a merge to main *is* the production deploy. Check before pushing, not after.
- Approved by the operator before any production page is edited.
