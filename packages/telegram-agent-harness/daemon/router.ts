/**
 * router.ts — Per-slot message routing between one Telegram chat and one Veyyon session.
 *
 * The in-session extension has the session it lives in; the daemon does not, so
 * routing is explicit: every (slot, chat) pair is bound to a session id in the
 * durable ledger, and that binding is what inbound text is delivered to and what
 * outbound transcript text is attributed from.
 */

import { escapeHtml } from "../extension/sanitizer";
import type { DaemonSlot } from "./config";
import type {
  DeliveryMode,
  GuiHostSessionControl,
  SessionEvent,
} from "./session-control";
import { SessionControlUnavailableError } from "./session-control";
import type { DaemonStore } from "./store";

export interface SlotRouterOptions {
  slot: DaemonSlot;
  store: DaemonStore;
  control: GuiHostSessionControl;
  /** Router UI text, already valid Telegram HTML. */
  send: (chatId: string, html: string) => Promise<void>;
  /** Agent prose, still markdown; the transport renders and chunks it. */
  relay: (chatId: string, markdown: string) => Promise<void>;
  log: (message: string) => void;
}

/** Chat-level routing commands. The daemon serves several sessions from one chat. */
const ROUTING_COMMANDS = ["/sessions", "/attach", "/new", "/detach", "/where"] as const;

export class SlotRouter {
  private readonly options: SlotRouterOptions;

  constructor(options: SlotRouterOptions) {
    this.options = options;
  }

  private get slotId(): string {
    return this.options.slot.slotId;
  }

  /** Session bound to `chatId`, or null when the chat has never been routed. */
  public boundSession(chatId: string): string | null {
    return this.options.store.getRoute(this.slotId, chatId)?.sessionId ?? null;
  }

  /**
   * Binds a chat to a session, attaching to the session's live output stream and
   * marking its existing transcript as already delivered — a fresh binding must not
   * replay a conversation that happened before the chat was listening.
   */
  public async bind(chatId: string, sessionId: string, workspace: string): Promise<void> {
    this.options.store.putRoute({ slotId: this.slotId, chatId, topicId: "", sessionId, workspace });
    await this.options.control.loadTranscript(sessionId);
  }

  /**
   * Delivers operator text to the chat's session, creating and binding one when the
   * chat is unrouted. Returns the text to acknowledge with, or null when the caller
   * should stay silent because the session itself will answer.
   */
  public async deliver(chatId: string, text: string, mode: DeliveryMode = "auto"): Promise<string | null> {
    const bound = this.boundSession(chatId);
    if (bound) {
      const outcome = await this.options.control.deliver(bound, text, mode);
      return outcome === "started" ? null : `↪️ <b>Queued as a ${outcome === "steered" ? "steer" : "follow-up"}</b> for the running turn.`;
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
    await this.bind(chatId, sessionId, workspace);
    await this.options.control.deliver(sessionId, text, mode);
    return `🔗 <b>Routed to session</b> <code>${escapeHtml(sessionId)}</code> in <code>${escapeHtml(workspace)}</code>.`;
  }

  public async abort(chatId: string): Promise<boolean> {
    const bound = this.boundSession(chatId);
    if (!bound) return false;
    return this.options.control.abort(bound);
  }

  public isBusy(chatId: string): boolean {
    const bound = this.boundSession(chatId);
    return bound !== null && this.options.control.isBusy(bound);
  }

  public statusText(chatId: string): string {
    const route = this.options.store.getRoute(this.slotId, chatId);
    const lines = [
      "🤖 <b>Veyyon Telegram daemon</b>",
      `Slot: <code>${escapeHtml(this.slotId)}</code>`,
      `Host: <code>${escapeHtml(this.options.control.endpoint ?? "not discovered")}</code>`,
    ];
    if (route) {
      lines.push(
        `Session: <code>${escapeHtml(route.sessionId)}</code>`,
        `Workspace: <code>${escapeHtml(route.workspace)}</code>`,
        `Turn: ${this.options.control.isBusy(route.sessionId) ? "running" : "idle"}`,
        `Delivered posts: ${this.options.store.deliveredCount(route.sessionId)}`,
      );
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
  public async handleCommand(text: string, chatId: string): Promise<boolean> {
    if (!SlotRouter.isRoutingCommand(text)) return false;
    const [verb, ...rest] = text.trim().split(/\s+/);
    const argument = rest.join(" ").trim();
    try {
      await this.options.send(chatId, await this.runCommand(verb.toLowerCase().replace(/@\w+$/, ""), argument, chatId));
    } catch (error) {
      const detail = error instanceof SessionControlUnavailableError
        ? `The Veyyon host is not reachable: ${error.message}`
        : error instanceof Error
          ? error.message
          : "Command failed.";
      await this.options.send(chatId, `⚠️ <b>${escapeHtml(detail)}</b>`);
    }
    return true;
  }

  private async runCommand(verb: string, argument: string, chatId: string): Promise<string> {
    if (verb === "/where") return this.statusText(chatId);

    if (verb === "/detach") {
      const removed = this.options.store.deleteRoute(this.slotId, chatId);
      return removed
        ? "🔌 <b>Chat detached.</b> The session keeps running; your next message starts or joins a session again."
        : "ℹ️ <b>This chat is not routed to a session.</b>";
    }

    if (verb === "/sessions") {
      const sessions = await this.options.control.listSessions();
      if (sessions.length === 0) return "ℹ️ <b>No Veyyon sessions are running.</b>";
      const bound = this.boundSession(chatId);
      const lines = sessions.slice(0, 15).map(session => {
        const marker = session.id === bound ? "➡️" : "•";
        const label = session.title ?? (session.cwd || "untitled");
        return `${marker} <code>${escapeHtml(session.id)}</code> — ${escapeHtml(label)} <i>(${escapeHtml(session.status)})</i>`;
      });
      return ["🗂 <b>Veyyon sessions</b>", ...lines, "", "Attach with <code>/attach &lt;id&gt;</code>."].join("\n");
    }

    if (verb === "/attach") {
      if (!argument) return "⚠️ <b>Usage:</b> <code>/attach &lt;session-id&gt;</code>";
      const sessions = await this.options.control.listSessions();
      const target = sessions.find(session => session.id === argument)
        ?? sessions.find(session => session.id.startsWith(argument));
      if (!target) return `🚫 <b>No running session matches</b> <code>${escapeHtml(argument)}</code>. Use <code>/sessions</code> to list them.`;
      await this.bind(chatId, target.id, target.cwd || target.workspace);
      return `🔗 <b>Attached to</b> <code>${escapeHtml(target.id)}</code> — ${escapeHtml(target.cwd || target.workspace)}.`;
    }

    // `/new`
    const workspace = argument || this.options.slot.workspace;
    if (!workspace) {
      return "⚠️ <b>Usage:</b> <code>/new &lt;absolute-project-path&gt;</code> — this bot's slot declares no workspace.";
    }
    const created = await this.options.control.createSession(workspace, `Telegram ${this.slotId}`);
    await this.bind(chatId, created, workspace);
    return `🆕 <b>Session</b> <code>${escapeHtml(created)}</code> <b>started in</b> <code>${escapeHtml(workspace)}</code>.`;
  }

  /**
   * Forwards one session event to every chat bound to it. `history` claims entries
   * without sending: it is the baseline that keeps a restart or a fresh binding from
   * replaying the whole transcript into the operator's chat.
   */
  public async onSessionEvent(event: SessionEvent): Promise<void> {
    if (event.kind === "streaming") return;
    const routes = this.options.store.routesForSession(event.sessionId).filter(route => route.slotId === this.slotId);
    for (const route of routes) {
      for (const entry of event.entries) {
        if (!this.options.store.claimDelivery(event.sessionId, entry.entryId, route.chatId)) continue;
        if (event.kind === "history") continue;
        try {
          await this.options.relay(route.chatId, entry.text);
        } catch (error) {
          this.options.log(
            `Slot ${this.slotId}: delivery of entry ${entry.entryId} to chat ${route.chatId} failed: ${error instanceof Error ? error.message : String(error)}`,
          );
        }
      }
    }
  }
}
