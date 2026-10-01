---
name: unslop-writing
description: "Cut AI tells, puffery, hedging, robotic jargon, and filler from text you write or edit for a human reader (commit messages, PR titles/bodies, docs, code comments, and chat messages). Apply before committing, posting, or sending; leave untouched prose alone."
---

# Unslop Writing

Edit text to remove machine-generated tells, robotic boilerplate, and empty jargon. Replace them with plain, active, grounded human voice.

Apply this skill to text you write or edit for human eyes: commit messages, pull request titles and descriptions, issue comments, code comments, documentation, and chat/Telegram messages.

## The 4-Step Editing Loop

1. **Scan:** Identify AI tells, filler phrases, abstract metaphors, and passive constructions.
2. **Rewrite:** Preserve the core meaning, replace fancy words with plain ones, and state what actually happened.
3. **Add Voice:** Be direct, specific, and grounded. Name exact numbers, mechanisms, and real tradeoffs.
4. **Self-Audit:** Ask: *"What makes this obviously AI-generated?"* Cut remaining pleasantries, hedging, and em dashes.

## Patterns to Detect and Eliminate

### 1. Puffery & Promotional Language
- **Cut:** "pivotal moment", "testament to", "evolving landscape", "setting the stage for", "indelible mark", "groundbreaking", "breathtaking", "seamless", "delve into".
- **Fix:** State what happened factually without exaggeration.

### 2. AI Vocabulary
- **Cut:** Additionally, crucial, delve, enduring, enhance, fostering, garner, interplay, intricate, landscape, pivotal, showcase, tapestry, testament, underscore, vibrant.
- **Fix:** Use ordinary, concrete words: "also", "important", "explore", "show", "prove", "context".

### 3. Em Dash Overuse
- **Cut:** Avoid em dashes (`—`) entirely. Em dashes are a prominent AI signature.
- **Fix:** Use periods, commas, or split the thought into two clear sentences. Avoid substituting parentheses or hyphens as crutches.

### 4. Inline Header Colons
- **Cut:** Bold labels followed by a colon that merely restates the line: `"**Performance:** Performance was improved by 20%."`
- **Fix:** Write flowing prose or a clean bullet list: `"Latency dropped 20% after query batching."`

### 5. Chatbot Pleasantries & Sycophancy
- **Cut:** "Certainly!", "I'd be happy to help!", "I hope this helps!", "Great question! You're absolutely right!", "Found the smoking gun!".
- **Fix:** Respond directly with the fact, decision, or result.

### 6. Jargon Metaphor Nouns
- **Cut:** Substrate, wedge, vector, locus, vantage, nexus, primitive (as noun), harness (as metaphor), bedrock, scaffolding (as metaphor), modality, paradigm, ratchet, evacuate (for moving code), endgame, north star, flywheel.
- **Fix:** Pick the concrete word: "base", "add", "method", "goal", "last phase", "move out".

### 7. Passive Voice & Weak Verbs
- **Cut:** "Queries are validated by the compiler", "The lockfile was regenerated", "significantly improves".
- **Fix:** Use active voice with the real actor: "The compiler validates queries", "Regenerated the lockfile", name the measured delta.

### 8. Fancy Synonyms
- **Cut:** "utilize" -> "use", "leverage" -> "use", "facilitate" -> "help", "in the event that" -> "if", "serves as" / "stands as" -> "is", "due to the fact that" -> "because".

### 9. Decorative Emojis
- **Cut:** Emojis prepended to every heading or bullet item (e.g. `🚀`, `✨`, `🔥`, `💡`, `✅`).
- **Fix:** Clean typography and markdown structure.

## Channel-Specific Standards

### Commit Messages
- Imperative mood, concise summary line (under 72 chars), blank line, detailed context.
- Author and committer MUST be a repository-authorized human identity:
  `Wladimir Kirjanovs <wladefant@gmail.com>`
- Explain *why* the change was made and what problem it solves, not just what files were touched.

### Pull Request Descriptions
- Clear problem statement, step-by-step summary of changes, and exact verification evidence.
- Include structured before/after comparison tables for visual changes.
- Every claim must be backed by observable evidence (SHAs, test commands, timestamps, URLs).

### Telegram Messages
- Use Telegram Premium rich HTML formatting (`<b>`, `<code>`, `<pre>`).
- Keep messages structured and scannable; never dump walls of unformatted markdown.
- **Every mention is a link:** Every issue, PR, commit, branch, release, and image mentioned MUST be a clickable link (e.g. `<a href="https://github.com/.../pull/123">#123</a>`).
