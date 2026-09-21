import { timingSafeEqual } from "node:crypto";
import type { ServerWebSocket } from "bun";

export function authorizedRelay(header: string | null, secret: string): boolean {
  if (secret.length < 43 || !header) return false;
  const actual = Buffer.from(header);
  const expected = Buffer.from(`Bearer ${secret}`);
  return actual.length === expected.length && timingSafeEqual(actual, expected);
}

export const SERVED_FILES: Record<string, string> = { "/": "index.html", "/app.js": "app.js", "/client.js": "client.js", "/style.css": "style.css" };

export function startRelay(secret: string, port = 3000) {
  if (secret.length < 43) throw new Error("RELAY_SECRET must contain at least 43 characters");
  let daemon: ServerWebSocket<undefined> | undefined;
  const pending = new Map<string, { finish: (response: Response) => void; timer: Timer }>();
  const buckets = new Map<string, { tokens: number; at: number }>();
  const unavailable = () => Response.json({ error: "Local daemon unavailable" }, { status: 503 });
  return Bun.serve<undefined>({
    port,
    maxRequestBodySize: 16384,
    async fetch(request, server) {
      const url = new URL(request.url);
      if (url.pathname === "/relay") {
        if (!authorizedRelay(request.headers.get("authorization"), secret)) return new Response("Unauthorized", { status: 401 });
        if (daemon) return new Response("Daemon already connected", { status: 409 });
        if (server.upgrade(request, { data: undefined })) return;
        return new Response("WebSocket required", { status: 400 });
      }
      if (url.pathname.startsWith("/api/")) {
        const initData = request.headers.get("x-telegram-init-data") ?? "";
        const appSession = request.headers.get("x-miniapp-session") ?? "";
        const launch = new URLSearchParams(initData);
        const shaped = url.pathname === "/api/session"
          ? request.method === "POST" && initData.length <= 16384 && /^[a-f0-9]{64}$/.test(launch.get("hash") ?? "") && /^\d+$/.test(launch.get("auth_date") ?? "") && Boolean(launch.get("user"))
          : /^\d+\.\d+\.[a-f0-9]{64}$/.test(appSession);
        if (!shaped) return Response.json({ error: "Open this app from Telegram again to authenticate." }, { status: 401 });
        // Traefik appends the actual peer to X-Forwarded-For; never trust its first entry.
        const ip = request.headers.get("x-forwarded-for")?.split(",").at(-1)?.trim() || server.requestIP(request)?.address || "unknown";
        const now = Date.now();
        const bucket = buckets.get(ip) ?? { tokens: 8, at: now };
        bucket.tokens = Math.min(8, bucket.tokens + (now - bucket.at) / 1000);
        bucket.at = now;
        if (buckets.size >= 2048 && !buckets.has(ip)) {
          const oldest = buckets.keys().next().value;
          if (oldest) buckets.delete(oldest);
        }
        buckets.set(ip, bucket);
        if (bucket.tokens < 1) return Response.json({ error: "Too many requests; retry shortly." }, { status: 429 });
        bucket.tokens--;
        if (!daemon) return unavailable();
        if (pending.size >= 64) return Response.json({ error: "Relay busy; retry shortly." }, { status: 429 });
        const id = crypto.randomUUID();
        const body = await request.text();
        return new Promise<Response>(resolve => {
          const timer = setTimeout(() => { pending.delete(id); resolve(unavailable()); }, 10000);
          pending.set(id, { finish: resolve, timer });
          daemon?.send(JSON.stringify({ id, path: url.pathname, method: request.method, initData, appSession, body }));
        });
      }
      const file = SERVED_FILES[url.pathname];
      if (!file) return new Response("Not found", { status: 404 });
      const asset = Bun.file(new URL(file, import.meta.url));
      if (!(await asset.exists())) return new Response("Not found", { status: 404 });
      return new Response(asset, { headers: { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'none'; script-src 'self' https://telegram.org; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'" } });
    },
    websocket: {
      maxPayloadLength: 1048576,
      idleTimeout: 60,
      open(socket) { daemon = socket; },
      message(socket, message) {
        if (socket !== daemon) return;
        try {
          const result = JSON.parse(String(message));
          const entry = pending.get(result.id);
          if (!entry) return;
          pending.delete(result.id); clearTimeout(entry.timer);
          entry.finish(Response.json(result.data, { status: Number.isInteger(result.status) && result.status >= 200 && result.status <= 599 ? result.status : 502, headers: { "Cache-Control": "no-store" } }));
        } catch { socket.close(1008, "Invalid response"); }
      },
      close(socket) {
        if (socket !== daemon) return;
        daemon = undefined;
        for (const entry of pending.values()) { clearTimeout(entry.timer); entry.finish(unavailable()); }
        pending.clear();
      },
    },
  });
}
if (import.meta.main) startRelay(process.env.RELAY_SECRET ?? "", Number(process.env.PORT ?? 3000));
