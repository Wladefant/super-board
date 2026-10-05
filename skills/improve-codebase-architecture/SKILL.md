---
name: improve-codebase-architecture
description: Survey a codebase for deepening opportunities, apply the deletion test to each, present them as an offline HTML report, then stop and ask which one to pursue. Report-only by default; never edits code.
disable-model-invocation: true
---
<!--
Adapted from https://github.com/mattpocock/skills at commit 24fe0ef7737efae15c87225755e9f6f5965e4888
Upstream path: skills/engineering/improve-codebase-architecture/SKILL.md
Copyright (c) 2026 Matt Pocock. Used under the MIT License, reproduced below.
Local changes (super-board, issue #513): exploration runs through Veyyon `task` lanes instead of
the Claude Code Explore tool; the report is one offline file with no CDN; report-only is the
default and the grilling loop is opt-in and bounded; the report is kept as a lane artifact and
attached to a GitHub issue; every candidate shows a deletion-test result.

MIT License

Copyright (c) 2026 Matt Pocock

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
-->

# Improve Codebase Architecture

Surface architectural friction and propose **deepening opportunities**: refactors that turn shallow modules into deep ones. The aim is testability and AI-navigability.

Built on the `codebase-design` skill. Read it first and use its seven terms exactly (**module**, **interface**, **depth**, **seam**, **adapter**, **leverage**, **locality**). Never write "component," "service," "API," or "boundary" for design talk. Domain nouns come from `GLOSSARY.md`; decisions in `docs/adr/` are not re-litigated.

## Modes

- **`--report-only` (default).** Explore, write the report, attach it, stop, and ask "Which of these would you like to explore?". The run changes no code file. `git status` is clean except the report artifact.
- **`--grill <candidate>`.** Only after a person picks a candidate. Walk that one candidate's decision tree (constraints, dependencies, shape of the deepened module, what sits behind the seam, which tests survive). Stop after the decision. Do not start a second candidate and do not run past the decision.

Scheduled runs (Gardener `--survey-depth`) are always `--report-only`.

## Process

### 1. Explore (Veyyon lanes)

**Scope before you scan.** Deepening pays off where code keeps changing. If the caller named a direction, take it. Otherwise run `git log --since=90.days --name-only --pretty=format:` and rank files by touch count. Those hot spots pull attention first. If changes are scattered, widen the net.

Read `GLOSSARY.md` and the ADRs in the area first.

Then delegate with the Veyyon `task` tool, not a harness-specific Explore tool:

1. **Scan lane** (`task`, Flash): list hot-spot modules with interface size versus implementation size, pass-through wrappers, call counts per module, and test reach. Facts only, no judgment.
2. **Judgment lane** (`sonnet`): take the scan result and apply the deletion test and the friction questions below. Output candidates with files, problem, solution, benefit, strength.

With no `task` tool available, do both steps inline. Do not skip the deletion test.

Friction questions:

- Where does understanding one concept require bouncing between many small modules?
- Where is a module **shallow**, with an interface nearly as complex as the implementation?
- Where were pure functions extracted only for testability, while the real bugs hide in how they are called (no **locality**)?
- Where do tightly coupled modules leak across their seams?
- Which parts are untested, or hard to test through their current interface?

### 2. Deletion test (mandatory, per candidate)

Imagine deleting the module. Write one of these results on every candidate:

- `pass-through`: complexity vanishes; the module earned nothing. Deleting it is the deepening.
- `concentrates`: complexity reappears across N callers; the module earns its keep, but its interface is too wide. Deepen it.
- `inconclusive`: say what evidence is missing.

A candidate with no recorded result is not reported. One adapter at a seam is a hypothetical seam; call that out.

### 3. Report (offline, kept)

Write ONE self-contained HTML file from `report-template.html`. See [HTML-REPORT.md](HTML-REPORT.md). Rules:

- Inline CSS and hand-built SVG only. No CDN, no remote font, no script that fetches. The file must render identically with the network blocked.
- Save it under the lane artifact directory as `architecture-review-<repo>-<UTC timestamp>.html`. Never leave it only in a temp directory.
- Attach it to a GitHub issue: the standing survey issue for the repo, or the issue that asked for the run. A bare path on one machine is not delivery. Post the candidate titles, strengths and deletion-test results as the comment body; the HTML is the attachment or a commit-pinned link.
- Each candidate card: files, problem, solution, benefits in terms of locality and leverage, before/after diagram, recommendation strength (`Strong`, `Worth exploring`, `Speculative`), deletion-test result.
- End with a **Top recommendation**.
- **ADR conflicts**: surface a candidate that contradicts an ADR only when the friction justifies reopening it, and mark it on the card.

Do NOT propose interfaces yet. After the report is attached, stop and ask: "Which of these would you like to explore?"

### 4. Grilling (only on request)

When a person picks a candidate, use the `grilling` skill if present, else ask the questions directly, one at a time. Side effects as decisions settle:

- A deepened module named after a concept missing from `GLOSSARY.md`: add the term.
- A fuzzy term sharpened: update `GLOSSARY.md` there.
- A candidate rejected for a load-bearing reason: offer an ADR so later surveys skip it. Skip ephemeral reasons.
- Alternative interfaces wanted: use the design-it-twice pattern in `codebase-design`.

End the grilling when the decision is recorded.
