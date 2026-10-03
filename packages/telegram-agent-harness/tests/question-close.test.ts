import { afterEach, beforeEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { TelegramPoller } from "../extension/poller";
import { OperatorQuestionService, QuestionStore, readQuestions } from "../src/operator-questions";

let dir: string;
let edits: Array<{ messageId: number; text: string }>;
let editOutcome: { ok: boolean; description?: string };
let reports: string[];

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-question-close-"));
  edits = [];
  editOutcome = { ok: true };
  reports = [];
});
afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

function service(): OperatorQuestionService {
  const poller = {
    sendTelegramMessage: async () => ({ ok: true, result: { message_id: 40 + edits.length } }),
    editTelegramMessage: async (_chat: string, messageId: number, text: string) => { edits.push({ messageId, text }); return editOutcome; },
  } as unknown as TelegramPoller;
  return new OperatorQuestionService(poller, () => ({ session_id: "s1", chat_id: "1", user_id: "1" }),
    path.join(dir, "decisions.json"), path.join(dir, "pool.db"), message => reports.push(message));
}

const input = { question: "Which layout?", options: [{ id: "a", label: "Compact" }, { id: "b", label: "Spacious" }], recommendation: "b" };

test("resolve closes a question answered elsewhere and rewrites the session copy to the verdict", async () => {
  const questions = service();
  const asked = await questions.ask(input);
  const closed = await questions.resolve(asked.decision_id, { choice: "b", text: "in the terminal" });
  expect(closed.status).toBe("answered");
  expect(edits.at(-1)?.text).toContain("Answered");
  expect(edits.at(-1)?.text).toContain("Spacious");
  const [stored] = await readQuestions(path.join(dir, "decisions.json"));
  expect(stored.transport.session_finalized).toBe(true);
});

test("drop records the reason on the session copy", async () => {
  const questions = service();
  const asked = await questions.ask(input);
  await questions.drop(asked.decision_id, "no longer needed");
  expect(edits.at(-1)?.text).toContain("Dropped");
  expect(edits.at(-1)?.text).toContain("no longer needed");
});

test("a refused edit leaves the copy unfinalized so the daemon retries it", async () => {
  const questions = service();
  const asked = await questions.ask(input);
  editOutcome = { ok: false, description: "Too Many Requests: retry after 5" };
  await questions.drop(asked.decision_id, "obsolete");
  const [stored] = await readQuestions(path.join(dir, "decisions.json"));
  expect(stored.status).toBe("dropped");
  expect(stored.transport.session_finalized).toBe(false);
  expect(reports.join()).toContain("not updated");
  editOutcome = { ok: true };
  await service().finalize(stored);
  expect((await readQuestions(path.join(dir, "decisions.json")))[0].transport.session_finalized).toBe(true);
});

test("the daemon store caches ids but cannot change a question's state", async () => {
  const asked = await service().ask(input);
  const store = new QuestionStore(path.join(dir, "decisions.json"), path.join(dir, "pool.db"));
  await store.cache(asked.decision_id, { topic_message_id: 77 });
  const [stored] = await store.list();
  expect([stored.status, stored.transport.topic_message_id]).toEqual(["pending", 77]);
  const { card } = await store.cardFor(asked.decision_id);
  expect(card.reply_markup.inline_keyboard).toBeTruthy();
});
