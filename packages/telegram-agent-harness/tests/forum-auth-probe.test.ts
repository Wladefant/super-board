// Reviewer probes from the independent review of PR #163 (head 5f57569).
// Drop into packages/telegram-agent-harness/tests/ to adopt as permanent coverage.
//
// These close finding 4: a bot-authored message in the served supergroup, and a
// callback query clicked inside a topic. Both behave correctly today; neither had a
// test. Same convention as the PR's own fixture — real TelegramPoller, real sqlite
// ledger read back through an independent handle, fetch faked at the HTTP boundary.
//
// Note: answerCallbackQuery is issued in runPollLoop right after ingest (poller.ts:700),
// ahead of the serial ledger queue, so it is NOT observable from redrivePendingUpdates.
// Do not add an assertion for it here.
import { afterEach, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { TelegramPoller, type PollerCallbacks } from "../extension/poller";
import type { MessageCorrelationBridge, TelegramUpdate } from "../extension/types";

const FORUM = "-1004422647618";
const OTHER_GROUP = "-1009999999999";
const OPERATOR = "1247617658";

const cleanup: Array<() => void> = [];
const originalFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const close of cleanup.splice(0)) close();
});

function build(access: Record<string, unknown>, options: Record<string, unknown>, bridge?: MessageCorrelationBridge) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "forum-auth-probe-"));
  const inbound: Array<{ text: string; threadId: number | undefined }> = [];
  const sent: Array<{ chatId: unknown; text: string; threadId: unknown }> = [];
  const callbacks: PollerCallbacks = {
    isIdle: () => true,
    onUserMessage: text => inbound.push({ text, threadId: poller.getActiveThreadId() }),
    onFollowUp: () => {},
    onSteer: () => {},
    onAbort: () => {},
    onRelease: async () => {},
    getStatusText: () => "probe",
    onLedgerFailure: () => {},
    onDecisionCallback: async (id, choice) => {
      inbound.push({ text: `decision:${id}:${choice}`, threadId: poller.getActiveThreadId() });
    },
  };
  const poller = new TelegramPoller(
    "0:test-only",
    dir,
    { dmPolicy: "allowlist", allowFrom: [OPERATOR], ...access } as never,
    callbacks,
    bridge,
    options as never,
  );
  globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
    const method = String(input).split("/").pop()!;
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (method === "sendMessage") {
      sent.push({ chatId: body.chat_id, text: String(body.text ?? ""), threadId: body.message_thread_id });
    }
    return Response.json({ ok: true, result: { message_id: 1, chat: { id: 1 }, date: 0 } });
  }) as typeof fetch;
  cleanup.push(() => {
    poller.stop();
    fs.rmSync(dir, { recursive: true, force: true });
  });
  const row = (updateId: number) => {
    const db = new Database(path.join(dir, "veyyon_bridge_state.db"), { readonly: true });
    try {
      return db
        .query("SELECT status, error, message_thread_id FROM update_ledger WHERE update_id = ?")
        .get(updateId) as { status: string; error: string | null; message_thread_id: number | null } | null;
    } finally {
      db.close();
    }
  };
  return { poller, inbound, sent, row };
}

function groupMessage(updateId: number, chat: string, fromId: string, isBot: boolean, threadId: number): TelegramUpdate {
  return {
    update_id: updateId,
    message: {
      message_id: updateId, date: 0, text: "deploy to prod",
      chat: { id: Number(chat), type: "supergroup" },
      from: { id: Number(fromId), is_bot: isBot, first_name: isBot ? "SomeBot" : "Operator" },
      message_thread_id: threadId,
    },
  } as TelegramUpdate;
}

test("a bot posting under an allowlisted id in a served topic is refused as non-operator", async () => {
  // The bot carries the operator's own allowlisted id, so the allowlist cannot be what
  // stops it: this isolates the sender_origin gate as the thing under test.
  const f = build({}, { forumChatId: FORUM });
  f.poller.ingestUpdates([groupMessage(1, FORUM, OPERATOR, true, 9)]);
  await f.poller.redrivePendingUpdates();
  expect(f.inbound).toEqual([]);
  expect(f.row(1)).toMatchObject({ status: "REJECTED", error: "NON_OPERATOR_ORIGIN" });
});

test("a non-allowlisted bot in a served topic is refused by the allowlist first", async () => {
  const f = build({}, { forumChatId: FORUM });
  f.poller.ingestUpdates([groupMessage(1, FORUM, "777888", true, 9)]);
  await f.poller.redrivePendingUpdates();
  expect(f.inbound).toEqual([]);
  expect(f.row(1)).toMatchObject({ status: "REJECTED", error: "UNAUTHORIZED" });
});

test("an allowlisted operator in a group that is not the slot's forum is refused", async () => {
  const f = build({}, { forumChatId: FORUM });
  f.poller.ingestUpdates([groupMessage(1, OTHER_GROUP, OPERATOR, false, 9)]);
  await f.poller.redrivePendingUpdates();
  expect(f.inbound).toEqual([]);
  expect(f.row(1)).toMatchObject({ status: "REJECTED", error: "UNAUTHORIZED" });
});

function topicCallback(updateId: number, threadId: number): TelegramUpdate {
  return {
    update_id: updateId,
    callback_query: {
      id: `click-${updateId}`,
      from: { id: Number(OPERATOR), is_bot: false, first_name: "Operator" },
      data: "cb:d_x",
      message: {
        message_id: 5, date: 0, caption: "Ship it?",
        chat: { id: Number(FORUM), type: "supergroup" },
        message_thread_id: threadId,
      },
    },
  } as unknown as TelegramUpdate;
}

test("a decision card clicked inside a topic authorizes, routes by thread, and answers in that topic", async () => {
  const record = {
    callbackToken: "cb:d_x", decisionId: "preference", choiceId: "Yes", sessionId: "sess-a",
    chatId: FORUM, userId: OPERATOR, questionHash: "h", expiresAt: Date.now() / 1000 + 100,
    consumedAt: null, createdAt: 0,
  };
  const seen: Array<{ chatId: string; userId: string }> = [];
  const bridge: MessageCorrelationBridge = {
    getSessionId: () => "sess-a",
    getSlotId: () => "slot",
    record: () => {},
    resolveReply: () => ({ decision: "reject_unknown", detail: "n/a" }),
    resolveCallback: (_token, userId, chatId) => {
      seen.push({ chatId, userId });
      return { decision: "deliver", record, detail: "ok" };
    },
    consumeCallback: () => true,
  };
  const f = build({}, { forumChatId: FORUM }, bridge);
  f.poller.ingestUpdates([topicCallback(1, 9)]);
  await f.poller.redrivePendingUpdates();

  // authorization carried the real chat and user through to the correlation bridge
  expect(seen).toEqual([{ chatId: FORUM, userId: OPERATOR }]);
  // the decision reached the session, tagged with the topic it was clicked in
  expect(f.inbound).toEqual([{ text: "decision:preference:Yes", threadId: 9 }]);
  // the topic survives in the ledger, not only in the poller's in-flight state
  expect(f.row(1)).toMatchObject({ status: "COMPLETED", message_thread_id: 9 });
  // and the confirmation went back into that topic rather than General
  const confirmation = f.sent.at(-1);
  expect(String(confirmation?.chatId)).toBe(FORUM);
  expect(confirmation?.threadId).toBe(9);
});

test("a card whose session is not the topic's session is refused in the topic it was clicked in", async () => {
  const bridge: MessageCorrelationBridge = {
    getSessionId: () => "sess-b",
    getSlotId: () => "slot",
    record: () => {},
    resolveReply: () => ({ decision: "reject_unknown", detail: "n/a" }),
    resolveCallback: () => ({ decision: "reject_foreign_session", detail: "Different session" }),
    consumeCallback: () => true,
  };
  const f = build({}, { forumChatId: FORUM }, bridge);
  f.poller.ingestUpdates([topicCallback(1, 14)]);
  await f.poller.redrivePendingUpdates();

  expect(f.inbound).toEqual([]);
  expect(f.row(1)?.status).toBe("REJECTED");
  const notice = f.sent.find(m => m.text.includes("not delivered"));
  expect(notice?.threadId).toBe(14);
});
