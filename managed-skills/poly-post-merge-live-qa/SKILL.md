---
name: poly-post-merge-live-qa
description: "Post-merge live QA procedure for PolySimulator staging: webhook confirm, deployment attestation, signed-in default-functionality smoke pass, browser verification, and revert on failure under default SUPERBOARD_MERGE_FIRST."
---

# Poly Post-Merge Live QA

Source of truth: `AGENTS.md` §11 in [Bavariance/polysimulator](https://github.com/Bavariance/polysimulator) (trim carve-out, token audit 2026-09-26). Read the repository `AGENTS.md` for the surrounding authorization, deploy-attestation (§10), and conflict rules.

## When to Use
Once a merge to `staging` swings application-marked `watchPaths` (auto-deploy). After the merge lands, immediately verify the load, not "later". Under default `SUPERBOARD_MERGE_FIRST`, this post-deploy verification is the primary live QA gate; if verification fails, revert the deployed commit immediately. When `SUPERBOARD_MERGE_FIRST=0`, this serves as post-deploy attestation after pre-merge QA has already passed. Production remains strictly forbidden.

1. **Watch the merge land and confirm deployment fired.** Use Dokploy read-only container/health checks and §10 cache-busted full-SHA checks:
   - Backend `https://staging-api.polysimulator.com/v1/version?expected=<full-sha>`
   - Frontend `https://staging.polysimulator.com/api/version?expected=<full-sha>`
   - `polysim-deploy` fallback path or `check_alembic_drift.py` fail-closed behavior is identical — never bake local assumptions into verification.

## Every application merge requires a signed-in default-functionality smoke pass

After every merge to `staging` that triggers an application deployment (modifying
`watchPaths`), run a signed-in default-functionality smoke pass in a real Chromium
browser (`browser` tool):
1. **Market feed:** Verify live SSE price streaming and recent events load without 503 errors.
2. **Markets view:** Verify `/markets` at both desktop (1440px/1920px) and mobile (320px/390px) widths.
3. **Category & filter controls:** Verify all filter tabs, especially the "All" filter.
4. **Search:** Verify search results exclude resolved/closed markets.
5. **Trading lifecycle verification:** Sign in with an authorized staging test account
   (optionally selecting or provisioning a `SANDBOX` wallet if desired), place a
   limit or market order, verify order fill, verify ledger entry, place an
   off-market limit order, cancel it, and verify reserve refund.

For documentation-, policy-, prompt-, or runbook-only PRs that do not trigger application
deployments, verify the repository merge state on GitHub; do not generate or run
unrelated trading or UI smoke tests.

## Browser Verification Mandate

An API call (even with valid tokens) bypasses BFF proxy routing, cookie forwarding,
client session state, client-side routing, and SSR hydration. Every user-facing
frontend change must be verified using the `browser` tool in Chromium while signed
in with a persistent session.

An API smoke test uses `https://staging-api.polysimulator.com`, not the frontend host.
A health endpoint is supporting evidence only; exercise a relevant business route.
Money-path QA performs a representative transaction through the actual staging
surface and verifies balances, positions, and ledger behavior without touching
MAIN improperly. Because staging has its own database
(`hgzyqmaanndcimnclxtv`), such a transaction writes staging rows, not production
rows — a real staging transaction is the expected way to QA a money path. The
MAIN invariant still applies on staging: ordinary top-ups and payments credit
SANDBOX rather than MAIN, and never reset MAIN outside the dedicated
`topup_main_reset` product.

## Attach & Confirm Evidence

Visual work requires before/after screenshots attached so they render in GitHub, plus viewport and measured results. Authenticated surfaces must visibly show the authenticated state. For this private repository:
- **Verification is mandatory:** Immediately after opening or commenting on an issue/PR, reload the page in GitHub and confirm every attached image visibly renders. Never claim visual verification without confirming the rendered asset.
- **Allowed formats:** `gh image <file>` user attachments; GitHub Release assets via `gh release upload <tag> <file>` or the Release Assets API; commit-pinned `github.com/<owner>/<repo>/raw/<full-40-char-sha>/<path>` URLs where the SHA is pushed and reachable.
- **Prohibited:** `raw.githubusercontent.com` URLs (404/403 without session cookies), relative local paths, unpushed branches, unverified asset UUIDs.
- API smoke tests use `https://staging-api.polysimulator.com`, not the frontend host.
