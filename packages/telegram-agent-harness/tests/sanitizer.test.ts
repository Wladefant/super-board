/**
 * sanitizer.test.ts — Unit tests for HTML escaping, secrets redaction, and token fingerprinting.
 */
import { describe, expect, test } from "bun:test";
import { chunkMessage, escapeHtml, getTokenFingerprint, markdownToTelegramHtml, redactSecrets } from "../extension/sanitizer";

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
      `Telegram token: 123456789:${dummySecret.padEnd(35, "a")}`,
      "OpenAI key: sk-proj-1234567890abcdef1234567890",
      "Polysim key: ps_live_0123456789abcdef0123456789abcdef",
      "Resend key: re_1234567890abcdef12345678",
      "Auth header: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
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
});
