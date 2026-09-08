/**
 * sanitizer.test.ts — Unit tests for HTML escaping, secrets redaction,
 * token fingerprinting, and canonical Markdown-to-Telegram-HTML conversion.
 */
import { describe, expect, test } from "bun:test";
import {
  chunkMessage,
  escapeHtml,
  formatTelegramCaption,
  getTokenFingerprint,
  markdownToTelegramHtml,
  redactSecrets,
} from "../extension/sanitizer";

describe("Sanitizer & Security Utilities", () => {
  test("escapeHtml escapes HTML special characters properly", () => {
    expect(escapeHtml("Hello <world> & 'test'")).toBe("Hello &lt;world&gt; &amp; 'test'");
    expect(escapeHtml("")).toBe("");
    expect(escapeHtml("No special chars 123")).toBe("No special chars 123");
  });

  test("getTokenFingerprint produces deterministic zero-leak fingerprints without last-4 leakage", () => {
    const dummyBotId = "9876543210";
    const dummySecret = `test_secret_${crypto.randomUUID().replace(/-/g, "")}`;
    const rawToken = `${dummyBotId}:${dummySecret}`;

    const fp1 = getTokenFingerprint(rawToken);
    const fp2 = getTokenFingerprint(rawToken);

    expect(fp1.botId).toBe(dummyBotId);
    expect(fp1.fingerprint).toBe(fp2.fingerprint);
    expect(fp1.fingerprint.startsWith(`bot_${dummyBotId}_`)).toBe(true);
    expect(fp1.redacted).toBe(`bot_${dummyBotId}_***`);
    expect(fp1.redacted).not.toContain(dummySecret.slice(-4));
    expect(fp1.fingerprint).not.toContain(dummySecret);
  });

  test("redactSecrets masks API keys, bot tokens, and bearer credentials", () => {
    const dummySecret = crypto.randomUUID().replace(/-/g, "");
    const textWithSecrets = [
      "Here is the token: 123456789:ABCdefGHIjklMNOpqrsTUVwxyz1234567890",
      `live key: ps_live_${dummySecret}`,
      "openai key: sk-proj-1234567890abcdef1234567890",
      "resend key: re_1234567890abcdef12345678",
      "bearer: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    ].join("\n");

    const redacted = redactSecrets(textWithSecrets);
    expect(redacted).not.toContain(dummySecret);
    expect(redacted).not.toContain("sk-proj-1234567890abcdef1234567890");
    expect(redacted).not.toContain("ps_live_0123456789abcdef0123456789abcdef");
    expect(redacted).not.toContain("re_1234567890abcdef12345678");
    expect(redacted).toContain("[REDACTED_SECRET]");
  });

  test("chunkMessage splits text within max chunk size on natural boundaries", () => {
    const paragraph1 = "A".repeat(2000);
    const paragraph2 = "B".repeat(2500);
    const combined = `${paragraph1}\n\n${paragraph2}`;

    const chunks = chunkMessage(combined, 4000);
    expect(chunks.length).toBe(2);
    expect(chunks[0].length).toBeLessThanOrEqual(4000);
    expect(chunks[1].length).toBeLessThanOrEqual(4000);
    expect(chunks[0]).toBe(paragraph1);
    expect(chunks[1]).toBe(paragraph2);
  });

  test("markdownToTelegramHtml converts markdown formatting into valid Telegram HTML", () => {
    // Bold & strikethrough
    expect(markdownToTelegramHtml("**bold text** and ~~strikethrough~~")).toBe(
      "<b>bold text</b> and <s>strikethrough</s>",
    );

    // Inline code & code blocks
    expect(markdownToTelegramHtml("Run `git status`")).toBe(
      "Run <code>git status</code>",
    );
    expect(markdownToTelegramHtml("```ts\nconst a = 1 < 2 && 3 > 2;\n```")).toBe(
      '<pre><code class="language-ts">const a = 1 &lt; 2 &amp;&amp; 3 &gt; 2;</code></pre>',
    );

    // Links
    expect(markdownToTelegramHtml("See [#4478](https://github.com/Bavariance/polysimulator/pull/4478?tab=files&view=split)")).toBe(
      'See <a href="https://github.com/Bavariance/polysimulator/pull/4478?tab=files&amp;view=split">#4478</a>',
    );

    // Bullets and headers
    const mdList = "# Crash Summary\n- lane 1\n* lane 2";
    expect(markdownToTelegramHtml(mdList)).toBe(
      "<b>Crash Summary</b>\n• lane 1\n• lane 2",
    );

    // Underscores in identifiers (e.g. UV_ENOSPC) must not be corrupted to italics
    expect(markdownToTelegramHtml("failed with `UV_ENOSPC` and raw UV_ENOSPC write error")).toBe(
      "failed with <code>UV_ENOSPC</code> and raw UV_ENOSPC write error",
    );

    // HTML escaping of unformatted angle brackets and ampersands
    expect(markdownToTelegramHtml("0 bytes free -> failure & crash <unhandled>")).toBe(
      "0 bytes free -&gt; failure &amp; crash &lt;unhandled&gt;",
    );

    // Preserves existing valid Telegram HTML tags
    expect(markdownToTelegramHtml("Pre-existing <b>bold</b> and <code>sha</code>")).toBe(
      "Pre-existing <b>bold</b> and <code>sha</code>",
    );
  });

  test("requirement 3.1: exact payload from step 1 round-trips to clickable links without raw HTML tags", () => {
    const payloadStep1 = [
      "You're right, my last message had bare numbers. Corrected, and I'm fixing the notifier so this cannot happen again.",
      "",
      'Merge queue: <a href="https://github.com/Bavariance/polysimulator/pull/4799">#4799</a>, <a href="https://github.com/Bavariance/polysimulator/pull/4440">#4440</a>, <a href="https://github.com/Bavariance/polysimulator/pull/4486">#4486</a>, <a href="https://github.com/Bavariance/polysimulator/pull/4441">#4441</a>, <a href="https://github.com/Bavariance/polysimulator/pull/4652">#4652</a>, <a href="https://github.com/Bavariance/polysimulator/pull/4509">#4509</a>',
      'Reviews: <a href="https://github.com/Bavariance/polysimulator/pull/4611">#4611</a>, <a href="https://github.com/Bavariance/polysimulator/pull/4444">#4444</a>, <a href="https://github.com/Bavariance/polysimulator/pull/4452">#4452</a>',
      'Lint fix: <a href="https://github.com/Bavariance/polysimulator/pull/4793">#4793</a>',
      'Browser QA: outcome cards <a href="https://github.com/Bavariance/polysimulator/issues/3215">#3215</a>, motion feedback <a href="https://github.com/Bavariance/polysimulator/pull/4683">#4683</a>',
      'Merged earlier: <a href="https://github.com/Bavariance/polysimulator/pull/4448">#4448</a>, <a href="https://github.com/Bavariance/polysimulator/pull/4496">#4496</a>; revert <a href="https://github.com/Bavariance/polysimulator/pull/4800">#4800</a> closed <a href="https://github.com/Bavariance/polysimulator/issues/54">#54</a>',
      'Runtime fix: <a href="https://github.com/Wladefant/veyyon/pull/9">veyyon #9</a>; Telegram harness: <a href="https://github.com/Wladefant/super-board/pull/84">super-board #84</a>',
    ].join("\n");

    const result = markdownToTelegramHtml(payloadStep1);
    expect(result).not.toContain("&lt;a");
    expect(result).not.toContain("&lt;/a&gt;");
    expect(result).toContain('<a href="https://github.com/Bavariance/polysimulator/pull/4799">#4799</a>');
    expect(result).toContain('<a href="https://github.com/Wladefant/veyyon/pull/9">veyyon #9</a>');
    expect(result).toContain('<a href="https://github.com/Wladefant/super-board/pull/84">super-board #84</a>');
  });

  test("requirement 3.2: nested formatting inside blockquote", () => {
    const md = "> Quote with **bold text**, *italic*, `code`, and bare #4799";
    const result = markdownToTelegramHtml(md);
    expect(result).toContain("<blockquote>");
    expect(result).toContain("</blockquote>");
    expect(result).toContain("<b>bold text</b>");
    expect(result).toContain("<i>italic</i>");
    expect(result).toContain("<code>code</code>");
    expect(result).toContain('<a href="https://github.com/Bavariance/polysimulator/issues/4799">#4799</a>');
  });

  test("requirement 3.3: expandable blockquote", () => {
    const md = ">> Expandable quote with **important** note and #4440";
    const result = markdownToTelegramHtml(md);
    expect(result).toContain("<blockquote expandable>");
    expect(result).toContain("</blockquote>");
    expect(result).toContain("<b>important</b>");
    expect(result).toContain('<a href="https://github.com/Bavariance/polysimulator/issues/4440">#4440</a>');

    const htmlInput = "<blockquote expandable>\nExisting blockquote with **bold** and #4440\n</blockquote>";
    const resultHtml = markdownToTelegramHtml(htmlInput);
    expect(resultHtml).toContain("<blockquote expandable>");
    expect(resultHtml).toContain("</blockquote>");
    expect(resultHtml).toContain("<b>bold</b>");
  });

  test("requirement 3.4: formatTelegramCaption limits to 1024 chars safely with balanced tags", () => {
    const longCaption = "Header: **bold** and #4799. " + "Extra detailed note for media. ".repeat(60);
    const caption = formatTelegramCaption(longCaption, 1024);
    expect(caption.length).toBeLessThanOrEqual(1024);
    expect(caption).toContain("<b>bold</b>");
    expect(caption).toContain('<a href="https://github.com/Bavariance/polysimulator/issues/4799">#4799</a>');
    // Ensure tags are balanced
    const opensB = (caption.match(/<b>/g) || []).length;
    const closesB = (caption.match(/<\/b>/g) || []).length;
    expect(opensB).toBe(closesB);
  });

  test("requirement 3.5: message with literal '<', '&', '*' in user text", () => {
    const input = "If price < 100 & qty > 5: formula 2 * 3 = 6 (and **bold**)";
    const result = markdownToTelegramHtml(input);
    expect(result).toContain("price &lt; 100 &amp; qty &gt; 5:");
    expect(result).toContain("2 * 3 = 6");
    expect(result).toContain("<b>bold</b>");
  });

  test("requirement 3.6: 5000-char split preserves tags and does not split inside blockquotes", () => {
    const longBq = "Intro line.\n\n<blockquote expandable>\n" + "Repeated line with **bold** and #4799.\n".repeat(120) + "</blockquote>\n\nOutro line.";
    expect(longBq.length).toBeGreaterThan(4500);

    const html = markdownToTelegramHtml(longBq);
    const chunks = chunkMessage(html, 2000);
    expect(chunks.length).toBeGreaterThanOrEqual(2);

    for (const chunk of chunks) {
      expect(chunk.length).toBeLessThanOrEqual(2000);
      const opens = (chunk.match(/<blockquote[^>]*>/g) || []).length;
      const closes = (chunk.match(/<\/blockquote>/g) || []).length;
      expect(opens).toBe(closes);
    }
  });

  test("tables convert to bullet lists", () => {
    const mdTable = [
      "| Task | Owner | Status |",
      "| --- | --- | --- |",
      "| #4799 | wladefant | Merged |",
      "| #4440 | erik | Review |",
    ].join("\n");

    const result = markdownToTelegramHtml(mdTable);
    expect(result).toContain("• <b>Task:</b>");
    expect(result).toContain("<b>Owner:</b>");
    expect(result).toContain('<a href="https://github.com/Bavariance/polysimulator/issues/4799">#4799</a>');
    expect(result).toContain('<a href="https://github.com/Bavariance/polysimulator/issues/4440">#4440</a>');
  });
});
