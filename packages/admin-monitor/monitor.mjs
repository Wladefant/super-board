// Second-site down monitor. Runs as a Cloudflare Worker cron (every 5 minutes) so it can
// report that the Dokploy host itself is down. Dokploy's own monitoring covers RAM, CPU,
// containers and build errors; this file only covers "is it reachable from outside".
//
// Rules: 2 consecutive failures -> one DOWN message; first success after DOWN -> one UP message.
// State per target lives in KV (key "t:<name>"). Never logs or sends secrets.

export const FAILS_BEFORE_DOWN = 2;
export const TIMEOUT_MS = 10_000;

/** A target is up when it answers below 500. 401/403/404 mean a live app behind auth or a bare API. */
export function isUp(status) {
  return Number.isInteger(status) && status > 0 && status < 500;
}

/** Pure state machine. Returns the next state and the message kind to send (or null). */
export function step(prev, up, now) {
  const s = { fails: 0, down: false, since: 0, ...(prev || {}) };
  if (up) {
    const recovered = s.down;
    const downSince = s.since;
    return { state: { fails: 0, down: false, since: 0 }, notify: recovered ? { kind: "up", downSince } : null };
  }
  const fails = s.fails + 1;
  if (!s.down && fails >= FAILS_BEFORE_DOWN) {
    return { state: { fails, down: true, since: now }, notify: { kind: "down", fails } };
  }
  return { state: { fails, down: s.down, since: s.since }, notify: null };
}

export function escapeHtml(v) {
  return String(v).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function adminLink(target, adminBase) {
  if (target.admin) return target.admin;
  return `${adminBase}/services/${encodeURIComponent(target.name)}`;
}

export function formatMessage(target, notify, adminBase, now) {
  const link = adminLink(target, adminBase);
  const name = escapeHtml(target.name);
  if (notify.kind === "down") {
    return `<b>DOWN</b> ${name}\n${escapeHtml(target.url)}\n<a href="${link}">Open in admin</a>`;
  }
  const mins = Math.max(1, Math.round((now - notify.downSince) / 60000));
  return `<b>RECOVERED</b> ${name} after about ${mins} min\n<a href="${link}">Open in admin</a>`;
}

async function probe(url, fetchImpl) {
  try {
    const res = await fetchImpl(url, { method: "GET", redirect: "manual", signal: AbortSignal.timeout(TIMEOUT_MS) });
    return res.status >= 300 && res.status < 400 ? 200 : res.status;
  } catch {
    return 0;
  }
}

export async function runChecks(env, targets, fetchImpl = fetch, now = Date.now()) {
  const sent = [];
  for (const t of targets) {
    const status = await probe(t.url, fetchImpl);
    const raw = await env.STATE.get(`t:${t.name}`);
    const { state, notify } = step(raw ? JSON.parse(raw) : null, isUp(status), now);
    await env.STATE.put(`t:${t.name}`, JSON.stringify(state));
    if (notify) {
      const text = formatMessage(t, notify, env.ADMIN_BASE_URL, now);
      const r = await fetchImpl(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text, parse_mode: "HTML", disable_web_page_preview: true }),
      });
      if (!r.ok) throw new Error(`telegram sendMessage failed: ${r.status}`);
      sent.push({ name: t.name, kind: notify.kind });
    }
  }
  return sent;
}
