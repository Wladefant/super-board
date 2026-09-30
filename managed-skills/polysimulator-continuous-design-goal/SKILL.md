---
name: polysimulator-continuous-design-goal
description: "Run PolySimulator designstaging autonomously with fifteen continuously replenished GPT-5.6 Sol UI agents covering comments, bugs, features, design proposals, implementation, review, integration, GitHub, and Superboard evidence."
---

## Continuous fifteen-agent design rule

Use this procedure whenever the PolySimulator design-system transformation is active.

1. Target only design websites, never software staging or production. Recheck Dokploy's design deployment lineage: on 2026-09-26 `https://polysimdesign.wladefant.de` used application `azY2E1zFVMyv4GwFwJWzY`, branch `integration/design-approved-remediation-linear-wave9`, and `tools/design-gallery/Dockerfile`. Do not assume the historical `designstaging` branch is what is deployed.
2. Maintain exactly fifteen asynchronous `openai-codex/gpt-5.6-sol` UI-design workers whenever at least fifteen independent actionable slices remain. Refill every completed slot after checking RAM; never exceed the system cap of twenty or spawn at 95%+ memory use.
3. Keep the interactive agent as orchestrator: decomposition, contracts, conflict resolution, integration decisions, and evidence synthesis. Delegate repository research, implementation, review, and verification through background `task` jobs.
4. Continuously allocate lanes across: reading and interpreting Wladimir's comments; reproducing frontend defects; proposing bug fixes; auditing unfinished surface states; identifying coherent new features; challenging and proposing design-system improvements; implementing approved/evidence-grounded remediations; independent exact-head review; integration; and desktop/mobile verification.
5. Give every writing worker a unique isolated worktree and branch, non-overlapping ownership, explicit acceptance criteria, and targeted checks. Never let writing lanes share a mutable checkout.
6. Before any unsuffixed design artifact changes, archive its exact current state to the next unused `.vN` file. Never alter an existing archive. Keep Calendar v2 and Profile v2 frozen as rejected negative references.
7. Follow `skill://komo-design-feedback` for design feedback intake: fetch open comments as one prompt, map threads to GitHub issues/PRs, reply with verified changes and PR links, then resolve with read-back. Komo replaces the legacy design-gallery pin-comment channel (`/__c/` API, `/data/comments.json`) only AFTER real browser→CLI→reply→resolve parity at 1440 and 390 is recorded in https://github.com/Bavariance/polysimulator/issues/5559. Until then dual-read and retain the existing overlay/store. Keep decision ledgers regardless of feedback migration. Never silently resolve operator-only decisions or fabricate product facts.
8. Enforce calm wagering UX and product truth: sacred MAIN baseline; distinct SANDBOX/API wallets; tenant-gated PROP_FIRM; category taker fees with zero resting maker fee; public route `/profile/[username]`; no terminal styling, decorative directional arrows, balance emoji, badge stacks, hover-hidden primary actions, or caretaker subtitles.
9. Record truthful provenance for every changed screen and element: agent ID, exact provider-prefixed model from runtime evidence, source/target version, design-system dependency, rationale, timestamp, commit, and verification. Mark unknown attribution as unknown rather than guessing.
10. Integrate only committed, independently reviewed exact-head work. Preserve individual commits; never squash, merge-commit, rebase published shared history, or force-push.
11. Before committing, verify `Wladimir Kirjanovs <wladefant@gmail.com>`. Push only the completed feature branch; never push directly to protected branches.
12. Link the authoritative GitHub issue, branch, commits, PR, evidence, and Superboard card with full URLs. Move Superboard through Building, QA, Review, and Done only when factually earned. A commit, push, or merge alone is not Done.
13. Do not deploy or mutate Dokploy, Supabase, Cloudflare, hosts, or other external systems without explicit current-conversation authorization.
14. Verify archive integrity, targeted behavior, desktop/mobile rendering, keyboard/focus/touch accessibility, product truth, and the design-system-to-screen diff before completion.
15. Continue autonomously without asking Wladimir questions while the no-question instruction is active. Record operator-only gates as explicit blockers, propose conservative alternatives, and continue all other work.
