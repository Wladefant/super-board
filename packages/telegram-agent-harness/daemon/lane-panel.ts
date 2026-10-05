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

import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
import { LANE_PANEL_CALLBACK_PREFIX } from "../extension/poller";
import { escapeHtml } from "../extension/sanitizer";
import { getDaemonRunDir } from "./config";
import type { FleetLane, FleetSnapshot } from "./fleet-state";
import type { RouteTarget } from "./router";
import type { DaemonStore } from "./store";
export interface PanelCallbackContext {
  userId: string;
  chatId: string;
  topicId: string;
  eventId: string;
  messageId?: number;
}

export type PanelAction = "stop" | "steer" | "followUp" | "confirm" | "cancel";
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
  confirm(target: RouteTarget, text: string, markup: Markup, userId: string): Promise<{ ephemeralMessageId: number } | "error">;
  editConfirm(target: RouteTarget, id: number, text: string, markup: Markup, userId: string): Promise<PanelOutcome>;
  prompt(target: RouteTarget, text: string, sessionId: string): Promise<{ messageId: number } | "error">;
}

export interface LanePanelOptions {
  slotId: string;
  /** The forum supergroup whose topics carry panels. */
  chatId: string;
  operatorId: string;
  store: Pick<DaemonStore, "getKv" | "setKv" | "deleteKv" | "listRoutes" | "auditControl"> & Partial<Pick<DaemonStore, "getControlAudit">> & { stateDir?: string; dbPath?: string };
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
  tokens: Record<"stop" | "steer" | "followUp", string>;
  records?: Record<"stop" | "steer" | "followUp", TokenRecord>;
}

interface TokenRecord {
  key: string;
  keyboardId?: string;
  target: RouteTarget;
  sessionId: string;
  action: PanelAction;
  expiresAt: number;
  nonce: string;
  token: string;
  consumed: boolean;
  pairedToken?: string;
  ephemeralMessageId?: number;
}

interface ArmedRecord {
  mode: "steer" | "followUp";
  messageId: number;
  userId: string;
  sessionId: string;
  expiresAt: number;
}

export function computeTokenHmac(
  secret: Buffer,
  sessionId: string,
  chatId: string,
  topicId: string,
  operatorId: string,
  expiresAt: number,
  action: PanelAction | string,
  nonce: string,
): string {
  const payload = `${sessionId}:${chatId}:${topicId}:${operatorId}:${expiresAt}:${action}:${nonce}`;
  return createHmac("sha256", secret).update(payload).digest("base64url");
}

export interface DaemonStoreLike {
  stateDir?: string;
  dbPath?: string;
  db?: { filename?: string };
  getKv?: (key: string) => string | null;
  setKv?: (key: string, value: string) => void;
}

export function deriveDaemonStateDir(store?: unknown): string {
  if (store && typeof store === "object") {
    if ("stateDir" in store && typeof store.stateDir === "string" && store.stateDir.length > 0) {
      return store.stateDir;
    }
    if ("dbPath" in store && typeof store.dbPath === "string" && store.dbPath.length > 0) {
      return path.dirname(store.dbPath);
    }
    if ("db" in store && store.db && typeof store.db === "object" && "filename" in store.db) {
      const filename = store.db.filename;
      if (typeof filename === "string" && filename.length > 0 && filename !== ":memory:") {
        return path.dirname(filename);
      }
    }
  }
  return process.env.VEYYON_TELEGRAM_DAEMON_DIR || getDaemonRunDir();
}

export function getSlotSecretFilePath(stateDir: string, slotId: string): string {
  const identity = createHash("sha256").update(slotId).digest("hex");
  return path.join(stateDir, "secrets", `${identity}.secret`);
}


function enforceWindowsOwnerAcl(target: string, directory: boolean): void {
  const script = `
    $ErrorActionPreference = 'Stop'
    $target = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${Buffer.from(target).toString("base64")}'))
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $item = Get-Item -LiteralPath $target
    $acl = $item.GetAccessControl([Security.AccessControl.AccessControlSections]::Access)
    foreach ($old in @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) { [void]$acl.RemoveAccessRuleSpecific($old) }
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', '${directory ? "ContainerInherit, ObjectInherit" : "None"}', 'None', 'Allow')
    $acl.AddAccessRule($rule)
    $item.SetAccessControl($acl)
    $actual = Get-Acl -LiteralPath $target
    if (!$actual.AreAccessRulesProtected) { throw 'Unprotected secret ACL' }
    $rules = @($actual.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne 1 -or $rules[0].IdentityReference -ne $sid -or $rules[0].AccessControlType -ne 'Allow' -or $rules[0].FileSystemRights -ne 'FullControl') { throw 'Unexpected secret ACL' }
  `;
  const result = spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64")],
    { windowsHide: true, encoding: "utf8", timeout: 15_000 });
  if (result.error || result.status !== 0) throw new Error("Cannot enforce owner-only daemon secret ACL");
}

function protectSecretsDirectory(secretsDir: string): void {
  if (process.platform === "win32") {
    enforceWindowsOwnerAcl(secretsDir, true);
  } else {
    try {
      fs.chmodSync(secretsDir, 0o700);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      throw new Error(`Failed to enforce 0700 permissions on secrets directory ${secretsDir}: ${message}`);
    }
  }
}

function protectSecretFile(filePath: string, fd?: number): void {
  if (process.platform === "win32") {
    enforceWindowsOwnerAcl(filePath, false);
  } else {
    try {
      if (typeof fd === "number") {
        fs.fchmodSync(fd, 0o600);
      } else {
        fs.chmodSync(filePath, 0o600);
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      throw new Error(`Failed to enforce 0600 permissions on secret file ${filePath}: ${message}`);
    }
  }
}

function parseSecretBuffer(buf: Buffer): Buffer {
  const str = buf.toString("utf8");
  if (!/^[0-9a-fA-F]{64}$/.test(str)) throw new Error("Invalid daemon signing secret encoding");
  const secret = Buffer.from(str, "hex");
  if (secret.length !== 32) throw new Error("Invalid daemon signing secret length");
  return secret;
}

function readSecretFileWithRetry(filePath: string): Buffer {
  const start = Date.now();
  while (Date.now() - start < 3000) {
    try {
      if (fs.existsSync(filePath)) {
        protectSecretFile(filePath);
        const data = fs.readFileSync(filePath);
        if (data.length >= 32) {
          return parseSecretBuffer(data);
        }
      }
    } catch (e: unknown) {
      if (!(e && typeof e === "object" && "code" in e && e.code === "ENOENT")) throw e;
    }
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);
  }
  const finalData = fs.readFileSync(filePath);
  if (finalData.length < 32) {
    throw new Error(`Secret file at ${filePath} is incomplete or empty`);
  }
  return parseSecretBuffer(finalData);
}

export function getDaemonSecret(
  store?: Pick<DaemonStore, "getKv" | "setKv"> | DaemonStoreLike | unknown,
  slotId = "daemon",
): Buffer {
  const stateDir = deriveDaemonStateDir(store);
  const secretPath = getSlotSecretFilePath(stateDir, slotId);
  const secretsDir = path.dirname(secretPath);

  fs.mkdirSync(secretsDir, { recursive: true, mode: 0o700 });
  protectSecretsDirectory(secretsDir);
  if (fs.existsSync(secretPath)) {
    protectSecretFile(secretPath);
    return readSecretFileWithRetry(secretPath);
  }

  let fd: number | null = null;
  try {
    fd = fs.openSync(secretPath, "wx", 0o600);
  } catch (err: unknown) {
    if (err && typeof err === "object" && "code" in err && err.code === "EEXIST") {
      return readSecretFileWithRetry(secretPath);
    }
    throw err;
  }

  try {
    protectSecretFile(secretPath, fd);
    const secretHex = randomBytes(32).toString("hex");
    fs.writeSync(fd, secretHex);
    fs.closeSync(fd);
    fd = null;
    return Buffer.from(secretHex, "hex");
  } catch (err) {
    if (fd !== null) {
      try { fs.closeSync(fd); } catch {}
      fd = null;
    }
    try { fs.unlinkSync(secretPath); } catch {}
    throw err;
  }
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

const ACTION_LABELS: Record<"stop" | "steer" | "followUp", string> = { stop: "⏹ Stop", steer: "🧭 Steer", followUp: "➕ Follow-up" };
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
    ? `🟢 <b>Running</b>${lane.elapsedMs === null ? "" : ` · ${escapeHtml(formatElapsed(lane.elapsedMs))}`}`
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
      const elapsed = child.status === "running" && child.elapsedMs !== null ? ` · ${escapeHtml(formatElapsed(child.elapsedMs))}` : "";
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
  private readonly tokensByNonce = new Map<string, TokenRecord>();
  private readonly armed = new Map<string, ArmedRecord>();
  private readonly processedEvents = new Set<string>();
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
    return { states: this.states.size, tokens: this.tokensByNonce.size, armed: this.armed.size };
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
      const hasConsumed = Boolean(state?.keyboard?.records && Object.values(state.keyboard.records).some(r => r.consumed));
      return !state || hasConsumed || now - state.lastAttemptAt >= (state.running ? this.activeIntervalMs : this.idleIntervalMs);
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
    for (const [nonce, record] of this.tokensByNonce) {
      if (now >= record.expiresAt || !liveKeys.has(record.key)) {
        this.tokensByNonce.delete(nonce);
        this.tokens.delete(record.token);
      }
    }
    for (const [key, armed] of this.armed) {
      if (armed.expiresAt <= now || !liveKeys.has(key)) {
        this.armed.delete(key);
      }
    }
    for (const [key, state] of this.states) {
      if (liveKeys.has(key)) continue;
      this.revoke(state);
      this.states.delete(key);
    }
  }

  private signingSecret?: Buffer;
  private getSecret(): Buffer {
    return this.signingSecret ??= getDaemonSecret(this.options.store, this.options.slotId);
  }

  private createTokenRecord(
    key: string,
    target: RouteTarget,
    sessionId: string,
    action: PanelAction,
    expiresAt: number,
    keyboardId?: string,
    register = true,
  ): TokenRecord {
    const nonce = randomBytes(9).toString("base64url");
    const sig = computeTokenHmac(
      this.getSecret(),
      sessionId,
      target.chatId,
      target.topicId,
      this.options.operatorId,
      expiresAt,
      action,
      nonce,
    );
    const token = `${LANE_PANEL_CALLBACK_PREFIX}${nonce}:${sig}`;
    const record: TokenRecord = {
      key,
      keyboardId,
      target,
      sessionId,
      action,
      expiresAt,
      nonce,
      token,
      consumed: false,
    };
    if (register) {
      this.tokens.set(token, record);
      this.tokensByNonce.set(nonce, record);
    }
    return record;
  }

  private keyboardFor(state: PanelState, key: string, target: RouteTarget, sessionId: string, now: number): Keyboard {
    const current = state.keyboard;
    const hasConsumedTokens = current && current.records && Object.values(current.records).some(r => r.consumed);
    if (current && current.sessionId === sessionId && now - current.issuedAt < this.tokenTtlMs / 2 && !hasConsumedTokens) return current;
    const keyboardId = randomBytes(6).toString("hex");
    const expiresAt = now + this.tokenTtlMs;
    const stopRec = this.createTokenRecord(key, target, sessionId, "stop", expiresAt, keyboardId, false);
    const steerRec = this.createTokenRecord(key, target, sessionId, "steer", expiresAt, keyboardId, false);
    const followUpRec = this.createTokenRecord(key, target, sessionId, "followUp", expiresAt, keyboardId, false);
    return {
      id: keyboardId,
      sessionId,
      issuedAt: now,
      tokens: {
        stop: stopRec.token,
        steer: steerRec.token,
        followUp: followUpRec.token,
      },
      records: {
        stop: stopRec,
        steer: steerRec,
        followUp: followUpRec,
      },
    };
  }

  private markup(target: RouteTarget, keyboard: Keyboard | null): Markup {
    const rows: Markup["inline_keyboard"] = [];
    if (keyboard) {
      rows.push((["stop", "steer", "followUp"] as const).map(action => ({
        text: ACTION_LABELS[action],
        callback_data: keyboard.tokens[action],
      })));
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
    if (!keyboard?.records) return;
    for (const record of Object.values(keyboard.records)) {
      this.tokens.set(record.token, record);
      this.tokensByNonce.set(record.nonce, record);
    }
  }

  private revoke(state: PanelState): void {
    if (state.keyboard) {
      for (const token of Object.values(state.keyboard.tokens)) {
        const record = this.tokens.get(token);
        if (record) {
          this.tokensByNonce.delete(record.nonce);
        }
        this.tokens.delete(token);
      }
    }
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
    let keyboard = lane ? this.keyboardFor(state, key, target, sessionId, now) : null;
    const text = renderLanePanel(lane, snapshot, { title: store.getKv(this.kvKey(target, "title")), formatClock: this.options.formatClock });
    let markup = this.markup(target, keyboard);
    let hash = createHash("sha256").update(text).update(JSON.stringify(markup)).digest("hex");

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
      keyboard = this.keyboardFor(state, key, target, sessionId, now);
      markup = this.markup(target, keyboard);
      hash = createHash("sha256").update(text).update(JSON.stringify(markup)).digest("hex");
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

  private resolve(
    data: string,
    context?: PanelCallbackContext,
  ): {
    record?: TokenRecord;
    action: PanelAction | "unknown";
    sessionId: string;
    error?: "forged" | "unauthorized" | "expired" | "replay" | "stale" | "unknown";
  } {
    if (!data.startsWith(LANE_PANEL_CALLBACK_PREFIX)) {
      return { action: "unknown", sessionId: "", error: "unknown" };
    }
    const rest = data.slice(LANE_PANEL_CALLBACK_PREFIX.length);
    const colonIdx = rest.indexOf(":");
    if (colonIdx === -1) {
      return { action: "unknown", sessionId: "", error: "forged" };
    }
    const nonce = rest.slice(0, colonIdx);
    const sig = rest.slice(colonIdx + 1);

    const record = this.tokensByNonce.get(nonce) ?? this.tokens.get(data);
    if (!record) {
      return { action: "unknown", sessionId: "", error: "forged" };
    }

    const expectedSig = computeTokenHmac(
      this.getSecret(),
      record.sessionId,
      record.target.chatId,
      record.target.topicId,
      this.options.operatorId,
      record.expiresAt,
      record.action,
      record.nonce,
    );
    const sigBuf = Buffer.from(sig);
    const expBuf = Buffer.from(expectedSig);
    if (sigBuf.length !== expBuf.length || !timingSafeEqual(sigBuf, expBuf)) {
      return { record, action: record.action, sessionId: record.sessionId, error: "forged" };
    }

    if (!context || !context.userId || !context.chatId || !context.topicId) {
      return { record, action: record.action, sessionId: record.sessionId, error: "unauthorized" };
    }
    if (context.userId !== this.options.operatorId) {
      return { record, action: record.action, sessionId: record.sessionId, error: "unauthorized" };
    }
    if (context.chatId !== record.target.chatId || context.topicId !== record.target.topicId) {
      return { record, action: record.action, sessionId: record.sessionId, error: "unauthorized" };
    }

    if (this.now() >= record.expiresAt) {
      return { record, action: record.action, sessionId: record.sessionId, error: "expired" };
    }

    if (record.consumed) {
      return { record, action: record.action, sessionId: record.sessionId, error: "replay" };
    }

    if (this.options.boundSession(record.target) !== record.sessionId) {
      return { record, action: record.action, sessionId: record.sessionId, error: "stale" };
    }

    if (record.keyboardId) {
      const state = this.states.get(record.key);
      if (state?.keyboard?.id !== record.keyboardId) {
        return { record, action: record.action, sessionId: record.sessionId, error: "stale" };
      }
    }

    return { record, action: record.action, sessionId: record.sessionId };
  }

  /**
   * The answer shown on the click itself, at receipt.
   */
  peek(data: string, context?: PanelCallbackContext): string {
    const res = this.resolve(data, context);
    if (res.error || !res.record) return PANEL_EXPIRED_ANSWER;
    const record = res.record;
    if (record.action === "stop") {
      const state = this.states.get(record.key);
      if (!state) return PANEL_EXPIRED_ANSWER;
      const now = this.now();
      if (!this.options.isBusy(record.target)) {
        state.stop = null;
        return "Nothing to stop: the session is idle.";
      }
      if (state.stop && now - state.stop.at < this.stopSettleMs) {
        return PANEL_ALREADY_STOPPING_ANSWER;
      }
      state.stop = { phase: "claimed", at: now };
      return "Confirm stopping the current turn.";
    }
    if (record.action === "confirm") {
      return "⏹ Stopping the current turn.";
    }
    if (record.action === "cancel") {
      return "Stop cancelled.";
    }
    return record.action === "steer"
      ? "🧭 Send your steer as a reply to the prompt."
      : "➕ Send your follow-up as a reply to the prompt. It runs after the current turn.";
  }

  /**
   * Acts on a click from the inbound ledger. Validates and audits once per event.
   */
  async run(data: string, context?: PanelCallbackContext): Promise<void> {
    const now = this.now();
    const eventId = context?.eventId ?? randomBytes(8).toString("hex");
    const userId = context?.userId ?? "unknown";

    if (context?.eventId) {
      if (this.processedEvents.has(context.eventId) || this.options.store.getControlAudit?.(context.eventId)) {
        this.log(`event ${context.eventId} already processed; skipping duplicate`);
        return;
      }
      this.processedEvents.add(context.eventId);
      if (this.processedEvents.size > 10_000) {
        const first = this.processedEvents.values().next().value;
        if (first) this.processedEvents.delete(first);
      }
    }

    const res = this.resolve(data, context);
    if (res.error || !res.record) {
      this.options.store.auditControl({
        eventId,
        userId,
        action: res.action,
        sessionId: res.sessionId,
        result: res.error ?? "unknown",
        at: now,
      });
      this.log(`panel button rejected (${res.error ?? "unknown"})`);
      return;
    }

    const record = res.record;

    if (record.action === "stop") {
      const state = this.states.get(record.key);
      if (state?.stop?.phase !== "claimed") {
        this.options.store.auditControl({
          eventId,
          userId,
          action: "stop",
          sessionId: record.sessionId,
          result: "stale",
          at: now,
        });
        this.log(`topic ${record.target.topicId}: stop already sent or not claimed; ignored`);
        return;
      }
      record.consumed = true;
      if (state) state.lastAttemptAt = 0;
      // Stop first click opens ephemeral confirm/cancel keyboard, 30-second expiry.
      const confirmExpiresAt = now + 30_000;
      const confirmTokenRec = this.createTokenRecord(
        record.key,
        record.target,
        record.sessionId,
        "confirm",
        confirmExpiresAt,
      );
      const cancelTokenRec = this.createTokenRecord(
        record.key,
        record.target,
        record.sessionId,
        "cancel",
        confirmExpiresAt,
      );
      confirmTokenRec.pairedToken = cancelTokenRec.nonce;
      cancelTokenRec.pairedToken = confirmTokenRec.nonce;

      const markup: Markup = {
        inline_keyboard: [[
          { text: "Confirm Stop", callback_data: confirmTokenRec.token },
          { text: "Cancel", callback_data: cancelTokenRec.token },
        ]],
      };

      const confirmRes = await this.options.transport.confirm(
        record.target,
        "Stop the current turn?",
        markup,
        userId,
      );

      if (confirmRes === "error" || !confirmRes?.ephemeralMessageId) {
        state.stop = null;
        this.tokens.delete(confirmTokenRec.token);
        this.tokensByNonce.delete(confirmTokenRec.nonce);
        this.tokens.delete(cancelTokenRec.token);
        this.tokensByNonce.delete(cancelTokenRec.nonce);
        this.options.store.auditControl({
          eventId,
          userId,
          action: "stop",
          sessionId: record.sessionId,
          result: "error",
          at: now,
        });
        this.log(`topic ${record.target.topicId}: confirm prompt failed`);
        return;
      }

      confirmTokenRec.ephemeralMessageId = confirmRes.ephemeralMessageId;
      cancelTokenRec.ephemeralMessageId = confirmRes.ephemeralMessageId;

      this.options.store.auditControl({
        eventId,
        userId,
        action: "stop",
        sessionId: record.sessionId,
        result: "confirm_prompted",
        at: now,
      });
      return;
    }

    if (record.action === "cancel") {
      // Cancel consumes paired confirm.
      record.consumed = true;
      if (record.pairedToken) {
        const paired = this.tokensByNonce.get(record.pairedToken);
        if (paired) paired.consumed = true;
      }
      const state = this.states.get(record.key);
      if (state) {
        state.stop = null;
        state.lastAttemptAt = 0;
      }

      if (record.ephemeralMessageId) {
        await this.options.transport.editConfirm(
          record.target,
          record.ephemeralMessageId,
          "Stop cancelled.",
          { inline_keyboard: [] },
          userId,
        ).catch(() => {});
      }

      this.options.store.auditControl({
        eventId,
        userId,
        action: "cancel",
        sessionId: record.sessionId,
        result: "cancelled",
        at: now,
      });
      return;
    }

    if (record.action === "confirm") {
      // Stop only executes confirmed single-use token, consumed synchronously before await.
      record.consumed = true;
      if (record.pairedToken) {
        const paired = this.tokensByNonce.get(record.pairedToken);
        if (paired) paired.consumed = true;
      }
      const state = this.states.get(record.key);
      if (state) {
        state.stop = { phase: "sent", at: now };
        state.lastAttemptAt = 0;
      }

      let stopped = false;
      let stopError: unknown = null;
      try {
        stopped = await this.options.stop(record.target);
        this.log(`topic ${record.target.topicId}: stop ${stopped ? "sent" : "found no active turn"}`);
        if (!stopped && state) state.stop = null;
      } catch (err) {
        stopError = err;
        if (state) state.stop = null;
        this.log(`topic ${record.target.topicId}: stop failed`);
      }

      if (record.ephemeralMessageId) {
        await this.options.transport.editConfirm(
          record.target,
          record.ephemeralMessageId,
          stopped ? "Turn stopped." : "Nothing to stop.",
          { inline_keyboard: [] },
          userId,
        ).catch(() => {});
      }

      this.options.store.auditControl({
        eventId,
        userId,
        action: "confirm",
        sessionId: record.sessionId,
        result: stopError ? "error" : stopped ? "ok" : "idle",
        at: now,
      });
      return;
    }

    if (record.action === "steer" || record.action === "followUp") {
      record.consumed = true;
      const state = this.states.get(record.key);
      if (state) state.lastAttemptAt = 0;
      const promptText = record.action === "steer"
        ? "🧭 Reply to this message with your steer for the lane."
        : "➕ Reply to this message with your follow-up for the lane.";

      const promptRes = await this.options.transport.prompt(record.target, promptText, record.sessionId);
      if (promptRes === "error" || !promptRes?.messageId) {
        this.options.store.auditControl({
          eventId,
          userId,
          action: record.action,
          sessionId: record.sessionId,
          result: "error",
          at: now,
        });
        this.log(`topic ${record.target.topicId}: prompt failed`);
        return;
      }

      this.armed.set(record.key, {
        mode: record.action,
        messageId: promptRes.messageId,
        userId,
        sessionId: record.sessionId,
        expiresAt: now + this.armTtlMs,
      });

      this.options.store.auditControl({
        eventId,
        userId,
        action: record.action,
        sessionId: record.sessionId,
        result: "ok",
        at: now,
      });
      return;
    }
  }
  auditRejected(token: string, context?: PanelCallbackContext, reason = "rejected"): void {
    const eventId = context?.eventId;
    if (!eventId) return;
    const res = this.resolve(token, context);
    const action = res.action !== "unknown" ? res.action : (token.startsWith(LANE_PANEL_CALLBACK_PREFIX) ? "panel" : "unknown");
    const sessionId = res.sessionId || res.record?.sessionId || "unknown";
    const userId = context?.userId ?? this.options.operatorId ?? "unknown";
    this.options.store.auditControl?.({
      eventId,
      userId,
      action,
      sessionId,
      result: reason,
      at: this.now(),
    });
  }

  /** The delivery mode a Steer or Follow-up click armed for this topic's next message; consumed once. */
  takeArmed(target: RouteTarget, userId?: string, replyToMessageId?: number): "steer" | "followUp" | null {
    const key = this.kvKey(target, "");
    const armed = this.armed.get(key);
    if (!armed) return null;
    const now = this.now();
    if (now >= armed.expiresAt) {
      this.armed.delete(key);
      return null;
    }
    const currentSession = this.options.boundSession(target);
    if (!currentSession || currentSession !== armed.sessionId) {
      this.armed.delete(key);
      return null;
    }
    if (replyToMessageId !== armed.messageId || userId !== armed.userId) {
      return null;
    }
    this.armed.delete(key);
    return armed.mode;
  }
}
