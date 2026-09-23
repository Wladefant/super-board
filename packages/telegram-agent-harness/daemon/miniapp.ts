import * as fs from "node:fs";
import * as path from "node:path";
import { authenticateInitData, authenticateAppSession, issueAppSession } from "./miniapp-auth";

export interface MiniAppRequest { id: string; path: string; method: string; initData: string; appSession?: string; body: string }
export interface MiniAppOptions {
  stateDir: string; token: string; allowedUsers: string[];
  session: (userId: string, context?: { sessionId?: string; topicId?: string }) => string | null;
  sessions: () => Promise<unknown>;
  dashboard: (userId: string, sessionId?: string | null) => unknown;
  status: () => unknown;
}
export async function miniAppRequest(request: MiniAppRequest, options: MiniAppOptions) {
  let user: string;
  let reqPath = request.path;
  let queryParams: URLSearchParams | undefined;
  const qIndex = reqPath.indexOf("?");
  if (qIndex >= 0) {
    queryParams = new URLSearchParams(reqPath.slice(qIndex + 1));
    reqPath = reqPath.slice(0, qIndex);
  }
  try {
    if (reqPath === "/api/session" && request.method === "POST") {
      user = authenticateInitData(request.initData, options.token, options.allowedUsers);
      return { id: request.id, status: 200, data: { appSession: issueAppSession(user, options.token), expiresIn: 28800 } };
    }
    user = authenticateAppSession(request.appSession ?? "", options.token, options.allowedUsers);
  }
  catch { return { id: request.id, status: 401, data: { error: "Open this app from Telegram again to authenticate." } }; }
  const respond = (status: number, data: unknown) => ({ id: request.id, status, data });
  try {
    const requestedSessionId = queryParams?.get("sessionId") || undefined;
    const requestedTopicId = queryParams?.get("topicId") || undefined;
    const session = options.session(user, { sessionId: requestedSessionId, topicId: requestedTopicId });
    if (reqPath === "/api/state" && request.method === "GET") {
      let sessions: unknown = null;
      try { sessions = await options.sessions(); } catch { /* unavailable is explicit in the response */ }
      return respond(200, {
        observedAt: Date.now(),
        session,
        sessions,
        status: options.status(),
        dashboard: options.dashboard(user, session),
      });
    }
    if (reqPath === "/api/approval" && request.method === "POST") {
      return respond(410, { error: "Telegram tool-call approvals have been removed. No operation was authorized or executed." });
    }
    return respond(404, { error: "Not found" });
  } catch { return respond(409, { error: "Request unavailable, expired, already decided, or not authorized for this session." }); }
}

/** Config is local to a slot; the daemon owns reconnect and shutdown with its poller. */
export function connectMiniApp(options: MiniAppOptions): () => void {
  const configPath = path.join(options.stateDir, "miniapp.json");
  if (!fs.existsSync(configPath)) return () => {};
  let config: { url: string; secret: string };
  try {
    const parsed = JSON.parse(fs.readFileSync(configPath, "utf8"));
    if (!parsed || typeof parsed.url !== "string" || typeof parsed.secret !== "string" || parsed.secret.length < 43) return () => {};
    const url = new URL(parsed.url);
    if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) return () => {};
    config = { url: url.origin, secret: parsed.secret };
  } catch { return () => {}; }
  let stopped = false;
  let socket: WebSocket | undefined;
  let retry: Timer | undefined;
  let heartbeat: Timer | undefined;
  const connect = () => {
    if (stopped) return;
    socket = new WebSocket(`${config.url.replace(/\/$/, "").replace(/^https:/, "wss:")}/relay`, { headers: { Authorization: `Bearer ${config.secret}` } });
    socket.onopen = () => { heartbeat = setInterval(() => socket?.send('{}'), 20000); };
    socket.onmessage = async event => {
      const current = socket;
      try {
        const request = JSON.parse(String(event.data)) as MiniAppRequest;
        if (typeof request.id !== "string" || typeof request.initData !== "string") return;
        const response = await miniAppRequest(request, options);
        if (current?.readyState === WebSocket.OPEN) current.send(JSON.stringify(response));
      } catch { /* malformed transport messages never execute a command */ }
    };
    socket.onerror = () => socket?.close();
    socket.onclose = () => { clearInterval(heartbeat); if (!stopped) retry = setTimeout(connect, 5000); };
  };
  connect();
  return () => { stopped = true; clearTimeout(retry); clearInterval(heartbeat); socket?.close(); };
}

export function miniAppUrl(stateDir: string): string | null {
  try {
    const { url } = JSON.parse(fs.readFileSync(path.join(stateDir, "miniapp.json"), "utf8"));
    return typeof url === "string" && /^https:\/\//.test(url) ? url : null;
  } catch { return null; }
}
