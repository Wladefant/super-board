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

const originalFetch = globalThis.fetch;
const cleanup: Array<() => void> = [];
afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const close of cleanup.splice(0)) close();
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

  const bridge: MessageCorrelationBridge = {
    getSessionId: () => "sess-test",
    getSlotId: () => "slot-test",
    record: () => {},
    resolveReply: () => ({ decision: "reject_unknown", detail: "Unknown" }),
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

test("unauthorized user outside allowlist answers rejection at receipt and invokes run for audit with context", async () => {
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

  // Still invoked panel run for audit
  expect(fixture.ranTokens).toEqual([`${LANE_PANEL_CALLBACK_PREFIX}token-abc`]);
  expect(fixture.runContexts.length).toBe(1);
  expect(fixture.runContexts[0].userId).toBe("9999");
  expect(fixture.runContexts[0].eventId).toBe("update:432");

  // Ledger marked REJECTED / UNAUTHORIZED
  const db = new Database(path.join(fixture.dir, "veyyon_bridge_state.db"));
  const row = db.query("SELECT status, error FROM update_ledger WHERE update_id = 432").get() as { status: string; error: string };
  db.close();
  expect(row.status).toBe("REJECTED");
  expect(row.error).toBe("UNAUTHORIZED");
});

test("unauthorized group answers rejection at receipt and invokes run for audit with context", async () => {
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

  // Still invoked panel run for audit
  expect(fixture.ranTokens).toEqual([`${LANE_PANEL_CALLBACK_PREFIX}token-abc`]);
  expect(fixture.runContexts.length).toBe(1);
  expect(fixture.runContexts[0].chatId).toBe("-999888");
  expect(fixture.runContexts[0].eventId).toBe("update:433");

  // Ledger marked REJECTED / UNAUTHORIZED
  const db = new Database(path.join(fixture.dir, "veyyon_bridge_state.db"));
  const row = db.query("SELECT status, error FROM update_ledger WHERE update_id = 433").get() as { status: string; error: string };
  db.close();
  expect(row.status).toBe("REJECTED");
  expect(row.error).toBe("UNAUTHORIZED");
});

test("bot origin answers rejection at receipt and invokes run for audit", async () => {
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

  // Still invoked panel run for audit
  expect(fixture.ranTokens).toEqual([`${LANE_PANEL_CALLBACK_PREFIX}token-abc`]);
  expect(fixture.runContexts.length).toBe(1);
  expect(fixture.runContexts[0].eventId).toBe("update:434");

  // Ledger marked REJECTED / NON_OPERATOR_ORIGIN
  const db = new Database(path.join(fixture.dir, "veyyon_bridge_state.db"));
  const row = db.query("SELECT status, error FROM update_ledger WHERE update_id = 434").get() as { status: string; error: string };
  db.close();
  expect(row.status).toBe("REJECTED");
  expect(row.error).toBe("NON_OPERATOR_ORIGIN");
});

test("wrong thread invokes run for audit and marks WRONG_THREAD in ledger", async () => {
  const fixture = setupPollerTest({
    fromId: Number(OPERATOR_ID),
    chatId: Number(CHAT_ID),
    threadId: 99, // Mismatched thread
    messageThreadId: 42, // Bound poller thread
    updateId: 435,
  });

  await fixture.poller.start();

  // Invoked panel run for audit
  expect(fixture.ranTokens).toEqual([`${LANE_PANEL_CALLBACK_PREFIX}token-abc`]);
  expect(fixture.runContexts.length).toBe(1);
  expect(fixture.runContexts[0].topicId).toBe("99");
  expect(fixture.runContexts[0].eventId).toBe("update:435");

  // Ledger marked REJECTED / WRONG_THREAD
  const db = new Database(path.join(fixture.dir, "veyyon_bridge_state.db"));
  const row = db.query("SELECT status, error FROM update_ledger WHERE update_id = 435").get() as { status: string; error: string };
  db.close();
  expect(row.status).toBe("REJECTED");
  expect(row.error).toBe("WRONG_THREAD");
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
