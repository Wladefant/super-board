/**
 * The Bot API send budget shared by every process of one bot.
 *
 * The governor in telegram-governor.ts queues sends inside one process. Telegram counts per bot token, so
 * the daemon, each session extension and workflows/portable/telegram_notifier.py must also see each
 * other's sends. They do through one small JSON file per bot under `~/.veyyon/run/telegram-budget/`,
 * guarded by an O_EXCL lock file that names its holder. workflows/portable/telegram_budget.py implements the same
 * file format and the same {@link reserve} arithmetic; keep the two in step.
 *
 * File format (times are epoch milliseconds):
 *   { "botBlockedUntil": number,
 *     "chats": { "<chat id>": { "lastAt": number, "blockedUntil": number, "sends": [[at, "panel"|"message"], ...] } } }
 *
 * The lock is held only for one read-compute-write of that file, never across a Telegram call. A holder that
 * dies is recognised by its pid being gone (see {@link SharedBudget}); a live holder is never evicted, however
 * long it holds. If the lock cannot be taken within {@link LOCK_TIMEOUT_MS}, or the file system fails, the
 * caller sends on its own in-process budget: a late message beats a lost one. Waiting never blocks the event loop.
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
/** A lock whose holder cannot be named (it died mid-write) is abandoned after this long. */
export const UNNAMED_LOCK_MS = 1_000;
/** Holds last microseconds. A named holder older than this is a dead process whose pid was reused. */
export const MAX_LOCK_AGE_MS = 5 * 60_000;

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

export function defaultBudgetDir(): string {
  return process.env.VEYYON_TELEGRAM_BUDGET_DIR || path.join(os.homedir(), ".veyyon", "run", "telegram-budget");
}

/** True while a process with this pid exists. `process.kill(pid, 0)` only probes; it never signals. */
function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}

/** The shared budget file of one bot. */
export class SharedBudget {
  private readonly file: string;
  private readonly lock: string;
  private readonly lockTimeoutMs: number;

  constructor(botId: string, dir: string = defaultBudgetDir(), lockTimeoutMs: number = LOCK_TIMEOUT_MS) {
    const safe = botId.replace(/[^A-Za-z0-9_-]/g, "_");
    this.file = path.join(dir, `${safe}.json`);
    this.lock = path.join(dir, `${safe}.lock`);
    this.lockTimeoutMs = lockTimeoutMs;
  }

  /**
   * Runs `fn` on the current state under the lock and saves the result. Resolves undefined, after at most the
   * lock timeout, when the lock cannot be taken or the file is unusable; the caller then sends on its own budget.
   * Waiting for the lock never blocks the event loop.
   */
  public async update<T>(fn: (state: BudgetState) => T, now: number = Date.now()): Promise<T | undefined> {
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      const token = `${process.pid}.${crypto.randomUUID()}`;
      // The uncontended case runs lock, update and release in one synchronous stretch, so two callers in this
      // process can never interleave between taking the lock and finishing with it.
      let got = this.tryLock(token);
      if (got === "busy") got = await this.waitForLock(token);
      if (got !== "held") return undefined;
      try {
        const state = this.read();
        const result = fn(state);
        this.write(state, now);
        return result;
      } finally {
        this.release(token);
      }
    } catch {
      return undefined;
    }
  }

  /** How long the bot-wide block lasts after `now`, or 0. Never changes the budget. */
  public async botBlockedMs(now: number): Promise<number> {
    return (await this.update((state) => Math.max(0, state.botBlockedUntil - now), now)) ?? 0;
  }

  /**
   * The lock is a file created with O_EXCL that names its holder (pid and a random token), so a crashed
   * holder is recognised by its pid being gone, not by how long the lock has existed.
   */
  private tryLock(token: string): "held" | "busy" | "failed" {
    try {
      const fd = fs.openSync(this.lock, "wx");
      try {
        fs.writeSync(fd, JSON.stringify({ pid: process.pid, token, at: Date.now() }));
      } finally {
        fs.closeSync(fd);
      }
      return "held";
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      return code === "EEXIST" || code === "EPERM" || code === "EACCES" || code === "EBUSY" ? "busy" : "failed";
    }
  }

  /** Retries with a timer, so the event loop stays free. Every pass checks the deadline, whatever shape the lock path is in. */
  private async waitForLock(token: string): Promise<"held" | "failed"> {
    const deadline = Date.now() + this.lockTimeoutMs;
    for (;;) {
      this.sweepIfAbandoned();
      if (Date.now() >= deadline) return "failed";
      await new Promise<void>((resolve) => setTimeout(resolve, 2 + Math.random() * 3));
      const got = this.tryLock(token);
      if (got !== "busy") return got === "held" ? "held" : "failed";
    }
  }

  /** Removes the lock when its holder is gone. Never throws. */
  private sweepIfAbandoned(): void {
    try {
      const stat = fs.statSync(this.lock);
      if (stat.isDirectory()) {
        // Left by the earlier mkdir-based lock, which carried no holder name: only an old one is abandoned.
        if (Date.now() - stat.mtimeMs > UNNAMED_LOCK_MS * 10) fs.rmdirSync(this.lock);
        return;
      }
      const raw = fs.readFileSync(this.lock, "utf8");
      const age = Date.now() - stat.mtimeMs;
      let pid: unknown;
      try {
        pid = (JSON.parse(raw) as { pid?: unknown }).pid;
      } catch {
        // The holder died between creating the file and writing its name.
      }
      const abandoned = typeof pid === "number" ? !pidAlive(pid) || age > MAX_LOCK_AGE_MS : age > UNNAMED_LOCK_MS;
      // Look again right before removing, so a lock a faster process just took over is left alone.
      if (abandoned && fs.readFileSync(this.lock, "utf8") === raw) fs.unlinkSync(this.lock);
    } catch {
      // Gone already, or not removable: the deadline decides.
    }
  }

  private release(token: string): void {
    try {
      const holder = (JSON.parse(fs.readFileSync(this.lock, "utf8")) as { token?: unknown }).token;
      if (holder === token) fs.unlinkSync(this.lock);
    } catch {
      // Already swept as abandoned; nothing of ours is left to remove.
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
