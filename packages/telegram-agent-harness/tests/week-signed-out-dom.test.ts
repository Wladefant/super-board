// The signed-out Week page shows its notice and no literal "null" or "undefined" (Refs #480, PR #541).
//
// This loads the real `week/week.js` module into a real DOM (happy-dom) built from the real
// `week/index.html`, with a real HTTP stub behind `fetch`. With no Telegram init data the
// session request is refused, so the page renders its signed-out state. `replaceChildren(null)`
// in that path once painted the text "null" next to the notice; this test fails on it.
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { Window } from "happy-dom";
import { readFileSync } from "node:fs";

const server = Bun.serve({
  port: 0,
  fetch(request) {
    const path = new URL(request.url).pathname;
    // Signed out: no init data, so no app session and no week.
    if (path === "/api/session" || path === "/api/week") return Response.json({ error: "Missing Telegram init data." }, { status: 401 });
    return Response.json({ error: "Unexpected" }, { status: 500 });
  },
});

const origin = `http://localhost:${server.port}`;
const window = new Window({
  url: `${origin}/`,
  settings: { disableJavaScriptEvaluation: true, disableJavaScriptFileLoading: true, disableCSSFileLoading: true },
});

const GLOBALS = ["window", "document", "location", "history", "localStorage", "fetch", "setInterval"] as const;
const g = globalThis as Record<string, unknown>;
const saved = Object.fromEntries(GLOBALS.map(name => [name, g[name]]));
const timers: Timer[] = [];
const inFlight = new Set<Promise<unknown>>();

/** Settles every request the page started, then yields so their continuations render. */
async function quiescent() {
  for (let pass = 0; pass < 50; pass += 1) {
    while (inFlight.size) await Promise.allSettled([...inFlight]);
    const turn = Promise.withResolvers<void>();
    setImmediate(turn.resolve);
    await turn.promise;
    if (!inFlight.size) return;
  }
  throw new Error("the week page never stopped issuing requests");
}

beforeAll(async () => {
  window.document.write(readFileSync(new URL("../week/index.html", import.meta.url), "utf8"));
  const nativeFetch = saved.fetch as typeof fetch;
  const nativeSetInterval = saved.setInterval as typeof setInterval;
  Object.assign(g, {
    window,
    document: window.document,
    location: window.location,
    history: window.history,
    localStorage: window.localStorage,
    // The page uses origin-relative paths as in a browser; resolving them is the only shim.
    fetch: (input: string | URL | Request, init?: RequestInit) => {
      const pending = nativeFetch(new URL(String(input instanceof Request ? input.url : input), origin), init);
      inFlight.add(pending);
      return pending.finally(() => inFlight.delete(pending));
    },
    // The 2-minute refresh never fires inside this test, but it must not outlive it.
    setInterval: (handler: () => void, timeout?: number) => {
      const timer = nativeSetInterval(handler, timeout);
      timers.push(timer);
      return timer;
    },
  });
  // Dynamic on purpose: week.js reads window, document and location as it evaluates, so a static
  // import would run it before the DOM globals above exist.
  await import("../week/week.js");
  await quiescent();
});

afterAll(async () => {
  for (const timer of timers) clearInterval(timer);
  Object.assign(g, saved);
  await window.happyDOM.close();
  server.stop(true);
});

describe("signed-out week page", () => {
  test("shows the sign-in notice and no 'null' or 'undefined' text anywhere", () => {
    const notice = window.document.getElementById("notice")!;
    expect(notice.hidden).toBe(false);
    expect(notice.className).toContain("notice-error");
    // Signing in again is the only way out, so there is no retry button.
    expect(notice.querySelector("button")).toBeNull();
    expect(notice.textContent).toBe(notice.querySelector("span")!.textContent);
    expect(window.document.getElementById("agenda")!.textContent).toBe("Sign in to see the week.");

    const text = window.document.body.textContent ?? "";
    expect(text).not.toMatch(/\bnull\b/i);
    expect(text).not.toMatch(/\bundefined\b/i);
  });
});
