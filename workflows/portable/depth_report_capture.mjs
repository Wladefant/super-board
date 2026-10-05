/**
 * Depth report capture: full-page screenshots of a served depth-survey report, with checks.
 *
 *   python workflows/portable/depth_report_preview.py --repo-root <repo> --port 4791
 *   node workflows/portable/depth_report_capture.mjs --base-url http://127.0.0.1:4791 \
 *     --expected-sha <40-hex> --output <dir>
 *
 * It reads the served SHA from GET /api/version and refuses to capture when that SHA differs from
 * --expected-sha or the served tree is dirty (evidence provenance rule,
 * https://github.com/Wladefant/super-board/issues/421). It aborts every request outside the served
 * origin, so a pass also proves the report needs no network.
 * For 1440x900 and 390x844, light and dark, it saves one full-page PNG and checks:
 *   no_horizontal_overflow  the document is no wider than the viewport
 *   text_contrast           every text run reaches WCAG AA (4.5:1, or 3:1 for large text)
 *   svg_labels_fit          every diagram label stays inside the box it labels
 *   offline                 the page made no request outside the served origin
 * It writes manifest.json (served SHA, files with sha256, checks) and exits 1 when a check fails.
 * Tracking: https://github.com/Wladefant/super-board/issues/517
 */

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  VIEWPORTS,
  THEMES,
  assertNotProduction,
  checkNoHorizontalOverflow,
  checkVersionEndpoint,
  resolveExecutablePath,
  resolvePuppeteer
} from './flow_qa_runner.mjs';

const CAPTURE_VIEWPORTS = ['1440x900', '390x844'];

function parseArgs(argv) {
  const opts = { baseUrl: 'http://127.0.0.1:4791', expectedSha: '', outputDir: './depth-report-capture' };
  for (let i = 2; i < argv.length; i++) {
    if (argv[i] === '--base-url') opts.baseUrl = argv[++i];
    else if (argv[i] === '--expected-sha') opts.expectedSha = argv[++i];
    else if (argv[i] === '--output') opts.outputDir = argv[++i];
  }
  return opts;
}

/** Runs in the page: contrast of every text run and fit of every diagram label. */
function inspectPage() {
  const parse = (c) => {
    const m = /rgba?\(([^)]+)\)/.exec(c || '');
    if (!m) return null;
    const [r, g, b, a = 1] = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return { r, g, b, a };
  };
  const lum = ({ r, g, b }) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
  const backgroundOf = (el) => {
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0) return c;
    }
    return parse(getComputedStyle(document.body).backgroundColor) || { r: 255, g: 255, b: 255, a: 1 };
  };
  const contrast = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    const el = t.parentElement;
    if (!t.textContent.trim() || seen.has(el)) continue;
    seen.add(el);
    const style = getComputedStyle(el);
    let fg; let bg;
    if (el instanceof SVGTextElement) {
      fg = parse(style.fill);
      const box = el.previousElementSibling;
      bg = box instanceof SVGRectElement ? parse(getComputedStyle(box).fill) : backgroundOf(el.ownerSVGElement);
    } else {
      fg = parse(style.color);
      bg = backgroundOf(el);
    }
    const size = parseFloat(style.fontSize);
    const large = size >= 24 || (size >= 18.66 && Number(style.fontWeight) >= 700);
    const value = fg && bg ? ratio(fg, bg) : 0;
    contrast.push({ text: t.textContent.trim().slice(0, 40), ratio: Math.round(value * 100) / 100, min: large ? 3 : 4.5 });
  }
  const labels = [];
  for (const text of document.querySelectorAll('svg text')) {
    const box = text.previousElementSibling;
    if (!(box instanceof SVGRectElement)) continue;
    const t = text.getBBox();
    const r = box.getBBox();
    labels.push({ text: text.textContent, fits: t.x >= r.x && t.y >= r.y && t.x + t.width <= r.x + r.width && t.y + t.height <= r.y + r.height });
  }
  return { contrast, labels, scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth };
}

export async function capture({ baseUrl, expectedSha, outputDir }) {
  assertNotProduction(baseUrl);
  if (!/^[0-9a-f]{40}$/i.test(expectedSha || '')) throw new Error('--expected-sha must be a 40-hex commit');
  const version = await checkVersionEndpoint(baseUrl, expectedSha);
  if (!version.passed) throw new Error(`Served SHA check failed: ${version.detail}`);
  const served = await (await fetch(`${baseUrl.replace(/\/+$/, '')}/api/version`)).json();
  if (served.dirty) throw new Error(`The served tree at ${served.sha} has local changes; commit them first`);
  fs.mkdirSync(outputDir, { recursive: true });

  const origin = new URL(baseUrl).origin;
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({
    executablePath: resolveExecutablePath(),
    headless: 'new',
    args: ['--no-sandbox', '--disable-gpu', '--log-level=3']
  });
  const shots = [];
  try {
    for (const vpKey of CAPTURE_VIEWPORTS) {
      for (const theme of THEMES) {
        const page = await browser.newPage();
        const blocked = [];
        await page.setRequestInterception(true);
        page.on('request', (req) => {
          const url = req.url();
          if (url.startsWith('data:') || url.startsWith('about:') || new URL(url).origin === origin) req.continue();
          else { blocked.push(url); req.abort('internetdisconnected'); }
        });
        const vp = VIEWPORTS[vpKey];
        await page.setViewport({ width: vp.width, height: vp.height, isMobile: vp.isMobile, hasTouch: vp.hasTouch, deviceScaleFactor: vp.deviceScaleFactor });
        await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: theme }]);
        await page.goto(`${origin}/`, { waitUntil: 'networkidle0', timeout: 30000 });
        const seen = await page.evaluate(inspectPage);
        const file = `depth-report-${vpKey}-${theme}-${served.sha.slice(0, 12)}.png`;
        await page.screenshot({ path: path.join(outputDir, file), fullPage: true });
        await page.close();
        const lowContrast = seen.contrast.filter((c) => c.ratio < c.min);
        const unfit = seen.labels.filter((l) => !l.fits);
        const checks = [
          checkNoHorizontalOverflow(seen.scrollWidth, seen.innerWidth),
          { name: 'text_contrast', passed: lowContrast.length === 0, detail: lowContrast.length ? `below AA: ${JSON.stringify(lowContrast)}` : `${seen.contrast.length} text runs, lowest ${Math.min(...seen.contrast.map((c) => c.ratio))}:1` },
          { name: 'svg_labels_fit', passed: unfit.length === 0, detail: unfit.length ? `outside their box: ${unfit.map((l) => l.text).join(', ')}` : `${seen.labels.length} labels inside their boxes` },
          { name: 'offline', passed: blocked.length === 0, detail: blocked.length ? `requests outside ${origin}: ${blocked.join(', ')}` : `no request outside ${origin}` }
        ];
        const sha256 = crypto.createHash('sha256').update(fs.readFileSync(path.join(outputDir, file))).digest('hex');
        shots.push({ viewport: vpKey, theme, file, sha256, checks });
      }
    }
  } finally {
    await browser.close().catch(() => {});
  }
  const failed = shots.flatMap((s) => s.checks.filter((c) => !c.passed).map((c) => `${s.viewport} ${s.theme} ${c.name}: ${c.detail}`));
  const hashes = new Set(shots.map((s) => s.sha256));
  if (hashes.size !== shots.length) failed.push('two captures are byte-identical');
  const manifest = { served_sha: served.sha, surveyed_sha: served.surveyed_sha, template: served.template, passed: failed.length === 0, failed, shots };
  fs.writeFileSync(path.join(outputDir, 'manifest.json'), JSON.stringify(manifest, null, 2), 'utf8');
  return manifest;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  capture(parseArgs(process.argv))
    .then((m) => {
      console.log(`DEPTH-REPORT-CAPTURE: ${m.passed ? 'PASS' : 'FAIL'} ${m.served_sha}`);
      for (const s of m.shots) console.log(`${s.viewport} ${s.theme} ${s.file} ${s.checks.map((c) => `${c.name}=${c.passed ? 'pass' : 'FAIL'}`).join(' ')}`);
      for (const f of m.failed) console.log(`FAIL ${f}`);
      process.exit(m.passed ? 0 : 1);
    })
    .catch((err) => { console.error(`depth report capture error: ${err.message}`); process.exit(2); });
}
