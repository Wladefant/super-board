import { test, expect } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { pathToFileURL } from "node:url";
const installed = process.env.TG_EXTENSION_PATH;
// Installed extension path is runtime-selected and does not exist on other hosts.

test.skipIf(!installed)("installed routing keeps command, authorization and decision-text paths separate", async () => {
  const { TelegramPoller } = await import(pathToFileURL(path.join(installed!, "poller.ts")).href);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-command-test-"));
  const commands: string[] = [], inbound: string[] = [];
  const poller = new TelegramPoller("0:test-only", dir, { dmPolicy: "allowlist", allowFrom: ["1"] }, {
    isIdle: () => true, onUserMessage: (text: string) => inbound.push(text), onFollowUp: () => {}, onSteer: () => {}, onAbort: () => {}, onRelease: async () => {}, getStatusText: () => "test", onTelegramTurnStart: () => {}, onLedgerFailure: () => {},
    onHarnessCommand: async (text: string) => { await Promise.resolve(); if (!/^\/(agents|usage|prompt|shot)(?:\s|$)/.test(text)) return false; commands.push(text); return true; },
  });
  try {
    for (const text of ["/agents", "/usage", "/prompt veyyon:root hello", "/shot veyyon:root"]) await poller.processLedgerRow({ update_id: 1, text, chat_id: "1", user_id: "1" });
    expect(commands).toHaveLength(4); expect(inbound).toHaveLength(0);
    await poller.processLedgerRow({ update_id: 2, text: "/agents", chat_id: "2", user_id: "2" });
    expect(commands).toHaveLength(4);
    await poller.processLedgerRow({ update_id: 3, text: "Please revise option A", chat_id: "1", user_id: "1" });
    expect(inbound).toEqual(["Please revise option A"]);
  } finally { poller.stop(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test.skipIf(!installed)("installed photo uses multipart sendPhoto and preserves session correlation across await", async () => {
  const { TelegramPoller } = await import(pathToFileURL(path.join(installed!, "poller.ts")).href);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-photo-test-"));
  const png = path.join(dir, "shot.png"); fs.writeFileSync(png, Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", "base64"));
  const originalFetch = globalThis.fetch;
  let session = "original"; const records: { sessionId: string; messageId: number }[] = [];
  const poller = new TelegramPoller("0:test-only", dir, { dmPolicy: "disabled", allowFrom: [] }, {}, { getSessionId: () => session, getSlotId: () => "test", record: (row: { sessionId: string; messageId: number }) => records.push(row) });
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    expect(url.endsWith("/sendPhoto")).toBe(true);
    const form = init.body as FormData; expect(form.get("photo")).toBeInstanceOf(Blob); expect(form.get("parse_mode")).toBe("HTML");
    await Promise.resolve(); session = "switched";
    return Response.json({ ok: true, result: { message_id: 9, chat: { id: 1 } } });
  }) as typeof fetch;
  try { await poller.sendTelegramPhoto("1", png, "<b>Session screenshot</b>"); expect(records[0].sessionId).toBe("original"); expect(records[0].messageId).toBe(9); }
  finally { globalThis.fetch = originalFetch; poller.stop(); fs.rmSync(dir, { recursive: true, force: true }); }
});

test.skipIf(!installed)("resolved decision edit removes inline buttons in the same request", async () => {
  const { TelegramPoller } = await import(pathToFileURL(path.join(installed!, "poller.ts")).href);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-decision-edit-"));
  const originalFetch = globalThis.fetch;
  const poller = new TelegramPoller("0:test-only", dir, { dmPolicy: "disabled", allowFrom: [] }, {});
  globalThis.fetch = (async (_url: string, init: RequestInit) => {
    await Promise.resolve();
    const body = JSON.parse(String(init.body));
    expect(body.reply_markup).toEqual({ inline_keyboard: [] });
    expect(body.text).toContain("Resolved");
    return Response.json({ ok: true, result: { message_id: 9 } });
  }) as typeof fetch;
  try { expect((await poller.editTelegramMessage("1", 9, "<b>Resolved</b>"))?.ok).toBe(true); }
  finally { globalThis.fetch = originalFetch; poller.stop(); fs.rmSync(dir, { recursive: true, force: true }); }
});
