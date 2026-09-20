/**
 * daemon-forum.test.ts — Unit tests for supergroup forum-topics mode in the Veyyon Telegram daemon.
 *
 * Covers:
 * - Forum slot configuration parsing ("mode": "forum", forumChatId)
 * - Topic creation via `createForumTopic` on /new or attach
 * - Inbound message routing by `message_thread_id` to bound sessions
 * - /topics command listing active topics with turn status
 * - Topic closure on /detach, /close, or session end
 * - Outbound session event relaying into topic threads
 * - Reconciling ended sessions automatically
 * - TelegramDaemon integration with forum mode
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
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
  type TelegramForumUpdate,
} from "../daemon/forum";
import { TelegramDaemon } from "../daemon/runtime";
import {
  type DaemonSessionSummary,
  type DeliveryMode,
  type GuiHostSessionControl,
  type SessionEvent,
} from "../daemon/session-control";
import { DaemonStore } from "../daemon/store";
import { BotPoolCoordinator } from "../extension/coordinator";

class FakeForumApiClient implements ForumApiClient {
  public topics: Map<number, { name: string; iconColor?: number; closed: boolean }> = new Map();
  public sentMessages: Array<{
    chatId: string | number;
    text: string;
    messageThreadId?: number;
    parseMode?: string;
  }> = [];
  public updatesQueue: TelegramForumUpdate[] = [];
  private threadCounter = 100;

  public async createForumTopic(
    chatId: string | number,
    name: string,
    iconColor?: number,
  ): Promise<ForumTopic> {
    const threadId = ++this.threadCounter;
    this.topics.set(threadId, { name, iconColor, closed: false });
    return {
      message_thread_id: threadId,
      name,
      icon_color: iconColor,
    };
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
    options?: {
      message_thread_id?: number;
      parse_mode?: string;
      reply_markup?: Record<string, unknown>;
    },
  ): Promise<{ ok: boolean; result: { message_id: number } }> {
    this.sentMessages.push({
      chatId,
      text,
      messageThreadId: options?.message_thread_id,
      parseMode: options?.parse_mode,
    });
    return { ok: true, result: { message_id: this.sentMessages.length } };
  }

  public async getUpdates(): Promise<{ ok: boolean; result: TelegramForumUpdate[] }> {
    const updates = [...this.updatesQueue];
    this.updatesQueue = [];
    return { ok: true, result: updates };
  }
}

interface FakeControl {
  control: GuiHostSessionControl;
  sessions: DaemonSessionSummary[];
  delivered: Array<{ sessionId: string; text: string; mode: DeliveryMode }>;
  created: Array<{ workspace: string; title: string }>;
  loaded: string[];
  busy: Set<string>;
}

function fakeControl(initialSessions: DaemonSessionSummary[] = []): FakeControl {
  const sessions = [...initialSessions];
  const delivered: Array<{ sessionId: string; text: string; mode: DeliveryMode }> = [];
  const created: Array<{ workspace: string; title: string }> = [];
  const loaded: string[] = [];
  const busy = new Set<string>();
  let counter = 0;

  const control = {
    endpoint: "tcp:127.0.0.1:7699",
    isBusy: (sessionId: string) => busy.has(sessionId),
    listSessions: async () => sessions,
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

  return { control: control as unknown as GuiHostSessionControl, sessions, delivered, created, loaded, busy };
}

const FORUM_CHAT_ID = "-1009876543210";

describe("ForumManager", () => {
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

  afterEach(async () => {
    await manager.stop();
    store.close();
    fs.rmSync(tempDir, { recursive: true, force: true });
  });

  test("formatTopicName uses title or directory basename and caps length", () => {
    expect(formatTopicName("session-1234567890", "C:/dev/my-project", "Feature X")).toBe("Feature X (session-)");
    expect(formatTopicName("session-abcdef", "C:/dev/super-board", null)).toBe("super-board (session-)");
  });

  test("ensureTopic creates a Telegram topic on the Bot API and records the route", async () => {
    const threadId = await manager.ensureTopic("sess-1", "C:/dev/proj", "Testing Topic");
    expect(threadId).toBeGreaterThan(0);
    expect(client.topics.get(threadId)).toBeDefined();
    expect(client.topics.get(threadId)?.name).toContain("Testing Topic");

    const route = store.getRoute("slot-forum", FORUM_CHAT_ID, String(threadId));
    expect(route).toBeDefined();
    expect(route?.sessionId).toBe("sess-1");
    expect(route?.workspace).toBe("C:/dev/proj");

    // Welcome message was sent into the topic thread
    const welcome = client.sentMessages.find(m => m.messageThreadId === threadId);
    expect(welcome).toBeDefined();
    expect(welcome?.text).toContain("Veyyon Session Attached");
    expect(welcome?.text).toContain("sess-1");
  });

  test("ensureTopic returns existing threadId if session already has a topic route", async () => {
    const threadId1 = await manager.ensureTopic("sess-1", "C:/dev/proj");
    const threadId2 = await manager.ensureTopic("sess-1", "C:/dev/proj");
    expect(threadId2).toBe(threadId1);
    expect(client.topics.size).toBe(1);
  });

  test("inbound messages in General topic prompt operator to use session topics", async () => {
    const reply = await manager.handleMessage("hello there", undefined);
    expect(reply).toContain("Veyyon Supergroup Forum Mode");
    expect(reply).toContain("/topics");
  });

  test("inbound messages in an unbound topic warn that the topic is not connected", async () => {
    const reply = await manager.handleMessage("fix the bug", 999);
    expect(reply).toContain("Unbound Topic (#999)");
    expect(reply).toContain("/attach");
  });

  test("inbound messages in a bound topic route to the session by message_thread_id", async () => {
    const threadId = await manager.ensureTopic("sess-1", "C:/dev/proj");

    // Turn is started (idle session) -> deliver returns null so agent answers directly
    const reply = await manager.handleMessage("make a test", threadId);
    expect(reply).toBeNull();
    expect(fake.delivered).toEqual([{ sessionId: "sess-1", text: "make a test", mode: "auto" }]);

    // When turn is busy, steer/queued acknowledgment is returned
    fake.busy.add("sess-1");
    const ack = await manager.handleMessage("change approach", threadId);
    expect(ack).toContain("Queued as a steer");
    expect(fake.delivered).toHaveLength(2);
    expect(fake.delivered[1].text).toBe("change approach");
  });

  test("/new starts a session, creates a topic, and acknowledges", async () => {
    const reply = await manager.handleCommand("/new", undefined);
    expect(reply).toContain("started in topic #");
    expect(fake.created).toHaveLength(1);
    expect(fake.created[0].workspace).toBe("C:/dev/forum-proj");
    expect(client.topics.size).toBe(1);
  });

  test("/topics lists active topics with running/idle status and highlights current topic", async () => {
    const t1 = await manager.ensureTopic("sess-1", "C:/dev/proj1");
    const t2 = await manager.ensureTopic("sess-2", "C:/dev/proj2");
    fake.busy.add("sess-2");

    const listing = await manager.handleCommand("/topics", t1);
    expect(listing).toContain("Active Forum Topics (2)");
    expect(listing).toContain(`➡️ <b>#${t1}</b>: <code>sess-1</code> — <i>C:/dev/proj1</i> (idle)`);
    expect(listing).toContain(`• <b>#${t2}</b>: <code>sess-2</code> — <i>C:/dev/proj2</i> (running)`);
  });

  test("/where reports session details inside a topic", async () => {
    const threadId = await manager.ensureTopic("sess-1", "C:/dev/proj1");
    const info = await manager.handleCommand("/where", threadId);
    expect(info).toContain(`Topic #${threadId}`);
    expect(info).toContain("sess-1");
    expect(info).toContain("C:/dev/proj1");
    expect(info).toContain("Turn status: idle");
  });

  test("/detach and /close close the forum topic and remove the route", async () => {
    const threadId = await manager.ensureTopic("sess-1", "C:/dev/proj1");
    expect(client.topics.get(threadId)?.closed).toBe(false);

    const reply = await manager.handleCommand("/detach", threadId);
    expect(reply).toContain(`Topic #${threadId} detached and closed`);
    expect(client.topics.get(threadId)?.closed).toBe(true);

    const route = store.getRoute("slot-forum", FORUM_CHAT_ID, String(threadId));
    expect(route).toBeNull();
  });

  test("outbound session events relay to the bound topic thread", async () => {
    const threadId = await manager.ensureTopic("sess-1", "C:/dev/proj1");
    client.sentMessages = [];

    const event: SessionEvent = {
      kind: "appended",
      sessionId: "sess-1",
      entries: [{ entryId: "e1", text: "Task completed successfully." }],
    };

    await manager.onSessionEvent(event);
    expect(client.sentMessages).toHaveLength(1);
    expect(client.sentMessages[0].messageThreadId).toBe(threadId);
    expect(client.sentMessages[0].text).toContain("Task completed successfully.");

    // Idempotent: same entryId is not delivered twice
    await manager.onSessionEvent(event);
    expect(client.sentMessages).toHaveLength(1);
  });

  test("reconcileEndedSessions closes topics for ended or non-existent sessions", async () => {
    const t1 = await manager.ensureTopic("sess-active", "C:/dev/proj1");
    const t2 = await manager.ensureTopic("sess-closed", "C:/dev/proj2");

    fake.sessions.push(
      { id: "sess-active", cwd: "C:/dev/proj1", workspace: "C:/dev/proj1", title: "Active", status: "Idle", modifiedAtMs: 1 },
      { id: "sess-closed", cwd: "C:/dev/proj2", workspace: "C:/dev/proj2", title: "Closed", status: "Closed", modifiedAtMs: 1 },
    );

    const closed = await manager.reconcileEndedSessions();
    expect(closed).toBe(1);
    expect(client.topics.get(t1)?.closed).toBe(false);
    expect(client.topics.get(t2)?.closed).toBe(true);
    expect(store.getRoute("slot-forum", FORUM_CHAT_ID, String(t2))).toBeNull();
    expect(store.getRoute("slot-forum", FORUM_CHAT_ID, String(t1))).toBeDefined();
  });
});

describe("DefaultTelegramForumClient wire contract", () => {
  test("calls Telegram Bot API endpoints with correct JSON payloads", async () => {
    const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
    const mockFetch = async (url: string | URL | Request, init?: RequestInit) => {
      calls.push({
        url: String(url),
        body: JSON.parse(String(init?.body || "{}")),
      });
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
});

describe("TelegramDaemon forum mode integration", () => {
  let tempDir: string;

  beforeEach(() => {
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "veyyon-daemon-forum-"));
  });

  afterEach(() => {
    fs.rmSync(tempDir, { recursive: true, force: true });
  });
  test("daemon starts forumManager for forum slot and handles events", async () => {
    const manifestPath = path.join(tempDir, "manifest.json");
    const stateDir = path.join(tempDir, "channels", "telegram-forum-slot");
    fs.mkdirSync(stateDir, { recursive: true });
    fs.writeFileSync(path.join(stateDir, ".env"), "TELEGRAM_BOT_TOKEN=123456789:ABCDefghIJKLmnOPQRstuvWXYZ\n");
    fs.writeFileSync(path.join(stateDir, "access.json"), JSON.stringify({ dmPolicy: "allowlist", allowFrom: ["12345"] }));
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

    const fakeClient = new FakeForumApiClient();
    const fake = fakeControl();

    const daemon = new TelegramDaemon({
      manifestPath,
      poolDbPath: path.join(tempDir, "pool.db"),
      daemonDbPath: path.join(tempDir, "daemon.db"),
      channelsDir: path.join(tempDir, "channels"),
      controlFactory: () => fake.control,
      forumClientFactory: () => fakeClient,
      log: () => {},
    });

    const status = await daemon.start();
    expect(status.slots).toHaveLength(1);
    expect(status.slots[0].slotId).toBe("telegram-forum-slot");
    expect(status.slots[0].polling).toBe(true);

    // Stop daemon releases lease and closes polling
    await daemon.stop();
    const stoppedStatus = daemon.status();
    expect(stoppedStatus.slots).toHaveLength(0);
  });
});
