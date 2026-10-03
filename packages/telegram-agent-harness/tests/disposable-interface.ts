// Manual real Telegram smoke. Hard-pinned to the operator-authorized disposable bot.
// No credentials are printed, copied, or supplied as CLI arguments.
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { pathToFileURL } from "node:url";
import { TelegramPoller } from "../extension/poller";
import { OperatorQuestionService } from "../src/operator-questions";
import { MessageContextStore } from "../src/message-context";
import { renderDashboard } from "../src/live-dashboard";
import type { MessageCorrelationBridge, BotPoolManifest } from "../extension/types";

if (!process.argv.includes("--disposable-heylolo2")) throw new Error("Explicit disposable route confirmation required");
const home = path.join(os.homedir(), ".veyyon", "telegram");
const work = process.env.TG_INTERFACE_QA_DIR;
if (!work) throw new Error("TG_INTERFACE_QA_DIR must name an isolated QA state directory");
fs.mkdirSync(work, { recursive: true });
const { BotPoolCoordinator } = await import(pathToFileURL(path.join(home, "coordinator.ts")).href);
const manifest = JSON.parse(fs.readFileSync(path.join(home, "manifest.json"), "utf8")) as BotPoolManifest;
const selected = manifest.slots.find(slot => slot.slotId === "telegram-heylolo2");
if (!selected) throw new Error("Disposable slot missing; refusing to select a substitute");
const manifestPath = path.join(work, "manifest.json");
fs.writeFileSync(manifestPath, JSON.stringify({ version: manifest.version, slots: [selected] }));
const session = "telegram-interface-disposable-qa";
const poolPath = path.join(home, "bot_pool.db");
const coordinator = new BotPoolCoordinator(poolPath, manifestPath, path.join(work, "empty-channels"));
const claim = await coordinator.acquireLease(session, `${work}/${selected.preferredProjects[0] ?? "heylolo2"}`, process.pid);
if (!claim.ok || !claim.slot) { coordinator.close(); throw new Error("Disposable lease unavailable; no route touched"); }
if (claim.slot.botId !== "8764470702" || claim.slot.slotId !== "telegram-heylolo2") {
  coordinator.releaseLease(claim.slot.slotId, session, process.pid); coordinator.close();
  throw new Error("Disposable bot identity mismatch; no traffic sent");
}
const access = coordinator.readAccessConfig(claim.slot.stateDir);
if (!access.allowFrom.includes("1247617658")) throw new Error("Disposable actor authorization missing");
const contextStore = new MessageContextStore(poolPath);
const decisions = path.join(work, "decisions.json");
const bridge: MessageCorrelationBridge = {
  getSessionId: () => session, getSlotId: () => selected.slotId,
  record: correlation => { coordinator.recordOutboundMessage(correlation); contextStore.record(correlation.botId, correlation.chatId, correlation.messageId, correlation); },
  resolveReply: (bot, chat, message) => {
    const result = coordinator.resolveReplyRouting(bot, chat, message, session);
    if (result.correlation) Object.assign(result.correlation, contextStore.lookup(bot, chat, message));
    return result;
  },
  resolveCallback: (token, actor, chat) => coordinator.validateDecisionCallback(token, actor, chat, session, decisions),
  consumeCallback: token => coordinator.consumeDecisionCallback(token),
};
const evidence = (kind: string, detail: unknown) => {
  const row = { at: new Date().toISOString(), kind, detail };
  fs.appendFileSync(path.join(work, "events.jsonl"), JSON.stringify(row) + "\n");
  console.log(JSON.stringify(row));
};
let service: OperatorQuestionService;
const poller = new TelegramPoller(coordinator.readRawTokenForSlot(claim.slot.stateDir), work, access, {
  isIdle: () => true,
  onUserMessage: text => evidence("MAIN_REPLY_RECEIVED", text),
  onSteer: text => evidence("MAIN_STEER_RECEIVED", text),
  onFollowUp: text => evidence("MAIN_FOLLOWUP_RECEIVED", text),
  onAbort: () => evidence("ABORT_REFUSED_IN_QA", null),
  onRelease: async () => evidence("RELEASE_REFUSED_IN_QA", null),
  getStatusText: () => "Disposable interface QA",
  onLedgerFailure: text => evidence("ERROR", text),
  onQuestionAnswer: async (id, event, answer) => { await service.answer(id, event, answer); evidence("QUESTION_INPUT", { id, ...answer }); },
}, bridge);
service = new OperatorQuestionService(poller, () => ({ session_id: session, chat_id: "1247617658", user_id: "1247617658" }),
  decisions, poolPath, text => evidence("ERROR", text));
let timer: Timer | undefined;
const stop = () => {
  clearInterval(timer); service.stop(); poller.stop(); contextStore.close();
  coordinator.releaseLease(selected.slotId, session, process.pid); coordinator.close();
  evidence("STOPPED_AND_LEASE_RELEASED", { pid: process.pid });
  process.exit(0);
};
process.on("SIGINT", stop); process.on("SIGTERM", stop);
void poller.start(); service.start();
const idsFile = path.join(work, "question-ids.json");
let ids: string[];
if (fs.existsSync(idsFile)) {
  ids = JSON.parse(fs.readFileSync(idsFile, "utf8"));
  for (const id of ids) evidence("RESTORED_QUESTION", await service.get(id));
} else {
  ids = [];
  for (const name of ["Concurrent A", "Concurrent B", "Prose only", "Option plus context", "Restart persistence"]) {
    const question = await service.ask({ question: `[Disposable QA · ${name}] Which layout should this test use?`,
      problem: "This is a disposable transport verification, not a real operator decision.",
      impact: "Only this QA question is recorded; no work is authorized.",
      options: [{ id: "compact", label: "Compact", description: "Keep all test lanes visible." },
                { id: "spacious", label: "Spacious", description: "Use larger touch targets." }], recommendation: "compact" });
    ids.push(question.decision_id);
    evidence("QUESTION_SENT", { name, id: question.decision_id });
  }
  fs.writeFileSync(idsFile, JSON.stringify(ids));
  for (const [lane, state] of [["QA lane A", "active"], ["QA lane B", "active"], ["QA exited lane", "exited"]] as const) {
    await poller.sendTelegramMessage("1247617658", `<b>Agent · ${lane}</b>\nDisposable reply-correlation context only; no live worker is claimed. Reply to this message for Main.`,
      undefined, undefined, { laneId: lane, laneState: state });
  }
}
for (const id of ids) void service.wait(id).then(answer => evidence("WAITING_QUESTION_ANSWER", answer));
let update = 0;
const dashboard = async () => {
  await poller.updateDashboard("1247617658", renderDashboard({ observedAt: Date.now(),
    lanes: [{ name: "Disposable transport QA", task: `Dashboard refresh ${++update}; no live worker registry asserted`, state: "active" }],
    blockers: [{ question: "Answer the disposable question cards above." }], mergeQueue: [] },
    "<b>Allowance windows</b>\nNot queried by this disposable transport smoke.\nCodex Spark: unavailable."));
  evidence("DASHBOARD_REFRESH", { update });
};
await dashboard();
timer = setInterval(() => { void dashboard().catch(error => evidence("ERROR", String(error))); }, 35_000);
evidence("READY", { pid: process.pid, slot: selected.slotId, botId: claim.slot.botId, questions: ids });
