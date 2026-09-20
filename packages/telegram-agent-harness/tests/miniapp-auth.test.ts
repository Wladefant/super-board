import { describe, expect, test } from "bun:test";
import { authenticateInitData } from "../daemon/miniapp-auth";
import { authorizedRelay, startRelay } from "../miniapp/relay";
import { miniAppRequest } from "../daemon/miniapp";
import { createHmac } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

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
      expect((await fetch(`http://localhost:${server.port}/api/state`)).status).toBe(503);
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
    const response = await fetch(`http://localhost:${server.port}/api/state`, { headers: { "x-telegram-init-data": params.toString() } });
    expect(response.status).toBe(200);
    expect((await response.json()).sessions).toEqual([{ id: "live-session" }]);
  } finally { ws.close(); server.stop(true); rmSync(dir, { recursive: true, force: true }); }
});
