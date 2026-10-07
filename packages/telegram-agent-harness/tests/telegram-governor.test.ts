import { describe, expect, test } from "bun:test";
import { readdirSync, readFileSync } from "node:fs";
import * as path from "node:path";
import {
  governedTelegramFetch, TelegramGovernor, TelegramQueueFullError, TelegramRateLimitedError, type GovernedRequest,
  type GovernorOptions,
} from "../extension/telegram-governor";

/** Lets every already-resolved promise chain run; no wall-clock time passes. */
async function flush(): Promise<void> {
  for (let i = 0; i < 50; i++) await Promise.resolve();
}

/** A clock the test moves by hand: sleep parks until the driver jumps the clock to its wake time. */
function fakeClock() {
  let t = 1_000_000;
  const sleepers: Array<{ wake: number; resolve: () => void }> = [];
  return {
    now: () => t,
    sleep: (ms: number) => {
      const { promise, resolve } = Promise.withResolvers<void>();
      sleepers.push({ wake: t + ms, resolve });
      return promise;
    },
    /** Lets pending work run, then jumps to the next wake-up, until `done` settles. */
    async drive<T>(done: Promise<T>): Promise<T> {
      let settled = false;
      void done.then(() => { settled = true; }, () => { settled = true; });
      for (let guard = 0; !settled && guard < 10_000; guard++) {
        await flush();
        if (settled) break;
        sleepers.sort((a, b) => a.wake - b.wake);
        const next = sleepers.shift();
        if (next) {
          t = Math.max(t, next.wake);
          next.resolve();
        }
      }
      return done;
    },
  };
}

interface Sent { at: number; chat: string; method: string; text: string }

/** Telegram stand-in: answers 429 when a chat gets under 1 s apart or a group gets a 21st message in a minute. */
function fakeTelegram(clock: { now: () => number }, script: { retryAfterOnCall?: Map<number, number> } = {}) {
  const sent: Sent[] = [];
  const statuses: number[] = [];
  let calls = 0;
  const fetch = async (url: string, init: RequestInit): Promise<Response> => {
    calls++;
    const body = JSON.parse(String(init.body));
    const chat = String(body.chat_id);
    const method = url.split("/").pop() ?? "";
    const now = clock.now();
    const scripted = script.retryAfterOnCall?.get(calls);
    const history = sent.filter((s) => s.chat === chat);
    const tooFast = history.length > 0 && now - history[history.length - 1].at < 1000;
    const groupFull = chat.startsWith("-") && history.filter((s) => now - s.at < 60_000).length >= 20;
    if (scripted !== undefined || tooFast || groupFull) {
      statuses.push(429);
      return Response.json({ ok: false, error_code: 429, parameters: { retry_after: scripted ?? 5 } }, { status: 429 });
    }
    statuses.push(200);
    sent.push({ at: now, chat, method, text: String(body.text) });
    return Response.json({ ok: true, result: { message_id: sent.length } });
  };
  return { sent, statuses, fetch, calls: () => calls };
}

function setup(script?: { retryAfterOnCall?: Map<number, number> }, options: GovernorOptions = {}) {
  const clock = fakeClock();
  const governor = new TelegramGovernor({ now: clock.now, sleep: clock.sleep, ...options });
  const telegram = fakeTelegram(clock, script);
  const call = (chat: string | number, request: GovernedRequest, text = "x", method = "sendMessage") =>
    governedTelegramFetch(
      `https://api.telegram.org/bot1:T/${method}`,
      { method: "POST", body: JSON.stringify({ chat_id: chat, text }) },
      { chatId: chat, ...request },
      { resolve: async () => [], fetch: telegram.fetch, governor },
    );
  return { clock, governor, telegram, call };
}

describe("TelegramGovernor budget", () => {
  test("spaces sends into one private chat at least one second apart", async () => {
    const { clock, telegram, call } = setup();
    await clock.drive(Promise.all([call(7, {}), call(7, {}), call(7, {})]));
    const gaps = telegram.sent.slice(1).map((s, i) => s.at - telegram.sent[i].at);
    expect(gaps).toEqual([1000, 1000]);
    expect(telegram.statuses).toEqual([200, 200, 200]);
  });

  test("does not make one chat wait for another", async () => {
    const { clock, telegram, call } = setup();
    await clock.drive(Promise.all([call(7, {}), call(8, {})]));
    expect(telegram.sent[0].at).toBe(telegram.sent[1].at);
  });

  test("keeps 4 of the 20 group sends per minute for panels", async () => {
    const { clock, telegram, call } = setup();
    const start = clock.now();
    // 17 ordinary messages: only 16 may go out inside the first minute.
    await clock.drive(Promise.all(Array.from({ length: 17 }, (_, i) => call(-100, { kind: "message" }, `m${i}`))));
    const last = telegram.sent[16];
    expect(last.at - start).toBeGreaterThanOrEqual(60_000);
    expect(telegram.sent.slice(0, 16).every((s) => s.at - start < 60_000)).toBe(true);
    expect(telegram.statuses.every((s) => s === 200)).toBe(true);
  });

  test("a panel is not queued behind ordinary messages that used up their share", async () => {
    const { clock, telegram, call } = setup();
    await clock.drive(Promise.all(Array.from({ length: 16 }, (_, i) => call(-100, { kind: "message" }, `m${i}`))));
    const before = clock.now();
    await clock.drive(call(-100, { kind: "panel" }, "panel"));
    expect(telegram.sent[16].text).toBe("panel");
    expect(telegram.sent[16].at - before).toBeLessThanOrEqual(1000);
  });

  test("a burst of 40 panel edits in a group draws no 429 and never exceeds 20 per minute", async () => {
    const { clock, telegram, call } = setup();
    const edits = Array.from({ length: 40 }, (_, i) =>
      call(-100, { kind: "panel" }, `edit${i}`, "editMessageText"));
    await clock.drive(Promise.all(edits));
    expect(telegram.statuses.filter((s) => s === 429)).toEqual([]);
    for (const s of telegram.sent) {
      expect(telegram.sent.filter((o) => o.at >= s.at && o.at - s.at < 60_000).length).toBeLessThanOrEqual(20);
    }
    expect(telegram.sent).toHaveLength(40);
  });

  test("coalesces queued edits of one message into the newest text", async () => {
    const { clock, telegram, call } = setup();
    const first = call(-100, { kind: "panel" }, "send", "sendMessage");
    const a = call(-100, { kind: "panel", coalesceKey: "edit:-100:5" }, "old", "editMessageText");
    const b = call(-100, { kind: "panel", coalesceKey: "edit:-100:5" }, "new", "editMessageText");
    const [, ra, rb] = await clock.drive(Promise.all([first, a, b]));
    expect(telegram.sent.map((s) => s.text)).toEqual(["send", "new"]);
    expect((await ra.json()).ok).toBe(true);
    expect((await rb.json()).ok).toBe(true);
  });
});

describe("TelegramGovernor retry_after", () => {
  test("waits out retry_after and retries once, without blocking other chats", async () => {
    const { clock, telegram, call } = setup({ retryAfterOnCall: new Map([[1, 7]]) });
    const start = clock.now();
    const first = call(7, {}, "first");
    const other = call(8, {}, "other chat");
    const [res] = await clock.drive(Promise.all([first, other]));
    expect((await res.json()).ok).toBe(true);
    expect(telegram.statuses[0]).toBe(429);
    const byChat = (chat: string) => telegram.sent.find((s) => s.chat === chat);
    expect(byChat("7")?.at).toBe(start + 7000);
    expect(byChat("8")?.at).toBeLessThan(start + 7000);
    expect(telegram.calls()).toBe(3);
  });

  test("a 429 on a call bound to no chat blocks the whole bot", async () => {
    const { clock, telegram, call } = setup({ retryAfterOnCall: new Map([[1, 4]]) });
    const start = clock.now();
    const menu = call(7, { kind: "other", chatId: undefined }, "menu", "setMyCommands");
    await flush();
    const send = call(8, {}, "after");
    await clock.drive(Promise.all([menu, send]));
    expect(telegram.sent.find((s) => s.chat === "8")?.at).toBeGreaterThanOrEqual(start + 4000);
  });

  test("a second 429 is returned to the caller and not retried in a loop", async () => {
    const { clock, telegram, call } = setup({ retryAfterOnCall: new Map([[1, 2], [2, 2]]) });
    const res = await clock.drive(call(7, {}));
    expect(res.status).toBe(429);
    expect(telegram.calls()).toBe(2);
  });

  test("a retry_after beyond the inline limit is recorded but not slept through", async () => {
    const { clock, governor, telegram, call } = setup({ retryAfterOnCall: new Map([[1, 3600]]) });
    const res = await clock.drive(call(7, {}));
    expect(res.status).toBe(429);
    expect(telegram.calls()).toBe(1);
    expect(governor.snapshot().chats[0].blockedForMs).toBe(3600_000);
  });

  test("a call queued behind a long retry_after fails fast instead of sleeping inline", async () => {
    const { clock, telegram, call } = setup({ retryAfterOnCall: new Map([[1, 3600]]) });
    await clock.drive(call(7, {}));
    const queued = call(7, {}, "later");
    await expect(clock.drive(queued)).rejects.toBeInstanceOf(TelegramRateLimitedError);
    expect(telegram.calls()).toBe(1);
  });

  test("logs governor state", async () => {
    const { clock, call } = setup({ retryAfterOnCall: new Map([[1, 2]]) });
    const lines: string[] = [];
    await clock.drive(call(7, { log: (l) => lines.push(l) }));
    expect(lines.some((l) => l.includes("429") && l.includes("2 s"))).toBe(true);
    expect(lines.some((l) => l.includes("retry_after"))).toBe(true);
    expect(lines.join("\n")).not.toContain(":T");
  });
});

describe("TelegramGovernor queues", () => {
  test("a message held by the group window does not hold up a panel", async () => {
    const { clock, telegram, call } = setup();
    await clock.drive(Promise.all(Array.from({ length: 16 }, (_, i) => call(-100, { kind: "message" }, `m${i}`))));
    const before = clock.now();
    const held = call(-100, { kind: "message" }, "held");
    const panel = call(-100, { kind: "panel" }, "panel");
    await clock.drive(panel);
    expect(telegram.sent.find((s) => s.text === "panel")?.at).toBeLessThanOrEqual(before + 1000);
    expect(telegram.sent.some((s) => s.text === "held")).toBe(false);
    await clock.drive(held);
    expect(telegram.sent.find((s) => s.text === "held")?.at).toBeGreaterThanOrEqual(before + 1000);
  });

  test("a full queue refuses a new message and logs it", async () => {
    const { clock, telegram, call } = setup(undefined, { maxQueuedPerChat: 3 });
    const lines: string[] = [];
    const accepted = [1, 2, 3].map((i) => call(7, { log: (l) => lines.push(l) }, `m${i}`));
    const refused = call(7, { log: (l) => lines.push(l) }, "m4");
    await expect(refused).rejects.toBeInstanceOf(TelegramQueueFullError);
    await clock.drive(Promise.all(accepted));
    expect(telegram.sent.map((s) => s.text)).toEqual(["m1", "m2", "m3"]);
    expect(lines.some((l) => l.includes("queue full"))).toBe(true);
  });

  test("a full queue drops its oldest panel edit for a newer panel call", async () => {
    const { clock, telegram, call } = setup(undefined, { maxQueuedPerChat: 2 });
    const p1 = call(7, { kind: "panel", coalesceKey: "e:1" }, "p1", "editMessageText");
    const p2 = call(7, { kind: "panel", coalesceKey: "e:2" }, "p2", "editMessageText");
    const p3 = call(7, { kind: "panel", coalesceKey: "e:3" }, "p3", "editMessageText");
    await expect(p1).rejects.toBeInstanceOf(TelegramQueueFullError);
    await clock.drive(Promise.all([p2, p3]));
    expect(telegram.sent.map((s) => s.text)).toEqual(["p2", "p3"]);
  });
});

describe("outbound calls reach Telegram only through the governor", () => {
  test("no source file outside the transport and the governor calls telegramFetch or a raw fetch to the Bot API", () => {
    const root = path.resolve(import.meta.dir, "..");
    const offenders: string[] = [];
    for (const dir of ["extension", "daemon", "miniapp"]) {
      for (const file of readdirSync(path.join(root, dir), { recursive: true, encoding: "utf8" })) {
        if (!file.endsWith(".ts")) continue;
        const name = path.basename(file);
        if (name === "telegram-fetch.ts" || name === "telegram-governor.ts") continue;
        const source = readFileSync(path.join(root, dir, file), "utf8");
        if (/\btelegramFetch\s*\(/.test(source) || /\bfetch\(\s*[`"'][^`"']*api\.telegram\.org/.test(source)) {
          offenders.push(`${dir}/${file}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
