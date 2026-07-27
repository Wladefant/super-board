# Agent Native production layer for Superboard and Claudex

**Status:** architecture decision for [Superboard issue #23](https://github.com/Wladefant/super-board/issues/23)

**Research snapshot:** 2026-07-27, Builder.io Agent Native commit [`c24e169`](https://github.com/BuilderIO/agent-native/tree/c24e1695a028c344a35eb58db6537cb6819156ff)

**Decision:** **pilot with constraints**

## Executive decision

Adopt Agent Native as a **replaceable visual cockpit and agent-native application framework**, not as a second orchestration system.

- **Superboard** remains authoritative for work, acceptance criteria, lifecycle status, and durable evidence.
- **Claudex** remains authoritative for model role, tool profile, context admission, permissions, and escalation.
- **Claude Code** remains the coding engine that reads and changes repositories.
- **GitHub** remains authoritative for branches, commits, pull requests, checks, approvals, and merge history.
- **Agent Native** provides the web UI, plans and recaps, session transcripts, follow-ups, approvals presentation, preview embedding, app context, and evidence projection.
- **Dokploy/VPS** hosts the cockpit, Postgres, and reverse proxy. Full coding execution runs in a separate private worker boundary.

The decisive test is reversibility: if the Agent Native service disappears, the issue, Project card, branch, PR, checks, approvals, and completion history must remain complete and recoverable.

## Why now

Current Agent Native is substantially broader than the ING-era Clips snapshot. Upstream now includes:

- A shared `defineAction()` model that can expose one validated operation to UI, HTTP, CLI, MCP, A2A, jobs, and agents.
- A reusable browser coding workspace through `@agent-native/code-agents-ui`.
- Harness adapters for Claude Code, Codex, Pi, and ACP-compatible agents, with resumable SQL-backed sessions.
- A visual Plan/Recap product for coding plans, diagrams, prototypes, annotated walkthroughs, review comments, and PR recaps.
- Production deployment, authentication, tenant-scoping, encrypted credentials, SSRF protection, approvals, and durable run primitives.

The old inlined ING copy is not an appropriate production base. It contains `@agent-native/core` **0.79.25**, while the reviewed upstream snapshot contains **0.123.2** and newer Code UI, Toolkit, Harness, Plan, security, and deployment contracts. The ING copy remains historical evidence only.

Sources:

- [Upstream framework README](https://github.com/BuilderIO/agent-native/blob/c24e1695a028c344a35eb58db6537cb6819156ff/README.md)
- [Current upstream core package 0.123.2](https://github.com/BuilderIO/agent-native/blob/c24e1695a028c344a35eb58db6537cb6819156ff/packages/core/package.json)
- [Historical ING copy at core 0.79.25](https://github.com/Wladefant/ing-qa-automation/blob/main/agent-native/packages/core/package.json)
- [Agent Native Plan](https://github.com/BuilderIO/agent-native/blob/c24e1695a028c344a35eb58db6537cb6819156ff/templates/plan/README.md)

## Current capability assessment

### Production-useful now

| Capability | Assessment | Intended use in this system |
|---|---|---|
| Shared typed actions | Strong | Narrow UI/API façades over governed Superboard and runner operations |
| Embedded agent panel | Strong | Contextual app chat and app actions |
| Code Agents UI | Strong reusable surface | Runs, transcripts, follow-ups, status, plan/auto mode, approvals |
| Harness agents | Strong integration seam | Drive Claude Code as the full coding runtime without replacing its loop |
| Plan and Visual Recap | Immediately useful | Human-reviewable plans before building and visual recaps after changes |
| Nitro/Node deployment | Suitable for VPS | Self-contained `.output`, Node 24, port 3000, Docker-compatible |
| Postgres persistence | Required and suitable | Cockpit sessions, projections, auth, encrypted credentials |
| Security defaults | Good if standard patterns are followed | Zod actions, scoped tables, access guards, SSRF-safe fetch, signed A2A/webhooks |
| Toolkit | Useful | Reusable shell, editor, collaboration, dashboards, and UI primitives |
| Dispatch | Technically substantial but overlapping | Do not adopt as the Superboard control plane |

### Important limits

1. **A plain production web app does not automatically become a safe coding host.** App mode has no filesystem or shell. Code mode needs a code-capable frame, a harness/host integration, or production code execution.
2. `AGENT_PROD_CODE_EXECUTION=trusted` exposes shell and filesystem tools. It is acceptable only inside a deliberately isolated, operator-controlled worker—not in the public web container.
3. The documented `sandboxed` `run-code` mode is process-level isolation, not an OS container; outbound networking is not blocked by Node itself. It is insufficient as the sole boundary for arbitrary repository coding.
4. The local Code run store is file-backed. A production browser host must implement the `CodeAgentsHost` contract against a durable runner/relay rather than assuming Desktop process control exists in the browser.
5. Agent Native packages are moving quickly and remain pre-1.0. Pin exact versions and upgrade intentionally; do not build production images against `@latest`.
6. Dispatch is explicitly a separate workspace control plane with its own vault, approvals, audit events, resources, destinations, recurring jobs, and cross-app delegation. Adopting it wholesale would duplicate Superboard and Claudex.

Sources:

- [Frames and App/Code modes](https://github.com/BuilderIO/agent-native/blob/c24e1695a028c344a35eb58db6537cb6819156ff/packages/core/docs/content/frames.mdx)
- [Agent-Native Code UI and browser host contract](https://github.com/BuilderIO/agent-native/blob/c24e1695a028c344a35eb58db6537cb6819156ff/packages/core/docs/content/code-agents-ui.mdx)
- [Harness Agents](https://github.com/BuilderIO/agent-native/blob/c24e1695a028c344a35eb58db6537cb6819156ff/packages/core/docs/content/harness-agents.mdx)
- [Deployment and production code execution](https://github.com/BuilderIO/agent-native/blob/c24e1695a028c344a35eb58db6537cb6819156ff/packages/core/docs/content/deployment.mdx)
- [Dispatch package boundary](https://github.com/BuilderIO/agent-native/blob/c24e1695a028c344a35eb58db6537cb6819156ff/packages/dispatch/README.md)
- [Toolkit package boundary](https://github.com/BuilderIO/agent-native/blob/c24e1695a028c344a35eb58db6537cb6819156ff/packages/toolkit/README.md)

## Ownership boundaries

| Concern | Authoritative owner | Agent Native role |
|---|---|---|
| Task definition and acceptance criteria | GitHub issue | Render, validate before launch, deep-link |
| Workflow status | GitHub Project | Read projection; request mutations through the Superboard adapter |
| Model/provider selection | Claudex | Display resolved role; never choose a provider independently |
| Context admission and tool profile | Claudex | Display policy and approval prompts |
| Source edits and tests | Claude Code | Session cockpit and transcript |
| Worktree/container allocation | Private runner | Display workspace identity and health |
| Branch, commits, PR, checks | Git/GitHub | Aggregate full evidence URLs |
| Preview deployment | Existing CI/Dokploy preview mechanism | Embed or link the preview |
| QA verdict | Superboard QA lane and human where required | Present evidence; never infer pass from transcript text |
| PR approval and merge | Human in GitHub | Deep-link only; no merge authority in the pilot |
| Durable audit history | GitHub issue, PR, checks, Project history | Secondary session telemetry and visual artifacts |
| Secrets | Runner/Dokploy secret stores and framework credential APIs | Never display values; store only when required and encrypted |
| Product-specific agent actions | Each SaaS application | `defineAction()` becomes the shared UI/agent/API contract |

Agent Native must not own a second backlog, Project status, acceptance record, merge decision, global permission policy, repository credential store, or completion ledger.

## Options considered

Scores are 1–5 and optimized for the near-term Superboard integration.

| Option | System-of-record integrity | Claudex fit | Security | Time to pilot | UX | Decision |
|---|---:|---:|---:|---:|---:|---|
| **A. Thin cockpit + private runner** | 5 | 5 | 4 | 4 | 4 | **Recommended** |
| B. Agent Native as the main session gateway/orchestrator | 3 | 3 | 3 | 2 | 5 | Possible later, after strict boundaries are proven |
| C. Dispatch control plane mirrored to Superboard | 1 | 2 | 2 | 2 | 5 | Reject |
| D. Embed Agent Native separately into every product first | 4 | 4 | 4 | 2 | 4 | Valuable second track, not the central coding cockpit pilot |

### Recommended shape: thin cockpit + private runner

Agent Native renders Superboard-governed sessions through the reusable Code UI. A narrow server adapter validates requests against GitHub. A private runner launches the existing Claudex-governed Claude Code runtime in an isolated workspace and streams normalized events back.

This can later evolve into a richer gateway without changing ownership.

### Why not Dispatch as the control plane

Dispatch has useful ideas—remote sessions, destinations, integrations, approvals, auditing, and cross-app delegation—but those are exactly the domains already governed by Superboard, Claudex, GitHub, and the existing channel system. Mirroring two control planes creates ambiguous recovery and states such as “Done in Dispatch, Building in GitHub.”

Use individual upstream interfaces or UI ideas where useful; do not adopt Dispatch jobs, approval state, vault grants, or workflow status as canonical.

## Recommended architecture

```text
Browser / phone
    |
    | HTTPS
    v
Dokploy / Traefik
    |
    +-- Agent Native cockpit (Node 24 / Nitro)
    |      - GitHub login + repository allowlist
    |      - CodeAgents UI + Plan/Recap links
    |      - Superboard projection
    |      - transcript/evidence projection
    |      - AGENT_PROD_CODE_EXECUTION=off
    |
    +-- Postgres 17
    |      - auth, encrypted credentials
    |      - non-authoritative run handles and transcript events
    |
    +-- authenticated command/event relay
               ^
               | outbound polling or mutually authenticated channel
               | signed, expiring, replay-protected command envelopes
               |
Private coding runner (no public ingress)
    - existing Claudex launcher/policy
    - Claude Code harness/session controller
    - one ephemeral container or VM per issue attempt
    - optional worktree inside that boundary for branch separation
    - repository/build/browser tools
    - real process-tree stop/cleanup
    - least-privilege GitHub identity
    - GitHub branch/PR/check evidence
```

### Web container

- Deploy as a normal Agent Native Node/Nitro app behind Dokploy’s Traefik/TLS.
- Use Node 24 and a pinned Agent Native version.
- Set `AGENT_PROD_CODE_EXECUTION=off`.
- Expose only authenticated UI/actions and authenticated relay endpoints.
- Bind every runner command to the exact run ID, issue URL, repository, requested action, and expected current state. Sign the complete envelope and include an expiry, nonce, and per-run monotonic sequence number.
- The runner accepts only an explicit command allowlist, rejects expired/replayed/out-of-order envelopes idempotently, and supports credential rotation and revocation.
- Use Postgres through `DATABASE_URL`; never rely on `data/app.db` in the container.
- Store only rebuildable session projections. Do not copy Superboard lifecycle status into an authoritative local table.
- Use the framework’s standard action validation, access guards, credential encryption, webhook verification, and `ssrfSafeFetch`.

### Private runner

The first runner controller should remain on the existing trusted coding machine so it can use the already-governed Claudex launcher. It makes outbound requests to the VPS; the VPS does not open SSH or a shell endpoint into the machine. Repository code and build commands execute inside an ephemeral container or VM with an explicit filesystem, network, process, and credential boundary. A Git worktree may be used inside that sandbox for checkout separation, but a worktree alone is not security isolation.

Each issue attempt receives:

- One recorded base SHA.
- One dedicated branch.
- One ephemeral container or VM, with an optional worktree inside it.
- One run ID and attempt number.
- Namespaced ports and preview resources.
- An explicit Claudex role/tool/permission result.
- Real process-tree cancellation—not merely marking the UI record stopped.
- A least-privilege GitHub identity that can create the run branch, commits, PR, checks, and evidence but has no administration or ruleset-bypass authority.

Repository rulesets must require approval from a human identity, reject self-approval by the runner identity, and have no bypass grant for that identity. The runner must not receive the Superboard adapter credential used for Project mutations. Direct merge, approval, protected-branch push, and Project-status mutation attempts by the runner must fail at GitHub, independently of Claudex policy.

A later VPS-native runner may be added as a separate container/VM with no public ingress and no mount of the Dokploy host root or Docker socket. It must reproduce Claudex policy rather than bypass it.

### Database and migrations

- Use a persistent Postgres database.
- Keep schema changes additive.
- Run idempotent migrations at startup.
- Never run `drizzle-kit push` against production.
- Include `owner_email` and `org_id` where data becomes multi-user.
- Test isolation with two accounts before any team rollout.

Sources:

- [Production deployment and Docker/VPS topology](https://github.com/BuilderIO/agent-native/blob/c24e1695a028c344a35eb58db6537cb6819156ff/packages/core/docs/content/deployment.mdx)
- [Database persistence and additive migrations](https://github.com/BuilderIO/agent-native/blob/c24e1695a028c344a35eb58db6537cb6819156ff/packages/core/docs/content/database.mdx)
- [Security and production checklist](https://github.com/BuilderIO/agent-native/blob/c24e1695a028c344a35eb58db6537cb6819156ff/packages/core/docs/content/security.mdx)

## Canonical lifecycle and evidence contract

1. The issue and binary acceptance criteria exist in GitHub.
2. A human moves the card to **Ready**.
3. The cockpit requests a run using the issue URL.
4. In one Postgres transaction, the Superboard adapter re-reads the issue/card and acquires a unique active lease for the issue. The lease is operational concurrency state, not workflow authority.
5. While holding the lease, the adapter performs Ready → Building, re-reads the Project item, and verifies Building before issuing any runner command. Failure releases the lease and starts no process.
6. The adapter emits a signed launch envelope only after that verification. The runner rejects duplicate, expired, replayed, out-of-order, or mismatched envelopes.
7. The runner creates the sandbox and launches Claude Code under Claudex.
8. The runner streams transcripts and runtime approvals to Agent Native.
9. Claude Code produces branch, commit, PR, check, and preview evidence.
10. Superboard advances Building → QA only after evidence is verified against the exact PR SHA.
11. QA records the tested SHA and binary verdict, then the card moves QA → Review.
12. A human identity approves and merges through GitHub after reviewing the exact tested SHA.
13. Review → Done occurs only after the adapter verifies the merged PR SHA and satisfied acceptance criteria.
14. On restart, the adapter reconciles operational leases against GitHub and the runner before admitting new work; it never infers canonical workflow state from a lease.

Every attempt appends a machine-readable issue comment with:

- schema version, run ID, and attempt number;
- issue, Project, and repository URLs;
- base SHA, branch URL, commit URLs, PR URL, and head SHA;
- Claudex launch receipt containing policy version, resolved role, tool profile, permission mode, sandbox ID, and runner identity without secrets;
- accepted command ID and sequence number, without signatures or secret material;
- check names, conclusions, and URLs;
- preview URL and deployed SHA;
- QA report URL and tested SHA;
- terminal outcome and timestamps.

Agent Native transcripts and screenshots are supplementary. Completion must remain provable from GitHub alone.

## Pilot

### Pilot repository

Use [`Wladefant/heylolo-hq`](https://github.com/Wladefant/heylolo-hq) for the first end-to-end pilot.

Why:

- It is a real private web application already deployed through the user’s VPS/Dokploy environment.
- It has low customer-data and transactional risk compared with a production SaaS backend.
- A visible but bounded UI/content change can prove issue → run → PR → build → preview → human review.
- Failure does not require migrating an existing SaaS data model or exposing trusted code execution publicly.

The pilot issue belongs on the HeyLolo board, not the Superboard System board. The reusable cockpit/runner implementation remains tracked in Superboard System.

### Pilot scope

- One repository.
- One Ready issue.
- One concurrent run.
- One private runner controller.
- One ephemeral container or VM, with an optional worktree inside it.
- One branch, PR, and preview.
- Human QA, approval, and merge.
- Polling/outbound relay before webhooks.
- No Dispatch control plane.
- No Clips dependency.
- No automatic merge.
- No migration of HeyLolo HQ to Agent Native internals.

### Binary acceptance criteria

The pilot passes only if all are true:

1. At admission time, a run request from Backlog, Building, QA, Review, Done, or Blocked is rejected before any runner command is emitted.
2. At admission time, the request references exactly one existing Ready issue whose body contains binary acceptance criteria.
3. Two simultaneous admission requests for the same issue produce exactly one active Postgres lease, one Building transition, and one runner launch.
4. A forced Ready → Building mutation or verification failure releases the lease and produces zero runner processes.
5. Agent Native stores no independent canonical workflow status; after each transition, its displayed status equals a fresh GitHub Project read.
6. The runner accepts only allowlisted actions whose signed envelope matches the run ID, issue URL, repository, expected state, unexpired timestamp, nonce, and next sequence number. Forged, expired, replayed, duplicated, out-of-order, and mismatched envelopes are rejected without side effects.
7. Rotating or revoking a relay credential causes commands signed with the old credential to be rejected.
8. Every launch produces a Claudex receipt containing the policy version, resolved role, tool profile, permission mode, sandbox ID, and runner identity; no launch uses Agent Native’s built-in model loop.
9. Attempting to change the resolved role, tool profile, or permission mode through Agent Native is rejected and leaves the Claudex receipt unchanged.
10. Repository code and build commands run in an ephemeral container or VM. A test process cannot read the controller’s home directory, provider credentials, unrelated repositories, host process namespace, or Docker socket.
11. A duplicate start for the same issue is rejected while its process tree or active lease exists.
12. Stop passes only when the complete process tree is terminated and its allocated ports and workspace are released. If termination fails, the attempt remains active, records the failure, and blocks replacement runs.
13. The issue receives full HTTPS links for its branch, commits, PR, checks, preview, and QA evidence.
14. The recorded PR head SHA, preview-deployed SHA, QA-tested SHA, and human-approved SHA are identical.
15. Failed, missing, pending, or cancelled required checks block Building → QA.
16. GitHub rejects direct approval, merge, protected-branch push, ruleset bypass, and Project mutation attempts made with the runner identity.
17. A distinct human identity approves and merges the exact QA-tested SHA through GitHub.
18. The card moves QA → Review after a passing QA verdict, and Review → Done only after the adapter verifies the merged SHA and every acceptance criterion.
19. Restarting the cockpit reconstructs the issue URL, Project item/status, run/attempt ID, branch, PR, head SHA, checks, preview SHA, QA-tested SHA, and terminal outcome from GitHub/runner evidence without mutating the Project card.
20. Deleting transient cockpit projections does not lose GitHub evidence and does not create a duplicate run; active leases are reconciled against the runner and Project before admission resumes.
21. The public web container has `AGENT_PROD_CODE_EXECUTION=off` and no Docker socket, controller home directory, repository checkout, or runner filesystem mount.
22. The private runner has no public inbound shell or command endpoint.
23. An unauthenticated account cannot read cockpit data or invoke an action; an authenticated but unauthorized account cannot read transcripts or control a run.
24. A repository absent from the server-side allowlist cannot be read, launched, or added by a browser-supplied parameter.
25. In a two-account test, each account is denied access to the other account’s runs, transcripts, actions, credentials, and evidence projections.
26. Canary secrets placed in runner and application environments do not appear in transcripts, action results, UI errors, evidence comments, or retained application logs.
27. Postgres data and encrypted credentials survive cockpit redeployment, while a container-filesystem reset loses no canonical work state.
28. A failed or interrupted attempt appends an explicit terminal outcome and blocker reason, cannot transition to Done, and remains distinct from any later successful attempt number.

## Staged delivery

### Phase 0 — immediate, low-risk usage

Evaluate Agent Native’s `/visual-plan` and `/visual-recap` skills in one repository. This proves whether its strongest review UX adds value before custom infrastructure is built. Do not install globally or connect the hosted service without an explicit user decision.

### Phase 1 — read-only production cockpit

- Scaffold from current upstream with exact package pins.
- Deploy the authenticated Node app and Postgres on Dokploy.
- Render Project cards, issues, PRs, checks, and existing evidence read-only.
- Add Plan/Recap links and a Code UI shell with no run-start mutation.

### Phase 2 — one governed runner

- Implement the `CodeAgentsHost`/background-run adapter.
- Add authenticated outbound runner pairing, signed replay-protected envelopes, heartbeat, transcripts, follow-ups, credential rotation, and verified process-tree stop.
- Add atomic Ready admission, ephemeral container/VM creation, optional in-sandbox worktree creation, and Claudex/Claude Code launch.
- Keep one active run globally until locking, authorization, sandboxing, and cleanup are verified.

### Phase 3 — full pilot lifecycle

- Add PR/check/preview evidence verification.
- Advance to QA through the Superboard adapter.
- Run the HeyLolo HQ pilot and verify every binary criterion.

### Phase 4 — product-level Agent Native adoption

Only after the cockpit pilot succeeds, select one SaaS product operation and implement it as a real `defineAction()` shared by UI and agents. This is the product-development layer Agent Native is designed for; it is separate from the coding-control cockpit.

## Production risks and gates

| Risk | Required control |
|---|---|
| Second source of truth | GitHub-only lifecycle; local state is rebuildable projection |
| Public shell/filesystem exposure | Code execution off in web; runner in separate private boundary |
| Weak process sandbox | Use an ephemeral container or VM; a worktree is checkout separation only and Node process isolation is insufficient |
| Relay command forgery or replay | Bind signed, expiring, sequenced commands to one run/issue/repository/action; reject replay and support revocation |
| Model-policy drift | Invoke Claudex; do not reimplement routing in Agent Native |
| Package churn | Pin exact versions and upstream commit; scheduled, reviewed upgrades |
| Unsafe permission defaults | Explicit least-privilege mode from Claudex; hide independent UI override |
| Excessive GitHub authority | Dedicated runner identity, enforced rulesets, human approval, no admin or bypass grant, separate Project credential |
| False stop state | Runner confirms complete process-tree termination and resource release before status changes or replacement runs |
| Preview mismatch | Record and verify exact deployed PR SHA |
| Secret leakage | Encrypted credential APIs, redacted events, no values in transcripts/actions |
| Tenant leaks | `owner_email`/`org_id`, access guards, two-account tests |
| Destructive migrations | Additive startup migrations; never production `drizzle-kit push` |
| Dispatch scope creep | Do not adopt Dispatch workflow, approvals, vault, or jobs as canonical |

## Final recommendation

**Build the pilot now, with the boundaries above.**

Do not deploy the old ING snapshot, do not treat Clips as the coding platform, and do not replace Superboard with Dispatch. Build a small Agent Native cockpit from current pinned upstream packages, host it on Dokploy with Postgres, and connect it to one private Claudex/Claude Code runner through an authenticated, signed, expiring, replay-protected outbound relay. Execute repository code only inside an ephemeral container or VM and enforce human approval and merge through GitHub rulesets.

This gives the user the missing visual, interactive, agent-native layer for web/SaaS coding while preserving the Superboard system that already works.