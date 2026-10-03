# Blueprint Animation Upstream Assets & Noncommercial Adaptation

## 1. Upstream Provenance & Attribution

- **Author**: Oğuz Bülbül ([@moguzbulbul](https://github.com/moguzbulbul), [oguz.design](https://oguz.design))
- **Source Repository**: [https://github.com/moguzbulbul/blueprint-animation](https://github.com/moguzbulbul/blueprint-animation)
- **Pinned Commit**: `29aa30b83db4632daf586420c52a251e7dac2d92` (release v1.3.1)
- **Upstream License**: Creative Commons Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)

### Preserved Upstream Files & SHA256 Integrity

The upstream reference files are stored verbatim in this directory:

| File | Size (bytes) | SHA256 Checksum |
| :--- | :--- | :--- |
| `example-scene.jsx` | 42,903 | `84db450d1ddfc3d9902156e5e67130b3c20ac51ea48b4d2a91cc9ce98a09ccb6` |
| `LICENSE` | 19,616 | `cd52919fb759d6d25d23af65fff81969a7379dee6a79f0d2688963a722caa7e4` |

---

## 2. Licensing & Noncommercial Boundary

- **License Scope**: All upstream materials and derived sample definitions in this directory are governed by CC BY-NC 4.0.
- **Operator Authorization**: The operator explicitly authorized local noncommercial adaptation in our harness without Claude Design or proprietary dependencies.
- **Commercial Restriction**: CC BY-NC 4.0 prohibits commercial use without explicit authorization from the upstream copyright holder. This directory is strictly segregated from commercial production code.

---

## 3. Upstream Runtime Architecture & Missing Global Exports

Inspection of `example-scene.jsx` reveals that the upstream component is authored against Claude Design's proprietary web container (`animations_v3`), which exposes specific animation and rendering primitives on `window` and assumes a global React runtime.

### Exact Missing Window Exports

1. **`window.CompositionStage`**
   - **Contract**: A React container component wrapping the composition view.
   - **Observed JSX Call** (lines 472–474):
     ```jsx
     <CompositionStage
       width={1600}
       height={1200}
       scenes={window.OM_SCENES}
       playback={window.OM_PLAYBACK}
       bg="#F2F2F2"
     >
       <Piece />
     </CompositionStage>
     ```
   - **Required Capabilities**: Renders a fixed-size canvas stage (`width`, `height`), manages background color (`bg`), provides composition context to child components, and wires scene playback metadata.

2. **`window.useComposition`**
   - **Contract**: A React hook providing timeline time and cue synchronization.
   - **Observed Usage** (line 29):
     ```javascript
     const { T, CUES, authoredTotal } = useComposition();
     ```
   - **Return Properties**:
     - `T`: Current animation playback time in seconds (floating point).
     - `CUES`: Dictionary mapping scene keys (e.g., `Actions`, `Details`, `Open work`, `Timeline`, `Panel`, `After`) to cue timestamps.
     - `authoredTotal`: Total authored composition duration in seconds.

3. **`window.Easing`**
   - **Contract**: Object providing interpolation easing curves.
   - **Observed Usage** (line 6):
     ```javascript
     const M = {
       enter: Easing.easeOutCubic,
       move: Easing.easeInOutCubic,
       draw: Easing.easeInOutQuad
     };
     ```
   - **Required Easing Functions**:
     - `Easing.easeOutCubic(t)`
     - `Easing.easeInOutCubic(t)`
     - `Easing.easeInOutQuad(t)`

4. **Global `React`**
   - **Contract**: Global `React` object (`React.createElement`, `React.Fragment`, hooks).
   - **Observed Usage**: `example-scene.jsx` contains uncompiled JSX and fragment syntax (`<> ... </>`) without importing React via ES modules or CommonJS (`const { ... } = window;` is used exclusively).

5. **Additional Global Window Hooks & Exports**
   - **`window.OM_SCENES`**: Passed into `CompositionStage` for scene tracking.
   - **`window.OM_PLAYBACK`**: Passed into `CompositionStage` for playback control.
   - **`window.NorthwindBlueprintApp`**: Exported entry point assigned to window at line 475 (`window.NorthwindBlueprintApp = NorthwindBlueprintApp;`).

---

## 4. Responsibility Separation & Local Runtime Contract

- **Directory Boundary**: This directory (`noncommercial/blueprint-animation`) houses only:
  1. The pinned upstream source (`example-scene.jsx`).
  2. The upstream license (`LICENSE`).
  3. This provenance and interface documentation (`README.md`).
  4. Original sample SVG screens (`screen.svg`, `screen-after.svg`) and JSON definitions (`explain.json`, `before-after.json`).
- **Runtime Implementation**: The standalone local runtime, CLI, and adapter are implemented separately by the parent workspace in `packages/blueprint-animation/runtime*` and `packages/blueprint-animation/package.json`. No proprietary Claude Design starter files or upstream prompt instructions are bundled or required.

---

## 5. Sample Scene Inputs

The included sample screens and JSON descriptors provide minimal, reproducible test fixtures:

- **`screen.svg`**: Original known baseline 800×600 SVG representing a cloud telemetry and operations console (navigation bar, throughput telemetry, node health card, quick actions, and event stream).
- **`screen-after.svg`**: Rearranged 800×600 SVG containing the exact same visual elements, with the high-priority Quick Actions panel promoted to the top right ([510, 100, 250, 170]) and the passive Node Health card shifted below ([510, 290, 250, 170]).
- **`explain.json`**: 3-step Explain mode descriptor with `name`, `what`, `why`, and `rect` bounding coordinates relative to `screen.svg`.
- **`before-after.json`**: 3-step Before/After mode descriptor with `name`, `problem`, `fix`, baseline `rect`, and target `afterRect` coordinates.
