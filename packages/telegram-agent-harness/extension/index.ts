/**
 * index.ts — Veyyon Telegram Session Extension (Thin Loader).
 *
 * Attaches Telegram as an alternate bidirectional I/O channel to the CURRENT
 * active Veyyon interactive root session.
 *
 * Acts as a stable loader that forwards all extension lifecycle hooks to a
 * dynamically imported runtime module (runtime.ts). Supports in-process hot reload
 * via Telegram `/reload` and Veyyon slash command `/tg-reload` without restarting
 * the host session.
 */

import type {
  ExtensionAPI,
  ExtensionCommandContext,
  ExtensionContext,
  MessageEndEvent,
  MessageUpdateEvent,
  SessionShutdownEvent,
  SessionStartEvent,
  ToolCallEvent,
} from "@veyyon/coding-agent";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { escapeHtml } from "./sanitizer";
import {
  ACTIVE_LEASE_SYMBOL,
  ACTIVE_ROOT_SYMBOL,
  type ActiveRootState,
  type GlobalTelegramState,
  isEligibleRootSession,
  isSubagent,
  TelegramRuntime,
  type TelegramRuntimeOptions,
} from "./runtime";

export {
  ACTIVE_LEASE_SYMBOL,
  ACTIVE_ROOT_SYMBOL,
  type ActiveRootState,
  type GlobalTelegramState,
  isEligibleRootSession,
  isSubagent,
};

export interface ReloadOptions {
  chatId?: string;
  interactive?: boolean;
  manifestPath?: string;
  runtimeSpecifier?: string;
  runtimeOptions?: TelegramRuntimeOptions;
}

export interface ReloadResult {
  success: boolean;
  sha?: string;
  error?: string;
}

export function getInstalledSourceSha(manifestPath?: string): string {
  const manifestFile =
    manifestPath ??
    path.join(os.homedir(), ".veyyon", "telegram", "install-manifest.json");
  try {
    if (fs.existsSync(manifestFile)) {
      const parsed = JSON.parse(fs.readFileSync(manifestFile, "utf-8")) as Record<string, unknown>;
      const candidate =
        parsed.source_sha ??
        parsed.sourceSha ??
        parsed.sha ??
        parsed.git_commit ??
        parsed.commit;
      if (typeof candidate === "string" && candidate.length > 0) {
        return candidate;
      }
    }
  } catch {}
  return "unknown";
}

let activeRuntime: TelegramRuntime | null = null;
let savedContext: ExtensionContext | null = null;
let currentApi: ExtensionAPI | null = null;
let reloadLock: Promise<unknown> = Promise.resolve();

export function getActiveRuntime(): TelegramRuntime | null {
  return activeRuntime;
}

export function setActiveRuntime(runtime: TelegramRuntime | null): void {
  activeRuntime = runtime;
}

export function setSavedContext(ctx: ExtensionContext | null): void {
  savedContext = ctx;
}

export async function loadRuntimeModule(
  specifier?: string,
): Promise<{ createRuntime: (pi: ExtensionAPI, options?: TelegramRuntimeOptions) => TelegramRuntime }> {
  const importUrl = specifier ?? `./runtime.ts?v=${Date.now()}`;
  // Dynamic import with cache-busting timestamp is REQUIRED for in-process hot reload
  // so the runtime engine invalidates its module cache on reload.
  const mod = (await import(importUrl)) as {
    createRuntime?: (pi: ExtensionAPI, options?: TelegramRuntimeOptions) => TelegramRuntime;
  };
  if (!mod || typeof mod.createRuntime !== "function") {
    throw new Error(`Module from "${importUrl}" does not export createRuntime`);
  }
  return { createRuntime: mod.createRuntime };
}

export async function reload(opts?: ReloadOptions): Promise<ReloadResult> {
  const prevLock = reloadLock;
  let releaseLock: () => void = () => {};
  reloadLock = new Promise<void>(resolve => {
    releaseLock = resolve;
  });

  try {
    await prevLock;
    return await executeReload(opts);
  } finally {
    releaseLock();
  }
}

async function executeReload(opts?: ReloadOptions): Promise<ReloadResult> {
  if (opts?.chatId && activeRuntime) {
    const allowFrom = activeRuntime.getAllowFrom();
    if (allowFrom.length > 0 && !allowFrom.includes(opts.chatId)) {
      currentApi?.logger?.warn(
        `Telegram /reload rejected from non-allowlisted chat ${opts.chatId}`,
      );
      return { success: false, error: "Unauthorized chat" };
    }
  }

  let newModule: { createRuntime: (pi: ExtensionAPI, options?: TelegramRuntimeOptions) => TelegramRuntime };
  try {
    newModule = await loadRuntimeModule(opts?.runtimeSpecifier);
  } catch (err: unknown) {
    const message = err instanceof Error ? err.message : String(err);
    currentApi?.logger?.error(`[Telegram Hot Reload] Dynamic import failed: ${message}`);
    if (opts?.chatId && activeRuntime) {
      try {
        await activeRuntime.notifyOperator(
          opts.chatId,
          `<b>Hot reload failed:</b> ${message}. Previous runtime retained.`,
        );
      } catch {}
    }
    return { success: false, error: message };
  }

  const oldRuntime = activeRuntime;
  const operatorChatId = opts?.chatId || oldRuntime?.getPrimaryChatId();

  if (oldRuntime) {
    try {
      await oldRuntime.dispose();
    } catch (err: unknown) {
      currentApi?.logger?.warn(
        `[Telegram Hot Reload] Error disposing previous runtime: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }

  if (!currentApi) {
    return { success: false, error: "ExtensionAPI not initialized" };
  }

  const newRuntime = newModule.createRuntime(currentApi, opts?.runtimeOptions);
  newRuntime.setReloadTrigger(reload);
  activeRuntime = newRuntime;

  if (savedContext) {
    try {
      await newRuntime.initSession(savedContext, { isReload: true });
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      currentApi?.logger?.error(`[Telegram Hot Reload] Failed to re-initialize session: ${message}`);
      return { success: false, error: message };
    }
  }

  const sha = getInstalledSourceSha(opts?.manifestPath);
  const targetChat = operatorChatId || newRuntime.getPrimaryChatId();
  if (targetChat) {
    try {
      await newRuntime.notifyOperator(
        targetChat,
        `<b>Telegram harness reloaded.</b> Source SHA: <code>${sha}</code>`,
      );
    } catch (notifyErr: unknown) {
      currentApi?.logger?.warn(
        `[Telegram Hot Reload] Failed to notify operator chat: ${notifyErr instanceof Error ? notifyErr.message : String(notifyErr)}`,
      );
    }
  }

  return { success: true, sha };
}

/**
 * Registers the operator-facing tools once per host load. Each call resolves the
 * runtime that is live at invocation time, so a `/reload` swapping the runtime module
 * never leaves a tool bound to a disposed channel.
 */
function registerOperatorTools(pi: ExtensionAPI): void {
  const z = pi.zod;

  const boundRuntime = (): TelegramRuntime => {
    const runtime = activeRuntime;
    if (!runtime || !runtime.getPoller()) {
      throw new Error("No active session-bound Telegram channel. Do not substitute a terminal question.");
    }
    return runtime;
  };

  pi.registerTool({
    name: "telegram_question",
    label: "Ask operator on Telegram",
    description: "Ask a clear question on the session's Telegram route, with labeled options, recommendation and prose replies. Returns only that question's answer; never grants approval. Use get/wait with its id after an interruption.",
    parameters: z.object({
      action: z.enum(["ask", "get", "wait"]).default("ask"),
      id: z.string().optional(),
      question: z.string().optional(),
      problem: z.string().optional(),
      impact: z.string().optional(),
      recommendation: z.string().optional(),
      options: z.array(z.object({ id: z.string(), label: z.string(), description: z.string().optional() })).optional(),
      details_url: z.string().optional(),
      wait: z.boolean().default(true),
    }),
    async execute(_id, params, signal, onUpdate) {
      const service = boundRuntime().getQuestions();
      if (!service) throw new Error("No active session-bound Telegram question receiver. Do not substitute a terminal question.");
      let question;
      if (params.action === "ask") {
        if (!params.question || !params.options || !params.recommendation) {
          throw new Error("Provide question, options with readable labels, and a recommended option id.");
        }
        question = await service.ask({
          ...params,
          question: params.question,
          options: params.options,
          recommendation: params.recommendation,
        });
      } else {
        if (!params.id) throw new Error("Question id is required for get/wait");
        question = await service.get(params.id);
      }
      onUpdate?.({ content: [{ type: "text", text: `Telegram question ${question.decision_id} is ${question.status}. Silence leaves it pending.` }] });
      const result = params.action === "get" || !params.wait
        ? question
        : await service.wait(question.decision_id, signal);
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: result };
    },
  });

  pi.registerTool({
    name: "telegram_message",
    label: "Send attributed Telegram message",
    description: "Send a lane update to the session's Telegram route with durable reply context. For a decision use telegram_question; do not send a question without its real labeled choices. Mark exited lanes explicitly.",
    parameters: z.object({
      text: z.string(),
      lane_id: z.string(),
      lane_state: z.enum(["active", "exited", "unknown"]),
    }),
    async execute(_id, params) {
      const runtime = boundRuntime();
      const poller = runtime.getPoller()!;
      const chat = poller.getPrimaryChatId();
      if (!chat) throw new Error("No authorized Telegram recipient");
      const sessionId = runtime.getSessionId();
      if (sessionId) runtime.getMessageContext()?.setLaneState(sessionId, params.lane_id, params.lane_state);
      const sent = await poller.sendTelegramMessage(
        chat,
        `<b>Agent · ${escapeHtml(params.lane_id)}</b>\n${params.text}`,
        undefined,
        undefined,
        { laneId: params.lane_id, laneState: params.lane_state },
      );
      if (!sent?.ok) throw new Error("Attributed message was not delivered");
      return { content: [{ type: "text", text: `Delivered message ${sent.result?.message_id}; replies return to Main with lane context.` }] };
    },
  });

  pi.registerTool({
    name: "telegram_dashboard",
    label: "Update Telegram fleet dashboard",
    description: "Refresh the one pinned Telegram dashboard with the actual lane tasks, questions waiting on the operator, and merge queue. Edits coalesce every 30 seconds; no new post per event. Supply observed state, never invent a live registry.",
    parameters: z.object({
      lanes: z.array(z.object({ name: z.string(), task: z.string(), state: z.enum(["active", "blocked", "exited"]) })),
      blockers: z.array(z.object({ question: z.string(), url: z.string().optional() })),
      mergeQueue: z.array(z.object({ title: z.string(), url: z.string(), state: z.string() })),
    }),
    async execute(_id, params) {
      const runtime = boundRuntime();
      const dashboard = runtime.getDashboard();
      if (!dashboard) throw new Error("No active Telegram dashboard");
      dashboard.set({ ...params, observedAt: Date.now() });
      const sessionId = runtime.getSessionId();
      const messageContext = runtime.getMessageContext();
      if (sessionId && messageContext) {
        for (const lane of params.lanes) {
          messageContext.setLaneState(sessionId, lane.name, lane.state === "exited" ? "exited" : "active");
        }
      }
      return { content: [{ type: "text", text: "Dashboard snapshot saved; the pinned message will update at the next coalesced refresh." }] };
    },
  });
}

export default function telegramSessionExtension(pi: ExtensionAPI): void {
  pi.setLabel("Telegram Alternate Channel");
  currentApi = pi;
  registerOperatorTools(pi);

  pi.on("session_start", async (event: SessionStartEvent, ctx: ExtensionContext) => {
    savedContext = ctx;
    if (!activeRuntime) {
      try {
        const mod = await loadRuntimeModule();
        activeRuntime = mod.createRuntime(pi);
        activeRuntime.setReloadTrigger(reload);
      } catch (err: unknown) {
        pi.logger?.error(
          `[Telegram Loader] Initial runtime load failed: ${err instanceof Error ? err.message : String(err)}`,
        );
        return;
      }

    }
    await activeRuntime.onSessionStart(event, ctx);
  });

  try {
    pi.on("session_switch", async (event: unknown, ctx: ExtensionContext) => {
      savedContext = ctx;
      await activeRuntime?.onSessionSwitch(event, ctx);
    });
  } catch (err: unknown) {
    pi.logger?.warn(
      `Telegram session_switch lifecycle event unavailable on this host: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  pi.on("message_start", async (event: { message: { role: string } }) => {
    await activeRuntime?.onMessageStart(event);
  });

  pi.on("message_update", async (event: MessageUpdateEvent) => {
    await activeRuntime?.onMessageUpdate(event);
  });

  pi.on("message_end", async (event: MessageEndEvent) => {
    await activeRuntime?.onMessageEnd(event);
  });

  pi.on("tool_call", async (event: ToolCallEvent, ctx: ExtensionContext) => {
    return await activeRuntime?.onToolCall(event, ctx);
  });

  pi.on("agent_end", async () => {
    await activeRuntime?.onAgentEnd();
  });

  pi.on("turn_end", async () => {
    await activeRuntime?.onTurnEnd();
  });

  pi.on("session_shutdown", async (event: SessionShutdownEvent) => {
    if (activeRuntime) {
      await activeRuntime.onSessionShutdown(event);
      activeRuntime = null;
    }
  });

  pi.registerCommand("telegram", {
    description: "Inspect, release, or reload Telegram bot lease for this session",
    handler: async (args: string, ctx: ExtensionCommandContext) => {
      const trimmed = args.trim().toLowerCase();
      if (trimmed === "reload") {
        ctx.ui.notify("Reloading Telegram harness runtime...", "info");
        const res = await reload({ interactive: true });
        if (res.success) {
          ctx.ui.notify(`Telegram harness reloaded (SHA: ${res.sha ?? "unknown"}).`, "info");
        } else {
          ctx.ui.notify(`Telegram harness reload failed: ${res.error ?? "unknown error"}`, "error");
        }
        return;
      }

      if (trimmed === "release") {
        if (activeRuntime) {
          await activeRuntime.dispose();
          activeRuntime = null;
          ctx.ui.notify("Telegram bot lease released.", "info");
        } else {
          ctx.ui.notify("No active Telegram bot lease.", "warning");
        }
        return;
      }

      const coordinator = activeRuntime?.getCoordinator();
      if (coordinator) {
        const poolStatus = coordinator.getPoolStatus();
        const slot = activeRuntime?.getActiveSlot();
        ctx.ui.notify(
          `Telegram Status: ${slot ? `Claimed [${slot.slotId}]` : "Unclaimed"} (Total: ${poolStatus.totalSlots}, Free: ${poolStatus.freeSlots})`,
          "info",
        );
      } else {
        ctx.ui.notify("Telegram Status: Unclaimed", "info");
      }
    },
  });

  pi.registerCommand("tg-reload", {
    description: "Hot reload Telegram harness runtime in-process",
    handler: async (_args: string, ctx: ExtensionCommandContext) => {
      ctx.ui.notify("Reloading Telegram harness runtime...", "info");
      const res = await reload({ interactive: true });
      if (res.success) {
        ctx.ui.notify(`Telegram harness reloaded (SHA: ${res.sha ?? "unknown"}).`, "info");
      } else {
        ctx.ui.notify(`Telegram harness reload failed: ${res.error ?? "unknown error"}`, "error");
      }
    },
  });
}
