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
import type { GuiHostSessionControl } from "./session-control";
import type { TopicLifecycle } from "./router";
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
  public async ensureTopic(sessionId: string, workspace: string, title?: string | null): Promise<number> {
    const existing = this.options.store
      .routesForSession(sessionId)
      .find(route => route.slotId === this.slotId && route.chatId === this.forumChatId && route.topicId !== "");
    if (existing) {
      return Number(existing.topicId);
    }

    const created = await this.client.createForumTopic(this.forumChatId, formatTopicName(sessionId, workspace, title));
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
      `<i>Write in this topic to steer the session. <code>/detach</code> closes it.</i>`,
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
}
