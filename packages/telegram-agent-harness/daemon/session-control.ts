/**
 * session-control.ts — Veyyon session control over the GUI Host action protocol.
 *
 * Wire contract: `packages/coding-agent/src/gui-host/` in the Veyyon fork
 * (`wire.ts` types, `actions/sessions.ts`, `actions/turn.ts`). Two properties of
 * that protocol shape this client:
 *
 * 1. A connection carries exactly one active session, and `SubmitPrompt`,
 *    `OpenSession` and `LoadTranscript` all activate. Push frames
 *    (`TranscriptAppended`, `StreamingChanged`) therefore say nothing about which
 *    session they belong to — so one connection is held per session and the
 *    connection itself is the attribution. The control connection never prompts,
 *    so it never becomes a second agent owner of a session.
 * 2. A prompt request settles when the session accepts the text, not when the turn
 *    ends. Turn output arrives later, unsolicited, on the same connection.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { GuiHostRequestError, SocketGuiHostPort, type GuiHostPort, type GuiHostResponse } from "../src/gui-host-client";

export interface DaemonSessionSummary {
  id: string;
  cwd: string;
  workspace: string;
  title: string | null;
  status: string;
  modifiedAtMs: number | null;
}

export interface TranscriptText {
  entryId: string;
  text: string;
}

export type SessionEvent =
  | { kind: "history"; sessionId: string; entries: TranscriptText[] }
  | { kind: "appended"; sessionId: string; entries: TranscriptText[] }
  | { kind: "streaming"; sessionId: string; active: boolean };

export type DeliveryMode = "auto" | "steer" | "followUp";
export type DeliveryOutcome = "started" | "steered" | "queued";

export interface SessionControlOptions {
  /** Endpoint the GUI host listens on, or null when none was discovered. */
  endpoint: string | null;
  onEvent: (event: SessionEvent) => void;
  onLog: (message: string) => void;
  /** Injected so tests drive a real socket without reaching for the operator's host. */
  portFactory?: (endpoint: string, onFrame: (frame: unknown) => void) => GuiHostPort;
}

/**
 * Directories a GUI host may have published its endpoint in, most specific first.
 *
 * `veyyon gui` writes its endpoint into `getAgentDir()` — the ACTIVE PROFILE's
 * agent directory, `~/.veyyon/profiles/<profile>/agent`, not `~/.veyyon`. A daemon
 * that only looked at `~/.veyyon` never found a running host.
 *
 * Which profile is active is decided by Veyyon's own resolution (env, then a
 * global default recorded in the config root), and this does not reimplement it:
 * the env-named profile is preferred, then EVERY profile that exists is searched,
 * so a host started under a non-default profile is still found. `~/.veyyon` stays
 * last because it is where an operator pointing the daemon at a host by hand
 * would write the file, and the error message tells them to.
 *
 * @param configRoot injected so tests scan a real profiles tree of their own
 *   rather than the operator's; defaults to the config root Veyyon uses.
 */
export function guiHostAgentDirs(configRoot?: string): string[] {
  const override = process.env.VEYYON_CODING_AGENT_DIR?.trim();
  if (override) return [path.resolve(override)];

  const root = configRoot ?? path.join(os.homedir(), process.env.VEYYON_CONFIG_DIR?.trim() || ".veyyon");
  const profilesRoot = path.join(root, "profiles");
  const preferred = process.env.VEYYON_PROFILE?.trim() || "default";

  const dirs = [path.join(profilesRoot, preferred, "agent")];
  let present: string[] = [];
  try {
    present = fs
      .readdirSync(profilesRoot, { withFileTypes: true })
      .filter(entry => entry.isDirectory() && entry.name !== preferred)
      .map(entry => path.join(profilesRoot, entry.name, "agent"));
  } catch {}
  dirs.push(...present.sort(), root);
  return dirs;
}

/**
 * Discovery order for the host endpoint. The host itself only ever knows the
 * endpoint it was told to listen on, so an explicit env value wins, then the
 * endpoint file the host publishes, then the documented default socket — and only
 * when it exists, because dialling a missing socket is a five-second stall per try.
 */
export function resolveGuiHostEndpoint(...agentDirs: string[]): string | null {
  const configured = process.env.VEYYON_GUI_HOST_ENDPOINT?.trim();
  if (configured) return normalizeEndpoint(configured);

  const candidates = agentDirs.length > 0 ? agentDirs : guiHostAgentDirs();
  for (const agentDir of candidates) {
    try {
      const written = fs.readFileSync(path.join(agentDir, "gui-host.endpoint"), "utf8").trim();
      if (written) return normalizeEndpoint(written);
    } catch {}
  }
  for (const agentDir of candidates) {
    const socketPath = path.join(agentDir, "gui-host.sock");
    if (fs.existsSync(socketPath)) return `unix:${socketPath}`;
  }
  return null;
}

function normalizeEndpoint(written: string): string {
  return /^(tcp|unix):/.test(written) ? written : `unix:${path.resolve(written)}`;
}

export class SessionControlUnavailableError extends Error {
  constructor(detail: string) {
    super(detail);
    this.name = "SessionControlUnavailableError";
  }
}

export class GuiHostSessionControl {
  private readonly options: SessionControlOptions;
  private control: GuiHostPort | null = null;
  private readonly sessionPorts = new Map<string, GuiHostPort>();
  private readonly streaming = new Set<string>();

  constructor(options: SessionControlOptions) {
    this.options = options;
  }

  public get endpoint(): string | null {
    return this.options.endpoint;
  }

  public isBusy(sessionId: string): boolean {
    return this.streaming.has(sessionId);
  }

  public async listSessions(): Promise<DaemonSessionSummary[]> {
    const response = await this.controlPort().request("ListSessions");
    return readSessionSummaries(response);
  }

  /** Read-only preview: unlike LoadTranscript, this never attaches or switches a session. */
  public async lastPrompt(sessionId: string): Promise<string | null> {
    const response = await this.controlPort().request({ PreviewSessionTranscript: { session: sessionId } });
    for (const snapshot of snapshotSections(response)) {
      const transcript = versionedValue(asRecord(snapshot.SessionTranscript)?.transcript);
      if (!Array.isArray(transcript)) continue;
      for (let index = transcript.length - 1; index >= 0; index--) {
        const entry = asRecord(transcript[index]);
        if (entry?.role !== "User" || !Array.isArray(entry.content)) continue;
        const text = entry.content.map(block => asRecord(asRecord(block)?.Text)?.text)
          .filter((text): text is string => typeof text === "string").join(" ").trim();
        if (text) return text;
      }
    }
    return null;
  }

  /** Session already serving `workspace`, newest first, or null. */
  public async findSession(workspace: string): Promise<DaemonSessionSummary | null> {
    const target = path.resolve(workspace).toLowerCase();
    const matches = (await this.listSessions())
      .filter(session => path.resolve(session.cwd || session.workspace).toLowerCase() === target)
      .sort((a, b) => (b.modifiedAtMs ?? 0) - (a.modifiedAtMs ?? 0));
    return matches[0] ?? null;
  }

  public async createSession(workspace: string, title: string): Promise<string> {
    const response = await this.controlPort().request({ CreateSession: { workspace, title } });
    const created = readActiveSessionId(response);
    if (!created) throw new SessionControlUnavailableError("CreateSession returned no active session id");
    return created;
  }

  /** The session a chat should talk to, created in `workspace` when none exists. */
  public async ensureSession(workspace: string, title: string): Promise<string> {
    const existing = await this.findSession(workspace);
    if (existing) return existing.id;
    return this.createSession(workspace, title);
  }

  /**
   * Deliver operator text. `auto` asks for a fresh turn and falls back to a steer
   * when the host reports one already running, which is the only truthful busy
   * signal: a locally cached idle flag races every turn boundary.
   */
  public async deliver(sessionId: string, text: string, mode: DeliveryMode = "auto"): Promise<DeliveryOutcome> {
    const port = this.sessionPort(sessionId);
    const payload = { session: sessionId, text, attachments: [] };
    if (mode === "steer") {
      await port.request({ Steer: payload });
      return "steered";
    }
    if (mode === "followUp") {
      await port.request({ FollowUp: payload });
      return "queued";
    }
    try {
      await port.request({ SubmitPrompt: payload });
      return "started";
    } catch (error) {
      if (error instanceof GuiHostRequestError && error.code === "TURN_IN_PROGRESS") {
        await port.request({ Steer: payload });
        return "steered";
      }
      throw error;
    }
  }

  /** True when a turn was aborted; false when the host reported none running. */
  public async abort(sessionId: string): Promise<boolean> {
    try {
      await this.sessionPort(sessionId).request({ AbortTurn: { session: sessionId } });
      return true;
    } catch (error) {
      if (error instanceof GuiHostRequestError && error.code === "NOT_RUNNING") return false;
      throw error;
    }
  }

  /** Replays a session's transcript as `history`, so already-said text is not resent. */
  public async loadTranscript(sessionId: string): Promise<void> {
    await this.sessionPort(sessionId).request({ LoadTranscript: { session: sessionId, before: null } });
  }

  public async usage(): Promise<Record<string, unknown> | null> {
    const response = await this.controlPort().request("GetUsage");
    for (const snapshot of snapshotSections(response)) {
      const usage = asRecord(snapshot.Usage);
      if (usage) return asRecord(usage.value) ?? usage;
    }
    return null;
  }

  public close(): void {
    for (const port of this.sessionPorts.values()) port.close?.();
    this.sessionPorts.clear();
    this.streaming.clear();
    this.control?.close?.();
    this.control = null;
  }

  private controlPort(): GuiHostPort {
    if (!this.control) this.control = this.openPort("control");
    return this.control;
  }

  private sessionPort(sessionId: string): GuiHostPort {
    const existing = this.sessionPorts.get(sessionId);
    if (existing) return existing;
    const port = this.openPort(sessionId);
    this.sessionPorts.set(sessionId, port);
    return port;
  }

  private openPort(sessionId: string): GuiHostPort {
    const endpoint = this.options.endpoint;
    if (!endpoint) {
      throw new SessionControlUnavailableError(
        "No Veyyon GUI host endpoint was discovered. Start one with `veyyon gui tcp:127.0.0.1:7699`, " +
          `or set VEYYON_GUI_HOST_ENDPOINT. Searched: ${guiHostAgentDirs()
            .map(dir => path.join(dir, "gui-host.endpoint"))
            .join(", ")}`,
      );
    }
    const onFrame = (frame: unknown) => {
      if (sessionId !== "control") this.dispatchFrame(sessionId, frame);
    };
    return this.options.portFactory
      ? this.options.portFactory(endpoint, onFrame)
      : new SocketGuiHostPort(endpoint, undefined, 15_000, onFrame);
  }

  private dispatchFrame(sessionId: string, frame: unknown): void {
    const record = asRecord(frame);
    if (!record) return;

    const appended = asRecord(record.TranscriptAppended);
    if (appended) {
      const entries = assistantTexts(appended.entries);
      if (entries.length > 0) this.options.onEvent({ kind: "appended", sessionId, entries });
      return;
    }

    if ("StreamingChanged" in record) {
      const active = record.StreamingChanged !== null && record.StreamingChanged !== undefined;
      if (active) this.streaming.add(sessionId);
      else this.streaming.delete(sessionId);
      this.options.onEvent({ kind: "streaming", sessionId, active });
      return;
    }

    const snapshot = asRecord(record.Snapshot);
    const transcript = asRecord(snapshot?.Transcript);
    if (transcript) {
      this.options.onEvent({ kind: "history", sessionId, entries: assistantTexts(transcript.value) });
      return;
    }

    const failure = asRecord(record.RequestFailed);
    const error = asRecord(failure?.error);
    if (error && typeof error.message === "string") {
      this.options.onLog(`GUI host rejected a request for session ${sessionId}: ${error.message}`);
    }
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** `Versioned<T>` from the wire contract: `{ revision, value }`. */
function versionedValue(value: unknown): unknown {
  return asRecord(value)?.value;
}

function snapshotSections(response: GuiHostResponse): Record<string, unknown>[] {
  const sections: Record<string, unknown>[] = [];
  for (const event of response.events) {
    const snapshot = asRecord(asRecord(event)?.Snapshot);
    if (snapshot) sections.push(snapshot);
  }
  return sections;
}

export function readSessionSummaries(response: GuiHostResponse): DaemonSessionSummary[] {
  for (const snapshot of snapshotSections(response)) {
    if (!Array.isArray(snapshot.Sessions)) continue;
    const value = versionedValue(snapshot.Sessions[0]);
    if (!Array.isArray(value)) continue;
    const summaries: DaemonSessionSummary[] = [];
    for (const entry of value) {
      const session = asRecord(entry);
      const id = typeof session?.id === "string" ? session.id : null;
      if (!id) continue;
      summaries.push({
        id,
        cwd: typeof session?.cwd === "string" ? session.cwd : "",
        workspace: typeof session?.workspace === "string" ? session.workspace : "",
        title: typeof session?.title === "string" ? session.title : null,
        status: typeof session?.status === "string" ? session.status : "Unknown",
        modifiedAtMs: typeof session?.modified_at_ms === "number" ? session.modified_at_ms : null,
      });
    }
    return summaries;
  }
  return [];
}

export function readActiveSessionId(response: GuiHostResponse): string | null {
  for (const snapshot of snapshotSections(response)) {
    const active = asRecord(versionedValue(snapshot.ActiveSession));
    if (active && typeof active.id === "string") return active.id;
  }
  return null;
}

/**
 * Assistant prose from transcript entries. Thinking blocks, tool calls and tool
 * results are deliberately dropped: the operator's chat gets what the agent said,
 * not its reasoning or its tool traffic.
 */
export function assistantTexts(value: unknown): TranscriptText[] {
  if (!Array.isArray(value)) return [];
  const texts: TranscriptText[] = [];
  for (const raw of value) {
    const entry = asRecord(raw);
    if (!entry || entry.role !== "Assistant" || typeof entry.id !== "string") continue;
    if (!Array.isArray(entry.content)) continue;
    const parts: string[] = [];
    for (const block of entry.content) {
      const text = asRecord(asRecord(block)?.Text)?.text;
      if (typeof text === "string" && text.trim()) parts.push(text);
    }
    if (parts.length > 0) texts.push({ entryId: entry.id, text: parts.join("\n\n") });
  }
  return texts;
}
