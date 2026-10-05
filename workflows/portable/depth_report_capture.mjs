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
 *   text_contrast           every text run reaches WCAG AA (4.5:1, or 3:1 for large text) against
 *                           each colour under it: a solid background, or every colour a gradient
 *                           passes through. Text over an image or a colour it cannot read is listed
 *                           as "not proven" and fails the check.
 *   svg_labels_fit          every diagram label stays inside the box it labels
 *   offline                 the page made no request outside the served origin
 * It writes manifest.json (served SHA, files with sha256, checks) and exits 1 when a check fails.
 * Tracking: https://github.com/Wladefant/super-board/issues/517, https://github.com/Wladefant/super-board/issues/559
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
    const m = /^rgba?\(([^)]+)\)$/.exec((c || '').trim());
    if (!m) return null;
    const [r, g, b, a = 1] = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return { r, g, b, a };
  };
  const lum = ({ r, g, b }) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05); };
  const over = (top, under) => ({ r: top.r * top.a + under.r * (1 - top.a), g: top.g * top.a + under.g * (1 - top.a), b: top.b * top.a + under.b * (1 - top.a), a: 1 });
  // Splits a computed background-image into its layers, top layer first.
  const layersOf = (value) => {
    const out = []; let depth = 0; let start = 0;
    for (let i = 0; i < value.length; i++) {
      if (value[i] === '(') depth++;
      else if (value[i] === ')') depth--;
      else if (value[i] === ',' && depth === 0) { out.push(value.slice(start, i).trim()); start = i + 1; }
    }
    return [...out, value.slice(start).trim()];
  };
  // Every colour a gradient layer paints over `under`, sampled along each pair of stops: a mix of two
  // stops can be darker than both (red to green), so the stops alone are not the worst case.
  // Null when the layer is not an sRGB gradient of rgb() stops: an image, or a colour it cannot read.
  const SAMPLES = 32;
  const gradientColours = (layer, under) => {
    const fns = layer.match(/[a-z-]+(?=\()/g) || [];
    if (!/^(?:repeating-)?(?:linear|radial|conic)-gradient\(/.test(layer) || /\bin [a-z]/.test(layer)) return null;
    if (fns.some((f) => !/^(?:(?:repeating-)?(?:linear|radial|conic)-gradient|rgba?|calc)$/.test(f))) return null;
    const stops = (layer.match(/rgba?\([^)]*\)/g) || []).map(parse);
    if (stops.length < 2 || stops.some((s) => !s)) return null;
    const out = [];
    for (let i = 0; i + 1 < stops.length; i++) {
      const [p, q] = [stops[i], stops[i + 1]];
      for (let k = 0; k <= SAMPLES; k++) {
        const t = k / SAMPLES;
        const a = p.a + (q.a - p.a) * t;
        // CSS mixes stops premultiplied by alpha.
        const mix = (ch) => p[ch] * p.a + (q[ch] * q.a - p[ch] * p.a) * t;
        for (const u of under) out.push({ r: mix('r') + u.r * (1 - a), g: mix('g') + u.g * (1 - a), b: mix('b') + u.b * (1 - a), a: 1 });
      }
    }
    return out;
  };
  // Every opaque colour that can sit under el: its ancestors' fills and gradients, painted from the
  // nearest opaque fill (or the white canvas) up to el. { reason } when one of them cannot be measured.
  const backgroundsOf = (el) => {
    const chain = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      chain.push(n);
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a === 1) break;
    }
    let under = [{ r: 255, g: 255, b: 255, a: 1 }];
    for (const n of chain.reverse()) {
      const s = getComputedStyle(n);
      const fill = parse(s.backgroundColor);
      if (!fill) return { reason: 'colour' };
      if (fill.a > 0) under = under.map((u) => over(fill, u));
      if (s.backgroundImage === 'none') continue;
      // A sized layer may leave the colours under it showing.
      const partial = s.backgroundSize.split(',').some((v) => v.trim() !== 'auto');
      for (const layer of layersOf(s.backgroundImage).reverse()) {
        const painted = gradientColours(layer, under);
        if (!painted) return { reason: 'background-image' };
        under = partial ? under.concat(painted) : painted;
      }
    }
    return { colours: under };
  };
  const contrast = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    const el = t.parentElement;
    if (!t.textContent.trim() || seen.has(el)) continue;
    seen.add(el);
    const style = getComputedStyle(el);
    const text = t.textContent.trim().slice(0, 40);
    let fg; let bg;
    if (el instanceof SVGTextContentElement) {
      // A <tspan> takes its fill from its <text>, which sits on the box drawn just before it.
      fg = parse(style.fill);
      const box = el.closest('text').previousElementSibling;
      const boxFill = box instanceof SVGRectElement ? parse(getComputedStyle(box).fill) : null;
      if (!(box instanceof SVGRectElement)) bg = backgroundsOf(el.ownerSVGElement);
      else if (!boxFill) bg = { reason: 'colour' };
      else if (boxFill.a === 1) bg = { colours: [boxFill] };
      else { bg = backgroundsOf(el.ownerSVGElement); if (bg.colours) bg = { colours: bg.colours.map((u) => over(boxFill, u)) }; }
    } else {
      fg = parse(style.color);
      bg = backgroundsOf(el);
    }
    if (!fg) { contrast.push({ text, reason: 'colour' }); continue; }
    if (bg.reason) { contrast.push({ text, reason: bg.reason }); continue; }
    const size = parseFloat(style.fontSize);
    const large = size >= 24 || (size >= 18.66 && Number(style.fontWeight) >= 700);
    const value = Math.min(...bg.colours.map((c) => ratio(over(fg, c), c)));
    contrast.push({ text, ratio: Math.round(value * 100) / 100, min: large ? 3 : 4.5 });
  }
  const labels = [];
  for (const text of document.querySelectorAll('svg text')) {
    const box = text.previousElementSibling;
    if (!(box instanceof SVGRectElement)) continue;
    const t = text.getBBox();
    const r = box.getBBox();
    labels.push({ text: text.textContent, fits: t.x >= r.x && t.y >= r.y && t.x + t.width <= r.x + r.width && t.y + t.height <= r.y + r.height });
  }
  return { contrast, labels, scrollWidth: document.documentElement.scrollWidth };
}

/** One read of GET /api/version, bounded by a timeout: refuses a wrong SHA or a dirty tree. */
async function readServedVersion(baseUrl, expectedSha, timeoutMs) {
  const res = await fetch(`${baseUrl.replace(/\/+$/, '')}/api/version`, {
    headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(timeoutMs)
  }).catch((err) => { throw new Error(`Served SHA check failed: GET /api/version: ${err.message}`); });
  if (!res.ok) throw new Error(`Served SHA check failed: GET /api/version returned HTTP ${res.status}`);
  const served = await res.json();
  if (String(served.sha || '').toLowerCase() !== expectedSha.toLowerCase()) {
    throw new Error(`Served SHA check failed: got "${served.sha}", expected "${expectedSha}"`);
  }
  if (served.dirty) throw new Error(`The served tree at ${served.sha} has local changes; commit them first`);
  return served;
}

export async function capture({ baseUrl, expectedSha, outputDir, versionTimeoutMs = 10000 }) {
  assertNotProduction(baseUrl);
  if (!/^[0-9a-f]{40}$/i.test(expectedSha || '')) throw new Error('--expected-sha must be a 40-hex commit');
  const served = await readServedVersion(baseUrl, expectedSha, versionTimeoutMs);
  fs.mkdirSync(outputDir, { recursive: true });

  const origin = new URL(baseUrl).origin;
  const puppeteer = resolvePuppeteer();
  const browser = await puppeteer.launch({
    executablePath: resolveExecutablePath(),
    headless: 'new',
    // No windowsHide here: @puppeteer/browsers spawns Chrome with only detached/env/stdio and drops it.
    // chrome.exe is a GUI-subsystem binary, so headless Chrome opens no console window (policy §13.10).
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
        // A text run the check cannot measure is never a pass.
        const notProven = seen.contrast.filter((c) => c.reason);
        const contrastProblems = [
          ...(lowContrast.length ? [`below AA: ${JSON.stringify(lowContrast)}`] : []),
          ...(notProven.length ? [`not proven: ${JSON.stringify(notProven)}`] : [])
        ];
        const unfit = seen.labels.filter((l) => !l.fits);
        const checks = [
          // vp.width, not innerWidth: a phone zooms out to fit wide content, so innerWidth grows with it.
          checkNoHorizontalOverflow(seen.scrollWidth, vp.width),
          { name: 'text_contrast', passed: contrastProblems.length === 0, detail: contrastProblems.join('; ') || `${seen.contrast.length} text runs, lowest ${Math.min(...seen.contrast.map((c) => c.ratio))}:1` },
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
