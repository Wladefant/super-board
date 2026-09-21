import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Database } from "bun:sqlite";
import { TelegramPoller } from "../extension/poller";
import { OperatorQuestionService, questionOperator, type QuestionRoute } from "../src/operator-questions";
import type { MessageCorrelationBridge, OutboundMessageCorrelation, TelegramUpdate } from "../extension/types";

const originalFetch = globalThis.fetch;
const cleanup: Array<() => void> = [];
afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const close of cleanup.splice(0)) close();
});

test("forum questions bind the authorized user, never the group identity", () => {
  const access = { dmPolicy: "allowlist", allowFrom: ["123", "456"], groups: { "-100": { allowFrom: ["123"] } } };
  expect(questionOperator(access, "-100")).toBe("123");
  expect(questionOperator(access, "456")).toBe("456");
  expect(() => questionOperator(access, "-999")).toThrow("exactly one authorized operator");
  expect(() => questionOperator({ ...access, groups: { "-100": {} } }, "-100")).toThrow("exactly one authorized operator");
  expect(() => questionOperator({ ...access, groups: { "-100": { allowFrom: ["789"] } } }, "-100")).toThrow("exactly one authorized operator");
});

interface QuestionFixtureOptions {
  route?: QuestionRoute;
  omitQuestionAnswerCallback?: boolean;
  questionAnswerError?: string;
}

function fixture(options: QuestionFixtureOptions = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-questions-"));
  const calls: Array<{ method: string; body: Record<string, unknown>; at: number }> = [];
  const messages = new Map<number, OutboundMessageCorrelation>();
  const turns: string[] = [];
  const questionEvents: Array<{ id: string; event: string; answer: { choice?: string; text?: string } }> = [];
  let messageCounter = 700;

  const currentRoute: QuestionRoute = options.route ?? {
    session_id: "test-session",
    chat_id: "1",
    user_id: "1",
  };

  const bridge: MessageCorrelationBridge = {
    getSessionId: () => currentRoute.session_id,
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
    resolveCallback: token => {
      return {
        decision: "deliver",
        detail: "Bound",
        record: {
          callbackToken: token,
          decisionId: token.startsWith("tq:") ? token : `tq:${token}`,
          choiceId: "opt-a",
          sessionId: currentRoute.session_id,
          chatId: currentRoute.chat_id,
          userId: currentRoute.user_id,
          questionHash: "hash",
          expiresAt: Date.now() / 1000 + 100,
          consumedAt: null,
          createdAt: 0,
        },
      };
    },
    consumeCallback: () => true,
  };

  let serviceInstance: OperatorQuestionService | undefined;

  const poller = new TelegramPoller(
    "0:disposable-test",
    dir,
    { allowFrom: ["1"], dmPolicy: "allowlist" },
    {
      isIdle: () => true,
      onUserMessage: text => turns.push(text),
      onSteer: text => turns.push(text),
      onFollowUp: () => {},
      onAbort: () => {},
      onRelease: async () => {},
      onTelegramTurnStart: () => {},
      getStatusText: () => "test",
      onLedgerFailure: () => {},
      onQuestionAnswer: options.omitQuestionAnswerCallback
        ? undefined
        : async (id, event, answer) => {
            questionEvents.push({ id, event, answer });
            if (options.questionAnswerError) throw new Error(options.questionAnswerError);
            if (serviceInstance) {
              await serviceInstance.answer(id, event, answer);
            }
          },
    },
    bridge,
    { outboundPaceMs: 0 },
  );

  const decisionsPath = path.join(dir, "decisions.json");
  const poolPath = path.join(dir, "pool.db");
  fs.writeFileSync(decisionsPath, JSON.stringify({ decisions: {} }));

  const service = new OperatorQuestionService(
    poller,
    () => currentRoute,
    decisionsPath,
    poolPath,
    () => {},
  );
  serviceInstance = service;

  globalThis.fetch = (async (url, init) => {
    const method = String(url).split("/").pop()!;
    const body = init?.body ? JSON.parse(String(init.body)) : {};
    calls.push({ method, body, at: Date.now() });

    if (method === "editMessageReplyMarkup") {
      return Response.json({ ok: true, result: true });
    }

    return Response.json({
      ok: true,
      result: {
        message_id: body.message_id ?? ++messageCounter,
        chat: { id: 1 },
        date: Date.now() / 1000,
        text: body.text,
      },
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

  return {
    dir,
    poller,
    service,
    calls,
    messages,
    turns,
    questionEvents,
    reply,
    decisionsPath,
    poolPath,
    currentRoute,
    getMessageId: (decisionId: string) => {
      const match = [...messages.values()].find(m => m.decisionId === decisionId);
      return match?.messageId;
    },
  };
}

test("service.ask sends formatted question card with buttons and decisionId starting with tq:", async () => {
  const f = fixture();
  const pending = await f.service.ask({
    question: "Which database migration strategy?",
    problem: "Locks could stall incoming orders",
    impact: "Option A avoids lock contention",
    recommendation: "opt-a",
    options: [
      { id: "opt-a", label: "Concurrent index", description: "Zero downtime" },
      { id: "opt-b", label: "Exclusive lock", description: "Maintenance window" },
    ],
  });

  expect(pending.decision_id).toMatch(/^tq:[a-z0-9]+/);
  expect(pending.status).toBe("pending");
  expect(pending.answer).toBeNull();

  // Telegram card was sent
  const sendCall = f.calls.find(c => c.method === "sendMessage");
  expect(sendCall).toBeDefined();
  expect(sendCall?.body.text).toContain("Which database migration strategy?");

  // Correlation was recorded with decisionId
  const questionMsgId = f.getMessageId(pending.decision_id);
  expect(questionMsgId).toBeDefined();
  expect(questionMsgId).toBeGreaterThan(0);
}, 20_000);

test("free-text reply to question card is intercepted, answered, and never triggers agent turn or steer", async () => {
  const f = fixture();
  const pending = await f.service.ask({
    question: "Do you prefer compact mode?",
    problem: "Details wrap on small screens",
    recommendation: "opt-a",
    options: [{ id: "opt-a", label: "Compact" }, { id: "opt-b", label: "Full" }],
  });

  const questionMsgId = f.getMessageId(pending.decision_id)!;
  const questionId = pending.decision_id;

  // Operator replies with free text to that question card
  f.poller.ingestUpdates([f.reply(questionMsgId, "Use compact mode for now")]);
  await f.poller.redrivePendingUpdates();

  // 1. Question answer was captured
  expect(f.questionEvents).toHaveLength(1);
  expect(f.questionEvents[0].id).toBe(questionId);
  expect(f.questionEvents[0].answer).toEqual({ text: "Use compact mode for now" });

  // 2. NO turn or steer was delivered to Main
  expect(f.turns).toHaveLength(0);

  // 3. Question status updated to answered
  const answered = await f.service.get(questionId);
  expect(answered.status).toBe("answered");
  expect(answered.answer?.text).toBe("Use compact mode for now");
}, 20_000);

test("free-text answer clears callback buttons and sends confirmation receipt to Telegram", async () => {
  const f = fixture();
  const pending = await f.service.ask({
    question: "Approve deployment to staging?",
    recommendation: "opt-a",
    options: [{ id: "opt-a", label: "Yes" }, { id: "opt-b", label: "No" }],
  });

  const questionMsgId = f.getMessageId(pending.decision_id)!;

  f.poller.ingestUpdates([f.reply(questionMsgId, "Approved via text")]);
  await f.poller.redrivePendingUpdates();

  // Buttons cleared
  const clearCall = f.calls.find(c => c.method === "editMessageReplyMarkup");
  expect(clearCall).toBeDefined();
  expect(clearCall?.body.message_id).toBe(questionMsgId);
  expect(clearCall?.body.reply_markup).toEqual({ inline_keyboard: [] });

  // Confirmation receipt sent
  const receiptCall = f.calls.find(c =>
    String(c.body.text).includes("Answer saved for this question"),
  );
  expect(receiptCall).toBeDefined();
  expect(receiptCall?.body.text).toContain("Returned to its waiting task, not sent as a new instruction");
}, 20_000);

test("parallel service.wait resolves promptly when free-text answer arrives", async () => {
  const f = fixture();
  const pending = await f.service.ask({
    question: "Select log level",
    recommendation: "opt-a",
    options: [{ id: "opt-a", label: "Debug" }, { id: "opt-b", label: "Info" }],
  });

  const questionMsgId = f.getMessageId(pending.decision_id)!;

  // Background wait call with 50ms poll interval
  const waitingPromise = f.service.wait(pending.decision_id, undefined, 50);

  // Telegram reply arrives while task is waiting
  f.poller.ingestUpdates([f.reply(questionMsgId, "Set level to info")]);
  await f.poller.redrivePendingUpdates();

  const answer = await waitingPromise;
  expect(answer).toMatchObject({
    question_id: pending.decision_id,
    text: "Set level to info",
    authorization: false,
  });
}, 20_000);

test("multimodal answer: choice callback followed by free text combines into complete answer", async () => {
  const f = fixture();
  const pending = await f.service.ask({
    question: "Choose retry strategy",
    recommendation: "opt-a",
    options: [{ id: "opt-a", label: "Exponential" }, { id: "opt-b", label: "Linear" }],
  });

  const questionMsgId = f.getMessageId(pending.decision_id)!;
  const questionId = pending.decision_id;

  // 1. Choice button callback clicked
  await f.service.answer(questionId, "event-click", { choice: "opt-a" });

  // Production state machine preserves selection vs submission distinction:
  // Selecting an option records selection but keeps question pending and answer null
  const intermediate = await f.service.get(questionId);
  expect(intermediate.status).toBe("pending");
  expect(intermediate.answer).toBeNull();
  expect(intermediate.transport.selection).toBe("opt-a");

  // 2. Free-text elaboration added via reply to the question card
  f.poller.ingestUpdates([f.reply(questionMsgId, "Max 5 retries with 500ms backoff", 901)]);
  await f.poller.redrivePendingUpdates();

  const result = await f.service.get(questionId);
  expect(result.status).toBe("answered");
  expect(result.answer).toMatchObject({
    question_id: questionId,
    choice_id: "opt-a",
    text: "Max 5 retries with 500ms backoff",
    origin: "telegram_account",
    actor_id: "1",
    authorization: false,
  });

  // Production state machine rejects answer mutation after answering
  await expect(
    f.service.answer(questionId, "event-mutate", { text: "Attempt to mutate answer" }),
  ).rejects.toThrow("Answer already sent; reopen the question to change it");

  const unmutated = await f.service.get(questionId);
  expect(unmutated.answer?.text).toBe("Max 5 retries with 500ms backoff");
  expect(unmutated.answer?.choice_id).toBe("opt-a");
}, 20_000);

test("question cannot be accessed or answered from an unrelated session route", async () => {
  const f = fixture({
    route: { session_id: "session-alpha", chat_id: "1", user_id: "1" },
  });

  const pending = await f.service.ask({
    question: "Session alpha question",
    recommendation: "opt-a",
    options: [{ id: "opt-a", label: "OK" }],
  });

  // Switch active session route to session-beta
  f.currentRoute.session_id = "session-beta";

  // Re-reading or answering from different session is refused
  await expect(f.service.get(pending.decision_id)).rejects.toThrow("unavailable on this session");
}, 20_000);

test("throwing onQuestionAnswer callback sends error notice to Telegram and fails delivery", async () => {
  const f = fixture({ questionAnswerError: "Session answer store locked" });
  const pending = await f.service.ask({
    question: "Test question",
    recommendation: "opt-a",
    options: [{ id: "opt-a", label: "OK" }],
  });
  const questionMsgId = f.getMessageId(pending.decision_id)!;

  f.poller.ingestUpdates([f.reply(questionMsgId, "Should fail delivery")]);
  await f.poller.redrivePendingUpdates();

  // Did NOT deliver turn to Main
  expect(f.turns).toHaveLength(0);

  // Delivery failure was reported back to Telegram
  const errorCall = f.calls.find(c => String(c.body.text).includes("Answer not delivered:"));
  expect(errorCall).toBeDefined();
  expect(errorCall?.body.text).toContain("Session answer store locked");
}, 20_000);

test("missing onQuestionAnswer callback marks ledger update as REJECTED", async () => {
  const f = fixture({ omitQuestionAnswerCallback: true });
  const sent = await f.poller.sendTelegramMessage("1", "Unanswered card", undefined, undefined, {
    decisionId: "tq:orphan",
  });
  const questionMsgId = sent?.result?.message_id!;

  f.poller.ingestUpdates([f.reply(questionMsgId, "Should fail delivery", 888)]);
  await f.poller.redrivePendingUpdates();

  const db = new Database(path.join(f.dir, "veyyon_bridge_state.db"), { readonly: true });
  const row = db.query("SELECT status, error FROM update_ledger WHERE update_id = 888").get() as { status: string; error: string } | null;
  db.close();

  expect(row?.status).toBe("REJECTED");
  expect(row?.error).toContain("Question receiver unavailable");
}, 20_000);

test("wait times out after timeoutMs and returns pending question with progress updates", async () => {
  const f = fixture();
  const pending = await f.service.ask({
    question: "Test question",
    recommendation: "opt-a",
    options: [{ id: "opt-a", label: "OK" }],
  });
  const progress: number[] = [];
  const result = await f.service.wait(pending.decision_id, undefined, 50, 200, elapsed => {
    progress.push(elapsed);
  });
  expect(result).toBeDefined();
  expect("status" in result && result.status).toBe("pending");
  expect("decision_id" in result && result.decision_id).toBe(pending.decision_id);
  expect(progress.length).toBeGreaterThan(0);
}, 10_000);

test("empty or whitespace-only reply is rejected as EMPTY_TEXT and never delivered to turns", async () => {
  const f = fixture();
  const sent = await f.poller.sendTelegramMessage("1", "Original message", undefined, undefined, {
    laneId: "lane-1",
    laneState: "active",
  });
  const msgId = sent?.result?.message_id!;

  f.poller.ingestUpdates([f.reply(msgId, "   \u200b  ", 890)]);
  await f.poller.redrivePendingUpdates();

  expect(f.turns).toHaveLength(0);
  const db = new Database(path.join(f.dir, "veyyon_bridge_state.db"), { readonly: true });
  const row = db.query("SELECT status, error FROM update_ledger WHERE update_id = 890").get() as { status: string; error: string } | null;
  db.close();
  expect(row?.status).toBe("REJECTED");
  expect(row?.error).toBe("EMPTY_TEXT");
}, 10_000);
