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

// Lane fills are CSS tokens (--lane-0 … --lane-9 in week.css), one set per colour scheme.
export const LANE_COLOURS = 10;

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

/**
 * Stable colour per project, assigned over the whole week so filtering never recolours a block.
 * The value is a CSS token reference, so a scheme change recolours every lane without a re-render.
 */
export function projectColors(data) {
  const names = [...new Set(data.blocks.map(b => b.project))].sort();
  return new Map(names.map((name, i) => [name, `var(--lane-${i % LANE_COLOURS})`]));
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

/**
 * The `start` instant sent for the week whose Monday is `iso`: noon UTC on that date. The server
 * floors `start` to a Monday in the PC's zone, and noon UTC falls on that Monday or the Tuesday after
 * it in every zone from UTC-12 to UTC+14, so the browser's own zone never shifts the week.
 */
export function weekRequestStart(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return Date.UTC(y, m - 1, d, 12);
}

/** The calendar date of `ms` in `zone`, as YYYY-MM-DD. */
export function zoneIsoDate(ms, zone) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(ms);
  const part = type => parts.find(p => p.type === type).value;
  return `${part('year')}-${part('month')}-${part('day')}`;
}

export function isoDate(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function shiftWeek(iso, weeks) {
  const [y, m, d] = iso.split('-').map(Number);
  return isoDate(new Date(y, m - 1, d + weeks * 7));
}

/** The weeks prev and next open, or null while the week on screen is unknown (no week loaded yet). */
export function adjacentWeeks(iso) {
  return iso ? { prev: shiftWeek(iso, -1), next: shiftWeek(iso, 1) } : null;
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

/** `a` moved `t` (0…1) of the way toward `b`, both #rrggbb, mixed in sRGB. */
export function mixColors(a, b, t) {
  return `#${[1, 3, 5].map(i => {
    const from = parseInt(a.slice(i, i + 2), 16);
    return Math.round(from + (parseInt(b.slice(i, i + 2), 16) - from) * t).toString(16).padStart(2, '0');
  }).join('')}`;
}

/** `color` moved toward `toward` in 5% steps until it keeps `min`:1 against every background. */
function readable(color, toward, backgrounds, min) {
  for (let step = 0; step < 20; step += 1) {
    const mixed = mixColors(color, toward, step / 20);
    if (backgrounds.every(bg => contrastRatio(mixed, bg) >= min)) return mixed;
  }
  return toward;
}

const HEX = /^#[0-9a-f]{6}$/i;

/** The scheme's colours that are drawn on the page surfaces; telegramTokens re-checks them on Telegram's. */
export const SURFACE_BOUND = ['--red', '--red-text', '--amber', ...Array.from({ length: 10 }, (_, i) => `--lane-${i}`)];

/**
 * Week view tokens from Telegram's theme (Telegram.WebApp.themeParams), or null when there is none
 * to use: outside Telegram, or a theme whose text does not reach 4.5:1 on its own surfaces.
 * Telegram supplies the surfaces and the text. Users pick those colours, so muted and accent text are
 * pulled toward the text colour until they keep 4.5:1, and control borders until 3:1 (WCAG AA).
 * `own` holds the scheme's SURFACE_BOUND colours from week.css. Each one is pulled toward the text colour
 * the same way: status text to 4.5:1, the status ring and lane fills to 3:1. Their tints and edges become
 * solid colours derived from them, so every pair stays checkable.
 */
export function telegramTokens(params, own = {}) {
  const pick = (...keys) => keys.map(key => params?.[key]).find(value => HEX.test(value ?? ''));
  const panel = pick('section_bg_color', 'bg_color');
  const text = pick('text_color');
  if (!panel || !text) return null;
  const bg = pick('secondary_bg_color', 'bg_color');
  const accent = pick('accent_text_color', 'link_color') ?? text;
  const surfaces = [bg, panel, mixColors(panel, text, 0.06)];
  const buttons = [mixColors(panel, accent, 0.12), mixColors(panel, accent, 0.22)];
  if (![...surfaces, ...buttons].every(surface => contrastRatio(text, surface) >= 4.5)) return null;
  const tokens = {
    '--bg': bg, '--panel': panel, '--panel-2': surfaces[2], '--btn': buttons[0], '--btn-hover': buttons[1],
    '--line': pick('section_separator_color') ?? mixColors(panel, text, 0.14),
    '--text': text, '--focus': text,
    '--muted': readable(pick('hint_color', 'subtitle_text_color') ?? mixColors(panel, text, 0.4), text, surfaces, 4.5),
    '--accent': readable(accent, text, surfaces, 4.5),
    '--ctl-line': readable(mixColors(panel, text, 0.35), text, surfaces, 3),
  };
  const color = name => (HEX.test(own[name] ?? '') ? own[name] : null);
  if (color('--red')) {
    const red = readable(color('--red'), text, [bg, panel], 3);
    Object.assign(tokens, { '--red': red, '--red-edge': red, '--red-tint': mixColors(bg, red, 0.1) });
  }
  if (color('--red-text')) {
    const tint = tokens['--red-tint'] ?? mixColors(bg, color('--red-text'), 0.1);
    tokens['--red-text'] = readable(color('--red-text'), text, [panel, surfaces[2], tint], 4.5);
  }
  if (color('--amber')) {
    const tint = mixColors(bg, color('--amber'), 0.1);
    const amber = readable(color('--amber'), text, [tint], 4.5);
    Object.assign(tokens, { '--amber': amber, '--amber-edge': amber, '--amber-tint': tint });
  }
  for (const name of SURFACE_BOUND.filter(name => name.startsWith('--lane-') && color(name))) {
    tokens[name] = readable(color(name), text, [bg, panel], 3);
  }
  return tokens;
}
