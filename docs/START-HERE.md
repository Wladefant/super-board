# START HERE — what actually works right now

**All status in this document was observed between `2026-07-27T23:36Z` and `2026-07-27T23:40Z` (UTC).**
Every HTTP status below is a real response to a real request made in that window, not an
inference from container health.

> **A deployment was still in flight while this was written.** The Clips app was mid-deploy.
> Anything in this document about Clips is a snapshot of a moving target — re-check it before
> acting. See [Re-check the deployment yourself](#re-check-the-deployment-yourself) for the
> exact commands.

**The distinction this document refuses to blur:** a container reporting healthy is not a
reachable app. An app is only in the LIVE bucket if a request to its public URL returned a
success status in the window above. Nothing is promoted to LIVE on the strength of a green
dot in a dashboard.

---

## 1. LIVE AND USABLE TODAY

### Deployed apps that answered a real request

| App | URL | Observed | Domain attached | Dokploy `applicationStatus` |
|---|---|---|---|---|
| Agent Native **Design** | https://design.wladefant.de | **HTTP 200** | yes (1) | `done` |
| Agent Native **Analytics** | https://analytics.wladefant.de | **HTTP 200** (redirects to `/ask`) | yes (1) | `done` |

Evidence beyond the root page: `GET https://design.wladefant.de/api/auth/get-session` returned
`401 {"error":"Unauthorized"}`, and Analytics returned the same. A JSON 401 from the auth route
proves the Node server is running and routing — this is not a static shell or a Traefik
placeholder. Both apps have a Let's Encrypt cert and resolve over HTTPS.

Both build from the parameterized root `Dockerfile` in the platform repo via the `TEMPLATE`
build arg: https://github.com/Wladefant/agent-native-platform/blob/main/Dockerfile

### The platform repo

- https://github.com/Wladefant/agent-native-platform — private, default branch `main`,
  19 template directories under `templates/`.
- The rename is complete: `Wladefant/ing-agent-native` resolves to
  `Wladefant/agent-native-platform`. Old links redirect; nothing is broken by the rename.

### Automated upstream sync — working, with a verified green run

- Workflow: https://github.com/Wladefant/agent-native-platform/blob/main/.github/workflows/upstream-sync.yml
  ("Upstream sync (BuilderIO/agent-native)"), daily at `04:17 UTC` plus manual dispatch with a
  `dry_run` input. Merge only — never rebase, never force-push.
- Last run: **success**, `2026-07-27T22:17:32Z` —
  https://github.com/Wladefant/agent-native-platform/actions/runs/30310189664
- Honest caveat: there are only **two** runs in total, and the first one
  (https://github.com/Wladefant/agent-native-platform/actions/runs/30309927301) **failed** before
  being fixed and re-run four minutes later. One green run is evidence the workflow can pass,
  not evidence it is durable. The first unattended scheduled run is 04:17 UTC.

### Boards that are ready to take work

| Board | Title | Linked repo |
|---|---|---|
| https://github.com/users/Wladefant/projects/5 | Superboard System | `Wladefant/super-board` |
| https://github.com/users/Wladefant/projects/7 | ING QA Automation | `Wladefant/ing-qa-automation` |
| https://github.com/users/Wladefant/projects/11 | Shipnovo | `Wladefant/shipnovo` |
| https://github.com/users/Wladefant/projects/12 | Elumi AI Website | `Wladefant/elumiai-website` |
| https://github.com/users/Wladefant/projects/13 | FNSKU Warehouse Scanner | `Wladefant/FNSKUWarehouseScanner` |

Boards **10** (PolySimulator) and **8** (Thibault Consulting) have **zero** linked repositories —
see [decision 1](#decision-1--the-polysimulator-board-cannot-link-to-its-repo).

### Documentation and skills that exist and are current

- [Agent Native operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md) — capability→surface routing.
- [Agent Native production layer](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-SUPERBOARD-PRODUCTION.md) — the decision and ownership boundaries.
- [Superboard GraphQL IDs](https://github.com/Wladefant/super-board/blob/main/docs/reference/BOARD-IDS.md) — every board's node ID, Status field ID and option IDs. Use this instead of re-querying.
- [Deploying a new app on Dokploy](https://github.com/Wladefant/super-board/blob/main/docs/runbooks/DOKPLOY-NEW-APP.md) — including the GitHub-provider trap that fails silently on private repos.
- [Missing upstream dependencies](https://github.com/Wladefant/super-board/blob/main/docs/reference/MISSING-UPSTREAM-DEPENDENCIES.md) — **read before running `/super-qa`.**
- Twelve skills live under https://github.com/Wladefant/super-board/tree/main/skills — including six design skills (`design-prototyping` plus `polysim-`, `shipnovo-`, `elumiai-`, `fnsku-`, `heylolo-hq-design`).

`/super-build` is intact: its dispatcher
`skills/super-build/scripts/super-build-dispatch.sh` exists. `/super-qa`'s does not — see
[decision 2](#decision-2--super-qa-cannot-run).

---

## 2. DEPLOYED BUT UNVERIFIED

These exist and respond. Nothing below has been proven end to end.

- **Design and Analytics sign-in and database.** The 401 above proves the server answers. It
  does **not** prove Postgres connectivity, that a user can register or sign in, or that any
  write persists. No account has been created on either app. Both have a dedicated Postgres
  (`agent-native-design-db`, `agent-native-analytics-db`, both `applicationStatus: done`), but
  the app→DB path is untested.
- **MinIO object storage.** https://minio-an.wladefant.de returned **HTTP 403** at
  `2026-07-27T23:40Z`. For an S3 API root with no credentials this is the *expected* answer and
  proves TLS, DNS and routing work. It proves nothing about whether the `clips` bucket exists
  or whether the Clips app's credentials are accepted. Untested.
- **The Design app as a design surface.** The app is reachable, but
  [`design-prototyping`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md)
  still declares its self-hosted endpoint as `PENDING` (SKILL.md lines 39–40) and instructs the
  agent to fall back to the offline HTML path. **Until that file is edited, no design work will
  route to https://design.wladefant.de even though it is up.** This is a one-line fix that has
  not been made; it is listed under PENDING below, not here, because the surface is unusable in
  practice.
- **Board 4 for HeyLolo HQ cards.** `docs/README.md` itself flags this as unconfirmed. Verify
  before filing there.

---

## 3. PENDING — not finished

- **Agent Native Clips is not usable.** At `2026-07-27T23:36Z` https://clips.wladefant.de
  returned **HTTP 502**; at `23:39Z` and again at `23:40Z` it returned **HTTP 500** with body
  `Internal Server Error`. Dokploy reported `applicationStatus: running` (i.e. mid-deploy, not
  settled) while its three siblings reported `done`. A domain *is* attached
  (`clips.wladefant.de`, Let's Encrypt). **A deploy lane was still working on this as this
  document was written** — the 502→500 change within three minutes is that lane making progress.
  Re-check before concluding anything.
- **`design-prototyping` still points at `PENDING`** instead of https://design.wladefant.de.
- **Open issues on https://github.com/users/Wladefant/projects/5:**
  - https://github.com/Wladefant/super-board/issues/27 — containerize templates
  - https://github.com/Wladefant/super-board/issues/28 — deploy Design
  - https://github.com/Wladefant/super-board/issues/30 — deploy Analytics and Clips
  - https://github.com/Wladefant/super-board/issues/33 — read-only cockpit (**not started**)
  - https://github.com/Wladefant/super-board/issues/40 — this document

  Issues 27, 28 and 30 are open while their apps are partly live; treat the *observed HTTP
  status above* as authoritative over the issue state, and vice versa for Clips.
- **The Elumi AI site serves a dead `og:url`.** Verified directly: https://elumiai.com returns
  **HTTP 200**, and its HTML still contains
  `<meta property="og:url" content="https://elumi.ai" />`. `elumi.ai` does not resolve.
  Tracked at https://github.com/Wladefant/elumiai-website/issues/1 (open). Note that
  `elumiai.de` also does not resolve — **the live host is `elumiai.com`**, and the repo's
  `homepage` field is unset, which is why the correct URL is hard to find.
- **`/super-qa` cannot run at all** — see decision 2.

---

## 4. NEEDS A HUMAN DECISION

### Decision 1 — the PolySimulator board cannot link to its repo

Board https://github.com/users/Wladefant/projects/10 is titled PolySimulator and has **zero**
linked repositories. Its repo https://github.com/Bavariance/polysimulator is owned by the
**Bavariance organization**; the board is owned by the **Wladefant user**. GitHub refuses the
link outright: *"Only projects owned by the same owner as the repository can be linked."* This
is a platform constraint, not a misconfiguration — no amount of retrying fixes it.

**The tradeoff:**

- **Recreate the board under the Bavariance org.** Auto-add workflows and repo→board card
  creation start working. Cost: the board is rebuilt from scratch, its number changes (so
  `BOARD-IDS.md` and `polysim-design` need updating), and it moves out of the personal-project
  list where every other board lives.
- **Keep board 10 where it is and add cards manually.** Zero migration cost, and the board stays
  next to its siblings. Cost: no auto-add, so every card is a manual step and cards *will* be
  forgotten — which is the exact failure mode the board system exists to prevent.

**Relevant precedent:** board https://github.com/users/Wladefant/projects/8 (Thibault
Consulting) already has the same shape — zero linked repos, and `CLAUDE.md` claims it maps to
`Bavariance/thibault-consulting`, a link that was never established. So this decision is really
being made for *two* boards, and whatever is chosen should be applied to both.

### Decision 2 — `/super-qa` cannot run

Both dispatchers the skill's algorithm loops around — `scripts/super-qa-dispatch.sh` and
`scripts/super-qa-file-bug.sh` — are absent, and so is the design spec that defined their
interface (`docs/superpowers/specs/2026-05-21-super-board-design.md`). This is inherited fork
debt from `EricTechPro/super-board`, not recent damage. Full analysis:
https://github.com/Wladefant/super-board/blob/main/docs/reference/MISSING-UPSTREAM-DEPENDENCIES.md

The skill has already been hardened to **halt loudly** rather than fail halfway
(https://github.com/Wladefant/super-board/issues/39, closed), so nothing silently pretends to QA.

**The tradeoff:**

- **Reconstruct the dispatchers.** `/super-qa` runs again. Cost: the spec that defined the
  contract is itself missing, so any reconstruction is a guess wearing the costume of an
  official interface — and it destroys the current honest signal that something is wrong.
- **Leave it halting.** No false confidence, and the gap stays visible. Cost: no automated QA
  lane; QA is manual until someone writes a *new* spec on purpose.
- **Write a fresh spec first, then implement to it.** The only path that produces a trustworthy
  `/super-qa`. Cost: real design work, not a patch.

### Decision 3 — the four pipeline skills are copies and will drift

https://github.com/Wladefant/super-board/blob/main/install.sh lines 25–33 install
`super-board`, `super-build`, `super-qa` and `super-review` into a target project with
`cp -R`. There is no `ln -s` anywhere in the script. Every installed project therefore holds a
**frozen snapshot**: a fix made in this repo never reaches it, and a fix made in a project never
comes back.

**The tradeoff:**

- **Symlink instead of copy.** Updates propagate instantly. Cost: projects become coupled to
  this checkout's path, break if it moves, are not portable to another machine or to CI, and a
  breaking change hits every project at once with no staging.
- **Keep copying, add a version stamp and a drift check.** Projects stay self-contained and
  updates stay deliberate. Cost: someone must actually run the re-install, and a stale copy is
  invisible until it misbehaves.
- **Keep copying, change nothing.** Zero work now; guaranteed silent divergence later.

### Decision 4 — the stale second clone at `C:\Users\wkiri\.claude\super-board-dns-17`

This is a second working copy of the same repo on branch `docs/issue-17-dns-recurrence`,
**40 commits behind `origin/main`**, carrying its own older copies of the skills — so an agent
that opens the wrong directory reads outdated skill text.

**I checked whether it holds anything unique, and it does not.** Findings:

- Two commits are not on `main` (`9219f7f` "record MSI Center DNS exhaustion incident",
  `47827e6` "correct the MSI service/process lineage direction").
- However their *content* is already on `main`: `docs/_session/dns-resolver-recurrence-2026-07/PRIOR-SESSION-FINDINGS.md`
  is **byte-identical** between this branch and `origin/main` (`git diff origin/main HEAD -- <that file>`
  produced no output). The work was carried over; only the commit objects differ.
- Its `docs/README.md` is strictly *behind* main by 39 lines, containing nothing main lacks.
- Working tree is completely clean: **zero modified files, zero untracked files.**

**Conclusion: nothing would be lost by deleting it.** I did not delete it — that is the
operator's call.

**The tradeoff:** deleting removes the drift trap permanently, at the cost of losing two commit
objects whose content is already preserved on `main`. Keeping it costs nothing today and
everything on the day an agent reads a stale skill from it without noticing.

### Decision 5 — the dead `og:url` on the live Elumi AI site

https://elumiai.com is live (HTTP 200) and its `og:url` and canonical still point at
`https://elumi.ai`, which does not resolve. Every share link and every crawler currently gets a
dead canonical. https://github.com/Wladefant/elumiai-website/issues/1

**The tradeoff:** point the canonical at `elumiai.com` (free, immediate, correct today) versus
registering `elumi.ai` and redirecting it (costs money and DNS setup, but keeps the shorter
brand domain viable). The issue cannot be closed by a code change alone until that brand
question is answered — **but the canonical should be corrected to `elumiai.com` regardless**,
because a dead canonical is strictly worse than a temporary one.

---

## The shortest real path to starting work on a product tomorrow

1. **Open the product's board** and read the top of `Ready`.
   PolySimulator https://github.com/users/Wladefant/projects/10 ·
   Shipnovo https://github.com/users/Wladefant/projects/11 ·
   Elumi AI https://github.com/users/Wladefant/projects/12 ·
   FNSKU https://github.com/users/Wladefant/projects/13 ·
   ING QA https://github.com/users/Wladefant/projects/7 ·
   System/tooling https://github.com/users/Wladefant/projects/5
   *For PolySimulator only:* the board has no linked repo, so file the issue in
   https://github.com/Bavariance/polysimulator first and add the card by node ID.
2. **File or pick an issue, then move its card to `Building`** before any work starts. Field and
   option IDs are already looked up in
   [BOARD-IDS.md](https://github.com/Wladefant/super-board/blob/main/docs/reference/BOARD-IDS.md) —
   use them with `gh api graphql` / `updateProjectV2ItemFieldValue`; do not re-derive them.
3. **If the work is UI**, load `design-prototyping` plus that product's design skill
   (`polysim-design`, `shipnovo-design`, `elumiai-design`, `fnsku-design`, `heylolo-hq-design`).
   Right now it will use the offline HTML path — change the `PENDING` line in
   [`design-prototyping/SKILL.md`](https://github.com/Wladefant/super-board/blob/main/skills/design-prototyping/SKILL.md)
   to `https://design.wladefant.de` first if you want the hosted app.
4. **Implement.** `/super-build` works. **`/super-qa` does not** — QA manually and do not wait
   for it.
5. **Move the card and comment the evidence** as full `https://` URLs.

**For a brand-new project**, start at
[`superboard-setup`](https://github.com/Wladefant/super-board/blob/main/skills/superboard-setup/SKILL.md);
if it needs a deploy, follow
[DOKPLOY-NEW-APP.md](https://github.com/Wladefant/super-board/blob/main/docs/runbooks/DOKPLOY-NEW-APP.md)
and pick the GitHub provider by **repo owner** — the wrong provider clones fine for public repos
and fails silently for private ones.

---

## Re-check the deployment yourself

This document's deployment section has a shelf life of minutes. Verify it rather than trusting it:

```bash
# The only question that matters: does the URL answer?
for u in https://design.wladefant.de https://analytics.wladefant.de https://clips.wladefant.de; do
  echo "$u -> $(curl -s -o /dev/null -w '%{http_code}' -m 20 -L $u)"
done

# Deeper than the root page: a JSON 401 here means the Node server is really running.
curl -s -w '\nHTTP %{http_code}\n' https://design.wladefant.de/api/auth/get-session
```

For Dokploy state, read the project (read-only) with the Dokploy MCP:
`project-one` with `projectId = mtRLA9hkop95jktJchjHD` (project "Agent Native"), then
`domain-byApplicationId` per app. Check **two** fields together — `applicationStatus` **and**
whether the app has a domain. Either one alone will mislead you.

Application IDs, for direct lookup:

| App | applicationId |
|---|---|
| `agent-native-design` | `XKYD5oYD0w8iShOOajzlc` |
| `agent-native-analytics` | `drH4XsXW_y0elNJ4N2Sw6` |
| `agent-native-clips` | `XNh-c8F6VmcsHx17m9pUd` |
| `agent-native-minio` | `fL1pqD9ly8VN1JPMOXlPR` |

---

## What this document does not claim

- That anyone has successfully signed in to any deployed app. No one has.
- That Clips works. At the last observation it returned HTTP 500.
- That the `clips` MinIO bucket exists or that its credentials are accepted.
- That the upstream sync is durable. One scheduled run has succeeded, once, after one failure.
- That board 4 is the right board for HeyLolo HQ cards.

Written against https://github.com/Wladefant/super-board/issues/40.
