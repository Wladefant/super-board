// Real local-socket tests for a session that is slow to answer a delivery (Wladefant/veyyon#505).
import { afterEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { TerminalSessionControl } from "../daemon/session-control";

const cleanup: (() => void)[] = [];
afterEach(() => { for (const close of cleanup.splice(0).reverse()) close(); });

type Request = { id: string; op: string; text?: string; ack?: boolean; messageId?: string };

/** `onDeliver` decides what the fake session writes for each deliver request; it may write later. */
async function session(root: string, sessionId: string, onDeliver: (request: Request, reply: (frame: object) => void) => void) {
  const token = (crypto.randomUUID() + crypto.randomUUID()).replaceAll("-", "");
  const nonce = crypto.randomUUID();
  const endpoint = process.platform === "win32" ? `\\\\.\\pipe\\veyyon-terminal-${nonce}` : path.join(root, `${nonce}.sock`);
  const received: Request[] = [];
  const sockets = new Set<net.Socket>();
  const closed: boolean[] = [];
  const server = net.createServer(socket => {
    sockets.add(socket);
    socket.on("close", () => { sockets.delete(socket); closed.push(true); });
    socket.on("error", () => {});
    socket.setEncoding("utf8");
    let buffer = "";
    const reply = (frame: object) => socket.write(JSON.stringify(frame) + "\n");
    socket.on("data", chunk => {
      buffer += chunk;
      while (buffer.includes("\n")) {
        const end = buffer.indexOf("\n");
        const request = JSON.parse(buffer.slice(0, end)) as Request;
        buffer = buffer.slice(end + 1);
        if (request.op === "deliver") { received.push(request); onDeliver(request, reply); continue; }
        reply({ id: request.id, ok: true, result: true });
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
  fs.writeFileSync(path.join(directory, `${sessionId}.json`), JSON.stringify({
    version: 1, sessionId, pid: process.pid, cwd: root, sessionFile: path.join(root, `${sessionId}.jsonl`), endpoint, token,
  }));
  return { received, closed, sockets };
}

function context(replyTimeoutMs: number) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "deliver-pending-"));
  cleanup.push(() => fs.rmSync(root, { recursive: true, force: true }));
  const logs: string[] = [];
  const waiters: { match: RegExp; done: () => void }[] = [];
  const control = new TerminalSessionControl({
    configRoot: root,
    replyTimeoutMs,
    onEvent: () => {},
    onLog: message => {
      logs.push(message);
      for (const waiter of waiters.splice(0)) { if (waiter.match.test(message)) waiter.done(); else waiters.push(waiter); }
    },
  });
  cleanup.push(() => control.close());
  const logged = (match: RegExp) => {
    if (logs.some(line => match.test(line))) return Promise.resolve();
    const { promise, resolve } = Promise.withResolvers<void>();
    waiters.push({ match, done: resolve });
    return promise;
  };
  return { root, logs, control, logged };
}

test("an acked delivery returns on the ack and reports the terminal's outcome from the event", async () => {
  const { root, logs, control, logged } = context(5_000);
  const sessionId = "acked";
  await session(root, sessionId, (request, reply) => {
    expect(request.ack).toBe(true);
    expect(typeof request.messageId).toBe("string");
    reply({ id: request.id, ok: true, result: { accepted: true, messageId: request.messageId, state: "pending" } });
    reply({ event: { kind: "delivery", sessionId, messageId: request.messageId, state: "delivered", outcome: "started" } });
  });
  expect(await control.deliver(sessionId, "hello")).toBe("queued");
  await logged(/delivered \(started\)/);
  expect(logs.some(line => /pending|failed/.test(line))).toBe(false);
});

test("a failure the terminal reports after the ack is logged as failed", async () => {
  const { root, control, logged } = context(5_000);
  const sessionId = "fails";
  await session(root, sessionId, (request, reply) => {
    reply({ id: request.id, ok: true, result: { accepted: true, messageId: request.messageId, state: "pending" } });
    reply({ event: { kind: "delivery", sessionId, messageId: request.messageId, state: "failed", error: "Error: boom" } });
  });
  await control.deliver(sessionId, "hello");
  await logged(/failed in the terminal: Error: boom/);
});

test("a session that misses the deadline leaves the delivery pending: no close, no retry, late reply confirmed", async () => {
  const { root, logs, control, logged } = context(100);
  const sessionId = "slow";
  let release: (() => void) | undefined;
  const fake = await session(root, sessionId, (request, reply) => {
    if (fake.received.length === 1) {
      release = () => reply({ id: request.id, ok: true, result: { accepted: true, messageId: request.messageId, state: "pending" } });
      return;
    }
    reply({ id: request.id, ok: true, result: "started" });
  });
  expect(await control.deliver(sessionId, "first")).toBe("queued");
  expect(logs.some(line => /pending: the session has not answered/.test(line))).toBe(true);
  expect(fake.received.map(request => request.text)).toEqual(["first"]);
  expect(fake.closed).toEqual([]);
  release?.();
  await logged(/accepted late/);
  // The same connection still works, and the first message was not sent again.
  expect(await control.deliver(sessionId, "second")).toBe("started");
  expect(fake.received.map(request => request.text)).toEqual(["first", "second"]);
  expect(fake.closed).toEqual([]);
});
