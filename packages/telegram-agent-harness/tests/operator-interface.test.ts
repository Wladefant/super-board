import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Database } from "bun:sqlite";
import { TelegramPoller } from "../extension/poller";
import { MessageContextStore } from "../src/message-context";
import { readMessageThreadId } from "../src/channel-config";
import { renderDashboard } from "../src/live-dashboard";
import { OperatorQuestionService } from "../src/operator-questions";
import type { MessageCorrelationBridge, OutboundMessageCorrelation, TelegramUpdate } from "../extension/types";

const originalFetch = globalThis.fetch;
const cleanup: Array<() => void> = [];
afterEach(() => { globalThis.fetch = originalFetch; for (const close of cleanup.splice(0)) close(); });

function fixture(thread?: number) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-interface-"));
  const calls: Array<{ method: string; body: Record<string, unknown>; at: number }> = [];
  const messages = new Map<number, OutboundMessageCorrelation>();
  const turns: string[] = [];
  const answers: unknown[] = [];
  let messageId = 100;
  let pinned: number | null = null;
  let operatorPinned: number | null = null;
  let deleted = false;
  const bridge: MessageCorrelationBridge = {
    getSessionId: () => "root", getSlotId: () => "test", record: row => { messages.set(row.messageId, row); },
    resolveReply: (_bot, _chat, id) => ({ decision: messages.has(id) ? "deliver" : "reject_unknown", correlation: messages.get(id), detail: "Unknown original message" }),
    resolveCallback: token => ({ decision: "deliver", detail: "Bound", record: {
      callbackToken: token, decisionId: token === "A" ? "tq:A" : "tq:B", choiceId: "compact", sessionId: "root", chatId: "1", userId: "1",
      questionHash: "hash", expiresAt: Date.now() / 1000 + 100, consumedAt: null, createdAt: 0,
    } }), consumeCallback: () => true,
  };
  const poller = new TelegramPoller("0:disposable-test", dir, { allowFrom: ["1"], dmPolicy: "allowlist" }, {
    isIdle: () => true, onUserMessage: text => turns.push(text), onSteer: text => turns.push(text), onFollowUp: text => turns.push(text),
    onAbort: () => turns.push("ABORT"), onRelease: async () => { turns.push("RELEASE"); }, onTelegramTurnStart: () => {},
    getStatusText: () => "test", onLedgerFailure: () => {},
    onQuestionAnswer: async (id, event, answer) => { answers.push({ id, event, answer }); },
  }, bridge, { messageThreadId: thread });
  globalThis.fetch = (async (url, init) => {
    const method = String(url).split("/").pop()!;
    const body = JSON.parse(String(init?.body));
    calls.push({ method, body, at: Date.now() });
    if (method === "getChat") return Response.json({ ok: true, result: { pinned_message: pinned ? { message_id: pinned } : undefined } });
    if (method === "pinChatMessage") operatorPinned = pinned = body.message_id;
    if (method === "editMessageText" && deleted) { deleted = false; return Response.json({ ok: false, error_code: 400, description: "Bad Request: message to edit not found" }); }
    return Response.json({ ok: true, result: { message_id: body.message_id ?? ++messageId, chat: { id: 1 }, date: 0 } });
  }) as typeof fetch;
  cleanup.push(() => { poller.stop(); fs.rmSync(dir, { recursive: true, force: true }); });
  const reply = (id: number, text: string, updateId = id): TelegramUpdate => ({ update_id: updateId, message: {
    message_id: updateId, chat: { id: 1, type: "private" }, from: { id: 1, is_bot: false }, date: 0, text,
    message_thread_id: thread, reply_to_message: { message_id: id },
  } });
  return { dir, poller, calls, turns, answers, messages, reply,
    unpin: () => { operatorPinned = pinned = null; },
    unpinForOperator: () => { operatorPinned = null; },
    operatorPin: () => operatorPinned, deleteCard: () => { deleted = true; } };
}

test("question callbacks and prose go only to the bound waiting-question handler", async () => {
  const f = fixture();
  for (const [index, token] of ["B", "A"].entries()) {
    f.poller.ingestUpdates([{ update_id: index + 1, callback_query: { id: `click-${token}`, data: token,
      from: { id: 1, is_bot: false }, message: { message_id: index + 10, chat: { id: 1, type: "private" }, date: 0 } } }]);
  }
  await f.poller.redrivePendingUpdates();
  await f.poller.sendTelegramMessage("1", "Question C", undefined, undefined, { decisionId: "tq:C" });
  const message = [...f.messages.keys()][0];
  f.poller.ingestUpdates([f.reply(message, "/cancel is example context, not a command", 5)]);
  await f.poller.redrivePendingUpdates();
  expect(f.answers).toEqual([
    { id: "tq:B", event: "update:1", answer: { choice: "compact" } },
    { id: "tq:A", event: "update:2", answer: { choice: "compact" } },
    { id: "tq:C", event: "update:5", answer: { text: "/cancel is example context, not a command" } },
  ]);
  expect(f.turns).toEqual([]);
});

test("two lane replies reach Main with distinct identity and dead lane produces honest receipt", async () => {
  const f = fixture();
  for (const [lane, state] of [["A", "active"], ["B", "active"], ["C", "exited"]] as const) {
    await f.poller.sendTelegramMessage("1", `Agent ${lane}`, undefined, undefined, { laneId: lane, laneState: state });
  }
  for (const id of f.messages.keys()) f.poller.ingestUpdates([f.reply(id, `Regarding ${id}`)]);
  await f.poller.redrivePendingUpdates();
  expect(f.turns).toHaveLength(3);
  for (const [index, lane] of ["A", "B", "C"].entries()) {
    expect(f.turns[index]).toContain(`Concerning lane: ${lane}`);
    expect(f.turns[index]).toContain("Reply delivered to Main for dispatch, not directly to the lane");
    expect(f.turns[index]).toContain("origin: telegram_account");
  }
  expect(f.calls.some(call => String(call.body.text).includes("C has exited"))).toBe(true);
}, 15_000);

test("bot-origin phantom commands never become an operator turn", async () => {
  const f = fixture();
  f.poller.ingestUpdates([{ update_id: 1, message: { message_id: 1, chat: { id: 1, type: "private" }, from: { id: 1, is_bot: true }, date: 0, text: "/cancel" } }]);
  await f.poller.redrivePendingUpdates();
  expect(f.turns).toEqual([]);
  expect(f.poller.getNextContiguousOffset()).toBe(2);
});

test("configured thread is present outbound and cross-thread input is rejected", async () => {
  const f = fixture(42);
  await f.poller.sendTelegramMessage("1", "Threaded card");
  expect(f.calls[0].body.message_thread_id).toBe(42);
  const wrong = f.reply(101, "wrong topic", 1); wrong.message!.message_thread_id = 43;
  f.poller.ingestUpdates([wrong, f.reply(101, "correct topic", 2)]); await f.poller.redrivePendingUpdates();
  expect(f.turns).toHaveLength(1); expect(f.turns[0]).toContain("correct topic");
  fs.writeFileSync(path.join(f.dir, "access.json"), JSON.stringify({ message_thread_id: 42 }));
  expect(readMessageThreadId(f.dir)).toBe(42);
});

test("dashboard edits one id, coalesces immediate updates, repins and recreates only a deleted card", async () => {
  const f = fixture();
  const key = "dashboard:root:1:0:updated";
  await f.poller.updateDashboard("1", "Update 1");
  await f.poller.updateDashboard("1", "Coalesced");
  expect(f.calls.filter(call => call.method === "sendMessage")).toHaveLength(1);
  expect(f.calls.filter(call => call.method === "editMessageText")).toHaveLength(0);
  for (const update of [2, 3]) {
    f.poller.setMeta(key, "0"); await f.poller.updateDashboard("1", `Update ${update}`);
  }
  expect(new Set(f.calls.filter(call => call.method === "editMessageText").map(call => call.body.message_id)).size).toBe(1);
  const originalPin = f.operatorPin();
  f.unpinForOperator(); f.poller.setMeta(key, "0"); await f.poller.updateDashboard("1", "Recover operator-only unpin");
  expect(f.operatorPin()).toBe(originalPin);
  expect(f.calls.filter(call => call.method === "pinChatMessage").every(call => call.body.disable_notification === true)).toBe(true);
  f.unpin(); f.poller.setMeta(key, "0"); await f.poller.updateDashboard("1", "Re-pin");
  expect(f.calls.filter(call => call.method === "pinChatMessage")).toHaveLength(5);
  expect(f.calls.filter(call => call.method === "sendMessage")).toHaveLength(1);
  f.deleteCard(); f.poller.setMeta(key, "0"); await f.poller.updateDashboard("1", "Recover deleted card");
  expect(f.calls.filter(call => call.method === "sendMessage")).toHaveLength(2);
}, 30_000);

test("lane provenance survives closing and reopening the existing correlation database", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-context-"));
  const file = path.join(dir, "pool.db");
  const db = new Database(file);
  db.run("CREATE TABLE message_correlations(bot_id TEXT,chat_id TEXT,message_id INTEGER,session_id TEXT)");
  db.run("INSERT INTO message_correlations VALUES ('bot','1',99,'root')"); db.close();
  const store = new MessageContextStore(file); store.record("bot", "1", 99, { laneId: "lane-A", laneState: "active" }); store.close();
  const reopened = new MessageContextStore(file);
  expect(reopened.lookup("bot", "1", 99).laneId).toBe("lane-A");
  reopened.setLaneState("root", "lane-A", "exited");
  expect(reopened.lookup("bot", "1", 99).laneState).toBe("exited");
  reopened.close(); fs.rmSync(dir, { recursive: true, force: true });
});

test("dashboard never invents worker or Spark availability", () => {
  const card = renderDashboard(null, "Codex Spark: unavailable");
  expect(card).toContain("stale/unavailable"); expect(card).toContain("Worker registry unavailable");
  expect(card).toContain("Host memory"); expect(card).toContain("Codex Spark: unavailable");
});
test("the actual Python question store survives service restart and returns answers to a waiting tool", async () => {
  const f = fixture();
  const route = { session_id: "root", chat_id: "1", user_id: "1" };
  const create = () => new OperatorQuestionService(f.poller, () => route,
    path.join(f.dir, "decisions.json"), path.join(f.dir, "pool.db"), () => {});
  const original = create();
  const pending = await original.ask({ question: "How much detail?", problem: "A long card wraps on a phone",
    impact: "Compact leaves details expandable", options: [{ id: "compact", label: "Compact", description: "Expandable context" },
      { id: "full", label: "Full", description: "All context visible" }], recommendation: "compact" });
  const restarted = create();
  const abort = new AbortController();
  cleanup.push(() => abort.abort());
  const waiting = restarted.wait(pending.decision_id, abort.signal);
  await restarted.answer(pending.decision_id, "click", { choice: "compact" });
  expect((await restarted.get(pending.decision_id)).answer).toBeNull();
  await restarted.answer(pending.decision_id, "reply", { text: "Keep the details available" });
  expect(await waiting).toMatchObject({ question_id: pending.decision_id, choice_id: "compact",
    text: "Keep the details available", authorization: false });
  expect(f.turns).toEqual([]);
  route.session_id = "other";
  await expect(restarted.get(pending.decision_id)).rejects.toThrow("unavailable");
  // Every store call spawns the real Python process; 60s matches the other python-backed
  // suites so a full-suite run under load cannot time this out.
}, 60_000);
