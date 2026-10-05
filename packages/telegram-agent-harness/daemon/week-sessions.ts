/**
 * week-sessions.ts — turns Veyyon session JSONL files into session blocks (issue #477).
 *
 * A block is a run of message timestamps with no gap over 30 min. Each session file yields one or
 * more blocks; subagent files are scanned too, and the overlap merge in week-summary counts the
 * parallel time once. Only `"type":"message"` lines are parsed, so tens-of-MB files stay cheap.
 * Fleet lanes from PR 447 (`FleetSnapshot`) mark blocks that are still running.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { execFile } from "node:child_process";
import { redactSecrets } from "../extension/sanitizer";
import type { FleetSnapshot } from "./fleet-state";
import { GAP_MS, type SessionBlock, type WeekBlock } from "./week-summary";

export const FIRST_MESSAGE_CHARS = 200;

export function splitBlocks(timestamps: number[], gapMs = GAP_MS): [number, number][] {
  const sorted = [...timestamps].filter(Number.isFinite).sort((a, b) => a - b);
  const out: [number, number][] = [];
  for (const t of sorted) {
    const last = out[out.length - 1];
    if (last && t - last[1] <= gapMs) last[1] = t;
    else out.push([t, t]);
  }
  return out;
}

/** Last two path segments collapse to the repo: `.wt-*` / `wt-*` worktrees report their parent directory. */
export function projectOf(cwd: string): string {
  const parts = cwd.split(/[\\/]+/).filter(Boolean);
  const last = parts[parts.length - 1] ?? "";
  if (/^\.?wt-/i.test(last) && parts.length > 1) return parts[parts.length - 2]!;
  return last || "unknown";
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? Object.fromEntries(Object.entries(value)) : null;
}

function firstText(content: unknown): string | null {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return null;
  for (const item of content) {
    const rec = asRecord(item);
    if (rec && rec.type === "text" && typeof rec.text === "string") return rec.text;
  }
  return null;
}

export interface ParsedSession {
  sessionId: string;
  cwd: string;
  model: string | null;
  firstMessage: string | null;
  timestamps: number[];
}

/** Parse the text of one session file. Exported for tests; `scanSessionFile` reads from disk. */
export function parseSessionText(text: string): ParsedSession | null {
  let sessionId: string | null = null;
  let cwd = "";
  let model: string | null = null;
  let firstMessage: string | null = null;
  const timestamps: number[] = [];
  for (const line of text.split("\n")) {
    if (!line.startsWith("{")) continue;
    const isMessage = line.startsWith('{"type":"message"');
    const isHeader = line.startsWith('{"type":"session"');
    const isModel = line.startsWith('{"type":"model_change"');
    if (!isMessage && !isHeader && !isModel) continue;
    let entry: Record<string, unknown> | null;
    try { entry = asRecord(JSON.parse(line)); } catch { continue; }
    if (!entry) continue;
    if (isHeader) {
      if (typeof entry.id === "string") sessionId = entry.id;
      if (typeof entry.cwd === "string") cwd = entry.cwd;
    } else if (isModel) {
      if (typeof entry.model === "string") model = entry.model;
    } else {
      const at = typeof entry.timestamp === "string" ? Date.parse(entry.timestamp) : NaN;
      if (Number.isFinite(at)) timestamps.push(at);
      const message = asRecord(entry.message);
      if (message && message.role === "user" && firstMessage === null) {
        const body = firstText(message.content);
        if (body && body.trim()) firstMessage = redactSecrets(body.trim().replace(/\s+/g, " ")).slice(0, FIRST_MESSAGE_CHARS);
      }
    }
  }
  return sessionId ? { sessionId, cwd, model, firstMessage, timestamps } : null;
}

export function blocksFromParsed(parsed: ParsedSession, gapMs = GAP_MS): SessionBlock[] {
  return splitBlocks(parsed.timestamps, gapMs).map(([startMs, endMs], index) => ({
    sessionId: parsed.sessionId,
    project: projectOf(parsed.cwd),
    cwd: parsed.cwd,
    tool: "veyyon",
    model: parsed.model,
    startMs,
    endMs,
    // The prompt belongs to the block it started; later blocks of the same session were resumed.
    firstMessage: index === 0 ? parsed.firstMessage : null,
  }));
}

export function scanSessionFile(file: string): SessionBlock[] {
  try {
    const parsed = parseSessionText(fs.readFileSync(file, "utf8"));
    return parsed ? blocksFromParsed(parsed) : [];
  } catch {
    return [];
  }
}

/** Every `*.jsonl` under `root` modified at or after `sinceMs` (files older than the window hold no activity in it). */
export function listSessionFiles(root: string, sinceMs: number): string[] {
  const out: string[] = [];
  const walk = (dir: string, depth: number) => {
    let entries: fs.Dirent[];
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory() && depth < 4) walk(full, depth + 1);
      else if (entry.isFile() && entry.name.endsWith(".jsonl")) {
        try { if (fs.statSync(full).mtimeMs >= sinceMs) out.push(full); } catch { /* vanished */ }
      }
    }
  };
  walk(root, 0);
  return out;
}

export function scanSessionRoot(root: string, sinceMs: number): SessionBlock[] {
  return listSessionFiles(root, sinceMs).flatMap(scanSessionFile);
}

/** Commit counts per block. Injected so tests need no git and a missing worktree yields null (unknown). */
export type CommitCounter = (cwd: string, startMs: number, endMs: number) => Promise<string[] | null>;

export const gitCommitCounter: CommitCounter = (cwd, startMs, endMs) => {
  const { promise, resolve } = Promise.withResolvers<string[] | null>();
  if (!cwd || !fs.existsSync(cwd)) { resolve(null); return promise; }
  // A commit shortly after the last message still belongs to the block.
  const until = new Date(endMs + 5 * 60_000).toISOString();
  // HEAD of the lane's own worktree: `--all` would credit a lane with its siblings' commits.
  execFile("git", ["log", "HEAD", "--no-merges", "--format=%H", `--since=${new Date(startMs).toISOString()}`, `--until=${until}`],
    { cwd, timeout: 15_000, windowsHide: true, maxBuffer: 4 * 1024 * 1024 }, (error, stdout) => {
      if (error) return resolve(null);
      resolve(stdout.split("\n").map(l => l.trim()).filter(l => /^[0-9a-f]{40}$/.test(l)).map(l => l.slice(0, 8)));
    });
  return promise;
};

export async function withCommits(blocks: SessionBlock[], count: CommitCounter, fleet?: { lanes: Pick<FleetSnapshot["lanes"][number], "id" | "status">[] } | null): Promise<WeekBlock[]> {
  const runningSessions = new Set((fleet?.lanes ?? []).filter(l => l.status === "running").map(l => l.id.split("/")[0]!));
  const out: WeekBlock[] = [];
  // Sequential on purpose: one `git` process at a time keeps the daemon light.
  for (const block of blocks) {
    const shas = await count(block.cwd, block.startMs, block.endMs);
    const live = runningSessions.has(block.sessionId) && block === lastBlockOf(blocks, block.sessionId);
    // A running lane may still commit, and unreadable git is unknown: neither is flagged.
    out.push({ ...block, commits: shas ? shas.length : null, commitShas: shas ?? [], noCommit: shas !== null && shas.length === 0 && !live, boards: [], live });
  }
  return out;
}

function lastBlockOf(blocks: SessionBlock[], sessionId: string): SessionBlock | undefined {
  let last: SessionBlock | undefined;
  for (const b of blocks) if (b.sessionId === sessionId && (!last || b.startMs > last.startMs)) last = b;
  return last;
}
