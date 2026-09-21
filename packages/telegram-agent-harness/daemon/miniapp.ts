import * as fs from "node:fs";
import * as path from "node:path";
import { authenticateInitData, authenticateAppSession, issueAppSession, appSessionRevocation } from "./miniapp-auth";
import { decideApproval, pendingApprovals } from "../extension/approvals";

export interface MiniAppRequest { id: string; path: string; method: string; initData: string; appSession?: string; body: string }
export interface MiniAppOptions {
  stateDir: string; token: string; allowedUsers: string[];
  session: (userId: string) => string | null;
  sessions: () => Promise<unknown>;
  dashboard: (userId: string) => unknown;
  status: () => unknown;
}
export async function miniAppRequest(request: MiniAppRequest, options: MiniAppOptions) {
  let user: string;
  try {
    if (request.path === "/api/session" && request.method === "POST") {
      user = authenticateInitData(request.initData, options.token, options.allowedUsers);
      return { id: request.id, status: 200, data: { appSession: issueAppSession(user, options.token), expiresIn: 28800 } };
    }
    user = authenticateAppSession(request.appSession ?? "", options.token, options.allowedUsers);
    if (appSessionRevocation(options.stateDir, request.appSession!)) throw new Error("Unauthorized");
  }
  catch { return { id: request.id, status: 401, data: { error: "Open this app from Telegram again to authenticate." } }; }
  const respond = (status: number, data: unknown) => ({ id: request.id, status, data });
  try {
    if (request.path === "/api/logout" && request.method === "POST") {
      appSessionRevocation(options.stateDir, request.appSession!, true);
      return respond(200, { revoked: true });
    }
    const session = options.session(user);
    if (request.path === "/api/state" && request.method === "GET") {
      let sessions: unknown = null;
      try { sessions = await options.sessions(); } catch { /* unavailable is explicit in the response */ }
      return respond(200, { observedAt: Date.now(), session, sessions, status: options.status(), dashboard: options.dashboard(user), approvals: session ? pendingApprovals(options.stateDir, session) : null });
    }
    if (request.path === "/api/approval" && request.method === "POST") {
      if (!session) return respond(409, { error: "Chat is not bound to a session." });
      let body: { token?: string; decision?: string } | null = null;
      try { body = JSON.parse(request.body); } catch { return respond(400, { error: "Invalid request body" }); }
      if (!body || typeof body !== "object" || (body.decision !== "approved" && body.decision !== "denied")) return respond(400, { error: "Invalid decision" });
      const result = decideApproval(options.stateDir, body.token, body.decision, { sessionId: session, userId: user, chatId: user });
      return respond(200, { state: result.state });
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
