/**
 * week-service.ts — builds `GET /api/week` (issue #477): scan, store, count commits, read GitHub
 * behind the guard, summarize, and keep the last snapshot with its as-of time.
 */
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";
import type { FleetSnapshot } from "./fleet-state";
import { defaultGithubDeps, GithubReader, type GithubReaderDeps } from "./week-github";
import { CardsReader, filterWeek, placeCards, type WeekFilter, type WeekQuery } from "./week-boards";
import { gitCommitCounter, scanSessionRoot, withCommits, type CommitCounter } from "./week-sessions";
import { WeekStore } from "./week-store";
import { resolveSessionsRoots } from "./session-control";
import { addDays, localZone, snapshotStaleness, summarizeWeek, weekStartOf, type BoardInfo, type WeekData } from "./week-summary";

export interface WeekServiceOptions {
  store: WeekStore;
  /** Directories holding Veyyon session JSONL files (searched recursively). */
  sessionRoots: string[];
  boards: BoardInfo[];
  github?: GithubReader;
  cards?: CardsReader;
  fleet?: () => Promise<FleetSnapshot | null>;
  countCommits?: CommitCounter;
  zone?: string;
  now?: () => number;
}

export function loadBoards(file: string): BoardInfo[] {
  const raw: unknown = JSON.parse(fs.readFileSync(file, "utf8"));
  const list = raw && typeof raw === "object" && "boards" in raw && Array.isArray(raw.boards) ? raw.boards : [];
  const out: BoardInfo[] = [];
  for (const item of list) {
    if (!item || typeof item !== "object") continue;
    const b = Object.fromEntries(Object.entries(item));
    if (typeof b.id !== "string" || typeof b.owner !== "string" || typeof b.number !== "number") continue;
    out.push({
      id: b.id, title: String(b.title ?? b.id), kind: String(b.kind ?? "other"), color: String(b.color ?? "#94a3b8"),
      repos: Array.isArray(b.repos) ? b.repos.map(String) : [], owner: b.owner, number: b.number,
      dateField: typeof b.dateField === "string" ? b.dateField : null,
    });
  }
  return out;
}

/** `start` is epoch ms or an ISO date; anything else (or absent) means the current week. */
export function parseStart(value: string | null | undefined, now: number): number {
  if (!value) return now;
  const n = /^\d{9,}$/.test(value) ? Number(value) : Date.parse(value);
  return Number.isFinite(n) ? n : now;
}

export class WeekService {
  private readonly zone: string;
  private readonly now: () => number;
  private readonly count: CommitCounter;

  constructor(private readonly options: WeekServiceOptions) {
    this.zone = options.zone ?? localZone();
    this.now = options.now ?? Date.now;
    this.count = options.countCommits ?? gitCommitCounter;
  }

  async get(start: number = this.now()): Promise<WeekData> {
    const now = this.now();
    const weekStart = weekStartOf(start, this.zone);
    const weekEnd = addDays(weekStart, 7, this.zone);
    const cached = this.options.store.lastSnapshot(weekStart);
    // A snapshot built after the week ended is final unless its GitHub part was stale. One built mid-week is rebuilt once the week is over.
    if (cached && cached.asOf >= weekEnd && !cached.stale) return cached;
    if (cached && !snapshotStaleness(cached.asOf, now).stale) return cached;
    try {
      for (const root of this.options.sessionRoots) this.options.store.appendBlocks(scanSessionRoot(root, weekStart));
      const fleet = await (this.options.fleet?.() ?? Promise.resolve(null)).catch(() => null);
      const blocks = await withCommits(this.options.store.blocksBetween(weekStart, weekEnd), this.count, fleet);
      const github = this.options.github ? await this.options.github.read(weekStart) : null;
      const cards = this.options.cards ? await this.options.cards.read() : null;
      const data = summarizeWeek({
        blocks, weekStart, zone: this.zone, asOf: now, boards: this.options.boards,
        pullRequests: github?.pullRequests, cards: cards ? placeCards(cards.cards, this.options.boards, weekStart, weekEnd, this.zone) : undefined,
        stale: (github?.stale ?? false) || (cards?.stale ?? false),
        staleReason: github?.staleReason ?? cards?.staleReason ?? null,
      });
      this.options.store.saveSnapshot(data);
      return data;
    } catch {
      if (cached) return { ...cached, stale: true, staleReason: "Rebuild failed; showing the last snapshot." };
      throw new Error("week data unavailable");
    }
  }

  /** The week for one board and kind filter; the stored snapshot always holds the unfiltered week. */
  async getFiltered(start: number, filter: WeekFilter): Promise<WeekData> {
    return filterWeek(await this.get(start), filter, this.zone);
  }
}

/** Production wiring for the daemon: one store under the slot state dir, guard-gated GitHub owners from boards.json. */
export function defaultWeekRoute(stateDir: string, fleet?: () => Promise<FleetSnapshot | null>): (query: WeekQuery) => Promise<WeekData> {
  const here = path.dirname(fileURLToPath(import.meta.url));
  const boards = loadBoards(path.join(here, "..", "week", "boards.json"));
  const owners = [...new Set(boards.flatMap(b => b.repos.map(r => r.split("/")[0]!)))].slice(0, 6);
  const repoRoot = process.env.SUPERBOARD_REPO_ROOT ?? path.resolve(here, "..", "..", "..");
  const store = new WeekStore(path.join(stateDir, "week.sqlite"));
  const deps: GithubReaderDeps = defaultGithubDeps(repoRoot);
  const github = new GithubReader(deps, owners);
  const cards = new CardsReader(deps, boards);
  const service = new WeekService({ store, sessionRoots: resolveSessionsRoots(), boards, github, cards, fleet });
  return query => service.getFiltered(parseStart(query.start, Date.now()), { board: query.board, kind: query.kind });
}
