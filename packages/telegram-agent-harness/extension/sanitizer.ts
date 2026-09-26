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
export const PROJECT_SLUG_MAP: Record<string, string> = {
  polysimulator: "Bavariance/polysimulator",
  polysim: "Bavariance/polysimulator",
  "super-board": "Wladefant/super-board",
  superboard: "Wladefant/super-board",
  veyyon: "Wladefant/veyyon",
  "codex-chatgpt-web": "Wladefant/codex-chatgpt-web",
};

export function resolveRepoSlug(nameOrSlug?: string): string | null {
  if (!nameOrSlug) return null;
  const trimmed = nameOrSlug.trim().replace(/[.,;:!?)>]+$/, "");
  if (trimmed.includes("/")) return trimmed;
  const lower = trimmed.toLowerCase();
  return PROJECT_SLUG_MAP[lower] || null;
}

export function extractMentionedRepos(text: string): Set<string> {
  const repos = new Set<string>();
  if (!text) return repos;
  // 1. Full GitHub URLs
  const urlMatches = text.matchAll(/https?:\/\/github\.com\/([a-zA-Z0-9_.-]+\/[a-zA-Z0-9_.-]+)/g);
  for (const m of urlMatches) {
    const slug = m[1].replace(/[.,;:!?)>]+$/, "");
    if (slug.includes("/")) repos.add(slug);
  }
  // 2. Explicit owner/repo#N
  const explicitMatches = text.matchAll(/\b([a-zA-Z0-9_.-]+\/[a-zA-Z0-9_.-]+)#\d+/g);
  for (const m of explicitMatches) {
    repos.add(m[1]);
  }
  // 3. Known project names or shorthand slugs
  for (const [name, slug] of Object.entries(PROJECT_SLUG_MAP)) {
    const re = new RegExp(`\\b${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\b`, "i");
    if (re.test(text)) {
      repos.add(slug);
    }
  }
  return repos;
}
/**
 * Converts Markdown to Telegram-compatible HTML.
 * Preserves pre-existing valid HTML tags (such as <a href="...">, <b>, <blockquote>) without
 * double-escaping, auto-links bare URLs, issue/PR references, and commit SHAs, renders tables
 * as monospace <pre> blocks (Telegram has no table entity), folds long quotes and <details>
 * into expandable blockquotes, and safely escapes all literal user text characters (<, >, &).
 *
 * `defaultRepo` is the session's own repository. It is required for a bare `#N` to link at all:
 * when the session's repository cannot be resolved the reference stays unlinked rather than
 * pointing at an unrelated project.
 */
export function markdownToTelegramHtml(markdown: string, defaultRepo?: string): string {
  if (!markdown) return "";

  const placeholders: string[] = [];
  function addPlaceholder(val: string): string {
    const idx = placeholders.length;
    const key = `\x01TGPH_${idx}\x01`;
    placeholders.push(val);
    return key;
  }
  const allRepos = extractMentionedRepos(markdown);
  const projectRepo = resolveRepoSlug(defaultRepo);
  if (projectRepo) {
    allRepos.add(projectRepo);
  }

  // 1. Normalize line endings
  let text = markdown.replace(/\r\n/g, "\n");

  // 2. Code blocks: ```lang\ncode\n```
  text = text.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (_m, lang, code) => {
    const trimmedLang = lang.trim();
    const attr = trimmedLang ? ` class="language-${escapeHtml(trimmedLang)}"` : "";
    return addPlaceholder(`<pre><code${attr}>${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`);
  });

  // 2b. Tables: Telegram has no table entity, so render aligned monospace <pre> blocks
  text = convertTablesToPre(text, addPlaceholder);

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

  // Generated command/help HTML already escapes its text. Keep valid entities
  // encoded once; restoring them never turns encoded markup into active tags.
  text = text.replace(/&(?:amp|lt|gt|quot|#\d+|#x[0-9a-f]+);/gi, entity => addPlaceholder(entity));

  // 7. Escape remaining user text (<, >, &)
  text = escapeHtml(text);

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
  text = text.replace(/(?:([a-zA-Z0-9_.-]+(?:\/[a-zA-Z0-9_.-]+)?))?#(\d+)/g, (match, explicitRepo, num, offset, fullText) => {
    // 1. Explicit qualifier attached to token (e.g. owner/repo#N or super-board#N)
    if (explicitRepo) {
      const targetRepo = explicitRepo.includes("/") ? explicitRepo : resolveRepoSlug(explicitRepo);
      if (targetRepo) {
        const textBefore = fullText.slice(0, offset);
        const isPr = /\b(?:PRs?|pull\s*requests?|pulls?)\s*:?\s*$/i.test(textBefore);
        const kind = isPr ? "pull" : "issues";
        return addPlaceholder(`<a href="https://github.com/${targetRepo}/${kind}/${num}">${match}</a>`);
      }
      return match;
    }

    // Bare '#number'
    const lineStart = fullText.lastIndexOf("\n", offset) + 1;
    let lineEnd = fullText.indexOf("\n", offset + match.length);
    if (lineEnd === -1) lineEnd = fullText.length;

    const beforeOnLine = fullText.slice(lineStart, offset);
    const afterOnLine = fullText.slice(offset + match.length, lineEnd);

    // 2. Repo named in the same sentence/line
    // 2a. Preceding repo name on same line: e.g. "super-board #121", "polysimulator PR #5157"
    const mPre = beforeOnLine.match(/\b([a-zA-Z0-9_.-]+(?:\/[a-zA-Z0-9_.-]+)?)\s*(?:\||:)?\s*(?:(?:PRs?|pull\s*requests?|pulls?|issues?)\s*:?\s*)?$/i);
    if (mPre) {
      const cand = mPre[1];
      const resolved = resolveRepoSlug(cand) || (cand.includes("/") ? cand : null);
      if (resolved) {
        const isPr = /\b(?:PRs?|pull\s*requests?|pulls?)\b/i.test(beforeOnLine);
        const kind = isPr ? "pull" : "issues";
        return addPlaceholder(`<a href="https://github.com/${resolved}/${kind}/${num}">${match}</a>`);
      }
    }

    // 2b. Following repo name on same line: e.g. "#5157 (polysimulator)", "#5157 in polysimulator"
    const mPost = afterOnLine.match(/^\s*(?:\(([^)]+)\)|(?:in|of|for)\s+([a-zA-Z0-9_.-]+(?:\/[a-zA-Z0-9_.-]+)?))/i);
    if (mPost) {
      const rawCand = (mPost[1] || mPost[2]).trim();
      const cand = rawCand.split(/\s+/)[0];
      const resolved = resolveRepoSlug(cand) || (cand.includes("/") ? cand : null);
      if (resolved) {
        const isPr = /\b(?:PRs?|pull\s*requests?|pulls?)\b/i.test(beforeOnLine + " " + rawCand);
        const kind = isPr ? "pull" : "issues";
        return addPlaceholder(`<a href="https://github.com/${resolved}/${kind}/${num}">${match}</a>`);
      }
    }

    // 3. Single configured project repo when NO other repo appears in the message
    if (allRepos.size > 1) {
      // Ambiguous: multiple repos appear in the message! Never guess.
      return match;
    }

    if (allRepos.size === 1 && projectRepo && allRepos.has(projectRepo)) {
      const isPr = /\b(?:PRs?|pull\s*requests?|pulls?)\s*:?\s*(?:#\d+[\s,;]*)*$/i.test(beforeOnLine);
      const kind = isPr ? "pull" : "issues";
      return addPlaceholder(`<a href="https://github.com/${projectRepo}/${kind}/${num}">${match}</a>`);
    }

    return match;
  });

  text = text.replace(/\b([0-9a-fA-F]{40})\b/g, (_match, sha) => {
    if (projectRepo) {
      const url = `https://github.com/${projectRepo}/commit/${sha}`;
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

/** GFM separator row (`|---|:--:|`); the lookahead insists on a pipe so a lone `---` rule is not a table. */
const TABLE_SEPARATOR = /^(?=.*\|)\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function renderTable(rows: string[][]): string {
  const columns = Math.max(...rows.map(row => row.length));
  const widths = Array.from({ length: columns }, (_, col) =>
    Math.max(1, ...rows.map(row => [...(row[col] ?? "")].length)));
  const format = (row: string[]) =>
    widths.map((width, col) => {
      const cell = row[col] ?? "";
      return cell + " ".repeat(width - [...cell].length);
    }).join(" | ").trimEnd();
  const [header, ...body] = rows;
  return [format(header), widths.map(width => "-".repeat(width)).join("-+-"), ...body.map(format)].join("\n");
}

/**
 * Links and issue/PR references inside a table: `[label](url)`, bare URLs, `owner/repo#N`,
 * a known short slug, or a bare `#N`. A quote is excluded from every URL body, since the
 * later link pass cannot carry one and would leave the raw Markdown on the reference line.
 */
const TABLE_REFERENCE = /\[[^\]\n]+\]\(https?:\/\/[^\s)"'>]+\)|https?:\/\/[^\s|)"'>]+|(?:[A-Za-z0-9_.-]+\/)?[A-Za-z0-9_.-]*#\d+/g;

/**
 * A table cell reduced to its literal text. Inline code keeps its content verbatim, so a cell
 * such as `` `<i>` `` keeps `<i>` instead of losing it to the tag strip, and formatting markers
 * outside code are dropped because a Telegram `<pre>` block cannot carry other entities. An
 * entity the source already escaped is decoded once here, so the single escape pass over the
 * finished block cannot escape it a second time.
 */
const DECODED_ENTITIES: Record<string, string> = {
  "&amp;": "&",
  "&lt;": "<",
  "&gt;": ">",
  "&quot;": '"',
  "&apos;": "'",
};

function tableCellText(cell: string): string {
  return cell
    .trim()
    .replace(/\\\|/g, "|")
    .split(/(`[^`]*`)/g)
    .map((part, index) => index % 2 === 1
      ? part.slice(1, -1)
      : part
        .replace(/\[([^\]\n]+)\]\([^)\s]+\)/g, "$1")
        .replace(/\*\*([^*]+)\*\*/g, "$1")
        .replace(/__([^_]+)__/g, "$1")
        .replace(/~~([^~]+)~~/g, "$1")
        .replace(/<\/?(?:b|strong|i|em|u|s|code)>/gi, ""))
    .join("")
    .replace(/&(?:amp|lt|gt|quot|apos);/gi, entity => DECODED_ENTITIES[entity.toLowerCase()] ?? entity);
}

/**
 * The reference tokens a table carries for the line below its block: `[label](url)`, bare URLs,
 * `owner/repo#N`, a known short slug, or a bare `#N`. A `word#N` whose word names no known
 * project keeps only its `#N`, so a cell such as `Lane#3` is not listed as a reference the
 * link pass would refuse. A URL the pattern stopped short of is dropped rather than listed
 * as its prefix, which would link somewhere the cell never named.
 */
function tableReferences(rows: string[]): string[] {
  const source = rows.join("\n");
  const tokens = new Set<string>();
  for (const match of source.matchAll(TABLE_REFERENCE)) {
    if (source[match.index + match[0].length] === '"') continue;
    const token = match[0];
    const shortSlug = /^([A-Za-z0-9_.-]+)#(\d+)$/.exec(token);
    tokens.add(shortSlug && !PROJECT_SLUG_MAP[shortSlug[1].toLowerCase()] ? `#${shortSlug[2]}` : token);
  }
  return [...tokens];
}

/**
 * Replaces GitHub-flavoured Markdown tables (header row, separator row, body rows) with
 * an aligned <pre> block. Runs on raw Markdown after fenced code is protected, so the
 * cell text is escaped exactly once. The table's links and issue/PR references follow on
 * one line below it, where the later passes make them clickable.
 */
function convertTablesToPre(src: string, addPlaceholder: (val: string) => string): string {
  const lines = src.split("\n");
  const out: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.includes("|") && i + 1 < lines.length && TABLE_SEPARATOR.test(lines[i + 1])) {
      const rowLines = [line];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
        if (!TABLE_SEPARATOR.test(lines[i])) rowLines.push(lines[i]);
        i++;
      }
      const rows = rowLines.map(row =>
        row
          .trim()
          .replace(/^\|/, "")
          .replace(/(?<!\\)\|$/, "")
          .split(/(?<!\\)\|/)
          .map(cell => tableCellText(cell)));
      out.push(addPlaceholder(`<pre>${escapeHtml(renderTable(rows))}</pre>`));
      const references = tableReferences(rowLines);
      if (references.length > 0) out.push(references.join(" · "));
      continue;
    }
    out.push(line);
    i++;
  }
  return out.join("\n");
}

/** A plain quote longer than either bound is folded into an expandable blockquote. */
const EXPANDABLE_QUOTE_LINES = 4;
const EXPANDABLE_QUOTE_CHARS = 400;
const EXPANDABLE_MARKER = /^\s*>\s*\[(?:expandable|!NOTE|!COLLAPSIBLE|!DETAILS)\]/i;

function convertBlockquotesToHtml(src: string, addPlaceholder: (val: string) => string): string {
  // <details><summary>S</summary>body</details> -> expandable quote led by the bold summary
  src = src.replace(/<details\b[^>]*>\s*(?:<summary>([\s\S]*?)<\/summary>)?([\s\S]*?)<\/details>/gi, (_m, summary: string | undefined, body: string) => {
    const title = summary?.trim();
    const lines = body.trim().split("\n");
    return (title ? [`**${title}**`, ...lines] : lines).map(l => `>> ${l}`).join("\n");
  });

  const lines = src.split("\n");
  const out: string[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*>>/.test(line) || EXPANDABLE_MARKER.test(line)) {
      const bqLines: string[] = [];
      if (EXPANDABLE_MARKER.test(line)) i++;
      while (i < lines.length && /^\s*>{1,2}/.test(lines[i])) {
        bqLines.push(lines[i].replace(/^\s*>{1,2}\s?/, ""));
        i++;
      }
      out.push(`${addPlaceholder("<blockquote expandable>")}\n${bqLines.join("\n")}\n${addPlaceholder("</blockquote>")}`);
      continue;
    }
    if (/^\s*>/.test(line)) {
      const bqLines: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i]) && !/^\s*>>/.test(lines[i])) {
        bqLines.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      const body = bqLines.join("\n");
      const expandable = bqLines.length > EXPANDABLE_QUOTE_LINES || body.length > EXPANDABLE_QUOTE_CHARS;
      out.push(`${addPlaceholder(expandable ? "<blockquote expandable>" : "<blockquote>")}\n${body}\n${addPlaceholder("</blockquote>")}`);
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
export function formatTelegramCaption(caption: string, maxLen = 1024, defaultRepo?: string): string {
  if (!caption) return "";
  const formatted = markdownToTelegramHtml(caption, defaultRepo);
  if (formatted.length <= maxLen) return formatted;
  const chunks = chunkMessage(formatted, maxLen);
  return chunks[0] || "";
}

/**
 * Comparable form of a Telegram delivery: markup, entities, URLs and punctuation removed,
 * leaving lowercase words separated by single spaces. The final reply and a
 * telegram_message carry the same prose, so equal content normalizes to equal strings.
 */
export function normalizeForDedupe(text: string): string {
  if (!text) return "";
  return text
    .replace(/<[^>]+>/g, " ")
    .replace(/&(?:[a-z]+|#\d+|#x[0-9a-f]+);/gi, " ")
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim()
    .toLowerCase();
}

/** A contained passage shorter than this is too generic ("done", "merged") to call a repeat. */
const REPEAT_MIN_CONTAINED_CHARS = 20;

/**
 * True when `candidate` is the same delivery as `earlier`: the identical text after markup,
 * entities, URLs and punctuation are normalized away, or a passage of it long enough to be
 * the same prose. Matching is exact, never a similarity score: a reply that changes one status
 * word or one issue number is new output and must reach the operator, so the cost of an
 * occasional repeated line is preferred to the cost of a swallowed answer.
 *
 * Containment is word-bounded. Normalization leaves exactly one space between words and trims
 * the ends, so padding both sides makes `includes` match whole words only: "now 12" no longer
 * swallows "now 1", and "pr 1224 merged" no longer swallows "224 merged".
 */
export function isRepeatDelivery(candidate: string, earlier: string): boolean {
  const next = normalizeForDedupe(candidate);
  const prior = normalizeForDedupe(earlier);
  if (!next || !prior) return false;
  if (next === prior) return true;
  return next.length >= REPEAT_MIN_CONTAINED_CHARS && ` ${prior} `.includes(` ${next} `);
}
