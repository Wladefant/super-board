// Fixture site for the e2e runs. Serves ./site on 127.0.0.1 and /api/version (served sha, for the receipt).
// SERVED_SHA = the 40-hex sha the lane built from. PORT defaults to 4173.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';

const root = new URL('./site/', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1');
const types = { '.html': 'text/html', '.txt': 'text/plain' };
http
  .createServer((req, res) => {
    const url = new URL(req.url ?? '/', 'http://localhost');
    if (url.pathname === '/api/version') {
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ sha: process.env.SERVED_SHA ?? '' }));
      return;
    }
    const file = path.join(root, url.pathname === '/' ? 'index.html' : path.basename(url.pathname));
    if (!fs.existsSync(file)) {
      res.writeHead(404).end('not found');
      return;
    }
    res.writeHead(200, { 'content-type': types[path.extname(file)] ?? 'application/octet-stream' });
    res.end(fs.readFileSync(file));
  })
  .listen(Number(process.env.PORT ?? 4173), '127.0.0.1', () => console.log('listening'));
