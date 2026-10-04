/**
 * The Bot API send budget shared by every process of one bot.
 *
 * The governor in telegram-governor.ts queues sends inside one process. Telegram counts per bot token, so
 * the daemon, each session extension and workflows/portable/telegram_notifier.py must also see each
 * other's sends. They do through one small JSON file per bot under `~/.veyyon/run/telegram-budget/`,
 * guarded by an atomic `mkdir` lock directory. workflows/portable/telegram_budget.py implements the same
 * file format and the same {@link reserve} arithmetic; keep the two in step.
 *
 * File format (times are epoch milliseconds):
 *   { "botBlockedUntil": number,
 *     "chats": { "<chat id>": { "lastAt": number, "blockedUntil": number, "sends": [[at, "panel"|"message"], ...] } } }
 *
 * The lock is held only for one read-compute-write of that file. A holder that dies leaves a lock that is
 * ignored after {@link STALE_LOCK_MS}. If the lock cannot be taken within {@link LOCK_TIMEOUT_MS}, or the
 * file system fails, the caller sends on its own in-process budget: a late message beats a lost one.
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

export interface BudgetConfig {
  chatIntervalMs: number;
  groupLimit: number;
  windowMs: number;
  panelReserve: number;
}

export interface ChatLedger {
  lastAt: number;
  blockedUntil: number;
  sends: Array<[number, string]>;
}

export interface BudgetState {
  botBlockedUntil: number;
  chats: Record<string, ChatLedger>;
}

export interface Reservation {
  /** 0 when the send was booked; otherwise the time to wait before asking again. Nothing is booked then. */
  waitMs: number;
  /** How long a 429 still blocks this chat (or the bot). */
  blockedMs: number;
  reason: string;
}

export const LOCK_TIMEOUT_MS = 2_000;
export const STALE_LOCK_MS = 10_000;

export function emptyState(): BudgetState {
  return { botBlockedUntil: 0, chats: {} };
}

export function isGroupChat(chatId: string): boolean {
  return chatId.startsWith("-");
}

/**
 * Books one send of `kind` into `chat` at `now` if the budget allows it, else says how long to wait.
 * Same rules as the in-process governor: spacing per chat, a group window with a share reserved for panels,
 * and 429 blocks.
 */
export function reserve(state: BudgetState, chat: string, kind: string, now: number, cfg: BudgetConfig): Reservation {
  const ledger = (state.chats[chat] ??= { lastAt: 0, blockedUntil: 0, sends: [] });
  // A clock that moved backwards must not freeze the chat: never trust a time in the future.
  ledger.lastAt = Math.min(ledger.lastAt, now);
  ledger.sends = ledger.sends.filter(([at]) => now - at < cfg.windowMs).map(([at, k]): [number, string] => [Math.min(at, now), k]);
  const blocked = Math.max(state.botBlockedUntil, ledger.blockedUntil);
  const spaced = ledger.lastAt + cfg.chatIntervalMs;
  let readyAt = Math.max(blocked, spaced);
  let reason = blocked > spaced ? "retry_after" : "chat interval";
  if (isGroupChat(chat)) {
    const panel = kind === "panel";
    const limit = panel ? cfg.groupLimit : cfg.groupLimit - cfg.panelReserve;
    const counted = panel ? ledger.sends : ledger.sends.filter(([, k]) => k !== "panel");
    if (counted.length >= limit) {
      const frees = counted[counted.length - limit][0] + cfg.windowMs;
      if (frees > readyAt) {
        readyAt = frees;
        reason = "group window";
      }
    }
  }
  if (readyAt <= now) {
    ledger.sends.push([now, kind === "panel" ? "panel" : "message"]);
    ledger.lastAt = now;
    return { waitMs: 0, blockedMs: Math.max(0, blocked - now), reason: "booked" };
  }
  return { waitMs: readyAt - now, blockedMs: Math.max(0, blocked - now), reason };
}

/** Records a 429: blocks `chat`, or the whole bot when the call was bound to no chat. */
export function recordRateLimit(state: BudgetState, chat: string | undefined, until: number): void {
  if (chat === undefined) {
    state.botBlockedUntil = Math.max(state.botBlockedUntil, until);
    return;
  }
  const ledger = (state.chats[chat] ??= { lastAt: 0, blockedUntil: 0, sends: [] });
  ledger.blockedUntil = Math.max(ledger.blockedUntil, until);
}

function sleepSync(ms: number): void {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
}

export function defaultBudgetDir(): string {
  return process.env.VEYYON_TELEGRAM_BUDGET_DIR || path.join(os.homedir(), ".veyyon", "run", "telegram-budget");
}

/** The shared budget file of one bot. */
export class SharedBudget {
  private readonly file: string;
  private readonly lock: string;

  constructor(botId: string, dir: string = defaultBudgetDir()) {
    const safe = botId.replace(/[^A-Za-z0-9_-]/g, "_");
    this.file = path.join(dir, `${safe}.json`);
    this.lock = path.join(dir, `${safe}.lock`);
  }

  /** Runs `fn` on the current state under the lock and saves the result. Returns undefined when the file is unusable. */
  public update<T>(fn: (state: BudgetState) => T, now: number = Date.now()): T | undefined {
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      if (!this.acquire()) return undefined;
      try {
        const state = this.read();
        const result = fn(state);
        this.write(state, now);
        return result;
      } finally {
        try {
          fs.rmdirSync(this.lock);
        } catch {
          // A stale-lock sweep by another process already removed it.
        }
      }
    } catch {
      return undefined;
    }
  }

  /** How long the bot-wide block lasts after `now`, or 0. Never writes. */
  public botBlockedMs(now: number): number {
    return this.update((state) => Math.max(0, state.botBlockedUntil - now), now) ?? 0;
  }

  private acquire(): boolean {
    const deadline = Date.now() + LOCK_TIMEOUT_MS;
    for (;;) {
      try {
        fs.mkdirSync(this.lock);
        return true;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "EEXIST") return false;
      }
      try {
        if (Date.now() - fs.statSync(this.lock).mtimeMs > STALE_LOCK_MS) {
          fs.rmdirSync(this.lock);
          continue;
        }
      } catch {
        continue;
      }
      if (Date.now() >= deadline) return false;
      sleepSync(2);
    }
  }

  private read(): BudgetState {
    try {
      const parsed: unknown = JSON.parse(fs.readFileSync(this.file, "utf8"));
      if (parsed && typeof parsed === "object" && "chats" in parsed && parsed.chats && typeof parsed.chats === "object") {
        const raw = parsed as { botBlockedUntil?: unknown; chats: Record<string, ChatLedger> };
        return { botBlockedUntil: Number(raw.botBlockedUntil) || 0, chats: raw.chats };
      }
    } catch {
      // Missing or half-written file: start from an empty budget.
    }
    return emptyState();
  }

  private write(state: BudgetState, now: number): void {
    for (const [chat, ledger] of Object.entries(state.chats)) {
      if (ledger.sends.length === 0 && ledger.blockedUntil < now) delete state.chats[chat];
    }
    const tmp = `${this.file}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(state));
    fs.renameSync(tmp, this.file);
  }
}

const budgets = new Map<string, SharedBudget | undefined>();

/** The shared budget of one bot, or undefined when `VEYYON_TELEGRAM_BUDGET_DIR=off`. */
export function sharedBudgetFor(botId: string): SharedBudget | undefined {
  if (process.env.VEYYON_TELEGRAM_BUDGET_DIR === "off") return undefined;
  const key = `${defaultBudgetDir()}|${botId}`;
  if (!budgets.has(key)) budgets.set(key, new SharedBudget(botId));
  return budgets.get(key);
}
