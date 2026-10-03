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
  test(`Herdr race regression: ${scenario}`, async () => {
    let state = scenario === "idle-to-prompt" ? "idle" : "working";
    let turn = 1;
    const calls: string[][] = [];
    const runner: CommandRunner = { async run(argv) {
      calls.push([...argv]);
      if (scenario === "idle-to-prompt") {
        return {
          exitCode: 0,
          stdout: JSON.stringify({
            result: { type: "agent_lifecycle", status: "working", generation: "gen-1", delivered: false },
          }),
          stderr: "",
        };
      }
      if (argv[4] === "snapshot") {
        const snapshot = { type: "agent_lifecycle", status: state, generation: `gen-${turn}`, delivered: false };
        state = scenario === "active-to-idle-abort" ? "idle" : "working";
        if (scenario === "active-to-subsequent-abort") turn++;
        return { exitCode: 0, stdout: JSON.stringify({ result: snapshot }), stderr: "" };
      }
      const generation = scenario === "active-to-subsequent-abort" ? `gen-${turn}` : argv[5];
      return {
        exitCode: 0,
        stdout: JSON.stringify({
          result: { type: "agent_lifecycle", status: state, generation, delivered: false },
        }),
        stderr: "",
      };
    } };
    const adapter = new HerdrAdapter(runner);
    const result = scenario === "idle-to-prompt" ? await adapter.prompt("builder", "hello") : await adapter.abort("builder");
    expect(result.ok).toBe(false);
    if (scenario === "idle-to-prompt") {
      expect(result).toMatchObject({ disposition: "rejected" });
      expect(calls).toEqual([["herdr", "agent", "lifecycle", "builder", "submit", "hello"]]);
    } else {
      expect(calls).toEqual([
        ["herdr", "agent", "lifecycle", "builder", "snapshot"],
        ["herdr", "agent", "lifecycle", "builder", "abort", "gen-1"],
      ]);
    }
  });
}

test("Herdr abort: matching delivered:true ack with the same token succeeds", async () => {
  const calls: string[][] = [];
  const runner: CommandRunner = {
    async run(argv) {
      calls.push([...argv]);
      if (argv[4] === "snapshot") {
        return {
          exitCode: 0,
          stdout: JSON.stringify({ result: { type: "agent_lifecycle", status: "working", generation: "gen-token-abc", delivered: false } }),
          stderr: "",
        };
      }
      return {
        exitCode: 0,
        stdout: JSON.stringify({ result: { type: "agent_lifecycle", status: "idle", generation: "gen-token-abc", delivered: true } }),
        stderr: "",
      };
    },
  };
  const adapter = new HerdrAdapter(runner);
  const result = await adapter.abort("builder");
  expect(result.ok).toBe(true);
  expect(result.detail).toBe("Herdr confirmed the interrupt reached the observed generation.");
  expect(calls).toEqual([
    ["herdr", "agent", "lifecycle", "builder", "snapshot"],
    ["herdr", "agent", "lifecycle", "builder", "abort", "gen-token-abc"],
  ]);
});

test("Herdr abort: mismatched generation token on ack fails without raw fallback", async () => {
  const calls: string[][] = [];
  const runner: CommandRunner = {
    async run(argv) {
      calls.push([...argv]);
      if (argv[4] === "snapshot") {
        return {
          exitCode: 0,
          stdout: JSON.stringify({ result: { type: "agent_lifecycle", status: "working", generation: "gen-token-abc", delivered: false } }),
          stderr: "",
        };
      }
      return {
        exitCode: 0,
        stdout: JSON.stringify({ result: { type: "agent_lifecycle", status: "idle", generation: "gen-token-diff", delivered: true } }),
        stderr: "",
      };
    },
  };
  const adapter = new HerdrAdapter(runner);
  const result = await adapter.abort("builder");
  expect(result.ok).toBe(false);
  expect(result.detail).toBe("Herdr did not confirm that abort. No raw interrupt or retry was sent.");
  expect(calls).toEqual([
    ["herdr", "agent", "lifecycle", "builder", "snapshot"],
    ["herdr", "agent", "lifecycle", "builder", "abort", "gen-token-abc"],
  ]);
});
