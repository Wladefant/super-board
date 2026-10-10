---
name: competitor-deep-dive
description: "Competitor and reference-site research for a startup. Covers the keyword matrix, one native sub-issue per competitor, signed-out analysis, signed-in workflow capture and reference sites for redesigns. Video frames go through competitor-video-frames. Source: Shipnovo research #133, #202 and #254, 2026-10-04 to 2026-10-10."
---

> Source of truth: [`managed-skills/competitor-deep-dive/SKILL.md`](https://github.com/Wladefant/super-board/blob/main/managed-skills/competitor-deep-dive/SKILL.md) in Wladefant/super-board. Edit it there and merge. Then `python scripts/install-managed-skills.py competitor-deep-dive` copies it to `~/.veyyon/profiles/default/agent/managed-skills/`. The installer backs up a local edit and then overwrites it.

# Competitor deep dive

Use this skill when you research competitors, reference websites or product videos for a startup.
It records how Shipnovo did it ([Wladefant/shipnovo#133](https://github.com/Wladefant/shipnovo/issues/133), 2026-10-04 to 2026-10-10).
For frames from YouTube videos, follow skill `competitor-video-frames`. This skill does not repeat it.

## 1. Tracker and keyword matrix

1. One parent issue per product: "Competitors". It stays an anchor. Every competitor is a native sub-issue (profile AGENTS.md §5). Never collect competitors as comments: #133 once held 20 competitors as 21 comments.
2. Write the target keywords on the parent (Shipnovo: K1 to K22, https://github.com/Wladefant/shipnovo/issues/133#issuecomment-6093776341).
3. Search each keyword (Germany first for a German product) and record the organic top 10. Ads are not visible through the search API, so do not list advertisers.
4. Build a keyword x competitor matrix: `K12#2` means keyword 12, rank 2. Rank by keyword coverage, then by best rank. Shipnovo top 3 on 2026-10-10: Billbee (7 keywords), SimpleSell (5), Sendcloud (4).
5. List every result that is not a competitor, with the reason: marketplaces, carriers, label hardware, review sites, blogs, forums, tools out of scope. This stops the next lane from checking them again.

## 2. One sub-issue per competitor (signed-out)

- Check first that no issue for the company exists (`gh issue list --search "<name> in:title" --state all`).
- Title `Competitor: <name>`. Copy labels, milestone and assignee from an existing competitor issue (Shipnovo: [#1107](https://github.com/Wladefant/shipnovo/issues/1107)).
- Body sections, in this order:
  1. Scope, with the parent link.
  2. Acceptance Criteria: 3 unticked boxes (signed-out analysis, signed-in workflows, takeaways).
  3. Snapshot: what it is, for whom, price.
  4. Users LOVE, Users HATE, Beginners, Power users, Phone users.
  5. Product-specific feature sections (Shipnovo: eBay/marketplace, shipping and warehouse, AI features).
  6. Price, Points relevant to us, Keywords it ranks for.
  7. A rating table (OMR, Trustpilot, G2, Capterra: score, count, date) and Sources with full links.
- Read public pages only: home, pricing, features, integrations, AI page, help center. Never click ad links (`aclk`). No signups in this step.
- Write "unknown on public pages" for anything not found. Quote prices in the page currency with the source link. Never invent.
- Link it as a native sub-issue with GraphQL `addSubIssue`, then read the parent's `subIssues` to verify.
- A company that is not a competitor gets no issue. Report "not a competitor: <reason>".
- Firecrawl may return 429: wait 5 s and retry, at most 3 times. When Firecrawl is out of credits, read pages directly with the `read` tool.

## 3. Signed-in workflow capture

- Use a test account per competitor: free trial or demo, created with a role address on our domain (for example `lane-read@...`), never with personal data.
- Capture the real flows a user runs: onboarding, connecting the marketplace, the order list, creating a label, bulk actions, settings. Capture at 1440 and 390.
- Commit the screenshots to a research branch, not to `main` (Shipnovo: 333 PNGs on `research/competitors-signed-in`, commit `aded4b11`). Write one synthesis issue (Shipnovo: [Wladefant/shipnovo#254](https://github.com/Wladefant/shipnovo/issues/254)) with the patterns worth copying.
- Shipnovo evaluated 6 tools signed in (Billbee, Sendcloud, Easyship, Afterbuy, plentymarkets, Packlink PRO) and skipped 6 with a reason each.
- Stop at a captcha or bot wall after 1 attempt. Sendcloud's signup uses Cloudflare Turnstile ([Wladefant/shipnovo#202](https://github.com/Wladefant/shipnovo/issues/202)). Ask the operator whether he registers by hand or the tool is skipped. Never solve a captcha with a service.
- Never type real payment data. Never contact a competitor's sales or support as a fake customer.

## 4. Reference sites for a redesign

- Before a homepage or page redesign, compare 10 to 20 leading sites. Split them into the home market and international. Shipnovo did this for [Wladefant/shipnovo#1164](https://github.com/Wladefant/shipnovo/issues/1164).
- For each site record: hero message, proof (logos, numbers, ratings), call to action, pricing display, navigation, mobile layout. Add a screenshot at 1440 and 390 for each, checked with `inspect_image`.
- End with a short list of patterns to adopt and patterns to avoid. The operator picks before any design work starts.

## 5. Images and evidence

- Every screenshot or frame goes through `inspect_image` before upload. Ask whether it shows the claimed screen. Crop very tall full-page captures first.
- Upload with `gh image --repo <owner/repo>`. Embed with Markdown image syntax. Check that each image loads (HTTP 200, `image/*`).
- The caption claims only what the image shows. A third-party video is labelled as third-party.

## 6. Done

- [ ] Keyword matrix on the parent, with the not-a-competitor list.
- [ ] One sub-issue per competitor, native link verified, boxes unticked until proven.
- [ ] Signed-in captures on a research branch, synthesis issue written, skipped tools named with a reason.
- [ ] Every image checked with `inspect_image` and loading.
- [ ] Downloaded videos and temp frames deleted.
