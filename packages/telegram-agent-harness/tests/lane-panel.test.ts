import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { LANE_PANEL_CALLBACK_PREFIX, TelegramPoller, type PollerCallbacks } from "../extension/poller";
import type { MessageCorrelationBridge, TelegramUpdate } from "../extension/types";
import type { FleetLane, FleetSnapshot } from "../daemon/fleet-state";
import { LanePanels, PANEL_ALREADY_STOPPING_ANSWER, PANEL_EXPIRED_ANSWER, renderLanePanel, type PanelOutcome } from "../daemon/lane-panel";
import type { RouteTarget } from "../daemon/router";
import { DaemonStore } from "../daemon/store";
import { TelegramGovernor } from "../extension/telegram-governor";

const originalFetch = globalThis.fetch;
const cleanup: Array<() => void> = [];
afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const close of cleanup.splice(0)) close();
});

const CHAT = "-100777";
const TOPIC: RouteTarget = { chatId: CHAT, topicId: "42" };
const MIN = 60_000;

function lane(overrides: Partial<FleetLane> = {}): FleetLane {
  return {
    id: "s1", name: "super-board", kind: "interactive", parentId: null, childIds: [], cwd: "C:/w/super-board",
    model: "anthropic/claude-opus-5-5:high", status: "running", startedAtMs: 0, elapsedMs: 5 * MIN,
    lastActivityMs: 0, lastAction: "bash: bun test", ...overrides,
  };
}

function snapshotOf(lanes: FleetLane[], questions: FleetSnapshot["questions"] = []): FleetSnapshot {
  return {
    version: 1, observedAt: 0, lanes,
    counts: { running: 0, idle: 0, subagents: 0 },
    usage: { available: false, observedAt: 0, windows: [] },
    hostMemory: { totalBytes: 1, freeBytes: 1, usedPercent: 0 },
    questions,
  };
}

interface Call { op: "send" | "edit" | "pin"; messageId?: number; text?: string; markup?: { inline_keyboard: Array<Array<Record<string, string>>> } }

function fixture(options: { miniAppLink?: string } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "lane-panel-"));
  const store = new DaemonStore(path.join(dir, "daemon.db"));
  cleanup.push(() => { store.close?.(); fs.rmSync(dir, { recursive: true, force: true }); });
  store.putRoute({ slotId: "slot", chatId: CHAT, topicId: TOPIC.topicId, sessionId: "s1", workspace: "C:/w/super-board" });
  const state = {
    now: 1_000_000,
    snapshot: snapshotOf([lane()]),
    busy: true,
    editOutcome: "ok" as PanelOutcome,
    nextMessageId: 500,
    stops: [] as RouteTarget[],
    /** When set, a panel post runs it first: a test holds the pass in flight there. */
    sendGate: null as (() => Promise<void>) | null,
    snapshots: 0,
  };
  const calls: Call[] = [];
  const panels = new LanePanels({
    slotId: "slot", chatId: CHAT, store,
    snapshot: async () => { state.snapshots++; return state.snapshot; },
    boundSession: target => store.getRoute("slot", target.chatId, target.topicId)?.sessionId ?? null,
    isBusy: () => state.busy,
    stop: async target => { state.stops.push(target); return true; },
    excludeTopic: topicId => topicId === "7",
    miniAppLink: options.miniAppLink,
    now: () => state.now,
    formatClock: () => "14:05",
    transport: {
      send: async (_target, text, markup) => {
        if (state.sendGate) await state.sendGate();
        const messageId = state.nextMessageId++;
        calls.push({ op: "send", messageId, text, markup });
        return { messageId };
      },
      edit: async (_target, messageId, text, markup) => {
        calls.push({ op: "edit", messageId, text, markup });
        return state.editOutcome;
      },
      pin: async (_target, messageId) => { calls.push({ op: "pin", messageId }); },
    },
  });
  const buttons = (): Record<string, string> => {
    const last = calls.filter(call => call.markup).at(-1)!;
    return Object.fromEntries(last.markup!.inline_keyboard.flat().map(button => [button.text, button.callback_data ?? button.url]));
  };
  return { store, state, calls, panels, buttons };
}

test("the panel is compact: state, short model, last action, child lanes and the open question", () => {
  const child = lane({ id: "s1/1-Fix", name: "Fix", kind: "subagent", parentId: "s1", elapsedMs: 2 * MIN, lastAction: "edit: lane-panel.ts", model: null });
  const done = lane({ id: "s1/2-Docs", name: "Docs", kind: "subagent", parentId: "s1", status: "idle", elapsedMs: null });
  const root = lane({ childIds: [child.id, done.id] });
  const text = renderLanePanel(root, snapshotOf([root, child, done], [
    { id: "q1", text: "Ship it <now>?", sessionId: "s1" },
    { id: "q2", text: "Other session", sessionId: "s9" },
  ]));
  expect(text).toBe([
    "🟢 <b>Running</b> · 5m",
    "<b>super-board</b>",
    "Model: <code>claude-opus-5-5:high</code>",
    "Last: bash: bun test",
    "",
    "<b>Lanes</b> · 1 running · 1 idle",
    "🟢 Fix · 2m — edit: lane-panel.ts",
    "⚪ Docs",
    "",
    "❓ <b>Waiting on you:</b> Ship it &lt;now&gt;?",
  ].join("\n"));
  expect(renderLanePanel(lane({ status: "idle", lastActivityMs: 1 }), snapshotOf([]), { formatClock: () => "14:05" }))
    .toStartWith("⚪ <b>Idle</b> · since 14:05\n");
  expect(renderLanePanel(null, snapshotOf([]), { title: "super-board" })).toBe("⚫ <b>Session ended</b>\n<b>super-board</b>");
});

test("one panel is posted and pinned, then edited in place only when its content changed", async () => {
  const f = fixture();
  await f.panels.tick();
  expect(f.calls.map(call => call.op)).toEqual(["send", "pin"]);
  expect(f.panels.messageId(TOPIC)).toBe(500);

  f.state.now += 31_000;
  await f.panels.tick();
  expect(f.calls).toHaveLength(2);
  expect(f.panels.stats.unchanged).toBe(1);

  f.state.snapshot = snapshotOf([lane({ lastAction: "bash: git push" })]);
  f.state.now += 31_000;
  await f.panels.tick();
  expect(f.calls.at(-1)).toMatchObject({ op: "edit", messageId: 500 });
  expect(f.calls.at(-1)!.text).toContain("git push");
  expect(f.panels.stats).toMatchObject({ sends: 1, edits: 1 });
});

test("edits keep the cadence: 30 s while running, 120 s while idle", async () => {
  const f = fixture();
  await f.panels.tick();
  for (const [step, action] of [[10_000, "a"], [10_000, "b"]] as const) {
    f.state.snapshot = snapshotOf([lane({ lastAction: action })]);
    f.state.now += step;
    await f.panels.tick();
  }
  expect(f.calls.filter(call => call.op === "edit")).toHaveLength(0);
  f.state.now += 10_000;
  await f.panels.tick();
  expect(f.calls.filter(call => call.op === "edit")).toHaveLength(1);

  f.state.snapshot = snapshotOf([lane({ status: "idle", lastActivityMs: 1 })]);
  f.state.now += 30_000;
  await f.panels.tick();
  expect(f.calls.filter(call => call.op === "edit")).toHaveLength(2);
  f.state.snapshot = snapshotOf([lane({ status: "idle", lastActivityMs: 1, lastAction: "changed" })]);
  f.state.now += 119_000;
  await f.panels.tick();
  expect(f.calls.filter(call => call.op === "edit")).toHaveLength(2);
  f.state.now += 1_000;
  await f.panels.tick();
  expect(f.calls.filter(call => call.op === "edit")).toHaveLength(3);
});

test("a failed edit retries on the next due pass; a deleted panel is posted again", async () => {
  const f = fixture();
  await f.panels.tick();
  f.state.snapshot = snapshotOf([lane({ lastAction: "next" })]);
  f.state.editOutcome = "error";
  f.state.now += 30_000;
  await f.panels.tick();
  f.state.editOutcome = "ok";
  f.state.now += 30_000;
  await f.panels.tick();
  expect(f.calls.filter(call => call.op === "edit")).toHaveLength(2);
  expect(f.panels.stats).toMatchObject({ edits: 1, failures: 1 });

  f.state.snapshot = snapshotOf([lane({ lastAction: "after delete" })]);
  f.state.editOutcome = "gone";
  f.state.now += 30_000;
  await f.panels.tick();
  expect(f.calls.slice(-2).map(call => call.op)).toEqual(["send", "pin"]);
  expect(f.panels.messageId(TOPIC)).toBe(501);
});

test("an ended session leaves its panel saying so, without buttons, and never gets a new one", async () => {
  const f = fixture();
  await f.panels.tick();
  f.state.snapshot = snapshotOf([]);
  f.state.now += 30_000;
  await f.panels.tick();
  expect(f.calls.at(-1)).toMatchObject({ op: "edit", text: "⚫ <b>Session ended</b>\n<b>super-board</b>" });
  expect(f.calls.at(-1)!.markup).toEqual({ inline_keyboard: [] });

  const fresh = fixture();
  fresh.state.snapshot = snapshotOf([]);
  await fresh.panels.tick();
  expect(fresh.calls).toEqual([]);
});

test("Stop uses the abort path; Steer and Follow-up arm the topic's next message once", async () => {
  const f = fixture();
  await f.panels.tick();
  const buttons = f.buttons();
  expect(Object.keys(buttons)).toEqual(["⏹ Stop", "🧭 Steer", "➕ Follow-up"]);
  for (const token of Object.values(buttons)) expect(token.startsWith(LANE_PANEL_CALLBACK_PREFIX)).toBe(true);

  expect(f.panels.peek(buttons["⏹ Stop"]!)).toBe("⏹ Stopping the current turn.");
  await f.panels.run(buttons["⏹ Stop"]!);
  expect(f.state.stops).toEqual([TOPIC]);

  expect(f.panels.takeArmed(TOPIC)).toBeNull();
  await f.panels.run(buttons["🧭 Steer"]!);
  expect(f.panels.takeArmed(TOPIC)).toBe("steer");
  expect(f.panels.takeArmed(TOPIC)).toBeNull();

  await f.panels.run(buttons["➕ Follow-up"]!);
  f.state.now += 5 * MIN;
  expect(f.panels.takeArmed(TOPIC)).toBeNull();
});

test("a double-tapped Stop stops the turn once; a later turn can be stopped again", async () => {
  const f = fixture();
  await f.panels.tick();
  const stop = f.buttons()["⏹ Stop"]!;

  // Both taps are answered at receipt before the ledger runs either.
  expect(f.panels.peek(stop)).toBe("⏹ Stopping the current turn.");
  expect(f.panels.peek(stop)).toBe(PANEL_ALREADY_STOPPING_ANSWER);
  await Promise.all([f.panels.run(stop), f.panels.run(stop)]);
  expect(f.state.stops).toEqual([TOPIC]);

  // A tap after the Stop went out, while the turn winds down, sends nothing either.
  f.state.now += 10_000;
  expect(f.panels.peek(stop)).toBe(PANEL_ALREADY_STOPPING_ANSWER);
  await f.panels.run(stop);
  expect(f.state.stops).toEqual([TOPIC]);

  // The turn ended; the next turn's Stop works again.
  f.state.busy = false;
  expect(f.panels.peek(stop)).toBe("Nothing to stop: the session is idle.");
  await f.panels.run(stop);
  expect(f.state.stops).toEqual([TOPIC]);
  f.state.busy = true;
  expect(f.panels.peek(stop)).toBe("⏹ Stopping the current turn.");
  await f.panels.run(stop);
  expect(f.state.stops).toEqual([TOPIC, TOPIC]);

  // A Stop the turn ignored lapses after the settle window, so the operator can press it again.
  f.state.now += 31_000;
  expect(f.panels.peek(stop)).toBe("⏹ Stopping the current turn.");
  await f.panels.run(stop);
  expect(f.state.stops).toEqual([TOPIC, TOPIC, TOPIC]);
});

test("after stop() nothing is posted, pinned or edited, not even by the pass in flight", async () => {
  const f = fixture();
  const entered = Promise.withResolvers<void>();
  const release = Promise.withResolvers<void>();
  f.state.sendGate = async () => { entered.resolve(); await release.promise; };
  const inFlight = f.panels.tick();
  await entered.promise;
  const stopped = f.panels.stop();
  release.resolve();
  await stopped;
  await inFlight;
  // The post already under way lands; its pin does not follow.
  expect(f.calls.map(call => call.op)).toEqual(["send"]);
  const snapshots = f.state.snapshots;

  f.state.sendGate = null;
  f.state.now += 10 * MIN;
  f.state.snapshot = snapshotOf([lane({ lastAction: "edit: something new" })]);
  await f.panels.tick();
  f.panels.start();
  await f.panels.stop();
  expect(f.calls.map(call => call.op)).toEqual(["send"]);
  // Not even a fleet snapshot is read once stopped.
  expect(f.state.snapshots).toBe(snapshots);

  // Stopped before its pass began: that pass posts nothing at all.
  const g = fixture();
  const pass = g.panels.tick();
  await g.panels.stop();
  await pass;
  expect(g.calls).toEqual([]);
});

test("panel state, tokens and arms go when the lane ends, the tokens expire or the topic loses its route", async () => {
  const f = fixture();
  await f.panels.tick();
  await f.panels.run(f.buttons()["🧭 Steer"]!);
  expect(f.panels.memory()).toEqual({ states: 1, tokens: 3, armed: 1 });

  // The lane ends: its buttons and its armed Steer are dropped with it.
  f.state.snapshot = snapshotOf([]);
  f.state.now += 2 * MIN;
  await f.panels.tick();
  expect(f.panels.memory()).toEqual({ states: 1, tokens: 0, armed: 0 });
  expect(f.panels.takeArmed(TOPIC)).toBeNull();

  // The topic loses its route: nothing about it is kept.
  f.store.deleteRoute("slot", CHAT, TOPIC.topicId);
  await f.panels.tick();
  expect(f.panels.memory()).toEqual({ states: 0, tokens: 0, armed: 0 });

  // Edits keep failing, so no fresh keyboard replaces the tokens: they and the arm are dropped once expired.
  const g = fixture();
  await g.panels.tick();
  await g.panels.run(g.buttons()["➕ Follow-up"]!);
  g.state.editOutcome = "error";
  for (let step = 1; step <= 31; step++) {
    g.state.now += 2 * MIN;
    g.state.snapshot = snapshotOf([lane({ lastAction: `step ${step}` })]);
    await g.panels.tick();
  }
  expect(g.panels.stats.failures).toBe(31);
  expect(g.panels.memory()).toEqual({ states: 1, tokens: 0, armed: 0 });
});

test("the panel post and its edits draw on the governor's panel queue; other sends stay messages", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "lane-panel-kind-"));
  const kinds: Array<string | undefined> = [];
  class RecordingGovernor extends TelegramGovernor {
    override schedule(...args: Parameters<TelegramGovernor["schedule"]>): Promise<Response> {
      kinds.push(args[0].kind);
      return super.schedule(...args);
    }
  }
  const callbacks: PollerCallbacks = {
    isIdle: () => true, onUserMessage: () => {}, onFollowUp: () => {}, onSteer: () => {}, onAbort: () => {}, onRelease: async () => {},
    getStatusText: () => "test", onLedgerFailure: () => {},
  };
  const poller = new TelegramPoller("0:test-only", dir, { dmPolicy: "allowlist", allowFrom: ["1"] }, callbacks, undefined, {
    sendTimeoutMs: 1000, outboundPaceMs: 0, governor: new RecordingGovernor({ chatIntervalMs: 0 }),
  });
  cleanup.push(() => { poller.stop(); fs.rmSync(dir, { recursive: true, force: true }); });
  globalThis.fetch = (async () => Response.json({ ok: true, result: { message_id: 9, chat: { id: Number(CHAT) } } })) as typeof fetch;

  await poller.sendTelegramMessage(CHAT, "<b>panel</b>", "HTML", { inline_keyboard: [] }, { sessionId: "s1" }, undefined, 42, "panel");
  await poller.editTelegramMessage(CHAT, 9, "<b>panel</b>", "HTML", undefined, undefined, "panel");
  await poller.pinTelegramMessage(CHAT, 9, "panel");
  await poller.sendTelegramMessage(CHAT, "a reply", "HTML", undefined, undefined, undefined, 42);
  expect(kinds).toEqual(["panel", "panel", "panel", "message"]);
});

test("stale buttons answer expired and do nothing: TTL, a newer keyboard, a rebound topic", async () => {
  const f = fixture();
  await f.panels.tick();
  const first = f.buttons();

  // The keyboard is reissued after half the TTL; the edit carrying it expires the old tokens.
  f.state.now += 31 * MIN;
  await f.panels.tick();
  const second = f.buttons();
  expect(second["⏹ Stop"]).not.toBe(first["⏹ Stop"]);
  expect(f.panels.peek(first["⏹ Stop"]!)).toBe(PANEL_EXPIRED_ANSWER);
  await f.panels.run(first["⏹ Stop"]!);
  expect(f.state.stops).toEqual([]);

  f.store.putRoute({ slotId: "slot", chatId: CHAT, topicId: TOPIC.topicId, sessionId: "s2", workspace: "C:/w" });
  expect(f.panels.peek(second["⏹ Stop"]!)).toBe(PANEL_EXPIRED_ANSWER);

  const ttl = fixture();
  await ttl.panels.tick();
  const token = ttl.buttons()["🧭 Steer"]!;
  ttl.state.now += 60 * MIN;
  expect(ttl.panels.peek(token)).toBe(PANEL_EXPIRED_ANSWER);
  expect(ttl.panels.peek(`${LANE_PANEL_CALLBACK_PREFIX}never-issued`)).toBe(PANEL_EXPIRED_ANSWER);
});

test("Open in Mini App appears only when configured, deep-linked to the topic", async () => {
  const off = fixture();
  await off.panels.tick();
  expect(Object.keys(off.buttons())).not.toContain("Open in Mini App");

  const on = fixture({ miniAppLink: "https://t.me/sb_bot/app" });
  await on.panels.tick();
  expect(on.buttons()["Open in Mini App"]).toBe("https://t.me/sb_bot/app?startapp=topic_42");
});

test("topics excluded by the daemon, such as the Questions topic, get no panel", async () => {
  const f = fixture();
  f.store.deleteRoute("slot", CHAT, TOPIC.topicId);
  f.store.putRoute({ slotId: "slot", chatId: CHAT, topicId: "7", sessionId: "s1", workspace: "C:/w" });
  f.store.putRoute({ slotId: "slot", chatId: "123", topicId: "", sessionId: "s1", workspace: "C:/w" });
  await f.panels.tick();
  expect(f.calls).toEqual([]);
});

function pollerFixture(fromId: number) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "lane-panel-poller-"));
  const calls: Array<{ method: string; body: Record<string, unknown> }> = [];
  const ran: string[] = [];
  const decided: string[] = [];
  const bridge: MessageCorrelationBridge = {
    getSessionId: () => "s1", getSlotId: () => "slot", record: () => {},
    resolveReply: () => ({ decision: "reject_unknown", detail: "Unknown message" }),
    resolveCallback: () => ({ decision: "reject_unknown", detail: "Unknown" }),
    consumeCallback: () => false,
  };
  const callbacks: PollerCallbacks = {
    isIdle: () => true, onUserMessage: () => {}, onFollowUp: () => {}, onSteer: () => {}, onAbort: () => {}, onRelease: async () => {},
    getStatusText: () => "test", onLedgerFailure: () => {},
    onDecisionCallback: async id => { decided.push(id); },
    lanePanel: { peek: data => `peeked ${data}`, run: async data => { ran.push(data); } },
  };
  const poller = new TelegramPoller("0:test-only", dir, { dmPolicy: "allowlist", allowFrom: ["1"], groups: { [CHAT]: {} } }, callbacks, bridge, { sendTimeoutMs: 1000 });
  cleanup.push(() => { poller.stop(); fs.rmSync(dir, { recursive: true, force: true }); });
  const update: TelegramUpdate = { update_id: 1, callback_query: {
    id: "click-1", from: { id: fromId, is_bot: false, first_name: "Op" }, data: `${LANE_PANEL_CALLBACK_PREFIX}tok`,
    message: { message_id: 500, chat: { id: Number(CHAT), type: "supergroup" }, date: 0, text: "panel", message_thread_id: 42 },
  } } as TelegramUpdate;
  let polls = 0;
  globalThis.fetch = (async (input, init) => {
    const url = new URL(String(input));
    if (url.pathname.endsWith("/getUpdates")) {
      if (++polls === 1) return Response.json({ ok: true, result: [update] });
      poller.stop();
      throw new Error("stopped");
    }
    calls.push({ method: url.pathname.split("/").pop()!, body: JSON.parse(String(init?.body ?? "{}")) });
    return Response.json({ ok: true });
  }) as typeof fetch;
  return { poller, calls, ran, decided };
}

test("a panel click is answered with its outcome at receipt and acted on once from the ledger", async () => {
  const f = pollerFixture(1);
  await f.poller.start();
  const answers = f.calls.filter(call => call.method === "answerCallbackQuery");
  expect(answers.map(call => call.body.text)).toEqual([`peeked ${LANE_PANEL_CALLBACK_PREFIX}tok`]);
  expect(f.ran).toEqual([`${LANE_PANEL_CALLBACK_PREFIX}tok`]);
  expect(f.decided).toEqual([]);
});

test("a panel click from an account outside the allowlist learns nothing and runs nothing", async () => {
  const f = pollerFixture(99);
  await f.poller.start();
  expect(f.calls.filter(call => call.method === "answerCallbackQuery").map(call => call.body.text))
    .toEqual(["Received; checking selection."]);
  expect(f.ran).toEqual([]);
});
