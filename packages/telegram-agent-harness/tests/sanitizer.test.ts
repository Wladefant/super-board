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
  isRepeatDelivery,
  redactSecrets,
} from "../extension/sanitizer";

describe("Sanitizer & Security Utilities", () => {
  test("escapeHtml escapes HTML special characters properly", () => {
    expect(escapeHtml("Hello <world> & 'test'")).toBe("Hello &lt;world&gt; &amp; 'test'");
    expect(escapeHtml("")).toBe("");
    expect(escapeHtml("No special chars 123")).toBe("No special chars 123");
  });

  test("generated help entities render once without activating encoded markup", () => {
    expect(markdownToTelegramHtml("<b>Resource &amp; quota</b>\n<code>/steer &lt;text&gt;</code>"))
      .toBe("<b>Resource &amp; quota</b>\n<code>/steer &lt;text&gt;</code>");
    expect(markdownToTelegramHtml("&lt;script&gt;alert(1)&lt;/script&gt; & raw"))
      .toBe("&lt;script&gt;alert(1)&lt;/script&gt; &amp; raw");
    expect(markdownToTelegramHtml("`&lt;literal&gt;`")).toBe("<code>&amp;lt;literal&amp;gt;</code>");
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
      `Telegram token: 123456789:${dummySecret.padEnd(35, "a")}`,
      "OpenAI key: sk-proj-1234567890abcdef1234567890",
      "Polysim key: ps_live_0123456789abcdef0123456789abcdef",
      "Resend key: re_1234567890abcdef12345678",
      "Auth header: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
      "Here is the token: 123456789:ABCdefGHIjklMNOpqrsTUVwxyz1234567890",
      `live key: ps_live_${dummySecret}`,
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
    const result = markdownToTelegramHtml(md, "Bavariance/polysimulator");
    expect(result).toContain("<blockquote>");
    expect(result).toContain("</blockquote>");
    expect(result).toContain("<b>bold text</b>");
    expect(result).toContain("<i>italic</i>");
    expect(result).toContain("<code>code</code>");
    expect(result).toContain('<a href="https://github.com/Bavariance/polysimulator/issues/4799">#4799</a>');
  });

  test("requirement 3.3: expandable blockquote", () => {
    const md = ">> Expandable quote with **important** note and #4440";
    const result = markdownToTelegramHtml(md, "Bavariance/polysimulator");
    expect(result).toContain("<blockquote expandable>");
    expect(result).toContain("</blockquote>");
    expect(result).toContain("<b>important</b>");
    expect(result).toContain('<a href="https://github.com/Bavariance/polysimulator/issues/4440">#4440</a>');

    const htmlInput = "<blockquote expandable>\nExisting blockquote with **bold** and #4440\n</blockquote>";
    const resultHtml = markdownToTelegramHtml(htmlInput, "Bavariance/polysimulator");
    expect(resultHtml).toContain("<blockquote expandable>");
    expect(resultHtml).toContain("</blockquote>");
    expect(resultHtml).toContain("<b>bold</b>");
  });

  test("requirement 3.4: formatTelegramCaption limits to 1024 chars safely with balanced tags", () => {
    const longCaption = "Header: **bold** and #4799. " + "Extra detailed note for media. ".repeat(60);
    const caption = formatTelegramCaption(longCaption, 1024, "Bavariance/polysimulator");
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

    const html = markdownToTelegramHtml(longBq, "Bavariance/polysimulator");
    const chunks = chunkMessage(html, 2000);
    expect(chunks.length).toBeGreaterThanOrEqual(2);

    for (const chunk of chunks) {
      expect(chunk.length).toBeLessThanOrEqual(2000);
      const opens = (chunk.match(/<blockquote[^>]*>/g) || []).length;
      const closes = (chunk.match(/<\/blockquote>/g) || []).length;
      expect(opens).toBe(closes);
    }
  });

  test("tables render as an aligned monospace block with their references linked below", () => {
    const mdTable = [
      "| Task | Owner | Status |",
      "| --- | --- | --- |",
      "| #4799 | **wladefant** | Merged |",
      "| #4440 | erik | [Review](https://example.com/r?a=1&b=2) |",
    ].join("\n");

    const result = markdownToTelegramHtml(mdTable, "Bavariance/polysimulator");
    expect(result).toContain([
      "<pre>Task  | Owner     | Status",
      "------+-----------+-------",
      "#4799 | wladefant | Merged",
      "#4440 | erik      | Review</pre>",
    ].join("\n"));
    expect(result).not.toContain("<b>");
    expect(result).toContain('<a href="https://github.com/Bavariance/polysimulator/issues/4799">#4799</a>');
    expect(result).toContain('<a href="https://github.com/Bavariance/polysimulator/issues/4440">#4440</a>');
    expect(result).toContain('<a href="https://example.com/r?a=1&amp;b=2">Review</a>');
  });

  test("a table reference keeps its short-slug qualifier", () => {
    const table = [
      "| Task | Status |",
      "| --- | --- |",
      "| polysimulator#9 | Open |",
      "| Lane#3 | Done |",
    ].join("\n");

    const result = markdownToTelegramHtml(table);
    // The short slug survives, so the reference does not lose its repository.
    const referenceLine = result.split("\n").at(-1) ?? "";
    expect(referenceLine).toContain('<a href="https://github.com/Bavariance/polysimulator/issues/9">polysimulator#9</a>');
    // A word that names no project is not a qualifier: that reference stays a bare #N.
    expect(referenceLine).toBe('<a href="https://github.com/Bavariance/polysimulator/issues/9">polysimulator#9</a> · #3');
  });

  test("table cells escape HTML once, keep inline code, and a lone horizontal rule is not a table", () => {
    const result = markdownToTelegramHtml("| a<b | c&d |\n| --- | --- |\n| x | y |");
    expect(result).toContain("<pre>a&lt;b | c&amp;d\n----+----\nx   | y</pre>");

    // Inline code is content: `<i>` stays visible instead of being deleted as a tag, and an
    // entity the source already escaped is not escaped a second time.
    const literal = markdownToTelegramHtml("| cell |\n| --- |\n| `<i>` &amp; done |");
    expect(literal).toContain("&lt;i&gt; &amp; done</pre>");

    const rule = markdownToTelegramHtml("before | after\n---\ntext");
    expect(rule).not.toContain("<pre>");
  });

  test("a cell URL the pattern cannot carry is not listed as its truncated prefix", () => {
    // An unencoded quote inside the URL stops the reference pattern mid-address. Listing what
    // it did match would link somewhere the cell never named, so the reference is dropped.
    const result = markdownToTelegramHtml('| cell |\n| --- |\n| [l](https://example.com/a"2") |');
    expect(result).toBe("<pre>cell\n----\nl</pre>");
  });

  test("long quotes and <details> fold into expandable blockquotes; short quotes stay plain", () => {
    expect(markdownToTelegramHtml("> short quote")).toBe("<blockquote>\nshort quote\n</blockquote>");

    const long = Array.from({ length: 6 }, (_, i) => `> line ${i + 1}`).join("\n");
    expect(markdownToTelegramHtml(long)).toStartWith("<blockquote expandable>\nline 1");

    const details = markdownToTelegramHtml("<details><summary>Evidence</summary>\nrun 1 passed\nrun 2 passed\n</details>");
    expect(details).toBe("<blockquote expandable>\n<b>Evidence</b>\nrun 1 passed\nrun 2 passed\n</blockquote>");
  });

  test("owner/repo#N links to that repo and bare #N links to the session repo passed in", () => {
    const qualified = markdownToTelegramHtml("Merged Wladefant/veyyon#19.", "Wladefant/super-board");
    expect(qualified).toContain('<a href="https://github.com/Wladefant/veyyon/issues/19">Wladefant/veyyon#19</a>');

    const bare = markdownToTelegramHtml("Merged PR #224 and PR #225.", "Wladefant/super-board");
    expect(bare).toContain('<a href="https://github.com/Wladefant/super-board/pull/224">#224</a>');
    expect(bare).toContain('<a href="https://github.com/Wladefant/super-board/pull/225">#225</a>');

    // A second repository in the message makes a bare #N ambiguous, so it is left unlinked.
    const mixed = markdownToTelegramHtml("Merged Wladefant/veyyon#19 and PR #224.", "Wladefant/super-board");
    expect(mixed).toContain("and PR #224.");
  });

  test("a delivery repeats an earlier one when it is the same text or a passage of it", () => {
    const earlier = "**Merged** [#224](https://github.com/Wladefant/super-board/pull/224): Telegram tables now render as monospace blocks.";
    expect(isRepeatDelivery("Merged #224: Telegram tables now render as monospace blocks.", earlier)).toBe(true);
    expect(isRepeatDelivery("Telegram tables now render as monospace blocks.", earlier)).toBe(true);
  });

  test("a delivery that changes a status word or an issue number is not a repeat", () => {
    // Long enough that word overlap alone would call these the same delivery.
    const running = "Checks on the order flow are still running and the ledger entries have not been verified yet, so the balances for the staging wallet remain unconfirmed today.";
    const failed = "Checks on the order flow failed and the ledger entries have not been verified yet, so the balances for the staging wallet remain unconfirmed today.";
    expect(isRepeatDelivery(failed, running)).toBe(false);
    expect(isRepeatDelivery(running, running)).toBe(true);

    const merged224 = "Merged PR 224 into staging after CI went green on every check across all three operating systems today.";
    const merged225 = "Merged PR 225 into staging after CI went green on every check across all three operating systems today.";
    expect(isRepeatDelivery(merged225, merged224)).toBe(false);
    expect(isRepeatDelivery(merged224, merged225)).toBe(false);
  });

  test("a delivery that adds material, or a short generic one, is not a repeat", () => {
    const earlier = "Merged #224: Telegram tables now render as monospace blocks.";
    const extended = `${earlier}\n\nNext: CI for #225 is red on the lint step; fixing the import order before re-running.`;
    expect(isRepeatDelivery(extended, earlier)).toBe(false);
    expect(isRepeatDelivery("Merged", earlier)).toBe(false);
    expect(isRepeatDelivery("", earlier)).toBe(false);
  });

  test("a bare #N stays unlinked when the session repository is unknown", () => {
    expect(markdownToTelegramHtml("Merged #224.")).toBe("Merged #224.");
  });
  test("issue/PR linkification adheres to multi-repo disambiguation rules", () => {
    // 1. Single repo context auto-links bare #N
    const singleRepoText = "Fixed issue #123 in current build.";
    const singleResult = markdownToTelegramHtml(singleRepoText, "Wladefant/super-board");
    expect(singleResult).toContain('<a href="https://github.com/Wladefant/super-board/issues/123">#123</a>');

    // 2. Multi-repo context: bare #N must remain plain text
    const multiRepoText = [
      "Header: https://github.com/Wladefant/veyyon/issues/19",
      "PRs in progress: #5157, #5071",
    ].join("\n");
    const multiResult = markdownToTelegramHtml(multiRepoText, "Bavariance/polysimulator");
    // Bare #5157 and #5071 must NOT be linked to veyyon or polysimulator because context has multiple repos
    expect(multiResult).not.toContain("https://github.com/Wladefant/veyyon/issues/5157");
    expect(multiResult).not.toContain("https://github.com/Bavariance/polysimulator/issues/5157");
    expect(multiResult).toContain("PRs in progress: #5157, #5071");

    // 3. Explicit qualifier owner/repo#N
    const explicitText = "Check Bavariance/polysimulator#5157 and Wladefant/super-board#121";
    const explicitResult = markdownToTelegramHtml(explicitText);
    expect(explicitResult).toContain('<a href="https://github.com/Bavariance/polysimulator/issues/5157">Bavariance/polysimulator#5157</a>');
    expect(explicitResult).toContain('<a href="https://github.com/Wladefant/super-board/issues/121">Wladefant/super-board#121</a>');

    // 4. Same-line repo qualifier (preceding or following)
    const sameLineText = [
      "Fixed in super-board #121 yesterday.",
      "Reviewing polysimulator PR #5157 right now.",
      "Tracked in #5071 (polysimulator).",
    ].join("\n");
    const sameLineResult = markdownToTelegramHtml(sameLineText, "Wladefant/veyyon");
    expect(sameLineResult).toContain('<a href="https://github.com/Wladefant/super-board/issues/121">#121</a>');
    expect(sameLineResult).toContain('<a href="https://github.com/Bavariance/polysimulator/pull/5157">#5157</a>');
    expect(sameLineResult).toContain('<a href="https://github.com/Bavariance/polysimulator/issues/5071">#5071</a>');
  });
});
