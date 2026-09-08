/**
 * poller.ts — Long-polling Telegram Bot API transport & message dispatcher.
 */

import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as path from "node:path";
import { escapeHtml, getTokenFingerprint, redactSecrets } from "./sanitizer";
import { downloadInboundMedia, selectInboundMedia, type InboundMedia } from "./inbound-media";
import type {
  AccessConfig,
  MessageCorrelationBridge,
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
  onHarnessCommand?: (text: string, chatId: string) => Promise<boolean>;
  onDecisionCallback?: (
    decisionId: string,
    choiceId: string,
    context?: string,
  ) => void | Promise<void>;
  /**
   * Reports a ledger write the poller could not complete. Required rather than
   * optional: an ingest failure means inbound Telegram traffic is being dropped and
   * the offset cannot advance, so no caller may silently discard it.
   */
  onLedgerFailure: (message: string) => void;
}

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
};

/**
 * Statuses this poller may still act on. Every other value — including a legacy status
 * it has never written — counts as terminal: such a row is never redriven into a
 * session, so an old update cannot replay into a new one, and never pins the Telegram
 * offset, so it cannot block delivery of current updates either.
 */
const NON_TERMINAL_STATUSES = ["PENDING", "PROCESSING"] as const;
const NON_TERMINAL_STATUS_SQL = NON_TERMINAL_STATUSES.map(status => `'${status}'`).join(", ");

export class TelegramPoller {
  private botToken: string;
  private botId: string;
  private stateDir: string;
  private accessConfig: AccessConfig;
  private callbacks: PollerCallbacks;
  private correlation: MessageCorrelationBridge | null;
  private abortController: AbortController;
  private db: Database;
  private isRunning = false;
  private primaryChatId: string | null = null;
  private pendingDrain: Promise<void> | null = null;

  constructor(
    botToken: string,
    stateDir: string,
    accessConfig: AccessConfig,
    callbacks: PollerCallbacks,
    correlation: MessageCorrelationBridge | null = null,
  ) {
    this.botToken = botToken;
    this.botId = getTokenFingerprint(botToken).botId;
    this.stateDir = stateDir;
    this.accessConfig = accessConfig;
    this.callbacks = callbacks;
    this.correlation = correlation;
    this.abortController = new AbortController();

    if (!fs.existsSync(stateDir)) {
      fs.mkdirSync(stateDir, { recursive: true });
    }

    const dbPath = path.join(stateDir, "veyyon_bridge_state.db");
    this.db = new Database(dbPath);
    this.initLedger();
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
    if (this.primaryChatId) return this.primaryChatId;
    if (this.accessConfig.allowFrom.length > 0) {
      return this.accessConfig.allowFrom[0];
    }
    return null;
  }

  public async sendTelegramMessage(
    chatId: string | number,
    text: string,
    parseMode: "HTML" | "Markdown" | undefined = "HTML",
    replyMarkup?: Record<string, unknown>,
    correlationMeta?: {
      requestId?: string | null;
      decisionId?: string | null;
      projectPath?: string | null;
    },
  ): Promise<TelegramSendMessageResponse | null> {
    const sanitized = redactSecrets(text);
    if (!sanitized.trim()) return null;

    try {
      const body: Record<string, unknown> = {
        chat_id: chatId,
        text: sanitized,
      };
      if (parseMode) {
        body.parse_mode = parseMode;
      }
      if (replyMarkup) {
        body.reply_markup = replyMarkup;
      }

      // Resolve the owning session BEFORE the roundtrip. An in-TUI session switch
      // during the await would otherwise index this outbound text against whichever
      // session arrived later, and a reply to it would be delivered into that
      // unrelated session instead of refused.
      const boundSlotId = this.correlation?.getSlotId() ?? null;
      const boundSessionId = this.correlation?.getSessionId() ?? null;

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
    parseMode: "HTML" | "Markdown" | undefined = "HTML",
  ): Promise<TelegramSendMessageResponse | null> {
    const sanitized = redactSecrets(text);
    if (!sanitized.trim()) return null;

    try {
      const body: Record<string, unknown> = {
        chat_id: chatId,
        message_id: messageId,
        text: sanitized,
        reply_markup: { inline_keyboard: [] },
      };
      if (parseMode) {
        body.parse_mode = parseMode;
      }

      const response = await fetch(
        `https://api.telegram.org/bot${this.botToken}/editMessageText`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: this.abortController.signal,
        },
      );

      const data = (await response.json()) as TelegramSendMessageResponse;
      return data;
    } catch {
      return null;
    }
  }
  public async clearCallbackButtons(chatId: string, messageId: number): Promise<boolean> {
    try {
      const response = await fetch(`https://api.telegram.org/bot${this.botToken}/editMessageReplyMarkup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chatId, message_id: messageId, reply_markup: { inline_keyboard: [] } }),
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

    // 1. Redrive any pending updates from previous crashed runs safely
    await this.redrivePendingUpdates();

    let offset = this.getNextContiguousOffset();

    while (this.isRunning && !this.abortController.signal.aborted) {
      try {
        const allowedUpdates = encodeURIComponent(JSON.stringify(["message", "callback_query"]));
        const url = `https://api.telegram.org/bot${this.botToken}/getUpdates?offset=${offset}&timeout=20&allowed_updates=${allowedUpdates}`;
        const res = await fetch(url, { signal: this.abortController.signal });

        if (!res.ok) {
          await Bun.sleep(3000);
          continue;
        }

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
        this.db.run("UPDATE update_ledger SET status = 'REJECTED', error = ? WHERE update_id = ?", [
          `PROCESSING_FAILED: ${detail}`,
          row.update_id,
        ]);
        this.callbacks.onLedgerFailure(`update ${row.update_id} could not be processed: ${detail}`);
        if (row.is_callback) {
          await this.sendTelegramMessage(row.chat_id, `Choice could not be delivered: ${escapeHtml(detail)}. Reply to the original message with your choice.`);
        }
      }
    }
  }

  private async processLedgerRow(row: LedgerRow): Promise<void> {
    // Mark as in-flight PROCESSING
    this.db.run("UPDATE update_ledger SET status = 'PROCESSING' WHERE update_id = ?", [row.update_id]);

    if ((!row.text || !row.text.trim()) && !row.media_json) {
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

    // 2. Allowlist authorization check: private DM only where chatId === fromId
    const isAllowed = this.accessConfig.allowFrom.includes(fromId) && chatId === fromId;
    if (!isAllowed) {
      this.db.run(
        "UPDATE update_ledger SET status = 'REJECTED', error = 'UNAUTHORIZED' WHERE update_id = ?",
        [row.update_id],
      );
      return;
    }

    this.primaryChatId = chatId;
    let rawText = row.text?.trim() ?? "";

    // 3. Callback query handling for interactive decision buttons
    if (row.is_callback === 1 || Boolean(row.callback_query_id)) {
      const callbackToken = row.callback_data || row.text || "";
      const cbQueryId = row.callback_query_id || "";

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
    if (!row.media_json && await this.callbacks.onHarnessCommand?.(rawText, chatId)) {
      this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
      return;
    }
    if (this.correlation?.getSessionId() !== inboundSessionId) {
      throw new Error("Session changed during message routing; resend to the intended session");
    }
    if (rawText === "/help" || rawText === "/start") {
      const helpMsg = [
        "🤖 <b>Veyyon Telegram Control Plane</b>",
        "",
        "<b>Commands:</b>",
        "/status — Inspect active session, model, turn state, and slot info",
        "/agents — List Herdr agents and this session",
        "/prompt backend:id text — Prompt the named target",
        "/shot backend:id — Latest session PNG",
        "/usage — Allowances and reset windows, including Spark",
        "/steer &lt;text&gt; — Steer active LLM generation with immediate instruction",
        "/cancel — Abort active turn or tool execution",
        "/release — Release bot lease and disconnect Telegram",
        "/help — Show this help message",
        "",
        "<i>Plain text sent while idle begins a new turn in the active Veyyon session.</i>",
        "<i>Plain text sent while busy steers the active turn at the next safe tool boundary.</i>",
      ].join("\n");
      await this.sendTelegramMessage(chatId, helpMsg);
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
      this.callbacks.onAbort();
      await this.sendTelegramMessage(chatId, "🛑 <b>Aborted current turn.</b>");
      this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
      return;
    }

    if (rawText === "/release") {
      await this.sendTelegramMessage(chatId, "🔓 <b>Releasing Telegram bot lease...</b>");
      await this.callbacks.onRelease();
      this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
      return;
    }

    // Handle explicit /steer command
    if (rawText.startsWith("/steer")) {
      const steerText = rawText.replace(/^\/steer\s*/i, "").trim();
      if (steerText) {
        this.callbacks.onTelegramTurnStart();
        this.callbacks.onSteer(steerText);
        this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
      } else {
        await this.sendTelegramMessage(chatId, "⚠️ <b>Usage:</b> <code>/steer &lt;instruction&gt;</code>");
        this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
      }
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

    if (this.callbacks.isIdle()) {
      this.callbacks.onUserMessage(deliveredText);
    } else {
      this.callbacks.onSteer(deliveredText);
    }

    this.db.run("UPDATE update_ledger SET status = 'COMPLETED' WHERE update_id = ?", [row.update_id]);
  }

  public async sendTelegramPhoto(chatId: string, file: string, caption: string): Promise<void> {
    const sessionId = this.correlation?.getSessionId();
    const slotId = this.correlation?.getSlotId();
    const form = new FormData();
    form.set("chat_id", chatId);
    form.set("photo", Bun.file(file), path.basename(file));
    form.set("caption", redactSecrets(caption));
    form.set("parse_mode", "HTML");
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

  public stop(): void {
    this.isRunning = false;
    this.abortController.abort();
    try {
      this.db.close();
    } catch {}
  }
}
