/**
 * PolySimulator Design Gallery — Pin-Comment Server
 * Zero runtime dependencies — built-in modules only.
 */

'use strict';

const http     = require('http');
const fs       = require('fs');
const path     = require('path');
const { URL }  = require('url');
const crypto   = require('crypto');

// ─── Config ──────────────────────────────────────────────────────────────────
const PORT        = parseInt(process.env.PORT || '8080', 10);
const GALLERY_DIR = path.resolve(process.env.GALLERY_DIR || './public');
const DB_PATH     = path.resolve(process.env.DB_PATH     || '/data/comments.json');
// OWNER_KEY: if non-empty, requests that supply this key are stamped role:"owner".
// Never stored in data file, never echoed back in responses.
const OWNER_KEY   = process.env.OWNER_KEY || '';

// ─── In-memory store + persistence ───────────────────────────────────────────
// threads: Map<id, thread>
// thread: { id, page, xPct, yPct, resolved, createdAt, author, comments: [{author,text,createdAt,role}] }
// role is always derived server-side from OWNER_KEY — never trusted from client.

let threads = {};   // keyed by thread id

function loadDb() {
  try {
    const raw = fs.readFileSync(DB_PATH, 'utf8');
    const data = JSON.parse(raw);
    threads = data.threads || {};
  } catch (e) {
    if (e.code !== 'ENOENT') console.error('[db] load error:', e.message);
    threads = {};
  }
}

// Serialised write queue to avoid concurrent corruptions.
let writeQueue = Promise.resolve();

function saveDb() {
  writeQueue = writeQueue.then(() => new Promise((resolve) => {
    const tmp = DB_PATH + '.tmp';
    const json = JSON.stringify({ threads }, null, 2);
    // Ensure parent dir exists
    try { fs.mkdirSync(path.dirname(DB_PATH), { recursive: true }); } catch (_) {}
    fs.writeFile(tmp, json, 'utf8', (err) => {
      if (err) { console.error('[db] write tmp error:', err.message); return resolve(); }
      fs.rename(tmp, DB_PATH, (err2) => {
        if (err2) console.error('[db] rename error:', err2.message);
        resolve();
      });
    });
  }));
}

loadDb();

// ─── Overlay assets (read once at startup) ───────────────────────────────────
const OVERLAY_JS  = fs.readFileSync(path.join(__dirname, 'overlay.js'),  'utf8');
const OVERLAY_CSS = fs.readFileSync(path.join(__dirname, 'overlay.css'), 'utf8');
// Cache-busting version from the overlay contents — changes whenever the overlay changes,
// so Cloudflare/browsers fetch the new file instead of a stale cached copy.
const OVERLAY_VER = crypto.createHash('sha1').update(OVERLAY_JS + OVERLAY_CSS).digest('hex').slice(0, 10);

const INJECT_BEFORE = '</body>';
const INJECT_SNIPPET = `\n<link rel="stylesheet" href="/__c/overlay.css?v=${OVERLAY_VER}">\n<script src="/__c/overlay.js?v=${OVERLAY_VER}" defer></script>\n`;

// ─── MIME types ──────────────────────────────────────────────────────────────
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.htm':  'text/html; charset=utf-8',
  '.css':  'text/css; charset=utf-8',
  '.js':   'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png':  'image/png',
  '.jpg':  'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif':  'image/gif',
  '.svg':  'image/svg+xml',
  '.ico':  'image/x-icon',
  '.woff': 'font/woff',
  '.woff2':'font/woff2',
};

function mimeFor(filePath) {
  return MIME[path.extname(filePath).toLowerCase()] || 'application/octet-stream';
}

// ─── Helpers ─────────────────────────────────────────────────────────────────
function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on('data', c => chunks.push(c));
    req.on('end', () => {
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
      catch (e) { reject(e); }
    });
    req.on('error', reject);
  });
}

function send(res, status, data) {
  const body = JSON.stringify(data);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

function err(res, status, msg) {
  send(res, status, { error: msg });
}

function sanitise(v, max) {
  if (typeof v !== 'string') return null;
  return v.slice(0, max).trim();
}

// Resolve role from the request and parsed body.
// Checks X-Owner-Key header and body.ownerKey (either accepted).
// Returns 'owner' only when OWNER_KEY is non-empty and the supplied key matches.
// The client-sent 'role' field is NEVER consulted.
function resolveRole(req, body) {
  if (!OWNER_KEY) return 'guest';
  const fromHeader = req.headers['x-owner-key'] || '';
  const fromBody   = (body && typeof body.ownerKey === 'string') ? body.ownerKey : '';
  const supplied   = fromHeader || fromBody;
  return supplied === OWNER_KEY ? 'owner' : 'guest';
}

// ─── API handlers ─────────────────────────────────────────────────────────────

function handleApi(req, res, urlObj) {
  const subPath = urlObj.pathname.replace(/^\/__c\/api/, '') || '/';
  const method  = req.method.toUpperCase();

  // GET /__c/api/threads?page=<pathname>
  if (method === 'GET' && subPath === '/threads') {
    const page = urlObj.searchParams.get('page') || '/';
    const result = Object.values(threads).filter(t => t.page === page);
    return send(res, 200, { threads: result });
  }

  // POST /__c/api/threads — create thread
  if (method === 'POST' && subPath === '/threads') {
    return readBody(req).then(body => {
      const page   = sanitise(body.page,   200);
      const author = sanitise(body.author,  80);
      const text   = sanitise(body.text,  4000);
      const xPct   = typeof body.xPct === 'number' ? body.xPct : null;
      const yPct   = typeof body.yPct === 'number' ? body.yPct : null;

      if (!page || !author || !text || xPct === null || yPct === null) {
        return err(res, 400, 'Missing required fields: page, xPct, yPct, author, text');
      }
      const role = resolveRole(req, body);
      const id = crypto.randomUUID();
      const now = new Date().toISOString();
      const thread = {
        id, page, xPct, yPct,
        resolved: false,
        createdAt: now,
        author,
        comments: [{ author, text, createdAt: now, role }],
      };
      threads[id] = thread;
      saveDb();
      send(res, 201, thread);
    }).catch(() => err(res, 400, 'Invalid JSON body'));
  }

  // POST /__c/api/threads/:id/reply
  const replyMatch = subPath.match(/^\/threads\/([^/]+)\/reply$/);
  if (method === 'POST' && replyMatch) {
    const id = replyMatch[1];
    if (!threads[id]) return err(res, 404, 'Thread not found');
    return readBody(req).then(body => {
      const author = sanitise(body.author, 80);
      const text   = sanitise(body.text, 4000);
      if (!author || !text) return err(res, 400, 'Missing author or text');
      const role    = resolveRole(req, body);
      const comment = { author, text, createdAt: new Date().toISOString(), role };
      threads[id].comments.push(comment);
      saveDb();
      send(res, 200, threads[id]);
    }).catch(() => err(res, 400, 'Invalid JSON body'));
  }

  // POST /__c/api/threads/:id/resolve
  const resolveMatch = subPath.match(/^\/threads\/([^/]+)\/resolve$/);
  if (method === 'POST' && resolveMatch) {
    const id = resolveMatch[1];
    if (!threads[id]) return err(res, 404, 'Thread not found');
    return readBody(req).then(body => {
      threads[id].resolved = !!body.resolved;
      saveDb();
      send(res, 200, threads[id]);
    }).catch(() => err(res, 400, 'Invalid JSON body'));
  }

  // DELETE /__c/api/threads/:id
  const deleteMatch = subPath.match(/^\/threads\/([^/]+)$/);
  if (method === 'DELETE' && deleteMatch) {
    const id = deleteMatch[1];
    if (!threads[id]) return err(res, 404, 'Thread not found');
    delete threads[id];
    saveDb();
    return send(res, 200, { deleted: id });
  }

  err(res, 404, 'Unknown API endpoint');
}

// ─── Static file handler (with HTML injection) ────────────────────────────────
function handleStatic(req, res, urlObj) {
  let relPath = urlObj.pathname;

  // Security: prevent path traversal
  const safe = path.normalize(relPath).replace(/^(\.\.[/\\])+/, '');
  let filePath = path.join(GALLERY_DIR, safe);

  // Directory → index.html
  try {
    const stat = fs.statSync(filePath);
    if (stat.isDirectory()) {
      filePath = path.join(filePath, 'index.html');
    }
  } catch (_) {
    // will 404 below
  }

  fs.readFile(filePath, (err2, data) => {
    if (err2) {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      return res.end('Not found');
    }

    const mime = mimeFor(filePath);
    if (mime.startsWith('text/html')) {
      let html = data.toString('utf8');
      const closeIdx = html.lastIndexOf(INJECT_BEFORE);
      if (closeIdx !== -1) {
        html = html.slice(0, closeIdx) + INJECT_SNIPPET + html.slice(closeIdx);
      } else {
        html += INJECT_SNIPPET;
      }
      const buf = Buffer.from(html, 'utf8');
      res.writeHead(200, { 'Content-Type': mime, 'Content-Length': buf.length, 'Cache-Control': 'no-cache' });
      return res.end(buf);
    }

    res.writeHead(200, { 'Content-Type': mime, 'Content-Length': data.length });
    res.end(data);
  });
}

// ─── Main request router ──────────────────────────────────────────────────────
const server = http.createServer((req, res) => {
  // CORS for local dev
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET,POST,DELETE,OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type,X-Owner-Key');
  if (req.method === 'OPTIONS') { res.writeHead(204); return res.end(); }

  let urlObj;
  try {
    urlObj = new URL(req.url, `http://localhost:${PORT}`);
  } catch (_) {
    res.writeHead(400); return res.end('Bad request');
  }

  const p = urlObj.pathname;

  // Overlay assets
  if (p === '/__c/overlay.js') {
    res.writeHead(200, { 'Content-Type': 'application/javascript; charset=utf-8', 'Cache-Control': 'no-store' });
    return res.end(OVERLAY_JS);
  }
  if (p === '/__c/overlay.css') {
    res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8', 'Cache-Control': 'no-store' });
    return res.end(OVERLAY_CSS);
  }

  // API
  if (p.startsWith('/__c/api')) {
    return handleApi(req, res, urlObj);
  }

  // Static gallery
  handleStatic(req, res, urlObj);
});

server.listen(PORT, () => {
  console.log(`[gallery] listening on http://localhost:${PORT}`);
  console.log(`[gallery] GALLERY_DIR = ${GALLERY_DIR}`);
  console.log(`[gallery] DB_PATH     = ${DB_PATH}`);
});
