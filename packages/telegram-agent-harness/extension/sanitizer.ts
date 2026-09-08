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

/**
 * Converts Markdown text into valid Telegram HTML with proper character escaping.
 *
 * Handles:
 * - Code blocks: ```lang\ncode\n``` -> <pre><code class="language-lang">...</code></pre>
 * - Inline code: `code` -> <code>...</code>
 * - Existing Telegram HTML tags preserved (<b>, <i>, <code>, <a>, etc.)
 * - HTML escaping of remaining special characters (<, >, &)
 * - Headers: # Header -> <b>Header</b>
 * - Bullets: - item or * item -> • item
 * - Links: [text](url) -> <a href="url">text</a>
 * - Bold: **text** or __text__ -> <b>text</b>
 * - Italic: *text* or _text_ -> <i>text</i>
 * - Strikethrough: ~~text~~ -> <s>text</s>
 */
export function markdownToTelegramHtml(markdown: string): string {
  if (!markdown) return "";

  const placeholders: string[] = [];
  function addPlaceholder(replacement: string): string {
    const key = `\x01PH_${placeholders.length}\x01`;
    placeholders.push(replacement);
    return key;
  }

  // 1. Code blocks: ```lang\ncode\n```
  let out = markdown.replace(/```([a-zA-Z0-9_-]*)\r?\n([\s\S]*?)```/g, (_m, lang, code) => {
    const escapedCode = escapeHtml(code.trimEnd());
    const attr = lang ? ` class="language-${escapeHtml(lang)}"` : "";
    return addPlaceholder(`<pre><code${attr}>${escapedCode}</code></pre>`);
  });

  // 2. Inline code: `code`
  out = out.replace(/`([^`\r\n]+)`/g, (_m, code) => {
    return addPlaceholder(`<code>${escapeHtml(code)}</code>`);
  });

  // 3. Existing valid Telegram HTML tags (preserve without double-escaping)
  const validTagRegex = /<\/?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|tg-spoiler|tg-emoji)(?:\s+[^>]*)*>|<a\s+(?:href="[^"]*"|href='[^']*')[^>]*>|<\/a>/gi;
  out = out.replace(validTagRegex, (match) => {
    return addPlaceholder(match);
  });

  // 4. Escape remaining HTML characters (<, >, &)
  out = escapeHtml(out);

  // 5. Headers: # Header -> <b>Header</b>
  out = out.replace(/^(#{1,6})\s+(.+)$/gm, (_m, _hashes, title) => {
    return `<b>${title.trim()}</b>`;
  });

  // 6. Bullets: - bullet or * bullet -> • bullet
  out = out.replace(/^(\s*)[-*]\s+(.+)$/gm, (_m, indent, text) => {
    return `${indent}• ${text}`;
  });

  // 7. Markdown links: [text](url)
  out = out.replace(/\[([^\]\r\n]+)\]\((https?:\/\/[^\s\)\"'>]+)\)/g, (_m, text, url) => {
    const safeUrl = url.replace(/&amp;/g, "&").replace(/&/g, "&amp;").replace(/"/g, "&quot;");
    return `<a href="${safeUrl}">${text}</a>`;
  });

  // 8. Bold: **text** or __text__
  out = out.replace(/\*\*([^*\r\n]+)\*\*/g, "<b>$1</b>");
  out = out.replace(/(^|[\s,.:;!?(])__([^_\r\n]+)__(?=[\s,.:;!?)]|$)/g, "$1<b>$2</b>");

  // 9. Italic: *text* (when not part of **) or _text_ (when not inside words)
  out = out.replace(/(^|[^\*])\*([^*\r\n]+)\*([^\*]|$)/g, "$1<i>$2</i>$3");
  out = out.replace(/(^|[\s,.:;!?(])_([^_\r\n]+)_(?=[\s,.:;!?)]|$)/g, "$1<i>$2</i>$3");

  // 10. Strikethrough: ~~text~~
  out = out.replace(/~~([^~\r\n]+)~~/g, "<s>$1</s>");

  // 11. Restore placeholders
  for (let i = 0; i < placeholders.length; i++) {
    const key = `\x01PH_${i}\x01`;
    out = out.replaceAll(key, placeholders[i]);
  }

  return out;
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
