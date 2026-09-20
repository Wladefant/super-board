/** A conservative call-site lexer, not a sandbox. Comments and inert literals never become commands.
 * Statically resolvable native process calls are classified as argv; dynamic process arguments
 * require exact approval instead of being silently allowed. Runtime tool guards remain the floor.
 */
type Token = { kind: "name" | "string" | "symbol"; value: string; dynamic?: boolean; embedded?: string[] };
type Value = string | Value[] | { [key: string]: Value };

export function decodeBase64(raw: string): string {
  try {
    const clean = raw.trim().replace(/^b['"]|['"]$/g, "").replace(/\s+/g, "");
    if (!clean || clean.length < 2) return "";
    const buf = Buffer.from(clean, "base64");
    if (!buf.length) return "";
    const u16 = buf.toString("utf16le");
    if (/^[\x20-\x7e\t\r\n]+$/.test(u16) && /[a-zA-Z]/.test(u16)) return u16;
    const u8 = buf.toString("utf8");
    if (/^[\x20-\x7e\t\r\n]+$/.test(u8)) return u8;
    return u8;
  } catch {
    return "";
  }
}

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
    // A JS regexp literal is data, not a sequence of executable identifiers.
    if (language !== "py" && c === "/" && (!tokens.length || /^(=|\(|\[|,|:|return|=>)$/.test(tokens[tokens.length - 1].value))) {
      let inClass = false; i++;
      while (i < code.length) {
        if (code[i] === "\\") { i += 2; continue; }
        if (code[i] === "[") inClass = true;
        if (code[i] === "]") inClass = false;
        if (code[i++] === "/" && !inClass) break;
      }
      while (/[a-z]/i.test(code[i] ?? "")) i++;
      tokens.push({ kind: "symbol", value: "regexp" }); continue;
    }
    let strPrefix = "";
    if (language === "py" && /^[bBruU]?[rR]?['"`]/.test(code.slice(i))) {
      const pMatch = /^[bBruU]?[rR]?/.exec(code.slice(i));
      if (pMatch && pMatch[0].length > 0) {
        strPrefix = pMatch[0];
        i += strPrefix.length;
      }
    }
    const sc = code[i];
    if (sc === "'" || sc === '"' || sc === "`") {
      const triple = language === "py" && code.slice(i, i + 3) === sc.repeat(3);
      const delimiter = triple ? sc.repeat(3) : sc;
      i += delimiter.length;
      const rawStart = i;
      let value = "", closed = false;
      while (i < code.length) {
        if (code.slice(i, i + delimiter.length) === delimiter) { i += delimiter.length; closed = true; break; }
        if (code[i] === "\\" && i + 1 < code.length) {
          const escaped = code[++i]; value += ({ n: "\n", r: "\r", t: "\t" } as Record<string, string>)[escaped] ?? escaped; i++;
        } else value += code[i++];
      }
      const rawValue = code.slice(rawStart, closed ? i - delimiter.length : i);
      const embedded = sc === "`" ? [...rawValue.matchAll(/(?<!\\)\$\{([\s\S]*?)\}/g)].map(match => match[1]) : [];
      tokens.push({ kind: "string", value, dynamic: !closed || embedded.length > 0, embedded }); continue;
    } else if (strPrefix) {
      i -= strPrefix.length;
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
  for (const token of tokens) for (const expression of token.embedded ?? []) {
    const result = evalCommands(expression, language, parseShell);
    commands.push(...result.commands);
    unresolved ||= result.unresolved;
  }
  const valueAt = (start: number): { value?: Value; end: number } => {
    const token = tokens[start];
    if (!token) return { end: start };
    let value: Value | undefined, end = start + 1;
    if (token.kind === "string" && !token.dynamic) {
      value = token.value;
      if (tokens[end]?.value === "." && tokens[end + 1]?.value === "join" && tokens[end + 2]?.value === "(") {
        const listArg = valueAt(end + 3);
        if (Array.isArray(listArg.value) && listArg.value.every(v => typeof v === "string")) {
          value = (listArg.value as string[]).join(token.value);
          end = tokens[listArg.end]?.value === ")" ? listArg.end + 1 : listArg.end;
        }
      }
    } else if (token.kind === "name") {
      let nameChain = token.value, next = start + 1;
      while (tokens[next]?.value === "." && tokens[next + 1]?.kind === "name") {
        nameChain += "." + tokens[next + 1].value;
        next += 2;
      }
      const resolved = aliases.get(nameChain) ?? nameChain;
      if (tokens[next]?.value === "(") {
        if (/^(atob|decodeBase64)$/.test(resolved) || /^(base64\.(b64decode|decodebytes)|b64decode)$/.test(resolved)) {
          const arg = valueAt(next + 1);
          if (typeof arg.value === "string") {
            value = decodeBase64(arg.value);
            end = tokens[arg.end]?.value === ")" ? arg.end + 1 : arg.end;
          } else end = next + 1;
        } else if (resolved === "Buffer.from") {
          const arg1 = valueAt(next + 1);
          let at = arg1.end;
          if (tokens[at]?.value === ",") {
            const arg2 = valueAt(at + 1);
            at = arg2.end;
            if (typeof arg1.value === "string" && (arg2.value === "base64" || typeof arg2.value !== "string")) {
              value = decodeBase64(arg1.value);
            }
          }
          end = tokens[at]?.value === ")" ? at + 1 : at;
        } else if (resolved === "[System.Convert]::FromBase64String" || resolved === "FromBase64String") {
          const arg = valueAt(next + 1);
          if (typeof arg.value === "string") {
            value = decodeBase64(arg.value);
            end = tokens[arg.end]?.value === ")" ? arg.end + 1 : arg.end;
          } else end = next + 1;
        } else {
          value = values.get(token.value);
        }
      } else {
        value = values.get(token.value);
      }
    } else if (token.value === "[") {
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
    while (tokens[end]?.value === "." && tokens[end + 1]?.kind === "name") {
      const method = tokens[end + 1].value;
      if (tokens[end + 2]?.value === "(") {
        let depth = 1, closeParen = end + 3;
        while (closeParen < tokens.length) {
          if (tokens[closeParen].value === "(") depth++;
          else if (tokens[closeParen].value === ")") { if (--depth === 0) break; }
          closeParen++;
        }
        if (/^(decode|toString|strip|trim)$/.test(method) && typeof value === "string") {
          if (method === "strip" || method === "trim") value = value.trim();
          end = closeParen + 1;
          continue;
        }
        if (method === "join" && Array.isArray(value)) {
          const joinArg = valueAt(end + 3);
          const delim = typeof joinArg.value === "string" ? joinArg.value : " ";
          value = (value as string[]).join(delim);
          end = closeParen + 1;
          continue;
        }
        if (method === "split" && typeof value === "string") {
          const splitArg = valueAt(end + 3);
          const delim = typeof splitArg.value === "string" ? splitArg.value : " ";
          value = value.split(delim);
          end = closeParen + 1;
          continue;
        }
      }
      break;
    }
    while (tokens[end]?.value === "+") {
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
    if (language === "py" && t.value === "from" && tokens[i + 2]?.value === "import") {
      const module = tokens[i + 1]?.value;
      for (let j = i + 3; tokens[j]?.kind === "name";) {
        const imported = tokens[j++].value;
        const local = tokens[j]?.value === "as" ? tokens[j + 1]?.value : imported;
        if (tokens[j]?.value === "as") j += 2;
        if (local) aliases.set(local, `${module}.${imported}`);
        if (tokens[j]?.value !== ",") break;
        j++;
      }
    }
    if (tokens[i + 1]?.value === "as" && tokens[i + 2]?.kind === "name" && !aliases.has(tokens[i + 2].value)) aliases.set(tokens[i + 2].value, t.value);
    if (tokens[i + 1]?.value === "=" && tokens[i + 2]?.value !== "=") {
      const result = valueAt(i + 2);
      if (result.value !== undefined) values.set(t.value, result.value); else values.delete(t.value);
      if (tokens[i + 2]?.kind === "name") {
        let target = tokens[i + 2].value, j = i + 3;
        while (tokens[j]?.value === "." && tokens[j + 1]?.kind === "name") { target += `.${tokens[j + 1].value}`; j += 2; }
        aliases.set(t.value, target);
      }
    }
    if (tokens[i - 1]?.value === ".") continue;
    let call = aliases.get(t.value) ?? t.value, at = i + 1;
    if (language === "py" && t.value === "__import__" && tokens[i + 1]?.value === "(" && tokens[i + 2]?.kind === "string") {
      const mod = tokens[i + 2].value;
      let close = i + 3;
      while (close < tokens.length && tokens[close].value !== ")") close++;
      let target = mod, j = close + 1;
      while (tokens[j]?.value === "." && tokens[j + 1]?.kind === "name") { target += `.${tokens[j + 1].value}`; j += 2; }
      if (target !== mod) {
        call = target;
        at = j;
      }
    }
    while (tokens[at]?.value === "." && tokens[at + 1]?.kind === "name") { call += `.${tokens[at + 1].value}`; at += 2; }
    for (let hop = 0; hop < 8; hop++) {
      const [head, ...tail] = call.split(".");
      const resolved = aliases.get(head);
      if (!resolved || resolved === head) break;
      call = [resolved, ...tail].join(".");
    }
    const leaf = call.split(".").pop()!;
    if (leaf === "$" && tokens[at]?.kind === "string") { append(tokens[at].dynamic ? undefined : tokens[at].value); continue; }
    if (tokens[at]?.value !== "(") continue;
    const argument = valueAt(at + 1);
    const complete = tokens[argument.end]?.value === "," || tokens[argument.end]?.value === ")";
    const value = complete ? argument.value : undefined;
    if (/^(os\.(system|popen)|subprocess\.(run|call|check_call|check_output|Popen)|.*\.(execSync|execFileSync|execFile|spawn|spawnSync)|execSync|execFileSync|execFile|spawn|spawnSync|Popen|system|popen|check_call|check_output)$/.test(call) ||
        /^(child_process|cp|childProcess)\.exec$/.test(call) || call === "subprocess.run" || call === "subprocess.call") {
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
    } else if (/^(fs(?:\.promises)?\.(rm|rmSync|rmdir|rmdirSync|unlink|unlinkSync)|shutil\.rmtree|os\.(remove|unlink|rmdir|kill))$/.test(call)) {
      commands.push(["rm", "-rf", ...(typeof value === "string" ? [value] : [])]);
    } else if (/^(read|write|open|fs(?:\.promises)?\.(readFile|readFileSync|writeFile|writeFileSync)|Bun\.(file|write)|Path|pathlib\.Path)$/.test(call)) {
      if (typeof value === "string") commands.push(["cat", value]);
    } else if (/^(eval|exec|Function)$/.test(call)) {
      if (typeof value === "string") {
        const inner = evalCommands(value, language, parseShell);
        commands.push(...inner.commands);
        unresolved ||= inner.unresolved;
      } else {
        unresolved = true;
      }
    }
  }
  return { commands, unresolved };
}
