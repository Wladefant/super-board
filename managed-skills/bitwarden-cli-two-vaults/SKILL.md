---
name: bitwarden-cli-two-vaults
description: "Access the operator's Bitwarden US and EU password vaults from the workstation CLI (bw-us / bw-eu), re-unlock them, and fetch secrets safely"
---

# Bitwarden CLI: two vaults

- `bw-us` is vault.bitwarden.com and `bw-eu` is vault.bitwarden.eu. Both are wrappers in `%LOCALAPPDATA%\Microsoft\WinGet\Links`.
- Each wrapper sets `BITWARDENCLI_APPDATA_DIR=~/.config/bw-<us|eu>` and reads `BW_SESSION` from `~/.veyyon/shared-auth/bw-<us|eu>.session` (user-only ACL).
- Check: `cmd /c "bw-us status"` should report `"status":"unlocked"`.
- Locked? Ask the operator (via Telegram) to run `bw-unlock us` or `bw-unlock eu` in a terminal. It prompts for the master password and saves a new session. Logged out? `bw-us login` (email + master password + emailed OTP), then `bw-unlock us`.
- Refresh: `bw-us sync`.
- Fetch: `bw-us list items --search <term>` (print only names/ids), `bw-us get password <id>` straight into an env var. Never echo secrets into transcripts, issues or logs.
- Never use the plain `bw` command: its default data dir is unconfigured.
- PolySimulator project secrets are in Bitwarden Secrets Manager: `bws secret list 1c1e3944-1deb-40b4-b6ff-b430015cb0e7`.
