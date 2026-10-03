import { escapeHtml } from "./sanitizer";

/** The menu and help are projections of the same supported command surface. */
export const DAEMON_ROUTING_COMMANDS = [
  { command: "sessions", description: "List running Veyyon sessions", group: "Routing & Workspaces", syntax: "/sessions", harness: false },
  { command: "attach", description: "Attach this chat to a running session", group: "Routing & Workspaces", syntax: "/attach <id>", harness: false },
  { command: "new", description: "Start a new session in a workspace", group: "Routing & Workspaces", syntax: "/new <path>", harness: false },
  { command: "where", description: "Show which session this chat is bound to", group: "Routing & Workspaces", syntax: "/where", harness: false },
  { command: "detach", description: "Detach this chat from the current session", group: "Routing & Workspaces", syntax: "/detach", harness: false },
  { command: "topics", description: "List session topics (forum mode)", group: "Routing & Workspaces", syntax: "/topics", harness: false },
  { command: "app", description: "Open the Superboard Mini App", group: "Routing & Workspaces", syntax: "/app", harness: false },
] as const;

/** The menu and help are projections of the same supported command surface. */
export const TELEGRAM_COMMANDS = [
  { command: "status", description: "Session state and available backend information", group: "Inspect", syntax: "/status", harness: false },
  { command: "agents", description: "Current session and reachable Herdr targets", group: "Inspect", syntax: "/agents", harness: true },
  { command: "usage", description: "Provider allowances and reset windows", group: "Inspect", syntax: "/usage", harness: true },
  { command: "shot", description: "Latest recorded PNG for the current session, if any", group: "Inspect", syntax: "/shot veyyon:SESSION_ID", harness: true },
  { command: "prompt", description: "Send text to a target copied from /agents", group: "Direct work", syntax: "/prompt backend:id text", harness: true },
  { command: "steer", description: "Send an instruction at the next safe tool boundary", group: "Direct work", syntax: "/steer text", harness: false },
  { command: "cancel", description: "Abort the active turn (does not undo completed work)", group: "Control", syntax: "/cancel", harness: false },
  { command: "release", description: "Disconnect this bot; reconnect from the terminal", group: "Control", syntax: "/release", harness: false },
  { command: "reload", description: "Hot reload Telegram harness runtime in-process", group: "Control", syntax: "/reload", harness: false },
  { command: "help", description: "Command syntax and how plain text is delivered", group: "Control", syntax: "/help", harness: false },
] as const;

export function availableCommands(hasHarness = true, isDaemon = false) {
  const base = TELEGRAM_COMMANDS.filter(command => hasHarness || !command.harness);
  if (isDaemon) {
    return [...DAEMON_ROUTING_COMMANDS, ...base];
  }
  return base;
}

export interface TelegramHelpOptions {
  hasHarness?: boolean;
  isDaemon?: boolean;
}

export function renderTelegramHelp(optionsOrHasHarness: boolean | TelegramHelpOptions = true): string {
  const opts: TelegramHelpOptions = typeof optionsOrHasHarness === "boolean"
    ? { hasHarness: optionsOrHasHarness, isDaemon: false }
    : { hasHarness: true, isDaemon: false, ...optionsOrHasHarness };
  const hasHarness = opts.hasHarness ?? true;
  const isDaemon = opts.isDaemon ?? false;
  const commands = availableCommands(hasHarness, isDaemon);
  const lines = ["<b>Veyyon Telegram control</b>"];
  const groups = isDaemon
    ? ["Routing & Workspaces", "Inspect", "Direct work", "Control"]
    : ["Inspect", "Direct work", "Control"];
  for (const group of groups) {
    const groupCommands = commands.filter(command => command.group === group);
    if (groupCommands.length === 0) continue;
    lines.push("", `<b>${group}</b>`);
    for (const command of groupCommands) {
      lines.push(`• <code>${escapeHtml(command.syntax)}</code> — ${escapeHtml(command.description)}`);
    }
  }
  if (hasHarness) {
    lines.push("", "Copy <code>backend:id</code> from <code>/agents</code>: <code>veyyon:SESSION_ID</code> or a reachable <code>herdr:ID</code>. Worker targets are not exposed by this host. <code>/shot</code> returns an existing PNG; it does not capture a live screen.");
  }
  lines.push("", "<blockquote expandable><b>Plain text and steering</b>\nWhen idle, plain text starts a new turn in this session. While busy, plain text (or <code>/steer text</code>) enters the steering queue at the next safe tool boundary, not halfway through a tool. When idle, <code>/steer text</code> starts a turn too.\n<code>/cancel</code> requests cancellation; already completed effects remain. <code>/release</code> disconnects Telegram without stopping the session. Reconnection requires the terminal; this chat cannot reconnect itself.</blockquote>");
  return lines.join("\n");
}

export interface TelegramCommandItem {
  command: string;
  description: string;
}

/**
 * Registers the command menu for every allowlisted operator's private chat, and for
 * a forum slot's own supergroup when it has one. No other group is ever registered:
 * DM-only actor gates have no group equivalent, so a bot sitting in an arbitrary
 * group must not advertise controls that chat cannot use.
 */
export async function registerTelegramCommands(
  botToken: string,
  allowedUsers: readonly string[],
  hasHarness: boolean | readonly TelegramCommandItem[] = true,
  signal?: AbortSignal,
  customCommands?: readonly TelegramCommandItem[],
  forumChatId?: string,
): Promise<void> {
  const commands: readonly TelegramCommandItem[] = Array.isArray(hasHarness)
    ? hasHarness
    : (customCommands ?? availableCommands(hasHarness).map(({ command, description }) => ({ command, description })));
  const chats = [...new Set(allowedUsers)].filter(chatId => /^\d+$/.test(chatId));
  if (forumChatId) chats.push(forumChatId);
  for (const chatId of chats) {
    try {
      const response = await fetch(`https://api.telegram.org/bot${botToken}/setMyCommands`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ commands, scope: { type: "chat", chat_id: chatId }, language_code: "" }),
        signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(5000)]) : AbortSignal.timeout(5000),
      });
      const result = await response.json() as { ok?: boolean };
      if (!response.ok || result.ok !== true) throw new Error("Registration refused");
    } catch {
      // Fetch errors can include the token-bearing URL; never propagate that error.
      throw new Error("Telegram command registration failed; the private-chat menu is not verified.");
    }
  }
  if (forumChatId) {
    try {
      await fetch(`https://api.telegram.org/bot${botToken}/setMyCommands`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ commands, scope: { type: "all_group_chats" }, language_code: "" }),
        signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(5000)]) : AbortSignal.timeout(5000),
      });
    } catch {
      // Best-effort scope fallback; chat-specific scope is primary
    }
  }
}
