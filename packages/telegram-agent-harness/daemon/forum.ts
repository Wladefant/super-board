/**
 * forum.ts — Opt-in supergroup forum-topics mode for the Veyyon Telegram daemon.
 *
 * Why this exists:
 * The default daemon mode uses 1-on-1 private DMs with the operator. In a team or
 * multi-session setting, an operator can enable forum mode on a slot by setting:
 *   "mode": "forum"
 *   "forumChatId": "-100..."
 * in manifest.json. In forum mode:
 * - One forum topic is created per Veyyon session via Telegram Bot API `createForumTopic`.
 * - Inbound messages inside a topic are routed by `message_thread_id` to its bound session.
 * - Outbound session events are posted into the session's topic thread.
 * - `/topics` lists all active topics and their bound sessions.
 * - When a session ends (or `/detach`/`/close` is run), the topic is closed via `closeForumTopic`.
 *
 * All forum logic is encapsulated here so DM mode remains 100% untouched.
 */

import * as path from "node:path";
import { escapeHtml, chunkMessage } from "../extension/sanitizer";
import type { DaemonSlot } from "./config";
import type {
  DeliveryMode,
  GuiHostSessionControl,
  SessionEvent,
} from "./session-control";
import { SessionControlUnavailableError } from "./session-control";
import type { DaemonStore, DaemonRoute } from "./store";

export interface ForumTopic {
  message_thread_id: number;
  name: string;
  icon_color?: number;
  icon_custom_emoji_id?: string;
}

export interface TelegramForumUpdate {
  update_id: number;
  message?: {
    message_id: number;
    message_thread_id?: number;
    is_topic_message?: boolean;
    from?: {
      id: number;
      is_bot: boolean;
      first_name?: string;
      username?: string;
    };
    chat: {
      id: number | string;
      type: "private" | "group" | "supergroup" | "channel";
      title?: string;
      username?: string;
    };
    date: number;
    text?: string;
  };
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
  getUpdates(options?: {
    offset?: number;
    limit?: number;
    timeout?: number;
    allowed_updates?: string[];
  }): Promise<{ ok: boolean; result?: TelegramForumUpdate[] }>;
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

  public async getUpdates(options?: {
    offset?: number;
    limit?: number;
    timeout?: number;
    allowed_updates?: string[];
  }): Promise<{ ok: boolean; result?: TelegramForumUpdate[] }> {
    const payload: Record<string, unknown> = {
      offset: options?.offset ?? 0,
      limit: options?.limit ?? 100,
      timeout: options?.timeout ?? 10,
      allowed_updates: options?.allowed_updates ?? ["message"],
    };
    return this.call<{ ok: boolean; result?: TelegramForumUpdate[] }>("getUpdates", payload);
  }
}

export function formatTopicName(sessionId: string, workspace: string, title?: string | null): string {
  const base = title?.trim() || path.basename(workspace) || "Session";
  const label = `${base} (${sessionId.slice(0, 8)})`;
  return label.slice(0, 128);
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
  pollIntervalMs?: number;
}

export class ForumManager {
  public readonly options: ForumManagerOptions;
  public readonly client: ForumApiClient;
  private isRunning = false;
  private pollOffset = 0;
  private pollingPromise: Promise<void> | null = null;
  private abortController: AbortController | null = null;

  constructor(options: ForumManagerOptions) {
    this.options = options;
    this.client =
      options.client ??
      new DefaultTelegramForumClient(options.token, options.apiBaseUrl, options.fetch);
  }

  public get running(): boolean {
    return this.isRunning;
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
   * Starts the forum manager's long-polling update loop.
   */
  public async start(): Promise<void> {
    if (this.isRunning) return;
    this.isRunning = true;
    this.abortController = new AbortController();
    this.log(`Started forum manager on chat ${this.forumChatId}`);
    this.pollingPromise = this.runPollLoop();
  }

  /**
   * Stops the forum manager and aborts active polling.
   */
  public async stop(): Promise<void> {
    if (!this.isRunning) return;
    this.isRunning = false;
    this.abortController?.abort();
    try {
      await this.pollingPromise;
    } catch {}
    this.log(`Stopped forum manager on chat ${this.forumChatId}`);
  }

  private async runPollLoop(): Promise<void> {
    while (this.isRunning) {
      try {
        const response = await this.client.getUpdates({
          offset: this.pollOffset,
          timeout: 5,
          allowed_updates: ["message"],
        });
        if (response?.ok && Array.isArray(response.result)) {
          for (const update of response.result) {
            this.pollOffset = Math.max(this.pollOffset, update.update_id + 1);
            await this.processUpdate(update);
          }
        }
        await this.reconcileEndedSessions().catch(() => {});
      } catch (error) {
        if (!this.isRunning) break;
        const msg = error instanceof Error ? error.message : String(error);
        this.log(`Poll error: ${msg}`);
        const { promise, resolve } = Promise.withResolvers<void>();
        setTimeout(resolve, this.options.pollIntervalMs ?? 2000);
        await promise;
      }
    }
  }

  /**
   * Processes a single inbound update from Telegram.
   */
  public async processUpdate(update: TelegramForumUpdate): Promise<void> {
    const msg = update.message;
    if (!msg || String(msg.chat.id) !== this.forumChatId) return;

    const text = msg.text?.trim();
    if (!text) return;

    const threadId = msg.message_thread_id;
    const reply = await this.handleMessage(text, threadId, msg.from?.id ? String(msg.from.id) : undefined);
    if (reply) {
      for (const chunk of chunkMessage(reply)) {
        await this.client.sendMessage(this.forumChatId, chunk, {
          message_thread_id: threadId,
          parse_mode: "HTML",
        }).catch(err => {
          this.log(`Failed to reply in thread ${threadId}: ${err instanceof Error ? err.message : String(err)}`);
        });
      }
    }
  }

  /**
   * Ensures a forum topic exists for the given Veyyon session.
   * If a route already exists for this session in this slot/forum, returns its topic ID.
   * Otherwise calls `createForumTopic` on the Bot API and stores the route.
   */
  public async ensureTopic(sessionId: string, workspace: string, title?: string | null): Promise<number> {
    const existing = this.options.store
      .routesForSession(sessionId)
      .find(r => r.slotId === this.slotId && r.chatId === this.forumChatId && r.topicId !== "");
    if (existing) {
      return Number(existing.topicId);
    }

    const topicName = formatTopicName(sessionId, workspace, title);
    const created = await this.client.createForumTopic(this.forumChatId, topicName);
    const threadId = created.message_thread_id;

    this.options.store.putRoute({
      slotId: this.slotId,
      chatId: this.forumChatId,
      topicId: String(threadId),
      sessionId,
      workspace,
    });

    // Send opening status message in topic
    const welcome = [
      `🆕 <b>Veyyon Session Attached</b>`,
      `Topic: <b>#${threadId}</b>`,
      `Session: <code>${escapeHtml(sessionId)}</code>`,
      `Workspace: <code>${escapeHtml(workspace)}</code>`,
      ``,
      `<i>Send messages in this topic to steer the session. Use /detach to close this topic.</i>`,
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
   * Closes a forum topic via `closeForumTopic` and removes its route from the store.
   */
  public async closeTopic(messageThreadId: number): Promise<boolean> {
    const route = this.options.store.getRoute(this.slotId, this.forumChatId, String(messageThreadId));
    if (route) {
      this.options.store.deleteRoute(this.slotId, this.forumChatId, String(messageThreadId));
    }
    try {
      await this.client.closeForumTopic(this.forumChatId, messageThreadId);
      this.log(`Closed forum topic #${messageThreadId}`);
      return true;
    } catch (err) {
      this.log(`closeForumTopic #${messageThreadId} failed: ${err instanceof Error ? err.message : String(err)}`);
      return false;
    }
  }

  /**
   * Closes all forum topics associated with a session ID.
   */
  public async closeSessionTopic(sessionId: string): Promise<number> {
    const routes = this.options.store
      .routesForSession(sessionId)
      .filter(r => r.slotId === this.slotId && r.chatId === this.forumChatId && r.topicId !== "");
    let closed = 0;
    for (const route of routes) {
      const threadId = Number(route.topicId);
      if (Number.isFinite(threadId)) {
        await this.closeTopic(threadId);
        closed++;
      }
    }
    return closed;
  }

  /**
   * Reconciles ended sessions: closes forum topics whose session is no longer
   * reported by the GUI host or whose status is Closed/Ended.
   */
  public async reconcileEndedSessions(): Promise<number> {
    try {
      const activeSessions = await this.options.control.listSessions();
      const activeIds = new Map(activeSessions.map(s => [s.id, s.status]));
      const routes = this.options.store
        .listRoutes(this.slotId)
        .filter(r => r.chatId === this.forumChatId && r.topicId !== "");

      let closed = 0;
      for (const route of routes) {
        const status = activeIds.get(route.sessionId);
        const isClosed = !status || status.toLowerCase() === "closed" || status.toLowerCase() === "ended";
        if (isClosed) {
          const threadId = Number(route.topicId);
          if (Number.isFinite(threadId)) {
            await this.closeTopic(threadId);
            closed++;
          }
        }
      }
      return closed;
    } catch {
      return 0;
    }
  }

  /**
   * Handles an inbound message inside the supergroup forum.
   * Dispatches commands (`/topics`, `/new`, `/attach`, `/detach`, `/where`, `/sessions`, `/help`)
   * or delivers operator text to the topic's bound session.
   */
  public async handleMessage(
    text: string,
    messageThreadId: number | undefined,
    userId?: string,
  ): Promise<string | null> {
    const trimmed = text.trim();
    const isCommand = trimmed.startsWith("/");

    if (isCommand) {
      return this.handleCommand(trimmed, messageThreadId);
    }

    // In General topic or unthreaded messages:
    if (messageThreadId === undefined || messageThreadId <= 1) {
      return [
        "ℹ️ <b>Veyyon Supergroup Forum Mode</b>",
        "Please send messages inside a specific session topic.",
        "Use <code>/topics</code> to list active topics, or <code>/new</code> to start a new session topic.",
      ].join("\n");
    }

    // Threaded message: route by message_thread_id
    const route = this.options.store.getRoute(this.slotId, this.forumChatId, String(messageThreadId));
    if (!route) {
      return [
        `⚠️ <b>Unbound Topic (#${messageThreadId})</b>`,
        `This topic is not bound to an active Veyyon session.`,
        `Use <code>/attach &lt;session-id&gt;</code> to bind this topic, or <code>/new</code> to create one.`,
      ].join("\n");
    }

    try {
      const outcome = await this.options.control.deliver(route.sessionId, text, "auto");
      if (outcome === "started") return null;
      return `↪️ <b>Queued as a ${outcome === "steered" ? "steer" : "follow-up"}</b> for the running turn.`;
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      return `⚠️ <b>Delivery failed:</b> ${escapeHtml(detail)}`;
    }
  }

  /**
   * Handles commands inside the forum.
   */
  public async handleCommand(text: string, messageThreadId: number | undefined): Promise<string> {
    const [rawVerb, ...rest] = text.trim().split(/\s+/);
    const verb = rawVerb.toLowerCase().replace(/@\w+$/, "");
    const arg = rest.join(" ").trim();

    if (verb === "/topics") {
      return this.listTopicsText(messageThreadId);
    }

    if (verb === "/help") {
      return [
        "🤖 <b>Veyyon Forum Mode Commands</b>",
        "• <code>/topics</code> — List all active forum topics and bound sessions",
        "• <code>/new [workspace]</code> — Create a new session and forum topic",
        "• <code>/attach &lt;session-id&gt;</code> — Attach topic to an existing session",
        "• <code>/detach</code> or <code>/close</code> — Close this topic and detach session",
        "• <code>/where</code> — Show session status for this topic",
        "• <code>/sessions</code> — List all running Veyyon sessions",
        "• <code>/help</code> — Show this help message",
      ].join("\n");
    }

    if (verb === "/sessions") {
      try {
        const sessions = await this.options.control.listSessions();
        if (sessions.length === 0) return "ℹ️ <b>No Veyyon sessions are running.</b>";
        const lines = sessions.slice(0, 15).map(s => {
          const label = s.title ?? (s.cwd || "untitled");
          return `• <code>${escapeHtml(s.id)}</code> — ${escapeHtml(label)} <i>(${escapeHtml(s.status)})</i>`;
        });
        return ["🗂 <b>Running Veyyon Sessions</b>", ...lines, "", "Use <code>/attach &lt;id&gt;</code> or <code>/new</code>."].join("\n");
      } catch (err) {
        return `⚠️ <b>Could not list sessions:</b> ${escapeHtml(err instanceof Error ? err.message : String(err))}`;
      }
    }

    if (verb === "/where") {
      if (messageThreadId !== undefined && messageThreadId > 1) {
        const route = this.options.store.getRoute(this.slotId, this.forumChatId, String(messageThreadId));
        if (route) {
          const isBusy = this.options.control.isBusy(route.sessionId);
          return [
            `🤖 <b>Topic #${messageThreadId}</b>`,
            `Session: <code>${escapeHtml(route.sessionId)}</code>`,
            `Workspace: <code>${escapeHtml(route.workspace)}</code>`,
            `Turn status: ${isBusy ? "running" : "idle"}`,
            `Delivered posts: ${this.options.store.deliveredCount(route.sessionId)}`,
          ].join("\n");
        }
        return `ℹ️ <b>Topic #${messageThreadId} is not bound to a session.</b>`;
      }
      const activeTopics = this.options.store
        .listRoutes(this.slotId)
        .filter(r => r.chatId === this.forumChatId && r.topicId !== "");
      return [
        `🤖 <b>Veyyon Forum Mode</b>`,
        `Slot: <code>${escapeHtml(this.slotId)}</code>`,
        `Forum Chat: <code>${escapeHtml(this.forumChatId)}</code>`,
        `Active topics: ${activeTopics.length}`,
        `Use <code>/topics</code> to list topics.`,
      ].join("\n");
    }

    if (verb === "/new") {
      const workspace = arg || this.options.slot.workspace;
      if (!workspace) {
        return "⚠️ <b>Usage:</b> <code>/new &lt;absolute-project-path&gt;</code> — slot declares no workspace.";
      }
      try {
        const sessionId = await this.options.control.createSession(workspace, `Telegram ${this.slotId}`);
        const threadId = await this.ensureTopic(sessionId, workspace);
        await this.options.control.loadTranscript(sessionId);
        return `🆕 <b>Session</b> <code>${escapeHtml(sessionId)}</code> <b>started in topic #${threadId}</b> (${escapeHtml(workspace)}).`;
      } catch (err) {
        return `⚠️ <b>Failed to create session:</b> ${escapeHtml(err instanceof Error ? err.message : String(err))}`;
      }
    }

    if (verb === "/attach") {
      if (!arg) return "⚠️ <b>Usage:</b> <code>/attach &lt;session-id&gt;</code>";
      try {
        const sessions = await this.options.control.listSessions();
        const target =
          sessions.find(s => s.id === arg) ?? sessions.find(s => s.id.startsWith(arg));
        if (!target) {
          return `🚫 <b>No running session matches</b> <code>${escapeHtml(arg)}</code>. Use <code>/sessions</code> to list.`;
        }

        if (messageThreadId !== undefined && messageThreadId > 1) {
          // Attach current topic to target session
          const workspace = target.cwd || target.workspace;
          this.options.store.putRoute({
            slotId: this.slotId,
            chatId: this.forumChatId,
            topicId: String(messageThreadId),
            sessionId: target.id,
            workspace,
          });
          await this.options.control.loadTranscript(target.id);
          return `🔗 <b>Attached topic #${messageThreadId} to session</b> <code>${escapeHtml(target.id)}</code> (${escapeHtml(workspace)}).`;
        }

        // Outside a thread: ensure or create topic
        const threadId = await this.ensureTopic(target.id, target.cwd || target.workspace, target.title);
        await this.options.control.loadTranscript(target.id);
        return `🔗 <b>Attached to session</b> <code>${escapeHtml(target.id)}</code> in topic #${threadId}.`;
      } catch (err) {
        return `⚠️ <b>Attach failed:</b> ${escapeHtml(err instanceof Error ? err.message : String(err))}`;
      }
    }

    if (verb === "/detach" || verb === "/close") {
      if (messageThreadId !== undefined && messageThreadId > 1) {
        await this.client.sendMessage(this.forumChatId, "🔌 <b>Topic detached and closing...</b>", {
          message_thread_id: messageThreadId,
          parse_mode: "HTML",
        }).catch(() => {});
        await this.closeTopic(messageThreadId);
        return `🔌 <b>Topic #${messageThreadId} detached and closed.</b>`;
      }
      return "⚠️ <b>Use /detach inside a specific topic to close it.</b>";
    }

    return `<b>Unknown command.</b> Use <code>/help</code> for supported forum commands.`;
  }

  /**
   * Generates the formatted `/topics` text listing all active forum topics.
   */
  public listTopicsText(currentMessageThreadId?: number): string {
    const routes = this.options.store
      .listRoutes(this.slotId)
      .filter(r => r.chatId === this.forumChatId && r.topicId !== "");

    if (routes.length === 0) {
      return [
        "ℹ️ <b>No forum topics are currently active.</b>",
        "Use <code>/new</code> to create a session and open a topic.",
      ].join("\n");
    }

    const lines = routes.map(r => {
      const threadId = Number(r.topicId);
      const isCurrent = currentMessageThreadId !== undefined && currentMessageThreadId === threadId;
      const marker = isCurrent ? "➡️" : "•";
      const isBusy = this.options.control.isBusy(r.sessionId);
      return `${marker} <b>#${r.topicId}</b>: <code>${escapeHtml(r.sessionId)}</code> — <i>${escapeHtml(r.workspace)}</i> (${isBusy ? "running" : "idle"})`;
    });

    return [
      `📋 <b>Active Forum Topics (${routes.length})</b>`,
      ...lines,
      "",
      "Switch to a topic above or create a new one with <code>/new</code>.",
    ].join("\n");
  }

  /**
   * Relays outbound session events to the session's forum topic.
   */
  public async onSessionEvent(event: SessionEvent): Promise<void> {
    if (event.kind === "streaming") return;

    const routes = this.options.store
      .routesForSession(event.sessionId)
      .filter(r => r.slotId === this.slotId && r.chatId === this.forumChatId && r.topicId !== "");

    for (const route of routes) {
      const threadId = Number(route.topicId);
      if (!Number.isFinite(threadId)) continue;

      for (const entry of event.entries) {
        // Idempotent delivery check
        const claimed = this.options.store.claimDelivery(
          event.sessionId,
          entry.entryId,
          `${this.forumChatId}:${route.topicId}`,
        );
        if (!claimed) continue;
        if (event.kind === "history") continue;

        try {
          for (const chunk of chunkMessage(entry.text)) {
            await this.client.sendMessage(this.forumChatId, chunk, {
              message_thread_id: threadId,
              parse_mode: "HTML",
            });
          }
        } catch (error) {
          this.log(
            `Failed to relay entry ${entry.entryId} to topic #${threadId}: ${
              error instanceof Error ? error.message : String(error)
            }`,
          );
        }
      }
    }
  }
}
