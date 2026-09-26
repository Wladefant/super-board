/**
 * store.ts — Durable daemon routing ledger (`~/.veyyon/telegram/daemon.db`).
 *
 * Two facts have to survive a daemon restart: which session a chat is talking to,
 * and which transcript entries have already been delivered to Telegram. Without the
 * second one a restart replays a whole conversation into the operator's chat.
 */

import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as path from "node:path";
import { getDaemonDbPath } from "./config";

export interface DaemonRoute {
  slotId: string;
  chatId: string;
  topicId: string;
  sessionId: string;
  workspace: string;
  createdAt: number;
  updatedAt: number;
}

interface RouteRow {
  slot_id: string;
  chat_id: string;
  topic_id: string;
  session_id: string;
  workspace: string;
  created_at: number;
  updated_at: number;
}

export class DaemonStore {
  private db: Database;

  constructor(dbPath: string = getDaemonDbPath()) {
    fs.mkdirSync(path.dirname(dbPath), { recursive: true });
    this.db = new Database(dbPath);
    this.db.run("PRAGMA journal_mode = WAL;");
    this.db.run(`
      CREATE TABLE IF NOT EXISTS routes (
        slot_id TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        topic_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL,
        workspace TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        PRIMARY KEY (slot_id, chat_id, topic_id)
      );
    `);
    this.db.run(`
      CREATE TABLE IF NOT EXISTS delivered_entries (
        session_id TEXT NOT NULL,
        entry_id TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        delivered_at INTEGER NOT NULL,
        PRIMARY KEY (session_id, entry_id)
      );
    `);
    this.db.run(`CREATE TABLE IF NOT EXISTS session_listings (
      slot_id TEXT NOT NULL, chat_id TEXT NOT NULL, topic_id TEXT NOT NULL,
      session_ids TEXT NOT NULL, PRIMARY KEY (slot_id, chat_id, topic_id)
    )`);
    // telegram_message texts the session's extension delivered, so the relay can hold back
    // a final reply that only repeats what the operator already has.
    this.db.run(`CREATE TABLE IF NOT EXISTS agent_messages (
      session_id TEXT NOT NULL, text TEXT NOT NULL, sent_at INTEGER NOT NULL
    )`);
    this.db.run("CREATE INDEX IF NOT EXISTS agent_messages_session ON agent_messages (session_id, sent_at)");
  }

  public putSessionListing(slotId: string, chatId: string, topicId: string, ids: string[]): void {
    this.db.run(`INSERT INTO session_listings VALUES (?, ?, ?, ?)
      ON CONFLICT(slot_id, chat_id, topic_id) DO UPDATE SET session_ids = excluded.session_ids`,
    [slotId, chatId, topicId, JSON.stringify(ids)]);
  }

  public getSessionListing(slotId: string, chatId: string, topicId: string): string[] {
    const row = this.db.query<{ session_ids: string }, [string, string, string]>(
      "SELECT session_ids FROM session_listings WHERE slot_id = ? AND chat_id = ? AND topic_id = ?",
    ).get(slotId, chatId, topicId);
    return row ? JSON.parse(row.session_ids) : [];
  }

  public getRoute(slotId: string, chatId: string, topicId = ""): DaemonRoute | null {
    const row = this.db
      .query<RouteRow, [string, string, string]>(
        "SELECT * FROM routes WHERE slot_id = ? AND chat_id = ? AND topic_id = ?",
      )
      .get(slotId, chatId, topicId);
    return row ? toRoute(row) : null;
  }

  public putRoute(route: Omit<DaemonRoute, "createdAt" | "updatedAt">): DaemonRoute {
    const now = Date.now();
    this.db.run(
      `INSERT INTO routes (slot_id, chat_id, topic_id, session_id, workspace, created_at, updated_at)
       VALUES (?, ?, ?, ?, ?, ?, ?)
       ON CONFLICT(slot_id, chat_id, topic_id) DO UPDATE SET
         session_id = excluded.session_id,
         workspace = excluded.workspace,
         updated_at = excluded.updated_at`,
      [route.slotId, route.chatId, route.topicId, route.sessionId, route.workspace, now, now],
    );
    const stored = this.getRoute(route.slotId, route.chatId, route.topicId);
    if (!stored) throw new Error(`Route for ${route.slotId}/${route.chatId} was not persisted`);
    return stored;
  }

  public deleteRoute(slotId: string, chatId: string, topicId = ""): boolean {
    return (
      this.db.run("DELETE FROM routes WHERE slot_id = ? AND chat_id = ? AND topic_id = ?", [
        slotId,
        chatId,
        topicId,
      ]).changes > 0
    );
  }

  public listRoutes(slotId?: string): DaemonRoute[] {
    const rows = slotId
      ? this.db.query<RouteRow, [string]>("SELECT * FROM routes WHERE slot_id = ? ORDER BY updated_at DESC").all(slotId)
      : this.db.query<RouteRow, []>("SELECT * FROM routes ORDER BY updated_at DESC").all();
    return rows.map(toRoute);
  }

  /** Every route bound to a session, so one session's output reaches each chat once. */
  public routesForSession(sessionId: string): DaemonRoute[] {
    return this.db
      .query<RouteRow, [string]>("SELECT * FROM routes WHERE session_id = ?")
      .all(sessionId)
      .map(toRoute);
  }

  /**
   * Claims one transcript entry for delivery. Returns false when it was already
   * claimed, which is what makes outbound delivery idempotent across restarts and
   * across a transcript snapshot that repeats history the daemon has already sent.
   *
   * `deliveryKey` identifies the destination, not a chat: in forum mode a route is a
   * chat and a topic, so the key is composite and the column keeps its original name
   * rather than rewriting every row of an existing daemon.db.
   */
  public claimDelivery(sessionId: string, entryId: string, deliveryKey: string): boolean {
    return (
      this.db.run(
        `INSERT INTO delivered_entries (session_id, entry_id, chat_id, delivered_at)
         VALUES (?, ?, ?, ?) ON CONFLICT(session_id, entry_id) DO NOTHING`,
        [sessionId, entryId, deliveryKey, Date.now()],
      ).changes > 0
    );
  }

  public deliveredCount(sessionId: string): number {
    const row = this.db
      .query<{ count: number }, [string]>("SELECT COUNT(*) AS count FROM delivered_entries WHERE session_id = ?")
      .get(sessionId);
    return row?.count ?? 0;
  }

  /** Records text telegram_message delivered for a session, for the relay's repeat check. */
  public recordAgentMessage(sessionId: string, text: string): void {
    this.db.run("INSERT INTO agent_messages (session_id, text, sent_at) VALUES (?, ?, ?)", [sessionId, text, Date.now()]);
  }

  /** Texts telegram_message delivered for a session since `sinceMs`; older rows are pruned. */
  public recentAgentMessages(sessionId: string, sinceMs: number): string[] {
    this.db.run("DELETE FROM agent_messages WHERE sent_at < ?", [sinceMs]);
    return this.db
      .query<{ text: string }, [string, number]>("SELECT text FROM agent_messages WHERE session_id = ? AND sent_at >= ?")
      .all(sessionId, sinceMs)
      .map(row => row.text);
  }

  public close(): void {
    this.db.close();
  }
}

function toRoute(row: RouteRow): DaemonRoute {
  return {
    slotId: row.slot_id,
    chatId: row.chat_id,
    topicId: row.topic_id,
    sessionId: row.session_id,
    workspace: row.workspace,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  };
}
