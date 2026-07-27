# Self-hosting a design gallery + pin-comment overlay on Dokploy

How to put a browsable design-prototype gallery online under your own domain, with a **self-hosted
Figma-style pin-comment overlay** so cofounders can review and comment — and the operator's comments are
**authoritative** (auto-actionable) while others' are suggestions. No third-party SaaS. Reusable across
projects. (Established on PolySimulator, 2026-06-20.)

## The two pieces

1. **The gallery + overlay server** — a single **zero-dependency Node app** (reusable code in this skill's
   `assets/design-gallery/`: `server.js`, `overlay.js`, `overlay.css`, `Dockerfile`, `README.md`). It:
   - serves the static gallery (the `docs/design` HTML files) from `GALLERY_DIR`,
   - **injects the overlay** (`<script src="/__c/overlay.js">`) into every HTML response — so you never edit
     the prototype files and it auto-applies to new ones,
   - exposes a JSON comment API under `/__c/api` (`GET/POST /threads`, `POST /threads/:id/reply`,
     `POST /threads/:id/resolve`, `DELETE /threads/:id`),
   - stores comments in a JSON file at `DB_PATH` (default `/data/comments.json`), atomic writes, in-memory cache.
2. **The Dokploy deployment** — host it under a subdomain of your own domain.

## Per-project setup

1. Copy `assets/design-gallery/` from this skill into the project repo (e.g. `tools/design-gallery/`).
   Adjust the Dockerfile's `COPY docs/design ./public` to your gallery's path if different.
2. Commit + push to the branch the Dokploy app will build.
3. Run the Dokploy REST recipe below with a project-specific subdomain, volume name, and `OWNER_KEY`.

## Dokploy deployment (REST recipe)

Infra facts (operator's Dokploy):
- API base `https://hosting.wladefant.de/api`; key in `~/.claude.json` → `mcpServers.dokploy.env.DOKPLOY_API_KEY`.
- **The Dokploy MCP tools frequently 401** (server caches the key at startup; a mid-session rotation breaks
  them). **Drive Dokploy via `curl` REST** with the key from `~/.claude.json` — that always works.
- **`*.wladefant.de` is a Cloudflare wildcard → the Hostinger Dokploy server (`145.223.98.52`).** So any
  `<name>.wladefant.de` already routes to the origin — **no DNS record needed**; just create the app + domain
  and enable https. (Decisive test: `curl http://<name>.wladefant.de` returns your app once deployed.)
- GitHub integration id (Bavariance org): `0-mOov2-Synn7Cl3JzfbP`. Hostinger server id: `4qU1cTpS6GTdr2QaZm15r`.
  PolySimulator project env id: `M8JY4FeD_oOGLHqoqqco7` (or create a project/env per `project.create`).

Steps (each is `curl -s -H "x-api-key:$KEY" -H "Content-Type: application/json" -X POST "$B/<endpoint>" -d '<json>'`):
1. `application.create` `{name, environmentId, serverId}` → returns `applicationId`.
2. `application.saveGithubProvider` `{applicationId, owner, repository, branch, githubId, buildPath:"/",
   enableSubmodules:false, triggerType:"push", watchPaths:["tools/design-gallery/**","docs/design/**"]}`
   (watchPaths so only relevant changes redeploy).
3. `application.saveBuildType` `{applicationId, buildType:"dockerfile", dockerfile:"tools/design-gallery/Dockerfile",
   dockerContextPath:"/", dockerBuildStage:null, herokuVersion:"", railpackVersion:""}`.
   **GOTCHA:** the REST zod requires `herokuVersion` AND `railpackVersion` present (empty strings are fine) even
   for a dockerfile build — omitting them returns a 400.
4. `domain.create` `{host:"<name>.wladefant.de", https:true, certificateType:"letsencrypt", stripPath:false,
   domainType:"application", applicationId, port:8080}` (`port` = the Node server's listen port).
5. `mounts.create` `{type:"volume", volumeName:"<name>-data", mountPath:"/data", serviceType:"application",
   serviceId:applicationId}`. **GOTCHA:** the field is `serviceId`, NOT `applicationId`. This volume persists
   `comments.json` across redeploys.
6. `application.saveEnvironment` `{applicationId, env:"OWNER_KEY=<random secret>"}` — the owner-comment key.
7. `application.deploy` `{applicationId, title:"..."}`.
8. **Verify** by polling the public `https://<name>.wladefant.de/` until it returns the gallery AND the
   overlay tags are injected AND the API round-trips (POST a test thread → GET → DELETE).

Cert note: the CF wildcard is proxied; enabling https/LE on the Dokploy domain adds the `:443` router and LE
issues fine through Cloudflare (the HTTP-01 challenge reaches the origin on :80). The browser sees a valid cert.

## Authorship — owner vs guest (so comments can drive automated changes)

Every comment stores `author` AND `role` ('owner' | 'guest'). Role is set **server-side**: the overlay sends
an owner key (set once in the overlay's Settings, stored in `localStorage.ds_owner_key`); the server stamps
`role:"owner"` only if it matches env `OWNER_KEY` — so guests can't fake it by typing a name.

**Convention (load-bearing):** when Claude later reads the comments to adjust the designs —
- **`role === "owner"` → authoritative; implement automatically.**
- **`role === "guest"` → a suggestion; surface it to the operator and do NOT auto-apply.**

Read the comments later via `GET /__c/api/threads?page=<encoded path>` or by reading `comments.json` on the
`/data` volume.

## Gotchas recap
- Dokploy MCP 401 → use curl REST.
- `saveBuildType` needs `herokuVersion` + `railpackVersion` (empty strings).
- `mounts.create` uses `serviceId` not `applicationId`.
- No DNS step for `*.wladefant.de` (Cloudflare wildcard already hits the origin).
- Set `watchPaths` so unrelated pushes don't rebuild the gallery.
- **Cloudflare caches `.js`/`.css` by extension** (`Cf-Cache-Status: HIT`, ~4h) even if the origin sends no
  cache header — so a redeployed `overlay.js` is served STALE to browsers while the API (uncached) is already
  new (symptom: owner-role works via API but the Settings panel is missing in the browser). Fix in the server:
  serve the overlay assets with `Cache-Control: no-store`, inject a **content-hash-versioned** URL
  (`/__c/overlay.js?v=<hash>`) so every deploy busts the cache, and serve the prototype HTML with
  `Cache-Control: no-cache`. (Already done in `assets/design-gallery/server.js`.)
