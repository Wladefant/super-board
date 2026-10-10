---
name: bitwarden-cli-two-vaults
description: "Access the operator's Bitwarden US and EU password vaults from the workstation CLI (bw-us / bw-eu), re-unlock them, and fetch secrets safely"
---

> Source of truth: [`managed-skills/bitwarden-cli-two-vaults/SKILL.md`](https://github.com/Wladefant/super-board/blob/main/managed-skills/bitwarden-cli-two-vaults/SKILL.md) in Wladefant/super-board. Edit it there and merge. Then `python scripts/install-managed-skills.py bitwarden-cli-two-vaults` copies it to `~/.veyyon/profiles/default/agent/managed-skills/`. The installer backs up a local edit and then overwrites it.

# Bitwarden CLI: two vaults

**The US vault (`bw-us`) is the operator's main vault (operator via Telegram, 2026-10-09, verbatim: "my main vault is the US one not the EU one. Everything you're sending, please send to the US").** Every new secret goes to `bw-us`. Never store new items only in `bw-eu`. Read from `bw-us` first; use `bw-eu` only to find older items that still need copying to US.

- `bw-us` is vault.bitwarden.com and `bw-eu` is vault.bitwarden.eu. Both are wrappers in `%LOCALAPPDATA%\Microsoft\WinGet\Links`.
- Each wrapper sets `BITWARDENCLI_APPDATA_DIR=~/.config/bw-<us|eu>` and reads `BW_SESSION` from `~/.veyyon/shared-auth/bw-<us|eu>.session` (user-only ACL).
- Check: `cmd /c "bw-us status"` should report `"status":"unlocked"`.
- Locked? Ask the operator (via Telegram) to run `bw-unlock us` or `bw-unlock eu` in a terminal. It prompts for the master password and saves a new session. Logged out? `bw-us login` (email + master password + emailed OTP), then `bw-unlock us`.
- Refresh: `bw-us sync`.
- Fetch: `bw-us list items --search <term>` (print only names/ids), `bw-us get password <id>` straight into an env var. Never echo secrets into transcripts, issues or logs.
- Never use the plain `bw` command: its default data dir is unconfigured.
- PolySimulator project secrets are in Bitwarden Secrets Manager: `bws secret list 1c1e3944-1deb-40b4-b6ff-b430015cb0e7`.
