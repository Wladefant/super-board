// Real local-socket transport tests. These are not model or Telegram live-delivery evidence.
import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { TerminalSessionControl, type SessionEvent } from "../daemon/session-control";

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
