/**
 * Tests for depth_report_capture.mjs against a local page server (real headless Chrome).
 *   node --test workflows/portable/test_depth_report_capture.mjs
 * Tracking: https://github.com/Wladefant/super-board/issues/517, https://github.com/Wladefant/super-board/issues/559
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { capture, readServedVersion } from './depth_report_capture.mjs';

const SHA = 'c'.repeat(40);

const CLEAN = `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<style>:root{color-scheme:light dark}body{margin:0;background:#fff;color:#111}
@media (prefers-color-scheme: dark){body{background:#111;color:#eee}}
svg{width:300px;background:#fff} text{font:11px sans-serif;fill:#111} rect{fill:#fff;stroke:#111}</style></head>
<body><h1>Report</h1><p>Plain text.</p>
<svg viewBox="0 0 320 200"><rect x="10" y="10" width="200" height="40"/><text x="20" y="34">module</text></svg></body></html>`;

const BROKEN = `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<style>body{margin:0;background:#fff;color:#111} .faint{color:#ddd} .wide{width:900px;height:10px}
svg{width:300px;background:#fff} text{font:11px sans-serif;fill:#111} rect{fill:#fff;stroke:#111}</style></head>
<body><p class="faint">Too faint to read.</p><div class="wide"></div><img src="https://example.invalid/x.png" alt="">
<svg viewBox="0 0 320 200"><rect x="10" y="10" width="30" height="40"/><text x="14" y="34">a-long-module-name</text></svg></body></html>`;

/** A page whose light and dark captures differ, with extra CSS and body. */
const page = (css, body) => `<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1">
<style>:root{color-scheme:light dark}body{margin:0;background:#fff;color:#111;font:16px sans-serif}
@media (prefers-color-scheme: dark){body{background:#111;color:#eee}} ${css}</style></head><body><h1>Report</h1>${body}</body></html>`;

// White on navy; black on white under a gradient from transparent to half-transparent white.
const READABLE_GRADIENTS = page(
  '.navy{background:linear-gradient(#0f172a,#1e3a8a);color:#fff} .veil{background:#fff linear-gradient(transparent,rgba(255,255,255,.5));color:#000}',
  '<p class="navy">White on navy.</p><p class="veil">Black under a white veil.</p>');

// White on a gradient that ends at #777 (4.48:1); black on red to green: both stops pass, the middle does not.
// The tspan sits in a navy box with a slate fill, so it fails only when measured against that box.
const LOW_GRADIENTS = page(
  '.fade{background:linear-gradient(#000,#777);color:#fff} .hue{background:linear-gradient(#f00,#0f0);color:#000} svg{width:300px;background:#fff} rect{fill:#0f172a} text{font:11px sans-serif;fill:#1e293b}',
  '<p class="fade">Fades to grey</p><p class="hue">Red to green</p>' +
  '<svg viewBox="0 0 320 200"><rect x="10" y="10" width="300" height="80"/><text x="20" y="34">box<tspan x="20" dy="18">dim note</tspan></text></svg>');

const PNG_1PX = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=';
const IMAGE_BACKGROUND = page(
  `.photo{background:#fff url("data:image/png;base64,${PNG_1PX}");color:#111}`,
  '<p class="photo">Text on a photo.</p>');

const contrastOf = (m, viewport, theme) => m.shots.find((s) => s.viewport === viewport && s.theme === theme).checks.find((c) => c.name === 'text_contrast');

const HANG = Symbol('hang');

function serve(page, version) {
  const server = http.createServer((req, res) => {
    if (req.url === '/api/version') {
      if (version === HANG) return; // never answers
      res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(version)); return;
    }
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    res.end(page);
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

async function run(page, version, expectedSha = SHA, versionTimeoutMs = 10000) {
  const server = await serve(page, version);
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), 'depth-capture-'));
  try {
    return await capture({ baseUrl: `http://127.0.0.1:${server.address().port}`, expectedSha, outputDir, versionTimeoutMs });
  } finally {
    server.closeAllConnections();
    server.close();
  }
}

test('a clean report passes every check in all four captures', async () => {
  const m = await run(CLEAN, { sha: SHA, dirty: false, surveyed_sha: 'd'.repeat(40), template: 't.html' });
  assert.equal(m.served_sha, SHA);
  assert.deepEqual(m.failed, []);
  assert.equal(m.passed, true);
  assert.deepEqual(m.shots.map((s) => `${s.viewport} ${s.theme}`), ['1440x900 light', '1440x900 dark', '390x844 light', '390x844 dark']);
  for (const s of m.shots) assert.ok(s.file.endsWith(`-${SHA.slice(0, 12)}.png`));
});

test('overflow, low contrast, a label outside its box and a network request each fail', async () => {
  const m = await run(BROKEN, { sha: SHA, dirty: false });
  assert.equal(m.passed, false);
  const at390 = Object.fromEntries(m.shots.find((s) => s.viewport === '390x844' && s.theme === 'light').checks.map((c) => [c.name, c.passed]));
  assert.deepEqual(at390, { no_horizontal_overflow: false, text_contrast: false, svg_labels_fit: false, offline: false });
});

test('readable text on a gradient passes, measured against every colour of the gradient', async () => {
  const m = await run(READABLE_GRADIENTS, { sha: SHA, dirty: false });
  for (const s of m.shots) assert.equal(contrastOf(m, s.viewport, s.theme).passed, true, `${s.viewport} ${s.theme}: ${contrastOf(m, s.viewport, s.theme).detail}`);
});

test('low-contrast text on a gradient fails with the ratio of its worst colour', async () => {
  const m = await run(LOW_GRADIENTS, { sha: SHA, dirty: false });
  const c = contrastOf(m, '1440x900', 'light');
  assert.equal(c.passed, false);
  assert.ok(c.detail.includes('"text":"Fades to grey","ratio":4.48,'), c.detail);
  assert.ok(c.detail.includes('"text":"Red to green","ratio":3.94,'), c.detail);
  assert.ok(c.detail.includes('"text":"dim note","ratio":1.22,'), c.detail);
});

test('text over a background image is not proven and never passes', async () => {
  const m = await run(IMAGE_BACKGROUND, { sha: SHA, dirty: false });
  const c = contrastOf(m, '390x844', 'dark');
  assert.equal(c.passed, false);
  assert.match(c.detail, /not proven: \[\{"text":"Text on a photo\.","reason":"background-image"\}\]/);
});

async function readVersion(version) {
  const server = await serve(CLEAN, version);
  try {
    return await readServedVersion(`http://127.0.0.1:${server.address().port}`, SHA, 1000);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}

test('capture prefers commit over SemVer and deploymentId', async () => {
  const version = await readVersion({ version: '1.0.0', commit: SHA, deploymentId: 'b'.repeat(40), sha: 'd'.repeat(40) });
  assert.equal(version.sha, SHA);
});

test('capture rejects SemVer-only with a clear error', async () => {
  await assert.rejects(readVersion({ version: '1.0.0' }), /40-hex commit/);
});

test('a served SHA that differs from the expected one is refused before any capture', async () => {
  await assert.rejects(run(CLEAN, { sha: 'e'.repeat(40), dirty: false }), /Served SHA/);
});

test('a dirty served tree is refused', async () => {
  await assert.rejects(run(CLEAN, { sha: SHA, dirty: true }), /local changes/);
});

test('a version endpoint that never answers is refused within the timeout', async () => {
  const started = Date.now();
  await assert.rejects(run(CLEAN, HANG, SHA, 500), /Served SHA check failed: GET \/api\/version/);
  assert.ok(Date.now() - started < 5000, 'the capture must not wait past its version timeout');
});
