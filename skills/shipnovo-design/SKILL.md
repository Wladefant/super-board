---
name: shipnovo-design
description: "Shipnovo's own design context for the design-prototyping skill: real design tokens, brand direction, staging and production URLs, and the design-system source of truth. Use whenever design-prototyping runs on Shipnovo, or when any Shipnovo UI surface is designed, restyled, or reviewed."
---

# Shipnovo design context

This is the per-project half of [`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md).
That skill owns the *method* -- Agent Native Design first, dark AND light, real
Mobbin references, no production edit before approval. This file owns the
*facts* about Shipnovo that the method needs. Read both; never restate the
method here.

Surface routing is the [Agent Native operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md).
Work lives on the board: NOT YET PROVISIONED -- Shipnovo has no Superboard. Provision it first (superboard-setup Step 1), then replace this line.

## Where the truth lives

| Thing | Source of truth |
|---|---|
| Repo | https://github.com/Wladefant/shipnovo |
| Design tokens | `src/app/globals.css` in that repo -- read it live, do not trust the table below if it disagrees |
| Production | NOT VERIFIED -- no deployment URL exists anywhere in the repo (README is stock create-next-app). Ask the operator before citing one. |
| Staging | NOT VERIFIED -- no staging URL in the repo. |
| Shared toolkit | [Wladefant/design-kit](https://github.com/Wladefant/design-kit) -- prototype linter, gallery + pin-comment overlay, DTCG token schema |

**The token table below is a snapshot, not the source.** Re-read `src/app/globals.css`
before designing; when it has drifted, fix this file in the same session rather
than designing against stale values.

## Design tokens

<!-- TOKENS:BEGIN -- harvested 2026-07-28 from src/app/globals.css @ 9d199a9 -->
Harvested from [`src/app/globals.css`](https://github.com/Wladefant/shipnovo/blob/main/src/app/globals.css).
Fonts come from [`src/app/layout.tsx`](https://github.com/Wladefant/shipnovo/blob/main/src/app/layout.tsx), not from the CSS.

Shipnovo uses the **shadcn token vocabulary** (so shadcn primitives work
unmodified) plus a small set of brand extensions. Tailwind v4 `@theme inline`
re-exports every variable as `--color-*`; `.dark` is toggled on `<html>` by
next-themes via `@custom-variant dark (&:is(.dark *))`.

**Colour — light (`:root`) / dark (`.dark`)**

| Token | Light | Dark | Use |
|---|---|---|---|
| `--background` / `--foreground` | `#f8fafc` / `#0f172a` | `#0b1120` / `#e6ebf2` | page ground + body text |
| `--card` / `--card-foreground` | `#ffffff` / `#0f172a` | `#111827` / `#e6ebf2` | cards |
| `--popover` / `--popover-foreground` | `#ffffff` / `#0f172a` | `#1a2233` / `#e6ebf2` | popovers |
| `--surface-raised` | `#ffffff` | `#1a2233` | raised surface |
| `--surface-overlay` | `#ffffff` | `#232c3d` | overlay surface |
| `--primary` / `--primary-hover` | `#1e3a8a` / `#1e40af` | `#3b82f6` / `#2563eb` | deep navy; brightened on dark to clear AA |
| `--primary-foreground` | `#ffffff` | `#0b1120` | text on primary |
| `--secondary` / `--secondary-foreground` | `#edf1f7` / `#1e293b` | `#1a2233` / `#e6ebf2` | secondary |
| `--muted` / `--muted-foreground` | `#f1f5f9` / `#475569` | `#161e2e` / `#9aa7bc` | muted |
| `--accent` / `--accent-foreground` | `#eef2f8` / `#0f172a` | `#1a2233` / `#e6ebf2` | shadcn hover surface — **neutral, not the brand** |
| `--brand` / `--brand-hover` | `#0d9488` / `#0f766e` | `#2dd4bf` / `#5eead4` | teal, the rationed brand accent |
| `--brand-foreground` | `#ffffff` | `#0b1120` | text on brand |
| `--success` | `#16a34a` | `#34d399` | success |
| `--warning` | `#d97706` | `#fbbf24` | amber — badges only |
| `--destructive` | `#dc2626` | `#f87171` | destructive |
| `--fg-subtle` | `#64748b` | `#6b7892` | subtle text |
| `--border` / `--border-strong` | `#e2e8f0` / `#cbd5e1` | `rgba(255,255,255,0.08)` / `rgba(255,255,255,0.14)` | borders |
| `--input` | `#e2e8f0` | `rgba(255,255,255,0.12)` | input borders |
| `--ring` | `#2563eb` | `#60a5fa` | focus ring |

A parallel `--sidebar-*` set exists in both modes (`--sidebar`, `-foreground`,
`-primary`, `-primary-foreground`, `-accent`, `-accent-foreground`, `-border`,
`-ring`) — light `#ffffff`/`#334155`, dark `#111827`/`#9aa7bc`. Read the file
for the full set rather than reproducing it from memory.

**Type** — `Inter` via `--font-inter` and `Geist Mono` via `--font-geist-mono`,
both loaded with `next/font/google` (`display: "swap"`, latin subset) in
`layout.tsx`; `@theme inline` maps them to `--font-sans` / `--font-mono` with
`ui-sans-serif, system-ui, sans-serif` and `ui-monospace, monospace` fallbacks.
`body` is **14px / line-height 1.45** — deliberately dense for a data tool.
`font-feature-settings: "tnum" 1, "zero" 1` is forced on `table`,
`.tabular-nums` and `[data-slot="badge"]` so money, tracking and pack numbers
lock to fixed width.

**Radius** — `--radius: 0.5rem`, with `--radius-sm` `calc(radius - 4px)` ·
`md` `calc(radius - 2px)` · `lg` `radius` · `xl` `calc(radius + 4px)`. The file
annotates this as `sm 4 · md 6 · lg 8 · xl 12 px`.

**Shadow** — `--shadow-sm` `0 1px 2px rgba(15,23,42,0.06)` · `--shadow-md`
`0 2px 4px rgba(15,23,42,0.06), 0 4px 8px rgba(15,23,42,0.04)` · `--shadow-lg`
`0 8px 24px rgba(15,23,42,0.1)` · `--shadow-overlay`
`0 16px 48px rgba(15,23,42,0.18)`.

⚠ **The shadow tokens are declared only in `:root` and are never redefined in
`.dark`.** Dark mode therefore inherits shadows tuned on light-mode navy
(`rgba(15,23,42,…)`), which read as nearly invisible on `#0b1120`. This is a
real gap in the token set, not a value to copy blindly — if a dark surface needs
elevation, raise it and say so rather than reaching for `--shadow-*`.

**Spacing** — there is no bespoke spacing scale; Tailwind's default spacing is
used as-is. Do not invent one.
<!-- TOKENS:END -->

## Brand direction

<!-- BRAND:BEGIN -- observed 2026-07-28 from the real stylesheet and layout -->
The stylesheet states its own thesis: a *"calm navy/teal instrument panel"*.
Shipnovo is a working tool for eBay sellers — orders in, labels out — and the
visual language is built for people looking at it all day, not for a landing
page. Light mode is a cool slate ground (`#f8fafc`) with white cards; dark mode
is navy-tinted near-black (`#0b1120`). Deep navy carries primary actions and
teal is **rationed** as the brand accent — note that `--accent` is deliberately
a neutral hover surface, *not* the brand colour, so shadcn primitives never
accidentally paint themselves teal.

Density is a design decision: 14px body, 1.45 leading, tabular figures forced on
every table, badge and numeric column. Nothing bounces. There is a global
`prefers-reduced-motion` kill switch that clamps all animation and transition
durations to `0.01ms` — respect it.

Deliberately off-limits, stated in the source itself:
- **Never pure `#000` / `#fff`** as fg/bg — the file says so explicitly.
- **Amber is badges only, never primary** — the comment reads *"no DHL-yellow"*.
  Shipnovo prints DHL and Deutsche Post labels; looking like the carrier is a
  brand mistake, not a shortcut.
- Every fg/bg pair is claimed to clear **WCAG AA in both modes**. That is the
  bar any new pair has to meet.
- Hard-coded hexes where a token exists.

**Theme reality check:** Shipnovo genuinely ships **both** themes. `layout.tsx`
mounts `ThemeProvider` with `attribute="class"`, `defaultTheme="system"`,
`enableSystem` and `disableTransitionOnChange`, and `.dark` carries a complete
parallel token set. So unlike PolySimulator, a light *and* a dark design here
are both real targets, and both must be checked — with the shadow caveat above.
The app is German-language (`<html lang="de">`, German metadata); design copy
should be German unless the operator says otherwise.
<!-- BRAND:END -->

## Surfaces

<!-- SURFACES:BEGIN -- from the real App Router tree; scope one at a time -->
Route groups in [`src/app`](https://github.com/Wladefant/shipnovo/tree/main/src/app),
each a valid scope for one design task:

**`(app)` — the signed-in tool** (shares `(app)/layout.tsx`, the sidebar shell)
- `dashboard` — the landing surface after sign-in
- `orders` — order list and detail
- `import` — eBay order import
- `shipping-runs` — the batch that produces labels
- `templates` — label/packing templates
- `billing`
- `settings`
- `support`
- `admin`

**`(auth)`** — sign-in / registration surfaces.

**`(marketing)`** — public site: landing `page.tsx`, `pricing`, `legal`.

**`packing-lists`** — sits outside all three groups, i.e. it has no shared
shell. This is the printed packing list, and print is its own design problem
(paper, no dark mode, identical ordering to the labels). Treat it as a distinct
surface, not as another app page.

Derived from the route tree, so it is a floor, not a census — components and
modals inside these routes are not listed. Confirm scope against the running app.
<!-- SURFACES:END -->

## Checks before handing a design to code

- Both themes rendered and screenshotted, per design-prototyping. Both are real here.
- Values come from the token table above, not hard-coded hexes.
- No verified staging or production URL exists yet — verify against a local
  `npm run dev` build and say so, rather than claiming a deployed check.
- Approved by the operator before any production component is edited.
