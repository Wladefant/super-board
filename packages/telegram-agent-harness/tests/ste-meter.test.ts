/**
 * ste-meter.test.ts - the outbound style meter runs the real checker, records one counts
 * line, and never throws or blocks when the checker is missing or the meter is off.
 */
import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { meterOutbound, steArgs } from "../extension/ste-meter";
import { TelegramPoller, type PollerCallbacks } from "../extension/poller";
import type { MessageCorrelationBridge } from "../extension/types";
import { instantTransport } from "./instant-transport";

const script = path.resolve(import.meta.dir, "../../../workflows/portable/ste_check.py");
const saved = { ...process.env };
const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  process.env = { ...saved };
});

function tmpMetrics(): string {
  return path.join(fs.mkdtempSync(path.join(os.tmpdir(), "ste-meter-")), "m.jsonl");
}

describe("STE outbound meter", () => {
  test("argument vector reads stdin and writes counts only", () => {
    expect(steArgs("s.py", "html", "src", "m.jsonl")).toEqual(
      ["s.py", "check", "--format", "html", "--summary-json", "--metrics", "m.jsonl", "--source", "src", "-"],
    );
  });

  test("a real message produces a summary and one metrics line", async () => {
    const metrics = tmpMetrics();
    process.env.SUPER_BOARD_STE_CHECK = script;
    process.env.SUPER_BOARD_STE_METRICS = metrics;
    const summary = await meterOutbound("<b>Done.</b> We utilize the cache prior to the run; it failed.", "html", "unit");
    expect(summary).not.toBeNull();
    const parsed = JSON.parse(summary as string);
    expect(parsed.errors).toBe(1);
    const line = JSON.parse(fs.readFileSync(metrics, "utf8").trim());
    expect(line.source).toBe("unit");
    expect(line.rules).toContain("semicolon");
    expect(line.rules).toContain("plain-word");
  });

  test("a missing checker resolves null and does not throw", async () => {
    process.env.SUPER_BOARD_STE_CHECK = path.join(os.tmpdir(), "no-such-ste-check.py");
    expect(await meterOutbound("Done.", "md", "unit")).toBeNull();
  });

  test("SUPER_BOARD_STE_METER=0 turns the meter off", async () => {
    const metrics = tmpMetrics();
    process.env.SUPER_BOARD_STE_CHECK = script;
    process.env.SUPER_BOARD_STE_METRICS = metrics;
    process.env.SUPER_BOARD_STE_METER = "0";
    expect(await meterOutbound("Done.", "md", "unit")).toBeNull();
    expect(fs.existsSync(metrics)).toBe(false);
  });

  test("the real send path delivers the message and records one metrics line", async () => {
    const metrics = tmpMetrics();
    process.env.SUPER_BOARD_STE_CHECK = script;
    process.env.SUPER_BOARD_STE_METRICS = metrics;
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-ste-"));
    const bridge: MessageCorrelationBridge = {
      getSessionId: () => "s", getSlotId: () => "t", record: () => {},
      resolveReply: () => ({ decision: "reject_unknown", detail: "" }),
      resolveCallback: () => ({ decision: "reject_unknown", record: null as never, detail: "" }),
      consumeCallback: () => false,
    };
    const callbacks = {
      isIdle: () => true, onUserMessage: () => {}, onFollowUp: () => {}, onSteer: () => {}, onAbort: () => {},
      onRelease: async () => {}, getStatusText: () => "t", onLedgerFailure: () => {},
    } as unknown as PollerCallbacks;
    const poller = new TelegramPoller("0:test-only", dir, { dmPolicy: "allowlist", allowFrom: ["1"] }, callbacks, bridge, instantTransport({ sendTimeoutMs: 1000 }));
    const sent: Array<Record<string, unknown>> = [];
    globalThis.fetch = (async (_input: unknown, init?: { body?: string }) => {
      sent.push(JSON.parse(String(init?.body ?? "{}")));
      return Response.json({ ok: true, result: { message_id: 7, chat: { id: 1 } } });
    }) as typeof fetch;
    try {
      const res = await poller.sendTelegramMessage("1", "Done. We utilize the cache prior to the run; it failed.");
      expect(res?.ok).toBe(true);
      expect(sent).toHaveLength(1);
      for (let i = 0; i < 50 && !fs.existsSync(metrics); i++) await Bun.sleep(100);
      const line = JSON.parse(fs.readFileSync(metrics, "utf8").trim().split("\n")[0]);
      expect(line.source).toBe("telegram-send");
      expect(line.rules).toContain("semicolon");
    } finally {
      poller.stop();
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});
