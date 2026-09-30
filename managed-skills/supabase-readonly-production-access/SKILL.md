---
name: supabase-readonly-production-access
description: "Use when an agent needs to inspect PolySimulator production Supabase data/logs safely, when evaluating whether a Supabase PAT is read-only, or when the operator offers a signed-in browser profile instead of a token"
---

# Supabase read-only production access

## Facts (verified 2026-09-20)
- Supabase Management API PATs are NOT scopable. A PAT = full privileges of its owner account. There is no read-only PAT flag.
- Read-only access therefore comes from the **account**, not the token: invite a second account to the org with role `Read-only`, log in as it, create its PAT at https://supabase.com/dashboard/account/tokens.
- Staging-only PAT (`sbp_[REDACTED]`) is stored at `C:\Users\wkiri\.veyyon\shared-auth\supabase_staging_management_pat.txt`; it only sees `hgzyqmaanndcimnclxtv`. Never touch production `<prod-supabase-ref>` with a write-capable token; the old prod PAT `sbp_[REDACTED]` is purged and forbidden.

## Verify a token before trusting it as read-only
1. `GET https://api.supabase.com/v1/projects` -> must list only what is expected.
2. `GET https://api.supabase.com/v1/projects/<ref>/database/query` style read (bounded SELECT) -> 200.
3. Harmless write probe, e.g. `PATCH https://api.supabase.com/v1/projects/<ref>/config/auth` with an unchanged field -> MUST return 403. A 200 means the token can write: stop, report, do not store it as read-only.
4. Store the verified token next to the staging one with a `_readonly` suffix; never print its value.

## Browser-profile alternative (convenience only, not a safety control)
- Session browsers on this host are ephemeral headless Chromes (`%TEMP%\puppeteer_dev_chrome_profile-*`), signed into nothing. The operator's real Chrome Default profile is locked (cookie DB unreadable) and has no CDP port.
- To make dashboard reading reusable: `chrome.exe --user-data-dir=C:\Users\wkiri\.veyyon\chrome-ops --remote-debugging-port=9222`, sign in once, then attach with `browser open app.cdp_url=http://127.0.0.1:9222`.
- A signed-in dashboard session has the account's full write power. Only read pages; never click mutating controls on production. It does not replace the read-only account.

## Operator communication
- Ask for the invite + token via Telegram with the exact 3-step instruction; the operator does not watch the terminal.
