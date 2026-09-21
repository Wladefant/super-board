/**
 * daemon-forum.test.ts — Supergroup forum-topics mode.
 *
 * A forum slot serves one supergroup and binds a session to a topic instead of to a
 * chat. Everything else is the machinery DM mode uses, so what is exercised here is
 * the part that differs: which chats the poller admits, which topic an admitted
 * message belongs to, the topic lifecycle on the Bot API, and the manifest and
 * daemon wiring that turns a slot into a forum slot.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { resolveDaemonSlots, type DaemonSlot } from "../daemon/config";
import {
  DefaultTelegramForumClient,
  ForumManager,
  formatTopicName,
  type ForumApiClient,
  type ForumTopic,
} from "../daemon/forum";
import { TelegramDaemon } from "../daemon/runtime";
import {
  type DaemonSessionSummary,
  type DeliveryMode,
  type TerminalSessionControl,
} from "../daemon/session-control";
import { DaemonStore } from "../daemon/store";
import { BotPoolCoordinator } from "../extension/coordinator";
import { TelegramPoller, type PollerCallbacks } from "../extension/poller";
import type { TelegramUpdate } from "../extension/types";

export class FakeForumApiClient implements ForumApiClient {
  public topics: Map<number, { name: string; closed: boolean }> = new Map();
  public sentMessages: Array<{ chatId: string | number; text: string; messageThreadId?: number }> = [];
  private threadCounter = 100;

  public async createForumTopic(chatId: string | number, name: string): Promise<ForumTopic> {
    const threadId = ++this.threadCounter;
    this.topics.set(threadId, { name, closed: false });
    return { message_thread_id: threadId, name };
  }

  public async closeForumTopic(chatId: string | number, messageThreadId: number): Promise<boolean> {
    const topic = this.topics.get(messageThreadId);
    if (!topic) return false;
    topic.closed = true;
    return true;
  }

  public async reopenForumTopic(chatId: string | number, messageThreadId: number): Promise<boolean> {
    const topic = this.topics.get(messageThreadId);
    if (!topic) return false;
    topic.closed = false;
    return true;
  }

  public async sendMessage(
    chatId: string | number,
    text: string,
    options?: { message_thread_id?: number },
  ): Promise<{ ok: boolean; result: { message_id: number } }> {
    this.sentMessages.push({ chatId, text, messageThreadId: options?.message_thread_id });
    return { ok: true, result: { message_id: this.sentMessages.length } };
  }
}

interface FakeControl {
  control: TerminalSessionControl;
  sessions: DaemonSessionSummary[];
  diskSessions: DaemonSessionSummary[];
  delivered: Array<{ sessionId: string; text: string; mode: DeliveryMode }>;
  created: Array<{ workspace: string; title: string }>;
  loaded: string[];
  busy: Set<string>;
}

function fakeControl(initialSessions: DaemonSessionSummary[] = []): FakeControl {
  const sessions = [...initialSessions];
  const diskSessions: DaemonSessionSummary[] = [];
  const delivered: Array<{ sessionId: string; text: string; mode: DeliveryMode }> = [];
  const created: Array<{ workspace: string; title: string }> = [];
  const loaded: string[] = [];
  const busy = new Set<string>();
  let counter = 0;

  const control = {
    endpoint: "tcp:127.0.0.1:7699",
    isBusy: (sessionId: string) => busy.has(sessionId),
    listSessions: async () => sessions,
    discoverDiskSessions: () => diskSessions,
    findSession: async (workspace: string) =>
      sessions.find(s => path.resolve(s.cwd).toLowerCase() === path.resolve(workspace).toLowerCase()) ?? null,
    createSession: async (workspace: string, title: string) => {
      created.push({ workspace, title });
      const id = `sess-forum-${++counter}`;
      sessions.push({ id, cwd: workspace, workspace, title, status: "Idle", modifiedAtMs: Date.now() });
      return id;
    },
    ensureSession: async (workspace: string, title: string) => {
      const existing = sessions.find(s => path.resolve(s.cwd).toLowerCase() === path.resolve(workspace).toLowerCase());
      if (existing) return existing.id;
      return control.createSession(workspace, title);
    },
    deliver: async (sessionId: string, text: string, mode: DeliveryMode = "auto") => {
      delivered.push({ sessionId, text, mode });
      if (mode === "steer") return "steered" as const;
      if (mode === "followUp") return "queued" as const;
      return busy.has(sessionId) ? ("steered" as const) : ("started" as const);
    },
    abort: async () => true,
    loadTranscript: async (sessionId: string) => {
      loaded.push(sessionId);
    },
    usage: async () => null,
    close: () => {},
  };

  return { control: control as unknown as TerminalSessionControl, sessions, diskSessions, delivered, created, loaded, busy };
}

const FORUM_CHAT_ID = "-1009876543210";
const OPERATOR_ID = "1247617658";

describe("ForumManager topic lifecycle", () => {
  let tempDir: string;
  let store: DaemonStore;
  let client: FakeForumApiClient;
  let fake: FakeControl;
  let slot: DaemonSlot;
  let manager: ForumManager;

  beforeEach(() => {
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "veyyon-forum-test-"));
    store = new DaemonStore(path.join(tempDir, "daemon.db"));
    client = new FakeForumApiClient();
    fake = fakeControl();
    slot = {
      slotId: "slot-forum",
      botId: "999888",
      stateDir: path.join(tempDir, "slot-forum"),
      fingerprint: "fp-forum",
      preferredProjects: [],
      enabled: true,
      workspace: "C:/dev/forum-proj",
      mode: "forum",
      forumChatId: FORUM_CHAT_ID,
    };
    manager = new ForumManager({
      slot,
      token: "fake-bot-token",
      forumChatId: FORUM_CHAT_ID,
      store,
      control: fake.control,
      client,
    });
  });

  afterEach(() => {
    store.close();
    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  test("formatTopicName uses folder basename and caps length", () => {
    expect(formatTopicName("session-1234567890", "C:/dev/my-project", "Feature X")).toBe("my-project");
    expect(formatTopicName("session-abcdef", "C:/dev/super-board", null)).toBe("super-board");
    expect(formatTopicName("session-1", "C:/dev/proj", null, 1)).toBe("proj");
    expect(formatTopicName("session-2", "C:/dev/proj", null, 2)).toBe("proj (2)");
    expect(formatTopicName("session-3", "C:/dev/proj", null, 3)).toBe("proj (3)");
  });

  test("ensureTopic creates a topic, records the route, and announces itself in the thread", async () => {
    const threadId = await manager.ensureTopic("sess-1", "C:/dev/proj", "Testing Topic");
    expect(threadId).toBeGreaterThan(0);
    expect(client.topics.get(threadId)?.name).toBe("proj");

    const route = store.getRoute("slot-forum", FORUM_CHAT_ID, String(threadId));
    expect(route?.sessionId).toBe("sess-1");
    expect(route?.workspace).toBe("C:/dev/proj");

    const welcome = client.sentMessages.find(m => m.messageThreadId === threadId);
    expect(welcome?.text).toContain("Veyyon session attached");
    expect(welcome?.text).toContain("sess-1");
  });

  test("ensureTopic reuses the topic a session already owns", async () => {
    const first = await manager.ensureTopic("sess-1", "C:/dev/proj");
    expect(await manager.ensureTopic("sess-1", "C:/dev/proj")).toBe(first);
    expect(client.topics.size).toBe(1);
  });

  test("different sessions in the same folder retain separate topic identities", async () => {
    const firstThread = await manager.ensureTopic("sess-1", "C:/dev/proj");
    const secondThread = await manager.ensureTopic("sess-2", "C:/dev/proj");
    expect(secondThread).not.toBe(firstThread);
    expect(store.getRoute("slot-forum", FORUM_CHAT_ID, String(firstThread))?.sessionId).toBe("sess-1");
    expect(store.getRoute("slot-forum", FORUM_CHAT_ID, String(secondThread))?.sessionId).toBe("sess-2");
    expect(client.topics.size).toBe(2);
  });

  test("ensureTopic does not reuse another slot's topic for the same session", async () => {
    store.putRoute({
      slotId: "other-slot",
      chatId: FORUM_CHAT_ID,
      topicId: "77",
      sessionId: "sess-1",
      workspace: "C:/dev/proj",
    });
    expect(await manager.ensureTopic("sess-1", "C:/dev/proj")).not.toBe(77);
  });

  test("closeTopic closes on Telegram and reports a Bot API failure rather than throwing", async () => {
    const threadId = await manager.ensureTopic("sess-1", "C:/dev/proj");
    expect(await manager.closeTopic(threadId)).toBe(true);
    expect(client.topics.get(threadId)?.closed).toBe(true);

    const logged: string[] = [];
    const failing = new ForumManager({
      slot,
      token: "fake-bot-token",
      forumChatId: FORUM_CHAT_ID,
      store,
      control: fake.control,
      log: message => logged.push(message),
      client: {
        ...client,
        closeForumTopic: async () => {
          throw new Error("TOPIC_NOT_MODIFIED");
        },
      } as unknown as ForumApiClient,
    });
    expect(await failing.closeTopic(threadId)).toBe(false);
    expect(logged.join("\n")).toContain("TOPIC_NOT_MODIFIED");
  });

  test("listTopicsText marks the current topic and reports each session's turn state", async () => {
    const t1 = await manager.ensureTopic("sess-1", "C:/dev/proj1");
    const t2 = await manager.ensureTopic("sess-2", "C:/dev/proj2");
    fake.busy.add("sess-2");

    const listing = manager.listTopicsText(String(t1));
    expect(listing).toContain("Session topics (2)");
    expect(listing).toContain(`➡️ <b>#${t1}</b>: <code>sess-1</code> — <i>C:/dev/proj1</i> (idle)`);
    expect(listing).toContain(`• <b>#${t2}</b>: <code>sess-2</code> — <i>C:/dev/proj2</i> (running)`);
  });

  test("listTopicsText points at /new when the forum has no bound topic", () => {
    expect(manager.listTopicsText("")).toContain("/new");
  });

  test("a live session's topic is never closed on its own", async () => {
    const threadId = await manager.ensureTopic("sess-1", "C:/dev/proj1");
    fake.sessions.length = 0;
    // No reconcile loop exists: absence from a host listing is a turn-status artifact,
    // and closing on it took a live operator's topic away seconds after it opened.
    await Promise.resolve();
    expect(client.topics.get(threadId)?.closed).toBe(false);
    expect(store.getRoute("slot-forum", FORUM_CHAT_ID, String(threadId))).not.toBeNull();
  });
  test("auto-attach: new live session -> creates topic, route, sends welcome, and binds", async () => {
    fake.sessions.push({
      id: "live-sess-1",
      cwd: "C:/dev/my-project",
      workspace: "C:/dev/my-project",
      title: "Feature X",
      status: "Idle",
      modifiedAtMs: Date.now(),
    });

    const res = await manager.reconcileAutoAttach();
    expect(res.created).toBe(1);
    expect(res.rebound).toBe(0);
    expect(res.skipped).toBe(0);
    expect(res.errors).toBe(0);

    expect(client.topics.size).toBe(1);
    const [threadId, topic] = [...client.topics.entries()][0];
    expect(topic.name).toBe("my-project");

    const route = store.getRoute("slot-forum", FORUM_CHAT_ID, String(threadId));
    expect(route?.sessionId).toBe("live-sess-1");
    expect(route?.workspace).toBe("C:/dev/my-project");

    expect(fake.loaded).toContain("live-sess-1");
    const welcome = client.sentMessages.find(m => m.messageThreadId === threadId);
    expect(welcome?.text).toContain("Veyyon session attached");
    expect(welcome?.text).toContain("live-sess-1");
  });

  test("missing owner endpoint never authorizes reassigning its topic to another session", async () => {
    store.putRoute({
      slotId: "slot-forum", chatId: FORUM_CHAT_ID, topicId: "77",
      sessionId: "temporarily-unavailable-owner", workspace: "C:/dev/my-project",
    });
    client.topics.set(77, { name: "my-project", closed: false });
    fake.sessions.push({
      id: "live-sess-2", cwd: "C:/dev/my-project", workspace: "C:/dev/my-project",
      title: "New Session", status: "Idle", modifiedAtMs: Date.now(),
    });
    const res = await manager.reconcileAutoAttach();
    expect(res.created).toBe(1);
    expect(res.rebound).toBe(0);
    expect(client.topics.size).toBe(2);
    expect(store.getRoute("slot-forum", FORUM_CHAT_ID, "77")?.sessionId).toBe("temporarily-unavailable-owner");
    expect(client.sentMessages.some(m => m.messageThreadId === 77)).toBe(false);
    expect(fake.loaded).toContain("live-sess-2");
  });

  test("auto-attach: two live sessions in same folder -> ordinal topic for second", async () => {
    fake.sessions.push(
      {
        id: "live-1",
        cwd: "C:/dev/alpha",
        workspace: "C:/dev/alpha",
        title: null,
        status: "Idle",
        modifiedAtMs: 1,
      },
      {
        id: "live-2",
        cwd: "C:/dev/alpha",
        workspace: "C:/dev/alpha",
        title: null,
        status: "Idle",
        modifiedAtMs: 2,
      },
    );

    const res = await manager.reconcileAutoAttach();
    expect(res.created).toBe(2);
    expect(client.topics.size).toBe(2);

    const topicNames = [...client.topics.values()].map(t => t.name).sort();
    expect(topicNames).toEqual(["alpha", "alpha (2)"]);

    expect(fake.loaded).toContain("live-1");
    expect(fake.loaded).toContain("live-2");
  });

  test("auto-attach: interval idempotence -> subsequent runs create nothing new", async () => {
    fake.sessions.push({
      id: "live-idem",
      cwd: "C:/dev/idem",
      workspace: "C:/dev/idem",
      title: null,
      status: "Idle",
      modifiedAtMs: Date.now(),
    });

    const res1 = await manager.reconcileAutoAttach();
    expect(res1.created).toBe(1);

    const topicCount = client.topics.size;
    const messageCount = client.sentMessages.length;

    const res2 = await manager.reconcileAutoAttach();
    expect(res2.created).toBe(0);
    expect(res2.rebound).toBe(0);
    expect(res2.skipped).toBe(1);
    expect(res2.errors).toBe(0);

    const res3 = await manager.reconcileAutoAttach();
    expect(res3.created).toBe(0);
    expect(res3.skipped).toBe(1);

    expect(client.topics.size).toBe(topicCount);
    expect(client.sentMessages.length).toBe(messageCount);
  });

  test("auto-attach: disk-only session -> nothing", async () => {
    fake.sessions.length = 0; // Wire sessions empty
    fake.diskSessions.push({
      id: "disk-only-sess",
      cwd: "C:/dev/disk-project",
      workspace: "C:/dev/disk-project",
      title: null,
      status: "Idle",
      modifiedAtMs: Date.now(),
    });

    const res = await manager.reconcileAutoAttach();
    expect(res.actions).toHaveLength(0);
    expect(res.created).toBe(0);
    expect(res.rebound).toBe(0);
    expect(client.topics.size).toBe(0);
    expect(store.listRoutes("slot-forum")).toHaveLength(0);
  });

  test("auto-attach: Telegram error -> retries next tick with backoff without stalling other sessions", async () => {
    fake.sessions.push(
      {
        id: "sess-fail",
        cwd: "C:/dev/fail",
        workspace: "C:/dev/fail",
        title: null,
        status: "Idle",
        modifiedAtMs: 1,
      },
      {
        id: "sess-ok",
        cwd: "C:/dev/ok",
        workspace: "C:/dev/ok",
        title: null,
        status: "Idle",
        modifiedAtMs: 2,
      },
    );

    const origCreate = client.createForumTopic.bind(client);
    client.createForumTopic = async (chatId, name) => {
      if (name.includes("fail")) {
        throw new Error("Telegram API createForumTopic failed: 429 Too Many Requests");
      }
      return origCreate(chatId, name);
    };

    const res = await manager.reconcileAutoAttach();
    expect(res.created).toBe(1);
    expect(res.errors).toBe(1);
    expect(client.topics.size).toBe(1);
    const okTopic = [...client.topics.values()].find(t => t.name === "ok");
    expect(okTopic).toBeDefined();
    expect(fake.loaded).toContain("sess-ok");

    // Immediate next tick: fail workspace is in backoff, does not throw, skips cleanly
    const res2 = await manager.reconcileAutoAttach();
    expect(res2.created).toBe(0);
    expect(res2.skipped).toBe(1); // sess-ok is already bound

    // Clear backoff & fix error -> retries and succeeds
    manager.clearWorkspaceBackoff("C:/dev/fail");
    client.createForumTopic = origCreate;

    const res3 = await manager.reconcileAutoAttach();
    expect(res3.created).toBe(1);
    expect(client.topics.size).toBe(2);
    expect(fake.loaded).toContain("sess-fail");
  });

  test("auto-attach: dryRun computes actions without calling Telegram or mutating store", async () => {
    fake.sessions.push({
      id: "live-dry",
      cwd: "C:/dev/dry",
      workspace: "C:/dev/dry",
      title: null,
      status: "Idle",
      modifiedAtMs: Date.now(),
    });

    const res = await manager.reconcileAutoAttach(undefined, { dryRun: true });
    expect(res.created).toBe(1);
    expect(res.actions).toHaveLength(1);
    expect(res.actions[0].action).toBe("create");
    expect(res.actions[0].folder).toBe("dry");

    expect(client.topics.size).toBe(0);
    expect(store.listRoutes("slot-forum")).toHaveLength(0);
  });
});

describe("poller authorization for a forum supergroup", () => {
  const cleanup: Array<() => void> = [];
  const originalFetch = globalThis.fetch;

  afterEach(() => {
    globalThis.fetch = originalFetch;
    for (const close of cleanup.splice(0)) close();
  });

  function groupPoller(access: Record<string, unknown>, options: Record<string, unknown> = {}) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "veyyon-forum-auth-"));
    const inbound: Array<{ text: string; threadId: number | undefined }> = [];
    const sent: Array<{ chatId: unknown; text: string; threadId: unknown }> = [];
    const callbacks: PollerCallbacks = {
      isIdle: () => true,
      onUserMessage: text => inbound.push({ text, threadId: poller.getActiveThreadId() }),
      onFollowUp: () => {},
      onSteer: () => {},
      onAbort: () => {},
      onRelease: async () => {},
      getStatusText: () => "test",
      onTelegramTurnStart: () => {},
      onLedgerFailure: () => {},
    };
    const poller = new TelegramPoller(
      "0:test-only",
      dir,
      { dmPolicy: "allowlist", allowFrom: [OPERATOR_ID], ...access } as never,
      callbacks,
      undefined,
      options as never,
    );
    globalThis.fetch = (async (input: string | URL | Request, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body ?? "{}"));
      if (String(input).endsWith("/sendMessage")) {
        sent.push({ chatId: body.chat_id, text: String(body.text ?? ""), threadId: body.message_thread_id });
      }
      return Response.json({ ok: true, result: { message_id: sent.length, chat: { id: 1 }, date: 0 } });
    }) as typeof fetch;
    cleanup.push(() => {
      poller.stop();
      fs.rmSync(dir, { recursive: true, force: true });
    });
    const ledgerRow = (updateId: number) => {
      const db = new Database(path.join(dir, "veyyon_bridge_state.db"), { readonly: true });
      try {
        return db
          .query("SELECT status, error, message_thread_id, correlated_session_id FROM update_ledger WHERE update_id = ?")
          .get(updateId) as { status: string; error: string | null; message_thread_id: number | null; correlated_session_id: string | null } | null;
      } finally {
        db.close();
      }
    };
    return { poller, inbound, sent, ledgerRow };
  }

  function groupMessage(updateId: number, text: string, threadId?: number): TelegramUpdate {
    return {
      update_id: updateId,
      message: {
        message_id: updateId,
        date: 0,
        text,
        chat: { id: Number(FORUM_CHAT_ID), type: "supergroup" },
        from: { id: Number(OPERATOR_ID), is_bot: false, first_name: "Operator" },
        ...(threadId === undefined ? {} : { message_thread_id: threadId }),
      },
    } as TelegramUpdate;
  }

  test("an operator's supergroup message is admitted, routed by topic, and recorded", async () => {
    const f = groupPoller({}, { forumChatId: FORUM_CHAT_ID });
    f.poller.ingestUpdates([groupMessage(1, "Hello", 9)]);
    await f.poller.redrivePendingUpdates();

    expect(f.inbound).toHaveLength(1);
    expect(f.inbound[0].text).toContain("Hello");
    expect(f.inbound[0].threadId).toBe(9);
    // The topic a message came from is the route key, so it has to survive a restart
    // in the ledger rather than only in the poller's in-flight state.
    expect(f.ledgerRow(1)).toMatchObject({ status: "COMPLETED", message_thread_id: 9 });
  });

  test("an access.json groups entry admits a supergroup that is not the slot's forum", async () => {
    const f = groupPoller({ groups: { [FORUM_CHAT_ID]: {} } });
    f.poller.ingestUpdates([groupMessage(1, "Hello", 9)]);
    await f.poller.redrivePendingUpdates();
    expect(f.inbound).toHaveLength(1);
    expect(f.ledgerRow(1)?.status).toBe("COMPLETED");
  });

  test("a per-group allowFrom narrows the channel allowlist for that chat alone", async () => {
    const f = groupPoller({ groups: { [FORUM_CHAT_ID]: { allowFrom: ["999"] } } });
    f.poller.ingestUpdates([groupMessage(1, "Hello", 9)]);
    await f.poller.redrivePendingUpdates();
    expect(f.inbound).toEqual([]);
    expect(f.ledgerRow(1)).toMatchObject({ status: "REJECTED", error: "UNAUTHORIZED" });
  });

  test("a per-group allowFrom cannot admit an account the channel allowlist omits", async () => {
    const f = groupPoller({ allowFrom: ["555"], groups: { [FORUM_CHAT_ID]: { allowFrom: [OPERATOR_ID] } } });
    f.poller.ingestUpdates([groupMessage(1, "Hello", 9)]);
    await f.poller.redrivePendingUpdates();
    // A group entry restricts where an allowlisted account may speak; it is never a
    // second door into the channel.
    expect(f.inbound).toEqual([]);
    expect(f.ledgerRow(1)).toMatchObject({ status: "REJECTED", error: "UNAUTHORIZED" });
  });

  test("an unserved group is rejected and the operator is told why, at most once", async () => {
    const f = groupPoller({});
    f.poller.ingestUpdates([groupMessage(1, "Hello", 9), groupMessage(2, "Anyone there?", 9)]);
    await f.poller.redrivePendingUpdates();

    expect(f.inbound).toEqual([]);
    expect(f.ledgerRow(1)).toMatchObject({ status: "REJECTED", error: "UNAUTHORIZED" });
    const notices = f.sent.filter(m => m.text.includes("does not serve this chat"));
    expect(notices).toHaveLength(1);
    expect(String(notices[0].chatId)).toBe(FORUM_CHAT_ID);
  });

  test("a non-allowlisted account in a served group is rejected silently", async () => {
    const f = groupPoller({ groups: { [FORUM_CHAT_ID]: {} } });
    const update = groupMessage(1, "Hello", 9);
    const message = update.message;
    if (message?.from) message.from.id = 999;
    f.poller.ingestUpdates([update]);
    await f.poller.redrivePendingUpdates();

    expect(f.inbound).toEqual([]);
    expect(f.ledgerRow(1)?.status).toBe("REJECTED");
    expect(f.sent).toEqual([]);
  });

  test("the General topic reports no routable thread and replies land in General", async () => {
    const f = groupPoller({}, { forumChatId: FORUM_CHAT_ID });
    // Telegram reports thread id 1 for the General topic on the updates that carry one.
    f.poller.ingestUpdates([groupMessage(1, "Hello", 1), groupMessage(2, "Hello again")]);
    await f.poller.redrivePendingUpdates();

    expect(f.inbound.map(entry => entry.threadId)).toEqual([undefined, undefined]);
  });

  test("the answer to a topic message goes back into that topic", async () => {
    const f = groupPoller({}, { forumChatId: FORUM_CHAT_ID });
    f.poller.ingestUpdates([groupMessage(1, "/status", 9)]);
    await f.poller.redrivePendingUpdates();

    const reply = f.sent.at(-1);
    expect(String(reply?.chatId)).toBe(FORUM_CHAT_ID);
    expect(reply?.threadId).toBe(9);
  });

  test("a channel pinned to one topic refuses a forum chat id", () => {
    expect(() => new TelegramPoller("0:test-only", os.tmpdir(), { dmPolicy: "allowlist", allowFrom: [] }, {}, undefined, {
      messageThreadId: 9,
      forumChatId: FORUM_CHAT_ID,
    } as never)).toThrow("pins it to one");
  });
});

describe("DefaultTelegramForumClient wire contract", () => {
  test("calls Telegram Bot API endpoints with correct JSON payloads", async () => {
    const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
    const mockFetch = async (url: string | URL | Request, init?: RequestInit) => {
      calls.push({ url: String(url), body: JSON.parse(String(init?.body || "{}")) });
      if (String(url).endsWith("/createForumTopic")) {
        return new Response(JSON.stringify({ ok: true, result: { message_thread_id: 42, name: "Topic 42" } }), { status: 200 });
      }
      if (String(url).endsWith("/closeForumTopic")) {
        return new Response(JSON.stringify({ ok: true, result: true }), { status: 200 });
      }
      if (String(url).endsWith("/sendMessage")) {
        return new Response(JSON.stringify({ ok: true, result: { message_id: 101 } }), { status: 200 });
      }
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    };

    const client = new DefaultTelegramForumClient("TEST_TOKEN", "https://api.telegram.org", mockFetch as typeof fetch);

    const topic = await client.createForumTopic("-100112233", "New Topic");
    expect(topic.message_thread_id).toBe(42);
    expect(calls[0].url).toBe("https://api.telegram.org/botTEST_TOKEN/createForumTopic");
    expect(calls[0].body).toEqual({ chat_id: "-100112233", name: "New Topic" });

    const closed = await client.closeForumTopic("-100112233", 42);
    expect(closed).toBe(true);
    expect(calls[1].url).toBe("https://api.telegram.org/botTEST_TOKEN/closeForumTopic");
    expect(calls[1].body).toEqual({ chat_id: "-100112233", message_thread_id: 42 });

    const sent = await client.sendMessage("-100112233", "<b>Hello</b>", { message_thread_id: 42 });
    expect(sent.ok).toBe(true);
    expect(calls[2].url).toBe("https://api.telegram.org/botTEST_TOKEN/sendMessage");
    expect(calls[2].body).toEqual({ chat_id: "-100112233", text: "<b>Hello</b>", parse_mode: "HTML", message_thread_id: 42 });
  });

  test("a Bot API error surfaces Telegram's description instead of an HTTP code", async () => {
    const mockFetch = async () =>
      new Response(JSON.stringify({ ok: false, description: "Bad Request: the chat is not a forum" }), { status: 400 });
    const client = new DefaultTelegramForumClient("TEST_TOKEN", "https://api.telegram.org", mockFetch as typeof fetch);
    await expect(client.createForumTopic("-100112233", "New Topic")).rejects.toThrow("the chat is not a forum");
  });
});

describe("Daemon slot manifest parsing for forum mode", () => {
  let tempDir: string;

  beforeEach(() => {
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "veyyon-manifest-test-"));
  });

  afterEach(() => {
    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  test("resolveDaemonSlots preserves mode and forumChatId from manifest", () => {
    const manifestPath = path.join(tempDir, "manifest.json");
    const stateDir = path.join(tempDir, "channels", "telegram-forum");
    fs.mkdirSync(stateDir, { recursive: true });
    fs.writeFileSync(path.join(stateDir, ".env"), "TELEGRAM_BOT_TOKEN=123456789:ABCDefghIJKLmnOPQRstuvWXYZ\n");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 1,
        slots: [
          {
            slotId: "telegram-forum",
            stateDir,
            enabled: true,
            daemon: true,
            mode: "forum",
            forumChatId: "-10099887766",
            defaultProject: "C:/dev/forum-proj",
          },
        ],
      }),
    );

    const coordinator = new BotPoolCoordinator(path.join(tempDir, "pool.db"), manifestPath, path.join(tempDir, "channels"));
    const slots = resolveDaemonSlots(coordinator, manifestPath);
    expect(slots).toHaveLength(1);
    expect(slots[0].slotId).toBe("telegram-forum");
    expect(slots[0].mode).toBe("forum");
    expect(slots[0].forumChatId).toBe("-10099887766");
    coordinator.close();
  });

  test("allowGroupChat adds the chat without disturbing the rest of access.json", () => {
    const stateDir = path.join(tempDir, "channels", "telegram-forum");
    fs.mkdirSync(stateDir, { recursive: true });
    const accessPath = path.join(stateDir, "access.json");
    fs.writeFileSync(accessPath, JSON.stringify({ dmPolicy: "allowlist", allowFrom: [OPERATOR_ID], groups: {} }));

    const coordinator = new BotPoolCoordinator(
      path.join(tempDir, "pool.db"),
      path.join(tempDir, "manifest.json"),
      path.join(tempDir, "channels"),
    );
    expect(coordinator.allowGroupChat(stateDir, FORUM_CHAT_ID)).toBe(true);
    expect(coordinator.allowGroupChat(stateDir, FORUM_CHAT_ID)).toBe(false);

    const access = coordinator.readAccessConfig(stateDir);
    expect(access.allowFrom).toEqual([OPERATOR_ID]);
    expect(access.groups).toEqual({ [FORUM_CHAT_ID]: {} });
    coordinator.close();
  });

  test("an unparseable access.json is left alone rather than replaced", () => {
    const stateDir = path.join(tempDir, "channels", "telegram-broken");
    fs.mkdirSync(stateDir, { recursive: true });
    const accessPath = path.join(stateDir, "access.json");
    fs.writeFileSync(accessPath, "{ not json");

    const coordinator = new BotPoolCoordinator(
      path.join(tempDir, "pool.db"),
      path.join(tempDir, "manifest.json"),
      path.join(tempDir, "channels"),
    );
    expect(coordinator.allowGroupChat(stateDir, FORUM_CHAT_ID)).toBe(false);
    expect(fs.readFileSync(accessPath, "utf8")).toBe("{ not json");
    coordinator.close();
  });
});

describe("TelegramDaemon forum mode integration", () => {
  let tempDir: string;

  beforeEach(() => {
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "veyyon-daemon-forum-"));
  });

  afterEach(() => {
    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  test("a forum slot polls once and authorizes its supergroup in access.json", async () => {
    const manifestPath = path.join(tempDir, "manifest.json");
    const stateDir = path.join(tempDir, "channels", "telegram-forum-slot");
    fs.mkdirSync(stateDir, { recursive: true });
    fs.writeFileSync(path.join(stateDir, ".env"), "TELEGRAM_BOT_TOKEN=123456789:ABCDefghIJKLmnOPQRstuvWXYZ\n");
    fs.writeFileSync(path.join(stateDir, "access.json"), JSON.stringify({ dmPolicy: "allowlist", allowFrom: [OPERATOR_ID] }));
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 1,
        slots: [
          {
            slotId: "telegram-forum-slot",
            stateDir,
            enabled: true,
            daemon: true,
            mode: "forum",
            forumChatId: "-10055443322",
            defaultProject: "C:/dev/test-forum",
          },
        ],
      }),
    );

    const fake = fakeControl();
    const pollers: Array<{ options: Record<string, unknown> }> = [];
    const daemon = new TelegramDaemon({
      manifestPath,
      poolDbPath: path.join(tempDir, "pool.db"),
      daemonDbPath: path.join(tempDir, "daemon.db"),
      channelsDir: path.join(tempDir, "channels"),
      controlFactory: () => fake.control,
      forumClientFactory: () => new FakeForumApiClient(),
      pollerFactory: (token, dir, access, callbacks, correlation, options) => {
        pollers.push({ options: (options ?? {}) as Record<string, unknown> });
        return {
          running: true,
          start: async () => {},
          stop: async () => {},
          getPrimaryChatId: () => null,
          getActiveThreadId: () => undefined,
          getMeta: () => null,
          sendTelegramMessage: async () => null,
        } as unknown as TelegramPoller;
      },
      log: () => {},
    });

    const status = await daemon.start();
    expect(status.slots).toHaveLength(1);
    expect(status.slots[0].slotId).toBe("telegram-forum-slot");
    expect(status.slots[0].polling).toBe(true);

    // One Telegram consumer per slot: a second getUpdates loop inside the forum
    // manager stole the first one's updates and the group looked dead.
    expect(pollers).toHaveLength(1);
    expect(pollers[0].options.forumChatId).toBe("-10055443322");

    const access = JSON.parse(fs.readFileSync(path.join(stateDir, "access.json"), "utf8"));
    expect(access.groups).toEqual({ "-10055443322": {} });
    expect(access.allowFrom).toEqual([OPERATOR_ID]);

    await daemon.stop();
    expect(daemon.status().slots).toHaveLength(0);
  });

  test("autoAttach: false in manifest opts out of topic creation", async () => {
    const manifestPath = path.join(tempDir, "manifest.json");
    const stateDir = path.join(tempDir, "channels", "telegram-forum-optout");
    fs.mkdirSync(stateDir, { recursive: true });
    fs.writeFileSync(path.join(stateDir, ".env"), "TELEGRAM_BOT_TOKEN=123456789:ABCDefghIJKLmnOPQRstuvWXYZ\n");
    fs.writeFileSync(path.join(stateDir, "access.json"), JSON.stringify({ dmPolicy: "allowlist", allowFrom: [OPERATOR_ID] }));
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 1,
        slots: [
          {
            slotId: "telegram-forum-optout",
            stateDir,
            enabled: true,
            daemon: true,
            mode: "forum",
            forumChatId: "-10055443322",
            autoAttach: false,
            defaultProject: "C:/dev/test-optout",
          },
        ],
      }),
    );

    const fake = fakeControl();
    fake.sessions.push({
      id: "live-optout-1",
      cwd: "C:/dev/test-optout",
      workspace: "C:/dev/test-optout",
      title: null,
      status: "Idle",
      modifiedAtMs: Date.now(),
    });

    const forumClient = new FakeForumApiClient();
    const daemon = new TelegramDaemon({
      manifestPath,
      poolDbPath: path.join(tempDir, "pool.db"),
      daemonDbPath: path.join(tempDir, "daemon.db"),
      channelsDir: path.join(tempDir, "channels"),
      controlFactory: () => fake.control,
      forumClientFactory: () => forumClient,
      pollerFactory: () => ({
        running: true,
        start: async () => {},
        stop: async () => {},
        getPrimaryChatId: () => null,
        getActiveThreadId: () => undefined,
        getMeta: () => null,
        sendTelegramMessage: async () => null,
      } as unknown as TelegramPoller),
      log: () => {},
    });

    await daemon.start();
    // autoAttach: false -> no topic created on daemon start
    expect(forumClient.topics.size).toBe(0);

    await daemon.stop();
  });

  test("daemon start auto-attaches live sessions and binds them to forum topics", async () => {
    const manifestPath = path.join(tempDir, "manifest.json");
    const stateDir = path.join(tempDir, "channels", "telegram-forum-auto");
    fs.mkdirSync(stateDir, { recursive: true });
    fs.writeFileSync(path.join(stateDir, ".env"), "TELEGRAM_BOT_TOKEN=123456789:ABCDefghIJKLmnOPQRstuvWXYZ\n");
    fs.writeFileSync(path.join(stateDir, "access.json"), JSON.stringify({ dmPolicy: "allowlist", allowFrom: [OPERATOR_ID] }));
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 1,
        slots: [
          {
            slotId: "telegram-forum-auto",
            stateDir,
            enabled: true,
            daemon: true,
            mode: "forum",
            forumChatId: "-10055443322",
            autoAttach: true,
            defaultProject: "C:/dev/auto-attached",
          },
        ],
      }),
    );

    const fake = fakeControl();
    fake.sessions.push({
      id: "live-auto-1",
      cwd: "C:/dev/auto-attached",
      workspace: "C:/dev/auto-attached",
      title: null,
      status: "Idle",
      modifiedAtMs: Date.now(),
    });

    const forumClient = new FakeForumApiClient();
    const daemon = new TelegramDaemon({
      manifestPath,
      poolDbPath: path.join(tempDir, "pool.db"),
      daemonDbPath: path.join(tempDir, "daemon.db"),
      channelsDir: path.join(tempDir, "channels"),
      controlFactory: () => fake.control,
      forumClientFactory: () => forumClient,
      pollerFactory: () => ({
        running: true,
        start: async () => {},
        stop: async () => {},
        getPrimaryChatId: () => null,
        getActiveThreadId: () => undefined,
        getMeta: () => null,
        sendTelegramMessage: async () => null,
      } as unknown as TelegramPoller),
      log: () => {},
    });

    await daemon.start();
    expect(forumClient.topics.size).toBe(1);
    const topic = [...forumClient.topics.values()][0];
    expect(topic.name).toBe("auto-attached");

    const store = new DaemonStore(path.join(tempDir, "daemon.db"));
    const routes = store.listRoutes("telegram-forum-auto");
    expect(routes).toHaveLength(1);
    expect(routes[0].sessionId).toBe("live-auto-1");
    store.close();

    await daemon.stop();
  });
});
