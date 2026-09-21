import { test, expect, describe, afterEach } from "bun:test";
import type { Subprocess } from "bun";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";

describe("veyyon-gui-host.ps1 launcher liveness, port checks, and stale pid handling", () => {
  const cleanupProcs: Array<{ kill: () => void; pid?: number }> = [];
  const cleanupDirs: string[] = [];
  const cleanupServers: Array<{ close: () => void }> = [];

  afterEach(async () => {
    for (const server of cleanupServers) {
      try {
        server.close();
      } catch {}
    }
    cleanupServers.length = 0;

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
    daemonLauncherPath: string;
    runDir: string;
    pidPath: string;
  } {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), "gui-host-test-"));
    cleanupDirs.push(tempDir);

    const daemonDir = path.join(tempDir, "daemon");
    const runDir = path.join(daemonDir, "run");
    fs.mkdirSync(runDir, { recursive: true });

    const sourceGuiLauncher = path.resolve(
      import.meta.dir,
      "../daemon/veyyon-gui-host.ps1"
    );
    const launcherPath = path.join(tempDir, "veyyon-gui-host.ps1");
    fs.copyFileSync(sourceGuiLauncher, launcherPath);

    const sourceDaemonLauncher = path.resolve(
      import.meta.dir,
      "../daemon/veyyon-telegram-daemon.ps1"
    );
    const daemonLauncherPath = path.join(tempDir, "veyyon-telegram-daemon.ps1");
    fs.copyFileSync(sourceDaemonLauncher, daemonLauncherPath);

    const pidPath = path.join(runDir, "gui-host.pid");
    return { tempDir, launcherPath, daemonLauncherPath, runDir, pidPath };
  }

  async function spawnFakeGuiHost(
    endpoint: string = "tcp:127.0.0.1:7699"
  ): Promise<{ proc: Subprocess; pid: number }> {
    const tempScript = path.join(os.tmpdir(), `fake-gui-host-${Date.now()}-${Math.random().toString(36).slice(2)}.ts`);
    fs.writeFileSync(
      tempScript,
      "process.stdout.write('GUI_HOST_READY\\n'); const { promise } = Promise.withResolvers(); await promise;",
      "utf8"
    );

    const proc = Bun.spawn(["bun", tempScript, "gui", endpoint], {
      stdout: "pipe",
      stderr: "pipe",
    });
    cleanupProcs.push(proc);

    const reader = (proc.stdout as ReadableStream<Uint8Array>).getReader();
    const decoder = new TextDecoder();
    let accumulated = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      accumulated += decoder.decode(value);
      if (accumulated.includes("GUI_HOST_READY")) break;
    }
    reader.releaseLock();

    try { fs.unlinkSync(tempScript); } catch {}
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

  function startListeningPort(port: number = 0): Promise<{ server: net.Server; port: number }> {
    const { promise, resolve, reject } = Promise.withResolvers<{ server: net.Server; port: number }>();
    const server = net.createServer();
    cleanupServers.push(server);
    server.listen(port, "127.0.0.1", () => {
      const assignedPort = (server.address() as net.AddressInfo).port;
      resolve({ server, port: assignedPort });
    });
    server.on("error", reject);
    return promise;
  }

  function runPsScript(
    scriptPath: string,
    args: string[] = [],
    extraEnv: Record<string, string> = {}
  ): { code: number; stdout: string; stderr: string } {
    const proc = Bun.spawnSync(
      [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        scriptPath,
        ...args,
      ],
      {
        cwd: path.dirname(scriptPath),
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

    fs.writeFileSync(pidPath, "999999", "utf8");
    expect(fs.existsSync(pidPath)).toBe(true);

    const res = runPsScript(launcherPath, ["status"]);
    expect(res.stdout).toContain("Deleting stale PID file");
    expect(res.stdout).toContain("999999");
    expect(fs.existsSync(pidPath)).toBe(false);
  }, 20_000);

  test("deletes stale pid file when process exists but command line does not match gui host entry", async () => {
    const { launcherPath, pidPath } = setupTestHarness();

    const { pid: unrelatedPid } = await spawnUnrelatedProcess();
    expect(unrelatedPid).toBeGreaterThan(0);

    fs.writeFileSync(pidPath, String(unrelatedPid), "utf8");
    expect(fs.existsSync(pidPath)).toBe(true);

    const res = runPsScript(launcherPath, ["status"]);
    expect(res.stdout).toContain("Deleting stale PID file");
    expect(res.stdout).toContain(String(unrelatedPid));
    expect(fs.existsSync(pidPath)).toBe(false);

    let procAlive = true;
    try {
      process.kill(unrelatedPid, 0);
    } catch {
      procAlive = false;
    }
    expect(procAlive).toBe(true);
  }, 20_000);

  test("status reports not running when process is dead and port closed", () => {
    const { launcherPath } = setupTestHarness();

    const res = runPsScript(launcherPath, ["status", "-Endpoint", "tcp:127.0.0.1:49152"]);
    expect(res.code).toBe(1);
    expect(res.stdout).toContain("GUI host: not running");
  }, 20_000);

  test("status reports running when matching process and open port exist", async () => {
    const { launcherPath, pidPath } = setupTestHarness();
    const { port: testPort } = await startListeningPort(0);
    const endpoint = `tcp:127.0.0.1:${testPort}`;

    const { pid: hostPid } = await spawnFakeGuiHost(endpoint);
    fs.writeFileSync(pidPath, String(hostPid), "utf8");

    const res = runPsScript(launcherPath, ["status", "-Endpoint", endpoint]);
    expect(res.code).toBe(0);
    expect(res.stdout).toContain(`GUI host: running (pid ${hostPid}, port ${testPort} listening)`);
  }, 20_000);

  test("falls back to process scan when pid file is absent but live process is running", async () => {
    const { launcherPath } = setupTestHarness();
    const { port: testPort } = await startListeningPort(0);
    const endpoint = `tcp:127.0.0.1:${testPort}`;

    const { pid: hostPid } = await spawnFakeGuiHost(endpoint);

    const res = runPsScript(launcherPath, ["status", "-Endpoint", endpoint]);
    expect(res.code).toBe(0);
    expect(res.stdout).toContain(`GUI host: running (pid ${hostPid}, port ${testPort} listening)`);
  }, 20_000);

  test("start refuses / reports already running if live process and port are active", async () => {
    const { launcherPath } = setupTestHarness();
    const { port: testPort } = await startListeningPort(0);
    const endpoint = `tcp:127.0.0.1:${testPort}`;

    const { pid: hostPid } = await spawnFakeGuiHost(endpoint);

    const res = runPsScript(launcherPath, ["start", "-Endpoint", endpoint]);
    expect(res.code).toBe(0);
    expect(res.stdout).toContain(`GUI host already running (pid ${hostPid}, port ${testPort} listening)`);
  }, 20_000);

  test("stop does not kill unmanaged process when pid file is absent", async () => {
    const { launcherPath } = setupTestHarness();
    const testPort = 49156;
    const endpoint = `tcp:127.0.0.1:${testPort}`;

    const { pid: hostPid } = await spawnFakeGuiHost(endpoint);

    const res = runPsScript(launcherPath, ["stop", "-Endpoint", endpoint]);
    expect(res.code).toBe(0);
    expect(res.stdout).toContain("unmanaged by this launcher");

    let procAlive = true;
    try {
      process.kill(hostPid, 0);
    } catch {
      procAlive = false;
    }
    expect(procAlive).toBe(true);
  }, 20_000);

  test("stop terminates managed process recorded in pid file and cleans up pid file", async () => {
    const { launcherPath, pidPath } = setupTestHarness();
    const testPort = 49157;
    const endpoint = `tcp:127.0.0.1:${testPort}`;

    const { proc, pid: hostPid } = await spawnFakeGuiHost(endpoint);
    fs.writeFileSync(pidPath, String(hostPid), "utf8");

    const res = runPsScript(launcherPath, ["stop", "-Endpoint", endpoint]);
    expect(res.code).toBe(0);
    expect(res.stdout).toContain("GUI host stopped");

    await proc.exited;
    let procAlive = true;
    try {
      process.kill(hostPid, 0);
    } catch {
      procAlive = false;
    }
    expect(procAlive).toBe(false);
    expect(fs.existsSync(pidPath)).toBe(false);
  }, 20_000);

  test("daemon launcher waits for port and logs clearly when absent", async () => {
    const { daemonLauncherPath, tempDir } = setupTestHarness();
    const unusedPort = 49158;

    // Create a mock daemon/main.ts so start can proceed if it wants to
    const daemonDir = path.join(tempDir, "daemon");
    fs.writeFileSync(
      path.join(daemonDir, "main.ts"),
      "console.log('DAEMON_MOCK'); process.exit(0);",
      "utf8"
    );

    const res = runPsScript(
      daemonLauncherPath,
      ["start", "-Endpoint", `tcp:127.0.0.1:${unusedPort}`],
      { VEYYON_GUI_HOST_PORT_WAIT_TIMEOUT: "1" }
    );

    expect(res.stdout).toContain(`GUI host port ${unusedPort} is absent after waiting 1 s.`);
  }, 20_000);

  test("daemon launcher detects open port immediately and logs ready", async () => {
    const { daemonLauncherPath, tempDir } = setupTestHarness();
    const { port: readyPort } = await startListeningPort(0);

    // Create a mock daemon/main.ts that stays running
    const daemonDir = path.join(tempDir, "daemon");
    fs.writeFileSync(
      path.join(daemonDir, "main.ts"),
      `import * as fs from "node:fs";
import * as path from "node:path";
const verb = process.argv[2] ?? "run";
if (verb === "stop") {
  const pidFile = path.join(import.meta.dir, "run", "daemon.pid");
  if (fs.existsSync(pidFile)) {
    const pid = Number.parseInt(fs.readFileSync(pidFile, "utf8").trim(), 10);
    if (pid > 0) {
      try { process.kill(pid, "SIGTERM"); } catch {}
    }
  }
  process.exit(0);
}
if (verb === "run") {
  process.on("SIGTERM", () => { process.exit(0); });
  const pidFile = path.join(import.meta.dir, "run", "daemon.pid");
  fs.mkdirSync(path.dirname(pidFile), { recursive: true });
  fs.writeFileSync(pidFile, String(process.pid), "utf8");
  process.stdout.write("DAEMON_READY\\n");
  const { promise } = Promise.withResolvers();
  await promise;
}`,
    );

    const res = runPsScript(
      daemonLauncherPath,
      ["start", "-Endpoint", `tcp:127.0.0.1:${readyPort}`, "-StartTimeoutSeconds", "5"],
      { VEYYON_GUI_HOST_PORT_WAIT_TIMEOUT: "5" }
    );

    expect(res.stdout).toContain(`GUI host port ${readyPort} is ready.`);

    // Cleanup daemon
    runPsScript(daemonLauncherPath, ["stop"]);
  }, 20_000);
});
