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
}
interface Owner {
  version: 1;
  sessionId: string;
  pid: number;
  cwd: string;
  sessionFile: string;
  endpoint: string;
  token: string;
}
export class SessionControlUnavailableError extends Error {}

export function isProcessAlive(pid: number): boolean {
  if (!Number.isSafeInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch (error) {
    return (error as NodeJS.ErrnoException).code !== "ESRCH";
  }
}

export function discoverOwners(configRoot?: string): Owner[] {
  const root = configRoot ?? path.join(os.homedir(), process.env.VEYYON_CONFIG_DIR?.trim() || ".veyyon");
  const roots = [root];
  const profiles = path.join(root, "profiles");
  if (fs.existsSync(profiles)) {
    for (const profile of fs.readdirSync(profiles, { withFileTypes: true })) {
      if (profile.isDirectory()) roots.push(path.join(profiles, profile.name));
    }
  }
  const owners: Owner[] = [];
  for (const profileRoot of roots) {
    const directory = path.join(profileRoot, "run", "terminals");
    if (!fs.existsSync(directory)) continue;
    for (const name of fs.readdirSync(directory)) {
      if (!name.endsWith(".json")) continue;
      try {
        const owner = JSON.parse(fs.readFileSync(path.join(directory, name), "utf8"));
        if (owner.version !== 1 || typeof owner.sessionId !== "string" || typeof owner.cwd !== "string" ||
            typeof owner.sessionFile !== "string" || typeof owner.endpoint !== "string" ||
            typeof owner.token !== "string" || !/^[a-f0-9]{64}$/.test(owner.token) || !isProcessAlive(owner.pid)) continue;
        // Never accept a TCP discovery endpoint. This is same-user local IPC, not a remote host.
        if (process.platform === "win32" ? !owner.endpoint.startsWith("\\\\.\\pipe\\veyyon-terminal-") : !path.isAbsolute(owner.endpoint)) continue;
        owners.push(owner);
      } catch { /* A terminal can exit or atomically republish while discovery runs. */ }
    }
  }
  return owners;
}

class TerminalConnection {
  private socket: net.Socket;
  private pending = new Map<string, { resolve: (value: unknown) => void; reject: (error: Error) => void; timer: NodeJS.Timeout }>();
  private ready: Promise<void>;
  public closed = false;
  constructor(readonly owner: Owner, onEvent: (event: SessionEvent) => void) {
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
            continue;
          }
          const pending = this.pending.get(frame.id);
          if (!pending) continue;
          clearTimeout(pending.timer);
          this.pending.delete(frame.id);
          if (frame.ok === true) pending.resolve(frame.result);
          else pending.reject(new SessionControlUnavailableError(String(frame.error)));
        } catch { this.close(); return; }
      }
    });
    this.socket.on("error", () => this.close());
    this.socket.on("close", () => this.close());
  }
  async request(op: string, payload: Record<string, unknown> = {}): Promise<unknown> {
    await this.ready;
    if (this.closed) throw new SessionControlUnavailableError("Terminal owner disconnected; no GUI fallback is permitted");
    const id = crypto.randomUUID();
    const { promise, resolve, reject } = Promise.withResolvers<unknown>();
    const timer = setTimeout(() => {
      this.pending.delete(id);
      reject(new SessionControlUnavailableError("Terminal request timed out; acceptance unknown; not retried"));
      this.close();
    }, 15_000);
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
      });
      try { await connection.request("subscribe"); return connection; }
      catch (error) { connection.close(); throw error; }
    })();
    this.connections.set(sessionId, pending);
    try { return await pending; } catch (error) { this.connections.delete(sessionId); throw error; }
  }
  async deliver(sessionId: string, text: string, mode: DeliveryMode = "auto"): Promise<DeliveryOutcome> {
    const result = await (await this.connection(sessionId)).request("deliver", { text, mode });
    if (result !== "started" && result !== "steered" && result !== "queued") throw new Error("Invalid terminal delivery response");
    return result;
  }
  async abort(sessionId: string): Promise<boolean> { return await (await this.connection(sessionId)).request("abort") === true; }
  async loadTranscript(sessionId: string): Promise<void> { await this.connection(sessionId); }
  close(): void {
    for (const pending of this.connections.values()) void pending.then(connection => connection.close(), () => {});
    this.connections.clear();
    this.streaming.clear();
  }
}
