<!--
Vendored from https://github.com/mattpocock/skills at commit 24fe0ef7737efae15c87225755e9f6f5965e4888
Upstream path: skills/engineering/improve-codebase-architecture/HTML-REPORT.md
Copyright (c) 2026 Matt Pocock. Used under the MIT License, reproduced below.
Local changes (super-board, issue #513): Tailwind and Mermaid CDNs removed; the report is one offline file built from report-template.html with inline CSS and hand-built SVG.

MIT License

Copyright (c) 2026 Matt Pocock

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
-->

# HTML Report Format

The architectural review is rendered as ONE self-contained HTML file built from [report-template.html](report-template.html). All CSS is inline. All diagrams are hand-built inline SVG or styled `<div>` boxes. There is no CDN, no remote font, and no script that fetches anything: the file must render the same with the network blocked.

## Scaffold

Copy `report-template.html`, repeat the `<article class="candidate">` block once per candidate, and fill the `{{placeholders}}`. Escape every inserted string for HTML. Keep the inline `<style>` as it is.

## Header

Repo name, date, and a compact legend: solid box = module, bar on a box = its interface, dashed line = seam, red arrow = leakage, thick dark box = deep module. No introduction paragraph. Straight into the candidates.

## Candidate card

The diagrams carry the weight. Prose is sparse, plain, and uses the glossary terms (from the `/codebase-design` skill) without ceremony.

Each candidate is one `<article>`:

- **Title**: short, names the deepening (e.g. "Collapse the Order intake pipeline").
- **Badge row**: recommendation strength (`Strong` = emerald, `Worth exploring` = amber, `Speculative` = slate), plus a tag for the dependency category (`in-process`, `local-substitutable`, `ports & adapters`, `mock`).
- **Deletion test**: a required line on every card, one of `pass-through`, `concentrates`, `inconclusive`, with one sentence of evidence. A card without it is invalid.
- **Files**: monospaced list (class `files`).
- **Before / After diagram**: the centrepiece. Two columns, side by side. See patterns below.
- **Problem**: one sentence. What hurts.
- **Solution**: one sentence. What changes.
- **Wins**: bullets, ≤6 words each. e.g. "Tests hit one interface", "Pricing logic stops leaking", "Delete 4 shallow wrappers".
- **ADR callout** (if applicable): one line in an amber-tinted box.

No paragraphs of explanation. If the diagram needs a paragraph to be understood, redraw the diagram.

## Diagram patterns

Pick the pattern that fits the candidate. Mix them. Don't make every diagram look the same. Variety is part of the point.

### SVG graph (the workhorse for dependencies / call flow)

Use hand-built inline SVG when the point is "X calls Y calls Z, and look at the mess." Draw modules as `<rect>` plus centred `<text>` (`text-anchor="middle"`), a module's interface as a filled bar on its edge (class `iface`; wide bar = shallow, narrow bar = deep), calls as `<path class="call">` with a marker arrow, a leaking call as `<path class="leak">`, and a seam as a dashed `<line>` (class `seam`). Set `marker-end` on each path and give each SVG its own marker ids (`call-<candidate-id>-before`, `leak-<candidate-id>-before`), because ids are global to the page. The template has a before/after pair to copy. No Mermaid.

### Hand-built boxes-and-arrows (for the "after" deep module)

Modules as `<div>`s with borders and labels, arrows as inline SVG `<line>` or `<path>` elements over a relative container. Use it when you want the "after" diagram to read as one thick-bordered deep module with greyed-out internals.

### Cross-section (good for layered shallowness)

Stack horizontal bands (a `<div>` with a left border) to show layers a call passes through. Before: 6 thin layers each doing nothing. After: 1 thick band labelled with the consolidated responsibility.

### Mass diagram (good for "interface as wide as implementation")

Two rectangles per module: one for interface surface area, one for implementation. Before: interface rectangle is nearly as tall as the implementation rectangle (shallow). After: interface rectangle is short, implementation rectangle is tall (deep).

### Call-graph collapse

Before: a tree of function calls rendered as nested boxes. After: the same tree collapsed into one box, with the now-internal calls shown faded inside it.

## Style guidance

- Lean editorial, not corporate-dashboard. Generous whitespace. Serif optional for headings.
- Colour sparingly: one accent (emerald or indigo) plus red for leakage and amber for warnings.
- Keep diagrams ~320px tall so before/after sits comfortably side by side without scrolling.
- Set module labels inside diagrams small, uppercase and letter-spaced, so they read as schematic, not as UI.
- No scripts at all. The report is static HTML and SVG.

## Top recommendation section

One larger card. Candidate name, one sentence on why, anchor link to its card. That's it.

## Tone

Plain English, concise, but the architectural nouns and verbs come straight from the `/codebase-design` skill. Concision is not an excuse to drift.

**Use exactly:** module, interface, implementation, depth, deep, shallow, seam, adapter, leverage, locality.

**Never substitute:** component, service, unit (for module) · API, signature (for interface) · boundary (for seam) · layer, wrapper (for module, when you mean module).

**Phrasings that fit the style:**

- "Order intake module is shallow: interface nearly matches the implementation."
- "Pricing leaks across the seam."
- "Deepen: one interface, one place to test."
- "Two adapters justify the seam: HTTP in prod, in-memory in tests."

**Wins bullets** name the gain in glossary terms: *"locality: bugs concentrate in one module"*, *"leverage: one interface, N call sites"*, *"interface shrinks; implementation absorbs the wrappers"*. Don't write *"easier to maintain"* or *"cleaner code"*, because those terms aren't in the glossary and don't earn their place.

No hedging, no throat-clearing, no "it's worth noting that…". If a sentence could be a bullet, make it a bullet. If a bullet could be cut, cut it. If a term isn't in the `/codebase-design` glossary, reach for one that is before inventing a new one.
