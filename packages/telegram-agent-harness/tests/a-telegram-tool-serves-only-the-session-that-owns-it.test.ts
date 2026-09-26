/**
 * WHY: the operator tools (`telegram_question`, `telegram_message`,
 * `telegram_dashboard`) are registered by the thin loader, which survives every hot
 * reload, while the runtime holding the Telegram channel is replaced by each one. A
 * handler therefore cannot capture the active root at registration time. The defect
 * this closes is a retained handler reaching into a process-global root owned by a
 * DIFFERENT runtime instance — a lease hijack that posts one session's questions, lane
 * messages and dashboard into another session's chat.
 *
 * Class closed: every tool the loader registers, swept from the host registry rather
 * than named here, must resolve its channel through the current runtime on each call
 * and refuse when the root is absent or foreign. A new tool added to the loader turns
 * the registry test red until its call shape is recorded below.
 *
 * Not covered: zod parameter validation. This package does not depend on zod — the host
 * injects it — so `pi.zod` is an inert stub here and nothing asserts on the schemas.
 */

import { afterEach, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@veyyon/coding-agent";
import telegramSessionExtension, {
  ACTIVE_ROOT_SYMBOL,
  type ActiveRootState,
  type GlobalTelegramState,
  registerOperatorTools,
  setActiveRuntime,
} from "../extension/index";
import { TelegramRuntime } from "../extension/runtime";
import { TelegramPoller } from "../extension/poller";
import { MessageContextStore } from "../src/message-context";
import { LiveDashboard } from "../src/live-dashboard";
import { OperatorQuestionService } from "../src/operator-questions";
import type { MessageCorrelationBridge, OutboundMessageCorrelation } from "../extension/types";

interface ToolUpdate {
  content: Array<{ type: string; text: string }>;
}

interface RegisteredTool {
  name: string;
  label: string;
  description: string;
  parameters: unknown;
  execute: (
    toolCallId: string,
    params: Record<string, unknown>,
    signal?: AbortSignal,
    onUpdate?: (update: ToolUpdate) => void,
  ) => Promise<{ content: Array<{ type: string; text: string }>; details?: unknown }>;
}

/**
 * The one valid call per registered tool. Pinned by exact equality against the host
 * registry, so a tool added to the loader fails here until its shape is recorded.
 */
const CALLS: Record<string, Record<string, unknown>> = {
  telegram_question: {
    action: "ask",
    question: "Merge the fork sync now?",
    recommendation: "merge",
    options: [
      { id: "merge", label: "Merge now" },
      { id: "hold", label: "Hold for review" },
    ],
    wait: false,
  },
  telegram_message: { text: "Lane finished its slice.", lane_id: "worker-auth", lane_state: "active" },
  telegram_dashboard: { lanes: [], blockers: [], mergeQueue: [] },
};

const originalFetch = globalThis.fetch;
const cleanup: Array<() => void> = [];

afterEach(() => {
  globalThis.fetch = originalFetch;
  setActiveRuntime(null);
  delete (globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL];
  for (const close of cleanup.splice(0)) close();
});

/**
 * Chainable no-op stand-in for the host's injected zod. Every factory and modifier
 * returns the same node so `z.enum([...]).default("ask")` and
 * `z.array(z.object({...}))` author without a zod dependency.
 */
function inertSchemaModule(): unknown {
  const node: Record<string, unknown> = {};
  for (const key of ["object", "string", "enum", "array", "boolean", "number", "optional", "default"]) {
    node[key] = () => node;
  }
  return node;
}

function createHost(options: { toolSurface?: boolean } = {}): {
  api: ExtensionAPI;
  tools: Map<string, RegisteredTool>;
  events: Set<string>;
  warnings: string[];
} {
  const tools = new Map<string, RegisteredTool>();
  const events = new Set<string>();
  const warnings: string[] = [];

  const base = {
    setLabel: () => {},
    on: (event: string) => {
      events.add(event);
    },
    registerCommand: () => {},
    sendUserMessage: () => {},
    abortActiveTurn: async () => {},
    logger: {
      info: () => {},
      warn: (message: string) => warnings.push(message),
      error: () => {},
      debug: () => {},
    },
  };

  const surface =
    options.toolSurface === false
      ? {}
      : {
          zod: inertSchemaModule(),
          registerTool: (tool: unknown) => {
            const registered = tool as RegisteredTool;
            tools.set(registered.name, registered);
          },
        };

  return { api: { ...base, ...surface } as unknown as ExtensionAPI, tools, events, warnings };
}

/**
 * A live channel: real poller over a stubbed Telegram API, real question, dashboard and
 * lane-provenance services, so a tool that reaches its root produces observable effects
 * (an HTTP send, a stored snapshot, a lane row) rather than a spy call.
 */
function createChannel(): {
  root: (instanceId: string) => ActiveRootState;
  calls: Array<{ method: string; body: Record<string, unknown> }>;
  correlations: Map<number, OutboundMessageCorrelation>;
  poller: TelegramPoller;
  store: MessageContextStore;
  laneStateOf: (messageId: number) => string | undefined;
  failSends: () => void;
} {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-tool-ownership-"));
  const calls: Array<{ method: string; body: Record<string, unknown> }> = [];
  const correlations = new Map<number, OutboundMessageCorrelation>();
  let messageCounter = 700;
  let sendsFail = false;

  const poolPath = path.join(dir, "pool.db");
  const initDb = new Database(poolPath);
  initDb.run(
    "CREATE TABLE message_correlations(bot_id TEXT, chat_id TEXT, message_id INTEGER, session_id TEXT, PRIMARY KEY (bot_id, chat_id, message_id))",
  );
  initDb.close();
  const store = new MessageContextStore(poolPath);
  // Opened after the store, whose constructor adds the lane provenance columns. Named
  // columns, because a positional insert would break against the widened table.
  const rows = new Database(poolPath);

  const bridge: MessageCorrelationBridge = {
    getSessionId: () => "owning-session",
    getSlotId: () => "slot-test",
    record: row => {
      correlations.set(row.messageId, { ...row });
      rows.run(
        "INSERT OR IGNORE INTO message_correlations(bot_id, chat_id, message_id, session_id) VALUES (?, ?, ?, ?)",
        [row.botId, row.chatId, row.messageId, "owning-session"],
      );
      store.record(row.botId, row.chatId, row.messageId, row);
    },
    resolveReply: () => ({ decision: "reject_unknown", detail: "Unused here" }),
    resolveCallback: () => ({ decision: "reject_unknown", detail: "Unused here" }),
    consumeCallback: () => false,
  };

  const poller = new TelegramPoller(
    "0:disposable-test",
    dir,
    { allowFrom: ["1"], dmPolicy: "allowlist" },
    {
      isIdle: () => true,
      onUserMessage: () => {},
      onFollowUp: () => {},
      onSteer: () => {},
      onAbort: () => {},
      onRelease: async () => {},
      getStatusText: () => "test",
      onLedgerFailure: () => {},
    },
    bridge,
    { outboundPaceMs: 0 },
  );

  globalThis.fetch = (async (url, init) => {
    const method = String(url).split("/").pop() ?? "";
    const body = init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
    calls.push({ method, body });
    if (sendsFail && method === "sendMessage") {
      return Response.json({ ok: false, description: "Bad Request: chat not found" }, { status: 400 });
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

  const route = { session_id: "owning-session", chat_id: "1", user_id: "1" };
  const questions = new OperatorQuestionService(
    poller,
    () => route,
    path.join(dir, "decisions.json"),
    poolPath,
    () => {},
    async (operation, payload) => {
      if (operation !== "register") return {};
      const input = payload as { question: string };
      return {
        question: {
          decision_id: "tq:1",
          question: input.question,
          status: "pending",
          answer: null,
          transport: { ...route, kind: "operator_question" as const, selection: null },
        },
        card: { id: "tq:1", text: input.question },
      };
    },
  );

  const dashboard = new LiveDashboard(poller, { run: async () => ({ stdout: "", stderr: "", exitCode: 1 }) }, () => "owning-session", () => {});

  cleanup.push(() => {
    poller.stop();
    questions.stop();
    dashboard.stop();
    rows.close();
    store.close();
    // The poller closes its own sqlite handle asynchronously, so Windows may still
    // hold the file for a moment after stop() is issued.
    fs.rmSync(dir, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
  });

  return {
    root: (instanceId: string) =>
      ({
        instanceId,
        sessionId: "owning-session",
        slotId: "slot-test",
        pi: {} as ExtensionAPI,
        poller,
        coordinator: {} as ActiveRootState["coordinator"],
        activeSlot: {} as ActiveRootState["activeSlot"],
        questions,
        messageContext: store,
        dashboard,
      }) satisfies ActiveRootState,
    calls,
    correlations,
    poller,
    store,
    laneStateOf: (messageId: number) => store.lookup("0", "1", messageId).laneState,
    failSends: () => {
      sendsFail = true;
    },
  };
}

function loadedTools(): Map<string, RegisteredTool> {
  const host = createHost();
  registerOperatorTools(host.api);
  return host.tools;
}

test("the loader registers exactly the operator tools this suite exercises", () => {
  const tools = loadedTools();
  expect([...tools.keys()].sort()).toEqual(Object.keys(CALLS).sort());
  for (const tool of tools.values()) {
    expect(tool.label.length).toBeGreaterThan(0);
    expect(typeof tool.execute).toBe("function");
  }
});

test("a host without the tool surface still attaches the channel and says the tools are missing", () => {
  const host = createHost({ toolSurface: false });
  telegramSessionExtension(host.api);

  expect(host.tools.size).toBe(0);
  expect(host.events.has("session_start")).toBe(true);
  expect(host.events.has("message_end")).toBe(true);
  expect(host.warnings.some(message => message.includes("Telegram operator tools not registered"))).toBe(true);
});

test("every operator tool refuses when no runtime is loaded", async () => {
  const tools = loadedTools();
  setActiveRuntime(null);
  const channel = createChannel();
  (globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL] = channel.root("orphaned-instance");

  for (const [name, params] of Object.entries(CALLS)) {
    const tool = tools.get(name)!;
    await expect(tool.execute("call-1", params)).rejects.toThrow();
  }
  expect(channel.calls).toEqual([]);
});

test("every operator tool refuses a root owned by another runtime instance", async () => {
  const host = createHost();
  registerOperatorTools(host.api);
  const runtime = new TelegramRuntime(host.api);
  setActiveRuntime(runtime);

  const channel = createChannel();
  (globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL] = channel.root("a-different-instance");

  for (const [name, params] of Object.entries(CALLS)) {
    const tool = host.tools.get(name)!;
    await expect(tool.execute("call-1", params)).rejects.toThrow();
  }
  // Nothing reached the foreign session's channel: no send, no pinned dashboard snapshot.
  expect(channel.calls).toEqual([]);
  expect(channel.poller.getMeta("dashboard-snapshot:owning-session")).toBeNull();
});

test("telegram_question publishes to the owned session and reports the pending state", async () => {
  const host = createHost();
  registerOperatorTools(host.api);
  const runtime = new TelegramRuntime(host.api);
  setActiveRuntime(runtime);
  const channel = createChannel();
  (globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL] = channel.root(runtime.instanceId);

  const updates: ToolUpdate[] = [];
  const result = await host.tools
    .get("telegram_question")!
    .execute("call-1", CALLS.telegram_question, undefined, update => updates.push(update));

  const sent = channel.calls.find(call => call.method === "sendMessage");
  expect(sent?.body.chat_id).toBe("1");
  expect(String(sent?.body.text)).toContain("Merge the fork sync now?");
  expect(updates[0]?.content[0]?.text).toContain("tq:1");
  expect(updates[0]?.content[0]?.text).toContain("pending");
  expect(JSON.parse(result.content[0]!.text).decision_id).toBe("tq:1");
});

test("telegram_question refuses when the owned root carries no question receiver", async () => {
  const host = createHost();
  registerOperatorTools(host.api);
  const runtime = new TelegramRuntime(host.api);
  setActiveRuntime(runtime);
  const channel = createChannel();
  const root = channel.root(runtime.instanceId);
  (globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL] = { ...root, questions: undefined };

  await expect(host.tools.get("telegram_question")!.execute("call-1", CALLS.telegram_question)).rejects.toThrow(
    /terminal question/,
  );
});

test("telegram_question rejects an ask without real labeled choices", async () => {
  const host = createHost();
  registerOperatorTools(host.api);
  const runtime = new TelegramRuntime(host.api);
  setActiveRuntime(runtime);
  const channel = createChannel();
  (globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL] = channel.root(runtime.instanceId);

  await expect(
    host.tools.get("telegram_question")!.execute("call-1", { action: "ask", question: "Proceed?", wait: false }),
  ).rejects.toThrow(/readable labels/);
  expect(channel.calls).toEqual([]);
});

test("telegram_message attributes the lane and records its state on the correlation row", async () => {
  const host = createHost();
  registerOperatorTools(host.api);
  const runtime = new TelegramRuntime(host.api);
  setActiveRuntime(runtime);
  const channel = createChannel();
  (globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL] = channel.root(runtime.instanceId);

  const result = await host.tools.get("telegram_message")!.execute("call-1", CALLS.telegram_message);

  const sent = channel.calls.find(call => call.method === "sendMessage");
  expect(String(sent?.body.text)).toContain("Agent · worker-auth");
  const [messageId] = [...channel.correlations.keys()];
  expect(channel.correlations.get(messageId!)?.laneId).toBe("worker-auth");
  expect(channel.laneStateOf(messageId!)).toBe("active");
  expect(result.content[0]!.text).toContain(String(messageId));
});

test("telegram_message reports failure when Telegram does not accept the message", async () => {
  const host = createHost();
  registerOperatorTools(host.api);
  const runtime = new TelegramRuntime(host.api);
  setActiveRuntime(runtime);
  const channel = createChannel();
  (globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL] = channel.root(runtime.instanceId);
  channel.failSends();

  await expect(host.tools.get("telegram_message")!.execute("call-1", CALLS.telegram_message)).rejects.toThrow(
    /not delivered/,
  );
});

test("telegram_dashboard stores the observed snapshot and reconciles each lane's state", async () => {
  const host = createHost();
  registerOperatorTools(host.api);
  const runtime = new TelegramRuntime(host.api);
  setActiveRuntime(runtime);
  const channel = createChannel();
  (globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL] = channel.root(runtime.instanceId);

  const active = await channel.poller.sendTelegramMessage("1", "still running", undefined, undefined, {
    laneId: "worker-auth",
    laneState: "active",
  });
  const gone = await channel.poller.sendTelegramMessage("1", "final report", undefined, undefined, {
    laneId: "worker-feed",
    laneState: "active",
  });

  await host.tools.get("telegram_dashboard")!.execute("call-1", {
    lanes: [
      { name: "worker-auth", task: "sync fork", state: "active" },
      { name: "worker-feed", task: "fix warmup", state: "exited" },
    ],
    blockers: [{ question: "Merge now?", url: "https://example.invalid/1" }],
    mergeQueue: [{ title: "PR 1", url: "https://example.invalid/pr/1", state: "open" }],
  });

  const stored = JSON.parse(channel.poller.getMeta("dashboard-snapshot:owning-session")!) as {
    lanes: Array<{ name: string; state: string }>;
    blockers: Array<{ question: string }>;
    observedAt: number;
  };
  expect(stored.lanes.map(lane => lane.state)).toEqual(["active", "exited"]);
  expect(stored.blockers[0]?.question).toBe("Merge now?");
  expect(stored.observedAt).toBeGreaterThan(0);

  expect(channel.laneStateOf(active!.result!.message_id)).toBe("active");
  expect(channel.laneStateOf(gone!.result!.message_id)).toBe("exited");
});
