/**
 * coordinator.ts — Telegram Bot Lease Pool Coordinator.
 *
 * Implements atomic SQLite WAL leases, Win32 PID + process start timestamp checks,
 * project affinity matching, Claude lock interoperability, and crash recovery.
 */

import { Database } from "bun:sqlite";
import { dlopen, FFIType, ptr } from "bun:ffi";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { getTokenFingerprint } from "./sanitizer";
import type {
  AccessConfig,
  BotLeaseRecord,
  BotPoolManifest,
  BusySlotHolder,
  ClaimResult,
  DiscoveredSlot,
  PoolStatusSummary,
  ProcessIdentity,
  OutboundMessageCorrelation,
  ReplyRoutingResolution,
  DecisionCallbackRecord,
  DecisionCallbackResolution,
} from "./types";
export function getDefaultPoolDbPath(): string {
  return process.env.VEYYON_POOL_DB || path.join(os.homedir(), ".veyyon", "telegram", "bot_pool.db");
}
export function getDefaultManifestPath(): string {
  return process.env.VEYYON_MANIFEST_PATH || path.join(os.homedir(), ".veyyon", "telegram", "manifest.json");
}
export function getDefaultChannelsDir(): string {
  return process.env.VEYYON_CHANNELS_DIR || path.join(os.homedir(), ".claude", "channels");
}
/** Pid files a Claude channel poller writes; a live owner locks the channel. */
const CLAUDE_PID_FILES = ["bot.pid", "poll.pid", "server.pid"] as const;

/**
 * Pid file a Veyyon channel poller writes: the legacy Python bridge's own lock, and the
 * mirror this coordinator writes for the lease owner.
 */
const VEYYON_PID_FILES = ["veyyon-bot.pid"] as const;

const LEASE_TTL_SECONDS = 20.0;
const HEARTBEAT_INTERVAL_MS = 5000;

interface Win32KernelSymbols {
  OpenProcess: (dwDesiredAccess: number, bInheritHandle: boolean, dwProcessId: number) => unknown;
  GetProcessTimes: (
    hProcess: unknown,
    lpCreationTime: unknown,
    lpExitTime: unknown,
    lpKernelTime: unknown,
    lpUserTime: unknown,
  ) => boolean;
  GetExitCodeProcess: (hProcess: unknown, lpExitCode: unknown) => boolean;
  CloseHandle: (hObject: unknown) => boolean;
}

// Win32 FFI for process liveness and creation time
let kernel32Symbols: Win32KernelSymbols | null = null;

try {
  if (process.platform === "win32") {
    const k32 = dlopen("kernel32.dll", {
      OpenProcess: {
        args: [FFIType.u32, FFIType.bool, FFIType.u32],
        returns: FFIType.ptr,
      },
      GetProcessTimes: {
        args: [FFIType.ptr, FFIType.ptr, FFIType.ptr, FFIType.ptr, FFIType.ptr],
        returns: FFIType.bool,
      },
      GetExitCodeProcess: {
        args: [FFIType.ptr, FFIType.ptr],
        returns: FFIType.bool,
      },
      CloseHandle: {
        args: [FFIType.ptr],
        returns: FFIType.bool,
      },
    });
    kernel32Symbols = k32.symbols;
  }
} catch {
  kernel32Symbols = null;
}

export function getProcessIdentity(pid: number): ProcessIdentity {
  if (pid <= 0) {
    return { alive: false, creationTime: 0n, uncertain: false };
  }

  if (pid === process.pid) {
    return { alive: true, creationTime: 1n, uncertain: false };
  }

  if (kernel32Symbols && process.platform === "win32") {
    try {
      const PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
      const STILL_ACTIVE = 259;
      const hProc = kernel32Symbols.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid);
      if (hProc) {
        try {
          const exitCodeBuf = new Uint32Array(1);
          if (kernel32Symbols.GetExitCodeProcess(hProc, ptr(exitCodeBuf))) {
            if (exitCodeBuf[0] !== STILL_ACTIVE) {
              return { alive: false, creationTime: 0n, uncertain: false };
            }
          }

          const creationTime = new BigUint64Array(1);
          const exitTime = new BigUint64Array(1);
          const kernelTime = new BigUint64Array(1);
          const userTime = new BigUint64Array(1);

          const ok = kernel32Symbols.GetProcessTimes(
            hProc,
            ptr(creationTime),
            ptr(exitTime),
            ptr(kernelTime),
            ptr(userTime),
          );
          if (ok) {
            return { alive: true, creationTime: creationTime[0], uncertain: false };
          }
          // Process exists and handle opened, but timestamps unavailable: fail closed (alive + uncertain)
          return { alive: true, creationTime: 0n, uncertain: true };
        } finally {
          kernel32Symbols.CloseHandle(hProc);
        }
      }
    } catch {
      // Fall through to kill(0) fallback
    }
  }

  // Fallback liveness check: fail-closed on error uncertainty
  try {
    process.kill(pid, 0);
    return { alive: true, creationTime: 0n, uncertain: true };
  } catch (err: unknown) {
    if (err && typeof err === "object" && "code" in err && err.code === "EPERM") {
      // EPERM means process exists but we lack query permission
      return { alive: true, creationTime: 0n, uncertain: true };
    }
    // ESRCH means process definitely does not exist
    return { alive: false, creationTime: 0n, uncertain: false };
  }
}

interface LeaseRow {
  slot_id: string;
  session_id: string;
  project_path: string;
  owner_pid: number;
  owner_proc_start: string;
  acquired_at: number;
  heartbeat_at: number;
  ttl_seconds: number;
  lease_status: string;
}

interface CorrelationRow {
  bot_id: string;
  chat_id: string;
  message_id: number;
  slot_id: string;
  session_id: string;
  request_id: string | null;
  decision_id: string | null;
  project_path: string | null;
  created_at: number;
}

/**
 * Matches projectCwd against a glob pattern.
 * Supports:
 * - Empty / "*" / "**" -> matches any project
 * - Wildcard patterns with * and ? (e.g. "*project-alpha*", "*-backend")
 * - Path globs with directory separators (e.g. "project-core/*", "/workspace/frontend")
 * - Case-insensitive and normalizes / vs \
 * - Plain project segment tokens (e.g. "core-service", "worker") for backward compatibility
 */
export function matchProjectGlob(pattern: string, projectCwd: string): boolean {
  const rawPat = String(pattern || "").trim();
  if (!rawPat || rawPat === "*" || rawPat === "**") {
    return true;
  }

  const normalizedCwd = projectCwd.replace(/\\/g, "/").toLowerCase();
  const normalizedPat = rawPat.replace(/\\/g, "/").toLowerCase();

  if (normalizedCwd === normalizedPat) {
    return true;
  }

  if (normalizedPat.includes("*") || normalizedPat.includes("?")) {
    const starReplacer = normalizedPat.includes("/") ? "[^/]*" : ".*";
    const regexStr = "^" + normalizedPat
      .replace(/[.+^${}()|[\]\\]/g, "\\$&")
      .replace(/\*\*/g, "___GLOBSTAR___")
      .replace(/\*/g, starReplacer)
      .replace(/___GLOBSTAR___/g, ".*")
      .replace(/\?/g, "[^/]") + "$";

    try {
      const regex = new RegExp(regexStr);
      if (regex.test(normalizedCwd)) {
        return true;
      }
    } catch {}

    try {
      const relaxedRegexStr = "(^|/)" + normalizedPat
        .replace(/[.+^${}()|[\]\\]/g, "\\$&")
        .replace(/\*\*/g, ".*")
        .replace(/\*/g, ".*")
        .replace(/\?/g, ".") + "(/|$)";
      const subRegex = new RegExp(relaxedRegexStr);
      if (subRegex.test(normalizedCwd)) {
        return true;
      }
    } catch {}
  }

  const segments = normalizedCwd
    .split("/")
    .map(s => s.trim())
    .filter(s => s.length > 0);

  if (segments.some(segment => segment === normalizedPat || segment.includes(normalizedPat))) {
    return true;
  }

  return false;
}

/**
 * Configuration-driven project affinity gate. A slot declares the projects (cwd globs) it serves.
 * Default (omitted, empty array, or discovered without explicit project config) = any project.
 * Wildcard ('*' or '**') = any project.
 * Otherwise, the session's project path must match at least one declared glob.
 */
export function isSlotEligibleForProject(preferredProjects: string[] | undefined, projectCwd: string): boolean {
  if (!preferredProjects || !Array.isArray(preferredProjects)) return true;
  const declared = preferredProjects.filter(p => String(p || "").trim().length > 0);
  if (declared.length === 0) return true;
  return declared.some(p => matchProjectGlob(p, projectCwd));
}

/**
 * Returns true when projectCwd matches one of the slot's declared project globs.
 */
export function slotMatchesProject(preferredProjects: string[] | undefined, projectCwd: string): boolean {
  if (!preferredProjects || !Array.isArray(preferredProjects)) return false;
  const declared = preferredProjects.filter(p => String(p || "").trim().length > 0);
  if (declared.length === 0) return false;
  return declared.some(p => matchProjectGlob(p, projectCwd));
}

/**
 * Returns true if the slot has a specific non-wildcard declaration matching the project.
 * Used for priority sorting so dedicated slots are claimed before shared/wildcard slots.
 */
export function slotHasSpecificAffinity(preferredProjects: string[] | undefined, projectCwd: string): boolean {
  if (!preferredProjects || !Array.isArray(preferredProjects)) return false;
  const declared = preferredProjects.filter(p => {
    const s = String(p || "").trim();
    return s.length > 0 && s !== "*" && s !== "**";
  });
  if (declared.length === 0) return false;
  return declared.some(p => matchProjectGlob(p, projectCwd));
}
export class BotPoolCoordinator {
  private db: Database;
  private dbPath: string;
  private manifestPath: string;
  private channelsDir: string;
  private activeHeartbeatTimers = new Map<string, Timer>();

  constructor(
    dbPath: string = getDefaultPoolDbPath(),
    manifestPath: string = getDefaultManifestPath(),
    channelsDir: string = getDefaultChannelsDir(),
  ) {
    this.dbPath = dbPath;
    this.manifestPath = manifestPath;
    this.channelsDir = channelsDir;

    const parentDir = path.dirname(dbPath);
    if (!fs.existsSync(parentDir)) {
      fs.mkdirSync(parentDir, { recursive: true });
    }

    this.db = new Database(dbPath);
    this.initDatabase();
  }

  private ensureDbOpen(): void {
    try {
      this.db.query("SELECT 1").get();
    } catch {
      this.db = new Database(this.dbPath);
      this.initDatabase();
    }
  }

  private initDatabase(): void {
    this.db.run("PRAGMA journal_mode = WAL;");
    this.db.run("PRAGMA busy_timeout = 5000;");
    this.db.run("PRAGMA synchronous = NORMAL;");

    this.db.run(`
      CREATE TABLE IF NOT EXISTS bot_slots (
        slot_id           TEXT PRIMARY KEY,
        state_dir         TEXT NOT NULL UNIQUE,
        bot_id            TEXT NOT NULL,
        fingerprint       TEXT NOT NULL,
        preferred_projects TEXT NOT NULL,
        enabled           INTEGER NOT NULL DEFAULT 1,
        created_at        REAL NOT NULL,
        updated_at        REAL NOT NULL
      );
    `);

    this.db.run(`
      CREATE TABLE IF NOT EXISTS bot_leases (
        slot_id           TEXT PRIMARY KEY,
        session_id        TEXT NOT NULL,
        project_path      TEXT NOT NULL,
        owner_pid         INTEGER NOT NULL,
        owner_proc_start  TEXT NOT NULL,
        acquired_at       REAL NOT NULL,
        heartbeat_at      REAL NOT NULL,
        ttl_seconds       REAL NOT NULL DEFAULT 20.0,
        lease_status      TEXT NOT NULL CHECK(lease_status IN ('ACTIVE', 'RELEASED')),
        FOREIGN KEY(slot_id) REFERENCES bot_slots(slot_id)
      );
    `);

    this.db.run("CREATE INDEX IF NOT EXISTS idx_leases_heartbeat ON bot_leases(heartbeat_at, lease_status);");

    // Shared outbound correlation index. Written by this coordinator (interactive
    // channel traffic) and by the portable Python sender (outbound notifications);
    // read by the poller to bind an inbound reply back to its originating session.
    this.db.run(`
      CREATE TABLE IF NOT EXISTS message_correlations (
        bot_id       TEXT NOT NULL,
        chat_id      TEXT NOT NULL,
        message_id   INTEGER NOT NULL,
        slot_id      TEXT NOT NULL,
        session_id   TEXT NOT NULL,
        request_id   TEXT,
        decision_id  TEXT,
        project_path TEXT,
        created_at   REAL NOT NULL,
        PRIMARY KEY (bot_id, chat_id, message_id)
      );
    `);

    this.db.run("CREATE INDEX IF NOT EXISTS idx_msgcorr_session ON message_correlations(session_id, created_at);");

    this.db.run(`
      CREATE TABLE IF NOT EXISTS decision_callbacks (
        callback_token TEXT PRIMARY KEY,
        decision_id    TEXT NOT NULL,
        choice_id      TEXT NOT NULL,
        session_id     TEXT NOT NULL,
        chat_id        TEXT NOT NULL,
        user_id        TEXT NOT NULL,
        question_hash  TEXT NOT NULL,
        expires_at     REAL NOT NULL,
        consumed_at    REAL,
        created_at     REAL NOT NULL
      );
    `);
    this.db.run("CREATE INDEX IF NOT EXISTS idx_decision_callbacks_dec_choice ON decision_callbacks(decision_id, choice_id);");
    this.db.run("CREATE INDEX IF NOT EXISTS idx_decision_callbacks_session ON decision_callbacks(session_id);");
  }

  public readRawTokenForSlot(stateDir: string): string | null {
    const envPath = path.join(stateDir, ".env");
    if (!fs.existsSync(envPath)) return null;
    try {
      const content = fs.readFileSync(envPath, "utf8");
      const match = content.match(/TELEGRAM_BOT_TOKEN\s*=\s*([^\r\n#]+)/);
      if (match && match[1]) {
        return match[1].trim();
      }
    } catch {
      return null;
    }
    return null;
  }

  public readAccessConfig(stateDir: string): AccessConfig {
    const accessPath = path.join(stateDir, "access.json");
    if (!fs.existsSync(accessPath)) {
      return { dmPolicy: "allowlist", allowFrom: [] };
    }
    try {
      const raw = fs.readFileSync(accessPath, "utf8");
      const parsed = JSON.parse(raw);
      const dmPolicy = typeof parsed.dmPolicy === "string" ? parsed.dmPolicy : "allowlist";
      const allowFrom = Array.isArray(parsed.allowFrom) ? parsed.allowFrom.map(String) : [];
      return { dmPolicy, allowFrom };
    } catch {
      return { dmPolicy: "allowlist", allowFrom: [] };
    }
  }

  public syncSlots(): DiscoveredSlot[] {
    this.ensureDbOpen();
    const slotsMap = new Map<string, DiscoveredSlot>();

    // 1. Read manifest slots
    if (fs.existsSync(this.manifestPath)) {
      try {
        const manifest = JSON.parse(fs.readFileSync(this.manifestPath, "utf8")) as BotPoolManifest;
        if (Array.isArray(manifest.slots)) {
          for (const s of manifest.slots) {
            if (!s.enabled) continue;
            const token = this.readRawTokenForSlot(s.stateDir);
            if (!token) continue;
            const fp = getTokenFingerprint(token);
            const projects = (s.projects && s.projects.length > 0) ? s.projects : (s.preferredProjects || []);
            slotsMap.set(s.slotId, {
              slotId: s.slotId,
              stateDir: s.stateDir,
              botId: fp.botId,
              fingerprint: fp.fingerprint,
              preferredProjects: projects,
              projects,
              enabled: s.enabled,
              daemon: s.daemon === true,
              ...(typeof s.defaultProject === "string" && s.defaultProject.trim().length > 0
                ? { defaultProject: s.defaultProject.trim() }
                : {}),
            });
          }
        }
      } catch {
        // Continue with directory discovery
      }
    }

    // 2. Discover channel directories under channelsDir.
    // Discovered slots default to an empty preference list (eligible for any project)
    // unless explicitly configured via slot.json/config.json/access.json.
    if (fs.existsSync(this.channelsDir)) {
      try {
        const entries = fs.readdirSync(this.channelsDir, { withFileTypes: true });
        for (const entry of entries) {
          if (!entry.isDirectory()) continue;
          if (!entry.name.startsWith("telegram-") && entry.name !== "telegram") continue;

          const slotId = entry.name;
          if (slotsMap.has(slotId)) continue;

          const stateDir = path.join(this.channelsDir, entry.name);
          const token = this.readRawTokenForSlot(stateDir);
          if (!token) continue;

          const fp = getTokenFingerprint(token);
          let preferred: string[] = [];
          for (const cfgFile of ["slot.json", "config.json", "access.json"]) {
            const cfgPath = path.join(stateDir, cfgFile);
            if (fs.existsSync(cfgPath)) {
              try {
                const parsed = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
                const configured = (Array.isArray(parsed.projects) && parsed.projects.length > 0)
                  ? parsed.projects
                  : (Array.isArray(parsed.preferredProjects) && parsed.preferredProjects.length > 0
                    ? parsed.preferredProjects
                    : null);
                if (configured) {
                  preferred = configured.map(String);
                  break;
                }
              } catch {
                // Ignore parse errors in config files
              }
            }
          }

          slotsMap.set(slotId, {
            slotId,
            stateDir,
            botId: fp.botId,
            fingerprint: fp.fingerprint,
            preferredProjects: preferred,
            enabled: true,
          });
        }
      } catch {
        // Ignore read errors
      }
    }

    const now = Date.now() / 1000;
    const upsertStmt = this.db.prepare(`
      INSERT INTO bot_slots (slot_id, state_dir, bot_id, fingerprint, preferred_projects, enabled, created_at, updated_at)
      VALUES ($slotId, $stateDir, $botId, $fingerprint, $preferredProjects, 1, $now, $now)
      ON CONFLICT(slot_id) DO UPDATE SET
        state_dir = excluded.state_dir,
        bot_id = excluded.bot_id,
        fingerprint = excluded.fingerprint,
        preferred_projects = excluded.preferred_projects,
        enabled = excluded.enabled,
        updated_at = excluded.updated_at;
    `);

    // Finalized explicitly. A statement from `prepare` is not cached on the Database
    // and is not finalized by `close()`, so leaking one here holds the SQLite handle
    // open for the life of the process: `close()` becomes a no-op, bot_pool.db keeps
    // its -wal/-shm, and the file stays locked. That is how earlier verifier runs left
    // their temp fixtures behind on Windows.
    try {
      const slotsList = Array.from(slotsMap.values());
      for (const slot of slotsList) {
        upsertStmt.run({
          $slotId: slot.slotId,
          $stateDir: slot.stateDir,
          $botId: slot.botId,
          $fingerprint: slot.fingerprint,
          $preferredProjects: JSON.stringify(slot.preferredProjects),
          $now: now,
        });
      }
      return slotsList;
    } finally {
      upsertStmt.finalize();
    }
  }

  /**
   * Applies the pid-file lock convention to one candidate set of files: an owner that
   * is alive — or whose liveness cannot be established, which counts as alive — holds
   * the channel, a definitely dead owner's file is cleaned up, and this process's own
   * mirror is skipped.
   */
  private checkPidFileConflict(
    stateDir: string,
    pidFiles: readonly string[],
    currentPid: number,
  ): { busy: boolean; pid: number | null; pidFile: string | null } {
    for (const pidFile of pidFiles) {
      const filePath = path.join(stateDir, pidFile);
      if (!fs.existsSync(filePath)) continue;

      try {
        const text = fs.readFileSync(filePath, "utf8").trim();
        const pid = Number.parseInt(text, 10);
        if (Number.isFinite(pid) && pid > 0) {
          if (pid === currentPid) {
            // Self-owned PID mirror
            continue;
          }
          const ident = getProcessIdentity(pid);
          if (ident.alive || ident.uncertain) {
            return { busy: true, pid, pidFile };
          }
          // Only unlink if process is definitely dead (!alive && !uncertain)
          if (!ident.alive && !ident.uncertain) {
            try {
              fs.unlinkSync(filePath);
            } catch {}
          }
        }
      } catch {}
    }
    return { busy: false, pid: null, pidFile: null };
  }

  private getSlotLease(slotId: string): BotLeaseRecord | null {
    this.ensureDbOpen();
    const row = this.db.query("SELECT * FROM bot_leases WHERE slot_id = ?").get(slotId) as LeaseRow | null;

    if (!row) return null;
    return {
      slotId: row.slot_id,
      sessionId: row.session_id,
      projectPath: row.project_path,
      ownerPid: row.owner_pid,
      ownerProcStart: row.owner_proc_start,
      acquiredAt: row.acquired_at,
      heartbeatAt: row.heartbeat_at,
      ttlSeconds: row.ttl_seconds,
      leaseStatus: row.lease_status as "ACTIVE" | "RELEASED",
    };
  }

  private isSlotBusy(
    slot: DiscoveredSlot,
    currentPid: number,
    currentSessionId: string,
  ): {
    busy: boolean;
    reason?: string;
    activePid?: number;
    holder?: { sessionId?: string; projectPath?: string; ownerPid?: number };
  } {
    // 1. Claude channel poller lock
    const claudeCheck = this.checkPidFileConflict(slot.stateDir, CLAUDE_PID_FILES, currentPid);
    if (claudeCheck.busy && claudeCheck.pid !== null) {
      const lease = this.getSlotLease(slot.slotId);
      const isMatchingLease = lease && lease.ownerPid === claudeCheck.pid && lease.leaseStatus === "ACTIVE";
      return {
        busy: true,
        reason: isMatchingLease
          ? `Veyyon session active (PID ${claudeCheck.pid}, Session ${lease.sessionId}, cwd ${lease.projectPath})`
          : `Claude channel poller active (PID ${claudeCheck.pid})`,
        activePid: claudeCheck.pid,
        holder: {
          sessionId: isMatchingLease ? lease.sessionId : undefined,
          projectPath: isMatchingLease ? lease.projectPath : slot.stateDir,
          ownerPid: claudeCheck.pid,
        },
      };
    }

    // 1b. Veyyon channel poller lock. The legacy Python bridge (veyyon_telegram_bridge.py)
    // holds the same channel through veyyon-bot.pid, which is also where this coordinator
    // mirrors its own owner pid; the bridge already refuses to start while that file names
    // a live process. Checking it here is the other half of that exclusion, so a manually
    // started legacy dispatcher blocks a native claim instead of both polling the one bot.
    // A definitely dead owner is cleaned up by the shared convention above, which leaves
    // stale-PID recovery unchanged.
    const veyyonCheck = this.checkPidFileConflict(slot.stateDir, VEYYON_PID_FILES, currentPid);
    if (veyyonCheck.busy && veyyonCheck.pid !== null) {
      const lease = this.getSlotLease(slot.slotId);
      const isMatchingLease = lease && lease.ownerPid === veyyonCheck.pid && lease.leaseStatus === "ACTIVE";
      return {
        busy: true,
        reason: isMatchingLease
          ? `Veyyon session active (PID ${veyyonCheck.pid}, Session ${lease.sessionId}, cwd ${lease.projectPath})`
          : `Veyyon channel poller active (PID ${veyyonCheck.pid} in ${veyyonCheck.pidFile})`,
        activePid: veyyonCheck.pid,
        holder: {
          sessionId: isMatchingLease ? lease.sessionId : undefined,
          projectPath: isMatchingLease ? lease.projectPath : slot.stateDir,
          ownerPid: veyyonCheck.pid,
        },
      };
    }

    // 2. Veyyon DB lease check
    const lease = this.getSlotLease(slot.slotId);
    if (!lease || lease.leaseStatus === "RELEASED") {
      return { busy: false };
    }

    if (lease.ownerPid === currentPid && lease.sessionId === currentSessionId) {
      // Already owned by this session
      return { busy: false };
    }

    const now = Date.now() / 1000;
    const elapsed = now - lease.heartbeatAt;
    const pidIdent = getProcessIdentity(lease.ownerPid);

    // Fail-closed on PID liveness or uncertainty
    if (pidIdent.alive || pidIdent.uncertain) {
      if (
        pidIdent.creationTime === 0n ||
        lease.ownerProcStart === "0" ||
        String(pidIdent.creationTime) === lease.ownerProcStart ||
        pidIdent.uncertain
      ) {
        return {
          busy: true,
          reason: `Veyyon session active (PID ${lease.ownerPid}, Session ${lease.sessionId}, cwd ${lease.projectPath})`,
          activePid: lease.ownerPid,
          holder: {
            sessionId: lease.sessionId,
            projectPath: lease.projectPath,
            ownerPid: lease.ownerPid,
          },
        };
      }

      // Recycled PID with different start time within TTL
      if (elapsed <= lease.ttlSeconds) {
        return {
          busy: true,
          reason: `Veyyon session lease active within TTL (PID ${lease.ownerPid}, Session ${lease.sessionId}, cwd ${lease.projectPath})`,
          activePid: lease.ownerPid,
          holder: {
            sessionId: lease.sessionId,
            projectPath: lease.projectPath,
            ownerPid: lease.ownerPid,
          },
        };
      }
    }

    // When definitely dead (!alive && !uncertain), or past TTL: reclaimable
    return { busy: false };
  }

  /**
   * Claims one named slot for `sessionId`, or reports who holds it. Shared by pool
   * acquisition and by a caller that owns a specific slot (the standalone daemon),
   * so a lease is written in exactly one place.
   */
  private claimSlot(
    slot: DiscoveredSlot,
    sessionId: string,
    projectCwd: string,
    ownerPid: number,
    procStartStr: string,
  ): { claimed: boolean; holder?: BusySlotHolder } {
    if (!this.readRawTokenForSlot(slot.stateDir)) {
      return { claimed: false };
    }

    const busyCheck = this.isSlotBusy(slot, ownerPid, sessionId);
    if (busyCheck.busy) {
      return {
        claimed: false,
        holder: {
          slotId: slot.slotId,
          sessionId: busyCheck.holder?.sessionId,
          projectPath: busyCheck.holder?.projectPath,
          ownerPid: busyCheck.activePid ?? busyCheck.holder?.ownerPid,
          reason: busyCheck.reason,
        },
      };
    }

    const now = Date.now() / 1000;

    try {
      this.db.run("BEGIN IMMEDIATE;");

      const currentLease = this.getSlotLease(slot.slotId);
      if (currentLease && currentLease.leaseStatus === "ACTIVE") {
        // A live lease held by this very session and process is ours to renew;
        // only a lease belonging to someone else blocks the claim.
        const heldByCaller =
          currentLease.sessionId === sessionId && currentLease.ownerPid === ownerPid;
        const el = now - currentLease.heartbeatAt;
        const live = getProcessIdentity(currentLease.ownerPid);
        if (!heldByCaller && (live.alive || live.uncertain)) {
          if (el <= currentLease.ttlSeconds) {
            this.db.run("ROLLBACK;");
            return {
              claimed: false,
              holder: {
                slotId: slot.slotId,
                sessionId: currentLease.sessionId,
                projectPath: currentLease.projectPath,
                ownerPid: currentLease.ownerPid,
                reason: `Active database lease held by session ${currentLease.sessionId} (PID ${currentLease.ownerPid}, cwd ${currentLease.projectPath})`,
              },
            };
          }
        }
      }

      this.db.run(
        `
        INSERT INTO bot_leases (
          slot_id, session_id, project_path, owner_pid, owner_proc_start,
          acquired_at, heartbeat_at, ttl_seconds, lease_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE')
        ON CONFLICT(slot_id) DO UPDATE SET
          session_id = excluded.session_id,
          project_path = excluded.project_path,
          owner_pid = excluded.owner_pid,
          owner_proc_start = excluded.owner_proc_start,
          acquired_at = excluded.acquired_at,
          heartbeat_at = excluded.heartbeat_at,
          ttl_seconds = excluded.ttl_seconds,
          lease_status = 'ACTIVE';
      `,
        [slot.slotId, sessionId, projectCwd, ownerPid, procStartStr, now, now, LEASE_TTL_SECONDS],
      );

      this.db.run("COMMIT;");
    } catch {
      try {
        this.db.run("ROLLBACK;");
      } catch {}
      return { claimed: false };
    }

    try {
      fs.writeFileSync(path.join(slot.stateDir, "veyyon-bot.pid"), String(ownerPid), "utf8");
      fs.writeFileSync(path.join(slot.stateDir, "bot.pid"), String(ownerPid), "utf8");
    } catch {}

    // Start unreferenced heartbeat
    this.startHeartbeat(slot.slotId, sessionId);
    return { claimed: true };
  }

  /**
   * Claims the slot named by `slotId` regardless of project affinity. The daemon
   * owns whole tokens rather than competing for a pool, and the lease it writes is
   * what makes every in-session poller see the slot as busy.
   */
  public acquireLeaseForSlot(
    slotId: string,
    sessionId: string,
    projectCwd: string,
    ownerPid: number = process.pid,
  ): ClaimResult {
    this.ensureDbOpen();
    const slot = this.syncSlots().find(candidate => candidate.slotId === slotId);
    if (!slot) {
      return { ok: false, error: "SLOT_NOT_FOUND", reason: `Slot '${slotId}' is not an enabled slot with a readable token.` };
    }
    const procStartStr = String(getProcessIdentity(ownerPid).creationTime);
    const result = this.claimSlot(slot, sessionId, projectCwd, ownerPid, procStartStr);
    if (result.claimed) return { ok: true, slot };
    return {
      ok: false,
      error: "SLOT_BUSY",
      reason: result.holder?.reason ?? `Slot '${slotId}' could not be claimed.`,
      busyHolders: result.holder ? [result.holder] : undefined,
    };
  }

  public async acquireLease(
    sessionId: string,
    projectCwd: string,
    ownerPid: number = process.pid,
    waitTimeoutMs = 0,
  ): Promise<ClaimResult> {
    this.ensureDbOpen();
    const startTime = Date.now();
    const pidIdent = getProcessIdentity(ownerPid);
    const procStartStr = String(pidIdent.creationTime);

    while (true) {
      const slots = this.syncSlots();

      // Daemon-owned slots leave the pool entirely. The lease alone was not enough:
      // a slot declaring no affinity is eligible for every project, so any terminal
      // opened while the daemon was down claimed the operator's daemon bot and held
      // it for the session's whole life — the daemon then refuses to steal it back,
      // which is exactly how the newest stack kept serving from an old in-session
      // poller. `acquireLeaseForSlot` still claims one by name for the daemon itself.
      const poolSlots = slots.filter(s => s.daemon !== true);

      // Configuration-driven project affinity: a session may claim only a slot
      // whose declared projects/preferredProjects covers its project (via glob match or empty/wildcard),
      // or a slot that declares no affinity at all (shared pool slot).
      // Specific affinity-matched slots are tried first so dedicated project bots
      // are consumed before shared/wildcard slots.
      const eligibleSlots = poolSlots.filter(s => isSlotEligibleForProject(s.projects ?? s.preferredProjects, projectCwd));
      const sortedSlots = [...eligibleSlots].sort((a, b) => {
        const aMatches = slotHasSpecificAffinity(a.projects ?? a.preferredProjects, projectCwd);
        const bMatches = slotHasSpecificAffinity(b.projects ?? b.preferredProjects, projectCwd);
        if (aMatches && !bMatches) return -1;
        if (!aMatches && bMatches) return 1;
        return 0;
      });

      const busyHolders: BusySlotHolder[] = [];

      for (const slot of sortedSlots) {
        const result = this.claimSlot(slot, sessionId, projectCwd, ownerPid, procStartStr);
        if (result.claimed) return { ok: true, slot };
        if (result.holder) busyHolders.push(result.holder);
      }

      if (waitTimeoutMs <= 0 || Date.now() - startTime >= waitTimeoutMs) {
        let reason: string;
        if (eligibleSlots.length === 0) {
          const daemonOwned = slots.length - poolSlots.length;
          const daemonNote = daemonOwned > 0 ? `, ${daemonOwned} owned by the standalone daemon` : "";
          reason = `No Telegram bot slot declares affinity for this project (${slots.length} slots in pool${daemonNote}, none eligible for cwd '${projectCwd}').`;
        } else if (busyHolders.length > 0) {
          const details = busyHolders
            .map(h => {
              const parts = [
                h.sessionId ? `session '${h.sessionId}'` : undefined,
                h.projectPath ? `cwd '${h.projectPath}'` : undefined,
                h.ownerPid ? `pid ${h.ownerPid}` : undefined,
              ].filter(Boolean).join(", ");
              return `slot '${h.slotId}' leased by ${parts || "unknown holder"}`;
            })
            .join("; ");
          reason = `All ${eligibleSlots.length} Telegram bot slot(s) eligible for this project are currently in use: ${details}.`;
        } else {
          reason = `All ${eligibleSlots.length} Telegram bot slots eligible for this project are currently in use.`;
        }

        return {
          ok: false,
          error: "POOL_EXHAUSTED",
          reason,
          busyHolders: busyHolders.length > 0 ? busyHolders : undefined,
        };
      }

      // Wait with backoff before retry
      await Bun.sleep(1000);
    }
  }

  /**
   * Re-points this process's ACTIVE lease at a new session id.
   *
   * Returns true only when a lease row was actually re-pointed. A lease reclaimed
   * after TTL expiry by another root no longer matches, so the caller learns it has
   * lost the channel instead of continuing to poll a bot it does not own and binding
   * outbound messages to a session the pool has already reassigned.
   */
  public updateLeaseSession(
    slotId: string,
    newSessionId: string,
    projectCwd: string,
    ownerPid: number = process.pid,
  ): boolean {
    this.ensureDbOpen();
    try {
      const now = Date.now() / 1000;
      const result = this.db.run(
        "UPDATE bot_leases SET session_id = ?, project_path = ?, heartbeat_at = ? WHERE slot_id = ? AND owner_pid = ? AND lease_status = 'ACTIVE'",
        [newSessionId, projectCwd, now, slotId, ownerPid],
      );
      if (result.changes < 1) {
        return false;
      }
      this.startHeartbeat(slotId, newSessionId);
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Persists the identity of an outbound Telegram message so a later reply can be
   * bound back to the session that produced it. Keyed on (botId, chatId, messageId).
   */
  public recordOutboundMessage(correlation: OutboundMessageCorrelation): boolean {
    this.ensureDbOpen();
    try {
      this.db.run(
        `
        INSERT INTO message_correlations (
          bot_id, chat_id, message_id, slot_id, session_id,
          request_id, decision_id, project_path, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bot_id, chat_id, message_id) DO UPDATE SET
          slot_id = excluded.slot_id,
          session_id = excluded.session_id,
          request_id = excluded.request_id,
          decision_id = excluded.decision_id,
          project_path = excluded.project_path,
          created_at = excluded.created_at;
      `,
        [
          correlation.botId,
          correlation.chatId,
          correlation.messageId,
          correlation.slotId,
          correlation.sessionId,
          correlation.requestId,
          correlation.decisionId,
          correlation.projectPath,
          correlation.createdAt,
        ],
      );
      this.touchHeartbeat(correlation.slotId, correlation.sessionId);
      return true;
    } catch {
      return false;
    }
  }

  public lookupOutboundCorrelation(
    botId: string,
    chatId: string,
    messageId: number,
  ): OutboundMessageCorrelation | null {
    this.ensureDbOpen();
    let row: CorrelationRow | null = null;
    try {
      row = this.db
        .query("SELECT * FROM message_correlations WHERE bot_id = ? AND chat_id = ? AND message_id = ?")
        .get(botId, chatId, messageId) as CorrelationRow | null;
    } catch {
      return null;
    }
    if (!row) return null;

    return {
      botId: row.bot_id,
      chatId: row.chat_id,
      messageId: row.message_id,
      slotId: row.slot_id,
      sessionId: row.session_id,
      requestId: row.request_id,
      decisionId: row.decision_id,
      projectPath: row.project_path,
      createdAt: row.created_at,
    };
  }

  /**
   * Fail-closed reply routing. An inbound reply is delivered only when its target
   * message is a known outbound message of the session asking for it; a reply to an
   * unknown, unbound, or foreign-session message is refused rather than injected into
   * whichever session currently holds the bot lease.
   */
  public resolveReplyRouting(
    botId: string,
    chatId: string,
    replyToMessageId: number,
    expectedSessionId: string,
  ): ReplyRoutingResolution {
    const correlation = this.lookupOutboundCorrelation(botId, chatId, replyToMessageId);
    if (!correlation) {
      return {
        decision: "reject_unknown",
        detail: "No outbound message correlation recorded for this bot, chat, and message.",
      };
    }
    if (!correlation.sessionId) {
      return {
        decision: "reject_unbound",
        correlation,
        detail: "Correlated message carries no originating session binding.",
      };
    }
    if (correlation.sessionId !== expectedSessionId) {
      return {
        decision: "reject_foreign_session",
        correlation,
        detail: "Correlated message belongs to a different session than the one holding this channel.",
      };
    }
    if (correlation.slotId && correlation.sessionId) {
      this.touchHeartbeat(correlation.slotId, correlation.sessionId);
    }
    return {
      decision: "deliver",
      correlation,
      detail: "Reply matches an outbound message of the active session.",
    };
  }

  public recordDecisionCallback(record: DecisionCallbackRecord): boolean {
    this.ensureDbOpen();
    try {
      this.db.run(
        `INSERT INTO decision_callbacks (
          callback_token, decision_id, choice_id, session_id,
          chat_id, user_id, question_hash, expires_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(callback_token) DO NOTHING;`,
        [
          record.callbackToken,
          record.decisionId,
          record.choiceId,
          record.sessionId,
          record.chatId,
          record.userId,
          record.questionHash,
          record.expiresAt,
          record.createdAt,
        ],
      );
      return true;
    } catch {
      return false;
    }
  }

  public lookupDecisionCallback(callbackToken: string): DecisionCallbackRecord | null {
    this.ensureDbOpen();
    try {
      const row = this.db
        .query("SELECT * FROM decision_callbacks WHERE callback_token = ?")
        .get(callbackToken) as Record<string, unknown> | null;
      if (!row) return null;
      return {
        callbackToken: String(row.callback_token),
        decisionId: String(row.decision_id),
        choiceId: String(row.choice_id),
        sessionId: String(row.session_id),
        chatId: String(row.chat_id),
        userId: String(row.user_id),
        questionHash: String(row.question_hash),
        expiresAt: Number(row.expires_at),
        consumedAt: row.consumed_at != null ? Number(row.consumed_at) : null,
        createdAt: Number(row.created_at),
      };
    } catch {
      return null;
    }
  }

  public consumeDecisionCallback(callbackToken: string, now?: number): boolean {
    this.ensureDbOpen();
    try {
      const nowTs = now ?? Date.now() / 1000;
      const res = this.db.run(
        "UPDATE decision_callbacks SET consumed_at = ? WHERE callback_token = ? AND consumed_at IS NULL",
        [nowTs, callbackToken],
      );
      return res.changes > 0;
    } catch {
      return false;
    }
  }

  public validateDecisionCallback(
    callbackToken: string,
    userId: string,
    chatId: string,
    expectedSessionId: string,
    decisionsPath?: string,
  ): DecisionCallbackResolution {
    const record = this.lookupDecisionCallback(callbackToken);
    if (!record) {
      return {
        decision: "reject_unknown",
        detail: "Callback token not recognized or forged.",
      };
    }

    if (record.userId !== String(userId) || record.chatId !== String(chatId)) {
      return {
        decision: "reject_unauthorized",
        record,
        detail: "Callback user or chat does not match authorized recipient.",
      };
    }

    if (record.sessionId !== expectedSessionId) {
      return {
        decision: "reject_foreign_session",
        record,
        detail: `Callback is bound to session '${record.sessionId}', but active session is '${expectedSessionId}'.`,
      };
    }

    const now = Date.now() / 1000;
    if (now > record.expiresAt) {
      return {
        decision: "reject_expired",
        record,
        detail: "Decision callback has expired.",
      };
    }

    if (record.consumedAt !== null) {
      return {
        decision: "reject_already_consumed",
        record,
        detail: "Decision choice was already submitted and consumed.",
      };
    }

    // Check canonical decisions file if available
    const decFile = decisionsPath || path.join(os.homedir(), ".veyyon", "workflows", "decisions.json");
    if (fs.existsSync(decFile)) {
      try {
        const content = fs.readFileSync(decFile, "utf8");
        const data = JSON.parse(content);
        const dec = data?.decisions?.[record.decisionId];
        if (dec && (dec.status === "answered" || dec.answer != null)) {
          return {
            decision: "reject_already_answered",
            record,
            detail: `Decision '${record.decisionId}' is already answered in canonical ledger.`,
          };
        }
      } catch {}
    }

    return {
      decision: "deliver",
      record,
      detail: "Callback identity, session binding, and expiry verified.",
    };
  }

  private startHeartbeat(slotId: string, sessionId: string): void {
    this.stopHeartbeat(slotId);

    const timer = setInterval(() => {
      try {
        this.ensureDbOpen();
        const now = Date.now() / 1000;
        this.db.run(
          "UPDATE bot_leases SET heartbeat_at = ? WHERE slot_id = ? AND session_id = ? AND lease_status = 'ACTIVE'",
          [now, slotId, sessionId],
        );
      } catch {}
    }, HEARTBEAT_INTERVAL_MS);

    if (typeof timer.unref === "function") {
      timer.unref();
    }

    this.activeHeartbeatTimers.set(slotId, timer);
  }

  public touchHeartbeat(slotId: string, sessionId: string): void {
    try {
      this.ensureDbOpen();
      const now = Date.now() / 1000;
      this.db.run(
        "UPDATE bot_leases SET heartbeat_at = ? WHERE slot_id = ? AND session_id = ? AND lease_status = 'ACTIVE'",
        [now, slotId, sessionId],
      );
    } catch {}
  }

  private stopHeartbeat(slotId: string): void {
    const existing = this.activeHeartbeatTimers.get(slotId);
    if (existing) {
      clearInterval(existing);
      this.activeHeartbeatTimers.delete(slotId);
    }
  }

  public releaseLease(slotId: string, sessionId: string, ownerPid: number = process.pid): boolean {
    this.stopHeartbeat(slotId);
    this.ensureDbOpen();

    let released = false;
    try {
      this.db.run(
        "UPDATE bot_leases SET lease_status = 'RELEASED' WHERE slot_id = ? AND session_id = ?",
        [slotId, sessionId],
      );
      released = true;
    } catch {}

    // Clean PID files if we own them
    const slot = this.syncSlots().find(s => s.slotId === slotId);
    if (slot) {
      const veyyonPidPath = path.join(slot.stateDir, "veyyon-bot.pid");
      const botPidPath = path.join(slot.stateDir, "bot.pid");

      try {
        if (fs.existsSync(veyyonPidPath)) {
          const content = fs.readFileSync(veyyonPidPath, "utf8").trim();
          if (content === String(ownerPid)) {
            fs.unlinkSync(veyyonPidPath);
          }
        }
      } catch {}

      try {
        if (fs.existsSync(botPidPath)) {
          const content = fs.readFileSync(botPidPath, "utf8").trim();
          if (content === String(ownerPid)) {
            fs.unlinkSync(botPidPath);
          }
        }
      } catch {}
    }

    return released;
  }

  public forceReleaseStaleLease(slotId: string): boolean {
    this.stopHeartbeat(slotId);
    this.ensureDbOpen();

    const slot = this.syncSlots().find(s => s.slotId === slotId);
    if (!slot) return false;

    try {
      this.db.run("UPDATE bot_leases SET lease_status = 'RELEASED' WHERE slot_id = ?", [slotId]);
    } catch {}

    const veyyonPidPath = path.join(slot.stateDir, "veyyon-bot.pid");
    const botPidPath = path.join(slot.stateDir, "bot.pid");

    try {
      if (fs.existsSync(veyyonPidPath)) fs.unlinkSync(veyyonPidPath);
    } catch {}

    try {
      if (fs.existsSync(botPidPath)) fs.unlinkSync(botPidPath);
    } catch {}

    return true;
  }

  public getPoolStatus(): PoolStatusSummary {
    const slots = this.syncSlots();
    let activeLeases = 0;
    let freeSlots = 0;

    const slotSummaries = slots.map(slot => {
      const lease = this.getSlotLease(slot.slotId);
      const claudeConflict = this.checkPidFileConflict(slot.stateDir, CLAUDE_PID_FILES, 0);

      let veyyonPid: number | null = null;
      const veyyonPidPath = path.join(slot.stateDir, "veyyon-bot.pid");
      if (fs.existsSync(veyyonPidPath)) {
        try {
          const text = fs.readFileSync(veyyonPidPath, "utf8").trim();
          const p = Number.parseInt(text, 10);
          if (Number.isFinite(p)) veyyonPid = p;
        } catch {}
      }

      const busyCheck = this.isSlotBusy(slot, 0, "");
      if (busyCheck.busy) {
        activeLeases++;
      } else {
        freeSlots++;
      }

      return {
        slotId: slot.slotId,
        stateDir: slot.stateDir,
        botId: slot.botId,
        fingerprint: slot.fingerprint,
        preferredProjects: slot.preferredProjects,
        enabled: slot.enabled,
        lease,
        claudePid: claudeConflict.pid,
        veyyonPid,
        isBusy: busyCheck.busy,
        busyReason: busyCheck.reason,
      };
    });

    return {
      manifestPath: this.manifestPath,
      dbPath: this.dbPath,
      totalSlots: slots.length,
      enabledSlots: slots.filter(s => s.enabled).length,
      activeLeases,
      freeSlots,
      slots: slotSummaries,
    };
  }

  public close(): void {
    for (const slotId of Array.from(this.activeHeartbeatTimers.keys())) {
      this.stopHeartbeat(slotId);
    }
    try {
      this.db.close();
    } catch {}
  }
}
