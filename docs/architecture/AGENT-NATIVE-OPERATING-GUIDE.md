# Agent Native operating guide

**Purpose:** tell a session which surface to use for what, without the operator having to
restate it. This is the operational form of the decision in
[Agent Native production layer for Superboard and Claudex](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-SUPERBOARD-PRODUCTION.md).

One sentence: **Superboard owns work, Claudex owns policy, Claude Code owns code, GitHub
owns evidence, Agent Native owns the view and the design surface.**

## Capability → surface

| Capability | Surface | Notes |
|---|---|---|
| Issues, acceptance criteria, card status, lifecycle | **Superboard** (GitHub Project) | The only place work exists. No task outside an issue. |
| Backlog priority, milestones, labels | **Superboard** | |
| Model and provider selection, routing | **Claudex** | Agent Native displays the resolved role; it never picks one. |
| Context admission, tool profile, permission mode, escalation | **Claudex** | |
| Reading and changing repositories, running tests | **Claude Code** | The coding runtime. Agent Native renders and links to it; it never drives it and never replaces it. |
| Cockpit, run list, live session transcript | **Agent Native** | Projection only — rebuildable, never authoritative. |
| Plans before building, visual recaps after | **Agent Native** | |
| Preview embedding, follow-ups, approvals presentation | **Agent Native** | Presentation. The approval decision itself happens in GitHub. |
| UI design, variants, prototypes, design→code handoff | **Agent Native Design** | See the [`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md) skill. |
| Analytics and dashboards | **Agent Native Analytics** | |
| Clips, walkthroughs, recorded demos | **Agent Native Clips** | |
| Source, branches, commits, PRs, checks | **GitHub** | |
| Durable evidence and completion history | **GitHub** | Completion must be provable from GitHub alone. |
| Merge and approval authority | **GitHub, human identity** | |
| Secrets | Runner / Dokploy secret stores | Never displayed, never in transcripts. |

**Reversibility test.** If the Agent Native service disappeared tonight, the issue, card,
branch, PR, checks, approvals and completion history must all still be complete and
recoverable. If a change would break that test, it belongs on the left of this table, not
in Agent Native.

## What must NOT move into Agent Native

| Never | Why |
|---|---|
| A second backlog or Project status of record | Creates "Done here, Building there" states with no recovery rule. |
| The acceptance record or completion ledger | Completion must be provable from GitHub alone. |
| Merge or approval authority | Human identity in GitHub approves; the runner identity is denied at the ruleset level. |
| Global permission or model policy | That is Claudex. Duplicating it produces silent policy drift. |
| A repository credential store | Credentials belong to the runner and Dokploy secret stores. |
| Dispatch as the control plane | Dispatch's jobs, approvals, vault and workflow status duplicate Superboard and Claudex. Borrow UI ideas, not the control plane. |
| Repository code execution in the public cockpit | See the standing constraint below. |

## Standing security constraint — the public cockpit never executes repository code

This is not a phase gate. It holds for every deployment, permanently.

The public Agent Native web container has:

- `AGENT_PROD_CODE_EXECUTION=off`
- no Docker socket
- no runner filesystem mount
- no repository checkout
- no trusted shell
- **no Project write credential and no GitHub write token** — the cockpit is a
  read-only projection, so it carries no credential-shaped field at all, not even an
  empty slot (an empty credential slot today is a filled one tomorrow)

Unavailability is proved with **synthetic non-resolving targets**: a Project item ID that
does not exist and a repository command that does not exist. A probe handed a real item ID
or a real command is refused before it runs — proving that a mutation is unavailable must
never be done by attempting a mutation that could succeed. A probe that *fails* is the
positive evidence; a probe that is accepted is the finding. See
[`references/agent-native.md`](https://github.com/Wladefant/super-board/blob/main/skills/super-board/references/agent-native.md).

Repository code and build commands run **only** inside the private runner's ephemeral
container or VM, which has no public inbound shell or command endpoint and reaches the
cockpit by outbound, signed, expiring, replay-protected envelopes. A git worktree is
checkout separation, not security isolation. `AGENT_PROD_CODE_EXECUTION=trusted` is
acceptable only inside that deliberately isolated boundary — never in the web container.

If a task seems to need the cockpit to run repo code, the answer is a runner command, not
a relaxed flag.

## Deployed apps

Self-hosted instances are being stood up on `wladefant.de` subdomains; **the self-hosted
URLs are pending** and each skill reads its endpoint from one marked block rather than
hardcoding it. Upstream first-party hosted instances, verified from the Agent Native
template registry:

- Design — https://design.agent-native.com (MCP https://design.agent-native.com/mcp)
- Plan — https://plan.agent-native.com
- Analytics — https://analytics.agent-native.com
- Clips — https://clips.agent-native.com
- Assets — https://assets.agent-native.com
- Slides — https://slides.agent-native.com
- Chat — https://chat.agent-native.com
- Dispatch — https://dispatch.agent-native.com (reference only; **not** adopted as a control plane)

Auth is OAuth per app. In Claude Code: `/mcp` → Authenticate/Reconnect the connector. On
`Session terminated` or `needs auth`, stop retrying and reconnect; never reinstall to fix
auth, never hand-roll MCP calls with curl, never paste tokens into a repo.

## Applying this without being asked

Three defaults a session should follow silently:

1. Work starts by checking the board and ends by moving the card with full `https://`
   evidence URLs. Never a bare SHA or filename.
2. Any UI/visual work goes to Agent Native Design via the `design-prototyping` skill before
   production code is touched.
3. Model, tool and permission questions are answered by Claudex, not by an Agent Native
   setting and not by improvising in-session.

The routing above is embedded in the
[`superboard-setup`](https://github.com/Wladefant/super-board/blob/main/skills/superboard-setup/SKILL.md)
and
[`claudex-optimized`](https://github.com/Wladefant/super-board/blob/main/skills/claudex-optimized/SKILL.md)
skills so it applies automatically.
