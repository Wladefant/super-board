# Prototype template & patterns

> ⛔ **SUPERSEDED VISUALS (2026-06-20).** Use this file ONLY for the single-file skeleton and
> the vanilla-JS wiring. Its visual motifs below — **side-aware accent recolor, atmospheric
> radial-mesh background, tabular-mono on all numbers, top glow rail, neon shadows** — are now
> **FORBIDDEN**. Read `forbidden-ai-tells.md` and follow the corrected house style in SKILL.md.
> Ignore every "patterns that landed well" item here that conflicts with the bans.

The proven structure for a PolySim design prototype. One self-contained `.html` file,
inline `<style>` + vanilla `<script>`, **dark and white themes side by side**, fully
interactive. The first approved prototype (`docs/design/trade-panel-redesign.html`) is the
canonical worked example — read it for a full reference.

## File skeleton
```html
<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>PolySim · <Surface> redesign</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  /* :root = DARK tokens; .light scope overrides them for the white theme */
  :root{ --midnight:#050912; --panel-base:#0b111b; --panel-raised:#111a2a;
         --muted:#9ba6bf; --green:#2dd6a0; --red:#ff6178; --blue:#4f8dff; --amber:#f5c769;
         --line:rgba(255,255,255,.08); --r-panel:20px; --r-tile:14px;
         --shadow-panel:0 24px 45px rgba(5,9,18,.65);
         --side:var(--green); --side-rgb:45,214,160; }   /* side-aware accent */
  .light{ --midnight:#f6f7f9; --panel-base:#ffffff; --panel-raised:#ffffff;
          --muted:#5b6577; --green:#0fae7e; --red:#e23d57; --blue:#2f6fe6; --amber:#c98a16;
          --line:rgba(16,24,40,.10); --shadow-panel:0 1px 2px rgba(16,24,40,.06),0 12px 30px rgba(16,24,40,.08);
          --side:var(--green); --side-rgb:15,174,126; color:#0b1220; }
  .mono{ font-family:"JetBrains Mono",monospace; font-feature-settings:"tnum" 1; }
  /* ...component styles, all referencing the vars so both themes share one stylesheet... */
</style></head>
<body>
  <!-- two columns: dark stage + light stage, so the operator sees both at once -->
  <main style="display:flex;gap:40px;flex-wrap:wrap;justify-content:center;padding:48px">
    <section class="dark">  <!-- DARK theme instance --> </section>
    <section class="light" style="background:var(--midnight);border-radius:24px;padding:24px"> <!-- LIGHT instance --> </section>
  </main>
  <script> /* one render() function wired to both instances, or instantiate per-section */ </script>
</body></html>
```
Two ways to show both themes:
- **Side-by-side instances** (preferred for a single component): render the component twice,
  the second wrapped in `.light`. Each gets its own state or they share one.
- **Theme toggle** that flips a class on `<body>` — use when the surface is page-sized and
  two columns won't fit. Default it to showing the dark, with the toggle obvious.

## Patterns that landed well
- **Side-aware accent.** Keep `--side`/`--side-rgb` CSS vars; JS sets them from the active
  choice (Yes→green, No→red, Sell→red). Wire the top glow rail, selected tile, the hero
  number, and the primary CTA to `var(--side)` so the whole panel shifts together. This is
  the single highest-impact "feels alive" move.
- **Tabular-mono numerals.** All prices, shares, balances, P&L use the `.mono` class. Money
  in DM Sans looks consumer-y; mono looks like a real exchange.
- **One emotional anchor.** Make the number the user cares about (payout / "To win" / total
  P&L) the largest figure in the summary, colored with `var(--side)`.
- **Atmosphere, not flat fill.** Dark: layered radial gradients tinted to `--side-rgb` + a
  faint masked grid + subtle grain. Light: drop the glows, use soft layered shadows.
- **Modal state machine** for the primary action where it matches the real flow:
  `confirm view → executing (spinner) → success receipt (new balance)`. Blur the scrim.
  Success state should go **fully green** regardless of side (success is always positive) —
  a known fix from the first prototype where the receipt kept the red No-accent.
- **Staggered load + hover lift.** `animation-delay` reveal on first paint; `translateY(-2px)`
  on interactive tiles. High-impact moments over scattered micro-interactions.

## Interaction wiring (vanilla JS)
- Hold a small `state` object; a single `render()` recomputes derived numbers and repaints.
- Recompute from first principles each render (shares = $ / price; payout = shares × $1;
  profit = payout − cost) so the prototype is believable, not hard-coded.
- Delegate clicks on container elements; toggle an `.on` class for selected segment/tile/tab.

## Serving & testing (Claude-in-Chrome)
- `file://` is mangled by the `navigate` tool. Serve: `python -m http.server 8765 --bind 127.0.0.1`
  from `docs/design/` (run_in_background), then navigate to `http://127.0.0.1:8765/<file>.html`.
- Screenshot: default, alternate selection (verify recolor), modal, success — **and the light
  theme**. Retry a screenshot once if CDP times out. Stop the server when done if the user is finished.
