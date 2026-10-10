---
name: veyyon-mcp-server-health
description: "Use when a Veyyon MCP server (supabase, dokploy, github, playwright, stripe, firecrawl, mail, pinthread) reports 'not connected', when the operator asks whether MCPs are logged in, or after npm/npx cache cleanups break a stdio wrapper"
---

# Veyyon MCP server health (profile `default`)

Config: `C:/Users/wkiri/.veyyon/profiles/default/agent/mcp.json`.
- Most servers are stdio wrappers, run as `node <wrapper>.js` in the same folder.
- Some servers are HTTP. Stripe is one of them.
- List every server's transport from the config. Do not assume that all of them are stdio.

## Server inventory (2026-10-08)
- **stdio, passed initialize + tools/list on 2026-10-08:**
  - supabase: 17 tools
  - github: 23 tools
  - dokploy: 604 tools
  - playwright: 25 tools
  - pinthread: 10 tools
  - firecrawl: 27 tools
  - goshen-mail: 9 tools
  - goshen-mail-send: 13 tools
- **HTTP:** stripe. When it returns HTTP 401 `missing_api_key`, the operator must run `/mcp reauth stripe` in the veyyon prompt (OAuth). Reinstalling does not fix it.
- **supabase-production-readonly:** NEVER probe it. Production is off-limits.

## Quick auth probes (no secrets printed)
- github: `mcp__github_get_me` returns login `Wladefant`.
- dokploy: `mcp__dokploy_user_session` returns the user id and activeOrganizationId.
- supabase: `mcp__supabase_list_tables`. Activate it first with `search_tool_bm25 "supabase list"`.

## Never run servers from the npx cache
Disk sweeps and npm evict `npm-cache/_npx/...`. The server then dies silently and shows "not connected".

Case, 2026-10-08: after the PC restart, dokploy failed with `MODULE_NOT_FOUND combined-stream` in the npx cache.

Each server runs from a stable install with an exact pin:
- **dokploy:**
  - Entry: `~/.veyyon/mcp-servers/dokploy/node_modules/@dokploy/mcp/build/index.js`.
  - Pin: `@dokploy/mcp@0.30.7`.
  - Reinstall: `npm install --prefix ~/.veyyon/mcp-servers/dokploy --save-exact @dokploy/mcp@0.30.7`.
  - The wrapper calls the entry directly with Node.
- **supabase:**
  - Entry: `~/.veyyon/mcp-servers/supabase/node_modules/@supabase/mcp-server-supabase/dist/cli.js`.
  - Reinstall: `npm install @supabase/mcp-server-supabase@latest` inside `~/.veyyon/mcp-servers/supabase`.
  - The wrapper is pinned to staging `hgzyqmaanndcimnclxtv`.
  - It reads the PAT from these places, in order:
    1. `~/.veyyon/secrets/supabase-staging-pat.txt`
    2. `~/.veyyon/shared-auth/supabase_staging_management_pat.txt`
    3. `SUPABASE_ACCESS_TOKEN`
  - The PAT must start with `sbp_`.

## Out-of-session smoke test
Spawn each stdio server with the real node (`where node`), NOT `process.execPath`, because that is veyyon.exe.
1. Send `initialize`.
2. Send `notifications/initialized`.
3. Send `tools/list`, and for supabase also `tools/call list_tables`.
4. Expect a JSON-RPC result. Give every spawn a timeout, and on Windows set windowsHide or CREATE_NO_WINDOW.

## After fixing on disk
The running session must run `/mcp reconnect <server>` (the operator types it in the veyyon prompt), or restart. The tool call error text tells you this. Tell the operator the exact command.

Until the reconnect, lanes can use the Dokploy REST API through the configured client, plus read-only staging SSH, for read-only attestation.
