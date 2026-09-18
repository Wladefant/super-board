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
  summary: string;
  command: string;
  target: string;
  reason: string;
  details: string;
  approvable: boolean;
  requestedAt: string;
  expiresAt: string;
  state: "pending" | "approved" | "denied" | "consumed" | "expired";
  summary?: string;
}
export interface ApprovalActor { sessionId: string; userId: string; chatId: string }
export type ApprovalDescription = Omit<ApprovalRecord, "token" | "operationHash" | "requestedAt" | "expiresAt" | "state">;
const TTL = 15 * 60 * 1000;
const REASONS: Record<string, string> = {
  shell_destructive_os: "This operation performs destructive filesystem or OS-level modifications.",
  destructive_git: "This operation can overwrite repository history or delete protected branches.",
  shared_db_ddl_dml: "This operation can modify database schema or overwrite stored data.",
  deployments: "This operation publishes deployments or updates live service infrastructure.",
  cloudflare_stripe_mutations: "This operation modifies live cloud infrastructure, keys, or payment records.",
  remote_ssh: "This operation executes commands or transfers files on a remote host.",
  secrets: "This operation accesses protected credentials, private keys, or environment secrets.",
};

/** Keep credentials out of both Telegram and the durable audit. Bind the original input by hash only. */
function findCommandWords(commands: string[][], targetApps: string[]): string[] | undefined {
  for (const cmd of commands) {
    let words = cmd;
    while (words.length && /^[A-Za-z_]\w*=/.test(words[0])) words = words.slice(1);
    while (words.length && /^(env|command|exec|call|if|then|do|while|!|time|nohup|xargs|sudo)$/i.test(words[0])) {
      words = words.slice(1);
      while (words.length && words[0].startsWith("-")) words = words.slice(1);
    }
    if (!words.length) continue;
    const app = words[0].replace(/\\/g, "/").split("/").pop()!.replace(/\.(exe|cmd|bat)$/i, "").toLowerCase();
    if (targetApps.includes(app)) return words;
    if (/^(sh|bash|zsh|cmd|powershell|pwsh)$/i.test(app)) {
      const idx = words.findIndex(w => /^(-[a-z]*c|\/c|-command)$/i.test(w));
      if (idx >= 0 && words[idx + 1]) {
        const inner = words.slice(idx + 1).join(" ").trim().split(/\s+/);
        const innerApp = inner[0]?.replace(/\\/g, "/").split("/").pop()!.replace(/\.(exe|cmd|bat)$/i, "").toLowerCase();
        if (targetApps.includes(innerApp)) return inner;
      }
    }
  }
  return undefined;
}

export function formatApprovalSummary(category: string, tool: string, input: Record<string, unknown>, commands: string[][], command: string): string {
  if (category === "destructive_git") {
    const gitCmd = findCommandWords(commands, ["git"]);
    if (gitCmd) {
      let idx = 1;
      while (idx < gitCmd.length && gitCmd[idx].startsWith("-")) {
        if (/^(-C|-c|--git-dir|--work-tree)$/.test(gitCmd[idx])) idx += 2;
        else idx++;
      }
      const action = gitCmd[idx];
      const rest = gitCmd.slice(idx + 1);
      if (action === "filter-branch" || action === "filter-repo") {
        return `Rewrite repository history with git ${action}`;
      }
      if (action === "push") {
        const isMirror = rest.some(a => a === "--mirror");
        const isDelete = rest.some(a => a === "--delete" || /^-[^-]*d$/.test(a));
        const nonFlags = rest.filter(a => !a.startsWith("-"));
        const remote = nonFlags[0] || "origin";
        const refspecs = nonFlags.slice(1);
        const refspec = refspecs[0] || "";
        if (isMirror) {
          return `Mirror push all refs to ${remote} (overwrites remote repository)`;
        }
        if (isDelete || refspec.startsWith(":")) {
          const branch = refspec ? refspec.replace(/^:/, "").replace(/^refs\/heads\//, "") : "branch";
          return `Delete branch ${branch} on ${remote}`;
        }
        const branch = refspec ? (refspec.includes(":") ? refspec.split(":").pop()! : refspec).replace(/^\+/, "").replace(/^refs\/heads\//, "") : "branch";
        return `Force-push branch ${branch} to ${remote} (rewrites shared history)`;
      }
    }
    return "Force-push git repository (rewrites shared history)";
  }

  if (category === "shell_destructive_os") {
    const osCmd = findCommandWords(commands, ["rm", "rmdir", "remove-item", "format", "dd", "shutdown", "reboot"]);
    if (osCmd) {
      const app = osCmd[0]?.replace(/\\/g, "/").split("/").pop()!.replace(/\.(exe|cmd|bat)$/i, "").toLowerCase();
      const args = osCmd.slice(1);
      if (app === "rm" || app === "rmdir" || app === "remove-item") {
        const targets = args.filter(a => !a.startsWith("-") && !/^[\/-][sq]/i.test(a)).join(" ");
        return `Remove files recursively at ${targets || "target path"}`;
      }
      if (app === "format") {
        const drive = args.find(a => /^[a-z]:$/i.test(a)) || "drive";
        return `Format drive ${drive}`;
      }
      if (app === "dd") return "Low-level disk overwrite with dd";
      if (app === "shutdown") return "Shut down the operating system";
      if (app === "reboot") return "Reboot the operating system";
    }
    return "Execute destructive operating system command";
  }

  if (category === "shared_db_ddl_dml") {
    const dbCmd = findCommandWords(commands, ["psql", "alembic", "supabase", "pg_restore"]);
    if (dbCmd) {
      const app = dbCmd[0]?.replace(/\\/g, "/").split("/").pop()!.replace(/\.(exe|cmd|bat)$/i, "").toLowerCase();
      const args = dbCmd.slice(1);
      if (app === "psql") return "Execute database queries via psql";
      if (app === "pg_restore") return "Restore database dump via pg_restore";
      if (app === "alembic") {
        const sub = args.find(a => !a.startsWith("-")) || "migration";
        return `Run database migration via alembic ${sub}`;
      }
      if (app === "supabase") {
        const sub = args.filter(a => !a.startsWith("-")).join(" ") || "db command";
        return `Modify database via supabase ${sub}`;
      }
    }
    return "Execute shared database DDL/DML operation";
  }

  if (category === "deployments") {
    const depCmd = findCommandWords(commands, ["dokploy", "fly", "wrangler", "deploy"]);
    if (depCmd) {
      const app = depCmd[0]?.replace(/\\/g, "/").split("/").pop()!.replace(/\.(exe|cmd|bat)$/i, "").toLowerCase();
      const args = depCmd.slice(1);
      if (app === "dokploy") return "Trigger deployment via dokploy";
      if (app === "fly") return "Deploy application using fly deploy";
      if (app === "wrangler") {
        const sub = args.find(a => !a.startsWith("-")) || "deploy";
        return `Deploy worker via wrangler ${sub}`;
      }
      if (/\b(prod|production)\b/i.test(args.join(" "))) return "Deploy application to production";
    }
    if (/\bdeploy\s+(prod|production)\b/i.test(command)) return "Deploy application to production";
    return "Publish or update application deployment";
  }

  if (category === "cloudflare_stripe_mutations") {
    const cfCmd = findCommandWords(commands, ["stripe", "wrangler", "topup_main_reset"]);
    if (cfCmd) {
      const app = cfCmd[0]?.replace(/\\/g, "/").split("/").pop()!.replace(/\.(exe|cmd|bat)$/i, "").toLowerCase();
      const args = cfCmd.slice(1);
      if (app === "stripe") {
        const sub = args.filter(a => !a.startsWith("-")).slice(0, 2).join(" ");
        return `Mutate payment resource via stripe ${sub || "mutation"}`;
      }
      if (app === "wrangler") {
        const sub = args.find(a => !a.startsWith("-")) || "resource";
        return `Modify Cloudflare resources via wrangler ${sub}`;
      }
      if (app === "topup_main_reset") return "Reset main account balance";
    }
    return "Modify cloud resources or payment state";
  }

  if (category === "remote_ssh") {
    const host = String(input.host ?? input.hostname ?? (tool === "ssh" ? input.host : undefined) ?? "");
    return `Execute command on remote host ${host || "remote"}`;
  }

  if (category === "secrets") {
    const explicit = [input.path, input.file].filter(x => typeof x === "string").join("; ");
    return `Access protected sensitive path (${explicit || "secret"})`;
  }

  if (category === "production_exclusion") {
    return "Access production environment (strictly prohibited)";
  }

  return `Execute guarded ${category.replace(/_/g, " ")} operation`;
}

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
  const details = safe(JSON.stringify({ tool, cwd, ...Object.fromEntries(Object.entries(input).filter(([key]) => !["command", "code", "application", "args", "env"].includes(key))), environmentKeys: Object.keys((input.env ?? {}) as object), ...(unresolved ? { unresolvedProcessArguments: true } : {}) }, null, 2));
  const summary = safe(formatApprovalSummary(category, tool, input, commands, safeCommand));
  return { ...context, requester: safe(context.requester), task: safe(context.task), cwd, category, summary, command: safeCommand, target,
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
  if (record.state === "denied") {
    return "Operator denied: do not run it or work around it; continue other work.";
  }
  return `Operator approved: run the identical call now (valid until ${record.expiresAt}).`;
}
