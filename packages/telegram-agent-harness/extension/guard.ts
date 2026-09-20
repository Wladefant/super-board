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

// Every dotted .env variant holds real values; the committed template variants hold placeholders.
const SECRET_PATH = /(?:^|[/\\])\.env(?:\.(?!(?:example|sample|template|dist|defaults|schema)\b)[\w-]+)*$|\b(id_(rsa|dsa|ecdsa|ed25519)|service_role|jwt_secret|agent\.db)\b|\.(pem|p12|pfx|key|keystore|jks|ppk)$|(?:^|[/\\])credentials(\.json)?$|(?:^|[/\\])\.(npmrc|netrc|pgpass|git-credentials|pypirc)$|(?:^|[/\\])\.ssh(?:[/\\]|$)|(?:^|[/\\])\.kube[/\\]config$|(?:^|[/\\])\.docker[/\\]config\.json$|(?:^|[/\\])\.gnupg[/\\]|(?:^|[/\\])secrets?\.(json|ya?ml|toml)$|(?:^|[/\\])terraform\.tfstate$|(?:^|[/\\])proc[/\\][^/\\]+[/\\]environ$/i;
const PROTECTED = /^(main|master|staging|production|prod)$/i;
const PRODUCTION = /(?:\bzaraprptkegxqpvnsubu\b|\bakamai-iad-prod\b)/i;
// The directories whose contents are the machine itself, wherever that machine is.
const SYSTEM_PATH = /^(?:\/(?:etc|bin|sbin|boot|lib|lib64|sys|proc|usr(?:\/(?:bin|sbin|lib|local\/bin))?|var\/(?:spool|lib|www))(?:\/|$)|[A-Za-z]:[\\/](?:windows|program files))/i;
const LOCAL_CONTEXT: ApprovalContext = { sessionId: "local", requester: "Local operator", task: "Local guarded operation", cwd: process.cwd() };
export function computeApprovalHash(category: string, content: string): string {
  return createHash("sha256").update(`${category}:${content.trim()}`).digest("hex");
}
/** Only the authenticated Telegram transport or an explicit local operator command calls this. */
export function approveOperation(stateDir: string, token: string, actor: ApprovalActor = { sessionId: "local", userId: "local-operator", chatId: "local" }): ApprovalRecord {
  return decideApproval(stateDir, token, "approved", actor);
}

/** A word whose runtime value the lexer cannot prove: substitution output, dynamic construction. */
export const DYNAMIC = "\u0000dynamic";
const INTERPRETER = /^(sh|bash|zsh|ksh|dash|ash|cmd|powershell|pwsh|python[\d.]*|py|node|bun|deno|perl|ruby|php|osascript|rscript|r)$/;
const FETCHER = /^(curl|wget|http|httpie|invoke-webrequest|iwr)$/;

function interpretAs(app: string, body: string): string[][] {
  return interpretCode(app, body).commands;
}
/** What an interpreter body finally runs: a call-site lex for a code language, a shell parse for a shell. */
function interpretCode(app: string, body: string): { commands: string[][]; unresolved: boolean } {
  if (/^(python[\d.]*|py)$/.test(app)) return evalCommands(body, "py", shellCommands);
  if (/^(node|bun|deno)$/.test(app)) return evalCommands(body, "js", shellCommands);
  // AppleScript reaches a shell only through `do shell script`.
  if (app === "osascript") {
    const commands: string[][] = [];
    for (const match of body.matchAll(/do\s+shell\s+script\s+(?:"((?:[^"\\]|\\.)*)"|'([^']*)')/gi)) {
      commands.push(...shellCommands((match[1] ?? match[2]).replace(/\\([\s\S])/g, "$1")));
    }
    return { commands, unresolved: false };
  }
  // Perl, Ruby, PHP, R and awk spell a shell escape as a call — `system(...)`, `exec(...)` — or as backticks.
  if (/^(perl|ruby|php|rscript|r|awk|gawk|mawk|nawk)$/.test(app)) {
    const lexed = evalCommands(body, "js", shellCommands), commands = [...lexed.commands];
    if (!/^(awk|gawk|mawk|nawk)$/.test(app)) {
      for (const match of body.matchAll(/`([^`]*)`|\bqx[{([]([^)}\]]*)[)}\]]/g)) commands.push(...shellCommands(match[1] ?? match[2]));
    }
    return { commands, unresolved: lexed.unresolved };
  }
  return { commands: shellCommands(body), unresolved: false };
}
/** `find / … | xargs rm -rf` deletes whatever the upstream stage enumerates: those paths are the operands. */
function pipedOperands(stages: string[][]): string[][] {
  const derived: string[][] = [];
  for (let k = 1; k < stages.length; k++) {
    if (executable(stages[k][0] ?? "") !== "xargs") continue;
    const producer = stages[k - 1], app = executable(producer[0] ?? ""), operands = producer.slice(1);
    const flag = operands.findIndex(arg => arg.startsWith("-"));
    const roots = app === "find"
      ? operands.slice(0, flag < 0 ? operands.length : flag)
      : /^(ls|locate|echo|printf|cat|grep|rg)$/.test(app) ? operands.filter(arg => !arg.startsWith("-")) : [];
    if (roots.length) derived.push([...stages[k], ...roots]);
  }
  return derived;
}
/** A stage that turns an operand back into executable text: `base64 -d`, `openssl enc -d -a`, `certutil -decode`. */
function decodesBase64(words: string[]): boolean {
  const app = executable(words[0] ?? ""), args = words.slice(1);
  if (app === "base64") return args.some(arg => arg === "--decode" || /^-[a-z]*d[a-z]*$/.test(arg));
  if (app === "openssl") return /^(enc|base64)$/.test(args[0] ?? "") && args.includes("-d");
  if (app === "certutil") return args.some(arg => /^[-/]decode$/i.test(arg));
  return false;
}
/** The text an `echo`/`printf` stage writes, or undefined when the operand is not statically known. */
function literalOutput(words: string[]): string | undefined {
  const app = executable(words[0] ?? "");
  if (app !== "echo" && app !== "printf") return undefined;
  let operands = words.slice(1).filter(arg => !/^-[neE]+$/.test(arg));
  if (app === "printf" && operands.length > 1 && /^%s\\?n?$/.test(operands[0])) operands = operands.slice(1);
  const text = operands.join(" ");
  return text.includes(DYNAMIC) ? undefined : text;
}
/** Decode-and-run reconstructed from parsed pipeline stages, so quoted prose can never look like a pipeline. */
function decodePipeline(stages: string[][]): string[][] {
  const derived: string[][] = [], apps = stages.map(stage => executable(stage[0] ?? ""));
  for (let k = 0; k < stages.length; k++) {
    if (!decodesBase64(stages[k])) continue;
    const sink = apps.findIndex((app, downstream) => downstream > k && INTERPRETER.test(app));
    if (sink < 0) continue;
    const payload = k > 0 ? literalOutput(stages[k - 1]) : undefined;
    const decoded = payload === undefined ? "" : decodeBase64(payload);
    derived.push(...(decoded ? interpretAs(apps[sink], decoded) : [[DYNAMIC]]));
  }
  // Fetching a script and piping it into an interpreter runs code this guard never sees.
  if (stages.length > 1 && FETCHER.test(apps[0]) && apps.slice(1).some(app => INTERPRETER.test(app))) derived.push([DYNAMIC]);
  return derived;
}
/** What `$(...)` expands to, when the lexer can prove it: a literal echo, or a literal echo decoded. */
function substitutionOutput(pipelines: string[][][]): string | undefined {
  if (pipelines.length !== 1) return undefined;
  const stages = pipelines[0];
  if (stages.length === 1) return literalOutput(stages[0]);
  if (stages.length !== 2 || !decodesBase64(stages[1])) return undefined;
  const payload = literalOutput(stages[0]);
  return payload === undefined ? undefined : decodeBase64(payload) || undefined;
}

/** Shell words grouped into pipelines, not a prose scan: separators outside quotes introduce invocations. */
function parseScript(source: string): { commands: string[][]; pipelines: string[][][] } {
  // $IFS expands to a separator at runtime, so resolving it keeps `rm${IFS}-rf${IFS}/` a real invocation.
  const command = source.replace(/\$\{IFS\}/g, " ").replace(/\$IFS(?!\w)/g, " ");
  const pipelines: string[][][] = [], derived: string[][] = [], words: string[] = [];
  const heredocs: { delimiter: string; quoted: boolean; stripTabs: boolean; owner: string[] }[] = [];
  let pipeline: string[][] = [], word = "", quote = "", started = false;
  const flushWord = () => { if (started) words.push(word); word = ""; started = false; };
  const flush = () => { flushWord(); if (words.length) pipeline.push(words.splice(0)); };
  const endPipeline = () => { flush(); if (pipeline.length) pipelines.push(pipeline); pipeline = []; };

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
      const inner = parseScript(command.slice(start, end));
      derived.push(...inner.commands);
      // The substitution's output becomes part of the surrounding word; unprovable output stays dynamic.
      word += substitutionOutput(inner.pipelines) ?? DYNAMIC;
      i = end; started = true; continue;
    }
    if (quote) {
      if (c === quote) { quote = ""; continue; }
      if (c === "\\" && quote === '"' && /["\\$`\n]/.test(command[i + 1] ?? "")) { word += command[++i]; continue; }
      word += c; continue;
    }
    // A here-string feeds its operand into this stage's interpreter exactly like a heredoc body.
    if (command.slice(i, i + 3) === "<<<") {
      flushWord();
      let j = i + 3;
      while (/[ \t]/.test(command[j] ?? "")) j++;
      let operand = "", inner = "";
      for (; j < command.length; j++) {
        const ch = command[j];
        if (inner) { if (ch === inner) inner = ""; else operand += ch; continue; }
        if (ch === "'" || ch === '"') { inner = ch; continue; }
        if (/[\s;&|\n]/.test(ch)) break;
        operand += ch;
      }
      const app = executable(words[0] ?? "");
      if (INTERPRETER.test(app)) derived.push(...interpretAs(app, operand));
      i = j - 1; continue;
    }
    if (command.slice(i, i + 2) === "<<") {
      const match = /^<<(-?)\s*(?:'([^']+)'|"([^"]+)"|([^\s;&|]+))/.exec(command.slice(i));
      if (match) {
        flushWord();
        heredocs.push({ delimiter: match[2] ?? match[3] ?? match[4], quoted: Boolean(match[2] || match[3]), stripTabs: match[1] === "-", owner: words.slice() });
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
        // stdin is only the program when no argv mode supplies one, and the consumer may be a later stage.
        if (pipeline.some(stage => INTERPRETER.test(executable(stage[0] ?? "")) && stage.slice(1).some((arg, k) => /^(-[a-z]*c[a-z]*|--eval|--command)$/.test(arg) && stage[k + 2] !== undefined))) continue;
        const consumer = pipeline.map(stage => executable(stage[0] ?? "")).find(app => INTERPRETER.test(app)) ?? executable(document.owner[0] ?? "");
        if (INTERPRETER.test(consumer)) derived.push(...interpretAs(consumer, body));
        else if (!document.quoted) derived.push(...parseScript(`echo "${body.replace(/"/g, '\\"')}"`).commands);
      }
      continue;
    }
    if (c === "'" || c === '"') { quote = c; started = true; }
    else if (c === "#" && !started) { while (i < command.length && command[i] !== "\n") i++; endPipeline(); }
    else if (c === "|" && command[i + 1] === "|") { endPipeline(); i++; }
    else if (c === "|") flush();
    else if (c === "&" && command[i + 1] === "&") { endPipeline(); i++; }
    else if (/[;&\n()]/.test(c)) endPipeline();
    else if (/\s/.test(c)) flushWord();
    // A word already carrying a drive prefix is a Windows path: its backslashes are separators, not escapes.
    else if (c === "\\" && !/^[A-Za-z]:/.test(word) && /[\s'";&|$`]/.test(command[i + 1] ?? "")) { started = true; word += command[++i]; }
    else { word += c; started = true; }
  }
  endPipeline();

  // Leading `NAME=value` assignments are the only variable binding the lexer can prove.
  const values = new Map<string, string>();
  for (const stages of pipelines) for (const stage of stages) {
    let k = 0;
    for (; k < stage.length; k++) {
      const assignment = /^([A-Za-z_]\w*)=([\s\S]*)$/.exec(stage[k]);
      if (!assignment) break;
      values.set(assignment[1], assignment[2]);
    }
    // An unquoted expansion word-splits, so a value carrying whitespace becomes several argv words.
    const expanded = stage.slice(0, k);
    for (; k < stage.length; k++) {
      const substituted = stage[k].replace(/\$\{(\w+)\}|\$(\w+)/g, (raw, braced, bare) => values.get(braced ?? bare) ?? raw);
      if (substituted === stage[k]) expanded.push(stage[k]);
      else expanded.push(...substituted.split(/\s+/).filter(Boolean));
    }
    stage.splice(0, stage.length, ...expanded);
  }

  const commands: string[][] = [];
  for (const stages of pipelines) commands.push(...decodePipeline(stages), ...pipedOperands(stages), ...stages);
  commands.push(...derived);
  return { commands, pipelines };
}
export function shellCommands(command: string): string[][] {
  return parseScript(command).commands;
}
function executable(value: string): string {
  // Outside quotes a backslash escapes the next character; a drive-qualified Windows path keeps its separators.
  const unescaped = /^[A-Za-z]:[\\/]/.test(value) ? value : value.replace(/\\([\s\S])/g, "$1");
  return unescaped.replace(/\\/g, "/").split("/").pop()!.replace(/\.(exe|cmd|bat)$/i, "").toLowerCase();
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
/** The path inside a `[user@]host:path` transfer spec; a drive letter and a URL scheme are local syntax. */
function remotePath(target: string): string | undefined {
  if (/^[a-z][a-z\d+.-]*:\/\//i.test(target)) return undefined;
  const spec = /^(?:[\w.-]+@)?[\w.-]{2,}:([\s\S]*)$/.exec(target);
  return spec ? spec[1] : undefined;
}

/** PowerShell accepts any unambiguous leading prefix of a parameter name, so `-e` is `-EncodedCommand`. */
function flagPrefixOf(flag: string, parameter: string): boolean {
  const given = flag.replace(/^[-/]+/, "").toLowerCase();
  return given.length > 0 && parameter.startsWith(given);
}
export const DYNAMIC_CATEGORY = "dynamic_code";

export function commandCategory(words: string[], cwd = process.cwd(), depth = 0): string | undefined {
  if (depth > 8) return "shell_destructive_os";
  while (words.length && /^[A-Za-z_]\w*=/.test(words[0])) words = words.slice(1);
  while (words[0] === "&" || words[0] === "{" || words[0] === "}") words = words.slice(1);
  if (!words.length) return;
  if (words[words.length - 1] === "}") words = words.slice(0, -1);
  if (!words.length) return;
  // Substitution output, variable indirection, brace expansion and globbing hide the real program name.
  // An unresolvable command name is dynamically constructed code, never a silent allow.
  const globbed = /[?*]|\[[^\]]*\]/.test(words[0]) && words[0] !== "[" && words[0] !== "[[";
  if (words[0].includes(DYNAMIC) || /[$`]/.test(words[0]) || /\{[^}]*,[^}]*\}/.test(words[0]) || globbed) return DYNAMIC_CATEGORY;
  const app = executable(words[0]), args = words.slice(1), command = [app, ...args].join(" ");
  const inspect = (script: string): string | undefined => script.includes(DYNAMIC)
    ? DYNAMIC_CATEGORY
    : selectCategory(shellCommands(script).map(inner => commandCategory(inner, cwd, depth + 1)));
  // The shell builtin reparses its argument string; argv wrappers do not.
  if (app === "eval" || app === "invoke-expression" || app === "iex") {
    const script = (args[0] === "--" ? args.slice(1) : args).join(" ").replace(/^\{|\}$/g, "").trim();
    return script ? inspect(script) : DYNAMIC_CATEGORY;
  }
  if (/^(env|command|exec|call|if|then|do|while|!|time|nohup|xargs|sudo|doas|stdbuf|setsid|nice|ionice|npx|bunx|uvx|taskset|chrt|setarch|arch|eatmydata|proxychains|proxychains4|torify|torsocks|catchsegv|setpriv|firejail|bwrap|retry)$/.test(app)) {
    let offset = 0;
    while (args[offset]?.startsWith("-")) {
      if (args[offset] === "--") { offset++; break; }
      const takesValue = app === "xargs"
        ? /^(-[aEdInPLs]|--arg-file|--eof|--delimiter|--replace|--max-args|--max-procs|--max-lines|--max-chars|--process-slot-var)$/.test(args[offset])
        : app === "time"
          ? /^(-[fo]|--format|--output)$/.test(args[offset])
          : /^(sudo|doas)$/.test(app)
            ? /^-[ugphCrtU]$/.test(args[offset])
            : /^(npx|bunx|uvx)$/.test(app)
              ? /^(-p|--package|-c|--call)$/.test(args[offset])
              : /^(nice|ionice|stdbuf)$/.test(app)
                ? /^-[ncioep]$/.test(args[offset])
                : /^(taskset|chrt)$/.test(app)
                  ? /^(-c|--cpu-list|-p|--pid)$/.test(args[offset])
                  : /^(proxychains4?|torify|torsocks|retry)$/.test(app)
                    ? /^(-f|-t|--times|-d|--delay)$/.test(args[offset])
                    : /^(--unset|-u|-a)$/.test(args[offset]);
      offset += takesValue ? 2 : 1;
    }
    // `taskset 0x1 cmd`, `chrt 99 cmd` and `setarch <arch> cmd` spend an operand of no fixed shape before
    // the program, so both readings are classified and the stronger one decides.
    const readings = [commandCategory(args.slice(offset), cwd, depth + 1)];
    if (/^(taskset|chrt|setarch|arch)$/.test(app)) readings.push(commandCategory(args.slice(offset + 1), cwd, depth + 1));
    return selectCategory(readings);
  }
  if (app === "timeout") {
    let offset = 0;
    while (args[offset]?.startsWith("-")) offset += /^(-s|--signal|-k|--kill-after)$/.test(args[offset]) ? 2 : 1;
    if (/^[\d.]+[smhd]?$/.test(args[offset] ?? "")) offset++;
    return commandCategory(args.slice(offset), cwd, depth + 1);
  }
  // Windows launches a program through `start` and `Start-Process` rather than by naming it first.
  if (app === "start") {
    let offset = 0;
    while (args[offset] !== undefined && args[offset].startsWith("/")) offset += /^\/(d|node|affinity|machine)$/i.test(args[offset]) ? 2 : 1;
    const rest = args.slice(offset);
    // `start "title" program …` spends its first operand on a window title, so both readings are classified.
    return selectCategory([commandCategory(rest, cwd, depth + 1), commandCategory(rest.slice(1), cwd, depth + 1)]);
  }
  if (/^(start-process|saps)$/.test(app)) {
    const named = args.findIndex(arg => /^-FilePath$/i.test(arg));
    const program = named >= 0 ? args[named + 1] : args.find((arg, k) => !arg.startsWith("-") && !args[k - 1]?.startsWith("-"));
    const listed = args.findIndex(arg => /^-(ArgumentList|Args)$/i.test(arg));
    // -ArgumentList takes a comma-separated array, which the lexer sees as one word.
    const list = listed >= 0 ? (args[listed + 1] ?? "").split(",").map(item => item.trim()).filter(Boolean) : [];
    return program === undefined ? DYNAMIC_CATEGORY : commandCategory([program, ...list], cwd, depth + 1);
  }
  // `watch` and `script -c` hand their argument back to a shell, so the payload is reparsed, not argv.
  if (app === "watch") {
    let offset = 0;
    while (args[offset]?.startsWith("-")) offset += /^(-n|--interval|-x|--exec|-d|--differences)$/.test(args[offset]) ? 2 : 1;
    return args[offset] === undefined ? DYNAMIC_CATEGORY : inspect(args.slice(offset).join(" "));
  }
  if (app === "script") {
    const inline = args.find(arg => arg.startsWith("--command="));
    if (inline) return inspect(inline.slice("--command=".length));
    const flagged = args.findIndex(arg => /^-[a-z]*c$/.test(arg));
    if (flagged >= 0) return args[flagged + 1] === undefined ? DYNAMIC_CATEGORY : inspect(args[flagged + 1]);
  }
  // `su`/`runuser` either reparse a -c payload or exec the argv after `--`.
  if (app === "su" || app === "runuser") {
    const inline = args.find(arg => arg.startsWith("--command="));
    if (inline) return inspect(inline.slice("--command=".length));
    const flagged = args.findIndex(arg => /^(-c|--command)$/.test(arg));
    if (flagged >= 0) return args[flagged + 1] === undefined ? DYNAMIC_CATEGORY : inspect(args[flagged + 1]);
    const separator = args.indexOf("--");
    if (separator >= 0) return commandCategory(args.slice(separator + 1), cwd, depth + 1);
  }
  // Wrappers that execute the rest of their argv once their own flags and operands are consumed.
  if (/^(flock|chroot|unshare|strace|ltrace|busybox|parallel)$/.test(app)) {
    let offset = 0;
    while (args[offset]?.startsWith("-")) {
      const takesValue = /^(strace|ltrace)$/.test(app)
        ? /^(-o|-e|-p|-s|-E|--output|--trace)$/.test(args[offset])
        : app === "flock"
          ? /^(-w|--wait|--timeout|-E|--conflict-exit-code)$/.test(args[offset])
          : app === "parallel"
            ? /^(-j|--jobs|-N|-d|--delimiter|--results)$/.test(args[offset])
            : /^(--map-user|--map-group|--setuid|--setgid)$/.test(args[offset]);
      offset += takesValue ? 2 : 1;
    }
    // `flock <lockfile> cmd` and `chroot <newroot> cmd` spend one operand before the program.
    if ((app === "flock" || app === "chroot") && args[offset] !== undefined) offset++;
    const inner = args.slice(offset), list = inner.indexOf(":::");
    // `parallel cmd ::: a b` runs the command once per trailing item, so those items are its operands.
    return commandCategory(list < 0 ? inner : [...inner.slice(0, list), ...inner.slice(list + 1)], cwd, depth + 1);
  }
  if (/^(pnpm|yarn)$/.test(app) && args[0] === "dlx") return commandCategory(args.slice(1), cwd, depth + 1);
  if (/^(pipx|poetry|uv|rye)$/.test(app) && /^(run|exec)$/.test(args[0] ?? "")) return commandCategory(args.slice(1), cwd, depth + 1);
  // The production ref must stay greppable, but a file reader can still carry real key material out.
  const readOnlyLocal = (/^(grep|rg|ag|ack)$/.test(app)
    || (app === "git" && /^(log|grep|show|status|diff|blame|ls-files)$/.test(args.find(arg => !arg.startsWith("-")) ?? "")))
    && !args.some(arg => /^[a-z][a-z\d+.-]*:\/\//i.test(arg) || /@[\w.-]+:/.test(arg));
  // A real process may read keys regardless of the transport. Never inspect source-file contents.
  if (!readOnlyLocal && PRODUCTION.test(command)) return "production_exclusion";
  if (args.some(arg => SECRET_PATH.test(arg))) return "secrets";
  // A secret manager hands out the same material the file holds, so reading it out is the same disclosure.
  if (app === "gh" && args[0] === "auth" && args[1] === "token") return "secrets";
  if (/^(vault|op|doppler|infisical)$/.test(app) && /^(read|get|kv|item|secrets|export)$/.test(args[0] ?? "")) return "secrets";
  if (app === "aws" && (args[0] === "secretsmanager" && /^(get-secret-value|list-secrets)$/.test(args[1] ?? "")
    || args[0] === "ssm" && args.includes("--with-decryption"))) return "secrets";
  if (app === "kubectl" && /^(get|describe)$/.test(args[0] ?? "") && /^secrets?$/.test(args[1] ?? "")) return "secrets";
  if (/^(fly|flyctl|wrangler|heroku|railway|vercel)$/.test(app)
    && /^(secrets?|config)$/.test(args[0] ?? "") && /^(list|get|reveal|pull)$/.test(args[1] ?? "")) return "secrets";
  if (/^(sh|bash|zsh|ksh|dash|ash|cmd|powershell|pwsh)$/.test(app)) {
    const found: (string | undefined)[] = [];
    if (/^(powershell|pwsh)$/.test(app)) {
      const encoded = args.findIndex(arg => flagPrefixOf(arg, "encodedcommand"));
      if (encoded >= 0 && args[encoded + 1]) {
        const decoded = decodeBase64(args[encoded + 1]);
        found.push(decoded ? inspect(decoded) : DYNAMIC_CATEGORY);
      }
    }
    // A POSIX shell takes -c anywhere in a combined cluster; only PowerShell abbreviates -Command.
    const script = args.findIndex(arg => /^(powershell|pwsh)$/.test(app)
      ? flagPrefixOf(arg, "command")
      : app === "cmd" ? /^[-/][ck]$/i.test(arg) : /^-[a-z]*c[a-z]*$/.test(arg));
    if (script >= 0) found.push(inspect(args.slice(script + 1).join(" ").replace(/^(&\s*)?\{|\}$/g, "").trim()) ?? (args[script + 1] === undefined ? DYNAMIC_CATEGORY : undefined));
    if (found.length) return selectCategory(found);
  }
  // Each interpreter spells "run this string" its own way: -c, -e/-E, -p/--print, -r, deno's `eval`
  // subcommand, and awk's program operand. Missing the spelling is missing the execution.
  if (/^(python[\d.]*|py|node|bun|deno|perl|ruby|php|osascript|rscript|r|awk|gawk|mawk|nawk)$/.test(app)) {
    const inlineFlag = /^(python[\d.]*|py)$/.test(app) ? /^(-c|--command)$/
      : app === "php" ? /^(-r|--run)$/
        : /^(node|bun)$/.test(app) ? /^(-e|-p|--eval|--print)$/
          : /^(-e|-E|--eval|--exec)$/;
    const idx = args.findIndex(arg => inlineFlag.test(arg) || inlineFlag.test(arg.replace(/=[\s\S]*$/, "")));
    let payload: string | undefined, inlined = idx >= 0;
    if (idx >= 0) {
      const assigned = /^[^=]+=([\s\S]*)$/.exec(args[idx]);
      payload = assigned && !inlineFlag.test(args[idx]) ? assigned[1] : args[idx + 1];
    } else if (app === "deno" && args[0] === "eval") {
      inlined = true;
      payload = args.slice(1).find(arg => !arg.startsWith("-"));
    } else if (/^(awk|gawk|mawk|nawk)$/.test(app) && !args.some(arg => /^(-f|--file)/.test(arg))) {
      // awk's first operand is its program; everything after it is data.
      payload = args.find((arg, k) => !arg.startsWith("-") && !/^(-v|--assign|-F|--field-separator)$/.test(args[k - 1] ?? ""));
      inlined = payload !== undefined;
    }
    if (inlined) {
      if (payload === undefined) return DYNAMIC_CATEGORY;
      const result = interpretCode(app, payload);
      const category = selectCategory([...result.commands.map(inner => commandCategory(inner, cwd, depth + 1)), result.unresolved ? DYNAMIC_CATEGORY : undefined]);
      if (category) return category;
    }
  }
  if (app === "ssh" || app === "plink") {
    let offset = 0;
    while (args[offset]?.startsWith("-")) offset += /^-[bcDEeFIiJLlmOoPpQRSWw]$/.test(args[offset]) ? 2 : 1;
    const remote = args.slice(offset + 1).join(" ");
    if (remote) return inspect(remote);
  }
  if (app === "format" && args.some(arg => /^[a-z]:$/i.test(arg))) return "shell_destructive_os";
  if (/^mkfs(\.\w+)?$/.test(app)) return "shell_destructive_os";
  // `dd` destroys only what it writes; reading /dev/urandom into an in-tree file is ordinary.
  if (app === "dd") {
    const sink = args.find(arg => /^of=/i.test(arg));
    if (sink && (/^of=[\\/]dev[\\/]/i.test(sink) || isDestructiveRmTarget(sink.slice(3), cwd))) return "shell_destructive_os";
  }
  if (app === "shutdown" || app === "reboot") return "shell_destructive_os";
  // Deleting out-of-tree also without -r, and through the cmd and PowerShell spellings of rm.
  if (/^(rm|unlink|shred|rmdir|rd|del|erase|remove-item|ri)$/.test(app)) {
    const cmdStyle = /^(rd|del|erase)$/.test(app);
    const operands: string[] = [];
    let literal = false;
    for (const arg of args) {
      // PowerShell binds a parameter with a colon as readily as with a space: `-Path:C:\`.
      const bound = literal ? null : /^-[A-Za-z]+:(.+)$/.exec(arg);
      if (literal || bound === null && !(cmdStyle ? /^[-/]\w/.test(arg) : arg.startsWith("-"))) operands.push(arg);
      else if (bound) operands.push(bound[1]);
      else if (arg === "--") literal = true;
    }
    if (operands.some(operand => isDestructiveRmTarget(operand, cwd))) return "shell_destructive_os";
  }
  if (app === "truncate" && args.some(arg => /^(-s|--size)/.test(arg))
    && args.filter(arg => !arg.startsWith("-")).some(operand => isDestructiveRmTarget(operand, cwd))) return "shell_destructive_os";
  // Overwriting a file destroys it as surely as rm does, whichever binary performs the write.
  const operands = args.filter(arg => !arg.startsWith("-")), written: string[] = [];
  if (/^(sort|install|shred|tee)$/.test(app)) {
    const flagged = args.findIndex(arg => /^(-o|--output)$/.test(arg));
    if (flagged >= 0 && args[flagged + 1]) written.push(args[flagged + 1]);
    const inline = args.find(arg => /^(-o|--output=)/.test(arg) && arg.length > 2);
    if (inline) written.push(inline.replace(/^(-o|--output=)/, ""));
  }
  if (app === "tee") written.push(...operands);
  // The final operand of a copying or linking command is the destination it clobbers.
  if (/^(cp|mv|install|rsync|ln|copy|move|xcopy|robocopy|scp|sftp)$/.test(app)) {
    // `-t DIR` names the destination and turns every operand into a source; otherwise the last operand is it.
    const flagged = args.findIndex(arg => /^(-t|--target-directory)$/.test(arg));
    const inline = args.find(arg => arg.startsWith("--target-directory="));
    if (inline) written.push(inline.slice("--target-directory=".length));
    else if (flagged >= 0 && args[flagged + 1]) written.push(args[flagged + 1]);
    else if (operands.length > 1) written.push(operands[operands.length - 1]);
  }
  if (app === "sed" && args.some(arg => /^(-[a-z]*i|--in-place)/.test(arg))) {
    const scripted = args.some(arg => /^(-e|--expression|-f|--file)/.test(arg));
    written.push(...operands.slice(scripted ? 0 : 1));
  }
  // Uploading into a remote /tmp is ordinary transfer; writing a remote system path is privileged persistence.
  if (written.some(target => {
    const remote = remotePath(target);
    return remote === undefined ? isDestructiveRmTarget(target, cwd) : SYSTEM_PATH.test(remote);
  })) return "shell_destructive_os";
  if (app === "find" && (args.includes("-delete") || args.some((arg, k) => /^-(exec|execdir|ok|okdir)$/.test(arg)
    && /^(rm|unlink|shred|rmdir|truncate|dd|sh|bash|zsh)$/.test(executable(args[k + 1] ?? ""))))) {
    const firstFlag = args.findIndex(arg => arg.startsWith("-"));
    if (args.slice(0, firstFlag < 0 ? args.length : firstFlag).some(root => isDestructiveRmTarget(root, cwd))) return "shell_destructive_os";
  }
  if (app === "docker" && args[0] === "run") {
    for (let k = 1; k < args.length; k++) {
      const spec = /^(?:--volume|--mount)=([\s\S]*)$/.exec(args[k])?.[1] ?? (/^(-v|--volume|--mount)$/.test(args[k]) ? args[k + 1] ?? "" : "");
      const host = spec.includes("=") ? /(?:^|,)(?:source|src)=([^,]+)/.exec(spec)?.[1] ?? "" : spec.split(":")[0];
      if (host && isDestructiveRmTarget(host, cwd)) return "shell_destructive_os";
    }
  }
  // Redirection truncates its target as surely as rm does.
  for (let k = 0; k < args.length; k++) {
    const redirect = /^\d?>{1,2}([\s\S]*)$/.exec(args[k]);
    if (redirect && isDestructiveRmTarget(redirect[1] || args[k + 1] || "", cwd)) return "shell_destructive_os";
  }
  // `psql --version` connects to nothing; every other invocation, bare included, opens a session.
  if ((app === "psql" || app === "pg_restore")
    && !(args.length > 0 && args.every(arg => /^(-V|--version|-\?|--help)$/.test(arg)))) return "shared_db_ddl_dml";
  if (app === "alembic" && args.some(a => /^(upgrade|downgrade|stamp)$/.test(a))) return "shared_db_ddl_dml";
  // A client is only the pipe: the statement is the mutation, whichever binary carries it.
  if (/^(mysql|mariadb|sqlite3|mongo|mongosh|clickhouse-client|cockroach|duckdb|sqlcmd|osql|surreal)$/.test(app)
    && args.some(arg => /\b(drop\s+(table|database|schema|index|view)|truncate\b|delete\s+from|alter\s+table|create\s+(table|database|schema|index)|insert\s+into|update\s+\S+\s+set|grant\s|revoke\s)/i.test(arg)
      || /\.(drop|dropDatabase|deleteMany|deleteOne|remove|updateMany|insertMany|renameCollection)\s*\(/.test(arg))) return "shared_db_ddl_dml";
  if (/^(redis-cli|valkey-cli)$/.test(app) && args.some(arg => /^(flushall|flushdb|shutdown)$/i.test(arg))) return "shared_db_ddl_dml";
  // A migration runner applies DDL that never appears on the command line.
  if (app === "prisma" && args[0] === "migrate" && /^(deploy|dev|reset|resolve)$/.test(args[1] ?? "")) return "shared_db_ddl_dml";
  if (app === "drizzle-kit" && /^(push|migrate|drop)$/.test(args[0] ?? "")) return "shared_db_ddl_dml";
  if (app === "knex" && /^(migrate|seed):/.test(args[0] ?? "")) return "shared_db_ddl_dml";
  if (app === "sequelize" && /^db:/.test(args[0] ?? "")) return "shared_db_ddl_dml";
  if (app === "typeorm" && /^(migration:(run|revert)|schema:(sync|drop))$/.test(args[0] ?? "")) return "shared_db_ddl_dml";
  if (/^(flyway|goose|dbmate|atlas|liquibase)$/.test(app)
    && args.some(a => /^(migrate|up|down|apply|clean|update|drop|rollback)$/.test(a))) return "shared_db_ddl_dml";
  if (/^(rails|rake|bundle|artisan)$/.test(app)
    && args.some(a => /^db:(migrate|drop|reset|rollback|schema:load)$/.test(a) || a === "migrate:fresh")) return "shared_db_ddl_dml";
  if (args.includes("manage.py") && args.some(a => /^(migrate|flush|sqlflush)$/.test(a))) return "shared_db_ddl_dml";
  if (app === "supabase") {
    if (args[0] === "db" && /^(push|reset|remote|execute|dump)$/.test(args[1] ?? "")) return "shared_db_ddl_dml";
    if (args[0] === "migration" && /^(up|repair)$/.test(args[1] ?? "")) return "shared_db_ddl_dml";
    if (args[0] === "projects" && args[1] === "delete") return "shared_db_ddl_dml";
    if (args[0] === "secrets" && /^(set|unset)$/.test(args[1] ?? "")) return "cloudflare_stripe_mutations";
  }
  // The management API performs the same mutation over HTTP; classify the endpoint, not only the project ref.
  // Every client spells a write differently: curl's -d/-F, wget's --post-data, PowerShell's -Method/-Body.
  if (/^(curl|wget|http|httpie|invoke-restmethod|irm|invoke-webrequest|iwr)$/.test(app)) {
    const url = args.find(arg => /^https?:\/\//i.test(arg)) ?? "";
    // PowerShell writes `-Method POST` with one dash and accepts any unambiguous prefix of the name.
    const cmdlet = /^(invoke-restmethod|irm|invoke-webrequest|iwr)$/.test(app);
    const verb = args.find((arg, k) => {
      const previous = args[k - 1] ?? "";
      return /^(-X|--request|--method)$/i.test(previous) || (cmdlet && /^-[A-Za-z]+$/.test(previous) && flagPrefixOf(previous, "method"));
    })
      ?? /^--(?:method|request)=([\s\S]+)$/i.exec(args.find(arg => /^--(method|request)=/i.test(arg)) ?? "")?.[1]
      ?? args[0] ?? "";
    const mutates = /^(POST|PUT|PATCH|DELETE)$/i.test(verb)
      || args.some(arg => /^(-d|--data|--data-raw|--data-binary|--data-urlencode|--json|--upload-file|-T|-F|--form|--post-data|--post-file|--body-data|-Body|-InFile|-Form)$/i.test(arg)
        || /^(--data[\w-]*|--post-data|--post-file|--body-data|--form|--json)=/i.test(arg));
    if (mutates && /api\.supabase\.com\/v1\/projects\/[^/]+\/(database|secrets|config)/i.test(url)) return "shared_db_ddl_dml";
    if (mutates && (/\/api\/[\w.]*(deploy|redeploy)\b/i.test(url) || /\b(api\.machines\.dev|api\.fly\.io)\b/i.test(url))) return "deployments";
    // Deleting a protected ref over the REST API is the same deletion as `git push --delete`.
    const ref = /\/git\/refs\/heads\/([\w./-]+)/i.exec(url)?.[1];
    if (mutates && ref && PROTECTED.test(ref)) return "destructive_git";
  }
  // `gh api` carries the same REST calls with the token already attached.
  if (app === "gh" && args[0] === "api") {
    const verb = args.find((arg, k) => /^(-X|--method)$/.test(args[k - 1] ?? "")) ?? "GET";
    const endpoint = args.slice(1).find(arg => !arg.startsWith("-") && !/^(GET|POST|PUT|PATCH|DELETE)$/i.test(arg)) ?? "";
    if (/^(POST|PUT|PATCH|DELETE)$/i.test(verb)) {
      const ref = /git\/refs\/heads\/([\w./-]+)/i.exec(endpoint)?.[1];
      if (ref && PROTECTED.test(ref)) return "destructive_git";
      if (/\/(deployments|pages\/builds)\b/i.test(endpoint)) return "deployments";
    }
  }
  if (app === "dokploy" || (args.includes("dokploy") && !/^(echo|printf|cat)$/.test(app))) return "deployments";
  if ((app === "fly" || app === "flyctl") && args.includes("deploy") && !/^(echo|printf|cat)$/.test(app)) return "deployments";
  if (app === "wrangler" && /^(deploy|publish)$/.test(args[0] ?? "")) return "deployments";
  if (/^(vercel|now|netlify)$/.test(app)
    && (args.some(a => /^--prod(uction)?$/.test(a)) || args[0] === "deploy")) return "deployments";
  if (app === "kubectl"
    && /^(apply|delete|create|replace|patch|scale|rollout|drain|cordon|uncordon|set|taint|exec)$/.test(args[0] ?? "")) return "deployments";
  if (app === "helm" && /^(install|upgrade|uninstall|delete|rollback)$/.test(args[0] ?? "")) return "deployments";
  if (/^(terraform|tofu)$/.test(app) && /^(apply|destroy|import)$/.test(args[0] ?? "")) return "deployments";
  if (app === "pulumi" && /^(up|destroy|refresh)$/.test(args[0] ?? "")) return "deployments";
  if (app === "railway" && /^(up|redeploy|down|delete)$/.test(args[0] ?? "")) return "deployments";
  if (/^(serverless|sls)$/.test(app) && /^(deploy|remove)$/.test(args[0] ?? "")) return "deployments";
  if (app === "ansible-playbook" || app === "capistrano") return "deployments";
  if (app === "eb" && /^(deploy|terminate)$/.test(args[0] ?? "")) return "deployments";
  if (app === "gcloud" && args.includes("deploy")) return "deployments";
  if (app === "aws" && (args[0] === "ecs" && args[1] === "update-service"
    || args[0] === "cloudformation" && /^(deploy|delete-stack|update-stack)$/.test(args[1] ?? "")
    || args[0] === "lambda" && /^update-function-(code|configuration)$/.test(args[1] ?? ""))) return "deployments";
  if (app === "deploy" && args.some(a => /^(prod|production)$/i.test(a))) return "deployments";
  // A wrapper script names its own purpose: deploy-prod.sh never reaches the argv checks below.
  if (/deploy[\w.-]*(prod|production)|(prod|production)[\w.-]*deploy/i.test(app)) return "deployments";
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
  const isProtected = (ref: string) => {
    const dest = ref.includes(":") ? ref.split(":").pop()! : ref.replace(/^\+/, "");
    return PROTECTED.test(dest.replace(/^refs\/heads\//, ""));
  };
  if (/^(filter-branch|filter-repo)$/.test(action ?? "")) return "destructive_git";
  // The hard-block set names protected-branch deletion, not only protected-branch pushes.
  if (action === "branch" && rest.some(a => /^-[a-zA-Z]*[dD]$/.test(a) || a === "--delete")
    && rest.filter(a => !a.startsWith("-")).some(isProtected)) return "destructive_git";
  if (action === "update-ref" && rest.some(a => /^(-d|--delete)$/.test(a))
    && rest.filter(a => !a.startsWith("-")).some(isProtected)) return "destructive_git";
  // Renaming or resetting a branch onto a protected name destroys that branch exactly as a delete does.
  if (action === "branch") {
    const operands = rest.filter(a => !a.startsWith("-"));
    const forced = rest.some(a => /^(-[a-zA-Z]*f|--force)$/.test(a));
    const renamed = rest.some(a => /^-[a-zA-Z]*M$/.test(a)) || (forced && rest.some(a => /^-[a-zA-Z]*m$/.test(a)));
    if (renamed && operands.length && isProtected(operands[operands.length - 1])) return "destructive_git";
    if (forced && operands.some(isProtected)) return "destructive_git";
  }
  // `git worktree remove` and `git clean` delete real files, so their targets face the out-of-tree test.
  if (action === "worktree" && rest[0] === "remove"
    && rest.slice(1).some(p => !p.startsWith("-") && isDestructiveRmTarget(p, cwd))) return "shell_destructive_os";
  if (action === "clean" && rest.some(p => !p.startsWith("-") && isDestructiveRmTarget(p, cwd))) return "shell_destructive_os";
  if (action === "push") {
    const isMirror = rest.some(a => a === "--mirror" || a === "--prune");
    if (isMirror) return "destructive_git";
    const isForce = rest.some(a => /^-[^-]*f/.test(a) || /^--force(?:-with-lease|-if-includes)?(?:=.*)?$/.test(a));
    const isDelete = rest.some(a => a === "--delete" || /^-[^-]*d$/.test(a));
    const nonFlags = rest.filter(a => !a.startsWith("-"));
    const refspecs = nonFlags.length > 1 ? nonFlags.slice(1) : nonFlags;
    if (isForce && refspecs.some(isProtected)) return "destructive_git";
    if (isDelete && refspecs.some(isProtected)) return "destructive_git";
    if (refspecs.some(r => r.startsWith("+") && isProtected(r))) return "destructive_git";
    if (refspecs.some(r => r.startsWith(":") && isProtected(r))) return "destructive_git";
  }
}
/** A proven category beats "the lexer could not resolve this"; production exclusion beats everything. */
function selectCategory(categories: (string | undefined)[]): string | undefined {
  if (categories.includes("production_exclusion")) return "production_exclusion";
  return categories.find(category => category && category !== DYNAMIC_CATEGORY) ?? categories.find(Boolean);
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
    // A substitution feeding an argument is data; only an unresolvable program name or eval payload is dynamic code.
    if (category !== "production_exclusion" && protectedPath(input)) category = "secrets";
    // Code the lexer cannot resolve is approval-gated; silently allowing it is the bypass it was meant to stop.
    if (!category && unresolved) category = DYNAMIC_CATEGORY;
    if (!category) return { allowed: true };
    commands = commands.map(words => words.map(word => word.split(DYNAMIC).join("<dynamic>")));
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
