// Loads the real `week/index.html` into a real DOM (happy-dom), with a real HTTP stub behind `fetch`, and
// runs its scripts in browser order: the page's own classic scripts as they parse, then `week/week.js`.
// The page uses origin-relative paths as in a browser; resolving them is the only shim. Stylesheets are
// inlined, so computed custom properties are week.css's. Each call evaluates the scripts afresh, so
// several test files can open their own page.
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

export interface WeekPageOptions {
  /** Stands in for `window.Telegram.WebApp` (telegram-web-app.js); omitted = a plain browser. */
  telegram?: object;
  /** The system scheme the page sees through `prefers-color-scheme`. */
  prefersColorScheme?: "light" | "dark";
  /** "classic" stops before the module week.js: the state of the page at its first paint. Default "all". */
  scripts?: "classic" | "all";
}

const INDEX = new URL("../../week/index.html", import.meta.url);

/** The page's own classic scripts (`<script src>` without type="module"), in document order. */
function classicScripts(html: string): string[] {
  return [...html.matchAll(/<script\b([^>]*)><\/script>/g)]
    .map(m => m[1])
    .filter(attrs => !/type="module"/.test(attrs))
    .map(attrs => /src="([^"]+)"/.exec(attrs)?.[1] ?? "")
    .filter(src => src && !/^https?:/.test(src))
    .map(src => readFileSync(new URL(src, INDEX), "utf8"));
}

/** index.html with each local stylesheet link replaced by its contents in a <style>. */
function inlineStyles(html: string): string {
  return html.replace(/<link rel="stylesheet" href="([^"]+)">/g, (_, href) => `<style>${readFileSync(new URL(href, INDEX), "utf8")}</style>`);
}

export async function openWeekPage(handler: (request: Request) => Response | Promise<Response>, path = "/", options: WeekPageOptions = {}): Promise<WeekPage> {
  const server = Bun.serve({ port: 0, fetch: handler });
  const origin = `http://localhost:${server.port}`;
  const window = new Window({
    url: `${origin}${path}`,
    width: 390,
    height: 844,
    settings: {
      disableJavaScriptEvaluation: true, disableJavaScriptFileLoading: true, disableCSSFileLoading: true,
      ...(options.prefersColorScheme ? { device: { prefersColorScheme: options.prefersColorScheme } } : {}),
    },
  });
  if (options.telegram) Object.assign(window, { Telegram: { WebApp: options.telegram } });
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

  const html = readFileSync(INDEX, "utf8");
  window.document.write(inlineStyles(html));
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
  // telegram-web-app.js is the stand-in above. The page's classic scripts run against the globals as a
  // browser runs them while it parses <head>, before the first paint.
  for (const source of classicScripts(html)) new Function(source)();
  // Dynamic on purpose: week.js reads window, document and location as it evaluates, so a static
  // import would run it before the DOM globals above exist. The query makes each page a new module.
  if (options.scripts !== "classic") {
    opened += 1;
    await import(`../../week/week.js?page=${opened}`);
    await quiescent();
  }

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
