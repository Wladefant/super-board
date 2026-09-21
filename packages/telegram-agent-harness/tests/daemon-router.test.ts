/**
 * Per-slot routing: which session a chat or forum topic talks to, what gets sent
 * back to it, and what must never be sent back (a transcript it already saw).
 *
 * The session control is a stand-in for the GUI host — its own wire behaviour is
 * covered against a real socket in daemon-session-control.test.ts — so these tests
 * assert the router's decisions, not the protocol.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { DaemonSlot } from "../daemon/config";
import { SlotRouter, getDaemonCommands, type RouteTarget, type TopicLifecycle } from "../daemon/router";
import { renderTelegramHelp } from "../extension/command-registry";
import {
  SessionControlUnavailableError,
  type DaemonSessionSummary,
  type DeliveryMode,
  type GuiHostSessionControl,
} from "../daemon/session-control";
import { DaemonStore } from "../daemon/store";

interface Delivered {
  sessionId: string;
  text: string;
  mode: DeliveryMode;
}

interface FakeControl {
  control: GuiHostSessionControl;
  delivered: Delivered[];
  created: { workspace: string; title: string }[];
  loaded: string[];
  aborted: string[];
  busy: Set<string>;
}

function fakeControl(sessions: DaemonSessionSummary[] = [], options: { unavailable?: boolean } = {}): FakeControl {
  const delivered: Delivered[] = [];
  const created: { workspace: string; title: string }[] = [];
  const loaded: string[] = [];
  const aborted: string[] = [];
  const busy = new Set<string>();
  let counter = 0;

  const control = {
    endpoint: "tcp:127.0.0.1:1",
    isBusy: (sessionId: string) => busy.has(sessionId),
    listSessions: async () => {
      if (options.unavailable) throw new SessionControlUnavailableError("socket refused");
      return sessions;
    },
    lastPrompt: async () => null,
    findSession: async (workspace: string) =>
      sessions.find(session => path.resolve(session.cwd).toLowerCase() === path.resolve(workspace).toLowerCase()) ?? null,
    createSession: async (workspace: string, title: string) => {
      created.push({ workspace, title });
      const id = `sess-new-${++counter}`;
      sessions.push({ id, cwd: workspace, workspace, title, status: "Idle", modifiedAtMs: Date.now() });
      return id;
    },
    ensureSession: async (workspace: string, title: string) => {
      const existing = sessions.find(
        session => path.resolve(session.cwd).toLowerCase() === path.resolve(workspace).toLowerCase(),
      );
      if (existing) return existing.id;
      return control.createSession(workspace, title);
    },
    deliver: async (sessionId: string, text: string, mode: DeliveryMode = "auto") => {
      delivered.push({ sessionId, text, mode });
      if (mode === "steer") return "steered" as const;
      if (mode === "followUp") return "queued" as const;
      return busy.has(sessionId) ? ("steered" as const) : ("started" as const);
    },
    abort: async (sessionId: string) => {
      aborted.push(sessionId);
      return busy.has(sessionId);
    },
    loadTranscript: async (sessionId: string) => {
      loaded.push(sessionId);
    },
    usage: async () => null,
    close: () => {},
  };

  return { control: control as unknown as GuiHostSessionControl, delivered, created, loaded, aborted, busy };
}

function summary(id: string, cwd: string, title: string | null = null): DaemonSessionSummary {
  return { id, cwd, workspace: cwd, title, status: "Idle", modifiedAtMs: 1 };
}

/** Records what a forum slot asked of Telegram, without a Bot API call. */
interface FakeTopics extends TopicLifecycle {
  opened: { sessionId: string; workspace: string }[];
  closed: number[];
}

function fakeTopics(): FakeTopics {
  const opened: { sessionId: string; workspace: string }[] = [];
  const closed: number[] = [];
  const threads = new Map<string, number>();
  return {
    opened,
    closed,
    ensureTopic: async (sessionId: string, workspace: string) => {
      const existing = threads.get(sessionId);
      if (existing !== undefined) return existing;
      opened.push({ sessionId, workspace });
      const threadId = 100 + threads.size;
      threads.set(sessionId, threadId);
      return threadId;
    },
    closeTopic: async (messageThreadId: number) => {
      closed.push(messageThreadId);
      return true;
    },
    listTopicsText: (currentTopicId: string) => `topics@${currentTopicId}`,
  };
}

const CHAT = "1247617658";
const FORUM_CHAT = "-1004422647618";
const DM: RouteTarget = { chatId: CHAT, topicId: "" };
const GENERAL: RouteTarget = { chatId: FORUM_CHAT, topicId: "" };
const TOPIC_9: RouteTarget = { chatId: FORUM_CHAT, topicId: "9" };
const TOPIC_14: RouteTarget = { chatId: FORUM_CHAT, topicId: "14" };

let root: string;
let store: DaemonStore;
let sent: { target: RouteTarget; html: string }[];
let relayed: { target: RouteTarget; markdown: string }[];

function buildRouter(
  slot: Partial<DaemonSlot>,
  control: GuiHostSessionControl,
  topics?: TopicLifecycle,
): SlotRouter {
  return new SlotRouter({
    slot: {
      slotId: "slot-1",
      stateDir: path.join(root, "slot-1"),
      botId: "1000001",
      fingerprint: "fp",
      preferredProjects: [],
      enabled: true,
      workspace: "C:/dev/demo",
      ...slot,
    },
    store,
    control,
    topics,
    send: async (target, html) => {
      sent.push({ target, html });
    },
    relay: async (target, markdown) => {
      relayed.push({ target, markdown });
    },
    log: () => {},
  });
}

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "veyyon-daemon-router-"));
  store = new DaemonStore(path.join(root, "daemon.db"));
  sent = [];
  relayed = [];
});

afterEach(() => {
  store.close();
  fs.rmSync(root, { recursive: true, force: true });
});

describe("inbound routing", () => {
  test("an unrouted chat gets a session in the slot's workspace and stays bound to it", async () => {
    const fake = fakeControl();
    const router = buildRouter({}, fake.control);

    const ack = await router.deliver(DM, "first message");
    expect(fake.created).toEqual([{ workspace: "C:/dev/demo", title: "Telegram slot-1" }]);
    expect(ack).toContain("sess-new-1");
    expect(router.boundSession(DM)).toBe("sess-new-1");

    // Second message reuses the binding rather than creating another session.
    expect(await router.deliver(DM, "second message")).toBeNull();
    expect(fake.created.length).toBe(1);
    expect(fake.delivered).toEqual([
      { sessionId: "sess-new-1", text: "first message", mode: "auto" },
      { sessionId: "sess-new-1", text: "second message", mode: "auto" },
    ]);
  });

  test("an existing session for the workspace is reused instead of starting a second one", async () => {
    const fake = fakeControl([summary("sess-existing", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);

    await router.deliver(DM, "hello");
    expect(fake.created).toEqual([]);
    expect(router.boundSession(DM)).toBe("sess-existing");
  });

  test("a slot with no resolvable workspace refuses to invent one", async () => {
    const fake = fakeControl();
    const router = buildRouter({ workspace: null }, fake.control);

    const ack = await router.deliver(DM, "hello");
    expect(ack).toContain("No workspace is configured");
    expect(fake.created).toEqual([]);
    expect(fake.delivered).toEqual([]);
    expect(router.boundSession(DM)).toBeNull();
  });

  test("steer and follow-up modes are acknowledged distinctly and reach the bound session", async () => {
    const fake = fakeControl([summary("sess-existing", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);
    await router.deliver(DM, "start");

    expect(await router.deliver(DM, "redirect", "steer")).toContain("steer");
    expect(await router.deliver(DM, "afterwards", "followUp")).toContain("follow-up");
    expect(fake.delivered.slice(1)).toEqual([
      { sessionId: "sess-existing", text: "redirect", mode: "steer" },
      { sessionId: "sess-existing", text: "afterwards", mode: "followUp" },
    ]);
  });

  test("busy state and abort follow the chat's binding, not the slot", async () => {
    const fake = fakeControl([summary("sess-existing", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);

    expect(router.isBusy(DM)).toBe(false);
    expect(await router.abort(DM)).toBe(false);
    expect(fake.aborted).toEqual([]);

    await router.deliver(DM, "start");
    fake.busy.add("sess-existing");
    expect(router.isBusy(DM)).toBe(true);
    expect(await router.abort(DM)).toBe(true);
    expect(fake.aborted).toEqual(["sess-existing"]);
  });
});

describe("forum topic routing", () => {
  test("each topic of one supergroup talks to its own session", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/a"), summary("sess-b", "C:/dev/b")]);
    const router = buildRouter({}, fake.control, fakeTopics());

    await router.bind(TOPIC_9, "sess-a", "C:/dev/a");
    await router.bind(TOPIC_14, "sess-b", "C:/dev/b");

    expect(router.boundSession(TOPIC_9)).toBe("sess-a");
    expect(router.boundSession(TOPIC_14)).toBe("sess-b");

    await router.deliver(TOPIC_9, "for a");
    await router.deliver(TOPIC_14, "for b");
    expect(fake.delivered).toEqual([
      { sessionId: "sess-a", text: "for a", mode: "auto" },
      { sessionId: "sess-b", text: "for b", mode: "auto" },
    ]);
  });

  test("one session's output reaches only its own topic", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/a"), summary("sess-b", "C:/dev/b")]);
    const router = buildRouter({}, fake.control, fakeTopics());
    await router.bind(TOPIC_9, "sess-a", "C:/dev/a");
    await router.bind(TOPIC_14, "sess-b", "C:/dev/b");

    await router.onSessionEvent({ kind: "appended", sessionId: "sess-a", entries: [{ entryId: "e1", text: "from a" }] });
    expect(relayed).toEqual([{ target: { chatId: FORUM_CHAT, topicId: "9" }, markdown: "from a" }]);
  });

  test("the General topic is a lobby: plain text binds nothing and says where to go", async () => {
    const fake = fakeControl();
    const router = buildRouter({}, fake.control, fakeTopics());

    const reply = await router.deliver(GENERAL, "fix the bug");
    expect(reply).toContain("General is the lobby");
    expect(reply).toContain("/new");
    expect(fake.created).toEqual([]);
    expect(fake.delivered).toEqual([]);
    expect(router.boundSession(GENERAL)).toBeNull();
  });

  test("/new from General opens a topic and binds the session to it, not to General", async () => {
    const fake = fakeControl();
    const topics = fakeTopics();
    const router = buildRouter({}, fake.control, topics);

    await router.handleCommand("/new", GENERAL);
    expect(topics.opened).toEqual([{ sessionId: "sess-new-1", workspace: "C:/dev/demo" }]);
    expect(sent.at(-1)?.html).toContain("started in topic #100");
    expect(router.boundSession({ chatId: FORUM_CHAT, topicId: "100" })).toBe("sess-new-1");
    expect(router.boundSession(GENERAL)).toBeNull();
  });

  test("/new inside a topic binds that topic instead of opening another", async () => {
    const fake = fakeControl();
    const topics = fakeTopics();
    const router = buildRouter({}, fake.control, topics);

    await router.handleCommand("/new", TOPIC_9);
    expect(topics.opened).toEqual([]);
    expect(router.boundSession(TOPIC_9)).toBe("sess-new-1");
  });

  test("/attach from General reuses the topic the session already owns", async () => {
    const fake = fakeControl([summary("sess-abcdef", "C:/dev/other", "Other")]);
    const topics = fakeTopics();
    const router = buildRouter({}, fake.control, topics);

    await router.handleCommand("/attach sess-abc", GENERAL);
    await router.handleCommand("/attach sess-abc", GENERAL);
    expect(topics.opened).toHaveLength(1);
    expect(router.boundSession({ chatId: FORUM_CHAT, topicId: "100" })).toBe("sess-abcdef");
  });

  test("/topics is answered by the topic lifecycle in forum mode and refused in direct mode", async () => {
    const forum = buildRouter({}, fakeControl().control, fakeTopics());
    await forum.handleCommand("/topics", TOPIC_9);
    expect(sent.at(-1)?.html).toBe("topics@9");

    const direct = buildRouter({}, fakeControl().control);
    await direct.handleCommand("/topics", DM);
    expect(sent.at(-1)?.html).toContain("direct-chat mode");
  });

  test("/detach unbinds the topic without closing, while /detach close closes it", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/a")]);
    const topics = fakeTopics();
    const router = buildRouter({}, fake.control, topics);
    await router.bind(TOPIC_9, "sess-a", "C:/dev/a");

    await router.handleCommand("/detach", GENERAL);
    expect(topics.closed).toEqual([]);
    expect(sent.at(-1)?.html).toContain("inside the topic");

    await router.handleCommand("/detach", TOPIC_9);
    expect(topics.closed).toEqual([]);
    expect(router.boundSession(TOPIC_9)).toBeNull();
    expect(sent.at(-1)?.html).toContain("Topic #9 detached");
    expect(sent.at(-1)?.html).toContain("/detach close");
    expect(fake.aborted).toEqual([]);

    await router.bind(TOPIC_9, "sess-a", "C:/dev/a");
    await router.handleCommand("/detach close", TOPIC_9);
    expect(topics.closed).toEqual([9]);
    expect(router.boundSession(TOPIC_9)).toBeNull();
    expect(sent.at(-1)?.html).toContain("Topic #9 detached and closed");
  });

  test("/where names the topic a message came from", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/a")]);
    const router = buildRouter({}, fake.control, fakeTopics());
    await router.bind(TOPIC_9, "sess-a", "C:/dev/a");

    await router.handleCommand("/where", TOPIC_9);
    expect(sent.at(-1)?.html).toContain("Topic: <b>#9</b>");
    expect(sent.at(-1)?.html).toContain("sess-a");

    await router.handleCommand("/where", GENERAL);
    expect(sent.at(-1)?.html).toContain("General is the lobby");
  });
});

describe("outbound delivery", () => {
  test("live output reaches the bound chat exactly once", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);
    await router.deliver(DM, "start");

    const event = { kind: "appended" as const, sessionId: "sess-a", entries: [{ entryId: "e1", text: "the answer" }] };
    await router.onSessionEvent(event);
    await router.onSessionEvent(event);

    expect(relayed).toEqual([{ target: DM, markdown: "the answer" }]);
  });

  test("binding a chat marks existing history delivered, so nothing is replayed", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);
    await router.deliver(DM, "start");
    expect(fake.loaded).toEqual(["sess-a"]);

    // The transcript snapshot the host replies with, containing pre-existing prose.
    await router.onSessionEvent({
      kind: "history",
      sessionId: "sess-a",
      entries: [{ entryId: "old-1", text: "said before the chat was listening" }],
    });
    expect(relayed).toEqual([]);

    // A snapshot that repeats history plus one new entry sends only the new one.
    await router.onSessionEvent({
      kind: "appended",
      sessionId: "sess-a",
      entries: [
        { entryId: "old-1", text: "said before the chat was listening" },
        { entryId: "new-1", text: "brand new" },
      ],
    });
    expect(relayed).toEqual([{ target: DM, markdown: "brand new" }]);
  });

  test("output for another slot's route is not delivered by this router", async () => {
    const fake = fakeControl();
    const router = buildRouter({}, fake.control);
    store.putRoute({ slotId: "other-slot", chatId: "999", topicId: "", sessionId: "sess-x", workspace: "C:/dev/demo" });

    await router.onSessionEvent({ kind: "appended", sessionId: "sess-x", entries: [{ entryId: "e1", text: "not mine" }] });
    expect(relayed).toEqual([]);
  });

  test("streaming events carry no text and are not relayed", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);
    await router.deliver(DM, "start");

    await router.onSessionEvent({ kind: "streaming", sessionId: "sess-a", active: true });
    expect(relayed).toEqual([]);
  });
});

describe("routing commands", () => {
  test("only routing verbs are claimed; ordinary text and other commands fall through", async () => {
    expect(SlotRouter.isRoutingCommand("/sessions")).toBe(true);
    expect(SlotRouter.isRoutingCommand("/attach abc")).toBe(true);
    expect(SlotRouter.isRoutingCommand("/attach@mybot abc")).toBe(true);
    expect(SlotRouter.isRoutingCommand("/status")).toBe(false);
    expect(SlotRouter.isRoutingCommand("please run the tests")).toBe(false);

    const router = buildRouter({}, fakeControl().control);
    expect(await router.handleCommand("/status", DM)).toBe(false);
    expect(sent).toEqual([]);
  });

  test("filters unassigned and old finished sessions, while all reveals them", async () => {
    const now = Date.now();
    const sessions = [
      { ...summary("old", "C:/old"), status: "Complete", modifiedAtMs: now - 3_700_000 },
      { ...summary("recent", "C:/recent"), status: "Complete", modifiedAtMs: now - 720_000 },
      { ...summary("live", "C:/live"), status: "Pending", modifiedAtMs: now - 7_200_000 },
      { ...summary("foreign", ""), workspace: "ws-global" },
    ];
    const router = buildRouter({}, fakeControl(sessions).control);
    await router.handleCommand("/sessions", DM);
    expect(sent[0].html).not.toContain("C:/old");
    expect(sent[0].html).not.toContain("No workspace");
    expect(sent[0].html).toContain("<b>recent</b> <code>C:/recent</code>");
    expect(sent[0].html).toContain("<b>live</b> <code>C:/live</code>");
    expect(sent[0].html).toContain("/attach <n> or /attach <folder>");
    await router.handleCommand("/sessions all", DM);
    expect(sent[1].html).toContain("<b>old</b> <code>C:/old</code>");
  });

  test("indices survive router recreation and host reorder and are isolated by chat and topic", async () => {
    const sessions = [summary("a", "C:/a"), summary("b", "C:/b")];
    const fake = fakeControl(sessions);
    const router = buildRouter({}, fake.control);
    await router.handleCommand("/sessions", TOPIC_9);
    sessions.reverse();
    await buildRouter({}, fake.control).handleCommand("/attach 1", TOPIC_9);
    expect(router.boundSession(TOPIC_9)).toBe("a");
    await router.handleCommand("/attach 1", TOPIC_14);
    await router.handleCommand("/attach 1", DM);
    expect(router.boundSession(TOPIC_14)).toBeNull();
    expect(router.boundSession(DM)).toBeNull();
    sessions.splice(sessions.findIndex(session => session.id === "a"), 1);
    await router.handleCommand("/attach 1", TOPIC_9);
    expect(sent.at(-1)?.html).toContain("No running session matches");
    expect(fake.loaded).toEqual(["a"]);
  });

  test("folder attachment is case-insensitive and ambiguous folders and ID prefixes refuse binding", async () => {
    const sessions = [summary("abc-one", "C:\\dev\\demo"), summary("abc-two", "D:/other")];
    const fake = fakeControl(sessions);
    const router = buildRouter({}, fake.control);
    await router.handleCommand("/attach DEMO", DM);
    expect(router.boundSession(DM)).toBe("abc-one");
    sessions.push(summary("xyz", "D:/demo"));
    await router.handleCommand("/attach demo", TOPIC_9);
    await router.handleCommand("/attach abc", TOPIC_9);
    expect(router.boundSession(TOPIC_9)).toBeNull();
    expect(sent.slice(-2).every(message => message.html.includes("More than one"))).toBe(true);
  });

  test("filters spawned subagents and keeps only top-level interactive sessions", async () => {
    const sessions: DaemonSessionSummary[] = [
      summary("main-veyyon", "C:/Users/wkiri/development/veyyon"),
      summary("main-sb", "C:/Users/wkiri/development/super-board"),
      { ...summary("sub-1", "C:/Users/wkiri/development/veyyon"), parent_path: "C:/main.jsonl", parentPath: "C:/main.jsonl" },
      { ...summary("sub-2", "C:/Users/wkiri/development/super-board"), kind: "subagent" },
      { ...summary("sub-3", "C:/Users/wkiri/development/super-board"), isSubagent: true },
      { ...summary("sub-4", "C:/Users/wkiri/development/super-board"), parentId: "main-sb" },
      { ...summary("agent-worker", "C:/Users/wkiri/development/super-board") },
    ];
    const router = buildRouter({}, fakeControl(sessions).control);
    await router.handleCommand("/sessions", DM);
    expect(sent[0].html).toContain("<b>super-board</b> <code>C:/Users/wkiri/development/super-board</code>");
    expect(sent[0].html).toContain("<b>veyyon</b> <code>C:/Users/wkiri/development/veyyon</code>");
    expect(sent[0].html).not.toContain("sub-1");
    expect(sent[0].html).not.toContain("sub-2");
    expect(sent[0].html).not.toContain("sub-3");
    expect(sent[0].html).not.toContain("sub-4");
    expect(sent[0].html).not.toContain("agent-worker");
    expect(sent[0].html).toContain("/attach <n> or /attach <folder>");
  });

  test("sorts by folder basename, suffixes duplicates with (2), and attaches by folder", async () => {
    const sessions = [
      summary("s1", "C:/dev/zebra"),
      summary("s2", "C:/dev/alpha"),
      summary("s3", "D:/other/alpha"),
    ];
    const router = buildRouter({}, fakeControl(sessions).control);
    await router.handleCommand("/sessions", DM);
    const expected = [
      "1. <b>alpha</b> <code>C:/dev/alpha</code>",
      "2. <b>alpha (2)</b> <code>D:/other/alpha</code>",
      "3. <b>zebra</b> <code>C:/dev/zebra</code>",
      "/attach <n> or /attach <folder>",
    ].join("\n");
    expect(sent[0].html).toBe(expected);

    // /attach by folder name
    await router.handleCommand("/attach zebra", DM);
    expect(router.boundSession(DM)).toBe("s1");

    // /attach by suffixed name
    await router.handleCommand("/attach alpha (2)", DM);
    expect(router.boundSession(DM)).toBe("s3");
  });

  test("/sessions groups sessions by workspace without headers or excerpts", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/demo", "Demo"), summary("sess-b", "C:/dev/other", "Other")]);
    const router = buildRouter({}, fake.control);
    await router.deliver(DM, "start");

    expect(await router.handleCommand("/sessions", DM)).toBe(true);
    expect(sent[0].html).toBe(
      "1. <b>demo</b> <code>C:/dev/demo</code>\n" +
      "2. <b>other</b> <code>C:/dev/other</code>\n" +
      "/attach <n> or /attach <folder>"
    );
    expect(sent[0].html).not.toContain("sess-a");
    expect(sent[0].html).not.toContain("Demo");
  });

  test("/attach binds to an existing session by id prefix and loads its history", async () => {
    const fake = fakeControl([summary("sess-abcdef", "C:/dev/other", "Other")]);
    const router = buildRouter({}, fake.control);

    await router.handleCommand("/attach sess-abc", DM);
    expect(router.boundSession(DM)).toBe("sess-abcdef");
    expect(fake.loaded).toEqual(["sess-abcdef"]);
    expect(store.getRoute("slot-1", CHAT)?.workspace).toBe("C:/dev/other");
  });

  test("/attach on an unknown id changes nothing", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);

    await router.handleCommand("/attach nope", DM);
    expect(sent[0].html).toContain("No running session matches");
    expect(router.boundSession(DM)).toBeNull();
  });

  test("/new starts a fresh session even when one already serves the workspace", async () => {
    const fake = fakeControl([summary("sess-existing", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);

    await router.handleCommand("/new", DM);
    expect(fake.created).toEqual([{ workspace: "C:/dev/demo", title: "Telegram slot-1" }]);
    expect(router.boundSession(DM)).toBe("sess-new-1");
  });

  test("/detach drops the binding without touching the session", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);
    await router.deliver(DM, "start");

    await router.handleCommand("/detach", DM);
    expect(router.boundSession(DM)).toBeNull();
    expect(fake.aborted).toEqual([]);
    expect(sent.at(-1)?.html).toContain("Chat detached");

    await router.handleCommand("/detach", DM);
    expect(sent.at(-1)?.html).toContain("not routed");
  });

  test("/where reports the route, and an unreachable host is reported instead of thrown", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);
    expect(await router.handleCommand("/where", DM)).toBe(true);
    expect(sent[0].html).toContain("not routed yet");

    const broken = buildRouter({}, fakeControl([], { unavailable: true }).control);
    expect(await broken.handleCommand("/sessions", DM)).toBe(true);
    expect(sent.at(-1)?.html).toContain("not reachable");
  });

  test("getDaemonCommands and renderTelegramHelp provide full routing command discovery in daemon mode", () => {
    const commands = getDaemonCommands();
    const names = commands.map(c => c.command);
    expect(names).toContain("app");
    expect(names).toContain("sessions");
    expect(names).toContain("attach");
    expect(names).toContain("new");
    expect(names).toContain("detach");
    expect(names).toContain("where");
    expect(names).toContain("status");
    expect(names).toContain("reload");
    expect(names).toContain("topics");

    const help = renderTelegramHelp({ isDaemon: true });
    expect(help).toContain("Routing & Workspaces");
    expect(help).toContain("/topics");
    expect(help).toContain("/sessions");
    expect(help).toContain("/attach &lt;id&gt;");
    expect(help).toContain("/new &lt;path&gt;");
    expect(help).toContain("/where");
    expect(help).toContain("/detach");
    expect(help).toContain("/app");
    expect(help).toContain("/reload");
  });
});
