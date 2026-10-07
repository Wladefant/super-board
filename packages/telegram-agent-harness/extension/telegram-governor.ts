/**
 * The one gate every outbound Bot API call passes through.
 *
 * Telegram documents about 1 message per second into one chat and 20 messages per minute into one group
 * (https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this). Exceeding either
 * earns a 429 with `retry_after`. This governor keeps one token budget per chat, shared by every sender
 * in the process (poller, panels, tools, daemon), and honours `retry_after` for the whole bot.
 *
 * - Spacing: at least `chatIntervalMs` between two sends into the same chat.
 * - Group window: at most `groupLimit` sends per `windowMs` into a group. `panelReserve` of them can only
 *   be spent by panels, so a chatty session cannot starve the dashboard.
 * - 429: the chat (or, for a call bound to no chat, the whole bot) is blocked until `retry_after` has passed.
 *   The call is retried once after that wait and the second answer is returned as is. A block longer than
 *   `maxRetryWaitMs` is never slept through: the first call gets the 429 back and queued calls fail with
 *   {@link TelegramRateLimitedError}.
 * - Queues: panels, replies to the operator (`priority`) and ordinary messages queue apart, so a message held by its group share never holds up a
 *   panel. Each chat queues at most `maxQueuedPerChat`; a full queue drops its oldest panel edit for a newer
 *   panel call and refuses a new message with {@link TelegramQueueFullError}.
 * - Coalescing: an edit of the same message that is still queued is replaced by the newer edit, and both
 *   callers get the answer to the one request that is sent.
 *
 * Calls go out through {@link telegramFetch}, so they keep IPv4-first connects.
 */
import { type BudgetConfig, isGroupChat, recordRateLimit, reserve, sharedBudgetFor, type SharedBudget } from "./telegram-budget";
import { telegramFetch, type TelegramFetchDeps } from "./telegram-fetch";

export { isGroupChat };

export type GovernorKind = "message" | "panel" | "other" | "inbound";

export interface GovernedRequest {
  /** Chat the call writes into. Omit for calls that are not bound to one (getFile, setMyCommands). */
  chatId?: string | number;
  /** `panel` draws on the reserved group budget. `inbound` (getUpdates, getMe) is never delayed by the budget. */
  kind?: GovernorKind;
  /** Calls with the same key that are still queued collapse into the newest one. */
  coalesceKey?: string;
  /** A reply to the operator. It queues apart from bulk messages, so it is never stuck behind them (the budget still counts it). */
  priority?: boolean;
  /** Per-attempt timeout. It starts when the request is sent, so queue time does not eat it. */
  timeoutMs?: number;
  /** One line per wait, rate limit and coalesce. Never carries a token. */
  log?: (message: string) => void;
}

export interface GovernorOptions {
  chatIntervalMs?: number;
  groupLimit?: number;
  windowMs?: number;
  panelReserve?: number;
  maxRetryWaitMs?: number;
  /** Queued, not-yet-sent calls one chat may hold per lane. More are refused so a stuck chat cannot grow memory. */
  maxQueuedPerChat?: number;
  now?: () => number;
  /** Budget file shared with the other processes of this bot. Omit for a process-local budget. */
  shared?: SharedBudget;
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

export interface GovernorSnapshot {
  blockedForMs: number;
  chats: Array<{ chat: string; sendsInWindow: number; queued: number; blockedForMs: number }>;
}

type Send = (signal: AbortSignal | undefined) => Promise<Response>;

interface Waiter {
  resolve: (response: Response) => void;
  reject: (error: unknown) => void;
}

interface Entry {
  send: Send;
  signal: AbortSignal | undefined;
  kind: GovernorKind;
  coalesceKey: string | undefined;
  timeoutMs: number | undefined;
  log: ((message: string) => void) | undefined;
  waiters: Waiter[];
  /** True once a request has left for Telegram; a started call can no longer absorb a newer edit. */
  started: boolean;
  /** True once the entry was dropped from a full queue; the chain skips it. */
  dropped: boolean;
  /** A reply to the operator: queued in its own lane and served before ordinary messages. */
  priority: boolean;
}

interface ChatState {
  /** Panels and ordinary messages queue separately, so a message waiting on its group share never holds up a panel. */
  tails: Record<"message" | "panel" | "urgent", Promise<void>>;
  /** Entries queued behind a tail and not yet picked up, oldest first. */
  waiting: Entry[];
  sends: Array<{ at: number; kind: GovernorKind }>;
  lastAt: number;
  /** A 429 on a chat-bound call limits that chat only. */
  blockedUntil: number;
  pending: Map<string, Entry>;
  /** Priority entries currently trying to book a send; ordinary messages stand aside for them. */
  urgentActive: number;
  /** Serializes booking in this chat; see {@link TelegramGovernor.exclusive}. */
  booking: Promise<void>;
}

const defaultSleep = (ms: number, signal?: AbortSignal): Promise<void> => {
  const { promise, resolve, reject } = Promise.withResolvers<void>();
  if (signal?.aborted) {
    reject(signal.reason);
    return promise;
  }
  const onAbort = () => {
    clearTimeout(timer);
    reject(signal?.reason);
  };
  const timer = setTimeout(() => {
    signal?.removeEventListener("abort", onAbort);
    resolve();
  }, ms);
  // A pending wait must not keep the process alive.
  timer.unref();
  signal?.addEventListener("abort", onAbort, { once: true });
  return promise;
};

function parseRetryAfter(body: unknown, header: string | null): number | undefined {
  if (body && typeof body === "object" && "parameters" in body) {
    const parameters = body.parameters;
    if (parameters && typeof parameters === "object" && "retry_after" in parameters && typeof parameters.retry_after === "number") {
      return parameters.retry_after;
    }
  }
  const fromHeader = header === null ? Number.NaN : Number(header);
  return Number.isFinite(fromHeader) ? fromHeader : undefined;
}

/** Raised instead of sleeping through a rate limit longer than the inline limit. */
export class TelegramRateLimitedError extends Error {
  constructor(public readonly retryAfterMs: number) {
    super(`Telegram rate limit: blocked for another ${Math.ceil(retryAfterMs / 1000)} s; the call was not sent.`);
    this.name = "TelegramRateLimitedError";
  }
}

/** Raised when a chat's queue is full, or when a panel edit is dropped to make room for a newer one. */
export class TelegramQueueFullError extends Error {
  constructor(chat: string, dropped: boolean) {
    super(dropped
      ? `Telegram outbound queue for chat ${chat} was full; an older panel edit was dropped for a newer one.`
      : `Telegram outbound queue for chat ${chat} is full; the call was not sent.`);
    this.name = "TelegramQueueFullError";
  }
}

export class TelegramGovernor {
  private readonly chatIntervalMs: number;
  private readonly groupLimit: number;
  private readonly windowMs: number;
  private readonly panelReserve: number;
  private readonly maxRetryWaitMs: number;
  private readonly maxQueuedPerChat: number;
  private readonly now: () => number;
  private readonly sleep: (ms: number, signal?: AbortSignal) => Promise<void>;
  private readonly shared: SharedBudget | undefined;
  /** Set by a 429 on a call that is not bound to a chat. */
  private blockedUntil = 0;
  private readonly chats = new Map<string, ChatState>();

  constructor(options: GovernorOptions = {}) {
    this.chatIntervalMs = options.chatIntervalMs ?? 1000;
    this.groupLimit = options.groupLimit ?? 20;
    this.windowMs = options.windowMs ?? 60_000;
    this.panelReserve = options.panelReserve ?? 4;
    this.maxRetryWaitMs = options.maxRetryWaitMs ?? 60_000;
    this.maxQueuedPerChat = options.maxQueuedPerChat ?? 200;
    this.now = options.now ?? Date.now;
    this.sleep = options.sleep ?? defaultSleep;
    this.shared = options.shared;
  }

  public snapshot(): GovernorSnapshot {
    const now = this.now();
    return {
      blockedForMs: Math.max(0, this.blockedUntil - now),
      chats: [...this.chats].map(([chat, state]) => ({
        chat,
        sendsInWindow: state.sends.filter((s) => now - s.at < this.windowMs).length,
        queued: state.waiting.length,
        blockedForMs: Math.max(0, state.blockedUntil - now),
      })),
    };
  }

  /**
   * Runs `send` once the budget allows it. Resolves with the final response, after at most one 429 retry.
   * Rejects with {@link TelegramRateLimitedError} when the wait would exceed the inline limit and with
   * {@link TelegramQueueFullError} when the chat's queue is full.
   */
  public schedule(request: GovernedRequest, send: Send, signal?: AbortSignal): Promise<Response> {
    const kind = request.kind ?? (request.chatId === undefined ? "other" : "message");
    const entry: Entry = {
      send, signal, kind, coalesceKey: request.coalesceKey, timeoutMs: request.timeoutMs, log: request.log,
      waiters: [], started: false, dropped: false, priority: request.priority === true && kind !== "panel",
    };
    if (request.chatId === undefined || kind === "inbound") return this.run(entry, undefined, undefined);

    const chat = String(request.chatId);
    const state = this.chatState(chat);
    if (request.coalesceKey) {
      const queued = state.pending.get(request.coalesceKey);
      if (queued && !queued.started) {
        queued.send = send;
        queued.signal = signal;
        request.log?.(`telegram governor: coalesced ${request.coalesceKey} into the queued edit`);
        const waiter = Promise.withResolvers<Response>();
        queued.waiters.push(waiter);
        return waiter.promise;
      }
    }
    if (state.waiting.length >= this.maxQueuedPerChat && !this.makeRoom(state, chat, entry)) {
      request.log?.(`telegram governor: chat ${chat} queue full (${state.waiting.length}), ${kind} call refused`);
      return Promise.reject(new TelegramQueueFullError(chat, false));
    }
    const first = Promise.withResolvers<Response>();
    entry.waiters.push(first);
    if (request.coalesceKey) state.pending.set(request.coalesceKey, entry);
    state.waiting.push(entry);
    const lane = kind === "panel" ? "panel" : request.priority ? "urgent" : "message";
    state.tails[lane] = state.tails[lane].then(async () => {
      if (entry.dropped) return;
      state.waiting.splice(state.waiting.indexOf(entry), 1);
      try {
        if (entry.priority) state.urgentActive++;
        const response = await this.run(entry, state, chat);
        entry.waiters.forEach((waiter, index) => waiter.resolve(index === 0 ? response : response.clone()));
      } catch (error) {
        for (const waiter of entry.waiters) waiter.reject(error);
      } finally {
        if (entry.priority) state.urgentActive--;
        if (entry.coalesceKey && state.pending.get(entry.coalesceKey) === entry) state.pending.delete(entry.coalesceKey);
      }
    });
    return first.promise;
  }

  /** A full queue drops its oldest queued panel edit for a newer panel call. Messages never displace anything. */
  private makeRoom(state: ChatState, chat: string, incoming: Entry): boolean {
    if (incoming.kind !== "panel") return false;
    const victim = state.waiting.find((e) => e.kind === "panel" && !e.started);
    if (!victim) return false;
    victim.dropped = true;
    state.waiting.splice(state.waiting.indexOf(victim), 1);
    if (victim.coalesceKey && state.pending.get(victim.coalesceKey) === victim) state.pending.delete(victim.coalesceKey);
    for (const waiter of victim.waiters) waiter.reject(new TelegramQueueFullError(chat, true));
    incoming.log?.(`telegram governor: chat ${chat} queue full, dropped the oldest queued panel edit`);
    return true;
  }

  private chatState(chat: string): ChatState {
    let state = this.chats.get(chat);
    if (!state) {
      state = {
        tails: { message: Promise.resolve(), panel: Promise.resolve(), urgent: Promise.resolve() },
        waiting: [], sends: [], lastAt: Number.NEGATIVE_INFINITY, blockedUntil: 0, pending: new Map(), urgentActive: 0,
        booking: Promise.resolve(),
      };
      this.chats.set(chat, state);
    }
    return state;
  }

  /** Blocks until the budget lets one send of `kind` into `chat` go out, then books it. */
  private async acquire(entry: Entry, state: ChatState, chat: string): Promise<void> {
    for (;;) {
      const waitMs = await this.exclusive(state, () => this.tryBook(entry, state, chat));
      if (waitMs === 0) return;
      await this.sleep(waitMs, entry.signal);
    }
  }

  /**
   * Runs `fn` alone among the senders of one chat. The message, panel and priority lanes all book here: without
   * this, two lanes that wake together both pass the one-second spacing check while the budget file is awaited,
   * book the same slot and draw a 429 from Telegram.
   */
  private async exclusive<T>(state: ChatState, fn: () => Promise<T>): Promise<T> {
    const previous = state.booking;
    const { promise, resolve } = Promise.withResolvers<void>();
    state.booking = promise;
    await previous;
    try {
      return await fn();
    } finally {
      resolve();
    }
  }

  /** Books one send if the budget allows it (returns 0), else returns how long to wait before asking again. */
  private async tryBook(entry: Entry, state: ChatState, chat: string): Promise<number> {
    const now = this.now();
    state.sends = state.sends.filter((s) => now - s.at < this.windowMs);
    const blocked = Math.max(this.blockedUntil, state.blockedUntil);
    if (blocked - now > this.maxRetryWaitMs) throw new TelegramRateLimitedError(blocked - now);
    const spaced = state.lastAt + this.chatIntervalMs;
    let readyAt = Math.max(blocked, spaced);
    let reason = blocked > spaced ? "retry_after" : "chat interval";
    if (isGroupChat(chat)) {
      const limit = entry.kind === "panel" ? this.groupLimit : this.groupLimit - this.panelReserve;
      const counted = entry.kind === "panel" ? state.sends : state.sends.filter((s) => s.kind !== "panel");
      if (counted.length >= limit) {
        const frees = counted[counted.length - limit].at + this.windowMs;
        if (frees > readyAt) {
          readyAt = frees;
          reason = "group window";
        }
      }
    }
    // A reply to the operator is waiting in this chat: ordinary messages let it take the next slot.
    if (!entry.priority && entry.kind !== "panel" && state.urgentActive > 0) return Math.max(readyAt - now, 50);
    if (readyAt <= now) {
      // The local budget allows it; the file also counts the other processes' sends to this chat.
      const claim = await this.shared?.update((ledger) => reserve(ledger, chat, entry.kind, now, this.config()), now);
      if (claim && claim.blockedMs > this.maxRetryWaitMs) throw new TelegramRateLimitedError(claim.blockedMs);
      if (claim && claim.waitMs > 0) {
        entry.log?.(`telegram governor: chat ${chat} ${entry.kind} waits ${claim.waitMs} ms (${claim.reason}, shared with other processes)`);
        return claim.waitMs;
      }
      state.sends.push({ at: now, kind: entry.kind });
      state.lastAt = now;
      return 0;
    }
    // The routine one-second spacing is not worth a line; a longer wait means a budget or a rate limit.
    if (reason !== "chat interval") entry.log?.(`telegram governor: chat ${chat} ${entry.kind} waits ${readyAt - now} ms (${reason})`);
    return readyAt - now;
  }

  /** Chat-less calls only honour a bot-wide block. getUpdates and downloads are not rate limited by send budgets. */
  private async waitUnblocked(entry: Entry): Promise<void> {
    if (entry.kind === "inbound") return;
    for (;;) {
      const now = this.now();
      const wait = Math.max(this.blockedUntil - now, (await this.shared?.botBlockedMs(now)) ?? 0);
      if (wait <= 0) return;
      if (wait > this.maxRetryWaitMs) throw new TelegramRateLimitedError(wait);
      entry.log?.(`telegram governor: ${entry.kind} call waits ${wait} ms (retry_after)`);
      await this.sleep(wait, entry.signal);
    }
  }

  /** The numbers the shared budget file is computed with. */
  private config(): BudgetConfig {
    return { chatIntervalMs: this.chatIntervalMs, groupLimit: this.groupLimit, windowMs: this.windowMs, panelReserve: this.panelReserve };
  }

  private attempt(entry: Entry): Promise<Response> {
    const signal = entry.timeoutMs === undefined
      ? entry.signal
      : entry.signal
        ? AbortSignal.any([entry.signal, AbortSignal.timeout(entry.timeoutMs)])
        : AbortSignal.timeout(entry.timeoutMs);
    entry.started = true;
    return entry.send(signal);
  }

  private async run(entry: Entry, state: ChatState | undefined, chat?: string): Promise<Response> {
    for (let attempt = 0; ; attempt++) {
      if (state && chat !== undefined) await this.acquire(entry, state, chat);
      else await this.waitUnblocked(entry);
      const response = await this.attempt(entry);
      if (response.status !== 429) return response;
      const body: unknown = await response.clone().json().catch(() => undefined);
      const retryAfterSeconds = Math.max(1, parseRetryAfter(body, response.headers.get("Retry-After")) ?? 30);
      const until = this.now() + retryAfterSeconds * 1000;
      const scope = state ? `chat ${chat}` : "bot";
      if (state) state.blockedUntil = Math.max(state.blockedUntil, until);
      else this.blockedUntil = Math.max(this.blockedUntil, until);
      await this.shared?.update((ledger) => recordRateLimit(ledger, state ? chat : undefined, until), this.now());
      const retry = attempt === 0 && entry.kind !== "inbound" && retryAfterSeconds * 1000 <= this.maxRetryWaitMs;
      entry.log?.(`telegram governor: 429, ${scope} blocked for ${retryAfterSeconds} s${retry ? ", one retry after the wait" : ", not retried"}`);
      if (!retry) return response;
    }
  }
}

const governors = new Map<string, TelegramGovernor>();

/** The process-wide governor of one bot; all senders of that bot share its budget. */
export function governorFor(botId: string): TelegramGovernor {
  let governor = governors.get(botId);
  if (!governor) {
    governor = new TelegramGovernor({ shared: sharedBudgetFor(botId) });
    governors.set(botId, governor);
  }
  return governor;
}

/** Test seam: forget every shared governor. */
export function resetGovernors(): void {
  governors.clear();
}

function botIdFromUrl(url: string): string {
  return /\/bot(\d+):/.exec(url)?.[1] ?? "unknown";
}

export interface GovernedFetchDeps extends TelegramFetchDeps {
  governor?: TelegramGovernor;
}

/**
 * Drop-in for {@link telegramFetch} on api.telegram.org: same IPv4-first transport, but queued behind the
 * bot's budget and retried once after a 429.
 */
export function governedTelegramFetch(
  url: string,
  init: RequestInit = {},
  request: GovernedRequest = {},
  deps: GovernedFetchDeps = {},
): Promise<Response> {
  const governor = deps.governor ?? governorFor(botIdFromUrl(url));
  return governor.schedule(
    request,
    (signal) => telegramFetch(url, { ...init, signal }, deps),
    init.signal ?? undefined,
  );
}
