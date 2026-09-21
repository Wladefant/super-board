import { describe, expect, test } from "bun:test";
import { authenticateInitData, authenticateAppSession, issueAppSession } from "../daemon/miniapp-auth";
import { authorizedRelay, startRelay } from "../miniapp/relay";
import { miniAppRequest, connectMiniApp } from "../daemon/miniapp";
import { createHmac } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { evaluateApproval, describeApproval, pendingApprovals } from "../extension/approvals";
import { createClient, getUnavailableState, UNAVAILABLE_MESSAGE, REOPEN_MESSAGE, SECTIONS, TerminalAuthError, isTerminalAuthError } from "../miniapp/client.js";
import { SERVED_FILES } from "../miniapp/relay";
import { readFileSync } from "node:fs";

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
    expect((await fetch(`http://localhost:${server.port}/relay`, { headers: { Authorization: `Bearer ${secret}` } })).status).toBe(409);
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
    for (const [path, method] of [["/api/unknown", "GET"], ["/api/state", "POST"], ["/api/approval", "GET"]]) {
      expect((await miniAppRequest({ id: "5", path, method, initData: "", appSession: aliceSession, body: "" }, options)).status).toBe(404);
    }
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

describe("Mini App client 401 session reset and unavailable state", () => {
  test("401 on /api/state drops cached session, re-runs /api/session with fresh initData, and retries original request once", async () => {
    const calls: { path: string; method?: string; headers: Record<string, string> }[] = [];
    let initDataCounter = 0;
    const getInitData = () => `init-data-version-${++initDataCounter}`;

    let sessionCount = 0;
    const transport = async (path: string, init?: RequestInit) => {
      const headers = (init?.headers ?? {}) as Record<string, string>;
      calls.push({ path, method: init?.method, headers });

      if (path === "/api/session") {
        return Response.json({ appSession: `session-token-${++sessionCount}` });
      }

      if (path === "/api/state") {
        if (headers["x-miniapp-session"] === "initial-stale-session") {
          return Response.json({ error: "Open this app from Telegram again to authenticate." }, { status: 401 });
        }
        if (headers["x-miniapp-session"] === "session-token-1") {
          return Response.json({ observedAt: 1700000000, sessions: [{ id: "s1" }] });
        }
      }
      return Response.json({ error: "Unexpected" }, { status: 500 });
    };

    const client = createClient(getInitData, transport);
    client.setSession("initial-stale-session");

    const result = await client("/api/state");
    expect(result).toEqual({ observedAt: 1700000000, sessions: [{ id: "s1" }] });
    expect(client.getSession()).toBe("session-token-1");

    expect(calls).toHaveLength(3);
    expect(calls[0].path).toBe("/api/state");
    expect(calls[0].headers["x-miniapp-session"]).toBe("initial-stale-session");
    expect(calls[1].path).toBe("/api/session");
    expect(calls[1].headers["x-telegram-init-data"]).toBe("init-data-version-1");
    expect(calls[2].path).toBe("/api/state");
    expect(calls[2].headers["x-miniapp-session"]).toBe("session-token-1");
  });

  test("401 on /api/approval drops cached session, re-runs /api/session, and retries approval decision once", async () => {
    const calls: { path: string; body?: unknown; headers: Record<string, string> }[] = [];
    const client = createClient(() => "fresh-launch-data", async (path: string, init?: RequestInit) => {
      const headers = (init?.headers ?? {}) as Record<string, string>;
      calls.push({ path, body: init?.body, headers });

      if (path === "/api/session") {
        return Response.json({ appSession: "new-approval-session" });
      }
      if (path === "/api/approval") {
        if (headers["x-miniapp-session"] === "stale-approval-session") {
          return Response.json({ error: "Open this app from Telegram again to authenticate." }, { status: 401 });
        }
        if (headers["x-miniapp-session"] === "new-approval-session") {
          return Response.json({ state: "approved" });
        }
      }
      return Response.json({ error: "Unexpected" }, { status: 500 });
    });

    client.setSession("stale-approval-session");
    const decision = { token: "appr-token-123", decision: "approved" };
    const res = await client("/api/approval", decision);
    expect(res).toEqual({ state: "approved" });
    expect(client.getSession()).toBe("new-approval-session");

    expect(calls).toHaveLength(3);
    expect(calls[0]).toMatchObject({ path: "/api/approval", headers: { "x-miniapp-session": "stale-approval-session" } });
    expect(calls[1]).toMatchObject({ path: "/api/session", headers: { "x-telegram-init-data": "fresh-launch-data" } });
    expect(calls[2]).toMatchObject({ path: "/api/approval", headers: { "x-miniapp-session": "new-approval-session" } });
    expect(JSON.parse(String(calls[2].body))).toEqual(decision);
  });

  test("double-401 on /api/approval drops cached session and throws TerminalAuthError without unbounded retry", async () => {
    const calls: { path: string; headers: Record<string, string> }[] = [];
    const client = createClient(() => "launch-data-approval", async (path: string, init?: RequestInit) => {
      const headers = (init?.headers ?? {}) as Record<string, string>;
      calls.push({ path, headers });

      if (path === "/api/session") return Response.json({ appSession: "new-approval-session-attempt" });
      if (path === "/api/approval") {
        return Response.json({ error: "Open this app from Telegram again to authenticate." }, { status: 401 });
      }
      return Response.json({ error: "Unexpected" }, { status: 500 });
    });

    client.setSession("stale-approval-session");

    let caughtError: unknown;
    try {
      await client("/api/approval", { token: "tok-1", decision: "approved" });
    } catch (err) {
      caughtError = err;
    }

    expect(caughtError).toBeInstanceOf(TerminalAuthError);
    expect(isTerminalAuthError(caughtError)).toBe(true);
    expect((caughtError as Error).message).toBe("Open this app from Telegram again to authenticate.");
    expect(client.getSession()).toBe("");
    expect(calls.map(c => c.path)).toEqual(["/api/approval", "/api/session", "/api/approval"]);
    expect(calls[0].headers["x-miniapp-session"]).toBe("stale-approval-session");
    expect(calls[1].headers["x-telegram-init-data"]).toBe("launch-data-approval");
    expect(calls[2].headers["x-miniapp-session"]).toBe("new-approval-session-attempt");
  });

  test("non-401 errors on /api/approval throw standard Error and do not clear session", async () => {
    const client = createClient(() => "launch-data", async (path: string) => {
      if (path === "/api/approval") return Response.json({ error: "Approval expired" }, { status: 400 });
      return Response.json({});
    });
    client.setSession("valid-session");
    let caughtError: unknown;
    try {
      await client("/api/approval", { token: "tok-expired", decision: "approved" });
    } catch (err) {
      caughtError = err;
    }
    expect(caughtError).toBeInstanceOf(Error);
    expect(caughtError).not.toBeInstanceOf(TerminalAuthError);
    expect(isTerminalAuthError(caughtError)).toBe(false);
    expect((caughtError as Error).message).toBe("Approval expired");
    expect(client.getSession()).toBe("valid-session");
  });

  test("double-401 drops session and renders explicit unavailable / re-open from Telegram state when retry fails", async () => {
    const calls: string[] = [];
    const client = createClient(() => "launch-data", async (path: string) => {
      calls.push(path);
      if (path === "/api/session") return Response.json({ appSession: "session-attempt-2" });
      if (path === "/api/state") {
        return Response.json({ error: "Open this app from Telegram again to authenticate." }, { status: 401 });
      }
      return Response.json({ error: "Unexpected" }, { status: 500 });
    });

    client.setSession("session-attempt-1");

    let caughtError: Error | undefined;
    try {
      await client("/api/state");
    } catch (err) {
      caughtError = err as Error;
    }

    expect(caughtError).toBeDefined();
    expect(caughtError?.message).toBe("Open this app from Telegram again to authenticate.");
    expect(caughtError).toBeInstanceOf(TerminalAuthError);
    expect(isTerminalAuthError(caughtError)).toBe(true);
    expect(calls).toEqual(["/api/state", "/api/session", "/api/state"]);
    expect(client.getSession()).toBe("");

    const unavailableState = getUnavailableState(caughtError?.message);
    expect(unavailableState.connection).toBe("Not connected");
    expect(unavailableState.notice).toBe("Open this app from Telegram again to authenticate.");
    expect(unavailableState.freshness).toBe("Unavailable");
    for (const section of SECTIONS) {
      expect(unavailableState.sections[section]).toBe(UNAVAILABLE_MESSAGE);
      expect(unavailableState.sections[section]).toBe("Unavailable until a secure connection is established.");
    }
  });

  test("double-401 drops session and renders explicit unavailable state when re-session exchange returns 401", async () => {
    const calls: string[] = [];
    const client = createClient(() => "expired-launch-data", async (path: string) => {
      calls.push(path);
      if (path === "/api/state") {
        return Response.json({ error: "Open this app from Telegram again to authenticate." }, { status: 401 });
      }
      if (path === "/api/session") {
        return Response.json({ error: "Open this app from Telegram again to authenticate." }, { status: 401 });
      }
      return Response.json({ error: "Unexpected" }, { status: 500 });
    });

    client.setSession("dead-session");

    let caughtError: Error | undefined;
    try {
      await client("/api/state");
    } catch (err) {
      caughtError = err as Error;
    }

    expect(caughtError).toBeDefined();
    expect(caughtError?.message).toBe("Open this app from Telegram again to authenticate.");
    expect(calls).toEqual(["/api/state", "/api/session"]);
    expect(caughtError).toBeInstanceOf(TerminalAuthError);
    expect(isTerminalAuthError(caughtError)).toBe(true);
    expect(client.getSession()).toBe("");

    const unavailableState = getUnavailableState(caughtError?.message);
    expect(unavailableState.connection).toBe("Not connected");
    expect(unavailableState.notice).toBe("Open this app from Telegram again to authenticate.");
    expect(unavailableState.freshness).toBe("Unavailable");
    for (const section of SECTIONS) {
      expect(unavailableState.sections[section]).toBe(UNAVAILABLE_MESSAGE);
    }
  });

  test("401 handler safely handles non-JSON error responses and clears session", async () => {
    const calls: string[] = [];
    const client = createClient(() => "launch", async (path: string) => {
      calls.push(path);
      if (path === "/api/session") return Response.json({ appSession: "session-ok" });
      if (path === "/api/state") return new Response("Unauthorized plain text", { status: 401 });
      return Response.json({});
    });
    client.setSession("stale");

    let caughtError: Error | undefined;
    try {
      await client("/api/state");
    } catch (err) {
      caughtError = err as Error;
    }
    expect(caughtError).toBeDefined();
    expect(caughtError?.message).toBe(REOPEN_MESSAGE);
    expect(client.getSession()).toBe("");
    expect(calls).toEqual(["/api/state", "/api/session", "/api/state"]);
  });

  test("relay serves client.js and all assets without path leak, and Dockerfile copies them", async () => {
    const secret = "e".repeat(64);
    const server = startRelay(secret, 0);
    try {
      for (const [route, file] of Object.entries(SERVED_FILES)) {
        const res = await fetch(`http://localhost:${server.port}${route}`);
        expect(res.status).toBe(200);
        expect(res.headers.get("cache-control")).toBe("no-store");
        expect(res.headers.get("content-security-policy")).toContain("default-src 'none'");
        const body = await res.text();
        expect(body.length).toBeGreaterThan(0);
        expect(body).not.toContain("ENOENT");
      }
      const missing = await fetch(`http://localhost:${server.port}/unknown-script.js`);
      expect(missing.status).toBe(404);
    } finally {
      server.stop(true);
    }

    const dockerfile = readFileSync(new URL("../miniapp/Dockerfile", import.meta.url), "utf8");
    const copyLine = dockerfile.split("\n").find(l => l.startsWith("COPY "));
    expect(copyLine).toBeDefined();
    for (const file of Object.values(SERVED_FILES)) {
      expect(copyLine).toContain(file);
    }
  });
});
