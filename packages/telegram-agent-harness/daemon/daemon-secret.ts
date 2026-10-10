/**
 * daemon-secret.ts — per-slot HMAC signing secrets shared by the daemon and the extension.
 *
 * Each bot slot signs its callback tokens with a 32-byte secret kept at
 * `<daemon stateDir>/secrets/<sha256(slotId)>.secret`. The daemon and every session's
 * extension process read the same file, so a token signed by one validates in the other.
 * The file is created once, owner-only (Windows ACL or 0600), and read with a short retry.
 */

import { createHash, randomBytes } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
import { getDaemonRunDir } from "./config";

export interface DaemonStoreLike {
  stateDir?: string;
  dbPath?: string;
  db?: { filename?: string };
  getKv?: (key: string) => string | null;
  setKv?: (key: string, value: string) => void;
}

export function deriveDaemonStateDir(store?: unknown): string {
  if (store && typeof store === "object") {
    if ("stateDir" in store && typeof store.stateDir === "string" && store.stateDir.length > 0) {
      return store.stateDir;
    }
    if ("dbPath" in store && typeof store.dbPath === "string" && store.dbPath.length > 0) {
      return path.dirname(store.dbPath);
    }
    if ("db" in store && store.db && typeof store.db === "object" && "filename" in store.db) {
      const filename = store.db.filename;
      if (typeof filename === "string" && filename.length > 0 && filename !== ":memory:") {
        return path.dirname(filename);
      }
    }
  }
  return process.env.VEYYON_TELEGRAM_DAEMON_DIR || getDaemonRunDir();
}

export function getSlotSecretFilePath(stateDir: string, slotId: string): string {
  const identity = createHash("sha256").update(slotId).digest("hex");
  return path.join(stateDir, "secrets", `${identity}.secret`);
}


function enforceWindowsOwnerAcl(target: string, directory: boolean): void {
  const script = `
    $ErrorActionPreference = 'Stop'
    $target = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${Buffer.from(target).toString("base64")}'))
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $item = Get-Item -LiteralPath $target
    $acl = $item.GetAccessControl([Security.AccessControl.AccessControlSections]::Access)
    foreach ($old in @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))) { [void]$acl.RemoveAccessRuleSpecific($old) }
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', '${directory ? "ContainerInherit, ObjectInherit" : "None"}', 'None', 'Allow')
    $acl.AddAccessRule($rule)
    $item.SetAccessControl($acl)
    $actual = Get-Acl -LiteralPath $target
    if (!$actual.AreAccessRulesProtected) { throw 'Unprotected secret ACL' }
    $rules = @($actual.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if ($rules.Count -ne 1 -or $rules[0].IdentityReference -ne $sid -or $rules[0].AccessControlType -ne 'Allow' -or $rules[0].FileSystemRights -ne 'FullControl') { throw 'Unexpected secret ACL' }
  `;
  const result = spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64")],
    { windowsHide: true, encoding: "utf8", timeout: 15_000 });
  if (result.error || result.status !== 0) throw new Error("Cannot enforce owner-only daemon secret ACL");
}

function protectSecretsDirectory(secretsDir: string): void {
  if (process.platform === "win32") {
    enforceWindowsOwnerAcl(secretsDir, true);
  } else {
    try {
      fs.chmodSync(secretsDir, 0o700);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      throw new Error(`Failed to enforce 0700 permissions on secrets directory ${secretsDir}: ${message}`);
    }
  }
}

function protectSecretFile(filePath: string, fd?: number): void {
  if (process.platform === "win32") {
    enforceWindowsOwnerAcl(filePath, false);
  } else {
    try {
      if (typeof fd === "number") {
        fs.fchmodSync(fd, 0o600);
      } else {
        fs.chmodSync(filePath, 0o600);
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      throw new Error(`Failed to enforce 0600 permissions on secret file ${filePath}: ${message}`);
    }
  }
}

function parseSecretBuffer(buf: Buffer): Buffer {
  const str = buf.toString("utf8");
  if (!/^[0-9a-fA-F]{64}$/.test(str)) throw new Error("Invalid daemon signing secret encoding");
  const secret = Buffer.from(str, "hex");
  if (secret.length !== 32) throw new Error("Invalid daemon signing secret length");
  return secret;
}

function readSecretFileWithRetry(filePath: string): Buffer {
  const start = Date.now();
  while (Date.now() - start < 3000) {
    try {
      if (fs.existsSync(filePath)) {
        protectSecretFile(filePath);
        const data = fs.readFileSync(filePath);
        if (data.length >= 32) {
          return parseSecretBuffer(data);
        }
      }
    } catch (e: unknown) {
      if (!(e && typeof e === "object" && "code" in e && e.code === "ENOENT")) throw e;
    }
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);
  }
  const finalData = fs.readFileSync(filePath);
  if (finalData.length < 32) {
    throw new Error(`Secret file at ${filePath} is incomplete or empty`);
  }
  return parseSecretBuffer(finalData);
}

export function getDaemonSecret(
  store?: Pick<DaemonStoreLike, "getKv" | "setKv"> | DaemonStoreLike | unknown,
  slotId = "daemon",
): Buffer {
  const stateDir = deriveDaemonStateDir(store);
  const secretPath = getSlotSecretFilePath(stateDir, slotId);
  const secretsDir = path.dirname(secretPath);

  fs.mkdirSync(secretsDir, { recursive: true, mode: 0o700 });
  protectSecretsDirectory(secretsDir);
  if (fs.existsSync(secretPath)) {
    protectSecretFile(secretPath);
    return readSecretFileWithRetry(secretPath);
  }

  let fd: number | null = null;
  try {
    fd = fs.openSync(secretPath, "wx", 0o600);
  } catch (err: unknown) {
    if (err && typeof err === "object" && "code" in err && err.code === "EEXIST") {
      return readSecretFileWithRetry(secretPath);
    }
    throw err;
  }

  try {
    protectSecretFile(secretPath, fd);
    const secretHex = randomBytes(32).toString("hex");
    fs.writeSync(fd, secretHex);
    fs.closeSync(fd);
    fd = null;
    return Buffer.from(secretHex, "hex");
  } catch (err) {
    if (fd !== null) {
      try { fs.closeSync(fd); } catch {}
      fd = null;
    }
    try { fs.unlinkSync(secretPath); } catch {}
    throw err;
  }
}
