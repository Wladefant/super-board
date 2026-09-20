/**
 * poller.ts — Long-polling Telegram Bot API transport & message dispatcher.
 */

import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as path from "node:path";
import {
  escapeHtml,
  formatTelegramCaption,
  getTokenFingerprint,
  markdownToTelegramHtml,
  redactSecrets,
} from "./sanitizer";
import { downloadInboundMedia, selectInboundMedia, type InboundMedia } from "./inbound-media";
import { registerTelegramCommands, renderTelegramHelp } from "./command-registry";
import type {
  AccessConfig,
  GroupAccessConfig,
  MessageCorrelationBridge,
  OutboundMessageCorrelation,
  TelegramGetUpdatesResponse,
  TelegramSendMessageResponse,
  TelegramUpdate,
} from "./types";

export interface PollerCallbacks {
  isIdle: () => boolean;
  getSessionFile?: () => string | undefined;
  onUserMessage: (text: string) => void;
  onFollowUp: (text: string) => void;
  onSteer: (text: string) => void;
  onAbort: () => void;
  onRelease: () => Promise<void>;
  getStatusText: () => string;
  onTelegramTurnStart: () => void;
  onHarnessCommand?: (text: string, chatId: string, userId?: string) => Promise<boolean>;
  onApprovalCallback?: (data: string, userId: string, chatId: string, sessionId: string) => Promise<string>;
  onDecisionCallback?: (
    decisionId: string,
    choiceId: string,
    context?: string,
  ) => void | Promise<void>;
  onQuestionAnswer?: (decisionId: string, eventId: string, answer: { choice?: string; text?: string }) => Promise<void>;
  /**
   * Reports an HTTP 409 conflict when Telegram getUpdates reports another poller
   * instance is polling with the same bot token.
   */
  onConflict?: (diagnosis: string, attempt: number, maxAttempts: number) => void;
  /**
   * Reports a ledger write the poller could not complete. Required rather than
   * optional: an ingest failure means inbound Telegram traffic is being dropped and
   * the offset cannot advance, so no caller may silently discard it.
   */
  onLedgerFailure: (message: string) => void;
}

/**
 * Stamps every inbound turn with the Telegram account it came from. A Telegram account
 * is not an attested human: the session must not read a bare instruction as an operator
 * identity, so the provenance travels with the text rather than beside it.
 */
function attributeSender(fromId: string, text: string): string {
  return `[Telegram sender: ${fromId}; origin: telegram_account; human presence not attested]\n${text}`;
}

export interface PollerOptions {
  maxConflictRetries?: number;
  initialConflictBackoffMs?: number;
  maxConflictBackoffMs?: number;
  conflictBackoffFactor?: number;
  /** Milliseconds to pace consecutive outbound requests. Defaults to 1250 ms. */
  outboundPaceMs?: number;
  /**
   * Forum topic every outbound message and dashboard pin is bound to. Omitted for a
   * plain chat, where Telegram rejects the field outright, so it has no default.
   */
  messageThreadId?: number;
  /**
   * Custom command surface for the chat-scoped menu (e.g. daemon commands).
   * When omitted, defaults to the standard availableCommands(hasHarness).
   */
  commands?: readonly { command: string; description: string }[];
  /**
   * Whether the poller is operating in standalone daemon mode.
   */
  isDaemon?: boolean;
  /**
   * Supergroup this channel serves in forum mode. Every topic of that chat is one
   * channel, so the topic is a property of each message rather than of the poller,
   * and {@link PollerOptions.messageThreadId} — which pins the channel to a single
   * topic — must stay unset when this is given.
   */
  forumChatId?: string;
}

const DEFAULT_POLLER_OPTIONS = {
  maxConflictRetries: 5,
  initialConflictBackoffMs: 1000,
  maxConflictBackoffMs: 15000,
  conflictBackoffFactor: 2.0,
  outboundPaceMs: 1250,
} satisfies PollerOptions;

interface LedgerRow {
  update_id: number;
  chat_id: string;
  user_id: string;
  text: string | null;
  received_at: number;
  // Rows written by an earlier bridge generation carry statuses this code never
  // defines (an installed ledger holds 'DISPATCHED_TO_SESSION'), so this is the value
  // as stored, not a closed union.
  status: string;
  error: string | null;
  reply_to_message_id: number | null;
  reply_to_text: string | null;
  correlated_session_id: string | null;
  correlated_request_id: string | null;
  callback_query_id: string | null;
  callback_data: string | null;
  is_callback: number | null;
  media_json: string | null;
  sender_origin: string | null;
  message_thread_id: number | null;
}

/**
 * Columns this poller's insert path writes, added in place to a ledger created by an
 * earlier bridge generation. Installed ledgers exist with only
 * (update_id, chat_id, user_id, received_at, status) — no text, no error, no status
 * CHECK — and `CREATE TABLE IF NOT EXISTS` is a no-op against them, so every column
 * missing from such a table has to be reconciled here or every insert fails.
 *
 * Additive and nullable on purpose. Rebuilding the table to restore NOT NULL and the
 * status CHECK would have to invent values for legacy rows and would reject the legacy
 * statuses they actually hold, so existing rows are preserved exactly as written.
 */
const UPDATE_LEDGER_ADDITIVE_COLUMNS: Record<string, string> = {
  text: "TEXT",
  error: "TEXT",
  reply_to_message_id: "INTEGER",
  reply_to_text: "TEXT",
  correlated_session_id: "TEXT",
  correlated_request_id: "TEXT",
  callback_query_id: "TEXT",
  callback_data: "TEXT",
  is_callback: "INTEGER DEFAULT 0",
  media_json: "TEXT",
  sender_origin: "TEXT",
  message_thread_id: "INTEGER",
};

/**
 * Statuses this poller may still act on. Every other value — including a legacy status
 * it has never written — counts as terminal: such a row is never redriven into a
 * session, so an old update cannot replay into a new one, and never pins the Telegram
 * offset, so it cannot block delivery of current updates either.
 */
const NON_TERMINAL_STATUSES = ["PENDING", "PROCESSING"] as const;
const NON_TERMINAL_STATUS_SQL = NON_TERMINAL_STATUSES.map(status => `'${status}'`).join(", ");

/**
 * How often an allowlisted operator is told that a chat is not served. Long enough
 * that a conversation in the wrong group cannot turn into a reply per message, short
 * enough that the answer is still there when they come back and try again.
 */
const CHAT_NOT_SERVED_NOTICE_MS = 60 * 60 * 1000;

export class TelegramPoller {
  private botToken: string;
  private botId: string;
  private stateDir: string;
  private accessConfig: AccessConfig;
  private callbacks: PollerCallbacks;
  private correlation: MessageCorrelationBridge | null;
  private options: typeof DEFAULT_POLLER_OPTIONS & PollerOptions;
  private readonly messageThreadId: number | undefined;
  private abortController: AbortController;
  private db: Database;
  private dbPath: string;
  private loopPromise: Promise<void> | null = null;
  private isRunning = false;
  private primaryChatId: string | null = null;
  private pendingDrain: Promise<void> | null = null;
  private nextOutboundAt = 0;
  private outboundReservation: Promise<void> = Promise.resolve();
  private dashboardUpdate: Promise<void> | null = null;
  /**
   * Topic of the update being processed right now, so a reply the handler produces
   * lands in the topic the operator wrote in. Set for the duration of one ledger row
   * and cleared after it: `drainPendingUpdates` awaits each row in turn, so there is
   * never a second row in flight to read a stale value.
   */
  private activeThreadId: number | undefined;

  public get running(): boolean {
    return this.isRunning;
  }

  constructor(
    botToken: string,
    stateDir: string,
    accessConfig: AccessConfig,
    callbacks: PollerCallbacks,
    correlation: MessageCorrelationBridge | null = null,
    threadOrOptions?: number | PollerOptions,
    options?: PollerOptions,
  ) {
    this.botToken = botToken;
    this.botId = getTokenFingerprint(botToken).botId;
    this.stateDir = stateDir;
    this.accessConfig = accessConfig;
    this.callbacks = callbacks;
    this.correlation = correlation;
    // Positional slot 6 carries either the forum topic this channel is pinned to
    // (number) or the poller tuning options (object). Two features claimed the same
    // argument, so callers of each shape are both still honoured.
    const threadId = typeof threadOrOptions === "number" ? threadOrOptions : undefined;
    const tuning = typeof threadOrOptions === "object" && threadOrOptions !== null ? threadOrOptions : options;
    this.options = { ...DEFAULT_POLLER_OPTIONS, ...(tuning || {}) };
    this.abortController = new AbortController();
    this.messageThreadId = threadId ?? this.options.messageThreadId;
    if (this.messageThreadId !== undefined && (!Number.isSafeInteger(this.messageThreadId) || this.messageThreadId <= 0)) {
      throw new Error("message_thread_id must be a positive integer");
    }
    if (this.messageThreadId !== undefined && this.options.forumChatId) {
      throw new Error("a forum channel serves every topic of its chat; message_thread_id pins it to one");
    }

    if (!fs.existsSync(stateDir)) {
      fs.mkdirSync(stateDir, { recursive: true });
    }

    this.dbPath = path.join(stateDir, "veyyon_bridge_state.db");
    this.db = new Database(this.dbPath);
    this.initLedger();
  }

  private ensureDbOpen(): void {
    try {
      this.db.query("SELECT 1").get();
    } catch {
      this.db = new Database(this.dbPath);
      this.initLedger();
    }
  }

  private initLedger(): void {
    this.db.run("PRAGMA journal_mode = WAL;");
    this.db.run("PRAGMA busy_timeout = 5000;");
    this.db.run(`
      CREATE TABLE IF NOT EXISTS update_ledger (
        update_id   INTEGER PRIMARY KEY,
        chat_id     TEXT NOT NULL,
        user_id     TEXT NOT NULL,
        text        TEXT,
        received_at REAL NOT NULL,
        status      TEXT NOT NULL CHECK(status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'REJECTED')),
        error       TEXT
      );
    `);
    this.db.run(`
      CREATE TABLE IF NOT EXISTS bridge_meta (
        key   TEXT PRIMARY KEY,
        value TEXT
      );
    `);

    this.migrateUpdateLedgerColumns();
  }

  /**
   * Brings an existing update_ledger up to the column set the insert path writes.
   * Idempotent: a table that already has every column is left untouched, and a table
   * missing some gets exactly those added, with its rows intact.
   */
  private migrateUpdateLedgerColumns(): void {
    const existingColumns = new Set(
      (this.db.query("PRAGMA table_info(update_ledger)").all() as Array<{ name: string }>).map(c => c.name),
    );
    for (const [column, columnType] of Object.entries(UPDATE_LEDGER_ADDITIVE_COLUMNS)) {
      if (!existingColumns.has(column)) {
        this.db.run(`ALTER TABLE update_ledger ADD COLUMN ${column} ${columnType};`);
      }
    }
  }

  public getNextContiguousOffset(): number {
    try {
      // If any pending or in-flight processing updates exist, start at the earliest non-terminal update
      const pendingRow = this.db
        .query(
          `SELECT MIN(update_id) as min_pending FROM update_ledger WHERE status IN (${NON_TERMINAL_STATUS_SQL})`,
        )
        .get() as { min_pending: number | null } | null;

      if (pendingRow && typeof pendingRow.min_pending === "number") {
        return pendingRow.min_pending;
      }

      // Every known update is terminal: COMPLETED, REJECTED, or a legacy status this
      // poller does not act on.
      const maxRow = this.db
        .query("SELECT MAX(update_id) as max_id FROM update_ledger")
        .get() as { max_id: number | null } | null;

      return (maxRow?.max_id ?? 0) + 1;
    } catch (err: unknown) {
      // The ledger is unreadable, so nothing is known to be handled. Offset 0 asks
      // Telegram for whatever is still pending and confirms nothing, which is the only
      // safe answer here; a computed offset would forget updates on no evidence.
      this.callbacks.onLedgerFailure(
        `next offset could not be computed: ${redactSecrets(err instanceof Error ? err.message : String(err))}`,
      );
      return 0;
    }
  }

  public getPrimaryChatId(): string | null {
    // A forum channel only ever serves its supergroup: the operator's own user id is
    // not a chat this poller speaks in, so it must never be the fallback here.
    if (this.options.forumChatId) return this.options.forumChatId;
    if (this.primaryChatId) return this.primaryChatId;
    if (this.accessConfig.allowFrom.length > 0) {
      return this.accessConfig.allowFrom[0];
    }
    return null;
  }

  /**
   * Forum topic of the update being handled right now, or undefined outside a topic
   * (a DM, or the supergroup's General topic). Read synchronously from a callback the
   * poller invoked, which is the only point at which it is meaningful.
   */
  public getActiveThreadId(): number | undefined {
    return this.activeThreadId;
  }
  public updateAccess(config: AccessConfig): void {
    this.accessConfig = config;
  }
  public getMeta(key: string): string | null {
    return (this.db.query("SELECT value FROM bridge_meta WHERE key = ?").get(key) as { value: string } | null)?.value ?? null;
  }

  public setMeta(key: string, value: string): void {
    this.db.run("INSERT INTO bridge_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", [key, value]);
  }

  private paceOutbound(): Promise<void> {
    const reserve = this.outboundReservation.then(async () => {
      const delay = Math.max(0, this.nextOutboundAt - Date.now());
      if (delay) await new Promise<void>((resolve, reject) => {
        const signal = this.abortController.signal;
        const abort = () => { clearTimeout(timer); reject(new Error("Telegram transport stopped")); };
        const timer = setTimeout(() => { signal.removeEventListener("abort", abort); resolve(); }, delay);
        signal.addEventListener("abort", abort, { once: true });
        if (signal.aborted) abort();
      });
      this.abortController.signal.throwIfAborted();
      const paceMs = this.options.outboundPaceMs ?? 1250;
      this.nextOutboundAt = paceMs > 0 ? Date.now() + paceMs : 0;
    });
    this.outboundReservation = reserve.catch(() => {});
    return reserve;
  }

  private observeRateLimit(data: TelegramSendMessageResponse): void {
    if (data.error_code === 429) {
      this.nextOutboundAt = Math.max(this.nextOutboundAt, Date.now() + Math.max(1, data.parameters?.retry_after ?? 30) * 1000);
    }
  }

  public updateDashboard(chatId: string, html: string): Promise<void> {
    // The caller refreshes its newest snapshot every 30s; never enqueue one edit per event.
    if (!this.dashboardUpdate) {
      this.dashboardUpdate = this.writeDashboard(chatId, html).finally(() => { this.dashboardUpdate = null; });
    }
    return this.dashboardUpdate;
  }

  private async writeDashboard(chatId: string, html: string): Promise<void> {
    const key = `dashboard:${this.correlation?.getSessionId()}:${chatId}:${this.messageThreadId ?? 0}`;
    const last = Number(this.getMeta(`${key}:updated`) ?? 0);
    if (Date.now() - last < 30_000) return;
    let messageId = Number(this.getMeta(key) ?? 0);
    if (messageId) {
      const edited = await this.editTelegramMessage(chatId, messageId, html);
      if (!edited?.ok && !edited?.description?.includes("message is not modified")) {
        if (edited?.error_code === 400 && edited.description?.includes("message to edit not found")) {
          messageId = 0;
        } else {
          throw new Error("Dashboard refresh delayed; existing card retained (no duplicate posted)");
        }
      }
    }
    if (!messageId) {
      const sent = await this.sendTelegramMessage(chatId, html);
      if (!sent?.ok || !sent.result?.message_id) throw new Error("Dashboard delivery failed");
      messageId = sent.result.message_id;
      this.setMeta(key, String(messageId));
    }
    // Private-chat operator pins can disappear while getChat still reports this id.
    // Reassert the same pin silently; coalescing and pacing bound this idempotent call.
    await this.dashboardApi("pinChatMessage", { chat_id: chatId, message_id: messageId, disable_notification: true });
    this.setMeta(`${key}:updated`, String(Date.now()));
  }

  private async dashboardApi(method: string, body: Record<string, unknown>): Promise<void> {
    await this.paceOutbound();
    const response = await fetch(`https://api.telegram.org/bot${this.botToken}/${method}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      signal: AbortSignal.any([this.abortController.signal, AbortSignal.timeout(3000)]),
    });
    const data = await response.json();
    this.observeRateLimit(data);
    if (!data.ok) throw new Error(`Dashboard ${method} unavailable; check pin permission and Telegram retry window`);
  }

  /**
   * Topic every outbound message defaults to: the topic this channel is pinned to,
   * else the topic of the update being handled, so a handler's reply comes back where
   * the operator wrote it. Undefined in a DM and in a forum's General topic.
   */
  private get outboundThreadId(): number | undefined {
    return this.messageThreadId ?? this.activeThreadId;
  }

  public async sendTelegramMessage(
    chatId: string | number,
    text: string,
    replyMarkupOrParseMode?: Record<string, unknown> | "HTML" | "Markdown",
    maybeReplyMarkup?: Record<string, unknown>,
    correlationMeta?: {
      requestId?: string | null;
      decisionId?: string | null;
      projectPath?: string | null;
      laneId?: string;
      laneState?: "active" | "exited" | "unknown";
    },
    defaultRepo = "Bavariance/polysimulator",
    /**
     * Forum topic to post into, for a send that is not a reply to the update being
     * handled — relaying a session's output into its own topic, above all. Omitted
     * falls back to {@link outboundThreadId}.
     */
    messageThreadId?: number,
  ): Promise<TelegramSendMessageResponse | null> {
    const sanitized = redactSecrets(text);
    const formatted = replyMarkupOrParseMode === "HTML" ? sanitized : markdownToTelegramHtml(sanitized, defaultRepo);
    if (!formatted.trim()) return null;

    const replyMarkup = typeof replyMarkupOrParseMode === "string"
      ? maybeReplyMarkup
      : (replyMarkupOrParseMode ?? maybeReplyMarkup);

    try {
      const body: Record<string, unknown> = {
        chat_id: chatId,
        text: formatted,
        parse_mode: "HTML",
      };
      const threadId = messageThreadId ?? this.outboundThreadId;
      if (threadId !== undefined) body.message_thread_id = threadId;
      if (replyMarkup) {
        body.reply_markup = replyMarkup;
      }

      // Resolve the owning session BEFORE the roundtrip. An in-TUI session switch
      // during the await would otherwise index this outbound text against whichever
      // session arrived later, and a reply to it would be delivered into that
      // unrelated session instead of refused.
      const boundSlotId = this.correlation?.getSlotId() ?? null;
      const boundSessionId = this.correlation?.getSessionId() ?? null;

      await this.paceOutbound();
      const response = await fetch(
        `https://api.telegram.org/bot${this.botToken}/sendMessage`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: AbortSignal.any([this.abortController.signal, AbortSignal.timeout(3000)]),
        },
      );

      const data = (await response.json()) as TelegramSendMessageResponse;
      this.observeRateLimit(data);
      if (
        data?.ok &&
        typeof data.result?.message_id === "number" &&
        this.correlation &&
        boundSlotId !== null &&
        boundSessionId !== null
      ) {
        this.correlation.record({
          botId: this.botId,
          // Keyed on the chat the API actually delivered to, so a destination given as
          // a name rather than a numeric id still produces a matchable key.
          chatId: String(data.result.chat?.id ?? chatId),
          messageId: data.result.message_id,
          slotId: boundSlotId,
          sessionId: boundSessionId,
          requestId: correlationMeta?.requestId ?? null,
          decisionId: correlationMeta?.decisionId ?? null,
          projectPath: correlationMeta?.projectPath ?? null,
          createdAt: Date.now() / 1000,
          laneId: correlationMeta?.laneId,
          laneState: correlationMeta?.laneState,
          senderOrigin: "agent",
        });
      }
      return data;
    } catch {
      return null;
    }
  }

  public async editTelegramMessage(
    chatId: string | number,
    messageId: number,
    text: string,
    _parseMode?: "HTML" | "Markdown",
    defaultRepo = "Bavariance/polysimulator",
    replyMarkup?: Record<string, unknown>,
  ): Promise<TelegramSendMessageResponse | null> {
    const formatted = markdownToTelegramHtml(redactSecrets(text), defaultRepo);
    if (!formatted.trim()) return null;

    try {
      const body: Record<string, unknown> = {
        chat_id: chatId,
        message_id: messageId,
        text: formatted,
        parse_mode: "HTML",
        reply_markup: replyMarkup ?? { inline_keyboard: [] },
      };

      await this.paceOutbound();
      const response = await fetch(
        `https://api.telegram.org/bot${this.botToken}/editMessageText`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: AbortSignal.any([this.abortController.signal, AbortSignal.timeout(3000)]),
        },
      );

      const data = (await response.json()) as TelegramSendMessageResponse;
      this.observeRateLimit(data);
      return data;
    } catch {
      return null;
    }
  }
  public async clearCallbackButtons(chatId: string, messageId: number, replacementText?: string): Promise<boolean> {
    try {
      const reply_markup = replacementText
        ? { inline_keyboard: [[{ text: replacementText, callback_data: "noop" }]] }
        : { inline_keyboard: [] };
      const response = await fetch(`https://api.telegram.org/bot${this.botToken}/editMessageReplyMarkup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chatId, message_id: messageId, reply_markup }),
        signal: AbortSignal.any([this.abortController.signal, AbortSignal.timeout(3000)]),
      });
      const data: unknown = await response.json();
      return Boolean(data && typeof data === "object" && "ok" in data && data.ok === true);
    } catch {
      return false;
    }
  }
  public async answerCallbackQuery(
    callbackQueryId: string,
    text?: string,
    showAlert: boolean = false,
  ): Promise<boolean> {
    try {
      const body: Record<string, unknown> = {
        callback_query_id: callbackQueryId,
        show_alert: showAlert,
      };
      if (text) {
        body.text = redactSecrets(text);
      }

      const response = await fetch(
        `https://api.telegram.org/bot${this.botToken}/answerCallbackQuery`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: AbortSignal.any([this.abortController.signal, AbortSignal.timeout(3000)]),
        },
      );
      const data: unknown = await response.json();
      return Boolean(data && typeof data === "object" && "ok" in data && data.ok === true);
    } catch {
      return false;
    }
  }


  public async start(): Promise<void> {
    if (this.isRunning) return;
    this.isRunning = true;
    this.abortController = new AbortController();
    this.ensureDbOpen();

    this.loopPromise = this.runPollLoop();
    try {
      await this.loopPromise;
    } finally {
      this.isRunning = false;
      this.loopPromise = null;
    }
  }

  private async runPollLoop(): Promise<void> {
    // The lease holder refreshes the operator's private menu on every startup.
    // Registration failure must not disconnect an otherwise usable input channel.
    if (this.accessConfig.dmPolicy !== "disabled") {
      try {
        await registerTelegramCommands(
          this.botToken,
          this.accessConfig.allowFrom,
          Boolean(this.callbacks.onHarnessCommand),
          this.abortController.signal,
          this.options.commands,
          this.options.forumChatId,
        );
      } catch {
        this.callbacks.onLedgerFailure("Telegram command registration failed; reconnect to retry the private-chat menu.");
      }
    }

    // 1. Redrive any pending updates from previous crashed runs safely
    await this.redrivePendingUpdates();

    let offset = this.getNextContiguousOffset();
    let conflictCount = 0;

    while (this.isRunning && !this.abortController.signal.aborted) {
      try {
        const allowedUpdates = encodeURIComponent(JSON.stringify(["message", "callback_query"]));
        const url = `https://api.telegram.org/bot${this.botToken}/getUpdates?offset=${offset}&timeout=20&allowed_updates=${allowedUpdates}`;
        const res = await fetch(url, { signal: this.abortController.signal });

        if (!res.ok) {
          if (res.status === 409) {
            conflictCount++;
            let description = "Conflict: terminated by other getUpdates request";
            try {
              const body = (await res.json()) as { description?: string };
              if (body?.description) {
                description = body.description;
              }
            } catch {}

            const isExhausted = conflictCount >= this.options.maxConflictRetries;
            const diagnosis = `Telegram getUpdates HTTP 409 Conflict (attempt ${conflictCount}/${this.options.maxConflictRetries}): ${description}. ${
              isExhausted
                ? "Conflict retry limit reached; terminating polling loop to prevent thrashing with another bot instance."
                : "Backing off before retry."
            }`;

            this.callbacks.onConflict?.(diagnosis, conflictCount, this.options.maxConflictRetries);

            if (isExhausted) {
              this.callbacks.onLedgerFailure(diagnosis);
              this.isRunning = false;
              break;
            }

            const backoff = Math.min(
              this.options.maxConflictBackoffMs,
              this.options.initialConflictBackoffMs * Math.pow(this.options.conflictBackoffFactor, conflictCount - 1),
            );
            await Bun.sleep(backoff);
            continue;
          }

          await Bun.sleep(3000);
          continue;
        }

        // Reset conflict counter on successful response
        conflictCount = 0;

        const data = (await res.json()) as TelegramGetUpdatesResponse;
        if (data.ok && Array.isArray(data.result)) {
          // Transactionally record incoming updates as PENDING before processing.
          // A failure here means inbound traffic is being dropped, so it is reported
          // and retried rather than swallowed.
          try {
            this.ingestUpdates(data.result);
            // Stop every click's spinner before media downloads or session delivery
            // can hold up the serial ledger queue. This acknowledges receipt only.
            await Promise.all(data.result.flatMap(update => update.callback_query
              ? [this.answerCallbackQuery(update.callback_query.id, "Received; checking selection.")]
              : []));
          } catch (err: unknown) {
            this.callbacks.onLedgerFailure(
              redactSecrets(err instanceof Error ? err.message : String(err)),
            );
          }

          // Process and advance offset past terminal contiguous updates
          await this.redrivePendingUpdates();
          offset = this.getNextContiguousOffset();
        }
        if (this.isRunning && !this.abortController.signal.aborted) {
          await Bun.sleep(100);
        }
      } catch (err: unknown) {
        if (this.abortController.signal.aborted) break;
        await Bun.sleep(2000);
      }
    }
  }

  public ingestUpdates(updates: TelegramUpdate[]): void {
    const now = Date.now() / 1000;
    try {
      this.db.run("BEGIN IMMEDIATE;");
      for (const update of updates) {
        if (update.callback_query) {
          const cb = update.callback_query;
          const chatId = cb.message ? String(cb.message.chat.id) : String(cb.from.id);
          const fromId = String(cb.from.id);
          const callbackData = cb.data ?? "";
          const callbackQueryId = cb.id;
          const replyToMessageId = cb.message?.message_id ?? null;

          this.db.run(
            `INSERT INTO update_ledger (
               update_id, chat_id, user_id, text, received_at, status,
               reply_to_message_id, reply_to_text, callback_query_id, callback_data, is_callback
             ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, 1)
             ON CONFLICT(update_id) DO NOTHING;`,
            [update.update_id, chatId, fromId, callbackData, now, replyToMessageId, cb.message?.text ?? cb.message?.caption ?? null, callbackQueryId, callbackData],
          );
          this.db.run("UPDATE update_ledger SET sender_origin = COALESCE(sender_origin, ?) WHERE update_id = ?",
            [cb.from.is_bot ? "agent" : "telegram_account", update.update_id]);
          this.db.run("UPDATE update_ledger SET message_thread_id = ? WHERE update_id = ?",
            [cb.message?.message_thread_id ?? null, update.update_id]);
          continue;
        }

        const msg = update.message;
        const chatId = msg ? String(msg.chat.id) : "";
        const fromId = msg?.from ? String(msg.from.id) : "";
        let text = msg?.text ?? msg?.caption ?? null;
        if (!text && msg) {
          if (msg.photo && Array.isArray(msg.photo) && msg.photo.length > 0) {
            text = "<photo>";
          } else if (msg.document) {
            text = msg.document.file_name ? `<file: ${msg.document.file_name}>` : "<file>";
          }
        }
        const replyToMessageId = msg?.reply_to_message?.message_id ?? null;
        let replyToText: string | null = null;
        const rMsg = msg?.reply_to_message;
        if (rMsg) {
          if (rMsg.text && rMsg.text.trim()) {
            replyToText = rMsg.text.trim();
          } else if (rMsg.caption && rMsg.caption.trim()) {
            replyToText = rMsg.caption.trim();
          } else if (rMsg.document?.file_name) {
            replyToText = `<file: ${rMsg.document.file_name}>`;
          } else if (rMsg.photo && Array.isArray(rMsg.photo) && rMsg.photo.length > 0) {
            replyToText = "<photo>";
          }
        }

        const media = selectInboundMedia(msg);
        this.db.run(
          `INSERT INTO update_ledger (
             update_id, chat_id, user_id, text, received_at, status,
             reply_to_message_id, reply_to_text, media_json
           ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
           ON CONFLICT(update_id) DO NOTHING;`,
          [update.update_id, chatId, fromId, text, now, replyToMessageId, replyToText, media ? JSON.stringify(media) : null],
        );
        this.db.run("UPDATE update_ledger SET sender_origin = COALESCE(sender_origin, ?) WHERE update_id = ?",
          [msg?.from?.is_bot ? "agent" : msg?.from ? "telegram_account" : "unknown", update.update_id]);
        this.db.run("UPDATE update_ledger SET message_thread_id = ? WHERE update_id = ?",
          [msg?.message_thread_id ?? null, update.update_id]);
      }
      this.db.run("COMMIT;");
    } catch (err: unknown) {
      try {
        this.db.run("ROLLBACK;");
      } catch {}
      // Never swallowed: a rolled-back ingest drops every update in this batch and
      // leaves the Telegram offset pinned, so the same batch is refetched forever
      // while nothing is delivered. The caller has to see that.
      throw new Error(
        `update_ledger ingest of ${updates.length} update(s) failed and was rolled back: ${
          err instanceof Error ? err.message : String(err)
        }`,
      );
    }
  }

  public redrivePendingUpdates(): Promise<void> {
    // One leased poller owns the queue. Concurrent redrives share its drain,
    // so delayed consumption cannot let two clicks dispatch simultaneously.
    if (!this.pendingDrain) {
      this.pendingDrain = Promise.resolve().then(() => this.drainPendingUpdates())
        .finally(() => { this.pendingDrain = null; });
    }
    return this.pendingDrain;
  }

  private async drainPendingUpdates(): Promise<void> {
    let rows: LedgerRow[];
    try {
      rows = this.db
        .query(
          `SELECT * FROM update_ledger WHERE status IN (${NON_TERMINAL_STATUS_SQL}) ORDER BY update_id ASC`,
        )
        .all() as LedgerRow[];
    } catch (err: unknown) {
      // The ledger itself is unreadable. Report it and return: throwing here would
      // escape start()'s startup redrive, which runs outside the poll loop's own
      // handler, and take down the channel with an unhandled rejection.
      this.callbacks.onLedgerFailure(
        `update_ledger could not be read: ${redactSecrets(err instanceof Error ? err.message : String(err))}`,
      );
      return;
    }

    for (const row of rows) {
      if (this.abortController.signal.aborted) break;
      try {
        await this.processLedgerRow(row);
      } catch (err: unknown) {
        // A row that throws must not stay non-terminal: it would pin the offset and
        // head-of-line block every later update indefinitely. Record why it failed
        // and move on, so the queue drains and the failure is visible in the ledger.
        const detail = redactSecrets(err instanceof Error ? err.message : String(err)).slice(0, 500);
        try {
          this.db.run("UPDATE update_ledger SET status = 'REJECTED', error = ? WHERE update_id = ?", [
            `PROCESSING_FAILED: ${detail}`,
            row.update_id,
          ]);
        } catch {}
        this.callbacks.onLedgerFailure(`update ${row.update_id} could not be processed: ${detail}`);
        if (row.is_callback) {
          await this.sendTelegramMessage(row.chat_id, `Choice could not be delivered: ${escapeHtml(detail)}. Reply to the original message with your choice.`);
        }
      }
    }
  }

  private async processLedgerRow(row: LedgerRow): Promise<void> {
    // A forum's General topic carries no thread id; Telegram reports 1 for it on the
    // updates that do, and neither is a topic a session can be bound to.
    this.activeThreadId =
      typeof row.message_thread_id === "number" && row.message_thread_id > 1 ? row.message_thread_id : undefined;
    try {
      await this.dispatchLedgerRow(row);
    } finally {
      this.activeThreadId = undefined;
    }
  }

  /**
   * Group or supergroup authorization for `chatId`, or null when this channel does
   * not serve that chat.
   */
  private groupAccess(chatId: string): GroupAccessConfig | null {
    const configured = this.accessConfig.groups?.[chatId];
    if (configured) return configured;
    // A forum channel's own supergroup is authorized by the manifest that put it in
    // forum mode. access.json is healed to match at startup, but a write that did not
    // land must not lock the operator out of the only chat this channel serves.
    return this.options.forumChatId === chatId ? {} : null;
  }

  /**
   * Tells an allowlisted operator that this bot does not serve the chat they wrote
   * in, at most once an hour per chat. Dropping the message instead is what made a
   * misconfigured forum look like a dead bot: every message vanished and the ledger
   * row said UNAUTHORIZED where nobody was looking.
   */
  private async reportChatNotServed(chatId: string): Promise<void> {
    const key = `chat-not-served-notice:${chatId}`;
    if (Date.now() - Number(this.getMeta(key) ?? 0) < CHAT_NOT_SERVED_NOTICE_MS) return;
    // Recorded before the send: a chat that keeps refusing delivery must not turn
    // every inbound message into another outbound attempt.
    this.setMeta(key, String(Date.now()));
    await this.sendTelegramMessage(
      chatId,
      [
        "🚫 <b>This bot does not serve this chat.</b>",
        "Your Telegram account is allowed, but this group is not one this bot is bound to, so nothing here reaches a session.",
        "Write in the bot's direct chat, or add this chat id to <code>groups</code> in the channel's <code>access.json</code> and reload.",
      ].join("\n"),
      "HTML",
    );
  }

  private async dispatchLedgerRow(row: LedgerRow): Promise<void> {
    // Mark as in-flight PROCESSING
    this.db.run("UPDATE update_ledger SET status = 'PROCESSING' WHERE update_id = ?", [row.update_id]);

    const hasVisibleText = Boolean(row.text?.replace(/[\s\u2000-\u200F\u2028-\u202F\u205F-\u206F\uFEFF]/g, ""));
    if (!hasVisibleText && !row.media_json) {
      this.db.run("UPDATE update_ledger SET status = 'REJECTED', error = 'EMPTY_TEXT' WHERE update_id = ?", [
        row.update_id,
      ]);
      return;
    }

    const chatId = row.chat_id;
    const fromId = row.user_id;

    // 1. dmPolicy check
    if (this.accessConfig.dmPolicy === "disabled") {
      this.db.run(
        "UPDATE update_ledger SET status = 'REJECTED', error = 'DM_POLICY_DISABLED' WHERE update_id = ?",
        [row.update_id],
      );
      return;
    }

    // 2. Allowlist authorization. A private chat is authorized by the allowlist alone
    //    (`chatId === fromId` is what makes it private); any other chat also has to be
    //    named in access.json `groups`, because an allowlisted Telegram account says
    //    nothing about which rooms that account may speak for. In a forum every topic
    //    of the supergroup is admitted: the topic selects the session, not the right
    //    to talk to one.
    const operatorIsAllowed = this.accessConfig.allowFrom.includes(fromId);
    const isDirectMessage = chatId === fromId;
    const group = isDirectMessage ? null : this.groupAccess(chatId);
    // A group's own allowFrom intersects the channel allowlist rather than replacing
    // it: a group entry restricts where an allowlisted account may speak, and must
    // never admit an account the channel itself does not allow.
    const isAllowed = operatorIsAllowed
      && (isDirectMessage || (group !== null && (group.allowFrom === undefined || group.allowFrom.includes(fromId))));
    if (!isAllowed) {
      this.db.run(
        "UPDATE update_ledger SET status = 'REJECTED', error = 'UNAUTHORIZED' WHERE update_id = ?",
        [row.update_id],
      );
      if (operatorIsAllowed && !isDirectMessage) await this.reportChatNotServed(chatId);
      return;
    }

    // Telegram authenticates an account, not the human or automation at its keyboard.
    // Bot-authored/unknown input is never admitted as an operator turn.
    if (row.sender_origin !== "telegram_account") {
      this.db.run("UPDATE update_ledger SET status = 'REJECTED', error = 'NON_OPERATOR_ORIGIN' WHERE update_id = ?", [row.update_id]);
      return;
    }
    if (this.messageThreadId !== undefined && row.message_thread_id !== this.messageThreadId) {
      this.db.run("UPDATE update_ledger SET status = 'REJECTED', error = 'WRONG_THREAD' WHERE update_id = ?", [row.update_id]);
      return;
    }
    this.primaryChatId = chatId;
    let rawText = row.text?.trim() ?? "";

    // 3. Callback query handling for interactive decision buttons
    if (row.is_callback === 1 || Boolean(row.callback_query_id)) {
      const callbackToken = row.callback_data || row.text || "";
      const cbQueryId = row.callback_query_id || "";
      if (callbackToken.startsWith("ap:")) {
        const sessionId = this.correlation?.getSessionId();
        try {
          if (!sessionId || !this.callbacks.onApprovalCallback) throw new Error("Approval handling is unavailable.");
          const outcome = await this.callbacks.onApprovalCallback(callbackToken, fromId, chatId, sessionId);
          if (cbQueryId) await this.answerCallbackQuery(cbQueryId, outcome);
          if (typeof row.reply_to_message_id === "number") {
            const isApproved = callbackToken.startsWith("ap:a:");
            const d = new Date();
            const timeStr = `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")} UTC`;
            const buttonText = isApproved ? `✅ Approved by you at ${timeStr}` : "❌ Denied";
            await this.clearCallbackButtons(chatId, row.reply_to_message_id, buttonText);
          }
          await this.sendTelegramMessage(chatId, escapeHtml(outcome));
          this.db.run("UPDATE update_ledger SET status = 'COMPLETED', correlated_session_id = ? WHERE update_id = ?", [sessionId, row.update_id]);
        } catch (error) {
          const detail = error instanceof Error ? error.message : "Approval decision unavailable; nothing authorized.";
          if (cbQueryId) await this.answerCallbackQuery(cbQueryId, detail, true);
          this.db.run("UPDATE update_ledger SET status = 'REJECTED', error = 'APPROVAL_REJECTED' WHERE update_id = ?", [row.update_id]);
        }
        return;
      }

      if (!this.correlation?.resolveCallback) {
        if (cbQueryId) {
          await this.answerCallbackQuery(cbQueryId, "Callback resolution unavailable.", true);
        }
        this.db.run(
          "UPDATE update_ledger SET status = 'REJECTED', error = 'CALLBACK_BRIDGE_UNAVAILABLE' WHERE update_id = ?",
          [row.update_id],
        );
        return;
      }

      const resolution = this.correlation.resolveCallback(callbackToken, fromId, chatId);
      if (resolution.decision !== "deliver" || !resolution.record) {
        if (cbQueryId) {
          await this.answerCallbackQuery(cbQueryId, `Selection rejected: ${resolution.detail}`, true);
        }
        this.db.run(
          "UPDATE update_ledger SET status = 'REJECTED', error = ? WHERE update_id = ?",
          [`CALLBACK_${resolution.decision.toUpperCase()}`, row.update_id],
        );
        await this.sendTelegramMessage(chatId, `Selection not delivered: ${escapeHtml(resolution.detail)}. Reconnect the original session or reply to the question with your choice.`);
        if (typeof row.reply_to_message_id === "number" &&
            ["reject_unknown", "reject_expired", "reject_already_consumed", "reject_already_answered"].includes(resolution.decision)) {
          await this.clearCallbackButtons(chatId, row.reply_to_message_id);
        }
        return;
      }

      const record = resolution.record;
      if (record.decisionId.startsWith("tq:")) {
        if (!this.callbacks.onQuestionAnswer || !this.correlation.consumeCallback) {
          throw new Error("Question delivery unavailable. Reply to the original question when the channel reconnects.");
        }
        await this.callbacks.onQuestionAnswer(record.decisionId, `update:${row.update_id}`, { choice: record.choiceId });
        if (!this.correlation.consumeCallback(callbackToken)) {
          this.callbacks.onLedgerFailure(`Question callback ${row.update_id} saved but consumption was not confirmed`);
        }
        this.db.run("UPDATE update_ledger SET status = 'COMPLETED', correlated_session_id = ? WHERE update_id = ?",
          [record.sessionId, row.update_id]);
        return;
      }
      if (!this.callbacks.onDecisionCallback || !this.correlation.consumeCallback) {
        throw new Error("Decision delivery unavailable; reply to the message with your choice.");
      }
      // Recheck after the asynchronous receipt acknowledgement, before delivery.
      if (record.sessionId !== this.correlation.getSessionId()) {
        throw new Error("Session changed; reconnect the original session and retry your choice.");
      }
      // A completed local delivery also deduplicates if the shared callback
      // store could not persist consumption after accepting the operator reply.
      const delivered = this.db.query(
        "SELECT update_id FROM update_ledger WHERE callback_data = ? AND status = 'COMPLETED' LIMIT 1",
      ).get(callbackToken);
      if (delivered) {
        this.db.run("UPDATE update_ledger SET status = 'REJECTED', error = 'CALLBACK_ALREADY_DELIVERED' WHERE update_id = ?", [row.update_id]);
        await this.sendTelegramMessage(chatId, "This choice was already delivered to the session.");
        if (typeof row.reply_to_message_id === "number") await this.clearCallbackButtons(chatId, row.reply_to_message_id);
        return;
      }
      this.callbacks.onTelegramTurnStart();
      await this.callbacks.onDecisionCallback(record.decisionId, record.choiceId,
        [`Callback identity: ${record.callbackToken}`, row.reply_to_text].filter(Boolean).join("\n"));
      // At-least-once across a crash between delivery and consumption: never
      // irreversibly discard a choice before the session has accepted it.
      if (!this.correlation.consumeCallback(callbackToken)) {
        this.callbacks.onLedgerFailure(`Callback ${row.update_id} delivered but consumption was not confirmed`);
      }

      // Preserve the original question/caption (including photo messages). Do not
      // claim canonical authorization or a cleared blocker merely from a click.
      if (typeof row.reply_to_message_id === "number") {
        await this.clearCallbackButtons(chatId, row.reply_to_message_id);
      }
      await this.sendTelegramMessage(chatId, `Choice <b>${escapeHtml(record.choiceId)}</b> delivered to the session.`);

      this.db.run(
        "UPDATE update_ledger SET status = 'COMPLETED', correlated_session_id = ? WHERE update_id = ?",
        [record.sessionId, row.update_id],
      );
      return;
    }

    // 3. Reply correlation gate. A reply carries the identity of the message it
    //    answers, so it must resolve to an outbound message of THIS session. An
    //    unknown, unbound, or foreign-session target is refused instead of being
    //    injected into whichever session currently holds the bot lease.
    const inboundSessionId = this.correlation?.getSessionId();
    let replyCorrelation: OutboundMessageCorrelation | null = null;
    if (typeof row.reply_to_message_id === "number") {
      const resolution = this.correlation
        ? this.correlation.resolveReply(this.botId, chatId, row.reply_to_message_id)
        : {
            decision: "reject_unavailable" as const,
            detail: "No correlation index is attached to this channel.",
          };

      if (resolution.decision !== "deliver") {
        this.db.run(
          "UPDATE update_ledger SET status = 'REJECTED', error = ?, correlated_session_id = ? WHERE update_id = ?",
          [
            `REPLY_${resolution.decision.replace(/^reject_/, "").toUpperCase()}`,
            resolution.correlation?.sessionId ?? null,
            row.update_id,
          ],
        );
        await this.sendTelegramMessage(
          chatId,
          [
            "🚫 <b>Reply not routed.</b>",
            escapeHtml(resolution.detail),
            "<i>Send a new message instead of replying to an earlier one.</i>",
          ].join("\n"),
        );
        return;
      }

      replyCorrelation = resolution.correlation ?? null;

      this.db.run(
        "UPDATE update_ledger SET correlated_session_id = ?, correlated_request_id = ? WHERE update_id = ?",
        [
          resolution.correlation?.sessionId ?? null,
          resolution.correlation?.requestId ?? null,
          row.update_id,
        ],
      );
    }
    if (replyCorrelation?.decisionId?.startsWith("tq:")) {
      if (!this.callbacks.onQuestionAnswer) throw new Error("Question receiver unavailable; answer was not delivered");
      try {
        await this.callbacks.onQuestionAnswer(replyCorrelation.decisionId, `update:${row.update_id}`, { text: rawText });
        this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
      } catch (error) {
        await this.sendTelegramMessage(chatId, `Answer not delivered: ${escapeHtml(String(error))}`);
        throw error;
      }
      return;
    }

    if (row.media_json) {
      const sessionFile = this.callbacks.getSessionFile?.();
      const sessionId = this.correlation?.getSessionId();
      try {
        if (!sessionFile) throw new Error("Session attachment directory unavailable. Please resend after reconnecting.");
        const media = JSON.parse(row.media_json) as InboundMedia;
        const attachment = await downloadInboundMedia(this.botToken, media,
          path.join(path.dirname(sessionFile), "local", "telegram-inbound"), row.update_id, this.abortController.signal);
        if (this.callbacks.getSessionFile?.() !== sessionFile || this.correlation?.getSessionId() !== sessionId) {
          throw new Error("Session changed during attachment download. Please resend to the current session.");
        }
        const caption = rawText === "<photo>" || rawText.startsWith("<file:") || rawText === "<file>" ? "" : rawText;
        rawText = `[Telegram ${media.mime_type === "application/pdf" ? "document" : "image"} from operator | ${caption}] attachment: ${attachment}`;
      } catch (error) {
        const detail = error instanceof Error ? error.message : "Attachment download failed. Please resend.";
        this.db.run("UPDATE update_ledger SET status = 'REJECTED', error = ? WHERE update_id = ?", [detail, row.update_id]);
        await this.sendTelegramMessage(chatId, escapeHtml(detail));
        return;
      }
    }

    // 4. Command handling
    if (!row.media_json) rawText = rawText.replace(/^(\/\w+)@\w+(?=\s|$)/, "$1");
    if (!row.media_json && await this.callbacks.onHarnessCommand?.(rawText, chatId, fromId)) {
      this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
      return;
    }
    if (this.correlation?.getSessionId() !== inboundSessionId) {
      throw new Error("Session changed during message routing; resend to the intended session");
    }
    if (rawText === "/help" || rawText === "/start") {
      const isDaemon = Boolean(
        this.options.isDaemon ||
        this.options.commands?.some(c => c.command === "sessions" || c.command === "attach" || c.command === "app"),
      );
      await this.sendTelegramMessage(
        chatId,
        renderTelegramHelp({
          hasHarness: Boolean(this.callbacks.onHarnessCommand),
          isDaemon,
        }),
      );
      this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
      return;
    }

    if (rawText === "/status") {
      const statusText = this.callbacks.getStatusText();
      await this.sendTelegramMessage(chatId, statusText);
      this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
      return;
    }

    if (rawText === "/cancel" || rawText === "/stop" || rawText === "/abort") {
      const idle = this.callbacks.isIdle();
      if (!idle) this.callbacks.onAbort();
      await this.sendTelegramMessage(chatId, idle
        ? "<b>No active turn to cancel.</b>"
        : "<b>Cancellation requested.</b> Already completed work is not undone.");
      this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
      return;
    }

    if (rawText === "/release") {
      await this.sendTelegramMessage(chatId, "<b>Disconnecting Telegram.</b> The session continues. Reconnect from the terminal; this chat cannot reconnect itself.");
      // onRelease stops the poller and closes this ledger.
      this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
      await this.callbacks.onRelease();
      return;
    }

    // Handle explicit /steer command
    if (/^\/steer(?:\s|$)/.test(rawText)) {
      const steerText = rawText.replace(/^\/steer\s*/i, "").trim();
      if (steerText) {
        const attributed = attributeSender(fromId, steerText);
        this.callbacks.onTelegramTurnStart();
        if (this.callbacks.isIdle()) this.callbacks.onUserMessage(attributed);
        else this.callbacks.onSteer(attributed);
        this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
      } else {
        await this.sendTelegramMessage(chatId, "⚠️ <b>Usage:</b> <code>/steer &lt;instruction&gt;</code>");
        this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
      }
      return;
    }

    if (!row.media_json && rawText.startsWith("/")) {
      await this.sendTelegramMessage(chatId, "<b>Unknown command.</b> Use <code>/help</code> for supported commands and argument syntax. Nothing was sent to the session.");
      this.db.run("UPDATE update_ledger SET status = 'REJECTED', error = 'UNKNOWN_COMMAND' WHERE update_id = ?", [row.update_id]);
      return;
    }

    // 5. Inbound text routing to the explicitly leased active session:
    //    idle starts a turn; busy text uses the native steering queue so an
    //    authorized operator message reaches continuous work at the next safe
    //    tool boundary instead of waiting for the session to stop.
    this.callbacks.onTelegramTurnStart();

    let deliveredText = rawText;
    if (typeof row.reply_to_message_id === "number") {
      const contextLines: string[] = [];
      const metaParts: string[] = [`post #${row.reply_to_message_id}`];
      if (replyCorrelation?.requestId) {
        metaParts.push(`task: ${replyCorrelation.requestId}`);
      }
      if (replyCorrelation?.laneId) {
        contextLines.push(`[Concerning lane: ${replyCorrelation.laneId}; last reported state: ${replyCorrelation.laneState ?? "unknown"}. Reply delivered to Main for dispatch, not directly to the lane.]`);
        if (replyCorrelation.laneState === "exited") {
          await this.sendTelegramMessage(chatId,
            `<b>${escapeHtml(replyCorrelation.laneId)} has exited.</b> Your reply is going to Main with the original lane context, not to a dead worker.`);
        }
      }
      if (replyCorrelation?.decisionId) {
        metaParts.push(`decision: ${replyCorrelation.decisionId}`);
      }
      contextLines.push(`[Replying to Telegram ${metaParts.join(" | ")}]`);
      if (row.reply_to_text && row.reply_to_text.trim()) {
        const quote = row.reply_to_text.trim().length > 300
          ? `${row.reply_to_text.trim().slice(0, 300)}…`
          : row.reply_to_text.trim();
        contextLines.push(`[Original post: "${quote}"]`);
      }
      contextLines.push(`[Note: Free-text reply; not automatic approval or authorization.]`);
      deliveredText = `${contextLines.join("\n")}\n\n${rawText}`;
    }

    const hasVisibleBody = Boolean(rawText.replace(/[\s\u2000-\u200F\u2028-\u202F\u205F-\u206F\uFEFF]/g, ""));
    if (!hasVisibleBody && !row.media_json) {
      this.db.run("UPDATE update_ledger SET status = 'REJECTED', error = 'EMPTY_TEXT' WHERE update_id = ?", [
        row.update_id,
      ]);
      return;
    }

    deliveredText = attributeSender(fromId, deliveredText);
    if (this.callbacks.isIdle()) {
      this.callbacks.onUserMessage(deliveredText);
    } else {
      this.callbacks.onSteer(deliveredText);
    }

    this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
  }

  public async sendTelegramPhoto(
    chatId: string,
    file: string,
    caption: string,
    replyMarkup?: Record<string, unknown>,
    defaultRepo = "Bavariance/polysimulator",
  ): Promise<void> {
    const sessionId = this.correlation?.getSessionId();
    const slotId = this.correlation?.getSlotId();
    const form = new FormData();
    form.set("chat_id", chatId);
    if (this.outboundThreadId !== undefined) form.set("message_thread_id", String(this.outboundThreadId));
    form.set("photo", Bun.file(file), path.basename(file));
    const formattedCaption = formatTelegramCaption(redactSecrets(caption), 1024, defaultRepo);
    if (formattedCaption) {
      form.set("caption", formattedCaption);
      form.set("parse_mode", "HTML");
    }
    if (replyMarkup) {
      form.set("reply_markup", JSON.stringify(replyMarkup));
    }
    const response = await fetch(`https://api.telegram.org/bot${this.botToken}/sendPhoto`, {
      method: "POST", body: form, signal: this.abortController.signal,
    });
    const data = await response.json() as TelegramSendMessageResponse;
    if (!response.ok || !data.ok || !data.result) throw new Error("Photo delivery failed");
    if (sessionId && slotId && this.correlation) {
      this.correlation.record({
        botId: this.botId, chatId: String(data.result.chat?.id ?? chatId),
        messageId: data.result.message_id, slotId, sessionId,
        requestId: null, decisionId: null, projectPath: null, createdAt: Date.now() / 1000,
      });
    }
  }

  public async sendMediaGroup(
    chatId: string,
    files: string[],
    caption?: string,
    defaultRepo = "Bavariance/polysimulator",
  ): Promise<void> {
    if (!files || files.length === 0) {
      throw new Error("Media group requires at least one file");
    }
    const targetFiles = files.slice(0, 10);
    if (targetFiles.length === 1) {
      return this.sendTelegramPhoto(chatId, targetFiles[0], caption ?? "", undefined, defaultRepo);
    }

    const sessionId = this.correlation?.getSessionId();
    const slotId = this.correlation?.getSlotId();
    const form = new FormData();
    form.set("chat_id", chatId);
    if (this.outboundThreadId !== undefined) form.set("message_thread_id", String(this.outboundThreadId));

    const formattedCaption = caption
      ? formatTelegramCaption(redactSecrets(caption), 1024, defaultRepo)
      : "";
    const mediaList = targetFiles.map((file, idx) => {
      const attachName = `photo_${idx}`;
      form.set(attachName, Bun.file(file), path.basename(file));
      const entry: Record<string, unknown> = {
        type: "photo",
        media: `attach://${attachName}`,
      };
      if (idx === 0 && formattedCaption) {
        entry.caption = formattedCaption;
        entry.parse_mode = "HTML";
      }
      return entry;
    });

    form.set("media", JSON.stringify(mediaList));

    const response = await fetch(`https://api.telegram.org/bot${this.botToken}/sendMediaGroup`, {
      method: "POST",
      body: form,
      signal: this.abortController.signal,
    });
    const data = (await response.json()) as {
      ok: boolean;
      result?: Array<{ message_id: number; chat?: { id: number } }>;
    };
    if (!response.ok || !data.ok || !Array.isArray(data.result) || data.result.length === 0) {
      throw new Error("Media group delivery failed");
    }

    if (sessionId && slotId && this.correlation) {
      for (const msg of data.result) {
        this.correlation.record({
          botId: this.botId,
          chatId: String(msg.chat?.id ?? chatId),
          messageId: msg.message_id,
          slotId,
          sessionId,
          requestId: null,
          decisionId: null,
          projectPath: null,
          createdAt: Date.now() / 1000,
        });
      }
    }
  }

  public async stop(): Promise<void> {
    this.isRunning = false;
    this.abortController.abort();
    if (this.loopPromise) {
      try {
        await this.loopPromise;
      } catch {}
      this.loopPromise = null;
    }
    try {
      this.db.close();
    } catch {}
  }
}
