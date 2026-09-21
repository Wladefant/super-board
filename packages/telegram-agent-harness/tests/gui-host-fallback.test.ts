import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { describe, expect, test } from "bun:test";
import { GuiHostFallbackManager } from "../daemon/gui-host-fallback";
import { SlotRouter, type RouteTarget, type SlotRouterOptions } from "../daemon/router";
import {
  GuiHostSessionControl,
  type DaemonSessionSummary,
  type DeliveryMode,
  type SessionEvent,
} from "../daemon/session-control";
import { DaemonStore } from "../daemon/store";
import { GuiHostRequestError } from "../src/gui-host-client";

class FakeSessionControl extends GuiHostSessionControl {
  public deliverCalls: Array<{ sessionId: string; text: string; mode: DeliveryMode }> = [];
  public ensureCalls: Array<{ workspace: string; title: string }> = [];
  public shouldFailWithRefused = false;
  public failCount = 0;
  public maxFailCount = 0;
  public closed = false;

  constructor() {
    super({ endpoint: "tcp:127.0.0.1:7699", onEvent: () => {}, onLog: () => {} });
  }

  public override async deliver(
    sessionId: string,
    text: string,
    mode: DeliveryMode = "auto",
  ): Promise<"started" | "queued" | "steered"> {
    if (this.shouldFailWithRefused || this.failCount < this.maxFailCount) {
      this.failCount++;
      throw new GuiHostRequestError("connect ECONNREFUSED 127.0.0.1:7699", "ECONNREFUSED");
    }
    this.deliverCalls.push({ sessionId, text, mode });
    return "started";
  }

  public override async ensureSession(workspace: string, title?: string): Promise<string> {
    if (this.shouldFailWithRefused || this.failCount < this.maxFailCount) {
      this.failCount++;
      throw new GuiHostRequestError("connect ECONNREFUSED 127.0.0.1:7699", "ECONNREFUSED");
    }
    this.ensureCalls.push({ workspace, title: title ?? "" });
    return "fake-session-1";
  }

  public override async listSessions(): Promise<DaemonSessionSummary[]> {
    if (this.shouldFailWithRefused || this.failCount < this.maxFailCount) {
      this.failCount++;
      throw new GuiHostRequestError("connect ECONNREFUSED 127.0.0.1:7699", "ECONNREFUSED");
    }
    return [
      {
        id: "fake-session-1",
        cwd: "C:/work",
        workspace: "work",
        title: "Test Session",
        status: "idle",
        modifiedAtMs: Date.now(),
      },
    ];
  }
  public override async loadTranscript(_sessionId: string): Promise<any[]> {
    return [];
  }
  public override async lastPrompt(_sessionId: string): Promise<string | null> {
    return null;
  }



  public override close(): void {
    this.closed = true;
  }
}

describe("GuiHostFallbackManager", () => {
  test("delivers message normally without triggering fallback when host is up", async () => {
    const fakeControl = new FakeSessionControl();
    let restartTriggered = false;

    const manager = new GuiHostFallbackManager({
      control: fakeControl,
      restartHost: async () => {
        restartTriggered = true;
      },
      isPortListening: async () => true,
    });

    const notifications: string[] = [];
    const result = await manager.withFallback(
      "chat-123",
      async text => {
        notifications.push(text);
      },
      async () => fakeControl.deliver("s1", "hello"),
    );

    expect(result).toBe("started");
    expect(notifications).toEqual([]);
    expect(restartTriggered).toBe(false);
    expect(fakeControl.deliverCalls).toHaveLength(1);
    expect(fakeControl.deliverCalls[0].text).toBe("hello");
  });

  test("on ECONNREFUSED, notifies chat, runs restartHost, waits for port, and retries request once", async () => {
    const fakeControl = new FakeSessionControl();
    fakeControl.maxFailCount = 1; // Fails first time with ECONNREFUSED, then succeeds

    let restartCount = 0;
    let portChecks = 0;

    const manager = new GuiHostFallbackManager({
      control: fakeControl,
      restartHost: async () => {
        restartCount++;
      },
      isPortListening: async () => restartCount > 0,
      portWaitTimeoutMs: 1000,
    });

    const notifications: string[] = [];
    const result = await manager.withFallback(
      "chat-123",
      async text => {
        notifications.push(text);
      },
      async () => fakeControl.deliver("s1", "hello"),
    );

    expect(result).toBe("started");
    expect(notifications).toEqual(["Veyyon host is down, restarting it…"]);
    expect(restartCount).toBe(1);
    expect(fakeControl.closed).toBe(true); // reset/closed called before retry
    expect(fakeControl.deliverCalls).toHaveLength(1);
  });

  test("chat notification is throttled to once per 10 minutes per chat", async () => {
    const fakeControl = new FakeSessionControl();
    fakeControl.maxFailCount = 1; // Fails first call, succeeds on retry

    let restartCount = 0;
    const manager = new GuiHostFallbackManager({
      control: fakeControl,
      restartHost: async () => {
        restartCount++;
      },
      isPortListening: async () => restartCount > 0,
      restartCooldownMs: 0,
      chatNoticeCooldownMs: 600_000,
    });

    const notifications: string[] = [];
    const replyFn = async (text: string) => {
      notifications.push(text);
    };

    // First operation fails once -> notifies -> restarts -> retries -> succeeds
    await manager.withFallback("chat-123", replyFn, async () => fakeControl.deliver("s1", "first"));
    expect(notifications).toEqual(["Veyyon host is down, restarting it…"]);

    // Force another failure for chat-123
    fakeControl.maxFailCount = fakeControl.failCount + 1;
    await manager.withFallback("chat-123", replyFn, async () => fakeControl.deliver("s1", "second"));

    // Notifications should STILL only have 1 entry for chat-123 because cooldown hasn't passed
    expect(notifications).toEqual(["Veyyon host is down, restarting it…"]);

    // But a DIFFERENT chat gets notified
    fakeControl.maxFailCount = fakeControl.failCount + 1;
    await manager.withFallback("chat-456", replyFn, async () => fakeControl.deliver("s1", "third"));
    expect(notifications).toHaveLength(2);
    expect(notifications[1]).toBe("Veyyon host is down, restarting it…");
  });

  test("does not trigger multiple restarts concurrently", async () => {
    const fakeControl = new FakeSessionControl();
    fakeControl.maxFailCount = 10;

    let restartCount = 0;
    const { promise: restartGate, resolve: resolveRestart } = Promise.withResolvers<void>();

    const manager = new GuiHostFallbackManager({
      control: fakeControl,
      restartHost: async () => {
        restartCount++;
        await restartGate;
      },
      isPortListening: async () => restartCount > 0,
    });

    const p1 = manager.ensureHostStarted();
    const p2 = manager.ensureHostStarted();

    resolveRestart();
    const [r1, r2] = await Promise.all([p1, p2]);

    expect(r1).toBe(true);
    expect(r2).toBe(true);
    expect(restartCount).toBe(1);
  });

  test("propagates error if port never becomes available", async () => {
    const fakeControl = new FakeSessionControl();
    fakeControl.shouldFailWithRefused = true;

    const manager = new GuiHostFallbackManager({
      control: fakeControl,
      restartHost: async () => {},
      isPortListening: async () => false, // Port stays down
      portWaitTimeoutMs: 100, // Short timeout for test
    });

    let caughtError: unknown = null;
    try {
      await manager.withFallback(
        "chat-123",
        async () => {},
        async () => fakeControl.deliver("s1", "msg"),
      );
    } catch (err) {
      caughtError = err;
    }

    expect(caughtError).toBeInstanceOf(GuiHostRequestError);
    expect((caughtError as GuiHostRequestError).code).toBe("ECONNREFUSED");
  });

  test("non-ECONNREFUSED error is rethrown without attempting restart", async () => {
    const fakeControl = new FakeSessionControl();
    let restartTriggered = false;

    const manager = new GuiHostFallbackManager({
      control: fakeControl,
      restartHost: async () => {
        restartTriggered = true;
      },
      isPortListening: async () => true,
    });

    const notifications: string[] = [];
    let caughtError: unknown = null;

    try {
      await manager.withFallback(
        "chat-123",
        async text => {
          notifications.push(text);
        },
        async () => {
          throw new GuiHostRequestError("Authentication failed", "AUTH_FAILED");
        },
      );
    } catch (err) {
      caughtError = err;
    }

    expect(caughtError).toBeInstanceOf(GuiHostRequestError);
    expect((caughtError as GuiHostRequestError).code).toBe("AUTH_FAILED");
    expect(notifications).toEqual([]);
    expect(restartTriggered).toBe(false);
  });
});

describe("SlotRouter with GuiHostFallbackManager", () => {
  test("recovers deliver and routing command when host is restarted", async () => {
    const fakeControl = new FakeSessionControl();
    fakeControl.maxFailCount = 1;

    let restartCount = 0;
    const fallbackManager = new GuiHostFallbackManager({
      control: fakeControl,
      restartHost: async () => {
        restartCount++;
      },
      isPortListening: async () => restartCount > 0,
    });

    const sentMessages: Array<{ target: RouteTarget; html: string }> = [];
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "gui-host-fallback-test-"));
    const store = new DaemonStore(path.join(tempDir, "daemon.db"));
    const options: SlotRouterOptions = {
      slot: {
        slotId: "test-slot",
        token: "123:ABC",
        workspace: "C:/work",
        forumChatId: null,
        mode: "personal",
      },
      store,
      control: fakeControl,
      send: async (target, html) => {
        sentMessages.push({ target, html });
      },
      relay: async () => {},
      log: () => {},
      fallbackManager,
    };
    const router = new SlotRouter(options);
    const target: RouteTarget = { chatId: "chat-999", topicId: "" };

    const deliverResult = await router.deliver(target, "Hello agent");
    expect(deliverResult).toContain("fake-session-1");
    expect(restartCount).toBe(1);
    expect(sentMessages.map(m => m.html)).toContain("Veyyon host is down, restarting it…");

    // Command /sessions should also work seamlessly with fallback
    fakeControl.maxFailCount = 1;
    fakeControl.failCount = 0;
    const commandResult = await router.handleCommand("/sessions", target);
    expect(commandResult).toBe(true);
    expect(sentMessages.some(m => m.html.includes("work"))).toBe(true);
  });
});
