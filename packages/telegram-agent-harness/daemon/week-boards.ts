/**
 * week-boards.ts — project cards for the Week view and the board/kind filters (issue #482).
 *
 * Cards: ONE batched GraphQL query per board owner, behind `scripts/super-board-gh-guard.sh`, cached
 * 5 minutes, independent of the week asked for (placement is computed per week from the raw dates).
 * Exit 75 from the guard means no `gh` call: the last cards are served with `stale = true`.
 *
 * Placement rule: a card with a planned date range (Target Date, Start date, Iteration) that overlaps
 * the week is placed by it (`derived = false`). Any other card is placed by its latest activity inside
 * the week (updatedAt, closedAt, mergedAt; `derived = true`). A card with neither does not appear.
 */
import type { GithubReaderDeps } from "./week-github";
import { GITHUB_CACHE_MS, GITHUB_QUERY_COST, GITHUB_RETRY_MS } from "./week-github";
import { addDays, calendarDay, summarizeWeek, type BoardInfo, type WeekBlock, type WeekCard, type WeekData } from "./week-summary";

const DAY_MS = 86_400_000;
const ITEMS_PER_BOARD = 100;

export interface RawCard {
  boardId: string;
  type: WeekCard["type"];
  title: string;
  url: string | null;
  repo: string | null;
  number: number | null;
  state: WeekCard["state"];
  updatedAt: number | null;
  closedAt: number | null;
  mergedAt: number | null;
  target: number | null;
  start: number | null;
  iterationStart: number | null;
  iterationEnd: number | null;
}

export interface CardsResult { cards: RawCard[]; fetchedAt: number; stale: boolean; staleReason: string | null; truncatedBoards: string[] }

// ------------------------------------------------------------------ query and parse

const NODE_FIELDS = `nodes { updatedAt
    content { __typename
      ... on Issue { number title url state closedAt repository { nameWithOwner } }
      ... on PullRequest { number title url state closedAt mergedAt repository { nameWithOwner } }
      ... on DraftIssue { title } }
    fieldValues(first: 15) { nodes { __typename
      ... on ProjectV2ItemFieldDateValue { date field { ... on ProjectV2FieldCommon { name } } }
      ... on ProjectV2ItemFieldIterationValue { startDate duration field { ... on ProjectV2FieldCommon { name } } } } } }`;

/**
 * One query per owner; each board is an aliased `projectV2`. The GraphQL page limit is 100 and a wider
 * window times out (HTTP 504, measured on 14 boards), so each board is read as its LAST 100 items: new
 * cards are appended at the end, where the week's activity is. A board with more items than that is
 * reported as truncated.
 */
export function buildCardsQuery(owner: string, boards: BoardInfo[]): string {
  const projects = boards.map((b, i) => `b${i}: projectV2(number: ${b.number}) { items(last: ${ITEMS_PER_BOARD}) { totalCount ${NODE_FIELDS} } }`);
  return `query { repositoryOwner(login: ${JSON.stringify(owner)}) { ... on ProjectV2Owner { ${projects.join("\n")} } } }`;
}

function obj(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value) ? Object.fromEntries(Object.entries(value)) : null;
}

function ms(value: unknown): number | null {
  if (typeof value !== "string") return null;
  // A bare date is a calendar day, kept as UTC midnight of that date (see calendarDay); never as a moment.
  const t = Date.parse(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T00:00:00Z` : value);
  return Number.isFinite(t) ? t : null;
}

function state(value: unknown, mergedAt: number | null): WeekCard["state"] {
  if (mergedAt !== null || value === "MERGED") return "MERGED";
  return value === "CLOSED" ? "CLOSED" : "OPEN";
}

export function parseCards(raw: unknown, boards: BoardInfo[]): { cards: RawCard[]; truncatedBoards: string[] } {
  const cards: RawCard[] = [];
  const truncatedBoards: string[] = [];
  const owner = obj(obj(obj(raw)?.data)?.repositoryOwner);
  if (!owner) return { cards, truncatedBoards };
  boards.forEach((board, i) => {
    const items = obj(obj(owner[`b${i}`])?.items);
    if (!items) return;
    const nodes = Array.isArray(items.nodes) ? items.nodes : [];
    if (typeof items.totalCount === "number" && items.totalCount > nodes.length) truncatedBoards.push(board.id);
    for (const node of nodes) {
      const item = obj(node);
      const content = obj(item?.content);
      if (!item || !content) continue; // redacted or inaccessible item
      const type = content.__typename === "PullRequest" ? "pr" : content.__typename === "Issue" ? "issue" : content.__typename === "DraftIssue" ? "draft" : null;
      if (!type) continue;
      const mergedAt = ms(content.mergedAt);
      const dates = { target: null as number | null, start: null as number | null, iterationStart: null as number | null, iterationEnd: null as number | null };
      const fieldNodes = obj(item.fieldValues)?.nodes;
      for (const value of Array.isArray(fieldNodes) ? fieldNodes : []) {
        const v = obj(value);
        if (!v) continue;
        const name = String(obj(v.field)?.name ?? "").toLowerCase();
        if (v.__typename === "ProjectV2ItemFieldIterationValue") {
          const startMs = ms(v.startDate);
          if (startMs !== null) { dates.iterationStart = startMs; dates.iterationEnd = startMs + (typeof v.duration === "number" ? v.duration : 7) * DAY_MS; }
        } else if (v.__typename === "ProjectV2ItemFieldDateValue") {
          const date = ms(v.date);
          if (date === null) continue;
          if (name === "target date" || name === board.dateField?.toLowerCase()) dates.target = date;
          else if (name === "start date" || name === "start") dates.start = date;
        }
      }
      const repo = obj(content.repository)?.nameWithOwner;
      cards.push({
        boardId: board.id, type, title: String(content.title ?? ""),
        url: typeof content.url === "string" ? content.url : null,
        repo: typeof repo === "string" ? repo : null,
        number: typeof content.number === "number" ? content.number : null,
        state: type === "draft" ? "OPEN" : state(content.state, mergedAt),
        updatedAt: ms(item.updatedAt), closedAt: ms(content.closedAt), mergedAt, ...dates,
      });
    }
  });
  return { cards, truncatedBoards };
}

// ------------------------------------------------------------------ placement

/** Place raw cards in one week. Pure: the same input always gives the same cards. */
export function placeCards(raw: RawCard[], boards: BoardInfo[], weekStart: number, weekEnd: number, zone: string): WeekCard[] {
  // Planned dates are calendar days: compare them with the week's calendar days, then map back to local midnights.
  const calStart = calendarDay(weekStart, zone), calEnd = calendarDay(weekEnd, zone);
  const toLocal = (cal: number) => addDays(weekStart, Math.round((cal - calStart) / DAY_MS), zone);
  const kindOf = new Map(boards.map(b => [b.id, b.kind]));
  const out: WeekCard[] = [];
  for (const card of raw) {
    const starts = [card.start, card.iterationStart, card.target].filter((x): x is number => x !== null);
    const ends = [card.target !== null ? card.target + DAY_MS : null, card.iterationEnd, card.start !== null ? card.start + DAY_MS : null].filter((x): x is number => x !== null);
    const base = { boardId: card.boardId, kind: kindOf.get(card.boardId) ?? "other", title: card.title, url: card.url, repo: card.repo, number: card.number, type: card.type, state: card.state };
    if (starts.length > 0) {
      const from = Math.min(...starts), to = Math.max(...ends);
      if (from < calEnd && to > calStart) {
        const source: WeekCard["source"] = card.target !== null ? "target" : card.iterationStart !== null ? "iteration" : "start";
        out.push({ ...base, at: toLocal(from), endAt: to - from > DAY_MS ? toLocal(to) : null, source, derived: false });
        continue;
      }
    }
    const activity = [card.updatedAt, card.closedAt, card.mergedAt].filter((x): x is number => x !== null && x >= weekStart && x < weekEnd);
    if (activity.length > 0) out.push({ ...base, at: Math.max(...activity), endAt: null, source: "activity", derived: true });
  }
  return out.sort((a, b) => a.at - b.at || a.boardId.localeCompare(b.boardId) || (a.number ?? 0) - (b.number ?? 0));
}

export interface WeekQuery { start: string | null; board: string | null; kind: string | null }

// ------------------------------------------------------------------ reader

export class CardsReader {
  private last: CardsResult | null = null;
  private inflight: Promise<CardsResult> | null = null;
  private checkedAt = 0;

  constructor(private readonly deps: GithubReaderDeps, private readonly boards: BoardInfo[], private readonly cacheMs = GITHUB_CACHE_MS) {}

  read(): Promise<CardsResult> {
    this.inflight ??= this.refresh().finally(() => { this.inflight = null; });
    return this.inflight;
  }

  private owners(): Map<string, BoardInfo[]> {
    const byOwner = new Map<string, BoardInfo[]>();
    for (const board of this.boards) byOwner.set(board.owner, [...(byOwner.get(board.owner) ?? []), board]);
    return byOwner;
  }

  private async refresh(): Promise<CardsResult> {
    const now = this.deps.now();
    if (this.last && !this.last.stale && now - this.last.fetchedAt < this.cacheMs) return this.last;
    if (this.last?.stale && now - this.checkedAt < GITHUB_RETRY_MS) return this.last;
    this.checkedAt = now;
    const byOwner = this.owners();
    const code = await this.deps.guard(GITHUB_QUERY_COST * 5 * byOwner.size);
    if (code !== 0) return this.stale(now, code === 75 ? "GitHub quota reserve reached; showing the last cards." : "GitHub guard unavailable; showing the last cards.");
    try {
      const cards: RawCard[] = [];
      const truncatedBoards: string[] = [];
      for (const [owner, boards] of byOwner) {
        const parsed = parseCards(await this.deps.graphql(buildCardsQuery(owner, boards)), boards);
        cards.push(...parsed.cards);
        truncatedBoards.push(...parsed.truncatedBoards);
      }
      this.last = { cards, fetchedAt: now, stale: false, staleReason: null, truncatedBoards };
      return this.last;
    } catch {
      return this.stale(now, "GitHub card read failed; showing the last cards.");
    }
  }

  private stale(now: number, reason: string): CardsResult {
    const previous = this.last ?? { cards: [], fetchedAt: 0, stale: true, staleReason: null, truncatedBoards: [] };
    this.last = { ...previous, stale: true, staleReason: reason };
    return this.last;
  }
}

// ------------------------------------------------------------------ filters

export interface WeekFilter { board: string | null; kind: string | null }

export const ALL_BOARDS = "all";
export const UNASSIGNED = "unassigned";

/**
 * Filter a full week by board id (or `all` / `unassigned`) and board kind. Blocks, projects, PRs, cards
 * and totals are all recomputed from the filtered parts, so totals of `all` equal the sum over boards
 * for cards, and every lane appears under exactly the boards its project belongs to.
 */
export function filterWeek(data: WeekData, filter: WeekFilter, zone: string): WeekData {
  const board = filter.board && filter.board !== ALL_BOARDS ? filter.board : null;
  const kind = filter.kind || null;
  if (!board && !kind) return data;
  const cards = Array.isArray(data.cards) ? data.cards : [];
  const unassigned = board === UNASSIGNED;
  const wanted = new Set(data.boards.filter(b => (unassigned || !board || b.id === board) && (!kind || b.kind === kind)).map(b => b.id));
  const repos = new Set(data.boards.filter(b => wanted.has(b.id)).flatMap(b => b.repos));
  const allRepos = new Set(data.boards.flatMap(b => b.repos));
  const keepBlock = (b: WeekBlock) => unassigned ? b.boards.length === 0 && !kind : b.boards.some(id => wanted.has(id));
  const filtered = summarizeWeek({
    blocks: data.blocks.filter(keepBlock), weekStart: data.weekStart, zone, asOf: data.asOf,
    boards: data.boards, stale: data.stale, staleReason: data.staleReason,
    pullRequests: data.pullRequests.filter(pr => unassigned ? !allRepos.has(pr.repo) && !kind : repos.has(pr.repo)),
    cards: unassigned ? [] : cards.filter(c => wanted.has(c.boardId)),
  });
  return { ...filtered, boards: data.boards };
}
