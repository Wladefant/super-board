import { test, expect, afterEach } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { BotPoolCoordinator } from "../extension/coordinator";
import { DaemonStore } from "../daemon/store";
import { getDaemonSecret } from "../daemon/daemon-secret";
import { createOperatorQuestionService, operatorQuestionPaths } from "../extension/question-factory";
import type { TelegramPoller } from "../extension/poller";

const cleanup: Array<() => void> = [];
afterEach(() => {
  for (const close of cleanup.splice(0)) close();
});

function setupTestEnvironment() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-question-factory-test-"));
  const poolDbPath = path.join(dir, "bot_pool.db");
  const daemonDbPath = path.join(dir, "daemon.db");
  const manifestPath = path.join(dir, "manifest.json");
  const channelsDir = path.join(dir, "channels");
  fs.mkdirSync(channelsDir, { recursive: true });

  const coordinator = new BotPoolCoordinator(poolDbPath, manifestPath, channelsDir);
  const store = new DaemonStore(daemonDbPath);
  coordinator.setAuditStore(store);

  const slotId = "slot-question-factory";
  const secret = getDaemonSecret(store, slotId);
  coordinator.setDaemonSecret(secret);

  cleanup.push(() => {
    coordinator.close();
    store.close();
    try {
      fs.rmSync(dir, { recursive: true, force: true });
    } catch {}
  });

  return { dir, poolDbPath, coordinator, store, slotId, secret };
}

/** Regression for the runtime construction order (pull #751): the factory must
 * land decisionsPath and poolPath in the constructor's path positions and the
 * signing coordinator/secret in the control positions, so a published question
 * carries markup the daemon validator accepts. Fails on the pre-fix head where
 * the construction passed (poller, route, logger, coordinator, secret). */
test("runtime question service factory publishes signed markup the daemon validator accepts", async () => {
  const { dir, poolDbPath, coordinator, secret } = setupTestEnvironment();
  type Keyboard = { inline_keyboard: Array<Array<{ text: string; callback_data: string }>> };
  const decisionsPath = path.join(dir, "decisions.json");
  let markup: Keyboard = { inline_keyboard: [] };
  let reported: string[] = [];
  const transport = {
    sendTelegramMessage: async (_chat: string, _text: string, _mode: string, keyboard: Keyboard) => {
      markup = keyboard;
      return { ok: true, result: { message_id: 91 } };
    },
    editTelegramMessage: async (_chat: string, _id: number, _text: string, _mode: string, _unused: unknown, keyboard: Keyboard) => {
      markup = keyboard;
      return { ok: true, result: { message_id: 91 } };
    },
  };
  const route = { session_id: "factory-session", chat_id: "-100123", user_id: "4242" };
  const service = createOperatorQuestionService({
    poller: transport as unknown as TelegramPoller,
    route: () => route,
    report: message => reported.push(message),
    coordinator,
    slotId: "slot-question-factory",
    decisionsPath,
    poolPath: poolDbPath,
    secret,
  });
  const question = await service.ask({
    question: "Runtime construction works?",
    options: [{ id: "yes", label: "Yes" }, { id: "no", label: "No" }],
    recommendation: "yes",
  });
  const yesButton = markup.inline_keyboard.flat().find(button => button.text.includes("Yes"));
  expect(yesButton).toBeDefined();
  const accepted = coordinator.validateDecisionCallback(yesButton!.callback_data, route.user_id, route.chat_id, route.session_id, decisionsPath);
  expect(accepted.decision).toBe("deliver");
  expect(accepted.record?.choiceId).toBe("yes");
  await service.answer(question.decision_id, "factory-select", { choice: accepted.record!.choiceId });
  expect(reported).toEqual([]);
}, 60000);

test("operatorQuestionPaths resolves the canonical machine paths", () => {
  const { decisionsPath, poolPath } = operatorQuestionPaths();
  expect(decisionsPath).toBe(path.join(os.homedir(), ".veyyon", "workflows", "decisions.json"));
  expect(poolPath.endsWith(path.join(".veyyon", "telegram", "bot_pool.db"))).toBe(true);
});
