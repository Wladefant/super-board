/**
 * Times the daemon's owner scan and its event-loop stall. Run with bun from the package directory:
 *   bun scripts/measure-owner-scan.ts [runs]
 * Prints one JSON line: wall time per scan and the longest gap a 5 ms ticker saw (the event-loop block).
 */
import * as sessionControl from "../daemon/session-control";

const runs = Number(process.argv[2] ?? 5);
const scan = (sessionControl as Record<string, unknown>).discoverOwners as () => unknown;

let maxGap = 0;
let last = performance.now();
const ticker = setInterval(() => {
  const now = performance.now();
  maxGap = Math.max(maxGap, now - last);
  last = now;
}, 5);

const wall: number[] = [];
for (let index = 0; index < runs; index++) {
  const started = performance.now();
  const found = await scan();
  wall.push(Math.round(performance.now() - started));
  if (index === 0) console.error(`owners found: ${(found as unknown[]).length}`);
  // Space the scans like the daemon's 10 s forum poll would, but short enough to measure.
  await new Promise(resolve => setTimeout(resolve, 50));
}
clearInterval(ticker);
console.log(JSON.stringify({ runs, wallMs: wall, maxEventLoopGapMs: Math.round(maxGap) }));
