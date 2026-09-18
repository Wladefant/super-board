#!/usr/bin/env bun
/**
 * main.ts — Daemon entrypoint: `bun daemon/main.ts [run|status|stop|check]`.
 *
 * `run` is the long-lived process; the other verbs read or signal it through the
 * files it maintains, so an operator can inspect a running daemon without a socket.
 */

import * as fs from "node:fs";
import {
  getDaemonPidPath,
  getDaemonStatusPath,
  readDaemonSlotIds,
} from "./config";
import { claimDaemonPidFile, TelegramDaemon } from "./runtime";
import { getProcessIdentity } from "../extension/coordinator";
import { resolveGuiHostEndpoint } from "./session-control";

async function run(): Promise<number> {
  const claim = claimDaemonPidFile();
  if (!claim.ok) {
    console.error(`A Telegram daemon is already running as PID ${claim.holder}. Stop it before starting another.`);
    return 69;
  }

  const daemon = new TelegramDaemon();
  const report = await daemon.start();
  if (report.slots.length === 0) {
    daemon.log('No slot opted into daemon ownership. Set "daemon": true on a slot in manifest.json or export VEYYON_TELEGRAM_DAEMON_SLOTS.');
    await daemon.stop();
    return 78;
  }
  if (!report.slots.some(slot => slot.polling)) {
    daemon.log("No opted-in slot could be claimed; every one is held elsewhere. Not polling.");
    await daemon.stop();
    return 75;
  }

  let stopped: Promise<void> | null = null;
  const shutdown = (): void => {
    stopped ??= daemon.stop();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
  process.on("SIGHUP", shutdown);

  // Resolves when every poller has stopped, which is what `onRelease` and the
  // signal handlers both lead to.
  await new Promise<void>(resolve => {
    const timer = setInterval(() => {
      if (stopped || daemon.status().slots.every(slot => !slot.polling)) {
        clearInterval(timer);
        resolve();
      }
    }, 1_000);
  });
  await (stopped ?? daemon.stop());
  return 0;
}

function status(): number {
  const pidPath = getDaemonPidPath();
  const pid = fs.existsSync(pidPath) ? Number.parseInt(fs.readFileSync(pidPath, "utf8").trim(), 10) : NaN;
  const alive = Number.isFinite(pid) && pid > 0 && getProcessIdentity(pid).alive;
  console.log(`daemon: ${alive ? `running (pid ${pid})` : "not running"}`);
  console.log(`gui host endpoint: ${resolveGuiHostEndpoint() ?? "not discovered"}`);
  console.log(`opted-in slots: ${[...readDaemonSlotIds()].join(", ") || "none"}`);
  if (fs.existsSync(getDaemonStatusPath())) {
    console.log(fs.readFileSync(getDaemonStatusPath(), "utf8").trimEnd());
  }
  return alive ? 0 : 3;
}

function stop(): number {
  const pidPath = getDaemonPidPath();
  const pid = fs.existsSync(pidPath) ? Number.parseInt(fs.readFileSync(pidPath, "utf8").trim(), 10) : NaN;
  if (!Number.isFinite(pid) || pid <= 0 || !getProcessIdentity(pid).alive) {
    console.log("No running Telegram daemon.");
    return 0;
  }
  process.kill(pid, "SIGTERM");
  console.log(`Sent SIGTERM to Telegram daemon PID ${pid}.`);
  return 0;
}

/** Reports whether this machine is configured for the daemon, without starting it. */
function check(): number {
  const slots = [...readDaemonSlotIds()];
  const endpoint = resolveGuiHostEndpoint();
  console.log(`opted-in slots: ${slots.join(", ") || "none"}`);
  console.log(`gui host endpoint: ${endpoint ?? "not discovered"}`);
  if (slots.length === 0) {
    console.error('No slot opted in. Add "daemon": true to a slot in ~/.veyyon/telegram/manifest.json.');
    return 78;
  }
  if (!endpoint) {
    console.error("No GUI host endpoint discovered; the daemon can poll but cannot drive sessions.");
    return 70;
  }
  return 0;
}

const verb = process.argv[2] ?? "run";
const exitCode = verb === "run"
  ? await run()
  : verb === "status"
    ? status()
    : verb === "stop"
      ? stop()
      : verb === "check"
        ? check()
        : (console.error(`Unknown verb '${verb}'. Use run, status, stop or check.`), 64);
process.exit(exitCode);
