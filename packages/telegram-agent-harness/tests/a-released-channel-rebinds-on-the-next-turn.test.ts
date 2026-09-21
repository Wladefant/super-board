/**
 * WHY: 2026-09-20 16:45Z the extension runtime was disposed while its host session
 * stayed alive. The lease row flipped to RELEASED, every `telegram_*` tool refused
 * with "No active session-bound Telegram channel", inbound operator messages were
 * dropped for ~20 minutes, and the only recovery was an operator typing /tg-reload
 * in a terminal he was not watching.
 *
 * Class closed: a host session that is still taking turns while the loader holds no
 * live poller. Whatever disposed the runtime — a lease steal, a crashed poller, a
 * reload that failed to re-claim — the loader re-attempts the claim on the next user
 * turn and on turn_end, bounded to one attempt per REBIND_MIN_INTERVAL_MS, and stays
 * silent after an explicit `/telegram release`. The suite drives the real loader over
 * a temporary bot pool and a stubbed Telegram HTTP boundary, so a rebind is observed
 * as a new runtime holding a started poller, never as a spy call.
 *
 * Not covered: why the runtime was disposed in the first place (undetermined, see the
 * pull request body), the daemon-side poller, which never enters this loader, and the
 * cross-process case where another live session already holds the slot — that path
 * ends in `acquireLease` returning busy, which `initSession` already covers.
 */

import { afterAll, beforeAll, expect, spyOn, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionCommandContext, ExtensionContext } from "@veyyon/coding-agent";
import telegramSessionExtension, {
  ACTIVE_ROOT_SYMBOL,
  getActiveRuntime,
  type GlobalTelegramState,
  setActiveRuntime,
  setSavedContext,
} from "../extension/index";

const OPERATOR_CHAT = "1247617658";
const SESSION_ID = "rebind-session-1";

let tempDir: string;
let poolEnv: { VEYYON_POOL_DB?: string; VEYYON_MANIFEST_PATH?: string; VEYYON_CHANNELS_DIR?: string };
let telegramCalls: string[];
let originalFetch: typeof globalThis.fetch;
let listeners: Map<string, ((...args: unknown[]) => Promise<unknown>)[]>;
let commands: Map<string, { handler: (args: string, ctx: ExtensionCommandContext) => Promise<void> }>;

/**
 * Chainable no-op stand-in for the host's injected zod, matching
 * a-telegram-tool-serves-only-the-session-that-owns-it.test.ts: the loader only uses it
 * to describe tool parameters, which nothing here asserts on.
 */
function inertSchemaModule(): unknown {
  const node: Record<string, unknown> = {};
  for (const key of ["object", "string", "enum", "array", "boolean", "number", "optional", "default"]) {
    node[key] = () => node;
  }
  return node;
}

function createHost(): ExtensionAPI {
  listeners = new Map();
  commands = new Map();
  return {
    setLabel: () => {},
    on: (event: string, handler: (...args: unknown[]) => Promise<unknown>) => {
      const list = listeners.get(event) ?? [];
      list.push(handler);
      listeners.set(event, list);
    },
    registerCommand: (name: string, def: { handler: (args: string, ctx: ExtensionCommandContext) => Promise<void> }) => {
      commands.set(name, def);
    },
    registerTool: () => {},
    zod: inertSchemaModule(),
    sendUserMessage: () => {},
    abortActiveTurn: async () => {},
    logger: { info: () => {}, warn: () => {}, error: () => {}, debug: () => {} },
  } as unknown as ExtensionAPI;
}

function createContext(): ExtensionContext {
  return {
    hasUI: true,
    isSubagent: false,
    taskDepth: 0,
    parentTaskPrefix: undefined,
    cwd: path.join(tempDir, "project"),
    isIdle: () => true,
    sessionManager: { getSessionId: () => SESSION_ID, getSessionFile: () => null },
    model: { id: "test-model" },
  } as unknown as ExtensionContext;
}

/** One free slot the default coordinator discovers through the VEYYON_* env overrides. */
function buildPool(root: string): void {
  const channelsDir = path.join(root, "channels");
  const stateDir = path.join(channelsDir, "telegram-rebind");
  fs.mkdirSync(stateDir, { recursive: true });
  fs.writeFileSync(path.join(stateDir, ".env"), "TELEGRAM_BOT_TOKEN=1000000000:AA-rebind-test-token\n", "utf8");
  fs.writeFileSync(
    path.join(stateDir, "access.json"),
    JSON.stringify({ dmPolicy: "allowlist", allowFrom: [OPERATOR_CHAT] }),
    "utf8",
  );
  fs.writeFileSync(
    path.join(root, "manifest.json"),
    JSON.stringify({ version: 1, slots: [{ slotId: "telegram-rebind", stateDir, projects: ["*"], enabled: true }] }),
    "utf8",
  );
}

const commandContext = { ui: { notify: () => {} } } as unknown as ExtensionCommandContext;

async function fireTurnEnd(): Promise<void> {
  for (const handler of listeners.get("turn_end") ?? []) await handler();
}

async function fireMessageStart(role: "assistant" | "user"): Promise<void> {
  for (const handler of listeners.get("message_start") ?? []) await handler({ message: { role } });
}

/** Runs one hook with the loader's clock advanced past the rebind interval. */
async function atClockOffset(offsetMs: number, hook: () => Promise<void>): Promise<void> {
  const real = Date.now();
  const clock = spyOn(Date, "now").mockImplementation(() => real + offsetMs);
  try {
    await hook();
  } finally {
    clock.mockRestore();
  }
}

/** The incident shape: the runtime is disposed, the loader still holds the instance. */
async function disposeRuntimeInPlace(): Promise<void> {
  const runtime = getActiveRuntime();
  expect(runtime).not.toBeNull();
  await runtime?.dispose();
  expect(runtime?.getPoller()).toBeNull();
}

beforeAll(async () => {
  tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-rebind-"));
  fs.mkdirSync(path.join(tempDir, "project"), { recursive: true });
  buildPool(tempDir);

  poolEnv = {
    VEYYON_POOL_DB: process.env.VEYYON_POOL_DB,
    VEYYON_MANIFEST_PATH: process.env.VEYYON_MANIFEST_PATH,
    VEYYON_CHANNELS_DIR: process.env.VEYYON_CHANNELS_DIR,
  };
  process.env.VEYYON_POOL_DB = path.join(tempDir, "bot_pool.db");
  process.env.VEYYON_MANIFEST_PATH = path.join(tempDir, "manifest.json");
  process.env.VEYYON_CHANNELS_DIR = path.join(tempDir, "channels");

  telegramCalls = [];
  originalFetch = globalThis.fetch;
  let messageId = 900;
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    const target = String(url);
    telegramCalls.push(target.slice(target.lastIndexOf("/") + 1).split("?")[0] ?? target);
    if (target.includes("/getUpdates")) {
      // A real long poll: settles only when the poller aborts, so the claim is
      // observable without the poll loop spinning against the stub.
      return await new Promise<Response>((_resolve, reject) => {
        const signal = init?.signal;
        const abort = (): void => reject(new DOMException("Aborted", "AbortError"));
        if (signal?.aborted) return abort();
        signal?.addEventListener("abort", abort, { once: true });
      });
    }
    return Response.json({
      ok: true,
      result: { message_id: ++messageId, chat: { id: Number(OPERATOR_CHAT) }, date: 0 },
    });
  }) as typeof globalThis.fetch;

  telegramSessionExtension(createHost());
  setSavedContext(createContext());
  for (const handler of listeners.get("session_start") ?? []) await handler({}, createContext());
  expect(getActiveRuntime()?.getPoller()).not.toBeNull();
});

afterAll(async () => {
  await getActiveRuntime()?.dispose();
  setActiveRuntime(null);
  setSavedContext(null);
  delete (globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL];
  globalThis.fetch = originalFetch;
  for (const [key, value] of Object.entries(poolEnv)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  fs.rmSync(tempDir, { recursive: true, force: true, maxRetries: 10, retryDelay: 50 });
});

test("a runtime disposed under a live session rebinds the channel on the operator's next message", async () => {
  const released = getActiveRuntime();
  await disposeRuntimeInPlace();

  // The agent's own message is not a turn the operator is waiting on.
  await fireMessageStart("assistant");
  expect(getActiveRuntime()).toBe(released);

  await fireMessageStart("user");

  const rebound = getActiveRuntime();
  expect(rebound).not.toBe(released);
  expect(rebound?.getPoller()).not.toBeNull();
  expect(rebound?.getSessionId()).toBe(SESSION_ID);
});

test("a channel lost again inside the rebind interval is left alone until the interval elapses", async () => {
  const released = getActiveRuntime();
  await disposeRuntimeInPlace();
  const callsBefore = telegramCalls.length;

  await fireTurnEnd();
  await fireMessageStart("user");

  expect(getActiveRuntime()).toBe(released);
  expect(getActiveRuntime()?.getPoller()).toBeNull();
  expect(telegramCalls.length).toBe(callsBefore);

  // The stand-down ends rather than latching: the next turn past the interval rebinds.
  await atClockOffset(61_000, fireTurnEnd);

  expect(getActiveRuntime()).not.toBe(released);
  expect(getActiveRuntime()?.getPoller()).not.toBeNull();
});

test("two turn boundaries landing in the same tick produce one claim, not two", async () => {
  await disposeRuntimeInPlace();
  const claimsBefore = telegramCalls.filter(call => call === "setMyCommands").length;

  await atClockOffset(122_000, async () => {
    await Promise.all([fireTurnEnd(), fireMessageStart("user")]);
  });

  const claimsAfter = telegramCalls.filter(call => call === "setMyCommands").length;
  expect(claimsAfter - claimsBefore).toBe(1);
  expect(getActiveRuntime()?.getPoller()).not.toBeNull();
});

test("a channel the operator released stays released, and /tg-reload re-arms it", async () => {
  await commands.get("telegram")?.handler("release", commandContext);
  expect(getActiveRuntime()).toBeNull();
  const callsBefore = telegramCalls.length;

  await atClockOffset(122_000, fireTurnEnd);
  await atClockOffset(183_000, () => fireMessageStart("user"));

  expect(getActiveRuntime()).toBeNull();
  expect(telegramCalls.length).toBe(callsBefore);

  await commands.get("tg-reload")?.handler("", commandContext);
  expect(getActiveRuntime()?.getPoller()).not.toBeNull();

  // Auto-rebind is armed again: the explicit reload cleared the operator's release.
  await disposeRuntimeInPlace();
  await atClockOffset(244_000, fireTurnEnd);
  expect(getActiveRuntime()?.getPoller()).not.toBeNull();
});
