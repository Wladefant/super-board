---
name: veyyon-mcp-server-health
description: "Use when a Veyyon MCP server (supabase, dokploy, github, playwright) reports 'not connected', when the operator asks whether MCPs are logged in, or after npm/npx cache cleanups break a stdio wrapper"
---

# Veyyon MCP server health (profile `default`)

Config: `C:/Users/wkiri/.veyyon/profiles/default/agent/mcp.json`. Every server is a stdio `node <wrapper>.js` in the same folder.

## Quick auth probes (no secrets printed)
- github: `mcp__github_get_me` -> login `Wladefant`.
- dokploy: `mcp__dokploy_user_session` -> user id + activeOrganizationId.
- supabase: `mcp__supabase_list_tables` (activate via `search_tool_bm25 "supabase list"`).

## Supabase wrapper facts
- Pinned to staging `hgzyqmaanndcimnclxtv`; PAT read from `~/.veyyon/secrets/supabase-staging-pat.txt`, else `~/.veyyon/shared-auth/supabase_staging_management_pat.txt`, else `SUPABASE_ACCESS_TOKEN`. Must start with `sbp_`.
- Server binary: `~/.veyyon/mcp-servers/supabase/node_modules/@supabase/mcp-server-supabase/dist/cli.js` (0.13.x). Never point the wrapper at an `npm-cache/_npx/...` path; npm evicts those and the server silently dies ("not connected").
- Reinstall: `npm install @supabase/mcp-server-supabase@latest` inside `~/.veyyon/mcp-servers/supabase`.

## Out-of-session smoke test (JS eval)
Spawn with the real node (`where node`), NOT `process.execPath` (that is veyyon.exe): send `initialize`, `notifications/initialized`, then `tools/call list_tables`; expect a JSON-RPC result with staging tables.

## After fixing on disk
The running session must run `/mcp reconnect supabase` (operator command in the veyyon prompt) or restart; the tool call error text tells you this. Tell the operator that exact command.
