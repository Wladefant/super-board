# Agent Native — read-only projection

> **Contract:** `payload/agent-native/super-board.json`, checked by
> `super_board_runtime.agent_native.evaluate_agent_native_payload`.
> Deployed evidence: `docs/architecture/AGENT-NATIVE-DEPLOYED-EVIDENCE.md`.

The cockpit renders the board and the design work. It is a window, and it is
**never a completion ledger**.

## Why the line is drawn here

The moment a window can also move a card, two things own the lifecycle and
neither can be trusted about it. A status set from the cockpit has no
compare-before-mutate record behind it, no QA linkage, and no merge evidence —
but on the board it looks exactly like one that does. A month later nobody can
tell which `Done` cards were reviewed and which were dragged.

The same argument rules out a second completion or lifecycle ledger. Two ledgers
disagree eventually, and then "is this done?" has two answers and no tiebreak.
The board is the ledger. The cockpit shows it.

## What the payload may declare

Permitted:

- **design presentation** — prototypes, boards, variants, snapshots;
- **read-only Project projection** — items, statuses, and field values as read
  from a paginated snapshot.

Off for PolySimulator: `plan`, `analytics`, `clips`.

Refused outright, wherever they appear in the payload:

- repository command execution, a trusted shell, a Docker socket, a runner
  filesystem mount, or a repository checkout;
- any credential-shaped field — an empty credential slot today is a filled one
  tomorrow;
- branch changes, pull-request creation, and pull-request merge verbs;
- Project mutation verbs (`addProjectV2ItemById`, `updateProjectV2ItemFieldValue`,
  `deleteProjectV2Item`, and anything shaped like them);
- a second completion or lifecycle ledger under any name.

Cockpit output is rendered from **sanitized read-only snapshots only**. It goes
through the same publication boundary as anything bound for GitHub, because
board text is written by humans and tooling and either can paste a credential
into it.

## The deployed cockpit

Static payload checks are necessary and not sufficient — a payload can declare
anything. The deployed cockpit must additionally hold:

- `AGENT_PROD_CODE_EXECUTION=off`;
- **no Project write credential** and no GitHub write token;
- no Docker socket, no runner filesystem mount, no repository checkout, and no
  trusted shell.

Unavailability is proved with **synthetic non-resolving targets**: a Project
item ID that does not exist and a repository command that does not exist. A
probe handed a real item ID or a real command is refused before it runs —
proving a mutation is unavailable must never be done by attempting a mutation
that could succeed. A probe that fails is the evidence; a probe that is accepted
is the finding.

## One correction that keeps coming back

**GitHub Apps cannot access personal (user-owned) Projects v2 at all.** Neither
can a fine-grained PAT be substituted for the classic-scope identity the runtime
requires. Any runbook that sends an operator to install an App for a personal
board sends them down a path that cannot work, and the failure reads like a
permissions problem rather than an impossibility.
`scan_stale_projects_guidance` fails the suite if that claim reappears anywhere
in the tree.
