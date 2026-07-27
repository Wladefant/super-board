# Superboard GraphQL IDs — single lookup table

Every Superboard, with the node and field IDs needed to add cards and move status
without rediscovering them. All boards belong to the **user** `Wladefant`
(node `U_kgDOBL7E1Q`; the legacy form `MDQ6VXNlcjc5NjExMDkz` still works but the API
now returns a deprecation warning for it).

Verified against the live API on 2026-07-28 by re-reading every board after mutation.

## Why the first three option IDs are identical everywhere

A new ProjectV2 is born with a Status field holding exactly three options —
`Todo` / `In Progress` / `Done` — whose IDs are always `f75ad846`, `47fc9ee4`,
`98236657`. The house pattern renames those three in place into
Backlog / Ready / Building and appends the remaining four, so **Backlog, Ready and
Building share the same IDs on every board** while QA, Review, Done and Blocked are
unique per board. Do not assume the last four; look them up here.

## Boards

| # | Board | Project node ID | Status field ID | Linked repos |
|---|-------|-----------------|-----------------|--------------|
| [3](https://github.com/users/Wladefant/projects/3) | Soundcore Second Brain | `PVT_kwHOBL7E1c4Bd4lf` | `PVTSSF_lAHOBL7E1c4Bd4lfzhYW1DM` | Wladefant/soundcore-work-workflow |
| [4](https://github.com/users/Wladefant/projects/4) | HeyLolo | `PVT_kwHOBL7E1c4Bd5Gu` | `PVTSSF_lAHOBL7E1c4Bd5GuzhYXSRo` | Wladefant/heylolo-api, Wladefant/heylolo-app |
| [5](https://github.com/users/Wladefant/projects/5) | Superboard System | `PVT_kwHOBL7E1c4Bd5R1` | `PVTSSF_lAHOBL7E1c4Bd5R1zhYXcJc` | Wladefant/super-board |
| [6](https://github.com/users/Wladefant/projects/6) | Master Board | `PVT_kwHOBL7E1c4Bd5SF` | `PVTSSF_lAHOBL7E1c4Bd5SFzhYXcXQ` | super-board, soundcore-work-workflow, heylolo-api, heylolo-app |
| [7](https://github.com/users/Wladefant/projects/7) | ING QA Automation | `PVT_kwHOBL7E1c4Bd5mk` | `PVTSSF_lAHOBL7E1c4Bd5mkzhYXurc` | Wladefant/ing-qa-automation |
| [8](https://github.com/users/Wladefant/projects/8) | Thibault Consulting | `PVT_kwHOBL7E1c4BeBUb` | `PVTSSF_lAHOBL7E1c4BeBUbzhYelYY` | (none linked) |
| [10](https://github.com/users/Wladefant/projects/10) | PolySimulator | `PVT_kwHOBL7E1c4Beofu` | `PVTSSF_lAHOBL7E1c4BeofuzhZBOrU` | (none — cross-owner, see below) |
| [11](https://github.com/users/Wladefant/projects/11) | Shipnovo | `PVT_kwHOBL7E1c4Beofv` | `PVTSSF_lAHOBL7E1c4BeofvzhZBOsI` | Wladefant/shipnovo |
| [12](https://github.com/users/Wladefant/projects/12) | Elumi AI Website | `PVT_kwHOBL7E1c4Beofw` | `PVTSSF_lAHOBL7E1c4BeofwzhZBOs8` | Wladefant/elumiai-website |
| [13](https://github.com/users/Wladefant/projects/13) | FNSKU Warehouse Scanner | `PVT_kwHOBL7E1c4Beofx` | `PVTSSF_lAHOBL7E1c4BeofxzhZBOtw` | Wladefant/FNSKUWarehouseScanner |

## Status option IDs

Order is the house order: Backlog → Ready → Building → QA → Review → Done, with
Blocked as the side lane for human input.

| Board | Backlog | Ready | Building | QA | Review | Done | Blocked |
|-------|---------|-------|----------|----|--------|------|---------|
| 3 Soundcore | `f75ad846` | `47fc9ee4` | `98236657` | `37828d24` | `e53db94c` | `0864266e` | `4c878c54` |
| 4 HeyLolo | `f75ad846` | `47fc9ee4` | `98236657` | `0c145236` | `ef50c73b` | `34276785` | `23213ed6` |
| 5 Superboard System | `f75ad846` | `47fc9ee4` | `98236657` | `66d9c698` | `bd9bb996` | `7dea3814` | `239f44e7` |
| 6 Master | `f75ad846` | `47fc9ee4` | `98236657` | `f65a95b3` | `33df03d0` | `4acd15c3` | `ab393a52` |
| 7 ing | `f75ad846` | `47fc9ee4` | `98236657` | `364cbc8f` | `9bf3d984` | `d8dcc1e3` | `8035ca10` |
| 8 Thibault | `f75ad846` | `47fc9ee4` | `98236657` | `b6d016c6` | `3bb790bf` | `7fe023a7` | `0a54e6c8` |
| 10 PolySimulator | `f75ad846` | `47fc9ee4` | `98236657` | `5de74f26` | `a01edbeb` | `5814971c` | `b5d6975c` |
| 11 Shipnovo | `f75ad846` | `47fc9ee4` | `98236657` | `385a2862` | `1e892abc` | `366606f2` | `d7f17b89` |
| 12 Elumi AI Website | `f75ad846` | `47fc9ee4` | `98236657` | `fb824b82` | `43401ea2` | `a0289e5c` | `bab47200` |
| 13 FNSKU | `f75ad846` | `47fc9ee4` | `98236657` | `b7a37493` | `d24c975e` | `e5ecb357` | `1c2a019f` |

## Repository node IDs

| Repo | Node ID |
|------|---------|
| Bavariance/polysimulator | `R_kgDOQXg0Lg` |
| Wladefant/shipnovo | `R_kgDOS-Tbhw` |
| Wladefant/elumiai-website | `R_kgDOTNUZEA` |
| Wladefant/FNSKUWarehouseScanner | `R_kgDOQhDJ6A` |

## PolySimulator is deliberately not repo-linked

`Bavariance/polysimulator` lives under the **Bavariance org** while board 10 is owned
by the **Wladefant user**. `linkProjectV2ToRepository` refuses this, verbatim:

```
Only projects owned by the same owner as the repository can be linked.
```

The board is fully usable — issues from any repo can still be added to it by node ID
via `addProjectV2ItemById`; only the repo-sidebar link is unavailable. Making the link
possible would mean recreating the board under the Bavariance org, which is a
deliberate ownership decision, not a workaround to apply silently. Board 8 (Thibault)
is likewise unlinked, so an unlinked board is not anomalous.

## Two traps when mutating these

**Option IDs must be passed as explicit strings.** IDs like `98236657` look numeric.
With `gh api graphql -F key=98236657` the value is coerced to a JSON number and the
mutation fails or silently mis-targets. Use `-f` (always string), or inline the IDs as
quoted literals in the query body.

**`updateProjectV2Field` replaces the whole option set.** Any existing option you omit
from `singleSelectOptions` is deleted, taking its cards' status with it. Always send
the complete list of seven, each existing option carrying its own `id` so it is renamed
in place rather than destroyed and recreated under a new ID.

Working shape:

```graphql
mutation($field:ID!) {
  updateProjectV2Field(input:{
    fieldId: $field
    singleSelectOptions: [
      { id: "f75ad846", name: "Backlog",  color: GREEN,  description: "not started" }
      { id: "47fc9ee4", name: "Ready",    color: YELLOW, description: "approved and ready to be picked up by a worker" }
      { id: "98236657", name: "Building", color: PURPLE, description: "a worker is actively implementing" }
      { name: "QA",      color: YELLOW, description: "implementation done, under test" }
      { name: "Review",  color: ORANGE, description: "awaiting human/code review" }
      { name: "Done",    color: PURPLE, description: "merged and complete" }
      { name: "Blocked", color: ORANGE, description: "cannot proceed until something is unblocked" }
    ]
  }) { projectV2Field { ... on ProjectV2SingleSelectField { id options { id name } } } }
}
```

Re-read the board after any mutation; the mutation's own return value is not
sufficient evidence that the board is in the intended state.
