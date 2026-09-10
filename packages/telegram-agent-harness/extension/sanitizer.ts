/**
 * sanitizer.ts — Outbound text sanitization, HTML escaping, secrets redaction,
 * token fingerprinting, and canonical Markdown-to-Telegram-HTML conversion.
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
 * Converts Markdown to Telegram-compatible HTML.
 * Preserves pre-existing valid HTML tags (such as <a href="...">, <b>, <blockquote>) without
 * double-escaping, auto-links bare URLs, issue/PR references, and commit SHAs, converts tables
 * to bullet lists, and safely escapes all literal user text characters (<, >, &).
 */
export function markdownToTelegramHtml(markdown: string, defaultRepo = "Bavariance/polysimulator"): string {
  if (!markdown) return "";

  const placeholders: string[] = [];
  function addPlaceholder(val: string): string {
    const idx = placeholders.length;
    const key = `\x01TGPH_${idx}\x01`;
    placeholders.push(val);
    return key;
  }

  // 1. Normalize line endings
  let text = markdown.replace(/\r\n/g, "\n");

  // 2. Code blocks: ```lang\ncode\n```
  text = text.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (_m, lang, code) => {
    const trimmedLang = lang.trim();
    const attr = trimmedLang ? ` class="language-${escapeHtml(trimmedLang)}"` : "";
    return addPlaceholder(`<pre><code${attr}>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`);
  });

  // 3. Inline code: `code`
  text = text.replace(/`([^`\n]+)`/g, (_m, code) => {
    return addPlaceholder(`<code>${escapeHtml(code)}</code>`);
  });

  // 4. Pre-existing <a> tags: preserve without double-escaping or re-linking
  text = text.replace(/<a\s+href=["']([^"']*)["'][^>]*>([\s\S]*?)<\/a>/gi, (_m, rawUrl, body) => {
    const safeUrl = rawUrl.replace(/&amp;/g, "&").replace(/&/g, "&amp;").replace(/"/g, "&quot;");
    return addPlaceholder(`<a href="${safeUrl}">${body}</a>`);
  });

  // 5. Pre-existing valid Telegram HTML tags: preserve tag delimiters
  const validTagsPattern = /<\/?(?:b|strong|i|em|u|ins|s|strike|del|code|pre|blockquote|tg-spoiler|tg-emoji)(?:\s+expandable)?(?:\s+class=["'][^"']*["'])*>/gi;
  text = text.replace(validTagsPattern, (match) => addPlaceholder(match));

  // 6. Markdown blockquotes: >> and > (before escapeHtml, with placeholder delimiters)
  text = convertBlockquotesToHtml(text, addPlaceholder);

  // 7. Escape remaining user text (<, >, &)
  text = escapeHtml(text);

  // 8. Tables: convert to bullet lines
  text = convertTablesToBullets(text);

  // 9. Headers: # Header -> <b>Header</b>
  text = text.replace(/^(#{1,6})\s+(.+)$/gm, (_m, _hashes, title) => `<b>${title.trim()}</b>`);

  // 10. Bullet lists: - item or * item or + item -> • item
  text = text.replace(/^(\s*)[-*+]\s+(.+)$/gm, "$1• $2");

  // 11. Markdown links: [text](url) - protect with placeholder so label and url are not double-linked
  text = text.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s\)\"'>]+)\)/g, (_m, label, url) => {
    const safeUrl = url.trim().replace(/&amp;/g, "&").replace(/&/g, "&amp;").replace(/"/g, "&quot;");
    return addPlaceholder(`<a href="${safeUrl}">${label}</a>`);
  });

  // 12. Bare URLs: https://... - protect with placeholder
  text = text.replace(/\bhttps?:\/\/[^\s<>"'\)]+/g, (token) => {
    const url = token.replace(/[.,;:!?)>]+$/, "");
    const trail = token.slice(url.length);
    const safeUrl = url.replace(/&amp;/g, "&").replace(/&/g, "&amp;").replace(/"/g, "&quot;");
    return addPlaceholder(`<a href="${safeUrl}">${url}</a>`) + trail;
  });

  // 13. Issue / PR and Commit References - protect with placeholder
  text = text.replace(/(?:([a-zA-Z0-9_.-]+(?:\/[a-zA-Z0-9_.-]+)?))?#(\d+)/g, (match, explicitRepo, num) => {
    const repo = explicitRepo || defaultRepo;
    if (repo && (repo.includes("/") || repo === defaultRepo)) {
      const url = `https://github.com/${repo}/issues/${num}`;
      return addPlaceholder(`<a href="${url}">${match}</a>`);
    }
    return match;
  });

  text = text.replace(/\b([0-9a-fA-F]{40})\b/g, (_match, sha) => {
    if (defaultRepo) {
      const url = `https://github.com/${defaultRepo}/commit/${sha}`;
      return addPlaceholder(`<a href="${url}"><code>${sha.slice(0, 8)}</code></a>`);
    }
    return addPlaceholder(`<code>${sha.slice(0, 8)}</code>`);
  });

  // 14. Inline formatting: bold, italic, strikethrough
  text = text.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  text = text.replace(/(^|[\s,.:;!?(])__([^_\n]+)__(?=[\s,.:;!?)]|$)/g, "$1<b>$2</b>");
  text = text.replace(/(^|[^\*])\*([^*\n\s](?:[^*\n]*[^*\n\s])?)\*(?=[^\*]|$)/g, "$1<i>$2</i>");
  text = text.replace(/(^|[\s,.:;!?(])_([^_\n\s](?:[^_\n]*[^_\n\s])?)_(?=[\s,.:;!?)]|$)/g, "$1<i>$2</i>");
  text = text.replace(/~~([^~\n]+)~~/g, "<s>$1</s>");

  // 15. Restore placeholders in reverse
  for (let i = placeholders.length - 1; i >= 0; i--) {
    const key = `\x01TGPH_${i}\x01`;
    text = text.replaceAll(key, placeholders[i]);
  }

  return text;
}

function convertTablesToBullets(src: string): string {
  const lines = src.split("\n");
  const out: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.includes("|") && (line.trim().startsWith("|") || line.trim().endsWith("|"))) {
      if (i + 1 < lines.length && /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$/.test(lines[i + 1])) {
        const headerCells = line.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
        i += 2;
        const tableRows: string[] = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
          const rowCells = lines[i].trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
          const parts: string[] = [];
          for (let hIdx = 0; hIdx < rowCells.length; hIdx++) {
            const cell = rowCells[hIdx];
            if (cell) {
              if (hIdx < headerCells.length && headerCells[hIdx]) {
                parts.push(`<b>${headerCells[hIdx]}:</b> ${cell}`);
              } else {
                parts.push(cell);
              }
            }
          }
          if (parts.length > 0) {
            tableRows.push("• " + parts.join(" | "));
          }
          i++;
        }
        out.push(...tableRows);
        continue;
      }
    }
    out.push(line);
    i++;
  }
  return out.join("\n");
}

function convertBlockquotesToHtml(src: string, addPlaceholder: (val: string) => string): string {
  const lines = src.split("\n");
  const out: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*>>\s*/.test(line) || /^\s*>\s*\[(?:expandable|!NOTE|!COLLAPSIBLE)\]/i.test(line)) {
      const bqLines: string[] = [];
      if (/^\s*>\s*\[(?:expandable|!NOTE|!COLLAPSIBLE)\]/i.test(line)) {
        i++;
      }
      while (i < lines.length && /^\s*>{1,2}\s*/.test(lines[i])) {
        bqLines.push(lines[i].replace(/^\s*>{1,2}\s?/, ""));
        i++;
      }
      const openTag = addPlaceholder("<blockquote expandable>");
      const closeTag = addPlaceholder("</blockquote>");
      out.push(`${openTag}\n${bqLines.join("\n")}\n${closeTag}`);
      continue;
    } else if (/^\s*>\s*/.test(line)) {
      const bqLines: string[] = [];
      while (i < lines.length && /^\s*>\s*/.test(lines[i]) && !/^\s*>>/.test(lines[i])) {
        bqLines.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      const openTag = addPlaceholder("<blockquote>");
      const closeTag = addPlaceholder("</blockquote>");
      out.push(`${openTag}\n${bqLines.join("\n")}\n${closeTag}`);
      continue;
    }
    out.push(line);
    i++;
  }
  return out.join("\n");
}

const BLOCK_TAGS: Record<string, true> = { blockquote: true, pre: true };

/**
 * Splits long HTML text into chunks that fit within Telegram's message limits (4096 max, 4000 safe).
 * Preserves HTML tags and ensures tags and blockquotes are never split brokenly or left unclosed.
 */
export function chunkMessage(text: string, maxChunkSize = 4000): string[] {
  if (!text) return [];
  if (text.length <= maxChunkSize) return [text];

  const chunks: string[] = [];
  let remaining = text;

  const tagRegex = /<\s*(\/)?\s*([a-zA-Z0-9_-]+)([^>]*)>/g;

  while (remaining.length > 0) {
    if (remaining.length <= maxChunkSize) {
      chunks.push(remaining);
      break;
    }

    let candidateEnd = maxChunkSize;

    // Ensure candidateEnd is not inside a tag <...>
    const lastLt = remaining.lastIndexOf("<", candidateEnd);
    const lastGt = remaining.lastIndexOf(">", candidateEnd);
    if (lastLt > lastGt) {
      candidateEnd = lastLt;
    }

    // Parse open tags up to candidateEnd
    const stack: Array<{ name: string; full: string }> = [];
    tagRegex.lastIndex = 0;
    let match: RegExpExecArray | null;
    const sub = remaining.slice(0, candidateEnd);
    while ((match = tagRegex.exec(sub)) !== null) {
      const isClose = Boolean(match[1]);
      const tagName = match[2].toLowerCase();
      const fullTag = match[0];
      if (isClose) {
        for (let i = stack.length - 1; i >= 0; i--) {
          if (stack[i].name === tagName) {
            stack.splice(i, 1);
            break;
          }
        }
      } else {
        if (!fullTag.endsWith("/>") && !["br", "hr", "img"].includes(tagName)) {
          stack.push({ name: tagName, full: fullTag });
        }
      }
    }

    // If there is an open blockquote or pre, see if we can split before it
    const hasOpenBlock = stack.some(t => Boolean(BLOCK_TAGS[t.name]));
    let chosenSplit = -1;

    if (hasOpenBlock) {
      let firstBlockIdx = -1;
      const currStack: string[] = [];
      tagRegex.lastIndex = 0;
      while ((match = tagRegex.exec(sub)) !== null) {
        const isClose = Boolean(match[1]);
        const tagName = match[2].toLowerCase();
        if (!isClose && BLOCK_TAGS[tagName]) {
          if (!currStack.some(t => BLOCK_TAGS[t])) {
            firstBlockIdx = match.index;
          }
          currStack.push(tagName);
        } else if (isClose && BLOCK_TAGS[tagName]) {
          if (currStack.length > 0 && currStack[currStack.length - 1] === tagName) {
            currStack.pop();
          }
        }
      }

      if (firstBlockIdx > Math.floor(maxChunkSize / 3)) {
        const splitCand = remaining.lastIndexOf("\n", firstBlockIdx);
        chosenSplit = splitCand > 0 ? splitCand : firstBlockIdx;
      }
    }

    if (chosenSplit <= 0) {
      for (const sep of ["\n\n", "\n", " "]) {
        const idx = remaining.lastIndexOf(sep, candidateEnd);
        if (idx > Math.floor(maxChunkSize / 3)) {
          const tLt = remaining.lastIndexOf("<", idx);
          const tGt = remaining.lastIndexOf(">", idx);
          if (tLt <= tGt) {
            chosenSplit = idx + (sep === "\n\n" ? 2 : 0);
            break;
          }
        }
      }
    }

    if (chosenSplit <= 0) {
      chosenSplit = candidateEnd;
    }

    // Determine open tags at chosenSplit
    const openTagsAtSplit: Array<{ name: string; full: string }> = [];
    tagRegex.lastIndex = 0;
    const splitSub = remaining.slice(0, chosenSplit);
    while ((match = tagRegex.exec(splitSub)) !== null) {
      const isClose = Boolean(match[1]);
      const tagName = match[2].toLowerCase();
      const fullTag = match[0];
      if (isClose) {
        for (let i = openTagsAtSplit.length - 1; i >= 0; i--) {
          if (openTagsAtSplit[i].name === tagName) {
            openTagsAtSplit.splice(i, 1);
            break;
          }
        }
      } else {
        if (!fullTag.endsWith("/>") && !["br", "hr", "img"].includes(tagName)) {
          openTagsAtSplit.push({ name: tagName, full: fullTag });
        }
      }
    }

    const closingTags = openTagsAtSplit.slice().reverse().map(t => `</${t.name}>`).join("");
    const reopeningTags = openTagsAtSplit.map(t => t.full).join("");

    chunks.push(remaining.slice(0, chosenSplit).trimEnd() + closingTags);
    remaining = reopeningTags + remaining.slice(chosenSplit).trimStart();
  }

  return chunks.filter(c => c.length > 0);
}

/**
 * Formats a caption for Telegram photo or media group uploads (1024 char limit).
 * Converts Markdown to Telegram HTML and truncates safely with balanced tags.
 */
export function formatTelegramCaption(caption: string, maxLen = 1024, defaultRepo = "Bavariance/polysimulator"): string {
  if (!caption) return "";
  const formatted = markdownToTelegramHtml(caption, defaultRepo);
  if (formatted.length <= maxLen) return formatted;
  const chunks = chunkMessage(formatted, maxLen);
  return chunks[0] || "";
}
