# PolySim design facts ported from the repository copy

Ported verbatim from `.claude/skills/polysim-design/SKILL.md` on `Bavariance/polysimulator`
branch `designstaging` (4,892 bytes, sha256 7c082367f3984f43ee3647214b4895d1bc8f221c1acde072d5f21a8ab02a4150)
so deleting the repository copy loses nothing. It is historical context. Where it conflicts with
`../SKILL.md` (for example the "RICH betting aesthetic" house style versus the hard bans),
`../SKILL.md` wins. Colour and font values here were superseded by `polysim-tokens.md`.

# PolySim design specifics

Use this **with** the universal `design-prototyping` skill — that skill is the *workflow*; this
one is the PolySimulator *facts*. PolySim is a Polymarket-style prediction-market paper-trading
simulator (outcomes settle $1/share; Yes/No pricing in cents).

## House style

**RICH, dark betting/trading aesthetic** — the `trade-panel`, `crypto-updown`, and `leaderboard`
prototypes are the gold standard: vibrant, premium, dense, unmistakably a real-money-style product
(though it's paper). The operator REJECTED a flat / minimal / editorial "Wikipedia" direction.
Obey the four hard bans (see design-prototyping `references/forbidden-ai-tells.md`); when unsure,
look **richer and more "betting product," not flatter**.

## Real tokens

See `references/polysim-tokens.md` (verified against `frontend/tailwind.config.ts` + `globals.css`).
Dark: bg `#050912`, panels `#0b111b` / `#111a2a` / `#1a2438`; **green `#2dd6a0`** (up/buy/yes/profit),
**red `#ff6178`** (down/sell/no/loss), blue `#4f8dff` (info/chart), amber `#f5c769` (warn/cooldown);
text `#eef2f8`, muted `#9ba6bf`; radii `rounded-panel 20px` / `rounded-tile 14px`; shadow
`0 24px 45px rgba(5,9,18,.65)`. Fonts: **DM Sans** (UI) + **JetBrains Mono** (numbers/short labels
only, `tnum`). Dark scrollbar thumb `rgba(60,70,90,.85)`.

## Where prototypes live

`docs/design/` on branch **`designstaging`** — gallery `index.html`, `<surface>-redesign.html`,
`<surface>-versions.html` (compare vs production), `<surface>-current.html` (auth-gated recreations),
`screenshots/`. ~84 prototypes covering every route in `frontend/app/`.

## Production baseline (for versions pages)

The real app runs on **`staging.polysimulator.com`** — framing is ALLOWED, so **embed the live UI**
for PUBLIC pages (`<iframe src="https://staging.polysimulator.com/<route>">`). The prod domain
`polysimulator.com` sends `X-Frame-Options: DENY` — do NOT embed it. For AUTH-GATED pages a live
embed only shows the login, so **recreate** the current UI as `<surface>-current.html`. ALWAYS also
save a **frozen screenshot** under `screenshots/` so there's a record if staging drifts.

## claude.ai "Meridian" design (the operator's exploration)

DesignSync project **"PolySimulator UI Redesign"**, projectId
**`839c69cd-f08b-4ec8-b57c-c5f944e96e7b`** (owner Erik). Read it via `DesignSync get_file`. Meridian
is a cleaner **TradingView/Alpaca** register (Geist + Geist Mono, single emerald accent `#2dd17a`,
hairline borders, "rule of one green"). **The operator KEEPS our rich house style but folds in V4's
strong TradingView elements:** a thin live **ticker rail**, a framed **browser-chrome app dashboard**
showcase, a **biggest-movers list**, a **developer/API section** (hero stats + `curl` block, BTC
5-minute Up/Down as the hook), and a **3-way audience router** (Traders→app, Developers→docs,
Investors→/company). Source files: `ps-tokens.css`, `ps-shared.jsx`, `ps-ds.jsx`, `ps-home-v1..v4.jsx`
(V4 = recommended/TradingView; V2 = subdomain split). The chosen homepage direction is **V4**.

## The product split (in progress)

Plan: a clean PUBLIC root site `polysimulator.com` (marketing homepage + `/company` + links into
developer docs) and the trading APP at `app.polysimulator.com`. Full route inventory, domain
architecture, homepage spec, and migration sequence: **`docs/triage/homepage-app-split-plan.md`**.

## Docs sites

- `docs-site/` = **Mintlify** developer docs — redesign = theming via `docs.json` (palm theme,
  `appearance: dark/strict`, Geist font, `custom.css`); high customization is on the free Starter
  plan. Limits + plan: **`docs/triage/mintlify-redesign-feasibility.md`**.
- `docs-site-b2b/` = **Astro** (B2B) — being migrated to Mintlify (≈8–10h; keep it a **separate**
  Mintlify project since B2B is private and public docs are open).

## Hosting prototypes for cofounders

Host the `docs/design` gallery at **`polysimdesign.wladefant.de`** (a separate Dokploy static app —
Dokploy already fronts `*.wladefant.de`) so cofounders can view (and, later, comment — a "simple
Figma"). Keep the gallery clean and searchable.
