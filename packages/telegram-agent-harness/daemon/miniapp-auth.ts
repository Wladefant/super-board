import { createHmac, timingSafeEqual } from "node:crypto";

/** Validate raw Telegram initData, never the untrusted initDataUnsafe object. */
export function authenticateInitData(raw: string, token: string, allowedUsers: readonly string[], now = Date.now()): string {
  if (!raw || raw.length > 16384) throw new Error("Unauthorized");
  const params = new URLSearchParams(raw);
  const seen = new Set<string>();
  for (const [key] of params) {
    if (seen.has(key)) throw new Error("Unauthorized");
    seen.add(key);
  }
  const hash = params.get("hash") ?? "";
  if (!/^[a-f0-9]{64}$/.test(hash)) throw new Error("Unauthorized");
  params.delete("hash");
  const check = [...params].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, value]) => `${key}=${value}`).join("\n");
  const secret = createHmac("sha256", "WebAppData").update(token).digest();
  const expected = createHmac("sha256", secret).update(check).digest();
  if (!timingSafeEqual(expected, Buffer.from(hash, "hex"))) throw new Error("Unauthorized");
  const date = Number(params.get("auth_date"));
  if (!Number.isSafeInteger(date) || date <= 0 || now / 1000 - date > 300 || date - now / 1000 > 30) throw new Error("Unauthorized");
  let user: { id?: unknown };
  try { user = JSON.parse(params.get("user") ?? "null"); } catch { throw new Error("Unauthorized"); }
  if (!user || !Number.isSafeInteger(user.id) || !allowedUsers.includes(String(user.id))) throw new Error("Unauthorized");
  return String(user.id);
}

/** App sessions are purpose-bound; Telegram launch credentials are exchanged once. */
export function issueAppSession(user: string, token: string, now = Date.now()): string {
  const payload = `${user}.${Math.floor(now / 1000)}`;
  const signature = createHmac("sha256", token).update(`miniapp-session:${payload}`).digest("hex");
  return `${payload}.${signature}`;
}

export function authenticateAppSession(session: string, token: string, allowedUsers: readonly string[], now = Date.now()): string {
  const match = /^(\d+)\.(\d+)\.([a-f0-9]{64})$/.exec(session);
  if (!match) throw new Error("Unauthorized");
  const [, user, issued, signature] = match;
  const age = now / 1000 - Number(issued);
  if (age < -30 || age > 28800 || !allowedUsers.includes(user)) throw new Error("Unauthorized");
  const expected = createHmac("sha256", token).update(`miniapp-session:${user}.${issued}`).digest();
  if (!timingSafeEqual(expected, Buffer.from(signature, "hex"))) throw new Error("Unauthorized");
  return user;
}
