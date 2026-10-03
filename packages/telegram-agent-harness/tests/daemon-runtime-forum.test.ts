/**
 * daemon-runtime-forum.test.ts — Auto-attach runtime loop, startup, and host recovery.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { TelegramDaemon } from "../daemon/runtime";
import {
  type DaemonSessionSummary,
  type TerminalSessionControl,
} from "../daemon/session-control";
import { TelegramPoller } from "../extension/poller";
import { FakeForumApiClient } from "./daemon-forum.test";
import type { MessageCorrelationBridge } from "../extension/types";

const OPERATOR_ID = "1247617658";
const FORUM_CHAT_ID = "-10077889900";

describe("TelegramDaemon forum auto-attach runtime", () => {
  let tempDir: string;
  let manifestPath: string;
  let stateDir: string;

  beforeEach(() => {
    tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "veyyon-runtime-forum-test-"));
    stateDir = path.join(tempDir, "channels", "slot-runtime-forum");
    fs.mkdirSync(stateDir, { recursive: true });
    fs.writeFileSync(path.join(stateDir, ".env"), "TELEGRAM_BOT_TOKEN=123:TOKEN\n");
    fs.writeFileSync(path.join(stateDir, "access.json"), JSON.stringify({ dmPolicy: "allowlist", allowFrom: [OPERATOR_ID] }));
    manifestPath = path.join(tempDir, "manifest.json");
  });

  afterEach(() => {
    try {
      fs.rmSync(tempDir, { recursive: true, force: true });
    } catch {}
  });

  function createManifest(autoAttach = true, intervalMs = 60_000) {
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 1,
        slots: [
          {
            slotId: "slot-runtime-forum",
            stateDir,
            enabled: true,
            daemon: true,
            mode: "forum",
            forumChatId: FORUM_CHAT_ID,
            autoAttach,
            autoAttachIntervalMs: intervalMs,
            defaultProject: "C:/dev/proj",
          },
        ],
      }),
    );
  }

  function fakeControl(sessions: DaemonSessionSummary[] = []) {
    const liveSessions = [...sessions];
    const loaded: string[] = [];
    const control = {
      isBusy: () => false,
      listSessions: async () => liveSessions,
      findSession: async (ws: string) => liveSessions.find(s => s.workspace === ws) ?? null,
      createSession: async () => "created-id",
      ensureSession: async () => "ensured-id",
      deliver: async () => "started",
      abort: async () => true,
      loadTranscript: async (id: string) => {
        loaded.push(id);
      },
      close: () => {},
    } as unknown as TerminalSessionControl;
    return { control, liveSessions, loaded };
  }

  function dummyPoller(): TelegramPoller {
    return {
      running: true,
      start: async () => {},
      stop: async () => {},
      getPrimaryChatId: () => null,
      getActiveThreadId: () => undefined,
      getMeta: () => null,
      sendTelegramMessage: async () => null,
    } as unknown as TelegramPoller;
  }

  test("startup reconciles live sessions automatically", async () => {
    createManifest(true);

    const fake = fakeControl([
      {
        id: "startup-sess",
        cwd: "C:/dev/startup",
        workspace: "C:/dev/startup",
        title: null,
        status: "Idle",
        modifiedAtMs: Date.now(),
      },
    ]);
    const forumClient = new FakeForumApiClient();

    const daemon = new TelegramDaemon({
      manifestPath,
      poolDbPath: path.join(tempDir, "pool.db"),
      daemonDbPath: path.join(tempDir, "daemon.db"),
      channelsDir: path.join(tempDir, "channels"),
      controlFactory: () => fake.control,
      forumClientFactory: () => forumClient,
      pollerFactory: () => dummyPoller(),
      log: () => {},
    });

    await daemon.start();

    expect(forumClient.topics.size).toBe(1);
    const topic = [...forumClient.topics.values()][0];
    expect(topic.name).toBe("startup");
    expect(fake.loaded).toContain("startup-sess");

    await daemon.stop();
  });

  test("reconciles newly registered owners on demand without a GUI recovery fallback", async () => {
    createManifest(true);
    const fake = fakeControl();
    const forumClient = new FakeForumApiClient();
    const daemon = new TelegramDaemon({
      manifestPath,
      poolDbPath: path.join(tempDir, "pool.db"),
      daemonDbPath: path.join(tempDir, "daemon.db"),
      channelsDir: path.join(tempDir, "channels"),
      controlFactory: () => fake.control,
      forumClientFactory: () => forumClient,
      pollerFactory: () => dummyPoller(),
      log: () => {},
    });
    await daemon.start();
    expect(forumClient.topics.size).toBe(0);
    fake.liveSessions.push({
      id: "recovered-sess", cwd: "C:/dev/recovered", workspace: "C:/dev/recovered",
      title: null, status: "Idle", modifiedAtMs: Date.now(),
    });
    await daemon.reconcileAllAutoAttach();
    expect(forumClient.topics.size).toBe(1);
    expect([...forumClient.topics.values()][0].name).toBe("recovered");
    expect(fake.loaded).toContain("recovered-sess");
    await daemon.stop();
  });
  test("explicitly detached session is skipped by auto-attach reconciliation", async () => {
    createManifest(true);

    const fake = fakeControl([
      {
        id: "sess-detached",
        cwd: "C:/dev/detached",
        workspace: "C:/dev/detached",
        title: null,
        status: "Idle",
        modifiedAtMs: Date.now(),
      },
    ]);
    const forumClient = new FakeForumApiClient();

    const daemon = new TelegramDaemon({
      manifestPath,
      poolDbPath: path.join(tempDir, "pool.db"),
      daemonDbPath: path.join(tempDir, "daemon.db"),
      channelsDir: path.join(tempDir, "channels"),
      controlFactory: () => fake.control,
      forumClientFactory: () => forumClient,
      pollerFactory: () => dummyPoller(),
      log: () => {},
    });

    await daemon.start();
    expect(forumClient.topics.size).toBe(1);
    const slot = daemon.getActiveSlot("slot-runtime-forum");
    expect(slot).toBeDefined();
    slot?.forumManager?.markDetached("sess-detached");
    slot?.forumManager?.options.store.deleteRoute("slot-runtime-forum", FORUM_CHAT_ID, "1");

    await daemon.reconcileAllAutoAttach();
    expect(slot?.forumManager?.options.store.getRoute("slot-runtime-forum", FORUM_CHAT_ID, "1")).toBeNull();

    await daemon.stop();
  });
  test("forum menu attaches through its authenticated owner and rejects replay", async () => {
    createManifest(true);
    const fake = fakeControl([
      { id: "owner-one", cwd: "C:/dev/one", workspace: "C:/dev/one", title: null, status: "Idle", modifiedAtMs: Date.now() },
      { id: "owner-two", cwd: "C:/dev/two", workspace: "C:/dev/two", title: null, status: "Idle", modifiedAtMs: Date.now() },
    ]);
    const forumClient = new FakeForumApiClient();
    let callbacks!: ConstructorParameters<typeof TelegramPoller>[3];
    let correlation!: MessageCorrelationBridge;
    let thread = 101;
    const sent: Array<Parameters<TelegramPoller["sendTelegramMessage"]>> = [];
    const daemon = new TelegramDaemon({
      manifestPath, poolDbPath: path.join(tempDir, "pool.db"),
      daemonDbPath: path.join(tempDir, "daemon.db"), channelsDir: path.join(tempDir, "channels"),
      controlFactory: () => fake.control, forumClientFactory: () => forumClient,
      pollerFactory: (_token, _state, _access, handlers, bridge) => {
        callbacks = handlers;
        correlation = bridge;
        return Object.assign(dummyPoller(), {
          getActiveThreadId: () => thread,
          sendTelegramMessage: async (...args: Parameters<TelegramPoller["sendTelegramMessage"]>) => {
            sent.push(args);
            return { ok: true, result: { message_id: sent.length } };
          },
        });
      },
      log: () => {},
    });
    await daemon.start();
    try {
      expect(await callbacks.onHarnessCommand?.("/sessions@ExampleBot", FORUM_CHAT_ID, OPERATOR_ID)).toBe(true);
      const menu = sent.find(args => args[1] === "Choose the running session to attach:");
      const markup = menu?.[2];
      if (!markup || typeof markup === "string" || !("inline_keyboard" in markup)) throw new Error("Missing attach buttons");
      const token = markup.inline_keyboard[0][0].callback_data;
      const selection = correlation.resolveCallback?.(token, OPERATOR_ID, FORUM_CHAT_ID);
      expect(selection?.decision).toBe("deliver");
      expect(correlation.resolveCallback?.(token, "unauthorized", FORUM_CHAT_ID).decision).toBe("reject_unauthorized");
      if (!selection?.record) throw new Error("Missing stored callback");
      await callbacks.onDecisionCallback?.(selection.record.decisionId, selection.record.choiceId);
      expect(fake.loaded).toContain(selection.record.choiceId);
      expect(correlation.consumeCallback?.(token)).toBe(true);
      expect(correlation.resolveCallback?.(token, OPERATOR_ID, FORUM_CHAT_ID).decision).toBe("reject_already_consumed");
    } finally {
      await daemon.stop();
    }
  });
  test("/app inside a forum topic replies in that topic thread with topic context in Mini App URL", async () => {
    createManifest(true);
    fs.writeFileSync(
      path.join(stateDir, "miniapp.json"),
      JSON.stringify({ url: "https://miniapp.example.com", secret: "a".repeat(64) }),
    );
    const fake = fakeControl([
      { id: "owner-topic", cwd: "C:/dev/topic", workspace: "C:/dev/topic", title: null, status: "Idle", modifiedAtMs: Date.now() },
    ]);
    const forumClient = new FakeForumApiClient();
    let callbacks!: ConstructorParameters<typeof TelegramPoller>[3];
    let thread = 101;
    const sent: Array<Parameters<TelegramPoller["sendTelegramMessage"]>> = [];
    const daemon = new TelegramDaemon({
      manifestPath, poolDbPath: path.join(tempDir, "pool.db"),
      daemonDbPath: path.join(tempDir, "daemon.db"), channelsDir: path.join(tempDir, "channels"),
      controlFactory: () => fake.control, forumClientFactory: () => forumClient,
      pollerFactory: (_token, _state, _access, handlers) => {
        callbacks = handlers;
        return Object.assign(dummyPoller(), {
          getActiveThreadId: () => thread,
          sendTelegramMessage: async (...args: Parameters<TelegramPoller["sendTelegramMessage"]>) => {
            sent.push(args);
            return { ok: true, result: { message_id: sent.length } };
          },
        });
      },
      log: () => {},
    });
    await daemon.start();
    try {
      expect(await callbacks.onHarnessCommand?.("/app@ExampleBot", FORUM_CHAT_ID, OPERATOR_ID)).toBe(true);
      const appMsg = sent.find(args => args[1] === "Open your Superboard dashboard");
      expect(appMsg).toBeDefined();
      expect(appMsg?.[6]).toBe(101);
      const markup = appMsg?.[2];
      if (!markup || typeof markup === "string" || !("inline_keyboard" in markup)) throw new Error("Missing app button");
      const webAppUrl = markup.inline_keyboard[0][0].web_app?.url;
      expect(webAppUrl).toContain("topicId=101");
      expect(webAppUrl).toBe("https://miniapp.example.com/?topicId=101&sessionId=owner-topic");
    } finally {
      await daemon.stop();
    }
  });
  test("inbound attachments resolve the transcript directory of the session bound to the topic", async () => {
    createManifest(true);
    const configRoot = path.join(tempDir, "veyyon-config");
    const transcript = path.join(configRoot, "sessions", "-dev-topic", "2026-09-24T00-00-00-000Z_owner-topic.jsonl");
    fs.mkdirSync(path.dirname(transcript), { recursive: true });
    fs.writeFileSync(transcript, "");
    const fake = fakeControl([
      { id: "owner-topic", cwd: "C:/dev/topic", workspace: "C:/dev/topic", title: null, status: "Idle", modifiedAtMs: Date.now() },
    ]);
    Object.assign(fake.control, { configRoot });
    const forumClient = new FakeForumApiClient();
    let callbacks!: ConstructorParameters<typeof TelegramPoller>[3];
    let thread = 101;
    const daemon = new TelegramDaemon({
      manifestPath, poolDbPath: path.join(tempDir, "pool.db"),
      daemonDbPath: path.join(tempDir, "daemon.db"), channelsDir: path.join(tempDir, "channels"),
      controlFactory: () => fake.control, forumClientFactory: () => forumClient,
      pollerFactory: (_token, _state, _access, handlers) => {
        callbacks = handlers;
        return Object.assign(dummyPoller(), { getActiveThreadId: () => thread });
      },
      log: () => {},
    });
    await daemon.start();
    try {
      expect(callbacks.getSessionFile?.()).toBe(transcript);
      thread = 999;
      expect(callbacks.getSessionFile?.()).toBeUndefined();
    } finally {
      await daemon.stop();
    }
  });
});
