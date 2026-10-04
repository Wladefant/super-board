/**
 * questions-topic.ts — the one forum topic that lists every open operator question.
 *
 * Invariant: the topic holds exactly one message per OPEN question, in creation order, each with its
 * buttons, plus one pinned index and at most one digest ping. Closing a question anywhere deletes its
 * copy here. The decision store is the truth; every Telegram message id kept here is a cache, so
 * `reconcile` can rebuild the topic from the store at any time and a crash loses nothing.
 */

import { escapeHtml } from "../extension/sanitizer";
import { isClosed, type Question, type QuestionStore } from "../src/operator-questions";
import type { DaemonStore } from "./store";
import { getDaemonSecret } from "./lane-panel";
import type { BotPoolCoordinator } from "../extension/coordinator";
export type TopicOutcome = "ok" | "gone" | "error";

export interface QuestionsTransport {
  createTopic(name: string): Promise<number>;
  /** `gone` means the topic itself no longer exists. `owner` makes replies route to the question's session. */
  send(
    threadId: number,
    text: string,
    markup: Record<string, unknown> | undefined,
    owner?: { sessionId: string; decisionId: string },
  ): Promise<{ messageId: number } | "gone" | "error">;
  edit(messageId: number, text: string, markup?: Record<string, unknown>): Promise<TopicOutcome>;
  remove(messageId: number): Promise<TopicOutcome>;
  pin(messageId: number): Promise<void>;
}

export interface QuestionsTopicOptions {
  chatId: string;
  /** Distinguishes the keys of two forum slots sharing one daemon database. */
  slotId: string;
  store: DaemonStore;
  ledger: Pick<QuestionStore, "list" | "cache" | "cardFor">;
  transport: QuestionsTransport;
  /** Rewrites the copy in the question's own topic to its verdict. */
  finalize: (question: Question) => Promise<void>;
  session: (sessionId: string) => { name: string; ended: boolean };
  /** Runs before every pass, for example to refresh which sessions are still running. */
  prepare?: () => Promise<void>;
  now?: () => number;
  log?: (message: string) => void;
  digestIntervalMs?: number;
  cardRefreshMs?: number;
  coordinator?: BotPoolCoordinator;
}

interface InlineKeyboardButton {
  text: string;
  callback_data?: string;
}

interface InlineKeyboardMarkup {
  inline_keyboard: InlineKeyboardButton[][];
}

function isInlineKeyboardMarkup(value: unknown): value is InlineKeyboardMarkup {
  if (!value || typeof value !== "object" || !("inline_keyboard" in value)) {
    return false;
  }
  return Array.isArray(value.inline_keyboard);
}

interface DigestState { at: number; seen: string[]; messageId: number | null }

const order = (left: Question, right: Question): number =>
  (left.transport.created_ts ?? Date.parse(left.created_at ?? "") / 1000) -
    (right.transport.created_ts ?? Date.parse(right.created_at ?? "") / 1000) ||
  left.decision_id.localeCompare(right.decision_id);

export class QuestionsTopic {
  private readonly now: () => number;
  private chain: Promise<void> = Promise.resolve();
  private timer: Timer | undefined;
  private watcher: Timer | undefined;

  constructor(private readonly options: QuestionsTopicOptions) {
    this.now = options.now ?? Date.now;
  }

  private key(name: string): string {
    return `questions:${this.options.slotId}:${this.options.chatId}:${name}`;
  }

  private log(message: string): void {
    this.options.log?.(`[Questions topic] ${message}`);
  }

  /** Thread id of the Questions topic, or null before it exists. */
  threadId(): number | null {
    const value = this.options.store.getKv(this.key("thread"));
    return value === null ? null : Number(value);
  }

  /**
   * Runs one reconcile after the burst of changes that triggered it. `probe` also checks that every
   * copy still exists, which costs one Telegram call per open question.
   */
  request(delayMs = 1_500, probe = false): void {
    if (this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = undefined;
      void this.run(probe).catch(error => this.log(`reconcile failed: ${String(error)}`));
    }, delayMs);
    this.timer.unref?.();
  }

  /**
   * Watches the decision store, which sessions write from their own processes. A change reconciles within
   * a few seconds; every half hour the topic is also checked for copies deleted by hand.
   */
  start(decisionsPath: string, pollMs = 5_000, healEveryMs = 1_800_000): void {
    if (this.watcher) return;
    let seen = 0;
    let lastPass = this.now();
    this.request(0, true);
    this.watcher = setInterval(() => {
      const stamp = Bun.file(decisionsPath).lastModified;
      const heal = this.now() - lastPass >= healEveryMs;
      if (stamp !== seen || heal) {
        seen = stamp;
        if (heal) lastPass = this.now();
        this.request(0, heal);
      }
    }, pollMs);
    this.watcher.unref?.();
  }

  stop(): void {
    clearTimeout(this.timer);
    clearInterval(this.watcher);
    this.timer = undefined;
    this.watcher = undefined;
  }

  /** Brings the topic in line with the store, probing every copy. Serialized and idempotent. */
  reconcile(): Promise<void> {
    return this.run(true);
  }

  private run(probe: boolean): Promise<void> {
    const run = this.chain.then(() => this.pass(true, probe));
    this.chain = run.catch(() => undefined);
    return run;
  }

  /**
   * Signs reply markup with HMAC-signed Answer tokens bound to session, chat, operator and expiry.
   */
  private signMarkup(question: Question, markup?: Record<string, unknown>): Record<string, unknown> | undefined {
    if (!this.options.coordinator || !isInlineKeyboardMarkup(markup)) {
      return markup;
    }
    const secret = getDaemonSecret(this.options.store, this.options.slotId);
    const ttlSeconds = (this.options.cardRefreshMs ?? 24 * 3_600_000) / 1000;
    const now = this.now() / 1000;
    const signedRows = markup.inline_keyboard.map(row => row.map(btn => {
      if (!btn.callback_data) return btn;
      const choiceId = btn.callback_data;
      const record = this.options.coordinator!.issueDecisionCallback({
        decisionId: question.decision_id,
        choiceId,
        sessionId: question.transport.session_id,
        chatId: this.options.chatId,
        userId: question.transport.user_id,
        secret,
        ttlSeconds,
        now,
      });
      return { ...btn, callback_data: record.callbackToken };
    }));
    return { ...markup, inline_keyboard: signedRows };
  }

  /** Telegram cannot list a topic, so a copy deleted by hand shows only when an edit finds it missing. */
  private async probeCopies(open: Question[]): Promise<void> {
    for (const question of open) {
      const messageId = question.transport.topic_message_id;
      if (!messageId) continue;
      let fresh;
      try { fresh = await this.options.ledger.cardFor(question.decision_id); } catch { continue; }
      const outcome = await this.options.transport.edit(messageId, this.copyText(fresh.question, fresh.card.text), this.signMarkup(fresh.question, fresh.card.reply_markup));
      if (outcome === "gone") await this.options.ledger.cache(question.decision_id, { topic_message_id: null });
    }
  }

  /** Redraws one open question's copy, for example after an option was selected. */
  async refresh(id: string): Promise<void> {
    const messageId = (await this.options.ledger.list()).find(q => q.decision_id === id)?.transport.topic_message_id;
    if (!messageId) return this.request(0);
    try {
      const { question, card } = await this.options.ledger.cardFor(id);
      const outcome = await this.options.transport.edit(messageId, this.copyText(question, card.text), this.signMarkup(question, card.reply_markup));
      if (outcome === "gone") await this.options.ledger.cache(id, { topic_message_id: null });
      if (outcome !== "ok") this.request(0);
    } catch { this.request(0); }
  }

  private async pass(mayRestart: boolean, probe: boolean): Promise<void> {
    const { ledger, transport } = this.options;
    await this.options.prepare?.();
    if (probe) await this.probeCopies((await ledger.list()).filter(q => q.status === "pending"));
    await this.baselineLegacy();
    const all = await ledger.list();
    const open = all.filter(q => q.status === "pending").sort(order);
    const thread = await this.ensureThread();

    for (const question of all.filter(isClosed)) {
      const copy = question.transport.topic_message_id;
      if (copy) {
        const outcome = await transport.remove(copy);
        if (outcome === "error") return this.retry("could not delete a closed question's copy");
        await ledger.cache(question.decision_id, { topic_message_id: null });
      }
      if (!question.transport.session_finalized) await this.options.finalize(question);
    }

    // The valid prefix keeps its messages; everything from the first gap or inversion is reposted.
    let valid = 0;
    let previous = 0;
    while (valid < open.length) {
      const id = open[valid].transport.topic_message_id;
      if (!id || id <= previous) break;
      previous = id;
      valid++;
    }
    const tail = open.slice(valid);
    for (const question of tail) {
      const stale = question.transport.topic_message_id;
      if (!stale) continue;
      if (await transport.remove(stale) === "error") return this.retry("could not delete an out-of-order copy");
      await ledger.cache(question.decision_id, { topic_message_id: null });
    }
    for (const stale of tail) {
      let fresh;
      try { fresh = await ledger.cardFor(stale.decision_id); } catch { continue; } // closed meanwhile
      const sent = await transport.send(thread, this.copyText(fresh.question, fresh.card.text), this.signMarkup(fresh.question, fresh.card.reply_markup),
        { sessionId: fresh.question.transport.session_id, decisionId: fresh.question.decision_id });
      if (sent === "gone") return this.topicLost(mayRestart);
      if (sent === "error") return this.retry("could not post a question");
      await ledger.cache(stale.decision_id, { topic_message_id: sent.messageId, topic_card_at: this.now() });
    }

    // Buttons are valid for 24 h; a long-open question gets fresh ones.
    for (const question of open.slice(0, valid)) {
      const issuedAt = question.transport.topic_card_at ?? 0;
      if (this.now() - issuedAt < (this.options.cardRefreshMs ?? 12 * 3_600_000)) continue;
      await this.refresh(question.decision_id);
      await ledger.cache(question.decision_id, { topic_card_at: this.now() });
    }

    const current = (await ledger.list()).filter(q => q.status === "pending").sort(order);
    if (await this.updateIndex(thread, current) === "gone") return this.topicLost(mayRestart);
    await this.digest(thread, current);
  }

  /**
   * Questions closed before the Questions topic existed keep their session copy as it is: rewriting
   * hundreds of old messages on the first start would stall the outbound queue ahead of the open ones.
   */
  private async baselineLegacy(): Promise<void> {
    const { store, ledger } = this.options;
    const key = this.key("baseline");
    if (store.getKv(key) !== null) return;
    for (const question of (await ledger.list()).filter(isClosed)) {
      if (!question.transport.session_finalized) await ledger.cache(question.decision_id, { session_finalized: true });
    }
    store.setKv(key, "1");
  }

  private retry(reason: string): void {
    this.log(`${reason}; will retry`);
    this.request(10_000, false);
  }

  /** The topic was deleted by hand: forget every cached id and rebuild from the store. */
  private async topicLost(mayRestart: boolean): Promise<void> {
    this.log("topic is gone; rebuilding it from the store");
    for (const name of ["thread", "index_id", "index_hash", "digest"]) this.options.store.deleteKv(this.key(name));
    for (const question of await this.options.ledger.list()) {
      if (question.transport.topic_message_id) await this.options.ledger.cache(question.decision_id, { topic_message_id: null });
    }
    if (mayRestart) await this.pass(false, false);
  }

  private async ensureThread(): Promise<number> {
    const known = this.threadId();
    if (known !== null) return known;
    const created = await this.options.transport.createTopic("Questions");
    this.options.store.setKv(this.key("thread"), String(created));
    for (const name of ["index_id", "index_hash", "digest"]) this.options.store.deleteKv(this.key(name));
    this.log(`created the Questions topic #${created}`);
    return created;
  }

  private link(thread: number, messageId: number): string {
    return `https://t.me/c/${this.options.chatId.replace(/^-100/, "")}/${thread}/${messageId}`;
  }

  private copyText(question: Question, cardText: string): string {
    const session = this.options.session(question.transport.session_id);
    const label = `<b>Session:</b> <code>${escapeHtml(session.name)}</code>${session.ended ? " — <i>session ended</i>" : ""}`;
    return `${label}\n${cardText}`;
  }

  private async updateIndex(thread: number, open: Question[]): Promise<TopicOutcome> {
    const { store, transport } = this.options;
    const lines = open.map((question, position) => {
      const messageId = question.transport.topic_message_id;
      const title = escapeHtml(question.question.length > 80 ? `${question.question.slice(0, 77)}...` : question.question);
      const session = this.options.session(question.transport.session_id);
      const text = messageId ? `<a href="${this.link(thread, messageId)}">${title}</a>` : title;
      return `${position + 1}. ${text} — <code>${escapeHtml(session.name)}</code>${session.ended ? " (session ended)" : ""}`;
    });
    const text = open.length
      ? `<b>Open questions (${open.length})</b>\n${lines.join("\n")}`
      : "<b>No open questions.</b>";
    if (store.getKv(this.key("index_hash")) === text) return "ok";
    const existing = store.getKv(this.key("index_id"));
    if (existing) {
      const outcome = await transport.edit(Number(existing), text);
      if (outcome === "ok") { store.setKv(this.key("index_hash"), text); return "ok"; }
      if (outcome === "error") { this.request(10_000); return "ok"; }
      store.deleteKv(this.key("index_id"));
    }
    const sent = await transport.send(thread, text, undefined);
    if (sent === "gone") return "gone";
    if (sent === "error") { this.request(10_000); return "ok"; }
    await transport.pin(sent.messageId);
    store.setKv(this.key("index_id"), String(sent.messageId));
    store.setKv(this.key("index_hash"), text);
    return "ok";
  }

  /**
   * The only reminder: one short ping, and only when questions arrived since the last one and at least
   * `digestIntervalMs` passed. The ping replaces its predecessor, so the topic never accumulates pings.
   */
  private async digest(thread: number, open: Question[]): Promise<void> {
    const { store, transport } = this.options;
    const raw = store.getKv(this.key("digest"));
    const now = this.now();
    // The first run only records a baseline: migrated questions are already in front of the operator.
    let state: DigestState = raw ? JSON.parse(raw) : { at: now, seen: open.map(q => q.decision_id), messageId: null };
    if (open.length === 0) {
      if (state.messageId) await transport.remove(state.messageId);
      state = { at: state.at, seen: [], messageId: null };
    } else {
      const ids = open.map(q => q.decision_id);
      const fresh = ids.filter(id => !state.seen.includes(id));
      if (fresh.length && now - state.at >= (this.options.digestIntervalMs ?? 2 * 3_600_000)) {
        if (state.messageId) await transport.remove(state.messageId);
        const sent = await transport.send(thread,
          `🔔 <b>${open.length} open question${open.length === 1 ? "" : "s"}</b>, ${fresh.length} new since the last ping. See the pinned list.`,
          undefined);
        state = { at: now, seen: ids, messageId: sent !== "gone" && sent !== "error" ? sent.messageId : null };
      } else {
        state = { ...state, seen: state.seen.filter(id => ids.includes(id)) };
      }
    }
    store.setKv(this.key("digest"), JSON.stringify(state));
  }
}
