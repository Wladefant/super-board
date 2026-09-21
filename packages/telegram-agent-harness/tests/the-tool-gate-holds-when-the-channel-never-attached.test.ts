/**
 * WHY: the tool gate is the only thing standing between a Telegram-driven agent and a
 * destructive shell command, and it used to switch itself off. `onToolCall` returned
 * early unless a poller was attached, but `initSession` installs the guard BEFORE it
 * builds the poller and leaves the guard in place when a later step fails — a slot whose
 * `access.json` carries a malformed `message_thread_id` takes exactly that path. The
 * session then ran with a live guard, no channel, and every dangerous command allowed.
 *
 * Class closed: a missing channel costs the operator the approval card, never the block.
 * The gate judges on the guard alone; delivery is best effort and its failure is named in
 * the block reason so the agent does not wait on a card nobody received.
 */

import { afterEach, beforeEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext, ToolCallEvent } from "@veyyon/coding-agent";
import { TelegramRuntime } from "../extension/runtime";
import type { BotPoolCoordinator } from "../extension/bot-pool";

let root: string, stateDir: string, workspace: string;

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "guard-channel-down-"));
  stateDir = path.join(root, "slot-1");
  workspace = path.join(root, "project");
  fs.mkdirSync(stateDir, { recursive: true });
  fs.mkdirSync(workspace, { recursive: true });
});

afterEach(() => {
  fs.rmSync(root, { recursive: true, force: true });
});

/** Leases the slot and hands back its token; everything past that is the runtime's job. */
function stubCoordinator(): BotPoolCoordinator {
  const stub = {
    acquireLease: async () => ({ ok: true, slot: { slotId: "slot-1", botId: "bot-1", stateDir, fingerprint: "fp", preferredProjects: [workspace], enabled: true } }),
    readRawTokenForSlot: () => "1000000001:AAtoken",
    readAccessConfig: () => ({ dmPolicy: "allowlist", allowFrom: ["1"] }),
    releaseLease: () => {},
    close: () => {},
  };
  return stub as unknown as BotPoolCoordinator;
}

function host(): ExtensionAPI {
  const api = {
    setLabel: () => {},
    on: () => {},
    registerCommand: () => {},
    sendUserMessage: () => {},
    abortActiveTurn: async () => {},
    logger: { info: () => {}, warn: () => {}, error: () => {}, debug: () => {} },
  };
  return api as unknown as ExtensionAPI;
}

function session(): ExtensionContext {
  const ctx = {
    hasUI: true,
    taskDepth: 0,
    cwd: workspace,
    agentId: "Main",
    isIdle: () => true,
    sessionManager: { getSessionId: () => "session-channel-down" },
  };
  return ctx as unknown as ExtensionContext;
}

function toolCall(input: Record<string, unknown>): ToolCallEvent {
  const event = { toolName: "bash", toolCallId: "call-1", input };
  return event as unknown as ToolCallEvent;
}

test("a slot that never attaches its channel still gates dangerous commands", async () => {
  fs.writeFileSync(path.join(stateDir, "access.json"), JSON.stringify({ dmPolicy: "allowlist", allowFrom: ["1"], message_thread_id: "general" }));
  const runtime = new TelegramRuntime(host(), { coordinatorFactory: stubCoordinator });

  // The malformed thread id aborts attachment after the guard is installed.
  expect(await runtime.initSession(session())).toBe(false);
  expect(runtime.getPoller()).toBeNull();

  const blocked = await runtime.onToolCall(toolCall({ command: "git push --force origin main", i: "push" }), session());
  expect(blocked?.block).toBe(true);
  expect(blocked?.reason).toContain("could not be delivered");

  // Production is refused on the same silent channel.
  const production = await runtime.onToolCall(toolCall({ command: "ssh akamai-iad-prod", i: "deploy" }), session());
  expect(production?.block).toBe(true);

  // Everyday work is untouched: a down channel gates nothing that was not dangerous.
  expect(await runtime.onToolCall(toolCall({ command: "bun test", i: "test" }), session())).toBeUndefined();
});

test("a session that never leased a slot has no guard and gates nothing", async () => {
  const runtime = new TelegramRuntime(host(), {
    coordinatorFactory: () => {
      const stub = { acquireLease: async () => ({ ok: false, reason: "Pool busy" }), close: () => {} };
      return stub as unknown as BotPoolCoordinator;
    },
  });

  expect(await runtime.initSession(session())).toBe(false);
  expect(await runtime.onToolCall(toolCall({ command: "git push --force origin main", i: "push" }), session())).toBeUndefined();
});
