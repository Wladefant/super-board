# Deploying a new app on Dokploy

Panel: **https://hosting.wladefant.de**. This is the deploy leg of the
[new-project bootstrap](https://github.com/Wladefant/super-board/blob/main/skills/superboard-setup/SKILL.md) —
it runs **last**, after the board exists and after the repo and skills are in
place. Deploying first produces an app with no issue behind it, which the
Superboard rule forbids.

Surface ownership is the
[Agent Native operating guide](https://github.com/Wladefant/super-board/blob/main/docs/architecture/AGENT-NATIVE-OPERATING-GUIDE.md):
Dokploy holds the running container and its secret store. It does not hold work
state, and it is never evidence of completion — that stays on GitHub.

---

## The one that bites: pick the GitHub provider by repo OWNER

Dokploy has **four** GitHub providers installed. They are not interchangeable,
and choosing wrong fails in the worst possible way — silently.

| Provider | githubId | Installation | Sees |
|---|---|---|---|
| **Dokploy-2025-10-26-hostinger** | `Y4Ma48-dyFxauwE5Jo4L0` | 91677512 | **all** repos under the personal **Wladefant** account, public *and* private |
| Dokploy-Bavariance | `0-mOov2-Synn7Cl3JzfbP` | 98593141 | the **Bavariance org** only (polysimulator, oil-perps-bot) |
| Dokploy-2026-06-23-c9jnw8 | — | — | Bavariance-scoped |
| Dokploy-2026-06-04-1rgka7 | — | — | Bavariance-scoped |

**Rule: match the installation to the repo's owner.**

- Repo under **Wladefant** (personal) → **Dokploy-2025-10-26-hostinger**. Always.
- Repo under **Bavariance** (org) → a Bavariance-scoped provider.

**Why it is dangerous rather than merely wrong.** Pointing a *personal* repo at
`Dokploy-Bavariance` appears to work when the repo is public — the clone
succeeds, the build goes green, and nothing warns you. The provider has no
access to that repo at all; it is only getting away with an anonymous public
clone. Flip the repo to private, or create the next one private, and it breaks
with an access error that looks like a credential problem and is not.

**And the specific way the mistake gets made:** copying the `githubId` out of an
existing app's config because that app deploys fine. That is how a
Bavariance-scoped id ends up on a personal repo. Do not copy it from a
neighbour — call `gitProvider.getAll`, read the installation, and match it to
the repo owner yourself.

---

## Sequence

1. **The issue exists first.** Product work → that product's board. Setup and
   tooling work → https://github.com/Wladefant/super-board and
   https://github.com/users/Wladefant/projects/5. Move the card to Building
   before deploying.

2. **Project → Create Application** in the Dokploy panel, inside the project
   that owns the product.

3. **Attach the git provider** — pick it by repo owner from the table above.
   Verify the resolved installation before saving; do not inherit it from
   another app.

4. **Set the build type.** MCP quirk: `application-saveBuildType` needs
   `herokuVersion: "24"`, `railpackVersion: "0.15.4"` and
   `dockerBuildStage: ""` present *even for a dockerfile build* — the tool
   mangles empty strings. If it still 400s, call the REST API directly with
   curl rather than fighting the tool.

5. **Domain.** `*.wladefant.de` is a **Cloudflare wildcard**, so a new
   subdomain needs **no DNS step** — assign `<app>.wladefant.de` in Dokploy and
   it resolves. Add basic auth here if the app should not be open (the HeyLolo
   HQ app is the reference for that).

6. **Secrets** go in the Dokploy environment store. Never in the repo, never in
   a transcript, never echoed into a card comment.

7. **Deploy**, then confirm the container is actually serving — not merely that
   the build went green.

8. **Move the card** with the full `https://` evidence URLs.

---

## Hard constraints on this swarm

- **Never add a `HEALTHCHECK` to a Dockerfile.** On this swarm it caused a
  permanent 502 crash-loop. Plain `nginx:alpine` works for static sites with no
  healthcheck at all. If you want liveness signal, get it from outside the
  container.
- **The public Agent Native cockpit never executes repository code** —
  `AGENT_PROD_CODE_EXECUTION=off`, no Docker socket, no runner filesystem
  mount, no repo checkout, no trusted shell. Repo code runs only inside the
  private runner's ephemeral container. If a deploy seems to need the cockpit
  to run repo code, the answer is a runner command, not a relaxed flag.

## Reference deployment

`heylolo-hq` (app id `UOZlEPj_cQxZnRm9ydvS8`) in project *Elumi AI* — repo
[Wladefant/heylolo-hq](https://github.com/Wladefant/heylolo-hq), **private**,
personal provider, domains `hq.wladefant.de` + `heylolo-hq.wladefant.de`, nginx
basic auth. Copy its *shape*, not its `githubId`.
