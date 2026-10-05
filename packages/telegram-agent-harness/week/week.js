import { createClient, isTerminalAuthError, OPEN_FROM_TELEGRAM } from './client.js';
import {
  ALL, BOARD_PARAM, BOARD_STORAGE_KEY, WEEK_PARAM,
  blockBoards, blockTitle, boardOptions, cardSpans, cardsOnDay, commitCount, daySegments, firstHour, formatDuration,
  isValidWeek, kindLabel, layoutDay, minutesOfDay, mondayOf, projectColors, resolveSelection,
  selectBlocks, selectionLabel, shiftWeek, summarize,
} from './week-model.js';

const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

const $ = id => document.getElementById(id);
const HOUR_PX = 48;
const REFRESH_MS = 120_000;
const request = createClient(() => tg?.initData || '');

const state = {
  week: initialWeek(),
  board: new URLSearchParams(location.search).get(BOARD_PARAM) || readStored() || ALL,
  data: null,
  loading: false,
  trigger: null,
};

function initialWeek() {
  const value = new URLSearchParams(location.search).get(WEEK_PARAM);
  return isValidWeek(value) ? value : mondayOf(new Date());
}

function readStored() {
  try { return localStorage.getItem(BOARD_STORAGE_KEY); } catch { return null; }
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'style') Object.assign(node.style, value);
    else if (key.startsWith('--')) node.style.setProperty(key, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  node.append(...children.flat().filter(c => c != null && c !== false));
  return node;
}

// ---- formatting in the payload's zone ----
let fmt = null;
function formatters(zone) {
  if (fmt?.zone === zone) return fmt;
  const make = opts => new Intl.DateTimeFormat('en-GB', { timeZone: zone, ...opts });
  fmt = {
    zone,
    time: make({ hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }),
    weekday: make({ weekday: 'short' }),
    dayLong: make({ weekday: 'short', day: 'numeric', month: 'short' }),
    monthDay: new Intl.DateTimeFormat('en-US', { timeZone: zone, month: 'short', day: 'numeric' }),
    stamp: make({ weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }),
  };
  return fmt;
}
const timeRange = (f, s, e) => `${f.time.format(s)}–${f.time.format(e)}`;
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

// ---- URL and storage ----
function writeUrl() {
  const params = new URLSearchParams(location.search);
  params.set(WEEK_PARAM, state.week);
  params.set(BOARD_PARAM, state.board);
  history.replaceState(null, '', `${location.pathname}?${params}${location.hash}`);
}

// ---- data ----
async function load({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  const week = state.week;
  if (!quiet) showLoading();
  try {
    const [y, m, d] = week.split('-').map(Number);
    const data = await request(`/api/week?start=${new Date(y, m - 1, d).getTime()}`);
    if (week !== state.week) return;
    if (data?.version !== 1 || !Array.isArray(data.blocks)) throw new Error('The server sent a week format this page does not know.');
    state.data = data;
    state.board = resolveSelection(state.board, readStored(), data.boards || []);
    writeUrl();
    hideNotice();
    render();
  } catch (error) {
    if (week !== state.week) return;
    showError(error);
  } finally {
    state.loading = false;
    if (week !== state.week) load();
  }
}

function showLoading() {
  $('calendar').setAttribute('aria-busy', 'true');
  $('week-title').textContent = weekTitleFromIso(state.week);
  $('grid').replaceChildren(el('p', { class: 'placeholder' }, 'Loading week…'));
  $('agenda').replaceChildren(el('p', { class: 'placeholder' }, 'Loading week…'));
}

function showError(error) {
  $('calendar').setAttribute('aria-busy', 'false');
  const auth = isTerminalAuthError(error);
  const message = auth ? OPEN_FROM_TELEGRAM : `Could not load this week. ${error?.message || ''}`.trim();
  const notice = $('notice');
  notice.hidden = false;
  notice.className = 'notice notice-error';
  notice.replaceChildren(el('span', {}, message), auth ? null : el('button', { type: 'button', class: 'btn', onclick: () => load() }, 'Try again'));
  if (!state.data) {
    $('grid').replaceChildren(el('p', { class: 'placeholder' }, auth ? 'Sign in to see the week.' : 'No data loaded.'));
    $('agenda').replaceChildren(el('p', { class: 'placeholder' }, auth ? 'Sign in to see the week.' : 'No data loaded.'));
  }
}

function hideNotice() {
  $('notice').hidden = true;
  $('notice').replaceChildren();
}

function weekTitleFromIso(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  const start = new Date(y, m - 1, d);
  const end = new Date(y, m - 1, d + 6);
  const f = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric' });
  return `${f.format(start)} – ${f.format(end)}`;
}

// ---- render ----
function render() {
  const data = state.data;
  if (!data) return;
  const f = formatters(data.zone);
  const summary = summarize(data, state.board);
  const colors = projectColors(data);
  $('calendar').setAttribute('aria-busy', 'false');
  $('week-title').textContent = `${f.monthDay.format(data.weekStart)} – ${f.monthDay.format(data.weekEnd - 1)}`;
  renderBoardSelect(data);
  renderStats(summary);
  renderAsOf(data, f);
  renderGrid(data, summary, colors, f);
  renderAgenda(data, summary, colors, f);
  renderSidebar(data, summary, colors, f);
}

function renderBoardSelect(data) {
  const select = $('board');
  const opts = boardOptions(data.boards || []);
  const count = value => selectBlocks(data, value).length;
  const option = o => el('option', { value: o.value, selected: o.value === state.board }, `${o.label} · ${plural(count(o.value), 'lane')}`);
  select.replaceChildren(
    option(opts.all),
    opts.kinds.length ? el('optgroup', { label: 'Calendars by kind' }, opts.kinds.map(option)) : null,
    opts.boards.length ? el('optgroup', { label: 'Boards' }, opts.boards.map(option)) : null,
    option(opts.unassigned),
  );
  select.value = state.board;
}

function renderStats(summary) {
  const nc = summary.noCommit.length;
  $('stats').replaceChildren(
    el('span', { class: 'num' }, plural(summary.blocks.length, 'lane')),
    el('span', { 'aria-hidden': 'true', class: 'dot-sep' }, '·'),
    el('span', { class: 'num' }, plural(summary.commits, 'commit')),
    nc ? el('span', { class: 'badge-nc' }, el('span', { class: 'swatch-nc', 'aria-hidden': 'true' }), `${nc} stopped with no commit`) : null,
  );
}

function renderAsOf(data, f) {
  const node = $('asof');
  node.hidden = !data.stale;
  if (!data.stale) return;
  node.textContent = `Snapshot as of ${f.stamp.format(data.asOf)}`;
  node.title = data.staleReason || 'The PC is not sending live data. This is the last stored snapshot.';
}

function blockLabel(block, f) {
  const commits = block.commits == null ? 'commits unknown' : plural(commitCount(block), 'commit');
  return `${blockTitle(block)}. ${block.project}. ${f.dayLong.format(block.startMs)} ${timeRange(f, block.startMs, block.endMs)}, ${formatDuration(block.endMs - block.startMs)}. ${block.noCommit ? 'Stopped with no commit.' : commits + '.'}${block.live ? ' Running now.' : ''}`;
}

function renderGrid(data, summary, colors, f) {
  const grid = $('grid');
  if (!summary.blocks.length && !summary.cards.length) {
    grid.replaceChildren(emptyMessage(data));
    return;
  }
  const segments = daySegments(summary.blocks, data.days);
  const hours = 24;
  const head = el('div', { class: 'grid-head' }, el('div', { class: 'gutter' }),
    data.days.map((day, i) => el('div', { class: 'day-head' },
      el('span', { class: 'day-name' }, f.dayLong.format(day.dayStart)),
      el('span', { class: 'day-hours num' }, formatDuration(summary.days[i].ms)))));
  // Cards sit in a 7-column grid: one bar per card across every day it covers.
  const allDay = summary.cards.length ? el('div', { class: 'grid-allday' },
    el('div', { class: 'gutter gutter-label' }, 'Cards'),
    el('div', { class: 'allday-lines', 'aria-hidden': 'true' }, data.days.map(() => el('span'))),
    el('div', { class: 'allday-bars' }, cardSpans(summary.cards, data.days).map(({ card, from, to }) =>
      cardChip(card, data, { gridColumn: `${from + 1} / ${to + 2}` })))) : null;
  const gutter = el('div', { class: 'gutter hours', style: { height: `${hours * HOUR_PX}px` } },
    Array.from({ length: hours }, (_, i) => el('span', { class: 'hour-label num', style: { top: `${i * HOUR_PX}px` } }, `${String(i).padStart(2, '0')}:00`)));
  const maxColumns = gridColumns();
  const columns = segments.map((segs, i) => {
    const { placed, overflow } = layoutDay(segs, maxColumns);
    const top = ms => (minutesOfDay(ms, data.zone) / 60) * HOUR_PX;
    const bottom = (s, e) => (e - s >= 24 * 3_600_000 || e >= data.days[i].dayEnd ? hours * HOUR_PX : top(e));
    const pos = (s, e, column, cols) => {
      const t = top(s);
      return { top: `${t}px`, height: `${Math.max(bottom(s, e) - t, 20)}px`, left: `calc(${(column / cols) * 100}% + 2px)`, width: `calc(${100 / cols}% - 4px)` };
    };
    return el('div', { class: 'day-col', style: { height: `${hours * HOUR_PX}px` }, role: 'group', 'aria-label': f.dayLong.format(data.days[i].dayStart) },
      placed.map(seg => {
        const height = bottom(seg.startMs, seg.endMs) - top(seg.startMs);
        return blockButton(seg.block, f, colors, { class: height < 64 ? 'blk short' : 'blk', style: pos(seg.startMs, seg.endMs, seg.column, seg.columns) },
          { roomy: height >= 40 });
      }),
      overflow.map(group => el('button', {
        type: 'button', class: 'more', style: pos(group.startMs, group.endMs, group.column, group.columns),
        'aria-label': `${group.segments.length} more lanes from ${f.time.format(group.startMs)}`,
        onclick: event => openList(event.currentTarget, group.segments.map(s => s.block), f, colors),
      }, `+${group.segments.length}`)));
  });
  // A refresh of the same week keeps the reader's scroll position; a new week opens at its first lane.
  const prev = grid.querySelector('.grid-body');
  const keep = prev && grid.dataset.week === String(data.weekStart) ? prev.scrollTop : null;
  const body = el('div', { class: 'grid-body' }, gutter, columns);
  grid.replaceChildren(head, allDay, body);
  grid.dataset.week = String(data.weekStart);
  grid.dataset.columns = String(maxColumns);
  body.scrollTop = keep ?? Math.max(0, firstHour(segments, data.zone) * HOUR_PX - HOUR_PX / 2);
}

/** Side-by-side lanes per day: each needs about 60px to stay readable, so wide screens show more. */
function gridColumns() {
  const dayWidth = ($('grid').clientWidth - 56) / 7;
  return Math.max(2, Math.min(4, Math.floor(dayWidth / 60)));
}

/** A lane as a button. `returnTo` receives focus after its detail closes (default: the button). */
function blockButton(block, f, colors, attrs, { roomy = false, withTime = false, returnTo = null } = {}) {
  const nc = block.noCommit;
  return el('button', {
    type: 'button', ...attrs, class: `${attrs.class}${nc ? ' nc' : ''}${block.live ? ' live' : ''}`,
    '--c': colors.get(block.project), 'aria-label': blockLabel(block, f),
    onclick: event => openBlock(returnTo || event.currentTarget, block, f),
  },
  withTime ? el('span', { class: 'row-time num' }, timeRange(f, block.startMs, block.endMs)) : null,
  el('span', { class: 'blk-title' }, blockTitle(block)),
  roomy ? el('span', { class: 'blk-meta num' }, `${block.project} · ${formatDuration(block.endMs - block.startMs)}`) : null,
  nc ? el('span', { class: 'blk-nc' }, 'No commit') : null);
}

function cardChip(card, data, style) {
  const board = data.boards.find(b => b.id === card.boardId);
  return el('button', {
    type: 'button', class: `card-chip${card.derived ? ' derived' : ''}`, '--c': board?.color || '#b7c4dd', style,
    'aria-label': `${card.title}, ${board?.title || card.boardId} card${card.derived ? ', placed by activity' : ''}`,
    onclick: event => openCard(event.currentTarget, card, data),
  }, el('span', { class: 'card-title' }, card.title));
}

function emptyMessage(data) {
  const label = selectionLabel(state.board, data.boards || []);
  return el('div', { class: 'empty' },
    el('p', { class: 'empty-title' }, state.board === ALL ? 'No lanes in this week.' : `No lanes for ${label} in this week.`),
    state.board === ALL
      ? el('p', { class: 'muted' }, 'Lanes appear here once the PC pushes a week summary.')
      : el('button', { type: 'button', class: 'btn', onclick: () => setBoard(ALL) }, 'Show all boards'));
}

function renderAgenda(data, summary, colors, f) {
  const agenda = $('agenda');
  if (!summary.blocks.length && !summary.cards.length) {
    agenda.replaceChildren(emptyMessage(data));
    return;
  }
  const segments = daySegments(summary.blocks, data.days);
  agenda.replaceChildren(...data.days.map((day, i) => {
    const segs = [...segments[i]].sort((a, b) => a.startMs - b.startMs);
    const cards = cardsOnDay(summary.cards, day);
    return el('section', { class: 'agenda-day', 'aria-labelledby': `agenda-${i}` },
      el('h2', { class: 'agenda-head', id: `agenda-${i}` },
        el('span', {}, f.dayLong.format(day.dayStart)),
        el('span', { class: 'num muted' }, summary.days[i].ms ? formatDuration(summary.days[i].ms) : 'No lanes')),
      segs.length || cards.length ? el('ul', { class: 'agenda-list' },
        cards.map(card => el('li', {}, cardChip(card, data))),
        segs.map(seg => el('li', {}, blockButton(seg.block, f, colors, { class: 'row' }, { roomy: true, withTime: true })))) : null);
  }));
}

function renderSidebar(data, summary, colors, f) {
  const projectCount = summary.projects.length;
  $('total').textContent = formatDuration(summary.totalMs);
  $('total-sub').textContent = projectCount ? `across ${plural(projectCount, 'project')}` : 'no lanes';
  const sum = summary.projects.reduce((s, p) => s + p.ms, 0) || 1;
  $('segments').replaceChildren(...summary.projects.map(p => el('span', { class: 'seg', '--c': colors.get(p.project), style: { flexGrow: String(p.ms / sum) } })));
  $('projects').replaceChildren(...summary.projects.map(p => el('li', {},
    el('span', { class: 'dot', '--c': colors.get(p.project), 'aria-hidden': 'true' }),
    el('span', { class: 'legend-name' }, p.project),
    el('span', { class: 'num' }, formatDuration(p.ms)))));
  $('parallel').textContent = summary.blocks.length ? `Parallel lanes counted once · ${formatDuration(summary.sessionMs)} of lane time` : '';

  $('report').replaceChildren(...summary.report.map(line => {
    const match = /^([A-Z][\w ]*?):\s*(.*)$/.exec(line);
    return el('li', {}, match ? [el('strong', {}, `${match[1]}: `), match[2]] : line);
  }));

  $('nocommit').replaceChildren(...(summary.noCommit.length
    ? summary.noCommit.map(block => el('li', {}, el('button', {
      type: 'button', class: 'nc-item', 'aria-label': blockLabel(block, f), onclick: event => openBlock(event.currentTarget, block, f),
    },
    el('span', { class: 'swatch-nc', '--c': colors.get(block.project), 'aria-hidden': 'true' }),
    el('span', { class: 'nc-text' }, el('span', { class: 'nc-title' }, blockTitle(block)),
      el('span', { class: 'muted num' }, `${block.project} · ${f.weekday.format(block.startMs)} ${f.time.format(block.startMs)} · ${formatDuration(block.endMs - block.startMs)}`)))))
    : [el('li', { class: 'muted' }, 'Every lane ended with a commit.')]));

  const max = Math.max(...summary.days.map(d => d.ms), 1);
  $('perday').replaceChildren(...summary.days.map(day => el('li', {},
    el('span', { class: 'perday-name' }, f.weekday.format(day.dayStart)),
    el('span', { class: 'bar', 'aria-hidden': 'true' }, el('span', { style: { width: `${(day.ms / max) * 100}%` } })),
    el('span', { class: 'num' }, formatDuration(day.ms)))));
}

// ---- detail popover / bottom sheet ----
function openDialog(trigger, title, body) {
  const dialog = $('detail');
  if (dialog.open) dialog.close();
  state.trigger = trigger;
  $('detail-title').textContent = title;
  $('detail-body').replaceChildren(...body);
  dialog.style.removeProperty('left');
  dialog.style.removeProperty('top');
  dialog.showModal();
  if (window.matchMedia('(min-width: 900px)').matches && trigger) placeNear(dialog, trigger);
  dialog.scrollTop = 0;
  $('detail-close').focus();
}

function placeNear(dialog, trigger) {
  const r = trigger.getBoundingClientRect();
  const w = dialog.offsetWidth;
  const h = dialog.offsetHeight;
  const gap = 8;
  let left = r.right + gap;
  if (left + w > innerWidth - gap) left = r.left - gap - w;
  left = Math.min(Math.max(gap, left), innerWidth - w - gap);
  const top = Math.min(Math.max(gap, r.top), innerHeight - h - gap);
  dialog.style.left = `${left}px`;
  dialog.style.top = `${Math.max(gap, top)}px`;
}

function row(term, value) {
  return value == null || value === '' ? null : el('div', { class: 'meta-row' }, el('dt', {}, term), el('dd', {}, value));
}

function openBlock(trigger, block, f) {
  const data = state.data;
  const boards = blockBoards(block, data).map(id => data.boards.find(b => b.id === id)?.title || id);
  const commits = block.commits == null ? 'Unknown (git was not readable)' : block.noCommit ? 'No commit' : plural(commitCount(block), 'commit');
  openDialog(trigger, blockTitle(block), [
    block.noCommit ? el('p', { class: 'badge-nc' }, el('span', { class: 'swatch-nc', 'aria-hidden': 'true' }), 'Stopped with no commit') : null,
    el('dl', { class: 'meta' },
      row('When', `${f.dayLong.format(block.startMs)} · ${timeRange(f, block.startMs, block.endMs)} · ${formatDuration(block.endMs - block.startMs)}${block.live ? ' · running' : ''}`),
      row('Project', block.project),
      row('Board', boards.length ? boards.join(', ') : 'Unassigned'),
      row('Tool', [block.tool, block.model].filter(Boolean).join(' · ')),
      row('Commits', `${commits}${block.filesChanged ? ` · ${plural(block.filesChanged, 'file')} changed` : ''}`)),
    block.firstMessage ? el('div', { class: 'first' }, el('h3', {}, 'First message'), el('p', {}, block.firstMessage)) : null,
  ].filter(Boolean));
}

const SOURCE_TEXT = {
  target: 'Placed by its Target date.',
  start: 'Placed by its Start date.',
  iteration: 'Placed by its Iteration.',
  activity: 'Placed by its last activity (update, close or merge). This board has no date field, so the day is derived, not planned.',
};
const TYPE_TEXT = { issue: 'Issue', pr: 'Pull request', draft: 'Draft' };
const STATE_TEXT = { OPEN: 'open', CLOSED: 'closed', MERGED: 'merged' };

function openCard(trigger, card, data) {
  const board = data.boards.find(b => b.id === card.boardId);
  const f = formatters(data.zone);
  const ref = card.repo && card.number ? `${card.repo}#${card.number}` : TYPE_TEXT[card.type] || 'Card';
  openDialog(trigger, card.title, [
    el('dl', { class: 'meta' },
      row('Board', board ? `${board.title} · ${kindLabel(board.kind)}` : card.boardId),
      row('Item', `${ref} · ${STATE_TEXT[card.state] || card.state}`),
      row('Date', card.endAt && card.endAt > card.at ? `${f.dayLong.format(card.at)} – ${f.dayLong.format(card.endAt - 1)}` : f.dayLong.format(card.at))),
    el('p', { class: 'muted' }, SOURCE_TEXT[card.source] || (card.derived ? SOURCE_TEXT.activity : '')),
    card.url && /^https:\/\/github\.com\//.test(card.url) ? el('a', { class: 'btn link-btn', href: card.url, target: '_blank', rel: 'noopener noreferrer' }, 'Open on GitHub') : null,
  ].filter(Boolean));
}

function openList(trigger, blocks, f, colors) {
  openDialog(trigger, plural(blocks.length, 'more lane'), [
    el('ul', { class: 'agenda-list' }, blocks.map(block => el('li', {},
      blockButton(block, f, colors, { class: 'row' }, { roomy: true, withTime: true, returnTo: trigger })))),
  ]);
}

const dialog = $('detail');
$('detail-close').addEventListener('click', () => dialog.close());
dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
dialog.addEventListener('close', () => {
  const trigger = state.trigger;
  state.trigger = null;
  if (trigger?.isConnected) trigger.focus({ preventScroll: false });
});

// Swipe down on the sheet header closes it on touch screens.
let swipe = null;
dialog.addEventListener('pointerdown', event => {
  if (event.pointerType === 'mouse' || !event.target.closest('.sheet-handle, .detail-head')) return;
  swipe = { y: event.clientY, id: event.pointerId };
});
dialog.addEventListener('pointermove', event => {
  if (!swipe || event.pointerId !== swipe.id) return;
  const dy = Math.max(0, event.clientY - swipe.y);
  dialog.style.translate = `0 ${dy}px`;
});
const endSwipe = event => {
  if (!swipe || event.pointerId !== swipe.id) return;
  const dy = event.clientY - swipe.y;
  swipe = null;
  dialog.style.translate = '';
  if (dy > 80) dialog.close();
};
dialog.addEventListener('pointerup', endSwipe);
dialog.addEventListener('pointercancel', endSwipe);

// ---- controls ----
function setBoard(value) {
  state.board = value;
  try { localStorage.setItem(BOARD_STORAGE_KEY, value); } catch { /* storage blocked: URL still carries it */ }
  writeUrl();
  render();
}

function setWeek(iso) {
  state.week = iso;
  writeUrl();
  load();
}

$('board').addEventListener('change', event => setBoard(event.target.value));
$('prev').addEventListener('click', () => setWeek(shiftWeek(state.week, -1)));
$('next').addEventListener('click', () => setWeek(shiftWeek(state.week, 1)));
$('today').addEventListener('click', () => setWeek(mondayOf(new Date())));
// A resize that changes how many lanes fit side by side re-lays the grid.
window.addEventListener('resize', () => {
  if (state.data && $('grid').dataset.columns && $('grid').dataset.columns !== String(gridColumns())) render();
});

load();
setInterval(() => { if (!document.hidden && !dialog.open) load({ quiet: true }); }, REFRESH_MS);
