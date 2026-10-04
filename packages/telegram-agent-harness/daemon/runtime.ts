/**
 * runtime.ts — Standalone Telegram daemon: owns opted-in bot tokens machine-wide.
 *
 * Why this exists: the in-session extension only polls while a Veyyon session is
 * running in the right project, so the bot goes silent the moment the operator
 * closes the terminal. The daemon polls independently and drives sessions through
 * the GUI host instead of living inside one.
 *
 * The no-double-poller invariant is unchanged and enforced by the same mechanism as
 * before: the daemon takes a real `bot_leases` lease per slot, so every in-session
 * poller sees the slot as busy and skips it. Nothing here bypasses the pool.
 */

import * as fs from "node:fs";
import * as path from "node:path";
import * as os from "node:os";
import { randomUUID } from "node:crypto";
import { isClosed, OperatorQuestionService, QuestionStore, questionOperator, readQuestions, type QuestionRoute } from "../src/operator-questions";
import {
  BotPoolCoordinator,
  getDefaultManifestPath,
  getProcessIdentity,
} from "../extension/coordinator";
import { TelegramPoller } from "../extension/poller";
import { governedTelegramFetch } from "../extension/telegram-governor";
import { resolveGithubRepo } from "../extension/github-repo";
import { chunkMessage, escapeHtml } from "../extension/sanitizer";
import type { AccessConfig, MessageCorrelationBridge } from "../extension/types";
import { BunCommandRunner } from "../extension/harness/command-runner";
import { handleInstalledCommand } from "../src/installed-commands";
import {
  getDaemonLogPath,
  getDaemonPidPath,
  getDaemonStatusPath,
  resolveDaemonSlots,
  type DaemonSlot,
} from "./config";
import { getDaemonCommands, SlotRouter, type RouteTarget } from "./router";
import {
  TerminalSessionControl,
  discoverOwners,
  findSessionFile,
  resolveSessionsRoots,
  type SessionEvent,
} from "./session-control";
import { DaemonStore } from "./store";
import { connectMiniApp, miniAppUrl, buildMiniAppUrl } from "./miniapp";
import { DefaultTelegramForumClient, ForumManager, workspaceFolder, type ForumApiClient, type AutoAttachResult } from "./forum";
import { QuestionsTopic } from "./questions-topic";
import { defaultFleetSources, FleetState, type FleetQuestion, type FleetSources } from "./fleet-state";
import { LanePanels, PANEL_EXPIRED_ANSWER } from "./lane-panel";

export interface DaemonSlotReport {
  slotId: string;
  botId: string;
  workspace: string | null;
  polling: boolean;
  /** Why the slot is not polling; absent when it is. */
  skipped?: string;
}

export interface DaemonStatusReport {
  pid: number;
  startedAt: number;
  transport: "terminal-ipc";
  slots: DaemonSlotReport[];
}

export interface DaemonRuntimeOptions {
  poolDbPath?: string;
  manifestPath?: string;
  daemonDbPath?: string;
  channelsDir?: string;
  log?: (message: string) => void;
  /** Injected in tests so no real Bot API call or GUI host connection is made. */
  pollerFactory?: (
    token: string,
    stateDir: string,
    access: AccessConfig,
    callbacks: ConstructorParameters<typeof TelegramPoller>[3],
    correlation: MessageCorrelationBridge,
    options?: ConstructorParameters<typeof TelegramPoller>[5],
  ) => TelegramPoller;
  controlFactory?: (
    onEvent: (event: SessionEvent) => void,
    onLog: (message: string) => void,
  ) => TerminalSessionControl;
  /** Injected in tests to fake Bot API calls for forum supergroup topics. */
  forumClientFactory?: (token: string, forumChatId: string) => ForumApiClient;
  /** Injected in tests so the lane panel reads fake sessions instead of the real terminal owners. */
  fleetSources?: FleetSources;
}

interface ActiveSlot {
  slot: DaemonSlot;
  poller: TelegramPoller;
  router: SlotRouter;
  forumManager?: ForumManager;
  questionsTopic?: QuestionsTopic;
  leaseSessionId: string;
  stopMiniApp: () => void;
  autoAttachTimer?: NodeJS.Timeout;
  lanePanels?: LanePanels;
}

export class TelegramDaemon {
  private readonly options: DaemonRuntimeOptions;
  private readonly coordinator: BotPoolCoordinator;
  private readonly store: DaemonStore;
  private readonly control: TerminalSessionControl;
  private readonly active: ActiveSlot[] = [];

  public getActiveSlot(slotId: string): ActiveSlot | undefined {
    return this.active.find(entry => entry.slot.slotId === slotId);
  }
  private readonly startedAt = Date.now();
  /** Last logged skip reason per slot, so a retry loop does not repeat itself. */
  private readonly skipReasons = new Map<string, string>();
  private stopping = false;

  constructor(options: DaemonRuntimeOptions = {}) {
    this.options = options;
    this.coordinator = new BotPoolCoordinator(options.poolDbPath, options.manifestPath, options.channelsDir);
    this.store = new DaemonStore(options.daemonDbPath);
    this.control = options.controlFactory
      ? options.controlFactory(event => this.fanOut(event), message => this.log(message))
      : new TerminalSessionControl({
          onEvent: event => this.fanOut(event),
          onLog: message => this.log(message),
        });
  }
  /**
   * One line per event, appended to `getDaemonLogPath()`.
   *
   * Appended, and NOT printed: the daemon is started detached, so its stdout is a
   * redirect — and both `cmd`'s `>>` and PowerShell's `*>>` buffer a long-lived
   * process's output, which left the log the launcher points the operator at empty
   * for the daemon's whole lifetime. The redirect still exists, for the crash that
   * happens before this class is reachable; this write is what makes a running
   * daemon watchable, and it lands in the same file.
   */
  public log(message: string): void {
    const line = `[${new Date().toISOString()}] ${message}`;
    if (this.options.log) {
      this.options.log(line);
      return;
    }
    try {
      fs.mkdirSync(path.dirname(getDaemonLogPath()), { recursive: true });
      fs.appendFileSync(getDaemonLogPath(), `${line}\n`, "utf8");
    } catch {
      console.log(line);
    }
  }

  /**
   * Claims every opted-in slot and starts polling it. A slot the daemon cannot claim
   * is reported and skipped, never force-claimed: a live in-session poller holding
   * that token is a legitimate owner, and stealing it is how 409 conflicts start.
   */
  public async start(): Promise<DaemonStatusReport> {
    const reports: DaemonSlotReport[] = [];
    for (const slot of this.optedInSlots()) reports.push(await this.claim(slot));
    const published = this.publish(reports);
    await this.reconcileAllAutoAttach().catch(err => {
      this.log(`Auto-attach on daemon start failed: ${err instanceof Error ? err.message : String(err)}`);
    });
    return published;
  }

  /**
   * Retries the opted-in slots that are not polling yet.
   *
   * A slot is skipped because someone else holds its token *right now* — an
   * interactive session that claimed it before the daemon started, most often. That
   * state ends on its own: the session exits, or its operator runs `/telegram
   * release`. Without a retry the daemon would have to be restarted by hand at
   * exactly that moment, which for a detached logon task means it never happens, so
   * `run` calls this on an interval and the handover completes unattended.
   */
  public async claimPending(): Promise<DaemonStatusReport> {
    const reports: DaemonSlotReport[] = [];
    for (const slot of this.optedInSlots()) {
      const active = this.active.find(entry => entry.slot.slotId === slot.slotId);
      const isRunning = Boolean(active?.poller.running);
      reports.push(
        active
          ? { slotId: slot.slotId, botId: slot.botId, workspace: slot.workspace, polling: isRunning }
          : await this.claim(slot),
      );
    }
    const published = this.publish(reports);
    await this.reconcileAllAutoAttach().catch(err => {
      this.log(`Auto-attach after claimPending failed: ${err instanceof Error ? err.message : String(err)}`);
    });
    return published;
  }

  private optedInSlots(): DaemonSlot[] {
    return resolveDaemonSlots(this.coordinator, this.options.manifestPath ?? getDefaultManifestPath());
  }

  /** Claims one slot and starts its poller, or reports why it stays unclaimed. */
  private async claim(slot: DaemonSlot): Promise<DaemonSlotReport> {
    const leaseSessionId = `daemon:${slot.slotId}`;
    const claim = this.coordinator.acquireLeaseForSlot(
      slot.slotId,
      leaseSessionId,
      slot.workspace ?? process.cwd(),
    );
    if (!claim.ok) {
      return this.unclaimed(slot, claim.reason ?? "unavailable");
    }

    const token = this.coordinator.readRawTokenForSlot(slot.stateDir);
    if (!token) {
      this.coordinator.releaseLease(slot.slotId, leaseSessionId);
      return this.unclaimed(slot, "bot token unreadable");
    }

    let activeSlot: ActiveSlot;
    try { activeSlot = this.startSlot(slot, token, leaseSessionId); }
    catch {
      this.coordinator.releaseLease(slot.slotId, leaseSessionId);
      return this.unclaimed(slot, "slot startup failed");
    }
    this.active.push(activeSlot);
    void activeSlot.poller
      .start()
      .then(() => {
        if (!activeSlot.poller.running) {
          this.onPollerEnd(slot.slotId, leaseSessionId, "loop terminated");
        }
      })
      .catch(error => {
        this.onPollerEnd(slot.slotId, leaseSessionId, error instanceof Error ? error.message : String(error));
      });
    this.skipReasons.delete(slot.slotId);
    this.log(`Slot ${slot.slotId} polling (bot ${slot.botId}, workspace ${slot.workspace ?? "unresolved"}, mode ${slot.mode ?? "dm"})`);
    return { slotId: slot.slotId, botId: slot.botId, workspace: slot.workspace, polling: true };
  }

  private onPollerEnd(slotId: string, leaseSessionId: string, reason: string): void {
    if (this.stopping) return;
    const index = this.active.findIndex(entry => entry.slot.slotId === slotId);
    if (index < 0) return;
    const [ended] = this.active.splice(index, 1);
    ended.questionsTopic?.stop();
    void ended.lanePanels?.stop();
    if (ended.autoAttachTimer) {
      clearInterval(ended.autoAttachTimer);
    }
    ended.stopMiniApp();
    this.coordinator.releaseLease(slotId, leaseSessionId);
    this.skipReasons.set(slotId, `poller ended: ${reason}`);
    this.log(`Slot ${slotId} poller ended: ${reason}`);
    this.publish(this.currentReports());
  }

  /** True when any opted-in slot is not currently active and polling. */
  public hasPendingSlots(): boolean {
    return this.optedInSlots().some(slot => {
      const active = this.active.find(entry => entry.slot.slotId === slot.slotId);
      const isRunning = Boolean(active?.poller.running);
      return !active || !isRunning;
    });
  }
  private currentReports(): DaemonSlotReport[] {
    return this.optedInSlots().map(slot => {
      const active = this.active.find(entry => entry.slot.slotId === slot.slotId);
      if (active) {
        const isRunning = active.poller.running;
        return { slotId: slot.slotId, botId: slot.botId, workspace: slot.workspace, polling: isRunning };
      }
      return {
        slotId: slot.slotId,
        botId: slot.botId,
        workspace: slot.workspace,
        polling: false,
        skipped: this.skipReasons.get(slot.slotId) ?? "not claimed",
      };
    });
  }

  /**
   * Reports a slot the daemon left alone. Logged only when the reason changes: with
   * a retry every few seconds, logging each attempt would bury the line that matters
   * under thousands of identical ones.
   */
  private unclaimed(slot: DaemonSlot, reason: string): DaemonSlotReport {
    if (this.skipReasons.get(slot.slotId) !== reason) {
      this.skipReasons.set(slot.slotId, reason);
      this.log(`Slot ${slot.slotId} not claimed: ${reason}`);
    }
    return { slotId: slot.slotId, botId: slot.botId, workspace: slot.workspace, polling: false, skipped: reason };
  }

  private publish(slots: DaemonSlotReport[]): DaemonStatusReport {
    const report: DaemonStatusReport = {
      pid: process.pid,
      startedAt: this.startedAt,
      transport: "terminal-ipc",
      slots,
    };
    this.writeStatus(report);
    return report;
  }

  private startSlot(slot: DaemonSlot, token: string, leaseSessionId: string): ActiveSlot {
    // Forum mode differs from direct-chat mode in exactly one thing: the routing key
    // is the topic a message came from rather than the chat. Everything below — the
    // ledger, authorization, commands, delivery, relay — is shared.
    const forumChatId = slot.mode === "forum" && slot.forumChatId ? String(slot.forumChatId) : null;
    // A slot the manifest put in forum mode is authorized for its supergroup; writing
    // that into access.json before reading it keeps the file the operator inspects in
    // agreement with the authorization the poller actually applies.
    if (forumChatId && this.coordinator.allowGroupChat(slot.stateDir, forumChatId)) {
      this.log(`Slot ${slot.slotId}: authorized forum chat ${forumChatId} in access.json`);
    }
    const access = this.coordinator.readAccessConfig(slot.stateDir);
    // Assigned after construction: the poller and the router each need the other,
    // and the poller is what knows which chat a callback is currently serving.
    let poller: TelegramPoller;
    let questionsTopic: QuestionsTopic | undefined;
    // Assigned after the poller exists; the poller's callbacks reach it through this variable.
    let lanePanels: LanePanels | undefined;
    const decisionsPath = path.join(os.homedir(), ".veyyon", "workflows", "decisions.json");
    const poolPath = this.options.poolDbPath ?? process.env.VEYYON_POOL_DB ?? path.join(os.homedir(), ".veyyon", "telegram", "bot_pool.db");
    // Questions are answered from the Questions topic too, where no session is bound. There the
    // expected owner is the one the outbound copy or the button was issued for.
    const inQuestionsTopic = (): boolean => {
      const thread = questionsTopic?.threadId();
      return thread !== null && thread !== undefined && poller.getActiveThreadId() === thread;
    };

    const forumManager = forumChatId
      ? new ForumManager({
          slot,
          token,
          forumChatId,
          store: this.store,
          control: this.control,
          client: this.options.forumClientFactory?.(token, forumChatId),
          log: message => this.log(message),
        })
      : undefined;

    const currentTarget = (): RouteTarget => ({
      chatId: forumChatId ?? poller.getPrimaryChatId() ?? "",
      // The General topic reports no thread id and is not a topic a session binds to,
      // so it routes as the chat itself — same key a direct chat uses.
      topicId: forumChatId ? String(poller.getActiveThreadId() ?? "") : "",
    });
    const sendTo = async (target: RouteTarget, text: string, parseMode?: "HTML", sessionId?: string, defaultRepo?: string): Promise<void> => {
      const threadId = target.topicId ? Number(target.topicId) : undefined;
      for (const chunk of chunkMessage(text)) {
        const result = await poller.sendTelegramMessage(target.chatId, chunk, parseMode, undefined, {
          sessionId: sessionId ?? router.boundSession(target) ?? leaseSessionId,
        }, defaultRepo, threadId);
        if (!result?.ok) throw new Error("Telegram rejected the outbound message");
      }
    };
    const router = new SlotRouter({
      slot,
      store: this.store,
      control: this.control,
      send: (target, html) => sendTo(target, html, "HTML"),
      // Bare #N in relayed prose links to the bound session's own repository.
      relay: (target, markdown, sessionId) => {
        const workspace = this.store.getRoute(slot.slotId, target.chatId, target.topicId)?.workspace
          ?? this.store.routesForSession(sessionId)[0]?.workspace
          ?? slot.workspace;
        return sendTo(target, markdown, undefined, sessionId, workspace ? resolveGithubRepo(workspace) : undefined);
      },
      log: message => this.log(message),
      topics: forumManager,
    });

    const sessionIdForChat = (): string => router.boundSession(currentTarget()) ?? leaseSessionId;
    const correlation: MessageCorrelationBridge = {
      getSessionId: sessionIdForChat,
      getSlotId: () => slot.slotId,
      record: correlationRow => {
        this.coordinator.recordOutboundMessage(correlationRow);
      },
      resolveReply: (botId, chatId, replyToMessageId) => {
        const resolved = this.coordinator.resolveReplyRouting(botId, chatId, replyToMessageId, sessionIdForChat());
        const owner = resolved.correlation;
        if (resolved.decision === "reject_foreign_session" && owner?.decisionId?.startsWith("tq:") && inQuestionsTopic()) {
          return this.coordinator.resolveReplyRouting(botId, chatId, replyToMessageId, owner.sessionId);
        }
        return resolved;
      },
      resolveCallback: (callbackToken, userId, chatId) => {
        const resolved = this.coordinator.validateDecisionCallback(callbackToken, userId, chatId, sessionIdForChat());
        const issued = resolved.record;
        if (resolved.decision === "reject_foreign_session" && issued?.decisionId.startsWith("tq:") && inQuestionsTopic()) {
          return this.coordinator.validateDecisionCallback(callbackToken, userId, chatId, issued.sessionId);
        }
        return resolved;
      },
      consumeCallback: callbackToken => this.coordinator.consumeDecisionCallback(callbackToken),
    };

    const runner = new BunCommandRunner();
    const callbacks = {
      isIdle: () => !router.isBusy(currentTarget()),
      // Inbound photos/documents are saved beside the routed session's transcript,
      // so the session that receives the message can read the attachment.
      getSessionFile: () => {
        const sessionId = router.boundSession(currentTarget());
        if (!sessionId) return undefined;
        return findSessionFile(sessionId, resolveSessionsRoots(this.control.configRoot)) ?? undefined;
      },
      onUserMessage: (text: string) => {
        // A Steer or Follow-up button on the topic's panel decides how its next message is delivered.
        const target = currentTarget();
        void this.acknowledge(router, target, text, lanePanels?.takeArmed(target) ?? "auto");
      },
      onSteer: (text: string) => {
        void this.acknowledge(router, currentTarget(), text, "steer");
      },
      onFollowUp: (text: string) => {
        void this.acknowledge(router, currentTarget(), text, "followUp");
      },
      onAbort: () => {
        const target = currentTarget();
        void router.abort(target).catch(error => {
          this.log(`Slot ${slot.slotId}: abort failed: ${error instanceof Error ? error.message : String(error)}`);
        });
      },
      onRelease: async () => {
        await this.stopSlot(slot.slotId);
      },
      getStatusText: () => router.statusText(currentTarget()),
      onHarnessCommand: async (text: string, chatId: string, userId?: string) => {
        // The poller hands over the chat it read the update from; the topic within it
        // is the one it is dispatching right now.
        const target: RouteTarget = { chatId, topicId: currentTarget().topicId };
        const send = async (html: string): Promise<void> => sendTo(target, html, "HTML");
        if (/^\/app(?:@\w+)?\s*$/i.test(text)) {
          const rawUrl = miniAppUrl(slot.stateDir);
          const threadId = target.topicId ? Number(target.topicId) : undefined;
          if (rawUrl) {
            const boundSession = router.boundSession(target);
            const url = buildMiniAppUrl(rawUrl, {
              topicId: target.topicId || undefined,
              sessionId: boundSession || undefined,
            });
            await poller.sendTelegramMessage(
              chatId,
              "Open your Superboard dashboard",
              { inline_keyboard: [[{ text: "Open Superboard", web_app: { url } }]] },
              undefined,
              undefined,
              undefined,
              threadId,
            );
          } else {
            await poller.sendTelegramMessage(
              chatId,
              "Mini App is not configured for this bot.",
              "HTML",
              undefined,
              undefined,
              undefined,
              threadId,
            );
          }
          return true;
        }
        if (await router.handleCommand(text, target)) {
          if (/^\/sessions(?:@\w+)?(?:\s+all)?\s*$/i.test(text) && userId) {
            const ids = this.store.getSessionListing(slot.slotId, target.chatId, target.topicId);
            const live = await this.control.listSessions();
            const now = Date.now() / 1000;
            const keyboard = ids.flatMap(id => {
              const session = live.find(candidate => candidate.id === id);
              if (!session) return [];
              const token = randomUUID();
              const recorded = this.coordinator.recordDecisionCallback({
                callbackToken: token, decisionId: `attach:${target.topicId}`, choiceId: id,
                sessionId: router.boundSession(target) ?? leaseSessionId,
                chatId: target.chatId, userId, questionHash: target.topicId,
                expiresAt: now + 300, createdAt: now, consumedAt: null,
              });
              const folder = (session.workspace || session.cwd || id).replace(/\\/g, "/").split("/").filter(Boolean).at(-1) ?? id;
              return recorded ? [[{ text: `Attach ${folder}`, callback_data: token }]] : [];
            });
            if (keyboard.length) await poller.sendTelegramMessage(chatId, "Choose the running session to attach:", {
              inline_keyboard: keyboard,
            }, undefined, undefined, undefined, target.topicId ? Number(target.topicId) : undefined);
          }
          return true;
        }
        return handleInstalledCommand(text, {
          session: () => {
            const route = target.topicId ? this.store.getRoute(slot.slotId, target.chatId, target.topicId) : null;
            return {
              id: router.boundSession(target) ?? leaseSessionId,
              cwd: route?.workspace || slot.workspace || "",
              idle: !router.isBusy(target),
              stateDir: slot.stateDir,
            };
          },
          send,
          photo: (file, caption) => {
            const ws = route?.workspace || slot.workspace;
            return poller.sendTelegramPhoto(chatId, file, caption, undefined, ws ? resolveGithubRepo(ws) : undefined);
          },
          mediaGroup: (files, caption) => {
            const ws = route?.workspace || slot.workspace;
            return poller.sendMediaGroup(chatId, files, caption, ws ? resolveGithubRepo(ws) : undefined);
          },
          latestPng: async () => null,
          inbound: async message => {
            await router.deliver(target, message, "auto");
          },
          reload: async () => {
            try {
              const reloadedAccess = this.coordinator.readAccessConfig(slot.stateDir);
              poller.updateAccess(reloadedAccess);
              await send(
                `<b>Telegram daemon reloaded.</b> Slot <code>${escapeHtml(slot.slotId)}</code> access rules and configuration refreshed.`,
              );
            } catch (err: unknown) {
              await send(`<b>Reload failed:</b> ${escapeHtml(err instanceof Error ? err.message : String(err))}`);
            }
          },
        }, runner);
      },
      onQuestionAnswer: async (decisionId: string, eventId: string, answer: { choice?: string; text?: string }) => {
        const target = currentTarget();
        const sessionId = inQuestionsTopic()
          ? (await readQuestions(decisionsPath)).find(question => question.decision_id === decisionId)?.transport.session_id
          : router.boundSession(target);
        if (!sessionId) throw new Error("Question receiver unavailable: this topic has no session owner.");
        const questions = this.questionService(poller, {
          session_id: sessionId, chat_id: target.chatId,
          user_id: questionOperator(this.coordinator.readAccessConfig(slot.stateDir), target.chatId),
        }, decisionsPath, poolPath);
        const updated = await questions.answer(decisionId, eventId, answer);
        if (isClosed(updated)) questionsTopic?.request(0);
        else await questionsTopic?.refresh(decisionId);
      },
      onDecisionCallback: async (decisionId: string, choiceId: string, context?: string) => {
        if (decisionId.startsWith("attach:")) {
          const target = currentTarget();
          if (decisionId !== `attach:${target.topicId}`) throw new Error("Attach selection belongs to another topic.");
          await router.handleCommand(`/attach ${choiceId}`, target);
          return;
        }
        await router.deliver(
          currentTarget(),
          `Decision recorded from Telegram: question=${decisionId} choice=${choiceId}\n${context ?? ""}`.trim(),
          "auto",
        );
      },
      onLedgerFailure: (message: string) => {
        this.log(`Slot ${slot.slotId} inbound ledger failure: ${message}. Inbound Telegram updates are not being recorded.`);
      },
      onConflict: (diagnosis: string, attempt: number, maxAttempts: number) => {
        this.log(`Slot ${slot.slotId} HTTP 409 conflict (attempt ${attempt}/${maxAttempts}): ${diagnosis}`);
      },
      // Buttons of a panel this daemon no longer serves (feature off, restarted) answer as expired.
      lanePanel: {
        peek: (data: string) => lanePanels?.peek(data) ?? PANEL_EXPIRED_ANSWER,
        run: async (data: string) => { await lanePanels?.run(data); },
      },
    };

    const pollerOptions = {
      commands: getDaemonCommands(),
      isDaemon: true,
      log: (message: string) => this.log(`Slot ${slot.slotId}: ${message}`),
      slotId: slot.slotId,
      ...(forumChatId ? { forumChatId } : {}),
    };

    poller = this.options.pollerFactory
      ? this.options.pollerFactory(token, slot.stateDir, access, callbacks, correlation, pollerOptions)
      : new TelegramPoller(token, slot.stateDir, access, callbacks, correlation, pollerOptions);

    const stopMiniApp = connectMiniApp({
      stateDir: slot.stateDir, token, allowedUsers: access.allowFrom,
      session: (userId, context) => {
        const routes = this.store.listRoutes(slot.slotId);
        if (context?.sessionId) {
          const match = routes.find(r => r.sessionId === context.sessionId);
          if (match) return match.sessionId;
        }
        if (context?.topicId) {
          const match = routes.find(r => r.topicId === context.topicId);
          if (match) return match.sessionId;
        }
        const direct = router.boundSession({ chatId: userId, topicId: "" });
        if (direct) return direct;
        if (forumChatId && access.allowFrom.includes(userId)) {
          const liveOwnerIds = new Set(discoverOwners().map(o => o.sessionId));
          const liveForumRoute = routes.find(r => r.chatId === forumChatId && r.topicId !== "" && liveOwnerIds.has(r.sessionId));
          if (liveForumRoute) return liveForumRoute.sessionId;
          const fallbackForumRoute = routes.find(r => r.chatId === forumChatId && r.topicId !== "");
          if (fallbackForumRoute) return fallbackForumRoute.sessionId;
        }
        return null;
      },
      sessions: async () => {
        const liveSessions = await this.control.listSessions().catch(() => []);
        const liveMap = new Map(liveSessions.map(s => [s.id, s]));
        const routes = this.store.listRoutes(slot.slotId);
        return routes.map(r => {
          const live = liveMap.get(r.sessionId);
          return {
            id: r.sessionId,
            topicId: r.topicId || null,
            chatId: r.chatId,
            workspace: r.workspace,
            cwd: r.workspace,
            title: r.topicId ? `Topic #${r.topicId}` : null,
            status: live ? (this.control.isBusy(r.sessionId) ? "Running" : "Idle") : "Inactive",
            live: Boolean(live),
            isSubagent: false,
            kind: "interactive" as const,
          };
        });
      },
      status: () => ({ polling: poller.running, slot: slot.slotId }),
      dashboard: (userId, sessionId) => {
        const session = sessionId ?? router.boundSession({ chatId: userId, topicId: "" });
        const raw = session ? poller.getMeta(`dashboard-snapshot:${session}`) : null;
        try { return raw ? JSON.parse(raw) : null; } catch { return null; }
      },
    });
    const url = miniAppUrl(slot.stateDir);
    if (url) for (const chatId of access.allowFrom) void governedTelegramFetch(`https://api.telegram.org/bot${token}/setChatMenuButton`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, menu_button: { type: "web_app", text: "Superboard", web_app: { url } } }),
    }, { kind: "other" }).then(async response => {
      const result = await response.json();
      if (!response.ok || !result.ok) this.log(`Slot ${slot.slotId}: Mini App menu registration rejected`);
    }).catch(() => this.log(`Slot ${slot.slotId}: Mini App menu registration unavailable`));
    let autoAttachTimer: NodeJS.Timeout | undefined;
    if (forumChatId) {
      questionsTopic = this.createQuestionsTopic(slot, token, forumChatId, poller, decisionsPath, poolPath);
      questionsTopic.start(decisionsPath);
    }
    if (forumChatId && slot.lanePanel) lanePanels = this.createLanePanels(slot, forumChatId, poller, router, decisionsPath, () => questionsTopic?.threadId());
    if (forumManager && slot.autoAttach !== false) {
      const intervalMs = slot.autoAttachIntervalMs ?? 10_000;
      autoAttachTimer = setInterval(() => {
        if (this.stopping) return;
        void forumManager.reconcileAutoAttach(router).catch(err => {
          this.log(`Slot ${slot.slotId}: auto-attach interval failed: ${err instanceof Error ? err.message : String(err)}`);
        });
      }, intervalMs);
    }
    return { slot, poller, router, forumManager, questionsTopic, leaseSessionId, stopMiniApp, autoAttachTimer, lanePanels };
  }

  private questionService(poller: TelegramPoller, route: QuestionRoute, decisionsPath: string, poolPath: string): OperatorQuestionService {
    return new OperatorQuestionService(poller, () => route, decisionsPath, poolPath, message => this.log(message));
  }

  /** The slot's live topic panels: one pinned, edited-in-place status message per session topic. */
  private createLanePanels(
    slot: DaemonSlot, forumChatId: string, poller: TelegramPoller, router: SlotRouter, decisionsPath: string,
    questionsThread: () => number | null | undefined,
  ): LanePanels {
    let openQuestions: FleetQuestion[] = [];
    // The panel shows no usage, so `veyyon usage` is never spawned for it: the failed read is cached for good.
    const fleet = new FleetState({
      ...(this.options.fleetSources ?? defaultFleetSources(this.control.configRoot)),
      runUsage: () => Promise.reject(new Error("the lane panel reads no usage")),
      questions: () => openQuestions,
    }, { usageCacheMs: Number.POSITIVE_INFINITY });
    const panels = new LanePanels({
      slotId: slot.slotId,
      chatId: forumChatId,
      store: this.store,
      snapshot: async () => {
        const questions = await readQuestions(decisionsPath).catch(() => []);
        openQuestions = questions.filter(question => !isClosed(question))
          .map(question => ({ id: question.decision_id, text: question.question, sessionId: question.transport.session_id }));
        return fleet.snapshot();
      },
      boundSession: target => router.boundSession(target),
      isBusy: target => router.isBusy(target),
      stop: target => router.abort(target),
      excludeTopic: topicId => String(questionsThread() ?? "") === topicId,
      miniAppLink: slot.lanePanelMiniAppLink,
      log: message => this.log(`Slot ${slot.slotId}: ${message}`),
      transport: {
        send: async (target, text, markup, sessionId) => {
          const sent = await poller.sendTelegramMessage(target.chatId, text, "HTML", markup, { sessionId }, undefined, Number(target.topicId));
          if (sent?.ok && sent.result?.message_id) return { messageId: sent.result.message_id };
          return /thread not found/i.test(sent?.description ?? "") ? "gone" : "error";
        },
        edit: async (target, messageId, text, markup) => {
          const edited = await poller.editTelegramMessage(target.chatId, messageId, text, "HTML", undefined, markup, "panel");
          if (edited?.ok || /not modified/i.test(edited?.description ?? "")) return "ok";
          return /message to edit not found/i.test(edited?.description ?? "") ? "gone" : "error";
        },
        pin: async (target, messageId) => { await poller.pinTelegramMessage(target.chatId, messageId, "panel"); },
      },
    });
    panels.start();
    return panels;
  }

  /** The slot's Questions topic: one list of every open operator question, kept in step with the decision store. */
  public createQuestionsTopic(
    slot: DaemonSlot, token: string, forumChatId: string, poller: TelegramPoller,
    decisionsPath: string, poolPath: string,
  ): QuestionsTopic {
    const client = this.options.forumClientFactory?.(token, forumChatId) ?? new DefaultTelegramForumClient(token);
    const ledger = new QuestionStore(decisionsPath, poolPath);
    let live = new Set<string>();
    return new QuestionsTopic({
      chatId: forumChatId,
      slotId: slot.slotId,
      store: this.store,
      ledger,
      prepare: async () => {
        live = new Set((await this.control.listSessions().catch(() => [])).map(session => session.id));
      },
      session: sessionId => {
        const workspace = this.store.routesForSession(sessionId)[0]?.workspace ?? "";
        return { name: workspace ? workspaceFolder(workspace) : sessionId.slice(0, 8), ended: !live.has(sessionId) };
      },
      transport: {
        createTopic: async name => (await client.createForumTopic(forumChatId, name)).message_thread_id,
        send: async (threadId, text, markup, owner) => {
          const sent = await poller.sendTelegramMessage(forumChatId, text, "HTML", markup,
            owner ? { sessionId: owner.sessionId, decisionId: owner.decisionId } : undefined, undefined, threadId);
          if (sent?.ok && sent.result?.message_id) return { messageId: sent.result.message_id };
          if (/thread not found/i.test(sent?.description ?? "")) return "gone";
          this.log(`Slot ${slot.slotId}: Questions topic send failed: ${sent?.description ?? "no response"}`);
          return "error";
        },
        edit: async (messageId, text, markup) => {
          const edited = await poller.editTelegramMessage(forumChatId, messageId, text, "HTML", undefined, markup);
          if (edited?.ok || /not modified/i.test(edited?.description ?? "")) return "ok";
          return /not found/i.test(edited?.description ?? "") ? "gone" : "error";
        },
        remove: messageId => poller.deleteTelegramMessage(forumChatId, messageId),
        pin: async messageId => { await poller.pinTelegramMessage(forumChatId, messageId); },
      },
      finalize: async question => {
        await this.questionService(poller, question.transport, decisionsPath, poolPath).finalize(question);
      },
      log: message => this.log(message),
    });
  }

  /**
   * Delivers text and posts the router's acknowledgement. Poller message callbacks
   * are fire-and-forget, so a failure here has to reach the operator's chat rather
   * than becoming an unhandled rejection in a background loop.
   */
  private async acknowledge(
    router: SlotRouter,
    target: RouteTarget,
    text: string,
    mode: "auto" | "steer" | "followUp",
  ): Promise<void> {
    try {
      const ack = await router.deliver(target, text, mode);
      if (ack) for (const chunk of chunkMessage(ack)) await this.sendTo(router, target, chunk);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      this.log(`Delivery to chat ${target.chatId} failed: ${detail}`);
      await this.sendTo(router, target, `⚠️ <b>Not delivered.</b> ${detail}`).catch(() => undefined);
    }
  }

  private async sendTo(router: SlotRouter, target: RouteTarget, html: string): Promise<void> {
    const active = this.active.find(entry => entry.router === router);
    await active?.poller.sendTelegramMessage(
      target.chatId,
      html,
      "HTML",
      undefined,
      undefined,
      undefined,
      target.topicId ? Number(target.topicId) : undefined,
    );
  }

  private fanOut(event: SessionEvent): void {
    for (const entry of this.active) {
      void entry.router.onSessionEvent(event).catch(error => {
        this.log(`Slot ${entry.slot.slotId}: session event handling failed: ${error instanceof Error ? error.message : String(error)}`);
      });
    }
  }

  public async stopSlot(slotId: string): Promise<boolean> {
    const index = this.active.findIndex(entry => entry.slot.slotId === slotId);
    if (index < 0) return false;
    const [entry] = this.active.splice(index, 1);
    entry.questionsTopic?.stop();
    await entry.lanePanels?.stop();
    if (entry.autoAttachTimer) {
      clearInterval(entry.autoAttachTimer);
      entry.autoAttachTimer = undefined;
    }
    entry.stopMiniApp();
    await entry.poller.stop();
    this.coordinator.releaseLease(entry.slot.slotId, entry.leaseSessionId);
    this.log(`Slot ${entry.slot.slotId} stopped and lease released`);
    return true;
  }

  /**
   * Reconciles auto-attach topics across all active forum slots.
   * Called on startup, on GUI host recovery, and directly by CLI dry-run.
   */
  public async reconcileAllAutoAttach(options?: { dryRun?: boolean }): Promise<Map<string, AutoAttachResult>> {
    const results = new Map<string, AutoAttachResult>();
    for (const entry of this.active) {
      if (entry.forumManager && entry.slot.autoAttach !== false) {
        try {
          const res = await entry.forumManager.reconcileAutoAttach(entry.router, options);
          results.set(entry.slot.slotId, res);
        } catch (err) {
          this.log(`Slot ${entry.slot.slotId}: reconcileAutoAttach failed: ${err instanceof Error ? err.message : String(err)}`);
        }
      }
    }
    return results;
  }

  public async stop(): Promise<void> {
    if (this.stopping) return;
    this.stopping = true;
    for (const entry of [...this.active]) await this.stopSlot(entry.slot.slotId);
    this.control.close();
    this.store.close();
    this.coordinator.close();
    try {
      fs.rmSync(getDaemonPidPath(), { force: true });
    } catch {}
    this.log("Daemon stopped");
  }

  public status(): DaemonStatusReport {
    return {
      pid: process.pid,
      startedAt: this.startedAt,
      transport: "terminal-ipc",
      slots: this.active.map(entry => ({
        slotId: entry.slot.slotId,
        botId: entry.slot.botId,
        workspace: entry.slot.workspace,
        polling: entry.poller.running,
      })),
    };
  }

  private writeStatus(report: DaemonStatusReport): void {
    try {
      fs.mkdirSync(path.dirname(getDaemonStatusPath()), { recursive: true });
      fs.writeFileSync(getDaemonStatusPath(), `${JSON.stringify(report, null, 2)}\n`, "utf8");
    } catch {}
  }
}

/**
 * Claims the machine-wide daemon pid file. Two daemons would poll the same tokens
 * and trade HTTP 409s forever, so a live holder wins and the new process refuses.
 */
export function claimDaemonPidFile(pidPath: string = getDaemonPidPath()): { ok: boolean; holder?: number } {
  fs.mkdirSync(path.dirname(pidPath), { recursive: true });
  const recorded = fs.existsSync(pidPath) ? fs.readFileSync(pidPath, "utf8").trim() : "";
  const existing = Number.parseInt(recorded, 10);
  if (Number.isFinite(existing) && existing > 0 && existing !== process.pid) {
    const identity = getProcessIdentity(existing);
    if (identity.alive || identity.uncertain) return { ok: false, holder: existing };
  }
  fs.writeFileSync(pidPath, String(process.pid), "utf8");
  return { ok: true };
}
