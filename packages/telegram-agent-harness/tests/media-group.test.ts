import { test, expect, afterEach } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { TelegramPoller } from "../extension/poller";

const originalFetch = globalThis.fetch;
const cleanup: Array<() => void> = [];

afterEach(() => {
  globalThis.fetch = originalFetch;
  for (const fn of cleanup.splice(0)) fn();
});

function createFixture() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "tg-media-group-test-"));
  const records: Array<{ sessionId: string; messageId: number; chatId: string }> = [];
  let currentSession = "session-test-4582";

  const poller = new TelegramPoller(
    "0:test-token",
    dir,
    { dmPolicy: "disabled", allowFrom: [] },
    {
      isIdle: () => true,
      onUserMessage: () => {},
      onFollowUp: () => {},
      onSteer: () => {},
      onAbort: () => {},
      onRelease: async () => {},
      getStatusText: () => "test",
      onTelegramTurnStart: () => {},
      onLedgerFailure: () => {},
    },
    {
      getSessionId: () => currentSession,
      getSlotId: () => "polysimulator",
      record: (row: { sessionId: string; messageId: number; chatId: string }) => records.push(row),
    },
  );

  cleanup.push(() => {
    poller.stop();
    fs.rmSync(dir, { recursive: true, force: true });
  });

  return { dir, poller, records, switchSession: (s: string) => { currentSession = s; } };
}

test("sendMediaGroup sends album of photos with caption on first photo only and records correlations", async () => {
  const { dir, poller, records } = createFixture();

  const file1 = path.join(dir, "shot1.png");
  const file2 = path.join(dir, "shot2.png");
  const file3 = path.join(dir, "shot3.png");
  fs.writeFileSync(file1, "img1");
  fs.writeFileSync(file2, "img2");
  fs.writeFileSync(file3, "img3");

  let requestedUrl = "";
  let receivedForm: FormData | null = null;

  globalThis.fetch = (async (url: string, init: RequestInit) => {
    requestedUrl = url;
    receivedForm = init.body as FormData;
    await Promise.resolve();
    return Response.json({
      ok: true,
      result: [
        { message_id: 101, chat: { id: 1247617658 } },
        { message_id: 102, chat: { id: 1247617658 } },
        { message_id: 103, chat: { id: 1247617658 } },
      ],
    });
  }) as typeof fetch;

  await poller.sendMediaGroup("1247617658", [file1, file2, file3], "<b>Multi-viewport proof</b>");

  expect(requestedUrl.endsWith("/sendMediaGroup")).toBe(true);
  expect(receivedForm).not.toBeNull();
  expect(receivedForm!.get("chat_id")).toBe("1247617658");

  const mediaJson = JSON.parse(String(receivedForm!.get("media")));
  expect(mediaJson).toHaveLength(3);

  // Caption on first photo only
  expect(mediaJson[0].type).toBe("photo");
  expect(mediaJson[0].media).toBe("attach://photo_0");
  expect(mediaJson[0].caption).toBe("<b>Multi-viewport proof</b>");
  expect(mediaJson[0].parse_mode).toBe("HTML");

  // Subsequent photos have no caption or parse_mode
  expect(mediaJson[1].type).toBe("photo");
  expect(mediaJson[1].media).toBe("attach://photo_1");
  expect(mediaJson[1].caption).toBeUndefined();
  expect(mediaJson[1].parse_mode).toBeUndefined();

  expect(mediaJson[2].type).toBe("photo");
  expect(mediaJson[2].media).toBe("attach://photo_2");
  expect(mediaJson[2].caption).toBeUndefined();

  // Blobs attached in FormData
  expect(receivedForm!.get("photo_0")).toBeInstanceOf(Blob);
  expect(receivedForm!.get("photo_1")).toBeInstanceOf(Blob);
  expect(receivedForm!.get("photo_2")).toBeInstanceOf(Blob);

  // Correlations recorded for all messages
  expect(records).toHaveLength(3);
  expect(records.map(r => r.messageId)).toEqual([101, 102, 103]);
  expect(records[0].sessionId).toBe("session-test-4582");
});

test("sendMediaGroup with single photo delegates to sendTelegramPhoto", async () => {
  const { dir, poller, records } = createFixture();
  const file1 = path.join(dir, "single.png");
  fs.writeFileSync(file1, "single");

  let requestedUrl = "";
  let receivedForm: FormData | null = null;

  globalThis.fetch = (async (url: string, init: RequestInit) => {
    requestedUrl = url;
    receivedForm = init.body as FormData;
    await Promise.resolve();
    return Response.json({
      ok: true,
      result: { message_id: 201, chat: { id: 1247617658 } },
    });
  }) as typeof fetch;

  await poller.sendMediaGroup("1247617658", [file1], "<b>Single photo</b>");

  expect(requestedUrl.endsWith("/sendPhoto")).toBe(true);
  expect(receivedForm!.get("caption")).toBe("<b>Single photo</b>");
  expect(records).toHaveLength(1);
  expect(records[0].messageId).toBe(201);
});

test("sendMediaGroup caps album to 10 items and rejects empty list", async () => {
  const { dir, poller } = createFixture();

  await expect(poller.sendMediaGroup("1247617658", [])).rejects.toThrow("at least one file");

  const files: string[] = [];
  for (let i = 0; i < 12; i++) {
    const f = path.join(dir, `img_${i}.png`);
    fs.writeFileSync(f, `data_${i}`);
    files.push(f);
  }

  let mediaCount = 0;
  globalThis.fetch = (async (url: string, init: RequestInit) => {
    const form = init.body as FormData;
    const media = JSON.parse(String(form.get("media")));
    mediaCount = media.length;
    return Response.json({
      ok: true,
      result: media.map((_: unknown, idx: number) => ({ message_id: 300 + idx, chat: { id: 1247617658 } })),
    });
  }) as typeof fetch;

  await poller.sendMediaGroup("1247617658", files, "Capped album");
  expect(mediaCount).toBe(10);
});
