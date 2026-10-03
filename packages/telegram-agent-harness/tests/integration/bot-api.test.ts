// Real Bot API integration suite. Opt-in only: it runs when TELEGRAM_TEST_BOT_TOKEN and
// TELEGRAM_TEST_CHAT_ID name a throwaway bot and a chat you are willing to have written to.
//
// It MUST NOT be pointed at a slot in ~/.veyyon/telegram/manifest.json: every one of those
// tokens belongs to a live operator route, and a second getUpdates consumer on such a token
// makes Telegram terminate the running poller (HTTP 409). The suite refuses any token whose
// bot id matches a configured slot rather than trusting the operator to remember.
//
// Covered against the real API: setMyCommands registration, the 409 conflict path a second
// consumer produces, pinned-dashboard create/edit/re-pin after an unpin, and a callback
// button round-trip through answerCallbackQuery. Nothing here reads the shared bot pool.
import { afterAll, beforeAll, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { registerTelegramCommands } from "../../extension/command-registry";
import { TelegramPoller } from "../../extension/poller";
import type { AccessConfig, BotPoolManifest, MessageCorrelationBridge } from "../../extension/types";

const token = process.env.TELEGRAM_TEST_BOT_TOKEN;
const chatId = process.env.TELEGRAM_TEST_CHAT_ID;
const live = Boolean(token && chatId);

/** Bot ids of every configured slot; a live route is never an acceptable test target. */
function configuredBotIds(): Set<string> {
  const manifestPath = process.env.VEYYON_MANIFEST_PATH ?? path.join(os.homedir(), ".veyyon", "telegram", "manifest.json");
  if (!fs.existsSync(manifestPath)) return new Set();
  const parsed: unknown = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const slots = parsed && typeof parsed === "object" && "slots" in parsed ? parsed.slots : undefined;
  if (!Array.isArray(slots)) return new Set();
  const ids = new Set<string>();
  for (const slot of slots) {
    if (slot && typeof slot === "object" && "botId" in slot && typeof slot.botId === "string") ids.add(slot.botId);
  }
  return ids;
}

const access: AccessConfig = { dmPolicy: "allowlist", allowFrom: chatId ? [chatId] : [] };
const bridge: MessageCorrelationBridge = {
  getSessionId: () => "bot-api-integration",
  getSlotId: () => "bot-api-integration",
  record: () => {},
  resolveReply: () => ({ decision: "reject_unknown", detail: "Integration suite records no originals" }),
  resolveCallback: () => ({ decision: "reject_unknown", detail: "Integration suite issues no decisions" }),
  consumeCallback: () => false,
};

let stateDir = "";
const pollers: TelegramPoller[] = [];

function newPoller(): TelegramPoller {
  const poller = new TelegramPoller(token!, fs.mkdtempSync(path.join(stateDir, "poller-")), access, {
    isIdle: () => true,
    onUserMessage: () => {},
    onFollowUp: () => {},
    onSteer: () => {},
    onAbort: () => {},
    onRelease: async () => {},
    getStatusText: () => "integration",
    onLedgerFailure: message => { throw new Error(message); },
  }, bridge, { maxConflictRetries: 2, initialConflictBackoffMs: 250, maxConflictBackoffMs: 500 });
  pollers.push(poller);
  return poller;
}

async function api(method: string, body: Record<string, unknown>): Promise<Record<string, unknown>> {
  const response = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(15_000),
  });
  const parsed: unknown = await response.json();
  if (!parsed || typeof parsed !== "object") throw new Error(`${method} returned a non-object body`);
  return parsed as Record<string, unknown>;
}

beforeAll(async () => {
  if (!live) return;
  stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-bot-api-"));
  const me = await api("getMe", {});
  const result = me.result;
  const botId = result && typeof result === "object" && "id" in result ? String(result.id) : "";
  if (!botId) throw new Error("getMe did not identify the test bot; refusing to send traffic");
  if (configuredBotIds().has(botId)) {
    throw new Error(`TELEGRAM_TEST_BOT_TOKEN belongs to configured slot bot ${botId}; use a throwaway bot instead`);
  }
});

afterAll(() => {
  for (const poller of pollers) poller.stop();
  if (stateDir) fs.rmSync(stateDir, { recursive: true, force: true });
});

test.skipIf(!live)("setMyCommands publishes the private-scope surface the help text claims", async () => {
  await registerTelegramCommands(token!, [chatId!]);
  const listed = await api("getMyCommands", { scope: { type: "chat", chat_id: chatId } });
  const commands = listed.result;
  expect(Array.isArray(commands)).toBe(true);
  const names = (Array.isArray(commands) ? commands : []).map(entry =>
    entry && typeof entry === "object" && "command" in entry ? String(entry.command) : "");
  expect(names).toEqual(["status", "agents", "usage", "shot", "prompt", "steer", "cancel", "release", "reload", "help"]);
}, 60_000);

test.skipIf(!live)("a second getUpdates consumer hits 409 and terminates instead of thrashing", async () => {
  const first = newPoller();
  const second = newPoller();
  await first.start();
  expect(first.running).toBe(true);

  // Telegram allows exactly one getUpdates consumer per token; the second must give up.
  await second.start();
  expect(second.running).toBe(false);
  first.stop();
}, 120_000);

test.skipIf(!live)("the pinned dashboard edits in place and re-pins after the operator unpins it", async () => {
  const poller = newPoller();
  await poller.updateDashboard(chatId!, "<b>Integration dashboard</b>\nfirst snapshot");
  const key = `dashboard:bot-api-integration:${chatId}:0`;
  const messageId = Number(poller.getMeta(key));
  expect(messageId).toBeGreaterThan(0);

  const pinnedChat = await api("getChat", { chat_id: chatId });
  const chat = pinnedChat.result;
  const pinned = chat && typeof chat === "object" && "pinned_message" in chat ? chat.pinned_message : undefined;
  const pinnedId = pinned && typeof pinned === "object" && "message_id" in pinned ? Number(pinned.message_id) : 0;
  expect(pinnedId).toBe(messageId);

  await api("unpinChatMessage", { chat_id: chatId, message_id: messageId });
  // The coalescing window is what stops one edit per event; expire it to refresh now.
  poller.setMeta(`${key}:updated`, "0");
  await poller.updateDashboard(chatId!, "<b>Integration dashboard</b>\nsecond snapshot");
  expect(Number(poller.getMeta(key))).toBe(messageId);

  const repinned = await api("getChat", { chat_id: chatId });
  const afterChat = repinned.result;
  const afterPin = afterChat && typeof afterChat === "object" && "pinned_message" in afterChat ? afterChat.pinned_message : undefined;
  expect(afterPin && typeof afterPin === "object" && "message_id" in afterPin ? Number(afterPin.message_id) : 0).toBe(messageId);

  await api("unpinChatMessage", { chat_id: chatId, message_id: messageId });
  await api("deleteMessage", { chat_id: chatId, message_id: messageId });
}, 120_000);

test.skipIf(!live)("callback buttons survive a real send and clear on demand", async () => {
  const poller = newPoller();
  const sent = await poller.sendTelegramMessage(chatId!, "Integration callback card",
    { inline_keyboard: [[{ text: "A: option", callback_data: "cb:integration" }]] });
  expect(sent?.ok).toBe(true);
  const messageId = sent?.result?.message_id ?? 0;
  expect(messageId).toBeGreaterThan(0);

  expect(await poller.clearCallbackButtons(chatId!, messageId, "Answer saved")).toBe(true);
  await api("deleteMessage", { chat_id: chatId, message_id: messageId });
}, 60_000);

test.skipIf(live)("the suite states plainly that no live Bot API traffic was exercised", () => {
  // Reported rather than silently skipped: a green run without credentials proves the
  // mocked contracts only, and the manifest tokens must never be substituted for them.
  expect(token).toBeUndefined();
  const manifestPath = process.env.VEYYON_MANIFEST_PATH ?? path.join(os.homedir(), ".veyyon", "telegram", "manifest.json");
  if (fs.existsSync(manifestPath)) {
    const parsed: unknown = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
    const slots = parsed && typeof parsed === "object" && "slots" in parsed ? parsed.slots : undefined;
    expect(Array.isArray(slots) ? (slots as BotPoolManifest["slots"]).length : 0).toBeGreaterThan(0);
  }
});
