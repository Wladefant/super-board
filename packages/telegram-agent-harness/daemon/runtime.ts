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
import {
  BotPoolCoordinator,
  getDefaultManifestPath,
  getProcessIdentity,
} from "../extension/coordinator";
import { TelegramPoller } from "../extension/poller";
import { chunkMessage } from "../extension/sanitizer";
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
import { getDaemonCommands, SlotRouter } from "./router";
import {
  GuiHostSessionControl,
  resolveGuiHostEndpoint,
  type SessionEvent,
} from "./session-control";
import { DaemonStore } from "./store";
import { connectMiniApp, miniAppUrl } from "./miniapp";
import { ForumManager, type ForumApiClient } from "./forum";

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
  endpoint: string | null;
  slots: DaemonSlotReport[];
}

export interface DaemonRuntimeOptions {
  poolDbPath?: string;
  manifestPath?: string;
  daemonDbPath?: string;
  channelsDir?: string;
  /** Overrides endpoint discovery; null means "no host reachable". */
  endpoint?: string | null;
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
  ) => GuiHostSessionControl;
  /** Injected in tests to fake Bot API calls for forum supergroup topics. */
  forumClientFactory?: (token: string, forumChatId: string) => ForumApiClient;
}

interface ActiveSlot {
  slot: DaemonSlot;
  poller?: TelegramPoller;
  forumManager?: ForumManager;
  router?: SlotRouter;
  /** Lease identity; also what in-session pollers see as the slot holder. */
  leaseSessionId: string;
  stopMiniApp: () => void;
}

export class TelegramDaemon {
  private readonly options: DaemonRuntimeOptions;
  private readonly coordinator: BotPoolCoordinator;
  private readonly store: DaemonStore;
  private readonly control: GuiHostSessionControl;
  private readonly active: ActiveSlot[] = [];
  private readonly startedAt = Date.now();
  /** Last logged skip reason per slot, so a retry loop does not repeat itself. */
  private readonly skipReasons = new Map<string, string>();
  private stopping = false;

  constructor(options: DaemonRuntimeOptions = {}) {
    this.options = options;
    this.coordinator = new BotPoolCoordinator(options.poolDbPath, options.manifestPath, options.channelsDir);
    this.store = new DaemonStore(options.daemonDbPath);
    const endpoint = options.endpoint !== undefined ? options.endpoint : resolveGuiHostEndpoint();
    this.control = options.controlFactory
      ? options.controlFactory(event => this.fanOut(event), message => this.log(message))
      : new GuiHostSessionControl({
          endpoint,
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
    return this.publish(reports);
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
      const isRunning = Boolean(active?.forumManager?.running || active?.poller?.running);
      reports.push(
        active
          ? { slotId: slot.slotId, botId: slot.botId, workspace: slot.workspace, polling: isRunning }
          : await this.claim(slot),
      );
    }
    return this.publish(reports);
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
    if (activeSlot.forumManager) {
      void activeSlot.forumManager
        .start()
        .then(() => {
          if (!activeSlot.forumManager?.running) {
            this.onPollerEnd(slot.slotId, leaseSessionId, "forum loop terminated");
          }
        })
        .catch(error => {
          this.onPollerEnd(slot.slotId, leaseSessionId, error instanceof Error ? error.message : String(error));
        });
    } else if (activeSlot.poller) {
      void activeSlot.poller
        .start()
        .then(() => {
          if (!activeSlot.poller?.running) {
            this.onPollerEnd(slot.slotId, leaseSessionId, "loop terminated");
          }
        })
        .catch(error => {
          this.onPollerEnd(slot.slotId, leaseSessionId, error instanceof Error ? error.message : String(error));
        });
    }
    this.skipReasons.delete(slot.slotId);
    this.log(`Slot ${slot.slotId} polling (bot ${slot.botId}, workspace ${slot.workspace ?? "unresolved"}, mode ${slot.mode ?? "dm"})`);
    return { slotId: slot.slotId, botId: slot.botId, workspace: slot.workspace, polling: true };
  }

  private onPollerEnd(slotId: string, leaseSessionId: string, reason: string): void {
    if (this.stopping) return;
    const index = this.active.findIndex(entry => entry.slot.slotId === slotId);
    if (index < 0) return;
    this.active[index].stopMiniApp();
    this.active.splice(index, 1);
    this.coordinator.releaseLease(slotId, leaseSessionId);
    this.skipReasons.set(slotId, `poller ended: ${reason}`);
    this.log(`Slot ${slotId} poller ended: ${reason}`);
    this.publish(this.currentReports());
  }

  /** True when any opted-in slot is not currently active and polling. */
  public hasPendingSlots(): boolean {
    return this.optedInSlots().some(slot => {
      const active = this.active.find(entry => entry.slot.slotId === slot.slotId);
      const isRunning = Boolean(active?.forumManager?.running || active?.poller?.running);
      return !active || !isRunning;
    });
  }
  private currentReports(): DaemonSlotReport[] {
    return this.optedInSlots().map(slot => {
      const active = this.active.find(entry => entry.slot.slotId === slot.slotId);
      if (active) {
        const isRunning = Boolean(active.forumManager?.running || active.poller?.running);
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
      endpoint: this.control.endpoint,
      slots,
    };
    this.writeStatus(report);
    return report;
  }

  private startSlot(slot: DaemonSlot, token: string, leaseSessionId: string): ActiveSlot {
    if (slot.mode === "forum" && slot.forumChatId) {
      const client = this.options.forumClientFactory
        ? this.options.forumClientFactory(token, slot.forumChatId)
        : undefined;
      const forumManager = new ForumManager({
        slot,
        token,
        forumChatId: slot.forumChatId,
        store: this.store,
        control: this.control,
        client,
        log: message => this.log(message),
      });
      return {
        slot,
        forumManager,
        leaseSessionId,
        stopMiniApp: () => {},
      };
    }

    const access = this.coordinator.readAccessConfig(slot.stateDir);
    // Assigned after construction: the poller and the router each need the other,
    // and the poller is what knows which chat a callback is currently serving.
    let poller: TelegramPoller;

    const currentChat = (): string => poller.getPrimaryChatId() ?? "";
    const router = new SlotRouter({
      slot,
      store: this.store,
      control: this.control,
      send: async (chatId, html) => {
        for (const chunk of chunkMessage(html)) await poller.sendTelegramMessage(chatId, chunk, "HTML");
      },
      relay: async (chatId, markdown) => {
        for (const chunk of chunkMessage(markdown)) await poller.sendTelegramMessage(chatId, chunk);
      },
      log: message => this.log(message),
    });

    const sessionIdForChat = (): string => router.boundSession(currentChat()) ?? leaseSessionId;
    const correlation: MessageCorrelationBridge = {
      getSessionId: sessionIdForChat,
      getSlotId: () => slot.slotId,
      record: correlationRow => {
        this.coordinator.recordOutboundMessage(correlationRow);
      },
      resolveReply: (botId, chatId, replyToMessageId) =>
        this.coordinator.resolveReplyRouting(botId, chatId, replyToMessageId, sessionIdForChat()),
      resolveCallback: (callbackToken, userId, chatId) =>
        this.coordinator.validateDecisionCallback(callbackToken, userId, chatId, sessionIdForChat()),
      consumeCallback: callbackToken => this.coordinator.consumeDecisionCallback(callbackToken),
    };

    const runner = new BunCommandRunner();
    const callbacks = {
      isIdle: () => !router.isBusy(currentChat()),
      onUserMessage: (text: string) => {
        void this.acknowledge(router, currentChat(), text, "auto");
      },
      onSteer: (text: string) => {
        void this.acknowledge(router, currentChat(), text, "steer");
      },
      onFollowUp: (text: string) => {
        void this.acknowledge(router, currentChat(), text, "followUp");
      },
      onAbort: () => {
        const chatId = currentChat();
        void router.abort(chatId).catch(error => {
          this.log(`Slot ${slot.slotId}: abort failed: ${error instanceof Error ? error.message : String(error)}`);
        });
      },
      onRelease: async () => {
        await this.stopSlot(slot.slotId);
      },
      getStatusText: () => router.statusText(currentChat()),
      onTelegramTurnStart: () => {},
      onHarnessCommand: async (text: string, chatId: string, userId?: string) => {
        if (/^\/app(?:@\w+)?\s*$/i.test(text)) {
          const url = miniAppUrl(slot.stateDir);
          if (url) {
            await poller.sendTelegramMessage(chatId, "Open your Superboard dashboard", {
              inline_keyboard: [[{ text: "Open Superboard", web_app: { url } }]],
            });
          } else await poller.sendTelegramMessage(chatId, "Mini App is not configured for this bot.");
          return true;
        }
        if (await router.handleCommand(text, chatId)) return true;
        return handleInstalledCommand(text, {
          session: () => ({
            id: router.boundSession(chatId) ?? leaseSessionId,
            cwd: slot.workspace ?? "",
            idle: !router.isBusy(chatId),
            stateDir: slot.stateDir,
          }),
          send: async html => {
            for (const chunk of chunkMessage(html)) await poller.sendTelegramMessage(chatId, chunk, "HTML");
          },
          photo: (file, caption) => poller.sendTelegramPhoto(chatId, file, caption),
          mediaGroup: (files, caption) => poller.sendMediaGroup(chatId, files, caption),
          latestPng: async () => null,
          inbound: async message => {
            await router.deliver(chatId, message, "auto");
          },
        }, runner);
      },
      onDecisionCallback: async (decisionId: string, choiceId: string, context?: string) => {
        const chatId = currentChat();
        await router.deliver(
          chatId,
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
    };

    const pollerOptions = {
      commands: getDaemonCommands(),
    };

    poller = this.options.pollerFactory
      ? this.options.pollerFactory(token, slot.stateDir, access, callbacks, correlation, pollerOptions)
      : new TelegramPoller(token, slot.stateDir, access, callbacks, correlation, pollerOptions);

    const stopMiniApp = connectMiniApp({
      stateDir: slot.stateDir, token, allowedUsers: access.allowFrom,
      session: userId => router.boundSession(userId),
      sessions: () => this.control.listSessions(),
      status: () => ({ polling: poller.running, slot: slot.slotId }),
      dashboard: userId => {
        const session = router.boundSession(userId);
        const raw = session ? poller.getMeta(`dashboard-snapshot:${session}`) : null;
        try { return raw ? JSON.parse(raw) : null; } catch { return null; }
      },
    });
    const url = miniAppUrl(slot.stateDir);
    if (url) for (const chatId of access.allowFrom) void fetch(`https://api.telegram.org/bot${token}/setChatMenuButton`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, menu_button: { type: "web_app", text: "Superboard", web_app: { url } } }),
    }).then(async response => {
      const result = await response.json();
      if (!response.ok || !result.ok) this.log(`Slot ${slot.slotId}: Mini App menu registration rejected`);
    }).catch(() => this.log(`Slot ${slot.slotId}: Mini App menu registration unavailable`));
    return { slot, poller, router, leaseSessionId, stopMiniApp };
  }

  /**
   * Delivers text and posts the router's acknowledgement. Poller message callbacks
   * are fire-and-forget, so a failure here has to reach the operator's chat rather
   * than becoming an unhandled rejection in a background loop.
   */
  private async acknowledge(
    router: SlotRouter,
    chatId: string,
    text: string,
    mode: "auto" | "steer" | "followUp",
  ): Promise<void> {
    try {
      const ack = await router.deliver(chatId, text, mode);
      if (ack) for (const chunk of chunkMessage(ack)) await this.sendTo(router, chatId, chunk);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      this.log(`Delivery to chat ${chatId} failed: ${detail}`);
      await this.sendTo(router, chatId, `⚠️ <b>Not delivered.</b> ${detail}`).catch(() => undefined);
    }
  }

  private async sendTo(router: SlotRouter, chatId: string, html: string): Promise<void> {
    const active = this.active.find(entry => entry.router === router);
    await active?.poller.sendTelegramMessage(chatId, html, "HTML");
  }

  private fanOut(event: SessionEvent): void {
    for (const entry of this.active) {
      if (entry.forumManager) {
        void entry.forumManager.onSessionEvent(event).catch(error => {
          this.log(`Slot ${entry.slot.slotId}: forum session event handling failed: ${error instanceof Error ? error.message : String(error)}`);
        });
      }
      if (entry.router) {
        void entry.router.onSessionEvent(event).catch(error => {
          this.log(`Slot ${entry.slot.slotId}: session event handling failed: ${error instanceof Error ? error.message : String(error)}`);
        });
      }
    }
  }

  public async stopSlot(slotId: string): Promise<boolean> {
    const index = this.active.findIndex(entry => entry.slot.slotId === slotId);
    if (index < 0) return false;
    const [entry] = this.active.splice(index, 1);
    entry.stopMiniApp();
    if (entry.forumManager) await entry.forumManager.stop();
    if (entry.poller) await entry.poller.stop();
    this.coordinator.releaseLease(entry.slot.slotId, entry.leaseSessionId);
    this.log(`Slot ${entry.slot.slotId} stopped and lease released`);
    return true;
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
      endpoint: this.control.endpoint,
      slots: this.active.map(entry => ({
        slotId: entry.slot.slotId,
        botId: entry.slot.botId,
        workspace: entry.slot.workspace,
        polling: Boolean(entry.forumManager?.running || entry.poller?.running),
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
