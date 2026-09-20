/** Channel-neutral execution guard. Transport origin is observability, never authority. */
import * as fs from "node:fs";
import { createHash } from "node:crypto";
import * as path from "node:path";
import { evalCommands, decodeBase64 } from "./guard-eval";
import { describeApproval, evaluateApproval, decideApproval, type ApprovalContext, type ApprovalActor, type ApprovalRecord } from "./approvals";

export interface ToolGuardEvaluation {
  allowed: boolean;
  reason?: string;
  category?: string;
  approvalHash?: string;
  approval?: ApprovalRecord;
}

export interface GuardConfig {
  mode: "minimal" | "off";
  allow: string[];
  deny: string[];
}

const DEFAULT_GUARD_CONFIG: GuardConfig = {
  mode: "minimal",
  allow: [],
  deny: [],
};

const loggedConfigErrors = new Set<string>();

export function loadGuardConfig(stateDir: string): GuardConfig {
  const configFile = path.join(stateDir, "guard.json");
  try {
    if (!fs.existsSync(configFile)) {
      return { ...DEFAULT_GUARD_CONFIG };
    }
    const raw = fs.readFileSync(configFile, "utf8");
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") {
      if (!loggedConfigErrors.has(configFile)) {
        loggedConfigErrors.add(configFile);
        console.warn(`[guard] Invalid guard config in ${configFile}, using default minimal`);
      }
      return { ...DEFAULT_GUARD_CONFIG };
    }
    const mode = parsed.mode === "off" ? "off" : "minimal";
    const allow = Array.isArray(parsed.allow) ? parsed.allow.filter((x: unknown) => typeof x === "string") : [];
    const deny = Array.isArray(parsed.deny) ? parsed.deny.filter((x: unknown) => typeof x === "string") : [];
    return { mode, allow, deny };
  } catch (err) {
    if (!loggedConfigErrors.has(configFile)) {
      loggedConfigErrors.add(configFile);
      console.warn(`[guard] Failed to load guard config from ${configFile}, using default minimal:`, err instanceof Error ? err.message : String(err));
    }
    return { ...DEFAULT_GUARD_CONFIG };
  }
}
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
  const heredocs: { delimiter: string; quoted: boolean; stripTabs: boolean; targetApp?: string }[] = [];
  let word = "", quote = "", started = false;
  const flushWord = () => { if (started) words.push(word); word = ""; started = false; };
  const flush = () => { flushWord(); if (words.length) commands.push(words.splice(0)); };

  // Pipeline check for base64 decode piped into an interpreter
  const b64Pipe = /(?:echo|printf)\s+(?:-n\s+)?['"]?([A-Za-z0-9+/=]{8,})['"]?\s*\|\s*(?:base64\s+(?:-d|--decode)|openssl\s+base64\s+-d)\s*\|\s*(sh|bash|zsh|powershell|pwsh|python3?|node|bun)\b/i.exec(command);
  if (b64Pipe) {
    const decoded = decodeBase64(b64Pipe[1]);
    if (decoded) {
      const interp = b64Pipe[2].toLowerCase();
      if (/^python/.test(interp)) commands.push(...evalCommands(decoded, "py", shellCommands).commands);
      else if (/^(node|bun)/.test(interp)) commands.push(...evalCommands(decoded, "js", shellCommands).commands);
      else commands.push(...shellCommands(decoded));
    }
  }

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
      const sub = command.slice(start, end);
      commands.push(...shellCommands(sub));
      const b64Sub = /(?:echo|printf)\s+(?:-n\s+)?['"]?([A-Za-z0-9+/=]{8,})['"]?\s*\|\s*(?:base64\s+(?:-d|--decode)|openssl\s+base64\s+-d)\b/i.exec(sub);
      if (b64Sub) {
        const decoded = decodeBase64(b64Sub[1]);
        if (decoded) commands.push(...shellCommands(decoded));
      }
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
        const targetApp = words[0] ? executable(words[0]) : undefined;
        heredocs.push({ delimiter: match[2] ?? match[3] ?? match[4], quoted: Boolean(match[2] || match[3]), stripTabs: match[1] === "-", targetApp });
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
        if (document.targetApp && /^(sh|bash|zsh|powershell|pwsh)$/.test(document.targetApp)) {
          commands.push(...shellCommands(body));
        } else if (document.targetApp && /^python/.test(document.targetApp)) {
          commands.push(...evalCommands(body, "py", shellCommands).commands);
        } else if (document.targetApp && /^(node|bun)/.test(document.targetApp)) {
          commands.push(...evalCommands(body, "js", shellCommands).commands);
        } else if (!document.quoted) {
          commands.push(...shellCommands(`echo "${body.replace(/"/g, '\\"')}"`));
        }
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
export function isDestructiveRmTarget(operand: string, cwd: string): boolean {
  const clean = operand.trim().replace(/^["']|["']$/g, "");
  if (!clean) return false;
  if (clean === "/" || clean === "\\" || /^(~|\$HOME|\$\{HOME\})(?:[\\/].*)?$/.test(clean)) {
    return true;
  }
  if (/^[A-Za-z]:[\\/]?$/.test(clean)) {
    return true;
  }
  if (clean === ".." || clean.startsWith("../") || clean.startsWith("..\\")) {
    return true;
  }
  const normCwd = path.resolve(cwd).replace(/\\/g, "/").replace(/\/+$/, "");
  const isWindows = process.platform === "win32" || /^[A-Za-z]:/i.test(cwd) || /^[A-Za-z]:/i.test(clean);
  const compareCwd = isWindows ? normCwd.toLowerCase() : normCwd;

  const isAbs = path.isAbsolute(clean) || /^[A-Za-z]:/i.test(clean) || clean.startsWith("/") || clean.startsWith("\\");
  if (isAbs) {
    const normTarget = path.resolve(clean).replace(/\\/g, "/").replace(/\/+$/, "");
    const compareTarget = isWindows ? normTarget.toLowerCase() : normTarget;
    if (compareTarget !== compareCwd && !compareTarget.startsWith(compareCwd + "/")) {
      return true;
    }
  } else {
    const normTarget = path.resolve(cwd, clean).replace(/\\/g, "/").replace(/\/+$/, "");
    const compareTarget = isWindows ? normTarget.toLowerCase() : normTarget;
    if (compareTarget !== compareCwd && !compareTarget.startsWith(compareCwd + "/")) {
      return true;
    }
  }
  return false;
}

export function commandCategory(words: string[], cwd = process.cwd(), depth = 0): string | undefined {
  if (depth > 8) return "shell_destructive_os";
  while (words.length && /^[A-Za-z_]\w*=/.test(words[0])) words = words.slice(1);
  while (words[0] === "&" || words[0] === "{" || words[0] === "}") words = words.slice(1);
  if (!words.length) return;
  if (words[words.length - 1] === "}") words = words.slice(0, -1);
  if (!words.length) return;
  const app = executable(words[0]), args = words.slice(1), command = [app, ...args].join(" ");
  // The shell builtin reparses its argument string; argv wrappers do not.
  if (app === "eval" || app === "invoke-expression" || app === "iex") {
    const script = (args[0] === "--" ? args.slice(1) : args).join(" ").replace(/^\{|\}$/g, "");
    return selectCategory(shellCommands(script).map(c => commandCategory(c, cwd, depth + 1)));
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
    return commandCategory(args.slice(offset), cwd, depth + 1);
  }
  // A real process may read keys regardless of the transport. Never inspect source-file contents.
  if (PRODUCTION.test(command)) return "production_exclusion";
  if (args.some(arg => SECRET_PATH.test(arg))) return "secrets";
  if (/^(sh|bash|zsh|cmd|powershell|pwsh)$/.test(app)) {
    const encIdx = args.findIndex(arg => /^(-[a-z]*enc[a-z]*|-[a-z]*e)$/i.test(arg));
    if (encIdx >= 0 && args[encIdx + 1]) {
      const decoded = decodeBase64(args[encIdx + 1]);
      if (decoded) return selectCategory(shellCommands(decoded).map(c => commandCategory(c, cwd, depth + 1)));
    }
    const i = args.findIndex(arg => /^(-[a-z]*c|\/c|-command)$/i.test(arg));
    if (i >= 0) {
      const innerCmd = args.slice(i + 1).join(" ").replace(/^(&\s*)?\{|\}$/g, "").trim();
      return selectCategory(shellCommands(innerCmd).map(c => commandCategory(c, cwd, depth + 1)));
    }
  }
  if (/^(node|bun|python|python3)$/.test(app)) {
    const i = args.findIndex(arg => /^(-c|-e|--eval)$/.test(arg));
    if (i >= 0) {
      const result = evalCommands(args[i + 1] ?? "", app.startsWith("python") ? "py" : "js", shellCommands);
      return selectCategory(result.commands.map(c => commandCategory(c, cwd, depth + 1)));
    }
  }
  if (app === "format" && args.some(arg => /^[a-z]:$/i.test(arg))) return "shell_destructive_os";
  if (app === "dd" && args.some(arg => /^if=/i.test(arg))) return "shell_destructive_os";
  if (app === "shutdown" || app === "reboot") return "shell_destructive_os";
  if (app === "rm" || app === "rmdir" || app === "remove-item") {
    const isRecursive = app === "rmdir"
      ? args.some(a => /^[\/-]s$/i.test(a))
      : app === "remove-item"
        ? args.some(a => /^-[a-z]*r/i.test(a) || /^-recurse$/i.test(a))
        : args.some(a => /^-[a-z]*r/i.test(a) || a === "--recursive");
    if (isRecursive) {
      let inDoubleDash = false;
      const operands: string[] = [];
      for (const arg of args) {
        if (inDoubleDash) operands.push(arg);
        else if (arg === "--") inDoubleDash = true;
        else if (app === "rmdir") { if (!/^[\/-][sq]/i.test(arg)) operands.push(arg); }
        else if (app === "remove-item") { if (!arg.startsWith("-")) operands.push(arg); }
        else { if (!arg.startsWith("-")) operands.push(arg); }
      }
      if (operands.some(op => isDestructiveRmTarget(op, cwd))) return "shell_destructive_os";
    }
  }
  if (app === "psql" || app === "pg_restore") return "shared_db_ddl_dml";
  if (app === "alembic" && args.some(a => /^(upgrade|downgrade|stamp)$/.test(a))) return "shared_db_ddl_dml";
  if (app === "supabase" && args[0] === "db" && /^(push|reset|remote)$/.test(args[1] ?? "")) return "shared_db_ddl_dml";
  if (app === "dokploy" || (args.includes("dokploy") && !/^(echo|printf|cat)$/.test(app))) return "deployments";
  if ((app === "fly" || app === "flyctl") && args.includes("deploy") && !/^(echo|printf|cat)$/.test(app)) return "deployments";
  if (app === "wrangler" && /^(deploy|publish)$/.test(args[0] ?? "")) return "deployments";
  if (app === "deploy" && args.some(a => /^(prod|production)$/i.test(a))) return "deployments";
  const deployIdx = args.findIndex(a => a.toLowerCase() === "deploy");
  if (deployIdx >= 0 && /^(prod|production)$/i.test(args[deployIdx + 1] ?? "") && !/^(echo|printf|cat)$/.test(app)) return "deployments";
  if (app === "wrangler" && /^(secret|kv|d1)$/.test(args[0] ?? "")) return "cloudflare_stripe_mutations";
  if (app === "stripe" && args.some(a => /^(create|update|cancel|delete)$/.test(a)) && !/^(echo|printf|cat)$/.test(app)) return "cloudflare_stripe_mutations";
  if (app === "topup_main_reset" || (args.includes("topup_main_reset") && !/^(echo|printf|cat)$/.test(app))) return "cloudflare_stripe_mutations";
  if (app !== "git") return;
  let i = 0;
  while (i < args.length && args[i].startsWith("-")) {
    if (/^(-C|-c|--git-dir|--work-tree)$/.test(args[i])) i += 2; else i++;
  }
  const action = args[i], rest = args.slice(i + 1);
  if (/^(filter-branch|filter-repo)$/.test(action ?? "")) return "destructive_git";
  if (action === "push") {
    const isMirror = rest.some(a => a === "--mirror");
    if (isMirror) return "destructive_git";
    const isForce = rest.some(a => /^-[^-]*f/.test(a) || /^--force(?:-with-lease|-if-includes)?(?:=.*)?$/.test(a));
    const isDelete = rest.some(a => a === "--delete" || /^-[^-]*d$/.test(a));
    const nonFlags = rest.filter(a => !a.startsWith("-"));
    const refspecs = nonFlags.length > 1 ? nonFlags.slice(1) : nonFlags;
    const isProtected = (ref: string) => {
      const dest = ref.includes(":") ? ref.split(":").pop()! : ref.replace(/^\+/, "");
      return PROTECTED.test(dest.replace(/^refs\/heads\//, ""));
    };
    if (isForce && refspecs.some(isProtected)) return "destructive_git";
    if (isDelete && refspecs.some(isProtected)) return "destructive_git";
    if (refspecs.some(r => r.startsWith("+") && isProtected(r))) return "destructive_git";
    if (refspecs.some(r => r.startsWith(":") && isProtected(r))) return "destructive_git";
  }
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
  private config?: GuardConfig;
  constructor(private stateDir: string) {}
  public getGuardConfig(): GuardConfig {
    if (!this.config) this.config = loadGuardConfig(this.stateDir);
    return this.config;
  }
  public reloadGuardConfig(): GuardConfig {
    this.config = loadGuardConfig(this.stateDir);
    return this.config;
  }
  public startTelegramTurn(turnId?: string): void { this.currentTurnState = "TELEGRAM_ACTIVE"; this.activeTelegramTurnId = turnId || String(Date.now()); }
  public startLocalTurn(): void { this.currentTurnState = "LOCAL_ACTIVE"; this.activeTelegramTurnId = null; }
  public endTurn(): void { this.currentTurnState = "IDLE"; this.activeTelegramTurnId = null; }
  public isTelegramTurnActive(): boolean { return this.currentTurnState === "TELEGRAM_ACTIVE"; }
  public evaluateToolCall(toolName: string, input: Record<string, unknown>, _isTelegramOverride?: boolean, context: ApprovalContext = LOCAL_CONTEXT): ToolGuardEvaluation {
    const cwd = String(input.cwd ?? context.cwd ?? process.cwd());
    let category: string | undefined;
    let commands: string[][] = [], unresolved = false;
    if (toolName === "bash") {
      commands = shellCommands(String(input.command ?? ""));
      category = selectCategory(commands.map(c => commandCategory(c, cwd)));
    } else if (toolName === "launch" && input.op === "start") {
      commands = [[String(input.application ?? ""), ...(Array.isArray(input.args) ? input.args.map(String) : [])]];
      category = commandCategory(commands[0], cwd);
    } else if (toolName === "eval") {
      const result = evalCommands(String(input.code ?? ""), String(input.language ?? "js"), shellCommands);
      commands = result.commands;
      unresolved = result.unresolved;
      category = selectCategory(result.commands.map(c => commandCategory(c, cwd)));
    } else if (toolName === "ssh") {
      category = PRODUCTION.test(`${input.host ?? ""} ${input.hostname ?? ""} ${input.command ?? ""}`) ? "production_exclusion" : undefined;
    }
    if (category !== "production_exclusion" && protectedPath(input)) category = "secrets";
    if (!category) return { allowed: true };
    if (category === "production_exclusion") return { allowed: false, category, reason: "Production is excluded for every transport; an approval cannot override this boundary." };

    const config = this.getGuardConfig();
    const isDenied = config.deny.includes(category);
    const isAllowed = config.allow.includes(category);
    if (!isDenied) {
      if (config.mode === "off") return { allowed: true };
      if (isAllowed) return { allowed: true };
    }

    // Bind approval to the full exact input, including cwd/env, not just matched words.
    const content = JSON.stringify({ toolName, input, cwd });
    try {
      const record = evaluateApproval(this.stateDir, computeApprovalHash(category, content), describeApproval(toolName, input, category, context, commands, unresolved));
      if (record.state === "consumed") return { allowed: true };
      if (record.state === "denied") return { allowed: false, category, reason: "The operator explicitly denied this exact operation. Do not retry or work around it; continue independent work." };
      return { allowed: false, category, approvalHash: record.token, approval: record,
        reason: `Operation '${category}' requires exact operator approval. Await Approve or Deny in this session's Telegram bot; denial is explicit and must not be worked around. Typed fallback: /approve ${record.token}. Expires ${record.expiresAt}. After approval retry the identical call once. Local operator equivalent: bun "${path.join(import.meta.dir, "guard.ts")}" approve "${this.stateDir}" ${record.token} ${JSON.stringify(context.sessionId)}` };
    } catch {
      return { allowed: false, category, reason: "Blocked — approval store is not writable, so this call was not approved. Restore write access before retrying." };
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
