import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { issueAppSession } from "../daemon/miniapp-auth";
import { miniAppRequest, type MiniAppOptions } from "../daemon/miniapp";
import { resolveWeekFile } from "../week/server";
import { GithubReader, type GithubReaderDeps } from "../daemon/week-github";
import { blocksFromParsed, parseSessionText, projectOf, splitBlocks, withCommits } from "../daemon/week-sessions";
import { WeekService } from "../daemon/week-service";
import { WeekStore } from "../daemon/week-store";
import {
  addDays, mergeIntervals, snapshotStaleness, summarizeWeek, weekStartOf,
  type BoardInfo, type SessionBlock, type WeekBlock,
} from "../daemon/week-summary";

const MIN = 60_000;
const HOUR = 60 * MIN;
const T0 = Date.UTC(2026, 9, 5, 8, 0); // Monday 2026-10-05 08:00Z
const BOARDS: BoardInfo[] = [
  { id: "Wladefant/5", title: "Superboard", kind: "veyyon-lanes", color: "#22d3ee", repos: ["Wladefant/super-board", "Wladefant/veyyon"] },
  { id: "Bavariance/1", title: "Polysimulator", kind: "polysimulator", color: "#6366f1", repos: ["Bavariance/polysimulator"] },
];

function block(over: Partial<WeekBlock> & { startMs: number; endMs: number }): WeekBlock {
  return { sessionId: "s", project: "super-board", cwd: "C:/x/super-board", tool: "veyyon", model: null, firstMessage: null, commits: 1, commitShas: ["aaaaaaaa"], noCommit: false, boards: [], live: false, ...over };
}

describe("session blocks", () => {
  test("a gap of exactly 30 min stays one block, 31 min splits", () => {
    expect(splitBlocks([T0, T0 + 30 * MIN, T0 + 60 * MIN])).toEqual([[T0, T0 + 60 * MIN]]);
    expect(splitBlocks([T0, T0 + 31 * MIN])).toEqual([[T0, T0], [T0 + 31 * MIN, T0 + 31 * MIN]]);
  });

  test("parses a session file: blocks, model, cwd, first message redacted to 200 chars", () => {
    const token = "ghp_" + "a".repeat(36);
    const lines = [
      JSON.stringify({ type: "session", id: "sess-1", timestamp: new Date(T0).toISOString(), cwd: "C:\\dev\\super-board" }),
      JSON.stringify({ type: "model_change", model: "anthropic/claude-sonnet-5-5" }),
      JSON.stringify({ type: "message", timestamp: new Date(T0).toISOString(), message: { role: "user", content: [{ type: "text", text: `use ${token} ${"x".repeat(400)}` }] } }),
      JSON.stringify({ type: "session_init", timestamp: new Date(T0 + 99 * HOUR).toISOString(), systemPrompt: "ignored" }),
      JSON.stringify({ type: "message", timestamp: new Date(T0 + 10 * MIN).toISOString(), message: { role: "assistant", content: [{ type: "text", text: "ok" }] } }),
      JSON.stringify({ type: "message", timestamp: new Date(T0 + 2 * HOUR).toISOString(), message: { role: "user", content: "later" } }),
    ].join("\n");
    const parsed = parseSessionText(lines)!;
    const blocks = blocksFromParsed(parsed);
    expect(blocks.map(b => [b.startMs, b.endMs])).toEqual([[T0, T0 + 10 * MIN], [T0 + 2 * HOUR, T0 + 2 * HOUR]]);
    expect(blocks[0]!.project).toBe("super-board");
    expect(blocks[0]!.model).toBe("anthropic/claude-sonnet-5-5");
    expect(blocks[0]!.firstMessage!.length).toBeLessThanOrEqual(200);
    expect(blocks[0]!.firstMessage).not.toContain(token);
    expect(blocks[1]!.firstMessage).toBeNull();
  });

  test("worktree directories report their repo", () => {
    expect(projectOf("C:\\Users\\w\\lanes\\week-data")).toBe("week-data");
    expect(projectOf("C:\\dev\\polysimulator\\.wt-fix-1")).toBe("polysimulator");
  });

  test("no-commit flag: zero commits flags; unreadable git and a running lane do not", async () => {
    const base: SessionBlock[] = [0, 1, 2, 3].map(i => ({ sessionId: `s${i}`, project: "p", cwd: `c${i}`, tool: "veyyon", model: null, startMs: T0, endMs: T0 + HOUR, firstMessage: null }));
    const counts: Record<string, string[] | null> = { c0: [], c1: ["a1", "a2", "a3"], c2: null, c3: [] };
    const fleet = { lanes: [{ id: "s3", status: "running" as const }] };
    const out = await withCommits(base, async cwd => counts[cwd] ?? null, fleet);
    expect(out.map(b => [b.commits, b.noCommit])).toEqual([[0, true], [3, false], [null, false], [0, false]]);
  });

  test("the same commit seen by two overlapping blocks is counted once in the totals", () => {
    const data = summarizeWeek({
      weekStart: weekStartOf(T0, "UTC"), zone: "UTC", asOf: T0, boards: BOARDS,
      blocks: [
        block({ sessionId: "a", startMs: T0, endMs: T0 + HOUR, commits: 2, commitShas: ["c1", "c2"] }),
        block({ sessionId: "b", startMs: T0, endMs: T0 + HOUR, commits: 2, commitShas: ["c2", "c3"] }),
      ],
    });
    expect(data.totals.commits).toBe(3);
  });
});

describe("week summary", () => {
  const weekStart = weekStartOf(T0, "UTC");

  test("parallel time in a project is counted once; session time is the sum", () => {
    const data = summarizeWeek({
      weekStart, zone: "UTC", asOf: T0, boards: BOARDS,
      blocks: [
        block({ sessionId: "a", startMs: T0, endMs: T0 + 60 * MIN }),
        block({ sessionId: "b", startMs: T0 + 30 * MIN, endMs: T0 + 90 * MIN }),
        block({ sessionId: "c", project: "polysimulator", startMs: T0, endMs: T0 + 30 * MIN }),
      ],
    });
    const sb = data.projects.find(p => p.project === "super-board")!;
    expect(sb.ms).toBe(90 * MIN); // would be 120 min without the overlap merge
    expect(sb.sessionMs).toBe(120 * MIN);
    expect(data.totals.activeMs).toBe(90 * MIN);
    expect(data.totals.sessionMs).toBe(150 * MIN);
    expect(sb.boards).toEqual(["Wladefant/5"]);
    expect(data.blocks.find(b => b.project === "polysimulator")!.boards).toEqual(["Bavariance/1"]);
  });

  test("mergeIntervals merges touching and nested intervals", () => {
    expect(mergeIntervals([[0, 5], [5, 8], [2, 3], [10, 12]])).toEqual([[0, 8], [10, 12]]);
  });

  test("hours per day split a block at local midnight", () => {
    const start = Date.UTC(2026, 9, 5, 23, 0);
    const data = summarizeWeek({ weekStart, zone: "UTC", asOf: T0, boards: BOARDS, blocks: [block({ startMs: start, endMs: start + 2 * HOUR })] });
    expect(data.days[0]!.ms).toBe(HOUR);
    expect(data.days[1]!.ms).toBe(HOUR);
  });

  test("a DST day is 23 hours long and the week still has 7 local days", () => {
    const zone = "Europe/Berlin";
    const start = weekStartOf(Date.UTC(2026, 2, 25, 12), zone); // Sunday 2026-03-29 is the spring-forward day
    const data = summarizeWeek({ weekStart: start, zone, asOf: T0, boards: BOARDS, blocks: [] });
    const lengths = data.days.map(d => (d.dayEnd - d.dayStart) / HOUR);
    expect(lengths).toEqual([24, 24, 24, 24, 24, 24, 23]);
    expect(addDays(start, 7, zone)).toBe(data.weekEnd);
  });

  test("no-commit lanes appear in the report and list", () => {
    const data = summarizeWeek({
      weekStart, zone: "UTC", asOf: T0, boards: BOARDS,
      blocks: [block({ sessionId: "z", startMs: T0, endMs: T0 + HOUR, commits: 0, noCommit: true, firstMessage: "fix the thing" })],
    });
    expect(data.noCommitBlocks).toEqual([`z:${T0}`]);
    expect(data.totals.noCommit).toBe(1);
    expect(data.report[2]).toContain("resume super-board");
  });

  test("snapshot staleness: fresh until 10 minutes, stale after", () => {
    expect(snapshotStaleness(T0, T0 + 10 * MIN).stale).toBe(false);
    expect(snapshotStaleness(T0, T0 + 10 * MIN + 1).stale).toBe(true);
  });
});

describe("store and service", () => {
  test("blocks are append-only: a grown block adds a row, an unchanged one does not, reads return the latest", () => {
    const store = new WeekStore(":memory:");
    const b: SessionBlock = { sessionId: "s", project: "p", cwd: "c", tool: "veyyon", model: null, startMs: T0, endMs: T0 + HOUR, firstMessage: "hi" };
    expect(store.appendBlocks([b])).toBe(1);
    expect(store.appendBlocks([b])).toBe(0);
    expect(store.appendBlocks([{ ...b, endMs: T0 + 2 * HOUR }])).toBe(1);
    const read = store.blocksBetween(T0 - HOUR, T0 + 5 * HOUR);
    expect(read).toHaveLength(1);
    expect(read[0]!.endMs).toBe(T0 + 2 * HOUR);
  });

  test("service serves the stored snapshot with its as-of time, marks it stale when old, and falls back when a rebuild fails", async () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "week-svc-"));
    try {
      let now = T0 + HOUR;
      const store = new WeekStore(":memory:");
      const session = [
        JSON.stringify({ type: "session", id: "s1", timestamp: new Date(T0).toISOString(), cwd: "C:\\dev\\super-board" }),
        JSON.stringify({ type: "message", timestamp: new Date(T0).toISOString(), message: { role: "user", content: "go" } }),
      ].join("\n");
      fs.writeFileSync(path.join(dir, "a.jsonl"), session);
      let counted = 0;
      const service = new WeekService({ store, sessionRoots: [dir], boards: BOARDS, zone: "UTC", now: () => now, countCommits: async () => { counted++; return []; } });
      const first = await service.get(T0);
      expect(first.asOf).toBe(now);
      expect(first.totals.noCommit).toBe(1);
      expect(first.stale).toBe(false);

      now += 5 * MIN;
      const second = await service.get(T0);
      expect(second.asOf).toBe(first.asOf); // served from the stored snapshot
      expect(counted).toBe(1);

      now += 20 * MIN; // snapshot older than 10 min and the rebuild fails
      fs.rmSync(dir, { recursive: true, force: true });
      const failing = new WeekService({ store, sessionRoots: [dir], boards: BOARDS, zone: "UTC", now: () => now, countCommits: async () => { throw new Error("boom"); } });
      const third = await failing.get(T0);
      expect(third.asOf).toBe(first.asOf);
      expect(third.stale).toBe(true);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("github guard", () => {
  function deps(guardCode: () => number) {
    const calls = { guard: 0, graphql: 0 };
    const value: GithubReaderDeps = {
      now: () => T0 + calls.guard * 6 * MIN,
      guard: async () => { calls.guard++; return guardCode(); },
      graphql: async () => { calls.graphql++; return { data: { o0: { nodes: [{ number: 7, title: "t", url: "u", state: "MERGED", mergedAt: "2026-10-05T10:00:00Z", headRefName: "b", repository: { nameWithOwner: "Wladefant/super-board" } }] } } }; },
    };
    return { calls, value };
  }

  test("reserve reached (exit 75) makes no gh call and serves the last result with stale=true", async () => {
    let code = 0;
    const { calls, value } = deps(() => code);
    const reader = new GithubReader(value, ["Wladefant"]);
    const fresh = await reader.read(T0);
    expect(fresh.stale).toBe(false);
    expect(fresh.pullRequests).toHaveLength(1);
    expect(calls.graphql).toBe(1);

    code = 75;
    const stale = await reader.read(T0); // clock moved 6 min: cache expired, guard runs again
    expect(calls.graphql).toBe(1);
    expect(stale.stale).toBe(true);
    expect(stale.pullRequests).toHaveLength(1);
    expect(stale.staleReason).toContain("quota");
  });

  test("a fresh result inside 5 minutes is cached: no guard, no gh", async () => {
    const calls = { guard: 0, graphql: 0 };
    const reader = new GithubReader({ now: () => T0, guard: async () => { calls.guard++; return 0; }, graphql: async () => { calls.graphql++; return { data: {} }; } }, ["Wladefant"]);
    await reader.read(T0);
    await reader.read(T0);
    expect(calls).toEqual({ guard: 1, graphql: 1 });
  });
});

describe("GET /api/week and static files", () => {
  const dirs: string[] = [];
  afterEach(() => { for (const d of dirs.splice(0)) fs.rmSync(d, { recursive: true, force: true }); });

  function options(week: MiniAppOptions["week"]): MiniAppOptions {
    const stateDir = fs.mkdtempSync(path.join(os.tmpdir(), "week-route-"));
    dirs.push(stateDir);
    return { stateDir, token: "123456:test-token", allowedUsers: ["42"], session: () => null, sessions: async () => [], dashboard: () => null, status: () => ({}), week };
  }

  test("401 without a valid app session, 200 JSON with one", async () => {
    const opts = options(async start => ({ version: 1, start }));
    const denied = await miniAppRequest({ id: "1", path: "/api/week?start=5", method: "GET", initData: "", appSession: "bad", body: "" }, opts);
    expect(denied.status).toBe(401);
    const ok = await miniAppRequest({ id: "2", path: "/api/week?start=5", method: "GET", initData: "", appSession: issueAppSession("42", opts.token), body: "" }, opts);
    expect(ok.status).toBe(200);
    expect(ok.data).toEqual({ version: 1, start: "5" });
  });

  test("static allow-list serves flat web files and the shared client, refuses everything else", () => {
    expect(resolveWeekFile("/")).toBe("index.html");
    expect(resolveWeekFile("/week.js")).toBe("week.js");
    expect(resolveWeekFile("/boards.json")).toBe("boards.json");
    expect(resolveWeekFile("/miniapp/client.js")).toBe("../miniapp/client.js");
    expect(resolveWeekFile("/../secret.json")).toBeUndefined();
    expect(resolveWeekFile("/a/b.js")).toBeUndefined();
    expect(resolveWeekFile("/server.ts")).toBeUndefined();
  });
});
