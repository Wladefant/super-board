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

function makePoller(options: ConstructorParameters<typeof TelegramPoller>[5], callbacks: Partial<PollerCallbacks>) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-menu-test-"));
  const poller = new TelegramPoller(
    "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
    dir,
    { dmPolicy: "allowlist", allowFrom: ["1247617658"] },
    {
      isIdle: () => true, onUserMessage: () => {}, onFollowUp: () => {}, onSteer: () => {}, onAbort: () => {},
      onRelease: async () => {}, getStatusText: () => "status", onLedgerFailure: () => {}, ...callbacks,
    } as PollerCallbacks,
    null,
    options,
  );
  cleanup.push(() => {
    poller.stop();
    try { fs.rmSync(dir, { recursive: true, force: true }); } catch {}
  });
  return poller;
}

const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });

test("polling starts while the menu registration is still hanging", async () => {
  let getUpdates = 0;
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    const u = String(url);
    if (u.includes("setMyCommands")) {
      // Hangs until the request is aborted, as a stalled connect does.
      return new Promise<Response>((_, reject) => init?.signal?.addEventListener("abort", () => reject(new Error("aborted"))));
    }
    if (u.includes("getUpdates")) { getUpdates++; await Bun.sleep(20); }
    return json({ ok: true, result: [] });
  }) as typeof fetch;

  const poller = makePoller({ registrationRetryDelaysMs: [] }, {});
  void poller.start();
  await Bun.sleep(300);
  expect(getUpdates).toBeGreaterThan(0);
});

test("a failed menu registration is retried and never reported as an inbound ledger failure", async () => {
  let setMyCommands = 0;
  const ledgerFailures: string[] = [];
  globalThis.fetch = (async (url: string | URL | Request) => {
    const u = String(url);
    if (u.includes("setMyCommands")) {
      setMyCommands++;
      if (setMyCommands <= 2) throw new Error("The operation timed out");
      return json({ ok: true, result: true });
    }
    if (u.includes("getUpdates")) await Bun.sleep(20);
    return json({ ok: true, result: [] });
  }) as typeof fetch;

  const poller = makePoller({ registrationRetryDelaysMs: [10, 10, 10] }, { onLedgerFailure: (m) => ledgerFailures.push(m) });
  void poller.start();
  await Bun.sleep(400);
  expect(setMyCommands).toBe(3);
  expect(ledgerFailures).toEqual([]);
});

test("a registration that never succeeds is reported once as a menu failure, not a ledger failure", async () => {
  const menuFailures: string[] = [];
  const ledgerFailures: string[] = [];
  globalThis.fetch = (async (url: string | URL | Request) => {
    if (String(url).includes("setMyCommands")) throw new Error("The operation timed out");
    if (String(url).includes("getUpdates")) await Bun.sleep(20);
    return json({ ok: true, result: [] });
  }) as typeof fetch;

  const poller = makePoller({ registrationRetryDelaysMs: [10, 10] }, {
    onLedgerFailure: (m) => ledgerFailures.push(m),
    onMenuRegistrationFailure: (m) => menuFailures.push(m),
  });
  void poller.start();
  await Bun.sleep(400);
  expect(menuFailures).toHaveLength(1);
  expect(menuFailures[0]).toContain("Inbound polling is unaffected");
  expect(menuFailures[0]).not.toContain("123456:");
  expect(ledgerFailures).toEqual([]);
});
