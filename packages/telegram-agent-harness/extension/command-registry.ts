import { escapeHtml } from "./sanitizer";

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
  { command: "help", description: "Command syntax and how plain text is delivered", group: "Control", syntax: "/help", harness: false },
] as const;

export function availableCommands(hasHarness = true) {
  return TELEGRAM_COMMANDS.filter(command => hasHarness || !command.harness);
}

export function renderTelegramHelp(hasHarness = true): string {
  const commands = availableCommands(hasHarness);
  const lines = ["<b>Veyyon Telegram control</b>"];
  for (const group of ["Inspect", "Direct work", "Control"]) {
    lines.push("", `<b>${group}</b>`);
    for (const command of commands.filter(command => command.group === group)) {
      lines.push(`• <code>${escapeHtml(command.syntax)}</code> — ${escapeHtml(command.description)}`);
    }
  }
  if (hasHarness) {
    lines.push("", "Copy <code>backend:id</code> from <code>/agents</code>: <code>veyyon:SESSION_ID</code> or a reachable <code>herdr:ID</code>. Worker targets are not exposed by this host. <code>/shot</code> returns an existing PNG; it does not capture a live screen.");
  }
  lines.push("", "<blockquote expandable><b>Plain text and steering</b>\nWhen idle, plain text starts a new turn in this session. While busy, plain text (or <code>/steer text</code>) enters the steering queue at the next safe tool boundary, not halfway through a tool. When idle, <code>/steer text</code> starts a turn too.\n<code>/cancel</code> requests cancellation; already completed effects remain. <code>/release</code> disconnects Telegram without stopping the session. Reconnection requires the terminal; this chat cannot reconnect itself.</blockquote>");
  return lines.join("\n");
}

/** DM-only actor gates have no group equivalent: never advertise group controls. */
export async function registerTelegramCommands(
  botToken: string,
  allowedUsers: readonly string[],
  hasHarness = true,
  signal?: AbortSignal,
): Promise<void> {
  const commands = availableCommands(hasHarness).map(({ command, description }) => ({ command, description }));
  for (const chatId of new Set(allowedUsers)) {
    if (!/^\d+$/.test(chatId)) continue;
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
}
