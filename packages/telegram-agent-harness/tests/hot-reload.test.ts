import { test, expect, describe, beforeEach } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type {
  ExtensionAPI,
  ExtensionCommandContext,
  ExtensionContext,
} from "@veyyon/coding-agent";
import {
  getInstalledSourceSha,
  loadRuntimeModule,
  reload,
  setActiveRuntime,
  getActiveRuntime,
  setSavedContext,
} from "../extension/index";
import telegramSessionExtension from "../extension/index";
import {
  TelegramRuntime,
  type TelegramRuntimeOptions,
} from "../extension/runtime";
import type { DiscoveredSlot, AccessConfig } from "../extension/types";
import { handleInstalledCommand } from "../src/installed-commands";

interface MockCoordinator {
  acquireLease: (sessionId: string, cwd: string, pid: number) => Promise<{ ok: boolean; slot?: DiscoveredSlot; reason?: string }>;
  releaseLease: (slotId: string, sessionId: string, pid?: number) => boolean;
  close: () => void;
  readRawTokenForSlot: (stateDir: string) => string | null;
  readAccessConfig: (stateDir: string) => AccessConfig;
  recordOutboundMessage: (c: unknown) => void;
  resolveReplyRouting: () => { decision: "deliver" | "reject_unavailable" };
  validateDecisionCallback: () => { decision: "deliver" | "reject_unavailable" };
  consumeDecisionCallback: () => boolean;
  applyDecisionAnswer: () => Promise<boolean>;
  getPoolStatus: () => { totalSlots: number; freeSlots: number };
}

interface MockPoller {
  start: () => Promise<void>;
  stop: () => Promise<void>;
  getPrimaryChatId: () => string | null;
  sendTelegramMessage: (chatId: string, text: string) => Promise<{ ok: boolean; result?: { message_id: number } }>;
}

/**
 * Stands in for the host-supplied zod by recording the shape a builder call declared
 * rather than validating values: the extension only needs it to describe its tools, and
 * tool behaviour is exercised against the real services in operator-interface.test.ts.
 */
function createSchemaRecorder(): Record<string, (arg?: unknown) => Record<string, unknown>> {
  const leaf = (kind: string): Record<string, unknown> => {
    const node: Record<string, unknown> = { kind };
    node.optional = () => node;
    node.default = () => node;
    return node;
  };
  return {
    object: (shape?: unknown) => ({ ...leaf("object"), keys: Object.keys((shape ?? {}) as object) }),
    string: () => leaf("string"),
    number: () => leaf("number"),
    boolean: () => leaf("boolean"),
    enum: () => leaf("enum"),
    array: () => leaf("array"),
  };
}

function createMockExtensionAPI(): {
  api: ExtensionAPI;
  listeners: Map<string, ((...args: unknown[]) => unknown)[]>;
  commands: Map<string, { description: string; handler: (args: string, ctx: ExtensionCommandContext) => Promise<void> }>;
  tools: Map<string, { label: string; description: string; parameterKeys: string[] }>;
  notifications: { message: string; type?: string }[];
  userMessages: { text: string; options?: unknown }[];
  aborted: boolean;
} {
  const listeners = new Map<string, ((...args: unknown[]) => unknown)[]>();
  const commands = new Map<string, { description: string; handler: (args: string, ctx: ExtensionCommandContext) => Promise<void> }>();
  const notifications: { message: string; type?: string }[] = [];
  const userMessages: { text: string; options?: unknown }[] = [];
  const tools = new Map<string, { label: string; description: string; parameterKeys: string[] }>();
  let aborted = false;

  const api = {
    setLabel: () => {},
    on: (event: string, handler: (...args: unknown[]) => unknown) => {
      const list = listeners.get(event) ?? [];
      list.push(handler);
      listeners.set(event, list);
    },
    registerCommand: (name: string, def: { description: string; handler: (args: string, ctx: ExtensionCommandContext) => Promise<void> }) => {
      commands.set(name, def);
    },
    zod: createSchemaRecorder(),
    registerTool: (def: { name: string; label: string; description: string; parameters: { keys: string[] } }) => {
      tools.set(def.name, { label: def.label, description: def.description, parameterKeys: def.parameters.keys });
    },
    sendUserMessage: (text: string, options?: unknown) => {
      userMessages.push({ text, options });
    },
    abortActiveTurn: async () => {
      aborted = true;
    },
    logger: {
      info: () => {},
      warn: () => {},
      error: () => {},
      debug: () => {},
    },
  } as unknown as ExtensionAPI;

  return { api, listeners, commands, tools, notifications, userMessages, aborted };
}

function createMockContext(sessionId = "test-session-123"): ExtensionContext {
  return {
    hasUI: true,
    isSubagent: false,
    taskDepth: 0,
    parentTaskPrefix: undefined,
    cwd: process.cwd(),
    isIdle: () => true,
    sessionManager: {
      getSessionId: () => sessionId,
      getSessionFile: () => null,
    },
    model: { id: "test-model" },
  } as unknown as ExtensionContext;
}

describe("Telegram Harness Hot Reload", () => {
  beforeEach(() => {
    setSavedContext(null);
  });

  test("getInstalledSourceSha reads commit hash from install-manifest.json", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "manifest-test-"));
    try {
      const manifestPath = path.join(tmpDir, "install-manifest.json");
      fs.writeFileSync(manifestPath, JSON.stringify({ source_sha: "abc1234567890def" }), "utf8");
      expect(getInstalledSourceSha(manifestPath)).toBe("abc1234567890def");

      fs.writeFileSync(manifestPath, JSON.stringify({ commit: "fedcba0987654321" }), "utf8");
      expect(getInstalledSourceSha(manifestPath)).toBe("fedcba0987654321");

      const missingPath = path.join(tmpDir, "non-existent.json");
      expect(getInstalledSourceSha(missingPath)).toBe("unknown");
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  test("TelegramRuntime acquires lease, starts poller, and disposes cleanly", async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "runtime-test-"));
    let leaseReleased = false;
    let pollerStopped = false;
    let pollerStarted = false;

    try {
      const slot: DiscoveredSlot = {
        slotId: "slot-1",
        botId: "123456",
        stateDir: tmpDir,
      };

      const coordinator: MockCoordinator = {
        acquireLease: async () => ({ ok: true, slot }),
        releaseLease: () => {
          leaseReleased = true;
          return true;
        },
        close: () => {},
        readRawTokenForSlot: () => "0000000000:TEST_TOKEN",
        readAccessConfig: () => ({ dmPolicy: "allowlist", allowFrom: ["1001"] }),
        recordOutboundMessage: () => {},
        resolveReplyRouting: () => ({ decision: "deliver" }),
        validateDecisionCallback: () => ({ decision: "deliver" }),
        consumeDecisionCallback: () => true,
        applyDecisionAnswer: async () => true,
        getPoolStatus: () => ({ totalSlots: 1, freeSlots: 0 }),
      };

      const poller: MockPoller = {
        start: async () => {
          pollerStarted = true;
        },
        stop: async () => {
          pollerStopped = true;
        },
        getPrimaryChatId: () => "1001",
        sendTelegramMessage: async () => ({ ok: true, result: { message_id: 1 } }),
      };

      const mockApi = createMockExtensionAPI();
      const runtime = new TelegramRuntime(mockApi.api, {
        coordinatorFactory: () => coordinator as unknown as never,
        pollerFactory: () => poller as unknown as never,
      });

      const ctx = createMockContext("sess-1");
      const initSuccess = await runtime.initSession(ctx);
      expect(initSuccess).toBe(true);
      expect(pollerStarted).toBe(true);
      expect(runtime.getSessionId()).toBe("sess-1");
      expect(runtime.getAllowFrom()).toEqual(["1001"]);
      expect(runtime.getPrimaryChatId()).toBe("1001");

      await runtime.dispose();
      expect(pollerStopped).toBe(true);
      expect(leaseReleased).toBe(true);
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  test("reload disposes old runtime, re-imports, re-acquires lease, and notifies operator", async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "hot-reload-test-"));
    const manifestPath = path.join(tmpDir, "install-manifest.json");
    fs.writeFileSync(manifestPath, JSON.stringify({ source_sha: "commit-xyz-789" }), "utf8");

    const events: string[] = [];
    const messagesSent: { chatId: string; text: string }[] = [];

    try {
      const slot: DiscoveredSlot = {
        slotId: "slot-reload",
        botId: "999999",
        stateDir: tmpDir,
      };

      const mockCoordinator = (id: string): MockCoordinator => ({
        acquireLease: async () => {
          events.push(`acquireLease:${id}`);
          return { ok: true, slot };
        },
        releaseLease: () => {
          events.push(`releaseLease:${id}`);
          return true;
        },
        close: () => {
          events.push(`closeCoordinator:${id}`);
        },
        readRawTokenForSlot: () => "0000000000:TEST_TOKEN",
        readAccessConfig: () => ({ dmPolicy: "allowlist", allowFrom: ["5555"] }),
        recordOutboundMessage: () => {},
        resolveReplyRouting: () => ({ decision: "deliver" }),
        validateDecisionCallback: () => ({ decision: "deliver" }),
        consumeDecisionCallback: () => true,
        applyDecisionAnswer: async () => true,
        getPoolStatus: () => ({ totalSlots: 1, freeSlots: 0 }),
      });

      const mockPoller = (id: string): MockPoller => ({
        start: async () => {
          events.push(`startPoller:${id}`);
        },
        stop: async () => {
          events.push(`stopPoller:${id}`);
        },
        getPrimaryChatId: () => "5555",
        sendTelegramMessage: async (chatId, text) => {
          messagesSent.push({ chatId, text });
          return { ok: true, result: { message_id: 10 } };
        },
      });

      const mockApi = createMockExtensionAPI();
      telegramSessionExtension(mockApi.api);

      // Start initial session
      const startHandler = mockApi.listeners.get("session_start")?.[0];
      expect(startHandler).toBeDefined();

      const initialRuntime = new TelegramRuntime(mockApi.api, {
        coordinatorFactory: () => mockCoordinator("runtime-1") as unknown as never,
        pollerFactory: () => mockPoller("runtime-1") as unknown as never,
      });
      setActiveRuntime(initialRuntime);

      const ctx = createMockContext("active-session-1");
      setSavedContext(ctx);
      await initialRuntime.initSession(ctx);

      expect(events).toContain("acquireLease:runtime-1");
      expect(events).toContain("startPoller:runtime-1");

      // Now trigger reload!
      const reloadResult = await reload({
        chatId: "5555",
        interactive: true,
        manifestPath,
        runtimeOptions: {
          coordinatorFactory: () => mockCoordinator("runtime-2") as unknown as never,
          pollerFactory: () => mockPoller("runtime-2") as unknown as never,
        },
      });

      expect(reloadResult.success).toBe(true);
      expect(reloadResult.sha).toBe("commit-xyz-789");

      // Verify sequence: old poller stopped and lease released BEFORE new poller started
      const stopIndex = events.indexOf("stopPoller:runtime-1");
      const releaseIndex = events.indexOf("releaseLease:runtime-1");
      expect(stopIndex).toBeGreaterThanOrEqual(0);
      expect(releaseIndex).toBeGreaterThanOrEqual(0);

      // Verify operator chat notification
      const reloadNotification = messagesSent.find(m => m.text.includes("Telegram harness reloaded"));
      expect(reloadNotification).toBeDefined();
      expect(reloadNotification?.chatId).toBe("5555");
      expect(reloadNotification?.text).toContain("commit-xyz-789");

      // Clean up active runtime
      await getActiveRuntime()?.dispose();
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  test("reload fails safe when dynamic import fails: existing runtime preserved", async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "import-fail-test-"));
    let oldPollerDisposed = false;
    let oldLeaseReleased = false;
    const operatorMessages: string[] = [];

    try {
      const slot: DiscoveredSlot = {
        slotId: "slot-safe",
        botId: "888888",
        stateDir: tmpDir,
      };

      const coordinator: MockCoordinator = {
        acquireLease: async () => ({ ok: true, slot }),
        releaseLease: () => {
          oldLeaseReleased = true;
          return true;
        },
        close: () => {},
        readRawTokenForSlot: () => "0000000000:TEST_TOKEN",
        readAccessConfig: () => ({ dmPolicy: "allowlist", allowFrom: ["7777"] }),
        recordOutboundMessage: () => {},
        resolveReplyRouting: () => ({ decision: "deliver" }),
        validateDecisionCallback: () => ({ decision: "deliver" }),
        consumeDecisionCallback: () => true,
        applyDecisionAnswer: async () => true,
        getPoolStatus: () => ({ totalSlots: 1, freeSlots: 0 }),
      };

      const poller: MockPoller = {
        start: async () => {},
        stop: async () => {
          oldPollerDisposed = true;
        },
        getPrimaryChatId: () => "7777",
        sendTelegramMessage: async (_chatId, text) => {
          operatorMessages.push(text);
          return { ok: true, result: { message_id: 11 } };
        },
      };

      const mockApi = createMockExtensionAPI();
      const runtime = new TelegramRuntime(mockApi.api, {
        coordinatorFactory: () => coordinator as unknown as never,
        pollerFactory: () => poller as unknown as never,
      });
      setActiveRuntime(runtime);

      const ctx = createMockContext("sess-safe");
      await runtime.initSession(ctx);

      // Attempt reload with invalid non-existent module specifier
      const result = await reload({
        chatId: "7777",
        interactive: true,
        runtimeSpecifier: "./non-existent-module-xyz.ts",
      });

      expect(result.success).toBe(false);
      expect(result.error).toBeDefined();

      // Old runtime must NOT be disposed!
      expect(oldPollerDisposed).toBe(false);
      expect(oldLeaseReleased).toBe(false);
      expect(getActiveRuntime()).toBe(runtime);

      // Operator must be notified of failure and that previous runtime is preserved
      expect(operatorMessages.some(msg => msg.includes("Hot reload failed"))).toBe(true);
      expect(operatorMessages.some(msg => msg.includes("Previous runtime retained"))).toBe(true);

      await runtime.dispose();
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  test("reload rejects unauthorized non-allowlisted chat ID", async () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "allowlist-test-"));
    try {
      const slot: DiscoveredSlot = {
        slotId: "slot-auth",
        botId: "777777",
        stateDir: tmpDir,
      };

      const coordinator: MockCoordinator = {
        acquireLease: async () => ({ ok: true, slot }),
        releaseLease: () => true,
        close: () => {},
        readRawTokenForSlot: () => "0000000000:TEST_TOKEN",
        readAccessConfig: () => ({ dmPolicy: "allowlist", allowFrom: ["12345"] }),
        recordOutboundMessage: () => {},
        resolveReplyRouting: () => ({ decision: "deliver" }),
        validateDecisionCallback: () => ({ decision: "deliver" }),
        consumeDecisionCallback: () => true,
        applyDecisionAnswer: async () => true,
        getPoolStatus: () => ({ totalSlots: 1, freeSlots: 0 }),
      };

      const poller: MockPoller = {
        start: async () => {},
        stop: async () => {},
        getPrimaryChatId: () => "12345",
        sendTelegramMessage: async () => ({ ok: true }),
      };

      const mockApi = createMockExtensionAPI();
      const runtime = new TelegramRuntime(mockApi.api, {
        coordinatorFactory: () => coordinator as unknown as never,
        pollerFactory: () => poller as unknown as never,
      });
      setActiveRuntime(runtime);

      const ctx = createMockContext("sess-auth");
      await runtime.initSession(ctx);

      // Unauthorized chat "99999" (allowlist only has "12345")
      const result = await reload({
        chatId: "99999",
      });

      expect(result.success).toBe(false);
      expect(result.error).toBe("Unauthorized chat");

      await runtime.dispose();
    } finally {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    }
  });

  test("concurrent reload calls are serialized by the mutex", async () => {
    let activeReloads = 0;
    let maxConcurrent = 0;

    const mockApi = createMockExtensionAPI();
    const origLoad = loadRuntimeModule;

    // Simulate concurrent reload calls
    const p1 = reload();
    const p2 = reload();
    const p3 = reload();

    const results = await Promise.all([p1, p2, p3]);
    for (const r of results) {
      expect(r.success).toBe(true);
    }
    await getActiveRuntime()?.dispose();
  });

  test("Extension registers tg-reload and telegram reload commands", async () => {
    const mockApi = createMockExtensionAPI();
    telegramSessionExtension(mockApi.api);
    setActiveRuntime({
      onSessionStart: async () => {},
      getPrimaryChatId: () => null,
      dispose: async () => {},
    } as unknown as TelegramRuntime);
    const rootContext = createMockContext();
    await mockApi.listeners.get("session_start")![0]({}, rootContext);
    setSavedContext(null); // This fixture tests command dispatch, not lease acquisition.

    expect(mockApi.commands.has("tg-reload")).toBe(true);
    expect(mockApi.commands.has("telegram")).toBe(true);

    const tgReload = mockApi.commands.get("tg-reload");
    const notifications: { msg: string; level: string }[] = [];
    const commandCtx = {
      ...rootContext,
      ui: {
        notify: (msg: string, level: string) => {
          notifications.push({ msg, level });
        },
      },
    } as unknown as ExtensionCommandContext;

    await tgReload?.handler("", commandCtx);
    expect(notifications.some(n => n.msg.includes("Telegram harness reloaded"))).toBe(true);

    await getActiveRuntime()?.dispose();
  });

  test("Extension registers the operator question, message and dashboard tools", () => {
    const mockApi = createMockExtensionAPI();
    telegramSessionExtension(mockApi.api);
    expect(mockApi.listeners.has("tool_call")).toBe(false);
    for (const toolName of ["bash", "eval", "write", "read", "ssh", "github", "supabase", "launch"]) {
      const results = (mockApi.listeners.get("tool_call") ?? []).map(handler =>
        handler({ toolName, input: { category: "any", command: "inert test data" } }));
      expect(results).toEqual([]);
    }
    expect(mockApi.userMessages).toEqual([]);

    expect([...mockApi.tools.keys()].sort()).toEqual(["telegram_dashboard", "telegram_message", "telegram_question"]);
    expect(mockApi.tools.get("telegram_question")?.parameterKeys).toContain("options");
    expect(mockApi.tools.get("telegram_message")?.parameterKeys).toEqual(["text", "lane_id", "lane_state", "rebind"]);
    expect(mockApi.tools.get("telegram_dashboard")?.parameterKeys).toEqual(["lanes", "blockers", "mergeQueue"]);
    // Without a session-bound channel every tool must refuse rather than fall back to the terminal.
    expect(mockApi.tools.get("telegram_question")?.description).toContain("never grants approval");
  });

  test("reload reports failure when initSession fails to acquire lease", async () => {
    const mockApi = createMockExtensionAPI();
    const slot: DiscoveredSlot = { slotId: "slot-fail", botId: "999", stateDir: os.tmpdir() };
    let acquireAttempts = 0;
    const coordinator: MockCoordinator = {
      acquireLease: async () => {
        acquireAttempts++;
        return acquireAttempts === 1 ? { ok: true, slot } : { ok: false, reason: "Pool exhausted" };
      },
      releaseLease: () => true,
      close: () => {},
      readRawTokenForSlot: () => "0000:TOKEN",
      readAccessConfig: () => ({ dmPolicy: "allowlist", allowFrom: ["123"] }),
      recordOutboundMessage: () => {},
      resolveReplyRouting: () => ({ decision: "deliver" }),
      validateDecisionCallback: () => ({ decision: "deliver" }),
      consumeDecisionCallback: () => true,
      applyDecisionAnswer: async () => true,
      getPoolStatus: () => ({ totalSlots: 1, freeSlots: 0 }),
    };
    const poller: MockPoller = {
      start: async () => {},
      stop: async () => {},
      getPrimaryChatId: () => "123",
      sendTelegramMessage: async () => ({ ok: true }),
    };
    const runtime = new TelegramRuntime(mockApi.api, {
      coordinatorFactory: () => coordinator as unknown as never,
      pollerFactory: () => poller as unknown as never,
    });
    setActiveRuntime(runtime);
    const ctx = createMockContext("sess-init-fail");
    setSavedContext(ctx);
    await runtime.initSession(ctx);

    // On reload, coordinator.acquireLease will return ok: false
    const result = await reload({
      runtimeOptions: {
        coordinatorFactory: () => coordinator as unknown as never,
        pollerFactory: () => poller as unknown as never,
      },
    });
    expect(result.success).toBe(false);
    expect(result.error).toContain("lease not acquired");
    await getActiveRuntime()?.dispose();
  });

  test("initSession is idempotent across successive turns for same session", async () => {
    const mockApi = createMockExtensionAPI();
    const slot: DiscoveredSlot = { slotId: "slot-idem", botId: "998", stateDir: os.tmpdir() };
    let acquireCount = 0;
    const coordinator: MockCoordinator = {
      acquireLease: async () => {
        acquireCount++;
        return { ok: true, slot };
      },
      releaseLease: () => true,
      close: () => {},
      readRawTokenForSlot: () => "0000:TOKEN",
      readAccessConfig: () => ({ dmPolicy: "allowlist", allowFrom: ["123"] }),
      recordOutboundMessage: () => {},
      resolveReplyRouting: () => ({ decision: "deliver" }),
      validateDecisionCallback: () => ({ decision: "deliver" }),
      consumeDecisionCallback: () => true,
      applyDecisionAnswer: async () => true,
      getPoolStatus: () => ({ totalSlots: 1, freeSlots: 0 }),
    };
    const poller: MockPoller = {
      start: async () => {},
      stop: async () => {},
      getPrimaryChatId: () => "123",
      sendTelegramMessage: async () => ({ ok: true }),
    };
    const runtime = new TelegramRuntime(mockApi.api, {
      coordinatorFactory: () => coordinator as unknown as never,
      pollerFactory: () => poller as unknown as never,
    });
    const ctx = createMockContext("sess-idem");
    expect(await runtime.initSession(ctx)).toBe(true);
    expect(acquireCount).toBe(1);

    // Successive turn on same session
    expect(await runtime.initSession(ctx)).toBe(true);
    expect(acquireCount).toBe(1); // Must NOT re-acquire lease
    await runtime.dispose();
  });

  test("child lifecycle cannot replace or dispose the root Telegram runtime", async () => {
    const rootHost = createMockExtensionAPI();
    const childHost = createMockExtensionAPI();
    const starts: string[] = [];
    let disposals = 0;
    const runtime = {
      onSessionStart: async (_event: unknown, ctx: ExtensionContext) => { starts.push(ctx.sessionManager.getSessionId()); },
      onSessionShutdown: async () => { disposals++; },
      getPoller: () => ({}),
    } as unknown as TelegramRuntime;
    setActiveRuntime(runtime);
    telegramSessionExtension(rootHost.api);
    await rootHost.listeners.get("session_start")![0]({}, createMockContext("root-owner"));
    telegramSessionExtension(childHost.api);
    const child = { ...createMockContext("child"), isSubagent: true, taskDepth: 1, parentTaskPrefix: "child" };
    await childHost.listeners.get("session_start")![0]({}, child);
    await childHost.listeners.get("turn_end")![0]();
    await childHost.listeners.get("session_shutdown")![0]({});
    expect(starts).toEqual(["root-owner"]);
    expect(disposals).toBe(0);
    expect(getActiveRuntime()).toBe(runtime);
    await rootHost.listeners.get("turn_end")![0]();
    await rootHost.listeners.get("session_shutdown")![0]({});
    expect(disposals).toBe(1);
    setActiveRuntime(null);
  });

  test("telegram_message provides detailed error and supports rebind: true when route is lost", async () => {
    const mockApi = createMockExtensionAPI();
    telegramSessionExtension(mockApi.api);
    const tool = mockApi.tools.get("telegram_message");
    expect(tool?.parameterKeys).toContain("rebind");
  });
});
