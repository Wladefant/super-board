import { afterEach, beforeEach, expect, test, describe } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { DangerousToolGuard } from "../guard";

let stateDir: string, guard: DangerousToolGuard;
beforeEach(() => {
  stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "guard-eval-97-"));
  guard = new DangerousToolGuard(stateDir);
});
afterEach(() => {
  fs.rmSync(stateDir, { recursive: true, force: true });
});

interface HarmlessTestCase {
  name: string;
  pattern: "heredoc" | "nested_subshell" | "python_c" | "powershell_command" | "invoke_expression" | "base64" | "eval_comment_prose";
  tool: "bash" | "eval";
  input: Record<string, unknown>;
}

const HARMLESS_CASES: HarmlessTestCase[] = [
  // 1. Heredoc harmless patterns
  {
    name: "cat heredoc with plain text",
    pattern: "heredoc",
    tool: "bash",
    input: { command: "cat <<EOF\nhello world\nEOF" },
  },
  {
    name: "cat quoted heredoc with psql command doc",
    pattern: "heredoc",
    tool: "bash",
    input: { command: "cat <<'EOF'\npsql -c 'DROP TABLE users'\nEOF" },
  },
  {
    name: "cat quoted heredoc with git force push doc",
    pattern: "heredoc",
    tool: "bash",
    input: { command: "cat <<'EOF'\ngit push --force origin main\nEOF" },
  },
  {
    name: "cat heredoc with production domain documentation",
    pattern: "heredoc",
    tool: "bash",
    input: { command: "cat <<'EOF'\nproduction: akamai-iad-prod\nEOF" },
  },

  // 2. Nested substitution harmless patterns
  {
    name: "echo git status in subshell",
    pattern: "nested_subshell",
    tool: "bash",
    input: { command: "echo $(git status)" },
  },
  {
    name: "echo git branch in subshell",
    pattern: "nested_subshell",
    tool: "bash",
    input: { command: "echo $(git branch --show-current)" },
  },
  {
    name: "nested echo calls",
    pattern: "nested_subshell",
    tool: "bash",
    input: { command: "echo $(echo $(echo harmless))" },
  },
  {
    name: "backtick subshell date",
    pattern: "nested_subshell",
    tool: "bash",
    input: { command: "echo `date`" },
  },

  // 3. Python -c harmless patterns
  {
    name: "python -c print hello",
    pattern: "python_c",
    tool: "bash",
    input: { command: "python -c \"print('hello world')\"" },
  },
  {
    name: "python -c sys.version",
    pattern: "python_c",
    tool: "bash",
    input: { command: "python -c \"import sys; print(sys.version)\"" },
  },
  {
    name: "python -c os.listdir",
    pattern: "python_c",
    tool: "bash",
    input: { command: "python -c \"import os; os.listdir('.')\"" },
  },

  // 4. PowerShell -Command harmless patterns
  {
    name: "powershell Get-Process",
    pattern: "powershell_command",
    tool: "bash",
    input: { command: "powershell -Command \"Get-Process | Select-Object -First 5\"" },
  },
  {
    name: "powershell Write-Host",
    pattern: "powershell_command",
    tool: "bash",
    input: { command: "powershell -Command \"Write-Host 'hello world'\"" },
  },
  {
    name: "powershell Get-ChildItem",
    pattern: "powershell_command",
    tool: "bash",
    input: { command: "powershell -Command \"Get-ChildItem -Path .\"" },
  },
  {
    name: "powershell scriptblock harmless Write-Host",
    pattern: "powershell_command",
    tool: "bash",
    input: { command: "powershell -Command \"& { Write-Host 'inside scriptblock' }\"" },
  },

  // 5. Invoke-Expression and iex harmless patterns
  {
    name: "powershell iex Get-Date",
    pattern: "invoke_expression",
    tool: "bash",
    input: { command: "powershell -Command \"Invoke-Expression 'Get-Date'\"" },
  },
  {
    name: "powershell iex Write-Output",
    pattern: "invoke_expression",
    tool: "bash",
    input: { command: "powershell -Command \"iex 'Write-Output harmless'\"" },
  },

  // 6. Base64 harmless patterns
  {
    name: "powershell EncodedCommand safe Write-Host",
    pattern: "base64",
    tool: "bash",
    input: { command: "powershell -EncodedCommand V3JpdGUtSG9zdCAiaGVsbG8i" },
  },
  {
    name: "echo base64 decode to stdout",
    pattern: "base64",
    tool: "bash",
    input: { command: "echo aGVsbG8gd29ybGQ= | base64 -d" },
  },
  {
    name: "eval python base64 decode and print",
    pattern: "base64",
    tool: "eval",
    input: { language: "py", code: "import base64\nprint(base64.b64decode('aGVsbG8gd29ybGQ=').decode())" },
  },
  {
    name: "eval js Buffer.from base64 and console.log",
    pattern: "base64",
    tool: "eval",
    input: { language: "js", code: "const msg = Buffer.from('aGVsbG8gd29ybGQ=', 'base64').toString(); console.log(msg);" },
  },
  {
    name: "subshell decoding harmless base64",
    pattern: "base64",
    tool: "bash",
    input: { command: "echo $(echo aGVsbG8= | base64 -d)" },
  },

  // 7. Eval comment and prose harmless patterns
  {
    name: "eval python comment with dangerous command",
    pattern: "eval_comment_prose",
    tool: "eval",
    input: { language: "py", code: "# git push --force origin main\nprint('safe')" },
  },
  {
    name: "eval js block comment with dangerous command",
    pattern: "eval_comment_prose",
    tool: "eval",
    input: { language: "js", code: "/* psql -c 'DROP TABLE users' */ const x = 1;" },
  },
  {
    name: "eval python multiline select query docstring",
    pattern: "eval_comment_prose",
    tool: "eval",
    input: { language: "py", code: "query = \"\"\"SELECT * FROM users WHERE active = true\"\"\"" },
  },
  {
    name: "eval python git push feature branch",
    pattern: "eval_comment_prose",
    tool: "eval",
    input: { language: "py", code: "import subprocess\nsubprocess.run(['git', 'push', 'origin', 'feat/test'])" },
  },
  {
    name: "eval python exec safe print",
    pattern: "eval_comment_prose",
    tool: "eval",
    input: { language: "py", code: "exec(\"print('harmless inside exec')\")" },
  },
  {
    name: "eval js Function return number",
    pattern: "eval_comment_prose",
    tool: "eval",
    input: { language: "js", code: "Function('return 42')()" },
  },
  {
    name: "eval python open package.json",
    pattern: "eval_comment_prose",
    tool: "eval",
    input: { language: "py", code: "open('package.json')" },
  },
  {
    name: "eval js rmSync inside local temp",
    pattern: "eval_comment_prose",
    tool: "eval",
    input: { language: "js", code: "fs.rmSync('temp/cache', { recursive: true })" },
  },
];

interface HardBlockTestCase {
  name: string;
  pattern: "heredoc" | "nested_subshell" | "python_c" | "powershell_command" | "invoke_expression" | "base64" | "eval_dynamic";
  expectedCategory: "production_exclusion" | "destructive_git" | "shared_db_ddl_dml" | "secrets" | "shell_destructive_os";
  tool: "bash" | "eval";
  input: Record<string, unknown>;
}

const HARD_BLOCK_CASES: HardBlockTestCase[] = [
  // 1. Heredocs executing through shells/interpreters
  {
    name: "heredoc bash rm -rf /",
    pattern: "heredoc",
    expectedCategory: "shell_destructive_os",
    tool: "bash",
    input: { command: "bash <<EOF\nrm -rf /\nEOF" },
  },
  {
    name: "heredoc sh quoted delimiter git push force",
    pattern: "heredoc",
    expectedCategory: "destructive_git",
    tool: "bash",
    input: { command: "sh <<'EOF'\ngit push --force origin main\nEOF" },
  },
  {
    name: "heredoc bash psql DROP TABLE",
    pattern: "heredoc",
    expectedCategory: "shared_db_ddl_dml",
    tool: "bash",
    input: { command: "bash <<EOF\npsql -c 'DROP TABLE users'\nEOF" },
  },
  {
    name: "heredoc bash secret id_rsa read",
    pattern: "heredoc",
    expectedCategory: "secrets",
    tool: "bash",
    input: { command: "bash <<EOF\ncat /keys/id_rsa\nEOF" },
  },
  {
    name: "heredoc bash production akamai-iad-prod ssh",
    pattern: "heredoc",
    expectedCategory: "production_exclusion",
    tool: "bash",
    input: { command: "bash <<EOF\nssh akamai-iad-prod\nEOF" },
  },
  {
    name: "heredoc python os.system rm -rf",
    pattern: "heredoc",
    expectedCategory: "shell_destructive_os",
    tool: "bash",
    input: { command: "python <<EOF\nimport os\nos.system('rm -rf C:/')\nEOF" },
  },

  // 2. Nested $(...) and backticks
  {
    name: "nested subshell git force push",
    pattern: "nested_subshell",
    expectedCategory: "destructive_git",
    tool: "bash",
    input: { command: "echo $(git push --force origin main)" },
  },
  {
    name: "double nested subshell rm -rf /",
    pattern: "nested_subshell",
    expectedCategory: "shell_destructive_os",
    tool: "bash",
    input: { command: "echo $(echo $(rm -rf /))" },
  },
  {
    name: "nested subshell cat secret id_rsa",
    pattern: "nested_subshell",
    expectedCategory: "secrets",
    tool: "bash",
    input: { command: "echo $(cat /keys/id_rsa)" },
  },
  {
    name: "nested subshell psql DROP TABLE",
    pattern: "nested_subshell",
    expectedCategory: "shared_db_ddl_dml",
    tool: "bash",
    input: { command: "echo $(psql -c 'DROP TABLE users')" },
  },
  {
    name: "nested subshell production host akamai-iad-prod",
    pattern: "nested_subshell",
    expectedCategory: "production_exclusion",
    tool: "bash",
    input: { command: "echo $(ssh akamai-iad-prod)" },
  },
  {
    name: "nested subshell production id zaraprptkegxqpvnsubu",
    pattern: "nested_subshell",
    expectedCategory: "production_exclusion",
    tool: "bash",
    input: { command: "echo $(psql postgresql://zaraprptkegxqpvnsubu)" },
  },

  // 3. Python -c with embedded shell
  {
    name: "python -c os.system git force push",
    pattern: "python_c",
    expectedCategory: "destructive_git",
    tool: "bash",
    input: { command: "python -c \"import os; os.system('git push --force origin main')\"" },
  },
  {
    name: "python -c shutil.rmtree root",
    pattern: "python_c",
    expectedCategory: "shell_destructive_os",
    tool: "bash",
    input: { command: "python -c \"import shutil; shutil.rmtree('/')\"" },
  },
  {
    name: "python -c psql DROP TABLE",
    pattern: "python_c",
    expectedCategory: "shared_db_ddl_dml",
    tool: "bash",
    input: { command: "python -c \"import os; os.system('psql -c \\\"DROP TABLE users\\\"')\"" },
  },
  {
    name: "python -c open secret id_rsa",
    pattern: "python_c",
    expectedCategory: "secrets",
    tool: "bash",
    input: { command: "python -c \"open('/keys/id_rsa')\"" },
  },
  {
    name: "python -c ssh production akamai-iad-prod",
    pattern: "python_c",
    expectedCategory: "production_exclusion",
    tool: "bash",
    input: { command: "python3 -c \"import os; os.system('ssh akamai-iad-prod')\"" },
  },
  {
    name: "python -c subprocess.run git delete branch",
    pattern: "python_c",
    expectedCategory: "destructive_git",
    tool: "bash",
    input: { command: "python -c \"import subprocess; subprocess.run(['git', 'push', '--delete', 'origin', 'main'])\"" },
  },

  // 4. PowerShell -Command blocks
  {
    name: "powershell -Command git push force main",
    pattern: "powershell_command",
    expectedCategory: "destructive_git",
    tool: "bash",
    input: { command: "powershell -Command \"git push --force origin main\"" },
  },
  {
    name: "powershell -Command Remove-Item root recurse",
    pattern: "powershell_command",
    expectedCategory: "shell_destructive_os",
    tool: "bash",
    input: { command: "powershell -Command \"Remove-Item -Recurse -Force C:\\\"" },
  },
  {
    name: "powershell -Command psql DROP TABLE",
    pattern: "powershell_command",
    expectedCategory: "shared_db_ddl_dml",
    tool: "bash",
    input: { command: "powershell -Command \"psql -c 'DROP TABLE users'\"" },
  },
  {
    name: "powershell -Command scriptblock call git push force",
    pattern: "powershell_command",
    expectedCategory: "destructive_git",
    tool: "bash",
    input: { command: "powershell -Command \"& { git push --force origin main }\"" },
  },
  {
    name: "powershell -Command bare scriptblock Remove-Item root",
    pattern: "powershell_command",
    expectedCategory: "shell_destructive_os",
    tool: "bash",
    input: { command: "powershell -Command \"{ Remove-Item -Recurse -Force / }\"" },
  },
  {
    name: "powershell -Command read secret service_role",
    pattern: "powershell_command",
    expectedCategory: "secrets",
    tool: "bash",
    input: { command: "powershell -Command \"cat /keys/service_role\"" },
  },

  // 5. Invoke-Expression and iex
  {
    name: "powershell Invoke-Expression git push force",
    pattern: "invoke_expression",
    expectedCategory: "destructive_git",
    tool: "bash",
    input: { command: "powershell -Command \"Invoke-Expression 'git push --force origin main'\"" },
  },
  {
    name: "powershell iex Remove-Item recurse root",
    pattern: "invoke_expression",
    expectedCategory: "shell_destructive_os",
    tool: "bash",
    input: { command: "powershell -Command \"iex 'Remove-Item -Recurse -Force C:\\'\"" },
  },
  {
    name: "powershell Invoke-Expression psql DROP TABLE",
    pattern: "invoke_expression",
    expectedCategory: "shared_db_ddl_dml",
    tool: "bash",
    input: { command: "powershell -Command \"Invoke-Expression 'psql -c \\\"DROP TABLE users\\\"'\"" },
  },
  {
    name: "powershell iex read secret jwt_secret",
    pattern: "invoke_expression",
    expectedCategory: "secrets",
    tool: "bash",
    input: { command: "powershell -Command \"iex 'cat /keys/jwt_secret'\"" },
  },
  {
    name: "powershell Invoke-Expression ssh production akamai-iad-prod",
    pattern: "invoke_expression",
    expectedCategory: "production_exclusion",
    tool: "bash",
    input: { command: "powershell -Command \"Invoke-Expression 'ssh akamai-iad-prod'\"" },
  },

  // 6. Base64 payloads
  {
    name: "eval python base64 os.system git push force",
    pattern: "base64",
    expectedCategory: "destructive_git",
    tool: "eval",
    input: { language: "py", code: "import base64, os\nos.system(base64.b64decode('Z2l0IHB1c2ggLS1mb3JjZSBvcmlnaW4gbWFpbg==').decode())" },
  },
  {
    name: "eval python base64 subprocess.run rm -rf /",
    pattern: "base64",
    expectedCategory: "shell_destructive_os",
    tool: "eval",
    input: { language: "py", code: "import base64, subprocess\nsubprocess.run(base64.b64decode('cm0gLXJmIC8=').decode(), shell=True)" },
  },
  {
    name: "eval python base64 open secret id_rsa",
    pattern: "base64",
    expectedCategory: "secrets",
    tool: "eval",
    input: { language: "py", code: "open(base64.b64decode('L2tleXMvaWRfcnNh').decode())" },
  },
  {
    name: "eval python base64 ssh production akamai-iad-prod",
    pattern: "base64",
    expectedCategory: "production_exclusion",
    tool: "eval",
    input: { language: "py", code: "import base64, os\nos.system(base64.b64decode('c3NoIGFrYW1haS1pYWQtcHJvZA==').decode())" },
  },
  {
    name: "eval js Buffer base64 psql DROP TABLE",
    pattern: "base64",
    expectedCategory: "shared_db_ddl_dml",
    tool: "eval",
    input: { language: "js", code: "const cmd = Buffer.from('cHNxbCAtYyAnRFJPUCBUQUJMRSB1c2Vycyc=', 'base64').toString(); child_process.execSync(cmd);" },
  },
  {
    name: "eval js atob rm -rf C:/",
    pattern: "base64",
    expectedCategory: "shell_destructive_os",
    tool: "eval",
    input: { language: "js", code: "const cmd = atob('cm0gLXJmIEM6Lw=='); Bun.spawn(cmd.split(' '));" },
  },
  {
    name: "eval js fs.readFileSync base64 secret .env.prod",
    pattern: "base64",
    expectedCategory: "secrets",
    tool: "eval",
    input: { language: "js", code: "fs.readFileSync(Buffer.from('L2FwcC8uZW52LnByb2Q=', 'base64').toString());" },
  },
  {
    name: "eval js production id zaraprptkegxqpvnsubu base64",
    pattern: "base64",
    expectedCategory: "production_exclusion",
    tool: "eval",
    input: { language: "js", code: "const cmd = Buffer.from('cHNxbCBwb3N0Z3Jlc3FsOi8vemFyYXBycHRrZWd4cXB2bnN1YnU=', 'base64').toString(); child_process.execSync(cmd);" },
  },
  {
    name: "powershell -EncodedCommand UTF-16LE git push force",
    pattern: "base64",
    expectedCategory: "destructive_git",
    tool: "bash",
    input: { command: "powershell -EncodedCommand ZwBpAHQAIABwAHUAcwBoACAALQAtAGYAbwByAGMAZQAgAG8AcgBpAGcAaQBuACAAbQBhAGkAbgA=" },
  },
  {
    name: "powershell -enc ASCII base64 git push force",
    pattern: "base64",
    expectedCategory: "destructive_git",
    tool: "bash",
    input: { command: "pwsh -enc Z2l0IHB1c2ggLS1mb3JjZSBvcmlnaW4gbWFpbg==" },
  },
  {
    name: "pipeline echo base64 decode piped to sh git push force",
    pattern: "base64",
    expectedCategory: "destructive_git",
    tool: "bash",
    input: { command: "echo Z2l0IHB1c2ggLS1mb3JjZSBvcmlnaW4gbWFpbg== | base64 -d | sh" },
  },
  {
    name: "pipeline echo base64 decode piped to bash rm -rf /",
    pattern: "base64",
    expectedCategory: "shell_destructive_os",
    tool: "bash",
    input: { command: "echo cm0gLXJmIC8= | base64 --decode | bash" },
  },
  {
    name: "subshell echo base64 in sh -c git push force",
    pattern: "base64",
    expectedCategory: "base64" === "base64" ? "destructive_git" : "destructive_git",
    tool: "bash",
    input: { command: "sh -c \"$(echo Z2l0IHB1c2ggLS1mb3JjZSBvcmlnaW4gbWFpbg== | base64 -d)\"" },
  },

  // 7. Eval dynamic execution constructs
  {
    name: "eval python exec() code string with git push force",
    pattern: "eval_dynamic",
    expectedCategory: "destructive_git",
    tool: "eval",
    input: { language: "py", code: "exec(\"import os; os.system('git push --force origin main')\")" },
  },
  {
    name: "eval python exec() with base64 decoded payload rm -rf /",
    pattern: "eval_dynamic",
    expectedCategory: "shell_destructive_os",
    tool: "eval",
    input: { language: "py", code: "import base64\nexec(base64.b64decode('aW1wb3J0IG9zOyBvcy5zeXN0ZW0oJ3JtIC1yZiAvJyk=').decode())" },
  },
  {
    name: "eval js Function() constructor with rm -rf /",
    pattern: "eval_dynamic",
    expectedCategory: "shell_destructive_os",
    tool: "eval",
    input: { language: "js", code: "Function(\"child_process.execSync('rm -rf /')\")()" },
  },
  {
    name: "eval python __import__('os').system git push force",
    pattern: "eval_dynamic",
    expectedCategory: "destructive_git",
    tool: "eval",
    input: { language: "py", code: "__import__('os').system('git push --force origin main')" },
  },
];

describe("Issue #97: Table-driven audit of guard-eval.ts and guard.ts", () => {
  test(`Audit inventory counts: ${HARMLESS_CASES.length} harmless + ${HARD_BLOCK_CASES.length} hard-block = ${HARMLESS_CASES.length + HARD_BLOCK_CASES.length} cases (>=30 required)`, () => {
    expect(HARMLESS_CASES.length).toBeGreaterThanOrEqual(20);
    expect(HARD_BLOCK_CASES.length).toBeGreaterThanOrEqual(25);
    expect(HARMLESS_CASES.length + HARD_BLOCK_CASES.length).toBeGreaterThanOrEqual(30);
  });

  describe("Part 1: Zero false-blocks of harmless developer commands", () => {
    for (const testCase of HARMLESS_CASES) {
      test(`[${testCase.pattern}] ${testCase.name} -> allowed: true`, () => {
        const result = guard.evaluateToolCall(testCase.tool, testCase.input);
        expect(result.allowed).toBe(true);
        expect(result.category).toBeUndefined();
      });
    }
  });

  describe("Part 2: Zero misses of the hard-block set across complex eval patterns", () => {
    for (const testCase of HARD_BLOCK_CASES) {
      test(`[${testCase.pattern}] ${testCase.name} -> category: ${testCase.expectedCategory}`, () => {
        const result = guard.evaluateToolCall(testCase.tool, testCase.input);
        expect(result.allowed).toBe(false);
        expect(result.category).toBe(testCase.expectedCategory);
      });
    }
  });
});
