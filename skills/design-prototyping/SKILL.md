---
name: design-prototyping
description: >-
  Use BEFORE writing or changing any production UI on ANY project — whenever the user
  wants to redesign, restyle, build, or rethink a component, page, panel, card, modal,
  or any visual surface. Triggers on "redesign", "new design", "make it look better",
  "build a component", "we need a redesign", "fresh look", "modernize the UI", or naming
  a specific surface (trade panel, market card, leaderboard, portfolio, order ticket,
  etc.). Runs design in the hosted Agent Native Design app — persistent screen boards,
  side-by-side variant exploration, human pick, snapshot-then-edit iteration, and coding
  handoff — with a standalone self-contained HTML prototype as the offline fallback.
  ALWAYS in BOTH a dark and a light theme, grounded in real Mobbin references and the
  app's real design tokens. ALWAYS design first; never edit the production component
  until the user approves the look. Project-specific tokens, brand, and URLs live in that
  project's own design skill (e.g. polysim-design). Work one surface at a time.
---

# Design (universal workflow)

The operator loves design and motion, reviews work like a hawk, and wants to **see and
click** a redesign before any production code changes. The rule never changes:
**design → review → (only on approval) port.**

What changed: the design surface is now the **Agent Native Design app**, not a pile of
one-off HTML files. Design gives persistent screen boards, real side-by-side variant
exploration with a human pick, snapshot-then-edit iteration, and a coding handoff bundle.
The standalone HTML prototype remains, demoted to the **offline / no-host fallback**.

---

## ENDPOINT — the one place to edit

<!-- ============================================================================
     DESIGN APP ENDPOINT — the ONLY place a URL is configured. Edit here, nowhere else.
     ========================================================================= -->

| Field | Value |
|---|---|
| **Self-hosted app URL** | `PENDING` — a `*.wladefant.de` subdomain is being deployed; fill in when live |
| **Self-hosted MCP URL** | `PENDING` — normally `<app URL>/mcp` |
| **Upstream hosted fallback** | `https://design.agent-native.com` · MCP `https://design.agent-native.com/mcp` |
| **Local dev** | `http://127.0.0.1:8099` (`pnpm install && pnpm dev` in the design template) |
| **MCP server name** | `agent-native-design` |
| **Auth** | OAuth. In Claude Code: `/mcp` → Authenticate/Reconnect the Design connector |

<!-- ========================================================================= -->

Until the self-hosted URL is filled in, **treat it as pending**: use the upstream hosted
app if the connector is already authenticated, otherwise fall back to the offline HTML
path below and say plainly that the self-hosted Design app is not yet available. Never
guess a subdomain. Never paste tokens, `Authorization`, or `http_headers` into a skill
file, a commit, or chat.

---

## Path selection

**Use the Design app (primary) when** the Design MCP connector is authenticated and
reachable — which is the normal case. It is the default for every redesign, new surface,
variant exploration, and design-to-code handoff.

**Use the offline HTML fallback when** any of these is true:

- the Design endpoint above is still `PENDING` and the upstream connector is not authenticated;
- the connector returns `Session terminated` / `needs auth` and reconnect is not possible right now;
- there is no network, or the operator explicitly asks for a local self-contained file;
- the artifact must be committed into a product repo and viewable with zero dependencies
  (e.g. a design that has to survive in `docs/design/` for reviewers without accounts).

Say which path you took and why. Do not silently downgrade.

---

## The non-negotiables (both paths)

1. **Mobbin first, always.** Real shipped UIs beat invented ones. Search Mobbin (the
   `mcp__mobbin__*` tools, `platform: web`) for the component before designing. Cite each
   screen you show as a markdown link to its `mobbin_url`.
2. **Dark + light, every time.** Never ship a design with only one theme. The light theme
   is a **real re-think** of surfaces, contrast, elevation and shadow weight — **not
   `filter: invert`**, not a token flip.
3. **Ground in the REAL tokens.** Use the app's actual colors, fonts, radii, shadows — see
   the project's own design skill for the verified token reference. Confirm against the
   live `tailwind.config` / `globals.css` rather than trusting memory.
4. **Fully interactive.** Buttons toggle, inputs recompute, the primary action opens a
   confirm → executing → success flow where that matches reality. A static mock is not enough.
5. **Look at it before calling it done.** Render/screenshot the key states and verify they
   actually work.
6. **Design first; don't port until told.** Big changes wait for explicit approval.
7. **Component by component.** One surface per pass. Don't boil the ocean.

## House style & the hard bans

Read `references/forbidden-ai-tells.md` first — the operator can spot AI-generated UI instantly.
The **five hard bans** (apply everywhere, both paths, every variant): (1) colored left-edge
accent rails; (2) arrows / chevrons of any kind (→ ← ↗ ↘, "View all →", chevron nav);
(3) purple/indigo→blue & teal gradient clichés, gradient text, generic design fonts
(Inter/Roboto/Arial/system-ui); (4) emoji (inline stroke SVG only); (5) **EYEBROWS —
absolute, never any overline/kicker label above a heading (uppercase, letter-spaced, mono,
accent, or grey alike). The heading leads alone.** Do **not** flatten the design into bland
editorial to avoid "AI slop" — keep it rich, confident, and specific to the product.

**ALWAYS pair with `frontend-design`.** Invoke it at the start of every pass to set the bar
for type, motion, and avoiding generic AI aesthetics. Then apply the house polish standard
below — every rule, every variant, both paths. It is house standard, not optional: the
operator confirmed this polish pass "makes everything so much better", and specifically
loves the **smoothness** and the **glassiness**. A design that skips it is not done.

---

## House polish standard (mandatory, both paths)

Eight rules. Each is checkable — read the CSS and answer yes or no. If you cannot point at
the line that satisfies a rule, the rule is not satisfied.

1. **Concentric radii.** Any rounded thing inside another rounded thing computes its radius:
   `inner = outer − padding`. A 16px card with 8px padding holds 8px children. Never give a
   nested element the same radius as its parent, and never nest a sharper corner inside a
   softer one — the gap between the curves is the tell. Prefer
   `border-radius: calc(var(--radius) - 8px)` over a hardcoded guess so it survives a token change.
2. **Tabular numerals on anything that changes.** Prices, balances, counters, timers,
   percentages, countdowns, table columns of figures — `font-variant-numeric: tabular-nums`
   (plus `font-feature-settings: "tnum" 1` where the face needs it). Digits must not shift
   width as they tick. Static labels may stay proportional.
3. **Press feedback on every interactive element.** `:active { transform: scale(0.96) }` for
   buttons and controls, ~0.98 for large cards and rows, with a fast ease-out transition
   (~120ms in, ~200ms out). Hover is not enough — the press must be felt. Give the element a
   `transform-origin: center` and never animate `width`/`height` to fake it.
4. **Staggered, interruptible reveals.** Lists, grids and stacked panels enter with a
   per-item delay (40–60ms apart, cap the stagger at ~8 items so long lists don't crawl).
   Every transition must be interruptible: animate `transform` and `opacity` only, and let a
   new state take over mid-flight rather than queueing. If a user can click through the
   animation and the UI locks or jumps, it is wrong. Entrances are short — 200–400ms.
5. **Layered shadows, never a single one.** Elevation is at least two stacked shadows: a
   tight contact shadow (~1–2px blur, low alpha) plus a wide ambient shadow (~12–32px blur,
   lower alpha), optionally a third for high elevation. One fat `0 4px 12px rgba(0,0,0,.1)`
   is the default-template look and is banned. In the light theme, shadows carry the
   elevation; in the dark theme, lean on surface lightness and a hairline border instead —
   dark shadows on dark surfaces do nothing.
6. **Subtle outlines on images and media.** Every photo, avatar, thumbnail, chart canvas and
   embedded media block gets a hairline inset edge —
   `box-shadow: inset 0 0 0 1px rgb(255 255 255 / 0.08)` on dark,
   `inset 0 0 0 1px rgb(0 0 0 / 0.06)` on light — so it reads as a placed object rather than
   a hole punched in the surface. It applies to the light theme too, at lower alpha.
7. **Glassy elevated surfaces.** Anything floating above the page — modals, popovers, sheets,
   sticky headers, toasts, command palettes — uses a translucent background plus
   `backdrop-filter: blur(16px) saturate(140%)` (ship the `-webkit-` prefix), a hairline
   border, and a layered shadow per rule 5. The background must be genuinely translucent
   (roughly 70–85% opaque) so content behind it moves through the blur; an opaque panel with
   a blur declared on it is not glass. Always provide an opaque fallback under
   `@supports not (backdrop-filter: blur(1px))`. Do not glass the base page surface — glass
   only means elevation.
8. **Honour `prefers-reduced-motion` fully.** Every animation and transition above sits
   inside a motion-safe path, with a real reduced branch:

   ```css
   @media (prefers-reduced-motion: reduce) {
     *, *::before, *::after {
       animation-duration: 0.01ms !important;
       animation-iteration-count: 1 !important;
       transition-duration: 0.01ms !important;
       scroll-behavior: auto !important;
     }
   }
   ```

   Reduced motion means the end state appears immediately — it never means the element fails
   to appear. Keep opacity/colour state changes; drop movement, scale, parallax and
   auto-playing loops. Test it: toggle the OS setting (or emulate it in DevTools) and confirm
   every screen still reaches its final state.

---

## PRIMARY PATH — the Agent Native Design app

Action names below are verbatim from the Design template's action set. Invoke them as MCP
tools on `agent-native-design`.

### 0. Orient

`view-screen` — takes no parameters. Returns what the user is currently looking at: view,
open design, active screen, selection, inspector tab, pending question overlay. **Call this
first before any other Design action.**

### 1. Create the shell

`create-design { title, projectType: "prototype" }` — omit `id`, the app generates it.
This creates an **empty** project: it returns `renderable: false` and contains no files
yet. Do not report a design as ready until it has renderable content.

### 2. Ask before generating (recommended)

`show-design-questions { designId, title, questions[] }` renders a full-canvas question
overlay; the answers come back as a chat message. **Stop and wait for the answers.** Skip
it when the prompt is already specific, when this is an edit to existing work, or when the
operator says "decide for me". Question types include `text-options`, `color-options`,
`slider`, `file`, `freeform`.

Always resolve the form factor here and carry it forward. Device frames: mobile 390×844,
tablet 768×1024, desktop 1440×900.

### 3. Explore directions

`present-design-variants { designId, prompt, variants[] }` — **2 to 5 variants, three by
default.** Each variant takes `id`, `label`, and optionally `description`, `accentColor`,
`features[]`, `content` (a full self-contained HTML screen), `width`, `height`.

- Supply `content` yourself. If you omit it the app fills in a generic dark placeholder
  screen — that is not a design.
- Variants must be **structurally and behaviourally distinct**, not colour swaps of one layout.
- Every variant obeys the five hard bans and the token grounding.
- The screens are placed on the overview board and the user gets inline buttons:
  *"Which screen should I keep?"*

### 4. The user picks

The pick arrives as a **chat message**, not a server event. It names the winning label,
filename, file id and variant set id, and instructs you to delete the losing screens.
Follow it exactly:

- `delete-file { id }` once per losing screen. It is idempotent — if a screen is already
  gone it returns `deleted: false`; continue, do not retry the cycle.
- If the chat buttons are unavailable, ask the user for the screen name. **Never** ask the
  user to paste HTML.

### 5. Snapshot

`get-design-snapshot { designId, fileId }` — call it **exactly once**. Returns the live
content, the file list, `tweaks`, `appliedTweaks`, `resolvedCssVars` and `lockedLayers`.
Pass `filename` instead of `fileId` only if you don't have the id; if several files match a
filename it errors and asks for the id.

### 6. Edit

`edit-design { designId, fileId, mode, ... }`:

- `mode: "replace-file"` + `replacementContent` — after a variant pick, this is the mode.
- `mode: "search-replace"` + `edits: [{ search, replace }]` — for targeted refinements.

**Stop after the first successful `edit-design` save.** Do **not** call `generate-design`
after a variant pick, do not re-snapshot in a loop, do not create an `index.html`.

`generate-design` is for **new** files only (a fresh screen in an existing design).
`generate-screens` is for genuinely distinct screens (Home / Dashboard / Checkout), not
device sizes.

### 7. Check quality before declaring done

- `run-design-audit { designId, fileId }` — read-only accessibility audit. For each `error`
  finding with `fixAvailable: true`, call `apply-a11y-fix`. Fix the rest with `edit-design`.
  **A design with audit errors is not ready.**
- `take-design-screenshot { designId, fileId }` — headless render, 1280 desktop + 375 mobile
  by default. Fix everything reported in `diagnostics`. If Chromium is unavailable it
  returns `{ ok: false, reason }` instead of throwing — then look at the design in the browser.

### 8. Hand off to code

`export-coding-handoff { id, format }` — note the param is **`id`**, not `designId`.
Returns `rawUrl`, `zipUrl`, `prompt`, `clipboardText` and `expiresAt`; the bundle reflects
current content with tweaks resolved into `:root`. **The links expire after 7 days** — if
the handoff must outlive that, commit the exported files into the repo.

Use the handoff as the input to production porting. Porting is still a **separate,
approved** step: branch off the working base, treat it as a reskin, keep all logic,
type-check, open a PR.

### Dark + light inside Design

Design has **no built-in dark/light toggle**. Theming is CSS custom properties in the
design's own `:root`, driven by the Tweaks knob system (`apply-tweaks`, and
`index-design-tokens` / `preview-design-token-edit` / `apply-design-token-edit` for token
work). So the dual-theme rule is satisfied **explicitly**:

- ship dark and light as **two real screens** on the board (or a real theme tweak that
  genuinely re-thinks surfaces), and
- expose the tokens the project actually uses.

The conventional knob is a `dark-mode` tweak bound to a `--dark-mode` CSS var (resolves to
`"1"`/`"0"`). Minimum `:root` tokens for a generated design: `--color-primary`,
`--color-accent`, `--color-surface`, `--color-text`, `--color-text-muted`,
`--font-heading`, `--font-body`, `--radius`.

Reusable brand tokens live in **design systems**: `create-design-system`,
`get-design-system`, `list-design-systems`, `set-default-design-system`, and importers
(`import-design-tokens`, `import-code`, `import-github`, `import-from-url`,
`analyze-brand-assets`). Set the project's design system once and pass `designSystemId`
rather than re-typing tokens per design.

### Version history — never lose a liked design

**Verified limitation, stated plainly:** Design has a `design_versions` table, but there is
**no agent-facing action to list or restore a design version**. The framework's generic
`create-resource-version` / `restore-resource-version` actions have no registered design
resource type. So version safety is a **discipline you enforce**, not a button:

1. **Before any risky reshape of a liked design, call `duplicate-design { id, title }`.**
   That deep copy is the "liked version" — never edit it again. This is the Design-app
   equivalent of `<surface>-original-vN.html`.
2. **Never destructively overwrite a liked version.** `edit-design` with
   `mode: "replace-file"` is destructive to that file. Duplicate first.
3. **Never pass `deleteSupersededSetIds`** for a variant set the operator liked. Superseded
   sets still render; leaving them costs nothing and preserves the exploration history.
   Only hard-delete sets the operator has explicitly finished with.
4. **`save-design-as-template`** promotes a direction worth reusing across projects;
   `create-design-from-template` starts from it.
5. **For anything the operator praised, also `export-coding-handoff` and commit the export**
   into the project repo. That is the only copy guaranteed to outlive the app, the 7-day
   handoff TTL, and any hosting change.
6. Keep the newest/canonical direction obvious, note a short changelog per iteration in the
   chat summary, and keep the **live production baseline** available for comparison (embed
   it for public pages, recreate auth-gated pages, save a frozen screenshot so there is a
   record if the live site drifts).

### When the connector fails

On `Session terminated` or `needs auth`: **stop retrying.** In Claude Code run `/mcp` →
Authenticate/Reconnect the Design connector, or
`npx -y @agent-native/core@latest reconnect <MCP URL from the ENDPOINT block>`. Never
reinstall to fix auth. Never hand-roll MCP calls with curl. If it still fails, take the
offline fallback and say so.

---

## FALLBACK PATH — standalone HTML prototype (offline / no host)

This is the previous workflow, kept intact. Use it only under the trigger conditions above.
Its implementation details live in `references/` and `assets/` — those files are still the
fallback's implementation and must not be deleted.

1. **`frontend-design` first** — set the bar for type, motion, and avoiding generic AI aesthetics.
2. **Pull Mobbin references** — one screen/flow per query, specific, plain language. Distill
   3–6 into a short "here's the pattern" synthesis before building.
3. **Map the real implementation + tokens** — dispatch an Explore subagent over the frontend
   to report the current component's JSX + props, the real tokens, fonts, formatting helpers,
   chart approach, skeletons. The redesign is almost always a visual reskin — keep the logic.
4. **Build the prototype** — a single self-contained `docs/design/<surface>-redesign.html`:
   inline `<style>` + vanilla JS, real fonts from Google Fonts, dark + light side by side,
   genuinely interactive. Motion on high-impact moments, not scattered micro-interactions.
   `references/prototype-template.md` has the skeleton (its retired visual motifs are
   superseded by the bans; use it only for structure).
5. **Open it and test live** in Chrome; screenshot default, alternate selection, modal, success,
   and the light theme. `file://` is mangled by the navigate tool — serve over localhost
   (`python -m http.server 8765 --bind 127.0.0.1`, background) and navigate to that.
6. **Present + park** — summarize the ideas (and the Mobbin ref each came from). Then stop;
   the prototype lives in `docs/design/`. Port into production only on explicit approval.

**Fallback version history:** commit every version the moment it's built; never
destructively overwrite a liked version (preserve it as `<surface>-original-vN.html`);
maintain a `<surface>-versions.html` with all versions live in iframes, newest/canonical on
top, a short changelog per entry, and the live PRODUCTION baseline at the bottom. Link every
prototype + versions page from the gallery `docs/design/index.html`.

**Fallback hosting + review comments:** the zero-dependency gallery with the Figma-style pin
comment overlay is still available — full recipe in
`references/self-host-gallery-comments.md`, reusable code in `assets/design-gallery/`.
Owner (operator) comments are authoritative — implement them automatically; guest comments
are suggestions — surface, don't auto-apply. Prefer the Design app's own review comments
when the primary path is available.

---

## References

- `references/forbidden-ai-tells.md` — the hard bans (READ FIRST, both paths).
- `references/prototype-template.md` — single-file skeleton + JS wiring for the fallback.
- `references/self-host-gallery-comments.md` — hosting the fallback gallery + pin-comment
  overlay on Dokploy, with author/owner-role gating. Code in `assets/design-gallery/`.
- **The project's own design skill, if that project defines one** (naming pattern:
  `<project>-design`) — real tokens, brand, staging URLs, design-system source, and any
  product-specific direction. Check the available-skills list before invoking it; when the
  project has no design skill, read the live `tailwind.config` / `globals.css` instead.
- Routing across surfaces: [Agent Native operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md).
