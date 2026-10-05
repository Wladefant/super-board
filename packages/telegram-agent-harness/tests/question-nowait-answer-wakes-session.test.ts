import { afterEach, beforeEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import type { TelegramPoller } from "../extension/poller";
import { OperatorQuestionService, readQuestions } from "../src/operator-questions";

let dir: string;
let woken: Array<{ sessionId: string; text: string }>;

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-nowait-wake-"));
  woken = [];
});
afterEach(() => fs.rmSync(dir, { recursive: true, force: true }));

function service(): OperatorQuestionService {
  const poller = {
    sendTelegramMessage: async () => ({ ok: true, result: { message_id: 7042 } }),
    editTelegramMessage: async () => ({ ok: true, result: { message_id: 7042 } }),
  } as unknown as TelegramPoller;
  return new OperatorQuestionService(poller, () => ({ session_id: "s1", chat_id: "1", user_id: "1" }),
    path.join(dir, "decisions.json"), path.join(dir, "pool.db"), () => {}, undefined, undefined, undefined,
    async (sessionId, text) => { woken.push({ sessionId, text }); });
}

const base = { question: "Ship it?", options: [{ id: "yes", label: "Yes" }, { id: "no", label: "No" }], recommendation: "yes" };

test("an answered wait:false question injects exactly one message into the asking session", async () => {
  const questions = service();
  const asked = await questions.ask({ ...base, wait: false });
  await questions.answer(asked.decision_id, "e1", { choice: "yes" });
  expect(woken).toHaveLength(0);
  await questions.answer(asked.decision_id, "e2", { choice: "__send" });
  expect(woken).toHaveLength(1);
  expect(woken[0]!.sessionId).toBe("s1");
  expect(woken[0]!.text).toContain(asked.decision_id);
  expect(woken[0]!.text).toContain("Yes");
  // A replayed tap must not wake the session again.
  await questions.answer(asked.decision_id, "e2", { choice: "__send" });
  expect(woken).toHaveLength(1);
  expect((await readQuestions(path.join(dir, "decisions.json")))[0]!.transport.answer_pushed).toBe(true);
});

test("an answered question that waits is left to its waiting tool call", async () => {
  const questions = service();
  const asked = await questions.ask(base);
  await questions.answer(asked.decision_id, "e1", { choice: "no" });
  await questions.answer(asked.decision_id, "e2", { choice: "__send" });
  expect(woken).toHaveLength(0);
});
