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
 * - 429: the bot is blocked until `retry_after` has passed. The call is retried once after that wait and
 *   the second answer is returned as is. A `retry_after` above `maxRetryWaitMs` is not waited out inline.
 * - Coalescing: an edit of the same message that is still queued is replaced by the newer edit, and both
 *   callers get the answer to the one request that is sent.
 *
 * Calls go out through {@link telegramFetch}, so they keep IPv4-first connects.
 */
import { telegramFetch, type TelegramFetchDeps } from "./telegram-fetch";

export type GovernorKind = "message" | "panel" | "other" | "inbound";

export interface GovernedRequest {
  /** Chat the call writes into. Omit for calls that are not bound to one (getFile, setMyCommands). */
  chatId?: string | number;
  /** `panel` draws on the reserved group budget. `inbound` (getUpdates, getMe) is never delayed by the budget. */
  kind?: GovernorKind;
  /** Calls with the same key that are still queued collapse into the newest one. */
  coalesceKey?: string;
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
  now?: () => number;
  sleep?: (ms: number, signal?: AbortSignal) => Promise<void>;
}

export interface GovernorSnapshot {
  blockedForMs: number;
  chats: Array<{ chat: string; sendsInWindow: number; queued: number }>;
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
  timeoutMs: number | undefined;
  log: ((message: string) => void) | undefined;
  waiters: Waiter[];
  started: boolean;
}

interface ChatState {
  tail: Promise<void>;
  sends: Array<{ at: number; kind: GovernorKind }>;
  lastAt: number;
  queued: number;
  pending: Map<string, Entry>;
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
  signal?.addEventListener("abort", onAbort, { once: true });
  return promise;
};

/** Groups, supergroups and channels have negative ids; a private chat id is positive. */
export function isGroupChat(chatId: string): boolean {
  return chatId.startsWith("-");
}

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

export class TelegramGovernor {
  private readonly chatIntervalMs: number;
  private readonly groupLimit: number;
  private readonly windowMs: number;
  private readonly panelReserve: number;
  private readonly maxRetryWaitMs: number;
  private readonly now: () => number;
  private readonly sleep: (ms: number, signal?: AbortSignal) => Promise<void>;
  private blockedUntil = 0;
  private readonly chats = new Map<string, ChatState>();

  constructor(options: GovernorOptions = {}) {
    this.chatIntervalMs = options.chatIntervalMs ?? 1000;
    this.groupLimit = options.groupLimit ?? 20;
    this.windowMs = options.windowMs ?? 60_000;
    this.panelReserve = options.panelReserve ?? 12;
    this.maxRetryWaitMs = options.maxRetryWaitMs ?? 60_000;
    this.now = options.now ?? Date.now;
    this.sleep = options.sleep ?? defaultSleep;
  }

  public snapshot(): GovernorSnapshot {
    const now = this.now();
    return {
      blockedForMs: Math.max(0, this.blockedUntil - now),
      chats: [...this.chats].map(([chat, state]) => ({
        chat,
        sendsInWindow: state.sends.filter((s) => now - s.at < this.windowMs).length,
        queued: state.queued,
      })),
    };
  }

  /** Runs `send` once the budget allows it. Resolves with the final response, after at most one 429 retry. */
  public schedule(request: GovernedRequest, send: Send, signal?: AbortSignal): Promise<Response> {
    const kind = request.kind ?? (request.chatId === undefined ? "other" : "message");
    if (request.chatId === undefined || kind === "inbound") {
      return this.run({ send, signal, kind, timeoutMs: request.timeoutMs, log: request.log, waiters: [], started: true }, undefined);
    }
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
    const entry: Entry = { send, signal, kind, timeoutMs: request.timeoutMs, log: request.log, waiters: [], started: false };
    const first = Promise.withResolvers<Response>();
    entry.waiters.push(first);
    const done = first.promise;
    if (request.coalesceKey) state.pending.set(request.coalesceKey, entry);
    state.queued++;
    state.tail = state.tail.then(async () => {
      try {
        const response = await this.run(entry, state, chat);
        this.settle(entry, response);
      } catch (error) {
        for (const waiter of entry.waiters) waiter.reject(error);
      } finally {
        state.queued--;
        if (request.coalesceKey && state.pending.get(request.coalesceKey) === entry) state.pending.delete(request.coalesceKey);
      }
    });
    return done;
  }

  private settle(entry: Entry, response: Response): void {
    entry.waiters.forEach((waiter, index) => waiter.resolve(index === 0 ? response : response.clone()));
  }

  private chatState(chat: string): ChatState {
    let state = this.chats.get(chat);
    if (!state) {
      state = { tail: Promise.resolve(), sends: [], lastAt: Number.NEGATIVE_INFINITY, queued: 0, pending: new Map() };
      this.chats.set(chat, state);
    }
    return state;
  }

  /** Blocks until the budget lets one send of `kind` into `chat` go out, then books it. */
  private async acquire(entry: Entry, state: ChatState, chat: string): Promise<void> {
    for (;;) {
      const now = this.now();
      state.sends = state.sends.filter((s) => now - s.at < this.windowMs);
      let readyAt = Math.max(this.blockedUntil, state.lastAt + this.chatIntervalMs);
      let reason = this.blockedUntil > state.lastAt + this.chatIntervalMs ? "retry_after" : "chat interval";
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
      if (readyAt <= now) {
        state.sends.push({ at: now, kind: entry.kind });
        state.lastAt = now;
        return;
      }
      // The routine one-second spacing is not worth a line; a longer wait means a budget or a rate limit.
      if (reason !== "chat interval") entry.log?.(`telegram governor: chat ${chat} ${entry.kind} waits ${readyAt - now} ms (${reason})`);
      await this.sleep(readyAt - now, entry.signal);
    }
  }

  private async waitUnblocked(entry: Entry): Promise<void> {
    for (;;) {
      const wait = this.blockedUntil - this.now();
      if (wait <= 0) return;
      entry.log?.(`telegram governor: ${entry.kind} call waits ${wait} ms (retry_after)`);
      await this.sleep(wait, entry.signal);
    }
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
      this.blockedUntil = Math.max(this.blockedUntil, this.now() + retryAfterSeconds * 1000);
      const retry = attempt === 0 && entry.kind !== "inbound" && retryAfterSeconds * 1000 <= this.maxRetryWaitMs;
      entry.log?.(`telegram governor: 429, bot blocked for ${retryAfterSeconds} s${retry ? ", one retry after the wait" : ", not retried"}`);
      if (!retry) return response;
    }
  }
}

const governors = new Map<string, TelegramGovernor>();

/** The process-wide governor of one bot; all senders of that bot share its budget. */
export function governorFor(botId: string): TelegramGovernor {
  let governor = governors.get(botId);
  if (!governor) {
    governor = new TelegramGovernor();
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
