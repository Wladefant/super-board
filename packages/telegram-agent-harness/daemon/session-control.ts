import { spawnSync } from "node:child_process";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";

export interface DaemonSessionSummary {
  id: string;
  cwd: string;
  workspace: string;
  title: string | null;
  status: string;
  modifiedAtMs: number | null;
  path?: string;
  parentPath?: string | null;
  parentId?: string | null;
  isSubagent?: boolean;
  kind?: "interactive" | "subagent";
}
export interface TranscriptText { entryId: string; text: string }
export type SessionEvent =
  | { kind: "history" | "appended"; sessionId: string; entries: TranscriptText[] }
  | { kind: "streaming"; sessionId: string; active: boolean };
export type DeliveryMode = "auto" | "steer" | "followUp";
export type DeliveryOutcome = "started" | "steered" | "queued";
export interface SessionControlOptions {
  configRoot?: string;
  onEvent: (event: SessionEvent) => void;
  onLog: (message: string) => void;
  /** Deadline for a session's reply to a delivery (default 15 s). */
  replyTimeoutMs?: number;
}
export interface Owner {
  version: 1;
  sessionId: string;
  pid: number;
  cwd: string;
  sessionFile: string;
  endpoint: string;
  token: string;
  /** Optional process start time (epoch ms) recorded by the owner when it published the file. */
  startedAtMs?: number;
}

// Clock skew / timestamp granularity allowance when comparing file times to process start times.
const START_TIME_TOLERANCE_MS = 2000;
export class SessionControlUnavailableError extends Error {}

export function isProcessAlive(pid: number): boolean {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (error) {
    return (error as NodeJS.ErrnoException).code !== "ESRCH";
  }
}

/** Actual process creation times (epoch ms) for the given PIDs; PIDs that cannot be read are omitted. */
export function readProcessStartTimes(pids: number[]): Map<number, number> {
  const times = new Map<number, number>();
  const unique = [...new Set(pids.filter(pid => Number.isSafeInteger(pid) && pid > 0))];
  if (unique.length === 0) return times;
  const run = (command: string, args: string[]): string => {
    const result = spawnSync(command, args, { encoding: "utf8", timeout: 15_000, stdio: ["ignore", "pipe", "ignore"], windowsHide: true, env: { ...process.env, LC_ALL: "C" } });
    return result.status === 0 ? result.stdout : "";
  };
  if (process.platform === "win32") {
    const script = `Get-CimInstance Win32_Process -Filter '${unique.map(pid => `ProcessId=${pid}`).join(" OR ")}' | ForEach-Object { "$($_.ProcessId) $(([DateTimeOffset]$_.CreationDate).ToUnixTimeMilliseconds())" }`;
    for (const line of run("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", script]).split(/\r?\n/)) {
      const match = /^(\d+) (\d+)$/.exec(line.trim());
      if (match) times.set(Number(match[1]), Number(match[2]));
    }
  } else {
    for (const line of run("ps", ["-o", "pid=,lstart=", "-p", unique.join(",")]).split("\n")) {
      const match = /^\s*(\d+)\s+(.+?)\s*$/.exec(line);
      const started = match ? Date.parse(match[2]!) : Number.NaN;
      if (match && Number.isFinite(started)) times.set(Number(match[1]), started);
    }
  }
  return times;
}

/**
 * A PID existing is not proof the owner is alive: the OS reuses PIDs. The owner file must have been
 * published by the process that now holds the PID. Genuine owners write the file after they start, so a
 * file last written before the process started (or whose recorded start time differs) belongs to a dead
 * predecessor. When the OS will not report a start time, fall back to PID existence alone.
 */
export function ownerIdentityMatches(owner: { startedAtMs?: unknown }, fileMtimeMs: number, actualStartMs: number | undefined): boolean {
  if (actualStartMs === undefined) return true;
  if (typeof owner.startedAtMs === "number" && Number.isFinite(owner.startedAtMs)) {
    return Math.abs(owner.startedAtMs - actualStartMs) <= START_TIME_TOLERANCE_MS;
  }
  return fileMtimeMs + START_TIME_TOLERANCE_MS >= actualStartMs;
}

export function discoverConfigRoots(configRoot?: string): string[] {
  const root = configRoot ?? path.join(os.homedir(), process.env.VEYYON_CONFIG_DIR?.trim() || ".veyyon");
  const roots = [root];
  const profiles = path.join(root, "profiles");
  if (fs.existsSync(profiles)) {
    for (const profile of fs.readdirSync(profiles, { withFileTypes: true })) {
      if (profile.isDirectory()) roots.push(path.join(profiles, profile.name));
    }
  }
  return roots;
}

export function resolveSessionsRoots(configRoot?: string): string[] {
  const roots = discoverConfigRoots(configRoot);
  const sessionRoots: string[] = [];
  for (const root of roots) {
    const agentSessions = path.join(root, "agent", "sessions");
    if (fs.existsSync(agentSessions)) sessionRoots.push(agentSessions);
    const directSessions = path.join(root, "sessions");
    if (fs.existsSync(directSessions)) sessionRoots.push(directSessions);
  }
  return sessionRoots;
}

export function findSessionFile(sessionId: string, sessionsRoots?: string[]): string | null {
  const roots = sessionsRoots ?? resolveSessionsRoots();
  for (const sessionsRoot of roots) {
    if (!fs.existsSync(sessionsRoot)) continue;
    try {
      const entries = fs.readdirSync(sessionsRoot, { withFileTypes: true });
      for (const entry of entries) {
        if (entry.isDirectory()) {
          const projectDir = path.join(sessionsRoot, entry.name);
          try {
            const files = fs.readdirSync(projectDir);
            for (const file of files) {
              if (file.endsWith(".jsonl") && (file.endsWith(`_${sessionId}.jsonl`) || file === `${sessionId}.jsonl`)) {
                return path.join(projectDir, file);
              }
            }
          } catch {}
        } else if (entry.isFile()) {
          if (entry.name.endsWith(".jsonl") && (entry.name.endsWith(`_${sessionId}.jsonl`) || entry.name === `${sessionId}.jsonl`)) {
            return path.join(sessionsRoot, entry.name);
          }
        }
      }
    } catch {}
  }
  return null;
}

export function getProjectKeyFromSessionPath(sessionPath?: string | null): string | null {
  if (!sessionPath) return null;
  const norm = sessionPath.replace(/\\/g, "/").replace(/\/+$/, "");
  const parts = norm.split("/").filter(Boolean);
  if (parts.length < 2) return null;
  return parts[parts.length - 2] ?? null;
}

export function discoverOwners(configRoot?: string): Owner[] {
  const roots = discoverConfigRoots(configRoot);
  const candidates: { owner: Owner; mtimeMs: number }[] = [];
  for (const profileRoot of roots) {
    const directory = path.join(profileRoot, "run", "terminals");
    if (!fs.existsSync(directory)) continue;
    for (const name of fs.readdirSync(directory)) {
      if (!name.endsWith(".json")) continue;
      try {
        const file = path.join(directory, name);
        const owner = JSON.parse(fs.readFileSync(file, "utf8"));
        if (owner.version !== 1 || typeof owner.sessionId !== "string" || typeof owner.cwd !== "string" ||
            typeof owner.sessionFile !== "string" || typeof owner.endpoint !== "string" ||
            typeof owner.token !== "string" || !/^[a-f0-9]{64}$/.test(owner.token) || !isProcessAlive(owner.pid)) continue;
        // Never accept a TCP discovery endpoint. This is same-user local IPC, not a remote host.
        if (process.platform === "win32" ? !owner.endpoint.startsWith("\\\\.\\pipe\\veyyon-terminal-") : !path.isAbsolute(owner.endpoint)) continue;
        candidates.push({ owner, mtimeMs: fs.statSync(file).mtimeMs });
      } catch { /* A terminal can exit or atomically republish while discovery runs. */ }
    }
  }
  // A live PID is not enough: Windows and Unix reuse PIDs, leaving stale owner files that point at strangers.
  const startTimes = readProcessStartTimes(candidates.map(candidate => candidate.owner.pid));
  return candidates
    .filter(candidate => ownerIdentityMatches(candidate.owner, candidate.mtimeMs, startTimes.get(candidate.owner.pid)))
    .map(candidate => candidate.owner);
}

/** How long a deliver waits for the session's reply. The session may be blocked on a large write. */
export const DELIVER_REPLY_TIMEOUT_MS = 15_000;
const PENDING_DELIVERY = Symbol("pending-delivery");
type ReplyFrame = { ok?: boolean; result?: unknown; error?: unknown };
export interface DeliveryEvent { messageId: string; state: string; outcome?: string; error?: string }

class TerminalConnection {
  private socket: net.Socket;
  private pending = new Map<string, { resolve: (value: unknown) => void; reject: (error: Error) => void; timer: NodeJS.Timeout; late?: (frame: ReplyFrame) => void }>();
  private ready: Promise<void>;
  public closed = false;
  public replyTimeoutMs = DELIVER_REPLY_TIMEOUT_MS;
  constructor(readonly owner: Owner, onEvent: (event: SessionEvent) => void, private readonly onDelivery: (event: DeliveryEvent) => void = () => {}) {
    this.socket = net.createConnection(owner.endpoint);
    const { promise, resolve, reject } = Promise.withResolvers<void>();
    this.ready = promise;
    const connectTimer = setTimeout(() => {
      reject(new SessionControlUnavailableError("Terminal owner connect timed out; no GUI fallback is permitted"));
      this.close();
    }, 5_000);
    this.socket.once("connect", () => {
      clearTimeout(connectTimer);
      resolve();
    });
    this.socket.once("error", error => {
      clearTimeout(connectTimer);
      reject(error);
    });
    let buffer = "";
    this.socket.setEncoding("utf8");
    this.socket.on("data", chunk => {
      buffer += chunk;
      if (Buffer.byteLength(buffer) > 4 * 1024 * 1024) { this.close(); return; }
      for (;;) {
        const newline = buffer.indexOf("\n");
        if (newline < 0) break;
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        try {
          const frame = JSON.parse(line);
          if (frame.event) {
            const event = frame.event;
            if (event.sessionId !== owner.sessionId) { this.close(); return; }
            if ((event.kind === "history" || event.kind === "appended") && Array.isArray(event.entries) &&
                event.entries.every((entry: TranscriptText) => typeof entry.entryId === "string" && typeof entry.text === "string")) onEvent(event);
            else if (event.kind === "streaming" && typeof event.active === "boolean") onEvent(event);
            else if (event.kind === "delivery" && typeof event.messageId === "string" && typeof event.state === "string") this.onDelivery(event);
            continue;
          }
          const pending = this.pending.get(frame.id);
          if (!pending) continue;
          clearTimeout(pending.timer);
          this.pending.delete(frame.id);
          if (pending.late) { pending.late(frame); continue; }
          if (frame.ok === true) pending.resolve(frame.result);
          else pending.reject(new SessionControlUnavailableError(String(frame.error)));
        } catch { this.close(); return; }
      }
    });
    this.socket.on("error", () => this.close());
    this.socket.on("close", () => this.close());
  }
  /**
   * With `lateReply`, a session that does not answer in time keeps the request in flight and the socket
   * open; the promise resolves with PENDING_DELIVERY and a later reply goes to `lateReply`. The message is
   * never sent twice, because the session may already have accepted it.
   */
  async request(op: string, payload: Record<string, unknown> = {}, lateReply?: (frame: ReplyFrame) => void): Promise<unknown> {
    await this.ready;
    if (this.closed) throw new SessionControlUnavailableError("Terminal owner disconnected; no GUI fallback is permitted");
    const id = crypto.randomUUID();
    const { promise, resolve, reject } = Promise.withResolvers<unknown>();
    const timer = setTimeout(() => {
      const entry = this.pending.get(id);
      if (lateReply && entry) {
        entry.late = lateReply;
        entry.timer = setTimeout(() => this.pending.delete(id), 30 * 60_000);
        entry.timer.unref?.();
        resolve(PENDING_DELIVERY);
        return;
      }
      this.pending.delete(id);
      reject(new SessionControlUnavailableError("Terminal request timed out; acceptance unknown; not retried"));
      this.close();
    }, this.replyTimeoutMs);
    this.pending.set(id, { resolve, reject, timer });
    this.socket.write(JSON.stringify({ version: 1, id, token: this.owner.token, sessionId: this.owner.sessionId, op, ...payload }) + "\n");
    return promise;
  }
  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.socket.destroy();
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new SessionControlUnavailableError("Terminal owner disconnected; delivery is not retried"));
    }
    this.pending.clear();
  }
}

export class TerminalSessionControl {
  private connections = new Map<string, Promise<TerminalConnection>>();
  private streaming = new Set<string>();
  constructor(private readonly options: SessionControlOptions) {}
  get configRoot(): string | undefined { return this.options.configRoot; }
  isBusy(sessionId: string): boolean { return this.streaming.has(sessionId); }
  async listSessions(): Promise<DaemonSessionSummary[]> {
    const owners = discoverOwners(this.options.configRoot);
    const counts = new Map<string, number>();
    for (const owner of owners) counts.set(owner.sessionId, (counts.get(owner.sessionId) ?? 0) + 1);
    return owners.filter(owner => counts.get(owner.sessionId) === 1).map(owner => ({
      id: owner.sessionId, cwd: owner.cwd, workspace: owner.cwd, path: owner.sessionFile,
      title: null, status: this.isBusy(owner.sessionId) ? "Running" : "Idle", modifiedAtMs: null,
      kind: "interactive", isSubagent: false,
    }));
  }
  async findSession(workspace: string): Promise<DaemonSessionSummary | null> {
    const matches = (await this.listSessions()).filter(session => path.resolve(session.cwd).toLowerCase() === path.resolve(workspace).toLowerCase());
    if (matches.length > 1) throw new SessionControlUnavailableError("Several terminals own this workspace; attach by exact session id");
    return matches[0] ?? null;
  }
  async createSession(_workspace: string, _title: string): Promise<string> {
    throw new SessionControlUnavailableError("Start a Veyyon terminal in the workspace; the daemon never creates or resumes a session");
  }
  async ensureSession(workspace: string, title: string): Promise<string> {
    return (await this.findSession(workspace))?.id ?? this.createSession(workspace, title);
  }
  private async connection(sessionId: string): Promise<TerminalConnection> {
    const existing = this.connections.get(sessionId);
    if (existing) {
      const connection = await existing;
      if (!connection.closed) return connection;
      this.connections.delete(sessionId);
    }
    const owners = discoverOwners(this.options.configRoot).filter(owner => owner.sessionId === sessionId);
    if (owners.length !== 1) throw new SessionControlUnavailableError("No unique live terminal owner for this session; stop-before-resume with the IPC-enabled build is required");
    const pending = (async () => {
      const connection = new TerminalConnection(owners[0]!, event => {
        if (event.kind === "streaming") {
          if (event.active) this.streaming.add(sessionId); else this.streaming.delete(sessionId);
        }
        this.options.onEvent(event);
      }, delivery => {
        const label = `delivery ${delivery.messageId} for ${sessionId}`;
        if (delivery.state === "failed") this.options.onLog(`${label} failed in the terminal: ${delivery.error ?? "unknown error"}`);
        else this.options.onLog(`${label} ${delivery.state}${delivery.outcome ? ` (${delivery.outcome})` : ""}`);
      });
      if (this.options.replyTimeoutMs) connection.replyTimeoutMs = this.options.replyTimeoutMs;
      try { await connection.request("subscribe"); return connection; }
      catch (error) { connection.close(); throw error; }
    })();
    this.connections.set(sessionId, pending);
    try { return await pending; } catch (error) { this.connections.delete(sessionId); throw error; }
  }
  /**
   * The message id lets a session that supports `deliver-ack` accept at once and run the delivery later.
   * An older session ignores `ack` and answers with the outcome. A reply that misses the deadline leaves
   * the delivery pending: it is logged as pending, never retried, and confirmed when the reply arrives.
   */
  async deliver(sessionId: string, text: string, mode: DeliveryMode = "auto"): Promise<DeliveryOutcome> {
    const messageId = crypto.randomUUID();
    const label = `delivery ${messageId} for ${sessionId}`;
    const result = await (await this.connection(sessionId)).request("deliver", { text, mode, ack: true, messageId }, frame => {
      if (frame.ok === true) this.options.onLog(`${label} accepted late`);
      else this.options.onLog(`${label} rejected late: ${String(frame.error)}`);
    });
    if (result === PENDING_DELIVERY) {
      this.options.onLog(`${label} pending: the session has not answered in ${(this.options.replyTimeoutMs ?? DELIVER_REPLY_TIMEOUT_MS) / 1000}s; not retried`);
      return "queued";
    }
    if (result === "started" || result === "steered" || result === "queued") return result;
    const ack = result as { accepted?: unknown; state?: unknown; outcome?: unknown } | null;
    if (ack && typeof ack === "object" && ack.accepted === true) return "queued";
    throw new Error("Invalid terminal delivery response");
  }
  async abort(sessionId: string): Promise<boolean> { return await (await this.connection(sessionId)).request("abort") === true; }
  async loadTranscript(sessionId: string): Promise<void> { await this.connection(sessionId); }
  close(): void {
    for (const pending of this.connections.values()) void pending.then(connection => connection.close(), () => {});
    this.connections.clear();
    this.streaming.clear();
  }
}
