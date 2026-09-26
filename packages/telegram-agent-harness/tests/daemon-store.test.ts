/**
 * Daemon routing ledger. The two properties that matter after a daemon restart:
 * a chat stays bound to its session, and an already-delivered transcript entry is
 * never sent to Telegram twice.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Database } from "bun:sqlite";
import { DaemonStore } from "../daemon/store";

let root: string;
let dbPath: string;
let store: DaemonStore;

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "veyyon-daemon-store-"));
  dbPath = path.join(root, "nested", "daemon.db");
  store = new DaemonStore(dbPath);
});

afterEach(() => {
  store.close();
  fs.rmSync(root, { recursive: true, force: true });
});

describe("daemon routing ledger", () => {
  test("creates its parent directory and starts empty", () => {
    expect(fs.existsSync(dbPath)).toBe(true);
    expect(store.listRoutes()).toEqual([]);
    expect(store.getRoute("slot-1", "555")).toBeNull();
  });

  test("a route round-trips and re-binding the same chat replaces the session", () => {
    const stored = store.putRoute({ slotId: "slot-1", chatId: "555", topicId: "", sessionId: "sess-a", workspace: "C:/dev/demo" });
    expect(stored.sessionId).toBe("sess-a");
    expect(stored.createdAt).toBeGreaterThan(0);

    store.putRoute({ slotId: "slot-1", chatId: "555", topicId: "", sessionId: "sess-b", workspace: "C:/dev/other" });
    expect(store.getRoute("slot-1", "555")).toMatchObject({ sessionId: "sess-b", workspace: "C:/dev/other" });
    expect(store.listRoutes("slot-1").length).toBe(1);
  });

  test("routes are scoped per slot and per chat", () => {
    store.putRoute({ slotId: "slot-1", chatId: "555", topicId: "", sessionId: "sess-a", workspace: "C:/a" });
    store.putRoute({ slotId: "slot-2", chatId: "555", topicId: "", sessionId: "sess-b", workspace: "C:/b" });
    store.putRoute({ slotId: "slot-1", chatId: "666", topicId: "", sessionId: "sess-a", workspace: "C:/a" });

    expect(store.getRoute("slot-1", "555")?.sessionId).toBe("sess-a");
    expect(store.getRoute("slot-2", "555")?.sessionId).toBe("sess-b");
    expect(store.listRoutes("slot-1").map(route => route.chatId).sort()).toEqual(["555", "666"]);
    expect(store.routesForSession("sess-a").map(route => `${route.slotId}/${route.chatId}`).sort())
      .toEqual(["slot-1/555", "slot-1/666"]);
  });

  test("deleting a route reports whether one existed", () => {
    store.putRoute({ slotId: "slot-1", chatId: "555", topicId: "", sessionId: "sess-a", workspace: "C:/a" });
    expect(store.deleteRoute("slot-1", "555")).toBe(true);
    expect(store.deleteRoute("slot-1", "555")).toBe(false);
    expect(store.getRoute("slot-1", "555")).toBeNull();
  });

  test("a transcript entry is claimable exactly once", () => {
    expect(store.claimDelivery("sess-a", "entry-1", "555")).toBe(true);
    expect(store.claimDelivery("sess-a", "entry-1", "555")).toBe(false);
    expect(store.claimDelivery("sess-a", "entry-2", "555")).toBe(true);
    expect(store.claimDelivery("sess-b", "entry-1", "555")).toBe(true);
    expect(store.deliveredCount("sess-a")).toBe(2);
    expect(store.deliveredCount("sess-unknown")).toBe(0);
  });

  test("routes and delivery claims survive reopening the database", () => {
    store.putRoute({ slotId: "slot-1", chatId: "555", topicId: "", sessionId: "sess-a", workspace: "C:/dev/demo" });
    store.claimDelivery("sess-a", "entry-1", "555");
    store.close();

    store = new DaemonStore(dbPath);
    expect(store.getRoute("slot-1", "555")?.sessionId).toBe("sess-a");
    expect(store.claimDelivery("sess-a", "entry-1", "555")).toBe(false);
  });

  test("telegram_message texts are returned per session and only inside the window", () => {
    const before = Date.now();
    store.recordAgentMessage("sess-a", "first");
    store.recordAgentMessage("sess-b", "other session");
    store.recordAgentMessage("sess-a", "second");

    expect(store.recentAgentMessages("sess-a", before).sort()).toEqual(["first", "second"]);
    expect(store.recentAgentMessages("sess-a", Date.now() + 1)).toEqual([]);
    // The expired rows were pruned, so widening the window again does not bring them back.
    expect(store.recentAgentMessages("sess-b", before)).toEqual([]);
  });

  test("a new turn drops the previous turn's telegram_message texts for that session only", () => {
    store.recordAgentMessage("sess-a", "previous turn");
    store.recordAgentMessage("sess-b", "other session");

    store.beginTurn("sess-a");

    expect(store.recentAgentMessages("sess-a", 0)).toEqual([]);
    expect(store.recentAgentMessages("sess-b", 0)).toEqual(["other session"]);
  });

  test("recording a telegram_message prunes expired rows on the write, not only on a read", () => {
    const raw = new Database(dbPath);
    raw.run("INSERT INTO agent_messages (session_id, text, sent_at) VALUES (?, ?, ?)",
      ["sess-a", "stale", Date.now() - 2 * 60 * 60 * 1000]);
    raw.close();

    // A session that goes quiet never reaches the read path, so the insert itself must bound
    // the table rather than leaving the row until the daemon's next routed event.
    store.recordAgentMessage("sess-b", "fresh");

    const probe = new Database(dbPath);
    const texts = probe
      .query<{ text: string }, []>("SELECT text FROM agent_messages ORDER BY sent_at")
      .all()
      .map(row => row.text);
    probe.close();
    expect(texts).toEqual(["fresh"]);
  });
});
