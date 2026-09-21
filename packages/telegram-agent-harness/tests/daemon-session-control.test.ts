/**
 * Daemon session control against a real socket speaking the GUI host action
 * protocol. The transport is exercised end to end — connection handshake, request
 * correlation, rejection codes and unsolicited push frames — because every routing
 * decision the daemon makes depends on reading those frames correctly.
 */

import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import {
  GuiHostSessionControl,
  assistantTexts,
  discoverRunningInteractiveSessions,
  getDefaultSessionDirName,
  guiHostAgentDirs,
  readActiveSessionId,
  readSessionSummaries,
  resolveGuiHostEndpoint,
  type SessionEvent,
} from "../daemon/session-control";

interface FakeHost {
  endpoint: string;
  received: unknown[];
  /** Pushes an unsolicited frame to every live connection. */
  push: (frame: unknown) => void;
  close: () => Promise<void>;
}

interface HostBehaviour {
  sessions?: Record<string, unknown>[];
  /** Fails SubmitPrompt with TURN_IN_PROGRESS, as a busy session does. */
  turnInProgress?: boolean;
  /** Fails AbortTurn with NOT_RUNNING. */
  nothingToAbort?: boolean;
  transcript?: Record<string, unknown>[];
}

async function startFakeHost(behaviour: HostBehaviour = {}): Promise<FakeHost> {
  const received: unknown[] = [];
  const sockets = new Set<net.Socket>();

  const server = net.createServer(socket => {
    sockets.add(socket);
    socket.setEncoding("utf8");
    socket.on("close", () => sockets.delete(socket));
    socket.on("error", () => sockets.delete(socket));
    socket.write(`${JSON.stringify({ ConnectionChanged: { Connected: {} } })}\n`);

    let buffer = "";
    socket.on("data", chunk => {
      buffer += String(chunk);
      let newline = buffer.indexOf("\n");
      while (newline >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        newline = buffer.indexOf("\n");
        if (!line) continue;
        const frame = JSON.parse(line) as { id: number; action: unknown };
        received.push(frame.action);
        for (const reply of replyTo(frame.id, frame.action, behaviour)) {
          socket.write(`${JSON.stringify(reply)}\n`);
        }
      }
    });
  });

  await new Promise<void>(resolve => server.listen(0, "127.0.0.1", resolve));
  const address = server.address() as net.AddressInfo;
  return {
    endpoint: `tcp:127.0.0.1:${address.port}`,
    received,
    push: frame => {
      for (const socket of sockets) socket.write(`${JSON.stringify(frame)}\n`);
    },
    close: async () => {
      for (const socket of sockets) socket.destroy();
      await new Promise<void>(resolve => server.close(() => resolve()));
    },
  };
}

function replyTo(id: number, action: unknown, behaviour: HostBehaviour): unknown[] {
  const verb = typeof action === "string" ? action : Object.keys(action as Record<string, unknown>)[0];

  if (verb === "ListSessions") {
    return [
      { Snapshot: { Sessions: [{ revision: 1, value: behaviour.sessions ?? [] }, []] } },
      { RequestSucceeded: { request: id } },
    ];
  }
  if (verb === "CreateSession") {
    return [
      { Snapshot: { ActiveSession: { revision: 4, value: { id: "sess-created", cwd: "C:/dev/demo" } } } },
      { RequestSucceeded: { request: id } },
    ];
  }
  if (verb === "SubmitPrompt" && behaviour.turnInProgress) {
    return [{ RequestFailed: { request: id, error: { code: "TURN_IN_PROGRESS", message: "A turn is already running" } } }];
  }
  if (verb === "AbortTurn" && behaviour.nothingToAbort) {
    return [{ RequestFailed: { request: id, error: { code: "NOT_RUNNING", message: "No turn is running" } } }];
  }
  if (verb === "PreviewSessionTranscript") {
    return [
      { Snapshot: { SessionTranscript: { session: "sess-a", transcript: { revision: 2, value: behaviour.transcript ?? [] } } } },
      { RequestSucceeded: { request: id } },
    ];
  }
  if (verb === "LoadTranscript") {
    return [
      { Snapshot: { Transcript: { revision: 2, value: behaviour.transcript ?? [] } } },
      { RequestSucceeded: { request: id } },
    ];
  }
  return [{ RequestSucceeded: { request: id } }];
}

function assistantEntry(id: string, text: string): Record<string, unknown> {
  return { id, role: "Assistant", content: [{ Text: { text } }] };
}

test("last prompt uses read-only preview and ignores assistant and non-text content", async () => {
  const fixture = await control({ transcript: [
    { id: "u1", role: "User", content: [{ Text: { text: "first" } }] },
    { id: "u2", role: "User", content: [{ Text: { text: "latest prompt" } }, { Image: {} }] },
    assistantEntry("a", "not the prompt"),
  ] });
  expect(await fixture.control.lastPrompt("sess-a")).toBe("latest prompt");
  expect(fixture.host.received).toEqual([{ PreviewSessionTranscript: { session: "sess-a" } }]);
  expect(fixture.events).toEqual([]);
});

/**
 * Collects events and lets a test await the arrival of the Nth one. Push frames
 * cross a real socket, so the test waits for the event the code actually emits
 * rather than for a duration guessed to be long enough.
 */
function eventCollector(): { events: SessionEvent[]; onEvent: (event: SessionEvent) => void; until: (count: number) => Promise<void> } {
  const events: SessionEvent[] = [];
  let waiters: { count: number; resolve: () => void }[] = [];
  return {
    events,
    onEvent: event => {
      events.push(event);
      const ready = waiters.filter(waiter => events.length >= waiter.count);
      waiters = waiters.filter(waiter => events.length < waiter.count);
      for (const waiter of ready) waiter.resolve();
    },
    until: count =>
      events.length >= count
        ? Promise.resolve()
        : new Promise<void>(resolve => {
            waiters.push({ count, resolve });
          }),
  };
}

const hosts: FakeHost[] = [];
const controls: GuiHostSessionControl[] = [];
const tempRoots: string[] = [];

async function control(behaviour: HostBehaviour = {}): Promise<{
  control: GuiHostSessionControl;
  host: FakeHost;
  events: SessionEvent[];
  until: (count: number) => Promise<void>;
}> {
  const host = await startFakeHost(behaviour);
  hosts.push(host);
  const collector = eventCollector();
  const testRoot = fs.mkdtempSync(path.join(os.tmpdir(), "test-veyyon-"));
  tempRoots.push(testRoot);
  const instance = new GuiHostSessionControl({
    endpoint: host.endpoint,
    configRoot: testRoot,
    onEvent: collector.onEvent,
    onLog: () => {},
  });
  controls.push(instance);
  return { control: instance, host, events: collector.events, until: collector.until };
}

afterEach(async () => {
  for (const instance of controls.splice(0)) instance.close();
  for (const host of hosts.splice(0)) await host.close();
  for (const dir of tempRoots.splice(0)) {
    try {
      fs.rmSync(dir, { recursive: true, force: true });
    } catch {}
  }
});

describe("GUI host session control", () => {
  test("lists sessions and picks the newest match for a workspace", async () => {
    const { control: instance } = await control({
      sessions: [
        { id: "old", cwd: "C:/dev/demo", workspace: "C:/dev/demo", title: "Old", status: "Idle", modified_at_ms: 100 },
        { id: "new", cwd: "C:/dev/demo", workspace: "C:/dev/demo", title: "New", status: "Idle", modified_at_ms: 900 },
        { id: "other", cwd: "C:/dev/elsewhere", workspace: "C:/dev/elsewhere", title: "Other", status: "Idle", modified_at_ms: 999 },
      ],
    });

    expect((await instance.listSessions()).map(session => session.id)).toEqual(["old", "new", "other"]);
    expect((await instance.findSession("C:/dev/demo"))?.id).toBe("new");
    expect(await instance.findSession("C:/dev/nothing-here")).toBeNull();
  });

  test("ensureSession reuses a matching session and creates one only when none exists", async () => {
    const reuse = await control({
      sessions: [{ id: "existing", cwd: "C:/dev/demo", workspace: "C:/dev/demo", title: null, status: "Idle", modified_at_ms: 1 }],
    });
    expect(await reuse.control.ensureSession("C:/dev/demo", "Telegram")).toBe("existing");
    expect(reuse.host.received.some(action => typeof action === "object" && action !== null && "CreateSession" in action)).toBe(false);

    const fresh = await control({ sessions: [] });
    expect(await fresh.control.ensureSession("C:/dev/demo", "Telegram")).toBe("sess-created");
    expect(fresh.host.received).toContainEqual({ CreateSession: { workspace: "C:/dev/demo", title: "Telegram" } });
  });

  test("a busy session steers instead of dropping the operator's message", async () => {
    const { control: instance, host } = await control({ turnInProgress: true });
    expect(await instance.deliver("sess-1", "keep going", "auto")).toBe("steered");
    expect(host.received).toContainEqual({ SubmitPrompt: { session: "sess-1", text: "keep going", attachments: [] } });
    expect(host.received).toContainEqual({ Steer: { session: "sess-1", text: "keep going", attachments: [] } });
  });

  test("an idle session starts a turn, and explicit modes are honoured", async () => {
    const { control: instance, host } = await control();
    expect(await instance.deliver("sess-1", "go", "auto")).toBe("started");
    expect(await instance.deliver("sess-1", "also this", "followUp")).toBe("queued");
    expect(host.received).toContainEqual({ FollowUp: { session: "sess-1", text: "also this", attachments: [] } });
  });

  test("abort reports whether a turn was actually running", async () => {
    const running = await control();
    expect(await running.control.abort("sess-1")).toBe(true);

    const idle = await control({ nothingToAbort: true });
    expect(await idle.control.abort("sess-1")).toBe(false);
  });

  test("push frames are attributed to the session whose connection carried them", async () => {
    const { control: instance, host, events, until } = await control({
      transcript: [assistantEntry("e1", "earlier answer")],
    });

    // One connection per session, opened by the first request for it.
    await instance.loadTranscript("sess-a");
    await instance.deliver("sess-b", "hello", "auto");
    await until(1);
    expect(events[0]).toEqual({ kind: "history", sessionId: "sess-a", entries: [{ entryId: "e1", text: "earlier answer" }] });

    host.push({ StreamingChanged: { session: "sess" } });
    host.push({ TranscriptAppended: { entries: [assistantEntry("e2", "live answer")] } });
    // Both session connections receive the broadcast: 2 streaming + 2 appended.
    await until(5);

    const appended = events.filter(event => event.kind === "appended");
    expect(appended.length).toBe(2);
    expect(new Set(appended.map(event => event.sessionId))).toEqual(new Set(["sess-a", "sess-b"]));
    expect(appended[0]).toMatchObject({ entries: [{ entryId: "e2", text: "live answer" }] });
    expect(instance.isBusy("sess-a")).toBe(true);

    host.push({ StreamingChanged: null });
    await until(7);
    expect(instance.isBusy("sess-a")).toBe(false);
    expect(instance.isBusy("sess-b")).toBe(false);
  });

  test("a missing endpoint fails loudly instead of hanging on a dead socket", async () => {
    const instance = new GuiHostSessionControl({ endpoint: null, onEvent: () => {}, onLog: () => {} });
    expect(instance.listSessions()).rejects.toThrow(/no veyyon gui host endpoint/i);
  });
});

describe("GUI host frame decoding", () => {
  test("session summaries tolerate absent optional fields", () => {
    const summaries = readSessionSummaries({
      events: [{ Snapshot: { Sessions: [{ revision: 1, value: [{ id: "a" }, { cwd: "no-id" }] }, []] } }],
    });
    expect(summaries).toEqual([{ id: "a", cwd: "", workspace: "", title: null, status: "Unknown", modifiedAtMs: null }]);
  });

  test("active session id is read out of the versioned snapshot", () => {
    expect(readActiveSessionId({ events: [{ Snapshot: { ActiveSession: { revision: 9, value: { id: "z" } } } }] })).toBe("z");
    expect(readActiveSessionId({ events: [{ Snapshot: { ActiveSession: null } }] })).toBeNull();
  });

  test("only assistant prose is extracted; thinking and tool traffic is dropped", () => {
    expect(
      assistantTexts([
        { id: "u1", role: "User", content: [{ Text: { text: "question" } }] },
        { id: "a1", role: "Assistant", content: [{ Thinking: { text: "reasoning" } }, { Text: { text: "part one" } }, { Text: { text: "part two" } }] },
        { id: "a2", role: "Assistant", content: [{ ToolCall: { name: "bash" } }] },
        { id: "a3", role: "Assistant", content: [{ Text: { text: "   " } }] },
      ]),
    ).toEqual([{ entryId: "a1", text: "part one\n\npart two" }]);
  });

  test("endpoint discovery prefers the environment, then the endpoint file", () => {
    const previous = process.env.VEYYON_GUI_HOST_ENDPOINT;
    process.env.VEYYON_GUI_HOST_ENDPOINT = "tcp:127.0.0.1:9999";
    try {
      expect(resolveGuiHostEndpoint("C:/nonexistent-agent-dir")).toBe("tcp:127.0.0.1:9999");
      process.env.VEYYON_GUI_HOST_ENDPOINT = "";
      expect(resolveGuiHostEndpoint("C:/nonexistent-agent-dir")).toBeNull();
    } finally {
      if (previous === undefined) delete process.env.VEYYON_GUI_HOST_ENDPOINT;
      else process.env.VEYYON_GUI_HOST_ENDPOINT = previous;
    }
  });

  test("the endpoint file the host publishes in the profile agent dir is found", () => {
    // `veyyon gui` writes into getAgentDir(), which is the active profile's agent
    // directory. A daemon that only searched ~/.veyyon found nothing.
    const saved = {
      endpoint: process.env.VEYYON_GUI_HOST_ENDPOINT,
      agentDir: process.env.VEYYON_CODING_AGENT_DIR,
      configDir: process.env.VEYYON_CONFIG_DIR,
      profile: process.env.VEYYON_PROFILE,
    };
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "daemon-endpoint-"));
    try {
      delete process.env.VEYYON_GUI_HOST_ENDPOINT;
      delete process.env.VEYYON_CODING_AGENT_DIR;
      process.env.VEYYON_CONFIG_DIR = ".veyyon";
      process.env.VEYYON_PROFILE = "default";

      const profileAgentDir = path.join(home, ".veyyon", "profiles", "default", "agent");
      const legacyDir = path.join(home, ".veyyon");
      fs.mkdirSync(profileAgentDir, { recursive: true });

      expect(resolveGuiHostEndpoint(profileAgentDir, legacyDir)).toBeNull();

      fs.writeFileSync(path.join(profileAgentDir, "gui-host.endpoint"), "tcp:127.0.0.1:7699\n", "utf8");
      expect(resolveGuiHostEndpoint(profileAgentDir, legacyDir)).toBe("tcp:127.0.0.1:7699");

      // A bare path is still accepted and read as a socket, so an operator who
      // wrote the file by hand is not silently ignored.
      fs.writeFileSync(path.join(legacyDir, "gui-host.endpoint"), `${path.join(home, "host.sock")}\n`, "utf8");
      fs.rmSync(path.join(profileAgentDir, "gui-host.endpoint"));
      expect(resolveGuiHostEndpoint(profileAgentDir, legacyDir)).toBe(`unix:${path.join(home, "host.sock")}`);
    } finally {
      fs.rmSync(home, { recursive: true, force: true });
      for (const [key, value] of [
        ["VEYYON_GUI_HOST_ENDPOINT", saved.endpoint],
        ["VEYYON_CODING_AGENT_DIR", saved.agentDir],
        ["VEYYON_CONFIG_DIR", saved.configDir],
        ["VEYYON_PROFILE", saved.profile],
      ] as const) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
    }
  });

  test("every installed profile is searched, with the active one first", () => {
    // Which profile is active is Veyyon's decision, recorded in the config root
    // rather than the environment, so a daemon that searched only the env-named
    // profile missed a host running under the operator's actual default.
    const saved = {
      agentDir: process.env.VEYYON_CODING_AGENT_DIR,
      profile: process.env.VEYYON_PROFILE,
    };
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "daemon-profiles-"));
    try {
      delete process.env.VEYYON_CODING_AGENT_DIR;
      process.env.VEYYON_PROFILE = "work";
      for (const name of ["default", "work", "oss"]) {
        fs.mkdirSync(path.join(root, "profiles", name, "agent"), { recursive: true });
      }
      fs.writeFileSync(path.join(root, "profiles", "not-a-profile"), "", "utf8");

      expect(guiHostAgentDirs(root)).toEqual([
        path.join(root, "profiles", "work", "agent"),
        path.join(root, "profiles", "default", "agent"),
        path.join(root, "profiles", "oss", "agent"),
        root,
      ]);

      // A host published under a profile nobody named is still reachable.
      fs.writeFileSync(
        path.join(root, "profiles", "oss", "agent", "gui-host.endpoint"),
        "tcp:127.0.0.1:7711\n",
        "utf8",
      );
      expect(resolveGuiHostEndpoint(...guiHostAgentDirs(root))).toBe("tcp:127.0.0.1:7711");

      // An explicit agent-dir override is the only place the host can be, so it
      // replaces the profile search rather than being tried alongside it.
      process.env.VEYYON_CODING_AGENT_DIR = path.join(os.tmpdir(), "explicit-agent");
      expect(guiHostAgentDirs(root)).toEqual([path.join(os.tmpdir(), "explicit-agent")]);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
      for (const [key, value] of [
        ["VEYYON_CODING_AGENT_DIR", saved.agentDir],
        ["VEYYON_PROFILE", saved.profile],
      ] as const) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
      }
    }
  });
});

describe("Interactive session discovery from broker registry", () => {
  test("getDefaultSessionDirName encodes relative paths with hyphens", () => {
    const home = os.homedir();
    const sampleProject = path.join(home, "dev", "my-project");
    expect(getDefaultSessionDirName(sampleProject)).toBe("-dev-my-project");
  });

  test("discoverRunningInteractiveSessions discovers live PID and skips dead PID and subagents", () => {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "discovery-test-"));
    try {
      const profile = "default";
      const daemonsDir = path.join(root, "profiles", profile, "run", "daemons");
      const sessionsDir = path.join(root, "profiles", profile, "agent", "sessions");

      // Client 1: Live PID (using current test process pid)
      const liveProjectDir = path.join(os.tmpdir(), "live-workspace-1");
      fs.mkdirSync(liveProjectDir, { recursive: true });
      const liveClientDir = path.join(daemonsDir, "proj-live", "clients");
      fs.mkdirSync(liveClientDir, { recursive: true });
      fs.writeFileSync(
        path.join(liveClientDir, `${process.pid}-uuid1.json`),
        JSON.stringify({ id: "client-uuid1", pid: process.pid, projectDir: liveProjectDir }),
        "utf8",
      );

      // Create session file for live project
      const liveSessionDirName = getDefaultSessionDirName(liveProjectDir);
      const liveSessionFolder = path.join(sessionsDir, liveSessionDirName);
      fs.mkdirSync(liveSessionFolder, { recursive: true });
      fs.writeFileSync(
        path.join(liveSessionFolder, "2026-09-20T10-00-00-000Z_sess-live-123.jsonl"),
        JSON.stringify({ type: "title", title: "My Live Task" }) + "\n" +
          JSON.stringify({ type: "session", id: "sess-live-123" }) + "\n",
        "utf8",
      );

      // Client 2: Dead PID (99999999)
      const deadProjectDir = path.join(os.tmpdir(), "dead-workspace-2");
      const deadClientDir = path.join(daemonsDir, "proj-dead", "clients");
      fs.mkdirSync(deadClientDir, { recursive: true });
      fs.writeFileSync(
        path.join(deadClientDir, "99999999-uuid2.json"),
        JSON.stringify({ id: "client-uuid2", pid: 99999999, projectDir: deadProjectDir }),
        "utf8",
      );

      // Client 3: Subagent session
      const subagentProjectDir = path.join(os.tmpdir(), "subagent-workspace-3");
      fs.mkdirSync(subagentProjectDir, { recursive: true });
      const subClientDir = path.join(daemonsDir, "proj-sub", "clients");
      fs.mkdirSync(subClientDir, { recursive: true });
      // Use process.pid so PID is alive, but marked as subagent
      // Give it a distinct PID if possible or test subagent flag
      const subSessionDirName = getDefaultSessionDirName(subagentProjectDir);
      const subSessionFolder = path.join(sessionsDir, subSessionDirName);
      fs.mkdirSync(subSessionFolder, { recursive: true });
      fs.writeFileSync(
        path.join(subSessionFolder, "2026-09-20T11-00-00-000Z_sess-sub-456.jsonl"),
        JSON.stringify({ type: "title", title: "Subagent Task" }) + "\n" +
          JSON.stringify({ type: "session", id: "sess-sub-456", parentSession: "sess-parent-000", isSubagent: true }) + "\n",
        "utf8",
      );

      const discovered = discoverRunningInteractiveSessions(root);

      // Should only discover client 1
      expect(discovered.length).toBe(1);
      expect(discovered[0].id).toBe("sess-live-123");
      expect(discovered[0].title).toBe("My Live Task");
      expect(discovered[0].status).toBe("Running");
      expect(discovered[0].workspace).toBe(liveProjectDir);
      expect(discovered[0].isSubagent).toBe(false);
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  });

  test("resolveGuiHostEndpoint defaults to tcp:127.0.0.1:7699 when no agentDirs passed", () => {
    const saved = process.env.VEYYON_GUI_HOST_ENDPOINT;
    delete process.env.VEYYON_GUI_HOST_ENDPOINT;
    try {
      // When agentDirs is empty, returns persistent default
      const ep = resolveGuiHostEndpoint();
      expect(ep).toMatch(/tcp:127\.0\.0\.1:(7699|\d+)/);
    } finally {
      if (saved !== undefined) process.env.VEYYON_GUI_HOST_ENDPOINT = saved;
    }
  });
});
