import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { DEFAULT_SEND_TIMEOUT_MS, failedBeforeRequestSent, TelegramPoller } from "../extension/poller";

const originalFetch = globalThis.fetch;
const closers: Array<() => void> = [];
afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const close of closers.splice(0)) close();
});

const TOKEN = "123456:secret-token-value-for-test";

function poller(log: string[], extra: Record<string, unknown> = {}): TelegramPoller {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-send-transport-"));
  const p = new TelegramPoller(
    TOKEN,
    dir,
    { allowFrom: ["1"], dmPolicy: "allowlist" },
    {
      isIdle: () => true, onUserMessage: () => {}, onSteer: () => {}, onFollowUp: () => {},
      onAbort: () => {}, onRelease: async () => {}, getStatusText: () => "t", onLedgerFailure: () => {},
    },
    undefined,
    { outboundPaceMs: 0, log: m => log.push(m), ...extra },
  );
  closers.push(() => { p.stop(); fs.rmSync(dir, { recursive: true, force: true }); });
  return p;
}

const ok = () => new Response(JSON.stringify({ ok: true, result: { message_id: 7, chat: { id: 1 } } }));
const coded = (code: string) => Object.assign(new Error("Unable to connect"), { code });

test("a connect failure is retried once and the message is delivered a single time", async () => {
  let attempts = 0;
  globalThis.fetch = (async () => {
    attempts++;
    if (attempts === 1) throw coded("ConnectionRefused");
    return ok();
  }) as typeof fetch;
  const log: string[] = [];
  const sent = await poller(log).sendTelegramMessage("1", "hello");
  expect(sent?.ok).toBe(true);
  expect(attempts).toBe(2);
  expect(log.join("\n")).toContain("ConnectionRefused");
});

test("a timeout is never retried, because the message may already be delivered", async () => {
  let attempts = 0;
  globalThis.fetch = (async () => {
    attempts++;
    throw new DOMException("The operation timed out.", "TimeoutError");
  }) as typeof fetch;
  const log: string[] = [];
  expect(await poller(log).sendTelegramMessage("1", "hello")).toBeNull();
  expect(attempts).toBe(1);
  expect(log[0]).toContain("TimeoutError");
});

test("two connect failures give up after one retry and the log never carries the token", async () => {
  let attempts = 0;
  globalThis.fetch = (async () => {
    attempts++;
    throw Object.assign(new Error(`connect failed for https://api.telegram.org/bot${TOKEN}/sendMessage`), { code: "ENOTFOUND" });
  }) as typeof fetch;
  const log: string[] = [];
  expect(await poller(log).sendTelegramMessage("1", "hello")).toBeNull();
  expect(attempts).toBe(2);
  expect(log.length).toBe(2);
  expect(log.join("\n")).not.toContain("secret-token-value-for-test");
});

test("a send gets the long default budget, and an explicit one still wins", async () => {
  const seen: number[] = [];
  const fakeTimeout = AbortSignal.timeout;
  AbortSignal.timeout = ((ms: number) => { seen.push(ms); return fakeTimeout.call(AbortSignal, ms); }) as typeof AbortSignal.timeout;
  try {
    globalThis.fetch = (async () => ok()) as typeof fetch;
    await poller([]).sendTelegramMessage("1", "a");
    await poller([], { sendTimeoutMs: 9_000 }).sendTelegramMessage("1", "b");
  } finally {
    AbortSignal.timeout = fakeTimeout;
  }
  expect(DEFAULT_SEND_TIMEOUT_MS).toBeGreaterThan(21_000);
  expect(seen).toEqual([DEFAULT_SEND_TIMEOUT_MS, 9_000]);
});

test("only connect and DNS errors count as failed before the request was sent", () => {
  expect(failedBeforeRequestSent(coded("EAI_AGAIN"))).toBe(true);
  expect(failedBeforeRequestSent(coded("ECONNRESET"))).toBe(false);
  expect(failedBeforeRequestSent(new DOMException("t", "TimeoutError"))).toBe(false);
  expect(failedBeforeRequestSent(null)).toBe(false);
});
