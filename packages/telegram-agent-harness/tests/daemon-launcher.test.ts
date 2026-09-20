import { test, expect, describe, afterEach } from "bun:test";
import type { Subprocess } from "bun";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

describe("veyyon-telegram-daemon.ps1 launcher liveness and stale pid handling", () => {
  const cleanupProcs: Array<{ kill: () => void; pid?: number }> = [];
  const cleanupDirs: string[] = [];

  afterEach(() => {
    for (const proc of cleanupProcs) {
      try {
        proc.kill();
      } catch {}
      if (proc.pid) {
        try {
          process.kill(proc.pid, "SIGKILL");
        } catch {}
      }
    }
    cleanupProcs.length = 0;

    for (const dir of cleanupDirs) {
      try {
        fs.rmSync(dir, { recursive: true, force: true });
      } catch {}
    }
    cleanupDirs.length = 0;
  });

  function setupTestHarness(): {
    tempDir: string;
    launcherPath: string;
    daemonEntry: string;
    runDir: string;
    pidPath: string;
  } {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-launcher-test-"));
    cleanupDirs.push(tempDir);

    const daemonDir = path.join(tempDir, "daemon");
    const runDir = path.join(daemonDir, "run");
    fs.mkdirSync(runDir, { recursive: true });

    // Copy the launcher script to tempDir
    const sourceLauncher = path.resolve(
      import.meta.dir,
      "../daemon/veyyon-telegram-daemon.ps1"
    );
    const launcherPath = path.join(tempDir, "veyyon-telegram-daemon.ps1");
    fs.copyFileSync(sourceLauncher, launcherPath);

    // Create a mock main.ts that responds to daemon verbs
    const daemonEntry = path.join(daemonDir, "main.ts");
    fs.writeFileSync(
      daemonEntry,
      `
import * as fs from "node:fs";
import * as path from "node:path";

const verb = process.argv[2] ?? "run";
if (verb === "status") {
  const pidFile = path.join(import.meta.dir, "run", "daemon.pid");
  const pid = fs.existsSync(pidFile) ? fs.readFileSync(pidFile, "utf8").trim() : "";
  console.log("daemon: running (pid " + pid + ")");
  process.exit(0);
}
if (verb === "stop") {
  const pidFile = path.join(import.meta.dir, "run", "daemon.pid");
  if (fs.existsSync(pidFile)) {
    const pid = Number.parseInt(fs.readFileSync(pidFile, "utf8").trim(), 10);
    if (pid > 0) {
      try { process.kill(pid, "SIGTERM"); } catch {}
    }
  }
  console.log("Sent SIGTERM to Telegram daemon PID " + process.pid);
  process.exit(0);
}
if (verb === "run") {
  process.on("SIGTERM", () => {
    process.exit(0);
  });
  process.stdout.write("DAEMON_READY\\n");
  const { promise } = Promise.withResolvers<void>();
  await promise;
}
`,
      "utf8"
    );

    const pidPath = path.join(runDir, "daemon.pid");
    return { tempDir, launcherPath, daemonEntry, runDir, pidPath };
  }

  async function spawnFakeDaemon(
    daemonEntry: string
  ): Promise<{ proc: Subprocess; pid: number }> {
    const proc = Bun.spawn(["bun", daemonEntry, "run"], {
      stdout: "pipe",
      stderr: "pipe",
    });
    cleanupProcs.push(proc);

    // Deterministically wait for DAEMON_READY line
    const reader = (proc.stdout as ReadableStream<Uint8Array>).getReader();
    const decoder = new TextDecoder();
    let accumulated = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      accumulated += decoder.decode(value);
      if (accumulated.includes("DAEMON_READY")) break;
    }
    reader.releaseLock();

    return { proc, pid: proc.pid };
  }

  async function spawnUnrelatedProcess(): Promise<{
    proc: Subprocess;
    pid: number;
  }> {
    const proc = Bun.spawn(
      [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        "[Console]::WriteLine('UNRELATED_READY'); $null = [Console]::ReadLine()",
      ],
      {
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
      }
    );
    cleanupProcs.push(proc);

    const reader = (proc.stdout as ReadableStream<Uint8Array>).getReader();
    const decoder = new TextDecoder();
    let accumulated = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      accumulated += decoder.decode(value);
      if (accumulated.includes("UNRELATED_READY")) break;
    }
    reader.releaseLock();

    return { proc, pid: proc.pid };
  }

  function runLauncher(
    launcherPath: string,
    verb: string,
    extraEnv: Record<string, string> = {}
  ): { code: number; stdout: string; stderr: string } {
    const proc = Bun.spawnSync(
      [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        launcherPath,
        verb,
      ],
      {
        cwd: path.dirname(launcherPath),
        env: {
          ...process.env,
          ...extraEnv,
        },
      }
    );
    return {
      code: proc.exitCode,
      stdout: proc.stdout.toString("utf8"),
      stderr: proc.stderr.toString("utf8"),
    };
  }

  test("deletes stale pid file when process does not exist (dead PID)", () => {
    const { launcherPath, pidPath } = setupTestHarness();

    // Write a definitely non-existent PID
    fs.writeFileSync(pidPath, "999999", "utf8");
    expect(fs.existsSync(pidPath)).toBe(true);

    const res = runLauncher(launcherPath, "status");
    // Should log deletion of stale pid file
    expect(res.stdout).toContain("Deleting stale PID file");
    expect(res.stdout).toContain("999999");
    // Stale pid file must be deleted
    expect(fs.existsSync(pidPath)).toBe(false);
  }, 20_000);

  test("deletes stale pid file when process exists but command line does not match daemon entry", async () => {
    const { launcherPath, pidPath } = setupTestHarness();

    // Spawn an unrelated process (powershell reading line)
    const { pid: unrelatedPid } = await spawnUnrelatedProcess();
    expect(unrelatedPid).toBeGreaterThan(0);

    // Put unrelated PID into daemon.pid
    fs.writeFileSync(pidPath, String(unrelatedPid), "utf8");
    expect(fs.existsSync(pidPath)).toBe(true);

    const res = runLauncher(launcherPath, "status");
    expect(res.stdout).toContain("Deleting stale PID file");
    expect(res.stdout).toContain(String(unrelatedPid));
    // The stale file should be removed
    expect(fs.existsSync(pidPath)).toBe(false);

    // The unrelated process must NOT have been killed
    let procAlive = true;
    try {
      process.kill(unrelatedPid, 0);
    } catch {
      procAlive = false;
    }
    expect(procAlive).toBe(true);
  }, 20_000);

  test("falls back to process scan when pid file is stale and live daemon process is running", async () => {
    const { launcherPath, daemonEntry, pidPath } = setupTestHarness();

    // Spawn fake daemon process using bun with daemonEntry in command line
    const { pid: daemonPid } = await spawnFakeDaemon(daemonEntry);
    expect(daemonPid).toBeGreaterThan(0);

    // Put a stale PID in daemon.pid
    fs.writeFileSync(pidPath, "999998", "utf8");

    const res = runLauncher(launcherPath, "status");
    // Should log stale pid deletion
    expect(res.stdout).toContain("Deleting stale PID file");
    expect(res.stdout).toContain("999998");

    // Fallback process scan must recover the live daemon PID and update pid file
    expect(fs.existsSync(pidPath)).toBe(true);
    const recoveredPid = Number.parseInt(
      fs.readFileSync(pidPath, "utf8").trim(),
      10
    );
    expect(recoveredPid).toBe(daemonPid);
    expect(res.stdout).toContain(`pid ${daemonPid}`);
  }, 20_000);

  test("start refuses if a live daemon is found and prints its PID", async () => {
    const { launcherPath, daemonEntry, pidPath } = setupTestHarness();

    // Spawn fake daemon process
    const { pid: daemonPid } = await spawnFakeDaemon(daemonEntry);

    // Even if pid file is missing or stale, start must find the live process
    if (fs.existsSync(pidPath)) {
      fs.unlinkSync(pidPath);
    }

    const res = runLauncher(launcherPath, "start");
    // Start must refuse and print its pid
    expect(res.stdout).toContain(
      `Telegram daemon already running (pid ${daemonPid})`
    );
    expect(res.code).toBe(1);
  }, 20_000);

  test("stop waits for exit and cleans up pid files", async () => {
    const { launcherPath, daemonEntry, pidPath } = setupTestHarness();

    // Spawn fake daemon process
    const { proc, pid: daemonPid } = await spawnFakeDaemon(daemonEntry);

    fs.writeFileSync(pidPath, String(daemonPid), "utf8");

    const res = runLauncher(launcherPath, "stop");
    expect(res.code).toBe(0);

    // Wait for the fake daemon process to exit deterministically
    await proc.exited;

    // Process should be stopped
    let procAlive = true;
    try {
      process.kill(daemonPid, 0);
    } catch {
      procAlive = false;
    }
    expect(procAlive).toBe(false);

    // Pid file should be cleaned up
    expect(fs.existsSync(pidPath)).toBe(false);
  }, 20_000);
  test("never adopts powershell wrapper as daemon even if wrapper command line contains daemon entry", async () => {
    const { launcherPath, daemonEntry, pidPath } = setupTestHarness();

    // Spawn a PowerShell process whose command line contains daemonEntry (simulating launcher wrapper)
    const wrapperProc = Bun.spawn(
      [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        `[Console]::WriteLine('WRAPPER_READY'); $null = [Console]::ReadLine() # ${daemonEntry}`,
      ],
      {
        stdin: "pipe",
        stdout: "pipe",
        stderr: "pipe",
      }
    );
    cleanupProcs.push(wrapperProc);

    const reader = (wrapperProc.stdout as ReadableStream<Uint8Array>).getReader();
    const decoder = new TextDecoder();
    let accumulated = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      accumulated += decoder.decode(value);
      if (accumulated.includes("WRAPPER_READY")) break;
    }
    reader.releaseLock();

    const wrapperPid = wrapperProc.pid;
    expect(wrapperPid).toBeGreaterThan(0);

    // Put wrapper PID in daemon.pid
    fs.writeFileSync(pidPath, String(wrapperPid), "utf8");

    // Run status: it must mark the wrapper as stale and delete daemon.pid, and NOT adopt wrapper
    const res = runLauncher(launcherPath, "status");
    expect(res.stdout).toContain("Deleting stale PID file");
    expect(res.stdout).toContain(String(wrapperPid));
    expect(fs.existsSync(pidPath)).toBe(false);

    // Wrapper process must NOT be killed
    let procAlive = true;
    try {
      process.kill(wrapperPid, 0);
    } catch {
      procAlive = false;
    }
    expect(procAlive).toBe(true);
  }, 20_000);
});
