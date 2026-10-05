/**
 * week-store.ts — SQLite store for the Week view (issue #477).
 *
 * `blocks` is append-only: a block row is never updated or deleted, so history survives the session
 * files being pruned. A re-scan of a longer-running block inserts a new row for the grown block;
 * readers take the row with the largest `end_ms` per (session, start). `snapshots` keeps the last
 * WeekData per week start, with its as-of time, so the app can show the last good data when the
 * PC scan or the GitHub guard fails.
 */
import { Database } from "bun:sqlite";
import * as fs from "node:fs";
import * as path from "node:path";
import type { SessionBlock, WeekData } from "./week-summary";

export class WeekStore {
  private readonly db: Database;

  constructor(dbPath: string) {
    if (dbPath !== ":memory:") fs.mkdirSync(path.dirname(dbPath), { recursive: true });
    this.db = new Database(dbPath);
    this.db.run("PRAGMA journal_mode = WAL;");
    this.db.run(`CREATE TABLE IF NOT EXISTS blocks (
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      session_id TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
      project TEXT NOT NULL, cwd TEXT NOT NULL, tool TEXT NOT NULL, model TEXT, first_message TEXT)`);
    this.db.run("CREATE INDEX IF NOT EXISTS blocks_window ON blocks (start_ms, end_ms)");
    this.db.run("CREATE TABLE IF NOT EXISTS snapshots (week_start INTEGER PRIMARY KEY, as_of INTEGER NOT NULL, json TEXT NOT NULL)");
  }

  /** Append blocks that are new or that grew since the last stored row. Returns the number of rows added. */
  appendBlocks(blocks: SessionBlock[]): number {
    const latest = this.db.query("SELECT MAX(end_ms) AS end_ms FROM blocks WHERE session_id = ? AND start_ms = ?");
    const insert = this.db.query("INSERT INTO blocks (session_id, start_ms, end_ms, project, cwd, tool, model, first_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)");
    let added = 0;
    this.db.transaction(() => {
      for (const b of blocks) {
        const row = latest.get(b.sessionId, b.startMs);
        const known = row && typeof row === "object" && "end_ms" in row && typeof row.end_ms === "number" ? row.end_ms : null;
        if (known !== null && known >= b.endMs) continue;
        insert.run(b.sessionId, b.startMs, b.endMs, b.project, b.cwd, b.tool, b.model, b.firstMessage);
        added++;
      }
    })();
    return added;
  }

  /** Blocks overlapping [fromMs, toMs), one per (session, start) with the latest end. */
  blocksBetween(fromMs: number, toMs: number): SessionBlock[] {
    const rows = this.db.query(
      `SELECT b.* FROM blocks b
       JOIN (SELECT session_id, start_ms, MAX(end_ms) AS end_ms FROM blocks GROUP BY session_id, start_ms) m
         ON m.session_id = b.session_id AND m.start_ms = b.start_ms AND m.end_ms = b.end_ms
       WHERE b.end_ms > ? AND b.start_ms < ? GROUP BY b.session_id, b.start_ms ORDER BY b.start_ms, b.session_id`,
    ).all(fromMs, toMs);
    const out: SessionBlock[] = [];
    for (const row of rows) {
      if (!row || typeof row !== "object") continue;
      const r = Object.fromEntries(Object.entries(row));
      out.push({
        sessionId: String(r.session_id), project: String(r.project), cwd: String(r.cwd), tool: String(r.tool),
        model: typeof r.model === "string" ? r.model : null,
        startMs: Number(r.start_ms), endMs: Number(r.end_ms),
        firstMessage: typeof r.first_message === "string" ? r.first_message : null,
      });
    }
    return out;
  }

  saveSnapshot(data: WeekData): void {
    this.db.query("INSERT INTO snapshots (week_start, as_of, json) VALUES (?, ?, ?) ON CONFLICT(week_start) DO UPDATE SET as_of = excluded.as_of, json = excluded.json")
      .run(data.weekStart, data.asOf, JSON.stringify(data));
  }

  /** The last stored snapshot for a week, or null. */
  lastSnapshot(weekStart: number): WeekData | null {
    const row = this.db.query("SELECT json FROM snapshots WHERE week_start = ?").get(weekStart);
    if (!row || typeof row !== "object" || !("json" in row) || typeof row.json !== "string") return null;
    try {
      const parsed: unknown = JSON.parse(row.json);
      return parsed && typeof parsed === "object" && "version" in parsed && parsed.version === 1 ? Object.assign({}, parsed) as WeekData : null;
    } catch { return null; }
  }

  close(): void { this.db.close(); }
}
