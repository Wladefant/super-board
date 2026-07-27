---
name: fnsku-design
description: "FNSKU Warehouse Scanner's own design context for the design-prototyping skill: real design tokens, brand direction, staging and production URLs, and the design-system source of truth. Use whenever design-prototyping runs on FNSKU Warehouse Scanner, or when any FNSKU Warehouse Scanner UI surface is designed, restyled, or reviewed."
---

# FNSKU Warehouse Scanner design context

This is the per-project half of [`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md).
That skill owns the *method* -- Agent Native Design first, dark AND light, real
Mobbin references, no production edit before approval. This file owns the
*facts* about FNSKU Warehouse Scanner that the method needs. Read both; never restate the
method here.

Surface routing is the [Agent Native operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md).
Work lives on the board: NOT YET PROVISIONED -- FNSKU Warehouse Scanner has no Superboard. Provision it first (superboard-setup Step 1), then replace this line.

## Where the truth lives

| Thing | Source of truth |
|---|---|
| Repo | https://github.com/Wladefant/FNSKUWarehouseScanner |
| Design tokens | `client/src/index.css` in that repo -- read it live, do not trust the table below if it disagrees |
| Production | NOT VERIFIED -- no deployment URL in the repo. It ships a Dockerfile and a .replit; ask the operator where it actually runs. |
| Staging | NOT VERIFIED -- no staging URL in the repo. |
| Shared toolkit | [Wladefant/design-kit](https://github.com/Wladefant/design-kit) -- prototype linter, gallery + pin-comment overlay, DTCG token schema |

**The token table below is a snapshot, not the source.** Re-read `client/src/index.css`
before designing; when it has drifted, fix this file in the same session rather
than designing against stale values.

Two files together define the system: `client/src/index.css` holds the CSS
custom properties, and
[`tailwind.config.ts`](https://github.com/Wladefant/FNSKUWarehouseScanner/blob/main/tailwind.config.ts)
maps them onto Tailwind's scale. Read both — the radius scale in particular
lives *only* in the Tailwind config.

## Design tokens

<!-- TOKENS:BEGIN -- harvested 2026-07-28 from client/src/index.css and tailwind.config.ts @ 8c53661 -->
Harvested from [`client/src/index.css`](https://github.com/Wladefant/FNSKUWarehouseScanner/blob/main/client/src/index.css)
and [`tailwind.config.ts`](https://github.com/Wladefant/FNSKUWarehouseScanner/blob/main/tailwind.config.ts).

**Colours are stored as bare HSL triplets, not hex.** Every value below is
consumed as `hsl(var(--token) / <alpha-value>)`, which is what makes Tailwind
opacity modifiers work. Writing a hex into this system breaks that — keep the
triplet form.

| Token | Light (`:root`) | Dark (`.dark`) | Use |
|---|---|---|---|
| `--background` / `--foreground` | `0 0% 100%` / `0 0% 6%` | `210 5% 8%` / `0 0% 98%` | page ground + text |
| `--card` / `--card-foreground` | `0 0% 98%` / `0 0% 6%` | `210 5% 10%` / `0 0% 98%` | cards |
| `--card-border` | `0 0% 94%` | `210 5% 13%` | card border |
| `--popover` / `--popover-foreground` | `0 0% 94%` / `0 0% 10%` | `210 5% 14%` / `0 0% 95%` | popovers |
| `--popover-border` | `0 0% 90%` | `210 5% 17%` | popover border |
| `--primary` / `--primary-foreground` | `36 100% 50%` / `0 0% 100%` | `36 100% 50%` / `0 0% 100%` | **amber — identical in both themes** |
| `--secondary` / `--secondary-foreground` | `210 18% 20%` / `0 0% 100%` | `210 18% 20%` / `0 0% 100%` | **also identical in both themes** |
| `--muted` / `--muted-foreground` | `0 0% 92%` / `0 0% 35%` | `210 5% 16%` / `0 0% 65%` | muted |
| `--accent` / `--accent-foreground` | `0 0% 94.5%` / `0 0% 6%` | `210 5% 16%` / `0 0% 95%` | neutral hover surface |
| `--success` | `160 84% 39%` | `160 84% 45%` | success |
| `--destructive` / `--destructive-foreground` | `0 84% 45%` / `0 0% 98%` | `0 84% 35%` / `0 0% 98%` | destructive |
| `--border` | `0 0% 89%` | `210 5% 18%` | borders |
| `--input` | `0 0% 75%` | `210 5% 30%` | input border |
| `--ring` | `36 100% 50%` | `36 100% 50%` | focus ring (amber, both) |

**Sidebar set** — `--sidebar` `0 0% 96%` / `210 5% 12%`; `--sidebar-foreground`
`0 0% 10%` / `0 0% 95%`; `--sidebar-border` `0 0% 92%` / `210 5% 15%`;
`--sidebar-accent` `0 0% 92%` / `210 5% 16%`; `--sidebar-primary` and
`--sidebar-ring` are `36 100% 50%` in both themes.

**Charts** — `--chart-1..5`, light: `36 100% 35%` · `210 100% 25%` ·
`160 84% 25%` · `270 60% 35%` · `30 80% 30%`; dark: `36 100% 60%` ·
`210 100% 70%` · `160 84% 65%` · `270 60% 70%` · `30 80% 65%`. Light uses dark
saturated chart colours, dark uses light ones — correctly inverted.

**Status dots** live in `tailwind.config.ts` as literal RGB, not as CSS
variables: `status.online rgb(34 197 94)` · `status.away rgb(245 158 11)` ·
`status.busy rgb(239 68 68)` · `status.offline rgb(156 163 175)`.

**Type — no typographic choice has been made.** `--font-sans` is
`Arial, Helvetica, sans-serif`, `--font-serif` is `Georgia, serif`,
`--font-mono` is `Menlo, monospace`. There is no webfont and no custom scale.
Record this as the truth rather than an aesthetic: if a design proposes a
typeface, it is proposing a *change*, and should say so.

**Radius** — `--radius: .5rem` is declared in the CSS, but the Tailwind scale
does **not** derive from it. `tailwind.config.ts` hardcodes
`lg: .5625rem (9px)` · `md: .375rem (6px)` · `sm: .1875rem (3px)`. Use those
three; treat `--radius` as vestigial until someone reconciles them.

**Shadow — effectively disabled.** The full `--shadow-2xs … --shadow-2xl` ramp
is declared in both themes, and **every layer is fully transparent**
(`hsl(0 0% 0% / 0.00)`). Nothing casts a shadow. Do not "read the shadow token"
and assume depth exists.

**Elevation is done with overlay tints instead.** `--elevate-1`
`rgba(0,0,0,.03)` / `rgba(255,255,255,.04)` and `--elevate-2` `rgba(0,0,0,.08)`
/ `rgba(255,255,255,.09)` are painted by the `.hover-elevate`,
`.active-elevate`, `.toggle-elevate` (+`-2`) utilities as `::after`/`::before`
layers that overlap the parent's border. `--button-outline` and
`--badge-outline` (`rgba(0/255…,.10)` and `.05`) and
`--opaque-button-border-intensity` (`-8` light / `9` dark, fed through
`hsl(from … calc(l + …))`) auto-derive button borders from their fill. Escape
hatches exist: `.no-default-hover-elevate` / `.no-default-active-elevate`. The
file's own comment notes these utilities **do not work on elements with
`overflow: hidden`** — a real constraint when designing new components.

**Spacing** — `--spacing: 0.25rem` is declared; otherwise Tailwind's default
scale is used. `--tracking-normal: 0em`.
<!-- TOKENS:END -->

## Brand direction

<!-- BRAND:BEGIN -- observed 2026-07-28 from the real stylesheet and config -->
An internal warehouse and eBay-operations tool — barcode scanning, order
matching, packing, inventory mapping. It reads as an unbranded shadcn/Radix
admin surface with one decision made: **amber `36 100% 50%` as primary**, held
identical across light and dark, and reused for the focus ring and the whole
sidebar-primary set. Secondary is a single desaturated slate (`210 18% 20%`),
also shared across themes. Everything else is neutral grey in light and a
blue-tinted near-black (`210 5% 8%`) in dark.

The defining structural choice is that **depth is expressed as tint, not
shadow**. The shadow ramp exists but is entirely transparent; elevation comes
from the `hover-elevate` / `active-elevate` / `toggle-elevate` overlay system,
which is contrast-aware in both themes and deliberately compounds (toggled +
hovered stack). Design new components inside that system rather than
reintroducing box-shadows — and remember overlay elevation breaks under
`overflow: hidden`.

Honest assessment: this is a **functional, not an art-directed, product**. The
type stack is Arial/Georgia/Menlo, the radius scale in the config contradicts
the `--radius` variable, and the shadow tokens are inert placeholders. Those are
loose threads, not a style. A redesign here has a lot of latitude — but it
should be proposed openly as introducing an identity, never smuggled in as
"following the tokens".

Deliberately off-limits:
- Hex colours in the token layer. The HSL-triplet form is load-bearing for
  Tailwind's `<alpha-value>` opacity modifiers.
- Re-enabling `box-shadow` on components that should use the elevate utilities.
- Assuming amber means "warning". Here it is **primary**; `--destructive` and
  `--success` carry meaning.

**Theme reality check:** the product genuinely ships **both** themes.
`tailwind.config.ts` sets `darkMode: ["class"]` and `.dark` carries a complete
parallel token set including inverted chart colours. Both are real design
targets. Note there is no theme-provider evidence in the token files themselves —
confirm in the client how `.dark` actually gets toggled before promising a
toggle in a design.
<!-- BRAND:END -->

## Surfaces

<!-- SURFACES:BEGIN -- the real page components; scope one at a time -->
Pages in
[`client/src/pages`](https://github.com/Wladefant/FNSKUWarehouseScanner/tree/main/client/src/pages),
each a valid scope for one design task. Rough size is a useful proxy for how
much surface area a redesign touches:

- `Scanner.tsx` (~64 KB) — the barcode scanning surface, the core of the product
- `WarehouseMap.tsx` (~59 KB) — inventory/location map
- `ItemizedReport.tsx` (~52 KB) and `Dashboard.tsx` (~51 KB)
- `Analytics.tsx` (~44 KB), `MatchQueue.tsx` (~40 KB)
- `Sellers.tsx`, `EbayTools.tsx`, `Admin.tsx`, `Orders.tsx`,
  `PurchaseOrders.tsx`, `Login.tsx`, `OrderEnrichment.tsx`, `Packing.tsx`,
  `Messages.tsx`, `ListingWorkshop.tsx`, `KnowledgeBase.tsx`, `Telegram.tsx`,
  `Pipeline.tsx`, `BusinessReport.tsx`, `Shipping.tsx` (~3 KB, likely a stub),
  `not-found.tsx`

Several of these are very large single files; expect a design task scoped to one
"page" to actually span many sub-surfaces. Narrow the scope further before
starting.

Also in the repo root: `Lager.html` and `Lager1.html`, standalone HTML files
outside the React client. They are **not** part of this token system and do not
share these tokens — do not treat them as app surfaces without checking with the
operator what they still serve.

Derived from the page file listing, so it is a floor, not a census. Confirm
scope against the running app.
<!-- SURFACES:END -->

## Checks before handing a design to code

- Both themes rendered and screenshotted, per design-prototyping. Both are real here.
- Colour values stay in bare-HSL-triplet form, not hex.
- Depth uses the elevate utilities, not `box-shadow`.
- No verified deployment URL exists yet — verify against a local run and say so,
  rather than claiming a deployed check.
- Approved by the operator before any production component is edited.
