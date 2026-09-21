/**
 * daemon-runtime-forum.test.ts — Auto-attach runtime loop, startup, and host recovery.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { GuiHostFallbackManager } from "../daemon/gui-host-fallback";
import { TelegramDaemon } from "../daemon/runtime";
import {
  type DaemonSessionSummary,
  type GuiHostSessionControl,
} from "../daemon/session-control";
import { TelegramPoller } from "../extension/poller";
import { FakeForumApiClient } from "./daemon-forum.test";

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
    fs.rmSync(tempDir, { recursive: true, force: true });
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
    const control: GuiHostSessionControl = {
      endpoint: "tcp:127.0.0.1:7699",
      isBusy: () => false,
      listSessions: async () => liveSessions,
      discoverDiskSessions: () => [],
      findSession: async (ws: string) => liveSessions.find(s => s.workspace === ws) ?? null,
      createSession: async () => "created-id",
      ensureSession: async () => "ensured-id",
      deliver: async () => "started",
      abort: async () => true,
      loadTranscript: async (id: string) => {
        loaded.push(id);
      },
      usage: async () => null,
      close: () => {},
    };
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

  test("reconciles on demand and wires fallback manager recovery", async () => {
    createManifest(true);

    const fake = fakeControl();
    const forumClient = new FakeForumApiClient();
    let hostRecoveredHandler: (() => void) | undefined;

    const daemon = new TelegramDaemon({
      manifestPath,
      poolDbPath: path.join(tempDir, "pool.db"),
      daemonDbPath: path.join(tempDir, "daemon.db"),
      channelsDir: path.join(tempDir, "channels"),
      controlFactory: () => fake.control,
      fallbackManagerFactory: (control, log) => {
        const fallback = new GuiHostFallbackManager(control, {
          log,
          onHostRecovered: () => {
            if (hostRecoveredHandler) hostRecoveredHandler();
          },
        });
        return fallback;
      },
      forumClientFactory: () => forumClient,
      pollerFactory: () => dummyPoller(),
      log: () => {},
    });

    await daemon.start();
    expect(forumClient.topics.size).toBe(0);

    // Wire trigger
    hostRecoveredHandler = () => {
      void daemon.reconcileAllAutoAttach();
    };

    // New session arrives
    fake.liveSessions.push({
      id: "recovered-sess",
      cwd: "C:/dev/recovered",
      workspace: "C:/dev/recovered",
      title: null,
      status: "Idle",
      modifiedAtMs: Date.now(),
    });

    // Simulate recovery event
    hostRecoveredHandler();
    // Allow microtasks to settle
    await Promise.resolve();
    await daemon.reconcileAllAutoAttach();

    expect(forumClient.topics.size).toBe(1);
    const topic = [...forumClient.topics.values()][0];
    expect(topic.name).toBe("recovered");
    expect(fake.loaded).toContain("recovered-sess");

    await daemon.stop();
  });
});
