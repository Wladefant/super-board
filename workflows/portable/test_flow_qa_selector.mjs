/**
 * Tests for the flow selector resolver: plain CSS plus `:has-text('...')`, the one text matcher
 * flows may use. `:has-text` is not CSS, so document.querySelector rejects it; the runner must
 * resolve it itself.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';

import {
  parseSelector,
  loadFlowData,
  executeStep,
  resolvePuppeteer,
  resolveExecutablePath,
  runFlows
} from './flow_qa_runner.mjs';

test('parseSelector lifts :has-text out of each comma alternative and keeps plain CSS', () => {
  assert.deepEqual(parseSelector("button[aria-label='Artikel anlegen'], button:has-text('Artikel anlegen')"), [
    { css: "button[aria-label='Artikel anlegen']", texts: [] },
    { css: 'button', texts: ['Artikel anlegen'] }
  ]);
  assert.deepEqual(parseSelector("table tbody tr:first-child button:has-text('Bearbeiten')"), [
    { css: 'table tbody tr:first-child button', texts: ['Bearbeiten'] }
  ]);
  assert.deepEqual(parseSelector(':has-text("x")'), [{ css: '*', texts: ['x'] }]);
});

test('parseSelector does not split on commas inside quotes, brackets or text', () => {
  assert.deepEqual(parseSelector("a[title='x, y']:has-text('Hallo, Welt')"), [
    { css: "a[title='x, y']", texts: ['Hallo, Welt'] }
  ]);
});

test('parseSelector refuses a :has-text that is not on the subject element', () => {
  assert.throws(() => parseSelector("div:has-text('x') button"), /last compound/);
  assert.throws(() => parseSelector('button:has-text(x)'), /quoted string/);
  assert.throws(() => parseSelector("button:has-text('x'"), /Unterminated/);
  assert.throws(() => parseSelector('  '), /Empty selector/);
});

test('flow steps resolve :has-text in a real page (tap, assert present, assert absent)', async () => {
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({
    executablePath: resolveExecutablePath() || undefined,
    headless: 'new',
    args: ['--no-sandbox']
  });
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowqa-selector-'));
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900 });
    await page.setContent(`
      <button id="hidden-copy" style="display:none">Abbrechen</button>
      <button id="cancel" style="width:120px;height:48px"
        onclick="document.title='cancelled'">  ABBRECHEN
        jetzt </button>
      <button id="save" style="width:120px;height:48px">Speichern</button>`);
    const run = (step) => executeStep(page, null, { id: 's', ...step }, '1440x900', 'light', {
      flow: { id: 'f' },
      baseUrl: 'http://localhost',
      outputDir,
      flowConstraints: {}
    });

    const tap = await run({ action: 'tap', selector: "button:has-text('abbrechen jetzt')" });
    assert.equal(await page.title(), 'cancelled', 'taps the visible copy, matching case- and whitespace-insensitively');
    assert.equal(tap.passed, true);

    const present = await run({ action: 'assert', selector: "main, button:has-text('Speichern')", expected_present: true });
    assert.equal(present.checks.filter((c) => !c.passed).length, 0);

    const missing = await run({ action: 'assert', selector: "button:has-text('Gibt es nicht')", expected_present: true, timeout_ms: 500 });
    assert.ok(
      missing.checks.some((c) => c.name === 'assert_present' && !c.passed),
      'a text that is not on the page fails assert_present instead of passing or throwing'
    );
  } finally {
    await browser.close();
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

test('every selector in the shipped flow files parses', () => {
  const flowsDir = path.join(path.dirname(new URL(import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')), 'flows');
  const files = fs.readdirSync(flowsDir).filter((f) => f.endsWith('.json'));
  assert.ok(files.length >= 2);
  let seen = 0;
  for (const file of files) {
    for (const flow of loadFlowData(path.join(flowsDir, file))) {
      for (const step of [...(flow.steps || []), ...(flow.cleanup || [])]) {
        if (!step.selector) continue;
        assert.doesNotThrow(() => parseSelector(step.selector), `${file} ${flow.id}/${step.id}: ${step.selector}`);
        seen++;
      }
    }
  }
  assert.ok(seen > 20, `expected many selectors, saw ${seen}`);
});

test('a missing control is a failed step_error check, skips the rest of the flow and still runs cleanup', async () => {
  const SHA = '915086acdf8a9061b4dae420935e876183046d9b';
  const server = http.createServer((req, res) => {
    if (req.url === '/api/version') {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ sha: SHA }));
    } else {
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end('<!DOCTYPE html><html><body><button style="width:80px;height:48px" onclick="document.title=\'cleaned\'">Aufraeumen</button></body></html>');
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowqa-missing-'));
  const flowPath = path.join(tmpDir, 'flow.json');
  fs.writeFileSync(flowPath, JSON.stringify({
    flows: [{
      id: 'missing-control',
      steps: [
        { id: 'goto', action: 'goto', url: '/' },
        { id: 'tap-missing', action: 'tap', selector: "button:has-text('Gibt es nicht')", timeout_ms: 500 },
        { id: 'never-reached', action: 'assert', selector: 'button', expected_present: true }
      ],
      cleanup: [{ id: 'cleanup', action: 'cleanup', selector: "button:has-text('aufraeumen')" }]
    }]
  }));
  try {
    const report = await runFlows({
      baseUrl: `http://127.0.0.1:${server.address().port}`,
      expectedSha: SHA,
      outputDir: tmpDir,
      flowDataPath: flowPath,
      viewports: ['1440x900'],
      themes: ['light']
    });
    assert.equal(report.passed, false);
    const failed = report.steps.find((s) => s.step === 'tap-missing');
    assert.ok(failed.checks.some((c) => c.name === 'step_error' && !c.passed && /No visible element/.test(c.detail)));
    assert.equal(report.steps.some((s) => s.step === 'never-reached'), false, 'steps after the error are skipped');
    assert.ok(report.assertions.failed >= 1);
    assert.equal(report.cleanup.passed, true);
  } finally {
    server.close();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  }
});

test('a tap that opens an overlay is judged by the control as tapped, not by the overlay covering it afterwards', async () => {
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({
    executablePath: resolveExecutablePath() || undefined,
    headless: 'new',
    args: ['--no-sandbox']
  });
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowqa-overlay-'));
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900 });
    await page.setContent(`
      <button id="open" style="width:120px;height:48px"
        onclick="document.body.insertAdjacentHTML('beforeend','<div role=dialog style=&quot;position:fixed;inset:0;background:#000&quot;></div>')">Oeffnen</button>`);
    const tap = await executeStep(page, null,
      { id: 'open', action: 'tap', selector: "button:has-text('Oeffnen')", checks: ['visible', 'element_from_point'] },
      '1440x900', 'light', { flow: { id: 'f' }, baseUrl: 'http://localhost', outputDir, flowConstraints: {} });
    assert.equal(await page.$eval('[role=dialog]', () => true), true, 'the tap really opened the overlay');
    assert.deepEqual(tap.checks.filter((c) => !c.passed), []);
  } finally {
    await browser.close();
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

test('cleanup with repeat and then deletes every matching row through its confirm dialog', async () => {
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({
    executablePath: resolveExecutablePath() || undefined,
    headless: 'new',
    args: ['--no-sandbox']
  });
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowqa-repeat-'));
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900 });
    await page.setContent(`
      <ul id="rows">
        <li><button aria-label="QA-x Seite 1.pdf löschen" onclick="ask(this)">d</button></li>
        <li><button aria-label="QA-x Seite 2.pdf löschen" onclick="ask(this)">d</button></li>
        <li><button aria-label="keep.pdf löschen" onclick="ask(this)">d</button></li>
      </ul>
      <script>
        function ask(btn) {
          const d = document.createElement('div'); d.setAttribute('role', 'dialog');
          d.innerHTML = '<button>Abbrechen</button><button>Löschen</button>';
          d.lastChild.onclick = () => { btn.closest('li').remove(); d.remove(); };
          document.body.append(d);
        }
      </script>`);
    await executeStep(page, null, {
      id: 'c', action: 'cleanup', selector: "button[aria-label^='QA-x']",
      then: "[role='dialog'] button:has-text('Löschen')", repeat: 5
    }, '1440x900', 'light', { flow: { id: 'f' }, baseUrl: 'http://localhost', outputDir, flowConstraints: {} });
    const left = await page.$$eval('#rows button', (b) => b.map((x) => x.getAttribute('aria-label')));
    assert.deepEqual(left, ['keep.pdf löschen'], 'only the QA rows are deleted');
  } finally {
    await browser.close();
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

test('goto url_from_link builds the target from a link on the page and fails when none matches', async () => {
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({ executablePath: resolveExecutablePath() || undefined, headless: 'new', args: ['--no-sandbox'] });
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowqa-link-'));
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900 });
    await page.setContent(`<main><a href="/shipping-runs/new?runId=11111111-2222-3333-4444-555555555555">x</a></main>`);
    const ctx = { flow: { id: 'f' }, baseUrl: 'http://localhost:1', outputDir, flowConstraints: {} };
    const step = {
      id: 'g', action: 'goto',
      url_from_link: { selector: "main a[href*='/shipping-runs/']", pattern: '/shipping-runs/(?:new\\?runId=)?([0-9a-f-]{36})', template: '/shipping-runs/$1/pack' }
    };
    const missing = { ...step, url_from_link: { ...step.url_from_link, pattern: '/nothing/([0-9]+)' } };
    await assert.rejects(() => executeStep(page, null, missing, '1440x900', 'light', ctx), /No link matching/);
    const requested = [];
    page.on('request', (req) => requested.push(req.url()));
    await executeStep(page, null, step, '1440x900', 'light', ctx).catch(() => {});
    assert.ok(
      requested.includes('http://localhost:1/shipping-runs/11111111-2222-3333-4444-555555555555/pack'),
      `requested: ${requested.join(', ')}`
    );
  } finally {
    await browser.close();
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

test('touch_only steps pass with a skip note on a desktop viewport', async () => {
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({ executablePath: resolveExecutablePath() || undefined, headless: 'new', args: ['--no-sandbox'] });
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowqa-touch-'));
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900 });
    await page.setContent('<main><div role="dialog">open</div></main>');
    const res = await executeStep(page, null, { id: 's', action: 'assert', selector: "[role='dialog']", touch_only: true, expected_present: false },
      '1440x900', 'light', { flow: { id: 'f' }, baseUrl: 'http://localhost', outputDir, flowConstraints: {} });
    assert.equal(res.passed, true);
    assert.equal(res.checks[0].name, 'touch_only_skipped');
  } finally {
    await browser.close();
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

test('assert with expected_text polls with locator semantics when node with same role is replaced after 500ms', async () => {
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({
    executablePath: resolveExecutablePath() || undefined,
    headless: 'new',
    args: ['--no-sandbox']
  });
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowqa-replace-'));
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900 });
    await page.setContent(`
      <div id="container">
        <p role="status">Wird ausgeführt</p>
      </div>
      <script>
        setTimeout(() => {
          document.getElementById('container').innerHTML = '<div role="status">Erledigt.</div>';
        }, 500);
      </script>
    `);
    const res = await executeStep(
      page,
      null,
      {
        id: 'assert_status',
        action: 'assert',
        selector: "[role='status']",
        expected_text: 'Erledigt.',
        timeout_ms: 3000
      },
      '1440x900',
      'light',
      { flow: { id: 'f' }, baseUrl: 'http://localhost', outputDir, flowConstraints: {} }
    );
    assert.equal(res.passed, true, `expected step to pass, got checks: ${JSON.stringify(res.checks)}`);
    const textCheck = res.checks.find((c) => c.name === 'assert_text');
    assert.ok(textCheck, 'must run assert_text check');
    assert.equal(textCheck.passed, true, 'assert_text must pass when node is replaced');
  } finally {
    await browser.close();
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});

test('assert with expected_visible polls with locator semantics when node with same role is replaced after 500ms', async () => {
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({
    executablePath: resolveExecutablePath() || undefined,
    headless: 'new',
    args: ['--no-sandbox']
  });
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), 'flowqa-visible-replace-'));
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1440, height: 900 });
    await page.setContent(`
      <div id="container">
        <p role="status" style="display:none">Wird ausgeführt</p>
      </div>
      <script>
        setTimeout(() => {
          document.getElementById('container').innerHTML = '<div role="status" style="display:block">Erledigt.</div>';
        }, 500);
      </script>
    `);
    const res = await executeStep(
      page,
      null,
      {
        id: 'assert_status_visible',
        action: 'assert',
        selector: "[role='status']",
        expected_visible: true,
        timeout_ms: 3000
      },
      '1440x900',
      'light',
      { flow: { id: 'f' }, baseUrl: 'http://localhost', outputDir, flowConstraints: {} }
    );
    assert.equal(res.passed, true, `expected step to pass, got checks: ${JSON.stringify(res.checks)}`);
    const visCheck = res.checks.find((c) => c.name === 'assert_visible');
    assert.ok(visCheck, 'must run assert_visible check');
    assert.equal(visCheck.passed, true, 'assert_visible must pass when node is replaced');
  } finally {
    await browser.close();
    fs.rmSync(outputDir, { recursive: true, force: true });
  }
});
