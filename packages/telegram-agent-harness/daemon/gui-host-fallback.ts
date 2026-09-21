/**
 * gui-host-fallback.ts — Automatic recovery and chat notifications on GUI host failure.
 *
 * When a request to the Veyyon GUI host fails with ECONNREFUSED (for example,
 * after a machine reboot where the daemon auto-started before the GUI host),
 * this manager:
 * 1. Alerts the operator's chat once per 10 minutes: "Veyyon host is down, restarting it…"
 * 2. Launches the GUI host via `veyyon-gui-host.ps1 start` (bounded, no tight loop).
 * 3. Waits for the host's TCP port to be listening.
 * 4. Cleans up stale sockets and retries the request once.
 */

import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { isConnectionRefusedError, type GuiHostSessionControl } from "./session-control";

export interface GuiHostFallbackOptions {
  control: GuiHostSessionControl;
  endpoint?: string | null;
  log?: (message: string) => void;
  /** Injected so tests drive a fake restart without invoking PowerShell. */
  restartHost?: () => Promise<void>;
  /** Injected port checker for tests. */
  isPortListening?: (host: string, port: number) => Promise<boolean>;
  /** Timeout in ms to wait for the host port to open; defaults to 15_000. */
  portWaitTimeoutMs?: number;
  /** Cooldown between restart attempts in ms; defaults to 5_000. */
  restartCooldownMs?: number;
  /** Rate limit per chat for down notifications in ms; defaults to 600_000 (10 min). */
  chatNoticeCooldownMs?: number;
  /** Callback invoked when GUI host recovery succeeds after an ECONNREFUSED. */
  onHostRecovered?: () => void;
}

export class GuiHostFallbackManager {
  private readonly lastNoticeTimes = new Map<string, number>();
  private activeRestart: Promise<boolean> | null = null;
  private lastRestartAttempt = 0;

  constructor(private readonly options: GuiHostFallbackOptions) {}

  public async withFallback<T>(
    chatId: string,
    replyFn: (text: string) => Promise<void>,
    operation: () => Promise<T>,
  ): Promise<T> {
    try {
      return await operation();
    } catch (error) {
      if (!isConnectionRefusedError(error)) {
        throw error;
      }

      await this.notifyChatIfDue(chatId, replyFn);
      const portReady = await this.ensureHostStarted();
      if (!portReady) {
        throw error;
      }

      this.options.control.close();
      const result = await operation();
      this.options.onHostRecovered?.();
      return result;
    }
  }

  private async notifyChatIfDue(chatId: string, replyFn: (text: string) => Promise<void>): Promise<void> {
    const now = Date.now();
    const cooldown = this.options.chatNoticeCooldownMs ?? 600_000;
    const last = this.lastNoticeTimes.get(chatId) ?? 0;
    if (now - last >= cooldown) {
      this.lastNoticeTimes.set(chatId, now);
      try {
        await replyFn("Veyyon host is down, restarting it…");
      } catch (err) {
        this.options.log?.(`Failed to send host restart notification to chat ${chatId}: ${err}`);
      }
    }
  }

  public async ensureHostStarted(): Promise<boolean> {
    if (this.activeRestart) {
      return this.activeRestart;
    }

    const now = Date.now();
    const cooldown = this.options.restartCooldownMs ?? 5_000;
    if (now - this.lastRestartAttempt < cooldown) {
      const { host, port } = this.resolveHostAndPort();
      return this.checkPort(host, port);
    }

    this.lastRestartAttempt = now;
    this.activeRestart = this.performRestart().finally(() => {
      this.activeRestart = null;
    });

    return this.activeRestart;
  }

  private async performRestart(): Promise<boolean> {
    const { host, port } = this.resolveHostAndPort();

    // If port is already up, no need to restart
    if (await this.checkPort(host, port)) {
      return true;
    }

    this.options.log?.(`GUI host port ${port} is closed; running GUI host start...`);

    if (this.options.restartHost) {
      try {
        await this.options.restartHost();
      } catch (err) {
        this.options.log?.(`Injected restartHost failed: ${err}`);
      }
    } else {
      const launcher = resolveGuiHostLauncher();
      if (!launcher) {
        this.options.log?.("Cannot restart GUI host: veyyon-gui-host.ps1 not found.");
        return false;
      }

      try {
        const proc = Bun.spawn(
          [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            launcher,
            "start",
            "-Endpoint",
            this.options.endpoint ?? `tcp:${host}:${port}`,
          ],
          {
            stdout: "pipe",
            stderr: "pipe",
          }
        );
        await proc.exited;
      } catch (err) {
        this.options.log?.(`Failed to execute ${launcher} start: ${err}`);
      }
    }

    // Wait for the port to become available
    const timeout = this.options.portWaitTimeoutMs ?? 15_000;
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (await this.checkPort(host, port)) {
        this.options.log?.(`GUI host port ${port} is ready.`);
        return true;
      }
      await sleep(250);
    }

    this.options.log?.(`GUI host port ${port} did not become available within ${timeout} ms.`);
    return false;
  }

  private resolveHostAndPort(): { host: string; port: number } {
    const ep = this.options.endpoint ?? this.options.control.endpoint ?? "tcp:127.0.0.1:7699";
    const match = /^tcp:([^:]+):(\d+)$/.exec(ep);
    if (match) {
      return { host: match[1], port: Number.parseInt(match[2], 10) };
    }
    return { host: "127.0.0.1", port: 7699 };
  }

  private checkPort(host: string, port: number): Promise<boolean> {
    if (this.options.isPortListening) {
      return this.options.isPortListening(host, port);
    }
    const { promise, resolve } = Promise.withResolvers<boolean>();
    const socket = net.createConnection({ host, port });
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("error", () => {
      socket.destroy();
      resolve(false);
    });
    return promise;
  }
}

export function resolveGuiHostLauncher(): string | null {
  if (process.env.VEYYON_GUI_HOST_LAUNCHER && fs.existsSync(process.env.VEYYON_GUI_HOST_LAUNCHER)) {
    return process.env.VEYYON_GUI_HOST_LAUNCHER;
  }

  const userInstalled = path.join(os.homedir(), ".veyyon", "telegram", "veyyon-gui-host.ps1");
  if (fs.existsSync(userInstalled)) {
    return userInstalled;
  }

  const beside = path.resolve(import.meta.dir, "veyyon-gui-host.ps1");
  if (fs.existsSync(beside)) {
    return beside;
  }

  const parent = path.resolve(import.meta.dir, "..", "veyyon-gui-host.ps1");
  if (fs.existsSync(parent)) {
    return parent;
  }

  return null;
}
function sleep(ms: number): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  const timer = setTimeout(resolve, ms);
  timer.unref?.();
  return promise;
}
