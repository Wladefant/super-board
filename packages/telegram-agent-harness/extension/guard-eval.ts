/** A conservative call-site lexer, not a sandbox. Comments and inert literals never become commands.
 * Statically resolvable native process calls are classified as argv; dynamic process arguments
 * require exact approval instead of being silently allowed. Runtime tool guards remain the floor.
 */
type Token = { kind: "name" | "string" | "symbol"; value: string; dynamic?: boolean };
type Value = string | Value[] | { [key: string]: Value };
function tokenize(code: string, language: string): Token[] {
  const tokens: Token[] = [];
  for (let i = 0; i < code.length;) {
    const c = code[i];
    if (/\s/.test(c)) { i++; continue; }
    if (language === "py" && c === "#" || language !== "py" && code.slice(i, i + 2) === "//") {
      while (i < code.length && code[i] !== "\n") i++;
      continue;
    }
    if (language !== "py" && code.slice(i, i + 2) === "/*") {
      const end = code.indexOf("*/", i + 2); i = end < 0 ? code.length : end + 2; continue;
    }
    if (c === "'" || c === '"' || c === "`") {
      const triple = language === "py" && code.slice(i, i + 3) === c.repeat(3);
      const delimiter = triple ? c.repeat(3) : c;
      i += delimiter.length;
      let value = "", closed = false;
      while (i < code.length) {
        if (code.slice(i, i + delimiter.length) === delimiter) { i += delimiter.length; closed = true; break; }
        if (code[i] === "\\" && i + 1 < code.length) {
          const escaped = code[++i]; value += ({ n: "\n", r: "\r", t: "\t" } as Record<string, string>)[escaped] ?? escaped; i++;
        } else value += code[i++];
      }
      tokens.push({ kind: "string", value, dynamic: !closed || c === "`" && value.includes("${") }); continue;
    }
    const name = /^[A-Za-z_$][\w$]*/.exec(code.slice(i));
    if (name) { tokens.push({ kind: "name", value: name[0] }); i += name[0].length; continue; }
    tokens.push({ kind: "symbol", value: c }); i++;
  }
  return tokens;
}
export function evalCommands(code: string, language: string, parseShell: (command: string) => string[][]): { commands: string[][]; unresolved: boolean } {
  const tokens = tokenize(code, language), values = new Map<string, Value>();
  const aliases = new Map<string, string>();
  const commands: string[][] = [];
  let unresolved = false;
  const valueAt = (start: number): { value?: Value; end: number } => {
    const token = tokens[start];
    if (!token) return { end: start };
    let value: Value | undefined, end = start + 1;
    if (token.kind === "string" && !token.dynamic) value = token.value;
    else if (token.kind === "name") value = values.get(token.value);
    else if (token.value === "[") {
      const items: Value[] = []; let at = start + 1;
      while (tokens[at] && tokens[at].value !== "]") {
        const item = valueAt(at);
        if (item.value === undefined) return { end: at };
        items.push(item.value); at = item.end;
        if (tokens[at]?.value === ",") at++; else if (tokens[at]?.value !== "]") return { end: at };
      }
      if (tokens[at]?.value === "]") { value = items; end = at + 1; }
    } else if (token.value === "{") {
      const object: { [key: string]: Value } = {}; let at = start + 1;
      while (tokens[at] && tokens[at].value !== "}") {
        const key = tokens[at++].value;
        if (tokens[at++]?.value !== ":") return { end: at };
        const item = valueAt(at);
        if (item.value === undefined) return { end: at };
        object[key] = item.value; at = item.end;
        if (tokens[at]?.value === ",") at++; else if (tokens[at]?.value !== "}") return { end: at };
      }
      if (tokens[at]?.value === "}") { value = object; end = at + 1; }
    }
    if (tokens[end]?.value === "+") {
      const rhs = valueAt(end + 1);
      value = typeof value === "string" && typeof rhs.value === "string" ? value + rhs.value : undefined;
      end = rhs.end;
    }
    return { value, end };
  };
  const append = (value?: Value) => {
    if (typeof value === "string") commands.push(...parseShell(value));
    else if (Array.isArray(value) && value.every(v => typeof v === "string")) commands.push(value as string[]);
    else unresolved = true;
  };
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (t.kind !== "name") continue;
    if (tokens[i + 1]?.value === "as" && tokens[i + 2]?.kind === "name") aliases.set(tokens[i + 2].value, t.value);
    if (tokens[i + 1]?.value === "=" && tokens[i + 2]?.value !== "=") {
      const result = valueAt(i + 2);
      if (result.value !== undefined) values.set(t.value, result.value); else values.delete(t.value);
      // Preserve imported/module aliases and captured process call names.
      if (tokens[i + 2]?.kind === "name") {
        let target = tokens[i + 2].value, j = i + 3;
        while (tokens[j]?.value === "." && tokens[j + 1]?.kind === "name") { target += `.${tokens[j + 1].value}`; j += 2; }
        aliases.set(t.value, target);
      }
    }
    // Only the root of a dotted call is visited (never words inside a string).
    if (tokens[i - 1]?.value === ".") continue;
    let call = aliases.get(t.value) ?? t.value, at = i + 1;
    while (tokens[at]?.value === "." && tokens[at + 1]?.kind === "name") { call += `.${tokens[at + 1].value}`; at += 2; }
    const leaf = call.split(".").pop()!;
    if (leaf === "$" && tokens[at]?.kind === "string") { append(tokens[at].dynamic ? undefined : tokens[at].value); continue; }
    if (tokens[at]?.value !== "(") continue;
    const argument = valueAt(at + 1);
    // Partial expressions (f-strings, indexing, calls, interpolation) are NOT static commands.
    const complete = tokens[argument.end]?.value === "," || tokens[argument.end]?.value === ")";
    const value = complete ? argument.value : undefined;
    if (/^(os\.(system|popen)|subprocess\.(run|call|check_call|check_output|Popen)|.*\.(execSync|execFileSync|execFile|spawn|spawnSync)|execSync|execFileSync|execFile|spawn|spawnSync|Popen|system|popen|check_call|check_output)$/.test(call) ||
        leaf === "exec" && call !== "exec" || call === "subprocess.run" || call === "subprocess.call") {
      if (leaf === "execFile" || leaf === "execFileSync" || leaf === "spawn" || leaf === "spawnSync") {
        if (call.startsWith("Bun.") && value && typeof value === "object" && !Array.isArray(value)) append(value.cmd);
        else if (Array.isArray(value)) append(value);
        else if (typeof value === "string" && tokens[argument.end]?.value === ",") {
          const args = valueAt(argument.end + 1).value;
          if (Array.isArray(args)) append([value, ...args]); else unresolved = true;
        } else if (typeof value === "string") append([value]); else unresolved = true;
      } else append(value);
    } else if (/^tool\.(bash|launch|ssh)$/.test(call)) {
      if (!value || typeof value !== "object" || Array.isArray(value)) unresolved = true;
      else if (leaf === "bash") append(value.command);
      else if (leaf === "ssh") commands.push(["ssh"]);
      else if (typeof value.application === "string" && Array.isArray(value.args)) append([value.application, ...value.args]);
      else unresolved = true;
    } else if (/^(eval|exec|Function)$/.test(call)) {
      // Dynamic native code evaluation is an execution construct, not inert quoted data.
      unresolved = true;
    }
  }
  return { commands, unresolved };
}
