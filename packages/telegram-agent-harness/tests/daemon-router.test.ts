/**
 * Per-slot routing: which session a chat talks to, what gets sent back to it, and
 * what must never be sent back (a transcript the chat already saw).
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
import { SlotRouter, getDaemonCommands } from "../daemon/router";
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

const CHAT = "1247617658";
let root: string;
let store: DaemonStore;
let sent: { chatId: string; html: string }[];
let relayed: { chatId: string; markdown: string }[];

function buildRouter(slot: Partial<DaemonSlot>, control: GuiHostSessionControl): SlotRouter {
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
    send: async (chatId, html) => {
      sent.push({ chatId, html });
    },
    relay: async (chatId, markdown) => {
      relayed.push({ chatId, markdown });
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

    const ack = await router.deliver(CHAT, "first message");
    expect(fake.created).toEqual([{ workspace: "C:/dev/demo", title: "Telegram slot-1" }]);
    expect(ack).toContain("sess-new-1");
    expect(router.boundSession(CHAT)).toBe("sess-new-1");

    // Second message reuses the binding rather than creating another session.
    expect(await router.deliver(CHAT, "second message")).toBeNull();
    expect(fake.created.length).toBe(1);
    expect(fake.delivered).toEqual([
      { sessionId: "sess-new-1", text: "first message", mode: "auto" },
      { sessionId: "sess-new-1", text: "second message", mode: "auto" },
    ]);
  });

  test("an existing session for the workspace is reused instead of starting a second one", async () => {
    const fake = fakeControl([summary("sess-existing", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);

    await router.deliver(CHAT, "hello");
    expect(fake.created).toEqual([]);
    expect(router.boundSession(CHAT)).toBe("sess-existing");
  });

  test("a slot with no resolvable workspace refuses to invent one", async () => {
    const fake = fakeControl();
    const router = buildRouter({ workspace: null }, fake.control);

    const ack = await router.deliver(CHAT, "hello");
    expect(ack).toContain("No workspace is configured");
    expect(fake.created).toEqual([]);
    expect(fake.delivered).toEqual([]);
    expect(router.boundSession(CHAT)).toBeNull();
  });

  test("steer and follow-up modes are acknowledged distinctly and reach the bound session", async () => {
    const fake = fakeControl([summary("sess-existing", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);
    await router.deliver(CHAT, "start");

    expect(await router.deliver(CHAT, "redirect", "steer")).toContain("steer");
    expect(await router.deliver(CHAT, "afterwards", "followUp")).toContain("follow-up");
    expect(fake.delivered.slice(1)).toEqual([
      { sessionId: "sess-existing", text: "redirect", mode: "steer" },
      { sessionId: "sess-existing", text: "afterwards", mode: "followUp" },
    ]);
  });

  test("busy state and abort follow the chat's binding, not the slot", async () => {
    const fake = fakeControl([summary("sess-existing", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);

    expect(router.isBusy(CHAT)).toBe(false);
    expect(await router.abort(CHAT)).toBe(false);
    expect(fake.aborted).toEqual([]);

    await router.deliver(CHAT, "start");
    fake.busy.add("sess-existing");
    expect(router.isBusy(CHAT)).toBe(true);
    expect(await router.abort(CHAT)).toBe(true);
    expect(fake.aborted).toEqual(["sess-existing"]);
  });
});

describe("outbound delivery", () => {
  test("live output reaches the bound chat exactly once", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);
    await router.deliver(CHAT, "start");

    const event = { kind: "appended" as const, sessionId: "sess-a", entries: [{ entryId: "e1", text: "the answer" }] };
    await router.onSessionEvent(event);
    await router.onSessionEvent(event);

    expect(relayed).toEqual([{ chatId: CHAT, markdown: "the answer" }]);
  });

  test("binding a chat marks existing history delivered, so nothing is replayed", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);
    await router.deliver(CHAT, "start");
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
    expect(relayed).toEqual([{ chatId: CHAT, markdown: "brand new" }]);
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
    await router.deliver(CHAT, "start");

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
    expect(await router.handleCommand("/status", CHAT)).toBe(false);
    expect(sent).toEqual([]);
  });

  test("/sessions lists running sessions and marks the bound one", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/demo", "Demo"), summary("sess-b", "C:/dev/other", "Other")]);
    const router = buildRouter({}, fake.control);
    await router.deliver(CHAT, "start");

    expect(await router.handleCommand("/sessions", CHAT)).toBe(true);
    expect(sent[0].html).toContain("➡️ <code>sess-a</code>");
    expect(sent[0].html).toContain("• <code>sess-b</code>");
  });

  test("/attach binds to an existing session by id prefix and loads its history", async () => {
    const fake = fakeControl([summary("sess-abcdef", "C:/dev/other", "Other")]);
    const router = buildRouter({}, fake.control);

    await router.handleCommand("/attach sess-abc", CHAT);
    expect(router.boundSession(CHAT)).toBe("sess-abcdef");
    expect(fake.loaded).toEqual(["sess-abcdef"]);
    expect(store.getRoute("slot-1", CHAT)?.workspace).toBe("C:/dev/other");
  });

  test("/attach on an unknown id changes nothing", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);

    await router.handleCommand("/attach nope", CHAT);
    expect(sent[0].html).toContain("No running session matches");
    expect(router.boundSession(CHAT)).toBeNull();
  });

  test("/new starts a fresh session even when one already serves the workspace", async () => {
    const fake = fakeControl([summary("sess-existing", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);

    await router.handleCommand("/new", CHAT);
    expect(fake.created).toEqual([{ workspace: "C:/dev/demo", title: "Telegram slot-1" }]);
    expect(router.boundSession(CHAT)).toBe("sess-new-1");
  });

  test("/detach drops the binding without touching the session", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);
    await router.deliver(CHAT, "start");

    await router.handleCommand("/detach", CHAT);
    expect(router.boundSession(CHAT)).toBeNull();
    expect(fake.aborted).toEqual([]);
    expect(sent.at(-1)?.html).toContain("Chat detached");

    await router.handleCommand("/detach", CHAT);
    expect(sent.at(-1)?.html).toContain("not routed");
  });

  test("/where reports the route, and an unreachable host is reported instead of thrown", async () => {
    const fake = fakeControl([summary("sess-a", "C:/dev/demo")]);
    const router = buildRouter({}, fake.control);
    expect(await router.handleCommand("/where", CHAT)).toBe(true);
    expect(sent[0].html).toContain("not routed yet");

    const broken = buildRouter({}, fakeControl([], { unavailable: true }).control);
    expect(await broken.handleCommand("/sessions", CHAT)).toBe(true);
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

    const help = renderTelegramHelp({ isDaemon: true });
    expect(help).toContain("Routing & Workspaces");
    expect(help).toContain("/sessions");
    expect(help).toContain("/attach &lt;id&gt;");
    expect(help).toContain("/new &lt;path&gt;");
    expect(help).toContain("/where");
    expect(help).toContain("/detach");
    expect(help).toContain("/app");
    expect(help).toContain("/reload");
  });
});
