// Request-level host guard for e2e tests. Copy next to e2e.config.ts as e2e.request-guard.ts.
// Policy: workflows/e2e/POLICY.md rule 6. Issue: https://github.com/Wladefant/super-board/issues/476
//
// Every test file calls installRequestGuard() once at the top level. It registers a beforeEach hook that
// routes every request of the attempt (browser.route) and aborts each one whose host the config refuses
// (route.abort). A test route registered later runs first; never call route.continue() on an external URL,
// use route.fallback() so this guard still sees it.
import { beforeEach } from '@e2e-dev/web';
import { requestHostRefusal } from './e2e.config.ts';

export function installRequestGuard(): void {
  beforeEach(async ({ browser }) => {
    await browser.route('**', async (route) => {
      const refusal = requestHostRefusal(route.request.url);
      if (refusal) {
        console.warn(`E2E_HOST_NOT_ALLOWED: aborted ${route.request.method} ${route.request.url} (${refusal})`);
        await route.abort();
        return;
      }
      await route.fallback();
    });
  });
}
