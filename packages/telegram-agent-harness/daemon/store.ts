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
   */
  public claimDelivery(sessionId: string, entryId: string, chatId: string): boolean {
    return (
      this.db.run(
        `INSERT INTO delivered_entries (session_id, entry_id, chat_id, delivered_at)
         VALUES (?, ?, ?, ?) ON CONFLICT(session_id, entry_id) DO NOTHING`,
        [sessionId, entryId, chatId, Date.now()],
      ).changes > 0
    );
  }

  public deliveredCount(sessionId: string): number {
    const row = this.db
      .query<{ count: number }, [string]>("SELECT COUNT(*) AS count FROM delivered_entries WHERE session_id = ?")
      .get(sessionId);
    return row?.count ?? 0;
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
