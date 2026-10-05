/**
 * week-summary.ts — pure week maths for the Week view (issue #477, parent #474).
 *
 * Input: session blocks (a lane's work, split at 30 min gaps) with a commit count per block.
 * Output: {@link WeekData}, the contract the UI slice reads from `GET /api/week`.
 *
 * Rules kept from the source prompt: local time zone; parallel sessions counted once in project
 * totals; a block with zero commits is flagged `noCommit`. A block whose commits could not be
 * read (`commits: null`, e.g. the worktree is gone) is NEVER flagged: unknown is not zero.
 */

export const GAP_MS = 30 * 60_000;

export interface SessionBlock {
  sessionId: string;
  project: string;
  cwd: string;
  tool: string;
  model: string | null;
  startMs: number;
  endMs: number;
  /** Redacted, at most 200 chars. */
  firstMessage: string | null;
}

export interface WeekBlock extends SessionBlock {
  /** Commits made in the block window in the lane's worktree; null = could not be read. */
  commits: number | null;
  /** Short hashes of those commits; totals de-duplicate by hash when blocks overlap. */
  commitShas: string[];
  noCommit: boolean;
  /** Registry ids of the boards this block's project belongs to; empty = unassigned. */
  boards: string[];
  /** The lane is running now (fleet snapshot). */
  live: boolean;
}

export interface BoardInfo { id: string; title: string; kind: string; color: string; repos: string[]; owner: string; number: number; dateField: string | null }

export interface WeekPullRequest { repo: string; number: number; title: string; url: string; state: string; mergedAt: string | null; branch: string }

/** One project card placed in the week (issue #482). `derived` = placed by activity, not by a planned date. */
export interface WeekCard {
  boardId: string;
  kind: string;
  title: string;
  url: string | null;
  repo: string | null;
  number: number | null;
  type: "issue" | "pr" | "draft";
  state: "OPEN" | "CLOSED" | "MERGED";
  /** Placement (ms). For a planned range this is its start. */
  at: number;
  /** End of a planned range longer than a day; null otherwise. */
  endAt: number | null;
  source: "target" | "start" | "iteration" | "activity";
  derived: boolean;
}

export interface WeekData {
  version: 1;
  /** Local-midnight start of the week (ms) and the exclusive end (start of the next local week). */
  weekStart: number;
  weekEnd: number;
  zone: string;
  /** When this data was computed (ms). The UI shows it as the "as of" badge. */
  asOf: number;
  /** True when part of the data is older than `asOf` (guard refused the GitHub read, or the PC scan failed). */
  stale: boolean;
  staleReason: string | null;
  totals: { blocks: number; commits: number; noCommit: number; activeMs: number; sessionMs: number; projects: number };
  /** Hours per project; parallel time inside a project is counted once. */
  projects: { project: string; ms: number; sessionMs: number; boards: string[] }[];
  /** Union of all lanes per local day, 7 entries (a DST day is 23 or 25 h long). */
  days: { dayStart: number; dayEnd: number; ms: number }[];
  blocks: WeekBlock[];
  noCommitBlocks: string[];
  report: [string, string, string];
  pullRequests: WeekPullRequest[];
  /** Project cards placed in this week, one entry per (board, card). */
  cards: WeekCard[];
  cardTotals: { cards: number; planned: number; derived: number };
  /** For the dropdown: every board with the projects seen this week that belong to it. */
  boards: (BoardInfo & { projects: string[]; cards: number })[];
}

// ------------------------------------------------------------------ time zone

const formatters = new Map<string, Intl.DateTimeFormat>();
function formatter(zone: string): Intl.DateTimeFormat {
  let f = formatters.get(zone);
  if (!f) {
    f = new Intl.DateTimeFormat("en-US", { timeZone: zone, hourCycle: "h23", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", second: "numeric" });
    formatters.set(zone, f);
  }
  return f;
}

export function localZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function parts(ms: number, zone: string) {
  const out: Record<string, number> = {};
  for (const p of formatter(zone).formatToParts(new Date(ms))) if (p.type !== "literal") out[p.type] = Number(p.value);
  return out as { year: number; month: number; day: number; hour: number; minute: number; second: number };
}

function offsetMs(ms: number, zone: string): number {
  const p = parts(ms, zone);
  return Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second) - Math.floor(ms / 1000) * 1000;
}

function localMidnight(year: number, month: number, day: number, zone: string): number {
  const wall = Date.UTC(year, month - 1, day);
  let guess = wall;
  for (let i = 0; i < 3; i++) guess = wall - offsetMs(guess, zone);
  return guess;
}

/** Start of the local day containing `ms`. */
export function dayStartOf(ms: number, zone: string): number {
  const p = parts(ms, zone);
  return localMidnight(p.year, p.month, p.day, zone);
}

/** Local midnight `n` calendar days after the local midnight `dayStart`. */
export function addDays(dayStart: number, n: number, zone: string): number {
  const p = parts(dayStart, zone);
  return localMidnight(p.year, p.month, p.day + n, zone);
}

/** Monday 00:00 local of the week containing `ms`. */
export function weekStartOf(ms: number, zone: string): number {
  const day = dayStartOf(ms, zone);
  const p = parts(day, zone);
  const weekday = new Date(Date.UTC(p.year, p.month - 1, p.day)).getUTCDay(); // 0 = Sunday
  return addDays(day, -((weekday + 6) % 7), zone);
}

// ------------------------------------------------------------------ intervals

export type Interval = readonly [startMs: number, endMs: number];

/** Merge overlapping or touching intervals. This is what makes parallel lanes count once. */
export function mergeIntervals(intervals: Interval[]): [number, number][] {
  const sorted = intervals.filter(([s, e]) => e > s).map(([s, e]) => [s, e] as [number, number]).sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const out: [number, number][] = [];
  for (const [s, e] of sorted) {
    const last = out[out.length - 1];
    if (last && s <= last[1]) last[1] = Math.max(last[1], e);
    else out.push([s, e]);
  }
  return out;
}

export function unionMs(intervals: Interval[]): number {
  return mergeIntervals(intervals).reduce((sum, [s, e]) => sum + (e - s), 0);
}

function clip(intervals: Interval[], from: number, to: number): Interval[] {
  const out: Interval[] = [];
  for (const [s, e] of intervals) {
    const cs = Math.max(s, from), ce = Math.min(e, to);
    if (ce > cs) out.push([cs, ce]);
  }
  return out;
}

// ------------------------------------------------------------------ summary

export interface SummaryInput {
  blocks: WeekBlock[];
  weekStart: number;
  zone: string;
  asOf: number;
  boards: BoardInfo[];
  pullRequests?: WeekPullRequest[];
  cards?: WeekCard[];
  stale?: boolean;
  staleReason?: string | null;
}

export function formatDuration(ms: number): string {
  const minutes = Math.round(ms / 60_000);
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

export function boardsForProject(project: string, boards: BoardInfo[]): BoardInfo[] {
  const name = project.toLowerCase();
  return boards.filter(b => b.repos.some(repo => repo.split("/").pop()!.toLowerCase() === name));
}

export function summarizeWeek(input: SummaryInput): WeekData {
  const { zone, weekStart } = input;
  const weekEnd = addDays(weekStart, 7, zone);
  const blocks = input.blocks
    .filter(b => b.endMs > weekStart && b.startMs < weekEnd)
    .map(b => ({ ...b, boards: boardsForProject(b.project, input.boards).map(x => x.id) }))
    .sort((a, b) => a.startMs - b.startMs || a.sessionId.localeCompare(b.sessionId));

  const byProject = new Map<string, WeekBlock[]>();
  for (const block of blocks) byProject.set(block.project, [...(byProject.get(block.project) ?? []), block]);

  const all = blocks.map(b => [b.startMs, b.endMs] as Interval);
  const projects = [...byProject.entries()].map(([project, list]) => {
    const intervals = list.map(b => [b.startMs, b.endMs] as Interval);
    const clipped = clip(intervals, weekStart, weekEnd);
    return {
      project,
      ms: unionMs(clipped),
      sessionMs: clipped.reduce((sum, [s, e]) => sum + (e - s), 0),
      boards: boardsForProject(project, input.boards).map(b => b.id),
    };
  }).sort((a, b) => b.ms - a.ms || a.project.localeCompare(b.project));

  const days: WeekData["days"] = [];
  for (let i = 0, start = weekStart; i < 7; i++) {
    const end = addDays(start, 1, zone);
    days.push({ dayStart: start, dayEnd: end, ms: unionMs(clip(all, start, end)) });
    start = end;
  }

  const noCommit = blocks.filter(b => b.noCommit);
  const commits = new Set(blocks.flatMap(b => b.commitShas)).size;
  const activeMs = unionMs(clip(all, weekStart, weekEnd));
  const sessionMs = clip(all, weekStart, weekEnd).reduce((sum, [s, e]) => sum + (e - s), 0);
  const prs = input.pullRequests ?? [];
  const cards = input.cards ?? [];
  const merged = prs.filter(pr => pr.mergedAt).length;

  const top = projects[0];
  const resume = [...noCommit].sort((a, b) => (b.endMs - b.startMs) - (a.endMs - a.startMs))[0];
  const report: WeekData["report"] = [
    `Shipped: ${commits} commit${commits === 1 ? "" : "s"}${prs.length ? `, ${merged} PR${merged === 1 ? "" : "s"} merged` : ""}.`,
    top ? `Most time: ${top.project}, ${formatDuration(top.ms)} of ${formatDuration(activeMs)}.` : "Most time: no lane activity this week.",
    resume
      ? `Next: resume ${resume.project}${resume.firstMessage ? ` ("${resume.firstMessage.slice(0, 60)}")` : ""}; ${noCommit.length} lane${noCommit.length === 1 ? "" : "s"} stopped without a commit.`
      : "Next: every lane with readable git history made a commit.",
  ];

  const seen = new Set(projects.map(p => p.project));
  return {
    version: 1,
    weekStart,
    weekEnd,
    zone,
    asOf: input.asOf,
    stale: input.stale ?? false,
    staleReason: input.staleReason ?? null,
    totals: { blocks: blocks.length, commits, noCommit: noCommit.length, activeMs, sessionMs, projects: projects.length },
    projects,
    days,
    blocks,
    noCommitBlocks: noCommit.map(b => `${b.sessionId}:${b.startMs}`),
    report,
    pullRequests: prs,
    cards,
    cardTotals: { cards: cards.length, planned: cards.length - cards.filter(c => c.derived).length, derived: cards.filter(c => c.derived).length },
    boards: input.boards.map(b => ({ ...b, projects: [...seen].filter(p => boardsForProject(p, [b]).length > 0).sort(), cards: cards.filter(c => c.boardId === b.id).length })),
  };
}

// ------------------------------------------------------------------ staleness

export const SNAPSHOT_MAX_AGE_MS = 10 * 60_000;

/** A stored snapshot is stale once it is older than `maxAgeMs`; the UI shows the as-of time either way. */
export function snapshotStaleness(asOf: number, now: number, maxAgeMs = SNAPSHOT_MAX_AGE_MS): { stale: boolean; ageMs: number } {
  const ageMs = Math.max(0, now - asOf);
  return { stale: ageMs > maxAgeMs, ageMs };
}
