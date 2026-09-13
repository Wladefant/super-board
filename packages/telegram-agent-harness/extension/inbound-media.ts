import * as fs from "node:fs/promises";
import * as path from "node:path";
import type { TelegramUpdate } from "./types";

export const MAX_MEDIA_BYTES = 20 * 1024 * 1024;
export interface InboundMedia { file_id: string; file_size?: number; mime_type: string; }
export function selectInboundMedia(message: TelegramUpdate["message"]): InboundMedia | null {
  if (message?.photo?.length) {
    const photo = message.photo.reduce((best, item) =>
      (item.width ?? 0) * (item.height ?? 0) >= (best.width ?? 0) * (best.height ?? 0) ? item : best);
    return { file_id: photo.file_id, file_size: photo.file_size, mime_type: "image/jpeg" };
  }
  if (message?.document) return { ...message.document, mime_type: message.document.mime_type ?? "application/octet-stream" };
  return null;
}

export async function downloadInboundMedia(token: string, media: InboundMedia, directory: string, updateId: number, signal?: AbortSignal): Promise<string> {
  const extensions: Record<string, string> = { "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif", "image/bmp": "bmp", "image/tiff": "tiff", "application/pdf": "pdf" };
  const ext = extensions[media.mime_type];
  if (!ext) throw new Error("Unsupported attachment: send an image or PDF.");
  if ((media.file_size ?? 0) > MAX_MEDIA_BYTES) throw new Error("Attachment exceeds the 20 MB limit.");
  const boundedSignal = signal ? AbortSignal.any([signal, AbortSignal.timeout(60000)]) : AbortSignal.timeout(60000);
  let temporary: string | undefined;
  try {
    const metadata = await fetch(`https://api.telegram.org/bot${token}/getFile`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ file_id: media.file_id }), signal: boundedSignal });
    const info = await metadata.json() as { ok?: boolean; result?: { file_path?: string; file_size?: number } };
    if (!metadata.ok || !info.ok || !info.result?.file_path) throw new Error();
    if ((info.result.file_size ?? 0) > MAX_MEDIA_BYTES) throw new Error();
    const remotePath = info.result.file_path;
    if (!/^[a-zA-Z0-9_./-]+$/.test(remotePath) || remotePath.split("/").includes("..")) throw new Error();
    const response = await fetch(`https://api.telegram.org/file/bot${token}/${remotePath}`, { signal: boundedSignal });
    if (!response.ok || !response.body || Number(response.headers.get("content-length") ?? 0) > MAX_MEDIA_BYTES) throw new Error();
    await fs.mkdir(directory, { recursive: true });
    const destination = path.resolve(directory, `${updateId}.${ext}`);
    temporary = `${destination}.${crypto.randomUUID()}.part`;
    const file = await fs.open(temporary, "wx");
    const reader = response.body.getReader();
    let size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_MEDIA_BYTES) throw new Error();
        let offset = 0;
        while (offset < value.byteLength) offset += (await file.write(value, offset, value.byteLength - offset)).bytesWritten;
      }
      if (!size) throw new Error();
    } finally { await reader.cancel().catch(() => {}); await file.close(); }
    await fs.rename(temporary, destination);
    return destination;
  } catch {
    if (temporary) await fs.unlink(temporary).catch(() => {});
    throw new Error("Attachment download failed or exceeded 20 MB. Please resend an image or PDF under 20 MB.");
  }
}
