/** Channel-neutral execution guard. Transport origin is observability, never authority. */
import { createHash } from "node:crypto";
import * as path from "node:path";
import { evalCommands } from "./guard-eval";
import { describeApproval, evaluateApproval, decideApproval, type ApprovalContext, type ApprovalActor, type ApprovalRecord } from "./approvals";

export interface ToolGuardEvaluation {
  allowed: boolean;
  reason?: string;
  category?: string;
  approvalHash?: string;
  approval?: ApprovalRecord;
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

const SECRET_PATH = /(?:^|[/\\])\.env\.prod\b|\b(id_rsa|service_role|jwt_secret|\.env\.prod)\b/i;
const PROTECTED = /^(main|master|staging|production|prod)$/i;
const PRODUCTION = /(?:\bzaraprptkegxqpvnsubu\b|\bakamai-iad-prod\b)/i;
const LOCAL_CONTEXT: ApprovalContext = { sessionId: "local", requester: "Local operator", task: "Local guarded operation", cwd: process.cwd() };
export function computeApprovalHash(category: string, content: string): string {
  return createHash("sha256").update(`${category}:${content.trim()}`).digest("hex");
}
/** Only the authenticated Telegram transport or an explicit local operator command calls this. */
export function approveOperation(stateDir: string, token: string, actor: ApprovalActor = { sessionId: "local", userId: "local-operator", chatId: "local" }): ApprovalRecord {
  return decideApproval(stateDir, token, "approved", actor);
}

/** Shell words, not a prose scan: separators outside quotes introduce invocations. */
export function shellCommands(command: string): string[][] {
  const commands: string[][] = [], words: string[] = [];
  const heredocs: { delimiter: string; quoted: boolean; stripTabs: boolean }[] = [];
  let word = "", quote = "", started = false;
  const flushWord = () => { if (started) words.push(word); word = ""; started = false; };
  const flush = () => { flushWord(); if (words.length) commands.push(words.splice(0)); };
  for (let i = 0; i < command.length; i++) {
    const c = command[i];
    // Command substitution executes even inside double quotes; single quotes remain data.
    if (quote !== "'" && (command.slice(i, i + 2) === "$(" || c === "`")) {
      const backtick = c === "`";
      let end = i + (backtick ? 1 : 2), nesting = 1;
      const start = end;
      for (; end < command.length; end++) {
        if (command[end] === "\\") { end++; continue; }
        if (backtick && command[end] === "`") break;
        if (!backtick && command[end] === "(") nesting++;
        if (!backtick && command[end] === ")" && --nesting === 0) break;
      }
      commands.push(...shellCommands(command.slice(start, end)));
      i = end; started = true; continue;
    }
    if (quote) {
      if (c === quote) { quote = ""; continue; }
      if (c === "\\" && quote === '"' && /["\\$`\n]/.test(command[i + 1] ?? "")) { word += command[++i]; continue; }
      word += c; continue;
    }
    if (command.slice(i, i + 2) === "<<" && command[i + 2] !== "<") {
      const match = /^<<(-?)\s*(?:'([^']+)'|"([^"]+)"|([^\s;&|]+))/.exec(command.slice(i));
      if (match) {
        flushWord();
        heredocs.push({ delimiter: match[2] ?? match[3] ?? match[4], quoted: Boolean(match[2] || match[3]), stripTabs: match[1] === "-" });
        i += match[0].length - 1; continue;
      }
    }
    if (c === "\n" && heredocs.length) {
      flush();
      for (const document of heredocs.splice(0)) {
        let body = "", start = i + 1;
        while (start < command.length) {
          let end = command.indexOf("\n", start);
          if (end < 0) end = command.length;
          const line = command.slice(start, end).replace(/\r$/, "");
          if ((document.stripTabs ? line.replace(/^\t+/, "") : line) === document.delimiter) { i = end; break; }
          body += line + "\n"; start = end + 1; i = end;
        }
        // Unquoted heredocs expand substitutions, but never execute their plain text.
        if (!document.quoted) commands.push(...shellCommands(`echo "${body.replace(/"/g, '\\"')}"`));
      }
      continue;
    }
    if (c === "'" || c === '"') { quote = c; started = true; }
    else if (c === "#" && !started) { while (i < command.length && command[i] !== "\n") i++; flush(); }
    else if (/[;&|\n()]/.test(c)) flush();
    else if (/\s/.test(c)) flushWord();
    else if (c === "\\" && /[\s'";&|$`]/.test(command[i + 1] ?? "")) { started = true; word += command[++i]; }
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
  // The shell builtin reparses its argument string; argv wrappers do not.
  if (app === "eval") {
    const script = (args[0] === "--" ? args.slice(1) : args).join(" ");
    return selectCategory(shellCommands(script).map(c => commandCategory(c, depth + 1)));
  }
  if (/^(env|command|exec|call|if|then|do|while|!|time|nohup|xargs)$/.test(app)) {
    let offset = 0;
    while (args[offset]?.startsWith("-")) {
      if (args[offset] === "--") { offset++; break; }
      const takesValue = app === "xargs"
        ? /^(-[aEdInPLs]|--arg-file|--eof|--delimiter|--replace|--max-args|--max-procs|--max-lines|--max-chars|--process-slot-var)$/.test(args[offset])
        : app === "time"
          ? /^(-[fo]|--format|--output)$/.test(args[offset])
          : /^(--unset|-u|-a)$/.test(args[offset]);
      offset += takesValue ? 2 : 1;
    }
    return commandCategory(args.slice(offset), depth + 1);
  }
  // A real process may read keys regardless of the transport. Never inspect source-file contents.
  if (PRODUCTION.test(command)) return "production_exclusion";
  if (args.some(arg => SECRET_PATH.test(arg) || DESTRUCTIVE_INTENTS.secrets.some(re => re.test(arg)))) return "secrets";
  if (/^(sh|bash|zsh|cmd|powershell|pwsh)$/.test(app)) {
    const i = args.findIndex(arg => /^(-[a-z]*c|\/c|-command)$/i.test(arg));
    if (i >= 0) return selectCategory(shellCommands(args.slice(i + 1).join(" ")).map(c => commandCategory(c, depth + 1)));
  }
  if (/^(node|bun|python|python3)$/.test(app)) {
    const i = args.findIndex(arg => /^(-c|-e|--eval)$/.test(arg));
    if (i >= 0) {
      const result = evalCommands(args[i + 1] ?? "", app.startsWith("python") ? "py" : "js", shellCommands);
      return selectCategory([...result.commands.map(c => commandCategory(c, depth + 1)), result.unresolved ? "shell_destructive_os" : undefined]);
    }
  }
  if (/^(ssh|scp|sftp)$/.test(app)) return "remote_ssh";
  if (app === "format" && args.some(arg => /^[a-z]:$/i.test(arg)) ||
      DESTRUCTIVE_INTENTS.shell_destructive_os.some(re => re.test(command)) && /^(rm|format|shutdown|reboot|sudo|dd|kill|taskkill)$/.test(app)) return "shell_destructive_os";
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
      action === "commit" && rest.includes("--amend") ||
      action === "reset" && rest.includes("--hard") || action === "clean" ||
      action === "branch" && (rest.some(a => /^-[^-]*D/.test(a)) ||
        rest.some(a => /^-[^-]*d/.test(a) || a === "--delete") && rest.some(a => /^-[^-]*f/.test(a))) ||
      action === "checkout" && rest.includes("--") && rest.includes(".") ||
      action === "restore" && rest.includes(".")) return "destructive_git";
  if (action !== "push") return;
  if (rest.some(a => /^-[^-]*f/.test(a) || /^\+/.test(a) || /^(--delete|--mirror|--all)$/.test(a))) return "destructive_git";
  const refs = rest.filter(a => !a.startsWith("-"));
  // No explicit destination means configured refspecs; require exact approval rather than guess.
  if (refs.length < 2) return "destructive_git";
  if (refs.slice(1).some(ref => PROTECTED.test(ref.split(":").pop()!.replace(/^refs\/heads\//, "")) || ref.startsWith(":"))) return "destructive_git";
}
function selectCategory(categories: (string | undefined)[]): string | undefined {
  return categories.includes("production_exclusion") ? "production_exclusion" : categories.find(Boolean);
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
  public evaluateToolCall(toolName: string, input: Record<string, unknown>, _isTelegramOverride?: boolean, context: ApprovalContext = LOCAL_CONTEXT): ToolGuardEvaluation {
    // Origin intentionally unused: every call follows exactly the same permission path.
    let category: string | undefined;
    let commands: string[][] = [], unresolved = false;
    if (toolName === "bash") {
      commands = shellCommands(String(input.command ?? ""));
      category = selectCategory(commands.map(c => commandCategory(c)));
    } else if (toolName === "launch" && input.op === "start") {
      commands = [[String(input.application ?? ""), ...(Array.isArray(input.args) ? input.args.map(String) : [])]];
      category = commandCategory(commands[0]);
    }
    else if (toolName === "launch" && input.op === "restart") category = "shell_destructive_os";
    else if (toolName === "eval") {
      const result = evalCommands(String(input.code ?? ""), String(input.language ?? "js"), shellCommands);
      commands = result.commands;
      unresolved = result.unresolved;
      category = selectCategory([...result.commands.map(c => commandCategory(c)), result.unresolved ? "shell_destructive_os" : undefined]);
    } else if (toolName === "ssh") {
      // An explicit remote-process tool is execution, not a prose mention.
      category = PRODUCTION.test(`${input.host ?? ""} ${input.hostname ?? ""} ${input.command ?? ""}`) ? "production_exclusion" : "remote_ssh";
    }
    if (category !== "production_exclusion" && protectedPath(input)) category = "secrets";
    if (!category && toolName in READ_ONLY_TOOLS) return { allowed: true };
    if (!category) return { allowed: true };
    if (category === "production_exclusion") return { allowed: false, category, reason: "Production is excluded for every transport; an approval cannot override this boundary." };
    // Bind approval to the full exact input, including cwd/env, not just matched words.
    const content = JSON.stringify({ toolName, input, cwd: String(input.cwd ?? context.cwd) });
    try {
      const record = evaluateApproval(this.stateDir, computeApprovalHash(category, content), describeApproval(toolName, input, category, context, commands, unresolved));
      if (record.state === "consumed") return { allowed: true };
      if (record.state === "denied") return { allowed: false, category, reason: "The operator explicitly denied this exact operation. Do not retry or work around it; continue independent work." };
      return { allowed: false, category, approvalHash: record.token, approval: record,
        reason: `Operation '${category}' requires exact operator approval. Await Approve or Deny in this session's Telegram bot; denial is explicit and must not be worked around. Typed fallback: /approve ${record.token}. Expires ${record.expiresAt}. After approval retry the identical call once. Local operator equivalent: bun "${path.join(import.meta.dir, "guard.ts")}" approve "${this.stateDir}" ${record.token} ${JSON.stringify(context.sessionId)}` };
    } catch {
      return { allowed: false, category, reason: "Cannot persist the approval request and audit. No operation executed; restore approval-store write access before retrying." };
    }
  }
}
if (import.meta.main) {
  try {
    const [action, stateDir, token, sessionId = "local"] = process.argv.slice(2);
    if (action !== "approve" || !stateDir || !token) throw new Error("Usage: bun guard.ts approve <stateDir> <full-token> <sessionId>");
    const record = approveOperation(stateDir, token, { sessionId, userId: "local-operator", chatId: "local" });
    console.log(JSON.stringify({ approved: true, expiresAt: record.expiresAt, singleUse: true }));
  } catch (error) { console.error(error instanceof Error ? error.message : String(error)); process.exitCode = 1; }
}
