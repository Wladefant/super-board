# PolySim real design tokens

Ported from the repository copy `.claude/skills/polysim-design/references/polysim-tokens.md`
on `Bavariance/polysimulator` branch `designstaging` (3,094 bytes), then reconciled against
live `frontend/tailwind.config.ts` and `frontend/app/globals.css` on `staging`
(reconciled 2026-10-03). Both files drift: re-confirm before relying on a value.

## Colors (dark theme — the live app)
| Token | Value | Use |
|---|---|---|
| `midnight` | `#050912` | App background (also `:root` background-color) |
| `panel-base` | `#0b111b` | Card / panel base |
| `panel-raised` | `#111a2a` | Elevated surfaces |
| `panel-muted` | `#1a2438` | Tertiary surfaces |
| `muted` | `#9ba6bf` | Secondary text |
| `accent.green` | `#2dd6a0` | UP / Buy / Yes / profit / positive |
| `accent.red` | `#ff6178` | DOWN / Sell / No / loss / negative |
| `accent.blue` | `#4f8dff` | Informational / chart |
| `accent.amber` | `#f5c769` | Warnings / cooldown / in-progress |

Borders are typically `rgba(255,255,255,.06–.10)` (`--color-border` = `rgba(255,255,255,.06)`).
Default text is `#f4f7ff` (`:root` color in `globals.css`).

`tailwind.config.ts` also defines `sports.live|scheduled|futures` and `status.*` palettes for
the sports / World Cup surfaces. Read the file for those; they are not repeated here.

### CSS custom properties in `globals.css` `:root`
These exist alongside the Tailwind tokens and have different values. Do not mix them up.
- `--color-bg #020512`, `--color-surface-1 #0b1120`, `--color-surface-3 #161f2f`,
  `--color-brand #00744e`, `--color-brand-accent #39dcb2`,
  `--color-text-primary #dcdbd8`, `--color-text-muted #9daab8`.
- Spacing `--space-1..8` = 4, 8, 12, 16, 24, 32, 48, 64 px.
- Radii `--radius-sm 6px`, `--radius-md 8px`, `--radius-lg 14px`, `--radius-xl 16px`.
- Motion: `--ease-out cubic-bezier(0.23,1,0.32,1)`, `--ease-in-out cubic-bezier(0.77,0,0.175,1)`,
  `--ease-drawer cubic-bezier(0.32,0.72,0,1)`; durations 150 / 200 / 300 ms.

## Radii & shadow (Tailwind)
- `rounded-panel` = `20px` (cards, modals)
- `rounded-tile` = `14px` (buttons, compact elements)
- `shadow-panel` = `0 24px 45px rgba(5,9,18,.65)`

## Typography
- Body/UI font: **DM Sans** (var `--font-sans`).
- `font-display` = `var(--font-display)` = **Clash Display**, falling back to DM Sans until the
  self-hosted font files land. (The older copy said "no display font today"; that is stale.)
- `font-mono` = `var(--font-mono)` = **Geist Mono**, then system monospace.
- House-style addition for prototypes: a **tabular monospace** for numbers with
  `font-feature-settings:"tnum" 1`. The SKILL names JetBrains Mono for prototypes; the live app's
  `font-mono` is Geist Mono.
- Slim dark scrollbar thumb: `rgba(60,70,90,.8)` (`.orderbook-scrollbar`).

## Formatting conventions (`frontend/lib/formatCurrency.ts` etc.)
- Currency: `≥$1M → $X.XM`, `≥$1K → $X.XK`, else `$X.XX`; null → `—`.
- Prices/probability as cents: `(price*100).toFixed(0|1) + '¢'` (e.g. `0.52 → 52¢`).
- P&L color: positive `accent.green`, negative `accent.red`, neutral `muted`/`amber`.
- P&L format: `+$12.50` / `-$3.25`, `+5.2%` / `-3.1%`.
- Direction (up/down/flat): convey with **color + signed numerals ONLY** (green `+4.2%` / red `-3.1%` / muted `0.0%`). **NEVER use arrow glyphs** (`↑ ↓ ▲ ▼ ↗ ↘`) — hard ban (operator: "remove those arrows forever and always"). See DESIGN-SYSTEM.md §7.

## Suggested white/light theme (starting point — refine per surface)
Not `invert()`. A deliberate light palette that keeps the same accents:
| Role | Suggested value |
|---|---|
| bg | `#f6f7f9` (warm near-white) |
| surface / card | `#ffffff` |
| surface raised | `#ffffff` with `box-shadow: 0 1px 2px rgba(16,24,40,.06), 0 8px 24px rgba(16,24,40,.06)` |
| border | `rgba(16,24,40,.10)` |
| text primary | `#0b1220` |
| text muted | `#5b6577` |
| accent green | `#0fae7e` (slightly darker for AA contrast on white) |
| accent red | `#e23d57` |
| accent blue | `#2f6fe6` |
| accent amber | `#c98a16` |
In light mode replace neon glows with soft layered shadows; keep motion but lighten it.

## App facts worth knowing
- Stack: Next.js 14 + React 18, Tailwind v3.4, custom components (no shadcn/Radix), react-icons.
- Market price chart is **custom SVG** (not a chart lib); Recharts is used only for profile/portfolio.
- Trade panel: `frontend/components/market/TradePanel.tsx`. Market detail:
  `frontend/app/markets/[id]/MarketDetailClient.tsx`.
- It's a paper-trading Polymarket-style sim — outcomes settle at $1/share; "Yes/No" pricing in cents.
