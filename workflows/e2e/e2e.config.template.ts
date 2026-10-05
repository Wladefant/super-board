// e2e.config.ts template for Superboard-managed repos.
// Policy: workflows/e2e/POLICY.md. Pins: workflows/e2e/pins.json. Issue: https://github.com/Wladefant/super-board/issues/476
//
// Per repo you change exactly two things: STAGING_HOSTS and the `app` block (url, optional command).
// Everything else stays as in this file so `e2e_guard.py config` passes.
import type { E2EConfig } from 'e2e';
import { web } from '@e2e-dev/web';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';

// Per repo, edit only this list. Exact hosts or parent domains (subdomains match). Never a production host.
const STAGING_HOSTS: string[] = []; // e.g. ['staging.example.test']

// BEGIN host-guard
// Fail closed. Do not edit this block: `e2e_guard.py config` compares it with the template byte for byte.
const normHostName = (h: string): string => h.toLowerCase().replace(/\.+$/, '');
// Production deny-list: exact hosts, plus tokens (project refs) that can sit inside a longer host.
const FORBIDDEN_EXACT_HOSTS: string[] = [
  'polysimulator.com',
  'www.polysimulator.com',
  'app.polysimulator.com',
  'prod.polysimulator.com',
];
const FORBIDDEN_HOST_TOKENS: string[] = ['zaraprptkegxqpvnsubu', 'akamai-iad-prod'];
const ALLOWED_HOSTS: string[] = ['localhost', '127.0.0.1', ...STAGING_HOSTS].map(normHostName);
export const hostRefusal = (rawHost: string): string | null => {
  const host = normHostName(rawHost);
  if (FORBIDDEN_EXACT_HOSTS.includes(host) || FORBIDDEN_HOST_TOKENS.some((t) => host.includes(t))) {
    return 'forbidden-production-host';
  }
  if (!ALLOWED_HOSTS.some((a) => host === a || host.endsWith('.' + a))) return 'not-in-allow-list';
  return null;
};
const appUrl = process.env.APP_URL ?? 'http://127.0.0.1:3000';
const appHost = normHostName(new URL(appUrl).hostname);
const appRefusal = hostRefusal(appHost);
if (appRefusal) {
  throw new Error(`E2E_HOST_NOT_ALLOWED: ${appHost} (${appRefusal}); allowed: ${ALLOWED_HOSTS.join(', ')}`);
}
// Request level: every network request of a page goes through this check (e2e.request-guard.ts calls it from
// browser.route and aborts the request). It covers subresources, iframe and popup documents, not only the top
// document (proven by fixture/tests/request-guard.e2e.ts). WebSocket traffic is not proven. Only http(s) and ws(s) are checked; data:, blob: and about: never leave the browser.
export const requestHostRefusal = (rawUrl: string): string | null => {
  let u: URL;
  try {
    u = new URL(rawUrl);
  } catch {
    return 'unparseable-url';
  }
  if (!['http:', 'https:', 'ws:', 'wss:'].includes(u.protocol)) return null;
  return hostRefusal(u.hostname);
};
// Runs before the page's own scripts in every document and frame. A navigation or redirect that lands on a
// host outside the allow-list is stopped and blanked, so no later step can act there. The redirect hops from
// APP_URL are also checked by e2e_run.py before the run starts.
const NAV_GUARD = `(() => {
  const allowed = ${JSON.stringify(ALLOWED_HOSTS)};
  const host = location.hostname.toLowerCase().replace(/\\.+$/, '');
  if (!host || allowed.some((a) => host === a || host.endsWith('.' + a))) return;
  window.stop();
  document.documentElement.innerHTML = '<title>E2E_HOST_NOT_ALLOWED</title>';
  throw new Error('E2E_HOST_NOT_ALLOWED: ' + host);
})();`;
// END host-guard

// Model route: OpenCode Go (OpenAI-compatible), our own API key, Flash class, thinking off.
// No `e2e login`, no subscription. Replay needs no key. The key arrives only in the process
// environment (E2E_MODEL_API_KEY) and only in record mode (see e2e_run.py --record).
const go = createOpenAICompatible({
  name: 'opencode-go',
  baseURL: process.env.E2E_MODEL_BASE_URL ?? 'https://opencode.ai/zen/go/v1',
  apiKey: process.env.E2E_MODEL_API_KEY,
  // OpenCode Go refuses requests without this header ("missing x-opencode-session").
  headers: { 'x-opencode-session': process.env.E2E_SESSION_ID ?? 'superboard-e2e' },
});
const model = go.chatModel(process.env.E2E_MODEL ?? 'qwen3.8-flash');
const providerOptions = { opencodeGo: { enable_thinking: false } };

const app = { url: appUrl };

export default {
  // Cached replay is the default. Record mode is opt-in: E2E_CACHE_MODE=read-write.
  cache: (process.env.E2E_CACHE_MODE as 'read-only' | 'read-write' | undefined) ?? 'read-only',
  agents: {
    default: {
      model,
      providerOptions,
      system: 'You are a thorough QA agent. Verify every outcome on screen. Never leave the app host.',
    },
  },
  // Target names are the viewports. e2e_receipt.py reads them for FLOW-QA-VIEWPORTS.
  targets: [
    { name: '390x844', engine: web({ viewport: { width: 390, height: 844 }, initScripts: [NAV_GUARD] }), app },
    { name: '390x420', engine: web({ viewport: { width: 390, height: 420 }, initScripts: [NAV_GUARD] }), app },
    { name: '1440x900', engine: web({ viewport: { width: 1440, height: 900 }, initScripts: [NAV_GUARD] }), app },
  ],
  workers: 1,
} satisfies E2EConfig;
