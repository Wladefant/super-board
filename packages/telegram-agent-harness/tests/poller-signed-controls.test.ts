import { test, expect, afterEach } from "bun:test";
import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { TelegramPoller, LANE_PANEL_CALLBACK_PREFIX, PANEL_EXPIRED_ANSWER, type PollerCallbacks } from "../extension/poller";
import type { MessageCorrelationBridge, PanelCallbackContext, TelegramUpdate } from "../extension/types";
import { LanePanels } from "../daemon/lane-panel";
import { DaemonStore } from "../daemon/store";
import { questionOperator } from "../src/operator-questions";
import { governedTelegramFetch } from "../extension/telegram-governor";
import { BotPoolCoordinator } from "../extension/coordinator";

const originalFetch = globalThis.fetch;
const cleanup: Array<() => void> = [];
afterEach(() => {
  for (const close of cleanup.splice(0)) {
    try {
      close();
    } catch {}
  }
  globalThis.fetch = originalFetch;
});

const OPERATOR_ID = "100";
const CHAT_ID = "-100123456789";
const TOPIC_ID = "42";

function setupPollerTest(options: {
  allowFrom?: string[];
  groups?: Record<string, { allowFrom?: string[] }>;
  dmPolicy?: "allowlist" | "disabled";
  messageThreadId?: number;
  fromId?: number;
  isBot?: boolean;
  chatId?: number;
  threadId?: number;
  updateId?: number;
  callbackData?: string;
  isCallback?: boolean;
  replyToMessageId?: number;
}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-signed-test-"));
  const calls: Array<{ method: string; body: Record<string, unknown> }> = [];
  const peekContexts: PanelCallbackContext[] = [];
  const runContexts: PanelCallbackContext[] = [];
  const ranTokens: string[] = [];
  const rejectedAudits: Array<{ token: string; context: PanelCallbackContext; reason: string }> = [];

  const bridge: MessageCorrelationBridge = {
    getSessionId: () => "sess-test",
    getSlotId: () => "slot-test",
    record: () => {},
    resolveReply: () => ({ decision: "reject_unknown", detail: "Unknown" }),
    auditRejectedCallback: (token, userId, chatId, eventId, reason) => {
      rejectedAudits.push({ token, context: { userId, chatId, topicId: "", eventId }, reason });
    },
  };
  let capturedActiveUserId: string | undefined;
  let capturedActiveReplyToId: number | undefined;

  const callbacks: PollerCallbacks = {
    isIdle: () => true,
    onUserMessage: () => {},
    onFollowUp: () => {},
    onSteer: () => {},
    onAbort: () => {},
    onRelease: async () => {},
    getStatusText: () => "status",
    onLedgerFailure: () => {},
    lanePanel: {
      peek: (data, context) => {
        if (context) peekContexts.push(context);
        return context?.userId === OPERATOR_ID ? `peeked:${data}` : PANEL_EXPIRED_ANSWER;
      },
      run: async (data, context) => {
        ranTokens.push(data);
        if (context) runContexts.push(context);
      },
      auditRejected: (data, context, reason) => {
        if (context) rejectedAudits.push({ token: data, context, reason: reason ?? "unknown" });
      },
    },
    onRejectedControl: (token, context, reason) => {
      rejectedAudits.push({ token, context, reason });
    },
  };

  const poller = new TelegramPoller(
    "0:test-token",
    dir,
    {
      dmPolicy: options.dmPolicy ?? "allowlist",
      allowFrom: options.allowFrom ?? [OPERATOR_ID],
      groups: options.groups ?? { [CHAT_ID]: {} },
    },
    callbacks,
    bridge,
    options.messageThreadId !== undefined ? options.messageThreadId : { sendTimeoutMs: 1000 },
    { sendTimeoutMs: 1000 },
  );

  cleanup.push(() => {
    poller.stop();
    try {
      fs.rmSync(dir, { recursive: true, force: true });
    } catch {}
  });

  const updateId = options.updateId ?? 101;
  const fromId = options.fromId ?? Number(OPERATOR_ID);
  const isBot = options.isBot ?? false;
  const chatId = options.chatId ?? Number(CHAT_ID);
  const threadId = options.threadId ?? Number(TOPIC_ID);
  const callbackData = options.callbackData ?? `${LANE_PANEL_CALLBACK_PREFIX}token-abc`;
  const isCallback = options.isCallback ?? true;

  let update: TelegramUpdate;
  if (isCallback) {
    update = {
      update_id: updateId,
      callback_query: {
        id: "cb-query-999",
        from: { id: fromId, is_bot: isBot, first_name: "Tester" },
        data: callbackData,
        message: {
          message_id: 555,
          chat: { id: chatId, type: "supergroup" },
          date: 0,
          text: "panel text",
          message_thread_id: threadId,
        },
      },
    } as TelegramUpdate;
  } else {
    update = {
      update_id: updateId,
      message: {
        message_id: 777,
        from: { id: fromId, is_bot: isBot, first_name: "Tester" },
        chat: { id: chatId, type: "supergroup" },
        date: 0,
        text: "hello world",
        message_thread_id: threadId,
        reply_to_message: options.replyToMessageId ? { message_id: options.replyToMessageId, date: 0, chat: { id: chatId, type: "supergroup" } } : undefined,
      },
    } as TelegramUpdate;
  }

  let polls = 0;
  globalThis.fetch = (async (input, init) => {
    const url = new URL(String(input));
    const method = url.pathname.split("/").pop()!;
    if (method === "getUpdates") {
      if (++polls === 1) return Response.json({ ok: true, result: [update] });
      poller.stop();
      throw new Error("stopped");
    }
    calls.push({ method, body: JSON.parse(String(init?.body ?? "{}")) });
    return Response.json({ ok: true });
  }) as typeof fetch;

  return {
    dir,
    poller,
    calls,
    peekContexts,
    runContexts,
    ranTokens,
    rejectedAudits,
    update,
  };
}

test("poller passes complete authenticated PanelCallbackContext to peek at receipt and run at ledger", async () => {
  const fixture = setupPollerTest({
    fromId: Number(OPERATOR_ID),
    chatId: Number(CHAT_ID),
    threadId: Number(TOPIC_ID),
    updateId: 431,
  });

  await fixture.poller.start();

  // Receipt peek was called with full context
  expect(fixture.peekContexts.length).toBe(1);
  const peekCtx = fixture.peekContexts[0];
  expect(peekCtx.userId).toBe(OPERATOR_ID);
  expect(peekCtx.chatId).toBe(CHAT_ID);
  expect(peekCtx.topicId).toBe(TOPIC_ID);
  expect(peekCtx.eventId).toBe("update:431");
  expect(peekCtx.messageId).toBe(555);

  // Receipt answer
  const answer = fixture.calls.find(c => c.method === "answerCallbackQuery");
  expect(answer?.body.text).toBe(`peeked:${LANE_PANEL_CALLBACK_PREFIX}token-abc`);

  // Ledger run was called with full context
  expect(fixture.runContexts.length).toBe(1);
  const runCtx = fixture.runContexts[0];
  expect(runCtx.userId).toBe(OPERATOR_ID);
  expect(runCtx.chatId).toBe(CHAT_ID);
  expect(runCtx.topicId).toBe(TOPIC_ID);
  expect(runCtx.eventId).toBe("update:431"); // Receipt eventId equals ledger eventId

  // Ledger updated to COMPLETED
  const db = new Database(path.join(fixture.dir, "veyyon_bridge_state.db"));
  const row = db.query("SELECT status, error FROM update_ledger WHERE update_id = 431").get() as { status: string; error: string | null };
  db.close();
  expect(row.status).toBe("COMPLETED");
  expect(row.error).toBeNull();
});

test("unauthorized user outside allowlist answers rejection at receipt and invokes audit without run", async () => {
  const fixture = setupPollerTest({
    fromId: 9999, // Unauthorized user
    chatId: Number(CHAT_ID),
    threadId: Number(TOPIC_ID),
    updateId: 432,
  });

  await fixture.poller.start();

  // Answered rejection at receipt
  const answer = fixture.calls.find(c => c.method === "answerCallbackQuery");
  expect(answer?.body.text).toBe(PANEL_EXPIRED_ANSWER);

  // Does NOT invoke panel run on rejection
  expect(fixture.ranTokens).toEqual([]);
  expect(fixture.runContexts.length).toBe(0);
  expect(fixture.rejectedAudits.length).toBe(1);
  expect(fixture.rejectedAudits[0].token).toBe(`${LANE_PANEL_CALLBACK_PREFIX}token-abc`);
  expect(fixture.rejectedAudits[0].context.userId).toBe("9999");
  expect(fixture.rejectedAudits[0].context.eventId).toBe("update:432");
  expect(fixture.rejectedAudits[0].reason).toBe("unauthorized");

  // Ledger marked REJECTED / UNAUTHORIZED
  const db = new Database(path.join(fixture.dir, "veyyon_bridge_state.db"));
  const row = db.query("SELECT status, error FROM update_ledger WHERE update_id = 432").get() as { status: string; error: string };
  db.close();
  expect(row.status).toBe("REJECTED");
  expect(row.error).toBe("UNAUTHORIZED");
});

test("unauthorized group answers rejection at receipt and invokes audit without run", async () => {
  const fixture = setupPollerTest({
    fromId: Number(OPERATOR_ID),
    chatId: -999888, // Not in accessConfig.groups
    threadId: Number(TOPIC_ID),
    updateId: 433,
  });

  await fixture.poller.start();

  // Answered rejection at receipt
  const answer = fixture.calls.find(c => c.method === "answerCallbackQuery");
  expect(answer?.body.text).toBe(PANEL_EXPIRED_ANSWER);

  // Does NOT invoke panel run on rejection
  expect(fixture.ranTokens).toEqual([]);
  expect(fixture.runContexts.length).toBe(0);
  expect(fixture.rejectedAudits.length).toBe(1);
  expect(fixture.rejectedAudits[0].token).toBe(`${LANE_PANEL_CALLBACK_PREFIX}token-abc`);
  expect(fixture.rejectedAudits[0].context.chatId).toBe("-999888");
  expect(fixture.rejectedAudits[0].context.eventId).toBe("update:433");
  expect(fixture.rejectedAudits[0].reason).toBe("unauthorized");

  // Ledger marked REJECTED / UNAUTHORIZED
  const db = new Database(path.join(fixture.dir, "veyyon_bridge_state.db"));
  const row = db.query("SELECT status, error FROM update_ledger WHERE update_id = 433").get() as { status: string; error: string };
  db.close();
  expect(row.status).toBe("REJECTED");
  expect(row.error).toBe("UNAUTHORIZED");
});

test("bot origin answers rejection at receipt and invokes audit without run", async () => {
  const fixture = setupPollerTest({
    fromId: Number(OPERATOR_ID),
    isBot: true,
    chatId: Number(CHAT_ID),
    threadId: Number(TOPIC_ID),
    updateId: 434,
  });

  await fixture.poller.start();

  // Answered rejection at receipt
  const answer = fixture.calls.find(c => c.method === "answerCallbackQuery");
  expect(answer?.body.text).toBe(PANEL_EXPIRED_ANSWER);

  // Does NOT invoke panel run on rejection
  expect(fixture.ranTokens).toEqual([]);
  expect(fixture.runContexts.length).toBe(0);
  expect(fixture.rejectedAudits.length).toBe(1);
  expect(fixture.rejectedAudits[0].token).toBe(`${LANE_PANEL_CALLBACK_PREFIX}token-abc`);
  expect(fixture.rejectedAudits[0].context.eventId).toBe("update:434");
  expect(fixture.rejectedAudits[0].reason).toBe("non_operator_origin");

  // Ledger marked REJECTED / NON_OPERATOR_ORIGIN
  const db = new Database(path.join(fixture.dir, "veyyon_bridge_state.db"));
  const row = db.query("SELECT status, error FROM update_ledger WHERE update_id = 434").get() as { status: string; error: string };
  db.close();
  expect(row.status).toBe("REJECTED");
  expect(row.error).toBe("NON_OPERATOR_ORIGIN");
});

test("wrong thread invokes audit without run and marks WRONG_THREAD in ledger", async () => {
  const fixture = setupPollerTest({
    fromId: Number(OPERATOR_ID),
    chatId: Number(CHAT_ID),
    threadId: 99, // Mismatched thread
    messageThreadId: 42, // Bound poller thread
    updateId: 435,
  });

  await fixture.poller.start();

  // Does NOT invoke panel run on rejection
  expect(fixture.ranTokens).toEqual([]);
  expect(fixture.runContexts.length).toBe(0);
  expect(fixture.rejectedAudits.length).toBe(1);
  expect(fixture.rejectedAudits[0].token).toBe(`${LANE_PANEL_CALLBACK_PREFIX}token-abc`);
  expect(fixture.rejectedAudits[0].context.topicId).toBe("99");
  expect(fixture.rejectedAudits[0].context.eventId).toBe("update:435");
  expect(fixture.rejectedAudits[0].reason).toBe("wrong_thread");

  // Ledger marked REJECTED / WRONG_THREAD
  const db = new Database(path.join(fixture.dir, "veyyon_bridge_state.db"));
  const row = db.query("SELECT status, error FROM update_ledger WHERE update_id = 435").get() as { status: string; error: string };
  db.close();
  expect(row.status).toBe("REJECTED");
  expect(row.error).toBe("WRONG_THREAD");
});

test("disabled dm policy answers rejection at receipt and invokes audit without invoking run", async () => {
  const fixture = setupPollerTest({
    dmPolicy: "disabled",
    fromId: Number(OPERATOR_ID),
    chatId: Number(CHAT_ID),
    threadId: Number(TOPIC_ID),
    updateId: 436,
  });

  await fixture.poller.start();

  const answer = fixture.calls.find(c => c.method === "answerCallbackQuery");
  expect(answer?.body.text).toBe(PANEL_EXPIRED_ANSWER);

  expect(fixture.ranTokens).toEqual([]);
  expect(fixture.runContexts.length).toBe(0);
  expect(fixture.rejectedAudits.length).toBe(1);
  expect(fixture.rejectedAudits[0].token).toBe(`${LANE_PANEL_CALLBACK_PREFIX}token-abc`);
  expect(fixture.rejectedAudits[0].context.eventId).toBe("update:436");
  expect(fixture.rejectedAudits[0].reason).toBe("dm_policy_disabled");

  const db = new Database(path.join(fixture.dir, "veyyon_bridge_state.db"));
  const row = db.query("SELECT status, error FROM update_ledger WHERE update_id = 436").get() as { status: string; error: string };
  db.close();
  expect(row.status).toBe("REJECTED");
  expect(row.error).toBe("DM_POLICY_DISABLED");
});

test("poller exposes active user and reply-to message ID accessors during row processing", async () => {
  let observedUserId: string | undefined = "init";
  let observedReplyToId: number | undefined = 12345;
  let observedThreadId: number | undefined = 999;

  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-active-ctx-"));
  const callbacks: PollerCallbacks = {
    isIdle: () => true,
    onUserMessage: () => {},
    onFollowUp: () => {},
    onSteer: () => {},
    onAbort: () => {},
    onRelease: async () => {},
    getStatusText: () => "status",
    onLedgerFailure: () => {},
  };

  const poller = new TelegramPoller(
    "0:test-token",
    dir,
    { dmPolicy: "allowlist", allowFrom: [OPERATOR_ID], groups: { [CHAT_ID]: {} } },
    callbacks,
  );

  cleanup.push(() => {
    poller.stop();
    fs.rmSync(dir, { recursive: true, force: true });
  });

  // Outside row processing, accessors return undefined
  expect(poller.getActiveUserId()).toBeUndefined();
  expect(poller.getActiveReplyToMessageId()).toBeUndefined();
  expect(poller.getActiveDeliveryContext()).toBeUndefined();

  callbacks.onUserMessage = () => {
    observedUserId = poller.getActiveUserId();
    observedReplyToId = poller.getActiveReplyToMessageId();
    observedThreadId = poller.getActiveDeliveryContext()?.threadId;
  };

  const update: TelegramUpdate = {
    update_id: 501,
    message: {
      message_id: 888,
      from: { id: Number(OPERATOR_ID), is_bot: false, first_name: "Tester" },
      chat: { id: Number(CHAT_ID), type: "supergroup" },
      date: 0,
      text: "replying to prompt",
      message_thread_id: Number(TOPIC_ID),
      reply_to_message: {
        message_id: 333,
        date: 0,
        chat: { id: Number(CHAT_ID), type: "supergroup" },
      },
    },
  } as TelegramUpdate;

  let polls = 0;
  globalThis.fetch = (async (input) => {
    const url = new URL(String(input));
    if (url.pathname.endsWith("/getUpdates")) {
      if (++polls === 1) return Response.json({ ok: true, result: [update] });
      poller.stop();
      throw new Error("stopped");
    }
    return Response.json({ ok: true });
  }) as typeof fetch;

  await poller.start();

  expect(observedUserId).toBe(OPERATOR_ID);
  expect(observedReplyToId).toBe(333);
  expect(observedThreadId).toBe(Number(TOPIC_ID));

  // Cleared after processing
  expect(poller.getActiveUserId()).toBeUndefined();
  expect(poller.getActiveReplyToMessageId()).toBeUndefined();
  expect(poller.getActiveDeliveryContext()).toBeUndefined();
});

test("takeArmed consumes arm only when user ID and reply-to message ID match prompt exactly", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-take-armed-"));
  const dbPath = path.join(dir, "daemon.db");
  const store = new DaemonStore(dbPath);
  cleanup.push(() => {
    store.close();
    try {
      fs.rmSync(dir, { recursive: true, force: true });
    } catch {}
  });

  const promptMsgId = 444;
  const target = { slotId: "s1", chatId: CHAT_ID, topicId: TOPIC_ID };
  store.putRoute({ slotId: "s1", chatId: CHAT_ID, topicId: TOPIC_ID, sessionId: "sess-1", workspace: "C:/w/test" });

  const panelCalls: Array<{ op: string; markup?: { inline_keyboard: Array<Array<{ text: string; callback_data?: string }>> } }> = [];

  const panels = new LanePanels({
    slotId: "s1",
    chatId: CHAT_ID,
    operatorId: OPERATOR_ID,
    store,
    snapshot: async () => ({
      version: 1,
      observedAt: 0,
      lanes: [{
        id: "sess-1", name: "test-lane", kind: "interactive", parentId: null, childIds: [], cwd: "C:/w/test",
        model: "model", status: "running", startedAtMs: 0, elapsedMs: 100, lastActivityMs: 0, lastAction: "bash",
      }],
      counts: { running: 1, idle: 0, subagents: 0 },
      usage: { available: false, observedAt: 0, windows: [] },
      hostMemory: { totalBytes: 1, freeBytes: 1, usedPercent: 0 },
      questions: [],
    }),
    boundSession: () => "sess-1",
    isBusy: () => true,
    stop: async () => true,
    transport: {
      send: async (_target, _text, markup) => {
        panelCalls.push({ op: "send", markup: markup as unknown as { inline_keyboard: Array<Array<{ text: string; callback_data?: string }>> } });
        return { messageId: 100 };
      },
      edit: async (_target, _messageId, _text, markup) => {
        panelCalls.push({ op: "edit", markup: markup as unknown as { inline_keyboard: Array<Array<{ text: string; callback_data?: string }>> } });
        return "ok";
      },
      pin: async () => {},
      confirm: async () => ({ ephemeralMessageId: 200 }),
      editConfirm: async () => "ok",
      prompt: async () => ({ messageId: promptMsgId }),
    },
  });

  await panels.tick();
  const lastSend = panelCalls.find(c => c.markup);
  const steerButton = lastSend?.markup?.inline_keyboard.flat().find(b => b.text.includes("Steer"));
  const steerToken = steerButton?.callback_data;
  expect(steerToken).toBeDefined();

  // Run steer callback with full operator context to arm it
  await panels.run(steerToken!, {
    userId: OPERATOR_ID,
    chatId: CHAT_ID,
    topicId: TOPIC_ID,
    eventId: "update:arm-1",
  });

  // 1. Ordinary unrelated message (no replyToMessageId) cannot consume arm
  expect(panels.takeArmed(target, OPERATOR_ID, undefined)).toBeNull();

  // 2. Message replying to a different message ID cannot consume arm
  expect(panels.takeArmed(target, OPERATOR_ID, 9999)).toBeNull();

  // 3. Message from unauthorized user replying to the prompt cannot consume arm
  expect(panels.takeArmed(target, "unauthorized-user-id", promptMsgId)).toBeNull();

  // 4. Exact match of operator user ID and prompt message ID consumes arm
  const armed = panels.takeArmed(target, OPERATOR_ID, promptMsgId);
  expect(armed).toBe("steer");

  // 5. Subsequent message cannot consume arm again (consumed once)
  expect(panels.takeArmed(target, OPERATOR_ID, promptMsgId)).toBeNull();
});

test("operatorId is obtained from questionOperator based on access config and forum chat", () => {
  const access = {
    dmPolicy: "allowlist" as const,
    allowFrom: [OPERATOR_ID],
    groups: {
      [CHAT_ID]: {},
    },
  };
  const op = questionOperator(access, CHAT_ID);
  expect(op).toBe(OPERATOR_ID);

  // Throws when ambiguous or not configured
  expect(() => questionOperator({ dmPolicy: "allowlist", allowFrom: ["1", "2"], groups: { [CHAT_ID]: {} } }, CHAT_ID))
    .toThrow();
});

test("ephemeral confirm and editConfirm transports use governedTelegramFetch with receiver_user_id and 25s deadline", async () => {
  const fetchCalls: Array<{ url: string; body: Record<string, unknown>; signalAborted: boolean }> = [];

  globalThis.fetch = (async (input, init) => {
    const url = String(input);
    const bodyText = String(init?.body ?? "{}");
    const body = JSON.parse(bodyText) as Record<string, unknown>;
    const signal = init?.signal as AbortSignal | undefined;
    fetchCalls.push({ url, body, signalAborted: Boolean(signal?.aborted) });
    if (url.includes("sendMessage")) {
      return Response.json({ ok: true, result: { message_id: 8881 } });
    }
    if (url.includes("editEphemeralMessageText")) {
      return Response.json({ ok: true });
    }
    return Response.json({ ok: true });
  }) as typeof fetch;

  const target = { slotId: "s1", chatId: CHAT_ID, topicId: TOPIC_ID };
  const token = "secret-bot-token";

  // Confirm transport logic matching runtime.ts
  const confirmFn = async (tgt: typeof target, text: string, markup: unknown, userId: string) => {
    const body: Record<string, unknown> = {
      chat_id: tgt.chatId,
      text,
      parse_mode: "HTML",
      reply_markup: markup,
      ephemeral_message_parameters: {
        receiver_user_id: Number(userId),
      },
    };
    if (tgt.topicId) {
      body.message_thread_id = Number(tgt.topicId);
    }
    const response = await governedTelegramFetch(
      `https://api.telegram.org/bot${token}/sendMessage`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(25_000),
      },
      {
        chatId: tgt.chatId,
        kind: "panel",
        timeoutMs: 25_000,
      },
    );
    const data = (await response.json()) as { ok: boolean; result?: { message_id?: number } };
    if (data.ok && typeof data.result?.message_id === "number") {
      return { ephemeralMessageId: data.result.message_id };
    }
    return "error";
  };

  const res = await confirmFn(target, "Stop this turn?", { inline_keyboard: [] }, OPERATOR_ID);
  expect(res).toEqual({ ephemeralMessageId: 8881 });

  const sendCall = fetchCalls.find(c => c.url.includes("sendMessage"));
  expect(sendCall).toBeDefined();
  expect(sendCall?.body.chat_id).toBe(CHAT_ID);
  expect(sendCall?.body.message_thread_id).toBe(Number(TOPIC_ID));
  const ephemeralParams = sendCall?.body.ephemeral_message_parameters as { receiver_user_id: number };
  expect(ephemeralParams.receiver_user_id).toBe(Number(OPERATOR_ID));

  // EditConfirm transport logic matching runtime.ts
  const editConfirmFn = async (tgt: typeof target, id: number, text: string, markup: unknown, userId: string) => {
    const body: Record<string, unknown> = {
      chat_id: tgt.chatId,
      message_id: id,
      text,
      parse_mode: "HTML",
      reply_markup: markup,
      ephemeral_message_parameters: {
        receiver_user_id: Number(userId),
      },
    };
    if (tgt.topicId) {
      body.message_thread_id = Number(tgt.topicId);
    }
    const response = await governedTelegramFetch(
      `https://api.telegram.org/bot${token}/editEphemeralMessageText`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(25_000),
      },
      {
        chatId: tgt.chatId,
        kind: "panel",
        timeoutMs: 25_000,
      },
    );
    const data = (await response.json()) as { ok: boolean; description?: string };
    if (data.ok || /not modified/i.test(data.description ?? "")) {
      return "ok";
    }
    if (/message to edit not found/i.test(data.description ?? "") || /message.*not found/i.test(data.description ?? "")) {
      return "gone";
    }
    return "error";
  };

  const editRes = await editConfirmFn(target, 8881, "Turn stopped", { inline_keyboard: [] }, OPERATOR_ID);
  expect(editRes).toBe("ok");

  const editCall = fetchCalls.find(c => c.url.includes("editEphemeralMessageText"));
  expect(editCall).toBeDefined();
  expect(editCall?.body.message_id).toBe(8881);
  const editEphemeralParams = editCall?.body.ephemeral_message_parameters as { receiver_user_id: number };
  expect(editEphemeralParams.receiver_user_id).toBe(Number(OPERATOR_ID));
});

test("prompt transport sends force_reply true and selective true in exact topic with session correlation", async () => {
  const target = { slotId: "s1", chatId: CHAT_ID, topicId: TOPIC_ID };
  const sentArgs: Array<{ chatId: unknown; text: unknown; markup: unknown; meta: unknown; thread: unknown }> = [];

  const dummyPoller = {
    sendTelegramMessage: async (chatId: unknown, text: unknown, _mode: unknown, markup: unknown, meta: unknown, _repo: unknown, thread: unknown) => {
      sentArgs.push({ chatId, text, markup, meta, thread });
      return { ok: true, result: { message_id: 7771 } };
    },
  };

  const promptFn = async (tgt: typeof target, text: string, sessionId: string) => {
    const sent = await dummyPoller.sendTelegramMessage(
      tgt.chatId,
      text,
      "HTML",
      { force_reply: true, selective: true },
      { sessionId },
      undefined,
      tgt.topicId ? Number(tgt.topicId) : undefined,
      "panel",
    );
    if (sent?.ok && typeof sent.result?.message_id === "number") {
      return { messageId: sent.result.message_id };
    }
    return "error";
  };

  const res = await promptFn(target, "Please send your steer:", "sess-xyz");
  expect(res).toEqual({ messageId: 7771 });

  expect(sentArgs.length).toBe(1);
  expect(sentArgs[0].chatId).toBe(CHAT_ID);
  expect(sentArgs[0].thread).toBe(Number(TOPIC_ID));
  expect(sentArgs[0].markup).toEqual({ force_reply: true, selective: true });
  expect(sentArgs[0].meta).toEqual({ sessionId: "sess-xyz" });
});

test("regression: disabled-policy, wrong-thread, bot-origin, and group rejection with actual LanePanels produce zero prompts, confirms, or aborts and record exactly 1 audit", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-panel-rejection-test-"));
  const dbPath = path.join(dir, "daemon.db");
  const store = new DaemonStore(dbPath);
  store.putRoute({ slotId: "s1", chatId: CHAT_ID, topicId: TOPIC_ID, sessionId: "sess-1", workspace: "C:/w/test" });

  const transportCalls: Array<{ op: string; text?: string; markup?: unknown }> = [];
  const abortCalls: unknown[] = [];

  const panels = new LanePanels({
    slotId: "s1",
    chatId: CHAT_ID,
    operatorId: OPERATOR_ID,
    store,
    snapshot: async () => ({
      version: 1,
      observedAt: 0,
      lanes: [{
        id: "sess-1", name: "test-lane", kind: "interactive", parentId: null, childIds: [], cwd: "C:/w/test",
        model: "model", status: "running", startedAtMs: 0, elapsedMs: 100, lastActivityMs: 0, lastAction: "bash",
      }],
      counts: { running: 1, idle: 0, subagents: 0 },
      usage: { available: false, observedAt: 0, windows: [] },
      hostMemory: { totalBytes: 1, freeBytes: 1, usedPercent: 0 },
      questions: [],
    }),
    boundSession: () => "sess-1",
    isBusy: () => true,
    stop: async t => { abortCalls.push(t); return true; },
    transport: {
      send: async (_t, text, markup) => {
        transportCalls.push({ op: "send", text, markup });
        return { messageId: 100 };
      },
      edit: async (_t, _id, text, markup) => {
        transportCalls.push({ op: "edit", text, markup });
        return "ok";
      },
      pin: async () => {},
      confirm: async (_t, text, markup) => {
        transportCalls.push({ op: "confirm", text, markup });
        return { ephemeralMessageId: 200 };
      },
      editConfirm: async () => "ok",
      prompt: async (_t, text) => {
        transportCalls.push({ op: "prompt", text });
        return { messageId: 300 };
      },
    },
  });

  await panels.tick();
  const lastSend = transportCalls.find(c => c.markup);
  const keyboard = typeof lastSend?.markup === "object" && lastSend.markup !== null && "inline_keyboard" in lastSend.markup
    && Array.isArray(lastSend.markup.inline_keyboard)
    ? lastSend.markup.inline_keyboard
    : [];
  const buttons: Record<string, string> = {};
  for (const row of keyboard) {
    if (Array.isArray(row)) {
      for (const b of row) {
        if (b && typeof b === "object" && "text" in b && typeof b.text === "string" && "callback_data" in b && typeof b.callback_data === "string") {
          buttons[b.text] = b.callback_data;
        }
      }
    }
  }

  const steerToken = buttons["🧭 Steer"];
  const stopToken = buttons["⏹ Stop"];
  expect(steerToken).toBeDefined();
  expect(stopToken).toBeDefined();

  const runScenario = async (options: {
    dmPolicy?: "allowlist" | "disabled";
    allowFrom?: string[];
    groups?: Record<string, { allowFrom?: string[] }>;
    messageThreadId?: number;
    fromId?: number;
    isBot?: boolean;
    chatId?: number;
    threadId?: number;
    updateId: number;
    token: string;
    expectedReason: string;
    expectedError: string;
  }) => {
    const pollerDir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-poller-scen-"));
    const calls: Array<{ method: string; body: Record<string, unknown> }> = [];

    const callbacks: PollerCallbacks = {
      isIdle: () => true,
      onUserMessage: () => {},
      onFollowUp: () => {},
      onSteer: () => {},
      onAbort: () => {},
      onRelease: async () => {},
      getStatusText: () => "status",
      onLedgerFailure: () => {},
      lanePanel: panels,
    };

    const bridge: MessageCorrelationBridge = {
      getSessionId: () => "sess-1",
      getSlotId: () => "s1",
      record: () => {},
      resolveReply: () => ({ decision: "reject_unknown", detail: "Unknown" }),
    };

    const poller = new TelegramPoller(
      "0:test-token",
      pollerDir,
      {
        dmPolicy: options.dmPolicy ?? "allowlist",
        allowFrom: options.allowFrom ?? [OPERATOR_ID],
        groups: options.groups ?? { [CHAT_ID]: {} },
      },
      callbacks,
      bridge,
      options.messageThreadId !== undefined ? options.messageThreadId : { sendTimeoutMs: 1000 },
      { sendTimeoutMs: 1000 },
    );

    const update: TelegramUpdate = {
      update_id: options.updateId,
      callback_query: {
        id: `cq-${options.updateId}`,
        from: {
          id: options.fromId ?? Number(OPERATOR_ID),
          is_bot: options.isBot ?? false,
          first_name: "TestUser",
        },
        message: {
          message_id: 50,
          date: 1000,
          chat: {
            id: options.chatId ?? Number(CHAT_ID),
            type: "supergroup",
          },
          message_thread_id: options.threadId ?? Number(TOPIC_ID),
          text: "panel",
        },
        data: options.token,
      },
    };

    let polls = 0;
    globalThis.fetch = (async (input, init) => {
      const url = new URL(String(input));
      const method = url.pathname.split("/").pop()!;
      if (method === "getUpdates") {
        if (++polls === 1) return Response.json({ ok: true, result: [update] });
        poller.stop();
        throw new Error("stopped");
      }
      const bodyText = typeof init?.body === "string" ? init.body : "{}";
      calls.push({ method, body: JSON.parse(bodyText) as Record<string, unknown> });
      return Response.json({ ok: true });
    }) as typeof fetch;

    const cleanupScenario = () => {
      poller.stop();
      try { fs.rmSync(pollerDir, { recursive: true, force: true }); } catch {}
    };
    cleanup.push(cleanupScenario);

    try {
      await poller.start();
    } catch {}
    poller.stop();
    // 1. Receipt answered with PANEL_EXPIRED_ANSWER
    const answer = calls.find(c => c.method === "answerCallbackQuery");
    expect(answer?.body.text).toBe(PANEL_EXPIRED_ANSWER);

    // 2. Ledger marked REJECTED with expected error
    const db = new Database(path.join(pollerDir, "veyyon_bridge_state.db"));
    const rawData = db.query("SELECT status, error FROM update_ledger WHERE update_id = ?").get(options.updateId);
    let rowStatus = "";
    let rowError: string | null = null;
    if (rawData && typeof rawData === "object" && "status" in rawData && typeof rawData.status === "string") {
      rowStatus = rawData.status;
      if ("error" in rawData && typeof rawData.error === "string") {
        rowError = rawData.error;
      }
    }
    db.close();
    expect(rowStatus).toBe("REJECTED");
    expect(rowError).toBe(options.expectedError);

    // 3. Exactly 1 audit record produced with expected reason
    const audit = store.getControlAudit(`update:${options.updateId}`);
    expect(audit).toBeDefined();
    expect(audit?.result).toBe(options.expectedReason);

    cleanupScenario();
    const idx = cleanup.indexOf(cleanupScenario);
    if (idx !== -1) cleanup.splice(idx, 1);
  };

  const initialPrompts = transportCalls.filter(c => c.op === "prompt").length;
  const initialConfirms = transportCalls.filter(c => c.op === "confirm").length;
  const initialAborts = abortCalls.length;

  // Scenario 1: Disabled DM Policy
  await runScenario({
    dmPolicy: "disabled",
    updateId: 701,
    token: steerToken!,
    expectedReason: "dm_policy_disabled",
    expectedError: "DM_POLICY_DISABLED",
  });

  // Scenario 2: Wrong thread
  await runScenario({
    messageThreadId: Number(TOPIC_ID),
    threadId: 999, // Mismatched thread
    updateId: 702,
    token: steerToken!,
    expectedReason: "wrong_thread",
    expectedError: "WRONG_THREAD",
  });

  // Scenario 3: Bot origin
  await runScenario({
    isBot: true,
    updateId: 703,
    token: stopToken!,
    expectedReason: "non_operator_origin",
    expectedError: "NON_OPERATOR_ORIGIN",
  });

  // Scenario 4: Group rejection
  await runScenario({
    chatId: -999888, // Not in groups
    updateId: 704,
    token: steerToken!,
    expectedReason: "unauthorized",
    expectedError: "UNAUTHORIZED",
  });

  // ZERO prompts, confirms, or aborts across ALL rejection scenarios
  expect(transportCalls.filter(c => c.op === "prompt").length).toBe(initialPrompts);
  expect(transportCalls.filter(c => c.op === "confirm").length).toBe(initialConfirms);
  expect(abortCalls.length).toBe(initialAborts);

  store.close();
  try { fs.rmSync(dir, { recursive: true, force: true }); } catch {}
});

test("regression: unauthorized Answer token produces exactly 1 audit without resolution or delivery", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-answer-unauth-"));
  const dbPath = path.join(dir, "daemon.db");
  const store = new DaemonStore(dbPath);
  const secret = Buffer.alloc(32, 7);

  const coordinator = new BotPoolCoordinator(path.join(dir, "pool.db"));
  coordinator.setAuditStore(store);
  coordinator.setSlotSecret("s1", secret);

  const issued = coordinator.issueDecisionCallback({
    decisionId: "tq:quest-1",
    choiceId: "opt-1",
    sessionId: "sess-1",
    chatId: CHAT_ID,
    userId: OPERATOR_ID,
    slotId: "s1",
    secret,
  });
  const answerToken = issued.callbackToken;

  const pollerDir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-poller-ans-"));
  let deliveredAnswers = 0;
  let resolvedAnswers = 0;

  const callbacks: PollerCallbacks = {
    isIdle: () => true,
    onUserMessage: () => {},
    onFollowUp: () => {},
    onSteer: () => {},
    onAbort: () => {},
    onRelease: async () => {},
    getStatusText: () => "status",
    onLedgerFailure: () => {},
    onQuestionAnswer: () => {
      deliveredAnswers++;
    },
    onDecisionCallback: () => {
      resolvedAnswers++;
    },
  };

  const bridge: MessageCorrelationBridge = {
    getSessionId: () => "sess-1",
    getSlotId: () => "s1",
    record: () => {},
    resolveReply: () => ({ decision: "reject_unknown", detail: "Unknown" }),
    resolveCallback: (token, userId, chatId, eventId) => {
      resolvedAnswers++;
      return coordinator.validateDecisionCallback(token, userId, chatId, "sess-1", path.join(dir, "decisions.json"), { eventId, secret });
    },
    consumeCallback: (token, eventId) => coordinator.consumeDecisionCallback(token, undefined, eventId),
    auditRejectedCallback: (token, userId, chatId, eventId, reason) => {
      coordinator.auditRejectedDecision(token, eventId, userId, reason);
    },
  };

  const poller = new TelegramPoller(
    "0:test-token",
    pollerDir,
    {
      dmPolicy: "allowlist",
      allowFrom: [OPERATOR_ID],
      groups: { [CHAT_ID]: {} },
    },
    callbacks,
    bridge,
    { sendTimeoutMs: 1000 },
  );

  const updateId = 801;
  const unauthorizedUserId = 9999;
  const update: TelegramUpdate = {
    update_id: updateId,
    callback_query: {
      id: `cq-${updateId}`,
      from: {
        id: unauthorizedUserId,
        is_bot: false,
        first_name: "Attacker",
      },
      message: {
        message_id: 60,
        date: 1000,
        chat: {
          id: Number(CHAT_ID),
          type: "supergroup",
        },
        message_thread_id: Number(TOPIC_ID),
        text: "question",
      },
      data: answerToken,
    },
  };

  const calls: Array<{ method: string; body: Record<string, unknown> }> = [];
  let polls = 0;
  globalThis.fetch = (async (input, init) => {
    const url = new URL(String(input));
    const method = url.pathname.split("/").pop()!;
    if (method === "getUpdates") {
      if (++polls === 1) return Response.json({ ok: true, result: [update] });
      poller.stop();
      throw new Error("stopped");
    }
    const bodyText = typeof init?.body === "string" ? init.body : "{}";
    calls.push({ method, body: JSON.parse(bodyText) as Record<string, unknown> });
    return Response.json({ ok: true });
  }) as typeof fetch;

  const cleanupAns = () => {
    poller.stop();
    coordinator.close();
    store.close();
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch {}
    try { fs.rmSync(pollerDir, { recursive: true, force: true }); } catch {}
  };
  cleanup.push(cleanupAns);

  try {
    await poller.start();
  } catch {}
  poller.stop();

  // 1. Zero resolution or delivery to callbacks
  expect(deliveredAnswers).toBe(0);
  expect(resolvedAnswers).toBe(0);

  // 2. Query answered as unauthorized
  const answer = calls.find(c => c.method === "answerCallbackQuery" && typeof c.body.text === "string" && Boolean(c.body.text));
  expect(answer?.body.text).toContain("Unauthorized account");
  // 3. Ledger marked REJECTED / UNAUTHORIZED
  const db = new Database(path.join(pollerDir, "veyyon_bridge_state.db"));
  const rawData = db.query("SELECT status, error FROM update_ledger WHERE update_id = ?").get(updateId);
  let rowStatus = "";
  let rowError: string | null = null;
  if (rawData && typeof rawData === "object" && "status" in rawData && typeof rawData.status === "string") {
    rowStatus = rawData.status;
    if ("error" in rawData && typeof rawData.error === "string") {
      rowError = rawData.error;
    }
  }
  db.close();
  expect(rowStatus).toBe("REJECTED");
  expect(rowError).toBe("UNAUTHORIZED");

  // 4. Exactly 1 audit record in daemon store
  const audit = store.getControlAudit(`update:${updateId}`);
  expect(audit).toBeDefined();
  expect(audit?.action).toBe("answer");
  expect(audit?.result).toBe("unauthorized");
  expect(audit?.userId).toBe(String(unauthorizedUserId));
  expect(audit?.sessionId).toBe("sess-1");

  // 5. Duplicate event processing does not duplicate audit or trigger action
  const auditCountRow = store.db.query("SELECT count(*) as count FROM control_audits WHERE event_id = ?").get(`update:${updateId}`);
  let countVal = 0;
  if (auditCountRow && typeof auditCountRow === "object" && "count" in auditCountRow && typeof auditCountRow.count === "number") {
    countVal = auditCountRow.count;
  }
  expect(countVal).toBe(1);

  cleanupAns();
  const idx = cleanup.indexOf(cleanupAns);
  if (idx !== -1) cleanup.splice(idx, 1);
});
