#!/usr/bin/env node
/**
 * Superboard: sweep every comment on a repo's issues and pull requests, newest first.
 *
 * This is the **inbound channel** of the board. Comments are how a human talks to the
 * system between sessions — leave them on any card whenever you like, then say "check
 * the comments" and get all of them in one pass, in date order, with nothing missed.
 * A watermark remembers the last sweep, so repeat runs show only what is new.
 *
 *   super-board-sweep-comments.mjs                 # new since last sweep, then move the watermark
 *   super-board-sweep-comments.mjs --peek          # same, but leave the watermark alone
 *   super-board-sweep-comments.mjs --all           # everything, ever
 *   super-board-sweep-comments.mjs --since 2026-07-20
 *   super-board-sweep-comments.mjs --author <login>
 *   super-board-sweep-comments.mjs --mine          # exclude the repo owner (what OTHERS said)
 *   super-board-sweep-comments.mjs --full          # don't truncate bodies
 *   super-board-sweep-comments.mjs --repo owner/name
 *
 * The repo defaults to whatever `gh` resolves in the current directory, so the same
 * script serves every project board. Auth comes from the `gh` CLI — no token lives in
 * this file or in the environment. The watermark is stored per project, under the
 * project's own .claude/super-board/, so boards never share a cursor.
 */

import { execFileSync } from 'node:child_process';
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';

function detectRepo() {
  try {
    return execFileSync('gh', ['repo', 'view', '--json', 'nameWithOwner', '-q', '.nameWithOwner'], {
      encoding: 'utf8',
    }).trim();
  } catch {
    console.error('Cannot determine the repo. Run inside a git repo with `gh`, or pass --repo owner/name.');
    process.exit(1);
  }
}

const repoArg = (() => { const i = process.argv.indexOf('--repo'); return i === -1 ? null : process.argv[i + 1]; })();
const REPO = repoArg ?? process.env.SWEEP_REPO ?? detectRepo();
const OWNER = REPO.split('/')[0];

// Per-project watermark: two boards must never share a cursor.
const STATE_DIR = join(process.cwd(), '.claude', 'super-board');
const STATE = join(STATE_DIR, 'comment-sweep.json');

const argv = process.argv.slice(2);
const has = (f) => argv.includes(f);
const val = (f) => { const i = argv.indexOf(f); return i === -1 ? null : argv[i + 1]; };

const BODY_LIMIT = has('--full') ? Infinity : 1200;

function gh(path) {
  const out = execFileSync('gh', ['api', path, '--paginate'], {
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
  });
  // --paginate concatenates JSON arrays; stitch them back into one array.
  return JSON.parse('[' + out.trim().replace(/\]\s*\[/g, ',').replace(/^\[|\]$/g, '') + ']');
}

function loadWatermark() {
  if (!existsSync(STATE)) return null;
  try { return JSON.parse(readFileSync(STATE, 'utf8')).lastSweep ?? null; } catch { return null; }
}

// ---------------------------------------------------------------- what to fetch
let since = null;
let mode;
if (has('--all')) { mode = 'all'; }
else if (val('--since')) { since = new Date(val('--since')).toISOString(); mode = 'since'; }
else { since = loadWatermark(); mode = since ? 'new' : 'all'; }

const q = since ? `?since=${encodeURIComponent(since)}&per_page=100` : '?per_page=100';

// Issue comments cover both issues and PR conversations; review comments are a separate
// endpoint and are easy to miss, so both are swept.
const comments = [
  ...gh(`repos/${REPO}/issues/comments${q}`).map((c) => ({ ...c, kind: 'comment' })),
  ...gh(`repos/${REPO}/pulls/comments${q}`).map((c) => ({ ...c, kind: 'review' })),
];

// ---------------------------------------------------------------- filter + sort
let rows = comments.map((c) => ({
  at: c.created_at,
  edited: c.updated_at !== c.created_at,
  who: c.user?.login ?? '?',
  num: Number((c.issue_url ?? c.pull_request_url ?? '').split('/').pop()),
  url: c.html_url,
  kind: c.kind,
  path: c.path ?? null,
  body: (c.body ?? '').trim(),
}));

const author = val('--author');
if (author) rows = rows.filter((r) => r.who.toLowerCase() === author.toLowerCase());
if (has('--mine')) rows = rows.filter((r) => r.who.toLowerCase() !== OWNER.toLowerCase());

rows.sort((a, b) => b.at.localeCompare(a.at));

// ---------------------------------------------------------------- titles
// One listing call rather than one call per issue — a wide sweep touches dozens of issues.
const titles = new Map();
const states = new Map();
if (rows.length) {
  for (const i of gh(`repos/${REPO}/issues?state=all&per_page=100`)) {
    titles.set(i.number, i.title);
    states.set(i.number, i.pull_request ? `PR/${i.state}` : i.state);
  }
}

// ---------------------------------------------------------------- report
const label = mode === 'new' ? `new since ${since}` : mode === 'since' ? `since ${since}` : 'all time';
console.log(`# Comment sweep — ${REPO} (${label})`);
console.log(`# ${rows.length} comment(s)${author ? ` by ${author}` : ''}${has('--mine') ? ` from people other than ${OWNER}` : ''}\n`);

if (rows.length === 0) {
  console.log('Nothing new.');
} else {
  const byAuthor = rows.reduce((m, r) => (m[r.who] = (m[r.who] ?? 0) + 1, m), {});
  console.log('By author: ' + Object.entries(byAuthor).map(([k, v]) => `${k}=${v}`).join('  ') + '\n');

  for (const r of rows) {
    const where = r.path ? ` · ${r.path}` : '';
    console.log('─'.repeat(78));
    console.log(`#${r.num} ${titles.get(r.num) ?? ''}   <${states.get(r.num) ?? '?'}>`);
    console.log(`${r.at}  ${r.who}  [${r.kind}${r.edited ? ', edited' : ''}]${where}`);
    console.log(r.url);
    console.log('');
    const body = r.body.length > BODY_LIMIT ? r.body.slice(0, BODY_LIMIT) + `\n… (${r.body.length - BODY_LIMIT} more chars, use --full)` : r.body;
    console.log(body);
    console.log('');
  }
}

// ---------------------------------------------------------------- watermark
if (!has('--peek') && !has('--all') && !val('--since')) {
  if (!existsSync(STATE_DIR)) mkdirSync(STATE_DIR, { recursive: true });
  writeFileSync(STATE, JSON.stringify({ lastSweep: new Date().toISOString(), repo: REPO }, null, 2));
  console.log(`\n(watermark moved — next run shows only newer comments; --peek to avoid this)`);
}
