import { TelegramGovernor } from "../extension/telegram-governor";
import type { PollerOptions } from "../extension/poller";

/**
 * Poller options for tests that exercise message flow, not Bot API pacing.
 *
 * Without them every outbound call waits on the real clock: the poller's 1250 ms pace plus the
 * process-wide per-bot governor's 1 s chat interval, whose state is shared by every test file that
 * uses the same bot id. A test with a few sends then takes seconds, and under full-suite load it
 * crosses bun's 5 s limit. Each call gets its own governor with no interval and no pacing sleep, so
 * nothing waits on a timer and no state leaks between tests. Pacing itself is covered by
 * telegram-governor.test.ts and telegram-budget.test.ts with injected clocks.
 */
export function instantTransport(overrides: PollerOptions = {}): PollerOptions {
  return {
    outboundPaceMs: 0,
    governor: new TelegramGovernor({ chatIntervalMs: 0, groupLimit: 1_000_000, sleep: async () => {} }),
    ...overrides,
  };
}
