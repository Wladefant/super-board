/**
 * The Questions topic holds exactly one message per open question, in creation order, and heals itself.
 * The fake transport models a Telegram topic: a map of live messages, so every assertion reads the
 * topic as the operator would see it.
 */

import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { DaemonStore } from "../daemon/store";
import { QuestionsTopic, type QuestionsTransport } from "../daemon/questions-topic";
import type { Question } from "../src/operator-questions";

let root: string;
let store: DaemonStore;
let clock: number;
let questions: Map<string, Question>;
let messages: Map<number, { text: string; thread: number; owner?: string }>;
let nextId: number;
let topics: number;
let finalized: string[];
let failSends: number;

const open = (id: string, sessionId: string, createdTs: number): Question => ({
  decision_id: id, question: `Question ${id}?`, status: "pending", answer: null, options: [{ id: "a", label: "A" }],
  transport: { session_id: sessionId, chat_id: "-1001", user_id: "7", kind: "operator_question", created_ts: createdTs,
    message_id: 500 + createdTs, topic_message_id: null, topic_card_at: null, session_finalized: false },
} as unknown as Question);

const ledger = {
  list: async () => [...questions.values()].map(q => structuredClone(q)),
  cache: async (id: string, fields: Record<string, unknown>) => { Object.assign(questions.get(id)!.transport, fields); },
  cardFor: async (id: string) => {
    const question = questions.get(id)!;
    if (question.status !== "pending") throw new Error("Question is not open");
    return { question: structuredClone(question), card: { text: question.question, reply_markup: { inline_keyboard: [[{ text: "A", callback_data: id }]] }, id } };
  },
};

const transport: QuestionsTransport = {
  createTopic: async () => ++topics + 1000,
  send: async (thread, text, _markup, owner) => {
    if (failSends > 0) { failSends--; return "error"; }
    const messageId = nextId++;
    messages.set(messageId, { text, thread, owner: owner?.decisionId });
    return { messageId };
  },
  edit: async (messageId, text) => {
    const message = messages.get(messageId);
    if (!message) return "gone";
    message.text = text;
    return "ok";
  },
  remove: async messageId => { messages.delete(messageId); return "ok"; },
  pin: async () => {},
};

const make = (options: Partial<ConstructorParameters<typeof QuestionsTopic>[0]> = {}): QuestionsTopic => new QuestionsTopic({
  chatId: "-1001", slotId: "slot", store, ledger, transport,
  finalize: async question => { finalized.push(question.decision_id); await ledger.cache(question.decision_id, { session_finalized: true }); },
  session: sessionId => ({ name: sessionId, ended: sessionId === "gone-session" }),
  now: () => clock, ...options,
});

/** What the operator sees: copies of open questions, then everything else (index, pings). */
const cards = (): string[] => [...messages.entries()].sort(([a], [b]) => a - b).filter(([, m]) => m.owner).map(([, m]) => m.owner!);
const others = (): string[] => [...messages.values()].filter(m => !m.owner).map(m => m.text);

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "veyyon-questions-topic-"));
  store = new DaemonStore(path.join(root, "daemon.db"));
  clock = 1_000_000;
  questions = new Map();
  messages = new Map();
  nextId = 100;
  topics = 0;
  finalized = [];
  failSends = 0;
});

afterEach(() => {
  store.close();
  fs.rmSync(root, { recursive: true, force: true });
});

describe("Questions topic", () => {
  test("migrates pending questions from several sessions in creation order, with one pinned index", async () => {
    for (const [id, session, ts] of [["q3", "s2", 30], ["q1", "s1", 10], ["q2", "s2", 20]] as const) questions.set(id, open(id, session, ts));
    const topic = make();
    await topic.reconcile();
    expect(cards()).toEqual(["q1", "q2", "q3"]);
    expect(others()).toHaveLength(1);
    expect(others()[0]).toContain("Open questions (3)");
    expect(topics).toBe(1);
  });

  test("answering anywhere deletes the copy, updates the index and finalizes the session copy once", async () => {
    questions.set("q1", open("q1", "s1", 10));
    questions.set("q2", open("q2", "s2", 20));
    const topic = make();
    await topic.reconcile();
    questions.get("q1")!.status = "answered";
    await topic.reconcile();
    await topic.reconcile();
    expect(cards()).toEqual(["q2"]);
    expect(others()[0]).toContain("Open questions (1)");
    expect(finalized).toEqual(["q1"]);
    questions.get("q2")!.status = "dropped";
    await topic.reconcile();
    expect(cards()).toEqual([]);
    expect(others()).toEqual(["<b>No open questions.</b>"]);
  });

  test("reconcile on an unchanged store changes nothing, also after a daemon restart", async () => {
    for (const [id, ts] of [["q1", 10], ["q2", 20]] as const) questions.set(id, open(id, "s1", ts));
    await make().reconcile();
    const before = JSON.stringify([...messages.entries()]);
    await make().reconcile();
    expect(JSON.stringify([...messages.entries()])).toBe(before);
    expect(topics).toBe(1);
  });

  test("an out-of-order tail is deleted and reposted, the ordered prefix is kept", async () => {
    for (const [id, ts] of [["q1", 10], ["q2", 20], ["q3", 30]] as const) questions.set(id, open(id, "s1", ts));
    const topic = make();
    await topic.reconcile();
    const first = questions.get("q1")!.transport.topic_message_id;
    // A question created earlier than q3 turns up late, for example a session that was offline.
    questions.set("q2b", open("q2b", "s2", 25));
    await topic.reconcile();
    expect(cards()).toEqual(["q1", "q2", "q2b", "q3"]);
    expect(questions.get("q1")!.transport.topic_message_id).toBe(first);
    expect(messages.size).toBe(5);
  });

  test("a failed send posts nothing twice and the next reconcile completes the list", async () => {
    questions.set("q1", open("q1", "s1", 10));
    questions.set("q2", open("q2", "s1", 20));
    failSends = 1;
    const topic = make();
    await topic.reconcile();
    await topic.reconcile();
    expect(cards()).toEqual(["q1", "q2"]);
  });

  test("a message deleted by hand is posted again and a deleted topic is rebuilt", async () => {
    questions.set("q1", open("q1", "s1", 10));
    questions.set("q2", open("q2", "s1", 20));
    const topic = make();
    await topic.reconcile();
    messages.delete(questions.get("q1")!.transport.topic_message_id!);
    await topic.reconcile();
    expect(cards()).toEqual(["q1", "q2"]);
    messages.clear();
    const gone = make({ transport: { ...transport, send: async (thread, text, markup, owner) => (thread === 1001 ? "gone" : transport.send(thread, text, markup, owner)) } });
    await gone.reconcile();
    expect(cards()).toEqual(["q1", "q2"]);
    expect(topics).toBe(2);
  });

  test("questions of ended sessions stay listed and are marked", async () => {
    questions.set("q1", open("q1", "gone-session", 10));
    await make().reconcile();
    expect(cards()).toEqual(["q1"]);
    expect(others()[0]).toContain("session ended");
    expect([...messages.values()].find(m => m.owner)?.text).toContain("session ended");
  });

  test("at most one digest ping, only for new questions and only after the interval", async () => {
    questions.set("q1", open("q1", "s1", 10));
    const topic = make();
    await topic.reconcile();
    questions.set("q2", open("q2", "s1", 20));
    clock += 3_600_000;
    await topic.reconcile();
    expect(others().filter(text => text.includes("🔔"))).toHaveLength(0);
    clock += 3_600_001;
    await topic.reconcile();
    await topic.reconcile();
    expect(others().filter(text => text.includes("🔔"))).toHaveLength(1);
    questions.set("q3", open("q3", "s1", 30));
    clock += 7_300_000;
    await topic.reconcile();
    expect(others().filter(text => text.includes("🔔"))).toHaveLength(1);
    expect(others().find(text => text.includes("🔔"))).toContain("3 open questions");
  });
  test("questions closed before the topic existed are baselined without any Telegram edit", async () => {
    for (let n = 1; n <= 5; n++) {
      const legacy = open(`old${n}`, "s1", n);
      legacy.status = "answered";
      questions.set(legacy.decision_id, legacy);
    }
    questions.set("q1", open("q1", "s1", 50));
    await make().reconcile();
    expect(finalized).toEqual([]);
    expect(cards()).toEqual(["q1"]);
    questions.get("q1")!.status = "answered";
    await make().reconcile();
    expect(finalized).toEqual(["q1"]);
  });

  test("a copy Telegram refuses to delete stays cached and is retried, never forgotten", async () => {
    questions.set("q1", open("q1", "s1", 10));
    let refuse = true;
    const stubborn: QuestionsTransport = { ...transport, remove: async id => refuse ? "error" : transport.remove(id) };
    const topic = make({ transport: stubborn });
    await topic.reconcile();
    questions.get("q1")!.status = "answered";
    await topic.reconcile();
    expect(cards()).toEqual(["q1"]);
    expect(questions.get("q1")!.transport.topic_message_id).not.toBeNull();
    refuse = false;
    await topic.reconcile();
    expect(cards()).toEqual([]);
  });
});
