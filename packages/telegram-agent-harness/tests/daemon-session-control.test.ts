// Real local-socket transport tests. These are not model or Telegram live-delivery evidence.
import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { spawn } from "node:child_process";
import { cachedOwners, discoverOwners, ownerIdentityMatches, TerminalSessionControl, type SessionEvent } from "../daemon/session-control";

const cleanup: (() => void)[] = [];
afterEach(() => { for (const close of cleanup.splice(0).reverse()) close(); });

async function owner(root: string, sessionId: string) {
  const token = (crypto.randomUUID() + crypto.randomUUID()).replaceAll("-", "");
  const nonce = crypto.randomUUID();
  const endpoint = process.platform === "win32" ? `\\\\.\\pipe\\veyyon-terminal-${nonce}` : path.join(root, `${nonce}.sock`);
  const received: string[] = [];
  const sockets = new Set<net.Socket>();
  const server = net.createServer(socket => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    socket.on("error", () => {});
    socket.setEncoding("utf8");
    let buffer = "";
    socket.on("data", chunk => {
      buffer += chunk;
      while (buffer.includes("\n")) {
        const end = buffer.indexOf("\n");
        const request = JSON.parse(buffer.slice(0, end));
        buffer = buffer.slice(end + 1);
        if (request.token !== token || request.sessionId !== sessionId) { socket.destroy(); return; }
        if (request.op === "subscribe") socket.write(JSON.stringify({ event: { kind: "history", sessionId, entries: [{ entryId: "old", text: "Existing response" }] } }) + "\n");
        if (request.op === "deliver") received.push(request.text);
        socket.write(JSON.stringify({ id: request.id, ok: true, result: request.op === "deliver" ? "started" : true }) + "\n");
      }
    });
  });
  const ready = Promise.withResolvers<void>();
  server.once("error", ready.reject);
  server.listen(endpoint, ready.resolve);
  await ready.promise;
  cleanup.push(() => { for (const socket of sockets) socket.destroy(); server.close(); });
  const directory = path.join(root, "run", "terminals");
  fs.mkdirSync(directory, { recursive: true });
  const recordPath = path.join(directory, `${sessionId}.json`);
  const record = { version: 1, sessionId, pid: process.pid, cwd: root, sessionFile: path.join(root, `${sessionId}.jsonl`), endpoint, token };
  fs.writeFileSync(recordPath, JSON.stringify(record));
  return { record, recordPath, received };
}

function context() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "terminal-transport-"));
  cleanup.push(() => fs.rmSync(root, { recursive: true, force: true }));
  const events: SessionEvent[] = [];
  const control = new TerminalSessionControl({ configRoot: root, onEvent: event => events.push(event), onLog: () => {} });
  cleanup.push(() => control.close());
  return { root, events, control };
}

test("same-workspace owners remain separate and deliver authentic subscriptions to the exact ID", async () => {
  const { root, events, control } = context();
  const first = await owner(root, "first");
  const second = await owner(root, "second");
  expect((await control.listSessions()).map(session => session.id).sort()).toEqual(["first", "second"]);
  await expect(control.findSession(root)).rejects.toThrow();
  expect(await control.deliver("first", "nonce")).toBe("started");
  expect(first.received).toEqual(["nonce"]);
  expect(second.received).toEqual([]);
  expect(events).toContainEqual({ kind: "history", sessionId: "first", entries: [{ entryId: "old", text: "Existing response" }] });
});

test("missing or duplicate owners fail closed without opening or creating a session", async () => {
  const { root, control } = context();
  await expect(control.deliver("missing", "nonce")).rejects.toThrow();
  await expect(control.createSession(root, "no fallback")).rejects.toThrow();
  const first = await owner(root, "duplicate");
  fs.writeFileSync(path.join(path.dirname(first.recordPath), "other.json"), JSON.stringify(first.record));
  expect(await control.listSessions()).toEqual([]);
  await expect(control.deliver("duplicate", "nonce")).rejects.toThrow();
  expect(first.received).toEqual([]);
});

test("incorrect discovery credentials cannot deliver to an owner", async () => {
  const { root, control } = context();
  const first = await owner(root, "protected");
  fs.writeFileSync(first.recordPath, JSON.stringify({ ...first.record, token: "f".repeat(64) }));
  await expect(control.deliver("protected", "nonce")).rejects.toThrow();
  expect(first.received).toEqual([]);
});

function publish(root: string, name: string, pid: number, extra: Record<string, unknown> = {}) {
  const directory = path.join(root, "run", "terminals");
  fs.mkdirSync(directory, { recursive: true });
  const nonce = crypto.randomUUID();
  const endpoint = process.platform === "win32" ? `\\\\.\\pipe\\veyyon-terminal-${nonce}` : path.join(root, `${nonce}.sock`);
  const token = (crypto.randomUUID() + crypto.randomUUID()).replaceAll("-", "");
  const file = path.join(directory, `${name}.json`);
  fs.writeFileSync(file, JSON.stringify({ version: 1, sessionId: name, pid, cwd: root, sessionFile: path.join(root, `${name}.jsonl`), endpoint, token, ...extra }));
  return file;
}

function idleChild() {
  const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore", windowsHide: true });
  cleanup.push(() => { child.kill(); });
  return child;
}

test("a stale owner file whose PID was recycled by another live process is not an owner", async () => {
  const { root } = context();
  const stranger = idleChild();
  const file = publish(root, "recycled", stranger.pid!);
  const hourAgo = new Date(Date.now() - 3_600_000);
  fs.utimesSync(file, hourAgo, hourAgo);
  expect(await discoverOwners(root, 0)).toEqual([]);
  // Same stale PID with a recorded start time that differs from the real process is also rejected.
  publish(root, "recorded", stranger.pid!, { startedAtMs: Date.now() - 3_600_000 });
  expect(await discoverOwners(root, 0)).toEqual([]);
});

test("a genuine single owner is discovered, with or without a recorded start time", async () => {
  const { root } = context();
  publish(root, "legacy", process.pid);
  expect((await discoverOwners(root, 0)).map(o => o.sessionId)).toEqual(["legacy"]);
  publish(root, "legacy", process.pid, { startedAtMs: Math.round(performance.timeOrigin) });
  expect((await discoverOwners(root, 0)).map(o => o.sessionId)).toEqual(["legacy"]);
});

test("two genuine owners for one session stay ambiguous while a recycled file beside them is ignored", async () => {
  const { root, control } = context();
  const first = await owner(root, "shared");
  fs.writeFileSync(path.join(path.dirname(first.recordPath), "second.json"), JSON.stringify(first.record));
  const stranger = idleChild();
  const stale = publish(root, "stale-shared", stranger.pid!, { sessionId: "shared" });
  const hourAgo = new Date(Date.now() - 3_600_000);
  fs.utimesSync(stale, hourAgo, hourAgo);
  expect((await discoverOwners(root, 0)).length).toBe(2);
  await expect(control.deliver("shared", "nonce")).rejects.toThrow();
  expect(first.received).toEqual([]);
});

test("owner identity falls back to PID existence only when the OS reports no start time", () => {
  expect(ownerIdentityMatches({}, 0, undefined)).toBe(true);
  expect(ownerIdentityMatches({}, 1_000, 10_000)).toBe(false);
  expect(ownerIdentityMatches({}, 10_000, 10_000)).toBe(true);
});

test("a scan is shared within its max age and fresh when asked", async () => {
  const { root } = context();
  publish(root, "first", process.pid);
  expect((await discoverOwners(root)).map(o => o.sessionId)).toEqual(["first"]);
  publish(root, "second", process.pid);
  // Within the cache window the second file is not seen yet; a forced scan sees both.
  expect((await discoverOwners(root)).map(o => o.sessionId)).toEqual(["first"]);
  expect((await discoverOwners(root, 0)).map(o => o.sessionId).sort()).toEqual(["first", "second"]);
});

test("concurrent callers share one scan", async () => {
  const { root } = context();
  publish(root, "only", process.pid);
  const [a, b] = await Promise.all([discoverOwners(root, 0), discoverOwners(root)]);
  expect(b).toBe(a);
});

test("cachedOwners answers without I/O and fills in after the first scan", async () => {
  const { root } = context();
  publish(root, "warm", process.pid);
  expect(cachedOwners(root)).toEqual([]);
  await discoverOwners(root);
  expect(cachedOwners(root).map(o => o.sessionId)).toEqual(["warm"]);
});

test("a scan prunes the owner file of a dead process and keeps the live one", async () => {
  const { root } = context();
  const child = idleChild();
  const deadPid = child.pid!;
  child.kill();
  await new Promise(resolve => child.once("exit", resolve));
  const dead = publish(root, "dead", deadPid);
  const live = publish(root, "alive", process.pid);
  expect((await discoverOwners(root, 0)).map(o => o.sessionId)).toEqual(["alive"]);
  expect(fs.existsSync(dead)).toBe(false);
  expect(fs.existsSync(live)).toBe(true);
});

test("a scan over many stale owner files never stalls the event loop", async () => {
  const { root } = context();
  const child = idleChild();
  const deadPid = child.pid!;
  child.kill();
  await new Promise(resolve => child.once("exit", resolve));
  for (let index = 0; index < 60; index++) publish(root, `stale${index}`, deadPid);
  publish(root, "alive", process.pid);
  let longest = 0;
  let last = performance.now();
  const ticker = setInterval(() => { const now = performance.now(); longest = Math.max(longest, now - last); last = now; }, 5);
  const owners = await discoverOwners(root, 0);
  clearInterval(ticker);
  expect(owners.map(o => o.sessionId)).toEqual(["alive"]);
  expect(longest).toBeLessThan(250);
});
