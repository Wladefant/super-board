import test from 'node:test';
import assert from 'node:assert/strict';
import * as runner from './flow_qa_runner.mjs';

const sha = 'a'.repeat(40);
const measured = { served_sha: sha, account: 'qa-user', url: 'http://127.0.0.1:4901/products', width: 390, height: 844, device_scale: 1 };
const page = (data) => ({
  evaluate: async () => data,
  viewport: () => ({ width: data.width, height: data.height }),
  screenshot: async () => Buffer.from('image bytes')
});

test('product capture binds measured browser source and image hash', async () => {
  assert.equal(typeof runner.captureProductScreenshot, 'function');
  const result = await runner.captureProductScreenshot(page(measured), sha, '390x844');
  assert.equal(result.record.account, 'qa-user');
  assert.equal(result.record.served_sha, sha);
  assert.equal(result.record.viewport, '390x844');
  assert.equal(result.record.device_scale, 1);
  assert.equal(result.record.url, measured.url);
  assert.equal(result.record.source, 'application');
  assert.equal(result.record.sha256.length, 64);
});
test('static mockup without session refuses capture', async () => {
  assert.equal(typeof runner.captureProductScreenshot, 'function');
  await assert.rejects(() => runner.captureProductScreenshot(page({ ...measured, account: null }), sha, '390x844'), /authenticated/);
});
test('stale served SHA refuses capture', async () => {
  assert.equal(typeof runner.captureProductScreenshot, 'function');
  await assert.rejects(() => runner.captureProductScreenshot(page({ ...measured, served_sha: 'b'.repeat(40) }), sha, '390x844'), /SHA/);
});
test('measured viewport mismatch refuses capture', async () => {
  assert.equal(typeof runner.captureProductScreenshot, 'function');
  await assert.rejects(() => runner.captureProductScreenshot(page({ ...measured, width: 1440 }), sha, '390x844'), /viewport/);
});
