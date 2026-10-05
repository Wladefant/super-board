# Hosting manifest (`hosting.json`)

Every project repo carries one `hosting.json` in its root. It says where the project runs, what it costs and what it still needs.
The audit `workflows/portable/hosting_audit.py` reads it every day and compares it with live Dokploy and Cloudflare.
Target: every project runs on Cloudflare and our own Dokploy servers, and Supabase is phased out.

## Schema (version 1)

```json
{
  "schema": 1,
  "project": "shipnovo",
  "stage": "live",
  "dns": { "provider": "cloudflare", "zones": ["example.de"] },
  "components": [
    {
      "name": "shipnovo-app",
      "kind": "web",
      "host": "dokploy-app",
      "url": "https://app.example.de",
      "dokploy_app_id": "ASRfSqAF...",
      "monthly_cost_usd": 4.5
    }
  ],
  "supabase": { "in_use": true, "projects": ["abcdefghij"], "used_for": ["auth", "db"] },
  "next_migration_step": "Move auth to our own Postgres on Dokploy.",
  "exempt": null
}
```

| Field | Type | Rule |
|---|---|---|
| `schema` | number | Must be `1`. |
| `project` | string | Unique across all manifests. |
| `stage` | `greenfield` or `live` | Same meaning as `Project stage:` in the repo AGENTS.md. |
| `dns.provider` | `cloudflare`, `hostinger`, `other`, `none` | Who answers DNS for the project. |
| `dns.zones` | list of strings | Optional. |
| `components[].name` | string | Unique inside the manifest. For Cloudflare it must equal the resource name. |
| `components[].kind` | `web`, `api`, `db`, `worker`, `static`, `storage`, `other` | |
| `components[].host` | `cloudflare-pages`, `cloudflare-workers`, `cloudflare-r2`, `cloudflare-d1`, `dokploy-app`, `dokploy-db`, `supabase`, `other` | |
| `components[].url` | string or `null` | Public URL. The audit probes it. |
| `components[].dokploy_app_id` | string or `null` | Required for `dokploy-*`. It is the application, compose or Postgres id. |
| `components[].monthly_cost_usd` | number or `null` | `null` means unknown. |
| `supabase.in_use` | boolean | `true` while any part of the project still depends on Supabase. |
| `supabase.used_for` | list | Required when `in_use` is `true`: `auth`, `db`, `storage`, `realtime`, `edge`. |
| `next_migration_step` | string or `null` | The single next step toward Cloudflare plus Dokploy. |
| `exempt` | `null` or `{ "reason": "..." }` | The only way to run on neither Dokploy nor Cloudflare. A reason is required. |

## Drift rules

The audit flags these. It never fixes them.

| Rule | Meaning |
|---|---|
| `unmanifested-live-app` | A Dokploy app or compose stack is live (`done` or `running`) and no manifest lists its id. |
| `manifest-url-down` | A manifest URL gives no answer or a status of 500 or more. |
| `supabase-in-use` | A manifest says Supabase is still in use. |
| `no-home` | A project has no Dokploy or Cloudflare component and no `exempt.reason`. |
| `stale-dokploy-id` | A manifest names a Dokploy id that no longer exists. |
| `cloudflare-unmanifested` | A Pages project, Worker, R2 bucket or D1 database exists and no manifest lists it. |
| `missing-manifest` | A tracked repo has no `hosting.json`. |
| `invalid-manifest` | A `hosting.json` breaks this schema. |

Safety rules:

- The audit is read-only. The one write is the tracking issue edit.
- PolySimulator production is never probed or audited. Manifests for it list staging only.
- The audit prints no secret. It keeps only ids, names, statuses and host names from Dokploy.

## Where it runs

- Repos: all `Wladefant` repos that are not archived, plus `Bavariance/polysimulator` (branch `staging`), and every clone directly under `C:/Users/wkiri/development`.
- Dokploy: `https://hosting.wladefant.de/api`, key `DOKPLOY_API_KEY`.
- Cloudflare: token in `~/.veyyon/shared-auth/cloudflare_api_token.txt`. Until that file exists the audit says so and skips Cloudflare.
- Output: the block between `<!-- hosting-audit:start -->` and `<!-- hosting-audit:end -->` in the issue "Projects live on Dokploy". A run that changes nothing makes no edit.
- Schedule: daily task `SuperboardHostingAudit`, started through `pythonw.exe` so no console window opens. Install it with `python workflows/portable/install_hosting_audit_task.py --script <path to hosting_audit.py>`.
