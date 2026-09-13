import { afterEach, beforeEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { DangerousToolGuard, approveOperation, computeApprovalHash } from "../guard";
let stateDir: string, guard: DangerousToolGuard;
beforeEach(() => { stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "guard-93-")); guard = new DangerousToolGuard(stateDir); });
afterEach(() => { fs.rmSync(stateDir, { recursive: true, force: true }); });
for (const tool of ["task", "write", "edit", "search", "todo", "irc"]) {
  test(`AC1: ${tool} treats instructions as data`, () => {
    const input = { prompt: "git push origin feat/93; truncation; psql; ssh; dokploy; stripe refunds create", content: "DROP TABLE x; git push --force origin main", input: "rm -rf temp", path: "docs/example.ts" };
    expect(guard.evaluateToolCall(tool, input, true)).toEqual({ allowed: true });
    expect(guard.evaluateToolCall(tool, input, false)).toEqual({ allowed: true });
  });
}
test("AC2: force overwrite is refused with origin parity", () => {
  const input = { command: "git push --force origin feat/93" };
  const local = guard.evaluateToolCall("bash", input, false);
  const remote = guard.evaluateToolCall("bash", input, true);
  expect(local).toEqual(remote); expect(local.allowed).toBe(false); expect(local.category).toBe("destructive_git");
});
test("ordinary working branches pass with both origins", () => {
  for (const branch of ["feat/93", "fix/93", "test/93"]) for (const origin of [false, true]) {
    expect(guard.evaluateToolCall("bash", { command: `git push -u origin ${branch}` }, origin)).toEqual({ allowed: true });
  }
});
test("protected trunks and destructive history stay gated", () => {
  for (const command of ["git push origin main", "git push origin HEAD:staging", "git push origin feat/x:refs/heads/production", "git reset --hard", "git clean -fd", "git branch -D old", "git rebase main", "git push origin +HEAD:feat/x"]) {
    expect(guard.evaluateToolCall("bash", { command }, true).category).toBe("destructive_git");
  }
});
test("eval inspects real calls, not inert comments and strings", () => {
  for (const language of ["py", "js"]) {
    const comment = language === "py" ? "#" : "//";
    expect(guard.evaluateToolCall("eval", { language, code: `${comment} git push --force origin main\ntext = 'ssh host; psql'` })).toEqual({ allowed: true });
  }
  expect(guard.evaluateToolCall("eval", { language: "py", code: 'import subprocess\nsubprocess.run(["git", "push", "--force", "origin", "feat/x"])' }).category).toBe("destructive_git");
  expect(guard.evaluateToolCall("eval", { language: "js", code: 'child_process.execSync("git push --force origin feat/x")' }).category).toBe("destructive_git");
});
test("AC3: full token, runnable local unlock, expiry and single-use grant", () => {
  const input = { command: "git push --force origin feat/x", cwd: "C:/work" };
  const blocked = guard.evaluateToolCall("bash", input);
  expect(blocked.approvalHash).toMatch(/^[a-f0-9]{64}$/);
  expect(blocked.reason).toContain(`/approve ${blocked.approvalHash}`);
  expect(blocked.reason).toContain('bun "');
  const record = approveOperation(stateDir, blocked.approvalHash!);
  expect(computeApprovalHash(record.category, record.content)).toBe(blocked.approvalHash!);
  expect(Date.parse(record.expiresAt) - Date.now()).toBeGreaterThan(899000);
  expect(fs.existsSync(path.join(stateDir, "approved", `${blocked.approvalHash}.json`))).toBe(true);
  expect(guard.evaluateToolCall("bash", input)).toEqual({ allowed: true });
  expect(fs.existsSync(path.join(stateDir, "approved", `${blocked.approvalHash}.json`))).toBe(false);
  expect(guard.evaluateToolCall("bash", input).allowed).toBe(false);
});

const invocations: Record<string, string[]> = {
  shared_db_ddl_dml: ["psql -c 'DROP TABLE x'", "alembic upgrade head", "supabase db reset", "pg_restore dump.sql"],
  deployments: ["dokploy compose redeploy", "deploy staging", "fly deploy", "docker compose up -d"],
  cloudflare_stripe_mutations: ["wrangler deploy", "cf worker upload", "stripe refunds create", "create_checkout_session"],
  remote_ssh: ["ssh host uptime", "scp a host:b", "sftp host"],
  shell_destructive_os: ["rm -rf temp", "format C:", "shutdown", "reboot", "sudo true", "dd if=image", "kill -9 123", "taskkill /f /pid 123"],
  destructive_git: ["git -C repo push --force-with-lease origin feat/x", "git branch --delete --force old", "git push -f origin feat/x", "git push origin HEAD:main"],
};
for (const [category, commands] of Object.entries(invocations)) {
  test(`${category}: genuine invocations versus quoted prose in both origins`, () => {
    for (const command of commands) for (const origin of [false, true]) {
      expect(guard.evaluateToolCall("bash", { command }, origin).category).toBe(category);
      expect(guard.evaluateToolCall("bash", { command: `echo "${command.replace(/"/g, '\\"')}"` }, origin)).toEqual({ allowed: true });
      expect(guard.evaluateToolCall("bash", { command: `printf ok; ${command}` }, origin).category).toBe(category);
      expect(guard.evaluateToolCall("bash", { command: `# ${command}\nprintf ok` }, origin)).toEqual({ allowed: true });
    }
  });
}
test("origin state and overrides never change permissions", () => {
  for (const input of [{ command: "git push origin feat/x" }, { command: "ssh host" }, { command: "git push --force origin feat/x" }]) {
    guard.startLocalTurn(); const local = guard.evaluateToolCall("bash", input);
    guard.startTelegramTurn("turn"); expect(guard.isTelegramTurnActive()).toBe(true);
    expect(guard.evaluateToolCall("bash", input)).toEqual(local);
    expect(guard.evaluateToolCall("bash", input, false)).toEqual(local);
    guard.endTurn(); expect(guard.evaluateToolCall("bash", input)).toEqual(local);
  }
});
test("path protections remain for every text tool without scanning payload", () => {
  for (const tool of ["task", "write", "edit", "search", "todo", "irc", "read", "glob", "grep"]) {
    for (const origin of [false, true]) {
      expect(guard.evaluateToolCall(tool, { path: "/keys/id_rsa" }, origin).category).toBe("secrets");
      expect(guard.evaluateToolCall(tool, { files: ["/keys/service_role"] }, origin).category).toBe("secrets");
      expect(guard.evaluateToolCall(tool, { content: "id_rsa jwt_secret", path: "docs/keys.md" }, origin)).toEqual({ allowed: true });
    }
  }
});
test("launch uses application and argv, not labels or log patterns", () => {
  expect(guard.evaluateToolCall("launch", { op: "start", application: "echo", args: ["ssh host; git push --force"], ready: { log: "psql" } })).toEqual({ allowed: true });
  expect(guard.evaluateToolCall("launch", { op: "start", application: "C:/Program Files/Git/bin/git.exe", args: ["push", "--force", "origin", "feat/x"] }).category).toBe("destructive_git");
  expect(guard.evaluateToolCall("launch", { op: "logs", name: "ssh", grep: "psql" })).toEqual({ allowed: true });
  expect(guard.evaluateToolCall("launch", { op: "start", application: "sh", args: ["-c", "ssh host"] }).category).toBe("remote_ssh");
});
test("remote path mentions are data, command boundaries and substitutions execute", () => {
  expect(guard.evaluateToolCall("bash", { command: "cat /docs/ssh/readme.txt" })).toEqual({ allowed: true });
  for (const command of ["printf ok && ssh host", "printf ok | ssh host", 'echo "$(ssh host)"', "echo `ssh host`"]) {
    expect(guard.evaluateToolCall("bash", { command }).category).toBe("remote_ssh");
  }
  expect(guard.evaluateToolCall("bash", { command: "echo '$(ssh host)'" })).toEqual({ allowed: true });
});
test("production exclusion cannot be superseded by another category or grant", () => {
  for (const origin of [false, true]) {
    const input = { command: "ssh host; psql postgresql://zaraprptkegxqpvnsubu", path: "id_rsa" };
    const result = guard.evaluateToolCall("bash", input, origin);
    expect(result.category).toBe("production_exclusion"); expect(result.allowed).toBe(false);
    expect(result.approvalHash).toBeUndefined();
    expect(fs.existsSync(path.join(stateDir, "approved"))).toBe(false);
  }
});
test("eval resolves process aliases, arrays, constants and concatenation", () => {
  for (const code of [
    'import subprocess as sp\nsp.run(["git","push","--force","origin","feat/x"])',
    'from subprocess import run as execute\nexecute(["git","push","--force","origin","feat/x"])',
    'import os\ncmd = "git " + "push --force origin feat/x"\nos.system(cmd)',
  ]) expect(guard.evaluateToolCall("eval", { language: "py", code }).category).toBe("destructive_git");
  for (const code of [
    'const cmd = ["git", "push", "--force", "origin", "feat/x"]; Bun.spawn(cmd)',
    'spawn("git", ["push", "--force", "origin", "feat/x"])',
    'await tool.bash({command: "git push --force origin feat/x"})',
    'await Bun.$`git push --force origin feat/x`',
  ]) expect(guard.evaluateToolCall("eval", { language: "js", code }).category).toBe("destructive_git");
});
test("eval dynamic process arguments require approval; ordinary code remains allowed", () => {
  for (const [language, code] of [
    ["py", "subprocess.run(make_command())"], ["js", "Bun.spawn(commandFromNetwork)"],
    ["py", 'os.system(f"git {action}")'], ["js", 'execSync(`git ${action}`)'],
  ]) expect(guard.evaluateToolCall("eval", { language, code }).allowed).toBe(false);
  for (const [language, code] of [
    ["py", '"""os.system("ssh host")"""\nprint("git push --force")'],
    ["js", '/* execSync("ssh host") */ const text = "git push --force"; console.log(text)'],
    ["js", 'const expression = /ssh/; expression.exec("ssh")'],
    ["py", 'subprocess.run(["git", "status"])'], ["js", 'Bun.spawn(["git","push","origin","feat/x"])'],
  ]) expect(guard.evaluateToolCall("eval", { language, code })).toEqual({ allowed: true });
});
test("eval native secret access and OS destruction stay gated", () => {
  expect(guard.evaluateToolCall("eval", { language: "py", code: 'open("/keys/id_rsa")' }).category).toBe("secrets");
  expect(guard.evaluateToolCall("eval", { language: "js", code: 'fs.readFileSync("/keys/jwt_secret")' }).category).toBe("secrets");
  expect(guard.evaluateToolCall("eval", { language: "py", code: 'shutil.rmtree("/data")' }).category).toBe("shell_destructive_os");
  expect(guard.evaluateToolCall("eval", { language: "js", code: 'fs.rmSync("/data", {recursive:true})' }).category).toBe("shell_destructive_os");
});
test("approval rejects unknown, partial, expired, malformed and mismatched records", () => {
  expect(() => approveOperation(stateDir, "a".repeat(12))).toThrow("64-character");
  expect(() => approveOperation(stateDir, "a".repeat(64))).toThrow("No pending");
  const input = { command: "ssh host" }, token = guard.evaluateToolCall("bash", input).approvalHash!;
  const pending = path.join(stateDir, "approved", "pending", `${token}.json`);
  const record = JSON.parse(fs.readFileSync(pending, "utf8"));
  fs.writeFileSync(pending, JSON.stringify({ ...record, expiresAt: new Date(0).toISOString() }));
  expect(() => approveOperation(stateDir, token)).toThrow("expired");
  const approved = path.join(stateDir, "approved", `${token}.json`);
  for (const invalid of [{ ...record, expiresAt: 0 }, { ...record, content: "other" }, { ...record, singleUse: false }, { ...record, expiresAt: "not-a-date" }, { ...record, expiresAt: new Date(0).toISOString() }]) {
    fs.writeFileSync(approved, JSON.stringify(invalid));
    expect(guard.evaluateToolCall("bash", input).allowed).toBe(false);
  }
  fs.writeFileSync(approved, "{");
  expect(guard.evaluateToolCall("bash", input).allowed).toBe(false);
});
test("approval cannot authorize a changed working directory or environment", () => {
  const input = { command: "ssh host", cwd: "C:/a", env: { TARGET: "a" } };
  const token = guard.evaluateToolCall("bash", input).approvalHash!;
  approveOperation(stateDir, token);
  expect(guard.evaluateToolCall("bash", { ...input, cwd: "C:/b" }).allowed).toBe(false);
  expect(guard.evaluateToolCall("bash", { ...input, env: { TARGET: "b" } }).allowed).toBe(false);
  expect(guard.evaluateToolCall("bash", input)).toEqual({ allowed: true });
});
test("local unlock instruction executes the real CLI without executing the refused operation", () => {
  const input = { command: "ssh host" }, token = guard.evaluateToolCall("bash", input).approvalHash!;
  const result = Bun.spawnSync([process.execPath, path.join(import.meta.dir, "../guard.ts"), "approve", stateDir, token]);
  expect(result.exitCode).toBe(0);
  expect(JSON.parse(result.stdout.toString()).approved).toBe(true);
  expect(guard.evaluateToolCall("bash", input)).toEqual({ allowed: true });
  expect(guard.evaluateToolCall("bash", input).allowed).toBe(false);
});

test("shell heredoc prose stays inert while substitutions and trailing commands execute", () => {
  for (const command of [
    "cat <<'EOF'\nssh host\npsql\nEOF",
    "cat <<EOF\nssh host\nEOF",
    "cat <<'EOF'\n$(ssh host)\nEOF",
  ]) expect(guard.evaluateToolCall("bash", { command })).toEqual({ allowed: true });
  for (const command of ["cat <<EOF\n$(ssh host)\nEOF", "cat <<'EOF'\ntext\nEOF\nssh host", "(ssh host)", "env -i ssh host", "if ssh host; then echo ok; fi"]) {
    expect(guard.evaluateToolCall("bash", { command }).category).toBe("remote_ssh");
  }
});
test("eval regex literals remain data while template interpolations execute", () => {
  expect(guard.evaluateToolCall("eval", { language: "js", code: 'const pattern = /execSync("ssh host")/;' })).toEqual({ allowed: true });
  expect(guard.evaluateToolCall("eval", { language: "js", code: 'const text = `${execSync("ssh host")}`;' }).category).toBe("remote_ssh");
});
test("two concurrent guard processes can consume only one approval", async () => {
  const input = { command: "ssh host" }, token = guard.evaluateToolCall("bash", input).approvalHash!;
  approveOperation(stateDir, token);
  const script = `import { DangerousToolGuard } from ${JSON.stringify(path.join(import.meta.dir, "../guard.ts").replace(/\\\\/g, "/"))}; console.log(JSON.stringify(new DangerousToolGuard(${JSON.stringify(stateDir)}).evaluateToolCall("bash", ${JSON.stringify(input)})));`;
  const children = [Bun.spawn([process.execPath, "-e", script], { stdout: "pipe" }), Bun.spawn([process.execPath, "-e", script], { stdout: "pipe" })];
  const results = await Promise.all(children.map(async child => {
    const text = await new Response(child.stdout).text();
    expect(await child.exited).toBe(0); return JSON.parse(text).allowed;
  }));
  expect(results.filter(Boolean)).toHaveLength(1);
});

test("combined forced deletion switches and amended history remain gated", () => {
  for (const command of ["git branch -df old", "git branch -d -f old", "git commit --amend"]) {
    expect(guard.evaluateToolCall("bash", { command }).category).toBe("destructive_git");
  }
  expect(guard.evaluateToolCall("bash", { command: "git branch -d merged" })).toEqual({ allowed: true });
});
test("dedicated remote-process tool has transport-neutral production exclusion", () => {
  for (const origin of [false, true]) {
    const denied = guard.evaluateToolCall("ssh", { host: "akamai-iad-prod", command: "uptime" }, origin);
    expect(denied.category).toBe("production_exclusion"); expect(denied.approvalHash).toBeUndefined();
    expect(guard.evaluateToolCall("ssh", { host: "test-host", command: "uptime" }, origin).category).toBe("remote_ssh");
  }
});
