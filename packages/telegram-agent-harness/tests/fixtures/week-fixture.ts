// Typed WeekData v1 fixtures for the Week view (GET /api/week contract, issue #477 / #482).
// Mirrors daemon/week-summary.ts WeekData; replace the local interface with that import once it ships.

export interface WeekBlock {
  sessionId: string;
  project: string;
  cwd: string;
  tool: string;
  model: string;
  startMs: number;
  endMs: number;
  firstMessage: string;
  commits: number | null;
  noCommit: boolean;
  live: boolean;
  boards: string[];
}

export interface WeekBoard { id: string; title: string; kind: string; color: string; repos: string[]; projects: string[] }

export interface WeekCard {
  boardId: string; title: string; url: string; repo: string; number: number;
  state: "open" | "closed" | "merged"; status: string | null; atMs: number;
  source: "target" | "start" | "iteration" | "updated" | "closed" | "merged"; derived: boolean; spanEndMs: number | null;
}

export interface WeekData {
  version: 1;
  weekStart: number;
  weekEnd: number;
  zone: string;
  asOf: number;
  stale: boolean;
  staleReason: string | null;
  totals: { blocks: number; commits: number; noCommit: number; activeMs: number; sessionMs: number; projects: number };
  projects: { project: string; ms: number; sessionMs: number; boards: string[] }[];
  days: { dayStart: number; dayEnd: number; ms: number }[];
  blocks: WeekBlock[];
  noCommitBlocks: string[];
  report: [string, string, string];
  pullRequests: { repo: string; number: number; title: string; url: string; state: string; mergedAt: number | null; branch: string }[];
  boards: WeekBoard[];
  cards?: WeekCard[];
}

export type WeekScenario = "normal" | "empty" | "stale" | "busy";

const HOUR = 3_600_000;
const MIN = 60_000;
/** Monday 2026-09-28 00:00 Europe/Berlin (UTC+2, no DST change in this week). */
export const WEEK_START = Date.UTC(2026, 8, 27, 22);

export const BOARDS: WeekBoard[] = [
  { id: "Wladefant/5", title: "Superboard", kind: "veyyon-lanes", color: "#8ab4ff", repos: ["Wladefant/super-board", "Wladefant/veyyon"], projects: ["super-board", "veyyon"] },
  { id: "Bavariance/1", title: "PolySimulator", kind: "polysimulator", color: "#f5c06b", repos: ["Bavariance/polysimulator"], projects: ["polysimulator"] },
  { id: "Wladefant/11", title: "Shipnovo", kind: "shipnovo", color: "#7ee0b8", repos: ["Wladefant/shipnovo"], projects: ["shipnovo"] },
  { id: "Wladefant/7", title: "ING TestING", kind: "ing", color: "#d9a6ff", repos: ["Wladefant/testing"], projects: ["testing"] },
];

const PROJECT_BOARDS: Record<string, string[]> = {
  "super-board": ["Wladefant/5"],
  veyyon: ["Wladefant/5"],
  polysimulator: ["Bavariance/1"],
  shipnovo: ["Wladefant/11"],
  testing: ["Wladefant/7"],
  scratch: [],
};

const MODELS = ["Opus 5.5", "Sonnet 5.5", "Gemini 3.8 Flash", "GPT-5.6 Sol"];
const MESSAGES: Record<string, string[]> = {
  "super-board": ["Build the week calendar data layer", "Fix gh-guard reserve check", "Board inventory for all projects", "Telegram dedupe in sanitizer"],
  veyyon: ["Port upstream tail fix", "Session crash monitor resume", "Usage window pacing"],
  polysimulator: ["Order panel quick buy fill", "Staging receipt for PR 5630", "Market page skeleton states"],
  shipnovo: ["Carrier rate table import", "Competitor audit: Sendcloud", "Shipment list filters on mobile"],
  testing: ["Runner window capture", "Release download on PC28GR"],
  scratch: ["Try a Bun build cache", "Quick look at a flaky test"],
};

function block(project: string, i: number, day: number, startH: number, durMin: number, commits: number | null, live = false): WeekBlock {
  const startMs = WEEK_START + day * 24 * HOUR + startH * HOUR;
  const msgs = MESSAGES[project];
  return {
    sessionId: `${project}-${day}-${i}`,
    project,
    cwd: `C:/Users/wkiri/lanes/${project}`,
    tool: "Veyyon",
    model: MODELS[(i + day) % MODELS.length],
    startMs,
    endMs: startMs + durMin * MIN,
    firstMessage: msgs[(i + day) % msgs.length],
    commits,
    noCommit: commits === 0,
    live,
    boards: PROJECT_BOARDS[project],
  };
}

function normalBlocks(): WeekBlock[] {
  return [
    block("super-board", 0, 0, 9.5, 140, 6),
    block("polysimulator", 1, 0, 10, 95, 3),
    block("shipnovo", 2, 0, 14, 60, 0),
    block("veyyon", 3, 0, 16.25, 200, 4),
    block("super-board", 4, 1, 8.5, 75, 2),
    block("testing", 5, 1, 11, 120, 1),
    block("polysimulator", 6, 1, 11.5, 150, 5),
    block("scratch", 7, 1, 20, 45, 0),
    block("super-board", 8, 2, 9, 300, 9),
    block("shipnovo", 9, 2, 13, 90, 2),
    block("veyyon", 10, 2, 22.5, 120, 3),
    block("polysimulator", 11, 3, 10, 50, 0),
    block("polysimulator", 12, 3, 10.5, 80, 2),
    block("super-board", 13, 3, 10.75, 70, 1),
    block("shipnovo", 14, 3, 11, 40, null),
    block("testing", 15, 3, 15, 110, 0),
    block("super-board", 16, 4, 9, 180, 7),
    block("veyyon", 17, 4, 13, 95, 2),
    block("shipnovo", 18, 5, 11, 60, 3),
    block("super-board", 19, 6, 18, 120, 4, true),
  ];
}

function busyBlocks(): WeekBlock[] {
  const out = normalBlocks().filter(b => b.startMs < WEEK_START + 2 * 24 * HOUR || b.startMs >= WEEK_START + 3 * 24 * HOUR);
  const projects = Object.keys(PROJECT_BOARDS);
  for (let i = 0; i < 40; i += 1) {
    const project = projects[i % projects.length];
    out.push(block(project, 100 + i, 2, 8 + (i % 14) * 0.75, 45 + (i % 5) * 25, i % 6 === 0 ? 0 : (i % 4) + 1));
  }
  return out;
}

function union(intervals: [number, number][], from: number, to: number): number {
  const sorted = intervals.map(([s, e]) => [Math.max(s, from), Math.min(e, to)]).filter(([s, e]) => e > s).sort((a, b) => a[0] - b[0]);
  let total = 0; let cs = 0; let ce = -Infinity;
  for (const [s, e] of sorted) {
    if (s > ce) { if (ce > cs) total += ce - cs; cs = s; ce = e; } else if (e > ce) ce = e;
  }
  return ce > cs ? total + ce - cs : total;
}

export function makeWeek(scenario: WeekScenario = "normal"): WeekData {
  const weekEnd = WEEK_START + 7 * 24 * HOUR;
  const blocks = scenario === "empty" ? [] : scenario === "busy" ? busyBlocks() : normalBlocks();
  const iv = (b: WeekBlock): [number, number] => [b.startMs, b.endMs];
  const projects = Object.entries(PROJECT_BOARDS)
    .map(([project, boards]) => {
      const list = blocks.filter(b => b.project === project).map(iv);
      return { project, ms: union(list, WEEK_START, weekEnd), sessionMs: list.reduce((s, [a, b]) => s + b - a, 0), boards };
    })
    .filter(p => p.sessionMs > 0);
  const days = Array.from({ length: 7 }, (_, d) => {
    const dayStart = WEEK_START + d * 24 * HOUR;
    return { dayStart, dayEnd: dayStart + 24 * HOUR, ms: union(blocks.map(iv), dayStart, dayStart + 24 * HOUR) };
  });
  const noCommit = blocks.filter(b => b.noCommit);
  const day = (d: number, h: number) => WEEK_START + d * 24 * HOUR + h * HOUR;
  return {
    version: 1,
    weekStart: WEEK_START,
    weekEnd,
    zone: "Europe/Berlin",
    asOf: scenario === "stale" ? day(5, 18.5) : day(6, 21),
    stale: scenario === "stale",
    staleReason: scenario === "stale" ? "The PC was offline; showing the last pushed snapshot." : null,
    totals: {
      blocks: blocks.length,
      commits: blocks.reduce((s, b) => s + (b.commits ?? 0), 0),
      noCommit: noCommit.length,
      activeMs: union(blocks.map(iv), WEEK_START, weekEnd),
      sessionMs: blocks.reduce((s, b) => s + b.endMs - b.startMs, 0),
      projects: projects.length,
    },
    projects,
    days,
    blocks,
    noCommitBlocks: noCommit.map(b => `${b.sessionId}:${b.startMs}`),
    report: blocks.length
      ? ["Shipped: 4 merged PRs, latest Week view data layer.", "Most time: super-board, 14h 45m.", "Next: resume Shipment list filters on mobile first; it stopped with no commit."]
      : ["Shipped: nothing merged this week.", "Most time: no lanes ran.", "Next: start a lane from the board."],
    pullRequests: scenario === "empty" ? [] : [
      { repo: "Wladefant/super-board", number: 485, title: "Health check log file", url: "https://github.com/Wladefant/super-board/pull/485", state: "merged", mergedAt: day(4, 12), branch: "fix/health-log" },
      { repo: "Wladefant/super-board", number: 486, title: "Week view data layer", url: "https://github.com/Wladefant/super-board/pull/486", state: "merged", mergedAt: day(6, 19), branch: "feat/week-data" },
      { repo: "Bavariance/polysimulator", number: 5630, title: "Quick buy fill on hub cards", url: "https://github.com/Bavariance/polysimulator/pull/5630", state: "merged", mergedAt: day(1, 16), branch: "fix/quick-buy" },
      { repo: "Wladefant/shipnovo", number: 210, title: "Carrier rate import", url: "https://github.com/Wladefant/shipnovo/pull/210", state: "merged", mergedAt: day(2, 15), branch: "feat/rates" },
    ],
    boards: BOARDS,
    cards: scenario === "empty" ? [] : [
      { boardId: "Wladefant/5", title: "Week view: UI with week grid", url: "https://github.com/Wladefant/super-board/issues/478", repo: "Wladefant/super-board", number: 478, state: "open", status: "Building", atMs: day(0, 0), source: "iteration", derived: false, spanEndMs: day(6, 24) },
      { boardId: "Bavariance/1", title: "Hub card quick buy", url: "https://github.com/Bavariance/polysimulator/issues/5629", repo: "Bavariance/polysimulator", number: 5629, state: "closed", status: "Done", atMs: day(1, 0), source: "target", derived: false, spanEndMs: null },
      { boardId: "Wladefant/11", title: "Carrier rate import", url: "https://github.com/Wladefant/shipnovo/issues/209", repo: "Wladefant/shipnovo", number: 209, state: "closed", status: null, atMs: day(2, 0), source: "closed", derived: true, spanEndMs: null },
      { boardId: "Wladefant/7", title: "Runner capture provenance", url: "https://github.com/Wladefant/testing/issues/31", repo: "Wladefant/testing", number: 31, state: "open", status: "Ready", atMs: day(3, 0), source: "target", derived: false, spanEndMs: null },
    ],
  };
}
