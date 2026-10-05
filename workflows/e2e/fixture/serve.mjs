// Fixture site for the e2e runs. Serves ./site on 127.0.0.1 and /api/version (served sha, for the receipt).
// A second listener on 127.0.0.2 plays "a host outside the allow-list": it counts every request it receives,
// and /api/hits on 127.0.0.1 reports the count, so a test can prove a refused request never left the browser.
// SERVED_SHA = the 40-hex sha the lane built from. PORT defaults to 4173.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

const root = new URL('./site/', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const types = { '.html': 'text/html', '.txt': 'text/plain' };
let outsideHits = 0;
const handler = (countHits) => (req, res) => {
  if (countHits) outsideHits += 1;
  const url = new URL(req.url ?? '/', 'http://localhost');
  if (url.pathname === '/api/version') {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ sha: process.env.SERVED_SHA ?? '' }));
    return;
  }
  if (url.pathname === '/api/hits') {
    res.writeHead(200, { 'content-type': 'text/plain', 'cache-control': 'no-store' });
    res.end(String(outsideHits));
    return;
  }
  const file = path.join(root, url.pathname === '/' ? 'index.html' : path.basename(url.pathname));
  if (!fs.existsSync(file)) {
    res.writeHead(404).end('not found');
    return;
  }
  res.writeHead(200, { 'content-type': types[path.extname(file)] ?? 'application/octet-stream' });
  res.end(fs.readFileSync(file));
};
const port = Number(process.env.PORT ?? 4173);
http.createServer(handler(false)).listen(port, '127.0.0.1', () => console.log('listening'));
http.createServer(handler(true)).listen(port, '127.0.0.2');
