import { Database } from "bun:sqlite";
import { randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { redactSecrets } from "./sanitizer";

export interface ApprovalContext {
  sessionId: string;
  requester: string;
  task: string;
  cwd: string;
  toolCallId?: string;
}
export interface ApprovalRecord extends ApprovalContext {
  token: string;
  operationHash: string;
  category: string;
  command: string;
  target: string;
  reason: string;
  details: string;
  approvable: boolean;
  requestedAt: string;
  expiresAt: string;
  state: "pending" | "approved" | "denied" | "consumed" | "expired";
}
export interface ApprovalActor { sessionId: string; userId: string; chatId: string }
export type ApprovalDescription = Omit<ApprovalRecord, "token" | "operationHash" | "requestedAt" | "expiresAt" | "state">;
const TTL = 15 * 60 * 1000;
const REASONS: Record<string, string> = {
  shell_destructive_os: "This operation can stop processes, remove files, or execute a process whose arguments cannot be resolved statically.",
  destructive_git: "This operation can overwrite repository history or change a protected branch.",
  shared_db_ddl_dml: "This operation can change database structure or stored data.",
  deployments: "This operation can change running services or publish a deployment.",
  cloudflare_stripe_mutations: "This operation can change cloud resources or payment state.",
  remote_ssh: "This operation executes on or transfers files to a remote host.",
  secrets: "This operation accesses a protected secret-bearing path.",
};

/** Keep credentials out of both Telegram and the durable audit. Bind the original input by hash only. */
export function describeApproval(tool: string, input: Record<string, unknown>, category: string, context: ApprovalContext, commands: string[][] = [], unresolved = false): ApprovalDescription {
  const command = typeof input.command === "string" ? input.command : typeof input.code === "string" ? input.code :
    typeof input.application === "string" ? JSON.stringify([input.application, ...(Array.isArray(input.args) ? input.args : [])]) : JSON.stringify(input);
  const secrets = Object.entries((input.env && typeof input.env === "object" ? input.env : {}) as Record<string, unknown>)
    .filter(([key, value]) => /token|secret|password|credential|api.?key|authorization/i.test(key) && typeof value === "string" && value.length > 0)
    .map(([, value]) => value as string);
  const safe = (text: string): string => {
    for (const secret of secrets) text = text.split(secret).join("[REDACTED_SECRET]");
    return redactSecrets(text)
      .replace(/(\b(?:[\w-]*(?:token|password|secret|api[_-]?key|authorization)[\w-]*)["']?\s*(?:[:=]|\s)\s*)["']?[^\s"',;]+/gi, "$1[REDACTED_SECRET]")
      .replace(/(https?:\/\/)[^\s/@]+:[^\s/@]+@/gi, "$1[REDACTED_SECRET]@")
      .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u202a-\u202e\u2066-\u2069]/g, c => `\\u${c.charCodeAt(0).toString(16).padStart(4, "0")}`);
  };
  const safeCommand = safe(command);
  const cwd = safe(String(input.cwd ?? context.cwd));
  const explicit = [input.path, input.host, input.hostname, input.name].filter(x => typeof x === "string").join("; ");
  const operands = commands.flatMap(words => words.slice(1).filter(word => !word.startsWith("-"))).join("; ");
  const target = safe([explicit || operands || (tool === "eval" ? "Files/processes named in the script" : "Whole-host or dynamically resolved target; inspect the complete command"), unresolved ? "Some process arguments are dynamic: their runtime targets cannot be proven from this request." : ""].filter(Boolean).join(". "));
  // env values are never copied to the display or audit, including apparently innocuous ones.
  const details = safe(JSON.stringify({ tool, cwd, ...Object.fromEntries(Object.entries(input).filter(([key]) => !["command", "code", "application", "args", "env"].includes(key))), environmentKeys: Object.keys((input.env ?? {}) as object) }, null, 2));
  return { ...context, requester: safe(context.requester), task: safe(context.task), cwd, category, command: safeCommand, target,
    reason: unresolved ? "The script executes a subprocess with dynamically constructed arguments. The guard cannot prove its runtime effects, so it requires approval; this does not mean a destructive action was observed." : REASONS[category] ?? "This category requires exact operator authorization.", details,
    // Never put an approval button on a redacted or truncated operation.
    approvable: safeCommand === command && !safeCommand.includes("[REDACTED_SECRET]") };
}

function withStore<T>(stateDir: string, run: (db: Database) => T): T {
  fs.mkdirSync(stateDir, { recursive: true });
  const file = path.join(stateDir, "approval-audit.sqlite");
  const db = new Database(file, { create: true });
  try {
    db.exec("PRAGMA busy_timeout=5000; PRAGMA journal_mode=WAL;");
    db.exec(`CREATE TABLE IF NOT EXISTS approvals (token TEXT PRIMARY KEY, operation_hash TEXT NOT NULL, session_id TEXT NOT NULL, requester TEXT NOT NULL, state TEXT NOT NULL, expires_at TEXT NOT NULL, record TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS approval_operation ON approvals(operation_hash, session_id, requester);
      CREATE TABLE IF NOT EXISTS approval_events (id INTEGER PRIMARY KEY, token TEXT NOT NULL, decision TEXT NOT NULL, at TEXT NOT NULL, actor TEXT NOT NULL, record TEXT NOT NULL);`);
    fs.chmodSync(file, 0o600);
    return db.transaction(() => run(db)).immediate();
  } finally { db.close(); }
}
function persist(db: Database, record: ApprovalRecord, actor: string): void {
  const json = JSON.stringify(record);
  db.run("INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(token) DO UPDATE SET state=excluded.state, expires_at=excluded.expires_at, record=excluded.record", [record.token, record.operationHash, record.sessionId, record.requester, record.state, record.expiresAt, json]);
  db.run("INSERT INTO approval_events(token, decision, at, actor, record) VALUES (?, ?, ?, ?, ?)", [record.token, record.state, new Date().toISOString(), actor, json]);
}
function expire(db: Database): void {
  const rows = db.query("SELECT record FROM approvals WHERE state IN ('pending','approved') AND expires_at <= ?").all(new Date().toISOString()) as { record: string }[];
  for (const row of rows) persist(db, { ...JSON.parse(row.record), state: "expired" }, "clock");
}

/** The transaction makes permission consumption and its audit entry indivisible. */
export function evaluateApproval(stateDir: string, operationHash: string, description: ApprovalDescription): ApprovalRecord {
  return withStore(stateDir, db => {
    expire(db);
    const row = db.query("SELECT record FROM approvals WHERE operation_hash=? AND session_id=? AND requester=? AND state IN ('pending','approved','denied') ORDER BY rowid DESC LIMIT 1").get(operationHash, description.sessionId, description.requester) as { record: string } | null;
    if (row) {
      const record: ApprovalRecord = JSON.parse(row.record);
      if (record.state === "approved") { record.state = "consumed"; persist(db, record, description.requester); }
      return record;
    }
    const record: ApprovalRecord = { ...description, operationHash, token: randomBytes(32).toString("hex"), requestedAt: new Date().toISOString(), expiresAt: new Date(Date.now() + TTL).toISOString(), state: "pending" };
    persist(db, record, record.requester);
    return record;
  });
}

export function decideApproval(stateDir: string, token: string, decision: "approved" | "denied", actor: ApprovalActor): ApprovalRecord {
  if (!/^[a-f0-9]{64}$/.test(token)) throw new Error("Use the complete 64-character approval token.");
  const result = withStore(stateDir, db => {
    expire(db);
    const row = db.query("SELECT record FROM approvals WHERE token=?").get(token) as { record: string } | null;
    if (!row) return "No pending operation for this token.";
    const record: ApprovalRecord = JSON.parse(row.record);
    if (record.sessionId !== actor.sessionId) return "This request belongs to another session.";
    if (record.state !== "pending") return `Request is ${record.state}; no permission changed.`;
    if (decision === "approved" && !record.approvable) return "Command contains redacted secrets. Resubmit without embedded credentials; blind approval is disabled.";
    record.state = decision;
    persist(db, record, JSON.stringify(actor));
    return record;
  });
  if (typeof result === "string") throw new Error(result);
  return result;
}

export function approvalCallback(token: string, decision: "approved" | "denied"): string {
  if (!/^[a-f0-9]{64}$/.test(token)) throw new Error("Invalid approval token");
  return `ap:${decision === "approved" ? "a" : "d"}:${Buffer.from(token, "hex").toString("base64url")}`;
}
export function parseApprovalCallback(data: string): { token: string; decision: "approved" | "denied" } | null {
  const match = /^ap:([ad]):([A-Za-z0-9_-]{43})$/.exec(data);
  if (!match) return null;
  const token = Buffer.from(match[2], "base64url").toString("hex");
  const decision = match[1] === "a" ? "approved" : "denied";
  return approvalCallback(token, decision) === data ? { token, decision } : null;
}
export function approvalOutcome(record: ApprovalRecord): string {
  return `Operator ${record.state} ${record.requester}'s ${record.category} request (${record.toolCallId ?? record.token}). Task: ${record.task}. ${record.state === "denied" ? "Denied — this call is blocked at the gate. The gate cannot prove no equivalent action ran elsewhere; continue independent work." : `One identical retry is authorized before ${record.expiresAt}; this decision did not execute anything.`}`;
}
