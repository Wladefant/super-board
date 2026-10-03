/**
 * Isolated acceptance run for the Questions topic against the real Bot API. Not a bun test: it posts to a
 * real forum supergroup, so it runs only when asked:
 *
 *   TG_QT_STATE_DIR=<slot state dir with .env> TG_QT_CHAT=<forum chat id> TG_QT_OPERATOR=<user id> bun tests/questions-topic-live.ts
 *
 * It never polls (no getUpdates, so it cannot disturb a running daemon), uses a temporary decision store,
 * pool and daemon database, creates its own throwaway topics and deletes them again at the end.
 * Output is one JSON line per observation, with the Telegram message ids.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { TelegramDaemon } from "../daemon/runtime";
import { DefaultTelegramForumClient } from "../daemon/forum";
import { TelegramPoller } from "../extension/poller";
import { OperatorQuestionService, readQuestions } from "../src/operator-questions";
import type { AccessConfig } from "../extension/types";

const need = (name: string): string => {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required`);
  return value;
};
const stateDir = need("TG_QT_STATE_DIR");
const chat = need("TG_QT_CHAT");
const operator = need("TG_QT_OPERATOR");
const token = /TELEGRAM_BOT_TOKEN\s*=\s*([^\r\n#]+)/.exec(fs.readFileSync(path.join(stateDir, ".env"), "utf8"))?.[1]?.trim();
if (!token) throw new Error("slot token missing");

const work = fs.mkdtempSync(path.join(os.tmpdir(), "veyyon-qt-live-"));
const decisions = path.join(work, "decisions.json");
const pool = path.join(work, "pool.db");
const evidence = (event: string, data: unknown): void => console.log(JSON.stringify({ event, data }));

const client = new DefaultTelegramForumClient(token);
const access: AccessConfig = { dmPolicy: "allowlist", allowFrom: [operator], groups: {}, pending: {} } as unknown as AccessConfig;
const topicsToDelete: number[] = [];

const sessionTopic = async (name: string): Promise<number> => {
  const created = (await client.createForumTopic(chat, name)).message_thread_id;
  topicsToDelete.push(created);
  return created;
};
const poller = (thread: number): TelegramPoller => new TelegramPoller(token, work, access, {} as ConstructorParameters<typeof TelegramPoller>[3],
  null, { messageThreadId: thread, outboundPaceMs: 1500, sendTimeoutMs: 20_000 });
const service = (sessionId: string, p: TelegramPoller): OperatorQuestionService =>
  new OperatorQuestionService(p, () => ({ session_id: sessionId, chat_id: chat, user_id: operator }), decisions, pool, message => evidence("report", message));
const ask = (svc: OperatorQuestionService, name: string) => svc.ask({
  question: `[isolated acceptance] ${name}: which layout?`, problem: "Isolated verification of the Questions topic.",
  options: [{ id: "a", label: "Compact" }, { id: "b", label: "Spacious" }], recommendation: "b",
});
/** The same event id makes a repeated answer idempotent, so a throttled edit may simply be tried again. */
const retried = async <T>(run: () => Promise<T>): Promise<T> => {
  for (let attempt = 1; ; attempt++) {
    try { return await run(); } catch (error) {
      if (attempt >= 5) throw error;
      evidence("retrying", String(error));
      await Bun.sleep(8_000);
    }
  }
};
const snapshot = async () => (await readQuestions(decisions)).map(q => ({
  id: q.decision_id.slice(0, 11), status: q.status, session: q.transport.session_id,
  sessionCopy: q.transport.message_id, questionsCopy: q.transport.topic_message_id, finalized: q.transport.session_finalized,
}));

const daemon = new TelegramDaemon({ poolDbPath: path.join(work, "daemon-pool.db"), daemonDbPath: path.join(work, "daemon.db"),
  manifestPath: path.join(work, "manifest.json"), channelsDir: path.join(work, "channels"), log: message => evidence("daemon", message) });
const slot = { slotId: "isolated-acceptance", botId: "0", stateDir: work } as unknown as Parameters<TelegramDaemon["createQuestionsTopic"]>[0];

let exitCode = 0;
try {
  const [topicA, topicB] = [await sessionTopic("isolated session A"), await sessionTopic("isolated session B")];
  await Bun.sleep(20_000); // Telegram throttles message bursts right after topics are created.
  const [pollerA, pollerB] = [poller(topicA), poller(topicB)];
  const [svcA, svcB] = [service("session-A", pollerA), service("session-B", pollerB)];
  const first = await ask(svcA, "A1");
  await Bun.sleep(4_000);
  const second = await ask(svcB, "B1");
  await Bun.sleep(4_000);
  const third = await ask(svcA, "A2");
  evidence("asked", { A1: first.decision_id, B1: second.decision_id, A2: third.decision_id, topicA, topicB });

  const topic = daemon.createQuestionsTopic(slot, token, chat, pollerA, decisions, pool);
  // Telegram throttles bursts in one chat; the daemon retries a refused send, and so does this loop.
  for (let attempt = 0; attempt < 8; attempt++) {
    await topic.reconcile();
    if ((await snapshot()).every(row => row.questionsCopy)) break;
    await Bun.sleep(15_000);
  }
  const questionsThread = topic.threadId()!;
  topicsToDelete.push(questionsThread);
  let rows = await snapshot();
  evidence("migrated", { questionsThread, rows });
  const order = rows.map(row => row.questionsCopy!);
  if (order.some(id => !id) || order.join() !== [...order].sort((a, b) => a - b).join()) throw new Error("Questions copies missing or out of order");

  // Answer B1 in its own session topic: the Questions copy must disappear and the index must shrink.
  await retried(() => svcB.answer(second.decision_id, "live-1", { choice: "b" }));
  await retried(() => svcB.answer(second.decision_id, "live-2", { choice: "__send" }));
  await topic.reconcile();
  rows = await snapshot();
  evidence("after-answer", rows);
  const answered = rows.find(row => row.id === second.decision_id.slice(0, 11))!;
  if (answered.questionsCopy !== null || !answered.finalized) throw new Error("answered question was not removed from the Questions topic");
  const deleted = await pollerA.deleteTelegramMessage(chat, order[1]);
  evidence("probe-deleted-copy", { messageId: order[1], outcome: deleted, expected: "gone, already deleted" });

  // Restart: a fresh QuestionsTopic over the same store must leave the topic unchanged.
  const before = JSON.stringify(rows);
  const restarted = daemon.createQuestionsTopic(slot, token, chat, pollerA, decisions, pool);
  await restarted.reconcile();
  const after = JSON.stringify(await snapshot());
  evidence("after-restart", { unchanged: before === after, rows: JSON.parse(after) });
  if (before !== after) throw new Error("restart reconcile changed the topic");

  // Drop the last question from the agent side: its copy goes and the index reads empty.
  await svcA.drop(first.decision_id, "isolated test finished");
  await svcA.drop(third.decision_id, "isolated test finished");
  await restarted.reconcile();
  evidence("after-drops", await snapshot());
  restarted.stop();
  topic.stop();
} catch (error) {
  exitCode = 1;
  evidence("FAILED", String(error));
} finally {
  for (const id of topicsToDelete) {
    await fetch(`https://api.telegram.org/bot${token}/deleteForumTopic`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chat, message_thread_id: id }) }).then(r => r.json()).then(r => evidence("deleted-topic", { id, ok: (r as { ok: boolean }).ok }));
  }
  // The daemon keeps its databases open until the process ends, so Windows may refuse the removal.
  try { fs.rmSync(work, { recursive: true, force: true }); } catch { evidence("temp-dir-left", work); }
}
process.exitCode = exitCode;
