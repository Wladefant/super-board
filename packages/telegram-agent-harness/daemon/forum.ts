/**
 * forum.ts — Opt-in supergroup forum-topics mode for the Veyyon Telegram daemon.
 *
 * Why this exists:
 * The default daemon mode talks to the operator in a private chat, and one chat is
 * bound to one session. In a team or multi-session setting an operator can enable
 * forum mode on a slot by setting:
 *   "mode": "forum"
 *   "forumChatId": "-100..."
 * in manifest.json. A forum topic then plays the part a private chat plays in DM
 * mode: one topic is bound to one session.
 *
 * What lives here is only the part of that which is about TOPICS: creating one for a
 * session, closing it, and listing them. Everything else — authorization, the update
 * ledger, routing commands, delivery, acknowledgements, outbound relay — is the same
 * machinery DM mode uses (`TelegramPoller` and `SlotRouter`), keyed on the topic
 * instead of the chat. A forum slot therefore runs exactly one Telegram consumer,
 * like every other slot; an earlier design polled `getUpdates` here as well, and the
 * two consumers stole each other's updates.
 */

import * as path from "node:path";
import { escapeHtml } from "../extension/sanitizer";
import type { DaemonSlot } from "./config";
import type { DaemonSessionSummary, GuiHostSessionControl } from "./session-control";
import {
  getSessionWorkspace,
  isTopLevelSession,
  type RouteTarget,
  type SlotRouter,
  type TopicLifecycle,
} from "./router";
import type { DaemonStore } from "./store";

export interface ForumTopic {
  message_thread_id: number;
  name: string;
  icon_color?: number;
  icon_custom_emoji_id?: string;
}

export interface ForumApiClient {
  createForumTopic(chatId: string | number, name: string, iconColor?: number): Promise<ForumTopic>;
  closeForumTopic(chatId: string | number, messageThreadId: number): Promise<boolean>;
  reopenForumTopic(chatId: string | number, messageThreadId: number): Promise<boolean>;
  sendMessage(
    chatId: string | number,
    text: string,
    options?: {
      message_thread_id?: number;
      parse_mode?: string;
      reply_markup?: Record<string, unknown>;
    },
  ): Promise<{ ok: boolean; result?: { message_id: number } }>;
}

export class DefaultTelegramForumClient implements ForumApiClient {
  private readonly token: string;
  private readonly apiBaseUrl: string;
  private readonly customFetch: typeof fetch;

  constructor(token: string, apiBaseUrl = "https://api.telegram.org", customFetch: typeof fetch = fetch) {
    this.token = token;
    this.apiBaseUrl = apiBaseUrl.replace(/\/+$/, "");
    this.customFetch = customFetch;
  }

  private async call<T>(method: string, body: Record<string, unknown>): Promise<T> {
    const url = `${this.apiBaseUrl}/bot${this.token}/${method}`;
    const response = await this.customFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      let errDetail = `HTTP ${response.status} ${response.statusText}`;
      try {
        const json = await response.json() as { description?: string };
        if (json.description) errDetail = json.description;
      } catch {}
      throw new Error(`Telegram API ${method} failed: ${errDetail}`);
    }
    return (await response.json()) as T;
  }

  public async createForumTopic(chatId: string | number, name: string, iconColor?: number): Promise<ForumTopic> {
    const payload: Record<string, unknown> = {
      chat_id: chatId,
      name: name.slice(0, 128),
    };
    if (iconColor !== undefined) payload.icon_color = iconColor;
    const res = await this.call<{ ok: boolean; result: ForumTopic }>("createForumTopic", payload);
    if (!res.ok || !res.result) throw new Error("createForumTopic returned no result");
    return res.result;
  }

  public async closeForumTopic(chatId: string | number, messageThreadId: number): Promise<boolean> {
    const res = await this.call<{ ok: boolean; result: boolean }>("closeForumTopic", {
      chat_id: chatId,
      message_thread_id: messageThreadId,
    });
    return Boolean(res.ok && res.result);
  }

  public async reopenForumTopic(chatId: string | number, messageThreadId: number): Promise<boolean> {
    const res = await this.call<{ ok: boolean; result: boolean }>("reopenForumTopic", {
      chat_id: chatId,
      message_thread_id: messageThreadId,
    });
    return Boolean(res.ok && res.result);
  }

  public async sendMessage(
    chatId: string | number,
    text: string,
    options?: {
      message_thread_id?: number;
      parse_mode?: string;
      reply_markup?: Record<string, unknown>;
    },
  ): Promise<{ ok: boolean; result?: { message_id: number } }> {
    const payload: Record<string, unknown> = {
      chat_id: chatId,
      text,
      parse_mode: options?.parse_mode ?? "HTML",
    };
    if (options?.message_thread_id !== undefined) {
      payload.message_thread_id = options.message_thread_id;
    }
    if (options?.reply_markup) {
      payload.reply_markup = options.reply_markup;
    }
    return this.call<{ ok: boolean; result?: { message_id: number } }>("sendMessage", payload);
  }
}

export function normalizeWorkspace(workspace: string): string {
  return workspace.replace(/\\/g, "/").replace(/\/+$/, "");
}

export function workspaceFolder(workspace: string): string {
  const norm = normalizeWorkspace(workspace);
  return norm.split("/").filter(Boolean).at(-1) ?? norm;
}

export function formatTopicName(
  sessionId: string,
  workspace: string,
  title?: string | null,
  ordinal?: number,
): string {
  const folder = workspace.replace(/\\/g, "/").split("/").filter(Boolean).at(-1);
  const base = folder || title?.trim() || path.basename(workspace) || "Session";
  const suffixed = ordinal && ordinal > 1 ? `${base} (${ordinal})` : base;
  return suffixed.slice(0, 128);
}

export interface AutoAttachAction {
  action: "create" | "rebind" | "skip";
  sessionId: string;
  workspace: string;
  folder: string;
  topicId?: number;
  topicName?: string;
  reason?: string;
}

export interface AutoAttachResult {
  actions: AutoAttachAction[];
  created: number;
  rebound: number;
  skipped: number;
  errors: number;
}

export interface ForumManagerOptions {
  slot: DaemonSlot;
  token: string;
  forumChatId: string;
  store: DaemonStore;
  control: GuiHostSessionControl;
  apiBaseUrl?: string;
  fetch?: typeof fetch;
  client?: ForumApiClient;
  log?: (message: string) => void;
}

/**
 * Topic lifecycle for one forum slot: the router asks it to open a topic when a
 * session needs one and to close a topic the operator detached.
 *
 * It deliberately does NOT close topics on its own. The GUI host reports a session's
 * TURN status (`Unknown`, `Complete`, `Interrupted`), not whether the session still
 * exists, and a session can drop out of a listing while it is alive — an earlier
 * reconcile loop read that absence as "ended" and closed a live operator's topic
 * seconds after opening it (daemon.log 2026-09-20T18:17:24Z closed topic #9). A
 * topic and its scrollback belong to the operator; only `/detach` closes one.
 */
export class ForumManager implements TopicLifecycle {
  public readonly options: ForumManagerOptions;
  public readonly client: ForumApiClient;
  private readonly workspaceBackoffs = new Map<string, { until: number; delayMs: number }>();

  constructor(options: ForumManagerOptions) {
    this.options = options;
    this.client =
      options.client ??
      new DefaultTelegramForumClient(options.token, options.apiBaseUrl, options.fetch);
  }

  public get slotId(): string {
    return this.options.slot.slotId;
  }

  public get forumChatId(): string {
    return String(this.options.forumChatId);
  }

  private log(message: string): void {
    if (this.options.log) {
      this.options.log(`[ForumSlot ${this.slotId}] ${message}`);
    }
  }

  /**
   * Topic for `sessionId`, creating one when the session has none yet. The route is
   * written here so the topic is routable the moment Telegram reports it, even if
   * the caller's own binding step fails.
   */
  public async ensureTopic(
    sessionId: string,
    workspace: string,
    title?: string | null,
    ordinal?: number,
    liveSessionIds?: Set<string>,
  ): Promise<number> {
    const existing = this.options.store
      .routesForSession(sessionId)
      .find(route => route.slotId === this.slotId && route.chatId === this.forumChatId && route.topicId !== "");
    if (existing) {
      return Number(existing.topicId);
    }

    const normTarget = normalizeWorkspace(workspace).toLowerCase();
    const folderTarget = workspaceFolder(workspace).toLowerCase();

    const candidateRoutes = this.options.store
      .listRoutes(this.slotId)
      .filter(route => route.chatId === this.forumChatId && route.topicId !== "");

    const deadWorkspaceRoute = candidateRoutes.find(route => {
      const isDead = !liveSessionIds || !liveSessionIds.has(route.sessionId);
      if (!isDead) return false;
      return normalizeWorkspace(route.workspace).toLowerCase() === normTarget;
    }) ?? candidateRoutes.find(route => {
      const isDead = !liveSessionIds || !liveSessionIds.has(route.sessionId);
      if (!isDead) return false;
      return workspaceFolder(route.workspace).toLowerCase() === folderTarget;
    }) ?? candidateRoutes.find(route => {
      const isDead = !liveSessionIds || !liveSessionIds.has(route.sessionId);
      if (!isDead) return false;
      return workspaceFolder(route.workspace).toLowerCase().replace(/[-_]/g, "") === folderTarget.replace(/[-_]/g, "");
    });

    if (deadWorkspaceRoute) {
      this.options.store.putRoute({
        slotId: this.slotId,
        chatId: this.forumChatId,
        topicId: deadWorkspaceRoute.topicId,
        sessionId,
        workspace,
      });
      const threadId = Number(deadWorkspaceRoute.topicId);
      this.log(`Reused forum topic #${threadId} for workspace ${workspace} (rebound to session ${sessionId})`);

      const note = `🔁 <b>Rebound to session</b> <code>${escapeHtml(sessionId)}</code>`;
      await this.client.sendMessage(this.forumChatId, note, {
        message_thread_id: threadId,
        parse_mode: "HTML",
      }).catch(err => {
        this.log(`Could not send rebind note to topic #${threadId}: ${err instanceof Error ? err.message : String(err)}`);
      });

      return threadId;
    }

    const topicName = formatTopicName(sessionId, workspace, title, ordinal);
    const created = await this.client.createForumTopic(this.forumChatId, topicName);
    const threadId = created.message_thread_id;

    this.options.store.putRoute({
      slotId: this.slotId,
      chatId: this.forumChatId,
      topicId: String(threadId),
      sessionId,
      workspace,
    });

    const welcome = [
      `🆕 <b>Veyyon session attached</b>`,
      `Session: <code>${escapeHtml(sessionId)}</code>`,
      `Workspace: <code>${escapeHtml(workspace)}</code>`,
      ``,
      `<i>Write in this topic to steer the session. <code>/detach close</code> closes it.</i>`,
    ].join("\n");

    await this.client.sendMessage(this.forumChatId, welcome, {
      message_thread_id: threadId,
      parse_mode: "HTML",
    }).catch(err => {
      this.log(`Could not send welcome message to topic #${threadId}: ${err instanceof Error ? err.message : String(err)}`);
    });

    this.log(`Created forum topic #${threadId} for session ${sessionId}`);
    return threadId;
  }

  /**
   * Closes a topic on Telegram. The route is the router's to remove; this is the
   * Telegram-side half, so closing an already-unbound topic still works.
   */
  public async closeTopic(messageThreadId: number): Promise<boolean> {
    try {
      await this.client.closeForumTopic(this.forumChatId, messageThreadId);
      this.log(`Closed forum topic #${messageThreadId}`);
      return true;
    } catch (err) {
      this.log(`closeForumTopic #${messageThreadId} failed: ${err instanceof Error ? err.message : String(err)}`);
      return false;
    }
  }

  /** `/topics`: every topic of this slot's forum and the session behind it. */
  public listTopicsText(currentTopicId: string): string {
    const routes = this.options.store
      .listRoutes(this.slotId)
      .filter(route => route.chatId === this.forumChatId && route.topicId !== "");

    if (routes.length === 0) {
      return [
        "ℹ️ <b>No forum topics are bound to a session.</b>",
        "Use <code>/new</code> to start a session in its own topic.",
      ].join("\n");
    }

    const lines = routes.map(route => {
      const marker = route.topicId === currentTopicId ? "➡️" : "•";
      const busy = this.options.control.isBusy(route.sessionId) ? "running" : "idle";
      return `${marker} <b>#${escapeHtml(route.topicId)}</b>: <code>${escapeHtml(route.sessionId)}</code> — <i>${escapeHtml(route.workspace)}</i> (${busy})`;
    });

    return [
      `📋 <b>Session topics (${routes.length})</b>`,
      ...lines,
      "",
      "Write inside a topic to steer its session, or open another with <code>/new</code>.",
    ].join("\n");
  }
  private isWorkspaceInBackoff(workspace: string): boolean {
    const key = normalizeWorkspace(workspace).toLowerCase();
    const entry = this.workspaceBackoffs.get(key);
    if (!entry) return false;
    if (Date.now() >= entry.until) {
      this.workspaceBackoffs.delete(key);
      return false;
    }
    return true;
  }

  private recordWorkspaceError(workspace: string, _error: unknown): void {
    const key = normalizeWorkspace(workspace).toLowerCase();
    const prev = this.workspaceBackoffs.get(key);
    const delayMs = prev ? Math.min(prev.delayMs * 2, 120_000) : 20_000;
    this.workspaceBackoffs.set(key, { until: Date.now() + delayMs, delayMs });
  }

  public clearWorkspaceBackoff(workspace: string): void {
    const key = normalizeWorkspace(workspace).toLowerCase();
    this.workspaceBackoffs.delete(key);
  }

  /**
   * Auto-attaches live top-level Veyyon sessions to forum topics:
   * - Rebinds existing topics whose bound session is dead
   * - Creates new topics (with ordinals if multiple live in the same folder)
   * - Idempotent, never closes topics or touches routes of live sessions
   */
  public async reconcileAutoAttach(
    bindingTarget?: SlotRouter | ((target: RouteTarget, sessionId: string, workspace: string) => Promise<void>),
    options?: { dryRun?: boolean },
  ): Promise<AutoAttachResult> {
    const dryRun = Boolean(options?.dryRun);
    const actions: AutoAttachAction[] = [];
    let createdCount = 0;
    let reboundCount = 0;
    let skippedCount = 0;
    let errorCount = 0;

    let wireSessions: DaemonSessionSummary[];
    try {
      wireSessions = await this.options.control.listSessions();
    } catch (err) {
      this.log(`Auto-attach reconcile skipped: cannot list wire sessions (${err instanceof Error ? err.message : String(err)})`);
      return { actions: [], created: 0, rebound: 0, skipped: 0, errors: 1 };
    }

    // Source of truth = live wire sessions only, top-level with non-empty workspace
    const liveSessions = wireSessions.filter(s => isTopLevelSession(s) && Boolean(getSessionWorkspace(s)));
    const liveSessionIds = new Set(liveSessions.map(s => s.id));

    const allRoutes = this.options.store
      .listRoutes(this.slotId)
      .filter(route => route.chatId === this.forumChatId && route.topicId !== "");
    const routesBySessionId = new Map<string, typeof allRoutes[0]>();
    for (const r of allRoutes) {
      routesBySessionId.set(r.sessionId, r);
    }

    // Dead routes: routes whose bound session is NOT in liveSessionIds
    const availableDeadRoutes = allRoutes.filter(r => !liveSessionIds.has(r.sessionId));

    // Count live sessions per folder that already have a topic
    const liveCountByFolder = new Map<string, number>();
    for (const s of liveSessions) {
      if (routesBySessionId.has(s.id)) {
        const f = workspaceFolder(getSessionWorkspace(s)).toLowerCase();
        liveCountByFolder.set(f, (liveCountByFolder.get(f) ?? 0) + 1);
      }
    }

    const bind = async (target: RouteTarget, sessionId: string, workspace: string): Promise<void> => {
      if (bindingTarget) {
        if ("bind" in bindingTarget && typeof bindingTarget.bind === "function") {
          await bindingTarget.bind(target, sessionId, workspace);
        } else if (typeof bindingTarget === "function") {
          await bindingTarget(target, sessionId, workspace);
        }
      } else {
        this.options.store.putRoute({
          slotId: this.slotId,
          chatId: target.chatId,
          topicId: target.topicId,
          sessionId,
          workspace,
        });
        await this.options.control.loadTranscript(sessionId).catch(() => undefined);
      }
    };

    for (const session of liveSessions) {
      const ws = getSessionWorkspace(session);
      const folder = workspaceFolder(ws);
      const folderKey = folder.toLowerCase();
      const normWs = normalizeWorkspace(ws).toLowerCase();

      // Session already has a route on this forum
      const existingRoute = routesBySessionId.get(session.id);
      if (existingRoute) {
        actions.push({
          action: "skip",
          sessionId: session.id,
          workspace: ws,
          folder,
          topicId: Number(existingRoute.topicId),
          reason: `already bound to topic #${existingRoute.topicId}`,
        });
        skippedCount++;
        continue;
      }

      // Check if a dead route exists for the same normalized workspace folder
      let deadIndex = availableDeadRoutes.findIndex(r => normalizeWorkspace(r.workspace).toLowerCase() === normWs);
      if (deadIndex === -1) {
        deadIndex = availableDeadRoutes.findIndex(r => workspaceFolder(r.workspace).toLowerCase() === folderKey);
      }
      if (deadIndex === -1) {
        deadIndex = availableDeadRoutes.findIndex(
          r => workspaceFolder(r.workspace).toLowerCase().replace(/[-_]/g, "") === folderKey.replace(/[-_]/g, ""),
        );
      }

      if (deadIndex >= 0) {
        const deadRoute = availableDeadRoutes.splice(deadIndex, 1)[0];
        const threadId = Number(deadRoute.topicId);

        actions.push({
          action: "rebind",
          sessionId: session.id,
          workspace: ws,
          folder,
          topicId: threadId,
          reason: `rebound from dead session ${deadRoute.sessionId}`,
        });

        if (dryRun) {
          reboundCount++;
          continue;
        }

        if (this.isWorkspaceInBackoff(ws)) {
          this.log(`Auto-attach skipping workspace ${ws} (session ${session.id}) due to active error backoff`);
          continue;
        }

        try {
          this.options.store.putRoute({
            slotId: this.slotId,
            chatId: this.forumChatId,
            topicId: deadRoute.topicId,
            sessionId: session.id,
            workspace: ws,
          });
          routesBySessionId.set(session.id, {
            slotId: this.slotId,
            chatId: this.forumChatId,
            topicId: deadRoute.topicId,
            sessionId: session.id,
            workspace: ws,
          });

          const note = `🔁 <b>Rebound to session</b> <code>${escapeHtml(session.id)}</code>`;
          await this.client.sendMessage(this.forumChatId, note, {
            message_thread_id: threadId,
            parse_mode: "HTML",
          }).catch(err => {
            this.log(`Could not send rebind note to topic #${threadId}: ${err instanceof Error ? err.message : String(err)}`);
          });

          await bind({ chatId: this.forumChatId, topicId: deadRoute.topicId }, session.id, ws);
          this.clearWorkspaceBackoff(ws);
          this.log(`Reused forum topic #${threadId} for workspace ${ws} (rebound to session ${session.id})`);
          reboundCount++;
        } catch (err) {
          errorCount++;
          this.recordWorkspaceError(ws, err);
          this.log(`Auto-attach rebind failed for workspace ${ws} (session ${session.id}): ${err instanceof Error ? err.message : String(err)}`);
        }
      } else {
        // No dead route: create a new topic
        const currentCount = liveCountByFolder.get(folderKey) ?? 0;
        const ordinal = currentCount + 1;
        liveCountByFolder.set(folderKey, ordinal);

        const topicName = formatTopicName(session.id, ws, session.title, ordinal);

        actions.push({
          action: "create",
          sessionId: session.id,
          workspace: ws,
          folder,
          topicName,
        });

        if (dryRun) {
          createdCount++;
          continue;
        }

        if (this.isWorkspaceInBackoff(ws)) {
          this.log(`Auto-attach skipping workspace ${ws} (session ${session.id}) due to active error backoff`);
          continue;
        }

        try {
          const created = await this.client.createForumTopic(this.forumChatId, topicName);
          const threadId = created.message_thread_id;

          this.options.store.putRoute({
            slotId: this.slotId,
            chatId: this.forumChatId,
            topicId: String(threadId),
            sessionId: session.id,
            workspace: ws,
          });
          routesBySessionId.set(session.id, {
            slotId: this.slotId,
            chatId: this.forumChatId,
            topicId: String(threadId),
            sessionId: session.id,
            workspace: ws,
          });

          const welcome = [
            `🆕 <b>Veyyon session attached</b>`,
            `Session: <code>${escapeHtml(session.id)}</code>`,
            `Workspace: <code>${escapeHtml(ws)}</code>`,
            ``,
            `<i>Write in this topic to steer the session. <code>/detach close</code> closes it.</i>`,
          ].join("\n");

          await this.client.sendMessage(this.forumChatId, welcome, {
            message_thread_id: threadId,
            parse_mode: "HTML",
          }).catch(err => {
            this.log(`Could not send welcome message to topic #${threadId}: ${err instanceof Error ? err.message : String(err)}`);
          });

          await bind({ chatId: this.forumChatId, topicId: String(threadId) }, session.id, ws);
          this.clearWorkspaceBackoff(ws);
          this.log(`Created forum topic #${threadId} (${topicName}) for session ${session.id}`);
          createdCount++;
        } catch (err) {
          errorCount++;
          this.recordWorkspaceError(ws, err);
          this.log(`Auto-attach failed to create topic for workspace ${ws} (session ${session.id}): ${err instanceof Error ? err.message : String(err)}`);
        }
      }
    }

    return { actions, created: createdCount, rebound: reboundCount, skipped: skippedCount, errors: errorCount };
  }
}
