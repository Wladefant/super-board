import test from "node:test";
import assert from "node:assert/strict";
import { step, isUp, runChecks, formatMessage, adminLink } from "./monitor.mjs";

test("adminLink: explicit admin wins, default is /services/<name>", () => {
  assert.equal(adminLink({ name: "x", admin: "https://h" }, "https://a"), "https://h");
  assert.equal(adminLink({ name: "pinthread-api" }, "https://a"), "https://a/services/pinthread-api");
});

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

test("failed Telegram send is retried next run and one bad target does not stop the others", async () => {
  const env = fakeEnv();
  const tg = [];
  let tgOk = false;
  const fetchImpl = async (url, opts) => {
    if (url.startsWith("https://api.telegram.org/")) {
      if (!tgOk) return { ok: false, status: 500 };
      tg.push(JSON.parse(opts.body));
      return { ok: true, status: 200 };
    }
    throw new Error("down");
  };
  const baseGet = env.STATE.get;
  env.STATE.get = async (k) => (k === "t:bad" ? "{not json" : baseGet(k));
  const targets = [{ name: "bad", url: "https://b.example" }, { name: "svc-a", url: "https://a.example" }];
  await runChecks(env, targets, fetchImpl, 0).catch(() => {});
  await assert.rejects(runChecks(env, targets, fetchImpl, 300000), /telegram sendMessage failed/);
  tgOk = true;
  await runChecks(env, targets, fetchImpl, 600000);
  assert.equal(tg.filter((m) => /svc-a/.test(m.text) && /DOWN/.test(m.text)).length, 1);
});

test("healthy unchanged target causes no KV write", async () => {
  const env = fakeEnv();
  let puts = 0;
  const put = env.STATE.put;
  env.STATE.put = async (k, v) => { puts++; return put(k, v); };
  const fetchImpl = async () => ({ status: 200 });
  await runChecks(env, [{ name: "a", url: "https://a" }], fetchImpl, 0);
  await runChecks(env, [{ name: "a", url: "https://a" }], fetchImpl, 1);
  assert.equal(puts, 0);
});

test("KV put failing never repeats a DOWN: nothing is sent while KV is broken", async () => {
  const env = fakeEnv();
  const tg = [];
  const fetchImpl = async (url, opts) => {
    if (url.startsWith("https://api.telegram.org/")) { tg.push(JSON.parse(opts.body)); return { ok: true, status: 200 }; }
    throw new Error("down");
  };
  const targets = [{ name: "svc-a", url: "https://a.example" }];
  await runChecks(env, targets, fetchImpl, 0);
  const put = env.STATE.put;
  env.STATE.put = async () => { throw new Error("kv down"); };
  for (const t of [300000, 600000, 900000]) await runChecks(env, targets, fetchImpl, t).catch(() => {});
  assert.equal(tg.length, 0);
  env.STATE.put = put;
  await runChecks(env, targets, fetchImpl, 1200000);
  await runChecks(env, targets, fetchImpl, 1500000);
  assert.equal(tg.length, 1);
});
