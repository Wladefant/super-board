---
name: supabase-staging-management-pat
description: "How to authenticate Supabase Management API calls for PolySimulator staging (hgzyqmaanndcimnclxtv): where the staging-only PAT lives, how to verify scope, which token to never use"
---

# Supabase staging Management PAT

Operator ruling 2026-09-20: always use the staging-scoped PAT; never mine transcripts again.

## Where
- Value file (secret, never print/commit): `C:\Users\wkiri\.veyyon\shared-auth\supabase_staging_management_pat.txt` (44 chars, prefix `sbp_[REDACTED]`).
- Read it into a variable and pass as `Authorization: Bearer $PAT`.

## Verify scope before first use
`GET https://api.supabase.com/v1/projects` must return ONLY `hgzyqmaanndcimnclxtv` (PolySimulator-Staging). If production `<prod-supabase-ref>` appears, you have the wrong token — stop.

## Use
- SQL (read-only or bounded staging writes per AGENTS.md §8): `POST /v1/projects/hgzyqmaanndcimnclxtv/database/query` body `{"query": "..."}`.
- Auth config: `GET/PATCH /v1/projects/hgzyqmaanndcimnclxtv/config/auth` (smtp_port must be a STRING).
- Logs: analytics endpoints for `auth_logs`, `edge_logs`, `postgres_logs` with explicit ISO windows.

## Never
- The other PAT `sbp_[REDACTED]` (found in PostDeployDaemonLogs.jsonl) sees production — do not use; ask operator to revoke.
- The Supabase MCP OAuth row (agent.db auth_credentials id=9) is expired and read-only; don't rely on it.
