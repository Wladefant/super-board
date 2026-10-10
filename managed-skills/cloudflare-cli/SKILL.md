---
name: cloudflare-cli
description: "Use the new unified Cloudflare CLI cf (npm package cf, technical preview) for DNS, Pages, Workers, R2, D1 and Tunnel: install, token auth from shared-auth via the cfa wrapper, commands, free-tier cost notes and safety rules"
---

> Source of truth: [`managed-skills/cloudflare-cli/SKILL.md`](https://github.com/Wladefant/super-board/blob/main/managed-skills/cloudflare-cli/SKILL.md) in Wladefant/super-board. Edit it there and merge. Then `python scripts/install-managed-skills.py cloudflare-cli` copies it to `~/.veyyon/profiles/default/agent/managed-skills/`. The installer backs up a local edit and then overwrites it.

# Cloudflare CLI (`cf`)

Policy: profile AGENTS.md §3, "Cheapest, Cloudflare First, No Supabase, All Projects".

## What it is
- `cf` is the new Cloudflare-wide CLI, announced as a technical preview. It is the next Wrangler, rebuilt on generated schemas. Source: https://blog.cloudflare.com/cf-cli-local-explorer/
- Installed globally: `npm install -g cf@1.0.0-beta.12` (pinned). Check: `cf --version`.
- It covers about 3,000 API operations (zones, dns, r2, d1, kv, pages, workers, zero-trust, ...).
- `cf deploy`, `cf dev`, `cf build`, `cf migrate` cover the Wrangler project flow. `wrangler` is not installed. Use `npx wrangler@4` only for a Wrangler feature that `cf` lacks.
- Preview quirks: no `--json` flag (output is already JSON). On Windows a non-zero exit may print `Assertion failed ... async.c`; ignore it.

## Auth
- Never use the Global API Key. Use a scoped API token.
- Default for `cf`/lanes (2026-10-10): token file `~/.veyyon/shared-auth/cloudflare_veyyon_lanes_token.txt` (scoped token `veyyon-lanes`, id 59322736312a2e497495d9c9dbdaae36). Scope: all zones in account 259beca7f28fdcd483c1ff85104c21f4 - Zone Read, DNS Write, Zone DNS Settings Write, Zone Settings Write, Cache Purge, Cache Settings Write, Analytics Read, Email Routing Rules Read; account-level - Account Analytics Read, R2 Storage Read+Write, Workers Scripts Read+Write, Pages Read+Write, Email Routing Addresses Read. Web Analytics (RUM) is NOT in it: this account has no Web Analytics permission group, so RUM endpoints need the account-level token below.
- The wrapper `cfa` (in `%LOCALAPPDATA%\Microsoft\WinGet\Links`) loads the default token file into `CLOUDFLARE_API_TOKEN` and runs `cf`. Use `cfa <args>`. Set `CF_TOKEN_FILE=<path>` to run one command with a different token file. If the file is missing, `cfa` exits 2.
- Account-level token (`completeaccess veyyon`, verified active 2026-10-10): `~/.veyyon/shared-auth/cloudflare_account_token_full.txt`. Full access to the account and all its zones. ACCOUNT-LEVEL USE ONLY: Web Analytics (RUM), R2 S3 (creds: `cloudflare_r2_s3_full.json`), Pages, Workers, and creating scoped tokens. WARNING: it can delete zones, DNS records and buckets - never run a DNS/zone/bucket deletion with it.
- Legacy default (kept, not the default anymore): `cloudflare_api_token.txt` (old account-wide token, operator-created). Also `cloudflare_staging_dns_token.txt` = DNS-edit token for polysimulator.com only.
- `cf auth whoami` shows `tokenValid`. An account-scoped token may show `accounts: []` when the token lacks `Account Settings: Read`; add that scope.
- Required scopes for a new lane token: Zone: Zone Read, DNS Write, Zone DNS Settings Write, Zone Settings Write, Cache Purge, Cache Settings Write; Account: Account Analytics Read, Workers R2 Storage Read/Write, Workers Scripts Read/Write, Pages Read/Write. (Web Analytics has no permission group in this account.)
- Create the token at https://dash.cloudflare.com/profile/api-tokens ("Create Token", custom), then save the value as the token file above. Never print it. All three token files and the R2 S3 creds are stored in Bitwarden US (`bw-us`, items named `Cloudflare ... - completeaccess veyyon` / `Cloudflare API token - veyyon-lanes`).
- wladefant.de is on Cloudflare (NS aspen/grant.ns.cloudflare.com, proxied), checked 2026-10-05. Account 259beca7f28fdcd483c1ff85104c21f4 hosts shipnovo.app and related domains (2026-10-10).

## Common commands (run `cf <group> --help` first; most write commands accept `--dry-run`)
- Zones: `cfa zones list --name wladefant.de`
- DNS: `cfa dns records list -z wladefant.de`; create with `cfa dns records create -z wladefant.de ... --dry-run` first.
- Pages and Workers: `cfa deploy` in a project dir; `cfa pages --help`, `cfa workers --help`.
- R2: `cfa r2 buckets list`.
- D1: `cfa d1 list`.
- Tunnel: `cfa zero-trust --help` (tunnels live under zero-trust).
- API schema for any command: `cfa schema <command..>`.

## Cost notes (free tier; verify at https://developers.cloudflare.com/workers/platform/pricing/)
- Workers Free: 100k requests/day. Pages Free: 500 builds/month, unlimited static requests.
- R2: 10 GB storage free, no egress fees. D1 Free: 5 GB, 5M reads/day. KV Free: 100k reads/day.
- Tunnel and DNS are free. Prefer them over paid hosting (cheapest first).

## Safety rules
- PolySimulator production stays off-limits, including polysimulator.com DNS.
- DNS and Workers changes in a live account: read first, `--dry-run`, then write. Record the change on the work-item issue.
- Never delete a zone, bucket, database or record set without asking the operator first.
- No token in logs, issues or commits. Redact before posting output.
- No visible windows: Python uses `CREATE_NO_WINDOW`, Node uses `windowsHide: true`. No `| tail`.
- Every command carries a timeout.
