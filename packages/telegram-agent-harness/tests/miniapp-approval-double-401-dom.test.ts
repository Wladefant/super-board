// Regression guard for the Mini App approval double-401 path (Refs #128).
//
// This exercises the real `miniapp/app.js` ES module — not a re-implementation of
// its logic — loaded against a real DOM (happy-dom) with the real markup from
// `miniapp/index.html`, and a real HTTP stub for `/api`. The client therefore uses
// its default `fetch` transport over a real socket, and the request log below is
// what the server actually received.
//
// The invariant under test: when an approval decision hits
// POST /api/approval 401 -> POST /api/session 200 -> POST /api/approval 401,
// app.js must render the terminal "unavailable" state and stop. Calling refresh()
// from that catch re-authenticates and paints a connected UI over a dead session,
// which is the loop this test must fail on.
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { Window } from "happy-dom";
import { readFileSync } from "node:fs";
import { REOPEN_MESSAGE, SECTIONS, UNAVAILABLE_MESSAGE } from "../miniapp/client.js";

const INIT_DATA = "query_id=dom-test&user=%7B%22id%22%3A1247617658%7D&auth_date=1700000000&hash=deadbeef";

type StubRequest = { method: string; path: string; session: string | null; initData: string | null };

const requests: StubRequest[] = [];
let approvalCalls = 0;
let sessionCalls = 0;

const statePayload = {
  status: { polling: true },
  session: "session-alpha",
  sessions: [{ id: "session-alpha", title: "Superboard lane", cwd: "C:/lanes/miniapp-401" }],
  approvals: [
    {
      token: "approval-token-1",
      requester: "LaneA",
      command: "git push origin HEAD",
      reason: "push lane branch",
      expiresAt: new Date("2026-01-01T00:00:00.000Z").toISOString(),
      approvable: true,
    },
  ],
  dashboard: {
    observedAt: Date.now(),
    lanes: [{ name: "LaneA", task: "miniapp 401", state: "active" }],
    blockers: [],
    mergeQueue: [],
  },
};

const server = Bun.serve({
  port: 0,
  fetch(request) {
    const url = new URL(request.url);
    requests.push({
      method: request.method,
      path: url.pathname,
      session: request.headers.get("x-miniapp-session"),
      initData: request.headers.get("x-telegram-init-data"),
    });
    if (url.pathname === "/api/session") {
      sessionCalls += 1;
      return Response.json({ appSession: `app-session-${sessionCalls}` });
    }
    if (url.pathname === "/api/state") {
      if (!request.headers.get("x-miniapp-session")) return Response.json({ error: REOPEN_MESSAGE }, { status: 401 });
      return Response.json(statePayload);
    }
    if (url.pathname === "/api/approval") {
      approvalCalls += 1;
      // Every approval decision is rejected, so the retry inside client.js
      // exhausts itself and app.js has to handle a terminal auth failure.
      return Response.json({ error: REOPEN_MESSAGE }, { status: 401 });
    }
    return Response.json({ error: "Unexpected" }, { status: 500 });
  },
});

const origin = `http://localhost:${server.port}`;
const window = new Window({
  url: `${origin}/`,
  settings: { disableJavaScriptEvaluation: true, disableJavaScriptFileLoading: true, disableCSSFileLoading: true },
});

const savedGlobals = {
  window: (globalThis as Record<string, unknown>).window,
  document: (globalThis as Record<string, unknown>).document,
  fetch: globalThis.fetch,
  setInterval: globalThis.setInterval,
};
const appTimers: Timer[] = [];
const inFlight = new Set<Promise<unknown>>();

function byId(id: string) {
  const node = window.document.getElementById(id);
  if (!node) throw new Error(`missing #${id} in miniapp/index.html`);
  return node;
}

// app.js fires its refresh/approval handlers without exposing a completion signal,
// so "the app is done reacting" is observed rather than waited out: settle every
// request already in flight, yield one macrotask so the promise continuations that
// consume those responses run, and repeat until no new request appears. A stray
// refresh() therefore shows up as another in-flight request instead of needing a
// wall-clock delay long enough to have caught it.
async function quiescent() {
  for (let pass = 0; pass < 50; pass += 1) {
    while (inFlight.size) await Promise.allSettled([...inFlight]);
    const turn = Promise.withResolvers<void>();
    setImmediate(turn.resolve);
    await turn.promise;
    if (!inFlight.size) return;
  }
  throw new Error(`miniapp never stopped issuing requests: ${JSON.stringify(requests)}`);
}

beforeAll(async () => {
  window.document.write(readFileSync(new URL("../miniapp/index.html", import.meta.url), "utf8"));
  (window as unknown as Record<string, unknown>).Telegram = {
    WebApp: { initData: INIT_DATA, ready() {}, expand() {} },
  };

  const nativeFetch = globalThis.fetch;
  // app.js -> client.js resolves its default transport to the global `fetch` and
  // issues origin-relative paths exactly as a browser page would. Resolving them
  // against the stub's origin is the only shim: the call itself is real HTTP.
  globalThis.fetch = ((input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(String(input instanceof Request ? input.url : input), origin);
    const pending = nativeFetch(url, init);
    inFlight.add(pending);
    return pending.finally(() => inFlight.delete(pending));
  }) as typeof fetch;
  // The app's own 15s poll never fires inside this test, but it must not outlive it.
  globalThis.setInterval = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) => {
    const timer = savedGlobals.setInterval(handler as () => void, timeout, ...args);
    appTimers.push(timer);
    return timer;
  }) as unknown as typeof globalThis.setInterval;
  (globalThis as Record<string, unknown>).window = window;
  (globalThis as Record<string, unknown>).document = window.document;

  // Dynamic import is required, not incidental: app.js reads window.Telegram, the
  // DOM and fetch during module evaluation, so it must load after the globals above
  // are installed. A static import would hoist above this setup.
  await import("../miniapp/app.js");
  await quiescent();
});

afterAll(async () => {
  for (const timer of appTimers) clearInterval(timer);
  globalThis.fetch = savedGlobals.fetch;
  globalThis.setInterval = savedGlobals.setInterval;
  (globalThis as Record<string, unknown>).window = savedGlobals.window;
  (globalThis as Record<string, unknown>).document = savedGlobals.document;
  await window.happyDOM.close();
  server.stop(true);
});

describe("real miniapp/app.js in a real DOM: approval double-401", () => {
  test("renders live approvals from the stub daemon before any failure", () => {
    expect([...window.document.querySelectorAll("#approvals button")].map(b => b.textContent)).toEqual(["Yes", "No"]);
    expect(byId("approvals").textContent).toContain("LaneA");
    expect(byId("connection").textContent).toBe("Daemon connected · Bot polling");
    expect(byId("notice").textContent).toBe("");
    expect(requests.map(r => `${r.method} ${r.path}`)).toEqual(["POST /api/session", "GET /api/state"]);
    expect(requests[0].initData).toBe(INIT_DATA);
    expect(requests[1].session).toBe("app-session-1");
  });

  test("approval 401 -> session 200 -> approval 401 renders the unavailable state and stops", async () => {
    const before = requests.length;
    (window.document.querySelectorAll("#approvals button")[0] as unknown as HTMLElement).click();
    await quiescent();

    const afterClick = requests.slice(before);
    expect(afterClick.map(r => `${r.method} ${r.path}`)).toEqual([
      "POST /api/approval",
      "POST /api/session",
      "POST /api/approval",
    ]);
    expect(afterClick[0].session).toBe("app-session-1");
    expect(afterClick[1].initData).toBe(INIT_DATA);
    expect(afterClick[2].session).toBe("app-session-2");
    // The terminal-auth catch must neither re-read state nor re-authenticate.
    expect(afterClick.filter(r => r.path === "/api/state")).toEqual([]);
    expect(approvalCalls).toBe(2);
    expect(sessionCalls).toBe(2);

    expect(byId("connection").textContent).toBe("Not connected");
    expect(byId("notice").textContent).toBe(REOPEN_MESSAGE);
    expect(byId("notice").textContent).toBe("Open this app from Telegram again to authenticate.");
    expect(byId("freshness").textContent).toBe("Unavailable");
    for (const section of SECTIONS) {
      expect(byId(section).textContent).toBe(UNAVAILABLE_MESSAGE);
      expect(byId(section).querySelectorAll("article")).toHaveLength(0);
      expect(byId(section).querySelectorAll("button")).toHaveLength(0);
    }
  });
});
