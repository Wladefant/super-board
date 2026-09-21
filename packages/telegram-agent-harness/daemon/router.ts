/**
 * router.ts — Per-slot message routing between one Telegram chat and one Veyyon session.
 *
 * The in-session extension has the session it lives in; the daemon does not, so
 * routing is explicit: every (slot, chat) pair is bound to a session id in the
 * durable ledger, and that binding is what inbound text is delivered to and what
 * outbound transcript text is attributed from.
 */

import { escapeHtml } from "../extension/sanitizer";
import { availableCommands } from "../extension/command-registry";
import type { DaemonSlot } from "./config";
import type {
  DeliveryMode,
  TerminalSessionControl,
  SessionEvent,
} from "./session-control";
import { SessionControlUnavailableError } from "./session-control";
import type { DaemonStore } from "./store";

/**
 * Where a message came from, and where its answer goes: a chat, plus the forum topic
 * inside it when the slot runs in forum mode. `topicId` is "" for a direct chat and
 * for a forum's General topic — General is the chat itself, not a topic a session can
 * be bound to.
 */
export interface RouteTarget {
  chatId: string;
  topicId: string;
}

/**
 * Forum topic lifecycle, supplied only when the slot runs in forum mode. Routing is
 * identical in both modes; the difference is that a forum binding needs a topic to
 * exist before a session can be bound to it.
 */
export interface TopicLifecycle {
  ensureTopic(
    sessionId: string,
    workspace: string,
    title?: string | null,
    ordinal?: number,
    liveSessionIds?: Set<string>,
  ): Promise<number>;
  closeTopic(messageThreadId: number): Promise<boolean>;
  listTopicsText(currentTopicId: string): string;
  markDetached?(sessionId: string): void;
  isDetached?(sessionId: string): boolean;
  clearDetached?(sessionId: string): void;
}

export interface SlotRouterOptions {
  slot: DaemonSlot;
  store: DaemonStore;
  control: TerminalSessionControl;
  /** Router UI text, already valid Telegram HTML. */
  send: (target: RouteTarget, html: string) => Promise<void>;
  /** Agent prose, still markdown; the transport renders and chunks it. */
  relay: (target: RouteTarget, markdown: string, sessionId: string) => Promise<void>;
  log: (message: string) => void;
  /** Present only in forum mode. */
  topics?: TopicLifecycle;
}

export interface DaemonCommandDescriptor {
  command: string;
  description: string;
}

/**
 * Chat-level routing commands. The daemon serves several sessions from one chat.
 * This is the canonical definition for the daemon's routing command surface.
 */
export const ROUTER_COMMANDS: readonly DaemonCommandDescriptor[] = [
  { command: "sessions", description: "List running Veyyon sessions" },
  { command: "attach", description: "Attach this chat to a running session" },
  { command: "new", description: "Start a new session in a workspace" },
  { command: "detach", description: "Detach this chat from the current session" },
  { command: "where", description: "Show which session this chat is bound to" },
  { command: "topics", description: "List active forum topics (forum mode)" },
] as const;

export const ROUTING_COMMANDS = ROUTER_COMMANDS.map(c => `/${c.command}`) as readonly string[];

/**
 * Full command set supported by the standalone daemon: routing commands first,
 * followed by the base harness commands. Read by the daemon runtime for Telegram
 * command registration without hardcoding a second list.
 */
export function getDaemonCommands(): DaemonCommandDescriptor[] {
  const routerCommands = ROUTER_COMMANDS.map(({ command, description }) => ({ command, description }));
  const baseCommands = availableCommands(true).map(({ command, description }) => ({ command, description }));
  const seen = new Set<string>();
  const result: DaemonCommandDescriptor[] = [{ command: "app", description: "Open the Superboard Mini App" }];
  for (const item of [...routerCommands, ...baseCommands]) {
    if (!seen.has(item.command)) {
      seen.add(item.command);
      result.push(item);
    }
  }
  return result;
}

export class SlotRouter {
  private readonly options: SlotRouterOptions;

  constructor(options: SlotRouterOptions) {
    this.options = options;
  }


  private get slotId(): string {
    return this.options.slot.slotId;
  }

  /** Session bound to `target`, or null when it has never been routed. */
  public boundSession(target: RouteTarget): string | null {
    return this.options.store.getRoute(this.slotId, target.chatId, target.topicId)?.sessionId ?? null;
  }

  /**
   * Delivery-claim key for a route. A topic-less route keeps the bare chat id it was
   * claimed under before forum mode existed, so an upgrade does not replay a whole
   * transcript into a chat that already received it.
   */
  private static claimKey(route: { chatId: string; topicId: string }): string {
    return route.topicId ? `${route.chatId}:${route.topicId}` : route.chatId;
  }

  /**
   * Binds a chat (or forum topic) to a session, attaching to the session's live
   * output stream and marking its existing transcript as already delivered — a fresh
   * binding must not replay a conversation that happened before it was listening.
   */
  public async bind(target: RouteTarget, sessionId: string, workspace: string): Promise<void> {
    this.options.store.putRoute({
      slotId: this.slotId,
      chatId: target.chatId,
      topicId: target.topicId,
      sessionId,
      workspace,
    });
    this.options.topics?.clearDetached?.(sessionId);
    await this.options.control.loadTranscript(sessionId).catch(() => undefined);
  }

  /**
   * Delivers operator text to the target's session, creating and binding one when
   * the target is unrouted. Returns the text to acknowledge with, or null when the
   * caller should stay silent because the session itself will answer.
   */
  public async deliver(target: RouteTarget, text: string, mode: DeliveryMode = "auto"): Promise<string | null> {
    {
      const bound = this.boundSession(target);
      if (bound) {
        const outcome = await this.options.control.deliver(bound, text, mode);
        return outcome === "started" ? null : `↪️ <b>Queued as a ${outcome === "steered" ? "steer" : "follow-up"}</b> for the running turn.`;
      }

      // A forum's General topic is the group's lobby, not one operator's chat. Binding
      // a session there would pour every topic's traffic into one transcript.
      if (this.options.topics && !target.topicId) {
        return [
          "ℹ️ <b>General is the lobby, not a session.</b>",
          "Open one with <code>/new</code>, list what is running with <code>/sessions</code>, or write inside an existing session topic.",
        ].join("\n");
      }

      const workspace = this.options.slot.workspace;
      if (!workspace) {
        return [
          "🚫 <b>No workspace is configured for this bot.</b>",
          `Slot <code>${escapeHtml(this.slotId)}</code> declares no project directory that exists on this machine, so a session cannot be created for it.`,
          "Use <code>/sessions</code> and <code>/attach &lt;id&gt;</code> to route this chat to a session that is already running.",
        ].join("\n");
      }

      const sessionId = await this.options.control.ensureSession(workspace, `Telegram ${this.slotId}`);
      await this.bind(target, sessionId, workspace);
      await this.options.control.deliver(sessionId, text, mode);
      return `🔗 <b>Routed to session</b> <code>${escapeHtml(sessionId)}</code> in <code>${escapeHtml(workspace)}</code>.`;
    }
  }

  public async abort(target: RouteTarget): Promise<boolean> {
    const bound = this.boundSession(target);
    if (!bound) return false;
    return this.options.control.abort(bound);
  }

  public isBusy(target: RouteTarget): boolean {
    const bound = this.boundSession(target);
    return bound !== null && this.options.control.isBusy(bound);
  }

  public statusText(target: RouteTarget): string {
    const route = this.options.store.getRoute(this.slotId, target.chatId, target.topicId);
    const lines = [
      "🤖 <b>Veyyon Telegram daemon</b>",
      `Slot: <code>${escapeHtml(this.slotId)}</code>`,
      `Host: <code>${escapeHtml(this.options.control.endpoint ?? "not discovered")}</code>`,
    ];
    if (target.topicId) lines.push(`Topic: <b>#${escapeHtml(target.topicId)}</b>`);
    if (route) {
      lines.push(
        `Session: <code>${escapeHtml(route.sessionId)}</code>`,
        `Workspace: <code>${escapeHtml(route.workspace)}</code>`,
        `Turn: ${this.options.control.isBusy(route.sessionId) ? "running" : "idle"}`,
        `Delivered posts: ${this.options.store.deliveredCount(route.sessionId)}`,
      );
    } else if (this.options.topics && !target.topicId) {
      lines.push("Session: <i>General is the lobby — open one with /new, or write in a session topic.</i>");
    } else {
      lines.push("Session: <i>not routed yet — send a message to start one.</i>");
    }
    return lines.join("\n");
  }

  /** True when `text` is a routing command this router owns. */
  public static isRoutingCommand(text: string): boolean {
    const verb = text.trim().split(/\s+/, 1)[0]?.toLowerCase().replace(/@\w+$/, "") ?? "";
    return (ROUTING_COMMANDS as readonly string[]).includes(verb);
  }

  /**
   * Handles a routing command. Returns false when `text` is not one, so the caller
   * falls through to the shared installed-command and message paths.
   */
  public async handleCommand(text: string, target: RouteTarget): Promise<boolean> {
    if (!SlotRouter.isRoutingCommand(text)) return false;
    const [verb, ...rest] = text.trim().split(/\s+/);
    const argument = rest.join(" ").trim();
    try {
      const reply = await this.runCommand(verb.toLowerCase().replace(/@\w+$/, ""), argument, target);
      await this.options.send(target, reply);
    } catch (error) {
      const detail = error instanceof SessionControlUnavailableError
        ? `The Veyyon host is not reachable: ${error.message}`
        : error instanceof Error
          ? error.message
          : "Command failed.";
      await this.options.send(target, `⚠️ <b>${escapeHtml(detail)}</b>`);
    }
    return true;
  }

  /**
   * Binds a session to the target, opening a forum topic for it first when the
   * command was issued outside one. Returns the topic the session now lives in, or
   * null in direct-chat mode.
   */
  private async bindWithTopic(
    target: RouteTarget,
    sessionId: string,
    workspace: string,
    title?: string | null,
  ): Promise<number | null> {
    const topics = this.options.topics;
    if (!topics || target.topicId) {
      await this.bind(target, sessionId, workspace);
      return target.topicId ? Number(target.topicId) : null;
    }
    // ensureTopic reuses the topic this session already owns, so attaching twice from
    // General never opens a duplicate topic for one session.
    const threadId = await topics.ensureTopic(sessionId, workspace, title);
    await this.bind({ chatId: target.chatId, topicId: String(threadId) }, sessionId, workspace);
    return threadId;
  }

  private workspace(session: { cwd: string; workspace: string }): string {
    const value = session.cwd || (/^(?:[A-Za-z]:[\\/]|\/)/.test(session.workspace) ? session.workspace : "");
    return value.replace(/\\/g, "/").replace(/\/+$/, "");
  }

  private folder(workspace: string): string {
    return workspace.split("/").at(-1) ?? workspace;
  }

  private isTopLevelSession(session: DaemonSessionSummary): boolean {
    return isTopLevelSession(session);
  }

  private async listAllSessions(): Promise<DaemonSessionSummary[]> {
    const wireSessions = await this.options.control.listSessions();
    const control = this.options.control;
    if ("discoverDiskSessions" in control && typeof control.discoverDiskSessions === "function") {
      const diskSessions = control.discoverDiskSessions();
      if (!diskSessions.length) return wireSessions;
      const map = new Map<string, DaemonSessionSummary>();
      for (const s of diskSessions) {
        map.set(s.id, s);
      }
      for (const s of wireSessions) {
        const existing = map.get(s.id);
        map.set(s.id, { ...existing, ...s });
      }
      return Array.from(map.values());
    }
    return wireSessions;
  }

  private visibleSession(session: { cwd: string; workspace: string; status: string; modifiedAtMs: number | null }, now: number): boolean {
    return !!this.workspace(session) && !(/^(complete|completed|finished|interrupted|aborted|error)$/i.test(session.status)
      && (session.modifiedAtMs === null || now - session.modifiedAtMs > 3_600_000));
  }

  private async runCommand(verb: string, argument: string, target: RouteTarget): Promise<string> {
    if (verb === "/where") return this.statusText(target);
    if (verb === "/topics") {
      if (!this.options.topics) {
        return "ℹ️ <b>This bot is running in direct-chat mode.</b> Forum topics are enabled when the slot specifies <code>\"mode\": \"forum\"</code>.";
      }
      return this.options.topics.listTopicsText(target.topicId);
    }

    if (verb === "/detach") {
      const topics = this.options.topics;
      if (topics && !target.topicId) return "ℹ️ <b>Run /detach inside the topic you want to close.</b>";
      const shouldClose = argument.trim().toLowerCase() === "close";
      const bound = this.boundSession(target);
      if (bound && topics?.markDetached) {
        topics.markDetached(bound);
      }
      const removed = this.options.store.deleteRoute(this.slotId, target.chatId, target.topicId);
      if (topics) {
        if (shouldClose) {
          await topics.closeTopic(Number(target.topicId));
          return removed
            ? `🔌 <b>Topic #${escapeHtml(target.topicId)} detached and closed.</b> The session keeps running.`
            : `🔌 <b>Topic #${escapeHtml(target.topicId)} closed.</b> It was not bound to a session.`;
        }
        return removed
          ? `🔌 <b>Topic #${escapeHtml(target.topicId)} detached.</b> The topic remains open; use <code>/detach close</code> to close it.`
          : `ℹ️ <b>Topic #${escapeHtml(target.topicId)} was not bound to a session.</b>`;
      }
      return removed
        ? "🔌 <b>Chat detached.</b> The session keeps running; your next message starts or joins a session again."
        : "ℹ️ <b>This chat is not routed to a session.</b>";
    }

    if (verb === "/sessions") {
      const now = Date.now();
      const allSessions = await this.listAllSessions();
      const interactive = allSessions.filter(session => this.isTopLevelSession(session));
      const sessions = interactive.filter(session =>
        argument.toLowerCase() === "all" || this.visibleSession(session, now));
      const dedupedSessions = [...sessions];
      dedupedSessions.sort((a, b) => this.folder(this.workspace(a)).localeCompare(this.folder(this.workspace(b)))
        || this.workspace(a).localeCompare(this.workspace(b))
        || (b.modifiedAtMs ?? 0) - (a.modifiedAtMs ?? 0) || a.id.localeCompare(b.id));
      if (!sessions.length) {
        this.options.store.putSessionListing(this.slotId, target.chatId, target.topicId, []);
        return "<b>No recent workspace sessions.</b> Use <code>/sessions all</code> to include older and unassigned sessions.";
      }
      const counts = new Map<string, number>();
      const labeled: { session: DaemonSessionSummary; folderName: string; displayName: string }[] = [];
      for (const session of dedupedSessions) {
        const base = this.folder(this.workspace(session)) || "session";
        const count = (counts.get(base.toLowerCase()) ?? 0) + 1;
        counts.set(base.toLowerCase(), count);
        const displayName = count === 1 ? base : `${base} (${count})`;
        labeled.push({ session, folderName: base, displayName });
      }
      this.options.store.putSessionListing(this.slotId, target.chatId, target.topicId, labeled.map(item => item.session.id));
      const lines = labeled.map((item, index) => {
        const hasTopic = Boolean(
          this.options.topics &&
          this.options.store.routesForSession(item.session.id).some(r => r.slotId === this.slotId && r.topicId !== "")
        );
        const pin = hasTopic ? "📌 " : "";
        return `${index + 1}. ${pin}<b>${escapeHtml(item.displayName)}</b> <code>${escapeHtml(this.workspace(item.session))}</code>`;
      });
      lines.push("/attach <n> or /attach <folder>");
      return lines.join("\n");
    }

    if (verb === "/attach") {
      if (!argument) return "<b>Usage:</b> <code>/attach &lt;index, folder or session-id&gt;</code>";
      const now = Date.now();
      const allSessions = await this.listAllSessions();
      const interactive = allSessions.filter(session => this.isTopLevelSession(session));
      const visible = interactive.filter(session => this.visibleSession(session, now));
      const targetPool = visible.length > 0 ? visible : interactive;
      const dedupedTargetPool = [...targetPool];
      dedupedTargetPool.sort((a, b) => this.folder(this.workspace(a)).localeCompare(this.folder(this.workspace(b)))
        || this.workspace(a).localeCompare(this.workspace(b))
        || (b.modifiedAtMs ?? 0) - (a.modifiedAtMs ?? 0) || a.id.localeCompare(b.id));
      const counts = new Map<string, number>();
      const labeled: { session: DaemonSessionSummary; folderName: string; displayName: string }[] = [];
      for (const session of dedupedTargetPool) {
        const base = this.folder(this.workspace(session)) || "session";
        const count = (counts.get(base.toLowerCase()) ?? 0) + 1;
        counts.set(base.toLowerCase(), count);
        const displayName = count === 1 ? base : `${base} (${count})`;
        labeled.push({ session, folderName: base, displayName });
      }

      let matches: DaemonSessionSummary[] = [];
      if (/^\d+$/.test(argument)) {
        const index = Number(argument) - 1;
        const savedIds = this.options.store.getSessionListing(this.slotId, target.chatId, target.topicId);
        const idFromStore = savedIds[index];
        if (idFromStore) {
          const found = allSessions.find(s => s.id === idFromStore);
          if (found) matches = [found];
        }
      } else {
        const argNorm = argument.trim().toLowerCase();
        const byExactDisplayName = labeled.filter(item => item.displayName.toLowerCase() === argNorm);
        const byFolderName = labeled.filter(item => item.folderName.toLowerCase() === argNorm);
        if (byFolderName.length === 1 && byExactDisplayName.length <= 1) {
          matches = [byFolderName[0].session];
        } else if (byFolderName.length > 1) {
          if (byExactDisplayName.length === 1 && byExactDisplayName[0].displayName.toLowerCase() !== byExactDisplayName[0].folderName.toLowerCase()) {
            matches = [byExactDisplayName[0].session];
          } else {
            matches = byFolderName.map(item => item.session);
          }
        } else if (byExactDisplayName.length === 1) {
          matches = [byExactDisplayName[0].session];
        } else {
          const exactId = allSessions.find(s => s.id.toLowerCase() === argNorm);
          if (exactId) {
            matches = [exactId];
          } else {
            const prefixMatches = allSessions.filter(s => s.id.toLowerCase().startsWith(argNorm));
            if (prefixMatches.length > 0) {
              matches = prefixMatches;
            } else {
              const normPath = argument.replace(/\\/g, "/").toLowerCase();
              const byPath = allSessions.filter(s => this.workspace(s).replace(/\\/g, "/").toLowerCase() === normPath);
              matches = byPath;
            }
          }
        }
      }

      if (matches.length > 1) return "<b>More than one session matches.</b> Use <code>/sessions</code> and attach with its number.";
      const session = matches[0];
      if (!session) return `🚫 <b>No running session matches</b> <code>${escapeHtml(argument)}</code>. Use <code>/sessions</code> in this chat/topic to refresh the listing.`;
      const workspace = this.workspace(session);
      const folderName = this.folder(workspace);
      const threadId = await this.bindWithTopic(target, session.id, workspace, folderName);
      return threadId === null
        ? `🔗 <b>Attached to</b> <code>${escapeHtml(session.id)}</code> — ${escapeHtml(workspace)}.`
        : `🔗 <b>Attached to</b> <code>${escapeHtml(session.id)}</code> in topic #${threadId} — ${escapeHtml(workspace)}.`;
    }

    // `/new`
    const workspace = argument || this.options.slot.workspace;
    if (!workspace) {
      return "⚠️ <b>Usage:</b> <code>/new &lt;absolute-project-path&gt;</code> — this bot's slot declares no workspace.";
    }
    const created = await this.options.control.createSession(workspace, `Telegram ${this.slotId}`);
    const threadId = await this.bindWithTopic(target, created, workspace);
    return threadId === null
      ? `🆕 <b>Session</b> <code>${escapeHtml(created)}</code> <b>started in</b> <code>${escapeHtml(workspace)}</code>.`
      : `🆕 <b>Session</b> <code>${escapeHtml(created)}</code> <b>started in topic #${threadId}</b> — <code>${escapeHtml(workspace)}</code>.`;
  }

  /**
   * Forwards one session event to every chat and topic bound to it. `history` claims
   * entries without sending: it is the baseline that keeps a restart or a fresh
   * binding from replaying the whole transcript into the operator's chat.
   */
  public async onSessionEvent(event: SessionEvent): Promise<void> {
    if (event.kind === "streaming") return;
    const routes = this.options.store.routesForSession(event.sessionId).filter(route => route.slotId === this.slotId);
    for (const route of routes) {
      for (const entry of event.entries) {
        if (!this.options.store.claimDelivery(event.sessionId, entry.entryId, SlotRouter.claimKey(route))) continue;
        if (event.kind === "history") continue;
        try {
          await this.options.relay({ chatId: route.chatId, topicId: route.topicId }, entry.text, event.sessionId);
        } catch (error) {
          this.options.log(
            `Slot ${this.slotId}: delivery of entry ${entry.entryId} to chat ${route.chatId} failed: ${error instanceof Error ? error.message : String(error)}`,
          );
        }
      }
    }
  }
}
export function isTopLevelSession(session: DaemonSessionSummary): boolean {
  if (session.isSubagent) return false;
  if (session.parentPath || session.parentId) return false;
  if (session.kind === "subagent") return false;
  const record = session as unknown as Record<string, unknown>;
  if (record.spawner) return false;
  if (record.parent_path || record.parent_id) return false;
  if (session.path) {
    const normalized = session.path.replace(/\\/g, "/");
    const filename = normalized.split("/").at(-1) ?? "";
    if (!/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.-]+Z_[a-f0-9-]+\.jsonl$/i.test(filename)) {
      return false;
    }
    const parentDir = normalized.split("/").slice(-2, -1)[0] ?? "";
    if (/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.-]+Z_[a-f0-9-]+$/i.test(parentDir)) {
      return false;
    }
  }
  if (/^(sub[-_]|agent[-_]|worker[-_])/i.test(session.id) || (session.title && /^subagent/i.test(session.title))) {
    return false;
  }
  return true;
}

export function getSessionWorkspace(session: { cwd?: string; workspace?: string }): string {
  const value = session.cwd || (/^(?:[A-Za-z]:[\\/]|\/)/.test(session.workspace ?? "") ? (session.workspace ?? "") : "");
  return value.replace(/\\/g, "/").replace(/\/+$/, "");
}
