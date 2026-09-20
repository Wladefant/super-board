import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { TelegramPoller } from "../extension/poller";
import type { MessageCorrelationBridge, OutboundMessageCorrelation } from "../extension/types";

const originalFetch = globalThis.fetch;
const cleanup: Array<() => void> = [];
afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const close of cleanup.splice(0)) close();
});

interface DashboardFixtureOptions {
  sessionId?: string;
  thread?: number;
}

function fixture(options: DashboardFixtureOptions = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-dashboard-pin-"));
  const calls: Array<{ method: string; body: Record<string, unknown>; at: number }> = [];
  const messages = new Map<number, OutboundMessageCorrelation>();
  let messageId = 200;
  let pinnedMessageId: number | null = null;
  let operatorPinnedId: number | null = null;
  let failNextEdit: { code: number; description: string } | null = null;
  let failNextPin = false;

  const sessionId = options.sessionId ?? "test-session";
  const bridge: MessageCorrelationBridge = {
    getSessionId: () => sessionId,
    getSlotId: () => "slot-test",
    record: row => { messages.set(row.messageId, row); },
    resolveReply: () => ({ decision: "reject_unknown", detail: "Unknown" }),
    resolveCallback: () => ({ decision: "reject_unknown", detail: "Unknown" }),
    consumeCallback: () => false,
  };

  const poller = new TelegramPoller(
    "0:disposable-test",
    dir,
    { allowFrom: ["1"], dmPolicy: "allowlist" },
    {
      isIdle: () => true,
      onUserMessage: () => {},
      onSteer: () => {},
      onFollowUp: () => {},
      onAbort: () => {},
      onRelease: async () => {},
      onTelegramTurnStart: () => {},
      getStatusText: () => "test",
      onLedgerFailure: () => {},
    },
    bridge,
    { messageThreadId: options.thread, outboundPaceMs: 0 },
  );

  globalThis.fetch = (async (url, init) => {
    const method = String(url).split("/").pop()!;
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    calls.push({ method, body, at: Date.now() });

    if (method === "pinChatMessage") {
      if (failNextPin) {
        failNextPin = false;
        return Response.json({ ok: false, error_code: 400, description: "Bad Request: not enough rights to pin a message" });
      }
      operatorPinnedId = pinnedMessageId = Number(body.message_id);
      return Response.json({ ok: true, result: true });
    }

    if (method === "editMessageText") {
      if (failNextEdit) {
        const failure = failNextEdit;
        failNextEdit = null;
        return Response.json({ ok: false, error_code: failure.code, description: failure.description });
      }
      return Response.json({
        ok: true,
        result: { message_id: body.message_id, chat: { id: 1 }, date: Date.now() / 1000, text: body.text },
      });
    }

    if (method === "sendMessage") {
      const id = ++messageId;
      return Response.json({
        ok: true,
        result: { message_id: id, chat: { id: 1 }, date: Date.now() / 1000, text: body.text },
      });
    }

    return Response.json({ ok: true, result: {} });
  }) as typeof fetch;

  cleanup.push(() => {
    poller.stop();
    fs.rmSync(dir, { recursive: true, force: true });
  });

  const updatedKey = `dashboard:${sessionId}:1:${options.thread ?? 0}:updated`;

  return {
    dir,
    poller,
    calls,
    updatedKey,
    expireCooldown: () => { poller.setMeta(updatedKey, "0"); },
    unpinForOperator: () => { operatorPinnedId = null; },
    unpinCompletely: () => { operatorPinnedId = pinnedMessageId = null; },
    currentPin: () => pinnedMessageId,
    currentOperatorPin: () => operatorPinnedId,
    simulateDeletedOnEdit: () => {
      failNextEdit = { code: 400, description: "Bad Request: message to edit not found" };
    },
    simulateNotModifiedOnEdit: () => {
      failNextEdit = { code: 400, description: "Bad Request: message is not modified: specified new message content and reply markup are exactly the same as a current content and reply markup of the message" };
    },
    simulateTransientErrorOnEdit: () => {
      failNextEdit = { code: 500, description: "Internal Server Error: transient network timeout" };
    },
    simulatePinFailure: () => {
      failNextPin = true;
    },
  };
}

test("initial dashboard post sends message and pins it silently with disable_notification: true", async () => {
  const f = fixture();
  await f.poller.updateDashboard("1", "<b>Fleet v1</b>");

  const sendCalls = f.calls.filter(c => c.method === "sendMessage");
  expect(sendCalls).toHaveLength(1);
  expect(sendCalls[0].body.text).toContain("Fleet v1");

  const pinCalls = f.calls.filter(c => c.method === "pinChatMessage");
  expect(pinCalls).toHaveLength(1);
  expect(pinCalls[0].body.chat_id).toBe("1");
  expect(pinCalls[0].body.message_id).toBe(sendCalls[0].body.message_id ?? 201);
  expect(pinCalls[0].body.disable_notification).toBe(true);
  expect(f.currentPin()).toBe(201);
}, 10_000);

test("immediate consecutive updates within cooldown are coalesced without sending or pinning", async () => {
  const f = fixture();
  await f.poller.updateDashboard("1", "<b>Fleet v1</b>");
  expect(f.calls.filter(c => c.method === "sendMessage")).toHaveLength(1);
  expect(f.calls.filter(c => c.method === "pinChatMessage")).toHaveLength(1);

  // Calling again immediately without expiring cooldown
  await f.poller.updateDashboard("1", "<b>Fleet v2 Coalesced</b>");
  await f.poller.updateDashboard("1", "<b>Fleet v3 Coalesced</b>");

  expect(f.calls.filter(c => c.method === "sendMessage")).toHaveLength(1);
  expect(f.calls.filter(c => c.method === "editMessageText")).toHaveLength(0);
  expect(f.calls.filter(c => c.method === "pinChatMessage")).toHaveLength(1);
}, 10_000);

test("concurrent calls to updateDashboard share a single in-flight promise", async () => {
  const f = fixture();
  const [p1, p2, p3] = [
    f.poller.updateDashboard("1", "<b>Concurrent 1</b>"),
    f.poller.updateDashboard("1", "<b>Concurrent 2</b>"),
    f.poller.updateDashboard("1", "<b>Concurrent 3</b>"),
  ];
  await Promise.all([p1, p2, p3]);

  // Only one message sent and one pin call
  expect(f.calls.filter(c => c.method === "sendMessage")).toHaveLength(1);
  expect(f.calls.filter(c => c.method === "pinChatMessage")).toHaveLength(1);
}, 10_000);

test("subsequent update edits existing message in place without posting a new card", async () => {
  const f = fixture();
  await f.poller.updateDashboard("1", "<b>Initial</b>");
  expect(f.calls.filter(c => c.method === "sendMessage")).toHaveLength(1);

  f.expireCooldown();
  await f.poller.updateDashboard("1", "<b>Updated after cooldown</b>");

  expect(f.calls.filter(c => c.method === "sendMessage")).toHaveLength(1); // No new message
  const editCalls = f.calls.filter(c => c.method === "editMessageText");
  expect(editCalls).toHaveLength(1);
  expect(editCalls[0].body.message_id).toBe(201);
  expect(editCalls[0].body.text).toContain("Updated after cooldown");

  const pinCalls = f.calls.filter(c => c.method === "pinChatMessage");
  expect(pinCalls).toHaveLength(2); // Initial pin + silent re-assertion
  expect(pinCalls[1].body.disable_notification).toBe(true);
}, 15_000);

test("reasserts pin silently when operator unpinned the card without reposting", async () => {
  const f = fixture();
  await f.poller.updateDashboard("1", "<b>Fleet active</b>");
  expect(f.currentOperatorPin()).toBe(201);

  // Operator unpins in Telegram UI
  f.unpinForOperator();
  expect(f.currentOperatorPin()).toBeNull();

  // Cooldown expires, dashboard updates
  f.expireCooldown();
  await f.poller.updateDashboard("1", "<b>Fleet refreshed</b>");

  // Re-asserts pin on the existing card 201
  expect(f.currentOperatorPin()).toBe(201);
  expect(f.calls.filter(c => c.method === "sendMessage")).toHaveLength(1);
  expect(f.calls.filter(c => c.method === "editMessageText")).toHaveLength(1);
}, 15_000);

test("recovers deleted dashboard card by creating a new message and pinning the new ID", async () => {
  const f = fixture();
  await f.poller.updateDashboard("1", "<b>Initial card</b>");
  expect(f.calls.filter(c => c.method === "sendMessage")).toHaveLength(1);
  expect(f.currentPin()).toBe(201);

  // Message 201 is deleted on Telegram by a moderator/operator
  f.simulateDeletedOnEdit();
  f.expireCooldown();

  await f.poller.updateDashboard("1", "<b>Recreated card</b>");

  // A new message (ID 202) must be created
  const sendCalls = f.calls.filter(c => c.method === "sendMessage");
  expect(sendCalls).toHaveLength(2);
  expect(sendCalls[1].body.text).toContain("Recreated card");

  // And the new ID 202 is pinned
  const pinCalls = f.calls.filter(c => c.method === "pinChatMessage");
  expect(pinCalls).toHaveLength(2);
  expect(pinCalls[1].body.message_id).toBe(202);
  expect(pinCalls[1].body.disable_notification).toBe(true);
  expect(f.currentPin()).toBe(202);
}, 15_000);

test("transient edit failure preserves existing message ID without creating duplicate card", async () => {
  const f = fixture();
  await f.poller.updateDashboard("1", "<b>Initial card</b>");
  expect(f.calls.filter(c => c.method === "sendMessage")).toHaveLength(1);

  // Transient 500 network error on edit
  f.simulateTransientErrorOnEdit();
  f.expireCooldown();

  await expect(f.poller.updateDashboard("1", "<b>Edit fails transiently</b>"))
    .rejects.toThrow("Dashboard refresh delayed; existing card retained (no duplicate posted)");

  // No duplicate message was posted
  expect(f.calls.filter(c => c.method === "sendMessage")).toHaveLength(1);

  // Next successful edit after cooldown still targets original message 201
  f.expireCooldown();
  await f.poller.updateDashboard("1", "<b>Edit retry succeeds</b>");
  const editCalls = f.calls.filter(c => c.method === "editMessageText");
  expect(editCalls).toHaveLength(2);
  expect(editCalls[1].body.message_id).toBe(201);
  expect(f.calls.filter(c => c.method === "sendMessage")).toHaveLength(1);
}, 15_000);

test("edit returning 400 'message is not modified' is non-fatal and reasserts pin silently", async () => {
  const f = fixture();
  await f.poller.updateDashboard("1", "<b>Identical content</b>");

  f.simulateNotModifiedOnEdit();
  f.expireCooldown();

  // Does not throw
  await f.poller.updateDashboard("1", "<b>Identical content</b>");

  // Re-asserts pin silently despite 400 not modified
  const pinCalls = f.calls.filter(c => c.method === "pinChatMessage");
  expect(pinCalls).toHaveLength(2);
  expect(pinCalls[1].body.message_id).toBe(201);
  expect(pinCalls[1].body.disable_notification).toBe(true);
}, 15_000);

test("pin failure throws an actionable error with retry advice", async () => {
  const f = fixture();
  f.simulatePinFailure();

  await expect(f.poller.updateDashboard("1", "<b>Unpinnable</b>"))
    .rejects.toThrow("Dashboard pinChatMessage unavailable; check pin permission and Telegram retry window");
}, 10_000);

test("dashboard cards across different sessions and threads are strictly isolated in state", async () => {
  const f1 = fixture({ sessionId: "session-A", thread: 10 });
  await f1.poller.updateDashboard("1", "<b>Session A Dashboard</b>");
  expect(f1.calls.filter(c => c.method === "sendMessage")).toHaveLength(1);

  const f2 = fixture({ sessionId: "session-B", thread: 20 });
  await f2.poller.updateDashboard("1", "<b>Session B Dashboard</b>");
  expect(f2.calls.filter(c => c.method === "sendMessage")).toHaveLength(1);

  const key1 = "dashboard:session-A:1:10";
  const key2 = "dashboard:session-B:1:20";
  expect(f1.poller.getMeta(key1)).toBeTruthy();
  expect(f1.poller.getMeta(key2)).toBeNull();
  expect(f2.poller.getMeta(key2)).toBeTruthy();
  expect(f2.poller.getMeta(key1)).toBeNull();
}, 20_000);
