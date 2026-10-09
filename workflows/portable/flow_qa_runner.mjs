/**
 * Flow QA Runner
 *
 * Automated Puppeteer-based flow validation runner executing declarative safe JSON actions
 * across multiple viewports (390x844, 390x420, 1440x900) and themes (light, dark).
 *
 * Implements CDP touch events (tap, swipe), screenshot capture per step,
 * named layout and accessibility checks, mutation QA-prefix enforcement,
 * fail-closed cleanup obligations, and production URL rejection.
 *
 * Schema: flow-qa/v1
 */

import fs from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

export const SCHEMA_VERSION = 'flow-qa/v1';

export const VIEWPORTS = {
  '390x844': {
    width: 390,
    height: 844,
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 2,
    description: 'Touch / Mobile'
  },
  '390x420': {
    width: 390,
    height: 420,
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 2,
    description: 'Touch Keyboard Open'
  },
  '1440x900': {
    width: 1440,
    height: 900,
    isMobile: false,
    hasTouch: false,
    deviceScaleFactor: 1,
    description: 'Desktop'
  }
};

export const THEMES = ['light', 'dark'];

export const ALLOWED_ACTIONS = [
  'goto',
  'tap',
  'type',
  'hover',
  'keyboard-open',
  'swipe',
  'assert',
  'upload',
  'cleanup'
];

export const FORBIDDEN_ACTION_KEYWORDS = [
  'eval',
  'evaluate',
  'script',
  'exec',
  'execute',
  'shell',
  'raw_js',
  'function',
  'delete_database',
  'drop_table',
  'run_code'
];

export const FORBIDDEN_PRODUCTION_DOMAINS = [
  'zaraprptkegxqpvnsubu', // PolySimulator Supabase production
  'akamai-iad-prod',      // PolySimulator production Akamai
  'polysimulator.com',    // Bare production domain
  'app.polysimulator.com',
  'prod.polysimulator.com'
];

// ============================================================================
// 1. Dependency Resolution & Environment Probes
// ============================================================================

/**
 * Resolves puppeteer or puppeteer-core from local modules, Veyyon, or environment.
 */
export function resolvePuppeteer() {
  const req = createRequire(import.meta.url);

  // 1. Try standard module resolution
  try { return req('puppeteer'); } catch (_) {}
  try { return req('puppeteer-core'); } catch (_) {}

  // 2. Try Veyyon or known sibling checkouts
  const candidateAnchorFiles = [
    'C:/Users/wkiri/development/veyyon/package.json',
    'C:/Users/wkiri/development/veyyon-verify/package.json',
    'C:/Users/wkiri/development/super-board/package.json'
  ];

  for (const anchor of candidateAnchorFiles) {
    if (fs.existsSync(anchor)) {
      try {
        const anchorReq = createRequire(anchor);
        return anchorReq('puppeteer-core');
      } catch (_) {}
      try {
        const anchorReq = createRequire(anchor);
        return anchorReq('puppeteer');
      } catch (_) {}
    }
  }

  // 3. Direct node_modules search in user home or local app data
  const candidateDirectories = [
    'C:/Users/wkiri/development/veyyon/node_modules/puppeteer-core',
    'C:/Users/wkiri/.veyyon/node_modules/puppeteer-core',
    'C:/Users/wkiri/AppData/Roaming/npm/node_modules/puppeteer',
    'C:/Users/wkiri/AppData/Roaming/npm/node_modules/puppeteer-core'
  ];

  for (const dir of candidateDirectories) {
    if (fs.existsSync(dir)) {
      const pkgPath = path.join(dir, 'package.json');
      if (fs.existsSync(pkgPath)) {
        try {
          const directReq = createRequire(pkgPath);
          return directReq('./lib/puppeteer/puppeteer-core.js');
        } catch (_) {}
      }
    }
  }

  throw new Error('Puppeteer could not be resolved. Ensure puppeteer or puppeteer-core is installed.');
}

/**
 * Resolves Chrome/Chromium executable path on the workstation.
 */
export function resolveExecutablePath() {
  if (process.env.PUPPETEER_EXECUTABLE_PATH && fs.existsSync(process.env.PUPPETEER_EXECUTABLE_PATH)) {
    return process.env.PUPPETEER_EXECUTABLE_PATH;
  }
  if (process.env.CHROME_BIN && fs.existsSync(process.env.CHROME_BIN)) {
    return process.env.CHROME_BIN;
  }

  const standardPaths = [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    `${process.env.LOCALAPPDATA}/Google/Chrome/Application/chrome.exe`,
    `${process.env.LOCALAPPDATA}/Chromium/Application/chrome.exe`,
    '/usr/bin/google-chrome',
    '/usr/bin/chromium-browser',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
  ];

  for (const p of standardPaths) {
    if (p && fs.existsSync(p)) {
      return p;
    }
  }

  return undefined; // Let puppeteer attempt bundled browser if available
}

// ============================================================================
// 2. Invariants & Runtime Safety Guards
// ============================================================================

/**
 * Validates whether a URL targets a forbidden PolySimulator production environment.
 */
export function isProductionUrl(urlString) {
  if (!urlString || typeof urlString !== 'string') {
    return { forbidden: false };
  }

  let parsed;
  try {
    parsed = new URL(urlString, 'http://localhost');
  } catch (err) {
    return { forbidden: true, reason: `Invalid URL format: ${urlString}` };
  }

  const hostname = parsed.hostname.toLowerCase();

  // Allow staging explicitly
  if (hostname === 'staging.polysimulator.com' || hostname.endsWith('.staging.polysimulator.com')) {
    return { forbidden: false };
  }

  for (const forbidden of FORBIDDEN_PRODUCTION_DOMAINS) {
    if (hostname === forbidden || hostname.endsWith(`.${forbidden}`)) {
      return {
        forbidden: true,
        reason: `Target hostname "${hostname}" matches forbidden production domain "${forbidden}"`
      };
    }
    if (urlString.toLowerCase().includes(forbidden)) {
      return {
        forbidden: true,
        reason: `Target URL contains forbidden production token "${forbidden}"`
      };
    }
  }

  return { forbidden: false };
}

/**
 * Asserts target URL is not production, failing closed immediately.
 */
export function assertNotProduction(urlString) {
  const check = isProductionUrl(urlString);
  if (check.forbidden) {
    const error = new Error(`Runtime Safety Refusal: ${check.reason}`);
    error.name = 'ProductionForbiddenError';
    error.category = 'safety';
    throw error;
  }
}

/**
 * Validates served commit SHA against expected SHA.
 */
export function verifyServedSha(servedSha, expectedSha) {
  const served = String(servedSha || '').trim().toLowerCase();
  const expected = String(expectedSha || '').trim().toLowerCase();
  const match = /^[0-9a-f]{40}$/.test(served) && /^[0-9a-f]{40}$/.test(expected) && served === expected;
  return {
    match,
    served_sha: served,
    expected_sha: expected,
    detail: match ? `Served SHA ${served} matches expected SHA ${expected}`
      : `Served SHA mismatch: got "${served}", expected full head "${expected}"`
  };
}

export function readServedSha(data) {
  const sha = [data?.commit, data?.sha, data?.served_sha, data?.version, data?.git_sha, data?.commitSha]
    .find((value) => typeof value === 'string' && /^[0-9a-f]{40}$/i.test(value));
  if (!sha) throw new Error('Served SHA requires a 40-hex commit');
  return sha;
}

/**
 * Checks /api/version before running actions.
 */
export async function checkVersionEndpoint(baseUrl, expectedSha, fetchFn = fetch) {
  assertNotProduction(baseUrl);

  const cleanBase = baseUrl.replace(/\/+$/, '');
  let data = null;
  try {
    const res = await fetchFn(`${cleanBase}/api/version`, {
      method: 'GET',
      headers: { 'Accept': 'application/json' },
      signal: AbortSignal.timeout(10000)
    });
    if (res.ok) {
      data = await res.json();
    }
  } catch (_) {}
  let versionSha = null;
  try { versionSha = readServedSha(data); } catch (_) {}
  if (!versionSha) {
    try {
      const res = await fetchFn(`${cleanBase}/api/health`, {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
        signal: AbortSignal.timeout(10000)
      });
      if (res.ok) {
        data = await res.json();
      }
    } catch (_) {}
  }
  try {
    if (!data) {
      return {
        passed: false,
        served_sha: null,
        expected_sha: expectedSha,
        detail: `GET /api/version and /api/health failed to return JSON`
      };
    }
    const servedSha = readServedSha(data);
    const shaCheck = verifyServedSha(servedSha, expectedSha);

    return {
      passed: shaCheck.match,
      served_sha: shaCheck.served_sha,
      expected_sha: shaCheck.expected_sha,
      detail: shaCheck.detail
    };
  } catch (err) {
    return {
      passed: false,
      served_sha: null,
      expected_sha: expectedSha,
      detail: `Failed to query /api/version: ${err.message}`
    };
  }
}

// ============================================================================
// 3. Declarative Action Validation & QA-Prefix Invariants
// ============================================================================

/**
 * Validates a declarative step against allowed actions, forbidden operations,
 * and ensures no arbitrary executable JS is present.
 */
export function validateSafeAction(step, flowConstraints = {}) {
  if (!step || typeof step !== 'object') {
    return {
      valid: false,
      error: 'Step must be a valid non-null object'
    };
  }

  const action = step.action;
  if (!action || typeof action !== 'string') {
    return {
      valid: false,
      error: `Missing or invalid "action" in step "${step.id || 'anonymous'}"`
    };
  }

  // 1. Must be in system allowlist
  if (!ALLOWED_ACTIONS.includes(action)) {
    return {
      valid: false,
      error: `Action "${action}" is not permitted. Allowed actions: ${ALLOWED_ACTIONS.join(', ')}`
    };
  }

  // 2. Check flow-specific allowed_actions
  if (Array.isArray(flowConstraints.allowed_actions) && flowConstraints.allowed_actions.length > 0) {
    if (!flowConstraints.allowed_actions.includes(action)) {
      return {
        valid: false,
        error: `Action "${action}" is not permitted by flow constraints: [${flowConstraints.allowed_actions.join(', ')}]`
      };
    }
  }

  // 3. Check flow-specific forbidden_actions
  if (Array.isArray(flowConstraints.forbidden_actions)) {
    if (flowConstraints.forbidden_actions.includes(action)) {
      return {
        valid: false,
        error: `Action "${action}" is forbidden by flow constraint list`
      };
    }
  }

  // 4. Ensure no arbitrary JS execution properties
  for (const forbiddenKey of FORBIDDEN_ACTION_KEYWORDS) {
    if (forbiddenKey in step) {
      return {
        valid: false,
        error: `Step contains forbidden execution property "${forbiddenKey}". Arbitrary JS in flow data is prohibited.`
      };
    }
  }

  // 5. If goto action, verify URL safety
  if (action === 'goto' && step.url) {
    const prodCheck = isProductionUrl(step.url);
    if (prodCheck.forbidden) {
      return {
        valid: false,
        error: `Action goto target URL is forbidden: ${prodCheck.reason}`
      };
    }
  }

  // 6. Action-specific validation
  if (action === 'hover') {
    const hasSelector = typeof step.selector === 'string' && step.selector.trim().length > 0;
    const hasCoords = step.x !== undefined && step.y !== undefined && !isNaN(Number(step.x)) && !isNaN(Number(step.y));
    if (!hasSelector && !hasCoords) {
      return {
        valid: false,
        error: `Action "hover" requires "selector" or "x" and "y" in step "${step.id || 'anonymous'}"`
      };
    }
  }

  return { valid: true };
}

/**
 * Validates mutation values (e.g. typing or form field modifications)
 * enforce the required "QA-" prefix to prevent accidental real data entry.
 */
export function validateMutationPayload(action, step) {
  if (!step || typeof step !== 'object') {
    return { valid: true };
  }

  const isTypeAction = action === 'type';
  const isMarkedMutation = !!step.mutation || !!step.is_mutation || !!step.qa_mutation;

  if (isTypeAction || isMarkedMutation) {
    const textVal = step.text ?? step.value ?? step.input;
    if (typeof textVal === 'string' && textVal.length > 0) {
      // A number input cannot hold the "QA-" prefix. A step may opt in with numeric_only when the
      // value is plain digits and the form is never submitted.
      if (step.numeric_only === true && /^\d+(\.\d+)?$/.test(textVal)) {
        return { valid: true };
      }
      const requiresQaPrefix = isMarkedMutation || (step.selector && !step.selector.includes('search'));
      if (requiresQaPrefix && !textVal.startsWith('QA-')) {
        return {
          valid: false,
          error: `Mutation field value "${textVal}" for selector "${step.selector || 'unknown'}" must begin with "QA-" prefix`
        };
      }
    }
  }

  if (action === 'cleanup') {
    const targetText = step.target_text ?? step.text ?? step.match_text;
    if (typeof targetText === 'string' && targetText.length > 0 && !targetText.startsWith('QA-')) {
      return {
        valid: false,
        error: `Cleanup target text "${targetText}" for step "${step.id || 'anonymous'}" must begin with "QA-" prefix`
      };
    }
  }

  return { valid: true };
}

// ============================================================================
// 4. Exported Named Check Helpers (Testable Units)
// ============================================================================

/**
 * Named Check 1: Visible Target
 * Confirms target exists, has positive bounding box, and is visible in computed style.
 */
export function checkVisible(rect, style = {}) {
  if (!rect) {
    return {
      name: 'visible',
      passed: false,
      detail: 'Element bounding client rect is null or element not found in DOM'
    };
  }

  const hasSize = rect.width > 0 && rect.height > 0;
  const isDisplayed = style.display !== 'none';
  const isVisible = style.visibility !== 'hidden';
  const isOpaque = style.opacity !== '0';

  const passed = hasSize && isDisplayed && isVisible && isOpaque;
  const detail = passed
    ? `Target is visible (${Math.round(rect.width)}x${Math.round(rect.height)}px)`
    : `Target is not visible: size=${rect.width}x${rect.height}, display=${style.display}, visibility=${style.visibility}, opacity=${style.opacity}`;

  return { name: 'visible', passed, detail };
}

/**
 * Named Check 2: Element From Point (Covered Check)
 * Confirms center point of target element is unobstructed (not covered by an overlay, header, or dialog).
 */
export function checkCovered(targetRect, pointElementInfo = {}) {
  return checkElementFromPoint(targetRect, pointElementInfo);
}

export function checkElementFromPoint(targetRect, pointElementInfo = {}) {
  if (!targetRect) {
    return {
      name: 'element_from_point',
      passed: false,
      detail: 'Cannot check elementFromPoint without target rect'
    };
  }

  const cx = Math.round(targetRect.x + targetRect.width / 2);
  const cy = Math.round(targetRect.y + targetRect.height / 2);

  if (pointElementInfo.isTargetOrDescendant === true) {
    return {
      name: 'element_from_point',
      passed: true,
      detail: `Target is unobstructed at center (${cx}, ${cy})`
    };
  }

  if (pointElementInfo.isTargetOrDescendant === false) {
    const coveringElement = pointElementInfo.coveringElementDescription || 'another element';
    return {
      name: 'element_from_point',
      passed: false,
      detail: `Target covered by <${coveringElement}> at center point (${cx}, ${cy})`
    };
  }

  return {
    name: 'element_from_point',
    passed: false,
    detail: `No element found at center point (${cx}, ${cy})`
  };
}

/**
 * Named Check 3: Tap Target Minimum 44x44
 * Confirms touch target width and height are both >= 44px for mobile accessibility.
 */
export function checkTapTargetMin44(rect) {
  if (!rect) {
    return {
      name: 'tap_target_min_44',
      passed: false,
      detail: 'Target rect is missing'
    };
  }

  const width = Math.round(rect.width);
  const height = Math.round(rect.height);
  const passed = width >= 44 && height >= 44;

  const detail = passed
    ? `Tap target size ${width}x${height}px meets minimum 44x44px standard`
    : `Tap target size ${width}x${height}px is below minimum 44x44px requirement`;

  return { name: 'tap_target_min_44', passed, detail };
}

/**
 * Named Check 4: No Horizontal Overflow
 * Confirms document scrollWidth does not exceed window innerWidth (causes mobile scroll drift).
 */
export function checkNoHorizontalOverflow(scrollWidth, innerWidth) {
  if (typeof scrollWidth !== 'number' || typeof innerWidth !== 'number') {
    return {
      name: 'no_horizontal_overflow',
      passed: false,
      detail: 'scrollWidth and innerWidth must be numbers'
    };
  }

  // 1px tolerance for fractional sub-pixel rounding
  const passed = scrollWidth <= innerWidth + 1;
  const detail = passed
    ? `No horizontal overflow: scrollWidth ${scrollWidth}px <= innerWidth ${innerWidth}px`
    : `Horizontal overflow detected: scrollWidth ${scrollWidth}px exceeds innerWidth ${innerWidth}px by ${scrollWidth - innerWidth}px`;

  return { name: 'no_horizontal_overflow', passed, detail };
}

/**
 * Named Check 5: Input Focus In Viewport
 * Confirms focused element rect is within visual viewport bounds with 2px subpixel tolerance.
 */
export function checkInputFocusInViewport(rect, viewportHeight, viewportWidth) {
  if (!rect) {
    return {
      name: 'input_focus_in_viewport',
      passed: false,
      detail: 'No focused element rect found'
    };
  }

  const topInBounds = rect.top >= -2;
  const bottomInBounds = rect.bottom <= viewportHeight + 2;
  const leftInBounds = rect.left >= -2;
  const rightInBounds = rect.right <= viewportWidth + 2;

  const passed = topInBounds && bottomInBounds && leftInBounds && rightInBounds;
  let detail = '';
  if (passed) {
    detail = `Focused input rect [top: ${Math.round(rect.top)}, bottom: ${Math.round(rect.bottom)}, left: ${Math.round(rect.left)}, right: ${Math.round(rect.right)}] is within visual viewport (${viewportWidth}x${viewportHeight}px)`;
  } else {
    const reasons = [];
    if (!topInBounds) reasons.push(`top ${Math.round(rect.top)} < 0`);
    if (!bottomInBounds) reasons.push(`bottom ${Math.round(rect.bottom)} > ${viewportHeight} (keyboard occlusion)`);
    if (!leftInBounds) reasons.push(`left ${Math.round(rect.left)} < 0`);
    if (!rightInBounds) reasons.push(`right ${Math.round(rect.right)} > ${viewportWidth}`);
    detail = `Focused input rect is outside visual viewport (${viewportWidth}x${viewportHeight}px): ${reasons.join(', ')}`;
  }

  return { name: 'input_focus_in_viewport', passed, detail };
}

/**
 * Named Check 6: No Document Reload
 * Confirms document identity is preserved and no full page reload occurred during tab/filter navigation.
 */
export function checkNoDocumentReload(beforeDocId, afterDocId, navigationObserved = false) {
  if (!beforeDocId || !afterDocId) {
    return {
      name: 'no_document_reload',
      passed: false,
      detail: 'Document identity token missing before or after action'
    };
  }

  if (navigationObserved) {
    return {
      name: 'no_document_reload',
      passed: false,
      detail: 'Full document navigation occurred during client-side tab/filter action'
    };
  }

  const passed = beforeDocId === afterDocId;
  const detail = passed
    ? 'Document identity preserved; no full page reload occurred'
    : 'Document identity token changed; page reloaded unexpectedly';

  return { name: 'no_document_reload', passed, detail };
}

/**
 * Named Check 7: Swipe Dismissal
 * Confirms target element was dismissed (hidden, removed from DOM, or translated off-screen) after swipe.
 */
export function checkSwipeDismissal(beforeRect, afterState = {}) {
  if (!beforeRect) {
    return {
      name: 'dismissed',
      passed: false,
      detail: 'Target was not present prior to swipe'
    };
  }

  const isRemoved = afterState.exists === false;
  const isHidden = afterState.visible === false || afterState.display === 'none' || afterState.opacity === '0';
  const isOffScreen = afterState.offScreen === true;

  const passed = isRemoved || isHidden || isOffScreen;
  const detail = passed
    ? `Element successfully dismissed after swipe (${isRemoved ? 'removed' : isHidden ? 'hidden' : 'off-screen'})`
    : 'Element remains visible and in DOM at original position after swipe';

  return { name: 'dismissed', passed, detail };
}

// ============================================================================
// 5. CDP Touch Interactions (Real Touchscreen Events)
// ============================================================================

/**
 * Dispatches real CDP touch tap at (x, y) via DevTools Protocol Input.dispatchTouchEvent.
 */
export async function dispatchCdpTap(cdpSession, x, y) {
  const roundX = Math.round(x);
  const roundY = Math.round(y);
  const touchPoint = {
    x: roundX,
    y: roundY,
    radiusX: 0.5,
    radiusY: 0.5,
    force: 0.5,
    id: 1
  };

  await cdpSession.send('Input.dispatchTouchEvent', {
    type: 'touchStart',
    touchPoints: [touchPoint]
  });

  await new Promise(r => setTimeout(r, 40));

  await cdpSession.send('Input.dispatchTouchEvent', {
    type: 'touchEnd',
    touchPoints: [touchPoint]
  });
}

/**
 * Dispatches real CDP touch swipe from (startX, startY) to (endX, endY) with intermediate touchMove events.
 */
export async function dispatchCdpSwipe(cdpSession, startX, startY, endX, endY, steps = 10, durationMs = 160) {
  const stepDelay = Math.max(8, Math.floor(durationMs / Math.max(1, steps)));

  // 1. Touch start
  await cdpSession.send('Input.dispatchTouchEvent', {
    type: 'touchStart',
    touchPoints: [{
      x: Math.round(startX),
      y: Math.round(startY),
      radiusX: 0.5,
      radiusY: 0.5,
      force: 0.5,
      id: 1
    }]
  });

  // 2. Intermediate touch moves
  let lastX = Math.round(startX);
  let lastY = Math.round(startY);
  for (let i = 1; i <= steps; i++) {
    const fraction = i / steps;
    lastX = Math.round(startX + (endX - startX) * fraction);
    lastY = Math.round(startY + (endY - startY) * fraction);

    await new Promise(r => setTimeout(r, stepDelay));
    await cdpSession.send('Input.dispatchTouchEvent', {
      type: 'touchMove',
      touchPoints: [{
        x: lastX,
        y: lastY,
        radiusX: 0.5,
        radiusY: 0.5,
        force: 0.5,
        id: 1
      }]
    });
  }

  // 3. Touch end
  await new Promise(r => setTimeout(r, stepDelay));
  await cdpSession.send('Input.dispatchTouchEvent', {
    type: 'touchEnd',
    touchPoints: [{
      x: lastX,
      y: lastY,
      radiusX: 0.5,
      radiusY: 0.5,
      force: 0.5,
      id: 1
    }]
  });
}

/**
 * Dispatches real CDP keyboard input via DevTools Protocol Input.dispatchKeyEvent.
 */
export async function dispatchCdpType(cdpSession, text, delay = 20) {
  if (!cdpSession) return;
  for (const char of text) {
    await cdpSession.send('Input.dispatchKeyEvent', {
      type: 'keyDown',
      text: char,
      unmodifiedText: char
    });
    await cdpSession.send('Input.dispatchKeyEvent', {
      type: 'keyUp'
    });
    if (delay > 0) {
      await new Promise(r => setTimeout(r, delay));
    }
  }
}

// ============================================================================
// 6. Browser Automation & Step Execution Engine
// ============================================================================

/**
 * Injects document identity token into the page for reload observation.
 */
async function stampDocumentIdentity(page) {
  return page.evaluate(() => {
    if (!window.__FLOW_QA_DOC_ID__) {
      window.__FLOW_QA_DOC_ID__ = 'doc-' + Date.now() + '-' + Math.random().toString(36).slice(2);
    }
    return window.__FLOW_QA_DOC_ID__;
  });
}

/**
 * Splits a flow selector into comma-separated alternatives and lifts every top-level
 * `:has-text('...')` out of each one. This is the only non-CSS selector form flows may use:
 * the subject element (the last compound) must contain the text, compared case-insensitively
 * with whitespace collapsed, like Playwright's `:has-text`. Everything else is plain CSS.
 * Returns [{ css, texts }] where `texts` may be empty.
 */
export function parseSelector(selector) {
  const parts = [];
  let current = '';
  let depth = 0;
  let quote = null;
  const flush = () => {
    if (current.trim()) parts.push(current.trim());
    current = '';
  };
  for (let i = 0; i < selector.length; i++) {
    const ch = selector[i];
    if (quote) {
      if (ch === '\\') { current += ch + (selector[++i] ?? ''); continue; }
      if (ch === quote) quote = null;
    } else if (ch === '"' || ch === "'") quote = ch;
    else if (ch === '(' || ch === '[') depth++;
    else if (ch === ')' || ch === ']') depth--;
    else if (ch === ',' && depth === 0) { flush(); continue; }
    current += ch;
  }
  flush();
  if (parts.length === 0) throw new Error(`Empty selector "${selector}"`);

  const marker = ':has-text(';
  return parts.map((part) => {
    const texts = [];
    const textAt = [];
    let css = '';
    let i = 0;
    let level = 0;
    let q = null;
    while (i < part.length) {
      const ch = part[i];
      if (!q && level === 0 && part.startsWith(marker, i)) {
        const open = part[i + marker.length];
        if (open !== "'" && open !== '"') throw new Error(`:has-text needs a quoted string in "${selector}"`);
        const start = i + marker.length + 1;
        let end = start;
        while (end < part.length && part[end] !== open) end += part[end] === '\\' ? 2 : 1;
        if (part[end + 1] !== ')') throw new Error(`Unterminated :has-text in "${selector}"`);
        texts.push(part.slice(start, end).replace(/\\(.)/g, '$1'));
        textAt.push(css.length);
        i = end + 2;
        continue;
      }
      if (q) { if (ch === '\\') { css += ch + (part[++i] ?? ''); i++; continue; } if (ch === q) q = null; }
      else if (ch === '"' || ch === "'") q = ch;
      else if (ch === '(' || ch === '[') level++;
      else if (ch === ')' || ch === ']') level--;
      css += ch;
      i++;
    }
    // :has-text filters the subject element, so nothing but the subject's own qualifiers may follow it.
    for (const at of textAt) {
      const after = css.slice(at).replace(/\([^)]*\)|\[[^\]]*\]/g, '');
      if (/[\s>+~]/.test(after)) throw new Error(`:has-text must be on the last compound of "${part}"`);
    }
    return { css: css.trim() === '' || /[\s>+~]$/.test(css) ? `${css}*` : css, texts };
  });
}

/**
 * Runs in the page: every element matching any parsed alternative, in document order, once.
 * Self-contained because Puppeteer serialises it.
 */
export function selectInPage(parts) {
  const norm = (value) => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const found = new Set();

  function queryUnder(root, css) {
    let matches = [];
    try {
      matches = Array.from(root.querySelectorAll(css));
    } catch (_) {}
    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of all) {
      if (el.shadowRoot) {
        matches = matches.concat(queryUnder(el.shadowRoot, css));
      }
    }
    return matches;
  }

  for (const { css, texts } of parts) {
    for (const el of queryUnder(document, css)) {
      const content = norm(el.textContent);
      if (texts.every((t) => content.includes(norm(t)))) found.add(el);
    }
  }
  return [...found];
}

/**
 * True for the error Puppeteer raises when a navigation destroys the execution context under a call.
 */
export function isDestroyedContextError(err) {
  return /Execution context was destroyed|Cannot find context with specified id/.test(String(err?.message ?? err));
}

/** Waits until the page has a live document that is done loading; never throws. */
async function settleNavigation(page, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      if ((await page.evaluate(() => document.readyState)) !== 'loading') return;
    } catch (_) {
      // The context is still being replaced; poll again.
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

/**
 * Runs `fn`. When a navigation destroys its execution context, waits for the new document to
 * settle and runs `fn` once more on it. A second failure, or any other error, propagates, so a
 * lookup that could not be made never reads as "absent", "not covered" or "no overflow".
 */
export async function retryOnNavigation(page, fn) {
  try {
    return await fn();
  } catch (err) {
    if (!isDestroyedContextError(err)) throw err;
    await settleNavigation(page);
    return fn();
  }
}

/** Handles for every element the flow selector matches, in document order. */
async function queryAll(page, selector) {
  const list = await page.evaluateHandle(selectInPage, parseSelector(selector));
  const handles = [];
  for (const prop of (await list.getProperties()).values()) {
    const el = prop.asElement();
    if (el) handles.push(el);
  }
  await list.dispose();
  return handles;
}

// Handles go through page.evaluate: ElementHandle.evaluate throws inside Puppeteer's call-site
// capture under Node 24 ("Cannot read properties of undefined (reading 'toString')").
async function isShown(page, handle) {
  return page.evaluate((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden' && !el.closest('[inert],[aria-hidden="true"]');
  }, handle);
}
async function isVisible(page, handle) {
  return isShown(page, handle);
}

/**
 * First match a user can act on. A page can hold several laid-out copies of one control (an inline
 * trade panel in the page body AND the same field inside an open bottom sheet). With one shown copy
 * that is the answer. With several, prefer the first whose centre is hit by elementFromPoint once
 * scrolled into view, so a copy sitting under the sheet's overlay loses to the one in the sheet.
 */
async function pickShown(page, handles) {
  const shown = [];
  for (const handle of handles) {
    if (await isShown(page, handle)) shown.push(handle);
  }
  if (shown.length < 2) return shown[0] || null;
  for (const handle of shown) {
    const reachable = await page.evaluate((el) => {
      el.scrollIntoView({ block: 'center', inline: 'nearest' });
      const r = el.getBoundingClientRect();
      const x = Math.round(r.x + r.width / 2);
      const y = Math.round(r.y + r.height / 2);
      let at = null;
      if (typeof document.elementsFromPoint === 'function') {
        const stack = document.elementsFromPoint(x, y);
        for (const cand of stack) {
          if (window.getComputedStyle(cand).pointerEvents !== 'none') {
            at = cand;
            break;
          }
        }
      }
      if (!at) at = document.elementFromPoint(x, y);
      return !!at && (at === el || el.contains(at));
    }, handle);
    if (reachable) return handle;
  }
  return shown[0];
}

/**
 * Polls until the selector matches (and, with `visible`, a match has a layout box).
 * Returns the first visible match, else the first match; null on timeout.
 * A page can render the same control twice (mobile and desktop copies) with one hidden by CSS.
 */
async function waitForTarget(page, selector, timeoutMs, { visible = true } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const { handles, shown } = await retryOnNavigation(page, async () => {
      const found = await queryAll(page, selector);
      return { handles: found, shown: await pickShown(page, found) };
    });
    if (shown) {
      for (const h of handles) {
        if (h !== shown) await h.dispose().catch(() => {});
      }
      return shown;
    }
    if (!visible && handles.length > 0) {
      const target = handles[0];
      for (let i = 1; i < handles.length; i++) {
        await handles[i].dispose().catch(() => {});
      }
      return target;
    }
    for (const h of handles) {
      await h.dispose().catch(() => {});
    }
    if (Date.now() >= deadline) return null;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

export async function firstVisibleHandle(page, selector, timeoutMs) {
  const handle = await waitForTarget(page, selector, timeoutMs);
  if (!handle) throw new Error(`No visible element for selector "${selector}"`);
  return handle;
}

/**
 * The element a step acts on right now: first visible match, else first match, else null.
 * A navigation under the lookup is retried once on the new document; a second failure throws.
 */
function currentTarget(page, selector) {
  return retryOnNavigation(page, () => currentTargetOnce(page, selector));
}

async function currentTargetOnce(page, selector) {
  const handles = await queryAll(page, selector);
  const picked = await pickShown(page, handles);
  const target = picked || handles[0] || null;
  for (const h of handles) {
    if (h !== target) await h.dispose().catch(() => {});
  }
  return target;
}

/**
 * Resolves the topmost hit element at (x, y), drilling recursively into open shadow roots.
 *
 * @param {Document|ShadowRoot|Element} rootOrTop - Starting document or top hit element
 * @param {number} x - Viewport horizontal coordinate
 * @param {number} y - Viewport vertical coordinate
 * @param {Function} [getComputedStyleFn] - Optional window.getComputedStyle function
 * @returns {Element|null} - Deepest element hit at (x, y)
 */
export function resolveDeepestHit(rootOrTop, x, y, getComputedStyleFn = (typeof window !== 'undefined' ? window.getComputedStyle : null)) {
  const getStyle = getComputedStyleFn || (() => ({ pointerEvents: 'auto' }));

  const hitAt = (scope) => {
    if (!scope) return null;
    if (typeof scope.elementsFromPoint === 'function') {
      const stack = scope.elementsFromPoint(x, y) || [];
      for (const cand of stack) {
        if (!cand) continue;
        try {
          if (cand.shadowRoot) {
            const shadowHit = hitAt(cand.shadowRoot);
            if (shadowHit && getStyle(shadowHit)?.pointerEvents !== 'none') return shadowHit;
          }
          if (getStyle(cand)?.pointerEvents !== 'none') return cand;
        } catch (_) {
          return cand;
        }
      }
      return stack[0] || null;
    }
    if (typeof scope.elementFromPoint === 'function') {
      return scope.elementFromPoint(x, y);
    }
    return null;
  };

  let current = null;
  if (rootOrTop && typeof rootOrTop.elementsFromPoint === 'function') {
    current = hitAt(rootOrTop);
  } else if (rootOrTop) {
    current = rootOrTop;
  }

  while (current && current.shadowRoot) {
    const deeper = hitAt(current.shadowRoot);
    if (!deeper || deeper === current) break;
    current = deeper;
  }

  return current;
}

/**
 * Evaluates whether `target` counts as hit by `deepHit`.
 * A control inside a shadow root counts as hit when it or its descendant is the topmost
 * deep element; the shadow host alone does not make the check fail for controls inside its shadow root.
 *
 * @param {Element} target - The control expected to be hit
 * @param {Element} deepHit - The element resolved at (x, y)
 * @returns {boolean}
 */
export function isTargetHit(target, deepHit) {
  if (!target || !deepHit) return false;
  if (target === deepHit) return true;

  if (typeof target.contains === 'function' && target.contains(deepHit)) {
    return true;
  }

  let curr = deepHit;
  while (curr) {
    if (curr === target) return true;
    const parent = curr.parentElement;
    if (parent) {
      curr = parent;
    } else {
      const root = typeof curr.getRootNode === 'function' ? curr.getRootNode() : null;
      curr = root && root !== curr ? root.host : null;
    }
  }

  let targetHost = typeof target.getRootNode === 'function' ? target.getRootNode()?.host : null;
  while (targetHost) {
    if (targetHost === deepHit) return true;
    targetHost = typeof targetHost.getRootNode === 'function' ? targetHost.getRootNode()?.host : null;
  }
  // Review widgets (e.g. Komo) mount an active pin catcher (.catch / .catcher)
  // across the viewport to receive clicks that place pins on the underlying target.
  if (deepHit.classList && (deepHit.classList.contains('catch') || deepHit.classList.contains('catcher'))) {
    return true;
  }

  return false;
}

/**
 * Evaluates in-page geometry and elementFromPoint for named checks.
 */
async function inspectTargetElement(page, selectorOrHandle, scroll = true) {
  return retryOnNavigation(page, () => inspectTargetElementOnce(page, selectorOrHandle, scroll));
}

async function inspectTargetElementOnce(page, selectorOrHandle, scroll) {
  if (!selectorOrHandle) return null;
  const isString = typeof selectorOrHandle === 'string';
  const handle = isString
    ? await currentTargetOnce(page, selectorOrHandle)
    : selectorOrHandle;
  if (!handle) return null;
  try {
    return await page.evaluate((el, doScroll) => {
    if (doScroll) {
      el.scrollIntoView({ block: 'center', inline: 'nearest' });
      let r = el.getBoundingClientRect();
      if (r.bottom > window.innerHeight - 56 || r.top < 0) {
        el.scrollIntoView({ block: 'start', inline: 'nearest' });
        r = el.getBoundingClientRect();
        if (r.bottom > window.innerHeight - 56) {
          const shift = r.bottom - (window.innerHeight - 80);
          if (shift > 0) {
            window.scrollBy(0, shift);
            let curr = el;
            while (curr && curr !== document.documentElement) {
              if (curr.scrollHeight > curr.clientHeight && curr.clientHeight > 0) {
                curr.scrollTop += shift;
              }
              curr = curr.parentElement || (typeof curr.getRootNode === 'function' ? curr.getRootNode()?.host : null);
            }
          }
        }
      }
    }

    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    let cx = Math.round(rect.x + rect.width / 2);
    let cy = Math.round(rect.y + rect.height / 2);

    let atPoint = null;
    let isTargetOrDescendant = false;
    let coveringElementDescription = '';

    const getHitAtRoot = (root, x, y) => {
      if (!root) return null;
      if (typeof root.elementsFromPoint === 'function') {
        const stack = root.elementsFromPoint(x, y) || [];
        for (const cand of stack) {
          if (!cand) continue;
          try {
            if (cand.shadowRoot) {
              const shadowHit = getHitAtRoot(cand.shadowRoot, x, y);
              if (shadowHit && window.getComputedStyle(shadowHit).pointerEvents !== 'none') return shadowHit;
            }
            if (window.getComputedStyle(cand).pointerEvents !== 'none') return cand;
          } catch (_) {
            return cand;
          }
        }
        return stack[0] || null;
      }
      if (typeof root.elementFromPoint === 'function') {
        return root.elementFromPoint(x, y);
      }
      return null;
    };

    const getDeepestHitElement = (x, y) => {
      if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return null;
      let top = getHitAtRoot(document, x, y);
      while (top && top.shadowRoot) {
        const deep = getHitAtRoot(top.shadowRoot, x, y);
        if (!deep || deep === top) break;
        top = deep;
      }
      return top;
    };

    const isHitOnTarget = (target, cand) => {
      if (!target || !cand) return false;
      if (target === cand) return true;
      if (typeof target.contains === 'function' && target.contains(cand)) return true;
      let curr = cand;
      while (curr) {
        if (curr === target) return true;
        const parent = curr.parentElement;
        if (parent) {
          curr = parent;
        } else {
          const root = typeof curr.getRootNode === 'function' ? curr.getRootNode() : null;
          curr = root && root !== curr ? root.host : null;
        }
      }
      let host = typeof target.getRootNode === 'function' ? target.getRootNode()?.host : null;
      while (host) {
        if (host === cand) return true;
        host = typeof host.getRootNode === 'function' ? host.getRootNode()?.host : null;
      }
      if (cand.classList && (cand.classList.contains('catch') || cand.classList.contains('catcher'))) {
        return true;
      }
      return false;
    };

    // A wrapped inline target (a link that breaks across lines) has a union box whose centre can fall in a
    // gap or on a neighbouring inline. The target is reachable when the union centre or the centre of any
    // of its own line boxes lands on it. A real overlay covers every one of them and still fails.
    const centres = [[cx, cy]];
    for (const cr of Array.from(el.getClientRects())) {
      if (cr.width > 0 && cr.height > 0) centres.push([Math.round(cr.x + cr.width / 2), Math.round(cr.y + cr.height / 2)]);
    }
    let firstMiss = null;
    for (const [px, py] of centres) {
      if (!(px >= 0 && px <= window.innerWidth && py >= 0 && py <= window.innerHeight)) continue;
      const hit = getDeepestHitElement(px, py);
      if (!hit) continue;
      if (isHitOnTarget(el, hit)) {
        atPoint = hit;
        isTargetOrDescendant = true;
        cx = px;
        cy = py;
        coveringElementDescription = '';
        break;
      }
      if (!firstMiss) firstMiss = hit;
    }
    if (!isTargetOrDescendant && firstMiss) {
      atPoint = firstMiss;
      coveringElementDescription = `${atPoint.tagName.toLowerCase()}${atPoint.className ? '.' + atPoint.className.toString().trim().replace(/\\s+/g, '.') : ''}${atPoint.id ? '#' + atPoint.id : ''}`;
    }

    // Effective hit area: probe outward from the centre while elementFromPoint still lands on the target.
    // Counts padding and ::before overlays that getBoundingClientRect() does not.
    const hits = (x, y) => {
      const a = getDeepestHitElement(x, y);
      return !!a && isHitOnTarget(el, a);
    };
    const reach = (dx, dy) => {
      let n = 0;
      while (n < 60 && hits(cx + dx * (n + 1), cy + dy * (n + 1))) n++;
      return n;
    };
    const hitRect = isTargetOrDescendant
      ? { width: reach(-1, 0) + reach(1, 0) + 1, height: reach(0, -1) + reach(0, 1) + 1 }
      : null;

    return {
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height, top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right },
      hitRect,
      style: { display: style.display, visibility: style.visibility, opacity: style.opacity },
      pointElementInfo: { isTargetOrDescendant, coveringElementDescription },
      isConnected: el.isConnected
    };
    }, handle, scroll);
  } finally {
    if (isString) {
      await handle.dispose().catch(() => {});
    }
  }
}

/**
 * Inspects page horizontal scroll geometry.
 */
async function inspectPageGeometry(page) {
  return retryOnNavigation(page, () => page.evaluate(() => {
    return {
      scrollWidth: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
      innerWidth: window.innerWidth
    };
  }));
}

/**
 * Inspects currently focused input element geometry.
 */
async function inspectFocusedElement(page) {
  return retryOnNavigation(page, () => page.evaluate(() => {
    const active = document.activeElement;
    if (!active || active === document.body) return null;
    const rect = active.getBoundingClientRect();
    return {
      tagName: active.tagName.toLowerCase(),
      rect: { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right, width: rect.width, height: rect.height }
    };
  }));
}

/** Measure the served application and signed-in account before saving image bytes. */
export async function captureProductScreenshot(page, expectedSha, viewportKey, label = 'exercised') {
  const measured = await page.evaluate(async () => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const [versionResponse, sessionResponse] = await Promise.all([
        fetch('/api/version', { signal: controller.signal, cache: 'no-store' }),
        fetch('/api/auth/session', { signal: controller.signal, cache: 'no-store' })
      ]);
      if (!versionResponse.ok || !sessionResponse.ok) throw new Error('authenticated application source unavailable');
      const version = await versionResponse.json();
      const session = await sessionResponse.json();
      return {
        served_sha: [version.commit, version.sha, version.served_sha, version.git_sha, version.commitSha]
          .find(value => typeof value === 'string' && /^[0-9a-f]{40}$/i.test(value)),
        account: session?.user?.id,
        url: location.href,
        device_scale: devicePixelRatio
      };
    } finally {
      clearTimeout(timeout);
    }
  });
  if (!measured.account) throw new Error('Capture requires an authenticated application account');
  if (!verifyServedSha(measured.served_sha, expectedSha).match) throw new Error('Capture served SHA differs from expected head');
  const vp = VIEWPORTS[viewportKey];
  const captureViewport = page.viewport();
  if (!vp || captureViewport?.width !== vp.width || captureViewport?.height !== vp.height) {
    throw new Error('Capture viewport differs from requested viewport');
  }
  assertNotProduction(measured.url);
  if (!/^https?:\/\//.test(measured.url)) throw new Error('Static mockup capture refused');
  const image = await page.screenshot({ fullPage: false });
  const record = {
    label, served_sha: measured.served_sha, account: measured.account,
    viewport: viewportKey, device_scale: measured.device_scale,
    url: measured.url, sha256: crypto.createHash('sha256').update(image).digest('hex'),
    source: 'application'
  };
  return { image, record };
}

/**
 * Executes a single flow step with assertions, checks, and screenshot.
 */
export async function executeStep(page, cdpSession, step, viewportKey, theme, context = {}) {
  const { flow, baseUrl, outputDir, flowConstraints } = context;

  // 1. Validate action & safety
  const safeCheck = validateSafeAction(step, flowConstraints);
  if (!safeCheck.valid) {
    const err = new Error(safeCheck.error);
    err.name = 'ActionValidationError';
    throw err;
  }

  // 2. Validate mutation QA-prefix
  const mutCheck = validateMutationPayload(step.action, step);
  if (!mutCheck.valid) {
    const err = new Error(mutCheck.error);
    err.name = 'MutationPrefixError';
    throw err;
  }

  const checksResults = [];
  const vpConfig = VIEWPORTS[viewportKey];

  // Stamp document ID before action
  let docIdBefore = null;
  try {
    docIdBefore = await stampDocumentIdentity(page);
  } catch (_) {}

  // A `load` event counts as a navigation only when it belongs to a new document. A goto resolves at
  // domcontentloaded, so the late `load` of the page it opened can fire during the next step; that
  // document still carries its identity token. A new or unreadable document reads as null.
  const loadDocIds = [];
  const onNav = () => {
    loadDocIds.push(page.evaluate(() => window.__FLOW_QA_DOC_ID__ || null).catch(() => null));
  };
  page.once('load', onNav);

  // Inject deliberate real-DOM overlay for obstruction negative control checks
  if (step.obstruction_overlay || step.inject_overlay) {
    await page.evaluate(() => {
      let overlay = document.getElementById('__flow_qa_obstruction_overlay__');
      if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = '__flow_qa_obstruction_overlay__';
        overlay.style.position = 'fixed';
        overlay.style.top = '0';
        overlay.style.left = '0';
        overlay.style.width = '100vw';
        overlay.style.height = '100vh';
        overlay.style.zIndex = '999999';
        overlay.style.backgroundColor = 'rgba(255, 0, 0, 0.4)';
        overlay.style.pointerEvents = 'auto';
        document.body.appendChild(overlay);
      }
    });
  }
  if (step.remove_obstruction_overlay) {
    await page.evaluate(() => {
      const overlay = document.getElementById('__flow_qa_obstruction_overlay__');
      if (overlay) overlay.remove();
    });
  }

  // Pre-action target inspection
  let preInspection = null;
  if (step.selector) {
    const shouldScroll = step.action !== 'assert' || Boolean(step.scroll) || Boolean(step.scroll_into_view);
    preInspection = await inspectTargetElement(page, step.selector, shouldScroll);
  }

  // A touch-only step (a swipe, which only phone sheets answer) is skipped with a passing note on a desktop viewport.
  if (step.touch_only && !vpConfig.hasTouch) {
    page.off('load', onNav);
    return {
      flow: flow.id || 'default-flow',
      step: step.id || 'step',
      viewport: viewportKey,
      theme,
      passed: true,
      checks: [{ name: 'touch_only_skipped', passed: true, detail: 'Touch-only step; skipped on a non-touch viewport' }],
      screenshot: null
    };
  }
  // A desktop-only step (e.g. desktop close control on non-touch viewports) is skipped with a passing note on touch or mobile viewports.
  if (step.desktop_only && (vpConfig.hasTouch || vpConfig.isMobile)) {
    page.off('load', onNav);
    return {
      flow: flow.id || 'default-flow',
      step: step.id || 'step',
      viewport: viewportKey,
      theme,
      passed: true,
      checks: [{ name: 'desktop_only_skipped', passed: true, detail: 'Desktop-only step; skipped on mobile/touch viewport' }],
      screenshot: null
    };
  }


  // Optional taps and typing skip targets that do not appear for every visitor.
  if (step.optional && (step.action === 'tap' || step.action === 'type')) {
    const optTimeout = step.timeout_ms || 2500;
    const shown = await waitForTarget(page, step.selector, optTimeout);
    if (!shown) {
      page.off('load', onNav);
      return {
        flow: flow.id || 'default-flow',
        step: step.id || 'step',
        viewport: viewportKey,
        theme,
        passed: true,
        checks: [{ name: 'optional_skipped', passed: true, detail: `Optional target "${step.selector}" not present; skipped` }],
        screenshot: null
      };
    }
  }
  // On mobile viewports whose height was temporarily reduced to simulate an on-screen keyboard
  // (e.g. 390x844 during keyboard-open), restore the viewport to its configured height when
  // executing non-input actions (simulating the software keyboard closing on blur/navigation).
  // Note: for 390x420, vpConfig.height is already 420, so page.viewport()?.height === vpConfig.height
  // and this block is a no-op, preserving 390x420 throughout.
  if (page.viewport()?.height !== vpConfig.height && step.action !== 'type' && step.action !== 'keyboard-open') {
    await page.setViewport({
      width: vpConfig.width,
      height: vpConfig.height,
      isMobile: vpConfig.isMobile,
      hasTouch: vpConfig.hasTouch,
      deviceScaleFactor: vpConfig.deviceScaleFactor
    });
    await new Promise(r => setTimeout(r, 150));
  }

  // 3. Execute declarative action
  const timeoutMs = step.timeout_ms || 10000;

  switch (step.action) {
    case 'goto': {
      let stepUrl = step.url;
      if (!stepUrl && step.url_from_link) {
        // Build the target from a link on the current page, for example a run id that is only known at run time.
        const { selector, pattern, template } = step.url_from_link;
        const hrefs = await page.$$eval(selector, (els) => els.map((el) => el.getAttribute('href') || ''));
        const re = new RegExp(pattern);
        const hit = hrefs.map((h) => re.exec(h)).find(Boolean);
        if (!hit) throw new Error(`No link matching "${pattern}" under "${selector}" for goto`);
        stepUrl = template.replace(/\$(\d)/g, (_, n) => hit[Number(n)] || '');
      }
      if (!stepUrl) throw new Error('Action "goto" requires "url" or "url_from_link"');
      const targetUrl = stepUrl.startsWith('http')
        ? stepUrl
        : `${baseUrl.replace(/\/+$/, '')}/${stepUrl.replace(/^\/+/, '')}`;
      assertNotProduction(targetUrl);
      await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
      break;
    }

    case 'hover': {
      let cx;
      let cy;
      if (step.selector) {
        let hoverHandle = await firstVisibleHandle(page, step.selector, timeoutMs);
        await page.evaluate((el) => {
          if (el && typeof el.scrollIntoView === 'function') {
            el.scrollIntoView({ block: 'center', inline: 'nearest' });
          }
        }, hoverHandle);
        await new Promise((r) => setTimeout(r, 100));
        let target = await inspectTargetElement(page, hoverHandle, false);
        if (!target && step.optional) {
          break;
        }
        if (!target) throw new Error(`Target "${step.selector}" not found for hover`);
        cx = target.rect.x + target.rect.width / 2;
        cy = target.rect.y + target.rect.height / 2;
        preInspection = target;
      } else if (step.x !== undefined && step.y !== undefined) {
        cx = Number(step.x);
        cy = Number(step.y);
      } else {
        throw new Error('Action "hover" requires "selector" or "x" and "y"');
      }

      await page.mouse.move(cx, cy);

      const settleMs = step.settle_ms ?? step.wait_ms ?? 200;
      if (settleMs > 0) {
        await new Promise((r) => setTimeout(r, settleMs));
      }
      break;
    }

    case 'tap': {
      if (!step.selector) throw new Error('Action "tap" requires "selector"');
      let tapHandle = await firstVisibleHandle(page, step.selector, timeoutMs);
      await page.evaluate((el) => {
        if (el && typeof el.scrollIntoView === 'function') {
          el.scrollIntoView({ block: 'center', inline: 'nearest' });
        }
      }, tapHandle);
      await new Promise(r => setTimeout(r, 200));
      let target = await inspectTargetElement(page, tapHandle, false);
      if (target && (target.rect.y < 0 || target.rect.y + target.rect.height > (page.viewport()?.height || 844))) {
        await page.evaluate((el) => {
          if (el && typeof el.scrollIntoView === 'function') {
            el.scrollIntoView({ block: 'center', inline: 'nearest' });
          }
        }, tapHandle);
        await new Promise(r => setTimeout(r, 200));
        target = await inspectTargetElement(page, tapHandle, false);
      }
      // If element was replaced or detached during hydration, re-acquire
      if (!target || !target.isConnected || !target.rect.width || !target.rect.height) {
        const settleDeadline = Date.now() + 2000;
        while (Date.now() < settleDeadline) {
          await new Promise(r => setTimeout(r, 100));
          try {
            tapHandle = await firstVisibleHandle(page, step.selector, 500);
            target = await inspectTargetElement(page, tapHandle, false);
            if (target && target.isConnected && target.rect.width > 0 && target.rect.height > 0) {
              break;
            }
          } catch {
            // retry until deadline
          }
        }
      }

      if (step.optional && (!target || !target.isConnected || !target.rect.width || !target.rect.height)) {
        break;
      }
      if (!target) throw new Error(`Target "${step.selector}" not found for tap`);
      // The tap's own checks judge the control as it was when tapped: afterwards a dialog it opened
      // covers it, which is not a defect.
      preInspection = target;

      const cx = target.rect.x + target.rect.width / 2;
      const cy = target.rect.y + target.rect.height / 2;
      // CDP tap or mouse click
      if (vpConfig.hasTouch && cdpSession) {
        await dispatchCdpTap(cdpSession, cx, cy);
      } else {
        await page.mouse.click(cx, cy);
      }
      break;
    }

    case 'type': {
      if (!step.selector) throw new Error('Action "type" requires "selector"');
      const typeTarget = await firstVisibleHandle(page, step.selector, timeoutMs);
      const readPreview = async () => {
        if (!step.preview_regex) return null;
        return page.evaluate((src) => {
          const m = new RegExp(src).exec(document.body.innerText);
          return m ? m[1] ?? m[0] : null;
        }, step.preview_regex);
      };
      const previewBefore = await readPreview();
      await page.evaluate((el) => {
        el.scrollIntoView({ block: 'center' });
        el.focus();
        if (typeof el.select === 'function') el.select();
      }, typeTarget);
      await page.keyboard.press('Backspace');
      const textToType = step.text ?? step.value ?? '';
      if (cdpSession) {
        await dispatchCdpType(cdpSession, textToType, step.delay || 20);
      } else {
        await typeTarget.type(textToType, { delay: step.delay || 20 });
      }
      if (step.preview_regex) {
        const settle = Date.now() + 4000;
        let previewAfter = await readPreview();
        while (previewAfter === previewBefore && Date.now() < settle) {
          await new Promise((resolve) => setTimeout(resolve, 200));
          previewAfter = await readPreview();
        }
        const changed = previewBefore !== null && previewAfter !== null && previewAfter !== previewBefore;
        checksResults.push({
          name: 'preview_updates',
          passed: changed,
          detail: changed
            ? `Preview changed from "${previewBefore}" to "${previewAfter}"`
            : `Preview did not update (before "${previewBefore}", after "${previewAfter}")`
        });
      }
      break;
    }

    case 'keyboard-open': {
      // Mobile-only: a desktop viewport has no on-screen keyboard, and switching its
      // emulation flags would reload the page and discard the flow state.
      if (vpConfig.hasTouch) {
        await page.setViewport({
          width: vpConfig.width,
          height: 420,
          isMobile: vpConfig.isMobile,
          hasTouch: vpConfig.hasTouch,
          deviceScaleFactor: vpConfig.deviceScaleFactor
        });
      }
      if (step.selector) {
        const focusTarget = await firstVisibleHandle(page, step.selector, timeoutMs);
        await focusTarget.focus();
      }
      break;
    }

    case 'swipe': {
      let startX, startY, endX, endY;
      const distance = step.distance || 150;
      const direction = (step.direction || 'left').toLowerCase();

      if (step.selector) {
        await waitForTarget(page, step.selector, timeoutMs, { visible: false });
        const target = await inspectTargetElement(page, step.selector);
        if (!target) throw new Error(`Target "${step.selector}" not found for swipe`);
        startX = target.rect.x + target.rect.width / 2;
        startY = target.rect.y + target.rect.height / 2;
      } else {
        startX = step.startX || vpConfig.width / 2;
        startY = step.startY || vpConfig.height / 2;
      }

      endX = startX;
      endY = startY;

      if (direction === 'left') endX = startX - distance;
      else if (direction === 'right') endX = startX + distance;
      else if (direction === 'up') endY = startY - distance;
      else if (direction === 'down') endY = startY + distance;

      if (cdpSession && vpConfig.hasTouch) {
        await dispatchCdpSwipe(cdpSession, startX, startY, endX, endY, 12, 180);
      } else {
        // Desktop mouse drag sequence
        await page.mouse.move(startX, startY);
        await page.mouse.down();
        const dragSteps = 12;
        for (let i = 1; i <= dragSteps; i++) {
          const curX = Math.round(startX + (endX - startX) * (i / dragSteps));
          const curY = Math.round(startY + (endY - startY) * (i / dragSteps));
          await page.mouse.move(curX, curY);
          await new Promise(r => setTimeout(r, 15));
        }
        await page.mouse.up();
      }
      break;
    }

    case 'assert': {
      if (step.selector) {
        let exists = false;
        if (step.expected_present !== false) {
          const target = await waitForTarget(page, step.selector, timeoutMs, { visible: false });
          exists = !!target;
          if (target) {
            await target.dispose().catch(() => {});
          }
          if (!exists) {
            checksResults.push({
              name: 'assert_present',
              passed: false,
              detail: `Assert failed: element "${step.selector}" not present`
            });
          }
        } else {
          const deadline = Date.now() + Math.min(timeoutMs, 6000);
          let shownCand = null;
          while (Date.now() < deadline) {
            shownCand = await retryOnNavigation(page, async () => {
              const handles = await queryAll(page, step.selector);
              const pick = await pickShown(page, handles);
              for (const h of handles) {
                if (h !== pick) await h.dispose().catch(() => {});
              }
              return pick;
            });
            if (!shownCand) break;
            await shownCand.dispose().catch(() => {});
            shownCand = null;
            await new Promise((r) => setTimeout(r, 100));
          }
          const shownEl = await retryOnNavigation(page, async () => {
            const handles = await queryAll(page, step.selector);
            const pick = await pickShown(page, handles);
            for (const h of handles) {
              if (h !== pick) await h.dispose().catch(() => {});
            }
            return pick;
          });
          const absent = !shownEl;
          if (shownEl) await shownEl.dispose().catch(() => {});
          if (!absent) {
            checksResults.push({
              name: 'assert_absent',
              passed: false,
              detail: `Assert failed: element "${step.selector}" is present but should be absent`
            });
          }
        }

        if (step.expected_visible !== undefined && (exists || step.expected_present === false)) {
          const deadline = Date.now() + Math.min(timeoutMs, 6000);
          let shown = false;
          let matched = false;
          while (Date.now() < deadline) {
            const current = await currentTarget(page, step.selector);
            if (current) {
              try {
                shown = await isShown(page, current);
              } finally {
                await current.dispose().catch(() => {});
              }
            } else {
              shown = false;
            }
            if (shown === step.expected_visible) {
              matched = true;
              break;
            }
            await new Promise((r) => setTimeout(r, 100));
          }
          checksResults.push({
            name: 'assert_visible',
            passed: matched,
            detail: matched
              ? `Element "${step.selector}" visibility is ${shown}`
              : `Expected visibility ${step.expected_visible}, got ${shown}`
          });
        }

        if (step.expected_text && (exists || step.expected_present === false)) {
          const deadline = Date.now() + Math.min(timeoutMs, 6000);
          let text = '';
          let textMatches = false;
          while (Date.now() < deadline) {
            const current = await currentTarget(page, step.selector);
            if (current) {
              try {
                text = (await page.evaluate((e) => e.textContent, current)) || '';
              } finally {
                await current.dispose().catch(() => {});
              }
              if (text && text.includes(step.expected_text)) {
                textMatches = true;
                break;
              }
            }
            await new Promise((r) => setTimeout(r, 150));
          }
          checksResults.push({
            name: 'assert_text',
            passed: textMatches,
            detail: textMatches
              ? `Text contains "${step.expected_text}"`
              : `Expected text "${step.expected_text}", got "${text}"`
          });
        }
      }
      break;
    }

    case 'upload': {
      if (!step.selector || !step.file) throw new Error('Action "upload" requires "selector" and "file"');
      const input = await currentTarget(page, step.selector);
      if (!input) throw new Error(`File input "${step.selector}" not found`);
      const resolvedFile = path.resolve(step.file);
      await input.uploadFile(resolvedFile);
      break;
    }

    case 'cleanup': {
      await page.evaluate(() => {
        const overlay = document.getElementById('__flow_qa_obstruction_overlay__');
        if (overlay) overlay.remove();
      }).catch(() => {});
      if (step.selector) {
        if (step.target_text || step.only_qa_threads || step.require_qa_prefix) {
          const prefix = step.target_text || 'QA-';
          if (!prefix.startsWith('QA-')) {
            throw new Error(`Cleanup step "${step.id || 'anonymous'}" refused: target prefix "${prefix}" does not start with "QA-"`);
          }
        }
        // Give a control that a previous cleanup click opens (a confirm dialog) a moment to render.
        // `repeat` clicks again while the control is still present (one row per uploaded page);
        // `then` is the confirm control clicked after each click.
        const rounds = step.repeat || 1;
        for (let round = 0; round < rounds; round++) {
          const el = await waitForTarget(page, step.selector, round === 0 ? step.timeout_ms || 1500 : 600, { visible: false });
          if (!el) break;
          if (step.target_text) {
            const isQa = await page.evaluate((targetEl, expectedPrefix) => {
              const scope = targetEl.closest('.thread, .comment, [data-thread], [data-comment], [role="article"], tr, li') || targetEl.parentElement || targetEl;
              const text = scope.textContent || '';
              return text.includes(expectedPrefix) || text.trim().startsWith(expectedPrefix);
            }, el, step.target_text);
            if (!isQa) {
              throw new Error(`Cleanup step "${step.id || 'anonymous'}" refused to delete target: text does not match "${step.target_text}"`);
            }
          }
          await el.click();
          if (step.then) {
            const confirm = await waitForTarget(page, step.then, 3000, { visible: false });
            if (confirm) await confirm.click();
            await new Promise((resolve) => setTimeout(resolve, 1200));
          }
        }
      }
      break;
    }

    default:
      throw new Error(`Unsupported action "${step.action}"`);
  }

  // Small stabilization delay
  await new Promise(r => setTimeout(r, 60));
  // A swiped sheet animates out; give it up to 1.5 s before judging whether it was dismissed.
  if (step.action === 'swipe' && step.assert_dismissal && step.selector) {
    const settleDeadline = Date.now() + 1500;
    while (Date.now() < settleDeadline && (await inspectTargetElement(page, step.selector))) {
      await new Promise((r) => setTimeout(r, 100));
    }
  }

  // Remove navigation listener
  page.off('load', onNav);

  // Post-action inspections
  const shouldScrollPost = step.action !== 'assert' || Boolean(step.scroll) || Boolean(step.scroll_into_view);
  const postInspection = step.selector ? await inspectTargetElement(page, step.selector, shouldScrollPost) : null;
  const geom = await inspectPageGeometry(page);
  let docIdAfter = null;
  try {
    docIdAfter = await page.evaluate(() => window.__FLOW_QA_DOC_ID__);
  } catch (_) {}

  // 4. Run requested or implied checks
  const requestedChecks = Array.isArray(step.checks) ? step.checks : [];

  // Check: visible
  const atTapTime = step.action === 'tap' ? preInspection || postInspection : postInspection || preInspection;
  if (step.selector && (requestedChecks.includes('visible') || (step.action === 'tap' && !step.optional))) {
    const insp = atTapTime;
    checksResults.push(checkVisible(insp?.rect, insp?.style));
  }

  // Check: element_from_point (covered fails)
  if (requestedChecks.includes('element_from_point') || requestedChecks.includes('covered')) {
    const insp = atTapTime;
    checksResults.push(checkCovered(insp?.rect, insp?.pointElementInfo));
  }

  // Check: tap_target_min_44
  if (vpConfig.isMobile && (requestedChecks.includes('tap_target_min_44') || requestedChecks.includes('target_min_44') || (step.action === 'tap' && !step.optional))) {
    const insp = preInspection || postInspection;
    checksResults.push(checkTapTargetMin44(insp?.rect || insp?.hitRect));
  }

  // Check: no_horizontal_overflow
  if (requestedChecks.includes('no_horizontal_overflow') || vpConfig.isMobile) {
    checksResults.push(checkNoHorizontalOverflow(geom.scrollWidth, geom.innerWidth));
  }

  // Check: input_focus_in_viewport
  if (requestedChecks.includes('input_focus_in_viewport') || step.action === 'keyboard-open') {
    const focused = await inspectFocusedElement(page);
    checksResults.push(checkInputFocusInViewport(focused?.rect, vpConfig.height, vpConfig.width));
  }

  // Check: no_document_reload
  if (requestedChecks.includes('no_document_reload') || step.no_document_reload) {
    const navObserved = (await Promise.all(loadDocIds)).some((id) => id !== docIdBefore);
    checksResults.push(checkNoDocumentReload(docIdBefore, docIdAfter, navObserved));
  }

  // Check: url_matches (regex on pathname+search after the step; waits for SPA navigation)
  if (step.expected_url) {
    const re = String(step.expected_url);
    const matched = await page
      .waitForFunction((r) => new RegExp(r).test(location.pathname + location.search), { timeout: step.timeout_ms || 8000 }, re)
      .then(() => true)
      .catch(() => false);
    const now = await page.evaluate(() => location.pathname + location.search);
    checksResults.push({
      name: 'url_matches',
      passed: matched,
      detail: matched ? `URL "${now}" matches /${re}/` : `URL "${now}" does not match /${re}/`
    });
  }

  // Check: clear_of_sticky_header (target top is not under a fixed/sticky header at the top)
  if (requestedChecks.includes('clear_of_sticky_header') && step.selector) {
    await new Promise((r) => setTimeout(r, 1200));
    const res = await page.evaluate((sel) => {
      const el = document.querySelector(sel);
      if (!el) return { missing: true };
      const top = el.getBoundingClientRect().top;
      let bottom = 0;
      for (const h of document.querySelectorAll('header, nav, [role="banner"]')) {
        const cs = getComputedStyle(h);
        if (cs.position !== 'fixed' && cs.position !== 'sticky') continue;
        const r = h.getBoundingClientRect();
        if (r.top <= 1 && r.bottom > 0 && r.width > innerWidth * 0.5) bottom = Math.max(bottom, r.bottom);
      }
      return { top, bottom };
    }, step.selector);
    const ok = !res.missing && res.top >= res.bottom - 1;
    checksResults.push({
      name: 'clear_of_sticky_header',
      passed: ok,
      detail: res.missing ? `Target "${step.selector}" not found` : `target top ${Math.round(res.top)}px, sticky header bottom ${Math.round(res.bottom)}px`
    });
  }

  // Check: dismissed (swipe dismissal)
  if (requestedChecks.includes('dismissed') || requestedChecks.includes('swipe_dismissal') || (step.action === 'swipe' && step.assert_dismissal)) {
    const afterState = {
      exists: !!postInspection?.isConnected,
      visible: postInspection?.style?.display !== 'none' && postInspection?.style?.visibility !== 'hidden',
      offScreen: postInspection ? (postInspection.rect.bottom <= 0 || postInspection.rect.top >= vpConfig.height || postInspection.rect.right <= 0 || postInspection.rect.left >= vpConfig.width) : true
    };
    checksResults.push(checkSwipeDismissal(preInspection?.rect, afterState));
  }

  // 5. Screenshot capture
  const screenshotFileName = `${flow.id || 'flow'}-${step.id || 'step'}-${viewportKey}-${theme}.png`;
  const screenshotPath = path.join(outputDir, screenshotFileName);

  let captureRecord = null;
  try {
    if (context.project === 'shipnovo') {
      const actualViewport = page.viewport();
      const captureViewportKey = Object.keys(VIEWPORTS).find(key =>
        VIEWPORTS[key].width === actualViewport?.width && VIEWPORTS[key].height === actualViewport?.height);
      const capture = await captureProductScreenshot(page, context.expectedSha, captureViewportKey, context.captureLabel);
      fs.writeFileSync(screenshotPath, capture.image);
      captureRecord = capture.record;
      fs.writeFileSync(screenshotPath + '.capture.json', JSON.stringify(captureRecord, null, 2));
    } else {
      await page.screenshot({ path: screenshotPath, fullPage: false });
    }
  } catch (err) {
    checksResults.push({ name: 'capture_provenance', passed: false, detail: err.message });
  }

  const stepPassed = checksResults.every(c => c.passed);

  return {
    flow: flow.id || 'default-flow',
    step: step.id || 'step',
    viewport: viewportKey,
    theme,
    passed: stepPassed,
    checks: checksResults,
    capture: captureRecord,
    screenshot: screenshotFileName
  };
}

// ============================================================================
// 7. Full Flow Execution & Report Generation
// ============================================================================

/**
 * Loads flow JSON file.
 */
export function loadFlowData(flowFilePath) {
  if (!fs.existsSync(flowFilePath)) {
    throw new Error(`Flow data file not found: ${flowFilePath}`);
  }
  const raw = fs.readFileSync(flowFilePath, 'utf8');
  const data = JSON.parse(raw);
  const flows = Array.isArray(data.flows) ? data.flows : (Array.isArray(data) ? data : [data]);
  return flows;
}

/**
 * Opens a page for the flows with the cookies of the storage state. Native dialogs (`confirm`,
 * `alert`) are accepted the way a user confirms them: an open dialog blocks every CDP call on the
 * page, so one left open stalls the run.
 */
export async function openFlowPage(browser, storageState = null) {
  const page = await browser.newPage();
  const cdpSession = await page.createCDPSession();
  page.on('dialog', (dialog) => { dialog.accept().catch(() => {}); });
  page.on('response', (res) => {
    if (res.url().includes('products')) {
      console.log(`[HTTP_RESP] ${res.status()} ${res.request().method()} ${res.url()}`);
    }
  });
  if (storageState && fs.existsSync(storageState)) {
    const stateContent = JSON.parse(fs.readFileSync(storageState, 'utf8'));
    if (Array.isArray(stateContent.cookies) && stateContent.cookies.length > 0) {
      await page.setCookie(...stateContent.cookies);
    }
    if (Array.isArray(stateContent.origins) && stateContent.origins.length > 0) {
      await page.evaluateOnNewDocument((origins) => {
        for (const entry of origins) {
          if (Array.isArray(entry.localStorage)) {
            for (const item of entry.localStorage) {
              try {
                localStorage.setItem(item.name, item.value);
              } catch (_) {}
            }
          }
        }
      }, stateContent.origins);
    }
  }
  return { page, cdpSession };
}

async function applyViewport(page, vp, theme) {
  await page.setViewport({
    width: vp.width,
    height: vp.height,
    isMobile: vp.isMobile,
    hasTouch: vp.hasTouch,
    deviceScaleFactor: vp.deviceScaleFactor
  });
  try {
    await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
  } catch (_) {}
}

/**
 * Sets viewport and colour scheme for the next run. When a step closed or broke the page, the run
 * continues on a fresh page with the same cookies instead of failing every remaining viewport.
 */
export async function prepareFlowPage(browser, current, storageState, vp, theme) {
  try {
    if (current.page.isClosed()) throw new Error('page closed');
    await applyViewport(current.page, vp, theme);
    return current;
  } catch (_) {
    await current.page.close().catch(() => {});
    const fresh = await openFlowPage(browser, storageState);
    await applyViewport(fresh.page, vp, theme);
    return fresh;
  }
}

/**
 * Executes flow QA suite and generates flow-qa/v1 report.
 */
export async function runFlows(options = {}) {
  if (!['before', 'after', 'exercised'].includes(options.captureLabel || 'exercised')) {
    throw new Error('Capture label must be before, after, or exercised');
  }
  const {
    project = 'shipnovo',
    baseUrl = 'http://localhost:3000',
    expectedSha = '',
    storageState = null,
    outputDir = './output',
    flowId = null,
    captureLabel = 'exercised',
    flowDataPath = null,
    viewports = ['390x844', '390x420', '1440x900'],
    themes = ['light', 'dark'],
    executablePath = null,
    headless = true
  } = options;

  // 1. Runtime Safety Check (Strict Refusal of PolySimulator production)
  assertNotProduction(baseUrl);

  // 2. Validate requested viewports and themes
  if (!Array.isArray(viewports) || viewports.length === 0) {
    throw new Error('No viewports specified: viewports must be a non-empty array');
  }
  const unsupportedViewports = viewports.filter(v => !VIEWPORTS[v]);
  if (unsupportedViewports.length > 0) {
    throw new Error(`Unsupported viewport(s): ${unsupportedViewports.join(', ')}. Supported viewports: ${Object.keys(VIEWPORTS).join(', ')}`);
  }
  if (!Array.isArray(themes) || themes.length === 0) {
    throw new Error('No themes specified: themes must be a non-empty array');
  }
  const unsupportedThemes = themes.filter(t => !THEMES.includes(t));
  if (unsupportedThemes.length > 0) {
    throw new Error(`Unsupported theme(s): ${unsupportedThemes.join(', ')}. Supported themes: ${THEMES.join(', ')}`);
  }

  // 3. Prepare output directory
  fs.mkdirSync(outputDir, { recursive: true });

  // 4. Verify Served SHA before actions
  const versionCheck = await checkVersionEndpoint(baseUrl, expectedSha);
  if (!versionCheck.passed) {
    const failedReport = {
      schema: SCHEMA_VERSION,
      project,
      served_sha: versionCheck.served_sha || 'unknown',
      expected_sha: expectedSha || 'unknown',
      passed: false,
      assertions: { passed: 0, failed: 1 },
      viewports: [],
      steps: [],
      cleanup: { passed: true, steps: [] },
      error: `Served SHA check failed: ${versionCheck.detail}`
    };
    fs.writeFileSync(path.join(outputDir, 'report.json'), JSON.stringify(failedReport, null, 2), 'utf8');
    return failedReport;
  }

  // 4. Load flow definitions
  const resolvedFlowPath = flowDataPath || path.join(path.dirname(fileURLToPath(import.meta.url)), 'flows', `${project}.json`);
  const source = {
    runner: crypto.createHash('sha256').update(fs.readFileSync(fileURLToPath(import.meta.url))).digest('hex'),
    flow: crypto.createHash('sha256').update(fs.readFileSync(resolvedFlowPath)).digest('hex'),
    project
  };
  const allFlows = loadFlowData(resolvedFlowPath);
  const flowsToRun = (options.flows && options.flows.length > 0)
    ? allFlows.filter(f => options.flows.includes(f.id))
    : (flowId ? allFlows.filter(f => f.id === flowId) : allFlows);

  if (flowsToRun.length === 0) {
    throw new Error(`No matching flows found (filter: "${flowId || 'all'}") in ${resolvedFlowPath}`);
  }

  // 5. Launch Puppeteer
  const puppeteer = resolvePuppeteer();
  const chromePath = executablePath || resolveExecutablePath();

  // CDP calls have no per-call timeout: on a loaded host a slow call is not a UI failure. The
  // `build_slot.py run --timeout` around the runner bounds the whole run.
  const browser = await puppeteer.launch({
    protocolTimeout: 0,
    executablePath: chromePath,
    headless: headless ? 'new' : false,
    args: [
      '--no-sandbox',
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
      '--disable-gpu',
      '--log-level=3'
    ]
  });

  const allStepReports = [];
  const allCleanupReports = [];
  const executedViewports = new Set();
  const executedThemes = new Set();
  let cleanupPassed = true;
  let totalAssertionsPassed = 0;
  let totalAssertionsFailed = 0;
  try {
    let current = await openFlowPage(browser, storageState);

    for (const flow of flowsToRun) {
      const flowConstraints = {
        allowed_actions: flow.allowed_actions,
        forbidden_actions: flow.forbidden_actions
      };

      for (const vpKey of viewports) {
        const vp = VIEWPORTS[vpKey];
        if (!vp) continue;

        for (const theme of themes) {
          executedViewports.add(vpKey);
          executedThemes.add(theme);
          current = await prepareFlowPage(browser, current, storageState, vp, theme);
          const { page, cdpSession } = current;
          const steps = Array.isArray(flow.steps) ? flow.steps : [];
          for (const step of steps) {
            let stepResult;
            let stepErrored = false;
            try {
              stepResult = await executeStep(page, cdpSession, step, vpKey, theme, {
                flow,
                baseUrl,
                outputDir,
                project, expectedSha, captureLabel,
                flowConstraints
              });
            } catch (stepErr) {
              // A flow bug or a safety refusal stops the run. A missing control or a timeout is a
              // real UI result: record it as a failed check, skip the rest of this flow and
              // viewport, and still run the cleanup below.
              if (['ActionValidationError', 'MutationPrefixError', 'ProductionForbiddenError'].includes(stepErr.name)) throw stepErr;
              stepErrored = true;
              stepResult = {
                flow: flow.id || 'default-flow',
                step: step.id || 'step',
                viewport: vpKey,
                theme,
                passed: false,
                checks: [{ name: 'step_error', passed: false, detail: stepErr.message }],
                screenshot: null
              };
            }

            allStepReports.push(stepResult);

            for (const chk of stepResult.checks) {
              if (chk.name?.endsWith('_skipped')) continue;
              if (chk.passed) totalAssertionsPassed++;
              else totalAssertionsFailed++;
            }
            if (stepErrored) break;
          }

          // Execute cleanup steps (fail-closed obligation)
          const cleanupSteps = Array.isArray(flow.cleanup) ? flow.cleanup : [];
          for (const cStep of cleanupSteps) {
            let cStepResult;
            try {
              cStepResult = await executeStep(page, cdpSession, cStep, vpKey, theme, {
                flow,
                baseUrl,
                outputDir,
                project, expectedSha, captureLabel,
                flowConstraints
              });
            } catch (cleanupErr) {
              cleanupPassed = false;
              cStepResult = {
                flow: flow.id || 'default-flow',
                step: cStep.id || 'cleanup-step',
                viewport: vpKey,
                theme,
                passed: false,
                checks: [{ name: 'cleanup_error', passed: false, detail: cleanupErr.message }],
                screenshot: null
              };
            }

            allCleanupReports.push(cStepResult);

            if (!cStepResult.passed) {
              cleanupPassed = false;
            }

            let stepCheckCount = 0;
            for (const chk of cStepResult.checks || []) {
              if (chk.name?.endsWith('_skipped')) continue;
              stepCheckCount++;
              if (chk.passed) {
                totalAssertionsPassed++;
              } else {
                totalAssertionsFailed++;
                cleanupPassed = false;
              }
            }
            if (!cStepResult.passed && stepCheckCount === 0) {
              totalAssertionsFailed++;
              cleanupPassed = false;
            }
          }
        }
      }
    }
  } finally {
    await Promise.race([browser.close().catch(() => {}), new Promise(r => setTimeout(r, 3000))]);
  }

  const hasExecutedCoverage = executedViewports.size > 0 && allStepReports.some(step =>
    step.checks.some(check => !check.name?.endsWith('_skipped')));
  const overallPassed = totalAssertionsPassed > 0 &&
    totalAssertionsFailed === 0 &&
    cleanupPassed &&
    hasExecutedCoverage &&
    allStepReports.every(s => s.passed) &&
    allCleanupReports.every(s => s.passed);

  const report = {
    schema: SCHEMA_VERSION,
    source,
    project,
    served_sha: versionCheck.served_sha || '',
    expected_sha: expectedSha,
    passed: overallPassed,
    assertions: {
      passed: totalAssertionsPassed,
      failed: totalAssertionsFailed
    },
    viewports: Array.from(executedViewports),
    steps: allStepReports,
    cleanup: {
      passed: cleanupPassed,
      steps: allCleanupReports
    }
  };

  fs.writeFileSync(path.join(outputDir, 'report.json'), JSON.stringify(report, null, 2), 'utf8');
  return report;
}

/**
 * Renders the PR comment `github_pr_gate.py` accepts as a FLOW-QA receipt: a marker line bound to
 * the served revision, the assertion counts, and the viewports that ran.
 */
export function formatReceipt(report) {
  const served = /^[0-9a-f]{40}$/i.test(report?.served_sha || '') ? report.served_sha : '';
  let passedCount = 0;
  let failedCount = 0;
  const steps = Array.isArray(report?.steps) ? report.steps : [];
  const cleanupSteps = Array.isArray(report?.cleanup?.steps) ? report.cleanup.steps : [];
  for (const step of [...steps, ...cleanupSteps]) {
    for (const check of step.checks || []) {
      if (check.name?.endsWith('_skipped')) continue;
      if (check.passed === true) passedCount++;
      else failedCount++;
    }
  }
  const hasExecutedSteps = steps.some(step =>
    (step.checks || []).some(check => !check.name?.endsWith('_skipped')));
  const countsMatch = report?.assertions?.passed === passedCount && report?.assertions?.failed === failedCount;
  const cleanupPassed = report?.cleanup?.passed ?? true;
  const executedViewports = Array.isArray(report?.viewports) ? report.viewports : [];
  const hasValidCoverage = executedViewports.length > 0 && executedViewports.every(v => VIEWPORTS[v]);
  const source = report?.source;
  const hasSource = /^[0-9a-f]{64}$/.test(source?.runner || '') && /^[0-9a-f]{64}$/.test(source?.flow || '') &&
    /^[a-z0-9-]+$/.test(source?.project || '');

  const state = (
    report?.passed === true &&
    served &&
    served.toLowerCase() === String(report?.expected_sha || '').toLowerCase() &&
    hasSource &&
    hasExecutedSteps &&
    countsMatch &&
    passedCount > 0 &&
    failedCount === 0 &&
    cleanupPassed &&
    hasValidCoverage
  ) ? 'PASS' : 'FAIL';

  const lines = [
    `FLOW-QA: ${state}${served ? ` ${served}` : ''}`,
    `FLOW-QA-ASSERTIONS pass=${passedCount} fail=${failedCount}`,
    `FLOW-QA-VIEWPORTS ${executedViewports.join(',')}`
  ];
  if (hasSource) lines.push(`FLOW-QA-SOURCE runner=${source.runner} flow=${source.flow} project=${source.project}`);
  for (const step of [...(report?.steps || []), ...(report?.cleanup?.steps || [])]) {
    if (step.capture) lines.push(`CAPTURE ${JSON.stringify(step.capture)}`);
  }
  if (report?.error) lines.push(`Error: ${report.error}`);
  return lines.join('\n') + '\n';
}

// ============================================================================
// 8. CLI Entrypoint
// ============================================================================

export function parseCliArgs(argv) {
  const options = {
    project: 'shipnovo',
    baseUrl: 'http://localhost:3000',
    expectedSha: '',
    storageState: null,
    outputDir: './output',
    flowId: null,
    headless: true
  };

  for (let i = 2; i < argv.length; i++) {
    const arg = argv[i];
    if (arg === '--project' && argv[i + 1]) options.project = argv[++i];
    else if (arg === '--base-url' && argv[i + 1]) options.baseUrl = argv[++i];
    else if (arg === '--expected-sha' && argv[i + 1]) options.expectedSha = argv[++i];
    else if (arg === '--storage-state' && argv[i + 1]) options.storageState = argv[++i];
    else if (arg === '--output' && argv[i + 1]) options.outputDir = argv[++i];
    else if (arg === '--capture-label' && argv[i + 1]) options.captureLabel = argv[++i];
    else if (arg === '--bind-sha' && argv[i + 1]) options.bindSha = argv[++i];
    else if (arg === '--flow' && argv[i + 1]) options.flowId = argv[++i];
    else if (arg === '--flows' && argv[i + 1]) options.flows = argv[++i].split(',');
    else if (arg === '--executable-path' && argv[i + 1]) options.executablePath = argv[++i];
    else if (arg === '--headless' && argv[i + 1]) options.headless = argv[++i] !== 'false';
    else if (arg === '--viewports' && argv[i + 1]) options.viewports = argv[++i].split(',');
    else if (arg === '--themes' && argv[i + 1]) options.themes = argv[++i].split(',');
    else if (arg === '--flow-data-path' && argv[i + 1]) options.flowDataPath = argv[++i];
  }

  return options;
}

// Run if directly executed
if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  const cliOptions = parseCliArgs(process.argv);
  runFlows(cliOptions)
    .then(report => {
      console.log(`Flow QA Complete: ${report.passed ? 'PASSED' : 'FAILED'} (${report.assertions.passed} passed, ${report.assertions.failed} failed)`);
      const receipt = formatReceipt(report);
      fs.writeFileSync(path.join(cliOptions.outputDir, 'receipt.txt'), receipt, 'utf8');
      console.log(receipt);
      process.exit(report.passed ? 0 : 1);
    })
    .catch(err => {
      console.error('Fatal Flow QA Runner Error:', err.message);
      process.exit(1);
    });
}
