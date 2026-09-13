/** Channel-neutral execution guard. Transport origin is observability, never authority. */
import { createHash, randomUUID } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { evalCommands } from "./guard-eval";

export interface ToolGuardEvaluation {
  allowed: boolean;
  reason?: string;
  category?: string;
  approvalHash?: string;
}

// Historical inventory retained intact for diagnosis; NOT applied to arbitrary tool text.
const READ_ONLY_TOOLS: Record<string, true> = {
  read: true, glob: true, grep: true, ast_grep: true,
  read_memory: true, output: true, job: true,
};
const DESTRUCTIVE_INTENTS: Record<string, RegExp[]> = {
  shared_db_ddl_dml: [
    /\b(drop\s+(database|table|schema|index|view|column)|alter\s+table|truncate(\s+table)?|delete\s+from|update\s+\w+\s+set|insert\s+into)\b/i,
    /\b(alembic\s+(upgrade|downgrade|revision|stamp)|psql|supabase\s+db|pg_stat|pg_dump|pg_restore)\b/i,
    /\b(prod(uction)?\s+db|shared\s+(database|db|postgres))\b/i,
  ],
  deployments: [
    /\b(deploy\s+(prod|staging)|redeploy|compose\.redeploy|dokploy|staging-api\s+deploy)\b/i,
    /\b(fly\s+deploy|docker\s+compose\s+up|compose\.update)\b/i,
    /\b(production\s+deploy|deploy\s+prod)\b/i,
  ],
  cloudflare_stripe_mutations: [
    /\b(wrangler\s+(deploy|publish|secret|kv|d1)|cf\s+worker\s+(upload|deploy)|maint_mode\s*=\s*1)\b/i,
    /\b(stripe\s+(charges|customers|subscriptions|refunds|payment_intents)\s+(create|update|cancel|delete))\b/i,
    /\b(topup_main_reset|create_checkout_session)\b/i,
  ],
  destructive_git: [
    /\bgit\s+(reset\s+--hard|clean\s+-[fxd]+|branch\s+-[dD]|checkout\s+--\s+\.|restore\s+\.)\b/i,
    /\bgit\s+push\s+.*(main|staging)\b/i,
    /\bgit\s+push\b/i,
    /\b(force-push|force\s+push|--force)\b/i,
  ],
  secrets: [
    /\b(resend_api_key|supabase_key|stripe_key|service_role_key|jwt_secret)\b/i,
    /\b(private_key|secret_key|id_rsa|ps_live_[a-f0-9]+)\b/i,
  ],
  remote_ssh: [/\b(ssh|scp|sftp)\b/i],
  shell_destructive_os: [
    /\b(rm\s+-[rf]+|format\s+[a-z]:|shutdown|reboot|sudo\b|dd\s+if=|kill\s+-9|taskkill\s+\/f)\b/i,
  ],
};
function extractAllStrings(val: unknown, depth = 0): string[] {
  if (depth > 10 || val === null || val === undefined) return [];
  if (typeof val === "string") return [val];
  if (typeof val === "number" || typeof val === "boolean" || typeof val === "bigint") return [String(val)];
  if (Array.isArray(val)) {
    const elements = val.map(item => extractAllStrings(item, depth + 1).join(" ")).filter(Boolean);
    return [elements.join(" "), ...elements];
  }
  if (typeof val === "object") {
    const result: string[] = [], record = val as Record<string, unknown>;
    if (typeof record.application === "string" && Array.isArray(record.args)) {
      result.push(`${record.application} ${record.args.map(a => String(a)).join(" ")}`);
    }
    for (const key of Object.keys(record)) result.push(...extractAllStrings(record[key], depth + 1));
    return result;
  }
  return [];
}

const SECRET_PATH = /\b(id_rsa|service_role|jwt_secret|\.env\.prod)\b/i;
const PROTECTED = /^(main|master|staging|production|prod)$/i;
const PRODUCTION = /(?:\bzaraprptkegxqpvnsubu\b|\bakamai-iad-prod\b)/i;
const TTL = 15 * 60 * 1000;
export interface ApprovalRecord {
  version: 1;
  category: string;
  content: string;
  expiresAt: string;
  singleUse: true;
}
export function computeApprovalHash(category: string, content: string): string {
  return createHash("sha256").update(`${category}:${content.trim()}`).digest("hex");
}
function validRecord(data: ApprovalRecord, token: string): boolean {
  return data.version === 1 && data.singleUse === true && typeof data.category === "string" &&
    typeof data.content === "string" && typeof data.expiresAt === "string" &&
    Number.isFinite(Date.parse(data.expiresAt)) && Date.parse(data.expiresAt) > Date.now() &&
    computeApprovalHash(data.category, data.content) === token;
}
/** Only an authenticated operator command may call this; the guard never approves itself. */
export function approveOperation(stateDir: string, token: string): ApprovalRecord {
  if (!/^[a-f0-9]{64}$/.test(token)) throw new Error("Use the complete 64-character approval token.");
  const pending = path.join(stateDir, "approved", "pending", `${token}.json`);
  let record: ApprovalRecord;
  try { record = JSON.parse(fs.readFileSync(pending, "utf8")); }
  catch { throw new Error("No pending operation for this token. Retry the refused call to request approval."); }
  if (!validRecord(record, token)) throw new Error("Approval request expired or invalid. Retry the refused call.");
  const approved = path.join(stateDir, "approved", `${token}.json`);
  const claim = `${pending}.${randomUUID()}.claimed`;
  // Atomic rename prevents two command processes from approving the same request.
  fs.renameSync(pending, claim);
  try {
    record.expiresAt = new Date(Date.now() + TTL).toISOString();
    fs.writeFileSync(approved, JSON.stringify(record), { flag: "wx", mode: 0o600 });
  } finally { fs.unlinkSync(claim); }
  return record;
}

/** Shell words, not a prose scan: separators outside quotes introduce invocations. */
export function shellCommands(command: string): string[][] {
  const commands: string[][] = [], words: string[] = [];
  let word = "", quote = "", started = false;
  const flushWord = () => { if (started) words.push(word); word = ""; started = false; };
  const flush = () => { flushWord(); if (words.length) commands.push(words.splice(0)); };
  for (let i = 0; i < command.length; i++) {
    const c = command[i];
    if (quote) {
      if (c === quote) { quote = ""; continue; }
      if (c === "\\" && quote === '"' && /["\\$`\n]/.test(command[i + 1] ?? "")) { word += command[++i]; continue; }
      word += c; continue;
    }
    if (c === "'" || c === '"') { quote = c; started = true; }
    else if (c === "#" && !started) { while (i < command.length && command[i] !== "\n") i++; flush(); }
    else if (/[;&|\n]/.test(c)) flush();
    else if (/\s/.test(c)) flushWord();
    else if (c === "\\" && /[\s'";&|]/.test(command[i + 1] ?? "")) { started = true; word += command[++i]; }
    else { word += c; started = true; }
  }
  flush();
  return commands;
}
function executable(value: string): string {
  return value.replace(/\\/g, "/").split("/").pop()!.replace(/\.(exe|cmd|bat)$/i, "").toLowerCase();
}
function commandCategory(words: string[], depth = 0): string | undefined {
  if (depth > 8) return "shell_destructive_os";
  while (words.length && /^[A-Za-z_]\w*=/.test(words[0])) words = words.slice(1);
  if (!words.length) return;
  const app = executable(words[0]), args = words.slice(1), command = [app, ...args].join(" ");
  if (app === "env" || app === "command" || app === "exec" || app === "call" || app === "&") return commandCategory(args, depth + 1);
  // A real process may read keys regardless of the transport. Never inspect source-file contents.
  if (args.some(arg => SECRET_PATH.test(arg) || DESTRUCTIVE_INTENTS.secrets.some(re => re.test(arg)))) return "secrets";
  if (PRODUCTION.test(command)) return "production_exclusion";
  if (/^(sh|bash|zsh|cmd|powershell|pwsh)$/.test(app)) {
    const i = args.findIndex(arg => /^(-[a-z]*c|\/c|-command)$/i.test(arg));
    if (i >= 0) return shellCommands(args.slice(i + 1).join(" ")).map(c => commandCategory(c, depth + 1)).find(Boolean);
  }
  if (/^(node|bun|python|python3)$/.test(app)) {
    const i = args.findIndex(arg => /^(-c|-e|--eval)$/.test(arg));
    if (i >= 0) {
      const result = evalCommands(args[i + 1] ?? "", app.startsWith("python") ? "py" : "js", shellCommands);
      if (result.unresolved) return "shell_destructive_os";
      return result.commands.map(c => commandCategory(c, depth + 1)).find(Boolean);
    }
  }
  if (/^(ssh|scp|sftp)$/.test(app)) return "remote_ssh";
  if (DESTRUCTIVE_INTENTS.shell_destructive_os.some(re => re.test(command)) && /^(rm|format|shutdown|reboot|sudo|dd|kill|taskkill)$/.test(app)) return "shell_destructive_os";
  if (/^(psql|pg_dump|pg_restore|pg_stat|alembic)$/.test(app) || app === "supabase" && args[0] === "db") return "shared_db_ddl_dml";
  if (/^(deploy|redeploy|dokploy|compose\.redeploy|compose\.update)$/.test(app) ||
      /^(fly|staging-api|polysim-deploy)$/.test(app) && args.some(a => /^(deploy|staging|prod|production)$/.test(a)) ||
      app === "docker" && args[0] === "compose" && args.includes("up")) return "deployments";
  if (app === "wrangler" && /^(deploy|publish|secret|kv|d1)$/.test(args[0] ?? "") ||
      app === "cf" && args[0] === "worker" && /^(upload|deploy)$/.test(args[1] ?? "") ||
      app === "stripe" && /^(charges|customers|subscriptions|refunds|payment_intents)$/.test(args[0] ?? "") && /^(create|update|cancel|delete)$/.test(args[1] ?? "") ||
      /^(topup_main_reset|create_checkout_session)$/.test(app)) return "cloudflare_stripe_mutations";
  if (app !== "git") return;
  let i = 0;
  while (i < args.length && args[i].startsWith("-")) {
    if (/^(-C|-c|--git-dir|--work-tree)$/.test(args[i])) i += 2; else i++;
  }
  const action = args[i], rest = args.slice(i + 1);
  if (rest.some(a => /^--force(?:-with-lease|-if-includes)?(?:=|$)/.test(a)) ||
      /^(rebase|filter-branch|filter-repo)$/.test(action ?? "") ||
      action === "reset" && rest.includes("--hard") || action === "clean" ||
      action === "branch" && rest.some(a => /^-[^-]*D/.test(a) || a === "--delete" && rest.includes("-f")) ||
      action === "checkout" && rest.includes("--") && rest.includes(".") ||
      action === "restore" && rest.includes(".")) return "destructive_git";
  if (action !== "push") return;
  if (rest.some(a => /^-[^-]*f/.test(a) || /^\+/.test(a) || /^(--delete|--mirror|--all)$/.test(a))) return "destructive_git";
  const refs = rest.filter(a => !a.startsWith("-"));
  // No explicit destination means configured refspecs; require exact approval rather than guess.
  if (refs.length < 2) return "destructive_git";
  if (refs.slice(1).some(ref => PROTECTED.test(ref.split(":").pop()!.replace(/^refs\/heads\//, "")) || ref.startsWith(":"))) return "destructive_git";
}
function protectedPath(input: unknown, depth = 0): boolean {
  if (!input || typeof input !== "object" || depth > 10) return false;
  return Object.entries(input).some(([key, value]) =>
    /^(path|paths|file|files|cwd|directory|source|destination)$/.test(key)
      ? extractAllStrings(value).some(value => SECRET_PATH.test(value))
      : typeof value === "object" && protectedPath(value, depth + 1));
}
export type TurnOrigin = "IDLE" | "TELEGRAM_ACTIVE" | "LOCAL_ACTIVE";
export class DangerousToolGuard {
  private currentTurnState: TurnOrigin = "IDLE";
  private activeTelegramTurnId: string | null = null;
  constructor(private stateDir: string) {}
  public startTelegramTurn(turnId?: string): void { this.currentTurnState = "TELEGRAM_ACTIVE"; this.activeTelegramTurnId = turnId || String(Date.now()); }
  public startLocalTurn(): void { this.currentTurnState = "LOCAL_ACTIVE"; this.activeTelegramTurnId = null; }
  public endTurn(): void { this.currentTurnState = "IDLE"; this.activeTelegramTurnId = null; }
  public isTelegramTurnActive(): boolean { return this.currentTurnState === "TELEGRAM_ACTIVE"; }
  public evaluateToolCall(toolName: string, input: Record<string, unknown>, _isTelegramOverride?: boolean): ToolGuardEvaluation {
    // Origin intentionally unused: every call follows exactly the same permission path.
    let category: string | undefined;
    if (protectedPath(input)) category = "secrets";
    else if (toolName === "bash") category = shellCommands(String(input.command ?? "")).map(c => commandCategory(c)).find(Boolean);
    else if (toolName === "launch" && input.op === "start") category = commandCategory([String(input.application ?? ""), ...(Array.isArray(input.args) ? input.args.map(String) : [])]);
    else if (toolName === "eval") {
      const result = evalCommands(String(input.code ?? ""), String(input.language ?? "js"), shellCommands);
      category = result.unresolved ? "shell_destructive_os" : result.commands.map(c => commandCategory(c)).find(Boolean);
    } else if (toolName === "ssh") category = "remote_ssh"; // Explicit execution tool, never a prose match.
    else if (toolName in READ_ONLY_TOOLS) return { allowed: true };
    if (!category) return { allowed: true };
    if (category === "production_exclusion") return { allowed: false, category, reason: "Production is excluded for every transport; an approval cannot override this boundary." };
    // Bind approval to the full exact input, including cwd/env, not just matched words.
    const content = JSON.stringify({ toolName, input });
    const approvalHash = this.computeApprovalHash(category, content);
    if (this.isLocallyApproved(approvalHash)) return { allowed: true };
    const pendingDir = path.join(this.stateDir, "approved", "pending");
    try {
      fs.mkdirSync(pendingDir, { recursive: true });
      const record: ApprovalRecord = { version: 1, category, content, expiresAt: new Date(Date.now() + TTL).toISOString(), singleUse: true };
      fs.writeFileSync(path.join(pendingDir, `${approvalHash}.json`), JSON.stringify(record), { mode: 0o600 });
    } catch {
      return { allowed: false, category, approvalHash, reason: `Approval token: ${approvalHash}. Cannot write approval request in ${pendingDir}; restore directory write access and retry. No operation executed.` };
    }
    return { allowed: false, category, approvalHash,
      reason: `Operation '${category}' requires exact operator approval. Approval token: ${approvalHash}. Send /approve ${approvalHash} to this session's Telegram bot, then retry the identical call once (15 minute expiry). Local equivalent: bun "${path.join(import.meta.dir, "guard.ts")}" approve "${this.stateDir}" ${approvalHash}` };
  }
  private computeApprovalHash(category: string, content: string): string { return computeApprovalHash(category, content); }
  private isLocallyApproved(approvalHash: string): boolean {
    const file = path.join(this.stateDir, "approved", `${approvalHash}.json`);
    const claim = `${file}.${randomUUID()}.consumed`;
    try {
      fs.renameSync(file, claim); // Exactly one competing caller can consume the grant.
      return validRecord(JSON.parse(fs.readFileSync(claim, "utf8")), approvalHash);
    } catch { return false; }
    finally { try { fs.unlinkSync(claim); } catch {} }
  }
}
if (import.meta.main) {
  try {
    const [action, stateDir, token] = process.argv.slice(2);
    if (action !== "approve" || !stateDir || !token) throw new Error("Usage: bun guard.ts approve <stateDir> <full-token>");
    const record = approveOperation(stateDir, token);
    console.log(JSON.stringify({ approved: true, token, expiresAt: record.expiresAt, singleUse: true }));
  } catch (error) { console.error(error instanceof Error ? error.message : String(error)); process.exitCode = 1; }
}
