/**
 * Writes that do not need to happen must not contend for SQLite's single write lock.
 *
 * The live daemon logged "update_ledger ingest of 0 update(s) failed and was rolled
 * back: database is locked" on empty long-poll batches, and crashed once with the same
 * error from `syncSlots` inside its supervisor timer. Both were writes of nothing: an
 * empty ingest, and an upsert of slot rows that already held the same values. A second
 * connection holding the write lock (here, `BEGIN IMMEDIATE`) reproduces the contention.
 */

import { afterEach, beforeEach, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { BotPoolCoordinator } from "../extension/coordinator";
import { TelegramPoller, type PollerCallbacks, type TelegramUpdate } from "../extension/poller";

let root: string;
const cleanup: Array<() => void> = [];

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "tg-ledger-lock-"));
});

afterEach(() => {
  for (const fn of cleanup.splice(0)) fn();
  fs.rmSync(root, { recursive: true, force: true });
});

/** Holds the database's write lock from a second connection until released. */
function holdWriteLock(dbPath: string): () => void {
  const holder = new Database(dbPath);
  holder.run("PRAGMA busy_timeout = 0;");
  holder.run("BEGIN IMMEDIATE;");
  let released = false;
  const release = (): void => {
    if (released) return;
    released = true;
    try {
      holder.run("ROLLBACK;");
    } finally {
      holder.close();
    }
  };
  cleanup.push(release);
  return release;
}

function poller(): { poller: TelegramPoller; failures: string[]; dbPath: string } {
  const failures: string[] = [];
  const callbacks: PollerCallbacks = {
    isIdle: () => true,
    onUserMessage: () => {},
    onFollowUp: () => {},
    onSteer: () => {},
    onAbort: () => {},
    onRelease: async () => {},
    getStatusText: () => "test",
    onLedgerFailure: text => {
      failures.push(text);
    },
  };
  const instance = new TelegramPoller("0:test-only", root, { dmPolicy: "allowlist", allowFrom: ["1"] }, callbacks);
  cleanup.push(() => instance.stop());
  return { poller: instance, failures, dbPath: path.join(root, "veyyon_bridge_state.db") };
}

test("an empty long-poll batch is not written while another connection holds the ledger lock", () => {
  const { poller: instance, dbPath } = poller();
  holdWriteLock(dbPath);

  const started = Date.now();
  expect(() => instance.ingestUpdates([])).not.toThrow();
  // busy_timeout is 5s: before the fix this waited the full timeout and then threw.
  expect(Date.now() - started).toBeLessThan(1_000);
});

test("a real batch still fails loudly when the ledger lock cannot be taken", () => {
  const { poller: instance, dbPath } = poller();
  const release = holdWriteLock(dbPath);
  const update: TelegramUpdate = {
    update_id: 7,
    message: { message_id: 1, chat: { id: 1, type: "private" }, from: { id: 1, is_bot: false, first_name: "Op" }, date: 0, text: "hi" },
  };

  expect(() => instance.ingestUpdates([update])).toThrow(/rolled back/);

  // The failed batch left no row, so a retry after the lock clears records it.
  release();
  instance.ingestUpdates([update]);
  const db = new Database(dbPath);
  try {
    expect(db.query("SELECT update_id FROM update_ledger").all()).toEqual([{ update_id: 7 }]);
  } finally {
    db.close();
  }
}, 15_000);

test("resolving unchanged slots does not write bot_pool.db, so a held write lock cannot fail it", () => {
  const channelsDir = path.join(root, "channels");
  const stateDir = path.join(channelsDir, "telegram-a");
  fs.mkdirSync(stateDir, { recursive: true });
  fs.writeFileSync(path.join(stateDir, ".env"), "TELEGRAM_BOT_TOKEN=1000000000:AAtesttoken\n", "utf8");
  const poolDbPath = path.join(root, "bot_pool.db");
  const coordinator = new BotPoolCoordinator(poolDbPath, path.join(root, "manifest.json"), channelsDir);
  cleanup.push(() => coordinator.close());

  // First call registers the slot (a genuine write).
  expect(coordinator.getPoolStatus().totalSlots).toBe(1);

  holdWriteLock(poolDbPath);
  const started = Date.now();
  expect(coordinator.getPoolStatus().totalSlots).toBe(1);
  // busy_timeout is 5s: before the fix this waited the full timeout and threw "database is locked".
  expect(Date.now() - started).toBeLessThan(1_000);
});

test("a changed slot is still written", () => {
  const channelsDir = path.join(root, "channels");
  const stateDir = path.join(channelsDir, "telegram-a");
  fs.mkdirSync(stateDir, { recursive: true });
  fs.writeFileSync(path.join(stateDir, ".env"), "TELEGRAM_BOT_TOKEN=1000000000:AAtesttoken\n", "utf8");
  const poolDbPath = path.join(root, "bot_pool.db");
  const coordinator = new BotPoolCoordinator(poolDbPath, path.join(root, "manifest.json"), channelsDir);
  cleanup.push(() => coordinator.close());
  const first = coordinator.getPoolStatus().slots[0]!;

  fs.writeFileSync(path.join(stateDir, ".env"), "TELEGRAM_BOT_TOKEN=1000000001:AAothertoken\n", "utf8");
  const second = coordinator.getPoolStatus().slots[0]!;
  expect(second.fingerprint).not.toBe(first.fingerprint);

  const db = new Database(poolDbPath, { readonly: true });
  try {
    expect(db.query("SELECT fingerprint FROM bot_slots WHERE slot_id = 'telegram-a'").get()).toEqual({ fingerprint: second.fingerprint });
  } finally {
    db.close();
  }
});
