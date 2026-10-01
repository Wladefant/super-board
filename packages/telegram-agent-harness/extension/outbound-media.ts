import { stat } from "node:fs/promises";
import * as path from "node:path";
import { formatTelegramCaption, redactSecrets } from "./sanitizer";
import type { TelegramSendMessageResponse } from "./types";

export type TelegramAttachmentKind = "photo" | "document";
export type TelegramAttachmentSelection = TelegramAttachmentKind | "auto";

export const TELEGRAM_PHOTO_LIMIT_BYTES = 10 * 1024 * 1024;
export const TELEGRAM_DOCUMENT_LIMIT_BYTES = 50 * 1024 * 1024;

const PHOTO_EXTENSIONS: Record<string, true> = {
  ".jpg": true,
  ".jpeg": true,
  ".png": true,
  ".webp": true,
};

export interface TelegramAttachmentRequest {
  token: string;
  chatId: string | number;
  filePath: string;
  kind?: TelegramAttachmentSelection;
  caption?: string;
  filename?: string;
  defaultRepo?: string;
  messageThreadId?: number;
  replyMarkup?: Record<string, unknown>;
  signal?: AbortSignal;
}

export interface TelegramAttachmentResult {
  kind: TelegramAttachmentKind;
  filename: string;
  sizeBytes: number;
  response: TelegramSendMessageResponse;
}

export function selectTelegramAttachmentKind(
  filePath: string,
  selection: TelegramAttachmentSelection = "auto",
): TelegramAttachmentKind {
  if (selection !== "auto") return selection;
  return PHOTO_EXTENSIONS[path.extname(filePath).toLowerCase()] ? "photo" : "document";
}

function displayMiB(bytes: number): string {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function safeFilename(filePath: string, requested?: string): string {
  const filename = path.basename(requested?.trim() || filePath);
  if (!filename || filename === "." || filename === "..") {
    throw new Error("Attachment filename must name a file, not a directory.");
  }
  return filename;
}

export async function sendTelegramAttachment(
  request: TelegramAttachmentRequest,
): Promise<TelegramAttachmentResult> {
  const kind = selectTelegramAttachmentKind(request.filePath, request.kind);
  const filename = safeFilename(request.filePath, request.filename);
  let fileStat;
  try {
    fileStat = await stat(request.filePath);
  } catch (error: unknown) {
    const detail = error instanceof Error ? error.message : String(error);
    throw new Error(`Attachment file is not readable: ${request.filePath} (${detail})`);
  }
  if (!fileStat.isFile()) {
    throw new Error(`Attachment path is not a regular file: ${request.filePath}`);
  }

  const limit = kind === "photo" ? TELEGRAM_PHOTO_LIMIT_BYTES : TELEGRAM_DOCUMENT_LIMIT_BYTES;
  if (fileStat.size > limit) {
    const suggestion = kind === "photo"
      ? " Compress the image or send it as a document if it is at most 50 MiB."
      : " Reduce the file below Telegram's document limit before retrying.";
    throw new Error(
      `Telegram ${kind} limit exceeded: ${filename} is ${displayMiB(fileStat.size)}; maximum is ${displayMiB(limit)}.${suggestion}`,
    );
  }

  const form = new FormData();
  form.set("chat_id", String(request.chatId));
  if (request.messageThreadId !== undefined) {
    form.set("message_thread_id", String(request.messageThreadId));
  }
  form.set(kind, Bun.file(request.filePath), filename);
  const caption = formatTelegramCaption(redactSecrets(request.caption ?? ""), 1024, request.defaultRepo);
  if (caption) {
    form.set("caption", caption);
    form.set("parse_mode", "HTML");
  }
  if (request.replyMarkup) {
    form.set("reply_markup", JSON.stringify(request.replyMarkup));
  }

  const timeout = AbortSignal.timeout(120_000);
  const signal = request.signal ? AbortSignal.any([request.signal, timeout]) : timeout;
  const method = kind === "photo" ? "sendPhoto" : "sendDocument";
  const response = await fetch(`https://api.telegram.org/bot${request.token}/${method}`, {
    method: "POST",
    body: form,
    signal,
  });
  let data: TelegramSendMessageResponse;
  try {
    data = (await response.json()) as TelegramSendMessageResponse;
  } catch {
    throw new Error(`Telegram ${method} failed (${response.status}): the API returned an unreadable response.`);
  }
  if (!response.ok || !data.ok || !data.result) {
    const detail = data.description?.trim() || response.statusText || "unknown Telegram API error";
    throw new Error(`Telegram ${method} failed (${data.error_code ?? response.status}): ${detail}`);
  }
  return { kind, filename, sizeBytes: fileStat.size, response: data };
}
