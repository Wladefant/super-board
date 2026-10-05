// Pure model for the Week view: board selection, per-selection totals and grid layout.
// No DOM access, so tests run it directly. Input is the WeekData v1 payload of GET /api/week.

/**
 * @typedef {{ sessionId: string, project: string, cwd?: string, tool?: string, model?: string,
 *   startMs: number, endMs: number, firstMessage?: string, commits: number | null,
 *   noCommit: boolean, live?: boolean, title?: string, filesChanged?: number, boards?: string[] }} Block
 * @typedef {{ id: string, title: string, kind: string, color?: string, repos?: string[] }} Board
 * @typedef {{ boardId: string, kind: string, title: string, url: string | null, repo: string | null, number: number | null,
 *   type: 'issue'|'pr'|'draft', state: 'OPEN'|'CLOSED'|'MERGED', at: number, endAt: number | null,
 *   source: 'target'|'start'|'iteration'|'activity', derived: boolean }} Card
 * @typedef {{ version: 1, weekStart: number, weekEnd: number, zone: string, asOf: number, stale: boolean,
 *   staleReason?: string | null, totals: object, projects: { project: string, ms: number, sessionMs: number, boards: string[] }[],
 *   days: { dayStart: number, dayEnd: number, ms: number }[], blocks: Block[], noCommitBlocks: string[],
 *   report: [string, string, string], pullRequests: { repo: string, number: number, title: string, url: string,
 *   state: string, mergedAt: string | null, branch: string }[], boards: Board[], cards?: Card[] }} WeekData
 */

export const BOARD_PARAM = 'board';
export const WEEK_PARAM = 'week';
export const BOARD_STORAGE_KEY = 'superboard.week.board';
export const ALL = 'all';
export const UNASSIGNED = 'unassigned';
const KIND_PREFIX = 'kind:';

export const KIND_LABELS = {
  'veyyon-lanes': 'Veyyon lanes',
  polysimulator: 'PolySimulator',
  shipnovo: 'Shipnovo',
  ing: 'ING',
  'product-fork': 'Product fork',
  other: 'Other boards',
};

// Pastel fills: every entry keeps 4.5:1 or more against the dark ink #0b1020 (tested).
export const PROJECT_PALETTE = ['#8ab4ff', '#7ee0b8', '#f5c06b', '#d9a6ff', '#7fd7e6', '#e6e07a', '#ffb38a', '#b7c4dd', '#a8e6a1', '#f2a7d0'];
export const INK = '#0b1020';

export const kindLabel = kind => KIND_LABELS[kind] || kind;

/** Dropdown options: All boards, one calendar per kind present, every board, and Unassigned. */
export function boardOptions(boards) {
  const kinds = [...new Set(boards.map(b => b.kind))].sort((a, b) => kindLabel(a).localeCompare(kindLabel(b)));
  return {
    all: { value: ALL, label: 'All boards' },
    kinds: kinds.map(kind => ({ value: KIND_PREFIX + kind, label: kindLabel(kind) })),
    boards: [...boards].sort((a, b) => a.title.localeCompare(b.title)).map(b => ({ value: b.id, label: b.title })),
    unassigned: { value: UNASSIGNED, label: 'Unassigned lanes' },
  };
}

export function isValidSelection(value, boards) {
  if (value === ALL || value === UNASSIGNED) return true;
  if (typeof value !== 'string' || !value) return false;
  if (value.startsWith(KIND_PREFIX)) return boards.some(b => KIND_PREFIX + b.kind === value);
  return boards.some(b => b.id === value);
}

/** The URL wins on load, then localStorage, then All boards. Invalid values fall through. */
export function resolveSelection(urlValue, storedValue, boards) {
  if (isValidSelection(urlValue, boards)) return urlValue;
  if (isValidSelection(storedValue, boards)) return storedValue;
  return ALL;
}

export function selectionLabel(value, boards) {
  if (value === ALL) return 'All boards';
  if (value === UNASSIGNED) return 'Unassigned lanes';
  if (value.startsWith(KIND_PREFIX)) return kindLabel(value.slice(KIND_PREFIX.length));
  return boards.find(b => b.id === value)?.title || value;
}

/** Board ids of a block: its own list when the server sends one, else its project's boards. */
export function blockBoards(block, data) {
  if (Array.isArray(block.boards)) return block.boards;
  return data.projects.find(p => p.project === block.project)?.boards || [];
}

/** Board ids the selection covers; null means every board (All). */
function selectedBoardIds(value, boards) {
  if (value === ALL) return null;
  if (value === UNASSIGNED) return new Set();
  if (value.startsWith(KIND_PREFIX)) return new Set(boards.filter(b => KIND_PREFIX + b.kind === value).map(b => b.id));
  return new Set([value]);
}

export function selectBlocks(data, value) {
  const ids = selectedBoardIds(value, data.boards);
  if (!ids) return data.blocks;
  if (value === UNASSIGNED) return data.blocks.filter(b => blockBoards(b, data).length === 0);
  return data.blocks.filter(b => blockBoards(b, data).some(id => ids.has(id)));
}

export function selectCards(data, value) {
  const cards = data.cards || [];
  const ids = selectedBoardIds(value, data.boards);
  return ids ? cards.filter(c => ids.has(c.boardId)) : cards;
}

function selectPullRequests(data, value) {
  const ids = selectedBoardIds(value, data.boards);
  if (!ids) return data.pullRequests;
  const repos = new Set(data.boards.filter(b => ids.has(b.id)).flatMap(b => b.repos || []));
  return data.pullRequests.filter(pr => repos.has(pr.repo));
}

/** Cards shown on a day: placed on it, or a planned range (at to endAt, exclusive) that covers it. */
export function cardsOnDay(cards, day) {
  return cards.filter(c => (c.endAt && c.endAt > c.at ? c.at < day.dayEnd && c.endAt > day.dayStart : c.at >= day.dayStart && c.at < day.dayEnd));
}

/**
 * Cards as bars over the days they cover: `from`/`to` are inclusive day indexes. Bars sort by first
 * day, then longest first, so a CSS grid with dense flow packs them into as few rows as possible.
 */
export function cardSpans(cards, days) {
  return cards
    .map(card => {
      const hits = days.flatMap((day, i) => (cardsOnDay([card], day).length ? [i] : []));
      return hits.length ? { card, from: hits[0], to: hits[hits.length - 1] } : null;
    })
    .filter(span => span !== null)
    .sort((a, b) => a.from - b.from || b.to - a.to || a.card.title.localeCompare(b.card.title));
}

/** Length of the union of [start, end) intervals clipped to [from, to): parallel time counts once. */
export function unionMs(intervals, from = -Infinity, to = Infinity) {
  const clipped = intervals
    .map(([s, e]) => [Math.max(s, from), Math.min(e, to)])
    .filter(([s, e]) => e > s)
    .sort((a, b) => a[0] - b[0]);
  let total = 0;
  let curS = 0;
  let curE = -Infinity;
  for (const [s, e] of clipped) {
    if (s > curE) {
      if (curE > curS) total += curE - curS;
      curS = s;
      curE = e;
    } else if (e > curE) curE = e;
  }
  if (curE > curS) total += curE - curS;
  return total;
}

/** Stable colour per project, assigned over the whole week so filtering never recolours a block. */
export function projectColors(data) {
  const names = [...new Set(data.blocks.map(b => b.project))].sort();
  return new Map(names.map((name, i) => [name, PROJECT_PALETTE[i % PROJECT_PALETTE.length]]));
}

export const commitCount = block => (typeof block.commits === 'number' ? block.commits : 0);
export const blockKey = block => `${block.sessionId}:${block.startMs}`;

export function blockTitle(block) {
  if (block.title) return block.title;
  const first = (block.firstMessage || '').split('\n').find(line => line.trim());
  return first ? first.trim() : block.project;
}

/** Totals, sidebar and report for one selection. Lane counts, commits and session time are additive across boards. */
export function summarize(data, value) {
  const blocks = selectBlocks(data, value);
  const intervals = b => [b.startMs, Math.max(b.startMs, Math.min(b.endMs, data.weekEnd))];
  const byProject = new Map();
  for (const block of blocks) {
    if (!byProject.has(block.project)) byProject.set(block.project, []);
    byProject.get(block.project).push(intervals(block));
  }
  const projects = [...byProject].map(([project, list]) => ({
    project,
    ms: unionMs(list, data.weekStart, data.weekEnd),
    sessionMs: list.reduce((sum, [s, e]) => sum + Math.max(0, Math.min(e, data.weekEnd) - Math.max(s, data.weekStart)), 0),
  })).sort((a, b) => b.ms - a.ms || a.project.localeCompare(b.project));
  const all = blocks.map(intervals);
  const days = data.days.map(day => ({ ...day, ms: unionMs(all, day.dayStart, day.dayEnd) }));
  const noCommit = blocks.filter(b => b.noCommit).sort((a, b) => b.endMs - b.startMs - (a.endMs - a.startMs));
  const pullRequests = selectPullRequests(data, value);
  // Same rules as the server's totals: activeMs is the union of every lane, sessionMs the plain sum.
  const totalMs = unionMs(all, data.weekStart, data.weekEnd);
  const sessionMs = projects.reduce((sum, p) => sum + p.sessionMs, 0);
  return {
    blocks,
    cards: selectCards(data, value),
    projects,
    days,
    noCommit,
    pullRequests,
    totalMs,
    sessionMs,
    commits: blocks.reduce((sum, b) => sum + commitCount(b), 0),
    report: value === ALL && data.report?.length === 3 ? data.report : buildReport(projects, noCommit, pullRequests, blocks),
  };
}

function buildReport(projects, noCommit, pullRequests, blocks) {
  const merged = pullRequests.filter(pr => pr.mergedAt || String(pr.state).toUpperCase() === 'MERGED');
  const shipped = merged.length
    ? `Shipped: ${merged.length} merged PR${merged.length === 1 ? '' : 's'}, latest ${[...merged].sort((a, b) => (Date.parse(b.mergedAt ?? '') || 0) - (Date.parse(a.mergedAt ?? '') || 0))[0].title}.`
    : `Shipped: no merged PRs; ${blocks.length} lane${blocks.length === 1 ? '' : 's'} ran.`;
  const most = projects[0] ? `Most time: ${projects[0].project}, ${formatDuration(projects[0].ms)}.` : 'Most time: no lanes this week.';
  const next = noCommit.length
    ? `Next: resume ${noCommit.slice(0, 2).map(blockTitle).join(' and ')} first; ${noCommit.length === 1 ? 'it' : 'they'} stopped with no commit.`
    : blocks.length ? 'Next: every lane ended with a commit.' : 'Next: start a lane from the board.';
  return [shipped, most, next];
}

export function formatDuration(ms) {
  const minutes = Math.round(ms / 60_000);
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m`;
}

/** Wall-clock minutes since local midnight in the payload's zone (DST-safe). */
export function minutesOfDay(ms, zone) {
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: zone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(ms);
  const get = type => Number(parts.find(p => p.type === type)?.value || 0);
  return get('hour') * 60 + get('minute');
}

/** Split blocks into per-day segments, so a lane past midnight shows on both days. */
export function daySegments(blocks, days) {
  return days.map(day => blocks
    .filter(b => b.startMs < day.dayEnd && b.endMs > day.dayStart)
    .map(b => ({ block: b, startMs: Math.max(b.startMs, day.dayStart), endMs: Math.min(b.endMs, day.dayEnd) })));
}

/**
 * Place overlapping segments side by side. Each cluster of mutually reachable overlaps gets
 * min(columns, maxColumns) columns; segments beyond that are returned as `hidden` per cluster.
 */
export function layoutDay(segments, maxColumns = 4) {
  const sorted = [...segments].sort((a, b) => a.startMs - b.startMs || b.endMs - a.endMs);
  const clusters = [];
  let current = null;
  for (const seg of sorted) {
    if (!current || seg.startMs >= current.endMs) {
      current = { endMs: seg.endMs, items: [] };
      clusters.push(current);
    }
    current.endMs = Math.max(current.endMs, seg.endMs);
    current.items.push(seg);
  }
  const placed = [];
  const overflow = [];
  for (const cluster of clusters) {
    const columnEnds = [];
    const assigned = cluster.items.map(seg => {
      let col = columnEnds.findIndex(end => end <= seg.startMs);
      if (col === -1) { col = columnEnds.length; columnEnds.push(seg.endMs); } else columnEnds[col] = seg.endMs;
      return { ...seg, column: col };
    });
    const total = columnEnds.length;
    const shown = Math.min(total, maxColumns);
    const hidden = assigned.filter(s => s.column >= shown);
    // With overflow, the last visible column holds the "+N more" control instead of a block.
    const visibleCols = hidden.length ? shown - 1 : shown;
    for (const seg of assigned) {
      if (seg.column < visibleCols) placed.push({ ...seg, columns: shown });
    }
    if (hidden.length || visibleCols < shown) {
      const rest = assigned.filter(s => s.column >= visibleCols);
      overflow.push({ startMs: cluster.items[0].startMs, endMs: cluster.endMs, column: visibleCols, columns: shown, segments: rest });
    }
  }
  return { placed, overflow };
}

/**
 * Hour the grid scrolls to first: the earliest start of a lane that begins that day. A lane that
 * only continues past midnight does not count, so a 00:30 tail never hides the working day.
 */
export function firstHour(segmentsByDay, zone) {
  let hour = 24;
  for (const day of segmentsByDay) {
    for (const seg of day) {
      if (seg.startMs === seg.block.startMs) hour = Math.min(hour, Math.floor(minutesOfDay(seg.startMs, zone) / 60));
    }
  }
  return hour === 24 ? 8 : hour;
}

/** Monday 00:00 local of the week containing `date` (a Date), as YYYY-MM-DD. */
export function mondayOf(date) {
  const d = new Date(date.getFullYear(), date.getMonth(), date.getDate());
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return isoDate(d);
}

export function isoDate(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function shiftWeek(iso, weeks) {
  const [y, m, d] = iso.split('-').map(Number);
  return isoDate(new Date(y, m - 1, d + weeks * 7));
}

export function isValidWeek(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return false;
  const [y, m, d] = value.split('-').map(Number);
  const date = new Date(y, m - 1, d);
  return date.getMonth() === m - 1 && date.getDay() === 1;
}

/** WCAG relative-luminance contrast ratio of two #rrggbb colours. */
export function contrastRatio(a, b) {
  const lum = hex => {
    const [r, g, bl] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255)
      .map(c => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}
