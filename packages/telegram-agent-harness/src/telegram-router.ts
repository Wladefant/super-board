import * as path from "node:path";
import type {
  AgentHarnessAdapter,
  AgentSession,
  ArtifactRef,
  DecisionAnswer,
  HarnessBackend,
  UsageLimit,
} from "./contract";

export interface InlineButton {
  text: string;
  callback_data?: string;
  url?: string;
}

export interface TelegramSendOptions {
  parseMode: "HTML";
  replyMarkup?: { inline_keyboard: InlineButton[][] };
}

export interface TelegramTransport {
  sendMessage(chatId: string, text: string, options: TelegramSendOptions): Promise<void>;
  sendPhoto(chatId: string, filePath: string, caption: string, options: TelegramSendOptions): Promise<void>;
  sendDocument(chatId: string, filePath: string, caption: string, options: TelegramSendOptions): Promise<void>;
  answerCallbackQuery(callbackQueryId: string, text: string, showAlert?: boolean): Promise<void>;
  editMessage(chatId: string, messageId: number, text: string, options: TelegramSendOptions): Promise<void>;
}

export interface ReplyBinding {
  messageId: number;
  backend?: HarnessBackend;
  sessionId?: string;
  decisionId?: string;
  quotedText?: string;
}

export interface TelegramMessageInput {
  chatId: string;
  actorId: string;
  text: string;
  reply?: ReplyBinding;
}

export interface TelegramCallbackInput {
  chatId: string;
  actorId: string;
  callbackQueryId: string;
  messageId: number;
  data: string;
}

export type CallbackResolution =
  | { decision: "reject"; detail: string }
  | {
      decision: "deliver";
      kind: "decision";
      backend: HarnessBackend;
      sessionId: string;
      interactionId: string;
      answer: DecisionAnswer;
      resolvedLabel: string;
    }
  | {
      decision: "deliver";
      kind: "artifact";
      backend: HarnessBackend;
      sessionId: string;
      artifact: ArtifactRef;
    };

/** Existing correlation/decision callback storage implements this boundary. */
export interface CallbackBridge {
  resolveCallback(data: string, actorId: string, chatId: string): Promise<CallbackResolution>;
}

export interface DecisionCardChoice {
  label: string;
  callbackData: string;
}

export interface DecisionCard {
  title: string;
  summary: string;
  action: string;
  risk?: string;
  detailsUrl?: string;
  choices: DecisionCardChoice[];
}

export function escapeHtml(value: string): string {
  return value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
}

export function renderDecisionCard(card: DecisionCard): { text: string; replyMarkup: TelegramSendOptions["replyMarkup"] } {
  const action = card.risk ? `${card.action} [${card.risk}]` : card.action;
  const lines = [
    `❓ <b>${escapeHtml(card.title)}</b>`,
    escapeHtml(card.summary),
    `<b>Action:</b> <code>${escapeHtml(action)}</code>`,
    "<i>Reply with guidance, or choose one option below.</i>",
  ];
  if (card.detailsUrl) lines.push(`<a href="${escapeHtml(card.detailsUrl)}">Details</a>`);
  return {
    text: lines.join("\n"),
    replyMarkup: { inline_keyboard: [card.choices.map(choice => ({ text: choice.label, callback_data: choice.callbackData }))] },
  };
}

const STATE_VIEW: Record<AgentSession["state"], { emoji: string; label: string }> = {
  working: { emoji: "🟢", label: "working" },
  blocked: { emoji: "🟡", label: "waiting" },
  idle: { emoji: "⚪", label: "idle" },
  unknown: { emoji: "🔴", label: "unknown" },
  stale: { emoji: "🔴", label: "stale" },
  error: { emoji: "🔴", label: "error" },
};

function compactProject(project?: string): string {
  if (!project) return "";
  const normalized = project.replaceAll("\\", "/").replace(/\/$/, "");
  return path.posix.basename(normalized);
}

function formatAge(timestamp: number | undefined, now: number): string {
  if (!timestamp) return "now";
  const seconds = Math.max(0, Math.round((now - timestamp) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.round(minutes / 60)}h`;
}

export function renderAgentList(sessions: AgentSession[], now = Date.now()): string {
  const lines = ["🤖 <b>Agents</b>", `<i>${sessions.length} observed · refreshed just now</i>`, ""];
  if (sessions.length === 0) lines.push("No live agents were reported.");
  for (const session of sessions) {
    const view = STATE_VIEW[session.state];
    const project = compactProject(session.project);
    const detail = project ? ` · ${escapeHtml(project)}` : "";
    const age = formatAge(session.updatedAt ?? session.observedAt, now);
    lines.push(`${view.emoji} <code>${escapeHtml(`${session.backend}:${session.id}`)}</code> · <b>${escapeHtml(session.name)}</b>`);
    lines.push(`   ${view.label}${detail} · ${age}`);
  }
  return lines.join("\n");
}

function formatReset(limit: UsageLimit, now: number): string {
  if (!limit.resetsAt) return "reset unavailable";
  const remaining = Math.max(0, limit.resetsAt - now);
  const hours = Math.floor(remaining / 3_600_000);
  const minutes = Math.floor((remaining % 3_600_000) / 60_000);
  if (hours >= 24) return `resets in ${Math.floor(hours / 24)}d ${hours % 24}h`;
  if (hours > 0) return `resets in ${hours}h ${minutes}m`;
  return `resets in ${minutes}m`;
}

export function renderUsage(limits: UsageLimit[], observedAt: number, now = Date.now()): string {
  const lines = ["📊 <b>Current usage</b>", `<i>Observed ${formatAge(observedAt, now)} ago</i>`, ""];
  for (const limit of limits) {
    lines.push(`• <b>${escapeHtml(limit.provider)}</b> · ${escapeHtml(limit.label)}: ${Math.round(limit.remainingPercent)}% remaining (${formatReset(limit, now)})`);
  }
  if (limits.length === 0) lines.push("Usage is currently unavailable.");
  return lines.join("\n");
}

export class TelegramHarnessRouter {
  private readonly adapters = new Map<HarnessBackend, AgentHarnessAdapter>();
  private readonly selectedByChat = new Map<string, { backend: HarnessBackend; sessionId: string }>();

  constructor(
    adapters: AgentHarnessAdapter[],
    private readonly transport: TelegramTransport,
    private readonly callbackBridge?: CallbackBridge,
  ) {
    for (const adapter of adapters) this.adapters.set(adapter.backend, adapter);
  }

  async handleMessage(input: TelegramMessageInput): Promise<void> {
    const raw = input.text.trim();
    if (raw === "/agents") {
      const sessions = (await Promise.all([...this.adapters.values()].map(adapter => adapter.listSessions()))).flat();
      const selectable = sessions.filter(session => session.state !== "stale" && session.state !== "error");
      const keyboard = selectable.length > 0
        ? { inline_keyboard: selectable.map(session => [{ text: `${STATE_VIEW[session.state].emoji} ${session.name}`, callback_data: `sel:${session.backend}:${session.id}` }]) }
        : undefined;
      await this.transport.sendMessage(input.chatId, renderAgentList(sessions), { parseMode: "HTML", replyMarkup: keyboard });
      return;
    }

    if (raw === "/usage") {
      const usage = await Promise.all([...this.adapters.values()].map(adapter => adapter.usage()));
      const available = usage.filter(result => result.available);
      const observedAt = available.length > 0 ? Math.max(...available.map(result => result.observedAt)) : Date.now();
      await this.transport.sendMessage(input.chatId, renderUsage(available.flatMap(result => result.limits), observedAt), { parseMode: "HTML" });
      return;
    }

    if (raw.startsWith("/prompt")) {
      const match = /^\/prompt\s+(\S+)\s+([\s\S]+)$/.exec(raw);
      if (!match) {
        await this.transport.sendMessage(input.chatId, "⚠️ <b>Usage:</b> <code>/prompt backend:id instruction</code>", { parseMode: "HTML" });
        return;
      }
      const target = this.resolveTarget(match[1]);
      if (!target) {
        await this.transport.sendMessage(input.chatId, "🔴 <b>Target not found.</b> Use <code>/agents</code> and copy the full backend:id.", { parseMode: "HTML" });
        return;
      }
      await this.deliverPrompt(input.chatId, target.backend, target.sessionId, match[2]);
      return;
    }

    if (raw.startsWith("/shot")) {
      const match = /^\/shot\s+(\S+)$/.exec(raw);
      const target = match ? this.resolveTarget(match[1]) : undefined;
      if (!target) {
        await this.transport.sendMessage(input.chatId, "⚠️ <b>Usage:</b> <code>/shot backend:id</code>", { parseMode: "HTML" });
        return;
      }
      const result = await this.adapters.get(target.backend)?.artifacts(target.sessionId);
      const image = result?.artifacts.filter(artifact => artifact.kind === "image").sort((a, b) => b.createdAt - a.createdAt)[0];
      if (!result?.available || !image) {
        await this.transport.sendMessage(input.chatId, `🔴 <b>No screenshot available.</b> ${escapeHtml(result?.detail ?? "The adapter is unavailable.")}`, { parseMode: "HTML" });
        return;
      }
      const size = image.width && image.height ? ` · ${image.width}x${image.height}` : "";
      const replyMarkup = image.originalCallbackData
        ? { inline_keyboard: [[{ text: "📄 Original document", callback_data: image.originalCallbackData }]] }
        : undefined;
      await this.transport.sendPhoto(input.chatId, image.path, `${escapeHtml(image.label)}${size}`, { parseMode: "HTML", replyMarkup });
      return;
    }

    if (input.reply?.decisionId && input.reply.backend && input.reply.sessionId) {
      const adapter = this.adapters.get(input.reply.backend);
      const result = adapter
        ? await adapter.answer(input.reply.sessionId, input.reply.decisionId, { kind: "text", text: raw })
        : { ok: false, detail: "The original decision backend is unavailable." };
      await this.transport.sendMessage(input.chatId, `${result.ok ? "💬" : "🔴"} <b>${result.ok ? "Guidance sent" : "Guidance not sent"}.</b> ${escapeHtml(result.detail)}`, { parseMode: "HTML" });
      return;
    }

    if (input.reply) {
      if (!input.reply.backend || !input.reply.sessionId) {
        await this.transport.sendMessage(input.chatId, "🟡 <b>Reply needs a target.</b> Use <code>/agents</code>, then <code>/prompt backend:id …</code>.", { parseMode: "HTML" });
        return;
      }
      const provenance = [`Replying to Telegram post #${input.reply.messageId}`];
      if (input.reply.quotedText) provenance.push(`Original post: "${input.reply.quotedText.slice(0, 300)}"`);
      provenance.push("Free-text reply; not automatic approval or authorization.");
      await this.deliverPrompt(input.chatId, input.reply.backend, input.reply.sessionId, `[${provenance.join("]\n[")}]\n\n${raw}`);
      return;
    }

    const selected = this.selectedByChat.get(input.chatId);
    if (selected) {
      await this.deliverPrompt(input.chatId, selected.backend, selected.sessionId, raw);
      return;
    }
    await this.transport.sendMessage(input.chatId, "🟡 <b>Choose an agent first.</b> Send <code>/agents</code>, then tap one or use <code>/prompt backend:id …</code>.", { parseMode: "HTML" });
  }

  async handleCallback(input: TelegramCallbackInput): Promise<void> {
    if (input.data.startsWith("sel:")) {
      const match = /^sel:(herdr|veyyon):(.+)$/.exec(input.data);
      if (!match || !this.adapters.has(match[1] as HarnessBackend)) {
        await this.transport.answerCallbackQuery(input.callbackQueryId, "That agent is unavailable.", true);
        return;
      }
      const backend = match[1] as HarnessBackend;
      const state = await this.adapters.get(backend)?.getState(match[2]);
      if (!state || state.state === "stale" || state.state === "error") {
        await this.transport.answerCallbackQuery(input.callbackQueryId, "That agent is no longer available.", true);
        return;
      }
      this.selectedByChat.set(input.chatId, { backend, sessionId: match[2] });
      await this.transport.answerCallbackQuery(input.callbackQueryId, `Selected ${state.name}.`);
      return;
    }

    if (!this.callbackBridge) {
      await this.transport.answerCallbackQuery(input.callbackQueryId, "This action is unavailable.", true);
      return;
    }
    const resolution = await this.callbackBridge.resolveCallback(input.data, input.actorId, input.chatId);
    if (resolution.decision === "reject") {
      await this.transport.answerCallbackQuery(input.callbackQueryId, resolution.detail, true);
      return;
    }
    await this.transport.answerCallbackQuery(input.callbackQueryId, "Working…");
    if (resolution.kind === "artifact") {
      await this.transport.sendDocument(input.chatId, resolution.artifact.originalPath ?? resolution.artifact.path, escapeHtml(resolution.artifact.label), { parseMode: "HTML" });
      return;
    }
    const adapter = this.adapters.get(resolution.backend);
    const result = adapter
      ? await adapter.answer(resolution.sessionId, resolution.interactionId, resolution.answer)
      : { ok: false, detail: "Decision backend is unavailable." };
    const title = result.ok ? resolution.resolvedLabel : "Decision not changed";
    await this.transport.editMessage(input.chatId, input.messageId, `${result.ok ? "✅" : "🔴"} <b>${escapeHtml(title)}</b>\n${escapeHtml(result.detail)}`, { parseMode: "HTML" });
  }

  private resolveTarget(value: string): { backend: HarnessBackend; sessionId: string } | undefined {
    const match = /^(herdr|veyyon):(.+)$/.exec(value);
    if (!match) return undefined;
    const backend = match[1] as HarnessBackend;
    return this.adapters.has(backend) ? { backend, sessionId: match[2] } : undefined;
  }

  private async deliverPrompt(chatId: string, backend: HarnessBackend, sessionId: string, prompt: string): Promise<void> {
    const adapter = this.adapters.get(backend);
    const result = adapter
      ? await adapter.prompt(sessionId, prompt, "auto")
      : { ok: false, disposition: "rejected" as const, detail: "Adapter unavailable." };
    const heading = result.disposition === "queued"
      ? "⏳ <b>Queued for next turn.</b>"
      : result.disposition === "steered"
        ? "⚡ <b>Steered into active turn.</b>"
        : result.ok
          ? "✅ <b>Prompt delivered.</b>"
          : "🔴 <b>Prompt not sent.</b>";
    if (result.ok) this.selectedByChat.set(chatId, { backend, sessionId });
    await this.transport.sendMessage(chatId, `${heading}\n${escapeHtml(result.detail)}`, { parseMode: "HTML" });
  }
}
