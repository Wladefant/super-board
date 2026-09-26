/**
 * runtime.ts — Dynamic Telegram Runtime implementation for Veyyon.
 *
 * Encapsulates the stateful bot coordinator, poller,
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
} from "@veyyon/coding-agent";
import { BotPoolCoordinator } from "./coordinator";
import { TelegramPoller, type PollerCallbacks, type PollerOptions } from "./poller";
import { resolveGithubRepo } from "./github-repo";
import { chunkMessage, escapeHtml, isRepeatDelivery, markdownToTelegramHtml, normalizeForDedupe } from "./sanitizer";
import type { AccessConfig, DiscoveredSlot, MessageCorrelationBridge } from "./types";
import { handleInstalledCommand } from "./harness/installed-commands";
import { BunCommandRunner, type CommandRunner } from "./harness/command-runner";
import { latestSessionPng } from "./harness/session-artifacts";
import { OperatorQuestionService } from "./harness/operator-questions";

/** A forum chat is not an operator account; callback ownership must name a user. */
function questionOperator(access: AccessConfig, chatId: string): string {
  if (!chatId.startsWith("-") && access.allowFrom.includes(chatId)) return chatId;
  const group = access.groups?.[chatId];
  const operators = group
    ? access.allowFrom.filter(id => !id.startsWith("-") && (!group.allowFrom || group.allowFrom.includes(id)))
    : [];
  if (operators.length !== 1) throw new Error("Question route requires exactly one authorized operator for this forum.");
  return operators[0];
}
import { MessageContextStore } from "./harness/message-context";
import { LiveDashboard, type DashboardSnapshot } from "./harness/live-dashboard";
import { readMessageThreadId } from "./harness/channel-config";
import * as os from "node:os";
import * as path from "node:path";
import { Database } from "bun:sqlite";
import * as fs from "node:fs";

export const ACTIVE_ROOT_SYMBOL = Symbol.for("veyyon.telegram.active_root");
export const ACTIVE_LEASE_SYMBOL = Symbol.for("veyyon.telegram.active_lease");

export interface ActiveRootState {
  instanceId: string;
  sessionId: string;
  slotId: string;
  pi: ExtensionAPI;
  poller: TelegramPoller;
  coordinator: BotPoolCoordinator;
  activeSlot: DiscoveredSlot;
  questions?: OperatorQuestionService;
  messageContext?: MessageContextStore;
  dashboard?: LiveDashboard;
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
interface DaemonRouteInfo {
  slot: DiscoveredSlot;
  token: string;
  chatId: string;
  topicId: string;
}

function findDaemonRoute(
  coordinator: BotPoolCoordinator,
  sessionId?: string,
  workspace?: string,
): DaemonRouteInfo | null {
  try {
    const dbPath = process.env.VEYYON_TELEGRAM_DAEMON_DB || path.join(os.homedir(), ".veyyon", "telegram", "daemon.db");
    if (!fs.existsSync(dbPath)) return null;
    const db = new Database(dbPath, { readonly: true });
    try {
      let row = sessionId
        ? db.query<{ slot_id: string; chat_id: string; topic_id: string }, [string]>(
            "SELECT slot_id, chat_id, topic_id FROM routes WHERE session_id = ? AND topic_id != '' LIMIT 1",
          ).get(sessionId)
        : null;
      if (!row && workspace) {
        const normWs = workspace.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
        const all = db.query<{ slot_id: string; chat_id: string; topic_id: string; workspace: string }, []>(
          "SELECT slot_id, chat_id, topic_id, workspace FROM routes WHERE topic_id != ''",
        ).all();
        row = all.find(r => r.workspace && r.workspace.replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase() === normWs) ?? null;
      }
      if (!row) return null;

      const slots = coordinator.syncSlots();
      const slot = slots.find(s => s.slotId === row!.slot_id);
      if (!slot) return null;

      const token = coordinator.readRawTokenForSlot(slot.stateDir);
      if (!token) return null;

      return {
        slot,
        token,
        chatId: row.chat_id,
        topicId: row.topic_id,
      };
    } finally {
      db.close();
    }
  } catch {
    return null;
  }
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
    options?: PollerOptions,
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
  /** Markdown delivered to Telegram since the last user message: telegram_message texts and forwarded replies. */
  private turnDeliveries: string[] = [];

  private poller: TelegramPoller | null = null;
  private coordinator: BotPoolCoordinator | null = null;
  private activeSlot: DiscoveredSlot | null = null;
  private accessConfig: AccessConfig | null = null;
  private sessionId: string | null = null;
  private questions: OperatorQuestionService | null = null;
  private messageContext: MessageContextStore | null = null;
  private dashboard: LiveDashboard | null = null;
  private isDisposed = false;
  private isDaemonClient = false;
  private cwd: string | null = null;

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

  public getQuestions(): OperatorQuestionService | null {
    return this.questions;
  }

  public getDashboard(): LiveDashboard | null {
    return this.dashboard;
  }

  public getMessageContext(): MessageContextStore | null {
    return this.messageContext;
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

  /** Records Markdown just delivered to Telegram, so the rest of this user turn never repeats it. */
  public recordTurnDelivery(text: string): void {
    this.turnDeliveries.push(text);
  }

  /**
   * Mirrors one assistant message into Telegram, streaming edits in place. `final` marks the
   * message_end pass: it is recorded as a turn delivery and ends the message's edit window.
   * Until anything of the message is on Telegram, text the turn already delivered is held
   * back, so a final reply never repeats a telegram_message the operator already has.
   */
  private syncAssistantOutput(targetText: string, final: boolean): Promise<void> {
    return this.queueOutbound(async () => {
      try {
        if (this.isDisposed || this.isDaemonManaged() || !this.poller || !targetText.trim()) return;

        const primaryChat = this.getPrimaryChatId();
        if (!primaryChat) return;

        if (this.sentTelegramMessageIds.length === 0 && this.turnDeliveries.length > 0) {
          if (final && this.turnDeliveries.some(earlier => isRepeatDelivery(targetText, earlier))) {
            this.pi.logger?.info?.(`[Telegram] Final reply not forwarded: it repeats a delivery from this turn (${targetText.length} chars).`);
            return;
          }
          if (!final) {
            // A streamed prefix of already-delivered text waits for message_end to decide.
            const partial = normalizeForDedupe(targetText);
            if (this.turnDeliveries.some(earlier => normalizeForDedupe(earlier).includes(partial))) return;
          }
        }

        // Converted once here; the poller receives finished HTML so nothing is re-parsed as Markdown.
        // Bare #N links to the session's own repository; outside a GitHub checkout the default applies.
        const fullHtml = markdownToTelegramHtml(targetText, this.cwd ? resolveGithubRepo(this.cwd) : undefined);
        const chunks = chunkMessage(fullHtml, 3800);
        for (let i = 0; i < chunks.length; i++) {
          const chunk = chunks[i];
          if (i < this.sentTelegramMessageIds.length) {
            if (chunk !== this.streamedChunks[i]) {
              await this.poller.editTelegramMessage(primaryChat, this.sentTelegramMessageIds[i], chunk, "HTML");
              this.streamedChunks[i] = chunk;
            }
          } else {
            const res = await this.poller.sendTelegramMessage(primaryChat, chunk, "HTML");
            if (res?.ok && typeof res.result?.message_id === "number") {
              this.sentTelegramMessageIds.push(res.result.message_id);
              this.streamedChunks.push(chunk);
            }
          }
        }
        if (final && this.sentTelegramMessageIds.length > 0) this.recordTurnDelivery(targetText);
      } finally {
        if (final) {
          this.sentTelegramMessageIds = [];
          this.streamedChunks = [];
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
      root.questions?.stop();
      root.dashboard?.stop();
      root.messageContext?.close();
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
      this.pi.logger?.warn(`[Telegram Runtime] Session ineligible: ${JSON.stringify({
        hasUI: ctx.hasUI,
        isSubagent: ctx.isSubagent,
        taskDepth: ctx.taskDepth,
        parentTaskPrefix: ctx.parentTaskPrefix,
        sessionId: ctx.sessionManager.getSessionId(),
        cwd: ctx.cwd,
        runtimeUrl: import.meta.url,
      })}`);
      return false;
    }

    const newSessionId = ctx.sessionManager.getSessionId();
    const globalState = globalThis as unknown as GlobalTelegramState;
    const existingRoot = globalState[ACTIVE_ROOT_SYMBOL];

    // Idempotency guard: if this runtime already holds the active session and poller, do not churn
    if (this.sessionId === newSessionId && this.poller && this.activeSlot && !opts?.isReload) {
      return true;
    }

    if (existingRoot && existingRoot.instanceId !== this.instanceId) {
      if (opts?.isReload) {
        try {
          await existingRoot.poller?.stop();
        } catch {}
        try {
          existingRoot.coordinator?.close();
        } catch {}
        delete globalState[ACTIVE_ROOT_SYMBOL];
        delete globalState[ACTIVE_LEASE_SYMBOL];
      } else if (existingRoot.sessionId === newSessionId) {
        this.pi.logger?.warn(`[Telegram Runtime] Session already owned by runtime ${existingRoot.instanceId}; requested ${this.instanceId}, session ${newSessionId}.`);
        if (existingRoot.activeSlot && existingRoot.sessionId !== newSessionId) {
          this.repointLeaseOrRelinquish(existingRoot, newSessionId, ctx.cwd);
        }
        return false;
      } else {
        this.pi.logger?.warn(`[Telegram Runtime] Root ownership conflict: active session ${existingRoot.sessionId}, requested ${newSessionId}.`);
        return false;
      }
    }

    const coordinator = this.options.coordinatorFactory ? this.options.coordinatorFactory() : new BotPoolCoordinator();
    const claim = await coordinator.acquireLease(newSessionId, ctx.cwd, process.pid);

    let activeSlot: DiscoveredSlot;
    let token: string;
    let isDaemonClient = false;
    let daemonRoute: DaemonRouteInfo | null = null;

    if (!claim.ok || !claim.slot) {
      daemonRoute = findDaemonRoute(coordinator, newSessionId, ctx.cwd);
      if (daemonRoute) {
        isDaemonClient = true;
        activeSlot = daemonRoute.slot;
        token = daemonRoute.token;
      } else {
        this.pi.logger?.warn(
          `Telegram bot lease not acquired for session ${newSessionId}: ${claim.reason || "Pool busy"}`,
        );
        coordinator.close();
        return false;
      }
    } else {
      activeSlot = claim.slot;
      const rawToken = coordinator.readRawTokenForSlot(activeSlot.stateDir);
      if (!rawToken) {
        coordinator.releaseLease(activeSlot.slotId, newSessionId, process.pid);
        coordinator.close();
        this.pi.logger?.warn(`Telegram bot token missing for slot ${activeSlot.slotId}. Released lease.`);
        return false;
      }
      token = rawToken;
    }

    this.coordinator = coordinator;
    this.activeSlot = activeSlot;
    this.sessionId = newSessionId;
    this.cwd = ctx.cwd;
    this.isDaemonClient = isDaemonClient;
    this.accessConfig = coordinator.readAccessConfig(activeSlot.stateDir);
    if (isDaemonClient && daemonRoute) {
      this.accessConfig = {
        ...this.accessConfig,
        allowFrom: [daemonRoute.chatId, ...this.accessConfig.allowFrom],
      };
    }

    const poolPath = process.env.VEYYON_POOL_DB || path.join(os.homedir(), ".veyyon", "telegram", "bot_pool.db");
    // Lane provenance is extra columns on the coordinator's own correlation rows. If the
    // pool predates them the channel still attaches; replies then reach Main without a
    // lane attribution rather than not at all.
    let messageContext: MessageContextStore | null = null;
    try {
      messageContext = new MessageContextStore(poolPath);
    } catch (err: unknown) {
      this.pi.logger?.warn(
        `Telegram lane provenance unavailable on slot ${activeSlot.slotId}: ${err instanceof Error ? err.message : String(err)}. Replies route to Main without lane context.`,
      );
    }
    this.messageContext = messageContext;

    const currentSessionId = (): string => {
      return this.sessionId ?? newSessionId;
    };


    const correlationBridge: MessageCorrelationBridge = {
      getSessionId: currentSessionId,
      getSlotId: () => activeSlot.slotId,
      record: correlation => {
        coordinator.recordOutboundMessage(correlation);
        messageContext?.record(correlation.botId, correlation.chatId, correlation.messageId, correlation);
      },
      resolveReply: (botId, chatId, replyToMessageId) => {
        const resolution = coordinator.resolveReplyRouting(botId, chatId, replyToMessageId, currentSessionId());
        if (resolution.correlation && messageContext) {
          Object.assign(resolution.correlation, messageContext.lookup(botId, chatId, replyToMessageId));
        }
        return resolution;
      },
      resolveCallback: (callbackToken, userId, chatId) =>
        coordinator.validateDecisionCallback(callbackToken, userId, chatId, currentSessionId()),
      consumeCallback: callbackToken =>
        coordinator.consumeDecisionCallback(callbackToken),
    };

    const runner = this.options.commandRunnerFactory ? this.options.commandRunnerFactory() : new BunCommandRunner();

    const pollerCallbacks: PollerCallbacks = {
      isIdle: () => ctx.isIdle(),
      getSessionFile: () => ctx.sessionManager.getSessionFile(),
      onUserMessage: (text: string) => {
        if (ctx.isIdle()) {
          this.pi.sendUserMessage(text);
        } else {
          this.pi.sendUserMessage(text, { deliverAs: "steer" });
        }
      },
      onFollowUp: (text: string) => {
        this.pi.sendUserMessage(text, { deliverAs: "followUp" });
      },
      onSteer: (text: string) => {
        this.pi.sendUserMessage(text, { deliverAs: "steer" });
      },
      // Abort only. The poller owns the operator-facing cancellation reply, so
      // sending one here would deliver it twice.
      onAbort: () => {
        void this.pi.abortActiveTurn();
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
      // The service is constructed after the poller it writes through, so the
      // receiver is resolved per answer rather than captured at wiring time.
      onQuestionAnswer: async (decisionId: string, eventId: string, answer: { choice?: string; text?: string }) => {
        if (!this.questions) throw new Error("Question receiver unavailable; answer was not delivered");
        await this.questions.answer(decisionId, eventId, answer);
      },
      onDecisionCallback: async (decisionId: string, choiceId: string, context?: string) => {
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
      onConflict: (diagnosis: string, attempt: number, maxAttempts: number) => {
        this.pi.logger?.warn(`Telegram poller HTTP 409 conflict on slot ${activeSlot.slotId}: ${diagnosis}`);
        if (attempt >= maxAttempts) {
          ctx.ui?.notify?.(
            `Telegram polling stopped on slot ${activeSlot.slotId}: HTTP 409 Conflict with another running bot instance.`,
            "error",
          );
        }
      },
    };

    let messageThreadId: number | undefined;
    if (isDaemonClient && daemonRoute?.topicId) {
      messageThreadId = Number(daemonRoute.topicId);
    } else {
      try {
        messageThreadId = readMessageThreadId(activeSlot.stateDir);
      } catch (err: unknown) {
        if (!isDaemonClient) {
          coordinator.releaseLease(activeSlot.slotId, newSessionId, process.pid);
          coordinator.close();
          messageContext?.close();
          this.messageContext = null;
          this.coordinator = null;
          this.pi.logger?.warn(
            `Telegram slot ${activeSlot.slotId} not attached: ${err instanceof Error ? err.message : String(err)}. Fix access.json before the channel can bind to its topic.`,
          );
          return false;
        }
      }
    }
    const pollerOptions: PollerOptions | undefined = {
      ...(messageThreadId !== undefined ? { messageThreadId } : {}),
      slotId: activeSlot.slotId,
    };

    const poller = this.options.pollerFactory
      ? this.options.pollerFactory(token, activeSlot.stateDir, this.accessConfig, pollerCallbacks, correlationBridge, pollerOptions)
      : new TelegramPoller(token, activeSlot.stateDir, this.accessConfig, pollerCallbacks, correlationBridge, pollerOptions);

    if (isDaemonClient && daemonRoute) {
      poller.setPrimaryChatId(daemonRoute.chatId);
    }

    this.poller = poller;

    const questions = new OperatorQuestionService(
      poller,
      () => {
        const chat = poller.getPrimaryChatId();
        if (!chat) throw new Error("No authorized operator chat for this session");
        return { session_id: currentSessionId(), chat_id: chat, user_id: questionOperator(this.accessConfig, chat) };
      },
      path.join(os.homedir(), ".veyyon", "workflows", "decisions.json"),
      poolPath,
      message => this.pi.logger?.warn(message),
    );
    this.questions = questions;

    const dashboard = new LiveDashboard(poller, runner, currentSessionId, message => this.pi.logger?.warn(message));
    this.dashboard = dashboard;

    questions.start();
    dashboard.start();
    globalState[ACTIVE_ROOT_SYMBOL] = {
      instanceId: this.instanceId,
      sessionId: newSessionId,
      slotId: activeSlot.slotId,
      pi: this.pi,
      poller,
      coordinator,
      activeSlot,
      questions,
      messageContext: messageContext ?? undefined,
      dashboard,
    };


    globalState[ACTIVE_LEASE_SYMBOL] = {
      slotId: activeSlot.slotId,
      sessionId: newSessionId,
    };

    if (!isDaemonClient) {
      void poller.start();
    }

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

  private isDaemonManaged(): boolean {
    if (this.isDaemonClient) return true;
    if (this.activeSlot?.daemon === true) {
      this.isDaemonClient = true;
      return true;
    }
    if (this.coordinator && this.sessionId) {
      const route = findDaemonRoute(this.coordinator, this.sessionId, this.cwd ?? undefined);
      if (route) {
        this.isDaemonClient = true;
        return true;
      }
    }
    return false;
  }

  public async onMessageStart(event: { message: { role: string } }): Promise<void> {
    if (event.message.role === "user") this.turnDeliveries = [];
    if (this.isDaemonManaged()) return;
    if (event.message.role === "assistant") {
      this.accumulatedAssistantText = "";
      this.sentTelegramMessageIds = [];
      this.streamedChunks = [];
    }
  }

  public async onMessageUpdate(event: MessageUpdateEvent): Promise<void> {
    if (event.message.role !== "assistant") return;
    if (this.isDaemonManaged()) return;
    const streamEvent = event.assistantMessageEvent;

    if (streamEvent.type === "text_delta" && streamEvent.delta) {
      this.accumulatedAssistantText += streamEvent.delta;

      if (!this.streamDebounceTimer) {
        this.streamDebounceTimer = setTimeout(async () => {
          this.streamDebounceTimer = null;
          await this.syncAssistantOutput(this.accumulatedAssistantText, false);
        }, 1500);

        if (typeof this.streamDebounceTimer.unref === "function") {
          this.streamDebounceTimer.unref();
        }
      }
    }
  }

  public async onMessageEnd(event: MessageEndEvent): Promise<void> {
    if (event.message.role !== "assistant") return;
    if (this.isDaemonManaged()) return;

    if (this.streamDebounceTimer) {
      clearTimeout(this.streamDebounceTimer);
      this.streamDebounceTimer = null;
    }

    const assistantMsg = event.message as AssistantMessage;
    const fullText = assistantMsg.content
      .filter((c): c is { type: "text"; text: string } => c.type === "text")
      .map(c => c.text)
      .join("\n");

    // The final pass resets the message's edit window itself, inside the outbound queue.
    await this.syncAssistantOutput(fullText, true);
    this.accumulatedAssistantText = "";
  }

  public async onSessionShutdown(_event: SessionShutdownEvent): Promise<void> {
    await this.dispose();
  }

  public async dispose(): Promise<void> {
    if (this.isDisposed) return;
    this.isDisposed = true;
    this.isDaemonClient = false;
    this.cwd = null;

    try {
      if (this.streamDebounceTimer) {
        clearTimeout(this.streamDebounceTimer);
        this.streamDebounceTimer = null;
      }

      // Stop the timer-driven services before the poller they write through, so a
      // coalesced refresh cannot fire against a stopped channel.
      this.questions?.stop();
      this.questions = null;
      this.dashboard?.stop();
      this.dashboard = null;

      if (this.messageContext) {
        try {
          this.messageContext.close();
        } catch (err) {
          this.pi.logger?.warn(`Error closing lane provenance store: ${err}`);
        }
        this.messageContext = null;
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
    } finally {
      const globalState = globalThis as unknown as GlobalTelegramState;
      if (globalState[ACTIVE_ROOT_SYMBOL]?.instanceId === this.instanceId) {
        delete globalState[ACTIVE_ROOT_SYMBOL];
        delete globalState[ACTIVE_LEASE_SYMBOL];
      }
    }
  }
}

export function createRuntime(pi: ExtensionAPI, options?: TelegramRuntimeOptions): TelegramRuntime {
  return new TelegramRuntime(pi, options);
}
