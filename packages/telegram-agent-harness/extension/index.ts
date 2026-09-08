/**
 * index.ts — Veyyon Telegram Session Extension.
 *
 * Attaches Telegram as an alternate bidirectional I/O channel to the CURRENT
 * active Veyyon interactive root session (sharing the exact conversation, turn state,
 * and tools) using atomic bot leases from the shared bot pool.
 */

import type {
  AssistantMessage,
  ExtensionAPI,
  ExtensionCommandContext,
  ExtensionContext,
  MessageEndEvent,
  MessageUpdateEvent,
  SessionShutdownEvent,
  SessionStartEvent,
  ToolCallEvent,
} from "@veyyon/coding-agent";
import { BotPoolCoordinator } from "./coordinator";
import { DangerousToolGuard } from "./guard";
import { TelegramPoller } from "./poller";
import { chunkMessage, escapeHtml } from "./sanitizer";
import type { DiscoveredSlot, MessageCorrelationBridge } from "./types";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { handleInstalledCommand } from "./harness/installed-commands";
import { BunCommandRunner } from "./harness/command-runner";
import { latestSessionPng } from "./harness/session-artifacts";

export const ACTIVE_ROOT_SYMBOL = Symbol.for("veyyon.telegram.active_root");
export const ACTIVE_LEASE_SYMBOL = Symbol.for("veyyon.telegram.active_lease");

export interface ActiveRootState {
  instanceId: string;
  sessionId: string;
  slotId: string;
  pi: ExtensionAPI;
  poller: TelegramPoller;
  guard: DangerousToolGuard;
  coordinator: BotPoolCoordinator;
  activeSlot: DiscoveredSlot;
}

export interface GlobalTelegramState {
  [ACTIVE_ROOT_SYMBOL]?: ActiveRootState;
  [ACTIVE_LEASE_SYMBOL]?: {
    slotId: string;
    sessionId: string;
  };
}

export function isSubagent(ctx: ExtensionContext): boolean {
  return Boolean(
    ctx.isSubagent === true ||
    (ctx.taskDepth ?? 0) > 0 ||
    Boolean(ctx.parentTaskPrefix),
  );
}

export function isEligibleRootSession(ctx: ExtensionContext): boolean {
  return Boolean(
    ctx.hasUI &&
    ctx.isSubagent !== true &&
    (ctx.taskDepth ?? 0) === 0 &&
    !ctx.parentTaskPrefix,
  );
}

function getStatusSummary(ctx: ExtensionContext, slot: DiscoveredSlot | null, sessionId: string): string {
  const modelName = ctx.model?.id ?? "default";
  const idleState = ctx.isIdle() ? "Idle" : "Running / Streaming";
  const slotName = slot?.slotId ?? "None";
  const botId = slot?.botId ?? "Unknown";

  return [
    "📊 <b>Veyyon Session Status</b>",
    `Session ID: <code>${sessionId}</code>`,
    `Model: <code>${modelName}</code>`,
    `State: <b>${idleState}</b>`,
    `Bot Slot: <code>${slotName}</code> (Bot ID: ${botId})`,
    `Directory: <code>${escapeHtml(ctx.cwd)}</code>`,
  ].join("\n");
}

export default function telegramSessionExtension(pi: ExtensionAPI): void {
  pi.setLabel("Telegram Alternate Channel");

  const instanceId = crypto.randomUUID();
  const globalState = globalThis as unknown as GlobalTelegramState;

  let streamDebounceTimer: Timer | null = null;
  let sentTelegramMessageIds: number[] = [];
  let streamedChunks: string[] = [];
  let accumulatedAssistantText = "";

  let outboundQueue: Promise<void> = Promise.resolve();

  function queueOutbound(task: () => Promise<void>): Promise<void> {
    const next = outboundQueue.then(task, task);
    outboundQueue = next;
    return next;
  }


  async function syncAssistantOutput(targetText: string): Promise<void> {
    return queueOutbound(async () => {
      const root = globalState[ACTIVE_ROOT_SYMBOL];
      if (!root || root.instanceId !== instanceId || !targetText.trim()) return;

      const primaryChat = root.poller.getPrimaryChatId();
      if (!primaryChat) return;

      const chunks = chunkMessage(targetText, 3800);
      for (let i = 0; i < chunks.length; i++) {
        const chunk = chunks[i];
        if (i < sentTelegramMessageIds.length) {
          if (chunk !== streamedChunks[i]) {
            await root.poller.editTelegramMessage(primaryChat, sentTelegramMessageIds[i], escapeHtml(chunk));
            streamedChunks[i] = chunk;
          }
        } else {
          const res = await root.poller.sendTelegramMessage(primaryChat, escapeHtml(chunk));
          if (res?.ok && typeof res.result?.message_id === "number") {
            sentTelegramMessageIds.push(res.result.message_id);
            streamedChunks.push(chunk);
          }
        }
      }
    });
  }

  /**
   * Re-points this root's lease at `nextSessionId`, or gives the channel up.
   *
   * Only a lease this process still holds may carry a new session identity. If the pool
   * reclaimed it, this root stops polling a bot it no longer owns and stops being the
   * active root, rather than binding its outbound messages to a session the pool has
   * already reassigned. Every re-entry path shares this rule: which lifecycle event the
   * new session id arrived through must not decide whether ownership is rechecked.
   *
   * Returns true when the lease still belongs to this process.
   */
  function repointLeaseOrRelinquish(
    root: ActiveRootState,
    nextSessionId: string,
    projectCwd: string,
  ): boolean {
    const repointed = root.coordinator.updateLeaseSession(
      root.activeSlot.slotId,
      nextSessionId,
      projectCwd,
      process.pid,
    );

    if (!repointed) {
      pi.logger.warn(
        `Telegram bot lease on slot ${root.activeSlot.slotId} is no longer held by this process; releasing the channel instead of re-pointing it to session ${nextSessionId}.`,
      );
      root.poller.stop();
      root.coordinator.close();
      delete globalState[ACTIVE_ROOT_SYMBOL];
      delete globalState[ACTIVE_LEASE_SYMBOL];
      return false;
    }

    root.sessionId = nextSessionId;
    globalState[ACTIVE_LEASE_SYMBOL] = {
      slotId: root.activeSlot.slotId,
      sessionId: nextSessionId,
    };
    return true;
  }

  // --------------------------------------------------------------------------
  // Lifecycle: Session Start (Acquire Lease & Start Poller)
  // --------------------------------------------------------------------------
  pi.on("session_start", async (_event: SessionStartEvent, ctx: ExtensionContext) => {
    if (!isEligibleRootSession(ctx)) {
      return;
    }

    const newSessionId = ctx.sessionManager.getSessionId();
    const existingRoot = globalState[ACTIVE_ROOT_SYMBOL];

    if (existingRoot) {
      if (existingRoot.instanceId === instanceId || existingRoot.sessionId === newSessionId) {
        if (existingRoot.activeSlot && existingRoot.sessionId !== newSessionId) {
          repointLeaseOrRelinquish(existingRoot, newSessionId, ctx.cwd);
        }
        return;
      }
      return;
    }

    const coordinator = new BotPoolCoordinator();
    const claim = await coordinator.acquireLease(newSessionId, ctx.cwd, process.pid);

    if (!claim.ok || !claim.slot) {
      pi.logger.warn(
        `Telegram bot lease not acquired for session ${newSessionId}: ${claim.reason || "Pool busy"}`,
      );
      coordinator.close();
      return;
    }

    const activeSlot = claim.slot;
    const token = coordinator.readRawTokenForSlot(activeSlot.stateDir);

    if (!token) {
      coordinator.releaseLease(activeSlot.slotId, newSessionId, process.pid);
      coordinator.close();
      pi.logger.warn(`Telegram bot token missing for slot ${activeSlot.slotId}. Released lease.`);
      return;
    }

    const guard = new DangerousToolGuard(activeSlot.stateDir);
    const accessConfig = coordinator.readAccessConfig(activeSlot.stateDir);

    // The session that owns this channel can change while the lease is held (an
    // in-TUI session switch), so the correlation surface resolves it on every call
    // instead of capturing the session id from session_start.
    const currentSessionId = (): string => {
      const root = globalState[ACTIVE_ROOT_SYMBOL];
      return root && root.instanceId === instanceId ? root.sessionId : newSessionId;
    };

    const correlationBridge: MessageCorrelationBridge = {
      getSessionId: currentSessionId,
      getSlotId: () => activeSlot.slotId,
      record: correlation => {
        coordinator.recordOutboundMessage(correlation);
      },
      resolveReply: (botId, chatId, replyToMessageId) =>
        coordinator.resolveReplyRouting(botId, chatId, replyToMessageId, currentSessionId()),
      resolveCallback: (callbackToken, userId, chatId) =>
        coordinator.validateDecisionCallback(callbackToken, userId, chatId, currentSessionId()),
      consumeCallback: callbackToken =>
        coordinator.consumeDecisionCallback(callbackToken),
    };

    try {
      const poller = new TelegramPoller(
        token,
        activeSlot.stateDir,
        accessConfig,
        {
          isIdle: () => ctx.isIdle(),
          onUserMessage: text => {
            if (guard) guard.startTelegramTurn();
            pi.sendUserMessage(text);
          },
          onFollowUp: text => {
            if (guard) guard.startTelegramTurn();
            pi.sendUserMessage(text, { deliverAs: "followUp" });
          },
          onSteer: text => {
            if (guard) guard.startTelegramTurn();
            pi.sendUserMessage(text, { deliverAs: "steer" });
          },
          onAbort: () => {
            ctx.abort();
          },
          onRelease: async () => {
            const root = globalState[ACTIVE_ROOT_SYMBOL];
            if (root && root.instanceId === instanceId) {
              root.poller.stop();
              root.coordinator.releaseLease(root.activeSlot.slotId, root.sessionId, process.pid);
              root.coordinator.close();
              delete globalState[ACTIVE_ROOT_SYMBOL];
              delete globalState[ACTIVE_LEASE_SYMBOL];
            }
          },
          getStatusText: () => getStatusSummary(ctx, activeSlot, currentSessionId()),
          onHarnessCommand: (text, chatId) => handleInstalledCommand(text, {
            session: () => ({ id: currentSessionId(), cwd: ctx.cwd, idle: ctx.isIdle() }),
            send: async html => {
              const sent = await poller.sendTelegramMessage(chatId, html);
              if (!sent?.ok) throw new Error("Telegram delivery failed");
            },
            photo: (file, caption) => poller.sendTelegramPhoto(chatId, file, caption),
            latestPng: async id => {
              if (id !== currentSessionId()) return null;
              const sessionFile = ctx.sessionManager.getSessionFile();
              return sessionFile ? latestSessionPng(sessionFile) : null;
            },
            inbound: async (message, idle) => {
              guard.startTelegramTurn();
              if (idle) pi.sendUserMessage(message);
              else pi.sendUserMessage(message, { deliverAs: "steer" });
            },
          }, new BunCommandRunner()),
          onTelegramTurnStart: () => {
            if (guard) guard.startTelegramTurn();
          },
          onDecisionCallback: async (decisionId, choiceId, context) => {
            if (guard) guard.startTelegramTurn();
            try {
              const workflowScript = path.join(os.homedir(), ".veyyon", "workflows", "decision_workflow.py");
              if (fs.existsSync(workflowScript)) {
                const proc = Bun.spawn([
                  "python",
                  workflowScript,
                  "resolve-callback",
                  "--id",
                  decisionId,
                  "--choice",
                  choiceId,
                  "--token",
                  `telegram-session-${currentSessionId()}`,
                  "--session",
                  currentSessionId(),
                  "--json",
                ]);
                await proc.exited;
              }
            } catch (err: unknown) {
              pi.logger.warn(`Could not trigger canonical decision resolution: ${String(err)}`);
            }

            const promptText = [
              `[Telegram Decision Received] Operator selected Option ${choiceId} for decision '${decisionId}'.`,
              context ? `Context: ${context}` : "",
              `Decision blocker has been cleared in request ledger. Resuming authorized work.`,
            ].filter(Boolean).join("\n");

            pi.sendUserMessage(promptText, { deliverAs: "followUp" });
          },
          onLedgerFailure: message => {
            pi.logger.warn(
              `Telegram inbound ledger failure on slot ${activeSlot.slotId}: ${message}. Inbound updates are not being recorded; delivery is stalled until this clears.`,
            );
          },
        },
        correlationBridge,
      );

      globalState[ACTIVE_ROOT_SYMBOL] = {
        instanceId,
        sessionId: newSessionId,
        slotId: activeSlot.slotId,
        pi,
        poller,
        guard,
        coordinator,
        activeSlot,
      };

      globalState[ACTIVE_LEASE_SYMBOL] = {
        slotId: activeSlot.slotId,
        sessionId: newSessionId,
      };

      void poller.start();

      const primaryChatId = poller.getPrimaryChatId();
      if (primaryChatId) {
        const greeting = [
          "🟢 <b>Veyyon Session Connected</b>",
          `Session: <code>${newSessionId}</code>`,
          `Model: <code>${ctx.model?.id ?? "default"}</code>`,
          `Slot: <code>${activeSlot.slotId}</code>`,
          `Project: <code>${escapeHtml(ctx.cwd)}</code>`,
        ].join("\n");
        await poller.sendTelegramMessage(primaryChatId, greeting);
      }
    } catch {
      coordinator.releaseLease(activeSlot.slotId, newSessionId, process.pid);
      coordinator.close();
      delete globalState[ACTIVE_ROOT_SYMBOL];
      delete globalState[ACTIVE_LEASE_SYMBOL];
    }
  });

  // --------------------------------------------------------------------------
  // Lifecycle: In-TUI Session Switch (Re-point Lease & Correlation Identity)
  // --------------------------------------------------------------------------
  // Registration is guarded because a host that predates this lifecycle event must
  // still load the extension; the lease then stays pinned until session_shutdown.
  try {
    pi.on("session_switch", async (_event: unknown, ctx: ExtensionContext) => {
      const root = globalState[ACTIVE_ROOT_SYMBOL];
      if (!root || root.instanceId !== instanceId) return;

      const switchedSessionId = ctx.sessionManager.getSessionId();
      if (!switchedSessionId || switchedSessionId === root.sessionId) return;

      repointLeaseOrRelinquish(root, switchedSessionId, ctx.cwd);
    });
  } catch (err: unknown) {
    pi.logger.warn(
      `Telegram session_switch lifecycle event unavailable on this host: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  // --------------------------------------------------------------------------
  // Outbound Assistant Message Streaming & Mirroring (Multi-Chunk Safe)
  // --------------------------------------------------------------------------
  pi.on("message_start", async event => {
    const root = globalState[ACTIVE_ROOT_SYMBOL];
    if (!root || root.instanceId !== instanceId) return;
    if (event.message.role === "assistant") {
      accumulatedAssistantText = "";
      sentTelegramMessageIds = [];
      streamedChunks = [];
    }
  });

  pi.on("message_update", async (event: MessageUpdateEvent) => {
    const root = globalState[ACTIVE_ROOT_SYMBOL];
    if (!root || root.instanceId !== instanceId || event.message.role !== "assistant") return;
    const streamEvent = event.assistantMessageEvent;

    if (streamEvent.type === "text_delta" && streamEvent.delta) {
      accumulatedAssistantText += streamEvent.delta;

      if (!streamDebounceTimer) {
        streamDebounceTimer = setTimeout(async () => {
          streamDebounceTimer = null;
          await syncAssistantOutput(accumulatedAssistantText);
        }, 1500);

        if (typeof streamDebounceTimer.unref === "function") {
          streamDebounceTimer.unref();
        }
      }
    }
  });

  pi.on("message_end", async (event: MessageEndEvent) => {
    const root = globalState[ACTIVE_ROOT_SYMBOL];
    if (!root || root.instanceId !== instanceId || event.message.role !== "assistant") return;

    if (streamDebounceTimer) {
      clearTimeout(streamDebounceTimer);
      streamDebounceTimer = null;
    }

    const assistantMsg = event.message as AssistantMessage;
    const fullText = assistantMsg.content
      .filter((c): c is { type: "text"; text: string } => c.type === "text")
      .map(c => c.text)
      .join("\n");

    if (fullText.trim()) {
      await syncAssistantOutput(fullText);
    }

    sentTelegramMessageIds = [];
    streamedChunks = [];
    accumulatedAssistantText = "";
  });

  // Assistant replies carry actionable explanations; raw tool lifecycle events stay local.

  pi.on("tool_call", async (event: ToolCallEvent) => {
    const root = globalState[ACTIVE_ROOT_SYMBOL];
    if (!root || root.instanceId !== instanceId || !root.guard) return;

    const evaluation = root.guard.evaluateToolCall(
      event.toolName,
      event.input as Record<string, unknown>,
    );

    if (!evaluation.allowed) {
      return {
        block: true,
        reason: evaluation.reason ?? "Production-sensitive operation blocked for remote turn.",
      };
    }
  });


  pi.on("agent_end", async () => {
    const root = globalState[ACTIVE_ROOT_SYMBOL];
    if (!root || root.instanceId !== instanceId) return;
    root.guard.endTurn();
  });

  pi.on("turn_end", async () => {
    const root = globalState[ACTIVE_ROOT_SYMBOL];
    if (!root || root.instanceId !== instanceId) return;
    root.guard.endTurn();
  });

  // --------------------------------------------------------------------------
  // Teardown: Session Shutdown (Within 2s budget)
  // --------------------------------------------------------------------------
  pi.on("session_shutdown", async (_event: SessionShutdownEvent) => {
    const root = globalState[ACTIVE_ROOT_SYMBOL];
    if (!root || root.instanceId !== instanceId) return;

    if (streamDebounceTimer) {
      clearTimeout(streamDebounceTimer);
      streamDebounceTimer = null;
    }

    root.poller.stop();
    root.coordinator.releaseLease(root.activeSlot.slotId, root.sessionId, process.pid);
    root.coordinator.close();

    delete globalState[ACTIVE_ROOT_SYMBOL];
    delete globalState[ACTIVE_LEASE_SYMBOL];
  });

  // --------------------------------------------------------------------------
  // CLI / TUI Commands
  // --------------------------------------------------------------------------
  pi.registerCommand("telegram", {
    description: "Inspect or release Telegram bot lease for this session",
    handler: async (args: string, ctx: ExtensionCommandContext) => {
      const trimmed = args.trim().toLowerCase();
      const root = globalState[ACTIVE_ROOT_SYMBOL];

      if (trimmed === "release") {
        if (root && root.instanceId === instanceId) {
          root.poller.stop();
          root.coordinator.releaseLease(root.activeSlot.slotId, root.sessionId, process.pid);
          root.coordinator.close();
          delete globalState[ACTIVE_ROOT_SYMBOL];
          delete globalState[ACTIVE_LEASE_SYMBOL];
          ctx.ui.notify("Telegram bot lease released.", "info");
        } else {
          ctx.ui.notify("No active Telegram bot lease.", "warning");
        }
        return;
      }

      // Default: status
      const coordinator = new BotPoolCoordinator();
      try {
        const poolStatus = coordinator.getPoolStatus();
        ctx.ui.notify(
          `Telegram Status: ${root ? `Claimed [${root.slotId}]` : "Unclaimed"} (Total: ${poolStatus.totalSlots}, Free: ${poolStatus.freeSlots})`,
          "info",
        );
      } finally {
        coordinator.close();
      }
    },
  });
}
