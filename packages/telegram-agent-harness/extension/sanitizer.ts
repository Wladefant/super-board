/**
 * sanitizer.ts — Outbound text sanitization, HTML escaping, secrets redaction, and token fingerprinting.
 */
import { createHmac } from "node:crypto";

export interface TokenFingerprint {
  botId: string;
  fingerprint: string;
  redacted: string;
}

/**
 * Derives safe, non-leaking identifiers from a Telegram Bot token.
 * Canonical format: <bot_id>:<secret_hash>
 */
export function getTokenFingerprint(token: string, salt = "veyyon-pool-v1"): TokenFingerprint {
  const clean = token.trim();
  const parts = clean.split(":");
  const botId = parts.length >= 2 && /^\d+$/.test(parts[0]) ? parts[0] : "unknown";

  const hmac = createHmac("sha256", salt);
  hmac.update(clean);
  const digest = hmac.digest("hex");
  const fingerprint = `bot_${botId}_${digest.slice(0, 12)}`;
  const redacted = `bot_${botId}_***`;

  return { botId, fingerprint, redacted };
}

/**
 * Escapes characters for Telegram HTML parse mode (<, >, &).
 */
export function escapeHtml(text: string): string {
  if (!text) return "";
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

const SENSITIVE_PATTERNS: RegExp[] = [
  // Telegram Bot tokens
  /\b\d{8,11}:[A-Za-z0-9_-]{35}\b/g,
  // Polymarket/Veyyon live API keys
  /\bps_live_[a-f0-9]{32,64}\b/gi,
  // OpenAI / Anthropic / general API keys
  /\bsk-[A-Za-z0-9_-]{20,}\b/g,
  // Resend keys
  /\bre_[A-Za-z0-9_-]{24,}\b/g,
  // Generic Bearer tokens
  /Bearer\s+[A-Za-z0-9._~+/-]{20,}/gi,
  // Password / Secret in json / env assignments
  /(?:api[_-]?key|secret|password|token|auth)\s*[:=]\s*["']?([A-Za-z0-9._~+/-]{16,})["']?/gi,
];

/**
 * Redacts known sensitive patterns from logs and outbound Telegram messages.
 */
export function redactSecrets(text: string): string {
  if (!text) return "";
  let result = text;
  for (const pattern of SENSITIVE_PATTERNS) {
    result = result.replace(pattern, (match, captured) => {
      if (captured && match.includes(captured)) {
        return match.replace(captured, "[REDACTED_SECRET]");
      }
      return "[REDACTED_SECRET]";
    });
  }
  return result;
}

/**
 * Splits long text into chunks that fit within Telegram's message limits (4096 max, 4000 safe).
 */
export function chunkMessage(text: string, maxChunkSize = 4000): string[] {
  if (!text) return [];
  if (text.length <= maxChunkSize) return [text];

  const chunks: string[] = [];
  let remaining = text;

  while (remaining.length > 0) {
    if (remaining.length <= maxChunkSize) {
      chunks.push(remaining);
      break;
    }

    // Try splitting on paragraph boundary
    let splitIdx = remaining.lastIndexOf("\n\n", maxChunkSize);
    if (splitIdx < maxChunkSize / 2) {
      // Try splitting on single newline
      splitIdx = remaining.lastIndexOf("\n", maxChunkSize);
    }
    if (splitIdx < maxChunkSize / 2) {
      // Try splitting on space
      splitIdx = remaining.lastIndexOf(" ", maxChunkSize);
    }
    if (splitIdx <= 0) {
      // Hard split
      splitIdx = maxChunkSize;
    }

    chunks.push(remaining.slice(0, splitIdx).trimEnd());
    remaining = remaining.slice(splitIdx).trimStart();
  }

  return chunks.filter(c => c.length > 0);
}
