import { describe, expect, test } from "bun:test";
import { authenticateInitData, authenticateAppSession, issueAppSession } from "../daemon/miniapp-auth";
import { authorizedRelay, startRelay } from "../miniapp/relay";
import { miniAppRequest, connectMiniApp } from "../daemon/miniapp";
import { createHmac } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { evaluateApproval, describeApproval, pendingApprovals } from "../extension/approvals";

const raw = 'query_id=test-query&user=%7B%22id%22%3A1247617658%2C%22first_name%22%3A%22Test%22%7D&auth_date=1700000000&hash=7d6f11c05a1930f4d8e1ff0735d05997656bfa3f819d27e52d2b180491c13cda';
const token = "123456:test-token";
const users = ["1247617658"];
describe("Telegram initData authentication", () => {
  test("accepts the fixed HMAC-SHA256 vector", () => expect(authenticateInitData(raw, token, users, 1700000000000)).toBe(users[0]));
  test("rejects tampering and a different bot", () => {
    expect(() => authenticateInitData(raw.replace('test-query', 'forged'), token, users, 1700000000000)).toThrow();
    expect(() => authenticateInitData(raw, 'other-token', users, 1700000000000)).toThrow();
  });
  test("rejects unauthorized users, expired and future credentials", () => {
    expect(() => authenticateInitData(raw, token, [], 1700000000000)).toThrow();
    expect(() => authenticateInitData(raw, token, users, 1700000301000)).toThrow();
    expect(() => authenticateInitData(raw, token, users, 1699999969000)).toThrow();
  });
  test("rejects duplicate parameters", () => expect(() => authenticateInitData(raw + '&auth_date=1700000000', token, users, 1700000000000)).toThrow());
});
describe("outbound relay handshake", () => {
  const secret = 'a'.repeat(64);
  test("requires exact authorization and a strong configured secret", () => {
    expect(authorizedRelay(`Bearer ${secret}`, secret)).toBe(true);
    expect(authorizedRelay(null, secret)).toBe(false);
    expect(authorizedRelay(`Bearer ${'b'.repeat(64)}`, secret)).toBe(false);
    expect(authorizedRelay('Bearer short', 'short')).toBe(false);
  });
  test("rejects unauthenticated handshake over real HTTP", async () => {
    const server = startRelay(secret, 0);
    try {
      const response = await fetch(`http://localhost:${server.port}/relay`);
      expect(response.status).toBe(401);
      expect((await fetch(`http://localhost:${server.port}/api/state`)).status).toBe(401);
    } finally { server.stop(true); }
  });
});

test("daemon API rejects forged auth before reading state or deciding approval", async () => {
  let touched = false;
  const options = {
    stateDir: "unused", token, allowedUsers: users,
    session: () => { touched = true; return null; },
    sessions: async () => { touched = true; return []; },
    dashboard: () => { touched = true; return null; },
    status: () => { touched = true; return {}; },
  };
  for (const [path, method] of [["/api/state", "GET"], ["/api/approval", "POST"]]) {
    const result = await miniAppRequest({ id: "request", path, method, initData: "forged", body: "{}" }, options);
    expect(result.status).toBe(401);
  }
  expect(touched).toBe(false);
});

test("relay forwards signed API state and rejects forged browser credentials", async () => {
  const secret = "c".repeat(64);
  const server = startRelay(secret, 0);
  const ws = new WebSocket(`ws://localhost:${server.port}/relay`, { headers: { Authorization: `Bearer ${secret}` } });
  const dir = mkdtempSync(join(tmpdir(), "miniapp-"));
  try {
    await new Promise<void>((resolve, reject) => { ws.onopen = () => resolve(); ws.onerror = reject; });
    ws.onmessage = async event => {
      ws.send(JSON.stringify(await miniAppRequest(JSON.parse(String(event.data)), {
        stateDir: dir, token, allowedUsers: users, session: () => null,
        sessions: async () => [{ id: "live-session" }], dashboard: () => null, status: () => ({ polling: true }),
      })));
    };
    expect((await fetch(`http://localhost:${server.port}/api/state`, { headers: { "x-telegram-init-data": "fake" } })).status).toBe(401);
    const params = new URLSearchParams({ auth_date: String(Math.floor(Date.now() / 1000)), user: JSON.stringify({ id: 1247617658 }) });
    const key = createHmac("sha256", "WebAppData").update(token).digest();
    params.set("hash", createHmac("sha256", key).update([...params].map(([k, v]) => `${k}=${v}`).join("\n")).digest("hex"));
    const exchange = await fetch(`http://localhost:${server.port}/api/session`, { method: "POST", headers: { "x-telegram-init-data": params.toString() } });
    expect(exchange.status).toBe(200);
    const { appSession } = await exchange.json();
    const response = await fetch(`http://localhost:${server.port}/api/state`, { headers: { "x-miniapp-session": appSession } });
    expect(response.status).toBe(200);
    expect((await response.json()).sessions).toEqual([{ id: "live-session" }]);
  } finally { ws.close(); server.stop(true); rmSync(dir, { recursive: true, force: true }); }
});

test("app session survives launch expiry but rejects wrong actors and absolute expiry", () => {
  const session = issueAppSession(users[0], token, 1700000000000);
  expect(authenticateAppSession(session, token, users, 1700000600000)).toBe(users[0]);
  expect(() => authenticateAppSession(session, "other", users, 1700000600000)).toThrow();
  expect(() => authenticateAppSession(session, token, [], 1700000600000)).toThrow();
  expect(() => authenticateAppSession(session, token, users, 1700028801000)).toThrow();
});

test("malformed relay configuration cannot take down daemon startup", () => {
  const dir = mkdtempSync(join(tmpdir(), "miniapp-config-"));
  try {
    for (const config of ["{", "null", "{}", JSON.stringify({ url: "https://", secret: "s".repeat(64) })]) {
      writeFileSync(join(dir, "miniapp.json"), config);
      expect(() => connectMiniApp({ stateDir: dir, token, allowedUsers: users, session: () => null, sessions: async () => [], dashboard: () => null, status: () => ({}) })()).not.toThrow();
    }
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("authenticated actors cannot read or decide another private chat's pending approvals", async () => {
  const dir = mkdtempSync(join(tmpdir(), "miniapp-actors-"));
  const alice = "111", bob = "222";
  const routes: Record<string, string> = { [alice]: "alice-session", [bob]: "bob-session" };
  const options = {
    stateDir: dir, token, allowedUsers: [alice, bob],
    session: (user: string) => routes[user] ?? null,
    sessions: async () => [], dashboard: (user: string) => ({ owner: user }), status: () => ({}),
  };
  try {
    const approval = evaluateApproval(dir, "bob-operation", describeApproval("bash", { command: "echo safe" }, "shell", { sessionId: routes[bob], requester: "bob-agent", task: "test", cwd: dir }));
    const aliceSession = issueAppSession(alice, token);
    const state = await miniAppRequest({ id: "1", path: "/api/state", method: "GET", initData: "", appSession: aliceSession, body: "" }, options);
    expect(state.status).toBe(200);
    expect(state.data).toMatchObject({ session: "alice-session", approvals: [], dashboard: { owner: alice } });
    const decision = { token: approval.token, decision: "denied" };
    const wrongActor = await miniAppRequest({ id: "2", path: "/api/approval", method: "POST", initData: "", appSession: aliceSession, body: JSON.stringify(decision) }, options);
    expect(wrongActor.status).toBe(409);
    expect(pendingApprovals(dir, routes[bob])).toHaveLength(1);
    const rightActor = await miniAppRequest({ id: "3", path: "/api/approval", method: "POST", initData: "", appSession: issueAppSession(bob, token), body: JSON.stringify(decision) }, options);
    expect(rightActor.status).toBe(200);
    expect(pendingApprovals(dir, routes[bob])).toHaveLength(0);
    expect((await miniAppRequest({ id: "4", path: "/api/approval", method: "POST", initData: "", appSession: issueAppSession(bob, token), body: JSON.stringify(decision) }, options)).status).toBe(409);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("anonymous flood never consumes relay forwarding slots", async () => {
  const server = startRelay("d".repeat(64), 0);
  const ws = new WebSocket(`ws://localhost:${server.port}/relay`, { headers: { Authorization: `Bearer ${"d".repeat(64)}` } });
  let forwarded = 0;
  try {
    await new Promise<void>((resolve, reject) => { ws.onopen = () => resolve(); ws.onerror = reject; });
    ws.onmessage = event => { forwarded++; const request = JSON.parse(String(event.data)); ws.send(JSON.stringify({ id: request.id, status: 200, data: { ok: true } })); };
    const responses = await Promise.all(Array.from({ length: 70 }, () => fetch(`http://localhost:${server.port}/api/state`)));
    expect(responses.every(response => response.status === 401)).toBe(true);
    expect(forwarded).toBe(0);
    const response = await fetch(`http://localhost:${server.port}/api/state`, { headers: { "x-miniapp-session": issueAppSession(users[0], token) } });
    expect(response.status).toBe(200);
    expect(forwarded).toBe(1);
  } finally { ws.close(); server.stop(true); }
});
