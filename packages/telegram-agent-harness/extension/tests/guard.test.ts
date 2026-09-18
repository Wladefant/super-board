import { afterEach, beforeEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { DangerousToolGuard, approveOperation, computeApprovalHash, loadGuardConfig, isDestructiveRmTarget } from "../guard";
import { Database } from "bun:sqlite";
import { decideApproval } from "../approvals";

let stateDir: string, guard: DangerousToolGuard;
beforeEach(() => {
  stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "guard-93-"));
  guard = new DangerousToolGuard(stateDir);
});
afterEach(() => {
  fs.rmSync(stateDir, { recursive: true, force: true });
});

for (const tool of ["task", "write", "edit", "search", "todo", "irc"]) {
  test(`AC1: ${tool} treats instructions as data`, () => {
    const input = { prompt: "git push origin feat/93; truncation; psql; ssh; dokploy; stripe refunds create", content: "DROP TABLE x; git push --force origin main", input: "rm -rf temp", path: "docs/example.ts" };
    expect(guard.evaluateToolCall(tool, input, true)).toEqual({ allowed: true });
    expect(guard.evaluateToolCall(tool, input, false)).toEqual({ allowed: true });
  });
}

test("AC2: force overwrite to protected branch is refused with origin parity", () => {
  const input = { command: "git push --force origin main" };
  const local = guard.evaluateToolCall("bash", input, false);
  const remote = guard.evaluateToolCall("bash", input, true);
  expect(local).toEqual(remote);
  expect(local.allowed).toBe(false);
  expect(local.category).toBe("destructive_git");
});

test("ordinary working branches pass with both origins, including force push and branch deletion", () => {
  for (const branch of ["feat/93", "fix/93", "test/93"]) {
    for (const origin of [false, true]) {
      expect(guard.evaluateToolCall("bash", { command: `git push -u origin ${branch}` }, origin)).toEqual({ allowed: true });
      expect(guard.evaluateToolCall("bash", { command: `git push --force origin ${branch}` }, origin)).toEqual({ allowed: true });
      expect(guard.evaluateToolCall("bash", { command: `git push -f origin ${branch}` }, origin)).toEqual({ allowed: true });
      expect(guard.evaluateToolCall("bash", { command: `git push origin +${branch}` }, origin)).toEqual({ allowed: true });
      expect(guard.evaluateToolCall("bash", { command: `git push --delete origin ${branch}` }, origin)).toEqual({ allowed: true });
      expect(guard.evaluateToolCall("bash", { command: `git push origin :${branch}` }, origin)).toEqual({ allowed: true });
    }
  }
});

test("protected trunks and destructive history stay gated", () => {
  for (const command of [
    "git push --force origin main",
    "git push -f origin staging",
    "git push origin +HEAD:main",
    "git push origin +staging",
    "git push origin :main",
    "git push --delete origin staging",
    "git push origin feat/x:refs/heads/production --force",
    "git push --mirror origin",
    "git filter-branch --force",
    "git filter-repo --analyze",
  ]) {
    expect(guard.evaluateToolCall("bash", { command }, true).category).toBe("destructive_git");
  }
});

test("Contract ALLOWED examples all return allowed: true without approval", () => {
  const allowedCommands = [
    // plain git push (any refs, incl. staging/main, non-force)
    "git push",
    "git push origin main",
    "git push origin staging",
    "git push origin HEAD:staging",
    "git push origin feat/x:main",
    "git push origin feat/x",
    "git push -u origin feat/test",
    // git history and branch operations
    "git rebase main",
    "git rebase -i HEAD~3",
    "git commit --amend",
    "git commit --amend --no-edit",
    "git branch -D old",
    "git branch -d old",
    "git branch -df old",
    "git checkout -- .",
    "git restore .",
    "git clean",
    "git clean -fd",
    "git reset --hard",
    "git reset --hard HEAD~1",
    // process management
    "kill 1234",
    "kill -9 1234",
    "taskkill /f /pid 1234",
    "taskkill /im node.exe",
    // privilege & orchestration
    "sudo apt update",
    "sudo systemctl restart nginx",
    "docker compose up",
    "docker compose up -d",
    // remote tools (unless production)
    "ssh host",
    "ssh user@example.com uptime",
    "scp file.txt host:/tmp/",
    "sftp host",
    // database read and migration revision
    "pg_dump my_db",
    "pg_dump -Fc mydb > backup.dump",
    "pg_stat_activity",
    "pg_stat_statements",
    "alembic revision -m 'add users table'",
    "alembic revision --autogenerate",
    "supabase db diff",
    "supabase status",
    // filesystem removal inside cwd
    "rm -rf build",
    "rm -rf dist",
    "rm -rf temp",
    "rm -rf ./temp/logs",
    "rm file.txt",
    "rm -f other.txt",
  ];

  for (const command of allowedCommands) {
    const result = guard.evaluateToolCall("bash", { command, cwd: "C:/work" });
    expect(result).toEqual({ allowed: true });
  }

  // launch restart is allowed
  expect(guard.evaluateToolCall("launch", { op: "restart", name: "dev-server" })).toEqual({ allowed: true });

  // ssh tool (non-production) is allowed
  expect(guard.evaluateToolCall("ssh", { host: "test-host", command: "uptime" })).toEqual({ allowed: true });
});

test("eval dynamic process arguments no longer trigger category; plain code remains allowed", () => {
  for (const [language, code] of [
    ["py", "subprocess.run(make_command())"],
    ["js", "Bun.spawn(commandFromNetwork)"],
    ["py", 'os.system(f"git {action}")'],
    ["js", 'execSync(`git ${action}`)'],
  ]) {
    expect(guard.evaluateToolCall("eval", { language, code })).toEqual({ allowed: true });
  }

  for (const [language, code] of [
    ["py", '"""os.system("ssh host")"""\nprint("git push --force")'],
    ["js", '/* execSync("ssh host") */ const text = "git push --force"; console.log(text)'],
    ["js", 'const expression = /ssh/; expression.exec("ssh")'],
    ["py", 'subprocess.run(["git", "status"])'],
    ["js", 'Bun.spawn(["git","push","origin","feat/x"])'],
  ]) {
    expect(guard.evaluateToolCall("eval", { language, code })).toEqual({ allowed: true });
  }
});

test("eval inspects real calls and gates destructive actions on protected branches", () => {
  for (const language of ["py", "js"]) {
    const comment = language === "py" ? "#" : "//";
    expect(guard.evaluateToolCall("eval", { language, code: `${comment} git push --force origin main\ntext = 'ssh host; psql'` })).toEqual({ allowed: true });
  }
  expect(guard.evaluateToolCall("eval", { language: "py", code: 'import subprocess\nsubprocess.run(["git", "push", "--force", "origin", "main"])' }).category).toBe("destructive_git");
  expect(guard.evaluateToolCall("eval", { language: "js", code: 'child_process.execSync("git push --force origin main")' }).category).toBe("destructive_git");
});

test("AC3: full token, runnable local unlock, expiry and single-use grant", () => {
  const input = { command: "git push --force origin main", cwd: "C:/work" };
  const blocked = guard.evaluateToolCall("bash", input);
  expect(blocked.approvalHash).toMatch(/^[a-f0-9]{64}$/);
  expect(blocked.reason).toContain(`/approve ${blocked.approvalHash}`);
  expect(blocked.reason).toContain('bun "');
  const record = approveOperation(stateDir, blocked.approvalHash!);
  expect(record.operationHash).toBe(computeApprovalHash(record.category, JSON.stringify({ toolName: "bash", input, cwd: input.cwd })));
  expect(Date.parse(record.expiresAt) - Date.now()).toBeGreaterThan(899000);
  expect(guard.evaluateToolCall("bash", input)).toEqual({ allowed: true });
  expect(guard.evaluateToolCall("bash", input).allowed).toBe(false);
});

const invocations: Record<string, string[]> = {
  shared_db_ddl_dml: ["psql -c 'DROP TABLE x'", "alembic upgrade head", "alembic downgrade -1", "alembic stamp head", "supabase db reset", "supabase db push", "supabase db remote commit", "pg_restore dump.sql"],
  deployments: ["dokploy compose redeploy", "deploy prod", "deploy production", "fly deploy", "wrangler deploy", "wrangler publish"],
  cloudflare_stripe_mutations: ["wrangler secret put KEY", "stripe refunds create", "stripe charges create", "topup_main_reset 100"],
  shell_destructive_os: ["rm -rf C:/", "rm -rf /", "rm -rf ../..", "format C:", "shutdown", "reboot", "dd if=image"],
  destructive_git: ["git -C repo push --force-with-lease origin main", "git push --delete origin main", "git push -f origin staging", "git push origin HEAD:main --force", "git push origin +HEAD:main", "git push origin :main", "git push --mirror origin", "git filter-branch --force", "git filter-repo --analyze"],
};

for (const [category, commands] of Object.entries(invocations)) {
  test(`${category}: genuine invocations versus quoted prose in both origins`, () => {
    for (const command of commands) for (const origin of [false, true]) {
      expect(guard.evaluateToolCall("bash", { command, cwd: "C:/work" }, origin).category).toBe(category);
      expect(guard.evaluateToolCall("bash", { command: `echo "${command.replace(/"/g, '\\"')}"`, cwd: "C:/work" }, origin)).toEqual({ allowed: true });
      expect(guard.evaluateToolCall("bash", { command: `printf ok; ${command}`, cwd: "C:/work" }, origin).category).toBe(category);
      expect(guard.evaluateToolCall("bash", { command: `# ${command}\nprintf ok`, cwd: "C:/work" }, origin)).toEqual({ allowed: true });
    }
  });
}

test("origin state and overrides never change permissions", () => {
  for (const input of [{ command: "git push origin feat/x" }, { command: "ssh host" }, { command: "git push --force origin main" }]) {
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
  expect(guard.evaluateToolCall("launch", { op: "start", application: "C:/Program Files/Git/bin/git.exe", args: ["push", "--force", "origin", "main"] }).category).toBe("destructive_git");
  expect(guard.evaluateToolCall("launch", { op: "logs", name: "ssh", grep: "psql" })).toEqual({ allowed: true });
  expect(guard.evaluateToolCall("launch", { op: "start", application: "sh", args: ["-c", "psql -c 'DROP TABLE users'"] }).category).toBe("shared_db_ddl_dml");
});

test("remote path mentions are data, command boundaries and substitutions execute", () => {
  expect(guard.evaluateToolCall("bash", { command: "cat /docs/ssh/readme.txt" })).toEqual({ allowed: true });
  for (const command of ["printf ok && psql", "printf ok | psql", 'echo "$(psql)"', "echo `psql`"]) {
    expect(guard.evaluateToolCall("bash", { command }).category).toBe("shared_db_ddl_dml");
  }
  expect(guard.evaluateToolCall("bash", { command: "echo '$(psql)'" })).toEqual({ allowed: true });
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
    'import subprocess as sp\nsp.run(["git","push","--force","origin","main"])',
    'from subprocess import run as execute\nexecute(["git","push","--force","origin","main"])',
    'import os\ncmd = "git " + "push --force origin main"\nos.system(cmd)',
  ]) expect(guard.evaluateToolCall("eval", { language: "py", code }).category).toBe("destructive_git");
  for (const code of [
    'const cmd = ["git", "push", "--force", "origin", "main"]; Bun.spawn(cmd)',
    'spawn("git", ["push", "--force", "origin", "main"])',
    'await tool.bash({command: "git push --force origin main"})',
    'await Bun.$`git push --force origin main`',
  ]) expect(guard.evaluateToolCall("eval", { language: "js", code }).category).toBe("destructive_git");
});

test("eval native secret access and OS destruction stay gated", () => {
  expect(guard.evaluateToolCall("eval", { language: "py", code: 'open("/keys/id_rsa")' }).category).toBe("secrets");
  expect(guard.evaluateToolCall("eval", { language: "js", code: 'fs.readFileSync("/keys/jwt_secret")' }).category).toBe("secrets");
  expect(guard.evaluateToolCall("eval", { language: "py", code: 'shutil.rmtree("/data")' }).category).toBe("shell_destructive_os");
  expect(guard.evaluateToolCall("eval", { language: "js", code: 'fs.rmSync("/data", {recursive:true})' }).category).toBe("shell_destructive_os");
});

test("approval rejects unknown, partial and expired grants", () => {
  expect(() => approveOperation(stateDir, "a".repeat(12))).toThrow("64-character");
  expect(() => approveOperation(stateDir, "a".repeat(64))).toThrow("No pending");
  const input = { command: "git push --force origin main" }, token = guard.evaluateToolCall("bash", input).approvalHash!;
  const db = new Database(path.join(stateDir, "approval-audit.sqlite"));
  db.run("UPDATE approvals SET expires_at = ?", [new Date(0).toISOString()]);
  db.close();
  expect(() => approveOperation(stateDir, token)).toThrow("expired");
  expect(guard.evaluateToolCall("bash", input).allowed).toBe(false);
});

test("approval cannot authorize a changed working directory or environment", () => {
  const input = { command: "git push --force origin main", cwd: "C:/a", env: { TARGET: "a" } };
  const token = guard.evaluateToolCall("bash", input).approvalHash!;
  approveOperation(stateDir, token);
  expect(guard.evaluateToolCall("bash", { ...input, cwd: "C:/b" }).allowed).toBe(false);
  expect(guard.evaluateToolCall("bash", { ...input, env: { TARGET: "b" } }).allowed).toBe(false);
  expect(guard.evaluateToolCall("bash", input)).toEqual({ allowed: true });
});

test("local unlock instruction executes the real CLI without executing the refused operation", () => {
  const input = { command: "git push --force origin main" }, token = guard.evaluateToolCall("bash", input).approvalHash!;
  const result = Bun.spawnSync([process.execPath, path.join(import.meta.dir, "../guard.ts"), "approve", stateDir, token]);
  expect(result.exitCode).toBe(0);
  expect(JSON.parse(result.stdout.toString()).approved).toBe(true);
  expect(guard.evaluateToolCall("bash", input)).toEqual({ allowed: true });
  expect(guard.evaluateToolCall("bash", input).allowed).toBe(false);
});

test("a grant for one command cannot authorize another command, session or requester", () => {
  const input = { command: "rm -rf C:/" };
  const token = guard.evaluateToolCall("bash", input).approvalHash!;
  expect(() => decideApproval(stateDir, token, "approved", { sessionId: "foreign", userId: "1", chatId: "2" })).toThrow("another session");
  approveOperation(stateDir, token);
  expect(guard.evaluateToolCall("bash", { command: "rm -rf /" }).allowed).toBe(false);
  expect(guard.evaluateToolCall("bash", input, false, { sessionId: "other", requester: "Other", task: "Other", cwd: "/tmp" }).allowed).toBe(false);
  expect(guard.evaluateToolCall("bash", input).allowed).toBe(true);
  const repeated = guard.evaluateToolCall("bash", input);
  expect(repeated.allowed).toBe(false);
  expect(repeated.approvalHash).not.toBe(token);
  expect(() => approveOperation(stateDir, token)).toThrow("consumed");
});

test("denial persists and is not mistaken for silence; audit retains full safe command and actor", () => {
  const input = { command: "rm -rf C:/" };
  const token = guard.evaluateToolCall("bash", input).approvalHash!;
  decideApproval(stateDir, token, "denied", { sessionId: "local", userId: "operator-42", chatId: "chat-7" });
  expect(new DangerousToolGuard(stateDir).evaluateToolCall("bash", input).reason).toContain("explicitly denied");
  expect(() => approveOperation(stateDir, token)).toThrow("denied");
  const db = new Database(path.join(stateDir, "approval-audit.sqlite"), { readonly: true });
  const events = db.query("SELECT decision, actor, at, record FROM approval_events ORDER BY id").all() as { decision: string; actor: string; at: string; record: string }[];
  db.close();
  expect(events.map(event => event.decision)).toEqual(["pending", "denied"]);
  expect(events[1].actor).toContain("operator-42");
  expect(JSON.parse(events[1].record).command).toBe(input.command);
  expect(JSON.parse(events[1].record).requester).toBe("Local operator");
  expect(Date.parse(events[1].at)).toBeGreaterThan(0);
});

test("secret-bearing commands cannot be approved blind and secrets never enter audit", () => {
  const secret = "synthetic-private-value";
  const result = guard.evaluateToolCall("bash", { command: `psql postgresql://user:${secret}@db/prod`, env: { PASSWORD: secret } });
  expect(result.approval!.approvable).toBe(false);
  expect(result.approval!.command).not.toContain(secret);
  expect(() => approveOperation(stateDir, result.approvalHash!)).toThrow("redacted");
  const db = new Database(path.join(stateDir, "approval-audit.sqlite"), { readonly: true });
  expect(JSON.stringify(db.query("SELECT * FROM approval_events").all())).not.toContain(secret);
  db.close();
});

test("shell heredoc prose stays inert while substitutions and trailing commands execute", () => {
  for (const command of [
    "cat <<'EOF'\npsql\nEOF",
    "cat <<EOF\npsql\nEOF",
    "cat <<'EOF'\n$(psql)\nEOF",
  ]) expect(guard.evaluateToolCall("bash", { command })).toEqual({ allowed: true });
  for (const command of ["cat <<EOF\n$(psql)\nEOF", "cat <<'EOF'\ntext\nEOF\npsql", "(psql)", "env -i psql", "if psql; then echo ok; fi"]) {
    expect(guard.evaluateToolCall("bash", { command }).category).toBe("shared_db_ddl_dml");
  }
});

test("eval regex literals remain data while template interpolations execute", () => {
  expect(guard.evaluateToolCall("eval", { language: "js", code: 'const pattern = /execSync("psql")/;' })).toEqual({ allowed: true });
  expect(guard.evaluateToolCall("eval", { language: "js", code: 'const text = `${execSync("psql")}`;' }).category).toBe("shared_db_ddl_dml");
});

test("two concurrent guard processes can consume only one approval", async () => {
  const input = { command: "git push --force origin main" }, token = guard.evaluateToolCall("bash", input).approvalHash!;
  approveOperation(stateDir, token);
  const guardFile = path.join(import.meta.dir, "../guard.ts").replace(/\\/g, "/");
  const stateDirNormalized = stateDir.replace(/\\/g, "/");
  const script = `import { DangerousToolGuard } from ${JSON.stringify(guardFile)}; console.log(JSON.stringify(new DangerousToolGuard(${JSON.stringify(stateDirNormalized)}).evaluateToolCall("bash", ${JSON.stringify(input)})));`;
  const children = [Bun.spawn([process.execPath, "-e", script], { stdout: "pipe" }), Bun.spawn([process.execPath, "-e", script], { stdout: "pipe" })];
  const results = await Promise.all(children.map(async child => {
    const text = await new Response(child.stdout).text();
    expect(await child.exited).toBe(0); return JSON.parse(text).allowed;
  }));
  expect(results.filter(Boolean)).toHaveLength(1);
}, 15000);

test("dedicated remote-process tool has transport-neutral production exclusion", () => {
  for (const origin of [false, true]) {
    const denied = guard.evaluateToolCall("ssh", { host: "akamai-iad-prod", command: "uptime" }, origin);
    expect(denied.category).toBe("production_exclusion");
    expect(denied.approvalHash).toBeUndefined();
    expect(guard.evaluateToolCall("ssh", { host: "test-host", command: "uptime" }, origin)).toEqual({ allowed: true });
  }
});

test("review delta: production environment paths are protected without scanning file content", () => {
  for (const origin of [false, true]) for (const file of [".env.prod", "backend/.env.prod", "C:\\app\\.env.prod", "/app/.env.prod"]) {
    expect(guard.evaluateToolCall("write", { path: file, content: "example" }, origin).category).toBe("secrets");
    expect(guard.evaluateToolCall("read", { path: file }, origin).category).toBe("secrets");
  }
  expect(guard.evaluateToolCall("write", { path: "docs/config.txt", content: "Document backend/.env.prod here" })).toEqual({ allowed: true });
  expect(guard.evaluateToolCall("write", { path: ".env.product", content: "example" })).toEqual({ allowed: true });
});

test("review delta: shell builtins and process wrappers preserve execution boundaries", () => {
  for (const origin of [false, true]) {
    for (const command of [
      'eval "git push --force origin main"',
      'eval "git " "push --force origin main"',
      "nohup git push --force origin main",
      "time git push --force origin main",
      "time -f %e -o timing.txt git push --force origin main",
      "printf ref | xargs git push --force origin main",
      "printf ref | xargs -I item git push --force origin main",
      "printf ref | xargs -n 1 -P 2 git push --force origin main",
      'bash -c "git push --force origin main"',
      'env X=1 git push -f origin staging',
    ]) expect(guard.evaluateToolCall("bash", { command }, origin).category).toBe("destructive_git");
    for (const command of [
      'echo \'eval "git push --force origin main"\'',
      'eval \'echo "git push --force origin main"\'',
      'nohup echo "git push --force origin main"',
      'time echo "git push --force origin main"',
      'printf ref | xargs echo "git push --force origin main"',
      "nohup git push origin feat/x",
      "time git push origin fix/x",
      "printf ref | xargs -n 1 echo",
    ]) expect(guard.evaluateToolCall("bash", { command }, origin)).toEqual({ allowed: true });
  }
});

test("rm -r target rule: relative paths inside cwd allowed, dangerous paths blocked", () => {
  const cwd = "C:/work/project";

  // Allowed: relative paths inside cwd
  for (const target of ["build", "dist", "temp", "./temp", "logs/debug", "sub/dir/cache"]) {
    expect(isDestructiveRmTarget(target, cwd)).toBe(false);
    expect(guard.evaluateToolCall("bash", { command: `rm -rf ${target}`, cwd })).toEqual({ allowed: true });
  }

  // Blocked: drive roots, root, home, parent traversal, outside absolute paths
  for (const target of ["C:/", "C:\\", "C:", "/", "\\", "~", "$HOME", "${HOME}", "..", "../..", "../../outside", "D:/other/dir", "C:/Windows"]) {
    expect(isDestructiveRmTarget(target, cwd)).toBe(true);
    expect(guard.evaluateToolCall("bash", { command: `rm -rf ${target}`, cwd }).category).toBe("shell_destructive_os");
  }

  // Non-recursive rm is never shell_destructive_os
  expect(guard.evaluateToolCall("bash", { command: "rm file.txt", cwd })).toEqual({ allowed: true });
  expect(guard.evaluateToolCall("bash", { command: "rm -f file.txt", cwd })).toEqual({ allowed: true });
});

test("guard.json configuration: mode off, allow, deny, invalid JSON fallback", () => {
  // 1. Default (absent guard.json): minimal mode, triggers block
  expect(guard.evaluateToolCall("bash", { command: "git push --force origin main" }).category).toBe("destructive_git");
  expect(guard.evaluateToolCall("bash", { command: "psql" }).category).toBe("shared_db_ddl_dml");

  // 2. Mode 'off': skips gating except production_exclusion
  const offDir = fs.mkdtempSync(path.join(os.tmpdir(), "guard-off-"));
  try {
    fs.writeFileSync(path.join(offDir, "guard.json"), JSON.stringify({ mode: "off" }));
    const offGuard = new DangerousToolGuard(offDir);
    expect(offGuard.evaluateToolCall("bash", { command: "git push --force origin main" })).toEqual({ allowed: true });
    expect(offGuard.evaluateToolCall("bash", { command: "psql" })).toEqual({ allowed: true });
    expect(offGuard.evaluateToolCall("bash", { command: "rm -rf C:/" })).toEqual({ allowed: true });
    // Production exclusion is NEVER filtered
    expect(offGuard.evaluateToolCall("bash", { command: "ssh akamai-iad-prod" }).category).toBe("production_exclusion");
    expect(offGuard.evaluateToolCall("bash", { command: "ssh akamai-iad-prod" }).allowed).toBe(false);
  } finally {
    fs.rmSync(offDir, { recursive: true, force: true });
  }

  // 3. Mode 'off' with deny: re-enables specified categories
  const offDenyDir = fs.mkdtempSync(path.join(os.tmpdir(), "guard-off-deny-"));
  try {
    fs.writeFileSync(path.join(offDenyDir, "guard.json"), JSON.stringify({ mode: "off", deny: ["destructive_git"] }));
    const offDenyGuard = new DangerousToolGuard(offDenyDir);
    expect(offDenyGuard.evaluateToolCall("bash", { command: "git push --force origin main" }).category).toBe("destructive_git");
    expect(offDenyGuard.evaluateToolCall("bash", { command: "git push --force origin main" }).allowed).toBe(false);
    expect(offDenyGuard.evaluateToolCall("bash", { command: "psql" })).toEqual({ allowed: true });
  } finally {
    fs.rmSync(offDenyDir, { recursive: true, force: true });
  }

  // 4. Mode 'minimal' with allow: un-gates allowed categories (never production_exclusion)
  const allowDir = fs.mkdtempSync(path.join(os.tmpdir(), "guard-allow-"));
  try {
    fs.writeFileSync(path.join(allowDir, "guard.json"), JSON.stringify({ mode: "minimal", allow: ["destructive_git", "production_exclusion"] }));
    const allowGuard = new DangerousToolGuard(allowDir);
    expect(allowGuard.evaluateToolCall("bash", { command: "git push --force origin main" })).toEqual({ allowed: true });
    expect(allowGuard.evaluateToolCall("bash", { command: "psql" }).category).toBe("shared_db_ddl_dml");
    // production_exclusion can NEVER be allowed
    expect(allowGuard.evaluateToolCall("bash", { command: "ssh akamai-iad-prod" }).category).toBe("production_exclusion");
  } finally {
    fs.rmSync(allowDir, { recursive: true, force: true });
  }

  // 5. Mode 'minimal' with allow and deny: deny takes precedence (re-enables)
  const allowDenyDir = fs.mkdtempSync(path.join(os.tmpdir(), "guard-allow-deny-"));
  try {
    fs.writeFileSync(path.join(allowDenyDir, "guard.json"), JSON.stringify({ mode: "minimal", allow: ["destructive_git"], deny: ["destructive_git"] }));
    const allowDenyGuard = new DangerousToolGuard(allowDenyDir);
    expect(allowDenyGuard.evaluateToolCall("bash", { command: "git push --force origin main" }).category).toBe("destructive_git");
    expect(allowDenyGuard.evaluateToolCall("bash", { command: "git push --force origin main" }).allowed).toBe(false);
  } finally {
    fs.rmSync(allowDenyDir, { recursive: true, force: true });
  }

  // 6. Invalid JSON falls back to minimal, logged once, never crashes
  const badJsonDir = fs.mkdtempSync(path.join(os.tmpdir(), "guard-bad-json-"));
  try {
    fs.writeFileSync(path.join(badJsonDir, "guard.json"), "{ invalid: json");
    const badGuard = new DangerousToolGuard(badJsonDir);
    expect(badGuard.evaluateToolCall("bash", { command: "git push --force origin main" }).category).toBe("destructive_git");
    expect(badGuard.evaluateToolCall("bash", { command: "git push --force origin main" }).allowed).toBe(false);
  } finally {
    fs.rmSync(badJsonDir, { recursive: true, force: true });
  }
});

test("ApprovalRecord summary contains category-specific plain-language description with concrete operands", () => {
  // Force push summary
  const gitBlocked = guard.evaluateToolCall("bash", { command: "git push --force origin main" });
  expect(gitBlocked.approval?.summary).toBe("Force-push branch main to origin (rewrites shared history)");

  // RM summary
  const rmBlocked = guard.evaluateToolCall("bash", { command: "rm -rf C:/" });
  expect(rmBlocked.approval?.summary).toBe("Remove files recursively at C:/");

  // Alembic migration summary
  const alembicBlocked = guard.evaluateToolCall("bash", { command: "alembic upgrade head" });
  expect(alembicBlocked.approval?.summary).toBe("Run database migration via alembic upgrade");

  // Dokploy summary
  const dokployBlocked = guard.evaluateToolCall("bash", { command: "dokploy compose redeploy" });
  expect(dokployBlocked.approval?.summary).toBe("Trigger deployment via dokploy");

  // Secrets summary
  const secretsBlocked = guard.evaluateToolCall("write", { path: ".env.prod", content: "SECRET=1" });
  expect(secretsBlocked.approval?.summary).toContain("Access protected sensitive path");
});
