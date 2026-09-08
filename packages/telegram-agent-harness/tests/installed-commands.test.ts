import { test, expect } from "bun:test";
import { handleInstalledCommand, type InstalledCommandPort } from "../src/installed-commands";
import type { CommandRunner } from "../src/contract";

function fixture(idle = true) {
  const sent: string[] = [], photos: string[][] = [], inbound: unknown[][] = [], calls: readonly string[][] = [];
  const state = { id: "root", cwd: "C:/project", idle };
  const port: InstalledCommandPort = {
    session: () => state,
    send: async text => { await Promise.resolve(); sent.push(text); },
    photo: async (file, caption) => { await Promise.resolve(); photos.push([file, caption]); },
    latestPng: async () => { await Promise.resolve(); return "C:/session/local/latest.png"; },
    inbound: async (...args) => { await Promise.resolve(); inbound.push(args); },
  };
  const runner: CommandRunner = { run: async argv => {
    await Promise.resolve(); (calls as string[][]).push([...argv]);
    if (argv[0] === "veyyon") return { exitCode: 0, stderr: "", stdout: JSON.stringify({ reports: [{ provider: "Codex Spark", limits: [{ label: "5-hour", status: "ok", amount: { remaining: 85, unit: "percent" }, window: { resetsAt: Date.now() + 3600000 } }] }] }) };
    return { exitCode: 0, stderr: "", stdout: JSON.stringify({ result: argv[2] === "list" ? { agents: [{ pane_id: "h1", name: "Herdr <worker>", agent_status: "idle" }] } : { agent: { pane_id: "h1", name: "Worker", agent_status: "idle" } } }) };
  } };
  return { port, runner, sent, photos, inbound, calls, state };
}

test("agents lists actual adapter and bound root, escapes labels, admits missing worker registry", async () => {
  const f = fixture();
  expect(await handleInstalledCommand("/agents", f.port, f.runner)).toBe(true);
  expect(f.calls).toEqual([["herdr", "agent", "list"]]);
  expect(f.sent[0]).toContain("herdr:Herdr &lt;worker&gt;"); expect(f.sent[0]).toContain("veyyon:root");
  expect(f.sent[0]).toContain("&lt;worker&gt;"); expect(f.sent[0]).toContain("registry is not exposed");
});
test("unavailable Herdr still renders the current session", async () => {
  const f = fixture(); f.runner.run = async () => ({ exitCode: 127, stdout: "", stderr: "unavailable" });
  await handleInstalledCommand("/agents", f.port, f.runner);
  expect(f.sent[0]).toContain("veyyon:root"); expect(f.sent[0]).toContain("herdr:unavailable");
});
test("idle and busy prompt enter existing inbound path after acknowledgement", async () => {
  for (const idle of [true, false]) {
    const f = fixture(idle);
    f.port.inbound = async (text, actualIdle) => { expect(f.sent).toHaveLength(1); f.inbound.push([text, actualIdle]); };
    await handleInstalledCommand("/prompt veyyon:root hello\nworld", f.port, f.runner);
    expect(f.inbound).toEqual([["hello\nworld", idle]]);
  }
});
test("prompt never redirects a worker or switched session to Main", async () => {
  const f = fixture();
  await handleInstalledCommand("/prompt veyyon:foreign hello", f.port, f.runner);
  expect(f.inbound).toHaveLength(0);
  f.port.send = async () => { f.state.id = "switched"; };
  f.state.id = "root";
  await handleInstalledCommand("/prompt veyyon:root hello", f.port, f.runner);
  expect(f.inbound).toHaveLength(0);
});
test("Herdr prompt uses get-state then actual CLI prompt argv", async () => {
  const f = fixture(); await handleInstalledCommand("/prompt herdr:h1 hello world", f.port, f.runner);
  expect(f.calls).toEqual([["herdr", "agent", "get", "h1"], ["herdr", "agent", "prompt", "h1", "hello world"]]);
  expect(f.inbound).toHaveLength(0);
});
test("shot sends latest PNG as native photo with session caption", async () => {
  const f = fixture(); await handleInstalledCommand("/shot veyyon:root", f.port, f.runner);
  expect(f.photos[0][0]).toBe("C:/session/local/latest.png"); expect(f.photos[0][1]).toContain("veyyon:root");
  f.port.latestPng = async () => null;
  await handleInstalledCommand("/shot veyyon:root", f.port, f.runner);
  expect(f.photos).toHaveLength(1); expect(f.sent[0]).toContain("No screenshot");
});
test("usage reports Spark remaining and reset, never synthetic cost", async () => {
  const f = fixture(); await handleInstalledCommand("/usage", f.port, f.runner);
  expect(f.calls).toEqual([["veyyon", "usage", "--json"]]);
  expect(f.sent[0]).toContain("Codex Spark"); expect(f.sent[0]).toContain("85% remaining"); expect(f.sent[0]).toContain("resets in");
  expect(f.sent[0]).not.toContain("$");
});
test("unknown commands and free-text decisions remain in canonical poller routing", async () => {
  const f = fixture();
  for (const text of ["/promptly hi", "Proceed with option A", "/cancel"]) expect(await handleInstalledCommand(text, f.port, f.runner)).toBe(false);
  expect(f.sent).toHaveLength(0); expect(f.inbound).toHaveLength(0);
});
test("invalid prompt and backend exceptions are explicit and not retried", async () => {
  const f = fixture(); await handleInstalledCommand("/prompt", f.port, f.runner);
  expect(f.sent[0]).toContain("Usage:");
  f.runner.run = async () => { throw new Error("private backend detail"); };
  await handleInstalledCommand("/usage", f.port, f.runner);
  expect(f.sent[1]).toContain("Command unavailable"); expect(f.sent[1]).not.toContain("private");
});
