// e2e.config.ts template for Superboard-managed repos.
// Policy: workflows/e2e/POLICY.md. Pins: workflows/e2e/pins.json. Issue: https://github.com/Wladefant/super-board/issues/476
//
// Per repo you change exactly two things: STAGING_HOSTS and the `app` block (url, optional command).
// Everything else stays as in this file so `e2e_guard.py config` passes.
import type { E2EConfig } from 'e2e';
import { web } from '@e2e-dev/web';
import { createOpenAICompatible } from '@ai-sdk/openai-compatible';

// BEGIN host-guard
// Fail closed: the app host must be local or an allow-listed staging host. Production is refused by name.
const STAGING_HOSTS: string[] = []; // e.g. ['staging.example.test']; never a production host
const ALLOWED_HOSTS: string[] = ['localhost', '127.0.0.1', ...STAGING_HOSTS];
const FORBIDDEN_HOSTS: string[] = [
  'zaraprptkegxqpvnsubu',
  'akamai-iad-prod',
  'polysimulator.com',
  'app.polysimulator.com',
  'prod.polysimulator.com',
];
const appUrl = process.env.APP_URL ?? 'http://127.0.0.1:3000';
const appHost = new URL(appUrl).hostname.toLowerCase();
if (FORBIDDEN_HOSTS.some((f) => appHost === f || appHost.includes(f)) || !ALLOWED_HOSTS.includes(appHost)) {
  throw new Error(`E2E_HOST_NOT_ALLOWED: ${appHost} is not in the allow-list (${ALLOWED_HOSTS.join(', ')})`);
}
// END host-guard

// Model route: OpenCode Go (OpenAI-compatible), our own API key, Flash class, thinking off.
// No `e2e login`, no subscription. Replay needs no key. The key arrives only in the process
// environment (E2E_MODEL_API_KEY) and only in record mode (see e2e_run.py --record).
const go = createOpenAICompatible({
  name: 'opencode-go',
  baseURL: process.env.E2E_MODEL_BASE_URL ?? 'https://opencode.ai/zen/go/v1',
  apiKey: process.env.E2E_MODEL_API_KEY,
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
    { name: '390x844', engine: web({ viewport: { width: 390, height: 844 } }), app },
    { name: '390x420', engine: web({ viewport: { width: 390, height: 420 } }), app },
    { name: '1440x900', engine: web({ viewport: { width: 1440, height: 900 } }), app },
  ],
  workers: 1,
} satisfies E2EConfig;
