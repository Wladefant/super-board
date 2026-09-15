/**
 * runtime.ts — Dynamic Telegram Runtime implementation for Veyyon.
 *
 * Encapsulates the stateful bot coordinator, poller, dangerous tool guard,
 * and correlation bridges. Instantiated dynamically by the thin extension loader
 * in index.ts so that code updates can be hot-reloaded in-process.
 */

import type {
  AssistantMessage,
  ExtensionAPI,
  ExtensionContext,
  MessageEndEvent,
  MessageUpdateEvent,
  SessionShutdownEvent,
  SessionStartEvent,
  ToolCallEvent,
} from "@veyyon/coding-agent";
import { BotPoolCoordinator } from "./coordinator";
import { DangerousToolGuard, approveOperation } from "./guard";
import { decideApproval, parseApprovalCallback, approvalOutcome } from "./approvals";
import { TelegramPoller, type PollerCallbacks } from "./poller";
import { chunkMessage, escapeHtml, markdownToTelegramHtml } from "./sanitizer";
import type { AccessConfig, DiscoveredSlot, MessageCorrelationBridge } from "./types";
import { handleInstalledCommand, renderApprovalRequest } from "./harness/installed-commands";
import { BunCommandRunner, type CommandRunner } from "./harness/command-runner";
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

export function getStatusSummary(ctx: ExtensionContext, slot: DiscoveredSlot | null, sessionId: string): string {
  const modelName = ctx.model?.id ?? "default";
  const idleState = ctx.isIdle() ? "Idle" : "Running / Streaming";
  const slotName = slot?.slotId ?? "None";
  const botId = slot?.botId ?? "Unknown";

  return [
    "📊 <b>Veyyon Session Status</b>",
    `• <b>Session ID:</b> <code>${sessionId}</code>`,
    `• <b>Model:</b> <code>${modelName}</code>`,
    `• <b>State:</b> <b>${idleState}</b>`,
    `• <b>Bot Slot:</b> <code>${slotName}</code> (Bot ID: ${botId})`,
    `• <b>Directory:</b> <code>${escapeHtml(ctx.cwd)}</code>`,
  ].join("\n");
}

export interface TelegramRuntimeOptions {
  coordinatorFactory?: () => BotPoolCoordinator;
  pollerFactory?: (
    token: string,
    stateDir: string,
    accessConfig: AccessConfig,
    callbacks: PollerCallbacks,
    correlationBridge: MessageCorrelationBridge,
  ) => TelegramPoller;
  commandRunnerFactory?: () => CommandRunner;
}

export class TelegramRuntime {
  public readonly instanceId = crypto.randomUUID();
  private pi: ExtensionAPI;
  private options: TelegramRuntimeOptions;
  private reloadTrigger: ((opts?: { chatId?: string; interactive?: boolean }) => Promise<{ success: boolean; sha?: string; error?: string }>) | null = null;

  private streamDebounceTimer: Timer | null = null;
  private sentTelegramMessageIds: number[] = [];
  private streamedChunks: string[] = [];
  private accumulatedAssistantText = "";
  private outboundQueue: Promise<void> = Promise.resolve();

  private poller: TelegramPoller | null = null;
  private guard: DangerousToolGuard | null = null;
  private coordinator: BotPoolCoordinator | null = null;
  private activeSlot: DiscoveredSlot | null = null;
  private accessConfig: AccessConfig | null = null;
  private sessionId: string | null = null;
  private isDisposed = false;

  constructor(pi: ExtensionAPI, options: TelegramRuntimeOptions = {}) {
    this.pi = pi;
    this.options = options;
  }

  public setReloadTrigger(fn: (opts?: { chatId?: string; interactive?: boolean }) => Promise<{ success: boolean; sha?: string; error?: string }>): void {
    this.reloadTrigger = fn;
  }

  public getPoller(): TelegramPoller | null {
    return this.poller;
  }

  public getCoordinator(): BotPoolCoordinator | null {
    return this.coordinator;
  }

  public getActiveSlot(): DiscoveredSlot | null {
    return this.activeSlot;
  }

  public getSessionId(): string | null {
    return this.sessionId;
  }

  public getAllowFrom(): string[] {
    return this.accessConfig?.allowFrom ?? [];
  }

  public getPrimaryChatId(): string | null {
    if (this.poller) {
      const chat = this.poller.getPrimaryChatId();
      if (chat) return chat;
    }
    if (this.accessConfig && this.accessConfig.allowFrom.length > 0) {
      return this.accessConfig.allowFrom[0];
    }
    return null;
  }

  public async notifyOperator(chatId: string, text: string): Promise<boolean> {
    if (this.poller) {
      try {
        const res = await this.poller.sendTelegramMessage(chatId, text);
        return Boolean(res?.ok);
      } catch {
        return false;
      }
    }
    return false;
  }

  private queueOutbound(task: () => Promise<void>): Promise<void> {
    const next = this.outboundQueue.then(task, task);
    this.outboundQueue = next;
    return next;
  }

  private async syncAssistantOutput(targetText: string): Promise<void> {
    return this.queueOutbound(async () => {
      if (this.isDisposed || !this.poller || !targetText.trim()) return;

      const primaryChat = this.getPrimaryChatId();
      if (!primaryChat) return;

      const fullHtml = markdownToTelegramHtml(targetText);
      const chunks = chunkMessage(fullHtml, 3800);
      for (let i = 0; i < chunks.length; i++) {
        const chunk = chunks[i];
        if (i < this.sentTelegramMessageIds.length) {
          if (chunk !== this.streamedChunks[i]) {
            await this.poller.editTelegramMessage(primaryChat, this.sentTelegramMessageIds[i], chunk);
            this.streamedChunks[i] = chunk;
          }
        } else {
          const res = await this.poller.sendTelegramMessage(primaryChat, chunk);
          if (res?.ok && typeof res.result?.message_id === "number") {
            this.sentTelegramMessageIds.push(res.result.message_id);
            this.streamedChunks.push(chunk);
          }
        }
      }
    });
  }

  private repointLeaseOrRelinquish(
    root: ActiveRootState,
    nextSessionId: string,
    projectCwd: string,
  ): boolean {
    const globalState = globalThis as unknown as GlobalTelegramState;
    const repointed = root.coordinator.updateLeaseSession(
      root.activeSlot.slotId,
      nextSessionId,
      projectCwd,
      process.pid,
    );

    if (!repointed) {
      this.pi.logger?.warn(
        `Telegram bot lease on slot ${root.activeSlot.slotId} is no longer held by this process; releasing the channel instead of re-pointing it to session ${nextSessionId}.`,
      );
      void root.poller.stop();
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

  public async initSession(ctx: ExtensionContext, opts?: { isReload?: boolean }): Promise<boolean> {
    if (!isEligibleRootSession(ctx)) {
      return false;
    }

    const newSessionId = ctx.sessionManager.getSessionId();
    const globalState = globalThis as unknown as GlobalTelegramState;
    const existingRoot = globalState[ACTIVE_ROOT_SYMBOL];

    if (existingRoot && existingRoot.instanceId !== this.instanceId) {
      if (existingRoot.sessionId === newSessionId) {
        if (existingRoot.activeSlot && existingRoot.sessionId !== newSessionId) {
          this.repointLeaseOrRelinquish(existingRoot, newSessionId, ctx.cwd);
        }
        return false;
      }
      return false;
    }

    const coordinator = this.options.coordinatorFactory ? this.options.coordinatorFactory() : new BotPoolCoordinator();
    const claim = await coordinator.acquireLease(newSessionId, ctx.cwd, process.pid);

    if (!claim.ok || !claim.slot) {
      this.pi.logger?.warn(
        `Telegram bot lease not acquired for session ${newSessionId}: ${claim.reason || "Pool busy"}`,
      );
      coordinator.close();
      return false;
    }

    const activeSlot = claim.slot;
    const token = coordinator.readRawTokenForSlot(activeSlot.stateDir);

    if (!token) {
      coordinator.releaseLease(activeSlot.slotId, newSessionId, process.pid);
      coordinator.close();
      this.pi.logger?.warn(`Telegram bot token missing for slot ${activeSlot.slotId}. Released lease.`);
      return false;
    }

    this.coordinator = coordinator;
    this.activeSlot = activeSlot;
    this.sessionId = newSessionId;
    this.guard = new DangerousToolGuard(activeSlot.stateDir);
    this.accessConfig = coordinator.readAccessConfig(activeSlot.stateDir);

    const currentSessionId = (): string => {
      return this.sessionId ?? newSessionId;
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

    const runner = this.options.commandRunnerFactory ? this.options.commandRunnerFactory() : new BunCommandRunner();

    const pollerCallbacks = {
      isIdle: () => ctx.isIdle(),
      getSessionFile: () => ctx.sessionManager.getSessionFile(),
      onUserMessage: (text: string) => {
        if (this.guard) this.guard.startTelegramTurn();
        if (ctx.isIdle()) {
          this.pi.sendUserMessage(text);
        } else {
          this.pi.sendUserMessage(text, { deliverAs: "steer" });
        }
      },
      onSteer: (text: string) => {
        if (this.guard) this.guard.startTelegramTurn();
        this.pi.sendUserMessage(text, { deliverAs: "steer" });
      },
      onCancel: async () => {
        await this.pi.abortActiveTurn();
        const primaryChat = this.poller?.getPrimaryChatId();
        if (primaryChat && this.poller) {
          await this.poller.sendTelegramMessage(primaryChat, "🛑 <b>Turn cancelled by operator.</b>");
        }
      },
      onRelease: async () => {
        await this.dispose();
      },
      getStatusText: () => getStatusSummary(ctx, activeSlot, currentSessionId()),
      onHarnessCommand: (text: string, chatId: string, userId?: string) => handleInstalledCommand(text, {
        session: () => ({
          id: currentSessionId(),
          cwd: ctx.cwd,
          idle: ctx.isIdle(),
          model: ctx.model?.id,
          stateDir: activeSlot.stateDir,
        }),
        approve: async token => {
          if (!userId) throw new Error("Authenticated actor is missing.");
          const record = approveOperation(activeSlot.stateDir, token, { sessionId: currentSessionId(), userId, chatId });
          await this.pi.sendUserMessage(approvalOutcome(record), ctx.isIdle() ? undefined : { deliverAs: "steer" });
          return record;
        },
        send: async html => {
          if (!this.poller) throw new Error("Poller unavailable");
          const sent = await this.poller.sendTelegramMessage(chatId, html);
          if (!sent?.ok) throw new Error("Telegram delivery failed");
        },
        photo: (file, caption) => this.poller?.sendTelegramPhoto(chatId, file, caption) ?? Promise.resolve(),
        mediaGroup: (files, caption) => this.poller?.sendMediaGroup(chatId, files, caption) ?? Promise.resolve(),
        latestPng: async id => {
          if (id !== currentSessionId()) return null;
          const sessionFile = ctx.sessionManager.getSessionFile();
          return sessionFile ? latestSessionPng(sessionFile) : null;
        },
        inbound: async (message, idle) => {
          if (this.guard) this.guard.startTelegramTurn();
          if (idle) this.pi.sendUserMessage(message);
          else this.pi.sendUserMessage(message, { deliverAs: "steer" });
        },
        reload: async () => {
          if (this.reloadTrigger) {
            await this.reloadTrigger({ chatId, interactive: true });
          } else if (this.poller) {
            await this.poller.sendTelegramMessage(chatId, "<b>Hot reload unavailable.</b> Reload trigger not configured.");
          }
        },
      }, runner),
      onApprovalCallback: async (data: string, userId: string, chatId: string, sessionId: string) => {
        const selection = parseApprovalCallback(data);
        if (!selection || sessionId !== currentSessionId()) throw new Error("Invalid or foreign-session approval callback.");
        const record = decideApproval(activeSlot.stateDir, selection.token, selection.decision, { sessionId, userId, chatId });
        await this.pi.sendUserMessage(approvalOutcome(record), ctx.isIdle() ? undefined : { deliverAs: "steer" });
        return record.state === "denied" ? "Denied. The requester was told not to run this operation." : `Approved once. Requester notified; identical retry expires ${record.expiresAt}.`;
      },
      onTelegramTurnStart: () => {
        if (this.guard) this.guard.startTelegramTurn();
      },
      onDecisionCallback: async (decisionId: string, choiceId: string, context?: string) => {
        if (this.guard) this.guard.startTelegramTurn();
        const decisionSessionId = currentSessionId();
        const canonicalResolved = await this.coordinator?.applyDecisionAnswer(
          decisionSessionId,
          decisionId,
          choiceId,
          `telegram:operator:${activeSlot.slotId}`,
        );
        if (!canonicalResolved) {
          const decisionPayload = `Decision recorded from Telegram: question=${decisionId} choice=${choiceId}\n${context || ""}`.trim();
          if (ctx.isIdle()) {
            this.pi.sendUserMessage(decisionPayload);
          } else {
            this.pi.sendUserMessage(decisionPayload, { deliverAs: "steer" });
          }
        }
      },
      onLedgerFailure: (message: string) => {
        this.pi.logger?.warn(
          `Telegram inbound ledger failure on slot ${activeSlot.slotId}: ${message}. Inbound updates are not being recorded; delivery is stalled until this clears.`,
        );
      },
    };

    const poller = this.options.pollerFactory
      ? this.options.pollerFactory(token, activeSlot.stateDir, this.accessConfig, pollerCallbacks, correlationBridge)
      : new TelegramPoller(token, activeSlot.stateDir, this.accessConfig, pollerCallbacks, correlationBridge);

    this.poller = poller;

    globalState[ACTIVE_ROOT_SYMBOL] = {
      instanceId: this.instanceId,
      sessionId: newSessionId,
      slotId: activeSlot.slotId,
      pi: this.pi,
      poller,
      guard: this.guard,
      coordinator,
      activeSlot,
    };

    globalState[ACTIVE_LEASE_SYMBOL] = {
      slotId: activeSlot.slotId,
      sessionId: newSessionId,
    };

    void poller.start();

    if (!opts?.isReload) {
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
    }

    return true;
  }

  public async onSessionStart(_event: SessionStartEvent, ctx: ExtensionContext): Promise<void> {
    await this.initSession(ctx, { isReload: false });
  }

  public async onSessionSwitch(_event: unknown, ctx: ExtensionContext): Promise<void> {
    const globalState = globalThis as unknown as GlobalTelegramState;
    const root = globalState[ACTIVE_ROOT_SYMBOL];
    if (!root || root.instanceId !== this.instanceId) return;

    const switchedSessionId = ctx.sessionManager.getSessionId();
    if (!switchedSessionId || switchedSessionId === this.sessionId) return;

    this.repointLeaseOrRelinquish(root, switchedSessionId, ctx.cwd);
  }

  public async onMessageStart(event: { message: { role: string } }): Promise<void> {
    if (event.message.role === "assistant") {
      this.accumulatedAssistantText = "";
      this.sentTelegramMessageIds = [];
      this.streamedChunks = [];
    }
  }

  public async onMessageUpdate(event: MessageUpdateEvent): Promise<void> {
    if (event.message.role !== "assistant") return;
    const streamEvent = event.assistantMessageEvent;

    if (streamEvent.type === "text_delta" && streamEvent.delta) {
      this.accumulatedAssistantText += streamEvent.delta;

      if (!this.streamDebounceTimer) {
        this.streamDebounceTimer = setTimeout(async () => {
          this.streamDebounceTimer = null;
          await this.syncAssistantOutput(this.accumulatedAssistantText);
        }, 1500);

        if (typeof this.streamDebounceTimer.unref === "function") {
          this.streamDebounceTimer.unref();
        }
      }
    }
  }

  public async onMessageEnd(event: MessageEndEvent): Promise<void> {
    if (event.message.role !== "assistant") return;

    if (this.streamDebounceTimer) {
      clearTimeout(this.streamDebounceTimer);
      this.streamDebounceTimer = null;
    }

    const assistantMsg = event.message as AssistantMessage;
    const fullText = assistantMsg.content
      .filter((c): c is { type: "text"; text: string } => c.type === "text")
      .map(c => c.text)
      .join("\n");

    if (fullText.trim()) {
      await this.syncAssistantOutput(fullText);
    }

    this.sentTelegramMessageIds = [];
    this.streamedChunks = [];
    this.accumulatedAssistantText = "";
  }

  public async onToolCall(event: ToolCallEvent, ctx: ExtensionContext): Promise<{ block: boolean; reason: string } | void> {
    if (!this.guard || !this.sessionId || !this.poller) return;

    const evaluation = this.guard.evaluateToolCall(
      event.toolName,
      event.input as Record<string, unknown>,
      undefined,
      {
        sessionId: this.sessionId,
        requester: ctx.agentId ?? "Main (interactive root agent)",
        task: String(event.input.i ?? event.input.title ?? `Run ${event.toolName}; no task description supplied`),
        cwd: ctx.cwd,
        toolCallId: event.toolCallId,
      },
    );

    if (!evaluation.allowed) {
      const chatId = this.poller.getPrimaryChatId();
      if (chatId && evaluation.approval) {
        const card = renderApprovalRequest(evaluation.approval);
        try {
          const chunks = chunkMessage(card.text);
          for (let n = 0; n < chunks.length; n++) {
            const sent = await this.poller.sendTelegramMessage(chatId, chunks[n], "HTML", n === chunks.length - 1 ? card.replyMarkup : undefined);
            if (!sent?.ok) break;
          }
        } catch {}
      }
      return {
        block: true,
        reason: evaluation.reason ?? "Sensitive operation requires operator approval.",
      };
    }
  }

  public async onAgentEnd(): Promise<void> {
    if (this.guard) this.guard.endTurn();
  }

  public async onTurnEnd(): Promise<void> {
    if (this.guard) this.guard.endTurn();
  }

  public async onSessionShutdown(_event: SessionShutdownEvent): Promise<void> {
    await this.dispose();
  }

  public async dispose(): Promise<void> {
    if (this.isDisposed) return;
    this.isDisposed = true;

    if (this.streamDebounceTimer) {
      clearTimeout(this.streamDebounceTimer);
      this.streamDebounceTimer = null;
    }

    if (this.poller) {
      try {
        await this.poller.stop();
      } catch (err) {
        this.pi.logger?.warn(`Error stopping poller: ${err}`);
      }
      this.poller = null;
    }

    if (this.coordinator && this.activeSlot && this.sessionId) {
      try {
        this.coordinator.releaseLease(this.activeSlot.slotId, this.sessionId, process.pid);
      } catch (err) {
        this.pi.logger?.warn(`Error releasing lease: ${err}`);
      }
      try {
        this.coordinator.close();
      } catch (err) {
        this.pi.logger?.warn(`Error closing coordinator: ${err}`);
      }
      this.coordinator = null;
    }

    const globalState = globalThis as unknown as GlobalTelegramState;
    if (globalState[ACTIVE_ROOT_SYMBOL]?.instanceId === this.instanceId) {
      delete globalState[ACTIVE_ROOT_SYMBOL];
      delete globalState[ACTIVE_LEASE_SYMBOL];
    }
  }
}

export function createRuntime(pi: ExtensionAPI, options?: TelegramRuntimeOptions): TelegramRuntime {
  return new TelegramRuntime(pi, options);
}
