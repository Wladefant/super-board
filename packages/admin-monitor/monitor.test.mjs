import test from "node:test";
import assert from "node:assert/strict";
import { step, isUp, runChecks, formatMessage } from "./monitor.mjs";

test("isUp: 4xx is up, 5xx and 0 are down", () => {
  assert.equal(isUp(200), true);
  assert.equal(isUp(401), true);
  assert.equal(isUp(404), true);
  assert.equal(isUp(502), false);
  assert.equal(isUp(525), false);
  assert.equal(isUp(0), false);
});

test("one failure does not alert; second does; third stays quiet; recovery alerts once", () => {
  let s = null;
  let r = step(s, false, 1000); assert.equal(r.notify, null); s = r.state;
  r = step(s, false, 2000); assert.equal(r.notify.kind, "down"); s = r.state;
  r = step(s, false, 3000); assert.equal(r.notify, null); s = r.state;
  r = step(s, true, 63000); assert.equal(r.notify.kind, "up"); assert.equal(r.notify.downSince, 2000); s = r.state;
  r = step(s, true, 64000); assert.equal(r.notify, null);
});

test("a single blip followed by success sends nothing", () => {
  let r = step(null, false, 1); r = step(r.state, true, 2);
  assert.equal(r.notify, null);
});

function fakeEnv() {
  const m = new Map();
  return { STATE: { get: async (k) => m.get(k) ?? null, put: async (k, v) => void m.set(k, v) },
    ADMIN_BASE_URL: "https://admin.example", TELEGRAM_BOT_TOKEN: "TOK", TELEGRAM_CHAT_ID: "1" };
}

test("runChecks sends one DOWN then one UP with the admin link, via Telegram", async () => {
  const env = fakeEnv();
  const tg = [];
  let healthy = false;
  const fetchImpl = async (url, opts) => {
    if (url.startsWith("https://api.telegram.org/")) { tg.push(JSON.parse(opts.body)); return { ok: true, status: 200 }; }
    if (!healthy) throw new Error("boom");
    return { status: 200 };
  };
  const targets = [{ name: "svc-a", url: "https://a.example" }];
  await runChecks(env, targets, fetchImpl, 0);
  await runChecks(env, targets, fetchImpl, 300000);
  await runChecks(env, targets, fetchImpl, 600000);
  healthy = true;
  await runChecks(env, targets, fetchImpl, 900000);
  await runChecks(env, targets, fetchImpl, 1200000);
  assert.equal(tg.length, 2);
  assert.match(tg[0].text, /DOWN/);
  assert.match(tg[0].text, /https:\/\/admin\.example\/services\/svc-a/);
  assert.match(tg[1].text, /RECOVERED/);
});

test("formatMessage escapes HTML in names", () => {
  const t = formatMessage({ name: "<b>x", url: "https://u" }, { kind: "down" }, "https://admin.example", 0);
  assert.ok(!t.includes("<b>x"));
});
