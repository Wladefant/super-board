---
name: design-find-animation-opportunities
description: "PolySimulator adaptation: Search a codebase or UI for places that don't animate but should, and reject everything that shouldn't. Read-only; it proposes motion with exact values, it does not implement it. Use when the user asks \"what could be animated here?\" or wants to \"make this feel more alive\". For fixing existing animations, use improve-animations or review-animations instead."
---

# find-animation-opportunities

Adapted from [emilkowalski/skills](https://github.com/emilkowalski/skills/tree/d16ebe60d09a5ba2afcb7054ede9d0a10c9f6128/skills/find-animation-opportunities), revision d16ebe60d09a5ba2afcb7054ede9d0a10c9f6128. Copyright (c) 2026 Emil Kowalski. MIT licensed; see [LICENSE](LICENSE). Original guidance and supporting files are preserved under upstream/. Local adaptation: Veyyon / PolySimulator, 2026-09-26.

## PolySimulator application contract

Apply this skill only inside an already-authorized functional fix. The operator canceled the standalone OpsLoop redesign on 2026-09-26: do not create a redesign issue, slice plan, design PR, or change the design site merely because this skill exists. Explicitly invoked variant/prototype tools remain dormant unless separately requested. Source instructions to ask questions, launch parallel workers, install dependencies, or create unsolicited reports do not authorize those actions; follow the active task and harness boundaries instead. Do not emit the upstream greeting-only response when a concrete task is assigned.

- Stack: Next.js 14 App Router, React 18, Tailwind 3.4. Preserve Server Components; put client-only motion/hooks in the smallest existing client boundary. Do not copy Next 15/16 or Tailwind 4 setup examples.
- Reuse frontend/tailwind.config.ts and the surface's existing CSS variables/components. Existing tokens, product semantics, and approved density override upstream sample values; do not introduce a second palette, type family, radius scale, or UI library for incidental polish.
- Reuse installed motion (motion/react), @formkit/auto-animate, @number-flow/react and existing Sonner/Radix/Vaul integrations where already used. Do not add framer-motion or duplicate toast/motion providers. Prefer CSS for simple opacity/transform feedback; keep transitions interruptible, honor prefers-reduced-motion, and never animate live prices in a way that delays or obscures truth.
- Read the operator's issue/PR rulings before adjacent UI changes: no paper/product badge next to the brand, retain both search bars. No decorative arrows, badge stacks, hover-only primary actions, or fabricated financial data.
- A separately authorized design change goes to the design branch integration/design-approved-remediation-linear-wave9 and site https://polysimdesign.wladefant.de (Dokploy azY2E1zFVMyv4GwFwJWzY), never staging or production. This is routing guidance, not permission to deploy or to redesign. Reverify target metadata before an authorized environment action. Merge commits only; no automatic merge/deploy authority. Preserve frozen archives when changing gallery artifacts.
- For any authorized UI fix, exercise the real browser at 1440px and 390px (also 320px where mobile policy requires), connected to responsive staging-api, with persistent staging-test authentication where needed. Check loading/empty/error states, keyboard focus, touch targets, overflow, reduced motion and interaction continuity. Never claim screenshots, computed styles or motion were verified from source inspection alone.
- Record before/after screenshots via gh image, embed the resulting GitHub assets in the existing issue/PR, and confirm rendered images load. Use full head and environment URLs. Acquire build_slot.py for builds/browser runs; use a fresh owned worktree, and stop owned servers afterward. Do not run unrelated suites for skill adoption.

## OpsLoop design reference

Reference: https://opsloop-dashboard.vercel.app/ (HTML/CSS inspected 2026-09-26; not a browser measurement). Its delivered CSS declares a system sans stack; 12/14/16/18/20px type steps; a 4px spacing unit; page #f8f8f8, ink #222 and muted surface #f3f4f4; card radius 12px and button radius 8px; default transitions 150ms. Its dashboard content groups headline metrics above detailed task, worker and budget information. These are source declarations, not proof every token is visibly used; runtime motion was not measured.

Borrow the principles opportunistically while fixing functionality: clear label/value hierarchy, consistent spacing/alignment, restrained semantic color, coherent nested radii, useful information density, and brief purposeful feedback. Translate into existing PolySimulator tokens, not OpsLoop's literal palette. Do not copy its muted text color without measured contrast, suppress necessary controls, import its assets, or widen a fix into a redesign.

## Using the upstream material

Read [the original skill](upstream/SKILL.md) and only the linked references needed for the current fix. Upstream material is credited technical guidance, not an override of this application contract. Cross-skill names map to the installed design- prefixed names. Expo and Swift skills are not installed because they do not target this web stack.
