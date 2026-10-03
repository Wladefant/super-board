# PolySimulator Superboard Enrollment & System Integration Specification

**Tracking Epic:** [Wladefant/super-board#49](https://github.com/Wladefant/super-board/issues/49)  
**Parent Implementation Plan:** [2026-08-01-polysimulator-superboard-implementation.md](https://github.com/Bavariance/polysimulator/blob/aae2cebf6af55698306db1de8b1abd9a32d610a7/docs/superpowers/plans/2026-08-01-polysimulator-superboard-implementation.md)  
**System Specification:** [2026-08-01-polysimulator-superboard-system-design.md](https://github.com/Bavariance/polysimulator/blob/docs/polysimulator-superboard-design/docs/superpowers/specs/2026-08-01-polysimulator-superboard-system-design.md) (PR [#2386](https://github.com/Bavariance/polysimulator/pull/2386))  
**Date:** 2026-09-29  
**Author:** Wladimir Kirjanovs <wladefant@gmail.com>  
**Status:** Integrated & Operational  

---

## 1. Executive Summary & Objective

This document formalizes the full start-to-finish Superboard setup, board lifecycle enrollment, and shared-runtime system integration for the [Bavariance/polysimulator](https://github.com/Bavariance/polysimulator) repository.

Historical context:
Before integration, two divergent project boards existed:
1. **The Official Repository Board**: [Bavariance/projects/1](https://github.com/orgs/Bavariance/projects/1) — containing 561+ active and historical items, but restricted to an obsolete three-status column model (`Todo / In progress / Done`).
2. **The Personal Duplicate Board**: [Wladefant/projects/10](https://github.com/users/Wladefant/projects/10) — configured with the canonical seven statuses, but unable to link directly to the organization-owned repository.

Under Epic [#49](https://github.com/Wladefant/super-board/issues/49), the repository-linked organization board was upgraded in-place to the canonical seven-status model, 588 items were non-destructively migrated and classified, Project 10 was redirected and retired, and Superboard 2.0.1 was pinned and installed into PolySimulator.

---

## 2. Multi-Board Topology & Architectural Boundaries

To avoid cross-project contamination while maintaining global program visibility, Superboard enforces strict multi-board partitioning:

```
+-------------------------------------------------------------------------+
|                  Master Board (Wladefant/projects/6)                    |
|             High-level program epics across all repositories            |
+------------------------------------+------------------------------------+
                                     |
               +---------------------+---------------------+
               |                                           |
               v                                           v
+-----------------------------+             +-----------------------------+
|    PolySimulator Board      |             |      Superboard Project     |
|   (Bavariance/projects/1)   |             |    (Wladefant/projects/5)   |
| Granular product, backend,  |             |  Harness, runtime tooling,  |
| UI, docs, and trading tasks |             |  workflows, model routing   |
+-----------------------------+             +-----------------------------+
```

1. **PolySimulator Product Board ([Bavariance/projects/1](https://github.com/orgs/Bavariance/projects/1))**:
   - The authoritative surface for all PolySimulator issues and PRs.
   - Pinned views: `Ready`, `Building`, `QA`, `Review`, `Blocked`, `Backlog`, `Done`.
2. **Master Board ([Wladefant/projects/6](https://github.com/users/Wladefant/projects/6))**:
   - Enrolls only single abstract program epics (e.g. "PolySimulator Superboard Program"), never granular product issues.
3. **Superboard Meta Board ([Wladefant/projects/5](https://github.com/users/Wladefant/projects/5))**:
   - Manages cross-repository governance, harness engine development (`veyyon`), and operational skills.
4. **Retired Duplicate ([Wladefant/projects/10](https://github.com/users/Wladefant/projects/10))**:
   - Closed and redirected via [Bavariance/polysimulator#2405](https://github.com/Bavariance/polysimulator/issues/2405).

---

## 3. In-Place Board Migration & Classification Census

The migration of `Bavariance/projects/1` executed non-destructively without losing history or renumbering item IDs:

- **Volume Reconciled**: 588 items in, 588 items out; zero dropped cards.
- **Status Renaming**:
  - Legacy `Todo` → Canonical `Backlog`
  - Legacy `In progress` → Canonical `Building`
  - Legacy `Done` → Canonical `Done` (preserved historical items)
  - Newly added: `Ready`, `QA`, `Review`, `Blocked`
- **Field Additions & Harmonization**:
  - `Test Area`: Seven core domains (Platform & Data, Public API & B2B, Markets & Trading, User & Auth, Design & Estate, Integrations, Infrastructure).
  - `Priority`: Normalized to `P1 / P2 / P3`.
  - `Effort (tokens)`: Replaced legacy estimate fields.
  - `Target Date`: Standardized ISO date field.
- **Initial Classification Snapshot**:
  - 181 active items classified with Test Area and Priority during initial pass.
  - Dispatch constraint established: **The dispatcher ONLY selects cards in `Ready`**. The system will never promote a card from `Backlog` to `Ready` autonomously; human operator approval is the required gate.

---

## 4. Shared Runtime Installation & Safety Proofs

PolySimulator installed Superboard version **2.0.1** via [PR #67](https://github.com/Wladefant/super-board/pull/67) and [PR #68](https://github.com/Wladefant/super-board/pull/68) (`145869ca1e75565cb1549af92dadc740779e6d65`).

Seven automated safety proofs were executed against the live board and repository tree:

| Proof ID | Verification Target | Observed Result | Verdict |
|---|---|---|---|
| **P1** | Non-`Ready` card refusal | Dispatcher refuses `Backlog`, `Building`, `QA`, `Review` cards | **PASS** |
| **P2** | Label exclusions | Pinned labels (`design`, `operator`, `blocked`) skip dispatch | **PASS** |
| **P3** | GraphQL quota protection | Bounded pagination prevents secondary GitHub rate-limit exhaustion | **PASS** |
| **P4** | Exact-SHA QA binding | Evidence receipts validate exact 40-character commit hashes | **PASS** |
| **P5** | Merge gate validation | Local test and lint passes required before merge-commit execution | **PASS** |
| **P6** | Agent Native isolation | Non-overlapping file boundaries enforced per parallel worktree | **PASS** |
| **P7** | Machine authentication | Fine-grained PAT / `gh` CLI operates securely without credential leakage | **PASS** |

---

## 5. Ongoing Governance & Sub-Issue Status

Under Superboard's **Adopt-or-Reject Policy** (AGENTS.md §5), parent issue [#49](https://github.com/Wladefant/super-board/issues/49) was automatically reopened by `adoption_audit.py` due to one remaining open sub-issue:
- [Bavariance/polysimulator#2396](https://github.com/Bavariance/polysimulator/issues/2396): *Consolidate the duplicated polysim-design skill without losing the token reference*.

**Current Status & Operating Contract:**
1. The shared Superboard runtime, lifecycle gates, and board taxonomy are 100% active and operational in PolySimulator.
2. Sub-issue #2396 is non-blocking (the `design` label is permanently non-dispatchable) and is explicitly scheduled for resolution during a natural pause in the design track.
3. Once #2396 completes the deduplication of `.claude/skills/polysim-design/` against `skills/polysim-design/`, parent issue #49 will finalize formal closure.
