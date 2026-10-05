// Local preview of the Week view against the typed fixture, for UI work and screenshot evidence.
// Not the production server (week/server.ts). Serves the static bundle the same way: week/* at /,
// miniapp/client.js at /miniapp/client.js, POST /api/session and GET /api/week.
//
//   bun scripts/week-preview.ts [--port 4790] [--root <package dir to serve>]
//
// The page URL picks the fixture: ?scenario=normal|empty|stale|busy|error|loading (read from Referer).
// GET /__served (and /api/version, which the Flow QA runner reads) returns the git SHA of the served tree,
// so captures and FLOW-QA receipts prove what they show.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, statSync } from "node:fs";
import { extname, join, resolve } from "node:path";
import { makeWeek, type WeekScenario } from "../tests/fixtures/week-fixture";

const args = process.argv.slice(2);
const flag = (name: string, fallback: string) => {
  const i = args.indexOf(name);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const port = Number(flag("--port", "4790"));
const root = resolve(flag("--root", join(import.meta.dir, "..")));

const git = (...a: string[]) => execFileSync("git", a, { cwd: root, encoding: "utf8", windowsHide: true }).trim();
const sha = git("rev-parse", "HEAD");
const dirty = git("status", "--porcelain", "--", ".").length > 0;

const TYPES: Record<string, string> = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml" };
const SCENARIOS: Record<string, true> = { normal: true, empty: true, stale: true, busy: true, error: true, loading: true };

function scenarioOf(req: Request): string {
  const referer = req.headers.get("referer");
  const value = referer ? new URL(referer).searchParams.get("scenario") || "normal" : "normal";
  return SCENARIOS[value] ? value : "normal";
}

function file(path: string): Response {
  if (!existsSync(path) || !statSync(path).isFile() || !TYPES[extname(path)]) {
    return new Response(`Not found at ${sha.slice(0, 12)}: this tree has no Week view at that path.`, { status: 404, headers: { "content-type": "text/plain; charset=utf-8" } });
  }
  return new Response(readFileSync(path), { headers: { "content-type": TYPES[extname(path)] } });
}

const server = Bun.serve({
  port,
  hostname: "127.0.0.1",
  async fetch(req) {
    const url = new URL(req.url);
    const headers = { "x-served-sha": sha };
    const withSha = (res: Response) => { res.headers.set("x-served-sha", sha); return res; };
    if (url.pathname === "/__served" || url.pathname === "/api/version") return Response.json({ sha, dirty, root }, { headers });
    if (url.pathname === "/api/session" && req.method === "POST") return Response.json({ appSession: "preview" }, { headers });
    if (url.pathname === "/api/week") {
      // no-store like the relay. "loading" sends its headers and one space, then never ends the body:
      // a never-answered request would hold Chrome's cache lock on /api/week and stall the next page's
      // request for up to 20 s. Bun sends the headers with the first chunk, hence the space.
      const api = { ...headers, "cache-control": "no-store" };
      const scenario = scenarioOf(req);
      if (scenario === "loading") {
        const body = new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode(" ")); } });
        return new Response(body, { headers: { ...api, "content-type": "application/json" } });
      }
      if (scenario === "error") return Response.json({ error: "The relay is not connected." }, { status: 502, headers: api });
      return Response.json(makeWeek(scenario as WeekScenario), { headers: api });
    }
    if (url.pathname === "/miniapp/client.js") return withSha(file(join(root, "miniapp", "client.js")));
    const name = url.pathname === "/" ? "index.html" : url.pathname.slice(1);
    if (name.includes("/") || name.includes("..")) return withSha(new Response("Not found", { status: 404 }));
    return withSha(file(join(root, "week", name)));
  },
});

console.log(`week preview on http://127.0.0.1:${server.port} serving ${root} at ${sha}${dirty ? " (dirty)" : ""}`);
