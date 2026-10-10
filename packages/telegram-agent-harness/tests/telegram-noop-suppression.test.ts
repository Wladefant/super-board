import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { shouldSuppressMainFinalReply, SlotRouter, type RouteTarget } from "../daemon/router";
import type { DaemonSessionSummary, TranscriptText } from "../daemon/session-control";
import { DaemonStore } from "../daemon/store";
import { TelegramRuntime } from "../extension/runtime";
import type { DiscoveredSlot } from "../extension/types";
import type { ExtensionAPI, ExtensionContext } from "@veyyon/coding-agent";

describe("W5: shouldSuppressMainFinalReply provenance predicate", () => {
  test("suppresses Main final reply when turn has no operator message and no substantive tools", () => {
    const entry: TranscriptText = {
      entryId: "e1",
      text: "Pinned lane PinthreadAccessBen3 finished without error.",
      hasOperatorMessage: false,
      hasSubstantiveToolCall: false,
    };
    expect(shouldSuppressMainFinalReply(entry)).toBe(true);
  });

  test("suppresses Main final reply when toolNames only includes job or poll", () => {
    const entryJob: TranscriptText = {
      entryId: "e1",
      text: "Background job bg_170 exited with code 0.",
      hasOperatorMessage: false,
      toolNames: ["job"],
    };
    expect(shouldSuppressMainFinalReply(entryJob)).toBe(true);

    const entryJobPoll: TranscriptText = {
      entryId: "e2",
      text: "No new results. All lanes running.",
      hasOperatorMessage: false,
      toolNames: ["job", "poll"],
    };
    expect(shouldSuppressMainFinalReply(entryJobPoll)).toBe(true);
  });

  test("suppresses Main final reply when turn provenance is nested under turn object", () => {
    const entry: TranscriptText = {
      entryId: "e1",
      text: "Background poll completed.",
      turn: {
        hasOperatorMessage: false,
        hasSubstantiveToolCall: false,
        isMain: true,
      },
    };
    expect(shouldSuppressMainFinalReply(entry)).toBe(true);
  });

  test("preserves reply answering operator input", () => {
    const entry: TranscriptText = {
      entryId: "e1",
      text: "Here is the status summary you asked for.",
      hasOperatorMessage: true,
      hasSubstantiveToolCall: false,
    };
    expect(shouldSuppressMainFinalReply(entry)).toBe(false);
  });

  test("preserves reply answering operator input arriving mid-turn", () => {
    const entry: TranscriptText = {
      entryId: "e1",
      text: "Applied mid-turn steer and adjusted file.",
      hasOperatorMessage: true,
      toolNames: ["job"],
    };
    expect(shouldSuppressMainFinalReply(entry)).toBe(false);
  });

  test("preserves substantive tool-work replies", () => {
    const entrySubstantive: TranscriptText = {
      entryId: "e1",
      text: "Dispatched 3 new implementation lanes.",
      hasOperatorMessage: false,
      hasSubstantiveToolCall: true,
    };
    expect(shouldSuppressMainFinalReply(entrySubstantive)).toBe(false);

    const entryWithTask: TranscriptText = {
      entryId: "e2",
      text: "Spawned task worker.",
      hasOperatorMessage: false,
      toolNames: ["task", "job"],
    };
    expect(shouldSuppressMainFinalReply(entryWithTask)).toBe(false);

    const entryWithBash: TranscriptText = {
      entryId: "e3",
      text: "Executed git status.",
      hasOperatorMessage: false,
      toolNames: ["bash"],
    };
    expect(shouldSuppressMainFinalReply(entryWithBash)).toBe(false);
  });

  test("preserves subagent and worker lane replies even if no operator input", () => {
    const entrySubagent: TranscriptText = {
      entryId: "e1",
      text: "Worker lane finished tests.",
      hasOperatorMessage: false,
      hasSubstantiveToolCall: false,
      isMain: false,
    };
    expect(shouldSuppressMainFinalReply(entrySubagent)).toBe(false);

    const entrySubagentViaTurn: TranscriptText = {
      entryId: "e2",
      text: "Worker lane report.",
      turn: {
        hasOperatorMessage: false,
        hasSubstantiveToolCall: false,
        isMain: false,
      },
    };
    expect(shouldSuppressMainFinalReply(entrySubagentViaTurn)).toBe(false);

    const entryFallbackMainFalse: TranscriptText = {
      entryId: "e3",
      text: "Worker report.",
      hasOperatorMessage: false,
      hasSubstantiveToolCall: false,
    };
    expect(shouldSuppressMainFinalReply(entryFallbackMainFalse, false)).toBe(false);
  });

  test("fails open for legacy entries without turn provenance", () => {
    const legacyEntry: TranscriptText = {
      entryId: "e1",
      text: "Legacy turn without provenance metadata.",
    };
    expect(shouldSuppressMainFinalReply(legacyEntry)).toBe(false);
  });
});

describe("W5: SlotRouter.onSessionEvent suppression in daemon mode", () => {
  const DM: RouteTarget = { chatId: "1001", topicId: "" };
  let tmpDir: string;
  let store: DaemonStore;
  let relayed: Array<{ target: RouteTarget; markdown: string }>;

  function fakeSummary(id: string, cwd: string, isSubagent = false): DaemonSessionSummary {
    return {
      id,
      cwd,
      workspace: cwd,
      path: path.join(cwd, `${id}.jsonl`),
      title: null,
      status: "Running",
      modifiedAtMs: Date.now(),
      kind: isSubagent ? "subagent" : "interactive",
      isSubagent,
    };
  }

  function fakeControl(sessions: DaemonSessionSummary[] = []) {
    return {
      control: {
        listSessions: async () => sessions,
        findSession: async (ws: string) => sessions.find(s => s.cwd === ws) ?? null,
        createSession: async () => "created",
        ensureSession: async () => "ensured",
        deliver: async () => "started" as const,
        abort: async () => true,
        loadTranscript: async () => {},
        isBusy: () => false,
      },
    };
  }

  function buildRouter(sessions: DaemonSessionSummary[] = [fakeSummary("sess-main", "C:/dev/demo")]) {
    relayed = [];
    return new SlotRouter({
      slot: {
        slotId: "slot-w5",
        stateDir: path.join(tmpDir, "slot-w5"),
        botId: "1000001",
        fingerprint: "fp",
        preferredProjects: [],
        enabled: true,
        workspace: "C:/dev/demo",
      },
      store,
      control: fakeControl(sessions).control as never,
      send: async () => true,
      relay: async (target, markdown) => {
        relayed.push({ target, markdown });
      },
      log: () => {},
    });
  }

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "w5-router-test-"));
    store = new DaemonStore(path.join(tmpDir, "test.db"));
  });

  afterEach(() => {
    store.close();
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  test("onSessionEvent suppresses Main final reply for no-op background turn", async () => {
    const router = buildRouter();
    await router.bind(DM, "sess-main", "C:/dev/demo");

    await router.onSessionEvent({
      kind: "appended",
      sessionId: "sess-main",
      entries: [
        {
          entryId: "entry-noop",
          text: "StepperAlign2 removed its base worktree. Step ended without error. Waiting for handoff.",
          hasOperatorMessage: false,
          hasSubstantiveToolCall: false,
          isMain: true,
        },
      ],
    });

    expect(relayed).toEqual([]);
  });

  test("onSessionEvent relays Main final reply when operator message was present", async () => {
    const router = buildRouter();
    await router.bind(DM, "sess-main", "C:/dev/demo");

    const answerText = "Here is the status of the running lanes.";
    await router.onSessionEvent({
      kind: "appended",
      sessionId: "sess-main",
      entries: [
        {
          entryId: "entry-op",
          text: answerText,
          hasOperatorMessage: true,
          hasSubstantiveToolCall: false,
          isMain: true,
        },
      ],
    });

    expect(relayed).toEqual([{ target: DM, markdown: answerText }]);
  });

  test("onSessionEvent relays Main final reply when substantive tool calls were made", async () => {
    const router = buildRouter();
    await router.bind(DM, "sess-main", "C:/dev/demo");

    const substantiveText = "Dispatched 2 implementation workers and merged PR #750.";
    await router.onSessionEvent({
      kind: "appended",
      sessionId: "sess-main",
      entries: [
        {
          entryId: "entry-subst",
          text: substantiveText,
          hasOperatorMessage: false,
          hasSubstantiveToolCall: true,
          toolNames: ["task", "job"],
          isMain: true,
        },
      ],
    });

    expect(relayed).toEqual([{ target: DM, markdown: substantiveText }]);
  });

  test("onSessionEvent relays subagent final reply even when background-only", async () => {
    const router = buildRouter();
    await router.bind(DM, "sess-main", "C:/dev/demo");

    const subagentText = "Subagent lane completed verification pass: 15 pass / 0 fail.";
    await router.onSessionEvent({
      kind: "appended",
      sessionId: "sess-main",
      entries: [
        {
          entryId: "entry-subagent",
          text: subagentText,
          hasOperatorMessage: false,
          hasSubstantiveToolCall: false,
          isMain: false,
        },
      ],
    });

    expect(relayed).toEqual([{ target: DM, markdown: subagentText }]);
  });
});

describe("W5: TelegramRuntime final reply suppression in standalone extension mode", () => {
  let tmpDir: string;
  let sent: string[];
  let runtime: TelegramRuntime;

  function createMockApi(): { api: ExtensionAPI; loggerWarn: string[]; loggerInfo: string[] } {
    const loggerWarn: string[] = [];
    const loggerInfo: string[] = [];
    const api = {
      logger: {
        warn: (msg: string) => loggerWarn.push(msg),
        info: (msg: string) => loggerInfo.push(msg),
        error: () => {},
        debug: () => {},
      },
      sendUserMessage: () => {},
      abortActiveTurn: async () => {},
      on: () => {},
      registerCommand: () => {},
    } as unknown as ExtensionAPI;
    return { api, loggerWarn, loggerInfo };
  }

  function createMockContext(sessionId = "sess-w5-rt"): ExtensionContext {
    return {
      sessionManager: {
        getSessionId: () => sessionId,
        getBranch: () => [],
      },
      hasUI: true,
      isSubagent: false,
      taskDepth: 0,
      parentTaskPrefix: undefined,
      cwd: "C:/dev/demo",
      ui: { notify: () => {} },
    } as unknown as ExtensionContext;
  }

  beforeEach(async () => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "w5-runtime-test-"));
    process.env.VEYYON_TELEGRAM_DAEMON_DB = path.join(tmpDir, "absent-daemon.db");
    delete (globalThis as Record<string | symbol, unknown>)[Symbol.for("veyyon.telegram.active_root")];
    delete (globalThis as Record<string | symbol, unknown>)[Symbol.for("veyyon.telegram.active_lease")];
    sent = [];
    const slot: DiscoveredSlot = { slotId: "slot-w5-rt", botId: "998877", stateDir: tmpDir };
    const coordinator = {
      acquireLease: async () => ({ ok: true, slot }),
      releaseLease: () => true,
      close: () => {},
      readRawTokenForSlot: () => "998877:TEST_TOKEN",
      readAccessConfig: () => ({ dmPolicy: "allowlist", allowFrom: ["2001"] }),
      recordOutboundMessage: () => {},
      resolveReplyRouting: () => ({ decision: "deliver" }),
      validateDecisionCallback: () => ({ decision: "deliver" }),
      consumeDecisionCallback: () => true,
      applyDecisionAnswer: async () => true,
      getPoolStatus: () => ({ totalSlots: 1, freeSlots: 0 }),
    };
    const poller = {
      start: async () => {},
      stop: async () => {},
      getPrimaryChatId: () => "2001",
      sendTelegramMessage: async (_chatId: string, text: string) => {
        sent.push(text);
        return { ok: true, result: { message_id: sent.length } };
      },
    };

    const mockApi = createMockApi();
    runtime = new TelegramRuntime(mockApi.api, {
      coordinatorFactory: () => coordinator as unknown as never,
      pollerFactory: () => poller as unknown as never,
    });
    await runtime.initSession(createMockContext("sess-" + Math.random().toString(36).slice(2)));
    sent.length = 0;
  });

  afterEach(async () => {
    if (runtime) await runtime.dispose().catch(() => {});
    delete (globalThis as Record<string | symbol, unknown>)[Symbol.for("veyyon.telegram.active_root")];
    delete (globalThis as Record<string | symbol, unknown>)[Symbol.for("veyyon.telegram.active_lease")];
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  function makeAssistantReply(text: string) {
    return {
      message: {
        role: "assistant",
        content: [{ type: "text", text }],
      },
    } as Parameters<TelegramRuntime["onMessageEnd"]>[0];
  }

  test("suppresses final reply when turn has no operator message and no substantive tools", async () => {
    // Background turn: no user message, only assistant start and end
    await runtime.onMessageStart({ message: { role: "assistant" } });
    await runtime.onMessageEnd(makeAssistantReply("Background acknowledgement: all lanes running."));

    expect(sent).toEqual([]);
  });

  test("suppresses final reply when turn only calls job tool", async () => {
    await runtime.onMessageStart({ message: { role: "assistant" } });
    runtime.onToolExecutionStart({ toolName: "job" });
    await runtime.onMessageEnd(makeAssistantReply("Background job bg_101 finished."));

    expect(sent).toEqual([]);
  });

  test("preserves final reply when turn had operator user message", async () => {
    // Turn started by operator user message
    await runtime.onMessageStart({ message: { role: "user" } });
    await runtime.onMessageStart({ message: { role: "assistant" } });
    await runtime.onMessageEnd(makeAssistantReply("Here is the answer to your request."));

    expect(sent).toHaveLength(1);
    expect(sent[0]).toContain("Here is the answer to your request.");
  });

  test("preserves final reply when operator input arrives mid-turn", async () => {
    // Turn started in background, but operator sends input mid-turn
    await runtime.onMessageStart({ message: { role: "assistant" } });
    runtime.onToolExecutionStart({ toolName: "job" });
    // Mid-turn operator message
    await runtime.onMessageStart({ message: { role: "user" } });
    await runtime.onMessageEnd(makeAssistantReply("Steered turn: answering operator."));

    expect(sent).toHaveLength(1);
    expect(sent[0]).toContain("Steered turn: answering operator.");
  });

  test("preserves final reply when substantive tool was executed", async () => {
    await runtime.onMessageStart({ message: { role: "assistant" } });
    runtime.onToolExecutionStart({ toolName: "task" });
    await runtime.onMessageEnd(makeAssistantReply("Substantive tool work completed."));

    expect(sent).toHaveLength(1);
    expect(sent[0]).toContain("Substantive tool work completed.");
  });
});
