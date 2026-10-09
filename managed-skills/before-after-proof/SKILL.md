---
name: before-after-proof
description: "Captures before/after screenshots and visual comparison tables for UI verification in PRs and issues. Enforces dual viewport (1440/1920 desktop & 320/390 mobile), Veyyon browser tool, and secure GitHub-native image hosting (gh image, release assets, commit-pinned raw). Drops all public upload hosts."
---

# Before-After Proof

Captures before and after visual comparisons for UI changes and embeds them into GitHub pull requests and issues.

## Golden Invariants

- **Dual-Viewport Mandate:** All user-facing frontend changes require visual comparison across BOTH desktop (1440px or 1920px) and mobile (320px or 390px) viewports. Mobile optimization must never degrade desktop density or layout.
- **Secure GitHub-Native Image Hosting:** ONLY use approved GitHub-hosted formats:
  1. **GitHub user attachments via CLI:** `gh image <file>` (`drogers0/gh-image` extension) to obtain `https://github.com/user-attachments/assets/<uuid>` markdown references.
  2. **GitHub Release assets:** `gh release upload <tag> <file>` or GitHub Release Assets API.
  3. **Commit-pinned raw repository assets:** Same-domain `github.com/<owner>/<repo>/raw/<full-40-char-sha>/<path>` where the commit is pushed and reachable.
- **STRICTLY PROHIBITED:** NEVER use `0x0.st`, public gists, third-party blob hosts, or `raw.githubusercontent.com` (which fails with HTTP 404/403 on private repositories due to missing session cookies).
- **Mandatory Display Confirmation:** Immediately after posting an issue or PR comment, reload the page on GitHub and verify every attached image visibly renders. Never claim visual proof without confirming the rendered asset.
- **Staging Only:** Drive tests against staging (`https://staging.polysimulator.com`) or local dev server. PolySimulator production (`https://polysimulator.com`, `<prod-supabase-ref>`) is strictly off-limits.

- **No-Visible-Change Rule:** If a change causes no visual UI difference, omit the before/after table. State that the change has no visible UI effect. Never post identical screenshots.
- **Pair Non-Identity Invariant:** Every before/after pair must prove a real visual difference. The gate rejects byte-identical images, re-encoded identical pixels, and near-identical pairs. The minimum changed-pixel ratio is 0.0005 (0.05%). Pairs with different dimensions are rejected.

## Capture Protocol with Veyyon Browser Tool

Use the built-in Veyyon `browser` tool to capture clean, reproducible screenshots of before and after states.

### 1. Acquire Build/Browser Slot (If Launching Local Services)
If previewing a local branch or Next.js build, arbitrate the slot first:
```bash
python C:/Users/wkiri/.veyyon/workflows/build_slot.py acquire <task-name>
```

### 2. Capture Desktop Viewport (1440x900 or 1920x1080)
```javascript
// Open tab with desktop viewport
browser({
  action: "open",
  name: "qa-desktop",
  url: "https://staging.polysimulator.com/markets",
  viewport: { width: 1440, height: 900 }
});

// Wait for target selector to stabilize
browser({
  action: "run",
  name: "qa-desktop",
  code: "await tab.waitForSelector('.market-card', { timeout: 10000 }); await wait(1000); await tab.screenshot({ save: '.artifacts/desktop-after.png' });"
});
```

### 3. Capture Mobile Viewport (390x844 or 320x568)
```javascript
// Open tab with mobile viewport
browser({
  action: "open",
  name: "qa-mobile",
  url: "https://staging.polysimulator.com/markets",
  viewport: { width: 390, height: 844 }
});

// Wait for target selector and capture
browser({
  action: "run",
  name: "qa-mobile",
  code: "await tab.waitForSelector('.market-card', { timeout: 10000 }); await wait(1000); await tab.screenshot({ save: '.artifacts/mobile-after.png' });"
});
```

### 4. Release Build Slot
```bash
python C:/Users/wkiri/.veyyon/workflows/build_slot.py release <task-name>
```

## Uploading Proof to GitHub

### Method A: `gh image` Extension (Preferred)
```bash
# Upload each screenshot to GitHub user attachments
DESKTOP_BEFORE=$(gh image .artifacts/desktop-before.png)
DESKTOP_AFTER=$(gh image .artifacts/desktop-after.png)
MOBILE_BEFORE=$(gh image .artifacts/mobile-before.png)
MOBILE_AFTER=$(gh image .artifacts/mobile-after.png)
```
`gh image` returns the formatted markdown image string: `![image](https://github.com/user-attachments/assets/<uuid>)`.

### Method B: GitHub Release Assets
```bash
# Upload to a tracking release tag
gh release upload qa-evidence-tag .artifacts/desktop-before.png .artifacts/desktop-after.png
```

## PR / Issue Comparison Table Format

Format visual evidence in pull requests and tracking issues as centered comparison tables:

```markdown
## Visual Comparison

### Desktop (1440px)
| Before | After |
| :---: | :---: |
| ![Desktop Before](<desktop-before-url>) | ![Desktop After](<desktop-after-url>) |

### Mobile (390px)
| Before | After |
| :---: | :---: |
| ![Mobile Before](<mobile-before-url>) | ![Mobile After](<mobile-after-url>) |

**Verification Notes:**
- Viewports exercised: 1440x900 (Desktop) and 390x844 (Mobile)
- Target: Staging (`hgzyqmaanndcimnclxtv`)
- Verified image rendering: Confirmed visible in GitHub UI
```

## Verification and Linting

### 1. Verify Image Pair Locally Before Posting
Run the pair check tool on the captured files:
```bash
python workflows/portable/evidence_lint.py pair <before-path-or-url> <after-path-or-url>
```
The tool decodes RGB pixels and verifies matching dimensions.
It fails with exit 1 if the images are byte-identical, re-encoded identical, or below the 0.0005 change threshold.

### 2. Verify Posted PR or Issue Comment
After posting evidence to GitHub, verify the comment:
```bash
python workflows/portable/evidence_lint.py verify-posted <comment-or-pr-url>
```
The command verifies that all media URLs resolve.
It also extracts every before/after table pair and confirms that each pair passes the pixel change threshold.

## Post-Upload Check
Open the PR or issue URL in a browser.
Confirm that all images render and show the intended visual delta.
