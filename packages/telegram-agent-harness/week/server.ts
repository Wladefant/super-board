/**
 * Week view server (issue #477, deployed by #479): static UI files from this directory, the shared
 * mini-app auth glue, `/api/*` forwarded to the PC daemon and the daemon's `/relay` WebSocket.
 * It reads PORT (default 8080) and RELAY_SECRET (43+ chars) from the environment.
 */
import { startRelay, type FileResolver } from "../miniapp/relay";

const STATIC_NAME = /^\/[A-Za-z0-9][A-Za-z0-9._-]*\.(html|js|css|svg)$/;

/** boards.json names private repos, so it is answered from `/api/boards` after the app-session check, never as a static file. */
export const WEEK_AUTHENTICATED: Readonly<Record<string, string>> = { "/boards.json": "/api/boards" };

/** Allow-list: `/` is index.html, any flat file in this directory with a public web extension, and the shared mini-app client. */
export const resolveWeekFile: FileResolver = pathname => {
  if (pathname === "/") return "index.html";
  if (pathname === "/miniapp/client.js") return "../miniapp/client.js";
  return STATIC_NAME.test(pathname) && !pathname.includes("..") ? pathname.slice(1) : undefined;
};

export function startWeekServer(secret: string, port: number, baseDir: URL | string = import.meta.url) {
  return startRelay(secret, port, baseDir, resolveWeekFile, WEEK_AUTHENTICATED);
}

if (import.meta.main) startWeekServer(process.env.RELAY_SECRET ?? "", Number(process.env.PORT ?? 8080));
