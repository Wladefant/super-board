import { test } from '@e2e-dev/web';
import { expect } from 'e2e';
import { installRequestGuard } from '../e2e.request-guard.ts';

installRequestGuard();

test('shop: deterministic locator step then one agent step', async ({ app, agent, screen }) => {
  await app.open('/');
  await expect(screen.getByRole('heading', { name: 'Spike Shop' })).toBeVisible();
  await agent.act('add the Blue boot to the cart');
  await expect(screen.getByRole('status')).toHaveText('Added Blue boot');
});
