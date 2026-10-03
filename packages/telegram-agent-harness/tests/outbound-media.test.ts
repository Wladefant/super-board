import { afterEach, beforeEach, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import {
  sendTelegramAttachment,
  TELEGRAM_DOCUMENT_LIMIT_BYTES,
  TELEGRAM_PHOTO_LIMIT_BYTES,
} from "../extension/outbound-media";

const originalFetch = globalThis.fetch;
let directory = "";

beforeEach(() => {
  directory = fs.mkdtempSync(path.join(os.tmpdir(), "tg-outbound-media-"));
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  fs.rmSync(directory, { recursive: true, force: true, maxRetries: 10, retryDelay: 25 });
});

function captureSuccessfulSend(chatId = -1004422647618) {
  const calls: Array<{ method: string; form: FormData }> = [];
  globalThis.fetch = (async (url: string | URL | Request, init?: RequestInit) => {
    const method = String(url).split("/").pop()!;
    const form = await new Request(url, init).formData();
    calls.push({ method, form });
    return Response.json({
      ok: true,
      result: { message_id: 731, chat: { id: chatId }, date: 1 },
    });
  }) as unknown as typeof fetch;
  return calls;
}

test("photo upload keeps the target supergroup topic, caption, and requested filename", async () => {
  const file = path.join(directory, "generated.png");
  fs.writeFileSync(file, Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=",
    "base64",
  ));
  const calls = captureSuccessfulSend();

  const sent = await sendTelegramAttachment({
    token: "0:test-only",
    chatId: "-1004422647618",
    messageThreadId: 344,
    filePath: file,
    kind: "auto",
    caption: "Proof for **issue 344**",
    filename: "topic-proof.png",
  });

  expect(sent.kind).toBe("photo");
  expect(sent.filename).toBe("topic-proof.png");
  expect(calls).toHaveLength(1);
  expect(calls[0]!.method).toBe("sendPhoto");
  expect(calls[0]!.form.get("chat_id")).toBe("-1004422647618");
  expect(calls[0]!.form.get("message_thread_id")).toBe("344");
  expect(calls[0]!.form.get("caption")).toContain("<b>issue 344</b>");
  expect(calls[0]!.form.get("parse_mode")).toBe("HTML");
  const uploaded = calls[0]!.form.get("photo") as File;
  expect(uploaded.name).toBe("topic-proof.png");
  expect(uploaded.size).toBe(fs.statSync(file).size);
});

test("PDF upload uses sendDocument in the intended topic and keeps its original filename", async () => {
  const file = path.join(directory, "generated-proof.pdf");
  fs.writeFileSync(file, "%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n");
  const calls = captureSuccessfulSend();

  const sent = await sendTelegramAttachment({
    token: "0:test-only",
    chatId: -1004422647618,
    messageThreadId: 344,
    filePath: file,
    kind: "auto",
    caption: "Generated PDF proof",
  });

  expect(sent.kind).toBe("document");
  expect(calls[0]!.method).toBe("sendDocument");
  expect(calls[0]!.form.get("message_thread_id")).toBe("344");
  expect((calls[0]!.form.get("document") as File).name).toBe("generated-proof.pdf");
});

test("generic file is a document and a direct chat send omits message_thread_id", async () => {
  const file = path.join(directory, "build-output.log");
  fs.writeFileSync(file, "safe generated fixture\n");
  const calls = captureSuccessfulSend(123456);

  const sent = await sendTelegramAttachment({
    token: "0:test-only",
    chatId: 123456,
    filePath: file,
    kind: "auto",
  });

  expect(sent.kind).toBe("document");
  expect(calls[0]!.method).toBe("sendDocument");
  expect(calls[0]!.form.get("chat_id")).toBe("123456");
  expect(calls[0]!.form.has("message_thread_id")).toBe(false);
  expect((calls[0]!.form.get("document") as File).name).toBe("build-output.log");
});

test("missing files and files over each Telegram limit fail before network access", async () => {
  let fetchCalls = 0;
  globalThis.fetch = (async () => {
    fetchCalls++;
    throw new Error("network must not be reached");
  }) as unknown as typeof fetch;

  await expect(sendTelegramAttachment({
    token: "0:test-only",
    chatId: "1",
    filePath: path.join(directory, "missing.pdf"),
  })).rejects.toThrow(/not readable/);

  const largePhoto = path.join(directory, "large.png");
  fs.writeFileSync(largePhoto, "x");
  fs.truncateSync(largePhoto, TELEGRAM_PHOTO_LIMIT_BYTES + 1);
  await expect(sendTelegramAttachment({
    token: "0:test-only",
    chatId: "1",
    filePath: largePhoto,
    kind: "photo",
  })).rejects.toThrow(/photo limit exceeded.*10\.0 MiB/i);

  const largeDocument = path.join(directory, "large.bin");
  fs.writeFileSync(largeDocument, "x");
  fs.truncateSync(largeDocument, TELEGRAM_DOCUMENT_LIMIT_BYTES + 1);
  await expect(sendTelegramAttachment({
    token: "0:test-only",
    chatId: "1",
    filePath: largeDocument,
    kind: "document",
  })).rejects.toThrow(/document limit exceeded.*50\.0 MiB/i);
  expect(fetchCalls).toBe(0);
});

test("Telegram API rejection reaches the caller with the method and API diagnostic", async () => {
  const file = path.join(directory, "proof.pdf");
  fs.writeFileSync(file, "%PDF-1.4\n%%EOF\n");
  globalThis.fetch = (async () => Response.json({
    ok: false,
    error_code: 400,
    description: "Bad Request: message thread not found",
  }, { status: 400 })) as unknown as typeof fetch;

  await expect(sendTelegramAttachment({
    token: "0:test-only",
    chatId: "-1004422647618",
    messageThreadId: 999,
    filePath: file,
  })).rejects.toThrow("Telegram sendDocument failed (400): Bad Request: message thread not found");
});
