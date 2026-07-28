# START HERE — what actually works right now

**All status in this document was observed between `2026-07-27T23:36Z` and `2026-07-27T23:40Z` (UTC)**,
except [the Design sign-in section](#a-human-can-sign-in-to-design--verified-in-a-browser) and
[the Analytics sign-in section](#a-human-can-sign-in-to-analytics--verified-in-a-browser), which
were observed in a real browser on `2026-07-28` (UTC) and are dated separately.
Every HTTP status below is a real response to a real request made in that window, not an
inference from container health.

> **Clips update, `2026-07-28T01:15Z`.** Clips was mid-deploy when the body of this document
> was written and its 502/500 readings below are **superseded**: `https://clips.wladefant.de/`
> now returns **302 → `/library` → 200**, serving 148 KB of real SSR HTML titled
> *Clips - Open Source screen recorder*, on commit
> [`834c57e69`](https://github.com/Wladefant/agent-native-platform/commit/834c57e69). All three
> apps are up. Re-check any status yourself with
> [these commands](#re-check-the-deployment-yourself).
>
> Two things this does **not** mean: nobody has signed in to Clips, and recording upload
> remains unproven — the chunk endpoint is not an action route, so it needs a real browser
> session. Container packaging defects that break frame extraction, remux and transcription
> are tracked in https://github.com/Wladefant/super-board/issues/46.

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

### A human can sign in to Design — verified in a browser

**Observed `2026-07-28` (UTC), in Chrome, against https://design.wladefant.de.** Every earlier
check in this document used a machine token derived from `A2A_SECRET`. This one did not: it is a
person-shaped sign-up, sign-out and sign-in through the actual forms.

**What the sign-in screen actually offers** (read off the page, not assumed). The root URL
redirects to `/_agent-native/sign-in`, which has exactly two tabs:

- **Create account** — email, password (min 8 chars), confirm password.
- **Sign in** — email, password, plus a *"Forgot password?"* link whose `href` is literally `#`.

There is **no Google button, no GitHub button, no SSO of any kind.** That is expected, not a
fault — see the failure-mode table below.

**What was done, in order:**

1. Created an account through the form with the synthetic address
   `superboard-probe-20260728@example.com`. **Signup returned a session immediately** — no
   verification screen, no "check your email" interstitial, no dead end. It landed straight on
   the Designs page.
2. Created a design through the UI, renamed it **"Superboard signin probe 2026-07-28"**
   (`/design/xIZFnucw7n_j2DCfZuM3j`). It appears on the Designs list after a full page load, so
   the write reached Postgres and came back.
3. **Signed out** via the account menu, then **signed back in** with the same account through the
   Sign in tab. The session was restored and the design was still listed. Sign-*in* is therefore
   proven independently of signup's auto-session.

No console errors at any step.

Two honest limits on this result:

- **Creating a design does not exercise the AI.** "New Design" opens a **Connect AI** dialog
  (Builder.io free credits, or your own provider keys); **"Skip to editor"** bypasses it and
  creates a blank design, which is the path used here. **No AI provider is configured on this
  deployment**, so prompt-driven generation — the app's headline feature — remains untested.
- **The probe account and its design still exist** on the deployment. Delete them when convenient.

#### Email verification is NOT required here — definitively

Account creation on this deployment **does not require email delivery, and no mail transport is
configured.** This is not an inference from the browser behaviour alone; the code says why:

[`better-auth-instance.ts`](https://github.com/Wladefant/agent-native-platform/blob/main/packages/core/src/server/better-auth-instance.ts)
computes `requireEmailVerification = (await isEmailConfigured()) && !shouldSkipEmailVerification()`,
and [`email.ts`](https://github.com/Wladefant/agent-native-platform/blob/main/packages/core/src/server/email.ts)
returns `isEmailConfigured() === false` unless a provider secret resolves. With no provider, the
app deliberately leaves verification off rather than locking people out of signup.

**Variable names only — no values are recorded here or anywhere in this repo.**

| Variable | Status in the Design deployment |
|---|---|
| `BETTER_AUTH_SECRET` | **set** |
| `BETTER_AUTH_URL`, `APP_URL` | **set** (both `https://design.wladefant.de`) |
| `DATABASE_URL`, `A2A_SECRET`, `SECRETS_ENCRYPTION_KEY` | **set** |
| `NODE_ENV`, `PORT`, `APP_NAME` | **set** |
| `RESEND_API_KEY`, `SENDGRID_API_KEY`, `EMAIL_FROM` | **unset** — no mail transport |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | **unset** |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | **unset** |
| `AUTH_SKIP_EMAIL_VERIFICATION` | **unset** (not needed — verification is already off) |

The four known Better Auth self-host failure modes, each checked rather than assumed:

- **Unstable secret resetting sessions on restart** — not present. `BETTER_AUTH_SECRET` is pinned
  explicitly in the deploy environment. *Caveat:* this was not empirically restart-tested, because
  a redeploy was still building when this was written. The code path is reassuring though — in
  production it throws at boot when the secret is missing rather than quietly generating an
  ephemeral one, so this failure could not have been hiding.
- **Wrong base URL breaking the callback** — not present. `BETTER_AUTH_URL` and `APP_URL` both
  match the live host exactly.
- **Email verification blocking signup with no mail transport** — not present, per above.
- **Social providers silently absent** — *present, and benign.* The missing Google/GitHub buttons
  are the direct consequence of their credentials being unset. Nothing is broken; there is simply
  no SSO until those variables are set.

### A human can sign in to Analytics — verified in a browser

**Observed `2026-07-28` (UTC), in Chrome, against https://analytics.wladefant.de.** Until now the
only evidence for Analytics was a machine token: an ingest call that returned
`202 {"success":true,"accepted":1}` and an API key whose `lastUsedAt` flipped from `null` to a
timestamp. That proved a Postgres write by a *robot*. This section is the person-shaped
equivalent — sign-up, sign-out, sign-in, and a write made and then seen through the UI.

**What the sign-in screen actually offers** (read off the page, not assumed). The root URL
redirects to `/_agent-native/sign-in?c=…`, where `c` is the base64 return path. Exactly two tabs:

- **Create account** — email, password (min 8 chars), confirm password.
- **Sign in** — email, password, plus a *"Forgot password?"* link whose `href` is literally `#`.

**No Google button, no GitHub button, no SSO of any kind** — identical to Design, and for the same
reason: those provider credentials are unset.

**What was done, in order:**

1. Created an account through the form with the synthetic address
   `analytics-probe-20260728@wladefant.de`. **Signup returned a session immediately** and landed
   on `/ask`. No verification screen, no mail interstitial.
2. **Signed out** via the account menu. Then requested a protected route (`/monitoring`) directly:
   it **redirected back to the sign-in screen**. The session is genuinely destroyed server-side —
   sign-out is not a cosmetic UI reset, and the routes really are auth-gated.
3. **Signed back in** with the same account through the Sign in tab. It succeeded *and* landed on
   `/monitoring` — the deep link that had bounced — so the `c=` return path survives the
   round-trip. **Sign-in is therefore proven independently of signup's auto-session**, which is
   the whole point: a signup that auto-sessions can otherwise hide a completely broken login.
4. **Made a real write through the UI and saw it come back.** On *Data Sources* → *First-party
   Analytics* (which started at **Not configured**), pressed **Generate Key**. The panel flipped
   to **Configured** and listed the new key as `anpk_…` **"never used"**. After a full page
   reload the green *✓ Configured* badge and the key were still there — so the write reached
   Postgres.
5. **Closed the ingest→UI loop as a human would see it.** Posting an event to the app's own
   endpoint `POST /api/analytics/track` with that key returned
   `202 {"success":true,"accepted":1}`, and on reload the key row in the UI had changed from
   **"never used"** to **"last used 7/28/2026"**. A signed-in person can therefore both cause an
   ingest and *see the application reflect it*.

No console errors at any step.

**Variable names only — no key or password value is recorded here, in this repo, or in any commit.**

#### What this does NOT prove — the honest limit

**A human still cannot look at ingested event data itself.** Every surface that would display
events is gated:

| Surface | State | Why |
|---|---|---|
| **Ask** | blocked | *"Connect AI to start chatting"* — **no AI provider configured**, same root cause as Design ([#42](https://github.com/Wladefant/super-board/issues/42)) |
| **Dashboards** | blocked | `/dashboards` **redirects to `/ask`**; dashboards are agent-built, so they inherit the same AI gate |
| **Sessions** | empty | *"Connect replay storage"* — session replay needs S3/Builder.io storage; shows *"No sessions found"* |
| **Monitoring** | empty | uptime monitors only (*"No monitors yet"*); the **Errors** view is a tab, not the route `/monitoring/errors`, which 404s |

So the verified claim is precise: **sign-in, session handling and the app→Postgres write path all
work for a real person, and the UI reflects a real ingest — but the analytics data itself is not
yet viewable, because the app's two data-rendering surfaces both require an AI provider.**

Two further findings from the same probe:

- **The pre-existing ingested event is not visible to a new account, and that is by design.**
  Analytics data and keys are **organization-scoped**. A fresh signup creates its own empty
  workspace (here *"Analytics Probe 20260728's workspace"*) whose First-party Analytics began at
  *Not configured*. The earlier machine token belongs to a different organization, so its event
  was never going to appear. This is correct multi-tenancy, not a fault — but it does mean
  **"an event was ingested" and "a human can see that event" are separate claims**, and the first
  never implied the second.
- **The endpoint the UI advertises is the vendor's, not this deployment's.** The First-party
  Analytics panel displays `Endpoint https://analytics.agent-native.com/track`. The self-hosted
  instance actually accepts events at `/api/analytics/track` on its own domain (confirmed: `202`).
  **Anyone following the UI would ship their events to Builder's hosted service instead of their
  own.** Also cosmetic but confusing: the page header still reads **"0 configured"** while the
  item beneath it reads **"Configured"**.
- **The probe account, its workspace and its generated key still exist** on the deployment.
  Delete them when convenient.

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

- **Analytics event data is not viewable by a human.** Sign-in and the app→Postgres write path are
  no longer in this bucket — both are proven in
  [section 1](#a-human-can-sign-in-to-analytics--verified-in-a-browser), and its dedicated Postgres
  (`agent-native-analytics-db`, `applicationStatus: done`) is confirmed reachable through the app.
  What remains unverified is the product's actual purpose: **no one has seen an ingested event
  rendered as data**, because *Ask* and *Dashboards* both require an AI provider and none is
  configured. *(Design left this bucket for the same reason on the same day.)*
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

- **Clips SSR is fixed — see the LIVE section.** The earlier 502/500 readings in this document
  are superseded. Retained above as history because the diagnosis is reusable.
- **`design-prototyping` pointed at `PENDING`** instead of https://design.wladefant.de.
  Fixed in https://github.com/Wladefant/super-board/commit/0acde12 and
  https://github.com/Wladefant/super-board/commit/102488f — the skill now names the live
  instance and the stale `PENDING` fallback trigger is retired.
- **Password reset on Design is a dead end.** No mail transport is configured, and `sendEmail()`
  refuses to send in production rather than logging a reset token — so the reset mail can never
  arrive. The *"Forgot password?"* link on the sign-in screen is `href="#"` and does nothing
  anyway. **A user who forgets their password cannot self-recover.** Fixing it means setting
  `RESEND_API_KEY` or `SENDGRID_API_KEY` (plus `EMAIL_FROM`) — note that doing so also switches
  email verification **on** for new signups, since the same check gates both.
- **No AI provider is configured on Design.** Design creation works without one, but
  prompt-driven generation — the reason to use the app — is untested and will prompt for
  Builder.io or provider keys on first use.
- **No AI provider is configured on Analytics either, and it costs more here.** On Design the AI
  gate blocks *generation*; on Analytics it blocks **reading your own data**. *Ask* refuses to
  chat and `/dashboards` redirects to `/ask`, so a signed-in user with successfully ingested
  events has **no way to view them**. This is the same decision already open as
  [#42](https://github.com/Wladefant/super-board/issues/42) — it should be decided for both apps
  at once, not twice.
- **The Analytics UI advertises the wrong ingest endpoint.** *Data Sources → First-party
  Analytics* shows `https://analytics.agent-native.com/track` — Builder's hosted service. The
  self-hosted instance accepts events at `/api/analytics/track` on its own domain. Following the
  UI sends your events to the vendor. The same panel also reports **"0 configured"** in its header
  while showing an item as **"Configured"**.
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

- That anyone has signed in to **Clips**. **Design** and **Analytics** both now have a verified
  human sign-in — created, signed out, signed back in, write persisted
  ([Design](#a-human-can-sign-in-to-design--verified-in-a-browser) ·
  [Analytics](#a-human-can-sign-in-to-analytics--verified-in-a-browser)). Clips does not.
- That anyone has **seen analytics data** in Analytics. A human caused an ingest and watched the
  UI reflect it (the key flipped to *"last used"*), but *Ask* and *Dashboards* are both blocked by
  the missing AI provider, so **no event has ever been rendered as data for a person**.
- That Analytics survives a container restart, or that its password reset works — the
  *"Forgot password?"* link is `href="#"` there too, and no mail transport is configured.
- That Design's AI generation works. A design was created via **Skip to editor**; no AI provider
  is configured.
- That Design's sessions survive a container restart. The secret is pinned in the environment,
  which is what makes that work, but it was not tested across a restart.
- That Clips works. At the last observation it returned HTTP 500.
- That the `clips` MinIO bucket exists or that its credentials are accepted.
- That the upstream sync is durable. One scheduled run has succeeded, once, after one failure.
- That board 4 is the right board for HeyLolo HQ cards.

Written against https://github.com/Wladefant/super-board/issues/40.
