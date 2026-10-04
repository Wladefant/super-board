// Fleet-state data layer tests (issue #427). Session files are real-shaped JSONL fixtures read through
// the default fs reader; only owners, the clock and the usage CLI are injected.
import { afterEach, beforeEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { defaultFleetSources, FleetState, parseFleetUsage, readSessionFile, runCommandWithTimeout, type FleetSources } from "../daemon/fleet-state";
import { discoverOwners, readProcessStartTimes, type Owner } from "../daemon/session-control";

const FIXTURES = path.join(import.meta.dir, "fixtures", "fleet-state");
const NOW = Date.parse("2026-10-04T10:08:40.000Z");
const tmp: string[] = [];
let root: string;
let mainFile: string;

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), "fleet-state-"));
  tmp.push(root);
  fs.cpSync(path.join(FIXTURES, "sessions"), path.join(root, "sessions"), { recursive: true });
  const dir = path.join(root, "sessions", "-proj");
  mainFile = path.join(dir, "2026-10-04T10-00-00-000Z_main1.jsonl");
  const stem = mainFile.replace(/\.jsonl$/, "");
  const touch = (file: string, iso: string) => fs.utimesSync(file, new Date(iso), new Date(iso));
  touch(mainFile, "2026-10-04T10:08:20.000Z"); // 20 s before NOW: running
  touch(path.join(stem, "Worker.jsonl"), "2026-10-04T10:08:30.000Z");
  touch(path.join(stem, "Worker", "Worker.Deep.jsonl"), "2026-10-04T10:05:00.000Z"); // 3 min: idle
  touch(path.join(stem, "Finished.jsonl"), "2026-10-04T09:00:00.000Z"); // over an hour: finished, excluded
  touch(path.join(stem, "__advisor.jsonl"), "2026-10-04T10:08:30.000Z");
});
afterEach(() => { for (const dir of tmp.splice(0)) fs.rmSync(dir, { recursive: true, force: true }); });

function owner(sessionId: string, sessionFile: string): Owner {
  return { version: 1, sessionId, pid: 1, cwd: "C:\\work\\proj", sessionFile, endpoint: "x", token: "0".repeat(64) };
}

function sources(over: Partial<FleetSources> = {}): FleetSources {
  return {
    ...defaultFleetSources(root),
    now: () => NOW,
    listOwners: () => [owner("main1", mainFile)],
    runUsage: async () => fs.readFileSync(path.join(FIXTURES, "usage", "usage.json"), "utf8"),
    hostMemory: () => ({ totalBytes: 1000, freeBytes: 250 }),
    ...over,
  };
}

test("snapshot lists the session and live subagents with parent links, model, elapsed and counts", async () => {
  const snapshot = await new FleetState(sources()).snapshot();
  const byId = new Map(snapshot.lanes.map(lane => [lane.id, lane]));
  expect([...byId.keys()].sort()).toEqual(["main1", "main1/Worker", "main1/Worker/Worker.Deep"].sort());

  const main = byId.get("main1")!;
  expect(main).toMatchObject({ kind: "interactive", parentId: null, name: "Fleet data layer", status: "running", model: "openai-codex/gpt-5.6-sol" });
  expect(main.childIds).toEqual(["main1/Worker"]);
  expect(main.elapsedMs).toBe(NOW - Date.parse("2026-10-04T10:00:00.000Z"));

  const worker = byId.get("main1/Worker")!;
  expect(worker).toMatchObject({ kind: "subagent", parentId: "main1", name: "Worker", status: "running", model: "google-antigravity/gemini-3.8-flash" });
  expect(worker.lastAction).toBe("read: Reading session-control.ts");
  expect(worker.childIds).toEqual(["main1/Worker/Worker.Deep"]);

  const deep = byId.get("main1/Worker/Worker.Deep")!;
  expect(deep).toMatchObject({ parentId: "main1/Worker", name: "Deep", status: "idle", lastAction: "Checking edge cases" });
  expect(snapshot.counts).toEqual({ running: 2, idle: 1, subagents: 2 });
  expect(snapshot.hostMemory).toEqual({ totalBytes: 1000, freeBytes: 250, usedPercent: 75 });
});

test("last action is redacted and never leaks a token", async () => {
  const snapshot = await new FleetState(sources()).snapshot();
  const main = snapshot.lanes.find(lane => lane.id === "main1")!;
  expect(main.lastAction).toContain("Deploy with token");
  expect(main.lastAction).not.toContain("ghp_abcdefghijklmnopqrstuvwxyz0123456789");
});

test("a huge session file is read from its head and tail windows only", () => {
  const big = path.join(root, "big.jsonl");
  const lines = fs.readFileSync(mainFile, "utf8").split("\n").filter(Boolean);
  const filler = JSON.stringify({ id: "z", parentId: null, timestamp: "2026-10-04T10:01:00.000Z", type: "custom_message", content: "x".repeat(900) });
  const body = [...lines.slice(0, 3), ...Array(2000).fill(filler), ...lines.slice(3)];
  fs.writeFileSync(big, body.join("\n") + "\n");
  expect(fs.statSync(big).size).toBeGreaterThan(1_500_000);
  const info = readSessionFile(big)!;
  expect(info.id).toBe("main1");
  expect(info.model).toBe("openai-codex/gpt-5.6-sol");
  expect(info.lastAction).toContain("Deploy with token");
});

test("an unreadable or headerless session file yields null and the lane stays visible from its owner", async () => {
  const broken = path.join(root, "broken.jsonl");
  fs.writeFileSync(broken, "not json\n");
  expect(readSessionFile(broken)).toBeNull();
  const snapshot = await new FleetState(sources({ listOwners: () => [owner("ghost", broken)] })).snapshot();
  expect(snapshot.lanes).toHaveLength(1);
  expect(snapshot.lanes[0]).toMatchObject({ id: "ghost", status: "idle", model: null, startedAtMs: null });
});

test("two owners claiming one session are ambiguous and both are dropped", async () => {
  const snapshot = await new FleetState(sources({ listOwners: () => [owner("main1", mainFile), owner("main1", mainFile)] })).snapshot();
  expect(snapshot.lanes).toEqual([]);
});

test("owners with a reused PID are excluded by ownerIdentityMatches through the default owner source", () => {
  const terminals = path.join(root, "run", "terminals");
  fs.mkdirSync(terminals, { recursive: true });
  const actualStart = readProcessStartTimes([process.pid]).get(process.pid);
  if (actualStart === undefined) return; // The OS refused to report a start time: nothing to compare against.
  const endpoint = process.platform === "win32" ? "\\\\.\\pipe\\veyyon-terminal-test" : path.join(root, "t.sock");
  const write = (name: string, sessionId: string, startedAtMs: number) =>
    fs.writeFileSync(path.join(terminals, name), JSON.stringify({ version: 1, sessionId, pid: process.pid, cwd: root, sessionFile: mainFile, endpoint, token: "a".repeat(64), startedAtMs }));
  write("live.json", "live", actualStart);
  write("reused.json", "reused", actualStart - 3_600_000);
  const sessions = discoverOwners(root).map(found => found.sessionId);
  expect(sessions).toContain("live");
  expect(sessions).not.toContain("reused");
});

test("usage keeps exhausted windows, skips malformed reports and is cached for 60 s", async () => {
  let calls = 0;
  let now = NOW;
  const state = new FleetState(sources({ now: () => now, runUsage: async () => { calls++; return fs.readFileSync(path.join(FIXTURES, "usage", "usage.json"), "utf8"); } }));
  const first = await state.snapshot();
  expect(first.usage.available).toBe(true);
  expect(first.usage.windows).toEqual([
    { provider: "google-antigravity", label: "Claude and GPT models", windowId: "5h", remainingPercent: 0, resetsAt: 1791143042000, status: "exhausted" },
    { provider: "google-antigravity", label: "Claude and GPT models", windowId: "weekly", remainingPercent: 37, resetsAt: 1791500000000, status: "ok" },
  ]);
  now += 30_000;
  await state.snapshot();
  expect(calls).toBe(1);
  now += 31_000;
  await state.snapshot();
  expect(calls).toBe(2);
});

test("concurrent snapshots share one usage call", async () => {
  let calls = 0;
  const gate = Promise.withResolvers<void>();
  const state = new FleetState(sources({ runUsage: async () => { calls++; await gate.promise; return "{}"; } }));
  const all = Promise.all([state.snapshot(), state.snapshot(), state.snapshot()]);
  gate.resolve();
  await all;
  expect(calls).toBe(1);
});

test("a failing or hanging usage CLI degrades to unavailable and is not respawned every refresh", async () => {
  let calls = 0;
  const state = new FleetState(sources({ runUsage: async () => { calls++; throw new Error("spawn timeout"); } }));
  const first = await state.snapshot();
  await state.snapshot();
  expect(first.usage).toMatchObject({ available: false, windows: [] });
  expect(calls).toBe(1);
  expect(first.lanes.length).toBe(3);
});

test("the usage timeout is passed to the runner", async () => {
  let seen = 0;
  await new FleetState(sources({ runUsage: async timeout => { seen = timeout; return "{}"; } }), { usageTimeoutMs: 1234 }).snapshot();
  expect(seen).toBe(1234);
});

test("parseFleetUsage rejects garbage without throwing", () => {
  expect(parseFleetUsage("not json", 1)).toMatchObject({ available: false, windows: [] });
  expect(parseFleetUsage(JSON.stringify({ reports: [] }), 1).available).toBe(false);
});

function sessionLines(extra: object[]): string[] {
  const lines = fs.readFileSync(mainFile, "utf8").split("\n").filter(Boolean).slice(0, 2);
  return [...lines, ...extra.map(entry => JSON.stringify(entry))];
}

test("a token straddling the truncation limit does not leak its prefix", () => {
  const file = path.join(root, "straddle.jsonl");
  const text = `${"x".repeat(170)} ghp_${"a".repeat(36)} tail`;
  const entry = { id: "s1", parentId: null, timestamp: "2026-10-04T10:07:00.000Z", type: "message", message: { role: "assistant", content: [{ type: "text", text }] } };
  fs.writeFileSync(file, sessionLines([entry]).join("\n") + "\n");
  const action = readSessionFile(file)!.lastAction!;
  expect(action).not.toContain("ghp_");
  expect(action.length).toBeLessThanOrEqual(200);
});

test("a secret in the title is redacted from the lane name", async () => {
  const file = path.join(root, "titled.jsonl");
  const lines = sessionLines([]);
  lines[0] = JSON.stringify({ type: "title", v: 1, title: `Deploy ghp_${"b".repeat(36)}`, updatedAt: "2026-10-04T10:00:00.000Z" });
  fs.writeFileSync(file, lines.join("\n") + "\n");
  const snapshot = await new FleetState(sources({ listOwners: () => [owner("titled", file)] })).snapshot();
  expect(snapshot.lanes[0]!.name).not.toContain("ghp_");
});

test("a 200 KB session file reports the newest model and action from its tail", () => {
  const file = path.join(root, "medium.jsonl");
  const filler = JSON.stringify({ id: "z", parentId: null, timestamp: "2026-10-04T10:01:00.000Z", type: "custom_message", content: "y".repeat(900) });
  const entries = [
    { id: "m9", parentId: null, timestamp: "2026-10-04T10:09:00.000Z", type: "model_change", model: "openai-codex/gpt-5.6-sol" },
    { id: "c9", parentId: "m9", timestamp: "2026-10-04T10:09:10.000Z", type: "custom", customType: "tool_execution_start", data: { toolName: "bash", intent: "Newest action" } },
  ];
  fs.writeFileSync(file, [...sessionLines([]), ...Array(220).fill(filler), ...entries.map(entry => JSON.stringify(entry))].join("\n") + "\n");
  const size = fs.statSync(file).size;
  expect(size).toBeGreaterThan(190_000);
  expect(size).toBeLessThan(256 * 1024);
  const info = readSessionFile(file)!;
  expect(info.model).toBe("openai-codex/gpt-5.6-sol");
  expect(info.lastAction).toBe("bash: Newest action");
});

test("a hanging usage command is killed and bounded by the timeout", async () => {
  const started = Date.now();
  const hang = { command: process.execPath, args: ["-e", "setTimeout(() => {}, 60000)"] };
  await expect(runCommandWithTimeout(hang, 300)).rejects.toThrow("timed out");
  expect(Date.now() - started).toBeLessThan(5000);
  const snapshot = await new FleetState({ ...sources(), runUsage: ms => runCommandWithTimeout(hang, ms) }, { usageTimeoutMs: 300 }).snapshot();
  expect(snapshot.usage).toMatchObject({ available: false });
});

test("a usage command that exits non-zero is rejected and one that prints JSON is returned", async () => {
  await expect(runCommandWithTimeout({ command: process.execPath, args: ["-e", "process.exit(3)"] }, 5000)).rejects.toThrow("exited 3");
  expect(await runCommandWithTimeout({ command: process.execPath, args: ["-e", "console.log('{\"reports\":[]}')"] }, 5000)).toContain("reports");
});
