import { createHmac } from 'node:crypto';
import { expect, test } from 'bun:test';
import { createClient } from '../miniapp/client.js';
import { issueAppSession, authenticateAppSession, authenticateInitData } from '../daemon/miniapp-auth';
import { miniAppRequest } from '../daemon/miniapp';
import { startRelay } from '../miniapp/relay';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// WHY: expired credentials must not trap clients or replay commands; revoked bearer
// sessions must stay rejected after reopening storage. External Telegram launch is not simulated proof.
for (const malformed of [false, true]) test(`401 clears credentials before decoding (malformed=${malformed})`, async () => {
  const calls: string[] = [];
  let exchanges = 0;
  const request = createClient(() => 'signed-launch', async (path: string) => {
    calls.push(path);
    if (path === '/api/session') return Response.json({ appSession: `session-${++exchanges}` });
    if (exchanges === 1) return malformed ? new Response('Unauthorized', { status: 401 }) : Response.json({ error: 'expired' }, { status: 401 });
    return Response.json({ ok: true });
  });
  await expect(request('/api/approval', { token: 'operation', decision: 'approved' })).rejects.toThrow();
  expect(calls).toEqual(['/api/session', '/api/approval']);
  expect(await request('/api/state')).toEqual({ ok: true });
  expect(calls).toEqual(['/api/session', '/api/approval', '/api/session', '/api/state']);
});

test('logout clears only successful logout credentials; ordinary failures preserve the session', async () => {
  let exchanges = 0;
  let status = 409;
  const request = createClient(() => 'signed-launch', async (path: string) => path === '/api/session'
    ? Response.json({ appSession: `session-${++exchanges}` }) : Response.json({ ok: true }, { status }));
  await expect(request('/api/logout', {})).rejects.toThrow();
  status = 200;
  await request('/api/state');
  expect(exchanges).toBe(1);
  await request('/api/logout', {});
  await request('/api/state');
  expect(exchanges).toBe(2);
});

test('revocation persists, rejects replay before data access, and isolates simultaneous sessions', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'miniapp-revoke-'));
  const token = '123:disposable-token';
  let reads = 0;
  const options = { stateDir: dir, token, allowedUsers: ['111'], session: () => { reads++; return null; }, sessions: async () => [], dashboard: () => null, status: () => ({}) };
  const first = issueAppSession('111', token), second = issueAppSession('111', token);
  const call = (appSession: string, path = '/api/state', method = 'GET') => miniAppRequest({ id: 'test', path, method, initData: '', appSession, body: '{}' }, { ...options });
  try {
    expect(first).not.toBe(second);
    expect((await call(first)).status).toBe(200);
    expect((await call(first, '/api/logout', 'GET')).status).toBe(404);
    expect((await call(first, '/api/logout', 'POST')).data).toEqual({ revoked: true });
    reads = 0;
    expect((await call(first)).status).toBe(401);
    expect((await call(first, '/api/approval', 'POST')).status).toBe(401);
    expect(reads).toBe(0);
    expect((await call(second)).status).toBe(200);
    expect((await call(second.replace(/^111/, '222'))).status).toBe(401);
    const parts = second.split('.'); parts[2] = 'f'.repeat(32);
    expect((await call(parts.join('.'))).status).toBe(401);
    expect(() => authenticateAppSession('111.1700000000.' + 'a'.repeat(64), token, ['111'])).toThrow();
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test('relay evicts least recently used peers, including recently denied peers', async () => {
  const server = startRelay('s'.repeat(64), 0);
  const session = issueAppSession('111', 'test');
  const call = (ip: string) => fetch(`http://localhost:${server.port}/api/state`, { headers: { 'x-miniapp-session': session, 'x-forwarded-for': ip } });
  try {
    for (let n = 0; n < 8; n++) expect((await call('old')).status).toBe(503);
    for (let n = 0; n < 2047; n++) await call(`peer-${n}`);
    // Consume accumulated tokens, then mark this old bucket as recently used.
    for (let n = 0; n < 8; n++) await call('old');
    expect((await call('old')).status).toBe(429);
    await call('new');
    expect((await call('old')).status).toBe(429);
  } finally { server.stop(true); }
});

test('launch validation rejects every malformed signed identity and freshness boundary', () => {
  const token = '123:test', now = 1700000000000;
  const signed = (fields: Record<string, string>) => {
    const p = new URLSearchParams(fields);
    const key = createHmac('sha256', 'WebAppData').update(token).digest();
    p.set('hash', createHmac('sha256', key).update([...p].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([k, v]) => `${k}=${v}`).join('\n')).digest('hex'));
    return p.toString();
  };
  for (const user of ['null', '{', '{}', '{"id":1.5}', '{"id":"111"}', '{"id":222}']) {
    expect(() => authenticateInitData(signed({ auth_date: '1700000000', user }), token, ['111'], now)).toThrow();
  }
  for (const auth_date of ['0', '-1', 'no', '1700000000.5', '1699999699', '1700000031']) {
    expect(() => authenticateInitData(signed({ auth_date, user: '{"id":111}' }), token, ['111'], now)).toThrow();
  }
  expect(() => authenticateInitData(signed({ auth_date: '1700000000', user: '{"id":111}', padding: 'x'.repeat(16384) }), token, ['111'], now)).toThrow();
  expect(() => authenticateInitData('', token, ['111'], now)).toThrow();
  const valid = signed({ auth_date: '1700000000', user: '{"id":111}' });
  expect(() => authenticateInitData(valid.replace(/hash=[^&]+/, 'hash=no'), token, ['111'], now)).toThrow();
  expect(() => authenticateAppSession(issueAppSession('111', token, now + 31000), token, ['111'], now)).toThrow();
});

test('failed exchange never reaches protected routes', async () => {
  const calls: string[] = [];
  const request = createClient(() => 'forged', async (path: string) => {
    calls.push(path); return Response.json({ error: 'Unauthorized' }, { status: 401 });
  });
  await expect(request('/api/approval', {})).rejects.toThrow('Unauthorized');
  expect(calls).toEqual(['/api/session']);
});

test('even correctly signed duplicate launch fields are rejected', () => {
  const token = '123:test';
  const p = new URLSearchParams('auth_date=1700000000&auth_date=1700000000&user=%7B%22id%22%3A111%7D');
  const key = createHmac('sha256', 'WebAppData').update(token).digest();
  p.set('hash', createHmac('sha256', key).update([...p].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([k, v]) => `${k}=${v}`).join('\n')).digest('hex'));
  expect(() => authenticateInitData(p.toString(), token, ['111'], 1700000000000)).toThrow();
});
