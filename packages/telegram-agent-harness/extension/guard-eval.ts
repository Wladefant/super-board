/** A conservative call-site lexer, not a sandbox. Comments and inert literals never become commands.
 * Statically resolvable native process calls are classified as argv; dynamic process arguments
 * require exact approval instead of being silently allowed. Runtime tool guards remain the floor.
 */
type Token = { kind: "name" | "string" | "symbol"; value: string; dynamic?: boolean; embedded?: string[] };
type Value = string | Value[] | { [key: string]: Value };

/** Decodes only canonical base64. Anything else returns "" so callers fall through to the raw string
 * instead of acting on mojibake that `Buffer.from(x, "base64")` silently produces for arbitrary text. */
export function decodeBase64(raw: string): string {
  try {
    const clean = raw.trim().replace(/^b['"]|['"]$/g, "").replace(/\s+/g, "");
    if (clean.length < 4 || clean.length % 4 === 1) return "";
    if (!/^[A-Za-z0-9+/]+={0,2}$/.test(clean)) return "";
    const buf = Buffer.from(clean, "base64");
    if (!buf.length) return "";
    if (buf.toString("base64").replace(/=+$/, "") !== clean.replace(/=+$/, "")) return "";
    const printable = /^[\x20-\x7e\t\r\n]+$/;
    const u16 = buf.toString("utf16le");
    if (printable.test(u16) && /[a-zA-Z]/.test(u16)) return u16;
    const u8 = buf.toString("utf8");
    return printable.test(u8) && /[a-zA-Z]/.test(u8) ? u8 : "";
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
    if (language === "py" && /^(?:[bBrRuUfF]{1,2})?['"`]/.test(code.slice(i))) {
      const pMatch = /^(?:[bBrRuUfF]{1,2})?/.exec(code.slice(i));
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
      // An f-string interpolation is as unprovable as a JS template one, and its expression still runs.
      const embedded = sc === "`" ? [...rawValue.matchAll(/(?<!\\)\$\{([\s\S]*?)\}/g)].map(match => match[1])
        : /[fF]/.test(strPrefix) ? [...rawValue.matchAll(/(?<!\{)\{([^{}]+)\}/g)].map(match => match[1]) : [];
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

/** `node:child_process` and `child_process` are the same module; `fs/promises` is the `fs.promises` namespace. */
function moduleName(specifier: string): string {
  return specifier.replace(/^node:/, "").replace(/^fs\/promises$/, "fs.promises");
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
        for (;;) {
          // `const cp = require("node:child_process")` binds the module, not the loader.
          if (/(^|\.)(require|import_module|__import__)$/.test(target) && tokens[j]?.value === "(" && tokens[j + 1]?.kind === "string" && !tokens[j + 1].dynamic && tokens[j + 2]?.value === ")") {
            target = moduleName(tokens[j + 1].value); j += 3; continue;
          }
          if (tokens[j]?.value === "." && tokens[j + 1]?.kind === "name") { target += `.${tokens[j + 1].value}`; j += 2; continue; }
          break;
        }
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
    for (;;) {
      // `require("node:child_process").execSync(...)` is one expression, and so are
      // `process.mainModule.require(...)` and `importlib.import_module(...)`: the module names the
      // callee, so resolve it and keep walking members from there.
      if (/(^|\.)(require|import_module)$/.test(call) && tokens[at]?.value === "(" && tokens[at + 1]?.kind === "string" && tokens[at + 2]?.value === ")") {
        if (tokens[at + 1].dynamic) { unresolved = true; break; }
        call = moduleName(tokens[at + 1].value);
        at += 3;
        continue;
      }
      if (tokens[at]?.value === "." && tokens[at + 1]?.kind === "name") { call += `.${tokens[at + 1].value}`; at += 2; continue; }
      if (tokens[at]?.value !== "[") break;
      const key = valueAt(at + 1);
      if (typeof key.value === "string" && tokens[key.end]?.value === "]") { call += `.${key.value}`; at = key.end + 1; continue; }
      // A computed member whose key the lexer cannot prove, then invoked, is dynamically constructed code.
      let close = at + 1, depth = 1;
      while (close < tokens.length && depth) { if (tokens[close].value === "[") depth++; else if (tokens[close].value === "]") depth--; close++; }
      if (tokens[close]?.value === "(") unresolved = true;
      break;
    }
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
    // `execv(path, argv)` and the variadic `execl(path, "rm", "-rf", "/")` run argv; the path names the image.
    if (/^(os\.)?(execv|execve|execvp|execvpe|execl|execlp|execle|spawnv|spawnve|spawnvp|spawnl|spawnlp|posix_spawn|posix_spawnp)$/.test(call)) {
      const rest: string[] = [];
      for (let k = argument.end; tokens[k]?.value === ",";) {
        const next = valueAt(k + 1);
        if (typeof next.value === "string") rest.push(next.value);
        else if (Array.isArray(next.value) && next.value.every(item => typeof item === "string")) rest.push(...(next.value as string[]));
        else break;
        k = next.end;
      }
      if (rest.length) append(rest); else unresolved = true;
      continue;
    }
    // `Deno.run({ cmd: [...] })` and `new Deno.Command(name, { args: [...] })` spawn like the Node pair.
    if (call === "Deno.run" || call === "Deno.Command") {
      if (typeof value === "string") {
        const options = tokens[argument.end]?.value === "," ? valueAt(argument.end + 1).value : undefined;
        const extra = options && typeof options === "object" && !Array.isArray(options) ? options.args : undefined;
        append(Array.isArray(extra) && extra.every(item => typeof item === "string") ? [value, ...(extra as string[])] : [value]);
      } else if (value && typeof value === "object" && !Array.isArray(value)) append(value.cmd);
      else unresolved = true;
      continue;
    }
    if (/^(os\.(system|popen|popen2|popen3|popen4)|subprocess\.(run|call|check_call|check_output|Popen|getoutput|getstatusoutput)|asyncio\.create_subprocess_(shell|exec)|.*\.(execSync|execFileSync|execFile|spawn|spawnSync|execa|execaSync|execaCommand|execaCommandSync)|execSync|execFileSync|execFile|spawn|spawnSync|Popen|system|popen|popen2|popen3|popen4|check_call|check_output|getoutput|getstatusoutput|create_subprocess_shell|create_subprocess_exec|execa|execaSync|execaCommand|execaCommandSync)$/.test(call) ||
        /^(child_process|cp|childProcess)\.exec$/.test(call)) {
      if (/^(execFile|execFileSync|spawn|spawnSync|execa|execaSync|create_subprocess_exec)$/.test(leaf)) {
        if (call.startsWith("Bun.") && value && typeof value === "object" && !Array.isArray(value)) append(value.cmd);
        else if (Array.isArray(value)) append(value);
        else if (typeof value === "string" && tokens[argument.end]?.value === ",") {
          // `spawn(file, [args])` and the variadic `create_subprocess_exec(file, "-rf", "/")` both name argv.
          const argv = [value];
          for (let k = argument.end; tokens[k]?.value === ",";) {
            const next = valueAt(k + 1);
            if (typeof next.value === "string") argv.push(next.value);
            else if (Array.isArray(next.value) && next.value.every(item => typeof item === "string")) argv.push(...(next.value as string[]));
            else break;
            k = next.end;
          }
          if (argv.length > 1) append(argv); else unresolved = true;
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
    } else if (/^(fs(?:\.promises)?\.(cp|cpSync|copyFile|copyFileSync|rename|renameSync|link|linkSync|symlink|symlinkSync)|shutil\.(move|copy|copy2|copyfile|copytree)|os\.(rename|replace|link|symlink))$/.test(call)) {
      // A copy, move or link clobbers its destination, and the destination is the second operand.
      const destination = tokens[argument.end]?.value === "," ? valueAt(argument.end + 1).value : undefined;
      commands.push(["tee", ...(typeof destination === "string" ? [destination] : [])]);
    } else if (/^(fs(?:\.promises)?\.(truncate|truncateSync|ftruncate|appendFile|appendFileSync|createWriteStream|chmod|chmodSync|chown|chownSync)|os\.(truncate|chmod|chown))$/.test(call)) {
      // Truncating, appending to or re-permissioning a file mutates the path it names.
      commands.push(["tee", ...(typeof value === "string" ? [value] : [])]);
    } else if (leaf === "uploadFile") {
      // Attaching a file to a form reads it, and the paths follow the selector.
      for (let k = argument.end; tokens[k]?.value === ",";) {
        const next = valueAt(k + 1);
        if (typeof next.value === "string") commands.push(["cat", next.value]);
        else if (Array.isArray(next.value)) for (const item of next.value) if (typeof item === "string") commands.push(["cat", item]);
        else break;
        k = next.end;
      }
    } else if (call === "process.binding" || call === "process._linkedBinding") {
      // A raw internal binding hands back a process API this lexer cannot follow.
      unresolved = true;
    } else if (/^(read|write|open|fs(?:\.promises)?\.(readFile|readFileSync|writeFile|writeFileSync)|Bun\.(file|write)|Path|pathlib\.Path)$/.test(call)) {
      // A write clobbers its path; only a read leaves the file intact.
      const mode = tokens[argument.end]?.value === "," ? valueAt(argument.end + 1).value : undefined;
      let effect = /write/i.test(leaf) || (typeof mode === "string" && /[wax+]/.test(mode)) ? "tee" : "cat";
      if (leaf === "Path") {
        // `Path(p)` is inert until a method names the effect: `.write_text()` clobbers, `.unlink()` deletes.
        const method = tokens[argument.end]?.value === ")" && tokens[argument.end + 1]?.value === "." ? tokens[argument.end + 2]?.value ?? "" : "";
        if (/^(unlink|rmdir)$/.test(method)) effect = "rm";
        else if (/^(write_text|write_bytes|touch|mkdir|chmod)$/.test(method)) effect = "tee";
        else if (/^(rename|replace)$/.test(method)) {
          const destination = valueAt(argument.end + 4).value;
          commands.push(["tee", ...(typeof destination === "string" ? [destination] : [])]);
        } else if (method === "open") {
          const openMode = valueAt(argument.end + 4).value;
          effect = typeof openMode === "string" && /[wax+]/.test(openMode) ? "tee" : "cat";
        }
      }
      if (typeof value === "string") commands.push(effect === "rm" ? ["rm", "-rf", value] : [effect, value]);
    } else if (call === "getattr") {
      // `getattr(os, "sys" + "tem")(...)` is the Python spelling of a computed member call.
      let close = at + 1, comma = -1;
      for (let depth = 1; close < tokens.length && depth > 0; close++) {
        const punctuation = tokens[close].value;
        if (punctuation === "(" || punctuation === "[" || punctuation === "{") depth++;
        else if (punctuation === ")" || punctuation === "]" || punctuation === "}") depth--;
        else if (punctuation === "," && depth === 1 && comma < 0) comma = close;
      }
      if (tokens[close]?.value === "(") {
        const name = comma < 0 ? undefined : valueAt(comma + 1).value;
        // A name the lexer cannot prove leaves the callee unknown; a proven one is that member being called.
        if (typeof name !== "string") unresolved = true;
        else if (/^(system|popen|exec|execSync|execFile|execFileSync|spawn|spawnSync|run|call|check_call|check_output|Popen)$/.test(name)) append(valueAt(close + 1).value);
      }
    } else if (/^(eval|exec|Function|(?:vm\.)?runIn(NewContext|ThisContext|Context))$/.test(call)) {
      if (typeof value === "string") {
        const inner = evalCommands(value, language, parseShell);
        commands.push(...inner.commands);
        unresolved ||= inner.unresolved;
        // A payload that is no code in the host language is still whatever the runtime finally hands a shell.
        if (!inner.commands.length && !inner.unresolved) commands.push(...parseShell(value));
      } else {
        unresolved = true;
      }
    } else if (/^(setTimeout|setInterval)$/.test(call) && typeof value === "string") {
      // A timer called with a callback is inert; called with a string, the runtime evaluates it as code.
      const inner = evalCommands(value, language, parseShell);
      commands.push(...inner.commands);
      unresolved ||= inner.unresolved;
      if (!inner.commands.length && !inner.unresolved) commands.push(...parseShell(value));
    }
  }
  return { commands, unresolved };
}
