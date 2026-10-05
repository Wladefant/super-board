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
  if (!expectedSha) {
    return {
      match: true,
      served_sha: servedSha || 'unknown',
      expected_sha: 'none',
      detail: 'No expected SHA specified; check skipped'
    };
  }

  const cleanServed = (servedSha || '').trim().toLowerCase();
  const cleanExpected = (expectedSha || '').trim().toLowerCase();

  if (!cleanServed) {
    return {
      match: false,
      served_sha: '',
      expected_sha: cleanExpected,
      detail: 'Server did not return a valid SHA'
    };
  }

  const isMatch = cleanServed === cleanExpected ||
    (cleanServed.length >= 7 && cleanExpected.startsWith(cleanServed)) ||
    (cleanExpected.length >= 7 && cleanServed.startsWith(cleanExpected));

  return {
    match: isMatch,
    served_sha: cleanServed,
    expected_sha: cleanExpected,
    detail: isMatch
      ? `Served SHA ${cleanServed} matches expected SHA ${cleanExpected}`
      : `Served SHA mismatch: got "${cleanServed}", expected "${cleanExpected}"`
  };
}

/**
 * Checks /api/version before running actions.
 */
export async function checkVersionEndpoint(baseUrl, expectedSha, fetchFn = fetch) {
  assertNotProduction(baseUrl);

  const versionUrl = `${baseUrl.replace(/\/+$/, '')}/api/version`;
  try {
    const res = await fetchFn(versionUrl, {
      method: 'GET',
      headers: { 'Accept': 'application/json' },
      signal: AbortSignal.timeout(10000)
    });

    if (!res.ok) {
      return {
        passed: false,
        served_sha: null,
        expected_sha: expectedSha,
        detail: `GET /api/version returned HTTP status ${res.status} ${res.statusText}`
      };
    }

    const data = await res.json();
    const servedSha = data.sha || data.served_sha || data.version || data.git_sha || null;
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

  await cdpSession.send('Input.dispatchTouchEvent', {
    type: 'touchStart',
    touchPoints: [{ x: roundX, y: roundY }]
  });

  await new Promise(r => setTimeout(r, 32));

  await cdpSession.send('Input.dispatchTouchEvent', {
    type: 'touchEnd',
    touchPoints: []
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
    touchPoints: [{ x: Math.round(startX), y: Math.round(startY) }]
  });

  // 2. Intermediate touch moves
  for (let i = 1; i <= steps; i++) {
    const fraction = i / steps;
    const currentX = Math.round(startX + (endX - startX) * fraction);
    const currentY = Math.round(startY + (endY - startY) * fraction);

    await new Promise(r => setTimeout(r, stepDelay));
    await cdpSession.send('Input.dispatchTouchEvent', {
      type: 'touchMove',
      touchPoints: [{ x: currentX, y: currentY }]
    });
  }

  // 3. Touch end
  await new Promise(r => setTimeout(r, stepDelay));
  await cdpSession.send('Input.dispatchTouchEvent', {
    type: 'touchEnd',
    touchPoints: []
  });
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
 * Returns the first element matching the selector that has a layout box, polling until timeoutMs.
 * A page can render the same control twice (mobile and desktop copies) with one hidden by CSS.
 */
export async function firstVisibleHandle(page, selector, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    for (const handle of await page.$$(selector)) {
      const shown = await handle.evaluate((el) => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden' && !el.closest('[inert],[aria-hidden="true"]');
      });
      if (shown) return handle;
    }
    if (Date.now() >= deadline) throw new Error(`No visible element for selector "${selector}"`);
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
}

/**
 * Evaluates in-page geometry and elementFromPoint for named checks.
 */
async function inspectTargetElement(page, selector, scroll = true) {
  if (!selector) return null;
  return page.evaluate((sel, doScroll) => {
    const all = Array.from(document.querySelectorAll(sel));
    const el = all.find((c) => {
      const r = c.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && getComputedStyle(c).visibility !== 'hidden' && !c.closest('[inert],[aria-hidden="true"]');
    }) || all[0] || null;
    if (el && doScroll) el.scrollIntoView({ block: 'center', inline: 'nearest' });
    if (!el) return null;

    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const cx = Math.round(rect.x + rect.width / 2);
    const cy = Math.round(rect.y + rect.height / 2);

    let atPoint = null;
    let isTargetOrDescendant = false;
    let coveringElementDescription = '';

    if (cx >= 0 && cx <= window.innerWidth && cy >= 0 && cy <= window.innerHeight) {
      atPoint = document.elementFromPoint(cx, cy);
      if (atPoint) {
        isTargetOrDescendant = atPoint === el || el.contains(atPoint);
        if (!isTargetOrDescendant) {
          coveringElementDescription = `${atPoint.tagName.toLowerCase()}${atPoint.className ? '.' + atPoint.className.toString().trim().replace(/\\s+/g, '.') : ''}${atPoint.id ? '#' + atPoint.id : ''}`;
        }
      }
    }

    // Effective hit area: probe outward from the centre while elementFromPoint still lands on the target.
    // Counts padding and ::before overlays that getBoundingClientRect() does not.
    const hits = (x, y) => {
      if (x < 0 || y < 0 || x >= window.innerWidth || y >= window.innerHeight) return false;
      const a = document.elementFromPoint(x, y);
      return !!a && (a === el || el.contains(a));
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
  }, selector, scroll);
}

/**
 * Inspects page horizontal scroll geometry.
 */
async function inspectPageGeometry(page) {
  return page.evaluate(() => {
    return {
      scrollWidth: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
      innerWidth: window.innerWidth
    };
  });
}

/**
 * Inspects currently focused input element geometry.
 */
async function inspectFocusedElement(page) {
  return page.evaluate(() => {
    const active = document.activeElement;
    if (!active || active === document.body) return null;
    const rect = active.getBoundingClientRect();
    return {
      tagName: active.tagName.toLowerCase(),
      rect: { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right, width: rect.width, height: rect.height }
    };
  });
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
  let navObserved = false;
  try {
    docIdBefore = await stampDocumentIdentity(page);
  } catch (_) {}

  const onNav = () => { navObserved = true; };
  page.once('load', onNav);

  // Pre-action target inspection
  let preInspection = null;
  if (step.selector) {
    preInspection = await inspectTargetElement(page, step.selector, step.action !== 'assert');
  }

  // An optional tap (for example a consent banner that only appears for new visitors) is skipped
  // with a passing note when its target never shows up.
  if (step.optional && step.action === 'tap') {
    const shown = await page.waitForSelector(step.selector, { timeout: 2500, visible: true }).catch(() => null);
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

  // 3. Execute declarative action
  const timeoutMs = step.timeout_ms || 10000;

  switch (step.action) {
    case 'goto': {
      const targetUrl = step.url.startsWith('http')
        ? step.url
        : `${baseUrl.replace(/\/+$/, '')}/${step.url.replace(/^\/+/, '')}`;
      assertNotProduction(targetUrl);
      await page.goto(targetUrl, { waitUntil: 'domcontentloaded', timeout: timeoutMs });
      break;
    }

    case 'tap': {
      if (!step.selector) throw new Error('Action "tap" requires "selector"');
      await firstVisibleHandle(page, step.selector, timeoutMs);
      const target = await inspectTargetElement(page, step.selector);
      if (!target) throw new Error(`Target "${step.selector}" not found for tap`);
      preInspection = target;

      const cx = target.rect.x + target.rect.width / 2;
      const cy = target.rect.y + target.rect.height / 2;

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
      await typeTarget.evaluate((el) => {
        el.scrollIntoView({ block: 'center' });
        el.focus();
        if (typeof el.select === 'function') el.select();
      });
      const textToType = step.text ?? step.value ?? '';
      await typeTarget.type(textToType, { delay: step.delay || 20 });
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
        await page.waitForSelector(step.selector, { timeout: timeoutMs });
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
        if (step.expected_present !== false) {
          await page.waitForSelector(step.selector, { timeout: 10000 }).catch(() => null);
        }
        const el = await page.$(step.selector);
        const exists = !!el;
        if (step.expected_present !== false && !exists) {
          checksResults.push({
            name: 'assert_present',
            passed: false,
            detail: `Assert failed: element "${step.selector}" not present`
          });
        }
        if (step.expected_present === false && exists) {
          checksResults.push({
            name: 'assert_absent',
            passed: false,
            detail: `Assert failed: element "${step.selector}" is present but should be absent`
          });
        }
        if (step.expected_text && exists) {
          const text = await page.evaluate(e => e.textContent, el);
          const textMatches = text && text.includes(step.expected_text);
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
      const input = await page.$(step.selector);
      if (!input) throw new Error(`File input "${step.selector}" not found`);
      const resolvedFile = path.resolve(step.file);
      await input.uploadFile(resolvedFile);
      break;
    }

    case 'cleanup': {
      if (step.selector) {
        const el = await page.$(step.selector);
        if (el) await el.click();
      }
      break;
    }

    default:
      throw new Error(`Unsupported action "${step.action}"`);
  }

  // Small stabilization delay
  await new Promise(r => setTimeout(r, 60));

  // Remove navigation listener
  page.off('load', onNav);

  // Post-action inspections
  const postInspection = step.selector ? await inspectTargetElement(page, step.selector, step.action !== 'assert') : null;
  const geom = await inspectPageGeometry(page);
  let docIdAfter = null;
  try {
    docIdAfter = await page.evaluate(() => window.__FLOW_QA_DOC_ID__);
  } catch (_) {}

  // 4. Run requested or implied checks
  const requestedChecks = Array.isArray(step.checks) ? step.checks : [];

  // Check: visible
  if (step.selector && (requestedChecks.includes('visible') || step.action === 'tap')) {
    const insp = postInspection || preInspection;
    checksResults.push(checkVisible(insp?.rect, insp?.style));
  }

  // Check: element_from_point (covered fails)
  if (requestedChecks.includes('element_from_point') || requestedChecks.includes('covered')) {
    const insp = postInspection || preInspection;
    checksResults.push(checkCovered(insp?.rect, insp?.pointElementInfo));
  }

  // Check: tap_target_min_44
  if (vpConfig.isMobile && (requestedChecks.includes('tap_target_min_44') || requestedChecks.includes('target_min_44') || step.action === 'tap')) {
    const insp = preInspection || postInspection;
    checksResults.push(checkTapTargetMin44(insp?.hitRect || insp?.rect));
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

  try {
    await page.screenshot({ path: screenshotPath, fullPage: false });
  } catch (err) {
    // If screenshot fails, record detail but continue
  }

  const stepPassed = checksResults.every(c => c.passed);

  return {
    flow: flow.id || 'default-flow',
    step: step.id || 'step',
    viewport: viewportKey,
    theme,
    passed: stepPassed,
    checks: checksResults,
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
 * Executes flow QA suite and generates flow-qa/v1 report.
 */
export async function runFlows(options = {}) {
  const {
    project = 'shipnovo',
    baseUrl = 'http://localhost:3000',
    expectedSha = '',
    storageState = null,
    outputDir = './output',
    flowId = null,
    flowDataPath = null,
    viewports = ['390x844', '390x420', '1440x900'],
    themes = ['light', 'dark'],
    executablePath = null,
    headless = true
  } = options;

  // 1. Runtime Safety Check (Strict Refusal of PolySimulator production)
  assertNotProduction(baseUrl);

  // 2. Prepare output directory
  fs.mkdirSync(outputDir, { recursive: true });

  // 3. Verify Served SHA before actions
  const versionCheck = await checkVersionEndpoint(baseUrl, expectedSha);
  if (!versionCheck.passed) {
    const failedReport = {
      schema: SCHEMA_VERSION,
      project,
      served_sha: versionCheck.served_sha || 'unknown',
      expected_sha: expectedSha || 'unknown',
      passed: false,
      assertions: { passed: 0, failed: 1 },
      steps: [],
      cleanup: { passed: true },
      error: `Served SHA check failed: ${versionCheck.detail}`
    };
    fs.writeFileSync(path.join(outputDir, 'report.json'), JSON.stringify(failedReport, null, 2), 'utf8');
    return failedReport;
  }

  // 4. Load flow definitions
  const resolvedFlowPath = flowDataPath || path.join(path.dirname(fileURLToPath(import.meta.url)), 'flows', `${project}.json`);
  const allFlows = loadFlowData(resolvedFlowPath);
  const flowsToRun = flowId ? allFlows.filter(f => f.id === flowId) : allFlows;

  if (flowsToRun.length === 0) {
    throw new Error(`No matching flows found (filter: "${flowId || 'all'}") in ${resolvedFlowPath}`);
  }

  // 5. Launch Puppeteer
  const puppeteer = resolvePuppeteer();
  const chromePath = executablePath || resolveExecutablePath();

  const browser = await puppeteer.launch({
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
  let cleanupPassed = true;
  let totalAssertionsPassed = 0;
  let totalAssertionsFailed = 0;

  try {
    const page = await browser.newPage();
    const cdpSession = await page.createCDPSession();

    // Load storage state if provided
    if (storageState && fs.existsSync(storageState)) {
      const stateContent = JSON.parse(fs.readFileSync(storageState, 'utf8'));
      if (Array.isArray(stateContent.cookies)) {
        await page.setCookie(...stateContent.cookies);
      }
    }

    for (const flow of flowsToRun) {
      const flowConstraints = {
        allowed_actions: flow.allowed_actions,
        forbidden_actions: flow.forbidden_actions
      };

      for (const vpKey of viewports) {
        const vp = VIEWPORTS[vpKey];
        if (!vp) continue;

        for (const theme of themes) {
          // Set viewport and theme
          await page.setViewport({
            width: vp.width,
            height: vp.height,
            isMobile: vp.isMobile,
            hasTouch: vp.hasTouch,
            deviceScaleFactor: vp.deviceScaleFactor
          });

          await page.emulateMediaFeatures([
            { name: 'prefers-color-scheme', value: theme }
          ]);

          const steps = Array.isArray(flow.steps) ? flow.steps : [];
          for (const step of steps) {
            const stepResult = await executeStep(page, cdpSession, step, vpKey, theme, {
              flow,
              baseUrl,
              outputDir,
              flowConstraints
            });

            allStepReports.push(stepResult);

            for (const chk of stepResult.checks) {
              if (chk.passed) totalAssertionsPassed++;
              else totalAssertionsFailed++;
            }
          }

          // Execute cleanup steps (fail-closed obligation)
          const cleanupSteps = Array.isArray(flow.cleanup) ? flow.cleanup : [];
          for (const cStep of cleanupSteps) {
            try {
              await executeStep(page, cdpSession, cStep, vpKey, theme, {
                flow,
                baseUrl,
                outputDir,
                flowConstraints
              });
            } catch (cleanupErr) {
              cleanupPassed = false;
              totalAssertionsFailed++;
            }
          }
        }
      }
    }
  } finally {
    await browser.close().catch(() => {});
  }

  const overallPassed = totalAssertionsFailed === 0 && cleanupPassed && allStepReports.every(s => s.passed);

  const report = {
    schema: SCHEMA_VERSION,
    project,
    served_sha: versionCheck.served_sha || expectedSha,
    expected_sha: expectedSha,
    passed: overallPassed,
    assertions: {
      passed: totalAssertionsPassed,
      failed: totalAssertionsFailed
    },
    viewports,
    steps: allStepReports,
    cleanup: {
      passed: cleanupPassed
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
  const served = /^[0-9a-f]{40}$/i.test(report.served_sha || '') ? report.served_sha : '';
  const state = report.passed && served && report.assertions.passed > 0 ? 'PASS' : 'FAIL';
  const lines = [
    `FLOW-QA: ${state}${served ? ` ${served}` : ''}`,
    `FLOW-QA-ASSERTIONS pass=${report.assertions.passed} fail=${report.assertions.failed}`,
    `FLOW-QA-VIEWPORTS ${(report.viewports || []).join(',')}`
  ];
  if (report.error) lines.push(`Error: ${report.error}`);
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
    else if (arg === '--flow' && argv[i + 1]) options.flowId = argv[++i];
    else if (arg === '--executable-path' && argv[i + 1]) options.executablePath = argv[++i];
    else if (arg === '--headless' && argv[i + 1]) options.headless = argv[++i] !== 'false';
    else if (arg === '--viewports' && argv[i + 1]) options.viewports = argv[++i].split(',');
    else if (arg === '--themes' && argv[i + 1]) options.themes = argv[++i].split(',');
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
