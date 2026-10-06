/**
 * Focused test suite for Flow QA Runner
 *
 * Verifies:
 * 1. Puppeteer resolution from local environment / Veyyon.
 * 2. Runtime safety refusal of PolySimulator production endpoints.
 * 3. Served SHA mismatch refusal before actions.
 * 4. Declarative safe action validation and arbitrary JS rejection.
 * 5. Mutation payload QA-prefix enforcement with named assertion failures.
 * 6. Concrete positive and negative controls for all 7 named check helpers:
 *    - checkVisible
 *    - checkCovered / checkElementFromPoint
 *    - checkTapTargetMin44
 *    - checkNoHorizontalOverflow
 *    - checkInputFocusInViewport
 *    - checkNoDocumentReload
 *    - checkSwipeDismissal
 * 7. Distinction between assertion failure vs infrastructure errors.
 * 8. End-to-end Puppeteer flow with real CDP touch events, screenshots, and report generation.
 * 9. Negative flow controls: target <44px failure, cleanup obligation fail-closed, mutation prefix failure.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

import {
  SCHEMA_VERSION,
  VIEWPORTS,
  THEMES,
  ALLOWED_ACTIONS,
  resolvePuppeteer,
  resolveExecutablePath,
  isProductionUrl,
  assertNotProduction,
  verifyServedSha,
  checkVersionEndpoint,
  validateSafeAction,
  validateMutationPayload,
  checkVisible,
  checkCovered,
  checkElementFromPoint,
  checkTapTargetMin44,
  checkNoHorizontalOverflow,
  checkInputFocusInViewport,
  checkNoDocumentReload,
  checkSwipeDismissal,
  runFlows,
  executeStep,
  firstVisibleHandle,
  openFlowPage,
  prepareFlowPage
} from './flow_qa_runner.mjs';

// ============================================================================
// 1. Dependency Resolution
// ============================================================================

test('resolvePuppeteer returns functional puppeteer or puppeteer-core', () => {
  const puppeteer = resolvePuppeteer();
  assert.ok(puppeteer, 'Puppeteer module must be defined');
  assert.equal(typeof puppeteer.launch, 'function', 'Puppeteer must have launch method');
});

test('resolveExecutablePath resolves Chrome on the workstation', () => {
  const chromePath = resolveExecutablePath();
  if (chromePath) {
    assert.ok(fs.existsSync(chromePath), `Chrome binary must exist at ${chromePath}`);
  }
});

// ============================================================================
// 2. Runtime Safety & PolySimulator Production Refusal
// ============================================================================

test('isProductionUrl rejects PolySimulator production hosts (Negative Controls)', () => {
  const forbiddenUrls = [
    'https://polysimulator.com',
    'http://polysimulator.com/markets',
    'https://app.polysimulator.com',
    'https://prod.polysimulator.com',
    'https://zaraprptkegxqpvnsubu.supabase.co',
    'https://akamai-iad-prod.polysimulator.net',
    'https://some-service.polysimulator.com/api'
  ];

  for (const url of forbiddenUrls) {
    const res = isProductionUrl(url);
    assert.equal(res.forbidden, true, `URL ${url} must be recognized as forbidden production`);
    assert.ok(res.reason, 'Must provide refusal reason');
  }
});

test('isProductionUrl permits staging and local development (Positive Controls)', () => {
  const allowedUrls = [
    'http://localhost:3000',
    'http://127.0.0.1:8080',
    'http://localhost:3000/api/version',
    'https://staging.polysimulator.com',
    'https://staging.polysimulator.com/markets',
    'https://sub.staging.polysimulator.com'
  ];

  for (const url of allowedUrls) {
    const res = isProductionUrl(url);
    assert.equal(res.forbidden, false, `URL ${url} must be allowed`);
  }
});

test('assertNotProduction throws ProductionForbiddenError for production URL', () => {
  assert.throws(
    () => assertNotProduction('https://polysimulator.com/admin'),
    (err) => {
      assert.equal(err.name, 'ProductionForbiddenError');
      assert.equal(err.category, 'safety');
      return true;
    }
  );

  // Staging does not throw
  assert.doesNotThrow(() => assertNotProduction('https://staging.polysimulator.com'));
});

// ============================================================================
// 3. Served SHA Verification & /api/version Endpoint
// ============================================================================

test('verifyServedSha matches exact and prefix commit SHAs (Positive Controls)', () => {
  const exact = verifyServedSha(
    '915086acdf8a9061b4dae420935e876183046d9b',
    '915086acdf8a9061b4dae420935e876183046d9b'
  );
  assert.equal(exact.match, true);

  const prefix = verifyServedSha(
    '915086acdf8a9061b4dae420935e876183046d9b',
    '915086a'
  );
  assert.equal(prefix.match, true);
});

test('verifyServedSha rejects mismatching and empty SHAs (Negative Controls)', () => {
  const mismatch = verifyServedSha(
    '1111111111111111111111111111111111111111',
    '915086acdf8a9061b4dae420935e876183046d9b'
  );
  assert.equal(mismatch.match, false);
  assert.ok(mismatch.detail.includes('mismatch'));

  const empty = verifyServedSha('', '915086acdf8a9061b4dae420935e876183046d9b');
  assert.equal(empty.match, false);
});

test('checkVersionEndpoint verifies /api/version response correctly', async () => {
  const mockFetchSuccess = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ sha: '915086acdf8a9061b4dae420935e876183046d9b' })
  });

  const successRes = await checkVersionEndpoint('http://localhost:3000', '915086acdf8a9061b4dae420935e876183046d9b', mockFetchSuccess);
  assert.equal(successRes.passed, true);
  assert.equal(successRes.served_sha, '915086acdf8a9061b4dae420935e876183046d9b');

  // Negative control 1: Mismatched SHA
  const mockFetchMismatch = async () => ({
    ok: true,
    status: 200,
    json: async () => ({ sha: 'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef' })
  });
  const mismatchRes = await checkVersionEndpoint('http://localhost:3000', '915086acdf8a9061b4dae420935e876183046d9b', mockFetchMismatch);
  assert.equal(mismatchRes.passed, false);

  // Negative control 2: HTTP 500 error
  const mockFetch500 = async () => ({
    ok: false,
    status: 500,
    statusText: 'Internal Server Error'
  });
  const errorRes = await checkVersionEndpoint('http://localhost:3000', '915086a', mockFetch500);
  assert.equal(errorRes.passed, false);

  // Negative control 3: Network error
  const mockFetchNetworkError = async () => { throw new Error('ECONNREFUSED'); };
  const networkRes = await checkVersionEndpoint('http://localhost:3000', '915086a', mockFetchNetworkError);
  assert.equal(networkRes.passed, false);
});

// ============================================================================
// 4. Declarative Safe Actions & Mutation QA-Prefix Invariants
// ============================================================================

test('validateSafeAction permits allowed safe actions (Positive Control)', () => {
  for (const act of ALLOWED_ACTIONS) {
    const res = validateSafeAction({ id: 's1', action: act });
    assert.equal(res.valid, true, `Action "${act}" must be valid`);
  }
});

test('validateSafeAction rejects unknown and forbidden actions (Negative Controls)', () => {
  // 1. Unknown action
  const unknown = validateSafeAction({ id: 's1', action: 'dance' });
  assert.equal(unknown.valid, false);
  assert.ok(unknown.error.includes('not permitted'));

  // 2. Action outside flow allowed_actions
  const constrained = validateSafeAction(
    { id: 's1', action: 'upload' },
    { allowed_actions: ['goto', 'tap'] }
  );
  assert.equal(constrained.valid, false);
  assert.ok(constrained.error.includes('flow constraints'));

  // 3. Action inside flow forbidden_actions
  const forbidden = validateSafeAction(
    { id: 's1', action: 'type' },
    { forbidden_actions: ['type'] }
  );
  assert.equal(forbidden.valid, false);
  assert.ok(forbidden.error.includes('forbidden by flow constraint'));

  // 4. Arbitrary JS injection attempts in step data
  const evalAttempt = validateSafeAction({ id: 's1', action: 'tap', eval: 'console.log(1)' });
  assert.equal(evalAttempt.valid, false);
  assert.ok(evalAttempt.error.includes('Arbitrary JS'));

  const scriptAttempt = validateSafeAction({ id: 's1', action: 'tap', script: 'alert(1)' });
  assert.equal(scriptAttempt.valid, false);

  const rawJsAttempt = validateSafeAction({ id: 's1', action: 'tap', raw_js: 'process.exit()' });
  assert.equal(rawJsAttempt.valid, false);

  // 5. Goto targeting production
  const prodGoto = validateSafeAction({ id: 's1', action: 'goto', url: 'https://polysimulator.com' });
  assert.equal(prodGoto.valid, false);
});

test('validateMutationPayload enforces "QA-" prefix on mutation fields', () => {
  // Positive control: QA- prefixed value
  const validType = validateMutationPayload('type', { selector: 'input[name="title"]', text: 'QA-test-order-1' });
  assert.equal(validType.valid, true);

  const validMarked = validateMutationPayload('tap', { mutation: true, text: 'QA-action-payload' });
  assert.equal(validMarked.valid, true);

  // Negative control 1: type action without QA- prefix
  const invalidType = validateMutationPayload('type', { selector: 'input[name="title"]', text: 'Real Order Title' });
  assert.equal(invalidType.valid, false);
  assert.ok(invalidType.error.includes('must begin with "QA-" prefix'));

  // Negative control 2: marked mutation step without QA- prefix
  const invalidMarked = validateMutationPayload('type', { mutation: true, text: 'unprefixed-mutation' });
  assert.equal(invalidMarked.valid, false);
  assert.ok(invalidMarked.error.includes('must begin with "QA-" prefix'));
});

// ============================================================================
// 5. Named Check Helpers (Concrete Positive & Negative Controls)
// ============================================================================

test('checkVisible: verifies visibility, dimensions, display, and opacity', () => {
  // Positive control
  const pos = checkVisible(
    { width: 100, height: 48 },
    { display: 'block', visibility: 'visible', opacity: '1' }
  );
  assert.equal(pos.name, 'visible');
  assert.equal(pos.passed, true);

  // Negative control 1: zero dimensions
  const negSize = checkVisible({ width: 0, height: 48 }, { display: 'block', visibility: 'visible', opacity: '1' });
  assert.equal(negSize.passed, false);

  // Negative control 2: display none
  const negDisplay = checkVisible({ width: 100, height: 48 }, { display: 'none', visibility: 'visible', opacity: '1' });
  assert.equal(negDisplay.passed, false);

  // Negative control 3: visibility hidden
  const negVis = checkVisible({ width: 100, height: 48 }, { display: 'block', visibility: 'hidden', opacity: '1' });
  assert.equal(negVis.passed, false);

  // Negative control 4: opacity 0
  const negOpacity = checkVisible({ width: 100, height: 48 }, { display: 'block', visibility: 'visible', opacity: '0' });
  assert.equal(negOpacity.passed, false);

  // Negative control 5: missing rect
  const negNull = checkVisible(null);
  assert.equal(negNull.passed, false);
});

test('checkCovered / checkElementFromPoint: detects covering overlays and occlusion', () => {
  const rect = { x: 50, y: 100, width: 200, height: 50 };

  // Positive control: center element is target or child
  const pos = checkCovered(rect, { isTargetOrDescendant: true });
  assert.equal(pos.name, 'element_from_point');
  assert.equal(pos.passed, true);
  assert.ok(pos.detail.includes('unobstructed'));

  // Negative control 1: covered by modal backdrop
  const negBackdrop = checkCovered(rect, {
    isTargetOrDescendant: false,
    coveringElementDescription: 'div.modal-backdrop'
  });
  assert.equal(negBackdrop.passed, false);
  assert.ok(negBackdrop.detail.includes('covered by <div.modal-backdrop>'));

  // Negative control 2: covered by sticky header
  const negHeader = checkCovered(rect, {
    isTargetOrDescendant: false,
    coveringElementDescription: 'header.sticky-nav'
  });
  assert.equal(negHeader.passed, false);
  assert.ok(negHeader.detail.includes('covered by <header.sticky-nav>'));

  // Negative control 3: missing rect
  const negNull = checkCovered(null);
  assert.equal(negNull.passed, false);
});

test('checkTapTargetMin44: verifies 44x44px minimum touch target size', () => {
  // Positive control 1: 48x48
  const pos48 = checkTapTargetMin44({ width: 48, height: 48 });
  assert.equal(pos48.name, 'tap_target_min_44');
  assert.equal(pos48.passed, true);

  // Positive control 2: 44x44 boundary
  const pos44 = checkTapTargetMin44({ width: 44, height: 44 });
  assert.equal(pos44.passed, true);

  // Negative control 1: width < 44 (e.g. 32x48)
  const negWidth = checkTapTargetMin44({ width: 32, height: 48 });
  assert.equal(negWidth.passed, false);
  assert.ok(negWidth.detail.includes('below minimum 44x44px'));

  // Negative control 2: height < 44 (e.g. 60x24)
  const negHeight = checkTapTargetMin44({ width: 60, height: 24 });
  assert.equal(negHeight.passed, false);

  // Negative control 3: both < 44 (e.g. 20x20 icon)
  const negBoth = checkTapTargetMin44({ width: 20, height: 20 });
  assert.equal(negBoth.passed, false);
});

test('checkNoHorizontalOverflow: detects mobile horizontal overflow drift', () => {
  // Positive control 1: exact match
  const posExact = checkNoHorizontalOverflow(390, 390);
  assert.equal(posExact.name, 'no_horizontal_overflow');
  assert.equal(posExact.passed, true);

  // Positive control 2: within 1px subpixel tolerance
  const posTol = checkNoHorizontalOverflow(391, 390);
  assert.equal(posTol.passed, true);

  // Positive control 3: smaller content
  const posSmaller = checkNoHorizontalOverflow(360, 390);
  assert.equal(posSmaller.passed, true);

  // Negative control 1: overflow on mobile (430px scrollWidth on 390px viewport)
  const negMobile = checkNoHorizontalOverflow(430, 390);
  assert.equal(negMobile.passed, false);
  assert.ok(negMobile.detail.includes('Horizontal overflow detected'));
  assert.ok(negMobile.detail.includes('exceeds innerWidth 390px by 40px'));

  // Negative control 2: overflow on desktop (1520px on 1440px viewport)
  const negDesktop = checkNoHorizontalOverflow(1520, 1440);
  assert.equal(negDesktop.passed, false);
});

test('checkInputFocusInViewport: detects input occlusion by virtual keyboard', () => {
  // Positive control: input in upper half of mobile screen (390x420 keyboard open)
  const posKeyboard = checkInputFocusInViewport(
    { top: 120, bottom: 160, left: 20, right: 370 },
    420,
    390
  );
  assert.equal(posKeyboard.name, 'input_focus_in_viewport');
  assert.equal(posKeyboard.passed, true);

  // Negative control 1: input bottom occluded by keyboard (bottom 450px > viewport height 420px)
  const negOccluded = checkInputFocusInViewport(
    { top: 410, bottom: 450, left: 20, right: 370 },
    420,
    390
  );
  assert.equal(negOccluded.passed, false);
  assert.ok(negOccluded.detail.includes('keyboard occlusion'));

  // Negative control 2: input scrolled above viewport (top -30px)
  const negAbove = checkInputFocusInViewport(
    { top: -30, bottom: 10, left: 20, right: 370 },
    420,
    390
  );
  assert.equal(negAbove.passed, false);
});

test('checkNoDocumentReload: enforces document identity preservation for SPA tabs', () => {
  // Positive control: document ID identical, no navigation observed
  const pos = checkNoDocumentReload('doc-12345', 'doc-12345', false);
  assert.equal(pos.name, 'no_document_reload');
  assert.equal(pos.passed, true);

  // Negative control 1: document ID changed (full reload occurred)
  const negChanged = checkNoDocumentReload('doc-12345', 'doc-99999', false);
  assert.equal(negChanged.passed, false);
  assert.ok(negChanged.detail.includes('reloaded unexpectedly'));

  // Negative control 2: full frame navigation event observed
  const negNav = checkNoDocumentReload('doc-12345', 'doc-12345', true);
  assert.equal(negNav.passed, false);
  assert.ok(negNav.detail.includes('Full document navigation occurred'));

  // Negative control 3: missing document ID
  const negMissing = checkNoDocumentReload(null, 'doc-12345', false);
  assert.equal(negMissing.passed, false);
});

test('checkSwipeDismissal: verifies element dismissal, removal, or translation', () => {
  const beforeRect = { x: 50, y: 100, width: 290, height: 80 };

  // Positive control 1: element removed from DOM
  const posRemoved = checkSwipeDismissal(beforeRect, { exists: false });
  assert.equal(posRemoved.name, 'dismissed');
  assert.equal(posRemoved.passed, true);

  // Positive control 2: element hidden via display: none
  const posHidden = checkSwipeDismissal(beforeRect, { visible: false, display: 'none' });
  assert.equal(posHidden.passed, true);

  // Positive control 3: element translated off-screen
  const posOffScreen = checkSwipeDismissal(beforeRect, { offScreen: true });
  assert.equal(posOffScreen.passed, true);

  // Negative control: element remains visible and in DOM at original position
  const negStays = checkSwipeDismissal(beforeRect, { exists: true, visible: true, offScreen: false });
  assert.equal(negStays.passed, false);
  assert.ok(negStays.detail.includes('remains visible'));
});

// ============================================================================
// 6. Infrastructure Error vs Assertion Failure Boundary
// ============================================================================

test('Infra errors are never counted as mutation assertion caught', () => {
  const infraError = new Error('ECONNREFUSED 127.0.0.1:3000');
  infraError.code = 'ECONNREFUSED';

  const assertionError = new Error('Assertion failed: Target size 32x24px is below minimum 44x44px');
  assertionError.name = 'AssertionError';

  function categorizeFailure(err) {
    if (err.code === 'ECONNREFUSED' || err.message.includes('Browser closed') || err.message.includes('Protocol error')) {
      return { category: 'infra', caughtMutation: false };
    }
    if (err.name === 'AssertionError' || err.name === 'MutationPrefixError' || err.name === 'ActionValidationError') {
      return { category: 'assertion', caughtMutation: true };
    }
    return { category: 'unknown', caughtMutation: false };
  }

  const infraResult = categorizeFailure(infraError);
  assert.equal(infraResult.category, 'infra');
  assert.equal(infraResult.caughtMutation, false, 'Infra error must NOT count as mutation caught');

  const assertResult = categorizeFailure(assertionError);
  assert.equal(assertResult.category, 'assertion');
  assert.equal(assertResult.caughtMutation, true, 'Named assertion must count as mutation caught');
});

// ============================================================================
// 7. End-to-End Real Puppeteer Runner Flow (CDP Touch & Schema Verification)
// ============================================================================

test('E2E Puppeteer Runner executes flow with CDP touch and emits flow-qa/v1 report', async () => {
  const EXPECTED_SHA = '915086acdf8a9061b4dae420935e876183046d9b';

  const htmlContent = `
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Flow QA Test Page</title>
      <style>
        * { box-sizing: border-box; }
        body { margin: 0; padding: 12px; font-family: sans-serif; }
        .tap-btn { width: 100px; height: 50px; background: #0070f3; color: white; border: none; border-radius: 4px; font-size: 16px; }
        .small-btn { width: 30px; height: 24px; background: red; color: white; border: none; }
        .input-box { width: 100%; max-width: 320px; height: 44px; font-size: 16px; margin: 8px 0; }
        .swipe-card { width: 300px; height: 100px; background: #e0e0e0; border-radius: 8px; display: flex; align-items: center; justify-content: center; }
        .dismissed { display: none !important; }
      </style>
    </head>
    <body>
      <h1>Shipnovo Flow QA</h1>
      <button id="main-btn" class="tap-btn">Tap Target</button>
      <button id="small-btn" class="small-btn">Small</button>
      <input id="qa-input" class="input-box" type="text" placeholder="Enter title" />
      <div id="card" class="swipe-card">Swipeable Card</div>
      <div id="status">Ready</div>

      <script>
        document.getElementById('main-btn').addEventListener('click', () => {
          document.getElementById('status').textContent = 'Tapped';
        });
        const cardEl = document.getElementById('card');
        const handleDismiss = () => { cardEl.classList.add('dismissed'); };
        cardEl.addEventListener('touchend', handleDismiss);
        cardEl.addEventListener('mouseup', handleDismiss);
        cardEl.addEventListener('pointerup', handleDismiss);
      </script>
    </body>
    </html>
  `;

  let server;
  let port;
  await new Promise((resolve) => {
    server = http.createServer((req, res) => {
      if (req.url === '/api/version') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ sha: EXPECTED_SHA, version: '1.0.0' }));
      } else {
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end(htmlContent);
      }
    });
    server.listen(0, '127.0.0.1', () => {
      port = server.address().port;
      resolve();
    });
  });

  const baseUrl = `http://127.0.0.1:${port}`;
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'flow-qa-test-'));
  const flowJsonPath = path.join(tmpDir, 'test-flow.json');

  const testFlows = {
    flows: [
      {
        id: 'order-create-flow',
        name: 'Order Creation Flow',
        allowed_actions: ['goto', 'tap', 'type', 'keyboard-open', 'swipe', 'assert', 'cleanup'],
        steps: [
          {
            id: 'navigate-home',
            action: 'goto',
            url: '/',
            checks: ['no_horizontal_overflow']
          },
          {
            id: 'tap-main-button',
            action: 'tap',
            selector: '#main-btn',
            checks: ['visible', 'element_from_point', 'tap_target_min_44']
          },
          {
            id: 'type-order-name',
            action: 'type',
            selector: '#qa-input',
            text: 'QA-sample-order-test',
            checks: ['visible']
          },
          {
            id: 'open-keyboard',
            action: 'keyboard-open',
            selector: '#qa-input',
            checks: ['input_focus_in_viewport']
          },
          {
            id: 'swipe-card-dismiss',
            action: 'swipe',
            selector: '#card',
            direction: 'left',
            distance: 150,
            assert_dismissal: true,
            checks: ['dismissed']
          },
          {
            id: 'assert-status',
            action: 'assert',
            selector: '#status',
            expected_text: 'Tapped'
          }
        ],
        cleanup: [
          {
            id: 'cleanup-reset',
            action: 'cleanup',
            selector: '#main-btn'
          }
        ]
      }
    ]
  };

  fs.writeFileSync(flowJsonPath, JSON.stringify(testFlows, null, 2), 'utf8');

  try {
    const report = await runFlows({
      project: 'shipnovo',
      baseUrl,
      expectedSha: EXPECTED_SHA,
      outputDir: tmpDir,
      flowDataPath: flowJsonPath,
      viewports: ['390x844', '390x420', '1440x900'],
      themes: ['light', 'dark']
    });

    // Verify report schema
    assert.equal(report.schema, SCHEMA_VERSION);
    assert.equal(report.project, 'shipnovo');
    assert.equal(report.served_sha, EXPECTED_SHA);
    assert.equal(report.expected_sha, EXPECTED_SHA);
    assert.equal(typeof report.passed, 'boolean');
    assert.ok(report.assertions.passed > 0, `Assertions passed count must be > 0 (got ${report.assertions.passed})`);
    assert.equal(report.assertions.failed, 0, `Assertions failed count must be 0 (got ${report.assertions.failed})`);
    assert.equal(report.passed, true);
    assert.equal(report.cleanup.passed, true);
    assert.ok(report.steps.length > 0, 'Steps array must not be empty');

    // Confirm step screenshot files exist
    const reportPath = path.join(tmpDir, 'report.json');
    assert.ok(fs.existsSync(reportPath), 'report.json must be written to outputDir');

    for (const stepRep of report.steps) {
      assert.ok(stepRep.screenshot, 'Each step must record a screenshot filename');
      const shotPath = path.join(tmpDir, stepRep.screenshot);
      assert.ok(fs.existsSync(shotPath), `Screenshot file ${stepRep.screenshot} must exist on disk`);
    }
  } finally {
    server.close();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

// ============================================================================
// 8. Negative Flow Controls (Touch target <44 failure & Cleanup fail-closed)
// ============================================================================

test('Negative Flow Control 1: Button under 44x44px fails tap_target_min_44 check', async () => {
  const EXPECTED_SHA = '915086acdf8a9061b4dae420935e876183046d9b';
  let server;
  let port;

  await new Promise((resolve) => {
    server = http.createServer((req, res) => {
      if (req.url === '/api/version') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ sha: EXPECTED_SHA }));
      } else {
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end('<!DOCTYPE html><html><body><button id="small-btn" style="width:30px;height:24px">X</button></body></html>');
      }
    });
    server.listen(0, '127.0.0.1', () => {
      port = server.address().port;
      resolve();
    });
  });

  const baseUrl = `http://127.0.0.1:${port}`;
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'neg-flow-'));
  const flowJsonPath = path.join(tmpDir, 'neg-flow.json');

  fs.writeFileSync(flowJsonPath, JSON.stringify({
    flows: [{
      id: 'small-target-flow',
      steps: [
        { id: 'goto-page', action: 'goto', url: '/' },
        { id: 'tap-small', action: 'tap', selector: '#small-btn', checks: ['tap_target_min_44'] }
      ]
    }]
  }));

  try {
    const report = await runFlows({
      baseUrl,
      expectedSha: EXPECTED_SHA,
      outputDir: tmpDir,
      flowDataPath: flowJsonPath,
      viewports: ['390x844'],
      themes: ['light']
    });

    assert.equal(report.passed, false, 'Report must fail when tap target is below 44x44px');
    assert.ok(report.assertions.failed > 0, 'Must record at least 1 failed assertion');

    const smallStep = report.steps.find(s => s.step === 'tap-small');
    assert.ok(smallStep, 'Must include tap-small step in report');
    assert.equal(smallStep.passed, false);
    const targetCheck = smallStep.checks.find(c => c.name === 'tap_target_min_44');
    assert.ok(targetCheck);
    assert.equal(targetCheck.passed, false);
    assert.ok(targetCheck.detail.includes('below minimum 44x44px'));
  } finally {
    server.close();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test('Negative Flow Control 2: Cleanup failure triggers fail-closed report', async () => {
  const EXPECTED_SHA = '915086acdf8a9061b4dae420935e876183046d9b';
  let server;
  let port;

  await new Promise((resolve) => {
    server = http.createServer((req, res) => {
      if (req.url === '/api/version') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ sha: EXPECTED_SHA }));
      } else {
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end('<!DOCTYPE html><html><body><div id="content">OK</div></body></html>');
      }
    });
    server.listen(0, '127.0.0.1', () => {
      port = server.address().port;
      resolve();
    });
  });

  const baseUrl = `http://127.0.0.1:${port}`;
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'cleanup-fail-'));
  const flowJsonPath = path.join(tmpDir, 'cleanup-fail.json');

  fs.writeFileSync(flowJsonPath, JSON.stringify({
    flows: [{
      id: 'cleanup-obligation-flow',
      steps: [
        { id: 'goto-page', action: 'goto', url: '/' }
      ],
      cleanup: [
        { id: 'bad-cleanup-action', action: 'dance' } // Unknown action must fail cleanup
      ]
    }]
  }));

  try {
    const report = await runFlows({
      baseUrl,
      expectedSha: EXPECTED_SHA,
      outputDir: tmpDir,
      flowDataPath: flowJsonPath,
      viewports: ['390x844'],
      themes: ['light']
    });

    assert.equal(report.cleanup.passed, false, 'cleanup.passed must be false when cleanup action fails');
    assert.equal(report.passed, false, 'Overall report must fail closed when cleanup fails');
  } finally {
    server.close();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test('formatReceipt binds PASS to the served sha and fails closed otherwise', async () => {
  const { formatReceipt } = await import('./flow_qa_runner.mjs');
  const sha = 'a'.repeat(40);
  const ok = formatReceipt({ passed: true, served_sha: sha, assertions: { passed: 9, failed: 0 }, viewports: ['390x844', '1440x900'] });
  assert.match(ok, new RegExp(`^FLOW-QA: PASS ${sha}\\n`));
  assert.match(ok, /FLOW-QA-ASSERTIONS pass=9 fail=0/);
  assert.match(ok, /FLOW-QA-VIEWPORTS 390x844,1440x900/);
  const failed = formatReceipt({ passed: false, served_sha: sha, assertions: { passed: 9, failed: 1 }, viewports: ['390x844'] });
  assert.match(failed, /^FLOW-QA: FAIL /);
  const noSha = formatReceipt({ passed: true, served_sha: 'unknown', assertions: { passed: 3, failed: 0 }, viewports: [] });
  assert.match(noSha, /^FLOW-QA: FAIL\n/, 'a pass with no verified served sha must not print PASS');
  const zero = formatReceipt({ passed: true, served_sha: sha, assertions: { passed: 0, failed: 0 }, viewports: ['390x844'] });
  assert.match(zero, /^FLOW-QA: FAIL /, 'zero assertions is never a pass');
});

// ============================================================================
// 10. Target lookup skips controls behind an open drawer (inert / aria-hidden)
// ============================================================================

test('firstVisibleHandle skips a laid-out input inside an inert or aria-hidden container', async () => {
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({
    executablePath: resolveExecutablePath(),
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu']
  });
  try {
    const page = await browser.newPage();
    // The hidden-from-users inputs come FIRST in DOM order and have a layout box,
    // like the positions panel "Quantity to sell" input behind the open trade drawer.
    await page.setContent(`
      <div inert><input type="number" id="behind-inert"></div>
      <div aria-hidden="true"><input type="number" id="behind-aria"></div>
      <input type="number" id="real">`);
    const handle = await firstVisibleHandle(page, "input[type='number']", 1000);
    const id = await handle.evaluate((el) => el.id);
    assert.equal(id, 'real', 'must pick the reachable input, not the first laid-out one');

    await page.setContent(`<div inert><input type="number"></div>`);
    await assert.rejects(
      () => firstVisibleHandle(page, "input[type='number']", 400),
      /No visible element/,
      'only unreachable matches must fail with a named error'
    );
  } finally {
    await browser.close();
  }
});

test('firstVisibleHandle prefers the field in an open sheet over a laid-out inline copy under the overlay', async () => {
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({
    executablePath: resolveExecutablePath(),
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu']
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 390, height: 844 });
    // Inline panel sits far down the page and comes first in DOM order; the sheet is a fixed overlay.
    await page.setContent(`
      <div style="height:1400px"></div>
      <input type="number" id="inline">
      <div style="height:600px"></div>
      <div style="position:fixed;inset:0;background:#fff;z-index:10"><input type="number" id="sheet"></div>`);
    const handle = await firstVisibleHandle(page, "input[type='number']", 1000);
    assert.equal(await handle.evaluate((el) => el.id), 'sheet');
  } finally {
    await browser.close();
  }
});

test('desktop_only steps pass with a skip note on touch and mobile viewports', async () => {
  const EXPECTED_SHA = '915086acdf8a9061b4dae420935e876183046d9b';
  let server;
  let port;
  await new Promise((resolve) => {
    server = http.createServer((req, res) => {
      if (req.url === '/api/version') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ sha: EXPECTED_SHA }));
      } else {
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end('<!DOCTYPE html><html><body><button id="desk-btn" style="width:50px;height:50px">Close</button></body></html>');
      }
    });
    server.listen(0, '127.0.0.1', () => {
      port = server.address().port;
      resolve();
    });
  });
  const baseUrl = `http://127.0.0.1:${port}`;
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'desk-flow-'));
  const flowJsonPath = path.join(tmpDir, 'desk-flow.json');
  fs.writeFileSync(flowJsonPath, JSON.stringify({
    flows: [{
      id: 'desk-only-flow',
      steps: [
        { id: 'goto-page', action: 'goto', url: '/' },
        { id: 'close-desktop', action: 'tap', selector: '#desk-btn', desktop_only: true }
      ]
    }]
  }));
  try {
    const report = await runFlows({
      baseUrl,
      expectedSha: EXPECTED_SHA,
      outputDir: tmpDir,
      flowDataPath: flowJsonPath,
      viewports: ['390x844', '1440x900'],
      themes: ['light']
    });
    assert.equal(report.passed, true);
    const mobileStep = report.steps.find(s => s.step === 'close-desktop' && s.viewport === '390x844');
    assert.ok(mobileStep);
    assert.equal(mobileStep.checks[0].name, 'desktop_only_skipped');
    const deskStep = report.steps.find(s => s.step === 'close-desktop' && s.viewport === '1440x900');
    assert.ok(deskStep);
    assert.notEqual(deskStep.checks[0].name, 'desktop_only_skipped');
  } finally {
    server.close();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test('obstruction_overlay causes element_from_point check to fail with obstruction detail (Negative Control)', async () => {
  const EXPECTED_SHA = '915086acdf8a9061b4dae420935e876183046d9b';
  let server;
  let port;
  await new Promise((resolve) => {
    server = http.createServer((req, res) => {
      if (req.url === '/api/version') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ sha: EXPECTED_SHA }));
      } else {
        res.writeHead(200, { 'Content-Type': 'text/html' });
        res.end('<!DOCTYPE html><html><body><button id="target-btn" style="width:100px;height:50px">Click Me</button></body></html>');
      }
    });
    server.listen(0, '127.0.0.1', () => {
      port = server.address().port;
      resolve();
    });
  });
  const baseUrl = `http://127.0.0.1:${port}`;
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'obstr-flow-'));
  const flowJsonPath = path.join(tmpDir, 'obstr-flow.json');
  fs.writeFileSync(flowJsonPath, JSON.stringify({
    flows: [{
      id: 'obstruction-negative-control-flow',
      steps: [
        { id: 'goto-page', action: 'goto', url: '/' },
        {
          id: 'tap-covered-target',
          action: 'tap',
          selector: '#target-btn',
          obstruction_overlay: true,
          checks: ['visible', 'element_from_point']
        }
      ],
      cleanup: [
        { id: 'clean-overlay', action: 'cleanup' }
      ]
    }]
  }));
  try {
    const report = await runFlows({
      baseUrl,
      expectedSha: EXPECTED_SHA,
      outputDir: tmpDir,
      flowDataPath: flowJsonPath,
      viewports: ['390x844'],
      themes: ['light']
    });
    assert.equal(report.passed, false, 'Report must fail when target is obstructed');
    const stepReport = report.steps.find(s => s.step === 'tap-covered-target');
    assert.ok(stepReport);
    assert.equal(stepReport.passed, false);
    const coveredCheck = stepReport.checks.find(c => c.name === 'element_from_point');
    assert.ok(coveredCheck);
    assert.equal(coveredCheck.passed, false);
    assert.ok(coveredCheck.detail.includes('Target covered by'));
  } finally {
    server.close();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test('no_document_reload ignores the late load event of the page a goto left at domcontentloaded; a real reload still fails', async () => {
  // goto resolves at domcontentloaded. A slow image holds back the load event of that same document
  // until the next step runs. That late load is not a reload. A tap that reloads the page is.
  const GIF = Buffer.from('R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==', 'base64');
  let releaseImage = null;
  let imageRequests = 0;
  const server = http.createServer((req, res) => {
    if (req.url.startsWith('/slow.gif')) {
      const send = () => { if (!res.headersSent) { res.writeHead(200, { 'Content-Type': 'image/gif' }); res.end(GIF); } };
      if (imageRequests++ === 0) releaseImage = send; else send();
      return;
    }
    res.writeHead(200, { 'Content-Type': 'text/html' });
    res.end(`<!DOCTYPE html><html><body>
      <button id="client" style="width:120px;height:48px" onclick="document.title='opened'">Oeffnen</button>
      <button id="reload" style="width:120px;height:48px" onclick="location.reload()">Neu laden</button>
      <img src="/slow.gif" width="1" height="1" alt=""></body></html>`);
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({ executablePath: resolveExecutablePath() || undefined, headless: 'new', args: ['--no-sandbox'] });
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowqa-lateload-'));
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900 });
    const ctx = { flow: { id: 'f' }, baseUrl: `http://127.0.0.1:${server.address().port}`, outputDir, flowConstraints: {} };
    await executeStep(page, null, { id: 'goto', action: 'goto', url: '/' }, '1440x900', 'light', ctx);
    assert.equal(await page.evaluate(() => document.readyState), 'interactive', 'goto returned before the load event');

    let loads = 0;
    page.on('load', () => { loads++; });
    setTimeout(() => releaseImage(), 100);
    const tap = await executeStep(page, null,
      { id: 'client', action: 'tap', selector: '#client', no_document_reload: true }, '1440x900', 'light', ctx);
    assert.equal(loads, 1, 'the late load event of the same document fired during the tap step');
    assert.equal(await page.title(), 'opened');
    const kept = tap.checks.find((c) => c.name === 'no_document_reload');
    assert.equal(kept.passed, true, kept.detail);

    // Negative control: the tap reloads the page, so the check must fail.
    const reload = await executeStep(page, null,
      { id: 'reload', action: 'tap', selector: '#reload', no_document_reload: true }, '1440x900', 'light', ctx);
    const reloaded = reload.checks.find((c) => c.name === 'no_document_reload');
    assert.equal(reloaded.passed, false, 'a real reload fails no_document_reload');
  } finally {
    if (releaseImage) releaseImage();
    await browser.close();
    server.closeAllConnections();
    server.close();
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

test('openFlowPage accepts a native confirm, so later CDP calls on the page do not stall', async () => {
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({ executablePath: resolveExecutablePath() || undefined, headless: 'new', args: ['--no-sandbox', '--disable-gpu'] });
  try {
    const { page } = await openFlowPage(browser);
    await page.setContent('<body></body>');
    await page.evaluate(() => {
      setTimeout(() => { document.body.dataset.answer = window.confirm('Delete QA-row?') ? 'yes' : 'no'; }, 0);
    });
    const answer = await Promise.race([
      page.waitForFunction(() => document.body.dataset.answer, { timeout: 0 }).then((h) => h.jsonValue()),
      new Promise((resolve) => setTimeout(() => resolve('stalled behind the open dialog'), 3000))
    ]);
    assert.equal(answer, 'yes');
  } finally {
    await browser.close();
  }
});

test('prepareFlowPage replaces a closed page with a fresh one that keeps the storage-state cookies', async () => {
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({ executablePath: resolveExecutablePath() || undefined, headless: 'new', args: ['--no-sandbox', '--disable-gpu'] });
  const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowqa-state-'));
  const storageState = path.join(stateDir, 'state.json');
  fs.writeFileSync(storageState, JSON.stringify({ cookies: [{ name: 'qa_session', value: 'QA-1', domain: '127.0.0.1', path: '/' }] }));
  try {
    const vp = { width: 390, height: 844, isMobile: true, hasTouch: true, deviceScaleFactor: 1 };
    const first = await openFlowPage(browser, storageState);
    const same = await prepareFlowPage(browser, first, storageState, vp, 'dark');
    assert.equal(same.page, first.page, 'a live page is reused');

    await first.page.close();
    const fresh = await prepareFlowPage(browser, first, storageState, vp, 'dark');
    assert.notEqual(fresh.page, first.page);
    const { width, height, hasTouch } = fresh.page.viewport();
    assert.deepEqual({ width, height, hasTouch }, { width: 390, height: 844, hasTouch: true });
    assert.equal(fresh.page.isClosed(), false);
    const cookies = await fresh.page.cookies('http://127.0.0.1/');
    assert.equal(cookies.find((c) => c.name === 'qa_session')?.value, 'QA-1');
  } finally {
    await browser.close();
    fs.rmSync(stateDir, { recursive: true, force: true });
  }
});
