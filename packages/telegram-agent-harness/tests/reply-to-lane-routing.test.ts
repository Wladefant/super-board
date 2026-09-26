import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Database } from "bun:sqlite";
import { TelegramPoller } from "../extension/poller";
import { MessageContextStore } from "../src/message-context";
import type { MessageCorrelationBridge, OutboundMessageCorrelation, TelegramUpdate, ChannelAccessConfig, PollerOptions } from "../extension/types";

const originalFetch = globalThis.fetch;
const cleanup: Array<() => void> = [];
afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const close of cleanup.splice(0)) close();
});

function fixture(
  accessOverrides?: Partial<ChannelAccessConfig>,
  optionsOverrides?: Partial<PollerOptions>,
) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-reply-lane-"));
  const calls: Array<{ method: string; body: Record<string, unknown>; at: number }> = [];
  const messages = new Map<number, OutboundMessageCorrelation>();
  const userTurns: string[] = [];
  const steerTurns: string[] = [];
  let isSessionIdle = true;
  let messageCounter = 500;

  const bridge: MessageCorrelationBridge = {
    getSessionId: () => "main-session",
    getSlotId: () => "slot-test",
    record: row => {
      messages.set(row.messageId, { ...row });
    },
    resolveReply: (_botId, _chatId, id) => {
      const stored = messages.get(id);
      if (stored) {
        return {
          decision: "deliver",
          correlation: stored,
          detail: "Bound",
        };
      }
      return { decision: "reject_unknown", detail: "Unknown original message" };
    },
    resolveCallback: () => ({ decision: "reject_unknown", detail: "Unknown" }),
    consumeCallback: () => false,
  };

  const poller = new TelegramPoller(
    "0:disposable-test",
    dir,
    { allowFrom: ["1"], dmPolicy: "allowlist", groups: { "-1004422647618": {} }, ...accessOverrides },
    {
      isIdle: () => isSessionIdle,
      onUserMessage: text => userTurns.push(text),
      onSteer: text => steerTurns.push(text),
      onFollowUp: () => {},
      onAbort: () => {},
      onRelease: async () => {},
      getStatusText: () => "test",
      onLedgerFailure: () => {},
    },
    bridge,
    { outboundPaceMs: 0, ...optionsOverrides },
  );

  globalThis.fetch = (async (url, init) => {
    const method = String(url).split("/").pop()!;
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    calls.push({ method, body, at: Date.now() });
    return Response.json({
      ok: true,
      result: { message_id: body.message_id ?? ++messageCounter, chat: { id: 1 }, date: Date.now() / 1000, text: body.text },
    });
  }) as typeof fetch;

  cleanup.push(() => {
    poller.stop();
    fs.rmSync(dir, { recursive: true, force: true });
  });

  const reply = (replyToId: number, text: string, updateId = replyToId + 1000): TelegramUpdate => ({
    update_id: updateId,
    message: {
      message_id: updateId,
      chat: { id: 1, type: "private" },
      from: { id: 1, is_bot: false, first_name: "Operator" },
      date: Date.now() / 1000,
      text,
      reply_to_message: { message_id: replyToId, date: 0, chat: { id: 1 } },
    },
  });
  const forumMessage = (threadId: number, text: string, replyToId?: number, updateId = 8000 + Math.floor(Math.random() * 1000)): TelegramUpdate => ({
    update_id: updateId,
    message: {
      message_id: updateId,
      message_thread_id: threadId,
      chat: { id: -1004422647618, type: "supergroup" },
      from: { id: 1, is_bot: false, first_name: "Operator" },
      date: Date.now() / 1000,
      text,
      ...(replyToId ? { reply_to_message: { message_id: replyToId, date: 0, chat: { id: -1004422647618 } } } : {}),
    },
  });

  return {
    dir,
    poller,
    calls,
    messages,
    userTurns,
    steerTurns,
    reply,
    forumMessage,
    setIdle: (idle: boolean) => { isSessionIdle = idle; },
    setLaneState: (laneId: string, state: "active" | "exited" | "unknown") => {
      for (const msg of messages.values()) {
        if (msg.laneId === laneId) {
          msg.laneState = state;
        }
      }
    },
  };
}

test("outbound message records laneId and active laneState in correlation bridge", async () => {
  const f = fixture();
  const sent = await f.poller.sendTelegramMessage(
    "1",
    "Lane worker started",
    undefined,
    undefined,
    { laneId: "worker-auth", laneState: "active", requestId: "req-123" },
  );
  expect(sent?.ok).toBe(true);
  const msgId = sent?.result?.message_id!;
  expect(msgId).toBeGreaterThan(0);

  const stored = f.messages.get(msgId);
  expect(stored).toBeDefined();
  expect(stored?.laneId).toBe("worker-auth");
  expect(stored?.laneState).toBe("active");
  expect(stored?.requestId).toBe("req-123");
});

test("reply to active lane injects provenance and routes to Main without dead worker alert", async () => {
  const f = fixture();
  const sent = await f.poller.sendTelegramMessage(
    "1",
    "Build finished on worker-db",
    undefined,
    undefined,
    { laneId: "worker-db", laneState: "active" },
  );
  const msgId = sent?.result?.message_id!;

  f.poller.ingestUpdates([f.reply(msgId, "Please run migrations")]);
  await f.poller.redrivePendingUpdates();

  expect(f.userTurns).toHaveLength(1);
  const delivered = f.userTurns[0];

  // Provenance headers
  expect(delivered).toContain("[Concerning lane: worker-db; last reported state: active. Reply delivered to Main for dispatch, not directly to the lane.]");
  expect(delivered).toContain(`[Replying to Telegram post #${msgId}]`);
  expect(delivered).toContain("[Telegram sender: 1; origin: telegram_account]");
  expect(delivered).toContain("Please run migrations");

  // Did NOT send an exited notice to Telegram
  expect(f.calls.some(c => String(c.body.text).includes("has exited"))).toBe(false);
});

test("reply to exited lane injects exited state in provenance AND sends honest receipt to Telegram", async () => {
  const f = fixture();
  const sent = await f.poller.sendTelegramMessage(
    "1",
    "Task finished, worker shutting down",
    undefined,
    undefined,
    { laneId: "worker-temp", laneState: "exited" },
  );
  const msgId = sent?.result?.message_id!;

  f.poller.ingestUpdates([f.reply(msgId, "Can you check logs?")]);
  await f.poller.redrivePendingUpdates();

  // Delivered to Main with exited notice
  expect(f.userTurns).toHaveLength(1);
  const delivered = f.userTurns[0];
  expect(delivered).toContain("[Concerning lane: worker-temp; last reported state: exited. Reply delivered to Main for dispatch, not directly to the lane.]");
  expect(delivered).toContain("Can you check logs?");

  // Honest alert was sent back to Telegram
  const alert = f.calls.find(c => String(c.body.text).includes("worker-temp has exited"));
  expect(alert).toBeDefined();
  expect(alert?.body.text).toContain("<b>worker-temp has exited.</b> Your reply is going to Main with the original lane context, not to a dead worker.");
});

test("dynamic lane state transition: transitioning active to exited updates subsequent replies", async () => {
  const f = fixture();
  const sent = await f.poller.sendTelegramMessage(
    "1",
    "Worker started tests",
    undefined,
    undefined,
    { laneId: "worker-e2e", laneState: "active" },
  );
  const msgId = sent?.result?.message_id!;

  // First reply while active
  f.poller.ingestUpdates([f.reply(msgId, "First reply: stay active", 101)]);
  await f.poller.redrivePendingUpdates();
  expect(f.userTurns).toHaveLength(1);
  expect(f.userTurns[0]).toContain("last reported state: active");
  expect(f.calls.some(c => String(c.body.text).includes("has exited"))).toBe(false);

  // Lane exits in the session
  f.setLaneState("worker-e2e", "exited");

  // Second reply after exit
  f.poller.ingestUpdates([f.reply(msgId, "Second reply: after exit", 102)]);
  await f.poller.redrivePendingUpdates();

  expect(f.userTurns).toHaveLength(2);
  expect(f.userTurns[1]).toContain("last reported state: exited");
  expect(f.calls.some(c => String(c.body.text).includes("worker-e2e has exited"))).toBe(true);
});

test("multiple concurrent lanes route independently with distinct lane identities", async () => {
  const f = fixture();
  const sentA = await f.poller.sendTelegramMessage("1", "Message from Lane A", undefined, undefined, { laneId: "lane-A", laneState: "active" });
  const sentB = await f.poller.sendTelegramMessage("1", "Message from Lane B", undefined, undefined, { laneId: "lane-B", laneState: "active" });

  const idA = sentA?.result?.message_id!;
  const idB = sentB?.result?.message_id!;

  f.poller.ingestUpdates([
    f.reply(idA, "Directive for A", 201),
    f.reply(idB, "Directive for B", 202),
  ]);
  await f.poller.redrivePendingUpdates();

  expect(f.userTurns).toHaveLength(2);
  expect(f.userTurns[0]).toContain("Concerning lane: lane-A");
  expect(f.userTurns[0]).toContain("Directive for A");
  expect(f.userTurns[1]).toContain("Concerning lane: lane-B");
  expect(f.userTurns[1]).toContain("Directive for B");
});

test("routes to onUserMessage when session is idle and onSteer when session is busy", async () => {
  const f = fixture();
  const sent = await f.poller.sendTelegramMessage("1", "Status report", undefined, undefined, { laneId: "lane-exec", laneState: "active" });
  const msgId = sent?.result?.message_id!;

  // 1. Idle delivery
  f.setIdle(true);
  f.poller.ingestUpdates([f.reply(msgId, "Idle reply", 301)]);
  await f.poller.redrivePendingUpdates();
  expect(f.userTurns).toHaveLength(1);
  expect(f.steerTurns).toHaveLength(0);
  expect(f.userTurns[0]).toContain("Idle reply");

  // 2. Busy steering delivery
  f.setIdle(false);
  f.poller.ingestUpdates([f.reply(msgId, "Busy steer reply", 302)]);
  await f.poller.redrivePendingUpdates();
  expect(f.userTurns).toHaveLength(1);
  expect(f.steerTurns).toHaveLength(1);
  expect(f.steerTurns[0]).toContain("Busy steer reply");
});

test("reply to message without lane metadata preserves request context without lane header", async () => {
  const f = fixture();
  const sent = await f.poller.sendTelegramMessage(
    "1",
    "General orchestrator milestone",
    undefined,
    undefined,
    { requestId: "topic-superboard" },
  );
  const msgId = sent?.result?.message_id!;

  f.poller.ingestUpdates([f.reply(msgId, "Proceed with next slice")]);
  await f.poller.redrivePendingUpdates();

  expect(f.userTurns).toHaveLength(1);
  const delivered = f.userTurns[0];
  expect(delivered).not.toContain("Concerning lane:");
  expect(delivered).toContain(`[Replying to Telegram post #${msgId} | task: topic-superboard]`);
  expect(delivered).toContain("Proceed with next slice");
});

test("lane provenance persists in MessageContextStore SQLite database across instance lifecycles", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-context-store-"));
  const poolPath = path.join(dir, "pool.db");
  const initDb = new Database(poolPath);
  initDb.run("CREATE TABLE message_correlations(bot_id TEXT, chat_id TEXT, message_id INTEGER, session_id TEXT, PRIMARY KEY (bot_id, chat_id, message_id))");
  initDb.run("INSERT INTO message_correlations VALUES ('0', '1', 99, 'main-session')");
  initDb.close();

  const store = new MessageContextStore(poolPath);
  store.record("0", "1", 99, { laneId: "worker-crypto", laneState: "active" });
  store.close();

  const reopened = new MessageContextStore(poolPath);
  const lookup = reopened.lookup("0", "1", 99);
  expect(lookup.laneId).toBe("worker-crypto");
  expect(lookup.laneState).toBe("active");

  reopened.setLaneState("main-session", "worker-crypto", "exited");
  const updatedLookup = reopened.lookup("0", "1", 99);
  expect(updatedLookup.laneState).toBe("exited");
  reopened.close();

  fs.rmSync(dir, { recursive: true, force: true });
});
test("forum topic message pointing to topic creation root message routes to bound session without reply correlation rejection", async () => {
  const f = fixture();
  // In Telegram forum topics, standard messages often carry reply_to_message pointing to the topic creation root message (message_id === message_thread_id)
  f.poller.ingestUpdates([f.forumMessage(14, "hello from operator", 14)]);
  await f.poller.redrivePendingUpdates();

  expect(f.userTurns).toHaveLength(1);
  expect(f.userTurns[0]).toContain("hello from operator");
  expect(f.userTurns[0]).not.toContain("[Replying to Telegram post #14]");
  expect(f.calls.some(c => String(c.body.text).includes("Reply not routed"))).toBe(false);
});

test("forum topic message with forum_topic_created object routes as regular topic message", async () => {
  const f = fixture();
  const update: TelegramUpdate = {
    update_id: 8901,
    message: {
      message_id: 8901,
      message_thread_id: 58,
      chat: { id: -1004422647618, type: "supergroup" },
      from: { id: 1, is_bot: false, first_name: "Operator" },
      date: Date.now() / 1000,
      text: "investigate latency",
      reply_to_message: { message_id: 58, date: 0, chat: { id: -1004422647618 }, forum_topic_created: { name: "polysimulator" } },
    },
  };
  f.poller.ingestUpdates([update]);
  await f.poller.redrivePendingUpdates();

  expect(f.userTurns).toHaveLength(1);
  expect(f.userTurns[0]).toContain("investigate latency");
  expect(f.userTurns[0]).not.toContain("[Replying to Telegram post #58]");
  expect(f.calls.some(c => String(c.body.text).includes("Reply not routed"))).toBe(false);
});

test("reply to unindexed message in forum topic delivers to bound session instead of rejecting with reply not routed", async () => {
  const f = fixture();
  // Replying to an older unindexed message (e.g. from before daemon restart or another user) inside topic #48
  f.poller.ingestUpdates([f.forumMessage(48, "continuing discussion", 99999)]);
  await f.poller.redrivePendingUpdates();

  expect(f.userTurns).toHaveLength(1);
  expect(f.userTurns[0]).toContain("continuing discussion");
  expect(f.calls.some(c => String(c.body.text).includes("Reply not routed"))).toBe(false);
});

test("reply to unindexed message in direct chat (DM) is rejected with reply not routed", async () => {
  const f = fixture();
  // In DM, unindexed reply targets cannot be routed safely because multiple sessions could exist
  f.poller.ingestUpdates([f.reply(99999, "hello in dm")]);
  await f.poller.redrivePendingUpdates();

  expect(f.userTurns).toHaveLength(0);
  expect(f.calls.some(c => String(c.body.text).includes("Reply not routed"))).toBe(true);
});
