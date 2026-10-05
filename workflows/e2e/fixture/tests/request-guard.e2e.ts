import { test } from '@e2e-dev/web';
import { expect } from 'e2e';
import { installRequestGuard } from '../e2e.request-guard.ts';

installRequestGuard();

test('request guard: fetch, iframe and popup to a host outside the allow-list never leave the browser', async ({ app, screen }) => {
  await app.open('/probe.html');
  await expect(screen.getByRole('status')).toHaveText('allowed-own,blocked-other,hits=0');
});
