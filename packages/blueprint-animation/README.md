# Local Blueprint Animation

Standalone local tooling. No Claude Design login, starter, or service is used.
Use this capability only for noncommercial work. The attributed upstream sample stays in `noncommercial/blueprint-animation`. Do not embed it in commercial products or marketing.

## Run from this package directory

```sh
npm ci --ignore-scripts --no-audit --no-fund
npx playwright install chromium
node cli.mjs preview --input sample --out ../../artifacts/blueprint-sample --port 3582
node cli.mjs capture --input sample --out ../../artifacts/blueprint-sample --times 0,3,4,5,6,7,12,19,26,33,39
node cli.mjs preview --input ../../noncommercial/blueprint-animation/explain.json --out ../../artifacts/blueprint-explain --port 3582
node cli.mjs capture --input ../../noncommercial/blueprint-animation/explain.json --out ../../artifacts/blueprint-explain --times 0,2,3,4,5,6,7,12,19,22
node cli.mjs capture --input ../../noncommercial/blueprint-animation/before-after.json --out ../../artifacts/blueprint-before-after --times 0,2,3,4,5,6,7,16,17,18,19,20,22
```

Preview URL: http://127.0.0.1:3582/. Stop with Ctrl+C. Build without a server: replace `preview` with `build`. Output is an offline `index.html`, bundled JavaScript, attribution, and license. Capture adds PNG frames and `frames.json`. PNG filmstrips are supported; video/GIF export is not claimed. Capture uses an installed Playwright Chromium. Preview works in an ordinary local browser.

## Operator input

Supply JSON with `width`, `height`, `screen` (local self-contained SVG export), and 3–6 `steps`. Relative SVG paths resolve beside the JSON. Each step has `name`, `rect: [x,y,width,height]`, and either `what`/`why` (Explain), or `problem`/`fix` (Before/After). Provide `after` for Before/After and `afterRect` when that module moves. Only supplied screens are shown; the runtime does not invent a design. Export assets and fonts into the SVG; external assets are not bundled.

Optional `wires: [[x,y,w,h,radius,label], ...]` gives exact blueprint geometry for the entire screen. Without it, the renderer extracts absolute SVG rect elements. This convenience does not infer transformed groups, paths, glyphs, icons, or text. Supply complete wires for those designs. SVG pixels are preserved at rest, but this is not a Figma layer importer and does not establish exact font fidelity without embedded fonts.

Each step lasts seven seconds (upstream slow factor 1.4). A continuous screen shows focus, full-screen scan into blueprint, construct/annotate, scan reveal, and explanation hold. Explain keeps all screen pixels unchanged. Before/After replaces each target region (union of old and new rects) while covered by blueprint. Prior changes persist; the supplied complete After screen is shown at the end. Rects must cover every changed pixel; use nonoverlapping regions where possible. Moving geometry is represented by the focus wire, not inferred individual SVG layers. Provide explicit scene JSX for richer layer-level choreography.

The shipped CRM sample executes the original pinned JSX composition with an original React context/playhead adapter. Its five changes and visual composition are not redesigned. Missing original font/color tokens are explicit local defaults, so no exact original Figma fidelity is claimed. `window.CompositionStage`, `useComposition`, and `Easing` are implemented locally, not proprietary imports. `window.React`, scene names, and playback context are wired by the local bundle.

Controls: Play, Pause, Replay, seek slider. Preview API for deterministic QA: `window.blueprint.seek(seconds)`, `.play()`, `.pause()`, `.duration`; `document.documentElement.dataset.time` reports committed time. Captures pause and seek, then wait for committed render before saving. Default state is paused. Reduced-motion users can inspect static frames without automatic animation.

## Agent skill integration

The managed `blueprint-animation` skill should call the commands above after confirming noncommercial use. Collect the local SVG/JSON contract rather than asking for Claude Design. Return the preview URL, absolute output directory, mode and PNG filmstrip. Main owns managed skill installation and fresh-session discovery proof; no user-authored profile skill is edited by this package.

## Verification on the implementation worktree

- Upstream sample: 11 PNGs captured at the times above, with no browser page errors.
- Explain: 10 PNGs. Start and final capture are pixel-identical; construct differs.
- Before/After: 13 PNGs. Final shows Quick Actions above Node Health. Construct and final differ from start.
- Browser preview: Play advances time; Pause stops it; Replay returns to the beginning; deterministic seek reaches 18 and 22 seconds.

Source pin and license hashes are in `noncommercial/blueprint-animation/README.md`. The sample is not an operator Figma design.
