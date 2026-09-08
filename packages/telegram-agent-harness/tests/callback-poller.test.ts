import { test, expect, afterEach } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { TelegramPoller, type PollerCallbacks } from "../extension/poller";
import type { CallbackValidationDecision, MessageCorrelationBridge, TelegramUpdate } from "../extension/types";

const originalFetch = globalThis.fetch;
const cleanup: Array<() => void> = [];
afterEach(() => { globalThis.fetch = originalFetch; for (const close of cleanup.splice(0)) close(); });
function fixture(decision: CallbackValidationDecision = "deliver", consume = true) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-callback-"));
  const calls: Array<{ method: string; body: Record<string, unknown> }> = [];
  const delivered: string[] = [];
  let session = "session-a";
  let consumed = false;
  const record = { callbackToken: "cb:d_test", decisionId: "preference", choiceId: "B", sessionId: "session-a", chatId: "1", userId: "1", questionHash: "hash", expiresAt: Date.now() / 1000 + 100, consumedAt: null, createdAt: 0 };
  const bridge: MessageCorrelationBridge = {
    getSessionId: () => session, getSlotId: () => "test", record: () => {},
    resolveReply: () => ({ decision: "reject_unknown", detail: "Unknown message" }),
    resolveCallback: () => ({ decision: consumed ? "reject_already_consumed" : decision, record, detail: "Old or foreign choice" }),
    consumeCallback: () => { if (consumed || !consume) return false; consumed = true; return true; },
  };
  const callbacks: PollerCallbacks = {
    isIdle: () => true, onUserMessage: () => {}, onFollowUp: () => {}, onSteer: () => {}, onAbort: () => {}, onRelease: async () => {}, getStatusText: () => "test", onTelegramTurnStart: () => {}, onLedgerFailure: () => {},
    onDecisionCallback: async (id, choice, context) => { delivered.push(`${id}:${choice}:${context}`); },
  };
  const poller = new TelegramPoller("0:test-only", dir, { dmPolicy: "allowlist", allowFrom: ["1"] }, callbacks, bridge);
  globalThis.fetch = (async (input, init) => {
    calls.push({ method: String(input).split("/").pop()!, body: JSON.parse(String(init?.body ?? "{}")) });
    return Response.json({ ok: true });
  }) as typeof fetch;
  cleanup.push(() => { poller.stop(); fs.rmSync(dir, { recursive: true, force: true }); });
  const update: TelegramUpdate = { update_id: 1, callback_query: { id: "click-1", from: { id: 1, is_bot: false, first_name: "Test" }, data: "cb:d_test", message: { message_id: 9, chat: { id: 1, type: "private" }, date: 0, caption: "Which layout?" } } };
  return { poller, calls, delivered, update, callbacks, bridge, switchSession: () => { session = "session-b"; } };
}

test("caption button delivers context once and removes markup without replacing the question", async () => {
  const f = fixture();
  f.poller.ingestUpdates([f.update]);
  await f.poller.redrivePendingUpdates();
  f.poller.ingestUpdates([{ ...f.update, update_id: 2 }]);
  await f.poller.redrivePendingUpdates();
  expect(f.delivered).toEqual(["preference:B:Callback identity: cb:d_test\nWhich layout?"]);
  expect(f.calls.find(c => c.method === "editMessageReplyMarkup")?.body).toEqual({ chat_id: "1", message_id: 9, reply_markup: { inline_keyboard: [] } });
  expect(f.calls.some(c => c.method === "editMessageText")).toBe(false);
  expect(JSON.stringify(f.calls)).not.toContain("Blocker cleared");
});
for (const rejection of ["reject_unknown", "reject_foreign_session", "reject_expired", "reject_already_consumed", "reject_already_answered"] as const) {
  test(`${rejection} gets visible feedback and never reaches the operator session`, async () => {
    const f = fixture(rejection);
    f.poller.ingestUpdates([f.update]); await f.poller.redrivePendingUpdates();
    expect(f.delivered).toEqual([]);
    expect(f.calls.some(c => c.method === "sendMessage" && String(c.body.text).includes("Selection not delivered"))).toBe(true);
    expect(f.calls.some(c => c.method === "editMessageReplyMarkup")).toBe(rejection !== "reject_foreign_session");
    expect(f.poller.getNextContiguousOffset()).toBe(2);
  });
}
test("session switch prevents delivery; unconfirmed post-delivery consumption remains visible", async () => {
  const f = fixture(); f.switchSession(); f.poller.ingestUpdates([f.update]); await f.poller.redrivePendingUpdates(); expect(f.delivered).toEqual([]);
  const raced = fixture("deliver", false); raced.poller.ingestUpdates([raced.update]); await raced.poller.redrivePendingUpdates(); expect(raced.delivered).toHaveLength(1);
  expect(raced.calls.some(c => c.method === "sendMessage" && String(c.body.text).includes("delivered"))).toBe(true);
  raced.poller.ingestUpdates([{ ...raced.update, update_id: 2 }]); await raced.poller.redrivePendingUpdates();
  expect(raced.delivered).toHaveLength(1);
  expect(raced.calls.some(c => c.method === "sendMessage" && String(c.body.text).includes("already delivered"))).toBe(true);
});
test("getUpdates explicitly requests callbacks and acknowledges before dispatch", async () => {
  const f = fixture();
  let polls = 0;
  const baseFetch = globalThis.fetch;
  globalThis.fetch = (async (input, init) => {
    const url = new URL(String(input));
    if (url.pathname.endsWith("/getUpdates")) {
      expect(JSON.parse(url.searchParams.get("allowed_updates")!)).toEqual(["message", "callback_query"]);
      if (++polls === 1) return Response.json({ ok: true, result: [f.update] });
      f.poller.stop(); throw new Error("stopped");
    }
    if (url.pathname.endsWith("/answerCallbackQuery")) expect(f.delivered).toEqual([]);
    return baseFetch(input, init);
  }) as typeof fetch;
  await f.poller.start();
  expect(f.delivered).toEqual(["preference:B:Callback identity: cb:d_test\nWhich layout?"]);
  expect(f.calls[0].method).toBe("answerCallbackQuery");
});

test("failed dispatch leaves token available for the next click", async () => {
  const f = fixture();
  const deliver = f.callbacks.onDecisionCallback!;
  f.callbacks.onDecisionCallback = async () => { throw new Error("Session switched before dispatch"); };
  f.poller.ingestUpdates([f.update]); await f.poller.redrivePendingUpdates();
  expect(f.bridge.resolveCallback!("cb:d_test", "1", "1").decision).toBe("deliver");
  f.callbacks.onDecisionCallback = deliver;
  f.poller.ingestUpdates([{ ...f.update, update_id: 2 }]); await f.poller.redrivePendingUpdates();
  expect(f.delivered).toHaveLength(1);
  expect(f.bridge.resolveCallback!("cb:d_test", "1", "1").decision).toBe("reject_already_consumed");
});

test("concurrent redrives serialize duplicate clicks until delivery completes", async () => {
  const f = fixture();
  const gate = Promise.withResolvers<void>();
  const entered = Promise.withResolvers<void>();
  const deliver = f.callbacks.onDecisionCallback!;
  f.callbacks.onDecisionCallback = async (...args) => {
    entered.resolve(); await gate.promise; await deliver(...args);
  };
  f.poller.ingestUpdates([f.update, { ...f.update, update_id: 2 }]);
  const first = f.poller.redrivePendingUpdates();
  await entered.promise;
  const second = f.poller.redrivePendingUpdates();
  expect(second).toBe(first);
  expect(f.bridge.resolveCallback!("cb:d_test", "1", "1").decision).toBe("deliver");
  gate.resolve(); await Promise.all([first, second]);
  expect(f.delivered).toHaveLength(1);
  expect(f.poller.getNextContiguousOffset()).toBe(3);
});

for (const stalledMethod of ["sendMessage", "editMessageReplyMarkup"]) {
  test(`stalled ${stalledMethod} aborts and cannot pin the ledger offset`, async () => {
    const f = fixture("reject_unknown");
    const baseFetch = globalThis.fetch;
    let aborted = false;
    globalThis.fetch = (async (input, init) => {
      if (!String(input).endsWith(`/${stalledMethod}`)) return baseFetch(input, init);
      const signal = init?.signal;
      if (!signal) throw new Error("Missing bounded signal");
      await new Promise<void>((_resolve, reject) => {
        // Platform-clock integration: native AbortSignal.timeout is not driven
        // by JS fake timers. The watchdog both bounds failure and models live IO.
        // Model a live network operation keeping the event loop alive; Bun's
        // AbortSignal.timeout timer itself is unreferenced.
        const deadline = setTimeout(() => reject(new Error("Transport deadline did not abort")), 4000);
        signal.addEventListener("abort", () => {
          clearTimeout(deadline); aborted = true; reject(signal.reason);
        }, { once: true });
      });
      throw new Error("Unreachable");
    }) as typeof fetch;
    f.poller.ingestUpdates([f.update]); await f.poller.redrivePendingUpdates();
    expect(aborted).toBe(true);
    expect(f.poller.getNextContiguousOffset()).toBe(2);
  }, 8000);
}

for (const idle of [true, false]) {
  for (const reply of [true, false]) {
    test(`session switch during command await refuses ${reply ? "reply" : "plain"} text while ${idle ? "idle" : "busy"}`, async () => {
      const f = fixture();
      f.callbacks.isIdle = () => idle;
      f.callbacks.onUserMessage = text => { f.delivered.push(text); };
      f.callbacks.onSteer = text => { f.delivered.push(text); };
      f.callbacks.onHarnessCommand = async () => { await Promise.resolve(); f.switchSession(); return false; };
      f.bridge.resolveReply = () => ({ decision: "deliver", detail: "Session A" });
      f.poller.ingestUpdates([{ update_id: 1, message: {
        message_id: 10, chat: { id: 1, type: "private" }, from: { id: 1, is_bot: false, first_name: "Test" },
        date: 0, text: "Choose B", ...(reply ? { reply_to_message: { message_id: 9, text: "Question" } } : {}),
      } }]);
      await f.poller.redrivePendingUpdates();
      expect(f.delivered).toEqual([]);
      expect(f.poller.getNextContiguousOffset()).toBe(2);
    });
  }
}
