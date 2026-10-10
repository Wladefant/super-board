import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as runner from './flow_qa_runner.mjs';
const { formatReceipt, parseCliArgs } = runner;
const selectAccountFlows = (...args) => {
  assert.equal(typeof runner.selectAccountFlows, 'function', 'runner must discover account-ready flows instead of fixture assumptions');
  return runner.selectAccountFlows(...args);
};

const source = JSON.parse(fs.readFileSync(new URL('./flows/shipnovo.json', import.meta.url)));
test('default account suite discovers messages and orders without fixed fixtures', () => {
  const flows = runner.selectAccountFlows
    ? selectAccountFlows(source.flows, { fixtureState: 'populated', readOnly: true })
    : source.flows;
  assert.doesNotMatch(JSON.stringify(flows), /QA-ART-489-NAT/,
    'the read-only account suite must not depend on a fixed article fixture');
  assert.ok(flows.some(f => f.id === 'messages_keyboard'), 'default suite must include messages keyboard');
  assert.ok(flows.some(f => f.id === 'account_orders_ready'), 'default suite must discover an order');
  for (const flow of flows) {
    assert.doesNotMatch(JSON.stringify(flow), /[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/i);
    assert.ok(flow.steps.some(s => s.readiness_missing), `${flow.id} must fail clearly when account records are missing`);
  }
});
test('four-state readiness is mandatory and unknown states fail', () => {
  for (const state of ['empty', 'populated', 'disconnected', 'limited']) {
    assert.ok(selectAccountFlows(source.flows, { fixtureState: state, readOnly: true }).length > 0, state);
  }
  assert.throws(() => selectAccountFlows(source.flows, { fixtureState: 'missing', readOnly: true }), /account not ready: unknown fixture state/);
});
test('receipt always names live or fixture scope, including infrastructure failure', () => {
  const report = { passed: false, steps: [], served_sha: 'a'.repeat(40), error: 'unavailable' };
  assert.match(formatReceipt(report), /scope: live account/);
  assert.match(formatReceipt({ ...report, fixture_state: 'populated' }), /scope: fixture populated on testbed a{40}/);
});
test('CLI accepts fixture-state and read-only without changing legacy defaults', () => {
  assert.equal(parseCliArgs(['node', 'runner', '--fixture-state', 'limited', '--read-only']).fixtureState, 'limited');
  assert.equal(parseCliArgs(['node', 'runner', '--read-only']).readOnly, true);
  assert.equal(parseCliArgs(['node', 'runner']).readOnly, undefined);
});

test('SELECT-only workspace transport allows only the reviewed action and discovered read record', () => {
  const policy = { origin: 'https://shipnovo-test.wladefant.de', actionId: '7f85b5dc5093692d3462e0aced38e6a3424937ad63', conversationIds: ['QA-1066-discovered-buyer-question'] };
  const request = { method: 'POST', url: policy.origin + '/messages', actionId: policy.actionId, body: JSON.stringify([{conversationId: policy.conversationIds[0]}]) };
  const allows = value => runner.isReadOnlyWorkspaceRequest?.(value, policy) ?? false;
  assert.equal(allows(request), true, 'approved SELECT-only workspace request must not turn the composer into an error state');
  for (const change of [
    {actionId: 'unknown-action'}, {url: policy.origin + '/orders'}, {url: 'https://shipnovo.app/messages'},
    {body: JSON.stringify([{conversationId:'QA-1066-unread-product-question'}])},
    {body: JSON.stringify([{conversationId:policy.conversationIds[0],body:'QA-unsaved'}])},
    {body: JSON.stringify([{conversationId:policy.conversationIds[0]},{conversationId:policy.conversationIds[0]}])}
  ]) assert.equal(allows({...request,...change}), false, JSON.stringify(change));
});

test('read-only browser blocks writes and reports missing account readiness by name', async () => {
  const http = await import('node:http');
  const os = await import('node:os');
  const path = await import('node:path');
  let writes = 0;
  const sha = 'b'.repeat(40);
  const server = http.createServer((request, response) => {
    if (request.method === 'POST') writes++;
    if (request.url === '/api/version') {
      response.setHeader('Content-Type', 'application/json');
      response.end(JSON.stringify({ sha }));
    } else {
      response.setHeader('Content-Type', 'text/html');
      response.end('<!doctype html><html><body><h1 id="ready">Ready</h1><script>fetch("/must-not-write",{method:"POST"}).catch(()=>{});</script></body></html>');
    }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'flow-readiness-'));
  const flowPath = path.join(directory, 'flows.json');
  fs.writeFileSync(flowPath, JSON.stringify({ flows: [{
    id: 'readiness-control', read_only: true,
    steps: [
      { id: 'open', action: 'goto', url: '/' },
      { id: 'present', action: 'assert', selector: '#ready', expected_present: true },
      { id: 'missing', action: 'assert', selector: '#missing', expected_present: true, readiness_missing: 'a conversation', timeout_ms: 250 },
      { id: 'absent', action: 'assert', selector: '#missing', expected_present: false }
    ]
  }] }));
  try {
    const report = await runner.runFlows({
      project: 'runner-control', baseUrl: `http://127.0.0.1:${server.address().port}`,
      expectedSha: sha, readOnly: true, flowDataPath: flowPath, outputDir: directory,
      viewports: ['390x844'], themes: ['light']
    });
    assert.equal(writes, 0, 'read-only run must not reach a write endpoint');
    assert.ok(report.blocked_requests.some(r => r.method === 'POST' && r.path === '/must-not-write'));
    assert.ok(report.steps.find(s => s.step === 'missing').checks.some(c =>
      c.name === 'assert_present' && !c.passed && c.detail === 'account not ready: a conversation'));
    assert.ok(report.steps.find(s => s.step === 'present').checks.some(c => c.name === 'assert_present' && c.passed));
    assert.ok(report.steps.find(s => s.step === 'absent').checks.some(c => c.name === 'assert_absent' && c.passed));
    assert.equal(report.assertions.failed, 1);
  } finally {
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
