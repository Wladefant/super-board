/**
 * Tests for depth_report_capture.mjs against a local page server (real headless Chrome).
 *   node --test workflows/portable/test_depth_report_capture.mjs
 * Tracking: https://github.com/Wladefant/super-board/issues/517
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { capture } from './depth_report_capture.mjs';

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

function serve(page, version) {
  const server = http.createServer((req, res) => {
    if (req.url === '/api/version') { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify(version)); return; }
    res.writeHead(200, { 'content-type': 'text/html; charset=utf-8' });
    res.end(page);
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server)));
}

async function run(page, version, expectedSha = SHA) {
  const server = await serve(page, version);
  const outputDir = fs.mkdtempSync(path.join(os.tmpdir(), 'depth-capture-'));
  try {
    return await capture({ baseUrl: `http://127.0.0.1:${server.address().port}`, expectedSha, outputDir });
  } finally {
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

test('a served SHA that differs from the expected one is refused before any capture', async () => {
  await assert.rejects(run(CLEAN, { sha: 'e'.repeat(40), dirty: false }), /Served SHA/);
});

test('a dirty served tree is refused', async () => {
  await assert.rejects(run(CLEAN, { sha: SHA, dirty: true }), /local changes/);
});
