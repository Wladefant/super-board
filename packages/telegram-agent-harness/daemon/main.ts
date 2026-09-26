#!/usr/bin/env bun
/**
 * main.ts — Daemon entrypoint: `bun daemon/main.ts [run|status|stop|check]`.
 *
 * `run` is the long-lived process; the other verbs read or signal it through the
 * files it maintains, so an operator can inspect a running daemon without a socket.
 */

import * as fs from "node:fs";
import {
  getDaemonDbPath,
  getDaemonPidPath,
  getDaemonStatusPath,
  readDaemonSlotIds,
  resolveDaemonSlots,
} from "./config";
import { claimDaemonPidFile, TelegramDaemon } from "./runtime";
import { BotPoolCoordinator, getDefaultManifestPath, getProcessIdentity } from "../extension/coordinator";
import {
  TerminalSessionControl,
  type DaemonSessionSummary,
} from "./session-control";
import { ForumManager } from "./forum";
import { DaemonStore } from "./store";
/**
 * How often `run` retries a slot someone else is holding. Seconds, not minutes: the
 * handover happens the moment an interactive session exits, and the gap is a window
 * in which the bot answers nobody.
 */
const CLAIM_RETRY_MS = Number.parseInt(process.env.VEYYON_TELEGRAM_CLAIM_RETRY_MS ?? "", 10) || 10_000;

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
  // Not an error: the token is held by a session that will exit or release it, and
  // the daemon is the thing that is supposed to be running when that happens.
  if (!report.slots.some(slot => slot.polling)) {
    daemon.log(
      `No opted-in slot is free yet; every one is held elsewhere. Staying up and retrying every ${CLAIM_RETRY_MS / 1_000}s.`,
    );
  }

  let stopped: Promise<void> | null = null;
  const shutdown = (): void => {
    stopped ??= daemon.stop();
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
  process.on("SIGHUP", shutdown);

  // Two conditions share one tick. Once a slot has polled, losing every poller means
  // `onRelease` or a signal ran and the daemon is done — the pre-retry behaviour.
  // Before that, nothing has been handed over yet, so an empty roster is the state
  // this loop exists to end, and it retries instead of exiting.
  let handedOver = report.slots.some(slot => slot.polling);
  let retrying = false;
  let nextRetryAt = Date.now() + CLAIM_RETRY_MS;
  const { promise, resolve } = Promise.withResolvers<void>();
  const timer = setInterval(() => {
    const polling = daemon.status().slots.some(slot => slot.polling);
    if (polling) handedOver = true;
    if (stopped || (handedOver && !polling && !daemon.hasPendingSlots())) {
      clearInterval(timer);
      resolve();
      return;
    }
      if (!daemon.hasPendingSlots() || retrying || Date.now() < nextRetryAt) return;
      retrying = true;
      void daemon
        .claimPending()
        .catch(error => {
          daemon.log(`Claim retry failed: ${error instanceof Error ? error.message : String(error)}`);
        })
        .finally(() => {
          nextRetryAt = Date.now() + CLAIM_RETRY_MS;
          retrying = false;
        });
  }, 1_000);
  await promise;
  await (stopped ?? daemon.stop());
  return 0;
}

function status(): number {
  const pidPath = getDaemonPidPath();
  const pid = fs.existsSync(pidPath) ? Number.parseInt(fs.readFileSync(pidPath, "utf8").trim(), 10) : NaN;
  const alive = Number.isFinite(pid) && pid > 0 && getProcessIdentity(pid).alive;
  console.log(`daemon: ${alive ? `running (pid ${pid})` : "not running"}`);
  console.log("session transport: authenticated live terminal IPC (no GUI fallback)");
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
  console.log(`opted-in slots: ${slots.join(", ") || "none"}`);
  console.log("session transport: authenticated live terminal IPC");
  if (slots.length === 0) {
    console.error('No slot opted in. Add "daemon": true to a slot in ~/.veyyon/telegram/manifest.json.');
    return 78;
  }
  return 0;
}

async function reconcileOnce(dryRun = true): Promise<number> {
  const manifestPath = getDefaultManifestPath();
  const coordinator = new BotPoolCoordinator(undefined, manifestPath);
  const slots = resolveDaemonSlots(coordinator, manifestPath);
  const forumSlots = slots.filter(s => s.mode === "forum" && s.forumChatId);

  if (forumSlots.length === 0) {
    console.log("No forum-mode slots configured in manifest.");
    coordinator.close();
    return 0;
  }

  const store = new DaemonStore(getDaemonDbPath());
  const control = new TerminalSessionControl({
    onEvent: () => {},
    onLog: () => {},
  });

  let wireSessions: DaemonSessionSummary[] = [];
  try {
    wireSessions = await control.listSessions();
  } catch (err) {
    console.error(`Failed to list live terminal owners: ${err instanceof Error ? err.message : String(err)}`);
    control.close();
    store.close();
    coordinator.close();
    return 1;
  }

  console.log(`[Auto-attach ${dryRun ? "DRY RUN" : "LIVE"}] Live terminal owners: ${wireSessions.length}`);

  for (const slot of forumSlots) {
    const isEnabled = slot.autoAttach !== false;
    console.log(`\nSlot: ${slot.slotId} (forumChatId: ${slot.forumChatId}, autoAttach: ${isEnabled})`);
    if (!isEnabled) {
      console.log("  Auto-attach is disabled (autoAttach: false). Skipping slot.");
      continue;
    }

    const manager = new ForumManager({
      slot,
      token: "dry-run-token",
      forumChatId: slot.forumChatId!,
      store,
      control,
      client: {
        createForumTopic: async (_chatId, name) => ({ message_thread_id: 9999, name }),
        closeForumTopic: async () => true,
        reopenForumTopic: async () => true,
        sendMessage: async () => ({ ok: true, result: { message_id: 9999 } }),
      },
      log: msg => console.log(`  ${msg}`),
    });

    const result = await manager.reconcileAutoAttach(undefined, { dryRun });
    if (result.actions.length === 0) {
      console.log("  No live top-level sessions found.");
    } else {
      for (const a of result.actions) {
        if (a.action === "create") {
          console.log(`  [CREATE] session: ${a.sessionId} | folder: "${a.folder}" | topic: "${a.topicName}" | workspace: ${a.workspace}`);
        } else if (a.action === "rebind") {
          console.log(`  [REBIND] session: ${a.sessionId} | folder: "${a.folder}" | topic #${a.topicId} | workspace: ${a.workspace} (${a.reason})`);
        } else {
          console.log(`  [SKIP]   session: ${a.sessionId} | folder: "${a.folder}" | topic #${a.topicId} | workspace: ${a.workspace} (${a.reason})`);
        }
      }
      console.log(`  Summary: ${result.created} created, ${result.rebound} rebound, ${result.skipped} skipped, ${result.errors} errors`);
    }
  }

  control.close();
  store.close();
  coordinator.close();
  return 0;
}

const rawArgs = process.argv.slice(2);
const isReconcileOnce = rawArgs.includes("--reconcile-once") || rawArgs.includes("reconcile");
const isDryRun = rawArgs.includes("--dry-run");

let exitCode = 0;
if (isReconcileOnce) {
  exitCode = await reconcileOnce(true);
} else {
  const verb = rawArgs[0] ?? "run";
  exitCode = verb === "run"
    ? await run()
    : verb === "status"
      ? status()
      : verb === "stop"
        ? stop()
        : verb === "check"
          ? check()
          : (console.error(`Unknown verb '${verb}'. Use run, status, stop, check or --reconcile-once --dry-run.`), 64);
}
process.exit(exitCode);
