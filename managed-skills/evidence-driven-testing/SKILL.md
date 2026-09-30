---
name: evidence-driven-testing
description: "Records verifiable visual, behavioral, and runtime evidence during test flows. Captures interactive sessions, assertion timelines, and dual-viewport verification using Veyyon browser tool, native recorders, or scripts, then embeds proof in GitHub issues and PRs."
---

# Evidence-Driven Testing

Record empirical proof of behavior, then attach it to the PR and tracker issue. Prose claims ("it works", "tested locally") are not evidence; every change requires verifiable proof.

## Golden Invariants

- **Original Scenario Defect Closure:** For bug fixes, reproduce and capture the exact failure **before** implementing any fix. That capture forms the "before" state. After the fix, exercise the identical scenario to prove defect absence.
- **Dual-Viewport Mandate:** All user-facing frontend changes require visual and interactive proof across BOTH desktop (1440px or 1920px) and mobile (320px or 390px) viewports. Mobile optimization must never compromise desktop legibility or layout.
- **Secure GitHub-Native Asset Hosting:** ONLY upload assets via GitHub-managed storage:
  1. `gh image <file>` (`drogers0/gh-image` extension) for screenshots -> `https://github.com/user-attachments/assets/<uuid>`.
  2. GitHub Release assets for videos (`evidence.mp4`) and large packages: `gh release upload <tag> <file>`.
  3. Commit-pinned raw repository assets: `github.com/<owner>/<repo>/raw/<full-40-char-sha>/<path>` (commit must be pushed).
- **STRICTLY PROHIBITED:** NEVER upload evidence to `0x0.st`, public gists, third-party blob hosts, or `raw.githubusercontent.com` (which fails on private repositories).
- **Staging Isolation:** Execute tests exclusively against staging (`https://staging.polysimulator.com` / `hgzyqmaanndcimnclxtv`) or isolated local dev servers. PolySimulator production (`https://polysimulator.com`, `<prod-supabase-ref>`) is strictly off-limits.
- **Build Slot Coordination:** Heavy compilation, dev servers, or builds must acquire the exclusive slot first:
  `python C:/Users/wkiri/.veyyon/workflows/build_slot.py acquire <task-name>`
  and release immediately upon completion:
  `python C:/Users/wkiri/.veyyon/workflows/build_slot.py release <task-name>`.

## Web UI Path: Veyyon Browser Tool

For browser-facing web UI, drive the real interface using the built-in `browser` tool with persistent staging authentication.

### 1. Verification Setup
```javascript
// Open tab with desktop viewport
browser({
  action: "open",
  name: "qa-run",
  url: "https://staging.polysimulator.com/markets",
  viewport: { width: 1440, height: 900 }
});
```

### 2. Interactive Drive & Assertion
```javascript
// Step through interaction and assert postcondition
browser({
  action: "run",
  name: "qa-run",
  code: `
    const obs = await tab.observe();
    const target = obs.elements.find(e => e.role === 'button' && e.name === 'Buy Yes');
    assert(target, 'Buy button missing');
    await (await tab.id(target.id)).click();
    await wait(500);
    await tab.screenshot({ save: '.artifacts/01-order-placed.png' });
  `
});
```

### 3. Mobile Viewport Assertion
Re-exercise the same critical path at mobile dimensions (`width: 390, height: 844`).

## Native Screen & Session Recorder (`scripts/evidence.py`)

The bundled recorder captures display sessions with FFmpeg, timestamps annotations, and burns subtitles into `evidence.mp4`.

### 1. Check Toolchain Readiness
```bash
python ~/.veyyon/profiles/default/agent/managed-skills/evidence-driven-testing/scripts/evidence.py doctor
```
Verifies FFmpeg, ffprobe, libx264, and the capture engine (`gdigrab` on Windows, `x11`/`wf-recorder` on Linux, `avfoundation` on macOS).

### 2. Start Recording
```bash
python ~/.veyyon/profiles/default/agent/managed-skills/evidence-driven-testing/scripts/evidence.py start \
  --output .artifacts/<task-name> \
  --title "<what is being verified>" \
  --commit "$(git rev-parse HEAD)" \
  --branch "$(git branch --show-current)" \
  --environment "Windows 11 / Chromium Staging"
```
Returns a `SESSION` path (e.g. `.artifacts/<task-name>/session.json`).

### 3. Annotate During Test
Add annotations as tests progress:
```bash
# Setup context
python ~/.veyyon/profiles/default/agent/managed-skills/evidence-driven-testing/scripts/evidence.py annotate "$SESSION" \
  --type setup --message "Signed in with staging QA account"

# Test start
python ~/.veyyon/profiles/default/agent/managed-skills/evidence-driven-testing/scripts/evidence.py annotate "$SESSION" \
  --type test_start --message "It should submit limit order and update balance"

# Assertion outcome (passed / failed / untested)
python ~/.veyyon/profiles/default/agent/managed-skills/evidence-driven-testing/scripts/evidence.py annotate "$SESSION" \
  --type assertion --result passed --message "Order filled and reserve balance debited"
```

### 4. Stop Recording & Finalize Evidence
```bash
python ~/.veyyon/profiles/default/agent/managed-skills/evidence-driven-testing/scripts/evidence.py stop "$SESSION"
```
Flushes capture, burns subtitle overlays into `evidence.mp4`, and generates `report.md` and `manifest.json`.

## Non-UI Runtime Evidence

Non-UI modifications (APIs, daemons, migrations, balance engines) do not use browser screenshots. Instead, record structured empirical data:
- **API routes:** Curl invocation, response status code, exact payload snippet, measured response latency in milliseconds.
- **Database / Balances:** Initial wallet balances, executed transaction, final balances, and resulting `ledger_entries` rows demonstrating balance conservation.
- **Migrations:** Alembic version table stamp, schema check (`check_migration_safety.py`), and reverse downgrade test proof.

## Uploading and Linking Evidence

1. **Upload Images:** `gh image .artifacts/01-order-placed.png` -> captures markdown embed URL.
2. **Upload Video:** `gh release upload qa-evidence-tag .artifacts/<task-name>/evidence.mp4`.
3. **Format Issue/PR Comment:**
   - Link the exact commit SHA tested (`reviewed-sha: <40-hex>`).
   - Embed the before and after visual comparison table or video link.
   - Include the generated `report.md` summary table of passed/failed assertions.
   - Reload on GitHub to confirm all media visibly renders.
