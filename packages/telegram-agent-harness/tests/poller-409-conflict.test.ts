import { test, expect, afterEach } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { TelegramPoller, type PollerCallbacks } from "../extension/poller";

const originalFetch = globalThis.fetch;
const cleanup: Array<() => void> = [];

afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const close of cleanup.splice(0)) close();
});

test("HTTP 409 conflict triggers bounded backoff, warning diagnosis, and terminates polling", async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-409-test-"));
  const conflictDiagnoses: string[] = [];
  const conflictAttempts: number[] = [];

  const callbacks: PollerCallbacks & { onConflict?: (diagnosis: string, attempt: number, maxAttempts: number) => void } = {
    isIdle: () => true,
    onUserMessage: () => {},
    onFollowUp: () => {},
    onSteer: () => {},
    onAbort: () => {},
    onRelease: async () => {},
    getStatusText: () => "status",
    onLedgerFailure: () => {},
    onConflict: (diagnosis: string, attempt: number) => {
      conflictDiagnoses.push(diagnosis);
      conflictAttempts.push(attempt);
    },
  };

  let getUpdatesFetchCount = 0;
  globalThis.fetch = (async (url: string | URL | Request) => {
    const urlStr = String(url);
    if (urlStr.includes("getUpdates")) {
      getUpdatesFetchCount++;
      return new Response(
        JSON.stringify({
          ok: false,
          error_code: 409,
          description: "Conflict: terminated by other getUpdates request; make sure that only one bot instance is running",
        }),
        {
          status: 409,
          statusText: "Conflict",
          headers: { "Content-Type": "application/json" },
        },
      );
    }
    return new Response(JSON.stringify({ ok: true, result: [] }));
  }) as typeof fetch;

  const poller = new TelegramPoller(
    "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
    dir,
    { dmPolicy: "allowlist", allowFrom: ["1247617658"] },
    callbacks as PollerCallbacks,
    null,
    {
      maxConflictRetries: 3,
      initialConflictBackoffMs: 10,
      maxConflictBackoffMs: 50,
    },
  );

  cleanup.push(() => {
    poller.stop();
    try {
      fs.rmSync(dir, { recursive: true, force: true });
    } catch {}
  });

  const startTime = Date.now();
  // With maxConflictRetries: 3 and backoffs 10ms, 20ms, start() must terminate automatically!
  // If it loops forever, Promise.race with a timeout will catch it.
  const pollerFinished = poller.start();
  const timeoutPromise = Bun.sleep(2000).then(() => "TIMED_OUT");

  const outcome = await Promise.race([pollerFinished.then(() => "FINISHED"), timeoutPromise]);
  const elapsed = Date.now() - startTime;

  // Assert termination: poller must terminate on its own, not time out
  expect(outcome).toBe("FINISHED");
  expect(poller.running).toBe(false);

  // Assert bounded attempts
  expect(getUpdatesFetchCount).toBe(3);
  expect(conflictAttempts).toEqual([1, 2, 3]);

  // Assert warning diagnosis content
  expect(conflictDiagnoses.length).toBeGreaterThanOrEqual(3);
  expect(conflictDiagnoses[0]).toContain("409");
  expect(conflictDiagnoses[0]).toContain("Conflict: terminated by other getUpdates request");
  expect(conflictDiagnoses[conflictDiagnoses.length - 1]).toContain("terminated");

  // Assert bound: elapsed time must be at least the backoffs (10 + 20 = 30ms) but well under 1s
  expect(elapsed).toBeGreaterThanOrEqual(25);
  expect(elapsed).toBeLessThan(1500);
});
