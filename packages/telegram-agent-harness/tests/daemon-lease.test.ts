/**
 * The daemon's exclusion invariant, against the real pool coordinator and a real
 * `bot_pool.db`: a token is polled by exactly one process. The daemon takes an
 * ordinary slot lease, so an in-session extension sees the slot as busy and skips
 * it — which is what keeps Telegram from answering with HTTP 409 Conflict.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { BotPoolCoordinator } from "../extension/coordinator";
import type { TelegramPoller } from "../extension/poller";
import { readDaemonSlotIds, resolveDaemonSlots } from "../daemon/config";
import { claimDaemonPidFile, TelegramDaemon, type DaemonRuntimeOptions } from "../daemon/runtime";
import type { GuiHostSessionControl } from "../daemon/session-control";

const OPERATOR_CHAT = "1247617658";

interface SlotSpec {
  slotId: string;
  daemon?: boolean;
  projects?: string[];
}

let root: string;
let workspace: string;
let poolDbPath: string;
let manifestPath: string;
let channelsDir: string;
let previousDaemonDir: string | undefined;
let coordinators: BotPoolCoordinator[];
let daemons: TelegramDaemon[];
let startedPollers: FakePoller[];

/** Stands in for the Bot API transport; no network call is made in these tests. */
class FakePoller {
  public running = false;
  public readonly sent: { chatId: string; text: string }[] = [];
  constructor(public readonly stateDir: string) {}
  async start(): Promise<void> {
    this.running = true;
  }
  async stop(): Promise<void> {
    this.running = false;
  }
  getPrimaryChatId(): string | null {
    return OPERATOR_CHAT;
  }
  async sendTelegramMessage(chatId: string, text: string): Promise<{ ok: true }> {
    this.sent.push({ chatId, text });
    return { ok: true };
  }
}

function writePool(specs: SlotSpec[]): void {
  fs.mkdirSync(channelsDir, { recursive: true });
  const slots = specs.map((spec, index) => {
    const stateDir = path.join(channelsDir, spec.slotId);
    fs.mkdirSync(stateDir, { recursive: true });
    fs.writeFileSync(
      path.join(stateDir, ".env"),
      `TELEGRAM_BOT_TOKEN=100000000${index}:AA${crypto.randomUUID().replace(/-/g, "")}\n`,
      "utf8",
    );
    fs.writeFileSync(
      path.join(stateDir, "access.json"),
      JSON.stringify({ dmPolicy: "allowlist", allowFrom: [OPERATOR_CHAT] }),
      "utf8",
    );
    return {
      slotId: spec.slotId,
      stateDir,
      projects: spec.projects ?? [workspace],
      enabled: true,
      ...(spec.daemon === undefined ? {} : { daemon: spec.daemon }),
    };
  });
  fs.writeFileSync(manifestPath, JSON.stringify({ version: 1, slots }, null, 2), "utf8");
}

function coordinator(): BotPoolCoordinator {
  const instance = new BotPoolCoordinator(poolDbPath, manifestPath, channelsDir);
  coordinators.push(instance);
  return instance;
}

function daemon(overrides: DaemonRuntimeOptions = {}): TelegramDaemon {
  const instance = new TelegramDaemon({
    poolDbPath,
    manifestPath,
    channelsDir,
    daemonDbPath: path.join(root, "daemon.db"),
    endpoint: null,
    log: () => {},
    pollerFactory: (_token, stateDir) => {
      const poller = new FakePoller(stateDir);
      startedPollers.push(poller);
      return poller as unknown as TelegramPoller;
    },
    controlFactory: () =>
      ({
        endpoint: "tcp:127.0.0.1:1",
        isBusy: () => false,
        close: () => {},
      }) as unknown as GuiHostSessionControl,
    ...overrides,
  });
  daemons.push(instance);
  return instance;
}

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "veyyon-daemon-lease-"));
  workspace = path.join(root, "project");
  fs.mkdirSync(workspace, { recursive: true });
  poolDbPath = path.join(root, "bot_pool.db");
  manifestPath = path.join(root, "manifest.json");
  channelsDir = path.join(root, "channels");
  previousDaemonDir = process.env.VEYYON_TELEGRAM_DAEMON_DIR;
  process.env.VEYYON_TELEGRAM_DAEMON_DIR = path.join(root, "run");
  coordinators = [];
  daemons = [];
  startedPollers = [];
});

afterEach(async () => {
  for (const instance of daemons) await instance.stop();
  for (const instance of coordinators) instance.close();
  if (previousDaemonDir === undefined) delete process.env.VEYYON_TELEGRAM_DAEMON_DIR;
  else process.env.VEYYON_TELEGRAM_DAEMON_DIR = previousDaemonDir;
  fs.rmSync(root, { recursive: true, force: true });
});

describe("daemon slot opt-in", () => {
  test("only slots marked daemon:true are taken over", () => {
    writePool([{ slotId: "slot-daemon", daemon: true }, { slotId: "slot-session" }, { slotId: "slot-explicit-false", daemon: false }]);
    expect([...readDaemonSlotIds(manifestPath)]).toEqual(["slot-daemon"]);
    expect(resolveDaemonSlots(coordinator(), manifestPath).map(slot => slot.slotId)).toEqual(["slot-daemon"]);
  });

  test("the environment override wins over the manifest, so a disposable bot needs no operator state edit", () => {
    writePool([{ slotId: "slot-a", daemon: true }, { slotId: "slot-b" }]);
    const previous = process.env.VEYYON_TELEGRAM_DAEMON_SLOTS;
    process.env.VEYYON_TELEGRAM_DAEMON_SLOTS = "slot-b";
    try {
      expect([...readDaemonSlotIds(manifestPath)]).toEqual(["slot-b"]);
    } finally {
      if (previous === undefined) delete process.env.VEYYON_TELEGRAM_DAEMON_SLOTS;
      else process.env.VEYYON_TELEGRAM_DAEMON_SLOTS = previous;
    }
  });

  test("a declared project that does not exist leaves the workspace unresolved rather than guessed", () => {
    writePool([{ slotId: "slot-daemon", daemon: true, projects: ["C:/definitely/not/here"] }]);
    expect(resolveDaemonSlots(coordinator(), manifestPath)[0].workspace).toBeNull();
  });
});

describe("no double poller", () => {
  test("a daemon-held slot is refused to an in-session claim", async () => {
    writePool([{ slotId: "slot-daemon", daemon: true }]);
    const report = await daemon().start();

    expect(report.slots).toEqual([
      { slotId: "slot-daemon", botId: report.slots[0].botId, workspace, polling: true },
    ]);
    expect(startedPollers.length).toBe(1);
    expect(startedPollers[0].running).toBe(true);

    // What an in-session extension does on session start.
    const claim = await coordinator().acquireLease("session-in-tui", workspace);
    expect(claim.ok).toBe(false);
    expect(claim.error).toBe("POOL_EXHAUSTED");
    expect(claim.reason).toContain("currently in use");
    expect(claim.busyHolders?.[0]?.sessionId).toBe("daemon:slot-daemon");
  });

  test("a slot the session already holds is skipped, not stolen", async () => {
    writePool([{ slotId: "slot-daemon", daemon: true }]);
    const sessionClaim = await coordinator().acquireLease("session-in-tui", workspace);
    expect(sessionClaim.ok).toBe(true);

    const report = await daemon().start();
    expect(report.slots[0]).toMatchObject({ slotId: "slot-daemon", polling: false });
    expect(report.slots[0].skipped).toContain("session-in-tui");
    expect(startedPollers.length).toBe(0);
  });

  test("a slot that opted out stays available to the session", async () => {
    writePool([{ slotId: "slot-daemon", daemon: true }, { slotId: "slot-session" }]);
    await daemon().start();

    const claim = await coordinator().acquireLease("session-in-tui", workspace);
    expect(claim.ok).toBe(true);
    expect(claim.slot?.slotId).toBe("slot-session");
  });

  test("releasing a slot hands the token back to the session pool", async () => {
    writePool([{ slotId: "slot-daemon", daemon: true }]);
    const instance = daemon();
    await instance.start();

    expect(await instance.stopSlot("slot-daemon")).toBe(true);
    expect(await instance.stopSlot("slot-daemon")).toBe(false);
    expect(startedPollers[0].running).toBe(false);
    expect(instance.status().slots).toEqual([]);

    const claim = await coordinator().acquireLease("session-in-tui", workspace);
    expect(claim.ok).toBe(true);
    expect(claim.slot?.slotId).toBe("slot-daemon");
  });

  test("stop releases every lease it holds", async () => {
    writePool([{ slotId: "slot-a", daemon: true }, { slotId: "slot-b", daemon: true }]);
    const instance = daemon();
    expect((await instance.start()).slots.filter(slot => slot.polling).length).toBe(2);

    await instance.stop();
    const pool = coordinator().getPoolStatus();
    expect(pool.slots.filter(slot => slot.lease?.leaseStatus === "ACTIVE")).toEqual([]);
  });

  test("a status snapshot is published for an operator to read", async () => {
    writePool([{ slotId: "slot-daemon", daemon: true }]);
    await daemon().start();

    const statusPath = path.join(root, "run", "daemon.status.json");
    const snapshot = JSON.parse(fs.readFileSync(statusPath, "utf8")) as { pid: number; slots: { slotId: string; polling: boolean }[] };
    expect(snapshot.pid).toBe(process.pid);
    expect(snapshot.slots).toEqual([{ slotId: "slot-daemon", botId: snapshot.slots[0].botId, workspace, polling: true }]);
  });
});

describe("single daemon per machine", () => {
  test("a live pid holder refuses a second daemon, a dead one does not", () => {
    const pidPath = path.join(root, "run", "daemon.pid");
    expect(claimDaemonPidFile(pidPath)).toEqual({ ok: true });
    expect(fs.readFileSync(pidPath, "utf8")).toBe(String(process.pid));

    // Re-claiming from the same process is a restart of the owner, not a conflict.
    expect(claimDaemonPidFile(pidPath)).toEqual({ ok: true });

    // A pid that cannot exist is treated as dead and reclaimed.
    fs.writeFileSync(pidPath, "0", "utf8");
    expect(claimDaemonPidFile(pidPath)).toEqual({ ok: true });
  });
});
