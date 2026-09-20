/**
 * config.ts — Daemon slot opt-in, paths and workspace resolution.
 *
 * The daemon owns a bot token for the whole machine, so it never guesses which
 * slots are its own: a slot participates only when `manifest.json` marks it
 * `"daemon": true` (or `VEYYON_TELEGRAM_DAEMON_SLOTS` names it). Every other slot
 * keeps belonging to the in-session extension, and the two never poll one token.
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
  getDefaultManifestPath,
  matchProjectGlob,
  type BotPoolCoordinator,
} from "../extension/coordinator";
import type { BotPoolManifest, DiscoveredSlot } from "../extension/types";

/** Directory holding daemon runtime state: pid file, log, status snapshot. */
export function getDaemonRunDir(): string {
  return (
    process.env.VEYYON_TELEGRAM_DAEMON_DIR ||
    path.join(os.homedir(), ".veyyon", "telegram", "daemon", "run")
  );
}

/** Routing ledger shared by the daemon and anything reading its routes. */
export function getDaemonDbPath(): string {
  return (
    process.env.VEYYON_TELEGRAM_DAEMON_DB ||
    path.join(os.homedir(), ".veyyon", "telegram", "daemon.db")
  );
}

export function getDaemonPidPath(): string {
  return path.join(getDaemonRunDir(), "daemon.pid");
}

export function getDaemonStatusPath(): string {
  return path.join(getDaemonRunDir(), "daemon.status.json");
}

/**
 * The one log file. The launcher redirects the detached process's stdout and
 * stderr here so a startup crash before any of this code runs is still readable,
 * and sets `VEYYON_TELEGRAM_DAEMON_LOG` so the daemon appends its own lines to the
 * same file rather than a second one. Both of those redirects buffer, which is why
 * the daemon writes here directly instead of printing.
 */
export function getDaemonLogPath(): string {
  return (
    process.env.VEYYON_TELEGRAM_DAEMON_LOG ||
    path.join(os.homedir(), ".veyyon", "telegram", "daemon.log")
  );
}

/**
 * Slot ids that opted into daemon ownership. `VEYYON_TELEGRAM_DAEMON_SLOTS` overrides
 * the manifest so a disposable bot can be driven without editing operator state.
 */
export function readDaemonSlotIds(manifestPath: string = getDefaultManifestPath()): Set<string> {
  const override = process.env.VEYYON_TELEGRAM_DAEMON_SLOTS;
  if (override !== undefined) {
    return new Set(
      override
        .split(",")
        .map(entry => entry.trim())
        .filter(Boolean),
    );
  }
  if (!fs.existsSync(manifestPath)) return new Set();
  try {
    const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8")) as BotPoolManifest;
    if (!Array.isArray(manifest.slots)) return new Set();
    return new Set(manifest.slots.filter(slot => slot.enabled && slot.daemon === true).map(slot => slot.slotId));
  } catch {
    return new Set();
  }
}

export interface DaemonSlot extends DiscoveredSlot {
  /**
   * Absolute directory a session for this slot runs in, or null when the slot's
   * declared projects resolve to nothing that exists. A null workspace still polls
   * and answers commands; it refuses to create a session instead of inventing a cwd.
   */
  workspace: string | null;
  /**
   * Slot mode: "dm" (default, 1-on-1 private chat) or "forum" (supergroup forum topics).
   */
  mode?: "dm" | "forum";
  /**
   * Supergroup chat id when mode is "forum" (e.g. "-1001234567890").
   */
  forumChatId?: string;
}

/**
 * Directories a project token may resolve to. `preferredProjects` holds tokens
 * ("ing", "polysim"), not paths, so the only trustworthy source of real
 * directories on this machine is what sessions have actually leased.
 */
export function knownProjectPaths(coordinator: BotPoolCoordinator): string[] {
  const seen = new Set<string>();
  const paths: string[] = [];
  for (const slot of coordinator.getPoolStatus().slots) {
    const projectPath = slot.lease?.projectPath;
    if (!projectPath || seen.has(projectPath)) continue;
    seen.add(projectPath);
    paths.push(projectPath);
  }
  return paths;
}

/**
 * Resolve the workspace a slot's sessions run in: an absolute declared directory
 * wins, then the first known project path a declared token matches, and finally the
 * slot's `defaultProject`.
 *
 * The fallback exists because a bot that serves every project declares no affinity
 * at all, and an empty declaration matches nothing: without it the daemon polls and
 * answers but refuses `/new`, having no directory to start a session in.
 */
export function resolveWorkspace(
  preferredProjects: string[] | undefined,
  candidates: string[],
  defaultProject?: string,
): string | null {
  const declared = (preferredProjects ?? []).map(entry => String(entry).trim()).filter(Boolean);
  for (const entry of declared) {
    if (path.isAbsolute(entry) && isDirectory(entry)) return entry;
  }
  for (const entry of declared) {
    for (const candidate of candidates) {
      if (isDirectory(candidate) && matchProjectGlob(entry, candidate)) return candidate;
    }
  }
  const fallback = defaultProject?.trim();
  if (fallback && path.isAbsolute(fallback) && isDirectory(fallback)) return fallback;
  return null;
}

function isDirectory(candidate: string): boolean {
  try {
    return fs.statSync(candidate).isDirectory();
  } catch {
    return false;
  }
}

/** Discovered slots that opted into the daemon, each with its resolved workspace. */
export function resolveDaemonSlots(
  coordinator: BotPoolCoordinator,
  manifestPath: string = getDefaultManifestPath(),
): DaemonSlot[] {
  const optedIn = readDaemonSlotIds(manifestPath);
  if (optedIn.size === 0) return [];
  const candidates = knownProjectPaths(coordinator);
  const manifestMap = new Map<string, { mode?: "dm" | "forum"; forumChatId?: string }>();
  if (fs.existsSync(manifestPath)) {
    try {
      const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8")) as {
        slots?: Array<{ slotId?: string; mode?: string; forumChatId?: string | number }>;
      };
      if (Array.isArray(manifest.slots)) {
        for (const slot of manifest.slots) {
          if (slot.slotId) {
            manifestMap.set(slot.slotId, {
              mode: slot.mode === "forum" ? "forum" : "dm",
              forumChatId: slot.forumChatId !== undefined ? String(slot.forumChatId) : undefined,
            });
          }
        }
      }
    } catch {}
  }
  return coordinator
    .syncSlots()
    .filter(slot => optedIn.has(slot.slotId))
    .map(slot => {
      const extra = manifestMap.get(slot.slotId);
      return {
        ...slot,
        mode: extra?.mode ?? "dm",
        forumChatId: extra?.forumChatId,
        workspace: resolveWorkspace(slot.projects ?? slot.preferredProjects, candidates, slot.defaultProject),
      };
    });
}
