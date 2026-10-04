/**
 * lane-panel.ts — one live panel per forum topic (issue #429, parent #425).
 *
 * Every topic bound to a live session gets one pinned message: the session's state, model, elapsed time,
 * last action, child lanes and open question, with Stop, Steer and Follow-up buttons. The message is edited
 * in place, only when its content changed (content hash), and at most once per `activeIntervalMs` while the
 * session runs and once per `idleIntervalMs` while it idles. The daemon sends the edits as `panel` calls,
 * so they draw on the governor's reserved panel budget and coalesce with any edit still queued.
 *
 * Message ids and content hashes live in the daemon store, so a restart keeps editing the same message.
 * Button tokens live in memory only. A restart, a newer keyboard, a rebind of the topic or the token TTL
 * makes an older token answer {@link PANEL_EXPIRED_ANSWER} and do nothing.
 */

import { createHash, randomBytes } from "node:crypto";
import { LANE_PANEL_CALLBACK_PREFIX } from "../extension/poller";
import { escapeHtml } from "../extension/sanitizer";
import type { FleetLane, FleetSnapshot } from "./fleet-state";
import type { RouteTarget } from "./router";
import type { DaemonStore } from "./store";

export type PanelAction = "stop" | "steer" | "followUp";
export type PanelOutcome = "ok" | "gone" | "error";
type Markup = { inline_keyboard: Array<Array<Record<string, string>>> };

export const PANEL_EXPIRED_ANSWER = "⌛ Expired. Use the buttons on the current panel.";
export const PANEL_ALREADY_STOPPING_ANSWER = "⏹ Already stopping.";

export interface PanelTransport {
  /** Posts a new panel into the topic. `gone` means the topic no longer exists. */
  send(target: RouteTarget, text: string, markup: Markup, sessionId: string): Promise<{ messageId: number } | "gone" | "error">;
  /** Edits the panel. `gone` means the message was deleted; `error` covers a refused or dropped edit. */
  edit(target: RouteTarget, messageId: number, text: string, markup: Markup): Promise<PanelOutcome>;
  pin(target: RouteTarget, messageId: number): Promise<void>;
}

export interface LanePanelOptions {
  slotId: string;
  /** The forum supergroup whose topics carry panels. */
  chatId: string;
  store: Pick<DaemonStore, "getKv" | "setKv" | "deleteKv" | "listRoutes">;
  transport: PanelTransport;
  snapshot(): Promise<FleetSnapshot>;
  /** Session bound to the topic right now. A token issued for another session is stale. */
  boundSession(target: RouteTarget): string | null;
  isBusy(target: RouteTarget): boolean;
  /** Stops the bound session's turn through the daemon's existing abort path. */
  stop(target: RouteTarget): Promise<boolean>;
  /** Topics that never get a panel, such as the Questions topic. */
  excludeTopic?(topicId: string): boolean;
  /** Mini App direct link (`https://t.me/<bot>/<app>`). Unset leaves the button out. */
  miniAppLink?: string;
  now?(): number;
  /** Wall-clock time for "idle since"; defaults to the host's local HH:MM. */
  formatClock?(ms: number): string;
  log?(message: string): void;
  activeIntervalMs?: number;
  idleIntervalMs?: number;
  tokenTtlMs?: number;
  /** How long a Steer or Follow-up click waits for the operator's next message. */
  armTtlMs?: number;
  /** How long a sent Stop makes further Stop taps answer "already stopping" while the turn winds down. */
  stopSettleMs?: number;
}

interface Keyboard {
  id: string;
  sessionId: string;
  issuedAt: number;
  tokens: Record<PanelAction, string>;
}

interface TokenRecord {
  key: string;
  keyboardId: string;
  target: RouteTarget;
  sessionId: string;
  action: PanelAction;
  expiresAt: number;
}

interface PanelState {
  lastAttemptAt: number;
  running: boolean;
  /** The keyboard on the message in Telegram. */
  keyboard: Keyboard | null;
  /**
   * The topic's Stop in progress. A tap claims it at receipt (`peek`), the ledger sends it once (`run`).
   * Further taps while it is set answer "already stopping" and send nothing.
   */
  stop: { phase: "claimed" | "sent"; at: number } | null;
}

export interface PanelStats {
  sends: number;
  edits: number;
  /** Due passes that found the content unchanged and made no call. */
  unchanged: number;
  failures: number;
}

const ACTION_LABELS: Record<PanelAction, string> = { stop: "⏹ Stop", steer: "🧭 Steer", followUp: "➕ Follow-up" };
const MAX_CHILD_LINES = 6;

function truncate(text: string, max: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

/** Coarse on purpose: minutes under a day, hours beyond, so a running panel changes at most once a minute. */
export function formatElapsed(ms: number): string {
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return "<1m";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

const localClock = (ms: number): string =>
  new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });

function descendants(root: FleetLane, byId: Map<string, FleetLane>): Array<{ lane: FleetLane; depth: number }> {
  const out: Array<{ lane: FleetLane; depth: number }> = [];
  const walk = (lane: FleetLane, depth: number): void => {
    for (const id of lane.childIds) {
      const child = byId.get(id);
      if (!child) continue;
      out.push({ lane: child, depth });
      walk(child, depth + 1);
    }
  };
  walk(root, 0);
  return out;
}

/** The panel text for a live session, or for one that ended (`lane` null). Telegram HTML. */
export function renderLanePanel(
  lane: FleetLane | null,
  snapshot: FleetSnapshot,
  options: { title?: string | null; formatClock?: (ms: number) => string } = {},
): string {
  const clock = options.formatClock ?? localClock;
  if (!lane) {
    const title = options.title ? `\n<b>${escapeHtml(truncate(options.title, 60))}</b>` : "";
    return `⚫ <b>Session ended</b>${title}`;
  }
  const lines: string[] = [];
  lines.push(lane.status === "running"
    ? `🟢 <b>Running</b>${lane.elapsedMs === null ? "" : ` · ${formatElapsed(lane.elapsedMs)}`}`
    : `⚪ <b>Idle</b>${lane.lastActivityMs > 0 ? ` · since ${clock(lane.lastActivityMs)}` : ""}`);
  lines.push(`<b>${escapeHtml(truncate(lane.name, 60))}</b>`);
  // `anthropic/claude-opus-5-5:high` reads as `claude-opus-5-5:high` on a phone.
  if (lane.model) lines.push(`Model: <code>${escapeHtml(lane.model.split("/").pop() || lane.model)}</code>`);
  if (lane.lastAction) lines.push(`Last: ${escapeHtml(truncate(lane.lastAction, 120))}`);

  const children = descendants(lane, new Map(snapshot.lanes.map(entry => [entry.id, entry])));
  if (children.length) {
    const running = children.filter(child => child.lane.status === "running").length;
    lines.push("", `<b>Lanes</b> · ${running} running · ${children.length - running} idle`);
    for (const { lane: child, depth } of children.slice(0, MAX_CHILD_LINES)) {
      const dot = child.status === "running" ? "🟢" : "⚪";
      const elapsed = child.status === "running" && child.elapsedMs !== null ? ` · ${formatElapsed(child.elapsedMs)}` : "";
      const action = child.status === "running" && child.lastAction ? ` — ${escapeHtml(truncate(child.lastAction, 48))}` : "";
      lines.push(`${"  ".repeat(depth)}${depth ? "↳ " : ""}${dot} ${escapeHtml(truncate(child.name, 28))}${elapsed}${action}`);
    }
    if (children.length > MAX_CHILD_LINES) lines.push(`+${children.length - MAX_CHILD_LINES} more`);
  }

  const questions = snapshot.questions.filter(question => question.sessionId === lane.id);
  if (questions.length) {
    const more = questions.length > 1 ? ` (+${questions.length - 1} more)` : "";
    lines.push("", `❓ <b>Waiting on you:</b> ${escapeHtml(truncate(questions[0]!.text, 140))}${more}`);
  }
  return lines.join("\n");
}

export class LanePanels {
  private readonly now: () => number;
  private readonly activeIntervalMs: number;
  private readonly idleIntervalMs: number;
  private readonly tokenTtlMs: number;
  private readonly armTtlMs: number;
  private readonly stopSettleMs: number;
  private readonly states = new Map<string, PanelState>();
  private readonly tokens = new Map<string, TokenRecord>();
  private readonly armed = new Map<string, { mode: "steer" | "followUp"; until: number }>();
  private chain: Promise<void> = Promise.resolve();
  private timer: Timer | undefined;
  /** Set by {@link stop}: nothing is scheduled, sent or edited afterwards. */
  private stopped = false;
  public readonly stats: PanelStats = { sends: 0, edits: 0, unchanged: 0, failures: 0 };

  constructor(private readonly options: LanePanelOptions) {
    this.now = options.now ?? Date.now;
    this.activeIntervalMs = options.activeIntervalMs ?? 30_000;
    this.idleIntervalMs = options.idleIntervalMs ?? 120_000;
    this.tokenTtlMs = options.tokenTtlMs ?? 60 * 60_000;
    this.armTtlMs = options.armTtlMs ?? 5 * 60_000;
    this.stopSettleMs = options.stopSettleMs ?? 30_000;
  }

  private log(message: string): void {
    this.options.log?.(`[Lane panel] ${message}`);
  }

  private kvKey(target: RouteTarget, name: string): string {
    return `lanepanel:${this.options.slotId}:${target.chatId}:${target.topicId}:${name}`;
  }

  /** Checks every `tickMs` which panels are due. A pass never overlaps the previous one. */
  start(tickMs = 10_000): void {
    if (this.timer || this.stopped) return;
    void this.tick();
    this.timer = setInterval(() => void this.tick(), tickMs);
    this.timer.unref?.();
  }

  /**
   * Stops for good: no further pass, send or edit, even from a pass already in flight.
   * Resolves once that pass is done, so the caller may close the store.
   */
  stop(): Promise<void> {
    this.stopped = true;
    clearInterval(this.timer);
    this.timer = undefined;
    return this.chain;
  }

  /** One pass over the due panels. Serialized, and it never rejects. */
  tick(): Promise<void> {
    if (this.stopped) return this.chain;
    const run = this.chain.then(() => this.pass()).catch(error => this.log(`pass failed: ${String(error)}`));
    this.chain = run;
    return run;
  }

  /** Entries held in memory, for diagnostics and tests. Bounded by the topics that still have a route. */
  memory(): { states: number; tokens: number; armed: number } {
    return { states: this.states.size, tokens: this.tokens.size, armed: this.armed.size };
  }

  private routes(): Array<{ target: RouteTarget; sessionId: string }> {
    return this.options.store.listRoutes(this.options.slotId)
      .filter(route => route.chatId === this.options.chatId && route.topicId !== "" && !this.options.excludeTopic?.(route.topicId))
      .map(route => ({ target: { chatId: route.chatId, topicId: route.topicId }, sessionId: route.sessionId }));
  }

  private async pass(): Promise<void> {
    const now = this.now();
    const routes = this.routes();
    this.prune(new Set(routes.map(({ target }) => this.kvKey(target, ""))), now);
    const due = routes.filter(({ target }) => {
      const state = this.states.get(this.kvKey(target, ""));
      return !state || now - state.lastAttemptAt >= (state.running ? this.activeIntervalMs : this.idleIntervalMs);
    });
    if (!due.length) return;
    const snapshot = await this.options.snapshot();
    for (const route of due) {
      if (this.stopped) return;
      await this.update(route.target, route.sessionId, snapshot);
    }
  }

  /**
   * Drops what can no longer be used: expired tokens and arms, and everything held for a topic whose
   * route is gone. The daemon runs for weeks, so nothing here may outlive its topic.
   */
  private prune(liveKeys: Set<string>, now: number): void {
    for (const [token, record] of this.tokens) if (now >= record.expiresAt) this.tokens.delete(token);
    for (const [key, armed] of this.armed) if (armed.until <= now || !liveKeys.has(key)) this.armed.delete(key);
    for (const [key, state] of this.states) {
      if (liveKeys.has(key)) continue;
      this.revoke(state);
      this.states.delete(key);
    }
  }

  private keyboardFor(state: PanelState, sessionId: string, now: number): Keyboard {
    const current = state.keyboard;
    if (current && current.sessionId === sessionId && now - current.issuedAt < this.tokenTtlMs / 2) return current;
    const token = (): string => `${LANE_PANEL_CALLBACK_PREFIX}${randomBytes(9).toString("base64url")}`;
    return { id: randomBytes(6).toString("hex"), sessionId, issuedAt: now, tokens: { stop: token(), steer: token(), followUp: token() } };
  }

  private markup(target: RouteTarget, keyboard: Keyboard | null): Markup {
    const rows: Markup["inline_keyboard"] = [];
    if (keyboard) {
      rows.push((Object.keys(ACTION_LABELS) as PanelAction[]).map(action => ({ text: ACTION_LABELS[action], callback_data: keyboard.tokens[action] })));
    }
    const link = this.options.miniAppLink;
    if (keyboard && link) {
      rows.push([{ text: "Open in Mini App", url: `${link}${link.includes("?") ? "&" : "?"}startapp=topic_${target.topicId}` }]);
    }
    return { inline_keyboard: rows };
  }

  /** Makes `keyboard` the one on the message: its tokens become valid and every older one expires. */
  private commit(key: string, state: PanelState, target: RouteTarget, keyboard: Keyboard | null): void {
    if (state.keyboard === keyboard) return;
    this.revoke(state);
    state.keyboard = keyboard;
    if (!keyboard) return;
    for (const action of Object.keys(keyboard.tokens) as PanelAction[]) {
      this.tokens.set(keyboard.tokens[action], {
        key, keyboardId: keyboard.id, target, sessionId: keyboard.sessionId, action,
        expiresAt: keyboard.issuedAt + this.tokenTtlMs,
      });
    }
  }

  private revoke(state: PanelState): void {
    if (state.keyboard) for (const token of Object.values(state.keyboard.tokens)) this.tokens.delete(token);
  }

  private async update(target: RouteTarget, sessionId: string, snapshot: FleetSnapshot): Promise<void> {
    const { store, transport } = this.options;
    const now = this.now();
    const key = this.kvKey(target, "");
    let state = this.states.get(key);
    if (!state) this.states.set(key, state = { lastAttemptAt: now, running: false, keyboard: null, stop: null });
    const lane = snapshot.lanes.find(entry => entry.id === sessionId && entry.kind === "interactive") ?? null;
    state.lastAttemptAt = now;
    state.running = lane?.status === "running";
    // A Stop is settled once the turn is over.
    if (!state.running) state.stop = null;
    if (!lane) {
      // The session ended: its buttons are dead now, even if the edit that removes them fails.
      this.revoke(state);
      state.keyboard = null;
      this.armed.delete(key);
    }

    const storedId = Number(store.getKv(this.kvKey(target, "id")) ?? 0);
    // A topic whose session is not running gets no new panel; an existing one shows that it ended.
    if (!lane && !storedId) return;
    if (lane) store.setKv(this.kvKey(target, "title"), lane.name);
    const keyboard = lane ? this.keyboardFor(state, sessionId, now) : null;
    const text = renderLanePanel(lane, snapshot, { title: store.getKv(this.kvKey(target, "title")), formatClock: this.options.formatClock });
    const markup = this.markup(target, keyboard);
    const hash = createHash("sha256").update(text).update(JSON.stringify(markup)).digest("hex");

    let messageId = storedId;
    if (messageId) {
      if (store.getKv(this.kvKey(target, "hash")) === hash) {
        this.stats.unchanged++;
        return;
      }
      const outcome = await transport.edit(target, messageId, text, markup);
      if (outcome === "ok") {
        this.stats.edits++;
        store.setKv(this.kvKey(target, "hash"), hash);
        this.commit(key, state, target, keyboard);
        return;
      }
      if (outcome === "error") {
        // Refused, dropped or rate limited: the hash stays old, so the next due pass tries again.
        this.stats.failures++;
        this.log(`topic ${target.topicId}: panel edit failed; retrying on the next pass`);
        return;
      }
      this.log(`topic ${target.topicId}: panel message ${messageId} is gone; posting a new one`);
      store.deleteKv(this.kvKey(target, "id"));
      store.deleteKv(this.kvKey(target, "hash"));
      this.revoke(state);
      state.keyboard = null;
      messageId = 0;
      if (!lane) return;
    }

    const sent = await transport.send(target, text, markup, sessionId);
    if (sent === "gone" || sent === "error") {
      this.stats.failures++;
      // Back off to the idle cadence: a topic that refuses posts must not be retried every 30 s.
      state.running = false;
      this.log(`topic ${target.topicId}: panel post failed (${sent})`);
      return;
    }
    this.stats.sends++;
    store.setKv(this.kvKey(target, "id"), String(sent.messageId));
    store.setKv(this.kvKey(target, "hash"), hash);
    this.commit(key, state, target, keyboard);
    if (this.stopped) return;
    await transport.pin(target, sent.messageId).catch(error => this.log(`topic ${target.topicId}: pin failed: ${String(error)}`));
  }

  /** Message id of the topic's panel, or null when it has none. */
  messageId(target: RouteTarget): number | null {
    const value = this.options.store.getKv(this.kvKey(target, "id"));
    return value === null ? null : Number(value);
  }

  private resolve(data: string): TokenRecord | null {
    const record = this.tokens.get(data);
    if (!record) return null;
    const state = this.states.get(record.key);
    if (this.now() >= record.expiresAt || state?.keyboard?.id !== record.keyboardId
        || this.options.boundSession(record.target) !== record.sessionId) {
      this.tokens.delete(data);
      return null;
    }
    return record;
  }

  /**
   * The answer shown on the click itself, at receipt. Steer and Follow-up only answer; the first Stop tap
   * claims the topic's Stop, so {@link run} sends it once and every further tap answers "already stopping".
   */
  peek(data: string): string {
    const record = this.resolve(data);
    if (!record) return PANEL_EXPIRED_ANSWER;
    if (record.action === "stop") {
      const state = this.states.get(record.key);
      if (!state) return PANEL_EXPIRED_ANSWER;
      const now = this.now();
      if (!this.options.isBusy(record.target)) {
        state.stop = null;
        return "Nothing to stop: the session is idle.";
      }
      // A claim whose run never came, or a sent Stop the turn ignored, lapses after the settle window.
      if (state.stop && now - state.stop.at < this.stopSettleMs) {
        return PANEL_ALREADY_STOPPING_ANSWER;
      }
      state.stop = { phase: "claimed", at: now };
      return "⏹ Stopping the current turn.";
    }
    return record.action === "steer"
      ? "🧭 Send your steer as the next message in this topic."
      : "➕ Send your follow-up as the next message in this topic. It runs after the current turn.";
  }

  /**
   * Acts on a click from the inbound ledger. A stale token does nothing; it was answered as expired.
   * A Stop runs only for the tap that claimed it in {@link peek}, and only once.
   */
  async run(data: string): Promise<void> {
    const record = this.resolve(data);
    if (!record) {
      this.log("stale panel button ignored");
      return;
    }
    if (record.action !== "stop") {
      this.armed.set(record.key, { mode: record.action, until: this.now() + this.armTtlMs });
      return;
    }
    const state = this.states.get(record.key);
    if (state?.stop?.phase !== "claimed") {
      this.log(`topic ${record.target.topicId}: stop already sent or not claimed; ignored`);
      return;
    }
    // Claimed and sent in one synchronous step, so a second run of the same tap cannot slip in.
    state.stop = { phase: "sent", at: this.now() };
    try {
      const stopped = await this.options.stop(record.target);
      this.log(`topic ${record.target.topicId}: stop ${stopped ? "sent" : "found no active turn"}`);
      if (!stopped) state.stop = null;
    } catch (error) {
      state.stop = null;
      this.log(`topic ${record.target.topicId}: stop failed: ${String(error)}`);
    }
  }

  /** The delivery mode a Steer or Follow-up click armed for this topic's next message; consumed once. */
  takeArmed(target: RouteTarget): "steer" | "followUp" | null {
    const key = this.kvKey(target, "");
    const armed = this.armed.get(key);
    this.armed.delete(key);
    return armed && armed.until > this.now() ? armed.mode : null;
  }
}
