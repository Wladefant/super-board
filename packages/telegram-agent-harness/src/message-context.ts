import { Database } from "bun:sqlite";

export interface LaneMessageContext {
  laneId?: string;
  laneState?: "active" | "exited" | "unknown";
  senderOrigin?: "agent";
}

/** Extra provenance on the shared correlation row, not a second routing database. */
export class MessageContextStore {
  private readonly db: Database;
  constructor(poolPath: string) {
    this.db = new Database(poolPath);
    this.db.run("PRAGMA busy_timeout = 3000");
    try {
      this.db.run("BEGIN IMMEDIATE");
      const columns = new Set((this.db.query("PRAGMA table_info(message_correlations)").all() as Array<{ name: string }>).map(row => row.name));
      if (!columns.size) throw new Error("The existing message_correlations store must be initialized by the coordinator first");
      for (const name of ["lane_id", "lane_state", "sender_origin"]) {
        if (!columns.has(name)) this.db.run(`ALTER TABLE message_correlations ADD COLUMN ${name} TEXT`);
      }
      this.db.run("COMMIT");
    } catch (error) {
      try { this.db.run("ROLLBACK"); } finally { this.db.close(); }
      throw error;
    }
  }
  record(botId: string, chatId: string, messageId: number, context: LaneMessageContext): void {
    this.db.run("UPDATE message_correlations SET lane_id = ?, lane_state = ?, sender_origin = 'agent' WHERE bot_id = ? AND chat_id = ? AND message_id = ?",
      [context.laneId ?? null, context.laneState ?? "unknown", botId, chatId, messageId]);
  }
  lookup(botId: string, chatId: string, messageId: number): LaneMessageContext {
    const row = this.db.query("SELECT lane_id, lane_state FROM message_correlations WHERE bot_id = ? AND chat_id = ? AND message_id = ?")
      .get(botId, chatId, messageId) as { lane_id: string | null; lane_state: LaneMessageContext["laneState"] } | null;
    return row ? { laneId: row.lane_id ?? undefined, laneState: row.lane_state ?? "unknown", senderOrigin: "agent" } : {};
  }
  setLaneState(sessionId: string, laneId: string, state: NonNullable<LaneMessageContext["laneState"]>): void {
    this.db.run("UPDATE message_correlations SET lane_state = ? WHERE session_id = ? AND lane_id = ?", [state, sessionId, laneId]);
  }
  close(): void { this.db.close(); }
}
