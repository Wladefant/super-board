/**
 * Week view server (issue #477, deployed by #479): static UI files from this directory, the shared
 * mini-app auth glue, `/api/*` forwarded to the PC daemon and the daemon's `/relay` WebSocket.
 * It reads PORT (default 8080) and RELAY_SECRET (43+ chars) from the environment.
 */
import { startRelay, type FileResolver } from "../miniapp/relay";

const STATIC_NAME = /^\/[A-Za-z0-9][A-Za-z0-9._-]*\.(html|js|css|json|svg)$/;

/** Allow-list: `/` is index.html, any flat file in this directory with a web extension, and the shared mini-app client. */
export const resolveWeekFile: FileResolver = pathname => {
  if (pathname === "/") return "index.html";
  if (pathname === "/miniapp/client.js") return "../miniapp/client.js";
  return STATIC_NAME.test(pathname) && !pathname.includes("..") ? pathname.slice(1) : undefined;
};

export function startWeekServer(secret: string, port: number, baseDir: URL | string = import.meta.url) {
  return startRelay(secret, port, baseDir, resolveWeekFile);
}

if (import.meta.main) startWeekServer(process.env.RELAY_SECRET ?? "", Number(process.env.PORT ?? 8080));
