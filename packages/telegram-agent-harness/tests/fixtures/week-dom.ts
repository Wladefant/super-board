// Loads the real `week/index.html` and `week/week.js` into a real DOM (happy-dom), with a real HTTP
// stub behind `fetch`. The page uses origin-relative paths as in a browser; resolving them is the
// only shim. Each call evaluates week.js afresh, so several test files can open their own page.
import { Window } from "happy-dom";
import { readFileSync } from "node:fs";

const GLOBALS = ["window", "document", "location", "history", "localStorage", "fetch", "setInterval", "innerWidth", "innerHeight"] as const;
let opened = 0;

export interface WeekPage {
  window: Window;
  /** Settles every request the page started, then yields so their continuations render. */
  quiescent(): Promise<void>;
  close(): Promise<void>;
}

export async function openWeekPage(handler: (request: Request) => Response | Promise<Response>, path = "/"): Promise<WeekPage> {
  const server = Bun.serve({ port: 0, fetch: handler });
  const origin = `http://localhost:${server.port}`;
  const window = new Window({
    url: `${origin}${path}`,
    width: 390,
    height: 844,
    settings: { disableJavaScriptEvaluation: true, disableJavaScriptFileLoading: true, disableCSSFileLoading: true },
  });
  const g = globalThis as Record<string, unknown>;
  const saved = Object.fromEntries(GLOBALS.map(name => [name, g[name]]));
  const timers: Timer[] = [];
  const inFlight = new Set<Promise<unknown>>();
  const nativeFetch = saved.fetch as typeof fetch;
  const nativeSetInterval = saved.setInterval as typeof setInterval;

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

  window.document.write(readFileSync(new URL("../../week/index.html", import.meta.url), "utf8"));
  Object.assign(g, {
    window,
    document: window.document,
    location: window.location,
    history: window.history,
    localStorage: window.localStorage,
    innerWidth: window.innerWidth,
    innerHeight: window.innerHeight,
    fetch: (input: string | URL | Request, init?: RequestInit) => {
      const pending = nativeFetch(new URL(String(input instanceof Request ? input.url : input), origin), init);
      inFlight.add(pending);
      return pending.finally(() => inFlight.delete(pending));
    },
    // The 2-minute refresh never fires inside a test, but it must not outlive it.
    setInterval: (handler: () => void, timeout?: number) => {
      const timer = nativeSetInterval(handler, timeout);
      timers.push(timer);
      return timer;
    },
  });
  // Dynamic on purpose: week.js reads window, document and location as it evaluates, so a static
  // import would run it before the DOM globals above exist. The query makes each page a new module.
  opened += 1;
  await import(`../../week/week.js?page=${opened}`);
  await quiescent();

  return {
    window,
    quiescent,
    async close() {
      for (const timer of timers) clearInterval(timer);
      Object.assign(g, saved);
      await window.happyDOM.close();
      server.stop(true);
    },
  };
}
