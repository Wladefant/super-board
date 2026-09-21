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
let operatorReleased = false;
let lastRebindAttemptAt = 0;
const REBIND_MIN_INTERVAL_MS = 60_000;

/**
 * Rebinds the channel when the runtime was disposed while the host session is
 * still alive (observed 2026-09-20: lease RELEASED mid-session, every
 * telegram_* tool failing with "No active session-bound Telegram channel" and
 * inbound silently dropped until an operator typed /tg-reload). Skipped after an
 * explicit `/telegram release`; a failed claim (pool busy) retries on a later turn.
 */
async function ensureChannelBound(reason: string): Promise<void> {
  if (operatorReleased || !savedContext || !currentApi) return;
  if (activeRuntime?.getPoller()) return;
  const now = Date.now();
  if (now - lastRebindAttemptAt < REBIND_MIN_INTERVAL_MS) return;
  lastRebindAttemptAt = now;
  currentApi.logger?.warn(`[Telegram Loader] Channel unbound at ${reason}; attempting automatic rebind.`);
  const res = await reload({ interactive: false });
  if (res.success && activeRuntime?.getPoller()) {
    currentApi.logger?.info?.(`[Telegram Loader] Channel rebound automatically (SHA: ${res.sha ?? "unknown"}).`);
  } else {
    currentApi.logger?.warn(
      `[Telegram Loader] Automatic rebind did not attach a channel: ${res.error ?? "lease not acquired"}.`,
    );
  }
}

export function getActiveRuntime(): TelegramRuntime | null {
  return activeRuntime;
}

export function setActiveRuntime(runtime: TelegramRuntime | null): void {
  activeRuntime = runtime;
}

export function setSavedContext(ctx: ExtensionContext | null): void {
  savedContext = ctx;
}

/**
 * The active root this loader's CURRENT runtime owns, or null.
 *
 * Tools are registered once per extension load and outlive every hot reload, while
 * the runtime holding the channel is replaced on each one. So ownership cannot be
 * a captured instance id: it is re-derived from whichever runtime is loaded now.
 * A retained handler belonging to an extension that is no longer active — its
 * runtime disposed, or another instance's runtime holding the process-global root —
 * therefore resolves null instead of borrowing that root's question service,
 * message route or dashboard.
 */
export interface LastKnownRoute {
  chatId?: string;
  slotId?: string;
  sessionId?: string;
  unboundAt?: number;
  unboundReason?: string;
}

export let lastKnownRoute: LastKnownRoute | null = null;

export function recordLastKnownRoute(route: Partial<LastKnownRoute>): void {
  lastKnownRoute = { ...lastKnownRoute, ...route };
}

export function recordRouteUnbound(reason: string): void {
  if (lastKnownRoute) {
    lastKnownRoute.unboundReason = reason;
    lastKnownRoute.unboundAt = Date.now();
  } else {
    lastKnownRoute = { unboundReason: reason, unboundAt: Date.now() };
  }
}

export function ownedRoot(): ActiveRootState | null {
  const runtime = activeRuntime;
  if (!runtime) return null;
  const root = (globalThis as unknown as GlobalTelegramState)[ACTIVE_ROOT_SYMBOL];
  if (root && root.instanceId === runtime.instanceId) {
    recordLastKnownRoute({
      chatId: root.poller.getPrimaryChatId() ?? undefined,
      slotId: root.activeSlot.slotId,
      sessionId: root.sessionId,
      unboundReason: undefined,
    });
    return root;
  }
  return null;
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
      const initialized = await newRuntime.initSession(savedContext, { isReload: true });
      if (!initialized) {
        const errorMsg = "Failed to re-initialize Telegram session after reload: bot lease not acquired or session not eligible.";
        currentApi?.logger?.error(`[Telegram Hot Reload] ${errorMsg}`);
        recordRouteUnbound(errorMsg);
        return { success: false, error: errorMsg };
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      currentApi?.logger?.error(`[Telegram Hot Reload] Failed to re-initialize session: ${message}`);
      recordRouteUnbound(`Reload failed: ${message}`);
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
 * Registers the operator-facing tools on the host.
 *
 * They live on the loader, not the runtime: a hot reload replaces the runtime but
 * must not re-register a tool name the host already holds. Each handler resolves
 * its channel through `ownedRoot()` on every call instead of capturing one.
 */
export function registerOperatorTools(pi: ExtensionAPI): void {
  const z = pi.zod;
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
      timeout: z.number().optional(),
    }),
    async execute(_id, params, signal, onUpdate) {
      let root = ownedRoot();
      if (!root && savedContext && activeRuntime) {
        try {
          await activeRuntime.initSession(savedContext, { isReload: true });
          root = ownedRoot();
        } catch {}
      }
      if (!root || !root.questions) {
        const routeInfo = lastKnownRoute
          ? ` (last bound to chat ${lastKnownRoute.chatId ?? "unknown"}, slot ${lastKnownRoute.slotId ?? "unknown"}, unbound: ${lastKnownRoute.unboundReason ?? "unknown"})`
          : "";
        currentApi?.logger?.warn(`Telegram question receiver unavailable${routeInfo}`);
        throw new Error(`No active session-bound Telegram question receiver${routeInfo}. Call telegram_message with rebind: true to re-acquire the channel. Do not substitute a terminal question.`);
      }
      const service = root.questions;
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
      const timeoutMs = (params.timeout ? Math.max(1, Math.min(300, params.timeout)) : 60) * 1000;
      let lastProgressSec = 0;
      const result = params.action === "get" || !params.wait
        ? question
        : await service.wait(
            question.decision_id,
            signal,
            200,
            timeoutMs,
            elapsedMs => {
              const elapsedSec = Math.floor(elapsedMs / 1000);
              if (elapsedSec > 0 && elapsedSec - lastProgressSec >= 5) {
                lastProgressSec = elapsedSec;
                onUpdate?.({ content: [{ type: "text", text: `Waiting for Telegram answer (${question.decision_id}), ${elapsedSec}s elapsed...` }] });
              }
            },
          );
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
      rebind: z.boolean().optional(),
    }),
    async execute(_id, params) {
      let root = ownedRoot();
      if ((!root || params.rebind) && savedContext && activeRuntime) {
        try {
          await activeRuntime.initSession(savedContext, { isReload: true });
          root = ownedRoot();
        } catch (err: unknown) {
          currentApi?.logger?.warn(`Telegram rebind failed: ${err instanceof Error ? err.message : String(err)}`);
        }
      }
      if (!root) {
        const routeInfo = lastKnownRoute
          ? ` (last bound to chat ${lastKnownRoute.chatId ?? "unknown"}, slot ${lastKnownRoute.slotId ?? "unknown"}, unbound: ${lastKnownRoute.unboundReason ?? "unknown"})`
          : "";
        currentApi?.logger?.warn(`Telegram route unavailable${routeInfo}`);
        throw new Error(`No active session-bound Telegram channel${routeInfo}. Call telegram_message with rebind: true to re-acquire the channel. Do not substitute a terminal question.`);
      }
      const chat = root.poller.getPrimaryChatId();
      if (!chat) throw new Error("No authorized Telegram recipient");
      root.messageContext?.setLaneState(root.sessionId, params.lane_id, params.lane_state);
      const sent = await root.poller.sendTelegramMessage(
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
      const root = ownedRoot();
      if (!root || !root.dashboard) throw new Error("No active Telegram dashboard");
      root.dashboard.set({ ...params, observedAt: Date.now() });
      for (const lane of params.lanes) {
        root.messageContext?.setLaneState(root.sessionId, lane.name, lane.state === "exited" ? "exited" : "active");
      }
      return { content: [{ type: "text", text: "Dashboard snapshot saved; the pinned message will update at the next coalesced refresh." }] };
    },
  });
}

export default function telegramSessionExtension(pi: ExtensionAPI): void {
  pi.setLabel("Telegram Alternate Channel");
  currentApi = pi;

  // Guarded for the same reason session_switch is below: a host that does not offer
  // the tool-registration surface must still load the channel rather than fail to
  // attach. Absence is logged, never silent.
  try {
    registerOperatorTools(pi);
  } catch (err: unknown) {
    pi.logger?.warn(
      `Telegram operator tools not registered on this host: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
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
    if (event.message.role === "user") {
      await ensureChannelBound("message_start");
    }
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
    await ensureChannelBound("turn_end");
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
        operatorReleased = false;
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
        operatorReleased = true;
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
      operatorReleased = false;
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
