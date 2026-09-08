import { test, expect, afterEach } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { Database } from "bun:sqlite";
import { downloadInboundMedia, selectInboundMedia, MAX_MEDIA_BYTES } from "../extension/inbound-media";
import { pathToFileURL } from "node:url";
// Installed extension path is runtime-selected and absent on other hosts.
const installed = process.env.TG_EXTENSION_PATH;
const originalFetch = globalThis.fetch;
const roots: string[] = [];
afterEach(() => { globalThis.fetch = originalFetch; for (const root of roots.splice(0)) fs.rmSync(root, { recursive: true, force: true }); });
function root() { const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-media-")); roots.push(dir); return dir; }
function mockDownload() {
 const calls: string[] = [];
 globalThis.fetch = (async (url: string, init?: RequestInit) => {
  calls.push(url);
  if (url.endsWith("/getFile")) { expect(JSON.parse(String(init?.body)).file_id).toBe("largest"); return Response.json({ ok: true, result: { file_path: "photos/file.jpg" } }); }
  if (url.includes("/file/bot")) return new Response(new Uint8Array([255,216,255,217]));
  return Response.json({ ok: true });
 }) as typeof fetch;
 return calls;
}
test("largest photo is chosen independently of array order", () => {
 expect(selectInboundMedia({ message_id: 1, chat: { id: 123, type: "private" }, date: 0, photo: [{ file_id: "largest", file_unique_id: "a", width: 100, height: 100 }, { file_id: "small", file_unique_id: "b", width: 10, height: 10 }] })?.file_id).toBe("largest");
});
for (const idle of [true, false]) test.skipIf(!installed)(`photo persisted and delivered through ${idle ? "idle" : "busy"} path with caption`, async () => {
 const { TelegramPoller } = await import(pathToFileURL(path.join(installed!, "poller.ts")).href);
 const dir = root(); const calls = mockDownload(); const delivered: string[] = [];
 const poller = new TelegramPoller("test-token", dir, { dmPolicy: "allowlist", allowFrom: ["123"] }, {
 isIdle: () => idle, getSessionFile: () => path.join(dir,"session.jsonl"), onUserMessage: (t: string) => { expect(idle).toBe(true); delivered.push(t); }, onSteer: (t: string) => { expect(idle).toBe(false); delivered.push(t); }, onFollowUp: () => {}, onAbort: () => {}, onRelease: async () => {}, getStatusText: () => "", onTelegramTurnStart: () => {}, onLedgerFailure: (t: string) => { throw new Error(t); }
 });
 poller.ingestUpdates([{ update_id: 42, message: { message_id: 3, chat: { id: 123, type: "private" }, from: { id: 123, is_bot: false }, date: 0, caption: "/cancel", photo: [{ file_id: "largest", file_unique_id: "unique", width: 10, height: 10 }] } }]);
 await poller.redrivePendingUpdates();
 const target = path.join(dir,"local","telegram-inbound","42.jpg");
 expect(delivered).toEqual([`[Telegram image from operator | /cancel] attachment: ${target}`]);
 expect(fs.readFileSync(target)).toEqual(Buffer.from([255,216,255,217]));
 const db = new Database(path.join(dir,"veyyon_bridge_state.db"));
 expect((db.query<{ media_json: string }, []>("select media_json from update_ledger").get())?.media_json).toContain("largest"); db.close(); poller.stop();
 expect(calls.some(c => c.includes("getUpdates"))).toBe(false);
});
test("PDF download uses fixed safe extension", async () => {
 mockDownload(); const dir = root(); const result = await downloadInboundMedia("test-token", { file_id: "largest", mime_type: "application/pdf" }, dir, 43);
 expect(result).toBe(path.join(dir,"43.pdf"));
});
test("oversize metadata and unsupported documents fail before fetch", async () => {
 globalThis.fetch = (() => { throw new Error("must not fetch"); }) as typeof fetch;
 await expect(downloadInboundMedia("test-token", { file_id: "x", mime_type: "image/png", file_size: MAX_MEDIA_BYTES+1 }, root(), 1)).rejects.toThrow("20 MB");
 await expect(downloadInboundMedia("test-token", { file_id: "x", mime_type: "application/zip" }, root(), 1)).rejects.toThrow("Unsupported");
});
test("stream limit removes partial download and hides token-bearing errors", async () => {
 const dir = root(); let calls=0;
 globalThis.fetch = (async () => ++calls === 1 ? Response.json({ ok: true, result: { file_path: "photos/x.jpg" } }) : new Response(new Uint8Array(MAX_MEDIA_BYTES+1))) as typeof fetch;
 await expect(downloadInboundMedia("SECRET", { file_id: "x", mime_type: "image/jpeg" }, dir, 1)).rejects.toThrow("20 MB");
 expect(fs.readdirSync(dir)).toEqual([]);
});
