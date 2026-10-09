import test from 'node:test';
import assert from 'node:assert/strict';
import { formatReceipt, verifyServedSha } from './flow_qa_runner.mjs';

const sha = 'a'.repeat(40);
const report = () => ({ served_sha: sha, expected_sha: sha, passed: true,
  assertions: { passed: 7, failed: 0 }, viewports: ['390x844', '390x420', '1440x900'],
  cleanup: { passed: true }, source: { runner: '1'.repeat(64), flow: '2'.repeat(64), project: 'shipnovo' } });

test('receipt records runner and flow identities', () => {
  assert.match(formatReceipt(report()), /FLOW-QA-SOURCE runner=1{64} flow=2{64} project=shipnovo/);
});
test('served SHA differs from head refuses PASS', () => {
  assert.match(formatReceipt({ ...report(), expected_sha: 'b'.repeat(40) }), /FLOW-QA: FAIL/);
});
test('zero assertions refuse PASS', () => {
  assert.match(formatReceipt({ ...report(), assertions: { passed: 0, failed: 0 } }), /FLOW-QA: FAIL/);
});
test('missing source refuses PASS', () => {
  assert.match(formatReceipt({ ...report(), source: undefined }), /FLOW-QA: FAIL/);
});
test('abbreviated SHA cannot prove served head', () => {
  assert.equal(verifyServedSha('aaaaaaa', sha).match, false);
});
