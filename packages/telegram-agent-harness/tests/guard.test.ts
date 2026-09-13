import { afterEach, beforeEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { DangerousToolGuard, approveOperation, computeApprovalHash } from "../extension/guard";
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
