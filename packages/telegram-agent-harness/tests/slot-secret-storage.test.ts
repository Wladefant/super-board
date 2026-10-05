import { test, expect, afterEach } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { spawnSync } from "node:child_process";
import { DaemonStore } from "../daemon/store";
import { getDaemonSecret, getSlotSecretFilePath } from "../daemon/lane-panel";

const cleanup: Array<() => void> = [];
afterEach(() => {
  for (const fn of cleanup.splice(0)) {
    try { fn(); } catch {}
  }
});

test("getDaemonSecret creates stable per-slot secret FILE with O_EXCL and never overwrites", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "slot-secret-test-"));
  const dbPath = path.join(dir, "daemon.db");
  const store = new DaemonStore(dbPath);
  cleanup.push(() => {
    store.close();
    fs.rmSync(dir, { recursive: true, force: true });
  });

  const slotId = "slot-alpha";
  const secret1 = getDaemonSecret(store, slotId);
  expect(secret1).toBeInstanceOf(Buffer);
  expect(secret1.length).toBe(32);

  // File exists on disk
  const secretFile = getSlotSecretFilePath(dir, slotId);
  expect(fs.existsSync(secretFile)).toBe(true);
  const fileContent = fs.readFileSync(secretFile, "utf8").trim();
  expect(fileContent.length).toBe(64); // 64 hex characters = 32 bytes
  expect(Buffer.from(fileContent, "hex")).toEqual(secret1);

  // Second call returns the EXACT same secret and does not overwrite the file
  const secret2 = getDaemonSecret(store, slotId);
  expect(secret2).toEqual(secret1);

  // Verify another slot gets its own independent secret file
  const slotBeta = "slot-beta";
  const secretBeta = getDaemonSecret(store, slotBeta);
  expect(secretBeta.length).toBe(32);
  expect(secretBeta).not.toEqual(secret1);

  const betaFile = getSlotSecretFilePath(dir, slotBeta);
  expect(fs.existsSync(betaFile)).toBe(true);
  expect(betaFile).not.toBe(secretFile);

  // Live DB KV table MUST NOT contain the secret (do not touch/store in live DB)
  expect(store.getKv(`lanepanel:${slotId}:secret`)).toBeNull();
  expect(store.getKv(`lanepanel:daemon:secret`)).toBeNull();
  expect(store.getKv(`lanepanel:slot:secret`)).toBeNull();
});

test("per-slot secret file enforces restricted permissions (Windows owner-only ACL or POSIX 0600)", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "slot-perm-test-"));
  const dbPath = path.join(dir, "daemon.db");
  const store = new DaemonStore(dbPath);
  cleanup.push(() => {
    store.close();
    fs.rmSync(dir, { recursive: true, force: true });
  });

  const slotId = "slot-secure";
  getDaemonSecret(store, slotId);
  const secretFile = getSlotSecretFilePath(dir, slotId);
  const secretsDir = path.dirname(secretFile);

  if (process.platform === "win32") {
    // Check icacls output on Windows
    const fileAcl = spawnSync("icacls.exe", [secretFile], { encoding: "utf8" });
    expect(fileAcl.status).toBe(0);
    // Inheritance is removed (/inheritance:r)
    expect(fileAcl.stdout.includes("(I)")).toBe(false);
    // Only current user has access
    const currentUser = process.env.USERNAME || "";
    if (currentUser) {
      expect(fileAcl.stdout.toLowerCase().includes(currentUser.toLowerCase())).toBe(true);
    }

    const dirAcl = spawnSync("icacls.exe", [secretsDir], { encoding: "utf8" });
    expect(dirAcl.status).toBe(0);
    expect(dirAcl.stdout.includes("(I)")).toBe(false);
  } else {
    // POSIX mode check
    const fileStat = fs.statSync(secretFile);
    expect(fileStat.mode & 0o777).toBe(0o600);
    const dirStat = fs.statSync(secretsDir);
    expect(dirStat.mode & 0o777).toBe(0o700);
  }
});

test("atomic concurrent creation across racing callers returns identical secret without overwrites", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "slot-race-test-"));
  const dbPath = path.join(dir, "daemon.db");
  const store = new DaemonStore(dbPath);
  cleanup.push(() => {
    store.close();
    fs.rmSync(dir, { recursive: true, force: true });
  });

  const slotId = "slot-concurrent";
  // Run 10 concurrent getDaemonSecret calls
  const promises = Array.from({ length: 10 }, () => Promise.resolve().then(() => getDaemonSecret(store, slotId)));
  const results = await Promise.all(promises);

  const firstSecret = results[0];
  expect(firstSecret.length).toBe(32);
  for (const secret of results) {
    expect(secret).toEqual(firstSecret);
  }

  const secretFile = getSlotSecretFilePath(dir, slotId);
  expect(fs.existsSync(secretFile)).toBe(true);
  const fileContent = fs.readFileSync(secretFile, "utf8").trim();
  expect(Buffer.from(fileContent, "hex")).toEqual(firstSecret);
}, 90000);

test("fails closed if permission enforcement or directory protection fails, leaving no secret file", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "slot-fail-closed-"));
  // Create a file where the secrets directory should be, making mkdir fail
  const secretsDirAsFile = path.join(dir, "secrets");
  fs.writeFileSync(secretsDirAsFile, "blocker");
  cleanup.push(() => {
    fs.rmSync(dir, { recursive: true, force: true });
  });

  const fakeStore = { stateDir: dir };
  expect(() => getDaemonSecret(fakeStore, "slot-blocked")).toThrow();

  // Verify no secret was stored
  const secretFile = path.join(secretsDirAsFile, "slot-blocked.secret");
  expect(fs.existsSync(secretFile)).toBe(false);
});

test("existing malformed keys fail closed and slot identities cannot alias", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "slot-invalid-key-"));
  cleanup.push(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = getSlotSecretFilePath(dir, "a/b");
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, "z".repeat(64));
  expect(() => getDaemonSecret({ stateDir: dir }, "a/b")).toThrow("Invalid daemon signing secret");
  expect(fs.readFileSync(file, "utf8")).toBe("z".repeat(64));
  expect(getSlotSecretFilePath(dir, "a_b")).not.toBe(file);
});

test("existing valid keys lose broad explicit grants before read", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "slot-existing-acl-"));
  cleanup.push(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = getSlotSecretFilePath(dir, "existing");
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, "ab".repeat(32), { mode: 0o666 });
  if (process.platform === "win32") {
    const grant = spawnSync("icacls.exe", [file, "/grant", "*S-1-1-0:(R)"], { encoding: "utf8", timeout: 15000 });
    expect(grant.status).toBe(0);
  } else fs.chmodSync(file, 0o666);
  expect(getDaemonSecret({ stateDir: dir }, "existing")).toEqual(Buffer.from("ab".repeat(32), "hex"));
  if (process.platform === "win32") {
    const acl = spawnSync("icacls.exe", [file], { encoding: "utf8", timeout: 15000 });
    expect(acl.status).toBe(0);
    expect(acl.stdout).not.toContain("Everyone");
    expect(acl.stdout).not.toContain("(I)");
  } else expect(fs.statSync(file).mode & 0o777).toBe(0o600);
});
