import { expect, test, spyOn } from "bun:test";
import { EventEmitter } from "node:events";
import * as net from "node:net";
import { SocketGuiHostPort } from "../src/gui-host-client";
import { HerdrAdapter } from "../src/herdr-adapter";
import type { CommandRunner } from "../src/contract";

const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };

// Controlled socket events plus virtual time: no scheduler-dependent sleeps.
test("successful handshake clears its deadline while a later request is active", async () => {
  const socket = new EventEmitter() as net.Socket;
  let destroyed = false;
  Object.assign(socket, { setEncoding() {}, write() { return true; }, destroy() { destroyed = true; return socket; } });
  const create = spyOn(net, "createConnection").mockImplementation(() => socket);
  let now = 0;
  const timers = new Map<object, { at: number; callback: () => void }>();
  const set = spyOn(globalThis, "setTimeout").mockImplementation(((callback: () => void, delay: number) => {
    const token = { unref() {} };
    timers.set(token, { at: now + delay, callback });
    return token;
  }) as typeof setTimeout);
  const clear = spyOn(globalThis, "clearTimeout").mockImplementation((token) => { timers.delete(token as object); });
  const tick = (elapsed: number) => {
    now += elapsed;
    for (const [token, timer] of timers) {
      if (timer.at <= now) { timers.delete(token); timer.callback(); }
    }
  };
  const port = new SocketGuiHostPort("tcp:localhost:1234", undefined, 100);
  try {
    const first = port.request({ first: true });
    await flush();
    socket.emit("data", '{"ConnectionChanged":{"Connected":{}}}\n');
    await flush();
    socket.emit("data", '{"RequestSucceeded":{"request":1}}\n');
    await first;
    tick(60);
    const second = port.request({ second: true });
    const observed = second.catch(error => error);
    await flush();
    tick(41);
    expect(destroyed).toBe(false);
    socket.emit("data", '{"RequestSucceeded":{"request":2}}\n');
    expect(await observed).toEqual({ events: [{ RequestSucceeded: { request: 2 } }] });
  } finally { port.close(); set.mockRestore(); clear.mockRestore(); create.mockRestore(); }
});

for (const scenario of ["idle-to-prompt", "active-to-idle-abort", "active-to-subsequent-abort"] as const) {
  test(`Herdr race reproduction: ${scenario}`, async () => {
    let state = scenario === "idle-to-prompt" ? "idle" : "working";
    let turn = 1;
    const deliveries: { state: string; turn: number; command: string }[] = [];
    const runner: CommandRunner = { async run(argv) {
      if (argv[2] === "get") {
        const snapshot = { agent: { name: "builder", agent_status: state } };
        // The native lifecycle changes after the snapshot, before the next CLI request.
        state = scenario === "active-to-idle-abort" ? "idle" : "working";
        if (scenario === "active-to-subsequent-abort") turn++;
        return { exitCode: 0, stdout: JSON.stringify({ result: snapshot }), stderr: "" };
      }
      deliveries.push({ state, turn, command: argv[2] });
      return { exitCode: 0, stdout: JSON.stringify({ result: {} }), stderr: "" };
    } };
    const adapter = new HerdrAdapter(runner);
    const result = scenario === "idle-to-prompt" ? await adapter.prompt("builder", "hello") : await adapter.abort("builder");
    // These assertions characterize the confirmed upstream defect, not closure.
    expect(result.ok).toBe(true);
    expect(deliveries).toEqual([{ state, turn, command: scenario === "idle-to-prompt" ? "prompt" : "send-keys" }]);
  });
}
