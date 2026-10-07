/**
 * fleet-state.ts — pure data layer for the Telegram fleet view (issue #427, parent #425).
 *
 * Builds a typed {@link FleetSnapshot} (every live session and subagent with parent links, model,
 * elapsed time, redacted last action, usage windows, host memory, open questions) from:
 *   - terminal owner files (liveness; PID-reuse is already rejected by `discoverOwners` through
 *     `ownerIdentityMatches`),
 *   - session JSONL files (top-level sessions, and subagents nested in `<session-stem>/` directories),
 *   - `veyyon usage --json`, run with a timeout and cached for 60 s (one call for all consumers).
 *
 * It has no Telegram dependency and no side effects beyond the injected readers. It is NOT wired into
 * the live daemon here (see #429 and #430).
 */
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { spawn } from "node:child_process";
import { redactSecrets } from "../extension/sanitizer";
import { discoverOwners, type Owner } from "./session-control";

export type FleetLaneStatus = "running" | "idle";

export interface FleetLane {
  /** Session id for top-level sessions; `<parent id>/<file stem>` for subagents. */
  id: string;
  /** Display name: the title for a top-level session, the agent id for a subagent. */
  name: string;
  kind: "interactive" | "subagent";
  parentId: string | null;
  childIds: string[];
  cwd: string;
  model: string | null;
  status: FleetLaneStatus;
  startedAtMs: number | null;
  elapsedMs: number | null;
  lastActivityMs: number;
  /** Last tool call or assistant line, truncated and passed through `redactSecrets`. */
  lastAction: string | null;
}

export interface FleetUsageWindow {
  provider: string;
  label: string;
  windowId: string;
  remainingPercent: number;
  resetsAt: number | null;
  status: string;
}

export interface FleetUsage {
  available: boolean;
  observedAt: number;
  windows: FleetUsageWindow[];
  detail?: string;
}

export interface FleetHostMemory {
  totalBytes: number;
  freeBytes: number;
  usedPercent: number;
}

export interface FleetQuestion {
  id: string;
  text: string;
  sessionId?: string;
}

export interface FleetSnapshot {
  version: 1;
  observedAt: number;
  lanes: FleetLane[];
  counts: { running: number; idle: number; subagents: number };
  usage: FleetUsage;
  hostMemory: FleetHostMemory;
  questions: FleetQuestion[];
}

/** What the session-file reader extracts from one JSONL file. */
export interface SessionFileInfo {
  id: string;
  cwd: string;
  title: string | null;
  startedAtMs: number | null;
  mtimeMs: number;
  model: string | null;
  lastAction: string | null;
}

export interface FleetSources {
  now(): number;
  /** Live terminal owners, already filtered by `ownerIdentityMatches`. */
  listOwners(): Owner[] | Promise<Owner[]>;
  /** Parse one session file; null when unreadable. */
  readSession(file: string): SessionFileInfo | null;
  /** Subagent session files nested directly under the given session file's `<stem>/` directory. */
  listChildFiles(file: string): string[];
  /** Raw stdout of `veyyon usage --json`. Must enforce its own timeout; may reject. */
  runUsage(timeoutMs: number): Promise<string>;
  hostMemory(): { totalBytes: number; freeBytes: number };
  questions(): FleetQuestion[];
}

export interface FleetStateOptions {
  /** A session or subagent whose file changed within this window is "running". Default 90 s. */
  activeWindowMs?: number;
  /** Subagent files untouched for longer than this are finished and left out. Default 10 min. */
  subagentLiveWindowMs?: number;
  usageCacheMs?: number;
  usageTimeoutMs?: number;
  lastActionMaxChars?: number;
}

const DEFAULTS = {
  activeWindowMs: 90_000,
  subagentLiveWindowMs: 10 * 60_000,
  usageCacheMs: 60_000,
  usageTimeoutMs: 20_000,
  lastActionMaxChars: 200,
};

const HEAD_BYTES = 64 * 1024;
const TAIL_BYTES = 256 * 1024;

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
const str = (value: unknown): string | undefined => typeof value === "string" && value.trim() ? value.trim() : undefined;
const num = (value: unknown): number | undefined => typeof value === "number" && Number.isFinite(value) ? value : undefined;

function parseLines(chunk: string, dropFirstPartial: boolean): Record<string, unknown>[] {
  const lines = chunk.split("\n");
  if (dropFirstPartial) lines.shift();
  const out: Record<string, unknown>[] = [];
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      const parsed = record(JSON.parse(line));
      if (parsed) out.push(parsed);
    } catch { /* partial trailing line or oversized entry cut by the window */ }
  }
  return out;
}

function readWindow(fd: number, position: number, length: number): string {
  const buffer = Buffer.alloc(length);
  const read = fs.readSync(fd, buffer, 0, length, position);
  return buffer.toString("utf8", 0, read);
}

function truncate(text: string, max: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  return flat.length > max ? `${flat.slice(0, max - 1)}…` : flat;
}

function describeEntry(entry: Record<string, unknown>): string | null {
  if (entry.type === "custom") {
    const data = record(entry.data);
    if (entry.customType === "tool_execution_start" && data) {
      const tool = str(data.toolName);
      if (tool) return str(data.intent) ? `${tool}: ${str(data.intent)}` : tool;
    }
    return null;
  }
  if (entry.type === "message") {
    const message = record(entry.message);
    if (message?.role !== "assistant" || !Array.isArray(message.content)) return null;
    for (const block of message.content) {
      const item = record(block);
      if (item?.type === "text" && str(item.text)) return str(item.text)!;
    }
  }
  return null;
}

/**
 * Default session-file reader. Session files reach tens of MB, so only a head window (header, first
 * model) and a tail window (current model, title, last action) are read.
 */
export function readSessionFile(file: string, maxActionChars = DEFAULTS.lastActionMaxChars): SessionFileInfo | null {
  let fd: number | undefined;
  try {
    fd = fs.openSync(file, "r");
    const stat = fs.fstatSync(fd);
    const head = parseLines(readWindow(fd, 0, Math.min(stat.size, HEAD_BYTES)), false);
    const tailStart = Math.max(0, stat.size - TAIL_BYTES);
    const tail = parseLines(readWindow(fd, tailStart, stat.size - tailStart), tailStart > 0);
    const header = head.find(entry => entry.type === "session");
    const id = str(header?.id);
    if (!header || !id) return null;

    let title = str(head.find(entry => entry.type === "title")?.title) ?? str(header.title) ?? null;
    let model: string | null = null;
    for (const entry of [...head, ...tail]) {
      if (entry.type === "model_change" && str(entry.model)) model = str(entry.model)!;
      else if (entry.type === "title_change" && str(entry.title)) title = str(entry.title)!;
    }
    if (!model) {
      for (let index = tail.length - 1; index >= 0 && !model; index--) {
        const message = record(tail[index]!.message);
        if (message?.role === "assistant" && str(message.model)) {
          model = str(message.provider) ? `${str(message.provider)}/${str(message.model)}` : str(message.model)!;
        }
      }
    }
    let lastAction: string | null = null;
    for (let index = tail.length - 1; index >= 0 && !lastAction; index--) lastAction = describeEntry(tail[index]!) ?? null;
    const started = Date.parse(String(header.timestamp));
    return {
      id,
      cwd: str(header.cwd) ?? "",
      title: title ? redactSecrets(title) : null,
      startedAtMs: Number.isFinite(started) ? started : null,
      mtimeMs: stat.mtimeMs,
      model,
      lastAction: lastAction ? truncate(redactSecrets(lastAction), maxActionChars) : null,
    };
  } catch {
    return null;
  } finally {
    if (fd !== undefined) try { fs.closeSync(fd); } catch { /* ignore */ }
  }
}

/** Subagent files for `<dir>/<stem>.jsonl` live in `<dir>/<stem>/*.jsonl` (advisor side files excluded). */
export function listChildSessionFiles(file: string): string[] {
  const dir = file.replace(/\.jsonl$/i, "");
  try {
    return fs.readdirSync(dir, { withFileTypes: true })
      .filter(entry => entry.isFile() && entry.name.endsWith(".jsonl") && !entry.name.startsWith("__"))
      .map(entry => path.join(dir, entry.name));
  } catch {
    return [];
  }
}

export interface UsageCommand { command: string; args: string[] }

/** Run a command, kill it when `timeoutMs` passes, and reject; no shell is involved. */
export function runCommandWithTimeout(spec: UsageCommand, timeoutMs: number): Promise<string> {
  const { promise, resolve, reject } = Promise.withResolvers<string>();
  const child = spawn(spec.command, spec.args, { windowsHide: true, stdio: ["ignore", "pipe", "ignore"] });
  const chunks: Buffer[] = [];
  let size = 0;
  const timer = setTimeout(() => { child.kill("SIGKILL"); reject(new Error("usage command timed out")); }, timeoutMs);
  child.stdout.on("data", (chunk: Buffer) => {
    size += chunk.length;
    if (size > 4 * 1024 * 1024) { child.kill("SIGKILL"); reject(new Error("usage output too large")); return; }
    chunks.push(chunk);
  });
  child.once("error", error => { clearTimeout(timer); reject(error); });
  child.once("close", code => {
    clearTimeout(timer);
    if (code === 0) resolve(Buffer.concat(chunks).toString("utf8")); else reject(new Error(`usage command exited ${code}`));
  });
  return promise;
}

export function defaultFleetSources(configRoot?: string, usageCommand: UsageCommand = { command: "veyyon", args: ["usage", "--json"] }): FleetSources {
  return {
    now: () => Date.now(),
    listOwners: () => discoverOwners(configRoot),
    readSession: file => readSessionFile(file),
    listChildFiles: listChildSessionFiles,
    runUsage: timeoutMs => runCommandWithTimeout(usageCommand, timeoutMs),
    hostMemory: () => ({ totalBytes: os.totalmem(), freeBytes: os.freemem() }),
    questions: () => [],
  };
}

/** Parse `veyyon usage --json` (`reports[].limits[]`). Exhausted windows are kept and flagged. */
export function parseFleetUsage(raw: string, observedAt: number): FleetUsage {
  try {
    const reports = record(JSON.parse(raw))?.reports;
    const windows: FleetUsageWindow[] = [];
    for (const reportValue of Array.isArray(reports) ? reports : []) {
      const report = record(reportValue);
      const provider = str(report?.provider);
      if (!provider || !Array.isArray(report?.limits)) continue;
      for (const limitValue of report.limits) {
        const limit = record(limitValue);
        const amount = record(limit?.amount);
        const window = record(limit?.window);
        const fraction = num(amount?.remainingFraction);
        const percent = fraction !== undefined ? fraction * 100 : amount?.unit === "percent" ? num(amount?.remaining) : undefined;
        if (percent === undefined) continue;
        windows.push({
          provider,
          label: str(limit?.label) ?? str(window?.label) ?? "Usage",
          windowId: str(window?.id) ?? str(record(limit?.scope)?.windowId) ?? "",
          remainingPercent: Math.max(0, Math.min(100, Math.round(percent * 10) / 10)),
          resetsAt: num(window?.resetsAt) ?? null,
          status: str(limit?.status) ?? "unknown",
        });
      }
    }
    return windows.length > 0
      ? { available: true, observedAt, windows }
      : { available: false, observedAt, windows: [], detail: "Veyyon reported no usage windows." };
  } catch {
    return { available: false, observedAt, windows: [], detail: "Veyyon returned unreadable usage data." };
  }
}

export class FleetState {
  private readonly options: typeof DEFAULTS;
  private usageCache: { at: number; value: FleetUsage } | null = null;
  private usageInflight: Promise<FleetUsage> | null = null;

  constructor(private readonly sources: FleetSources, options: FleetStateOptions = {}) {
    this.options = { ...DEFAULTS, ...options };
  }

  private usage(now: number): Promise<FleetUsage> {
    const cached = this.usageCache;
    if (cached && now - cached.at < this.options.usageCacheMs) return Promise.resolve(cached.value);
    this.usageInflight ??= (async () => {
      try {
        // execFile's own timeout may not bound a shell-wrapped child on Windows, so the promise is raced too.
        const timeout = Promise.withResolvers<never>();
        const timer = setTimeout(() => timeout.reject(new Error("veyyon usage timed out")), this.options.usageTimeoutMs);
        const raw = await Promise.race([this.sources.runUsage(this.options.usageTimeoutMs), timeout.promise]).finally(() => clearTimeout(timer));
        const value = parseFleetUsage(raw, now);
        this.usageCache = { at: now, value };
        return value;
      } catch {
        // A failure is cached too: a hung or missing CLI must not be re-spawned on every refresh.
        const value: FleetUsage = { available: false, observedAt: now, windows: [], detail: "Veyyon usage is unavailable." };
        this.usageCache = { at: now, value };
        return value;
      } finally {
        this.usageInflight = null;
      }
    })();
    return this.usageInflight;
  }

  private lane(info: SessionFileInfo, base: Pick<FleetLane, "id" | "name" | "kind" | "parentId">, now: number): FleetLane {
    return {
      ...base,
      childIds: [],
      cwd: info.cwd,
      model: info.model,
      status: now - info.mtimeMs <= this.options.activeWindowMs ? "running" : "idle",
      startedAtMs: info.startedAtMs,
      elapsedMs: info.startedAtMs === null ? null : Math.max(0, now - info.startedAtMs),
      lastActivityMs: info.mtimeMs,
      lastAction: info.lastAction,
    };
  }

  private collect(file: string, parent: FleetLane | null, ancestry: Set<string>, lanes: FleetLane[], now: number): void {
    for (const childFile of this.sources.listChildFiles(file)) {
      const stem = path.basename(childFile).replace(/\.jsonl$/i, "");
      const info = this.sources.readSession(childFile);
      if (!info || !parent || now - info.mtimeMs > this.options.subagentLiveWindowMs) continue;
      const id = `${parent.id}/${stem}`;
      if (ancestry.has(id)) continue;
      const name = stem.split(".").pop() || stem;
      const child = this.lane(info, { id, name, kind: "subagent", parentId: parent.id }, now);
      parent.childIds.push(id);
      lanes.push(child);
      this.collect(childFile, child, new Set(ancestry).add(id), lanes, now);
    }
  }

  async snapshot(): Promise<FleetSnapshot> {
    const now = this.sources.now();
    const usagePromise = this.usage(now);
    const lanes: FleetLane[] = [];
    const owners = await this.sources.listOwners();
    const claims = new Map<string, number>();
    for (const owner of owners) claims.set(owner.sessionId, (claims.get(owner.sessionId) ?? 0) + 1);
    for (const owner of owners) {
      // Two owners claiming one session are ambiguous; neither is trusted (matches listSessions).
      if (claims.get(owner.sessionId) !== 1) continue;
      const info = this.sources.readSession(owner.sessionFile);
      const base = { id: owner.sessionId, name: info?.title ?? path.basename(owner.cwd) ?? owner.sessionId, kind: "interactive" as const, parentId: null };
      const root = info
        ? this.lane(info, base, now)
        : { ...base, childIds: [], cwd: owner.cwd, model: null, status: "idle" as const, startedAtMs: null, elapsedMs: null, lastActivityMs: 0, lastAction: null };
      lanes.push(root);
      this.collect(owner.sessionFile, root, new Set([root.id]), lanes, now);
    }
    const memory = this.sources.hostMemory();
    const usedPercent = memory.totalBytes > 0 ? Math.round((1 - memory.freeBytes / memory.totalBytes) * 1000) / 10 : 0;
    return {
      version: 1,
      observedAt: now,
      lanes,
      counts: {
        running: lanes.filter(lane => lane.status === "running").length,
        idle: lanes.filter(lane => lane.status === "idle").length,
        subagents: lanes.filter(lane => lane.kind === "subagent").length,
      },
      usage: await usagePromise,
      hostMemory: { ...memory, usedPercent },
      questions: this.sources.questions(),
    };
  }
}
