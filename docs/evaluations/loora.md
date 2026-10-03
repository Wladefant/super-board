# Tooling Evaluation: Loora

- **Target Issue:** [Wladefant/super-board#72](https://github.com/Wladefant/super-board/issues/72)
- **Subject:** [Loora](https://github.com/lassejlv/loora) by [Lasse Vestergaard (@lassejv)](https://x.com/lassejv/status/2083204316995658128)
- **Repository:** [`lassejlv/loora`](https://github.com/lassejlv/loora)
- **Evaluated Head:** [`c96b02a`](https://github.com/lassejlv/loora/tree/main) (September 2026)
- **Evaluation Date:** 2026-09-21
- **Recommendation:** **NO** — Reject self-hosted adoption for the `design-prototyping` lane; preserve the offline standalone HTML prototype flow.

---

## 1. What It Is

[Loora](https://github.com/lassejlv/loora) is an open-source infinite-canvas UI design tool architected for dual human and autonomous agent interaction. Unlike bitmap graphics or unstructured HTML canvases, Loora models documents as a structured tree of typed nodes (`page`, `component`, `frame`, `group`, `text`, `shape`, `vector`, `image`, `instance`) managed by a deterministic TypeScript Canvas Engine ([`packages/canvas`](https://github.com/lassejlv/loora/tree/main/packages/canvas)).

### Core Architecture & Stack
- **Monorepo Runtime:** Bun workspaces monorepo ([`package.json`](https://github.com/lassejlv/loora/blob/main/package.json)).
- **Frontend & App Shell:** TanStack Start / React 19 web app ([`apps/web`](https://github.com/lassejlv/loora/tree/main/apps/web)) and cross-platform desktop client using Vite + Tauri ([`apps/desktop`](https://github.com/lassejlv/loora/tree/main/apps/desktop)).
- **State & Data Layer:** Drizzle ORM on Neon Serverless Postgres ([`packages/db`](https://github.com/lassejlv/loora/tree/main/packages/db)), oRPC typed RPC procedures ([`packages/rpc`](https://github.com/lassejlv/loora/tree/main/packages/rpc)), and Redis for rate-limiting and SSE fallback.
- **Realtime Layer:** Cloudflare Worker utilizing Durable Objects (`RealtimeRoom`, `RealtimeUser`, `RealtimeIngress`) for hibernatable WebSocket rooms and presence ([`apps/ws-server`](https://github.com/lassejlv/loora/tree/main/apps/ws-server)).
- **Agent Integration:** Cloudflare Worker MCP transport ([`apps/mcp`](https://github.com/lassejlv/loora/tree/main/apps/mcp)) exposing a 33-tool catalog, plus an in-editor assistant ([`packages/assistant`](https://github.com/lassejlv/loora/tree/main/packages/assistant)) powered by OpenAI ChatGPT OAuth.
- **Export Pipeline:** One-way code compilation to clean HTML, JSX, Tailwind React/TSX, JSON document schemas, and headless Chromium PNG screenshots ([`packages/canvas/src/export`](https://github.com/lassejlv/loora/tree/main/packages/canvas)).

---

## 2. Fit Against the Stated Need

Issue [Wladefant/super-board#72](https://github.com/Wladefant/super-board/issues/72) evaluated Loora for the `design-prototyping` lane ([`skills/design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md)) across four primary requirements:

### 1. Self-Hostability on VPS ("Run-everything-on-the-VPS rule") — **FAIL**
- **Severe Vendor Coupling:** While [`Dockerfile`](https://github.com/lassejlv/loora/blob/main/Dockerfile) packages `apps/web` for Railway container deployments, Loora cannot function as an autonomous self-contained stack on a standard VPS or Dokploy instance.
- **Cloudflare Durable Objects Requirement:** The authoritative realtime multiplayer engine ([`apps/ws-server/README.md`](https://github.com/lassejlv/loora/blob/main/apps/ws-server/README.md)) relies directly on Cloudflare Workers and proprietary Durable Objects (`RealtimeRoom`, `RealtimeUser`). It cannot run inside standard Bun/Node Docker containers without Cloudflare edge infrastructure or Miniflare emulation.
- **External SaaS Dependencies:** Production configuration ([`.env.example`](https://github.com/lassejlv/loora/blob/main/.env.example)) requires Cloudflare for SaaS custom hostnames, Cloudflare Email API, Polar billing integration, and Neon serverless database connection pooling.
- **Precedent:** Super Board deliberately parked the self-hosted Agent Native stack on 2026-08-02 ([`docs/runbooks/AGENT-NATIVE-PARKED.md`](https://github.com/Wladefant/super-board/blob/main/docs/runbooks/AGENT-NATIVE-PARKED.md)) due to maintenance overhead, upstream rebuild churn ([#44](https://github.com/Wladefant/super-board/issues/44)), and missing AI provider configuration ([#42](https://github.com/Wladefant/super-board/issues/42)). Deploying Loora would recreate the exact same operational failure with higher architectural complexity.

### 2. Precise Agent Control — **PASS**
- **Comprehensive MCP Surface:** Loora implements 33 canonical MCP tools ([`apps/mcp/src/tools.json`](https://github.com/lassejlv/loora/blob/main/apps/mcp/src/tools.json)) over Streamable HTTP and stdio.
- **Structured Mutation Vocabulary:** Agents do not scrape DOM or inject raw HTML strings. Tools like `createPage`, `insertNodes`, `patchNodes`, `createComponent`, `createInstance`, and `setTokens` ([`packages/agent/src/canvas-tools.ts`](https://github.com/lassejlv/loora/blob/main/packages/agent/src/canvas-tools.ts)) dispatch atomic transactions validated by the Canvas Engine with automatic layout repair.
- **Automated Verification:** The `getScreenshot` tool captures headless Chromium PNG renders via Playwright, enabling vision models to inspect visual hierarchy and tokens.
- **Skill Guidance:** Ships a dedicated agent guidance skill ([`skills/loora-design-guide`](https://github.com/lassejlv/loora/tree/main/skills/loora-design-guide/SKILL.md)) for structured Canvas authoring.

### 3. Branches, Version History & Variant Exploration — **PASS**
- **First-Class Document Branching:** Built-in MCP and RPC lifecycle tools (`createBranch`, `proposeBranch`, `compareBranch`, `applyBranch`, `closeBranch`) enable isolated variant exploration without overwriting Main.
- **Field-Level Semantic Merge:** Merging branches calculates field-level three-way semantic diffs with explicit conflict choices, resolving the loss of exploration history in static HTML flows.

### 4. Usability for Human Designer ("Brandy can open it") — **UNSATISFACTORY (Self-Hosted)**
- The web and desktop interfaces provide rich canvas interactions, properties panels, and design token management.
- However, self-hosting requires configuring Better Auth (`BETTER_AUTH_SECRET`, OAuth providers), preview access tokens (`REQUIRE_PREVIEW_ACCESS`), and connection ticket signing. Without active deployment and authentication infrastructure, the designer cannot access the canvas.

### Comparison Against Baseline (Offline Standalone HTML Prototype)
The offline HTML prototype defined in [`skills/design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md) remains superior for Super Board's operational reality:
- **Zero VPS Overhead:** 0 MB RAM consumed on the shared, RAM-constrained Windows/VPS host (Loora requires running Bun, Chromium Playwright, Postgres, and Redis).
- **Zero External SaaS:** No dependence on Cloudflare Workers, Polar billing, or Neon database pools.
- **Direct Version Control:** Standalone HTML prototypes commit directly to Git repositories, survive offline, and render instantly in any browser.

---

## 3. Cost and Licensing

- **License:** **GNU Affero General Public License v3.0 ([AGPL-3.0](https://github.com/lassejlv/loora/blob/main/LICENSE))**, Copyright (C) 2026 Lasse Vestergaard.
- **License Terms:** Free to use, fork, and self-host for commercial and private purposes. However, modifying the source and offering it over a network mandates publishing the complete source code under AGPL-3.0.
- **Infrastructure / Operating Cost:**
  - **Cloudflare Workers:** Requires a paid Cloudflare Workers subscription for production Durable Objects support in `apps/ws-server`.
  - **Database & Services:** Neon Postgres database fees, Redis instance, and S3/R2 object storage bucket for asset uploads.
  - **Host Resources:** High memory footprint due to server-side Playwright Chromium instances for screenshot rendering.
- **Hosted SaaS Pricing:** Loora Cloud is gated via [Polar](https://polar.sh) plans (Free tier, Pro subscription, and metered MCP tool call limits).

---

## 4. Risks

1. **Vendor Lock-in to Cloudflare Edge Primitives:** The realtime WebSocket architecture is hardcoded against Cloudflare Durable Objects. It cannot deploy to standard Docker/Dokploy environments without maintaining Cloudflare worker bindings.
2. **Infrastructure Maintenance Burden:** Maintaining a 13-package monorepo across Drizzle, Better Auth, Polar, and Tauri on a private VPS will absorb substantial development bandwidth with no direct product return.
3. **Host RAM Starvation:** Headless Chromium required by the MCP execution service for screenshot generation risks crashing or destabilizing our RAM-tight workstation environment.
4. **Upstream Drift:** The repository is under active early-stage development, with frequent schema refactors and tight integration with Railway and Cloudflare services.

---

## 5. Recommendation

### Verdict
**NO (Reject self-hosted adoption for the design-prototyping lane).**

### Recommendation Sentence
We recommend rejecting self-hosted deployment of Loora on our VPS infrastructure and retaining the standalone offline HTML prototype workflow in `skills/design-prototyping`, because Loora's architecture is deeply coupled to proprietary Cloudflare Durable Objects, Neon Postgres, and Polar billing services, re-introducing the severe maintenance and reliability burdens that forced the shutdown of Agent Native.

### Condition for Reconsideration
Re-evaluate Loora only if upstream releases a self-contained, single-container Docker distribution that eliminates Cloudflare Durable Object dependencies in favor of standard WebSockets/Redis, or if Super Board adopts Loora purely as an externally hosted SaaS tool over the MCP protocol without running infrastructure on our VPS.
