// The lock tests below use the real clock on purpose: the lock deadline and the event-loop check measure real
// waiting on a file lock, which a fake clock cannot drive.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
  emptyState, recordRateLimit, reserve, SharedBudget, UNNAMED_LOCK_MS, type BudgetConfig,
} from "../extension/telegram-budget";
import { governedTelegramFetch, TelegramGovernor, type GovernedRequest } from "../extension/telegram-governor";

const CFG: BudgetConfig = { chatIntervalMs: 1000, groupLimit: 20, windowMs: 60_000, panelReserve: 12 };

/** Lets every pending promise chain run, including the shared budget's awaited file work. */
async function flush(): Promise<void> {
  for (let i = 0; i < 20; i++) await Promise.resolve();
  await new Promise<void>((resolve) => setImmediate(resolve));
}

/** One clock for every simulated process; sleep parks until the driver jumps to the wake time. */
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

/** Telegram stand-in, one per bot token: 429 when a chat gets under 1 s apart or a group a 21st message in a minute. */
function fakeTelegram(clock: { now: () => number }, retryAfterOnCall: Map<number, number> = new Map()) {
  const sent: Array<{ at: number; chat: string; text: string; kind: string }> = [];
  const statuses: number[] = [];
  let calls = 0;
  const fetch = async (_url: string, init: RequestInit): Promise<Response> => {
    calls++;
    const body = JSON.parse(String(init.body));
    const chat = String(body.chat_id);
    const now = clock.now();
    const history = sent.filter((s) => s.chat === chat);
    const tooFast = history.length > 0 && now - history[history.length - 1].at < 1000;
    const groupFull = chat.startsWith("-") && history.filter((s) => now - s.at < 60_000).length >= 20;
    const scripted = retryAfterOnCall.get(calls);
    if (scripted !== undefined || tooFast || groupFull) {
      statuses.push(429);
      return Response.json({ ok: false, parameters: { retry_after: scripted ?? 5 } }, { status: 429 });
    }
    statuses.push(200);
    sent.push({ at: now, chat, text: String(body.text), kind: String(body.kind) });
    return Response.json({ ok: true, result: { message_id: sent.length } });
  };
  return { sent, statuses, fetch, calls: () => calls };
}

let dir: string;
beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-budget-"));
});
afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

/** Two senders that share nothing but the budget file, like the daemon and the notifier. */
function twoProcesses(retryAfterOnCall?: Map<number, number>) {
  const clock = fakeClock();
  const telegram = fakeTelegram(clock, retryAfterOnCall);
  const make = () => new TelegramGovernor({ now: clock.now, sleep: clock.sleep, shared: new SharedBudget("1", dir) });
  const [a, b] = [make(), make()];
  const call = (governor: TelegramGovernor, chat: string | number, request: GovernedRequest, text = "x") =>
    governedTelegramFetch(
      "https://api.telegram.org/bot1:T/sendMessage",
      { method: "POST", body: JSON.stringify({ chat_id: chat, text, kind: request.kind ?? "message" }) },
      { chatId: chat, ...request },
      { resolve: async () => [], fetch: telegram.fetch, governor },
    );
  return { clock, telegram, a, b, call };
}

describe("shared budget across processes", () => {
  test("two senders into one private chat together keep one send per second", async () => {
    const { clock, telegram, a, b, call } = twoProcesses();
    const sends = [0, 1, 2, 3, 4, 5].map((i) => call(i % 2 === 0 ? a : b, 7, {}, `m${i}`));
    await clock.drive(Promise.all(sends));
    const gaps = telegram.sent.slice(1).map((s, i) => s.at - telegram.sent[i].at);
    expect(gaps.every((gap) => gap >= 1000)).toBe(true);
    expect(telegram.statuses.every((s) => s === 200)).toBe(true);
    expect(telegram.sent).toHaveLength(6);
  });

  test("two senders into one group together stay within the 8 per minute ordinary-message share", async () => {
    const { clock, telegram, a, b, call } = twoProcesses();
    const sends = Array.from({ length: 12 }, (_, i) => call(i % 2 === 0 ? a : b, -100, { kind: "message" }, `m${i}`));
    await clock.drive(Promise.all(sends));
    expect(telegram.statuses.filter((s) => s === 429)).toEqual([]);
    for (const s of telegram.sent) {
      expect(telegram.sent.filter((o) => o.at >= s.at && o.at - s.at < 60_000).length).toBeLessThanOrEqual(8);
    }
    expect(telegram.sent).toHaveLength(12);
  });

  test("a panel in one process still has its reserved group share while another process used up the messages", async () => {
    const { clock, telegram, a, b, call } = twoProcesses();
    await clock.drive(Promise.all(Array.from({ length: 8 }, (_, i) => call(a, -100, { kind: "message" }, `m${i}`))));
    const before = clock.now();
    await clock.drive(call(b, -100, { kind: "panel" }, "panel"));
    expect(telegram.sent[8].at - before).toBeLessThanOrEqual(1000);
  });

  test("a 429 seen by one process holds back the others for that chat, not for other chats", async () => {
    const { clock, telegram, a, b, call } = twoProcesses(new Map([[1, 7]]));
    const start = clock.now();
    const first = call(a, 7, {}, "from a");
    await flush();
    const second = call(b, 7, {}, "from b");
    const other = call(b, 8, {}, "other chat");
    await clock.drive(Promise.all([first, second, other]));
    const at = (text: string) => telegram.sent.find((s) => s.text === text)?.at ?? 0;
    expect(at("from a")).toBe(start + 7000);
    expect(at("from b")).toBeGreaterThanOrEqual(start + 7000);
    expect(at("other chat")).toBeLessThan(start + 7000);
    expect(telegram.calls()).toBe(4);
  });
});

describe("shared budget file", () => {
  test("reserve books a send, then asks for the chat interval", () => {
    const state = emptyState();
    expect(reserve(state, "7", "message", 5000, CFG).waitMs).toBe(0);
    const next = reserve(state, "7", "message", 5400, CFG);
    expect(next.waitMs).toBe(600);
    expect(state.chats["7"].sends).toHaveLength(1);
  });

  test("a recorded 429 blocks that chat and reports how long", () => {
    const state = emptyState();
    recordRateLimit(state, "7", 20_000);
    const claim = reserve(state, "7", "message", 10_000, CFG);
    expect(claim).toMatchObject({ waitMs: 10_000, blockedMs: 10_000, reason: "retry_after" });
    expect(reserve(state, "8", "message", 10_000, CFG).waitMs).toBe(0);
    recordRateLimit(state, undefined, 30_000);
    expect(reserve(state, "8", "message", 10_000, CFG).blockedMs).toBe(20_000);
  });

  test("a clock that moved backwards does not freeze a chat", () => {
    const state = emptyState();
    reserve(state, "7", "message", 900_000, CFG);
    expect(reserve(state, "7", "message", 100_000, CFG).waitMs).toBe(1000);
    expect(reserve(state, "7", "message", 101_000, CFG).waitMs).toBe(0);
  });

  test("an unusable budget directory never blocks a send", async () => {
    const blocker = path.join(dir, "file");
    fs.writeFileSync(blocker, "x");
    const clock = fakeClock();
    const telegram = fakeTelegram(clock);
    const governor = new TelegramGovernor({ now: clock.now, sleep: clock.sleep, shared: new SharedBudget("1", path.join(blocker, "sub")) });
    const res = await clock.drive(
      governedTelegramFetch(
        "https://api.telegram.org/bot1:T/sendMessage",
        { method: "POST", body: JSON.stringify({ chat_id: 7, text: "x" }) },
        { chatId: 7 },
        { resolve: async () => [], fetch: telegram.fetch, governor },
      ),
    );
    expect(res.status).toBe(200);
  });

  const lockFile = () => path.join(dir, "1.lock");
  const heldBy = (pid: number) => fs.writeFileSync(lockFile(), JSON.stringify({ pid, token: "other", at: Date.now() }));
  const age = (target: string, ms: number) => {
    const old = new Date(Date.now() - ms);
    fs.utimesSync(target, old, old);
  };

  test("a lock left by a dead process is swept at once", async () => {
    const dead = Bun.spawnSync([process.execPath, "-e", "0"]).pid;
    heldBy(dead);
    const budget = new SharedBudget("1", dir);
    expect(await budget.update((state) => reserve(state, "7", "message", Date.now(), CFG).waitMs)).toBe(0);
    expect(fs.existsSync(lockFile())).toBe(false);
  });

  test("a live holder is never evicted, however long it holds; the caller gives up at the deadline", async () => {
    heldBy(process.pid);
    age(lockFile(), 11_500);
    const before = fs.readFileSync(lockFile(), "utf8");
    const budget = new SharedBudget("1", dir, 300);
    let ran = false;
    const started = Date.now();
    expect(await budget.update(() => { ran = true; return 1; })).toBeUndefined();
    expect(ran).toBe(false);
    expect(Date.now() - started).toBeLessThan(1500);
    expect(fs.readFileSync(lockFile(), "utf8")).toBe(before);
  });

  test("one deadline covers a lock of any shape, including a regular file with no holder", async () => {
    fs.writeFileSync(lockFile(), "");
    const budget = new SharedBudget("1", dir, 300);
    const started = Date.now();
    expect(await budget.update(() => 1)).toBeUndefined();
    expect(Date.now() - started).toBeLessThan(1500);
    // Once it is old enough to be a crashed writer, it is swept.
    age(lockFile(), UNNAMED_LOCK_MS + 500);
    expect(await budget.update(() => 1)).toBe(1);
  });

  test("the earlier mkdir lock: kept while recent, swept when old", async () => {
    fs.mkdirSync(lockFile());
    const budget = new SharedBudget("1", dir, 200);
    expect(await budget.update(() => 1)).toBeUndefined();
    expect(fs.existsSync(lockFile())).toBe(true);
    age(lockFile(), 60_000);
    expect(await budget.update(() => 1)).toBe(1);
  });

  test("waiting for the lock never blocks the event loop", async () => {
    heldBy(process.pid);
    const budget = new SharedBudget("1", dir, 300);
    let ticks = 0;
    const timer = setInterval(() => { ticks++; }, 10);
    await budget.update(() => 1);
    clearInterval(timer);
    expect(ticks).toBeGreaterThanOrEqual(10);
  });

  test("concurrent read-modify-write cycles lose no update", async () => {
    const budget = new SharedBudget("1", dir);
    const results = await Promise.all(Array.from({ length: 25 }, () => budget.update((state) => ++state.botBlockedUntil)));
    expect(results.every((r) => r !== undefined)).toBe(true);
    expect(JSON.parse(fs.readFileSync(path.join(dir, "1.json"), "utf8")).botBlockedUntil).toBe(25);
  });

  test("a corrupt file starts from an empty budget", async () => {
    fs.writeFileSync(path.join(dir, "1.json"), "{not json");
    const budget = new SharedBudget("1", dir);
    expect(await budget.update((state) => reserve(state, "7", "message", 5000, CFG).waitMs)).toBe(0);
    expect(JSON.parse(fs.readFileSync(path.join(dir, "1.json"), "utf8")).chats["7"].sends).toEqual([[5000, "message"]]);
  });
});
